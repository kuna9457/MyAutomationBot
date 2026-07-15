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
import plotly.graph_objects as go
import streamlit as st

import config
from config import (Broker, Environment, Mode, Segment, ALL_INSTRUMENTS,
                    instruments_for_segment)
from db_manager import DBManager
from engine import TradingEngine
import backtester
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

mode_label = st.sidebar.radio(
    "Trading Mode", ["Intraday (15m)", "Swing (Daily)"], index=0)
mode = Mode.SWING if mode_label.startswith("Swing") else Mode.INTRADAY

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

symbols = st.sidebar.multiselect(
    "Instruments",
    [i.symbol for i in universe],
    default=[i.symbol for i in universe],
)
selected = [i for i in universe if i.symbol in symbols]

capital = st.sidebar.number_input(
    "Total Capital (₹)", min_value=10_000.0, value=float(config.TOTAL_CAPITAL),
    step=10_000.0)

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
        eng = TradingEngine(environment, mode, broker_choice, selected, capital)
        eng.start()
        st.session_state.engine = eng
        st.sidebar.success("Bot started.")

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
           f"|  MCX commodities trade until 23:30 IST")

tab_dash, tab_logs, tab_bt = st.tabs(
    ["🖥️ Live Dashboard", "📒 Trade Logs & Analytics", "🧪 Backtesting Engine"])


# --------------------------------------------------------------------------- #
#  Tab 1 — Live Dashboard
#
#  Auto-refresh is done with st.fragment(run_every=...), which reruns ONLY this
#  fragment on a timer. Crucially it does NOT reload the page, so st.session_state
#  (and the running engine handle) survive — a full-page meta-refresh would start
#  a new session, drop the engine, and make the bot look like it "stopped" after a
#  few seconds while leaking the old background thread. The engine is re-read from
#  session_state on every fragment rerun so it always reflects the latest state.
# --------------------------------------------------------------------------- #
# Poll every 2s only while the bot is running; otherwise no timer (idle = static).
_running = bool(st.session_state.engine and st.session_state.engine.state.running)


@st.fragment(run_every=2 if _running else None)
def render_dashboard() -> None:
    eng = st.session_state.engine
    if eng is None:
        st.info("Bot not started. Configure the sidebar and press **Start Bot**. "
                "It runs in Paper/Simulation mode out of the box — no broker "
                "tokens required.")
        return

    snap = eng.state.snapshot()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Feed", snap["feed_status"])
    c2.metric("Broker", snap["broker_name"])
    c3.metric("Storage", snap["db_backend"])
    c4.metric("Day PnL (₹)", f"{snap['day_pnl']:.2f}")

    st.subheader("📡 Detected Signals")
    if snap["last_signals"]:
        st.dataframe(pd.DataFrame(snap["last_signals"]),
                     use_container_width=True, hide_index=True)
    else:
        st.write("No signals yet — waiting for strategy conditions to align.")

    st.subheader("📌 Open Positions")
    if snap["open_positions"]:
        rows = []
        for sym, t in snap["open_positions"].items():
            lp = t.get("_live_price", t["entry_price"])
            direction = 1 if t["side"] == "BUY" else -1
            upnl = (lp - t["entry_price"]) * t["quantity"] * direction
            rows.append({
                "Symbol": sym, "Side": t["side"], "Qty": t["quantity"],
                "Entry": round(t["entry_price"], 2), "LTP": round(lp, 2),
                "SL": round(t["stop_loss"], 2), "Target": round(t["target"], 2),
                "Unreal PnL": round(upnl, 2),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True,
                     hide_index=True)
    else:
        st.write("No open positions.")

    st.subheader("📝 Activity Log")
    st.code("\n".join(snap["log"][:40]) or "—", language="text")

    st.caption("🔄 Live — refreshing every 2s" if snap["running"]
               else "⏸️ Bot stopped.")


with tab_dash:
    render_dashboard()


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


# --------------------------------------------------------------------------- #
#  Tab 3 — Backtesting Engine
# --------------------------------------------------------------------------- #
with tab_bt:
    bc1, bc2, bc3 = st.columns(3)
    ticker = bc1.selectbox("Ticker", [i.symbol for i in ALL_INSTRUMENTS])
    bt_mode = bc2.selectbox("Mode", ["Swing", "Intraday"])
    init_cap = bc3.number_input("Initial Capital (₹)", value=100_000.0,
                                step=10_000.0)
    d1, d2 = st.columns(2)
    start_d = d1.date_input("Start", value=date.today() - timedelta(days=365))
    end_d = d2.date_input("End", value=date.today())

    if st.button("🚀 Run Backtest", type="primary"):
        inst = config.INSTRUMENTS_BY_SYMBOL[ticker]
        with st.spinner("Running backtest..."):
            result = backtester.run_backtest(
                ticker, str(start_d), str(end_d), init_cap,
                Mode.SWING if bt_mode == "Swing" else Mode.INTRADAY,
                lot_size=inst.lot_size,
            )
        m = result.metrics
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
