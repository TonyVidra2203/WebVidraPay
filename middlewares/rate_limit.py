# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------

import time
from typing import Dict

from aiogram import types
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware


# -----------------------------------------------------------------------------
# Раздел: Константы и глобальные переменные
# -----------------------------------------------------------------------------

RATE_LIMIT: float = 1.0
_last_timestamps: Dict[int, float] = {}


# -----------------------------------------------------------------------------
# Раздел: Middleware ограничения частоты сообщений
# -----------------------------------------------------------------------------

class RateLimitMiddleware(BaseMiddleware):
    """
    Middleware для ограничения частоты сообщений от пользователей.
    Отклоняет сообщение, если прошло меньше RATE_LIMIT секунд
    с момента последнего сообщения того же пользователя.
    """

    async def on_pre_process_update(self, update: types.Update, data: Dict) -> None:
        """
        Проверить, не превышен ли лимит сообщений для пользователя.
        """
        message = update.message or update.callback_query and update.callback_query.message
        if not message or not message.from_user:
            return

        user_id = message.from_user.id
        now = time.monotonic()
        last_time = _last_timestamps.get(user_id, 0.0)

        if now - last_time < RATE_LIMIT:
            raise CancelHandler()

        _last_timestamps[user_id] = now
