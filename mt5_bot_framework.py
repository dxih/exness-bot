"""
╔══════════════════════════════════════════════════════════════════════════════╗
║              MT5 GENERIC TRADING BOT FRAMEWORK                             ║
║              Broker : Exness (works with any MT5 broker)                   ║
║              Author : (your name)                                          ║
║              Usage  : Subclass or fill in the strategy hooks below         ║
╚══════════════════════════════════════════════════════════════════════════════╝

HOW TO USE THIS FRAMEWORK
─────────────────────────
1. Fill in the CONFIG section with your account credentials and preferences.
2. Implement your entry logic inside `get_signal()`.
3. Optionally override any hook (on_start, on_tick, on_trade_open, etc.).
4. Run:  python mt5_bot_framework.py

Every function is either fully implemented (infrastructure) or clearly marked
as a strategy hook with a docstring explaining what to return/do.
"""

# ── Standard Library ──────────────────────────────────────────────────────────
import time
import logging
import sys
from datetime import datetime
from typing import Optional, Literal

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    import MetaTrader5 as mt5
    import pandas as pd
    import pandas_ta as ta
except ImportError as e:
    sys.exit(
        f"[FATAL] Missing dependency: {e}\n"
        "Run:  pip install MetaTrader5 pandas pandas-ta"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 ── CONFIGURATION
#  Edit all values in this section before running.
# ══════════════════════════════════════════════════════════════════════════════

class Config:
    """
    Central configuration for the bot.
    All tuneable parameters live here so nothing is hardcoded in logic.
    """

    # ── Account ───────────────────────────────────────────────────────────────
    LOGIN: int    = 0                    # Your MT5 account number
    PASSWORD: str = ""                   # Your MT5 password
    SERVER: str   = "Exness-MT5Trial"    # Server name shown on MT5 login screen

    # ── Instrument ────────────────────────────────────────────────────────────
    SYMBOL: str    = "EURUSDm"           # Symbol to trade (check Market Watch for exact name)
    TIMEFRAME: int = mt5.TIMEFRAME_M15  # Candle timeframe (see MT5 timeframe constants below)
    #
    # Common timeframes:
    #   mt5.TIMEFRAME_M1   →  1 minute
    #   mt5.TIMEFRAME_M5   →  5 minutes
    #   mt5.TIMEFRAME_M15  →  15 minutes  ← default
    #   mt5.TIMEFRAME_M30  →  30 minutes
    #   mt5.TIMEFRAME_H1   →  1 hour
    #   mt5.TIMEFRAME_H4   →  4 hours
    #   mt5.TIMEFRAME_D1   →  1 day

    # ── Position Sizing ───────────────────────────────────────────────────────
    LOT_SIZE: float = 0.01               # Volume per trade (0.01 = micro lot, safest for demo)

    # ── Risk Management ───────────────────────────────────────────────────────
    STOP_LOSS_PIPS: int   = 20           # Stop loss distance in pips (0 = disabled)
    TAKE_PROFIT_PIPS: int = 40           # Take profit distance in pips (0 = disabled)
    MAX_OPEN_TRADES: int  = 1            # Max simultaneous open positions (1 = one at a time)
    MAX_DAILY_LOSS: float = 0.0          # Max daily loss in account currency (0.0 = disabled)

    # ── Candle Data ───────────────────────────────────────────────────────────
    CANDLE_COUNT: int = 100              # Number of historical candles to fetch per tick

    # ── Loop Timing ───────────────────────────────────────────────────────────
    SLEEP_SECONDS: int = 60             # Seconds to wait between each signal check

    # ── Order Execution ───────────────────────────────────────────────────────
    DEVIATION: int  = 20                # Max price deviation in points (slippage tolerance)
    MAGIC: int      = 20240101          # Magic number — unique ID to tag this bot's trades
    COMMENT: str    = "mt5_bot"         # Comment attached to each order (visible in MT5)

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_FILE: str   = "bot_log.txt"     # Log file path (set to "" to disable file logging)
    LOG_LEVEL: int  = logging.INFO      # Logging verbosity (DEBUG, INFO, WARNING, ERROR)


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 ── LOGGER SETUP
#  Internal — no edits needed.
# ══════════════════════════════════════════════════════════════════════════════

def _setup_logger(cfg: Config) -> logging.Logger:
    """
    Configure and return the module-level logger.
    Writes to both stdout and a log file (if LOG_FILE is set).
    """
    logger = logging.getLogger("mt5_bot")
    logger.setLevel(cfg.LOG_LEVEL)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    # File handler (optional)
    if cfg.LOG_FILE:
        fh = logging.FileHandler(cfg.LOG_FILE)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 ── CONNECTION
# ══════════════════════════════════════════════════════════════════════════════

def connect(cfg: Config, log: logging.Logger) -> bool:
    """
    Initialize the MT5 terminal and log in with the provided credentials.

    Returns:
        True  — connection and login succeeded.
        False — failed; bot will not start.

    Notes:
        - MT5 terminal must already be installed and not blocked by firewall.
        - On Exness demo, the server name is usually "Exness-MT5Trial" or
          "Exness-MT5Trial2". Confirm it on the MT5 login screen.
    """
    if not mt5.initialize():
        log.error(f"mt5.initialize() failed — {mt5.last_error()}")
        return False

    authorized = mt5.login(cfg.LOGIN, password=cfg.PASSWORD, server=cfg.SERVER)
    if not authorized:
        log.error(f"mt5.login() failed — {mt5.last_error()}")
        mt5.shutdown()
        return False

    info = mt5.account_info()
    log.info("━" * 55)
    log.info("  MT5 CONNECTED")
    log.info(f"  Account  : {info.login}  ({info.server})")
    log.info(f"  Name     : {info.name}")
    log.info(f"  Balance  : ${info.balance:,.2f}  |  Equity: ${info.equity:,.2f}")
    log.info(f"  Currency : {info.currency}")
    log.info(f"  Leverage : 1:{info.leverage}")
    log.info("━" * 55)
    return True


def disconnect(log: logging.Logger) -> None:
    """
    Gracefully shut down the MT5 connection.
    Always called in the finally block of the main loop.
    """
    mt5.shutdown()
    log.info("MT5 connection closed.")


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 ── MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════

def get_candles(cfg: Config, log: logging.Logger) -> Optional[pd.DataFrame]:
    """
    Fetch the last N closed candles for the configured symbol and timeframe.

    Returns:
        pd.DataFrame with columns: time, open, high, low, close, tick_volume,
        spread, real_volume — or None if the fetch failed.

    Notes:
        - Index 0 in the returned DataFrame = the oldest candle.
        - Index -1 = the still-forming (current) candle. Do NOT use for signals.
        - Index -2 = the last *fully closed* candle. Use this for signal logic.
    """
    rates = mt5.copy_rates_from_pos(cfg.SYMBOL, cfg.TIMEFRAME, 0, cfg.CANDLE_COUNT)
    if rates is None or len(rates) == 0:
        log.warning(f"No candle data returned for {cfg.SYMBOL}. Check symbol name.")
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    return df


def get_tick(symbol: str, log: logging.Logger) -> Optional[object]:
    """
    Fetch the latest bid/ask tick for a symbol.

    Returns:
        mt5.Tick object with attributes: bid, ask, last, volume, time
        or None on failure.

    Usage example:
        tick = get_tick(cfg.SYMBOL, log)
        if tick:
            print(tick.bid, tick.ask)
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.warning(f"Could not fetch tick for {symbol}.")
    return tick


def get_symbol_info(symbol: str, log: logging.Logger) -> Optional[object]:
    """
    Fetch and return MT5 symbol metadata (digits, pip size, min lot, etc.).

    Returns:
        mt5.SymbolInfo object, or None on failure.

    Key attributes:
        .digits       — decimal places in price (e.g. 5 for EURUSD)
        .point        — smallest price increment (0.00001 for 5-digit brokers)
        .trade_contract_size  — units per lot (usually 100,000)
        .volume_min   — minimum lot size allowed
        .volume_step  — lot size increment
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        log.error(f"Symbol '{symbol}' not found. Check Market Watch in MT5.")
        return None
    # Auto-select symbol if not visible in Market Watch
    if not info.visible:
        mt5.symbol_select(symbol, True)
    return info


def get_account_info(log: logging.Logger) -> Optional[object]:
    """
    Fetch and return the current account state from MT5.

    Returns:
        mt5.AccountInfo object with attributes:
            .balance   — deposited funds
            .equity    — balance + open P&L
            .profit    — current open trade P&L
            .margin    — used margin
            .margin_free — available margin
        or None on failure.
    """
    info = mt5.account_info()
    if info is None:
        log.warning("Could not fetch account info.")
    return info


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 5 ── INDICATORS
#  Add any indicator calculations here.
#  These are called inside get_signal() to enrich the DataFrame.
# ══════════════════════════════════════════════════════════════════════════════

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    ─── STRATEGY HOOK ───────────────────────────────────────────────────────
    Compute and attach technical indicators to the candle DataFrame.
    Called automatically before get_signal() on every tick.

    Instructions:
        - Add new columns to df using pandas_ta or manual calculations.
        - Return the modified df.
        - All indicator columns you add here are available inside get_signal().

    Default implementation: SMA 10 and SMA 20.

    Examples using pandas_ta:
        df["rsi"]      = ta.rsi(df["close"], length=14)
        df["ema_fast"] = ta.ema(df["close"], length=9)
        df["ema_slow"] = ta.ema(df["close"], length=21)
        df["macd"], df["macd_signal"], df["macd_hist"] = ta.macd(df["close"]).T.values
        bbands         = ta.bbands(df["close"], length=20)
        df["bb_upper"] = bbands["BBU_20_2.0"]
        df["bb_lower"] = bbands["BBL_20_2.0"]
    ─────────────────────────────────────────────────────────────────────────
    """
    # ── Default: SMA crossover indicators ────────────────────────────────────
    df["sma_fast"] = ta.sma(df["close"], length=10)
    df["sma_slow"] = ta.sma(df["close"], length=20)
    return df


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 6 ── SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def get_signal(df: pd.DataFrame, log: logging.Logger) -> Optional[Literal["buy", "sell"]]:
    """
    ─── STRATEGY HOOK ───────────────────────────────────────────────────────
    Analyse the enriched candle DataFrame and return a trade signal.

    Parameters:
        df  — DataFrame from get_candles() + add_indicators(). All indicator
              columns you added in add_indicators() are available here.

    Returns:
        "buy"   — open a long position
        "sell"  — open a short position
        None    — no signal; do nothing this tick

    Rules:
        - Always read from df.iloc[-2] (last *closed* candle), never df.iloc[-1]
          (the still-forming candle will give false signals).
        - Validate that indicator values are not NaN before comparing.
        - Log the reason for every signal you generate.

    Default implementation: SMA crossover.
    ─────────────────────────────────────────────────────────────────────────
    """
    # Guard: need at least 2 rows with valid indicator values
    if df is None or len(df) < 3:
        return None

    prev = df.iloc[-3]   # candle before last closed
    last = df.iloc[-2]   # last fully closed candle (use this for signals)

    # Guard: skip if indicators haven't warmed up yet
    if pd.isna(last.get("sma_fast")) or pd.isna(last.get("sma_slow")):
        return None
    if pd.isna(prev.get("sma_fast")) or pd.isna(prev.get("sma_slow")):
        return None

    # ── Default: SMA crossover logic ─────────────────────────────────────────
    bullish_cross = (prev["sma_fast"] <= prev["sma_slow"]) and (last["sma_fast"] > last["sma_slow"])
    bearish_cross = (prev["sma_fast"] >= prev["sma_slow"]) and (last["sma_fast"] < last["sma_slow"])

    if bullish_cross:
        log.info(f"SIGNAL: BUY  | SMA_fast={last['sma_fast']:.5f} crossed above SMA_slow={last['sma_slow']:.5f}")
        return "buy"

    if bearish_cross:
        log.info(f"SIGNAL: SELL | SMA_fast={last['sma_fast']:.5f} crossed below SMA_slow={last['sma_slow']:.5f}")
        return "sell"

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 7 ── RISK MANAGEMENT FILTERS
# ══════════════════════════════════════════════════════════════════════════════

def passes_risk_filter(
    cfg: Config,
    signal: str,
    df: pd.DataFrame,
    log: logging.Logger
) -> bool:
    """
    ─── STRATEGY HOOK ───────────────────────────────────────────────────────
    Secondary gate that can block a signal even after get_signal() fires.
    Use this for session filters, volatility checks, spread checks, etc.

    Parameters:
        cfg    — Config object
        signal — "buy" or "sell" (already validated by get_signal)
        df     — enriched candle DataFrame

    Returns:
        True  — signal is approved; proceed to place_order()
        False — signal is blocked; log the reason and skip

    Default implementation: checks max daily loss limit (if configured)
    and that the current spread is not abnormally wide.
    ─────────────────────────────────────────────────────────────────────────
    """
    # ── Daily loss limit ─────────────────────────────────────────────────────
    if cfg.MAX_DAILY_LOSS > 0:
        account = get_account_info(log)
        if account:
            daily_loss = account.balance - account.equity
            if daily_loss >= cfg.MAX_DAILY_LOSS:
                log.warning(f"RISK BLOCK: Daily loss limit reached (${daily_loss:.2f} >= ${cfg.MAX_DAILY_LOSS:.2f})")
                return False

    # ── Spread check (skip if spread > 5x the normal for this pair) ──────────
    symbol_info = get_symbol_info(cfg.SYMBOL, log)
    if symbol_info:
        spread_pips = symbol_info.spread / 10  # convert points to pips
        if spread_pips > 5:
            log.warning(f"RISK BLOCK: Spread too wide ({spread_pips:.1f} pips). Skipping.")
            return False

    # ── Add additional filters here ───────────────────────────────────────────
    # Examples:
    #   - Trading session filter (London / New York hours only)
    #   - ATR-based volatility check
    #   - News event blackout window

    return True  # approved


def calculate_lot_size(cfg: Config, symbol_info, log: logging.Logger) -> float:
    """
    ─── STRATEGY HOOK ───────────────────────────────────────────────────────
    Determine the trade volume (lot size) for the next order.

    Parameters:
        cfg         — Config object
        symbol_info — mt5.SymbolInfo object for the symbol

    Returns:
        float — lot size to use (must be >= symbol_info.volume_min
                and a multiple of symbol_info.volume_step)

    Default implementation: returns the fixed LOT_SIZE from Config.

    Advanced example (fixed % risk per trade):
        account    = mt5.account_info()
        risk_amt   = account.balance * 0.01      # risk 1% of balance
        pip_value  = 10.0                         # approx for EURUSD at 0.1 lot
        sl_pips    = cfg.STOP_LOSS_PIPS
        lot        = risk_amt / (sl_pips * pip_value)
        lot        = max(symbol_info.volume_min, round(lot, 2))
        return lot
    ─────────────────────────────────────────────────────────────────────────
    """
    return cfg.LOT_SIZE


def calculate_sl_tp(
    cfg: Config,
    signal: str,
    price: float,
    symbol_info,
    log: logging.Logger
) -> tuple[float, float]:
    """
    ─── STRATEGY HOOK ───────────────────────────────────────────────────────
    Compute stop loss and take profit prices for an order.

    Parameters:
        signal      — "buy" or "sell"
        price       — entry price (ask for buy, bid for sell)
        symbol_info — mt5.SymbolInfo (used for .point and .digits)

    Returns:
        (stop_loss_price, take_profit_price)
        Return (0.0, 0.0) to place an order with no SL/TP.

    Default implementation: fixed pip distance from Config.

    Advanced alternatives:
        - ATR-based SL:    sl = price ± (atr_value * 1.5)
        - Support/Resistance levels from df
        - Percentage of price
    ─────────────────────────────────────────────────────────────────────────
    """
    # Disable SL/TP if set to 0 in config
    if cfg.STOP_LOSS_PIPS == 0 and cfg.TAKE_PROFIT_PIPS == 0:
        return 0.0, 0.0

    pip = symbol_info.point * 10  # 1 pip = 10 points on 5-digit brokers
    digits = symbol_info.digits

    if signal == "buy":
        sl = round(price - cfg.STOP_LOSS_PIPS * pip, digits)  if cfg.STOP_LOSS_PIPS   > 0 else 0.0
        tp = round(price + cfg.TAKE_PROFIT_PIPS * pip, digits) if cfg.TAKE_PROFIT_PIPS > 0 else 0.0
    else:  # sell
        sl = round(price + cfg.STOP_LOSS_PIPS * pip, digits)  if cfg.STOP_LOSS_PIPS   > 0 else 0.0
        tp = round(price - cfg.TAKE_PROFIT_PIPS * pip, digits) if cfg.TAKE_PROFIT_PIPS > 0 else 0.0

    return sl, tp


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 8 ── ORDER EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def place_order(cfg: Config, signal: str, log: logging.Logger) -> bool:
    """
    Build and send a market order to MT5.

    Parameters:
        signal — "buy" or "sell"

    Returns:
        True  — order placed successfully.
        False — order failed (logged with reason).

    Notes:
        - Uses ORDER_FILLING_IOC (Immediate Or Cancel). Some brokers require
          ORDER_FILLING_FOK or ORDER_FILLING_RETURN. Change if orders fail
          with retcode 10030.
        - The magic number (Config.MAGIC) lets you filter this bot's trades
          in MT5 terminal and in positions queries.
    """
    symbol_info = get_symbol_info(cfg.SYMBOL, log)
    if symbol_info is None:
        return False

    tick = get_tick(cfg.SYMBOL, log)
    if tick is None:
        return False

    # Determine order type and entry price
    if signal == "buy":
        order_type = mt5.ORDER_TYPE_BUY
        price      = tick.ask
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price      = tick.bid

    lot = calculate_lot_size(cfg, symbol_info, log)
    sl, tp = calculate_sl_tp(cfg, signal, price, symbol_info, log)

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       cfg.SYMBOL,
        "volume":       lot,
        "type":         order_type,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "deviation":    cfg.DEVIATION,
        "magic":        cfg.MAGIC,
        "comment":      f"{cfg.COMMENT}_{signal}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(
            f"ORDER OPENED | {signal.upper()} {lot} lot @ {price:.5f} "
            f"| SL: {sl:.5f}  TP: {tp:.5f}  Ticket: #{result.order}"
        )
        return True
    else:
        log.error(f"ORDER FAILED | retcode={result.retcode}  comment={result.comment}")
        return False


def close_position(position, log: logging.Logger) -> bool:
    """
    Close a single open position by its ticket.

    Parameters:
        position — an element from mt5.positions_get() results

    Returns:
        True on success, False on failure.
    """
    symbol = position.symbol
    tick   = mt5.symbol_info_tick(symbol)

    # To close a BUY we send a SELL, and vice versa
    close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price      = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       position.volume,
        "type":         close_type,
        "position":     position.ticket,
        "price":        price,
        "deviation":    20,
        "magic":        position.magic,
        "comment":      "bot_close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"POSITION CLOSED | Ticket #{position.ticket} | P&L: ${position.profit:.2f}")
        return True
    else:
        log.error(f"CLOSE FAILED | Ticket #{position.ticket} | {result.comment}")
        return False


def close_all_positions(cfg: Config, log: logging.Logger) -> None:
    """
    Close every open position for the configured symbol.
    Called on bot shutdown (Ctrl+C) and optionally on signal reversal.
    """
    positions = mt5.positions_get(symbol=cfg.SYMBOL)
    if not positions:
        log.info("No open positions to close.")
        return
    for pos in positions:
        close_position(pos, log)


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 9 ── POSITION STATE QUERIES
# ══════════════════════════════════════════════════════════════════════════════

def get_open_positions(cfg: Config) -> list:
    """
    Return a list of all open positions for the configured symbol and magic number.

    Returns:
        List of mt5.TradePosition objects (empty list if none).
    """
    positions = mt5.positions_get(symbol=cfg.SYMBOL)
    if not positions:
        return []
    # Filter by magic number so the bot only manages its own trades
    return [p for p in positions if p.magic == cfg.MAGIC]


def is_at_max_trades(cfg: Config) -> bool:
    """
    Check if the bot has already reached its MAX_OPEN_TRADES limit.

    Returns:
        True  — at or above limit; do not open new trades.
        False — below limit; new trade is allowed.
    """
    return len(get_open_positions(cfg)) >= cfg.MAX_OPEN_TRADES


def get_current_direction(cfg: Config) -> Optional[Literal["buy", "sell"]]:
    """
    Return the direction of the existing open position, if any.

    Returns:
        "buy"  — currently holding a long position
        "sell" — currently holding a short position
        None   — no open position
    """
    positions = get_open_positions(cfg)
    if not positions:
        return None
    # mt5.ORDER_TYPE_BUY == 0, mt5.ORDER_TYPE_SELL == 1
    return "buy" if positions[0].type == mt5.ORDER_TYPE_BUY else "sell"


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 10 ── LIFECYCLE HOOKS
#  These run at specific points in the bot's lifecycle.
#  Override them to add custom behaviour without touching the main loop.
# ══════════════════════════════════════════════════════════════════════════════

def on_start(cfg: Config, log: logging.Logger) -> None:
    """
    ─── LIFECYCLE HOOK ──────────────────────────────────────────────────────
    Called once after successful MT5 connection, before the main loop begins.

    Use for:
        - Printing strategy parameters to the log
        - Pre-fetching any warmup data
        - Sending a startup notification (Telegram, email, etc.)
        - Any one-time initialisation your strategy needs
    ─────────────────────────────────────────────────────────────────────────
    """
    log.info(f"Bot started | Symbol: {cfg.SYMBOL} | TF: {cfg.TIMEFRAME} | Lot: {cfg.LOT_SIZE}")
    log.info(f"Strategy   | SMA crossover | SL: {cfg.STOP_LOSS_PIPS} pips | TP: {cfg.TAKE_PROFIT_PIPS} pips")


def on_tick(cfg: Config, df: pd.DataFrame, log: logging.Logger) -> None:
    """
    ─── LIFECYCLE HOOK ──────────────────────────────────────────────────────
    Called on every iteration of the main loop, regardless of signal.

    Use for:
        - Printing current indicator values to the log
        - Updating a live dashboard
        - Recording tick data to a database
        - Trailing stop logic (modify existing SL as price moves)

    Default implementation: logs current price and account equity.
    ─────────────────────────────────────────────────────────────────────────
    """
    tick    = get_tick(cfg.SYMBOL, log)
    account = get_account_info(log)
    if tick and account:
        log.info(
            f"Price: {tick.bid:.5f}/{tick.ask:.5f} "
            f"| Balance: ${account.balance:.2f}  Equity: ${account.equity:.2f}"
            f"  P&L: ${account.profit:.2f}"
        )


def on_trade_open(cfg: Config, signal: str, log: logging.Logger) -> None:
    """
    ─── LIFECYCLE HOOK ──────────────────────────────────────────────────────
    Called immediately after a new order is placed successfully.

    Parameters:
        signal — "buy" or "sell" that was just executed

    Use for:
        - Sending a trade notification (Telegram bot, email, SMS)
        - Logging the trade to a spreadsheet or database
        - Setting a timer for position review
    ─────────────────────────────────────────────────────────────────────────
    """
    pass  # Add your notification or logging code here


def on_trade_close(cfg: Config, log: logging.Logger) -> None:
    """
    ─── LIFECYCLE HOOK ──────────────────────────────────────────────────────
    Called immediately after a position is closed.

    Use for:
        - Sending a close notification with P&L
        - Recording the trade result
        - Updating a performance dashboard
    ─────────────────────────────────────────────────────────────────────────
    """
    pass  # Add your notification or logging code here


def on_error(error_msg: str, log: logging.Logger) -> None:
    """
    ─── LIFECYCLE HOOK ──────────────────────────────────────────────────────
    Called whenever a recoverable error occurs inside the main loop
    (e.g. failed data fetch, order rejection).

    Parameters:
        error_msg — human-readable description of what went wrong

    Use for:
        - Sending an alert when something goes wrong
        - Incrementing an error counter to trigger a circuit breaker
        - Writing to a separate error log
    ─────────────────────────────────────────────────────────────────────────
    """
    log.error(f"ERROR: {error_msg}")


def on_stop(cfg: Config, log: logging.Logger) -> None:
    """
    ─── LIFECYCLE HOOK ──────────────────────────────────────────────────────
    Called when the bot is shutting down (after Ctrl+C or fatal error),
    before positions are closed and MT5 is disconnected.

    Use for:
        - Sending a shutdown notification
        - Flushing any buffered data to disk
        - Printing a final performance summary
    ─────────────────────────────────────────────────────────────────────────
    """
    account = get_account_info(log)
    if account:
        log.info(f"Final Balance: ${account.balance:.2f} | Final Equity: ${account.equity:.2f}")
    log.info("Bot shutting down...")


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 11 ── MAIN LOOP
#  Core engine — no edits needed here.
#  Customise behaviour through the hooks in Section 10.
# ══════════════════════════════════════════════════════════════════════════════

def run(cfg: Config) -> None:
    """
    Main execution loop.

    Flow per tick:
        1. Fetch candles
        2. Add indicators
        3. Call on_tick() hook
        4. Check for signal
        5. Run risk filter
        6. Handle position (reverse, skip, or open)
        7. Sleep until next tick
    """
    log = _setup_logger(cfg)

    if not connect(cfg, log):
        log.error("Connection failed. Exiting.")
        return

    on_start(cfg, log)

    try:
        while True:
            log.info(f"{'─'*50}  {datetime.now().strftime('%H:%M:%S')}")

            # ── 1. Fetch data ──────────────────────────────────────────────
            df = get_candles(cfg, log)
            if df is None:
                on_error("Failed to fetch candle data", log)
                time.sleep(cfg.SLEEP_SECONDS)
                continue

            # ── 2. Add indicators ──────────────────────────────────────────
            df = add_indicators(df)

            # ── 3. on_tick hook ────────────────────────────────────────────
            on_tick(cfg, df, log)

            # ── 4. Get signal ──────────────────────────────────────────────
            signal = get_signal(df, log)

            if signal is None:
                log.info("No signal this tick.")
                time.sleep(cfg.SLEEP_SECONDS)
                continue

            # ── 5. Risk filter ─────────────────────────────────────────────
            if not passes_risk_filter(cfg, signal, df, log):
                time.sleep(cfg.SLEEP_SECONDS)
                continue

            # ── 6. Position logic ──────────────────────────────────────────
            current_direction = get_current_direction(cfg)

            if current_direction == signal:
                # Already in the same direction — do nothing
                log.info(f"Already in a {signal.upper()} position. Holding.")

            elif current_direction is not None and current_direction != signal:
                # Signal reversed — close existing and open new
                log.info(f"Signal reversed ({current_direction} → {signal}). Closing and reversing.")
                close_all_positions(cfg, log)
                on_trade_close(cfg, log)
                time.sleep(1)   # brief pause before opening new order
                if place_order(cfg, signal, log):
                    on_trade_open(cfg, signal, log)

            else:
                # No position open — open fresh
                if not is_at_max_trades(cfg):
                    if place_order(cfg, signal, log):
                        on_trade_open(cfg, signal, log)
                else:
                    log.info(f"Max open trades ({cfg.MAX_OPEN_TRADES}) reached. Skipping.")

            time.sleep(cfg.SLEEP_SECONDS)

    except KeyboardInterrupt:
        log.info("Keyboard interrupt received (Ctrl+C).")

    except Exception as e:
        log.exception(f"Unexpected error in main loop: {e}")

    finally:
        on_stop(cfg, log)
        close_all_positions(cfg, log)
        disconnect(log)


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    config = Config()
    run(config)
