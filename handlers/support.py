# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
import logging
import contextlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ContentType,
)

from keyboards.inline import Callback
from db.users import get_all_users
from handlers.common import send_welcome
from handlers.chat.utils import bot_send, safe_delete


# -----------------------------------------------------------------------------
# Раздел: Константы и глобальные структуры
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# support_threads[user_id] = {
#   "user_msg_id": int | None,
#   "admin_msgs": {admin_id: msg_id},
#   "admins": set[int],
#   "history": [(role, text, time, display_name), ...],
#   "user_media": [ {type, file_id, caption, viewed_by_admins:set[int]}, ...],
#   "op_media":   [ {type, file_id, caption, viewed_by_user:bool}, ...],
# }
support_threads: Dict[int, Dict[str, Any]] = {}

# pending_reply_from_admin[admin_id] = (user_id, prompt_msg_id)
pending_reply_from_admin: Dict[int, Tuple[int, int]] = {}
# pending_reply_from_user[user_id] = prompt_msg_id
pending_reply_from_user: Dict[int, int] = {}

MAX_LINES = 14


# -----------------------------------------------------------------------------
# Раздел: Вспомогательные функции (утилиты)
# -----------------------------------------------------------------------------
def _h(text: Optional[str]) -> str:
    """
    Экранирует HTML-символы в тексте.
    """
    t = (text or "").strip()
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _now() -> str:
    """
    Возвращает текущее время в формате HH:MM.
    """
    return datetime.now().strftime("%H:%M")


def _display_name_for_user(chat: types.Chat, fallback_id: int) -> str:
    """
    Формирует отображаемое имя пользователя.
    """
    full = (getattr(chat, "full_name", None) or "").strip()
    if full:
        return _h(full)
    if getattr(chat, "username", None):
        return _h(chat.username)
    return str(fallback_id)


def _render_support_card(user_id: int) -> str:
    """
    Формирует текст карточки поддержки на основе истории сообщений.

    Формат:

    🛟 Поддержка
    ─────
    🙋 Tony Hopkins:
    — Привет

    👤 Оператор:
    — Привет!
    — Как дела?

    🙋 Tony Hopkins:
    — Нормально…
    — У тебя как?
    """
    data = support_threads.get(user_id) or {}
    history = data.get("history", [])

    lines: List[str] = [
        "🛟 Поддержка",
        "─────",
    ]

    if not history:
        lines.append("Пока сообщений нет.")
        return "\n".join(lines).rstrip()

    recent = history[-MAX_LINES:]

    # groups: (role, display_name, [texts])
    groups: List[Tuple[str, str, List[str]]] = []

    for item in recent:
        if isinstance(item, (tuple, list)) and len(item) >= 4:
            role, text, _, display = item
        else:
            role, text = item[0], item[1]
            display = "Пользователь" if role != "op" else "Оператор"

        text = text or ""
        display_name = "Оператор" if role == "op" else (display or "Пользователь")

        if groups and groups[-1][0] == role and groups[-1][1] == display_name:
            groups[-1][2].append(text)
        else:
            groups.append((role, display_name, [text]))

    # Рендер групп
    for role, display_name, texts in groups:
        header = f"👤 {display_name}:" if role == "op" else f"🙋 {display_name}:"

        lines.append("")  # отступ между блоками
        lines.append(header)

        for txt in texts:
            msg_lines = (txt or "").splitlines() or [""]
            first = True
            for line in msg_lines:
                if first:
                    lines.append(f"— {line or ' '}")
                    first = False
                else:
                    lines.append(f"   {line or ' '}")

    return "\n".join(lines).rstrip()


def _kb_user_chat(user_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура для пользователя.
    """
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("💬 Ответить", callback_data="support:user_reply"),
        InlineKeyboardButton("📎 Вложения", callback_data="support:user_attach"),
    )
    kb.add(
        InlineKeyboardButton(
            "❌ Завершить", callback_data=f"support:close:{user_id}"
        )
    )
    return kb


def _kb_admin_chat(user_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура для администратора.
    """
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton(
            "💬 Ответить", callback_data=f"support:reply_to_user:{user_id}"
        ),
        InlineKeyboardButton(
            "📎 Вложения", callback_data=f"support:admin_attach:{user_id}"
        ),
    )
    kb.add(
        InlineKeyboardButton(
            "❌ Завершить", callback_data=f"support:close:{user_id}"
        )
    )
    return kb


