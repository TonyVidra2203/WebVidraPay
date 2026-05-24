from __future__ import annotations

from aiogram import Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.mastercard import (
    add_mastercard_card,
    delete_mastercard_card,
    get_mastercard_cards,
    toggle_mastercard_card,
)
from handlers.mastercard.menu import is_mastercard_user, mastercard_main_keyboard


class MasterCardCardStates(StatesGroup):
    bank_name = State()
    sbp_phone = State()
    card_number = State()
    min_amount = State()


def _cards_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("➕ Добавить реквизиты", callback_data="mc_card_add"))
    return kb


def _card_actions_kb(card_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔄 Вкл/выкл", callback_data=f"mc_card_toggle:{card_id}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"mc_card_delete:{card_id}"),
    )
    return kb


async def mastercard_cards_menu(message: types.Message) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    cards = await get_mastercard_cards(message.from_user.id)

    if not cards:
        await message.answer(
            "💳 <b>Мои реквизиты</b>\n\n"
            "У вас пока нет добавленных реквизитов.",
            parse_mode="HTML",
            reply_markup=_cards_menu_kb(),
        )
        return

    await message.answer(
        "💳 <b>Мои реквизиты</b>\n\n"
        "Ваши добавленные карты:",
        parse_mode="HTML",
        reply_markup=_cards_menu_kb(),
    )

    for card in cards:
        status = "🟢 Активна" if int(card.get("is_active") or 0) else "🔴 Выключена"

        text = (
            f"💳 <b>Реквизиты #{card['id']}</b>\n\n"
            f"Банк: <b>{card.get('bank_name') or '—'}</b>\n"
            f"СБП: <code>{card.get('sbp_phone') or '—'}</code>\n"
            f"Карта: <code>{card.get('card_number') or '—'}</code>\n"
            f"Минимальное поступление: <b>{int(card.get('min_amount') or 0)} ₽</b>\n"
            f"Статус: <b>{status}</b>"
        )

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=_card_actions_kb(int(card["id"])),
        )


async def mc_card_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.answer()
    await state.finish()

    await callback.message.answer(
        "🏦 Введите название банка.\n\n"
        "Например: <b>Тинькофф</b>",
        parse_mode="HTML",
    )
    await MasterCardCardStates.bank_name.set()


async def mc_card_bank_name(message: types.Message, state: FSMContext) -> None:
    bank_name = (message.text or "").strip()

    if len(bank_name) < 2:
        await message.answer("⚠️ Введите нормальное название банка.")
        return

    await state.update_data(bank_name=bank_name)

    await message.answer(
        "📱 Введите номер СБП.\n\n"
        "Если СБП нет — отправьте <b>-</b>",
        parse_mode="HTML",
    )
    await MasterCardCardStates.sbp_phone.set()


async def mc_card_sbp_phone(message: types.Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    sbp_phone = None if value == "-" else value

    await state.update_data(sbp_phone=sbp_phone)

    await message.answer(
        "💳 Введите номер карты.\n\n"
        "Если карты нет — отправьте <b>-</b>",
        parse_mode="HTML",
    )
    await MasterCardCardStates.card_number.set()


async def mc_card_number(message: types.Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    card_number = None if value == "-" else value

    data = await state.get_data()
    sbp_phone = data.get("sbp_phone")

    if not sbp_phone and not card_number:
        await message.answer("⚠️ Нужно указать хотя бы СБП или номер карты.")
        return

    await state.update_data(card_number=card_number)

    await message.answer(
        "⚙️ Введите минимальное поступление в рублях.\n\n"
        "Например: <b>3000</b>",
        parse_mode="HTML",
    )
    await MasterCardCardStates.min_amount.set()


async def mc_card_min_amount(message: types.Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()

    if not raw.isdigit():
        await message.answer("⚠️ Введите сумму цифрами. Например: 3000")
        return

    min_amount = int(raw)

    if min_amount < 100:
        await message.answer("⚠️ Минимальная сумма слишком маленькая.")
        return

    data = await state.get_data()

    await add_mastercard_card(
        owner_id=message.from_user.id,
        bank_name=data["bank_name"],
        sbp_phone=data.get("sbp_phone"),
        card_number=data.get("card_number"),
        min_amount=min_amount,
    )

    await state.finish()

    await message.answer(
        "✅ Реквизиты добавлены.",
        reply_markup=mastercard_main_keyboard(),
    )


async def mc_card_toggle(callback: types.CallbackQuery) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        card_id = int((callback.data or "").split(":")[1])
    except Exception:
        await callback.answer("Ошибка данных", show_alert=True)
        return

    ok = await toggle_mastercard_card(card_id, callback.from_user.id)
    await callback.answer("Готово" if ok else "Карта не найдена", show_alert=not ok)

    if ok:
        await callback.message.delete()
        await callback.message.answer(
            "🔄 Статус реквизитов изменён.\n\n"
            "Откройте «Мои реквизиты», чтобы посмотреть обновленный список.",
            reply_markup=mastercard_main_keyboard(),
        )


async def mc_card_delete(callback: types.CallbackQuery) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        card_id = int((callback.data or "").split(":")[1])
    except Exception:
        await callback.answer("Ошибка данных", show_alert=True)
        return

    ok = await delete_mastercard_card(card_id, callback.from_user.id)
    await callback.answer("Удалено" if ok else "Карта не найдена", show_alert=not ok)

    if ok:
        await callback.message.delete()
        await callback.message.answer(
            "🗑 Реквизиты удалены.",
            reply_markup=mastercard_main_keyboard(),
        )


def register_mastercard_card_handlers(dp: Dispatcher) -> None:
    dp.register_message_handler(mastercard_cards_menu, text="💳 Карты", state="*")

    dp.register_callback_query_handler(mc_card_add_start, lambda c: c.data == "mc_card_add", state="*")
    dp.register_callback_query_handler(mc_card_toggle, lambda c: c.data and c.data.startswith("mc_card_toggle:"), state="*")
    dp.register_callback_query_handler(mc_card_delete, lambda c: c.data and c.data.startswith("mc_card_delete:"), state="*")

    dp.register_message_handler(mc_card_bank_name, state=MasterCardCardStates.bank_name)
    dp.register_message_handler(mc_card_sbp_phone, state=MasterCardCardStates.sbp_phone)
    dp.register_message_handler(mc_card_number, state=MasterCardCardStates.card_number)
    dp.register_message_handler(mc_card_min_amount, state=MasterCardCardStates.min_amount)