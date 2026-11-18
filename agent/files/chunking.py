"""Segments → chunks of at most RAG_CHUNK_BUDGET tokens (SPEC §9a.4), keeping locators.

Long pages are split on paragraph/sentence boundaries ("p. 12", "p. 12 (2)"); runs of tiny
slides are merged so search hits carry enough context ("slides 3–5").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent.brain.budgets import RAG_CHUNK_BUDGET, estimate_tokens, truncate_to_tokens
from agent.files.extract import Segment

MERGE_BELOW_TOKENS = 120


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    locator: str
    text: str


def _split_long(seg: Segment, budget: int) -> list[ChunkDraft]:
    pieces = re.split(r"(?<=[.!?])\s+|\n", seg.text)
    out, buf = [], ""
    for piece in pieces:
        candidate = f"{buf} {piece}".strip()
        if estimate_tokens(candidate) <= budget:
            buf = candidate
            continue
        if buf:
            out.append(buf)
        buf = piece if estimate_tokens(piece) <= budget else truncate_to_tokens(piece, budget)
    if buf:
        out.append(buf)
    if len(out) == 1:
        return [ChunkDraft(seg.locator, out[0])]
    return [
        ChunkDraft(seg.locator if i == 0 else f"{seg.locator} ({i + 1})", t)
        for i, t in enumerate(out)
    ]


def _merged_locator(first: str, last: str) -> str:
    if first == last:
        return first
    kind, _, a = first.partition(" ")
    _, _, b = last.partition(" ")
    plural = {"slide": "slides", "p.": "pp."}.get(kind, kind)
    return f"{plural} {a}–{b}"


def chunk_segments(segments: list[Segment], budget: int = RAG_CHUNK_BUDGET) -> list[ChunkDraft]:
    out: list[ChunkDraft] = []
    run: list[Segment] = []

    def flush_run() -> None:
        if run:
            out.append(
                ChunkDraft(
                    _merged_locator(run[0].locator, run[-1].locator), "\n".join(s.text for s in run)
                )
            )
            run.clear()

    for seg in segments:
        tokens = estimate_tokens(seg.text)
        if tokens > budget:
            flush_run()
            out.extend(_split_long(seg, budget))
            continue
        if (
            tokens < MERGE_BELOW_TOKENS
            and estimate_tokens("\n".join([*(s.text for s in run), seg.text])) <= budget
        ):
            run.append(seg)
            continue
        flush_run()
        if tokens < MERGE_BELOW_TOKENS:
            run.append(seg)
        else:
            out.append(ChunkDraft(seg.locator, seg.text))
    flush_run()
    return out
