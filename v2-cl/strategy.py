"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  NAS100  –  Fair Value Gap (FVG) Reversal Strategy                         ║
║  Market    : NAS100 (US100, NAS100m, or broker-specific symbol)            ║
║  Timeframe : M1                                                             ║
║  Session   : 09:50 – 11:10 New York time only                              ║
║                                                                             ║
║  LOGIC OVERVIEW                                                             ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  LONG setup                                                                 ║
║    1. Price sweeps a recent swing LOW (breaks it)                           ║
║    2. Price reverses and breaks the preceding swing HIGH  →  displacement   ║
║    3. That impulse up leaves a Fair Value Gap (3-candle pattern)            ║
║    4. Price retraces INTO the FVG  →  BUY                                  ║
║    SL : 1 handle (1 point) below the swept low                             ║
║    TP : 2 × Risk (2R)                                                       ║
║                                                                             ║
║  SHORT setup (mirror image)                                                 ║
║    1. Price sweeps a recent swing HIGH                                      ║
║    2. Price breaks the preceding swing LOW  →  displacement                 ║
║    3. That impulse down leaves an FVG                                       ║
║    4. Price retraces INTO the FVG  →  SELL                                  ║
║    SL : 1 handle (1 point) above the swept high                            ║
║    TP : 2 × Risk (2R)                                                       ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Literal
from zoneinfo import ZoneInfo   # Python 3.9+  (pip install tzdata on Windows)

import pandas as pd

# ── NY timezone ───────────────────────────────────────────────────────────────
NY_TZ = ZoneInfo("America/New_York")

# ── Strategy tunables ─────────────────────────────────────────────────────────
SESSION_START_H, SESSION_START_M = 9, 50    # 09:50 NY
SESSION_END_H,   SESSION_END_M   = 11, 10   # 11:10 NY

SWING_LOOKBACK   = 10    # candles to look left when detecting swing highs/lows
SL_HANDLE        = 1.0   # 1 NAS100 point buffer beyond the swept pivot
RISK_REWARD      = 2.0   # TP = entry ± (SL distance × RISK_REWARD)

# ── Candles needed in history ─────────────────────────────────────────────────
MIN_CANDLES_REQUIRED = SWING_LOOKBACK * 3 + 5


# ══════════════════════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FVG:
    """A three-candle Fair Value Gap."""
    direction: Literal["bull", "bear"]
    top: float          # upper boundary of the gap
    bottom: float       # lower boundary of the gap
    formed_at: int      # DataFrame integer index of the middle candle


@dataclass
class SetupState:
    """
    Carries all detected structure between calls to get_signal().
    One instance is created per bot session and mutated each tick.
    """
    # Phase tracking
    phase: Literal["idle", "swept", "displaced"] = "idle"
    direction: Optional[Literal["bull", "bear"]] = None   # direction of the SETUP (bull = buy)

    # Key price levels
    swept_pivot: Optional[float] = None     # the low (bull) or high (bear) that was swept
    structure_break: Optional[float] = None # the high (bull) or low (bear) that was broken

    # Pending FVG waiting for a retracement entry
    pending_fvg: Optional[FVG] = None

    def reset(self) -> None:
        self.phase          = "idle"
        self.direction      = None
        self.swept_pivot    = None
        self.structure_break = None
        self.pending_fvg    = None


# ── Global state singleton (used by live_bot) ─────────────────────────────────
_state = SetupState()


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def in_session(ts: pd.Timestamp) -> bool:
    """Return True if *ts* (UTC-aware or naive broker time) is inside the NY session window."""
    ny = ts.tz_convert(NY_TZ) if ts.tzinfo else ts.tz_localize("UTC").tz_convert(NY_TZ)
    start = ny.replace(hour=SESSION_START_H, minute=SESSION_START_M, second=0, microsecond=0)
    end   = ny.replace(hour=SESSION_END_H,   minute=SESSION_END_M,   second=0, microsecond=0)
    return start <= ny <= end


def recent_swing_low(df: pd.DataFrame, before_idx: int, lookback: int = SWING_LOOKBACK) -> Optional[float]:
    """
    Return the lowest 'low' in the *lookback* candles ending at (before_idx - 1).
    This is used as the "recent swing low" we watch to be swept.
    """
    start = max(0, before_idx - lookback)
    window = df.iloc[start:before_idx]
    if window.empty:
        return None
    return float(window["low"].min())


def recent_swing_high(df: pd.DataFrame, before_idx: int, lookback: int = SWING_LOOKBACK) -> Optional[float]:
    """
    Return the highest 'high' in the *lookback* candles ending at (before_idx - 1).
    """
    start = max(0, before_idx - lookback)
    window = df.iloc[start:before_idx]
    if window.empty:
        return None
    return float(window["high"].max())


