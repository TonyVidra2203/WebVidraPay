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
from db.users import get_user


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


def _redirect(user_id: int, anchor: str = "") -> RedirectResponse:
    suffix = f"#{anchor}" if anchor else ""
    return RedirectResponse(
        url=f"/mastercard?user_id={int(user_id)}{suffix}",
        status_code=303,
    )


def _alert_redirect(user_id: int, message: str, anchor: str = "cards") -> HTMLResponse:
    safe_message = (
        str(message or "")
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", " ")
    )
    suffix = f"#{anchor}" if anchor else ""
    return HTMLResponse(
        f"""
        <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
        <body style="background:#000;color:#fff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
        <script>
          alert('{safe_message}');
          window.location.href = '/mastercard?user_id={int(user_id)}{suffix}';
        </script>
        </body></html>
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
        return True, f"Лимит переводов за сутки: {today_count}/{transfer_limit} шт.", next_midnight.strftime("%d.%m %H:%M")

    if daily_limit > 0 and today_sum >= daily_limit:
        return True, f"Дневной лимит суммы: {_fmt_money(today_sum)} из {_fmt_money(daily_limit)}", next_midnight.strftime("%d.%m %H:%M")

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


async def _load_audit_logs(owner_id: int, card_names: dict[int, str], limit: int = 18) -> list[dict[str, Any]]:
    await _ensure_mastercard_web_tables()
    db = await get_db()
    cur = await db.execute(
        """
        SELECT card_id, action, title, details, amount, diff, created_at
          FROM mastercard_card_audit
         WHERE owner_id = ?
      ORDER BY id DESC
         LIMIT ?
        """,
        (int(owner_id), int(limit)),
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


async def _set_card_balance(card_id: int, user_id: int, target_balance: Any) -> None:
    try:
        desired = float(str(target_balance or "").replace(" ", "").replace(",", ".").strip())
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
    await add_withdrawal(admin_id=int(user_id), card_id=int(card_id), amount=float(diff))
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
    return role == "mastercard"


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
          </style>
        </head>
        <body><div class="wrap"><div class="card"><div class="bad">⛔</div><h2>Нет доступа</h2><p class="muted">Эта панель доступна только роли MasterCard.</p></div></div></body>
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
  <style>
    :root {{
      --bg:#000000;
      --card:#0b0b0d;
      --card2:#111114;
      --card3:#171719;
      --line:rgba(255,255,255,.085);
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
      padding:8px 10px 9px;
      border-radius:18px;
      border:1px solid rgba(214,179,95,.42);
      background:
        radial-gradient(circle at 18% 0%, rgba(214,179,95,.30), transparent 45%),
        linear-gradient(135deg, rgba(214,179,95,.18), rgba(255,255,255,.045));
      box-shadow:
        0 12px 34px rgba(214,179,95,.12),
        0 14px 36px rgba(0,0,0,.36),
        inset 0 1px 0 rgba(255,255,255,.06);
      text-align:right;
    }}
    .top-balance span{{
      display:block;
      margin-bottom:3px;
      color:rgba(246,243,234,.62);
      font-size:9.5px;
      line-height:1;
      font-weight:950;
      text-transform:uppercase;
      letter-spacing:.45px;
      white-space:nowrap;
    }}
    .top-balance b{{
      display:block;
      color:var(--accent2);
      font-size:24px;
      line-height:1;
      font-weight:1000;
      letter-spacing:-.65px;
      white-space:nowrap;
      text-shadow:0 0 20px rgba(214,179,95,.40);
    }}
    .panel{{border:1px solid var(--line);border-radius:28px;background:#080809;box-shadow:0 22px 70px rgba(0,0,0,.55);overflow:hidden}}
    .nav{{position:sticky;top:0;z-index:5;display:grid;grid-template-columns:repeat(4,1fr);gap:7px;padding:10px;background:rgba(8,8,9,.96);border-bottom:1px solid var(--line);backdrop-filter:blur(10px)}}
    .nav-btn{{min-width:0;min-height:40px;border:1px solid var(--line);border-radius:15px;background:rgba(255,255,255,.035);color:var(--muted);font-size:11.5px;font-weight:950;line-height:1}}
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
    .card-slide{{scroll-snap-align:center;min-width:0;position:relative;min-height:205px;padding:17px;border-radius:28px;border:1px solid var(--line);background:linear-gradient(145deg,#141416,#080809);overflow:hidden;box-shadow:0 18px 44px rgba(0,0,0,.38)}}
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
    .modal-box{{width:100%;max-width:520px;max-height:88svh;overflow:auto;border:1px solid var(--line);border-radius:28px;background:#080809;box-shadow:0 30px 90px rgba(0,0,0,.78)}}
    .modal-head{{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px;border-bottom:1px solid var(--line);background:rgba(8,8,9,.96);backdrop-filter:blur(10px)}}
    .modal-title{{font-size:18px;font-weight:950;line-height:1.1}}
    .modal-close{{width:38px;height:38px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.045);color:var(--muted);font-size:20px;line-height:1}}
    .modal-content{{padding:14px}}
    .modal-card-form{{display:none}}
    .modal-card-form.active{{display:block}}
    .withdraw-form{{display:none}}
    .withdraw-form.active{{display:block}}
    .withdraw-note{{color:var(--muted);font-size:12.5px;line-height:1.4;margin-bottom:10px}}
    .stats-only{{display:grid;gap:13px}}
    .stat-block{{padding:13px;border-radius:22px;border:1px solid var(--line);background:linear-gradient(180deg,#101012,#080809)}}
    .stat-block-title{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:11px;font-size:14px;font-weight:950;color:var(--text)}}
    .stat-block-badge{{padding:5px 8px;border-radius:999px;border:1px solid rgba(214,179,95,.18);background:rgba(214,179,95,.06);color:var(--accent);font-size:10px;font-weight:950;text-transform:uppercase;letter-spacing:.35px}}
    .stat-grid{{display:grid;grid-template-columns:1fr 1fr;gap:9px}}
    .stat-card{{min-width:0;padding:12px;border-radius:17px;border:1px solid rgba(255,255,255,.055);background:rgba(255,255,255,.024)}}
    .stat-label{{color:var(--muted2);font-size:10px;font-weight:950;text-transform:uppercase;letter-spacing:.35px;line-height:1.15}}
    .stat-value{{margin-top:7px;font-size:19px;line-height:1.08;font-weight:950;color:var(--accent);overflow-wrap:anywhere}}
    .orders-list{{display:grid;gap:8px}}
    .order-card{{border:1px solid var(--line);border-radius:18px;background:linear-gradient(180deg,#101012,#09090a);overflow:hidden}}
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
      .top-balance{{min-width:118px;padding:7px 9px;border-radius:16px}}
      .top-balance span{{font-size:8.7px}}
      .top-balance b{{font-size:21px}}
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
        <div class="top-balance" aria-label="Сумма на всех картах">
          <span>Баланс карт</span>
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
      if (modal) modal.addEventListener('click', function(event) {{ if (event.target === modal) closeEditModal(); }});
      if (withdrawModal) withdrawModal.addEventListener('click', function(event) {{ if (event.target === withdrawModal) closeWithdrawModal(); }});
      document.addEventListener('keydown', function(event) {{ if (event.key === 'Escape') {{ closeEditModal(); closeWithdrawModal(); }} }});
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
      if (['cards','add','orders','stats'].indexOf(hash) >= 0) showTab(hash);
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

    cards = await get_cards_by_owner(user_id)
    completed_orders = await get_completed_orders_by_master(user_id)

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
              <article class="card-slide {'off' if not is_active else ''}" data-card-id="{card_id}">
                <div class="slide-top">
                  <div class="slide-title-wrap">
                    <div class="slide-bank">{bank_name}</div>
                    <div class="slide-icons">
                      <button class="eye-btn" type="button" data-toggle-requisites="{card_id}" aria-label="Показать реквизиты">🙈</button>
                      <form class="icon-form" method="post" action="/mastercard/card/delete" onsubmit="return confirm('Удалить карту {bank_name}?')">
                        <input type="hidden" name="user_id" value="{int(user_id)}">
                        <input type="hidden" name="card_id" value="{card_id}">
                        <button class="icon-btn trash" type="submit" aria-label="Удалить карту">🗑️</button>
                      </form>
                      <form class="icon-form" method="post" action="/mastercard/card/toggle">
                        <input type="hidden" name="user_id" value="{int(user_id)}">
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
                  <input type="hidden" name="card_id" value="{card_id}">
                  <div class="withdraw-note">Текущий баланс карты: <b>{balance}</b>. Укажите сумму, которую нужно вывести с этой карты.</div>
                  <label>Сумма вывода, руб.<input name="amount" inputmode="decimal" placeholder="0" required></label>
                  <div class="split-actions">
                    <button class="btn ghost" type="button" data-withdraw-close="1">Отменить</button>
                    <button class="btn" type="submit">Вывести</button>
                  </div>
                </form>
              </div>
            """

            modal_edit_html += f"""
              <div class="modal-card-form" id="modal-edit-card-{card_id}">
                <form class="form" method="post" action="/mastercard/card/update">
                  <input type="hidden" name="user_id" value="{int(user_id)}">
                  <input type="hidden" name="card_id" value="{card_id}">
                  <input type="hidden" name="bank_name" value="{_esc(card.get('bank_name'))}">
                  <input type="hidden" name="sbp_phone" value="{_esc(card.get('sbp_phone'))}">
                  <input type="hidden" name="card_number" value="{_format_card_groups(card.get('card_number')) if card.get('card_number') else ''}">
                  <div class="field-box">
                    <div class="box-title">Баланс карты</div>
                    <label>Текущий баланс, руб.<input name="current_balance" inputmode="decimal" value="{_fmt_compact_money(card.get('_balance'))}" placeholder="0"></label>
                  </div>
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
    audit_logs = await _load_audit_logs(int(user_id), card_names)
    if audit_logs:
        audit_html = ""
        for item in audit_logs:
            action = _esc(item.get("title") or "Действие")
            card_name = _esc(item.get("card_name") or "Карта")
            details = _esc(item.get("details") or "")
            when = _fmt_date_short(item.get("created_at"))
            diff = float(item.get("diff") or 0.0)
            amount = float(item.get("amount") or 0.0)
            is_alert = abs(diff) >= 1000 or str(item.get("action") or "") in {"balance_adjust", "limit_off"}
            alert_class = "log-alert" if is_alert else ""
            diff_html = ""
            if abs(diff) >= 0.01:
                sign = "+" if diff > 0 else ""
                diff_html = f'<div class="log-diff">Отклонение: {sign}{_fmt_money(diff)}</div>'
            elif amount > 0:
                diff_html = f'<div class="log-diff">Сумма: {_fmt_money(amount)}</div>'
            audit_html += f"""
              <div class="log-card {alert_class}">
                <div class="log-top">
                  <div><div class="log-name">{action}</div><div class="log-text">{card_name}</div></div>
                  <div class="log-time">{when}</div>
                </div>
                <div class="log-text">{details}</div>
                {diff_html}
              </div>
            """
    else:
        audit_html = """
          <div class="empty">
            <div class="empty-title">Журнал пока пуст</div>
            <div class="empty-text">Здесь будут фиксироваться выводы, ручные правки баланса, включения/выключения и срабатывания лимитов.</div>
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
              <div class="view-switch" aria-label="Вид карт">
                <button class="view-switch-btn active" type="button" data-view-mode="swipe">Свайп</button>
                <button class="view-switch-btn" type="button" data-view-mode="grid">Плитка</button>
              </div>
            </div>
          </div>
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
              <div class="field-box">
                <div class="box-title">Реквизиты</div>
                <label>Банк<input name="bank_name" required placeholder="Например: Сбер"></label>
                <label>СБП / телефон<input name="sbp_phone" inputmode="tel" value="+7" placeholder="+79991234567"></label>
                <label>Номер карты<input name="card_number" inputmode="numeric" placeholder="0000 0000 0000 0000"></label>
              </div>
              <div class="field-box">
                <div class="box-title">Лимиты</div>
                <input type="hidden" name="max_amount_rub" value="{DEFAULT_MAX_AMOUNT_RUB}">
                <label>Мин. сумма, руб.<input name="min_amount_rub" inputmode="numeric" value="{DEFAULT_MIN_AMOUNT_RUB}"></label>
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
              <div class="log-title">Журнал контроля</div>
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
      </section>
    """

    return _page("MasterCard", body, _fmt_money(total_balance))


@router.post("/card/add")
async def mastercard_add_card(
    user_id: int = Form(...),
    bank_name: str = Form(...),
    sbp_phone: str = Form(""),
    card_number: str = Form(""),
    min_amount_rub: str = Form(""),
    max_amount_rub: str = Form(""),
    daily_limit_rub: str = Form(""),
    daily_transfer_limit: str = Form(""),
    transfer_pause_minutes: str = Form(""),
    current_balance: str = Form(""),
):
    if not await _is_mastercard_user(user_id):
        return _redirect(user_id)

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
    return _redirect(user_id, "cards")


@router.post("/card/update")
async def mastercard_update_card(
    user_id: int = Form(...),
    card_id: int = Form(...),
    bank_name: str = Form(...),
    sbp_phone: str = Form(""),
    card_number: str = Form(""),
    min_amount_rub: str = Form(""),
    max_amount_rub: str = Form(""),
    daily_limit_rub: str = Form(""),
    daily_transfer_limit: str = Form(""),
    transfer_pause_minutes: str = Form(""),
    current_balance: str = Form(""),
):
    if not await _is_mastercard_user(user_id):
        return _redirect(user_id)

    card = await get_card_by_id(card_id)
    if not card or int(card.get("owner_id") or 0) != int(user_id):
        return _redirect(user_id, "cards")

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
    await _set_card_balance(int(card_id), int(user_id), current_balance)
    await _log_card_audit(
        owner_id=int(user_id),
        card_id=int(card_id),
        action="settings_update",
        title="Настройки карты обновлены",
        details="Пользователь сохранил баланс и лимиты карты.",
    )
    return _redirect(user_id, "cards")


@router.post("/card/toggle")
async def mastercard_toggle_card(
    user_id: int = Form(...),
    card_id: int = Form(...),
):
    if not await _is_mastercard_user(user_id):
        return _redirect(user_id)

    card = await get_card_by_id(card_id)
    if not card or int(card.get("owner_id") or 0) != int(user_id):
        return _redirect(user_id, "cards")

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
                "cards",
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
    return _redirect(user_id, "cards")


@router.post("/card/delete")
async def mastercard_delete_card(
    user_id: int = Form(...),
    card_id: int = Form(...),
):
    if not await _is_mastercard_user(user_id):
        return _redirect(user_id)

    await _log_card_audit(
        owner_id=int(user_id),
        card_id=int(card_id),
        action="delete_card",
        title="Карта удалена",
        details=f"Удалена карта {str(card.get('bank_name') or '').strip() or '#' + str(card_id)}.",
    )
    await delete_card(card_id=int(card_id), owner_id=int(user_id))
    return _redirect(user_id, "cards")


@router.post("/card/withdraw")
async def mastercard_withdraw_card(
    user_id: int = Form(...),
    card_id: int = Form(...),
    amount: str = Form(...),
):
    if not await _is_mastercard_user(user_id):
        return _redirect(user_id)

    card = await get_card_by_id(card_id)
    if not card or int(card.get("owner_id") or 0) != int(user_id):
        return _redirect(user_id, "cards")

    value = _to_float_or_none(amount)
    if value and value > 0:
        before = float(await get_card_balance(int(card_id)) or 0)
        await add_withdrawal(admin_id=int(user_id), card_id=int(card_id), amount=float(value))
        await _log_card_audit(
            owner_id=int(user_id),
            card_id=int(card_id),
            action="withdraw",
            title="Вывод с карты",
            details=f"До вывода было {_fmt_money(before)}, после расчётно {_fmt_money(before - float(value))}.",
            amount=float(value),
        )

    return _redirect(user_id, "cards")
