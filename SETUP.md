# Exness MT5 Bot — Setup Guide

## Requirements
- Windows PC or Windows VPS (MT5 Python library is Windows-only)
- Python 3.7+
- MetaTrader 5 terminal installed
- Exness demo account

## Step 1 — Install Python Libraries
Open Command Prompt and run:
```
pip install MetaTrader5 pandas pandas-ta
```

## Step 2 — Open Exness Demo Account
1. Go to https://www.exness.com
2. Register and open a **Demo MT5 account**
3. Note your: Login number, Password, Server name (shown on MT5 login screen)

## Step 3 — Edit the Bot Config
Open exness_bot.py and update these 3 lines:
```python
LOGIN    = 123456789          # ← Your actual account number
PASSWORD = "your_password"    # ← Your actual password
SERVER   = "Exness-MT5Trial"  # ← Exact server name from MT5 login screen
```

## Step 4 — Check the Symbol Name
Exness demo accounts often use symbols with an 'm' suffix.
In MT5 terminal → Market Watch → right-click → Show All
Find your symbol (e.g. EURUSDm) and update:
```python
SYMBOL = "EURUSDm"
```

## Step 5 — Run the Bot
```
python exness_bot.py
```

The bot will:
- Connect to your MT5 account
- Check for signals every 60 seconds
- Log everything to bot_log.txt and your terminal

## Stopping the Bot
Press Ctrl+C — it will close any open positions cleanly before shutting down.

## Files Created
- exness_bot.py — the bot
- bot_log.txt — auto-created trade log
