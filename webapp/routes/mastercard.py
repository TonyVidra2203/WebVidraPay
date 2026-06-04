from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db.connection import get_db
from db.cards import (
    add_card,
    add_withdrawal,
    delete_card,
    get_card_balance,
    get_card_by_id,
    get_cards_by_owner,
    set_card_active,
    update_card,
)
from db.p2p import get_completed_orders_by_master
from db.users import get_user, get_user_mastercard_deposit, set_user_mastercard_deposit

router = APIRouter(prefix="/mastercard", tags=["mastercard-web"])

DEFAULT_MIN_AMOUNT_RUB = 1200
DEFAULT_MAX_AMOUNT_RUB = 30000
DEFAULT_DAILY_LIMIT_RUB = 30000
DEFAULT_DAILY_TRANSFER_LIMIT = 3
DEFAULT_TRANSFER_PAUSE_MINUTES = 30
NSK_TZ = ZoneInfo("Asia/Novosibirsk")


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _to_float_or_none(value: Any) -> Optional[float]:
    raw = str(value or "").replace(" ", "").replace(",", ".").strip()
    if raw in {"", "-", "нет"}:
        return None
    amount = float(raw)
    if amount < 0:
        raise ValueError("negative")
    return amount


def _to_int_or_none(value: Any) -> Optional[int]:
    raw = str(value or "").replace(" ", "").strip()
    if raw in {"", "-", "нет"}:
        return None
    amount = int(raw)
    if amount < 0:
        raise ValueError("negative")
    return amount


def _fmt_money(value: Any) -> str:
    try:
        return f"{float(value or 0):,.0f}".replace(",", " ") + " ₽"
    except Exception:
        return "0 ₽"


def _fmt_compact_money(value: Any) -> str:
    try:
        amount = float(value or 0)
    except Exception:
        amount = 0.0
    return f"{amount:,.0f}".replace(",", " ")


def _parse_date(value: Any) -> Optional[datetime]:
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
    dt = _parse_date(value)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(NSK_TZ)


def _is_today_nsk(value: Any) -> bool:
    dt = _to_nsk_datetime(value)
    return bool(dt and dt.date() == datetime.now(NSK_TZ).date())


def _fmt_date_short(value: Any) -> str:
    dt = _parse_date(value)
    if not dt:
        return _esc(value or "—")
    return dt.strftime("%d.%m %H:%M")


def _short_number(value: Any) -> str:
    text = str(value or "").replace(" ", "").strip()
    if not text:
        return "—"
    if len(text) <= 8:
        return _esc(text)
    return _esc(f"{text[:4]} •••• {text[-4:]}")


