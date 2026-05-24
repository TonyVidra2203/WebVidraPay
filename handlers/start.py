# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
from aiogram import types, Dispatcher
from aiogram.dispatcher import FSMContext
from aiogram.types import (
    ReplyKeyboardRemove,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

from db.users import add_or_update_user, get_user, set_referral_link, count_users, get_admin_user_ids
from handlers.admin import admin_start
from handlers.common import send_welcome
from keyboards.inline import shop_reply_keyboard


# -----------------------------------------------------------------------------
# Раздел: Обработчики команд
# -----------------------------------------------------------------------------
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    """
    Обрабатывает команду /start: регистрирует/обновляет пользователя,
    назначает реферальную ссылку и перенаправляет по роли.
    + Уведомляет админов при первом появлении нового пользователя.
    """
    await state.finish()
    user_id = message.from_user.id
    username = message.from_user.username
    args = message.get_args() or ""
    referrer_id = int(args) if args.isdigit() and int(args) != user_id else None

    existing_user = await get_user(user_id)
    is_new_user = existing_user is None
    role = (existing_user or {}).get("role")

    await add_or_update_user(user_id, username, referrer_id)
    bot = message.bot
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={user_id}"
    await set_referral_link(user_id, ref_link)

    # --- Уведомление админам о новом пользователе (только при первой регистрации) ---
    if is_new_user:
        try:
            admin_ids = await get_admin_user_ids()
            total_users = await count_users()

            full_name = (message.from_user.full_name or "").strip()
            uname = f"@{username}" if username else "—"
            ref_text = str(referrer_id) if referrer_id is not None else "—"

            # Кликабельное упоминание (в Telegram) через tg://user?id=
            mention = f"<a href=\"tg://user?id={user_id}\">{full_name or 'Пользователь'}</a>"

            text = (
                "🆕 <b>Новый пользователь</b>\n"
                f"• {mention}\n"
                f"• ID: <code>{user_id}</code>\n"
                f"• Username: {uname}\n"
                f"• Referrer ID: <code>{ref_text}</code>\n"
                f"• Всего пользователей: <b>{total_users}</b>\n"
            )

            for admin_id in admin_ids:
                # не шлём самому себе, если админ регается
                if admin_id == user_id:
                    continue
                try:
                    await bot.send_message(
                        admin_id,
                        text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    # админ мог заблокировать бота / недоступен — не ломаем /start
                    pass

        except Exception:
            # на любые сбои уведомления не должны влиять на регистрацию
            pass
    # --- конец уведомления ---

    if role is None:
        user_after = await get_user(user_id)
        role = (user_after or {}).get("role")

    role_norm = str(role).strip().lower() if role is not None else ""

    if role_norm == "admin":
        await admin_start(message, state)
        return

    if role_norm == "operator":
        await bot.send_message(
            message.chat.id,
            "Оператор",
            reply_markup=ReplyKeyboardRemove(),
        )
        await send_welcome(bot, message.chat.id)
        return

    if role_norm == "mastercard":
        kb = ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add(KeyboardButton("💳 Карты"), KeyboardButton("✅ Заявки"))
        kb.add(KeyboardButton("▶️ Начать сессию"), KeyboardButton("⏹ Завершить сессию"))
        await bot.send_message(message.chat.id, "Меню MasterCard:", reply_markup=kb)
        await send_welcome(bot, message.chat.id)
        return

    if role_norm == "shop":
        await bot.send_message(
            message.chat.id,
            "Меню Shop:",
            reply_markup=shop_reply_keyboard(),
        )
        await send_welcome(bot, message.chat.id)
        return

    await send_welcome(bot, message.chat.id)


# -----------------------------------------------------------------------------
# Раздел: Регистрация хендлеров
# -----------------------------------------------------------------------------
def register(dp: Dispatcher) -> None:
    """
    Регистрирует хендлер команды /start.
    """
    dp.register_message_handler(cmd_start, commands=["start"], state="*")
