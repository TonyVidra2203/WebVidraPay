"""
Модуль P2P-заявок: инициализация схемы, CRUD и выборки, работа со статусами
и чеками (PDF).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from db.connection import get_db

# -----------------------------------------------------------------------------
# Раздел: SQL-константы и DDL
# -----------------------------------------------------------------------------

CREATE_P2P_ORDERS_SQL: str = """
CREATE TABLE IF NOT EXISTS p2p_orders (
    order_id       INTEGER,
    user_id        INTEGER NOT NULL,
    operator_id    INTEGER,
    btc_amount     REAL,
    rub_amount     REAL,
    total_rub      REAL,
    wallet         TEXT,
    comment        TEXT,
    public_id      TEXT,
    ff_order_id    TEXT,
    status         TEXT    NOT NULL DEFAULT 'pending',
    tx_to          TEXT,
    tx_link_sent   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL,
    completed_at   TEXT,
    user_link      TEXT,
    operator_username TEXT,
    bank_card      TEXT,
    bank_name      TEXT,
    payment_method TEXT,
    card_id        INTEGER,
    payment_confirmed_at TEXT,
    exchange_started_at TEXT,
    ff_funds_sent_at TEXT,
    tx_ready_at TEXT
);
"""

CREATE_COMPLETED_ORDERS_SQL: str = """
CREATE TABLE IF NOT EXISTS completed_p2p_orders (
    order_code           TEXT PRIMARY KEY,
    user_link            TEXT,
    operator_username    TEXT,
    btc_amount           REAL,
    rub_amount           REAL,
    total_rub            REAL,
    wallet               TEXT,
    created_at           TEXT    NOT NULL,
    status               TEXT    NOT NULL,
    bank_card            TEXT,
    bank_name            TEXT,
    card_id              INTEGER
);
"""

CREATE_RECEIPTS_SQL: str = """
CREATE TABLE IF NOT EXISTS p2p_receipts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL,
    file_id     TEXT    NOT NULL,
    uploaded_at TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(order_id) REFERENCES p2p_orders(order_id)
);
"""

DROP_PENDING_UNIQUE_IDX_SQL: str = "DROP INDEX IF EXISTS ux_p2p_pending_user;"

CREATE_PENDING_UNIQUE_IDX_SQL: str = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_p2p_pending_user
ON p2p_orders(user_id)
WHERE status = 'pending'
  AND (payment_method IS NULL OR payment_method != 'akkula');
"""


DROP_TRIGGER_COMPLETED_SQL: str = "DROP TRIGGER IF EXISTS trg_insert_completed;"

CREATE_TRIGGER_COMPLETED_SQL: str = """
CREATE TRIGGER trg_insert_completed
AFTER UPDATE OF status ON p2p_orders
WHEN NEW.status = 'completed'
BEGIN
    INSERT INTO completed_p2p_orders (
        order_code,
        user_link,
        operator_username,
        btc_amount,
        rub_amount,
        total_rub,
        wallet,
        created_at,
        status,
        bank_card,
        bank_name,
        card_id
    ) VALUES (
        UPPER(SUBSTR(NEW.operator_username,1,2))
        || '-' ||
        printf(
            '%05d',
            (
                COALESCE(
                    (SELECT MAX(CAST(SUBSTR(order_code,4) AS INTEGER))
                     FROM completed_p2p_orders),
                    843
                ) + 1
            )
        ),
        NEW.user_link,
        NEW.operator_username,
        NEW.btc_amount,
        NEW.rub_amount,
        NEW.total_rub,
        NEW.wallet,
        NEW.created_at,
        NEW.status,
        NEW.bank_card,
        NEW.bank_name,
        NEW.card_id
    );
END;
"""
# -----------------------------------------------------------------------------
# Раздел: Инициализация и миграции
# -----------------------------------------------------------------------------