# -----------------------------------------------------------------------------
# Раздел: Состояния FSM
# -----------------------------------------------------------------------------
class SupportStates(StatesGroup):
    """
    Набор состояний для диалога с поддержкой.
    """

    waiting_user_msg = State()
    waiting_admin_msg = State()


# -----------------------------------------------------------------------------
# Раздел: Управление потоками поддержки
# -----------------------------------------------------------------------------
def _thread(user_id: int) -> Dict[str, Any]:
    """
    Возвращает (или инициализирует) структуру потока поддержки для пользователя.
    """
    return support_threads.setdefault(
        user_id,
        {
            "user_msg_id": None,
            "admin_msgs": {},
            "admins": set(),
            "history": [],
            "user_media": [],  # список dict: {"type":..., "file_id":..., "caption":..., "viewed_by_admins": set[int]}
            "op_media": [],    # список dict: {"type":..., "file_id":..., "caption":..., "viewed_by_user": bool}
        },
    )


async def _ensure_support_thread(bot: Bot, user_id: int) -> Dict[str, Any]:
    """
    Гарантирует наличие карточек поддержки у пользователя и админов.
    """
    data = support_threads.get(user_id)
    if data:
        return data

    admins = await get_all_users()
    admin_ids: Set[int] = {
        int(a["telegram_id"]) for a in admins if a.get("role") == "Admin"
    }
    initial = _render_support_card(user_id)

    try:
        user_msg = await bot_send(
            bot,
            user_id,
            initial,
            parse_mode="HTML",
            reply_markup=_kb_user_chat(user_id),
        )
    except Exception:
        user_msg = await bot.send_message(
            user_id,
            initial,
            parse_mode="HTML",
            reply_markup=_kb_user_chat(user_id),
        )

    admin_msgs: Dict[int, int] = {}
    for aid in admin_ids:
        try:
            msg = await bot_send(
                bot,
                aid,
                initial,
                parse_mode="HTML",
                reply_markup=_kb_admin_chat(user_id),
            )
            admin_msgs[aid] = msg.message_id
        except Exception as e:
            logger.warning(
                "[support] Не удалось отправить карточку админу %s: %s", aid, e
            )

    support_threads[user_id] = {
        "user_msg_id": user_msg.message_id,
        "admin_msgs": admin_msgs,
        "admins": admin_ids,
        "history": [],
        "user_media": [],
        "op_media": [],
    }
    return support_threads[user_id]


async def _append_and_update(bot: Bot, user_id: int, role: str, text: str) -> None:
    """
    Добавляет запись в историю и обновляет карточки у пользователя и админов.
    """
    data = await _ensure_support_thread(bot, user_id)
    if role == "op":
        display = "Оператор"
    else:
        try:
            chat = await bot.get_chat(user_id)
            display = _display_name_for_user(chat, user_id)
        except Exception:
            display = str(user_id)

    data["history"].append((role, text, _now(), display))
    card = _render_support_card(user_id)

    with contextlib.suppress(Exception):
        await safe_delete(bot, user_id, data.get("user_msg_id"))

    try:
        user_new = await bot_send(
            bot,
            user_id,
            card,
            parse_mode="HTML",
            reply_markup=_kb_user_chat(user_id),
        )
        data["user_msg_id"] = user_new.message_id
    except Exception:
        pass

    to_remove: List[int] = []
    for aid, mid in list(data.get("admin_msgs", {}).items()):
        with contextlib.suppress(Exception):
            await safe_delete(bot, aid, mid)
        try:
            msg = await bot_send(
                bot,
                aid,
                card,
                parse_mode="HTML",
                reply_markup=_kb_admin_chat(user_id),
            )
            data["admin_msgs"][aid] = msg.message_id
        except Exception as e:
            logger.warning(
                "[support] Не удалось обновить карточку у админа %s: %s", aid, e
            )
            to_remove.append(aid)

    for aid in to_remove:
        data["admin_msgs"].pop(aid, None)
        data["admins"].discard(aid)


