"""Thin smoke tests for the Telegram layer (handlers are thin; logic is tested elsewhere)."""

from agent.canvas.fake import FakeCanvasSource
from agent.config import Settings
from agent.core.clock import SystemClock
from agent.flows import Services
from agent.telegram.bot import build_dispatcher
from agent.telegram.chat import safe_html
from agent.telegram.middleware import OwnerOnlyMiddleware
from agent.telegram.notifier import ConsoleNotifier, split_message


def test_safe_html_escapes_everything_but_bold_and_italic() -> None:
    raw = "<b>Важно</b>: a < b && <script>x</script> <i>ok</i>"
    assert (
        safe_html(raw)
        == "<b>Важно</b>: a &lt; b &amp;&amp; &lt;script&gt;x&lt;/script&gt; <i>ok</i>"
    )


def test_split_message_respects_limit_and_lines() -> None:
    text = "\n".join(f"строка {i} " + "x" * 50 for i in range(200))
    parts = split_message(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text


def test_dispatcher_wires_whitelist_and_routers(sessionmaker) -> None:
    settings = Settings(_env_file=None, telegram_owner_id=42)
    svc = Services(
        settings, sessionmaker, SystemClock(settings.zone), FakeCanvasSource(), ConsoleNotifier()
    )
    dp = build_dispatcher(settings, svc)
    assert any(isinstance(m, OwnerOnlyMiddleware) for m in dp.update.outer_middleware)
    assert {r.name for r in dp.sub_routers} == {
        "setup_wizard",
        "commands",
        "quiz",
        "mock",
        "voice",
        "files",
        "chat",
    }
    assert dp.workflow_data["svc"] is svc
