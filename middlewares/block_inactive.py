# -----------------------------------------------------------------------------
# Middleware: блокировка неактивных (заблокированных) пользователей
# -----------------------------------------------------------------------------
import time
from typing import Any, Dict, Optional, Tuple

from aiogram import types
from aiogram.dispatcher.handler import CancelHandler
from aiogram.dispatcher.middlewares import BaseMiddleware

from db.users import is_user_active


class BlockInactiveUsersMiddleware(BaseMiddleware):
    """
    Глобально блокирует обработку любых апдейтов от пользователей,
    у которых is_active = 0 (False) в базе данных.

    Пользователю ничего не отправляет. Для callback_query делает пустой answer(),
    чтобы убрать "loading" на кнопке.
    """

    def __init__(self, cache_ttl_seconds: int = 5) -> None:
        super().__init__()
        self._cache_ttl = max(0, int(cache_ttl_seconds))
        # user_id -> (is_active, ts)
        self._cache: Dict[int, Tuple[bool, float]] = {}

    def _extract_user_id(self, update: types.Update) -> Optional[int]:
        u = update

        if u.message and u.message.from_user:
            return u.message.from_user.id
        if u.edited_message and u.edited_message.from_user:
            return u.edited_message.from_user.id
        if u.callback_query and u.callback_query.from_user:
            return u.callback_query.from_user.id
        if u.inline_query and u.inline_query.from_user:
            return u.inline_query.from_user.id
        if u.chosen_inline_result and u.chosen_inline_result.from_user:
            return u.chosen_inline_result.from_user.id
        if u.shipping_query and u.shipping_query.from_user:
            return u.shipping_query.from_user.id
        if u.pre_checkout_query and u.pre_checkout_query.from_user:
            return u.pre_checkout_query.from_user.id

        return None

    async def on_pre_process_update(self, update: types.Update, data: Dict[str, Any]) -> None:
        user_id = self._extract_user_id(update)
        if not user_id:
            return

        # Кэш, чтобы не ходить в БД на каждый апдейт
        if self._cache_ttl > 0:
            cached = self._cache.get(user_id)
            if cached:
                is_active_cached, ts = cached
                if (time.time() - ts) <= self._cache_ttl:
                    if not is_active_cached:
                        # Убираем "loading" на кнопках
                        if update.callback_query:
                            try:
                                await update.callback_query.answer()
                            except Exception:
                                pass
                        raise CancelHandler()
                    return

        # Источник истины — БД
        try:
            active = bool(await is_user_active(user_id))
        except Exception:
            # Если БД временно упала — лучше НЕ блокировать всех подряд
            return

        if self._cache_ttl > 0:
            self._cache[user_id] = (active, time.time())

        if not active:
            if update.callback_query:
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
            raise CancelHandler()
