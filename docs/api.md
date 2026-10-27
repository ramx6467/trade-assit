# API quick reference

OpenAPI is available at `/docs` when the backend is running.

TradeMind AI serves **one instrument only**: `XAUUSD`, displayed as `XAU/USD — Gold`.
Any other `symbol` value is rejected with HTTP 400 (`... XAU/USD — Gold) only.`). The
default market-data provider is **xaus.com (XAUS)**, a keyless gold API, with the
synthetic `DEMO` feed as the offline fallback. `goldprice.dev`, Twelve Data, and
SiftingIO remain selectable but are not the default.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/health` | Runtime mode, `instrument`, and broker/AI status |
| GET | `/api/v1/instruments` | Instrument catalog (one row: XAUUSD) |
| GET | `/api/v1/market-data/candles?symbol=XAUUSD&timeframe=1d` | Candle history from the configured provider |
| GET | `/api/v1/market-data/history?symbol=XAUUSD&timeframe=1d&before=…` | Paged older candles for chart scroll-back |
| GET | `/api/v1/market-data/quote?symbol=XAUUSD&timeframe=1d` | Latest quote (price + provider timestamp + staleness) |
| GET | `/api/v1/market-data/status?symbol=XAUUSD` | Connection/data status plus `available_timeframes`, `timeframe_note`, a separate `quote_status` (`FRESH`/`STALE`/`UNAVAILABLE`), and separate quote/candle `sources` |
| GET | `/api/v1/market-data/diagnostics?symbol=XAUUSD&timeframe=5m` | Provenance trace: provider, requested symbol/timeframe, latest quote (price/time/age), latest candle (open time/close/age, forming/closed), and whether the quote and candle feeds differ. No keys or secrets |
| GET | `/api/v1/market/session?symbol=XAUUSD` | Trading-session state (separate from quote freshness) |
| GET | `/api/v1/analysis/snapshot?symbol=XAUUSD&timeframe=1d` | One candle set plus indicator, FVG, order-block, structure, setup results, and an independent `candle_freshness` block (`status`, `latest_candle_timestamp`, `age_seconds`, `bar_seconds`, `stale_threshold_seconds`, `candle_count`) |
| GET | `/api/v1/indicators?symbol=XAUUSD&timeframe=1d` | Latest indicator values |
| GET | `/api/v1/analysis/fvgs` | Three-candle FVG detections |
| GET | `/api/v1/analysis/order-blocks` | Mechanical candidate, confirmed, and invalidated order-block zones |
| GET | `/api/v1/analysis/structure` | Confirmed pivots, BOS/CHoCH, sweeps, and pivot clusters |
| GET | `/api/v1/analysis/setup` | Illustrative, non-actionable setup watch |
| GET/POST | `/api/v1/analysis/evaluate?symbol=XAUUSD&timeframe=1d&side=BUY` | Structured directional rule evaluation; data freshness, EMA trend, latest close, confirmed break, and ATR checks |
| POST | `/api/v1/risk/position-size` | Hypothetical size; requires `unit_value` |
| POST | `/api/v1/paper/orders` | Simulated MARKET fill or pending LIMIT order; optional validated stop/target fields |
| GET | `/api/v1/paper/history` | Persisted simulated orders, positions, per-currency equity/P&L, fees, and slippage model |
| GET | `/api/v1/brokers` | Provider catalog: DEMO plus the gold market-data integrations (xaus.com, goldprice.dev), all advertising `XAUUSD` only |
| GET | `/api/v1/integrations/tradingview` | TradingView link-only status and account/widget capability flags |
| GET | `/api/v1/ai/status` | AI configured/verified state (no key returned). Returns one canonical `ai_state` (`CHECKING`/`VERIFIED`/`UNVERIFIED`/`EXPIRED`/`ERROR`/`QUOTA_EXCEEDED`/`CONFIGURATION_ERROR`), `ai_state_label`, `in_flight`, and `verification_lifetime_seconds` |
| POST | `/api/v1/ai/verify` | One real provider request to validate the configured key/model; the result drives the canonical `ai_state` |
| POST | `/api/v1/ai/chat` | Optional backend AI chat call; a genuine success refreshes `VERIFIED`, and the response echoes the same `ai_status` object |

Errors use FastAPI's JSON `detail` response.

## Provider status and freshness

Two independent axes are reported and must not be conflated:

- **Session** (`/market/session`): `OPEN`, `CLOSED`, or `SCHEDULE_UNKNOWN`. With no broker
  configured, XAUUSD reports the **reference-feed session**: the documented OTC spot-gold
  clock (open Sunday 18:00, closed Friday 17:00 America/New_York, daily 17:00–18:00 ET
  maintenance break), labelled `session_basis: "reference_feed"`. The payload mirrors the
  contract at the top level — `session_status`, `source`, `checked_at`, `next_open`,
  `next_close`, `timezone`, `explanation` — with the full detail under `session`. DST is
  handled by `zoneinfo`. A provider that publishes its own calendar is preferred
  (`session_source: "provider"`); an unverifiable schedule stays `SCHEDULE_UNKNOWN`. A
  session state is never used to imply that a quote is fresh.
- **Data** (`/market-data/status`, `/market-data/quote`): `DEMO`, `LIVE`, `STALE`,
  `DELAYED` (reachable but no quote in hand), `UNAVAILABLE` (no quote observed yet),
  `RATE_LIMITED`, `QUOTA_EXCEEDED`, `PLAN_GATED`, or `ERROR`. The status endpoint does not
  spend a provider credit — it reports the last quote seen in memory, and shows `null` when
  a price has never been observed. Connectivity (`CONNECTED` / `CONFIGURED` /
  `DISCONNECTED` / `ERROR`) is reported separately and never used to imply freshness.

The quote and the candle series are reported as distinct sources
(`/market-data/status` → `sources.quote` / `sources.candles`). XAUS serves the quote from
`/api/v1/spot` (upstream `gold-api.com`) and the OHLCV candles from `/api/v1/chart` (an
exchange/futures feed), so the two prices can legitimately differ. The chart's last-price
line and the header price both use the same validated quote; the divergence is disclosed
(`/market-data/diagnostics`, and a `Quote vs chart close:` note in the chart footer) rather
than silently combined or replaced with a fabricated number.

A provider connection alone never marks a price `LIVE`: the reported `data_status` also
requires a recent successful update. A price flagged `is_stale` by the provider is
surfaced as `STALE`, never relabeled `LIVE`.

## Timeframes (XAUS default)

`/market-data/status` returns `available_timeframes` for the active provider. XAUS serves
all documented frames with no plan gating:

| Timeframe | Source |
| --- | --- |
| `1m`, `2m`, `5m`, `15m`, `30m`, `1h`, `1d`, `1w` | Native XAUS `/api/v1/chart` OHLCV |
| `3m` | Aggregated from real `1m` candles |
| `2h`, `4h` | Aggregated from real `1h` candles |

Aggregated frames use only complete, contiguous source buckets (first open, max high, min
low, last close, summed volume); incomplete buckets are dropped with a warning, never
padded. XAUS is keyless, GET-only, CORS-open, and imposes no enforced rate limit, so
`RATE_LIMITED`/`QUOTA_EXCEEDED` are not expected but are still surfaced if the upstream
asks us to slow down.

When XAUS answers a request for an interval it does not really serve, it returns HTTP 200
with a substituted (coarser) interval; the adapter rejects that response rather than
present it as the requested interval.

## goldprice.dev intervals (plan-gated, optional)

If you switch `INTERNATIONAL_DATA_PROVIDER` back to `gold`, `/market-data/status` returns
`available_timeframes` for the configured plan. Requests for a gated interval return a
structured `PLAN_GATED` error naming the tier that unlocks it — the backend never
fabricates or downsamples intraday candles from daily bars.

| Plan | Intervals |
| --- | --- |
| Free (`free_2026`) | `1d` only (30-day window ⇒ ~21 bars) |
| Plus | `1h`, `4h` |
| Pro | `1m`, `5m`, `15m`, `30m`, `2h` + WebSocket stream |

Rate limit on the free tier is **10 requests/minute**; over-limit responses surface
`RATE_LIMITED` with the provider's `retry_after_seconds`.

`/analysis/snapshot` is the UI's consistent single-candle-set response. AI receives
generated backend analysis context; it has no model-callable function tools. The API
contains no live order route. When only one provider bar is available (a single quote
rather than a bar series), the UI explains that OHLC history is unavailable instead of
synthesizing a candle.
