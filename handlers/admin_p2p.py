# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
from __future__ import annotations

import math
from typing import Any, Dict, Optional

from aiogram import Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.exceptions import BotBlocked, CantInitiateConversation

from db.connection import get_db
from db.p2p import get_order_by_id
from handlers.chat.instruction import (
    INSTRUCTION_TEXT_TEMPLATE,
    INSTRUCTION_TEXT_TEMPLATE_USER,
)
from handlers.chat.operator import update_operator_order_card_with_requisites
from handlers.chat.utils import bot_send
from handlers.common import pending_buy_messages
from handlers.chat.instruction import format_requisite_for_user, html_escape


# -----------------------------------------------------------------------------
# Раздел: Состояния FSM
# -----------------------------------------------------------------------------
class AdminP2PStates(StatesGroup):
    """Состояния админского сценария отправки реквизитов P2P."""
    waiting_account = State()
    waiting_bank_name = State()
    waiting_comment = State()
    confirm = State()


# -----------------------------------------------------------------------------
# Раздел: Вспомогательные функции
# -----------------------------------------------------------------------------
async def _safe_delete(bot: types.Bot, chat_id: int, message_id: Optional[int]) -> None:
    """Безопасно удаляет сообщение, игнорируя ошибки."""
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def _make_confirm_kb(order_id: int, user_id: int) -> InlineKeyboardMarkup:
    """Создаёт инлайн-клавиатуру подтверждения отправки реквизитов."""
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton(
            "✅ Отправить",
            callback_data=f"admin_p2p_send:{user_id}:{order_id}",
        ),
        InlineKeyboardButton(
            "↩️ Изменить",
            callback_data=f"admin_p2p_edit:{user_id}:{order_id}",
        ),
    )
    kb.add(
        InlineKeyboardButton(
            "❌ Отмена",
            callback_data=f"admin_p2p_cancel:{user_id}:{order_id}",
        )
    )
    return kb


# -----------------------------------------------------------------------------
# Раздел: Рендеринг шаблонов сообщений
# -----------------------------------------------------------------------------
async def _render_admin_template(data: Dict[str, Any]) -> str:
    """Рендерит превью-шаблон для администратора с реквизитами и суммой оплаты."""
    order_id = data["target_order_id"]
    rec = await get_order_by_id(order_id)
    payment = math.ceil(float(rec["total_rub"])) if rec and rec.get("total_rub") is not None else 0

    card = (data.get("account") or "").strip()
    bank = (data.get("bank_name") or "").strip()
    comment = (data.get("comment") or "без комментария").strip()

    text = INSTRUCTION_TEXT_TEMPLATE.format(card=card, bank=bank, comment=comment, amount=payment)

    cleanup_targets = [
        "Реквизиты отправлены пользователю",
        "Реквизиты отправлены пользователю.",
    ]
    lines = [line for line in text.splitlines() if all(target not in line for target in cleanup_targets)]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Раздел: Хэндлеры — запуск, отмена и редактирование
# -----------------------------------------------------------------------------
async def admin_p2p_start(call: types.CallbackQuery, state: FSMContext) -> None:
    """Старт сценария отправки реквизитов: запрос номера карты/СБП."""
    try:
        await call.answer()
    except Exception:
        pass

    try:
        _, user_id_str, order_id_str = call.data.split(":")
        user_id = int(user_id_str)
        order_id = int(order_id_str)
    except Exception:
        await call.message.answer("Некорректные параметры заявки.")
        return

    await state.finish()
    await state.update_data(target_user_id=user_id, target_order_id=order_id, last_prompt_id=None)

    await AdminP2PStates.waiting_account.set()
    prompt = await call.message.answer("💳 Введите <b>номер СБП/номер карты</b>:", parse_mode="HTML")
    await state.update_data(last_prompt_id=prompt.message_id)


async def admin_p2p_cancel(call: types.CallbackQuery, state: FSMContext) -> None:
    """Отмена сценария: удаление служебного сообщения и сброс состояния."""
    await call.answer()
    await _safe_delete(call.bot, call.message.chat.id, call.message.message_id)
    await state.finish()


