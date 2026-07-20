# Claude Code Prompt: WebSocket Architecture & 1-Min Scalper Strategy Integration

**Context:**
You are an expert algorithmic trading developer. We are building a Streamlit-based automated trading bot in Python. We need to upgrade the existing architecture to fix previous gaps, shift entirely to WebSocket for live data, and introduce a new aggressive 1-minute scalping mode.

**Please implement the following updates to the codebase:**

## 1. WebSocket-First Architecture for Live Data
- **Live Feed & PnL:** Transition the live trading architecture so that *all* live feed data (price updates, ticks) and live PnL calculations are driven exclusively via WebSocket. Do not rely on REST API polling for live state.
- **Single Source of Truth:** Anything related to "live" status (current market price, live PnL of open positions) must strictly come from the WebSocket connection.
- **UI Integration:** Ensure the Streamlit front-end can reactively display these WebSocket updates without manual page refreshes (consider using Streamlit auto-refresh components or asynchronous background threads that update a shared state dictionary).

## 2. Dashboard UI & Logging Enhancements
- **Readable Logs:** Create dedicated, highly readable Streamlit UI components/tabs to view:
  - **Activity Log:** Real-time system activities (e.g., WebSocket connected, signal generated, order sent).
  - **Trade Log:** Table showing historical and recent trade executions.
  - **Holding Positions:** A clean view of currently open trades/holdings.
- **Live PnL Display:** Prominently display the Live PnL in the Streamlit UI, continuously updated by the WebSocket feed.

## 3. "Aggressive Scalper" Mode (1-Minute Timeframe)
- **New Trading Mode:** Introduce a new "Aggressive Scalper" mode operating strictly on a **1-minute timeframe**.
- **Signal & Execution:** The system must actively check 1-minute candles to see if a trade signal is generated and subsequently punch the trade/order to the broker. 

## 4. Scalping Strategy Implementation (VWAP-ATR)
Implement the strategy logic exactly as defined below (from our `scalping.md`):

**Core Logic:**
- **Timeframe:** 1-minute (M1).
- **Trend Identification (VWAP):** 
  - Bullish: Price trading consistently above 1-min VWAP.
  - Bearish: Price trading consistently below 1-min VWAP.
- **Entry Triggers:**
  - *Long Entry:* Price is above VWAP -> Pull-back touches/dips slightly below VWAP -> Bullish reversal candle closes with its high exceeding the previous candle's high (momentum breakout).
  - *Short Entry:* Price is below VWAP -> Pull-back touches/rises slightly above VWAP -> Bearish reversal candle closes with its low falling below the previous candle's low.
- **Risk Management (ATR):**
  - Calculate 7-period ATR at the exact moment of entry.
  - *Stop-Loss (SL):* 1.0 × ATR from the entry price.
  - *Take-Profit (TP):* 1.0 × ATR from the entry price (1:1 RR ratio).
  - *Position Sizing:* Risk_Amount (Total Capital per trade) / (ATR_Value * Contract_Multiplier). Cash risk per trade must remain constant.
- **Trade Execution Rules:**
  - *Time Filter:* Ignore the first 15 minutes of the trading session (09:15–09:30 IST).
  - *Time Exit:* If 1:1 TP or SL is not hit within 7 minutes of entry, close the trade at the market price.
  - *Slippage Management:* Use limit orders slightly inside the spread to enter.

**Instructions for Claude:**
1. Review the existing `app.py`, `strategy.py`, and `broker_api.py` files.
2. Update the system architecture to handle the WebSocket requirements robustly.
3. Add the UI components for logging and positions.
4. Implement the "Aggressive Scalper" strategy in the strategy engine and hook it up to the 1-minute data pipeline.
5. Provide the complete updated Python code files.
