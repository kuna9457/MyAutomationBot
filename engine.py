"""
engine.py
The orchestrator. Ties the four decoupled layers together into one running bot:

    data_feed  --candles-->  strategy  --signal-->  broker  --fill-->  db_manager

It runs in a background thread so the Streamlit UI stays responsive, exposes a
thread-safe snapshot of state (connection status, live signals, open positions,
PnL) for the dashboard, and honours per-segment market hours — critically, MCX
commodities keep trading into the night while equity stops at 15:30.

Start/Stop from the UI simply flip the thread on and off.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

import config
from broker_api import BaseBroker, make_broker
from config import (Broker, Environment, Instrument, Mode, Segment,
                    market_hours_for_segment, params_for_mode)
from data_feed import MarketDataFeed, SimulatedFeed, make_feed
from db_manager import DBManager
from strategy import generate_signal, position_size


class BotState:
    """Thread-safe container the UI polls each rerun."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.feed_status = "🔴 Not started"
        self.broker_name = "-"
        self.db_backend = "-"
        self.last_signals: list[dict] = []      # recently detected signals
        self.open_positions: dict[str, dict] = {}   # symbol -> trade doc + live px
        self.day_pnl = 0.0
        self.realized_pnl = 0.0
        self.log: list[str] = []

    def push_log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.log.insert(0, f"[{stamp}] {msg}")
            self.log = self.log[:200]

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "feed_status": self.feed_status,
                "broker_name": self.broker_name,
                "db_backend": self.db_backend,
                "last_signals": list(self.last_signals),
                "open_positions": dict(self.open_positions),
                "day_pnl": self.day_pnl,
                "realized_pnl": self.realized_pnl,
                "log": list(self.log),
            }


