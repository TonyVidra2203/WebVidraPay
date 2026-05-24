from typing import Optional

from db.connection import get_db


async def _ensure_phone_column() -> None:
    db = await get_db()
    cur = await db.execute("PRAGMA table_info(casino_wallets)")
    rows = await cur.fetchall()
    columns = {str(row[1]) for row in rows}

    if "phone" not in columns:
        await db.execute("ALTER TABLE casino_wallets ADD COLUMN phone TEXT")
        await db.commit()


async def init_casino_wallets_table() -> None:
    db = await get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS casino_wallets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            casino_key   TEXT    NOT NULL,
            wallet       TEXT    NOT NULL,
            phone        TEXT,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, casino_key)
        )
        """
    )
    await db.commit()
    await _ensure_phone_column()


async def get_casino_wallet(user_id: int, casino_key: str) -> Optional[str]:
    await init_casino_wallets_table()
    db = await get_db()
    cur = await db.execute(
        """
        SELECT wallet
        FROM casino_wallets
        WHERE user_id = ? AND casino_key = ?
        LIMIT 1
        """,
        (int(user_id), str(casino_key)),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return str(row[0] or "").strip() or None


async def get_casino_phone(user_id: int, casino_key: str) -> Optional[str]:
    await init_casino_wallets_table()
    db = await get_db()
    cur = await db.execute(
        """
        SELECT phone
        FROM casino_wallets
        WHERE user_id = ? AND casino_key = ?
        LIMIT 1
        """,
        (int(user_id), str(casino_key)),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return str(row[0] or "").strip() or None


async def upsert_casino_wallet(user_id: int, casino_key: str, wallet: str) -> None:
    await init_casino_wallets_table()
    db = await get_db()
    await db.execute(
        """
        INSERT INTO casino_wallets (user_id, casino_key, wallet)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, casino_key)
        DO UPDATE SET
            wallet = excluded.wallet,
            updated_at = datetime('now')
        """,
        (int(user_id), str(casino_key), str(wallet)),
    )
    await db.commit()


async def upsert_casino_phone(user_id: int, casino_key: str, phone: str) -> None:
    await init_casino_wallets_table()
    db = await get_db()
    await db.execute(
        """
        INSERT INTO casino_wallets (user_id, casino_key, wallet, phone)
        VALUES (?, ?, '', ?)
        ON CONFLICT(user_id, casino_key)
        DO UPDATE SET
            phone = excluded.phone,
            updated_at = datetime('now')
        """,
        (int(user_id), str(casino_key), str(phone)),
    )
    await db.commit()


async def reset_casino_profile(user_id: int, casino_key: str) -> None:
    await init_casino_wallets_table()
    db = await get_db()
    await db.execute(
        """
        DELETE FROM casino_wallets
        WHERE user_id = ? AND casino_key = ?
        """,
        (int(user_id), str(casino_key)),
    )
    await db.commit()