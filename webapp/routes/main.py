from datetime import datetime, timezone
import html
from pathlib import Path
import math
import secrets

import utils.helpers as helpers
from utils.helpers import (
    btc_required_for_usdt_ff_float,
    get_binance_ticker_price,
    get_btc_price,
    get_usd_rub,
    validate_wallet_for_asset,
)

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from webapp.db import get_db_connection
from db.users import get_user_by_web_password, get_user_commission

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

SESSION_USER_ID_KEY = "user_id"
SESSION_GUEST_USER_ID_KEY = "guest_user_id"
SESSION_GUEST_ORDER_IDS_KEY = "guest_order_ids"

COMMISSION_PERCENT = 22.0
MIN_RUB_AMOUNT = 1200.0
ACTIVE_WEB_COMPLETION_HOLD_SECONDS = 60
WEB_ORDER_TTL_SECONDS = 30 * 60


def get_current_user_id(request: Request):
    user_id = request.session.get(SESSION_USER_ID_KEY)
    if user_id is not None:
        return user_id
    return request.session.get(SESSION_GUEST_USER_ID_KEY)


def is_guest_session(request: Request) -> bool:
    return SESSION_USER_ID_KEY not in request.session and SESSION_GUEST_USER_ID_KEY in request.session


def ensure_guest_user_id(request: Request) -> int:
    existing = request.session.get(SESSION_GUEST_USER_ID_KEY)
    if existing is not None:
        try:
            return int(existing)
        except Exception:
            pass

    guest_user_id = -secrets.randbelow(2_000_000_000) - 1
    request.session[SESSION_GUEST_USER_ID_KEY] = guest_user_id
    request.session.setdefault(SESSION_GUEST_ORDER_IDS_KEY, [])
    return guest_user_id


def get_guest_order_ids(request: Request):
    raw = request.session.get(SESSION_GUEST_ORDER_IDS_KEY, [])
    result = []

    if not isinstance(raw, list):
        return result

    for value in raw:
        try:
            result.append(int(value))
        except Exception:
            continue

    return result


def add_guest_order_id(request: Request, order_id: int):
    order_ids = get_guest_order_ids(request)
    if order_id not in order_ids:
        order_ids.insert(0, int(order_id))
    request.session[SESSION_GUEST_ORDER_IDS_KEY] = order_ids[:100]


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def _format_dt(value):
    if not value:
        return "—"

    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_local = dt.astimezone()
        return dt_local.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value)


def _parse_dt(value):
    if not value:
        return None

    try:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_order_active_for_web(order: dict) -> bool:
    status = str(order.get("status") or "").strip().lower()

    if status in ("canceled", "cancelled", "rejected", "expired"):
        return False

    tx_ready_dt = _parse_dt(order.get("tx_ready_at"))
    completed_dt = _parse_dt(order.get("completed_at"))
    hold_base_dt = tx_ready_dt or completed_dt

    if hold_base_dt is None:
        return status not in ("completed", "finished", "done", "closed")

    now = datetime.now(timezone.utc)
    return (now - hold_base_dt).total_seconds() < _get_order_completion_hold_seconds(order)


def _round_up_to_100(value: float) -> int:
    return int(math.ceil(float(value) / 100.0) * 100)


def _normalize_asset(asset: str) -> str:
    value = str(asset or "").strip().upper()
    return value if value in {"BTC", "LTC", "USDT", "XMR"} else "BTC"


def _min_rub_amount_for_asset(asset: str) -> float:
    asset = _normalize_asset(asset)
    if asset == "XMR":
        return 6000.0
    return 1200.0


def _get_order_completion_hold_seconds(order: dict) -> int:
    return ACTIVE_WEB_COMPLETION_HOLD_SECONDS


def _is_pending_order_expired(order: dict) -> bool:
    status = str(order.get("status") or "").strip().lower()
    if status != "pending":
        return False

    payment_confirmed_at = str(order.get("payment_confirmed_at") or "").strip()
    exchange_started_at = str(order.get("exchange_started_at") or "").strip()
    ff_funds_sent_at = str(order.get("ff_funds_sent_at") or "").strip()
    tx_ready_at = str(order.get("tx_ready_at") or "").strip()
    tx_to = str(order.get("tx_to") or "").strip()

    if payment_confirmed_at or exchange_started_at or ff_funds_sent_at or tx_ready_at or tx_to:
        return False

    created_dt = _parse_dt(order.get("created_at"))
    if created_dt is None:
        return False

    now = datetime.now(timezone.utc)
    age_seconds = (now - created_dt).total_seconds()
    return age_seconds >= WEB_ORDER_TTL_SECONDS


def _is_blocking_new_order(order: dict) -> bool:
    if not order:
        return False

    status = str(order.get("status") or "").strip().lower()
    if status in ("canceled", "cancelled", "rejected", "expired"):
        return False

    if _is_pending_order_expired(order):
        return False

    return _is_order_active_for_web(order)


