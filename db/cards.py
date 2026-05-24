"""
Модуль управления картами и выплатами: инициализация схемы, CRUD-операции,
выборки и расчёт баланса по картам.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiosqlite

from db.connection import get_db

# -----------------------------------------------------------------------------
# Раздел: SQL-константы
# -----------------------------------------------------------------------------

_CREATE_TABLE_CARDS_NEW: str = """
CREATE TABLE IF NOT EXISTS cards_new (
    card_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name    TEXT    NOT NULL,
    sbp_phone    TEXT,
    card_number  TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_TABLE_WITHDRAWALS: str = """
CREATE TABLE IF NOT EXISTS withdrawals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id   INTEGER NOT NULL,
    card_id    INTEGER NOT NULL,
    amount     REAL    NOT NULL,
    date       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# -----------------------------------------------------------------------------
# Раздел: Инициализация и миграции
# -----------------------------------------------------------------------------

async def init_cards_table() -> None:
    """
    Инициализирует таблицы `cards` и `withdrawals`.
    Если таблица `cards` отсутствует — создаёт схему заново.
    Если в старой схеме обнаружены ограничения UNIQUE — выполняет миграцию
    в новую схему без изменения данных.
    """
    db = await get_db()

    await db.execute(_CREATE_TABLE_WITHDRAWALS)

    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cards';"
    )
    row = await cursor.fetchone()

    if row is None:
        await db.execute(_CREATE_TABLE_CARDS_NEW)
        await db.execute("ALTER TABLE cards_new RENAME TO cards;")
        await db.commit()
        return

    create_sql = row[0] or ""
    if "UNIQUE" in create_sql.upper():
        await db.execute(_CREATE_TABLE_CARDS_NEW)
        await db.execute(
            """
            INSERT INTO cards_new
                (card_id, bank_name, sbp_phone, card_number, is_active, created_at, updated_at)
            SELECT
                card_id, bank_name, sbp_phone, card_number, is_active, created_at, updated_at
            FROM cards;
            """
        )
        await db.execute("ALTER TABLE cards RENAME TO cards_old;")
        await db.execute("ALTER TABLE cards_new RENAME TO cards;")
        await db.execute("DROP TABLE IF EXISTS cards_old;")
        await db.commit()

# -----------------------------------------------------------------------------
# Раздел: Выборки
# -----------------------------------------------------------------------------

async def get_all_cards() -> List[Dict[str, Any]]:
    """Возвращает список всех карт, отсортированный по card_id."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM cards ORDER BY card_id;")
    rows = await cursor.fetchall()
    return [
        {
            "card_id": r["card_id"],
            "bank_name": r["bank_name"],
            "sbp_phone": r["sbp_phone"],
            "card_number": r["card_number"],
            "is_active": bool(r["is_active"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


async def get_card_by_id(card_id: int | str) -> Optional[Dict[str, Any]]:
    """Возвращает карту по идентификатору, либо None, если не найдена."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM cards WHERE card_id = ?;", (card_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "card_id": row["card_id"],
        "bank_name": row["bank_name"],
        "sbp_phone": row["sbp_phone"],
        "card_number": row["card_number"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def get_active_cards() -> List[Dict[str, Any]]:
    """Возвращает список активных карт."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM cards WHERE COALESCE(is_active,1) = 1 ORDER BY card_id;"
    )
    rows = await cursor.fetchall()
    return [
        {
            "card_id": r["card_id"],
            "bank_name": r["bank_name"],
            "sbp_phone": r["sbp_phone"],
            "card_number": r["card_number"],
            "is_active": bool(r["is_active"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]

# -----------------------------------------------------------------------------
# Раздел: CRUD-операции по картам
# -----------------------------------------------------------------------------

async def add_card(
    bank_name: str,
    sbp_phone: Optional[str] = None,
    card_number: Optional[str] = None,
) -> None:
    """Добавляет новую карту."""
    db = await get_db()
    await db.execute(
        "INSERT INTO cards (bank_name, sbp_phone, card_number) VALUES (?, ?, ?);",
        (bank_name, sbp_phone, card_number),
    )
    await db.commit()


async def update_card(card_id: int, **fields: Any) -> None:
    """
    Обновляет указанные поля карты. Всегда обновляет `updated_at`.
    Поля передаются как именованные аргументы.
    """
    if not fields:
        raise ValueError("Не указаны поля для обновления.")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values()) + [card_id]
    db = await get_db()
    await db.execute(
        f"UPDATE cards SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE card_id = ?;",
        params,
    )
    await db.commit()


async def delete_card(card_id: int) -> None:
    """Удаляет карту по идентификатору."""
    db = await get_db()
    await db.execute("DELETE FROM cards WHERE card_id = ?;", (card_id,))
    await db.commit()

# -----------------------------------------------------------------------------
# Раздел: Операции выводов и расчёты
# -----------------------------------------------------------------------------

async def add_withdrawal(admin_id: int, card_id: int, amount: float) -> None:
    """Фиксирует вывод средств по карте."""
    db = await get_db()
    await db.execute(
        "INSERT INTO withdrawals (admin_id, card_id, amount) VALUES (?, ?, ?);",
        (admin_id, card_id, amount),
    )
    await db.commit()


async def get_card_balance(card_id: int) -> float:
    """
    Рассчитывает баланс карты как сумму зачислений по заявкам,
    привязанным к данной карте (p2p_orders.card_id), минус сумму выводов
    из таблицы withdrawals.

    Важно:
    - Учитываются только заявки со статусом 'completed'.
    - Старые заявки без card_id в расчёт не входят (баланс по ним не
      разносится по нескольким картам).
    """
    db = await get_db()

    # Сумма по завершённым P2P-заявкам, явно привязанным к этой карте
    cur1 = await db.execute(
        """
        SELECT SUM(total_rub)
        FROM p2p_orders
        WHERE card_id = ? AND status = 'completed'
        """,
        (card_id,),
    )
    row1 = await cur1.fetchone()
    p2p_sum = float(row1[0] or 0.0)

    # Сумма выводов по этой карте
    cur2 = await db.execute(
        "SELECT SUM(amount) FROM withdrawals WHERE card_id = ?;",
        (card_id,),
    )
    row2 = await cur2.fetchone()
    withdraw_sum = float(row2[0] or 0.0)

    return p2p_sum - withdraw_sum
