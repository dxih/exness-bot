import MetaTrader5 as mt5
import pandas as pd
import pytz
import csv
import time
from datetime import datetime, date

# =========================
# CONFIG
# =========================
LOGIN = 12345678
PASSWORD = "YOUR_PASSWORD"
SERVER = "Exness-MT5Real"

SYMBOL = "USTEC"            # NAS100 on Exness
LOT = 0.10
TIMEFRAME = mt5.TIMEFRAME_M1
RISK_REWARD = 2
MAX_SPREAD = 25             # points
MAGIC = 999111
LOG_FILE = "trade_log.csv"

NY_TZ = pytz.timezone("America/New_York")

# =========================
# STATE
# =========================
last_trade_day = None
trade_taken_today = False

# =========================
# MT5 CONNECTION
# =========================
def connect():
    if not mt5.initialize():
        raise Exception("MT5 initialize failed")

    if not mt5.login(LOGIN, password=PASSWORD, server=SERVER):
        raise Exception("MT5 login failed")

    print("Connected to Exness MT5")

# =========================
# SESSION FILTER
# =========================
def inside_session():
    now = datetime.now(NY_TZ)
    start = now.replace(hour=9, minute=50, second=0, microsecond=0)
    end = now.replace(hour=11, minute=10, second=0, microsecond=0)
    return start <= now <= end

# =========================
# DAILY RESET
# =========================
def reset_daily():
    global last_trade_day, trade_taken_today

    today = date.today()
    if last_trade_day != today:
        last_trade_day = today
        trade_taken_today = False

# =========================
# GET DATA
# =========================
def get_data(bars=300):
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, bars)
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df

# =========================
# SPREAD CHECK
# =========================
def acceptable_spread():
    tick = mt5.symbol_info_tick(SYMBOL)
    spread = (tick.ask - tick.bid) / mt5.symbol_info(SYMBOL).point
    return spread <= MAX_SPREAD

# =========================
# FAIR VALUE GAP
# =========================
def bullish_fvg(df):
    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

    if c3["low"] > c1["high"]:
        return c1["high"], c3["low"]

    return None

def bearish_fvg(df):
    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

    if c3["high"] < c1["low"]:
        return c3["high"], c1["low"]

    return None

# =========================
# LIQUIDITY SWEEP
# =========================
def detect_buy_setup(df):
    recent_low = df["low"].iloc[-20:-5].min()
    latest = df.iloc[-1]

    # price swept low then displaced upward
    if df.iloc[-5]["low"] < recent_low and latest["close"] > df.iloc[-5]["high"]:
        fvg = bullish_fvg(df)
        if not fvg:
            return None

        fvg_low, fvg_high = fvg
        current_price = mt5.symbol_info_tick(SYMBOL).ask

        if fvg_low <= current_price <= fvg_high:
            sl = df.iloc[-5]["low"] - 1
            risk = current_price - sl
            tp = current_price + (risk * RISK_REWARD)
            return current_price, sl, tp

    return None

def detect_sell_setup(df):
    recent_high = df["high"].iloc[-20:-5].max()
    latest = df.iloc[-1]

    # price swept high then displaced downward
    if df.iloc[-5]["high"] > recent_high and latest["close"] < df.iloc[-5]["low"]:
        fvg = bearish_fvg(df)
        if not fvg:
            return None

        fvg_low, fvg_high = fvg
        current_price = mt5.symbol_info_tick(SYMBOL).bid

        if fvg_low <= current_price <= fvg_high:
            sl = df.iloc[-5]["high"] + 1
            risk = sl - current_price
            tp = current_price - (risk * RISK_REWARD)
            return current_price, sl, tp

    return None

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
        writer.writerow([
            datetime.now(),
            side,
            entry,
            sl,
            tp
        ])

# =========================
# PLACE ORDER
# =========================
def place_order(side, entry, sl, tp):
    global trade_taken_today

    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": LOT,
        "type": order_type,
        "price": entry,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": MAGIC,
        "comment": "Institutional_FVG_Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        trade_taken_today = True
        log_trade(side, entry, sl, tp)
        print("Trade placed:", side)
    else:
        print("Order failed:", result)

# =========================
# MAIN
# =========================
def run():
    connect()

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

# =========================
# START
# =========================
if __name__ == "__main__":
    run()
