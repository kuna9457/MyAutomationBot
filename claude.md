# Development Plan: Automated Trading Bot with Streamlit UI

## 1. System Architecture overview
Develop a fully automated algorithmic trading system in Python with a Streamlit front-end for UI control. The system must support two trading modes (Intraday and Swing), two execution environments (Paper/Sandbox via Upstox and Live via Upstox/Kotak Neo/Dhan), and encompass a Backtesting engine.

### Tech Stack
*   **Backend / Logic:** Python 3.10+
*   **UI:** Streamlit
*   **Database:** MongoDB (Trade Logging)
*   **Data Export:** Pandas / Openpyxl (Excel Reporting)
*   **Brokers:** Upstox (WebSocket V3 + Sandbox `sandbox=True`), `dhanhq` (Dhan API), Kotak Neo API.
*   **Environment Config:** `python-dotenv` (.env file)
*   **Technical Indicators:** `pandas-ta` or `TA-Lib`

---

## 2. Streamlit UI Layout
The Streamlit app should be organized using a Sidebar for global controls and Tabs for main views.

### Sidebar Controls
*   **Environment Toggle:** Radio button -> `Paper Trading (Sandbox)` vs. `Live Trading`.
*   **Trading Mode:** Radio button -> `Intraday (15m)` vs. `Swing (Daily)`.
*   **Broker Selection:** Dropdown -> `Upstox`, `Kotak Neo`, `Dhan` (Only active if 'Live Trading' is selected).
*   **Global Actions:** "Start Bot" and "Stop Bot" buttons.

### Main View Tabs
1.  **Live Dashboard:** 
    *   Displays WebSocket connection status.
    *   Shows real-time detected signals based on the active strategy.
    *   Displays current open positions and today's PnL.
2.  **Trade Logs & Analytics:**
    *   Fetches trade history from MongoDB.
    *   Displays a data table of all historical trades (Entry, Exit, PnL, RR, Mode).
    *   Button: "Export High-Level Analysis to Excel".
3.  **Backtesting Engine:**
    *   Inputs: Ticker symbol, Date Range, Initial Capital.
    *   Output Metrics: Total Return, Max Drawdown, Sharpe Ratio, Calmar Ratio, Win Rate.
    *   Visuals: Equity curve chart.

---

## 3. Trading Strategies & Indicators

### Mode 1: Intraday (15-Minute Timeframe)
*   **Goal:** Frequent trade generation.
*   **Risk/Reward (RR):** 1:2.
*   **Risk per Trade:** Maximum 2% of total capital.
*   **Indicators:**
    1.  **VWAP (Volume Weighted Average Price):** Acts as the baseline trend filter (Buy above VWAP, Sell below).
    2.  **MACD (12, 26, 9):** Acts as the trigger.
    *   *Entry Logic (Long):* Price crosses above VWAP + MACD line crosses above Signal line.
*   **Position Sizing Formula:** 
    $$Position\ Size = \frac{Total\ Capital \times 0.02}{Entry\ Price - Stop\ Loss\ Price}$$

### Mode 2: Swing Trading (Daily Timeframe)
*   **Goal:** Capturing larger multi-day/week trends.
*   **Risk/Reward (RR):** 1:3.
*   **Risk per Trade:** Maximum 3% of total capital.
*   **Indicators (4 Categories):**
    1.  **Trend:** 200 EMA (Price must be above 200 EMA for Longs).
    2.  **Momentum:** RSI (Relative Strength Index) - Look for breakouts above 50.
    3.  **Volatility:** Bollinger Bands (Entry triggered on bouncing off the lower band or breaking the middle band).
    4.  **Volume:** Volume SMA (20) - Entry candle volume must be strictly > 20-period Volume SMA.

---

## 4. Execution & Broker Integration

### Paper Trading (Sandbox)
*   **Broker:** Upstox Sandbox Environment.
*   **Implementation:** Initialize Upstox client with `sandbox=True`. Ensure the `SANDBOX_ACCESS_TOKEN` is loaded from `.env`.
*   **Logging:** All executed paper trades must be pushed to a MongoDB collection named `paper_trades`.

### Live Trading
*   Switch logic routing based on Sidebar dropdown:
    *   **Upstox:** Use official Upstox Python SDK (`upstox-python-sdk`).
    *   **Dhan:** Use official `dhanhq` library.
    *   **Kotak Neo:** Use Kotak Neo REST API client.
