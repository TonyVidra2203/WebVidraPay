# -----------------------------------------------------------------------------
# Раздел: описание модуля
# -----------------------------------------------------------------------------
"""
Реферальные начисления: запись комиссий, попытка начисления и агрегирование.

Дополнено:
- Ручные корректировки реферального счёта админом (плюс/минус)
- Итоговый реферальный баланс = начисления + корректировки

Совместимо с существующей инфраструктурой БД и моделью пользователей.
"""

# -----------------------------------------------------------------------------
# Раздел: импорты
# -----------------------------------------------------------------------------
from datetime import datetime, timezone
from typing import Optional

from db.connection import get_db
from db.users import get_user

# -----------------------------------------------------------------------------
# Раздел: SQL-схемы
# -----------------------------------------------------------------------------
_REFERRALS_TABLE: str = """
CREATE TABLE IF NOT EXISTS referral_commissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referred_user_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Уникальность по order_id защищает от повторного начисления по одной сделке
_CREATE_UNIQUE_INDEX: str = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_ref_comm_order
ON referral_commissions(order_id);
"""

# Ручные корректировки реф. счёта (можно + и -)
_REFERRAL_ADJUSTMENTS_TABLE: str = """
CREATE TABLE IF NOT EXISTS referral_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    admin_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
"""

_CREATE_ADJUSTMENTS_INDEX: str = """
CREATE INDEX IF NOT EXISTS idx_ref_adj_referrer
ON referral_adjustments(referrer_id);
"""

# -----------------------------------------------------------------------------
# Раздел: внутренняя инициализация схемы
# -----------------------------------------------------------------------------
async def _ensure_schema() -> None:
    """
    Гарантирует наличие таблиц/индексов модуля.
    Вызов идемпотентен.
    """
    db = await get_db()
    await db.execute(_REFERRALS_TABLE)
    await db.execute(_CREATE_UNIQUE_INDEX)
    await db.execute(_REFERRAL_ADJUSTMENTS_TABLE)
    await db.execute(_CREATE_ADJUSTMENTS_INDEX)

    # Заявки на вывод реферальных
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS referral_withdraw_requests (
            request_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount_rub REAL NOT NULL,
            coin TEXT NOT NULL,
            wallet TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            processed_at TEXT
        );
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ref_withdraw_status
        ON referral_withdraw_requests(status);
        """
    )

    await db.commit()


# -----------------------------------------------------------------------------
# Раздел: асинхронные функции
# -----------------------------------------------------------------------------
async def add_referral_commission(
    referrer_id: int,
    referred_user_id: int,
    order_id: int,
    amount: float,
) -> bool:
    """
    Добавляет запись о реферальной комиссии.

    Защита от дублей реализована за счёт UNIQUE-индекса по order_id.
    При повторной вставке с тем же order_id операция тихо игнорируется.

    Возвращает:
      True  — запись реально добавлена (начисление произошло)
      False — запись проигнорирована (начисление по order_id уже было)
    """
    await _ensure_schema()
    created_at: str = datetime.now(timezone.utc).isoformat()
    db = await get_db()

    cur = await db.execute(
        """
        INSERT OR IGNORE INTO referral_commissions
        (referrer_id, referred_user_id, order_id, amount, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (referrer_id, referred_user_id, order_id, amount, created_at),
    )
    await db.commit()

    # Для INSERT OR IGNORE: rowcount == 1 если вставка произошла, иначе 0
    try:
        inserted = bool(getattr(cur, "rowcount", 0) == 1)
    except Exception:
        inserted = False

    return inserted


