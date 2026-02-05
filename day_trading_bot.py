#!/usr/bin/env python3
"""
CoinDCX INR Day-Trading Bot (EMA + RSI, 15m)

SAFE TESTING INSTRUCTIONS:
- Keep DRY_RUN = True to prevent any real orders.
- Run `auth_test.py` (provided separately) with your API credentials to verify authentication first.
- When ready, set DRY_RUN = False to place real orders. Use at your own risk.
- Create a file named STOP in the working directory to gracefully stop the bot.

SETUP:
- Python 3.12+, `pip install requests numpy`
- Fill API_KEY and API_SECRET placeholders below OR use environment variables:
  COINDCX_API_KEY / COINDCX_API_SECRET

NOTES:
- Spot trading only
- Uses exact signed compact JSON string for private endpoints (per CoinDCX spec)
"""

import os
import time
import json
import hmac
import hashlib
import random
import requests
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
import sys

# =====================================================================
# CONFIGURATION
# =====================================================================
API_KEY = ""  # set later, or use environment variable COINDCX_API_KEY
API_SECRET = ""  # set later, or use environment variable COINDCX_API_SECRET
BASE_URL = "https://api.coindcx.com"
PUBLIC_BASE_URL = "https://public.coindcx.com"
INTERVAL = "15m"
DRY_RUN = True  # True = no real trading, False = place orders
MAX_OPEN_TRADES = 3
GLOBAL_EXPOSURE_LIMIT = 0.40  # max 40% of INR balance in open holdings
SLEEP_BETWEEN_COINS_SEC = 1.5
FULL_CYCLE_SLEEP_SEC = 900  # 15 minutes
STOP_FILE = "STOP"
SHOW_DASHBOARD = True

# Watchlist of INR spot markets
WATCHLIST = [
    "BTCINR", "ETHINR", "SOLINR",
    "BNBINR", "XRPINR", "DOGEINR",
    "AVAXINR", "LINKINR", "DOTINR",
    "SUIINR", "NEARINR", "APTINR",
    "PEPEINR", "WIFINR",
]

# =====================================================================
# UTILS / LOGGING
# =====================================================================
def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str) -> None:
    print(f"[{now_ts()}] {msg}")

def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

def get_credentials() -> Tuple[str, str]:
    key = API_KEY or get_env("COINDCX_API_KEY", "")
    secret = API_SECRET or get_env("COINDCX_API_SECRET", "")
    return key, secret

# =====================================================================
# SIGNING / PRIVATE POST (exact compact JSON)
# =====================================================================
def sign_request(body: Dict[str, Any], secret: str) -> str:
    payload = json.dumps(body, separators=(",", ":"))
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

def authenticated_post(endpoint: str, body: Dict[str, Any]) -> Dict[str, Any]:
    key, secret = get_credentials()
    if not key or not secret:
        raise RuntimeError("Missing COINDCX_API_KEY / COINDCX_API_SECRET")
    body = dict(body)
    body["timestamp"] = int(time.time() * 1000)
    sig = sign_request(body, secret)
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": key,
        "X-AUTH-SIGNATURE": sig,
    }
    payload = json.dumps(body, separators=(",", ":"))
    r = requests.post(BASE_URL + endpoint, data=payload, headers=headers, timeout=15)
    if r.status_code >= 400:
        log(f"HTTP {r.status_code}: {r.text.strip()}")
        r.raise_for_status()
    return r.json()