async def _get_latest_user_order_for_web(current_user_id: int):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT *
            FROM p2p_orders
            WHERE user_id = ?
            ORDER BY COALESCE(order_id, 0) DESC, rowid DESC
            LIMIT 1
            """,
            (int(current_user_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def _get_new_order_block_context(request: Request, current_user_id: int | None, guest_mode: bool):
    if current_user_id is None:
        return None

    latest_order = await _get_latest_user_order_for_web(int(current_user_id))
    if not latest_order:
        return None

    if not _is_blocking_new_order(latest_order):
        return None

    order_id = _safe_int(latest_order.get("order_id"), 0)
    status_meta = _status_meta(latest_order)

    completed_dt = _parse_dt(latest_order.get("tx_ready_at")) or _parse_dt(latest_order.get("completed_at"))
    seconds_left = 0

    if completed_dt is not None:
        now = datetime.now(timezone.utc)
        seconds_left = max(
            0,
            int(_get_order_completion_hold_seconds(latest_order) - (now - completed_dt).total_seconds())
        )

    return {
        "request": request,
        "error": "Нельзя создавать новую заявку, пока предыдущая ещё активна.",
        "current_user_id": current_user_id,
        "is_guest_mode": guest_mode,
        "blocking_order_id": order_id,
        "blocking_order_status_label": status_meta.get("label") or "Активна",
        "blocking_order_hint": status_meta.get("hint") or "Сначала дождитесь завершения текущей заявки.",
        "blocking_order_seconds_left": seconds_left,
    }


async def _render_new_order_blocked_page(context: dict) -> HTMLResponse:
    request = context.get("request")
    current_user_id = context.get("current_user_id")
    is_guest_mode = bool(context.get("is_guest_mode"))
    order_id = _safe_int(context.get("blocking_order_id"), 0)
    status_label = str(context.get("blocking_order_status_label") or "Активна")
    hint = str(context.get("blocking_order_hint") or "Сначала отмените или завершите предыдущую заявку.")
    error = str(context.get("error") or "Предыдущая заявка ещё активна.")

    cancel_form = ""
    if order_id > 0:
        cancel_form = f"""
          <form method="post" action="/orders/{order_id}/cancel?next=/orders/new" style="margin:0">
            <button class="btn danger" type="submit">Отменить заявку</button>
          </form>
        """

    return HTMLResponse(
        f"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
  <title>Заявка активна</title>
  <style>
    :root {{
      --bg:#000;
      --card:#121318;
      --card2:#171820;
      --line:rgba(255,255,255,.18);
      --text:#f6f3ea;
      --muted:#a9acb4;
      --accent:#d6b35f;
      --accent2:#e1c46f;
      --danger:#ff6969;
    }}
    *{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
    html,body{{margin:0;width:100%;min-height:100%;background:#000;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}}
    body{{background:radial-gradient(circle at 50% 0%,rgba(214,179,95,.14),transparent 42%),#000}}
    .page{{min-height:100vh;min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:18px}}
    .box{{width:100%;max-width:430px;border:1px solid rgba(214,179,95,.26);border-radius:28px;background:linear-gradient(180deg,#171820,#0d0d10);box-shadow:0 24px 70px rgba(0,0,0,.72);overflow:hidden}}
    .head{{padding:22px 20px 12px;text-align:center}}
    .icon{{width:58px;height:58px;margin:0 auto 13px;border-radius:20px;display:grid;place-items:center;border:1px solid rgba(214,179,95,.30);background:rgba(214,179,95,.10);color:var(--accent2);font-size:28px;font-weight:950}}
    h1{{margin:0;font-size:22px;line-height:1.15;font-weight:950;letter-spacing:-.25px}}
    .text{{padding:0 22px 18px;text-align:center;color:var(--muted);font-size:14px;line-height:1.45}}
    .status{{margin:0 18px 16px;padding:12px;border:1px solid rgba(255,255,255,.12);border-radius:18px;background:rgba(255,255,255,.035)}}
    .row{{display:flex;justify-content:space-between;gap:12px;color:var(--muted);font-size:12px;line-height:1.35}}
    .row + .row{{margin-top:7px}}
    .row b{{color:var(--text);text-align:right}}
    .actions{{display:grid;gap:9px;padding:0 18px 18px}}
    .btn{{width:100%;min-height:48px;border:0;border-radius:17px;background:linear-gradient(135deg,#e1c46f,#caa24e);color:#161108;font-size:15px;font-weight:950;display:flex;align-items:center;justify-content:center;text-align:center;text-decoration:none;cursor:pointer}}
    .btn.ghost{{border:1px solid var(--line);background:rgba(255,255,255,.045);color:var(--muted)}}
    .btn.danger{{border:1px solid rgba(255,105,105,.28);background:rgba(255,105,105,.12);color:#ffd1d1}}
    .btn:active{{transform:scale(.99)}}
  </style>
</head>
<body>
  <main class="page">
    <section class="box">
      <div class="head">
        <div class="icon">!</div>
        <h1>Предыдущая заявка ещё активна</h1>
      </div>
      <div class="text">{html.escape(error)}<br>{html.escape(hint)}</div>
      <div class="status">
        <div class="row"><span>Заявка</span><b>#{order_id if order_id > 0 else "—"}</b></div>
        <div class="row"><span>Статус</span><b>{html.escape(status_label)}</b></div>
      </div>
      <div class="actions">
        <a class="btn ghost" href="/orders{('?focus=' + str(order_id)) if order_id > 0 else ''}">Открыть мои заявки</a>
        {cancel_form}
      </div>
    </section>
  </main>
</body>
</html>
        """,
        status_code=409,
    )


async def _calculate_coin_amount_for_web(asset: str, rub_amount: float) -> float:
    quote = await _build_order_quote_for_web(
        asset=asset,
        rub_amount=float(rub_amount),
        coin_amount=None,
    )
    return float(quote["coin_amount"])


