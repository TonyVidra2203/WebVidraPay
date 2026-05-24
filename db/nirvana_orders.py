from __future__ import annotations

import datetime as dt
from contextlib import suppress
from typing import Any, Dict, Optional

from db.connection import get_db


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _row_to_dict(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


async def init_nirvana_orders_db() -> None:
    db = await get_db()

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS nirvana_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL UNIQUE,
            tracker_id TEXT,
            p2p_order_id INTEGER,
            tg_user_id INTEGER,
            status TEXT NOT NULL DEFAULT 'CREATED',
            amount REAL,
            amount_crypto REAL,
            crypto_asset TEXT,
            amount_fiat_received REAL,
            rate REAL,
            token TEXT,
            currency TEXT,
            receiver TEXT,
            bank_name TEXT,
            recipient_name TEXT,
            redirect_url TEXT,
            callback_url TEXT,
            raw_create_response TEXT,
            raw_status_response TEXT,
            processed_success INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )

    with suppress(Exception):
        await db.execute("ALTER TABLE nirvana_orders ADD COLUMN crypto_asset TEXT")
    with suppress(Exception):
        await db.execute("ALTER TABLE nirvana_orders ADD COLUMN redirect_url TEXT")

    await db.execute("CREATE INDEX IF NOT EXISTS idx_nirvana_orders_client_id ON nirvana_orders(client_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_nirvana_orders_tracker_id ON nirvana_orders(tracker_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_nirvana_orders_p2p_order_id ON nirvana_orders(p2p_order_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_nirvana_orders_tg_user_id ON nirvana_orders(tg_user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_nirvana_orders_status ON nirvana_orders(status)")

    await db.commit()


async def save_nirvana_order(
    *,
    client_id: str,
    tracker_id: Optional[str] = None,
    p2p_order_id: Optional[int] = None,
    tg_user_id: Optional[int] = None,
    status: str = "CREATED",
    amount: Optional[float] = None,
    amount_crypto: Optional[float] = None,
    crypto_asset: Optional[str] = None,
    amount_fiat_received: Optional[float] = None,
    rate: Optional[float] = None,
    token: Optional[str] = None,
    currency: Optional[str] = None,
    receiver: Optional[str] = None,
    bank_name: Optional[str] = None,
    recipient_name: Optional[str] = None,
    redirect_url: Optional[str] = None,
    callback_url: Optional[str] = None,
    raw_create_response: Optional[str] = None,
    raw_status_response: Optional[str] = None,
) -> None:
    await init_nirvana_orders_db()

    db = await get_db()
    now = _utcnow_iso()

    await db.execute(
        """
        INSERT INTO nirvana_orders (
            client_id,
            tracker_id,
            p2p_order_id,
            tg_user_id,
            status,
            amount,
            amount_crypto,
            crypto_asset,
            amount_fiat_received,
            rate,
            token,
            currency,
            receiver,
            bank_name,
            recipient_name,
            redirect_url,
            callback_url,
            raw_create_response,
            raw_status_response,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(client_id) DO UPDATE SET
            tracker_id = excluded.tracker_id,
            p2p_order_id = excluded.p2p_order_id,
            tg_user_id = excluded.tg_user_id,
            status = excluded.status,
            amount = excluded.amount,
            amount_crypto = excluded.amount_crypto,
            crypto_asset = excluded.crypto_asset,
            amount_fiat_received = excluded.amount_fiat_received,
            rate = excluded.rate,
            token = excluded.token,
            currency = excluded.currency,
            receiver = excluded.receiver,
            bank_name = excluded.bank_name,
            recipient_name = excluded.recipient_name,
            redirect_url = excluded.redirect_url,
            callback_url = excluded.callback_url,
            raw_create_response = excluded.raw_create_response,
            raw_status_response = excluded.raw_status_response,
            updated_at = excluded.updated_at
        """,
        (
            str(client_id),
            tracker_id,
            p2p_order_id,
            tg_user_id,
            str(status or "CREATED").upper(),
            amount,
            amount_crypto,
            crypto_asset,
            amount_fiat_received,
            rate,
            token,
            currency,
            receiver,
            bank_name,
            recipient_name,
            redirect_url,
            callback_url,
            raw_create_response,
            raw_status_response,
            now,
            now,
        ),
    )

    await db.commit()


async def get_nirvana_order_by_client_id(client_id: str) -> Optional[Dict[str, Any]]:
    await init_nirvana_orders_db()

    db = await get_db()
    cur = await db.execute(
        """
        SELECT *
        FROM nirvana_orders
        WHERE client_id = ?
        LIMIT 1
        """,
        (str(client_id),),
    )
    row = await cur.fetchone()
    await cur.close()

    return _row_to_dict(row)


async def get_nirvana_order_by_p2p_order_id(p2p_order_id: int) -> Optional[Dict[str, Any]]:
    await init_nirvana_orders_db()

    db = await get_db()
    cur = await db.execute(
        """
        SELECT *
        FROM nirvana_orders
        WHERE p2p_order_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(p2p_order_id),),
    )
    row = await cur.fetchone()
    await cur.close()

    return _row_to_dict(row)


async def update_nirvana_order_status(
    *,
    client_id: str,
    status: str,
    amount_fiat_received: Optional[float] = None,
    raw_status_response: Optional[str] = None,
) -> None:
    await init_nirvana_orders_db()

    db = await get_db()
    now = _utcnow_iso()

    await db.execute(
        """
        UPDATE nirvana_orders
        SET
            status = ?,
            amount_fiat_received = COALESCE(?, amount_fiat_received),
            raw_status_response = COALESCE(?, raw_status_response),
            updated_at = ?
        WHERE client_id = ?
        """,
        (
            str(status or "").upper(),
            amount_fiat_received,
            raw_status_response,
            now,
            str(client_id),
        ),
    )

    await db.commit()


async def mark_nirvana_order_success_processed(client_id: str) -> None:
    await init_nirvana_orders_db()

    db = await get_db()
    now = _utcnow_iso()

    await db.execute(
        """
        UPDATE nirvana_orders
        SET
            processed_success = 1,
            updated_at = ?
        WHERE client_id = ?
        """,
        (now, str(client_id)),
    )

    await db.commit()