async def _cleanup_support_thread(bot: Bot, user_id: int) -> None:
    """
    Удаляет карточки и очищает данные потока поддержки.
    """
    data = support_threads.pop(user_id, None)
    if not data:
        return
    with contextlib.suppress(Exception):
        await safe_delete(bot, user_id, data.get("user_msg_id"))
    for aid, mid in list(data.get("admin_msgs", {}).items()):
        with contextlib.suppress(Exception):
            await safe_delete(bot, aid, mid)


def _extract_media_payloads(message: types.Message) -> List[Dict[str, Any]]:
    """
    Извлекает метаданные вложений из сообщения.
    """
    items: List[Dict[str, Any]] = []
    cap = getattr(message, "caption", None)
    if message.content_type == ContentType.PHOTO and message.photo:
        ph = max(message.photo, key=lambda p: p.width * p.height)
        items.append({"type": "photo", "file_id": ph.file_id, "caption": cap})
    elif message.content_type == ContentType.DOCUMENT and message.document:
        items.append(
            {"type": "document", "file_id": message.document.file_id, "caption": cap}
        )
    elif message.content_type == ContentType.VIDEO and message.video:
        items.append({"type": "video", "file_id": message.video.file_id, "caption": cap})
    elif message.content_type == ContentType.ANIMATION and message.animation:
        items.append(
            {"type": "animation", "file_id": message.animation.file_id, "caption": cap}
        )
    elif message.content_type == ContentType.AUDIO and message.audio:
        items.append({"type": "audio", "file_id": message.audio.file_id, "caption": cap})
    elif message.content_type == ContentType.VOICE and message.voice:
        items.append({"type": "voice", "file_id": message.voice.file_id, "caption": cap})
    elif message.content_type == ContentType.VIDEO_NOTE and message.video_note:
        items.append(
            {"type": "video_note", "file_id": message.video_note.file_id, "caption": None}
        )
    return items


async def _send_media_item(bot: Bot, chat_id: int, item: Dict[str, Any]) -> None:
    """
    Отправляет одно вложение по его типу.
    """
    t = item.get("type")
    fid = item.get("file_id")
    caption = item.get("caption")
    try:
        if t == "photo":
            await bot.send_photo(chat_id, fid, caption=caption)
        elif t == "document":
            await bot.send_document(chat_id, fid, caption=caption)
        elif t == "video":
            await bot.send_video(chat_id, fid, caption=caption)
        elif t == "animation":
            await bot.send_animation(chat_id, fid, caption=caption)
        elif t == "audio":
            await bot.send_audio(chat_id, fid, caption=caption)
        elif t == "voice":
            await bot.send_voice(chat_id, fid, caption=caption)
        elif t == "video_note":
            await bot.send_video_note(chat_id, fid)
    except Exception as e:
        logger.warning("send media failed: %r", e)


async def _name_of(bot: Bot, user_id: int) -> str:
    """
    Возвращает безопасное имя пользователя/чата для уведомлений.
    """
    try:
        ch = await bot.get_chat(user_id)
        full = (getattr(ch, "full_name", None) or "").strip()
        if full:
            return _h(full)
        if getattr(ch, "username", None):
            return f"@{_h(ch.username)}"
    except Exception:
        pass
    return str(user_id)


# -----------------------------------------------------------------------------
# Раздел: Обработчики (пользователь)
# -----------------------------------------------------------------------------
async def show_support(callback: types.CallbackQuery, state: FSMContext) -> None:
    """
    Показывает пользователю ввод для описания проблемы.
    """
    await callback.answer()
    if callback.message:
        with contextlib.suppress(Exception):
            await callback.message.delete()
    sent = await callback.bot.send_message(
        callback.from_user.id,
        "💬 Опишите, пожалуйста, вашу проблему (можно приложить скрин/файл/видео).",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("❌ Отменить", callback_data="support:user_cancel")
        ),
    )
    await state.update_data(prompt_msg_id=sent.message_id)
    await SupportStates.waiting_user_msg.set()