async def _build_order_quote_for_web(
    *,
    asset: str,
    rub_amount: float | None = None,
    coin_amount: float | None = None,
    user_id: int | None = None,
) -> dict:
    asset = _normalize_asset(asset)

    rub_value = float(rub_amount) if rub_amount is not None else 0.0
    coin_value = float(coin_amount) if coin_amount is not None else 0.0

    has_rub = rub_value > 0
    has_coin = coin_value > 0

    if not has_rub and not has_coin:
        raise ValueError("Нужно передать сумму в рублях или в монете.")

    base_rate = await get_usd_rub()
    if base_rate is None or float(base_rate) <= 0:
        raise RuntimeError("Не удалось получить курс USD→₽.")
    base_rate = float(base_rate)

    # Комиссия WEB-версии как в P2P:
    # до 10000 ₽ — 25%
    # от 10000 ₽ — 23%
    calculation_rub_amount = float(rub_value) if has_rub else 0.0

    if calculation_rub_amount >= 10000:
        commission = 23.0
    else:
        commission = 25.0

    if asset == "USDT":
        if has_rub:
            rub_result = float(rub_value)
            coin_result = int(math.floor(rub_result / base_rate))
        else:
            coin_result = int(math.floor(coin_value))
            rub_result = float(coin_result) * base_rate

        if rub_result <= 0:
            raise RuntimeError("Ошибка расчёта суммы заявки.")

        if coin_result <= 0:
            raise RuntimeError("Сумма слишком мала — получается меньше 1 USDT.")

        total_rub = _round_up_to_100(
            float(rub_result) * (1 + float(commission) / 100.0)
        )

        rub_per_coin = float(rub_result) / float(coin_result)

        return {
            "coin": "USDT",
            "rub_amount": float(round(rub_result, 2)),
            "coin_amount": float(coin_result),
            "total_rub": int(total_rub),
            "commission_percent": float(commission),
            "usd_rub_rate": float(round(base_rate, 4)),
            "rub_per_coin": float(round(rub_per_coin, 8)),
        }

    if asset == "BTC":
        display_asset_usd = await get_btc_price()
        if not display_asset_usd or float(display_asset_usd) <= 0:
            raise RuntimeError("Не удалось получить цену BTC.")
        display_asset_usd = float(display_asset_usd)
    elif asset == "LTC":
        display_asset_usd = await get_binance_ticker_price("LTCUSDT")
        if not display_asset_usd or float(display_asset_usd) <= 0:
            raise RuntimeError("Не удалось получить цену LTC.")
        display_asset_usd = float(display_asset_usd)
    elif asset == "XMR":
        display_asset_usd = await get_binance_ticker_price("XMRUSDT")
        if not display_asset_usd or float(display_asset_usd) <= 0:
            raise RuntimeError("Не удалось получить цену XMR.")
        display_asset_usd = float(display_asset_usd)
    else:
        raise RuntimeError(f"Неподдерживаемый актив: {asset}")

    api_key = None
    api_secret = None
    usdt_ccy = "USDTTRC"

    try:
        from config.settings import settings

        api_key = getattr(settings, "FF_API_KEY", None)
        api_secret = getattr(settings, "FF_API_SECRET", None)
        usdt_ccy = getattr(settings, "FF_USDT_CCY", "USDTTRC")
    except Exception:
        pass

    async def _usdt_from_btc_ff_float(btc_amount: float) -> float | None:
        if asset != "BTC":
            return None
        if not (api_key and api_secret):
            return None
        if btc_amount <= 0:
            return 0.0

        est_usdt = display_asset_usd * btc_amount
        if est_usdt <= 0:
            est_usdt = 1.0

        low = 0.0
        high = max(est_usdt * 1.25, 5.0)

        for _ in range(4):
            try:
                req_btc = await btc_required_for_usdt_ff_float(
                    api_key=api_key,
                    api_secret=api_secret,
                    usdt_ccy=usdt_ccy,
                    usdt_target=float(high),
                )
            except Exception:
                req_btc = None

            if not req_btc:
                break
            if req_btc > btc_amount:
                break

            high *= 2.0

        best = 0.0
        for _ in range(18):
            mid = 0.5 * (low + high)
            try:
                req_btc = await btc_required_for_usdt_ff_float(
                    api_key=api_key,
                    api_secret=api_secret,
                    usdt_ccy=usdt_ccy,
                    usdt_target=float(mid),
                )
            except Exception:
                req_btc = None

            if not req_btc:
                break

            if req_btc <= btc_amount:
                best = mid
                low = mid
            else:
                high = mid

            if high - low < 1e-6:
                break

        return best if best > 0 else None

    if has_coin:
        coin_result = float(coin_value)

        if asset == "BTC":
            usdt_out_precise = await _usdt_from_btc_ff_float(float(coin_result))
            if usdt_out_precise and usdt_out_precise > 0:
                rub_result = float(usdt_out_precise) * base_rate
            else:
                rub_result = coin_result * display_asset_usd * base_rate * 0.995
        else:
            rub_result = coin_result * display_asset_usd * base_rate * 0.995
    else:
        rub_result = float(rub_value)
        usdt_target = rub_result / base_rate

        if asset == "BTC":
            asset_amt_precise = None
            if api_key and api_secret:
                try:
                    asset_amt_precise = await btc_required_for_usdt_ff_float(
                        api_key=api_key,
                        api_secret=api_secret,
                        usdt_ccy=usdt_ccy,
                        usdt_target=float(usdt_target),
                    )
                except Exception:
                    asset_amt_precise = None

            if asset_amt_precise and asset_amt_precise > 0:
                coin_result = float(asset_amt_precise)
            else:
                coin_result = (usdt_target / display_asset_usd) * 1.01
        else:
            coin_result = (usdt_target / display_asset_usd) * 1.01

    if rub_result <= 0 or coin_result <= 0:
        raise RuntimeError("Ошибка расчёта суммы заявки.")

    total_rub = _round_up_to_100(
        float(rub_result) * (1 + float(commission) / 100.0)
    )

    rub_per_coin = float(rub_result) / float(coin_result)

    result = {
        "coin": asset,
        "rub_amount": float(round(rub_result, 2)),
        "coin_amount": float(round(coin_result, 8)),
        "total_rub": int(total_rub),
        "commission_percent": float(commission),
        "usd_rub_rate": float(round(base_rate, 4)),
        "rub_per_coin": float(round(rub_per_coin, 8)),
    }

    return result




