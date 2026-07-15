"""
strategy.py
Pure strategy logic — indicators + entry signals + position sizing.

This module NEVER imports a broker or a database. It takes a candle DataFrame
in, and returns signals/sizes out. That decoupling is Immutable Rule #3, and it
is what lets the same strategy run over Upstox, Dhan, Kotak or the simulator, and
over NSE equity or MCX commodities, without change.

Indicators are implemented directly in pandas/numpy rather than pandas-ta so the
system runs on any numpy version without dependency breakage. If you prefer
pandas-ta / TA-Lib, swap the bodies of the _indicator functions — the signatures
are stable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from config import Mode, StrategyParams, params_for_mode


# --------------------------------------------------------------------------- #
#  Indicator primitives  (input: OHLCV columns open, high, low, close, volume)
# --------------------------------------------------------------------------- #
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP. Resets each calendar day so intraday VWAP is meaningful."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    tpv = typical * df["volume"]
    if isinstance(df.index, pd.DatetimeIndex):
        day = df.index.normalize()
        cum_tpv = tpv.groupby(day).cumsum()
        cum_vol = df["volume"].groupby(day).cumsum()
    else:
        cum_tpv = tpv.cumsum()
        cum_vol = df["volume"].cumsum()
    return cum_tpv / cum_vol.replace(0, np.nan)


def macd(series: pd.Series, fast: int, slow: int, signal: int):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def bollinger(series: pd.Series, period: int, num_std: float):
    mid = sma(series, period)
    std = series.rolling(period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# --------------------------------------------------------------------------- #
#  Indicator enrichment — adds all columns a strategy might read.
# --------------------------------------------------------------------------- #
def enrich(df: pd.DataFrame, params: StrategyParams) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]

    # Intraday indicators
    out["vwap"] = vwap(out)
    m, s, h = macd(close, params.macd_fast, params.macd_slow, params.macd_signal)
    out["macd"], out["macd_signal"], out["macd_hist"] = m, s, h

    # Swing indicators
    out["ema_trend"] = ema(close, params.ema_trend)
    out["rsi"] = rsi(close, params.rsi_period)
    ub, mb, lb = bollinger(close, params.bb_period, params.bb_std)
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = ub, mb, lb
    out["vol_sma"] = sma(out["volume"], params.vol_sma)

    out["atr"] = atr(out, params.atr_period)
    return out


# --------------------------------------------------------------------------- #
#  Signal object
# --------------------------------------------------------------------------- #
@dataclass
class Signal:
    side: str            # "BUY" (long). Shorting kept out of MVP for safety.
    entry_price: float
    stop_loss: float
    target: float
    reason: str

    @property
    def risk_reward(self) -> float:
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.target - self.entry_price)
        return reward / risk if risk else 0.0


def _crossed_above(series: pd.Series, other: pd.Series) -> bool:
    """True if `series` crossed above `other` on the latest bar."""
    if len(series) < 2:
        return False
    return series.iloc[-2] <= other.iloc[-2] and series.iloc[-1] > other.iloc[-1]


# --------------------------------------------------------------------------- #
#  Entry logic
# --------------------------------------------------------------------------- #
def intraday_signal(df: pd.DataFrame, params: StrategyParams) -> Optional[Signal]:
    """
    Long entry (Intraday, 15m):
      Price above VWAP  AND  MACD line crosses above its Signal line.
    Stop = ATR-based (recent structure); Target = 1:2 RR.
    """
    if len(df) < max(params.macd_slow, params.atr_period) + 2:
        return None
    last = df.iloc[-1]
    price_above_vwap = last["close"] > last["vwap"]
    macd_cross = _crossed_above(df["macd"], df["macd_signal"])
    if not (price_above_vwap and macd_cross):
        return None

    entry = float(last["close"])
    atr_val = float(last["atr"]) if not np.isnan(last["atr"]) else entry * 0.005
    stop = entry - params.atr_sl_mult * atr_val
    if stop >= entry:
        return None
    target = entry + params.risk_reward * (entry - stop)   # enforces 1:2
    return Signal("BUY", entry, stop, target,
                  "Price>VWAP + MACD bullish cross")


def swing_signal(df: pd.DataFrame, params: StrategyParams) -> Optional[Signal]:
    """
    Long entry (Swing, Daily). The Bollinger reclaim/bounce is the TRIGGER event;
    trend, momentum and volume are confirming filters. (Requiring the RSI-cross
    and the band reclaim to land on the exact same bar almost never happens, so
    momentum is treated as a state — RSI in breakout territory above 50.)
      Trend:      close > 200 EMA
      Momentum:   RSI above 50 (breakout territory)
      Volatility: close reclaims the middle band, or bounces off the lower band  <-- trigger
      Volume:     entry candle volume strictly > 20-period volume SMA
    Stop = ATR-based; Target = 1:3 RR.
    """
    if len(df) < params.ema_trend + 2:
        return None
    last = df.iloc[-1]
    prev = df.iloc[-2]

    trend_ok = last["close"] > last["ema_trend"]
    momentum_ok = last["rsi"] > params.rsi_breakout            # RSI > 50 (state)
    # Trigger event: reclaiming the middle band from below, or bouncing off lower.
    volatility_trigger = (
        (prev["close"] <= prev["bb_mid"] and last["close"] > last["bb_mid"])
        or (prev["low"] <= prev["bb_lower"] and last["close"] > prev["close"])
    )
    volume_ok = last["volume"] > last["vol_sma"]

    if not (trend_ok and momentum_ok and volatility_trigger and volume_ok):
        return None

    entry = float(last["close"])
    atr_val = float(last["atr"]) if not np.isnan(last["atr"]) else entry * 0.01
    stop = entry - params.atr_sl_mult * atr_val
    if stop >= entry:
        return None
    target = entry + params.risk_reward * (entry - stop)   # enforces 1:3
    return Signal("BUY", entry, stop, target,
                  "Trend+Momentum+Volatility+Volume aligned")


def generate_signal(df: pd.DataFrame, mode: Mode) -> Optional[Signal]:
    """Dispatch to the correct strategy after enriching with indicators."""
    params = params_for_mode(mode)
    enriched = enrich(df, params)
    if mode == Mode.SWING:
        return swing_signal(enriched, params)
    return intraday_signal(enriched, params)


# --------------------------------------------------------------------------- #
#  Position sizing — the risk core. Never exceed the per-trade risk cap.
# --------------------------------------------------------------------------- #
def position_size(
    total_capital: float,
    signal: Signal,
    params: StrategyParams,
    lot_size: int = 1,
) -> tuple[int, float]:
    """
    Returns (quantity, risk_amount).

        risk_capital = total_capital * risk_per_trade
        raw_qty      = risk_capital / (entry - stop)

    For commodities quantity is rounded down to whole lots. The realised risk is
    recomputed from the rounded quantity so the reported risk never overstates.
    """
    per_unit_risk = abs(signal.entry_price - signal.stop_loss)
    if per_unit_risk <= 0:
        return 0, 0.0
    risk_capital = total_capital * params.risk_per_trade
    raw_qty = risk_capital / per_unit_risk

    if lot_size > 1:
        lots = int(raw_qty // lot_size)
        qty = lots * lot_size
    else:
        qty = int(raw_qty)

    realized_risk = qty * per_unit_risk
    return max(qty, 0), realized_risk
