# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
import asyncio
import hmac
import hashlib
import json
import time
from typing import Any, Optional
import logging
from datetime import datetime, timezone


from aiohttp import web
from aiogram import Bot

from config.settings import AKKULA_WEBHOOK_HOST, AKKULA_WEBHOOK_PORT, AKKULA_WEBHOOK_SECRET, AKKULA_API_SECRET




# -----------------------------------------------------------------------------
# Раздел: Константы и настройки
# -----------------------------------------------------------------------------
AKKULA_HOST: str = AKKULA_WEBHOOK_HOST or "0.0.0.0"
AKKULA_PORT: int = int(AKKULA_WEBHOOK_PORT or 8082)

# Секрет для проверки webhook подписи (HMAC-SHA256)
AKKULA_WEBHOOK_SECRET: Optional[str] = (AKKULA_WEBHOOK_SECRET or "").strip() or None

ROUTE_PATH: str = "/akkula/webhook"

# Отклоняем запросы старше 5 минут
MAX_SKEW_SEC: int = 300

APP_BOT_KEY = "bot"

log = logging.getLogger("akkula_webhook")


# -----------------------------------------------------------------------------
# Раздел: Вспомогательные функции
# -----------------------------------------------------------------------------
def _json_error(message: str, code: int = 400) -> web.Response:
    """Формирует JSON-ответ об ошибке."""
    return web.json_response({"ok": False, "error": message}, status=code)


def _get_header(request: web.Request, name: str) -> Optional[str]:
    v = request.headers.get(name)
    return v.strip() if isinstance(v, str) else None


def _verify_timestamp(ts_raw: Optional[str]) -> bool:
    """
    Проверяет unix timestamp (секунды), не старше MAX_SKEW_SEC.
    """
    if not ts_raw:
        return False
    try:
        ts = int(ts_raw)
    except ValueError:
        return False
    now = int(time.time())
    return abs(now - ts) <= MAX_SKEW_SEC


def _verify_iso_timestamp(iso_raw: Optional[str]) -> bool:
    """
    Проверяет ISO8601 timestamp из payload (например "2026-01-20T12:30:00Z"),
    не старше MAX_SKEW_SEC.
    """
    if not iso_raw or not isinstance(iso_raw, str):
        return False
    s = iso_raw.strip()
    if not s:
        return False

    try:
        # поддержка "...Z"
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        diff = abs((now - dt).total_seconds())
        return diff <= MAX_SKEW_SEC
    except Exception:
        return False


