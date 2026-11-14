import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg  # noqa: E402
import strategy as st  # noqa: E402
from risk import (Position, RiskState, account_gate, floor_to_precision,  # noqa: E402
                  market_gate, record_close, roll_day, size_position)

STEP = st.INTERVAL_MS["15m"]


def raw(t, close, high=None, low=None):
    return {"time": t, "open": close, "high": high or close, "low": low or close,
            "close": close, "volume": 1}


# ── candles ────────────────────────────────────────────────────────────────
def test_normalize_sorts_oldest_first_and_drops_forming_candle():
    now = 10 * STEP + 5
    api = [raw(10 * STEP, 103), raw(9 * STEP, 102), raw(8 * STEP, 101)]   # newest first, like CoinDCX
    out = st.normalize_candles(api, "15m", now)
    assert [c.time for c in out] == [8 * STEP, 9 * STEP]                   # 10*STEP still forming
    assert [c.close for c in out] == [101, 102]


def test_normalize_dedupes_and_skips_bad_rows():
    api = [raw(STEP, 1), raw(STEP, 1), {"time": "x"}, raw(2 * STEP, 2)]
    out = st.normalize_candles(api, "15m", 10 * STEP)
    assert [c.time for c in out] == [STEP, 2 * STEP]


# ── indicators ─────────────────────────────────────────────────────────────
def test_ema_constant_series_equals_constant():
    e = st.ema([5.0] * 30, 12)
    assert e[10] is None and abs(e[-1] - 5.0) < 1e-12


def test_rsi_edge_cases():
    assert st.rsi([float(i) for i in range(30)], 14)[-1] == 100.0     # only gains
    assert st.rsi([10.0] * 30, 14)[-1] == 50.0                         # flat (v1 gave 0)
    assert st.rsi([float(30 - i) for i in range(30)], 14)[-1] == 0.0   # only losses


def test_atr_simple():
    cs = [st.Candle(i, 10, 11, 9, 10, 1) for i in range(30)]
    assert abs(st.atr(cs, 14)[-1] - 2.0) < 1e-12


# ── signals ────────────────────────────────────────────────────────────────
def _ind(**kw):
    base = dict(close=110, ema_fast=101, ema_slow=100, ema_trend=90, rsi=60, atr=2,
                prev_ema_fast=99, prev_ema_slow=100)
    base.update(kw)
    return st.Indicators(**base)


def test_entry_signal_requires_all_conditions():
    assert st.entry_signal(_ind(), cfg) is not None
    assert st.entry_signal(_ind(prev_ema_fast=101), cfg) is None        # no fresh cross
    assert st.entry_signal(_ind(ema_trend=120), cfg) is None            # below trend
    assert st.entry_signal(_ind(rsi=25), cfg) is None                   # RSI too low
    assert st.entry_signal(_ind(rsi=80), cfg) is None                   # RSI too high


def test_entry_levels_and_fee_filter():
    s = st.entry_signal(_ind(), cfg)
    assert abs(s.stop - (110 - cfg.ATR_STOP_MULT * 2)) < 1e-9
    assert abs(s.target - (110 + cfg.TARGET_R * cfg.ATR_STOP_MULT * 2)) < 1e-9
    assert st.entry_signal(_ind(atr=0.05), cfg) is None                 # target can't pay fees


def test_bear_cross_exit():
    assert st.exit_on_signal(_ind(prev_ema_fast=101, ema_fast=99), cfg)
    assert not st.exit_on_signal(_ind(), cfg)


# ── sizing ─────────────────────────────────────────────────────────────────
def test_floor_never_rounds_up():
    assert floor_to_precision(0.123456789, 4) == 0.1234
    assert floor_to_precision(1.0, 2) == 1.0


def test_size_position_risk_including_fees():
    c = SimpleNamespace(**{**vars(cfg), "MAX_POSITION_PCT": 100.0})
    qty, _ = size_position(100000, 100000, entry=100, stop=98, cfg=c, min_qty=0, qty_precision=6)
    fee = c.FEE_PCT / 100
    loss = qty * ((100 - 98) + 100 * fee + 98 * fee)
    assert abs(loss - 100000 * c.RISK_PER_TRADE_PCT / 100) < 1                # ~0.5% of equity


def test_size_position_caps_and_skips():
    qty, note = size_position(100000, 100000, entry=100, stop=99.99, cfg=cfg, min_qty=0, qty_precision=6)
    assert qty * 100 <= 100000 * cfg.MAX_POSITION_PCT / 100 + 1e-6 and "capped" in note
    qty, note = size_position(100000, 50, entry=100, stop=98, cfg=cfg, min_qty=0, qty_precision=6)
    assert qty == 0 and "minimum" in note                                     # below ₹100
    qty, note = size_position(100000, 100000, entry=100, stop=98, cfg=cfg, min_qty=10_000, qty_precision=0)
    assert qty == 0                                                           # never rounds UP to min


# ── gates ──────────────────────────────────────────────────────────────────
def test_loss_streak_cooldown():
    s = RiskState()
    d = date(2026, 1, 1)
    for _ in range(cfg.LOSS_STREAK_LIMIT):
        record_close(s, -100, d, cfg)
    ok, why = account_gate(s, date(2026, 1, 2), cfg)
    assert not ok and "cooldown" in why
    ok, _ = account_gate(s, date(2026, 1, 1 + cfg.COOLDOWN_DAYS + 1), cfg)
    assert ok


def test_win_resets_streak():
    s = RiskState()
    record_close(s, -1, date(2026, 1, 1), cfg)
    record_close(s, +1, date(2026, 1, 1), cfg)
    assert s.loss_streak == 0


def test_daily_loss_and_trade_count_limits():
    s = RiskState()
    roll_day(s, date(2026, 1, 1), 100000)
    s.realized_pnl_today = -100000 * cfg.MAX_DAILY_LOSS_PCT / 100
    assert not account_gate(s, date(2026, 1, 1), cfg)[0]
    roll_day(s, date(2026, 1, 2), 100000)                                     # new day resets
    assert account_gate(s, date(2026, 1, 2), cfg)[0]
    s.trades_today = cfg.MAX_TRADES_PER_DAY
    assert not account_gate(s, date(2026, 1, 2), cfg)[0]


def test_market_gate_no_adding_and_exposure():
    s = RiskState(positions={"BTCINR": Position("BTCINR", 1, 1, 0.9, 1.2, "")})
    assert not market_gate(s, "BTCINR", 100000, 0, 10, cfg)[0]
    assert not market_gate(s, "ETHINR", 100000, 39000, 2000, cfg)[0]          # > 40%
    assert market_gate(s, "ETHINR", 100000, 10000, 2000, cfg)[0]


def test_state_roundtrip(tmp_path):
    p = str(tmp_path / "s.json")
    s = RiskState(paper_cash=5.0, positions={"X": Position("X", 1, 2, 1, 3, "t")})
    s.save(p)
    s2 = RiskState.load(p)
    assert s2.paper_cash == 5.0 and s2.positions["X"].entry == 2
