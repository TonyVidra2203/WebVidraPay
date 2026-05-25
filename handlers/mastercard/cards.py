from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.exceptions import MessageNotModified

from db.cards import (
    add_card,
    delete_card,
    get_card_balance,
    get_card_by_id,
    get_cards_by_owner,
    set_card_active,
    update_card,
)
from db.p2p import get_completed_p2p_orders_by_card
from handlers.mastercard.menu import is_mastercard_user, mastercard_main_keyboard


DEFAULT_MIN_AMOUNT_RUB = 1200
DEFAULT_MAX_AMOUNT_RUB = 30000
DEFAULT_DAILY_LIMIT_RUB = 30000
DEFAULT_DAILY_TRANSFER_LIMIT = 3
DEFAULT_TRANSFER_PAUSE_MINUTES = 30


class MasterCardBrowseStates(StatesGroup):
    browsing = State()


class MasterCardAddStates(StatesGroup):
    waiting_bank = State()
    waiting_sbp = State()
    waiting_number = State()


class MasterCardEditLimitStates(StatesGroup):
    waiting_value = State()


LIMIT_FIELD_CONFIG: Dict[str, Dict[str, Any]] = {
    "min": {
        "field": "min_amount_rub",
        "title": "Мин. сумма",
        "prompt": "Введите минимальную сумму оплаты в рублях:",
        "kind": "money",
        "allow_empty": False,
    },
    "max": {
        "field": "max_amount_rub",
        "title": "Макс. сумма",
        "prompt": "Введите максимальную сумму оплаты в рублях:",
        "kind": "money",
        "allow_empty": False,
    },
    "daily": {
        "field": "daily_limit_rub",
        "title": "Дневной лимит",
        "prompt": "Введите дневной лимит по карте в рублях:",
        "kind": "money",
        "allow_empty": False,
    },
    "count": {
        "field": "daily_transfer_limit",
        "title": "Переводов в день",
        "prompt": "Введите количество переводов в день:",
        "kind": "int",
        "allow_empty": False,
    },
    "pause": {
        "field": "transfer_pause_minutes",
        "title": "Пауза",
        "prompt": "Введите паузу между переводами в минутах:",
        "kind": "int",
        "allow_empty": False,
    },
}


def _main_cards_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Добавить", callback_data="mc_card_add"),
        InlineKeyboardButton("✏️ Редактировать", callback_data="mc_card_edit_select"),
    )
    return kb


def _build_card_keyboard(card_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("⬅️", callback_data="mc_card_browse_prev"),
        InlineKeyboardButton("🗑️ Удалить", callback_data=f"mc_card_delete:{card_id}"),
        InlineKeyboardButton("➡️", callback_data="mc_card_browse_next"),
    )
    kb.row(
        InlineKeyboardButton("🔄 Вкл/Выкл", callback_data=f"mc_card_toggle:{card_id}"),
    )
    kb.row(
        InlineKeyboardButton("Мин. сумма", callback_data=f"mc_limit_edit:{card_id}:min"),
        InlineKeyboardButton("Макс. сумма", callback_data=f"mc_limit_edit:{card_id}:max"),
    )
    kb.row(
        InlineKeyboardButton("Дневной лимит", callback_data=f"mc_limit_edit:{card_id}:daily"),
    )
    kb.row(
        InlineKeyboardButton("Переводов/день", callback_data=f"mc_limit_edit:{card_id}:count"),
        InlineKeyboardButton("Пауза", callback_data=f"mc_limit_edit:{card_id}:pause"),
    )
    kb.row(
        InlineKeyboardButton("⬅️ Назад", callback_data="mc_card_back_to_list"),
    )
    return kb


def _clean_bank_name(value: Any) -> str:
    bank = str(value or "—").strip()

    replacements = {
        "сбер": "Сбер",
        "тинькофф": "Тинь",
        "т-": "Тинь",
        "альфа": "Альф",
        "райффайзен": "Райф",
        "озон": "Озон",
        "wildberries": "ВБ",
        "вайлдберриз": "ВБ",
        "втб": "ВТБ",
    }

    low = bank.lower()
    for key, replacement in replacements.items():
        if key in low:
            return replacement[:4]

    return bank[:4]


