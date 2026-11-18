"""DB-facing planner operations: load inputs, propose, apply, undo.

The only way a plan reaches the `blocks` table is `apply_proposal()` (called from a Telegram
button) or `auto_move_missed()` (small_auto mode, one block, user notified). The LLM can only
create proposals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import Settings
from agent.core.clock import Clock
from agent.core.timeutil import local_to_utc, parse_hhmm, parse_time_range
from agent.db.models import Block, BusySlot, Course, Exam, PlanVersion, Proposal, Setting, Topic
from agent.planner.allocator import PlanResult, build_plan
from agent.planner.model import ExamInfo, Interval, PlanConfig, PlannedBlock, TopicInfo
from agent.planner.render import render_diff
from agent.planner.validators import validate_plan
from agent.prep.settings_service import effective

log = logging.getLogger(__name__)

OVERRIDES_KEY = "plan_overrides"
PROPOSAL_TTL = timedelta(hours=36)


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlanInputs:
    exams: list[ExamInfo]
    topics: list[TopicInfo]
    busy: list[Interval]
    cfg: PlanConfig
    exam_names: dict[int, str]
    topic_names: dict[int, str]


# --- overrides (pauses / day caps), persisted only when a proposal is applied -----------------


async def get_overrides(session: AsyncSession) -> dict[str, dict[str, str | float]]:
    row = await session.get(Setting, OVERRIDES_KEY)
    raw = row.value_json if row else {}
    return {
        "paused_until": dict(raw.get("paused_until", {})),
        "day_caps": dict(raw.get("day_caps", {})),
    }


async def _set_overrides(session: AsyncSession, value: dict[str, Any], clock: Clock) -> None:
    row = await session.get(Setting, OVERRIDES_KEY) or Setting(key=OVERRIDES_KEY)
    row.value_json, row.updated_at, row.updated_by, row.is_locked = (
        value,
        clock.now(),
        "proposal",
        False,
    )
    session.add(row)


def _cfg_with_overrides(cfg: PlanConfig, overrides: dict[str, dict[str, Any]]) -> PlanConfig:
    paused = {int(k): date.fromisoformat(v) for k, v in overrides["paused_until"].items()}
    caps = {date.fromisoformat(k): float(v) for k, v in overrides["day_caps"].items()}
    return replace(cfg, paused_until=MappingProxyType(paused), day_caps=MappingProxyType(caps))


# --- inputs ----------------------------------------------------------------------------------


async def load_inputs(session: AsyncSession, settings: Settings, clock: Clock) -> PlanInputs:
    eff = await effective(session, settings)
    cfg = PlanConfig(
        zone=settings.zone,
        study_window=parse_time_range(eff["study_window"]),
        quiet_hours=parse_time_range(eff["quiet_hours"]),
        cap_hours=float(eff["cap_hours"]),
        final_week_cap_hours=float(eff["final_week_cap_hours"]),
        between_exams_cap_hours=float(eff["between_exams_cap_hours"]),
        lighter_weekday=int(eff["lighter_weekday"]),
        eve_review_only=bool(eff["eve_review_only"]),
        no_new_topics_eve=bool(eff["no_new_topics_eve"]),
        protected_sleep=bool(eff["protected_sleep"]),
        no_blocks_during_exams=bool(eff["no_blocks_during_exams"]),
    )
    cfg = _cfg_with_overrides(cfg, await get_overrides(session))

    rows = (
        await session.execute(select(Exam, Course).outerjoin(Course, Exam.course_id == Course.id))
    ).all()
    exams = [
        ExamInfo(
            e.id,
            e.title,
            e.starts_at,
            e.duration_min,
            e.final_weight,
            e.target_grade,
            c.current_score if c else None,
        )
        for e, c in sorted(rows, key=lambda r: r[0].starts_at)
    ]
    topic_rows = (await session.scalars(select(Topic).where(Topic.status == "active"))).all()
    topics = [
        TopicInfo(
            t.id,
            t.exam_id,
            t.name,
            t.effort_hours,
            t.importance,
            t.level,
            t.target_level,
            t.last_studied_at,
            t.next_review_at,
            t.progress_min,
            t.review_step,
        )
        for t in topic_rows
    ]
    last_day = max((e.starts_at.astimezone(cfg.zone).date() for e in exams), default=clock.today())
    busy = await _busy_intervals(session, clock.today(), last_day, cfg)
    return PlanInputs(
        exams, topics, busy, cfg, {e.id: e.title for e in exams}, {t.id: t.name for t in topics}
    )


async def _busy_intervals(
    session: AsyncSession, start: date, end: date, cfg: PlanConfig
) -> list[Interval]:
    out: list[Interval] = []
    for slot in (await session.scalars(select(BusySlot))).all():
        d = start
        while d <= end:
            in_range = (slot.valid_from is None or d >= slot.valid_from) and (
                slot.valid_until is None or d <= slot.valid_until
            )
            matches = slot.on_date == d if slot.on_date else slot.weekday == d.weekday()
            if in_range and matches:
                out.append(
                    Interval(
                        local_to_utc(d, parse_hhmm(slot.start_local), cfg.zone),
                        local_to_utc(d, parse_hhmm(slot.end_local), cfg.zone),
                    )
                )
            d += timedelta(days=1)
    return out


# --- current plan ----------------------------------------------------------------------------


def _to_planned(b: Block) -> PlannedBlock:
    return PlannedBlock(b.starts_at, b.ends_at, b.exam_id, b.topic_id, b.kind)  # type: ignore[arg-type]


async def future_blocks(session: AsyncSession, now: datetime) -> list[PlannedBlock]:
    rows = await session.scalars(
        select(Block)
        .where(Block.status == "planned", Block.starts_at > now)
        .order_by(Block.starts_at)
    )
    return [_to_planned(b) for b in rows]


async def exam_history(session: AsyncSession, before: datetime, cfg: PlanConfig) -> dict[int, date]:
    """exam_id → local date of its latest block before `before` (any status)."""
    rows = await session.execute(
        select(Block.exam_id, func.max(Block.starts_at))
        .where(Block.starts_at < before)
        .group_by(Block.exam_id)
    )
    return {
        exam_id: _as_utc(ts).astimezone(cfg.zone).date() for exam_id, ts in rows if ts is not None
    }


def _as_utc(ts: datetime) -> datetime:
    """Aggregates bypass the UTCDateTime type, so SQLite returns naive values (stored as UTC)."""
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


async def current_version(session: AsyncSession) -> PlanVersion | None:
    return await session.scalar(
        select(PlanVersion)
        .where(PlanVersion.applied_at.is_not(None))
        .order_by(PlanVersion.id.desc())
    )


# --- proposals -------------------------------------------------------------------------------


def compute_plan(
    inputs: PlanInputs,
    now: datetime,
    anchor: list[PlannedBlock] | None = None,
    history: dict[int, date] | None = None,
) -> PlanResult:
    result = build_plan(
        inputs.exams,
        inputs.topics,
        inputs.cfg,
        busy=inputs.busy,
        now=now,
        anchor=anchor or [],
        history=history,
    )
    violations = validate_plan(
        result.blocks, inputs.exams, inputs.topics, inputs.cfg, busy=inputs.busy
    )
    if violations:  # never propose a plan that breaks a non-negotiable
        raise PlanError("; ".join(f"{v.code}: {v.message}" for v in violations[:5]))
    return result


async def _supersede_pending(session: AsyncSession) -> None:
    for p in await session.scalars(
        select(Proposal).where(Proposal.status == "pending", Proposal.kind == "plan")
    ):
        p.status = "superseded"


def feasibility_note(result: PlanResult, inputs: PlanInputs) -> str:
    f = result.feasibility
    if f.ok:
        return ""
    drops = ", ".join(f"{c.name} ({c.hours_saved:.1f} ч)" for c in f.drop_candidates[:5])
    return (
        f"\n⚠️ Не хватает ≈{f.shortfall_hours:.1f} ч до экзаменов. Я не перегружаю дни; "
        f"меньше всего баллов потеряем, если отложить: {drops}."
    )


async def propose_plan(
    session: AsyncSession,
    inputs: PlanInputs,
    clock: Clock,
    *,
    reason: str,
    proposed_by: str,
    changes: dict[str, Any] | None = None,
    alt_changes: dict[str, Any] | None = None,
) -> Proposal | None:
    """Create a plan proposal (option A, optional compromise B, and KEEP). Returns None when the
    new plan equals the current one."""
    now = clock.now()
    current = await future_blocks(session, now)
    day_start = local_to_utc(clock.today(), parse_hhmm("00:00"), inputs.cfg.zone)
    history = await exam_history(session, day_start, inputs.cfg)
    options: list[dict[str, Any]] = []
    for key, chg in (("A", changes), ("B", alt_changes)):
        if key == "B" and chg is None:
            continue
        tuned = await _inputs_with_changes(session, inputs, chg or {})
        result = compute_plan(tuned, now, anchor=current, history=history)
        if key == "A" and not changes and list(result.blocks) == current:
            return None
        options.append(
            {
                "key": key,
                "changes": chg or {},
                "blocks": [b.to_json() for b in result.blocks],
                "diff": render_diff(current, result.blocks, inputs.cfg.zone, inputs.exam_names),
                "note": feasibility_note(result, tuned),
            }
        )
    options.append({"key": "KEEP"})
    await _supersede_pending(session)
    proposal = Proposal(
        created_at=now,
        kind="plan",
        summary_ru=reason,
        options_json=options,
        status="pending",
        expires_at=now + PROPOSAL_TTL,
    )
    session.add(proposal)
    await session.flush()
    return proposal


async def _inputs_with_changes(
    session: AsyncSession, inputs: PlanInputs, changes: dict[str, Any]
) -> PlanInputs:
    if not changes:
        return inputs
    ov = await get_overrides(session)
    ov["paused_until"].update({str(k): v for k, v in changes.get("paused_until", {}).items()})
    ov["day_caps"].update(changes.get("day_caps", {}))
    return replace(inputs, cfg=_cfg_with_overrides(inputs.cfg, ov))


async def apply_proposal(
    session: AsyncSession, proposal_id: int, option_key: str, clock: Clock
) -> PlanVersion | None:
    """Apply a confirmed plan proposal. Only called from the Telegram callback handler."""
    proposal = await session.get(Proposal, proposal_id)
    now = clock.now()
    if proposal is None or proposal.status != "pending":
        raise PlanError("Это предложение уже неактуально.")
    if proposal.expires_at and proposal.expires_at < now:
        proposal.status = "expired"
        raise PlanError("Срок предложения истёк — попроси новый план.")
    option = next((o for o in proposal.options_json if o["key"] == option_key), None)
    if option is None:
        raise PlanError("Неизвестный вариант.")
    proposal.chosen_option = option_key
    if option_key == "KEEP":
        proposal.status = "rejected"
        return None
    blocks = [PlannedBlock.from_json(b) for b in option["blocks"]]
    live = set((await session.scalars(select(Topic.id).where(Topic.status == "active"))).all())
    if any(b.topic_id is not None and b.topic_id not in live for b in blocks):
        proposal.status = "superseded"  # topics were edited since the proposal was computed
        raise PlanError("Темы изменились после этого предложения — пересчитай: /replan.")
    if option.get("restore_overrides") is not None:  # undo restores the old agreements too
        await _set_overrides(session, option["restore_overrides"], clock)
    elif option.get("changes"):
        ov = await get_overrides(session)
        ov["paused_until"].update(
            {str(k): v for k, v in option["changes"].get("paused_until", {}).items()}
        )
        ov["day_caps"].update(option["changes"].get("day_caps", {}))
        await _set_overrides(session, ov, clock)
    version = await write_version(
        session,
        blocks,
        clock,
        reason=proposal.summary_ru,
        proposed_by="planner",
        overrides=await get_overrides(session),
    )
    proposal.status = "applied"
    return version


async def write_version(
    session: AsyncSession,
    blocks: list[PlannedBlock],
    clock: Clock,
    *,
    reason: str,
    proposed_by: str,
    overrides: dict[str, Any],
) -> PlanVersion:
    """Replace all future planned blocks with `blocks` and record a plan version."""
    now = clock.now()
    future = [b for b in blocks if b.start > now]
    parent = await current_version(session)
    version = PlanVersion(
        created_at=now,
        parent_id=parent.id if parent else None,
        reason=reason,
        proposed_by=proposed_by,
        applied_at=now,
        snapshot_json={"blocks": [b.to_json() for b in future], "overrides": overrides},
    )
    session.add(version)
    await session.flush()
    await session.execute(delete(Block).where(Block.status == "planned", Block.starts_at > now))
    session.add_all(
        Block(
            plan_version_id=version.id,
            starts_at=b.start,
            ends_at=b.end,
            exam_id=b.exam_id,
            topic_id=b.topic_id,
            kind=b.kind,
            status="planned",
        )
        for b in future
    )
    await session.flush()
    log.info("Applied plan version %s (%d future blocks)", version.id, len(future))
    return version


async def propose_undo(session: AsyncSession, inputs: PlanInputs, clock: Clock) -> Proposal | None:
    """«Верни как было»: propose restoring the previous applied version (one tap)."""
    current = await current_version(session)
    if current is None or current.parent_id is None:
        return None
    previous = await session.get(PlanVersion, current.parent_id)
    if previous is None:
        return None
    now = clock.now()
    blocks = [PlannedBlock.from_json(b) for b in previous.snapshot_json.get("blocks", [])]
    blocks = [b for b in blocks if b.start > now]
    existing = await future_blocks(session, now)
    await _supersede_pending(session)
    prev_over = previous.snapshot_json.get("overrides", {"paused_until": {}, "day_caps": {}})
    proposal = Proposal(
        created_at=now,
        kind="plan",
        summary_ru=f"Вернуть план версии #{previous.id} ({previous.reason[:60]})",
        options_json=[
            {
                "key": "A",
                "changes": {},
                "restore_overrides": prev_over,
                "blocks": [b.to_json() for b in blocks],
                "diff": render_diff(existing, blocks, inputs.cfg.zone, inputs.exam_names),
                "note": "",
            },
            {"key": "KEEP"},
        ],
        status="pending",
        expires_at=now + PROPOSAL_TTL,
    )
    session.add(proposal)
    await session.flush()
    return proposal
