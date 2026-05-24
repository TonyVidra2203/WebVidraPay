# -----------------------------------------------------------------------------
# Раздел: описание модуля
# -----------------------------------------------------------------------------
"""
Работа с таблицей withdrawals: добавление записей о выводах средств.
"""

# -----------------------------------------------------------------------------
# Раздел: импорты
# -----------------------------------------------------------------------------
from datetime import datetime, timezone

import aiosqlite
from db.connection import get_db

# -----------------------------------------------------------------------------
# Раздел: операции с выводами
# -----------------------------------------------------------------------------
async def add_withdrawal(admin_id: int, card_id: int, amount: float) -> None:
    """Сохраняет запись о выводе средств в базу данных."""
    db = await get_db()
    created_at: str = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO withdrawals (admin_id, card_id, amount, date)
        VALUES (?, ?, ?, ?)
        """,
        (admin_id, card_id, amount, created_at),
    )
    await db.commit()
