#!/usr/bin/env python3
"""
CoinDCX INR Spot Bot v2 — EMA crossover + trend + RSI filter, risk-sized,
with bot-managed stop-loss and target.

Paper trading by default (no API keys needed). Live trading needs BOTH:
    export DRY_RUN=false
    export CONFIRM_LIVE=YES
Create a file named STOP in the working directory to stop gracefully.

IMPORTANT: CoinDCX INR markets have no stop orders, so stops are checked by
this bot every MONITOR_INTERVAL_SEC. If the bot is not running, open positions
have NO stop. Run it under systemd/tmux and monitor it.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import config as cfg
import strategy as st
from broker import LiveBroker, PaperBroker
from coindcx_client import CoinDCXClient, CoinDCXError
from risk import (Position, RiskState, account_gate, market_gate, record_close,
                  roll_day, size_position)

IST = ZoneInfo("Asia/Kolkata")
LIVE = cfg.is_live()
MODE = "LIVE" if LIVE else "PAPER"
os.makedirs(cfg.DATA_DIR, exist_ok=True)
STATE_PATH = os.path.join(cfg.DATA_DIR, f"state_{MODE.lower()}.json")
TRADES_CSV = os.path.join(cfg.DATA_DIR, f"trades_{MODE.lower()}.csv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(os.path.join(cfg.DATA_DIR, "bot.log"), encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
_logger = logging.getLogger("bot")


def log(msg: str) -> None:
    _logger.info(f"[{MODE}] {msg}")


def now_ist() -> datetime:
    return datetime.now(IST)


def fmt_price(p: float) -> str:
    """Enough decimals for cheap coins (PEPE) without clutter for BTC."""
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:.8f}".rstrip("0")


def write_trade(row: Dict) -> None:
    new = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["time", "mode", "market", "side", "qty", "price",
                                          "fee", "pnl", "reason"])
        if new:
            w.writeheader()
        w.writerow(row)


# ── Bot ────────────────────────────────────────────────────────────────────
class Bot:
    def __init__(self):
        self.client = CoinDCXClient(cfg.API_KEY, cfg.API_SECRET, cfg.BASE_URL,
                                    cfg.PUBLIC_BASE_URL, cfg.HTTP_TIMEOUT_SEC)
        self.state = RiskState.load(STATE_PATH)
        self.broker = (LiveBroker(self.client, self.state, cfg, log) if LIVE
                       else PaperBroker(self.state, cfg, log))
        self.tickers: Dict[str, dict] = {}
        self.rows: List[Dict[str, str]] = []
        self._unmanaged_logged = False

    # helpers ---------------------------------------------------------------
    def refresh_tickers(self) -> Dict[str, dict]:
        self.tickers = self.client.tickers()
        return self.tickers

    def details(self, market: str) -> Optional[dict]:
        return self.client.markets().get(market)

    def save(self) -> None:
        self.state.save(STATE_PATH)

    # startup ---------------------------------------------------------------
    def startup(self) -> bool:
        log(f"Starting v2 | interval {cfg.INTERVAL} | {len(cfg.WATCHLIST)} markets | state {STATE_PATH}")
        if not cfg.DRY_RUN and not LIVE:
            log("DRY_RUN=false but CONFIRM_LIVE is not 'YES' → staying in PAPER mode")
        if LIVE:
            try:
                self.client.balances()
                log("Auth OK")
            except CoinDCXError as e:
                log(f"Auth failed: {e}")
                return False
            self.reconcile()
        self.refresh_tickers()
        markets = self.client.markets()
        for m in cfg.WATCHLIST:
            d = markets.get(m)
            if not d:
                log(f"{m}: not listed on CoinDCX → ignored")
            elif d.get("status") != "active":
                log(f"{m}: status {d.get('status')} → ignored")
        return True

    def reconcile(self) -> None:
        """Drop positions that no longer exist on the exchange (sold manually)."""
        bals = self.client.balances()
        for m, p in list(self.state.positions.items()):
            d = self.details(m) or {}
            held = bals.get(m[:-3], {}).get("total", 0.0)
            if held < max(float(d.get("min_quantity", 0) or 0), p.qty * 0.5):
                log(f"{m}: position in state but only {held} held on exchange → removed from state")
                del self.state.positions[m]
            elif held < p.qty:
                p.qty = held
        managed = set(self.state.positions)
        unmanaged = [c for c, b in bals.items()
                     if c != "INR" and b["total"] > 0 and f"{c}INR" in cfg.WATCHLIST
                     and f"{c}INR" not in managed]
        if unmanaged and not self._unmanaged_logged:
            log(f"Holdings NOT managed by the bot (left untouched): {', '.join(unmanaged)}")
            self._unmanaged_logged = True
        self.save()

    # exits -----------------------------------------------------------------
    def close_position(self, market: str, reason: str) -> None:
        p = self.state.positions.get(market)
        d = self.details(market)
        if not p or not d:
            return
        fill = self.broker.sell(market, p.qty, self.refresh_tickers, d)
        if not fill:
            log(f"{market}: EXIT FAILED ({reason}); will retry next check")
            return
        pnl = (fill.avg_price - p.entry) * fill.qty - fill.fee - p.entry_fee * (fill.qty / p.qty)
        today = now_ist().date()
        if fill.qty < p.qty * 0.999:
            p.entry_fee *= 1 - fill.qty / p.qty
            p.qty -= fill.qty
            log(f"{market}: partial exit {fill.qty}, {p.qty} left")
        else:
            del self.state.positions[market]
        record_close(self.state, pnl, today, cfg)
        write_trade({"time": now_ist().isoformat(timespec="seconds"), "mode": MODE,
                     "market": market, "side": "sell", "qty": fill.qty,
                     "price": fill.avg_price, "fee": round(fill.fee, 2),
                     "pnl": round(pnl, 2), "reason": reason})
        log(f"{market}: EXIT {reason} {fill.qty} @ ₹{fill.avg_price:,.4f} | P&L ₹{pnl:,.2f} "
            f"| streak {self.state.loss_streak} | today ₹{self.state.realized_pnl_today:,.2f}")
        if self.state.cooldown_until:
            log(f"Cooldown active until {self.state.cooldown_until}")
        self.save()

    def check_stops(self) -> None:
        if not self.state.positions:
            return
        tickers = self.refresh_tickers()
        for m, p in list(self.state.positions.items()):
            t = tickers.get(m)
            if not t or not t.get("bid"):
                continue
            bid = float(t["bid"])
            if bid <= p.stop:
                self.close_position(m, f"STOP (bid {bid} ≤ {p.stop:.4f})")
            elif bid >= p.target:
                self.close_position(m, f"TARGET (bid {bid} ≥ {p.target:.4f})")

    # entries ---------------------------------------------------------------
    def evaluate(self) -> None:
        now = now_ist()
        now_ms = int(time.time() * 1000)
        tickers = self.refresh_tickers()
        equity, avail_inr, exposure = self.broker.account(tickers)
        roll_day(self.state, now.date(), equity)
        if LIVE:
            self.reconcile()
        log(f"Equity ₹{equity:,.2f} | free INR ₹{avail_inr:,.2f} | in coins ₹{exposure:,.2f} "
            f"| open {len(self.state.positions)} | trades today {self.state.trades_today}")
        self.rows = []
        markets = self.client.markets()

        for market in cfg.WATCHLIST:
            d = markets.get(market)
            if not d or d.get("status") != "active":
                continue
            try:
                raw = self.client.candles(d["pair"], cfg.INTERVAL, cfg.CANDLE_LIMIT)
            except CoinDCXError as e:
                log(f"{market}: candles failed ({e})")
                continue
            candles = st.normalize_candles(raw, cfg.INTERVAL, now_ms)
            ind_all = st.compute_indicators(candles, cfg)
            ind = ind_all[-1] if ind_all else None
            pos = self.state.positions.get(market)
            signal = "-"

            if pos and st.exit_on_signal(ind, cfg):
                signal = "EXIT"
                self.close_position(market, "EMA bear cross")
            elif not pos:
                sig = st.entry_signal(ind, cfg)
                if sig:
                    signal = "SKIP"
                    ok, why = account_gate(self.state, now.date(), cfg)
                    if ok:
                        if self.try_enter(market, d, sig, equity, avail_inr, exposure):
                            signal = "BUY"
                        equity, avail_inr, exposure = self.broker.account(self.refresh_tickers())
                    else:
                        log(f"{market}: entry signal but blocked → {why}")
            self.rows.append({
                "market": market,
                "price": fmt_price(ind.close) if ind else "-",
                "rsi": f"{ind.rsi:.1f}" if ind else "-",
                "hold": "Y" if market in self.state.positions else "N",
                "sig": signal,
                "reason": st.describe(ind, cfg),
            })
            time.sleep(0.3)
        self.save()

    def try_enter(self, market: str, d: dict, sig: st.EntrySignal,
                  equity: float, avail_inr: float, exposure: float) -> bool:
        t = self.tickers.get(market)
        if not t or not t.get("ask") or not t.get("bid"):
            log(f"{market}: no bid/ask price → skip")
            return False
        ask, bid = float(t["ask"]), float(t["bid"])
        risk_dist = sig.entry_ref - sig.stop
        stop = ask - risk_dist
        spread_pct = (ask - bid) / ask * 100 if ask > 0 else 100.0
        if spread_pct > cfg.MAX_SPREAD_PCT:
            log(f"{market}: signal skipped → spread {spread_pct:.2f}% > {cfg.MAX_SPREAD_PCT:.2f}% "
                f"(bid {fmt_price(bid)} / ask {fmt_price(ask)})")
            return False
        if bid <= stop:
            log(f"{market}: signal skipped → bid {fmt_price(bid)} already at/below stop {fmt_price(stop)}")
            return False
        qty, note = size_position(equity, avail_inr, ask, stop, cfg,
                                  float(d.get("min_quantity", 0) or 0),
                                  int(d.get("target_currency_precision", 6)))
        if qty <= 0:
            log(f"{market}: signal skipped → {note}")
            return False
        ok, why = market_gate(self.state, market, equity, exposure, qty * ask, cfg)
        if not ok:
            log(f"{market}: signal skipped → {why}")
            return False
        log(f"{market}: BUY {qty} @ ~₹{ask:,.4f} ({note}) | spread {spread_pct:.2f}% | {sig.reason}")
        fill = self.broker.buy(market, qty, t, d)
        if not fill:
            log(f"{market}: buy not filled")
            return False
        self.state.positions[market] = Position(
            market=market, qty=fill.qty, entry=fill.avg_price,
            stop=fill.avg_price - risk_dist,
            target=fill.avg_price + cfg.TARGET_R * risk_dist,
            opened_at=now_ist().isoformat(timespec="seconds"), entry_fee=fill.fee)
        self.state.trades_today += 1
        p = self.state.positions[market]
        write_trade({"time": p.opened_at, "mode": MODE, "market": market, "side": "buy",
                     "qty": fill.qty, "price": fill.avg_price, "fee": round(fill.fee, 2),
                     "pnl": "", "reason": sig.reason})
        log(f"{market}: OPEN {fill.qty} @ ₹{fill.avg_price:,.4f} | stop ₹{p.stop:,.4f} "
            f"| target ₹{p.target:,.4f}")
        self.save()
        return True

    # dashboard -------------------------------------------------------------
    def dashboard(self, next_eval: float) -> None:
        if not cfg.SHOW_DASHBOARD or not self.rows:
            return
        print("\n" + f" CoinDCX v2 • {MODE} • {cfg.INTERVAL} ".center(78, "═"))
        print(f"{'Market':10}{'Price':>16}{'RSI':>7}{'Hold':>6}{'Signal':>8}  Reason")
        for r in self.rows:
            print(f"{r['market']:10}{r['price']:>16}{r['rsi']:>7}{r['hold']:>6}{r['sig']:>8}  {r['reason']}")
        for m, p in self.state.positions.items():
            print(f"  • {m}: {p.qty} @ {fmt_price(p.entry)} | stop {fmt_price(p.stop)} "
                  f"| target {fmt_price(p.target)}")
        nxt = datetime.fromtimestamp(next_eval, IST).strftime("%H:%M:%S")
        print(f" P&L today ₹{self.state.realized_pnl_today:,.2f} • total ₹{self.state.realized_pnl_total:,.2f}"
              f" • next signal check {nxt} IST ".center(78, "═"))

    # main loop -------------------------------------------------------------
    def next_eval_time(self) -> float:
        step = st.INTERVAL_MS[cfg.INTERVAL] / 1000
        return (time.time() // step + 1) * step + cfg.CANDLE_CLOSE_DELAY_SEC

    def run(self) -> None:
        if not self.startup():
            return
        next_eval = time.time()            # evaluate once immediately
        while True:
            try:
                if os.path.exists(cfg.STOP_FILE):
                    log("STOP file found → exiting. Open positions are NOT protected while stopped!")
                    break
                if time.time() >= next_eval:
                    self.evaluate()
                    next_eval = self.next_eval_time()
                    self.dashboard(next_eval)
                self.check_stops()
                time.sleep(cfg.MONITOR_INTERVAL_SEC)
            except KeyboardInterrupt:
                log("Interrupted → exiting. Open positions are NOT protected while stopped!")
                break
            except Exception as e:                       # keep running, but say why
                _logger.exception(f"[{MODE}] loop error: {e}")
                time.sleep(60)
        self.save()


if __name__ == "__main__":
    Bot().run()
