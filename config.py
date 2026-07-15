"""
config.py
Central configuration: environment loading, the tradable instrument universe
(NSE equity + MCX commodities), market hours, and strategy parameters.

Nothing here talks to a broker or a database — it is pure configuration so the
rest of the system can stay decoupled (Immutable Rule #3).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from enum import Enum

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # dotenv is optional; env can be set by the OS instead
    pass


# --------------------------------------------------------------------------- #
#  Enums for the two "axes" of the system
# --------------------------------------------------------------------------- #
class Mode(str, Enum):
    INTRADAY = "Intraday"
    SWING = "Swing"


class Environment(str, Enum):
    PAPER = "Paper"
    LIVE = "Live"


class Broker(str, Enum):
    UPSTOX = "Upstox"
    DHAN = "Dhan"
    KOTAK = "Kotak Neo"
    SIMULATED = "Simulated"   # used automatically when no credentials exist


class Segment(str, Enum):
    EQUITY = "NSE_EQUITY"
    MCX = "MCX_COMMODITY"


# --------------------------------------------------------------------------- #
#  Environment variables
# --------------------------------------------------------------------------- #
def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


MONGO_URI = _env("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB_NAME = _env("MONGO_DB_NAME", "trading_bot")

UPSTOX_SANDBOX_TOKEN = _env("UPSTOX_SANDBOX_TOKEN")
UPSTOX_LIVE_ACCESS_TOKEN = _env("UPSTOX_LIVE_ACCESS_TOKEN")
UPSTOX_LIVE_API_KEY = _env("UPSTOX_LIVE_API_KEY")
UPSTOX_LIVE_SECRET = _env("UPSTOX_LIVE_SECRET")

DHAN_CLIENT_ID = _env("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = _env("DHAN_ACCESS_TOKEN")

KOTAK_NEO_CONSUMER_KEY = _env("KOTAK_NEO_CONSUMER_KEY")
KOTAK_NEO_CONSUMER_SECRET = _env("KOTAK_NEO_CONSUMER_SECRET")
KOTAK_NEO_ACCESS_TOKEN = _env("KOTAK_NEO_ACCESS_TOKEN")

try:
    TOTAL_CAPITAL = float(_env("TOTAL_CAPITAL", "100000") or "100000")
except ValueError:
    TOTAL_CAPITAL = 100_000.0


# --------------------------------------------------------------------------- #
#  Instrument universe
#  `instrument_key` is the Upstox V3 style key; adapt per broker in broker_api.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Instrument:
    symbol: str            # human friendly name shown in the UI
    segment: Segment
    instrument_key: str    # broker feed subscription key (Upstox format shown)
    lot_size: int = 1      # commodities trade in lots; equity lot_size = 1
    tick_size: float = 0.05
    reference_price: float = 100.0   # only used to seed the simulated feed


# NSE equities (cash) -------------------------------------------------------- #
EQUITY_INSTRUMENTS = [
    Instrument("RELIANCE", Segment.EQUITY, "NSE_EQ|INE002A01018", 1, 0.05, 2900.0),
    Instrument("TCS",      Segment.EQUITY, "NSE_EQ|INE467B01029", 1, 0.05, 3850.0),
    Instrument("INFY",     Segment.EQUITY, "NSE_EQ|INE009A01021", 1, 0.05, 1550.0),
    Instrument("HDFCBANK", Segment.EQUITY, "NSE_EQ|INE040A01034", 1, 0.05, 1650.0),
    Instrument("SBIN",     Segment.EQUITY, "NSE_EQ|INE062A01020", 1, 0.05, 820.0),
]

# MCX commodities (the newly requested additions) ---------------------------- #
# These are REAL front-month futures instrument_keys pulled from the Upstox MCX
# instrument master, with real lot/tick sizes and live reference prices. They
# EXPIRE — refresh them each expiry with tools/refresh_mcx.py (which rolls to the
# nearest active future automatically).
#
# NOTE on live sizing: MCX futures carry a contract multiplier (e.g. GOLD is
# quoted per 10g on a 1kg contract). The position-sizing here sizes on raw price
# distance and is exact for equities; for LIVE commodity orders verify quantity
# against your broker's contract multiplier and margin. Paper/simulation is fine.
MCX_INSTRUMENTS = [
    Instrument("GOLD",       Segment.MCX, "MCX_FO|466583", 1,    1.0,  142419.0),
    Instrument("CRUDEOIL",   Segment.MCX, "MCX_FO|520702", 100,  1.0,  7580.0),
    Instrument("NATURALGAS", Segment.MCX, "MCX_FO|538685", 1250, 0.10, 279.7),
    Instrument("SILVER",     Segment.MCX, "MCX_FO|471725", 30,   1.0,  223320.0),
]

ALL_INSTRUMENTS = EQUITY_INSTRUMENTS + MCX_INSTRUMENTS
INSTRUMENTS_BY_SYMBOL = {i.symbol: i for i in ALL_INSTRUMENTS}


def instruments_for_segment(segment: Segment) -> list[Instrument]:
    return [i for i in ALL_INSTRUMENTS if i.segment == segment]


# --------------------------------------------------------------------------- #
#  Market hours (IST). MCX stays open into the night — this is the whole point
#  of the commodity addition, so the engine must respect the later close.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MarketHours:
    open_t: time
    close_t: time

    def is_open(self, now_t: time) -> bool:
        return self.open_t <= now_t <= self.close_t


# NSE equity: 09:15 - 15:30 IST
EQUITY_HOURS = MarketHours(time(9, 15), time(15, 30))
# MCX: 09:00 - 23:30 IST (winter session runs to 23:55; using 23:30 as a safe cutoff)
MCX_HOURS = MarketHours(time(9, 0), time(23, 30))


def market_hours_for_segment(segment: Segment) -> MarketHours:
    return MCX_HOURS if segment == Segment.MCX else EQUITY_HOURS


# --------------------------------------------------------------------------- #
#  Strategy parameters — one place, enforcing the Immutable Risk Rules.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StrategyParams:
    mode: Mode
    timeframe: str
    risk_per_trade: float      # fraction of capital risked per trade
    risk_reward: float         # reward : risk ratio (the "2" or "3" in 1:2 / 1:3)
    # Intraday indicator params
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    # Swing indicator params
    ema_trend: int = 200
    rsi_period: int = 14
    rsi_breakout: float = 50.0
    bb_period: int = 20
    bb_std: float = 2.0
    vol_sma: int = 20
    atr_period: int = 14
    atr_sl_mult: float = 1.5   # stop distance = atr_sl_mult * ATR


INTRADAY_PARAMS = StrategyParams(
    mode=Mode.INTRADAY, timeframe="15m",
    risk_per_trade=0.02, risk_reward=2.0,   # Hard 1:2, max 2% — Immutable Rule #1
)
SWING_PARAMS = StrategyParams(
    mode=Mode.SWING, timeframe="1d",
    risk_per_trade=0.03, risk_reward=3.0,   # Hard 1:3, max 3% — Immutable Rule #1
)


def params_for_mode(mode: Mode) -> StrategyParams:
    return SWING_PARAMS if mode == Mode.SWING else INTRADAY_PARAMS


# --------------------------------------------------------------------------- #
#  Credential presence helpers — lets the engine auto-pick Simulated broker.
# --------------------------------------------------------------------------- #
def has_upstox_sandbox() -> bool:
    return bool(UPSTOX_SANDBOX_TOKEN)


def has_upstox_live() -> bool:
    return bool(UPSTOX_LIVE_ACCESS_TOKEN)


def has_dhan() -> bool:
    return bool(DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN)


def has_kotak() -> bool:
    return bool(KOTAK_NEO_ACCESS_TOKEN)


def reload_tokens() -> None:
    """
    Re-read broker tokens from the environment / .env and update the module
    globals in place. Called after the UI refreshes the Upstox token so the
    running process picks up the new token without a restart.
    """
    global UPSTOX_SANDBOX_TOKEN, UPSTOX_LIVE_ACCESS_TOKEN, UPSTOX_LIVE_API_KEY
    global UPSTOX_LIVE_SECRET, DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN
    global KOTAK_NEO_ACCESS_TOKEN
    try:
        from dotenv import load_dotenv as _ld
        _ld(override=True)
    except Exception:
        pass
    UPSTOX_SANDBOX_TOKEN = _env("UPSTOX_SANDBOX_TOKEN")
    UPSTOX_LIVE_ACCESS_TOKEN = _env("UPSTOX_LIVE_ACCESS_TOKEN")
    UPSTOX_LIVE_API_KEY = _env("UPSTOX_LIVE_API_KEY")
    UPSTOX_LIVE_SECRET = _env("UPSTOX_LIVE_SECRET")
    DHAN_CLIENT_ID = _env("DHAN_CLIENT_ID")
    DHAN_ACCESS_TOKEN = _env("DHAN_ACCESS_TOKEN")
    KOTAK_NEO_ACCESS_TOKEN = _env("KOTAK_NEO_ACCESS_TOKEN")


# Local storage fallback location (used when MongoDB is unreachable)
LOCAL_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(LOCAL_DB_DIR, exist_ok=True)
