"""
app.py
Streamlit front-end. Sidebar for global controls, three tabs for the main views.

Run:  streamlit run app.py

The engine runs in a background thread, so the UI is a thin control + display
layer. It reads a thread-safe snapshot each rerun and never touches broker or
strategy logic directly (keeping the separation from Immutable Rule #3).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config
from config import (Broker, Environment, Mode, Segment, ALL_INSTRUMENTS,
                    instruments_for_segment)
from db_manager import DBManager
from engine import TradingEngine
import backtester
import broker_api
import strategy
import upstox_auth

st.set_page_config(page_title="Algo Trading Bot", page_icon="📈", layout="wide")

# --------------------------------------------------------------------------- #
#  Session state
# --------------------------------------------------------------------------- #
if "engine" not in st.session_state:
    st.session_state.engine = None
if "db" not in st.session_state:
    st.session_state.db = DBManager()

db: DBManager = st.session_state.db


# --------------------------------------------------------------------------- #
#  Upstox token refresh (OAuth). Live tokens expire daily (~03:30 IST), so the
#  UI lets you re-auth in-place: log in at Upstox, come back, token is saved to
#  .env and activated live (no restart). Works two ways —
#    • Auto-capture: if your Upstox app's redirect URI points at THIS Streamlit
#      URL, login redirects back here with ?code=... and we exchange it instantly.
#    • Manual paste: land on any registered redirect URI, copy the URL, paste it.
# --------------------------------------------------------------------------- #
def refresh_token_from_code(code: str) -> tuple[bool, str]:
    api_key, api_secret, redirect_uri = upstox_auth.get_credentials()
    if not (api_key and api_secret):
        return False, "UPSTOX_LIVE_API_KEY / UPSTOX_LIVE_SECRET missing in .env."
    res = upstox_auth.exchange_code(code, api_key, api_secret, redirect_uri)
    if not res["ok"]:
        return False, res["error"]
    try:
        upstox_auth.save_token(res["token"])
    except Exception:
        return False, ("Token fetched but .env couldn't be written "
                       "(python-dotenv missing). Copy it manually.")
    config.reload_tokens()   # activate the new token in this running process
    who = res.get("user_name") or res.get("email") or "user"
    return True, f"✅ Token refreshed for {who}. Saved to .env and active now."


# Auto-capture: handle a redirect that landed back on this app with ?code=...
_code = st.query_params.get("code")
if _code and st.session_state.get("_last_code") != _code:
    st.session_state["_last_code"] = _code
    ok, msg = refresh_token_from_code(_code)
    st.session_state["_token_msg"] = ("success" if ok else "error", msg)
    try:
        del st.query_params["code"]        # codes are single-use; drop from URL
    except Exception:
        st.query_params.clear()


# --------------------------------------------------------------------------- #
#  Sidebar — global controls
# --------------------------------------------------------------------------- #
st.sidebar.title("⚙️ Controls")

env_label = st.sidebar.radio(
    "Environment", ["Paper Trading (Sandbox)", "Live Trading"], index=0)
environment = Environment.LIVE if env_label.startswith("Live") else Environment.PAPER

MODE_LABELS = {
    "Intraday (15m)": Mode.INTRADAY,
    "Swing (Daily)": Mode.SWING,
    "⚡ Aggressive Scalper (1m)": Mode.SCALPER,
}
mode_label = st.sidebar.radio("Trading Mode", list(MODE_LABELS), index=0)
mode = MODE_LABELS[mode_label]

# --- Strategy picker -------------------------------------------------------- #
# The mode fixes the timeframe; the strategy is chosen separately, so one mode can
# host several. The list is built from the registry, so a newly registered
# strategy appears here with no change to this file.
_choices = strategy.strategies_for_mode(mode)
_names = [s.name for s in _choices]
_default_key = strategy.default_strategy(mode).key
_default_ix = next((i for i, s in enumerate(_choices) if s.key == _default_key), 0)
_picked_name = st.sidebar.selectbox("Strategy", _names, index=_default_ix,
                                    key="live_strategy")
selected_strategy = next(s for s in _choices if s.name == _picked_name)

_p = selected_strategy.params
st.sidebar.caption(f"_{selected_strategy.summary}_")
st.sidebar.caption(
    f"**{_p.timeframe}** · risk **{_p.risk_per_trade:.0%}**/trade · "
    f"RR **1:{_p.risk_reward:g}** · SL **{_p.atr_sl_mult:g}×ATR({_p.atr_period})**"
    + (f" · {'long+short' if _p.allow_short else 'long only'}")
    + (f" · exit after {_p.max_hold_minutes}m" if _p.max_hold_minutes else "")
)

broker_choice = Broker.SIMULATED
if environment == Environment.LIVE:
    broker_name = st.sidebar.selectbox(
        "Live Broker", ["Upstox", "Dhan", "Kotak Neo"])
    broker_choice = {"Upstox": Broker.UPSTOX, "Dhan": Broker.DHAN,
                     "Kotak Neo": Broker.KOTAK}[broker_name]
    st.sidebar.warning("⚠️ Live mode places REAL orders when broker "
                       "credentials are present in .env.")

st.sidebar.markdown("### Universe")
segments = st.sidebar.multiselect(
    "Segments",
    ["NSE Equity", "MCX Commodity"],
    default=["NSE Equity", "MCX Commodity"],
)
universe: list = []
if "NSE Equity" in segments:
    universe += instruments_for_segment(Segment.EQUITY)
if "MCX Commodity" in segments:
    universe += instruments_for_segment(Segment.MCX)

# The equity universe is now the full Nifty 100 (~114 names). Selecting ALL of
# them by default would subscribe 100+ live WebSocket feeds the moment the bot
# starts, so the default is capped to a manageable slice — every instrument is
# still selectable, and the Backtesting tab's bulk test can use the whole list.
_DEFAULT_UNIVERSE_CAP = 15
_universe_syms = [i.symbol for i in universe]
symbols = st.sidebar.multiselect(
    "Instruments",
    _universe_syms,
    default=_universe_syms[:_DEFAULT_UNIVERSE_CAP],
    help=f"{len(_universe_syms)} instruments available (Nifty 100 + MCX). "
         "Add or remove as you like.",
)
selected = [i for i in universe if i.symbol in symbols]

capital = st.sidebar.number_input(
    "Total Capital (₹)", min_value=10_000.0, value=float(config.TOTAL_CAPITAL),
    step=10_000.0)

# --------------------------------------------------------------------------- #
#  MCX commodity settings — FIXED lots per symbol (separate from equity).
#
#  Commodities are NOT risk-sized like equity. Here the user pre-selects how many
#  lots to trade per symbol when a signal fires; the strategy still decides the
#  SL/TP price levels, and the margin follows automatically from the lots. The
#  estimate below uses each contract's reference price and effective leverage so
#  the user sees the funding requirement before starting the bot.
# --------------------------------------------------------------------------- #
mcx_selected = [i for i in selected if i.segment == Segment.MCX]
mcx_lots: dict[str, int] = {}


@st.cache_data(ttl=300, show_spinner=False)
def _mcx_margin_preview(instrument_key: str, symbol: str, lots: int,
                        ref_price: float, mult: int,
                        token: str, is_paper: bool) -> tuple[float, str]:
    """Margin (₹) for the sidebar preview, cached for 5 min so the auto-refreshing
    UI doesn't hammer the margin API. Mirrors engine._mcx_margin exactly:
      PAPER: hardcoded per-lot table (config.MCX_MARGIN_PER_LOT) → notional
      LIVE:  live Upstox fetch → hardcoded table → notional÷leverage
    `lots` is the LOT COUNT (Upstox counts MCX quantity in lots), so margin is
    per_lot × lots. `token`/`is_paper` are in the cache key so switching either
    re-computes."""
    if lots <= 0:
        return 0.0, "none"
    # PAPER never calls the live API — it uses the hardcoded broker-side figures.
    if not is_paper:
        m = broker_api.fetch_upstox_margin(token, instrument_key, lots, "BUY", "D")
        if m and m > 0:
            return float(m), "live"
    per_lot = config.mcx_margin_per_lot(symbol)
    if per_lot > 0:
        return per_lot * lots, "hardcoded"
    lev = config.SEGMENT_MAX_LEVERAGE.get(Segment.MCX, 1.0)
    notional = ref_price * lots * mult
    return (notional / lev if lev > 0 else notional), "notional"


if mcx_selected:
    _tok = config.UPSTOX_LIVE_ACCESS_TOKEN or config.UPSTOX_SANDBOX_TOKEN
    _is_paper = environment == Environment.PAPER
    _SRC_LABEL = {"live": "live from broker", "hardcoded": "fixed broker-side rate",
                  "notional": "rough estimate", "none": ""}
    with st.sidebar.expander("🛢️ MCX Commodity Settings", expanded=True):
        st.caption(
            "Commodities trade a **fixed number of lots** you set here — not "
            "risk-based sizing. The strategy still sets SL/TP; margin is a "
            "**fixed per-lot** broker-side figure (₹/lot × lots) in Paper mode, "
            "and the broker's real SPAN+exposure margin in Live mode. "
            "No % cap applies — the only limit is the margin your capital can fund.")
        for inst in mcx_selected:
            lots = st.number_input(
                f"{inst.symbol} — lots per trade", min_value=0, max_value=1000,
                value=1, step=1, key=f"mcx_lots_{inst.symbol}",
                help=f"Lot size {inst.lot_size} · quoted per contract. 0 = don't "
                     f"trade this commodity.")
            mcx_lots[inst.symbol] = int(lots)
            margin, src = _mcx_margin_preview(
                inst.instrument_key, inst.symbol, int(lots),
                inst.reference_price, max(inst.contract_multiplier, 1), _tok,
                _is_paper)
            if int(lots) <= 0:
                st.caption("_0 lots — this commodity won't be traded._")
            else:
                st.caption(f"≈ **₹{margin:,.0f}** margin for {int(lots)} lot(s) "
                           f"· _{_SRC_LABEL.get(src, src)}_")

st.sidebar.markdown("---")
col_a, col_b = st.sidebar.columns(2)
start_clicked = col_a.button("▶️ Start Bot", use_container_width=True,
                             type="primary")
stop_clicked = col_b.button("⏹️ Stop Bot", use_container_width=True)

if start_clicked:
    if not selected:
        st.sidebar.error("Select at least one instrument.")
    else:
        if st.session_state.engine and st.session_state.engine.state.running:
            st.session_state.engine.stop()
        eng = TradingEngine(environment, mode, broker_choice, selected, capital,
                            strategy_key=selected_strategy.key, mcx_lots=mcx_lots)
        eng.start()
        st.session_state.engine = eng
        st.sidebar.success(f"Bot started — {selected_strategy.name}.")

if stop_clicked and st.session_state.engine:
    st.session_state.engine.stop()
    st.sidebar.info("Bot stopped.")

# --------------------------------------------------------------------------- #
#  Sidebar — Upstox token refresh panel
# --------------------------------------------------------------------------- #
st.sidebar.markdown("---")
_pending_msg = st.session_state.get("_token_msg")
with st.sidebar.expander("🔑 Upstox Token", expanded=bool(_pending_msg)):
    if _pending_msg:
        kind, text = st.session_state.pop("_token_msg")
        (st.success if kind == "success" else st.error)(text)

    _tok = config.UPSTOX_LIVE_ACCESS_TOKEN
    if _tok:
        st.caption(f"Current live token: `{_tok[:8]}…`  ({len(_tok)} chars)")
    else:
        st.caption("No live token set.")

    api_key, api_secret, redirect_uri = upstox_auth.get_credentials()
    if not (api_key and api_secret):
        st.warning("Add **UPSTOX_LIVE_API_KEY** and **UPSTOX_LIVE_SECRET** to "
                   ".env to refresh the token from here.")
    else:
        login_url = upstox_auth.build_login_url(api_key, redirect_uri)
        st.link_button("1) Log in at Upstox ↗", login_url,
                       use_container_width=True)
        st.caption(f"Redirect URI: `{redirect_uri}`  — must EXACTLY match the "
                   "one registered in your Upstox app.")
        pasted = st.text_input(
            "2) Paste the redirected URL (or just the code)",
            key="tok_paste", placeholder="https://127.0.0.1:5000/?code=...")
        if st.button("3) Exchange & Save Token", use_container_width=True,
                     type="primary"):
            code = upstox_auth.extract_code(pasted)
            if not code:
                st.error("Couldn't find an authorization code in that input.")
            else:
                ok, msg = refresh_token_from_code(code)
                st.session_state["_token_msg"] = (
                    "success" if ok else "error", msg)
                st.rerun()

    if st.button("Check token validity", use_container_width=True):
        r = upstox_auth.check_token(config.UPSTOX_LIVE_ACCESS_TOKEN)
        if r["ok"]:
            st.success(f"Valid ✓ — {r['user_name']}")
        else:
            st.error(r["error"])

engine = st.session_state.engine


# --------------------------------------------------------------------------- #
#  Header
# --------------------------------------------------------------------------- #
st.title("📈 Automated Trading Bot")
st.caption(f"Environment: **{environment.value}**  |  Mode: **{mode.value}**  "
           f"|  Strategy: **{selected_strategy.name}**  "
           f"|  MCX commodities trade until 23:30 IST")

_live_eng = st.session_state.engine
if (_live_eng and _live_eng.state.running
        and _live_eng.strategy.key != selected_strategy.key):
    # The sidebar shows what WOULD start; the engine keeps trading what it was
    # started with. Saying so prevents "I switched strategy" surprises.
    st.warning(
        f"⚠️ The bot is still running **{_live_eng.strategy.name}**. Your "
        f"selection (**{selected_strategy.name}**) takes effect when you press "
        f"**Start Bot** again.")

tab_dash, tab_pos, tab_act, tab_logs, tab_bt = st.tabs(
    ["🖥️ Live Dashboard", "📌 Holdings", "📝 Activity Log",
     "📒 Trade Log & Analytics", "🧪 Backtesting Engine"])


# --------------------------------------------------------------------------- #
#  Live views
#
#  Auto-refresh is done with st.fragment(run_every=...), which reruns ONLY that
#  fragment on a timer. Crucially it does NOT reload the page, so st.session_state
#  (and the running engine handle) survive — a full-page meta-refresh would start
#  a new session, drop the engine, and make the bot look like it "stopped" after a
#  few seconds while leaking the old background thread. The engine is re-read from
#  session_state on every fragment rerun so it always reflects the latest state.
# --------------------------------------------------------------------------- #
_eng0 = st.session_state.engine
_running = bool(_eng0 and _eng0.state.running)
# The Scalper works 1-minute bars with a 7-minute time exit, so the UI has to keep
# up with it; the slower modes don't need a 1s cadence.
_refresh = (1 if (_eng0 and _eng0.mode == Mode.SCALPER) else 2) if _running else None


def _need_engine() -> bool:
    if st.session_state.engine is None:
        st.info("Bot not started. Configure the sidebar and press **Start Bot**. "
                "It runs in Paper/Simulation mode out of the box — no broker "
                "tokens required.")
        return False
    return True


def _position_row(sym: str, t: dict) -> dict:
    """Mark-to-market display values for one open position."""
    lp = t.get("_live_price", t["entry_price"])
    direction = 1 if t["side"] == "BUY" else -1
    mult = int(t.get("contract_multiplier", 1) or 1)
    upnl = (lp - t["entry_price"]) * t["quantity"] * direction * mult
    risk = abs(t["entry_price"] - t["stop_loss"])
    # How far price has travelled from entry toward the target, as a fraction
    # of the risk taken. +1.0R == at target, -1.0R == at stop.
    r_mult = ((lp - t["entry_price"]) * direction / risk) if risk else 0.0
    return {
        "Symbol": sym,
        "Side": "🟢 LONG" if t["side"] == "BUY" else "🔴 SHORT",
        "Qty": t["quantity"],
        "Entry": round(t["entry_price"], 2), "LTP": round(lp, 2),
        "SL": round(t["stop_loss"], 2), "Target": round(t["target"], 2),
        "R": round(r_mult, 2),
        "Unreal PnL (₹)": round(upnl, 2),
    }


# Column widths shared by the open-positions header and each trade row, so the
# ❌ Close button lines up under its own column.
_POS_COLS = [1.6, 1.3, 0.8, 1.1, 1.1, 1.1, 1.1, 0.8, 1.4, 1.2]
_POS_HEADERS = ["Symbol", "Side", "Qty", "Entry", "LTP", "SL", "Target", "R",
                "Unreal PnL (₹)", "Action"]


def _render_open_positions(eng, snap: dict) -> None:
    """One row per open trade, each with its own ❌ Close button for a manual,
    at-market exit. Kept as individual rows (not a single dataframe) precisely so
    every trade can carry its own button."""
    positions = snap["open_positions"]
    if not positions:
        st.write("No open positions.")
        return
    head = st.columns(_POS_COLS)
    for col, label in zip(head, _POS_HEADERS):
        col.markdown(f"**{label}**")
    for sym, t in positions.items():
        r = _position_row(sym, t)
        row = st.columns(_POS_COLS)
        row[0].write(r["Symbol"])
        row[1].write(r["Side"])
        row[2].write(str(r["Qty"]))
        row[3].write(f"{r['Entry']:.2f}")
        row[4].write(f"{r['LTP']:.2f}")
        row[5].write(f"{r['SL']:.2f}")
        row[6].write(f"{r['Target']:.2f}")
        row[7].write(f"{r['R']:+.2f}")
        row[8].write(f"{r['Unreal PnL (₹)']:,.2f}")
        # Stable per-symbol key so Streamlit keeps the buttons distinct across reruns.
        if row[9].button("❌ Close", key=f"close_{sym}",
                         help=f"Close {sym} now at the latest price"):
            if eng.close_position(sym):
                st.toast(f"Closed {sym} at market.")
            else:
                st.toast(f"{sym} was already closed.")
            st.rerun()
    st.caption("**R** = progress in units of risk: +1.00 is the target, "
               "−1.00 is the stop. LTP updates from WebSocket ticks. "
               "**❌ Close** squares the position off immediately at the latest "
               "price — the exit is logged with reason `MANUAL`.")


def _pnl_header(snap: dict) -> None:
    """The number the user actually watches — driven by WebSocket ticks. This is
    TODAY's PnL: it resets each morning but every past day is kept on disk (see the
    Day-wise PnL table), so a restart rebuilds today's figure instead of losing it."""
    total, real, unreal = (snap["day_pnl"], snap["realized_pnl"],
                           snap["unrealized_pnl"])
    day = snap.get("trading_day") or ""
    c1, c2, c3 = st.columns([2, 1, 1])
    c1.metric(f"💰 Today's PnL (₹){f' · {day}' if day else ''}", f"{total:,.2f}",
              delta=f"{unreal:+,.2f} open", delta_color="normal")
    c2.metric("Realized today (₹)", f"{real:,.2f}")
    c3.metric("Open positions", len(snap["open_positions"]))