async def init_p2p_db() -> None:
    """Создаёт таблицы/индексы/триггеры и выполняет мягкие миграции схемы."""
    db = await get_db()

    # Базовая таблица заявок
    await db.execute(CREATE_P2P_ORDERS_SQL)

    # Миграции p2p_orders
    cur = await db.execute("PRAGMA table_info(p2p_orders)")
    rows = await cur.fetchall()
    await cur.close()
    existing_cols = {row[1] for row in rows}

    if "user_link" not in existing_cols:
        await db.execute("ALTER TABLE p2p_orders ADD COLUMN user_link TEXT")
    if "operator_username" not in existing_cols:
        await db.execute("ALTER TABLE p2p_orders ADD COLUMN operator_username TEXT")
    if "bank_card" not in existing_cols:
        await db.execute("ALTER TABLE p2p_orders ADD COLUMN bank_card TEXT")
    if "bank_name" not in existing_cols:
        await db.execute("ALTER TABLE p2p_orders ADD COLUMN bank_name TEXT")
    if "payment_method" not in existing_cols:
        await db.execute("ALTER TABLE p2p_orders ADD COLUMN payment_method TEXT")
    if "card_id" not in existing_cols:
        await db.execute("ALTER TABLE p2p_orders ADD COLUMN card_id INTEGER")
    if "payment_confirmed_at" not in existing_cols:
        await db.execute("ALTER TABLE p2p_orders ADD COLUMN payment_confirmed_at TEXT")
    if "exchange_started_at" not in existing_cols:
        await db.execute("ALTER TABLE p2p_orders ADD COLUMN exchange_started_at TEXT")
    if "ff_funds_sent_at" not in existing_cols:
        await db.execute("ALTER TABLE p2p_orders ADD COLUMN ff_funds_sent_at TEXT")
    if "tx_ready_at" not in existing_cols:
        await db.execute("ALTER TABLE p2p_orders ADD COLUMN tx_ready_at TEXT")

    # Таблица завершённых заявок
    await db.execute(CREATE_COMPLETED_ORDERS_SQL)

    # Миграции completed_p2p_orders
    cur2 = await db.execute("PRAGMA table_info(completed_p2p_orders)")
    rows2 = await cur2.fetchall()
    await cur2.close()
    existing_cols_completed = {row[1] for row in rows2}

    if "card_id" not in existing_cols_completed:
        await db.execute("ALTER TABLE completed_p2p_orders ADD COLUMN card_id INTEGER")

    # Таблица чеков
    await db.execute(CREATE_RECEIPTS_SQL)

    # ------------------------------------------------------------
    # ✅ ВАЖНО: уникальность pending (non-akkula)
    # ------------------------------------------------------------
    await db.execute("DROP INDEX IF EXISTS ux_p2p_pending_user")

    await db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_p2p_pending_user_non_akkula
        ON p2p_orders(user_id)
        WHERE status = 'pending' AND IFNULL(LOWER(payment_method),'') != 'akkula';
        """
    )

    # ------------------------------------------------------------
    # ✅ НОВОЕ: таблица идемпотентности действий по заявке
    # (строго 1 раз на (order_id, action))
    # ------------------------------------------------------------
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS p2p_order_actions (
            order_id    INTEGER NOT NULL,
            action      TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'claimed',   -- claimed | sent | failed
            message_id  INTEGER,
            error       TEXT,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            PRIMARY KEY(order_id, action)
        );
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS ix_p2p_order_actions_order ON p2p_order_actions(order_id)"
    )

    # ------------------------------------------------------------
    # ✅ НОВОЕ: операторские уведомления по заявке
    # Нужны, чтобы web-заявки обновлялись у всех админов так же,
    # как и обычные TG-заявки, даже если web и bot работают
    # в разных процессах.
    # ------------------------------------------------------------
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS p2p_operator_notifications (
            order_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            operator_id  INTEGER NOT NULL,
            chat_id      INTEGER NOT NULL,
            message_id   INTEGER NOT NULL,
            created_at   TEXT    NOT NULL,
            PRIMARY KEY(order_id, operator_id, message_id)
        );
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_p2p_operator_notifications_order
        ON p2p_operator_notifications(order_id)
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_p2p_operator_notifications_user
        ON p2p_operator_notifications(user_id)
        """
    )

    # Триггер completed
    await db.execute(DROP_TRIGGER_COMPLETED_SQL)
    await db.execute(CREATE_TRIGGER_COMPLETED_SQL)

    await db.commit()



# -----------------------------------------------------------------------------
# Раздел: Вспомогательные функции выборки
# -----------------------------------------------------------------------------

async def _fetchall_dict(
    query: str,
    params: Tuple[Any, ...] = (),
) -> List[Dict[str, Any]]:
    """Выполняет SELECT и возвращает список словарей."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cur = await db.execute(query, params)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(row) for row in rows]


async def _fetchone_dict(
    query: str,
    params: Tuple[Any, ...] = (),
) -> Optional[Dict[str, Any]]:
    """Выполняет SELECT и возвращает одну строку в виде словаря или None."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def expire_stale_web_orders(user_id: Optional[int] = None) -> int:
    """
    Помечает как expired старые WEB-заявки, которым больше 30 минут.

    Истекают только заявки:
    - со статусом pending,
    - с comment, начинающимся на WEB,
    - без подтверждения оплаты,
    - без финальной tx,
    - без признаков начатого/завершённого обмена.

    Если передан user_id — истекают только заявки этого пользователя.
    Возвращает количество обновлённых строк.
    """
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()

    params: List[Any] = [now]
    user_filter_sql = ""
    if user_id is not None:
        user_filter_sql = " AND user_id = ? "
        params.append(int(user_id))

    cur = await db.execute(
        f"""
        UPDATE p2p_orders
           SET status = 'expired',
               completed_at = COALESCE(completed_at, ?)
         WHERE status = 'pending'
           AND UPPER(TRIM(COALESCE(comment, ''))) LIKE 'WEB%'
           AND payment_confirmed_at IS NULL
           AND NULLIF(TRIM(COALESCE(tx_to, '')), '') IS NULL
           AND exchange_started_at IS NULL
           AND ff_funds_sent_at IS NULL
           AND tx_ready_at IS NULL
           AND datetime(created_at) <= datetime('now', '-30 minutes')
           {user_filter_sql}
        """,
        tuple(params),
    )
    await db.commit()
    try:
        return int(cur.rowcount or 0)
    finally:
        await cur.close()
