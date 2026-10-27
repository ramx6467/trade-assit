# TradeMind AI — Twelve Data API Consumption Audit & Fixes

Scope: reduce unnecessary Twelve Data API consumption while preserving every
existing feature. This document records the audit, the exact changes, and the
results. It does **not** claim that any provider credits were restored — code
cannot do that.

---

## 1. Why credits were being consumed

The frontend never calls Twelve Data directly; it only calls the FastAPI
backend (`/api/v1/...`). Every provider request therefore originates in the
backend, but the backend was being asked to make requests far more often than
the data changed, and the same data was fetched through several endpoints.

The dominant consumers were **automatic, repeating requests**:

| Source | Trigger | Provider call | Rough volume |
| --- | --- | --- | --- |
| `/market-data/status` | frontend poll every **10 s** | Twelve Data `/price` | ~4–6/min → ~5,000–8,600/day per open tab |
| `/analysis/snapshot` | frontend poll every **60 s** when not streaming | Twelve Data `/time_series` | ~1,440/day |
| `/market-data/instruments` | Markets page open | one `/price` **per instrument** | 3 × per visit (Twelve Data symbols) |
| `/market/session` | symbol change | Twelve Data `/price` | 1 per symbol change |
| duplicate candle fetches | same symbol/timeframe at depth 240 and 1000 | 2 × `/time_series` | per mismatch |

A single open dashboard tab could therefore exceed Twelve Data's free-tier
budget (commonly 8 requests/minute and **800/day**) within roughly two to three
hours of idling. The app simply did not treat `/price` and `/time_series` as
scarce, metered credits.

Root causes:
1. `/market-data/status` fetched a live quote on every call, purely to render a
   price that was already derivable from cached data / the chart.
2. The frontend polled status (every 10 s) and the snapshot (every 60 s) with no
   regard for the provider's daily budget, and kept polling while the tab was
   hidden.
3. The provider candle cache key included the requested `limit`, so identical
   symbol/timeframe data requested at different depths caused separate calls.
4. Overview endpoints fetched a live quote **per instrument** / per symbol.
5. There was no request deduplication for simultaneous identical calls.

---

## 2. Every duplicate or unnecessary request found

1. **Status poll → live quote.** `/market-data/status` called
   `market_data.get_latest_quote()` on every request. Polled every 10 s. The
   quote cache TTL was 15 s, so ~1 in 1.5 polls still hit the provider.
2. **Snapshot poll → candles.** `api.snapshot` every 60 s (when not streaming),
   each triggering a `/time_series` call once the 60 s candle TTL expired.
3. **Per-instrument quotes.** `_instrument_rows()` fetched a live quote for
   every instrument (3 for Twelve Data) whenever `/market-data/instruments` was
   opened.
4. **Session quote.** `/market/session` fetched a live quote on each symbol
   change although it only needed availability.
5. **Limit-split duplicate candles.** The provider cache key was
   `(symbol, timeframe, limit, start, end)`, so `/analysis/snapshot` (limit
   1000), `/indicators` + `/analysis/*` + `/ai/chat` (limit 240) each fetched
   the same candles separately.
6. **3 m source fetch vs. 1 m cache.** The 3 m path requested 1 m data at
   `limit × 3`; because the key included `limit`, it never reused a plain 1 m
   fetch for the same symbol.
7. **No deduplication.** Two concurrent identical requests (e.g. snapshot on
   mount plus WS bucket rollover) each spent a credit.
8. **Hidden-tab polling.** Both polls ran while the tab was backgrounded.

Checked and found **not** to be problems:
- The frontend REST layer (`src/services/api.ts`) performs **no** automatic
  retries, so there is no 429 retry storm from normal requests.
- There is **one** WebSocket connection per tab (not one per rerender); the
  reconnect backoff is bounded to 30 s, and a failed stream keeps the socket
  open rather than reconnecting in a loop.
- No backend startup task, background scheduler, or `asyncio` poll loop issues
  provider requests.
- Indicator toggles, drawing toggles, and zoom/pan do **not** trigger any
  fetch — they are pure client-side chart state.
- StrictMode's double effect invocation does not double the initial candle
  fetch (guarded by a ref) and does not double the poll intervals (cleared on
  cleanup).

---

## 3. Files changed

Backend:
- `backend/app/market_data/providers.py`
  - New `TwelveDataProvider.get_cached_quote()` (cache-only, no network).
  - New `MarketDataService.get_cached_quote()`.
  - Candle cache key no longer includes `limit`; a cached set is reused when it
    already covers the requested depth, otherwise refreshed.
  - Per-key in-flight locks: simultaneous identical requests share one call.
  - `CANDLE_CACHE_TTL` 60 s → **120 s**; `QUOTE_CACHE_TTL` 15 s → **30 s**.
- `backend/app/main.py`
  - New `_status_quote(symbol, configured)`: configured providers are read from
    the quote cache only; DEMO stays free.
  - `market_status()` no longer fetches a live quote (and no longer marks
    success from a status view).
  - `_instrument_rows()` no longer fetches per-instrument quotes for configured
    providers.
  - `market_session_endpoint()` uses the cache-only quote.
  - `_serve_cached()` returns the **newest** cached candles (`[-limit:]`), not
    the oldest.