def _daily_pnl_frame(snap: dict) -> pd.DataFrame:
    """Day-wise history from the engine snapshot (rebuilt from storage)."""
    return pd.DataFrame(snap.get("daily_pnl") or [])


@st.fragment(run_every=_refresh)
def render_dashboard() -> None:
    if not _need_engine():
        return
    snap = st.session_state.engine.state.snapshot()

    _pnl_header(snap)
    eng = st.session_state.engine
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Feed", snap["feed_status"])
    c2.metric("Strategy", eng.strategy.name)
    c3.metric("Broker", snap["broker_name"])
    c4.metric("Storage", snap["db_backend"])

    st.subheader("📶 Live Market Data (WebSocket)")
    st.caption("Proof the stream is alive. **Source** must read `WS` and "
               "**Tick age** must stay low while the market is open — `REST` "
               "means the socket dropped and it fell back to polling.")
    if snap["live_quotes"]:
        st.dataframe(pd.DataFrame(list(snap["live_quotes"].values())),
                     use_container_width=True, hide_index=True)
    else:
        st.write("Waiting for the first tick…")

    st.subheader("📡 Detected Signals")
    if snap["last_signals"]:
        sig_df = pd.DataFrame(snap["last_signals"]).rename(
            columns={"deployed": "Capital Deployed (₹)"})
        st.dataframe(sig_df, use_container_width=True, hide_index=True)
    else:
        st.write("No signals yet — waiting for strategy conditions to align.")

    st.subheader("📅 Day-wise PnL")
    st.caption("One row per trading day, newest first. Persisted to local storage, "
               "so every past day survives a restart — today's live figure above "
               "resets each morning; the days below never disappear.")
    daily = _daily_pnl_frame(snap)
    if daily.empty:
        st.write("No trading days recorded yet.")
    else:
        st.dataframe(daily, use_container_width=True, hide_index=True)

    st.caption(f"🔄 Live — refreshing every {_refresh}s" if snap["running"]
               else "⏸️ Bot stopped.")


