import datetime as dt
from typing import Any, Dict, Optional

from db.connection import get_db


async def init_akkula_orders_db() -> None:
    db = await get_db()

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS akkula_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_order_id TEXT NOT NULL UNIQUE,
            order_id TEXT,
            tg_user_id INTEGER NOT NULL,
            status TEXT,
            amount_rub REAL,
            amount_usdt REAL,
            network TEXT,
            recipient_wallet TEXT,
            short_payment_url TEXT,
            payment_url TEXT,
            qr_image_url TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )

    cur = await db.execute("PRAGMA table_info(akkula_orders)")
    rows = await cur.fetchall()
    await cur.close()
    existing_cols = {row[1] for row in rows}

    if "user_selected_asset" not in existing_cols:
        await db.execute("ALTER TABLE akkula_orders ADD COLUMN user_selected_asset TEXT")
    if "user_recipient_wallet" not in existing_cols:
        await db.execute("ALTER TABLE akkula_orders ADD COLUMN user_recipient_wallet TEXT")
    if "p2p_order_id" not in existing_cols:
        await db.execute("ALTER TABLE akkula_orders ADD COLUMN p2p_order_id INTEGER")
    if "tx_hash" not in existing_cols:
        await db.execute("ALTER TABLE akkula_orders ADD COLUMN tx_hash TEXT")
    if "processed_completed" not in existing_cols:
        await db.execute("ALTER TABLE akkula_orders ADD COLUMN processed_completed INTEGER NOT NULL DEFAULT 0")
    if "link_message_id" not in existing_cols:
        await db.execute("ALTER TABLE akkula_orders ADD COLUMN link_message_id INTEGER")

    await db.execute("CREATE INDEX IF NOT EXISTS idx_akkula_orders_partner ON akkula_orders(partner_order_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_akkula_orders_order_id ON akkula_orders(order_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_akkula_orders_user ON akkula_orders(tg_user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_akkula_orders_p2p ON akkula_orders(p2p_order_id)")
    await db.commit()


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def save_akkula_order(
    *,
    partner_order_id: str,
    order_id: Optional[str],
    tg_user_id: int,
    status: Optional[str] = None,
    amount_rub: Optional[float] = None,
    amount_usdt: Optional[float] = None,
    network: Optional[str] = None,
    recipient_wallet: Optional[str] = None,
    short_payment_url: Optional[str] = None,
    payment_url: Optional[str] = None,
    qr_image_url: Optional[str] = None,
    expires_at: Optional[str] = None,
    user_selected_asset: Optional[str] = None,
    user_recipient_wallet: Optional[str] = None,
    p2p_order_id: Optional[int] = None,
    link_message_id: Optional[int] = None,
) -> None:
    await init_akkula_orders_db()
    db = await get_db()
    now = _utcnow_iso()

    await db.execute(
        """
        INSERT INTO akkula_orders (
            partner_order_id, order_id, tg_user_id, status, amount_rub, amount_usdt, network,
            recipient_wallet, short_payment_url, payment_url, qr_image_url, expires_at,
            user_selected_asset, user_recipient_wallet, p2p_order_id,
            link_message_id,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(partner_order_id) DO UPDATE SET
            order_id=excluded.order_id,
            tg_user_id=excluded.tg_user_id,
            status=COALESCE(excluded.status, akkula_orders.status),
            amount_rub=COALESCE(excluded.amount_rub, akkula_orders.amount_rub),
            amount_usdt=COALESCE(excluded.amount_usdt, akkula_orders.amount_usdt),
            network=COALESCE(excluded.network, akkula_orders.network),
            recipient_wallet=COALESCE(excluded.recipient_wallet, akkula_orders.recipient_wallet),
            short_payment_url=COALESCE(excluded.short_payment_url, akkula_orders.short_payment_url),
            payment_url=COALESCE(excluded.payment_url, akkula_orders.payment_url),
            qr_image_url=COALESCE(excluded.qr_image_url, akkula_orders.qr_image_url),
            expires_at=COALESCE(excluded.expires_at, akkula_orders.expires_at),

            user_selected_asset=COALESCE(excluded.user_selected_asset, akkula_orders.user_selected_asset),
            user_recipient_wallet=COALESCE(excluded.user_recipient_wallet, akkula_orders.user_recipient_wallet),
            p2p_order_id=COALESCE(excluded.p2p_order_id, akkula_orders.p2p_order_id),

            link_message_id=COALESCE(excluded.link_message_id, akkula_orders.link_message_id),

            updated_at=excluded.updated_at
        """,
        (
            partner_order_id,
            order_id,
            tg_user_id,
            status,
            amount_rub,
            amount_usdt,
            network,
            recipient_wallet,
            short_payment_url,
            payment_url,
            qr_image_url,
            expires_at,
            user_selected_asset,
            user_recipient_wallet,
            p2p_order_id,
            link_message_id,
            now,
            now,
        ),
    )
    await db.commit()