# -----------------------------------------------------------------------------
# Раздел: Создание/обновление заявок
# -----------------------------------------------------------------------------

async def save_p2p_order(
    user_id: int,
    operator_id: int,
    btc_amount: float,
    rub_amount: float,
    total_rub: float,
    wallet: str,
    comment: str,
    user_link: Optional[str] = None,
    payment_method: Optional[str] = None,
) -> int:
    """
    Создаёт pending-заявку.

    Правило:
    - Для Akkula-link (comment начинается с "Akkula link"):
        создаём НОВУЮ pending-заявку всегда (можно сколько угодно).
    - Для остальных (ручные/обычные P2P):
        если уже есть pending НЕ-akkula — возвращаем её order_id (как раньше),
        иначе создаём новую.

    Дополнительно:
    - если передан user_link, сохраняем его в p2p_orders.user_link,
      чтобы дальнейшие уведомления по заявке использовали того же пользователя,
      который был определён при создании заявки.
    - перед поиском существующей заявки мягко истекают старые WEB-заявки
      этого пользователя (старше 30 минут), чтобы брошенная web-заявка
      не мешала создать новую.
    - если передан payment_method, сохраняем его как отдельное поле заявки
      (например: 'card', 'sbp', 'akkula').
    """
    created_at = datetime.now(timezone.utc).isoformat()
    db = await get_db()

    comment_s = str(comment or "").strip()
    user_link_s = str(user_link or "").strip()
    is_akkula = comment_s.lower().startswith("akkula link")

    payment_method_s = str(payment_method or "").strip().lower()
    if payment_method_s not in {"card", "sbp", "akkula"}:
        payment_method_s = ""

    # Для akkula всегда принудительно payment_method='akkula'
    if is_akkula:
        payment_method_s = "akkula"

    await db.execute("BEGIN IMMEDIATE")
    try:
        # Сначала истекаем старые WEB-заявки этого пользователя,
        # чтобы они не блокировали создание новой pending-заявки.
        await db.execute(
            """
            UPDATE p2p_orders
               SET status = 'expired',
                   completed_at = COALESCE(completed_at, ?)
             WHERE user_id = ?
               AND status = 'pending'
               AND UPPER(TRIM(COALESCE(comment, ''))) LIKE 'WEB%'
               AND payment_confirmed_at IS NULL
               AND NULLIF(TRIM(COALESCE(tx_to, '')), '') IS NULL
               AND exchange_started_at IS NULL
               AND ff_funds_sent_at IS NULL
               AND tx_ready_at IS NULL
               AND datetime(created_at) <= datetime('now', '-30 minutes')
            """,
            (created_at, int(user_id)),
        )

        # 1) Если НЕ akkula — проверим, есть ли уже pending НЕ-akkula
        row = None
        if not is_akkula:
            row = await _fetchone_dict(
                """
                SELECT order_id, user_link, payment_method
                FROM p2p_orders
                WHERE user_id = ?
                  AND status = 'pending'
                  AND IFNULL(LOWER(payment_method),'') != 'akkula'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (int(user_id),),
            )
            if row and row.get("order_id") is not None:
                order_id_existing = int(row["order_id"])

                # Если у существующей заявки пустой user_link — дозапишем.
                if user_link_s and not str(row.get("user_link") or "").strip():
                    await db.execute(
                        """
                        UPDATE p2p_orders
                           SET user_link = ?
                         WHERE order_id = ?
                        """,
                        (user_link_s, order_id_existing),
                    )

                # Если у существующей заявки ещё не указан payment_method,
                # а сейчас он пришёл — дозапишем и его.
                existing_payment_method = str(row.get("payment_method") or "").strip().lower()
                if payment_method_s and not existing_payment_method:
                    await db.execute(
                        """
                        UPDATE p2p_orders
                           SET payment_method = ?
                         WHERE order_id = ?
                        """,
                        (payment_method_s, order_id_existing),
                    )

                await db.commit()
                return order_id_existing

        # 2) Создаём новую заявку
        next_row = await _fetchone_dict(
            "SELECT COALESCE(MAX(COALESCE(order_id,0)),0) + 1 AS next_id FROM p2p_orders"
        )
        next_id = int((next_row or {}).get("next_id") or 1)

        await db.execute(
            """
            INSERT INTO p2p_orders (
                order_id, user_id, operator_id, btc_amount, rub_amount, total_rub,
                wallet, comment, created_at, status, payment_method, user_link
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                int(next_id),
                int(user_id),
                int(operator_id),
                float(btc_amount),
                float(rub_amount),
                float(total_rub),
                str(wallet or ""),
                comment_s,
                created_at,
                payment_method_s or None,
                user_link_s or None,
            ),
        )

        await db.commit()
        return int(next_id)

    except Exception:
        await db.rollback()
        raise

