import MetaTrader5 as mt5
import pandas as pd
import pytz
import csv
import time
from datetime import datetime, date

# =========================
# CONFIG
# =========================
LOGIN    = 12345678
PASSWORD = "YOUR_PASSWORD"
SERVER   = "Exness-MT5Trial"   # FIX: was "Exness-MT5Real" (live account) — changed to demo

SYMBOL        = "NAS100m"      # FIX: was "USTEC" — Exness demo uses NAS100m
LOT           = 0.10
TIMEFRAME     = mt5.TIMEFRAME_M1
RISK_REWARD   = 2
MAX_SPREAD    = 25             # points
MAGIC         = 999111
LOG_FILE      = "trade_log.csv"
SWING_LOOKBACK = 20            # candles to scan when detecting the sweep candle

NY_TZ = pytz.timezone("America/New_York")

# =========================
# STATE
# =========================
last_trade_day    = None
trade_taken_today = False

# =========================
# MT5 CONNECTION
# =========================
def connect():
    if not mt5.initialize():
        raise Exception(f"MT5 initialize failed: {mt5.last_error()}")
    if not mt5.login(LOGIN, password=PASSWORD, server=SERVER):
        mt5.shutdown()
        raise Exception(f"MT5 login failed: {mt5.last_error()}")
    info = mt5.account_info()
    print(f"Connected | Account: {info.login} | Balance: ${info.balance:,.2f} | Server: {info.server}")

# =========================
# SESSION FILTER
# =========================
def inside_session():
    now   = datetime.now(NY_TZ)
    start = now.replace(hour=9,  minute=50, second=0, microsecond=0)
    end   = now.replace(hour=11, minute=10, second=0, microsecond=0)
    return start <= now <= end

# =========================
# DAILY RESET
# =========================
def reset_daily():
    global last_trade_day, trade_taken_today
    today = date.today()
    if last_trade_day != today:
        last_trade_day    = today
        trade_taken_today = False

# =========================
# GET DATA
# =========================
def get_data(bars=300):
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, bars)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df

# =========================
# SPREAD CHECK
# =========================
def acceptable_spread():
    tick     = mt5.symbol_info_tick(SYMBOL)
    sym_info = mt5.symbol_info(SYMBOL)
    if tick is None or sym_info is None:
        return False
    spread = (tick.ask - tick.bid) / sym_info.point
    return spread <= MAX_SPREAD

# =========================
# FVG DETECTION
# FIX: was reading iloc[-1] (forming candle). Now accepts explicit index
# so the caller controls which candles to use (always closed candles).
# =========================
def bullish_fvg(df, i):
    """
    Bullish FVG: candle[i-2].high < candle[i].low  →  gap above c1, below c3.
    Uses closed candles only — caller must ensure i <= len(df)-2.
    """
    if i < 2:
        return None
    c1 = df.iloc[i - 2]
    c3 = df.iloc[i]
    if c3["low"] > c1["high"]:
        return float(c1["high"]), float(c3["low"])
    return None

def bearish_fvg(df, i):
    """
    Bearish FVG: candle[i].high < candle[i-2].low  →  gap below c1, above c3.
    """
    if i < 2:
        return None
    c1 = df.iloc[i - 2]
    c3 = df.iloc[i]
    if c3["high"] < c1["low"]:
        return float(c3["high"]), float(c1["low"])
    return None

# =========================
# SWEEP CANDLE FINDER
# FIX: was hardcoded to iloc[-5]. Now searches the full lookback window
# for the actual candle whose low/high broke the recent swing level.
# =========================
def find_sweep_candle_idx(df, last_closed_idx, direction):
    """
    Walk backwards from last_closed_idx to find the candle that swept
    the recent swing low (direction='bull') or swing high (direction='bear').

    Returns the integer index into df, or None if not found.
    """
    search_start = max(0, last_closed_idx - SWING_LOOKBACK)

    if direction == "bull":
        # Swing low = min low in the window before the search zone
        ref_start = max(0, search_start - SWING_LOOKBACK)
        recent_low = float(df["low"].iloc[ref_start:search_start].min())
        for i in range(last_closed_idx, search_start, -1):
            if df.iloc[i]["low"] < recent_low:
                return i

    else:  # bear
        ref_start = max(0, search_start - SWING_LOOKBACK)
        recent_high = float(df["high"].iloc[ref_start:search_start].max())
        for i in range(last_closed_idx, search_start, -1):
            if df.iloc[i]["high"] > recent_high:
                return i

    return None

