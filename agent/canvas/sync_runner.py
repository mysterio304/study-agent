"""Full Canvas sync (SPEC §4): courses, assignments + submissions, groups, files, announcements,
inbox, missing submissions → DB + domain events.

Rules:
- network first, then one short DB transaction per scope (no lock held across HTTP calls);
- the first successful sync of a scope seeds state silently (no notification flood);
- a failing scope is recorded in `sync_state` and never blocks the others;
- slow-changing scopes (groups, files) refresh at most hourly.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.canvas.api import CanvasSource
from agent.canvas.client import CanvasError
from agent.canvas.differ import StoredAssignment, diff_assignment, ref
from agent.canvas.normalize import AssignmentSnap, assignment
from agent.canvas.structure import sync_course_structure
from agent.canvas.sync import content_hash, sync_courses
from agent.core.clock import Clock
from agent.core.text import strip_html
from agent.core.timeutil import TimeRange, parse_iso_utc, parse_time_range
from agent.db.models import (
    Announcement,
    Assignment,
    AssignmentGroup,
    Conversation,
    Course,
    SyncState,
)
from agent.notify.dispatch import enqueue_events
from agent.notify.events import Event, FileChanged, MissingFlagged, NewAnnouncement, NewMessage

log = logging.getLogger(__name__)

SLOW_SCOPE_INTERVAL = timedelta(hours=1)
DEFAULT_QUIET = parse_time_range("00:30-08:00")
ANNOUNCEMENT_LOOKBACK = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class SyncReport:
    events: list[Event]
    failed_scopes: list[str]


def _course_id(announcement: dict[str, Any]) -> int:
    return int(str(announcement.get("context_code", "course_0")).removeprefix("course_"))


class CanvasSync:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        source: CanvasSource,
        clock: Clock,
        *,
        quiet: Callable[[], Awaitable[TimeRange]] | None = None,
        summarise: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._sm, self._source, self._clock = sessionmaker, source, clock
        self._quiet_provider, self._summarise = quiet, summarise
        self._quiet = DEFAULT_QUIET
        self._self_id: int | None = None

    # --- scope bookkeeping -------------------------------------------------------------------

    async def _state(self, scope: str) -> SyncState | None:
        async with self._sm() as s:
            return await s.get(SyncState, scope)

    async def _due(self, scope: str, interval: timedelta | None) -> bool:
        st = await self._state(scope)
        if interval is None or st is None or st.last_success_at is None:
            return True
        return self._clock.now() - st.last_success_at >= interval

    async def _finish(
        self,
        s: AsyncSession,
        scope: str,
        events: list[Event],
        summaries: dict[int, str] | None = None,
    ) -> list[Event]:
        """Mark success and queue notifications in the SAME transaction as the new state.
        The scope's first (seeding) run is silent."""
        st = await s.get(SyncState, scope) or SyncState(scope=scope, initialized=False)
        first = not st.initialized
        st.initialized, st.last_success_at, st.last_error, st.consecutive_failures = (
            True,
            self._clock.now(),
            None,
            0,
        )
        s.add(st)
        if first:
            return []
        await enqueue_events(s, events, clock=self._clock, quiet=self._quiet, summaries=summaries)
        return events

    async def _fail(self, scope: str, exc: Exception) -> None:
        async with self._sm() as s:
            st = await s.get(SyncState, scope) or SyncState(scope=scope, initialized=False)
            st.last_error = f"{type(exc).__name__}: {exc}"[:500]
            st.consecutive_failures = (st.consecutive_failures or 0) + 1
            s.add(st)
            await s.commit()

    # --- entry point --------------------------------------------------------------------------

    async def run(self) -> SyncReport:
        events: list[Event] = []
        failed: list[str] = []
        if self._quiet_provider is not None:
            try:
                self._quiet = await self._quiet_provider()
            except (ValueError, SQLAlchemyError):
                log.warning("Invalid quiet hours setting; using the default %s", DEFAULT_QUIET)

        async def scope(name: str, coro: Awaitable[list[Event]]) -> None:
            try:
                events.extend(await coro)
            except (CanvasError, SQLAlchemyError, ValueError, KeyError, TypeError) as exc:
                log.warning("Canvas sync scope %s failed: %s", name, type(exc).__name__)
                failed.append(name)
                await self._fail(name, exc)

        await scope("courses", self._courses())
        async with self._sm() as s:
            courses = {
                c.id: (c.course_code or c.name) for c in (await s.scalars(select(Course))).all()
            }
        for cid, label in courses.items():
            if await self._due(f"groups:{cid}", SLOW_SCOPE_INTERVAL):  # before assignments (FK)
                await scope(f"groups:{cid}", self._groups(cid))
            await scope(f"assignments:{cid}", self._assignments(cid, label))
            if await self._due(f"files:{cid}", SLOW_SCOPE_INTERVAL):
                await scope(f"files:{cid}", self._files(cid, label))
        await scope("announcements", self._announcements(courses))
        await scope("conversations", self._conversations())
        await scope("missing", self._missing(courses))
        return SyncReport(events, failed)

    async def _courses(self) -> list[Event]:
        async with self._sm() as s:
            await sync_courses(s, self._source, self._clock)
            await s.commit()
        return []

    # --- assignments --------------------------------------------------------------------------

    async def _assignments(self, course_id: int, label: str) -> list[Event]:
        raw = await self._source.list_assignments(course_id)
        subs = await self._source.list_my_submissions(course_id) or []
        if self._self_id is None:
            self._self_id = await self._source.get_self_id()
        by_assignment = {s["assignment_id"]: s for s in subs if "assignment_id" in s}
        snaps = [assignment(a, course_id, by_assignment.get(a["id"])) for a in raw]
        now = self._clock.now()
        async with self._sm() as s:
            known_groups = set(
                (
                    await s.scalars(
                        select(AssignmentGroup.id).where(AssignmentGroup.course_id == course_id)
                    )
                ).all()
            )
            events: list[Event] = []
            for snap in snaps:
                row = await s.get(Assignment, snap.id)
                old = (
                    None
                    if row is None
                    else StoredAssignment(
                        row.due_at,
                        row.score,
                        row.grader_comment_count or 0,
                        row.is_missing,
                        row.user_state,
                    )
                )
                events += diff_assignment(
                    old, snap, course_name=label, self_id=self._self_id, now=now
                )
                self._store_assignment(s, row, snap, known_groups)
            events = await self._finish(s, f"assignments:{course_id}", events)
            await s.commit()
        return events

    def _store_assignment(
        self, s: AsyncSession, row: Assignment | None, snap: AssignmentSnap, known_groups: set[int]
    ) -> None:
        row = row or Assignment(id=snap.id)
        # Groups refresh hourly; link only to groups we already have (FK), the next sync fills it.
        group_id = snap.group_id if snap.group_id in known_groups else None
        row.course_id, row.group_id, row.name = snap.course_id, group_id, snap.name
        row.due_at, row.points_possible, row.html_url = (
            snap.due_at,
            snap.points_possible,
            snap.html_url,
        )
        row.submission_state, row.submitted_at = snap.workflow_state, snap.submitted_at
        row.score, row.grade, row.excused = snap.score, snap.grade, snap.excused
        # 'missing' can come from the submission or from /missing_submissions: keep it until submitted.
        still_missing = bool(row.is_missing) and not snap.submitted
        row.grader_comment_count, row.is_missing = (
            len(snap.comments),
            snap.is_missing or still_missing,
        )
        row.omit_from_final_grade, row.submission_types = (
            snap.omit_from_final_grade,
            list(snap.submission_types),
        )
        row.stats_json, row.synced_at = snap.stats, self._clock.now()
        row.content_hash = content_hash(snap.hash_payload())
        s.add(row)

    async def _groups(self, course_id: int) -> list[Event]:
        groups = await self._source.list_assignment_groups(course_id)
        async with self._sm() as s:
            for g in groups:
                row = await s.get(AssignmentGroup, g["id"]) or AssignmentGroup(id=g["id"])
                row.course_id, row.name = course_id, str(g.get("name") or "")[:255]
                row.group_weight, row.rules_json = g.get("group_weight"), g.get("rules") or None
                row.content_hash = content_hash([row.name, row.group_weight, row.rules_json])
                s.add(row)
            await self._finish(s, f"groups:{course_id}", [])
            await s.commit()
        return []

    async def _files(self, course_id: int, label: str) -> list[Event]:
        changed = await sync_course_structure(self._sm, self._source, course_id)
        async with self._sm() as s:
            events: list[Event] = [
                FileChanged(label, name, is_new, stamp, fid) for fid, name, is_new, stamp in changed
            ]
            events = await self._finish(s, f"files:{course_id}", events)
            await s.commit()
        return events

    # --- announcements, inbox, missing --------------------------------------------------------

    async def _announcements(self, courses: dict[int, str]) -> list[Event]:
        since = self._clock.today() - ANNOUNCEMENT_LOOKBACK
        raw = await self._source.list_announcements(list(courses), since)
        async with self._sm() as s:  # short read: which are new, which courses are seeded
            known = set((await s.scalars(select(Announcement.id))).all())
            seeded = {
                int(st.scope.removeprefix("announcements:"))
                for st in (
                    await s.scalars(
                        select(SyncState).where(SyncState.scope.like("announcements:%"))
                    )
                ).all()
                if st.initialized
            }
        fresh = [a for a in raw if a["id"] not in known]
        texts = {a["id"]: strip_html(a.get("message")) for a in fresh}
        summaries: dict[int, str] = {}
        if self._summarise is not None:  # LLM before the write transaction
            for a in fresh:
                if _course_id(a) in seeded and (summary := await self._summarise(texts[a["id"]])):
                    summaries[a["id"]] = summary
        async with self._sm() as s:
            events: list[Event] = []
            for a in fresh:
                cid = _course_id(a)
                s.add(
                    Announcement(
                        id=a["id"],
                        course_id=cid,
                        title=str(a.get("title") or "")[:500],
                        message_html=a.get("message"),
                        posted_at=parse_iso_utc(a.get("posted_at")),
                        html_url=a.get("html_url"),
                        summary_ru=summaries.get(a["id"]),
                        content_hash=content_hash(texts[a["id"]]),
                    )
                )
                if cid in seeded:  # a course seen for the first time is seeded silently
                    events.append(
                        NewAnnouncement(
                            a["id"],
                            courses.get(cid, "Canvas"),
                            str(a.get("title") or ""),
                            texts[a["id"]],
                            a.get("html_url"),
                        )
                    )
            for cid in courses.keys() - seeded:
                s.add(
                    SyncState(
                        scope=f"announcements:{cid}",
                        initialized=True,
                        last_success_at=self._clock.now(),
                    )
                )
            events = await self._finish(s, "announcements", events, summaries)
            await s.commit()
        return events

    async def _conversations(self) -> list[Event]:
        raw = await self._source.list_conversations()
        if raw is None:
            return []
        async with self._sm() as s:
            events: list[Event] = []
            for c in raw:
                last = parse_iso_utc(c.get("last_message_at"))
                row = await s.get(Conversation, c["id"])
                is_new_message = row is None or (
                    last is not None and (row.last_message_at is None or last > row.last_message_at)
                )
                row = row or Conversation(id=c["id"])
                row.subject, row.last_message = (
                    str(c.get("subject") or "")[:500],
                    c.get("last_message"),
                )
                row.last_message_at, row.workflow_state = last, c.get("workflow_state")
                row.content_hash = content_hash([row.subject, row.last_message, str(last)])
                s.add(row)
                if is_new_message and c.get("workflow_state") == "unread":
                    names = [p.get("name", "") for p in c.get("participants") or []][:4]
                    events.append(
                        NewMessage(
                            c["id"], row.subject or "(без темы)", row.last_message or "", names
                        )
                    )
            events = await self._finish(s, "conversations", events)
            await s.commit()
        return events

    async def _missing(self, courses: dict[int, str]) -> list[Event]:
        raw = await self._source.list_missing_submissions()
        async with self._sm() as s:
            events: list[Event] = []
            for a in raw:
                row = await s.get(Assignment, a["id"])
                if row is None or row.is_missing or row.user_state is not None:
                    continue  # unknown course, already flagged, or closed by the student
                row.is_missing = True
                snap = assignment(a, row.course_id, {"missing": True})
                events.append(MissingFlagged(ref(snap, courses.get(row.course_id, "Canvas"))))
            events = await self._finish(s, "missing", events)
            await s.commit()
        return events
