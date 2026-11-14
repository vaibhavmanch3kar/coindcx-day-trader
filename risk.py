"""
Position sizing, rounding and the risk gates that every new trade must pass.
Pure logic plus a small JSON-backed state, shared by the bot and backtester.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Dict, Optional, Tuple


# ── Rounding ───────────────────────────────────────────────────────────────
def floor_to_precision(value: float, precision: int) -> float:
    """Round DOWN to the given number of decimals (never buy/sell more than intended)."""
    f = 10 ** precision
    return math.floor(value * f + 1e-9) / f


def round_price(value: float, precision: int) -> float:
    return round(value, precision)


# ── Sizing ─────────────────────────────────────────────────────────────────
def size_position(equity: float, available_inr: float, entry: float, stop: float, cfg,
                  min_qty: float, qty_precision: int) -> Tuple[float, str]:
    """
    Quantity so that hitting the stop loses about RISK_PER_TRADE_PCT of equity
    (fees included), capped by MAX_POSITION_PCT and available INR.
    Returns (qty, note). qty == 0 means skip, note says why.
    """
    if entry <= 0 or stop <= 0 or stop >= entry:
        return 0.0, "invalid entry/stop"
    fee = cfg.FEE_PCT / 100
    risk_inr = equity * cfg.RISK_PER_TRADE_PCT / 100
    # loss per unit if stopped = price move + fee on entry + fee on exit
    loss_per_unit = (entry - stop) + entry * fee + stop * fee
    qty = risk_inr / loss_per_unit
    notes = []

    cap_value = equity * cfg.MAX_POSITION_PCT / 100
    if qty * entry > cap_value:
        qty = cap_value / entry
        notes.append(f"capped at {cfg.MAX_POSITION_PCT:.0f}% of equity")
    spendable = available_inr / (1 + fee) * 0.995      # keep room for fees
    if qty * entry > spendable:
        qty = spendable / entry
        notes.append("capped by available INR")

    qty = floor_to_precision(qty, qty_precision)
    if qty <= 0 or qty < min_qty:
        return 0.0, f"qty below market minimum ({min_qty})"
    if qty * entry < cfg.MIN_NOTIONAL_INR:
        return 0.0, f"order value below ₹{cfg.MIN_NOTIONAL_INR:.0f} minimum"
    return qty, "; ".join(notes) or "risk-sized"


# ── State ──────────────────────────────────────────────────────────────────
@dataclass
class Position:
    market: str
    qty: float
    entry: float
    stop: float
    target: float
    opened_at: str
    entry_fee: float = 0.0
    entry_order_id: str = ""


@dataclass
class RiskState:
    day: str = ""
    day_start_equity: float = 0.0
    realized_pnl_today: float = 0.0
    trades_today: int = 0
    loss_streak: int = 0
    cooldown_until: str = ""                  # ISO date; no new trades before it
    realized_pnl_total: float = 0.0
    tds_withheld_total: float = 0.0
    paper_cash: Optional[float] = None        # paper mode only
    positions: Dict[str, Position] = field(default_factory=dict)

    # persistence -----------------------------------------------------------
    @classmethod
    def load(cls, path: str) -> "RiskState":
        if not os.path.exists(path):
            return cls()
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        positions = {k: Position(**v) for k, v in raw.pop("positions", {}).items()}
        return cls(**raw, positions=positions)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        data = asdict(self)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)                   # atomic write


# ── Daily roll / gates ─────────────────────────────────────────────────────
def roll_day(state: RiskState, today: date, equity: float) -> None:
    if state.day != today.isoformat():
        state.day = today.isoformat()
        state.day_start_equity = equity
        state.realized_pnl_today = 0.0
        state.trades_today = 0


def account_gate(state: RiskState, today: date, cfg) -> Tuple[bool, str]:
    """Rules that block ALL new entries."""
    if state.cooldown_until and today < date.fromisoformat(state.cooldown_until):
        return False, f"cooldown after {cfg.LOSS_STREAK_LIMIT} losses until {state.cooldown_until}"
    if state.trades_today >= cfg.MAX_TRADES_PER_DAY:
        return False, f"max {cfg.MAX_TRADES_PER_DAY} trades today"
    if state.day_start_equity > 0:
        limit = state.day_start_equity * cfg.MAX_DAILY_LOSS_PCT / 100
        if state.realized_pnl_today <= -limit:
            return False, f"daily loss limit hit (₹{state.realized_pnl_today:,.0f})"
    if len(state.positions) >= cfg.MAX_OPEN_POSITIONS:
        return False, f"max {cfg.MAX_OPEN_POSITIONS} open positions"
    return True, "ok"


def market_gate(state: RiskState, market: str, equity: float, exposure_value: float,
                new_value: float, cfg) -> Tuple[bool, str]:
    """Rules for one specific new entry."""
    if market in state.positions:
        return False, "already in a position (never add)"
    cap = equity * cfg.GLOBAL_EXPOSURE_PCT / 100
    if exposure_value + new_value > cap:
        return False, f"exposure would exceed {cfg.GLOBAL_EXPOSURE_PCT:.0f}% of equity"
    return True, "ok"


def record_close(state: RiskState, pnl: float, today: date, cfg) -> None:
    """Update P&L, loss streak and cooldown after a position closes."""
    state.realized_pnl_today += pnl
    state.realized_pnl_total += pnl
    if pnl < 0:
        state.loss_streak += 1
        if state.loss_streak >= cfg.LOSS_STREAK_LIMIT:
            state.cooldown_until = (today + timedelta(days=cfg.COOLDOWN_DAYS + 1)).isoformat()
            state.loss_streak = 0
    else:
        state.loss_streak = 0
