"""Download → extract → chunk → index (SPEC §7). Runs as a scheduled job a few files at a time.

- only files of courses mapped to an exam, only PDF/PPTX/DOCX, size-capped;
- network and CPU work (download, extraction in a worker thread) happen outside DB transactions;
- a file's chunks are replaced atomically; changed files (index_state reset to 'pending' by the
  Canvas sync) are re-indexed automatically;
- states: pending → indexed | no_text (scanned PDF) | unsupported | unavailable | failed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.canvas.api import CanvasSource, FileUnavailable
from agent.canvas.client import CanvasError
from agent.core.clock import Clock
from agent.db.models import Chunk, CourseFile, Exam
from agent.files.chunking import chunk_segments
from agent.files.extract import ExtractionError, extract, kind_of

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ATTEMPTS_STATE = "failed"


@dataclass(frozen=True, slots=True)
class IndexResult:
    file_id: int
    name: str
    state: str
    chunks: int


def safe_name(name: str) -> str:
    stem = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._")
    return stem[:120] or "file"


def file_path(data_dir: Path, row: CourseFile) -> Path:
    key = row.canvas_file_id or f"upload{row.id}"
    return data_dir / "files" / str(row.course_id) / f"{key}-{safe_name(row.display_name)}"


async def index_local_file(
    sessionmaker: async_sessionmaker, row_id: int, path: Path, clock: Clock
) -> IndexResult:
    """Extract + chunk + store for a file already on disk."""
    async with sessionmaker() as s:
        row = await s.get(CourseFile, row_id)
        name, course_id = row.display_name, row.course_id
    try:
        segments = await asyncio.to_thread(extract, path, name)
        state = "indexed" if segments else "no_text"
    except ExtractionError as exc:
        log.info("Extraction failed for %s: %s", name, exc)
        segments, state = [], "failed"
    drafts = await asyncio.to_thread(chunk_segments, segments)  # CPU-bound for big files
    try:
        digest = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
    except OSError:
        digest, state = None, "failed"  # file vanished (e.g. cleaned up) — never leave it pending
    async with sessionmaker() as s:
        await s.execute(delete(Chunk).where(Chunk.file_id == row_id))
        s.add_all(
            Chunk(
                file_id=row_id, course_id=course_id, ordinal=i, locator=d.locator[:64], text=d.text
            )
            for i, d in enumerate(drafts)
        )
        row = await s.get(CourseFile, row_id)
        row.local_path, row.sha256, row.index_state, row.indexed_at = (
            str(path),
            digest,
            state,
            clock.now(),
        )
        await s.commit()
    return IndexResult(row_id, name, state, len(drafts))


async def index_pending(
    sessionmaker: async_sessionmaker,
    source: CanvasSource,
    data_dir: Path,
    clock: Clock,
    *,
    limit: int = 5,
) -> list[IndexResult]:
    async with sessionmaker() as s:
        exam_courses = set(
            (await s.scalars(select(Exam.course_id).where(Exam.course_id.is_not(None)))).all()
        )
        rows = [
            (r.id, r.canvas_file_id, r.display_name, file_path(data_dir, r))
            for r in (
                await s.scalars(
                    select(CourseFile)
                    .where(
                        CourseFile.index_state == "pending", CourseFile.course_id.in_(exam_courses)
                    )
                    .order_by(CourseFile.kind.desc(), CourseFile.id)  # past papers first
                )
            ).all()
        ]
    results: list[IndexResult] = []
    for row_id, canvas_id, name, path in rows:
        if len(results) >= limit:
            break
        if kind_of(name) is None:
            await _set_state(sessionmaker, row_id, "unsupported")
            continue
        if canvas_id is None:
            continue  # uploads are indexed directly when received
        try:
            await source.download_file(canvas_id, path, max_bytes=MAX_FILE_BYTES)
        except FileUnavailable as exc:
            log.info("Skipping %s: %s", name, exc)
            await _set_state(sessionmaker, row_id, "unavailable")
            continue
        except CanvasError as exc:
            log.warning("Download failed for %s: %s", name, type(exc).__name__)
            continue  # stays pending; retried next run
        results.append(await index_local_file(sessionmaker, row_id, path, clock))
    return results


async def _set_state(sessionmaker: async_sessionmaker, row_id: int, state: str) -> None:
    async with sessionmaker() as s:
        (await s.get(CourseFile, row_id)).index_state = state
        await s.commit()
