from __future__ import annotations

import html
import logging
import random
import re
from contextlib import suppress
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

from aiohttp import web
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import settings
from db.cards import get_active_cards
from db.connection import get_db
from services.nirvana import NirvanaAPIError
from services.nirvana_payin import create_nirvana_ns_pk_qr_order
from services.vidrapay_payin import parse_vidrapay_token

logger = logging.getLogger(__name__)


ORDER_ASSET_COMMENT_RE = re.compile(r"\((BTC|LTC|USDT|XMR)\)", re.IGNORECASE)

VIDRAPAY_METHODS: Dict[str, Dict[str, Any]] = {
    "sbp": {
        "title": "СБП",
        "subtitle": "Перевод через Систему быстрых платежей",
        "db_value": "vidrapay_sbp",
        "icon": "⚡",
        "enabled": True,
        "card_field": "sbp_phone",
        "requisites_label": "Телефон для СБП",
    },
    "card": {
        "title": "Перевод на карту",
        "subtitle": "Обычный банковский перевод",
        "db_value": "vidrapay_card",
        "icon": "💳",
        "enabled": True,
        "card_field": "card_number",
        "requisites_label": "Номер карты",
    },
    "qr": {
        "title": "Оплата по QR-коду",
        "subtitle": "Автоматический обмен 24/7",
        "db_value": "vidrapay_qr",
        "icon": "▦",
        "enabled": True,
        "card_field": "",
        "requisites_label": "QR-ссылка",
    },
}


def _safe_text(value: Any, default: str = "—") -> str:
    text = str(value or "").strip()
    return html.escape(text if text else default)


