from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiohttp


@dataclass
class AkkulaAPIError(Exception):
    message: str
    code: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    http_status: Optional[int] = None

    def __str__(self) -> str:
        base = self.message or "Akkula API error"
        if self.code:
            base = f"{base} ({self.code})"
        return base


class AkkulaClient:
    """
    Async клиент для Akkula Partner API.
    Auth: X-API-Key
    Base URL: https://akkula.kg/api/partner/v1
    """

    def __init__(self, api_key: str, base_url: str, timeout_sec: int = 20):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)

    def _headers(self) -> Dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        _retry_429: int = 1,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"

        async with aiohttp.ClientSession(timeout=self.timeout, headers=self._headers()) as session:
            async with session.request(method, url, params=params, json=json) as resp:
                status = resp.status
                text = await resp.text()

                # Пытаемся распарсить json всегда
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = None

                # Rate limit: 429
                if status == 429 and _retry_429 > 0:
                    retry_after = resp.headers.get("Retry-After")
                    delay = 1.0
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            delay = 1.0
                    await asyncio.sleep(max(0.2, min(delay, 5.0)))
                    return await self._request(
                        method,
                        path,
                        params=params,
                        json=json,
                        _retry_429=_retry_429 - 1,
                    )

                # HTTP ошибки
                if status >= 400:
                    if isinstance(payload, dict):
                        raise AkkulaAPIError(
                            message=payload.get("error") or f"HTTP {status}",
                            code=payload.get("code"),
                            details=payload.get("details"),
                            http_status=status,
                        )
                    raise AkkulaAPIError(
                        message=f"HTTP {status}: {text[:200]}",
                        http_status=status,
                    )

                # success:false при 200 тоже считаем ошибкой
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise AkkulaAPIError(
                        message=payload.get("error") or "Akkula error",
                        code=payload.get("code"),
                        details=payload.get("details"),
                        http_status=status,
                    )

                if isinstance(payload, dict) and "data" in payload:
                    return payload["data"]

                # fallback
                if isinstance(payload, dict):
                    return payload

                raise AkkulaAPIError(message="Unexpected Akkula response format", http_status=status)

    async def get_limits(self, *, amount_rub: Optional[float] = None, network: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if amount_rub is not None:
            params["amount_rub"] = amount_rub
        if network:
            params["network"] = network
        return await self._request("GET", "/limits", params=params)

    async def create_order(
        self,
        *,
        partner_order_id: str,
        amount_rub: float,
        recipient_wallet: str,
        network: Optional[str] = None,
        client_email: Optional[str] = None,
        client_phone: Optional[str] = None,
        client_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "partner_order_id": partner_order_id,
            "amount_rub": amount_rub,
            "recipient_wallet": recipient_wallet,
        }
        if network:
            body["network"] = network
        if client_email:
            body["client_email"] = client_email
        if client_phone:
            body["client_phone"] = client_phone
        if client_name:
            body["client_name"] = client_name
        if metadata:
            body["metadata"] = metadata

        return await self._request("POST", "/orders", json=body)

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/orders/{order_id}")

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/orders/{order_id}/cancel")
