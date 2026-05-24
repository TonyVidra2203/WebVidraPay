from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from config.settings import (
    NIRVANA_API_PRIVATE,
    NIRVANA_API_PUBLIC,
    NIRVANA_BASE_URL,
    NIRVANA_CALLBACK_URL,
    NIRVANA_CURRENCY,
    NIRVANA_TIMEOUT_SEC,
    NIRVANA_TOKEN,
)
from db.nirvana_orders import save_nirvana_order
from services.nirvana import (
    NirvanaClient,
    build_nirvana_callback_url,
)


def build_nirvana_client_id(*, p2p_order_id: int, tg_user_id: int) -> str:
    return f"p2p_{int(p2p_order_id)}_{int(tg_user_id)}_{int(time.time())}"


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_nested_dict(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _extract_nirvana_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = _get_nested_dict(payload, "data")
    source = data if data else payload
    extra = _get_nested_dict(source, "extra")

    return {
        "tracker_id": source.get("trackerID") or source.get("trackerId") or source.get("id"),
        "status": str(source.get("status") or payload.get("status") or "CREATED").upper(),
        "amount_crypto": _safe_float(source.get("amountCrypto")),
        "rate": _safe_float(source.get("rate")),
        "receiver": source.get("receiver"),
        "bank_name": extra.get("bankName"),
        "recipient_name": extra.get("recipientName"),
        "redirect_url": (
            source.get("redirectURL")
            or source.get("redirectUrl")
            or source.get("paymentURL")
            or source.get("paymentUrl")
            or payload.get("redirectURL")
            or payload.get("redirectUrl")
            or ""
        ),
    }


async def create_nirvana_payin_for_p2p_order(
    *,
    p2p_order_id: int,
    tg_user_id: int,
    amount: int | float,
    token: Optional[str] = None,
    currency: Optional[str] = None,
    user_ip: str = "127.0.0.1",
    user_agent: str = "TelegramBot",
    user_email: str = "client@example.com",
) -> Dict[str, Any]:
    client_id = build_nirvana_client_id(
        p2p_order_id=p2p_order_id,
        tg_user_id=tg_user_id,
    )

    callback_url = build_nirvana_callback_url(
        NIRVANA_CALLBACK_URL,
        order_id=p2p_order_id,
        client_id=client_id,
    )

    selected_token = str(token or NIRVANA_TOKEN).strip()
    selected_currency = str(currency or NIRVANA_CURRENCY).strip()

    client = NirvanaClient(
        api_public=NIRVANA_API_PUBLIC,
        api_private=NIRVANA_API_PRIVATE,
        base_url=NIRVANA_BASE_URL,
        timeout_sec=NIRVANA_TIMEOUT_SEC,
    )

    response = await client.create_payin(
        client_id=client_id,
        amount=amount,
        token=selected_token,
        currency=selected_currency,
        callback_url=callback_url,
        user_ip=user_ip,
        user_agent=user_agent,
        user_email=user_email,
        user_id=str(tg_user_id),
    )

    extracted = _extract_nirvana_payload(response)

    await save_nirvana_order(
        client_id=client_id,
        tracker_id=extracted.get("tracker_id"),
        p2p_order_id=int(p2p_order_id),
        tg_user_id=int(tg_user_id),
        status=extracted.get("status") or "CREATED",
        amount=float(amount),
        amount_crypto=extracted.get("amount_crypto"),
        rate=extracted.get("rate"),
        token=selected_token,
        currency=selected_currency,
        receiver=extracted.get("receiver"),
        bank_name=extracted.get("bank_name"),
        recipient_name=extracted.get("recipient_name"),
        redirect_url=extracted.get("redirect_url"),
        callback_url=callback_url,
        raw_create_response=json.dumps(response, ensure_ascii=False),
    )

    return {
        "client_id": client_id,
        "callback_url": callback_url,
        "token": selected_token,
        "currency": selected_currency,
        "raw": response,
        **extracted,
    }


async def create_nirvana_ns_pk_qr_order(
    *,
    p2p_order_id: int,
    tg_user_id: int,
    amount: int | float,
    user_ip: str = "127.0.0.1",
    user_agent: str = "TelegramBot",
    user_email: str = "client@example.com",
) -> Dict[str, Any]:
    payment = await create_nirvana_payin_for_p2p_order(
        p2p_order_id=p2p_order_id,
        tg_user_id=tg_user_id,
        amount=amount,
        token="НСПК",
        currency="RUB",
        user_ip=user_ip,
        user_agent=user_agent,
        user_email=user_email,
    )

    return {
        **payment,
        "redirect_url": payment.get("redirect_url") or "",
    }


def render_nirvana_payment_text(payment: Dict[str, Any]) -> str:
    receiver = payment.get("receiver") or "не получен"
    bank_name = payment.get("bank_name") or "не указан"
    recipient_name = payment.get("recipient_name") or "не указан"
    amount_crypto = payment.get("amount_crypto")
    rate = payment.get("rate")
    status = payment.get("status") or "CREATED"

    lines = [
        "💳 <b>Оплата через NirvanaPay</b>",
        "",
        f"🏦 Банк: <b>{bank_name}</b>",
        f"👤 Получатель: <b>{recipient_name}</b>",
        f"💰 Реквизиты: <code>{receiver}</code>",
        "",
    ]

    if amount_crypto is not None:
        lines.append(f"₿ Сумма crypto: <b>{amount_crypto}</b>")

    if rate is not None:
        lines.append(f"📈 Курс: <b>{rate}</b>")

    lines.extend(
        [
            f"📌 Статус: <b>{status}</b>",
            "",
            "После оплаты статус будет проверен автоматически.",
        ]
    )

    return "\n".join(lines)