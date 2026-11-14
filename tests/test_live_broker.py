"""LiveBroker against a fake client: partial fills, cancel on timeout, never oversell."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import broker as br  # noqa: E402
import config as cfg  # noqa: E402
from risk import RiskState  # noqa: E402

DETAILS = {"min_quantity": 0.01, "target_currency_precision": 2, "base_currency_precision": 2}


class FakeLive:
    def __init__(self, fill_ratio=1.0, held=10.0):
        self.fill_ratio, self.held = fill_ratio, held
        self.orders, self.cancelled = {}, []

    def create_limit_order(self, side, market, price, qty, cid):
        oid = str(len(self.orders) + 1)
        self.orders[oid] = {"side": side, "price": price, "qty": qty}
        return {"id": oid, "status": "init"}

    def order_status(self, order_id="", client_order_id=""):
        o = self.orders[order_id]
        filled = round(o["qty"] * self.fill_ratio, 2)
        done = self.fill_ratio >= 1 or order_id in self.cancelled
        status = "filled" if self.fill_ratio >= 1 else ("partially_cancelled" if done else "partially_filled")
        if done and o["side"] == "sell" and not o.get("settled"):
            self.held -= filled                      # exchange balance drops after a sell
            o["settled"] = True
        return {"id": order_id, "status": status, "total_quantity": o["qty"],
                "remaining_quantity": o["qty"] - filled, "avg_price": o["price"],
                "fee_amount": filled * o["price"] * 0.0059}

    def cancel_order(self, order_id="", client_order_id=""):
        self.cancelled.append(order_id)
        return {"status": "success"}

    def balances(self):
        return {"TEST": {"balance": self.held, "locked": 0.0, "total": self.held}}


def make(client):
    c = SimpleNamespace(**{**vars(cfg), "ORDER_FILL_TIMEOUT_SEC": 0, "EXIT_RETRIES": 3})
    b = br.LiveBroker(client, RiskState(), c, lambda m: None)
    br.time.sleep = lambda s: None                     # don't actually wait in tests
    return b


def test_full_fill_buy_uses_limit_above_ask():
    client = FakeLive()
    fill = make(client).buy("TESTINR", 2.0, {"ask": "100"}, DETAILS)
    assert fill.qty == 2.0 and fill.avg_price == round(100 * (1 + cfg.SLIPPAGE_PCT / 100), 2)


def test_partial_fill_is_cancelled_and_reported():
    client = FakeLive(fill_ratio=0.5)
    fill = make(client).buy("TESTINR", 2.0, {"ask": "100"}, DETAILS)
    assert client.cancelled and fill.qty == 1.0


def test_sell_never_exceeds_holding():
    client = FakeLive(held=1.5)
    fill = make(client).sell("TESTINR", 2.0, lambda: {"TESTINR": {"bid": "100"}}, DETAILS)
    assert sum(o["qty"] for o in client.orders.values()) <= 1.5
    assert fill.qty == 1.5


def test_partial_sell_retries_until_done():
    client = FakeLive(fill_ratio=0.5, held=4.0)
    fill = make(client).sell("TESTINR", 4.0, lambda: {"TESTINR": {"bid": "100"}}, DETAILS)
    assert len(client.orders) == 3 and abs(fill.qty - 3.5) < 1e-9     # 2 + 1 + 0.5 over 3 tries
