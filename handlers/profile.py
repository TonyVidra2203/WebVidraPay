# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
from __future__ import annotations
import asyncio
import time
from decimal import Decimal, ROUND_DOWN
from typing import Tuple
from utils.helpers import get_usdt_rub_rate
from db.users import get_user, ensure_user_web_password
from db.referrals import (
    create_referral_withdraw_request,
    get_referral_withdraw_request,
    set_referral_withdraw_status,
    add_referral_adjustment,
    get_referral_balance,
)



import aiosqlite
from aiogram import types, Dispatcher
from aiogram.dispatcher import FSMContext
from aiogram.types import InputFile, InlineKeyboardMarkup, InlineKeyboardButton

from binance import BinanceClient
from db.connection import get_db
from handlers.buy.p2p import cancel_p2p
from keyboards.inline import Callback, cancel_buy_keyboard


# -----------------------------------------------------------------------------
# Раздел: Callbacks/States для вывода реф. счёта
# -----------------------------------------------------------------------------
CB_REF_WITHDRAW = "ref_withdraw"
CB_REF_WITHDRAW_COIN_PREFIX = "ref_withdraw_coin:"  # + BTC/LTC
CB_REF_WITHDRAW_BACK = "ref_withdraw_back"
CB_REFWD_PREFIX = "refwd:"

STATE_REF_WITHDRAW_WALLET = "ref_withdraw_wallet"


# -----------------------------------------------------------------------------
# Раздел: Конвертация RUB -> монета (временная логика)
# -----------------------------------------------------------------------------


def _fmt_amount(value: float, decimals: int) -> str:
    q = Decimal("1." + ("0" * decimals))
    return str(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))


async def _convert_rub_to_coin_amount(rub_amount: int, coin: str) -> tuple[float, str, float]:
    """
    Конвертирует rub_amount (RUB) в выбранную монету через Binance цену к USDT.
    Возвращает: (coin_amount_float, coin_amount_str, usdt_amount_float)
    """
    rate = await get_usdt_rub_rate()
    if not rate or rate <= 0:
        rate = 1.0  # защита от деления на 0, по факту сюда почти не попадём

    usdt_amount = float(rub_amount) / float(rate)

    client = BinanceClient()

    if coin == "BTC":
        price = await client.get_price("BTCUSDT")  # 1 BTC в USDT
        coin_amount = usdt_amount / price if price > 0 else 0.0
        return coin_amount, _fmt_amount(coin_amount, 8), usdt_amount

    if coin == "LTC":
        price = await client.get_price("LTCUSDT")  # 1 LTC в USDT
        coin_amount = usdt_amount / price if price > 0 else 0.0
        return coin_amount, _fmt_amount(coin_amount, 8), usdt_amount

    return 0.0, "0", usdt_amount


# -----------------------------------------------------------------------------
# Раздел: Вспомогательные функции БД
# -----------------------------------------------------------------------------
async def _fetch_scalar(
    db: aiosqlite.Connection,
    query: str,
    params: Tuple[object, ...] = (),
) -> int:
    """Выполнить запрос и вернуть скалярное значение (int), подставляя параметры."""
    cur = await db.execute(query, params)
    try:
        row = await cur.fetchone()
    finally:
        await cur.close()
    return int((row[0] if row else 0) or 0)


# -----------------------------------------------------------------------------
# Раздел: Клавиатуры
# -----------------------------------------------------------------------------
def _personal_account_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура под личным кабинетом.
    Кнопки:
    - 🔐 Мой пароль
    - 💸 Вывод реферальных
    - 🚫 Отмена
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔐 Мой пароль",
                    callback_data=Callback.SHOW_WEB_PASSWORD,
                )
            ],
            [
                InlineKeyboardButton(
                    text="💸 Вывод реферальных",
                    callback_data=CB_REF_WITHDRAW,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Отмена",
                    callback_data=Callback.CANCEL_BUY,
                )
            ],
        ]
    )


async def show_web_password(callback: types.CallbackQuery) -> None:
    """Показывает пользователю его личный пароль для входа в web-кабинет."""
    await callback.answer()

    user_id = callback.from_user.id

    try:
        password = await ensure_user_web_password(user_id)
    except Exception as e:
        await callback.message.answer(
            f"❌ Не удалось получить пароль для web-входа: {e}"
        )
        return

    text = (
        "🔐 <b>Ваш пароль для входа в web-кабинет</b>\n\n"
        f"<code>{password}</code>\n\n"
        "Используйте этот пароль на странице входа в web-версию.\n"
        "Никому его не передавайте."
    )

    await callback.message.answer(text, parse_mode="HTML")