def _hmac_hex(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


async def _process_akkula_event(app: web.Application, payload: dict, event_type: str) -> None:
    """
    Обработка Akkula webhook событий в фоне.

    Требования:
    - Пользователь НЕ получает никаких статусных сообщений Akkula
    - При completed:
        1) делаем паузу 15 секунд (ждём резерв USDT)
        2) удаляем сообщение с платёжной ссылкой
        3) запускаем обмен по конкретной p2p-заявке (p2p_order_id)
    - Админ-уведомления по этой ветке (Akkula link) получает ТОЛЬКО один админ: 6216500555

    Дополнительно:
    - ЖЁСТКАЯ идемпотентность "не более 1 раза" по p2p_order_id для автозапуска и уведомлений:
      используем db.p2p.try_claim_p2p_action(order_id, action)
    """
    bot: Optional[Bot] = app.get(APP_BOT_KEY)

    AKKULA_NOTIFY_ADMIN_ID = 6216500555

    partner_order_id = payload.get("partner_order_id")
    order_id = payload.get("order_id")
    status = (payload.get("status") or "").strip().lower()
    previous_status = (payload.get("previous_status") or "").strip().lower() if payload.get("previous_status") else None

    data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tx_hash = payload.get("tx_hash") or (data_obj.get("tx_hash") if isinstance(data_obj, dict) else None)

    if not partner_order_id or not status:
        return

    try:
        log.info(
            "Akkula event processed: event=%s partner_order_id=%s status=%s prev=%s order_id=%s",
            event_type, partner_order_id, status, previous_status, order_id
        )
    except Exception:
        pass

    # 1) Обновляем статус в БД и вытаскиваем запись akkula_orders
    rec = None
    try:
        from db.akkula_orders import update_akkula_order_status, get_akkula_order_by_partner_id

        await update_akkula_order_status(
            partner_order_id=str(partner_order_id),
            status=str(status) if status else "unknown",
            order_id=str(order_id) if order_id else None,
            tx_hash=str(tx_hash) if tx_hash else None,
        )

        rec = await get_akkula_order_by_partner_id(str(partner_order_id))
    except Exception:
        rec = None

    if not rec:
        log.warning("Akkula event ignored: partner_order_id not found in DB: %s", partner_order_id)
        return

    tg_user_id = rec.get("tg_user_id")
    if not tg_user_id:
        return

    # 2) На любых статусах кроме completed — молчим (никаких сообщений пользователю)
    if status != "completed":
        return

    # 3) completed — идемпотентность на уровне akkula_orders (защита от дублей по partner_order_id)
    ok_first = True
    try:
        from db.akkula_orders import try_mark_akkula_completed_processed
        ok_first = await try_mark_akkula_completed_processed(str(partner_order_id))
    except Exception:
        ok_first = True

    if not ok_first:
        return

    if not bot:
        return

    # 4) p2p_order_id берем строго из akkula_orders
    p2p = None
    p2p_order_id = rec.get("p2p_order_id")

    try:
        log.info("Akkula completed matched DB: tg_user_id=%s p2p_order_id=%s", tg_user_id, p2p_order_id)
    except Exception:
        pass

    # Если p2p_order_id отсутствует — дальше нет "ключа заявки", не рискуем делать автозапуск
    if not p2p_order_id:
        # Тут можно было бы уведомить админа, но без order_id мы не можем гарантировать "не более 1 раза".
        # Поэтому просто выходим молча.
        return

    # ------------------------------------------------------------
    # ✅ ГЛАВНЫЙ ЗАМОК: автозапуск обмена по этой p2p-заявке разрешён только 1 раз
    # ------------------------------------------------------------
    try:
        from db.p2p import try_claim_p2p_action
        can_autostart = await try_claim_p2p_action(int(p2p_order_id), "akkula_autostart_exchange")
    except Exception:
        # Если БД-замок недоступен, безопаснее НЕ запускать обмен повторно.
        return

    if not can_autostart:
        return

    # 5) Загружаем p2p-заявку
    try:
        from db.p2p import get_order_by_id
        p2p = await get_order_by_id(int(p2p_order_id))
    except Exception:
        p2p = None

    # ❗ ВАЖНО: НЕ ДЕЛАЕМ fallback на get_pending_order(user) — при мультиссылках это может быть другая заявка.

    if not p2p:
        # Админ-уведомление: тоже "не более 1 раза" по этой заявке (уже держим общий замок autostart_exchange)
        try:
            await bot.send_message(
                AKKULA_NOTIFY_ADMIN_ID,
                (
                    "⚠️ <b>Akkula completed, но не найдена p2p-заявка</b>\n\n"
                    f"partner_order_id: <code>{partner_order_id}</code>\n"
                    f"user_id: <code>{tg_user_id}</code>\n"
                    f"p2p_order_id: <code>{p2p_order_id or '—'}</code>\n"
                    f"order_id: <code>{order_id or '—'}</code>\n"
                    f"tx_hash: <code>{tx_hash or '—'}</code>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass

        # Зафиксируем, что автозапуск по заявке фактически "провалился"
        try:
            from db.p2p import mark_p2p_action_failed
            await mark_p2p_action_failed(int(p2p_order_id), "akkula_autostart_exchange", error="p2p order not found")
        except Exception:
            pass
        return

    # 6) Проверяем статус заявки
    try:
        st = str(p2p.get("status") or "").lower().strip()
    except Exception:
        st = ""

    if st and st != "pending":
        # Админ-уведомление: "не более 1 раза" по заявке обеспечено тем же замком autostart_exchange
        try:
            await bot.send_message(
                AKKULA_NOTIFY_ADMIN_ID,
                (
                    "⚠️ <b>Akkula completed, но заявка не pending</b>\n\n"
                    f"partner_order_id: <code>{partner_order_id}</code>\n"
                    f"user_id: <code>{tg_user_id}</code>\n"
                    f"p2p_order_id: <code>{p2p.get('order_id') or p2p_order_id or '—'}</code>\n"
                    f"p2p_status: <code>{st or '—'}</code>\n"
                    f"order_id: <code>{order_id or '—'}</code>\n"
                    f"tx_hash: <code>{tx_hash or '—'}</code>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass

        try:
            from db.p2p import mark_p2p_action_failed
            await mark_p2p_action_failed(int(p2p_order_id), "akkula_autostart_exchange", error=f"p2p status={st}")
        except Exception:
            pass
        return

    # 7) Пауза после подтверждения оплаты (ждём резерв USDT)
    try:
        await asyncio.sleep(25)
    except Exception:
        pass

    # 8) Удаляем сообщение со ссылкой (тоже можно сделать одноразовым действием)
    msg_id = rec.get("link_message_id")
    if msg_id:
        try:
            from db.p2p import try_claim_p2p_action, mark_p2p_action_sent
            if await try_claim_p2p_action(int(p2p_order_id), "akkula_delete_link_message"):
                try:
                    await bot.delete_message(chat_id=int(tg_user_id), message_id=int(msg_id))
                except Exception:
                    pass
                try:
                    await mark_p2p_action_sent(int(p2p_order_id), "akkula_delete_link_message", message_id=int(msg_id))
                except Exception:
                    pass
        except Exception:
            pass

    # 9) Запускаем обмен по конкретной заявке (ТОЛЬКО 1 раз гарантирован замком выше)
    try:
        from handlers.chat.instruction import start_exchange_from_p2p
        await start_exchange_from_p2p(bot=bot, p2p=p2p, operator_id=None)

        # фиксируем успех автозапуска
        try:
            from db.p2p import mark_p2p_action_sent
            await mark_p2p_action_sent(int(p2p_order_id), "akkula_autostart_exchange")
        except Exception:
            pass

    except Exception as e:
        try:
            await bot.send_message(
                AKKULA_NOTIFY_ADMIN_ID,
                (
                    "❌ <b>Ошибка автозапуска обмена по Akkula</b>\n\n"
                    f"partner_order_id: <code>{partner_order_id}</code>\n"
                    f"user_id: <code>{tg_user_id}</code>\n"
                    f"p2p_order_id: <code>{p2p.get('order_id') or p2p_order_id or '—'}</code>\n"
                    f"order_id: <code>{order_id or '—'}</code>\n"
                    f"tx_hash: <code>{tx_hash or '—'}</code>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass

        try:
            from db.p2p import mark_p2p_action_failed
            await mark_p2p_action_failed(int(p2p_order_id), "akkula_autostart_exchange", error=str(e)[:500])
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Раздел: HTTP-хэндлеры
# -----------------------------------------------------------------------------
async def handle_akkula_webhook(request: web.Request) -> web.Response:
    """
    Принимает Akkula webhook.

    Проверки:
    - Наличие webhook_secret / api_secret
    - Timestamp: X-Webhook-Timestamp (unix) ИЛИ payload["timestamp"] (ISO8601), не старше 5 минут
    - X-Webhook-Signature: hex HMAC-SHA256 от СЫРОГО тела запроса
    """
    webhook_secret = (AKKULA_WEBHOOK_SECRET or "").strip()
    api_secret = (AKKULA_API_SECRET or "").strip()

    if not webhook_secret and not api_secret:
        log.warning("Webhook rejected: missing secrets (AKKULA_WEBHOOK_SECRET and AKKULA_API_SECRET are empty)")
        return _json_error("Server not configured: missing webhook secret", 500)

    # ЧИТАЕМ BODY РОВНО ОДИН РАЗ
    body_bytes = await request.read()

    # Timestamp: сначала из заголовка, иначе пробуем из payload.timestamp (ISO8601)
    ts_hdr = _get_header(request, "X-Webhook-Timestamp")

    payload_for_ts = None
    iso_ts_ok = False
    if not ts_hdr:
        try:
            payload_for_ts = json.loads(body_bytes.decode("utf-8"))
            if isinstance(payload_for_ts, dict):
                iso_ts_ok = _verify_iso_timestamp(payload_for_ts.get("timestamp"))
        except Exception:
            iso_ts_ok = False

    if ts_hdr:
        ts_ok = _verify_timestamp(ts_hdr)
    else:
        ts_ok = iso_ts_ok

    if not ts_ok:
        log.warning(
            "Webhook rejected: invalid/expired timestamp header=%r payload_ts=%r",
            ts_hdr,
            (payload_for_ts or {}).get("timestamp") if isinstance(payload_for_ts, dict) else None,
        )
        return _json_error("Invalid or expired timestamp", 401)

    # Signature
    sig_hdr = _get_header(request, "X-Webhook-Signature")
    if not sig_hdr:
        log.warning("Webhook rejected: missing X-Webhook-Signature")
        return _json_error("Missing signature", 401)

    # ВАЖНО: Akkula могла выдать два секрета. На этапе отладки принимаем подпись,
    # если она совпала хотя бы с одним. Потом сузим до правильного.
    expected_candidates = []
    if webhook_secret:
        expected_candidates.append(_hmac_hex(body_bytes, webhook_secret))
    if api_secret and api_secret != webhook_secret:
        expected_candidates.append(_hmac_hex(body_bytes, api_secret))

    sig_l = sig_hdr.lower()
    ok_sig = any(hmac.compare_digest(sig_l, exp.lower()) for exp in expected_candidates)

    if not ok_sig:
        log.warning(
            "Webhook rejected: invalid signature. ts=%r sig_prefix=%s body_len=%d",
            ts_hdr,
            sig_l[:12],
            len(body_bytes),
        )
        return _json_error("Invalid signature", 401)

    # Парсим JSON (можем переиспользовать payload_for_ts если он уже dict)
    payload = payload_for_ts
    if not isinstance(payload, dict):
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            log.warning("Webhook rejected: invalid JSON payload (len=%d)", len(body_bytes))
            return _json_error("Invalid JSON payload", 400)

    if not isinstance(payload, dict):
        log.warning("Webhook rejected: payload is not dict")
        return _json_error("Invalid payload format", 400)

    # Определяем event_type
    event_type = payload.get("event") or _get_header(request, "X-Webhook-Event") or "unknown"

    # --- ДОБАВЛЕННАЯ ДИАГНОСТИКА ---
    try:
        log.info("Akkula webhook headers: ts=%r sig_prefix=%s event_hdr=%r",
                 ts_hdr, (sig_l[:12] if sig_hdr else None), _get_header(request, "X-Webhook-Event"))
    except Exception:
        pass

    try:
        log.info("Akkula payload keys=%s", list(payload.keys()))
    except Exception:
        pass

    try:
        # ограничиваем размер, чтобы не заспамить лог
        payload_preview = json.dumps(payload, ensure_ascii=False)
        if len(payload_preview) > 2000:
            payload_preview = payload_preview[:2000] + "...<cut>"
        log.info("Akkula payload=%s", payload_preview)
    except Exception:
        pass
    # --- КОНЕЦ ДИАГНОСТИКИ ---

    log.info("Webhook accepted: event=%s", event_type)

    try:
        asyncio.create_task(_process_akkula_event(request.app, payload, str(event_type)))
    except Exception:
        pass

    return web.json_response({"ok": True})


# -----------------------------------------------------------------------------
# Раздел: Инициализация и управление сервером
# -----------------------------------------------------------------------------
def build_app(*, bot: Bot) -> web.Application:
    """Создаёт aiohttp-приложение и регистрирует маршруты."""
    app = web.Application()
    app[APP_BOT_KEY] = bot
    app.router.add_post(ROUTE_PATH, handle_akkula_webhook)
    return app


async def start_akkula_webhook_server(
    *,
    bot: Bot,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> web.AppRunner:
    """Запускает aiohttp-сервер и возвращает AppRunner."""
    host = host or AKKULA_HOST
    port = port or AKKULA_PORT
    app = build_app(bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    print(f"[AKKULA] webhook server started at http://{host}:{port}{ROUTE_PATH}")
    return runner


async def stop_akkula_webhook_server(runner: Optional[web.AppRunner]) -> None:
    """Безопасно останавливает сервер; спокойно принимает None."""
    if runner is None:
        return
    try:
        await runner.cleanup()
    except Exception:
        pass
