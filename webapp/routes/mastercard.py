from __future__ import annotations

import html
import math
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db.cards import (
    add_card,
    delete_card,
    get_card_balance,
    get_card_by_id,
    get_cards_by_owner,
    set_card_active,
    update_card,
)
from db.p2p import get_active_orders_by_master, get_completed_orders_by_master
from db.users import get_user


router = APIRouter(prefix="/mastercard", tags=["mastercard-web"])

DEFAULT_MIN_AMOUNT_RUB = 1200
DEFAULT_MAX_AMOUNT_RUB = 30000
DEFAULT_DAILY_LIMIT_RUB = 30000
DEFAULT_DAILY_TRANSFER_LIMIT = 3
DEFAULT_TRANSFER_PAUSE_MINUTES = 30

_web_mc_sessions: dict[int, datetime] = {}


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


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


def _redirect(user_id: int, anchor: str = "") -> RedirectResponse:
    suffix = f"#{anchor}" if anchor else ""
    return RedirectResponse(
        url=f"/mastercard?user_id={int(user_id)}{suffix}",
        status_code=303,
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
            body{margin:0;background:#070b14;color:#fff;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
            .wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
            .card{max-width:420px;background:#111827;border:1px solid rgba(255,255,255,.12);border-radius:22px;padding:24px;text-align:center}
            .bad{color:#ff6b6b;font-size:34px}
          </style>
        </head>
        <body><div class="wrap"><div class="card"><div class="bad">⛔</div><h2>Нет доступа</h2><p>Эта панель доступна только роли MasterCard.</p></div></div></body>
        </html>
        """
    )


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <style>
    :root {{
      --bg:#050814;
      --card:#0e1728;
      --card2:#111d32;
      --line:rgba(255,255,255,.10);
      --text:#f8fafc;
      --muted:#93a4bd;
      --accent:#23d3a4;
      --accent2:#28a8ff;
      --danger:#ff5f6d;
      --warn:#ffc857;
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0;
      background:
        radial-gradient(circle at top left, rgba(35,211,164,.18), transparent 34%),
        radial-gradient(circle at top right, rgba(40,168,255,.16), transparent 32%),
        var(--bg);
      color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
    }}
    a {{ color:inherit; text-decoration:none; }}
    .wrap {{ max-width:1120px; margin:0 auto; padding:18px; }}
    .hero {{
      padding:22px;
      border:1px solid var(--line);
      border-radius:26px;
      background:linear-gradient(135deg, rgba(17,29,50,.96), rgba(14,23,40,.88));
      box-shadow:0 20px 60px rgba(0,0,0,.28);
      margin-bottom:16px;
    }}
    .hero h1 {{ margin:0 0 8px; font-size:28px; }}
    .hero p {{ margin:0; color:var(--muted); }}
    .nav {{
      display:grid;
      grid-template-columns:repeat(4,minmax(0,1fr));
      gap:10px;
      margin:16px 0;
    }}
    .btn, button {{
      border:0;
      border-radius:16px;
      padding:13px 14px;
      background:linear-gradient(135deg, var(--accent), var(--accent2));
      color:#03101a;
      font-weight:800;
      cursor:pointer;
      text-align:center;
      font-size:14px;
      box-shadow:0 12px 28px rgba(35,211,164,.18);
    }}
    .btn.secondary, button.secondary {{
      background:#17243a;
      color:var(--text);
      border:1px solid var(--line);
      box-shadow:none;
    }}
    .btn.danger, button.danger {{
      background:linear-gradient(135deg, #ff7a7a, var(--danger));
      color:#fff;
    }}
    .grid {{
      display:grid;
      grid-template-columns:repeat(2,minmax(0,1fr));
      gap:14px;
    }}
    .card {{
      background:rgba(14,23,40,.92);
      border:1px solid var(--line);
      border-radius:22px;
      padding:16px;
      box-shadow:0 14px 42px rgba(0,0,0,.18);
    }}
    .card h2 {{ margin:0 0 12px; font-size:20px; }}
    .muted {{ color:var(--muted); }}
    .pill {{
      display:inline-flex;
      align-items:center;
      gap:6px;
      padding:6px 10px;
      border-radius:999px;
      background:#17243a;
      border:1px solid var(--line);
      color:var(--muted);
      font-size:13px;
      margin:2px 4px 2px 0;
    }}
    .ok {{ color:var(--accent); }}
    .bad {{ color:var(--danger); }}
    input {{
      width:100%;
      border:1px solid var(--line);
      border-radius:14px;
      padding:12px;
      background:#08111f;
      color:var(--text);
      outline:none;
      margin:6px 0 10px;
    }}
    label {{ display:block; color:var(--muted); font-size:13px; }}
    .row {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
    table {{ width:100%; border-collapse:collapse; overflow:hidden; }}
    th,td {{ padding:10px 8px; border-bottom:1px solid var(--line); text-align:left; font-size:14px; }}
    th {{ color:var(--muted); font-weight:600; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
    .actions form {{ margin:0; }}
    .small {{ font-size:12px; padding:9px 10px; border-radius:12px; }}
    @media(max-width:760px){{
      .grid,.nav,.row {{ grid-template-columns:1fr; }}
      .hero h1 {{ font-size:24px; }}
      th,td {{ font-size:13px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    {body}
  </div>
</body>
</html>
        """
    )


@router.get("", response_class=HTMLResponse)
async def mastercard_home(request: Request, user_id: int) -> HTMLResponse:
    if not await _is_mastercard_user(user_id):
        return await _render_access_denied()

    cards = await get_cards_by_owner(user_id)
    active_orders = await get_active_orders_by_master(user_id)
    completed_orders = await get_completed_orders_by_master(user_id)

    total_profit = 0
    for order in completed_orders:
        total_rub = float(order.get("total_rub") or 0)
        rub_amount = float(order.get("rub_amount") or 0)
        total_profit += math.ceil(max(total_rub - rub_amount, 0) * 0.35)

    session_started = _web_mc_sessions.get(user_id)
    session_text = (
        f"Активна с {session_started.strftime('%H:%M %d.%m.%Y')}"
        if session_started
        else "Не активна"
    )

    cards_html = ""
    for card in cards:
        card_id = int(card["card_id"])
        balance = await get_card_balance(card_id)
        status = bool(card.get("is_active", True))

        cards_html += f"""
        <div class="card" id="card-{card_id}">
          <h2>{'🟢' if status else '🔴'} {_esc(card.get('bank_name') or 'Банк')} #{card_id}</h2>
          <div class="pill">Баланс: <b>{_fmt_money(balance)}</b></div>
          <div class="pill">СБП: <b>{_esc(card.get('sbp_phone') or '—')}</b></div>
          <div class="pill">Карта: <b>{_esc(card.get('card_number') or '—')}</b></div>
          <div class="pill">Мин: <b>{_fmt_money(card.get('min_amount_rub'))}</b></div>
          <div class="pill">Макс: <b>{_fmt_money(card.get('max_amount_rub'))}</b></div>
          <div class="pill">День: <b>{_fmt_money(card.get('daily_limit_rub'))}</b></div>
          <div class="pill">Переводов/день: <b>{_esc(card.get('daily_transfer_limit') or '—')}</b></div>
          <div class="pill">Пауза: <b>{_esc(card.get('transfer_pause_minutes') or '—')} мин.</b></div>

          <form method="post" action="/mastercard/card/update">
            <input type="hidden" name="user_id" value="{user_id}">
            <input type="hidden" name="card_id" value="{card_id}">
            <div class="row">
              <div><label>Банк</label><input name="bank_name" value="{_esc(card.get('bank_name'))}"></div>
              <div><label>СБП</label><input name="sbp_phone" value="{_esc(card.get('sbp_phone'))}"></div>
            </div>
            <label>Номер карты</label>
            <input name="card_number" value="{_esc(card.get('card_number'))}">
            <div class="row">
              <div><label>Мин. сумма</label><input name="min_amount_rub" value="{_esc(card.get('min_amount_rub'))}"></div>
              <div><label>Макс. сумма</label><input name="max_amount_rub" value="{_esc(card.get('max_amount_rub'))}"></div>
            </div>
            <div class="row">
              <div><label>Дневной лимит</label><input name="daily_limit_rub" value="{_esc(card.get('daily_limit_rub'))}"></div>
              <div><label>Переводов в день</label><input name="daily_transfer_limit" value="{_esc(card.get('daily_transfer_limit'))}"></div>
            </div>
            <label>Пауза между переводами, минут</label>
            <input name="transfer_pause_minutes" value="{_esc(card.get('transfer_pause_minutes'))}">
            <button type="submit">💾 Сохранить</button>
          </form>

          <div class="actions">
            <form method="post" action="/mastercard/card/toggle">
              <input type="hidden" name="user_id" value="{user_id}">
              <input type="hidden" name="card_id" value="{card_id}">
              <button class="secondary small" type="submit">🔄 Вкл/Выкл</button>
            </form>
            <form method="post" action="/mastercard/card/delete" onsubmit="return confirm('Удалить карту?')">
              <input type="hidden" name="user_id" value="{user_id}">
              <input type="hidden" name="card_id" value="{card_id}">
              <button class="danger small" type="submit">🗑️ Удалить</button>
            </form>
          </div>
        </div>
        """

    if not cards_html:
        cards_html = '<div class="card"><h2>💳 Карт пока нет</h2><p class="muted">Добавьте первую карту ниже.</p></div>'

    active_rows = "".join(
        f"""
        <tr>
          <td>#{_esc(o.get('order_id'))}</td>
          <td>{_fmt_money(o.get('total_rub'))}</td>
          <td>{_esc(o.get('status'))}</td>
          <td>{_esc(o.get('created_at'))}</td>
        </tr>
        """
        for o in active_orders[:20]
    ) or '<tr><td colspan="4" class="muted">Активных заявок нет</td></tr>'

    completed_rows = "".join(
        f"""
        <tr>
          <td>#{_esc(o.get('order_id'))}</td>
          <td>{_fmt_money(o.get('total_rub'))}</td>
          <td>{_fmt_money(max(float(o.get('total_rub') or 0) - float(o.get('rub_amount') or 0), 0) * 0.35)}</td>
          <td>{_esc(o.get('completed_at'))}</td>
        </tr>
        """
        for o in completed_orders[:20]
    ) or '<tr><td colspan="4" class="muted">Завершённых заявок нет</td></tr>'

    body = f"""
    <div class="hero">
      <h1>💳 MasterCard кабинет</h1>
      <p>Управление картами, лимитами, сессией и заявками.</p>
    </div>

    <div class="nav">
      <a class="btn secondary" href="#cards">💳 Карты</a>
      <a class="btn secondary" href="#add">➕ Добавить</a>
      <a class="btn secondary" href="#orders">✅ Заявки</a>
      <a class="btn secondary" href="#report">📊 Отчёт</a>
    </div>

    <div class="grid">
      <div class="card">
        <h2>▶️ Сессия</h2>
        <p class="muted">{_esc(session_text)}</p>
        <div class="actions">
          <form method="post" action="/mastercard/session/start">
            <input type="hidden" name="user_id" value="{user_id}">
            <button type="submit">▶️ Начать</button>
          </form>
          <form method="post" action="/mastercard/session/end">
            <input type="hidden" name="user_id" value="{user_id}">
            <button class="danger" type="submit">⏹ Завершить</button>
          </form>
        </div>
      </div>

      <div class="card" id="report">
        <h2>📊 Отчёт</h2>
        <div class="pill">Завершённых заявок: <b>{len(completed_orders)}</b></div>
        <div class="pill">Прибыль: <b>{_fmt_money(total_profit)}</b></div>
      </div>
    </div>

    <div class="card" id="add" style="margin-top:14px;">
      <h2>➕ Добавить карту</h2>
      <form method="post" action="/mastercard/card/add">
        <input type="hidden" name="user_id" value="{user_id}">
        <div class="row">
          <div><label>Банк</label><input name="bank_name" required placeholder="Сбер"></div>
          <div><label>СБП</label><input name="sbp_phone" placeholder="+79991234567"></div>
        </div>
        <label>Номер карты</label>
        <input name="card_number" placeholder="16 цифр">
        <div class="row">
          <div><label>Мин. сумма</label><input name="min_amount_rub" value="{DEFAULT_MIN_AMOUNT_RUB}"></div>
          <div><label>Макс. сумма</label><input name="max_amount_rub" value="{DEFAULT_MAX_AMOUNT_RUB}"></div>
        </div>
        <div class="row">
          <div><label>Дневной лимит</label><input name="daily_limit_rub" value="{DEFAULT_DAILY_LIMIT_RUB}"></div>
          <div><label>Переводов в день</label><input name="daily_transfer_limit" value="{DEFAULT_DAILY_TRANSFER_LIMIT}"></div>
        </div>
        <label>Пауза между переводами, минут</label>
        <input name="transfer_pause_minutes" value="{DEFAULT_TRANSFER_PAUSE_MINUTES}">
        <button type="submit">✅ Добавить карту</button>
      </form>
    </div>

    <h2 id="cards" style="margin:20px 0 12px;">💳 Карты</h2>
    <div class="grid">{cards_html}</div>

    <div class="card" id="orders" style="margin-top:14px;">
      <h2>✅ Активные заявки</h2>
      <table>
        <thead><tr><th>ID</th><th>Сумма</th><th>Статус</th><th>Создана</th></tr></thead>
        <tbody>{active_rows}</tbody>
      </table>
    </div>

    <div class="card" style="margin-top:14px;">
      <h2>📦 Завершённые заявки</h2>
      <table>
        <thead><tr><th>ID</th><th>Сумма</th><th>Прибыль 35%</th><th>Завершена</th></tr></thead>
        <tbody>{completed_rows}</tbody>
      </table>
    </div>
    """
    return _page("MasterCard кабинет", body)


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
):
    if not await _is_mastercard_user(user_id):
        return _redirect(user_id)

    await add_card(
        owner_id=int(user_id),
        bank_name=bank_name.strip(),
        sbp_phone=sbp_phone.strip() or None,
        card_number=card_number.replace(" ", "").strip() or None,
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
):
    if not await _is_mastercard_user(user_id):
        return _redirect(user_id)

    card = await get_card_by_id(card_id)
    if not card or int(card.get("owner_id") or 0) != int(user_id):
        return _redirect(user_id, "cards")

    await update_card(
        int(card_id),
        bank_name=bank_name.strip(),
        sbp_phone=sbp_phone.strip() or None,
        card_number=card_number.replace(" ", "").strip() or None,
        min_amount_rub=_to_float_or_none(min_amount_rub),
        max_amount_rub=_to_float_or_none(max_amount_rub),
        daily_limit_rub=_to_float_or_none(daily_limit_rub),
        daily_transfer_limit=_to_int_or_none(daily_transfer_limit),
        transfer_pause_minutes=_to_int_or_none(transfer_pause_minutes),
    )
    return _redirect(user_id, f"card-{card_id}")


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

    await set_card_active(
        card_id=int(card_id),
        owner_id=int(user_id),
        is_active=not bool(card.get("is_active", True)),
    )
    return _redirect(user_id, f"card-{card_id}")


@router.post("/card/delete")
async def mastercard_delete_card(
    user_id: int = Form(...),
    card_id: int = Form(...),
):
    if not await _is_mastercard_user(user_id):
        return _redirect(user_id)

    await delete_card(card_id=int(card_id), owner_id=int(user_id))
    return _redirect(user_id, "cards")


@router.post("/session/start")
async def mastercard_session_start(user_id: int = Form(...)):
    if await _is_mastercard_user(user_id):
        _web_mc_sessions[int(user_id)] = datetime.now(timezone.utc)
    return _redirect(user_id)


@router.post("/session/end")
async def mastercard_session_end(user_id: int = Form(...)):
    if await _is_mastercard_user(user_id):
        _web_mc_sessions.pop(int(user_id), None)
    return _redirect(user_id)