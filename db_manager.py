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

    def _log_path(self, env: Environment) -> str:
        # A single, stable running workbook per environment — the "live bot
        # excel sheet". Distinct from export_excel's timestamped snapshots: this
        # one is rewritten after every insert/close so it always reflects every
        # trade the bot has taken. Environment-scoped, so live and paper never
        # share a file (Rule #2).
        return os.path.join(config.LOCAL_DB_DIR, f"{_collection_name(env)}_log.xlsx")

    def sync_excel_log(self, env: Environment) -> Optional[str]:
        """Refresh the running Excel log from the source of truth (Mongo/JSON).
        Rewritten in full so exits update the same rows as their entries. Failure
        (e.g. the file is open in Excel and locked) is logged, never raised — the
        bot must not lose a trade because a spreadsheet couldn't be written."""
        try:
            trades = self.get_trades(env)
            summary = self.analytics_summary(env)
            daily = self.daily_pnl(env)
            path = self._log_path(env)
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                (trades if not trades.empty else pd.DataFrame(
                    columns=["trade_id"])).to_excel(
                    writer, sheet_name="Trades", index=False)
                pd.DataFrame([summary]).T.rename(columns={0: "value"}).to_excel(
                    writer, sheet_name="Summary")
                daily.to_excel(writer, sheet_name="Daily PnL", index=False)
            return path
        except Exception as exc:
            print(f"[DBManager] Excel log update failed ({exc}).")
            return None

    # -- schema ------------------------------------------------------------- #
    @staticmethod
    def new_trade(
        mode: str, environment: str, broker: str, ticker: str, side: str,
        entry_price: float, stop_loss: float, target: float, quantity: int,
        risk_amount: float, segment: str = "", contract_multiplier: int = 1,
        strategy: str = "", entry_reason: str = "",
    ) -> dict[str, Any]:
        """Builds a trade doc matching the Section-5 schema."""
        return {
            "trade_id": str(uuid.uuid4()),
            "timestamp": datetime.utcnow().isoformat(),
            "mode": mode,
            # Which registered strategy produced this trade. A mode can host
            # several, so without this you can't tell them apart in the logs.
            "strategy": strategy,
            # The pattern / price-action that triggered this entry — the SAME text
            # the dashboard shows in its "reason" column (e.g. "Price>VWAP + MACD
            # bullish cross"). Persisted so the Excel log and analytics record WHY
            # each trade was taken, not just its numbers.
            "entry_reason": entry_reason,
            "environment": environment,
            "broker": broker,
            "segment": segment,
            "ticker": ticker,
            "side": side,
            "entry_price": round(float(entry_price), 4),
            "stop_loss": round(float(stop_loss), 4),
            "target": round(float(target), 4),
            "quantity": int(quantity),
            # Stored per-trade rather than looked up at close time: contract specs
            # change at expiry, and a closed trade's PnL must stay reproducible
            # from the document itself.
            "contract_multiplier": int(contract_multiplier),
            "risk_amount": round(float(risk_amount), 2),
            "status": "OPEN",
            "exit_price": None,
            "realized_pnl": None,
            "exit_timestamp": None,
            # Why the position was closed — "TARGET", "STOP-LOSS" or "TIME-EXIT".
            # Filled by close_trade so the log reads the full story: the entry
            # pattern that opened it and the exit condition that closed it.
            "exit_reason": None,
        }

    # -- create ------------------------------------------------------------- #
    def insert_trade(self, trade: dict, env: Environment) -> str:
        if self.db is not None:
            self.db[_collection_name(env)].insert_one(dict(trade))
        else:
            with open(self._local_path(env), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(trade) + "\n")
        self.sync_excel_log(env)
        return trade["trade_id"]

    # -- update (close a position) ----------------------------------------- #
    def close_trade(
        self, trade_id: str, exit_price: float, env: Environment,
        exit_reason: str = "",
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
                    "exit_reason": exit_reason,
                }},
            )
            doc.update(status="CLOSED", exit_price=exit_price, realized_pnl=pnl,
                       exit_reason=exit_reason)
            self.sync_excel_log(env)
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
                r["exit_reason"] = exit_reason
                updated = r
        if updated is not None:
            with open(self._local_path(env), "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
            self.sync_excel_log(env)
        return updated

    @staticmethod
    def _pnl(doc: dict, exit_price: float) -> float:
        """Realized PnL in rupees. Direction-aware (a SELL profits when price
        falls) and multiplier-aware for commodities. Trades written before
        contract_multiplier existed default to 1, which is what they assumed."""
        direction = 1 if doc["side"] == "BUY" else -1
        mult = int(doc.get("contract_multiplier", 1) or 1)
        return ((exit_price - doc["entry_price"]) * doc["quantity"]
                * direction * mult)

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

    # -- destructive: wipe an environment ---------------------------------- #
    def reset_environment(self, env: Environment) -> dict[str, Any]:
        """Permanently delete ALL trades for ONE environment and its running Excel
        log, returning the bot to a blank slate. IRREVERSIBLE.

        Scoped to a single environment on purpose — Immutable Rule #2 keeps paper
        and live separate, so resetting the paper book must never touch the live
        one (and vice-versa). Returns counts of what was removed for the UI to
        confirm. The whole-history day-wise record is derived from these trades, so
        clearing them clears every past day too — a true fresh start."""
        removed = 0
        files: list[str] = []
        if self.db is not None:
            res = self.db[_collection_name(env)].delete_many({})
            removed = int(getattr(res, "deleted_count", 0) or 0)
        else:
            path = self._local_path(env)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    removed = sum(1 for line in fh if line.strip())
                os.remove(path)
                files.append(path)
        # Drop the running Excel workbook too, so it doesn't resurrect old numbers.
        log = self._log_path(env)
        if os.path.exists(log):
            try:
                os.remove(log)
                files.append(log)
            except OSError as exc:            # e.g. open in Excel and locked
                print(f"[DBManager] Could not remove {log} ({exc}).")
        print(f"[DBManager] Reset {_collection_name(env)}: removed {removed} "
              f"trade(s).")
        return {"trades_removed": removed, "files_removed": files}

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

    # -- day-wise tracking -------------------------------------------------- #
    # The "trading day" of a trade is the calendar date of its (UTC) timestamp.
    # Indian market hours (≈03:30–18:00 UTC for the equity + MCX evening session)
    # never cross a UTC-date boundary, so this date is also the IST trading date,
    # and it rolls over at 00:00 UTC = 05:30 IST — i.e. every pre-market morning,
    # which is exactly when the live day-PnL counter should reset to zero.
    @staticmethod
    def today_key() -> str:
        """The current trading-day key, consistent with how trades are stamped."""
        return datetime.utcnow().date().isoformat()

    @staticmethod
    def _trade_day(ts: Any) -> str:
        return str(ts)[:10] if ts else ""

    def today_realized(self, env: Environment, day: Optional[str] = None) -> float:
        """Realized PnL booked on ONE trading day (default: today).

        This is the source of truth for the live "today's PnL" figure. Because it
        is computed from stored trades, it survives a restart — the number the bot
        shows after you reopen it is rebuilt from disk, not lost. Grouping by the
        entry timestamp's date means each day owns the trades opened that day, so a
        new morning starts at ₹0 while yesterday stays on record."""
        df = self.get_trades(env)
        if df.empty:
            return 0.0
        day = day or self.today_key()
        closed = df[df["status"] == "CLOSED"].copy()
        if closed.empty:
            return 0.0
        closed["_day"] = closed["timestamp"].map(self._trade_day)
        today = closed[closed["_day"] == day]
        if today.empty:
            return 0.0
        return round(float(today["realized_pnl"].astype(float).sum()), 2)

    def daily_pnl(self, env: Environment) -> pd.DataFrame:
        """Day-wise history: one row per trading day, newest first. This is the
        permanent record the dashboard reads so past days are never lost on a
        restart — every day the bot has ever traded stays here, on disk."""
        df = self.get_trades(env)
        if df.empty:
            return pd.DataFrame(columns=[
                "Date", "Trades", "Closed", "Open", "Wins", "Win Rate %",
                "Realized PnL (₹)"])
        df = df.copy()
        df["_day"] = df["timestamp"].map(self._trade_day)
        rows = []
        for day, g in df.groupby("_day"):
            closed = g[g["status"] == "CLOSED"]
            pnl = closed["realized_pnl"].astype(float) if not closed.empty \
                else pd.Series(dtype=float)
            wins = int((pnl > 0).sum())
            rows.append({
                "Date": day,
                "Trades": int(len(g)),
                "Closed": int(len(closed)),
                "Open": int((g["status"] == "OPEN").sum()),
                "Wins": wins,
                "Win Rate %": round(100 * wins / len(closed), 2) if len(closed)
                else 0.0,
                "Realized PnL (₹)": round(float(pnl.sum()), 2),
            })
        out = pd.DataFrame(rows).sort_values("Date", ascending=False)
        return out.reset_index(drop=True)

    def export_excel(self, env: Environment, path: Optional[str] = None) -> str:
        """High-level analysis workbook: raw trades + a summary sheet."""
        if path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(config.LOCAL_DB_DIR,
                                f"{_collection_name(env)}_analysis_{stamp}.xlsx")
        trades = self.get_trades(env)
        summary = self.analytics_summary(env)
        daily = self.daily_pnl(env)

        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            (trades if not trades.empty else pd.DataFrame(
                columns=["trade_id"])).to_excel(
                writer, sheet_name="Trades", index=False)
            pd.DataFrame([summary]).T.rename(columns={0: "value"}).to_excel(
                writer, sheet_name="Summary")
            daily.to_excel(writer, sheet_name="Daily PnL", index=False)
            if not trades.empty and "mode" in trades:
                by_mode = trades[trades["status"] == "CLOSED"].groupby("mode")[
                    "realized_pnl"].agg(["count", "sum", "mean"])
                if not by_mode.empty:
                    by_mode.to_excel(writer, sheet_name="By Mode")
        return path