def _status_meta(order):
    status = str(order.get("status") or "").strip().lower()
    operator_id = _safe_int(order.get("operator_id"), 0)
    bank_card = str(order.get("bank_card") or "").strip()
    bank_name = str(order.get("bank_name") or "").strip()
    tx_to = str(order.get("tx_to") or "").strip()
    payment_method = str(order.get("payment_method") or "").strip().lower()
    payment_confirmed_at = str(order.get("payment_confirmed_at") or "").strip()

    exchange_started_at = str(order.get("exchange_started_at") or "").strip()
    ff_funds_sent_at = str(order.get("ff_funds_sent_at") or "").strip()
    tx_ready_at = str(order.get("tx_ready_at") or "").strip()
    completed_at = str(order.get("completed_at") or "").strip()

    if status in ("expired",):
        return {
            "label": "Истекла",
            "class": "danger",
            "hint": "Срок жизни web-заявки истёк. Неоплаченная заявка перенесена в историю.",
        }

    if status in ("canceled", "cancelled"):
        return {
            "label": "Отменена",
            "class": "danger",
            "hint": "Заявка отменена.",
        }

    if status in ("rejected",):
        return {
            "label": "Отклонена",
            "class": "danger",
            "hint": "Заявка была отклонена оператором.",
        }

    if tx_ready_at or tx_to:
        if _is_order_active_for_web(order):
            return {
                "label": "Перевод отправлен",
                "class": "success",
                "hint": "Ссылка на транзакцию уже готова. Детали перевода показаны ниже.",
            }
        return {
            "label": "Завершена",
            "class": "success",
            "hint": "Заявка завершена. Монеты отправлены или обмен закрыт.",
        }

    if status == "completed" or completed_at:
        if _is_order_active_for_web(order):
            return {
                "label": "Завершается",
                "class": "success",
                "hint": "Финальный этап завершён. Заявка ещё временно отображается в активных.",
            }
        return {
            "label": "Завершена",
            "class": "success",
            "hint": "Заявка завершена. Монеты отправлены или обмен закрыт.",
        }

    if ff_funds_sent_at:
        return {
            "label": "Обмен выполняется",
            "class": "success",
            "hint": "Средства уже отправлены на обменник. Ожидаем финальную транзакцию.",
        }

    if exchange_started_at:
        return {
            "label": "Обмен запущен",
            "class": "info",
            "hint": "Оператор начал обмен. Средства готовятся к отправке на обменник.",
        }

    if payment_confirmed_at:
        return {
            "label": "Ожидание запуска обмена",
            "class": "info",
            "hint": "Вы подтвердили оплату. Как только средства поступят, обмен начнется.",
        }

    if bank_card or bank_name:
        return {
            "label": "Ожидает оплату",
            "class": "warn",
            "hint": "Реквизиты выданы. Можно оплачивать заявку.",
        }

    if operator_id > 0:
        if payment_method == "akkula":
            return {
                "label": "Ожидает оплату",
                "class": "warn",
                "hint": "Оператор взял заявку. Ожидается завершение оплаты.",
            }
        return {
            "label": "Оператор подключён",
            "class": "info",
            "hint": "Оператор уже взял заявку и готовит реквизиты.",
        }

    return {
        "label": "Ожидает оператора",
        "class": "muted",
        "hint": "Заявка создана и отправлена операторам",
    }