async def cancel_support(callback: types.CallbackQuery, state: FSMContext) -> None:
    """
    Отменяет создание обращения и возвращает приветствие.
    """
    await callback.answer()
    await state.finish()
    if callback.message:
        with contextlib.suppress(Exception):
            await callback.message.delete()
    await send_welcome(callback.bot, callback.from_user.id)


async def on_user_support_message(
    message: types.Message, state: FSMContext
) -> None:
    """
    Обрабатывает первое сообщение пользователя в обращении.
    """
    user_id = message.from_user.id
    if user_id in pending_reply_from_user:
        return

    data_state = await state.get_data()
    prompt_msg_id = data_state.get("prompt_msg_id")
    if prompt_msg_id:
        with contextlib.suppress(Exception):
            await safe_delete(message.bot, user_id, prompt_msg_id)

    await _ensure_support_thread(message.bot, user_id)

    if message.content_type == ContentType.TEXT:
        txt = _h((message.text or "").strip())
        if txt:
            await _append_and_update(message.bot, user_id, role="user", text=txt)
    else:
        media = _extract_media_payloads(message)
        caption = getattr(message, "caption", None)
        if caption:
            txt = _h(caption.strip())
        else:
            # системный текст для файлов — курсивом
            txt = '<i>(отправил(а) файл, нажмите "Вложения" для загрузки.)</i>'

        # каждое вложение: список админов, которые уже посмотрели
        for it in media:
            it["viewed_by_admins"] = set()

        _thread(user_id)["user_media"].extend(media)
        await _append_and_update(message.bot, user_id, role="user", text=txt)

    with contextlib.suppress(Exception):
        await safe_delete(message.bot, user_id, message.message_id)

    await SupportStates.waiting_user_msg.set()


async def user_reply_request(callback: types.CallbackQuery) -> None:
    """
    Запрашивает у пользователя текст/файл для ответа оператору.
    """
    await callback.answer()
    if callback.from_user.id not in support_threads:
        await callback.message.answer("⚠️ Диалог не активен.")
        return

    prompt = await bot_send(
        callback.bot, callback.from_user.id, "✏️ Введите текст ответа или прикрепите файл:"
    )
    pending_reply_from_user[callback.from_user.id] = prompt.message_id


async def handle_reply_from_user(message: types.Message) -> None:
    """
    Обрабатывает ответ пользователя после нажатия «Ответить».
    """
    user_id = message.from_user.id
    prompt_message_id = pending_reply_from_user.pop(user_id, None)
    if prompt_message_id is None:
        return
    if user_id not in support_threads:
        with contextlib.suppress(Exception):
            await safe_delete(message.bot, user_id, prompt_message_id)
        return

    if message.content_type == ContentType.TEXT:
        text = _h((message.text or "").strip())
        if text:
            await _append_and_update(message.bot, user_id, role="user", text=text)
    else:
        media = _extract_media_payloads(message)
        caption = getattr(message, "caption", None)
        if caption:
            text = _h(caption.strip())
        else:
            text = '<i>(отправил(а) файл, нажмите "Вложения" для загрузки.)</i>'

        for it in media:
            it["viewed_by_admins"] = set()

        _thread(user_id)["user_media"].extend(media)
        await _append_and_update(message.bot, user_id, role="user", text=text)

    with contextlib.suppress(Exception):
        await safe_delete(message.bot, user_id, prompt_message_id)
    with contextlib.suppress(Exception):
        await safe_delete(message.bot, user_id, message.message_id)


async def user_attach_request(callback: types.CallbackQuery) -> None:
    """
    Отправляет пользователю новые вложения от оператора.

    Каждый файл от оператора доступен пользователю один раз.
    """
    await callback.answer()
    uid = callback.from_user.id
    if uid not in support_threads:
        await callback.message.answer("⚠️ Диалог не активен.")
        return

    data = _thread(uid)
    items = data["op_media"]

    # только те, которые пользователь ещё не видел
    to_send = [it for it in items if not it.get("viewed_by_user")]
    if not to_send:
        await bot_send(callback.bot, uid, "📎 Новых вложений нет.")
        return

    for it in to_send:
        await _send_media_item(callback.bot, uid, it)
        it["viewed_by_user"] = True


