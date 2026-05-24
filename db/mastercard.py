from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiosqlite

from db.connection import get_db


async def init_mastercard_db() -> None:
    db = await get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mastercard_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            bank_name TEXT NOT NULL,
            sbp_phone TEXT,
            card_number TEXT,
            min_amount INTEGER NOT NULL DEFAULT 3000,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    await db.commit()


async def add_mastercard_card(
    owner_id: int,
    bank_name: str,
    sbp_phone: Optional[str],
    card_number: Optional[str],
    min_amount: int = 3000,
) -> None:
    db = await get_db()
    await db.execute(
        """
        INSERT INTO mastercard_cards
            (owner_id, bank_name, sbp_phone, card_number, min_amount)
        VALUES (?, ?, ?, ?, ?)
        """,
        (owner_id, bank_name, sbp_phone, card_number, int(min_amount)),
    )
    await db.commit()


async def get_mastercard_cards(owner_id: int) -> List[Dict[str, Any]]:
    db = await get_db()
    db.row_factory = aiosqlite.Row

    cursor = await db.execute(
        """
        SELECT *
        FROM mastercard_cards
        WHERE owner_id = ?
        ORDER BY id DESC
        """,
        (owner_id,),
    )
    rows = await cursor.fetchall()

    return [dict(row) for row in rows]


async def get_mastercard_card(card_id: int, owner_id: int) -> Optional[Dict[str, Any]]:
    db = await get_db()
    db.row_factory = aiosqlite.Row

    cursor = await db.execute(
        """
        SELECT *
        FROM mastercard_cards
        WHERE id = ? AND owner_id = ?
        LIMIT 1
        """,
        (int(card_id), int(owner_id)),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def toggle_mastercard_card(card_id: int, owner_id: int) -> bool:
    card = await get_mastercard_card(card_id, owner_id)
    if not card:
        return False

    new_value = 0 if int(card.get("is_active") or 0) else 1

    db = await get_db()
    await db.execute(
        """
        UPDATE mastercard_cards
        SET is_active = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND owner_id = ?
        """,
        (new_value, int(card_id), int(owner_id)),
    )
    await db.commit()
    return True


async def delete_mastercard_card(card_id: int, owner_id: int) -> bool:
    db = await get_db()
    cursor = await db.execute(
        """
        DELETE FROM mastercard_cards
        WHERE id = ? AND owner_id = ?
        """,
        (int(card_id), int(owner_id)),
    )
    await db.commit()
    return cursor.rowcount > 0

async def update_mastercard_card_min_amount(
    card_id: int,
    owner_id: int,
    min_amount: int,
) -> bool:
    db = await get_db()
    cursor = await db.execute(
        """
        UPDATE mastercard_cards
        SET min_amount = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND owner_id = ?
        """,
        (int(min_amount), int(card_id), int(owner_id)),
    )
    await db.commit()
    return cursor.rowcount > 0