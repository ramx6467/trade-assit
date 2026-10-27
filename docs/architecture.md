# Architecture

The frontend is a React/TypeScript Vite application with hand-authored responsive CSS. `src/services/api.ts` centralizes typed HTTP access; the Vite development server proxies `/api` to FastAPI. `PriceChart` is a small adapter around [TradingView Lightweight Charts](https://tradingview.github.io/lightweight-charts/docs/series-types) v5 candlestick and line series. The backend AI integration uses the official [OpenAI Python SDK](https://github.com/openai/openai-python) Responses API.

The Python backend separates deterministic calculations (`app/analysis`), the synthetic provider (`app/market_data`), API validation/orchestration (`app/main.py`), and the simulated order surface. All providers pass candles through `market_data/normalize.py`, which emits timezone-aware UTC timestamps, instrument/timeframe/source identity, and warnings for gaps without filling them. SQLAlchemy persists paper orders and net positions; migrations add order brackets and a per-quote-currency demo ledger. The only configured market-data source is synthetic DEMO.

## Analysis rules

- EMA is seeded by an SMA of the first `period` closes, then updated with alpha `2/(period+1)`.
- RSI uses Wilder-smoothed average gains/losses after the initial simple-average window. A flat window scores 50; a positive gain with zero loss scores 100.
- ATR uses true range and Wilder smoothing, seeded by the initial mean. First true range is high minus low.
- Bollinger Bands use a simple moving average and population standard deviation over the rolling period; bands warm up after `period` closes.
- MACD is the difference between SMA-seeded fast and slow EMAs. The signal EMA starts at the first valid MACD point; the histogram is MACD minus signal. No value is emitted before warm-up.
- VWAP is typical price `(high + low + close)/3` weighted by exchange or tick volume. It resets when the caller's session ID changes. DEMO volume is unavailable, so DEMO VWAP is `null` and no synthetic volume is substituted. No exchange session calendar is configured.
- Bullish FVG: current low exceeds high two candles earlier. Bearish FVG: current high is below low two candles earlier. Zones include all source timestamps and a stable instrument/timeframe-scoped ID. Full mitigation is a later bar reaching the far boundary; a close beyond that boundary marks invalidation.
- Swing pivots compare a bar with two neighbors on each side; the result's confirmation timestamp is the second right-side bar. No unconfirmed pivot is emitted.
- BOS/CHoCH use a close beyond the most recent confirmed pivot, with one break per pivot; first/continuation breaks are BOS and opposite-direction breaks are CHoCH. A liquidity sweep is a wick through a confirmed pivot followed by a close back across it. Support/resistance clusters group confirmed pivots within a configurable percentage tolerance.
- An order-block candidate is the nearest opposing candle within five bars before a displacement candle with body at least 1.0 prior ATR. It becomes confirmed only when that candle also closes beyond a confirmed pivot in the displacement direction. The full opposing-candle range is the zone; a later close through its distal edge invalidates it. These configurable mechanical rules do not identify institutional intent.

These are explicit prototype rules. SMC terminology varies between practitioners and detections should be read as mechanical annotations, not institutional intent. Historical evaluation must truncate input candles to the intended as-of timestamp so future bars cannot affect a past result.

## Planned boundaries

Replace the demo market-data module behind a provider protocol before using external data. A future broker adapter must keep authentication server-side and must remain disconnected until verified. User identity and access controls should be added before saving user-specific account data. Live order execution is out of scope and disabled.

TradingView integration status distinguishes its website link from widget embeds and licensed charting libraries. [TradingView Widgets](https://www.tradingview.com/widget-docs/getting-started/) are embeddable components with TradingView-provided data; they do not create a personal-account session for this app. [Advanced Charts](https://www.tradingview.com/charting-library-docs/latest/quick-start/) requires access to TradingView's private repository, its library is non-redistributable, and the integrator supplies a datafeed. The app currently enables none of these embeds and only opens the public website.
