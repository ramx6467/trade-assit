# Timeframe, Provider Status, AI Context & Empty-Chart Fixes

Scope: five reported bugs across the market-data → chart → indicator → SMC →
analysis → AI-assistant flow. This document records the audited root causes and
the implemented, tested fixes.

## 1. Root causes (PHASE 1 audit)

Data flow traced end to end:

```
Twelve Data (REST/WS)
  -> app/market_data/providers.py  (TwelveDataProvider, MarketDataService)
  -> app/market_data/normalize.py  (normalize_candles)
  -> app/market_data/demo.py       (DEMO feed)
  -> app/main.py                   (_candles, _data_status, market_status, ai_chat)
  -> frontend/src/services/api.ts  (snapshot / candles / chat)
  -> frontend/src/App.tsx          (setMarket -> indicators/SMC/AI context)
  -> frontend/src/components/PriceChart.tsx
```

| Bug | Root cause |
|-----|-----------|
| 1. 3m empty chart | Twelve Data has no `3min` interval. `TwelveDataProvider.get_historical_candles` correctly rejected it, but nothing derived 3m, so the chart/indicators were empty. |
| 2. Provider "DISCONNECTED" | `MarketDataService.status()` hardcoded `connection_status: "DISCONNECTED"` and `main.market_status` passed it straight through. Credentials configured but streaming never verified was reported as disconnected. |
| 3. Stale AI answer after context change | `sendChat` appended the reply to the conversation without recording the instrument/timeframe it answered; a reply that arrived after the chart context changed was presented as current. |
| 4. Inconsistent AI context/status | Same as 3 plus no request-version guard: nothing marked an answer as belonging to a superseded context. |
| 5. Empty/misleading chart | On a failed timeframe request the chart simply said "Chart unavailable for this selection." with no symbol, timeframe, reason, or retry. |

## 2. Files changed

Backend
- `app/market_data/aggregate.py` (new) — deterministic OHLC aggregation.
- `app/market_data/providers.py` — `AGGREGATION_PLANS` + service wiring.
- `app/main.py` — `_last_market_success`, `_connection_status()`, honest
  `market_status` message, WS status + instrument rows use the computed status.
- `tests/test_aggregation.py` (new), `tests/test_market_providers.py` (added).

Frontend
- `src/App.tsx` — chat context labels + request-sequence guard; informative
  empty-chart panel with retry.
- `src/components/AIChatWorkspace.tsx` — `ChatMessage` context fields + label.
- `src/analysis.css` — `.message-context`, `.empty-chart-*` styles.
- `src/App.test.tsx` — updated empty-chart assertion; added context-staleness test.

## 3. Supported timeframes & unsupported handling

Twelve Data is asked only for intervals it documents: `1m, 5m, 15m, 30m, 1h,
4h, 1d, 1w`. The provider still **rejects** anything else
(`test_twelve_data_rejects_unsupported_interval_instead_of_guessing`) so it can
never silently substitute data.

3-minute candles are produced by explicit aggregation of real 1-minute candles
(`AGGREGATION_PLANS = {"TWELVE_DATA": {"3m": ("1m", 3)}}`). DEMO already serves
3m natively. No UI timeframe is left unsupported, so none must be disabled.

## 4. 3-minute aggregation (PHASE 3)

`aggregate_candles()` builds each target bucket only when it holds a complete,
contiguous set of source candles:

- Open = first source open; High = max; Low = min; Close = last close.
- Volume = sum **only** when every source candle has volume, else `null`.
- Incomplete buckets are dropped and reported; never padded with invented prices.
- Timestamps are UTC-normalized and aligned to interval boundaries, then
  re-validated by `normalize_candles` for the derived timeframe.

## 5. Connection status (PHASE 4)

`_connection_status(symbol)` is derived from real request outcomes:

1. no provider configured → `DEMO`
2. fresh WebSocket tick → `STREAMING`
3. recent provider error → its real status (`QUOTA_EXCEEDED` / `RATE_LIMITED` /
   `DISCONNECTED` / `ERROR`)
4. a successful REST fetch recorded → `CONNECTED`
5. configured but no request recorded yet → `CONFIGURED` (never `DISCONNECTED`)

Credentials alone never read as connected. A failing quote no longer marks the
provider disconnected when candles succeed, and stale cached candles are served
with an explicit `STALE:` warning instead of being presented as live.

## 6. Stale AI context (PHASE 6/8)

- Every user/assistant message records the `contextSymbol`/`contextTimeframe`
  it was produced for.
- Assistant messages display that context; when it differs from the active
  context the label reads "· previous context" so older analysis is never shown
  as current.
- A monotonic `chatSeq` guard drops a reply if a newer request superseded it.
- History sent to the backend is reduced to `{role, text}`.

## 7. Empty chart (PHASE 5)

The empty-chart surface now shows the symbol, the requested timeframe, the
actual reason (error or provider warning) and a **Retry** button.

## 8. Tests executed (actual results)

- Backend: `./.venv/bin/python -m pytest` → **198 passed** (was 189; +6
  aggregation, +3 provider/connection-status).
- Frontend: `npx vitest run` → **86 passed** (was 85; +1 context-staleness).
- Frontend: `npx tsc --noEmit` → clean; `npm run build` → success.

## 9. Remaining provider limitations

- Twelve Data REST credits were exhausted in this environment, so the live
  Twelve Data 3m path is covered by unit tests with mocked responses rather than
  a live request.
- Long-range 1m history still requires `start_date`/`end_date` pagination, and
  provider rate limits must keep being respected.

## 10. Manual steps

- None required to run the fixes; restart the backend so the new aggregation
  plan and status logic load.
