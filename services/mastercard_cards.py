from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from db.cards import (
    get_active_cards_for_amount,
    get_card_balance,
    get_card_by_id,
    set_card_active,
)
from db.connection import get_db
from db.users import get_all_users, get_user


NSK_TZ = ZoneInfo("Asia/Novosibirsk")


def _role_is_mastercard(role: Any) -> bool:
    """
    Роль в проекте встречается в разных регистрах:
    MasterCard / mastercard.
    Сравниваем мягко, чтобы не ломать старые данные.
    """
    return str(role or "").strip().lower() == "mastercard"


def _parse_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None

    raw = raw.replace("Z", "+00:00")

    for candidate in (raw, raw.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate)
        except Exception:
            pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            pass

    return None


def _to_nsk_datetime(value: Any) -> Optional[datetime]:
    dt = _parse_datetime(value)
    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(NSK_TZ)


def _fmt_money(value: Any) -> str:
    try:
        return f"{float(value or 0):,.0f}".replace(",", " ") + " ₽"
    except Exception:
        return "0 ₽"


def _next_nsk_midnight() -> datetime:
    now = datetime.now(NSK_TZ)
    return datetime(now.year, now.month, now.day, tzinfo=NSK_TZ) + timedelta(days=1)


def _limit_state_for_card(card: Dict[str, Any]) -> Tuple[bool, str, str]:
    """
    Проверяет лимитное состояние карты.

    Возвращает:
      (заблокирована_лимитом, причина, до_какого_времени)
    """
    now = datetime.now(NSK_TZ)
    next_midnight = _next_nsk_midnight()

    today_count = int(card.get("_today_count") or 0)
    today_sum = float(card.get("_today_sum") or 0.0)

    transfer_limit = int(card.get("daily_transfer_limit") or 0)
    daily_limit = float(card.get("daily_limit_rub") or 0.0)

    if transfer_limit > 0 and today_count >= transfer_limit:
        return (
            True,
            f"Лимит переводов за сутки: {today_count}/{transfer_limit} шт.",
            next_midnight.strftime("%d.%m %H:%M"),
        )

    if daily_limit > 0 and today_sum >= daily_limit:
        return (
            True,
            f"Дневной лимит суммы: {_fmt_money(today_sum)} из {_fmt_money(daily_limit)}",
            next_midnight.strftime("%d.%m %H:%M"),
        )

    last_done = card.get("_last_completed_nsk")
    pause_minutes = int(card.get("transfer_pause_minutes") or 0)

    if pause_minutes > 0 and isinstance(last_done, datetime):
        unlock_at = last_done + timedelta(minutes=pause_minutes)
        if unlock_at > now:
            return (
                True,
                f"Пауза после перевода: {pause_minutes} мин.",
                unlock_at.strftime("%d.%m %H:%M"),
            )

    return False, "", ""


async def get_mastercard_user_ids(*, only_active_session_ids: Optional[set[int]] = None) -> List[int]:
    """
    Возвращает telegram_id пользователей с ролью MasterCard.

    Если передан only_active_session_ids, оставляет только тех,
    кто сейчас считается активным в Telegram-сессии.
    """
    users = await get_all_users()
    result: List[int] = []

    for user in users:
        try:
            telegram_id = int(user.get("telegram_id") or 0)
        except Exception:
            telegram_id = 0

        if telegram_id <= 0:
            continue

        if not _role_is_mastercard(user.get("role")):
            continue

        if only_active_session_ids is not None and telegram_id not in only_active_session_ids:
            continue

        result.append(telegram_id)

    return result


async def _get_card_today_limit_stats(card_id: int) -> Dict[str, Any]:
    """
    Считает дневную статистику карты по завершённым p2p_orders.

    Используется тот же принцип, что на сайте Mastercard:
    - количество завершённых заявок за текущий день по Новосибирску;
    - сумма завершённых заявок;
    - время последней завершённой заявки.
    """
    now_nsk = datetime.now(NSK_TZ)
    start_nsk = datetime(now_nsk.year, now_nsk.month, now_nsk.day, tzinfo=NSK_TZ)
    start_utc = start_nsk.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    db = await get_db()
    cur = await db.execute(
        """
        SELECT
            COUNT(*),
            COALESCE(SUM(COALESCE(total_rub, 0)), 0),
            MAX(COALESCE(completed_at, created_at))
          FROM p2p_orders
         WHERE status = 'completed'
           AND card_id = ?
           AND datetime(COALESCE(completed_at, created_at)) >= datetime(?)
        """,
        (int(card_id), start_utc),
    )
    row = await cur.fetchone()
    await cur.close()

    if not row:
        return {"count": 0, "sum": 0.0, "last": None}

    return {
        "count": int(row[0] or 0),
        "sum": float(row[1] or 0.0),
        "last": _to_nsk_datetime(row[2]) if row[2] else None,
    }


async def enrich_card_with_mastercard_limits(card: Dict[str, Any]) -> Dict[str, Any]:
    """
    Добавляет к карте служебные поля:
    _balance, _today_count, _today_sum, _last_completed_nsk,
    _limit_blocked, _limit_reason, _limit_until.
    """
    result = dict(card)
    card_id = int(result.get("card_id") or 0)

    stats = await _get_card_today_limit_stats(card_id)
    balance = await get_card_balance(card_id)

    result["_balance"] = float(balance or 0.0)
    result["_today_count"] = int(stats.get("count") or 0)
    result["_today_sum"] = float(stats.get("sum") or 0.0)
    result["_last_completed_nsk"] = stats.get("last")

    blocked, reason, until = _limit_state_for_card(result)
    result["_limit_blocked"] = blocked
    result["_limit_reason"] = reason
    result["_limit_until"] = until

    return result


