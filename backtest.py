#!/usr/bin/env python3
"""
Portfolio backtest of the v2 strategy on real CoinDCX candles, using the SAME
strategy and risk code as the live bot.

    python backtest.py --days 180
    python backtest.py --days 365 --interval 1h --markets BTCINR,ETHINR

Assumptions (conservative where it matters):
- Signals use closed candles; entries/exits fill at the NEXT candle's open.
- Buys pay +SLIPPAGE_PCT, sells get -SLIPPAGE_PCT, plus FEE_PCT each side.
- If a candle touches both stop and target, the STOP is assumed to hit first.
- A candle that opens beyond the stop exits at the open (gap), not the stop.
- 1% TDS on sells is reported separately (it is a tax credit, not a cost).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List
from zoneinfo import ZoneInfo

import config as cfg
import strategy as st
from coindcx_client import CoinDCXClient
from risk import (Position, RiskState, account_gate, market_gate, record_close,
                  roll_day, size_position)

IST = ZoneInfo("Asia/Kolkata")


def fetch_history(client: CoinDCXClient, market: str, pair: str, interval: str, days: int,
                  cache_dir: str) -> List[st.Candle]:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{market}_{interval}_{days}d.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        with open(path) as f:
            raw = json.load(f)
    else:
        step = st.INTERVAL_MS[interval]
        end = int(time.time() * 1000)
        start = end - days * 86_400_000
        window = step * 900                      # < 1000 candles per request
        raw, t = [], start
        while t < end:
            raw.extend(client.candles(pair, interval, 1000, start_ms=t, end_ms=min(t + window, end)))
            t += window
            time.sleep(0.2)
        with open(path, "w") as f:
            json.dump(raw, f)
    return st.normalize_candles(raw, interval, int(time.time() * 1000))


def ist_date(ms: int):
    return datetime.fromtimestamp(ms / 1000, IST).date()


def run(days: int, interval: str, markets: List[str], start_inr: float, out_csv: str) -> Dict:
    client = CoinDCXClient(public_base_url=cfg.PUBLIC_BASE_URL, base_url=cfg.BASE_URL)
    details = client.markets()
    data: Dict[str, List[st.Candle]] = {}
    inds: Dict[str, list] = {}
    for m in markets:
        d = details.get(m)
        if not d:
            print(f"{m}: not listed, skipped")
            continue
        candles = fetch_history(client, m, d["pair"], interval, days, os.path.join(cfg.DATA_DIR, "candles"))
        if len(candles) < cfg.EMA_TREND + 50:
            print(f"{m}: only {len(candles)} candles, skipped")
            continue
        data[m], inds[m] = candles, st.compute_indicators(candles, cfg)

    index = {m: {c.time: i for i, c in enumerate(cs)} for m, cs in data.items()}
    timeline = sorted({c.time for cs in data.values() for c in cs})
    slip, fee = cfg.SLIPPAGE_PCT / 100, cfg.FEE_PCT / 100

    state = RiskState(paper_cash=start_inr)
    risk_dist: Dict[str, float] = {}
    pending_entry: Dict[str, st.EntrySignal] = {}
    pending_exit: Dict[str, str] = {}
    last_close: Dict[str, float] = {}
    trades, equity_curve = [], []
    fees_total = 0.0
    peak, max_dd = start_inr, 0.0

    def equity() -> float:
        return state.paper_cash + sum(p.qty * last_close.get(m, p.entry) for m, p in state.positions.items())

    def exposure() -> float:
        return sum(p.qty * last_close.get(m, p.entry) for m, p in state.positions.items())

    def do_exit(m: str, price: float, t: int, reason: str):
        nonlocal fees_total
        p = state.positions.pop(m)
        px = price * (1 - slip)
        value = p.qty * px
        f = value * fee
        state.paper_cash += value - f
        state.tds_withheld_total += value * cfg.TDS_PCT / 100
        fees_total += f
        pnl = (px - p.entry) * p.qty - f - p.entry_fee
        record_close(state, pnl, ist_date(t), cfg)
        trades.append({"market": m, "entry_time": p.opened_at,
                       "exit_time": datetime.fromtimestamp(t / 1000, IST).isoformat(timespec="minutes"),
                       "qty": p.qty, "entry": round(p.entry, 6), "exit": round(px, 6),
                       "pnl": round(pnl, 2), "r": round(pnl / (risk_dist[m] * p.qty), 2), "reason": reason})

    for t in timeline:
        today = ist_date(t)
        roll_day(state, today, equity())
        for m in data:
            i = index[m].get(t)
            if i is None:
                continue
            c = data[m][i]
            # 1) orders queued at the previous close fill at this open
            if m in pending_exit and m in state.positions:
                do_exit(m, c.open, t, pending_exit.pop(m))
            pending_exit.pop(m, None)
            sig = pending_entry.pop(m, None)
            if sig and m not in state.positions:
                ok, _ = account_gate(state, today, cfg)
                if ok:
                    fill = c.open * (1 + slip)
                    dist = sig.entry_ref - sig.stop
                    d = details[m]
                    qty, _ = size_position(equity(), state.paper_cash, fill, fill - dist, cfg,
                                           float(d.get("min_quantity", 0) or 0),
                                           int(d.get("target_currency_precision", 6)))
                    ok2, _ = market_gate(state, m, equity(), exposure(), qty * fill, cfg)
                    if qty > 0 and ok2:
                        f = qty * fill * fee
                        state.paper_cash -= qty * fill + f
                        fees_total += f
                        state.trades_today += 1
                        risk_dist[m] = dist
                        state.positions[m] = Position(
                            m, qty, fill, fill - dist, fill + cfg.TARGET_R * dist,
                            datetime.fromtimestamp(t / 1000, IST).isoformat(timespec="minutes"), f)
            # 2) stop / target inside this candle (stop first if both)
            p = state.positions.get(m)
            if p:
                if c.low <= p.stop:
                    do_exit(m, min(c.open, p.stop), t, "STOP")
                elif c.high >= p.target:
                    do_exit(m, max(c.open, p.target), t, "TARGET")
            # 3) signals on this closed candle → act at next open
            last_close[m] = c.close
            ind = inds[m][i]
            if m in state.positions:
                if st.exit_on_signal(ind, cfg):
                    pending_exit[m] = "EMA bear cross"
            else:
                s = st.entry_signal(ind, cfg)
                if s:
                    pending_entry[m] = s
        eq = equity()
        equity_curve.append((t, eq))
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)

    # close anything still open at the last price (mark-to-market, no fee)
    final_eq = equity()

    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()) if trades else ["market"])
        w.writeheader()
        w.writerows(trades)

    wins = [x for x in trades if x["pnl"] > 0]
    losses = [x for x in trades if x["pnl"] <= 0]
    gross_win = sum(x["pnl"] for x in wins)
    gross_loss = -sum(x["pnl"] for x in losses)
    per_market = defaultdict(lambda: [0, 0.0])
    for x in trades:
        per_market[x["market"]][0] += 1
        per_market[x["market"]][1] += x["pnl"]
    bh = [(cs[-1].close / cs[cfg.EMA_TREND].close - 1) * 100 for cs in data.values()]
    first = datetime.fromtimestamp(timeline[0] / 1000, IST).date() if timeline else None
    last = datetime.fromtimestamp(timeline[-1] / 1000, IST).date() if timeline else None
    return {
        "period": f"{first} → {last}", "interval": interval, "markets": len(data),
        "start_inr": start_inr, "end_equity": round(final_eq, 2),
        "return_pct": round((final_eq / start_inr - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2), "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_r": round(sum(x["r"] for x in trades) / len(trades), 2) if trades else 0,
        "fees_paid": round(fees_total, 2), "tds_withheld": round(state.tds_withheld_total, 2),
        "open_at_end": len(state.positions),
        "buy_hold_equal_weight_pct": round(sum(bh) / len(bh), 2) if bh else None,
        "per_market": {m: {"trades": n, "pnl": round(p, 2)} for m, (n, p) in sorted(per_market.items())},
        "exits": {r: sum(1 for x in trades if x["reason"] == r) for r in {x["reason"] for x in trades}},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest the v2 strategy on CoinDCX candles")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--interval", default=cfg.INTERVAL, choices=list(st.INTERVAL_MS))
    ap.add_argument("--markets", default=",".join(cfg.WATCHLIST))
    ap.add_argument("--start-inr", type=float, default=cfg.PAPER_START_INR)
    ap.add_argument("--out", default=os.path.join(cfg.DATA_DIR, "backtest_trades.csv"))
    a = ap.parse_args()
    res = run(a.days, a.interval, [m.strip().upper() for m in a.markets.split(",")], a.start_inr, a.out)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    print(f"\nTrade list: {a.out}")


if __name__ == "__main__":
    main()