def _no_cache_response(html_text: str, *, status: int = 200) -> web.Response:
    return web.Response(
        text=html_text,
        status=status,
        content_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def _format_amount(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0

    if amount.is_integer():
        return str(int(amount))

    return f"{amount:.2f}".rstrip("0").rstrip(".")


def _format_method_amount(value: Any) -> str:
    """
    Показывает сумму метода оплаты так же, как пользователь видел её в боте:
    итоговая сумма заявки из p2p_orders.total_rub, без дополнительных web-комиссий.
    """
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0

    return str(int(round(amount)))


def _format_crypto_amount(value: Any, asset: str) -> str:
    try:
        num = float(value)
    except Exception:
        return str(value or "").strip()

    asset_u = str(asset or "").upper().strip()
    if asset_u == "USDT":
        return str(int(round(num)))

    formatted = f"{num:.8f}".rstrip("0").rstrip(".")
    return formatted or "0"


def _resolve_asset(order: Dict[str, Any]) -> str:
    comment = str(order.get("comment") or "")
    match = ORDER_ASSET_COMMENT_RE.search(comment)
    if match:
        return str(match.group(1) or "BTC").upper()

    asset = str(order.get("asset") or "").upper().strip()
    if asset in {"BTC", "LTC", "USDT", "XMR"}:
        return asset

    return "BTC"


def _method_key_from_db_value(payment_method: Any) -> str:
    current = str(payment_method or "").strip()
    for key, method in VIDRAPAY_METHODS.items():
        if current == str(method.get("db_value") or ""):
            return key
    if current.startswith("vidrapay_"):
        return current.replace("vidrapay_", "", 1)
    return current


async def _get_p2p_order(order_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    db = await get_db()
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
            comment,
            bank_card,
            bank_name,
            card_id
        FROM p2p_orders
        WHERE order_id = ? AND user_id = ?
        LIMIT 1
        """,
        (int(order_id), int(user_id)),
    )
    row = await cur.fetchone()

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
        "bank_card": row[10] or "",
        "bank_name": row[11] or "",
        "card_id": int(row[12] or 0),
    }
    order["asset"] = _resolve_asset(order)
    return order


async def _set_payment_method(order_id: int, method: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE p2p_orders SET payment_method = ? WHERE order_id = ?",
        (method, int(order_id)),
    )
    await db.commit()


async def _set_payment_card(order_id: int, method: str, card: Dict[str, Any], requisites: str) -> None:
    db = await get_db()
    await db.execute(
        """
        UPDATE p2p_orders
        SET
            payment_method = ?,
            bank_card = ?,
            bank_name = ?,
            card_id = ?
        WHERE order_id = ?
        """,
        (
            method,
            str(requisites or "").strip(),
            str(card.get("bank_name") or "").strip(),
            int(card.get("card_id") or 0),
            int(order_id),
        ),
    )
    await db.commit()


async def _get_cards_for_method(method_key: str, order_id: int = 0) -> List[Dict[str, Any]]:
    method = VIDRAPAY_METHODS.get(method_key) or {}
    card_field = str(method.get("card_field") or "").strip()
    if not card_field:
        return []

    cards = await get_active_cards()
    cards = await _filter_cards_by_vidrapay_distribution(cards, order_id=order_id)

    result: List[Dict[str, Any]] = []
    seen_banks: Set[str] = set()

    for card in cards:
        requisites = str(card.get(card_field) or "").strip()
        bank_name = str(card.get("bank_name") or "").strip()
        bank_key = bank_name.casefold()
        if not requisites or not bank_name or bank_key in seen_banks:
            continue
        seen_banks.add(bank_key)
        result.append(card)

    return result

async def _get_all_cards_for_method(method_key: str, order_id: int = 0) -> List[Dict[str, Any]]:
    method = VIDRAPAY_METHODS.get(method_key) or {}
    card_field = str(method.get("card_field") or "").strip()
    if not card_field:
        return []

    cards = await get_active_cards()
    cards = await _filter_cards_by_vidrapay_distribution(cards, order_id=order_id)

    result: List[Dict[str, Any]] = []
    for card in cards:
        requisites = str(card.get(card_field) or "").strip()
        bank_name = str(card.get("bank_name") or "").strip()
        if requisites and bank_name:
            result.append(card)
    return result


async def _get_user_last_success_card(
    *,
    user_id: int,
    method_key: str,
    order_id: int = 0,
) -> Optional[Dict[str, Any]]:
    """
    Возвращает последние рабочие реквизиты пользователя для метода «Проверенный банк».

    Важно: раньше поиск был слишком строгим и учитывал только заявки
    с payment_method = 'vidrapay_card'. Из-за этого метод оставался серым,
    если предыдущая успешная сделка была создана обычной P2P-веткой
    с payment_method = 'card' или если у старой заявки не был сохранён card_id.

    Теперь поиск идёт по последним completed-заявкам пользователя:
    - сначала пробуем найти активную карту по card_id;
    - если card_id в старой заявке не сохранён, пробуем сопоставить реквизиты
      по bank_card + bank_name;
    - карта всё равно должна быть активной, иметь нужное поле реквизитов
      и проходить текущий фильтр распределения VidraPay.
    """

    method = VIDRAPAY_METHODS.get(method_key) or {}
    card_field = str(method.get("card_field") or "").strip()

    if not card_field:
        return None

    db = await get_db()

    try:
        cur = await db.execute(
            """
            SELECT
                order_id,
                card_id,
                bank_card,
                bank_name,
                payment_method
              FROM p2p_orders
             WHERE user_id = ?
               AND status = 'completed'
               AND (
                    (card_id IS NOT NULL AND card_id > 0)
                    OR IFNULL(TRIM(bank_card), '') != ''
               )
          ORDER BY
                COALESCE(completed_at, created_at) DESC,
                order_id DESC
             LIMIT 30
            """,
            (int(user_id),),
        )

        rows = await cur.fetchall() or []
        await cur.close()

    except Exception:
        logger.exception(
            "Failed to load last user card user_id=%s method=%s",
            user_id,
            method_key,
        )
        return None

    if not rows:
        return None

    try:
        cards = await get_active_cards()
        cards = await _filter_cards_by_vidrapay_distribution(
            cards,
            order_id=order_id,
        )
    except Exception:
        logger.exception(
            "Failed to load active cards for familiar VidraPay user_id=%s order_id=%s",
            user_id,
            order_id,
        )
        return None

    active_cards: Dict[int, Dict[str, Any]] = {}
    cards_by_requisites: Dict[tuple[str, str], Dict[str, Any]] = {}

    for card in cards:
        try:
            card_id = int(card.get("card_id") or 0)
        except Exception:
            card_id = 0

        bank_name = str(card.get("bank_name") or "").strip()
        requisites = str(card.get(card_field) or "").strip()

        if not bank_name or not requisites:
            continue

        if card_id > 0:
            active_cards[card_id] = card

        cards_by_requisites.setdefault(
            (bank_name.casefold(), requisites),
            card,
        )

    if not active_cards and not cards_by_requisites:
        return None

    for row in rows:
        try:
            last_card_id = int(row[1] or 0)
        except Exception:
            last_card_id = 0

        if last_card_id > 0:
            card = active_cards.get(last_card_id)
            if card:
                return card

        last_requisites = str(row[2] or "").strip()
        last_bank_name = str(row[3] or "").strip()

        if last_requisites and last_bank_name:
            card = cards_by_requisites.get((last_bank_name.casefold(), last_requisites))
            if card:
                return card

        if last_requisites:
            for card in cards:
                requisites = str(card.get(card_field) or "").strip()
                if requisites == last_requisites:
                    bank_name = str(card.get("bank_name") or "").strip()
                    if bank_name:
                        return card

    return None


async def _ensure_vidrapay_distribution_tables() -> None:
    db = await get_db()

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS vidrapay_distribution_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    await db.execute(
        """
        INSERT OR IGNORE INTO vidrapay_distribution_settings(key, value, updated_at)
        VALUES('enabled', '0', CURRENT_TIMESTAMP)
        """
    )

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS vidrapay_card_distribution_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            method_key TEXT NOT NULL,
            card_id INTEGER NOT NULL UNIQUE,
            bank_name TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    await db.commit()


async def _is_vidrapay_distribution_enabled() -> bool:
    await _ensure_vidrapay_distribution_tables()

    db = await get_db()
    cur = await db.execute(
        "SELECT value FROM vidrapay_distribution_settings WHERE key = 'enabled'"
    )
    row = await cur.fetchone()
    await cur.close()

    return bool(row and str(row[0]) == "1")


async def _filter_cards_by_vidrapay_distribution(
    cards: List[Dict[str, Any]],
    *,
    order_id: int = 0,
) -> List[Dict[str, Any]]:
    if not cards:
        return []

    if not await _is_vidrapay_distribution_enabled():
        return cards

    db = await get_db()
    cur = await db.execute(
        """
        SELECT card_id
          FROM vidrapay_card_distribution_usage
         WHERE order_id != ?
        """,
        (int(order_id or 0),),
    )
    rows = await cur.fetchall() or []
    await cur.close()

    used_card_ids = {int(row[0]) for row in rows if row and row[0] is not None}

    result: List[Dict[str, Any]] = []
    for card in cards:
        card_id = int(card.get("card_id") or 0)
        if card_id > 0 and card_id not in used_card_ids:
            result.append(card)

    return result


async def _claim_vidrapay_distribution_card(
    *,
    order_id: int,
    user_id: int,
    method_key: str,
    card: Dict[str, Any],
) -> bool:
    if not await _is_vidrapay_distribution_enabled():
        return True

    card_id = int(card.get("card_id") or 0)
    if card_id <= 0:
        return False

    db = await get_db()

    try:
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO vidrapay_card_distribution_usage(
                order_id,
                user_id,
                method_key,
                card_id,
                bank_name,
                created_at
            )
            VALUES(?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                int(order_id),
                int(user_id),
                str(method_key or ""),
                card_id,
                str(card.get("bank_name") or "").strip(),
            ),
        )
        await db.commit()

        return int(cur.rowcount or 0) > 0

    except Exception:
        logger.exception(
            "Failed to claim VidraPay distribution card order_id=%s card_id=%s",
            order_id,
            card_id,
        )
        return False


async def _release_vidrapay_distribution_card(order_id: int, card_id: int) -> None:
    if int(order_id or 0) <= 0 or int(card_id or 0) <= 0:
        return

    db = await get_db()
    await db.execute(
        """
        DELETE FROM vidrapay_card_distribution_usage
         WHERE order_id = ?
           AND card_id = ?
        """,
        (int(order_id), int(card_id)),
    )
    await db.commit()



async def _admin_ids() -> List[int]:
    try:
        db = await get_db()
        cur = await db.execute(
            "SELECT telegram_id FROM users WHERE role = 'Admin' AND telegram_id IS NOT NULL"
        )
        rows = await cur.fetchall() or []
        await cur.close()
    except Exception:
        logger.exception("Failed to load admin ids for VidraPay webhook")
        return []

    result: List[int] = []
    seen: Set[int] = set()
    for row in rows:
        if not row:
            continue
        with suppress(Exception):
            admin_id = int(row[0])
            if admin_id and admin_id not in seen:
                seen.add(admin_id)
                result.append(admin_id)
    return result


def _short_wallet(wallet: Any) -> str:
    text = str(wallet or "").strip()
    if len(text) <= 18:
        return text or "—"
    return f"{text[:8]}…{text[-8:]}"


def _format_amount_for_asset(asset: str, value: Any) -> str:
    return _format_crypto_amount(value, asset or "BTC")


async def _send_bot_message(
    chat_id: int,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[tuple[int, int]]:
    bot = Bot(token=settings.bot_token, parse_mode="HTML")
    try:
        sent = await bot.send_message(
            int(chat_id),
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return int(sent.chat.id), int(sent.message_id)
    finally:
        with suppress(Exception):
            session = await bot.get_session()
            await session.close()


async def _send_many_bot_messages(ids: List[int], text: str, *, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    bot = Bot(token=settings.bot_token, parse_mode="HTML")
    try:
        sent: Set[int] = set()
        for raw_id in ids:
            with suppress(Exception):
                chat_id = int(raw_id)
                if chat_id in sent:
                    continue
                sent.add(chat_id)
                await bot.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
    finally:
        with suppress(Exception):
            session = await bot.get_session()
            await session.close()


async def _delete_vidrapay_payment_message(order: Dict[str, Any]) -> None:
    """
    Удаляет в Telegram сообщение «Оплата через VidraPay» с кнопками QR/Оплатить/Отменить.

    Message_id сохраняется в БД в handlers/buy/p2p.py в момент отправки сообщения.
    Поэтому удаление работает даже если vidrapay_webhook.py запущен отдельным процессом.
    """
    order_id = int(order.get("order_id") or 0)
    user_id = int(order.get("user_id") or 0)

    if order_id <= 0 or user_id <= 0:
        return

    chat_id = 0
    message_id = 0

    try:
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
        await db.commit()

        cur = await db.execute(
            """
            SELECT chat_id, message_id
              FROM p2p_vidrapay_messages
             WHERE order_id = ?
               AND user_id = ?
               AND deleted_at IS NULL
             LIMIT 1
            """,
            (order_id, user_id),
        )
        row = await cur.fetchone()
        await cur.close()

        if row:
            chat_id = int(row[0] or 0)
            message_id = int(row[1] or 0)
    except Exception:
        logger.exception("Failed to load VidraPay payment message id for order_id=%s", order_id)
        return

    if chat_id <= 0 or message_id <= 0:
        return

    bot = Bot(token=settings.bot_token, parse_mode="HTML")
    try:
        with suppress(Exception):
            await bot.delete_message(chat_id, message_id)

        with suppress(Exception):
            db = await get_db()
            await db.execute(
                """
                UPDATE p2p_vidrapay_messages
                   SET deleted_at = datetime('now')
                 WHERE order_id = ?
                """,
                (order_id,),
            )
            await db.commit()
    finally:
        with suppress(Exception):
            session = await bot.get_session()
            await session.close()

async def _notify_paid_to_admins(order: Dict[str, Any]) -> None:
    """
    Единая точка входа для отметки «Оплатил» из VidraPay.

    Важно:
    - реквизиты остаются VidraPay-специфичными и выдаются на платёжном сайте;
    - после нажатия «Оплатил» дальше используется тот же обработчик, что и в обычной P2P-ветке:
      handlers.chat.instruction.handle_paid();
    - поэтому карточка оператору, кнопка ff_ready и дальнейший запуск обмена идут через общий P2P-процесс.
    """
    order_id = int(order.get("order_id") or 0)
    user_id = int(order.get("user_id") or 0)

    if order_id <= 0:
        return

    bot = Bot(token=settings.bot_token, parse_mode="HTML")

    class _VidraPayFakeUser:
        def __init__(self, uid: int) -> None:
            self.id = int(uid)

    class _VidraPayFakeMessage:
        def __init__(self, bot_obj: Bot, chat_id: int) -> None:
            self.bot = bot_obj
            self.chat = type("_VidraPayFakeChat", (), {"id": int(chat_id)})()
            self.message_id = 0

        async def delete(self) -> None:
            return None

        async def edit_reply_markup(self, *args: Any, **kwargs: Any) -> None:
            return None

    class _VidraPayFakeCallback:
        def __init__(self, bot_obj: Bot, uid: int, oid: int) -> None:
            self.bot = bot_obj
            self.from_user = _VidraPayFakeUser(uid)
            self.message = _VidraPayFakeMessage(bot_obj, uid)
            self.data = f"paid:{int(oid)}"

        async def answer(self, *args: Any, **kwargs: Any) -> None:
            return None

    try:
        from handlers.chat.instruction import handle_paid

        # handle_paid сам подтянет актуальную заявку из db.p2p.get_order_by_id(order_id)
        # и применит ту же логику, что используется в простой P2P-ветке.
        await handle_paid(_VidraPayFakeCallback(bot, user_id, order_id))  # type: ignore[arg-type]
    finally:
        with suppress(Exception):
            session = await bot.get_session()
            await session.close()


async def _cancel_web_order(order: Dict[str, Any]) -> None:
    order_id = int(order.get("order_id") or 0)
    user_id = int(order.get("user_id") or 0)
    operator_id = int(order.get("operator_id") or 0)

    db = await get_db()
    await db.execute(
        """
        UPDATE p2p_orders
           SET status = 'canceled'
         WHERE order_id = ?
           AND user_id = ?
           AND status != 'completed'
        """,
        (order_id, user_id),
    )
    await db.commit()

    msg = f"🚫 Пользователь WEB user_id {user_id} отменил оплату по заявке №<b>{order_id}</b>."
    ids = await _admin_ids()
    if operator_id and operator_id not in ids:
        ids.insert(0, operator_id)
    await _send_many_bot_messages(ids, msg)


def _build_view_order(order: Dict[str, Any], token: str) -> Dict[str, Any]:
    asset = _resolve_asset(order)
    amount_crypto = order.get("btc_amount")

    # total_rub — итоговая сумма заявки С КОМИССИЕЙ.
    pay_amount_rub = int(order.get("total_rub") or 0)
    if pay_amount_rub <= 0:
        pay_amount_rub = int(order.get("rub_amount") or 0)

    # rub_amount — фактическая сумма заявки ДО комиссии.
    receive_amount_rub = float(order.get("rub_amount") or 0)
    if receive_amount_rub <= 0:
        receive_amount_rub = float(pay_amount_rub or 0)

    return {
        "client_id": token,
        "order_id": order.get("order_id") or "",
        "status": order.get("status") or "pending",
        "amount": pay_amount_rub,
        "receive_amount": receive_amount_rub,
        "wallet": order.get("wallet") or "",
        "amount_crypto": amount_crypto,
        "crypto_asset": asset,
        "payment_method": order.get("payment_method") or "",
        "bank_card": order.get("bank_card") or "",
        "bank_name": order.get("bank_name") or "",
        "card_id": order.get("card_id") or 0,
    }


def _render_base_page(
    order: Dict[str, Any],
    token: str,
    main_html: str,
    error_text: str = "",
    extra_html: str = "",
    show_summary: bool = True,
) -> str:
    view = _build_view_order(order, token)

    client_id_raw = str(view.get("client_id") or "").strip()
    amount_raw = float(view.get("amount") or 0)
    receive_amount_raw = float(view.get("receive_amount") or 0)
    receive_amount = html.escape(_format_amount(receive_amount_raw))
    order_id = _safe_text(view.get("order_id") or client_id_raw)

    crypto_amount_raw = view.get("amount_crypto") or ""
    crypto_asset_raw = view.get("crypto_asset") or ""

    wallet_raw = view.get("wallet") or ""
    wallet_line = _safe_text(wallet_raw) if wallet_raw else "—"

    asset_names = {
        "BTC": "Bitcoin",
        "LTC": "Litecoin",
        "XMR": "Monero",
        "USDT": "Tether",
        "ETH": "Ethereum",
        "BNB": "Binance Coin",
        "TRX": "Tron",
        "TON": "Toncoin",
        "SOL": "Solana",
        "XRP": "Ripple",
        "DOGE": "Dogecoin",
    }

    crypto_asset = str(crypto_asset_raw or "").upper().strip()
    crypto_name = asset_names.get(crypto_asset, crypto_asset)

    crypto_line = "—"
    if crypto_amount_raw and crypto_asset:
        crypto_amount = _format_crypto_amount(crypto_amount_raw, crypto_asset)
        if crypto_name and crypto_name != crypto_asset:
            crypto_line = _safe_text(f"{crypto_amount} {crypto_asset} ({crypto_name})")
        else:
            crypto_line = _safe_text(f"{crypto_amount} {crypto_asset}")

    error_html = ""
    if error_text:
        error_html = f"""
            <div class="error-box">
                {html.escape(error_text)}
            </div>
        """

    summary_html = ""
    if show_summary:
        summary_html = f"""
                    <div class="summary">
                        <div class="summary-card primary compact-summary">
                            <div class="compact-summary-top">
                                <div>
                                    <div class="summary-label">К получению</div>
                                    <div class="summary-value amount">{crypto_line}</div>
                                    <div class="summary-rub">≈ {receive_amount} ₽</div>
                                </div>
                            </div>

                            <div class="compact-wallet">
                                <div class="summary-value wallet">{wallet_line}</div>
                            </div>
                        </div>
                    </div>
        """

    return f"""
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">

    <title>Vidra-Pay</title>

    <style>
        :root {{
            --bg: #08090b;
            --card: #111318;
            --card-soft: #171a21;
            --accent: #d6b35f;
            --accent-line: rgba(214, 179, 95, .28);
            --text: #f6f3ea;
            --muted: #a9acb4;
            --muted-soft: #747986;
            --line: rgba(255, 255, 255, .08);
            --danger-bg: rgba(255, 91, 91, .10);
            --ok-bg: rgba(70, 220, 145, .10);
        }}

        * {{
            box-sizing: border-box;
            -webkit-tap-highlight-color: transparent;
        }}

        html,
        body {{
            margin: 0;
            width: 100%;
            min-width: 0;
            min-height: 100%;
            overflow-x: hidden;
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
            scrollbar-width: none;
        }}

        body::-webkit-scrollbar {{
            width: 0;
            height: 0;
            display: none;
        }}

        body {{
            background:
                radial-gradient(circle at 20% -10%, rgba(214, 179, 95, .14), transparent 34%),
                radial-gradient(circle at 100% 0%, rgba(255, 255, 255, .05), transparent 28%),
                linear-gradient(180deg, #0b0c10 0%, #06070a 100%);
        }}

        .page {{
            width: 100%;
            max-width: 100%;
            min-height: 100svh;
            padding: 14px;
            overflow-x: hidden;
        }}

        .shell {{
            width: 100%;
            max-width: 520px;
            margin: 0 auto;
            overflow-x: hidden;
        }}

        .header {{
            display: flex;
            align-items: center;
            gap: 14px;
            padding: 12px 4px 10px;
        }}

        .logo {{
            width: 58px;
            height: 58px;
            flex: 0 0 58px;
            border-radius: 18px;
            object-fit: cover;
            border: 1px solid var(--accent-line);
            box-shadow: 0 10px 26px rgba(0, 0, 0, .28);
        }}

        .brand {{
            min-width: 0;
            flex: 1;
        }}

        .brand-title {{
            font-size: 25px;
            line-height: 1;
            font-weight: 850;
            letter-spacing: -.3px;
        }}

        .brand-subtitle {{
            margin-top: 6px;
            color: var(--muted);
            font-size: 13.5px;
            line-height: 1.25;
        }}

        .safe {{
            flex: 0 0 auto;
            padding: 8px 10px;
            border-radius: 999px;
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, .035);
            color: var(--muted);
            font-size: 12px;
            font-weight: 700;
            white-space: nowrap;
        }}

        .panel {{
            overflow: hidden;
            border: 1px solid var(--line);
            border-radius: 28px;
            background:
                radial-gradient(circle at 20% 0%, rgba(214, 179, 95, .08), transparent 30%),
                linear-gradient(180deg, rgba(23, 26, 33, .98), rgba(13, 15, 20, .98));
            box-shadow: 0 22px 60px rgba(0, 0, 0, .34);
        }}

        .content {{
            padding: 14px 18px 18px;
        }}

        .summary {{
            display: grid;
            gap: 7px;
            margin-top: 0;
            margin-bottom: 12px;
        }}

        .summary-card {{
            min-width: 0;
            padding: 10px 12px;
            border-radius: 16px;
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, .032);
        }}

        .summary-card.primary {{
            border-color: var(--accent-line);
            background:
                linear-gradient(135deg, rgba(214, 179, 95, .13), rgba(255, 255, 255, .024)),
                var(--card-soft);
        }}

        .summary-label {{
            color: var(--muted);
            font-size: 11.5px;
            line-height: 1.2;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: .45px;
        }}

        .summary-value {{
            margin-top: 6px;
            color: var(--text);
            font-size: 17px;
            line-height: 1.2;
            font-weight: 800;
            overflow-wrap: anywhere;
        }}

        .summary-value.amount {{
            color: var(--text);
            font-size: 20px;
            line-height: 1.12;
            font-weight: 850;
            letter-spacing: -.2px;
        }}

        .summary-rub {{
            margin-top: 4px;
            color: var(--accent);
            font-size: 12px;
            line-height: 1.2;
            font-weight: 700;
        }}

        .wallet {{
            font-size: 13px;
            line-height: 1.45;
            word-break: break-all;
            color: var(--text);
        }}

        .compact-summary {{
            display: grid;
            gap: 8px;
        }}

        .compact-summary-top {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
        }}

        .compact-wallet {{
            padding-top: 8px;
            border-top: 1px solid var(--line);
        }}

        .compact-wallet .wallet {{
            margin-top: 3px;
            color: #d9dbe0;
            font-size: 11.8px;
            line-height: 1.35;
        }}

        .section-title {{
            margin: 20px 0 10px;
            color: var(--muted);
            font-size: 13px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: .45px;
        }}

        .section-hint {{
            margin: -4px 0 12px;
            color: var(--muted);
            font-size: 13px;
            line-height: 1.4;
        }}

        .methods {{
            display: grid;
            gap: 10px;
        }}

        .banks-dropdown {{
            display: grid;
            gap: 10px;
            transform-origin: top center;
            animation: dropdownReveal .34s ease-out both;
        }}

        .banks-dropdown .method-card {{
            opacity: 0;
            transform: translateY(-8px) scale(.985);
            animation: bankItemReveal .32s ease-out both;
            animation-delay: calc(var(--i, 0) * 45ms);
        }}

        .method-card.any-bank {{
            border-color: rgba(214, 179, 95, .50);
            background:
                radial-gradient(circle at 14% 0%, rgba(214, 179, 95, .20), transparent 36%),
                linear-gradient(135deg, rgba(214, 179, 95, .14), rgba(255, 255, 255, .045));
            box-shadow: 0 14px 34px rgba(0, 0, 0, .20);
        }}

        .method-card.any-bank .method-icon {{
            background: rgba(214, 179, 95, .16);
            border-color: rgba(214, 179, 95, .38);
        }}

        .any-bank-badge {{
            display: inline-flex;
            align-items: center;
            margin-bottom: 4px;
            padding: 3px 7px;
            border-radius: 999px;
            border: 1px solid rgba(214, 179, 95, .30);
            background: rgba(214, 179, 95, .10);
            color: var(--accent);
            font-size: 10px;
            line-height: 1;
            font-weight: 900;
            text-transform: uppercase;
            letter-spacing: .35px;
        }}

        @keyframes dropdownReveal {{
            from {{
                opacity: 0;
                transform: translateY(-10px) scaleY(.96);
            }}
            to {{
                opacity: 1;
                transform: translateY(0) scaleY(1);
            }}
        }}

        @keyframes bankItemReveal {{
            from {{
                opacity: 0;
                transform: translateY(-8px) scale(.985);
            }}
            to {{
                opacity: 1;
                transform: translateY(0) scale(1);
            }}
        }}

        .method-card {{
            width: 100%;
            min-width: 0;
            display: grid;
            grid-template-columns: 46px minmax(0, 1fr) auto;
            align-items: center;
            gap: 12px;
            min-height: 74px;
            padding: 13px;
            border-radius: 20px;
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, .035);
            color: var(--text);
            text-decoration: none;
        }}

        .method-card:active {{
            transform: scale(.99);
        }}

        .method-card.disabled {{
            opacity: .42;
            pointer-events: none;
        }}

        .method-card.qr-disabled {{
            opacity: .34;
            cursor: not-allowed;
            pointer-events: none;
            filter: grayscale(1);
            border-color: rgba(255, 255, 255, .055);
            background: rgba(255, 255, 255, .018);
            box-shadow: none;
        }}

        .method-card.qr-disabled .method-icon {{
            color: rgba(255, 255, 255, .36);
            border-color: rgba(255, 255, 255, .09);
            background: rgba(255, 255, 255, .035);
        }}

        .method-card.qr-disabled .method-title {{
            color: rgba(246, 243, 234, .58);
        }}

        .method-card.qr-disabled .method-subtitle,
        .method-card.qr-disabled .method-pay-label,
        .method-card.qr-disabled .method-pay {{
            color: rgba(169, 172, 180, .48);
        }}

        .method-card.qr-disabled .method-side {{
            min-width: 86px;
        }}

        .method-card.qr-disabled .method-pay {{
            white-space: normal;
            font-size: 12px;
            line-height: 1.2;
        }}

        .method-icon {{
            width: 46px;
            height: 46px;
            border-radius: 16px;
            display: grid;
            place-items: center;
            border: 1px solid rgba(214, 179, 95, .22);
            background: rgba(214, 179, 95, .075);
            color: var(--accent);
            font-size: 22px;
            font-weight: 900;
        }}

        .method-info {{
            min-width: 0;
        }}

        .method-title {{
            font-size: 16.5px;
            line-height: 1.15;
            font-weight: 820;
            color: var(--text);
        }}

        .method-subtitle {{
            margin-top: 4px;
            color: var(--muted);
            font-size: 12.8px;
            line-height: 1.25;
        }}

        .method-side {{
            min-width: 86px;
            text-align: right;
        }}

        .method-pay-label {{
            color: var(--muted-soft);
            font-size: 10.5px;
            line-height: 1.1;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: .35px;
            white-space: nowrap;
        }}

        .method-pay {{
            margin-top: 4px;
            color: var(--accent);
            font-size: 14px;
            line-height: 1.15;
            font-weight: 850;
            white-space: nowrap;
        }}

        .details-box {{
            display: grid;
            gap: 9px;
            padding: 13px;
            border-radius: 22px;
            border: 1px solid var(--accent-line);
            background:
                radial-gradient(circle at 12% 0%, rgba(214, 179, 95, .12), transparent 34%),
                linear-gradient(180deg, rgba(255, 255, 255, .045), rgba(255, 255, 255, .02));
        }}

        .detail-row {{
            min-width: 0;
            padding: 10px 11px;
            border-radius: 16px;
            border: 1px solid rgba(255, 255, 255, .055);
            background: rgba(0, 0, 0, .12);
        }}

        .detail-label {{
            color: var(--muted-soft);
            font-size: 10.6px;
            line-height: 1.2;
            font-weight: 850;
            text-transform: uppercase;
            letter-spacing: .45px;
        }}

        .detail-value {{
            margin-top: 5px;
            color: var(--text);
            font-size: 15.5px;
            line-height: 1.25;
            font-weight: 820;
            overflow-wrap: anywhere;
        }}

        .detail-value.big {{
            color: var(--accent);
            font-size: 19px;
            letter-spacing: -.15px;
        }}

        .requisite-line {{
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            align-items: center;
            gap: 9px;
            margin-top: 5px;
        }}

        .requisite-line .detail-value {{
            margin-top: 0;
            padding: 9px 10px;
            border-radius: 14px;
            border: 1px solid rgba(214, 179, 95, .18);
            background: rgba(214, 179, 95, .055);
        }}

        .copy-btn {{
            min-height: 38px;
            padding: 0 12px;
            border: 1px solid rgba(214, 179, 95, .30);
            border-radius: 14px;
            background: rgba(214, 179, 95, .12);
            color: var(--accent);
            font: inherit;
            font-size: 12px;
            line-height: 1;
            font-weight: 850;
            cursor: pointer;
            white-space: nowrap;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, .05);
        }}

        .copy-btn:active {{
            transform: scale(.98);
        }}

        .copy-btn.copied {{
            background: rgba(70, 220, 145, .15);
            border-color: rgba(70, 220, 145, .32);
            color: #d8ffe8;
        }}

        .notice-box {{
            margin-top: 12px;
            display: grid;
            grid-template-columns: 34px minmax(0, 1fr);
            gap: 10px;
            align-items: start;
            padding: 12px;
            border-radius: 18px;
            border: 1px solid rgba(214, 179, 95, .18);
            background: rgba(214, 179, 95, .065);
            color: #eee4c7;
            font-size: 12.8px;
            line-height: 1.42;
        }}

        .notice-icon {{
            width: 34px;
            height: 34px;
            display: grid;
            place-items: center;
            border-radius: 13px;
            background: rgba(214, 179, 95, .12);
            color: var(--accent);
            font-size: 17px;
        }}

        .payment-actions {{
            display: grid;
            grid-template-columns: 1.35fr 1fr;
            gap: 10px;
            margin-top: 14px;
        }}

        .pay-action-btn {{
            min-height: 48px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            border-radius: 17px;
            text-decoration: none;
            font-size: 14px;
            line-height: 1;
            font-weight: 900;
            letter-spacing: -.05px;
            box-shadow: 0 14px 30px rgba(0, 0, 0, .18);
        }}

        .pay-action-btn:active {{
            transform: scale(.99);
        }}

        .pay-action-btn.paid {{
            background: linear-gradient(135deg, #e1c46f, #caa24e);
            color: #171209;
        }}

        .pay-action-btn.cancel-payment {{
            border: 1px solid rgba(255, 255, 255, .075);
            background: rgba(255, 255, 255, .045);
            color: var(--muted);
        }}

        .finish-box {{
            display: grid;
            gap: 12px;
            justify-items: center;
            text-align: center;
            padding: 22px 16px;
            border-radius: 24px;
            border: 1px solid rgba(214, 179, 95, .20);
            background:
                radial-gradient(circle at 50% 0%, rgba(214, 179, 95, .14), transparent 38%),
                rgba(255, 255, 255, .035);
        }}

        .finish-icon {{
            width: 58px;
            height: 58px;
            display: grid;
            place-items: center;
            border-radius: 22px;
            background: rgba(214, 179, 95, .12);
            color: var(--accent);
            font-size: 28px;
            border: 1px solid rgba(214, 179, 95, .22);
        }}

        .finish-title {{
            color: var(--text);
            font-size: 21px;
            line-height: 1.15;
            font-weight: 900;
            letter-spacing: -.25px;
        }}

        .finish-text {{
            max-width: 330px;
            color: var(--muted);
            font-size: 13.5px;
            line-height: 1.45;
        }}

        .error-box {{
            margin-top: 16px;
            padding: 13px 14px;
            border-radius: 18px;
            border: 1px solid rgba(255, 141, 141, .25);
            background: var(--danger-bg);
            color: #ffd4d4;
            font-size: 13.5px;
            line-height: 1.4;
        }}

        .footer {{
            display: grid;
            gap: 10px;
            padding: 15px 18px 18px;
            border-top: 1px solid var(--line);
            color: var(--muted);
            font-size: 12.5px;
            line-height: 1.35;
            background: rgba(0, 0, 0, .10);
        }}

        .footer-row {{
            min-width: 0;
            display: flex;
            justify-content: space-between;
            gap: 12px;
        }}

        .footer-value {{
            min-width: 0;
            color: #d9dbe0;
            text-align: right;
            overflow-wrap: anywhere;
        }}

        .confirm-backdrop {{
            position: fixed;
            inset: 0;
            z-index: 70;
            display: none;
            align-items: center;
            justify-content: center;
            padding: 18px;
            background: rgba(0, 0, 0, .62);
            backdrop-filter: blur(10px);
        }}

        .confirm-backdrop.open {{
            display: flex;
        }}

        .confirm-box {{
            width: 100%;
            max-width: 360px;
            border-radius: 24px;
            border: 1px solid var(--line);
            background:
                radial-gradient(circle at 16% 0%, rgba(214, 179, 95, .13), transparent 34%),
                linear-gradient(180deg, #181b22, #101218);
            box-shadow: 0 24px 80px rgba(0, 0, 0, .54);
            padding: 18px;
        }}

        .confirm-title {{
            color: var(--text);
            font-size: 19px;
            line-height: 1.2;
            font-weight: 850;
            letter-spacing: -.2px;
        }}

        .confirm-text {{
            margin-top: 9px;
            color: var(--muted);
            font-size: 13.5px;
            line-height: 1.45;
        }}

        .confirm-choice {{
            margin-top: 12px;
            padding: 11px 12px;
            border-radius: 16px;
            border: 1px solid var(--accent-line);
            background: rgba(214, 179, 95, .09);
            color: var(--accent);
            font-size: 14px;
            line-height: 1.25;
            font-weight: 850;
        }}

        .confirm-actions {{
            display: grid;
            grid-template-columns: 1fr 1.35fr;
            gap: 9px;
            margin-top: 16px;
        }}

        .confirm-btn {{
            min-height: 44px;
            border: 0;
            border-radius: 15px;
            font-size: 13.5px;
            line-height: 1.1;
            font-weight: 850;
            cursor: pointer;
        }}

        .confirm-btn.cancel {{
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, .045);
            color: var(--muted);
        }}

        .confirm-btn.ok {{
            background: var(--accent);
            color: #171209;
            box-shadow: 0 12px 28px rgba(214, 179, 95, .18);
        }}

        @media (max-width: 390px) {{
            .page {{
                padding: 10px;
            }}

            .header {{
                gap: 10px;
            }}

            .logo {{
                width: 50px;
                height: 50px;
                flex-basis: 50px;
                border-radius: 16px;
            }}

            .brand-title {{
                font-size: 22px;
            }}

            .brand-subtitle {{
                font-size: 12.5px;
            }}

            .safe {{
                display: none;
            }}

            .content {{
                padding: 18px 14px 14px;
            }}

            .summary-value.amount {{
                font-size: 19px;
            }}

            .method-card {{
                grid-template-columns: 42px minmax(0, 1fr) auto;
                gap: 10px;
                padding: 11px;
                min-height: 68px;
                border-radius: 18px;
            }}

            .method-icon {{
                width: 42px;
                height: 42px;
                border-radius: 14px;
                font-size: 20px;
            }}

            .method-title {{
                font-size: 15.5px;
            }}

            .method-subtitle {{
                font-size: 12px;
            }}

            .method-side {{
                min-width: 76px;
            }}

            .method-pay-label {{
                font-size: 9.5px;
            }}

            .method-pay {{
                font-size: 12.3px;
            }}

            .detail-value.big {{
                font-size: 17.5px;
            }}

            .requisite-line {{
                grid-template-columns: 1fr;
            }}

            .copy-btn {{
                width: 100%;
            }}

            .payment-actions {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>

<body>
    <main class="page">
        <div class="shell">
            <header class="header">
                <img class="logo" src="/static/img/vidra-pay-logo.jpg" alt="Vidra-Pay">

                <div class="brand">
                    <div class="brand-title">Vidra-Pay</div>
                    <div class="brand-subtitle">Безопасная оплата заявки</div>
                </div>

                <div class="safe">защищено</div>
            </header>

            <section class="panel">
                <div class="content">
                    {summary_html}

                    {error_html}

                    {main_html}
                </div>

                <footer class="footer">
                    <div class="footer-row">
                        <span>Номер заявки</span>
                        <span class="footer-value">#{order_id}</span>
                    </div>
                </footer>
            </section>
        </div>
    </main>

    {extra_html}
</body>
</html>
"""


def _render_method_choice_page(order: Dict[str, Any], token: str, error_text: str = "") -> str:
    view = _build_view_order(order, token)

    client_id_raw = str(view.get("client_id") or "").strip()
    amount_raw = float(view.get("amount") or 0)
    pay_amount_text = html.escape(_format_method_amount(amount_raw))
    token_q = quote(client_id_raw)

    method_cards: List[str] = []

    familiar_card = order.get("_familiar_card")
    familiar_available = False
    familiar_bank = ""
    familiar_card_id = 0

    if isinstance(familiar_card, dict) and familiar_card:
        familiar_bank_raw = str(familiar_card.get("bank_name") or "ваш банк").strip()
        familiar_bank = html.escape(familiar_bank_raw or "ваш банк")
        familiar_card_id = int(familiar_card.get("card_id") or 0)
        familiar_available = familiar_card_id > 0

    if familiar_available:
        method_cards.append(
            f"""
                <a
                    class="method-card any-bank"
                    href="/vidrapay/pay/{token_q}/method/familiar_card"
                    data-familiar-choice="{familiar_bank}"
                    data-familiar-url="/vidrapay/pay/{token_q}/method/familiar_card?confirm=1"
                    style="
                        border-color: rgba(214, 179, 95, .72);
                        background:
                            radial-gradient(circle at 12% 0%, rgba(214, 179, 95, .26), transparent 38%),
                            linear-gradient(135deg, rgba(214, 179, 95, .19), rgba(255, 255, 255, .045));
                        box-shadow:
                            0 18px 42px rgba(0, 0, 0, .30),
                            0 0 0 1px rgba(214, 179, 95, .18) inset;
                    "
                >
                    <div class="method-icon" style="
                        background: rgba(214, 179, 95, .22);
                        border-color: rgba(214, 179, 95, .46);
                    ">★</div>

                    <div class="method-info">
                        <div class="any-bank-badge">приоритетный вариант</div>
                        <div class="method-title">Проверенный банк</div>
                        <div class="method-subtitle">Ранее вы уже переводили через {familiar_bank}</div>
                    </div>

                    <div class="method-side">
                        <div class="method-pay-label">К оплате</div>
                        <div class="method-pay">{pay_amount_text} ₽</div>
                    </div>
                </a>
            """
        )
    else:
        method_cards.append(
            f"""
                <div class="method-card disabled">
                    <div class="method-icon">★</div>
                    <div class="method-info">
                        <div class="method-title">Проверенный банк</div>
                        <div class="method-subtitle">Нет доступных повторных реквизитов</div>
                    </div>
                    <div class="method-side"></div>
                </div>
            """
        )

    for key, method in VIDRAPAY_METHODS.items():
        title = html.escape(str(method.get("title") or key))
        subtitle = html.escape(str(method.get("subtitle") or ""))
        icon = str(method.get("icon") or "₽")
        enabled = bool(method.get("enabled"))

        if key == "qr":
            method_cards.append(
                f"""
                    <div class="method-card disabled qr-disabled" aria-disabled="true" title="Оплата по QR-коду временно в доработке">
                        <div class="method-icon">{icon}</div>
                        <div class="method-info">
                            <div class="method-title">{title}</div>
                            <div class="method-subtitle">Временно в доработке</div>
                        </div>
                        <div class="method-side">
                            <div class="method-pay-label">Статус</div>
                            <div class="method-pay">Скоро</div>
                        </div>
                    </div>
                """
            )
            continue

        if enabled:
            method_cards.append(
                f"""
                    <a
                        class="method-card"
                        href="/vidrapay/pay/{token_q}/method/{quote(key)}"
                    >
                        <div class="method-icon">{icon}</div>
                        <div class="method-info">
                            <div class="method-title">{title}</div>
                            <div class="method-subtitle">{subtitle}</div>
                        </div>
                        <div class="method-side">
                            <div class="method-pay-label">К оплате</div>
                            <div class="method-pay">{pay_amount_text} ₽</div>
                        </div>
                    </a>
                """
            )
        else:
            method_cards.append(
                f"""
                    <div class="method-card disabled">
                        <div class="method-icon">{icon}</div>
                        <div class="method-info">
                            <div class="method-title">{title}</div>
                            <div class="method-subtitle">{subtitle}</div>
                        </div>
                        <div class="method-side"></div>
                    </div>
                """
            )

    main_html = f"""
        <div class="methods" style="margin-top:14px;">
            {"".join(method_cards)}
        </div>
    """

    familiar_confirm_html = """
    <div class="confirm-backdrop" id="familiarConfirm" aria-hidden="true">
        <div class="confirm-box" role="dialog" aria-modal="true">
            <div class="confirm-title">Проверенный банк</div>
            <div class="confirm-text">
                Выберите этот способ только если можете перевести <b>с того же банка и с того же счёта</b>,
                с которого успешно оплачивали в прошлый раз.<br><br>
                Если такой возможности нет, лучше вернитесь назад и выберите другой способ оплаты,
                чтобы подобрать другой банк и новые реквизиты.
            </div>
            <div class="confirm-choice" id="familiarConfirmName">Банк</div>
            <div class="confirm-actions">
                <button class="confirm-btn cancel" type="button" data-familiar-cancel="1">Назад</button>
                <button class="confirm-btn ok" type="button" data-familiar-ok="1">Продолжить</button>
            </div>
        </div>
    </div>

    <script>
        (function () {
            var modal = document.getElementById('familiarConfirm');
            var nameBox = document.getElementById('familiarConfirmName');
            var okBtn = document.querySelector('[data-familiar-ok]');
            var cancelBtn = document.querySelector('[data-familiar-cancel]');
            var pendingUrl = '';
            var lockedScrollY = 0;

            function lockPage() {
                lockedScrollY = window.scrollY || document.documentElement.scrollTop || 0;
                document.body.style.position = 'fixed';
                document.body.style.top = '-' + lockedScrollY + 'px';
                document.body.style.left = '0';
                document.body.style.right = '0';
                document.body.style.width = '100%';
            }

            function unlockPage() {
                var top = document.body.style.top;
                document.body.style.position = '';
                document.body.style.top = '';
                document.body.style.left = '';
                document.body.style.right = '';
                document.body.style.width = '';
                if (top) window.scrollTo(0, Math.abs(parseInt(top, 10)) || lockedScrollY || 0);
            }

            function openModal(choice, url) {
                pendingUrl = url || '';
                if (nameBox) nameBox.textContent = choice || 'Проверенный банк';
                if (modal) {
                    modal.classList.add('open');
                    modal.setAttribute('aria-hidden', 'false');
                }
                lockPage();
            }

            function closeModal() {
                pendingUrl = '';
                if (modal) {
                    modal.classList.remove('open');
                    modal.setAttribute('aria-hidden', 'true');
                }
                unlockPage();
            }

            document.querySelectorAll('[data-familiar-url]').forEach(function (item) {
                item.addEventListener('click', function (event) {
                    event.preventDefault();
                    openModal(
                        item.getAttribute('data-familiar-choice') || 'Проверенный банк',
                        item.getAttribute('data-familiar-url') || item.getAttribute('href')
                    );
                });
            });

            if (okBtn) {
                okBtn.addEventListener('click', function () {
                    if (pendingUrl) window.location.href = pendingUrl;
                });
            }

            if (cancelBtn) cancelBtn.addEventListener('click', closeModal);

            if (modal) {
                modal.addEventListener('click', function (event) {
                    if (event.target === modal) closeModal();
                });
            }

            document.addEventListener('keydown', function (event) {
                if (event.key === 'Escape') closeModal();
            });
        })();
    </script>
    """

    return _render_base_page(order, token, main_html, error_text=error_text, extra_html=familiar_confirm_html)

def _render_bank_choice_page(
    order: Dict[str, Any],
    token: str,
    method_key: str,
    cards: List[Dict[str, Any]],
    error_text: str = "",
) -> str:
    view = _build_view_order(order, token)
    client_id_raw = str(view.get("client_id") or "").strip()
    amount_raw = float(view.get("amount") or 0)
    pay_amount_text = html.escape(_format_method_amount(amount_raw))

    method = VIDRAPAY_METHODS.get(method_key) or VIDRAPAY_METHODS["card"]
    method_title = html.escape(str(method.get("title") or "Способ оплаты"))
    icon = str(method.get("icon") or "🏦")
    method_key_q = quote(method_key)
    token_q = quote(client_id_raw)

    bank_cards = []
    if cards:
        bank_cards.append(
            f"""
                <a
                    class="method-card any-bank"
                    href="/vidrapay/pay/{token_q}/method/{method_key_q}/card/any"
                    data-confirm-choice="Любой банк"
                    data-confirm-url="/vidrapay/pay/{token_q}/method/{method_key_q}/card/any?confirm=1"
                    style="--i:0"
                >
                    <div class="method-icon">✨</div>
                    <div class="method-info">
                        <div class="any-bank-badge">если вашего банка нет</div>
                        <div class="method-title">Любой банк</div>
                        <div class="method-subtitle">Реквизиты будут подобраны автоматически</div>
                    </div>
                    <div class="method-side">
                        <div class="method-pay-label">К оплате</div>
                        <div class="method-pay">{pay_amount_text} ₽</div>
                    </div>
                </a>
            """
        )

    for index, card in enumerate(cards, start=1):
        card_id = int(card.get("card_id") or 0)
        if card_id <= 0:
            continue

        bank_name_raw = str(card.get("bank_name") or "Банк").strip()
        bank_name = html.escape(bank_name_raw)
        bank_cards.append(
            f"""
                <a
                    class="method-card"
                    href="/vidrapay/pay/{token_q}/method/{method_key_q}/card/{quote(str(card_id))}"
                    data-confirm-choice="{bank_name}"
                    data-confirm-url="/vidrapay/pay/{token_q}/method/{method_key_q}/card/{quote(str(card_id))}?confirm=1"
                    style="--i:{index}"
                >
                    <div class="method-icon">🏦</div>
                    <div class="method-info">
                        <div class="method-title">{bank_name}</div>
                        <div class="method-subtitle">Вы будете переводить именно с этого банка</div>
                    </div>
                    <div class="method-side">
                        <div class="method-pay-label">К оплате</div>
                        <div class="method-pay">{pay_amount_text} ₽</div>
                    </div>
                </a>
            """
        )

    if bank_cards:
        banks_html = f"""
            <div class="banks-dropdown">
                {''.join(bank_cards)}
            </div>
        """
    else:
        banks_html = """
            <div class="error-box">
                Сейчас нет доступных реквизитов для выбранного способа оплаты.
                Вернитесь в Telegram и ожидайте оператора.
            </div>
        """

    main_html = f"""
        <div class="methods" style="margin-top:14px;">
            {banks_html}
        </div>
    """

    confirm_html = """
    <div class="confirm-backdrop" id="choiceConfirm" aria-hidden="true">
        <div class="confirm-box" role="dialog" aria-modal="true" aria-labelledby="choiceConfirmTitle">
            <div class="confirm-title" id="choiceConfirmTitle">Подтвердите банк</div>
            <div class="confirm-text" id="choiceConfirmText">
                Переводите с того же банка, который выбрали из списка.
                Если вашего банка нет в списке, нажмите «Любой банк» — реквизиты будут подобраны автоматически.
            </div>
            <div class="confirm-choice" id="choiceConfirmName">Банк</div>
            <div class="confirm-actions">
                <button class="confirm-btn cancel" type="button" data-confirm-cancel="1">Назад</button>
                <button class="confirm-btn ok" type="button" data-confirm-ok="1">Показать реквизиты</button>
            </div>
        </div>
    </div>

    <script>
        (function () {
            var confirmModal = document.getElementById('choiceConfirm');
            var confirmName = document.getElementById('choiceConfirmName');
            var confirmText = document.getElementById('choiceConfirmText');
            var confirmOk = document.querySelector('[data-confirm-ok]');
            var confirmCancel = document.querySelector('[data-confirm-cancel]');
            var pendingUrl = '';
            var lockedScrollY = 0;

            function lockPage() {
                lockedScrollY = window.scrollY || document.documentElement.scrollTop || 0;
                document.body.style.position = 'fixed';
                document.body.style.top = '-' + lockedScrollY + 'px';
                document.body.style.left = '0';
                document.body.style.right = '0';
                document.body.style.width = '100%';
            }

            function unlockPage() {
                var top = document.body.style.top;
                document.body.style.position = '';
                document.body.style.top = '';
                document.body.style.left = '';
                document.body.style.right = '';
                document.body.style.width = '';

                if (top) {
                    window.scrollTo(0, Math.abs(parseInt(top, 10)) || lockedScrollY || 0);
                }
            }

            function hasOpenLayer() {
                return confirmModal && confirmModal.classList.contains('open');
            }

            function refreshLock() {
                if (hasOpenLayer()) {
                    if (document.body.style.position !== 'fixed') lockPage();
                } else {
                    unlockPage();
                }
            }

            function openConfirm(choice, url) {
                pendingUrl = url || '';
                var normalizedChoice = choice || 'Банк';
                if (confirmName) confirmName.textContent = normalizedChoice;
                if (confirmText) {
                    if (normalizedChoice.toLowerCase() === 'любой банк') {
                        confirmText.textContent = 'Если вашего банка нет в списке, можно выбрать «Любой банк». Реквизиты для оплаты будут подобраны автоматически.';
                    } else {
                        confirmText.textContent = 'Переводите с того же банка, который выбрали из списка. Это нужно, чтобы платёж прошёл быстрее и без лишних проверок.';
                    }
                }
                if (confirmModal) {
                    confirmModal.classList.add('open');
                    confirmModal.setAttribute('aria-hidden', 'false');
                }
                refreshLock();
            }

            function closeConfirm() {
                pendingUrl = '';
                if (confirmModal) {
                    confirmModal.classList.remove('open');
                    confirmModal.setAttribute('aria-hidden', 'true');
                }
                refreshLock();
            }

            document.querySelectorAll('[data-confirm-url]').forEach(function (item) {
                item.addEventListener('click', function (event) {
                    event.preventDefault();
                    openConfirm(
                        item.getAttribute('data-confirm-choice') || item.textContent.trim(),
                        item.getAttribute('data-confirm-url') || item.getAttribute('href')
                    );
                });
            });

            if (confirmOk) {
                confirmOk.addEventListener('click', function () {
                    if (pendingUrl) window.location.href = pendingUrl;
                });
            }

            if (confirmCancel) {
                confirmCancel.addEventListener('click', closeConfirm);
            }

            if (confirmModal) {
                confirmModal.addEventListener('click', function (event) {
                    if (event.target === confirmModal) closeConfirm();
                });
            }

            document.addEventListener('keydown', function (event) {
                if (event.key === 'Escape') closeConfirm();
            });
        })();
    </script>
    """

    return _render_base_page(order, token, main_html, error_text=error_text, extra_html=confirm_html)


def _render_payment_details_page(
    order: Dict[str, Any],
    token: str,
    method_key: str,
    card: Optional[Dict[str, Any]] = None,
) -> str:
    view = _build_view_order(order, token)
    method = VIDRAPAY_METHODS.get(method_key) or VIDRAPAY_METHODS["card"]

    method_title = html.escape(str(method.get("title") or "Способ оплаты"))
    requisites_label = html.escape(str(method.get("requisites_label") or "Реквизиты"))
    amount_text = html.escape(_format_method_amount(view.get("amount") or 0))

    bank_name = str(order.get("bank_name") or "").strip()
    requisites = str(order.get("bank_card") or "").strip()

    if card:
        bank_name = str(card.get("bank_name") or bank_name).strip()
        card_field = str(method.get("card_field") or "").strip()
        if card_field:
            requisites = str(card.get(card_field) or requisites).strip()

    main_html = f"""
        <div class="notice-box" style="margin-top:4px; margin-bottom:12px;">
            <div class="notice-icon">!</div>
            <div>
                Переводите <b>строго на эти реквизиты</b> и, по возможности, с выбранного банка.
                После отправки перевода нажмите <b>Оплатил</b> — оператор проверит платёж и запустит обмен.
            </div>
        </div>

        <div class="details-box">
            <div class="detail-row">
                <div class="detail-label">Способ оплаты</div>
                <div class="detail-value">{method_title}</div>
            </div>

            <div class="detail-row">
                <div class="detail-label">Банк получателя</div>
                <div class="detail-value">{_safe_text(bank_name)}</div>
            </div>

            <div class="detail-row">
                <div class="detail-label">{requisites_label}</div>
                <div class="requisite-line">
                    <div class="detail-value big" id="payRequisite">{_safe_text(requisites)}</div>
                    <button class="copy-btn" type="button" data-copy-target="payRequisite" aria-label="Скопировать реквизиты">⧉ Скопировать</button>
                </div>
            </div>

            <div class="detail-row">
                <div class="detail-label">Сумма к оплате</div>
                <div class="detail-value big">{amount_text} ₽</div>
            </div>
        </div>

        <div class="payment-actions">
            <a class="pay-action-btn paid" href="/vidrapay/pay/{quote(str(token))}/paid">✓ Оплатил</a>
            <a class="pay-action-btn cancel-payment" href="/vidrapay/pay/{quote(str(token))}/cancel">Отменить</a>
        </div>
    """

    copy_script = """
    <script>
        (function () {
            document.querySelectorAll('[data-copy-target]').forEach(function (button) {
                button.addEventListener('click', function () {
                    var targetId = button.getAttribute('data-copy-target');
                    var target = document.getElementById(targetId);
                    var text = target ? (target.textContent || '').trim() : '';
                    if (!text) return;

                    function done() {
                        var oldText = button.textContent;
                        button.classList.add('copied');
                        button.textContent = '✓ Скопировано';
                        setTimeout(function () {
                            button.classList.remove('copied');
                            button.textContent = oldText;
                        }, 1400);
                    }

                    function fail() {
                        var oldText = button.textContent;
                        button.textContent = 'Зажмите и скопируйте';
                        setTimeout(function () { button.textContent = oldText; }, 1600);
                    }

                    if (navigator.clipboard && window.isSecureContext && navigator.clipboard.writeText) {
                        navigator.clipboard.writeText(text).then(done).catch(function () {
                            if (fallbackCopy(text)) done(); else fail();
                        });
                    } else {
                        if (fallbackCopy(text)) done(); else fail();
                    }
                });
            });

            function fallbackCopy(text) {
                var input = document.createElement('textarea');
                input.value = text;
                input.setAttribute('readonly', 'readonly');
                input.style.position = 'fixed';
                input.style.left = '-9999px';
                document.body.appendChild(input);
                input.select();
                var ok = false;
                try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
                document.body.removeChild(input);
                return ok;
            }
        })();
    </script>
    """

    return _render_base_page(order, token, main_html, extra_html=copy_script, show_summary=False)


def _render_qr_payment_page(order: Dict[str, Any], token: str) -> str:
    view = _build_view_order(order, token)

    qr_url = str(order.get("bank_card") or "").strip()
    amount_text = html.escape(_format_method_amount(view.get("amount") or 0))
    qr_url_html = html.escape(qr_url)
    qr_q = quote(qr_url, safe="")
    token_q = quote(str(token))

    main_html = f"""
        <div class="notice-box" style="margin-top:4px; margin-bottom:12px;">
            <div class="notice-icon">▦</div>
            <div>
                Отсканируйте QR-код камерой телефона или банковским приложением.
                Оплачивайте <b>ровно указанную сумму</b>. После перевода нажмите <b>Оплатил</b>.
            </div>
        </div>

        <div class="details-box">
            <div class="detail-row">
                <div class="detail-label">Способ оплаты</div>
                <div class="detail-value">НСПК / Оплата по QR</div>
            </div>

            <div class="detail-row">
                <div class="detail-label">Сумма к оплате</div>
                <div class="detail-value big">{amount_text} ₽</div>
            </div>

            <div style="
                display:grid;
                justify-items:center;
                gap:12px;
                padding:16px 12px;
                border-radius:20px;
                border:1px solid rgba(214, 179, 95, .18);
                background:rgba(0,0,0,.14);
            ">
                <div style="
                    padding:12px;
                    border-radius:22px;
                    background:#fff;
                    box-shadow:0 16px 38px rgba(0,0,0,.28);
                ">
                    <img
                        src="https://api.qrserver.com/v1/create-qr-code/?size=260x260&data={qr_q}"
                        alt="QR-код для оплаты НСПК"
                        style="display:block;width:260px;max-width:72vw;height:260px;max-height:72vw;border-radius:12px;"
                    >
                </div>

                <a
                    href="{qr_url_html}"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="pay-action-btn paid"
                    style="width:100%;max-width:300px;"
                >
                    Открыть оплату
                </a>
            </div>

            <div class="detail-row">
                <div class="detail-label">QR-ссылка</div>
                <div class="requisite-line">
                    <div class="detail-value" id="qrLinkValue">{qr_url_html}</div>
                    <button class="copy-btn" type="button" data-copy-target="qrLinkValue">⧉ Скопировать</button>
                </div>
            </div>
        </div>

        <div class="payment-actions">
            <a class="pay-action-btn paid" href="/vidrapay/pay/{token_q}/paid">✓ Оплатил</a>
            <a class="pay-action-btn cancel-payment" href="/vidrapay/pay/{token_q}/cancel">Отменить</a>
        </div>
    """

    copy_script = """
    <script>
        (function () {
            document.querySelectorAll('[data-copy-target]').forEach(function (button) {
                button.addEventListener('click', function () {
                    var targetId = button.getAttribute('data-copy-target');
                    var target = document.getElementById(targetId);
                    var text = target ? (target.textContent || '').trim() : '';
                    if (!text) return;

                    function done() {
                        var oldText = button.textContent;
                        button.classList.add('copied');
                        button.textContent = '✓ Скопировано';
                        setTimeout(function () {
                            button.classList.remove('copied');
                            button.textContent = oldText;
                        }, 1400);
                    }

                    function fail() {
                        var oldText = button.textContent;
                        button.textContent = 'Зажмите и скопируйте';
                        setTimeout(function () { button.textContent = oldText; }, 1600);
                    }

                    if (navigator.clipboard && window.isSecureContext && navigator.clipboard.writeText) {
                        navigator.clipboard.writeText(text).then(done).catch(function () {
                            if (fallbackCopy(text)) done(); else fail();
                        });
                    } else {
                        if (fallbackCopy(text)) done(); else fail();
                    }
                });
            });

            function fallbackCopy(text) {
                var input = document.createElement('textarea');
                input.value = text;
                input.setAttribute('readonly', 'readonly');
                input.style.position = 'fixed';
                input.style.left = '-9999px';
                document.body.appendChild(input);
                input.select();
                var ok = false;
                try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
                document.body.removeChild(input);
                return ok;
            }
        })();
    </script>
    """

    return _render_base_page(order, token, main_html, extra_html=copy_script, show_summary=False)


async def _create_and_render_nirvana_qr_payment(
    order: Dict[str, Any],
    token: str,
    request: web.Request,
) -> web.Response:
    order_id = int(order.get("order_id") or 0)
    user_id = int(order.get("user_id") or 0)

    amount = int(order.get("total_rub") or 0)
    if amount <= 0:
        amount = int(order.get("rub_amount") or 0)

    if order_id <= 0 or user_id <= 0 or amount <= 0:
        return _no_cache_response(
            _render_method_choice_page(
                order,
                token,
                "Не удалось подготовить оплату по QR: неверные данные заявки.",
            ),
            status=400,
        )

    method = VIDRAPAY_METHODS.get("qr") or {}
    method_db = str(method.get("db_value") or "vidrapay_qr")

    try:
        await _set_payment_method(order_id, method_db)
        order["payment_method"] = method_db
    except Exception:
        logger.exception("Failed to set VidraPay QR payment method for order_id=%s", order_id)

    try:
        forwarded_for = str(request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        peername = request.transport.get_extra_info("peername") if request.transport else None
        peer_ip = ""
        if peername and isinstance(peername, tuple) and peername:
            peer_ip = str(peername[0] or "").strip()

        user_ip = forwarded_for or peer_ip or "127.0.0.1"
        user_agent = str(request.headers.get("User-Agent") or "TelegramBot").strip() or "TelegramBot"

        payment = await create_nirvana_ns_pk_qr_order(
            p2p_order_id=order_id,
            tg_user_id=user_id,
            amount=amount,
            user_ip=user_ip,
            user_agent=user_agent,
            user_email=f"user{user_id}@vidrapay.local",
        )

    except NirvanaAPIError as exc:
        logger.exception("Nirvana QR order failed for VidraPay order_id=%s: %s", order_id, exc)

        error_text = str(exc)
        if "ликвид" in error_text.lower():
            user_error = (
                "Оплата по QR сейчас временно недоступна: Vidra-Pay не нашёл свободный QR-реквизит "
                "на эту сумму. Выберите другой способ оплаты или попробуйте QR чуть позже."
            )
        elif "not active" in error_text.lower() or "active account" in error_text.lower():
            user_error = (
                "Оплата по QR сейчас временно недоступна на стороне провайдера. "
                "Выберите другой способ оплаты или попробуйте позже."
            )
        else:
            user_error = (
                "NirvanaPay временно не выдал QR-ссылку. "
                "Попробуйте другой способ оплаты или повторите позже."
            )

        return _no_cache_response(
            _render_method_choice_page(order, token, user_error),
            status=200,
        )

    except Exception:
        logger.exception("Failed to create Nirvana QR order for VidraPay order_id=%s", order_id)
        return _no_cache_response(
            _render_method_choice_page(
                order,
                token,
                "Не удалось создать оплату по QR. Попробуйте другой способ оплаты или повторите позже.",
            ),
            status=200,
        )

    qr_url = str(payment.get("receiver") or payment.get("redirect_url") or "").strip()
    if not qr_url:
        logger.error("Nirvana QR order has no receiver order_id=%s response=%r", order_id, payment.get("raw"))
        return _no_cache_response(
            _render_method_choice_page(
                order,
                token,
                "NirvanaPay создал заявку, но не вернул QR-ссылку. Попробуйте другой способ оплаты.",
            ),
            status=200,
        )

    try:
        db = await get_db()
        await db.execute(
            """
            UPDATE p2p_orders
               SET bank_card = ?,
                   bank_name = ?,
                   payment_method = ?,
                   card_id = 0
             WHERE order_id = ?
               AND user_id = ?
            """,
            (
                qr_url,
                "НСПК QR",
                method_db,
                order_id,
                user_id,
            ),
        )
        await db.commit()

        order["bank_card"] = qr_url
        order["bank_name"] = "НСПК QR"
        order["payment_method"] = method_db
        order["card_id"] = 0
    except Exception:
        logger.exception("Failed to save VidraPay QR requisites for order_id=%s", order_id)

    return _no_cache_response(_render_qr_payment_page(order, token), status=200)


async def vidrapay_pay_page(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()

    if not token:
        return web.Response(text="token is required", status=400)

    try:
        order_id, user_id = parse_vidrapay_token(token)
    except Exception:
        return web.Response(text="Платёжная ссылка недействительна", status=403)

    order = await _get_p2p_order(order_id, user_id)
    if not order:
        return web.Response(text="Заявка не найдена", status=404)

    status = str(order.get("status") or "").lower().strip()
    if status not in ("pending", ""):
        return _no_cache_response(
            _render_finish_page(
                order,
                token,
                "Заявка недоступна",
                "Эта заявка уже закрыта или недоступна для оплаты.",
                show_summary=False,
            )
        )

    if order.get("payment_method") and order.get("bank_card") and order.get("bank_name"):
        method_key = _method_key_from_db_value(order.get("payment_method"))
        if method_key == "qr":
            return _no_cache_response(_render_qr_payment_page(order, token))
        if method_key in VIDRAPAY_METHODS:
            return _no_cache_response(_render_payment_details_page(order, token, method_key))

    try:
        familiar_card = await _get_user_last_success_card(
            user_id=user_id,
            method_key="card",
            order_id=order_id,
        )
        if familiar_card:
            order["_familiar_card"] = familiar_card
    except Exception:
        logger.exception(
            "Failed to load familiar VidraPay card for user_id=%s order_id=%s",
            user_id,
            order_id,
        )

    return _no_cache_response(_render_method_choice_page(order, token))


async def vidrapay_select_method(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()
    method_key = str(request.match_info.get("method_key") or "").strip().lower()

    if not token:
        return web.Response(text="token is required", status=400)

    try:
        order_id, user_id = parse_vidrapay_token(token)
    except Exception:
        return web.Response(text="Платёжная ссылка недействительна", status=403)

    order = await _get_p2p_order(order_id, user_id)
    if not order:
        return web.Response(text="Заявка не найдена", status=404)

    status = str(order.get("status") or "").lower().strip()
    if status not in ("pending", ""):
        return _no_cache_response(
            _render_finish_page(
                order,
                token,
                "Заявка недоступна",
                "Эта заявка уже закрыта или недоступна для оплаты.",
                show_summary=False,
            )
        )

    if order.get("payment_method") and order.get("bank_card") and order.get("bank_name"):
        saved_method_key = _method_key_from_db_value(order.get("payment_method"))
        if saved_method_key in VIDRAPAY_METHODS:
            return _no_cache_response(_render_payment_details_page(order, token, saved_method_key))

    if method_key == "familiar_card":
        if str(request.query.get("confirm") or "").strip() != "1":
            try:
                familiar_card = await _get_user_last_success_card(
                    user_id=user_id,
                    method_key="card",
                    order_id=order_id,
                )
                if familiar_card:
                    order["_familiar_card"] = familiar_card
            except Exception:
                logger.exception(
                    "Failed to load familiar VidraPay card before confirm order_id=%s",
                    order_id,
                )

            return _no_cache_response(_render_method_choice_page(order, token), status=200)

        selected_card = await _get_user_last_success_card(
            user_id=user_id,
            method_key="card",
            order_id=order_id,
        )
        if not selected_card:
            return _no_cache_response(
                _render_method_choice_page(
                    order,
                    token,
                    "Проверенный банк сейчас недоступен. Выберите другой способ оплаты.",
                ),
                status=404,
            )

        method = VIDRAPAY_METHODS["card"]
        method_db = str(method.get("db_value") or "vidrapay_card")
        card_field = str(method.get("card_field") or "card_number").strip()
        requisites = str(selected_card.get(card_field) or "").strip()
        selected_card_id = int(selected_card.get("card_id") or 0)

        if not requisites or selected_card_id <= 0:
            return _no_cache_response(
                _render_method_choice_page(
                    order,
                    token,
                    "Для проверенного банка сейчас нет реквизитов. Выберите другой способ оплаты.",
                ),
                status=404,
            )

        claimed = await _claim_vidrapay_distribution_card(
            order_id=order_id,
            user_id=user_id,
            method_key="card",
            card=selected_card,
        )

        if not claimed:
            return _no_cache_response(
                _render_method_choice_page(
                    order,
                    token,
                    "Проверенный банк уже был выдан другому пользователю. Выберите другой способ оплаты.",
                ),
                status=409,
            )

        try:
            await _set_payment_card(order_id, method_db, selected_card, requisites)
            order["payment_method"] = method_db
            order["bank_card"] = requisites
            order["bank_name"] = str(selected_card.get("bank_name") or "").strip()
            order["card_id"] = selected_card_id
        except Exception:
            await _release_vidrapay_distribution_card(order_id, selected_card_id)
            logger.exception(
                "Failed to set familiar VidraPay card for order_id=%s card_id=%s",
                order_id,
                selected_card_id,
            )
            return _no_cache_response(
                _render_method_choice_page(
                    order,
                    token,
                    "Не удалось сохранить проверенный банк. Попробуйте другой способ оплаты.",
                ),
                status=500,
            )

        return _no_cache_response(_render_payment_details_page(order, token, "card", selected_card))

    method = VIDRAPAY_METHODS.get(method_key)
    if not method:
        return web.Response(text="Неизвестный способ оплаты", status=400)

    if not bool(method.get("enabled")):
        return web.Response(text="Этот способ оплаты пока недоступен", status=403)

    if method_key == "qr":
        try:
            familiar_card = await _get_user_last_success_card(
                user_id=user_id,
                method_key="card",
                order_id=order_id,
            )
            if familiar_card:
                order["_familiar_card"] = familiar_card
        except Exception:
            logger.exception(
                "Failed to reload familiar VidraPay card after disabled QR click order_id=%s",
                order_id,
            )

        return _no_cache_response(
            _render_method_choice_page(
                order,
                token,
                "Оплата по QR-коду сейчас временно в доработке. Выберите другой способ оплаты.",
            ),
            status=200,
        )

    method_db = str(method.get("db_value") or f"vidrapay_{method_key}")

    try:
        await _set_payment_method(order_id, method_db)
        order["payment_method"] = method_db
    except Exception:
        logger.exception("Failed to set VidraPay payment method for order_id=%s", order_id)

    try:
        cards = await _get_cards_for_method(method_key, order_id=order_id)
    except Exception:
        logger.exception("Failed to load cards for VidraPay method=%s order_id=%s", method_key, order_id)
        cards = []

    return _no_cache_response(_render_bank_choice_page(order, token, method_key, cards))


async def vidrapay_select_card(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()
    method_key = str(request.match_info.get("method_key") or "").strip().lower()
    card_id_raw = str(request.match_info.get("card_id") or "").strip()

    method = VIDRAPAY_METHODS.get(method_key)
    if not method:
        return web.Response(text="Неизвестный способ оплаты", status=400)

    if not bool(method.get("enabled")):
        return web.Response(text="Этот способ оплаты пока недоступен", status=403)

    is_any_bank = card_id_raw.lower() == "any"
    card_id = 0
    if not is_any_bank:
        try:
            card_id = int(card_id_raw)
        except (TypeError, ValueError):
            return web.Response(text="Некорректный банк", status=400)

    if not token:
        return web.Response(text="token is required", status=400)

    try:
        order_id, user_id = parse_vidrapay_token(token)
    except Exception:
        return web.Response(text="Платёжная ссылка недействительна", status=403)

    order = await _get_p2p_order(order_id, user_id)
    if not order:
        return web.Response(text="Заявка не найдена", status=404)

    status = str(order.get("status") or "").lower().strip()
    if status not in ("pending", ""):
        return _no_cache_response(
            _render_finish_page(
                order,
                token,
                "Заявка недоступна",
                "Эта заявка уже закрыта или недоступна для оплаты.",
                show_summary=False,
            )
        )

    if order.get("payment_method") and order.get("bank_card") and order.get("bank_name"):
        saved_method_key = _method_key_from_db_value(order.get("payment_method"))
        if saved_method_key in VIDRAPAY_METHODS:
            return _no_cache_response(_render_payment_details_page(order, token, saved_method_key))

    cards = await _get_cards_for_method(method_key, order_id=order_id)
    all_cards = await _get_all_cards_for_method(method_key, order_id=order_id)

    if str(request.query.get("confirm") or "").strip() != "1":
        return _no_cache_response(
            _render_bank_choice_page(order, token, method_key, cards),
            status=200,
        )

    if is_any_bank:
        selected_card = random.choice(all_cards) if all_cards else None
    else:
        selected_card = next(
            (card for card in cards if int(card.get("card_id") or 0) == card_id),
            None,
        )

    if not selected_card:
        return web.Response(
            text=_render_bank_choice_page(
                order,
                token,
                method_key,
                cards,
                "Выбранный банк сейчас недоступен. Выберите другой банк.",
            ),
            content_type="text/html",
            status=404,
        )

    card_field = str(method.get("card_field") or "").strip()
    requisites = str(selected_card.get(card_field) or "").strip() if card_field else ""
    if not requisites:
        return web.Response(
            text=_render_bank_choice_page(
                order,
                token,
                method_key,
                cards,
                "Для выбранного банка нет реквизитов. Выберите другой банк.",
            ),
            content_type="text/html",
            status=404,
        )

    method_db = str(method.get("db_value") or f"vidrapay_{method_key}")
    selected_card_id = int(selected_card.get("card_id") or 0)

    claimed = await _claim_vidrapay_distribution_card(
        order_id=order_id,
        user_id=user_id,
        method_key=method_key,
        card=selected_card,
    )

    if not claimed:
        return web.Response(
            text=_render_bank_choice_page(
                order,
                token,
                method_key,
                cards,
                "Эти реквизиты уже были выданы другому пользователю. Выберите другой банк.",
            ),
            content_type="text/html",
            status=409,
        )

    try:
        await _set_payment_card(order_id, method_db, selected_card, requisites)
        order["payment_method"] = method_db
        order["bank_card"] = requisites
        order["bank_name"] = str(selected_card.get("bank_name") or "").strip()
        order["card_id"] = selected_card_id

    except Exception:
        await _release_vidrapay_distribution_card(order_id, selected_card_id)
        logger.exception(
            "Failed to set VidraPay selected card for order_id=%s method=%s card_id=%s",
            order_id,
            method_key,
            selected_card_id,
        )
        return web.Response(
            text=_render_bank_choice_page(
                order,
                token,
                method_key,
                cards,
                "Не удалось сохранить выбранный банк. Попробуйте ещё раз.",
            ),
            content_type="text/html",
            status=500,
        )

    return _no_cache_response(_render_payment_details_page(order, token, method_key, selected_card))



def _render_finish_page(
    order: Dict[str, Any],
    token: str,
    title: str,
    text: str,
    *,
    auto_close: bool = False,
    show_summary: bool = True,
) -> str:
    lowered_title = str(title or "").lower()

    close_script = ""
    if auto_close:
        close_script = """
        <script>
            (function () {
                setTimeout(function () {
                    try {
                        if (window.Telegram && window.Telegram.WebApp) {
                            window.Telegram.WebApp.close();
                            return;
                        }
                    } catch (e) {}
                    try { window.close(); } catch (e) {}
                }, 900);
            })();
        </script>
        """

    icon = "✓"
    if "отмен" in lowered_title:
        icon = "×"
    elif "недоступ" in lowered_title:
        icon = "!"

    main_html = f"""
        <div class="finish-box">
            <div class="finish-icon">{icon}</div>
            <div class="finish-title">{html.escape(title)}</div>
            <div class="finish-text">{html.escape(text)}</div>
        </div>
    """
    return _render_base_page(order, token, main_html, extra_html=close_script, show_summary=show_summary)


async def vidrapay_mark_paid(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()

    if not token:
        return web.Response(text="token is required", status=400)

    try:
        order_id, user_id = parse_vidrapay_token(token)
    except Exception:
        return web.Response(text="Платёжная ссылка недействительна", status=403)

    order = await _get_p2p_order(order_id, user_id)
    if not order:
        return web.Response(text="Заявка не найдена", status=404)

    status = str(order.get("status") or "").lower().strip()
    if status not in ("pending", ""):
        return _no_cache_response(
            _render_finish_page(
                order,
                token,
                "Заявка недоступна",
                "Эта заявка уже закрыта или недоступна для оплаты.",
                show_summary=False,
            )
        )

    if not str(order.get("bank_card") or "").strip():
        return _no_cache_response(
            _render_method_choice_page(order, token, "Сначала выберите банк и получите реквизиты."),
            status=400,
        )

    await _delete_vidrapay_payment_message(order)

    try:
        await _notify_paid_to_admins(order)
    except Exception:
        logger.exception("Failed to notify admins about WEB paid order_id=%s", order_id)

    return _no_cache_response(
        _render_finish_page(
            order,
            token,
            "Оплата отмечена",
            "Уведомление отправлено администраторам. Вернитесь в Telegram и ожидайте подтверждения обмена.",
            auto_close=True,
            show_summary=False,
        )
    )


async def vidrapay_cancel_payment(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()

    if not token:
        return web.Response(text="token is required", status=400)

    try:
        order_id, user_id = parse_vidrapay_token(token)
    except Exception:
        return web.Response(text="Платёжная ссылка недействительна", status=403)

    order = await _get_p2p_order(order_id, user_id)
    if not order:
        return web.Response(text="Заявка не найдена", status=404)

    status = str(order.get("status") or "").lower().strip()
    if status not in ("pending", ""):
        return _no_cache_response(
            _render_finish_page(
                order,
                token,
                "Заявка недоступна",
                "Эта заявка уже закрыта или недоступна для оплаты.",
                show_summary=False,
            )
        )

    payment_already_marked = False
    with suppress(Exception):
        from db.p2p import try_claim_p2p_action
        payment_already_marked = not await try_claim_p2p_action(order_id, "operator_paid_notify_web")

    if payment_already_marked:
        return _no_cache_response(
            _render_finish_page(
                order,
                token,
                "Оплата уже отмечена",
                "Отменить заявку после отметки оплаты нельзя. Вернитесь в Telegram и ожидайте подтверждения обмена.",
                show_summary=False,
            )
        )

    await _delete_vidrapay_payment_message(order)

    try:
        await _cancel_web_order(order)
        order["status"] = "canceled"
    except Exception:
        logger.exception("Failed to cancel WEB order_id=%s", order_id)

    return _no_cache_response(
        _render_finish_page(
            order,
            token,
            "Оплата отменена",
            "Заявка отменена. Вернитесь в Telegram для дальнейших действий.",
            auto_close=True,
            show_summary=False,
        )
    )


app = web.Application()
app.router.add_static("/static/", path="webapp/static", name="static")
app.router.add_get("/vidrapay/pay/{token}", vidrapay_pay_page)
app.router.add_get("/vidrapay/pay/{token}/method/{method_key}", vidrapay_select_method)
app.router.add_get("/vidrapay/pay/{token}/method/{method_key}/card/{card_id}", vidrapay_select_card)
app.router.add_get("/vidrapay/pay/{token}/paid", vidrapay_mark_paid)
app.router.add_get("/vidrapay/pay/{token}/cancel", vidrapay_cancel_payment)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    web.run_app(
        app,
        host="127.0.0.1",
        port=8085,
    )
