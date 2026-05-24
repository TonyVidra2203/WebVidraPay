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
from services.vidrapay_payin import parse_vidrapay_token
from services.nirvana import NirvanaAPIError
from services.nirvana_payin import create_nirvana_ns_pk_qr_order

logger = logging.getLogger(__name__)


ORDER_ASSET_COMMENT_RE = re.compile(r"\((BTC|LTC|USDT|XMR)\)", re.IGNORECASE)

VIDRAPAY_CARD_MAX_SUCCESS_PAYMENTS = 3
VIDRAPAY_CARD_WINDOW_HOURS = 20
VIDRAPAY_CARD_COOLDOWN_HOURS = 1

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
        "title": "Оплата по QR",
        "subtitle": "Оплата через НСПК / QR-код",
        "db_value": "vidrapay_qr",
        "icon": "▦",
        "enabled": True,
        "card_field": "",
        "requisites_label": "QR-код",
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


async def _is_vidrapay_card_available(card_id: int) -> bool:
    """
    Проверяет лимиты выдачи карты для VidraPay.

    Карта считается недоступной, если:
    - по ней уже было 3 успешных оплаты за последние 20 часов;
    - или после последней успешной оплаты прошло меньше 1 часа.

    Учитываются только завершённые заявки p2p_orders.status = 'completed'.
    Время успешной оплаты берётся из completed_at, а если оно пустое — из created_at.
    """
    try:
        cid = int(card_id or 0)
    except (TypeError, ValueError):
        return False

    if cid <= 0:
        return False

    try:
        db = await get_db()
        cur = await db.execute(
            """
            WITH completed AS (
                SELECT
                    datetime(
                        COALESCE(
                            NULLIF(TRIM(COALESCE(completed_at, '')), ''),
                            NULLIF(TRIM(COALESCE(created_at, '')), '')
                        )
                    ) AS paid_at
                FROM p2p_orders
                WHERE card_id = ?
                  AND status = 'completed'
            )
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN paid_at IS NOT NULL
                         AND paid_at >= datetime('now', ?)
                        THEN 1
                        ELSE 0
                    END
                ), 0) AS payments_in_window,
                MAX(paid_at) AS last_paid_at
            FROM completed
            """,
            (
                cid,
                f"-{VIDRAPAY_CARD_WINDOW_HOURS} hours",
            ),
        )
        row = await cur.fetchone()
        await cur.close()
    except Exception:
        logger.exception("Failed to check VidraPay card limits for card_id=%s", card_id)
        return True

    payments_in_window = 0
    last_paid_at = ""

    if row:
        try:
            payments_in_window = int(row[0] or 0)
        except (TypeError, ValueError):
            payments_in_window = 0
        last_paid_at = str(row[1] or "").strip()

    if payments_in_window >= VIDRAPAY_CARD_MAX_SUCCESS_PAYMENTS:
        return False

    if last_paid_at:
        try:
            db = await get_db()
            cur = await db.execute(
                "SELECT CASE WHEN datetime(?) > datetime('now', ?) THEN 1 ELSE 0 END",
                (
                    last_paid_at,
                    f"-{VIDRAPAY_CARD_COOLDOWN_HOURS} hours",
                ),
            )
            cooldown_row = await cur.fetchone()
            await cur.close()
            if cooldown_row and int(cooldown_row[0] or 0) == 1:
                return False
        except Exception:
            logger.exception("Failed to check VidraPay card cooldown for card_id=%s", card_id)
            return True

    return True


async def _get_cards_for_method(method_key: str) -> List[Dict[str, Any]]:
    method = VIDRAPAY_METHODS.get(method_key) or {}
    card_field = str(method.get("card_field") or "").strip()
    if not card_field:
        return []

    cards = await get_active_cards()
    result: List[Dict[str, Any]] = []
    seen_banks: Set[str] = set()

    for card in cards:
        card_id = int(card.get("card_id") or 0)
        requisites = str(card.get(card_field) or "").strip()
        bank_name = str(card.get("bank_name") or "").strip()
        bank_key = bank_name.casefold()

        if not requisites or not bank_name or bank_key in seen_banks:
            continue

        if not await _is_vidrapay_card_available(card_id):
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