- `backend/tests/conftest.py`: autouse fixture clearing the class-level provider
  caches between tests (deterministic, no cross-test leakage).
- `backend/tests/test_request_budget.py` (new): 5 tests locking in cache
  sharing, deduplication, credit-free status/instruments, and newest-candle
  stale fallback.

Frontend:
- `frontend/src/App.tsx`
  - Status poll skips while `document.hidden`.
  - Snapshot fallback poll 60 s → **120 s** and skips while `document.hidden`.
  - WebSocket price events also update `marketStatus.price`.
- `frontend/src/App.test.tsx` (new test): status polling pauses while the tab is
  hidden and resumes when visible.

---

## 4. Cache strategy

- **Historical candles** are cached in `TwelveDataProvider` by
  `(symbol, timeframe, start, end)` — deliberately **excluding `limit`** — for
  120 s. A request is served from cache when the cached set already covers the
  requested depth (`len(cached) >= limit`, newest rows), otherwise it refreshes.
  A ranged request (`start` **and** `end`) is served in full.
- Because the key excludes `limit`, one provider call now serves the chart
  snapshot (depth 1000), indicators, analysis endpoints, and the AI context
  (depth 240) for the same symbol/timeframe.
- **Quotes** are cached per symbol for 30 s. Quotes are never treated as proof
  that the feed is live, and status views read the cache without refreshing it.
- **Frontend** keeps a merged candle buffer and de-duplicates by timestamp when
  paginating; it does not re-fetch historical candles on zoom, pan, rerender, or
  overlay toggles.
- Stale-cache fallback on provider failure serves the **most recent** valid
  candles and labels them `STALE:` with the age.

## 5. Request scheduling strategy

- One authoritative backend market-data service (`MarketDataService`) mediates
  all provider access; the browser never calls Twelve Data.
- Per-key in-flight locks deduplicate concurrent identical candle requests to a
  single credit.
- The frontend uses a single WebSocket per tab for live prices, a 120 s snapshot
  fallback poll (only when configured and not streaming), and a 10 s status poll
  that is now credit-free.
- Obsolete requests are already cancelled by the `loadSequence` guard
  (symbol/timeframe changes discard in-flight snapshot results), and pagination
  requests are guarded by `loadingOlderRef`.

## 6. Rate-limit behaviour

- Provider errors are classified (`QUOTA_EXCEEDED` vs `RATE_LIMITED`,
  authentication, subscription) from the real HTTP status and structured body.
- On failure, the last valid candles are preserved and labelled; the backend does
  **not** retry a non-retryable quota error.
- The frontend REST layer performs no automatic retries, so a 429 never
  triggers a retry loop.
- Status no longer spends credits, so even a transient limit cannot be worsened
  by the status poll.

## 7. WebSocket changes

- No new connections were added. There is exactly one WebSocket per tab, opened
  only when a provider is configured, closed on cleanup, and reconnected with a
  bounded (≤30 s) backoff.
- The frontend now also reflects the streamed price in `marketStatus.price`.
- The backend holds a failed stream socket open rather than forcing the client
  into a reconnect loop.
- On plans without realtime entitlement, the stream is rejected and the app
  falls back to the REST snapshot poll (labelled, not disguised as live).

## 8. Actual tests and build results

Run with mocked provider responses; no live credits were consumed.

- Backend: `./.venv/bin/python -m pytest -q` → **203 passed** (was 198; +5 in
  `tests/test_request_budget.py`).
- Frontend: `npx vitest run` → **87 passed** (was 86; +1 hidden-tab polling
  test).
- Frontend typecheck: `npx tsc --noEmit` → clean.
- Frontend build: `npm run build` → success (456.61 kB JS / 40.24 kB CSS).

## 9. Remaining provider restrictions

- Code cannot restore exhausted credits. Until Twelve Data resets the daily
  quota (or the plan is upgraded), live REST calls will still return
  `QUOTA_EXCEEDED`; the app will show the cached candles as `STALE` and the
  honest credit message.
- Real-time WebSocket access is plan-dependent; on plans without it the app uses
  the (now budget-respecting) REST poll.
- No paid provider was introduced and no keys are exposed to the browser.

## 10. Manual steps required

1. Restart the backend so the new caching/status logic loads; if running
   `uvicorn --reload`, it reloads automatically.
2. Wait for the Twelve Data credit window to reset (or upgrade the plan). Do not
   repeatedly retry while exhausted.
3. If a different credit budget is required, adjust the frontend snapshot poll
   interval (`frontend/src/App.tsx`, currently 120 s) — the candles cache TTL
   (120 s) and status behaviour need no change.
4. Optional verification: watch the backend log while the dashboard sits idle —
   no `/price` calls should be issued for status, and `/time_series` calls should
   occur at most once per symbol/timeframe per cache window.
