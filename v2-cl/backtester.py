"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  backtester.py  –  NAS100 FVG Strategy Backtester                          ║
║                                                                             ║
║  Runs entirely offline — NO MT5 connection required.                        ║
║  Feed it a CSV of M1 OHLCV data and it simulates every trade.               ║
║                                                                             ║
║  USAGE                                                                      ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  1. Export M1 data from MT5:                                                ║
║       Tools → History Center → NAS100 → M1 → Export to CSV                ║
║     OR download from your broker's portal.                                  ║
║                                                                             ║
║  2. Run:                                                                    ║
║       python backtester.py --csv nas100_m1.csv                              ║
║       python backtester.py --csv nas100_m1.csv --lot 0.1 --start 2024-01-01║
║                                                                             ║
║  CSV FORMAT EXPECTED  (standard MT5 export)                                 ║
║    Date,Time,Open,High,Low,Close,Volume                                     ║
║    OR                                                                       ║
║    <DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<TICKVOL>                     ║
║  (The loader is flexible — it auto-detects common formats)                  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import argparse
import logging
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

# ── Strategy imports ──────────────────────────────────────────────────────────
from strategy import (
    SetupState,
    add_indicators,
    get_signal,
    compute_sl_tp,
    in_session,
    MIN_CANDLES_REQUIRED,
    SL_HANDLE,
    RISK_REWARD,
)

NY_TZ = ZoneInfo("America/New_York")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("backtester")


# ══════════════════════════════════════════════════════════════════════════════
#  TRADE RECORD
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    direction:   str            # 'buy' | 'sell'
    entry_time:  pd.Timestamp
    entry_price: float
    sl:          float
    tp:          float
    lot:         float
    exit_time:   Optional[pd.Timestamp] = None
    exit_price:  Optional[float] = None
    result:      Optional[str]   = None   # 'tp' | 'sl' | 'eod'
    pnl_pts:     float = 0.0             # points (raw price difference)
    pnl_usd:     float = 0.0             # USD PnL (uses NAS100 point value)

    @property
    def risk_pts(self) -> float:
        return abs(self.entry_price - self.sl)

    @property
    def rr_achieved(self) -> float:
        if self.risk_pts == 0:
            return 0.0
        return self.pnl_pts / self.risk_pts


# ══════════════════════════════════════════════════════════════════════════════
#  CSV LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_csv(path: str) -> pd.DataFrame:
    """
    Load M1 OHLCV data from a CSV file.
    Handles both MT5 export formats and generic formats.

    Required output columns: open, high, low, close, volume
    Index: pd.DatetimeIndex (UTC-naive, treated as UTC internally)
    """
    raw = pd.read_csv(path, sep=None, engine="python")
    raw.columns = [c.strip().lstrip("<").rstrip(">").lower() for c in raw.columns]

    # ── Build datetime index ───────────────────────────────────────────────
    if "date" in raw.columns and "time" in raw.columns:
        raw["datetime"] = pd.to_datetime(raw["date"].astype(str) + " " + raw["time"].astype(str))
    elif "datetime" in raw.columns:
        raw["datetime"] = pd.to_datetime(raw["datetime"])
    elif "timestamp" in raw.columns:
        raw["datetime"] = pd.to_datetime(raw["timestamp"])
    else:
        # Hope the first column is a datetime
        raw["datetime"] = pd.to_datetime(raw.iloc[:, 0])

    raw.set_index("datetime", inplace=True)
    raw.sort_index(inplace=True)

    # ── Normalise column names ─────────────────────────────────────────────
    rename = {}
    for col in raw.columns:
        for target in ("open", "high", "low", "close"):
            if target in col:
                rename[col] = target
        if "vol" in col or "tick" in col:
            rename[col] = "volume"
    raw.rename(columns=rename, inplace=True)

    required = {"open", "high", "low", "close"}
    missing  = required - set(raw.columns)
    if missing:
        sys.exit(f"[ERROR] CSV is missing columns: {missing}")

    df = raw[["open", "high", "low", "close"]].copy().astype(float)
    log.info(f"Loaded {len(df):,} M1 candles  [{df.index[0]}  →  {df.index[-1]}]")
    return df


# ══════════════════════════════════════════════════════════════════════════════
#  TRADE SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

