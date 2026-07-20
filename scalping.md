# VWAP-ATR 1-Minute Scalping Strategy

## Overview
This is a high-frequency, trend-following scalping strategy designed for highly liquid Indian market instruments (Nifty/Bank Nifty Futures or heavy-volume stocks). It utilizes VWAP to determine the prevailing trend and ATR to dynamically adjust stop-losses and position sizes, maintaining a strict 1:1 Risk-Reward (RR) ratio.

## Core Logic

### 1. Market Environment Filter
*   **Timeframe:** 1-minute (M1).
*   **Trend Identification:**
    *   **Bullish Context:** Price action is consistently trading above the 1-minute VWAP.
    *   **Bearish Context:** Price action is consistently trading below the 1-minute VWAP.
*   **Volatility Check:** To ensure the ATR is reliable, the algorithm must verify the ATR value is within a defined 'normal' range for that instrument to avoid entering during extreme, erratic volatility spikes.

### 2. Entry Triggers
*   **Long Entry:**
    1.  Price is above VWAP.
    2.  A pull-back occurs where the price touches or dips slightly below the VWAP line.
    3.  A bullish reversal candle closes, with its high exceeding the high of the previous candle (a "momentum breakout" from the pullback).
*   **Short Entry:**
    1.  Price is below VWAP.
    2.  A pull-back occurs where the price touches or rises slightly above the VWAP line.
    3.  A bearish reversal candle closes, with its low falling below the low of the previous candle.

### 3. Risk Management & Position Sizing
*   **Dynamic Stop-Loss (SL):** 
    *   Set at exactly `1.0 × ATR` (7-period) from the entry price. 
    *   The ATR value is calculated at the exact moment the entry signal is confirmed.
*   **Take-Profit (TP):** 
    *   Set at exactly `1.0 × ATR` (7-period) from the entry price, creating a 1:1 RR ratio.
*   **Position Sizing Calculation:** 
    *   `Risk_Amount` = (Total Capital per trade)
    *   `Quantity` = `Risk_Amount` / `(ATR_Value * Contract_Multiplier)`
    *   *This ensures that regardless of volatility, the actual cash risk per trade remains constant.*

### 4. Trade Execution Rules
*   **Time Filter:** Ignore the first 15 minutes of the trading session (09:15–09:30 IST) to allow initial market volatility and price discovery to settle.
*   **Time Exit:** To avoid stagnation in ranging markets, if the 1:1 TP or SL is not hit within 7 minutes of entry, the trade is closed at the market price.
*   **Slippage Management:** Use limit orders slightly inside the spread to enter, ensuring that the target/stop execution accounts for common brokerage and tax costs in the Indian market.

## Strategy Summary Table

| Parameter | Setting |
| :--- | :--- |
| **Strategy Type** | Trend-Following / Mean Reversion Pullback |
| **Indicator** | VWAP + 7-period ATR |
| **RR Ratio** | 1:1 |
| **Position Sizing** | Volatility-adjusted (ATR based) |
| **Max Trade Duration** | 7 minutes |
| **Primary Goal** | High-frequency, consistent small-win extraction |