class TradingEngine:
    def __init__(
        self,
        environment: Environment,
        mode: Mode,
        broker_choice: Broker,
        instruments: list[Instrument],
        total_capital: float,
        poll_seconds: float = 3.0,
    ):
        self.environment = environment
        self.mode = mode
        self.broker_choice = broker_choice
        self.instruments = instruments
        self.total_capital = total_capital
        self.poll_seconds = poll_seconds

        self.state = BotState()
        self.db = DBManager()
        self.broker: Optional[BaseBroker] = None
        self.feed: Optional[MarketDataFeed] = None

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        if self.state.running:
            return
        self._stop.clear()

        # Broker: real if creds exist for the chosen env, else Simulated.
        self.broker = make_broker(self.environment, self.broker_choice)

        # Feed: real Upstox data if a token is present, else simulated.
        # Market data is READ-ONLY, so we prefer the LIVE token (which is the one
        # that stays valid) for real candles even in Paper mode — that gives true
        # paper trading = real market data + simulated execution. Fall back to the
        # sandbox token, then to the simulator.
        token = config.UPSTOX_LIVE_ACCESS_TOKEN or config.UPSTOX_SANDBOX_TOKEN
        self.feed = make_feed(prefer_real=bool(token), access_token=token,
                              mode=self.mode)
        self._start_feed_resilient()

        with self.state.lock:
            self.state.running = True
            self.state.broker_name = self.broker.name
            self.state.db_backend = self.db.backend
            self.state.feed_status = self.feed.status()

        self.state.push_log(
            f"Bot started — {self.environment.value} / {self.mode.value} / "
            f"{self.broker.name} / {len(self.instruments)} instruments")
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _start_feed_resilient(self) -> None:
        """Start the chosen feed; if a real feed fails (no data / auth / network),
        fall back to the SimulatedFeed so Start Bot never throws in the UI."""
        try:
            self.feed.start(self.instruments)
        except Exception as exc:
            self.state.push_log(
                f"⚠️ Live feed unavailable ({exc}); using simulated feed.")
            self.feed = SimulatedFeed()
            self.feed.start(self.instruments)

    def stop(self) -> None:
        self._stop.set()
        if self.feed:
            self.feed.stop()
        with self.state.lock:
            self.state.running = False
            self.state.feed_status = "🔴 Stopped"
        self.state.push_log("Bot stopped.")

    # -- main loop ---------------------------------------------------------- #
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # never let one bad tick kill the bot
                self.state.push_log(f"⚠️ tick error: {exc}")
            self._stop.wait(self.poll_seconds)

    def _tick(self) -> None:
        now_t = datetime.now().time()
        with self.state.lock:
            self.state.feed_status = self.feed.status()

        for inst in self.instruments:
            hours = market_hours_for_segment(inst.segment)
            market_open = hours.is_open(now_t)

            df = self.feed.get_candles(inst, lookback=260)
            if df.empty:
                continue
            live_price = float(df["close"].iloc[-1])

            # 1) manage an open position (exit on SL/TP) regardless of new signals
            if inst.symbol in self.state.open_positions:
                self._manage_open(inst, live_price)
                continue

            # 2) only look for new entries while that segment's market is open
            if not market_open:
                continue

            sig = generate_signal(df, self.mode)
            if sig is None:
                continue

            params = params_for_mode(self.mode)
            qty, risk_amt = position_size(
                self.total_capital, sig, params, inst.lot_size)

            sig_row = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "symbol": inst.symbol, "segment": inst.segment.value,
                "side": sig.side, "entry": round(sig.entry_price, 2),
                "stop": round(sig.stop_loss, 2), "target": round(sig.target, 2),
                "rr": round(sig.risk_reward, 2), "qty": qty, "reason": sig.reason,
            }
            with self.state.lock:
                self.state.last_signals.insert(0, sig_row)
                self.state.last_signals = self.state.last_signals[:50]

            if qty <= 0:
                self.state.push_log(
                    f"{inst.symbol}: signal skipped (qty=0, capital too small "
                    f"for lot size {inst.lot_size}).")
                continue

            self._enter(inst, sig, qty, risk_amt)

    # -- order handling ----------------------------------------------------- #
    def _enter(self, inst: Instrument, sig, qty: int, risk_amt: float) -> None:
        res = self.broker.place_market_order(inst, sig.side, qty, sig.entry_price)
        if not res.ok:
            self.state.push_log(f"{inst.symbol}: order rejected — {res.message}")
            return

        trade = self.db.new_trade(
            mode=self.mode.value, environment=self.environment.value,
            broker=self.broker.name, ticker=inst.symbol, side=sig.side,
            entry_price=res.filled_price or sig.entry_price,
            stop_loss=sig.stop_loss, target=sig.target, quantity=qty,
            risk_amount=risk_amt, segment=inst.segment.value,
        )
        self.db.insert_trade(trade, self.environment)
        trade["_live_price"] = res.filled_price or sig.entry_price
        with self.state.lock:
            self.state.open_positions[inst.symbol] = trade
        self.state.push_log(
            f"ENTER {inst.symbol} {sig.side} qty={qty} @ {trade['entry_price']:.2f} "
            f"SL={sig.stop_loss:.2f} TP={sig.target:.2f} (risk ₹{risk_amt:.0f})")

    def _manage_open(self, inst: Instrument, live_price: float) -> None:
        trade = self.state.open_positions[inst.symbol]
        trade["_live_price"] = live_price
        exit_price = None
        if live_price <= trade["stop_loss"]:
            exit_price = trade["stop_loss"]
            reason = "STOP-LOSS"
        elif live_price >= trade["target"]:
            exit_price = trade["target"]
            reason = "TARGET"
        if exit_price is None:
            self._recompute_unrealized()
            return

        res = self.broker.square_off(inst, trade["side"], trade["quantity"],
                                     exit_price)
        closed = self.db.close_trade(trade["trade_id"], exit_price,
                                     self.environment)
        pnl = closed["realized_pnl"] if closed else 0.0
        with self.state.lock:
            self.state.open_positions.pop(inst.symbol, None)
            self.state.realized_pnl += pnl
        self.state.push_log(
            f"EXIT {inst.symbol} @ {exit_price:.2f} [{reason}] "
            f"PnL ₹{pnl:.2f} (order {res.order_id})")
        self._recompute_unrealized()

    def _recompute_unrealized(self) -> None:
        with self.state.lock:
            unreal = 0.0
            for t in self.state.open_positions.values():
                direction = 1 if t["side"] == "BUY" else -1
                unreal += (t.get("_live_price", t["entry_price"])
                           - t["entry_price"]) * t["quantity"] * direction
            self.state.day_pnl = round(self.state.realized_pnl + unreal, 2)