def _withdraw_coin_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора монеты для вывода (BTC/LTC в один ряд)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="BTC (Bitcoin)",
                callback_data=f"{CB_REF_WITHDRAW_COIN_PREFIX}BTC",
            ),
            InlineKeyboardButton(
                text="LTC (Litecoin)",
                callback_data=f"{CB_REF_WITHDRAW_COIN_PREFIX}LTC",
            ),
        ],
        [
            InlineKeyboardButton(
                text="↩️ Назад",
                callback_data=CB_REF_WITHDRAW_BACK,
            )
        ],
    ])


# -----------------------------------------------------------------------------
# Раздел: Асинхронные функции — профиль
# -----------------------------------------------------------------------------
async def personal_account(callback: types.CallbackQuery) -> None:
    """Показать личный кабинет пользователя с реферальной статистикой."""
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass

    user_id = callback.from_user.id
    db = await get_db()

    exchanges_count = await _fetch_scalar(
        db,
        "SELECT COUNT(*) FROM p2p_orders WHERE user_id = ? AND status = 'completed'",
        (user_id,),
    )
    referrals_count = await _fetch_scalar(
        db,
        "SELECT COUNT(*) FROM users WHERE referrer_id = ?",
        (user_id,),
    )

    referral_balance = await get_referral_balance(user_id)

    me = await callback.bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={user_id}"

    text = (
        f"Ваш уникальный ID: {user_id}\n"
        f"Количество обменов: {exchanges_count}\n"
        f"Количество рефералов: {referrals_count}\n"
        f"Реферальный счёт: {referral_balance:.2f} RUB\n\n"
        "Для входа в web-кабинет используйте кнопку «🔐 Мой пароль» ниже.\n\n"
        f"Ваша реферальная ссылка:\n{ref_link}"
    )

    photo = InputFile("assets/cabinet.jpg")
    await callback.bot.send_photo(
        chat_id=user_id,
        photo=photo,
        caption=text,
        parse_mode="HTML",
        reply_markup=_personal_account_keyboard(),
    )


