"""
Модуль учёта расходов: создание, получение и удаление записей.
"""

from __future__ import annotations

from typing import Any, Dict, List

import aiosqlite

from db.connection import get_db

# -----------------------------------------------------------------------------
# Раздел: Выборки
# -----------------------------------------------------------------------------

async def get_expenses() -> List[Dict[str, Any]]:
    """Возвращает список всех расходов, отсортированных по убыванию ID."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT id, title, amount FROM expenses ORDER BY id DESC"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]

# -----------------------------------------------------------------------------
# Раздел: Запись данных
# -----------------------------------------------------------------------------

async def add_expense(title: str, amount: float) -> None:
    """Добавляет новую запись о расходе."""
    db = await get_db()
    await db.execute(
        "INSERT INTO expenses (title, amount) VALUES (?, ?);",
        (title, amount),
    )
    await db.commit()

# -----------------------------------------------------------------------------
# Раздел: Удаление данных
# -----------------------------------------------------------------------------

async def delete_expense(expense_id: int) -> None:
    """Удаляет запись о расходе по ID."""
    db = await get_db()
    await db.execute("DELETE FROM expenses WHERE id = ?;", (expense_id,))
    await db.commit()