def _serialize_order(row, focus_order_id=None):
    order = dict(row)

    order_id = _safe_int(order.get("order_id"))
    raw_status = str(order.get("status") or "").strip()
    status = raw_status.lower()

    coin_raw = str(order.get("payment_method") or "").strip().upper()
    if coin_raw in ("BTC", "LTC", "USDT", "XMR"):
        coin = coin_raw
    else:
        comment = str(order.get("comment") or "").upper()
        if "USDT" in comment:
            coin = "USDT"
        elif "LTC" in comment:
            coin = "LTC"
        elif "XMR" in comment:
            coin = "XMR"
        else:
            coin = "BTC"

    bank_card = str(order.get("bank_card") or "").strip()
    bank_name = str(order.get("bank_name") or "").strip()
    tx_to = str(order.get("tx_to") or "").strip()
    payment_confirmed_at = str(order.get("payment_confirmed_at") or "").strip()
    exchange_started_at = str(order.get("exchange_started_at") or "").strip()
    ff_funds_sent_at = str(order.get("ff_funds_sent_at") or "").strip()
    tx_ready_at = str(order.get("tx_ready_at") or "").strip()
    completed_at = str(order.get("completed_at") or "").strip()
    wallet = str(order.get("wallet") or "").strip()
    comment_raw = str(order.get("comment") or "").strip().upper()

    has_payment_details = bool(bank_card or bank_name)
    has_paid = bool(payment_confirmed_at)
    has_exchange_started = bool(exchange_started_at)
    has_ff_funds_sent = bool(ff_funds_sent_at)
    has_tx = bool(tx_to)
    has_tx_ready = bool(tx_ready_at or tx_to)

    show_web_progress = bool(
        has_paid
        or has_exchange_started
        or has_ff_funds_sent
        or has_tx_ready
    )

    web_step_payment_received = bool(has_exchange_started or has_ff_funds_sent or has_tx_ready)
    web_step_exchange_started = bool(has_ff_funds_sent or has_tx_ready)
    web_step_wallet_transfer = bool(
        has_tx_ready
        or has_tx
        or completed_at
    )

    can_mark_paid = bool(
        has_payment_details
        and not has_paid
        and not has_tx
        and status == "pending"
    )

    can_cancel = bool(
        has_payment_details
        and not has_paid
        and not has_tx
        and status == "pending"
    )

    hold_base_dt = _parse_dt(tx_ready_at) or _parse_dt(completed_at)
    hold_seconds_left = 0
    if hold_base_dt is not None:
        now = datetime.now(timezone.utc)
        hold_seconds_left = max(0, int(_get_order_completion_hold_seconds(order) - (now - hold_base_dt).total_seconds()))

    created_dt = _parse_dt(order.get("created_at"))
    is_web_order = comment_raw.startswith("WEB")
    web_expire_seconds_left = 0

    if (
        is_web_order
        and status == "pending"
        and created_dt is not None
        and not has_paid
        and not has_exchange_started
        and not has_ff_funds_sent
        and not has_tx_ready
    ):
        now = datetime.now(timezone.utc)
        web_expire_seconds_left = max(
            0,
            int(WEB_ORDER_TTL_SECONDS - (now - created_dt).total_seconds())
        )

    show_web_expire_timer = bool(
        is_web_order
        and status == "pending"
        and web_expire_seconds_left > 0
    )

    meta = _status_meta(order)
    is_active_for_web = _is_order_active_for_web(order)

    order["order_id"] = order_id
    order["coin"] = coin
    order["btc_amount"] = _safe_float(order.get("btc_amount"))
    order["rub_amount"] = _safe_float(order.get("rub_amount"))
    order["total_rub"] = _safe_float(order.get("total_rub"))
    order["operator_id"] = _safe_int(order.get("operator_id"))
    order["wallet"] = wallet
    order["bank_card"] = bank_card
    order["bank_name"] = bank_name
    order["tx_to"] = tx_to
    order["payment_confirmed_at"] = payment_confirmed_at
    order["exchange_started_at"] = exchange_started_at
    order["ff_funds_sent_at"] = ff_funds_sent_at
    order["tx_ready_at"] = tx_ready_at
    order["completed_at"] = completed_at

    order["has_payment_details"] = has_payment_details
    order["can_mark_paid"] = can_mark_paid
    order["can_cancel"] = can_cancel

    order["status_label"] = meta["label"]
    order["status_class"] = meta["class"]
    order["status_hint"] = meta["hint"]

    order["created_at_view"] = _format_dt(order.get("created_at"))
    order["completed_at_view"] = _format_dt(completed_at)
    order["payment_confirmed_at_view"] = _format_dt(payment_confirmed_at)
    order["exchange_started_at_view"] = _format_dt(exchange_started_at)
    order["ff_funds_sent_at_view"] = _format_dt(ff_funds_sent_at)
    order["tx_ready_at_view"] = _format_dt(tx_ready_at)

    order["is_focus"] = focus_order_id is not None and order_id == focus_order_id
    order["order_status_raw"] = raw_status

    order["show_web_progress"] = show_web_progress
    order["web_step_payment_received"] = web_step_payment_received
    order["web_step_exchange_started"] = web_step_exchange_started
    order["web_step_wallet_transfer"] = web_step_wallet_transfer

    order["is_active_for_web"] = is_active_for_web
    order["show_tx_details"] = bool(has_tx_ready)
    order["hold_seconds_left"] = hold_seconds_left
    order["hold_minutes_left"] = max(1, math.ceil(hold_seconds_left / 60)) if hold_seconds_left > 0 else 0

    order["show_web_expire_timer"] = show_web_expire_timer
    order["web_expire_seconds_left"] = web_expire_seconds_left
    order["web_expire_minutes_left"] = max(1, math.ceil(web_expire_seconds_left / 60)) if web_expire_seconds_left > 0 else 0

    return order


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return RedirectResponse(url="/orders/new", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "current_user_id": request.session.get(SESSION_USER_ID_KEY),
            "is_guest_mode": is_guest_session(request),
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def login_action(
    request: Request,
    password: str = Form(...),
):
    password = (password or "").strip()

    if not password:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Введите пароль.",
                "current_user_id": request.session.get(SESSION_USER_ID_KEY),
                "is_guest_mode": is_guest_session(request),
            },
            status_code=400,
        )

    try:
        user = await get_user_by_web_password(password)
    except Exception as e:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": f"Ошибка проверки пароля: {e}",
                "current_user_id": request.session.get(SESSION_USER_ID_KEY),
                "is_guest_mode": is_guest_session(request),
            },
            status_code=500,
        )

    if not user:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Неверный пароль.",
                "current_user_id": request.session.get(SESSION_USER_ID_KEY),
                "is_guest_mode": is_guest_session(request),
            },
            status_code=400,
        )

    user_id = int(user["telegram_id"])

    request.session.pop(SESSION_GUEST_USER_ID_KEY, None)
    request.session.pop(SESSION_GUEST_ORDER_IDS_KEY, None)
    request.session[SESSION_USER_ID_KEY] = user_id

    return RedirectResponse(url="/orders/new", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    users = []
    users_count = 0
    error = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]

        cur.execute("SELECT * FROM users ORDER BY rowid DESC LIMIT 50")
        users = [dict(row) for row in cur.fetchall()]

        conn.close()

    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "users": users,
            "users_count": users_count,
            "error": error,
            "current_user_id": get_current_user_id(request),
            "is_guest_mode": is_guest_session(request),
        },
    )


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    current_user_id = get_current_user_id(request)
    guest_mode = is_guest_session(request)

    orders = []
    orders_count = 0
    latest_order = None
    focus_order_id = _safe_int(request.query_params.get("focus"), 0)
    error = None

    try:
        # Сначала мягко истекаем старые WEB-заявки текущего пользователя,
        # чтобы они сразу уходили из active в history.
        if current_user_id is not None:
            try:
                from db.p2p import expire_stale_web_orders
                await expire_stale_web_orders(int(current_user_id))
            except Exception:
                pass

        conn = get_db_connection()
        cur = conn.cursor()

        if guest_mode:
            guest_order_ids = get_guest_order_ids(request)

            if guest_order_ids:
                placeholders = ",".join(["?"] * len(guest_order_ids))

                cur.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM p2p_orders
                    WHERE order_id IN ({placeholders})
                    """,
                    tuple(guest_order_ids),
                )
                orders_count = cur.fetchone()[0]

                cur.execute(
                    f"""
                    SELECT *
                    FROM p2p_orders
                    WHERE order_id IN ({placeholders})
                    ORDER BY COALESCE(order_id, 0) DESC, rowid DESC
                    LIMIT 20
                    """,
                    tuple(guest_order_ids),
                )
                rows = cur.fetchall()
                orders = [_serialize_order(row, focus_order_id=focus_order_id) for row in rows]
            else:
                orders = []
                orders_count = 0

        elif current_user_id:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM p2p_orders
                WHERE user_id = ?
                """,
                (int(current_user_id),),
            )
            orders_count = cur.fetchone()[0]

            cur.execute(
                """
                SELECT *
                FROM p2p_orders
                WHERE user_id = ?
                ORDER BY COALESCE(order_id, 0) DESC, rowid DESC
                LIMIT 20
                """,
                (int(current_user_id),),
            )
            rows = cur.fetchall()
            orders = [_serialize_order(row, focus_order_id=focus_order_id) for row in rows]

        if orders:
            active_orders = [order for order in orders if order.get("is_active_for_web")]
            history_orders = [order for order in orders if not order.get("is_active_for_web")]

            if focus_order_id > 0:
                focused = next(
                    (
                        order
                        for order in orders
                        if int(order.get("order_id") or 0) == focus_order_id
                    ),
                    None,
                )
                latest_order = focused or (active_orders[0] if active_orders else orders[0])
            else:
                latest_order = active_orders[0] if active_orders else orders[0]

            orders = active_orders + history_orders

        conn.close()

    except Exception as e:
        error = str(e)

    return templates.TemplateResponse(
        "orders.html",
        {
            "request": request,
            "orders": orders,
            "orders_count": orders_count,
            "latest_order": latest_order,
            "focus_order_id": focus_order_id,
            "error": error,
            "current_user_id": current_user_id,
            "is_guest_mode": guest_mode,
        },
    )