# -----------------------------------------------------------------------------
# Раздел: Обработчики (администратор)
# -----------------------------------------------------------------------------
async def reply_to_user(callback: types.CallbackQuery) -> None:
    """
    Создаёт запрос на ответ админа пользователю.
    """
    await callback.answer()
    try:
        _, action, user_id_str = callback.data.split(":")
        assert action == "reply_to_user"
        user_id = int(user_id_str)
    except Exception:
        return

    if user_id not in support_threads:
        await callback.message.answer("⚠️ Диалог не активен.")
        return

    prompt = await callback.message.answer(
        "✏️ Введите текст ответа или прикрепите файл для пользователя:"
    )
    pending_reply_from_admin[callback.from_user.id] = (user_id, prompt.message_id)


async def handle_reply_from_admin(message: types.Message) -> None:
    """
    Обрабатывает сообщение админа в ответ пользователю.
    """
    admin_id = message.from_user.id
    entry = pending_reply_from_admin.pop(admin_id, None)
    if not entry:
        return

    try:
        user_id, prompt_message_id = entry
    except Exception:
        return

    if user_id not in support_threads:
        with contextlib.suppress(Exception):
            await safe_delete(message.bot, admin_id, prompt_message_id)
        await message.answer("⚠️ Диалог не активен.")
        return

    if message.content_type == ContentType.TEXT:
        text = _h((message.text or "").strip())
        if text:
            await _append_and_update(message.bot, user_id, role="op", text=text)
    else:
        media = _extract_media_payloads(message)
        caption = getattr(message, "caption", None)
        if caption:
            text = _h(caption.strip())
        else:
            text = '<i>(отправил(а) файл, нажмите "Вложения" для загрузки.)</i>'

        # каждое вложение: пользователь может скачать один раз
        for it in media:
            it["viewed_by_user"] = False

        _thread(user_id)["op_media"].extend(media)
        await _append_and_update(message.bot, user_id, role="op", text=text)

    with contextlib.suppress(Exception):
        await safe_delete(message.bot, admin_id, prompt_message_id)
    with contextlib.suppress(Exception):
        await safe_delete(message.bot, admin_id, message.message_id)


async def admin_attach_request(callback: types.CallbackQuery) -> None:
    """
    Отправляет админу новые вложения от пользователя.

    Каждый админ может скачать каждое вложение пользователя один раз.
    """
    await callback.answer()
    try:
        _, _, user_id_str = callback.data.split(":")
        user_id = int(user_id_str)
    except Exception:
        return

    if user_id not in support_threads:
        await callback.message.answer("⚠️ Диалог не активен.")
        return

    admin_id = callback.from_user.id
    data = _thread(user_id)
    items = data["user_media"]

    to_send: List[Dict[str, Any]] = []
    for it in items:
        viewed: set = it.setdefault("viewed_by_admins", set())
        if admin_id not in viewed:
            to_send.append(it)

    if not to_send:
        await callback.message.answer("📎 Новых вложений нет.")
        return

    for it in to_send:
        await _send_media_item(callback.bot, admin_id, it)
        it["viewed_by_admins"].add(admin_id)


async def close_support(callback: types.CallbackQuery, state: FSMContext) -> None:
    """
    Закрывает диалог поддержки и уведомляет стороны.
    """
    await callback.answer()
    try:
        _, action, user_id_str = callback.data.split(":")
        assert action == "close"
        user_id = int(user_id_str)
    except Exception:
        return

    data = support_threads.get(user_id)
    initiator_id = callback.from_user.id
    if not data:
        with contextlib.suppress(Exception):
            await callback.message.answer("Чат уже закрыт.")
        return

    initiator_is_user = initiator_id == user_id
    user_name = await _name_of(callback.bot, user_id)
    _ = "Пользователь" if initiator_is_user else "Оператор"  # для совместимости
    _ = await _name_of(callback.bot, initiator_id)  # для совместимости с логикой

    user_notice = "🔒 Сессия поддержки завершена."
    admin_notice = f"🔒 Сессия поддержки с {user_name} завершена."

    try:
        data["user_media"].clear()
        data["op_media"].clear()
        data["history"].clear()
    except Exception:
        pass

    await _cleanup_support_thread(callback.bot, user_id)

    with contextlib.suppress(Exception):
        await callback.bot.send_message(user_id, user_notice)
        await send_welcome(callback.bot, user_id)

    admins = list(set(data.get("admins") or []))
    for aid in admins:
        with contextlib.suppress(Exception):
            await callback.bot.send_message(aid, admin_notice)

    if initiator_id not in admins and initiator_id != user_id:
        with contextlib.suppress(Exception):
            await callback.bot.send_message(initiator_id, "✅ Чат закрыт.")

    await state.finish()