# =========================
# SETUP DETECTION
# FIX: uses last closed candle (iloc[-2]) throughout
# FIX: sweep candle found dynamically, not hardcoded to -5
# FIX: FVG scanned across full post-sweep range
# FIX: added SL/TP sanity check before returning
# =========================
def detect_buy_setup(df):
    n              = len(df)
    last_closed    = n - 2          # FIX: last fully closed candle
    current_candle = df.iloc[last_closed]

    # Reference swing low: lowest low in the 20 candles before the lookback zone
    ref_end   = max(0, last_closed - SWING_LOOKBACK)
    ref_start = max(0, ref_end - SWING_LOOKBACK)
    if ref_end <= ref_start:
        return None
    recent_low = float(df["low"].iloc[ref_start:ref_end].min())

    # FIX: find the actual sweep candle dynamically
    sweep_idx = find_sweep_candle_idx(df, last_closed, "bull")
    if sweep_idx is None:
        return None

    sweep_candle = df.iloc[sweep_idx]

    # Sweep: the candle broke below the recent low
    if sweep_candle["low"] >= recent_low:
        return None

    # Displacement: current candle closed above the sweep candle's high
    if current_candle["close"] <= sweep_candle["high"]:
        return None

    # Find most recent bullish FVG formed after the sweep
    fvg = None
    for i in range(sweep_idx + 2, last_closed + 1):
        result = bullish_fvg(df, i)
        if result:
            fvg = result  # keep the latest

    if fvg is None:
        return None

    fvg_low, fvg_high = fvg
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None
    current_price = tick.ask

    if not (fvg_low <= current_price <= fvg_high):
        return None

    sl   = float(sweep_candle["low"]) - 1.0
    risk = current_price - sl
    tp   = current_price + risk * RISK_REWARD

    # FIX: sanity check — SL must be below entry, TP above entry
    if sl >= current_price or tp <= current_price:
        return None

    return current_price, sl, tp


def detect_sell_setup(df):
    n              = len(df)
    last_closed    = n - 2
    current_candle = df.iloc[last_closed]

    ref_end   = max(0, last_closed - SWING_LOOKBACK)
    ref_start = max(0, ref_end - SWING_LOOKBACK)
    if ref_end <= ref_start:
        return None
    recent_high = float(df["high"].iloc[ref_start:ref_end].max())

    sweep_idx = find_sweep_candle_idx(df, last_closed, "bear")
    if sweep_idx is None:
        return None

    sweep_candle = df.iloc[sweep_idx]

    if sweep_candle["high"] <= recent_high:
        return None

    if current_candle["close"] >= sweep_candle["low"]:
        return None

    fvg = None
    for i in range(sweep_idx + 2, last_closed + 1):
        result = bearish_fvg(df, i)
        if result:
            fvg = result

    if fvg is None:
        return None

    fvg_low, fvg_high = fvg
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None
    current_price = tick.bid

    if not (fvg_low <= current_price <= fvg_high):
        return None

    sl   = float(sweep_candle["high"]) + 1.0
    risk = sl - current_price
    tp   = current_price - risk * RISK_REWARD

    # FIX: sanity check
    if sl <= current_price or tp >= current_price:
        return None

    return current_price, sl, tp

# =========================
# DUPLICATE CHECK
# =========================
def open_position_exists():
    positions = mt5.positions_get(symbol=SYMBOL)
    return positions is not None and len(positions) > 0

# =========================
# LOGGING
# =========================
def log_trade(side, entry, sl, tp):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now(), side, entry, sl, tp])

# =========================
# PLACE ORDER
# =========================
def place_order(side, entry, sl, tp):
    global trade_taken_today

    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       LOT,
        "type":         order_type,
        "price":        entry,
        "sl":           sl,
        "tp":           tp,
        "deviation":    20,
        "magic":        MAGIC,
        "comment":      "Institutional_FVG_Bot",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
        # NOTE: if you get retcode 10030 from Exness, change to:
        # "type_filling": mt5.ORDER_FILLING_FOK,
    }

    result = mt5.order_send(request)
    if result is None:
        print(f"order_send returned None — {mt5.last_error()}")
        return

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        trade_taken_today = True
        log_trade(side, entry, sl, tp)
        print(f"✅ Trade placed: {side} @ {entry:.2f} | SL={sl:.2f} | TP={tp:.2f}")
    else:
        print(f"❌ Order failed | retcode={result.retcode} | {result.comment}")

# =========================
# CLOSE ALL POSITIONS
# FIX: added — original had no graceful shutdown
# =========================
def close_all_positions():
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return
    for pos in positions:
        trade_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(SYMBOL)
        price = tick.bid if pos.type == 0 else tick.ask
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       SYMBOL,
            "volume":       pos.volume,
            "type":         trade_type,
            "position":     pos.ticket,
            "price":        price,
            "deviation":    20,
            "magic":        MAGIC,
            "comment":      "bot_shutdown",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"Closed position #{pos.ticket} | P&L: ${pos.profit:.2f}")
        else:
            print(f"Failed to close #{pos.ticket}")

# =========================
# MAIN
# =========================
def run():
    connect()

    try:
        while True:
            reset_daily()

            if not inside_session():
                time.sleep(10)
                continue

            if trade_taken_today:
                time.sleep(10)
                continue

            if open_position_exists():
                time.sleep(10)
                continue

            if not acceptable_spread():
                time.sleep(5)
                continue

            df = get_data()
            if df is None:
                print("Warning: failed to fetch candles — retrying.")
                time.sleep(5)
                continue

            buy = detect_buy_setup(df)
            if buy:
                place_order("BUY", *buy)
                time.sleep(60)
                continue

            sell = detect_sell_setup(df)
            if sell:
                place_order("SELL", *sell)
                time.sleep(60)
                continue

            time.sleep(5)

    except KeyboardInterrupt:
        print("\nCtrl+C received — shutting down gracefully.")

    finally:
        # FIX: close open trades and disconnect on exit
        close_all_positions()
        mt5.shutdown()
        print("MT5 disconnected.")

# =========================
# START
# =========================
if __name__ == "__main__":
    run()
