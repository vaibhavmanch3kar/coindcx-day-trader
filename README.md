# CoinDCX INR Day-Trading Bot (EMA+RSI, 15m)

Production-ready Python bot for Spot INR markets on CoinDCX. Uses EMA(12/26) crossover + RSI(14), market orders, stop-loss, and clear CLI dashboard.

## Features
- Proper HMAC-SHA256 signing (exact compact JSON) for private endpoints on `https://api.coindcx.com`
- Public candles from `https://public.coindcx.com/market_data/candles`
- Multi-market WATCHLIST (BTCINR, ETHINR, SOLINR, etc.)
- Risk controls: 5–10% INR allocation, max 3 open trades, global exposure cap, stop-loss after buys
- Professional terminal dashboard with color-coded signals
- DRY_RUN mode for safe testing
- STOP file for graceful shutdown

## Requirements
- Python 3.12+
- `pip install -r requirements.txt`
- CoinDCX API key and secret

## Quick Start (Safe Test)
1. Create venv and install deps:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
2. Verify credentials:
   ```bash
   export COINDCX_API_KEY='YOUR_KEY'
   export COINDCX_API_SECRET='YOUR_SECRET'
   python auth_test.py
   ```
   Expect `HTTP 200` and your user info. If `401`, fix key/secret/IP whitelist/time.
3. Run bot (dry run):
   ```bash
   python day_trading_bot.py
   ```
   - DRY_RUN defaults to True in this folder version. The bot will not place real orders.
   - A color dashboard shows markets, indicators, signals, exposure and next cycle time.
4. Stop safely:
   ```bash
   touch STOP
   ```

## Going Live
- Open `day_trading_bot.py` and set `DRY_RUN = False`
- Keep exposure conservative initially (e.g., lower GLOBAL_EXPOSURE_LIMIT to 0.20)
- Start with 1–2 markets and observe behavior

## Run Persistently
### tmux
```bash
sudo apt install tmux -y
tmux
source venv/bin/activate
export COINDCX_API_KEY='YOUR_KEY'
export COINDCX_API_SECRET='YOUR_SECRET'
python day_trading_bot.py
```
- Detach: Ctrl+b, then d
- Reattach: `tmux attach`

### systemd (optional)
Create `/etc/coindcx-bot.env`:
```bash
sudo bash -c 'cat >/etc/coindcx-bot.env <<EOF
COINDCX_API_KEY=YOUR_KEY
COINDCX_API_SECRET=YOUR_SECRET
COINDCX_PUBLIC_BASE_URL=https://public.coindcx.com
EOF'
sudo chmod 600 /etc/coindcx-bot.env
```
Service unit `/etc/systemd/system/coindcx-bot.service`:
```bash
sudo bash -c 'cat >/etc/systemd/system/coindcx-bot.service <<EOF
[Unit]
Description=CoinDCX INR Day-Trading Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/coindcx-day-trader
EnvironmentFile=/etc/coindcx-bot.env
ExecStart=/root/coindcx-day-trader/venv/bin/python /root/coindcx-day-trader/day_trading_bot.py
Restart=always
RestartSec=15
User=root

[Install]
WantedBy=multi-user.target
EOF'
sudo systemctl daemon-reload
sudo systemctl enable coindcx-bot
sudo systemctl start coindcx-bot
sudo journalctl -u coindcx-bot -f
```

## Publish to GitHub
1. Move only the folder `coindcx-day-trader/` to a clean repo:
   ```
   coindcx-day-trader/
     ├─ day_trading_bot.py
     ├─ auth_test.py
     ├─ requirements.txt
     ├─ README.md
     └─ .gitignore
   ```
2. Create a GitHub repo and push:
   ```bash
   git init
   git add .
   git commit -m "Initial commit: CoinDCX INR bot"
   git branch -M main
   git remote add origin https://github.com/yourname/coindcx-day-trader.git
   git push -u origin main
   ```

## Security Notes
- Do not commit real API keys. Use environment variables or systemd env file with `chmod 600`.
- Consider IP whitelisting for API keys.
- This strategy is educational. Markets are risky — use at your own discretion.

