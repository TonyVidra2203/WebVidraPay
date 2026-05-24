# -----------------------------------------------------------------------------
# Раздел: описание модуля
# -----------------------------------------------------------------------------
"""
Работа с таблицей sms_events: инициализация, вставка и выборка событий SMS.
"""

# -----------------------------------------------------------------------------
# Раздел: импорты
# -----------------------------------------------------------------------------
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

from db.connection import get_db

# -----------------------------------------------------------------------------
# Раздел: SQL-схема
# -----------------------------------------------------------------------------
_CREATE_SMS_EVENTS: str = """
CREATE TABLE IF NOT EXISTS sms_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_hash    TEXT    NOT NULL UNIQUE,
    received_at   TEXT    NOT NULL,
    sender        TEXT,
    body          TEXT    NOT NULL,
    card_last4    TEXT,
    amount_rub    REAL,
    user_id       INTEGER,
    parsed_ok     INTEGER NOT NULL DEFAULT 0
);
"""

# -----------------------------------------------------------------------------
# Раздел: инициализация таблицы
# -----------------------------------------------------------------------------
async def init_sms_events_db() -> None:
    """Создаёт таблицу sms_events, если она ещё не существует."""
    db = await get_db()
    await db.execute(_CREATE_SMS_EVENTS)
    await db.commit()

# -----------------------------------------------------------------------------
# Раздел: операции записи
# -----------------------------------------------------------------------------
async def insert_sms_event(
    *,
    event_hash: str,
    sender: Optional[str],
    body: str,
    card_last4: Optional[str],
    amount_rub: Optional[float],
    user_id: Optional[int],
    parsed_ok: bool,
) -> None:
    """
    Добавляет SMS-событие в базу данных.

    Использует INSERT OR IGNORE, чтобы избежать дублирования по event_hash.
    """
    db = await get_db()
    await db.execute(
        """
        INSERT OR IGNORE INTO sms_events
        (event_hash, received_at, sender, body, card_last4, amount_rub, user_id, parsed_ok)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_hash,
            datetime.now(timezone.utc).isoformat(),
            sender,
            body,
            card_last4,
            amount_rub,
            user_id,
            int(parsed_ok),
        ),
    )
    await db.commit()

# -----------------------------------------------------------------------------
# Раздел: операции чтения
# -----------------------------------------------------------------------------
async def list_sms_events(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Возвращает последние SMS-события для указанного пользователя.

    Параметры:
        user_id — ID пользователя.
        limit — максимальное количество записей (по умолчанию 10).
    """
    db = await get_db()
    cur = await db.execute(
        """
        SELECT received_at, amount_rub, card_last4
        FROM sms_events
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]
