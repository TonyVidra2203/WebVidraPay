import asyncio
import html
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any, Dict, Optional, Set, Tuple, List

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher import FSMContext
from aiogram.types import (
    ContentType,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

from binance import BinanceClient, BinanceNotionalTooSmall
from config.settings import settings
from db.connection import get_db
from db.p2p import delete_order, get_pending_order
from db.referrals import try_add_referral_commission
from db.users import get_user
from db.settings import is_ton_profit_split_enabled
from handlers.chat.states import InstructionStates
from handlers.chat.templates import DEFAULT_OP_KB
from handlers.chat.utils import bot_send, safe_delete
from handlers.common import pending_buy_messages, send_welcome
from keyboards.inline import Callback
from services.ff import FFAPIError, create_order, get_order_details
from utils.helpers import clear_history
from db.admin_debts import apply_profit_to_debt_kopeks, rub_to_kopeks, kopeks_to_rub

logger = logging.getLogger(__name__)

TON_WITHDRAW_FEE = Decimal("0.03")
BTC_MEMPOOL = "https://mempool.space/tx/{tx}"

INSTRUCTION_CARD_PROMPT = "Введите номер карты или телефона:"
INSTRUCTION_BANK_PROMPT = "Введите название банка:"
INSTRUCTION_COMMENT_PROMPT = "Введите комментарий к переводу (или «нет»):"

INSTRUCTION_TEXT_TEMPLATE_USER = (
    "📝 Реквизиты для оплаты:\n"
    "➖➖➖➖➖➖➖➖➖➖\n"
    "▶ Номер:    {card}\n"
    "▶ Банк:     {bank}\n"
    "▶ Коммент.:  {comment}\n"
    "▶ Сумма:    {amount} ₽\n"
    "➖➖➖➖➖➖➖➖➖➖\n"
    "‼️ Точно сверяйте номер, банк и переводите СТРОГО ту СУММУ, которая УКАЗАНА в инструкции. "
    "Ошибка — деньги потеряны!"
)

INSTRUCTION_TEXT_TEMPLATE = (
    "📝 Реквизиты для оплаты:\n"
    "➖➖➖➖➖➖➖➖➖➖\n"
    "▶ Номер:    {card}\n"
    "▶ Банк:     {bank}\n"
    "▶ Коммент.:  {comment}\n"
    "▶ Сумма:    {amount} ₽\n"
    "➖➖➖➖➖➖➖➖➖➖"
)

BTC_LIKE = {"BTC", "LTC"}
ASSET_MARKERS = ("BTC", "LTC", "USDT", "XMR")
SUPPORT_BUTTON_DELAY_SEC = 10 * 60


def html_escape(value: Any) -> str:
    return html.escape(str(value) if value is not None else "")


def _short_wallet(addr: str) -> str:
    a = (addr or "").strip()
    if not a:
        return "—"
    if len(a) <= 12:
        return a
    return f"{a[:6]}...{a[-4:]}"


def _format_amount_for_asset(asset: str, amount: float) -> str:
    a = (asset or "BTC").upper()
    try:
        s = f"{amount:.8f}" if a in BTC_LIKE else f"{amount:.2f}"
    except Exception:
        s = str(amount)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _extract_asset_from_comment(comment: str) -> str:
    up = (comment or "").upper()
    for code in ASSET_MARKERS:
        if f"({code})" in up:
            return code
    return "BTC"


def format_requisite_for_user(raw: Any) -> str:
    s = (str(raw) if raw is not None else "").strip()
    if not s:
        return "—"

    cleaned = (
        s.replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
        .replace("\u200e", "")
        .replace("\u200f", "")
    )
    digits = "".join(ch for ch in cleaned if ch.isdigit())

    normalized: Optional[str] = None
    if len(digits) == 10 and digits.startswith("9"):
        normalized = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        normalized = "7" + digits[1:]
    elif len(digits) == 11 and digits.startswith("7"):
        normalized = digits

    if normalized:
        return f"<code>{html_escape('+' + normalized)}</code>"

    return f"<code>{html_escape(s)}</code>"


async def get_user_card(user_id: int) -> Optional[str]:
    user = await get_user(user_id)
    if not user:
        return None
    return user.get("bank_card") or user.get("card")


async def get_user_bank(user_id: int) -> Optional[str]:
    user = await get_user(user_id)
    if not user:
        return None
    return user.get("bank_name") or user.get("bank")


@dataclass
class RuntimeState:
    pending_check_receipts: Dict[int, int] = field(default_factory=dict)
    receipt_ui_messages: Dict[int, Set[int]] = field(default_factory=dict)
    pending_reject_reasons: Dict[int, Dict[str, int]] = field(default_factory=dict)
    pending_ff_ready_buttons: Dict[Tuple[int, int], Tuple[int, int]] = field(default_factory=dict)
    pending_operator_order_cards: Dict[Tuple[int, int], Tuple[int, int]] = field(default_factory=dict)
    pending_manual_finish: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    started_exchanges: Set[int] = field(default_factory=set)
    status_cards: Dict[int, Tuple[int, int]] = field(default_factory=dict)
    usdt_spent_by_order: Dict[int, float] = field(default_factory=dict)
    profit_ton_by_order: Dict[int, float] = field(default_factory=dict)
    ff_ui_messages_by_order: Dict[int, Set[Tuple[int, int]]] = field(default_factory=dict)


STATE = RuntimeState()


def _track_receipt_msg(user_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    STATE.receipt_ui_messages.setdefault(user_id, set()).add(int(message_id))

def _track_ff_ui_message(order_id: int, chat_id: Optional[int], message_id: Optional[int]) -> None:
    try:
        oid = int(order_id)
        cid = int(chat_id)
        mid = int(message_id)
    except Exception:
        return

    STATE.ff_ui_messages_by_order.setdefault(oid, set()).add((cid, mid))


async def _ensure_order_ui_messages_table() -> None:
    """
    Локальная таблица для UI-сообщений заявок.

    Зачем нужна:
    - WEB-подтверждение оплаты рассылается всем админам;
    - STATE хранит message_id только в памяти текущего процесса;
    - после перезапуска/другого процесса часть карточек у админов может остаться неизвестной.
    Поэтому дублируем chat_id/message_id в SQLite и при финализации удаляем карточки у всех админов.
    """
    db = await get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS p2p_order_ui_messages (
            order_id   INTEGER NOT NULL,
            chat_id    INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            kind       TEXT NOT NULL DEFAULT 'ff_ui',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (order_id, chat_id, message_id)
        )
        """
    )
    await db.commit()


async def _remember_order_ui_message(
    order_id: int,
    chat_id: Optional[int],
    message_id: Optional[int],
    *,
    kind: str = "ff_ui",
) -> None:
    """
    Запоминает UI-сообщение заявки и в памяти, и в БД.

    Если БД по какой-то причине недоступна, память всё равно работает,
    поэтому старое поведение не ломается.
    """
    try:
        oid = int(order_id)
        cid = int(chat_id)
        mid = int(message_id)
    except Exception:
        return

    if oid <= 0 or cid == 0 or mid <= 0:
        _track_ff_ui_message(oid, cid, mid)
        return

    _track_ff_ui_message(oid, cid, mid)

    with suppress(Exception):
        await _ensure_order_ui_messages_table()
        db = await get_db()
        await db.execute(
            """
            INSERT OR IGNORE INTO p2p_order_ui_messages
                (order_id, chat_id, message_id, kind)
            VALUES (?, ?, ?, ?)
            """,
            (oid, cid, mid, str(kind or "ff_ui")),
        )
        await db.commit()


async def _load_order_ui_messages(order_id: int) -> Set[Tuple[int, int]]:
    """
    Возвращает все сохранённые UI-сообщения по заявке из БД.
    """
    refs: Set[Tuple[int, int]] = set()
    try:
        oid = int(order_id)
    except Exception:
        return refs

    if oid <= 0:
        return refs

    with suppress(Exception):
        await _ensure_order_ui_messages_table()
        db = await get_db()
        cur = await db.execute(
            """
            SELECT chat_id, message_id
              FROM p2p_order_ui_messages
             WHERE order_id = ?
            """,
            (oid,),
        )
        rows = await cur.fetchall() or []
        with suppress(Exception):
            await cur.close()

        for row in rows:
            try:
                refs.add((int(row[0]), int(row[1])))
            except Exception:
                continue

    return refs


async def _forget_order_ui_messages(order_id: int) -> None:
    """
    Удаляет из БД сохранённые ссылки на UI-сообщения заявки.
    """
    try:
        oid = int(order_id)
    except Exception:
        return

    if oid <= 0:
        return

    with suppress(Exception):
        await _ensure_order_ui_messages_table()
        db = await get_db()
        await db.execute(
            "DELETE FROM p2p_order_ui_messages WHERE order_id = ?",
            (oid,),
        )
        await db.commit()

async def _cleanup_receipt_ui(bot: Bot, user_id: int) -> None:
    ids = STATE.receipt_ui_messages.pop(user_id, set())
    for mid in ids:
        with suppress(Exception):
            await safe_delete(bot, user_id, mid)


async def _auto_delete(bot: Bot, chat_id: int, message_id: int, delay: int = 6) -> None:
    with suppress(Exception):
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id, message_id)


async def _build_mention(bot: Bot, user_id: int) -> str:
    try:
        uid = int(user_id)
    except Exception:
        return html_escape(str(user_id))

    if uid <= 0:
        return f"WEB-гость / user_id <code>{html_escape(uid)}</code>"

    try:
        chat = await bot.get_chat(uid)
        if getattr(chat, "username", None):
            return f"@{chat.username}"
        return f'<a href="tg://user?id={uid}">{html_escape(getattr(chat, "full_name", str(uid)))}</a>'
    except Exception:
        return f'<a href="tg://user?id={uid}">{html_escape(str(uid))}</a>'

async def _update_user_status_card(
    bot: Bot,
    order_id: int,
    *,
    asset: str,
    amount: float,
    wallet: str,
    step1_done: bool,
    step2_done: bool,
    step3_done: bool,
    extra_line: str = "",
    user_id: Optional[int] = None,
) -> None:
    order_id_int = int(order_id)
    msg_ref = STATE.status_cards.get(order_id_int)

    # Важно для VidraPay/webhook:
    # нажатие «Оплатил» на сайте может обрабатываться отдельным процессом,
    # поэтому message_id карточки ожидания есть в БД, но отсутствует
    # в локальном STATE процесса бота, где админ жмёт «Завершить».
    if not msg_ref and user_id and int(user_id) > 0:
        with suppress(Exception):
            db = await get_db()
            cur = await db.execute(
                """
                SELECT message_id
                  FROM p2p_order_actions
                 WHERE order_id = ?
                   AND action IN ('user_paid_status_card', 'user_exchange_status_card')
                   AND status = 'sent'
                   AND message_id IS NOT NULL
                 ORDER BY
                   CASE action
                     WHEN 'user_paid_status_card' THEN 0
                     ELSE 1
                   END,
                   updated_at DESC
                 LIMIT 1
                """,
                (order_id_int,),
            )
            row = await cur.fetchone()
            with suppress(Exception):
                await cur.close()

            if row and row[0]:
                msg_ref = (int(user_id), int(row[0]))
                STATE.status_cards[order_id_int] = msg_ref

    if not msg_ref:
        return

    chat_id, message_id = msg_ref
    asset_u = (asset or "BTC").upper()
    amount_str = _format_amount_for_asset(asset_u, float(amount))
    short_wallet = _short_wallet(wallet)

    icon1 = "✅" if step1_done else "⏳"
    icon2 = "✅" if step2_done else "⏳"
    icon3 = "✅" if step3_done else "⏳"

    text = (
        f"🧾 Заявка №{order_id_int}\n\n"
        f"Монета: {asset_u}\n"
        f"Сумма: {amount_str} {asset_u}\n"
        f"Адрес: {short_wallet}\n\n"
        f"1️⃣ Оплата получена — {icon1}\n"
        f"2️⃣ Средства на обменнике — {icon2}\n"
        f"3️⃣ Перевод на ваш кошелёк — {icon3}"
    )
    if extra_line:
        text += f"\n\n{extra_line}"

    with suppress(Exception):
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML",
        )


def _kb_user_support_after_wait(order_id: int) -> InlineKeyboardMarkup:
    """Кнопка связи с поддержкой, которая появляется под карточкой статуса через 10 минут."""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            "Связаться с поддержкой",
            callback_data=f"user_support_sms:{int(order_id)}",
        )
    )
    return kb


async def _order_exchange_already_started_or_closed(order_id: int) -> bool:
    """
    Проверяет, нужно ли НЕ показывать кнопку поддержки:
    - заявка уже не pending;
    - обмен уже был запущен/захвачен через action exchange_start.
    """
    try:
        oid = int(order_id)
    except Exception:
        return True

    if oid <= 0:
        return True

    with suppress(Exception):
        from db.p2p import get_order_by_id

        order = await get_order_by_id(oid)
        if not order:
            return True

        status = str((order or {}).get("status") or "").strip().lower()
        if status and status != "pending":
            return True

    with suppress(Exception):
        db = await get_db()
        cur = await db.execute(
            """
            SELECT status
              FROM p2p_order_actions
             WHERE order_id = ?
               AND action = 'exchange_start'
             LIMIT 1
            """,
            (oid,),
        )
        row = await cur.fetchone()
        with suppress(Exception):
            await cur.close()

        if row:
            action_status = str(row[0] or "").strip().lower()
            if action_status in {"claimed", "sent"}:
                return True

    return False


async def _show_support_button_after_wait(
    bot: Bot,
    *,
    order_id: int,
    user_id: int,
    message_id: int,
) -> None:
    """
    Через 10 минут после появления карточки статуса добавляет кнопку поддержки,
    если обмен к этому моменту ещё не начался.
    """
    try:
        oid = int(order_id)
        uid = int(user_id)
        mid = int(message_id)
    except Exception:
        return

    if oid <= 0 or uid <= 0 or mid <= 0:
        return

    await asyncio.sleep(SUPPORT_BUTTON_DELAY_SEC)

    try:
        current_ref = STATE.status_cards.get(oid)
        if current_ref and (int(current_ref[0]) != uid or int(current_ref[1]) != mid):
            return

        if await _order_exchange_already_started_or_closed(oid):
            return

        await bot.edit_message_reply_markup(
            chat_id=uid,
            message_id=mid,
            reply_markup=_kb_user_support_after_wait(oid),
        )
    except Exception:
        logger.exception("Failed to show support button for order_id=%s user_id=%s", oid, uid)


async def _finalize_exchange_ui(bot: Bot, order_id: int, operator_id: Optional[int]) -> None:
    order_id_int = int(order_id)

    # 1) Собираем ВСЕ UI-сообщения по заявке:
    #    - из памяти текущего процесса;
    #    - из БД, куда сохраняются карточки, отправленные всем админам.
    #
    # Это чинит ситуацию, когда WEB-оплата разослана всем админам,
    # один админ завершил/запустил обмен, а у остальных карточки не исчезли
    # из-за потери локального STATE или обработки в другом процессе.
    tracked_refs: Set[Tuple[int, int]] = set()
    tracked_refs.update(STATE.ff_ui_messages_by_order.pop(order_id_int, set()))
    tracked_refs.update(await _load_order_ui_messages(order_id_int))

    for chat_id, message_id in list(tracked_refs):
        with suppress(Exception):
            await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))

    await _forget_order_ui_messages(order_id_int)

    # 2) Удаляем сообщения из старой карты pending_ff_ready_buttons
    ff_keys_to_delete: List[Tuple[int, int]] = []
    for key, msg_ref in list(STATE.pending_ff_ready_buttons.items()):
        try:
            op_id_key, oid_key = key
        except Exception:
            continue

        try:
            oid_key_int = int(oid_key)
        except Exception:
            continue

        if oid_key_int != order_id_int:
            continue

        ff_keys_to_delete.append((int(op_id_key), oid_key_int))

        if msg_ref:
            try:
                chat_id, message_id = msg_ref
                with suppress(Exception):
                    await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
            except Exception:
                pass

    for key in ff_keys_to_delete:
        with suppress(Exception):
            STATE.pending_ff_ready_buttons.pop(key, None)

    # 3) Удаляем все операторские карточки по заявке
    op_card_keys_to_delete: List[Tuple[int, int]] = []
    for key, msg_ref in list(STATE.pending_operator_order_cards.items()):
        try:
            op_id_key, oid_key = key
        except Exception:
            continue

        try:
            oid_key_int = int(oid_key)
        except Exception:
            continue

        if oid_key_int != order_id_int:
            continue

        op_card_keys_to_delete.append((int(op_id_key), oid_key_int))

        if msg_ref:
            try:
                chat_id, message_id = msg_ref
                with suppress(Exception):
                    await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
            except Exception:
                pass

    for key in op_card_keys_to_delete:
        with suppress(Exception):
            STATE.pending_operator_order_cards.pop(key, None)

    # 4) Совместимость со старыми точечными ключами
    if operator_id:
        with suppress(Exception):
            STATE.pending_ff_ready_buttons.pop((int(operator_id), order_id_int), None)
        with suppress(Exception):
            STATE.pending_operator_order_cards.pop((int(operator_id), order_id_int), None)

    # 5) Снимаем лок запуска обмена
    STATE.started_exchanges.discard(order_id_int)



async def _admin_ids() -> List[int]:
    try:
        db = await get_db()
        cur = await db.execute(
            "SELECT telegram_id FROM users WHERE role = 'Admin' AND telegram_id IS NOT NULL"
        )
        rows = await cur.fetchall() or []
        await cur.close()
        out: List[int] = []
        for r in rows:
            if not r:
                continue
            with suppress(Exception):
                out.append(int(r[0]))
        seen: Set[int] = set()
        uniq: List[int] = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq
    except Exception:
        return []


async def _notify_admin(
    bot: Bot,
    text: str,
    *,
    admin_ids: Optional[List[int]] = None,
    exclude_ids: Optional[List[int]] = None,
) -> None:
    ids = admin_ids if admin_ids is not None else await _admin_ids()

    excluded: Set[int] = set()
    for raw in (exclude_ids or []):
        with suppress(Exception):
            excluded.add(int(raw))

    sent_once: Set[int] = set()
    for aid in ids:
        try:
            aid_int = int(aid)
        except Exception:
            continue

        if aid_int in excluded or aid_int in sent_once:
            continue

        sent_once.add(aid_int)
        with suppress(Exception):
            await bot.send_message(
                aid_int,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

def _is_out_of_limits_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return ("out of limits" in s) or ("limits" in s) or ("limit" in s and "error" in s)


def _is_akkula_order(order: Optional[Dict[str, Any]]) -> bool:
    pm = str((order or {}).get("payment_method") or "").lower().strip()
    return pm == "akkula"


def _is_vidrapay_order(order: Optional[Dict[str, Any]]) -> bool:
    pm = str((order or {}).get("payment_method") or "").lower().strip()
    return pm in {"vidrapay", "vidra-pay"} or pm.startswith("vidrapay") or pm.startswith("vidra-pay")


def _is_web_order(order: Optional[Dict[str, Any]]) -> bool:
    if not order:
        return False

    comment = str((order or {}).get("comment") or "").strip().upper()
    payment_method = str((order or {}).get("payment_method") or "").strip().lower()

    return (
        comment.startswith("WEB")
        or payment_method.startswith("vidrapay_")
        or payment_method in {"vidrapay", "vidra-pay"}
        or payment_method.startswith("vidrapay")
        or payment_method.startswith("vidra-pay")
    )


async def _ensure_vidrapay_distribution_tables_for_completion() -> None:
    """
    Таблица фактических успешных использований VidraPay-карт.

    Важно: старые версии создавали UNIQUE на card_id и писали сюда карту
    сразу при выдаче реквизитов. Новая логика пишет запись только после
    успешного завершения обмена и допускает несколько успешных сделок
    по одной карте во времени.
    """
    db = await get_db()

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS vidrapay_card_distribution_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            method_key TEXT NOT NULL,
            card_id INTEGER NOT NULL,
            bank_name TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.commit()

    # Мягкая миграция со старой схемы, где card_id был UNIQUE.
    # UNIQUE ломает правило "до 3 успешных переводов", поэтому убираем его.
    try:
        cur = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'vidrapay_card_distribution_usage'"
        )
        row = await cur.fetchone()
        with suppress(Exception):
            await cur.close()

        create_sql = str(row[0] or "") if row else ""
        if "CARD_ID INTEGER NOT NULL UNIQUE" in create_sql.upper():
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS vidrapay_card_distribution_usage_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    method_key TEXT NOT NULL,
                    card_id INTEGER NOT NULL,
                    bank_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO vidrapay_card_distribution_usage_new
                    (id, order_id, user_id, method_key, card_id, bank_name, created_at)
                SELECT id, order_id, user_id, method_key, card_id, bank_name, created_at
                  FROM vidrapay_card_distribution_usage
                """
            )
            await db.execute("DROP TABLE vidrapay_card_distribution_usage")
            await db.execute(
                "ALTER TABLE vidrapay_card_distribution_usage_new RENAME TO vidrapay_card_distribution_usage"
            )
            await db.commit()
    except Exception:
        logger.exception("Failed to migrate VidraPay distribution usage table")

    with suppress(Exception):
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vidrapay_card_usage_order_card
                ON vidrapay_card_distribution_usage(order_id, card_id)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vidrapay_card_usage_card_created
                ON vidrapay_card_distribution_usage(card_id, created_at)
            """
        )
        await db.commit()


async def _record_successful_vidrapay_card_usage(order: Optional[Dict[str, Any]]) -> None:
    """
    Засчитывает использование карты VidraPay только после успешной сделки.

    Это исправляет баг: раньше карта попадала под паузу/лимиты сразу после
    показа реквизитов на сайте оплаты, даже если пользователь отменил заявку.
    """
    if not order or not _is_web_order(order):
        return

    payment_method = str((order or {}).get("payment_method") or "").strip().lower()
    if not payment_method.startswith("vidrapay_"):
        return

    try:
        order_id = int((order or {}).get("order_id") or 0)
        user_id = int((order or {}).get("user_id") or 0)
        card_id = int((order or {}).get("card_id") or 0)
    except Exception:
        return

    if order_id <= 0 or card_id <= 0:
        return

    method_key = payment_method.replace("vidrapay_", "", 1) or "card"
    bank_name = str((order or {}).get("bank_name") or "").strip()

    try:
        await _ensure_vidrapay_distribution_tables_for_completion()
        db = await get_db()
        await db.execute(
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
                order_id,
                user_id,
                method_key,
                card_id,
                bank_name,
            ),
        )
        await db.commit()
    except Exception:
        logger.exception(
            "Failed to record successful VidraPay card usage order_id=%s card_id=%s",
            order_id,
            card_id,
        )


def _is_binance_order(order: Optional[Dict[str, Any]]) -> bool:
    pm = str((order or {}).get("payment_method") or "").lower().strip()
    return pm == "paycore"


async def _get_pending_non_akkula_order(user_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    try:
        db = await get_db()
        cur = await db.execute(
            """
            SELECT order_id, operator_id, btc_amount, wallet, comment, total_rub, bank_card, bank_name
              FROM p2p_orders
             WHERE user_id = ?
               AND status = 'pending'
               AND IFNULL(LOWER(payment_method),'') != 'akkula'
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (user_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None, None
        cols = ["order_id", "operator_id", "btc_amount", "wallet", "comment", "total_rub", "bank_card", "bank_name"]
        d = dict(zip(cols, row))
        oid = int(d["order_id"])
        return d, oid
    except Exception:
        return None, None


async def instruction_callback_handler(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        user_id = int((callback.data or "").split(":", 1)[1])
    except Exception:
        await callback.message.answer("⚠️ Неверные данные.")
        return

    operator = await get_user(callback.from_user.id)
    if not operator or operator.get("role") not in ("Operator", "Admin", "MasterCard"):
        return

    with suppress(Exception):
        from services.ff import check_wallets_status
        btc_ok, ton_ok = await check_wallets_status()
        if not (btc_ok and ton_ok):
            with suppress(Exception):
                await bot_send(
                    callback.bot,
                    user_id,
                    "⚙️ Обмен временно недоступен: сервис ведёт технические работы с кошельками. "
                    "Пожалуйста, попробуйте позже.",
                )
            with suppress(Exception):
                await callback.bot.send_message(
                    callback.from_user.id,
                    "⚠️ Поиск оператора остановлен, обменник на тех. обслуживании!",
                    reply_markup=DEFAULT_OP_KB,
                )
            return

    prompt = await bot_send(callback.bot, callback.from_user.id, "💰 Введите сумму к оплате в рублях:")
    await state.update_data(user_id=user_id, prompt_id=prompt.message_id)
    await InstructionStates.waiting_amount.set()


async def process_amount(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    if pid := data.get("prompt_id"):
        with suppress(Exception):
            await safe_delete(message.bot, message.chat.id, pid)

    text = (message.text or "").strip()
    if text.lower() == "отменить":
        await state.finish()
        await message.bot.send_message(message.chat.id, "Инструкция отменена.", reply_markup=DEFAULT_OP_KB)
        return

    try:
        amount = float(text.replace(",", "."))
    except Exception:
        await bot_send(message.bot, message.chat.id, "⚠️ Введите сумму числом.")
        return

    await state.update_data(amount=amount)
    prompt = await bot_send(message.bot, message.chat.id, INSTRUCTION_CARD_PROMPT)
    await state.update_data(prompt_id=prompt.message_id)
    await InstructionStates.waiting_card.set()


async def process_card(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    if pid := data.get("prompt_id"):
        with suppress(Exception):
            await safe_delete(message.bot, message.chat.id, pid)

    raw = (message.text or "").strip()
    if raw.lower() == "отменить":
        await state.finish()
        await message.bot.send_message(message.chat.id, "Инструкция отменена.", reply_markup=DEFAULT_OP_KB)
        return

    await state.update_data(card=raw)
    prompt = await bot_send(message.bot, message.chat.id, INSTRUCTION_BANK_PROMPT)
    await state.update_data(prompt_id=prompt.message_id)
    await InstructionStates.waiting_bank.set()


async def process_bank(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    if pid := data.get("prompt_id"):
        with suppress(Exception):
            await safe_delete(message.bot, message.chat.id, pid)

    raw = (message.text or "").strip()
    if raw.lower() == "отменить":
        await state.finish()
        await message.bot.send_message(message.chat.id, "Инструкция отменена.", reply_markup=DEFAULT_OP_KB)
        return

    await state.update_data(bank=raw)
    prompt = await bot_send(message.bot, message.chat.id, INSTRUCTION_COMMENT_PROMPT)
    await state.update_data(prompt_id=prompt.message_id)
    await InstructionStates.waiting_comment.set()


async def process_comment(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    if pid := data.get("prompt_id"):
        with suppress(Exception):
            await safe_delete(message.bot, message.chat.id, pid)

    raw = (message.text or "").strip()
    if raw.lower() == "отменить":
        await state.finish()
        await message.bot.send_message(message.chat.id, "Инструкция отменена.", reply_markup=DEFAULT_OP_KB)
        return

    comment = raw if raw.lower() not in ("нет", "-", "—") else "без комментария"

    data = await state.get_data()
    card_raw = str(data["card"])
    bank_raw = str(data["bank"])
    amount = data["amount"]
    user_id = int(data["user_id"])
    operator_id = int(message.from_user.id)

    if user_id in pending_buy_messages:
        chat_id, msg_id = pending_buy_messages.pop(user_id)
        with suppress(Exception):
            await message.bot.delete_message(chat_id, msg_id)

    pending, db_order_id = await _get_pending_non_akkula_order(user_id)
    is_web_order = _is_web_order(pending)
    is_web_guest_order = str((pending or {}).get("comment") or "").strip().upper().startswith("WEB-GUEST")

    if pending and db_order_id is not None:
        try:
            db = await get_db()
            await db.execute(
                """
                UPDATE p2p_orders
                   SET bank_card = ?, bank_name = ?, operator_id = ?
                 WHERE order_id = ?
                """,
                (card_raw, bank_raw, operator_id, db_order_id),
            )
            await db.commit()
        except Exception:
            logger.exception("Failed to update p2p_orders bank реквизиты (order_id=%s)", db_order_id)

    card_user_html = format_requisite_for_user(card_raw)
    card_op_html = f"<code>{html_escape(card_raw)}</code>"

    text_op = INSTRUCTION_TEXT_TEMPLATE.format(
        card=card_op_html,
        bank=html_escape(bank_raw),
        comment=html_escape(comment),
        amount=amount,
    )
    text_us = INSTRUCTION_TEXT_TEMPLATE_USER.format(
        card=card_user_html,
        bank=html_escape(bank_raw),
        comment=html_escape(comment),
        amount=amount,
    )

    if db_order_id is not None:
        paid_cb = f"paid:{db_order_id}"
        cancel_cb = f"cancel_pay:{db_order_id}"
    else:
        paid_cb = "paid"
        cancel_cb = "cancel_pay"

    ikb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Оплатил", callback_data=paid_cb),
        InlineKeyboardButton("🚫 Отменить", callback_data=cancel_cb),
    )

    # Для обычной Telegram-ветки и для WEB-пользователя с Telegram ID
    # реквизиты отправляем пользователю в Telegram.
    # Не отправляем только настоящему WEB-гостю.
    if (not is_web_order) or (is_web_order and not is_web_guest_order and user_id > 0):
        await bot_send(message.bot, user_id, text_us, parse_mode="HTML", reply_markup=ikb)

    sent_op = await message.bot.send_message(
        operator_id,
        text_op,
        parse_mode="HTML",
        reply_markup=DEFAULT_OP_KB,
    )

    if db_order_id is not None:
        STATE.pending_operator_order_cards[(operator_id, int(db_order_id))] = (sent_op.chat.id, sent_op.message_id)

    await state.finish()



async def _get_mastercard_owner_id_for_order(order: Optional[Dict[str, Any]]) -> Optional[int]:
    """
    Возвращает владельца карты Mastercard по card_id заявки.

    Для WEB/VidraPay-заявок это позволяет отправлять подтверждение оплаты
    не всем админам, а именно тому Mastercard, чья карта была выдана пользователю.
    """
    if not order:
        return None

    try:
        card_id = int((order or {}).get("card_id") or 0)
    except Exception:
        card_id = 0

    if card_id <= 0:
        return None

    try:
        from db.cards import get_card_by_id

        card = await get_card_by_id(card_id)
    except Exception:
        logger.exception("Failed to load Mastercard owner for card_id=%s", card_id)
        return None

    if not card:
        return None

    try:
        owner_id = int(card.get("owner_id") or 0)
    except Exception:
        owner_id = 0

    if owner_id <= 0:
        return None

    try:
        owner = await get_user(owner_id)
    except Exception:
        owner = None

    role = str((owner or {}).get("role") or "").strip().lower()
    if role != "mastercard":
        return None

    return owner_id


async def handle_paid(callback: types.CallbackQuery) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    callback_user_id = int(callback.from_user.id)
    parts = (callback.data or "").split(":")
    order_id_btn: Optional[int] = None
    if len(parts) >= 2:
        with suppress(Exception):
            order_id_btn = int(parts[1])

    p2p: Optional[Dict[str, Any]] = None

    if order_id_btn is not None:
        with suppress(Exception):
            from db.p2p import get_order_by_id
            p2p = await get_order_by_id(order_id_btn)

    if not p2p:
        p2p = await get_pending_order(callback_user_id)

    if not p2p:
        with suppress(Exception):
            await callback.bot.send_message(
                callback_user_id,
                "⚠️ Активная заявка не найдена.",
            )
        return

    order_user_id = int(p2p.get("user_id") or 0)
    is_guest_user = order_user_id <= 0
    is_web_order = _is_web_order(p2p)

    if (not is_guest_user) and (not is_web_order) and (order_user_id != callback_user_id):
        return

    operator_id = p2p.get("operator_id")
    db_order_id = p2p.get("order_id", "—")

    try:
        order_id_int = int(db_order_id)
    except Exception:
        order_id_int = None

    asset = _extract_asset_from_comment(str((p2p or {}).get("comment") or ""))
    try:
        amount = float((p2p or {}).get("btc_amount") or 0)
    except Exception:
        amount = 0.0

    wallet = str((p2p or {}).get("wallet") or "")
    if not wallet and asset == "BTC" and not is_guest_user and not is_web_order:
        with suppress(Exception):
            user = await get_user(order_user_id)
            if user:
                wallet = str(user.get("btc_wallet") or "")

    amount_str = _format_amount_for_asset(asset, amount)
    short_wallet = _short_wallet(wallet)

    can_send_user_card = (not is_guest_user) and ((not is_web_order) or _is_vidrapay_order(p2p))
    if can_send_user_card and order_id_int is not None:
        try:
            from db.p2p import try_claim_p2p_action
            can_send_user_card = await try_claim_p2p_action(order_id_int, "user_paid_status_card")
        except Exception:
            can_send_user_card = False

    if can_send_user_card and not is_guest_user:
        card_text = (
            f"🧾 Заявка №{db_order_id}\n\n"
            f"Монета: {asset}\n"
            f"Сумма: {amount_str} {asset}\n"
            f"Адрес: {short_wallet}\n\n"
            f"1️⃣ Оплата получена — ⏳\n"
            f"2️⃣ Средства на обменнике — ⏳\n"
            f"3️⃣ Перевод на ваш кошелёк — ⏳\n\n"
            "❗️ Если обмен не начнется в течение 10 минут — напишите в поддержку. Кнопка появится автоматически под этим сообщением!"
        )

        sent_card = await callback.bot.send_message(order_user_id, card_text, parse_mode="HTML")
        if order_id_int is not None:
            STATE.status_cards[order_id_int] = (sent_card.chat.id, sent_card.message_id)
            asyncio.create_task(
                _show_support_button_after_wait(
                    callback.bot,
                    order_id=int(order_id_int),
                    user_id=int(order_user_id),
                    message_id=int(sent_card.message_id),
                )
            )
            with suppress(Exception):
                from db.p2p import mark_p2p_action_sent
                await mark_p2p_action_sent(order_id_int, "user_paid_status_card", message_id=sent_card.message_id)

    card = (p2p or {}).get("bank_card", "—")
    bank = (p2p or {}).get("bank_name", "—")
    amount_val = (p2p or {}).get("total_rub")
    try:
        amount_rub = int(float(amount_val)) if amount_val is not None else "—"
    except Exception:
        amount_rub = amount_val or "—"

    saved_user_link = str((p2p or {}).get("user_link") or "").strip()

    if saved_user_link:
        mention = saved_user_link
    elif is_web_order and is_guest_user:
        mention = f"web-guest / user_id {order_user_id}"
    elif is_web_order:
        try:
            chat = await callback.bot.get_chat(order_user_id)
            if getattr(chat, "username", None):
                mention = f"@{chat.username}"
            else:
                mention = html_escape(getattr(chat, "full_name", str(order_user_id)))
        except Exception:
            mention = f"WEB user_id {order_user_id}"
    else:
        try:
            chat = await callback.bot.get_chat(order_user_id)
            mention = f"@{chat.username}" if getattr(chat, "username", None) else f"{chat.full_name}"
        except Exception:
            mention = f"user_id {order_user_id}"

    ikb_mastercard = InlineKeyboardMarkup()
    ikb_mastercard.row(
        InlineKeyboardButton("🧾 Чек", callback_data=f"op_view_receipt:{db_order_id}:{order_user_id}"),
        InlineKeyboardButton("✉️ SMS-чат", callback_data=f"operator_message:{order_user_id}:{db_order_id}"),
    )
    ikb_mastercard.add(
        InlineKeyboardButton("✅ Готово — начать обмен", callback_data=f"ff_ready:{db_order_id}:{order_user_id}")
    )

    ikb_admin = InlineKeyboardMarkup()
    ikb_admin.row(
        InlineKeyboardButton("🧾 Чек", callback_data=f"op_view_receipt:{db_order_id}:{order_user_id}"),
        InlineKeyboardButton("✉️ SMS-чат", callback_data=f"operator_message:{order_user_id}:{db_order_id}"),
    )
    ikb_admin.add(
        InlineKeyboardButton("✅ Готово — начать обмен", callback_data=f"ff_ready:{db_order_id}:{order_user_id}")
    )
    ikb_admin.add(InlineKeyboardButton("✅ Завершить", callback_data=f"finish_order:{db_order_id}:{order_user_id}"))

    action_name = "operator_paid_notify_web" if is_web_order else "operator_paid_notify"

    can_notify = True
    if order_id_int is not None:
        try:
            from db.p2p import try_claim_p2p_action
            can_notify = await try_claim_p2p_action(order_id_int, action_name)
        except Exception:
            can_notify = False

    if not can_notify:
        return

    recipients: List[Tuple[int, InlineKeyboardMarkup, str]] = []

    # Mastercard-владелец карты получает рабочее уведомление без кнопки «Завершить».
    mastercard_owner_id: Optional[int] = None
    with suppress(Exception):
        mastercard_owner_id = await _get_mastercard_owner_id_for_order(p2p)

    mastercard_mention = "—"
    if mastercard_owner_id:
        with suppress(Exception):
            mc_chat = await callback.bot.get_chat(int(mastercard_owner_id))
            if getattr(mc_chat, "username", None):
                mastercard_mention = f"@{mc_chat.username}"
            else:
                mastercard_mention = html_escape(getattr(mc_chat, "full_name", str(mastercard_owner_id)))

        if mastercard_mention == "—":
            with suppress(Exception):
                mc_user = await get_user(int(mastercard_owner_id))
                raw_mc_username = str((mc_user or {}).get("username") or "").strip()
                if raw_mc_username:
                    mastercard_mention = raw_mc_username if raw_mc_username.startswith("@") else f"@{raw_mc_username}"
                else:
                    mastercard_mention = f"user_id {int(mastercard_owner_id)}"

    wallet_full = str(wallet or "").strip() or "—"
    if wallet_full != "—" and len(wallet_full) > 12:
        wallet_for_mastercard = f"{wallet_full[:4]}…{wallet_full[-5:]}"
    else:
        wallet_for_mastercard = wallet_full

    mastercard_text = (
        "‼️Подтверждение оплаты‼️\n\n"
        f"🆔 Заявка №{html_escape(db_order_id)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Монета: {html_escape(asset)}\n"
        f"📦 К выдаче: {html_escape(amount_str)} {html_escape(asset)}\n"
        f"🏷 Кошелёк: {html_escape(wallet_for_mastercard)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💳 Карта/СБП: {html_escape(card)}\n"
        f"🏦 Банк: {html_escape(bank)}\n"
        f"💸 Сумма: {html_escape(amount_rub)} ₽"
    )

    admin_text = (
        "‼️Подтверждение оплаты‼️\n\n"
        f"👤 {mention}\n"
        f"👤 ID: {html_escape(order_user_id)}\n"
        f"🧑‍💼 Mastercard: {mastercard_mention}\n\n"
        f"🆔 Заявка №{html_escape(db_order_id)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Монета: {html_escape(asset)}\n"
        f"📦 К выдаче: {html_escape(amount_str)} {html_escape(asset)}\n"
        f"🏷 Кошелёк: {html_escape(wallet_full)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💳 Карта: {html_escape(card)}\n"
        f"🏦 Банк: {html_escape(bank)}\n"
        f"💸 Сумма: {html_escape(amount_rub)} ₽"
    )

    if mastercard_owner_id:
        recipients.append((int(mastercard_owner_id), ikb_mastercard, "mastercard"))

    # Админы получают такое же уведомление, но с кнопкой ручного завершения.
    # Это нужно и для WEB/VidraPay, и для обычной P2P-ветки после подтверждения оплаты.
    admin_ids = await _admin_ids()
    for aid in admin_ids:
        try:
            aid_int = int(aid)
        except Exception:
            continue
        recipients.append((aid_int, ikb_admin, "admin"))

    # Для старых обычных сценариев, где operator_id не является Mastercard/Admin,
    # оставляем уведомление назначенному оператору.
    if (not is_web_order) and operator_id:
        try:
            op_id_int = int(operator_id)
        except Exception:
            op_id_int = 0

        if op_id_int > 0 and all(rid != op_id_int for rid, _, _ in recipients):
            recipients.append((op_id_int, ikb_admin, "operator"))

    # Дедупликация: одному Telegram ID отправляем одно уведомление.
    unique_recipients: List[Tuple[int, InlineKeyboardMarkup, str]] = []
    seen_recipient_ids: Set[int] = set()
    for rid, kb, kind in recipients:
        if rid <= 0 or rid in seen_recipient_ids:
            continue
        seen_recipient_ids.add(rid)
        unique_recipients.append((rid, kb, kind))

    if is_web_order and not unique_recipients:
        logger.warning(
            "VidraPay WEB order has no recipients for paid notification: order_id=%s card_id=%s",
            db_order_id,
            (p2p or {}).get("card_id"),
        )
        with suppress(Exception):
            await callback.bot.send_message(
                callback_user_id,
                "⚠️ Не удалось отправить уведомление по этой оплате. Напишите в поддержку.",
            )
        return

    for recipient_id, recipient_kb, _kind in unique_recipients:
        notification_text = mastercard_text if _kind == "mastercard" else admin_text

        with suppress(Exception):
            sent = await callback.bot.send_message(
                int(recipient_id),
                notification_text,
                parse_mode="HTML",
                reply_markup=recipient_kb,
                disable_web_page_preview=True,
            )
            STATE.pending_ff_ready_buttons[(int(recipient_id), int(db_order_id))] = (
                sent.chat.id,
                sent.message_id,
            )
            if order_id_int is not None:
                await _remember_order_ui_message(int(order_id_int), sent.chat.id, sent.message_id)

    if order_id_int is not None:
        with suppress(Exception):
            from db.p2p import mark_p2p_action_sent
            await mark_p2p_action_sent(order_id_int, action_name)

async def handle_cancel(callback: types.CallbackQuery) -> None:
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()

    user_id = int(callback.from_user.id)
    parts = (callback.data or "").split(":")
    order_id_btn: Optional[int] = None
    if len(parts) >= 2:
        with suppress(Exception):
            order_id_btn = int(parts[1])

    if order_id_btn is not None:
        p2p: Optional[Dict[str, Any]] = None
        with suppress(Exception):
            from db.p2p import get_order_by_id
            p2p = await get_order_by_id(order_id_btn)

        if p2p and int(p2p.get("user_id") or 0) != user_id:
            return

        operator_id = (p2p or {}).get("operator_id")
        if operator_id:
            mention = await _build_mention(callback.bot, user_id)
            with suppress(Exception):
                await callback.bot.send_message(
                    int(operator_id),
                    f"🚫 Пользователь {mention} отменил инструкцию по заявке №<b>{order_id_btn}</b>.",
                    parse_mode="HTML",
                )

        if not p2p:
            await callback.bot.send_message(
                user_id,
                "Не удалось найти заявку для отмены.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await send_welcome(callback.bot, user_id)
            return

        try:
            db = await get_db()
            await db.execute(
                """
                UPDATE p2p_orders
                   SET status = 'canceled'
                 WHERE order_id = ?
                   AND user_id = ?
                   AND status != 'completed'
                """,
                (int(order_id_btn), int(user_id)),
            )
            await db.commit()
        except Exception:
            logger.exception("Failed to cancel single order (order_id=%s user_id=%s)", order_id_btn, user_id)

        await callback.bot.send_message(
            user_id,
            "Вы отменили инструкцию по этой заявке. Возврат в главное меню.",
            reply_markup=ReplyKeyboardRemove(),
        )
        with suppress(Exception):
            await clear_history(callback.bot, {}, user_id)
        await send_welcome(callback.bot, user_id)
        return

    p2p = await get_pending_order(user_id)
    operator_id = p2p["operator_id"] if p2p else None

    if operator_id:
        mention = await _build_mention(callback.bot, user_id)
        with suppress(Exception):
            await callback.bot.send_message(int(operator_id), f"🚫 Пользователь {mention} отменил инструкцию.", parse_mode="HTML")

    if not p2p:
        await callback.bot.send_message(
            user_id,
            "Нет активной инструкции для отмены.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await send_welcome(callback.bot, user_id)
        return

    await delete_order(user_id)
    await callback.bot.send_message(
        user_id,
        "Вы отменили инструкцию. Возврат в главное меню.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await clear_history(callback.bot, {}, user_id)
    await send_welcome(callback.bot, user_id)


async def start_exchange_from_p2p(*, bot: Bot, p2p: Dict[str, Any], operator_id: Optional[int] = None) -> bool:
    """
    Запускает обмен по заявке.

    Возвращает:
    - True  -> обмен реально стартовал, FF-ордер создан, средства отправлены в обменник,
               запущен трекинг статуса
    - False -> старт обмена не состоялся

    Важно:
    - при ошибках старта НЕ отправляем пользователю уведомления об ошибках
    - отдельные текстовые ошибки оператору НЕ шлём:
      оператор получает ту же карточку ошибки, что и все админы, из handle_ff_ready()

    Логика:
    - старая рабочая логика покупки TON сохраняется;
    - если USDT не хватает, бот продаёт любые другие активы в USDT;
    - в FF отправляется deposit_amount + 0.03 TON, чтобы после комиссии Binance
      в FF пришла ровно сумма, которую запросил FF;
    - прибыль считается как total_rub - rub_amount;
    - если Split включён — прибыль переводится в TON и отправляется на 2 TON-кошелька в пропорции 60/40;
    - если Split выключен — прибыль остаётся на Binance и никуда не выводится.
    """
    if not p2p:
        return False

    PROFIT_WALLET_60 = "UQAYuvlCvD4SyfH2GxRKbr65mpX19nbwnDS7tzmFUsBGB2rn"
    PROFIT_WALLET_40 = "UQCpK5mq8O6xc3AbdRbqiIm2ouRAr-BCLrjpZa9CpVlS24QE"

    db_order_id = int(p2p["order_id"])
    user_id = int(p2p.get("user_id") or 0)
    started_ok = False

    if db_order_id in STATE.started_exchanges:
        return False
    STATE.started_exchanges.add(db_order_id)

    try:
        with suppress(Exception):
            await _cleanup_receipt_ui(bot, user_id)

        with suppress(Exception):
            if user_id in pending_buy_messages:
                chat_id, msg_id = pending_buy_messages.pop(user_id)
                with suppress(Exception):
                    await bot.delete_message(chat_id, msg_id)

        # Не удаляем пользовательскую карточку статуса при старте обмена.
        # Если автообмен упадёт, пользователь должен продолжать видеть процесс с 1 галочкой,
        # а после ручного завершения эта же карточка обновится до 3 галочек.

        can_start = True
        try:
            from db.p2p import try_claim_p2p_action
            can_start = await try_claim_p2p_action(int(db_order_id), "exchange_start")
        except Exception:
            can_start = False

        if not can_start:
            return False

        try:
            from services.ff import check_wallets_status
            btc_ok, ton_ok = await check_wallets_status()
        except Exception:
            btc_ok, ton_ok = True, True

        if not (btc_ok and ton_ok):
            mention = await _build_mention(bot, user_id)
            await _notify_admin(
                bot,
                "⚠️ <b>Автообмен остановлен: FF wallets maintenance</b>\n\n"
                f"Заявка: <b>#{db_order_id}</b>\n"
                f"Пользователь: {mention}",
            )
            with suppress(Exception):
                from db.p2p import mark_p2p_action_failed
                await mark_p2p_action_failed(int(db_order_id), "exchange_start", error="FF wallets maintenance")
            return False

        try:
            crypto_amount = float(p2p.get("btc_amount") or 0)
        except Exception:
            crypto_amount = 0.0

        asset = _extract_asset_from_comment(str(p2p.get("comment") or ""))

        wallet = p2p.get("wallet")
        if not wallet:
            with suppress(Exception):
                user = await get_user(user_id)
                wallet = user.get("btc_wallet") if user else None

        if not wallet:
            mention = await _build_mention(bot, user_id)
            await _notify_admin(
                bot,
                "❌ <b>Ошибка автозапуска обмена</b>\n\n"
                f"Заявка: <b>#{db_order_id}</b>\n"
                f"Пользователь: {mention}\n"
                "Причина: <b>не найден кошелёк получателя</b>",
            )
            with suppress(Exception):
                from db.p2p import mark_p2p_action_failed
                await mark_p2p_action_failed(int(db_order_id), "exchange_start", error="wallet not found")
            return False

        wallet_str = str(wallet).strip()
        wl_lower = wallet_str.lower()

        if asset == "LTC":
            to_ccy = "LTC"
        elif asset == "USDT":
            to_ccy = getattr(settings, "FF_USDT_CCY", "USDTTRC")
        elif asset == "XMR":
            to_ccy = "XMR"
        else:
            to_ccy = "BTC"

        if asset == "BTC" and wallet_str:
            if wl_lower.startswith("ltc1") or wallet_str[:1] in ("L", "M", "l", "m"):
                to_ccy = "LTC"

        if db_order_id not in STATE.status_cards and user_id > 0:
            with suppress(Exception):
                db = await get_db()
                cur = await db.execute(
                    """
                    SELECT message_id
                      FROM p2p_order_actions
                     WHERE order_id = ?
                       AND action = 'user_paid_status_card'
                       AND status = 'sent'
                       AND message_id IS NOT NULL
                     ORDER BY updated_at DESC
                     LIMIT 1
                    """,
                    (int(db_order_id),),
                )
                row = await cur.fetchone()
                with suppress(Exception):
                    await cur.close()
                if row and row[0]:
                    STATE.status_cards[int(db_order_id)] = (int(user_id), int(row[0]))

        if db_order_id not in STATE.status_cards:
            can_create_card = True
            try:
                from db.p2p import try_claim_p2p_action
                can_create_card = await try_claim_p2p_action(int(db_order_id), "user_exchange_status_card")
            except Exception as e:
                logger.exception(
                    "try_claim_p2p_action(user_exchange_status_card) failed for order_id=%s: %s",
                    db_order_id,
                    e,
                )
                can_create_card = True

            if can_create_card:
                amount_str = _format_amount_for_asset(asset, crypto_amount)
                card_text = (
                    f"🧾 Заявка №{db_order_id}\n\n"
                    f"Монета: {asset}\n"
                    f"Сумма: {amount_str} {asset}\n"
                    f"Адрес: {_short_wallet(wallet_str)}\n\n"
                    f"1️⃣ Оплата получена — ✅\n"
                    f"2️⃣ Средства на обменнике — ⏳\n"
                    f"3️⃣ Перевод на ваш кошелёк — ⏳\n\n"
                    "❗️ Если обмен не начнется в течение 10 минут — напишите в поддержку."
                )
                with suppress(Exception):
                    sent_card = await bot.send_message(user_id, card_text, parse_mode="HTML")
                    STATE.status_cards[db_order_id] = (sent_card.chat.id, sent_card.message_id)
                    with suppress(Exception):
                        from db.p2p import mark_p2p_action_sent
                        await mark_p2p_action_sent(int(db_order_id), "user_exchange_status_card", message_id=sent_card.message_id)

        await _update_user_status_card(
            bot=bot,
            order_id=db_order_id,
            asset=asset,
            amount=crypto_amount,
            wallet=wallet_str,
            step1_done=True,
            step2_done=False,
            step3_done=False,
        )

        try:
            ff_res = await create_order(
                from_ccy="TON",
                to_ccy=to_ccy,
                amount=float(crypto_amount),
                direction="to",
                order_type="fixed",
                to_address=wallet_str,
            )
        except FFAPIError as e:
            if _is_binance_order(p2p):
                mention = await _build_mention(bot, user_id)
                await _notify_admin(
                    bot,
                    "❌ <b>Binance-заявка: ошибка создания FF-ордера</b>\n\n"
                    f"Заявка: <b>#{db_order_id}</b>\n"
                    f"Пользователь: {mention}\n"
                    f"Монета: <b>{html_escape(asset)}</b>\n"
                    f"К выдаче: <b>{html_escape(_format_amount_for_asset(asset, crypto_amount))} {html_escape(asset)}</b>\n"
                    f"Кошелёк: <code>{html_escape(wallet_str)}</code>\n"
                    f"Ошибка: <code>{html_escape(str(e))}</code>",
                )

            with suppress(Exception):
                from db.p2p import mark_p2p_action_failed
                await mark_p2p_action_failed(int(db_order_id), "exchange_start", error=f"FF create_order error: {e}")
            return False
        except Exception as e:
            with suppress(Exception):
                from db.p2p import mark_p2p_action_failed
                await mark_p2p_action_failed(int(db_order_id), "exchange_start", error=str(e)[:500])
            return False

        with suppress(Exception):
            from db.p2p import mark_p2p_action_sent
            await mark_p2p_action_sent(int(db_order_id), "exchange_start")

        ff_order_id = str(
            ff_res.get("id")
            or ff_res.get("orderId")
            or ff_res.get("order_id")
            or ""
        ).strip()
        public_id = str(
            ff_res.get("token")
            or ff_res.get("publicId")
            or ff_res.get("public_id")
            or ff_res.get("public")
            or ff_res.get("publicToken")
            or ""
        ).strip()

        from_info = ff_res.get("from") or {}
        deposit_addr = str(from_info.get("address") or "").strip()
        deposit_memo = str(from_info.get("memo") or from_info.get("tag") or "").strip()
        deposit_amount_raw = from_info.get("amount", "0")

        try:
            deposit_amount = Decimal(str(deposit_amount_raw or "0")).quantize(
                Decimal("0.00000001"),
                rounding=ROUND_DOWN,
            )
        except Exception:
            deposit_amount = Decimal("0")

        if not ff_order_id or not public_id or not deposit_addr or deposit_amount <= 0:
            err = (
                "create_order вернул неполные данные: "
                f"id={ff_order_id or '—'}, token={public_id or '—'}, "
                f"address={deposit_addr or '—'}, amount={deposit_amount}"
            )
            with suppress(Exception):
                from db.p2p import mark_p2p_action_failed
                await mark_p2p_action_failed(int(db_order_id), "exchange_start", error=err[:500])
            return False

        with suppress(Exception):
            from db.p2p import update_p2p_order_token
            await update_p2p_order_token(int(db_order_id), ff_order_id=ff_order_id, public_id=public_id)

        client = BinanceClient()

        async def _withdraw_ton_with_one_retry(*, amount_dec: Decimal, address: str, memo: str) -> None:
            amt_str = f"{amount_dec:.8f}"
            try:
                await client.withdrawal_ton(
                    amount=float(amt_str),
                    address=address,
                    network="TON",
                    memo=memo,
                )
                return
            except Exception:
                pass

            await asyncio.sleep(10)
            await client.withdrawal_ton(
                amount=float(amt_str),
                address=address,
                network="TON",
                memo=memo,
            )

        async def _auto_prepare_and_withdraw_regular() -> None:
            last_free_ton: Optional[Decimal] = None
            last_free_usdt: Optional[Decimal] = None
            last_price_tonusdt: Optional[Decimal] = None
            last_need_ton: Optional[Decimal] = None
            last_need_usdt: Optional[Decimal] = None

            TON_Q = Decimal("0.00000001")
            MIN_TONUSDT_NOTIONAL = Decimal("5.10")
            BUY_MARGIN = Decimal("1.015")
            EXTRA_TON_BUFFER = Decimal("0.05")

            async def _get_free_ton() -> Decimal:
                nonlocal last_free_ton
                try:
                    bal_ton = await client.get_balance(asset="TON")
                    free_ton = Decimal(str(bal_ton.get("free", "0") or "0")).quantize(
                        TON_Q,
                        rounding=ROUND_DOWN,
                    )
                except Exception:
                    free_ton = Decimal("0")
                last_free_ton = free_ton
                return free_ton

            async def _get_free_usdt() -> Decimal:
                nonlocal last_free_usdt
                try:
                    bal_usdt = await client.get_balance("USDT")
                    free_usdt = Decimal(str(bal_usdt.get("free", "0") or "0")).quantize(
                        TON_Q,
                        rounding=ROUND_DOWN,
                    )
                except Exception:
                    free_usdt = Decimal("0")
                last_free_usdt = free_usdt
                return free_usdt

            async def _buy_ton_for_usdt(need_usdt: Decimal) -> None:
                await client.convert_usdt_to_ton(float(f"{need_usdt:.8f}"))
                for _ in range(12):
                    await asyncio.sleep(1)
                    free_ton = await _get_free_ton()
                    if free_ton > 0:
                        break

            dep = Decimal(str(deposit_amount)).quantize(TON_Q, rounding=ROUND_DOWN)

            # В FF отправляем сумму + fee Binance, чтобы в FF пришёл ровно dep
            desired_withdraw_ff = (dep + TON_WITHDRAW_FEE).quantize(
                TON_Q,
                rounding=ROUND_DOWN,
            )

            # Прибыль считаем строго от рублёвой комиссии заявки
            try:
                total_rub_dec = Decimal(str(p2p.get("total_rub") or "0")).quantize(TON_Q, rounding=ROUND_DOWN)
            except Exception:
                total_rub_dec = Decimal("0")

            try:
                rub_amount_dec = Decimal(str(p2p.get("rub_amount") or "0")).quantize(TON_Q, rounding=ROUND_DOWN)
            except Exception:
                rub_amount_dec = Decimal("0")

            profit_rub_dec = (total_rub_dec - rub_amount_dec).quantize(TON_Q, rounding=ROUND_DOWN)
            if profit_rub_dec < 0:
                profit_rub_dec = Decimal("0")

            profit_ton_target = Decimal("0")
            if total_rub_dec > 0 and profit_rub_dec > 0:
                from utils.helpers import get_usd_rub

                usd_rub_raw = await get_usd_rub()
                usd_rub_rate = Decimal(str(usd_rub_raw or "0")).quantize(TON_Q, rounding=ROUND_DOWN)
                if usd_rub_rate <= 0:
                    raise RuntimeError("Не удалось получить курс USD/RUB для расчёта прибыли в TON.")

                price = Decimal(str(await client.get_price("TONUSDT"))).quantize(TON_Q, rounding=ROUND_DOWN)
                if price <= 0:
                    raise RuntimeError("Не удалось получить цену TONUSDT.")

                last_price_tonusdt = price

                profit_usdt_target = (profit_rub_dec / usd_rub_rate).quantize(TON_Q, rounding=ROUND_DOWN)
                if profit_usdt_target > 0:
                    profit_ton_target = (profit_usdt_target / price).quantize(TON_Q, rounding=ROUND_DOWN)

            desired_total_ton = (desired_withdraw_ff + profit_ton_target).quantize(
                TON_Q,
                rounding=ROUND_DOWN,
            )

            for _ in range(3):
                free_ton = await _get_free_ton()
                if free_ton >= desired_total_ton:
                    break

                deficit = (desired_total_ton - free_ton).quantize(
                    TON_Q,
                    rounding=ROUND_DOWN,
                )
                if deficit <= 0:
                    break

                need_ton = (deficit + EXTRA_TON_BUFFER).quantize(
                    TON_Q,
                    rounding=ROUND_DOWN,
                )
                last_need_ton = need_ton

                price = Decimal(str(await client.get_price("TONUSDT"))).quantize(
                    TON_Q,
                    rounding=ROUND_DOWN,
                )
                if price <= 0:
                    raise RuntimeError("Не удалось получить цену TONUSDT.")
                last_price_tonusdt = price

                need_usdt = (need_ton * price * BUY_MARGIN).quantize(
                    TON_Q,
                    rounding=ROUND_DOWN,
                )
                last_need_usdt = need_usdt

                free_usdt = await _get_free_usdt()

                if free_usdt < need_usdt:
                    balances = await client.get_spot_balances(only_nonzero=True)
                    for bal in balances:
                        asset_code = str(bal.get("asset") or "").upper().strip()
                        if not asset_code or asset_code in {"TON", "USDT"}:
                            continue
                        if free_usdt >= need_usdt:
                            break
                        try:
                            _, usdt_got = await client.sell_for_usdt(
                                asset_code,
                                float(need_usdt - free_usdt),
                            )
                        except Exception:
                            continue
                        if usdt_got and usdt_got > 0:
                            free_usdt = (free_usdt + Decimal(str(usdt_got))).quantize(
                                TON_Q,
                                rounding=ROUND_DOWN,
                            )

                free_usdt = await _get_free_usdt()
                if free_usdt <= 0:
                    raise RuntimeError("На балансе Binance нет USDT и не удалось продать другие активы для покупки TON.")

                if need_usdt > free_usdt:
                    need_usdt = free_usdt.quantize(TON_Q, rounding=ROUND_DOWN)
                    last_need_usdt = need_usdt

                if need_usdt < MIN_TONUSDT_NOTIONAL:
                    raise RuntimeError(
                        f"Сумма покупки TON меньше минимального notional TONUSDT: {need_usdt}"
                    )

                await _buy_ton_for_usdt(need_usdt)

            free_ton_final = await _get_free_ton()
            if free_ton_final < desired_withdraw_ff:
                short = (desired_withdraw_ff - free_ton_final).quantize(
                    TON_Q,
                    rounding=ROUND_DOWN,
                )
                raise RuntimeError(
                    f"После покупки TON всё равно недостаточно для вывода на депозит FixedFloat. Не хватает {short} TON."
                )

            # В FF уходит сумма с учётом комиссии Binance
            await _withdraw_ton_with_one_retry(
                amount_dec=desired_withdraw_ff,
                address=deposit_addr,
                memo=deposit_memo,
            )

            # После FF:
            # - если Split включён -> отправляем прибыль 60/40 на 2 TON-кошелька;
            # - если Split выключен -> прибыль остаётся на Binance.
            if profit_ton_target > 0:
                split_enabled = await is_ton_profit_split_enabled()

                free_ton_after_ff = await _get_free_ton()
                distributable_profit_ton = min(
                    profit_ton_target,
                    free_ton_after_ff.quantize(TON_Q, rounding=ROUND_DOWN),
                ).quantize(TON_Q, rounding=ROUND_DOWN)

                if distributable_profit_ton > 0:
                    if split_enabled:
                        profit_ton_60 = (distributable_profit_ton * Decimal("0.60")).quantize(
                            TON_Q,
                            rounding=ROUND_DOWN,
                        )
                        profit_ton_40 = (distributable_profit_ton - profit_ton_60).quantize(
                            TON_Q,
                            rounding=ROUND_DOWN,
                        )

                        if profit_ton_60 > 0:
                            await _withdraw_ton_with_one_retry(
                                amount_dec=profit_ton_60,
                                address=PROFIT_WALLET_60,
                                memo="",
                            )

                        if profit_ton_40 > 0:
                            await _withdraw_ton_with_one_retry(
                                amount_dec=profit_ton_40,
                                address=PROFIT_WALLET_40,
                                memo="",
                            )

                    STATE.profit_ton_by_order[db_order_id] = float(distributable_profit_ton)
                else:
                    STATE.profit_ton_by_order[db_order_id] = 0.0
            else:
                STATE.profit_ton_by_order[db_order_id] = 0.0

        try:
            await _auto_prepare_and_withdraw_regular()
        except Exception as e:
            with suppress(Exception):
                from db.p2p import mark_p2p_action_failed
                await mark_p2p_action_failed(
                    int(db_order_id),
                    "exchange_start",
                    error=f"TON withdraw error: {str(e)[:450]}",
                )
            return False

        with suppress(Exception):
            from db.p2p import set_ff_funds_sent_at
            await set_ff_funds_sent_at(int(db_order_id))

        with suppress(Exception):
            await _update_user_status_card(
                bot=bot,
                order_id=db_order_id,
                asset=asset,
                amount=crypto_amount,
                wallet=wallet_str,
                step1_done=True,
                step2_done=True,
                step3_done=False,
            )

        asyncio.create_task(
            track_ff_order_status(
                bot=bot,
                user_id=user_id,
                ff_order_id=ff_order_id,
                token=public_id,
                db_order_id=db_order_id,
                asset=asset,
                exchange_started_by_id=operator_id,
            )
        )

        started_ok = True
        return True

    finally:
        if not started_ok:
            STATE.started_exchanges.discard(db_order_id)



def _humanize_exchange_error(raw_error: Any) -> str:
    text = str(raw_error or "").strip()
    if not text:
        return "Не удалось запустить обмен. Точная причина не была сохранена."

    low = text.lower()

    if "wallet not found" in low:
        return "Не найден кошелёк получателя для отправки."

    if "ff wallets maintenance" in low:
        return "Обменник временно недоступен: кошельки FixedFloat находятся на техническом обслуживании."

    if "create_order вернул неполные данные" in low:
        return (
            "Обменник вернул неполные данные для старта обмена: "
            "не удалось получить все реквизиты для отправки."
        )

    if "ff create_order error:" in low:
        details = text.split(":", 1)[1].strip() if ":" in text else text

        details_low = details.lower()

        if "out of limits" in details_low or "limit" in details_low:
            return "Сумма заявки не проходит по лимитам обменника. Попробуйте изменить сумму."

        if "minimum" in details_low and "amount" in details_low:
            return "Сумма заявки меньше минимально допустимой для обменника."

        if "maximum" in details_low and "amount" in details_low:
            return "Сумма заявки превышает максимально допустимую для обменника."

        if "address" in details_low and ("invalid" in details_low or "not valid" in details_low):
            return "Указан некорректный адрес кошелька получателя."

        if "pair" in details_low and ("not found" in details_low or "unsupported" in details_low):
            return "Обменник не поддерживает выбранное направление обмена."

        if "insufficient liquidity" in details_low or "not enough liquidity" in details_low:
            return "В обменнике сейчас недостаточно ликвидности для выполнения этой заявки."

        if "maintenance" in details_low:
            return "Обменник временно недоступен из-за технических работ."

        if "timeout" in details_low:
            return "Обменник не ответил вовремя. Попробуйте запустить обмен ещё раз."

        if "too many requests" in details_low:
            return "Обменник временно отклонил запрос из-за слишком большого количества обращений. Попробуйте повторить позже."

        return f"Ошибка при создании заявки в обменнике: {details}"

    if "ton withdraw error:" in low:
        details = text.split(":", 1)[1].strip() if ":" in text else text
        details_low = details.lower()

        if "insufficient balance" in details_low:
            return "Недостаточно средств на Binance для отправки TON в обменник."

        if "minimum notional" in details_low or "notional" in details_low:
            return "Сумма покупки TON слишком маленькая для Binance."

        if "не удалось получить цену tonusdt" in low or "tonusdt" in details_low and "price" in details_low:
            return "Не удалось получить актуальную цену TON/USDT на Binance."

        if "нет usdt" in low or "no usdt" in details_low:
            return "На Binance недостаточно USDT для покупки TON."

        if "не хватает" in low and "ton" in low:
            return f"На Binance недостаточно TON для отправки в обменник: {details}"

        if "withdraw" in details_low and "disabled" in details_low:
            return "Вывод TON на Binance временно недоступен."

        if "timeout" in details_low:
            return "Binance не ответил вовремя при выводе TON. Попробуйте повторить запуск."

        return f"Ошибка при отправке TON в обменник: {details}"

    if "not enough" in low and "liquidity" in low:
        return "В обменнике сейчас недостаточно ликвидности для выполнения заявки."

    if "out of limits" in low or ("limit" in low and "error" in low):
        return "Сумма заявки не проходит по лимитам обменника."

    if "timeout" in low:
        return "Внешний сервис не ответил вовремя. Попробуйте повторить запуск обмена."

    if "insufficient balance" in low:
        return "Недостаточно средств для запуска обмена."

    if "invalid address" in low or ("address" in low and "invalid" in low):
        return "Указан некорректный адрес кошелька."

    if "too many requests" in low:
        return "Слишком много запросов к внешнему сервису. Попробуйте повторить чуть позже."

    return text


async def handle_ff_ready(callback: types.CallbackQuery) -> None:
    await callback.answer()

    operator_id = int(callback.from_user.id)
    operator = await get_user(operator_id)
    operator_role = str((operator or {}).get("role") or "").strip()

    if not operator or operator_role not in ("Operator", "Admin", "MasterCard"):
        with suppress(Exception):
            sent = await callback.bot.send_message(operator_id, "⚠️ Недостаточно прав для запуска обмена.")
            await _remember_order_ui_message(0, sent.chat.id, sent.message_id)
        return

    parts = (callback.data or "").split(":")
    order_id: Optional[int] = None
    user_id: Optional[int] = None

    try:
        if len(parts) >= 2:
            order_id = int(parts[1])
        if len(parts) >= 3:
            user_id = int(parts[2])
    except Exception:
        with suppress(Exception):
            sent = await callback.bot.send_message(operator_id, "⚠️ Неверные данные кнопки ff_ready.")
            await _remember_order_ui_message(0, sent.chat.id, sent.message_id)
        return

    if order_id is None:
        with suppress(Exception):
            sent = await callback.bot.send_message(operator_id, "⚠️ Не удалось определить order_id для запуска обмена.")
            await _remember_order_ui_message(0, sent.chat.id, sent.message_id)
        return

    p2p: Optional[Dict[str, Any]] = None
    with suppress(Exception):
        from db.p2p import get_order_by_id
        p2p = await get_order_by_id(int(order_id))

    if not p2p:
        with suppress(Exception):
            sent = await callback.bot.send_message(
                operator_id,
                f"⚠️ Заявка #{order_id} не найдена.",
            )
            await _remember_order_ui_message(int(order_id), sent.chat.id, sent.message_id)
        return

    real_order_id = int(p2p.get("order_id") or 0)
    real_user_id = int(p2p.get("user_id") or 0)
    order_user_id = real_user_id if user_id is None else int(user_id)

    def _build_order_kb(*, include_finish: bool) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("🧾 Чек", callback_data=f"op_view_receipt:{real_order_id}:{order_user_id}"),
            InlineKeyboardButton("✉️ SMS-чат", callback_data=f"operator_message:{order_user_id}:{real_order_id}"),
        )
        kb.add(
            InlineKeyboardButton("✅ Готово — начать обмен", callback_data=f"ff_ready:{real_order_id}:{order_user_id}")
        )
        if include_finish:
            kb.add(
                InlineKeyboardButton("✅ Завершить", callback_data=f"finish_order:{real_order_id}:{order_user_id}")
            )
        return kb

    mastercard_error_kb = _build_order_kb(include_finish=False)
    admin_error_kb = _build_order_kb(include_finish=True)

    async def _broadcast_exchange_error(error_text: str) -> None:
        current = p2p
        with suppress(Exception):
            from db.p2p import get_order_by_id
            refreshed = await get_order_by_id(int(real_order_id))
            if refreshed:
                current = refreshed

        saved_user_link = str((current or {}).get("user_link") or "").strip()
        is_web_order_local = _is_web_order(current)
        is_guest_user_local = int((current or {}).get("user_id") or 0) <= 0

        if saved_user_link:
            mention = saved_user_link
        elif is_web_order_local and is_guest_user_local:
            mention = f"web-guest / user_id {real_user_id}"
        else:
            mention = await _build_mention(callback.bot, real_user_id)

        card = str((current or {}).get("bank_card") or "—")
        bank = str((current or {}).get("bank_name") or "—")
        wallet_local = str((current or {}).get("wallet") or "").strip()
        asset_local = _extract_asset_from_comment(str((current or {}).get("comment") or ""))
        payment_method_raw = str((current or {}).get("payment_method") or "").strip().lower()

        if payment_method_raw == "paycore":
            payment_method = "PayCore"
        elif payment_method_raw == "akkula":
            payment_method = "Akkula"
        elif payment_method_raw == "p2p":
            payment_method = "P2P"
        elif payment_method_raw.startswith("vidrapay") or payment_method_raw.startswith("vidra-pay"):
            payment_method = "VidraPay"
        else:
            payment_method = payment_method_raw or "—"

        try:
            total_rub_raw = (current or {}).get("total_rub")
            amount_rub = int(float(total_rub_raw)) if total_rub_raw is not None else "—"
        except Exception:
            amount_rub = (current or {}).get("total_rub") or "—"

        try:
            crypto_amount = float((current or {}).get("btc_amount") or 0)
        except Exception:
            crypto_amount = 0.0

        amount_crypto_str = _format_amount_for_asset(asset_local, crypto_amount)
        human_error_text = _humanize_exchange_error(error_text)

        operator_name = ""
        try:
            op_chat = await callback.bot.get_chat(int(operator_id))
            operator_name = getattr(op_chat, "username", "") or getattr(op_chat, "full_name", "") or ""
        except Exception:
            operator_name = ""

        # Пользователь не должен видеть ошибку обмена.
        # Оставляем/возвращаем ему только карточку процесса с 1 галочкой.
        if real_user_id > 0:
            with suppress(Exception):
                await _update_user_status_card(
                    bot=callback.bot,
                    order_id=int(real_order_id),
                    asset=asset_local,
                    amount=float(crypto_amount),
                    wallet=wallet_local,
                    step1_done=True,
                    step2_done=False,
                    step3_done=False,
                    user_id=real_user_id,
                )

        common_details = (
            f"🆔 Заявка: <b>#{real_order_id}</b>\n"
            f"👤 Пользователь: {mention}\n"
            f"👤 ID: <b>{real_user_id}</b>\n"
            f"🧑‍💼 Инициатор: <b>{html_escape(operator_name or str(operator_id))}</b>\n"
            f"💳 Метод: <b>{html_escape(payment_method)}</b>\n\n"
            f"🪙 Монета: <b>{html_escape(asset_local)}</b>\n"
            f"📦 К выдаче: <b>{html_escape(amount_crypto_str)} {html_escape(asset_local)}</b>\n"
            f"🏷 Адрес: <code>{html_escape(wallet_local or '—')}</code>\n"
            f"💸 Сумма: <b>{html_escape(amount_rub)} ₽</b>\n"
            f"🏦 Банк: <b>{html_escape(bank)}</b>\n"
            f"💳 Реквизит: {format_requisite_for_user(card)}\n\n"
            f"Причина: <b>{html_escape(human_error_text)}</b>"
        )

        mastercard_error_text = (
            "🚨 <b>ОШИБКА ОБМЕНА!</b>\n\n"
            f"{common_details}"
        )

        admin_error_text = (
            "🚨 <b>ОШИБКА ОБМЕНА — НУЖНО РУЧНОЕ ЗАВЕРШЕНИЕ</b>\n\n"
            f"{common_details}"
        )

        # Mastercard должен получить ошибку заменой того же уведомления.
        initiator_kb = admin_error_kb if operator_role in ("Admin", "Operator") else mastercard_error_kb
        initiator_text = admin_error_text if operator_role in ("Admin", "Operator") else mastercard_error_text

        initiator_updated = False
        with suppress(Exception):
            edited = await callback.message.edit_text(
                initiator_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=initiator_kb,
            )
            STATE.pending_ff_ready_buttons[(int(operator_id), int(real_order_id))] = (
                edited.chat.id,
                edited.message_id,
            )
            await _remember_order_ui_message(int(real_order_id), edited.chat.id, edited.message_id)
            initiator_updated = True

        if not initiator_updated:
            with suppress(Exception):
                sent = await callback.bot.send_message(
                    int(operator_id),
                    initiator_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=initiator_kb,
                )
                STATE.pending_ff_ready_buttons[(int(operator_id), int(real_order_id))] = (
                    sent.chat.id,
                    sent.message_id,
                )
                await _remember_order_ui_message(int(real_order_id), sent.chat.id, sent.message_id)

        # Админы получают отдельную карточку ошибки с кнопкой «Завершить».
        admin_ids = await _admin_ids()
        sent_once: Set[int] = set()

        for aid in admin_ids:
            try:
                aid_int = int(aid)
            except Exception:
                continue

            if aid_int in sent_once or aid_int == int(operator_id):
                continue

            sent_once.add(aid_int)

            with suppress(Exception):
                sent = await callback.bot.send_message(
                    aid_int,
                    admin_error_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=admin_error_kb,
                )
                STATE.pending_ff_ready_buttons[(aid_int, int(real_order_id))] = (sent.chat.id, sent.message_id)
                await _remember_order_ui_message(int(real_order_id), sent.chat.id, sent.message_id)

    is_guest_user = real_user_id <= 0
    wallet = str(p2p.get("wallet") or "").strip()
    asset = _extract_asset_from_comment(str(p2p.get("comment") or ""))

    if not wallet and not is_guest_user and asset == "BTC":
        with suppress(Exception):
            user = await get_user(real_user_id)
            if user:
                wallet = str(user.get("btc_wallet") or "").strip()

    # ВАЖНО: кнопки скрываем сразу, чтобы исключить двойной клик.
    # При ошибке обмена это же сообщение будет отредактировано и вернёт нужные кнопки.
    with suppress(Exception):
        await callback.message.edit_reply_markup(reply_markup=None)

    if not wallet:
        await _broadcast_exchange_error("wallet not found")
        return

    try:
        started = await start_exchange_from_p2p(
            bot=callback.bot,
            p2p=p2p,
            operator_id=operator_id,
        )

        if not started:
            action_status = ""
            error_text = ""
            with suppress(Exception):
                db = await get_db()
                cur = await db.execute(
                    """
                    SELECT status, error
                      FROM p2p_order_actions
                     WHERE order_id = ? AND action = ?
                     LIMIT 1
                    """,
                    (int(real_order_id), "exchange_start"),
                )
                row = await cur.fetchone()
                await cur.close()
                if row:
                    action_status = str(row[0] or "").strip().lower()
                    error_text = str(row[1] or "").strip()

            # Если второй участник нажал «Готово» после первого, это не ошибка обмена.
            # Просто убираем кнопки у второго уведомления и не показываем пользователю/админам ложную ошибку.
            if action_status in {"claimed", "sent"} and not error_text:
                with suppress(Exception):
                    await callback.message.edit_reply_markup(reply_markup=None)
                with suppress(Exception):
                    await callback.bot.send_message(
                        operator_id,
                        f"ℹ️ Обмен по заявке №<b>{real_order_id}</b> уже запущен другим участником.",
                        parse_mode="HTML",
                    )
                return

            await _broadcast_exchange_error(error_text or "Не удалось запустить обмен. Причина не была сохранена.")
            return

        await _remember_order_ui_message(int(real_order_id), callback.message.chat.id, callback.message.message_id)
        await _finalize_exchange_ui(
            bot=callback.bot,
            order_id=int(real_order_id),
            operator_id=int(operator_id),
        )

        with suppress(Exception):
            db = await get_db()
            cur = await db.execute(
                """
                SELECT status
                  FROM p2p_order_actions
                 WHERE order_id = ? AND action = ?
                 LIMIT 1
                """,
                (int(real_order_id), "exchange_start"),
            )
            row = await cur.fetchone()
            await cur.close()

            if row and str(row[0] or "").strip().lower() == "sent":
                from db.p2p import set_exchange_started_at
                await set_exchange_started_at(int(real_order_id))

    except Exception as e:
        logger.exception("handle_ff_ready failed for order_id=%s", real_order_id)
        await _broadcast_exchange_error(str(e))



async def handle_finish_order(callback: types.CallbackQuery) -> None:
    await callback.answer()

    operator_id = int(callback.from_user.id)
    operator = await get_user(operator_id)
    if not operator or operator.get("role") not in ("Operator", "Admin"):
        with suppress(Exception):
            await callback.bot.send_message(operator_id, "⚠️ Завершение заявки доступно только администратору/оператору.")
        return

    parts = (callback.data or "").split(":")
    try:
        order_id = int(parts[1])
        user_id = int(parts[2])
    except Exception:
        with suppress(Exception):
            await callback.bot.send_message(operator_id, "⚠️ Неверные данные кнопки finish_order.")
        return

    p2p: Optional[Dict[str, Any]] = None
    with suppress(Exception):
        from db.p2p import get_order_by_id
        p2p = await get_order_by_id(order_id)

    if not p2p:
        with suppress(Exception):
            await callback.bot.send_message(operator_id, "⚠️ Заявка не найдена.")
        return

    prompt = await callback.bot.send_message(
        operator_id,
        f"🔗 Отправьте ссылку транзакции для заявки №<b>{order_id}</b>\n\n"
        f"Если передумали — отправьте <b>отменить</b>.",
        parse_mode="HTML",
    )

    STATE.pending_manual_finish[operator_id] = {
        "order_id": int(order_id),
        "user_id": int(user_id),
        "prompt_id": int(prompt.message_id),
    }


async def handle_finish_order_link(message: types.Message) -> None:
    operator_id = int(message.from_user.id)
    if operator_id not in STATE.pending_manual_finish:
        return

    raw_link = (message.text or "").strip()
    if not raw_link:
        await message.answer("⚠️ Ссылка не может быть пустой. Отправьте ссылку транзакции.")
        return

    info = STATE.pending_manual_finish.pop(operator_id, {})
    order_id = int(info.get("order_id") or 0)
    user_id = int(info.get("user_id") or 0)
    prompt_id = info.get("prompt_id")

    with suppress(Exception):
        await message.bot.delete_message(chat_id=operator_id, message_id=message.message_id)

    if prompt_id:
        with suppress(Exception):
            await message.bot.delete_message(chat_id=operator_id, message_id=int(prompt_id))

    if raw_link.lower() == "отменить":
        note = await message.bot.send_message(operator_id, "Операция ручного завершения отменена.")
        asyncio.create_task(_auto_delete(message.bot, operator_id, note.message_id, delay=6))
        return

    p2p: Optional[Dict[str, Any]] = None
    with suppress(Exception):
        from db.p2p import get_order_by_id
        p2p = await get_order_by_id(order_id)

    if not p2p:
        note = await message.bot.send_message(operator_id, "⚠️ Заявка не найдена.")
        asyncio.create_task(_auto_delete(message.bot, operator_id, note.message_id, delay=6))
        return

    real_user_id = int(p2p.get("user_id") or user_id or 0)
    db_order_id = int(p2p.get("order_id") or order_id)
    is_guest_user = real_user_id <= 0

    asset = _extract_asset_from_comment(str((p2p or {}).get("comment") or ""))
    wallet = str((p2p or {}).get("wallet") or "")

    try:
        amount = float((p2p or {}).get("btc_amount") or 0)
    except Exception:
        amount = 0.0

    operator_username = ""
    with suppress(Exception):
        op = await get_user(int(operator_id))
        if op:
            operator_username = op.get("username") or str(operator_id)
    operator_username = operator_username or str(operator_id)

    existing_bank_card = (p2p or {}).get("bank_card") or ""
    existing_bank_name = (p2p or {}).get("bank_name") or ""

    fb_card = ""
    fb_bank = ""
    if not existing_bank_card or not existing_bank_name:
        with suppress(Exception):
            fb_card = (await get_user_card(int(operator_id))) or ""
        with suppress(Exception):
            fb_bank = (await get_user_bank(int(operator_id))) or ""

        if not fb_card and not is_guest_user:
            with suppress(Exception):
                fb_card = (await get_user_card(real_user_id)) or ""
        if not fb_bank and not is_guest_user:
            with suppress(Exception):
                fb_bank = (await get_user_bank(real_user_id)) or ""

    with suppress(Exception):
        from db.p2p import set_tx_ready_at
        await set_tx_ready_at(int(db_order_id))

    did_finalize = False
    try:
        from db.p2p import try_finalize_p2p_order
        did_finalize = await try_finalize_p2p_order(
            int(db_order_id),
            tx_to=raw_link,
            user_link=str((p2p or {}).get("user_link") or "").strip() or None,
            operator_username=operator_username,
            bank_card_fallback=fb_card,
            bank_name_fallback=fb_bank,
        )
    except Exception:
        logger.exception("Failed to finalize manual order (order_id=%s)", db_order_id)

    if not did_finalize:
        note = await message.bot.send_message(
            operator_id,
            f"⚠️ Заявка №<b>{db_order_id}</b> уже была завершена ранее.",
            parse_mode="HTML",
        )
        asyncio.create_task(_auto_delete(message.bot, operator_id, note.message_id, delay=6))
        await _finalize_exchange_ui(
            bot=message.bot,
            order_id=int(db_order_id),
            operator_id=int(operator_id),
        )
        return

    # Важно для связки бота и Mastercard-кабинета:
    # ручное завершение через кнопку «Завершить» тоже должно засчитывать
    # успешное использование VidraPay-карты. Иначе баланс в кабинете меняется
    # по completed-заявке, а история/повторный «проверенный банк» и часть
    # ограничений VidraPay могут не видеть факт успешной сделки.
    with suppress(Exception):
        refreshed_for_usage = None
        from db.p2p import get_order_by_id
        refreshed_for_usage = await get_order_by_id(int(db_order_id))
        await _record_successful_vidrapay_card_usage(refreshed_for_usage or p2p)

    with suppress(Exception):
        from db.p2p import mark_p2p_action_sent
        await mark_p2p_action_sent(int(db_order_id), "exchange_start")

    with suppress(Exception):
        from db.p2p import set_exchange_started_at, set_ff_funds_sent_at
        await set_exchange_started_at(int(db_order_id))
        await set_ff_funds_sent_at(int(db_order_id))

    await _update_user_status_card(
        bot=message.bot,
        order_id=int(db_order_id),
        asset=asset,
        amount=float(amount),
        wallet=wallet,
        step1_done=True,
        step2_done=True,
        step3_done=True,
        extra_line="",
        user_id=real_user_id,
    )

    if not is_guest_user:
        try:
            me = await message.bot.get_me()
            bot_username = getattr(me, "username", "") or ""

            total_rub_raw = (p2p or {}).get("total_rub")
            if total_rub_raw in (None, "", 0):
                with suppress(Exception):
                    from db.p2p import get_order_by_id
                    again = await get_order_by_id(db_order_id)
                    total_rub_raw = (again or {}).get("total_rub")

            try:
                total_rub_val = float(total_rub_raw) if total_rub_raw is not None else 0.0
            except Exception:
                total_rub_val = 0.0

            potential_bonus = round(total_rub_val * 0.02, 2)
            potential_bonus_str = f"{potential_bonus:.2f}".rstrip("0").rstrip(".")
            referral_link = (
                f"https://t.me/{bot_username}?start={real_user_id}"
                if bot_username
                else f"/start {real_user_id}"
            )

            final_text = (
                "🎉 <b>Перевод отправлен!</b>\n\n"
                "⏳ Скорость поступления зависит только от работы сети — обычно это занимает немного времени.\n"
                "Вы можете отслеживать поступление по этой ссылке:\n\n"
                f"{raw_link}\n\n"
                "💡 <b>А знаете, что ещё приятно?</b>\n"
                f"Если бы подобную сделку совершил ваш друг, зарегистрированный по вашей ссылке, "
                f"вы бы получили <b>{html_escape(potential_bonus_str)} ₽</b> бонусом!\n\n"
                "🔗 <b>Ваша реферальная ссылка:</b>\n"
                f"{referral_link}\n"
                "Отправьте её другу и зарабатывайте <b>ВСЕГДА</b> на его обменах!\n\n"
                "➡️ Чтобы начать <b>новый обмен</b>, нажмите или введите команду <b>/start</b>"
            )

            await message.bot.send_message(
                real_user_id,
                final_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Failed to send final referral message for manual finish (order_id=%s)", db_order_id)

    try:
        can_notify_admin = True
        try:
            from db.p2p import try_claim_p2p_action
            can_notify_admin = await try_claim_p2p_action(int(db_order_id), "admin_completed_notify")
        except Exception:
            can_notify_admin = True

        if can_notify_admin:
            current = p2p
            with suppress(Exception):
                from db.p2p import get_order_by_id
                refreshed = await get_order_by_id(int(db_order_id))
                if refreshed:
                    current = refreshed

            mention_user = await _build_mention(message.bot, real_user_id)

            operator_username_for_msg = ""
            try:
                op_chat = await message.bot.get_chat(int(operator_id))
                operator_username_for_msg = (
                    getattr(op_chat, "username", "")
                    or getattr(op_chat, "full_name", "")
                    or ""
                )
            except Exception:
                operator_username_for_msg = ""

            pay_method_raw = str((current or {}).get("payment_method") or "").strip().lower()
            if pay_method_raw == "paycore":
                pay_method = "PayCore"
            elif pay_method_raw == "akkula":
                pay_method = "Akkula"
            elif pay_method_raw == "p2p":
                pay_method = "P2P"
            else:
                pay_method = pay_method_raw or "—"

            bank_name = str((current or {}).get("bank_name") or "—")
            requisite = (current or {}).get("bank_card") or "—"
            requisite_html = format_requisite_for_user(requisite)

            wallet_value = str((current or {}).get("wallet") or wallet or "")
            short_wallet = _short_wallet(wallet_value)

            asset_code = _extract_asset_from_comment(str((current or {}).get("comment") or ""))
            try:
                amount_value = float((current or {}).get("btc_amount") or amount or 0)
            except Exception:
                amount_value = float(amount or 0)

            amount_str = _format_amount_for_asset(asset_code, amount_value)

            total_rub_raw_now = (current or {}).get("total_rub")
            if total_rub_raw_now in (None, "", 0):
                with suppress(Exception):
                    from db.p2p import get_order_by_id
                    again = await get_order_by_id(db_order_id)
                    total_rub_raw_now = (again or {}).get("total_rub")

            try:
                rub_sum = str(int(float(total_rub_raw_now))) if total_rub_raw_now is not None else "—"
            except Exception:
                rub_sum = str(total_rub_raw_now or "—")

            tx_hash = raw_link
            for sep in ("/tx/", "txid=", "hash="):
                if sep in raw_link:
                    try:
                        tx_hash = raw_link.split(sep, 1)[1].split("&", 1)[0].split("?", 1)[0].strip("/")
                        break
                    except Exception:
                        tx_hash = raw_link

            msg = (
                "✅ <b>Сделка завершена</b>\n\n"
                f"🆔 Заявка: <b>#{db_order_id}</b>\n"
                f"👤 Пользователь: {mention_user}\n"
                f"👤 ID: <b>{real_user_id}</b>\n"
                f"🧑‍💼 Админ/Оператор: <b>{html_escape(operator_username_for_msg or '—')}</b>\n"
                f"💳 Метод: <b>{html_escape(pay_method)}</b>\n\n"
                f"🪙 Монета: <b>{html_escape(asset_code)}</b>\n"
                f"📦 К выдаче: <b>{html_escape(amount_str)} {html_escape(asset_code)}</b>\n"
                f"🏷 Адрес: <code>{html_escape(short_wallet)}</code>\n"
                f"💸 Сумма: <b>{html_escape(rub_sum)} ₽</b>\n\n"
                f"🏦 Банк: <b>{html_escape(bank_name)}</b>\n"
                f"💳 Реквизит: {requisite_html}\n\n"
                f"🔗 Tx: <a href=\"{html_escape(raw_link)}\">открыть</a>\n"
                f"🧾 TxID: <code>{html_escape(tx_hash)}</code>"
            )

            await _notify_admin(
                message.bot,
                msg,
            )

            mastercard_owner_id = await _get_mastercard_owner_id_for_order(current)
            if mastercard_owner_id:
                with suppress(Exception):
                    await message.bot.send_message(
                        int(mastercard_owner_id),
                        msg,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )

            with suppress(Exception):
                from db.p2p import mark_p2p_action_sent
                await mark_p2p_action_sent(int(db_order_id), "admin_completed_notify")

    except Exception:
        logger.exception("Failed to notify admins about completed deal (order_id=%s)", db_order_id)
        with suppress(Exception):
            from db.p2p import mark_p2p_action_failed
            await mark_p2p_action_failed(int(db_order_id), "admin_completed_notify", error="notify failed")

    try:
        total_rub_raw = (p2p or {}).get("total_rub")
        if total_rub_raw in (None, "", 0):
            with suppress(Exception):
                from db.p2p import get_order_by_id
                again = await get_order_by_id(db_order_id)
                total_rub_raw = (again or {}).get("total_rub")

        try:
            total_rub_val = float(total_rub_raw) if total_rub_raw is not None else 0.0
        except Exception:
            total_rub_val = 0.0

        ref_info = await try_add_referral_commission(
            order_id=db_order_id,
            user_id=real_user_id,
            total_rub=total_rub_val,
        )

        if ref_info:
            referrer_id = int(ref_info.get("referrer_id") or 0)
            bonus_amount = float(ref_info.get("amount") or 0.0)

            if referrer_id and bonus_amount > 0:
                text = (
                    "💸 <b>Реферальное начисление</b>\n"
                    f"➕ <b>+{html_escape(f'{bonus_amount:.2f}')} ₽</b>"
                )

                with suppress(Exception):
                    await message.bot.send_message(
                        referrer_id,
                        text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
    except Exception:
        logger.exception("Ошибка при начислении/уведомлении реферальной комиссии по заявке %s", db_order_id)

    await _finalize_exchange_ui(
        bot=message.bot,
        order_id=int(db_order_id),
        operator_id=int(operator_id),
    )


async def track_ff_order_status(
    bot: Bot,
    user_id: int,
    ff_order_id: str,
    token: str,
    db_order_id: int,
    asset: Optional[str] = None,
    exchange_started_by_id: Optional[int] = None,
) -> None:
    from db.p2p import get_order_by_id

    prev_status: Optional[str] = None
    sent_link = False

    error_attempts = 0
    max_error_attempts = 3

    SHOW_LINK_ASSETS = {"BTC", "LTC", "USDT"}

    def _build_tx_link(asset_code: Optional[str], txid: Optional[str]) -> Optional[str]:
        """
        Возвращает ссылку на обозреватель по txid.
        BTC -> mempool.space, LTC -> Blockchair, USDT(TRC20) -> Tronscan.
        """
        a = (asset_code or "BTC").upper().strip()
        tx = (txid or "").strip()
        if not tx:
            return None

        if a == "BTC":
            return BTC_MEMPOOL.format(tx=tx)

        if a == "LTC":
            return f"https://blockchair.com/litecoin/transaction/{tx}"

        if a in ("USDT", "USDTTRC20", "USDTTRC"):
            return f"https://tronscan.org/#/transaction/{tx}"

        return None

    def _extract_tx_hash_from_link_any(link_val: Optional[str]) -> str:
        """
        Достаёт txid из ссылки разных обозревателей:
        - mempool: /tx/<id>
        - blockchair: /transaction/<id>
        - tronscan: #/transaction/<id>
        """
        if not link_val:
            return "—"
        s = str(link_val).strip()

        if "/tx/" in s:
            h = s.split("/tx/", 1)[1].strip().strip("/")
            if "?" in h:
                h = h.split("?", 1)[0]
            return h or "—"

        if "/transaction/" in s:
            h = s.split("/transaction/", 1)[1].strip().strip("/")
            if "?" in h:
                h = h.split("?", 1)[0]
            return h or "—"

        return "—"

    async def _complete_exchange(link: Optional[str]) -> None:
        current: Optional[Dict[str, Any]] = None
        with suppress(Exception):
            current = await get_order_by_id(db_order_id)

        if not current:
            with suppress(Exception):
                await _update_user_status_card(
                    bot=bot,
                    order_id=db_order_id,
                    asset=(asset or "BTC"),
                    amount=0.0,
                    wallet="",
                    step1_done=True,
                    step2_done=True,
                    step3_done=True,
                    extra_line="",
                )
            await _finalize_exchange_ui(bot=bot, order_id=db_order_id, operator_id=None)
            return

        operator_id: Optional[int] = None
        with suppress(Exception):
            operator_id = current.get("operator_id")

        try:
            amount = float(current.get("btc_amount") or 0)
        except Exception:
            amount = 0.0

        wallet = str(current.get("wallet") or "")
        is_guest_user = int(user_id) <= 0
        is_web_order = _is_web_order(current)

        user_rec = None
        if not is_guest_user:
            with suppress(Exception):
                user_rec = await get_user(user_id)

        if is_guest_user:
            display = f"WEB-гость / user_id {user_id}"
            user_link = display
        else:
            display = (user_rec or {}).get("username") or (user_rec or {}).get("full_name") or str(user_id)
            user_link = f'<a href="tg://user?id={user_id}">{html_escape(display)}</a>'

        operator_username = ""
        if operator_id:
            with suppress(Exception):
                op = await get_user(int(operator_id))
                if op:
                    operator_username = op.get("username") or str(operator_id)
            operator_username = operator_username or str(operator_id)
        else:
            with suppress(Exception):
                me2 = await bot.get_me()
                operator_username = getattr(me2, "username", "") or ""

        existing_bank_card = (current or {}).get("bank_card") or ""
        existing_bank_name = (current or {}).get("bank_name") or ""

        fb_card = ""
        fb_bank = ""
        if not existing_bank_card or not existing_bank_name:
            op_card = ""
            op_bank = ""
            user_card = ""
            user_bank = ""

            if operator_id:
                with suppress(Exception):
                    op_card = (await get_user_card(int(operator_id))) or ""
                with suppress(Exception):
                    op_bank = (await get_user_bank(int(operator_id))) or ""

            if not is_guest_user:
                with suppress(Exception):
                    user_card = (await get_user_card(user_id)) or ""
                with suppress(Exception):
                    user_bank = (await get_user_bank(user_id)) or ""

            fb_card = (op_card or user_card or "") or ""
            fb_bank = (op_bank or user_bank or "") or ""

        if link:
            with suppress(Exception):
                from db.p2p import set_tx_ready_at
                await set_tx_ready_at(int(db_order_id))

        from db.p2p import try_finalize_p2p_order

        did_finalize = False
        with suppress(Exception):
            did_finalize = await try_finalize_p2p_order(
                db_order_id,
                tx_to=link,
                user_link=user_link,
                operator_username=operator_username,
                bank_card_fallback=fb_card,
                bank_name_fallback=fb_bank,
            )

        if not did_finalize:
            await _finalize_exchange_ui(bot=bot, order_id=db_order_id, operator_id=operator_id)
            return

        # Лимиты/паузы VidraPay-карт засчитываем только после успешной финализации сделки.
        await _record_successful_vidrapay_card_usage(current)

        # Финализирующую карточку в Telegram должен получать любой не-гость,
        # включая WEB-пользователя с Telegram ID.
        if not is_guest_user:
            await _update_user_status_card(
                bot=bot,
                order_id=db_order_id,
                asset=(asset or "BTC"),
                amount=amount,
                wallet=wallet,
                step1_done=True,
                step2_done=True,
                step3_done=True,
                extra_line="",
            )

        asset_code = (asset or _extract_asset_from_comment(str((current or {}).get("comment") or "")) or "BTC").upper()
        amount_str = _format_amount_for_asset(asset_code, amount)
        short_wallet = _short_wallet(wallet)

        tx_link_line = "🔗 Tx: <b>не удалось получить ссылку</b>"
        if link:
            tx_hash = _extract_tx_hash_from_link_any(link)
            tx_link_line = (
                f"🔗 Tx: <a href=\"{html_escape(link)}\">открыть</a>\n"
                f"🧾 TxID: <code>{html_escape(tx_hash)}</code>"
            )

        # Финальное реферальное сообщение в Telegram тоже должно идти любому не-гостю,
        # включая WEB-пользователя с Telegram ID.
        if not is_guest_user:
            try:
                me = await bot.get_me()
                bot_username = getattr(me, "username", "") or ""

                total_rub_raw = (current or {}).get("total_rub")
                if total_rub_raw in (None, "", 0):
                    again = await get_order_by_id(db_order_id)
                    total_rub_raw = (again or {}).get("total_rub")

                try:
                    total_rub_val = float(total_rub_raw) if total_rub_raw is not None else 0.0
                except Exception:
                    total_rub_val = 0.0

                potential_bonus = round(total_rub_val * 0.02, 2)
                potential_bonus_str = f"{potential_bonus:.2f}".rstrip("0").rstrip(".")
                referral_link = (
                    f"https://t.me/{bot_username}?start={user_id}"
                    if bot_username
                    else f"/start {user_id}"
                )

                base_msg = (
                    "🎉 <b>Перевод отправлен!</b>\n\n"
                    "⏳ Скорость поступления зависит только от работы сети — обычно это занимает немного времени.\n"
                )

                if asset_code in SHOW_LINK_ASSETS and link:
                    base_msg += f"Вы можете отслеживать поступление по этой ссылке:\n\n{link}\n\n"
                else:
                    base_msg += "\n"

                final_msg_text = (
                    f"{base_msg}"
                    "💡 <b>А знаете, что ещё приятно?</b>\n"
                    f"Если бы подобную сделку совершил ваш друг, зарегистрированный по вашей ссылке, "
                    f"вы бы получили <b>{html_escape(potential_bonus_str)} ₽</b> бонусом!\n\n"
                    "🔗 <b>Ваша реферальная ссылка:</b>\n"
                    f"{referral_link}\n"
                    "Отправьте её другу и зарабатывайте <b>ВСЕГДА</b> на его обменах!\n\n"
                    "➡️ Чтобы начать <b>новый обмен</b>, нажмите или введите команду <b>/start</b>"
                )

                await bot.send_message(
                    user_id,
                    final_msg_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Ошибка при отправке финального сообщения пользователю по заявке %s", db_order_id)

        try:
            can_notify_admin = True
            try:
                from db.p2p import try_claim_p2p_action
                can_notify_admin = await try_claim_p2p_action(int(db_order_id), "admin_completed_notify")
            except Exception:
                can_notify_admin = True

            if can_notify_admin:
                mention_user = await _build_mention(bot, user_id)

                pay_method = str((current or {}).get("payment_method") or "—")
                bank_name = str((current or {}).get("bank_name") or fb_bank or "—")
                requisite = str((current or {}).get("bank_card") or fb_card or "—")
                rub_raw = (current or {}).get("total_rub")
                try:
                    rub_sum = f"{int(float(rub_raw))} ₽" if rub_raw is not None else "—"
                except Exception:
                    rub_sum = html_escape(str(rub_raw or "—"))

                requisite_html = format_requisite_for_user(requisite)

                msg = (
                    "✅ <b>Сделка завершена</b>\n\n"
                    f"🆔 Заявка: <b>#{db_order_id}</b>\n"
                    f"👤 Пользователь: {mention_user}\n"
                    f"👤 ID: <b>{user_id}</b>\n"
                    f"🧑‍💼 Админ/Оператор: <b>{html_escape(operator_username or '—')}</b>\n"
                    f"💳 Метод: <b>{html_escape(pay_method)}</b>\n\n"
                    f"🪙 Монета: <b>{html_escape(asset_code)}</b>\n"
                    f"📦 К выдаче: <b>{html_escape(amount_str)} {html_escape(asset_code)}</b>\n"
                    f"🏷 Адрес: <code>{html_escape(short_wallet)}</code>\n"
                    f"💸 Сумма: <b>{html_escape(rub_sum)}</b>\n\n"
                    f"🏦 Банк: <b>{html_escape(bank_name)}</b>\n"
                    f"💳 Реквизит: {requisite_html}\n\n"
                    f"{tx_link_line}"
                )

                completed_notify_recipient_ids: List[int] = []

                # operator_id в заявке не всегда равен Mastercard, который нажал
                # «Готово — начать обмен». Поэтому финальное уведомление отправляем:
                # 1) оператору из заявки, если он есть;
                # 2) тому, кто фактически запустил автообмен кнопкой ff_ready;
                # 3) владельцу Mastercard-реквизита по card_id заявки.
                for raw_recipient_id in (operator_id, exchange_started_by_id):
                    with suppress(Exception):
                        recipient_id = int(raw_recipient_id)
                        if recipient_id > 0 and recipient_id not in completed_notify_recipient_ids:
                            completed_notify_recipient_ids.append(recipient_id)

                mastercard_owner_id: Optional[int] = None
                with suppress(Exception):
                    mastercard_owner_id = await _get_mastercard_owner_id_for_order(current)

                if mastercard_owner_id:
                    with suppress(Exception):
                        mc_id = int(mastercard_owner_id)
                        if mc_id > 0 and mc_id not in completed_notify_recipient_ids:
                            completed_notify_recipient_ids.append(mc_id)

                for recipient_id in completed_notify_recipient_ids:
                    with suppress(Exception):
                        await bot.send_message(
                            int(recipient_id),
                            msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )

                await _notify_admin(
                    bot,
                    msg,
                    exclude_ids=completed_notify_recipient_ids,
                )

                with suppress(Exception):
                    from db.p2p import mark_p2p_action_sent
                    await mark_p2p_action_sent(int(db_order_id), "admin_completed_notify")

        except Exception:
            logger.exception("Failed to notify admins about completed deal (order_id=%s)", db_order_id)
            with suppress(Exception):
                from db.p2p import mark_p2p_action_failed
                await mark_p2p_action_failed(int(db_order_id), "admin_completed_notify", error="notify failed")

        try:
            total_rub_raw = (current or {}).get("total_rub")
            if total_rub_raw in (None, "", 0):
                again = await get_order_by_id(db_order_id)
                total_rub_raw = (again or {}).get("total_rub")

            try:
                total_rub_val = float(total_rub_raw) if total_rub_raw is not None else 0.0
            except Exception:
                total_rub_val = 0.0

            ref_info = await try_add_referral_commission(order_id=db_order_id, user_id=user_id, total_rub=total_rub_val)

            if ref_info:
                referrer_id = int(ref_info.get("referrer_id") or 0)
                bonus_amount = float(ref_info.get("amount") or 0.0)

                if referrer_id and bonus_amount > 0:
                    text = (
                        "💸 <b>Реферальное начисление</b>\n"
                        f"➕ <b>+{html_escape(f'{bonus_amount:.2f}')} ₽</b>"
                    )

                    with suppress(Exception):
                        await bot.send_message(
                            referrer_id,
                            text,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )

        except Exception:
            logger.exception("Ошибка при начислении/уведомлении реферальной комиссии по заявке %s", db_order_id)

        await _finalize_exchange_ui(bot=bot, order_id=db_order_id, operator_id=operator_id)

    while not sent_link:
        try:
            resp = await get_order_details(ff_order_id, token)
            data = resp.get("data", resp) if isinstance(resp, dict) else resp
            status = data.get("status")
            tx_id = data.get("to", {}).get("tx", {}).get("id")
            error_attempts = 0
        except FFAPIError:
            logger.exception("Ошибка FFAPI при трекинге (id=%s, order_id=%s)", ff_order_id, db_order_id)
            error_attempts += 1
            if error_attempts >= max_error_attempts:
                await _complete_exchange(link=None)
                sent_link = True
                break
            await asyncio.sleep(10)
            continue
        except Exception:
            logger.exception("Ошибка трекинга (id=%s, order_id=%s)", ff_order_id, db_order_id)
            error_attempts += 1
            if error_attempts >= max_error_attempts:
                await _complete_exchange(link=None)
                sent_link = True
                break
            await asyncio.sleep(10)
            continue

        if status == "NEW" and prev_status != "NEW":
            prev_status = "NEW"

        if tx_id:
            link = _build_tx_link(asset, str(tx_id))
            await _complete_exchange(link=link)
            sent_link = True
            break

        if status in ("EXPIRED", "EMERGENCY"):
            with suppress(Exception):
                await _complete_exchange(link=None)
            sent_link = True
            break

        await asyncio.sleep(15)


async def handle_check_from_user(message: types.Message) -> None:
    user_id = int(message.from_user.id)
    if user_id not in STATE.pending_check_receipts:
        return
    if message.content_type != ContentType.DOCUMENT:
        return

    _track_receipt_msg(user_id, message.message_id)

    doc = message.document
    mime = (getattr(doc, "mime_type", "") or "").lower()
    name = (getattr(doc, "file_name", "") or "").lower()
    is_pdf = (mime == "application/pdf") or name.endswith(".pdf")
    if not is_pdf:
        sent = await message.answer(
            "❌ Неверный формат чека.\n"
            "Принимаем только <b>PDF</b>. Пожалуйста, отправьте файл PDF "
            "(скрепка → Файл → выберите PDF).",
            parse_mode="HTML",
        )
        _track_receipt_msg(user_id, sent.message_id)
        return

    operator_id = STATE.pending_check_receipts.pop(user_id)

    from db.p2p import get_order_by_id, get_p2p_order_id_by_user

    rec = await get_pending_order(user_id)
    if not rec:
        with suppress(Exception):
            last_id = await get_p2p_order_id_by_user(user_id)
            if last_id:
                rec = await get_order_by_id(last_id)

    db_order_id = (rec or {}).get("order_id", "—")
    bank_card = (rec or {}).get("bank_card", "—")
    bank_name = (rec or {}).get("bank_name", "—")
    total_raw = (rec or {}).get("total_rub")

    try:
        amount = int(float(total_raw)) if total_raw is not None else "—"
    except Exception:
        amount = total_raw or "—"

    mention = await _build_mention(message.bot, user_id)

    caption = (
        "━━━━━━━━━━━━━━━━━━\n"
        "🧾 <b>ЧЕК</b>\n"
        f"Заявка: <b>#{db_order_id}</b>\n\n"
        "Данные для сверки:\n"
        f"• Номер: <code>{html_escape(bank_card)}</code>\n"
        f"• Банк:  {html_escape(bank_name)}\n"
        f"• Сумма: <b>{html_escape(amount)} ₽</b>"
    )

    ikb = InlineKeyboardMarkup()
    ikb.row(
        InlineKeyboardButton("❌ Отклонить чек", callback_data=f"op_reject_receipt:{user_id}:{db_order_id}"),
        InlineKeyboardButton("📥 Открыть заявку", callback_data=f"operator_open_order:{user_id}:{db_order_id}"),
    )
    ikb.add(InlineKeyboardButton("✅ Готово — начать обмен", callback_data=f"ff_ready:{db_order_id}:{user_id}"))
    ikb.add(InlineKeyboardButton("✅ Завершить", callback_data=f"finish_order:{db_order_id}:{user_id}"))

    try:
        sent_op = await message.bot.send_document(
            chat_id=operator_id,
            document=doc.file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=ikb,
        )
        with suppress(Exception):
            STATE.pending_ff_ready_buttons[(int(operator_id), int(db_order_id))] = (sent_op.chat.id, sent_op.message_id)
    except Exception:
        sent_fail = await message.answer("⚠️ Не удалось доставить чек оператору. Попробуйте ещё раз позже.")
        _track_receipt_msg(user_id, sent_fail.message_id)
        return

    sent_ok = await message.answer("🧾 Чек отправлен оператору и находится на проверке.", parse_mode="HTML")
    _track_receipt_msg(user_id, sent_ok.message_id)


async def handle_op_view_receipt(callback: types.CallbackQuery) -> None:
    await callback.answer()

    parts = (callback.data or "").split(":")
    try:
        order_id = int(parts[1])
        user_id = int(parts[2])
    except Exception:
        await callback.bot.send_message(callback.from_user.id, "⚠️ Неверные данные кнопки «Чек».")
        return

    operator_id = int(callback.from_user.id)
    with suppress(Exception):
        await callback.message.delete()

    STATE.pending_check_receipts[user_id] = operator_id

    mention = await _build_mention(callback.bot, user_id)
    await callback.bot.send_message(
        operator_id,
        f"🧾 Запрос чека отправлен пользователю {mention} по заявке №<b>{order_id}</b>.",
        parse_mode="HTML",
    )

    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("📎 Прикрепить чек (PDF)", callback_data=f"op_attach_receipt:{operator_id}")
    )

    user_text = (
        f"🧾 По вашей заявке №<b>{order_id}</b> оператор запросил чек об оплате.\n\n"
        "Пожалуйста, отправьте чек <b>строго в формате PDF</b>.\n"
        "Нажмите кнопку ниже, затем прикрепите файл."
    )
    try:
        sent = await callback.bot.send_message(user_id, user_text, parse_mode="HTML", reply_markup=kb)
        _track_receipt_msg(user_id, sent.message_id)
    except Exception:
        with suppress(Exception):
            await callback.bot.send_message(operator_id, "⚠️ Пользователь недоступен: запрос чека не доставлен.")


async def handle_op_attach_receipt(callback: types.CallbackQuery) -> None:
    await callback.answer()

    parts = (callback.data or "").split(":")
    try:
        operator_id = int(parts[1])
    except Exception:
        operator_id = None

    user_id = int(callback.from_user.id)
    if operator_id:
        STATE.pending_check_receipts[user_id] = operator_id

    with suppress(Exception):
        await callback.message.delete()

    sent = await callback.bot.send_message(
        user_id,
        "📎 Отправьте чек <b>ОДНИМ</b> файлом <b>PDF</b> (кнопка «скрепка» → «Файл»).\n"
        "Иные форматы (фото, скрин, архив, docx) <b>не принимаются</b>.",
        parse_mode="HTML",
    )
    _track_receipt_msg(user_id, sent.message_id)


async def handle_wrong_receipt_format(message: types.Message) -> None:
    user_id = int(message.from_user.id)
    if user_id not in STATE.pending_check_receipts:
        return

    if message.content_type == ContentType.DOCUMENT:
        doc = message.document
        mime = (getattr(doc, "mime_type", "") or "").lower()
        name = (getattr(doc, "file_name", "") or "").lower()
        if (mime == "application/pdf") or name.endswith(".pdf"):
            return

    sent = await message.answer(
        "❌ Неверный формат чека.\n"
        "Примем только <b>PDF</b>. Пожалуйста, отправьте файл PDF "
        "(кнопка «скрепка» → «Файл» → выберите PDF).",
        parse_mode="HTML",
    )
    _track_receipt_msg(user_id, sent.message_id)


async def handle_op_reject_receipt(callback: types.CallbackQuery) -> None:
    await callback.answer()
    parts = (callback.data or "").split(":")
    try:
        user_id = int(parts[1])
        order_id = int(parts[2])
    except Exception:
        await callback.bot.send_message(callback.from_user.id, "⚠️ Неверные данные «Отклонить чек».")
        return

    operator_id = int(callback.from_user.id)
    with suppress(Exception):
        await callback.message.delete()

    prompt = await callback.bot.send_message(operator_id, "⚠️ Введите причину отказа чека:", parse_mode="HTML")
    STATE.pending_reject_reasons[operator_id] = {"user_id": user_id, "order_id": order_id, "prompt_id": prompt.message_id}


async def handle_op_reject_reason(message: types.Message) -> None:
    operator_id = int(message.from_user.id)
    if operator_id not in STATE.pending_reject_reasons:
        return

    reason = (message.text or "").strip()
    if not reason:
        await message.answer("⚠️ Причина не может быть пустой. Введите, пожалуйста, текст объяснения.")
        return

    info = STATE.pending_reject_reasons.pop(operator_id, {})
    user_id = int(info.get("user_id") or 0)
    order_id = info.get("order_id")
    prompt_id = info.get("prompt_id")

    with suppress(Exception):
        await message.bot.delete_message(chat_id=operator_id, message_id=message.message_id)
    if prompt_id:
        with suppress(Exception):
            await message.bot.delete_message(chat_id=operator_id, message_id=int(prompt_id))

    try:
        sent_user = await message.bot.send_message(
            user_id,
            (
                f"❌ <b>Чек не принят</b> по заявке №<b>{order_id}</b>.\n"
                f"Причина: <i>{html_escape(reason)}</i>\n\n"
                "Пожалуйста, загрузите <b>новый чек</b> строго в формате PDF "
                "(скрепка → Файл → выберите PDF)."
            ),
            parse_mode="HTML",
        )
        _track_receipt_msg(user_id, sent_user.message_id)
        STATE.pending_check_receipts[user_id] = operator_id
    except Exception:
        note = await message.answer("⚠️ Не удалось отправить уведомление пользователю.")
        asyncio.create_task(_auto_delete(message.bot, message.chat.id, note.message_id, delay=6))
        return

    note = await message.answer("Ожидаем новый PDF-чек...", parse_mode="HTML")
    asyncio.create_task(_auto_delete(message.bot, message.chat.id, note.message_id, delay=6))


def register_instruction_handlers(dp: Dispatcher) -> None:
    dp.register_callback_query_handler(
        instruction_callback_handler,
        lambda c: (c.data or "").startswith(Callback.OPERATOR_ACCEPT),
        state="*",
    )

    dp.register_message_handler(process_amount, state=InstructionStates.waiting_amount)
    dp.register_message_handler(process_card, state=InstructionStates.waiting_card)
    dp.register_message_handler(process_bank, state=InstructionStates.waiting_bank)
    dp.register_message_handler(process_comment, state=InstructionStates.waiting_comment)

    dp.register_callback_query_handler(
        handle_paid,
        lambda c: (c.data or "").startswith("paid"),
        state="*",
    )
    dp.register_callback_query_handler(
        handle_cancel,
        lambda c: (c.data or "").startswith("cancel_pay"),
        state="*",
    )
    dp.register_callback_query_handler(
        handle_op_view_receipt,
        lambda c: (c.data or "").startswith("op_view_receipt:"),
        state="*",
    )
    dp.register_callback_query_handler(
        handle_ff_ready,
        lambda c: (c.data or "").startswith("ff_ready:"),
        state="*",
    )
    dp.register_callback_query_handler(
        handle_finish_order,
        lambda c: (c.data or "").startswith("finish_order:"),
        state="*",
    )
    dp.register_callback_query_handler(
        handle_op_reject_receipt,
        lambda c: (c.data or "").startswith("op_reject_receipt:"),
        state="*",
    )

    dp.register_message_handler(
        handle_check_from_user,
        content_types=ContentType.DOCUMENT,
        state="*",
    )
    dp.register_message_handler(
        handle_op_reject_reason,
        lambda message: int(message.from_user.id) in STATE.pending_reject_reasons,
        content_types=types.ContentType.TEXT,
        state="*",
    )
    dp.register_message_handler(
        handle_finish_order_link,
        lambda message: int(message.from_user.id) in STATE.pending_manual_finish,
        content_types=types.ContentType.TEXT,
        state="*",
    )