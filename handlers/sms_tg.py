# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, Optional

from aiogram import Dispatcher, types

from config.settings import settings
from db.sms_events import insert_sms_event


# -----------------------------------------------------------------------------
# Раздел: Константы и регулярные выражения
# -----------------------------------------------------------------------------
logger = logging.getLogger("sms_tg")

CHANNEL_ID: int = int(getattr(settings, "telegram_sms_channel_id", 0))

RE_LAST4_STRICT = re.compile(
    r"""(?ix)
    (?:\*{2,}\s*|\b(?:карта|card|visa|mastercard|mc)\b[ :#№\-]*)
    (\d{4})\b
    """
)

RE_TBANK_TOPUP = re.compile(
    r"""
    Пополнение,\s*счет\s*RUB\.
    [^0-9]*                             # произвольный текст до суммы
    (?P<amount>\d{1,3}(?:[ \u00A0]?\d{3})*(?:[.,]\d{1,2})?)\s*RUB
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


# -----------------------------------------------------------------------------
# Раздел: Утилиты парсинга и хеширования
# -----------------------------------------------------------------------------
def _event_hash(payload: Dict[str, Any]) -> str:
    """Детерминированный SHA-256 хеш события."""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_last4_strict(text: str) -> Optional[str]:
    """Извлекает last4 карты только при явном указании (****1234 / карта 1234 и т.п.)."""
    if not text:
        return None
    m = RE_LAST4_STRICT.search(text)
    return m.group(1) if m else None


def _parse_tbank_topup(text: str) -> Optional[Dict[str, Any]]:
    """Парсит входящее пополнение T-Bank по простой эвристике."""

    def _norm(n: str) -> float:
        return float((n or "").replace("\u00A0", " ").replace(" ", "").replace(",", "."))

    if not text:
        return None

    low = text.lower()
    if "t-bank" not in low and "tinkoff" not in low and "тинькофф" not in low and "т-банк" not in low:
        return None

    m = RE_TBANK_TOPUP.search(text)
    if not m:
        return None

    try:
        amount_rub = _norm(m.group("amount"))
        return {
            "amount_rub": amount_rub,
            "bank": "T-Bank",
            "kind": "incoming_topup",
        }
    except Exception:
        logger.exception("T-Bank topup parse failed")
        return None


# -----------------------------------------------------------------------------
# Раздел: Обработка поста из SMS-канала
# -----------------------------------------------------------------------------
async def _process_post(message: types.Message) -> None:
    """
    Обрабатывает пост из канала пересылки SMS.

    ВАЖНО: функционал «Брелок/автообмен» удалён.
    Сейчас этот модуль только:
      1) пытается распарсить пополнение (T-Bank),
      2) сохраняет событие в db.sms_events.
    """
    text = message.text or message.caption or ""
    if not text:
        return

    sender = message.chat.title or "SMSChannel"
    event_id = _event_hash({"chat_id": message.chat.id, "msg_id": message.message_id, "text": text})

    tbank = _parse_tbank_topup(text)
    if not tbank:
        # сохраняем как "не распознано"
        try:
            await insert_sms_event(
                event_hash=event_id,
                sender=sender,
                body=text,
                card_last4=None,
                amount_rub=None,
                user_id=None,
                parsed_ok=False,
            )
        except Exception:
            logger.exception("insert_sms_event (non-matching) failed")
        return

    amount_rub: float = float(tbank["amount_rub"])
    last4 = _parse_last4_strict(text)

    try:
        await insert_sms_event(
            event_hash=event_id,
            sender=sender,
            body=text,
            card_last4=last4,
            amount_rub=amount_rub,
            user_id=None,          # без привязки к пользователю (брелок удалён)
            parsed_ok=True,
        )
    except Exception:
        logger.exception("insert_sms_event failed: event_hash=%s", event_id)


# -----------------------------------------------------------------------------
# Раздел: Хэндлеры и регистрация
# -----------------------------------------------------------------------------
async def on_channel_post(message: types.Message) -> None:
    """Точка входа для постов из канала пересылки SMS."""
    if CHANNEL_ID == 0:
        logger.warning("CHANNEL_ID not configured (settings.telegram_sms_channel_id missing or 0)")
        return

    if message.chat.id != CHANNEL_ID:
        return

    if not (message.text or message.caption):
        return

    await _process_post(message)


def register(dp: Dispatcher) -> None:
    """Регистрирует хендлеры для шлюза SMS-канала."""
    dp.register_channel_post_handler(on_channel_post, content_types=types.ContentTypes.ANY)