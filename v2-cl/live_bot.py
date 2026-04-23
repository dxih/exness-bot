"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  live_bot.py  –  NAS100 FVG Strategy  |  Live / Demo Trading               ║
║  Built on: mt5_bot_framework.py                                             ║
║                                                                             ║
║  QUICK START                                                                ║
║  1. Set LOGIN, PASSWORD, SERVER, SYMBOL below.                              ║
║  2. Confirm your broker's NAS100 symbol name in Market Watch.               ║
║  3. Run:  python live_bot.py                                                ║
║  4. Stop:  Ctrl+C  (open trades are closed automatically)                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
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
except ImportError as e:
    sys.exit(f"[FATAL] Missing dependency: {e}\nRun:  pip install MetaTrader5 pandas")

# ── Strategy ──────────────────────────────────────────────────────────────────
from strategy import (
    SetupState,
    add_indicators,
    get_signal,
    compute_sl_tp,
    in_session,
    MIN_CANDLES_REQUIRED,
)

# ── Framework helpers (re-used directly) ──────────────────────────────────────
from mt5_bot_framework import (
    _setup_logger,
    connect,
    disconnect,
    get_candles,
    get_tick,
    get_symbol_info,
    get_account_info,
    get_open_positions,
    get_current_direction,
    is_at_max_trades,
    close_all_positions,
)


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  ← edit this block
# ══════════════════════════════════════════════════════════════════════════════

class Config:
    # ── Account ───────────────────────────────────────────────────────────────
    LOGIN:    int = 0                       # Your MT5 account number
    PASSWORD: str = ""                      # Your MT5 password
    SERVER:   str = "Exness-MT5Trial"       # Server (check MT5 login screen)

    # ── Instrument ────────────────────────────────────────────────────────────
    # Common NAS100 symbol names by broker:
    #   Exness       →  "NAS100"  or  "NAS100m"
    #   IC Markets   →  "US100+"  or  "USTEC"
    #   FTMO / prop  →  "NAS100"
    SYMBOL:    str = "NAS100m"
    TIMEFRAME: int = mt5.TIMEFRAME_M1       # 1-minute chart (do not change)

    # ── Risk / Position Sizing ────────────────────────────────────────────────
    LOT_SIZE:       float = 0.10            # Lot size per trade
    MAX_OPEN_TRADES: int  = 1               # Max concurrent trades (keep at 1)
    MAX_DAILY_LOSS: float = 0.0             # Daily loss cap in $  (0 = disabled)

    # ── Candle History ────────────────────────────────────────────────────────
    CANDLE_COUNT: int = 150                 # Keep ≥ MIN_CANDLES_REQUIRED × 2

    # ── Loop Timing ───────────────────────────────────────────────────────────
    SLEEP_SECONDS: int = 30                 # Poll every 30 s on M1 chart

    # ── MT5 Order Settings ────────────────────────────────────────────────────
    DEVIATION: int = 50                     # Max slippage in points (NAS100 moves fast)
    MAGIC:     int = 20240601               # Unique bot ID tag
    COMMENT:   str = "FVG_NAS100"

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_FILE:  str = "nas100_fvg_live.log"
    LOG_LEVEL: int = logging.INFO


# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOM ORDER EXECUTION  (SL/TP derived from swept pivot, not pip distance)
# ══════════════════════════════════════════════════════════════════════════════

def place_fvg_order(
    cfg: Config,
    signal: str,
    swept_pivot: float,
    log: logging.Logger,
) -> bool:
    """
    Place a market order with SL/TP calculated from the swept pivot level.

    Parameters
    ----------
    signal       : 'buy' or 'sell'
    swept_pivot  : the low (buy) or high (sell) that was swept before entry

    Returns
    -------
    True on successful fill, False otherwise.
    """
    sym_info = get_symbol_info(cfg.SYMBOL, log)
    if sym_info is None:
        return False

    tick = get_tick(cfg.SYMBOL, log)
    if tick is None:
        return False

    if signal == "buy":
        order_type = mt5.ORDER_TYPE_BUY
        entry      = tick.ask
    else:
        order_type = mt5.ORDER_TYPE_SELL
        entry      = tick.bid

    sl, tp = compute_sl_tp(signal, entry, swept_pivot)

    # Validate SL/TP are on the correct side of entry
    if signal == "buy"  and (sl >= entry or tp <= entry):
        log.error(f"SL/TP sanity check failed for BUY  | entry={entry} SL={sl} TP={tp}")
        return False
    if signal == "sell" and (sl <= entry or tp >= entry):
        log.error(f"SL/TP sanity check failed for SELL | entry={entry} SL={sl} TP={tp}")
        return False

    # Clamp lot to broker limits
    lot = max(sym_info.volume_min, cfg.LOT_SIZE)
    lot = round(lot / sym_info.volume_step) * sym_info.volume_step

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       cfg.SYMBOL,
        "volume":       lot,
        "type":         order_type,
        "price":        entry,
        "sl":           sl,
        "tp":           tp,
        "deviation":    cfg.DEVIATION,
        "magic":        cfg.MAGIC,
        "comment":      f"{cfg.COMMENT}_{signal}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None:
        log.error(f"order_send returned None — {mt5.last_error()}")
        return False

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        risk_pts = abs(entry - sl)
        log.info(
            f"✅ ORDER FILLED | {signal.upper()} {lot} lot @ {entry:.2f} "
            f"| SL={sl:.2f}  TP={tp:.2f} "
            f"| Risk={risk_pts:.2f} pts  Ticket=#{result.order}"
        )
        return True

    # Some brokers require ORDER_FILLING_FOK or ORDER_FILLING_RETURN
    # If you see retcode 10030, change type_filling above.
    log.error(f"❌ ORDER FAILED | retcode={result.retcode}  comment={result.comment}")
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  LIFECYCLE HOOKS
# ══════════════════════════════════════════════════════════════════════════════

