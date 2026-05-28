from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from aiogram import Dispatcher, types
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from db.p2p import get_completed_orders_by_master
from db.users import get_user
from handlers.common import active_mc_sessions, send_welcome


def mastercard_main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура MasterCard."""
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("💳 Мои карты"), KeyboardButton("✅ Заявки"))
    kb.add(KeyboardButton("⚙️ Лимиты"), KeyboardButton("📊 Отчет прибыли"))
    kb.add(KeyboardButton("▶️ Начать сессию"), KeyboardButton("⏹ Завершить сессию"))
    return kb


async def is_mastercard_user(user_id: int) -> bool:
    """Проверяет, что пользователь имеет роль MasterCard."""
    user = await get_user(user_id)
    role = str((user or {}).get("role") or "").strip().lower()
    return role == "mastercard"


async def show_mastercard_menu(message: types.Message) -> None:
    """Показывает главное меню MasterCard."""
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к меню MasterCard.")
        return

    await message.answer(
        "💳 <b>Меню MasterCard</b>\n\n"
        "Выберите действие:",
        reply_markup=mastercard_main_keyboard(),
        parse_mode="HTML",
    )


async def mastercard_start_session(message: types.Message) -> None:
    """Начинает рабочую сессию MasterCard."""
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    active_mc_sessions[message.from_user.id] = datetime.now(timezone.utc)

    await message.answer(
        "▶️ <b>Сессия MasterCard начата.</b>\n\n"
        "Теперь вы можете получать заявки.",
        parse_mode="HTML",
        reply_markup=mastercard_main_keyboard(),
    )


async def mastercard_end_session(message: types.Message) -> None:
    """Завершает рабочую сессию MasterCard и показывает прибыль за сессию."""
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    user_id = message.from_user.id
    start = active_mc_sessions.pop(user_id, None)

    if not start:
        await message.answer(
            "⚠️ Активная сессия не найдена.",
            reply_markup=mastercard_main_keyboard(),
        )
        return

    end = datetime.now(timezone.utc)
    total_profit = 0

    try:
        orders = await get_completed_orders_by_master(user_id)
    except Exception:
        orders = []

    for order in orders:
        comp_dt = _parse_completed_at(order.get("completed_at"))
        if comp_dt is None:
            continue

        if start <= comp_dt <= end:
            total_rub = float(order.get("total_rub") or 0)
            rub_amount = float(order.get("rub_amount") or 0)
            base_margin = max(total_rub - rub_amount, 0)
            total_profit += math.ceil(base_margin * 0.35)

    await message.answer(
        "⏹ <b>Сессия MasterCard завершена.</b>\n\n"
        f"Прибыль за сессию: <b>{total_profit} ₽</b>",
        parse_mode="HTML",
        reply_markup=mastercard_main_keyboard(),
    )


async def mastercard_cards_stub(message: types.Message) -> None:
    """Заглушка раздела реквизитов."""
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    await message.answer(
        "💳 <b>Мои реквизиты</b>\n\n"
        "Раздел будет добавлен следующим шагом.",
        parse_mode="HTML",
        reply_markup=mastercard_main_keyboard(),
    )


async def mastercard_orders_stub(message: types.Message) -> None:
    """Заглушка раздела заявок."""
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    await message.answer(
        "✅ <b>Заявки</b>\n\n"
        "Сейчас заявки приходят во время активной сессии.\n"
        "Отдельный список заявок добавим следующим шагом.",
        parse_mode="HTML",
        reply_markup=mastercard_main_keyboard(),
    )


async def mastercard_limits_stub(message: types.Message) -> None:
    """Заглушка раздела лимитов."""
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    await message.answer(
        "⚙️ <b>Лимиты</b>\n\n"
        "Управление минимальными поступлениями добавим следующим шагом.",
        parse_mode="HTML",
        reply_markup=mastercard_main_keyboard(),
    )


async def mastercard_profit_report(message: types.Message) -> None:
    """Показывает общий отчет прибыли MasterCard."""
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    user_id = message.from_user.id
    total_profit = 0
    total_orders = 0

    try:
        orders = await get_completed_orders_by_master(user_id)
    except Exception:
        orders = []

    for order in orders:
        total_rub = float(order.get("total_rub") or 0)
        rub_amount = float(order.get("rub_amount") or 0)
        base_margin = max(total_rub - rub_amount, 0)
        total_profit += math.ceil(base_margin * 0.35)
        total_orders += 1

    await message.answer(
        "📊 <b>Отчет прибыли MasterCard</b>\n\n"
        f"Завершенных заявок: <b>{total_orders}</b>\n"
        f"Общая прибыль: <b>{total_profit} ₽</b>",
        parse_mode="HTML",
        reply_markup=mastercard_main_keyboard(),
    )


def _parse_completed_at(value: object) -> Optional[datetime]:
    """Безопасно приводит completed_at к timezone-aware datetime."""
    if not value:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def register_mastercard_menu_handlers(dp: Dispatcher) -> None:
    """Регистрирует кнопки меню MasterCard."""
    dp.register_message_handler(show_mastercard_menu, text="💳 MasterCard", state="*")
    dp.register_message_handler(mastercard_orders_stub, text="✅ Заявки", state="*")
    dp.register_message_handler(mastercard_profit_report, text="📊 Отчет прибыли", state="*")
    dp.register_message_handler(mastercard_start_session, text="▶️ Начать сессию", state="*")
    dp.register_message_handler(mastercard_end_session, text="⏹ Завершить сессию", state="*")