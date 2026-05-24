# -----------------------------------------------------------------------------
# Раздел: Импорты
# -----------------------------------------------------------------------------
import hashlib
import hmac
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import ClientSession

from config.settings import settings, BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_BASE_URL


# -----------------------------------------------------------------------------
# Раздел: Исключения
# -----------------------------------------------------------------------------
class BinanceAPIError(Exception):
    """Ошибка взаимодействия с API Binance."""


class BinanceNotionalTooSmall(BinanceAPIError):
    """Сумма сделки ниже минимально допустимой для символа (MIN_NOTIONAL/NOTIONAL)."""

    def __init__(self, symbol: str, required_min: float, attempted: float) -> None:
        super().__init__(
            f"NOTIONAL_TOO_SMALL: symbol={symbol} required_min={required_min} attempted={attempted}"
        )
        self.symbol = symbol
        self.required_min = float(required_min)
        self.attempted = float(attempted)


# -----------------------------------------------------------------------------
# Раздел: Клиент Binance
# -----------------------------------------------------------------------------
class BinanceClient:
    """Клиент для работы с REST API Binance."""

    def __init__(
        self,
        api_key: str = BINANCE_API_KEY,
        api_secret: str = BINANCE_API_SECRET,
        base_url: str = BINANCE_BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")

    # -------------------------------------------------------------------------
    # Раздел: Вспомогательные методы
    # -------------------------------------------------------------------------
    @property
    def _headers(self) -> Dict[str, str]:
        """Базовые заголовки с API-ключом."""
        return {"X-MBX-APIKEY": self.api_key}

    def _sign_params(self, params: Dict[str, Any]) -> Tuple[str, str]:
        """Подписывает параметры SHA256 HMAC. Возвращает (query, signature)."""
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        return query, signature

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Выполняет HTTP-запрос и возвращает JSON."""
        async with ClientSession() as session:
            async with session.request(
                method,
                url,
                headers=headers,
                proxy=settings.binance_proxy or None,
            ) as resp:
                return await resp.json()

    async def _get_server_time(self) -> int:
        """Возвращает serverTime Binance в миллисекундах."""
        url = f"{self.base_url}/api/v3/time"
        data = await self._request_json("GET", url)
        if "serverTime" not in data:
            raise BinanceAPIError(f"Time error: {data}")
        return int(data["serverTime"])

    # -------------------------------------------------------------------------
    # Раздел: Работа с ценами и балансами
    # -------------------------------------------------------------------------
    async def get_price(self, symbol: str) -> float:
        """Возвращает текущую цену для указанного символа."""
        url = f"{self.base_url}/api/v3/ticker/price?symbol={symbol}"
        data = await self._request_json("GET", url)
        if "price" not in data:
            raise BinanceAPIError(f"Price error: {data}")
        return float(data["price"])

    async def get_balance(self, asset: Optional[str] = None) -> Dict[str, Any]:
        """
        Возвращает объект аккаунта или баланс конкретного актива,
        если указан параметр asset.
        """
        ts = await self._get_server_time()
        params = {"timestamp": ts, "recvWindow": 5000}
        query, sig = self._sign_params(params)
        url = f"{self.base_url}/api/v3/account?{query}&signature={sig}"
        data = await self._request_json("GET", url, headers=self._headers)

        if data.get("code"):
            raise BinanceAPIError(f"Balance error: {data}")

        if asset:
            for e in data.get("balances", []) or []:
                if e.get("asset") == asset:
                    return e
            return {"asset": asset, "free": "0.00000000", "locked": "0.00000000"}

        return data

    async def get_spot_balances(self, only_nonzero: bool = True) -> List[Dict[str, Any]]:
        """
        Возвращает список активов спотового аккаунта:
        [{"asset": "USDT", "free": float, "locked": float, "total": float}, ...]
        """
        ts = await self._get_server_time()
        params = {"timestamp": ts, "recvWindow": 5000}
        query, sig = self._sign_params(params)
        url = f"{self.base_url}/api/v3/account?{query}&signature={sig}"
        data = await self._request_json("GET", url, headers=self._headers)

        if data.get("code"):
            raise BinanceAPIError(f"Balance error: {data}")

        result: List[Dict[str, Any]] = []
        for e in data.get("balances", []) or []:
            asset = (e.get("asset") or "").upper()
            if not asset:
                continue
            try:
                free = float(e.get("free") or 0)
                locked = float(e.get("locked") or 0)
            except (TypeError, ValueError):
                continue
            total = free + locked
            if only_nonzero and total <= 0:
                continue
            result.append({"asset": asset, "free": free, "locked": locked, "total": total})

        result.sort(key=lambda x: x["total"], reverse=True)
        return result

    # -------------------------------------------------------------------------
    # Раздел: Информация о символах и квантование
    # -------------------------------------------------------------------------
    async def _get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """Возвращает exchangeInfo по символу, проверяя статус TRADING."""
        url = f"{self.base_url}/api/v3/exchangeInfo?symbol={symbol}"
        info = await self._request_json("GET", url)
        if info.get("code"):
            raise BinanceAPIError(f"exchangeInfo error: {info}")

        symbols = info.get("symbols") or []
        if not symbols:
            raise BinanceAPIError(f"Symbol not found: {symbol}")

        s = symbols[0]
        if (s.get("status") or "").upper() != "TRADING":
            raise BinanceAPIError(f"Symbol not tradable now: {symbol}")
        return s

    @staticmethod
    def _step_to_decimals(step_size: str) -> int:
        """Возвращает количество знаков после запятой для шага LOT_SIZE."""
        if "." in step_size:
            return len(step_size.rstrip("0").split(".")[1])
        return 0

    async def _quantize_qty(self, symbol: str, qty: float) -> Tuple[float, str, int]:
        """
        Квантование количества по LOT_SIZE.
        Возвращает (qty_float, qty_str, decimals). Если меньше minQty — вернёт 0.
        """
        s = await self._get_symbol_info(symbol)
        lot: Dict[str, Any] = {}
        for f in s.get("filters", []) or []:
            if (f.get("filterType") or "") == "LOT_SIZE":
                lot = f
                break

        step = lot.get("stepSize", "0.00000001")
        min_qty = float(lot.get("minQty", "0") or 0)
        max_qty = float(lot.get("maxQty", "1000000000") or 1e9)
        step_f = float(step) if float(step) > 0 else 1e-8

        k = int(float(qty) / step_f)
        adj = k * step_f

        if adj < min_qty:
            return 0.0, "0", self._step_to_decimals(step)
        if adj > max_qty:
            adj = max_qty

        decimals = self._step_to_decimals(step)
        qty_str = f"{adj:.{decimals}f}"
        return float(qty_str), qty_str, decimals

    async def _check_symbol_tradable(self, symbol: str) -> None:
        """Проверяет доступность символа к торговле."""
        url = f"{self.base_url}/api/v3/exchangeInfo?symbol={symbol}"
        info = await self._request_json("GET", url)
        if info.get("code"):
            raise BinanceAPIError(f"exchangeInfo error: {info}")

        symbols = info.get("symbols") or []
        if not symbols:
            raise BinanceAPIError(f"Symbol not found: {symbol}")

        s = symbols[0]
        if (s.get("status") or "").upper() != "TRADING":
            raise BinanceAPIError(f"Symbol not tradable now: {symbol}")

    # -------------------------------------------------------------------------
    # Раздел: Торговые операции
    # -------------------------------------------------------------------------
    async def _spot_market_buy(
        self,
        symbol: str,
        quote_amount: float,
        safety_bps: int = 20,
    ) -> float:
        """
        Покупка MARKET с quoteOrderQty, уменьшая сумму на safety_bps.
        Возвращает количество купленного базового актива (executedQty).

        Важно: заранее проверяет MIN_NOTIONAL/NOTIONAL, чтобы не ловить
        Filter failure: NOTIONAL и уметь красиво сообщать причину.
        """
        if quote_amount <= 0:
            raise BinanceAPIError("Quote amount <= 0")

        s = await self._get_symbol_info(symbol)

        min_notional: float = 0.0
        for f in s.get("filters", []) or []:
            ft = (f.get("filterType") or "").upper()
            if ft in {"MIN_NOTIONAL", "NOTIONAL"}:
                try:
                    min_notional = float(
                        f.get("minNotional")
                        or f.get("notional")
                        or f.get("min_notional")
                        or 0
                    )
                except Exception:
                    min_notional = 0.0
                break

        amt_dec = (
            Decimal(str(quote_amount))
            * (Decimal("1") - Decimal(safety_bps) / Decimal("10000"))
        ).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

        if amt_dec <= 0:
            raise BinanceAPIError("Quote amount <= 0 after safety margin")

        attempted = float(amt_dec)

        if min_notional > 0 and attempted < min_notional:
            raise BinanceNotionalTooSmall(
                symbol=symbol,
                required_min=min_notional,
                attempted=attempted,
            )

        ts = await self._get_server_time()
        payload = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": str(amt_dec),
            "timestamp": ts,
            "recvWindow": 5000,
        }
        query, sig = self._sign_params(payload)
        url = f"{self.base_url}/api/v3/order?{query}&signature={sig}"
        data = await self._request_json("POST", url, headers=self._headers)

        if data.get("code"):
            msg = (data.get("msg") or "").upper()
            if "NOTIONAL" in msg and min_notional > 0:
                raise BinanceNotionalTooSmall(
                    symbol=symbol,
                    required_min=min_notional,
                    attempted=attempted,
                )
            raise BinanceAPIError(f"Spot market buy error: {data}")

        return float(data.get("executedQty", 0) or 0)

    async def _spot_market_sell(
        self,
        symbol: str,
        base_quantity: float,
        qty_str: Optional[str] = None,
    ) -> Tuple[float, float]:
        """
        Продажа MARKET по символу. Возвращает (base_executed_qty, quote_received).
        Можно передать qty_str с нужной точностью.
        """
        ts = await self._get_server_time()
        quantity = qty_str if qty_str is not None else f"{round(float(base_quantity), 8):.8f}"
        payload = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": quantity,
            "timestamp": ts,
            "recvWindow": 5000,
        }
        query, sig = self._sign_params(payload)
        url = f"{self.base_url}/api/v3/order?{query}&signature={sig}"
        data = await self._request_json("POST", url, headers=self._headers)

        if data.get("code"):
            raise BinanceAPIError(f"Spot market sell error: {data}")

        base_executed = float(data.get("executedQty", 0) or 0)
        quote_got = float(data.get("cummulativeQuoteQty", 0) or 0)
        return base_executed, quote_got

    # -------------------------------------------------------------------------
    # Раздел: Конвертации
    # -------------------------------------------------------------------------
    async def convert_usdt_to_ton(self, usdt_amount: float) -> float:
        """
        Конвертация USDT -> TON через Convert API; при ошибке — fallback на
        MARKET-покупку TONUSDT с safety_bps.
        """
        recv_window = 5000

        try:
            ts1 = await self._get_server_time()
            amt_dec = Decimal(str(usdt_amount)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            amt = float(amt_dec)

            params_q = {
                "fromAsset": "USDT",
                "toAsset": "TON",
                "fromAmount": f"{amt:.8f}",
                "timestamp": ts1,
                "recvWindow": recv_window,
            }
            q, sig_q = self._sign_params(params_q)
            url_q = f"{self.base_url}/sapi/v1/convert/getQuote?{q}&signature={sig_q}"
            dq = await self._request_json("POST", url_q, headers=self._headers)

            quote_id = dq.get("quoteId")
            to_amt = dq.get("toAmount")
            if not quote_id or to_amt is None:
                raise BinanceAPIError(f"Convert quote error: {dq}")

            ts2 = await self._get_server_time()
            params_a = {"quoteId": quote_id, "timestamp": ts2, "recvWindow": recv_window}
            qa, sig_a = self._sign_params(params_a)
            url_a = f"{self.base_url}/sapi/v1/convert/acceptQuote?{qa}&signature={sig_a}"
            da = await self._request_json("POST", url_a, headers=self._headers)

            status = (da.get("orderStatus") or "").upper()
            if status not in {"ACCEPT_SUCCESS", "SUCCESS", "PROCESS"}:
                raise BinanceAPIError(f"Convert accept error: {da}")

            return float(to_amt)

        except BinanceAPIError:
            return await self._spot_market_buy("TONUSDT", float(usdt_amount), safety_bps=20)

    async def convert_any_to_usdt(self, asset: str, amount_asset: float) -> float:
        """
        Продаёт любой актив за USDT через MARKET SELL пары <ASSET>USDT.
        Возвращает фактически полученное количество USDT.
        """
        asset = (asset or "").upper().strip()
        if not asset or amount_asset <= 0:
            return 0.0
        if asset == "USDT":
            return float(amount_asset)

        symbol = f"{asset}USDT"
        s = await self._get_symbol_info(symbol)

        bal = await self.get_balance(asset)
        free = float(bal.get("free", 0) or 0)
        qty_target = min(float(amount_asset), free)
        if qty_target <= 0:
            return 0.0

        price = float(await self.get_price(symbol))
        if price <= 0:
            return 0.0

        qty_adj, qty_str, _ = await self._quantize_qty(symbol, qty_target)
        if qty_adj <= 0:
            min_notional = 0.0
            for f in s.get("filters", []) or []:
                if (f.get("filterType") or "") == "MIN_NOTIONAL":
                    try:
                        min_notional = float(f.get("minNotional") or 0)
                    except Exception:
                        min_notional = 0.0
                    break
            min_qty = (min_notional / price) if min_notional > 0 else 0.0
            if min_qty > 0 and free >= min_qty:
                qty_adj, qty_str, _ = await self._quantize_qty(symbol, min_qty)

        if qty_adj <= 0:
            return 0.0

        _, quote_got = await self._spot_market_sell(symbol, qty_adj, qty_str=qty_str)
        return float(quote_got)

    async def convert_any_to_ton(self, asset: str, amount_asset: float) -> float:
        """
        Универсальный маршрут конвертации: <ASSET> -> USDT -> TON.
        Возвращает фактически купленный объём TON.
        """
        usdt_amount = await self.convert_any_to_usdt(asset, amount_asset)
        if usdt_amount <= 0:
            return 0.0

        ton_got = await self.convert_usdt_to_ton(usdt_amount)
        return float(ton_got)

    async def sell_for_usdt(self, asset: str, max_quote_usdt: float) -> Tuple[float, float]:
        """
        Частичная продажа актива asset за USDT на сумму до max_quote_usdt.
        Возвращает (base_sold, usdt_got).
        """
        asset = (asset or "").upper().strip()
        if not asset or asset in {"USDT", "TON"} or max_quote_usdt <= 0:
            return 0.0, 0.0

        symbol = f"{asset}USDT"
        s = await self._get_symbol_info(symbol)

        bal = await self.get_balance(asset)
        free = float(bal.get("free", 0) or 0)
        if free <= 0:
            return 0.0, 0.0

        price = float(await self.get_price(symbol))
        if price <= 0:
            return 0.0, 0.0

        qty_target = min(free, float(max_quote_usdt) / price)

        qty_adj, qty_str, _ = await self._quantize_qty(symbol, qty_target)
        if qty_adj <= 0:
            min_notional = 0.0
            for f in s.get("filters", []) or []:
                if (f.get("filterType") or "") == "MIN_NOTIONAL":
                    try:
                        min_notional = float(f.get("minNotional") or 0)
                    except Exception:
                        min_notional = 0.0
                    break
            min_qty = (min_notional / price) if min_notional > 0 else 0.0
            if min_qty > 0 and free >= min_qty:
                qty_adj, qty_str, _ = await self._quantize_qty(symbol, min_qty)

        if qty_adj <= 0:
            return 0.0, 0.0

        min_notional = 0.0
        for f in s.get("filters", []) or []:
            if (f.get("filterType") or "") == "MIN_NOTIONAL":
                try:
                    min_notional = float(f.get("minNotional") or 0)
                except Exception:
                    min_notional = 0.0
                break
        if min_notional and (qty_adj * price) < min_notional:
            return 0.0, 0.0

        base_sold, quote_got = await self._spot_market_sell(symbol, qty_adj, qty_str=qty_str)
        return base_sold, quote_got

    # -------------------------------------------------------------------------
    # Раздел: Вывод средств
    # -------------------------------------------------------------------------
    async def withdrawal_ton(
        self,
        amount: float,
        address: str,
        network: str = "TON",
        memo: Optional[str] = None,
        fee: Optional[float] = None,
    ) -> str:
        """
        Создаёт заявку на вывод TON.
        Возвращает идентификатор транзакции вывода.

        Важно:
        - отправляет ровно запрошенную сумму;
        - если свободного баланса TON недостаточно, выбрасывает ошибку;
        - не подменяет сумму вывода текущим балансом.
        """
        amt_dec = Decimal(str(amount)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if amt_dec <= 0:
            raise BinanceAPIError(f"Withdraw error: invalid TON amount {amount}")

        info = await self.get_balance("TON")
        free_dec = Decimal(str(info.get("free", 0) or 0)).quantize(
            Decimal("0.00000001"),
            rounding=ROUND_DOWN,
        )

        if free_dec < amt_dec:
            raise BinanceAPIError(
                f"Withdraw error: insufficient TON balance. required={amt_dec:.8f}, free={free_dec:.8f}"
            )

        amt_str = f"{amt_dec:.8f}"
        ts = await self._get_server_time()
        payload: Dict[str, Any] = {
            "coin": "TON",
            "address": address,
            "amount": amt_str,
            "network": network,
            "timestamp": ts,
            "recvWindow": 5000,
        }

        if memo:
            payload["addressTag"] = memo
        if fee is not None:
            payload["withdrawOrderId"] = str(fee)

        query, sig = self._sign_params(payload)
        url = f"{self.base_url}/sapi/v1/capital/withdraw/apply?{query}&signature={sig}"
        data = await self._request_json("POST", url, headers=self._headers)

        if data.get("code"):
            raise BinanceAPIError(f"Withdraw error: {data}")

        return str(data.get("id"))