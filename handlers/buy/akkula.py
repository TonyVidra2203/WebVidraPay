import asyncio
import math
import time
from typing import Optional, Tuple

import aiohttp
from aiogram import Dispatcher
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import AKKULA_API_KEY, AKKULA_BASE_URL, AKKULA_TIMEOUT_SEC
from db.akkula_orders import save_akkula_order
from db.connection import get_db
from db.p2p import get_pending_order, save_p2p_order
from db.users import (
    get_user_btc_wallet,
    get_user_ltc_wallet,
    get_user_usdt_trc20_wallet,
    set_user_btc_wallet,
    set_user_ltc_wallet,
    set_user_usdt_trc20_wallet,
)
from keyboards.inline import Callback, buy_keyboard
from services.akkula import AkkulaAPIError, AkkulaClient
from utils.helpers import get_btc_price, get_binance_ticker_price, get_usdt_rub_rate

AKKULA_DEFAULT_CLIENT_PHONE = "+79039158797"
AKKULA_DEFAULT_CLIENT_NAME = "Буклов Владислав"

AKKULA_FIXED_NETWORK = "bsc"
AKKULA_FIXED_RECIPIENT_WALLET = "0xa44d61ca85f125834820f82139df6dcf8c2de05f"

AKKULA_COMMISSION_PERCENT = 20.0
AKKULA_MIN_RUB_RECEIVE = 1000.0
AKKULA_ROUND_PAY_STEP = 100

AKKULA_CB_EDIT_AMOUNT = "akkula_edit:amount"
AKKULA_CB_EDIT_WALLET = "akkula_edit:wallet"
AKKULA_CB_CONFIRM = "akkula_confirm:create"

AKKULA_CB_COPY_LINK = "akkula_final:copy_link"
AKKULA_CB_SHOW_QR = "akkula_final:qr"
AKKULA_CB_FINAL_CANCEL = "akkula_final:cancel"
AKKULA_CB_INPUT_CANCEL = "akkula_input:cancel"

AKKULA_EPHEMERAL_DELETE_SEC = 15


class AkkulaLinkStates(StatesGroup):
    waiting_asset = State()
    editing_amount_rub = State()
    editing_recipient_wallet = State()


def _calc_pay_total_from_net(net_rub: float) -> float:
    total = float(net_rub) * (1.0 + AKKULA_COMMISSION_PERCENT / 100.0)
    return float(math.ceil(total / AKKULA_ROUND_PAY_STEP) * AKKULA_ROUND_PAY_STEP)


def _calc_net_from_pay_total(payable_rub: float) -> float:
    p = float(payable_rub)
    if p <= 0:
        return 0.0
    net = p / (1.0 + AKKULA_COMMISSION_PERCENT / 100.0)
    net = math.floor(net * 100.0) / 100.0
    return float(net)


def _min_payable() -> float:
    return _calc_pay_total_from_net(float(AKKULA_MIN_RUB_RECEIVE))


def _asset_title(asset: str) -> str:
    return {
        "BTC": "BTC (Bitcoin)",
        "LTC": "LTC (Litecoin)",
        "USDT_TRC20": "USDT (TRC20)",
    }.get(asset, asset)


def _asset_comment_code(asset: str) -> str:
    if asset == "LTC":
        return "LTC"
    if asset == "USDT_TRC20":
        return "USDT"
    return "BTC"


def _format_akkula_error(e: AkkulaAPIError) -> str:
    if e.code == "INVALID_WALLET":
        return "Неверный адрес кошелька. Проверьте формат."
    if e.code in ("AMOUNT_TOO_LOW", "AMOUNT_TOO_HIGH"):
        return "Сумма вне допустимого диапазона. Попробуйте другую сумму."
    if e.code == "DAILY_LIMIT_EXCEEDED":
        return "Превышен дневной лимит. Попробуйте позже."
    if e.code == "INSUFFICIENT_BALANCE":
        return "Сейчас невозможно создать ссылку (недостаточно средств у партнёра)."
    if e.code == "DUPLICATE_ORDER":
        return "Не удалось создать заказ (дубликат ID). Попробуйте ещё раз."
    if e.http_status == 429:
        return "Сервис временно перегружен (лимит запросов). Попробуйте ещё раз через минуту."
    if e.http_status in (401, 403):
        return "Ошибка доступа (проверьте API ключ/доступ)."
    if e.code == "VALIDATION_ERROR":
        return f"{e.message}"
    return f"Ошибка: {e}"


