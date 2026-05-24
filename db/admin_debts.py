from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any, Tuple

import aiosqlite

from db.connection import get_db

DATETIME_FORMAT: str = "%d.%m.%Y %H:%M"


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime(DATETIME_FORMAT)


_CREATE_ADMIN_DEBTS_TABLE: str = """
CREATE TABLE IF NOT EXISTS admin_debts (
    admin_id     INTEGER PRIMARY KEY,
    debt_kopeks  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);
"""


def rub_to_kopeks(amount_rub: Decimal) -> int:
    """
    Храним деньги в копейках (INTEGER), чтобы избежать float-ошибок.
    amount_rub: Decimal("12500.50") -> 1250050
    """
    if amount_rub is None:
        return 0
    a = Decimal(str(amount_rub)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return int((a * 100).to_integral_value(rounding=ROUND_DOWN))


def kopeks_to_rub(amount_kopeks: int) -> Decimal:
    try:
        k = int(amount_kopeks or 0)
    except Exception:
        k = 0
    return (Decimal(k) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


async def init_admin_debts_db() -> None:
    db = await get_db()
    await db.execute(_CREATE_ADMIN_DEBTS_TABLE)
    await db.commit()


async def _fetchone(query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    db = await get_db()
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    await cur.close()
    return dict(row) if row else None


async def get_admin_debt_kopeks(admin_id: int) -> int:
    await init_admin_debts_db()
    row = await _fetchone("SELECT debt_kopeks FROM admin_debts WHERE admin_id = ?", (int(admin_id),))
    if not row:
        return 0
    try:
        return int(row.get("debt_kopeks") or 0)
    except Exception:
        return 0


async def set_admin_debt_kopeks(admin_id: int, debt_kopeks: int) -> int:
    """
    Устанавливает долг (в копейках). Возвращает фактически сохранённое значение (не < 0).
    """
    await init_admin_debts_db()
    now = _now_str()
    val = int(debt_kopeks or 0)
    if val < 0:
        val = 0

    db = await get_db()
    await db.execute(
        """
        INSERT INTO admin_debts (admin_id, debt_kopeks, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(admin_id) DO UPDATE SET
            debt_kopeks = excluded.debt_kopeks,
            updated_at  = excluded.updated_at
        """,
        (int(admin_id), int(val), now, now),
    )
    await db.commit()
    return val


async def add_admin_debt_kopeks(admin_id: int, delta_kopeks: int) -> int:
    """
    Прибавляет/убавляет долг. Результат не уходит ниже 0.
    Возвращает новый долг.
    """
    await init_admin_debts_db()

    delta = int(delta_kopeks or 0)
    db = await get_db()
    now = _now_str()

    # транзакция, чтобы два параллельных начисления не сломали долг
    await db.execute("BEGIN IMMEDIATE")
    try:
        cur = await db.execute("SELECT debt_kopeks FROM admin_debts WHERE admin_id = ?", (int(admin_id),))
        row = await cur.fetchone()
        await cur.close()

        current = int(row[0]) if row and row[0] is not None else 0
        new_val = current + delta
        if new_val < 0:
            new_val = 0

        await db.execute(
            """
            INSERT INTO admin_debts (admin_id, debt_kopeks, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(admin_id) DO UPDATE SET
                debt_kopeks = excluded.debt_kopeks,
                updated_at  = excluded.updated_at
            """,
            (int(admin_id), int(new_val), now, now),
        )
        await db.commit()
        return int(new_val)
    except Exception:
        await db.rollback()
        raise


async def apply_profit_to_debt_kopeks(admin_id: int, profit_kopeks: int) -> Tuple[int, int, int]:
    """
    Ключевая функция для выплат:
    - Если долг есть, "съедаем" прибыль в погашение долга.
    Возвращает кортеж:
      (paid_to_debt_kopeks, remaining_profit_kopeks, new_debt_kopeks)
    """
    await init_admin_debts_db()

    profit = int(profit_kopeks or 0)
    if profit <= 0:
        debt = await get_admin_debt_kopeks(admin_id)
        return 0, 0, debt

    db = await get_db()
    now = _now_str()

    await db.execute("BEGIN IMMEDIATE")
    try:
        cur = await db.execute("SELECT debt_kopeks FROM admin_debts WHERE admin_id = ?", (int(admin_id),))
        row = await cur.fetchone()
        await cur.close()

        debt = int(row[0]) if row and row[0] is not None else 0

        if debt <= 0:
            await db.commit()
            return 0, profit, 0

        paid = min(profit, debt)
        new_debt = debt - paid
        remaining = profit - paid

        await db.execute(
            """
            INSERT INTO admin_debts (admin_id, debt_kopeks, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(admin_id) DO UPDATE SET
                debt_kopeks = excluded.debt_kopeks,
                updated_at  = excluded.updated_at
            """,
            (int(admin_id), int(new_debt), now, now),
        )
        await db.commit()
        return int(paid), int(remaining), int(new_debt)
    except Exception:
        await db.rollback()
        raise
