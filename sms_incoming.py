# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
import hashlib
import json
import re
from typing import Any, Dict, Optional, Tuple

from aiohttp import web
from config.settings import settings


# -----------------------------------------------------------------------------
# Раздел: Константы и настройки
# -----------------------------------------------------------------------------
SMS_HOST: str = getattr(settings, "SMS_SERVER_HOST", "0.0.0.0")
SMS_PORT: int = int(getattr(settings, "SMS_SERVER_PORT", 8085))
SMS_SECRET: str = str(getattr(settings, "SMS_FORWARDER_SECRET", "") or "")

RE_LAST4 = re.compile(r"(?:\*{0,4}\s*|\b(?:VISA|MASTERCARD|MC)\s*)?(\d{4})\b")
RE_AMOUNT = re.compile(
    r"(?:(?:на|на\s+сумму|сумма)\s*)?(\d+(?:[.,]\d+)?)\s*(?:р|руб|₽)\b",
    re.IGNORECASE,
)


# -----------------------------------------------------------------------------
# Раздел: Утилиты
# -----------------------------------------------------------------------------
def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _event_hash(obj: Dict[str, Any]) -> str:
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _extract_fields(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Нормализует входящие поля из разных смс-форвардеров.
    Возвращает (sender, body, timestamp_str).
    """
    sender = (
        data.get("sender")
        or data.get("from")
        or data.get("address")
        or data.get("phone")
        or None
    )
    body = data.get("message") or data.get("body") or data.get("text") or None
    ts = data.get("timestamp") or data.get("time") or data.get("date") or None

    if sender is not None:
        sender = str(sender)

    if body is not None:
        body = str(body)

    if ts is not None:
        ts = str(ts)

    return sender, body, ts


def _parse_body(body: str) -> Tuple[Optional[str], Optional[float]]:
    """Пытается достать последние 4 цифры карты и сумму RUB из текста SMS."""
    m4 = RE_LAST4.search(body)
    last4 = m4.group(1) if m4 else None

    m_amt = RE_AMOUNT.search(body)
    amount_rub: Optional[float] = None
    if m_amt:
        raw = (m_amt.group(1) or "").replace(",", ".").strip()
        try:
            amount_rub = float(raw)
        except ValueError:
            amount_rub = None

    return last4, amount_rub


# -----------------------------------------------------------------------------
# Раздел: HTTP-хэндлер
# -----------------------------------------------------------------------------
async def handle_sms(request: web.Request) -> web.Response:
    """
    Принимает входящую SMS в формате JSON, валидирует, парсит и записывает событие.
    Ожидает секрет в заголовке X-Secret или query-параметре ?secret=...
    """
    if not SMS_SECRET:
        return _json_error("Server not configured: missing SMS_FORWARDER_SECRET", 500)

    secret_hdr = request.headers.get("X-Secret") or request.query.get("secret")
    if secret_hdr != SMS_SECRET:
        return _json_error("Forbidden", 403)

    try:
        data = await request.json()
    except Exception:
        return _json_error("Invalid JSON payload", 400)

    sender, body, ts = _extract_fields(data)
    if not body:
        return _json_error("Missing required field: message/body/text", 400)

    event_id = _event_hash({"s": sender, "b": body, "t": ts})

    last4, amount_rub = _parse_body(body)

    user_id: Optional[int] = None
    # Привязка SMS к пользователю через «брелок» удалена.
    # Событие сохраняем как есть, без определения user_id.

    from db.sms_events import insert_sms_event  # локальный импорт
    await insert_sms_event(
        event_hash=event_id,
        sender=sender,
        body=body,
        card_last4=last4,
        amount_rub=amount_rub,
        user_id=user_id,
        parsed_ok=bool(last4 and (amount_rub is not None)),
    )

    return web.json_response(
        {
            "ok": True,
            "event_id": event_id,
            "parsed": {"last4": last4, "amount_rub": amount_rub, "user_id": user_id},
        }
    )


# -----------------------------------------------------------------------------
# Раздел: Сборка приложения
# -----------------------------------------------------------------------------
def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/sms", handle_sms)
    return app


_runner: Optional[web.AppRunner] = None
_site: Optional[web.TCPSite] = None


async def start_sms_server() -> None:
    global _runner, _site
    if _runner is not None:
        return

    app = build_app()
    _runner = web.AppRunner(app)
    await _runner.setup()

    _site = web.TCPSite(_runner, SMS_HOST, SMS_PORT)
    await _site.start()


async def stop_sms_server() -> None:
    global _runner, _site
    try:
        if _site is not None:
            await _site.stop()
    finally:
        _site = None

    try:
        if _runner is not None:
            await _runner.cleanup()
    finally:
        _runner = None