import pandas as pd
import pytz
from datetime import time

# =========================
# CONFIG
# =========================
CSV_FILE        = "nas100_m1.csv"   
INITIAL_BALANCE = 10000
RISK_PER_TRADE  = 100                      # USD risked per trade
RR              = 2
SWING_LOOKBACK  = 20

NY_TZ = pytz.timezone("America/New_York")

# =========================
# LOAD DATA
# FIX: original CSV had only 3 rows — use the v2-cl sample which has real data.
# Expected columns: time, open, high, low, close (standard MT5 export format)
# =========================
df = pd.read_csv(CSV_FILE)

# Normalise column names (MT5 exports sometimes use angle-bracket headers)
df.columns = [c.strip("<>").lower() for c in df.columns]

# Parse time — handle both "DATE TIME" split columns and single datetime column
if "date" in df.columns and "time" in df.columns:
    df["time"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str))
    df = df.drop(columns=["date"])
else:
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
# FIX: now accepts explicit index i — no forming-candle risk
# =========================
def bullish_fvg(data, i):
    if i < 2:
        return None
    c1 = data.iloc[i - 2]
    c3 = data.iloc[i]
    if c3["low"] > c1["high"]:
        return float(c1["high"]), float(c3["low"])
    return None

def bearish_fvg(data, i):
    if i < 2:
        return None
    c1 = data.iloc[i - 2]
    c3 = data.iloc[i]
    if c3["high"] < c1["low"]:
        return float(c3["high"]), float(c1["low"])
    return None

# =========================
# SWEEP CANDLE FINDER
# FIX: replaces hardcoded iloc[-5] with dynamic search
# =========================
def find_sweep_candle_idx(data, last_idx, direction):
    search_start = max(0, last_idx - SWING_LOOKBACK)
    ref_start    = max(0, search_start - SWING_LOOKBACK)

    if direction == "bull":
        recent_low = float(data["low"].iloc[ref_start:search_start].min())
        for i in range(last_idx, search_start, -1):
            if data.iloc[i]["low"] < recent_low:
                return i
    else:
        recent_high = float(data["high"].iloc[ref_start:search_start].max())
        for i in range(last_idx, search_start, -1):
            if data.iloc[i]["high"] > recent_high:
                return i
    return None

# =========================
# BUY SETUP
# =========================
def detect_buy(data, i):
    # i is the last closed candle in the simulation window
    ref_end   = max(0, i - SWING_LOOKBACK)
    ref_start = max(0, ref_end - SWING_LOOKBACK)
    if ref_end <= ref_start:
        return None

    recent_low = float(data["low"].iloc[ref_start:ref_end].min())
    current    = data.iloc[i]

    sweep_idx = find_sweep_candle_idx(data, i, "bull")
    if sweep_idx is None:
        return None

    sweep_candle = data.iloc[sweep_idx]

    if sweep_candle["low"] >= recent_low:
        return None
    if current["close"] <= sweep_candle["high"]:
        return None

    fvg = None
    for j in range(sweep_idx + 2, i + 1):
        result = bullish_fvg(data, j)
        if result:
            fvg = result

    if fvg is None:
        return None

    fvg_low, fvg_high = fvg
    entry = float(current["close"])

    if not (fvg_low <= entry <= fvg_high):
        return None

    sl   = float(sweep_candle["low"]) - 1.0
    risk = entry - sl
    tp   = entry + risk * RR

    if sl >= entry or tp <= entry:
        return None

    return "BUY", entry, sl, tp


def detect_sell(data, i):
    ref_end   = max(0, i - SWING_LOOKBACK)
    ref_start = max(0, ref_end - SWING_LOOKBACK)
    if ref_end <= ref_start:
        return None

    recent_high = float(data["high"].iloc[ref_start:ref_end].max())
    current     = data.iloc[i]

    sweep_idx = find_sweep_candle_idx(data, i, "bear")
    if sweep_idx is None:
        return None

    sweep_candle = data.iloc[sweep_idx]

    if sweep_candle["high"] <= recent_high:
        return None
    if current["close"] >= sweep_candle["low"]:
        return None

    fvg = None
    for j in range(sweep_idx + 2, i + 1):
        result = bearish_fvg(data, j)
        if result:
            fvg = result

    if fvg is None:
        return None

    fvg_low, fvg_high = fvg
    entry = float(current["close"])

    if not (fvg_low <= entry <= fvg_high):
        return None

    sl   = float(sweep_candle["high"]) + 1.0
    risk = sl - entry
    tp   = entry - risk * RR

    if sl <= entry or tp >= entry:
        return None

    return "SELL", entry, sl, tp

# =========================
# TRADE SIMULATION
# =========================
def simulate_trade(data, start_i, side, entry, sl, tp):
    for j in range(start_i + 1, len(data)):
        candle = data.iloc[j]
        if side == "BUY":
            if candle["low"]  <= sl: return j, -1
            if candle["high"] >= tp: return j,  RR
        else:
            if candle["high"] >= sl: return j, -1
            if candle["low"]  <= tp: return j,  RR
    return len(data) - 1, 0   # open at end of data

# =========================
# BACKTEST
# FIX: start at index 60 (enough history for double lookback)
# FIX: skip candle if not enough prior history
# =========================
balance        = INITIAL_BALANCE
trades         = []
last_trade_day = None

MIN_START = SWING_LOOKBACK * 3   # need enough candles for ref window + sweep window

i = MIN_START
while i < len(df) - 1:   # -1 so iloc[i] is always a closed candle
    row = df.iloc[i]
    day = row["time"].date()

    if not in_session(row["time"]):
        i += 1
        continue

    if day == last_trade_day:
        i += 1
        continue

    trade = detect_buy(df, i) or detect_sell(df, i)

    if trade:
        side, entry, sl, tp = trade
        exit_i, result_r    = simulate_trade(df, i, side, entry, sl, tp)
        pnl                 = result_r * RISK_PER_TRADE
        balance            += pnl

        trades.append({
            "time":    row["time"],
            "side":    side,
            "entry":   entry,
            "sl":      sl,
            "tp":      tp,
            "R":       result_r,
            "pnl":     pnl,
            "balance": balance,
        })

        last_trade_day = day
        i = exit_i
    else:
        i += 1

# =========================
# RESULTS
# =========================
results = pd.DataFrame(trades)

if results.empty:
    print("No trades found. Check your CSV has enough session-hour data.")
else:
    wins     = len(results[results["R"] > 0])
    losses   = len(results[results["R"] < 0])
    total    = len(results)
    win_rate = wins / total * 100

    print("=" * 40)
    print("         BACKTEST RESULTS")
    print("=" * 40)
    print(f"  Trades      : {total}")
    print(f"  Wins        : {wins}")
    print(f"  Losses      : {losses}")
    print(f"  Win Rate    : {win_rate:.1f}%")
    print(f"  Start Bal   : ${INITIAL_BALANCE:,.2f}")
    print(f"  Final Bal   : ${balance:,.2f}")
    print(f"  Net P&L     : ${balance - INITIAL_BALANCE:+,.2f}")
    print("=" * 40)

    results.to_csv("backtest_results.csv", index=False)
    print("Trade journal saved → backtest_results.csv")