*   **Logging:** Live trades pushed to a MongoDB collection named `live_trades`.

---

## 5. MongoDB Schema Definition
Create a generic schema for trades:
```json
{
  "trade_id": "UUID",
  "timestamp": "ISO Date",
  "mode": "Intraday/Swing",
  "environment": "Paper/Live",
  "broker": "Upstox/Dhan/Kotak",
  "ticker": "String",
  "side": "BUY/SELL",
  "entry_price": Float,
  "stop_loss": Float,
  "target": Float,
  "quantity": Integer,
  "risk_amount": Float,
  "status": "OPEN/CLOSED",
  "exit_price": Float,
  "realized_pnl": Float
}
6. Execution Steps for the AIGenerate app.py (Streamlit UI layout).Generate strategy.py (Intraday and Swing indicator logic using pandas-ta).Generate broker_api.py (Wrapper for Upstox Sandbox, Live Upstox, Dhan, Kotak).Generate db_manager.py (MongoDB CRUD and Excel export using pandas).Generate backtester.py (Vectorized backtesting engine calculating Sharpe, Calmar, etc.).
---

### 2. System Memory File (`memory.md`)
Keep this file in your project directory. Whenever you start a new chat with an AI assistant to tweak or upgrade the bot, upload this file first. It acts as the bot's "brain" so the AI doesn't forget your architecture or break existing rules.

```markdown
# System Memory & State: Trading Bot

## Project State
*   **Current Phase:** Initial MVP generation.
*   **Core Goal:** A dual-mode (Intraday/Swing) algorithmic trading bot with Streamlit UI, Upstox Sandbox for paper trading, and real broker integrations for live execution.

## Immutable Rules
1.  **Risk Management is Strict:** 
    *   Intraday: NEVER exceed 2% risk per trade. Hard 1:2 RR.
    *   Swing: NEVER exceed 3% risk per trade. Hard 1:3 RR.
2.  **Environment Separation:** Sandbox/Paper trades and Live trades MUST go to separate MongoDB collections (`paper_trades` vs `live_trades`). Never cross-contaminate data.
3.  **Modular Codebase:** Strategy logic must remain entirely decoupled from Broker execution logic. This ensures that if we change from Upstox to Dhan, the strategy logic (`strategy.py`) does not need to be rewritten.
4.  **Sandbox API:** Upstox Sandbox must be initialized using `upstox_client.Configuration(sandbox=True)`.

## Future Tweaks (Do not implement yet, but keep architecture open)
*   Integrating machine learning/AI price predictions over the current standard indicators.
*   Adding Webhooks for TradingView integration.
*   Dynamic Risk-Reward trailing stops based on ATR.

## Changelog
*   [Date] - Initial project architecture and logic defined via Claude plan.
3. Setup & Environment GuideBefore having the AI generate the code, make sure your local environment is ready for it.Step 1: Install DependenciesOpen your terminal and run:Bashpip install streamlit pandas numpy pandas-ta pymongo openpyxl python-dotenv upstox-python-sdk dhanhq
Step 2: Create the .env FileCreate a file named .env in your project folder. The AI's code will look for this file to authenticate your brokers securely. Do not share this file with anyone.Code snippet# MongoDB
MONGO_URI="mongodb://localhost:27017/"

# Upstox (Paper & Live)
UPSTOX_SANDBOX_TOKEN="your_sandbox_token_here"
UPSTOX_LIVE_API_KEY="your_live_api_key_here"
UPSTOX_LIVE_SECRET="your_live_secret_here"

# Dhan
DHAN_CLIENT_ID="your_dhan_client_id"
DHAN_ACCESS_TOKEN="your_dhan_access_token"

# Kotak Neo
KOTAK_NEO_CONSUMER_KEY="your_consumer_key"
KOTAK_NEO_CONSUMER_SECRET="your_consumer_secret"
KOTAK_NEO_ACCESS_TOKEN="your_access_token"
Step 3: Setup Upstox SandboxTo get your Sandbox token:Go to the Upstox Developer portal.Click New Sandbox App and fill in the details.  Click Generate to get your Sandbox Access Token (Valid for 30 days). Paste this into your .env file.  Once you pass development_plan.md to Claude, it will generate the Python files. You will run the system simply by typing streamlit run app.py in your terminal.