def simulate_trade(
    trade: Trade,
    df: pd.DataFrame,
    entry_bar_idx: int,
    point_value: float,
) -> Trade:
    """
    Walk forward bar-by-bar from entry_bar_idx and check if SL or TP is hit.
    Simulation ends at the last bar in df.

    Uses high/low of each candle to check if either level was touched.
    In the rare case both SL and TP are within the same candle, assumes
    the adverse outcome (SL) — a conservative approach.

    Parameters
    ----------
    trade         : Trade object with entry_price, sl, tp set.
    df            : Full M1 DataFrame.
    entry_bar_idx : Integer index of the candle AFTER entry (first bar to check).
    point_value   : USD value of 1 point per 1 lot (e.g. 1.0 for NAS100 CFDs).
    """
    n = len(df)
    for i in range(entry_bar_idx, n):
        candle = df.iloc[i]
        bar_high = float(candle["high"])
        bar_low  = float(candle["low"])
        ts       = df.index[i]

        if trade.direction == "buy":
            sl_hit = bar_low  <= trade.sl
            tp_hit = bar_high >= trade.tp
        else:
            sl_hit = bar_high >= trade.sl
            tp_hit = bar_low  <= trade.tp

        # Conservative: if both in same candle, assume SL hit first
        if sl_hit:
            trade.exit_time  = ts
            trade.exit_price = trade.sl
            trade.result     = "sl"
            trade.pnl_pts    = -trade.risk_pts
            break
        if tp_hit:
            trade.exit_time  = ts
            trade.exit_price = trade.tp
            trade.result     = "tp"
            trade.pnl_pts    = abs(trade.tp - trade.entry_price)
            break

        # End of data — close at last close
        if i == n - 1:
            exit_px          = float(candle["close"])
            trade.exit_time  = ts
            trade.exit_price = exit_px
            trade.result     = "eod"
            trade.pnl_pts    = (exit_px - trade.entry_price) if trade.direction == "buy" \
                               else (trade.entry_price - exit_px)

    trade.pnl_usd = trade.pnl_pts * trade.lot * point_value
    return trade


