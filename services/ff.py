# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Tuple

from aiohttp import ClientSession, ClientTimeout, ClientError

from config.settings import FF_API_KEY, FF_API_SECRET


# -----------------------------------------------------------------------------
# Раздел: Константы
# -----------------------------------------------------------------------------

BASE_URL: str = "https://ff.io/api/v2"
HTTP_TIMEOUT: ClientTimeout = ClientTimeout(total=30)

# Важно:
# FixedFloat изменил API-код TON.
# В /api/v2/ccies сейчас приходит:
# code=GRAMTON, coin=GRAM, name=Gram, network=TON.
# Поэтому в запросы /price и /create старые обозначения TON/GRAM отправляем как GRAMTON.
FF_TON_API_CODE: str = "GRAMTON"
FF_TON_ALIASES = {"TON", "GRAM", "TONCOIN", "GRAMTON"}

HEADERS_TEMPLATE: Dict[str, str] = {
    "X-API-KEY": FF_API_KEY,
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json",
}

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Раздел: Исключения
# -----------------------------------------------------------------------------

class FFAPIError(Exception):
    """Ошибка взаимодействия с API FixedFloat."""


# -----------------------------------------------------------------------------
# Раздел: Вспомогательные функции
# -----------------------------------------------------------------------------

def _normalize_ccy(ccy: Any) -> str:
    """
    Нормализовать код валюты перед отправкой в FixedFloat.

    Старый код TON больше не подходит для FF API.
    Актуальный код из /api/v2/ccies: GRAMTON.
    """
    value = str(ccy or "").strip().upper()

    if value in FF_TON_ALIASES:
        return FF_TON_API_CODE

    return value


def _sign_payload(payload_json: str) -> str:
    """
    Подписать тело запроса по схеме HMAC-SHA256.
    """
    return hmac.new(
        FF_API_SECRET.encode(),
        payload_json.encode(),
        hashlib.sha256,
    ).hexdigest()


