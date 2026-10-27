# TradeMind AI

TradeMind AI is a local analysis and paper-trading prototype for a **single instrument: gold (`XAUUSD`, displayed as `XAU/USD — Gold`)**. It provides a responsive React terminal, a candlestick chart, deterministic indicators and fair-value-gap detection, a cautious setup watch, an optional backend AI assistant, and simulated order entry. Market data comes from [xaus.com](https://xaus.com) (XAUS, keyless), with `goldprice.dev` selectable and a synthetic `DEMO` feed as the offline fallback. It never sends a broker order, and it never presents a stale price as live.

## Quick start

Requirements: Python 3.11+ and Node.js 22.12+.

You can also run `scripts/setup.sh` once, then launch the services with `scripts/run-backend.sh` and `scripts/run-frontend.sh` in separate terminals.

1. In `backend`, create and activate a virtual environment, then install dependencies:

   ```bash
   cd backend
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   alembic -c alembic.ini upgrade head
   uvicorn app.main:app --reload --port 8000
   ```

2. In a second terminal:

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

3. Open the Vite URL printed in the terminal (normally `http://localhost:5173`). API docs are at `http://localhost:8000/docs`.

For shell users on Windows, activate the backend environment with `.venv\\Scripts\\activate`.

## Configuration

Copy `.env.example` to `backend/.env` or export values in the backend shell. The backend loads that local `.env` file, with existing process environment variables taking precedence.

| Variable | Purpose |
| --- | --- |
| `AI_PROVIDER` | Active provider: `gemini` (default) or `openai` |
| `GEMINI_API_KEY` | Server-side Google AI Studio/Gemini API key |
| `GEMINI_MODEL` | Primary Gemini model; defaults to `gemini-flash-latest`. The alias maps to the current flash release |
| `GEMINI_FALLBACK_MODEL` | Optional secondary Gemini model, tried once when the primary is unavailable or its free-tier daily quota is exhausted |
| `GEMINI_FALLBACK_MODELS` | Optional comma-separated list of fallback models (in addition to `GEMINI_FALLBACK_MODEL`) |
| `OPENAI_API_KEY` | Optional server-side key for AI assistant requests |
| `OPENAI_MODEL` | Model identifier; the local backend template defaults to `gpt-6-luna`, subject to account access |
| `DATABASE_URL` | SQLAlchemy database URL; defaults to local SQLite (`sqlite:///./trademind.db`) |
| `FRONTEND_ORIGIN` | Single allowed browser origin for CORS |
| `LIVE_TRADING_ENABLED` | Must remain `false`; there is no live order route or adapter |
| `PAPER_INITIAL_BALANCE` | Virtual starting funds in each quote-currency ledger |
| `PAPER_FEE_RATE` | Simulated notional fee fraction; defaults to zero |
| `PAPER_SLIPPAGE_BPS` | Adverse simulated market-fill slippage; defaults to 0.5 basis points |
| `INTERNATIONAL_DATA_PROVIDER` | Market-data provider: `xaus` (default, keyless), `gold` (goldprice.dev), `twelvedata`, or `sifting`. Empty selects the synthetic `DEMO` feed |
| `INTERNATIONAL_DATA_API_KEY` | API key for `gold`/`twelvedata`/`sifting`. Not needed for `xaus`. Server-side only; never exposed to the browser |

Never place credentials in frontend variables or source control. Restart the backend after changing configuration. `GET /api/v1/ai/status` reports configured and verified state without returning a key. `POST /api/v1/ai/verify` makes one small real provider request and reports invalid credentials, model access/request errors, rate limits, and network errors without exposing provider details. A key/model pair is not verified until that request succeeds or an AI chat request succeeds.

The status payload exposes exactly one canonical `ai_state` — `CHECKING`, `VERIFIED`, `UNVERIFIED`, `EXPIRED`, `ERROR`, `QUOTA_EXCEEDED`, or `CONFIGURATION_ERROR` — plus a human `ai_state_label`. The frontend renders only this value; there is no second, conflicting verification state in the UI. A genuinely successful verification *or chat* keeps `VERIFIED` for `AI_VERIFICATION_LIFETIME_SECONDS` (default 300 = five minutes) and then becomes the neutral `EXPIRED` (re-verify), so the badge no longer flickers between "Verified" and "Connected — verification expired" per request. `CONFIGURATION_ERROR` (missing key, invalid key, unavailable model) and `ERROR`/`QUOTA_EXCEEDED` are distinct, and a cached success never masks a later failure. `POST /api/v1/ai/chat` returns the same `ai_status` object as `/api/v1/ai/status`, so the UI updates from a single source of truth.

### Market data: gold only

TradeMind AI serves exactly one instrument, `XAUUSD` (`XAU/USD — Gold`). Any other `symbol`
is rejected with HTTP 400. The default market-data provider is **xaus.com (XAUS)**, which
is keyless (no API key required); when no provider is selected the app falls back to the
deterministic synthetic `DEMO` feed. `goldprice.dev`, Twelve Data, and SiftingIO remain
selectable but are not the default.

Two independent signals are reported and must not be conflated:

- **Session** (`GET /api/v1/market/session`): `OPEN`, `CLOSED`, or `SCHEDULE_UNKNOWN`.
  With no broker configured, XAUUSD reports the **reference-feed session**: the documented
  OTC spot-gold clock that the aggregated reference feed follows — open Sunday 18:00,
  closed Friday 17:00 America/New_York, with the daily 17:00–18:00 ET maintenance break.
  The payload is labelled `session_basis: "reference_feed"` and `session_source: "documented"`;
  it is a calendar fact (`zoneinfo` applies EDT/EST correctly), never inferred from quotes.
  A provider that publishes its own calendar (`session_source: "provider"`) takes precedence,
  and a schedule that cannot be verified stays `SCHEDULE_UNKNOWN` instead of guessing.
- **Data** (`GET /api/v1/market-data/status` and `/market-data/quote`): `DEMO`, `LIVE`,
  `STALE`, `DELAYED` (reachable but no quote in hand), `UNAVAILABLE` (no quote yet),
  `RATE_LIMITED`, `QUOTA_EXCEEDED`, `PLAN_GATED`, or `ERROR`. Connectivity
  (`CONNECTED`/`CONFIGURED`/`DISCONNECTED`/`ERROR`) is reported separately and is never
  used to imply that a price is fresh.

The header price and the chart's horizontal last-price line both use the **same validated
quote**; the candle series is drawn from the provider's OHLCV feed unchanged. XAUS serves the
quote (`/api/v1/spot`, upstream `gold-api.com`) and the candles (`/api/v1/chart`, exchange
OHLCV) from *different* upstreams, so the two can legitimately differ (for example a spot
quote vs Friday's futures close). The divergence is disclosed in the chart footer
(`Quote vs chart close: …`) and reported by `GET /api/v1/market-data/diagnostics` rather than
being hidden — neither number is ever altered or fabricated.

A successful connection is not evidence of a fresh price: `LIVE` requires a recent
successful update, a provider-flagged `is_stale` quote is shown as `STALE`, and the UI never
invents, interpolates, or carries a stale price forward as `LIVE`. The status endpoint reads
the last quote from memory and does not spend a provider request.

**Quote freshness and candle freshness are reported independently.** `GET /api/v1/market-data/status`
returns `quote_status` (`FRESH`/`STALE`/`UNAVAILABLE`) alongside the quote-driven `data_status`,
and `GET /api/v1/analysis/snapshot` returns `candle_freshness` (`FRESH`/`STALE`/`UNAVAILABLE`
with `latest_candle_timestamp`, `age_seconds`, `bar_seconds`, `stale_threshold_seconds`, and
`candle_count`). A series is `STALE` once its newest bar is older than four of its own intervals.
A fresh spot quote never makes an old OHLC series fresh (XAUS serves the two from different
upstreams), so the dashboard shows Quote, Candles, and the AI provider as three separate
statuses. The AI market context receives both, and the assistant is instructed to report stale
candles instead of presenting a live setup from them.

**XAUS timeframes** (no plan gating; see `available_timeframes` in `/market-data/status`):

| Timeframe | Source |
| --- | --- |
| `1m`, `2m`, `5m`, `15m`, `30m`, `1h`, `1d`, `1w` | Native XAUS OHLCV candles |
| `3m` | Aggregated from real `1m` candles |
| `2h`, `4h` | Aggregated from real `1h` candles |

Aggregation only ever uses complete, contiguous source buckets and drops incomplete ones
with a warning — a candle is never padded, and never built from a single quote. XAUS
requires no API key, is GET-only, and imposes no enforced rate limit (fair use). When XAUS
answers an unsupported interval with a coarser series (HTTP 200 + a substituted `interval`),
the adapter rejects it instead of presenting the wrong interval.

If you switch to `goldprice.dev`, intervals become plan-gated and the app only offers what
the key can actually serve:

| Plan | Intervals |
| --- | --- |
| Free (`free_2026`) | `1d` only |
| Plus | `1h`, `4h` |
| Pro | `1m`, `5m`, `15m`, `30m`, `2h` + WebSocket stream |

Requesting a gated interval returns a structured `PLAN_GATED` error naming the tier that
unlocks it. The backend never aggregates or downsamples daily bars into intraday candles,
and never builds a candle from a single quote. On the free tier the 30-day history window
yields roughly 21 daily bars, so long-window indicators (for example EMA-50/EMA-200,
Bollinger Bands, and MACD) legitimately report `Unavailable` rather than fabricated values.
The free-tier rate limit is 10 requests/minute; over-limit responses surface `RATE_LIMITED`
with the provider's `retry_after_seconds`, and the frontend does not poll faster than the
provider budget allows.

### Gemini quota and model fallback

Google AI Studio free-tier limits are enforced **per project, per model, per day**. Exhausting one model does not consume another model's quota, so `GEMINI_FALLBACK_MODEL` is a legitimate, free remediation — it does not enable billing. Set it to a model you have confirmed is available to your key, for example:

```
GEMINI_MODEL=gemini-flash-latest
GEMINI_FALLBACK_MODEL=gemini-3.1-flash-lite
```

Backend behavior:

- A daily/project quota exhaustion (`RESOURCE_EXHAUSTED`, `GenerateRequestsPerDayPerProjectPerModel-FreeTier`) is **not** retried in a loop. The service tries the configured fallback once; if that also returns quota exhaustion it stops and returns a clear error.
- A transient per-minute rate limit and 5xx/network errors use bounded exponential backoff with jitter (limited attempts and a total wait ceiling). Authentication errors never fall back or retry.
- Chat failures return HTTP 502 with a human-readable `detail` string plus machine-readable `X-AI-Error-Code` and `Retry-After` headers. Raw provider errors, stack traces, and credentials are never returned.
- The frontend does **not** call the provider automatically on page load; verification is an explicit action, which conserves the limited daily quota. The submit button is disabled while a request is pending.

A code change cannot restore provider-side quota. If every configured model is exhausted, AI chat reports that clearly while charts, indicators, market data, deterministic BUY/SELL analysis, and paper trading remain available.

## Implemented

- Versioned FastAPI endpoints for health, the instrument catalog, provider-backed candles/quotes/status, market session, indicator snapshots, FVG zones, confirmed swing pivots, directional BUY/SELL rule evaluations, risk sizing, paper orders/history, integration catalog, TradingView status, and AI chat.
- Formal market-data and broker adapter protocols keep future providers behind normalized boundaries. The configured market-data provider is xaus.com (XAUS, normalized to `XAUUSD`/OHLCV), falling back to the synthetic `DEMO` feed; concrete broker adapters are absent.
- Deterministic candle series labeled `DEMO` when no provider is configured; bars align to completed UTC timeframe boundaries. DEMO volume is unavailable and is not fabricated.
- Shared candle validation tags instrument, timeframe, and data source; timestamps are normalized to UTC, OHLC and intervals are checked, and gaps are reported without filling them.
- EMA (SMA-seeded), SMA, Wilder-smoothed RSI and ATR, MACD, Bollinger Bands, and session-reset typical-price VWAP. VWAP is unavailable without exchange/tick volume. Warm-up values are null.
- Three-candle FVG zones with stable IDs, source timestamps, full-mitigation and invalidation state; confirmed-pivot swings, BOS/CHoCH, liquidity sweeps, support/resistance clusters, and mechanical candidate/confirmed/invalidated order blocks.
- AI requests include backend-generated candle, indicator, pattern, structure, and strategy context for the supported instrument and the requested timeframe; end-of-data and freshness caveats are passed through rather than hidden.
- Decimal position-size arithmetic requires an explicit unit-value input and rejects missing contract metadata.
- Market paper orders are simulated fills with configurable fees/slippage; limit orders stay pending until a later history refresh sees a completed demo close cross the limit. Stop-loss/take-profit levels are validated, persisted, and close net positions when a completed demo close crosses them. Position accounting includes open positions and realized/unrealized P&L.
- The simulated ledger is partitioned by quote currency with configurable initial balance. It does not aggregate currencies without an FX source. DEMO unit definitions are explicit synthetic units, not broker lots or exchange contracts; availability is a simplified simulator estimate.
- Dashboard BUY/SELL evaluation buttons call a backend rule engine using EMA20/50 alignment, close vs EMA20, a confirmed structure break on the latest candle, ATR-derived 1.5R stop distance, and a 2R target. It rejects stale input and labels output DEMO and unvalidated.
- Chart drawing controls toggle backend-derived FVG boundaries, confirmed order-block boundaries, and support/resistance levels; Clear Drawings removes those overlays.
- Responsive dashboard with a single fixed instrument (`XAU/USD — Gold`), plan-driven timeframe buttons, one large candlestick chart, indicator toggles, assistant context, and an order-review modal. Supported timeframes come from the backend (`available_timeframes`), so a plan-gated interval is never offered.
- TradingView-style chart footer: a display-timezone selector (default `Asia/Kolkata` IST, plus `Etc/UTC` and the instrument/provider exchange zone) that formats the axis and crosshair through the chart library's official localization options, a live one-second clock showing the zone abbreviation and UTC offset (`17:30:00 IST UTC+05:30`), the latest candle time in the selected zone, the quote/candle source labels, and a disclosed quote-vs-chart-close difference. Switching zones only re-formats labels — stored timestamps stay UTC and the visible range, zoom, scroll, and drawings are preserved.
- Gemini (default provider) uses the official `google-genai` SDK from the backend, with a configurable primary model and optional fallback model. OpenAI is available as an alternate provider via the official Python SDK Responses API. Missing configuration returns a 503 while the rest of the app remains available.

## Limitations

- Paper accounting is useful only as a transparent demonstration: balances restart from configured starting capital, P&L uses synthetic one-unit conventions, fee rate defaults to zero, slippage defaults to 0.5 bps, and currencies are not converted or aggregated. Stop/target and limit triggers use completed candle closes, so intrabar ordering is not simulated. Position sizing still requires caller-supplied verified unit-value and conversion metadata.
- The directional setup evaluator is mechanical and unvalidated. It refuses to signal from stale or insufficient data; it is not a trade signal or a performance claim.
- xaus.com is a third-party aggregated gold feed and is not the LBMA/COMEX benchmark. Its OHLCV volume is upstream exchange/futures volume, not spot tick volume, so VWAP is available but is labelled with that provenance. When a provider window cannot fill a long-window indicator, that indicator reports `Unavailable` rather than an extrapolated value. If no provider is configured the app runs on a synthetic `DEMO` series and labels it as such.
- Because xaus.com publishes no trading calendar, XAUUSD reports the documented OTC spot-gold **reference-feed** session (`session_basis: "reference_feed"`), labelled as such rather than as a specific broker's executable hours. Session state is a calendar fact and never forces a quote to `STALE`; an unverifiable schedule stays `SCHEDULE_UNKNOWN`.
- The provider catalog lists three entries: the synthetic `DEMO` feed, `xaus.com (XAUS Gold API)`, and `goldprice.dev (Gold API)`, each advertising `XAUUSD` only. Concrete broker/execution adapters and OAuth flows are not implemented.
- TradingView is a link-only integration. No chart widget or personal account authentication is enabled. TradingView's Advanced Charts library is distributed through its private GitHub repository upon access approval and has license conditions; no library files are present here.
- AI uses the selected provider with caller-selected model and receives structured backend context. It does not expose a function-tool registry; API/provider errors are sanitized. Gemini responses may come from the configured fallback model when the primary is unavailable or its free-tier daily quota is exhausted.
- Authentication, user accounts, rate limiting, WebSockets, and production deployment hardening are not implemented. Alembic currently migrates only the paper-order table. Do not expose this development server to the public internet.
- `LIVE_TRADING_ENABLED` is informational and live execution is absent; there is no path to place a real order.
- The frontend uses custom responsive CSS instead of Tailwind CSS.

## Tests

Backend tests:

```bash
cd backend
pytest -q
```

Frontend build and tests:

```bash
cd frontend
npm run build
npm test
```

Verification: backend `282 passed`; frontend `120 passed`; TypeScript/Vite production build succeeded. The AI verification was also confirmed against the live provider (never printing the key): `POST /api/v1/ai/verify` returned `ai_state: "VERIFIED"` using the resolved `gemini-3.1-flash-lite` model behind the configured `gemini-flash-latest` alias, and a real `POST /api/v1/ai/chat` request used the configured Gemini model, received the backend market context, and correctly reported `candle_status: "STALE"` with the quote unavailable instead of inventing a price. The default `XAUUSD` provider **xaus.com (XAUS)** was verified end-to-end against the live, keyless API, separately from the mocked unit tests: a live quote (`/api/v1/spot`, upstream `gold-api.com`), native OHLCV candles for every advertised interval (`/api/v1/chart`, `5m` requested as `interval=5m` and never aggregated), indicators and VWAP from that volume, the deterministic reference-feed session (`CLOSED` on a Sunday before 18:00 New York time, correct `next_open`), and the quote-vs-candle diagnostics (a real spot-vs-futures divergence of `-15.00` reported, not hidden). `goldprice.dev`, Twelve Data, and SiftingIO remain selectable; the synthetic `DEMO` feed remains the offline fallback.

The test suite covers EMA, RSI, ATR, VWAP and session resets, MACD and Bollinger calculations, warm-up behavior, candle normalization, gap reporting, duplicate/out-of-order/wrong-timestamp-unit handling, native-vs-aggregated intervals, FVG boundaries/mitigation, swing/structure confirmation, order-block confirmation/invalidation, single-instrument symbol validation, API demo/status labeling, quote/candle source disclosure, the reference-feed session (`OPEN`/`CLOSED`/`SCHEDULE_UNKNOWN`, weekend and daylight-saving boundaries, session/data independence), AI-context construction, risk validation, simulated order history, chart coordinate conversion, the quote-driven last-price line, timezone resolution and switching, the chart footer clock, and the frontend's plan-driven timeframe and staleness UI.

## Architecture and safety

See [`docs/architecture.md`](docs/architecture.md), [`docs/security.md`](docs/security.md), and [`docs/api.md`](docs/api.md). This is a development prototype, not investment advice. Demo strategy labels are unvalidated and do not represent a probability of success.
