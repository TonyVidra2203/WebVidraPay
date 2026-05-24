from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp


@dataclass
class NirvanaAPIError(Exception):
    message: str
    http_status: Optional[int] = None
    response: Optional[Dict[str, Any]] = None
    raw_response: Optional[str] = None

    def __str__(self) -> str:
        base = self.message or "Nirvana API error"
        if self.http_status is not None:
            base = f"{base} (HTTP {self.http_status})"
        return base


class NirvanaClient:
    """
    Async client for NirvanaPay API.
    Supports:
    - old API: /create/in, /create/out, /transaction/status
    - v2 API: /api/v2/order
    """

    def __init__(
        self,
        *,
        api_public: str,
        api_private: str,
        base_url: str = "https://api.nirvanapay.pro",
        timeout_sec: int = 20,
    ) -> None:
        self.api_public = str(api_public or "").strip()
        self.api_private = str(api_private or "").strip()
        self.base_url = str(base_url or "https://api.nirvanapay.pro").rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=int(timeout_sec or 20))

        if not self.api_public:
            raise NirvanaAPIError("NIRVANA_API_PUBLIC is empty")
        if not self.api_private:
            raise NirvanaAPIError("NIRVANA_API_PRIVATE is empty")

    def _headers(self) -> Dict[str, str]:
        return {
            "ApiPublic": self.api_public,
            "ApiPrivate": self.api_private,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"

        async with aiohttp.ClientSession(timeout=self.timeout, headers=self._headers()) as session:
            async with session.request(method, url, json=json_body) as response:
                text = await response.text()
                payload: Optional[Dict[str, Any]] = None

                if text:
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            payload = parsed
                    except Exception:
                        payload = None

                if response.status >= 400:
                    message = "Nirvana request failed"
                    if isinstance(payload, dict):
                        message = str(
                            payload.get("reason")
                            or payload.get("message")
                            or payload.get("error")
                            or message
                        )
                    raise NirvanaAPIError(
                        message=message,
                        http_status=response.status,
                        response=payload,
                        raw_response=text,
                    )

                if not isinstance(payload, dict):
                    raise NirvanaAPIError(
                        message="Nirvana returned invalid JSON",
                        http_status=response.status,
                        raw_response=text,
                    )

                if str(payload.get("status") or "").upper() == "ERROR":
                    raise NirvanaAPIError(
                        message=str(payload.get("reason") or payload.get("message") or "Nirvana returned ERROR"),
                        http_status=response.status,
                        response=payload,
                        raw_response=text,
                    )

                return payload

    async def create_payin(
        self,
        *,
        client_id: str,
        amount: int | float,
        token: str,
        currency: str,
        callback_url: str,
        user_ip: str = "127.0.0.1",
        user_agent: str = "TelegramBot",
        user_email: str = "client@example.com",
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body = {
            "clientID": str(client_id),
            "amount": amount,
            "token": str(token),
            "currency": str(currency),
            "callbackUrl": str(callback_url),
            "userInfo": {
                "ip": str(user_ip or "127.0.0.1"),
                "ua": str(user_agent or "TelegramBot"),
                "email": str(user_email or "client@example.com"),
                "id": str(user_id or client_id),
            },
        }
        return await self._request("POST", "/create/in", json_body=body)

    async def create_v2_order(
        self,
        *,
        client_order_id: str,
        amount: int | float,
        token_code: str,
        currency_code: str = "RUB",
        callback_url: str,
        success_url: str = "",
        fail_url: str = "",
        user_ip: str = "127.0.0.1",
        user_agent: str = "TelegramBot",
        user_email: str = "client@example.com",
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        redirect_url = str(success_url or callback_url).strip()

        body = {
            "amount": amount,
            "redirectURL": redirect_url,
            "siteName": "Vidra-Pay",
            "callbackURL": str(callback_url),
            "externalID": str(client_order_id),
            "currency": str(currency_code),
            "userInfo": {
                "id": str(user_id or client_order_id),
                "ip": str(user_ip or "127.0.0.1"),
                "userAgent": str(user_agent or "TelegramBot"),
                "email": str(user_email or "client@example.com"),
            },
        }

        return await self._request("POST", "/api/v2/order", json_body=body)

    async def create_payout(
        self,
        *,
        client_id: str,
        amount: int | float,
        token: str,
        currency: str,
        receiver: str,
        callback_url: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body = {
            "clientID": str(client_id),
            "amount": amount,
            "token": str(token),
            "currency": str(currency),
            "receiver": str(receiver),
            "extra": dict(extra or {}),
            "callbackUrl": str(callback_url),
        }
        return await self._request("POST", "/create/out", json_body=body)

    async def get_status(self, *, client_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/transaction/status",
            json_body={"clientID": str(client_id)},
        )

    async def get_balance(self) -> Dict[str, Any]:
        return await self._request("GET", "/client/balance")

    async def create_appeal(
        self,
        *,
        external_id: str,
        new_amount: int | float,
        receipt_base64: str,
    ) -> Dict[str, Any]:
        body = {
            "new_amount": new_amount,
            "external_id": str(external_id),
            "receipt": str(receipt_base64),
        }
        return await self._request("POST", "/create/appeal", json_body=body)


def build_nirvana_callback_url(base_callback_url: str, *, order_id: int, client_id: str) -> str:
    separator = "&" if "?" in str(base_callback_url) else "?"
    return f"{str(base_callback_url).rstrip()}{separator}order_id={int(order_id)}&client_id={str(client_id)}"


def encode_receipt_jpeg_base64(file_path: str | Path) -> str:
    path = Path(file_path)
    raw = path.read_bytes()
    return base64.b64encode(raw).decode("ascii")