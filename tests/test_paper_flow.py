"""End-to-end paper trade with a fake exchange: entry → stop exit → P&L, streak, CSV."""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy as st  # noqa: E402


class FakeClient:
    def __init__(self):
        self.bid, self.ask = 100.0, 100.1

    def tickers(self):
        return {"TESTINR": {"market": "TESTINR", "bid": str(self.bid), "ask": str(self.ask),
                            "last_price": str(self.bid)}}

    def markets(self):
        return {"TESTINR": {"coindcx_name": "TESTINR", "pair": "I-TEST_INR", "status": "active",
                            "min_quantity": 0.01, "target_currency_precision": 2,
                            "base_currency_precision": 2}}


def test_paper_entry_then_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHOW_DASHBOARD", "false")
    import config
    importlib.reload(config)
    import day_trading_bot as bot_mod
    importlib.reload(bot_mod)

    bot = bot_mod.Bot()
    bot.client = FakeClient()
    bot.refresh_tickers()
    sig = st.EntrySignal(entry_ref=100.0, stop=96.0, target=108.0, reason="test")
    bot.try_enter("TESTINR", bot.client.markets()["TESTINR"], sig, 100000, 100000, 0)

    pos = bot.state.positions["TESTINR"]
    assert abs(pos.entry - 100.1) < 1e-9 and abs(pos.stop - 96.1) < 1e-9
    assert pos.qty * pos.entry <= 100000 * config.MAX_POSITION_PCT / 100 + 1
    cash_after_buy = bot.state.paper_cash

    bot.client.bid = 96.0                       # price falls through the stop
    bot.check_stops()
    assert "TESTINR" not in bot.state.positions
    assert bot.state.realized_pnl_total < 0
    assert bot.state.loss_streak == 1
    loss = -bot.state.realized_pnl_total
    assert loss <= 100000 * config.RISK_PER_TRADE_PCT / 100 * 1.05      # ≈ planned 0.5% risk
    assert bot.state.paper_cash > cash_after_buy
    rows = open(os.path.join(tmp_path, "trades_paper.csv")).read().strip().splitlines()
    assert len(rows) == 3 and "STOP" in rows[-1]                          # header + buy + sell
    assert os.path.exists(os.path.join(tmp_path, "state_paper.json"))


def _bot(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHOW_DASHBOARD", "false")
    import config
    importlib.reload(config)
    import day_trading_bot as bot_mod
    importlib.reload(bot_mod)
    bot = bot_mod.Bot()
    bot.client = FakeClient()
    return bot


def test_wide_spread_is_skipped(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch)
    bot.client.bid, bot.client.ask = 80.10, 80.562          # 0.57% spread, as seen live on DOTINR
    bot.refresh_tickers()
    sig = st.EntrySignal(entry_ref=80.468, stop=80.32, target=80.62, reason="test")
    assert not bot.try_enter("TESTINR", bot.client.markets()["TESTINR"], sig, 100000, 100000, 0)
    assert not bot.state.positions


def test_bid_already_below_stop_is_skipped(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch)
    bot.client.bid, bot.client.ask = 100.00, 100.20          # 0.2% spread passes the spread check
    bot.refresh_tickers()
    sig = st.EntrySignal(entry_ref=100.0, stop=99.90, target=100.2, reason="test")   # stop 0.1 below
    assert not bot.try_enter("TESTINR", bot.client.markets()["TESTINR"], sig, 100000, 100000, 0)
