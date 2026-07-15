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
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import config
from config import Broker, Environment, Instrument, Segment


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

    def square_off(
        self, instrument: Instrument, side: str, quantity: int,
        ref_price: float = 0.0,
    ) -> OrderResult:
        # Exit is just an opposite market order; default impl flips the side.
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

    def place_market_order(self, instrument, side, quantity, ref_price=0.0):
        if self._order_api is None:
            return OrderResult(False, "", self.name, message="Not connected")
        try:
            import upstox_client  # type: ignore
            body = upstox_client.PlaceOrderRequest(
                quantity=quantity,
                product="I" if instrument.segment == Segment.EQUITY else "D",
                validity="DAY",
                price=0,
                instrument_token=instrument.instrument_key,
                order_type="MARKET",
                transaction_type=side,
                disclosed_quantity=0,
                trigger_price=0,
                is_amo=False,
            )
            resp = self._order_api.place_order(body, api_version="v2")
            oid = getattr(getattr(resp, "data", None), "order_id", "") or "UPX"
            return OrderResult(True, oid, self.name, ref_price, quantity,
                               "sandbox" if self.sandbox else "live")
        except Exception as exc:
            return OrderResult(False, "", self.name, message=str(exc))


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