async def admin_p2p_edit(call: types.CallbackQuery, state: FSMContext) -> None:
    """Редактирование реквизитов: повторный ввод с первого шага."""
    await call.answer()
    data = await state.get_data()
    await _safe_delete(call.bot, call.message.chat.id, data.get("last_prompt_id"))
    await _safe_delete(call.bot, call.message.chat.id, call.message.message_id)

    await AdminP2PStates.waiting_account.set()
    prompt = await call.message.answer(
        "Изменим. Сначала снова введите 💳 <b>номер СБП/номер карты</b>:",
        parse_mode="HTML",
    )
    await state.update_data(last_prompt_id=prompt.message_id)


# -----------------------------------------------------------------------------
# Раздел: Хэндлеры — сбор реквизитов
# -----------------------------------------------------------------------------
async def on_account(msg: types.Message, state: FSMContext) -> None:
    """Принимает номер карты/СБП и запрашивает название банка."""
    data = await state.get_data()
    await _safe_delete(msg.bot, msg.chat.id, data.get("last_prompt_id"))
    await _safe_delete(msg.bot, msg.chat.id, msg.message_id)

    await state.update_data(account=(msg.text or "").strip())
    await AdminP2PStates.waiting_bank_name.set()
    prompt = await msg.answer("🏦 Укажите <b>название банка</b>:", parse_mode="HTML")
    await state.update_data(last_prompt_id=prompt.message_id)


async def on_bank_name(msg: types.Message, state: FSMContext) -> None:
    """Принимает название банка и запрашивает комментарий."""
    data = await state.get_data()
    await _safe_delete(msg.bot, msg.chat.id, data.get("last_prompt_id"))
    await _safe_delete(msg.bot, msg.chat.id, msg.message_id)

    await state.update_data(bank_name=(msg.text or "").strip())
    await AdminP2PStates.waiting_comment.set()
    prompt = await msg.answer(
        "📝 Укажите <b>комментарий</b> (если не нужен — напишите «-»):",
        parse_mode="HTML",
    )
    await state.update_data(last_prompt_id=prompt.message_id)


async def on_comment(msg: types.Message, state: FSMContext) -> None:
    """Принимает комментарий, показывает превью и предлагает подтвердить/изменить."""
    data = await state.get_data()
    await _safe_delete(msg.bot, msg.chat.id, data.get("last_prompt_id"))
    await _safe_delete(msg.bot, msg.chat.id, msg.message_id)

    comment_raw = (msg.text or "").strip()
    await state.update_data(comment=("без комментария" if comment_raw == "-" else comment_raw))

    data = await state.get_data()
    user_id: int = data["target_user_id"]
    order_id: int = data["target_order_id"]

    text_preview = await _render_admin_template(data)

    await AdminP2PStates.confirm.set()
    preview = await msg.answer(
        text_preview,
        parse_mode="Markdown",
        reply_markup=_make_confirm_kb(order_id, user_id),
    )
    await state.update_data(preview_message_id=preview.message_id, last_prompt_id=None)