# -----------------------------------------------------------------------------
# Раздел: Вывод реф. счёта — шаги
# -----------------------------------------------------------------------------
async def start_ref_withdraw(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Нажали 'Вывод' -> проверяем минималку и показываем условия/выбор монеты."""
    await callback.answer()
    await state.finish()

    user_id = callback.from_user.id
    db = await get_db()

    referral_balance_rub = await get_referral_balance(user_id)

    if referral_balance_rub < 1000:
        warn_text = (
            "🚫 Недостаточно средств для вывода.\n"
            "Минимальная сумма вывода: 1 000 RUB.\n\n"
            "Через 5 секунд верну в меню…"
        )

        try:
            await callback.message.edit_caption(caption=warn_text, reply_markup=None)
        except Exception:
            await callback.message.answer(warn_text)

        await asyncio.sleep(5)

        exchanges_count = await _fetch_scalar(
            db,
            "SELECT COUNT(*) FROM p2p_orders WHERE user_id = ? AND status = 'completed'",
            (user_id,),
        )
        referrals_count = await _fetch_scalar(
            db,
            "SELECT COUNT(*) FROM users WHERE referrer_id = ?",
            (user_id,),
        )
        referral_balance = await get_referral_balance(user_id)

        me = await callback.bot.get_me()
        ref_link = f"https://t.me/{me.username}?start={user_id}"

        cabinet_text = (
            f"Ваш уникальный ID: {user_id}\n"
            f"Количество обменов: {exchanges_count}\n"
            f"Количество рефералов: {referrals_count}\n"
            f"Реферальный счёт: {referral_balance:.2f} RUB\n\n"
            f"Ваша реферальная ссылка:\n{ref_link}"
        )

        try:
            await callback.message.edit_caption(
                caption=cabinet_text,
                parse_mode="HTML",
                reply_markup=_personal_account_keyboard(),
            )
        except Exception:
            await callback.message.answer(cabinet_text, reply_markup=_personal_account_keyboard())

        return

    text = (
        "💸 Вывод реферальных\n\n"
        "• Минимальная сумма: 1 000 RUB\n"
        "• Срок обработки: до 24 часов\n"
        "• Комиссия сети оплачивается из суммы вывода\n\n"
        "Выберите монету для вывода:"
    )

    try:
        await callback.message.edit_caption(caption=text, reply_markup=_withdraw_coin_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=_withdraw_coin_keyboard())


async def choose_ref_withdraw_coin(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Выбор монеты -> просим ввести кошелёк (без кнопок под сообщением)."""
    await callback.answer()

    data = callback.data or ""
    coin_raw = data.replace(CB_REF_WITHDRAW_COIN_PREFIX, "", 1)

    if coin_raw == "BTC":
        coin = "BTC"
    elif coin_raw == "LTC":
        coin = "LTC"
    else:
        return

    await state.update_data(ref_withdraw_coin=coin)
    await state.set_state(STATE_REF_WITHDRAW_WALLET)

    text = f"Введите кошелёк для вывода {coin}:"
    try:
        await callback.message.edit_caption(caption=text, reply_markup=None)
    except Exception:
        await callback.message.answer(text)


async def back_to_personal_account_fast(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Быстрый возврат в личный кабинет через редактирование caption (без удаления сообщения)."""
    await callback.answer()
    await state.finish()

    user_id = callback.from_user.id
    db = await get_db()

    exchanges_count = await _fetch_scalar(
        db,
        "SELECT COUNT(*) FROM p2p_orders WHERE user_id = ? AND status = 'completed'",
        (user_id,),
    )
    referrals_count = await _fetch_scalar(
        db,
        "SELECT COUNT(*) FROM users WHERE referrer_id = ?",
        (user_id,),
    )
    referral_balance = await get_referral_balance(user_id)

    me = await callback.bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={user_id}"

    text = (
        f"Ваш уникальный ID: {user_id}\n"
        f"Количество обменов: {exchanges_count}\n"
        f"Количество рефералов: {referrals_count}\n"
        f"Реферальный счёт: {referral_balance:.2f} RUB\n\n"
        f"Ваша реферальная ссылка:\n{ref_link}"
    )

    try:
        await callback.message.edit_caption(
            caption=text,
            parse_mode="HTML",
            reply_markup=_personal_account_keyboard(),
        )
    except Exception:
        await callback.message.answer(text, reply_markup=_personal_account_keyboard())


async def input_ref_withdraw_wallet(message: types.Message, state: FSMContext) -> None:
    """Пользователь вводит кошелёк -> создаём заявку и отправляем её админам на обработку."""
    wallet = (message.text or "").strip()
    if not wallet:
        await message.answer("Пожалуйста, введите кошелёк текстом.")
        return

    data = await state.get_data()
    coin = data.get("ref_withdraw_coin", "")

    if coin not in {"BTC", "LTC"}:
        await message.answer("🚫 Выберите монету заново.")
        await state.finish()
        return

    user_id = message.from_user.id
    referral_balance_rub = await get_referral_balance(user_id)

    if referral_balance_rub < 1000:
        await message.answer(
            "🚫 Недостаточно средств для вывода.\n"
            "Минимальная сумма вывода: 1 000 RUB."
        )
        await state.finish()
        return

    coin_amount, coin_amount_str, _ = await _convert_rub_to_coin_amount(
        int(referral_balance_rub), coin
    )

    request_id = f"{user_id}-{int(time.time())}"

    # 1) Записываем заявку в БД (pending)
    await create_referral_withdraw_request(
        request_id=request_id,
        user_id=user_id,
        amount_rub=float(referral_balance_rub),
        coin=coin,
        wallet=wallet,
    )

    # 2) Подтверждение пользователю
    user_text = (
        "✅ Заявка на вывод создана\n\n"
        f"• ID заявки: <code>{request_id}</code>\n"
        f"• Монета: {coin}\n"
        f"• Сумма: {float(referral_balance_rub):.2f} RUB ≈ <b>{coin_amount_str}</b> {coin}\n"
        f"• Кошелёк: <code>{wallet}</code>\n\n"
        "⏱ Срок обработки: до 24 часов."
    )
    await message.answer(user_text, parse_mode="HTML")

    # 3) Уведомление админам с кнопками
    admin_ids = await _get_admin_ids()
    admin_text = (
        "<b>💸 Заявка на вывод реферальных</b>\n\n"
        f"ID заявки: <code>{request_id}</code>\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Username: @{message.from_user.username or '-'}\n"
        f"Монета: {coin}\n"
        f"Сумма: {float(referral_balance_rub):.2f} RUB ≈ {coin_amount_str} {coin}\n"
        f"Кошелёк: <code>{wallet}</code>\n"
    )

    kb = _admin_ref_withdraw_kb(request_id)
    for aid in admin_ids:
        try:
            await message.bot.send_message(
                aid, admin_text, parse_mode="HTML", reply_markup=kb
            )
        except Exception:
            pass

    await state.finish()



async def admin_refwd_decision(callback: types.CallbackQuery) -> None:
    """
    Админ нажал "Оплачено" или "Отказ" под заявкой.
    """
    await callback.answer()

    admin = await get_user(callback.from_user.id)
    if not admin or admin.get("role") != "Admin":
        await callback.answer("Нет доступа.", show_alert=True)
        return

    data = callback.data or ""
    if not data.startswith(CB_REFWD_PREFIX):
        return

    try:
        payload = data[len(CB_REFWD_PREFIX):]
        request_id, action = payload.rsplit(":", 1)
    except Exception:
        await callback.message.answer("Некорректные данные заявки.")
        return

    req = await get_referral_withdraw_request(request_id)
    if not req:
        await callback.message.answer("Заявка не найдена.")
        return

    if req.get("status") != "pending":
        # чтобы не обрабатывали дважды
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(f"Заявка уже обработана: {req.get('status')}.")
        return

    user_id = int(req["user_id"])

    if action == "paid":
        # Обнуляем реф. счёт через корректировку (дельтой)
        balance = await get_referral_balance(user_id)
        if balance != 0:
            await add_referral_adjustment(
                referrer_id=user_id,
                admin_id=callback.from_user.id,
                amount=round(-float(balance), 2),
                reason=f"withdraw_paid:{request_id}",
            )

        await set_referral_withdraw_status(request_id, "paid")

        # Сообщение пользователю
        try:
            await callback.bot.send_message(
                user_id,
                "✅ Ваш вывод реферальных успешно выполнен.\n"
                "Если есть вопросы — напишите в поддержку.",
            )
        except Exception:
            pass

        # Убираем кнопки у админа
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.message.answer(f"✅ Заявка {request_id} отмечена как ОПЛАЧЕНО.")
        return

    if action == "reject":
        await set_referral_withdraw_status(request_id, "rejected")

        # Сообщение пользователю
        try:
            await callback.bot.send_message(
                user_id,
                "❌ В выводе реферальных отказано.\n"
                "Пожалуйста, обратитесь в поддержку за подробностями.",
            )
        except Exception:
            pass

        # Убираем кнопки у админа
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.message.answer(f"❌ Заявка {request_id} отмечена как ОТКАЗ.")
        return


async def _get_admin_ids() -> list[int]:
    """
    Список активных админов из таблицы users.
    """
    db = await get_db()
    cur = await db.execute(
        "SELECT telegram_id FROM users WHERE role = 'Admin' AND is_active = 1"
    )
    rows = await cur.fetchall()
    await cur.close()
    return [int(r[0]) for r in rows if r and r[0]]


def _admin_ref_withdraw_kb(request_id: str) -> InlineKeyboardMarkup:
    """
    Кнопки под заявкой для админов.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Оплачено",
                    callback_data=f"{CB_REFWD_PREFIX}{request_id}:paid",
                ),
                InlineKeyboardButton(
                    text="❌ Отказ",
                    callback_data=f"{CB_REFWD_PREFIX}{request_id}:reject",
                ),
            ]
        ]
    )



