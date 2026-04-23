import pandas as pd
import pytz
from datetime import time

# =========================
# CONFIG
# =========================
CSV_FILE = "nas100_m1.csv"
INITIAL_BALANCE = 10000
RISK_PER_TRADE = 100
RR = 2

NY_TZ = pytz.timezone("America/New_York")

# =========================
# LOAD DATA
# =========================
df = pd.read_csv(CSV_FILE)
df["time"] = pd.to_datetime(df["time"])
df = df.sort_values("time").reset_index(drop=True)

# =========================
# SESSION FILTER
# =========================
def in_session(ts):
    ts = ts.tz_localize("UTC").tz_convert(NY_TZ)
    return time(9, 50) <= ts.time() <= time(11, 10)

# =========================
# FVG DETECTION
# =========================
def bullish_fvg(data, i):
    c1 = data.iloc[i-2]
    c3 = data.iloc[i]

    if c3.low > c1.high:
        return c1.high, c3.low
    return None

def bearish_fvg(data, i):
    c1 = data.iloc[i-2]
    c3 = data.iloc[i]

    if c3.high < c1.low:
        return c3.high, c1.low
    return None

# =========================
# BUY SETUP
# =========================
def detect_buy(data, i):
    recent = data.iloc[i-20:i-5]
    sweep_candle = data.iloc[i-5]
    current = data.iloc[i]

    recent_low = recent.low.min()

    if sweep_candle.low < recent_low and current.close > sweep_candle.high:
        fvg = bullish_fvg(data, i)
        if not fvg:
            return None

        fvg_low, fvg_high = fvg

        if fvg_low <= current.close <= fvg_high:
            entry = current.close
            sl = sweep_candle.low - 1
            risk = entry - sl
            tp = entry + risk * RR
            return ("BUY", entry, sl, tp)

    return None

# =========================
# SELL SETUP
# =========================
def detect_sell(data, i):
    recent = data.iloc[i-20:i-5]
    sweep_candle = data.iloc[i-5]
    current = data.iloc[i]

    recent_high = recent.high.max()

    if sweep_candle.high > recent_high and current.close < sweep_candle.low:
        fvg = bearish_fvg(data, i)
        if not fvg:
            return None

        fvg_low, fvg_high = fvg

        if fvg_low <= current.close <= fvg_high:
            entry = current.close
            sl = sweep_candle.high + 1
            risk = sl - entry
            tp = entry - risk * RR
            return ("SELL", entry, sl, tp)

    return None

# =========================
# TRADE SIMULATION
# =========================
def simulate_trade(data, start_i, side, entry, sl, tp):
    for j in range(start_i + 1, len(data)):
        candle = data.iloc[j]

        if side == "BUY":
            if candle.low <= sl:
                return j, -1
            if candle.high >= tp:
                return j, RR

        if side == "SELL":
            if candle.high >= sl:
                return j, -1
            if candle.low <= tp:
                return j, RR

    return len(data)-1, 0

# =========================
# BACKTEST
# =========================
balance = INITIAL_BALANCE
equity_curve = []
trades = []
last_trade_day = None

i = 25

while i < len(df):
    row = df.iloc[i]
    day = row.time.date()

    if not in_session(row.time):
        i += 1
        continue

    if day == last_trade_day:
        i += 1
        continue

    trade = detect_buy(df, i)

    if not trade:
        trade = detect_sell(df, i)

    if trade:
        side, entry, sl, tp = trade

        exit_i, result_r = simulate_trade(df, i, side, entry, sl, tp)

        pnl = result_r * RISK_PER_TRADE
        balance += pnl

        trades.append({
            "time": row.time,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "R": result_r,
            "pnl": pnl,
            "balance": balance
        })

        equity_curve.append(balance)
        last_trade_day = day
        i = exit_i
    else:
        i += 1

# =========================
# RESULTS
# =========================
results = pd.DataFrame(trades)

wins = len(results[results["R"] > 0])
losses = len(results[results["R"] < 0])
total = len(results)

win_rate = (wins / total * 100) if total else 0

print("========== BACKTEST ==========")
print("Trades:", total)
print("Wins:", wins)
print("Losses:", losses)
print("Win Rate:", round(win_rate, 2), "%")
print("Final Balance:", round(balance, 2))

results.to_csv("backtest_results.csv", index=False)
print("Trade journal saved to backtest_results.csv")
