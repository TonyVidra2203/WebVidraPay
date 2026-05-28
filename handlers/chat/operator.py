# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from db.users import get_all_users, get_user

from aiogram import Bot, Dispatcher, types
from aiogram.types import ContentType, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.exceptions import MessageNotModified

from services.mastercard_cards import (
    format_mastercard_card_button_title,
    get_available_mastercard_cards_for_amount,
    get_mastercard_card_for_issue,
)
from db.connection import get_db
from db.cards import get_all_cards
from db.p2p import assign_operator_to_order, delete_order, get_order_by_id, get_pending_order
from handlers.chat.instruction import INSTRUCTION_TEXT_TEMPLATE, INSTRUCTION_TEXT_TEMPLATE_USER
from handlers.chat.templates import DEFAULT_OP_KB
from handlers.chat.utils import bot_send, safe_delete, safe_edit
from handlers.common import (
    active_chats,
    chat_histories,
    pending_buy_messages,
    pending_operator_messages,
    send_welcome,
)
from utils.helpers import clear_history, get_usd_rub


# -----------------------------------------------------------------------------
# Раздел: Глобальные константы и состояния
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)

MAX_LINES: int = 14

# sms_threads: order_id -> { user_msg_id: int, op_msg_id: int, history: list }
sms_threads: Dict[int, Dict[str, Any]] = {}

# Ожидания ввода от оператора/пользователя
pending_operator_texts: Dict[int, Tuple[int, Optional[int], int]] = {}
pending_reply_to_user: Dict[int, Tuple[int, Optional[int], int]] = {}
pending_reply_to_operator: Dict[int, Tuple[int, Optional[int], int]] = {}

# Карточки заявок у операторов: (operator_id, order_id) -> message_id карточки
operator_order_msgs: Dict[Tuple[int, int], int] = {}


# -----------------------------------------------------------------------------
# Раздел: Вспомогательные функции
# -----------------------------------------------------------------------------
def _h(text: Optional[str]) -> str:
    """Экранирует спецсимволы для HTML."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _guess_asset_from_wallet(raw_wallet: Optional[str], rec: Optional[Dict[str, Any]] = None) -> str:
    """
    Определяет актив для подписи в карточке оператора.

    Приоритет:
    1) comment заявки вида: "по P2P (XMR)"
    2) эвристика по адресу кошелька
    3) BTC как fallback
    """
    # 1) Пытаемся взять asset из комментария заявки
    try:
        comment = str((rec or {}).get("comment") or "").strip().upper()
    except Exception:
        comment = ""

    if "(XMR)" in comment:
        return "XMR"
    if "(LTC)" in comment:
        return "LTC"
    if "(USDT)" in comment:
        return "USDT(TRC20)"
    if "(BTC)" in comment:
        return "BTC"

    # 2) Fallback — по кошельку
    w = (raw_wallet or "").strip()
    if not w:
        return "BTC"

    low = w.lower()

    # USDT TRC20
    if w.startswith("T") and len(w) == 34:
        return "USDT(TRC20)"

    # LTC
    if low.startswith("ltc1"):
        return "LTC"
    if w[:1] in ("L", "M") and 26 <= len(w) <= 62:
        return "LTC"

    # XMR
    if w[:1] == "4" and len(w) in (95, 106):
        return "XMR"
    if w[:1] == "8" and len(w) == 95:
        return "XMR"

    # BTC
    if low.startswith("bc1"):
        return "BTC"
    if w[:1] in ("1", "3") and 26 <= len(w) <= 62:
        return "BTC"

    return "BTC"


def _format_asset_amount_for_operator(asset: str, amount: float) -> str:
    """
    Показывает оператору фактическое количество монет без ложного округления.
    Значение должно совпадать с тем, что сохранено в заявке и уходит в обменник.
    """
    asset_u = str(asset or "").upper()

    if asset_u.startswith("USDT"):
        return str(int(round(float(amount))))

    precision_map = {
        "BTC": 8,
        "LTC": 8,
        "XMR": 8,
    }
    precision = precision_map.get(asset_u, 8)

    formatted = f"{float(amount):.{precision}f}".rstrip("0").rstrip(".")
    return formatted or "0"

def _now() -> str:
    """Текущее время в формате HH:MM."""
    return datetime.now().strftime("%H:%M")


def _is_guest_user_id(user_id: Optional[int]) -> bool:
    try:
        return int(user_id) < 0
    except Exception:
        return False


def _is_guest_order(rec: Optional[Dict[str, Any]], user_id: Optional[int]) -> bool:
    """
    Историческое имя функции сохранено, но по факту она определяет WEB-заявку:
    - WEB-GUEST (гостевая web-сессия),
    - WEB (авторизованный web-пользователь),
    - а также любые записи с отрицательным user_id.
    """
    if _is_guest_user_id(user_id):
        return True

    comment = str((rec or {}).get("comment") or "").strip().upper()
    return comment.startswith("WEB")

async def _build_order_user_mention(bot: Bot, user_id: int, rec: Optional[Dict[str, Any]] = None) -> str:
    if _is_guest_order(rec, user_id):
        return "WEB-гость"

    try:
        chat = await bot.get_chat(user_id)
        if getattr(chat, "username", None):
            return f"@{chat.username}"

        full_name = (getattr(chat, "full_name", None) or "").strip()
        return f'<a href="tg://user?id={user_id}">{_h(full_name or str(user_id))}</a>'
    except Exception:
        return f'<a href="tg://user?id={user_id}">{_h(str(user_id))}</a>'


def _kb_op_guest_order_actions(
    user_id: int,
    order_id: int,
    *,
    show_cards: bool = True,
    hide_cancel: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()

    if show_cards:
        kb.row(
            InlineKeyboardButton(
                "💳 Карты",
                callback_data=f"operator_cards:{user_id}:{order_id}",
            ),
        )

    if not hide_cancel:
        kb.row(
            InlineKeyboardButton(
                "❌ Отменить",
                callback_data=f"operator_reject:{user_id}:{order_id}",
            )
        )

    return kb


def _render_sms_card(order_id: int) -> str:
    """
    Формирует текст карточки SMS-чата для указанной заявки.

    Формат (пример):

    🧾 Заявка №123
    SMS-чат
    ─────

    🙋 Tony Hopkins:
    — Привет

    👤 Оператор:
    — Привет!
    — Как дела?

    🙋 Tony Hopkins:
    — Ничего, нормально…
    — У тебя как?
    """
    data = sms_threads.get(order_id) or {}
    history: List[Any] = data.get("history", [])

    lines: List[str] = [
        f"🧾 Заявка №{order_id}",
        "",
        "SMS-чат",
        "─────",
    ]

    if not history:
        lines.append("Пока сообщений нет.")
        return "\n".join(lines).rstrip()

    recent = history[-MAX_LINES:]

    # groups: (role, display_name, [texts])
    groups: List[Tuple[str, str, List[str]]] = []

    for item in recent:
        # Поддерживаем и новый формат (role, text, ts, display),
        # и старый (role, text)
        if isinstance(item, (tuple, list)) and len(item) >= 4:
            role, text, _, display = item
        else:
            role, text = item[0], item[1]
            display = "Пользователь" if role != "op" else "Оператор"

        text = text or ""
        display_name = "Оператор" if role == "op" else (display or "Пользователь")

        if groups and groups[-1][0] == role and groups[-1][1] == display_name:
            # Продолжаем текущий блок того же отправителя
            groups[-1][2].append(text)
        else:
            # Новый блок отправителя
            groups.append((role, display_name, [text]))

    # Рендер групп в стиле поддержки
    for role, display_name, texts in groups:
        header = f"👤 {display_name}:" if role == "op" else f"🙋 {display_name}:"

        lines.append("")  # пустая строка перед блоком
        lines.append(header)

        for txt in texts:
            msg_lines = (txt or "").splitlines() or [""]
            first = True
            for line in msg_lines:
                if first:
                    # Первая строка сообщения — с длинным тире
                    lines.append(f"— {line or ' '}")
                    first = False
                else:
                    # Последующие строки — с отступом
                    lines.append(f"   {line or ' '}")

    return "\n".join(lines).rstrip()


# -----------------------------------------------------------------------------
# Раздел: Клавиатуры
# -----------------------------------------------------------------------------
def _kb_user_chat(op_id: int, order_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для пользователя в SMS-чате."""
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(
            "💬 Ответить",
            callback_data=f"reply_to_operator:{op_id}:{order_id}",
        )
    )
    return kb