def _format_card_groups(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return "—"
    groups = [digits[i:i + 4] for i in range(0, len(digits), 4)]
    return _esc(" ".join(groups))


def _last4(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return "—"
    return _esc(digits[-4:])


def _normalize_card_number(value: Any) -> Optional[str]:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits or None


def _normalize_sbp_phone(value: Any) -> Optional[str]:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits or digits == "7":
        return None
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    elif not digits.startswith("7"):
        digits = "7" + digits
    return "+" + digits


def _redirect(user_id: int, anchor: str = "", admin_id: Optional[int] = None) -> RedirectResponse:
    suffix = f"#{anchor}" if anchor else ""
    admin_part = f"&admin_id={int(admin_id)}" if admin_id else ""
    return RedirectResponse(
        url=f"/mastercard?user_id={int(user_id)}{admin_part}{suffix}",
        status_code=303,
    )


def _alert_redirect(
    user_id: int,
    message: str,
    anchor: str = "cards",
    admin_id: Optional[int] = None,
) -> HTMLResponse:
    safe_message = _esc(str(message or ""))
    suffix = f"#{anchor}" if anchor else ""
    admin_part = f"&admin_id={int(admin_id)}" if admin_id else ""
    target_url = f"/mastercard?user_id={int(user_id)}{admin_part}{suffix}"
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="ru">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
          <title>MasterCard</title>
          <style>
            :root {{
              --bg:#000000;
              --card:#121318;
              --line:rgba(255,255,255,.20);
              --text:#f6f3ea;
              --muted:#a9acb4;
              --accent:#d6b35f;
              --accent2:#e1c46f;
            }}
            *{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
            html,body{{margin:0;width:100%;min-height:100%;background:#000;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}}
            .wrap{{min-height:100vh;min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:18px;background:radial-gradient(circle at 50% 0%,rgba(214,179,95,.14),transparent 42%),#000}}
            .modal{{width:100%;max-width:360px;border:1px solid rgba(214,179,95,.28);border-radius:26px;background:linear-gradient(180deg,#171820,#0d0d10);box-shadow:0 28px 70px rgba(0,0,0,.72);overflow:hidden;text-align:center}}
            .head{{padding:20px 18px 10px}}
            .icon{{width:54px;height:54px;margin:0 auto 12px;border-radius:19px;display:grid;place-items:center;border:1px solid rgba(214,179,95,.30);background:rgba(214,179,95,.10);color:var(--accent2);font-size:26px;font-weight:950}}
            h1{{margin:0;font-size:20px;line-height:1.15;font-weight:950;letter-spacing:-.25px}}
            .text{{padding:8px 20px 18px;color:var(--muted);font-size:14px;line-height:1.45}}
            .actions{{padding:0 18px 18px}}
            .ok{{width:100%;min-height:48px;border:0;border-radius:17px;background:linear-gradient(135deg,#e1c46f,#caa24e);color:#161108;font-size:15px;font-weight:950;cursor:pointer}}
            .ok:active{{transform:scale(.99)}}
          </style>
        </head>
        <body>
          <main class="wrap">
            <section class="modal" role="dialog" aria-modal="true">
              <div class="head">
                <div class="icon">!</div>
                <h1>Уведомление</h1>
              </div>
              <div class="text">{safe_message}</div>
              <div class="actions">
                <button class="ok" type="button" onclick="window.location.href='{target_url}'">OK</button>
              </div>
            </section>
          </main>
        </body>
        </html>
        """
    )


async def _ensure_mastercard_web_tables() -> None:
    db = await get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mastercard_card_limit_locks (
            card_id INTEGER PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            locked_until TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mastercard_card_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            card_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            title TEXT NOT NULL,
            details TEXT,
            amount REAL DEFAULT 0,
            diff REAL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.commit()


async def _ensure_withdrawals_table() -> None:
    db = await get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS withdrawals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id   INTEGER NOT NULL,
            card_id    INTEGER NOT NULL,
            amount     REAL    NOT NULL,
            date       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.commit()


async def _get_table_columns(table_name: str) -> set[str]:
    db = await get_db()
    cur = await db.execute(f"PRAGMA table_info({table_name})")
    rows = await cur.fetchall() or []
    await cur.close()
    return {str(row[1]) for row in rows}


async def _record_card_withdrawal(admin_id: int, card_id: int, amount: float) -> None:
    """
    Записывает операцию вывода/корректировки баланса.

    В старых установках таблица withdrawals могла быть создана раньше с немного
    другой схемой, из-за чего прямой вызов db.cards.add_withdrawal иногда падал
    Internal Server Error. Этот helper сначала проверяет реальные колонки таблицы
    и вставляет только те поля, которые в ней есть. Для расчёта баланса главное —
    чтобы были card_id и amount, их использует get_card_balance().
    """
    await _ensure_withdrawals_table()
    columns = await _get_table_columns("withdrawals")
    db = await get_db()

    if "card_id" not in columns or "amount" not in columns:
        raise RuntimeError("Таблица withdrawals не содержит обязательные поля card_id и amount")

    insert_fields: list[str] = []
    values: list[Any] = []

    if "admin_id" in columns:
        insert_fields.append("admin_id")
        values.append(int(admin_id))
    elif "user_id" in columns:
        insert_fields.append("user_id")
        values.append(int(admin_id))

    insert_fields.extend(["card_id", "amount"])
    values.extend([int(card_id), float(amount)])

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if "date" in columns:
        insert_fields.append("date")
        values.append(now_utc)
    elif "created_at" in columns:
        insert_fields.append("created_at")
        values.append(now_utc)

    placeholders = ", ".join("?" for _ in insert_fields)
    fields_sql = ", ".join(insert_fields)

    await db.execute(
        f"INSERT INTO withdrawals ({fields_sql}) VALUES ({placeholders})",
        values,
    )
    await db.commit()



async def _log_card_audit(
        *,
        owner_id: int,
        card_id: int,
        action: str,
        title: str,
        details: str = "",
        amount: float = 0.0,
        diff: float = 0.0,
) -> None:
    try:
        await _ensure_mastercard_web_tables()
        db = await get_db()
        await db.execute(
            """
            INSERT INTO mastercard_card_audit(owner_id, card_id, action, title, details, amount, diff, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (int(owner_id), int(card_id), str(action), str(title), str(details), float(amount or 0), float(diff or 0)),
        )
        await db.commit()
    except Exception:
        return


async def _set_limit_lock(owner_id: int, card_id: int, reason: str, locked_until: str = "") -> None:
    await _ensure_mastercard_web_tables()
    db = await get_db()
    await db.execute(
        """
        INSERT INTO mastercard_card_limit_locks(card_id, owner_id, reason, locked_until, updated_at)
        VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(card_id) DO UPDATE SET
            owner_id = excluded.owner_id,
            reason = excluded.reason,
            locked_until = excluded.locked_until,
            updated_at = CURRENT_TIMESTAMP
        """,
        (int(card_id), int(owner_id), str(reason), str(locked_until or "")),
    )
    await db.commit()


async def _has_limit_lock(card_id: int) -> bool:
    await _ensure_mastercard_web_tables()
    db = await get_db()
    cur = await db.execute(
        "SELECT 1 FROM mastercard_card_limit_locks WHERE card_id = ? LIMIT 1",
        (int(card_id),),
    )
    row = await cur.fetchone()
    await cur.close()
    return bool(row)


async def _clear_limit_lock(card_id: int) -> None:
    await _ensure_mastercard_web_tables()
    db = await get_db()
    await db.execute("DELETE FROM mastercard_card_limit_locks WHERE card_id = ?", (int(card_id),))
    await db.commit()


def _next_nsk_midnight() -> datetime:
    now = datetime.now(NSK_TZ)
    return datetime(now.year, now.month, now.day, tzinfo=NSK_TZ).replace(day=now.day) + timedelta(days=1)


def _limit_state_for_card(card: dict[str, Any]) -> tuple[bool, str, str]:
    now = datetime.now(NSK_TZ)
    next_midnight = datetime(now.year, now.month, now.day, tzinfo=NSK_TZ) + timedelta(days=1)

    today_count = int(card.get("_today_count") or 0)
    today_sum = float(card.get("_today_sum") or 0.0)
    transfer_limit = int(card.get("daily_transfer_limit") or 0)
    daily_limit = float(card.get("daily_limit_rub") or 0.0)

    if transfer_limit > 0 and today_count >= transfer_limit:
        return True, f"Лимит переводов за сутки: {today_count}/{transfer_limit} шт.", next_midnight.strftime(
            "%d.%m %H:%M")

    if daily_limit > 0 and today_sum >= daily_limit:
        return True, f"Дневной лимит суммы: {_fmt_money(today_sum)} из {_fmt_money(daily_limit)}", next_midnight.strftime(
            "%d.%m %H:%M")

    last_done = card.get("_last_completed_nsk")
    pause_minutes = int(card.get("transfer_pause_minutes") or 0)
    if pause_minutes > 0 and isinstance(last_done, datetime):
        unlock_at = last_done + timedelta(minutes=pause_minutes)
        if unlock_at > now:
            return True, f"Пауза после перевода: {pause_minutes} мин.", unlock_at.strftime("%d.%m %H:%M")

    return False, "", ""


async def _get_card_today_limit_stats(card_id: int) -> dict[str, Any]:
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
    last_dt = _to_nsk_datetime(row[2]) if row and row[2] else None
    return {"count": int(row[0] or 0) if row else 0, "sum": float(row[1] or 0.0) if row else 0.0, "last": last_dt}




async def _sum_reserve_withdrawals_for_cards(card_ids: list[int]) -> float:
    if not card_ids:
        return 0.0

    await _ensure_withdrawals_table()
    columns = await _get_table_columns("withdrawals")
    if "card_id" not in columns or "amount" not in columns:
        return 0.0

    placeholders = ", ".join("?" for _ in card_ids)
    db = await get_db()
    cur = await db.execute(
        f"""
        SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0)
          FROM withdrawals
         WHERE card_id IN ({placeholders})
        """,
        [int(card_id) for card_id in card_ids],
    )
    row = await cur.fetchone()
    await cur.close()
    return float(row[0] or 0.0) if row else 0.0


async def _count_reserve_withdrawal_logs(card_ids: list[int]) -> int:
    if not card_ids:
        return 0

    await _ensure_withdrawals_table()
    columns = await _get_table_columns("withdrawals")
    if "card_id" not in columns or "amount" not in columns:
        return 0

    placeholders = ", ".join("?" for _ in card_ids)
    db = await get_db()
    cur = await db.execute(
        f"""
        SELECT COUNT(*)
          FROM withdrawals
         WHERE card_id IN ({placeholders})
           AND amount > 0
        """,
        [int(card_id) for card_id in card_ids],
    )
    row = await cur.fetchone()
    await cur.close()
    return int(row[0] or 0) if row else 0


async def _load_reserve_withdrawal_logs(
    card_names: dict[int, str],
    limit: int = 5,
    offset: int = 0,
) -> list[dict[str, Any]]:
    card_ids = [int(card_id) for card_id in card_names.keys() if int(card_id or 0) > 0]
    if not card_ids:
        return []

    await _ensure_withdrawals_table()
    columns = await _get_table_columns("withdrawals")
    if "card_id" not in columns or "amount" not in columns:
        return []

    date_column = "date" if "date" in columns else ("created_at" if "created_at" in columns else "")
    date_select = date_column if date_column else "''"

    placeholders = ", ".join("?" for _ in card_ids)
    db = await get_db()
    cur = await db.execute(
        f"""
        SELECT card_id, amount, {date_select} AS created_at
          FROM withdrawals
         WHERE card_id IN ({placeholders})
           AND amount > 0
      ORDER BY id DESC
         LIMIT ? OFFSET ?
        """,
        [*card_ids, int(limit), max(int(offset), 0)],
    )
    rows = await cur.fetchall() or []
    await cur.close()

    result: list[dict[str, Any]] = []
    for row in rows:
        card_id = int(row[0] or 0)
        amount = float(row[1] or 0.0)
        result.append({
            "card_id": card_id,
            "card_name": card_names.get(card_id, f"Карта #{card_id}"),
            "amount": amount,
            "created_at": row[2] or "",
        })
    return result


def _reserve_control_status_text(debt: float) -> str:
    if debt > 0.01:
        return "Есть недодача в резерв — держатель карт ещё должен вернуть деньги обменнику."
    if debt < -0.01:
        return "Есть переплата в резерв — держатель карт отправил больше, чем получил на карты."
    return "Резерв закрыт — полученные средства и выводы в резерв сходятся."

async def _load_audit_logs(owner_id: int, card_names: dict[int, str], limit: int = 5, offset: int = 0) -> list[dict[str, Any]]:
    await _ensure_mastercard_web_tables()
    db = await get_db()
    cur = await db.execute(
        """
        SELECT card_id, action, title, details, amount, diff, created_at
          FROM mastercard_card_audit
         WHERE owner_id = ?
           AND ABS(COALESCE(diff, 0)) >= 0.01
      ORDER BY id DESC
         LIMIT ? OFFSET ?
        """,
        (int(owner_id), int(limit), max(int(offset), 0)),
    )
    rows = await cur.fetchall() or []
    await cur.close()

    result: list[dict[str, Any]] = []
    for row in rows:
        card_id = int(row[0] or 0)
        result.append({
            "card_id": card_id,
            "card_name": card_names.get(card_id, f"Карта #{card_id}"),
            "action": str(row[1] or ""),
            "title": str(row[2] or ""),
            "details": str(row[3] or ""),
            "amount": float(row[4] or 0.0),
            "diff": float(row[5] or 0.0),
            "created_at": row[6] or "",
        })
    return result



async def _count_audit_logs(owner_id: int) -> int:
    await _ensure_mastercard_web_tables()
    db = await get_db()
    cur = await db.execute(
        """
        SELECT COUNT(*)
          FROM mastercard_card_audit
         WHERE owner_id = ?
           AND ABS(COALESCE(diff, 0)) >= 0.01
        """,
        (int(owner_id),),
    )
    row = await cur.fetchone()
    await cur.close()
    return int(row[0] or 0) if row else 0


async def _get_audit_money_summary(owner_id: int) -> dict[str, Any]:
    await _ensure_mastercard_web_tables()
    db = await get_db()
    cur = await db.execute(
        """
        SELECT
            COUNT(*),
            COALESCE(SUM(CASE WHEN diff > 0 THEN diff ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN diff < 0 THEN ABS(diff) ELSE 0 END), 0),
            COALESCE(SUM(diff), 0),
            COALESCE(MAX(ABS(diff)), 0)
          FROM mastercard_card_audit
         WHERE owner_id = ?
           AND ABS(COALESCE(diff, 0)) >= 0.01
        """,
        (int(owner_id),),
    )
    row = await cur.fetchone()
    await cur.close()

    plus_total = float(row[1] or 0.0) if row else 0.0
    minus_total = float(row[2] or 0.0) if row else 0.0
    net_total = float(row[3] or 0.0) if row else 0.0
    max_diff = float(row[4] or 0.0) if row else 0.0

    return {
        "count": int(row[0] or 0) if row else 0,
        "plus_total": plus_total,
        "minus_total": minus_total,
        "net_total": net_total,
        "max_diff": max_diff,
        "risk_text": (
            "Есть крупные отклонения — проверьте последние операции."
            if max_diff >= 10000 else
            "Контроль без критичных отклонений."
        ),
    }



async def _clear_audit_logs(owner_id: int) -> None:
    await _ensure_mastercard_web_tables()
    db = await get_db()
    await db.execute(
        """
        DELETE FROM mastercard_card_audit
         WHERE owner_id = ?
           AND ABS(COALESCE(diff, 0)) >= 0.01
        """,
        (int(owner_id),),
    )
    await db.commit()



async def _reset_mastercard_card_data(owner_id: int) -> dict[str, int]:
    """
    Сбрасывает рабочие данные Mastercard-карт владельца без удаления самих карт
    и без изменения реквизитов/лимитов.

    Что сбрасывается:
    - привязка заявок к этим картам (card_id -> NULL), чтобы статистика и баланс
      стартовали с нуля;
    - withdrawals по этим картам;
    - audit-логи и временные лимитные блокировки по этим картам;
    - служебные записи VidraPay-распределения по этим картам.

    Важно: после сброса карты, которые были выключены именно временной
    лимитной блокировкой, включаются обратно. Иначе VidraPay продолжает брать
    только active-карты и может не показать реквизиты даже при балансе ниже депозита.
    """
    cards = await get_cards_by_owner(int(owner_id))
    card_ids = [
        int(card.get("card_id") or 0)
        for card in cards
        if int(card.get("card_id") or 0) > 0
    ]

    await _ensure_mastercard_web_tables()
    await _ensure_withdrawals_table()

    db = await get_db()

    if not card_ids:
        await db.execute("DELETE FROM mastercard_card_audit WHERE owner_id = ?", (int(owner_id),))
        await db.execute("DELETE FROM mastercard_card_limit_locks WHERE owner_id = ?", (int(owner_id),))
        await db.commit()
        return {
            "cards": 0,
            "orders_unlinked": 0,
            "withdrawals_deleted": 0,
            "audit_deleted": 0,
            "locks_deleted": 0,
            "vidrapay_usage_deleted": 0,
            "cards_reactivated": 0,
        }

    placeholders = ", ".join("?" for _ in card_ids)

    orders_unlinked = 0
    withdrawals_deleted = 0
    audit_deleted = 0
    locks_deleted = 0
    vidrapay_usage_deleted = 0
    cards_reactivated = 0

    locked_card_ids: list[int] = []
    try:
        cur = await db.execute(
            f"""
            SELECT card_id
              FROM mastercard_card_limit_locks
             WHERE owner_id = ?
                OR card_id IN ({placeholders})
            """,
            [int(owner_id), *card_ids],
        )
        rows = await cur.fetchall() or []
        await cur.close()
        for row in rows:
            try:
                locked_card_id = int(row[0] or 0)
            except Exception:
                locked_card_id = 0
            if locked_card_id > 0 and locked_card_id in card_ids and locked_card_id not in locked_card_ids:
                locked_card_ids.append(locked_card_id)
    except Exception:
        locked_card_ids = []

    try:
        cur = await db.execute(
            f"SELECT COUNT(*) FROM p2p_orders WHERE card_id IN ({placeholders})",
            card_ids,
        )
        row = await cur.fetchone()
        await cur.close()
        orders_unlinked = int(row[0] or 0) if row else 0
    except Exception:
        orders_unlinked = 0

    try:
        await db.execute(
            f"UPDATE p2p_orders SET card_id = NULL WHERE card_id IN ({placeholders})",
            card_ids,
        )
    except Exception:
        pass

    columns_withdrawals = await _get_table_columns("withdrawals")
    if "card_id" in columns_withdrawals:
        try:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM withdrawals WHERE card_id IN ({placeholders})",
                card_ids,
            )
            row = await cur.fetchone()
            await cur.close()
            withdrawals_deleted = int(row[0] or 0) if row else 0
        except Exception:
            withdrawals_deleted = 0

        try:
            await db.execute(
                f"DELETE FROM withdrawals WHERE card_id IN ({placeholders})",
                card_ids,
            )
        except Exception:
            pass

    try:
        cur = await db.execute(
            f"""
            SELECT COUNT(*)
              FROM mastercard_card_audit
             WHERE owner_id = ?
                OR card_id IN ({placeholders})
            """,
            [int(owner_id), *card_ids],
        )
        row = await cur.fetchone()
        await cur.close()
        audit_deleted = int(row[0] or 0) if row else 0
    except Exception:
        audit_deleted = 0

    try:
        await db.execute(
            f"""
            DELETE FROM mastercard_card_audit
             WHERE owner_id = ?
                OR card_id IN ({placeholders})
            """,
            [int(owner_id), *card_ids],
        )
    except Exception:
        pass

    try:
        cur = await db.execute(
            f"""
            SELECT COUNT(*)
              FROM mastercard_card_limit_locks
             WHERE owner_id = ?
                OR card_id IN ({placeholders})
            """,
            [int(owner_id), *card_ids],
        )
        row = await cur.fetchone()
        await cur.close()
        locks_deleted = int(row[0] or 0) if row else 0
    except Exception:
        locks_deleted = 0

    try:
        await db.execute(
            f"""
            DELETE FROM mastercard_card_limit_locks
             WHERE owner_id = ?
                OR card_id IN ({placeholders})
            """,
            [int(owner_id), *card_ids],
        )
    except Exception:
        pass

    try:
        cur = await db.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'vidrapay_card_distribution_usage'
             LIMIT 1
            """
        )
        usage_table = await cur.fetchone()
        await cur.close()
        if usage_table:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM vidrapay_card_distribution_usage WHERE card_id IN ({placeholders})",
                card_ids,
            )
            row = await cur.fetchone()
            await cur.close()
            vidrapay_usage_deleted = int(row[0] or 0) if row else 0

            await db.execute(
                f"DELETE FROM vidrapay_card_distribution_usage WHERE card_id IN ({placeholders})",
                card_ids,
            )
    except Exception:
        vidrapay_usage_deleted = 0

    if locked_card_ids:
        locked_placeholders = ", ".join("?" for _ in locked_card_ids)
        try:
            cur = await db.execute(
                f"""
                SELECT COUNT(*)
                  FROM cards
                 WHERE owner_id = ?
                   AND card_id IN ({locked_placeholders})
                   AND COALESCE(is_active, 1) = 0
                """,
                [int(owner_id), *locked_card_ids],
            )
            row = await cur.fetchone()
            await cur.close()
            cards_reactivated = int(row[0] or 0) if row else 0

            await db.execute(
                f"""
                UPDATE cards
                   SET is_active = 1,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE owner_id = ?
                   AND card_id IN ({locked_placeholders})
                """,
                [int(owner_id), *locked_card_ids],
            )
        except Exception:
            cards_reactivated = 0

    await db.commit()

    return {
        "cards": len(card_ids),
        "orders_unlinked": orders_unlinked,
        "withdrawals_deleted": withdrawals_deleted,
        "audit_deleted": audit_deleted,
        "locks_deleted": locks_deleted,
        "vidrapay_usage_deleted": vidrapay_usage_deleted,
        "cards_reactivated": cards_reactivated,
    }



async def _set_card_balance(
    card_id: int,
    user_id: int,
    target_balance: Any,
    admin_id: Optional[int] = None,
) -> None:
    raw = str(target_balance or "").replace(" ", "").replace(",", ".").strip()
    if raw == "":
        return

    try:
        desired = float(raw)
    except Exception:
        return

    if desired < 0:
        return

    card = await get_card_by_id(card_id)
    if not card or int(card.get("owner_id") or 0) != int(user_id):
        return

    current = float(await get_card_balance(card_id) or 0)
    diff = current - desired
    if abs(diff) < 0.01:
        return

    # Баланс карты считается как completed-заявки минус withdrawals.
    # Поэтому корректировка баланса фиксируется технической записью в withdrawals:
    # положительная сумма уменьшает баланс, отрицательная — увеличивает.
    await _record_card_withdrawal(
        admin_id=int(admin_id or user_id),
        card_id=int(card_id),
        amount=float(diff),
    )

    await _log_card_audit(
        owner_id=int(user_id),
        card_id=int(card_id),
        action="balance_adjust",
        title="Ручная правка баланса",
        details=f"Было {_fmt_money(current)}, указано {_fmt_money(desired)}. Система зафиксировала разницу.",
        amount=float(desired),
        diff=float(desired - current),
    )


async def _is_mastercard_user(user_id: int) -> bool:
    user = await get_user(int(user_id))
    role = str((user or {}).get("role") or "").strip().lower()
    return role in {"mastercard", "admin"}


async def _is_admin_user(user_id: int) -> bool:
    user = await get_user(int(user_id))
    role = str((user or {}).get("role") or "").strip().lower()
    return role == "admin"


async def _render_access_denied() -> HTMLResponse:
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="ru">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>MasterCard</title>
          <style>
            body{margin:0;background:#000;color:#f6f3ea;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
            .wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:18px}
            .card{max-width:360px;background:#101010;border:1px solid rgba(255,255,255,.09);border-radius:24px;padding:24px;text-align:center}
            .bad{color:#d6b35f;font-size:34px}.muted{color:#a9acb4;line-height:1.45}


    /* Android Chrome-safe version: brighter surfaces, no fragile blur, stable rows. */
    html.is-android body{{background:#000!important;color:#f7f4ec!important}}
    html.is-android .page{{min-height:100vh!important;min-height:100dvh!important;padding:10px 10px 24px!important}}
    html.is-android .shell{{max-width:540px}}
    html.is-android .panel{{background:#111217!important;border-color:rgba(255,255,255,.24)!important;box-shadow:none!important}}
    html.is-android .nav{{background:#111217!important;backdrop-filter:none!important;-webkit-backdrop-filter:none!important;border-color:rgba(255,255,255,.22)!important}}
    html.is-android .nav-btn{{background:#1d1e25!important;border-color:rgba(255,255,255,.20)!important;color:#dedfe4!important}}
    html.is-android .nav-btn.active{{background:#2a2417!important;border-color:rgba(214,179,95,.48)!important;color:#e7c66c!important}}
    html.is-android .card-slide{{background:linear-gradient(145deg,#23242c,#15161c)!important;border-color:rgba(255,255,255,.26)!important;box-shadow:none!important}}
    html.is-android .card-slide::before{{opacity:.75}}
    html.is-android .tile,
    html.is-android .form-box,
    html.is-android .edit-card,
    html.is-android .stat-block,
    html.is-android .order-card,
    html.is-android .log-card{{background:#171820!important;border-color:rgba(255,255,255,.24)!important;box-shadow:none!important}}
    html.is-android .quick-stat,
    html.is-android .stat-card,
    html.is-android .orders-mini,
    html.is-android .limit-chip,
    html.is-android .order-details{{background:#202129!important;border-color:rgba(255,255,255,.18)!important}}
    html.is-android input{{background:#0b0c10!important;border-color:rgba(255,255,255,.24)!important;color:#fff!important}}
    html.is-android label,
    html.is-android .stat-label,
    html.is-android .quick-label,
    html.is-android .orders-mini-label,
    html.is-android .limit-label{{color:#9fa3ad!important}}
    html.is-android .section-note,
    html.is-android .subtitle,
    html.is-android .order-meta,
    html.is-android .log-text{{color:#b6b9c1!important}}
    html.is-android .slide-line{{display:grid!important;grid-template-columns:auto minmax(0,1fr)!important;align-items:center!important;gap:12px!important}}
    html.is-android .slide-line b{{max-width:100%!important;text-align:right!important;white-space:normal!important;word-break:break-word!important}}
    html.is-android .slide-limits{{gap:9px!important}}
    html.is-android .slide-bottom{{display:grid!important;grid-template-columns:1fr!important;gap:12px!important;align-items:stretch!important}}
    html.is-android .slide-actions{{width:100%!important;min-width:0!important;grid-template-columns:1fr 1fr!important}}
    html.is-android .slide-action{{min-height:44px!important}}
    html.is-android .slide-balance{{display:flex!important;align-items:flex-end!important;justify-content:space-between!important;gap:10px!important}}
    html.is-android .cards-carousel{{grid-auto-columns:92%!important}}
    html.is-android .cards-grid{{grid-template-columns:1fr!important}}
    html.is-android .tile{{min-height:0!important}}
    html.is-android .tile-row{{align-items:flex-start!important}}
    html.is-android .modal-backdrop{{backdrop-filter:none!important;-webkit-backdrop-filter:none!important;background:rgba(0,0,0,.84)!important}}
    html.is-android .modal-box{{background:#14151b!important;border-color:rgba(255,255,255,.26)!important;max-height:90vh!important}}
    html.is-android .modal-head{{background:#14151b!important;backdrop-filter:none!important;-webkit-backdrop-filter:none!important}}
    html.is-android .top-balance b{{color:#f0cc70!important;text-shadow:0 0 14px rgba(214,179,95,.44)!important}}

    @media (max-width: 430px){{
      html.is-android .top{{align-items:flex-start!important}}
      html.is-android .logo{{width:42px!important;height:42px!important;flex-basis:42px!important}}
      html.is-android .title{{font-size:20px!important}}
      html.is-android .subtitle{{font-size:11.5px!important}}
      html.is-android .top-balance{{min-width:122px!important}}
      html.is-android .top-balance b{{font-size:20px!important}}
      html.is-android .panel{{border-radius:24px!important}}
      html.is-android .nav{{grid-template-columns:repeat(4,minmax(0,1fr))!important;gap:6px!important;padding:8px!important}}
      html.is-android .nav-btn{{min-height:40px!important;font-size:10.6px!important;border-radius:13px!important;padding:0 4px!important}}
      html.is-android .tab{{padding:12px!important}}
      html.is-android .section-head{{display:grid!important;grid-template-columns:1fr!important;gap:8px!important}}
      html.is-android .head-actions{{width:100%!important}}
      html.is-android .view-switch{{width:100%!important;min-width:0!important}}
      html.is-android .view-switch-btn{{min-height:34px!important}}
      html.is-android .card-slide{{padding:15px!important;border-radius:24px!important;min-height:0!important}}
      html.is-android .slide-top{{display:grid!important;grid-template-columns:1fr auto!important}}
      html.is-android .slide-bank{{font-size:19px!important}}
      html.is-android .slide-icons{{gap:6px!important}}
      html.is-android .eye-btn,
      html.is-android .icon-btn{{width:31px!important;height:30px!important}}
      html.is-android .slide-body{{margin-top:22px!important;font-size:13px!important}}
      html.is-android .stat-grid,
      html.is-android .orders-mini-stats,
      html.is-android .quick-stats{{grid-template-columns:1fr 1fr!important;gap:8px!important}}
      html.is-android .stat-value,
      html.is-android .orders-mini-value,
      html.is-android .quick-value{{font-size:17px!important}}
      html.is-android .order-toggle{{grid-template-columns:minmax(0,1fr) auto!important}}
    }}

  </style>
        </head>
        <body><div class="wrap"><div class="card"><div class="bad">⛔</div><h2>Нет доступа</h2><p class="muted">Эта панель доступна только роли MasterCard или Admin.</p></div></div></body>
        </html>
        """
    )


def _page(title: str, body: str, header_amount: str = "") -> HTMLResponse:
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <title>{_esc(title)}</title>
  <script>
    (function(){{
      try {{
        document.documentElement.classList.add(/Android/i.test(navigator.userAgent) ? "is-android" : "not-android");
      }} catch(e) {{}}
    }})();
  </script>
  <style>
    :root {{
      --bg:#000000;
      --card:#14151a;
      --card2:#191a20;
      --card3:#202128;
      --line:rgba(255,255,255,.20);
      --line2:rgba(214,179,95,.26);
      --text:#f6f3ea;
      --muted:#a9acb4;
      --muted2:#747986;
      --accent:#d6b35f;
      --accent2:#e1c46f;
      --danger:#ff6969;
      --ok:#75e0a7;
    }}
    *{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
    html,body{{margin:0;width:100%;min-height:100%;background:#000;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;overflow-x:hidden}}
    body{{background:#000}}
    button,input{{font:inherit}}
    button{{cursor:pointer}}
    a{{color:inherit;text-decoration:none}}
    .page{{width:100%;min-height:100svh;background:#000;padding:10px 10px 24px}}
    .shell{{width:100%;max-width:520px;margin:0 auto}}
    .top{{display:flex;align-items:center;gap:12px;padding:8px 2px 12px}}
    .logo{{width:48px;height:48px;flex:0 0 48px;border-radius:17px;display:grid;place-items:center;border:1px solid var(--line2);background:linear-gradient(135deg,rgba(214,179,95,.16),rgba(255,255,255,.035));color:var(--accent);font-weight:950;letter-spacing:-1px}}
    .brand{{min-width:0;flex:1}}
    .title{{font-size:22px;line-height:1;font-weight:950;letter-spacing:-.35px}}
    .subtitle{{margin-top:5px;color:var(--muted);font-size:12.5px;line-height:1.25}}
    .top-balance{{
      flex:0 0 auto;
      min-width:132px;
      text-align:right;
      padding:0 1px 0 0;
    }}
    .top-balance span{{
      display:block;
      margin-bottom:4px;
      color:rgba(246,243,234,.62);
      font-size:10px;
      line-height:1;
      font-weight:950;
      text-transform:uppercase;
      letter-spacing:.42px;
      white-space:nowrap;
    }}
    .top-balance b{{
      display:block;
      color:var(--accent2);
      font-size:21px;
      line-height:.96;
      font-weight:1000;
      letter-spacing:-.35px;
      white-space:nowrap;
      text-shadow:0 0 12px rgba(214,179,95,.28);
    }}
    .panel{{border:1px solid var(--line);border-radius:28px;background:#101116;box-shadow:0 18px 54px rgba(0,0,0,.42);overflow:hidden}}
    .nav{{position:sticky;top:0;z-index:5;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;padding:10px;background:#101116;border-bottom:1px solid var(--line);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}}
    .nav-btn{{min-width:0;min-height:42px;border:1px solid rgba(255,255,255,.18);border-radius:15px;background:#1a1b21;color:#d1d3d8;font-size:11.5px;font-weight:950;line-height:1;display:flex;align-items:center;justify-content:center;text-align:center}}
    .nav-btn.active{{border-color:var(--line2);background:rgba(214,179,95,.13);color:var(--accent)}}
    .tab{{display:none;padding:14px}}
    .tab.active{{display:block}}
    .section-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin:3px 0 9px}}
    .section-title{{font-size:20px;line-height:1.1;font-weight:950;letter-spacing:-.3px}}
    .section-note{{margin-top:5px;color:var(--muted);font-size:12.5px;line-height:1.35}}
    .quick-stats{{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:13px}}
    .quick-stat{{padding:12px;border-radius:19px;border:1px solid var(--line);background:linear-gradient(180deg,#111114,#0c0c0e)}}
    .quick-label{{color:var(--muted2);font-size:10.5px;font-weight:950;text-transform:uppercase;letter-spacing:.35px}}
    .quick-value{{margin-top:6px;font-size:20px;line-height:1;font-weight:950;color:var(--accent)}}
    .head-actions{{display:flex;align-items:center;gap:8px;flex:0 0 auto}}
    .view-switch{{display:grid;grid-template-columns:1fr 1fr;min-width:132px;padding:3px;border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.035)}}
    .view-switch-btn{{min-height:30px;border:0;border-radius:12px;background:transparent;color:var(--muted);padding:0 9px;font-size:11.5px;font-weight:950;white-space:nowrap;transition:.18s ease}}
    .view-switch-btn.active{{background:linear-gradient(135deg,#e1c46f,#caa24e);color:#171209;box-shadow:0 8px 18px rgba(214,179,95,.13)}}
    .cards-carousel{{display:grid;grid-auto-flow:column;grid-auto-columns:88%;gap:12px;overflow-x:auto;scroll-snap-type:x mandatory;padding:2px 2px 14px;margin:0 -2px;scrollbar-width:none}}
    .cards-carousel::-webkit-scrollbar{{display:none}}
    .card-slide{{scroll-snap-align:center;min-width:0;position:relative;min-height:205px;padding:17px;border-radius:28px;border:1px solid rgba(255,255,255,.23);background:linear-gradient(145deg,#1f2027,#121318);overflow:hidden;box-shadow:0 16px 38px rgba(0,0,0,.30)}}
    .card-slide::before{{content:"";position:absolute;inset:-1px;background:radial-gradient(circle at 16% 0%,rgba(214,179,95,.18),transparent 42%);pointer-events:none}}
    .card-slide.off{{opacity:.66}}
    .slide-top,.slide-body,.slide-bottom{{position:relative}}
    .slide-top{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}}
    .slide-bank{{min-width:0;font-size:21px;line-height:1.1;font-weight:950;letter-spacing:-.35px;overflow-wrap:anywhere}}
    .slide-title-wrap{{min-width:0;display:grid;gap:8px}}
    .slide-icons{{display:flex;align-items:center;gap:7px;flex-wrap:wrap}}
    .icon-form{{margin:0;display:inline-flex}}
    .eye-btn,.icon-btn{{width:32px;height:30px;border:1px solid rgba(255,255,255,.065);border-radius:12px;background:rgba(255,255,255,.025);color:rgba(169,172,180,.78);font-size:13px;font-weight:850;line-height:1;display:grid;place-items:center;box-shadow:none}}
    .eye-btn.active{{border-color:rgba(214,179,95,.20);background:rgba(214,179,95,.055);color:rgba(214,179,95,.86)}}
    .icon-btn.power.on{{border-color:rgba(117,224,167,.16);background:rgba(117,224,167,.045);color:rgba(117,224,167,.82)}}
    .icon-btn.power.off{{border-color:rgba(255,105,105,.16);background:rgba(255,105,105,.045);color:rgba(255,105,105,.82)}}
    .icon-btn.trash{{border-color:rgba(255,255,255,.065);background:rgba(255,255,255,.018);color:rgba(255,209,209,.72)}}
    .slide-status{{flex:0 0 auto;padding:6px 9px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.04);color:var(--muted);font-size:10px;font-weight:950;text-transform:uppercase;letter-spacing:.35px}}
    .slide-status.on{{border-color:rgba(117,224,167,.24);background:rgba(117,224,167,.08);color:var(--ok)}}
    .slide-body{{display:grid;gap:8px;margin-top:28px;color:var(--muted);font-size:14px;font-weight:850}}
    .slide-line{{display:flex;justify-content:space-between;gap:12px;border-top:1px solid rgba(255,255,255,.06);padding-top:8px}}
    .slide-line b{{color:var(--text);font-weight:950;white-space:nowrap;overflow-wrap:anywhere}}
    .copy-secret{{display:none;align-items:center;gap:7px;justify-content:flex-end}}
    .requisites-visible .copy-secret{{display:inline-flex}}
    .copy-mini{{width:25px;height:25px;display:inline-grid;place-items:center;border:1px solid rgba(255,255,255,.07);border-radius:9px;background:rgba(255,255,255,.035);color:var(--muted);font-size:12px;line-height:1;cursor:pointer}}
    .copy-mini:active{{transform:scale(.96)}}
    .copy-mini.copied{{border-color:rgba(117,224,167,.28);background:rgba(117,224,167,.10);color:#d8ffe8}}
    .slide-limits{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:2px}}
    .limit-chip{{min-width:0;padding:8px 9px;border-radius:14px;border:1px solid rgba(255,255,255,.06);background:rgba(255,255,255,.025)}}
    .limit-label{{display:block;color:var(--muted2);font-size:9.5px;line-height:1;font-weight:950;text-transform:uppercase;letter-spacing:.32px}}
    .limit-value{{display:block;margin-top:5px;color:var(--text);font-size:12px;line-height:1.1;font-weight:950;white-space:nowrap}}
    .limit-warning{{margin-top:9px;padding:9px 10px;border-radius:15px;border:1px solid rgba(255,105,105,.18);background:rgba(255,105,105,.055);color:#ffd1d1;font-size:11.5px;line-height:1.35;font-weight:800}}
    .stat-log{{margin-top:12px;display:grid;gap:9px}}
    .log-title{{font-size:16px;line-height:1.1;font-weight:950;letter-spacing:-.2px;margin:2px 0 1px}}
    .log-card{{padding:11px 12px;border-radius:18px;border:1px solid var(--line);background:linear-gradient(180deg,#101012,#09090a)}}
    .log-top{{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}}
    .log-name{{font-size:13px;line-height:1.25;font-weight:950;color:var(--text)}}
    .log-time{{flex:0 0 auto;color:var(--muted2);font-size:10.5px;font-weight:900}}
    .log-text{{margin-top:5px;color:var(--muted);font-size:12px;line-height:1.35}}
    .log-diff{{margin-top:7px;color:var(--accent);font-size:12px;font-weight:950}}
    .log-alert{{border-color:rgba(255,105,105,.24);background:rgba(255,105,105,.045)}}
    .log-pager{{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:center;margin-top:2px}}
    .log-pager-info{{color:var(--muted);font-size:12px;font-weight:900;text-align:center;white-space:nowrap}}
    .log-pager a{{min-height:38px;border-radius:15px;display:flex;align-items:center;justify-content:center}}

    .secret-full{{display:none}}
    .requisites-visible .secret-mask{{display:none}}
    .requisites-visible .secret-full{{display:inline}}
    .slide-bottom{{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;margin-top:24px}}
    .slide-actions{{display:grid;grid-template-columns:1fr 1fr;gap:8px;min-width:154px}}
    .slide-action.withdraw{{border-color:rgba(214,179,95,.34);background:rgba(214,179,95,.12);color:var(--accent)}}
    .slide-balance{{display:grid;gap:4px}}
    .slide-balance-label{{color:var(--muted2);font-size:10.5px;line-height:1;font-weight:950;text-transform:uppercase;letter-spacing:.35px}}
    .slide-balance-value{{color:var(--accent);font-size:16px;line-height:1;font-weight:950;white-space:nowrap}}
    .slide-action{{min-height:36px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.045);color:var(--muted);padding:0 11px;font-size:12px;font-weight:950}}
    .cards-grid{{display:none;grid-template-columns:1fr 1fr;gap:10px}}
    .cards-grid.show{{display:grid}}
    .cards-carousel.hide{{display:none}}
    .tile{{min-width:0;position:relative;padding:13px;border-radius:22px;border:1px solid var(--line);background:linear-gradient(180deg,#111114,#0b0b0d);overflow:hidden;text-align:left;color:var(--text)}}
    .tile::before{{content:"";position:absolute;inset:0;background:radial-gradient(circle at 16% 0%,rgba(214,179,95,.11),transparent 40%);pointer-events:none}}
    .tile.off{{opacity:.62}}
    .tile-top{{position:relative;display:flex;align-items:flex-start;justify-content:space-between;gap:8px}}
    .tile-bank{{min-width:0;font-size:15px;line-height:1.15;font-weight:950;overflow-wrap:anywhere}}
    .dot{{width:9px;height:9px;flex:0 0 9px;margin-top:3px;border-radius:999px;background:var(--muted2)}}
    .dot.on{{background:var(--ok);box-shadow:0 0 0 4px rgba(117,224,167,.09)}}
    .tile-lines{{position:relative;display:grid;gap:4px;margin-top:12px;color:var(--muted);font-size:12px;line-height:1.25;font-weight:750}}
    .balance{{position:relative;margin-top:13px;color:var(--accent);font-size:16px;line-height:1;font-weight:950}}
    .empty{{padding:22px 16px;border-radius:24px;border:1px dashed var(--line2);background:rgba(214,179,95,.045);text-align:center}}
    .empty-title{{font-size:18px;font-weight:950}}
    .empty-text{{margin-top:8px;color:var(--muted);font-size:13px;line-height:1.45}}
    .edit-card,.stat-card{{border:1px solid var(--line);border-radius:24px;background:linear-gradient(180deg,#101012,#09090a);overflow:hidden}}
    .add-card{{padding:0;background:transparent;border:0}}
    .form{{display:grid;gap:11px}}
    .field-box{{display:grid;gap:10px;padding:12px;border-radius:19px;border:1px solid rgba(255,255,255,.065);background:rgba(255,255,255,.025)}}
    .box-title{{color:var(--accent);font-size:11px;font-weight:950;text-transform:uppercase;letter-spacing:.45px}}
    label{{display:grid;gap:6px;color:var(--muted);font-size:12px;font-weight:850}}
    input{{width:100%;min-height:45px;border:1px solid rgba(255,255,255,.09);border-radius:16px;background:#030303;color:var(--text);padding:11px 12px;outline:none;font-size:15px}}
    input:focus{{border-color:rgba(214,179,95,.50);box-shadow:0 0 0 3px rgba(214,179,95,.08)}}
    .two{{display:grid;grid-template-columns:1fr 1fr;gap:9px}}
    .btn{{width:100%;min-height:46px;border:0;border-radius:17px;background:linear-gradient(135deg,#e1c46f,#caa24e);color:#161108;font-size:14px;font-weight:950}}
    .btn:active{{transform:scale(.99)}}
    .btn.ghost{{border:1px solid var(--line);background:rgba(255,255,255,.045);color:var(--muted)}}
    .btn.danger{{border:1px solid rgba(255,105,105,.26);background:rgba(255,105,105,.11);color:#ffd1d1}}
    .help{{margin-top:10px;color:var(--muted2);font-size:12px;line-height:1.4}}
    .edit-list{{display:grid;gap:10px}}
    .edit-summary{{width:100%;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;border:0;background:transparent;color:var(--text);padding:14px;text-align:left}}
    .edit-name{{font-size:15.5px;font-weight:950;overflow-wrap:anywhere}}
    .edit-sub{{margin-top:4px;color:var(--muted);font-size:12px;line-height:1.3}}
    .edit-balance{{color:var(--accent);font-size:12px;font-weight:950;white-space:nowrap}}
    .edit-body{{display:none;padding:0 14px 14px}}
    .edit-card.open .edit-body{{display:block}}
    .form-actions{{display:grid;gap:9px;margin-top:2px}}
    .split-actions{{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:9px}}
    .modal-backdrop{{position:fixed;inset:0;z-index:50;display:none;align-items:flex-end;justify-content:center;padding:12px;background:rgba(0,0,0,.72);backdrop-filter:blur(10px)}}
    .modal-backdrop.open{{display:flex}}
    .modal-box{{width:100%;max-width:520px;max-height:88dvh;overflow:auto;border:1px solid rgba(255,255,255,.22);border-radius:28px;background:#121318;box-shadow:0 28px 70px rgba(0,0,0,.70)}}
    .modal-head{{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px;border-bottom:1px solid var(--line);background:#121318;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}}
    .modal-title{{font-size:18px;font-weight:950;line-height:1.1}}
    .modal-close{{width:38px;height:38px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.045);color:var(--muted);font-size:20px;line-height:1}}
    .modal-content{{padding:14px}}
    .modal-card-form{{display:none}}
    .modal-card-form.active{{display:block}}
    .withdraw-form{{display:none}}
    .withdraw-form.active{{display:block}}
    .withdraw-note{{color:var(--muted);font-size:12.5px;line-height:1.4;margin-bottom:10px}}
    .admin-deposit-mini{{
      flex:0 0 auto;
      min-height:30px;
      border:1px solid rgba(214,179,95,.20);
      border-radius:13px;
      background:rgba(214,179,95,.055);
      color:rgba(225,196,111,.86);
      display:inline-flex;
      align-items:center;
      justify-content:center;
      gap:5px;
      padding:0 9px;
      font-size:11px;
      line-height:1;
      font-weight:950;
      white-space:nowrap;
    }}
    .admin-deposit-mini span{{font-size:13px;line-height:1}}
    .admin-deposit-mini b{{font-size:10px;line-height:1;text-transform:uppercase;letter-spacing:.22px;color:rgba(246,243,234,.58)}}
    .admin-deposit-mini:active{{transform:scale(.98)}}
    .admin-deposit-box{{max-width:390px}}
    .stats-only{{display:grid;gap:13px}}
    .stat-block{{padding:13px;border-radius:22px;border:1px solid rgba(255,255,255,.15);background:linear-gradient(180deg,#16161a,#0d0d10)}}
    .stat-block-title{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:11px;font-size:14px;font-weight:950;color:var(--text)}}
    .stat-block-badge{{padding:5px 8px;border-radius:999px;border:1px solid rgba(214,179,95,.18);background:rgba(214,179,95,.06);color:var(--accent);font-size:10px;font-weight:950;text-transform:uppercase;letter-spacing:.35px}}
    .stats-admin-reset{{margin:0;flex:0 0 auto}}
    .stats-reset-btn{{min-height:34px;border:1px solid rgba(255,105,105,.26);border-radius:14px;background:rgba(255,105,105,.10);color:#ffd1d1;padding:0 12px;font-size:11.5px;font-weight:950;white-space:nowrap}}
    .stats-reset-btn:active{{transform:scale(.99)}}
    .stat-grid{{display:grid;grid-template-columns:1fr 1fr;gap:9px}}
    .stat-card{{min-width:0;padding:12px;border-radius:17px;border:1px solid rgba(255,255,255,.055);background:rgba(255,255,255,.024)}}
    .stat-label{{color:var(--muted2);font-size:10px;font-weight:950;text-transform:uppercase;letter-spacing:.35px;line-height:1.15}}
    .stat-value{{margin-top:7px;font-size:19px;line-height:1.08;font-weight:950;color:var(--accent);overflow-wrap:anywhere}}
    .orders-list{{display:grid;gap:8px}}
    .order-card{{border:1px solid rgba(255,255,255,.15);border-radius:18px;background:linear-gradient(180deg,#16161a,#0d0d10);overflow:hidden}}
    .order-toggle{{width:100%;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;padding:11px 12px;border:0;background:transparent;color:var(--text);text-align:left}}
    .order-main{{min-width:0}}
    .order-title{{font-size:13.5px;line-height:1.15;font-weight:950;color:var(--text)}}
    .order-meta{{margin-top:4px;color:var(--muted);font-size:11.5px;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .order-side{{text-align:right}}
    .order-sum{{color:var(--accent);font-size:13px;font-weight:950;white-space:nowrap}}
    .order-profit{{margin-top:4px;color:var(--ok);font-size:11px;font-weight:900;white-space:nowrap}}
    .order-details{{display:none;margin:0 10px 10px;padding:10px;border-radius:15px;border:1px solid rgba(255,255,255,.055);background:rgba(255,255,255,.025);animation:detailsDrop .18s ease-out both}}
    .order-card.open .order-details{{display:grid;gap:8px}}
    .order-detail-row{{display:flex;justify-content:space-between;gap:12px;color:var(--muted);font-size:11.5px;line-height:1.25}}
    .order-detail-row b{{color:var(--text);font-size:12px;text-align:right;overflow-wrap:anywhere}}
    @keyframes detailsDrop{{from{{opacity:0;transform:translateY(-6px)}}to{{opacity:1;transform:translateY(0)}}}}
    .orders-mini-stats{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}}
    .orders-mini{{padding:10px;border-radius:17px;border:1px solid var(--line);background:rgba(255,255,255,.025)}}
    .orders-mini-label{{color:var(--muted2);font-size:9.5px;font-weight:950;text-transform:uppercase;letter-spacing:.32px}}
    .orders-mini-value{{margin-top:5px;color:var(--accent);font-size:18px;font-weight:950;line-height:1}}
    @media (max-width: 520px) {{
      html{{-webkit-text-size-adjust:100%;text-size-adjust:100%}}
      .page{{min-height:100vh;min-height:100dvh;padding:10px 10px 22px}}
      .top{{align-items:flex-start;gap:10px;padding:8px 1px 12px}}
      .logo{{width:44px;height:44px;flex-basis:44px;border-radius:15px}}
      .title{{font-size:21px}}
      .subtitle{{font-size:12px}}
      .top-balance{{min-width:122px;padding-top:1px}}
      .top-balance span{{font-size:9.2px;margin-bottom:5px;color:rgba(246,243,234,.68)}}
      .top-balance b{{font-size:20px;color:#e6c76e;text-shadow:0 0 11px rgba(214,179,95,.34)}}
      .panel{{border-color:rgba(255,255,255,.16);background:#0b0b0d}}
      .tab{{padding:13px}}
      .section-head{{align-items:center;gap:8px}}
      .section-title{{font-size:19px}}
      .cards-carousel{{grid-auto-columns:90%;gap:11px;padding-bottom:13px}}
      .card-slide{{border-color:rgba(255,255,255,.17);background:linear-gradient(145deg,#19191d,#0d0d10)}}
      .slide-line{{align-items:flex-start}}
      .slide-line span{{min-width:0}}
      .slide-line b{{max-width:62%;text-align:right;white-space:normal;word-break:break-word}}
      .slide-bottom{{align-items:flex-end}}
      .slide-actions{{min-width:150px}}
      .tile,.form-box,.edit-card,.stat-block,.order-card,.log-card{{border-color:rgba(255,255,255,.22);background:linear-gradient(180deg,#1c1d24,#111217)}}
      input{{font-size:16px;min-height:47px;background:#050506;border-color:rgba(255,255,255,.16)}}
      label{{font-size:11px}}
      .primary,.ghost,.danger-btn,.slide-action{{min-height:43px;display:flex;align-items:center;justify-content:center;text-align:center}}
      .stat-grid,.orders-mini-stats{{gap:8px}}
      .stat-card,.orders-mini{{min-width:0;padding:11px 9px}}
      .stat-value,.orders-mini-value{{font-size:17px;line-height:1.1}}
      .order-toggle{{grid-template-columns:minmax(0,1fr) minmax(86px,auto);padding:12px}}
      .order-side{{min-width:86px}}
    }}

    @media(max-width:390px){{
      .page{{padding:8px 8px 20px}}
      .nav{{gap:6px;padding:9px}}
      .nav-btn{{font-size:10.5px;min-height:38px;border-radius:14px}}
      .tab{{padding:12px}}
      .cards-carousel{{grid-auto-columns:91%;gap:10px}}
      .card-slide{{min-height:198px;padding:15px;border-radius:25px}}
      .slide-bank{{font-size:19px}}
      .cards-grid{{gap:8px}}
      .tile{{padding:11px;border-radius:20px}}
      .tile-bank{{font-size:14px}}
      .two,.split-actions{{grid-template-columns:1fr}}
      .section-head{{align-items:center}}
      .view-switch{{min-width:126px}}
      .view-switch-btn{{font-size:11px;padding:0 7px}}
      .top-balance{{min-width:112px}}
      .top-balance span{{font-size:8.5px}}
      .top-balance b{{font-size:19px}}
    }}


    /* Android Chrome-safe version: brighter surfaces, no fragile blur, stable rows. */
    html.is-android body{{background:#000!important;color:#f7f4ec!important}}
    html.is-android .page{{min-height:100vh!important;min-height:100dvh!important;padding:10px 10px 24px!important}}
    html.is-android .shell{{max-width:540px}}
    html.is-android .panel{{background:#111217!important;border-color:rgba(255,255,255,.24)!important;box-shadow:none!important}}
    html.is-android .nav{{background:#111217!important;backdrop-filter:none!important;-webkit-backdrop-filter:none!important;border-color:rgba(255,255,255,.22)!important}}
    html.is-android .nav-btn{{background:#1d1e25!important;border-color:rgba(255,255,255,.20)!important;color:#dedfe4!important}}
    html.is-android .nav-btn.active{{background:#2a2417!important;border-color:rgba(214,179,95,.48)!important;color:#e7c66c!important}}
    html.is-android .card-slide{{background:linear-gradient(145deg,#23242c,#15161c)!important;border-color:rgba(255,255,255,.26)!important;box-shadow:none!important}}
    html.is-android .card-slide::before{{opacity:.75}}
    html.is-android .tile,
    html.is-android .form-box,
    html.is-android .edit-card,
    html.is-android .stat-block,
    html.is-android .order-card,
    html.is-android .log-card{{background:#171820!important;border-color:rgba(255,255,255,.24)!important;box-shadow:none!important}}
    html.is-android .quick-stat,
    html.is-android .stat-card,
    html.is-android .orders-mini,
    html.is-android .limit-chip,
    html.is-android .order-details{{background:#202129!important;border-color:rgba(255,255,255,.18)!important}}
    html.is-android input{{background:#0b0c10!important;border-color:rgba(255,255,255,.24)!important;color:#fff!important}}
    html.is-android label,
    html.is-android .stat-label,
    html.is-android .quick-label,
    html.is-android .orders-mini-label,
    html.is-android .limit-label{{color:#9fa3ad!important}}
    html.is-android .section-note,
    html.is-android .subtitle,
    html.is-android .order-meta,
    html.is-android .log-text{{color:#b6b9c1!important}}
    html.is-android .slide-line{{display:grid!important;grid-template-columns:auto minmax(0,1fr)!important;align-items:center!important;gap:12px!important}}
    html.is-android .slide-line b{{max-width:100%!important;text-align:right!important;white-space:normal!important;word-break:break-word!important}}
    html.is-android .slide-limits{{gap:9px!important}}
    html.is-android .slide-bottom{{display:grid!important;grid-template-columns:1fr!important;gap:12px!important;align-items:stretch!important}}
    html.is-android .slide-actions{{width:100%!important;min-width:0!important;grid-template-columns:1fr 1fr!important}}
    html.is-android .slide-action{{min-height:44px!important}}
    html.is-android .slide-balance{{display:flex!important;align-items:flex-end!important;justify-content:space-between!important;gap:10px!important}}
    html.is-android .cards-carousel{{grid-auto-columns:92%!important}}
    html.is-android .cards-grid{{grid-template-columns:1fr!important}}
    html.is-android .tile{{min-height:0!important}}
    html.is-android .tile-row{{align-items:flex-start!important}}
    html.is-android .modal-backdrop{{backdrop-filter:none!important;-webkit-backdrop-filter:none!important;background:rgba(0,0,0,.84)!important}}
    html.is-android .modal-box{{background:#14151b!important;border-color:rgba(255,255,255,.26)!important;max-height:90vh!important}}
    html.is-android .modal-head{{background:#14151b!important;backdrop-filter:none!important;-webkit-backdrop-filter:none!important}}
    html.is-android .top-balance b{{color:#f0cc70!important;text-shadow:0 0 14px rgba(214,179,95,.44)!important}}

    @media (max-width: 430px){{
      html.is-android .top{{align-items:flex-start!important}}
      html.is-android .logo{{width:42px!important;height:42px!important;flex-basis:42px!important}}
      html.is-android .title{{font-size:20px!important}}
      html.is-android .subtitle{{font-size:11.5px!important}}
      html.is-android .top-balance{{min-width:122px!important}}
      html.is-android .top-balance b{{font-size:20px!important}}
      html.is-android .panel{{border-radius:24px!important}}
      html.is-android .nav{{grid-template-columns:repeat(4,minmax(0,1fr))!important;gap:6px!important;padding:8px!important}}
      html.is-android .nav-btn{{min-height:40px!important;font-size:10.6px!important;border-radius:13px!important;padding:0 4px!important}}
      html.is-android .tab{{padding:12px!important}}
      html.is-android .section-head{{display:grid!important;grid-template-columns:1fr!important;gap:8px!important}}
      html.is-android .head-actions{{width:100%!important}}
      html.is-android .view-switch{{width:100%!important;min-width:0!important}}
      html.is-android .view-switch-btn{{min-height:34px!important}}
      html.is-android .card-slide{{padding:15px!important;border-radius:24px!important;min-height:0!important}}
      html.is-android .slide-top{{display:grid!important;grid-template-columns:1fr auto!important}}
      html.is-android .slide-bank{{font-size:19px!important}}
      html.is-android .slide-icons{{gap:6px!important}}
      html.is-android .eye-btn,
      html.is-android .icon-btn{{width:31px!important;height:30px!important}}
      html.is-android .slide-body{{margin-top:22px!important;font-size:13px!important}}
      html.is-android .stat-grid,
      html.is-android .orders-mini-stats,
      html.is-android .quick-stats{{grid-template-columns:1fr 1fr!important;gap:8px!important}}
      html.is-android .stat-value,
      html.is-android .orders-mini-value,
      html.is-android .quick-value{{font-size:17px!important}}
      html.is-android .order-toggle{{grid-template-columns:minmax(0,1fr) auto!important}}
    }}

  </style>
</head>
<body>
  <main class="page">
    <div class="shell">
      <header class="top">
        <div class="logo">MC</div>
        <div class="brand">
          <div class="title">MasterCard</div>
          <div class="subtitle">Управление картами</div>
        </div>
        <div class="top-balance" aria-label="Баланс карт и депозит">
          <span>Баланс / депозит</span>
          <b>{_esc(header_amount)}</b>
        </div>
      </header>
      {body}
    </div>
  </main>
  <script>
    (function() {{
      function showTab(name) {{
        document.querySelectorAll('.tab').forEach(function(el) {{ el.classList.remove('active'); }});
        document.querySelectorAll('.nav-btn').forEach(function(el) {{ el.classList.remove('active'); }});
        var tab = document.getElementById('tab-' + name);
        var btn = document.querySelector('[data-tab="' + name + '"]');
        if (tab) tab.classList.add('active');
        if (btn) btn.classList.add('active');
        if (history.replaceState) history.replaceState(null, '', '#' + name);
      }}
      document.querySelectorAll('[data-tab]').forEach(function(btn) {{
        btn.addEventListener('click', function() {{ showTab(btn.getAttribute('data-tab') || 'cards'); }});
      }});
      function setCardsView(mode, cardId) {{
        var carousel = document.getElementById('cardsCarousel');
        var grid = document.getElementById('cardsGrid');
        if (!carousel || !grid) return;
        var isGrid = mode === 'grid';
        grid.classList.toggle('show', isGrid);
        carousel.classList.toggle('hide', isGrid);
        document.querySelectorAll('[data-view-mode]').forEach(function(btn) {{
          btn.classList.toggle('active', btn.getAttribute('data-view-mode') === mode);
        }});
        if (!isGrid && cardId) {{
          var slide = document.querySelector('.card-slide[data-card-id="' + cardId + '"]');
          if (slide && slide.scrollIntoView) {{
            setTimeout(function() {{ slide.scrollIntoView({{behavior:'smooth', inline:'center', block:'nearest'}}); }}, 40);
          }}
        }}
      }}
      document.querySelectorAll('[data-view-mode]').forEach(function(btn) {{
        btn.addEventListener('click', function() {{ setCardsView(btn.getAttribute('data-view-mode') || 'swipe'); }});
      }});
      document.querySelectorAll('[data-open-slide]').forEach(function(tile) {{
        tile.addEventListener('click', function() {{ setCardsView('swipe', tile.getAttribute('data-open-slide') || ''); }});
      }});
      document.querySelectorAll('[data-toggle-requisites]').forEach(function(btn) {{
        btn.addEventListener('click', function(event) {{
          event.preventDefault();
          event.stopPropagation();
          var card = btn.closest('.card-slide');
          if (!card) return;
          var visible = card.classList.toggle('requisites-visible');
          btn.classList.toggle('active', visible);
          btn.textContent = visible ? '👁' : '🙈';
          btn.setAttribute('aria-label', visible ? 'Скрыть реквизиты' : 'Показать реквизиты');
        }});
      }});
      function copyText(value, btn) {{
        value = String(value || '').trim();
        if (!value) return;
        function done() {{
          if (!btn) return;
          btn.classList.add('copied');
          var old = btn.textContent;
          btn.textContent = '✓';
          setTimeout(function() {{ btn.classList.remove('copied'); btn.textContent = old || '⧉'; }}, 900);
        }}
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          navigator.clipboard.writeText(value).then(done).catch(function() {{}});
        }} else {{
          var area = document.createElement('textarea');
          area.value = value;
          area.style.position = 'fixed';
          area.style.left = '-9999px';
          document.body.appendChild(area);
          area.focus();
          area.select();
          try {{ document.execCommand('copy'); done(); }} catch (e) {{}}
          document.body.removeChild(area);
        }}
      }}
      document.querySelectorAll('[data-copy-secret]').forEach(function(btn) {{
        btn.addEventListener('click', function(event) {{
          event.preventDefault();
          event.stopPropagation();
          copyText(btn.getAttribute('data-copy-secret') || '', btn);
        }});
      }});
      var modal = document.getElementById('editModal');
      var modalTitle = document.getElementById('editModalTitle');
      var withdrawModal = document.getElementById('withdrawModal');
      var withdrawModalTitle = document.getElementById('withdrawModalTitle');
      var depositModal = document.getElementById('depositModal');
      function openDepositModal() {{
        if (!depositModal) return;
        depositModal.classList.add('open');
        document.body.style.overflow = 'hidden';
        var input = depositModal.querySelector('input[name="deposit_rub"]');
        if (input) setTimeout(function() {{ input.focus(); input.select(); }}, 120);
      }}
      function closeDepositModal() {{
        if (!depositModal) return;
        depositModal.classList.remove('open');
        document.body.style.overflow = '';
      }}
      function openEditModal(cardId, title) {{
        if (!modal) return;
        document.querySelectorAll('.modal-card-form').forEach(function(item) {{ item.classList.remove('active'); }});
        var form = document.getElementById('modal-edit-card-' + cardId);
        if (!form) return;
        form.classList.add('active');
        if (modalTitle) modalTitle.textContent = title || 'Редактировать карту';
        modal.classList.add('open');
        document.body.style.overflow = 'hidden';
      }}
      function closeEditModal() {{
        if (!modal) return;
        modal.classList.remove('open');
        document.body.style.overflow = '';
      }}
      document.querySelectorAll('[data-edit-modal]').forEach(function(btn) {{
        btn.addEventListener('click', function(event) {{
          event.preventDefault();
          event.stopPropagation();
          openEditModal(btn.getAttribute('data-edit-modal') || '', btn.getAttribute('data-edit-title') || 'Редактировать карту');
        }});
      }});
      document.querySelectorAll('[data-open-edit]').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          var box = document.getElementById(btn.getAttribute('data-open-edit') || '');
          if (!box) return;
          document.querySelectorAll('.edit-card').forEach(function(item) {{ if (item !== box) item.classList.remove('open'); }});
          box.classList.toggle('open');
        }});
      }});
      function openWithdrawModal(cardId, title) {{
        if (!withdrawModal) return;
        document.querySelectorAll('.withdraw-form').forEach(function(item) {{ item.classList.remove('active'); }});
        var form = document.getElementById('withdraw-card-' + cardId);
        if (!form) return;
        form.classList.add('active');
        if (withdrawModalTitle) withdrawModalTitle.textContent = 'Вывод · ' + (title || 'карта');
        withdrawModal.classList.add('open');
        document.body.style.overflow = 'hidden';
        var input = form.querySelector('input[name="amount"]');
        if (input) setTimeout(function() {{ input.focus(); }}, 120);
      }}
      function closeWithdrawModal() {{
        if (!withdrawModal) return;
        withdrawModal.classList.remove('open');
        document.body.style.overflow = '';
      }}
      document.querySelectorAll('[data-withdraw-modal]').forEach(function(btn) {{
        btn.addEventListener('click', function(event) {{
          event.preventDefault();
          event.stopPropagation();
          openWithdrawModal(btn.getAttribute('data-withdraw-modal') || '', btn.getAttribute('data-withdraw-title') || 'карта');
        }});
      }});
      document.querySelectorAll('[data-modal-close]').forEach(function(btn) {{ btn.addEventListener('click', closeEditModal); }});
      document.querySelectorAll('[data-withdraw-close]').forEach(function(btn) {{ btn.addEventListener('click', closeWithdrawModal); }});
      document.querySelectorAll('[data-deposit-open]').forEach(function(btn) {{ btn.addEventListener('click', openDepositModal); }});
      document.querySelectorAll('[data-deposit-close]').forEach(function(btn) {{ btn.addEventListener('click', closeDepositModal); }});
      if (modal) modal.addEventListener('click', function(event) {{ if (event.target === modal) closeEditModal(); }});
      if (withdrawModal) withdrawModal.addEventListener('click', function(event) {{ if (event.target === withdrawModal) closeWithdrawModal(); }});
      if (depositModal) depositModal.addEventListener('click', function(event) {{ if (event.target === depositModal) closeDepositModal(); }});
      document.addEventListener('keydown', function(event) {{ if (event.key === 'Escape') {{ closeEditModal(); closeWithdrawModal(); closeDepositModal(); }} }});
      function onlyDigits(value) {{ return String(value || '').replace(/\\D/g, ''); }}
      function formatCardInput(input) {{
        var digits = onlyDigits(input.value).slice(0, 16);
        var parts = [];
        for (var i = 0; i < digits.length; i += 4) parts.push(digits.slice(i, i + 4));
        var formatted = parts.join(' ');
        if (digits.length > 0 && digits.length % 4 === 0 && digits.length < 16) formatted += ' ';
        input.value = formatted;
      }}
      function formatSbpInput(input) {{
        var digits = onlyDigits(input.value);
        if (digits.indexOf('8') === 0) digits = '7' + digits.slice(1);
        if (digits.indexOf('7') !== 0) digits = '7' + digits;
        digits = digits.slice(0, 11);
        input.value = '+' + digits;
      }}
      document.querySelectorAll('input[name="card_number"]').forEach(function(input) {{
        formatCardInput(input);
        input.addEventListener('input', function() {{ formatCardInput(input); }});
      }});
      document.querySelectorAll('input[name="sbp_phone"]').forEach(function(input) {{
        if (!input.value) input.value = '+7';
        input.addEventListener('focus', function() {{ if (!input.value) input.value = '+7'; }});
        input.addEventListener('input', function() {{ formatSbpInput(input); }});
      }});
      var hash = String(window.location.hash || '').replace('#','');
      if (hash.indexOf('card-') === 0) {{
        showTab('cards');
        setCardsView('swipe', hash.replace('card-', ''));
      }} else if (['cards','add','orders','stats'].indexOf(hash) >= 0) {{
        showTab(hash);
      }}
    }})();
  </script>
</body>
</html>
        """
    )


@router.get("", response_class=HTMLResponse)
async def mastercard_home(request: Request, user_id: int) -> HTMLResponse:
    if not await _is_mastercard_user(user_id):
        return await _render_access_denied()

    admin_id: Optional[int] = None
    admin_mode = False
    try:
        admin_id_raw = request.query_params.get("admin_id") or ""
        admin_id = int(admin_id_raw) if admin_id_raw else None
    except Exception:
        admin_id = None

    if admin_id and await _is_admin_user(admin_id):
        admin_mode = True
    else:
        admin_id = None

    admin_hidden_input = (
        f'<input type="hidden" name="admin_id" value="{int(admin_id)}">'
        if admin_mode and admin_id else ""
    )
    stats_reset_form = (
        f"""
        <form class="stats-admin-reset" method="post" action="/mastercard/cards/reset"
              onsubmit="return confirm('Сбросить всю статистику и балансы по картам этого кабинета? Карты и настройки останутся.');">
          <input type="hidden" name="user_id" value="{int(user_id)}">
          <input type="hidden" name="admin_id" value="{int(admin_id)}">
          <button class="stats-reset-btn" type="submit">Сбросить</button>
        </form>
        """
        if admin_mode and admin_id else ""
    )
    balance_admin_field_template = (
        '<div class="field-box">'
        '<div class="box-title">Админ</div>'
        '<label>Баланс карты, руб.<input name="target_balance" inputmode="decimal" value="{balance_raw}"></label>'
        '<div class="help">Поле видно только админу. После сохранения баланс сразу пересчитывается на карте.</div>'
        '</div>'
        if admin_mode else ""
    )

    cards = await get_cards_by_owner(user_id)
    completed_orders = await get_completed_orders_by_master(user_id)
    mastercard_deposit = float(await get_user_mastercard_deposit(int(user_id)) or 0.0)

    owned_card_ids = {
        int(card.get("card_id") or 0)
        for card in cards
        if int(card.get("card_id") or 0) > 0
    }
    if owned_card_ids:
        completed_orders = [
            order for order in completed_orders
            if int(order.get("card_id") or 0) in owned_card_ids
        ]
    else:
        completed_orders = []

    today_order_stats: dict[int, dict[str, Any]] = {}
    total_completed_amount = 0.0
    total_profit = 0.0
    today_completed_count = 0
    today_completed_amount = 0.0
    today_profit = 0.0

    for order in completed_orders:
        try:
            total_rub = float(order.get("total_rub") or 0)
        except Exception:
            total_rub = 0.0
        try:
            rub_amount = float(order.get("rub_amount") or 0)
        except Exception:
            rub_amount = 0.0

        order_profit_value = total_rub * 0.09
        total_completed_amount += total_rub
        total_profit += order_profit_value

        when = order.get("completed_at") or order.get("created_at")
        if _is_today_nsk(when):
            today_completed_count += 1
            today_completed_amount += total_rub
            today_profit += order_profit_value
            try:
                card_id_for_order = int(order.get("card_id") or 0)
            except Exception:
                card_id_for_order = 0
            if card_id_for_order > 0:
                item = today_order_stats.setdefault(card_id_for_order, {"count": 0, "sum": 0.0, "last": None})
                item["count"] = int(item.get("count") or 0) + 1
                item["sum"] = float(item.get("sum") or 0.0) + total_rub
                order_dt = _to_nsk_datetime(when)
                if order_dt:
                    prev_last = item.get("last")
                    if not isinstance(prev_last, datetime) or order_dt > prev_last:
                        item["last"] = order_dt

    enriched_cards: list[dict[str, Any]] = []
    total_balance = 0.0
    active_count = 0

    for raw_card in cards:
        card = dict(raw_card)
        card_id = int(card.get("card_id") or 0)
        balance = await get_card_balance(card_id)
        card["_balance"] = balance
        card["_today_count"] = int(today_order_stats.get(card_id, {}).get("count") or 0)
        card["_today_sum"] = float(today_order_stats.get(card_id, {}).get("sum") or 0.0)
        card["_last_completed_nsk"] = today_order_stats.get(card_id, {}).get("last")

        is_blocked, block_reason, block_until = _limit_state_for_card(card)
        card["_limit_blocked"] = is_blocked
        card["_limit_reason"] = block_reason
        card["_limit_until"] = block_until

        if is_blocked and bool(card.get("is_active", True)):
            await set_card_active(card_id=card_id, owner_id=int(user_id), is_active=False)
            await _set_limit_lock(int(user_id), card_id, block_reason, block_until)
            await _log_card_audit(
                owner_id=int(user_id),
                card_id=card_id,
                action="limit_off",
                title="Карта выключена лимитом",
                details=f"{block_reason}. До: {block_until}",
            )
            card["is_active"] = False
        elif not is_blocked and await _has_limit_lock(card_id):
            await set_card_active(card_id=card_id, owner_id=int(user_id), is_active=True)
            await _clear_limit_lock(card_id)
            await _log_card_audit(
                owner_id=int(user_id),
                card_id=card_id,
                action="limit_on",
                title="Карта включена после лимита",
                details="Ограничение прошло автоматически.",
            )
            card["is_active"] = True

        total_balance += float(balance or 0)
        if bool(card.get("is_active", True)):
            active_count += 1
        enriched_cards.append(card)

    inactive_count = max(len(enriched_cards) - active_count, 0)
    deposit_left = max(float(mastercard_deposit or 0.0) - float(total_balance or 0.0), 0.0)
    deposit_progress_text = (
        f"{_fmt_compact_money(total_balance)} / {_fmt_compact_money(mastercard_deposit)}"
        if mastercard_deposit > 0 else
        f"{_fmt_compact_money(total_balance)} / 0"
    )
    deposit_status_text = (
        "Депозит достигнут — карты не будут показываться в VidraPay."
        if mastercard_deposit > 0 and total_balance >= mastercard_deposit else
        "Новые заявки будут учитываться заранее: баланс карт + сумма заявки не должен достигать депозит."
        if mastercard_deposit > 0 else
        "Депозит не задан — карты не будут показываться в VidraPay до установки депозита."
    )
    deposit_panel_html = ""
    admin_deposit_button_html = ""
    admin_deposit_modal_html = ""
    if admin_mode and admin_id:
        admin_deposit_button_html = f"""
          <button class="admin-deposit-mini" type="button" data-deposit-open="1" aria-label="Установить депозит Mastercard">
            <span>₽</span>
            <b>депозит</b>
          </button>
        """
        admin_deposit_modal_html = f"""
          <div class="modal-backdrop" id="depositModal" aria-hidden="true">
            <div class="modal-box admin-deposit-box" role="dialog" aria-modal="true">
              <div class="modal-head">
                <div class="modal-title">Депозит Mastercard</div>
                <button class="modal-close" type="button" data-deposit-close="1" aria-label="Закрыть">×</button>
              </div>
              <div class="modal-content">
                <form class="form" method="post" action="/mastercard/deposit/update">
                  <input type="hidden" name="user_id" value="{int(user_id)}">
                  <input type="hidden" name="admin_id" value="{int(admin_id)}">
                  <div class="field-box">
                    <div class="box-title">Админ</div>
                    <label>Депозит, руб.<input name="deposit_rub" inputmode="decimal" value="{_fmt_compact_money(mastercard_deposit)}" placeholder="Например: 20000" required></label>
                    <div class="help">Видно только админу. VidraPay будет скрывать карты, если баланс карт + сумма новой заявки достигает депозита.</div>
                  </div>
                  <div class="quick-stats" style="margin-bottom:0">
                    <div class="quick-stat"><div class="quick-label">Баланс / депозит</div><div class="quick-value">{_esc(deposit_progress_text)}</div></div>
                    <div class="quick-stat"><div class="quick-label">Остаток</div><div class="quick-value">{_fmt_money(deposit_left)}</div></div>
                  </div>
                  <div class="form-actions" style="margin-top:11px">
                    <button class="btn" type="submit">Сохранить</button>
                  </div>
                </form>
              </div>
            </div>
          </div>
        """

    if enriched_cards:
        tiles_html = ""
        carousel_html = ""
        edit_html = ""
        modal_edit_html = ""
        withdraw_modal_html = ""
        for card in enriched_cards:
            card_id = int(card.get("card_id") or 0)
            is_active = bool(card.get("is_active", True))
            bank_name = _esc(card.get("bank_name") or "Банк")
            card_number = _short_number(card.get("card_number"))
            card_last4 = _last4(card.get("card_number"))
            sbp_last4 = _last4(card.get("sbp_phone"))
            full_card_number = _format_card_groups(card.get("card_number"))
            full_sbp_phone = _esc(card.get("sbp_phone") or "—")
            balance = _fmt_money(card.get("_balance"))
            today_count = int(card.get("_today_count") or 0)
            today_sum = float(card.get("_today_sum") or 0.0)
            transfer_limit = int(card.get("daily_transfer_limit") or 0)
            daily_limit = float(card.get("daily_limit_rub") or 0)
            transfers_text = f"{today_count}/{transfer_limit}" if transfer_limit > 0 else f"{today_count}/∞"
            daily_limit_text = _fmt_compact_money(daily_limit) if daily_limit > 0 else "∞"
            rub_limit_text = f"{_fmt_compact_money(today_sum)}/{daily_limit_text}"
            limit_blocked = bool(card.get("_limit_blocked"))
            limit_reason = _esc(card.get("_limit_reason") or "")
            limit_until = _esc(card.get("_limit_until") or "")
            limit_warning_html = (
                f'<div class="limit-warning">Лимит: {limit_reason}. Можно включить после {limit_until}.</div>'
                if limit_blocked else ""
            )

            carousel_html += f"""
              <article id="card-{card_id}" class="card-slide {'off' if not is_active else ''}" data-card-id="{card_id}">
                <div class="slide-top">
                  <div class="slide-title-wrap">
                    <div class="slide-bank">{bank_name}</div>
                    <div class="slide-icons">
                      <button class="eye-btn" type="button" data-toggle-requisites="{card_id}" aria-label="Показать реквизиты">🙈</button>
                      <form class="icon-form" method="post" action="/mastercard/card/delete" onsubmit="return confirm('Удалить карту {bank_name}?')">
                        <input type="hidden" name="user_id" value="{int(user_id)}">
                        {admin_hidden_input}
                        <input type="hidden" name="card_id" value="{card_id}">
                        <button class="icon-btn trash" type="submit" aria-label="Удалить карту">🗑️</button>
                      </form>
                      <form class="icon-form" method="post" action="/mastercard/card/toggle">
                        <input type="hidden" name="user_id" value="{int(user_id)}">
                        {admin_hidden_input}
                        <input type="hidden" name="card_id" value="{card_id}">
                        <button class="icon-btn power {'on' if is_active else 'off'}" type="submit" aria-label="{'Выключить карту' if is_active else 'Включить карту'}">⏻</button>
                      </form>
                    </div>
                  </div>
                  <div class="slide-status {'on' if is_active else ''}">{'активна' if is_active else 'выкл'}</div>
                </div>
                <div class="slide-body">
                  <div class="slide-line"><span>Карта</span><b><span class="secret-mask">•••• {card_last4}</span><span class="copy-secret"><button class="copy-mini" type="button" data-copy-secret="{full_card_number}" aria-label="Скопировать номер карты">⧉</button><span>{full_card_number}</span></span></b></div>
                  <div class="slide-line"><span>СБП</span><b><span class="secret-mask">•••• {sbp_last4}</span><span class="copy-secret"><button class="copy-mini" type="button" data-copy-secret="{full_sbp_phone}" aria-label="Скопировать СБП">⧉</button><span>{full_sbp_phone}</span></span></b></div>
                  <div class="slide-limits">
                    <div class="limit-chip"><span class="limit-label">Переводы / сутки</span><span class="limit-value">{transfers_text}</span></div>
                    <div class="limit-chip"><span class="limit-label">Лимит / сутки</span><span class="limit-value">{rub_limit_text}</span></div>
                  </div>
                  {limit_warning_html}
                </div>
                <div class="slide-bottom">
                  <div class="slide-balance"><span class="slide-balance-label">Баланс</span><span class="slide-balance-value">{balance}</span></div>
                  <div class="slide-actions">
                    <button class="slide-action withdraw" type="button" data-withdraw-modal="{card_id}" data-withdraw-title="{bank_name}" data-withdraw-balance="{balance}">Вывод</button>
                    <button class="slide-action" type="button" data-edit-modal="{card_id}" data-edit-title="{bank_name}">Править</button>
                  </div>
                </div>
              </article>
            """

            tiles_html += f"""
              <button class="tile {'off' if not is_active else ''}" type="button" data-open-slide="{card_id}">
                <div class="tile-top">
                  <div class="tile-bank">{bank_name}</div>
                  <div class="dot {'on' if is_active else ''}" title="{'Активна' if is_active else 'Выключена'}"></div>
                </div>
                <div class="tile-lines">
                  <div>Карта: •••• {card_last4}</div>
                  <div>СБП: •••• {sbp_last4}</div>
                </div>
                <div class="balance">{balance}</div>
              </button>
            """

            edit_html += f"""
              <div class="edit-card" id="edit-card-{card_id}">
                <button class="edit-summary" type="button" data-open-edit="edit-card-{card_id}">
                  <div>
                    <div class="edit-name">{bank_name}</div>
                    <div class="edit-sub">{card_number} · {'активна' if is_active else 'выключена'}</div>
                  </div>
                  <div class="edit-balance">{balance}</div>
                </button>
                <div class="edit-body">
                  <form class="form" method="post" action="/mastercard/card/update">
                    <input type="hidden" name="user_id" value="{int(user_id)}">
                        {admin_hidden_input}
                    <input type="hidden" name="card_id" value="{card_id}">
                    <div class="field-box">
                      <div class="box-title">Реквизиты</div>
                      <label>Банк<input name="bank_name" value="{_esc(card.get('bank_name'))}" required></label>
                      <label>СБП / телефон<input name="sbp_phone" inputmode="tel" value="{_esc(card.get('sbp_phone'))}" placeholder="+79991234567"></label>
                      <label>Номер карты<input name="card_number" inputmode="numeric" value="{_format_card_groups(card.get('card_number')) if card.get('card_number') else ''}" placeholder="0000 0000 0000 0000"></label>
                    </div>
                    <div class="field-box">
                      <div class="box-title">Лимиты</div>
                      <div class="two">
                        <label>Мин. сумма, руб.<input name="min_amount_rub" inputmode="numeric" value="{_fmt_compact_money(card.get('min_amount_rub'))}"></label>
                        <label>Макс. сумма, руб.<input name="max_amount_rub" inputmode="numeric" value="{_fmt_compact_money(card.get('max_amount_rub'))}"></label>
                      </div>
                      <div class="two">
                        <label>Дневной лимит, руб.<input name="daily_limit_rub" inputmode="numeric" value="{_fmt_compact_money(card.get('daily_limit_rub'))}"></label>
                        <label>Переводов в день, шт.<input name="daily_transfer_limit" inputmode="numeric" value="{_fmt_compact_money(card.get('daily_transfer_limit'))}"></label>
                      </div>
                      <label>Пауза, мин.<input name="transfer_pause_minutes" inputmode="numeric" value="{_fmt_compact_money(card.get('transfer_pause_minutes'))}"></label>
                    </div>
                    {balance_admin_field_template.format(balance_raw=_fmt_compact_money(card.get("_balance")))}
                    <div class="form-actions">
                      <button class="btn" type="submit">Сохранить</button>
                    </div>
                  </form>
                </div>
              </div>
            """

            withdraw_modal_html += f"""
              <div class="withdraw-form" id="withdraw-card-{card_id}">
                <form class="form" method="post" action="/mastercard/card/withdraw">
                  <input type="hidden" name="user_id" value="{int(user_id)}">
                        {admin_hidden_input}
                  <input type="hidden" name="card_id" value="{card_id}">
                  <div class="withdraw-note">Текущий баланс карты: <b>{balance}</b>. Укажите сумму, которую нужно вывести с этой карты.</div>
                  <label>Сумма вывода, руб.<input name="amount" inputmode="decimal" placeholder="0" required></label>
                  <div class="form-actions">
                    <button class="btn" type="submit">Вывести</button>
                  </div>
                </form>
              </div>
            """

            modal_edit_html += f"""
              <div class="modal-card-form" id="modal-edit-card-{card_id}">
                <form class="form" method="post" action="/mastercard/card/update">
                  <input type="hidden" name="user_id" value="{int(user_id)}">
                        {admin_hidden_input}
                  <input type="hidden" name="card_id" value="{card_id}">
                  <input type="hidden" name="bank_name" value="{_esc(card.get('bank_name'))}">
                  <input type="hidden" name="sbp_phone" value="{_esc(card.get('sbp_phone'))}">
                  <input type="hidden" name="card_number" value="{_format_card_groups(card.get('card_number')) if card.get('card_number') else ''}">
                  <div class="field-box">
                    <div class="box-title">Лимиты</div>
                    <div class="two">
                      <label>Минимальная сумма, руб.<input name="min_amount_rub" inputmode="numeric" value="{_fmt_compact_money(card.get('min_amount_rub'))}"></label>
                      <label>Максимальная сумма, руб.<input name="max_amount_rub" inputmode="numeric" value="{_fmt_compact_money(card.get('max_amount_rub'))}"></label>
                    </div>
                    <div class="two">
                      <label>Дневной лимит, руб.<input name="daily_limit_rub" inputmode="numeric" value="{_fmt_compact_money(card.get('daily_limit_rub'))}"></label>
                      <label>Переводов в день, шт.<input name="daily_transfer_limit" inputmode="numeric" value="{_fmt_compact_money(card.get('daily_transfer_limit'))}"></label>
                    </div>
                    <label>Пауза, мин.<input name="transfer_pause_minutes" inputmode="numeric" value="{_fmt_compact_money(card.get('transfer_pause_minutes'))}"></label>
                  </div>
                  {balance_admin_field_template.format(balance_raw=_fmt_compact_money(card.get("_balance")))}
                  <div class="form-actions">
                    <button class="btn" type="submit">Сохранить</button>
                  </div>
                </form>
              </div>
            """
    else:
        tiles_html = """
          <div class="empty">
            <div class="empty-title">Карт пока нет</div>
            <div class="empty-text">Добавьте первую карту. После сохранения она появится здесь.</div>
          </div>
        """
        carousel_html = tiles_html
        edit_html = """
          <div class="empty">
            <div class="empty-title">Редактировать пока нечего</div>
            <div class="empty-text">Сначала добавьте карту.</div>
          </div>
        """
        modal_edit_html = ""
        withdraw_modal_html = ""

    if completed_orders:
        order_rows_html = ""
        for order in completed_orders[:30]:
            order_id = _esc(order.get("order_id") or "—")
            bank_name = _esc(order.get("bank_name") or "—")
            when = _fmt_date_short(order.get("completed_at") or order.get("created_at"))
            amount = _fmt_money(order.get("total_rub"))
            try:
                total_rub_for_profit = float(order.get("total_rub") or 0)
            except Exception:
                total_rub_for_profit = 0.0
            order_profit = total_rub_for_profit * 0.09
            rub_amount = _fmt_money(order.get("rub_amount"))
            asset_amount = _esc(order.get("btc_amount") or order.get("amount_crypto") or "—")
            wallet = _esc(order.get("wallet") or "—")
            status = _esc(order.get("status") or "completed")
            card_id_text = _esc(order.get("card_id") or "—")
            order_rows_html += f"""
              <div class="order-card">
                <button class="order-toggle" type="button" data-order-toggle="{order_id}">
                  <div class="order-main">
                    <div class="order-title">Заявка #{order_id}</div>
                    <div class="order-meta">{bank_name} · {when}</div>
                  </div>
                  <div class="order-side">
                    <div class="order-sum">{amount}</div>
                    <div class="order-profit">+{_fmt_money(order_profit)}</div>
                  </div>
                </button>
                <div class="order-details">
                  <div class="order-detail-row"><span>Карта</span><b>#{card_id_text} · {bank_name}</b></div>
                  <div class="order-detail-row"><span>Сумма заявки</span><b>{amount}</b></div>
                  <div class="order-detail-row"><span>Сумма без комиссии</span><b>{rub_amount}</b></div>
                  <div class="order-detail-row"><span>Прибыль Mastercard 9%</span><b>{_fmt_money(order_profit)}</b></div>
                  <div class="order-detail-row"><span>Крипто</span><b>{asset_amount}</b></div>
                  <div class="order-detail-row"><span>Кошелёк</span><b>{wallet}</b></div>
                  <div class="order-detail-row"><span>Статус</span><b>{status}</b></div>
                </div>
              </div>
            """
    else:
        order_rows_html = """
          <div class="empty">
            <div class="empty-title">Завершённых заявок нет</div>
            <div class="empty-text">Когда сделки будут завершены, краткая история появится здесь.</div>
          </div>
        """

    card_names = {
        int(card.get("card_id") or 0): str(card.get("bank_name") or f"Карта #{int(card.get('card_id') or 0)}")
        for card in enriched_cards
    }
    reserve_card_ids = [int(card_id) for card_id in card_names.keys() if int(card_id or 0) > 0]

    try:
        reserve_page = max(int(request.query_params.get("log_page") or 1), 1)
    except Exception:
        reserve_page = 1

    logs_per_page = 5
    admin_url_part = f"&admin_id={int(admin_id)}" if admin_mode and admin_id else ""

    reserve_total_received = float(total_completed_amount or 0.0)
    reserve_total_sent = await _sum_reserve_withdrawals_for_cards(reserve_card_ids)
    reserve_debt = reserve_total_received - reserve_total_sent
    reserve_status_text = _reserve_control_status_text(reserve_debt)

    reserve_log_total = await _count_reserve_withdrawal_logs(reserve_card_ids)
    reserve_pages = max((reserve_log_total + logs_per_page - 1) // logs_per_page, 1)
    if reserve_page > reserve_pages:
        reserve_page = reserve_pages

    reserve_offset = (reserve_page - 1) * logs_per_page
    reserve_logs = await _load_reserve_withdrawal_logs(
        card_names,
        limit=logs_per_page,
        offset=reserve_offset,
    )

    reserve_pagination_html = ""
    if reserve_log_total > logs_per_page:
        prev_page = max(reserve_page - 1, 1)
        next_page = min(reserve_page + 1, reserve_pages)
        prev_disabled = " style='opacity:.45;pointer-events:none'" if reserve_page <= 1 else ""
        next_disabled = " style='opacity:.45;pointer-events:none'" if reserve_page >= reserve_pages else ""
        reserve_pagination_html = f"""
          <div class="log-pager">
            <a class="btn ghost" href="/mastercard?user_id={int(user_id)}{admin_url_part}&log_page={prev_page}#stats"{prev_disabled}>← Назад</a>
            <div class="log-pager-info">{reserve_page} / {reserve_pages}</div>
            <a class="btn ghost" href="/mastercard?user_id={int(user_id)}{admin_url_part}&log_page={next_page}#stats"{next_disabled}>Вперёд →</a>
          </div>
        """

    if reserve_logs:
        audit_html = ""
        running_debt = reserve_debt
        for item in reserve_logs:
            card_name = _esc(item.get("card_name") or "Карта")
            when = _fmt_date_short(item.get("created_at"))
            amount = float(item.get("amount") or 0.0)
            debt_after_this_log = running_debt
            running_debt += amount
            alert_class = "log-alert" if debt_after_this_log > 0.01 else ""
            audit_html += f"""
              <div class="log-card {alert_class}">
                <div class="log-top">
                  <div>
                    <div class="log-name">Вывод в резерв</div>
                    <div class="log-text">{card_name}</div>
                  </div>
                  <div class="log-time">{when}</div>
                </div>
                <div class="log-text">Держатель карт отправил средства в резерв обменника.</div>
                <div class="log-diff">Сумма вывода: {_fmt_money(amount)}</div>
                <div class="log-text">Недодача после этого вывода: {_fmt_money(max(debt_after_this_log, 0.0))}</div>
              </div>
            """
        audit_html += reserve_pagination_html
    else:
        audit_html = """
          <div class="empty">
            <div class="empty-title">Выводов в резерв пока нет</div>
            <div class="empty-text">Когда держатель карт нажмёт «Вывести», операция появится здесь и уменьшит его недодачу.</div>
          </div>
        """

    body = f"""
      <section class="panel">
        <nav class="nav" aria-label="Разделы кабинета">
          <button class="nav-btn active" type="button" data-tab="cards">Карты</button>
          <button class="nav-btn" type="button" data-tab="add">Добавить</button>
          <button class="nav-btn" type="button" data-tab="orders">Заявки</button>
          <button class="nav-btn" type="button" data-tab="stats">Статистика</button>
        </nav>

        <section class="tab active" id="tab-cards">
          <div class="section-head">
            <div class="section-title">Мои карты</div>
            <div class="head-actions">
              {admin_deposit_button_html}
              <div class="view-switch" aria-label="Вид карт">
                <button class="view-switch-btn active" type="button" data-view-mode="swipe">Свайп</button>
                <button class="view-switch-btn" type="button" data-view-mode="grid">Плитка</button>
              </div>
            </div>
          </div>
          {deposit_panel_html}
          <div class="cards-carousel" id="cardsCarousel">{carousel_html}</div>
          <div class="cards-grid" id="cardsGrid">{tiles_html}</div>
        </section>

        <section class="tab" id="tab-add">
          <div class="section-head">
            <div>
              <div class="section-title">Добавить карту</div>
              <div class="section-note">Форма разделена на реквизиты и лимиты.</div>
            </div>
          </div>
          <div class="add-card">
            <form class="form" method="post" action="/mastercard/card/add">
              <input type="hidden" name="user_id" value="{int(user_id)}">
                        {admin_hidden_input}
              <div class="field-box">
                <div class="box-title">Реквизиты</div>
                <label>Банк<input name="bank_name" required placeholder="Например: Сбер"></label>
                <label>СБП / телефон<input name="sbp_phone" inputmode="tel" value="+7" placeholder="+79991234567"></label>
                <label>Номер карты<input name="card_number" inputmode="numeric" placeholder="0000 0000 0000 0000"></label>
              </div>
              <div class="field-box">
                <div class="box-title">Лимиты</div>
                <div class="two">
                  <label>Мин. сумма, руб.<input name="min_amount_rub" inputmode="numeric" value="{DEFAULT_MIN_AMOUNT_RUB}"></label>
                  <label>Макс. сумма, руб.<input name="max_amount_rub" inputmode="numeric" value="{DEFAULT_MAX_AMOUNT_RUB}"></label>
                </div>
                <div class="two">
                  <label>Дневной лимит, руб.<input name="daily_limit_rub" inputmode="numeric" value="{DEFAULT_DAILY_LIMIT_RUB}"></label>
                  <label>Переводов в день, шт.<input name="daily_transfer_limit" inputmode="numeric" value="{DEFAULT_DAILY_TRANSFER_LIMIT}"></label>
                </div>
                <label>Пауза, мин.<input name="transfer_pause_minutes" inputmode="numeric" value="{DEFAULT_TRANSFER_PAUSE_MINUTES}"></label>
              </div>
              <button class="btn" type="submit">Добавить карту</button>
            </form>
          </div>
          <div class="help">Поля лимитов можно оставить пустыми, если ограничение не нужно.</div>
        </section>

        <section class="tab" id="tab-orders">
          <div class="section-head">
            <div>
              <div class="section-title">Заявки</div>
              <div class="section-note">Короткая история завершённых сделок.</div>
            </div>
          </div>
          <div class="orders-mini-stats">
            <div class="orders-mini"><div class="orders-mini-label">Завершено</div><div class="orders-mini-value">{len(completed_orders)}</div></div>
            <div class="orders-mini"><div class="orders-mini-label">Прибыль</div><div class="orders-mini-value">{_fmt_money(total_profit)}</div></div>
          </div>
          <div class="orders-list">{order_rows_html}</div>
        </section>

        <section class="tab" id="tab-stats">
          <div class="section-head">
            <div>
              <div class="section-title">Статистика</div>
              <div class="section-note">Только отдельная сводка, без карт и форм.</div>
            </div>
            {stats_reset_form}
          </div>
          <div class="stats-only">
            <div class="stat-block">
              <div class="stat-block-title">
                <span>За всё время</span>
                <span class="stat-block-badge">общая</span>
              </div>
              <div class="stat-grid">
                <div class="stat-card"><div class="stat-label">Прибыль</div><div class="stat-value">{_fmt_money(total_profit)}</div></div>
                <div class="stat-card"><div class="stat-label">Сделки</div><div class="stat-value">{len(completed_orders)}</div></div>
                <div class="stat-card"><div class="stat-label">Сумма заявок</div><div class="stat-value">{_fmt_money(total_completed_amount)}</div></div>
                <div class="stat-card"><div class="stat-label">Средний чек</div><div class="stat-value">{_fmt_money(total_completed_amount / len(completed_orders) if completed_orders else 0)}</div></div>
              </div>
            </div>

            <div class="stat-block">
              <div class="stat-block-title">
                <span>За сегодня</span>
                <span class="stat-block-badge">до 00:00 НСК</span>
              </div>
              <div class="stat-grid">
                <div class="stat-card"><div class="stat-label">Прибыль</div><div class="stat-value">{_fmt_money(today_profit)}</div></div>
                <div class="stat-card"><div class="stat-label">Сделки</div><div class="stat-value">{today_completed_count}</div></div>
                <div class="stat-card"><div class="stat-label">Сумма заявок</div><div class="stat-value">{_fmt_money(today_completed_amount)}</div></div>
                <div class="stat-card"><div class="stat-label">Средний чек</div><div class="stat-value">{_fmt_money(today_completed_amount / today_completed_count if today_completed_count else 0)}</div></div>
              </div>
            </div>

                        <div class="stat-log">
              <div class="log-title">Контроль резерва</div>

              <div class="stat-grid" style="margin-bottom:2px">
                <div class="stat-card"><div class="stat-label">Получено на карты</div><div class="stat-value">{_fmt_money(reserve_total_received)}</div></div>
                <div class="stat-card"><div class="stat-label">Отправлено в резерв</div><div class="stat-value">{_fmt_money(reserve_total_sent)}</div></div>
                <div class="stat-card"><div class="stat-label">Недодача</div><div class="stat-value">{_fmt_money(max(reserve_debt, 0.0))}</div></div>
                <div class="stat-card"><div class="stat-label">Переплата</div><div class="stat-value">{_fmt_money(abs(min(reserve_debt, 0.0)))}</div></div>
              </div>

              <div class="log-card" style="border-color:rgba(214,179,95,.18);background:rgba(214,179,95,.045)">
                <div class="log-top">
                  <div>
                    <div class="log-name">Финансовый контроль</div>
                    <div class="log-text">{_esc(reserve_status_text)}</div>
                  </div>
                  <div class="log-time">ДОЛГ</div>
                </div>
                <div class="log-diff">Текущий долг держателя перед резервом: {_fmt_money(max(reserve_debt, 0.0))}</div>
              </div>

              <div class="log-title" style="margin-top:4px">История выводов в резерв</div>
              {audit_html}
            </div>
          </div>
        </section>

      <div class="modal-backdrop" id="editModal" aria-hidden="true">
        <div class="modal-box" role="dialog" aria-modal="true">
          <div class="modal-head">
            <div class="modal-title" id="editModalTitle">Редактировать карту</div>
            <button class="modal-close" type="button" data-modal-close="1" aria-label="Закрыть">×</button>
          </div>
          <div class="modal-content">{modal_edit_html}</div>
        </div>
      </div>

      <div class="modal-backdrop" id="withdrawModal" aria-hidden="true">
        <div class="modal-box" role="dialog" aria-modal="true">
          <div class="modal-head">
            <div class="modal-title" id="withdrawModalTitle">Вывод с карты</div>
            <button class="modal-close" type="button" data-withdraw-close="1" aria-label="Закрыть">×</button>
          </div>
          <div class="modal-content">{withdraw_modal_html}</div>
        </div>
      </div>

      {admin_deposit_modal_html}
      </section>
    """

    return _page("MasterCard", body, f"{_fmt_compact_money(total_balance)} / {_fmt_compact_money(mastercard_deposit)}")



@router.post("/deposit/update")
async def mastercard_update_deposit(
        user_id: int = Form(...),
        admin_id: str = Form(""),
        deposit_rub: str = Form(""),
):
    admin_actor_id = None
    try:
        parsed_admin_id = int(admin_id) if str(admin_id or "").strip() else None
    except Exception:
        parsed_admin_id = None

    if parsed_admin_id and await _is_admin_user(parsed_admin_id):
        admin_actor_id = parsed_admin_id

    if not await _is_mastercard_user(int(user_id)):
        return await _render_access_denied()

    if not admin_actor_id:
        return await _render_access_denied()

    try:
        value = _to_float_or_none(deposit_rub)
    except Exception:
        return _alert_redirect(
            int(user_id),
            "Введите корректную сумму депозита.",
            "cards",
            admin_id=admin_actor_id,
        )

    await set_user_mastercard_deposit(int(user_id), float(value or 0.0))
    return _redirect(int(user_id), "cards", admin_id=admin_actor_id)


@router.post("/audit/clear")
async def mastercard_clear_audit(
        user_id: int = Form(...),
        admin_id: str = Form(""),
):
    admin_actor_id = None
    try:
        parsed_admin_id = int(admin_id) if str(admin_id or "").strip() else None
    except Exception:
        parsed_admin_id = None

    if parsed_admin_id and await _is_admin_user(parsed_admin_id):
        admin_actor_id = parsed_admin_id

    if not admin_actor_id:
        return await _render_access_denied()

    if not await _is_mastercard_user(int(user_id)):
        return await _render_access_denied()

    await _clear_audit_logs(int(user_id))
    return _redirect(int(user_id), "stats", admin_id=int(admin_actor_id))


@router.post("/cards/reset")
async def mastercard_reset_card_data(
        user_id: int = Form(...),
        admin_id: str = Form(""),
):
    admin_actor_id = None
    try:
        parsed_admin_id = int(admin_id) if str(admin_id or "").strip() else None
    except Exception:
        parsed_admin_id = None

    if parsed_admin_id and await _is_admin_user(parsed_admin_id):
        admin_actor_id = parsed_admin_id

    if not admin_actor_id:
        return await _render_access_denied()

    if not await _is_mastercard_user(int(user_id)):
        return await _render_access_denied()

    await _reset_mastercard_card_data(int(user_id))
    return _redirect(int(user_id), "stats", admin_id=int(admin_actor_id))


@router.post("/card/add")
async def mastercard_add_card(
        user_id: int = Form(...),
        admin_id: str = Form(""),
        bank_name: str = Form(...),
        sbp_phone: str = Form(""),
        card_number: str = Form(""),
        min_amount_rub: str = Form(""),
        max_amount_rub: str = Form(""),
        daily_limit_rub: str = Form(""),
        daily_transfer_limit: str = Form(""),
        transfer_pause_minutes: str = Form(""),
        target_balance: str = Form(""),
):
    admin_actor_id = None
    try:
        parsed_admin_id = int(admin_id) if str(admin_id or "").strip() else None
    except Exception:
        parsed_admin_id = None
    if parsed_admin_id and await _is_admin_user(parsed_admin_id):
        admin_actor_id = parsed_admin_id

    if not await _is_mastercard_user(user_id):
        return _redirect(user_id, admin_id=admin_actor_id)

    await add_card(
        owner_id=int(user_id),
        bank_name=bank_name.strip(),
        sbp_phone=_normalize_sbp_phone(sbp_phone),
        card_number=_normalize_card_number(card_number),
        min_amount_rub=_to_float_or_none(min_amount_rub),
        max_amount_rub=_to_float_or_none(max_amount_rub),
        daily_limit_rub=_to_float_or_none(daily_limit_rub),
        daily_transfer_limit=_to_int_or_none(daily_transfer_limit),
        transfer_pause_minutes=_to_int_or_none(transfer_pause_minutes),
    )
    return _redirect(user_id, "cards", admin_id=admin_actor_id)


@router.post("/card/update")
async def mastercard_update_card(
        user_id: int = Form(...),
        admin_id: str = Form(""),
        card_id: int = Form(...),
        bank_name: str = Form(...),
        sbp_phone: str = Form(""),
        card_number: str = Form(""),
        min_amount_rub: str = Form(""),
        max_amount_rub: str = Form(""),
        daily_limit_rub: str = Form(""),
        daily_transfer_limit: str = Form(""),
        transfer_pause_minutes: str = Form(""),
        target_balance: str = Form(""),
):
    admin_actor_id = None
    try:
        parsed_admin_id = int(admin_id) if str(admin_id or "").strip() else None
    except Exception:
        parsed_admin_id = None
    if parsed_admin_id and await _is_admin_user(parsed_admin_id):
        admin_actor_id = parsed_admin_id

    if not await _is_mastercard_user(user_id):
        return _redirect(user_id, admin_id=admin_actor_id)

    card = await get_card_by_id(card_id)
    if not card or int(card.get("owner_id") or 0) != int(user_id):
        return _redirect(user_id, "cards", admin_id=admin_actor_id)

    await update_card(
        int(card_id),
        bank_name=bank_name.strip(),
        sbp_phone=_normalize_sbp_phone(sbp_phone),
        card_number=_normalize_card_number(card_number),
        min_amount_rub=_to_float_or_none(min_amount_rub),
        max_amount_rub=_to_float_or_none(max_amount_rub),
        daily_limit_rub=_to_float_or_none(daily_limit_rub),
        daily_transfer_limit=_to_int_or_none(daily_transfer_limit),
        transfer_pause_minutes=_to_int_or_none(transfer_pause_minutes),
    )
    if admin_actor_id:
        await _set_card_balance(
            card_id=int(card_id),
            user_id=int(user_id),
            target_balance=target_balance,
            admin_id=int(admin_actor_id),
        )

    await _log_card_audit(
        owner_id=int(user_id),
        card_id=int(card_id),
        action="settings_update",
        title="Настройки карты обновлены",
        details=(
            "Админ изменил реквизиты/лимиты и при необходимости баланс карты."
            if admin_actor_id else
            "Пользователь изменил реквизиты и/или лимиты карты. Баланс через кабинет Mastercard не меняется."
        ),
    )
    return _redirect(user_id, "cards", admin_id=admin_actor_id)


@router.post("/card/toggle")
async def mastercard_toggle_card(
        user_id: int = Form(...),
        admin_id: str = Form(""),
        card_id: int = Form(...),
):
    admin_actor_id = None
    try:
        parsed_admin_id = int(admin_id) if str(admin_id or "").strip() else None
    except Exception:
        parsed_admin_id = None
    if parsed_admin_id and await _is_admin_user(parsed_admin_id):
        admin_actor_id = parsed_admin_id

    if not await _is_mastercard_user(user_id):
        return _redirect(user_id, admin_id=admin_actor_id)

    card = await get_card_by_id(card_id)
    if not card or int(card.get("owner_id") or 0) != int(user_id):
        return _redirect(user_id, f"card-{int(card_id)}", admin_id=admin_actor_id)

    currently_active = bool(card.get("is_active", True))
    if not currently_active:
        stats = await _get_card_today_limit_stats(int(card_id))
        card_for_check = dict(card)
        card_for_check["_today_count"] = stats.get("count") or 0
        card_for_check["_today_sum"] = stats.get("sum") or 0.0
        card_for_check["_last_completed_nsk"] = stats.get("last")
        blocked, reason, until = _limit_state_for_card(card_for_check)
        if blocked:
            await _set_limit_lock(int(user_id), int(card_id), reason, until)
            return _alert_redirect(
                int(user_id),
                f"Карту нельзя включить: {reason}. Дождитесь окончания лимита ({until}) или измените лимиты этой карты.",
                f"card-{int(card_id)}",
                admin_id=admin_actor_id,
            )
        await _clear_limit_lock(int(card_id))

    new_state = not currently_active
    await set_card_active(
        card_id=int(card_id),
        owner_id=int(user_id),
        is_active=new_state,
    )
    await _log_card_audit(
        owner_id=int(user_id),
        card_id=int(card_id),
        action="toggle_on" if new_state else "toggle_off",
        title="Карта включена" if new_state else "Карта выключена",
        details="Пользователь изменил статус карты вручную.",
    )
    return _redirect(user_id, f"card-{int(card_id)}", admin_id=admin_actor_id)


@router.post("/card/delete")
async def mastercard_delete_card(
        user_id: int = Form(...),
        admin_id: str = Form(""),
        card_id: int = Form(...),
):
    admin_actor_id = None
    try:
        parsed_admin_id = int(admin_id) if str(admin_id or "").strip() else None
    except Exception:
        parsed_admin_id = None
    if parsed_admin_id and await _is_admin_user(parsed_admin_id):
        admin_actor_id = parsed_admin_id

    if not await _is_mastercard_user(user_id):
        return _redirect(user_id, admin_id=admin_actor_id)

    card = await get_card_by_id(card_id)
    if not card or int(card.get("owner_id") or 0) != int(user_id):
        return _redirect(user_id, "cards", admin_id=admin_actor_id)

    card_title = str(card.get("bank_name") or "").strip() or f"Карта #{int(card_id)}"
    await _log_card_audit(
        owner_id=int(user_id),
        card_id=int(card_id),
        action="delete_card",
        title="Карта удалена",
        details=f"Удалена карта {card_title}.",
    )
    await delete_card(card_id=int(card_id), owner_id=int(user_id))
    return _redirect(user_id, "cards", admin_id=admin_actor_id)


@router.post("/card/withdraw")
async def mastercard_withdraw_card(
        user_id: int = Form(...),
        admin_id: str = Form(""),
        card_id: int = Form(...),
        amount: str = Form(...),
):
    admin_actor_id = None
    try:
        parsed_admin_id = int(admin_id) if str(admin_id or "").strip() else None
    except Exception:
        parsed_admin_id = None
    if parsed_admin_id and await _is_admin_user(parsed_admin_id):
        admin_actor_id = parsed_admin_id

    if not await _is_mastercard_user(user_id):
        return _redirect(user_id, admin_id=admin_actor_id)

    card = await get_card_by_id(card_id)
    if not card or int(card.get("owner_id") or 0) != int(user_id):
        return _redirect(user_id, "cards", admin_id=admin_actor_id)

    try:
        value = _to_float_or_none(amount)
    except Exception:
        return _alert_redirect(int(user_id), "Введите корректную сумму вывода.", "cards", admin_id=admin_actor_id)

    if not value or value <= 0:
        return _alert_redirect(int(user_id), "Введите сумму вывода больше нуля.", "cards", admin_id=admin_actor_id)

    before = float(await get_card_balance(int(card_id)) or 0)
    if value > before:
        return _alert_redirect(
            int(user_id),
            f"Недостаточно средств на карте. Доступно: {_fmt_money(before)}, запрошено: {_fmt_money(value)}.",
            "cards",
            admin_id=admin_actor_id,
        )

    await _record_card_withdrawal(
        admin_id=int(admin_actor_id or user_id),
        card_id=int(card_id),
        amount=float(value),
    )
    await _log_card_audit(
        owner_id=int(user_id),
        card_id=int(card_id),
        action="withdraw",
        title="Вывод с карты",
        details=f"До вывода было {_fmt_money(before)}, после вывода стало {_fmt_money(before - float(value))}.",
        amount=float(value),
        diff=-float(value),
    )

    return _redirect(user_id, "cards", admin_id=admin_actor_id)
