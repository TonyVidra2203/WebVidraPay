"""
Модуль подключения к SQLite с единым пулом соединений и базовыми настройками.
"""

from __future__ import annotations

import aiosqlite
from config.settings import DB_PATH

# -----------------------------------------------------------------------------
# Раздел: Глобальное соединение
# -----------------------------------------------------------------------------

_db: aiosqlite.Connection | None = None

# -----------------------------------------------------------------------------
# Раздел: Работа с соединением
# -----------------------------------------------------------------------------

async def get_db() -> aiosqlite.Connection:
    """
    Возвращает единое соединение с SQLite.
    Настройки:
      - timeout: ожидание до 30 сек при блокировке
      - WAL: журналирование для параллельного чтения/записи
      - busy_timeout: внутренний таймаут драйвера
      - synchronous=NORMAL: баланс между скоростью и надёжностью
      - foreign_keys=ON: контроль ссылочной целостности
    """
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH, timeout=30.0)
        await _db.execute("PRAGMA journal_mode=WAL;")
        await _db.execute("PRAGMA busy_timeout=5000;")
        await _db.execute("PRAGMA synchronous=NORMAL;")
        await _db.execute("PRAGMA foreign_keys=ON;")
        _db.row_factory = aiosqlite.Row
    return _db


async def close_db() -> None:
    """Закрывает текущее соединение с БД (если открыто)."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
