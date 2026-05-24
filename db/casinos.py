from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from db.connection import get_db


def _slugify(name: str) -> str:
    value = (name or "").strip().lower()
    value = value.replace("ё", "е")
    value = re.sub(r"[^a-zA-Z0-9а-яА-Я]+", "_", value, flags=re.UNICODE)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "casino"


async def init_casinos_table() -> None:
    db = await get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS casinos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            casino_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            url TEXT,
            telegram TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.commit()


async def list_casinos() -> List[Dict[str, Any]]:
    db = await get_db()
    cur = await db.execute(
        """
        SELECT id, casino_key, name, url, telegram, created_at
        FROM casinos
        ORDER BY name COLLATE NOCASE ASC
        """
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_casino_by_key(casino_key: str) -> Optional[Dict[str, Any]]:
    db = await get_db()
    cur = await db.execute(
        """
        SELECT id, casino_key, name, url, telegram, created_at
        FROM casinos
        WHERE casino_key = ?
        LIMIT 1
        """,
        (casino_key,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def add_casino(name: str, url: str, telegram: str) -> str:
    db = await get_db()

    base_key = _slugify(name)
    casino_key = base_key
    suffix = 2

    while await get_casino_by_key(casino_key):
        casino_key = f"{base_key}_{suffix}"
        suffix += 1

    await db.execute(
        """
        INSERT INTO casinos (casino_key, name, url, telegram)
        VALUES (?, ?, ?, ?)
        """,
        (
            casino_key,
            (name or "").strip(),
            (url or "").strip(),
            (telegram or "").strip(),
        ),
    )
    await db.commit()
    return casino_key


async def delete_casino(casino_key: str) -> bool:
    db = await get_db()
    cur = await db.execute(
        "DELETE FROM casinos WHERE casino_key = ?",
        (casino_key,),
    )
    await db.commit()
    return (cur.rowcount or 0) > 0


async def ensure_default_casinos(defaults: Dict[str, Dict[str, str]]) -> None:
    await init_casinos_table()

    existing = await list_casinos()
    if existing:
        return

    db = await get_db()
    for casino_key, item in (defaults or {}).items():
        await db.execute(
            """
            INSERT OR IGNORE INTO casinos (casino_key, name, url, telegram)
            VALUES (?, ?, ?, ?)
            """,
            (
                casino_key,
                (item.get("name") or "").strip(),
                (item.get("url") or "").strip(),
                (item.get("telegram") or "").strip(),
            ),
        )
    await db.commit()