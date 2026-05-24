# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
from typing import Optional

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from handlers.common import send_welcome


# -----------------------------------------------------------------------------
# Раздел: Заглушки БД (временные, для совместимости)
# -----------------------------------------------------------------------------
try:
    from db.users import get_referrals_count  # type: ignore
except Exception:  # pragma: no cover
    async def get_referrals_count(user_id: int) -> int:
        """Временная заглушка: количество рефералов."""
        return 0


try:
    from db.referrals import get_inviter_commission_sum  # type: ignore
except Exception:  # pragma: no cover
    async def get_inviter_commission_sum(inviter_id: int) -> float:
        """Временная заглушка: сумма комиссий пригласившего."""
        return 0.0


# -----------------------------------------------------------------------------
# Раздел: Вспомогательные функции
# -----------------------------------------------------------------------------
async def _make_ref_link(bot: Bot, user_id: int) -> str:
    """Формирует персональную реферальную ссылку для пользователя."""
    me = await bot.get_me()
    username: Optional[str] = getattr(me, "username", None)
    if not username:
        return f"tg://resolve?domain=&start={user_id}"
    return f"https://t.me/{username}?start={user_id}"


# -----------------------------------------------------------------------------
# Раздел: Хендлеры — реферальная программа
# -----------------------------------------------------------------------------
async def show_referral_menu(callback: types.CallbackQuery) -> None:
    """Показывает меню реферальной программы с краткой статистикой."""
    user_id = callback.from_user.id

    try:
        refs_count = await get_referrals_count(user_id)
    except Exception:
        refs_count = 0

    try:
        total_commission = await get_inviter_commission_sum(user_id)
    except Exception:
        total_commission = 0.0

    kb = (
        InlineKeyboardMarkup()
        .add(InlineKeyboardButton("🔗 Сгенерировать ссылку", callback_data="gen_ref_link"))
        .add(InlineKeyboardButton("🏠 Меню", callback_data="main_menu"))
    )

    text = (
        "<b>Реферальная программа</b>\n\n"
        "Приглашайте друзей — вы получаете <b>2%</b> от суммы каждой их "
        "завершённой сделки.\n\n"
        f"👥 Ваши рефералы: <b>{refs_count}</b>\n"
        f"💰 Начислено всего: <b>{total_commission:.2f}</b>\n"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def send_ref_link(callback: types.CallbackQuery) -> None:
    """Генерирует и отправляет пользователю его реферальную ссылку."""
    link = await _make_ref_link(callback.bot, callback.from_user.id)
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🏠 Меню", callback_data="main_menu"))
    await callback.message.edit_text(f"<code>{link}</code>", parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def main_menu(callback: types.CallbackQuery) -> None:
    """Возврат в главное меню бота."""
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_welcome(callback.bot, callback.message.chat.id)
    await callback.answer()


# -----------------------------------------------------------------------------
# Раздел: Регистрация хендлеров
# -----------------------------------------------------------------------------
def register_referral(dp: Dispatcher) -> None:
    """Регистрирует обработчики реферального меню и генерации ссылки."""
    dp.register_callback_query_handler(
        show_referral_menu, lambda c: (c.data or "") == "ref_menu"
    )
    dp.register_callback_query_handler(
        send_ref_link, lambda c: (c.data or "") in ("gen_ref_link", "generate_ref_link")
    )
    dp.register_callback_query_handler(
        main_menu, lambda c: (c.data or "") == "main_menu"
    )
