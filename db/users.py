from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
import secrets
import string

import aiosqlite

from db.connection import get_db

DATETIME_FORMAT: str = "%d.%m.%Y %H:%M"
WEB_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
WEB_PASSWORD_LENGTH = 10


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime(DATETIME_FORMAT)


def _generate_web_password(length: int = WEB_PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(WEB_PASSWORD_ALPHABET) for _ in range(length))


_CREATE_USERS_TABLE: str = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id        INTEGER PRIMARY KEY,
    username           TEXT,
    role               TEXT    NOT NULL DEFAULT 'User',
    created_at         TEXT    NOT NULL,
    last_active        TEXT    NOT NULL,
    btc_wallet         TEXT,
    ltc_wallet         TEXT,
    usdt_trc20_wallet  TEXT,
    is_active          INTEGER NOT NULL DEFAULT 1,
    referrer_id        INTEGER,
    referral_link      TEXT,
    commission_percent REAL,
    binance_verified   INTEGER NOT NULL DEFAULT 0,
    web_password       TEXT UNIQUE
);
"""

_BASE_SELECT: str = """
SELECT
    telegram_id, username, role,
    created_at, last_active,
    btc_wallet, ltc_wallet, usdt_trc20_wallet,
    is_active, referrer_id, referral_link,
    commission_percent,
    binance_verified,
    web_password
