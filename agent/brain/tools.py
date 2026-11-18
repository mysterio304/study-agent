"""Tool registry, schemas and toolset profiles (SPEC §9a.3–4).

Profiles are fixed, small, and serialised deterministically (sorted by name, constant schemas),
so every (model, profile) pair keeps one byte-stable cached prefix.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent.brain import tool_handlers as h
from agent.brain.budgets import TOOL_RESULT_BUDGET, truncate_to_tokens
from agent.brain.router import Profile

log = logging.getLogger(__name__)

Handler = Callable[[h.ToolContext, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler

    def definition(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


S = {"type": "string"}
I = {"type": "integer"}  # noqa: E741
DATE = {"type": "string", "description": "YYYY-MM-DD (HKT)"}

_TOOLS = [
    Tool(
        "get_plan",
        "Read the study plan (blocks) for 1–7 days.",
        _obj({"start_date": DATE, "days": I}),
        h.get_plan,
    ),
    Tool(
        "get_progress", "Per-exam topic levels, weakest topics and debt.", _obj({}), h.get_progress
    ),
    Tool(
        "explain_priority",
        "Why the plan is what it is: priority terms for a topic, or today's top topics.",
        _obj({"topic": S}),
        h.explain_priority,
    ),
    Tool(
        "propose_plan_change",
        "Propose a plan change for the student to confirm with a button. Options: pause an exam until a date, "
        "per-day hour caps. Optional 'alternative' = compromise option B. No args = recompute from progress.",
        _obj(
            {
                "reason": {
                    "type": "string",
                    "description": "Short Russian summary shown on the card",
                },
                "pause_exam": S,
                "pause_until": DATE,
                "day_caps": {
                    "type": "array",
                    "items": _obj({"date": DATE, "hours": {"type": "number"}}, ["date", "hours"]),
                },
                "alternative": _obj(
                    {
                        "pause_exam": S,
                        "pause_until": DATE,
                        "day_caps": {
                            "type": "array",
                            "items": _obj(
                                {"date": DATE, "hours": {"type": "number"}}, ["date", "hours"]
                            ),
                        },
                    }
                ),
            },
            ["reason"],
        ),
        h.propose_plan_change,
    ),
    Tool(
        "propose_undo",
        "Propose restoring the previous plan version («верни как было»).",
        _obj({}),
        h.propose_undo_tool,
    ),
    Tool(
        "propose_settings_change",
        "Propose changing a setting (cap_hours, final_week_cap_hours, between_exams_cap_hours, study_window, "
        "quiet_hours, lighter_weekday, voice_mode). Applies after the student confirms.",
        _obj(
            {"key": S, "value": {"type": ["string", "number", "boolean"]}, "reason": S},
            ["key", "value"],
        ),
        h.propose_settings_change,
    ),
    Tool(
        "log_study_session",
        "Record study the student did (topic, minutes, kind learn|practice|review|mock).",
        _obj({"topic": S, "exam": S, "minutes": I, "kind": S, "note": S}, ["topic", "minutes"]),
        h.log_study_session,
    ),
    Tool(
        "set_topic_level",
        "Set a topic's level 0–3 (0 untouched, 1 read, 2 can solve, 3 can explain).",
        _obj({"topic": S, "exam": S, "level": I}, ["topic", "level"]),
        h.set_topic_level,
    ),
    Tool(
        "manage_topics",
        "Edit the topic list: action add|remove|rename|merge.",
        _obj(
            {
                "action": S,
                "exam": S,
                "name": S,
                "new_name": S,
                "into": S,
                "effort_hours": {"type": "number"},
                "importance": I,
            },
            ["action", "name"],
        ),
        h.manage_topics,
    ),
    Tool(
        "get_course_scores",
        "Current Canvas course scores and final-exam weights.",
        _obj({}),
        h.get_course_scores,
    ),
    Tool(
        "calc_needed_final",
        "Final-exam % needed for a target letter grade (estimate).",
        _obj({"course": S, "target": S}, ["course"]),
        h.calc_needed_final,
    ),
    Tool("get_deadlines", "Upcoming unsubmitted Canvas deadlines.", _obj({}), h.get_deadlines),
    Tool(
        "save_agreement",
        "Remember an agreement with the student (free text).",
        _obj({"text": S, "exam": S}, ["text"]),
        h.save_agreement,
    ),
    Tool("list_agreements", "List active agreements.", _obj({}), h.list_agreements),
    Tool(
        "add_personal_reminder",
        "Create a reminder at a local time 'YYYY-MM-DD HH:MM' (HKT).",
        _obj({"text": S, "at": S}, ["text", "at"]),
        h.add_personal_reminder,
    ),
    Tool("list_reminders", "List pending reminders.", _obj({}), h.list_reminders),
    Tool(
        "escalate",
        "Hand this request to the stronger model when it needs deeper reasoning.",
        _obj({"reason": S}, ["reason"]),
        h.escalate,
    ),
    Tool("get_usage", "Today's LLM spend vs budget.", _obj({}), h.get_usage),
    Tool(
        "set_exam_info",
        "Record what the student says about an exam: final_weight (fraction or percent) and/or target_grade.",
        _obj({"exam": S, "final_weight": {"type": "number"}, "target_grade": S}, ["exam"]),
        h.set_exam_info,
    ),
    Tool(
        "add_busy_time",
        "Block time the planner must not use (lectures, appointments): either date (one-off) or "
        "weekday 0=Mon..6=Sun (weekly, optional until date), start/end HH:MM HKT. Creates a replan proposal.",
        _obj(
            {"date": DATE, "weekday": I, "start": S, "end": S, "label": S, "until": DATE},
            ["start", "end"],
        ),
        h.add_busy_time,
    ),
]
TOOLS: dict[str, Tool] = {t.name: t for t in _TOOLS}

PROFILES: dict[Profile, tuple[str, ...]] = {
    "chat": (
        "add_busy_time",
        "add_personal_reminder",
        "escalate",
        "get_plan",
        "get_progress",
        "get_usage",
        "list_agreements",
        "list_reminders",
        "log_study_session",
        "manage_topics",
        "propose_plan_change",
        "propose_settings_change",
        "save_agreement",
        "set_exam_info",
        "set_topic_level",
    ),
    "plan": (
        "add_busy_time",
        "calc_needed_final",
        "escalate",
        "explain_priority",
        "get_plan",
        "get_progress",
        "list_agreements",
        "propose_plan_change",
        "propose_settings_change",
        "propose_undo",
        "save_agreement",
    ),
    "material": ("escalate", "get_progress", "log_study_session", "set_topic_level"),
    "grades": (
        "calc_needed_final",
        "escalate",
        "get_course_scores",
        "get_deadlines",
        "set_exam_info",
    ),
}


def tool_definitions(profile: Profile) -> list[dict[str, Any]]:
    """Deterministic, byte-stable tool list for a profile."""
    return [TOOLS[name].definition() for name in sorted(PROFILES[profile])]


def profile_fingerprint(profile: Profile) -> str:
    return json.dumps(tool_definitions(profile), sort_keys=True, ensure_ascii=False)


async def run_tool(
    ctx: h.ToolContext, profile: Profile, name: str, args: dict[str, Any]
) -> tuple[str, bool]:
    """Execute one tool call → (result text, is_error). Tools outside the profile are refused."""
    if name not in PROFILES[profile]:
        return f"Tool {name} is not available here.", True
    try:
        text = await TOOLS[name].handler(ctx, args if isinstance(args, dict) else {})
        return truncate_to_tokens(text, TOOL_RESULT_BUDGET), False
    except (h.ToolError, KeyError, TypeError, ValueError) as exc:
        return truncate_to_tokens(f"Error: {exc}", 300), True
    except Exception:  # tool bug: log, tell the model, keep the turn alive
        log.exception("Tool %s failed", name)
        return "Internal error in tool; tell the student briefly and continue.", True


async def run_tools_parallel(
    ctx: h.ToolContext, profile: Profile, calls: list[Any]
) -> list[dict[str, Any]]:
    """Run independent tool calls concurrently; results go back in ONE user message."""
    results = await asyncio.gather(*(run_tool(ctx, profile, c.name, c.input) for c in calls))
    return [
        {
            "type": "tool_result",
            "tool_use_id": c.id,
            "content": text,
            **({"is_error": True} if err else {}),
        }
        for c, (text, err) in zip(calls, results, strict=True)
    ]
