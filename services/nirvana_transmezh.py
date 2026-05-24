from __future__ import annotations

import html
from typing import Any, Dict, Optional
from urllib.parse import quote

from config.settings import NIRVANA_CALLBACK_URL
from services.nirvana_payin import create_nirvana_payin_for_p2p_order


NIRVANA_TRANSMEZH_TOKEN = "ТрансМежбанк"
NIRVANA_TRANSMEZH_CURRENCY = "RUB"


def build_nirvana_pay_url(client_id: str) -> str:
    """
    Страница оплаты на нашем сервере.

    Если callback:
        https://api.akkulavidra.ru/nirvana/callback

    То страница оплаты:
        https://api.akkulavidra.ru/nirvana/pay/<client_id>
    """
    callback_url = str(NIRVANA_CALLBACK_URL or "").strip()

    if callback_url.endswith("/callback"):
        base_url = callback_url[: -len("/callback")]
    else:
        base_url = callback_url.rstrip("/")

    return f"{base_url}/pay/{quote(str(client_id))}"


def build_qr_url(target_url: str) -> str:
    """
    QR-картинка для Telegram-кнопки.

    Важно:
    Это QR не прямого банковского платежа, а QR страницы оплаты.
    На странице оплаты будут реквизиты и кнопки открытия банков.
    """
    safe_url = str(target_url or "").strip()
    return f"https://api.qrserver.com/v1/create-qr-code/?size=360x360&data={quote(safe_url)}"


def build_bank_open_links() -> Dict[str, str]:
    """
    Ссылки для открытия банков.

    Важно:
    Универсального способа автоматически подставить номер карты/сумму
    во все банки нет, если Nirvana отдаёт только receiver.
    Эти ссылки нужны, чтобы пользователь быстрее открыл банк.
    Реквизиты он сможет скопировать на странице оплаты.
    """
    return {
        "Сбербанк": "sberbankonline://",
        "Т-Банк": "tinkoffbank://",
        "ВТБ": "vtb24://",
        "Альфа-Банк": "alfabank://",
    }


def render_nirvana_transmezh_payment_text(
    *,
    asset: str,
    crypto_amount: float,
    rub_amount: int,
    wallet: str,
    payment: Dict[str, Any],
) -> str:
    sep = "➖" * 10

    bank_name = str(payment.get("bank_name") or "не указан").strip()
    recipient_name = str(payment.get("recipient_name") or "не указан").strip()
    receiver = str(payment.get("receiver") or "не получены").strip()
    client_id = str(payment.get("client_id") or "").strip()

    pay_url = build_nirvana_pay_url(client_id)

    lines = [
        "📥 <b>Данные платежа</b>",
        sep,
        f"▶ Монета: <b>{html.escape(str(asset))}</b>",
        f"▶ К выдаче: <b>{float(crypto_amount):.8f} {html.escape(str(asset))}</b>",
        f"▶ Сумма оплаты: <b>{int(rub_amount)} RUB</b>",
        sep,
        "💳 <b>Оплата через банк</b>",
        f"🏦 Банк: <b>{html.escape(bank_name)}</b>",
        f"👤 Получатель: <b>{html.escape(recipient_name)}</b>",
        f"💰 Реквизиты: <code>{html.escape(receiver)}</code>",
        sep,
        "📌 Нажмите <b>✅ Оплатить</b>, чтобы открыть удобную страницу оплаты.",
        "📲 Кнопка <b>QR</b> откроет QR-код этой же страницы.",
        "",
        "⏱ <b>Время на оплату: 10 минут</b>",
        "",
        "🤖 После поступления средств я запущу обмен автоматически.",
        "",
        f"ID платежа: <code>{html.escape(client_id)}</code>",
        f"Страница оплаты: {html.escape(pay_url)}",
    ]

    if wallet:
        lines.insert(5, f"▶ Кошелёк: <code>{html.escape(str(wallet))}</code>")

    return "\n".join(lines)


async def create_transmezh_payment_for_order(
    *,
    p2p_order_id: int,
    tg_user_id: int,
    amount: int,
) -> Dict[str, Any]:
    """
    Создаёт Nirvana Pay-In только через ТрансМежбанк.
    """
    payment = await create_nirvana_payin_for_p2p_order(
        p2p_order_id=int(p2p_order_id),
        tg_user_id=int(tg_user_id),
        amount=int(amount),
        token=NIRVANA_TRANSMEZH_TOKEN,
        currency=NIRVANA_TRANSMEZH_CURRENCY,
    )

    client_id = str(payment.get("client_id") or "").strip()
    pay_url = build_nirvana_pay_url(client_id)

    payment["pay_url"] = pay_url
    payment["qr_url"] = build_qr_url(pay_url)
    payment["bank_links"] = build_bank_open_links()

    return payment