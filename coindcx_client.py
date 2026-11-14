"""
Thin CoinDCX REST client (spot). Endpoints and fields checked against
https://docs.coindcx.com and the live public API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Optional

import requests


class CoinDCXError(RuntimeError):
    pass


class CoinDCXClient:
    def __init__(self, api_key: str = "", api_secret: str = "",
                 base_url: str = "https://api.coindcx.com",
                 public_base_url: str = "https://public.coindcx.com",
                 timeout: int = 15):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._markets_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._markets_cached_at = 0.0

    # ── HTTP helpers ──────────────────────────────────────────────────────
    def _get(self, url: str, params: Optional[dict] = None, retries: int = 3) -> Any:
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    raise CoinDCXError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, CoinDCXError, ValueError) as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise CoinDCXError(f"GET {url} failed: {last}")

    def _post(self, endpoint: str, body: Dict[str, Any]) -> Any:
        if not self.api_key or not self.api_secret:
            raise CoinDCXError("Missing COINDCX_API_KEY / COINDCX_API_SECRET")
        body = dict(body)
        body["timestamp"] = int(time.time() * 1000)
        payload = json.dumps(body, separators=(",", ":"))       # sign exactly what is sent
        sig = hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json",
                   "X-AUTH-APIKEY": self.api_key, "X-AUTH-SIGNATURE": sig}
        r = self.session.post(self.base_url + endpoint, data=payload, headers=headers,
                              timeout=self.timeout)
        if r.status_code >= 400:
            raise CoinDCXError(f"POST {endpoint} HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    # ── Public ────────────────────────────────────────────────────────────
    def markets(self, max_age_sec: int = 6 * 3600) -> Dict[str, Dict[str, Any]]:
        """markets_details keyed by coindcx_name (cached; the response is ~0.5 MB)."""
        if self._markets_cache is None or time.time() - self._markets_cached_at > max_age_sec:
            data = self._get(self.base_url + "/exchange/v1/markets_details")
            self._markets_cache = {m["coindcx_name"]: m for m in data}
            self._markets_cached_at = time.time()
        return self._markets_cache

    def tickers(self) -> Dict[str, Dict[str, Any]]:
        """All tickers keyed by market. Fields: bid, ask, last_price, timestamp."""
        return {t["market"]: t for t in self._get(self.base_url + "/exchange/ticker")}

    def candles(self, pair: str, interval: str, limit: int = 500,
                start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> List[dict]:
        """Raw candles, NEWEST FIRST (as CoinDCX returns them). Use strategy.normalize_candles."""
        params: Dict[str, Any] = {"pair": pair, "interval": interval, "limit": min(limit, 1000)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        return self._get(self.public_base_url + "/market_data/candles", params)

    # ── Private ───────────────────────────────────────────────────────────
    def user_info(self) -> Dict[str, Any]:
        return self._post("/exchange/v1/users/info", {})

    def balances(self) -> Dict[str, Dict[str, float]]:
        """{currency: {"balance": free, "locked": locked, "total": free+locked}}"""
        out = {}
        for b in self._post("/exchange/v1/users/balances", {}):
            free = float(b.get("balance", 0) or 0)
            locked = float(b.get("locked_balance", 0) or 0)
            out[b["currency"]] = {"balance": free, "locked": locked, "total": free + locked}
        return out

    def create_limit_order(self, side: str, market: str, price: float, qty: float,
                           client_order_id: str) -> Dict[str, Any]:
        body = {"side": side, "order_type": "limit_order", "market": market,
                "price_per_unit": price, "total_quantity": qty,
                "client_order_id": client_order_id}
        res = self._post("/exchange/v1/orders/create", body)
        orders = res.get("orders") if isinstance(res, dict) else None
        if not orders:
            raise CoinDCXError(f"unexpected create response: {res}")
        return orders[0]

    def order_status(self, order_id: str = "", client_order_id: str = "") -> Dict[str, Any]:
        body = {"id": order_id} if order_id else {"client_order_id": client_order_id}
        return self._post("/exchange/v1/orders/status", body)

    def cancel_order(self, order_id: str = "", client_order_id: str = "") -> Dict[str, Any]:
        body = {"id": order_id} if order_id else {"client_order_id": client_order_id}
        return self._post("/exchange/v1/orders/cancel", body)