async def try_add_referral_commission(
    order_id: int,
    user_id: int,
    total_rub: float,
) -> Optional[dict]:
    """
    Пытается начислить 2% пригласившему пользователя по завершённой сделке.

    Возвращает:
      dict с данными начисления, если начисление произошло (вставка в БД была выполнена),
      иначе None (нет реферера / reward <= 0 / дубль по order_id).
    """
    user = await get_user(user_id)
    referrer_id: Optional[int] = user.get("referrer_id") if user else None
    if not referrer_id:
        return None

    reward: float = round(total_rub * 0.02, 2)
    if reward <= 0:
        return None

    inserted = await add_referral_commission(referrer_id, user_id, order_id, reward)
    if not inserted:
        return None

    return {
        "referrer_id": referrer_id,
        "referred_user_id": user_id,
        "order_id": order_id,
        "amount": reward,
        "total_rub": float(total_rub),
    }

async def get_inviter_commission_sum(inviter_id: int) -> float:
    """
    Возвращает суммарную величину начислений пригласившему пользователю
    (ТОЛЬКО начисления, без ручных корректировок).
    """
    await _ensure_schema()
    db = await get_db()
    cur = await db.execute(
        """
        SELECT COALESCE(SUM(amount), 0.0)
        FROM referral_commissions
        WHERE referrer_id = ?
        """,
        (inviter_id,),
    )
    row = await cur.fetchone()
    return float(row[0] or 0.0)


async def add_referral_adjustment(
    referrer_id: int,
    admin_id: int,
    amount: float,
    reason: Optional[str] = None,
) -> None:
    """
    Добавляет ручную корректировку реферального счёта.

    amount может быть положительным (прибавить) или отрицательным (убавить).
    reason опционально (например: "коррекция", "компенсация", "ошибка начисления").
    """
    await _ensure_schema()
    if not isinstance(amount, (int, float)) or amount == 0:
        return

    created_at: str = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    await db.execute(
        """
        INSERT INTO referral_adjustments (referrer_id, admin_id, amount, reason, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (referrer_id, admin_id, float(amount), reason, created_at),
    )
    await db.commit()


async def get_referral_adjustments_sum(referrer_id: int) -> float:
    """Сумма ручных корректировок по пользователю (может быть отрицательной)."""
    await _ensure_schema()
    db = await get_db()
    cur = await db.execute(
        """
        SELECT COALESCE(SUM(amount), 0.0)
        FROM referral_adjustments
        WHERE referrer_id = ?
        """,
        (referrer_id,),
    )
    row = await cur.fetchone()
    return float(row[0] or 0.0)


async def get_referral_balance(referrer_id: int) -> float:
    """
    Итоговый реферальный счёт пользователя:
    начисления + ручные корректировки.
    """
    commissions = await get_inviter_commission_sum(referrer_id)
    adjustments = await get_referral_adjustments_sum(referrer_id)
    return round(float(commissions + adjustments), 2)


async def create_referral_withdraw_request(
    request_id: str,
    user_id: int,
    amount_rub: float,
    coin: str,
    wallet: str,
) -> None:
    await _ensure_schema()
    created_at: str = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    await db.execute(
        """
        INSERT OR REPLACE INTO referral_withdraw_requests
        (request_id, user_id, amount_rub, coin, wallet, status, created_at, processed_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL)
        """,
        (request_id, user_id, float(amount_rub), str(coin), str(wallet), created_at),
    )
    await db.commit()


async def get_referral_withdraw_request(request_id: str) -> Optional[dict]:
    await _ensure_schema()
    db = await get_db()
    cur = await db.execute(
        """
        SELECT request_id, user_id, amount_rub, coin, wallet, status, created_at, processed_at
        FROM referral_withdraw_requests
        WHERE request_id = ?
        """,
        (request_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None

    keys = [
        "request_id",
        "user_id",
        "amount_rub",
        "coin",
        "wallet",
        "status",
        "created_at",
        "processed_at",
    ]
    return dict(zip(keys, row))


async def set_referral_withdraw_status(request_id: str, status: str) -> None:
    await _ensure_schema()
    processed_at: str = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    await db.execute(
        """
        UPDATE referral_withdraw_requests
           SET status = ?, processed_at = ?
         WHERE request_id = ?
        """,
        (status, processed_at, request_id),
    )
    await db.commit()