async def update_akkula_order_status(
    *,
    partner_order_id: str,
    status: str,
    order_id: Optional[str] = None,
    tx_hash: Optional[str] = None,
) -> None:
    await init_akkula_orders_db()
    db = await get_db()
    now = _utcnow_iso()

    await db.execute(
        """
        UPDATE akkula_orders
        SET status = ?,
            order_id = COALESCE(?, order_id),
            tx_hash = COALESCE(?, tx_hash),
            updated_at = ?
        WHERE partner_order_id = ?
        """,
        (status, order_id, tx_hash, now, partner_order_id),
    )
    await db.commit()


async def get_akkula_order_by_partner_id(partner_order_id: str) -> Optional[Dict[str, Any]]:
    await init_akkula_orders_db()
    db = await get_db()
    cur = await db.execute(
        """
        SELECT
            partner_order_id, order_id, tg_user_id, status, amount_rub, amount_usdt, network,
            recipient_wallet, short_payment_url, payment_url, qr_image_url, expires_at,
            user_selected_asset, user_recipient_wallet, p2p_order_id,
            tx_hash, processed_completed,
            link_message_id,
            created_at, updated_at
        FROM akkula_orders
        WHERE partner_order_id = ?
        """,
        (partner_order_id,),
    )
    row = await cur.fetchone()
    await cur.close()

    if not row:
        return None

    keys = [
        "partner_order_id",
        "order_id",
        "tg_user_id",
        "status",
        "amount_rub",
        "amount_usdt",
        "network",
        "recipient_wallet",
        "short_payment_url",
        "payment_url",
        "qr_image_url",
        "expires_at",
        "user_selected_asset",
        "user_recipient_wallet",
        "p2p_order_id",
        "tx_hash",
        "processed_completed",
        "link_message_id",
        "created_at",
        "updated_at",
    ]
    return dict(zip(keys, row))


async def get_akkula_order_by_p2p_order_id(p2p_order_id: int) -> Optional[Dict[str, Any]]:
    """
    Находит последнюю (по created_at) запись akkula_orders для конкретной p2p-заявки.
    Нужна для защиты от дублей: если пользователь нажал "Оплатить по ссылке" несколько раз,
    мы не создаём новые ссылки, а показываем уже созданную.

    Возвращает dict в том же формате, что и get_akkula_order_by_partner_id, либо None.
    """
    await init_akkula_orders_db()
    db = await get_db()

    cur = await db.execute(
        """
        SELECT
            partner_order_id, order_id, tg_user_id, status, amount_rub, amount_usdt, network,
            recipient_wallet, short_payment_url, payment_url, qr_image_url, expires_at,
            user_selected_asset, user_recipient_wallet, p2p_order_id,
            tx_hash, processed_completed,
            link_message_id,
            created_at, updated_at
        FROM akkula_orders
        WHERE p2p_order_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (int(p2p_order_id),),
    )
    row = await cur.fetchone()
    await cur.close()

    if not row:
        return None

    keys = [
        "partner_order_id",
        "order_id",
        "tg_user_id",
        "status",
        "amount_rub",
        "amount_usdt",
        "network",
        "recipient_wallet",
        "short_payment_url",
        "payment_url",
        "qr_image_url",
        "expires_at",
        "user_selected_asset",
        "user_recipient_wallet",
        "p2p_order_id",
        "tx_hash",
        "processed_completed",
        "link_message_id",
        "created_at",
        "updated_at",
    ]
    return dict(zip(keys, row))


async def try_mark_akkula_completed_processed(partner_order_id: str) -> bool:
    """
    Идемпотентность: помечаем completed-ивент обработанным ровно один раз.
    Возвращает True, если мы первые (и должны запускать обмен),
    False — если уже обработано ранее.
    """
    await init_akkula_orders_db()
    db = await get_db()
    now = _utcnow_iso()

    cur = await db.execute(
        """
        UPDATE akkula_orders
           SET processed_completed = 1,
               updated_at = ?
         WHERE partner_order_id = ?
           AND processed_completed = 0
        """,
        (now, str(partner_order_id)),
    )
    await db.commit()
    try:
        return (cur.rowcount or 0) > 0
    except Exception:
        return False