async def refresh_mastercard_card_limit_state(card: Dict[str, Any]) -> Dict[str, Any]:
    """
    Синхронизирует активность карты с лимитами.

    Если лимит достигнут — карта выключается.
    Если лимит прошёл, а карта была выключена именно лимитом — пока НЕ включаем её здесь,
    потому что причина выключения в таблице cards не хранится.
    Автовключение уже есть на странице сайта через mastercard_card_limit_locks.
    """
    result = await enrich_card_with_mastercard_limits(card)

    card_id = int(result.get("card_id") or 0)
    owner_id = int(result.get("owner_id") or 0)
    is_active = bool(result.get("is_active", True))
    blocked = bool(result.get("_limit_blocked"))

    if card_id > 0 and owner_id > 0 and blocked and is_active:
        await set_card_active(card_id=card_id, owner_id=owner_id, is_active=False)
        result["is_active"] = False

    return result


async def get_available_mastercard_cards_for_amount(amount_rub: float) -> List[Dict[str, Any]]:
    """
    Возвращает карты Mastercard, которые можно показать оператору для заявки.

    Условия:
    - владелец карты имеет роль MasterCard;
    - карта активна;
    - сумма проходит min_amount_rub / max_amount_rub;
    - не достигнуты дневные лимиты и пауза.
    """
    amount = float(amount_rub or 0)
    mastercard_ids = set(await get_mastercard_user_ids())

    if not mastercard_ids:
        return []

    raw_cards = await get_active_cards_for_amount(amount)
    result: List[Dict[str, Any]] = []

    for raw_card in raw_cards:
        try:
            owner_id = int(raw_card.get("owner_id") or 0)
        except Exception:
            owner_id = 0

        if owner_id <= 0 or owner_id not in mastercard_ids:
            continue

        card = await refresh_mastercard_card_limit_state(raw_card)

        if not bool(card.get("is_active", True)):
            continue

        if bool(card.get("_limit_blocked")):
            continue

        result.append(card)

    result.sort(
        key=lambda item: (
            float(item.get("_today_sum") or 0.0),
            int(item.get("_today_count") or 0),
            int(item.get("card_id") or 0),
        )
    )
    return result


async def get_mastercard_card_for_issue(card_id: int, amount_rub: float) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Проверяет конкретную карту перед выдачей реквизитов.

    Возвращает:
      (карта, "")
      или
      (None, причина_отказа)
    """
    card = await get_card_by_id(int(card_id))
    if not card:
        return None, "Карта не найдена."

    owner_id = int(card.get("owner_id") or 0)
    if owner_id <= 0:
        return None, "Эта карта не принадлежит пользователю Mastercard."

    owner = await get_user(owner_id)
    if not owner or not _role_is_mastercard(owner.get("role")):
        return None, "Владелец карты больше не имеет роль Mastercard."

    amount = float(amount_rub or 0)

    min_amount = card.get("min_amount_rub")
    if min_amount is not None and float(min_amount or 0) > 0 and amount < float(min_amount):
        return None, f"Сумма меньше минимума карты: {_fmt_money(min_amount)}."

    max_amount = card.get("max_amount_rub")
    if max_amount is not None and float(max_amount or 0) > 0 and amount > float(max_amount):
        return None, f"Сумма больше максимума карты: {_fmt_money(max_amount)}."

    card = await refresh_mastercard_card_limit_state(card)

    if not bool(card.get("is_active", True)):
        reason = str(card.get("_limit_reason") or "").strip()
        until = str(card.get("_limit_until") or "").strip()
        if reason:
            return None, f"Карта сейчас недоступна: {reason}. До: {until or '—'}."
        return None, "Карта выключена."

    if bool(card.get("_limit_blocked")):
        reason = str(card.get("_limit_reason") or "").strip()
        until = str(card.get("_limit_until") or "").strip()
        return None, f"Карта упёрлась в лимит: {reason}. До: {until or '—'}."

    return card, ""


def format_mastercard_card_button_title(card: Dict[str, Any]) -> str:
    """
    Красивый текст кнопки выбора карты в Telegram.
    """
    card_id = int(card.get("card_id") or 0)
    bank_name = str(card.get("bank_name") or "Банк").strip()
    balance = float(card.get("_balance") or 0.0)

    today_count = int(card.get("_today_count") or 0)
    transfer_limit = int(card.get("daily_transfer_limit") or 0)
    today_sum = float(card.get("_today_sum") or 0.0)
    daily_limit = float(card.get("daily_limit_rub") or 0.0)

    transfers_text = f"{today_count}/{transfer_limit}" if transfer_limit > 0 else f"{today_count}/∞"
    daily_text = f"{_fmt_money(today_sum)}/{_fmt_money(daily_limit)}" if daily_limit > 0 else f"{_fmt_money(today_sum)}/∞"

    return f"#{card_id} · {bank_name} · баланс {_fmt_money(balance)} · {transfers_text} · {daily_text}"


def get_card_requisites_text(card: Dict[str, Any]) -> str:
    """
    Формирует реквизиты для сообщения пользователю.
    """
    bank_name = str(card.get("bank_name") or "").strip()
    sbp_phone = str(card.get("sbp_phone") or "").strip()
    card_number = str(card.get("card_number") or "").strip()

    lines: List[str] = []

    if bank_name:
        lines.append(f"Банк: {bank_name}")

    if sbp_phone:
        lines.append(f"СБП: {sbp_phone}")

    if card_number:
        lines.append(f"Карта: {card_number}")

    return "\n".join(lines).strip()