@router.get("/orders/new", response_class=HTMLResponse)
async def new_order_page(request: Request):
    current_user_id = get_current_user_id(request)
    guest_mode = is_guest_session(request)

    if current_user_id is None:
        current_user_id = ensure_guest_user_id(request)
        guest_mode = True

    block_context = await _get_new_order_block_context(
        request=request,
        current_user_id=int(current_user_id) if current_user_id is not None else None,
        guest_mode=guest_mode,
    )
    if block_context:
        return await _render_new_order_blocked_page(block_context)

    return templates.TemplateResponse(
        "order_new.html",
        {
            "request": request,
            "error": None,
            "current_user_id": current_user_id,
            "is_guest_mode": guest_mode,
        },
    )


@router.get("/orders/calculate")
async def calculate_order_values(
    request: Request,
    coin: str,
    rub_amount: float | None = None,
    coin_amount: float | None = None,
):
    try:
        has_rub = rub_amount is not None and float(rub_amount) > 0
        has_coin = coin_amount is not None and float(coin_amount) > 0

        if not has_rub and not has_coin:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Нужно указать сумму в рублях или в монете.",
                },
                status_code=400,
            )

        raw_current_user_id = get_current_user_id(request)
        try:
            current_user_id = int(raw_current_user_id) if raw_current_user_id is not None else None
        except Exception:
            current_user_id = None

        asset = _normalize_asset(coin)
        min_rub_amount = _min_rub_amount_for_asset(asset)

        quote = await _build_order_quote_for_web(
            asset=asset,
            rub_amount=float(rub_amount) if has_rub else None,
            coin_amount=float(coin_amount) if has_coin else None,
            user_id=current_user_id,
        )

        if float(quote["rub_amount"]) < min_rub_amount:
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"Минимальная сумма заявки для {asset} — {int(min_rub_amount)} ₽.",
                    **quote,
                },
                status_code=400,
            )

        return JSONResponse(
            {
                "ok": True,
                "min_rub_amount": float(min_rub_amount),
                **quote,
            }
        )

    except Exception as e:
        return JSONResponse(
            {
                "ok": False,
                "error": str(e),
            },
            status_code=500,
        )


@router.post("/orders/new", response_class=HTMLResponse)
async def create_order(
    request: Request,
    coin: str = Form(...),
    rub_amount: float = Form(...),
    btc_amount: float = Form(0.0),
    wallet: str = Form(...),
    payment_details_type: str = Form("card"),
    comment: str = Form("по P2P"),
):
    raw_current_user_id = get_current_user_id(request)
    guest_mode = is_guest_session(request)

    try:
        current_user_id = int(raw_current_user_id) if raw_current_user_id is not None else None
    except Exception:
        current_user_id = None

    if current_user_id is not None and current_user_id > 0:
        guest_mode = False
        request.session[SESSION_USER_ID_KEY] = int(current_user_id)
        request.session.pop(SESSION_GUEST_USER_ID_KEY, None)
        request.session.pop(SESSION_GUEST_ORDER_IDS_KEY, None)
    else:
        current_user_id = ensure_guest_user_id(request)
        guest_mode = True

    block_context = await _get_new_order_block_context(
        request=request,
        current_user_id=int(current_user_id) if current_user_id is not None else None,
        guest_mode=guest_mode,
    )
    if block_context:
        return await _render_new_order_blocked_page(block_context)

    try:
        from aiogram import Bot
        from aiogram.utils.exceptions import (
            BotBlocked,
            CantInitiateConversation,
            ChatNotFound,
            Unauthorized,
        )

        from config.settings import settings
        from db.p2p import (
            delete_operator_notifications_by_order,
            delete_order,
            get_pending_order,
            save_operator_notification,
            save_p2p_order,
        )
        from db.users import get_all_users
        from handlers.buy.p2p import _format_operator_card, _user_mention
        from handlers.common import active_mc_sessions, pending_operator_messages
        from keyboards.inline import operator_keyboard

        asset = _normalize_asset(coin)
        wallet_value = str(wallet or "").strip()

        is_wallet_valid, wallet_error = validate_wallet_for_asset(asset, wallet_value)
        if not is_wallet_valid:
            return templates.TemplateResponse(
                "order_new.html",
                {
                    "request": request,
                    "error": wallet_error,
                    "current_user_id": current_user_id,
                    "is_guest_mode": guest_mode,
                },
                status_code=400,
            )

        safe_comment = (comment or "").strip() or "по P2P"

        payment_details_type_raw = str(payment_details_type or "").strip().lower()
        if payment_details_type_raw == "sbp":
            payment_method = "sbp"
            payment_details_label = "СБП"
        else:
            payment_method = "card"
            payment_details_label = "Номер карты"

        source_prefix = "WEB-GUEST" if guest_mode else "WEB"
        web_comment = f"{source_prefix} | {safe_comment} | {payment_details_label} ({asset})"

        base_rub_amount = float(rub_amount)
        min_rub_amount = _min_rub_amount_for_asset(asset)

        if base_rub_amount < min_rub_amount:
            return templates.TemplateResponse(
                "order_new.html",
                {
                    "request": request,
                    "error": f"Минимальная сумма заявки для {asset} — {int(min_rub_amount)} ₽.",
                    "current_user_id": current_user_id,
                    "is_guest_mode": guest_mode,
                },
                status_code=400,
            )

        quote = await _build_order_quote_for_web(
            asset=asset,
            rub_amount=base_rub_amount,
            coin_amount=None,
            user_id=int(current_user_id) if current_user_id is not None else None,
        )

        calculated_coin_amount = float(quote["coin_amount"])
        final_total_rub = int(quote["total_rub"])
        final_rub_amount = float(quote["rub_amount"])

        bot_token = (
            getattr(settings, "bot_token", None)
            or getattr(settings, "BOT_TOKEN", None)
            or getattr(settings, "telegram_bot_token", None)
            or getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        )
        if not bot_token:
            raise RuntimeError("Не найден токен Telegram-бота в settings")

        bot = Bot(token=bot_token)
        try:
            if guest_mode:
                user_mention = "WEB-гость"
            else:
                user_mention = await _user_mention(bot, int(current_user_id))

            existing = await get_pending_order(int(current_user_id))
            if existing and str(existing.get("status") or "").lower() == "pending":
                old_order_id = int(existing.get("order_id") or 0)
                await delete_order(int(current_user_id))
                if old_order_id > 0:
                    try:
                        await delete_operator_notifications_by_order(old_order_id)
                    except Exception:
                        pass

            order_id = await save_p2p_order(
                user_id=int(current_user_id),
                operator_id=0,
                btc_amount=float(calculated_coin_amount),
                rub_amount=float(final_rub_amount),
                total_rub=float(final_total_rub),
                wallet=wallet_value,
                comment=web_comment,
                user_link=user_mention,
                payment_method=payment_method,
            )

            if guest_mode:
                add_guest_order_id(request, int(order_id))

            try:
                await delete_operator_notifications_by_order(int(order_id))
            except Exception:
                pass

            ops = []
            all_users = await get_all_users()
            for u in all_users:
                role = u.get("role")
                tid = u.get("telegram_id")
                if isinstance(tid, int) and (
                    role in ("Operator", "Admin")
                    or (role == "MasterCard" and tid in active_mc_sessions)
                ):
                    ops.append(tid)

            pending_operator_messages[int(current_user_id)] = []

            header = "🌐 WEB-заявка (гость)" if guest_mode else "🌐 WEB-заявка"

            text_for_ops = _format_operator_card(
                order_id=int(order_id),
                user_mention=user_mention,
                btc_amount=float(calculated_coin_amount),
                total_rub=int(final_total_rub),
                header=header,
                asset=asset,
            )

            for op in ops:
                try:
                    sent = await bot.send_message(
                        op,
                        text_for_ops,
                        parse_mode="HTML",
                        reply_markup=operator_keyboard(int(current_user_id), int(order_id)),
                    )

                    pending_operator_messages[int(current_user_id)].append(
                        (sent.chat.id, sent.message_id)
                    )

                    try:
                        await save_operator_notification(
                            order_id=int(order_id),
                            user_id=int(current_user_id),
                            operator_id=int(op),
                            chat_id=int(sent.chat.id),
                            message_id=int(sent.message_id),
                        )
                    except Exception:
                        pass

                except (ChatNotFound, BotBlocked, CantInitiateConversation, Unauthorized):
                    continue
        finally:
            await bot.session.close()

        return RedirectResponse(url=f"/orders?focus={int(order_id)}", status_code=303)

    except Exception as e:
        return templates.TemplateResponse(
            "order_new.html",
            {
                "request": request,
                "error": str(e),
                "current_user_id": current_user_id,
                "is_guest_mode": guest_mode,
            },
            status_code=500,
        )


