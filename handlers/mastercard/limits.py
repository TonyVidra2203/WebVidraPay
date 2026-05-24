from __future__ import annotations

from aiogram import Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.mastercard import (
    get_mastercard_cards,
    update_mastercard_card_min_amount,
)
from handlers.mastercard.menu import is_mastercard_user, mastercard_main_keyboard


class MasterCardLimitStates(StatesGroup):
    waiting_min_amount = State()


def _limits_cards_kb(cards: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    for card in cards:
        card_id = int(card["id"])
        bank_name = card.get("bank_name") or "Банк"
        min_amount = int(card.get("min_amount") or 0)

        kb.add(
            InlineKeyboardButton(
                f"#{card_id} {bank_name} — мин. {min_amount} ₽",
                callback_data=f"mc_limit_edit:{card_id}",
            )
        )

    return kb


async def mastercard_limits_menu(message: types.Message) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    cards = await get_mastercard_cards(message.from_user.id)

    if not cards:
        await message.answer(
            "⚙️ <b>Лимиты</b>\n\n"
            "Сначала добавьте реквизиты в разделе «💳 Мои реквизиты».",
            parse_mode="HTML",
            reply_markup=mastercard_main_keyboard(),
        )
        return

    await message.answer(
        "⚙️ <b>Лимиты MasterCard</b>\n\n"
        "Выберите реквизиты, для которых нужно изменить минимальное поступление:",
        parse_mode="HTML",
        reply_markup=_limits_cards_kb(cards),
    )


async def mc_limit_edit_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        card_id = int((callback.data or "").split(":")[1])
    except Exception:
        await callback.answer("Ошибка данных", show_alert=True)
        return

    await callback.answer()
    await state.finish()
    await state.update_data(mc_limit_card_id=card_id)

    await callback.message.answer(
        "⚙️ Введите новый минимальный лимит поступления в рублях.\n\n"
        "Например: <b>3000</b>",
        parse_mode="HTML",
    )

    await MasterCardLimitStates.waiting_min_amount.set()


async def mc_limit_save(message: types.Message, state: FSMContext) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        await state.finish()
        return

    raw = (message.text or "").strip()

    if not raw.isdigit():
        await message.answer("⚠️ Введите сумму цифрами. Например: 3000")
        return

    min_amount = int(raw)

    if min_amount < 100:
        await message.answer("⚠️ Минимальный лимит слишком маленький.")
        return

    data = await state.get_data()
    card_id = int(data.get("mc_limit_card_id") or 0)

    if not card_id:
        await message.answer("⚠️ Не удалось определить реквизиты.")
        await state.finish()
        return

    ok = await update_mastercard_card_min_amount(
        card_id=card_id,
        owner_id=message.from_user.id,
        min_amount=min_amount,
    )

    await state.finish()

    if not ok:
        await message.answer(
            "⚠️ Реквизиты не найдены.",
            reply_markup=mastercard_main_keyboard(),
        )
        return

    await message.answer(
        f"✅ Минимальное поступление обновлено: <b>{min_amount} ₽</b>",
        parse_mode="HTML",
        reply_markup=mastercard_main_keyboard(),
    )


def register_mastercard_limit_handlers(dp: Dispatcher) -> None:
    dp.register_message_handler(mastercard_limits_menu, text="⚙️ Лимиты", state="*")
    dp.register_callback_query_handler(
        mc_limit_edit_start,
        lambda c: c.data and c.data.startswith("mc_limit_edit:"),
        state="*",
    )
    dp.register_message_handler(
        mc_limit_save,
        state=MasterCardLimitStates.waiting_min_amount,
    )