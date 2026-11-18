"""Small text helpers shared by Canvas sync, notifications and discovery."""

from __future__ import annotations

import hashlib
import html
import re


def strip_html(value: str | None) -> str:
    """Canvas HTML → readable plain text (paragraphs kept as newlines)."""
    text = re.sub(r"<(br|p|li|h\d)[^>]*>", "\n", value or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def shorten(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def stable_hash(text: str, length: int = 12) -> str:
    """Process-independent short hash (Python's hash() is salted per process)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]
