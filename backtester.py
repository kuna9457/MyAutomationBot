"""
backtester.py
Vectorized backtesting engine.

Given historical daily/intraday candles for one instrument, it walks the same
strategy signals used live and computes the Section-3 metrics:
Total Return, Max Drawdown, Sharpe, Calmar, Win Rate, plus an equity curve for
the UI chart.

Historical data source, in priority order:
  1. yfinance (if installed and a mappable ticker is given)
  2. synthetic random-walk series (always available — lets the engine be tested)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from config import Mode, params_for_mode
from strategy import (enrich, intraday_signal, position_size, swing_signal)


TRADING_DAYS = 252


@dataclass
class BacktestResult:
    metrics: dict
    equity_curve: pd.Series
    trades: pd.DataFrame


# --------------------------------------------------------------------------- #
#  Data acquisition
# --------------------------------------------------------------------------- #
def _yf_symbol(ticker: str) -> str:
    """Map our symbols to yfinance tickers where a sensible mapping exists."""
    mapping = {
        "GOLD": "GC=F", "CRUDEOIL": "CL=F", "NATURALGAS": "NG=F", "SILVER": "SI=F",
    }
    if ticker in mapping:
        return mapping[ticker]
    return ticker if ticker.endswith(".NS") else f"{ticker}.NS"


def _fetch_upstox_hist(
    instrument_key: str, start: str, end: str, interval: str, token: str
) -> pd.DataFrame:
    """Real historical candles from Upstox for one instrument over [start, end].
    Daily for swing; 1-minute resampled to 15m for intraday. Indexed IST-naive."""
    import upstox_client  # type: ignore
    cfg = upstox_client.Configuration()
    cfg.access_token = token
    hist = upstox_client.HistoryApi(upstox_client.ApiClient(cfg))
    up_interval = "day" if interval == "1d" else "1minute"
    resp = hist.get_historical_candle_data1(
        instrument_key, up_interval, end, start, api_version="v2")
    candles = resp.data.candles
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=[
        "ts", "open", "high", "low", "close", "volume", "oi"][:len(candles[0])])
    df["ts"] = (pd.to_datetime(df["ts"], utc=True)
                .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    df = (df.set_index("ts").sort_index()
          [["open", "high", "low", "close", "volume"]].astype(float))
    df = df[~df.index.duplicated(keep="last")]
    if interval != "1d":
        df = df.resample("15min").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}).dropna()
    return df


def fetch_history(
    ticker: str, start: str, end: str, interval: str = "1d",
    instrument_key: str = "", token: str = "",
) -> pd.DataFrame:
    # 1) REAL Upstox historical data — the good path. Ticker-specific & real, so
    #    every instrument gives genuinely different results.
    if instrument_key and token:
        try:
            df = _fetch_upstox_hist(instrument_key, start, end, interval, token)
            if len(df) > 30:
                print(f"[backtester] {ticker}: {len(df)} real Upstox candles.")
                return df
            print(f"[backtester] {ticker}: Upstox returned too few candles "
                  f"({len(df)}); trying next source.")
        except Exception as exc:
            print(f"[backtester] {ticker}: Upstox history failed ({exc}); "
                  "trying next source.")

    # 2) yfinance, if installed
    try:
        import yfinance as yf  # type: ignore
        df = yf.download(_yf_symbol(ticker), start=start, end=end,
                         interval=interval, progress=False, auto_adjust=True,
                         timeout=20)
        if df is not None and not df.empty:
            df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
            df.index = pd.to_datetime(df.index)
            return df.dropna()
    except Exception as exc:
        print(f"[backtester] yfinance unavailable ({exc}); using synthetic data.")

    # 3) Synthetic — seeded PER TICKER so different symbols give different series
    #    (a fixed seed made every backtest identical — the bug being fixed here).
    return synthetic_history(start, end, interval, seed_key=ticker)


def synthetic_history(start: str, end: str, interval: str = "1d",
                      seed_key: str = "") -> pd.DataFrame:
    freq = "1D" if interval == "1d" else "15min"
    idx = pd.date_range(start=start, end=end, freq=freq)
    if len(idx) < 50:
        idx = pd.date_range(end=datetime.now(), periods=400, freq=freq)
    # Real intraday history (e.g. yfinance) only spans ~60 days, so a multi-year
    # 15m range would balloon to 100k+ bars and stall the backtest for no realism.
    # Cap to the most recent slice, mirroring what a real intraday feed would give.
    MAX_INTRADAY_BARS = 6000
    if freq == "15min" and len(idx) > MAX_INTRADAY_BARS:
        idx = idx[-MAX_INTRADAY_BARS:]
    n = len(idx)
    # Derive the seed from the ticker so each symbol has its own price path — with a
    # fixed seed, every ticker produced the exact same numbers.
    seed = (abs(hash(seed_key)) % (2**32)) if seed_key else 42
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0004, 0.012, n)
    # inject a few trends so signals trigger
    for _ in range(max(3, n // 120)):
        s = rng.integers(0, n - 20)
        rets[s:s + 15] += rng.normal(0.003, 0.001)
    price = 100 * np.exp(np.cumsum(rets))
    close = pd.Series(price, index=idx)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    vol = np.abs(rng.normal(1_000_000, 300_000, n)).astype(int) + 1000
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


# --------------------------------------------------------------------------- #
#  Core simulation
# --------------------------------------------------------------------------- #
def run_backtest(
    ticker: str,
    start: str,
    end: str,
    initial_capital: float,
    mode: Mode,
    lot_size: int = 1,
) -> BacktestResult:
    params = params_for_mode(mode)
    interval = "1d" if mode == Mode.SWING else "15m"
    # Resolve the Upstox instrument key + live token so we backtest on REAL data.
    import config
    inst = config.INSTRUMENTS_BY_SYMBOL.get(ticker)
    instrument_key = inst.instrument_key if inst else ""
    token = config.UPSTOX_LIVE_ACCESS_TOKEN or config.UPSTOX_SANDBOX_TOKEN
    data = fetch_history(ticker, start, end, interval,
                         instrument_key=instrument_key, token=token)
    # Enrich ONCE up front. Indicators are causal (each row uses only past/current
    # data), so a value at row i is identical whether computed on the full series
    # or on data[:i+1]. This lets the walk-forward loop read pre-computed columns
    # instead of re-enriching a growing window every bar (which was O(n^2) and made
    # intraday backtests hang). We call the mode's signal fn directly on the slice.
    data = enrich(data, params)
    signal_fn = swing_signal if mode == Mode.SWING else intraday_signal

    capital = initial_capital
    equity = []
    trades = []
    position = None  # dict: entry, stop, target, qty
    warmup = params.ema_trend + 2 if mode == Mode.SWING else params.macd_slow + 2

    # The signal fns read only the last two bars (indicators are pre-computed), but
    # keep a >= warmup-sized tail so their internal length guard still passes.
    tail = warmup + 6
    for i in range(warmup, len(data)):
        window = data.iloc[max(0, i - tail + 1): i + 1]
        bar = data.iloc[i]
        price = float(bar["close"])

        # manage an open position first
        if position is not None:
            hit_sl = bar["low"] <= position["stop"]
            hit_tp = bar["high"] >= position["target"]
            exit_price = None
            if hit_sl and hit_tp:
                exit_price = position["stop"]        # assume worst case
            elif hit_sl:
                exit_price = position["stop"]
            elif hit_tp:
                exit_price = position["target"]
            if exit_price is not None:
                pnl = (exit_price - position["entry"]) * position["qty"]
                capital += pnl
                trades.append({
                    "entry_time": position["time"], "exit_time": bar.name,
                    "entry": position["entry"], "exit": exit_price,
                    "qty": position["qty"], "pnl": pnl,
                    "rr": params.risk_reward, "win": pnl > 0,
                })
                position = None

        # look for a new entry only when flat. `window` is already enriched, so we
        # call the signal fn directly (generate_signal would re-enrich = O(n^2)).
        if position is None:
            sig = signal_fn(window, params)
            if sig is not None:
                qty, risk_amt = position_size(capital, sig, params, lot_size)
                if qty > 0:
                    position = {
                        "entry": sig.entry_price, "stop": sig.stop_loss,
                        "target": sig.target, "qty": qty, "time": bar.name,
                    }

        equity.append(capital)

    equity_curve = pd.Series(equity, index=data.index[warmup:])
    trades_df = pd.DataFrame(trades)
    metrics = _metrics(equity_curve, trades_df, initial_capital, interval)
    return BacktestResult(metrics, equity_curve, trades_df)


# --------------------------------------------------------------------------- #
#  Metrics
# --------------------------------------------------------------------------- #
def _metrics(equity: pd.Series, trades: pd.DataFrame,
             initial_capital: float, interval: str) -> dict:
    if equity.empty:
        return {"Total Return %": 0.0, "Max Drawdown %": 0.0, "Sharpe": 0.0,
                "Calmar": 0.0, "Win Rate %": 0.0, "Total Trades": 0,
                "Final Equity": initial_capital}

    total_return = (equity.iloc[-1] / initial_capital - 1) * 100

    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    max_dd = drawdown.min() * 100  # negative

    rets = equity.pct_change().dropna()
    periods_per_year = TRADING_DAYS if interval == "1d" else TRADING_DAYS * 25
    if rets.std() > 0:
        sharpe = (rets.mean() / rets.std()) * np.sqrt(periods_per_year)
    else:
        sharpe = 0.0

    years = max(len(equity) / periods_per_year, 1e-9)
    cagr = (equity.iloc[-1] / initial_capital) ** (1 / years) - 1
    calmar = (cagr / abs(max_dd / 100)) if max_dd != 0 else 0.0

    win_rate = (100 * trades["win"].mean()) if not trades.empty else 0.0

    return {
        "Total Return %": round(total_return, 2),
        "Max Drawdown %": round(max_dd, 2),
        "Sharpe": round(float(sharpe), 2),
        "Calmar": round(float(calmar), 2),
        "Win Rate %": round(float(win_rate), 2),
        "Total Trades": int(len(trades)),
        "Final Equity": round(float(equity.iloc[-1]), 2),
    }