def _last4(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-4:] if len(digits) >= 4 else "—"


def _money_short(value: Any) -> str:
    try:
        return str(int(round(float(value or 0))))
    except Exception:
        return "0"


def _fmt_amount(value: Any) -> str:
    if value in (None, "", "—"):
        return "—"

    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"

    if amount.is_integer():
        return f"{int(amount)} ₽"

    return f"{amount:.2f} ₽"


def _fmt_int(value: Any, suffix: str = "") -> str:
    if value in (None, "", "—"):
        return "—"
    try:
        return f"{int(value)}{suffix}"
    except Exception:
        return "—"


def _parse_money(raw: str) -> float:
    value = (raw or "").replace(" ", "").replace(",", ".").strip()
    amount = float(value)
    if amount <= 0:
        raise ValueError("not_positive")
    return amount


def _parse_positive_int(raw: str) -> int:
    value = (raw or "").replace(" ", "").strip()
    amount = int(value)
    if amount <= 0:
        raise ValueError("not_positive")
    return amount


async def _format_added_date(raw: Any) -> str:
    if isinstance(raw, datetime):
        return raw.strftime("%d.%m.%Y")

    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "").replace("T", " ")).strftime("%d.%m.%Y")
        except ValueError:
            parts = raw.split("T")[0].split("-")
            return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 else "—"

    return "—"


async def _compose_list_text(cards: List[Dict[str, Any]]) -> str:
    if not cards:
        return "Сохранённых карт нет."

    lines: List[str] = ["💳 Карты", ""]

    for card in cards:
        balance = await get_card_balance(int(card["card_id"]))

        emoji = "🟢" if card.get("is_active", True) else "🔴"
        bank = _clean_bank_name(card.get("bank_name"))
        phone_tail = _last4(card.get("sbp_phone"))
        card_tail = _last4(card.get("card_number"))

        lines.append(
            f"{emoji} {bank} • {card_tail} — {phone_tail} •  {_money_short(balance)} ₽"
        )

    return "\n".join(lines)


async def _compose_card_text(card: Dict[str, Any]) -> str:
    orders = await get_completed_p2p_orders_by_card(card.get("card_number") or "")
    count = len(orders)
    turnover = sum(abs(float(o.get("total_rub") or 0)) for o in orders)

    balance = await get_card_balance(int(card["card_id"]))
    added_str = await _format_added_date(card.get("created_at"))

    status_active = bool(card.get("is_active", True))
    status_emoji = "🟢" if status_active else "🔴"

    sbp = card.get("sbp_phone") or "—"
    num = card.get("card_number") or "—"

    return (
        f"Банк: {card.get('bank_name', '—')} (ID: {card.get('card_id', '—')})\n\n"
        f"Статус: {'Активная' if status_active else 'Неактивна'} {status_emoji}\n"
        f"Добавлена: {added_str}\n"
        f"Кол-во переводов: {count}\n"
        f"Общий оборот: {turnover:.0f} руб.\n\n"
        f"СБП: {sbp}\n"
        f"Карта: {num}\n\n"
        f"Мин. сумма: {_fmt_amount(card.get('min_amount_rub'))}\n"
        f"Макс. сумма: {_fmt_amount(card.get('max_amount_rub'))}\n"
        f"Дневной лимит: {_fmt_amount(card.get('daily_limit_rub'))}\n"
        f"Переводов в день: {_fmt_int(card.get('daily_transfer_limit'))}\n"
        f"Пауза между переводами: {_fmt_int(card.get('transfer_pause_minutes'), ' мин.')}\n\n"
        f"Текущий баланс: {balance:.0f}₽"
    )


async def _show_card(bot: Bot, chat_id: int, card: Dict[str, Any]) -> None:
    text = await _compose_card_text(card)
    kb = _build_card_keyboard(int(card["card_id"]))
    await bot.send_message(chat_id, text, reply_markup=kb)


