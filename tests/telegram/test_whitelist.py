from types import SimpleNamespace

from agent.telegram.middleware import OwnerOnlyMiddleware

OWNER = 42


async def _run(mw: OwnerOnlyMiddleware, user_id: int | None):
    calls: list[str] = []

    async def handler(event, data):
        calls.append("handled")
        return "ok"

    data = {"event_from_user": SimpleNamespace(id=user_id) if user_id is not None else None}
    result = await mw(handler, SimpleNamespace(update_id=1), data)
    return result, calls


async def test_owner_updates_pass_through() -> None:
    result, calls = await _run(OwnerOnlyMiddleware(OWNER), OWNER)
    assert result == "ok" and calls == ["handled"]


async def test_strangers_are_dropped() -> None:
    result, calls = await _run(OwnerOnlyMiddleware(OWNER), 999)
    assert result is None and calls == []


async def test_updates_without_user_are_dropped() -> None:
    result, calls = await _run(OwnerOnlyMiddleware(OWNER), None)
    assert result is None and calls == []
