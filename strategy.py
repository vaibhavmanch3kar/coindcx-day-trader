"""
Indicators and trading signals. Pure functions, shared by the live bot and
the backtester so both always use exactly the same logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

INTERVAL_MS = {"1m": 60_000, "15m": 900_000, "1h": 3_600_000, "1d": 86_400_000}


# ── Candles ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Candle:
    time: int      # open time, ms
    open: float
    high: float
    low: float
    close: float
    volume: float


def normalize_candles(raw: Sequence[Dict[str, Any]], interval: str, now_ms: int) -> List[Candle]:
    """
    CoinDCX returns candles NEWEST FIRST and includes the candle that is still
    forming. Return closed candles only, oldest first, de-duplicated by time.
    """
    step = INTERVAL_MS[interval]
    by_time: Dict[int, Candle] = {}
    for c in raw:
        try:
            t = int(c["time"])
            candle = Candle(t, float(c["open"]), float(c["high"]), float(c["low"]),
                            float(c["close"]), float(c.get("volume", 0) or 0))
        except (KeyError, TypeError, ValueError):
            continue
        if t + step <= now_ms:          # closed candles only
            by_time[t] = candle
    return [by_time[t] for t in sorted(by_time)]


# ── Indicators (each returns a list aligned to the input, None until ready) ──
def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period           # SMA seed
    out[period - 1] = e
    for i in range(period, len(values)):
        e = (values[i] - e) * k + e
        out[i] = e
    return out


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0   # v1 returned 0 here (wrong)
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """Wilder's RSI."""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out[period] = _rsi_from_avgs(avg_g, avg_l)
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from_avgs(avg_g, avg_l)
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    """Wilder's Average True Range."""
    out: List[Optional[float]] = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    a = sum(trs[:period]) / period
    out[period] = a
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
        out[i + 1] = a
    return out


# ── Signals ────────────────────────────────────────────────────────────────
@dataclass
class Indicators:
    close: float
    ema_fast: float
    ema_slow: float
    ema_trend: float
    rsi: float
    atr: float
    prev_ema_fast: float
    prev_ema_slow: float

    @property
    def bull_cross(self) -> bool:
        return self.prev_ema_fast <= self.prev_ema_slow and self.ema_fast > self.ema_slow

    @property
    def bear_cross(self) -> bool:
        return self.prev_ema_fast >= self.prev_ema_slow and self.ema_fast < self.ema_slow


def compute_indicators(candles: Sequence[Candle], cfg) -> List[Optional[Indicators]]:
    """Indicators for every candle index (None where not enough history)."""
    closes = [c.close for c in candles]
    ef, es, et = ema(closes, cfg.EMA_FAST), ema(closes, cfg.EMA_SLOW), ema(closes, cfg.EMA_TREND)
    r, a = rsi(closes, cfg.RSI_PERIOD), atr(candles, cfg.ATR_PERIOD)
    out: List[Optional[Indicators]] = [None] * len(candles)
    for i in range(1, len(candles)):
        vals = (ef[i], es[i], et[i], r[i], a[i], ef[i - 1], es[i - 1])
        if any(v is None for v in vals):
            continue
        out[i] = Indicators(closes[i], ef[i], es[i], et[i], r[i], a[i], ef[i - 1], es[i - 1])
    return out


@dataclass
class EntrySignal:
    entry_ref: float   # reference price (last close); live entry uses the ask
    stop: float
    target: float
    reason: str


def entry_signal(ind: Optional[Indicators], cfg) -> Optional[EntrySignal]:
    """
    Long entry when ALL are true on the last CLOSED candle:
      - EMA fast crosses above EMA slow
      - close is above the trend EMA
      - RSI between RSI_MIN and RSI_MAX (momentum, not overbought)
    Stop = close - ATR_STOP_MULT x ATR; target = close + TARGET_R x risk.
    Skipped if the target is too small to pay for round-trip fees.
    """
    if ind is None or not ind.bull_cross:
        return None
    if ind.close <= ind.ema_trend:
        return None
    if not (cfg.RSI_MIN <= ind.rsi <= cfg.RSI_MAX):
        return None
    risk = cfg.ATR_STOP_MULT * ind.atr
    if risk <= 0:
        return None
    stop = ind.close - risk
    target = ind.close + cfg.TARGET_R * risk
    target_pct = (target - ind.close) / ind.close * 100
    if target_pct < cfg.MIN_TARGET_FEE_MULT * 2 * cfg.FEE_PCT:
        return None
    return EntrySignal(ind.close, stop, target,
                       f"bull cross, close>EMA{cfg.EMA_TREND}, RSI {ind.rsi:.1f}")


def exit_on_signal(ind: Optional[Indicators], cfg) -> bool:
    return bool(cfg.EXIT_ON_BEAR_CROSS and ind is not None and ind.bear_cross)


def describe(ind: Optional[Indicators], cfg) -> str:
    """Short reason text for the dashboard."""
    if ind is None:
        return "warming up"
    parts = [
        ("cross↑" if ind.bull_cross else ("cross↓" if ind.bear_cross else "no cross")),
        ("trend✓" if ind.close > ind.ema_trend else "trend✗"),
        ("RSI✓" if cfg.RSI_MIN <= ind.rsi <= cfg.RSI_MAX else "RSI✗"),
    ]
    return " | ".join(parts)