@st.fragment(run_every=_refresh)
def render_holdings() -> None:
    if not _need_engine():
        return
    eng = st.session_state.engine
    snap = eng.state.snapshot()
    _pnl_header(snap)
    st.subheader("📌 Currently Open")
    _render_open_positions(eng, snap)
    st.caption(f"🔄 Live — refreshing every {_refresh}s" if snap["running"]
               else "⏸️ Bot stopped.")


@st.fragment(run_every=_refresh)
def render_activity() -> None:
    if not _need_engine():
        return
    snap = st.session_state.engine.state.snapshot()
    st.subheader("📝 Activity Log")
    st.caption("Newest first. Entries, exits, rejections and feed problems.")
    n = st.slider("Lines to show", 20, 200, 60, step=20, key="log_lines")
    st.code("\n".join(snap["log"][:n]) or "—", language="text")
    st.caption(f"🔄 Live — refreshing every {_refresh}s" if snap["running"]
               else "⏸️ Bot stopped.")


with tab_dash:
    render_dashboard()

with tab_pos:
    render_holdings()

with tab_act:
    render_activity()


# --------------------------------------------------------------------------- #
#  Tab 2 — Trade Logs & Analytics
# --------------------------------------------------------------------------- #
with tab_logs:
    view_env = st.radio("Show trades from", ["Paper", "Live"], horizontal=True)
    env_sel = Environment.PAPER if view_env == "Paper" else Environment.LIVE

    summary = db.analytics_summary(env_sel)
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Total Trades", summary["total_trades"])
    s2.metric("Win Rate %", summary["win_rate"])
    s3.metric("Total PnL (₹)", summary["total_pnl"])
    s4.metric("Avg PnL (₹)", summary["avg_pnl"])

    st.subheader("📅 Day-wise PnL")
    st.caption("Rebuilt from stored trades — the permanent day-by-day record. "
               "Visible even when the bot is stopped, because it reads from disk.")
    daily = db.daily_pnl(env_sel)
    if daily.empty:
        st.write("No trading days recorded yet.")
    else:
        st.dataframe(daily, use_container_width=True, hide_index=True)

    trades = db.get_trades(env_sel)
    st.subheader(f"{view_env} Trade History  ·  collection: "
                 f"`{'paper_trades' if env_sel==Environment.PAPER else 'live_trades'}`")
    if trades.empty:
        st.write("No trades recorded yet.")
    else:
        st.dataframe(trades, use_container_width=True, hide_index=True)

    if st.button("⬇️ Export High-Level Analysis to Excel"):
        path = db.export_excel(env_sel)
        st.success(f"Exported to: {path}")
        try:
            with open(path, "rb") as fh:
                st.download_button("Download workbook", fh.read(),
                                   file_name=path.split("\\")[-1])
        except Exception:
            pass

    # -- Danger zone: full portfolio reset --------------------------------- #
    with st.expander("🧨 Danger Zone — Reset Portfolio"):
        st.warning(
            f"This permanently deletes **all {view_env} trades** recorded till "
            "date (every day, closed and open) and the running Excel log, then "
            "starts fresh from zero. **This cannot be undone.** Only the "
            f"**{view_env}** book is affected — the other environment is untouched.")
        st.caption("Tip: export to Excel first if you want a copy before wiping.")

        eng = st.session_state.engine
        eng_here = eng is not None and eng.environment == env_sel
        if eng_here and eng.state.snapshot()["open_positions"]:
            st.error("⚠️ There are OPEN positions in this environment. For live "
                     "trading, close them first — resetting only forgets them "
                     "here, it does not square off real broker positions.")

        confirm = st.checkbox(
            f"I understand — permanently delete all {view_env} data.",
            key="reset_confirm")
        if st.button("🗑️ Reset Portfolio Now", type="primary", disabled=not confirm,
                     key="reset_btn"):
            # Use the engine when it owns THIS environment so the live dashboard is
            # cleared too; otherwise just wipe storage.
            if eng_here:
                stats = eng.reset_portfolio()
            else:
                stats = db.reset_environment(env_sel)
            st.session_state.reset_confirm = False
            st.success(f"Portfolio reset — removed {stats['trades_removed']} "
                       f"{view_env} trade(s). Starting fresh.")
            st.rerun()


