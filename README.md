# 📈 Automated Trading Bot (NSE Equity + MCX Commodities)

A dual-mode (Intraday / Swing) algorithmic trading bot with a Streamlit UI,
Upstox Sandbox paper trading, live Upstox / Dhan / Kotak Neo execution, MongoDB
trade logging, an Excel analytics export, and a vectorized backtesting engine.

**MCX commodities — GOLD, CRUDEOIL, NATURALGAS (+ SILVER) — are fully wired in.**
They flow through the same feed → strategy → broker → DB pipeline as equities,
with one difference the engine respects: **MCX trades until 23:30 IST**, so the
bot keeps hunting and executing opportunities into the night while equity stops
at 15:30.

> ✅ **Runs today with zero broker tokens.** With an empty `.env` it uses a
> simulated live feed, a simulated broker, and local-JSON storage, so you can
> click *Start Bot* and watch signals fire immediately. Fill in `.env` and it
> upgrades to the real WebSocket feed, real brokers, and MongoDB automatically.

---

## 1. Setup

```bash
# from the project folder
pip install -r requirements.txt          # core deps (already tested on py3.11)
copy .env.example .env                    # then edit .env with your tokens
streamlit run app.py
```

Open the URL Streamlit prints (default http://localhost:8501).

Optional extras (only if you use them):
```bash
pip install upstox-python-sdk    # real Upstox sandbox/live + WebSocket V3 feed
pip install dhanhq               # Dhan live
pip install neo-api-client       # Kotak Neo live
pip install yfinance             # real historical data in the backtester
```
MongoDB is optional — install it locally (or point `MONGO_URI` at Atlas) to use
`paper_trades` / `live_trades` collections; otherwise trades are saved to
`./data/*.jsonl`.

---

## 2. Using the UI

**Sidebar**
- **Environment** — *Paper Trading (Sandbox)* vs *Live Trading*
- **Trading Mode** — *Intraday (15m)* vs *Swing (Daily)*
- **Live Broker** — Upstox / Dhan / Kotak Neo (only shown in Live)
- **Segments / Instruments** — pick NSE Equity and/or MCX Commodity names
- **Total Capital**, **Start Bot**, **Stop Bot**

**Tabs**
1. **Live Dashboard** — feed/broker/storage status, detected signals, open
   positions with live unrealized PnL, day PnL, activity log (auto-refreshes).
2. **Trade Logs & Analytics** — history from `paper_trades` / `live_trades`,
   win-rate/PnL summary, **Export High-Level Analysis to Excel**.
3. **Backtesting Engine** — pick ticker/mode/dates/capital → Total Return, Max
   Drawdown, Sharpe, Calmar, Win Rate + equity-curve chart.

---

## 3. Strategies (from the plan)

| | Intraday (15m) | Swing (Daily) |
|---|---|---|
| Risk / trade | **≤ 2%** | **≤ 3%** |
| Risk : Reward | **1 : 2** | **1 : 3** |
| Entry | Price > VWAP **and** MACD(12,26,9) bullish cross | Close > 200 EMA **and** RSI breaks 50 **and** Bollinger reclaim/bounce **and** volume > 20-SMA |
| Stop | ATR-based | ATR-based |

Position size = `(capital × risk%) / (entry − stop)`, rounded **down to whole
lots** for commodities (GOLD lot 100, CRUDEOIL 100, NATURALGAS 1250, SILVER 30).
The realized risk is recomputed from the rounded quantity so it never overstates
— verified in testing (₹500k @ 2% → ₹9,994 risk on a trade).

---

## 4. Architecture (decoupled by design)

```
config.py     instruments (equity + MCX), market hours, strategy params, env
strategy.py   indicators + entry signals + position sizing  (no broker/db imports)
data_feed.py  SimulatedFeed + Upstox WebSocket V3 feed       (one interface)
broker_api.py Simulated / Upstox(sandbox+live) / Dhan / Kotak (one interface)
db_manager.py MongoDB CRUD (paper_trades|live_trades) + Excel + JSON fallback
backtester.py vectorized engine: Return, MaxDD, Sharpe, Calmar, Win Rate
engine.py     threaded orchestrator: feed → strategy → broker → db, start/stop
app.py        Streamlit UI (sidebar + 3 tabs)
```

Strategy logic never imports a broker or the database, so switching Upstox →
Dhan → Kotak, or adding instruments, never touches `strategy.py`. Paper and Live
trades are written to **separate collections** and can never cross-contaminate.

---

## 5. Going live (important)

1. Get tokens and put them in `.env`.
2. **MCX instrument keys are placeholders** (`MCX_FO|GOLD_FUT` etc.). Before
   live commodity trading, replace them in `config.py > MCX_INSTRUMENTS` with the
   current-expiry contract tokens from your broker's instrument master — these
   change every expiry.
3. Test thoroughly in Paper/Sandbox first. Live mode places **real orders**.

> This software is for educational/personal automation. Trading carries risk;
> you are responsible for every order it places. Validate on paper before risking
> capital.
