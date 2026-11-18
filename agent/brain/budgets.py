"""Per-call-type limits (ARCHITECTURE §11.4) and a conservative local token estimator.

The estimator deliberately over-counts (UTF-8 bytes / 3; Cyrillic is 2 bytes per char), so a
request that passes the guard is safely inside its budget. Real counts from `usage` are logged
and can be compared in /usage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

ModelRole = Literal["cheap", "smart"]
Thinking = Literal["off", "low", "medium"]

HAIKU_PRICE_STEP_TOKENS = 100_000
HAIKU_HARD_LIMIT_TOKENS = 90_000  # stay well clear of the 100K price step


@dataclass(frozen=True, slots=True)
class CallSpec:
    name: str
    role: ModelRole
    thinking: Thinking
    max_tokens: int
    input_budget: int  # estimated input tokens


CALL_SPECS: dict[str, CallSpec] = {
    s.name: s
    for s in (
        CallSpec("chat_turn", "cheap", "low", 1_200, 12_000),
        CallSpec("escalated_turn", "smart", "low", 2_500, 18_000),
        CallSpec("plan_writeup", "smart", "low", 1_200, 8_000),
        CallSpec("checkin_parse", "cheap", "off", 600, 3_000),
        CallSpec("history_summary", "cheap", "off", 400, 4_000),
        CallSpec("announcement_summary", "cheap", "off", 300, 3_000),
        CallSpec("quiz_generate", "cheap", "low", 1_500, 4_000),
        CallSpec("answer_grade", "cheap", "off", 300, 2_000),
        CallSpec("diagnostic_analysis", "smart", "medium", 2_000, 10_000),
        CallSpec("topic_discovery", "smart", "medium", 3_000, 12_000),
    )
}

# Component budgets used by the context builders (asserted in tests).
STATIC_PREFIX_BUDGET = 4_000  # tools (largest profile) + system prompt + static profile
DYNAMIC_CONTEXT_BUDGET = 1_500
HISTORY_BUDGET = 2_500
TOOL_RESULT_BUDGET = 1_500  # per tool call
RAG_CHUNK_BUDGET = 400
RAG_MAX_CHUNKS = 5


def estimate_tokens(obj: Any) -> int:
    """Conservative token estimate for any JSON-serialisable request part."""
    if obj is None:
        return 0
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return len(text.encode("utf-8")) // 3 + 1


def truncate_to_tokens(text: str, budget: int) -> str:
    """Cut `text` so that estimate_tokens(text) <= budget (keeps the beginning)."""
    if estimate_tokens(text) <= budget:
        return text
    data = text.encode("utf-8")[: max(0, (budget - 2) * 3)]
    return data.decode("utf-8", errors="ignore") + "…"


class PromptTooLarge(RuntimeError):
    pass


def request_tokens(system: Any, tools: Any, messages: Any) -> int:
    return estimate_tokens(system) + estimate_tokens(tools) + estimate_tokens(messages)


def check_request(spec: CallSpec, model: str, *, system: Any, tools: Any, messages: Any) -> int:
    """Raise PromptTooLarge if the request exceeds its call-type budget or, for Haiku, the hard
    ceiling below the 100K price step. Returns the estimate."""
    est = request_tokens(system, tools, messages)
    if "haiku" in model and est >= HAIKU_HARD_LIMIT_TOKENS:
        raise PromptTooLarge(f"Haiku request ~{est} tokens ≥ {HAIKU_HARD_LIMIT_TOKENS}")
    if est > spec.input_budget:
        raise PromptTooLarge(f"{spec.name}: ~{est} tokens > budget {spec.input_budget}")
    return est