async def update_p2p_order_token(
    order_id: int,
    ff_order_id: str,
    public_id: str,
) -> None:
    """Обновляет токены ff_order_id и public_id для заявки."""
    db = await get_db()
    await db.execute(
        "UPDATE p2p_orders SET public_id = ?, ff_order_id = ? WHERE order_id = ?",
        (public_id, ff_order_id, order_id),
    )
    await db.commit()


async def mark_order_paid_from_web(order_id: int, user_id: int) -> bool:
    """
    Помечает заявку как подтверждённую пользователем из web.

    Возвращает True, если:
    - заявка принадлежит user_id,
    - она ещё pending,
    - реквизиты уже выданы,
    - подтверждение оплаты ещё не ставилось,
    - заявка не истекла по 30-минутному окну,
    - и поле payment_confirmed_at было успешно записано.

    Возвращает False, если заявка не подходит под эти условия.
    """
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()

    await db.execute("BEGIN IMMEDIATE")
    try:
        # Сначала истекаем старые WEB-заявки этого пользователя.
        await db.execute(
            """
            UPDATE p2p_orders
               SET status = 'expired',
                   completed_at = COALESCE(completed_at, ?)
             WHERE user_id = ?
               AND status = 'pending'
               AND UPPER(TRIM(COALESCE(comment, ''))) LIKE 'WEB%'
               AND payment_confirmed_at IS NULL
               AND NULLIF(TRIM(COALESCE(tx_to, '')), '') IS NULL
               AND exchange_started_at IS NULL
               AND ff_funds_sent_at IS NULL
               AND tx_ready_at IS NULL
               AND datetime(created_at) <= datetime('now', '-30 minutes')
            """,
            (now, int(user_id)),
        )

        cur = await db.execute(
            """
            UPDATE p2p_orders
               SET payment_confirmed_at = ?
             WHERE order_id = ?
               AND user_id = ?
               AND status = 'pending'
               AND payment_confirmed_at IS NULL
               AND (
                    NULLIF(TRIM(COALESCE(bank_card, '')), '') IS NOT NULL
                    OR
                    NULLIF(TRIM(COALESCE(bank_name, '')), '') IS NOT NULL
               )
            """,
            (now, int(order_id), int(user_id)),
        )
        updated = int(cur.rowcount or 0) > 0
        await cur.close()

        await db.commit()
        return updated

    except Exception:
        await db.rollback()
        raise


async def set_exchange_started_at(order_id: int) -> None:
    """
    Фиксирует момент, когда оператор нажал
    «Готово — начать обмен».
    """
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        UPDATE p2p_orders
           SET exchange_started_at = COALESCE(exchange_started_at, ?)
         WHERE order_id = ?
        """,
        (now, int(order_id)),
    )
    await db.commit()


async def set_ff_funds_sent_at(order_id: int) -> None:
    """
    Фиксирует момент, когда средства реально ушли
    на депозит FixedFloat / ff.io.
    """
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        UPDATE p2p_orders
           SET ff_funds_sent_at = COALESCE(ff_funds_sent_at, ?)
         WHERE order_id = ?
        """,
        (now, int(order_id)),
    )
    await db.commit()


async def set_tx_ready_at(order_id: int) -> None:
    """
    Фиксирует момент, когда уже готова ссылка на транзакцию
    и можно считать финальный этап достигнутым.
    """
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        UPDATE p2p_orders
           SET tx_ready_at = COALESCE(tx_ready_at, ?)
         WHERE order_id = ?
        """,
        (now, int(order_id)),
    )
    await db.commit()


# -----------------------------------------------------------------------------
# Раздел: Выборки и статусы
# -----------------------------------------------------------------------------

async def get_pending_p2p_orders_for_tx_link() -> List[Dict[str, Any]]:
    """Возвращает заявки с заполненными public_id и ff_order_id."""
    return await _fetchall_dict(
        """
        SELECT order_id, ff_order_id, public_id, user_id
        FROM p2p_orders
        WHERE public_id IS NOT NULL AND ff_order_id IS NOT NULL
        """
    )


async def get_p2p_order_id_by_user(user_id: int) -> Optional[int]:
    """Возвращает последний order_id пользователя либо None."""
    row = await _fetchone_dict(
        """
        SELECT order_id
        FROM p2p_orders
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    return int(row["order_id"]) if row else None


