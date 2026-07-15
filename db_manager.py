"""
db_manager.py
Persistence layer: trade logging + Excel analytics export.

Immutable Rule #2 is enforced here — Paper trades go to `paper_trades`, Live
trades go to `live_trades`, and the two never mix. The collection is chosen
purely from the Environment enum, so no caller can accidentally cross-write.

MongoDB is used when reachable; otherwise the manager transparently falls back
to newline-delimited JSON files under ./data so the bot never loses trades just
because Mongo isn't installed.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any, Optional

import pandas as pd

import config
from config import Environment


PAPER_COLLECTION = "paper_trades"
LIVE_COLLECTION = "live_trades"


def _collection_name(env: Environment) -> str:
    return PAPER_COLLECTION if env == Environment.PAPER else LIVE_COLLECTION


class DBManager:
    def __init__(self):
        self.client = None
        self.db = None
        self._connect_mongo()

    # -- connection --------------------------------------------------------- #
    def _connect_mongo(self) -> None:
        try:
            from pymongo import MongoClient  # type: ignore
            client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=1500)
            client.admin.command("ping")            # force a real connection test
            self.client = client
            self.db = client[config.MONGO_DB_NAME]
            print("[DBManager] Connected to MongoDB.")
        except Exception as exc:
            print(f"[DBManager] MongoDB unavailable ({exc}); using local JSON.")
            self.client = None
            self.db = None

    @property
    def backend(self) -> str:
        return "MongoDB" if self.db is not None else "Local JSON"

    def _local_path(self, env: Environment) -> str:
        return os.path.join(config.LOCAL_DB_DIR, f"{_collection_name(env)}.jsonl")

    # -- schema ------------------------------------------------------------- #
    @staticmethod
    def new_trade(
        mode: str, environment: str, broker: str, ticker: str, side: str,
        entry_price: float, stop_loss: float, target: float, quantity: int,
        risk_amount: float, segment: str = "",
    ) -> dict[str, Any]:
        """Builds a trade doc matching the Section-5 schema."""
        return {
            "trade_id": str(uuid.uuid4()),
            "timestamp": datetime.utcnow().isoformat(),
            "mode": mode,
            "environment": environment,
            "broker": broker,
            "segment": segment,
            "ticker": ticker,
            "side": side,
            "entry_price": round(float(entry_price), 4),
            "stop_loss": round(float(stop_loss), 4),
            "target": round(float(target), 4),
            "quantity": int(quantity),
            "risk_amount": round(float(risk_amount), 2),
            "status": "OPEN",
            "exit_price": None,
            "realized_pnl": None,
            "exit_timestamp": None,
        }

    # -- create ------------------------------------------------------------- #
    def insert_trade(self, trade: dict, env: Environment) -> str:
        if self.db is not None:
            self.db[_collection_name(env)].insert_one(dict(trade))
        else:
            with open(self._local_path(env), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(trade) + "\n")
        return trade["trade_id"]

    # -- update (close a position) ----------------------------------------- #
    def close_trade(
        self, trade_id: str, exit_price: float, env: Environment
    ) -> Optional[dict]:
        if self.db is not None:
            coll = self.db[_collection_name(env)]
            doc = coll.find_one({"trade_id": trade_id})
            if not doc:
                return None
            pnl = self._pnl(doc, exit_price)
            coll.update_one(
                {"trade_id": trade_id},
                {"$set": {
                    "status": "CLOSED",
                    "exit_price": round(float(exit_price), 4),
                    "realized_pnl": round(pnl, 2),
                    "exit_timestamp": datetime.utcnow().isoformat(),
                }},
            )
            doc.update(status="CLOSED", exit_price=exit_price, realized_pnl=pnl)
            return doc
        # local fallback: rewrite the file
        rows = self._read_local(env)
        updated = None
        for r in rows:
            if r["trade_id"] == trade_id and r["status"] == "OPEN":
                r["status"] = "CLOSED"
                r["exit_price"] = round(float(exit_price), 4)
                r["realized_pnl"] = round(self._pnl(r, exit_price), 2)
                r["exit_timestamp"] = datetime.utcnow().isoformat()
                updated = r
        if updated is not None:
            with open(self._local_path(env), "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
        return updated

    @staticmethod
    def _pnl(doc: dict, exit_price: float) -> float:
        direction = 1 if doc["side"] == "BUY" else -1
        return (exit_price - doc["entry_price"]) * doc["quantity"] * direction

    # -- read --------------------------------------------------------------- #
    def _read_local(self, env: Environment) -> list[dict]:
        path = self._local_path(env)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def get_trades(self, env: Environment) -> pd.DataFrame:
        if self.db is not None:
            docs = list(self.db[_collection_name(env)].find({}, {"_id": 0}))
        else:
            docs = self._read_local(env)
        if not docs:
            return pd.DataFrame()
        df = pd.DataFrame(docs).sort_values("timestamp", ascending=False)
        return df.reset_index(drop=True)

    def get_open_trades(self, env: Environment) -> list[dict]:
        df = self.get_trades(env)
        if df.empty:
            return []
        return df[df["status"] == "OPEN"].to_dict("records")

    # -- analytics + Excel export ------------------------------------------ #
    def analytics_summary(self, env: Environment) -> dict[str, Any]:
        df = self.get_trades(env)
        closed = df[df["status"] == "CLOSED"] if not df.empty else pd.DataFrame()
        if closed.empty:
            return {"total_trades": len(df), "closed_trades": 0, "win_rate": 0.0,
                    "total_pnl": 0.0, "avg_pnl": 0.0, "best": 0.0, "worst": 0.0}
        pnl = closed["realized_pnl"].astype(float)
        wins = (pnl > 0).sum()
        return {
            "total_trades": len(df),
            "closed_trades": len(closed),
            "win_rate": round(100 * wins / len(closed), 2),
            "total_pnl": round(pnl.sum(), 2),
            "avg_pnl": round(pnl.mean(), 2),
            "best": round(pnl.max(), 2),
            "worst": round(pnl.min(), 2),
        }

    def export_excel(self, env: Environment, path: Optional[str] = None) -> str:
        """High-level analysis workbook: raw trades + a summary sheet."""
        if path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(config.LOCAL_DB_DIR,
                                f"{_collection_name(env)}_analysis_{stamp}.xlsx")
        trades = self.get_trades(env)
        summary = self.analytics_summary(env)

        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            (trades if not trades.empty else pd.DataFrame(
                columns=["trade_id"])).to_excel(
                writer, sheet_name="Trades", index=False)
            pd.DataFrame([summary]).T.rename(columns={0: "value"}).to_excel(
                writer, sheet_name="Summary")
            if not trades.empty and "mode" in trades:
                by_mode = trades[trades["status"] == "CLOSED"].groupby("mode")[
                    "realized_pnl"].agg(["count", "sum", "mean"])
                if not by_mode.empty:
                    by_mode.to_excel(writer, sheet_name="By Mode")
        return path
