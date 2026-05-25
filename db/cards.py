"""
Модуль управления картами и выплатами: инициализация схемы, CRUD-операции,
выборки и расчёт баланса по картам.

Логика MasterCard:
- карта может принадлежать пользователю через owner_id;
- при создании MasterCard-карт можно задавать ограничения:
  min_amount_rub, max_amount_rub, daily_limit_rub,
  daily_transfer_limit, transfer_pause_minutes;
- старые админские карты остаются совместимыми: owner_id может быть NULL.
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
    card_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id                INTEGER,
    bank_name               TEXT    NOT NULL,
    sbp_phone               TEXT,
    card_number             TEXT,
    min_amount_rub          REAL,
    max_amount_rub          REAL,
    daily_limit_rub         REAL,
    daily_transfer_limit    INTEGER,
    transfer_pause_minutes  INTEGER,
    is_active               INTEGER NOT NULL DEFAULT 1,
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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

_CARD_FIELDS: set[str] = {
    "owner_id",
    "bank_name",
    "sbp_phone",
    "card_number",
    "min_amount_rub",
    "max_amount_rub",
    "daily_limit_rub",
    "daily_transfer_limit",
    "transfer_pause_minutes",
    "is_active",
}


# -----------------------------------------------------------------------------
# Раздел: Внутренние helpers
# -----------------------------------------------------------------------------

def _safe_col(row: aiosqlite.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _card_row_to_dict(row: aiosqlite.Row) -> Dict[str, Any]:
    """Преобразует строку cards в обычный dict с единым набором ключей."""
    return {
        "card_id": row["card_id"],
        "owner_id": _safe_col(row, "owner_id"),
        "bank_name": row["bank_name"],
        "sbp_phone": row["sbp_phone"],
        "card_number": row["card_number"],
        "min_amount_rub": _safe_col(row, "min_amount_rub"),
        "max_amount_rub": _safe_col(row, "max_amount_rub"),
        "daily_limit_rub": _safe_col(row, "daily_limit_rub"),
        "daily_transfer_limit": _safe_col(row, "daily_transfer_limit"),
        "transfer_pause_minutes": _safe_col(row, "transfer_pause_minutes"),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _get_card_columns(db: aiosqlite.Connection) -> set[str]:
    """Возвращает набор колонок текущей таблицы cards."""
    cursor = await db.execute("PRAGMA table_info(cards);")
    rows = await cursor.fetchall()
    await cursor.close()
    return {row[1] for row in rows}


async def _add_missing_card_columns(db: aiosqlite.Connection) -> None:
    """
    Мягкая миграция существующей таблицы cards.

    SQLite поддерживает ADD COLUMN, поэтому данные старых карт сохраняются.
    """
    columns = await _get_card_columns(db)

    migrations: list[tuple[str, str]] = [
        ("owner_id", "ALTER TABLE cards ADD COLUMN owner_id INTEGER;"),
        ("min_amount_rub", "ALTER TABLE cards ADD COLUMN min_amount_rub REAL;"),
        ("max_amount_rub", "ALTER TABLE cards ADD COLUMN max_amount_rub REAL;"),
        ("daily_limit_rub", "ALTER TABLE cards ADD COLUMN daily_limit_rub REAL;"),
        ("daily_transfer_limit", "ALTER TABLE cards ADD COLUMN daily_transfer_limit INTEGER;"),
        ("transfer_pause_minutes", "ALTER TABLE cards ADD COLUMN transfer_pause_minutes INTEGER;"),
    ]

    changed = False
    for column_name, sql in migrations:
        if column_name not in columns:
            await db.execute(sql)
            changed = True

    if changed:
        await db.commit()


# -----------------------------------------------------------------------------
# Раздел: Инициализация и миграции
# -----------------------------------------------------------------------------

async def init_cards_table() -> None:
    """
    Инициализирует таблицы `cards` и `withdrawals`.

    Если таблица `cards` отсутствует — создаёт актуальную схему.
    Если в старой схеме обнаружены UNIQUE-ограничения — переносит данные
    в новую таблицу без UNIQUE.
    Если таблица уже существует — добавляет недостающие поля.
    """
    db = await get_db()

    await db.execute(_CREATE_TABLE_WITHDRAWALS)

    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cards';"
    )
    row = await cursor.fetchone()
    await cursor.close()

    if row is None:
        await db.execute(_CREATE_TABLE_CARDS_NEW)
        await db.execute("ALTER TABLE cards_new RENAME TO cards;")
        await db.commit()
        return

    create_sql = row[0] or ""
    if "UNIQUE" in create_sql.upper():
        await db.execute(_CREATE_TABLE_CARDS_NEW)

        old_columns = await _get_card_columns(db)
        source_columns = [
            "card_id",
            "owner_id",
            "bank_name",
            "sbp_phone",
            "card_number",
            "min_amount_rub",
            "max_amount_rub",
            "daily_limit_rub",
            "daily_transfer_limit",
            "transfer_pause_minutes",
            "is_active",
            "created_at",
            "updated_at",
        ]

        available_source_columns = [col for col in source_columns if col in old_columns]
        insert_columns = ", ".join(available_source_columns)
        select_columns = ", ".join(available_source_columns)

        await db.execute(
            f"""
            INSERT INTO cards_new ({insert_columns})
            SELECT {select_columns}
            FROM cards;
            """
        )
        await db.execute("ALTER TABLE cards RENAME TO cards_old;")
        await db.execute("ALTER TABLE cards_new RENAME TO cards;")
        await db.execute("DROP TABLE IF EXISTS cards_old;")
        await db.commit()

    await _add_missing_card_columns(db)


# -----------------------------------------------------------------------------
# Раздел: Выборки
# -----------------------------------------------------------------------------

async def get_all_cards(owner_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Возвращает список всех карт, отсортированный по card_id.

    Если передан owner_id — возвращает только карты конкретного Mastercard-пользователя.
    """
    db = await get_db()
    db.row_factory = aiosqlite.Row
    await _add_missing_card_columns(db)

    if owner_id is None:
        cursor = await db.execute("SELECT * FROM cards ORDER BY card_id;")
    else:
        cursor = await db.execute(
            "SELECT * FROM cards WHERE owner_id = ? ORDER BY card_id;",
            (owner_id,),
        )

    rows = await cursor.fetchall()
    await cursor.close()
    return [_card_row_to_dict(r) for r in rows]


