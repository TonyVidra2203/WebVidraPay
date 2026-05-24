# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------

import asyncio
import hashlib
import hmac
import json
from typing import Any, Dict, Tuple

from aiohttp import ClientSession, ClientTimeout, ClientError

from config.settings import FF_API_KEY, FF_API_SECRET


# -----------------------------------------------------------------------------
# Раздел: Константы
# -----------------------------------------------------------------------------

BASE_URL: str = "https://ff.io/api/v2"
HTTP_TIMEOUT: ClientTimeout = ClientTimeout(total=30)

HEADERS_TEMPLATE: Dict[str, str] = {
    "X-API-KEY": FF_API_KEY,
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json",
}


# -----------------------------------------------------------------------------
# Раздел: Исключения
# -----------------------------------------------------------------------------

class FFAPIError(Exception):
    """Ошибка взаимодействия с API FixedFloat."""


# -----------------------------------------------------------------------------
# Раздел: Вспомогательные функции (подпись и HTTP-запросы)
# -----------------------------------------------------------------------------

def _sign_payload(payload_json: str) -> str:
    """
    Подписать тело запроса по схеме HMAC-SHA256.
    """
    return hmac.new(
        FF_API_SECRET.encode(),
        payload_json.encode(),
        hashlib.sha256,
    ).hexdigest()


async def _post_api(
    endpoint: str,
    payload: Dict[str, Any],
    *,
    timeout: ClientTimeout = HTTP_TIMEOUT,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> Dict[str, Any]:
    """
    Выполнить POST-запрос к API FixedFloat и вернуть поле 'data'.

    Любые сетевые ошибки/таймауты оборачиваются в FFAPIError, чтобы их
    можно было корректно обработать в хэндлерах.

    Добавлено:
    - несколько попыток при временных сетевых проблемах/таймаутах;
    - увеличенный базовый таймаут.
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
                        raise FFAPIError(
                            f"Invalid JSON response (status={resp.status}): {text}"
                        ) from exc

                    code = resp_json.get("code")
                    if resp.status != 200 or code != 0:
                        msg = resp_json.get("msg") or resp_json.get("message") or text
                        raise FFAPIError(f"{endpoint} error: {msg}")

                    data = resp_json.get("data")
                    if not isinstance(data, dict):
                        raise FFAPIError(
                            f"{endpoint} invalid 'data' payload: {data!r}"
                        )
                    return data

        except asyncio.TimeoutError as exc:
            last_error = exc
            if attempt >= retries:
                raise FFAPIError(f"{endpoint} request timeout") from exc
            await asyncio.sleep(retry_delay)

        except ClientError as exc:
            last_error = exc
            if attempt >= retries:
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

    ВНИМАНИЕ:
    - Параметр order_type сейчас игнорируется и ВСЕГДА отправляется type="fixed".
      Это сделано, чтобы гарантировать фиксированный курс для всех ордеров.
    """
    payload = {
        "fromCcy": from_ccy,
        "toCcy": to_ccy,
        "amount": amount,
        "direction": direction,
        # FF-ордеры теперь всегда создаются как фиксированные
        "type": "fixed",
        "toAddress": to_address,
    }
    return await _post_api("create", payload)


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
    - При direction="to" параметр amount — это сумма в toCcy (то есть "к выдаче").
    - Endpoint возвращает min/max и/или errors с кодами LIMIT_MIN / LIMIT_MAX.
    """
    payload = {
        "fromCcy": from_ccy,
        "toCcy": to_ccy,
        "amount": amount,
        "direction": direction,
        "type": order_type or "fixed",
    }
    return await _post_api("price", payload)


async def get_order_details(order_id: str, token: str) -> Dict[str, Any]:
    """
    Получить подробности ордера по id и token.
    """
    return await _post_api("order", {"id": order_id, "token": token})


# -----------------------------------------------------------------------------
# Раздел: Проверка доступности кошельков FF
# -----------------------------------------------------------------------------

async def check_wallets_status() -> Tuple[bool, bool]:
    """
    Проверить доступность кошельков BTC и TON.

    Возвращает кортеж (btc_ok, ton_ok). Если ручка недоступна или формат
    ответа неожиданен — применяется fail-open (считаем, что OK).
    """
    try:
        # В официальной документации метод называется ccies,
        # но здесь оставляем "currencies" для совместимости с текущим кодом.
        data = await _post_api("currencies", {})
    except FFAPIError:
        return True, True
    except Exception:
        return True, True

    def _ok(d: Dict[str, Any]) -> bool:
        flags = []
        for k in ("enabled", "canSend", "sendEnabled", "available"):
            v = d.get(k)
            if isinstance(v, bool):
                flags.append(v)
        return all(flags) if flags else True

    btc_ok = _ok(data.get("BTC", {}))
    ton_ok = _ok(data.get("TON", {}))
    return btc_ok, ton_ok
