MODULE 1 — Single Candle Patterns

These are detected from only one candle.

Pattern	Bullish	Bearish	Strength	What it Means
Hammer	✅		High	Rejection of lower prices
Hanging Man		✅	Medium	Selling pressure appears
Shooting Star		✅	High	Buyers rejected
Inverted Hammer	✅		Medium	Possible reversal
Doji	Both	Both	Medium	Indecision
Dragonfly Doji	✅		High	Strong rejection
Gravestone Doji		✅	High	Strong upper rejection
Long-legged Doji	Both	Both	Medium	Volatility
Marubozu Bullish	✅		High	Strong momentum
Marubozu Bearish		✅	High	Strong selling
Spinning Top	Both	Both	Weak	Pause
MODULE 2 — Two Candle Patterns

These compare Candle A vs Candle B.

Examples:

Bullish Engulfing
Bearish Engulfing
Tweezer Top
Tweezer Bottom
Harami
Harami Cross
Piercing Pattern
Dark Cloud Cover
Matching Low
Matching High
Kicker
On Neck
In Neck
Thrusting Pattern

These require body size, wick size, relative positioning, and previous trend to be evaluated.

MODULE 3 — Three Candle Patterns

Examples include:

Morning Star

Evening Star

Three White Soldiers

Three Black Crows

Three Inside Up

Three Inside Down

Three Outside Up

Three Outside Down

Abandoned Baby

Tri-Star

Upside Gap Two Crows

Deliberation

Advance Block

Identical Three Crows

Stick Sandwich

Breakaway

Tasuki Gap

Mat Hold

Rising Three Methods

Falling Three Methods

Professionally these are among the strongest because they incorporate changing momentum over several bars.

MODULE 4 — Market Structure

This is far more important than candlestick patterns.

Detect:

Higher High (HH)

Higher Low (HL)

Lower High (LH)

Lower Low (LL)

Swing High

Swing Low

Internal Swing

External Swing

Break of Structure (BOS)

Market Structure Shift (MSS)

Change of Character (CHOCH)

Trend

Range

Compression

Expansion

Impulse

Correction

Without market structure, candlestick signals generate many false positives.

MODULE 5 — Chart Patterns

Continuation Patterns

Flag

Bull Flag

Bear Flag

Pennant

Rectangle

Ascending Triangle

Descending Triangle

Symmetrical Triangle

Channel

Rising Channel

Falling Channel

Cup

Handle

Reversal Patterns

Double Top

Double Bottom

Triple Top

Triple Bottom

Head and Shoulders

Inverse Head and Shoulders

Rounded Bottom

Rounded Top

Broadening Wedge

Diamond

Megaphone

Wedge Breakout

These require identifying pivots, trendlines, and breakout confirmation rather than a fixed number of candles.

MODULE 6 — Price Action Concepts

These are not classic "patterns" but are essential.

Inside Bar

Outside Bar

Mother Bar

NR4

NR7

Wide Range Bar

Pin Bar

Fakey

False Breakout

Liquidity Grab

Break and Retest

Support Flip

Resistance Flip

Compression

Expansion

Trend Exhaustion

Spring

Upthrust

These concepts are widely used because they capture order-flow behavior more directly than memorizing candlestick names.

MODULE 7 — Liquidity Detection

Institutional-style features:

Equal Highs

Equal Lows

Liquidity Sweep

Stop Hunt

Swing Failure Pattern

Breakout Failure

Trap

Bull Trap

Bear Trap

Gap Fill

Session High Break

Session Low Break

Previous Day High

Previous Day Low

Weekly High

Weekly Low

Monthly High

Monthly Low

MODULE 8 — Trend Detection

EMA Trend

SMA Trend

Price Action Trend

ADX Trend

Slope Detection

Regression Trend

Multi-Timeframe Trend

Trend Strength Score

Trend Exhaustion

Pullback Quality

MODULE 9 — Support & Resistance

Static Support

Static Resistance

Dynamic Support

Dynamic Resistance

Trendline

Parallel Channel

Supply Zone

Demand Zone

Order Block

Breaker Block

Mitigation Block

Fair Value Gap

Volume Gap

Liquidity Zone

Pivot Levels

VWAP Levels

Anchored VWAP

MODULE 10 — Volume & Volatility Context

Volume Spike

Volume Dry Up

ATR Expansion

ATR Compression

High Volatility

Low Volatility

Opening Range

Session Breakout

Gap Up

Gap Down

Volume Confirmation

Without context such as volume or volatility, many visually correct patterns fail.

Beyond Pattern Detection: Pattern Formation Prediction

This is the feature that would make your Pine Script stand out.

Instead of only saying:

"Head and Shoulders detected"

the script estimates what is currently forming.

For example:

Pattern Probability

Head & Shoulder
███████████ 78%

Double Top
███████ 52%

Ascending Triangle
█████ 35%

Bull Flag
██ 18%

This would require scoring multiple characteristics continuously, such as:

Swing geometry
Trend context
Volume behavior
Relative pivot distances
Time symmetry
Breakout pressure
Volatility contraction
Support/resistance proximity

The score would update every new candle.

Example output:

Current Formation

Bull Flag
Probability : 81%

Status

Pole Completed
Flag Developing
Breakout Pending
Estimated 3-6 candles remaining

This is much more useful than waiting for a pattern to finish.

Confidence Engine

Rather than assigning arbitrary percentages, the confidence score should be built from weighted evidence. For example:

Trend alignment: 20%
Market structure: 20%
Pattern geometry: 25%
Candle confirmation: 10%
Volume confirmation: 10%
Support/resistance context: 10%
Volatility conditions: 5%

The final confidence becomes a weighted score rather than a guessed number.

Suggested Development Roadmap

I recommend building this in phases instead of trying to code everything at once:

Phase 1: Candlestick engine (single, double, triple candle recognition)
Phase 2: Swing and market structure engine (HH, HL, LH, LL, BOS, CHOCH)
Phase 3: Chart pattern engine (flags, triangles, wedges, head & shoulders, double tops/bottoms)
Phase 4: Context engine (support/resistance, trend, volatility, volume)
Phase 5: Pattern prediction engine with continuously updating confidence scores
Phase 6: Smart visualization on TradingView (labels, zones, projected pattern outlines, confidence dashboard, alerts)