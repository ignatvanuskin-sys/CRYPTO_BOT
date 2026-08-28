from __future__ import annotations

import time
from collections import defaultdict

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery

_STALE_AFTER = 3600  # records older than 1h are pruned


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user throttling: 0.8s for messages, 0.3s for callbacks.

    ONE instance can serve both update types — use `register(dp)`.
    Prunes stale entries to avoid unbounded memory growth.
    """

    def __init__(self, message_rate: float = 0.8, callback_rate: float = 0.3):
        self.message_rate = message_rate
        self.callback_rate = callback_rate
        self._last_message: dict[int, float] = defaultdict(float)
        self._last_callback: dict[int, float] = defaultdict(float)
        self._last_prune = time.monotonic()

    def register(self, dp) -> None:
        dp.message.middleware(self)
        dp.callback_query.middleware(self)

    def _prune_if_needed(self) -> None:
        now = time.monotonic()
        if now - self._last_prune < 600:  # prune at most every 10 min
            return
        self._last_prune = now
        cutoff = now - _STALE_AFTER
        self._last_message = defaultdict(float, {k: v for k, v in self._last_message.items() if v > cutoff})
        self._last_callback = defaultdict(float, {k: v for k, v in self._last_callback.items() if v > cutoff})

    async def __call__(self, handler, event, data):
        self._prune_if_needed()
        if isinstance(event, Message):
            if event.from_user is None:
                return await handler(event, data)
            user_id = event.from_user.id
            now = time.monotonic()
            last = self._last_message[user_id]
            if now - last < self.message_rate:
                if event.text and event.text.startswith("/"):
                    await event.answer("Слишком часто. Подождите секунду.")
                return None
            self._last_message[user_id] = now
        elif isinstance(event, CallbackQuery):
            if event.from_user is None:
                return await handler(event, data)
            user_id = event.from_user.id
            now = time.monotonic()
            last = self._last_callback[user_id]
            if now - last < self.callback_rate:
                await event.answer("Слишком быстро", show_alert=False)
                return None
            self._last_callback[user_id] = now

        return await handler(event, data)
