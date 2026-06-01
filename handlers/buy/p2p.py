import asyncio
import html
import json
import logging
import math
import re
import uuid
from contextlib import suppress
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.exceptions import (
    BotBlocked,
    CantInitiateConversation,
    ChatNotFound,
    Unauthorized,
)

from config.settings import settings
from db.connection import get_db
from db.p2p import get_pending_order, save_p2p_order, save_operator_notification
from db.users import (
    get_all_users,
    get_user_btc_wallet,
    get_user_commission,
    is_binance_verified,
    set_binance_verified,
    set_user_btc_wallet,
)
from db.cards import is_mastercard_balance_correction_active
from handlers.common import pending_buy_messages, pending_operator_messages, send_welcome
from keyboards.inline import Callback, cancel_buy_keyboard, operator_keyboard
import utils.helpers as helpers
from utils.helpers import (
    btc_required_for_usdt_ff_float,
    get_binance_ticker_price,
    get_btc_price,
    get_usd_rub,
)
from services.paycore import PaycoreAPIError, PaycoreClient
from services.nirvana import NirvanaAPIError, NirvanaClient
from services.nirvana_transmezh import (
    create_transmezh_payment_for_order,
    render_nirvana_transmezh_payment_text,
)
from db.nirvana_orders import (
    get_nirvana_order_by_client_id,
    mark_nirvana_order_success_processed,
    update_nirvana_order_status,
)

logger = logging.getLogger(__name__)

BTC_REGEX = re.compile(r"^(bc1[a-z0-9]{25,87}|[13][A-Za-z0-9]{25,34})$")
PHONE_CLEAN_RE = re.compile(r"[^\d+]+")
ORDER_ASSET_COMMENT_RE = re.compile(r"\((BTC|LTC|USDT|XMR)\)", re.IGNORECASE)

ORDER_ASSETS: Dict[int, str] = {}
PAYCORE_ORDER_TIMERS: Dict[int, Dict[str, asyncio.Task]] = {}
PAYCORE_STATUS_TASKS: Dict[int, asyncio.Task] = {}
PAYCORE_WATCH_META: Dict[int, Dict[str, Any]] = {}

AMOUNT_INPUT_TEMPLATE = (
    "Укажи количество <b>{asset}</b> или <b>RUB</b>,\n"
    "которое хочешь получить на свой кошелек\n\n"
    "‼️<u>Учитывай колебания курса и указывай сумму с запасом</u>‼️\n\n"
    "Пример: <b>{example_asset}</b> или <b>{example_rub}</b>"
)

MIN_RUB_EXCHANGE = 3000.0
AKKULA_ONLY_MAX_RUB = 5000
EPHEMERAL_DELETE_SEC = 3
USDT_FIXED_COMMISSION_P2P = 20.0
BINANCE_FIXED_COMMISSION = 15.0
NIRVANA_AUTO_MAX_RUB = 10000


class P2PStates(StatesGroup):
    amount = State()
    confirm = State()
    wallet = State()
    method = State()
    paycore_consent = State()
    paycore_phone = State()
    await_receipt_pdf = State()


def _build_payment_keyboard(*, paycore_only: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)

    if paycore_only:
        kb.add(
            InlineKeyboardButton(
                "💳 Оплатить картой / QR-код",
                callback_data=Callback.PAY_PAYCORE,
            )
        )
    else:
        kb.row(
            InlineKeyboardButton("СБП", callback_data=Callback.PAY_SBP),
            InlineKeyboardButton("На карту", callback_data=Callback.PAY_CARD),
        )

    kb.add(
        InlineKeyboardButton(
            "🚫 Отмена",
            callback_data=Callback.CANCEL_BUY,
        )
    )

    return kb


def _paycore_consent_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Согласен", callback_data="paycore_consent_yes"),
        InlineKeyboardButton("🚫 Отмена", callback_data=Callback.CANCEL_BUY),
    )
    return kb


def _phone_request_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton("📱 Отправить номер телефона", request_contact=True))
    return kb


def _paycore_keyboard(pay_url: str, order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)

    kb.row(
        InlineKeyboardButton(
            "📲 QR-код",
            callback_data=f"paycore_show_qr:{order_id}",
        ),
        InlineKeyboardButton(
            "✅ Оплатить",
            url=pay_url,
        ),
    )

    kb.add(
        InlineKeyboardButton(
            "❌ Отменить заявку",
            callback_data=f"user_cancel_order:{order_id}",
        )
    )

    return kb


async def paycore_show_qr_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id

    try:
        _, raw_order_id = (callback.data or "").split(":", 1)
        order_id = int(raw_order_id)
    except Exception:
        await callback.answer("⚠️ Не удалось открыть QR-код", show_alert=True)
        return

    order = await _get_order_by_id(order_id)
    if not order or int(order.get("user_id") or 0) != int(user_id):
        await callback.answer("⚠️ Заявка не найдена или уже закрыта", show_alert=True)
        return

    if str(order.get("status") or "").lower().strip() != "pending":
        await callback.answer("⚠️ Заявка уже не активна", show_alert=True)
        return

    tx = await _get_paycore_transaction(order_id)
    pay_url = str((tx or {}).get("pay_url") or "").strip()
    if not pay_url:
        await callback.answer("⚠️ Ссылка оплаты не найдена", show_alert=True)
        return

    qr_url = (
        "https://api.qrserver.com/v1/create-qr-code/"
        f"?size=260x260&margin=0&data={quote(pay_url)}"
    )

    await callback.answer("QR-код открыт на 20 секунд")

    try:
        sent = await callback.bot.send_photo(
            chat_id=user_id,
            photo=qr_url,
        )
    except Exception:
        logger.exception("Failed to send VidraPay QR code for order_id=%s", order_id)
        with suppress(Exception):
            await callback.bot.send_message(user_id, "⚠️ Не удалось показать QR-код. Используйте кнопку «Ссылка».")
        return

    async def _delete_qr_later(chat_id: int, message_id: int) -> None:
        await asyncio.sleep(20)
        with suppress(Exception):
            await callback.bot.delete_message(chat_id, message_id)

    asyncio.create_task(_delete_qr_later(sent.chat.id, sent.message_id))

def _binance_verify_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Согласен", callback_data=Callback.BINANCE_VERIFY_YES),
        InlineKeyboardButton("🚫 Отмена", callback_data=Callback.BINANCE_CANCEL),
    )
    return kb


def _binance_assets_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("BTC (Bitcoin)", callback_data=Callback.BINANCE_ASSET_BTC),
        InlineKeyboardButton("LTC (Litecoin)", callback_data=Callback.BINANCE_ASSET_LTC),
    )
    kb.row(
        InlineKeyboardButton("XMR (Monero)", callback_data=Callback.BINANCE_ASSET_XMR),
        InlineKeyboardButton("USDT (TRC20)", callback_data=Callback.BINANCE_ASSET_USDT),
    )
    kb.row(
        InlineKeyboardButton("ETH (Ethereum)", callback_data="asset_paused:ETH"),
        InlineKeyboardButton("USDT (BEP20)", callback_data="asset_paused:USDT_BEP20"),
    )
    kb.row(
        InlineKeyboardButton("BNB (Binance Coin)", callback_data="asset_paused:BNB"),
        InlineKeyboardButton("TON (Toncoin)", callback_data="asset_paused:TON"),
    )
    kb.row(
        InlineKeyboardButton("SOL (Solana)", callback_data="asset_paused:SOL"),
        InlineKeyboardButton("XRP (Ripple)", callback_data="asset_paused:XRP"),
    )
    kb.row(
        InlineKeyboardButton("TRX (Tron)", callback_data="asset_paused:TRX"),
        InlineKeyboardButton("DOGE (Dogecoin)", callback_data="asset_paused:DOGE"),
    )
    kb.add(InlineKeyboardButton("🚫 Отмена", callback_data=Callback.BINANCE_CANCEL))
    return kb



def _sanitize_number_text(raw: str) -> str:
    txt = (raw or "").strip().replace(",", ".")
    return re.sub(r"[\s\u00A0\u2007\u202F]", "", txt)


def _is_valid_decimal(txt: str) -> bool:
    if any(ch in txt for ch in ("e", "E", "+", "-")):
        return False
    if txt.count(".") > 1:
        return False
    return bool(re.fullmatch(r"\d+(\.\d+)?", txt))