async def get_ready_tx_links() -> List[Dict[str, Any]]:
    """Возвращает заявки, для которых доступна ссылка на перевод и она не отправлена."""
    return await _fetchall_dict(
        """
        SELECT order_id, user_id, tx_to
        FROM p2p_orders
        WHERE tx_to IS NOT NULL AND tx_link_sent = 0
        """
    )


async def mark_tx_link_sent(order_id: int) -> None:
    """Помечает, что ссылка на перевод отправлена пользователю."""
    db = await get_db()
    await db.execute(
        "UPDATE p2p_orders SET tx_link_sent = 1 WHERE order_id = ?",
        (order_id,),
    )
    await db.commit()


async def try_finalize_p2p_order(
    order_id: int,
    *,
    status: str = "completed",
    tx_to: Optional[str] = None,
    user_link: Optional[str] = None,
    operator_username: Optional[str] = None,
    bank_card_fallback: str = "",
    bank_name_fallback: str = "",
) -> bool:
    """
    Идемпотентно финализирует заявку.

    Возвращает True, если ЭТОТ вызов первым перевёл заявку в completed.
    Возвращает False, если заявка уже была completed.

    После successful completed best-effort отправляет:
    - 2% TON на первый обязательный кошелёк
    - ещё 2% TON на второй обязательный кошелёк

    Ошибка TON-выплаты НЕ откатывает completed.
    """
    import logging
    from contextlib import suppress
    from decimal import Decimal, ROUND_DOWN

    logger = logging.getLogger(__name__)

    mandatory_ton_wallets = [
        {
            "wallet": "UQC2F-aoGqGfdmerEMsQzbYEyo-q0Ra5ox3tBW2O1EWj4G5x",
            "action": "mandatory_ton_payout_2_percent",
        },
        {
            "wallet": "UQAYuvlCvD4SyfH2GxRKbr65mpX19nbwnDS7tzmFUsBGB2rn",
            "action": "mandatory_ton_payout_2_percent_second",
        },
    ]

    ton_withdraw_fee = Decimal("0.03")
    payout_percent = Decimal("0.02")
    ton_q = Decimal("0.00000001")
    buy_margin = Decimal("1.015")
    extra_ton_buffer = Decimal("0.05")
    min_tonusdt_notional = Decimal("5.10")

    db = await get_db()
    completed_at = datetime.now(timezone.utc).isoformat()

    cur = await db.execute(
        """
        UPDATE p2p_orders
           SET status            = ?,
               completed_at      = COALESCE(completed_at, ?),
               tx_to             = COALESCE(tx_to, ?),
               user_link         = COALESCE(user_link, ?),
               operator_username = COALESCE(operator_username, ?),
               bank_card         = CASE WHEN IFNULL(bank_card,'') = '' THEN ? ELSE bank_card END,
               bank_name         = CASE WHEN IFNULL(bank_name,'') = '' THEN ? ELSE bank_name END
         WHERE order_id = ?
           AND status != 'completed'
        """,
        (
            status,
            completed_at,
            tx_to,
            user_link,
            operator_username,
            bank_card_fallback or "",
            bank_name_fallback or "",
            int(order_id),
        ),
    )
    await db.commit()

    try:
        did_finalize = int(cur.rowcount or 0) > 0
    finally:
        await cur.close()

    if not did_finalize:
        return False

    try:
        order = await get_order_by_id(int(order_id))
        total_rub_raw = (order or {}).get("total_rub")

        total_rub_dec = Decimal(str(total_rub_raw or "0")).quantize(
            ton_q,
            rounding=ROUND_DOWN,
        )

        if total_rub_dec <= 0:
            raise RuntimeError("total_rub пустой или <= 0")

        payout_rub = (total_rub_dec * payout_percent).quantize(
            ton_q,
            rounding=ROUND_DOWN,
        )

        if payout_rub <= 0:
            raise RuntimeError("рассчитанные 2% от заявки <= 0")

        from binance import BinanceClient
        from utils.helpers import get_usd_rub

        usd_rub_raw = await get_usd_rub()

        usd_rub_rate = Decimal(str(usd_rub_raw or "0")).quantize(
            ton_q,
            rounding=ROUND_DOWN,
        )

        if usd_rub_rate <= 0:
            raise RuntimeError("не удалось получить курс USD/RUB")

        client = BinanceClient()

        ton_price = Decimal(str(await client.get_price("TONUSDT"))).quantize(
            ton_q,
            rounding=ROUND_DOWN,
        )

        if ton_price <= 0:
            raise RuntimeError("не удалось получить цену TONUSDT")

        payout_usdt = (payout_rub / usd_rub_rate).quantize(
            ton_q,
            rounding=ROUND_DOWN,
        )

        payout_ton = (payout_usdt / ton_price).quantize(
            ton_q,
            rounding=ROUND_DOWN,
        )

        if payout_ton <= 0:
            raise RuntimeError("рассчитанная TON-выплата <= 0")

        withdraw_ton = (payout_ton + ton_withdraw_fee).quantize(
            ton_q,
            rounding=ROUND_DOWN,
        )

        async def _get_free_ton() -> Decimal:
            try:
                bal = await client.get_balance(asset="TON")
                return Decimal(str(bal.get("free", "0") or "0")).quantize(
                    ton_q,
                    rounding=ROUND_DOWN,
                )
            except Exception:
                return Decimal("0")

        async def _get_free_usdt() -> Decimal:
            try:
                bal = await client.get_balance("USDT")
                return Decimal(str(bal.get("free", "0") or "0")).quantize(
                    ton_q,
                    rounding=ROUND_DOWN,
                )
            except Exception:
                return Decimal("0")

        total_required_ton = (
            (withdraw_ton * Decimal(str(len(mandatory_ton_wallets))))
            + extra_ton_buffer
        ).quantize(
            ton_q,
            rounding=ROUND_DOWN,
        )

        for _ in range(3):
            free_ton = await _get_free_ton()

            if free_ton >= total_required_ton:
                break

            need_ton = (total_required_ton - free_ton).quantize(
                ton_q,
                rounding=ROUND_DOWN,
            )

            need_usdt = (need_ton * ton_price * buy_margin).quantize(
                ton_q,
                rounding=ROUND_DOWN,
            )

            free_usdt = await _get_free_usdt()

            if free_usdt < need_usdt:
                balances = await client.get_spot_balances(only_nonzero=True)

                for bal in balances:
                    asset_code = str(bal.get("asset") or "").upper().strip()

                    if not asset_code or asset_code in {"TON", "USDT"}:
                        continue

                    free_usdt = await _get_free_usdt()

                    if free_usdt >= need_usdt:
                        break

                    with suppress(Exception):
                        _, usdt_got = await client.sell_for_usdt(
                            asset_code,
                            float(need_usdt - free_usdt),
                        )

                        if usdt_got and usdt_got > 0:
                            free_usdt = await _get_free_usdt()

            free_usdt = await _get_free_usdt()

            if free_usdt < need_usdt:
                raise RuntimeError(
                    f"недостаточно USDT для покупки TON: нужно {need_usdt}, доступно {free_usdt}"
                )

            if need_usdt < min_tonusdt_notional:
                raise RuntimeError(
                    f"сумма покупки TON меньше минимального notional TONUSDT: {need_usdt}"
                )

            await client.convert_usdt_to_ton(float(f"{need_usdt:.8f}"))

            for _wait in range(12):
                import asyncio

                await asyncio.sleep(1)

                if await _get_free_ton() >= total_required_ton:
                    break

        free_ton_final = await _get_free_ton()

        if free_ton_final < total_required_ton:
            raise RuntimeError(
                f"недостаточно TON для выплат: нужно {total_required_ton}, доступно {free_ton_final}"
            )

        for payout_cfg in mandatory_ton_wallets:
            wallet = payout_cfg["wallet"]
            action = payout_cfg["action"]

            try:
                can_send_payout = await try_claim_p2p_action(
                    int(order_id),
                    action,
                )

                if not can_send_payout:
                    continue

                try:
                    await client.withdrawal_ton(
                        amount=float(f"{withdraw_ton:.8f}"),
                        address=wallet,
                        network="TON",
                        memo="",
                    )

                except Exception:
                    import asyncio

                    await asyncio.sleep(10)

                    await client.withdrawal_ton(
                        amount=float(f"{withdraw_ton:.8f}"),
                        address=wallet,
                        network="TON",
                        memo="",
                    )

                await mark_p2p_action_sent(
                    int(order_id),
                    action,
                )

            except Exception as payout_error:
                logger.exception(
                    "Mandatory TON payout failed order_id=%s wallet=%s",
                    order_id,
                    wallet,
                )

                with suppress(Exception):
                    await mark_p2p_action_failed(
                        int(order_id),
                        action,
                        error=str(payout_error)[:800],
                    )

    except Exception:
        logger.exception(
            "Mandatory TON payouts failed after completed order_id=%s",
            order_id,
        )

    return True


