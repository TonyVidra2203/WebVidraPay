from __future__ import annotations

from aiogram import Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.cards import get_card_by_id, get_cards_by_owner, update_card
from handlers.mastercard.menu import is_mastercard_user, mastercard_main_keyboard


class MasterCardLimitStates(StatesGroup):
    waiting_min_amount = State()
    waiting_max_amount = State()


def _fmt_amount(value) -> str:
    if value in (None, "", "—"):
        return "—"

    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"

    if amount.is_integer():
        return f"{int(amount)} ₽"

    return f"{amount:.2f} ₽"


def _parse_optional_amount(raw: str) -> float | None:
    value = (raw or "").replace(",", ".").strip()

    if value in {"", "-", "0"}:
        return None

    try:
        amount = float(value)
    except ValueError:
        raise ValueError("invalid_amount")

    if amount < 0:
        raise ValueError("negative_amount")

    return amount


def _limits_cards_kb(cards: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    for card in cards:
        card_id = int(card["card_id"])
        bank_name = card.get("bank_name") or "Банк"
        min_amount = _fmt_amount(card.get("min_amount_rub"))
        max_amount = _fmt_amount(card.get("max_amount_rub"))

        kb.add(
            InlineKeyboardButton(
                f"#{card_id} {bank_name} — {min_amount} / {max_amount}",
                callback_data=f"mc_limit_edit:{card_id}",
            )
        )

    return kb


async def mastercard_limits_menu(message: types.Message) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        return

    cards = await get_cards_by_owner(message.from_user.id)

    if not cards:
        await message.answer(
            "⚙️ <b>Лимиты</b>\n\n"
            "Сначала добавьте реквизиты в разделе «💳 Карты».",
            parse_mode="HTML",
            reply_markup=mastercard_main_keyboard(),
        )
        return

    await message.answer(
        "⚙️ <b>Лимиты MasterCard</b>\n\n"
        "Выберите реквизиты, для которых нужно изменить минимальную и максимальную сумму:",
        parse_mode="HTML",
        reply_markup=_limits_cards_kb(cards),
    )


async def mc_limit_edit_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not await is_mastercard_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        card_id = int((callback.data or "").split(":")[1])
    except (IndexError, ValueError, TypeError):
        await callback.answer("Ошибка данных", show_alert=True)
        return

    card = await get_card_by_id(card_id)

    if not card or int(card.get("owner_id") or 0) != int(callback.from_user.id):
        await callback.answer("Реквизиты не найдены", show_alert=True)
        return

    await callback.answer()
    await state.finish()
    await state.update_data(mc_limit_card_id=card_id)

    await callback.message.answer(
        "⚙️ Введите новый минимальный лимит в рублях.\n\n"
        "Например: <b>3000</b>\n"
        "Если минимума нет — отправьте <b>-</b>",
        parse_mode="HTML",
    )

    await MasterCardLimitStates.waiting_min_amount.set()


async def mc_limit_min_save(message: types.Message, state: FSMContext) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        await state.finish()
        return

    raw = (message.text or "").strip()

    try:
        min_amount = _parse_optional_amount(raw)
    except ValueError:
        await message.answer(
            "⚠️ Введите сумму цифрами. Например: 3000. "
            "Если минимума нет — отправьте -"
        )
        return

    if min_amount is not None and min_amount < 100:
        await message.answer(
            "⚠️ Минимальный лимит слишком маленький. "
            "Укажите от 100 ₽ или отправьте -."
        )
        return

    await state.update_data(mc_limit_min_amount=min_amount)

    await message.answer(
        "⚙️ Введите новый максимальный лимит в рублях.\n\n"
        "Например: <b>50000</b>\n"
        "Если максимума нет — отправьте <b>-</b>",
        parse_mode="HTML",
    )

    await MasterCardLimitStates.waiting_max_amount.set()


async def mc_limit_max_save(message: types.Message, state: FSMContext) -> None:
    if not await is_mastercard_user(message.from_user.id):
        await message.answer("⛔ У вас нет доступа.")
        await state.finish()
        return

    raw = (message.text or "").strip()

    try:
        max_amount = _parse_optional_amount(raw)
    except ValueError:
        await message.answer(
            "⚠️ Введите сумму цифрами. Например: 50000. "
            "Если максимума нет — отправьте -"
        )
        return

    data = await state.get_data()
    card_id = int(data.get("mc_limit_card_id") or 0)
    min_amount = data.get("mc_limit_min_amount")

    if not card_id:
        await message.answer("⚠️ Не удалось определить реквизиты.")
        await state.finish()
        return

    if min_amount is not None and max_amount is not None:
        if float(max_amount) < float(min_amount):
            await message.answer("⚠️ Максимальный лимит не может быть меньше минимального.")
            return

    card = await get_card_by_id(card_id)

    if not card or int(card.get("owner_id") or 0) != int(message.from_user.id):
        await message.answer(
            "⚠️ Реквизиты не найдены.",
            reply_markup=mastercard_main_keyboard(),
        )
        await state.finish()
        return

    await update_card(
        card_id,
        min_amount_rub=min_amount,
        max_amount_rub=max_amount,
    )

    await state.finish()

    await message.answer(
        "✅ Лимиты обновлены.\n\n"
        f"Мин. сумма: <b>{_fmt_amount(min_amount)}</b>\n"
        f"Макс. сумма: <b>{_fmt_amount(max_amount)}</b>",
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
        mc_limit_min_save,
        state=MasterCardLimitStates.waiting_min_amount,
    )
    dp.register_message_handler(
        mc_limit_max_save,
        state=MasterCardLimitStates.waiting_max_amount,
    )
