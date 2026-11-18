"""Canvas → DB synchronisation.

M1 covers courses (needed for course mapping and grade context). Assignments, announcements,
files and inbox sync with diffing/notifications arrive in M3.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agent.canvas.api import CanvasSource
from agent.core.clock import Clock
from agent.db.models import Course, SyncState

log = logging.getLogger(__name__)


def content_hash(payload: Any) -> str:
    """Stable hash of a JSON-serialisable payload (key order independent)."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


async def _mark_success(session: AsyncSession, scope: str, clock: Clock) -> SyncState:
    state = await session.get(SyncState, scope) or SyncState(scope=scope)
    state.initialized = True
    state.last_success_at = clock.now()
    state.last_error = None
    state.consecutive_failures = 0
    session.add(state)
    return state


async def sync_courses(session: AsyncSession, source: CanvasSource, clock: Clock) -> list[int]:
    """Upsert active courses. Returns ids of courses that are new or changed."""
    changed: list[int] = []
    for course in await source.list_courses():
        data = asdict(course)
        digest = content_hash(data)
        row = await session.get(Course, course.id)
        if row is not None and row.content_hash == digest:
            continue
        row = row or Course(id=course.id)
        row.name = course.name
        row.course_code = course.course_code
        row.current_score = course.current_score
        row.current_grade = course.current_grade
        row.apply_group_weights = course.apply_group_weights
        row.syllabus_html = course.syllabus_html
        row.content_hash = digest
        row.synced_at = clock.now()
        session.add(row)
        changed.append(course.id)
    await _mark_success(session, "courses", clock)
    await session.flush()
    log.info("Course sync: %d new/changed", len(changed))
    return changed
