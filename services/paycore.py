# -----------------------------------------------------------------------------
# Paycore API client (v2)
# -----------------------------------------------------------------------------
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiohttp


class PaycoreAPIError(Exception):
    pass


@dataclass
class PaycoreClient:
    base_url: str
    merchant_token: str
    timeout_sec: int = 20

    def _hash(self, transaction_id: str) -> str:
        """
        Paycore hash: sha256(MerchantToken + transactionID)
        """
        raw = f"{self.merchant_token}{transaction_id}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    async def create_transaction(
        self,
        *,
        transaction_id: str,
        amount: str,
        phone_number: str,
        transaction_type: str = "payment",
    ) -> str:
        h = self._hash(transaction_id)
        url = f"{self.base_url.rstrip('/')}/api/v2/exchanger/{h}?transaction_id={transaction_id}"

        payload = {
            "amount": str(amount),
            "phone_number": str(phone_number),
            "transaction_type": str(transaction_type),
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise PaycoreAPIError(f"Paycore create failed: HTTP {resp.status}: {text}")
                try:
                    data = json.loads(text)
                except Exception:
                    raise PaycoreAPIError(f"Paycore create invalid JSON: {text}")

        pay_url = data.get("url")
        if not pay_url:
            raise PaycoreAPIError(f"Paycore create: no 'url' in response: {data}")
        return str(pay_url)

    async def get_status(self, *, transaction_id: str) -> Dict[str, Any]:
        h = self._hash(transaction_id)
        url = f"{self.base_url.rstrip('/')}/api/v2/exchanger/{h}?transaction_id={transaction_id}"

        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise PaycoreAPIError(f"Paycore status failed: HTTP {resp.status}: {text}")
                try:
                    data = json.loads(text)
                except Exception:
                    raise PaycoreAPIError(f"Paycore status invalid JSON: {text}")

        if not isinstance(data, dict):
            raise PaycoreAPIError(f"Paycore status unexpected response: {data}")
        return data
