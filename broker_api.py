"""
broker_api.py
Execution layer. A thin, uniform wrapper over each broker so the engine can
place/track orders without knowing which broker is behind it.

    BaseBroker (interface)
      ├── SimulatedBroker   -> paper fills, no network, always available
      ├── UpstoxBroker      -> sandbox (paper) OR live, via upstox-python-sdk
      ├── DhanBroker        -> live, via dhanhq
      └── KotakNeoBroker    -> live, via neo-api-client

Every broker returns the same OrderResult shape, so strategy/engine code is
broker-agnostic (Immutable Rule #3). Real SDK calls are wrapped in try/except
and degrade to a clear error rather than crashing the bot.

LIMIT ORDERS: implemented for Simulated and Upstox. Dhan and Kotak inherit the
BaseBroker default, which degrades a limit request to a MARKET order — correct
but subject to slippage, which matters for the Scalper's 1:1 RR. They are left
un-implemented rather than written blind: an untested order-placement path is a
worse failure than a market fill, since it risks a malformed LIVE order.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import config
from config import Broker, Environment, Instrument, Segment

# Upstox's real order-margin endpoint. Returns SPAN + Exposure + peak margin for a
# basket of instruments — the ACTUAL cash the broker blocks, which for MCX futures
# is nothing like notional ÷ leverage. Read-only, so it is safe to call with the
# live token even from Paper mode.
UPSTOX_MARGIN_URL = "https://api.upstox.com/v2/charges/margin"


def fetch_upstox_margin(
    token: str, instrument_key: str, quantity: int,
    side: str = "BUY", product: str = "D", timeout: float = 10.0,
) -> Optional[float]:
    """Real margin (₹) required to trade `quantity` units of one instrument, from
    Upstox's /charges/margin API. Returns None if it can't be determined — no
    token, network/auth error, or an unexpected response shape — so callers can
    fall back to an estimate. NEVER raises.

    `quantity` is in the SAME units an order uses (lots × lot_size). `price` is
    sent as 0 so Upstox margins against the live LTP; no live price is needed here.
    `product` is "D" (delivery/NRML) for F&O/commodity, "I" (intraday/MIS) for
    equity — margin differs between them.
    """
    if not token or quantity <= 0:
        return None
    try:
        import requests
        body = {"instruments": [{
            "instrument_key": instrument_key,
            "quantity": int(quantity),
            "transaction_type": side,
            "product": product,
            "price": 0,
        }]}
        resp = requests.post(
            UPSTOX_MARGIN_URL,
            headers={"Authorization": f"Bearer {token}",
                     "accept": "application/json",
                     "Content-Type": "application/json"},
            json=body, timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", {}) or {}
        # Upstox reports the basket total under final_margin/required_margin; fall
        # back to summing the per-instrument legs if those aren't present.
        for key in ("final_margin", "required_margin"):
            val = data.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
        margins = data.get("margins") or []
        total = sum(float(m.get("total_margin", 0) or 0) for m in margins)
        return total if total > 0 else None
    except Exception:
        return None


@dataclass
class OrderResult:
    ok: bool
    order_id: str
    broker: str
    filled_price: float = 0.0
    quantity: int = 0
    message: str = ""


class BaseBroker:
    name: str = "Base"

    def connect(self) -> bool:
        raise NotImplementedError

    def place_market_order(
        self, instrument: Instrument, side: str, quantity: int,
        ref_price: float = 0.0,
    ) -> OrderResult:
        raise NotImplementedError

    def place_limit_order(
        self, instrument: Instrument, side: str, quantity: int,
        limit_price: float, ref_price: float = 0.0,
    ) -> OrderResult:
        """Entry at a chosen price rather than whatever the book offers — the
        Scalper uses this to sit just inside the spread (scalping.md §4), because
        at a 1:1 RR on 1-minute bars, market-order slippage eats the edge.

        Default: brokers that don't implement limits degrade to a market order so
        no broker silently drops the order.
        """
        return self.place_market_order(instrument, side, quantity, ref_price)

    def required_margin(
        self, instrument: Instrument, quantity: int, side: str = "BUY",
    ) -> Optional[float]:
        """Real margin (₹) the broker blocks to hold `quantity` of `instrument`, or
        None if this broker can't tell us (the caller then falls back to an
        estimate). Only Upstox implements it today; the rest inherit None."""
        return None

    def square_off(
        self, instrument: Instrument, side: str, quantity: int,
        ref_price: float = 0.0,
    ) -> OrderResult:
        # Exit is just an opposite market order; default impl flips the side.
        # Exits stay MARKET on purpose: a stop or a time-exit must actually get
        # out, and an unfilled limit would leave the position open past its stop.
        opposite = "SELL" if side == "BUY" else "BUY"
        return self.place_market_order(instrument, opposite, quantity, ref_price)


# --------------------------------------------------------------------------- #
#  Simulated broker — paper trading with no credentials. Always works.
# --------------------------------------------------------------------------- #
class SimulatedBroker(BaseBroker):
    name = "Simulated"

    def connect(self) -> bool:
        return True

    def place_market_order(self, instrument, side, quantity, ref_price=0.0):
        return OrderResult(
            ok=True,
            order_id=f"SIM-{uuid.uuid4().hex[:10]}",
            broker=self.name,
            filled_price=ref_price,
            quantity=quantity,
            message="Simulated fill",
        )

    def place_limit_order(self, instrument, side, quantity, limit_price,
                          ref_price=0.0):
        # Optimistic: assumes the limit fills at its price. Real limits inside the
        # spread sometimes don't fill at all, so paper results here are slightly
        # kinder than live would be.
        return OrderResult(
            ok=True,
            order_id=f"SIM-{uuid.uuid4().hex[:10]}",
            broker=self.name,
            filled_price=limit_price or ref_price,
            quantity=quantity,
            message=f"Simulated LIMIT fill @ {limit_price:.2f}",
        )


# --------------------------------------------------------------------------- #
#  Upstox — handles BOTH sandbox (paper) and live via the same SDK.
# --------------------------------------------------------------------------- #
class UpstoxBroker(BaseBroker):
    name = "Upstox"

    def __init__(self, sandbox: bool):
        self.sandbox = sandbox
        self._client = None
        self._order_api = None

    def connect(self) -> bool:
        token = (config.UPSTOX_SANDBOX_TOKEN if self.sandbox
                 else config.UPSTOX_LIVE_ACCESS_TOKEN)
        if not token:
            return False
        try:
            import upstox_client  # type: ignore
            cfg = upstox_client.Configuration()
            # Sandbox flag per the plan: Upstox sandbox environment for paper.
            if hasattr(cfg, "sandbox"):
                cfg.sandbox = self.sandbox
            cfg.access_token = token
            self._client = upstox_client.ApiClient(cfg)
            self._order_api = upstox_client.OrderApi(self._client)
            # Validate the token up front so a bad/expired token is caught here
            # rather than at the moment we try to place a real order.
            if not self.sandbox:
                profile = upstox_client.UserApi(self._client).get_profile(
                    api_version="v2")
                print(f"[UpstoxBroker] live token OK — "
                      f"{getattr(profile.data, 'user_name', 'user')}")
            return True
        except Exception as exc:  # SDK missing or auth failed
            print(f"[UpstoxBroker] connect failed: {exc}")
            return False

    def _place(self, instrument, side, quantity, order_type: str,
               price: float, ref_price: float) -> OrderResult:
        """Shared order path. `price` is ignored by the API for MARKET orders and
        is the limit price for LIMIT orders."""
        if self._order_api is None:
            return OrderResult(False, "", self.name, message="Not connected")
        try:
            import upstox_client  # type: ignore
            body = upstox_client.PlaceOrderRequest(
                quantity=quantity,
                product="I" if instrument.segment == Segment.EQUITY else "D",
                validity="DAY",
                price=round(float(price), 2) if order_type == "LIMIT" else 0,
                instrument_token=instrument.instrument_key,
                order_type=order_type,
                transaction_type=side,
                disclosed_quantity=0,
                trigger_price=0,
                is_amo=False,
            )
            resp = self._order_api.place_order(body, api_version="v2")
            oid = getattr(getattr(resp, "data", None), "order_id", "") or "UPX"
            # NOTE: this reports the order as filled at the requested price. Upstox
            # returns an order_id, not a fill — a LIMIT resting inside the spread
            # may fill later, partially, or not at all. Polling get_order_details
            # for the true average price is the correct next step before trusting
            # live PnL to the rupee.
            fill = price if order_type == "LIMIT" else ref_price
            return OrderResult(True, oid, self.name, fill, quantity,
                               f"{'sandbox' if self.sandbox else 'live'} {order_type}")
        except Exception as exc:
            return OrderResult(False, "", self.name, message=str(exc))

    def place_market_order(self, instrument, side, quantity, ref_price=0.0):
        return self._place(instrument, side, quantity, "MARKET", 0.0, ref_price)

    def place_limit_order(self, instrument, side, quantity, limit_price,
                          ref_price=0.0):
        return self._place(instrument, side, quantity, "LIMIT", limit_price,
                           ref_price)

    def required_margin(self, instrument, quantity, side="BUY"):
        # Prefer the LIVE token — the margin endpoint is read-only and the live
        # token is the one that stays valid, so this works even in Paper mode.
        token = (config.UPSTOX_LIVE_ACCESS_TOKEN
                 or (config.UPSTOX_SANDBOX_TOKEN if self.sandbox else ""))
        product = "I" if instrument.segment == Segment.EQUITY else "D"
        return fetch_upstox_margin(token, instrument.instrument_key, quantity,
                                   side, product)


# --------------------------------------------------------------------------- #
#  Dhan — live, via dhanhq
# --------------------------------------------------------------------------- #
class DhanBroker(BaseBroker):
    name = "Dhan"

    def __init__(self):
        self._client = None

    def connect(self) -> bool:
        if not config.has_dhan():
            return False
        try:
            from dhanhq import dhanhq  # type: ignore
            self._client = dhanhq(config.DHAN_CLIENT_ID, config.DHAN_ACCESS_TOKEN)
            return True
        except Exception as exc:
            print(f"[DhanBroker] connect failed: {exc}")
            return False

    def place_market_order(self, instrument, side, quantity, ref_price=0.0):
        if self._client is None:
            return OrderResult(False, "", self.name, message="Not connected")
        try:
            exchange = ("MCX" if instrument.segment == Segment.MCX
                        else self._client.NSE)
            resp = self._client.place_order(
                security_id=instrument.instrument_key.split("|")[-1],
                exchange_segment=exchange,
                transaction_type=(self._client.BUY if side == "BUY"
                                  else self._client.SELL),
                quantity=quantity,
                order_type=self._client.MARKET,
                product_type=self._client.INTRA,
                price=0,
            )
            oid = str(resp.get("data", {}).get("orderId", "DHAN"))
            return OrderResult(True, oid, self.name, ref_price, quantity, "live")
        except Exception as exc:
            return OrderResult(False, "", self.name, message=str(exc))


# --------------------------------------------------------------------------- #
#  Kotak Neo — live, via neo-api-client
# --------------------------------------------------------------------------- #
class KotakNeoBroker(BaseBroker):
    name = "Kotak Neo"

    def __init__(self):
        self._client = None

    def connect(self) -> bool:
        if not config.has_kotak():
            return False
        try:
            from neo_api_client import NeoAPI  # type: ignore
            self._client = NeoAPI(
                access_token=config.KOTAK_NEO_ACCESS_TOKEN,
                environment="prod",
            )
            return True
        except Exception as exc:
            print(f"[KotakNeoBroker] connect failed: {exc}")
            return False

    def place_market_order(self, instrument, side, quantity, ref_price=0.0):
        if self._client is None:
            return OrderResult(False, "", self.name, message="Not connected")
        try:
            exchange = "mcx" if instrument.segment == Segment.MCX else "nse_cm"
            resp = self._client.place_order(
                exchange_segment=exchange,
                product="MIS",
                price="0",
                order_type="MKT",
                quantity=str(quantity),
                validity="DAY",
                trading_symbol=instrument.symbol,
                transaction_type="B" if side == "BUY" else "S",
            )
            oid = str(resp.get("nOrdNo", "KOTAK"))
            return OrderResult(True, oid, self.name, ref_price, quantity, "live")
        except Exception as exc:
            return OrderResult(False, "", self.name, message=str(exc))


# --------------------------------------------------------------------------- #
#  Factory — pick the right broker for the chosen environment, and fall back to
#  the Simulator whenever credentials are missing so the bot always runs.
# --------------------------------------------------------------------------- #
def make_broker(environment: Environment, broker_choice: Broker) -> BaseBroker:
    if environment == Environment.PAPER:
        # Paper execution is simulated (guaranteed fills, zero risk of a real
        # order, no dependency on a possibly-expired sandbox token). Real MARKET
        # DATA still comes from the live feed, so this is true paper trading:
        # real prices in, simulated fills out — logged to `paper_trades`.
        # (To route paper orders to the actual Upstox Sandbox instead, set a
        # valid UPSTOX_SANDBOX_TOKEN and swap in UpstoxBroker(sandbox=True).)
        sim = SimulatedBroker()
        sim.connect()
        return sim

    # LIVE
    mapping = {
        Broker.UPSTOX: lambda: UpstoxBroker(sandbox=False),
        Broker.DHAN: DhanBroker,
        Broker.KOTAK: KotakNeoBroker,
    }
    factory = mapping.get(broker_choice)
    if factory:
        b = factory()
        if b.connect():
            return b
        print(f"[make_broker] {broker_choice} live connect failed; "
              f"falling back to Simulated to avoid unintended orders.")
    sim = SimulatedBroker()
    sim.connect()
    return sim
