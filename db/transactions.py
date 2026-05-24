# -----------------------------------------------------------------------------
# Раздел: описание модуля
# -----------------------------------------------------------------------------
"""
Работа с таблицей transactions: создание, добавление, получение и удаление транзакций.
"""

# -----------------------------------------------------------------------------
# Раздел: импорты
# -----------------------------------------------------------------------------
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite
from db.connection import get_db

# -----------------------------------------------------------------------------
# Раздел: SQL-запросы
# -----------------------------------------------------------------------------
_TRANSACTIONS_TABLE: str = """
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    btc_amount     REAL    NOT NULL,
    rub_amount     REAL    NOT NULL,
    total_rub      REAL    NOT NULL,
    created_at     TEXT    NOT NULL
);
"""

_INSERT_TRANSACTION: str = """
INSERT INTO transactions (user_id, btc_amount, rub_amount, total_rub, created_at)
VALUES (?, ?, ?, ?, ?);
"""

_SELECT_TRANSACTION: str = """
SELECT * FROM transactions WHERE transaction_id = ?;
"""

_SELECT_ALL_TRANSACTIONS: str = """
SELECT * FROM transactions;
"""

_DELETE_TRANSACTION: str = """
DELETE FROM transactions WHERE transaction_id = ?;
"""

# -----------------------------------------------------------------------------
# Раздел: инициализация таблицы
# -----------------------------------------------------------------------------
async def init_orders_db() -> None:
    """Создаёт таблицу transactions, если она ещё не существует."""
    db = await get_db()
    await db.execute(_TRANSACTIONS_TABLE)
    await db.commit()

# -----------------------------------------------------------------------------
# Раздел: операции записи
# -----------------------------------------------------------------------------
async def add_transaction(
    user_id: int,
    btc_amount: float,
    rub_amount: float,
    total_rub: float,
) -> None:
    """Добавляет новую транзакцию в базу данных."""
    created_at: str = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    await db.execute(
        _INSERT_TRANSACTION,
        (user_id, btc_amount, rub_amount, total_rub, created_at),
    )
    await db.commit()

# -----------------------------------------------------------------------------
# Раздел: операции чтения
# -----------------------------------------------------------------------------
async def get_all_transactions() -> List[Dict[str, Any]]:
    """Возвращает список всех транзакций."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cur = await db.execute(_SELECT_ALL_TRANSACTIONS)
    rows = await cur.fetchall()
    await cur.close()
    return [dict(row) for row in rows]


async def get_transaction(transaction_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает транзакцию по её ID, если она существует."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cur = await db.execute(_SELECT_TRANSACTION, (transaction_id,))
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None

# -----------------------------------------------------------------------------
# Раздел: операции удаления
# -----------------------------------------------------------------------------
async def delete_transaction(transaction_id: int) -> None:
    """Удаляет транзакцию по её ID."""
    db = await get_db()
    await db.execute(_DELETE_TRANSACTION, (transaction_id,))
    await db.commit()