# ══════════════════════════════════════════════════════════════════════════════
#  BACK-TEST ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def backtest(
    df: pd.DataFrame,
    lot: float = 0.10,
    point_value: float = 1.0,
    start_date: Optional[str] = None,
    end_date:   Optional[str] = None,
    initial_balance: float = 10_000.0,
) -> List[Trade]:
    """
    Run the FVG strategy over the full M1 DataFrame.

    Parameters
    ----------
    df              : M1 OHLCV DataFrame (from load_csv).
    lot             : Lot size per trade.
    point_value     : USD per point per lot.  NAS100 CFDs: typically $1 per pt per lot.
    start_date      : 'YYYY-MM-DD' filter — ignore bars before this date.
    end_date        : 'YYYY-MM-DD' filter — ignore bars after this date.
    initial_balance : Starting account balance for equity curve.

    Returns
    -------
    List of completed Trade objects.
    """
    # ── Date range filter ─────────────────────────────────────────────────
    if start_date:
        df = df[df.index >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df.index <= pd.Timestamp(end_date) + pd.Timedelta(days=1)]

    if len(df) < MIN_CANDLES_REQUIRED:
        log.error("Not enough data after date filter.")
        return []

    df = df.copy()
    df = add_indicators(df)
    df.reset_index(inplace=True)   # numeric integer index for easy slicing
    df.rename(columns={df.columns[0]: "time"}, inplace=True)

    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    state = SetupState()

    # Convert df back to time-indexed for get_signal compatibility
    df_t = df.set_index("time")

    n = len(df_t)
    log.info(f"Running backtest on {n:,} bars  ({df_t.index[0].date()} → {df_t.index[-1].date()})")

    for bar_idx in range(MIN_CANDLES_REQUIRED, n - 1):
        # Slice: feed a rolling window ending at bar_idx+1
        # (so that df_t.iloc[-2] is bar_idx, the last *closed* candle)
        window = df_t.iloc[: bar_idx + 2]

        # ── Close open trade if SL/TP reached by this bar ─────────────────
        if open_trade is not None and open_trade.exit_time is None:
            candle = df_t.iloc[bar_idx]
            bar_high = float(candle["high"])
            bar_low  = float(candle["low"])
            ts       = df_t.index[bar_idx]

            if open_trade.direction == "buy":
                sl_hit = bar_low  <= open_trade.sl
                tp_hit = bar_high >= open_trade.tp
            else:
                sl_hit = bar_high >= open_trade.sl
                tp_hit = bar_low  <= open_trade.tp

            if sl_hit:
                open_trade.exit_time  = ts
                open_trade.exit_price = open_trade.sl
                open_trade.result     = "sl"
                open_trade.pnl_pts    = -open_trade.risk_pts
                open_trade.pnl_usd    = open_trade.pnl_pts * lot * point_value
                trades.append(open_trade)
                open_trade = None
                state.reset()
                continue

            if tp_hit:
                open_trade.exit_time  = ts
                open_trade.exit_price = open_trade.tp
                open_trade.result     = "tp"
                open_trade.pnl_pts    = abs(open_trade.tp - open_trade.entry_price)
                open_trade.pnl_usd    = open_trade.pnl_pts * lot * point_value
                trades.append(open_trade)
                open_trade = None
                state.reset()
                continue

            # Trade still open — skip signal logic until it resolves
            continue

        # ── Session reset ─────────────────────────────────────────────────
        last_ts = df_t.index[bar_idx]
        if not in_session(last_ts) and state.phase != "idle":
            state.reset()

        # ── Signal ────────────────────────────────────────────────────────
        # Snapshot pivot before signal call (signal() resets state on entry)
        pivot_snap = state.swept_pivot
        signal     = get_signal(window, log, state)

        if signal is None:
            continue

        # ── Entry ─────────────────────────────────────────────────────────
        entry_candle = df_t.iloc[bar_idx]
        entry_price  = float(entry_candle["close"])   # simulate fill at close

        if pivot_snap is None:
            log.warning(f"Signal fired but no swept pivot snapshot — skipping. bar={bar_idx}")
            continue

        sl, tp = compute_sl_tp(signal, entry_price, pivot_snap)

        open_trade = Trade(
            direction   = signal,
            entry_time  = df_t.index[bar_idx],
            entry_price = entry_price,
            sl          = sl,
            tp          = tp,
            lot         = lot,
        )
        log.info(
            f"  ENTRY {signal.upper()} @ {entry_price:.2f} "
            f"| SL={sl:.2f}  TP={tp:.2f} "
            f"| {df_t.index[bar_idx]}"
        )

    # ── EOD close for any still-open trade ───────────────────────────────
    if open_trade is not None:
        last_close       = float(df_t.iloc[-1]["close"])
        open_trade.exit_time  = df_t.index[-1]
        open_trade.exit_price = last_close
        open_trade.result     = "eod"
        open_trade.pnl_pts    = (last_close - open_trade.entry_price) \
                                 if open_trade.direction == "buy" \
                                 else (open_trade.entry_price - last_close)
        open_trade.pnl_usd    = open_trade.pnl_pts * lot * point_value
        trades.append(open_trade)

    return trades