# -----------------------------------------------------------------------------
# Раздел: Хэндлеры — отправка пользователю и пост-обработка
# -----------------------------------------------------------------------------
async def admin_p2p_send(call: types.CallbackQuery, state: FSMContext) -> None:
    """Отправляет реквизиты пользователю, обновляет заказ и интерфейс оператора."""
    await call.answer()
    data = await state.get_data()

    user_id: int = data["target_user_id"]
    order_id: int = data["target_order_id"]

    rec = await get_order_by_id(order_id)
    payment = math.ceil(float(rec["total_rub"])) if rec and rec.get("total_rub") is not None else 0

    bank = (data.get("bank_name") or "").strip()
    chosen = (data.get("account") or "").strip()
    comment = (data.get("comment") or "без комментария").strip()

    # Сохраняем уже выбранный ранее способ оплаты заявки.
    # Для web-заявок он должен прийти из main.py/db.p2p.py как 'sbp' или 'card'.
    # Для старых/обычных сценариев оставляем мягкий fallback в 'p2p'.
    existing_payment_method = str((rec or {}).get("payment_method") or "").strip().lower()
    if existing_payment_method in {"sbp", "card", "akkula", "paycore"}:
        final_payment_method = existing_payment_method
    else:
        final_payment_method = "p2p"

    # 1) Удаляем pending_buy сообщение у пользователя.
    if user_id in pending_buy_messages:
        chat_id, msg_id = pending_buy_messages.pop(user_id)
        await _safe_delete(call.bot, chat_id, msg_id)

    # 2) Отправляем пользователю шаблон.
    text_user = INSTRUCTION_TEXT_TEMPLATE_USER.format(
        card=format_requisite_for_user(chosen),
        bank=html_escape(bank),
        comment=html_escape(comment),
        amount=payment,
    )
    ikb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Оплатил", callback_data="paid"),
        InlineKeyboardButton("🚫 Отменить", callback_data="cancel_pay"),
    )
    try:
        await bot_send(call.bot, user_id, text_user, parse_mode="HTML", reply_markup=ikb)
    except (BotBlocked, CantInitiateConversation):
        await call.message.answer("Не удалось отправить сообщение пользователю.")
        return

    # 3) Обновляем p2p_orders, но НЕ затираем выбранный ранее способ оплаты web-заявки.
    try:
        db_conn = await get_db()
        await db_conn.execute(
            """
            UPDATE p2p_orders
               SET bank_card = ?, bank_name = ?, payment_method = ?
             WHERE order_id  = ?
            """,
            (chosen, bank, final_payment_method, order_id),
        )
        await db_conn.commit()
    except Exception:
        pass

    # 4) Чистим у админа превью и кнопку.
    preview_id = data.get("preview_message_id")
    try:
        if preview_id:
            await _safe_delete(call.bot, call.message.chat.id, preview_id)
        if not preview_id or call.message.message_id != preview_id:
            await _safe_delete(call.bot, call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    # 5) Обновляем карточку заявки у оператора.
    await update_operator_order_card_with_requisites(
        call.bot,
        operator_id=call.from_user.id,
        user_id=user_id,
        order_id=order_id,
        requisites=chosen,
        bank_name=bank,
        amount=payment,
        comment=comment,
    )

    # 6) Сообщение админу об успехе.
    try:
        method_label = "СБП" if final_payment_method == "sbp" else "номер карты" if final_payment_method == "card" else "реквизиты"
        await call.bot.send_message(
            call.from_user.id,
            f"✅ {method_label.capitalize()} отправлены пользователю по заявке #{order_id}.",
        )
    except Exception:
        pass

    await state.finish()


# -----------------------------------------------------------------------------
# Раздел: Регистрация хэндлеров
# -----------------------------------------------------------------------------
def register_admin_p2p_handlers(dp: Dispatcher) -> None:
    """Регистрирует хэндлеры админского сценария P2P в диспетчере."""
    dp.register_callback_query_handler(
        admin_p2p_start,
        lambda c: c.data.startswith("admin_p2p_start:"),
        state="*",
    )
    dp.register_callback_query_handler(
        admin_p2p_cancel,
        lambda c: c.data.startswith("admin_p2p_cancel:"),
        state="*",
    )
    dp.register_callback_query_handler(
        admin_p2p_edit,
        lambda c: c.data.startswith("admin_p2p_edit:"),
        state="*",
    )
    dp.register_callback_query_handler(
        admin_p2p_send,
        lambda c: c.data.startswith("admin_p2p_send:"),
        state="*",
    )

    dp.register_message_handler(
        on_account,
        state=AdminP2PStates.waiting_account,
        content_types=types.ContentType.TEXT,
    )
    dp.register_message_handler(
        on_bank_name,
        state=AdminP2PStates.waiting_bank_name,
        content_types=types.ContentType.TEXT,
    )
    dp.register_message_handler(
        on_comment,
        state=AdminP2PStates.waiting_comment,
        content_types=types.ContentType.TEXT,
    )