async def _get_repeat_vidrapay_card_for_user(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Возвращает последнюю успешную карту пользователя для кнопки «Повторить реквизиты».

    Кнопка показывается только если:
    - у пользователя уже была успешная VidraPay/P2P-оплата с сохранённой картой;
    - эта карта сейчас активна в админке;
    - у карты есть реквизиты для прежнего способа оплаты;
    - карта не попала под лимиты VidraPay: 3 оплаты / 20 часов и пауза 1 час.
    """
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        return None

    if uid <= 0:
        return None

    try:
        db = await get_db()
        cur = await db.execute(
            """
            SELECT
                order_id,
                payment_method,
                bank_card,
                bank_name,
                card_id,
                datetime(
                    COALESCE(
                        NULLIF(TRIM(COALESCE(completed_at, '')), ''),
                        NULLIF(TRIM(COALESCE(created_at, '')), '')
                    )
                ) AS paid_at
            FROM p2p_orders
            WHERE user_id = ?
              AND status = 'completed'
              AND COALESCE(card_id, 0) > 0
              AND TRIM(COALESCE(bank_card, '')) != ''
              AND TRIM(COALESCE(bank_name, '')) != ''
            ORDER BY paid_at DESC, order_id DESC
            LIMIT 20
            """,
            (uid,),
        )
        rows = await cur.fetchall() or []
        await cur.close()
    except Exception:
        logger.exception("Failed to load repeat VidraPay card for user_id=%s", user_id)
        return None

    if not rows:
        return None

    try:
        active_cards = await get_active_cards()
    except Exception:
        logger.exception("Failed to load active cards for repeat VidraPay card user_id=%s", user_id)
        return None

    active_by_id: Dict[int, Dict[str, Any]] = {}
    for card in active_cards:
        with suppress(Exception):
            cid = int(card.get("card_id") or 0)
            if cid > 0 and cid not in active_by_id:
                active_by_id[cid] = card

    for row in rows:
        if not row:
            continue

        try:
            card_id = int(row[4] or 0)
        except (TypeError, ValueError):
            continue

        if card_id <= 0:
            continue

        method_key = _method_key_from_db_value(row[1] or "")
        method = VIDRAPAY_METHODS.get(method_key) or {}
        if not method or not bool(method.get("enabled")):
            continue

        card_field = str(method.get("card_field") or "").strip()
        if not card_field:
            continue

        active_card = active_by_id.get(card_id)
        if not active_card:
            continue

        requisites = str(active_card.get(card_field) or "").strip()
        bank_name = str(active_card.get("bank_name") or "").strip()
        if not requisites or not bank_name:
            continue

        if not await _is_vidrapay_card_available(card_id):
            continue

        repeat_card = dict(active_card)
        repeat_card["repeat_method_key"] = method_key
        repeat_card["repeat_method_db"] = str(method.get("db_value") or f"vidrapay_{method_key}")
        repeat_card["repeat_card_field"] = card_field
        repeat_card["repeat_requisites"] = requisites
        return repeat_card

    return None


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
    order_id = int(order.get("order_id") or 0)
    user_id = int(order.get("user_id") or 0)
    operator_id = int(order.get("operator_id") or 0)

    can_notify = True
    with suppress(Exception):
        from db.p2p import try_claim_p2p_action
        can_notify = await try_claim_p2p_action(order_id, "operator_paid_notify_web")
    if not can_notify:
        return

    bank = str(order.get("bank_name") or "—").strip()
    card = str(order.get("bank_card") or "—").strip()

    try:
        amount_rub = int(float(order.get("total_rub") or order.get("rub_amount") or 0))
        amount_rub_text = f"{amount_rub} ₽"
    except Exception:
        amount_rub_text = f"{order.get('total_rub') or order.get('rub_amount') or '—'} ₽"

    mention = f"Пользователь {user_id}" if user_id > 0 else "Пользователь"

    bot = Bot(token=settings.bot_token, parse_mode="HTML")
    try:
        if user_id > 0:
            with suppress(Exception):
                chat = await bot.get_chat(int(user_id))
                username = str(getattr(chat, "username", "") or "").strip()
                full_name = str(getattr(chat, "full_name", "") or "").strip()
                if username:
                    mention = f"@{username}"
                elif full_name:
                    mention = html.escape(full_name)
    finally:
        with suppress(Exception):
            session = await bot.get_session()
            await session.close()

    admin_text = (
        "━━━━━━━━━━━━━━━━━━\n"
        "‼️<b>Подтверждение оплаты!</b>‼️\n\n"
        f"👤 {mention}\n"
        f"🆔 <b>Заявка №{order_id}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>Карта:</b> <code>{html.escape(card)}</code>\n"
        f"🏦 <b>Банк:</b> {html.escape(bank)}\n"
        f"💸 <b>Сумма:</b> <b>{html.escape(amount_rub_text)}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    ikb = InlineKeyboardMarkup()
    ikb.row(
        InlineKeyboardButton("🧾 Чек", callback_data=f"op_view_receipt:{order_id}:{user_id}"),
        InlineKeyboardButton("📥 Заявка", callback_data=f"operator_open_order:{user_id}:{order_id}"),
    )
    ikb.add(InlineKeyboardButton("✅ Готово — начать обмен", callback_data=f"ff_ready:{order_id}:{user_id}"))
    ikb.add(InlineKeyboardButton("✅ Завершить", callback_data=f"finish_order:{order_id}:{user_id}"))

    ids: List[int] = []
    if operator_id:
        ids.append(operator_id)
    else:
        ids = await _admin_ids()

    await _send_many_bot_messages(ids, admin_text, reply_markup=ikb)

    with suppress(Exception):
        from db.p2p import mark_p2p_action_sent
        await mark_p2p_action_sent(order_id, "operator_paid_notify_web")

    if user_id > 0:
        asset = _resolve_asset(order)
        amount_crypto = _format_amount_for_asset(asset, order.get("btc_amount"))
        wallet = _short_wallet(order.get("wallet"))
        user_text = (
            f"🧾 Заявка №{order_id}\n\n"
            f"Монета: {html.escape(asset)}\n"
            f"Сумма: {html.escape(amount_crypto)} {html.escape(asset)}\n"
            f"Адрес: {html.escape(wallet)}\n\n"
            "1️⃣ Оплата получена — ⏳\n"
            "2️⃣ Средства на обменнике — ⏳\n"
            "3️⃣ Перевод на ваш кошелёк — ⏳\n\n"
            "❗️ Если перевод не подтверждён в течение 10 минут — напишите в поддержку."
        )
        sent_ref: Optional[tuple[int, int]] = None
        with suppress(Exception):
            sent_ref = await _send_bot_message(user_id, user_text)

        if sent_ref and order_id > 0:
            with suppress(Exception):
                db = await get_db()
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS p2p_order_actions (
                        order_id    INTEGER NOT NULL,
                        action      TEXT    NOT NULL,
                        status      TEXT    NOT NULL DEFAULT 'claimed',
                        message_id  INTEGER,
                        error       TEXT,
                        created_at  TEXT    NOT NULL,
                        updated_at  TEXT    NOT NULL,
                        PRIMARY KEY(order_id, action)
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT INTO p2p_order_actions (
                        order_id,
                        action,
                        status,
                        message_id,
                        error,
                        created_at,
                        updated_at
                    )
                    VALUES (?, 'user_paid_status_card', 'sent', ?, NULL, datetime('now'), datetime('now'))
                    ON CONFLICT(order_id, action) DO UPDATE SET
                        status = 'sent',
                        message_id = excluded.message_id,
                        error = NULL,
                        updated_at = datetime('now')
                    """,
                    (int(order_id), int(sent_ref[1])),
                )
                await db.commit()


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
            gap: 9px;
            margin-top: 4px;
        }}

        .summary-card {{
            min-width: 0;
            padding: 13px 14px;
            border-radius: 18px;
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
            font-size: 23px;
            line-height: 1.15;
            font-weight: 850;
            letter-spacing: -.2px;
        }}

        .summary-rub {{
            margin-top: 6px;
            color: var(--accent);
            font-size: 13px;
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
            gap: 12px;
        }}

        .compact-summary-top {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
        }}

        .compact-wallet {{
            padding-top: 12px;
            border-top: 1px solid var(--line);
        }}

        .compact-wallet .wallet {{
            margin-top: 5px;
            color: #d9dbe0;
            font-size: 12.7px;
            line-height: 1.4;
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
            margin-bottom: 5px;
            padding: 4px 8px;
            border-radius: 999px;
            border: 1px solid rgba(214, 179, 95, .30);
            background: rgba(214, 179, 95, .10);
            color: var(--accent);
            font-size: 10.5px;
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
                font-size: 21px;
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


def _render_method_choice_page(
    order: Dict[str, Any],
    token: str,
    error_text: str = "",
    repeat_card: Optional[Dict[str, Any]] = None,
) -> str:
    view = _build_view_order(order, token)

    client_id_raw = str(view.get("client_id") or "").strip()
    amount_raw = float(view.get("amount") or 0)
    pay_amount_text = html.escape(_format_method_amount(amount_raw))

    method_cards = []

    if repeat_card:
        repeat_bank = html.escape(str(repeat_card.get("bank_name") or "прошлый банк").strip() or "прошлый банк")
        method_cards.append(
            f"""
                <a
                    class="method-card any-bank"
                    href="/vidrapay/pay/{quote(client_id_raw)}/repeat"
                    data-repeat-confirm-url="/vidrapay/pay/{quote(client_id_raw)}/repeat"
                    data-repeat-confirm-bank="{repeat_bank}"
                >
                    <div class="method-icon">↻</div>
                    <div class="method-info">
                        <div class="any-bank-badge">быстрее и надёжнее</div>
                        <div class="method-title">Повторить реквизиты</div>
                        <div class="method-subtitle">Снова оплатить на знакомую карту: {repeat_bank}</div>
                    </div>
                    <div class="method-side">
                        <div class="method-pay-label">К оплате</div>
                        <div class="method-pay">{pay_amount_text} ₽</div>
                    </div>
                </a>
            """
        )

    for key, method in VIDRAPAY_METHODS.items():
        title = html.escape(str(method.get("title") or key))
        subtitle = html.escape(str(method.get("subtitle") or ""))
        icon = str(method.get("icon") or "₽")
        enabled = bool(method.get("enabled"))

        if enabled:
            method_cards.append(
                f"""
                    <a
                        class="method-card"
                        href="/vidrapay/pay/{quote(client_id_raw)}/method/{quote(key)}"
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
        <div class="section-title">Выберите способ оплаты</div>
        <div class="methods">
            {"".join(method_cards)}
        </div>
    """

    repeat_confirm_html = """
    <div class="confirm-backdrop" id="repeatConfirm" aria-hidden="true">
        <div class="confirm-box" role="dialog" aria-modal="true" aria-labelledby="repeatConfirmTitle">
            <div class="confirm-title" id="repeatConfirmTitle">Повторить прошлые реквизиты?</div>
            <div class="confirm-text" id="repeatConfirmText">
                Лучше переводить на эти реквизиты <b>с той же банковской карты</b>, с которой вы уже оплачивали в прошлый раз.
                Так платёж выглядит привычнее для банка и обычно проходит спокойнее.
            </div>
            <div class="confirm-choice" id="repeatConfirmBank">Знакомые реквизиты</div>
            <div class="confirm-actions">
                <button class="confirm-btn cancel" type="button" data-repeat-confirm-cancel="1">Назад</button>
                <button class="confirm-btn ok" type="button" data-repeat-confirm-ok="1">Согласиться</button>
            </div>
        </div>
    </div>

    <script>
        (function () {
            var modal = document.getElementById('repeatConfirm');
            var bankBox = document.getElementById('repeatConfirmBank');
            var okBtn = document.querySelector('[data-repeat-confirm-ok]');
            var cancelBtn = document.querySelector('[data-repeat-confirm-cancel]');
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

            function openModal(url, bank) {
                pendingUrl = url || '';
                if (bankBox) bankBox.textContent = bank ? ('Банк: ' + bank) : 'Знакомые реквизиты';
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

            document.querySelectorAll('[data-repeat-confirm-url]').forEach(function (item) {
                item.addEventListener('click', function (event) {
                    event.preventDefault();
                    openModal(
                        item.getAttribute('data-repeat-confirm-url') || item.getAttribute('href'),
                        item.getAttribute('data-repeat-confirm-bank') || ''
                    );
                });
            });

            if (okBtn) {
                okBtn.addEventListener('click', function () {
                    if (pendingUrl) window.location.href = pendingUrl;
                });
            }

            if (cancelBtn) {
                cancelBtn.addEventListener('click', closeModal);
            }

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
    """ if repeat_card else ""

    return _render_base_page(order, token, main_html, error_text=error_text, extra_html=repeat_confirm_html)


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
        if method_key in VIDRAPAY_METHODS:
            return _no_cache_response(_render_payment_details_page(order, token, method_key))

    repeat_card = await _get_repeat_vidrapay_card_for_user(user_id)
    return _no_cache_response(_render_method_choice_page(order, token, repeat_card=repeat_card))


async def vidrapay_repeat_card(request: web.Request) -> web.Response:
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
        saved_method_key = _method_key_from_db_value(order.get("payment_method"))
        if saved_method_key in VIDRAPAY_METHODS:
            return _no_cache_response(_render_payment_details_page(order, token, saved_method_key))

    repeat_card = await _get_repeat_vidrapay_card_for_user(user_id)
    if not repeat_card:
        fresh_repeat_card = await _get_repeat_vidrapay_card_for_user(user_id)
        return _no_cache_response(
            _render_method_choice_page(
                order,
                token,
                "Повторные реквизиты сейчас недоступны. Выберите другой способ оплаты.",
                repeat_card=fresh_repeat_card,
            ),
            status=404,
        )

    method_key = str(repeat_card.get("repeat_method_key") or "").strip().lower()
    method = VIDRAPAY_METHODS.get(method_key)
    if not method:
        fresh_repeat_card = await _get_repeat_vidrapay_card_for_user(user_id)
        return _no_cache_response(
            _render_method_choice_page(
                order,
                token,
                "Повторные реквизиты сейчас недоступны. Выберите другой способ оплаты.",
                repeat_card=fresh_repeat_card,
            ),
            status=404,
        )

    card_field = str(method.get("card_field") or "").strip()
    requisites = str(repeat_card.get(card_field) or repeat_card.get("repeat_requisites") or "").strip()
    if not requisites:
        fresh_repeat_card = await _get_repeat_vidrapay_card_for_user(user_id)
        return _no_cache_response(
            _render_method_choice_page(
                order,
                token,
                "У повторной карты нет подходящих реквизитов. Выберите другой способ оплаты.",
                repeat_card=fresh_repeat_card,
            ),
            status=404,
        )

    method_db = str(method.get("db_value") or f"vidrapay_{method_key}")

    try:
        await _set_payment_card(order_id, method_db, repeat_card, requisites)
        order["payment_method"] = method_db
        order["bank_card"] = requisites
        order["bank_name"] = str(repeat_card.get("bank_name") or "").strip()
        order["card_id"] = int(repeat_card.get("card_id") or 0)
    except Exception:
        logger.exception(
            "Failed to set VidraPay repeat card for order_id=%s user_id=%s",
            order_id,
            user_id,
        )
        fresh_repeat_card = await _get_repeat_vidrapay_card_for_user(user_id)
        return _no_cache_response(
            _render_method_choice_page(
                order,
                token,
                "Не удалось сохранить повторные реквизиты. Попробуйте выбрать способ оплаты вручную.",
                repeat_card=fresh_repeat_card,
            ),
            status=500,
        )

    return _no_cache_response(_render_payment_details_page(order, token, method_key, repeat_card))



async def _redirect_to_nirvana_qr_payment(order: Dict[str, Any], token: str, request: web.Request) -> web.Response:
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
        logger.exception("Failed to set QR payment method for order_id=%s", order_id)

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
        logger.exception("Nirvana QR order failed for order_id=%s: %s", order_id, exc)

        error_text = str(exc)
        if "ликвид" in error_text.lower():
            user_error = (
                "Оплата по QR сейчас временно недоступна: "
                "NirvanaPay не нашёл свободный QR-реквизит на эту сумму. "
                "Выберите другой способ оплаты или попробуйте QR чуть позже."
            )
        else:
            user_error = (
                "NirvanaPay временно не выдал QR-ссылку. "
                "Попробуйте другой способ оплаты или повторите позже."
            )

        return _no_cache_response(
            _render_method_choice_page(
                order,
                token,
                user_error,
            ),
            status=200,
        )

    except Exception:
        logger.exception("Failed to create Nirvana QR order for order_id=%s", order_id)
        return _no_cache_response(
            _render_method_choice_page(
                order,
                token,
                "Не удалось создать оплату по QR. Попробуйте другой способ оплаты или повторите позже.",
            ),
            status=200,
        )

    qr_url = str(
        payment.get("receiver")
        or payment.get("redirect_url")
        or ""
    ).strip()

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
                   payment_method = ?
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
    except Exception:
        logger.exception("Failed to save QR requisites for order_id=%s", order_id)

    token_q = quote(str(token))
    qr_q = quote(qr_url, safe="")
    amount_text = html.escape(_format_method_amount(amount))
    qr_url_html = html.escape(qr_url)

    main_html = f"""
        <div class="details-box" style="margin-top:14px;">
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

            <div class="notice-box">
                <div class="notice-icon">▦</div>
                <div>
                    Отсканируйте QR-код камерой телефона или банковским приложением.
                    Оплачивайте ровно указанную сумму: <b>{amount_text} ₽</b>.
                    После перевода нажмите «Я оплатил».
                </div>
            </div>

            <div class="detail-row">
                <div class="detail-label">QR-ссылка</div>
                <div class="requisite-line">
                    <div class="detail-value" id="qrLinkValue">{qr_url_html}</div>
                    <button class="copy-btn" type="button" data-copy-target="qrLinkValue">Скопировать</button>
                </div>
            </div>
        </div>

        <div class="payment-actions">
            <a class="pay-action-btn paid" href="/vidrapay/pay/{token_q}/paid">✅ Я оплатил</a>
            <a class="pay-action-btn cancel-payment" href="/vidrapay/pay/{token_q}/cancel">Отменить</a>
        </div>

        <script>
            (function () {{
                document.querySelectorAll('[data-copy-target]').forEach(function (btn) {{
                    btn.addEventListener('click', function () {{
                        var id = btn.getAttribute('data-copy-target');
                        var el = document.getElementById(id);
                        var text = el ? (el.textContent || '').trim() : '';
                        if (!text) return;

                        navigator.clipboard.writeText(text).then(function () {{
                            var old = btn.textContent;
                            btn.textContent = 'Скопировано';
                            btn.classList.add('copied');
                            setTimeout(function () {{
                                btn.textContent = old || 'Скопировать';
                                btn.classList.remove('copied');
                            }}, 1400);
                        }}).catch(function () {{}});
                    }});
                }});
            }})();
        </script>
    """

    return _no_cache_response(
        _render_base_page(order, token, main_html),
        status=200,
    )


async def vidrapay_select_method(request: web.Request) -> web.Response:
    token = str(request.match_info.get("token") or "").strip()
    method_key = str(request.match_info.get("method_key") or "").strip().lower()

    method = VIDRAPAY_METHODS.get(method_key)
    if not method:
        return web.Response(text="Неизвестный способ оплаты", status=400)

    if not bool(method.get("enabled")):
        return web.Response(text="Этот способ оплаты пока недоступен", status=403)

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

    if method_key == "qr":
        return await _redirect_to_nirvana_qr_payment(order, token, request)

    method_db = str(method.get("db_value") or f"vidrapay_{method_key}")

    try:
        await _set_payment_method(order_id, method_db)
        order["payment_method"] = method_db
    except Exception:
        logger.exception("Failed to set VidraPay payment method for order_id=%s", order_id)

    try:
        cards = await _get_cards_for_method(method_key)
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

    cards = await _get_cards_for_method(method_key)
    all_cards = await _get_all_cards_for_method(method_key)

    if str(request.query.get("confirm") or "").strip() != "1":
        return _no_cache_response(_render_bank_choice_page(order, token, method_key, cards), status=200)

    if is_any_bank:
        selected_card = random.choice(all_cards) if all_cards else None
    else:
        selected_card = next((card for card in cards if int(card.get("card_id") or 0) == card_id), None)

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

    try:
        await _set_payment_card(order_id, method_db, selected_card, requisites)
        order["payment_method"] = method_db
        order["bank_card"] = requisites
        order["bank_name"] = str(selected_card.get("bank_name") or "").strip()
        order["card_id"] = int(selected_card.get("card_id") or 0)
    except Exception:
        logger.exception(
            "Failed to set VidraPay selected card for order_id=%s method=%s card_id=%s",
            order_id,
            method_key,
            card_id,
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
app.router.add_get("/vidrapay/pay/{token}/repeat", vidrapay_repeat_card)
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
