# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Tuple

import math
from aiogram import Bot, types

from db.p2p import get_completed_orders_by_master
from keyboards.inline import buy_keyboard


# -----------------------------------------------------------------------------
# Раздел: Глобальные структуры состояния
# -----------------------------------------------------------------------------
active_chats: Dict[int, int] = {}
chat_histories: Dict[int, List[Tuple[int, int]]] = {}
order_data: Dict[int, dict] = {}
pending_buy_messages: Dict[int, Tuple[int, int]] = {}
pending_operator_messages: Dict[int, List[Tuple[int, int]]] = {}
active_mc_sessions: Dict[int, datetime] = {}


# -----------------------------------------------------------------------------
# Раздел: Асинхронные функции — приветствие и меню
# -----------------------------------------------------------------------------
async def send_welcome(bot: Bot, chat_id: int) -> None:
    """Отправить приветственное изображение с кнопками."""
    with open("assets/menu.jpg", "rb") as photo:
        await bot.send_photo(chat_id=chat_id, photo=photo, reply_markup=buy_keyboard())


# -----------------------------------------------------------------------------
# Раздел: Асинхронные функции — сессии MasterCard
# -----------------------------------------------------------------------------
async def mc_start_session(message: types.Message) -> None:
    """Начать сессию MasterCard и сохранить момент старта."""
    active_mc_sessions[message.from_user.id] = datetime.now(timezone.utc)
    await message.answer("▶️ Сессия начата.")


async def mc_end_session(message: types.Message) -> None:
    """Завершить сессию MasterCard, посчитать прибыль (35% от маржи) и вывести её."""
    user_id = message.from_user.id
    start = active_mc_sessions.pop(user_id, None)
    if not start:
        await message.answer("⚠️ Сессия не найдена или уже была завершена.")
        return

    end = datetime.now(timezone.utc)
    total_profit = 0

    orders = await get_completed_orders_by_master(user_id)
    for order in orders:
        comp_at = order.get("completed_at")
        if not comp_at:
            continue

        if isinstance(comp_at, str):
            try:
                comp_dt = datetime.fromisoformat(comp_at)
            except ValueError:
                continue
        else:
            comp_dt = comp_at

        if comp_dt.tzinfo is None:
            comp_dt = comp_dt.replace(tzinfo=UTC)

        if start <= comp_dt <= end:
            base_margin = (order.get("total_rub", 0) - order.get("rub_amount", 0))
            total_profit += math.ceil(base_margin * 0.35)

    await message.answer(f"⏹ Сессия завершена.\nПрибыль: {total_profit} ₽")
