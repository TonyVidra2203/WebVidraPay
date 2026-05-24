import asyncio
import logging
import re
from contextlib import suppress

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.casino_wallets import (
    get_casino_phone,
    get_casino_wallet,
    reset_casino_profile,
    upsert_casino_phone,
    upsert_casino_wallet,
)
from db.casinos import ensure_default_casinos, get_casino_by_key, init_casinos_table, list_casinos
from handlers.buy.p2p import _normalize_phone, _paycore_create_and_send
from handlers.support import _append_and_update, _ensure_support_thread
from keyboards.inline import Callback
from utils.helpers import get_usd_rub

logger = logging.getLogger(__name__)

CASINO_SELECT_PREFIX = "casino_select:"
CASINO_CARD_TOPUP_PREFIX = "casino_card_topup:"
CASINO_CARD_WITHDRAW_PREFIX = "casino_card_withdraw:"
CASINO_CARD_RESET_PREFIX = "casino_card_reset:"
CASINO_WITHDRAW_SUPPORT_PREFIX = "casino_withdraw_support:"
CASINO_CARD_BACK_TO_LIST = "casino_card_back_to_list"
CASINO_WALLET_BACK_TO_LIST = "casino_wallet_back_to_list"

CASINO_TOPUP_COMMISSION_PERCENT = 5.0
CASINO_WITHDRAW_COMMISSION_PERCENT = 10.0


class CasinoStates(StatesGroup):
    waiting_wallet = State()
    waiting_amount = State()
    waiting_phone = State()
    waiting_owner_phone = State()


DEFAULT_CASINO_DATA = {
    "riobet": {
        "name": "RioBet",
        "url": "https://riobet.com",
        "telegram": "@riobetbonus",
    },
    "vodka": {
        "name": "Vodka",
        "url": "https://vodka.bet",
        "telegram": "@VodkaCasino",
    },
    "melstroy": {
        "name": "Melstroy",
        "url": "https://melstroy.com",
        "telegram": "—",
    },
    "pokerdom": {
        "name": "PokerDom",
        "url": "https://pokerdom.com",
        "telegram": "—",
    },
}


async def _ensure_casino_storage_ready() -> None:
    await init_casinos_table()
    await ensure_default_casinos(DEFAULT_CASINO_DATA)


async def _get_casino(casino_key: str) -> dict:
    item = await get_casino_by_key(casino_key)
    return item or {}


async def casino_list_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    items = await list_casinos()

    row = []
    for item in items:
        name = item.get("name") or "Казино"
        casino_key = item.get("casino_key") or ""
        row.append(
            InlineKeyboardButton(
                name,
                callback_data=f"{CASINO_SELECT_PREFIX}{casino_key}",
            )
        )
        if len(row) == 2:
            kb.row(*row)
            row = []

    if row:
        kb.row(*row)

    return kb


def casino_card_keyboard(casino_key: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)

    kb.row(
        InlineKeyboardButton("💸 Пополнить", callback_data=f"{CASINO_CARD_TOPUP_PREFIX}{casino_key}"),
        InlineKeyboardButton("💵 Вывести", callback_data=f"{CASINO_CARD_WITHDRAW_PREFIX}{casino_key}"),
    )

    kb.row(
        InlineKeyboardButton("🔄 Сбросить", callback_data=f"{CASINO_CARD_RESET_PREFIX}{casino_key}"),
    )

    kb.add(
        InlineKeyboardButton("⬅️ Назад", callback_data=CASINO_CARD_BACK_TO_LIST),
    )

    return kb


def casino_wallet_input_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("⬅️ Назад", callback_data=CASINO_WALLET_BACK_TO_LIST),
    )
    return kb


def casino_withdraw_support_keyboard(casino_key: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            "🛟 Поддержка по выводу",
            callback_data=f"{CASINO_WITHDRAW_SUPPORT_PREFIX}{casino_key}",
        )
    )
    kb.add(
        InlineKeyboardButton("⬅️ Назад", callback_data=CASINO_CARD_BACK_TO_LIST),
    )
    return kb


def _casino_key_from_callback(data: str) -> str:
    data = str(data or "")
    if data.startswith(CASINO_SELECT_PREFIX):
        return data.replace(CASINO_SELECT_PREFIX, "", 1).strip()
    return ""


