"""
=======================================================
  EXNESS MT5 FOREX BOT — SMA CROSSOVER STRATEGY
  For Demo Account Use Only
  
  Strategy : SMA 10 crosses above SMA 20 → BUY
             SMA 10 crosses below SMA 20 → SELL
             
  Requires : MetaTrader5 Python library (Windows only)
             pip install MetaTrader5 pandas pandas-ta
=======================================================
"""

import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
import time
import logging
from datetime import datetime

# ─────────────────────────────────────────────
#  CONFIGURATION — Edit these before running
# ─────────────────────────────────────────────

LOGIN      = 123456789        # Your Exness MT5 demo account number
PASSWORD   = "your_password"  # Your MT5 password
SERVER     = "Exness-MT5Trial"  # Your Exness demo server name (check MT5 login screen)

SYMBOL     = "EURUSDm"        # Exness demo symbols often have 'm' suffix e.g. EURUSDm
TIMEFRAME  = mt5.TIMEFRAME_M15  # 15-minute candles (good balance of speed vs noise)
LOT_SIZE   = 0.01             # Micro lot — smallest safe size for demo

SMA_FAST   = 10               # Fast SMA period
SMA_SLOW   = 20               # Slow SMA period

STOP_LOSS_PIPS   = 20         # Stop loss in pips
TAKE_PROFIT_PIPS = 40         # Take profit in pips (2:1 reward/risk)

MAX_OPEN_TRADES  = 1          # Only 1 trade open at a time
SLEEP_SECONDS    = 60         # Check for signals every 60 seconds

# ─────────────────────────────────────────────
#  LOGGING SETUP
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("bot_log.txt"),
        logging.StreamHandler()          # also print to terminal
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  CONNECT TO MT5
# ─────────────────────────────────────────────

def connect():
    """Initialize and login to MT5 terminal."""
    if not mt5.initialize():
        log.error(f"MT5 initialize() failed: {mt5.last_error()}")
        return False

    authorized = mt5.login(LOGIN, password=PASSWORD, server=SERVER)
    if not authorized:
        log.error(f"MT5 login failed: {mt5.last_error()}")
        mt5.shutdown()
        return False

    info = mt5.account_info()
    log.info("=" * 50)
    log.info(f"  Connected to Exness MT5")
    log.info(f"  Account : {info.login}")
    log.info(f"  Balance : ${info.balance:,.2f}")
    log.info(f"  Server  : {info.server}")
    log.info(f"  Symbol  : {SYMBOL} | TF: M15 | Lot: {LOT_SIZE}")
    log.info("=" * 50)
    return True


# ─────────────────────────────────────────────
#  FETCH CANDLE DATA
# ─────────────────────────────────────────────

def get_candles(symbol, timeframe, count=100):
    """Fetch the last N candles and compute SMAs."""
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        log.warning("No candle data received.")
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    # Calculate SMAs
    df["sma_fast"] = ta.sma(df["close"], length=SMA_FAST)
    df["sma_slow"] = ta.sma(df["close"], length=SMA_SLOW)

    return df


# ─────────────────────────────────────────────
#  SIGNAL DETECTION
# ─────────────────────────────────────────────

def get_signal(df):
    """
    Detect SMA crossover on the last two completed candles.
    Returns: 'buy', 'sell', or None
    """
    # Use index -2 (last closed candle) and -3 (candle before that)
    # Avoid index -1 which is the still-forming candle
    if df is None or len(df) < SMA_SLOW + 2:
        return None

    prev  = df.iloc[-3]   # candle before last
    last  = df.iloc[-2]   # last fully closed candle

    # Check SMAs are valid (not NaN)
    if pd.isna(last["sma_fast"]) or pd.isna(last["sma_slow"]):
        return None
    if pd.isna(prev["sma_fast"]) or pd.isna(prev["sma_slow"]):
        return None

    # Bullish crossover: fast crossed above slow
    bullish = (prev["sma_fast"] <= prev["sma_slow"]) and (last["sma_fast"] > last["sma_slow"])
    # Bearish crossover: fast crossed below slow
    bearish = (prev["sma_fast"] >= prev["sma_slow"]) and (last["sma_fast"] < last["sma_slow"])

    if bullish:
        log.info(f"  SIGNAL: BUY  | SMA{SMA_FAST}={last['sma_fast']:.5f} > SMA{SMA_SLOW}={last['sma_slow']:.5f}")
        return "buy"
    elif bearish:
        log.info(f"  SIGNAL: SELL | SMA{SMA_FAST}={last['sma_fast']:.5f} < SMA{SMA_SLOW}={last['sma_slow']:.5f}")
        return "sell"

    return None


