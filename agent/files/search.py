"""Retrieval over course files. `Retriever` is the interface (embeddings can be added later);
`FtsRetriever` is SQLite FTS5 with BM25 ranking (SPEC §7, ARCHITECTURE D9)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent.brain.budgets import RAG_CHUNK_BUDGET, RAG_MAX_CHUNKS, truncate_to_tokens

_WORD_RE = re.compile(r"[\w\-]{2,}", re.UNICODE)
_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "what",
        "how",
        "does",
        "why",
        "is",
        "are",
        "was",
        "were",
        "of",
        "to",
        "in",
        "on",
        "a",
        "an",
        "что",
        "как",
        "это",
        "для",
        "или",
        "и",
        "в",
        "на",
        "по",
        "не",
        "почему",
        "зачем",
        "объясни",
        "расскажи",
    ]
)


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: int
    file_name: str
    locator: str
    text: str
    score: float

    def citation(self) -> str:
        return f"{self.file_name}, {self.locator}"


class Retriever(Protocol):
    async def search(
        self,
        session: AsyncSession,
        query: str,
        *,
        course_id: int | None = None,
        limit: int = RAG_MAX_CHUNKS,
        kinds: tuple[str, ...] | None = None,
    ) -> list[Hit]: ...


def fts_query(query: str) -> str | None:
    """User text → safe FTS5 MATCH expression: quoted terms joined by OR (no operator injection)."""
    terms = [t for t in (w.lower() for w in _WORD_RE.findall(query)) if t not in _STOP]
    if not terms:
        return None
    unique = list(dict.fromkeys(terms))[:12]
    return " OR ".join('"' + t.replace('"', "") + '"' for t in unique)


class FtsRetriever:
    async def search(
        self,
        session: AsyncSession,
        query: str,
        *,
        course_id: int | None = None,
        limit: int = RAG_MAX_CHUNKS,
        kinds: tuple[str, ...] | None = None,
    ) -> list[Hit]:
        match = fts_query(query)
        if match is None:
            return []
        sql = (
            "SELECT c.id, f.display_name, c.locator, c.text, bm25(chunks_fts) AS score "
            "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
            "JOIN course_files f ON f.id = c.file_id "
            "WHERE chunks_fts MATCH :match"
        )
        params: dict[str, object] = {"match": match, "limit": max(1, min(limit, RAG_MAX_CHUNKS))}
        if course_id is not None:
            sql += " AND c.course_id = :course"
            params["course"] = course_id
        if kinds:
            sql += " AND f.kind IN (" + ", ".join(f":k{i}" for i in range(len(kinds))) + ")"
            params |= {f"k{i}": k for i, k in enumerate(kinds)}
        sql += " ORDER BY score LIMIT :limit"
        rows = (await session.execute(text(sql), params)).all()
        return [
            Hit(r[0], r[1], r[2], truncate_to_tokens(r[3], RAG_CHUNK_BUDGET), float(r[4]))
            for r in rows
        ]

    async def count_hits(
        self, session: AsyncSession, query: str, *, course_id: int, kinds: tuple[str, ...]
    ) -> int:
        """How many chunks of the given kinds match (used for past-paper topic frequency)."""
        match = fts_query(query)
        if match is None:
            return 0
        sql = (
            "SELECT count(*) FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
            "JOIN course_files f ON f.id = c.file_id WHERE chunks_fts MATCH :match AND c.course_id = :course "
            "AND f.kind IN (" + ", ".join(f":k{i}" for i in range(len(kinds))) + ")"
        )
        params = {"match": match, "course": course_id} | {f"k{i}": k for i, k in enumerate(kinds)}
        return int((await session.execute(text(sql), params)).scalar() or 0)