# -----------------------------------------------------------------------------
# Раздел: Регистрация обработчиков
# -----------------------------------------------------------------------------
def register_profile_handlers(dp: Dispatcher) -> None:
    """Зарегистрировать обработчики личного кабинета и отмены покупки."""
    dp.register_callback_query_handler(
        personal_account,
        lambda c: c.data == Callback.PERSONAL_ACCOUNT,
    )
    dp.register_callback_query_handler(
        show_web_password,
        lambda c: c.data == Callback.SHOW_WEB_PASSWORD,
        state="*",
    )
    dp.register_callback_query_handler(
        cancel_p2p,
        lambda c: c.data == Callback.CANCEL_BUY,
        state="*",
    )

    # Вывод реф. счёта
    dp.register_callback_query_handler(
        start_ref_withdraw,
        lambda c: c.data == CB_REF_WITHDRAW,
        state="*",
    )
    dp.register_callback_query_handler(
        choose_ref_withdraw_coin,
        lambda c: (c.data or "").startswith(CB_REF_WITHDRAW_COIN_PREFIX),
        state="*",
    )
    dp.register_callback_query_handler(
        back_to_personal_account_fast,
        lambda c: c.data == CB_REF_WITHDRAW_BACK,
        state="*",
    )
    dp.register_message_handler(
        input_ref_withdraw_wallet,
        state=STATE_REF_WITHDRAW_WALLET,
        content_types=types.ContentTypes.TEXT,
    )

    # Админские решения по заявке (Оплачено / Отказ)
    dp.register_callback_query_handler(
        admin_refwd_decision,
        lambda c: (c.data or "").startswith(CB_REFWD_PREFIX),
        state="*",
    )
