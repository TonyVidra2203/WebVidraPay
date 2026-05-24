"""
Единая таблица брелоков, привязанных к пользователям.

Объединяет:
- состояние брелока (номер, банк, кошелёк, статус, PIN, total_topup);
- историю пополнений (аналог brelok_history) в виде JSON-массива;
- легаси-поля из users (последние 4 цифры, старый BTC-адрес).

История хранится в поле history_json:

[
  {
    "rub_amount": int,
    "btc_amount": float,
    "txid": str | null,
    "created_at": "YYYY-MM-DD HH:MM:SS"
  },
  ...
]
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional, Literal

from .connection import get_db

# -----------------------------------------------------------------------------
# Типы и константы
# -----------------------------------------------------------------------------

Status = Literal["active", "inactive"]


def _now_str_utc() -> str:
    """Возвращает текущее время в UTC строкой YYYY-MM-DD HH:MM:SS."""
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def _safe_load_history(raw: Optional[str]) -> List[Dict[str, Any]]:
    """Разбирает JSON-историю, при ошибке возвращает пустой список."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data  # type: ignore[return-value]
    except Exception:
        pass
    return []


def _dump_history(history: List[Dict[str, Any]]) -> str:
    """Сериализует историю в JSON."""
    return json.dumps(history, ensure_ascii=False)


# -----------------------------------------------------------------------------
# SQL-схема
# -----------------------------------------------------------------------------

CREATE_SQL: str = """
CREATE TABLE IF NOT EXISTS user_breloks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    number             INTEGER UNIQUE,
    owner_telegram_id  INTEGER UNIQUE,
    bank               TEXT,
    wallet             TEXT,
    wallet_asset       TEXT,  -- << НОВОЕ
    wallet_network     TEXT,  -- << НОВОЕ
    status             TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    total_topup        INTEGER NOT NULL DEFAULT 0,
    pin_code_hash      TEXT,
    last4_hint         TEXT,
    legacy_btc_address TEXT,
    history_json       TEXT    NOT NULL DEFAULT '[]',
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    CHECK (total_topup >= 0)
);
"""


INDEXES_SQL: List[str] = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_breloks_number ON user_breloks(number);",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_breloks_owner "
        "ON user_breloks(owner_telegram_id);"
    ),
    "CREATE INDEX IF NOT EXISTS ix_user_breloks_status ON user_breloks(status);",
]


# -----------------------------------------------------------------------------
# Инициализация таблицы
# -----------------------------------------------------------------------------

async def ensure_table() -> None:
    """Создаёт таблицу user_breloks и индексы при необходимости."""
    db = await get_db()
    await db.execute(CREATE_SQL)

    # Лёгкая миграция: добавляем новые колонки, если их ещё нет
    cur = await db.execute("PRAGMA table_info(user_breloks)")
    cols = [r["name"] for r in await cur.fetchall()]
    await cur.close()

    if "wallet_asset" not in cols:
        await db.execute("ALTER TABLE user_breloks ADD COLUMN wallet_asset TEXT")
    if "wallet_network" not in cols:
        await db.execute("ALTER TABLE user_breloks ADD COLUMN wallet_network TEXT")

    for sql in INDEXES_SQL:
        await db.execute(sql)
    await db.commit()



# -----------------------------------------------------------------------------
# Базовые выборки
# -----------------------------------------------------------------------------