# --------------------------------------------------------------------------- #
#  Tab 3 — Backtesting Engine
# --------------------------------------------------------------------------- #
with tab_bt:
    bc1, bc2, bc3 = st.columns(3)
    ticker = bc1.selectbox("Ticker", [i.symbol for i in ALL_INSTRUMENTS])
    BT_MODES = {"Swing": Mode.SWING, "Intraday": Mode.INTRADAY,
                "Scalper": Mode.SCALPER}
    bt_mode = bc2.selectbox("Mode", list(BT_MODES))
    init_cap = bc3.number_input("Initial Capital (₹)", value=100_000.0,
                                step=10_000.0)

    _bt_choices = strategy.strategies_for_mode(BT_MODES[bt_mode])
    _bt_name = st.selectbox("Strategy", [s.name for s in _bt_choices],
                            key="bt_strategy")
    bt_strategy = next(s for s in _bt_choices if s.name == _bt_name)
    st.caption(f"_{bt_strategy.summary}_")
    # 1-minute history is only available for a short window, so a year-long
    # Scalper range would return nothing usable and silently fall back to
    # synthetic data. Default to a range each mode can actually be tested over.
    _default_span = {"Swing": 365, "Intraday": 30, "Scalper": 5}[bt_mode]
    d1, d2 = st.columns(2)
    start_d = d1.date_input("Start",
                            value=date.today() - timedelta(days=_default_span))
    end_d = d2.date_input("End", value=date.today())
    if bt_mode == "Scalper":
        st.caption("⚡ Scalper backtests run on **1-minute** candles. Upstox "
                   "serves only a short window of minute history — keep the "
                   "range to a few days or it will fall back to synthetic data.")

    if st.button("🚀 Run Backtest", type="primary"):
        inst = config.INSTRUMENTS_BY_SYMBOL[ticker]
        with st.spinner(f"Backtesting {bt_strategy.name} on {ticker}..."):
            result = backtester.run_backtest(
                ticker, str(start_d), str(end_d), init_cap,
                BT_MODES[bt_mode], lot_size=inst.lot_size,
                strategy_key=bt_strategy.key,
            )
        m = result.metrics
        # Make the data source explicit. A silent fall-back to synthetic
        # random-walk prices is exactly what made backtest trade prices not
        # match the real instrument — surface it loudly instead of hiding it.
        _src = m.get("Data Source", "synthetic")
        if _src == "upstox":
            st.success(f"📈 Real Upstox historical candles for {ticker}.")
        elif _src == "yfinance":
            st.info(f"📊 Real yfinance historical candles for {ticker}.")
        else:
            st.warning(
                "⚠️ **Synthetic data** — these trades are on a simulated "
                "random walk, NOT real prices for this instrument, so the "
                "entry/exit prices will not match the market. Upstox history "
                "was unavailable for this range (intraday/scalper minute data "
                "is limited; try a shorter range or check the access token).")
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Total Return %", m["Total Return %"])
        k2.metric("Max Drawdown %", m["Max Drawdown %"])
        k3.metric("Sharpe", m["Sharpe"])
        k4.metric("Calmar", m["Calmar"])
        k5.metric("Win Rate %", m["Win Rate %"])
        st.caption(f"Trades: {m['Total Trades']}  ·  Final Equity: "
                   f"₹{m['Final Equity']:.2f}")

        if not result.equity_curve.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=result.equity_curve.index, y=result.equity_curve.values,
                mode="lines", name="Equity", line=dict(color="#2E86DE")))
            fig.update_layout(title="Equity Curve", height=420,
                              xaxis_title="Time", yaxis_title="Equity (₹)")
            st.plotly_chart(fig, use_container_width=True)

        if not result.trades.empty:
            st.subheader("Backtest Trades")
            st.dataframe(result.trades, use_container_width=True, hide_index=True)
        else:
            st.info("No trades were generated in this window. Try a wider date "
                    "range or the other mode.")

    # ----------------------------------------------------------------------- #
    #  Bulk / bucket backtest — same strategy + params across many instruments,
    #  all equity curves overlaid in one chart for side-by-side comparison.
    # ----------------------------------------------------------------------- #
    st.markdown("---")
    st.subheader("🧺 Bulk Backtest (compare a bucket)")
    st.caption("Pick several instruments and run the **same strategy with the "
               "same parameters** on all of them. Every equity curve is drawn "
               "in the one chart (a different colour each) so you can see which "
               "symbols the strategy performed best on.")

    bulk_symbols = st.multiselect(
        "Bucket — instruments to test together",
        [i.symbol for i in ALL_INSTRUMENTS],
        key="bt_bulk_symbols",
        help="All use the Mode, Strategy, capital and date range selected above.")
    norm_bulk = st.checkbox(
        "Normalise curves to % return (start all at 0%)", value=True,
        key="bt_bulk_norm",
        help="Recommended for comparison — removes the effect of each symbol "
             "starting from the same capital and lets you compare shapes.")

    if st.button("🚀 Run Bulk Backtest", key="bt_bulk_run"):
        if not bulk_symbols:
            st.error("Select at least one instrument for the bucket.")
        else:
            prog = st.progress(0.0, text="Starting…")

            def _cb(done, total, sym):
                prog.progress(done / total, text=f"{sym} ({done}/{total})")

            with st.spinner(f"Backtesting {bt_strategy.name} across "
                            f"{len(bulk_symbols)} instruments..."):
                bulk_results = backtester.run_bulk_backtest(
                    bulk_symbols, str(start_d), str(end_d), init_cap,
                    BT_MODES[bt_mode], strategy_key=bt_strategy.key,
                    progress_cb=_cb)
            prog.empty()

            summary = backtester.bulk_summary_frame(bulk_results)

            # Overlaid equity curves — one coloured line per instrument.
            fig = go.Figure()
            palette = px.colors.qualitative.Dark24
            plotted = 0
            for idx, (sym, res) in enumerate(bulk_results.items()):
                eq = res.equity_curve
                if eq.empty:
                    continue
                y = ((eq / init_cap - 1) * 100).values if norm_bulk else eq.values
                fig.add_trace(go.Scatter(
                    x=eq.index, y=y, mode="lines", name=sym,
                    line=dict(color=palette[idx % len(palette)], width=2)))
                plotted += 1
            if plotted:
                fig.update_layout(
                    title=f"Bulk Equity Curves — {bt_strategy.name}",
                    height=480, xaxis_title="Time",
                    yaxis_title="Return (%)" if norm_bulk else "Equity (₹)",
                    legend_title="Instrument", hovermode="x unified")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No equity curves to plot — none of the selected "
                        "instruments generated data in this window.")

            st.subheader("📊 Comparison")
            st.caption("Sorted by Total Return — the top row performed best.")
            st.dataframe(summary, use_container_width=True, hide_index=True)

            if (summary["Data Source"] == "synthetic").any():
                st.warning(
                    "⚠️ Some instruments fell back to **synthetic** random-walk "
                    "data (real history unavailable for this range) — their "
                    "curves are not comparable to the real ones. Check the "
                    "**Data Source** column.")