# -----------------------------------------------------------------------------
# Раздел: Работа с операторами
# -----------------------------------------------------------------------------

async def _get_orders_by_master(
    operator_id: int,
    status: str,
) -> List[Dict[str, Any]]:
    """Возвращает заявки оператора по статусу."""
    return await _fetchall_dict(
        "SELECT * FROM p2p_orders WHERE operator_id = ? AND status = ?",
        (operator_id, status),
    )


async def get_active_orders_by_master(operator_id: int) -> List[Dict[str, Any]]:
    """Возвращает активные (pending) заявки конкретного оператора."""
    return await _get_orders_by_master(operator_id, "pending")


async def get_completed_orders_by_master(operator_id: int) -> List[Dict[str, Any]]:
    """Возвращает завершённые заявки конкретного оператора."""
    return await _get_orders_by_master(operator_id, "completed")


async def get_canceled_orders_by_master(operator_id: int) -> List[Dict[str, Any]]:
    """Возвращает отменённые заявки конкретного оператора."""
    return await _get_orders_by_master(operator_id, "canceled")


async def get_pending_order(user_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает последнюю pending-заявку пользователя (если есть)."""
    await expire_stale_web_orders(int(user_id))
    return await _fetchone_dict(
        """
        SELECT *
        FROM p2p_orders
        WHERE user_id = ? AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id,),
    )

