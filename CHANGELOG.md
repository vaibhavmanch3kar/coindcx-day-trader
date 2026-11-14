# Changelog

## v2.0.0 — 2026-11-14

v1 is preserved in `legacy/day_trading_bot_v1.py` and on the `backup/v1-legacy` branch.

### Fixed (v1 bugs, checked against live CoinDCX data)
- **Candles were read backwards.** CoinDCX returns candles newest first; v1 treated the oldest
  candle (~25 h old) as the current price and ran EMA/RSI in reverse. Candles are now sorted
  oldest-first and the still-forming candle is dropped.
- **Entry rules could never trigger.** v1 required an EMA bullish crossover *and* RSI < 30 at the
  same time. On 10 days × 14 markets there were 318 crossovers and 0 with RSI < 30 (median RSI at a
  crossover ≈ 55). New entry: crossover + close above EMA200 + RSI 50–70.
- **Stop-loss was never placed.** v1 checked a top-level `status == "filled"` that CoinDCX does not
  return. Stop orders are also not offered on INR markets. Stops and targets are now checked by the
  bot every 30 s and exited with limit orders.
- **Market orders on markets that don't support them** (e.g. WIFINR, AVAXINR). All orders are now
  aggressive limit orders with partial-fill handling and cancel on timeout.
- **RSI was 0 when there were no losing candles** (should be 100; 50 when flat).
- **Exposure and open-trade caps were counted mid-loop**, so early markets saw an almost empty total.
- **Quantities could round UP to the market minimum**; now rounds down and checks the ₹100 minimum order.
- **Locked balances were ignored**; holdings now include `locked_balance`.
- `markets_details` (~0.5 MB) was downloaded 14× per cycle; now cached.

### Added
- Risk-based sizing (0.5% of equity per trade, fees included), per-coin and total exposure caps,
  daily loss limit, max trades per day, 3-losses → 3-days-off cooldown, no adding to positions.
- Paper mode that works without API keys and tracks positions, P&L, fees and TDS.
- Live mode requires `DRY_RUN=false` **and** `CONFIRM_LIVE=YES`.
- Persistent state (`data/state_*.json`), trade log CSV, file logging.
- Startup reconciliation: positions sold manually are dropped; holdings not opened by the bot are never touched.
- Signals evaluated right after each candle closes (no drift).
- `backtest.py` portfolio backtester using the same strategy and risk code.
- Unit tests (`pytest`).
- `auth_test.py` masks personal details.
