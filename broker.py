"""
Order execution for live and paper modes with the same interface.

Live mode uses aggressive LIMIT orders (ask + slippage for buys, bid - slippage
for sells). CoinDCX INR markets have no stop orders and some (e.g. WIFINR,
AVAXINR) have no market orders, so limit orders work everywhere and cap slippage.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from coindcx_client import CoinDCXClient, CoinDCXError
from risk import RiskState, floor_to_precision, round_price


@dataclass
class Fill:
    qty: float
    avg_price: float
    fee: float          # INR


def _coin(market: str) -> str:
    return market[:-3] if market.endswith("INR") else market


class PaperBroker:
    """Simulated fills at the current ask/bid, with fees and TDS, cash kept in state."""

    def __init__(self, state: RiskState, cfg, log: Callable[[str], None]):
        self.state, self.cfg, self.log = state, cfg, log
        if self.state.paper_cash is None:
            self.state.paper_cash = cfg.PAPER_START_INR

    def account(self, tickers: Dict[str, dict]) -> Tuple[float, float, float]:
        """(equity, available_inr, coin_exposure_value)"""
        exposure = sum(p.qty * float(tickers.get(m, {}).get("bid") or p.entry)
                       for m, p in self.state.positions.items())
        return self.state.paper_cash + exposure, self.state.paper_cash, exposure

    def buy(self, market: str, qty: float, ticker: dict, details: dict) -> Optional[Fill]:
        price = float(ticker["ask"])
        value = qty * price
        fee = value * self.cfg.FEE_PCT / 100
        if value + fee > self.state.paper_cash:
            self.log(f"{market}: PAPER buy skipped, not enough paper cash")
            return None
        self.state.paper_cash -= value + fee
        return Fill(qty, price, fee)

    def sell(self, market: str, qty: float, ticker_fn: Callable[[], Dict[str, dict]],
             details: dict) -> Optional[Fill]:
        price = float(ticker_fn()[market]["bid"])
        value = qty * price
        fee = value * self.cfg.FEE_PCT / 100
        tds = value * self.cfg.TDS_PCT / 100
        self.state.paper_cash += value - fee - tds
        self.state.tds_withheld_total += tds
        return Fill(qty, price, fee)


class LiveBroker:
    def __init__(self, client: CoinDCXClient, state: RiskState, cfg, log: Callable[[str], None]):
        self.client, self.state, self.cfg, self.log = client, state, cfg, log

    def account(self, tickers: Dict[str, dict]) -> Tuple[float, float, float]:
        bals = self.client.balances()
        inr = bals.get("INR", {"balance": 0.0, "total": 0.0})
        exposure = 0.0
        for cur, b in bals.items():
            if cur == "INR" or b["total"] <= 0:
                continue
            t = tickers.get(f"{cur}INR")
            if t and t.get("bid"):
                exposure += b["total"] * float(t["bid"])
        return inr["total"] + exposure, inr["balance"], exposure

    def holding(self, market: str) -> float:
        return self.client.balances().get(_coin(market), {}).get("total", 0.0)

    # ── internals ─────────────────────────────────────────────────────────
    def _execute(self, side: str, market: str, qty: float, price: float, details: dict) -> Fill:
        price = round_price(price, int(details.get("base_currency_precision", 2)))
        qty = floor_to_precision(qty, int(details.get("target_currency_precision", 6)))
        cid = f"v2-{market}-{side}-{uuid.uuid4().hex[:10]}"
        try:
            order = self.client.create_limit_order(side, market, price, qty, cid)
            oid = str(order.get("id", ""))
        except CoinDCXError as e:
            # The request may still have reached the exchange: look it up by client id.
            self.log(f"{market}: create {side} error ({e}); checking by client_order_id")
            try:
                oid = str(self.client.order_status(client_order_id=cid).get("id", ""))
            except CoinDCXError:
                return Fill(0.0, 0.0, 0.0)
        deadline = time.time() + self.cfg.ORDER_FILL_TIMEOUT_SEC
        status: Dict = {}
        while time.time() < deadline:
            time.sleep(2)
            status = self.client.order_status(order_id=oid)
            if status.get("status") in ("filled", "cancelled", "rejected", "partially_cancelled"):
                break
        if status.get("status") not in ("filled", "cancelled", "rejected", "partially_cancelled"):
            try:
                self.client.cancel_order(order_id=oid)
            except CoinDCXError as e:
                self.log(f"{market}: cancel failed ({e})")
            time.sleep(1)
            status = self.client.order_status(order_id=oid)
        total = float(status.get("total_quantity", qty) or qty)
        remaining = float(status.get("remaining_quantity", total) or 0)
        filled = max(total - remaining, 0.0)
        avg = float(status.get("avg_price", 0) or 0) or price
        fee = float(status.get("fee_amount", 0) or 0) or filled * avg * self.cfg.FEE_PCT / 100
        self.log(f"{market}: {side.upper()} {status.get('status')} filled {filled} @ {avg} fee ₹{fee:.2f}")
        return Fill(filled, avg, fee)

    # ── public ────────────────────────────────────────────────────────────
    def buy(self, market: str, qty: float, ticker: dict, details: dict) -> Optional[Fill]:
        price = float(ticker["ask"]) * (1 + self.cfg.SLIPPAGE_PCT / 100)
        fill = self._execute("buy", market, qty, price, details)
        return fill if fill.qty > 0 else None

    def sell(self, market: str, qty: float, ticker_fn: Callable[[], Dict[str, dict]],
             details: dict) -> Optional[Fill]:
        """Sell qty, retrying at the fresh bid until filled or retries run out."""
        min_qty = float(details.get("min_quantity", 0) or 0)
        held = self.holding(market)
        target = min(qty, held)                       # never sell more than we hold
        if target < qty:
            self.log(f"{market}: only {held} held (position says {qty}); selling {target}")
        sold, value, fees = 0.0, 0.0, 0.0
        for _ in range(self.cfg.EXIT_RETRIES):
            left = min(target - sold, self.holding(market))
            if left <= 0 or left < min_qty:
                break
            bid = float(ticker_fn()[market]["bid"])
            fill = self._execute("sell", market, left, bid * (1 - self.cfg.SLIPPAGE_PCT / 100), details)
            sold += fill.qty
            value += fill.qty * fill.avg_price
            fees += fill.fee
            if target - sold < max(min_qty, 1e-12):
                break
        if sold <= 0:
            return None
        if target - sold >= min_qty and min_qty > 0:
            self.log(f"{market}: WARNING {target - sold} still unsold after {self.cfg.EXIT_RETRIES} tries")
        self.state.tds_withheld_total += value * self.cfg.TDS_PCT / 100
        return Fill(sold, value / sold, fees)