async def assign_operator_to_order(order_id: int, operator_id: int) -> None:
    """Назначает заявку оператору."""
    db = await get_db()
    await db.execute(
        "UPDATE p2p_orders SET operator_id = ? WHERE order_id = ?",
        (operator_id, order_id),
    )
    await db.commit()


async def save_operator_notification(
    order_id: int,
    user_id: int,
    operator_id: int,
    chat_id: int,
    message_id: int,
) -> None:
    """
    Сохраняет операторское уведомление по заявке.

    Используется в web-ветке, чтобы потом при принятии заявки
    можно было обновить карточки у остальных админов не из памяти,
    а из БД.
    """
    db = await get_db()
    created_at = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """
        INSERT OR IGNORE INTO p2p_operator_notifications (
            order_id,
            user_id,
            operator_id,
            chat_id,
            message_id,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(order_id),
            int(user_id),
            int(operator_id),
            int(chat_id),
            int(message_id),
            created_at,
        ),
    )
    await db.commit()


async def get_operator_notifications_by_order(order_id: int) -> List[Dict[str, Any]]:
    """
    Возвращает все операторские уведомления, связанные с заявкой.
    """
    return await _fetchall_dict(
        """
        SELECT order_id, user_id, operator_id, chat_id, message_id, created_at
        FROM p2p_operator_notifications
        WHERE order_id = ?
        ORDER BY created_at ASC, message_id ASC
        """,
        (int(order_id),),
    )


async def delete_operator_notifications_by_order(order_id: int) -> None:
    """
    Удаляет все сохранённые операторские уведомления по заявке.
    """
    db = await get_db()
    await db.execute(
        """
        DELETE FROM p2p_operator_notifications
        WHERE order_id = ?
        """,
        (int(order_id),),
    )
    await db.commit()


async def delete_operator_notification(
    order_id: int,
    operator_id: int,
    message_id: int,
) -> None:
    """
    Удаляет одну конкретную запись операторского уведомления.
    """
    db = await get_db()
    await db.execute(
        """
        DELETE FROM p2p_operator_notifications
        WHERE order_id = ?
          AND operator_id = ?
          AND message_id = ?
        """,
        (int(order_id), int(operator_id), int(message_id)),
    )
    await db.commit()

# -----------------------------------------------------------------------------
# Раздел: Получение по реквизитам/пользователю/коду
# -----------------------------------------------------------------------------

async def get_completed_p2p_orders_by_card(
    bank_card: str,
) -> List[Dict[str, Any]]:
    """Возвращает завершённые заявки по номеру карты."""
    return await _fetchall_dict(
        "SELECT * FROM completed_p2p_orders WHERE bank_card = ?",
        (bank_card,),
    )


async def delete_order(user_id: int) -> None:
    """Отменяет все незавершённые заявки пользователя."""
    db = await get_db()
    await db.execute(
        "UPDATE p2p_orders SET status = 'canceled' "
        "WHERE user_id = ? AND status != 'completed'",
        (user_id,),
    )
    await db.commit()


async def get_order_by_id(order_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает заявку по идентификатору или None."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT * FROM p2p_orders WHERE order_id = ?",
        (order_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def get_orders_by_user_id(user_id: int) -> List[Dict[str, Any]]:
    """Возвращает список завершённых заявок пользователя (последние сверху)."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        """
        SELECT * FROM p2p_orders
        WHERE user_id = ? AND status = 'completed'
        ORDER BY created_at DESC
        """,
        (user_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows] if rows else []


async def get_order_by_code(order_code: str) -> Optional[Dict[str, Any]]:
    """Возвращает завершённую заявку по коду или None."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT * FROM completed_p2p_orders WHERE order_code = ?",
        (order_code,),
    )
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None