def _is_wallet_valid(asset: str, wallet: str) -> bool:
    w = (wallet or "").strip()
    if not w:
        return False
    if asset == "USDT_TRC20":
        return w.startswith("T") and len(w) >= 30
    return len(w) >= 20


async def _calculate_receive_from_net(net_rub: float, asset: str) -> Tuple[str, float, float]:
    usdt_rub = await get_usdt_rub_rate()
    if not usdt_rub or usdt_rub <= 0:
        return "-", 0.0, 0.0

    approx_rub_net = float(net_rub)
    amount_usdt_net = float(net_rub) / float(usdt_rub)

    if asset == "USDT_TRC20":
        receive = f"{amount_usdt_net:.2f} USDT (≈ {int(round(approx_rub_net))} RUB)"
        return receive, float(amount_usdt_net), approx_rub_net

    if asset == "BTC":
        btc_usdt = await get_btc_price()
        if not btc_usdt or btc_usdt <= 0:
            return "-", 0.0, 0.0
        btc_amount = amount_usdt_net / float(btc_usdt)
        receive = f"{btc_amount:.8f} BTC (≈ {int(round(approx_rub_net))} RUB)"
        return receive, float(btc_amount), approx_rub_net

    if asset == "LTC":
        ltc_usdt = await get_binance_ticker_price("LTCUSDT")
        if not ltc_usdt or ltc_usdt <= 0:
            return "-", 0.0, 0.0
        ltc_amount = amount_usdt_net / float(ltc_usdt)
        receive = f"{ltc_amount:.6f} LTC (≈ {int(round(approx_rub_net))} RUB)"
        return receive, float(ltc_amount), approx_rub_net

    return "-", 0.0, 0.0


def _template_text(
    *,
    asset: str,
    pay_rub: Optional[float],
    net_rub: Optional[float],
    receive_text: str,
    user_wallet: str,
    pay_url: Optional[str] = None,
) -> str:
    coin = _asset_title(asset)
    pay_str = f"{int(pay_rub)} RUB" if pay_rub is not None else "-"

    asset_code = (asset or "").upper().strip()
    if asset_code == "USDT_TRC20":
        wallet_label = "📥 USDT-кошелёк (TRC20):"
    elif asset_code == "LTC":
        wallet_label = "📥 LTC-кошелёк:"
    else:
        wallet_label = "📥 BTC-кошелёк:"

    receive_str = receive_text if receive_text else "-"
    sep = "➖" * 10

    lines = [
        "📝 Создание платёжной ссылки",
        sep,
        f"▶ Монета: {coin}",
        f"▶ Вы получите: {receive_str}",
        f"▶ Сумма ссылки: {pay_str}",
        sep,
        wallet_label,
        (user_wallet or "—"),
        sep,
    ]

    if pay_url:
        safe_url = str(pay_url).strip()
        lines.append(f"🔗 <a href=\"{safe_url}\">Ссылка на оплату</a>")
    else:
        lines.append("‼️ Заполните сумму ссылки и кошелёк, затем нажмите «Подтвердить».")

    return "\n".join(lines)


def _coins_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="BTC (Bitcoin)", callback_data="akkula_asset:BTC")],
            [
                InlineKeyboardButton(text="LTC (Litecoin)", callback_data="akkula_asset:LTC"),
                InlineKeyboardButton(text="USDT (TRC20)", callback_data="akkula_asset:USDT_TRC20"),
            ],
            [InlineKeyboardButton(text="🚫 Отмена", callback_data=Callback.CANCEL_BUY)],
        ]
    )


