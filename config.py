"""
Settings for the CoinDCX INR spot bot (v2).

Every value can be overridden with an environment variable of the same name,
e.g.  export RISK_PER_TRADE_PCT=0.25
"""

import os


def _env(name: str, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "y", "on")
    if cast is list:
        return [x.strip().upper() for x in raw.split(",") if x.strip()]
    return cast(raw)


# ── API ────────────────────────────────────────────────────────────────────
API_KEY = _env("COINDCX_API_KEY", "")
API_SECRET = _env("COINDCX_API_SECRET", "")
BASE_URL = _env("COINDCX_BASE_URL", "https://api.coindcx.com")
PUBLIC_BASE_URL = _env("COINDCX_PUBLIC_BASE_URL", "https://public.coindcx.com")
HTTP_TIMEOUT_SEC = _env("HTTP_TIMEOUT_SEC", 15, int)

# ── Mode ───────────────────────────────────────────────────────────────────
# Paper trading is the default. Live trading needs BOTH:
#   DRY_RUN=false  and  CONFIRM_LIVE=YES
DRY_RUN = _env("DRY_RUN", True, bool)
CONFIRM_LIVE = _env("CONFIRM_LIVE", "")
PAPER_START_INR = _env("PAPER_START_INR", 100000.0, float)

# ── Markets & timeframe ────────────────────────────────────────────────────
INTERVAL = _env("INTERVAL", "15m")          # CoinDCX supports 1m, 15m, 1h, 1d
CANDLE_LIMIT = _env("CANDLE_LIMIT", 400, int)  # max 1000; needs > EMA_TREND + 50
WATCHLIST = _env("WATCHLIST", [
    "BTCINR", "ETHINR", "SOLINR", "BNBINR", "XRPINR", "DOGEINR",
    "AVAXINR", "LINKINR", "DOTINR", "SUIINR", "NEARINR", "APTINR",
    "PEPEINR", "WIFINR",
], list)

# ── Strategy (long-only spot) ──────────────────────────────────────────────
EMA_FAST = _env("EMA_FAST", 12, int)
EMA_SLOW = _env("EMA_SLOW", 26, int)
EMA_TREND = _env("EMA_TREND", 200, int)      # only buy above this trend EMA
RSI_PERIOD = _env("RSI_PERIOD", 14, int)
RSI_MIN = _env("RSI_MIN", 50.0, float)       # momentum confirmation...
RSI_MAX = _env("RSI_MAX", 70.0, float)       # ...but not already overbought
ATR_PERIOD = _env("ATR_PERIOD", 14, int)
ATR_STOP_MULT = _env("ATR_STOP_MULT", 1.5, float)   # stop = entry - 1.5 x ATR
TARGET_R = _env("TARGET_R", 2.0, float)             # target = entry + 2 x risk
EXIT_ON_BEAR_CROSS = _env("EXIT_ON_BEAR_CROSS", True, bool)

# ── Costs ──────────────────────────────────────────────────────────────────
# Fee per side in %, incl. GST. 0.59 matches CoinDCX's INR example in its API
# docs; check https://coindcx.com/fees for your own tier.
FEE_PCT = _env("FEE_PCT", 0.59, float)
SLIPPAGE_PCT = _env("SLIPPAGE_PCT", 0.10, float)    # limit price offset past bid/ask
# Skip entries when the bid/ask spread is wider than this (thin INR books can be 0.5%+),
# or when the bid is already at/below the stop the moment we would buy.
MAX_SPREAD_PCT = _env("MAX_SPREAD_PCT", 0.30, float)
# Skip trades whose target is too small to pay for fees:
# target distance must be at least MIN_TARGET_FEE_MULT x round-trip fee.
MIN_TARGET_FEE_MULT = _env("MIN_TARGET_FEE_MULT", 3.0, float)
# India deducts 1% TDS on crypto sells. It is a tax credit, not a fee, but it
# reduces the INR you get back, so the bot reports it separately.
TDS_PCT = _env("TDS_PCT", 1.0, float)

# ── Risk rules ─────────────────────────────────────────────────────────────
RISK_PER_TRADE_PCT = _env("RISK_PER_TRADE_PCT", 0.5, float)  # % of equity lost if stop hits
MAX_POSITION_PCT = _env("MAX_POSITION_PCT", 20.0, float)     # max % of equity in one coin
GLOBAL_EXPOSURE_PCT = _env("GLOBAL_EXPOSURE_PCT", 40.0, float)  # max % of equity in all coins
MAX_OPEN_POSITIONS = _env("MAX_OPEN_POSITIONS", 3, int)
MAX_TRADES_PER_DAY = _env("MAX_TRADES_PER_DAY", 6, int)
MAX_DAILY_LOSS_PCT = _env("MAX_DAILY_LOSS_PCT", 2.0, float)  # halt for the day after this
LOSS_STREAK_LIMIT = _env("LOSS_STREAK_LIMIT", 3, int)        # N losses in a row...
COOLDOWN_DAYS = _env("COOLDOWN_DAYS", 3, int)                # ...= N days off
MIN_NOTIONAL_INR = _env("MIN_NOTIONAL_INR", 100.0, float)    # CoinDCX INR minimum

# ── Loop timing ────────────────────────────────────────────────────────────
MONITOR_INTERVAL_SEC = _env("MONITOR_INTERVAL_SEC", 30, int)  # stop/target checks
CANDLE_CLOSE_DELAY_SEC = _env("CANDLE_CLOSE_DELAY_SEC", 15, int)
ORDER_FILL_TIMEOUT_SEC = _env("ORDER_FILL_TIMEOUT_SEC", 30, int)
EXIT_RETRIES = _env("EXIT_RETRIES", 5, int)

# ── Files ──────────────────────────────────────────────────────────────────
DATA_DIR = _env("DATA_DIR", "data")
STOP_FILE = _env("STOP_FILE", "STOP")
SHOW_DASHBOARD = _env("SHOW_DASHBOARD", True, bool)


def is_live() -> bool:
    return (not DRY_RUN) and CONFIRM_LIVE == "YES"