async def start_support_from_admin(callback: types.CallbackQuery) -> None:
    """
    Запуск SMS-чата поддержки из админской карточки БД.

    Кнопка: support:from_admin:<user_id>
    """
    await callback.answer()
    data_str = callback.data or ""

    try:
        prefix, action, user_id_str = data_str.split(":")
        assert prefix == "support" and action == "from_admin"
        user_id = int(user_id_str)
    except Exception:
        return

    bot = callback.bot
    admin_id = callback.from_user.id

    # Гарантируем наличие потока поддержки (создаст карточку у пользователя и админов)
    data = await _ensure_support_thread(bot, user_id)

    # Гарантируем/обновим карточку у ТЕКУЩЕГО админа
    card_text = _render_support_card(user_id)
    admin_msgs = data.get("admin_msgs") or {}
    old_msg_id = admin_msgs.get(admin_id)

    with contextlib.suppress(Exception):
        if old_msg_id:
            await safe_delete(bot, admin_id, old_msg_id)

    try:
        msg = await bot_send(
            bot,
            admin_id,
            card_text,
            parse_mode="HTML",
            reply_markup=_kb_admin_chat(user_id),
        )
        admin_msgs[admin_id] = msg.message_id
        data["admin_msgs"] = admin_msgs
        data.setdefault("admins", set()).add(admin_id)
    except Exception:
        # тихо, чат всё равно доступен через другие точки
        pass


# -----------------------------------------------------------------------------
# Раздел: Регистрация хендлеров
# -----------------------------------------------------------------------------
def register_support_handlers(dp: Dispatcher) -> None:
    """
    Регистрирует все обработчики, связанные с поддержкой.
    """
    dp.register_callback_query_handler(
        show_support, lambda c: c.data == Callback.TECH_SUPPORT, state="*"
    )
    dp.register_callback_query_handler(
        cancel_support, lambda c: c.data == "support:user_cancel", state="*"
    )

    # запуск чата поддержки из админского меню БД
    dp.register_callback_query_handler(
        start_support_from_admin,
        lambda c: (c.data or "").startswith("support:from_admin:"),
        state="*",
    )

    dp.register_message_handler(
        handle_reply_from_user,
        lambda m: m.from_user.id in pending_reply_from_user,
        state="*",
        content_types=ContentType.ANY,
    )
    dp.register_message_handler(
        handle_reply_from_admin,
        lambda m: m.from_user.id in pending_reply_from_admin,
        state="*",
        content_types=ContentType.ANY,
    )

    dp.register_message_handler(
        on_user_support_message,
        state=SupportStates.waiting_user_msg,
        content_types=ContentType.ANY,
    )

    dp.register_callback_query_handler(
        user_reply_request, lambda c: c.data == "support:user_reply", state="*"
    )
    dp.register_callback_query_handler(
        user_attach_request, lambda c: c.data == "support:user_attach", state="*"
    )

    dp.register_callback_query_handler(
        reply_to_user,
        lambda c: c.data and c.data.startswith("support:reply_to_user:"),
        state="*",
    )
    dp.register_callback_query_handler(
        admin_attach_request,
        lambda c: c.data and c.data.startswith("support:admin_attach:"),
        state="*",
    )

    dp.register_callback_query_handler(
        close_support,
        lambda c: c.data and c.data.startswith("support:close:"),
        state="*",
    )