def _normalize_phone(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = PHONE_CLEAN_RE.sub("", s)
    if s.count("+") > 1 or ("+" in s and not s.startswith("+")):
        s = s.replace("+", "")
    digits = s[1:] if s.startswith("+") else s
    digits = re.sub(r"\D+", "", digits)
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("8"):
        return f"+7{digits[1:]}"
    if len(digits) == 10:
        return f"+7{digits}"
    return f"+{digits}" if s.startswith("+") else digits


def _format_asset_amount_for_user(asset: str, amount: float) -> str:
    """
    Показывает пользователю фактическое количество монет без ложного округления.
    Значение должно совпадать с тем, что реально сохранено в заявке и уйдёт в обменник.
    """
    asset_u = str(asset or "").upper()

    if asset_u == "USDT":
        return str(int(round(float(amount))))

    precision_map = {
        "BTC": 8,
        "LTC": 8,
        "XMR": 8,
    }
    precision = precision_map.get(asset_u, 8)

    formatted = f"{float(amount):.{precision}f}".rstrip("0").rstrip(".")
    return formatted or "0"


def _paycore_is_success(status_data: Dict[str, Any], *, expected_amount: Optional[int] = None) -> bool:
    def _s(v: Any) -> str:
        return str(v or "").strip()

    if expected_amount is not None:
        amt_raw = _s(status_data.get("amount"))
        try:
            amt_int = int(float(amt_raw))
        except Exception:
            return False
        if amt_int != int(expected_amount):
            return False

    raw_status = status_data.get("status")
    try:
        st_int = int(str(raw_status).strip())
    except Exception:
        return False
    return st_int == 2


async def _user_mention(bot: Bot, user_id: int) -> str:
    try:
        chat = await bot.get_chat(user_id)
        return f'<a href="tg://user?id={user_id}">{html.escape(chat.full_name)}</a>'
    except Exception:
        return f'<a href="tg://user?id={user_id}">{user_id}</a>'


def _format_operator_card(
    order_id: int,
    user_mention: str,
    btc_amount: float,
    total_rub: int,
    *,
    canceled: bool = False,
    header: str = "📥 Заявка",
    asset: str = "BTC",
) -> str:
    asset_label = (asset or "BTC").upper()

    def _format_asset_amount(asset_name: str, amount: float) -> str:
        asset_u = str(asset_name or "").upper()

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

    amount_text = _format_asset_amount(asset_label, btc_amount)

    base = (
        f"{header} #{order_id}\n\n"
        f"Пользователь: {user_mention}\n"
        f"К выдаче: {amount_text} {asset_label}\n"
        f"К оплате: {total_rub}₽"
    )
    if canceled:
        return base + "\n\n⛔ <i>Заявка отменена.</i>"
    return base

def _resolve_order_asset(order_id: int, order: Optional[Dict[str, Any]] = None) -> str:
    """
    Определить asset заявки.

    Порядок:
      1) runtime-кэш ORDER_ASSETS;
      2) comment из заявки/БД вида: "по P2P (XMR)";
      3) BTC как безопасный fallback.
    """
    try:
        oid = int(order_id)
    except Exception:
        oid = 0

    cached = str(ORDER_ASSETS.get(oid, "") or "").strip().upper()
    if cached in {"BTC", "LTC", "USDT", "XMR"}:
        return cached

    comment = ""
    if isinstance(order, dict):
        comment = str(order.get("comment") or "").strip()

    if comment:
        m = ORDER_ASSET_COMMENT_RE.search(comment)
        if m:
            asset = str(m.group(1) or "").upper()
            if oid:
                ORDER_ASSETS[oid] = asset
            return asset

    return "BTC"


async def _get_order_by_id(order_id: int) -> Optional[Dict[str, Any]]:
    db = await get_db()
    try:
        cur = await db.execute(
            """
            SELECT
                order_id,
                user_id,
                operator_id,
                btc_amount,
                rub_amount,
                total_rub,
                wallet,
                status,
                payment_method,
                comment
            FROM p2p_orders
            WHERE order_id = ?
            LIMIT 1
            """,
            (int(order_id),),
        )
        row = await cur.fetchone()
    except Exception:
        return None

    if not row:
        return None

    order = {
        "order_id": int(row[0]),
        "user_id": int(row[1]),
        "operator_id": int(row[2] or 0),
        "btc_amount": float(row[3] or 0),
        "rub_amount": float(row[4] or 0),
        "total_rub": int(row[5] or 0),
        "wallet": row[6] or "",
        "status": row[7] or "",
        "payment_method": row[8] or "",
        "comment": row[9] or "",
    }

    order["asset"] = _resolve_order_asset(order["order_id"], order)
    return order


async def _finalize_order(
    user_id: int,
    btc_amt: float,
    rub_amount: float,
    total: int,
    wallet: str,
    bot: Bot,
    asset: str = "BTC",
    *,
    notify_ops: bool = True,
    notify_user: bool = True,
) -> int:
    asset = (asset or "BTC").upper()

    try:
        existing = await get_pending_order(user_id)
    except Exception:
        existing = None

    if existing and (existing.get("status") == "pending"):
        try:
            kb = InlineKeyboardMarkup()
            kb.add(
                InlineKeyboardButton(
                    "🚫 Отменить прошлую заявку",
                    callback_data=f"cancel_buy:{existing['order_id']}",
                )
            )
            await bot.send_message(
                user_id,
                "⚠️ У вас уже есть активная заявка. Сначала отмените/закройте её, затем создавайте новую.",
                reply_markup=kb,
            )
        except Exception:
            logger.exception("Не удалось уведомить пользователя о существующей активной заявке")
        return int(existing["order_id"])

    try:
        order_id = await save_p2p_order(
            user_id=user_id,
            operator_id=0,
            btc_amount=btc_amt,
            rub_amount=rub_amount,
            total_rub=total,
            wallet=wallet,
            comment=f"по P2P ({asset})",
        )
    except Exception:
        logger.exception("Ошибка при сохранении заявки, проверяю pending у пользователя %s", user_id)
        existing = await get_pending_order(user_id)
        if existing:
            return int(existing["order_id"])
        raise

    ORDER_ASSETS[int(order_id)] = asset

    if notify_ops:
        user_mention = await _user_mention(bot, user_id)
        from handlers.common import active_mc_sessions

        ops: List[int] = []
        all_users = await get_all_users()

        for u in all_users:
            role = str(u.get("role") or "").strip().lower()
            tid = u.get("telegram_id")

            try:
                tid = int(tid)
            except Exception:
                continue

            if tid <= 0:
                continue

            if role == "mastercard":
                try:
                    if await is_mastercard_balance_correction_active(tid):
                        continue
                except Exception:
                    logger.exception("Не удалось проверить коррекцию баланса Mastercard для %s", tid)
                    continue
                ops.append(tid)
            elif role == "admin":
                ops.append(tid)

        pending_operator_messages[user_id] = []

        text_for_ops = _format_operator_card(
            order_id=int(order_id),
            user_mention=user_mention,
            btc_amount=float(btc_amt),
            total_rub=int(total),
            asset=asset,
        )

        for op in ops:
            try:
                sent = await bot.send_message(
                    op,
                    text_for_ops,
                    parse_mode="HTML",
                    reply_markup=operator_keyboard(user_id, int(order_id)),
                )
                pending_operator_messages[user_id].append((sent.chat.id, sent.message_id))

                try:
                    await save_operator_notification(
                        order_id=int(order_id),
                        user_id=int(user_id),
                        operator_id=int(op),
                        chat_id=int(sent.chat.id),
                        message_id=int(sent.message_id),
                    )
                except Exception:
                    logger.exception("Не удалось сохранить уведомление Mastercard по заявке #%s", order_id)
            except (ChatNotFound, BotBlocked, CantInitiateConversation, Unauthorized) as e:
                logger.warning("Пропущен оператор %s: %s", op, e)
            except Exception:
                logger.exception("Не удалось отправить нотификацию оператору %s", op)
    else:
        pending_operator_messages[user_id] = []

    if notify_user:
        try:
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(InlineKeyboardButton("🚫 Отмена", callback_data=f"cancel_buy:{order_id}"))

            sent_user = await bot.send_message(
                user_id,
                "⚠️ Заявка создана! Ожидайте свободного оператора...\n\n"
                ,
                reply_markup=kb,
            )
            pending_buy_messages[user_id] = (sent_user.chat.id, sent_user.message_id)
        except Exception:
            logger.exception("Не удалось отправить пользователю подтверждение заявки")

    return int(order_id)

async def _notify_ops_paycore_paid(bot: Bot, order: Dict[str, Any], status_data: Dict[str, Any]) -> None:
    user_id = int(order["user_id"])
    order_id = int(order["order_id"])
    user_mention = await _user_mention(bot, user_id)
    asset = _resolve_order_asset(order_id, order)

    text = _format_operator_card(
        order_id=order_id,
        user_mention=user_mention,
        btc_amount=float(order.get("btc_amount", 0) or 0),
        total_rub=int(order.get("total_rub", 0) or 0),
        header="✅ Paycore оплатил",
        asset=asset,
    )

    st = status_data.get("status")
    amt = status_data.get("amount")
    text += f"\n\nPaycore status: <b>{html.escape(str(st))}</b>\namount: <b>{html.escape(str(amt))}</b>"

    ops: List[int] = []
    seen: set[int] = set()
    all_users = await get_all_users()

    for u in all_users:
        role = str(u.get("role") or "").strip().lower()
        tid = u.get("telegram_id")

        try:
            tid = int(tid)
        except Exception:
            continue

        if tid <= 0 or tid in seen:
            continue

        if role == "mastercard":
            try:
                if await is_mastercard_balance_correction_active(tid):
                    continue
            except Exception:
                logger.exception("Не удалось проверить коррекцию баланса Mastercard для %s", tid)
                continue
            seen.add(tid)
            ops.append(tid)
        elif role == "admin":
            seen.add(tid)
            ops.append(tid)

    pending_operator_messages[user_id] = []

    for op in ops:
        with suppress(Exception):
            sent = await bot.send_message(
                op,
                text,
                parse_mode="HTML",
                reply_markup=operator_keyboard(user_id, order_id),
            )
            pending_operator_messages[user_id].append((sent.chat.id, sent.message_id))
            with suppress(Exception):
                await save_operator_notification(
                    order_id=int(order_id),
                    user_id=int(user_id),
                    operator_id=int(op),
                    chat_id=int(sent.chat.id),
                    message_id=int(sent.message_id),
                )


async def _edit_operator_cards_to_canceled(bot: Bot, user_id: int, order: Dict[str, Any]) -> None:
    try:
        order_id = int(order.get("order_id") or 0)
    except Exception:
        order_id = 0
    if not order_id:
        return

    try:
        btc_amount = float(order.get("btc_amount", 0) or 0)
    except Exception:
        btc_amount = 0.0

    try:
        total_rub = int(order.get("total_rub", 0) or 0)
    except Exception:
        total_rub = 0

    try:
        user_mention = await _user_mention(bot, user_id)
    except Exception:
        user_mention = f'<a href="tg://user?id={user_id}">{user_id}</a>'

    asset = _resolve_order_asset(order_id, order)
    text_canceled = _format_operator_card(
        order_id=order_id,
        user_mention=user_mention,
        btc_amount=btc_amount,
        total_rub=total_rub,
        canceled=True,
        asset=asset,
    )

    async def _close_message(chat_id: int, message_id: int) -> bool:
        with suppress(Exception):
            await bot.delete_message(chat_id, message_id)
            return True

        with suppress(Exception):
            await bot.edit_message_text(
                text=text_canceled,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML",
                reply_markup=None,
                disable_web_page_preview=True,
            )
            return True

        with suppress(Exception):
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
            return True

        return False

    cached_msgs = pending_operator_messages.pop(user_id, []) or []
    for chat_id, msg_id in cached_msgs:
        with suppress(Exception):
            await _close_message(int(chat_id), int(msg_id))

    try:
        db = await get_db()
    except Exception:
        logger.exception("Failed to get_db in _edit_operator_cards_to_canceled (order_id=%s)", order_id)
        return

    with suppress(Exception):
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS p2p_operator_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id    INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                message_id  INTEGER NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await db.commit()

    try:
        cur = await db.execute(
            "SELECT id, chat_id, message_id FROM p2p_operator_messages WHERE order_id = ?",
            (order_id,),
        )
        rows = await cur.fetchall() or []
    except Exception:
        rows = []

    for row in rows:
        try:
            rec_id = int(row[0])
            chat_id = int(row[1])
            msg_id = int(row[2])
        except Exception:
            continue

        closed = False
        with suppress(Exception):
            closed = await _close_message(chat_id, msg_id)

        if closed:
            with suppress(Exception):
                await db.execute("DELETE FROM p2p_operator_messages WHERE id = ?", (rec_id,))
                await db.commit()

    with suppress(Exception):
        await db.execute("DELETE FROM p2p_operator_messages WHERE order_id = ?", (order_id,))
        await db.commit()


async def _ensure_paycore_table() -> None:
    db = await get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS paycore_transactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id         INTEGER NOT NULL,
            transaction_id   TEXT    NOT NULL,
            pay_url          TEXT    NOT NULL,
            last_status_json TEXT,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    await db.commit()


async def _save_paycore_transaction(order_id: int, transaction_id: str, pay_url: str) -> None:
    db = await get_db()
    await _ensure_paycore_table()
    await db.execute(
        "INSERT INTO paycore_transactions (order_id, transaction_id, pay_url) VALUES (?, ?, ?)",
        (order_id, transaction_id, pay_url),
    )
    await db.commit()


async def _get_paycore_transaction(order_id: int) -> Optional[Dict[str, str]]:
    db = await get_db()
    await _ensure_paycore_table()
    cur = await db.execute(
        "SELECT transaction_id, pay_url, last_status_json FROM paycore_transactions "
        "WHERE order_id = ? ORDER BY id DESC LIMIT 1",
        (order_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {"transaction_id": row[0], "pay_url": row[1], "last_status_json": row[2] or ""}


async def _update_paycore_status(order_id: int, status_json: str) -> None:
    db = await get_db()
    await _ensure_paycore_table()
    await db.execute(
        "UPDATE paycore_transactions SET last_status_json = ?, updated_at = datetime('now') WHERE order_id = ?",
        (status_json, order_id),
    )
    await db.commit()


def _cancel_paycore_timers(order_id: int) -> None:
    tasks = PAYCORE_ORDER_TIMERS.pop(int(order_id), None)
    if not tasks:
        return
    for t in tasks.values():
        with suppress(Exception):
            if t and not t.done():
                t.cancel()


def _cancel_paycore_status_task(order_id: int) -> None:
    oid = int(order_id)
    task = PAYCORE_STATUS_TASKS.get(oid)
    if not task:
        return
    with suppress(KeyError):
        del PAYCORE_STATUS_TASKS[oid]
    with suppress(Exception):
        if not task.done():
            task.cancel()


async def _vidrapay_is_marked_paid(order_id: int) -> bool:
    """
    True, если пользователь уже нажал «Оплатил» на странице VidraPay/в Telegram.

    Таймеры оплаты живут в памяти процесса бота, а отметка оплаты может прийти
    из web-процесса. Поэтому проверяем не только in-memory задачи, но и БД.
    """
    try:
        db = await get_db()
        cur = await db.execute(
            """
            SELECT 1
              FROM p2p_order_actions
             WHERE order_id = ?
               AND action IN (
                   'operator_paid_notify_web',
                   'operator_paid_notify',
                   'user_paid_status_card',
                   'user_exchange_status_card'
               )
               AND status IN ('claimed', 'sent')
             LIMIT 1
            """,
            (int(order_id),),
        )
        row = await cur.fetchone()
        with suppress(Exception):
            await cur.close()
        return bool(row)
    except Exception:
        logger.exception("Failed to check VidraPay paid marker for order_id=%s", order_id)
        return False


async def _schedule_paycore_deadline(bot: Bot, user_id: int, order_id: int) -> None:
    async def _remind():
        await asyncio.sleep(11 * 60)
        try:
            db = await get_db()
            cur = await db.execute("SELECT status FROM p2p_orders WHERE order_id=? LIMIT 1", (order_id,))
            row = await cur.fetchone()
            if not row or row[0] != "pending":
                return
            if await _vidrapay_is_marked_paid(order_id):
                return
        except Exception:
            return

        try:
            sent = await bot.send_message(
                user_id,
                "⏳ Осталось <b>5 минут</b> на оплату. Если не успеете — заявка будет отменена автоматически.",
                parse_mode="HTML",
            )
        except Exception:
            return

        await asyncio.sleep(20)
        with suppress(Exception):
            await bot.delete_message(sent.chat.id, sent.message_id)

    async def _autocancel():
        await asyncio.sleep(16 * 60)
        try:
            db = await get_db()
            cur = await db.execute("SELECT status FROM p2p_orders WHERE order_id=? LIMIT 1", (order_id,))
            row = await cur.fetchone()
            if not row or row[0] != "pending":
                return
            if await _vidrapay_is_marked_paid(order_id):
                return
            await db.execute("UPDATE p2p_orders SET status='canceled' WHERE order_id=?", (order_id,))
            await db.commit()
        except Exception:
            return

        with suppress(Exception):
            _cancel_paycore_status_task(order_id)

        order = await _get_order_by_id(order_id)

        if user_id in pending_buy_messages:
            chat_id, msg_id = pending_buy_messages.pop(user_id)
            with suppress(Exception):
                await bot.delete_message(int(chat_id), int(msg_id))

        with suppress(Exception):
            db = await get_db()
            await db.execute(
                "UPDATE p2p_vidrapay_messages SET deleted_at=datetime('now') WHERE order_id=?",
                (int(order_id),),
            )
            await db.commit()

        if order:
            with suppress(Exception):
                await _edit_operator_cards_to_canceled(bot, user_id, order)

        with suppress(Exception):
            await bot.send_message(user_id, "⛔ Время на оплату истекло. Заявка отменена автоматически.")

        with suppress(Exception):
            await send_welcome(bot, user_id)

    PAYCORE_ORDER_TIMERS[order_id] = {
        "remind": asyncio.create_task(_remind()),
        "autocancel": asyncio.create_task(_autocancel()),
    }


async def _paycore_watch_and_autostart(
    bot: Bot,
    *,
    order_id: int,
    user_id: int,
    transaction_id: str,
    timeout_sec: int = 10 * 60,
    poll_interval_sec: int = 10,
) -> None:
    """
    Старое имя функции оставлено специально, чтобы минимально трогать старую архитектуру.

    Теперь внутри это не Paycore, а Nirvana ТрансМежбанк:
    transaction_id = client_id Nirvana.
    """
    started = asyncio.get_running_loop().time()
    client_id = str(transaction_id or "").strip()

    async def _admin_ids() -> List[int]:
        try:
            all_users = await get_all_users()
        except Exception:
            return []

        ids: List[int] = []
        for u in all_users or []:
            try:
                if u.get("role") == "Admin" and isinstance(u.get("telegram_id"), int):
                    ids.append(int(u["telegram_id"]))
            except Exception:
                continue
        return ids

    async def _notify_admin(text: str) -> None:
        admin_ids = await _admin_ids()
        if not admin_ids:
            return

        for aid in admin_ids:
            with suppress(Exception):
                await bot.send_message(
                    aid,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

    async def _cleanup_user_ui_for_success(*, oid: int, uid: int) -> None:
        meta = PAYCORE_WATCH_META.pop(int(oid), None) or {}
        wait_chat_id = meta.get("wait_chat_id")
        wait_message_id = meta.get("wait_message_id")
        if wait_chat_id and wait_message_id:
            with suppress(Exception):
                await bot.delete_message(int(wait_chat_id), int(wait_message_id))

        if uid in pending_buy_messages:
            chat_id, msg_id = pending_buy_messages.pop(uid)
            with suppress(Exception):
                await bot.delete_message(int(chat_id), int(msg_id))

    client = NirvanaClient(
        api_public=getattr(settings, "nirvana_api_public", ""),
        api_private=getattr(settings, "nirvana_api_private", ""),
        base_url=getattr(settings, "nirvana_base_url", "https://api.nirvanapay.pro"),
        timeout_sec=int(getattr(settings, "nirvana_timeout_sec", 20)),
    )

    def _status_from_payload(payload: Dict[str, Any]) -> str:
        status = payload.get("status")
        if status is None and isinstance(payload.get("data"), dict):
            status = payload["data"].get("status")
        return str(status or "").upper().strip()

    def _amount_fiat_received(payload: Dict[str, Any]) -> Optional[float]:
        value = payload.get("amountFiatReceived")
        if value is None and isinstance(payload.get("data"), dict):
            value = payload["data"].get("amountFiatReceived")

        if value is None:
            return None

        try:
            return float(value)
        except Exception:
            return None

    try:
        while True:
            if asyncio.get_running_loop().time() - started > timeout_sec:
                with suppress(Exception):
                    await bot.send_message(
                        user_id,
                        "⏳ Время ожидания оплаты истекло.\n"
                        "Если вы уже оплатили — напишите в поддержку и укажите номер заявки.",
                    )

                order = await _get_order_by_id(order_id)
                asset = _resolve_order_asset(order_id, order)

                user_mention = await _user_mention(bot, user_id)
                await _notify_admin(
                    "⚠️ <b>P2P: истекло время ожидания Nirvana ТрансМежбанк</b>\n\n"
                    f"Заявка: <b>#{order_id}</b>\n"
                    f"Пользователь: {user_mention}\n"
                    f"Asset: <b>{html.escape(str(asset))}</b>\n"
                    f"Client ID: <code>{html.escape(client_id)}</code>\n"
                    f"Статус заявки: <b>{html.escape(str((order or {}).get('status') or ''))}</b>"
                )
                return

            order = await _get_order_by_id(order_id)
            if not order:
                return

            if int(order.get("user_id") or 0) != int(user_id):
                return

            st_order = str(order.get("status") or "").lower().strip()
            if st_order in ("canceled", "cancelled", "done", "completed", "finished", "success"):
                return

            try:
                status_data = await client.get_status(client_id=client_id)
                status = _status_from_payload(status_data)
                amount_fiat_received = _amount_fiat_received(status_data)

                with suppress(Exception):
                    await update_nirvana_order_status(
                        client_id=client_id,
                        status=status or "UNKNOWN",
                        amount_fiat_received=amount_fiat_received,
                        raw_status_response=json.dumps(status_data, ensure_ascii=False),
                    )

            except asyncio.CancelledError:
                raise

            except NirvanaAPIError as e:
                logger.warning("Nirvana get_status error order_id=%s client_id=%s: %s", order_id, client_id, e)
                await asyncio.sleep(poll_interval_sec)
                continue

            except Exception as e:
                logger.exception("Unexpected Nirvana status polling error order_id=%s", order_id)
                user_mention = await _user_mention(bot, user_id)
                await _notify_admin(
                    "❌ <b>P2P: ошибка проверки Nirvana ТрансМежбанк</b>\n\n"
                    f"Заявка: <b>#{order_id}</b>\n"
                    f"Пользователь: {user_mention}\n"
                    f"Client ID: <code>{html.escape(client_id)}</code>\n"
                    f"Ошибка: <code>{html.escape(str(e))}</code>"
                )
                await asyncio.sleep(poll_interval_sec)
                continue

            if status == "SUCCESS":
                nirvana_order = await get_nirvana_order_by_client_id(client_id)
                if nirvana_order and int(nirvana_order.get("processed_success") or 0) == 1:
                    return

                with suppress(Exception):
                    await mark_nirvana_order_success_processed(client_id)

                with suppress(Exception):
                    _cancel_paycore_timers(order_id)

                with suppress(Exception):
                    await _cleanup_user_ui_for_success(oid=int(order_id), uid=int(user_id))

                try:
                    from handlers.chat.instruction import start_exchange_from_p2p  # type: ignore

                    try:
                        op_id = int(order.get("operator_id") or 0) or None
                    except Exception:
                        op_id = None

                    await start_exchange_from_p2p(
                        bot=bot,
                        p2p=order,
                        operator_id=op_id,
                    )
                    return

                except Exception as e:
                    logger.exception("Nirvana success received, but start_exchange_from_p2p failed")

                    asset = _resolve_order_asset(order_id, order)
                    user_mention = await _user_mention(bot, user_id)

                    await _notify_admin(
                        "❌ <b>P2P: оплата Nirvana прошла, но обмен не запустился</b>\n\n"
                        f"Заявка: <b>#{order_id}</b>\n"
                        f"Пользователь: {user_mention}\n"
                        f"Asset: <b>{html.escape(str(asset))}</b>\n"
                        f"Client ID: <code>{html.escape(client_id)}</code>\n"
                        f"Ошибка: <code>{html.escape(str(e))}</code>"
                    )

                    with suppress(Exception):
                        await bot.send_message(
                            user_id,
                            "✅ Оплата подтверждена.\n"
                            "Я уведомил оператора для ручного запуска обмена.",
                        )
                    return

            if status in ("ERROR", "CANCELED", "CANCELLED"):
                with suppress(Exception):
                    await bot.send_message(
                        user_id,
                        "❌ Платёж не прошёл или был отменён.\n"
                        "Создайте новую заявку или обратитесь в поддержку.",
                    )

                user_mention = await _user_mention(bot, user_id)
                await _notify_admin(
                    "❌ <b>P2P: Nirvana вернула ошибочный статус</b>\n\n"
                    f"Заявка: <b>#{order_id}</b>\n"
                    f"Пользователь: {user_mention}\n"
                    f"Client ID: <code>{html.escape(client_id)}</code>\n"
                    f"Статус: <b>{html.escape(status)}</b>"
                )
                return

            await asyncio.sleep(poll_interval_sec)

    finally:
        with suppress(KeyError):
            del PAYCORE_STATUS_TASKS[int(order_id)]


async def _start_asset_flow(
    callback: types.CallbackQuery,
    state: FSMContext,
    *,
    asset: str,
    paycore_only: bool = False,
    binance_mode: bool = False,
) -> None:
    user_id = callback.from_user.id

    # Сначала проверяем, нет ли уже активной заявки
    try:
        existing = await get_pending_order(user_id)
    except Exception:
        existing = None

    if existing and str(existing.get("status") or "").lower() == "pending":
        order_id = int(existing["order_id"])

        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton(
                "🚫 Отменить прошлую заявку",
                callback_data=f"cancel_buy:{order_id}",
            )
        )

        with suppress(Exception):
            await callback.message.delete()

        await callback.bot.send_message(
            user_id,
            "⚠️ У вас уже есть активная заявка. Сначала отмените/закройте её, затем создавайте новую.",
            reply_markup=kb,
        )
        return

    with suppress(Exception):
        await callback.message.delete()

    asset = (asset or "BTC").upper()
    await state.update_data(
        asset=asset,
        paycore_only=bool(paycore_only),
        binance_mode=bool(binance_mode),
    )

    if asset == "LTC":
        text = AMOUNT_INPUT_TEMPLATE.format(asset=asset, example_asset="1.35", example_rub="2500")
    elif asset == "XMR":
        text = AMOUNT_INPUT_TEMPLATE.format(asset=asset, example_asset="0.50", example_rub="2500")
    elif asset == "USDT":
        text = (
            "Укажи сумму в <b>RUB</b>,\n"
            "которую хочешь получить на свой кошелек <b>USDT (TRC20)</b>\n\n"
            "‼️<u>Учитывай колебания курса и указывай сумму с запасом</u>‼️\n\n"
            "Пример: <b>2500</b>"
        )
    else:
        text = AMOUNT_INPUT_TEMPLATE.format(asset=asset, example_asset="0.001", example_rub="2500")

    sent_prompt = await callback.bot.send_message(
        user_id,
        text,
        parse_mode="HTML",
        reply_markup=cancel_buy_keyboard(),
    )
    await state.update_data(amount_prompt_msg_id=sent_prompt.message_id)
    await P2PStates.amount.set()


async def _paycore_start_phone_collection(bot: Bot, state: FSMContext, user_id: int) -> None:
    with suppress(Exception):
        await state.update_data(paycore_phone=None)

    data = await state.get_data()

    # удаляем старые сообщения (если были)
    consent_msg_id = data.get("paycore_consent_msg_id")
    if consent_msg_id:
        with suppress(Exception):
            await bot.delete_message(user_id, int(consent_msg_id))
        with suppress(Exception):
            await state.update_data(paycore_consent_msg_id=None)

    old_prompt_id = data.get("paycore_phone_prompt_msg_id")
    if old_prompt_id:
        with suppress(Exception):
            await bot.delete_message(user_id, int(old_prompt_id))
        with suppress(Exception):
            await state.update_data(paycore_phone_prompt_msg_id=None)

    # теперь просто просим ввести номер текстом — без кнопки "поделиться контактом"
    sent = await bot.send_message(
        user_id,
        "📱 Введите номер телефона, с которого будет выполнен перевод.\n\n"
       ,
    )
    await state.update_data(paycore_phone_prompt_msg_id=sent.message_id)
    await P2PStates.paycore_phone.set()


async def start_p2p(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    data_raw = callback.data or ""
    if data_raw == Callback.P2P_ASSISTANT:
        asset = "BTC"
    elif data_raw == Callback.P2P_ASSISTANT_LTC:
        asset = "LTC"
    elif data_raw == Callback.P2P_ASSISTANT_USDT:
        asset = "USDT"
    elif data_raw == Callback.P2P_ASSISTANT_XMR:
        asset = "XMR"
    else:
        asset = "BTC"

    # Обычные заявки всегда ведём по стандартной P2P-ветке.
    await state.update_data(binance_mode=False, binance_verified=False, paycore_only=False)
    await _start_asset_flow(
        callback=callback,
        state=state,
        asset=asset,
        paycore_only=False,
        binance_mode=False,
    )


async def handle_amount(message: types.Message, state: FSMContext) -> None:
    retry_kb = cancel_buy_keyboard()
    raw = (message.text or "").strip()

    async def _auto_delete_message(chat_id: int, message_id: int, delay: int = EPHEMERAL_DELETE_SEC) -> None:
        try:
            await asyncio.sleep(delay)
            await message.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    async def _ephemeral_warning(text: str) -> None:
        with suppress(Exception):
            await message.delete()
        try:
            sent = await message.bot.send_message(chat_id=message.chat.id, text=text)
            asyncio.create_task(_auto_delete_message(sent.chat.id, sent.message_id, delay=EPHEMERAL_DELETE_SEC))
        except Exception:
            pass

    if not raw:
        await message.answer(
            "⚠️ Введите сумму цифрами. Пример:\n`2500`",
            parse_mode="Markdown",
            reply_markup=retry_kb,
        )
        return

    txt = _sanitize_number_text(raw)
    if not _is_valid_decimal(txt):
        await message.answer(
            "⚠️ Разрешены только цифры и одна точка. Примеры:\n`2500`, `2500.50`",
            parse_mode="Markdown",
            reply_markup=retry_kb,
        )
        return

    if "." in txt:
        _, frac_part = txt.split(".", 1)
        if len(frac_part) > 8:
            await message.answer(
                "⚠️ Максимум 8 знаков после точки. Пример: `2500.50`",
                parse_mode="Markdown",
                reply_markup=retry_kb,
            )
            return

    if len(txt.split(".", 1)[0]) > 12:
        await message.answer("⚠️ Слишком большая сумма. Введите поменьше.", reply_markup=retry_kb)
        return

    try:
        val = float(txt)
    except ValueError:
        await message.answer(
            "⚠️ Не получилось распознать число. Примеры: `2500`, `2500.50`",
            parse_mode="Markdown",
            reply_markup=retry_kb,
        )
        return

    if val <= 0:
        await message.answer("⚠️ Сумма должна быть положительной.", reply_markup=retry_kb)
        return

    base_rate = await get_usd_rub()
    if base_rate is None or base_rate <= 0:
        await message.answer("⚠️ Не удалось получить курс USDT→₽.", reply_markup=retry_kb)
        return

    data_state = await state.get_data()
    asset = (data_state.get("asset") or "BTC").upper()
    if asset not in ("BTC", "LTC", "USDT", "XMR"):
        asset = "BTC"

    binance_mode = bool(data_state.get("binance_mode"))
    paycore_only = bool(data_state.get("paycore_only"))
    is_binance_flow = bool(binance_mode and paycore_only)

    if asset == "USDT":
        rub_amount = float(val)

        min_rub_exchange = 6000.0 if asset == "XMR" else MIN_RUB_EXCHANGE

        if rub_amount < min_rub_exchange:
            await _ephemeral_warning(f"⚠️ Минимум — {int(min_rub_exchange)} ₽.")
            return

        usdt_int = int(math.floor(rub_amount / float(base_rate)))
        if usdt_int <= 0:
            await message.answer("⚠️ Сумма слишком мала для расчёта USDT.", reply_markup=retry_kb)
            return

        if is_binance_flow:
            commission = BINANCE_FIXED_COMMISSION
        else:
            commission = await get_user_commission(message.from_user.id)
            if commission is None:
                commission = helpers.get_order_commission_percent(rub_amount)

        total = math.ceil(rub_amount * (1 + float(commission) / 100) / 100) * 100

        with suppress(Exception):
            prompt_msg_id = (data_state or {}).get("amount_prompt_msg_id")
            if prompt_msg_id:
                await message.bot.delete_message(message.chat.id, int(prompt_msg_id))
                await state.update_data(amount_prompt_msg_id=None)

        with suppress(Exception):
            await message.bot.delete_message(message.chat.id, message.message_id)

        await state.update_data(
            asset="USDT",
            btc_amount=float(usdt_int),
            rub_amount=float(rub_amount),
            total_min=int(total),
            amount_input_mode="RUB",
        )

        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("✅ Подтвердить", callback_data=Callback.CONFIRM_ORDER),
            InlineKeyboardButton("↩️ Назад", callback_data="back"),
        ).add(InlineKeyboardButton("🚫 Отмена", callback_data=Callback.CANCEL_BUY))

        await message.answer(
            f"USDT: `1.0000$`\nКурс USDT→₽: `{base_rate:.2f}`\n\n"
            f"Вы получите: `{int(usdt_int)} USDT`\n*К оплате*: `{total:.0f}₽`",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        await P2PStates.confirm.set()
        return

    user_id = message.from_user.id

    if asset == "BTC":
        display_asset_usd = await get_btc_price()
    elif asset == "LTC":
        display_asset_usd = await get_binance_ticker_price("LTCUSDT")
    elif asset == "XMR":
        display_asset_usd = await get_binance_ticker_price("XMRUSDT")
        if not display_asset_usd:
            display_asset_usd = await get_binance_ticker_price("XMRUSDC")
    else:
        display_asset_usd = None

    api_key = getattr(settings, "FF_API_KEY", None)
    api_secret = getattr(settings, "FF_API_SECRET", None)
    usdt_ccy = getattr(settings, "FF_USDT_CCY", "USDTTRC")

    async def _usdt_from_btc_ff_float(btc_amount: float) -> Optional[float]:
        if not (api_key and api_secret):
            return None
        if btc_amount <= 0:
            return 0.0

        est_usdt = (display_asset_usd or 0) * btc_amount
        if est_usdt <= 0:
            est_usdt = 1.0

        low = 0.0
        high = max(est_usdt * 1.25, 5.0)
        for _ in range(4):
            try:
                req_btc = await btc_required_for_usdt_ff_float(
                    api_key=api_key,
                    api_secret=api_secret,
                    usdt_ccy=usdt_ccy,
                    usdt_target=float(high),
                )
            except Exception:
                req_btc = None
            if not req_btc:
                break
            if req_btc > btc_amount:
                break
            high *= 2.0

        best = 0.0
        for _ in range(18):
            mid = 0.5 * (low + high)
            try:
                req_btc = await btc_required_for_usdt_ff_float(
                    api_key=api_key,
                    api_secret=api_secret,
                    usdt_ccy=usdt_ccy,
                    usdt_target=float(mid),
                )
            except Exception:
                req_btc = None
            if not req_btc:
                break
            if req_btc <= btc_amount:
                best = mid
                low = mid
            else:
                high = mid
            if high - low < 1e-6:
                break
        return best if best > 0 else None

    if asset == "BTC":
        is_crypto_input = val < 1
    elif asset == "XMR":
        is_crypto_input = ("." in txt) or val <= 50
    else:
        is_crypto_input = val <= 10

    amount_input_mode = "ASSET" if is_crypto_input else "RUB"

    if is_crypto_input:
        asset_amt = float(val)
        if asset == "BTC":
            usdt_out_precise = None
            with suppress(Exception):
                usdt_out_precise = await _usdt_from_btc_ff_float(float(asset_amt))
            if usdt_out_precise and usdt_out_precise > 0:
                rub_amount = usdt_out_precise * base_rate
            else:
                if not display_asset_usd:
                    await message.answer("⚠️ Не удалось получить цену BTC.", reply_markup=retry_kb)
                    return
                rub_amount = asset_amt * float(display_asset_usd) * base_rate * 0.995
        else:
            if not display_asset_usd:
                await message.answer(f"⚠️ Не удалось получить цену {asset}.", reply_markup=retry_kb)
                return
            rub_amount = asset_amt * float(display_asset_usd) * base_rate * 0.995
    else:
        rub_amount = float(val)
        usdt_target = rub_amount / base_rate

        if asset == "BTC":
            asset_amt_precise = None
            if api_key and api_secret:
                with suppress(Exception):
                    asset_amt_precise = await btc_required_for_usdt_ff_float(
                        api_key=api_key,
                        api_secret=api_secret,
                        usdt_ccy=usdt_ccy,
                        usdt_target=float(usdt_target),
                    )
            if asset_amt_precise and asset_amt_precise > 0:
                asset_amt = float(asset_amt_precise)
            else:
                if not display_asset_usd:
                    await message.answer("⚠️ Не удалось получить котировку. Попробуйте позже.", reply_markup=retry_kb)
                    return
                asset_amt = (usdt_target / float(display_asset_usd)) * 1.01
        else:
            if not display_asset_usd:
                await message.answer("⚠️ Не удалось получить котировку. Попробуйте позже.", reply_markup=retry_kb)
                return
            asset_amt = (usdt_target / float(display_asset_usd)) * 1.01

    min_rub_exchange = 6000.0 if asset == "XMR" else MIN_RUB_EXCHANGE

    if rub_amount < min_rub_exchange:
        await _ephemeral_warning(f"⚠️ Минимум — {int(min_rub_exchange)} ₽.")
        return

    if is_binance_flow:
        commission = BINANCE_FIXED_COMMISSION
    else:
        commission = await get_user_commission(user_id)
        if commission is None:
            commission = helpers.get_order_commission_percent(rub_amount)

    total = math.ceil(rub_amount * (1 + float(commission) / 100) / 100) * 100

    with suppress(Exception):
        prompt_msg_id = (data_state or {}).get("amount_prompt_msg_id")
        if prompt_msg_id:
            await message.bot.delete_message(message.chat.id, int(prompt_msg_id))
            await state.update_data(amount_prompt_msg_id=None)

    with suppress(Exception):
        await message.bot.delete_message(message.chat.id, message.message_id)

    await state.update_data(
        asset=asset,
        btc_amount=float(asset_amt),
        rub_amount=float(rub_amount),
        total_min=int(total),
        amount_input_mode=amount_input_mode,
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=Callback.CONFIRM_ORDER),
        InlineKeyboardButton("↩️ Назад", callback_data="back"),
    ).add(InlineKeyboardButton("🚫 Отмена", callback_data=Callback.CANCEL_BUY))

    lines = []
    if display_asset_usd:
        lines.append(f"{asset}: `{float(display_asset_usd):.4f}$`")
    lines.append(f"Курс USDT→₽: `{base_rate:.2f}`")
    info_header = "\n".join(lines)

    if is_crypto_input:
        display_amount_str = _format_asset_amount_for_user(asset, float(asset_amt))
    else:
        display_amount_str = _format_asset_amount_for_user(asset, float(asset_amt))

    await message.answer(
        f"{info_header}\n\nВы получите: `{display_amount_str} {asset}`\n*К оплате*: `{total:.0f}₽`",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    await P2PStates.confirm.set()


async def confirm_p2p(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    user_id = callback.from_user.id
    data = await state.get_data()

    asset = (data.get("asset") or "BTC").upper()
    binance_mode = bool(data.get("binance_mode"))
    paycore_only = bool(data.get("paycore_only"))

    if binance_mode and paycore_only:
        if asset == "BTC":
            sent = await callback.bot.send_message(user_id, "📥 Введите ваш BTC-адрес:")
        elif asset == "LTC":
            sent = await callback.bot.send_message(user_id, "📥 Введите ваш LTC-адрес:")
        elif asset == "XMR":
            sent = await callback.bot.send_message(
                user_id,
                "📥 Введите ваш XMR-адрес (обычно начинается с `4...` или `8...`):",
                parse_mode="Markdown",
            )
        else:
            sent = await callback.bot.send_message(
                user_id,
                "📥 Введите ваш USDT-адрес в сети TRC20 (обычно начинается с `T...`):",
                parse_mode="Markdown",
            )
        await state.update_data(wallet_prompt_msg_id=sent.message_id)
        await P2PStates.wallet.set()
        return

    if asset == "BTC":
        saved = await get_user_btc_wallet(user_id)
        if saved:
            kb = InlineKeyboardMarkup(row_width=2).add(
                InlineKeyboardButton("✅ Использовать", callback_data="use_saved_wallet"),
                InlineKeyboardButton("❌ Ввести новый", callback_data="new_wallet"),
            )
            sent = await callback.bot.send_message(
                user_id,
                f"💼 Ваш BTC-адрес:\n<code>{saved}</code>",
                parse_mode="HTML",
                reply_markup=kb,
            )
            await state.update_data(wallet_prompt_msg_id=sent.message_id)
        else:
            sent = await callback.bot.send_message(user_id, "📥 Введите ваш BTC-адрес:")
            await state.update_data(wallet_prompt_msg_id=sent.message_id)
    elif asset == "LTC":
        sent = await callback.bot.send_message(user_id, "📥 Введите ваш LTC-адрес:")
        await state.update_data(wallet_prompt_msg_id=sent.message_id)
    elif asset == "XMR":
        sent = await callback.bot.send_message(
            user_id,
            "📥 Введите ваш XMR-адрес (обычно начинается с `4...` или `8...`):",
            parse_mode="Markdown",
        )
        await state.update_data(wallet_prompt_msg_id=sent.message_id)
    else:
        sent = await callback.bot.send_message(
            user_id,
            "📥 Введите ваш USDT-адрес в сети TRC20 (обычно начинается с `T...`):",
            parse_mode="Markdown",
        )
        await state.update_data(wallet_prompt_msg_id=sent.message_id)

    await P2PStates.wallet.set()


async def handle_wallet(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    asset = (data.get("asset") or "BTC").upper()

    raw = (message.text or "").strip()
    wallet_clean = re.sub(r"[\s\u00A0\u2007\u202F]+", "", raw)

    user_id = message.from_user.id

    is_valid, error_text = helpers.validate_wallet_for_asset(asset, wallet_clean)
    if not is_valid:
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("↩️ Ввести заново", callback_data="new_wallet"),
            InlineKeyboardButton("🚫 Отмена", callback_data=Callback.CANCEL_BUY),
        )

        if asset == "BTC":
            fallback_text = (
                "⚠️ Неверный BTC-адрес.\n\n"
                "Обычно начинается с `bc1`, `1` или `3`.\n"
                "Проверьте, что адрес вставлен полностью и без пробелов."
            )
        elif asset == "LTC":
            fallback_text = (
                "⚠️ Похоже, это некорректный LTC-адрес.\n\n"
                "Обычно начинается с `L`, `M`, `3` или `ltc1`.\n"
                "Проверьте, что адрес вставлен полностью и без пробелов."
            )
        elif asset == "XMR":
            fallback_text = (
                "⚠️ Похоже, это некорректный XMR-адрес.\n\n"
                "Обычно Monero-адрес начинается с `4...` или `8...`.\n"
                "Проверьте, что адрес вставлен полностью и без пробелов."
            )
        else:
            fallback_text = (
                "⚠️ Похоже, это некорректный USDT TRC20-адрес.\n\n"
                "Обычно начинается с `T...` и не содержит пробелов."
            )

        text = (error_text or "").strip() or fallback_text
        if not text.startswith("⚠️"):
            text = f"⚠️ {text}"

        await message.answer(text, parse_mode="Markdown", reply_markup=kb)
        return

    if asset == "BTC":
        with suppress(Exception):
            await set_user_btc_wallet(user_id, wallet_clean)

    await state.update_data(wallet=wallet_clean)

    with suppress(Exception):
        prompt_msg_id = data.get("wallet_prompt_msg_id")
        if prompt_msg_id:
            await message.bot.delete_message(message.chat.id, int(prompt_msg_id))
            await state.update_data(wallet_prompt_msg_id=None)

    with suppress(Exception):
        await message.bot.delete_message(message.chat.id, message.message_id)

    paycore_only = bool(data.get("paycore_only"))
    binance_mode = bool(data.get("binance_mode"))
    is_binance_flow = bool(binance_mode and paycore_only)

    if not all(k in data for k in ("btc_amount", "rub_amount", "total_min")):
        await message.bot.send_message(
            user_id,
            "⚠️ Не удалось найти данные заявки. Начните заново."
        )
        await state.finish()
        return

    amount_value = float(data.get("btc_amount") or 0.0)
    total_value = int(data.get("total_min") or 0)

    amount_text = f"{_format_asset_amount_for_user(asset, amount_value)} {asset}"
    if (not is_binance_flow) and total_value < NIRVANA_AUTO_MAX_RUB:
        await _paycore_create_and_send(message.bot, state, user_id, phone="")
        return

    text = (
        "✅ Проверьте данные заявки:\n\n"
        f"• Монета: <b>{html.escape(asset)}</b>\n"
        f"• К выдаче: <b>{html.escape(amount_text)}</b>\n"
        f"• К оплате: <b>{total_value}₽</b>\n"
        f"• Кошелёк: <code>{html.escape(wallet_clean)}</code>\n\n"
        "Выберите способ оплаты:"
    )

    sent = await message.bot.send_message(
        user_id,
        text,
        parse_mode="HTML",
        reply_markup=_build_payment_keyboard(paycore_only=is_binance_flow),
    )
    await state.update_data(method_prompt_msg_id=sent.message_id)
    await P2PStates.method.set()


async def use_saved_wallet(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    user_id = callback.from_user.id
    wallet = await get_user_btc_wallet(user_id)
    if not wallet:
        await callback.bot.send_message(user_id, "⚠️ Сохранённого кошелька не найдено, введите новый:")
        return

    data = await state.get_data()
    asset = (data.get("asset") or "BTC").upper()
    paycore_only = bool(data.get("paycore_only"))
    binance_mode = bool(data.get("binance_mode"))
    is_binance_flow = bool(binance_mode and paycore_only)

    if not all(k in data for k in ("btc_amount", "rub_amount", "total_min")):
        await callback.bot.send_message(user_id, "⚠️ Не удалось найти данные заявки. Начните заново.")
        await state.finish()
        return

    await state.update_data(wallet=wallet)

    amount_value = float(data.get("btc_amount") or 0.0)
    total_value = int(data.get("total_min") or 0)

    if asset == "USDT":
        amount_text = f"{int(amount_value)} {asset}"
    else:
        amount_text = f"{amount_value:.8f} {asset}"
    if (not is_binance_flow) and total_value < NIRVANA_AUTO_MAX_RUB:
        await _paycore_create_and_send(callback.bot, state, user_id, phone="")
        return


    text = (
        "✅ Проверьте данные заявки:\n\n"
        f"• Монета: <b>{html.escape(asset)}</b>\n"
        f"• К выдаче: <b>{html.escape(amount_text)}</b>\n"
        f"• К оплате: <b>{total_value}₽</b>\n"
        f"• Кошелёк: <code>{html.escape(wallet)}</code>\n\n"
        "Выберите способ оплаты:"
    )

    sent = await callback.bot.send_message(
        user_id,
        text,
        parse_mode="HTML",
        reply_markup=_build_payment_keyboard(paycore_only=is_binance_flow),
    )
    await state.update_data(method_prompt_msg_id=sent.message_id)
    await P2PStates.method.set()


async def new_wallet(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    data = await state.get_data()
    asset = (data.get("asset") or "BTC").upper()

    if asset == "LTC":
        text = "📥 Введите ваш LTC-адрес:"
    elif asset == "USDT":
        text = "📥 Введите ваш USDT-адрес в сети TRC20 (обычно начинается с `T...`):"
    elif asset == "XMR":
        text = "📥 Введите ваш XMR-адрес (обычно начинается с `4...` или `8...`):"
    else:
        text = "📥 Введите ваш BTC-адрес:"

    sent = await callback.bot.send_message(
        callback.from_user.id,
        text,
        parse_mode="Markdown",
    )
    await state.update_data(wallet_prompt_msg_id=sent.message_id)
    await P2PStates.wallet.set()



async def back_to_amount(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    await state.reset_state(with_data=False)
    data = await state.get_data()
    asset = (data.get("asset") or "BTC").upper()

    if asset == "LTC":
        text = AMOUNT_INPUT_TEMPLATE.format(asset=asset, example_asset="1.35", example_rub="2500")
    elif asset == "XMR":
        text = AMOUNT_INPUT_TEMPLATE.format(asset=asset, example_asset="0.50", example_rub="2500")
    elif asset == "USDT":
        text = (
            "Укажи сумму в <b>RUB</b>,\n"
            "которую хочешь получить на свой кошелек <b>USDT (TRC20)</b>\n\n"
            "‼️<u>Учитывай колебания курса и указывай сумму с запасом</u>‼️\n\n"
            "Пример: <b>2500</b>"
        )
    else:
        text = AMOUNT_INPUT_TEMPLATE.format(asset=asset, example_asset="0.001", example_rub="2500")

    sent_prompt = await callback.bot.send_message(
        callback.from_user.id,
        text,
        parse_mode="HTML",
        reply_markup=cancel_buy_keyboard(),
    )
    await state.update_data(amount_prompt_msg_id=sent_prompt.message_id)
    await P2PStates.amount.set()

async def cancel_p2p(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    order = await get_pending_order(user_id)
    if order:
        order_id = int(order["order_id"])

        with suppress(Exception):
            _cancel_paycore_timers(order_id)
        with suppress(Exception):
            _cancel_paycore_status_task(order_id)

        if order.get("status") != "canceled":
            db = await get_db()
            with suppress(Exception):
                await db.execute("UPDATE p2p_orders SET status='canceled' WHERE order_id=?", (order_id,))
                await db.commit()
            with suppress(Exception):
                await _edit_operator_cards_to_canceled(callback.bot, user_id, order)

        if user_id in pending_buy_messages:
            chat_id, msg_id = pending_buy_messages.pop(user_id)
            with suppress(Exception):
                await callback.bot.edit_message_reply_markup(chat_id, msg_id, None)

    with suppress(Exception):
        await state.finish()
    with suppress(Exception):
        await callback.message.delete()

    await send_welcome(callback.bot, user_id)


async def cancel_p2p_by_id(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    try:
        _, raw_id = (callback.data or "").split(":", 1)
        order_id = int(raw_id)
    except Exception:
        await cancel_p2p(callback, state)
        return

    with suppress(Exception):
        _cancel_paycore_timers(order_id)
    with suppress(Exception):
        _cancel_paycore_status_task(order_id)

    order = await _get_order_by_id(order_id)
    if not order:
        with suppress(Exception):
            await callback.bot.send_message(user_id, "⚠️ Заявка не найдена или уже закрыта.")
        with suppress(Exception):
            await state.finish()
        with suppress(Exception):
            await callback.message.delete()
        await send_welcome(callback.bot, user_id)
        return

    if int(order.get("user_id") or 0) != int(user_id):
        with suppress(Exception):
            await callback.bot.send_message(user_id, "⚠️ Это не ваша заявка.")
        return

    db = await get_db()
    with suppress(Exception):
        await db.execute("UPDATE p2p_orders SET status='canceled' WHERE order_id = ?", (order_id,))
        await db.commit()

    with suppress(Exception):
        await _edit_operator_cards_to_canceled(callback.bot, user_id, order)

    if user_id in pending_buy_messages:
        chat_id, msg_id = pending_buy_messages.pop(user_id)
        with suppress(Exception):
            await callback.bot.edit_message_reply_markup(chat_id, msg_id, None)

    with suppress(Exception):
        await state.finish()
    with suppress(Exception):
        await callback.message.delete()

    await send_welcome(callback.bot, user_id)


async def cancel_active_order(callback: types.CallbackQuery, state: FSMContext) -> None:
    with suppress(Exception):
        await callback.answer("Операция отменена")

    user_id = callback.from_user.id
    with suppress(Exception):
        await callback.message.edit_reply_markup(None)
    with suppress(Exception):
        await callback.message.delete()

    db = await get_db()
    try:
        cur = await db.execute(
            """
            SELECT order_id, btc_amount, total_rub, status
            FROM p2p_orders
            WHERE user_id = ? AND status = 'pending'
            ORDER BY datetime(created_at) DESC
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
    except Exception:
        rows = []

    if not rows:
        with suppress(Exception):
            await state.finish()
        await send_welcome(callback.bot, user_id)
        return

    order_ids = [int(r[0]) for r in rows]
    placeholders = ",".join("?" * len(order_ids))
    with suppress(Exception):
        await db.execute(f"UPDATE p2p_orders SET status='canceled' WHERE order_id IN ({placeholders})", tuple(order_ids))
        await db.commit()

    for oid in order_ids:
        with suppress(Exception):
            _cancel_paycore_timers(oid)
        with suppress(Exception):
            _cancel_paycore_status_task(oid)

    for r in rows:
        od = {"order_id": int(r[0]), "btc_amount": float(r[1] or 0), "total_rub": int(r[2] or 0), "status": r[3] or ""}
        with suppress(Exception):
            await _edit_operator_cards_to_canceled(callback.bot, user_id, od)

    if user_id in pending_buy_messages:
        chat_id, msg_id = pending_buy_messages.pop(user_id)
        with suppress(Exception):
            await callback.bot.edit_message_reply_markup(chat_id, msg_id, None)

    with suppress(Exception):
        await state.finish()

    await send_welcome(callback.bot, user_id)


async def pay_card_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    user_id = callback.from_user.id
    data = await state.get_data()
    if not all(k in data for k in ("btc_amount", "rub_amount", "total_min")):
        await callback.bot.send_message(user_id, "⚠️ Не удалось найти данные заявки. Начните заново.")
        await state.finish()
        return

    asset = (data.get("asset") or "BTC").upper()
    wallet = data.get("wallet") or (await get_user_btc_wallet(user_id) if asset == "BTC" else None)
    if not wallet:
        await callback.bot.send_message(user_id, "⚠️ Не найден адрес для зачисления. Начните заново.")
        await state.finish()
        return

    order_id = await _finalize_order(
        user_id=user_id,
        btc_amt=float(data["btc_amount"]),
        rub_amount=float(data["rub_amount"]),
        total=int(data["total_min"]),
        wallet=wallet,
        bot=callback.bot,
        asset=asset,
    )

    db = await get_db()
    with suppress(Exception):
        await db.execute("UPDATE p2p_orders SET payment_method=? WHERE order_id=?", ("card", order_id))
        await db.commit()

    await state.finish()


async def pay_sbp_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    user_id = callback.from_user.id
    data = await state.get_data()
    if not all(k in data for k in ("btc_amount", "rub_amount", "total_min")):
        await callback.bot.send_message(user_id, "⚠️ Не удалось найти данные заявки. Начните заново.")
        await state.finish()
        return

    asset = (data.get("asset") or "BTC").upper()
    wallet = data.get("wallet") or (await get_user_btc_wallet(user_id) if asset == "BTC" else None)
    if not wallet:
        await callback.bot.send_message(user_id, "⚠️ Не найден адрес для зачисления. Начните заново.")
        await state.finish()
        return

    order_id = await _finalize_order(
        user_id=user_id,
        btc_amt=float(data["btc_amount"]),
        rub_amount=float(data["rub_amount"]),
        total=int(data["total_min"]),
        wallet=wallet,
        bot=callback.bot,
        asset=asset,
    )

    db = await get_db()
    with suppress(Exception):
        await db.execute("UPDATE p2p_orders SET payment_method=? WHERE order_id=?", ("sbp", order_id))
        await db.commit()

    await state.finish()


async def pay_paycore_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    user_id = callback.from_user.id

    data = await state.get_data()
    if not all(k in data for k in ("btc_amount", "rub_amount", "total_min")):
        await callback.bot.send_message(user_id, "⚠️ Не удалось найти данные заявки. Начните заново.")
        await state.finish()
        return

    await _paycore_create_and_send(callback.bot, state, user_id, phone="")

async def paycore_consent_yes(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _paycore_start_phone_collection(callback.bot, state, callback.from_user.id)


async def handle_paycore_phone(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id

    async def _safe_delete(msg_id: Optional[int]) -> None:
        if not msg_id:
            return
        with suppress(Exception):
            await message.bot.delete_message(message.chat.id, int(msg_id))

    data_before = await state.get_data()
    await _safe_delete(data_before.get("paycore_phone_error_msg_id"))
    with suppress(Exception):
        await message.bot.delete_message(message.chat.id, message.message_id)

    phone_raw = message.contact.phone_number if (message.contact and message.contact.phone_number) else (message.text or "")
    phone = _normalize_phone(phone_raw)

    # принимаем любой "вменяемый" номер: 10..15 цифр
    digits = phone[1:] if phone.startswith("+") else phone
    digits = re.sub(r"\D+", "", digits or "")

    if not digits or not (10 <= len(digits) <= 15):
        sent_err = await message.answer(
            "⚠️ Не получилось распознать номер.\n\n"
            "Введите номер телефона цифрами (можно с пробелами/скобками/плюсом).\n"
            "Примеры: `8 999 123-45-67`, `+7(999)1234567`, `9991234567`",
            parse_mode="Markdown",
            reply_markup=_phone_request_kb(),
        )
        await state.update_data(paycore_phone_error_msg_id=sent_err.message_id)
        return

    # гарантируем, что в Paycore уйдёт номер с "+"
    # если пользователь ввёл 10 цифр (часто РФ без кода) — считаем это РФ и добавляем +7
    if len(digits) == 10:
        phone = f"+7{digits}"
    else:
        phone = f"+{digits}"

    await state.update_data(paycore_phone=phone)
    data_after = await state.get_data()
    await _safe_delete(data_after.get("paycore_phone_prompt_msg_id"))
    with suppress(Exception):
        await state.update_data(paycore_phone_error_msg_id=None)

    await _paycore_create_and_send(message.bot, state, user_id, phone)


async def _paycore_create_and_send(bot: Bot, state: FSMContext, user_id: int, phone: str) -> None:
    data = await state.get_data()

    async def _admin_ids() -> List[int]:
        try:
            all_users = await get_all_users()
        except Exception:
            return []

        ids: List[int] = []
        for u in all_users or []:
            try:
                if u.get("role") == "Admin" and isinstance(u.get("telegram_id"), int):
                    ids.append(int(u["telegram_id"]))
            except Exception:
                continue

        return ids

    async def _notify_admin(text: str) -> None:
        admin_ids = await _admin_ids()
        if not admin_ids:
            return

        for aid in admin_ids:
            with suppress(Exception):
                await bot.send_message(
                    aid,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

    asset = (data.get("asset") or "BTC").upper()
    wallet = data.get("wallet") or (await get_user_btc_wallet(user_id) if asset == "BTC" else None)

    if not wallet:
        user_mention = await _user_mention(bot, user_id)
        await _notify_admin(
            "❌ <b>P2P: ошибка до создания страницы оплаты VidraPay</b>\n\n"
            f"Пользователь: {user_mention}\n"
            f"Asset: <b>{html.escape(asset)}</b>\n"
            "Причина: <b>не найден адрес кошелька</b>"
        )
        await bot.send_message(user_id, "⚠️ Не найден адрес для зачисления. Начните заявку заново.")
        await state.finish()
        return

    try:
        order_id = await _finalize_order(
            user_id=user_id,
            btc_amt=float(data["btc_amount"]),
            rub_amount=float(data["rub_amount"]),
            total=int(data["total_min"]),
            wallet=wallet,
            bot=bot,
            asset=asset,
            notify_ops=False,
            notify_user=False,
        )

        rub_amount = float(data.get("rub_amount") or data["total_min"])
        crypto_amount = float(data.get("btc_amount") or 0)

        from services.vidrapay_payin import build_vidrapay_pay_url

        pay_url = build_vidrapay_pay_url(
            p2p_order_id=int(order_id),
            tg_user_id=int(user_id),
        )

        db = await get_db()
        with suppress(Exception):
            await db.execute(
                "UPDATE p2p_orders SET payment_method=? WHERE order_id=?",
                ("VidraPay", int(order_id)),
            )
            await db.commit()

        with suppress(Exception):
            await _save_paycore_transaction(
                int(order_id),
                f"vidrapay:{int(order_id)}",
                pay_url,
            )

    except Exception as e:
        logger.exception("Failed to create VidraPay pay page for user_id=%s", user_id)

        user_mention = await _user_mention(bot, user_id)
        await _notify_admin(
            "❌ <b>P2P: ошибка создания страницы оплаты VidraPay</b>\n\n"
            f"Пользователь: {user_mention}\n"
            f"Asset: <b>{html.escape(asset)}</b>\n"
            f"Ошибка: <code>{html.escape(str(e))}</code>"
        )

        await bot.send_message(
            user_id,
            "❌ Сейчас не удалось создать страницу оплаты.\n"
            "Попробуйте позже или создайте заявку заново.",
        )
        await state.finish()
        return

    text = (
        "💳 <b>Vidra-Pay</b>\n\n"
        f"Заявка: <b>#{int(order_id)}</b>\n"
        f"К получению: <b>{html.escape(_format_asset_amount_for_user(asset, crypto_amount))} {html.escape(asset)}</b>\n"
        f"Сумма заявки: <b>{int(round(rub_amount))}₽</b>\n\n"
        "<i>⚠️ QR-код может отсканировать другой человек, чтобы оплатить вашу заявку.</i>\n\n"
        "<u>На оплату дается 16 минут</u>"
    )

    sent = await bot.send_message(
        user_id,
        text,
        parse_mode="HTML",
        reply_markup=_paycore_keyboard(pay_url, int(order_id)),
        disable_web_page_preview=True,
    )

    pending_buy_messages[user_id] = (sent.chat.id, sent.message_id)

    with suppress(Exception):
        db = await get_db()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS p2p_vidrapay_messages (
                order_id    INTEGER PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                message_id  INTEGER NOT NULL,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                deleted_at  TEXT
            )
            """
        )
        await db.execute(
            """
            INSERT OR REPLACE INTO p2p_vidrapay_messages (
                order_id,
                user_id,
                chat_id,
                message_id,
                created_at,
                deleted_at
            )
            VALUES (?, ?, ?, ?, datetime('now'), NULL)
            """,
            (int(order_id), int(user_id), int(sent.chat.id), int(sent.message_id)),
        )
        await db.commit()

    with suppress(Exception):
        await _schedule_paycore_deadline(bot, user_id, int(order_id))

    await state.finish()


async def paycore_paid_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    try:
        _, raw = (callback.data or "").split(":", 1)
        order_id = int(raw)
    except Exception:
        await callback.bot.send_message(user_id, "⚠️ Ошибка кнопки оплаты (неверный номер заявки).")
        return

    order = await _get_order_by_id(order_id)
    if not order or int(order.get("user_id") or 0) != int(user_id):
        await callback.bot.send_message(user_id, "⚠️ Активная заявка не найдена или уже закрыта.")
        return

    if str(order.get("payment_method") or "").lower().strip() != "paycore":
        await callback.bot.send_message(user_id, "⚠️ Эта заявка не относится к оплате по ссылке.")
        return

    existing_task = PAYCORE_STATUS_TASKS.get(int(order_id))
    if existing_task and not existing_task.done():
        with suppress(Exception):
            if callback.message:
                await callback.message.delete()
        with suppress(Exception):
            await callback.bot.send_message(user_id, "⏳ Ожидайте подтверждение о поступлении средств...")
        return

    with suppress(Exception):
        _cancel_paycore_timers(order_id)

    with suppress(Exception):
        if callback.message:
            await callback.message.delete()

    tx = await _get_paycore_transaction(order_id)
    if not tx or not tx.get("transaction_id"):
        await callback.bot.send_message(user_id, "⚠️ Не найдена транзакция для этой заявки.")
        return

    wait_msg = await callback.bot.send_message(user_id, "⏳ Ожидайте подтверждение о поступлении средств...")
    PAYCORE_WATCH_META[int(order_id)] = {"wait_chat_id": int(wait_msg.chat.id), "wait_message_id": int(wait_msg.message_id)}

    task = asyncio.create_task(
        _paycore_watch_and_autostart(
            callback.bot,
            order_id=int(order_id),
            user_id=int(user_id),
            transaction_id=str(tx["transaction_id"]),
            timeout_sec=16 * 60,
            poll_interval_sec=10,
        )
    )
    PAYCORE_STATUS_TASKS[int(order_id)] = task


async def binance_new_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    user_id = callback.from_user.id

    already_verified = False
    with suppress(Exception):
        already_verified = await is_binance_verified(user_id)

    await state.update_data(binance_mode=True, binance_verified=bool(already_verified), paycore_only=True)

    if already_verified:
        text = (
            "🪙 <b>Выберите монету для покупки:</b>\n\n"
            "❗ <b>Важно:</b>\n"
            "• 🏦 Оплата <b>строго с банка пользователя</b>, который прошёл верификацию\n"
            "• 👤 <b>ФИО в банке должны совпадать</b> с данными верификации\n"
            "• ⛔ Оплата с чужого банка — <b>возврат невозможен</b>"
        )
        await callback.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=_binance_assets_keyboard())
        return

    text = (
        "⚠️ <b>Внимание!</b> ⚠️\n\n"
        "💎 Вы покупаете <b>чистую криптовалюту</b> через биржу <b>Binance</b>.\n\n"
        "🔐 <b>Для оплаты требуется РАЗОВАЯ верификация:</b>\n"
        "• Фото паспорта\n"
        "• Селфи\n\n"
        "Это необходимо, потому что:\n"
        "• ⚡ Автообмен работает <b>24/7</b>\n"
        "• 🏢 Перевод осуществляется на <b>юридическое лицо</b>\n"
        "• 🛡 Банки не блокируют такие платежи\n"
        "• 💸 <b>Самая низкая комиссия</b>\n\n"
        "❗ <b>Важно:</b>\n"
        "• Оплата строго с банка верифицированного человека\n"
        "• Если оплатить с чужого банка — возврат невозможен\n\n"
        "Согласны пройти верификацию?"
    )

    await callback.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=_binance_verify_keyboard())


async def binance_verify_yes_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    user_id = callback.from_user.id
    with suppress(Exception):
        await set_binance_verified(user_id, True)

    await state.update_data(binance_mode=True, binance_verified=True, paycore_only=True)

    text = (
        "🪙 <b>Выберите монету для покупки:</b>\n\n"
        "❗ <b>Важно:</b>\n"
        "• 🏦 Оплата <b>строго с банка пользователя</b>, который прошёл верификацию\n"
        "• 👤 <b>ФИО в банке должны совпадать</b> с данными верификации\n"
        "• ⛔ Оплата с чужого банка — <b>возврат невозможен</b>"
    )

    await callback.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=_binance_assets_keyboard())


async def binance_asset_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    data_raw = callback.data or ""
    if data_raw == Callback.BINANCE_ASSET_LTC:
        asset = "LTC"
    elif data_raw == Callback.BINANCE_ASSET_USDT:
        asset = "USDT"
    elif data_raw == Callback.BINANCE_ASSET_XMR:
        asset = "XMR"
    else:
        asset = "BTC"

    await _start_asset_flow(
        callback=callback,
        state=state,
        asset=asset,
        paycore_only=True,
        binance_mode=True,
    )

async def binance_cancel_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    with suppress(Exception):
        await state.finish()
    with suppress(Exception):
        await callback.message.delete()
    await send_welcome(callback.bot, callback.from_user.id)



async def ask_receipt_pdf(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Пользователь нажал кнопку 'Загрузить чек'."""
    await callback.answer()
    user_id = callback.from_user.id

    order = await get_pending_order(user_id)
    if not order:
        await state.finish()
        await callback.bot.send_message(user_id, "⚠️ Активная заявка не найдена. Создайте новую заявку.")
        return

    await callback.bot.send_message(
        user_id,
        "📄 Отправьте чек об оплате PDF-файлом.",
    )

    await P2PStates.await_receipt_pdf.set()



async def handle_receipt_pdf(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    order = await get_pending_order(user_id)
    if not order:
        await state.finish()
        await message.answer("⚠️ Активная заявка не найдена. Создайте новую заявку.")
        return

    if not message.document:
        await message.answer("⚠️ Нужно отправить чек **файлом PDF**.", parse_mode="Markdown")
        return

    doc = message.document
    mime_ok = doc.mime_type == "application/pdf"
    name_ok = (doc.file_name or "").lower().endswith(".pdf")
    if not (mime_ok or name_ok):
        await message.answer("⚠️ Формат не принят. Пришлите **PDF-файл**.", parse_mode="Markdown")
        return

    try:
        from db import p2p as p2p_db

        if hasattr(p2p_db, "save_p2p_receipt"):
            await p2p_db.save_p2p_receipt(order_id=order["order_id"], file_id=doc.file_id)
        else:
            db = await get_db()
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS p2p_receipts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id    INTEGER NOT NULL,
                    file_id     TEXT    NOT NULL,
                    uploaded_at TEXT    NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            await db.execute(
                "INSERT INTO p2p_receipts (order_id, file_id) VALUES (?, ?)",
                (order["order_id"], doc.file_id),
            )
            await db.commit()
    except Exception:
        logger.exception("Ошибка сохранения чека PDF в БД")
        await message.answer("❌ Не удалось сохранить чек. Попробуйте отправить ещё раз.")
        return

    await message.answer("✅ Чек получен. Оператор приступил к проверке.")
    await state.finish()


async def handle_not_pdf(message: types.Message, state: FSMContext) -> None:
    await message.answer(
        "⚠️ Нужно отправить чек **именно файлом PDF** (не фото/скриншот).\nЕсли у вас изображение — сохраните его как PDF и отправьте.",
        parse_mode="Markdown",
    )


async def amount_not_text(message: types.Message, state: FSMContext) -> None:
    await message.answer(
        "⚠️ Введите сумму **текстом**. Примеры:\n`1000` — рубли\n`0.001` — криптовалюта\n`2 500,50` — можно с пробелами и запятой",
        parse_mode="Markdown",
        reply_markup=cancel_buy_keyboard(),
    )


async def wallet_not_text(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    asset = (data.get("asset") or "BTC").upper()

    if asset == "LTC":
        example = "L..."
        coin_label = "LTC-адрес"
    elif asset == "USDT":
        example = "T..."
        coin_label = "USDT TRC20-адрес"
    elif asset == "XMR":
        example = "4... или 8..."
        coin_label = "XMR-адрес"
    else:
        example = "bc1q... или 1... / 3..."
        coin_label = "BTC-адрес"

    await message.answer(
        f"⚠️ Пришлите {coin_label} **текстом**.\nПример: `{example}`",
        parse_mode="Markdown",
    )



async def paused_asset_callback(callback: types.CallbackQuery) -> None:
    await callback.answer("⛔ Обмен по этой монете временно приостановлен", show_alert=True)


async def _start_akkula_link_for_order(bot: Bot, *, user_id: int, order_id: int) -> None:
    """
    Создаёт/показывает Akkula-ссылку для уже созданной p2p_orders заявки.
    Используется и кнопкой p2p_akkula_link, и автозапуском по порогу суммы.
    """
    order = await _get_order_by_id(order_id)
    if not order or int(order.get("user_id") or 0) != int(user_id):
        await bot.send_message(user_id, "⚠️ Заявка не найдена или это не ваша заявка.")
        return

    status = str(order.get("status") or "").lower().strip()
    if status != "pending":
        await bot.send_message(user_id, "⚠️ Эта заявка уже закрыта или неактивна.")
        return

    try:
        op_id_now = int(order.get("operator_id") or 0)
    except Exception:
        op_id_now = 0
    if op_id_now:
        await bot.send_message(
            user_id,
            "⚠️ Заявку уже принял оператор. Оплата по ссылке недоступна для этой заявки.",
        )
        return

    pm = str(order.get("payment_method") or "").lower().strip()
    if pm == "paycore":
        await bot.send_message(
            user_id,
            "⚠️ Для этой заявки уже выбран способ оплаты Paycore.\n"
            "Создайте новую заявку, чтобы оплатить через Akkula.",
        )
        return

    asset_raw = (ORDER_ASSETS.get(int(order_id)) or "BTC").upper().strip()
    if asset_raw == "USDT":
        akk_asset = "USDT_TRC20"
    elif asset_raw == "LTC":
        akk_asset = "LTC"
    else:
        akk_asset = "BTC"

    user_wallet = str(order.get("wallet") or "").strip()
    if not user_wallet:
        await bot.send_message(user_id, "⚠️ В заявке не найден кошелёк получения. Создайте заявку заново.")
        return

    try:
        payable_rub = float(order.get("total_rub") or 0)
    except Exception:
        payable_rub = 0.0
    if payable_rub <= 0:
        await bot.send_message(user_id, "⚠️ В заявке не найдена сумма к оплате. Создайте заявку заново.")
        return

    try:
        net_rub = float(order.get("rub_amount") or 0)
    except Exception:
        net_rub = 0.0

    try:
        from handlers.buy.akkula import (
            AKKULA_DEFAULT_CLIENT_NAME,
            AKKULA_DEFAULT_CLIENT_PHONE,
            AKKULA_FIXED_NETWORK,
            AKKULA_FIXED_RECIPIENT_WALLET,
            _calculate_receive_from_net,
            _asset_title,
            _final_keyboard,  # ✅ важно: теперь принимает pay_url
        )
        from config.settings import AKKULA_API_KEY, AKKULA_BASE_URL, AKKULA_TIMEOUT_SEC
        from services.akkula import AkkulaAPIError, AkkulaClient
        from db.akkula_orders import save_akkula_order
    except Exception:
        await bot.send_message(user_id, "❌ Модуль Akkula не доступен. Попробуйте позже.")
        return

    def _build_payment_text(*, asset: str, receive_text: str, pay_rub: float, wallet: str) -> str:
        sep = "➖" * 10
        coin = _asset_title(asset)
        pay_str = f"{int(pay_rub)} RUB"
        recv = receive_text or "-"

        info = (
            "<i>🔗 «Ссылка» — можно скопировать и отправить другому для оплаты.</i>"
        )

        return "\n".join(
            [
                "📥 Данные платежа:",
                sep,
                f"▶ Монета: {coin}",
                f"▶ Поступят: {recv}",
                f"▶ Сумма оплаты: {pay_str}",
                sep,
                str(wallet),
                sep,
                info,
                sep,
            ]
        )

    async def _resolve_final_url(url: str) -> str:
        """
        Делает ровно то же, что и кнопка «Ссылка» (akkula_final_copy_link):
        разворачивает редиректы и возвращает финальный URL (обычно без 'akkula').
        """
        u = str(url or "").strip()
        if not u:
            return ""
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(u, allow_redirects=True) as resp:
                    return str(resp.url)
        except Exception:
            return u

    async def _hide_operator_cards_for_order(oid: int) -> None:
        cached = pending_operator_messages.get(user_id, []) or []

        async def _close_one(chat_id: int, msg_id: int) -> None:
            from contextlib import suppress

            with suppress(Exception):
                await bot.delete_message(chat_id, msg_id)
                return
            with suppress(Exception):
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
                return

        for chat_id, msg_id in cached:
            from contextlib import suppress

            with suppress(Exception):
                await _close_one(int(chat_id), int(msg_id))

        try:
            db_local = await get_db()
        except Exception:
            return

        from contextlib import suppress

        with suppress(Exception):
            await db_local.execute(
                """
                CREATE TABLE IF NOT EXISTS p2p_operator_messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id    INTEGER NOT NULL,
                    chat_id     INTEGER NOT NULL,
                    message_id  INTEGER NOT NULL,
                    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            await db_local.commit()

        try:
            cur = await db_local.execute(
                "SELECT id, chat_id, message_id FROM p2p_operator_messages WHERE order_id = ?",
                (int(oid),),
            )
            rows = await cur.fetchall() or []
        except Exception:
            rows = []

        for row in rows:
            try:
                rec_id = int(row[0])
                chat_id = int(row[1])
                msg_id = int(row[2])
            except Exception:
                continue

            with suppress(Exception):
                await _close_one(chat_id, msg_id)

            with suppress(Exception):
                await db_local.execute("DELETE FROM p2p_operator_messages WHERE id = ?", (rec_id,))
                await db_local.commit()

        with suppress(Exception):
            await db_local.execute("DELETE FROM p2p_operator_messages WHERE order_id = ?", (int(oid),))
            await db_local.commit()

    creating_msg_id: Optional[int] = None
    try:
        creating_msg = await bot.send_message(user_id, "⏳ Создаю реквизиты платежа...")
        creating_msg_id = int(creating_msg.message_id)
    except Exception:
        creating_msg_id = None

    from contextlib import suppress

    with suppress(Exception):
        await _hide_operator_cards_for_order(int(order_id))

    # --- пытаемся найти уже созданную ссылку по p2p_order_id ---
    existing_rec = None
    try:
        from db.akkula_orders import get_akkula_order_by_p2p_order_id  # type: ignore

        existing_rec = await get_akkula_order_by_p2p_order_id(int(order_id))
    except Exception:
        existing_rec = None

    with suppress(Exception):
        await asyncio.sleep(1.5)

    # --- повторная проверка: заявка должна быть pending и без оператора ---
    order_check = await _get_order_by_id(order_id)
    if not order_check or int(order_check.get("user_id") or 0) != int(user_id):
        with suppress(Exception):
            if creating_msg_id:
                await bot.delete_message(user_id, creating_msg_id)
        await bot.send_message(user_id, "⚠️ Заявка не найдена или уже закрыта.")
        return

    try:
        op_id_after = int(order_check.get("operator_id") or 0)
    except Exception:
        op_id_after = 0
    if op_id_after:
        with suppress(Exception):
            if creating_msg_id:
                await bot.delete_message(user_id, creating_msg_id)
        await bot.send_message(
            user_id,
            "⚠️ Заявку уже принял оператор. Оплата по ссылке недоступна для этой заявки.",
        )
        return

    st_after = str(order_check.get("status") or "").lower().strip()
    if st_after != "pending":
        with suppress(Exception):
            if creating_msg_id:
                await bot.delete_message(user_id, creating_msg_id)
        await bot.send_message(user_id, "⚠️ Эта заявка уже закрыта или неактивна.")
        return

    # --- если ссылка уже есть в БД Akkula — просто покажем её ---
    if existing_rec:
        existing_url = str(existing_rec.get("payment_url") or existing_rec.get("short_payment_url") or "").strip()
        existing_pid = str(existing_rec.get("partner_order_id") or "").strip()
        if existing_url and existing_pid:
            with suppress(Exception):
                if creating_msg_id:
                    await bot.delete_message(user_id, creating_msg_id)

            # ✅ фиксируем метод оплаты в p2p_orders, даже если ссылка существовала
            try:
                db = await get_db()
                await db.execute(
                    "UPDATE p2p_orders SET payment_method = ? WHERE order_id = ?",
                    ("akkula", int(order_id)),
                )
                await db.commit()
            except Exception:
                pass

            try:
                receive_text, _, _ = await _calculate_receive_from_net(
                    float(net_rub) if net_rub > 0 else float(payable_rub),
                    akk_asset,
                )
            except Exception:
                receive_text = "-"

            # ✅ URL для кнопки "Оплатить": как «Ссылка» (разворачиваем редиректы → обычно без 'akkula')
            btn_url = await _resolve_final_url(existing_url)

            text = _build_payment_text(
                asset=str(akk_asset),
                receive_text=str(receive_text),
                pay_rub=float(payable_rub),
                wallet=str(user_wallet),
            )

            sent = await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
                reply_markup=_final_keyboard(existing_pid, pay_url=btn_url),
                disable_web_page_preview=True,
            )
            with suppress(Exception):
                pending_buy_messages[user_id] = (sent.chat.id, sent.message_id)
            return

    # --- создаём новую ссылку через Akkula ---
    client = AkkulaClient(api_key=AKKULA_API_KEY, base_url=AKKULA_BASE_URL, timeout_sec=AKKULA_TIMEOUT_SEC)
    partner_order_id = f"p2p-{int(user_id)}-{int(order_id)}-{uuid.uuid4().hex[:8]}"[:50]

    try:
        limits = await client.get_limits(amount_rub=float(payable_rub), network=str(AKKULA_FIXED_NETWORK))
        if not limits.get("can_create_order", True):
            reason = limits.get("reason") or "Нельзя создать заказ на указанную сумму."
            with suppress(Exception):
                if creating_msg_id:
                    await bot.delete_message(user_id, creating_msg_id)
            await bot.send_message(user_id, f"❌ {reason}")
            return

        akk_order = await client.create_order(
            partner_order_id=str(partner_order_id),
            amount_rub=float(payable_rub),
            recipient_wallet=str(AKKULA_FIXED_RECIPIENT_WALLET),
            network=str(AKKULA_FIXED_NETWORK),
            client_phone=str(AKKULA_DEFAULT_CLIENT_PHONE),
            client_name=str(AKKULA_DEFAULT_CLIENT_NAME),
            metadata={
                "tg_user_id": int(user_id),
                "source": "auto_or_button",
                "user_selected_asset": str(akk_asset),
                "user_recipient_wallet": str(user_wallet),
                "p2p_order_id": int(order_id),
                "fixed_wallet": True,
            },
        )
    except AkkulaAPIError as e:
        with suppress(Exception):
            if creating_msg_id:
                await bot.delete_message(user_id, creating_msg_id)

        try:
            from handlers.buy.akkula import _format_akkula_error

            msg = _format_akkula_error(e)
        except Exception:
            msg = f"Ошибка: {e}"
        await bot.send_message(user_id, f"❌ {msg}")
        return
    except Exception:
        logger.exception("Unexpected error while creating Akkula order from P2P (order_id=%s)", order_id)
        with suppress(Exception):
            if creating_msg_id:
                await bot.delete_message(user_id, creating_msg_id)
        await bot.send_message(user_id, "❌ Не удалось создать ссылку Akkula (внутренняя ошибка).")
        return

    pay_url = str(akk_order.get("payment_url") or akk_order.get("short_payment_url") or "").strip()
    qr_url = str(akk_order.get("qr_image_url") or "").strip()
    expires_at = akk_order.get("expires_at")
    amount_usdt = akk_order.get("amount_usdt")
    akk_status = akk_order.get("status")
    akkula_order_id = akk_order.get("order_id")

    if not pay_url:
        with suppress(Exception):
            if creating_msg_id:
                await bot.delete_message(user_id, creating_msg_id)
        await bot.send_message(user_id, "❌ Бот не вернул ссылку. Попробуйте позже.")
        return

    # --- финальная проверка на оператора перед фиксацией ---
    order_after_create = await _get_order_by_id(order_id)
    if order_after_create:
        try:
            op_after_create = int(order_after_create.get("operator_id") or 0)
        except Exception:
            op_after_create = 0
        if op_after_create:
            with suppress(Exception):
                if creating_msg_id:
                    await bot.delete_message(user_id, creating_msg_id)
            await bot.send_message(
                user_id,
                "⚠️ Заявку уже принял оператор. Оплата по ссылке отменена для этой заявки.",
            )
            return

    # ✅ фиксируем метод оплаты
    try:
        db = await get_db()
        await db.execute("UPDATE p2p_orders SET payment_method = ? WHERE order_id = ?", ("akkula", int(order_id)))
        await db.commit()
    except Exception:
        pass

    try:
        receive_text, _, _ = await _calculate_receive_from_net(
            float(net_rub) if net_rub > 0 else float(payable_rub),
            akk_asset,
        )
    except Exception:
        receive_text = "-"

    # ✅ URL для кнопки "Оплатить": как «Ссылка» (разворачиваем редиректы → обычно без 'akkula')
    btn_url = await _resolve_final_url(pay_url)

    final_text = _build_payment_text(
        asset=str(akk_asset),
        receive_text=str(receive_text),
        pay_rub=float(payable_rub),
        wallet=str(user_wallet),
    )

    with suppress(Exception):
        if creating_msg_id:
            await bot.delete_message(user_id, creating_msg_id)

    sent = await bot.send_message(
        chat_id=user_id,
        text=final_text,
        parse_mode="HTML",
        reply_markup=_final_keyboard(str(partner_order_id), pay_url=btn_url),
        disable_web_page_preview=True,
    )

    try:
        await save_akkula_order(
            partner_order_id=str(partner_order_id),
            order_id=str(akkula_order_id) if akkula_order_id else None,
            tg_user_id=int(user_id),
            status=str(akk_status) if akk_status else None,
            amount_rub=float(payable_rub),
            amount_usdt=float(amount_usdt) if amount_usdt is not None else None,
            network=str(AKKULA_FIXED_NETWORK),
            recipient_wallet=str(AKKULA_FIXED_RECIPIENT_WALLET),
            short_payment_url=str(akk_order.get("short_payment_url")) if akk_order.get("short_payment_url") else None,
            payment_url=str(akk_order.get("payment_url")) if akk_order.get("payment_url") else None,
            qr_image_url=str(qr_url) if qr_url else None,
            expires_at=str(expires_at) if expires_at else None,
            user_selected_asset=str(akk_asset),
            user_recipient_wallet=str(user_wallet),
            p2p_order_id=int(order_id),
            link_message_id=int(sent.message_id),
        )
    except Exception:
        pass

    with suppress(Exception):
        pending_buy_messages[user_id] = (sent.chat.id, sent.message_id)


async def p2p_akkula_link_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    try:
        _, raw = (callback.data or "").split(":", 1)
        order_id = int(raw.strip())
    except Exception:
        await callback.bot.send_message(user_id, "⚠️ Не удалось распознать заявку.")
        return

    with suppress(Exception):
        if callback.message:
            await callback.message.delete()

    await _start_akkula_link_for_order(callback.bot, user_id=user_id, order_id=order_id)


def register_p2p_handlers(dp: Dispatcher) -> None:
    dp.register_callback_query_handler(
        paycore_paid_callback,
        lambda c: isinstance(c.data, str) and c.data.startswith("paycore_paid:"),
        state="*",
    )

    dp.register_callback_query_handler(
        p2p_akkula_link_callback,
        lambda c: isinstance(c.data, str) and c.data.startswith("p2p_akkula_link:"),
        state="*",
    )

    dp.register_callback_query_handler(
        binance_new_callback,
        lambda c: c.data == Callback.BINANCE_NEW,
        state="*",
    )
    dp.register_callback_query_handler(
        binance_verify_yes_callback,
        lambda c: c.data == Callback.BINANCE_VERIFY_YES,
        state="*",
    )
    dp.register_callback_query_handler(
        binance_asset_callback,
        lambda c: c.data in (
            Callback.BINANCE_ASSET_BTC,
            Callback.BINANCE_ASSET_LTC,
            Callback.BINANCE_ASSET_USDT,
            Callback.BINANCE_ASSET_XMR,
        ),
        state="*",
    )
    dp.register_callback_query_handler(
        binance_cancel_callback,
        lambda c: c.data == Callback.BINANCE_CANCEL,
        state="*",
    )

    dp.register_callback_query_handler(
        start_p2p,
        lambda c: c.data in (
            Callback.P2P_ASSISTANT,
            Callback.P2P_ASSISTANT_LTC,
            Callback.P2P_ASSISTANT_USDT,
            Callback.P2P_ASSISTANT_XMR,
        ),
    )

    dp.register_message_handler(handle_amount, content_types=[types.ContentType.TEXT], state=P2PStates.amount)
    dp.register_message_handler(amount_not_text, content_types=types.ContentType.ANY, state=P2PStates.amount)

    dp.register_callback_query_handler(confirm_p2p, lambda c: c.data == Callback.CONFIRM_ORDER, state=P2PStates.confirm)
    dp.register_callback_query_handler(back_to_amount, lambda c: c.data == "back", state=P2PStates.confirm)

    dp.register_message_handler(handle_wallet, state=P2PStates.wallet, content_types=types.ContentType.TEXT)
    dp.register_message_handler(wallet_not_text, content_types=types.ContentType.ANY, state=P2PStates.wallet)
    dp.register_callback_query_handler(use_saved_wallet, lambda c: c.data == "use_saved_wallet", state=P2PStates.wallet)
    dp.register_callback_query_handler(new_wallet, lambda c: c.data == "new_wallet", state=P2PStates.wallet)

    dp.register_callback_query_handler(pay_card_callback, lambda c: c.data == Callback.PAY_CARD, state="*")
    dp.register_callback_query_handler(pay_sbp_callback, lambda c: c.data == Callback.PAY_SBP, state="*")
    dp.register_callback_query_handler(pay_paycore_callback, lambda c: c.data == Callback.PAY_PAYCORE, state="*")
    dp.register_callback_query_handler(
        paycore_show_qr_callback,
        lambda c: isinstance(c.data, str) and c.data.startswith("paycore_show_qr:"),
        state="*",
    )

    dp.register_callback_query_handler(
        paycore_consent_yes,
        lambda c: c.data == "paycore_consent_yes",
        state=P2PStates.paycore_consent,
    )
    dp.register_message_handler(
        handle_paycore_phone,
        content_types=[types.ContentType.CONTACT, types.ContentType.TEXT],
        state=P2PStates.paycore_phone,
    )

    dp.register_callback_query_handler(
        ask_receipt_pdf,
        lambda c: isinstance(c.data, str) and (
            c.data == "upload_receipt"
            or c.data.startswith("upload_receipt:")
            or c.data == "attach_receipt"
            or c.data.startswith("attach_receipt:")
            or "receipt" in c.data
        ),
        state="*",
    )

    dp.register_message_handler(handle_receipt_pdf, content_types=[types.ContentType.DOCUMENT], state=P2PStates.await_receipt_pdf)
    dp.register_message_handler(handle_not_pdf, content_types=types.ContentType.ANY, state=P2PStates.await_receipt_pdf)

    dp.register_callback_query_handler(
        cancel_p2p_by_id,
        lambda c: isinstance(c.data, str) and c.data.startswith("cancel_buy:"),
        state="*",
    )
    dp.register_callback_query_handler(cancel_p2p, lambda c: c.data == Callback.CANCEL_BUY, state="*")
    dp.register_callback_query_handler(cancel_active_order, lambda c: c.data == "cancel_active_order", state="*")