@router.post("/orders/{order_id}/paid")
async def mark_order_paid(request: Request, order_id: int):
    current_user_id = get_current_user_id(request)
    guest_mode = is_guest_session(request)

    if current_user_id is None:
        return RedirectResponse(url="/orders/new", status_code=303)

    try:
        from aiogram import Bot
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from config.settings import settings
        from db.p2p import (
            get_order_by_id,
            mark_order_paid_from_web,
            save_operator_notification,
            try_claim_p2p_action,
        )
        from db.users import get_all_users, get_user

        if guest_mode:
            allowed_order_ids = set(get_guest_order_ids(request))
            if int(order_id) not in allowed_order_ids:
                return RedirectResponse(url="/orders", status_code=303)

        updated = await mark_order_paid_from_web(int(order_id), int(current_user_id))
        if not updated:
            return RedirectResponse(url=f"/orders?focus={int(order_id)}", status_code=303)

        order = await get_order_by_id(int(order_id))
        if not order:
            return RedirectResponse(url="/orders", status_code=303)

        operator_id = _safe_int(order.get("operator_id"), 0)
        if operator_id <= 0:
            return RedirectResponse(url=f"/orders?focus={int(order_id)}", status_code=303)

        if not await try_claim_p2p_action(int(order_id), "web_paid_notify_operator"):
            return RedirectResponse(url=f"/orders?focus={int(order_id)}", status_code=303)

        bot_token = (
            getattr(settings, "bot_token", None)
            or getattr(settings, "BOT_TOKEN", None)
            or getattr(settings, "telegram_bot_token", None)
            or getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        )
        if not bot_token:
            return RedirectResponse(url=f"/orders?focus={int(order_id)}", status_code=303)

        bot = Bot(token=bot_token)
        try:
            op_user = await get_user(int(operator_id))
            if not op_user:
                return RedirectResponse(url=f"/orders?focus={int(order_id)}", status_code=303)

            card = str(order.get("bank_card") or "").strip() or "—"
            bank = str(order.get("bank_name") or "").strip() or "—"
            amount_rub = math.ceil(float(order.get("total_rub") or 0))
            mention = str(order.get("user_link") or f"user_id={int(current_user_id)}")

            admin_text = (
                "━━━━━━━━━━━━━━━━━━\n"
                "‼️<b>Подтверждение оплаты из WEB!</b>‼️\n\n"
                f"👤 {mention}\n"
                f"🆔 <b>Заявка №{int(order_id)}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💳 <b>Карта:</b> <code>{card}</code>\n"
                f"🏦 <b>Банк:</b> {bank}\n"
                f"💸 <b>Сумма:</b> <b>{amount_rub} ₽</b>\n"
                "━━━━━━━━━━━━━━━━━━"
            )

            ikb = InlineKeyboardMarkup()
            ikb.row(
                InlineKeyboardButton(
                    "🧾 Чек",
                    callback_data=f"op_view_receipt:{int(order_id)}:{int(current_user_id)}",
                ),
                InlineKeyboardButton(
                    "📥 Заявка",
                    callback_data=f"operator_open_order:{int(current_user_id)}:{int(order_id)}",
                ),
            )
            ikb.add(
                InlineKeyboardButton(
                    "✅ Готово — начать обмен",
                    callback_data=f"ff_ready:{int(order_id)}:{int(current_user_id)}",
                )
            )
            ikb.add(
                InlineKeyboardButton(
                    "✅ Завершить",
                    callback_data=f"finish_order:{int(order_id)}:{int(current_user_id)}",
                )
            )

            recipients: set[int] = set()

            if op_user:
                recipients.add(int(operator_id))

            try:
                all_users = await get_all_users()
            except Exception:
                all_users = []

            for user in all_users:
                try:
                    tid = int(user.get("telegram_id") or 0)
                    role = str(user.get("role") or "").strip().lower()
                except Exception:
                    continue

                if tid > 0 and role == "admin":
                    recipients.add(tid)

            for recipient_id in sorted(recipients):
                try:
                    sent = await bot.send_message(
                        int(recipient_id),
                        admin_text,
                        parse_mode="HTML",
                        reply_markup=ikb,
                    )
                    try:
                        await save_operator_notification(
                            order_id=int(order_id),
                            user_id=int(current_user_id),
                            operator_id=int(recipient_id),
                            chat_id=int(sent.chat.id),
                            message_id=int(sent.message_id),
                        )
                    except Exception:
                        pass
                except Exception:
                    continue
        finally:
            await bot.session.close()

        return RedirectResponse(url=f"/orders?focus={int(order_id)}", status_code=303)

    except Exception:
        return RedirectResponse(url=f"/orders?focus={int(order_id)}", status_code=303)