def _kb_op_chat(user_id: int, order_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для оператора в SMS-чате."""
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton(
            "💬 Ответить",
            callback_data=f"reply_to_user:{user_id}:{order_id}",
        ),
        InlineKeyboardButton(
            "📄 Заявка",
            callback_data=f"operator_open_order:{user_id}:{order_id}",
        ),
    )
    kb.row(
        InlineKeyboardButton(
            "🚪 Закрыть чат",
            callback_data=f"close_sms_chat:{user_id}:{order_id}",
        )
    )
    return kb


def _kb_op_order_actions(
    user_id: int,
    order_id: int,
    *,
    hide_cancel: bool = False,
    hide_payment_actions: bool = False,
) -> InlineKeyboardMarkup:
    """
    Клавиатура действий по заявке для оператора.

    :param user_id: Идентификатор пользователя.
    :param order_id: Идентификатор заявки.
    :param hide_cancel: Скрыть кнопку «❌ Отменить».
    :param hide_payment_actions: Скрыть «Карты» и «P2P».
    """
    kb = InlineKeyboardMarkup()
    if not hide_payment_actions:
        kb.row(
            InlineKeyboardButton(
                "💳 Карты",
                callback_data=f"operator_cards:{user_id}:{order_id}",
            ),
            InlineKeyboardButton(
                "🔁 P2P",
                callback_data=f"operator_p2p_warn:{user_id}:{order_id}",
            ),

            InlineKeyboardButton(
                "✉️ SMS",
                callback_data=f"operator_message:{user_id}:{order_id}",
            ),
        )
    else:
        kb.row(
            InlineKeyboardButton(
                "✉️ SMS",
                callback_data=f"operator_message:{user_id}:{order_id}",
            ),
        )

    if not hide_cancel:
        kb.row(
            InlineKeyboardButton(
                "❌ Отменить",
                callback_data=f"operator_reject:{user_id}:{order_id}",
            )
        )
    return kb


def _kb_op_taken_label(taker_name: str) -> InlineKeyboardMarkup:
    """Лейбл вместо кнопки «Принять» у остальных операторов."""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(f"✅ Взял: {taker_name}", callback_data="noop"))
    return kb


# -----------------------------------------------------------------------------
# Раздел: P2P — шлюз согласия пользователя перед поиском реквизитов
# -----------------------------------------------------------------------------

# order_id -> контекст шлюза (какие сообщения чистить и кто участники)
# {
#   "op_id": int,
#   "user_id": int,
#   "op_wait_msg_id": Optional[int],     # "ждём ответ..." у админа
#   "user_warn_msg_id": Optional[int],   # предупреждение у пользователя
# }
pending_p2p_consent: Dict[int, Dict[str, Optional[int]]] = {}


def _kb_op_p2p_warn_confirm(user_id: int, order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Да", callback_data=f"operator_p2p_warn_yes:{user_id}:{order_id}"),
        InlineKeyboardButton("❌ Нет", callback_data=f"operator_p2p_warn_no:{user_id}:{order_id}"),
    )
    return kb


def _kb_user_p2p_consent(op_id: int, order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Согласен", callback_data=f"user_p2p_agree:{op_id}:{order_id}"),
        InlineKeyboardButton("🚫 Отменить", callback_data=f"user_cancel_order:{order_id}"),
    )
    return kb


async def _start_admin_p2p_flow(bot: Bot, *, op_id: int, user_id: int, order_id: int) -> None:
    """
    Запускает существующий FSM-сценарий admin_p2p_start (ввод СБП/карты и т.д.)
    БЕЗ импорта handlers/admin_p2p.py (чтобы не словить циклический импорт).
    """
    try:
        from aiogram import Dispatcher  # локально, чтобы не ломать импорты
        dp = Dispatcher.get_current()
        state = dp.current_state(chat=op_id, user=op_id)  # FSMContext

        # Сброс предыдущего состояния, как в admin_p2p_start
        await state.finish()
        await state.update_data(
            target_user_id=user_id,
            target_order_id=order_id,
            last_prompt_id=None,
        )

        # Важно: строка состояния должна совпасть с AdminP2PStates.waiting_account
        await state.set_state("AdminP2PStates:waiting_account")

        prompt = await bot.send_message(op_id, "💳 Введите <b>номер СБП/номер карты</b>:", parse_mode="HTML")
        await state.update_data(last_prompt_id=prompt.message_id)
    except Exception:
        logger.exception("Failed to start admin p2p flow automatically")


async def operator_p2p_warn_start(callback: types.CallbackQuery) -> None:
    """Оператор нажал P2P под карточкой заявки — спрашиваем, отправлять ли предупреждение."""
    await callback.answer()
    op_id = callback.from_user.id

    parts = (callback.data or "").split(":")
    try:
        user_id = int(parts[1])
        order_id = int(parts[2])
    except Exception:
        await bot_send(callback.bot, op_id, "⚠️ Неверные данные кнопки.")
        return

    text = (
        f"🔁 P2P по заявке #{order_id}\n\n"
        "Предупредить пользователя о том, что реквизиты будут выданы через P2P "
        "и потребуется чек в PDF?"
    )
    await bot_send(
        callback.bot,
        op_id,
        text,
        reply_markup=_kb_op_p2p_warn_confirm(user_id, order_id),
    )


async def operator_p2p_warn_no(callback: types.CallbackQuery) -> None:
    """
    Оператор нажал 'Нет' — НЕ предупреждаем пользователя,
    а сразу запускаем стандартный процесс ввода реквизитов (как раньше по P2P).
    """
    await callback.answer()
    op_id = callback.from_user.id

    parts = (callback.data or "").split(":")
    try:
        user_id = int(parts[1])
        order_id = int(parts[2])
    except Exception:
        try:
            await safe_delete(callback.bot, op_id, callback.message.message_id)
        except Exception:
            pass
        return

    # Убираем сообщение-вопрос у админа
    try:
        await safe_delete(callback.bot, op_id, callback.message.message_id)
    except Exception:
        try:
            await safe_edit(callback.message, reply_markup=None)
        except Exception:
            pass

    # На всякий случай очищаем возможный контекст ожидания по этой заявке
    try:
        pending_p2p_consent.pop(order_id, None)
    except Exception:
        pass

    # Запускаем старую логику ввода реквизитов
    await _start_admin_p2p_flow(callback.bot, op_id=op_id, user_id=user_id, order_id=order_id)


async def operator_p2p_warn_yes(callback: types.CallbackQuery) -> None:
    """Оператор подтвердил — отправляем пользователю предупреждение и ставим 'ожидание' у админа."""
    await callback.answer()
    op_id = callback.from_user.id

    parts = (callback.data or "").split(":")
    try:
        user_id = int(parts[1])
        order_id = int(parts[2])
    except Exception:
        await bot_send(callback.bot, op_id, "⚠️ Неверные данные кнопки.")
        return

    warn_text = (
        "⚠️ <b>Внимание!</b>\n\n"
    "Реквизиты подбираются через <b>P2P-маркет</b> — ожидание до <b>10 минут</b> ⏳\n"
    "После оплаты потребуется <b>чек в формате PDF</b> 📄\n\n"
    "❗️После получения реквизитов вы <b>ОБЯЗАНЫ</b> произвести перевод\n"
    "❗️Отмена после получения = <b>БАН (24 часа)</b>\n"
    "❗️Не успели перевести и скинуть чек в течении 15 минут = <b>ОТМЕНА</b>\n\n"
    "Если есть сомнения — <b>лучше отмените заявку заранее</b>"
    )

    user_msg = await bot_send(
        callback.bot,
        user_id,
        warn_text,
        reply_markup=_kb_user_p2p_consent(op_id, order_id),
    )

    # Убираем сообщение-вопрос у админа
    try:
        await safe_delete(callback.bot, op_id, callback.message.message_id)
    except Exception:
        pass

    # Служебный статус у админа, который должен исчезнуть после решения пользователя
    wait_msg = await bot_send(
        callback.bot,
        op_id,
        f"⏳ Предупреждение отправлено пользователю по заявке #{order_id}. Ожидаем ответ…",
    )

    pending_p2p_consent[order_id] = {
        "op_id": op_id,
        "user_id": user_id,
        "op_wait_msg_id": wait_msg.message_id,
        "user_warn_msg_id": user_msg.message_id,
    }


async def user_p2p_agree(callback: types.CallbackQuery) -> None:
    """
    Пользователь согласился на P2P:
    - удаляем предупреждение у пользователя
    - у админа удаляем "ожидание"
    - автоматически запускаем старую P2P-логику (FSM admin_p2p_start)
    - НЕ отправляем пользователю "Идет процесс..." (по требованию)
    """
    await callback.answer()
    user_id = callback.from_user.id

    parts = (callback.data or "").split(":")
    try:
        op_id = int(parts[1])
        order_id = int(parts[2])
    except Exception:
        try:
            await safe_edit(callback.message, reply_markup=None)
        except Exception:
            pass
        return

    # Удаляем предупреждение у пользователя
    try:
        await safe_delete(callback.bot, user_id, callback.message.message_id)
    except Exception:
        try:
            await safe_edit(callback.message, reply_markup=None)
        except Exception:
            pass

    # Удаляем "ожидание" у админа и чистим контекст
    ctx = pending_p2p_consent.pop(order_id, None) or {}
    op_wait_msg_id = ctx.get("op_wait_msg_id")
    if op_wait_msg_id:
        try:
            await safe_delete(callback.bot, op_id, int(op_wait_msg_id))
        except Exception:
            pass

    # Запускаем прежнюю логику ввода реквизитов у админа
    await _start_admin_p2p_flow(callback.bot, op_id=op_id, user_id=user_id, order_id=order_id)


# -----------------------------------------------------------------------------
# Раздел: SMS-чат (создание, обновление, очистка)
# -----------------------------------------------------------------------------
async def _ensure_thread(bot: Bot, user_id: int, op_id: int, order_id: int) -> Dict[str, Any]:
    """Гарантирует наличие карточек SMS-чата у пользователя и оператора."""
    data = sms_threads.get(order_id)
    if data:
        return data

    initial_text = _render_sms_card(order_id)
    user_msg = await bot_send(
        bot,
        user_id,
        initial_text,
        parse_mode="HTML",
        reply_markup=_kb_user_chat(op_id, order_id),
    )
    op_msg = await bot_send(
        bot,
        op_id,
        initial_text,
        parse_mode="HTML",
        reply_markup=_kb_op_chat(user_id, order_id),
    )
    sms_threads[order_id] = {
        "user_msg_id": user_msg.message_id,
        "op_msg_id": op_msg.message_id,
        "history": [],
    }
    return sms_threads[order_id]


async def _append_and_update(
    bot: Bot,
    user_id: int,
    op_id: int,
    order_id: int,
    *,
    role: str,
    text: str,
) -> None:
    """Добавляет сообщение в историю SMS-чата и перерисовывает карточки."""
    data = await _ensure_thread(bot, user_id, op_id, order_id)

    display: Optional[str] = "Оператор" if role == "op" else None
    if role != "op":
        try:
            chat = await bot.get_chat(user_id)
            full = (getattr(chat, "full_name", None) or "").strip()
            if full:
                display = _h(full)
            elif getattr(chat, "username", None):
                display = _h(chat.username)
            else:
                display = str(user_id)
        except Exception:
            display = str(user_id)

    data["history"].append((role, text, _now(), display))
    card = _render_sms_card(order_id)

    try:
        await safe_delete(bot, user_id, data.get("user_msg_id"))
    except Exception:
        pass
    try:
        await safe_delete(bot, op_id, data.get("op_msg_id"))
    except Exception:
        pass

    try:
        user_new = await bot_send(
            bot,
            user_id,
            card,
            parse_mode="HTML",
            reply_markup=_kb_user_chat(op_id, order_id),
        )
        data["user_msg_id"] = user_new.message_id
    except Exception:
        pass

    try:
        op_new = await bot_send(
            bot,
            op_id,
            card,
            parse_mode="HTML",
            reply_markup=_kb_op_chat(user_id, order_id),
        )
        data["op_msg_id"] = op_new.message_id
    except Exception:
        pass


async def _cleanup_sms_thread(bot: Bot, user_id: int, op_id: int, order_id: int) -> None:
    """Удаляет карточки SMS-чата и очищает состояние для заявки."""
    data = sms_threads.pop(order_id, None)
    if not data:
        return

    try:
        await safe_delete(bot, user_id, data.get("user_msg_id"))
    except Exception:
        pass
    try:
        await safe_delete(bot, op_id, data.get("op_msg_id"))
    except Exception:
        pass


async def close_sms_chat(callback: types.CallbackQuery) -> None:
    """Закрывает SMS-чат без уведомлений обеим сторонам."""
    await callback.answer()
    parts = (callback.data or "").split(":")
    try:
        user_id = int(parts[1])
        order_id = int(parts[2])
    except Exception:
        try:
            await safe_edit(callback.message, reply_markup=None)
        except Exception:
            pass
        return

    op_id = callback.from_user.id
    await _cleanup_sms_thread(callback.bot, user_id, op_id, order_id)

    try:
        await safe_delete(callback.bot, op_id, callback.message.message_id)
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Раздел: Хендлеры — принятие/отклонение/отмена заявки
# -----------------------------------------------------------------------------
async def operator_accept(callback: types.CallbackQuery) -> None:
    """
    Принятие заявки оператором / Mastercard.

    После принятия заявки реквизиты НЕ выдаются автоматически.
    Mastercard должен нажать «💳 Карты», выбрать карту из сайта,
    и только после этого реквизиты будут выданы пользователю.
    """
    await callback.answer()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except MessageNotModified:
        pass
    except Exception:
        pass

    op_id = callback.from_user.id
    parts = (callback.data or "").split(":", 2)
    user_id: Optional[int] = None
    order_id: Optional[int] = None

    if len(parts) >= 3:
        try:
            user_id = int(parts[1])
            order_id = int(parts[2])
        except ValueError:
            user_id = None
            order_id = None

    if order_id is None:
        try:
            user_id = int((callback.data or "").split(":", 1)[1])
        except Exception:
            await callback.message.answer("⚠️ Неверные данные кнопки.")
            return
        rec = await get_pending_order(user_id)
    else:
        rec = await get_order_by_id(order_id)

    if not rec:
        await callback.message.answer("⚠️ Заявка не найдена.")
        return

    operator = await get_user(op_id)
    role = str((operator or {}).get("role") or "").strip()
    if role not in ("Operator", "Admin", "MasterCard", "mastercard"):
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    order_db_id = int(rec.get("order_id"))
    user_id = int(rec.get("user_id", user_id))
    is_guest = _is_guest_order(rec, user_id)

    await assign_operator_to_order(order_db_id, op_id)

    taker_name = "оператор"
    try:
        chat = await callback.bot.get_chat(op_id)
        if getattr(chat, "username", None):
            taker_name = f"@{chat.username}"
        else:
            full = (getattr(chat, "full_name", None) or "").strip()
            taker_name = full or str(op_id)
    except Exception:
        pass

    mem_entries = list(pending_operator_messages.get(user_id, []))
    seen_pairs = set()

    for cid, mid in mem_entries:
        try:
            cid_i = int(cid)
            mid_i = int(mid)
        except Exception:
            continue

        if cid_i == op_id:
            continue

        seen_pairs.add((cid_i, mid_i))

        try:
            await callback.bot.edit_message_reply_markup(
                chat_id=cid_i,
                message_id=mid_i,
                reply_markup=_kb_op_taken_label(taker_name),
            )
        except MessageNotModified:
            pass
        except Exception:
            pass

    try:
        from db.p2p import get_operator_notifications_by_order
    except Exception:
        get_operator_notifications_by_order = None

    if get_operator_notifications_by_order is not None:
        try:
            db_entries = await get_operator_notifications_by_order(order_db_id)
        except Exception:
            db_entries = []

        for item in db_entries:
            try:
                chat_id = int(item.get("chat_id"))
                message_id = int(item.get("message_id"))
            except Exception:
                continue

            if chat_id == op_id:
                continue

            if (chat_id, message_id) in seen_pairs:
                continue

            try:
                await callback.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=_kb_op_taken_label(taker_name),
                )
            except MessageNotModified:
                pass
            except Exception:
                pass

    if not is_guest and user_id in pending_buy_messages:
        chat_id, msg_id = pending_buy_messages[user_id]
        try:
            await callback.bot.edit_message_text(
                "✅ Оператор принял вашу заявку! Ожидайте реквизиты...",
                chat_id,
                msg_id,
                reply_markup=None,
            )
        except MessageNotModified:
            pass
        except Exception:
            logger.exception(
                "Не удалось обновить пользовательское уведомление по заявке #%s",
                order_db_id,
            )

    btc_amt = float(rec.get("btc_amount", 0))

    raw_wallet = rec.get("wallet", "")
    wallet = _h(raw_wallet)
    asset = _guess_asset_from_wallet(str(raw_wallet), rec)

    mention = await _build_order_user_mention(callback.bot, user_id, rec)
    base_rate = await get_usd_rub()

    raw_sum = (
        rec.get("rub_amount")
        or rec.get("amount_rub")
        or rec.get("amount")
        or rec.get("total_rub", 0)
    )
    try:
        sum_rub = math.ceil(float(raw_sum))
    except Exception:
        sum_rub = math.ceil(float(rec.get("total_rub", 0)))

    payment = math.ceil(float(rec.get("total_rub", sum_rub)))

    now = datetime.now()
    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M")

    text_op = (
        f"📥 Заявка #{order_db_id}\n\n"
        f"От: {mention}\n"
        f"Сумма: {sum_rub} ₽\n"
        f"К выдаче: {_format_asset_amount_for_operator(asset, btc_amt)} {asset}\n"
        f"Кошелек {asset}: <code>{wallet}</code>\n\n"
        f"Дата: {date_str}\n"
        f"Время: {time_str}\n"
        f"Курс: {base_rate:.2f} ₽\n\n"
        f"💰 <b>К оплате: {payment} ₽</b>"
    )

    kb_op = (
        _kb_op_guest_order_actions(user_id, order_db_id)
        if is_guest
        else _kb_op_order_actions(user_id, order_db_id)
    )

    try:
        await callback.message.edit_text(text_op, parse_mode="HTML", reply_markup=kb_op)
        operator_order_msgs[(op_id, order_db_id)] = callback.message.message_id
    except MessageNotModified:
        pass
    except Exception:
        new_msg = await bot_send(
            callback.bot,
            op_id,
            text_op,
            parse_mode="HTML",
            reply_markup=kb_op,
        )
        operator_order_msgs[(op_id, order_db_id)] = new_msg.message_id


async def user_cancel_order(callback: types.CallbackQuery) -> None:
    """
    Отмена заявки пользователем.

    - Удаляет текущее сообщение (это может быть реквизиты или предупреждение P2P).
    - Удаляет "✅ Оператор принял вашу заявку! Ожидайте реквизиты..." (pending_buy_messages), чтобы не засорять чат.
    - Помечает заявку как canceled в БД (если не completed).
    - Массово удаляет карточки/уведомления по заявке у всех админов/операторов:
      * из in-memory pending_operator_messages,
      * из in-memory operator_order_msgs,
      * из БД-реестра операторских уведомлений.
    - Обновляет/уведомляет операторов/админов через существующие функции.
    - Чистит SMS-чат.
    - Возвращает пользователя в главное меню.

    Дополнительно:
    - Если отмена произошла из P2P-шлюза согласия — удаляет у админа служебное сообщение ожидания.
    """
    await callback.answer()
    user_id = callback.from_user.id

    # 1) Удаляем текущее сообщение (реквизиты/предупреждение/прочее)
    try:
        await safe_delete(callback.bot, user_id, callback.message.message_id)
    except Exception:
        try:
            await safe_edit(callback.message, reply_markup=None)
        except Exception:
            pass

    # 2) Пробуем извлечь order_id из callback_data: user_cancel_order:<order_id> или cancel_pay
    order_id: Optional[int] = None
    try:
        parts = (callback.data or "").split(":")
        if len(parts) >= 2 and parts[1].strip():
            order_id = int(parts[1])
    except Exception:
        order_id = None

    # 3) Получаем заявку из БД (по order_id) или из pending по user_id
    rec: Optional[Dict[str, Any]] = None
    if order_id is not None:
        try:
            rec = await get_order_by_id(order_id)
        except Exception:
            rec = None

    if rec is None:
        rec = await get_pending_order(user_id)
        if rec and rec.get("order_id"):
            try:
                order_id = int(rec["order_id"])
            except Exception:
                pass

    if not rec or order_id is None:
        try:
            await bot_send(callback.bot, user_id, "⚠️ Активная заявка не найдена.")
            await send_welcome(callback.bot, user_id)
        except Exception:
            pass
        return

    # 4) Удаляем "✅ Оператор принял вашу заявку! Ожидайте реквизиты...", если оно ещё висит
    if user_id in pending_buy_messages:
        try:
            chat_id, msg_id = pending_buy_messages.pop(user_id)
            await safe_delete(callback.bot, chat_id, msg_id)
        except Exception:
            pass

    # 5) Определяем operator_id из заявки (если есть)
    try:
        operator_id: Optional[int] = int(rec.get("operator_id")) if rec.get("operator_id") else None
    except Exception:
        operator_id = None

    # 6) Если отмена пришла из P2P-шлюза — чистим у админа служебное сообщение ожидания
    try:
        ctx = pending_p2p_consent.pop(order_id, None)
        if ctx:
            op_wait_msg_id = ctx.get("op_wait_msg_id")
            ctx_op_id = ctx.get("op_id")
            if op_wait_msg_id:
                # основной вариант: operator_id из заявки
                if operator_id:
                    try:
                        await safe_delete(callback.bot, operator_id, int(op_wait_msg_id))
                    except Exception:
                        pass
                # запасной вариант: op_id из контекста (если отличается/не записан в заявке)
                if ctx_op_id and (not operator_id or int(ctx_op_id) != int(operator_id)):
                    try:
                        await safe_delete(callback.bot, int(ctx_op_id), int(op_wait_msg_id))
                    except Exception:
                        pass
    except Exception:
        pass

    # 7) До очистки реестров собираем все известные сообщения операторов/админов по заявке,
    #    чтобы реально удалить их из Telegram у всех.
    notifications_to_delete: List[Tuple[int, int]] = []
    seen_notifications = set()

    # 7.1) Старое in-memory хранилище массовых уведомлений
    try:
        for cid, mid in list(pending_operator_messages.get(user_id, [])):
            try:
                pair = (int(cid), int(mid))
            except Exception:
                continue
            if pair in seen_notifications:
                continue
            seen_notifications.add(pair)
            notifications_to_delete.append(pair)
    except Exception:
        pass

    # 7.2) Карточки открытых заявок у операторов
    try:
        for (op_id_key, oid_key), mid in list(operator_order_msgs.items()):
            try:
                if int(oid_key) != int(order_id):
                    continue
                pair = (int(op_id_key), int(mid))
            except Exception:
                continue
            if pair in seen_notifications:
                continue
            seen_notifications.add(pair)
            notifications_to_delete.append(pair)
    except Exception:
        pass

    # 7.3) Реестр уведомлений из БД (важно для multi-process / web+bot)
    try:
        from db.p2p import get_operator_notifications_by_order
        db_entries = await get_operator_notifications_by_order(order_id)
    except Exception:
        db_entries = []

    for item in db_entries:
        try:
            pair = (int(item.get("chat_id")), int(item.get("message_id")))
        except Exception:
            continue
        if pair in seen_notifications:
            continue
        seen_notifications.add(pair)
        notifications_to_delete.append(pair)

    # 8) Ставим статус canceled в БД
    try:
        db = await get_db()
        await db.execute(
            "UPDATE p2p_orders SET status='canceled' WHERE order_id=? AND status!='completed'",
            (order_id,),
        )
        await db.commit()
    except Exception:
        pass

    # 9) Реально удаляем карточки/уведомления у всех админов/операторов
    for chat_id, message_id in notifications_to_delete:
        try:
            await safe_delete(callback.bot, chat_id, message_id)
        except Exception:
            pass

    # 10) Чистим in-memory реестры после удаления
    try:
        pending_operator_messages.pop(user_id, None)
    except Exception:
        pass

    try:
        keys_to_remove = []
        for key in list(operator_order_msgs.keys()):
            try:
                _, oid_key = key
                if int(oid_key) == int(order_id):
                    keys_to_remove.append(key)
            except Exception:
                continue

        for key in keys_to_remove:
            operator_order_msgs.pop(key, None)
    except Exception:
        pass

    # 11) Чистим сохранённые операторские уведомления по заявке из БД
    try:
        from db.p2p import delete_operator_notifications_by_order
        await delete_operator_notifications_by_order(order_id)
    except Exception:
        pass

    # 12) Уведомления/обновления карточек у операторов/админов (как было раньше)
    try:
        from handlers.buy.p2p import _edit_operator_cards_to_canceled, _notify_admins_cancelled

        order_dict = {
            "order_id": order_id,
            "btc_amount": float(rec.get("btc_amount") or 0),
            "total_rub": int(rec.get("total_rub") or 0),
            "status": "canceled",
        }
        await _edit_operator_cards_to_canceled(callback.bot, user_id, order_dict)
        await _notify_admins_cancelled(callback.bot, order_dict, user_id)
    except Exception:
        pass

    # 13) Чистим SMS-чат
    if operator_id:
        try:
            await _cleanup_sms_thread(callback.bot, user_id, operator_id, order_id)
        except Exception:
            pass

    # 14) Возвращаем пользователя в главное меню
    try:
        await send_welcome(callback.bot, user_id)
    except Exception:
        pass



async def operator_reject(callback: types.CallbackQuery) -> None:
    """Отмена заявки оператором (p2p-стиль)."""
    await callback.answer()

    op_id = callback.from_user.id
    parts = (callback.data or "").split(":")
    try:
        user_id = int(parts[1])
    except Exception:
        await safe_edit(callback.message, reply_markup=None)
        return

    order_id: Optional[int] = None
    if len(parts) >= 3:
        try:
            order_id = int(parts[2])
        except Exception:
            order_id = None

    rec: Optional[Dict[str, Any]] = None
    if order_id is not None:
        rec = await get_order_by_id(order_id)
    if rec is None:
        rec = await get_pending_order(user_id)
        if rec and rec.get("order_id"):
            order_id = int(rec["order_id"])

    try:
        await safe_delete(callback.bot, op_id, callback.message.message_id)
    except Exception:
        pass

    if order_id is None or rec is None:
        return

    try:
        operator_order_msgs.pop((op_id, order_id), None)
    except Exception:
        pass

    db = await get_db()
    await db.execute(
        "UPDATE p2p_orders SET status='canceled' WHERE order_id=? AND status!='completed'",
        (order_id,),
    )
    await db.commit()

    if user_id in pending_buy_messages:
        chat_id, msg_id = pending_buy_messages.pop(user_id)
        try:
            await callback.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    try:
        await _cleanup_sms_thread(callback.bot, user_id, op_id, order_id)
    except Exception:
        pass

    try:
        from handlers.buy.p2p import _edit_operator_cards_to_canceled, _notify_admins_cancelled

        od = {
            "order_id": order_id,
            "btc_amount": float(rec.get("btc_amount") or 0),
            "total_rub": int(rec.get("total_rub") or 0),
            "status": "canceled",
        }
        await _edit_operator_cards_to_canceled(callback.bot, user_id, od)
        await _notify_admins_cancelled(callback.bot, od, user_id)
    except Exception:
        pass

    try:
        await bot_send(callback.bot, user_id, "🚫 Заявка отменена.")
        await send_welcome(callback.bot, user_id)
    except Exception:
        pass



def _normalize_role_name(user: Optional[Dict[str, Any]]) -> str:
    """Возвращает роль пользователя в нижнем регистре."""
    return str((user or {}).get("role") or "").strip().lower()


def _card_has_required_requisites(card: Dict[str, Any], method: str) -> bool:
    """Проверяет, есть ли у карты реквизиты под выбранный способ оплаты."""
    method_l = str(method or "").strip().lower()
    if method_l == "sbp":
        return bool(str(card.get("sbp_phone") or "").strip())
    return bool(str(card.get("card_number") or "").strip())


def _mask_requisites(value: Any) -> str:
    """Коротко маскирует карту/телефон для кнопки выбора."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return "—"
    if len(digits) <= 4:
        return digits
    if len(digits) <= 11:
        return f"••{digits[-4:]}"
    return f"{digits[:4]}••{digits[-4:]}"


def _format_card_choice_button(card: Dict[str, Any], *, method: str, is_admin: bool = False) -> str:
    """
    Компактный и понятный заголовок кнопки выбора карты.

    Telegram ограничивает длину текста кнопки, поэтому показываем:
    банк → нужные реквизиты → id карты → владелец для админа.
    """
    bank = str(card.get("bank_name") or "Банк").strip()
    card_id = int(card.get("card_id") or 0)
    owner_id = int(card.get("owner_id") or 0)

    method_l = str(method or "").strip().lower()
    if method_l == "sbp":
        req = _mask_requisites(card.get("sbp_phone"))
        prefix = "⚡ СБП"
    else:
        req = _mask_requisites(card.get("card_number"))
        prefix = "💳 Карта"

    base = f"{prefix} • {bank} • {req} • #{card_id}"
    if is_admin and owner_id > 0:
        base += f" • MC {owner_id}"

    return base[:64]


def _build_cards_choice_text(
    *,
    order_id: int,
    amount_rub: int,
    method: str,
    is_admin: bool,
    cards_count: int,
) -> str:
    method_label = "СБП" if str(method or "").lower() == "sbp" else "карта"
    if is_admin:
        mode = (
            "👑 <b>Режим админа:</b> показаны карты всех Mastercard "
            "без фильтров суммы, лимитов и пауз."
        )
    else:
        mode = (
            "💳 <b>Режим Mastercard:</b> показаны только ваши доступные карты "
            "с учётом суммы, лимитов и пауз."
        )

    return (
        f"💳 <b>Выбор карты для заявки #{order_id}</b>\n\n"
        f"Способ: <b>{_h(method_label)}</b>\n"
        f"Сумма: <b>{int(amount_rub)} ₽</b>\n"
        f"Найдено карт: <b>{int(cards_count)}</b>\n\n"
        f"{mode}\n\n"
        "Нажмите на подходящую карту ниже."
    )


# -----------------------------------------------------------------------------
# Раздел: Хендлеры — работа с картами
# -----------------------------------------------------------------------------
async def operator_cards(callback: types.CallbackQuery) -> None:
    """
    Выбор Mastercard-карты для передачи реквизитов пользователю.

    Логика прав:
    - MasterCard видит только свои карты и только те, которые проходят фильтры суммы/лимитов/паузы;
    - Admin видит карты всех Mastercard без фильтров суммы/лимитов/паузы;
    - Operator оставлен совместимым со старым сценарием и видит доступные карты по фильтрам.
    """
    await callback.answer()

    parts = (callback.data or "").split(":")
    user_id: Optional[int] = None
    order_id: Optional[int] = None

    try:
        if len(parts) >= 3:
            user_id = int(parts[1])
            order_id = int(parts[2])
        else:
            user_id = int(parts[1])
    except Exception:
        await callback.bot.send_message(callback.from_user.id, "⚠️ Неверные данные.")
        return

    rec: Optional[Dict[str, Any]] = None
    if order_id is not None:
        try:
            rec = await get_order_by_id(order_id)
        except Exception:
            rec = None

    if rec is None and user_id is not None:
        rec = await get_pending_order(user_id)

    if not rec:
        await callback.bot.send_message(callback.from_user.id, "⚠️ Заявка не найдена.")
        return

    operator_id = int(callback.from_user.id)
    operator = await get_user(operator_id)
    role = _normalize_role_name(operator)

    if role not in {"operator", "admin", "mastercard"}:
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    try:
        amount_rub = math.ceil(float(rec.get("total_rub") or rec.get("rub_amount") or 0))
    except Exception:
        amount_rub = 0

    order_db_id = int(rec["order_id"])
    method = str(rec.get("payment_method") or "card").strip().lower()
    if method not in {"sbp", "card"}:
        method = "card"

    is_admin = role == "admin"
    is_mastercard = role == "mastercard"

    if is_admin:
        # Админ может выдать любую карту любого Mastercard без проверки лимитов,
        # min/max, дневных ограничений и пауз.
        try:
            cards = await get_all_cards()
        except Exception:
            logger.exception("Не удалось получить все карты для админа")
            cards = []

        cards = [
            card for card in cards
            if int(card.get("owner_id") or 0) > 0
            and _card_has_required_requisites(card, method)
        ]
    else:
        # Mastercard и обычный оператор работают через существующую проверку доступности.
        cards = await get_available_mastercard_cards_for_amount(float(amount_rub))

        if is_mastercard:
            cards = [
                card for card in cards
                if int(card.get("owner_id") or 0) == operator_id
            ]

        cards = [
            card for card in cards
            if _card_has_required_requisites(card, method)
        ]

    if not cards:
        if is_admin:
            reason = (
                "У Mastercard пока нет карт с нужными реквизитами под выбранный способ оплаты."
            )
        elif is_mastercard:
            reason = (
                "У вас нет доступных карт под эту заявку.\n\n"
                "Возможные причины: карта выключена, не подходит способ оплаты, "
                "достигнут лимит, не подходит min/max или действует пауза."
            )
        else:
            reason = (
                "Нет доступных Mastercard-карт под эту сумму.\n\n"
                "Возможные причины: карты выключены, достигнут лимит, "
                "не подходит min/max или действует пауза."
            )

        await callback.bot.send_message(callback.from_user.id, f"⚠️ {reason}")
        return

    kb = InlineKeyboardMarkup(row_width=1)
    for card in cards:
        card_id = int(card.get("card_id") or 0)
        if card_id <= 0:
            continue

        payload = f"operator_card_selected:{user_id}:{order_db_id}:{card_id}"

        kb.add(
            InlineKeyboardButton(
                _format_card_choice_button(card, method=method, is_admin=is_admin),
                callback_data=payload,
            )
        )

    kb.add(
        InlineKeyboardButton(
            "↩️ Назад к заявке",
            callback_data=f"operator_open_order:{user_id}:{order_db_id}",
        )
    )

    await callback.bot.send_message(
        callback.from_user.id,
        _build_cards_choice_text(
            order_id=order_db_id,
            amount_rub=amount_rub,
            method=method,
            is_admin=is_admin,
            cards_count=len(cards),
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )

async def operator_card_selected(callback: types.CallbackQuery) -> None:
    """Выдаёт пользователю реквизиты Mastercard-карты с проверкой лимитов."""
    await callback.answer()

    try:
        await callback.message.delete()
    except Exception:
        pass

    parts = (callback.data or "").split(":")
    user_id: Optional[int] = None
    order_id: Optional[int] = None
    card_id: Optional[int] = None

    try:
        if len(parts) >= 4:
            user_id = int(parts[1])
            order_id = int(parts[2])
            card_id = int(parts[3])
        else:
            user_id = int(parts[1])
            card_id = int(parts[2])
    except Exception:
        await callback.bot.send_message(callback.from_user.id, "⚠️ Неверные данные.")
        return

    rec: Optional[Dict[str, Any]] = None
    if order_id is not None:
        try:
            rec = await get_order_by_id(order_id)
        except Exception:
            rec = None

    if rec is None and user_id is not None:
        rec = await get_pending_order(user_id)

    if not rec:
        await callback.bot.send_message(callback.from_user.id, "⚠️ Заявка не найдена.")
        return

    db_order_id = int(rec["order_id"])
    operator_id = callback.from_user.id
    user_id = int(rec.get("user_id", user_id))
    is_guest = _is_guest_order(rec, user_id)

    operator = await get_user(operator_id)
    role = str((operator or {}).get("role") or "").strip()
    if role not in ("Operator", "Admin", "MasterCard", "mastercard"):
        await callback.answer("🚫 Доступ запрещён", show_alert=True)
        return

    try:
        payment = math.ceil(float(rec.get("total_rub") or rec.get("rub_amount") or 0))
    except Exception:
        payment = 0

    role_normalized = _normalize_role_name(operator)
    is_admin = role_normalized == "admin"

    method = str(rec.get("payment_method") or "card").strip().lower()
    if method not in {"sbp", "card"}:
        method = "card"

    if is_admin:
        # Админ выдаёт любую карту Mastercard без проверки лимитов/паузы/min/max.
        try:
            all_cards = await get_all_cards()
        except Exception:
            logger.exception("Не удалось получить список карт для админской выдачи")
            all_cards = []

        card = None
        for item in all_cards:
            try:
                if int(item.get("card_id") or 0) == int(card_id or 0):
                    card = item
                    break
            except Exception:
                continue

        if not card:
            await callback.bot.send_message(operator_id, "⚠️ Карта не найдена.")
            return

        if int(card.get("owner_id") or 0) <= 0:
            await callback.bot.send_message(operator_id, "⚠️ У этой карты не указан владелец Mastercard.")
            return
    else:
        card, deny_reason = await get_mastercard_card_for_issue(
            int(card_id or 0),
            float(payment),
        )
        if not card:
            await callback.bot.send_message(
                operator_id,
                f"⚠️ Карту нельзя выдать.\n\nПричина: {deny_reason}",
            )
            return

        if role_normalized == "mastercard" and int(card.get("owner_id") or 0) != operator_id:
            await callback.answer("🚫 Можно выдавать только свои карты.", show_alert=True)
            return

    if method == "sbp":
        chosen = str(card.get("sbp_phone") or "").strip()
        if not chosen:
            await callback.bot.send_message(operator_id, "⚠️ У этой карты не указан номер для СБП.")
            return
    else:
        method = "card"
        chosen = str(card.get("card_number") or "").strip()
        if not chosen:
            await callback.bot.send_message(operator_id, "⚠️ У этой карты не указан номер карты.")
            return

    bank_name = str(card.get("bank_name") or "Банк").strip()
    mastercard_owner_id = int(card.get("owner_id") or operator_id)

    db_conn = await get_db()
    await db_conn.execute(
        """
        UPDATE p2p_orders
           SET bank_card = ?,
               bank_name = ?,
               payment_method = ?,
               card_id = ?,
               operator_id = ?
         WHERE order_id = ?
        """,
        (
            chosen,
            bank_name,
            method,
            int(card["card_id"]),
            mastercard_owner_id,
            db_order_id,
        ),
    )
    await db_conn.commit()

    comment = await get_user_comment(operator_id) or "без комментария"

    if not is_guest and user_id in pending_buy_messages:
        chat_id, msg_id = pending_buy_messages.pop(user_id)
        try:
            await callback.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    if not is_guest:
        text_user = INSTRUCTION_TEXT_TEMPLATE_USER.format(
            card=chosen,
            bank=bank_name,
            comment=comment,
            amount=payment,
        )

        ikb = InlineKeyboardMarkup().add(
            InlineKeyboardButton("✅ Оплатил", callback_data="paid"),
            InlineKeyboardButton("🚫 Отменить", callback_data=f"user_cancel_order:{db_order_id}"),
        )

        await bot_send(
            callback.bot,
            user_id,
            text_user,
            parse_mode="Markdown",
            reply_markup=ikb,
        )
    else:
        await callback.bot.send_message(
            operator_id,
            "ℹ️ Это WEB-заявка. Реквизиты сохранены в заявке и будут показаны на web-странице.",
        )

    await update_operator_order_card_with_requisites(
        callback.bot,
        operator_id=operator_id,
        user_id=user_id,
        order_id=db_order_id,
        rec=rec,
        number=chosen,
        bank_name=bank_name,
        comment=comment,
        payment_rub=payment,
    )


# -----------------------------------------------------------------------------
# Раздел: Хендлеры — открытие заявки и обновление карточки
# -----------------------------------------------------------------------------
async def operator_open_order(callback: types.CallbackQuery) -> None:
    """Открывает карточку по заявке для оператора; если есть реквизиты — отображает их."""
    await callback.answer()
    parts = (callback.data or "").split(":")
    try:
        user_id = int(parts[1])
        order_id = int(parts[2])
    except Exception:
        await callback.bot.send_message(callback.from_user.id, "⚠️ Неверные данные кнопки.")
        return

    rec = await get_order_by_id(order_id)
    if not rec:
        await callback.bot.send_message(callback.from_user.id, "⚠️ Заявка не найдена.")
        return

    user_chat = await callback.bot.get_chat(user_id)
    mention = (
        f"@{user_chat.username}"
        if getattr(user_chat, "username", None)
        else f'<a href="tg://user?id={user_id}">{_h(user_chat.full_name or str(user_id))}</a>'
    )

    btc_amt = float(rec.get("btc_amount", 0))

    raw_wallet = rec.get("wallet", "")
    wallet = _h(raw_wallet)
    asset = _guess_asset_from_wallet(str(raw_wallet), rec)

    total_rub = float(rec.get("total_rub", 0))

    raw_sum = (
        rec.get("rub_amount")
        or rec.get("amount_rub")
        or rec.get("amount")
        or rec.get("total_rub", 0.0)
    )
    try:
        sum_rub = math.ceil(float(raw_sum))
    except Exception:
        sum_rub = math.ceil(float(total_rub))

    base_rate = await get_usd_rub()
    now = datetime.now()
    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M")

    bank_card = (rec or {}).get("bank_card")
    bank_name = (rec or {}).get("bank_name")

    comment: Optional[str]
    try:
        op_id = rec.get("operator_id")
        comment = await get_user_comment(op_id) if op_id else None
    except Exception:
        comment = None
    comment = comment or "без комментария"

    requisites_exist = bool(bank_card and bank_name)

    if requisites_exist:
        text_op = (
            f"📥 Заявка #{order_id}\n\n"
            f"От: {mention}\n"
            f"Сумма: {sum_rub} ₽\n"
            f"К выдаче: {_format_asset_amount_for_operator(asset, btc_amt)} {asset}\n"
            f"Кошелек {asset}: <code>{wallet}</code>\n\n"
            f"Дата: {date_str}\n"
            f"Время: {time_str}\n"
            f"Курс: {base_rate:.2f} ₽\n\n"
            f"📝 Реквизиты для оплаты:\n"
            f"➖➖➖➖➖➖➖➖➖➖\n"
            f"▶ Номер:    {_h(str(bank_card))}\n"
            f"▶ Банк:     {_h(bank_name)}\n"
            f"▶ Коммент.: {_h(comment)}\n"
            f"▶ Сумма:    {math.ceil(total_rub)} ₽\n"
            f"➖➖➖➖➖➖➖➖➖➖"
        )
    else:
        text_op = (
            f"📥 Заявка #{order_id}\n\n"
            f"От: {mention}\n"
            f"Сумма: {sum_rub} ₽\n"
            f"К выдаче: {_format_asset_amount_for_operator(asset, btc_amt)} {asset}\n"
            f"Кошелек {asset}: <code>{wallet}</code>\n\n"
            f"Дата: {date_str}\n"
            f"Время: {time_str}\n"
            f"Курс: {base_rate:.2f} ₽\n\n"
            f"💰 <b>К оплате: {math.ceil(total_rub)} ₽</b>"
        )

    msg = await bot_send(
        callback.bot,
        callback.from_user.id,
        text_op,
        parse_mode="HTML",
        reply_markup=_kb_op_order_actions(user_id, order_id, hide_cancel=requisites_exist),
    )

    try:
        operator_order_msgs[(callback.from_user.id, order_id)] = msg.message_id
    except Exception:
        pass


async def update_operator_order_card_with_requisites(
    bot: Bot,
    *,
    operator_id: int,
    user_id: int,
    order_id: int,
    rec: Optional[Dict[str, Any]] = None,
    number: Optional[str] = None,
    bank_name: Optional[str] = None,
    comment: Optional[str] = None,
    payment_rub: Optional[int] = None,
    requisites: Optional[str] = None,
    amount: Optional[int] = None,
) -> None:
    """
    Обновляет у оператора карточку заявки, добавляя блок реквизитов.

    Совместима с обоими стилями вызова:
    - старый: rec=..., number=..., bank_name=..., comment=..., payment_rub=...
    - новый: requisites=..., bank_name=..., comment=..., amount=...

    Дополнительно:
    - если у заявки payment_method='sbp', в карточке оператора будет подпись "СБП",
      иначе "Номер".
    """
    try:
        if rec is None:
            try:
                rec = await get_order_by_id(order_id)
            except Exception:
                rec = None

        rec = rec or {}
        is_guest = _is_guest_order(rec, user_id)

        btc_amt = float(rec.get("btc_amount", 0) or 0)

        raw_wallet = rec.get("wallet", "")
        wallet = _h(raw_wallet)
        asset = _guess_asset_from_wallet(str(raw_wallet), rec)

        raw_sum = (
            rec.get("rub_amount")
            or rec.get("amount_rub")
            or rec.get("amount")
            or rec.get("total_rub", 0)
        )
        try:
            sum_rub = math.ceil(float(raw_sum))
        except Exception:
            sum_rub = math.ceil(float(rec.get("total_rub", 0) or 0))

        base_rate = await get_usd_rub()
        try:
            base_rate_value = float(base_rate or 0)
        except Exception:
            base_rate_value = 0.0

        now = datetime.now()
        date_str = now.strftime("%d.%m.%Y")
        time_str = now.strftime("%H:%M")

        mention = await _build_order_user_mention(bot, user_id, rec)

        req_value_raw = requisites if requisites is not None else number
        req_number = _h(str(req_value_raw or ""))
        req_bank = _h(bank_name or "")
        req_comment = _h(comment or "без комментария")
        payment = int(amount if amount is not None else (payment_rub if payment_rub is not None else 0))

        payment_method = str(rec.get("payment_method") or "").strip().lower()
        req_label = "СБП" if payment_method == "sbp" else "Номер"

        updated_text = (
            f"📥 Заявка #{order_id}\n\n"
            f"От: {mention}\n"
            f"Сумма: {sum_rub} ₽\n"
            f"К выдаче: {_format_asset_amount_for_operator(asset, btc_amt)} {asset}\n"
            f"Кошелек {asset}: <code>{wallet}</code>\n\n"
            f"Дата: {date_str}\n"
            f"Время: {time_str}\n"
            f"Курс: {base_rate_value:.2f} ₽\n\n"
            f"📝 Реквизиты для оплаты:\n"
            f"➖➖➖➖➖➖➖➖➖➖\n"
            f"▶ {req_label}:    {req_number}\n"
            f"▶ Банк:     {req_bank}\n"
            f"▶ Коммент.: {req_comment}\n"
            f"▶ Сумма:    {payment} ₽\n"
            f"➖➖➖➖➖➖➖➖➖➖"
        )

        kb = (
            _kb_op_guest_order_actions(
                user_id,
                order_id,
                show_cards=False,
                hide_cancel=True,
            )
            if is_guest
            else _kb_op_order_actions(
                user_id,
                order_id,
                hide_cancel=True,
                hide_payment_actions=True,
            )
        )

        key = (operator_id, order_id)
        msg_id = operator_order_msgs.get(key)

        if msg_id:
            try:
                await bot.edit_message_text(
                    updated_text,
                    chat_id=operator_id,
                    message_id=msg_id,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                return
            except Exception:
                pass

        sent = await bot_send(
            bot,
            operator_id,
            updated_text,
            parse_mode="HTML",
            reply_markup=kb,
        )
        try:
            operator_order_msgs[key] = sent.message_id
        except Exception:
            pass

    except Exception:
        logger.exception(
            "Не удалось обновить карточку заявки оператора с реквизитами "
            "(operator_id=%s, user_id=%s, order_id=%s)",
            operator_id,
            user_id,
            order_id,
        )

# -----------------------------------------------------------------------------
# Раздел: Хендлеры — обмен сообщениями
# -----------------------------------------------------------------------------
async def close_chat(message: types.Message) -> None:
    """Закрытие текущего чата оператором."""
    user = await get_user(message.from_user.id)
    if not user or user.get("role") not in ("Operator", "Admin"):
        return

    partner = active_chats.get(message.from_user.id)
    if not partner:
        await message.answer("⚠️ Нет активного чата.")
        return

    try:
        rec = await get_pending_order(partner)
    except Exception:
        rec = None

    await delete_order(partner)
    await bot_send(message.bot, partner, "🚫 Чат закрыт.", reply_markup=message.reply_markup)
    await bot_send(message.bot, message.from_user.id, "✅ Чат закрыт.", reply_markup=message.reply_markup)

    await clear_history(message.bot, chat_histories, message.from_user.id)
    active_chats.pop(message.from_user.id, None)
    active_chats.pop(partner, None)

    if rec and rec.get("order_id"):
        await _cleanup_sms_thread(message.bot, partner, message.from_user.id, int(rec["order_id"]))

    await send_welcome(message.bot, partner)


async def relay(message: types.Message) -> None:
    """Ретрансляция сообщений между участниками активного чата."""
    partner = active_chats.get(message.from_user.id)
    if not partner:
        return

    user = await get_user(message.from_user.id)
    is_operator = (user or {}).get("role") in ("Operator", "Admin")
    prefix = "Оператор:" if is_operator else "Пользователь:"

    try:
        if message.content_type == ContentType.TEXT:
            kb = InlineKeyboardMarkup().add(
                InlineKeyboardButton(
                    "Ответить",
                    callback_data=(
                        f"reply_to_user:{message.from_user.id}"
                        if is_operator
                        else f"reply_to_operator:{message.from_user.id}"
                    ),
                )
            )
            sent = await bot_send(message.bot, partner, f"{prefix} {message.text}", reply_markup=kb)
        else:
            sent = await message.bot.copy_message(partner, message.chat.id, message.message_id)

        key = partner if is_operator else message.from_user.id
        chat_histories.setdefault(key, []).append((partner, sent.message_id))
    except Exception:
        logger.exception("relay error")


async def operator_message(callback: types.CallbackQuery) -> None:
    """Запрос текста/файла от оператора для пользователя внутри SMS-чата."""
    await callback.answer()
    operator_id = callback.from_user.id
    parts = (callback.data or "").split(":")
    try:
        user_id = int(parts[1])
    except Exception:
        return

    order_id: Optional[int] = None
    if len(parts) >= 3:
        try:
            order_id = int(parts[2])
        except Exception:
            order_id = None

    if order_id is None:
        rec = await get_pending_order(user_id)
        order_id = int(rec["order_id"]) if rec and rec.get("order_id") else None

    prompt = await bot_send(
        callback.bot, operator_id, "✏️ Введите текст или прикрепите файл для пользователя:"
    )
    pending_operator_texts[operator_id] = (user_id, order_id, prompt.message_id)


async def operator_message_input(message: types.Message) -> None:
    """Обработка ввода оператора для пользователя в SMS-чате."""
    operator_id = message.from_user.id
    entry = pending_operator_texts.pop(operator_id, None)
    if not entry or not isinstance(entry, (tuple, list)) or len(entry) < 3:
        return

    user_id, order_id, prompt_message_id = entry
    await safe_delete(message.bot, operator_id, prompt_message_id)

    if order_id is None:
        try:
            await safe_delete(message.bot, operator_id, message.message_id)
        except Exception:
            pass
        return

    if message.content_type == ContentType.TEXT:
        text = _h((message.text or "").strip())
    else:
        cap = getattr(message, "caption", None)
        text = _h((cap or "(см. вложение)").strip())

    await _append_and_update(message.bot, user_id, operator_id, order_id, role="op", text=text)

    try:
        await safe_delete(message.bot, operator_id, message.message_id)
    except Exception:
        pass


async def reply_to_user(callback: types.CallbackQuery) -> None:
    """Запрос ответа оператора пользователю в рамках SMS-чата."""
    await callback.answer()
    op_id = callback.from_user.id
    parts = (callback.data or "").split(":")
    try:
        user_id = int(parts[1])
    except Exception:
        return
    try:
        order_id = int(parts[2])
    except Exception:
        order_id = None

    try:
        await safe_edit(callback.message, reply_markup=None)
    except Exception:
        pass

    prompt = await bot_send(
        callback.bot, op_id, "✏️ Введите текст ответа или прикрепите файл для пользователя:"
    )
    pending_reply_to_user[op_id] = (user_id, order_id, prompt.message_id)


async def handle_reply_from_operator(message: types.Message) -> None:
    """Обработка ответа оператора пользователю."""
    operator_id = message.from_user.id
    entry = pending_reply_to_user.pop(operator_id, None)
    if not entry or not isinstance(entry, (tuple, list)) or len(entry) != 3:
        return

    user_id, order_id, prompt_message_id = entry
    await safe_delete(message.bot, operator_id, prompt_message_id)

    if order_id is None:
        try:
            await safe_delete(message.bot, operator_id, message.message_id)
        except Exception:
            pass
        return

    if message.content_type == ContentType.TEXT:
        text = _h((message.text or "").strip())
    else:
        cap = getattr(message, "caption", None)
        text = _h((cap or "(см. вложение)").strip())

    await _append_and_update(message.bot, user_id, operator_id, order_id, role="op", text=text)

    try:
        await safe_delete(message.bot, operator_id, message.message_id)
    except Exception:
        pass


async def reply_to_operator(callback: types.CallbackQuery) -> None:
    """Запрос ответа пользователя оператору в рамках SMS-чата."""
    await callback.answer()
    user_id = callback.from_user.id
    parts = (callback.data or "").split(":")
    try:
        op_id = int(parts[1])
    except Exception:
        return
    try:
        order_id = int(parts[2])
    except Exception:
        order_id = None

    try:
        await safe_edit(callback.message, reply_markup=None)
    except Exception:
        pass

    prompt = await bot_send(
        callback.bot, user_id, "✏️ Введите текст ответа или прикрепите файл для оператора:"
    )
    pending_reply_to_operator[user_id] = (op_id, order_id, prompt.message_id)


async def handle_reply_from_user(message: types.Message) -> None:
    """Обработка ответа пользователя оператору."""
    user_id = message.from_user.id
    entry = pending_reply_to_operator.pop(user_id, None)
    if not entry or not isinstance(entry, (tuple, list)) or len(entry) != 3:
        return

    operator_id, order_id, prompt_message_id = entry
    await safe_delete(message.bot, user_id, prompt_message_id)

    if order_id is None:
        try:
            await safe_delete(message.bot, user_id, message.message_id)
        except Exception:
            pass
        return

    if message.content_type == ContentType.TEXT:
        text = _h((message.text or "").strip())
    else:
        cap = getattr(message, "caption", None)
        text = _h((cap or "(см. вложение)").strip())

    await _append_and_update(message.bot, user_id, operator_id, order_id, role="user", text=text)

    try:
        await safe_delete(message.bot, user_id, message.message_id)
    except Exception:
        pass


async def get_user_comment(user_id: int) -> None:
    """
    Заглушка: комментарий оператора не используется.
    Всегда возвращает None, чтобы подставлялось 'без комментария'.
    """
    return None


# -----------------------------------------------------------------------------
# Раздел: Регистрация хендлеров
# -----------------------------------------------------------------------------
def register_operator_handlers(dp: Dispatcher) -> None:
    """Регистрирует все обработчики, связанные с работой оператора."""
    dp.register_callback_query_handler(
        operator_accept,
        lambda c: (c.data or "").startswith("operator_accept"),
        state="*",
    )
    dp.register_callback_query_handler(
        operator_open_order,
        lambda c: (c.data or "").startswith("operator_open_order:"),
        state="*",
    )
    dp.register_callback_query_handler(
        operator_cards,
        lambda c: (c.data or "").startswith("operator_cards:"),
        state="*",
    )
    dp.register_callback_query_handler(
        operator_card_selected,
        lambda c: (c.data or "").startswith("operator_card_selected:"),
        state="*",
    )
    dp.register_callback_query_handler(
        operator_reject,
        lambda c: (c.data or "").startswith("operator_reject:"),
        state="*",
    )
    dp.register_callback_query_handler(
        user_cancel_order,
        lambda c: (c.data or "").startswith("user_cancel_order:"),
        state="*",
    )
    dp.register_callback_query_handler(
        user_cancel_order, lambda c: (c.data or "") == "cancel_pay", state="*"
    )
    dp.register_callback_query_handler(
        operator_message,
        lambda c: (c.data or "").startswith("operator_message:"),
        state="*",
    )
    dp.register_callback_query_handler(
        close_sms_chat,
        lambda c: (c.data or "").startswith("close_sms_chat:"),
        state="*",
    )
    dp.register_message_handler(
        operator_message_input,
        lambda m: m.from_user.id in pending_operator_texts,
        content_types=ContentType.ANY,
    )
    dp.register_callback_query_handler(
        reply_to_user,
        lambda c: (c.data or "").startswith("reply_to_user:"),
        state="*",
    )
    dp.register_message_handler(
        handle_reply_from_operator,
        lambda m: m.from_user.id in pending_reply_to_user,
        content_types=ContentType.ANY,
    )
    dp.register_callback_query_handler(
        reply_to_operator,
        lambda c: (c.data or "").startswith("reply_to_operator:"),
        state="*",
    )
    dp.register_callback_query_handler(
        lambda c: c.answer("Заявка уже принята другим оператором."),
        lambda c: (c.data or "") == "noop",
        state="*",
    )
    dp.register_message_handler(
        handle_reply_from_user,
        lambda m: m.from_user.id in pending_reply_to_operator,
        content_types=ContentType.ANY,
    )
    dp.register_message_handler(close_chat, commands=["close_chat"])
    dp.register_message_handler(close_chat, lambda m: m.text == "Закрыть чат", state=None)
    dp.register_message_handler(
        relay,
        lambda m: m.from_user.id in active_chats,
        content_types=ContentType.ANY,
    )
    dp.register_callback_query_handler(
        operator_p2p_warn_start,
        lambda c: (c.data or "").startswith("operator_p2p_warn:"),
        state="*",
    )
    dp.register_callback_query_handler(
        operator_p2p_warn_yes,
        lambda c: (c.data or "").startswith("operator_p2p_warn_yes:"),
        state="*",
    )
    dp.register_callback_query_handler(
        operator_p2p_warn_no,
        lambda c: (c.data or "").startswith("operator_p2p_warn_no:"),
        state="*",
    )
    dp.register_callback_query_handler(
        user_p2p_agree,
        lambda c: (c.data or "").startswith("user_p2p_agree:"),
        state="*",
    )