def _editor_keyboard(pay_rub: Optional[float], user_wallet: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✏️ Сумма", callback_data=AKKULA_CB_EDIT_AMOUNT),
            InlineKeyboardButton(text="✏️ Кошелёк", callback_data=AKKULA_CB_EDIT_WALLET),
        ]
    ]
    if pay_rub is not None and (user_wallet or "").strip():
        rows.append([InlineKeyboardButton(text="✅ Подтвердить", callback_data=AKKULA_CB_CONFIRM)])
    rows.append([InlineKeyboardButton(text="🚫 Отмена", callback_data=Callback.CANCEL_BUY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _final_keyboard(partner_order_id: str, pay_url: str = "") -> InlineKeyboardMarkup:
    pid = str(partner_order_id).strip()
    url = str(pay_url or "").strip()

    rows = []

    # ✅ Кнопка оплаты (URL уже финальный, без akkula)
    if url:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Оплатить (online - банк)",
                    url=url,
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(text="🔗 Ссылка", callback_data=f"{AKKULA_CB_COPY_LINK}:{pid}"),
            InlineKeyboardButton(text="🧾 Оплата по QR", callback_data=f"{AKKULA_CB_SHOW_QR}:{pid}"),
        ]
    )

    rows.append([InlineKeyboardButton(text="🚫 Отменить", callback_data=f"{AKKULA_CB_FINAL_CANCEL}:{pid}")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _input_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚫 Отмена", callback_data=AKKULA_CB_INPUT_CANCEL)]]
    )


async def _auto_delete_message(bot, chat_id: int, message_id: int, delay: int = AKKULA_EPHEMERAL_DELETE_SEC) -> None:
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _get_saved_wallet(user_id: int, asset: str) -> str:
    a = (asset or "").upper().strip()
    if a == "LTC":
        return str((await get_user_ltc_wallet(int(user_id))) or "")
    if a == "USDT_TRC20":
        return str((await get_user_usdt_trc20_wallet(int(user_id))) or "")
    return str((await get_user_btc_wallet(int(user_id))) or "")


async def _save_wallet(user_id: int, asset: str, wallet: str) -> None:
    a = (asset or "").upper().strip()
    if a == "LTC":
        await set_user_ltc_wallet(int(user_id), wallet)
    elif a == "USDT_TRC20":
        await set_user_usdt_trc20_wallet(int(user_id), wallet)
    else:
        await set_user_btc_wallet(int(user_id), wallet)


async def akkula_create_link_start(call: CallbackQuery, state: FSMContext):
    sep = "➖" * 10
    await call.answer()
    await state.finish()

    try:
        await call.message.delete()
    except Exception:
        pass

    sent = await call.message.bot.send_message(
        chat_id=call.from_user.id,
        text=(
            "🔗  Создание платёжной ссылки\n\n"
            "Вы создаёте ссылку для приёма оплаты. После оплаты бот автоматически выполнит обмен и отправит монету на ваш кошелёк.\n"
            f"{sep}\n"
            "• Можно отправлять другим пользователям\n"
            "• Можно оплачивать самому\n"
            "• Оплата по QR коду\n"
            "• Реквизиты подставляются автоматически\n"
            "• Автообмен работает 24/7\n"
            f"{sep}\n\n"
            "📥  Выберите, какую монету хотите получить после оплаты ссылки:"
        ),
        reply_markup=_coins_keyboard(),
    )

    await state.update_data(asset_prompt_message_id=sent.message_id)
    await AkkulaLinkStates.waiting_asset.set()


async def akkula_asset_chosen(call: CallbackQuery, state: FSMContext):
    await call.answer()
    asset = (call.data or "").split(":", 1)[-1].strip()

    try:
        await call.message.delete()
    except Exception:
        pass

    saved_wallet = ""
    try:
        saved_wallet = await _get_saved_wallet(call.from_user.id, asset)
    except Exception:
        saved_wallet = ""

    await state.update_data(
        asset=asset,
        pay_rub=None,
        net_rub=None,
        receive_text="-",
        crypto_amount=0.0,
        approx_rub_net=0.0,
        user_wallet=saved_wallet,
        template_message_id=None,
        last_prompt_message_id=None,
        final_pay_url="",
        final_qr_url="",
    )

    sent = await call.message.bot.send_message(
        chat_id=call.from_user.id,
        text=_template_text(asset=asset, pay_rub=None, net_rub=None, receive_text="-", user_wallet=saved_wallet),
        reply_markup=_editor_keyboard(pay_rub=None, user_wallet=saved_wallet),
    )
    await state.update_data(template_message_id=sent.message_id)
    await AkkulaLinkStates.waiting_asset.set()


async def akkula_edit_amount(call: CallbackQuery, state: FSMContext):
    await call.answer()

    sent = await call.message.bot.send_message(
        chat_id=call.from_user.id,
        text=(
            "💸 Введите сумму, которую будут ОПЛАЧИВАТЬ по ссылке (в рублях).\n"
            f"Минимум: {int(_min_payable())} RUB.\n\n"
            "Пример: 10000"
        ),
        reply_markup=_input_cancel_keyboard(),
    )
    await state.update_data(last_prompt_message_id=sent.message_id)
    await AkkulaLinkStates.editing_amount_rub.set()


async def akkula_amount_edited(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")

    try:
        pay_rub = float(raw)
    except ValueError:
        sent = await message.answer("Введите сумму числом (например: 10000).", reply_markup=_input_cancel_keyboard())
        asyncio.create_task(_auto_delete_message(message.bot, sent.chat.id, sent.message_id, delay=3))
        try:
            await message.delete()
        except Exception:
            pass
        return

    if pay_rub <= 0:
        sent = await message.answer("Сумма должна быть больше 0.", reply_markup=_input_cancel_keyboard())
        asyncio.create_task(_auto_delete_message(message.bot, sent.chat.id, sent.message_id, delay=3))
        try:
            await message.delete()
        except Exception:
            pass
        return

    pay_rub_norm = int(pay_rub) if float(pay_rub).is_integer() else int(round(pay_rub))

    if float(pay_rub_norm) < float(_min_payable()):
        try:
            await message.delete()
        except Exception:
            pass

        sent = await message.bot.send_message(chat_id=message.chat.id, text=f"⚠️ Минимум — {int(_min_payable())} ₽.")
        asyncio.create_task(_auto_delete_message(message.bot, sent.chat.id, sent.message_id, delay=3))
        return

    net_rub = _calc_net_from_pay_total(float(pay_rub_norm))

    data = await state.get_data()
    asset = data.get("asset")
    template_message_id = data.get("template_message_id")
    last_prompt_message_id = data.get("last_prompt_message_id")
    user_wallet = str(data.get("user_wallet") or "")

    if not asset or not template_message_id:
        await message.answer(
            "Сессия устарела. Нажмите «Создать ссылку оплаты» и начните заново.",
            reply_markup=buy_keyboard(),
        )
        await state.finish()
        return

    if last_prompt_message_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=int(last_prompt_message_id))
        except Exception:
            pass

    try:
        await message.delete()
    except Exception:
        pass

    receive_text, crypto_amount, approx_rub_net = await _calculate_receive_from_net(float(net_rub), asset)

    await state.update_data(
        pay_rub=float(pay_rub_norm),
        net_rub=float(net_rub),
        receive_text=receive_text,
        crypto_amount=float(crypto_amount),
        approx_rub_net=float(approx_rub_net),
        last_prompt_message_id=None,
    )

    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=int(template_message_id),
            text=_template_text(
                asset=asset,
                pay_rub=float(pay_rub_norm),
                net_rub=float(net_rub),
                receive_text=receive_text,
                user_wallet=user_wallet,
            ),
            reply_markup=_editor_keyboard(pay_rub=float(pay_rub_norm), user_wallet=user_wallet),
        )
    except Exception:
        pass

    await AkkulaLinkStates.waiting_asset.set()


async def akkula_edit_wallet(call: CallbackQuery, state: FSMContext):
    await call.answer()

    data = await state.get_data()
    asset = (data.get("asset") or "BTC").upper().strip()

    ps_text = "<i>P.S. После первого ввода кошелёк сохраняется</i>\n\n"
    if asset == "USDT_TRC20":
        text = ps_text + "📝 Введите кошелёк USDT (TRC20):"
    elif asset == "LTC":
        text = ps_text + "📝 Введите кошелёк LTC:"
    else:
        text = ps_text + "📝 Введите кошелёк BTC:"

    sent = await call.message.bot.send_message(
        chat_id=call.from_user.id,
        text=text,
        reply_markup=_input_cancel_keyboard(),
        parse_mode="HTML",
    )

    await state.update_data(last_prompt_message_id=sent.message_id)
    await AkkulaLinkStates.editing_recipient_wallet.set()


async def akkula_wallet_edited(message: Message, state: FSMContext):
    wallet = (message.text or "").strip()

    data = await state.get_data()
    asset = data.get("asset")
    template_message_id = data.get("template_message_id")
    last_prompt_message_id = data.get("last_prompt_message_id")

    pay_rub = data.get("pay_rub")
    net_rub = data.get("net_rub")
    receive_text = data.get("receive_text") or "-"

    if not asset or not template_message_id:
        await message.answer(
            "Сессия устарела. Нажмите «Создать ссылку оплаты» и начните заново.",
            reply_markup=buy_keyboard(),
        )
        await state.finish()
        return

    if not _is_wallet_valid(asset, wallet):
        sent = await message.answer("⚠️ Кошелёк выглядит некорректно.", reply_markup=_input_cancel_keyboard())
        asyncio.create_task(_auto_delete_message(message.bot, sent.chat.id, sent.message_id, delay=3))
        try:
            await message.delete()
        except Exception:
            pass
        return

    if last_prompt_message_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=int(last_prompt_message_id))
        except Exception:
            pass

    try:
        await message.delete()
    except Exception:
        pass

    try:
        await _save_wallet(message.from_user.id, asset, wallet)
    except Exception:
        pass

    await state.update_data(user_wallet=wallet, last_prompt_message_id=None)

    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=int(template_message_id),
            text=_template_text(
                asset=asset,
                pay_rub=pay_rub,
                net_rub=net_rub,
                receive_text=receive_text,
                user_wallet=wallet,
            ),
            reply_markup=_editor_keyboard(pay_rub=pay_rub, user_wallet=wallet),
        )
    except Exception:
        pass

    await AkkulaLinkStates.waiting_asset.set()


