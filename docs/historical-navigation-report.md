# Historical Navigation & Data Range Report

## Root causes
- `api.snapshot()` is the primary load path and `_analysis_snapshot` in backend always fetches **240** candles (hardcoded). So the frontend displays only a recent 240-bar window.
- Twelve Data `/time_series` supports `outputsize` (1..5000) and also `start_date/end_date`. The current code uses `outputsize` when no range is given (or defaults to 240). Provider can return much more, but we don't ask for more.
- No pagination/lazy loading: when the user scrolls left, the frontend/chart never requests older candles and the backend has no endpoint that accepts a start boundary in a way the UI exploits.
- `PriceChart.tsx` was changed earlier to stop calling `fitContent()` on every `candles` update (good). But no left-edge detection is implemented to prepend older history. 
- In App.tsx, live updates only mutate the tail (append new candle/update current) and `snapshot()` replaces the whole dataset when called; the 60s poll also replaces with the latest snapshot (240 candles) — so history older than the window could be lost if not preserved. But we need to grow the history.

## What exists
- Backend: `/api/v1/market-data/candles` accepts `limit`, `start`, `end`. `snapshot`/other endpoints hardcode 240. Twelve Data provider already passes through `start`/`end` and allows `limit` up to 5000; it caches by `(symbol,timeframe,limit,start,end)`.
- Frontend: API has `candles()` and `snapshot()`. PriceChart uses `lightweight-charts@5.0.7` (timeScale can be manipulated; left edge events are possible). No left-edge handler exists.

## Recommended minimal fix (preserves zoom behavior)
1. **Backend**: allow endpoints that need more history to request larger limits. Change `_analysis_snapshot` and calls that need chart history to use a larger limit (e.g. 1000) or make it configurable. Also keep `market-data/candles` flexible (limit already up to 1000 in API, provider up to 5000). 
2. **Frontend API**: extend `candles()` to accept `limit`, `start`, `end`. Extend `snapshot()` similarly or add a separate `history` call. 
3. **Chart pagination**: add a `timeScale().subscribeVisibleTimeRangeChange` handler to detect when the visible range reaches near the oldest loaded time; if so, request candles older than the oldest (using `start` < oldest timestamp) with a reasonable limit, then merge: combine, deduplicate by timestamp, sort chronologically, call `series.setData` with merged array, and use `timeScale().scrollToPosition()`/preserve position (to avoid jumping). Don't call `fitContent()`. 
4. **Live updates**: continue to update tail only (append/update current candle) when streaming; don't replace entire history on every poll unless necessary. 
5. **Diagnostics**: log symbol/timeframe/requested range/count/earliest/latest (no keys).

## Notes on provider limits
- Twelve Data allows up to 5000 candles per request for `time_series` (outputsize). For long windows (e.g. 1m) 5000 is ~3.5 days; still finite. The app must paginate across ranges (using `start_date`/`end_date`) to get full history if needed. 
- Some timeframes (3m) not supported by Twelve Data — true provider limitation (document it). Others vary. 
- Rate limits exist; must avoid duplicate requests and backoff on 429/QUOTA_EXCEEDED.

This is the smallest reliable path to view previous candles without breaking zoom.

## Status (implemented)

- Backend snapshot history raised to `CHART_HISTORY_LIMIT = 1000`;
  `/market-data/candles` keeps its 240 default and `/market-data/history`
  accepts `start`/`end`.
- Frontend `api.candleHistory()` added; `App.tsx` merges/dedupes/sorts on
  left-edge scroll, preserves the visible range on prepend, stops when history is
  exhausted, and exposes a return-to-latest control.
- See `docs/timeframe-status-ai-fixes.md` for the follow-up fixes (3m
  aggregation, provider connection status, AI context, empty-chart panel).