async def _edit_card(message: types.Message, card: Dict[str, Any]) -> None:
    text = await _compose_card_text(card)
    kb = _build_card_keyboard(int(card["card_id"]))

    try:
        await message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass


async def _get_owned_card(owner_id: int, card_id: int) -> Optional[Dict[str, Any]]:
    card = await get_card_by_id(card_id)
    if not card:
        return None

    try:
        if int(card.get("owner_id") or 0) != int(owner_id):
            return None
    except Exception:
        return None

    return card


async def mastercard_cards_menu(message: types.Message, state: FSMContext) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    await state.finish()

    cards = await get_cards_by_owner(message.from_user.id)
    text = await _compose_list_text(cards)

    await message.answer(text, reply_markup=_main_cards_kb())


async def mc_card_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.answer()

    try:
        await callback.message.delete()
    except Exception:
        pass

    await state.finish()

    await callback.bot.send_message(
        callback.from_user.id,
        "Введите название банка:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await MasterCardAddStates.waiting_bank.set()


async def mc_card_add_bank(message: types.Message, state: FSMContext) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await state.finish()
        return

    bank = (message.text or "").strip()
    if len(bank) < 2:
        await message.answer("⚠️ Введите нормальное название банка.")
        return

    await state.update_data(bank=bank)

    await message.answer("Введите телефон для СБП (+7XXXXXXXXXX) или «пропустить»:")
    await MasterCardAddStates.waiting_sbp.set()


async def mc_card_add_sbp(message: types.Message, state: FSMContext) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await state.finish()
        return

    text = (message.text or "").strip()
    sbp: Optional[str] = None

    if text.lower() not in {"пропустить", "-", "нет"}:
        if not re.match(r"^\+7\d{10}$", text):
            await message.answer("⚠️ Неверный формат. Пример: +79991234567")
            return
        sbp = text

    await state.update_data(sbp=sbp)

    await message.answer("Введите 16-значный номер карты или «пропустить»:")
    await MasterCardAddStates.waiting_number.set()


async def mc_card_add_number(message: types.Message, state: FSMContext) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await state.finish()
        return

    text = (message.text or "").replace(" ", "").strip()
    num: Optional[str] = None

    if text.lower() not in {"пропустить", "-", "нет"}:
        if not re.match(r"^\d{16}$", text):
            await message.answer("⚠️ Должно быть 16 цифр.")
            return
        num = text

    data = await state.get_data()
    if not data.get("sbp") and not num:
        await message.answer("⚠️ Нужно указать хотя бы СБП или номер карты.")
        return

    await add_card(
        owner_id=message.from_user.id,
        bank_name=data.get("bank", ""),
        sbp_phone=data.get("sbp"),
        card_number=num,
        min_amount_rub=DEFAULT_MIN_AMOUNT_RUB,
        max_amount_rub=DEFAULT_MAX_AMOUNT_RUB,
        daily_limit_rub=DEFAULT_DAILY_LIMIT_RUB,
        daily_transfer_limit=DEFAULT_DAILY_TRANSFER_LIMIT,
        transfer_pause_minutes=DEFAULT_TRANSFER_PAUSE_MINUTES,
    )

    await state.finish()

    await message.answer(
        "✅ Карта добавлена.\n\n"
        "Базовые ограничения установлены автоматически:\n"
        f"• Мин. сумма: {DEFAULT_MIN_AMOUNT_RUB} ₽\n"
        f"• Макс. сумма: {DEFAULT_MAX_AMOUNT_RUB} ₽\n"
        f"• Дневной лимит: {DEFAULT_DAILY_LIMIT_RUB} ₽\n"
        f"• Переводов в день: {DEFAULT_DAILY_TRANSFER_LIMIT}\n"
        f"• Пауза: {DEFAULT_TRANSFER_PAUSE_MINUTES} мин.",
        reply_markup=mastercard_main_keyboard(),
    )


async def mc_card_browse_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.answer()

    try:
        await callback.message.delete()
    except Exception:
        pass

    cards = await get_cards_by_owner(callback.from_user.id)
    if not cards:
        await callback.bot.send_message(
            callback.from_user.id,
            "ℹ️ Нет карт.",
            reply_markup=mastercard_main_keyboard(),
        )
        return

    await state.update_data(cards=cards, idx=0)
    await _show_card(callback.bot, callback.from_user.id, cards[0])
    await MasterCardBrowseStates.browsing.set()


async def mc_card_browse_prev(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.answer()

    data = await state.get_data()
    cards = data.get("cards") or await get_cards_by_owner(callback.from_user.id)

    if not cards:
        await callback.message.edit_text("ℹ️ Нет карт.")
        await state.finish()
        return

    idx = (int(data.get("idx", 0)) - 1) % len(cards)
    await state.update_data(cards=cards, idx=idx)
    await _edit_card(callback.message, cards[idx])


async def mc_card_browse_next(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.answer()

    data = await state.get_data()
    cards = data.get("cards") or await get_cards_by_owner(callback.from_user.id)

    if not cards:
        await callback.message.edit_text("ℹ️ Нет карт.")
        await state.finish()
        return

    idx = (int(data.get("idx", 0)) + 1) % len(cards)
    await state.update_data(cards=cards, idx=idx)
    await _edit_card(callback.message, cards[idx])


async def mc_card_back_to_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.answer()

    try:
        await callback.message.delete()
    except Exception:
        pass

    await state.finish()

    cards = await get_cards_by_owner(callback.from_user.id)
    text = await _compose_list_text(cards)

    await callback.bot.send_message(
        callback.from_user.id,
        text,
        reply_markup=_main_cards_kb(),
    )


async def mc_card_toggle(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        card_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError, TypeError):
        await callback.answer("Ошибка данных", show_alert=True)
        return

    card = await _get_owned_card(callback.from_user.id, card_id)
    if not card:
        await callback.answer("Карта не найдена", show_alert=True)
        return

    new_status = not bool(card.get("is_active", True))

    await set_card_active(
        card_id=card_id,
        owner_id=callback.from_user.id,
        is_active=new_status,
    )

    updated_card = {**card, "is_active": new_status}
    await callback.answer("Готово")
    await _edit_card(callback.message, updated_card)

    data = await state.get_data()
    cards = data.get("cards")
    if cards:
        refreshed_cards = [
            ({**c, "is_active": new_status} if int(c.get("card_id") or 0) == card_id else c)
            for c in cards
        ]
        await state.update_data(cards=refreshed_cards)


async def mc_card_delete(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        card_id = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError, TypeError):
        await callback.answer("Ошибка данных", show_alert=True)
        return

    card = await _get_owned_card(callback.from_user.id, card_id)
    if not card:
        await callback.answer("Карта не найдена", show_alert=True)
        return

    await delete_card(card_id=card_id, owner_id=callback.from_user.id)
    await callback.answer("Удалено")

    try:
        await callback.message.delete()
    except Exception:
        pass

    await state.finish()

    cards = await get_cards_by_owner(callback.from_user.id)
    text = await _compose_list_text(cards)

    await callback.bot.send_message(
        callback.from_user.id,
        text,
        reply_markup=_main_cards_kb(),
    )


async def mc_limit_edit_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка данных", show_alert=True)
        return

    try:
        card_id = int(parts[1])
    except (ValueError, TypeError):
        await callback.answer("Ошибка карты", show_alert=True)
        return

    limit_key = parts[2]
    config = LIMIT_FIELD_CONFIG.get(limit_key)
    if not config:
        await callback.answer("Ошибка ограничения", show_alert=True)
        return

    card = await _get_owned_card(callback.from_user.id, card_id)
    if not card:
        await callback.answer("Карта не найдена", show_alert=True)
        return

    await callback.answer()
    await state.finish()
    await state.update_data(mc_limit_card_id=card_id, mc_limit_key=limit_key)

    current_value = card.get(config["field"])
    await callback.bot.send_message(
        callback.from_user.id,
        f"{config['title']}\n"
        f"Текущее значение: {_fmt_amount(current_value) if config['kind'] == 'money' else _fmt_int(current_value)}\n\n"
        f"{config['prompt']}",
        reply_markup=ReplyKeyboardRemove(),
    )
    await MasterCardEditLimitStates.waiting_value.set()


async def mc_limit_value_entered(message: types.Message, state: FSMContext) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await state.finish()
        return

    data = await state.get_data()

    try:
        card_id = int(data.get("mc_limit_card_id") or 0)
    except Exception:
        card_id = 0

    limit_key = str(data.get("mc_limit_key") or "")
    config = LIMIT_FIELD_CONFIG.get(limit_key)

    if not card_id or not config:
        await state.finish()
        await message.answer("⚠️ Сессия редактирования не найдена.", reply_markup=mastercard_main_keyboard())
        return

    card = await _get_owned_card(message.from_user.id, card_id)
    if not card:
        await state.finish()
        await message.answer("⚠️ Карта не найдена.", reply_markup=mastercard_main_keyboard())
        return

    raw = message.text or ""

    try:
        if config["kind"] == "money":
            new_value = _parse_money(raw)
        else:
            new_value = _parse_positive_int(raw)
    except Exception:
        await message.answer("⚠️ Введите положительное число.")
        return

    # Проверяем связку min/max.
    if limit_key == "min":
        max_amount = card.get("max_amount_rub")
        if max_amount is not None and float(new_value) > float(max_amount):
            await message.answer("⚠️ Мин. сумма не может быть больше макс. суммы.")
            return

    if limit_key == "max":
        min_amount = card.get("min_amount_rub")
        if min_amount is not None and float(new_value) < float(min_amount):
            await message.answer("⚠️ Макс. сумма не может быть меньше мин. суммы.")
            return

    await update_card(card_id, **{config["field"]: new_value})
    await state.finish()

    updated = await get_card_by_id(card_id)
    await message.answer("✅ Ограничение обновлено.", reply_markup=mastercard_main_keyboard())

    if updated:
        await _show_card(message.bot, message.from_user.id, updated)


def register_mastercard_card_handlers(dp: Dispatcher) -> None:
    dp.register_message_handler(mastercard_cards_menu, text="💳 Карты", state="*")

    dp.register_callback_query_handler(
        mc_card_add_start,
        lambda c: c.data == "mc_card_add",
        state="*",
    )
    dp.register_callback_query_handler(
        mc_card_browse_start,
        lambda c: c.data == "mc_card_edit_select",
        state="*",
    )
    dp.register_callback_query_handler(
        mc_card_browse_prev,
        lambda c: c.data == "mc_card_browse_prev",
        state="*",
    )
    dp.register_callback_query_handler(
        mc_card_browse_next,
        lambda c: c.data == "mc_card_browse_next",
        state="*",
    )
    dp.register_callback_query_handler(
        mc_card_back_to_list,
        lambda c: c.data == "mc_card_back_to_list",
        state="*",
    )
    dp.register_callback_query_handler(
        mc_card_toggle,
        lambda c: c.data and c.data.startswith("mc_card_toggle:"),
        state="*",
    )
    dp.register_callback_query_handler(
        mc_card_delete,
        lambda c: c.data and c.data.startswith("mc_card_delete:"),
        state="*",
    )
    dp.register_callback_query_handler(
        mc_limit_edit_start,
        lambda c: c.data and c.data.startswith("mc_limit_edit:"),
        state="*",
    )

    dp.register_message_handler(mc_card_add_bank, state=MasterCardAddStates.waiting_bank)
    dp.register_message_handler(mc_card_add_sbp, state=MasterCardAddStates.waiting_sbp)
    dp.register_message_handler(mc_card_add_number, state=MasterCardAddStates.waiting_number)

    dp.register_message_handler(
        mc_limit_value_entered,
        state=MasterCardEditLimitStates.waiting_value,
    )