async def akkula_confirm_create(call: CallbackQuery, state: FSMContext):
    await call.answer()

    data = await state.get_data()
    asset = data.get("asset")

    pay_rub = data.get("pay_rub")
    net_rub = data.get("net_rub")

    template_message_id = data.get("template_message_id")
    user_wallet = str(data.get("user_wallet") or "").strip()
    crypto_amount = float(data.get("crypto_amount") or 0.0)
    receive_text = str(data.get("receive_text") or "-")

    if not asset or pay_rub is None:
        await call.answer("Сначала укажите сумму.", show_alert=True)
        return
    if not user_wallet:
        await call.answer("Сначала укажите кошелёк получения.", show_alert=True)
        return
    if crypto_amount <= 0 or not net_rub or float(net_rub) <= 0:
        await call.answer("Не удалось рассчитать сумму к получению. Попробуйте позже.", show_alert=True)
        return

    payable = float(pay_rub)
    if payable < float(_min_payable()):
        await call.message.bot.send_message(
            chat_id=call.from_user.id,
            text=f"⚠️ Сумма слишком мала. Минимум — {int(_min_payable())} ₽.",
            reply_markup=buy_keyboard(),
        )
        await state.finish()
        return

    # ✅ Важно: НЕ отменяем другие Akkula pending-заявки.
    # Но если есть активная pending-заявка НЕ akkula — не смешиваем ветки.
    existing = await get_pending_order(call.from_user.id)
    if existing:
        pm_existing = str(existing.get("payment_method") or "").lower().strip()
        if pm_existing and pm_existing != "akkula":
            await call.message.bot.send_message(
                chat_id=call.from_user.id,
                text=(
                    "⚠️ У вас уже есть активная заявка на обмен. "
                    "Завершите или отмените её, затем создайте платёжную ссылку снова."
                ),
                reply_markup=buy_keyboard(),
            )
            await state.finish()
            return

    comment_code = _asset_comment_code(asset)
    p2p_comment = f"Akkula link ({comment_code})"
    p2p_order_id = 0

    # ✅ Создаём НОВУЮ p2p-заявку под каждую ссылку
    try:
        p2p_order_id = await save_p2p_order(
            user_id=int(call.from_user.id),
            operator_id=0,
            btc_amount=float(crypto_amount),
            rub_amount=float(net_rub),
            total_rub=float(payable),
            wallet=user_wallet,
            comment=p2p_comment,
        )

        db = await get_db()
        await db.execute(
            "UPDATE p2p_orders SET payment_method = ? WHERE order_id = ?",
            ("akkula", int(p2p_order_id)),
        )
        await db.commit()
    except Exception:
        await call.message.bot.send_message(
            chat_id=call.from_user.id,
            text="❌ Не удалось подготовить заявку для автообмена. Попробуйте ещё раз.",
            reply_markup=buy_keyboard(),
        )
        await state.finish()
        return

    client = AkkulaClient(api_key=AKKULA_API_KEY, base_url=AKKULA_BASE_URL, timeout_sec=AKKULA_TIMEOUT_SEC)
    partner_order_id = f"tg-{call.from_user.id}-{int(time.time())}"[:50]

    try:
        limits = await client.get_limits(amount_rub=float(payable), network=AKKULA_FIXED_NETWORK)
        if not limits.get("can_create_order", True):
            reason = limits.get("reason") or "Нельзя создать заказ на указанную сумму."
            await call.message.bot.send_message(
                chat_id=call.from_user.id,
                text=f"❌ {reason}",
                reply_markup=buy_keyboard(),
            )
            await state.finish()
            return

        order = await client.create_order(
            partner_order_id=partner_order_id,
            amount_rub=float(payable),
            recipient_wallet=AKKULA_FIXED_RECIPIENT_WALLET,
            network=AKKULA_FIXED_NETWORK,
            client_phone=AKKULA_DEFAULT_CLIENT_PHONE,
            client_name=AKKULA_DEFAULT_CLIENT_NAME,
            metadata={
                "tg_user_id": call.from_user.id,
                "source": "telegram_bot",
                "user_selected_asset": asset,
                "user_recipient_wallet": user_wallet,
                "p2p_order_id": int(p2p_order_id),
                "fixed_wallet": True,
            },
        )
    except AkkulaAPIError as e:
        await call.message.bot.send_message(
            chat_id=call.from_user.id,
            text=f"❌ {_format_akkula_error(e)}",
            reply_markup=buy_keyboard(),
        )
        await state.finish()
        return
    except Exception:
        await call.message.bot.send_message(
            chat_id=call.from_user.id,
            text="❌ Не удалось создать ссылку (внутренняя ошибка).",
            reply_markup=buy_keyboard(),
        )
        await state.finish()
        return

    short_url = order.get("payment_url") or order.get("short_payment_url") or ""
    qr_url = order.get("qr_image_url") or ""
    expires_at = order.get("expires_at")
    amount_usdt = order.get("amount_usdt")
    status = order.get("status")
    akkula_order_id = order.get("order_id")

    await state.update_data(
        final_pay_url=str(short_url),
        final_qr_url=str(qr_url),
        final_partner_order_id=str(partner_order_id),
        final_p2p_order_id=int(p2p_order_id) if p2p_order_id else None,
    )

    final_text = _template_text(
        asset=asset,
        pay_rub=float(payable),
        net_rub=float(net_rub),
        receive_text=receive_text,
        user_wallet=user_wallet,
        pay_url=str(short_url) if short_url else None,
    )

    # Пытаемся отредактировать исходное сообщение (чтобы webhook удалил именно его).
    link_message_id: Optional[int] = int(template_message_id) if template_message_id else None

    try:
        await call.message.bot.edit_message_text(
            chat_id=call.from_user.id,
            message_id=int(template_message_id or call.message.message_id),
            text=final_text,
            reply_markup=_final_keyboard(partner_order_id),
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            sent2 = await call.message.bot.send_message(
                chat_id=call.from_user.id,
                text=final_text,
                reply_markup=_final_keyboard(partner_order_id),
                disable_web_page_preview=True,
            )
            link_message_id = sent2.message_id
        except Exception:
            pass

    # Сохраняем akkula_order (уже с link_message_id)
    try:
        await save_akkula_order(
            partner_order_id=str(partner_order_id),
            order_id=str(akkula_order_id) if akkula_order_id else None,
            tg_user_id=int(call.from_user.id),
            status=str(status) if status else None,
            amount_rub=float(payable),
            amount_usdt=float(amount_usdt) if amount_usdt is not None else None,
            network=str(AKKULA_FIXED_NETWORK),
            recipient_wallet=AKKULA_FIXED_RECIPIENT_WALLET,
            short_payment_url=str(order.get("short_payment_url")) if order.get("short_payment_url") else None,
            payment_url=str(order.get("payment_url")) if order.get("payment_url") else None,
            qr_image_url=str(qr_url) if qr_url else None,
            expires_at=str(expires_at) if expires_at else None,
            user_selected_asset=str(asset),
            user_recipient_wallet=str(user_wallet),
            p2p_order_id=int(p2p_order_id) if p2p_order_id else None,
            link_message_id=int(link_message_id) if link_message_id else None,
        )
    except Exception:
        pass


async def akkula_cancel_input(call: CallbackQuery, state: FSMContext):
    await call.answer()

    data = await state.get_data()
    last_prompt_message_id = data.get("last_prompt_message_id")

    try:
        await call.message.delete()
    except Exception:
        pass

    if last_prompt_message_id and int(last_prompt_message_id) != int(call.message.message_id):
        try:
            await call.message.bot.delete_message(chat_id=call.from_user.id, message_id=int(last_prompt_message_id))
        except Exception:
            pass

    await state.update_data(last_prompt_message_id=None)
    await AkkulaLinkStates.waiting_asset.set()


async def akkula_cancel(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.finish()

    try:
        await call.message.delete()
    except Exception:
        pass

    await call.message.bot.send_message(
        chat_id=call.from_user.id,
        text="🚫 Отменено. Возврат в меню.",
        reply_markup=buy_keyboard(),
    )


async def akkula_final_copy_link(call: CallbackQuery, state: FSMContext):
    await call.answer()

    # ожидаем: "akkula_final:copy_link:<partner_order_id>"
    partner_order_id = ""
    parts = (call.data or "").split(":", 2)
    if len(parts) >= 3:
        partner_order_id = parts[2].strip()

    url = ""
    if partner_order_id:
        try:
            from db.akkula_orders import get_akkula_order_by_partner_id
            rec = await get_akkula_order_by_partner_id(partner_order_id)
            if rec:
                url = str(rec.get("payment_url") or rec.get("short_payment_url") or "").strip()
        except Exception:
            url = ""

    # fallback на старые сообщения (если кнопка без :partner_order_id)
    if not url:
        data = await state.get_data()
        url = str(data.get("final_pay_url") or "").strip()

    if not url:
        await call.answer("Ссылка не найдена. Создайте ссылку заново.", show_alert=True)
        return

    final_url = url
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                final_url = str(resp.url)
    except Exception:
        final_url = url

    sent = await call.message.bot.send_message(
        chat_id=call.from_user.id,
        text=f"🔗 Ссылка для оплаты:\n{final_url}",
        disable_web_page_preview=True,
    )
    asyncio.create_task(_auto_delete_message(call.message.bot, sent.chat.id, sent.message_id, delay=AKKULA_EPHEMERAL_DELETE_SEC))


async def akkula_final_show_qr(call: CallbackQuery, state: FSMContext):
    await call.answer()

    # ожидаем: "akkula_final:qr:<partner_order_id>"
    partner_order_id = ""
    parts = (call.data or "").split(":", 2)
    if len(parts) >= 3:
        partner_order_id = parts[2].strip()

    qr_url = ""
    if partner_order_id:
        try:
            from db.akkula_orders import get_akkula_order_by_partner_id
            rec = await get_akkula_order_by_partner_id(partner_order_id)
            if rec:
                qr_url = str(rec.get("qr_image_url") or "").strip()
        except Exception:
            qr_url = ""

    # fallback на старые сообщения
    if not qr_url:
        data = await state.get_data()
        qr_url = str(data.get("final_qr_url") or "").strip()

    if not qr_url:
        await call.answer("QR не найден. Создайте ссылку заново.", show_alert=True)
        return

    try:
        sent = await call.message.bot.send_photo(
            chat_id=call.from_user.id,
            photo=qr_url,
            caption="🧾 QR-код для оплаты",
        )
        asyncio.create_task(_auto_delete_message(call.message.bot, sent.chat.id, sent.message_id, delay=AKKULA_EPHEMERAL_DELETE_SEC))
        return
    except Exception:
        pass

    sent = await call.message.bot.send_message(
        chat_id=call.from_user.id,
        text=f"🧾 QR-код:\n{qr_url}",
        disable_web_page_preview=False,
    )
    asyncio.create_task(_auto_delete_message(call.message.bot, sent.chat.id, sent.message_id, delay=AKKULA_EPHEMERAL_DELETE_SEC))


async def akkula_final_cancel(call: CallbackQuery, state: FSMContext):
    await call.answer()

    # ожидаем: "akkula_final:cancel:<partner_order_id>"
    partner_order_id = ""
    parts = (call.data or "").split(":", 2)
    if len(parts) >= 3:
        partner_order_id = parts[2].strip()

    # Снимаем клавиатуру / пытаемся убрать сообщение со ссылкой
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Отменяем только ЭТУ заявку (если найдём связь с p2p_order_id)
    if partner_order_id:
        try:
            from db.akkula_orders import get_akkula_order_by_partner_id
            rec = await get_akkula_order_by_partner_id(partner_order_id)
            p2p_order_id = rec.get("p2p_order_id") if rec else None

            if p2p_order_id:
                db = await get_db()
                await db.execute(
                    "UPDATE p2p_orders SET status='canceled' WHERE order_id = ? AND status='pending'",
                    (int(p2p_order_id),),
                )
                await db.commit()
        except Exception:
            pass

    # Завершаем FSM
    await state.finish()

    # Удаляем текущее сообщение со ссылкой (чтобы не висело в чате)
    try:
        await call.message.delete()
    except Exception:
        pass

    # Вместо текста "🚫 Отменено..." — показываем обычное меню с картинкой (как при стандартной отмене)
    try:
        from handlers.common import send_welcome
        await send_welcome(call.message.bot, call.from_user.id)
    except Exception:
        # fallback на старое поведение, если send_welcome недоступен
        await call.message.bot.send_message(
            chat_id=call.from_user.id,
            text="",
            reply_markup=buy_keyboard(),
        )


def register_akkula_handlers(dp: Dispatcher):
    dp.register_callback_query_handler(
        akkula_create_link_start,
        lambda c: c.data == Callback.AKKULA_CREATE_LINK,
        state="*",
    )

    dp.register_callback_query_handler(
        akkula_asset_chosen,
        lambda c: (c.data or "").startswith("akkula_asset:"),
        state=AkkulaLinkStates.waiting_asset,
    )

    dp.register_callback_query_handler(
        akkula_edit_amount,
        lambda c: (c.data or "") == AKKULA_CB_EDIT_AMOUNT,
        state="*",
    )

    dp.register_message_handler(
        akkula_amount_edited,
        state=AkkulaLinkStates.editing_amount_rub,
        content_types=["text"],
    )

    dp.register_callback_query_handler(
        akkula_edit_wallet,
        lambda c: (c.data or "") == AKKULA_CB_EDIT_WALLET,
        state="*",
    )

    dp.register_message_handler(
        akkula_wallet_edited,
        state=AkkulaLinkStates.editing_recipient_wallet,
        content_types=["text"],
    )

    dp.register_callback_query_handler(
        akkula_confirm_create,
        lambda c: (c.data or "") == AKKULA_CB_CONFIRM,
        state="*",
    )

    dp.register_callback_query_handler(
        akkula_cancel_input,
        lambda c: (c.data or "") == AKKULA_CB_INPUT_CANCEL,
        state=[AkkulaLinkStates.editing_amount_rub, AkkulaLinkStates.editing_recipient_wallet],
    )

    dp.register_callback_query_handler(
        akkula_cancel,
        lambda c: c.data == Callback.CANCEL_BUY,
        state="*",
    )

    # ✅ Новые callback_data с partner_order_id + поддержка старых сообщений
    dp.register_callback_query_handler(
        akkula_final_copy_link,
        lambda c: (c.data or "").startswith(f"{AKKULA_CB_COPY_LINK}:") or (c.data or "") == AKKULA_CB_COPY_LINK,
        state="*",
    )

    dp.register_callback_query_handler(
        akkula_final_show_qr,
        lambda c: (c.data or "").startswith(f"{AKKULA_CB_SHOW_QR}:") or (c.data or "") == AKKULA_CB_SHOW_QR,
        state="*",
    )

    dp.register_callback_query_handler(
        akkula_final_cancel,
        lambda c: (c.data or "").startswith(f"{AKKULA_CB_FINAL_CANCEL}:") or (c.data or "") == AKKULA_CB_FINAL_CANCEL,
        state="*",
    )