@router.post("/orders/{order_id}/cancel")
async def cancel_order(request: Request, order_id: int):
    current_user_id = get_current_user_id(request)
    guest_mode = is_guest_session(request)
    next_url = str(request.query_params.get("next") or "").strip()
    redirect_after_cancel = next_url if next_url in {"/orders/new", "/orders"} else f"/orders?focus={int(order_id)}"

    if current_user_id is None:
        return RedirectResponse(url="/orders/new", status_code=303)

    try:
        from aiogram import Bot
        from config.settings import settings
        from db.p2p import (
            delete_operator_notifications_by_order,
            get_operator_notifications_by_order,
            get_order_by_id,
        )
        from handlers.chat.utils import safe_delete
        from handlers.common import pending_operator_messages

        if guest_mode:
            allowed_order_ids = set(get_guest_order_ids(request))
            if int(order_id) not in allowed_order_ids:
                return RedirectResponse(url="/orders", status_code=303)

        order = await get_order_by_id(int(order_id))
        if not order:
            return RedirectResponse(url="/orders", status_code=303)

        order_user_id = _safe_int(order.get("user_id"), 0)
        if order_user_id != int(current_user_id):
            return RedirectResponse(url="/orders", status_code=303)

        status = str(order.get("status") or "").strip().lower()
        payment_confirmed_at = str(order.get("payment_confirmed_at") or "").strip()
        exchange_started_at = str(order.get("exchange_started_at") or "").strip()
        ff_funds_sent_at = str(order.get("ff_funds_sent_at") or "").strip()
        tx_ready_at = str(order.get("tx_ready_at") or "").strip()
        tx_to = str(order.get("tx_to") or "").strip()
        operator_id = _safe_int(order.get("operator_id"), 0)

        # Разрешаем отменить зависшую web-заявку даже после кнопки "Оплатил",
        # но только пока обмен ещё не запущен и транзакция не отправлена.
        can_cancel = bool(
            status == "pending"
            and not exchange_started_at
            and not ff_funds_sent_at
            and not tx_ready_at
            and not tx_to
        )

        if not can_cancel:
            return RedirectResponse(url=redirect_after_cancel, status_code=303)

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE p2p_orders
                SET status = ?, completed_at = ?
                WHERE order_id = ? AND user_id = ?
                """,
                (
                    "canceled",
                    datetime.now(timezone.utc).isoformat(),
                    int(order_id),
                    int(current_user_id),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        bot_token = (
            getattr(settings, "bot_token", None)
            or getattr(settings, "BOT_TOKEN", None)
            or getattr(settings, "telegram_bot_token", None)
            or getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        )

        if bot_token:
            bot = Bot(token=bot_token, parse_mode="HTML")
            try:
                # 1) Исходные уведомления о новой WEB-заявке у админов/операторов из памяти
                pending_msgs = pending_operator_messages.pop(int(order_user_id), [])
                for chat_id, message_id in pending_msgs:
                    try:
                        await safe_delete(bot, int(chat_id), int(message_id))
                    except Exception:
                        pass

                # 2) Исходные уведомления о новой WEB-заявке у админов/операторов из БД
                try:
                    db_notifications = await get_operator_notifications_by_order(int(order_id))
                except Exception:
                    db_notifications = []

                for item in db_notifications:
                    try:
                        chat_id = int(item.get("chat_id"))
                        message_id = int(item.get("message_id"))
                    except Exception:
                        continue

                    try:
                        await safe_delete(bot, chat_id, message_id)
                    except Exception:
                        pass

                # 3) Если заявку уже принял оператор — удаляем его карточку заявки
                if operator_id > 0:
                    try:
                        from handlers.chat.operator import operator_order_msgs

                        op_msg_id = operator_order_msgs.pop(
                            (int(operator_id), int(order_id)),
                            None,
                        )
                        if op_msg_id:
                            await safe_delete(bot, int(operator_id), int(op_msg_id))
                    except Exception:
                        pass

            finally:
                await bot.session.close()

        # 4) В любом случае чистим БД-хранилище уведомлений по заявке
        try:
            await delete_operator_notifications_by_order(int(order_id))
        except Exception:
            pass

        return RedirectResponse(url=redirect_after_cancel, status_code=303)

    except Exception:
        return RedirectResponse(url=redirect_after_cancel, status_code=303)