# ══════════════════════════════════════════════════════════════════════════════
#  PERFORMANCE REPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_report(trades: List[Trade], initial_balance: float = 10_000.0) -> None:
    if not trades:
        print("\n  No trades taken. Check your CSV date range and session filter.\n")
        return

    total       = len(trades)
    wins        = [t for t in trades if t.result == "tp"]
    losses      = [t for t in trades if t.result == "sl"]
    partials    = [t for t in trades if t.result == "eod"]
    win_rate    = len(wins) / total * 100
    total_pnl   = sum(t.pnl_usd for t in trades)
    avg_win     = sum(t.pnl_usd for t in wins)  / max(len(wins), 1)
    avg_loss    = sum(t.pnl_usd for t in losses) / max(len(losses), 1)
    profit_factor = (
        sum(t.pnl_usd for t in wins) / abs(sum(t.pnl_usd for t in losses))
        if losses else float("inf")
    )

    # Equity curve & drawdown
    balance  = initial_balance
    peak     = initial_balance
    max_dd   = 0.0
    equity_curve: List[float] = [balance]
    for t in trades:
        balance += t.pnl_usd
        peak = max(peak, balance)
        dd = (peak - balance) / peak * 100
        max_dd = max(max_dd, dd)
        equity_curve.append(balance)

    # Consecutive wins/losses
    max_consec_wins = max_consec_losses = 0
    cur_w = cur_l = 0
    for t in trades:
        if t.result == "tp":
            cur_w += 1; cur_l = 0
        elif t.result == "sl":
            cur_l += 1; cur_w = 0
        max_consec_wins   = max(max_consec_wins, cur_w)
        max_consec_losses = max(max_consec_losses, cur_l)

    sep = "═" * 55
    print(f"\n{sep}")
    print(f"  NAS100 FVG STRATEGY — BACKTEST RESULTS")
    print(sep)
    print(f"  Total Trades     : {total}")
    print(f"  Wins (TP)        : {len(wins)}   ({win_rate:.1f}%)")
    print(f"  Losses (SL)      : {len(losses)}")
    print(f"  Closed at EOD    : {len(partials)}")
    print(f"  ─────────────────────────────────────────────")
    print(f"  Net P&L          : ${total_pnl:+,.2f}")
    print(f"  Final Balance    : ${initial_balance + total_pnl:,.2f}  (started ${initial_balance:,.2f})")
    print(f"  Profit Factor    : {profit_factor:.2f}")
    print(f"  Avg Win  (USD)   : ${avg_win:+.2f}")
    print(f"  Avg Loss (USD)   : ${avg_loss:+.2f}")
    print(f"  Max Drawdown     : {max_dd:.2f}%")
    print(f"  Max Consec Wins  : {max_consec_wins}")
    print(f"  Max Consec Loss  : {max_consec_losses}")
    print(sep)

    # Per-trade detail
    print(f"\n  {'#':>4}  {'DIR':<5}  {'ENTRY':>10}  {'EXIT':>10}  "
          f"{'SL':>10}  {'TP':>10}  {'RESULT':<6}  {'P&L USD':>10}  {'R:R':>6}")
    print(f"  {'─'*4}  {'─'*5}  {'─'*10}  {'─'*10}  "
          f"{'─'*10}  {'─'*10}  {'─'*6}  {'─'*10}  {'─'*6}")
    for i, t in enumerate(trades, 1):
        print(
            f"  {i:>4}  {t.direction.upper():<5}  "
            f"{t.entry_price:>10.2f}  {t.exit_price:>10.2f}  "
            f"{t.sl:>10.2f}  {t.tp:>10.2f}  "
            f"{t.result:<6}  {t.pnl_usd:>+10.2f}  {t.rr_achieved:>+6.2f}R"
        )
    print(f"\n{sep}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  CSV EXPORT
# ══════════════════════════════════════════════════════════════════════════════

def export_trades(trades: List[Trade], path: str = "backtest_results.csv") -> None:
    """Export all trades to a CSV for further analysis in Excel / spreadsheet."""
    rows = []
    for t in trades:
        rows.append({
            "direction":   t.direction,
            "entry_time":  t.entry_time,
            "entry_price": t.entry_price,
            "sl":          t.sl,
            "tp":          t.tp,
            "exit_time":   t.exit_time,
            "exit_price":  t.exit_price,
            "result":      t.result,
            "pnl_pts":     round(t.pnl_pts, 2),
            "pnl_usd":     round(t.pnl_usd, 2),
            "risk_pts":    round(t.risk_pts, 2),
            "rr_achieved": round(t.rr_achieved, 3),
            "lot":         t.lot,
        })
    pd.DataFrame(rows).to_csv(path, index=False)
    log.info(f"Trade log exported → {path}")


# ══════════════════════════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="NAS100 FVG Strategy Backtester",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "--csv", required=True,
        help="Path to M1 OHLCV CSV file (MT5 export format).\n"
             "Expected columns: Date, Time, Open, High, Low, Close, Volume"
    )
    p.add_argument(
        "--lot", type=float, default=0.10,
        help="Lot size per trade (default: 0.10)"
    )
    p.add_argument(
        "--point-value", type=float, default=1.0,
        help="USD value per point per 1.0 lot.\n"
             "NAS100 CFDs on most brokers: 1.0  (default: 1.0)"
    )
    p.add_argument(
        "--start", default=None,
        help="Start date filter: YYYY-MM-DD  (optional)"
    )
    p.add_argument(
        "--end", default=None,
        help="End date filter: YYYY-MM-DD  (optional)"
    )
    p.add_argument(
        "--balance", type=float, default=10_000.0,
        help="Initial account balance for equity curve (default: 10000)"
    )
    p.add_argument(
        "--export", default="backtest_results.csv",
        help="Output CSV path for detailed trade log (default: backtest_results.csv)"
    )
    return p.parse_args()


def main() -> None:
    args   = parse_args()
    df     = load_csv(args.csv)
    trades = backtest(
        df,
        lot             = args.lot,
        point_value     = args.point_value,
        start_date      = args.start,
        end_date        = args.end,
        initial_balance = args.balance,
    )
    print_report(trades, initial_balance=args.balance)
    if trades:
        export_trades(trades, path=args.export)


if __name__ == "__main__":
    main()