def _is_valid_usdt_trc20_wallet(wallet: str) -> bool:
    wallet = str(wallet or "").strip()
    wallet = re.sub(r"[\s\u00A0\u2007\u202F]+", "", wallet)
    return (
        wallet.startswith("T")
        and 30 <= len(wallet) <= 60
        and bool(re.fullmatch(r"[A-Za-z0-9]+", wallet))
    )


def _sanitize_number_text(raw: str) -> str:
    txt = (raw or "").strip().replace(",", ".")
    return re.sub(r"[\s\u00A0\u2007\u202F]", "", txt)


def _is_valid_decimal(txt: str) -> bool:
    if any(ch in txt for ch in ("e", "E", "+", "-")):
        return False
    if txt.count(".") > 1:
        return False
    return bool(re.fullmatch(r"\d+(\.\d+)?", txt))


def _casino_total_with_fee(rub_amount: float) -> int:
    return int(round(float(rub_amount)))


async def _send_temporary_notice(bot: Bot, user_id: int, text: str, seconds: int = 15) -> None:
    msg = await bot.send_message(
        user_id,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    async def _cleanup() -> None:
        await asyncio.sleep(seconds)
        with suppress(Exception):
            await bot.delete_message(user_id, msg.message_id)

    asyncio.create_task(_cleanup())


async def _casino_card_text(casino_key: str, user_id: int) -> str:
    casino = await _get_casino(casino_key)
    if not casino:
        return "🎰 <b>Казино</b>\n\nИнформация временно недоступна."

    saved_wallet = await get_casino_wallet(user_id, casino_key)
    saved_phone = await get_casino_phone(user_id, casino_key)

    if saved_wallet:
        if len(saved_wallet) > 12:
            wallet_display = f"{saved_wallet[:6]}…{saved_wallet[-6:]}"
        else:
            wallet_display = saved_wallet
        wallet_block = f"<code>{wallet_display}</code>"
    else:
        wallet_block = "<i>не указан</i>"

    phone_block = f"<code>{saved_phone}</code>" if saved_phone else "<i>не указан</i>"

    casino_url = casino.get("url") or "—"
    casino_telegram = casino.get("telegram") or "—"
    casino_name = casino.get("name") or "Казино"

    return (
        f"🎰 <b>{casino_name}</b>\n\n"
        f"🌍 {casino_url}\n"
        f"🔗 {casino_telegram}\n"
        "━━━━━━━━━━━━━━━\n"
        f"📥 {wallet_block}\n"
        f"📱 {phone_block}"
    )


async def _show_casino_list(bot: Bot, user_id: int) -> None:
    await _ensure_casino_storage_ready()

    items = await list_casinos()
    if not items:
        await bot.send_message(
            user_id,
            "🎰 Раздел казино временно недоступен.\n\nСписок казино пока пуст.",
        )
        return

    await bot.send_message(
        user_id,
        "⚠️ <b>ВНИМАНИЕ</b>\n\n"
        "Для пополнения используется <b>QR-код (СБП)</b> — такой способ оплаты не вызывает у банка лишних вопросов "
        "и является безопасным для внесения депозита.\n\n"
        "💸 <b>Условия:</b>\n"
        "• Ввод депозита в <b>любое казино</b> — <b>без процентов</b> и <b>на любую сумму</b>\n"
        "• Вывод выигрыша — <b>10%</b> комиссии, переводом <b>на любую вашу карту</b>\n\n"
        "🔐 <b>Важно:</b>\n"
        "• Перед первым пополнением требуется <b>РАЗОВАЯ верификация</b>\n"
        "• Пополнять можно только с <b>ВАШЕГО верифицированного номера телефона</b>\n\n"
        "👇 <b>Выберите казино:</b>",
        parse_mode="HTML",
        reply_markup=await casino_list_keyboard(),
    )


async def _show_casino_card(bot: Bot, user_id: int, casino_key: str) -> None:
    await bot.send_message(
        user_id,
        await _casino_card_text(casino_key, user_id),
        parse_mode="HTML",
        reply_markup=casino_card_keyboard(casino_key),
        disable_web_page_preview=True,
    )


async def _ask_casino_wallet(bot: Bot, user_id: int, state: FSMContext, casino_key: str) -> None:
    casino = await _get_casino(casino_key)
    casino_name = casino.get("name", "казино")
    casino_url = casino.get("url") or "—"
    casino_telegram = casino.get("telegram") or "—"

    await state.update_data(
        casino_key=casino_key,
        casino_next_action="profile_collect_wallet",
    )

    msg = await bot.send_message(
        user_id,
        f"🎰 <b>{casino_name}</b>\n"
        f"🌍 {casino_url}\n"
        f"🔗 {casino_telegram}\n\n"
        "Отправьте номер кошелька выбранного казино для пополнения в "
        "<b>USDT сети (TRC20 / TRX)</b>\n\n"
        "⚠️ Пришлите кошелёк одним сообщением",
        parse_mode="HTML",
        reply_markup=casino_wallet_input_keyboard(),
        disable_web_page_preview=True,
    )
    await state.update_data(casino_wallet_msg_id=msg.message_id)
    await CasinoStates.waiting_wallet.set()


async def _ask_casino_owner_phone(bot: Bot, user_id: int, state: FSMContext, casino_key: str) -> None:
    casino = await _get_casino(casino_key)
    casino_name = casino.get("name", "казино")

    await state.update_data(
        casino_key=casino_key,
        casino_next_action="profile_collect_owner_phone",
    )

    msg = await bot.send_message(
        user_id,
        f"📱 <b>{casino_name}</b>\n\n"
        "Теперь введите номер телефона <b>владельца карты</b>, "
        "с которого будет производиться перевод и который должен быть верифицирован.\n\n",
    )
    await state.update_data(casino_owner_phone_msg_id=msg.message_id)
    await CasinoStates.waiting_owner_phone.set()


async def _start_casino_profile_flow(bot: Bot, user_id: int, state: FSMContext, casino_key: str) -> None:
    await state.finish()
    await _ask_casino_wallet(bot, user_id, state, casino_key)


async def _ensure_casino_profile_or_show_card(
    bot: Bot,
    user_id: int,
    state: FSMContext,
    casino_key: str,
) -> None:
    saved_wallet = await get_casino_wallet(user_id, casino_key)
    saved_phone = await get_casino_phone(user_id, casino_key)

    if not saved_wallet:
        await _ask_casino_wallet(bot, user_id, state, casino_key)
        return

    if not saved_phone:
        await _ask_casino_owner_phone(bot, user_id, state, casino_key)
        return

    await state.finish()
    await _show_casino_card(bot, user_id, casino_key)


async def _start_casino_amount_flow(bot: Bot, user_id: int, state: FSMContext, casino_key: str, wallet: str) -> None:
    casino = await _get_casino(casino_key)
    casino_name = casino.get("name", "казино")

    await state.update_data(
        casino_key=casino_key,
        casino_wallet=wallet,
        wallet=wallet,
        asset="USDT",
        paycore_only=True,
        binance_mode=False,
        casino_flow=True,
    )

    msg = await bot.send_message(
        user_id,
        f"💸 <b>{casino_name}</b>\n\n"
        "Введите сумму пополнения в рублях.\n"
        "Пример: 1200",
        parse_mode="HTML",
    )

    await state.update_data(casino_amount_msg_id=msg.message_id)
    await CasinoStates.waiting_amount.set()


async def casino_entrypoint(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    with suppress(Exception):
        await callback.message.delete()

    with suppress(Exception):
        await state.finish()

    await _show_casino_list(callback.bot, callback.from_user.id)


async def casino_select_stub(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    with suppress(Exception):
        await callback.message.delete()

    casino_key = _casino_key_from_callback(callback.data or "")
    if not casino_key:
        with suppress(Exception):
            await state.finish()
        await callback.bot.send_message(
            callback.from_user.id,
            "⚠️ Не удалось определить выбранное казино.",
        )
        return

    casino = await _get_casino(casino_key)
    if not casino:
        with suppress(Exception):
            await state.finish()
        await callback.bot.send_message(
            callback.from_user.id,
            "⚠️ Выбранное казино не найдено.",
        )
        await _show_casino_list(callback.bot, callback.from_user.id)
        return

    await _ensure_casino_profile_or_show_card(
        callback.bot,
        callback.from_user.id,
        state,
        casino_key,
    )


async def casino_card_back_to_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    with suppress(Exception):
        await callback.message.delete()

    with suppress(Exception):
        await state.finish()

    await _show_casino_list(callback.bot, callback.from_user.id)


async def casino_wallet_back_to_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    with suppress(Exception):
        await callback.message.delete()

    with suppress(Exception):
        await state.finish()

    await _show_casino_list(callback.bot, callback.from_user.id)


async def casino_card_reset_stub(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    casino_key = (callback.data or "").replace(CASINO_CARD_RESET_PREFIX, "", 1)
    casino = await _get_casino(casino_key)
    casino_name = casino.get("name", "казино")

    if not casino_key or not casino:
        with suppress(Exception):
            await state.finish()
        await _send_temporary_notice(
            callback.bot,
            callback.from_user.id,
            "⚠️ Не удалось определить казино для сброса.",
        )
        return

    with suppress(Exception):
        await callback.message.delete()

    with suppress(Exception):
        await state.finish()

    await reset_casino_profile(callback.from_user.id, casino_key)

    await callback.bot.send_message(
        callback.from_user.id,
        f"🔄 Данные для <b>{casino_name}</b> сброшены.\n\n"
        "Введите данные заново.",
        parse_mode="HTML",
    )

    await _start_casino_profile_flow(
        callback.bot,
        callback.from_user.id,
        state,
        casino_key,
    )


async def casino_card_topup_stub(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    casino_key = (callback.data or "").replace(CASINO_CARD_TOPUP_PREFIX, "", 1)
    casino = await _get_casino(casino_key)
    casino_name = casino.get("name", "казино")

    if not casino_key or not casino:
        with suppress(Exception):
            await state.finish()
        await _send_temporary_notice(
            callback.bot,
            callback.from_user.id,
            "⚠️ Не удалось определить казино.",
        )
        return

    saved_wallet = await get_casino_wallet(callback.from_user.id, casino_key)
    saved_phone = await get_casino_phone(callback.from_user.id, casino_key)

    if not saved_wallet or not saved_phone:
        with suppress(Exception):
            await callback.message.delete()

        await callback.bot.send_message(
            callback.from_user.id,
            f"⚠️ Для <b>{casino_name}</b> сначала нужно заполнить данные профиля.",
            parse_mode="HTML",
        )

        await _ensure_casino_profile_or_show_card(
            callback.bot,
            callback.from_user.id,
            state,
            casino_key,
        )
        return

    with suppress(Exception):
        await callback.message.delete()

    with suppress(Exception):
        await state.finish()

    await _start_casino_amount_flow(
        callback.bot,
        callback.from_user.id,
        state,
        casino_key,
        saved_wallet,
    )


async def casino_card_withdraw_stub(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    with suppress(Exception):
        await callback.message.delete()

    with suppress(Exception):
        await state.finish()

    casino_key = (callback.data or "").replace(CASINO_CARD_WITHDRAW_PREFIX, "", 1)
    casino = await _get_casino(casino_key)
    casino_name = casino.get("name", "казино")

    if not casino_key or not casino:
        await callback.bot.send_message(
            callback.from_user.id,
            "⚠️ Не удалось определить казино.",
        )
        await _show_casino_list(callback.bot, callback.from_user.id)
        return

    await callback.bot.send_message(
        callback.from_user.id,
        f"💵 <b>{casino_name}</b>\n\n"
        f"Вывод выигрыша выполняется <b>переводом на любую вашу карту</b>.\n\n"
        f"⚠️ Комиссия за вывод составляет <b>{int(CASINO_WITHDRAW_COMMISSION_PERCENT)}%</b>.\n\n"
        "Чтобы оформить заявку на вывод, нажмите кнопку ниже. "
        "После этого администраторы получат уведомление, и с вами откроется чат для согласования суммы и реквизитов.",
        parse_mode="HTML",
        reply_markup=casino_withdraw_support_keyboard(casino_key),
        disable_web_page_preview=True,
    )


async def casino_withdraw_support_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    with suppress(Exception):
        await callback.message.delete()

    with suppress(Exception):
        await state.finish()

    casino_key = (callback.data or "").replace(CASINO_WITHDRAW_SUPPORT_PREFIX, "", 1)
    casino = await _get_casino(casino_key)
    casino_name = casino.get("name", "казино")

    if not casino_key or not casino:
        await callback.bot.send_message(
            callback.from_user.id,
            "⚠️ Не удалось определить казино.",
        )
        await _show_casino_list(callback.bot, callback.from_user.id)
        return

    user_id = callback.from_user.id
    bot = callback.bot

    await _ensure_support_thread(bot, user_id)
    await _append_and_update(
        bot,
        user_id,
        role="user",
        text=(
            f"🎰 <b>Заявка на вывод выигрыша</b>\n"
            f"Казино: <b>{casino_name}</b>\n"
            f"Комиссия на вывод: <b>{int(CASINO_WITHDRAW_COMMISSION_PERCENT)}%</b>\n"
            "Пользователь хочет вывести выигрыш на карту."
        ),
    )


async def handle_casino_wallet_input(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    raw_wallet = (message.text or "").strip()
    wallet = re.sub(r"[\s\u00A0\u2007\u202F]+", "", raw_wallet)

    with suppress(Exception):
        await message.delete()

    data = await state.get_data()

    wallet_msg_id = data.get("casino_wallet_msg_id")
    if wallet_msg_id:
        with suppress(Exception):
            await message.bot.delete_message(user_id, wallet_msg_id)

    casino_key = str(data.get("casino_key") or "").strip()
    casino_next_action = str(data.get("casino_next_action") or "").strip()
    casino = await _get_casino(casino_key)
    casino_name = casino.get("name", "казино")

    if not casino_key or not casino:
        await state.finish()
        await message.bot.send_message(
            user_id,
            "⚠️ Не удалось определить казино. Начните заново из списка казино.",
        )
        return

    if not _is_valid_usdt_trc20_wallet(wallet):
        await message.bot.send_message(
            user_id,
            "⚠️ Похоже, это некорректный <b>USDT TRC20-кошелёк</b>.\n\n"
            "Обычно он начинается с <b>T</b> и не содержит пробелов.",
            parse_mode="HTML",
        )
        return

    await upsert_casino_wallet(user_id, casino_key, wallet)

    if casino_next_action == "topup":
        with suppress(Exception):
            await state.update_data(
                casino_key=casino_key,
                casino_wallet=wallet,
                wallet=wallet,
                casino_next_action="topup",
            )
        await _start_casino_amount_flow(message.bot, user_id, state, casino_key, wallet)
        return

    if casino_next_action == "profile_collect_wallet":
        await _ask_casino_owner_phone(message.bot, user_id, state, casino_key)
        return

    await state.finish()

    await message.bot.send_message(
        user_id,
        f"✅ Кошелёк для <b>{casino_name}</b> сохранён.",
        parse_mode="HTML",
    )
    await _show_casino_card(message.bot, user_id, casino_key)


async def handle_casino_owner_phone_input(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    phone_raw = message.text or ""
    phone = _normalize_phone(phone_raw)

    with suppress(Exception):
        await message.delete()

    data = await state.get_data()

    owner_phone_msg_id = data.get("casino_owner_phone_msg_id")
    if owner_phone_msg_id:
        with suppress(Exception):
            await message.bot.delete_message(user_id, owner_phone_msg_id)

    casino_key = str(data.get("casino_key") or "").strip()
    casino = await _get_casino(casino_key)
    casino_name = casino.get("name", "казино")

    if not casino_key or not casino:
        await state.finish()
        await message.bot.send_message(
            user_id,
            "⚠️ Не удалось определить казино. Начните заново из списка казино.",
        )
        return

    digits = phone[1:] if phone.startswith("+") else phone
    digits = re.sub(r"\D+", "", digits or "")

    if not digits or not (10 <= len(digits) <= 15):
        await message.bot.send_message(
            user_id,
            "⚠️ Не получилось распознать номер.\n\n"
            "Введите номер телефона цифрами.\n"
            "Примеры:\n"
            "<code>8 999 123-45-67</code>\n"
            "<code>+7(999)1234567</code>\n"
            "<code>9991234567</code>",
            parse_mode="HTML",
        )
        return

    if len(digits) == 10:
        phone = f"+7{digits}"
    else:
        phone = f"+{digits}"

    await upsert_casino_phone(user_id, casino_key, phone)
    await state.finish()

    await message.bot.send_message(
        user_id,
        f"✅ Номер телефона для <b>{casino_name}</b> сохранён.",
        parse_mode="HTML",
    )
    await _show_casino_card(message.bot, user_id, casino_key)


async def handle_casino_amount_input(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    raw = (message.text or "").strip()

    with suppress(Exception):
        await message.delete()

    data = await state.get_data()

    amount_msg_id = data.get("casino_amount_msg_id")
    if amount_msg_id:
        with suppress(Exception):
            await message.bot.delete_message(user_id, amount_msg_id)

    casino_key = str(data.get("casino_key") or "").strip()
    wallet = str(data.get("casino_wallet") or data.get("wallet") or "").strip()

    if not casino_key or not wallet:
        await state.finish()
        await message.bot.send_message(
            user_id,
            "⚠️ Не удалось восстановить данные для пополнения. Начните заново.",
        )
        return

    casino = await _get_casino(casino_key)
    if not casino:
        await state.finish()
        await message.bot.send_message(
            user_id,
            "⚠️ Выбранное казино не найдено. Начните заново.",
        )
        return

    saved_phone = await get_casino_phone(user_id, casino_key)
    if not saved_phone:
        await state.finish()
        await message.bot.send_message(
            user_id,
            "⚠️ Не найден сохранённый номер телефона для оплаты. Заполните данные казино заново.",
        )
        await _ensure_casino_profile_or_show_card(
            message.bot,
            user_id,
            state,
            casino_key,
        )
        return

    txt = _sanitize_number_text(raw)
    if not txt or not _is_valid_decimal(txt):
        await message.bot.send_message(
            user_id,
            "⚠️ Введите сумму пополнения <b>цифрами</b>.\n\n"
            "Примеры: <b>1200</b>, <b>5000</b>",
            parse_mode="HTML",
        )
        return

    try:
        rub_amount = float(txt)
    except Exception:
        await message.bot.send_message(
            user_id,
            "⚠️ Не удалось распознать сумму. Введите её ещё раз цифрами.",
        )
        return

    if rub_amount <= 0:
        await message.bot.send_message(
            user_id,
            "⚠️ Сумма должна быть больше нуля.",
        )
        return

    usd_rub = await get_usd_rub()
    if not usd_rub or float(usd_rub) <= 0:
        await message.bot.send_message(
            user_id,
            "⚠️ Не удалось получить курс USD→RUB. Попробуйте позже.",
        )
        return

    total_min = _casino_total_with_fee(float(rub_amount))
    net_rub_amount = float(rub_amount) * (1 - CASINO_TOPUP_COMMISSION_PERCENT / 100.0)
    usdt_amount = round(float(net_rub_amount) / float(usd_rub), 8)

    if usdt_amount <= 0:
        await message.bot.send_message(
            user_id,
            "⚠️ Сумма слишком мала. Укажите сумму побольше.",
        )
        return

    await state.update_data(
        casino_key=casino_key,
        casino_wallet=wallet,
        wallet=wallet,
        asset="USDT",
        btc_amount=float(usdt_amount),
        rub_amount=float(rub_amount),
        total_min=int(total_min),
        amount_input_mode="RUB",
        paycore_only=True,
        binance_mode=False,
        casino_flow=True,
        paycore_phone=saved_phone,
    )

    await _paycore_create_and_send(message.bot, state, user_id, saved_phone)


async def handle_casino_phone_input(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    phone_raw = message.text or ""
    phone = _normalize_phone(phone_raw)

    with suppress(Exception):
        await message.delete()

    digits = phone[1:] if phone.startswith("+") else phone
    digits = re.sub(r"\D+", "", digits or "")

    if not digits or not (10 <= len(digits) <= 15):
        await message.bot.send_message(
            user_id,
            "⚠️ Не получилось распознать номер.\n\n"
            "Введите номер телефона цифрами.\n"
            "Примеры:\n"
            "<code>8 999 123-45-67</code>\n"
            "<code>+7(999)1234567</code>\n"
            "<code>9991234567</code>",
            parse_mode="HTML",
        )
        return

    if len(digits) == 10:
        phone = f"+7{digits}"
    else:
        phone = f"+{digits}"

    await state.update_data(paycore_phone=phone)
    await _paycore_create_and_send(message.bot, state, user_id, phone)


async def casino_wallet_not_text(message: types.Message, state: FSMContext) -> None:
    with suppress(Exception):
        await message.delete()

    await message.bot.send_message(
        message.from_user.id,
        "⚠️ Пришлите кошелёк <b>текстом</b>.\n\n"
        "Нужен именно <b>USDT TRC20-адрес</b>.",
        parse_mode="HTML",
    )


async def casino_owner_phone_not_text(message: types.Message, state: FSMContext) -> None:
    with suppress(Exception):
        await message.delete()

    await message.bot.send_message(
        message.from_user.id,
        "⚠️ Пришлите номер телефона <b>текстом</b>.\n\n"
        "Пример: <code>+79991234567</code>",
        parse_mode="HTML",
    )


async def casino_amount_not_text(message: types.Message, state: FSMContext) -> None:
    with suppress(Exception):
        await message.delete()

    await message.bot.send_message(
        message.from_user.id,
        "⚠️ Пришлите сумму <b>текстом</b>.\n\n"
        "Пример: <b>1200</b>",
        parse_mode="HTML",
    )


async def casino_phone_not_text(message: types.Message, state: FSMContext) -> None:
    with suppress(Exception):
        await message.delete()

    await message.bot.send_message(
        message.from_user.id,
        "⚠️ Пришлите номер телефона <b>текстом</b>.\n\n"
        "Пример: <code>+79991234567</code>",
        parse_mode="HTML",
    )


def register_casino_handlers(dp: Dispatcher) -> None:
    dp.register_callback_query_handler(
        casino_entrypoint,
        lambda c: c.data == Callback.CASINO_TOPUP_CRYPTO,
        state="*",
    )
    dp.register_callback_query_handler(
        casino_select_stub,
        lambda c: isinstance(c.data, str) and c.data.startswith(CASINO_SELECT_PREFIX),
        state="*",
    )
    dp.register_callback_query_handler(
        casino_card_back_to_list,
        lambda c: c.data == CASINO_CARD_BACK_TO_LIST,
        state="*",
    )
    dp.register_callback_query_handler(
        casino_wallet_back_to_list,
        lambda c: c.data == CASINO_WALLET_BACK_TO_LIST,
        state="*",
    )
    dp.register_callback_query_handler(
        casino_card_topup_stub,
        lambda c: isinstance(c.data, str) and c.data.startswith(CASINO_CARD_TOPUP_PREFIX),
        state="*",
    )
    dp.register_callback_query_handler(
        casino_card_withdraw_stub,
        lambda c: isinstance(c.data, str) and c.data.startswith(CASINO_CARD_WITHDRAW_PREFIX),
        state="*",
    )
    dp.register_callback_query_handler(
        casino_withdraw_support_start,
        lambda c: isinstance(c.data, str) and c.data.startswith(CASINO_WITHDRAW_SUPPORT_PREFIX),
        state="*",
    )
    dp.register_callback_query_handler(
        casino_card_reset_stub,
        lambda c: isinstance(c.data, str) and c.data.startswith(CASINO_CARD_RESET_PREFIX),
        state="*",
    )

    dp.register_message_handler(
        handle_casino_wallet_input,
        content_types=[types.ContentType.TEXT],
        state=CasinoStates.waiting_wallet,
    )
    dp.register_message_handler(
        casino_wallet_not_text,
        content_types=types.ContentType.ANY,
        state=CasinoStates.waiting_wallet,
    )

    dp.register_message_handler(
        handle_casino_owner_phone_input,
        content_types=[types.ContentType.TEXT],
        state=CasinoStates.waiting_owner_phone,
    )
    dp.register_message_handler(
        casino_owner_phone_not_text,
        content_types=types.ContentType.ANY,
        state=CasinoStates.waiting_owner_phone,
    )

    dp.register_message_handler(
        handle_casino_amount_input,
        content_types=[types.ContentType.TEXT],
        state=CasinoStates.waiting_amount,
    )
    dp.register_message_handler(
        casino_amount_not_text,
        content_types=types.ContentType.ANY,
        state=CasinoStates.waiting_amount,
    )