FROM users
"""


async def init_users_db() -> None:
    db = await get_db()
    await db.execute(_CREATE_USERS_TABLE)

    cursor = await db.execute("PRAGMA table_info(users)")
    rows = await cursor.fetchall()
    await cursor.close()
    existing = {row[1] for row in rows}

    deprecated_columns = {
        "card_info",
        "sbp_info",
        "bank_info",
        "comment_info",
    }

    if any(col in existing for col in deprecated_columns):
        await _migrate_users_table_cleanup(db, existing)
    else:
        if "commission_percent" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN commission_percent REAL")
        if "binance_verified" not in existing:
            await db.execute(
                "ALTER TABLE users ADD COLUMN binance_verified INTEGER NOT NULL DEFAULT 0"
            )
        if "ltc_wallet" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN ltc_wallet TEXT")
        if "usdt_trc20_wallet" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN usdt_trc20_wallet TEXT")
        if "web_password" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN web_password TEXT")

    await db.commit()


async def _migrate_users_table_cleanup(
    db: aiosqlite.Connection,
    existing_columns: set[str],
) -> None:
    desired_columns: List[str] = [
        "telegram_id",
        "username",
        "role",
        "created_at",
        "last_active",
        "btc_wallet",
        "ltc_wallet",
        "usdt_trc20_wallet",
        "is_active",
        "referrer_id",
        "referral_link",
        "commission_percent",
        "binance_verified",
        "web_password",
    ]

    common_columns: List[str] = [col for col in desired_columns if col in existing_columns]

    await db.execute("ALTER TABLE users RENAME TO users_old;")
    await db.execute(_CREATE_USERS_TABLE)

    if common_columns:
        cols_sql = ", ".join(common_columns)
        await db.execute(
            f"INSERT INTO users ({cols_sql}) SELECT {cols_sql} FROM users_old;"
        )

    await db.execute("DROP TABLE users_old;")


async def _execute(query: str, params: tuple = ()) -> None:
    db = await get_db()
    await db.execute(query, params)
    await db.commit()


async def _fetchone(query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    db = await get_db()
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def _fetchall(query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    db = await get_db()
    cur = await db.execute(query, params)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _generate_unique_web_password() -> str:
    await init_users_db()

    for _ in range(100):
        candidate = _generate_web_password()
        existing = await _fetchone(
            "SELECT telegram_id FROM users WHERE web_password = ?",
            (candidate,),
        )
        if not existing:
            return candidate

    raise RuntimeError("Не удалось сгенерировать уникальный web_password")


async def add_or_update_user(
    telegram_id: int,
    username: Optional[str],
    referrer_id: Optional[int] = None,
) -> None:
    await init_users_db()
    now = _now_str()

    existing = await _fetchone(
        "SELECT telegram_id, referrer_id, web_password FROM users WHERE telegram_id = ?",
        (telegram_id,),
    )

    if referrer_id == telegram_id:
        referrer_id = None

    if existing:
        await _execute(
            "UPDATE users SET username = ?, last_active = ? WHERE telegram_id = ?",
            (username, now, telegram_id),
        )
        if existing.get("referrer_id") is None and referrer_id is not None:
            await _execute(
                "UPDATE users SET referrer_id = ? WHERE telegram_id = ?",
                (referrer_id, telegram_id),
            )
        if not (existing.get("web_password") or "").strip():
            new_password = await _generate_unique_web_password()
            await _execute(
                "UPDATE users SET web_password = ? WHERE telegram_id = ?",
                (new_password, telegram_id),
            )
    else:
        web_password = await _generate_unique_web_password()
        await _execute(
            """
            INSERT INTO users
            (telegram_id, username, role, created_at, last_active, is_active, referrer_id, web_password)
            VALUES (?, ?, 'User', ?, ?, 1, ?, ?)
            """,
            (telegram_id, username, now, now, referrer_id, web_password),
        )


async def get_referrals_count(user_id: int) -> int:
    await init_users_db()
    db = await get_db()
    cur = await db.execute("SELECT COUNT(1) FROM users WHERE referrer_id = ?", (user_id,))
    row = await cur.fetchone()
    await cur.close()
    return int(row[0]) if row and row[0] is not None else 0


async def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    await init_users_db()
    return await _fetchone(f"{_BASE_SELECT} WHERE telegram_id = ?", (telegram_id,))


async def get_user_by_web_password(password: str) -> Optional[Dict[str, Any]]:
    await init_users_db()
    password = (password or "").strip()
    if not password:
        return None
    return await _fetchone(f"{_BASE_SELECT} WHERE web_password = ?", (password,))


async def get_all_users() -> List[Dict[str, Any]]:
    await init_users_db()
    return await _fetchall(_BASE_SELECT)


async def set_field(user_id: int, field: str, value: Union[str, int, float, None]) -> None:
    await init_users_db()
    await _execute(f"UPDATE users SET {field} = ? WHERE telegram_id = ?", (value, user_id))


async def get_field(user_id: int, field: str) -> Optional[Any]:
    await init_users_db()
    row = await _fetchone(f"SELECT {field} FROM users WHERE telegram_id = ?", (user_id,))
    return row.get(field) if row else None


async def set_user_active(telegram_id: int, active: bool) -> None:
    await set_field(telegram_id, "is_active", 1 if active else 0)


async def is_user_active(telegram_id: int) -> bool:
    val = await get_field(telegram_id, "is_active")
    if val is None:
        return True
    return bool(val)


async def set_referral_link(telegram_id: int, link: str) -> None:
    await set_field(telegram_id, "referral_link", link)


async def set_user_btc_wallet(telegram_id: int, btc_wallet: str) -> None:
    await set_field(telegram_id, "btc_wallet", btc_wallet)


async def get_user_btc_wallet(telegram_id: int) -> Optional[str]:
    return await get_field(telegram_id, "btc_wallet")


async def delete_user_btc_wallet(telegram_id: int) -> None:
    await set_field(telegram_id, "btc_wallet", None)


async def set_user_ltc_wallet(telegram_id: int, ltc_wallet: str) -> None:
    await set_field(telegram_id, "ltc_wallet", ltc_wallet)


async def get_user_ltc_wallet(telegram_id: int) -> Optional[str]:
    return await get_field(telegram_id, "ltc_wallet")


async def delete_user_ltc_wallet(telegram_id: int) -> None:
    await set_field(telegram_id, "ltc_wallet", None)


async def set_user_usdt_trc20_wallet(telegram_id: int, wallet: str) -> None:
    await set_field(telegram_id, "usdt_trc20_wallet", wallet)


async def get_user_usdt_trc20_wallet(telegram_id: int) -> Optional[str]:
    return await get_field(telegram_id, "usdt_trc20_wallet")


async def delete_user_usdt_trc20_wallet(telegram_id: int) -> None:
    await set_field(telegram_id, "usdt_trc20_wallet", None)


async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    await init_users_db()
    db = await get_db()
    cur = await db.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def get_user_by_btc_wallet(wallet: str) -> Optional[Dict[str, Any]]:
    await init_users_db()
    db = await get_db()
    cur = await db.execute("SELECT * FROM users WHERE btc_wallet = ?", (wallet,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def set_user_commission(telegram_id: int, percent: float) -> None:
    await init_users_db()
    await _execute(
        "UPDATE users SET commission_percent = ? WHERE telegram_id = ?",
        (percent, telegram_id),
    )


async def get_user_commission(telegram_id: int) -> Optional[float]:
    await init_users_db()
    row = await _fetchone(
        "SELECT commission_percent FROM users WHERE telegram_id = ?",
        (telegram_id,),
    )
    return row.get("commission_percent") if row else None


async def set_binance_verified(telegram_id: int, verified: bool) -> None:
    await set_field(telegram_id, "binance_verified", 1 if verified else 0)


async def is_binance_verified(telegram_id: int) -> bool:
    val = await get_field(telegram_id, "binance_verified")
    return bool(val)


async def get_user_web_password(telegram_id: int) -> Optional[str]:
    password = await get_field(telegram_id, "web_password")
    if password is None:
        return None
    return str(password).strip() or None


async def ensure_user_web_password(telegram_id: int) -> str:
    await init_users_db()

    existing_password = await get_user_web_password(telegram_id)
    if existing_password:
        return existing_password

    user = await get_user(telegram_id)
    if not user:
        raise RuntimeError("Пользователь не найден")

    new_password = await _generate_unique_web_password()
    await _execute(
        "UPDATE users SET web_password = ? WHERE telegram_id = ?",
        (new_password, telegram_id),
    )
    return new_password


async def regenerate_user_web_password(telegram_id: int) -> str:
    await init_users_db()

    user = await get_user(telegram_id)
    if not user:
        raise RuntimeError("Пользователь не найден")

    new_password = await _generate_unique_web_password()
    await _execute(
        "UPDATE users SET web_password = ? WHERE telegram_id = ?",
        (new_password, telegram_id),
    )
    return new_password


async def count_users() -> int:
    await init_users_db()
    db = await get_db()
    cur = await db.execute("SELECT COUNT(1) FROM users")
    row = await cur.fetchone()
    await cur.close()
    return int(row[0]) if row and row[0] is not None else 0


async def get_admin_user_ids() -> List[int]:
    await init_users_db()
    db = await get_db()
    cur = await db.execute("SELECT telegram_id FROM users WHERE LOWER(role) = 'admin'")
    rows = await cur.fetchall()
    await cur.close()
    return [int(r[0]) for r in rows] if rows else []