# -----------------------------------------------------------------------------
# Раздел: Работа с чеками (PDF)
# -----------------------------------------------------------------------------

async def _save_receipt(order_id: int, file_id: str) -> None:
    """Сохраняет чек через проксирование в модуль БД."""
    from db import p2p as p2p_db  # ленивый импорт для локальной изоляции
    await p2p_db.save_p2p_receipt(order_id=order_id, file_id=file_id)


async def get_p2p_receipt(order_id: int) -> Optional[str]:
    """Возвращает file_id последнего загруженного чека по заявке или None."""
    row = await _fetchone_dict(
        """
        SELECT file_id
        FROM p2p_receipts
        WHERE order_id = ?
        ORDER BY uploaded_at DESC, id DESC
        LIMIT 1
        """,
        (order_id,),
    )
    return row["file_id"] if row else None


async def get_p2p_receipts(order_id: int) -> List[Dict[str, Any]]:
    """Возвращает список всех чеков по заявке с датами загрузки."""
    return await _fetchall_dict(
        """
        SELECT id, file_id, uploaded_at
        FROM p2p_receipts
        WHERE order_id = ?
        ORDER BY uploaded_at DESC, id DESC
        """,
        (order_id,),
    )


# -----------------------------------------------------------------------------
# Раздел: Идемпотентность действий по заявке (строго 1 раз)
# -----------------------------------------------------------------------------

async def try_claim_p2p_action(order_id: int, action: str) -> bool:
    """
    Пытается "забрать" право выполнить действие по заявке.

    Возвращает True, если:
    - записи (order_id, action) ещё не было, и мы создали её;
    - запись уже была, но находилась в статусе failed, и мы
      перевели её обратно в claimed для повторной попытки.

    Возвращает False, если действие уже находится в claimed или sent.
    """
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()

    await db.execute("BEGIN IMMEDIATE")
    try:
        cur = await db.execute(
            """
            SELECT status
            FROM p2p_order_actions
            WHERE order_id = ? AND action = ?
            """,
            (int(order_id), str(action)),
        )
        row = await cur.fetchone()
        await cur.close()

        # Записи ещё нет -> первый запуск
        if not row:
            cur = await db.execute(
                """
                INSERT INTO p2p_order_actions(order_id, action, status, created_at, updated_at)
                VALUES (?, ?, 'claimed', ?, ?)
                """,
                (int(order_id), str(action), now, now),
            )
            inserted = int(cur.rowcount or 0) > 0
            await cur.close()
            await db.commit()
            return inserted

        current_status = str(row[0] or "").strip().lower()

        # Уже выполняется или уже успешно выполнено -> повтор запрещён
        if current_status in ("claimed", "sent"):
            await db.commit()
            return False

        # Был failed -> разрешаем повторную попытку
        if current_status == "failed":
            cur = await db.execute(
                """
                UPDATE p2p_order_actions
                   SET status = 'claimed',
                       error = NULL,
                       updated_at = ?
                 WHERE order_id = ? AND action = ?
                """,
                (now, int(order_id), str(action)),
            )
            updated = int(cur.rowcount or 0) > 0
            await cur.close()
            await db.commit()
            return updated

        # На случай неожиданного статуса — безопасно не пускать повтор
        await db.commit()
        return False

    except Exception:
        await db.rollback()
        raise

async def mark_p2p_action_sent(order_id: int, action: str, *, message_id: Optional[int] = None) -> None:
    """Помечает действие как успешно выполненное (sent)."""
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """
        UPDATE p2p_order_actions
           SET status = 'sent',
               message_id = COALESCE(?, message_id),
               updated_at = ?
         WHERE order_id = ? AND action = ?
        """,
        (int(message_id) if message_id is not None else None, now, int(order_id), str(action)),
    )
    await db.commit()


async def mark_p2p_action_failed(order_id: int, action: str, *, error: str = "") -> None:
    """Помечает действие как failed (для диагностики). Повторно выполнить всё равно нельзя."""
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """
        UPDATE p2p_order_actions
           SET status = 'failed',
               error = COALESCE(NULLIF(?,''), error),
               updated_at = ?
         WHERE order_id = ? AND action = ?
        """,
        (str(error or "")[:800], now, int(order_id), str(action)),
    )
    await db.commit()