# =====================================================================
# PUBLIC DATA HELPERS
# =====================================================================
def get_candles(pair: str, interval: str = INTERVAL, limit: int = 100) -> List[Any]:
    params = {"pair": pair, "interval": interval, "limit": limit}
    r = requests.get(PUBLIC_BASE_URL + "/market_data/candles", params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def extract_closes(candles: List[Any]) -> List[float]:
    closes: List[float] = []
    for c in candles:
        v = None
        if isinstance(c, dict):
            v = c.get("close")
        elif isinstance(c, (list, tuple)) and len(c) >= 5:
            v = c[4]
        if v is None:
            continue
        try:
            closes.append(float(v))
        except Exception:
            continue
    return closes

def get_market_details(market: str) -> Optional[Dict[str, Any]]:
    r = requests.get(BASE_URL + "/exchange/v1/markets_details", timeout=15)
    r.raise_for_status()
    markets = r.json()
    for m in markets:
        if m.get("coindcx_name") == market:
            return m
    return None

def market_to_public_pair(details: Dict[str, Any]) -> Optional[str]:
    return details.get("pair")

# =====================================================================
# AUTH / ACCOUNT HELPERS
# =====================================================================
def get_balances() -> List[Dict[str, Any]]:
    return authenticated_post("/exchange/v1/users/balances", {})

def get_active_orders(market: str) -> List[Dict[str, Any]]:
    res = authenticated_post("/exchange/v1/orders/active_orders", {"market": market})
    if isinstance(res, dict) and "orders" in res:
        return res.get("orders") or []
    if isinstance(res, list):
        return res
    return []

# =====================================================================
# INDICATORS
# =====================================================================
def calculate_ema(prices: List[float], period: int) -> Optional[List[float]]:
    if len(prices) < period:
        return None
    ema = float(np.mean(prices[:period]))
    multiplier = 2 / (period + 1)
    emas = [ema]
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
        emas.append(ema)
    return emas

def calculate_rsi(prices: List[float], period: int = 14) -> Optional[List[float]]:
    if len(prices) <= period:
        return None
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    rs = avg_gain / avg_loss if avg_loss != 0 else 0
    rsi_values = [100 - (100 / (1 + rs))]
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 0
        rsi_values.append(100 - (100 / (1 + rs)))
    return rsi_values

# =====================================================================
# ORDER HELPERS
# =====================================================================
def adjust_quantity(qty: float, min_qty: float, precision: int) -> float:
    step = 10 ** (-precision)
    adj = max(min_qty, round(qty - (qty % step), precision))
    return adj

def place_market_order(side: str, market: str, total_quantity: float) -> Dict[str, Any]:
    body = {
        "side": side,
        "order_type": "market_order",
        "market": market,
        "total_quantity": total_quantity,
    }
    if DRY_RUN:
        log(f"DRY_RUN: would place {side} market on {market} qty {total_quantity}")
        return {"status": "simulated", "orders": [{"avg_price": None}]}
    return authenticated_post("/exchange/v1/orders/create", body)

def place_stop_loss(market: str, qty: float, stop_price: float, limit_price: float) -> Dict[str, Any]:
    body = {
        "side": "sell",
        "order_type": "stop_limit",
        "market": market,
        "price_per_unit": limit_price,
        "total_quantity": qty,
        "stop_price": stop_price,
    }
    if DRY_RUN:
        log(f"DRY_RUN: would place stop-limit on {market} qty {qty} stop {stop_price:.2f} limit {limit_price:.2f}")
        return {"status": "simulated"}
    return authenticated_post("/exchange/v1/orders/create", body)

def color(text: str, fg: Optional[str] = None, bold: bool = False) -> str:
    codes = []
    if bold:
        codes.append("1")
    if fg == "red":
        codes.append("31")
    elif fg == "green":
        codes.append("32")
    elif fg == "yellow":
        codes.append("33")
    elif fg == "blue":
        codes.append("34")
    elif fg == "magenta":
        codes.append("35")
    elif fg == "cyan":
        codes.append("36")
    elif fg == "gray":
        codes.append("90")
    if not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"

def render_dashboard(rows: List[Dict[str, str]], exposure_pct: float, open_orders: int, next_time_str: str) -> None:
    headers = ["Market", "Price", "EMA12", "EMA26", "RSI", "Hold", "Signal", "Reason"]
    widths = [10, 16, 14, 14, 7, 6, 8, 52]
    total_width = sum(widths) + len(widths) + 1
    sys.stdout.write("\033[2J\033[H")
    title = " CoinDCX INR Day-Trading Bot • 15m • EMA+RSI "
    banner = "╔" + "═" * (total_width - 2) + "╗"
    print(color(banner, fg="cyan", bold=True))
    print(color("║" + title.center(total_width - 2) + "║", fg="cyan", bold=True))
    print(color("╠" + "═" * (total_width - 2) + "╣", fg="cyan", bold=True))
    header_line = "║"
    for h, w in zip(headers, widths):
        header_line += color(h.center(w), fg="gray", bold=True) + "│"
    header_line = header_line[:-1] + "║"
    print(header_line)
    print(color("╟" + "─" * (total_width - 2) + "╢", fg="gray"))
    for r in rows:
        try:
            rsi_val = float(r["rsi"])
        except Exception:
            rsi_val = 0.0
        rsi_col = "green" if rsi_val < 30 else ("red" if rsi_val > 70 else "yellow")
        sig_txt = r["sig"]
        line = "║"
        line += r["market"].ljust(widths[0]) + "│"
        line += r["price"].rjust(widths[1]) + "│"
        line += r["ema12"].rjust(widths[2]) + "│"
        line += r["ema26"].rjust(widths[3]) + "│"
        line += color(r["rsi"].rjust(widths[4]), fg=rsi_col, bold=True) + "│"
        line += (color(r["hold"].center(widths[5]), fg="green", bold=True) if r["hold"] == "Y" else color(r["hold"].center(widths[5]), fg="red", bold=True)) + "│"
        line += sig_txt.center(widths[6]) + "│"
        line += r["reason"][:widths[7]].ljust(widths[7]) + "║"
        print(line)
    print(color("╠" + "═" * (total_width - 2) + "╣", fg="cyan", bold=True))
    status = f"Open orders: {open_orders} • Exposure: {exposure_pct*100:.1f}% • Next: {next_time_str}"
    status_color = "green" if exposure_pct <= GLOBAL_EXPOSURE_LIMIT else "red"
    print(color("║" + status.center(total_width - 2) + "║", fg=status_color, bold=True))
    print(color("╚" + "═" * (total_width - 2) + "╝", fg="cyan", bold=True))

# =====================================================================
# MAIN LOOP
# =====================================================================
def run():
    log("Starting CoinDCX 15m EMA+RSI Day-Trading Bot (SPOT, INR)")
    try:
        _ = get_balances()
        log("Auth OK")
    except Exception as e:
        log(f"Auth failed: {e}")
        return

    while True:
        try:
            if os.path.exists(STOP_FILE):
                log("STOP file found; exiting gracefully")
                break

            balances = get_balances()
            inr_balance = float(next((b["balance"] for b in balances if b["currency"] == "INR"), 0.0))
            currency_bal = {b["currency"]: float(b["balance"]) for b in balances}

            total_open_orders = 0
            total_holding_value_inr = 0.0
            table_rows: List[Dict[str, str]] = []

            log(f"Account INR balance ₹{inr_balance:,.2f}")

            for market in WATCHLIST:
                time.sleep(SLEEP_BETWEEN_COINS_SEC)
                details = get_market_details(market)
                if not details:
                    log(f"{market}: market details not found; skipping")
                    continue

                min_qty = float(details.get("min_quantity", 0.0))
                target_precision = int(details.get("target_currency_precision", 6))
                public_pair = market_to_public_pair(details)
                if not public_pair:
                    log(f"{market}: public pair not available; skipping")
                    continue
                step_qty = 10 ** (-target_precision)
                effective_min_qty = max(min_qty, step_qty)

                active_orders = get_active_orders(market)
                total_open_orders += len(active_orders)

                candles = get_candles(public_pair, INTERVAL, limit=100)
                closes = extract_closes(candles)
                if len(closes) < 40:
                    log(f"{market}: insufficient candle data ({len(closes)})")
                    continue
                current_price = float(closes[-1])
                if current_price <= 0:
                    log(f"{market}: invalid current price ({current_price}) → skipping")
                    continue

                symbol = market.replace("INR", "")
                holding_qty = currency_bal.get(symbol, 0.0)
                total_holding_value_inr += holding_qty * current_price

                ema12_series = calculate_ema(closes, 12)
                ema26_series = calculate_ema(closes, 26)
                rsi_series = calculate_rsi(closes, 14)
                if not ema12_series or not ema26_series or not rsi_series:
                    log(f"{market}: indicator series not available")
                    continue

                ema12 = float(ema12_series[-1])
                ema26 = float(ema26_series[-1])
                rsi14 = float(rsi_series[-1])

                prev_ema12 = float(ema12_series[-2]) if len(ema12_series) >= 2 else ema12
                prev_ema26 = float(ema26_series[-2]) if len(ema26_series) >= 2 else ema26

                not_holding = holding_qty < effective_min_qty
                buy_signal = (prev_ema12 <= prev_ema26 and ema12 > ema26) and (rsi14 < 30) and not_holding
                sell_signal = (prev_ema12 >= prev_ema26 and ema12 < ema26) and (rsi14 > 70) and (holding_qty >= effective_min_qty)

                log(f"{market}: price ₹{current_price:,.2f} | EMA12 {ema12:.2f} / EMA26 {ema26:.2f} | RSI {rsi14:.1f} | holding {holding_qty:.8f}")

                if buy_signal:
                    if total_open_orders >= MAX_OPEN_TRADES:
                        log(f"{market}: max open trades reached ({total_open_orders}) → skip buy")
                        continue
                    if total_holding_value_inr > GLOBAL_EXPOSURE_LIMIT * inr_balance:
                        log(f"{market}: exposure {total_holding_value_inr:.2f} > {GLOBAL_EXPOSURE_LIMIT*100:.0f}% of INR {inr_balance:.2f} → skip buy")
                        continue

                    percent = random.uniform(0.05, 0.10)
                    inr_to_use = inr_balance * percent
                    qty = inr_to_use / current_price
                    qty = adjust_quantity(qty, min_qty, target_precision)

                    log(f"{market}: BUY signal → using {percent*100:.1f}% (₹{inr_to_use:,.2f}) → qty {qty:.8f}")
                    buy_res = place_market_order("buy", market, qty)

                    if not DRY_RUN and buy_res.get("status") == "filled":
                        try:
                            entry_price = float(buy_res["orders"][0]["avg_price"])
                            stop_price = entry_price * 0.98
                            limit_price = stop_price * 0.99
                            _ = place_stop_loss(market, qty, stop_price, limit_price)
                            log(f"{market}: stop-loss placed at ₹{stop_price:.2f}")
                        except Exception as e:
                            log(f"{market}: failed to place stop-loss → {e}")

                elif sell_signal:
                    qty = adjust_quantity(holding_qty, effective_min_qty, target_precision)
                    if qty < effective_min_qty:
                        log(f"{market}: sell qty below min ({qty:.8f} < {effective_min_qty:.8f}) → skip")
                        continue
                    log(f"{market}: SELL signal → qty {qty:.8f}")
                    _ = place_market_order("sell", market, qty)
                else:
                    log(f"{market}: no trade | up {(prev_ema12 <= prev_ema26) and (ema12 > ema26)} | down {(prev_ema12 >= prev_ema26) and (ema12 < ema26)} | rsi<30 {rsi14 < 30} | rsi>70 {rsi14 > 70} | holding {holding_qty >= effective_min_qty}")
                sig = "-"
                sig_col = None
                if buy_signal:
                    sig = "BUY"
                    sig_col = "green"
                elif sell_signal:
                    sig = "SELL"
                    sig_col = "red"
                up_ok = (prev_ema12 <= prev_ema26) and (ema12 > ema26)
                dn_ok = (prev_ema12 >= prev_ema26) and (ema12 < ema26)
                r_low = (rsi14 < 30)
                r_high = (rsi14 > 70)
                def mark(ok: bool) -> str:
                    return color("✓", fg="green", bold=True) if ok else color("✗", fg="gray")
                reason = f"↑ {mark(up_ok)} | ↓ {mark(dn_ok)} | RSI<30 {mark(r_low)} | RSI>70 {mark(r_high)}"
                row = {
                    "market": market,
                    "price": f"{current_price:,.2f}",
                    "ema12": f"{ema12:.2f}",
                    "ema26": f"{ema26:.2f}",
                    "rsi": f"{rsi14:.1f}",
                    "hold": "Y" if holding_qty >= effective_min_qty else "N",
                    "sig": color(sig, fg=sig_col, bold=True) if sig_col else sig,
                    "reason": reason,
                }
                table_rows.append(row)

            next_at = time.localtime(time.time() + FULL_CYCLE_SLEEP_SEC)
            next_str = time.strftime("%H:%M:%S", next_at)
            if SHOW_DASHBOARD:
                render_dashboard(table_rows, (total_holding_value_inr / inr_balance) if inr_balance > 0 else 0.0, total_open_orders, next_str)
            log(f"Full cycle done. Sleeping 15m → next at {next_str}")
            time.sleep(FULL_CYCLE_SLEEP_SEC)

        except KeyboardInterrupt:
            log("Interrupted by user; exiting")
            break
        except Exception as e:
            log(f"Error in main loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run()
