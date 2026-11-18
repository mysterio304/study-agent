"""Owner-only whitelist. Registered as an *outer* middleware on every update, so nothing —
messages, callbacks, poll answers — reaches a handler unless it comes from the owner."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int) -> None:
        self._owner_id = owner_id

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != self._owner_id:
            # Log the fact, never the content.
            log.warning(
                "Dropped update %s from non-owner %s",
                getattr(event, "update_id", "?"),
                getattr(user, "id", None),
            )
            return None
        return await handler(event, data)