def _safe_payload_for_log(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Подготовить payload для логов без полного раскрытия кошелька получателя.
    """
    safe = dict(payload)

    if safe.get("toAddress"):
        address = str(safe["toAddress"])
        if len(address) > 12:
            safe["toAddress"] = f"{address[:6]}...{address[-6:]}"
        else:
            safe["toAddress"] = "***"

    return safe


async def _post_api(
    endpoint: str,
    payload: Dict[str, Any],
    *,
    timeout: ClientTimeout = HTTP_TIMEOUT,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> Any:
    """
    Выполнить POST-запрос к API FixedFloat и вернуть поле 'data'.

    Особенности:
    - сетевые ошибки и таймауты оборачиваются в FFAPIError;
    - при временных сетевых сбоях выполняются повторы;
    - поддерживается data как dict и как list, потому что /ccies возвращает список;
    - при ошибке пишем подробный лог без секретов.
    """
    url = f"{BASE_URL}/{endpoint}"
    payload_json = json.dumps(payload, separators=(",", ":"))

    headers = dict(HEADERS_TEMPLATE)
    headers["X-API-SIGN"] = _sign_payload(payload_json)

    last_error = None

    for attempt in range(1, retries + 1):
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.post(url, data=payload_json, headers=headers) as resp:
                    text = await resp.text()

                    try:
                        resp_json: Dict[str, Any] = json.loads(text)
                    except Exception as exc:
                        logger.error(
                            "FF invalid JSON: endpoint=%s status=%s payload=%s response=%s",
                            endpoint,
                            resp.status,
                            _safe_payload_for_log(payload),
                            text,
                        )
                        raise FFAPIError(
                            f"{endpoint} invalid JSON response (status={resp.status}): {text}"
                        ) from exc

                    code = resp_json.get("code")
                    if resp.status != 200 or code != 0:
                        msg = resp_json.get("msg") or resp_json.get("message") or text
                        logger.error(
                            "FF API error: endpoint=%s status=%s code=%s msg=%s payload=%s response=%s",
                            endpoint,
                            resp.status,
                            code,
                            msg,
                            _safe_payload_for_log(payload),
                            text,
                        )
                        raise FFAPIError(f"{endpoint} error: {msg}")

                    return resp_json.get("data")

        except asyncio.TimeoutError as exc:
            last_error = exc
            if attempt >= retries:
                logger.error(
                    "FF timeout: endpoint=%s payload=%s",
                    endpoint,
                    _safe_payload_for_log(payload),
                )
                raise FFAPIError(f"{endpoint} request timeout") from exc
            await asyncio.sleep(retry_delay)

        except ClientError as exc:
            last_error = exc
            if attempt >= retries:
                logger.error(
                    "FF network error: endpoint=%s payload=%s error=%s",
                    endpoint,
                    _safe_payload_for_log(payload),
                    exc,
                )
                raise FFAPIError(f"{endpoint} network error: {exc}") from exc
            await asyncio.sleep(retry_delay)

        except asyncio.CancelledError:
            raise

    raise FFAPIError(f"{endpoint} unknown error: {last_error!r}")


# -----------------------------------------------------------------------------
# Раздел: Основные функции API
# -----------------------------------------------------------------------------

async def create_order(
    from_ccy: str,
    to_ccy: str,
    amount: float,
    direction: str,
    order_type: str,
    to_address: str,
) -> Dict[str, Any]:
    """
    Создать ордер FixedFloat.

    Важно:
    - старые обозначения TON/GRAM автоматически отправляются как GRAMTON;
    - order_type сейчас игнорируется и всегда отправляется type="fixed",
      чтобы гарантировать фиксированный курс.
    """
    payload = {
        "fromCcy": _normalize_ccy(from_ccy),
        "toCcy": _normalize_ccy(to_ccy),
        "amount": amount,
        "direction": direction,
        "type": "fixed",
        "toAddress": str(to_address or "").strip(),
    }

    data = await _post_api("create", payload)

    if not isinstance(data, dict):
        raise FFAPIError(f"create invalid 'data' payload: {data!r}")

    return data


async def get_price(
    from_ccy: str,
    to_ccy: str,
    amount: float,
    direction: str,
    order_type: str = "fixed",
) -> Dict[str, Any]:
    """
    Получить расчёт/лимиты через /price.

    Важно:
    - старые обозначения TON/GRAM автоматически отправляются как GRAMTON;
    - при direction="to" параметр amount — это сумма в toCcy, то есть "к выдаче".
    """
    payload = {
        "fromCcy": _normalize_ccy(from_ccy),
        "toCcy": _normalize_ccy(to_ccy),
        "amount": amount,
        "direction": direction,
        "type": order_type or "fixed",
    }

    data = await _post_api("price", payload)

    if not isinstance(data, dict):
        raise FFAPIError(f"price invalid 'data' payload: {data!r}")

    return data


async def get_order_details(order_id: str, token: str) -> Dict[str, Any]:
    """
    Получить подробности ордера по id и token.
    """
    data = await _post_api("order", {"id": order_id, "token": token})

    if not isinstance(data, dict):
        raise FFAPIError(f"order invalid 'data' payload: {data!r}")

    return data


# -----------------------------------------------------------------------------
# Раздел: Проверка доступности кошельков FF
# -----------------------------------------------------------------------------

async def check_wallets_status() -> Tuple[bool, bool]:
    """
    Проверить доступность кошельков BTC и TON/GRAMTON.

    Возвращает кортеж (btc_ok, ton_ok).

    Если ручка недоступна или формат ответа неожиданный — применяется fail-open:
    считаем, что всё OK, чтобы не выключить обмен из-за временного сбоя проверки.
    """
    try:
        data = await _post_api("ccies", {})
    except FFAPIError as exc:
        logger.warning("FF ccies check failed: %s", exc)
        return True, True
    except Exception as exc:
        logger.warning("Unexpected FF ccies check error: %s", exc)
        return True, True

    if not isinstance(data, list):
        logger.warning("FF ccies unexpected data format: %r", data)
        return True, True

    by_code: Dict[str, Dict[str, Any]] = {}
    by_coin: Dict[str, Dict[str, Any]] = {}
    by_network: Dict[str, Dict[str, Any]] = {}

    for item in data:
        if not isinstance(item, dict):
            continue

        code = str(item.get("code") or "").strip().upper()
        coin = str(item.get("coin") or "").strip().upper()
        network = str(item.get("network") or "").strip().upper()

        if code:
            by_code[code] = item
        if coin:
            by_coin[coin] = item
        if network:
            by_network[network] = item

    def _flag_ok(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value

        if isinstance(value, int):
            return bool(value)

        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"1", "true", "yes", "y", "on", "enabled"}:
                return True
            if v in {"0", "false", "no", "n", "off", "disabled"}:
                return False

        return None

    def _ok(item: Dict[str, Any]) -> bool:
        """
        FF /ccies обычно отдаёт recv/send.
        Для нашего сценария важны оба:
        - BTC должен быть доступен как toCcy/send;
        - GRAMTON должен быть доступен как fromCcy/recv.
        Но если формат изменился, не блокируем обмен жёстко.
        """
        if not item:
            return True

        flags = []

        for key in ("recv", "send", "enabled", "canSend", "sendEnabled", "available"):
            parsed = _flag_ok(item.get(key))
            if parsed is not None:
                flags.append(parsed)

        return all(flags) if flags else True

    btc_item = by_code.get("BTC") or by_coin.get("BTC") or {}
    ton_item = (
        by_code.get(FF_TON_API_CODE)
        or by_code.get("TON")
        or by_code.get("GRAM")
        or by_coin.get("TON")
        or by_coin.get("GRAM")
        or by_network.get("TON")
        or {}
    )

    btc_ok = _ok(btc_item)
    ton_ok = _ok(ton_item)

    return btc_ok, ton_ok