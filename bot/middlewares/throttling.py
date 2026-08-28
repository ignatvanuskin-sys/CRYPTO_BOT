from __future__ import annotations

import time
from collections import defaultdict

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery


class ThrottlingMiddleware(BaseMiddleware):
    """Simple per-user throttling: 0.8s for messages, 0.3s for callbacks."""

    def __init__(self, message_rate: float = 0.8, callback_rate: float = 0.3):
        self.message_rate = message_rate
        self.callback_rate = callback_rate
        self._last_message: dict[int, float] = defaultdict(float)
        self._last_callback: dict[int, float] = defaultdict(float)

    async def __call__(self, handler, event, data):
        user_id = None
        is_callback = isinstance(event, CallbackQuery)
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
            now = time.monotonic()
            last = self._last_message[user_id]
            if now - last < self.message_rate:
                # Silently drop or answer with throttling message for commands
                if event.text and event.text.startswith("/"):
                    await event.answer("Слишком часто. Подождите секунду.")
                return None
            self._last_message[user_id] = now
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id
            now = time.monotonic()
            last = self._last_callback[user_id]
            if now - last < self.callback_rate:
                await event.answer("Слишком быстро", show_alert=False)
                return None
            self._last_callback[user_id] = now

        return await handler(event, data)