def find_fvg_bull(df: pd.DataFrame, from_idx: int) -> Optional[FVG]:
    """
    Scan forward from *from_idx* looking for a bullish FVG (up-displacement candle).

    Bullish FVG pattern (three consecutive candles i-1, i, i+1):
        candle[i+1].low  >  candle[i-1].high    ← gap between them
        candle[i]        is the displacement (impulse) candle

    Returns the MOST RECENT (latest) bullish FVG found, or None.
    """
    best: Optional[FVG] = None
    n = len(df)
    for i in range(from_idx + 1, n - 1):
        prev_high = df.iloc[i - 1]["high"]
        next_low  = df.iloc[i + 1]["low"]
        if next_low > prev_high:                 # gap exists
            best = FVG(
                direction="bull",
                top=next_low,
                bottom=prev_high,
                formed_at=i,
            )
    return best


def find_fvg_bear(df: pd.DataFrame, from_idx: int) -> Optional[FVG]:
    """
    Scan forward from *from_idx* looking for a bearish FVG (down-displacement candle).

    Bearish FVG pattern (three consecutive candles i-1, i, i+1):
        candle[i+1].high  <  candle[i-1].low    ← gap below
        candle[i]         is the displacement (impulse) candle

    Returns the MOST RECENT (latest) bearish FVG found, or None.
    """
    best: Optional[FVG] = None
    n = len(df)
    for i in range(from_idx + 1, n - 1):
        prev_low   = df.iloc[i - 1]["low"]
        next_high  = df.iloc[i + 1]["high"]
        if next_high < prev_low:                 # gap exists below
            best = FVG(
                direction="bear",
                top=prev_low,
                bottom=next_high,
                formed_at=i,
            )
    return best


