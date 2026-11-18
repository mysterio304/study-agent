"""Canvas client: read-only guard, pagination, auth handling, error mapping."""

from __future__ import annotations

import httpx
import pytest
import respx

from agent.canvas.client import (
    CanvasAccessDenied,
    CanvasClient,
    CanvasError,
    ReadOnlyViolation,
)

BASE = "https://canvas.example.edu"
TOKEN = "test-token-not-real-123456"


@pytest.fixture
async def client():
    c = CanvasClient(BASE, TOKEN, max_retries=0)
    yield c
    await c.aclose()


# --- read-only guard --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", "OPTIONS"])
async def test_request_refuses_non_get_methods(client: CanvasClient, method: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(ReadOnlyViolation):
            await client.request(method, "/api/v1/courses/1/assignments/2/submissions")
        assert not route.called, "a write request must never reach the network"


async def test_underlying_http_client_cannot_be_used_to_write(client: CanvasClient) -> None:
    """Even bypassing `request()`, the transport-level hook blocks writes."""
    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(ReadOnlyViolation):
            await client._http.post(f"{BASE}/api/v1/conversations", json={"body": "hi"})
        assert not route.called


# --- auth & pagination ------------------------------------------------------------------------


@respx.mock
async def test_get_json_sends_bearer_token(client: CanvasClient) -> None:
    route = respx.get(f"{BASE}/api/v1/users/self").mock(
        return_value=httpx.Response(200, json={"id": 7})
    )
    assert await client.get_json("/api/v1/users/self") == {"id": 7}
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


@respx.mock
async def test_paginate_follows_link_header_until_no_next(client: CanvasClient) -> None:
    page2 = f"{BASE}/api/v1/courses?page=2&per_page=100"
    respx.get(f"{BASE}/api/v1/courses", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=[{"id": 3}])
    )
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1}, {"id": 2}],
            headers={"link": f'<{page2}>; rel="next", <{BASE}/api/v1/courses?page=1>; rel="first"'},
        )
    )
    items = await client.get_paginated("/api/v1/courses", {"enrollment_state": "active"})
    assert [i["id"] for i in items] == [1, 2, 3]


@respx.mock
async def test_paginate_passes_list_params_and_per_page(client: CanvasClient) -> None:
    route = respx.get(f"{BASE}/api/v1/courses").mock(return_value=httpx.Response(200, json=[]))
    await client.get_paginated(
        "/api/v1/courses", {"enrollment_state": "active", "include[]": ["total_scores", "term"]}
    )
    query = route.calls.last.request.url.params
    assert query.get_list("include[]") == ["total_scores", "term"]
    assert query["per_page"] == "100"


@respx.mock
async def test_paginate_refuses_next_link_to_foreign_host(client: CanvasClient) -> None:
    """The token must never be sent to another host, even if a Link header points there."""
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1}], headers={"Link": '<https://evil.example.com/x>; rel="next"'}
        )
    )
    with pytest.raises(CanvasError, match="foreign host"):
        await client.get_paginated("/api/v1/courses")


# --- errors -----------------------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_hidden_tabs_raise_access_denied(client: CanvasClient, status: int) -> None:
    respx.get(f"{BASE}/api/v1/courses/5/files").mock(
        return_value=httpx.Response(status, json={"errors": [{"message": "unauthorized"}]})
    )
    with pytest.raises(CanvasAccessDenied) as exc:
        await client.get_paginated("/api/v1/courses/5/files")
    assert exc.value.status == status


@respx.mock
async def test_optional_returns_none_on_access_denied(client: CanvasClient) -> None:
    respx.get(f"{BASE}/api/v1/courses/5/files").mock(return_value=httpx.Response(403))
    assert await client.optional(client.get_paginated("/api/v1/courses/5/files")) is None


@respx.mock
async def test_server_error_raises_canvas_error_without_token(client: CanvasClient) -> None:
    respx.get(f"{BASE}/api/v1/courses").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(CanvasError) as exc:
        await client.get_paginated("/api/v1/courses")
    assert TOKEN not in str(exc.value)


@respx.mock
async def test_retries_on_429_then_succeeds() -> None:
    c = CanvasClient(BASE, TOKEN, max_retries=2, backoff_base=0)
    route = respx.get(f"{BASE}/api/v1/users/self").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"id": 1})]
    )
    try:
        assert await c.get_json("/api/v1/users/self") == {"id": 1}
        assert route.call_count == 2
    finally:
        await c.aclose()


# --- file download ----------------------------------------------------------------------------


@respx.mock
async def test_download_follows_redirect_without_leaking_token(
    client: CanvasClient, tmp_path
) -> None:
    file_url = f"{BASE}/files/99/download?download_frd=1"
    cdn_url = "https://cdn.files.example.net/abc/lecture1.pdf"
    respx.get(file_url).mock(return_value=httpx.Response(302, headers={"Location": cdn_url}))
    cdn = respx.get(cdn_url).mock(return_value=httpx.Response(200, content=b"%PDF-1.7 data"))

    dest = tmp_path / "lecture1.pdf"
    size = await client.download(file_url, dest)

    assert dest.read_bytes() == b"%PDF-1.7 data"
    assert size == len(b"%PDF-1.7 data")
    assert "Authorization" not in cdn.calls.last.request.headers


def test_constructor_rejects_http_base_url() -> None:
    with pytest.raises(ValueError):
        CanvasClient("http://canvas.example.edu", TOKEN)


def test_repr_does_not_contain_token() -> None:
    c = CanvasClient(BASE, TOKEN)
    assert TOKEN not in repr(c)
