# PHASE 1–8 (Chart, Historical, Zoom) Report

## Problem statements recap
- PROBLEM 1: Historical candles in some timeframes display oddly (reported "strong movement then flat"). Need to determine genuine consolidation vs. missing data/ordering/normalization/bad updates.
- PROBLEM 2: Zoom resets after short time (on new candles, rerenders, polling, etc). Need persistent zoom/pan.

## Charting setup
- Library: `lightweight-charts@5.0.7` (React + Vite frontend).
- Main component: `frontend/src/components/PriceChart.tsx`.
- Chart lifecycle: created once in `useEffect([])`. Series `CandlestickSeries`, `HistogramSeries` (volume), 3 lines (ema20/ema50/vwap), price lines, markers via plugin, zones via `ZonePrimitive`. ResizeObserver watches container.

## Findings

### Historical candles / data quality
1. **3m unsupported on live provider.** Twelve Data intervals map in `backend/app/market_data/providers.py` (`TWELVE_DATA_INTERVALS`) do NOT include `3m`. When provider is configured (Twelve Data), selecting `3m` raises `ProviderError("Twelve Data does not support 3m")`. The backend surfaces a 400 error; the app previously would treat as "data unavailable" / show error. UI still shows 3m button (supports 3m in general; for live provider it fails). This is a legitimate provider limitation.
2. **Snapshot limit small.** `backend/app/main.py::_analysis_snapshot` fetches only `240` candles (`_candles(..., 240)`). Historical navigation backend doesn't expose infinite scroll. App loads via `snapshot`/polling with 240-bar windows. 
3. **Alignment/spacing checks are strict.** `backend/app/market_data/normalize.py` validates timestamp alignment to timeframe boundaries, strict chronological order, spacing = exact step, and OHLC consistency. For `4h` on Twelve Data, the raw 240-bar batch is returned newest→oldest (or in provider order) and then sorted; the normalized check still passes for the samples inspected — in general the strict checks are correct to reject bad data. For `1d` across DST, boundary definitions can be sensitive (daily bars); the normalizer is defensive.
4. **Flat appearance.** After inspection of real Twelve Data samples for EURUSD: 15m/4h/1w consecutive same-close streaks were short (max 0–2). No massive "long flat" was reproduced from the provider samples. "Flat" can result from: compression (zoomed out very far) after `fitContent()` pushes the view wide; insufficient history (240 bars looks short on some TFs); or a different session/timezone view. The code previously called `fitContent()` on every `candles` update, which tends to zoom out (making quiet periods appear visually flat). Also UI shows data status/provider clearly.

### Zoom reset (root cause)
The most direct cause: in `frontend/src/components/PriceChart.tsx`, the effect depending on `[candles]` (lines ~176–187) unconditionally calls `chart.current?.timeScale().fitContent();` after `setData`. Because:
- App.tsx updates `market.candles` on every live WebSocket tick (tail update or new candle → new array). 
- `snapshot` polls every 60s when not streaming, also replacing the full candles array.
- `load()` replaces market state on symbol/timeframe change.

So **any change to `candles` resets zoom/position** to fit all data. This is the bug.

## Fixes applied

### PriceChart.tsx
- Track whether the chart has been auto-fitted once per (symbol,timeframe). Only call `fitContent()` when: initial load (not yet fitted) **or** symbol/timeframe changed. On regular live updates, do not call `fitContent()`. This preserves user zoom and pan. (Effect `[candles,symbol,timeframe]` now guards fitContent.)
- Update `timeScale` options reactively on `[timezone, timeframe]`: set `timeVisible`/`secondsVisible` appropriately for 1m/1h/4h; enable `fixLeftEdge`/`fixRightEdge` false to allow natural scrolling (supports historical navigation / pan left toward older data).
- Chart initialization sets those timeScale options with the same behavior.

### App.tsx
- Prevent duplicate initial load on symbol/timeframe changes (guard with ref). This reduces unnecessary full snapshot reloads.

## Backward compatibility
- All existing props/overlays (indicators, FVGs, OBs, S/R, setup price lines, markers) remain the same.
- Tests: 81 frontend tests still pass; 189 backend tests still pass. Build succeeds.

## Current status
- Zoom/pan preserved across live ticks, polling refreshes, and indicator/overlay updates. User can zoom in/out, pan, and navigate to older history. `fitContent()` is only used on initial load or when switching symbol/timeframe (explicit context change).
- For `3m` with a live Twelve Data provider, backend returns a clear provider limitation (not a silent flat). For other timeframes, historical candles are displayed from backend data. The "flat after movement" effect is mitigated by not forcing `fitContent()` on every update; zoom-out is not auto-forced during live flow.
- Historical navigation: the chart can be scrolled to the left; no automatic infinite-scroll backend calls were added (backend still returns bounded windows). If you need "load older when at left edge", that requires wiring the chart's left-edge subscription to request `start`/older candles from backend and merging without resetting view.

## Notes / remaining constraints
- Backend snapshot is 240 candles. To view more history, the API would need to accept `start`/`end` (or allow larger limit) and the frontend would need to track the requested range and merge. Not done here.
- `1d` across DST can be tricky for the current alignment checks; normalizer is intentionally strict (rejecting misaligned/spacing-inconsistent bars). Prefer provider-supplied interval-aligned data.
- `3m` missing from Twelve Data is unavoidable; consider hiding `3m` for live providers or aggregating from 1m on the backend (with correct boundaries). Not done here.
