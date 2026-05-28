from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from aiohttp import ClientSession
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.exceptions import MessageNotModified

import utils.helpers as helpers
from db.cards import add_card, delete_card, get_all_cards, get_card_balance, get_cards_by_owner, init_cards_table, update_card
from db.connection import get_db
from db.p2p import get_completed_p2p_orders_by_card
from db.settings import (
    get_usdt_rub_rate_manual,
    set_usdt_rub_rate_manual,
    is_ton_profit_split_enabled,
    toggle_ton_profit_split_enabled,
)
from db.transactions import add_transaction
from db.user_breloks import (
    Status,
    assign_to_user as brelok_assign_to_user,
    delete_by_number as brelok_delete_by_number,
    ensure_table as ensure_breloks_table,
    get_by_number as brelok_get_by_number,
    list_all as breloks_list_all,
    set_pin_hash_by_number as set_pin_hash,
    set_status_by_number as brelok_set_status,
    upsert_brelok as user_brelok_upsert,
)
from db.users import get_all_users, get_user
from db.withdrawals import add_withdrawal
from handlers.common import send_welcome
from utils.helpers import HTTP_TIMEOUT, get_binance_ticker_price
from db.casino_wallets import (
    get_casino_phone,
    get_casino_wallet,
    reset_casino_profile,
)
from db.casinos import add_casino, delete_casino, init_casinos_table, list_casinos


class CourseStates(StatesGroup):
    waiting_commission = State()


class ReserveRechargeStates(StatesGroup):
    waiting_amount = State()
    waiting_date = State()
    waiting_price = State()


class CardWithdrawStates(StatesGroup):
    waiting_withdraw_amount = State()


class AnnounceStates(StatesGroup):
    waiting_text = State()
    waiting_photo = State()


class CardBrowseStates(StatesGroup):
    browsing = State()


class CardAddStates(StatesGroup):
    waiting_bank = State()
    waiting_sbp = State()
    waiting_number = State()


class CardEditBankStates(StatesGroup):
    waiting_bank = State()


class CardEditSBPStates(StatesGroup):
    waiting_sbp = State()


class CardEditNumberStates(StatesGroup):
    waiting_number = State()


class RateStates(StatesGroup):
    waiting_rate = State()


class RateCalcStates(StatesGroup):
    waiting_paste = State()


class BrelokUpsertStates(StatesGroup):
    waiting_line = State()


class BrelokAssignStates(StatesGroup):
    waiting_number = State()
    waiting_owner_id = State()


class BrelokStatusStates(StatesGroup):
    waiting_number = State()


class BrelokBrowseStates(StatesGroup):
    browsing = State()


class BrelokEditBankStates(StatesGroup):
    waiting_bank = State()


class BrelokEditWalletStates(StatesGroup):
    waiting_wallet = State()


class BrelokEditOwnerStates(StatesGroup):
    waiting_owner = State()


class BrelokAddStates(StatesGroup):
    waiting_number = State()
    waiting_bank = State()
    waiting_pin = State()


class BrelokWithdrawStates(StatesGroup):
    waiting_amount = State()


class AdminDebtStates(StatesGroup):
    waiting_add = State()
    waiting_set = State()


class AdminCasinoStates(StatesGroup):
    waiting_name = State()
    waiting_url = State()
    waiting_telegram = State()

# Profit admins (должны совпадать с handlers/chat/instruction.py)
PROFIT_ADMIN_1_ID = 6216500555
PROFIT_ADMIN_2_ID = 5762061609
PROFIT_ADMIN_IDS = (PROFIT_ADMIN_1_ID, PROFIT_ADMIN_2_ID)


ADMIN_KB: ReplyKeyboardMarkup = ReplyKeyboardMarkup(resize_keyboard=True)
ADMIN_KB.row(
    KeyboardButton("💳 Карты"),
    KeyboardButton("💰 Резерв"),
    KeyboardButton("🗄️ БД"),
)
ADMIN_KB.row(
    KeyboardButton("🧾 Бухгалтерия"),
    KeyboardButton("🔄 Курс"),
    KeyboardButton("✉️ Объявление"),
)
ADMIN_KB.row(
    KeyboardButton("📉 Долг"),
    KeyboardButton("VidraPay"),
    KeyboardButton("Split"),
)


def _build_card_keyboard(card_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("⬅️", callback_data="card_browse_prev"),
        InlineKeyboardButton("🗑️ Удалить", callback_data=f"card_delete:{card_id}"),
        InlineKeyboardButton("➡️", callback_data="card_browse_next"),
    )
    kb.row(
        InlineKeyboardButton("🔄 Вкл/Выкл", callback_data=f"card_toggle:{card_id}"),
        InlineKeyboardButton("💸 Вывод", callback_data=f"card_withdraw:{card_id}"),
        InlineKeyboardButton("💰 Ввод", callback_data=f"card_deposit:{card_id}"),
    )
    kb.row(
        InlineKeyboardButton("✏️ Банк", callback_data=f"card_edit_bank:{card_id}"),
        InlineKeyboardButton("📞 SBP", callback_data=f"card_edit_sbp:{card_id}"),
        InlineKeyboardButton("💳 Номер", callback_data=f"card_edit_number:{card_id}"),
    )
    return kb


async def _format_added_date(raw: Any) -> str:
    if isinstance(raw, datetime):
        return raw.strftime("%d.%m.%Y")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw).strftime("%d.%m.%Y")
        except ValueError:
            parts = raw.split("T")[0].split("-")
            return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 else "—"
    return "—"


async def _compose_card_text(card: Dict[str, Any]) -> str:
    orders = await get_completed_p2p_orders_by_card(card.get("card_number") or "")
    count = len(orders)
    turnover = sum(abs(o.get("total_rub", 0)) for o in orders)
    balance = await get_card_balance(card["card_id"])

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
        f"Общий оборот: {turnover} руб.\n\n"
        f"СБП: {sbp}\n"
        f"Карта: {num}\n\n"
        f"Текущий баланс: {balance:.0f}₽"
    )


async def _show_card(bot: Bot, chat_id: int, card: Dict[str, Any]) -> None:
    text = await _compose_card_text(card)
    kb = _build_card_keyboard(card["card_id"])
    await bot.send_message(chat_id, text, reply_markup=kb)


async def _edit_card(message: types.Message, card: Dict[str, Any]) -> None:
    text = await _compose_card_text(card)
    kb = _build_card_keyboard(card["card_id"])
    try:
        await message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass


async def admin_start(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    await init_cards_table()
    await init_casinos_table()
    await state.finish()

    split_enabled = await is_ton_profit_split_enabled()
    split_status = "🟢 ON" if split_enabled else "🔴 OFF"

    distribution_enabled = await _is_vidrapay_distribution_enabled()
    distribution_status = "🟢 ON" if distribution_enabled else "🔴 OFF"

    await message.bot.send_message(
        message.chat.id,
        f"👑 Админ-меню\n\n"
        f"Split: {split_status}\n"
        f"Распределение: {distribution_status}",
        reply_markup=ADMIN_KB,
    )
    await send_welcome(message.bot, message.chat.id)

async def admin_course_menu(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    curr = await get_usdt_rub_rate_manual()
    curr_str = f"{curr:.2f} ₽" if curr is not None else "—"

    text = "🔄 Курс USDT→RUB\n\n" f"Текущий курс: {curr_str}\n\n" "Выберите действие:"
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🧮 Вставить баланс и рассчитать", callback_data="rate_calc"))
    kb.add(InlineKeyboardButton("✏️ Изменить вручную", callback_data="rate_edit"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu"))
    await message.bot.send_message(message.chat.id, text, reply_markup=kb)

async def admin_split_toggle(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    await state.finish()

    new_enabled = await toggle_ton_profit_split_enabled()

    status_text = "включён" if new_enabled else "выключен"
    status_emoji = "🟢" if new_enabled else "🔴"

    await message.bot.send_message(
        message.chat.id,
        f"{status_emoji} Split {status_text}.\n\n"
        f"{'TON-прибыль снова распределяется 60/40 между двумя кошельками.' if new_enabled else 'TON-прибыль больше не переводится на два TON-кошелька и остаётся на Binance.'}",
        reply_markup=ADMIN_KB,
    )

async def _ensure_vidrapay_distribution_table() -> None:
    db = await get_db()

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS vidrapay_distribution_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    await db.execute(
        """
        INSERT OR IGNORE INTO vidrapay_distribution_settings(key, value, updated_at)
        VALUES('enabled', '0', CURRENT_TIMESTAMP)
        """
    )

    await db.commit()


async def _is_vidrapay_distribution_enabled() -> bool:
    await _ensure_vidrapay_distribution_table()

    db = await get_db()
    cur = await db.execute(
        "SELECT value FROM vidrapay_distribution_settings WHERE key = 'enabled'"
    )
    row = await cur.fetchone()
    await cur.close()

    return bool(row and str(row[0]) == "1")


async def _set_vidrapay_distribution_enabled(enabled: bool) -> None:
    await _ensure_vidrapay_distribution_table()

    db = await get_db()
    await db.execute(
        """
        INSERT INTO vidrapay_distribution_settings(key, value, updated_at)
        VALUES('enabled', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP
        """,
        ("1" if enabled else "0",),
    )
    await db.commit()


async def _reset_vidrapay_distribution_limits() -> None:
    db = await get_db()

    tables_to_clear = (
        "vidrapay_card_distribution_usage",
        "vidrapay_distribution_usage",
        "card_distribution_usage",
    )

    for table_name in tables_to_clear:
        try:
            await db.execute(f"DELETE FROM {table_name}")
        except Exception:
            pass

    await db.commit()


async def admin_distribution_toggle(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    await state.finish()

    old_enabled = await _is_vidrapay_distribution_enabled()
    new_enabled = not old_enabled

    await _set_vidrapay_distribution_enabled(new_enabled)
    await _reset_vidrapay_distribution_limits()

    status_text = "включено" if new_enabled else "выключено"
    status_emoji = "🟢" if new_enabled else "🔴"

    await message.bot.send_message(
        message.chat.id,
        f"{status_emoji} Распределение {status_text}.\n\n"
        f"Счётчики ограничений по выдаче банковских карт сброшены.",
        reply_markup=ADMIN_KB,
    )



async def rate_calc_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(
        callback.from_user.id,
        "Вставьте текст с балансом\nИ я рассчитаю курс USDT→RUB.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await RateCalcStates.waiting_paste.set()


def _clean_num(s: str) -> float:
    for sp in (" ", "\u00A0", "\u202F", "\u2009"):
        s = s.replace(sp, "")
    s = s.strip("),. ")
    s = s.replace(",", ".")
    return float(s)


def _find_currency_pairs(text: str, rub_variants: str, btc_variants: str) -> Iterable[Tuple[str, str]]:
    pairs1 = re.findall(rf"([\d\s.,]+)\s*({rub_variants}|{btc_variants})\b", text, flags=re.IGNORECASE)
    pairs2 = re.findall(rf"({rub_variants}|{btc_variants})\s*[:=]?\s*([\d\s.,]+)\b", text, flags=re.IGNORECASE)
    yield from ((amt, unit) for amt, unit in pairs1)
    yield from ((amt, unit) for unit, amt in pairs2)


async def rate_calc_paste_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    text = (message.text or "").strip()
    rub_variants = r"(?:RUB|RUR|₽|РУБ\.?|руб\.?)"
    btc_variants = r"(?:BTC|БТК)"

    rub_amount: Optional[float] = None
    btc_amount: Optional[float] = None

    for amt_raw, unit_raw in _find_currency_pairs(text, rub_variants, btc_variants):
        unit = (
            unit_raw.upper()
            .replace("РУБ", "RUB")
            .replace("RUR", "RUB")
            .replace("₽", "RUB")
            .replace("БТК", "BTC")
        )
        try:
            val = _clean_num(amt_raw)
        except Exception:
            continue
        if unit == "RUB" and rub_amount is None:
            rub_amount = val
        elif unit == "BTC" and btc_amount is None:
            btc_amount = val
        if rub_amount is not None and btc_amount is not None:
            break

    if not rub_amount or not btc_amount or rub_amount <= 0 or btc_amount <= 0:
        await message.answer("⚠️ Не удалось распарсить RUB и BTC из текста. Проверьте формат.")
        return

    from config.settings import settings as cfg

    btcusdt = await get_binance_ticker_price("BTCUSDT", cfg.binance_base_url, HTTP_TIMEOUT)
    if not btcusdt or btcusdt <= 0:
        await message.answer("⚠️ Не удалось получить цену BTCUSDT с Binance.")
        return

    raw_usdt_rub = rub_amount / (btc_amount * btcusdt)
    if not (0 < raw_usdt_rub < 1_000_000):
        await message.answer("⚠️ Результат выглядит некорректно. Проверьте входные данные.")
        return

    usdt_rub = raw_usdt_rub + 0.20

    await set_usdt_rub_rate_manual(usdt_rub)
    await state.finish()

    calc_info = (
        f"🧮 Расчёт курса завершён\n\n"
        f"Входные данные:\n"
        f"• RUB: {rub_amount:,.2f}\n"
        f"• BTC: {btc_amount:.8f}\n"
        f"• BTCUSDT: {btcusdt:,.2f}\n\n"
        f"Итог:\n"
        f"• 1 USDT = {usdt_rub:.2f} ₽"
    ).replace(",", " ")
    await message.answer(calc_info, reply_markup=ADMIN_KB)


async def course_basic_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    rate = await get_usdt_rub_rate_manual()
    rate_str = f"{rate:.2f}" if rate else "—"
    await callback.bot.send_message(
        callback.from_user.id,
        f"🔄 Курс (ручной): 1 USDT ≈ {rate_str} ₽",
        reply_markup=ADMIN_KB,
    )


async def rate_edit_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(
        callback.from_user.id,
        "Введите курс USDT→RUB (например: 102.35):",
        reply_markup=ReplyKeyboardRemove(),
    )
    await RateStates.waiting_rate.set()


async def rate_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return
    try:
        rate = float((message.text or "").replace(",", "."))
        if rate <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Неверный формат. Введите положительное число, например: 102.35")
        return
    await set_usdt_rub_rate_manual(rate)
    await state.finish()
    await message.answer(f"✅ Курс обновлён: 1 USDT = {rate:.2f} ₽", reply_markup=ADMIN_KB)


async def commission_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(
        callback.from_user.id,
        "Введите комиссию обменника (в %):",
        reply_markup=ReplyKeyboardRemove(),
    )
    await CourseStates.waiting_commission.set()


async def commission_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return
    try:
        commission = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Неверный формат. Введите число.")
        return

    helpers_file = Path(__file__).parents[1] / "utils" / "helpers.py"
    if helpers_file.exists():
        lines = helpers_file.read_text("utf-8").splitlines(keepends=True)
        new_line = f"COMMISSION: float = {commission:.2f}\n"
        for i, ln in enumerate(lines):
            if ln.strip().startswith("COMMISSION"):
                lines[i] = new_line
                break
        else:
            lines.insert(0, new_line)
        helpers_file.write_text("".join(lines), "utf-8")

    importlib.reload(helpers)
    await message.bot.send_message(
        message.chat.id,
        f"✅ Комиссия установлена: {commission:.2f} %",
        reply_markup=ADMIN_KB,
    )
    await state.finish()


async def announce_start(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return
    await state.finish()
    await message.bot.send_message(
        message.chat.id,
        "✉️ Введите текст объявления",
        reply_markup=ReplyKeyboardRemove(),
    )
    await AnnounceStates.waiting_text.set()


async def announce_text_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return
    await state.update_data(text=(message.text or "").strip())
    await message.bot.send_message(
        message.chat.id,
        "📸 Прикрепите фото или /skip",
        reply_markup=ReplyKeyboardRemove(),
    )
    await AnnounceStates.waiting_photo.set()


async def announce_photo_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return
    text = (await state.get_data()).get("text", "")
    for u in await get_all_users():
        try:
            await message.bot.send_photo(u["telegram_id"], message.photo[-1].file_id, caption=text)
        except Exception:
            pass
    await message.bot.send_message(message.chat.id, "✅ Объявление с фото отправлено")
    await state.finish()


async def announce_skip(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return
    text = (await state.get_data()).get("text", "")
    for u in await get_all_users():
        try:
            await message.bot.send_message(u["telegram_id"], text)
        except Exception:
            pass
    await message.bot.send_message(message.chat.id, "✅ Объявление отправлено")
    await state.finish()


async def admin_cards_message(message: types.Message, state: FSMContext) -> None:
    await state.finish()

    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    await init_cards_table()

    users = await get_all_users()
    mastercard_users = [
        u for u in users
        if str(u.get("role") or "").strip().lower() == "mastercard"
    ]

    kb = InlineKeyboardMarkup(row_width=1)

    if not mastercard_users:
        await message.bot.send_message(
            message.chat.id,
            "💳 Кабинеты MasterCard\n\nПользователей с ролью MasterCard пока нет.",
            reply_markup=kb,
        )
        return

    lines: List[str] = [
        "💳 Кабинеты MasterCard",
        "",
        "Нажмите на нужный кабинет, чтобы открыть его и редактировать карты.",
        "",
    ]

    for mc_user in mastercard_users:
        owner_id = int(mc_user["telegram_id"])
        username = (mc_user.get("username") or "").strip()
        title = f"@{username}" if username else f"ID {owner_id}"

        try:
            cards = await get_cards_by_owner(owner_id)
        except Exception:
            cards = []

        active_count = sum(1 for c in cards if c.get("is_active", True))
        lines.append(f"• {title} — карт: {len(cards)}, активных: {active_count}")

        kb.add(
            InlineKeyboardButton(
                f"Открыть {title} ({len(cards)})",
                callback_data=f"mc_admin_open:{owner_id}",
            )
        )

    await message.bot.send_message(
        message.chat.id,
        "\n".join(lines),
        reply_markup=kb,
    )





def _admin_casino_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("➕ Добавить", callback_data="admin_casino_add"),
        InlineKeyboardButton("🗑️ Удалить", callback_data="admin_casino_delete"),
    )
    kb.add(
        InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu"),
    )
    return kb


def _admin_casino_delete_kb(items: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for item in items:
        name = item.get("name") or item.get("casino_key") or "Казино"
        casino_key = item.get("casino_key") or ""
        kb.add(
            InlineKeyboardButton(
                f"🗑️ {name}",
                callback_data=f"admin_casino_delete_one:{casino_key}",
            )
        )
    kb.add(
        InlineKeyboardButton("⬅️ Назад", callback_data="admin_casino_menu"),
    )
    return kb


async def admin_casino_menu(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    await state.finish()
    await init_casinos_table()

    casinos = await list_casinos()
    if casinos:
        lines = ["🎰 <b>Управление казино</b>", "", "Текущий список:"]
        for item in casinos:
            name = item.get("name") or "—"
            url = item.get("url") or "—"
            telegram = item.get("telegram") or "—"
            lines.append(f"• <b>{name}</b>")
            lines.append(f"  🌍 {url}")
            lines.append(f"  🔗 {telegram}")
    else:
        lines = [
            "🎰 <b>Управление казино</b>",
            "",
            "Список казино пока пуст.",
        ]

    await message.bot.send_message(
        message.chat.id,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_admin_casino_menu_kb(),
        disable_web_page_preview=True,
    )


async def admin_casino_menu_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    fake_message = callback.message
    fake_message.chat = callback.message.chat
    fake_message.from_user = callback.from_user
    await admin_casino_menu(fake_message, state)


async def admin_casino_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.bot.send_message(
        callback.from_user.id,
        "Введите название казино:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await AdminCasinoStates.waiting_name.set()


async def admin_casino_name_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не должно быть пустым.")
        return

    await state.update_data(casino_name=name)
    await message.answer("Введите ссылку на казино:")
    await AdminCasinoStates.waiting_url.set()


async def admin_casino_url_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    url = (message.text or "").strip()
    if not url:
        await message.answer("⚠️ Ссылка не должна быть пустой.")
        return

    await state.update_data(casino_url=url)
    await message.answer("Введите ссылку на Telegram-канал или @username:")
    await AdminCasinoStates.waiting_telegram.set()


async def admin_casino_telegram_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    telegram = (message.text or "").strip()
    if not telegram:
        await message.answer("⚠️ Поле Telegram не должно быть пустым.")
        return

    data = await state.get_data()
    name = (data.get("casino_name") or "").strip()
    url = (data.get("casino_url") or "").strip()

    casino_key = await add_casino(name, url, telegram)

    await state.finish()
    await message.answer(
        f"✅ Казино добавлено.\n\n"
        f"Название: {name}\n"
        f"Ключ: {casino_key}\n"
        f"Ссылка: {url}\n"
        f"Telegram: {telegram}",
        reply_markup=ADMIN_KB,
    )
    await admin_casino_menu(message, state)


async def admin_casino_delete_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    casinos = await list_casinos()
    if not casinos:
        await callback.bot.send_message(
            callback.from_user.id,
            "ℹ️ Сейчас нет казино для удаления.",
            reply_markup=ADMIN_KB,
        )
        return

    await callback.bot.send_message(
        callback.from_user.id,
        "Выберите казино, которое нужно удалить:",
        reply_markup=_admin_casino_delete_kb(casinos),
    )


async def admin_casino_delete_one(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()

    try:
        casino_key = callback.data.split(":", 1)[1]
    except Exception:
        return

    casinos = await list_casinos()
    item = next((x for x in casinos if (x.get("casino_key") or "") == casino_key), None)
    name = item.get("name") if item else casino_key

    ok = await delete_casino(casino_key)

    try:
        await callback.message.delete()
    except Exception:
        pass

    if ok:
        await callback.bot.send_message(
            callback.from_user.id,
            f"✅ Казино <b>{name}</b> удалено.",
            parse_mode="HTML",
            reply_markup=ADMIN_KB,
        )
    else:
        await callback.bot.send_message(
            callback.from_user.id,
            "⚠️ Не удалось удалить казино.",
            reply_markup=ADMIN_KB,
        )

    fake_message = callback.message
    fake_message.chat = callback.message.chat
    fake_message.from_user = callback.from_user
    await admin_casino_menu(fake_message, state)


async def card_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(callback.from_user.id, "Введите название банка:")
    await CardAddStates.waiting_bank.set()


async def card_add_bank(message: types.Message, state: FSMContext) -> None:
    await state.update_data(bank=(message.text or "").strip())
    await message.answer("Введите телефон для СБП (+7XXXXXXXXXX) или «пропустить»:")
    await CardAddStates.waiting_sbp.set()


async def card_add_sbp(message: types.Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    sbp: Optional[str] = None
    if text.lower() != "пропустить":
        if not re.match(r"^\+7\d{10}$", text):
            await message.answer("⚠️ Неверный формат.")
            return
        sbp = text
    await state.update_data(sbp=sbp)
    await message.answer("Введите 16-значный номер карты или «пропустить»:")
    await CardAddStates.waiting_number.set()


async def card_add_number(message: types.Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    num: Optional[str] = None
    if text.lower() != "пропустить":
        if not re.match(r"^\d{16}$", text):
            await message.answer("⚠️ Должно быть 16 цифр.")
            return
        num = text

    data = await state.get_data()
    await add_card(bank_name=data.get("bank", ""), sbp_phone=data.get("sbp"), card_number=num)
    await message.answer("✅ Карта добавлена", reply_markup=ADMIN_KB)
    await state.finish()


async def card_browse_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    cards = await get_all_cards()
    if not cards:
        await callback.bot.send_message(callback.from_user.id, "ℹ️ Нет карт.", reply_markup=ADMIN_KB)
        return
    await state.update_data(cards=cards, idx=0)
    await _show_card(callback.bot, callback.from_user.id, cards[0])
    await CardBrowseStates.browsing.set()


async def card_browse_prev(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    idx = (data["idx"] - 1) % len(data["cards"])
    await state.update_data(idx=idx)
    await _edit_card(callback.message, data["cards"][idx])


async def card_browse_next(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    idx = (data["idx"] + 1) % len(data["cards"])
    await state.update_data(idx=idx)
    await _edit_card(callback.message, data["cards"][idx])


async def card_edit_bank_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    card_id = int(callback.data.split(":", 1)[1])
    await state.update_data(card_id=card_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(callback.from_user.id, "Введите название банка:")
    await CardEditBankStates.waiting_bank.set()


async def card_edit_bank_entered(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    await update_card(data["card_id"], bank_name=(message.text or "").strip())
    await message.answer("✅ Банк обновлён", reply_markup=ADMIN_KB)
    await state.finish()


async def card_edit_sbp_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    card_id = int(callback.data.split(":", 1)[1])
    await state.update_data(card_id=card_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(callback.from_user.id, "Введите телефон SBP:")
    await CardEditSBPStates.waiting_sbp.set()


async def card_edit_sbp_entered(message: types.Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not re.match(r"^\+7\d{10}$", text):
        await message.answer("⚠️ Неверный формат")
        return
    data = await state.get_data()
    await update_card(data["card_id"], sbp_phone=text)
    await message.answer("✅ SBP обновлён", reply_markup=ADMIN_KB)
    await state.finish()


async def card_edit_number_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    card_id = int(callback.data.split(":", 1)[1])
    await state.update_data(card_id=card_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(callback.from_user.id, "Введите 16-значный номер карты:")
    await CardEditNumberStates.waiting_number.set()


async def card_edit_number_entered(message: types.Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not re.match(r"^\d{16}$", text):
        await message.answer("⚠️ Неверный формат")
        return
    data = await state.get_data()
    await update_card(data["card_id"], card_number=text)
    await message.answer("✅ Номер карты обновлён", reply_markup=ADMIN_KB)
    await state.finish()


async def card_delete(callback: types.CallbackQuery, state: FSMContext) -> None:
    card_id = int(callback.data.split(":", 1)[1])
    await delete_card(card_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(callback.from_user.id, "✅ Карта удалена", reply_markup=ADMIN_KB)


async def back_to_admin(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await admin_start(callback.message, state)


async def card_toggle_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    card_id = int(callback.data.split(":", 1)[1])
    cards = await get_all_cards()
    card = next((c for c in cards if c["card_id"] == card_id), None)
    if not card:
        return
    new_status = not card.get("is_active", True)
    await update_card(card_id, is_active=new_status)
    updated_card = {**card, "is_active": new_status}
    await _edit_card(callback.message, updated_card)


async def card_withdraw_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    card_id = int(callback.data.split(":", 1)[1])
    is_deposit = callback.data.startswith("card_deposit:")

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    balance = await get_card_balance(card_id)
    action_text = "ввода" if is_deposit else "вывода"
    action_emoji = "💰" if is_deposit else "💸"

    await callback.bot.send_message(
        callback.from_user.id,
        f"{action_emoji} Карта {card_id}\n"
        f"Текущий баланс: {balance:.2f}₽\n\n"
        f"Введите сумму для {action_text} (в рублях):",
        reply_markup=ReplyKeyboardRemove(),
    )

    await state.update_data(card_id=card_id, card_operation="deposit" if is_deposit else "withdraw")
    await CardWithdrawStates.waiting_withdraw_amount.set()


async def card_withdraw_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    data = await state.get_data()
    card_id = int(data.get("card_id"))
    operation = data.get("card_operation", "withdraw")

    try:
        amount = float((message.text or "").replace(" ", "").replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Неверный формат суммы. Введите положительное число.")
        return

    try:
        cur_balance = await get_card_balance(card_id)
    except Exception:
        await message.answer("⚠️ Не удалось получить баланс карты.")
        return

    if operation == "withdraw":
        if not cur_balance or cur_balance <= 0:
            await message.answer("ℹ️ На карте нет средств для вывода.", reply_markup=ADMIN_KB)
            await state.finish()
            return

        if amount > cur_balance:
            amount = float(cur_balance)

        db_amount = amount
        success_text = "снято"
        success_emoji = "💸"
    else:
        db_amount = -amount
        success_text = "зачислено"
        success_emoji = "💰"

    try:
        await add_withdrawal(message.from_user.id, card_id, db_amount)
    except Exception as e:
        if "withdrawals.date" in str(e):
            try:
                db = await get_db()
                await db.execute(
                    "INSERT INTO withdrawals(admin_id, card_id, amount, date) VALUES(?, ?, ?, CURRENT_TIMESTAMP)",
                    (message.from_user.id, card_id, db_amount),
                )
                await db.commit()
            except Exception as e2:
                action_name = "ввода" if operation == "deposit" else "вывода"
                await message.answer(f"❌ Ошибка при сохранении {action_name}: {e2}")
                return
        else:
            action_name = "ввода" if operation == "deposit" else "вывода"
            await message.answer(f"❌ Ошибка при {action_name}: {e}")
            return

    try:
        new_balance = await get_card_balance(card_id)
    except Exception:
        new_balance = None

    await message.answer(
        f"✅ {success_emoji} На карту {card_id} {success_text} {amount:.2f}₽\n"
        f"Текущий баланс: {0.0 if new_balance is None else new_balance:.2f}₽",
        reply_markup=ADMIN_KB,
    )
    await state.finish()


async def admin_reserve(message: types.Message, state: FSMContext) -> None:
    from config.settings import settings as cfg_settings

    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    api_key = cfg_settings.binance_api_key
    api_secret = cfg_settings.binance_api_secret
    base_url = cfg_settings.binance_base_url

    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}"
    signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    url = f"{base_url}/api/v3/account?{query_string}&signature={signature}"
    headers = {"X-MBX-APIKEY": api_key}

    try:
        async with ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.get(
                url,
                headers=headers,
                proxy=cfg_settings.binance_proxy or None,
            ) as resp:
                if resp.status != 200:
                    await message.answer("⚠️ Не удалось получить резерв с Binance.")
                    return
                data = await resp.json()
    except Exception:
        await message.answer("⚠️ Ошибка при подключении к Binance.")
        return

    balances: List[Dict[str, Any]] = data.get("balances", []) or []

    assets: List[Dict[str, Any]] = []
    for b in balances:
        asset = (b.get("asset") or "").upper()
        if not asset:
            continue
        try:
            free = float(b.get("free") or 0)
            locked = float(b.get("locked") or 0)
        except (TypeError, ValueError):
            continue
        total = free + locked
        if total <= 0:
            continue
        assets.append({"asset": asset, "free": free, "locked": locked, "total": total})

    if not assets:
        await message.answer("💰 Резерв на Binance: балансы пусты.")
        return

    usdt_rub = await get_usdt_rub_rate_manual() or 0.0
    threshold_rub = 1000.0

    lines: List[str] = []
    total_rub_sum = 0.0

    assets.sort(key=lambda x: x["asset"] != "USDT")

    for item in assets:
        asset = item["asset"]
        total_qty = float(item["total"])

        if asset == "USDT":
            usdt_equiv = total_qty
        else:
            symbol = f"{asset}USDT"
            price = await get_binance_ticker_price(symbol, base_url, HTTP_TIMEOUT)
            if not price:
                continue
            usdt_equiv = total_qty * price

        rub_equiv = usdt_equiv * usdt_rub
        if rub_equiv < threshold_rub:
            continue

        total_rub_sum += rub_equiv
        qty_str = f"{total_qty:.8f}".rstrip("0").rstrip(".")
        rub_str = f"{rub_equiv:,.0f}".replace(",", " ")
        lines.append(f"• {asset}: {qty_str} (≈ {rub_str} ₽)")

    if not lines:
        await message.answer(f"💰 На Binance нет активов ≥ {int(threshold_rub)} ₽.")
        return

    total_rub_str = f"{total_rub_sum:,.0f}".replace(",", " ")
    text = "💰 Резерв Binance\n\n" + "\n".join(lines) + f"\n\nИТОГО: {total_rub_str} ₽"

    kb_inline = InlineKeyboardMarkup().add(
        InlineKeyboardButton("Пополнить", callback_data="reserve_recharge")
    )
    await message.bot.send_message(message.chat.id, text, reply_markup=kb_inline)


async def reserve_recharge_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()

    wallet = "TJyDxYx8SDSgxf35Y7sk3R8hM5mRRXxjR3"

    text = (
        "💰 Пополнение резерва\n\n"
        "Кошелёк USDT (TRC20):\n"
        f"`{wallet}`\n\n"
        "P.S. Можно просто нажать пальцем на адрес кошелька один раз и он скопируется."
    )

    await callback.bot.send_message(
        callback.from_user.id,
        text,
        parse_mode="Markdown",
        reply_markup=ADMIN_KB,
    )

async def reserve_amount_entered(message: types.Message, state: FSMContext) -> None:
    try:
        usdt = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Неверный формат суммы.")
        return
    await state.update_data(usdt=usdt)
    await message.answer("Введите дату пополнения (ДД.ММ.ГГГГ):")
    await ReserveRechargeStates.waiting_date.set()


async def reserve_date_entered(message: types.Message, state: FSMContext) -> None:
    try:
        dt: date = datetime.strptime((message.text or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("⚠️ Неверный формат даты.")
        return
    await state.update_data(date=str(dt))
    await message.answer("Введите цену покупки USDT в рублях (RUB за USDT):")
    await ReserveRechargeStates.waiting_price.set()


async def reserve_price_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return
    try:
        price_rub = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Неверный формат цены.")
        return

    data = await state.get_data()
    usdt_amount = float(data.get("usdt", 0.0))
    rub_amount = usdt_amount * price_rub

    await add_transaction(
        user_id=message.from_user.id,
        btc_amount=0.0,
        rub_amount=rub_amount,
        total_rub=rub_amount,
    )

    await message.answer("✅ Данные о пополнении сохранены.", reply_markup=ADMIN_KB)
    await state.finish()


def _rub_to_kopeks(amount_rub: Decimal) -> int:
    return int((amount_rub * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_DOWN))


def _kopeks_to_rub(amount_k: int) -> Decimal:
    return (Decimal(int(amount_k)) / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


async def _ensure_admin_debts_table() -> str:
    db = await get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_debts (
            admin_id     INTEGER PRIMARY KEY,
            debt_kopeks  INTEGER NOT NULL DEFAULT 0,
            updated_at   TEXT
        );
        """
    )

    cur = await db.execute("PRAGMA table_info(admin_debts)")
    rows = await cur.fetchall()
    await cur.close()
    existing = {str(r[1]) for r in rows}

    if "updated_at" not in existing:
        try:
            await db.execute("ALTER TABLE admin_debts ADD COLUMN updated_at TEXT")
        except Exception:
            pass

    if "debt_kopeks" in existing:
        debt_col = "debt_kopeks"
    elif "debt_k" in existing:
        debt_col = "debt_k"
    else:
        try:
            await db.execute("ALTER TABLE admin_debts ADD COLUMN debt_kopeks INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        debt_col = "debt_kopeks"

    if "debt_rub" in existing:
        try:
            await db.execute(
                f"""
                UPDATE admin_debts
                   SET {debt_col} = CAST((COALESCE(debt_rub, 0) * 100) AS INTEGER)
                 WHERE ({debt_col} IS NULL OR {debt_col} = 0)
                """
            )
        except Exception:
            pass

    await db.commit()
    return debt_col


async def _get_admin_debt(admin_id: int) -> Decimal:
    debt_col = await _ensure_admin_debts_table()
    db = await get_db()

    cur = await db.execute(
        f"SELECT {debt_col} FROM admin_debts WHERE admin_id = ?",
        (int(admin_id),),
    )
    row = await cur.fetchone()
    await cur.close()

    debt_k = int(row[0]) if row and row[0] is not None else 0
    if debt_k < 0:
        debt_k = 0
    return _kopeks_to_rub(debt_k)


async def _set_admin_debt(admin_id: int, new_debt_rub: Decimal) -> None:
    """
    Устанавливает долг админа в RUB (Decimal), хранит в копейках (INTEGER).
    Гарантирует, что при схеме admin_debts с created_at NOT NULL — поле будет заполнено.
    Совместимо со старыми вариантами колонок debt_kopeks/debt_k.
    """
    debt_col = await _ensure_admin_debts_table()
    db = await get_db()

    # нормализуем сумму
    try:
        new_debt_rub = Decimal(str(new_debt_rub))
    except Exception:
        new_debt_rub = Decimal("0.00")

    if new_debt_rub < 0:
        new_debt_rub = Decimal("0.00")

    new_debt_rub = new_debt_rub.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    new_k = _rub_to_kopeks(new_debt_rub)

    try:
        await db.execute("BEGIN IMMEDIATE;")

        # Узнаём реальную схему таблицы (есть ли created_at)
        cur_cols = await db.execute("PRAGMA table_info(admin_debts)")
        cols = await cur_cols.fetchall()
        await cur_cols.close()
        colnames = {str(r[1]) for r in (cols or [])}
        has_created_at = "created_at" in colnames

        if has_created_at:
            # created_at должен быть NOT NULL => берём старый created_at если есть, иначе ставим now
            await db.execute(
                f"""
                INSERT INTO admin_debts(admin_id, {debt_col}, created_at, updated_at)
                VALUES(
                    ?,
                    ?,
                    COALESCE(
                        (SELECT created_at FROM admin_debts WHERE admin_id = ?),
                        datetime('now')
                    ),
                    datetime('now')
                )
                ON CONFLICT(admin_id) DO UPDATE SET
                    {debt_col} = excluded.{debt_col},
                    updated_at = datetime('now')
                """,
                (int(admin_id), int(new_k), int(admin_id)),
            )
        else:
            await db.execute(
                f"""
                INSERT INTO admin_debts(admin_id, {debt_col}, updated_at)
                VALUES(?, ?, datetime('now'))
                ON CONFLICT(admin_id) DO UPDATE SET
                    {debt_col} = excluded.{debt_col},
                    updated_at = datetime('now')
                """,
                (int(admin_id), int(new_k)),
            )

        await db.commit()

    except Exception:
        try:
            await db.execute("ROLLBACK;")
        except Exception:
            pass
        raise

async def _add_admin_debt(admin_id: int, delta_rub: Decimal) -> None:
    """
    Добавляет/уменьшает долг админа на величину delta_rub.
    Долг хранится в копейках (INTEGER). Ниже 0 не опускается.
    Совместимо с таблицей admin_debts, где created_at может быть NOT NULL.
    """
    debt_col = await _ensure_admin_debts_table()
    db = await get_db()

    # нормализуем дельту до копеек
    try:
        delta_rub = Decimal(str(delta_rub))
    except Exception:
        delta_rub = Decimal("0.00")

    delta_rub = delta_rub.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    delta_k = _rub_to_kopeks(delta_rub)

    try:
        await db.execute("BEGIN IMMEDIATE;")

        # Узнаём реальную схему таблицы (есть ли created_at)
        cur_cols = await db.execute("PRAGMA table_info(admin_debts)")
        cols = await cur_cols.fetchall()
        await cur_cols.close()
        colnames = {str(r[1]) for r in (cols or [])}
        has_created_at = "created_at" in colnames

        # текущий долг
        cur = await db.execute(
            f"SELECT {debt_col} FROM admin_debts WHERE admin_id = ?",
            (int(admin_id),),
        )
        row = await cur.fetchone()
        await cur.close()

        old_k = int(row[0]) if row and row[0] is not None else 0
        new_k = old_k + int(delta_k)
        if new_k < 0:
            new_k = 0

        # ВАЖНО: если created_at обязателен (NOT NULL) — заполняем его.
        if has_created_at:
            await db.execute(
                f"""
                INSERT INTO admin_debts(admin_id, {debt_col}, created_at, updated_at)
                VALUES(
                    ?,
                    ?,
                    COALESCE(
                        (SELECT created_at FROM admin_debts WHERE admin_id = ?),
                        datetime('now')
                    ),
                    datetime('now')
                )
                ON CONFLICT(admin_id) DO UPDATE SET
                    {debt_col} = excluded.{debt_col},
                    updated_at = datetime('now')
                """,
                (int(admin_id), int(new_k), int(admin_id)),
            )
        else:
            await db.execute(
                f"""
                INSERT INTO admin_debts(admin_id, {debt_col}, updated_at)
                VALUES(?, ?, datetime('now'))
                ON CONFLICT(admin_id) DO UPDATE SET
                    {debt_col} = excluded.{debt_col},
                    updated_at = datetime('now')
                """,
                (int(admin_id), int(new_k)),
            )

        await db.commit()

    except Exception:
        try:
            await db.execute("ROLLBACK;")
        except Exception:
            pass
        raise

async def admin_debt_menu(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    await state.finish()

    debt1 = await _get_admin_debt(PROFIT_ADMIN_1_ID)
    debt2 = await _get_admin_debt(PROFIT_ADMIN_2_ID)

    d1 = f"{debt1:,.2f}".replace(",", " ")
    d2 = f"{debt2:,.2f}".replace(",", " ")

    text = (
        "🧾 Долги админов\n\n"
        f"Admin1 ({PROFIT_ADMIN_1_ID}): <b>{d1} ₽</b>\n"
        f"Admin2 ({PROFIT_ADMIN_2_ID}): <b>{d2} ₽</b>\n\n"
        "Нажмите на админа ниже и введите сумму долга в RUB.\n"
        "Если долг > 0 — прибыль этого админа НЕ выводится на кошелёк, а уменьшает долг.\n"
        "Когда долг станет 0 — выплаты на кошелёк снова идут как раньше."
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Admin1", callback_data=f"admin_debt_pick:{PROFIT_ADMIN_1_ID}"),
        InlineKeyboardButton("Admin2", callback_data=f"admin_debt_pick:{PROFIT_ADMIN_2_ID}"),
    )
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu"))

    await message.answer(text, reply_markup=kb)


async def admin_debt_pick_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    try:
        target_id = int((callback.data or "").split(":", 1)[1])
    except Exception:
        await callback.answer("⚠️ Неверные данные", show_alert=True)
        return

    if target_id not in PROFIT_ADMIN_IDS:
        await callback.answer("⚠️ Неверный админ", show_alert=True)
        return

    try:
        await callback.message.delete()
    except Exception:
        pass

    await state.finish()
    await state.update_data(debt_target_admin_id=target_id)

    cur_debt = await _get_admin_debt(target_id)
    cur_str = f"{cur_debt:,.2f}".replace(",", " ")

    await callback.bot.send_message(
        callback.from_user.id,
        f"Введите НОВОЕ значение долга в RUB для админа {target_id}.\n"
        f"Текущий долг: {cur_str} ₽\n\n"
        "Пример: 25000",
        reply_markup=ReplyKeyboardRemove(),
    )
    await AdminDebtStates.waiting_set.set()


async def admin_debt_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.bot.send_message(
        callback.from_user.id,
        "Введите сумму в RUB, на которую изменить долг.\nПример: 1500\nМожно отрицательное: -500.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await AdminDebtStates.waiting_add.set()


async def admin_debt_add_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user.get("role") != "Admin":
        return

    # Парсим дельту (можно отрицательную)
    raw = (message.text or "").replace(" ", "").replace(",", ".").strip()
    try:
        delta = Decimal(raw)
    except Exception:
        await message.answer("⚠️ Неверный формат. Введите число, например 1500 или -500")
        return

    # Определяем ЦЕЛЕВОГО админа, которому меняем долг:
    # 1) если ранее был выбран Admin1/Admin2 через меню — берём его из state
    # 2) иначе (fallback) — меняем долг тому, кто вводит (старое поведение, но безопасное)
    data = await state.get_data()
    target_id = data.get("debt_target_admin_id")
    try:
        target_admin_id = int(target_id) if target_id is not None else int(message.from_user.id)
    except Exception:
        target_admin_id = int(message.from_user.id)

    # Дополнительная защита: если в state случайно лежит "левый" id — не даём менять
    # (чтобы долги крутились только для PROFIT_ADMIN_1_ID / PROFIT_ADMIN_2_ID)
    if target_admin_id not in PROFIT_ADMIN_IDS:
        await message.answer(
            "⚠️ Не выбран админ из списка (Admin1/Admin2). Откройте «📉 Долг» и выберите админа.",
            reply_markup=ADMIN_KB,
        )
        await state.finish()
        return

    # Применяем изменение
    try:
        await _add_admin_debt(target_admin_id, delta)
    except Exception as e:
        await message.answer(f"❌ Ошибка при изменении долга: {e}", reply_markup=ADMIN_KB)
        return

    await state.finish()

    # Подтверждение с новым значением
    try:
        new_debt = await _get_admin_debt(target_admin_id)
        new_str = f"{new_debt:,.2f}".replace(",", " ")
        sign = "+" if delta >= 0 else ""
        delta_str = f"{sign}{delta:,.2f}".replace(",", " ")
        await message.answer(
            f"✅ Готово.\nАдмин: {target_admin_id}\nИзменение: {delta_str} ₽\nНовый долг: {new_str} ₽",
            reply_markup=ADMIN_KB,
        )
    except Exception:
        await message.answer("✅ Готово.", reply_markup=ADMIN_KB)

    await admin_debt_menu(message, state)

async def admin_debt_set_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.bot.send_message(
        callback.from_user.id,
        "Введите НОВОЕ значение долга в RUB.\nПример: 25000",
        reply_markup=ReplyKeyboardRemove(),
    )
    await AdminDebtStates.waiting_set.set()


async def admin_debt_set_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    data = await state.get_data()
    target_id = data.get("debt_target_admin_id")

    # на всякий случай (если попадём сюда без выбора) — не ломаем старый сценарий
    try:
        target_admin_id = int(target_id) if target_id is not None else int(message.from_user.id)
    except Exception:
        target_admin_id = int(message.from_user.id)

    raw = (message.text or "").replace(" ", "").replace(",", ".").strip()
    try:
        val = Decimal(raw)
    except Exception:
        await message.answer("⚠️ Неверный формат. Введите число, например 25000")
        return

    await _set_admin_debt(target_admin_id, val)
    await state.finish()

    await message.answer("✅ Долг установлен.", reply_markup=ADMIN_KB)
    await admin_debt_menu(message, state)

async def admin_breloks_menu(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    await ensure_breloks_table()
    rows = await breloks_list_all(limit=100000, offset=0)

    total = len(rows)
    active = sum(1 for r in rows if (r.get("status") or "").lower() == "active")
    assigned = sum(1 for r in rows if r.get("owner_telegram_id"))
    free = total - assigned

    text = (
        f"Всего брелоков: {total}\n"
        f"Активных: {active} • Свободных: {free}\n"
        f"Привязано к пользователям: {assigned}"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Добавить", callback_data="brelok_add"),
        InlineKeyboardButton("✏️ Редактировать", callback_data="brelok_edit"),
    )
    await message.bot.send_message(message.chat.id, text, reply_markup=kb)


def _fmt_rub(n: int | float | None) -> str:
    try:
        v = int(n or 0)
    except Exception:
        v = 0
    s = str(v)
    parts: List[str] = []
    while s:
        parts.append(s[-3:])
        s = s[:-3]
    return " ".join(reversed(parts)) + "₽"


async def _compose_brelok_text(row: Dict[str, Any]) -> str:
    number = row.get("number")
    bank = row.get("bank") or "—"
    status_raw = (row.get("status") or "active").lower()
    is_active = status_raw == "active"
    status_emoji = "🟢" if is_active else "🔴"
    last4 = str(number)[-4:] if number is not None else "—"

    current_balance = int(row.get("total_topup") or 0)

    monthly_total = 0
    history_raw = row.get("history_json")
    if history_raw:
        try:
            history = json.loads(history_raw) or []
        except Exception:
            history = []

        now = datetime.now()
        month_ago = now - timedelta(days=30)

        for rec in history:
            try:
                created_at = rec.get("created_at")
                rub_amount = int(rec.get("rub_amount") or 0)
            except Exception:
                continue

            try:
                ts = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

            if ts >= month_ago:
                monthly_total += rub_amount

    owner_display = "—"
    owner_id = row.get("owner_telegram_id")
    if owner_id:
        user = await get_user(int(owner_id))
        if user:
            parts: List[str] = []
            first = user.get("first_name") or ""
            last = user.get("last_name") or ""
            username = user.get("username") or ""

            if first:
                parts.append(first)
            if last:
                parts.append(last)
            if username:
                nick = f"@{username}"
                parts.append(nick)

            if not parts:
                parts.append(str(owner_id))

            owner_display = ", ".join(parts)

    return (
        f"Брелок №{last4} ({bank})\n\n"
        f"Статус: {'Активен' if is_active else 'Неактивен'} {status_emoji}\n"
        f"Общий объем за месяц: {_fmt_rub(monthly_total)}\n"
        f"Текущий баланс: {_fmt_rub(current_balance)}\n\n"
        f"Владелец: {owner_display}"
    )


def _build_brelok_keyboard(number: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("⬅️", callback_data="brelok_browse_prev"),
        InlineKeyboardButton("🔄 Вкл/Выкл", callback_data=f"brelok_toggle:{number}"),
        InlineKeyboardButton("➡️", callback_data="brelok_browse_next"),
    )
    kb.row(
        InlineKeyboardButton("👤+", callback_data=f"brelok_edit_owner:{number}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"brelok_delete:{number}"),
        InlineKeyboardButton("👤-", callback_data=f"brelok_owner_clear:{number}"),
    )
    kb.row(
        InlineKeyboardButton("📜 История", callback_data=f"brelok_history_admin:{number}"),
        InlineKeyboardButton("💸 Вывод", callback_data=f"brelok_withdraw:{number}"),
    )
    return kb


async def _show_brelok_card(bot: Bot, chat_id: int, row: Dict[str, Any]) -> None:
    text = await _compose_brelok_text(row)
    kb = _build_brelok_keyboard(int(row["number"]))
    await bot.send_message(chat_id, text, reply_markup=kb)


async def _edit_brelok_card(message: types.Message, row: Dict[str, Any]) -> None:
    text = await _compose_brelok_text(row)
    kb = _build_brelok_keyboard(int(row["number"]))
    try:
        await message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass


def _likely_btc_address(addr: str) -> bool:
    addr = (addr or "").strip()
    return 14 <= len(addr) <= 120 and addr.startswith(("1", "3", "bc1", "tb1", "BC1"))


async def _delete_safe(bot: Bot, chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def brelok_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    sent = await callback.bot.send_message(callback.from_user.id, "Введите номер брелока (число):")
    await state.update_data(last_bot_msg_id=sent.message_id)
    await BrelokAddStates.waiting_number.set()


async def brelok_add_number(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    await _delete_safe(message.bot, message.chat.id, data.get("last_bot_msg_id"))
    await _delete_safe(message.bot, message.chat.id, message.message_id)

    try:
        number = int((message.text or "").strip())
        if number <= 0:
            raise ValueError
    except Exception:
        sent = await message.bot.send_message(message.chat.id, "⚠️ Должно быть положительное целое. Введите номер:")
        await state.update_data(last_bot_msg_id=sent.message_id)
        return

    exists = await brelok_get_by_number(number)
    if exists:
        sent = await message.bot.send_message(message.chat.id, "⚠️ Такой номер уже существует. Введите другой номер:")
        await state.update_data(last_bot_msg_id=sent.message_id)
        return

    await state.update_data(number=number)
    sent = await message.bot.send_message(message.chat.id, "Введите название банка:")
    await state.update_data(last_bot_msg_id=sent.message_id)
    await BrelokAddStates.waiting_bank.set()


async def brelok_add_bank(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    await _delete_safe(message.bot, message.chat.id, data.get("last_bot_msg_id"))
    await _delete_safe(message.bot, message.chat.id, message.message_id)

    bank = (message.text or "").strip()
    if not bank:
        sent = await message.bot.send_message(message.chat.id, "⚠️ Банк не может быть пустым. Введите название банка:")
        await state.update_data(last_bot_msg_id=sent.message_id)
        return

    await state.update_data(bank=bank)
    sent = await message.bot.send_message(message.chat.id, "Введите PIN карты (4 цифры):")
    await state.update_data(last_bot_msg_id=sent.message_id)
    await BrelokAddStates.waiting_pin.set()


async def brelok_add_wallet(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    await _delete_safe(message.bot, message.chat.id, data.get("last_bot_msg_id"))
    await _delete_safe(message.bot, message.chat.id, message.message_id)

    wallet = (message.text or "").strip()
    if not _likely_btc_address(wallet):
        sent = await message.bot.send_message(
            message.chat.id,
            "⚠️ Адрес выглядит некорректно. Допустимые префиксы: 1 / 3 / bc1 / tb1\nВведите BTC-адрес:",
        )
        await state.update_data(last_bot_msg_id=sent.message_id)
        return

    await state.update_data(wallet=wallet)
    sent = await message.bot.send_message(message.chat.id, "Введите PIN карты (4 цифры):")
    await state.update_data(last_bot_msg_id=sent.message_id)
    await BrelokAddStates.waiting_pin.set()


_PIN_RE = re.compile(r"^\d{4}$")


async def brelok_add_pin(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    await _delete_safe(message.bot, message.chat.id, data.get("last_bot_msg_id"))
    await _delete_safe(message.bot, message.chat.id, message.message_id)

    pin = (message.text or "").strip()
    if not _PIN_RE.fullmatch(pin):
        sent = await message.bot.send_message(message.chat.id, "⚠️ PIN должен содержать ровно 4 цифры. Введите PIN ещё раз:")
        await state.update_data(last_bot_msg_id=sent.message_id)
        return

    number = int(data["number"])
    bank = data["bank"]

    await user_brelok_upsert(
        number=number,
        bank=bank,
        wallet=None,
        owner_telegram_id=None,
        status="active",
    )
    await set_pin_hash(number, pin)

    await state.finish()
    await message.bot.send_message(
        message.chat.id,
        f"✅ Брелок №{number} добавлен.\nБанк: {bank}",
        reply_markup=ADMIN_KB,
    )


async def brelok_upsert_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    msg = (
        "Вставьте параметры брелока в формате:\n"
        "`номер;банк;кошелек;pin`\n\n"
        "Пример:\n"
        "`1234;Т-Банк;bc1q...;1234`\n\n"
        "_PIN — ровно 4 цифры. Будет сохранён в открытом виде и показан пользователю._"
    )
    await callback.bot.send_message(callback.from_user.id, msg, parse_mode="Markdown")
    await BrelokUpsertStates.waiting_line.set()


async def brelok_upsert_line_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    raw = (message.text or "").strip()
    parts = [p.strip() for p in raw.split(";")]
    if len(parts) != 4:
        await message.answer("⚠️ Нужно 4 поля через ';' (номер;банк;кошелек;pin).")
        return

    try:
        number = int(parts[0])
        bank = parts[1]
        wallet = parts[2]
        pin = parts[3]
        if not bank or not wallet:
            raise ValueError("банк/кошелек пусты")
        if not re.fullmatch(r"\d{4}", pin):
            raise ValueError("PIN должен быть ровно 4 цифры")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка разбора: {e}. Проверьте данные.")
        return

    await user_brelok_upsert(
        number=number,
        bank=bank,
        wallet=wallet,
        owner_telegram_id=None,
        status="active",
    )
    await set_pin_hash(number, pin)

    await state.finish()
    await message.answer("✅ Брелок сохранён (PIN установлен).", reply_markup=ADMIN_KB)


async def brelok_assign_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(callback.from_user.id, "Введите номер брелока (число):")
    await BrelokAssignStates.waiting_number.set()


async def brelok_assign_number_entered(message: types.Message, state: FSMContext) -> None:
    try:
        number = int((message.text or "").strip())
    except ValueError:
        await message.answer("⚠️ Номер должен быть целым числом.")
        return
    await state.update_data(number=number)
    await message.answer("Введите Telegram ID владельца:")
    await BrelokAssignStates.waiting_owner_id.set()


async def brelok_assign_owner_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return
    try:
        owner_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("⚠️ Telegram ID должен быть числом.")
        return

    data = await state.get_data()
    number = int(data["number"])

    await brelok_assign_to_user(number, owner_id)
    await state.finish()
    await message.answer(f"✅ Брелок №{number} привязан к пользователю {owner_id}.", reply_markup=ADMIN_KB)


async def brelok_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    rows = await breloks_list_all(limit=50, offset=0)
    if not rows:
        await callback.bot.send_message(callback.from_user.id, "ℹ️ Брелоки не найдены.", reply_markup=ADMIN_KB)
        return

    def fmt(n: int | float | None) -> str:
        try:
            v = int(n or 0)
        except Exception:
            v = 0
        s = str(v)
        parts: List[str] = []
        while s:
            parts.append(s[-3:])
            s = s[:-3]
        return " ".join(reversed(parts))

    lines: List[str] = []
    for r in rows:
        last4 = str(r.get("number") or "")[-4:]
        bank = r.get("bank") or "—"
        used = fmt(r.get("total_topup"))
        owner = r.get("owner_telegram_id") or "—"
        status = (r.get("status") or "active").lower()
        emoji = "🟢" if status == "active" else "🔴"
        lines.append(f"{emoji} №{last4} · {bank} · Объём {used}₽ · 👤 {owner}")
    text = "📋 Список брелоков (первые 50):\n\n" + "\n".join(lines)

    await callback.bot.send_message(callback.from_user.id, text, reply_markup=ADMIN_KB)


async def brelok_activate_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await state.update_data(target_status="active")
    await callback.bot.send_message(callback.from_user.id, "🟢 Введите номер брелока для активации:")
    await BrelokStatusStates.waiting_number.set()


async def brelok_deactivate_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await state.update_data(target_status="inactive")
    await callback.bot.send_message(callback.from_user.id, "🔴 Введите номер брелока для деактивации:")
    await BrelokStatusStates.waiting_number.set()


async def brelok_status_number_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    data = await state.get_data()
    raw_status = (data.get("target_status") or "").lower()
    if raw_status not in ("active", "inactive"):
        await message.answer("⚠️ Не выбрано действие. Повторите через меню брелоков.", reply_markup=ADMIN_KB)
        await state.finish()
        return

    target_status: Status = "active" if raw_status == "active" else "inactive"

    try:
        number = int((message.text or "").strip())
    except ValueError:
        await message.answer("⚠️ Номер должен быть целым числом.")
        return

    row = await brelok_get_by_number(number)
    if not row:
        await message.answer(f"⚠️ Брелок №{number} не найден.")
        return

    await brelok_set_status(number, target_status)

    emoji = "🟢" if target_status == "active" else "🔴"
    human = "Активна" if target_status == "active" else "Неактивна"
    await message.answer(f"✅ Брелок №{number}: статус изменён на {human} {emoji}", reply_markup=ADMIN_KB)
    await state.finish()


async def brelok_browse_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    rows = await breloks_list_all(limit=100000, offset=0)
    if not rows:
        await callback.bot.send_message(callback.from_user.id, "ℹ️ Нет брелоков.", reply_markup=ADMIN_KB)
        return

    await state.update_data(breloks=rows, idx=0)
    await _show_brelok_card(callback.bot, callback.from_user.id, rows[0])
    await BrelokBrowseStates.browsing.set()


async def brelok_browse_prev(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    rows: List[Dict[str, Any]] = data.get("breloks", [])
    idx = (data.get("idx", 0) - 1) % len(rows)
    await state.update_data(idx=idx)
    await _edit_brelok_card(callback.message, rows[idx])


async def brelok_browse_next(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    rows: List[Dict[str, Any]] = data.get("breloks", [])
    idx = (data.get("idx", 0) + 1) % len(rows)
    await state.update_data(idx=idx)
    await _edit_brelok_card(callback.message, rows[idx])


async def brelok_toggle(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    rows: List[Dict[str, Any]] = data.get("breloks", [])
    idx = data.get("idx", 0)
    row = rows[idx]

    current = (row.get("status") or "active").lower()
    new_status: Status = "inactive" if current == "active" else "active"

    await brelok_set_status(int(row["number"]), new_status)
    row["status"] = new_status
    await _edit_brelok_card(callback.message, row)


async def brelok_edit_bank_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    number = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_number=number)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(callback.from_user.id, "Введите новое название банка:")
    await BrelokEditBankStates.waiting_bank.set()


async def brelok_edit_bank_entered(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    number = int(data.get("edit_number"))
    rows: List[Dict[str, Any]] = (await state.get_data()).get("breloks", [])
    current = next((r for r in rows if int(r.get("number", -1)) == number), None)
    if not current:
        await state.finish()
        await message.answer("⚠️ Брелок не найден.", reply_markup=ADMIN_KB)
        return

    bank = (message.text or "").strip() or current.get("bank") or ""
    await user_brelok_upsert(
        number=number,
        bank=bank,
        wallet=current.get("wallet"),
        owner_telegram_id=current.get("owner_telegram_id"),
        status=current.get("status") or "active",
    )
    current["bank"] = bank
    await state.finish()
    await message.answer("✅ Банк обновлён.", reply_markup=ADMIN_KB)


async def brelok_edit_wallet_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    number = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_number=number)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(callback.from_user.id, "Введите новый BTC-кошелёк:")
    await BrelokEditWalletStates.waiting_wallet.set()


async def brelok_edit_wallet_entered(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    number = int(data.get("edit_number"))
    rows: List[Dict[str, Any]] = (await state.get_data()).get("breloks", [])
    current = next((r for r in rows if int(r.get("number", -1)) == number), None)
    if not current:
        await state.finish()
        await message.answer("⚠️ Брелок не найден.", reply_markup=ADMIN_KB)
        return

    wallet = (message.text or "").strip() or current.get("wallet") or ""
    await user_brelok_upsert(
        number=number,
        bank=current.get("bank"),
        wallet=wallet,
        owner_telegram_id=current.get("owner_telegram_id"),
        status=current.get("status") or "active",
    )
    current["wallet"] = wallet
    await state.finish()
    await message.answer("✅ Кошелёк обновлён.", reply_markup=ADMIN_KB)


async def brelok_edit_owner_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    number = int(callback.data.split(":", 1)[1])
    await state.update_data(edit_number=number)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.bot.send_message(callback.from_user.id, "Введите Telegram ID владельца (или 0 чтобы отвязать):")
    await BrelokEditOwnerStates.waiting_owner.set()


async def brelok_edit_owner_entered(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    number = int(data.get("edit_number"))
    rows: List[Dict[str, Any]] = (await state.get_data()).get("breloks", [])
    current = next((r for r in rows if int(r.get("number", -1)) == number), None)
    if not current:
        await state.finish()
        await message.answer("⚠️ Брелок не найден.", reply_markup=ADMIN_KB)
        return

    text = (message.text or "").strip()
    try:
        owner = int(text)
    except ValueError:
        await message.answer("⚠️ Введите числовой Telegram ID (или 0 чтобы отвязать).")
        return

    owner_val: Optional[int] = None if owner == 0 else owner
    await user_brelok_upsert(
        number=number,
        bank=current.get("bank"),
        wallet=current.get("wallet"),
        owner_telegram_id=owner_val,
        status=current.get("status") or "active",
    )
    current["owner_telegram_id"] = owner_val
    await state.finish()
    await message.answer("✅ Владелец обновлён.", reply_markup=ADMIN_KB)


async def brelok_delete(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        number = int(callback.data.split(":", 1)[1])
    except Exception:
        return

    data = await state.get_data()
    rows: List[Dict[str, Any]] = data.get("breloks", [])

    del_idx = next((i for i, r in enumerate(rows) if int(r.get("number", -1)) == number), None)

    await brelok_delete_by_number(number)

    if del_idx is None:
        try:
            await callback.message.edit_text("ℹ️ Брелок удалён.", reply_markup=None)
        except Exception:
            pass
        await state.finish()
        return

    rows.pop(del_idx)

    if not rows:
        try:
            await callback.message.edit_text("ℹ️ Брелоки отсутствуют.", reply_markup=None)
        except Exception:
            pass
        await callback.bot.send_message(callback.from_user.id, "Готово ✅", reply_markup=ADMIN_KB)
        await state.finish()
        return

    new_idx = min(del_idx, len(rows) - 1)
    await state.update_data(breloks=rows, idx=new_idx)
    await _edit_brelok_card(callback.message, rows[new_idx])


async def brelok_clear_owner(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        number = int(callback.data.split(":", 1)[1])
    except Exception:
        return

    row = await brelok_get_by_number(number)
    if not row:
        await callback.message.answer(f"⚠️ Брелок №{number} не найден.")
        return

    await user_brelok_upsert(
        number=number,
        bank=row.get("bank"),
        wallet=row.get("wallet"),
        owner_telegram_id=None,
        status=row.get("status") or "active",
    )

    new_row = await brelok_get_by_number(number)
    if new_row:
        await _edit_brelok_card(callback.message, new_row)
    await callback.message.answer(f"👤 Владелец брелока №{number} удалён.")


async def brelok_withdraw_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["role"] != "Admin":
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    try:
        number = int(callback.data.split(":", 1)[1])
    except Exception:
        return

    await state.update_data(brelok_withdraw_number=number)
    await callback.bot.send_message(
        callback.from_user.id,
        f"💸 Брелок №{number}\nВведите сумму вывода (в рублях):",
        reply_markup=ReplyKeyboardRemove(),
    )
    await BrelokWithdrawStates.waiting_amount.set()


async def brelok_withdraw_amount_entered(message: types.Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["role"] != "Admin":
        return

    data = await state.get_data()
    number = data.get("brelok_withdraw_number")

    if number is None:
        await message.answer("⚠️ Не удалось определить номер брелока. Повторите операцию.", reply_markup=ADMIN_KB)
        await state.finish()
        return

    raw = (message.text or "").replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите положительную сумму в рублях (например: 15000).")
        return

    row = await brelok_get_by_number(int(number))
    if not row:
        await message.answer(f"⚠️ Брелок №{number} не найден.", reply_markup=ADMIN_KB)
        await state.finish()
        return

    current_total = int(row.get("total_topup") or 0)
    if current_total <= 0:
        await message.answer(f"ℹ️ У брелока №{number} нет средств для вывода (баланс 0₽).", reply_markup=ADMIN_KB)
        await state.finish()
        return

    if amount > current_total:
        amount = float(current_total)

    new_total = current_total - int(amount)

    db = await get_db()
    from datetime import UTC

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    await db.execute(
        "UPDATE user_breloks SET total_topup = ?, updated_at = ? WHERE number = ?",
        (new_total, now_str, int(number)),
    )
    await db.commit()

    await state.finish()
    await message.answer(
        f"✅ С брелока №{number} списано {int(amount):,}₽.\nТекущий баланс: {new_total:,}₽.",
        reply_markup=ADMIN_KB,
    )


def register_admin_handlers(dp: Dispatcher) -> None:
    dp.register_message_handler(admin_start, commands=["start"], state="*")

    dp.register_message_handler(admin_course_menu, lambda m: m.text == "🔄 Курс", state="*")
    dp.register_message_handler(admin_split_toggle, lambda m: m.text == "Split", state="*")
    dp.register_callback_query_handler(rate_edit_start, lambda c: c.data == "rate_edit", state="*")
    dp.register_message_handler(rate_entered, state=RateStates.waiting_rate)
    dp.register_callback_query_handler(rate_calc_start, lambda c: c.data == "rate_calc", state="*")
    dp.register_message_handler(rate_calc_paste_entered, state=RateCalcStates.waiting_paste)

    dp.register_message_handler(admin_reserve, lambda m: m.text == "💰 Резерв", state="*")

    dp.register_callback_query_handler(course_basic_callback, lambda c: c.data == "course_basic", state="*")
    dp.register_callback_query_handler(commission_callback, lambda c: c.data == "course_commission", state="*")
    dp.register_message_handler(commission_entered, state=CourseStates.waiting_commission)

    dp.register_message_handler(announce_start, lambda m: m.text == "✉️ Объявление", state="*")
    dp.register_message_handler(announce_text_entered, state=AnnounceStates.waiting_text)
    dp.register_message_handler(announce_photo_entered, content_types=["photo"], state=AnnounceStates.waiting_photo)
    dp.register_message_handler(announce_skip, commands=["skip"], state=AnnounceStates.waiting_photo)

    dp.register_message_handler(admin_cards_message, lambda m: m.text == "💳 Карты", state="*")
    dp.register_callback_query_handler(card_add_start, lambda c: c.data == "card_add", state="*")
    dp.register_message_handler(card_add_bank, state=CardAddStates.waiting_bank)
    dp.register_message_handler(card_add_sbp, state=CardAddStates.waiting_sbp)
    dp.register_message_handler(card_add_number, state=CardAddStates.waiting_number)
    dp.register_callback_query_handler(card_browse_start, lambda c: c.data == "card_edit_select", state="*")
    dp.register_callback_query_handler(card_browse_prev, lambda c: c.data == "card_browse_prev", state=CardBrowseStates.browsing)
    dp.register_callback_query_handler(card_browse_next, lambda c: c.data == "card_browse_next", state=CardBrowseStates.browsing)
    dp.register_callback_query_handler(card_edit_bank_start, lambda c: c.data.startswith("card_edit_bank:"), state="*")
    dp.register_message_handler(card_edit_bank_entered, state=CardEditBankStates.waiting_bank)
    dp.register_callback_query_handler(card_edit_sbp_start, lambda c: c.data.startswith("card_edit_sbp:"), state="*")
    dp.register_message_handler(card_edit_sbp_entered, state=CardEditSBPStates.waiting_sbp)
    dp.register_callback_query_handler(card_edit_number_start, lambda c: c.data.startswith("card_edit_number:"), state="*")
    dp.register_message_handler(card_edit_number_entered, state=CardEditNumberStates.waiting_number)
    dp.register_callback_query_handler(card_delete, lambda c: c.data.startswith("card_delete:"), state="*")
    dp.register_callback_query_handler(card_toggle_callback, lambda c: c.data.startswith("card_toggle:"), state="*")
    dp.register_callback_query_handler(
        card_withdraw_start,
        lambda c: c.data.startswith("card_withdraw:") or c.data.startswith("card_deposit:"),
        state="*",
    )
    dp.register_message_handler(card_withdraw_entered, state=CardWithdrawStates.waiting_withdraw_amount)

    dp.register_callback_query_handler(reserve_recharge_start, lambda c: c.data == "reserve_recharge", state="*")
    dp.register_message_handler(reserve_amount_entered, state=ReserveRechargeStates.waiting_amount)
    dp.register_message_handler(reserve_date_entered, state=ReserveRechargeStates.waiting_date)
    dp.register_message_handler(reserve_price_entered, state=ReserveRechargeStates.waiting_price)

    dp.register_message_handler(admin_debt_menu, lambda m: m.text == "📉 Долг", state="*")
    dp.register_callback_query_handler(admin_debt_pick_start, lambda c: (c.data or "").startswith("admin_debt_pick:"), state="*")
    dp.register_message_handler(admin_debt_add_entered, state=AdminDebtStates.waiting_add)
    dp.register_message_handler(admin_debt_set_entered, state=AdminDebtStates.waiting_set)

    dp.register_message_handler(admin_distribution_toggle, lambda m: m.text == "VidraPay", state="*")
    dp.register_callback_query_handler(admin_casino_menu_callback, lambda c: c.data == "admin_casino_menu", state="*")
    dp.register_callback_query_handler(admin_casino_add_start, lambda c: c.data == "admin_casino_add", state="*")
    dp.register_callback_query_handler(admin_casino_delete_start, lambda c: c.data == "admin_casino_delete", state="*")
    dp.register_callback_query_handler(admin_casino_delete_one, lambda c: (c.data or "").startswith("admin_casino_delete_one:"), state="*")
    dp.register_message_handler(admin_casino_name_entered, state=AdminCasinoStates.waiting_name)
    dp.register_message_handler(admin_casino_url_entered, state=AdminCasinoStates.waiting_url)
    dp.register_message_handler(admin_casino_telegram_entered, state=AdminCasinoStates.waiting_telegram)

    dp.register_callback_query_handler(back_to_admin, lambda c: c.data == "admin_menu", state="*")