def on_start(cfg: Config, log: logging.Logger) -> None:
    log.info("═" * 60)
    log.info("  NAS100 FVG STRATEGY — LIVE BOT STARTING")
    log.info(f"  Symbol    : {cfg.SYMBOL}")
    log.info(f"  Timeframe : M1")
    log.info(f"  Session   : 09:50 – 11:10 NY")
    log.info(f"  Lot size  : {cfg.LOT_SIZE}")
    log.info(f"  Magic #   : {cfg.MAGIC}")
    log.info("═" * 60)


def on_tick(cfg: Config, df: pd.DataFrame, log: logging.Logger, state: SetupState) -> None:
    tick    = get_tick(cfg.SYMBOL, log)
    account = get_account_info(log)
    if tick and account:
        ts = df.index[-2]
        log.info(
            f"Bid={tick.bid:.2f} Ask={tick.ask:.2f} "
            f"| Equity=${account.equity:,.2f}  P&L=${account.profit:+.2f} "
            f"| Phase={state.phase}"
        )


def on_trade_open(cfg: Config, signal: str, log: logging.Logger) -> None:
    """Add Telegram / email notification here if desired."""
    log.info(f"🟢 Trade opened: {signal.upper()} on {cfg.SYMBOL}")


def on_trade_close(cfg: Config, log: logging.Logger) -> None:
    log.info(f"🔴 Position closed on {cfg.SYMBOL}.")


def on_stop(cfg: Config, log: logging.Logger) -> None:
    account = get_account_info(log)
    if account:
        log.info(f"Final Balance: ${account.balance:,.2f} | Equity: ${account.equity:,.2f}")
    log.info("Bot shut down cleanly.")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run(cfg: Config) -> None:
    log   = _setup_logger(cfg)
    state = SetupState()   # fresh state per session

    if not connect(cfg, log):
        log.error("MT5 connection failed. Exiting.")
        return

    on_start(cfg, log)

    try:
        while True:
            log.info(f"{'─' * 55}  {datetime.now().strftime('%H:%M:%S')}")

            # ── 1. Fetch candles ───────────────────────────────────────────
            df = get_candles(cfg, log)
            if df is None:
                log.warning("Candle fetch failed — retrying next cycle.")
                time.sleep(cfg.SLEEP_SECONDS)
                continue

            # ── 2. Indicators (pass-through for this strategy) ────────────
            df = add_indicators(df)

            # ── 3. on_tick hook ────────────────────────────────────────────
            on_tick(cfg, df, log, state)

            # ── 4. Session reset at start of each new session ─────────────
            last_time = df.index[-2]
            if not in_session(last_time) and state.phase != "idle":
                log.info("Session ended — resetting strategy state.")
                state.reset()

            # ── 5. Signal ─────────────────────────────────────────────────
            # Save swept_pivot BEFORE calling get_signal (it resets after signal)
            pivot_snapshot = state.swept_pivot
            signal = get_signal(df, log, state)

            if signal is None:
                time.sleep(cfg.SLEEP_SECONDS)
                continue

            # ── 6. Risk filter ─────────────────────────────────────────────
            if not _passes_risk_filter(cfg, log):
                time.sleep(cfg.SLEEP_SECONDS)
                continue

            # ── 7. Position management ────────────────────────────────────
            current_dir = get_current_direction(cfg)

            if current_dir == signal:
                log.info(f"Already in a {signal.upper()} — holding.")

            elif current_dir is not None and current_dir != signal:
                log.info(f"Signal flipped {current_dir}→{signal} — closing then reversing.")
                close_all_positions(cfg, log)
                on_trade_close(cfg, log)
                time.sleep(1)
                if place_fvg_order(cfg, signal, pivot_snapshot, log):
                    on_trade_open(cfg, signal, log)

            else:
                if not is_at_max_trades(cfg):
                    if place_fvg_order(cfg, signal, pivot_snapshot, log):
                        on_trade_open(cfg, signal, log)
                else:
                    log.info(f"Max trades ({cfg.MAX_OPEN_TRADES}) reached — skipping.")

            time.sleep(cfg.SLEEP_SECONDS)

    except KeyboardInterrupt:
        log.info("Ctrl+C received — shutting down gracefully.")

    except Exception as exc:
        log.exception(f"Unexpected error in main loop: {exc}")

    finally:
        on_stop(cfg, log)
        close_all_positions(cfg, log)
        disconnect(log)


# ══════════════════════════════════════════════════════════════════════════════
#  INTERNAL RISK FILTER
# ══════════════════════════════════════════════════════════════════════════════

def _passes_risk_filter(cfg: Config, log: logging.Logger) -> bool:
    """Check daily loss limit and spread before placing an order."""
    # Daily loss cap
    if cfg.MAX_DAILY_LOSS > 0:
        account = get_account_info(log)
        if account:
            daily_loss = account.balance - account.equity
            if daily_loss >= cfg.MAX_DAILY_LOSS:
                log.warning(f"RISK BLOCK: Daily loss ${daily_loss:.2f} ≥ limit ${cfg.MAX_DAILY_LOSS:.2f}")
                return False

    # Spread sanity (NAS100 spread is normally < 3 pts during RTH)
    sym_info = mt5.symbol_info(cfg.SYMBOL)
    if sym_info and sym_info.spread > 20:   # 20 points = roughly 2 pts on NAS100
        log.warning(f"RISK BLOCK: Spread too wide ({sym_info.spread} pts). Skipping.")
        return False

    return True


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    config = Config()
    run(config)