async def get_cards_by_owner(owner_id: int) -> List[Dict[str, Any]]:
    """Возвращает все карты конкретного Mastercard-пользователя."""
    return await get_all_cards(owner_id=owner_id)


async def get_card_by_id(card_id: int | str) -> Optional[Dict[str, Any]]:
    """Возвращает карту по идентификатору, либо None, если не найдена."""
    db = await get_db()
    db.row_factory = aiosqlite.Row
    await _add_missing_card_columns(db)

    cursor = await db.execute("SELECT * FROM cards WHERE card_id = ?;", (card_id,))
    row = await cursor.fetchone()
    await cursor.close()

    if not row:
        return None

    return _card_row_to_dict(row)


async def get_active_cards(owner_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Возвращает список активных карт.

    Если owner_id не передан — возвращает все активные карты.
    Если owner_id передан — только активные карты конкретного владельца.
    """
    db = await get_db()
    db.row_factory = aiosqlite.Row
    await _add_missing_card_columns(db)

    if owner_id is None:
        cursor = await db.execute(
            "SELECT * FROM cards WHERE COALESCE(is_active, 1) = 1 ORDER BY card_id;"
        )
    else:
        cursor = await db.execute(
            """
            SELECT *
            FROM cards
            WHERE owner_id = ? AND COALESCE(is_active, 1) = 1
            ORDER BY card_id;
            """,
            (owner_id,),
        )

    rows = await cursor.fetchall()
    await cursor.close()
    return [_card_row_to_dict(r) for r in rows]


async def get_active_cards_for_amount(
    amount_rub: float,
    owner_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Возвращает активные карты, подходящие под сумму платежа.

    Фильтры:
    - min_amount_rub: если заполнен, сумма должна быть >= min_amount_rub;
    - max_amount_rub: если заполнен, сумма должна быть <= max_amount_rub.
    """
    db = await get_db()
    db.row_factory = aiosqlite.Row
    await _add_missing_card_columns(db)

    params: list[Any] = [float(amount_rub), float(amount_rub)]
    owner_filter = ""

    if owner_id is not None:
        owner_filter = "AND owner_id = ?"
        params.append(owner_id)

    cursor = await db.execute(
        f"""
        SELECT *
        FROM cards
        WHERE COALESCE(is_active, 1) = 1
          AND (min_amount_rub IS NULL OR min_amount_rub <= ?)
          AND (max_amount_rub IS NULL OR max_amount_rub >= ?)
          {owner_filter}
        ORDER BY card_id;
        """,
        params,
    )

    rows = await cursor.fetchall()
    await cursor.close()
    return [_card_row_to_dict(r) for r in rows]


# -----------------------------------------------------------------------------
# Раздел: CRUD-операции по картам
# -----------------------------------------------------------------------------

async def add_card(
    bank_name: str,
    sbp_phone: Optional[str] = None,
    card_number: Optional[str] = None,
    owner_id: Optional[int] = None,
    min_amount_rub: Optional[float] = None,
    max_amount_rub: Optional[float] = None,
    daily_limit_rub: Optional[float] = None,
    daily_transfer_limit: Optional[int] = None,
    transfer_pause_minutes: Optional[int] = None,
    monthly_limit_rub: Optional[float] = None,
) -> int:
    """
    Добавляет новую карту и возвращает её card_id.

    monthly_limit_rub оставлен в сигнатуре только для совместимости со старыми вызовами.
    В новую схему он не записывается.
    """
    db = await get_db()
    await _add_missing_card_columns(db)

    cursor = await db.execute(
        """
        INSERT INTO cards (
            owner_id,
            bank_name,
            sbp_phone,
            card_number,
            min_amount_rub,
            max_amount_rub,
            daily_limit_rub,
            daily_transfer_limit,
            transfer_pause_minutes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            owner_id,
            bank_name,
            sbp_phone,
            card_number,
            min_amount_rub,
            max_amount_rub,
            daily_limit_rub,
            daily_transfer_limit,
            transfer_pause_minutes,
        ),
    )
    await db.commit()
    return int(cursor.lastrowid)


async def update_card(card_id: int, **fields: Any) -> None:
    """
    Обновляет указанные поля карты. Всегда обновляет `updated_at`.

    Защита:
    - неизвестные поля не принимаются;
    - SQL собирается только из whitelist-полей.
    """
    if not fields:
        raise ValueError("Не указаны поля для обновления.")

    # Совместимость: если старый код передал monthly_limit_rub — игнорируем,
    # потому что месячный лимит больше не используется.
    fields.pop("monthly_limit_rub", None)

    if not fields:
        return

    unknown_fields = set(fields) - _CARD_FIELDS
    if unknown_fields:
        raise ValueError(f"Недопустимые поля карты: {', '.join(sorted(unknown_fields))}")

    set_clause = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values()) + [card_id]

    db = await get_db()
    await _add_missing_card_columns(db)

    await db.execute(
        f"""
        UPDATE cards
        SET {set_clause}, updated_at = CURRENT_TIMESTAMP
        WHERE card_id = ?;
        """,
        params,
    )
    await db.commit()


async def set_card_active(
    card_id: int,
    is_active: bool,
    owner_id: Optional[int] = None,
) -> None:
    """
    Включает или выключает карту.

    Если owner_id передан — меняет только карту этого владельца.
    """
    db = await get_db()

    if owner_id is None:
        await db.execute(
            """
            UPDATE cards
            SET is_active = ?, updated_at = CURRENT_TIMESTAMP
            WHERE card_id = ?;
            """,
            (1 if is_active else 0, card_id),
        )
    else:
        await db.execute(
            """
            UPDATE cards
            SET is_active = ?, updated_at = CURRENT_TIMESTAMP
            WHERE card_id = ? AND owner_id = ?;
            """,
            (1 if is_active else 0, card_id, owner_id),
        )

    await db.commit()


async def delete_card(card_id: int, owner_id: Optional[int] = None) -> None:
    """
    Удаляет карту по идентификатору.

    Если owner_id передан — удаляет карту только у этого владельца.
    """
    db = await get_db()

    if owner_id is None:
        await db.execute("DELETE FROM cards WHERE card_id = ?;", (card_id,))
    else:
        await db.execute(
            "DELETE FROM cards WHERE card_id = ? AND owner_id = ?;",
            (card_id, owner_id),
        )

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
    - Старые заявки без card_id в расчёт не входят.
    """
    db = await get_db()

    cur1 = await db.execute(
        """
        SELECT SUM(total_rub)
        FROM p2p_orders
        WHERE card_id = ? AND status = 'completed'
        """,
        (card_id,),
    )
    row1 = await cur1.fetchone()
    await cur1.close()
    p2p_sum = float(row1[0] or 0.0)

    cur2 = await db.execute(
        "SELECT SUM(amount) FROM withdrawals WHERE card_id = ?;",
        (card_id,),
    )
    row2 = await cur2.fetchone()
    await cur2.close()
    withdraw_sum = float(row2[0] or 0.0)

    return p2p_sum - withdraw_sum
