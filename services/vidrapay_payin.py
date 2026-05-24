from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlsplit, urlunsplit

from config.settings import settings


VIDRAPAY_TOKEN_PREFIX = "vp"


def _secret_key() -> str:
    return str(getattr(settings, "bot_token", "") or "").strip()


def build_vidrapay_token(*, p2p_order_id: int, tg_user_id: int) -> str:
    order_id = int(p2p_order_id)
    user_id = int(tg_user_id)

    payload = f"{order_id}:{user_id}"
    secret = _secret_key()

    if not secret:
        raise RuntimeError("BOT_TOKEN не найден для подписи VidraPay-ссылки")

    signature = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24]

    return f"{VIDRAPAY_TOKEN_PREFIX}_{order_id}_{user_id}_{signature}"


def parse_vidrapay_token(token: str) -> tuple[int, int]:
    raw = str(token or "").strip()
    parts = raw.split("_")

    if len(parts) != 4 or parts[0] != VIDRAPAY_TOKEN_PREFIX:
        raise ValueError("Некорректная VidraPay-ссылка")

    order_id = int(parts[1])
    user_id = int(parts[2])
    signature = parts[3]

    expected = build_vidrapay_token(
        p2p_order_id=order_id,
        tg_user_id=user_id,
    ).split("_")[-1]

    if not hmac.compare_digest(signature, expected):
        raise ValueError("Некорректная подпись VidraPay-ссылки")

    return order_id, user_id


def public_pay_base_url() -> str:
    raw = str(getattr(settings, "nirvana_callback_url", "") or "").strip()
    if not raw:
        return ""

    parsed = urlsplit(raw)
    path = parsed.path or ""

    for suffix in (
        "/nirvana/callback",
        "/vidrapay/callback",
    ):
        if path.endswith(suffix):
            path = path[: -len(suffix)]

    path = path.rstrip("/")

    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            path,
            "",
            "",
        )
    ).rstrip("/")


def build_vidrapay_pay_url(*, p2p_order_id: int, tg_user_id: int) -> str:
    base_url = public_pay_base_url()
    if not base_url:
        raise RuntimeError("Публичный URL оплаты не задан")

    token = build_vidrapay_token(
        p2p_order_id=int(p2p_order_id),
        tg_user_id=int(tg_user_id),
    )

    return f"{base_url}/vidrapay/pay/{token}"