def price_inside_fvg(price: float, fvg: FVG) -> bool:
    """Return True if *price* is inside the FVG boundaries."""
    return fvg.bottom <= price <= fvg.top


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS HOOK  (no technical indicators needed — price action only)
# ══════════════════════════════════════════════════════════════════════════════

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Pass-through — this strategy reads raw OHLC candles only."""
    return df


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def get_signal(
    df: pd.DataFrame,
    log: logging.Logger,
    state: Optional[SetupState] = None,
) -> Optional[Literal["buy", "sell"]]:
    """
    Evaluate the current candle DataFrame and return 'buy', 'sell', or None.

    This function is STATEFUL — it progresses through three phases:
        idle       →  watching for a swing sweep
        swept      →  low/high swept; watching for displacement + FVG
        displaced  →  FVG formed; watching for price to retrace into it

    Parameters
    ----------
    df    : DataFrame with columns open, high, low, close (index = time).
            Must contain at least MIN_CANDLES_REQUIRED rows.
    log   : Logger.
    state : SetupState instance.  Uses module-level _state if None.

    Returns
    -------
    'buy' | 'sell' | None
    """
    if state is None:
        state = _state

    if df is None or len(df) < MIN_CANDLES_REQUIRED:
        return None

    # ── Session gate ──────────────────────────────────────────────────────────
    # df index may be UTC; convert for the session check
    last_time = df.index[-2]   # last closed candle timestamp
    if isinstance(last_time, pd.Timestamp):
        if not in_session(last_time):
            if state.phase != "idle":
                log.info("Outside session window – resetting state.")
                state.reset()
            return None
    # ─────────────────────────────────────────────────────────────────────────

    # We always work off the *last closed* candle (index -2).
    # Index -1 is the still-forming candle.
    n        = len(df)
    last_idx = n - 2          # integer position of last closed candle
    candle   = df.iloc[last_idx]
    curr_low  = float(candle["low"])
    curr_high = float(candle["high"])
    curr_close= float(candle["close"])

    # ══════════════════════════════════════════════════════════════════════════
    #  PHASE: IDLE  →  detect a swing sweep
    # ══════════════════════════════════════════════════════════════════════════
    if state.phase == "idle":
        # ── Bullish sweep: price breaks a recent swing LOW then closes above it
        sw_low = recent_swing_low(df, last_idx)
        if sw_low is not None and curr_low < sw_low and curr_close > sw_low:
            state.phase       = "swept"
            state.direction   = "bull"
            state.swept_pivot = sw_low
            log.info(f"[FVG] BULL sweep detected | swept low={sw_low:.2f} | candle={last_idx}")
            return None

        # ── Bearish sweep: price breaks a recent swing HIGH then closes below it
        sw_high = recent_swing_high(df, last_idx)
        if sw_high is not None and curr_high > sw_high and curr_close < sw_high:
            state.phase       = "swept"
            state.direction   = "bear"
            state.swept_pivot = sw_high
            log.info(f"[FVG] BEAR sweep detected | swept high={sw_high:.2f} | candle={last_idx}")
            return None

    # ══════════════════════════════════════════════════════════════════════════
    #  PHASE: SWEPT  →  wait for displacement that breaks the structure high/low
    #                   and leaves an FVG
    # ══════════════════════════════════════════════════════════════════════════
    elif state.phase == "swept":
        swept_at_idx = _find_sweep_candle_idx(df, state)

        if state.direction == "bull":
            # Need price to break above the recent swing HIGH (before the sweep)
            sw_high = recent_swing_high(df, swept_at_idx or last_idx)
            if sw_high is None:
                return None
            if curr_high > sw_high:
                # Structure broken upward — look for bullish FVG in the move
                search_from = (swept_at_idx or last_idx - SWING_LOOKBACK)
                fvg = find_fvg_bull(df, search_from)
                if fvg:
                    state.phase          = "displaced"
                    state.structure_break = sw_high
                    state.pending_fvg    = fvg
                    log.info(
                        f"[FVG] BULL displacement | broke high={sw_high:.2f} "
                        f"| FVG [{fvg.bottom:.2f} – {fvg.top:.2f}]"
                    )
                else:
                    log.info(f"[FVG] BULL structure break but no FVG found — resetting.")
                    state.reset()

        else:  # bear
            sw_low = recent_swing_low(df, swept_at_idx or last_idx)
            if sw_low is None:
                return None
            if curr_low < sw_low:
                search_from = (swept_at_idx or last_idx - SWING_LOOKBACK)
                fvg = find_fvg_bear(df, search_from)
                if fvg:
                    state.phase          = "displaced"
                    state.structure_break = sw_low
                    state.pending_fvg    = fvg
                    log.info(
                        f"[FVG] BEAR displacement | broke low={sw_low:.2f} "
                        f"| FVG [{fvg.bottom:.2f} – {fvg.top:.2f}]"
                    )
                else:
                    log.info(f"[FVG] BEAR structure break but no FVG found — resetting.")
                    state.reset()

    # ══════════════════════════════════════════════════════════════════════════
    #  PHASE: DISPLACED  →  wait for price to retrace into the FVG
    # ══════════════════════════════════════════════════════════════════════════
    elif state.phase == "displaced":
        fvg = state.pending_fvg
        if fvg is None:
            state.reset()
            return None

        if state.direction == "bull":
            # Entry when price pulls back INTO the FVG (close or low touches it)
            if price_inside_fvg(curr_low, fvg) or price_inside_fvg(curr_close, fvg):
                sl_price = state.swept_pivot - SL_HANDLE
                entry    = curr_close               # approximate; live bot uses ask
                risk     = entry - sl_price
                tp_price = entry + risk * RISK_REWARD

                log.info(
                    f"[FVG] BUY SIGNAL | entry≈{entry:.2f} "
                    f"| SL={sl_price:.2f} | TP={tp_price:.2f} "
                    f"| R={risk:.2f}  TP={tp_price:.2f}"
                )
                state.reset()
                return "buy"

            # Invalidate if price runs through the FVG top without entering
            # (momentum resuming without a clean retracement)
            if curr_close > fvg.top * 1.002:   # 0.2% buffer above gap top
                log.info("[FVG] BULL FVG missed (price ran through) — resetting.")
                state.reset()

        else:  # bear
            if price_inside_fvg(curr_high, fvg) or price_inside_fvg(curr_close, fvg):
                sl_price = state.swept_pivot + SL_HANDLE
                entry    = curr_close
                risk     = sl_price - entry
                tp_price = entry - risk * RISK_REWARD

                log.info(
                    f"[FVG] SELL SIGNAL | entry≈{entry:.2f} "
                    f"| SL={sl_price:.2f} | TP={tp_price:.2f} "
                    f"| R={risk:.2f}"
                )
                state.reset()
                return "sell"

            if curr_close < fvg.bottom * 0.998:
                log.info("[FVG] BEAR FVG missed (price ran through) — resetting.")
                state.reset()

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  SL / TP CALCULATOR  (called by live_bot and backtester)
# ══════════════════════════════════════════════════════════════════════════════

def compute_sl_tp(
    signal: str,
    entry_price: float,
    swept_pivot: float,
) -> tuple[float, float]:
    """
    Compute exact SL and TP prices from the swept pivot price level.

    Parameters
    ----------
    signal       : 'buy' or 'sell'
    entry_price  : actual fill price (ask for buy, bid for sell)
    swept_pivot  : the low (buy) or high (sell) that was swept

    Returns
    -------
    (stop_loss_price, take_profit_price)
    """
    if signal == "buy":
        sl   = swept_pivot - SL_HANDLE
        risk = entry_price - sl
        tp   = entry_price + risk * RISK_REWARD
    else:
        sl   = swept_pivot + SL_HANDLE
        risk = sl - entry_price
        tp   = entry_price - risk * RISK_REWARD
    return round(sl, 2), round(tp, 2)


# ══════════════════════════════════════════════════════════════════════════════
#  INTERNAL HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _find_sweep_candle_idx(df: pd.DataFrame, state: SetupState) -> Optional[int]:
    """
    Walk backwards through df to find the candle index where the sweep occurred
    (i.e. the candle whose low < swept_pivot for bull, or high > swept_pivot for bear).
    Returns None if not found (shouldn't happen in practice).
    """
    if state.swept_pivot is None:
        return None
    n = len(df)
    for i in range(n - 2, max(n - SWING_LOOKBACK * 2, 0), -1):
        c = df.iloc[i]
        if state.direction == "bull" and float(c["low"]) < state.swept_pivot:
            return i
        if state.direction == "bear" and float(c["high"]) > state.swept_pivot:
            return i
    return None