# ─────────────────────────────────────────────
#  POSITION MANAGEMENT
# ─────────────────────────────────────────────

def get_open_positions(symbol):
    """Return list of open positions for the symbol."""
    positions = mt5.positions_get(symbol=symbol)
    return positions if positions else []


def has_open_position(symbol):
    return len(get_open_positions(symbol)) >= MAX_OPEN_TRADES


def close_all_positions(symbol):
    """Close all open positions for the symbol."""
    positions = get_open_positions(symbol)
    for pos in positions:
        trade_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        price = mt5.symbol_info_tick(symbol).bid if pos.type == 0 else mt5.symbol_info_tick(symbol).ask

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    symbol,
            "volume":    pos.volume,
            "type":      trade_type,
            "position":  pos.ticket,
            "price":     price,
            "deviation": 20,
            "magic":     20240101,
            "comment":   "bot_close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"  CLOSED position #{pos.ticket} | P&L: ${pos.profit:.2f}")
        else:
            log.error(f"  Failed to close #{pos.ticket}: {result.comment}")


# ─────────────────────────────────────────────
#  PLACE ORDER
# ─────────────────────────────────────────────

def place_order(symbol, signal):
    """Place a buy or sell market order with SL and TP."""
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        log.error(f"Symbol {symbol} not found. Check the symbol name.")
        return

    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)

    tick     = mt5.symbol_info_tick(symbol)
    pip_size = symbol_info.point * 10  # 1 pip = 10 points for most pairs

    if signal == "buy":
        order_type = mt5.ORDER_TYPE_BUY
        price      = tick.ask
        sl         = round(price - STOP_LOSS_PIPS * pip_size, symbol_info.digits)
        tp         = round(price + TAKE_PROFIT_PIPS * pip_size, symbol_info.digits)
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price      = tick.bid
        sl         = round(price + STOP_LOSS_PIPS * pip_size, symbol_info.digits)
        tp         = round(price - TAKE_PROFIT_PIPS * pip_size, symbol_info.digits)

    request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    symbol,
        "volume":    LOT_SIZE,
        "type":      order_type,
        "price":     price,
        "sl":        sl,
        "tp":        tp,
        "deviation": 20,
        "magic":     20240101,
        "comment":   f"bot_{signal}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"  ORDER PLACED: {signal.upper()} {LOT_SIZE} lot @ {price:.5f} | SL: {sl:.5f} | TP: {tp:.5f}")
    else:
        log.error(f"  ORDER FAILED: {result.comment} (code {result.retcode})")


# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────

def run():
    log.info("Starting Exness SMA Crossover Bot...")

    if not connect():
        log.error("Could not connect to MT5. Exiting.")
        return

    try:
        while True:
            log.info(f"--- Checking signal [{datetime.now().strftime('%H:%M:%S')}] ---")

            df = get_candles(SYMBOL, TIMEFRAME)
            if df is None:
                time.sleep(SLEEP_SECONDS)
                continue

            signal = get_signal(df)

            if signal:
                if has_open_position(SYMBOL):
                    # Close existing opposite position before opening new one
                    positions = get_open_positions(SYMBOL)
                    for pos in positions:
                        existing_type = "buy" if pos.type == 0 else "sell"
                        if existing_type != signal:
                            log.info("  Closing opposite position before reversing...")
                            close_all_positions(SYMBOL)
                            time.sleep(1)
                            place_order(SYMBOL, signal)
                        else:
                            log.info(f"  Already in a {existing_type} position. Skipping.")
                else:
                    place_order(SYMBOL, signal)
            else:
                # Print current SMA state for monitoring
                last = df.iloc[-2]
                if not pd.isna(last["sma_fast"]):
                    direction = "BULLISH" if last["sma_fast"] > last["sma_slow"] else "BEARISH"
                    log.info(f"  No signal | {direction} | SMA{SMA_FAST}: {last['sma_fast']:.5f} | SMA{SMA_SLOW}: {last['sma_slow']:.5f}")

            # Print account status
            info = mt5.account_info()
            log.info(f"  Balance: ${info.balance:.2f} | Equity: ${info.equity:.2f} | Open P&L: ${info.profit:.2f}")

            time.sleep(SLEEP_SECONDS)

    except KeyboardInterrupt:
        log.info("Bot stopped by user (Ctrl+C).")
    finally:
        close_all_positions(SYMBOL)
        mt5.shutdown()
        log.info("MT5 connection closed.")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    run()