async def get_by_owner(owner_telegram_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает брелок по Telegram ID владельца."""
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM user_breloks WHERE owner_telegram_id = ?",
        (owner_telegram_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_by_number(number: int) -> Optional[Dict[str, Any]]:
    """Возвращает брелок по номеру."""
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM user_breloks WHERE number = ?",
        (number,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_all(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Возвращает список брелоков с пагинацией по номеру."""
    db = await get_db()
    cur = await db.execute(
        """
        SELECT * FROM user_breloks
        ORDER BY number
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


# -----------------------------------------------------------------------------
# UPSERT / CRUD по номеру
# -----------------------------------------------------------------------------

async def upsert_brelok(
    number: int,
    bank: Optional[str],
    wallet: Optional[str],
    owner_telegram_id: Optional[int] = None,
    status: Optional[Status] = None,
) -> int:
    """
    Создаёт или обновляет брелок по номеру.

    - Если запись существует, обновляет только переданные поля (bank/wallet/owner/status).
    - Если записи нет, создаёт новую (total_topup=0, history_json='[]').
    """
    db = await get_db()
    now = _now_str_utc()

    # Проверяем, есть ли запись с таким номером
    cur = await db.execute("SELECT id FROM user_breloks WHERE number = ?", (number,))
    row = await cur.fetchone()

    if row:
        sets: List[str] = ["updated_at = ?"]
        params: List[Any] = [now]

        if bank is not None:
            sets.append("bank = ?")
            params.append(bank)
        if wallet is not None:
            sets.append("wallet = ?")
            params.append(wallet)
        if owner_telegram_id is not None:
            # Обеспечиваем уникальность: сначала отвяжем этот ID от других брелоков
            await db.execute(
                "UPDATE user_breloks SET owner_telegram_id = NULL WHERE owner_telegram_id = ?",
                (owner_telegram_id,),
            )
            sets.append("owner_telegram_id = ?")
            params.append(owner_telegram_id)
        if status is not None:
            if status not in ("active", "inactive"):
                raise ValueError("status must be 'active' or 'inactive'")
            sets.append("status = ?")
            params.append(status)

        params.append(number)
        await db.execute(
            f"UPDATE user_breloks SET {', '.join(sets)} WHERE number = ?",
            params,
        )
        await db.commit()
        return int(row["id"])

    # Вставка новой записи
    if status is None:
        status = "active"

    last4_hint = str(number)[-4:] if number is not None else None

    cur = await db.execute(
        """
        INSERT INTO user_breloks (
            number, owner_telegram_id, bank, wallet, status,
            total_topup, pin_code_hash, last4_hint, legacy_btc_address,
            history_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, NULL, ?, NULL, '[]', ?, ?)
        """,
        (number, owner_telegram_id, bank, wallet, status, last4_hint, now, now),
    )
    await db.commit()
    return int(cur.lastrowid)


async def assign_to_user(number: int, owner_telegram_id: int) -> None:
    """
    Привязывает брелок с указанным номером к пользователю.
    Гарантирует, что у пользователя будет не больше одного брелока.
    """
    db = await get_db()
    now = _now_str_utc()

    # Сначала отвязываем этот ID от всех других брелоков
    await db.execute(
        "UPDATE user_breloks SET owner_telegram_id = NULL WHERE owner_telegram_id = ?",
        (owner_telegram_id,),
    )
    # Потом привязываем к нужному номеру
    await db.execute(
        """
        UPDATE user_breloks
           SET owner_telegram_id = ?, updated_at = ?
         WHERE number = ?
        """,
        (owner_telegram_id, now, number),
    )
    await db.commit()


async def set_status_by_number(number: int, status: Status) -> None:
    """Устанавливает статус ('active' | 'inactive') по номеру брелока."""
    if status not in ("active", "inactive"):
        raise ValueError("status must be 'active' or 'inactive'")
    db = await get_db()
    now = _now_str_utc()
    await db.execute(
        """
        UPDATE user_breloks
           SET status = ?, updated_at = ?
         WHERE number = ?
        """,
        (status, now, number),
    )
    await db.commit()


async def set_pin_hash_by_number(number: int, pin_code: str) -> None:
    """
    Устанавливает/обновляет PIN по номеру брелока.

    ВАЖНО: PIN сохраняется в открытом виде (без хэширования).
    """
    db = await get_db()
    now = _now_str_utc()
    await db.execute(
        """
        UPDATE user_breloks
           SET pin_code_hash = ?, updated_at = ?
         WHERE number = ?
        """,
        (pin_code, now, number),
    )
    await db.commit()


async def delete_by_number(number: int) -> None:
    """Удаляет брелок по номеру."""
    db = await get_db()
    await db.execute("DELETE FROM user_breloks WHERE number = ?", (number,))
    await db.commit()


# -----------------------------------------------------------------------------
# Обновление по владельцу (для user-хендлеров)
# -----------------------------------------------------------------------------

async def update_wallet(owner_telegram_id: int, wallet: str) -> None:
    """Обновляет BTC-кошелёк по ID владельца (легаси-вариант)."""
    await update_wallet_with_asset(
        owner_telegram_id=owner_telegram_id,
        wallet=wallet,
        wallet_asset="BTC",
        wallet_network="btc",
    )



async def update_wallet_with_asset(
    owner_telegram_id: int,
    wallet: str,
    wallet_asset: str,
    wallet_network: Optional[str] = None,
) -> None:
    """
    Обновляет кошелёк и тип монеты/сети по ID владельца.

    Примеры:
        wallet_asset: "BTC" | "LTC" | "USDT" | "TON"
        wallet_network: "btc" | "ltc" | "trc20" | "ton"
    """
    db = await get_db()
    now = _now_str_utc()
    await db.execute(
        """
        UPDATE user_breloks
           SET wallet = ?, wallet_asset = ?, wallet_network = ?, updated_at = ?
         WHERE owner_telegram_id = ?
        """,
        (wallet, wallet_asset.upper(), wallet_network, now, owner_telegram_id),
    )
    await db.commit()


async def set_total_topup(owner_telegram_id: int, total_topup: int) -> None:
    """Принудительно задаёт суммарное пополнение (₽) для пользователя."""
    if total_topup < 0:
        raise ValueError("total_topup must be >= 0")
    db = await get_db()
    now = _now_str_utc()
    await db.execute(
        """
        UPDATE user_breloks
           SET total_topup = ?, updated_at = ?
         WHERE owner_telegram_id = ?
        """,
        (int(total_topup), now, owner_telegram_id),
    )
    await db.commit()


async def add_total_topup(owner_telegram_id: int, delta_rub: int) -> None:
    """Увеличивает сумму пополнений (₽) для пользователя."""
    if delta_rub < 0:
        raise ValueError("delta_rub must be >= 0")
    db = await get_db()
    now = _now_str_utc()
    await db.execute(
        """
        UPDATE user_breloks
           SET total_topup = total_topup + ?, updated_at = ?
         WHERE owner_telegram_id = ?
        """,
        (int(delta_rub), now, owner_telegram_id),
    )
    await db.commit()


# -----------------------------------------------------------------------------
# Работа с PIN по владельцу (используется в handlers/brelok.py)
# -----------------------------------------------------------------------------

async def set_pin_hash(owner_telegram_id: int, pin_code: str) -> None:
    """
    Устанавливает/обновляет PIN для пользователя.

    ВАЖНО: PIN сохраняется в открытом виде (без хэширования).
    """
    db = await get_db()
    now = _now_str_utc()
    await db.execute(
        """
        UPDATE user_breloks
           SET pin_code_hash = ?, updated_at = ?
         WHERE owner_telegram_id = ?
        """,
        (pin_code, now, owner_telegram_id),
    )
    await db.commit()


async def clear_pin(owner_telegram_id: int) -> None:
    """Удаляет PIN (обнуляет хэш) по ID владельца."""
    db = await get_db()
    now = _now_str_utc()
    await db.execute(
        """
        UPDATE user_breloks
           SET pin_code_hash = NULL, updated_at = ?
         WHERE owner_telegram_id = ?
        """,
        (now, owner_telegram_id),
    )
    await db.commit()


async def get_pin_hash(owner_telegram_id: int) -> Optional[str]:
    """
    Возвращает PIN по ID владельца.

    На данный момент в поле pin_code_hash хранится сам PIN в открытом виде
    (хэш больше не используется).
    """
    db = await get_db()
    cur = await db.execute(
        "SELECT pin_code_hash FROM user_breloks WHERE owner_telegram_id = ?",
        (owner_telegram_id,),
    )
    row = await cur.fetchone()
    return row["pin_code_hash"] if row and row["pin_code_hash"] is not None else None


# -----------------------------------------------------------------------------
# История пополнений (замена brelok_history для новой схемы)
# -----------------------------------------------------------------------------

async def add_history_record(
    owner_telegram_id: int,
    *,
    rub_amount: int,
    btc_amount: float,
    txid: Optional[str] = None,
) -> None:
    """
    Добавляет запись об операции пополнения:

    - увеличивает total_topup на rub_amount;
    - дописывает объект в history_json.
    """
    if rub_amount < 0:
        raise ValueError("rub_amount must be >= 0")

    db = await get_db()
    cur = await db.execute(
        "SELECT history_json, total_topup FROM user_breloks WHERE owner_telegram_id = ?",
        (owner_telegram_id,),
    )
    row = await cur.fetchone()
    if not row:
        # Нет записи брелока для пользователя — считаем, что пополнение некуда писать.
        return

    history = _safe_load_history(row["history_json"])
    current_total = int(row["total_topup"] or 0)

    now = _now_str_utc()
    history.append(
        {
            "rub_amount": int(rub_amount),
            "btc_amount": float(btc_amount),
            "txid": txid,
            "created_at": now,
        }
    )
    new_total = current_total + int(rub_amount)
    history_json = _dump_history(history)

    await db.execute(
        """
        UPDATE user_breloks
           SET history_json = ?, total_topup = ?, updated_at = ?
         WHERE owner_telegram_id = ?
        """,
        (history_json, new_total, now, owner_telegram_id),
    )
    await db.commit()


async def list_history_by_owner(
    owner_telegram_id: int,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Возвращает последние операции пользователя из history_json.

    Возвращаемый формат совпадает с тем, что ожидает handlers/brelok.py:
    - rub_amount: int
    - btc_amount: float
    - txid: str | None
    - created_at: str ("YYYY-MM-DD HH:MM:SS")
    """
    db = await get_db()
    cur = await db.execute(
        "SELECT history_json FROM user_breloks WHERE owner_telegram_id = ?",
        (owner_telegram_id,),
    )
    row = await cur.fetchone()
    if not row:
        return []

    history = _safe_load_history(row["history_json"])
    # Берём последние limit записей, начиная с самых свежих
    return list(reversed(history[-limit:]))


# -----------------------------------------------------------------------------
# Поиск по last4 и агрегированная info (для sms_tg / брелока)
# -----------------------------------------------------------------------------


async def get_user_by_brelok_last4(last4: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает словарь с информацией о брелоке по последним 4 цифрам.

    Формат:
        {
            "telegram_id": int,
            ... (поля из user_breloks)
        }

    Ищем только активные брелоки с привязанным владельцем.
    """
    last4 = str(last4)[-4:]
    db = await get_db()
    cur = await db.execute(
        """
        SELECT
            owner_telegram_id AS telegram_id,
            id,
            number,
            owner_telegram_id,
            bank,
            wallet,
            status,
            total_topup,
            pin_code_hash,
            last4_hint,
            legacy_btc_address,
            history_json,
            created_at,
            updated_at
        FROM user_breloks
        WHERE last4_hint = ?
          AND owner_telegram_id IS NOT NULL
          AND status = 'active'
        ORDER BY id DESC
        LIMIT 1
        """,
        (last4,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_brelok_info(owner_telegram_id: int) -> Optional[Dict[str, Any]]:
    """
    Возвращает агрегированную информацию по брелоку пользователя.

    Дополнительно к полям таблицы добавляет:
        - brelok_enabled: 0/1 (активен ли брелок и есть ли адрес)
        - brelok_btc_address: legacy-ключ (для старого кода)
        - brelok_wallet_asset / brelok_wallet_network: активная монета/сеть
    """
    row = await get_by_owner(owner_telegram_id)
    if not row:
        return None

    info: Dict[str, Any] = dict(row)
    wallet = info.get("wallet") or info.get("legacy_btc_address")

    asset = (info.get("wallet_asset") or "BTC").upper()
    network = info.get("wallet_network")

    info["brelok_wallet_asset"] = asset
    info["brelok_wallet_network"] = network
    info["brelok_btc_address"] = wallet  # для совместимости со старым кодом
    info["brelok_enabled"] = 1 if wallet and info.get("status") == "active" else 0

    return info
