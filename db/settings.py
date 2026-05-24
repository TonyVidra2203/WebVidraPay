# -----------------------------------------------------------------------------
# Раздел: описание модуля
# -----------------------------------------------------------------------------
"""
Работа с таблицей настроек приложения: хранение, получение, кэширование значений.
"""

# -----------------------------------------------------------------------------
# Раздел: импорты
# -----------------------------------------------------------------------------
import time
from typing import Optional, Dict, Tuple

from aiosqlite import Row
from db.connection import get_db

# -----------------------------------------------------------------------------
# Раздел: константы и кэш
# -----------------------------------------------------------------------------
_CACHE_TTL_SEC: int = 60
_cache: Dict[str, Tuple[float, Optional[str]]] = {}

# -----------------------------------------------------------------------------
# Раздел: инициализация таблицы
# -----------------------------------------------------------------------------
async def init_settings_table() -> None:
    """Создаёт таблицу настроек, если она отсутствует."""
    db = await get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    await db.commit()

# -----------------------------------------------------------------------------
# Раздел: операции с настройками
# -----------------------------------------------------------------------------
async def get_setting(key: str) -> Optional[str]:
    """
    Возвращает значение настройки по ключу.

    Использует локальный кэш для сокращения обращений к БД.
    """
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] <= _CACHE_TTL_SEC:
        return hit[1]

    db = await get_db()
    cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row: Optional[Row] = await cur.fetchone()
    await cur.close()

    val: Optional[str] = row["value"] if row else None
    _cache[key] = (now, val)
    return val


async def set_setting(key: str, value: str) -> None:
    """Устанавливает или обновляет значение настройки по ключу."""
    db = await get_db()
    await db.execute(
        """
        INSERT INTO settings(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    await db.commit()
    _cache[key] = (time.time(), value)

# -----------------------------------------------------------------------------
# Раздел: хелперы для курса USDT/RUB
# -----------------------------------------------------------------------------
USDT_RUB_KEY: str = "USDT_RUB_RATE"


async def get_usdt_rub_rate_manual() -> Optional[float]:
    """Возвращает сохранённый вручную курс USDT/RUB, если он задан."""
    value = await get_setting(USDT_RUB_KEY)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def set_usdt_rub_rate_manual(rate: float) -> None:
    """Сохраняет вручную заданный курс USDT/RUB с округлением до 6 знаков."""
    await set_setting(USDT_RUB_KEY, f"{rate:.6f}")

# -----------------------------------------------------------------------------
# Раздел: хелперы для переключателя распределения прибыли TON
# -----------------------------------------------------------------------------
TON_PROFIT_SPLIT_ENABLED_KEY: str = "TON_PROFIT_SPLIT_ENABLED"


async def is_ton_profit_split_enabled() -> bool:
    """
    Возвращает состояние переключателя распределения прибыли TON.

    По умолчанию возвращает True, чтобы сохранить текущее поведение бота:
    если настройка ещё ни разу не создавалась, split считается включённым.
    """
    value = await get_setting(TON_PROFIT_SPLIT_ENABLED_KEY)
    if value is None:
        return True

    normalized = str(value).strip().lower()
    return normalized in {"1", "true", "yes", "on", "enabled"}


async def set_ton_profit_split_enabled(enabled: bool) -> None:
    """Сохраняет состояние переключателя распределения прибыли TON."""
    await set_setting(TON_PROFIT_SPLIT_ENABLED_KEY, "1" if enabled else "0")


async def toggle_ton_profit_split_enabled() -> bool:
    """
    Переключает состояние распределения прибыли TON и возвращает новое значение.
    """
    new_value = not await is_ton_profit_split_enabled()
    await set_ton_profit_split_enabled(new_value)
    return new_value