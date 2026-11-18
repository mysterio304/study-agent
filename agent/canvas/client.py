"""Read-only async Canvas LMS client.

Safety properties (each covered by tests in tests/canvas/test_client.py):

1. **Read-only.** `request()` refuses every method other than GET, and an httpx request hook
   re-checks every outgoing request, so even direct use of the underlying client cannot write.
2. **Token confinement.** The bearer token is only ever sent to the configured Canvas host.
   Pagination links to other hosts are refused; file downloads that redirect to a file CDN
   are fetched with a separate client that has no credentials.
3. **No secrets in errors or reprs.**
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Any, TypeVar

import httpx

log = logging.getLogger(__name__)

T = TypeVar("T")
Params = Mapping[str, str | int | list[str]] | None

_ALLOWED_METHODS = frozenset({"GET"})
_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_PAGES = 200  # hard stop against pagination loops


class CanvasError(RuntimeError):
    """Any Canvas failure. Messages never include the access token."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CanvasAccessDenied(CanvasError):
    """401/403/404 — typically a course tab hidden from students. Callers may fall back."""


class ReadOnlyViolation(CanvasError):
    """Raised before any non-GET request leaves the process."""


def parse_link_header(value: str | None) -> dict[str, str]:
    """Parse an RFC 5988 Link header into {rel: url}."""
    if not value:
        return {}
    return {rel: url for url, rel in _LINK_RE.findall(value)}


class CanvasClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        per_page: int = 100,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base_url = base_url.rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("Canvas base URL must use https://")
        self._base = httpx.URL(base_url)
        self._per_page = per_page
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        hooks = {"request": [self._guard_request]}
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=False,
            event_hooks=hooks,
            transport=transport,
        )
        # Credential-free client for file CDNs that Canvas redirects downloads to.
        self._bare = httpx.AsyncClient(
            timeout=timeout * 4, follow_redirects=True, event_hooks=hooks, transport=transport
        )

    def __repr__(self) -> str:
        return f"CanvasClient(base_url={str(self._base)!r})"

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._bare.aclose()

    async def __aenter__(self) -> CanvasClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- guards -------------------------------------------------------------------------------

    async def _guard_request(self, request: httpx.Request) -> None:
        if request.method.upper() not in _ALLOWED_METHODS:
            raise ReadOnlyViolation(f"Canvas client is read-only; refused {request.method}")
        if "authorization" in request.headers and request.url.host != self._base.host:
            raise CanvasError("Refusing to send Canvas credentials to a foreign host")

    def _same_host(self, url: httpx.URL) -> bool:
        return url.scheme == "https" and url.host == self._base.host

    # --- core request -------------------------------------------------------------------------

    async def request(self, method: str, url: str, params: Params = None) -> httpx.Response:
        """Perform a request with retries. Only GET is permitted."""
        if method.upper() not in _ALLOWED_METHODS:
            raise ReadOnlyViolation(f"Canvas client is read-only; refused {method}")
        attempt = 0
        while True:
            try:
                response = await self._http.request("GET", url, params=params)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise CanvasError(
                        f"Network error talking to Canvas: {type(exc).__name__}"
                    ) from None
                await self._sleep_backoff(attempt, None)
                attempt += 1
                continue
            if response.status_code in _RETRY_STATUSES and attempt < self._max_retries:
                await self._sleep_backoff(attempt, response.headers.get("Retry-After"))
                attempt += 1
                continue
            self._raise_for_status(response)
            return response

    async def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self._backoff_base * (2**attempt)
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        await asyncio.sleep(min(delay, 60.0))

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        path = response.request.url.path
        if status in (401, 403, 404):
            raise CanvasAccessDenied(f"Canvas denied access to {path} ({status})", status)
        raise CanvasError(f"Canvas error {status} for {path}", status)

    # --- helpers ------------------------------------------------------------------------------

    async def get_json(self, path: str, params: Params = None) -> Any:
        return (await self.request("GET", path, params)).json()

    async def get_paginated(self, path: str, params: Params = None) -> list[Any]:
        query: dict[str, Any] = dict(params or {})
        query.setdefault("per_page", self._per_page)
        response = await self.request("GET", path, query)
        items: list[Any] = list(response.json())
        for _ in range(_MAX_PAGES):
            next_url = parse_link_header(response.headers.get("link")).get("next")
            if not next_url:
                return items
            if not self._same_host(httpx.URL(next_url)):
                raise CanvasError("Pagination link points to a foreign host; refusing")
            response = await self.request("GET", next_url)  # opaque URL, already has params
            items.extend(response.json())
        raise CanvasError(f"Pagination exceeded {_MAX_PAGES} pages for {path}")

    @staticmethod
    async def optional(call: Awaitable[T]) -> T | None:
        """Await `call`; return None if Canvas denies access (hidden tab)."""
        try:
            return await call
        except CanvasAccessDenied as exc:
            log.info("Canvas access denied (%s); treating as unavailable", exc.status)
            return None

    async def download(self, url: str, dest: Path, *, max_bytes: int = 200 * 1024 * 1024) -> int:
        """Download a file to `dest`. Canvas typically 302-redirects to a file CDN; that hop
        is fetched without credentials. Returns the number of bytes written."""
        target = httpx.URL(url)
        if self._same_host(target):
            first = await self.request("GET", url)
            if first.is_redirect:
                target = (
                    first.next_request.url
                    if first.next_request
                    else httpx.URL(first.headers["Location"])
                )
            else:
                return await asyncio.to_thread(_write_capped, dest, first.content, max_bytes)
        if self._same_host(target):
            response = await self.request("GET", str(target))
            return await asyncio.to_thread(_write_capped, dest, response.content, max_bytes)
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        too_big = False
        async with self._bare.stream("GET", str(target)) as response:
            self._raise_for_status(response)
            with dest.open("wb") as fh:
                async for chunk in response.aiter_bytes():
                    written += len(chunk)
                    if written > max_bytes:
                        too_big = True
                        break
                    fh.write(chunk)
        if too_big:
            dest.unlink(missing_ok=True)  # noqa: ASYNC240 - tiny local fs op on the error path
            raise CanvasError(f"File exceeds {max_bytes} bytes; aborted")
        return written


def _write_capped(dest: Path, content: bytes, max_bytes: int) -> int:
    if len(content) > max_bytes:
        raise CanvasError(f"File exceeds {max_bytes} bytes; aborted")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return len(content)
