"""FastAPI application for TradeMind AI.

.. warning::
    RECONSTRUCTION NOTICE. This module was accidentally overwritten to a single
    line and has been reconstructed from the test suite (``backend/tests``),
    the other intact application modules, and the documented API contract in
    ``docs/api.md``. It is a faithful, test-verified reconstruction - not the
    byte-identical original. Behaviour is pinned by ``backend/tests``.

Exposes market data, deterministic analysis, AI review/chat, risk sizing,
simulated (paper) trading, broker/integration catalogues, and market sessions.
All market values come from a real provider when configured; otherwise the
explicit synthetic DEMO feed is used and labelled as such.
"""
from __future__ import annotations

from app import config  # noqa: F401 - loads backend/.env before other imports
from app import ai_config

import json
import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.database import Base, SessionLocal, engine
from app import models  # noqa: F401 - register ORM models on Base metadata
from app.models import PaperOrderRecord, PaperPositionRecord

from app.market_data.providers import (
    AGGREGATION_PLANS,
    XAUS_NATIVE_INTERVALS,
    MarketDataService,
    ProviderError,
    TIMEFRAMES,
    close_market_streams,
)
from app.market_data.normalize import (
    TIMEFRAME_SECONDS,
    DataInvalidError,
    PriceScaleMismatchError,
    validate_price_scale,
    validate_xauusd_candles,
)
from app.market_data.demo import SYMBOLS

from app.analysis.indicators import ema, sma, rsi, atr, vwap, bollinger_bands, macd, adx
from app.analysis.smc import (
    market_structure,
    dealing_range,
    detect_fvgs,
    detect_order_blocks,
    ORDER_BLOCK_METHODOLOGY,
)
from app.analysis.strategy import (
    evaluate_trading_setup,
    _make_empty_response,
    INSTRUMENT_SPECS,
)

from app.market_hours import market_session, market_session_summary

# Diagnostics logger: records provider/quote/candle provenance with no secrets.
# Point it at any log handler to trace a chart/quote disagreement.
market_logger = logging.getLogger("trademind.market")

from app.ai_config import (
    AIServiceError,
    active_ai_config,
    classify_gemini_error,
    classify_openai_error,
    get_ai_status,
    is_retryable_error,
    record_verification,
    verify_ai_configuration,
)
from app.ai_knowledge import knowledge_for_query


# ---------------------------------------------------------------------------
# Application and shared state
# ---------------------------------------------------------------------------

app = FastAPI(title="TradeMind AI", version="1.0.0")

_frontend_origin = os.getenv("FRONTEND_ORIGIN", "http://127.0.0.1:5173")
_app_origins = [o.strip() for o in _frontend_origin.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_app_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The single shared market-data service. Tests replace/ patch this object.
market_data = MarketDataService()


async def _shutdown_market_streams() -> None:
    """Release the shared upstream provider stream(s) on shutdown."""
    try:
        await close_market_streams()
    except Exception:  # pragma: no cover - shutdown must never raise
        pass


app.add_event_handler("shutdown", _shutdown_market_streams)


def _stream_info() -> dict[str, Any]:
    """Safe diagnostics for the shared live-stream connection (no secrets)."""
    getter = getattr(market_data, "stream_diagnostics", None)
    if not callable(getter):
        return {}
    try:
        return getter() or {}
    except Exception:  # pragma: no cover - diagnostics must never break a request
        return {}

# Most recent provider failure per symbol: (when, message, status).
_market_data_errors: dict[str, tuple[datetime, str, str]] = {}
# Most recent *validated* live tick per symbol (the full tick dict: price,
# timestamp, and its own freshness). SiftingIO replays the last known price when
# a symbol is first subscribed, so this is the last genuine provider quote even
# when the market is closed. It is only ever reported with its own timestamp and
# is never presented as LIVE once it is older than the freshness window.
_last_ticks: dict[str, dict[str, Any]] = {}
# Most recent live WebSocket tick timestamp per symbol (ISO-8601). Only live
# streams set this; REST/candle fetches never do.
_last_market_updates: dict[str, str] = {}
# Most recent successful REST candle/quote fetch per symbol. Used to report an
# honest provider connection status from real request outcomes, independently of
# data freshness (a configured key alone is never treated as "connected").
_last_market_success: dict[str, datetime] = {}

LIVE_FRESH_SECONDS = 90.0
STALE_SECONDS = 300.0
ERROR_TTL_SECONDS = 300.0
DEFAULT_LIMIT = 240
MAX_LIMIT = 5000
# This deployment serves a single instrument. Every API entry point validates
# against this tuple, so no request for another market is ever accepted or
# forwarded to a provider.
SUPPORTED_SYMBOLS: tuple[str, ...] = ("XAUUSD",)
# The display name the UI shows for the one supported instrument.
SUPPORTED_SYMBOL_NAME = "XAU/USD — Gold"
# Timeframes the DEMO feed serves. A configured provider may narrow this further
# (for example the GOLD adapter advertises only the intervals its key's plan can
# actually request), so callers must prefer the provider capability when present.
ALL_TIMEFRAMES = tuple(TIMEFRAMES)
# The chart snapshot intentionally asks for a deeper window than the raw
# candles endpoint default. 240 bars was too shallow for intraday timeframes
# (for example 240 x 1m is only four hours), which is why the chart appeared to
# show just the most recent day. /market-data/candles keeps its documented
# 240-bar default; the snapshot requests enough history to be useful and to let
# the frontend paginate older bars on left-scroll.
CHART_HISTORY_LIMIT = 1000

# 8 and 6 decimal-place quantisers so stored decimals stringify consistently
# (for example 0 -> "0E-8", 1.07 -> "1.07000000", 2 -> "2.000000").
_E8 = Decimal("0.00000001")
_E6 = Decimal("0.000001")

SYSTEM_PROMPT = (
    "You are TradeMind AI, a concise trading education and chart-analysis assistant.\n"
    "Always answer the user's latest question directly before adding explanation. "
    "Use the minimum detail needed: usually 1-4 sentences or 2-5 short bullets. "
    "Expand only when requested or when safety and accuracy require it.\n"
    "Do not return a chart-analysis report unless the latest question asks for chart "
    "analysis, current indicator analysis, or a trade/setup evaluation. Do not repeat "
    "a template for unrelated questions. Mention market structure, liquidity, FVGs, "
    "order blocks, indicators, entries, stops, targets, and risk only when relevant.\n"
    "Treat the latest user message as authoritative. Conversation history is for "
    "continuity only: prior assistant text is not evidence, a current answer, or a "
    "source of market facts. Never answer a previous question in place of the latest one.\n"
    "Use only backend market facts supplied with the current request. Never invent "
    "prices, timestamps, market hours, indicator values, patterns, or trade setups. "
    "If information is missing, stale, or unavailable, state exactly that. Distinguish "
    "verified facts from interpretations.\n"
    "For market-session questions, use the supplied deterministic session schedule; "
    "do not infer OPEN/CLOSED from quotes, candles, or provider connectivity. If the "
    "schedule is unverified, answer UNKNOWN and say why. Provider connection, quote "
    "freshness, and candle freshness are separate facts.\n"
    "For current-price questions, use only the supplied quote and identify its source, "
    "timestamp, currency/unit, and freshness. Never present DEMO or stale data as a "
    "fresh live price.\n"
    "For chart analysis, use only the selected symbol/timeframe and supplied valid "
    "candles, indicators, and multi-timeframe context. A stale candle series cannot "
    "support a current live setup. Claim an ICT/SMC pattern only when the supplied "
    "calculations support it. For setup decisions, follow the deterministic backend "
    "BUY/SELL/WATCHLIST/NO_SETUP result and never invent or alter levels.\n"
    "For educational questions, explain the concept directly without treating it as "
    "a live-market request. Answer general unrelated questions normally, within scope. "
    "Never guarantee profit or describe a setup as certain."
)

# Also register tables at import for local development (idempotent). In tests
# conftest creates them; on a fresh dev database this avoids a missing-table 500.
try:  # pragma: no cover - defensive bootstrap
    Base.metadata.create_all(bind=engine)
except Exception:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"] = "user"
    text: str = Field(default="", max_length=4000)
    content: str | None = Field(default=None, max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    symbol: str | None = None
    timeframe: str | None = None
    include_market_context: bool = True
    history: list[ChatMessage] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_chat_input(self) -> "ChatRequest":
        self.message = self.message.strip()
        if not self.message:
            raise ValueError("message must not be empty")
        if len(self.history) > 100:
            raise ValueError("history may contain at most 100 messages")
        return self


class PaperOrderRequest(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(gt=0)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None

    @model_validator(mode="after")
    def _require_entry_price_for_limit(self) -> "PaperOrderRequest":
        if self.order_type == "LIMIT" and self.entry_price is None:
            raise ValueError("entry_price is required for LIMIT orders")
        return self


class PositionSizeRequest(BaseModel):
    equity: Decimal
    risk_percent: float
    entry: float
    stop_loss: float
    unit_value: float
    side: Literal["BUY", "SELL"] = "BUY"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _d8(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(_E8, rounding=ROUND_HALF_UP)


def _d6(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(_E6, rounding=ROUND_HALF_UP)


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tick_is_newest(symbol: str, timestamp: Any) -> bool:
    """Return True when ``timestamp`` is at least as new as the last kept tick.

    SiftingIO replays its last cached snapshot on every (re)subscribe, so an
    arrival-time check would let that older replay overwrite a newer tick and
    become the "current" price. Comparison is strictly by the provider's own
    timestamp. A missing/invalid stamp is never treated as newest.
    """
    if not timestamp:
        return False
    previous = _last_ticks.get(symbol) or {}
    previous_stamp = previous.get("timestamp")
    if not previous_stamp:
        return True
    try:
        return _parse_ts(timestamp) >= _parse_ts(previous_stamp)
    except Exception:
        return False


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _fee_rate() -> float:
    return _env_float("PAPER_FEE_RATE", 0.0)


def _slippage_bps() -> float:
    return _env_float("PAPER_SLIPPAGE_BPS", 2.0)


def _initial_balance() -> float:
    return _env_float("PAPER_INITIAL_BALANCE", 100000.0)


def _all_env_keys_present() -> bool:  # pragma: no cover - documentation helper
    return True


def _known_symbols() -> set[str]:
    symbols = set(SYMBOLS)
    try:
        for row in market_data.get_instruments():
            if isinstance(row, dict) and row.get("symbol"):
                symbols.add(str(row["symbol"]).upper())
    except Exception:
        pass
    return symbols


def _validate_symbol(symbol: str, *, status_code: int = 400) -> str:
    sym = (symbol or "").strip().upper()
    if sym not in SUPPORTED_SYMBOLS:
        raise HTTPException(
            status_code=status_code,
            detail=(
                f"Unsupported instrument: {symbol!r}. This deployment serves "
                f"{', '.join(SUPPORTED_SYMBOLS)} (XAU/USD — Gold) only."
            ),
        )
    return sym


def _provider_label(symbol: str) -> str:
    """Human-readable name of the provider actually serving ``symbol``."""
    try:
        status = market_data.status(symbol)
    except Exception:  # pragma: no cover - defensive
        return "the configured provider"
    if not status.get("configured"):
        return "the DEMO feed"
    name = str(status.get("provider") or "").upper()
    return {
        "XAUS": "xaus.com",
        "GOLD": "goldprice.dev",
        "TWELVE_DATA": "Twelve Data",
        "SIFTING": "SiftingIO",
    }.get(name, name or "the configured provider")


def _available_timeframes(symbol: str) -> list[str]:
    """Timeframes that can actually be served for ``symbol``.

    The provider capability wins when it exposes one (for example the XAUS or
    GOLD adapter narrows the set to what the account can genuinely serve);
    otherwise the DEMO feed serves the full documented frame set.
    """
    capability = market_data.available_timeframes(symbol)
    if capability:
        return [frame for frame in ALL_TIMEFRAMES if frame in capability]
    return list(ALL_TIMEFRAMES)


def _timeframe_note(symbol: str) -> str:
    """Explain, honestly, when a provider cannot serve every documented frame."""
    capability = market_data.available_timeframes(symbol)
    if capability is None:
        return ""
    frames = _available_timeframes(symbol)
    if len(frames) >= len(ALL_TIMEFRAMES):
        return ""
    return (
        "Only "
        + ", ".join(frames)
        + " can be requested for "
        + symbol
        + f": {_provider_label(symbol)} does not serve the remaining intervals on this "
        "account. Candles are never fabricated or aggregated up from a coarser feed."
    )


TIMEFRAME_ALIASES: dict[str, str] = {
    "1min": "1m", "2min": "2m", "3min": "3m", "5min": "5m",
    "15min": "15m", "30min": "30m", "60m": "1h", "60min": "1h",
    "1hour": "1h", "2hour": "2h", "4hour": "4h",
    "1day": "1d", "daily": "1d", "1week": "1w", "weekly": "1w",
    "m1": "1m", "m2": "2m", "m3": "3m", "m5": "5m",
    "m15": "15m", "m30": "30m", "h1": "1h", "h2": "2h",
    "h4": "4h", "d1": "1d", "w1": "1w",
}


def _validate_timeframe(timeframe: str, *, status_code: int = 400) -> str:
    raw = (timeframe or "").strip()
    tf = TIMEFRAME_ALIASES.get(raw.lower(), raw)
    if tf not in TIMEFRAMES:
        raise HTTPException(
            status_code=status_code,
            detail=f"Unsupported timeframe: {timeframe!r}. Supported: {', '.join(TIMEFRAMES)}.",
        )
    return tf


def _validate_side(side: str, *, status_code: int = 400) -> str:
    value = (side or "").strip().upper()
    if value not in {"BUY", "SELL"}:
        raise HTTPException(status_code=status_code, detail="side must be BUY or SELL")
    return value


def _exchange_timezone(symbol: str) -> str:
    return SYMBOLS.get(symbol.upper(), {}).get("exchange_timezone") or "UTC"


def _provider_id(symbol: str) -> str:
    try:
        return market_data.status(symbol).get("provider") or "DEMO"
    except Exception:
        return "DEMO"


def _last_successful_timestamp(rows: list[dict]) -> str | None:
    return rows[-1]["timestamp"] if rows else None


def _data_status(symbol: str) -> str:
    """Resolve the honest market-data status for a symbol.

    Order of precedence:
      1. No provider configured -> "DEMO" (explicit synthetic feed).
      2. A fresh WebSocket tick -> "LIVE" (a verified stream overrides a stale
         REST error, because the stream is the stronger signal).
      3. A recent provider error -> that error status (e.g. QUOTA_EXCEEDED).
      4. A recent REST quote whose own observation timestamp is fresh -> LIVE;
         an older or provider-flagged-stale quote -> STALE, never LIVE.
      5. Any older verified tick -> "STALE" (SiftingIO replays the last known
         price on subscribe; an old replay is stale, never the provider's
         static status).
      6. No tick at all -> "DELAYED" once a request has succeeded (reachable but
         no quote in hand), else "UNAVAILABLE". A concrete provider status is
         passed through. "DISCONNECTED" is never used here: it describes
         connectivity, which _connection_status() owns.
    """
    try:
        status = market_data.status(symbol)
    except Exception as exc:  # pragma: no cover - defensive
        return "ERROR"

    if not status.get("configured"):
        return "DEMO"

    error = _market_data_errors.get(symbol)
    if error:
        when, _message, error_status = error
        try:
            error_age = (_now() - _parse_ts(when)).total_seconds()
        except Exception:
            error_age = 0.0
        if error_age <= ERROR_TTL_SECONDS and error_status in {"PRICE_SCALE_MISMATCH", "DATA_INVALID", "QUOTA_EXCEEDED"}:
            return error_status

    last_tick = _last_market_updates.get(symbol)
    tick_age: float | None = None
    if last_tick:
        try:
            tick_age = (_now() - _parse_ts(last_tick)).total_seconds()
        except Exception:
            tick_age = None
    if tick_age is not None and 0 <= tick_age <= LIVE_FRESH_SECONDS:
        return "LIVE"

    # A fresh REST quote is genuine evidence of live data even without a socket:
    # it carries the provider's own observation timestamp, so a recent one is
    # LIVE. A merely configured key is never enough (handled above), and a price
    # the provider itself flags stale is never promoted.
    quote = None
    if status.get("configured"):
        getter = getattr(market_data, "get_cached_quote", None)
        quote = getter(symbol) if getter else None
    quote_age: float | None = None
    if quote and quote.get("price") is not None:
        stamp = quote.get("timestamp")
        if stamp:
            try:
                quote_age = (_now() - _parse_ts(stamp)).total_seconds()
            except Exception:
                quote_age = None
        if not quote.get("is_stale") and quote_age is not None and 0 <= quote_age <= LIVE_FRESH_SECONDS:
            return "LIVE"

    error = _market_data_errors.get(symbol)
    if error:
        when, _message, error_status = error
        try:
            error_age = (_now() - _parse_ts(when)).total_seconds()
        except Exception:
            error_age = 0.0
        if error_age <= ERROR_TTL_SECONDS and error_status:
            return error_status

    if tick_age is not None:
        # A verified tick exists but is not fresh (SiftingIO replays the last
        # known price on subscribe). Report it honestly as STALE instead of the
        # provider's static status, which never reflects the replay.
        return "STALE"

    if quote is not None and quote.get("price") is not None:
        # A cached quote that is not fresh is still the last genuine price the
        # provider gave us. It is reported as STALE rather than dropped or, far
        # worse, relabelled LIVE.
        return "STALE"

    # No verified tick and no recent error. "DISCONNECTED" is a *connection*
    # state, not a data-freshness one, and the provider exposes it statically
    # regardless of reachability - so it must never be used here. A provider
    # that already served a request is reachable but its latest quote is not in
    # hand -> DELAYED. With no recorded success there is simply no quote yet ->
    # UNAVAILABLE. Connectivity is reported separately by _connection_status().
    provider_status = status.get("data_status")
    if provider_status and provider_status not in {"DISCONNECTED", "CONNECTED"}:
        return provider_status
    return "DELAYED" if _last_market_success.get(symbol) is not None else "UNAVAILABLE"


def _connection_status(symbol: str) -> str:
    """Provider connection state derived from real request outcomes.

    This is deliberately independent of market-open state and of data freshness:

      * No provider configured            -> "DEMO"
      * Fresh WebSocket tick              -> "STREAMING"
      * Recent provider error             -> that error status
                                             (DISCONNECTED / RATE_LIMITED /
                                              QUOTA_EXCEEDED / ERROR)
      * Recent successful REST fetch      -> "CONNECTED"
      * Configured but no request recorded-> "CONFIGURED" (never "DISCONNECTED")

    A configured key is never reported as connected on its own.
    """
    try:
        status = market_data.status(symbol)
    except Exception:  # pragma: no cover - defensive
        return "ERROR"

    if not status.get("configured"):
        return "DEMO"

    last_tick = _last_market_updates.get(symbol)
    if last_tick:
        try:
            tick_age = (_now() - _parse_ts(last_tick)).total_seconds()
        except Exception:
            tick_age = None
        if tick_age is not None and 0 <= tick_age <= LIVE_FRESH_SECONDS:
            return "STREAMING"

    error = _market_data_errors.get(symbol)
    success_time = _last_market_success.get(symbol)
    if error:
        when, _message, error_status = error
        try:
            error_age = (_now() - _parse_ts(when)).total_seconds()
        except Exception:
            error_age = 0.0
        if (success_time is None or success_time < when) and error_age <= ERROR_TTL_SECONDS and error_status:
            return error_status

    if success_time is not None:
        return "CONNECTED"

    return "CONFIGURED"


def _status_quote(symbol: str, configured: bool) -> dict[str, Any] | None:
    """Return a quote for status/overview views without spending a provider credit.

    A configured provider is only ever read from the in-memory quote cache, so
    rendering status cannot itself exhaust the provider's rate or credit budget.
    When no REST quote has been fetched yet, fall back to the last validated
    stream tick: that is still a genuine provider price (the stream replays the
    last known value on subscribe) and it carries its own timestamp, so it is
    reported honestly as STALE rather than dropped. The synthetic DEMO feed has
    no upstream cost, so it stays fully populated.
    """
    if configured:
        getter = getattr(market_data, "get_cached_quote", None)
        cached = getter(symbol) if getter else None
        if cached and cached.get("price") is not None:
            return cached
        tick = _last_ticks.get(symbol)
        if tick and tick.get("price") is not None:
            return tick
        provider = _provider_object(symbol)
        if provider and not getattr(provider, "requires_key", True):
            getter_latest = getattr(market_data, "get_latest_quote", None)
            if getter_latest:
                try:
                    q = getter_latest(symbol)
                    _last_market_success[symbol] = _now()
                    return q
                except Exception:
                    pass
        return cached
    getter = getattr(market_data, "get_latest_quote", None)
    if getter is None:
        return None
    try:
        return getter(symbol)
    except Exception:
        return None



def _provider_object(symbol: str) -> Any | None:
    """The active provider instance for ``symbol`` (or ``None`` if unresolved)."""
    resolver = getattr(market_data, "provider_for", None)
    if not callable(resolver):
        return None
    try:
        return resolver(symbol)
    except ProviderError:
        return None
    except Exception:
        return None


def _source_detail(symbol: str, attribute: str) -> str | None:
    provider = _provider_object(symbol)
    detail = getattr(provider, attribute, None)
    return str(detail) if detail else None


def _data_sources(symbol: str, quote: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe where the quote and the candle series actually come from.

    A provider can aggregate two upstreams (XAUS serves a spot quote from
    gold-api.com and OHLCV candles from its exchange chart feed), so the two
    prices are labelled separately instead of being presented as one feed.
    """
    provider_id = _provider_id(symbol)
    quote = quote or {}
    quote_source = (
        quote.get("price_source")
        or _source_detail(symbol, "quote_source_name")
        or quote.get("provider")
        or provider_id
    )
    return {
        "provider": provider_id,
        "quote": {
            "provider": provider_id,
            "source": quote_source,
            "detail": _source_detail(symbol, "quote_source_detail"),
            "as_of": quote.get("timestamp"),
        },
        "candles": {
            "provider": provider_id,
            "detail": _source_detail(symbol, "candle_source_detail"),
        },
    }


def _age_seconds(timestamp: Any, now: datetime) -> float | None:
    if not timestamp:
        return None
    try:
        return round((now - _parse_ts(timestamp)).total_seconds(), 3)
    except Exception:
        return None


def _candle_freshness(timeframe: str, rows: list[dict], now: datetime | None = None) -> dict[str, Any]:
    """Independent freshness of the OHLC candle series.

    Candle freshness is deliberately NOT derived from the quote: a fresh spot
    quote (XAUS serves a spot quote and an exchange OHLCV chart feed from two
    upstreams) can coexist with a closed/stale candle feed, and the assistant
    must be able to see that. A series is STALE once its newest bar is older than
    four of its own intervals, matching the strategy engine's threshold.
    """
    now = now or _now()
    bar_seconds = TIMEFRAME_SECONDS.get(timeframe)
    latest_ts = rows[-1].get("timestamp") if rows else None
    age = _age_seconds(latest_ts, now)
    threshold = (bar_seconds * 4) if bar_seconds else None
    if age is None or threshold is None:
        status = "UNAVAILABLE"
    elif age <= threshold:
        status = "FRESH"
    else:
        status = "STALE"
    return {
        "status": status,
        "state": f"CANDLES_{status}" if status in {"FRESH", "STALE"} else f"DATA_{status}",
        "latest_candle_timestamp": latest_ts,
        "age_seconds": age,
        "bar_seconds": bar_seconds,
        "stale_threshold_seconds": threshold,
        "candle_count": len(rows),
    }


def _quote_freshness(symbol: str, quote: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    """Freshness of the quote, reported separately from candle freshness."""
    now = now or _now()
    price = quote.get("price") if quote else None
    stamp = quote.get("timestamp") if quote else None
    age = _age_seconds(stamp, now)
    flagged_stale = bool(quote.get("is_stale")) if quote else False
    if price is None:
        status = "UNAVAILABLE"
    elif flagged_stale:
        status = "STALE"
    elif age is not None and 0 <= age <= LIVE_FRESH_SECONDS:
        status = "FRESH"
    else:
        status = "STALE"
    return {
        "status": status,
        "state": f"QUOTE_{status}" if status in {"FRESH", "STALE"} else f"DATA_{status}",
        "price": price,
        "timestamp": stamp,
        "age_seconds": age,
        "provider_flagged_stale": flagged_stale,
        "source": (quote or {}).get("price_source") or (quote or {}).get("provider"),
    }


def _price_diagnostics(symbol: str, timeframe: str, limit: int = 2) -> dict[str, Any]:
    """Provenance snapshot for one instrument+timeframe: quote vs candle series.

    Written to the ``trademind.market`` logger and returned by the diagnostics
    endpoint. It reports *where* each number came from and how old it is - never
    a key, token or secret - so a quote/candle disagreement can be traced
    without guessing which component is wrong.
    """
    now = _now()
    status = market_data.status(symbol)
    configured = bool(status.get("configured"))
    quote = _status_quote(symbol, configured)
    if quote is None or quote.get("price") is None:
        # Nothing in the cache yet (a fresh process has not served a quote
        # request): fetch one live so the diagnostics show a real quote/candle
        # comparison. Failure is tolerated - the field is simply left empty.
        getter = getattr(market_data, "get_latest_quote", None)
        if getter is not None:
            try:
                quote = dict(getter(symbol, timeframe))
            except Exception:
                quote = quote
    rows, warnings = _candles(symbol, timeframe, limit=max(1, min(int(limit), 10)))
    latest = rows[-1] if rows else None
    bar_seconds = TIMEFRAME_SECONDS.get(timeframe)
    latest_ts = latest.get("timestamp") if latest else None
    forming: bool | None = None
    if latest_ts and bar_seconds:
        try:
            forming = _parse_ts(latest_ts).timestamp() + bar_seconds > now.timestamp()
        except Exception:
            forming = None
    quote_price = quote.get("price") if quote else None
    chart_close = latest.get("close") if latest else None
    difference = None
    if quote_price is not None and chart_close is not None:
        difference = round(float(quote_price) - float(chart_close), 4)
    diagnostics = {
        "symbol": symbol,
        "timeframe": timeframe,
        "provider": status.get("provider"),
        "configured": configured,
        "data_status": _data_status(symbol),
        "sources": _data_sources(symbol, quote),
        "quote": {
            "price": quote_price,
            "timestamp": quote.get("timestamp") if quote else None,
            "age_seconds": _age_seconds(quote.get("timestamp") if quote else None, now),
            "is_stale": quote.get("is_stale") if quote else None,
            "upstream_state": quote.get("provider_data_state") if quote else None,
        },
        "candle": {
            "open_time": latest_ts,
            "close_price": chart_close,
            "age_seconds": _age_seconds(latest_ts, now),
            "bar_seconds": bar_seconds,
            "is_forming": forming,
            "state": None if forming is None else ("forming" if forming else "closed"),
        },
        "chart_series_last": {
            "timestamp": latest_ts,
            "close": chart_close,
            "count": len(rows),
        },
        "quote_minus_chart_close": difference,
        "sources_differ": difference is not None and abs(difference) > 0.005,
        "checked_at_utc": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "warnings": warnings,
    }
    market_logger.info(
        "market diagnostics provider=%s symbol=%s timeframe=%s quote=%s@%s age=%ss "
        "candle_open=%s close=%s age=%ss state=%s sources_differ=%s",
        diagnostics["provider"], symbol, timeframe,
        quote_price, diagnostics["quote"]["timestamp"], diagnostics["quote"]["age_seconds"],
        latest_ts, chart_close, diagnostics["candle"]["age_seconds"],
        diagnostics["candle"]["state"], diagnostics["sources_differ"],
    )
    return diagnostics


# ---------------------------------------------------------------------------
# Timestamp provenance diagnostics
# ---------------------------------------------------------------------------
#
# Everything below is derived from what the provider actually returned at
# request time. Nothing is assumed from documentation, naming or convention, so
# that a quote/candle disagreement - or a wrong-looking chart date - can be
# traced to a specific stage instead of guessed at. No keys, tokens or secrets
# pass through here.


def _detect_raw_timestamp_format(value: Any) -> str:
    """Name a raw provider timestamp's wire format by inspecting the value."""
    if value is None or isinstance(value, bool):
        return "missing"
    if isinstance(value, (int, float)):
        magnitude = abs(int(value))
        if 1_000_000_000 <= magnitude < 100_000_000_000:
            return "unix_epoch_seconds"
        if 100_000_000_000 <= magnitude < 100_000_000_000_000:
            return "unix_epoch_milliseconds"
        return "unrecognised_numeric_scale"
    text = str(value).strip()
    if text[-1:] in {"Z", "z"}:
        return "iso8601_utc"
    if re.search(r"[+-]\d{2}:?\d{2}$", text):
        return "iso8601_explicit_offset"
    return "iso8601_without_offset"


def _raw_instant_to_utc(value: Any) -> datetime | None:
    """Convert one supported raw timestamp to a UTC instant, exactly once.

    Epoch seconds and milliseconds are separated by magnitude. An ISO string
    must carry an offset; a naive string is rejected rather than silently
    assumed to be UTC or local. Names, not formatted text, remain canonical.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        magnitude = abs(float(value))
        seconds = magnitude / 1000.0 if magnitude >= 100_000_000_000 else magnitude
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text[-1:] in {"Z", "z"}:
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _display_in(value: datetime | None, zone: str) -> str | None:
    """Format an instant for display only - never stored, never re-parsed."""
    if value is None:
        return None
    try:
        return value.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return None


def _source_interval(provider_id: str, timeframe: str) -> tuple[str, str]:
    """Resolve (source interval, native/aggregated) the way the service layer does."""
    plan = AGGREGATION_PLANS.get(provider_id, {}).get(timeframe)
    if plan is not None:
        source, factor = plan
        return source, f"aggregated_{factor}x_{source}"
    return timeframe, "native"


def _raw_feed_tail(provider: Any, source_timeframe: str) -> dict[str, Any] | None:
    """The newest raw point for a source interval, exactly as the feed returned it.

    Deliberately unshaped: this is the wire value, before normalisation, so the
    diagnostic can prove the conversion rather than restate its output.
    """
    native = XAUS_NATIVE_INTERVALS.get(source_timeframe)
    fetcher = getattr(provider, "_fetch_points", None)
    if getattr(provider, "provider_id", None) != "XAUS" or native is None or not callable(fetcher):
        return None
    try:
        points = fetcher(source_timeframe, native, 5)
    except Exception:
        return None
    if not points:
        return None
    point = points[-1]
    return dict(point) if isinstance(point, dict) else None


def _candle_cache_state(provider: Any, symbol: str, source_timeframe: str) -> tuple[float | None, int | None]:
    """(created epoch, row count) of the provider's in-memory candle cache."""
    lock = getattr(provider, "_cache_lock", None)
    cache = getattr(provider, "_candle_cache", None)
    if lock is None or cache is None:
        return None, None
    with lock:
        entry = cache.get((symbol, source_timeframe, None, None))
    if not entry:
        return None, None
    created, rows = entry
    try:
        return float(created), len(rows)
    except (TypeError, ValueError):
        return None, None


def _candle_timestamp_diagnostics(symbol: str, timeframe: str) -> dict[str, Any]:
    """Trace one candle's instant from the raw feed to the chart series.

    Reports the requested diagnostic fields plus a ``consistency`` block that
    proves the candles endpoint, the analysis snapshot, the indicator context
    and the epoch-seconds value handed to the chart all resolve to the same
    instant. An undeterminable field is ``null`` with the reason in ``notes``.
    """
    now = _now()
    status = market_data.status(symbol)
    configured = bool(status.get("configured"))
    provider_id = status.get("provider") or "DEMO"
    provider = _provider_object(symbol)
    source_interval, kind = _source_interval(provider_id, timeframe)
    bar_seconds = TIMEFRAME_SECONDS.get(timeframe)
    notes: list[str] = []

    rows, warnings = _candles(symbol, timeframe, DEFAULT_LIMIT)
    latest = rows[-1] if rows else None
    normalized_ts = latest.get("timestamp") if latest else None
    normalized_dt = _parse_ts(normalized_ts) if normalized_ts else None

    raw_point = _raw_feed_tail(provider, source_interval) if provider is not None else None
    if raw_point is None:
        notes.append(
            "Raw wire values are only introspectable for the XAUS feed; for other providers "
            "raw_latest_timestamp is null and only the normalized series is reported."
        )
    raw_value = raw_point.get("t") if raw_point else None
    raw_dt = _raw_instant_to_utc(raw_value)
    raw_format = _detect_raw_timestamp_format(raw_value)

    intraday = bool(bar_seconds and bar_seconds < 86400)
    raw_is_forming = None
    if raw_dt is not None and intraday:
        raw_is_forming = int(raw_dt.timestamp()) % bar_seconds != 0

    cache_created, cached_rows = (
        _candle_cache_state(provider, symbol, source_interval) if provider is not None else (None, None)
    )
    cache_created_dt = (
        datetime.fromtimestamp(cache_created, tz=timezone.utc) if cache_created is not None else None
    )

    if cache_created is None:
        notes.append(
            "cache_created_at_utc is null: the active provider exposes no in-process candle cache."
        )

    quote = _status_quote(symbol, configured)
    if quote is None and configured:
        getter = getattr(market_data, "get_latest_quote", None)
        if callable(getter):
            try:
                quote = getter(symbol, timeframe)
            except Exception:
                quote = None

    # The chart library (lightweight-charts) takes epoch seconds for intraday
    # and a date for date-only frames; the useful check here is the instant.
    chart_seconds = int(normalized_dt.timestamp()) if normalized_dt is not None else None
    chart_instant = (
        datetime.fromtimestamp(chart_seconds, tz=timezone.utc) if chart_seconds is not None else None
    )

    ascending = all(
        _parse_ts(left["timestamp"]) <= _parse_ts(right["timestamp"]) for left, right in zip(rows, rows[1:])
    )

    if kind == "native" and intraday and raw_dt is not None and normalized_dt is not None:
        # The feed stamps a completed bar at its bucket start and the
        # still-forming bar at its last tick. Normalisation keeps the former and
        # drops the latter, so the newest kept bar is the bucket the raw tail
        # falls inside.
        expected = int(raw_dt.timestamp()) // bar_seconds * bar_seconds
        raw_and_normalized_agree: bool | None = chart_seconds == expected
    else:
        raw_and_normalized_agree = None
        if kind != "native":
            notes.append(
                f"{timeframe} is aggregated from the {source_interval} source series, so a raw "
                "wire timestamp is compared against that source interval rather than the derived bar."
            )
        if not intraday:
            notes.append(
                f"{timeframe} bars are stored as 00:00Z trading-date markers, so there is no intraday "
                "alignment comparison to make."
            )

    timestamp_validation_passed: bool | None = None
    if normalized_dt is None or not bar_seconds:
        timestamp_validation_passed = None
    elif intraday:
        timestamp_validation_passed = (
            normalized_dt.tzinfo is not None
            and int(normalized_dt.timestamp()) % bar_seconds == 0
            and normalized_dt <= now
            and ascending
        )
    else:
        timestamp_validation_passed = (
            normalized_dt.tzinfo is not None
            and int(normalized_dt.timestamp()) % 86400 == 0
            and normalized_dt <= now
            and ascending
        )

    # Cross-check the components that must all render the same candle instant.
    # The snapshot is built through its own endpoint path, so this is a genuine
    # end-to-end comparison rather than a restatement of one variable.
    snapshot_ts: str | None = None
    try:
        snapshot = analysis_snapshot(symbol=symbol, timeframe=timeframe)
        snapshot_ts = snapshot.get("last_successful_timestamp")
    except Exception as exc:  # pragma: no cover - diagnostics must never 500
        notes.append(f"Snapshot cross-check unavailable: {type(exc).__name__}")

    indicator_ts: str | None = None
    try:
        indicator_ts = _indicator_result(symbol, timeframe, rows, warnings=warnings).get("candle_timestamp")
    except Exception as exc:  # pragma: no cover - diagnostics must never 500
        notes.append(f"Indicator cross-check unavailable: {type(exc).__name__}")

    comparisons = {
        "candles_endpoint_timestamp": normalized_ts,
        "snapshot_last_successful_timestamp": snapshot_ts,
        "indicator_candle_timestamp": indicator_ts,
        "chart_series_epoch_seconds": chart_seconds,
        "chart_series_instant_utc": _iso_utc(chart_instant),
    }
    # Compare instants, never their representations: one component may hold an
    # ISO string while the chart is handed epoch seconds for the same moment.
    instants: set[datetime] = set()
    for candidate in (normalized_ts, snapshot_ts, indicator_ts):
        if isinstance(candidate, str):
            try:
                instants.add(_parse_ts(candidate))
            except Exception:  # pragma: no cover - defensive
                pass
    if chart_instant is not None:
        instants.add(chart_instant)
    resolved = len([v for v in (normalized_ts, snapshot_ts, indicator_ts) if isinstance(v, str)]) + (
        1 if chart_instant is not None else 0
    )
    all_agree = resolved >= 2 and len(instants) == 1

    quote_freshness = _quote_freshness(symbol, quote, now)
    candle_freshness = _candle_freshness(timeframe, rows, now)

    return {
        "symbol": symbol,
        "requested_timeframe": timeframe,
        "provider": provider_id,
        "server_now_utc": _iso_utc(now),
        "server_now_ist": _display_in(now, "Asia/Kolkata"),
        "raw_latest_timestamp": _iso_utc(raw_dt),
        "raw_latest_timestamp_value": raw_value,
        "raw_timestamp_format": raw_format,
        "raw_latest_is_forming_bar": raw_is_forming,
        "normalized_latest_timestamp_utc": normalized_ts,
        "latest_candle_open_utc": normalized_ts,
        "latest_candle_close": latest.get("close") if latest else None,
        "latest_candle_display_ist": _display_in(normalized_dt, "Asia/Kolkata"),
        "latest_candle_age_seconds": candle_freshness.get("age_seconds"),
        "latest_quote_timestamp_utc": (quote or {}).get("timestamp"),
        "latest_quote_age_seconds": quote_freshness.get("age_seconds"),
        "latest_quote_price": (quote or {}).get("price"),
        "cache_created_at_utc": _iso_utc(cache_created_dt),
        "cache_age_seconds": (
            round(now.timestamp() - cache_created, 3) if cache_created is not None else None
        ),
        "cache_row_count": cached_rows,
        "source_interval": source_interval,
        "source_interval_kind": kind,
        "bar_seconds": bar_seconds,
        "candle_count": len(rows),
        "sorted_ascending_utc": ascending,
        "normalized_from_raw_tail_agree": raw_and_normalized_agree,
        "data_is_stale": bool(candle_freshness.get("status") == "STALE"),
        "timestamp_validation_passed": timestamp_validation_passed,
        "candle_freshness": candle_freshness,
        "quote_freshness": quote_freshness,
        "consistency": {
            **comparisons,
            "components_resolved": resolved,
            "all_components_agree": all_agree,
        },
        "warnings": warnings,
        "notes": notes,
        "display_timezone": "Asia/Kolkata",
        "display_timezone_note": (
            "Timestamps are stored and compared in UTC; Timezone conversion happens once, "
            "at the display boundary."
        ),
    }

def _candles(
    symbol: str,
    timeframe: str,
    limit: int = DEFAULT_LIMIT,
    start: str | None = None,
    end: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Fetch normalized candles from the provider.

    On failure this returns an empty series plus an explicit, human-readable
    error warning. It never serves stale cache and never fabricates candles:
    indicators and BUY/SELL evaluation must not be computed from data the
    provider could not supply.
    """
    bounded = max(1, min(int(limit), MAX_LIMIT))
    try:
        if start or end:
            rows = market_data.get_historical_candles(symbol, timeframe, bounded, start, end)
        else:
            rows = market_data.get_historical_candles(symbol, timeframe, bounded)
    except ProviderError as exc:
        err_status = getattr(exc, "status", "ERROR")
        _market_data_errors[symbol] = (_now(), str(exc), err_status)
        return [], [f"Historical market data unavailable: {exc}"]
    except PriceScaleMismatchError as exc:
        _market_data_errors[symbol] = (_now(), str(exc), "PRICE_SCALE_MISMATCH")
        return [], [f"Historical market data rejected (PRICE_SCALE_MISMATCH): {exc}"]
    except (DataInvalidError, ValueError) as exc:
        _market_data_errors[symbol] = (_now(), str(exc), "DATA_INVALID")
        return [], [f"Historical market data unavailable: {exc}"]

    # Strictly validate returned candles for XAU/USD gold scale
    cached_q = getattr(market_data, "get_cached_quote", lambda s: None)(symbol)
    quote_p = float(cached_q["price"]) if cached_q and cached_q.get("price") else None
    try:
        validate_xauusd_candles(rows, timeframe=timeframe, quote_price=quote_p)
    except PriceScaleMismatchError as exc:
        _market_data_errors[symbol] = (_now(), str(exc), "PRICE_SCALE_MISMATCH")
        return [], [f"Historical market data rejected (PRICE_SCALE_MISMATCH): {exc}"]
    except (DataInvalidError, ValueError) as exc:
        _market_data_errors[symbol] = (_now(), str(exc), "DATA_INVALID")
        return [], [f"Historical market data rejected (DATA_INVALID): {exc}"]

    _market_data_errors.pop(symbol, None)
    rows = list(rows or [])
    _last_market_success[symbol] = _now()
    return rows, []


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def _indicator_result(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    *,
    data_status: str | None = None,
    provider: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    symbol = symbol.upper()
    warnings = list(warnings or [])
    data_status = data_status or _data_status(symbol)
    provider = provider or _provider_id(symbol)
    candle_timestamp = _last_successful_timestamp(candles)

    indicators: dict[str, Any] = {
        "ema9": None,
        "ema20": None,
        "ema50": None,
        "ema200": None,
        "sma20": None,
        "rsi14": None,
        "macd": {"line": None, "signal": None, "histogram": None},
        "atr14": None,
        "adx14": None,
        "bollinger20": {"middle": None, "upper": None, "lower": None},
        "vwap": None,
        "vwap_status": "UNAVAILABLE",
        "vwap_message": "VWAP unavailable: this feed does not provide suitable volume data for this instrument.",
    }
    series: dict[str, list[dict]] = {"ema20": [], "ema50": [], "vwap": []}

    if candles:
        closes = [float(c["close"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        volumes = [c.get("volume") for c in candles]
        timestamps = [c["timestamp"] for c in candles]
        session_ids = [str(c["timestamp"])[:10] for c in candles]

        volume_type = "unavailable"
        for c in candles:
            vt = c.get("volume_type")
            if vt and vt not in {"unavailable", "unverified"}:
                volume_type = vt
                break

        # A provider may return fewer candles than the longest indicator needs
        # (for example a shallow feed yields about 21 daily bars). Each indicator
        # that cannot be computed is reported as "Unavailable" with a warning
        # instead of crashing the endpoint.
        def _compute(label: str, func, *args, needs: int | None = None):
            try:
                return func(*args)
            except (ValueError, TypeError) as exc:
                if needs is not None and len(closes) < needs:
                    warnings.append(
                        f"{label} unavailable: needs at least {needs} completed "
                        f"{timeframe} candles but the feed returned {len(closes)}."
                    )
                else:
                    warnings.append(f"{label} unavailable: {exc}")
                return None

        ema9_values = _compute("EMA(9)", ema, closes, 9, needs=9)
        ema20_values = _compute("EMA(20)", ema, closes, 20, needs=20)
        ema50_values = _compute("EMA(50)", ema, closes, 50, needs=50)
        ema200_values = _compute("EMA(200)", ema, closes, 200, needs=200)
        sma20_values = _compute("SMA(20)", sma, closes, 20, needs=20)
        rsi14_values = _compute("RSI(14)", rsi, closes, 14, needs=15)
        atr14_values = _compute("ATR(14)", atr, highs, lows, closes, 14, needs=15)
        adx14_values = _compute("ADX(14)", adx, highs, lows, closes, 14, needs=27)
        macd_values = _compute("MACD(12,26,9)", macd, closes, needs=35)
        bollinger = _compute("Bollinger Bands(20,2)", bollinger_bands, closes, 20, 2.0, needs=20)

        if ema9_values:
            indicators["ema9"] = ema9_values[-1]
        if ema20_values:
            indicators["ema20"] = ema20_values[-1]
        if ema50_values:
            indicators["ema50"] = ema50_values[-1]
        if ema200_values:
            indicators["ema200"] = ema200_values[-1]
        if sma20_values:
            indicators["sma20"] = sma20_values[-1]
        if rsi14_values:
            indicators["rsi14"] = rsi14_values[-1]
        if atr14_values:
            indicators["atr14"] = atr14_values[-1]
        if adx14_values:
            indicators["adx14"] = adx14_values[-1]
        if macd_values:
            indicators["macd"] = {
                "line": macd_values["line"][-1],
                "signal": macd_values["signal"][-1],
                "histogram": macd_values["histogram"][-1],
            }
        if bollinger:
            indicators["bollinger20"] = {
                "middle": bollinger["middle"][-1],
                "upper": bollinger["upper"][-1],
                "lower": bollinger["lower"][-1],
            }

        if ema20_values:
            series["ema20"] = [
                {"time": ts, "value": value}
                for ts, value in zip(timestamps, ema20_values)
                if value is not None
            ]
        if ema50_values:
            series["ema50"] = [
                {"time": ts, "value": value}
                for ts, value in zip(timestamps, ema50_values)
                if value is not None
            ]

        has_usable_volume = volume_type in {"exchange", "tick"} and any(
            v not in (None, "") for v in volumes
        )
        if has_usable_volume:
            vwap_values = vwap(
                highs,
                lows,
                closes,
                volumes,
                session_ids=session_ids,
                volume_type=volume_type,
                timestamps=timestamps,
            )
            indicators["vwap"] = vwap_values[-1]
            indicators["vwap_status"] = (
                "AVAILABLE" if vwap_values[-1] is not None else "INSUFFICIENT_DATA"
            )
            indicators["vwap_message"] = None
            series["vwap"] = [
                {"time": ts, "value": value}
                for ts, value in zip(timestamps, vwap_values)
                if value is not None
            ]

    return {
        "symbol": symbol,
        "instrument": symbol,
        "timeframe": timeframe,
        "provider": provider,
        "data_status": data_status,
        "indicators": indicators,
        "series": series,
        "candle_timestamp": candle_timestamp,
        "calculated_at": _now().isoformat(),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Deterministic setup evaluation
# ---------------------------------------------------------------------------


def _blocked_setup(symbol: str, timeframe: str, side: str, message: str) -> dict[str, Any]:
    return _make_empty_response(
        symbol=symbol,
        timeframe=timeframe,
        side=side,
        provider=_provider_id(symbol),
        data_status=_data_status(symbol),
        status="DATA_UNAVAILABLE",
        explanation=f"Setup evaluation blocked: {message}",
        missing=["Provider candle data"],
        max_risk=_env_float("PAPER_MAX_RISK_PERCENT", 1.0),
        warnings=[message],
    )


def _evaluate_from_rows(
    symbol: str,
    timeframe: str,
    side: str,
    rows: list[dict],
    warnings: list[str],
    quote: dict[str, Any] | None = None,
    htf_candles: list[dict] | None = None,
    htf_timeframe: str | None = None,
) -> dict[str, Any]:
    data_status = _data_status(symbol)
    provider = _provider_id(symbol)
    indicator_result = _indicator_result(symbol, timeframe, rows)
    indicators = indicator_result.get("indicators") if isinstance(indicator_result, dict) else None
    structure = market_structure(rows)
    fvgs = detect_fvgs(rows, symbol, timeframe)
    order_blocks = detect_order_blocks(rows, symbol, timeframe)
    result = evaluate_trading_setup(
        symbol,
        timeframe,
        side,  # type: ignore[arg-type]
        rows,
        warnings,
        provider,
        data_status,
        min_rr=_env_float("PAPER_MIN_RR", 1.5),
        max_risk_percent=_env_float("PAPER_MAX_RISK_PERCENT", 1.0),
        equity=Decimal(str(_env_float("PAPER_EQUITY", 100000.0))),
        precomputed_indicators=indicators,
        precomputed_structure=structure,
        precomputed_fvgs=fvgs,
        precomputed_order_blocks=order_blocks,
        quote=quote,
        htf_candles=htf_candles,
        htf_timeframe=htf_timeframe,
    )
    result["provider"] = provider
    result["data_status"] = data_status
    return result


def _evaluate_setup_structured(symbol: str, timeframe: str, side: str, **_: Any) -> dict[str, Any]:
    symbol = symbol.upper()
    side = side.upper()
    try:
        rows, warnings = _candles(symbol, timeframe, DEFAULT_LIMIT)
    except (ProviderError, ValueError) as exc:
        return _blocked_setup(symbol, timeframe, side, str(exc))

    if not rows:
        msg = warnings[0] if warnings else f"Historical market data unavailable for {symbol} on {timeframe}."
        return _blocked_setup(symbol, timeframe, side, msg)

    quote = None
    try:
        quote = dict(market_data.get_latest_quote(symbol, timeframe))
    except Exception:
        cached = market_data.get_cached_quote(symbol)
        if cached:
            quote = dict(cached)

    htf_map = {
        "1m": "15m",
        "2m": "15m",
        "3m": "15m",
        "5m": "15m",
        "15m": "1h",
        "30m": "1h",
        "1h": "4h",
        "2h": "1d",
        "4h": "1d",
        "1d": "1w",
    }
    htf_tf = htf_map.get(timeframe)
    htf_candles = None
    if htf_tf and len(rows) >= 20:
        try:
            htf_candles, _ = _candles(symbol, htf_tf, 60)
        except Exception:
            htf_candles = None

    return _evaluate_from_rows(
        symbol, timeframe, side, rows, warnings, quote=quote, htf_candles=htf_candles, htf_timeframe=htf_tf
    )


# ---------------------------------------------------------------------------
# AI helpers
# ---------------------------------------------------------------------------

_AI_ERROR_CODE_ALIASES = {
    "INVALID_KEY": "invalid_credentials",
    "MODEL_UNAVAILABLE": "unsupported_model",
    "RATE_LIMITED": "rate_limited",
    "QUOTA_EXCEEDED": "quota_exceeded",
    "NETWORK_ERROR": "network_error",
    "TIMEOUT": "timeout",
    "INVALID_REQUEST": "invalid_request",
    "PROVIDER_UNAVAILABLE": "provider_unavailable",
    "EMPTY_RESPONSE": "empty_response",
    "ERROR": "provider_error",
}

_NETWORK_EXC_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectTimeout",
    "ReadTimeout",
    "ConnectError",
    "ConnectionError",
    "NetworkError",
    "TimeoutError",
}

# Exception class names shared by the OpenAI and (test) SDK shapes. Mapping these
# by name keeps the reported code stable regardless of which provider classifier
# ran, which is what the verification UI depends on.
_EXC_NAME_CODES = {
    "AuthenticationError": "invalid_credentials",
    "PermissionDeniedError": "invalid_credentials",
    "NotFoundError": "unsupported_model",
    "RateLimitError": "rate_limited",
    "InsufficientQuotaError": "quota_exceeded",
    "APIConnectionError": "network_error",
    "APITimeoutError": "timeout",
    "ConnectTimeout": "timeout",
    "ReadTimeout": "timeout",
    "InternalServerError": "provider_unavailable",
}


def _ai_error_status(exc: Exception) -> tuple[str, str]:
    """Map a provider exception to a (lowercase_code, safe_message) tuple."""
    name = type(exc).__name__
    provider = "gemini"
    try:
        provider = active_ai_config()[0] or "gemini"
    except Exception:
        provider = "gemini"

    if provider == "gemini":
        status, message = classify_gemini_error(exc)
    else:
        status, message = classify_openai_error(exc)

    if status == "ERROR" and name in _NETWORK_EXC_NAMES:
        status = "NETWORK_ERROR"

    code = _EXC_NAME_CODES.get(name) or _AI_ERROR_CODE_ALIASES.get(status, "provider_error")
    return code, message


def _openai_service_error(exc: Exception, model: str | None = None) -> AIServiceError:
    status, message = classify_openai_error(exc)
    if status == "ERROR" and type(exc).__name__ in _NETWORK_EXC_NAMES:
        status = "NETWORK_ERROR"
    return AIServiceError(
        status,
        message,
        retryable=is_retryable_error(status, exc),
        http_status=502,
        provider="openai",
        model=model,
    )


def _record_ai_failure(exc: AIServiceError) -> None:
    try:
        code = exc.as_error_payload().get("error", {}).get("code")
    except Exception:  # pragma: no cover - defensive
        code = None
    try:
        record_verification(
            exc.status,
            exc.message,
            error_code=code,
            provider=exc.provider,
            retry_after_seconds=exc.retry_after_seconds,
        )
    except Exception:  # pragma: no cover - defensive
        pass


_LIVE_REQUEST_RE = re.compile(
    r"\b(give|provide|show|get|send|fetch|pull|need|want|request)\b[^.?!\n]{0,40}\b(live|real[\s-]?time)\b",
    re.IGNORECASE,
)


def _requests_live_analysis(message: str) -> bool:
    if not message:
        return False
    return bool(_LIVE_REQUEST_RE.search(message))


def _live_data_available(symbol: str) -> bool:
    try:
        return bool(market_data.status(symbol).get("configured"))
    except Exception:
        return False


def _gemini_generate(
    prompt: str,
    *,
    history: list[ChatMessage] | None = None,
    system_instruction: str = SYSTEM_PROMPT,
) -> tuple[str, str]:
    try:
        import google.genai.types as genai_types
    except Exception as exc:  # pragma: no cover - missing optional dependency
        raise AIServiceError(
            "PROVIDER_UNAVAILABLE",
            "The Google GenAI SDK is not installed in the backend environment.",
            http_status=503,
            provider="gemini",
        ) from exc

    contents = []
    for item in history or []:
        role = getattr(item, "role", "user")
        text = (getattr(item, "text", None) or getattr(item, "content", "") or "").strip()
        if not text or role not in {"user", "assistant"}:
            continue
        contents.append(
            genai_types.Content(
                role="model" if role == "assistant" else "user",
                parts=[genai_types.Part.from_text(text=text)],
            )
        )
    contents.append(
        genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=prompt)])
    )
    config = genai_types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.2,
        max_output_tokens=1024,
    )
    text, model_used = ai_config.gemini_generate_content(
        contents,
        config,
        models=ai_config.gemini_models(),
        total_deadline=ai_config.gemini_total_deadline_seconds(),
        request_timeout_ms=ai_config.gemini_request_timeout_ms(),
    )
    return text, model_used


def _openai_generate(prompt: str, key: str, model: str) -> tuple[str, str]:
    try:
        import openai
    except Exception as exc:  # pragma: no cover - missing optional dependency
        raise AIServiceError(
            "PROVIDER_UNAVAILABLE",
            "The OpenAI SDK is not installed in the backend environment.",
            http_status=503,
            provider="openai",
        ) from exc

    client = openai.OpenAI(api_key=key, timeout=20.0)
    try:
        response = client.responses.create(model=model, input=prompt, store=False)
    except AIServiceError:
        raise
    except Exception as exc:
        raise _openai_service_error(exc, model) from exc
    text = getattr(response, "output_text", None)
    return (text if isinstance(text, str) else ""), model


def classify_chat_intent(message: str) -> str:
    """Route context by the latest user message, never by stale conversation history."""
    text = re.sub(r"\s+", " ", (message or "").strip().lower())
    if not text:
        return "general"
    if re.fullmatch(r"(?:hi|hello|hey|good morning|good afternoon|good evening|thanks|thank you|how are you)[!.?, ]*", text):
        return "greeting"
    price_question = bool(re.search(r"\b(price|quote|trading at)\b", text)) and not bool(
        re.search(r"\bprice action\b", text)
    )
    if price_question and (
        re.search(r"\b(gold|xau/?usd|current|latest|live|spot|market)\b", text)
        or re.search(r"\bwhat(?:'s| is) (?:the )?(?:current )?(?:price|quote)\b", text)
    ) or re.search(r"\bhow much is (?:gold|xau/?usd)\b", text):
        return "current_price"
    if re.search(r"\b(market|gold|xau/?usd|trading|session)\b", text) and re.search(
        r"\b(open|closed|close|hours?|next open|reopen|tradable|weekend|holiday|market status)\b", text
    ):
        return "market_status"
    if re.search(r"\bcandles?\b", text) and re.search(r"\b(fresh|stale|status|updated|age|current|latest)\b", text):
        return "candle_status"
    if re.search(r"\b(provider|feed|connection|connected|data status|data freshness|freshness|stale|quote status|candle status)\b", text):
        return "provider_status"
    if re.search(r"\b(should i|can i|is there a|evaluate|assess|valid)\b", text) and re.search(
        r"\b(buy|sell|long|short|setup|trade|entry)\b", text
    ) or re.search(r"\b(buy|sell)\s+(?:setup|signal|candidate)\b", text):
        return "setup_evaluation"
    if re.search(r"\b(analy[sz]e|analysis|review)\b", text) and re.search(r"\b(chart|market|setup|price action)\b", text):
        return "chart_analysis"
    if re.search(r"\b(current|latest|these|the)\b", text) and re.search(
        r"\b(ema|sma|rsi|macd|bollinger|atr|adx|vwap|indicator|liquidity|structure|fvg|order block)\b", text
    ):
        return "current_indicators" if re.search(r"\b(ema|sma|rsi|macd|bollinger|atr|adx|vwap|indicator)\b", text) else "current_smc"
    if re.search(r"\bthese (?:values|indicators|signals)\b", text):
        return "current_indicators"
    asks_definition = bool(re.search(r"\b(what is|what are|define|definition|explain|meaning of|how does)\b", text))
    asks_for_current_levels = bool(re.search(r"\b(my|current|now|this setup|the setup|my trade|where is|calculate)\b", text))
    if (asks_definition or re.search(r"\bwhat does\b", text)) and not asks_for_current_levels and re.search(
        r"\b(ict|smc|smart money concepts?|fvg|fair value gap|liquidity|liquidity sweep|order block|bos|choch|mss|rsi|ema|sma|macd|bollinger|atr|adx|vwap|indicator|risk.reward|stop[- ]?loss|take[- ]?profit|market structure|trading|forex|candlestick|leverage|margin|spread|slippage|market order|limit order)\b", text
    ):
        return "education"
    if re.search(r"\b(current|latest|now)\b", text) and re.search(r"\b(setup|buy|sell|long|short)\b", text):
        return "setup_evaluation"
    if re.search(r"\b(entry|stop[- ]?loss|take[- ]?profit|target|risk.reward|position size|position sizing)\b", text) and re.search(
        r"\b(current|now|my|this|setup|trade|price|level|where|what|calculate)\b", text
    ):
        return "risk_levels"
    return "general"


def _normalized_chat_history(history: list[ChatMessage], latest_message: str) -> list[ChatMessage]:
    """Bound history and remove a duplicated pending copy of the current user turn."""
    raw: list[tuple[str, str]] = []
    for item in history[-8:]:
        role = getattr(item, "role", "user")
        text = (getattr(item, "text", None) or getattr(item, "content", "") or "").strip()
        if role in {"user", "assistant"} and text:
            raw.append((role, text[:4000]))
    if raw and raw[-1] == ("user", latest_message.strip()):
        raw.pop()

    normalized: list[ChatMessage] = []
    for role, text in raw:
        if normalized and normalized[-1].role == role:
            normalized[-1].text = f"{normalized[-1].text}\n{text}"[:4000]
        else:
            normalized.append(ChatMessage(role=role, text=text[:4000]))
    return normalized[-8:]


def _build_prompt(
    message: str,
    context: dict | None,
    history: list[ChatMessage],
    *,
    intent: str,
    symbol: str,
    timeframe: str,
) -> str:
    del history  # History is sent to Gemini as role-tagged turns, never flattened here.
    parts = [
        f"Request type: {intent}. This is only a routing hint; answer the latest question itself.",
        f"Selected instrument/timeframe: {symbol} / {timeframe}.",
        "Latest user message (answer this message, not an earlier one):\n" + message,
    ]
    if context is not None:
        parts.append(
            "Relevant backend facts for this question (authoritative; use only fields present here):\n"
            + json.dumps(context, separators=(",", ":"), default=str)
        )
    knowledge = knowledge_for_query(message) if intent in {"education", "chart_analysis", "current_indicators", "current_smc", "setup_evaluation", "risk_levels"} else ""
    if knowledge:
        parts.append("Brief reference knowledge relevant to the latest question:\n" + knowledge)
    return "\n\n".join(parts)


def _setup_summary(result: dict[str, Any]) -> dict[str, Any]:
    explanation = result.get("explanation")
    if isinstance(explanation, str) and len(explanation) > 300:
        explanation = explanation[:297] + "..."
    return {
        "status": result.get("status"),
        "direction": result.get("direction"),
        "setup_type": result.get("setup_type"),
        "current_price": result.get("current_price"),
        "entry_price": result.get("entry_price"),
        "stop_loss": result.get("stop_loss"),
        "take_profit_1": result.get("take_profit_1"),
        "take_profit_2": result.get("take_profit_2"),
        "take_profit_3": result.get("take_profit_3"),
        "risk_reward_ratio": result.get("risk_reward_ratio"),
        "conditions_satisfied": list(result.get("conditions_satisfied", []))[:6],
        "conditions_not_satisfied": list(result.get("conditions_not_satisfied", []))[:6],
        "explanation": explanation,
    }


def _trim_zone(zone: dict[str, Any], keep: tuple[str, ...]) -> dict[str, Any]:
    return {key: zone.get(key) for key in keep if key in zone}


def _bounded_structure(structure: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(structure, dict):
        return {}
    return {
        "swing_highs": list(structure.get("swing_highs", []))[-8:],
        "swing_lows": list(structure.get("swing_lows", []))[-8:],
        "breaks": list(structure.get("breaks", []))[-6:],
        "liquidity_sweeps": list(structure.get("liquidity_sweeps", []))[-6:],
        "support_clusters": list(structure.get("support_clusters", []))[-5:],
        "resistance_clusters": list(structure.get("resistance_clusters", []))[-5:],
        "equal_highs": list(structure.get("equal_highs", []))[-5:],
        "equal_lows": list(structure.get("equal_lows", []))[-5:],
    }


def _premium_discount(bounds: dict | None, price: float | None) -> dict[str, Any] | None:
    """Locate price inside the dealing range (premium / equilibrium / discount).

    Pure arithmetic over the backend-computed dealing range. It reports a
    position, not a signal: being in premium does not make a sell correct.
    """
    if not bounds or price is None:
        return None
    high, low = float(bounds["high"]), float(bounds["low"])
    if high <= low:
        return None
    position = (float(price) - low) / (high - low)
    if position > 0.5:
        zone = "premium"
    elif position < 0.5:
        zone = "discount"
    else:
        zone = "equilibrium"
    return {
        "zone": zone,
        "position": round(position, 4),
        "premium_zone": [round(bounds["equilibrium"], 5), round(high, 5)],
        "discount_zone": [round(low, 5), round(bounds["equilibrium"], 5)],
        "equilibrium": round(bounds["equilibrium"], 5),
    }


def _invalidation_levels(structure: dict[str, Any], price: float | None) -> dict[str, Any] | None:
    """Nearest confirmed structural levels that would invalidate a bias.

    Uses only confirmed pivots/clusters already computed: the closest resistance
    above price and the closest support below it, plus the latest swing extremes.
    """
    if price is None:
        return None
    highs = [float(x["price"]) for x in structure.get("swing_highs", []) if x.get("price") is not None]
    lows = [float(x["price"]) for x in structure.get("swing_lows", []) if x.get("price") is not None]
    resistance = [float(x["price"]) for x in structure.get("resistance_clusters", []) if x.get("price") is not None]
    support = [float(x["price"]) for x in structure.get("support_clusters", []) if x.get("price") is not None]
    above = sorted(v for v in [*highs, *resistance] if v > price)
    below = sorted((v for v in [*lows, *support] if v < price), reverse=True)
    return {
        "nearest_resistance_above": above[0] if above else None,
        "nearest_support_below": below[0] if below else None,
        "latest_swing_high": highs[-1] if highs else None,
        "latest_swing_low": lows[-1] if lows else None,
    }


def _build_market_context(symbol: str, timeframe: str) -> dict[str, Any]:
    rows, warnings = _candles(symbol, timeframe, DEFAULT_LIMIT)
    data_status = _data_status(symbol)
    provider = _provider_id(symbol)

    if not rows:
        candle_fresh = _candle_freshness(timeframe, [])
        quote = _status_quote(symbol, bool(market_data.status(symbol).get("configured")))
        quote_fresh = _quote_freshness(symbol, quote)
        return {
            "instrument": symbol,
            "symbol": symbol,
            "timeframe": timeframe,
            "provider": provider,
            "data_status": "DATA_UNAVAILABLE" if data_status == "DEMO" else "CANDLES_UNAVAILABLE",
            "current_price": float(quote["price"]) if quote and quote.get("price") else None,
            "data_age_seconds": None,
            "quote": quote_fresh,
            "candle_data": candle_fresh,
            "data_freshness": {
                "quote_status": quote_fresh["status"],
                "candle_status": "UNAVAILABLE",
                "quote_age_seconds": quote_fresh["age_seconds"],
                "candle_age_seconds": None,
                "candle_count": 0,
                "candles_sufficient": False,
            },
            "recent_candles": [],
            "indicators": {},
            "strategy": {
                "status": "DATA_UNAVAILABLE",
                "buy_setup": {"status": "DATA_UNAVAILABLE", "explanation": "Candle data unavailable from provider"},
                "sell_setup": {"status": "DATA_UNAVAILABLE", "explanation": "Candle data unavailable from provider"},
            },
            "market_structure": {},
            "dealing_range": None,
            "premium_discount": None,
            "invalidation_levels": None,
            "fvgs": [],
            "order_blocks": [],
            "warnings": warnings[:8] or ["Historical candle data unavailable from upstream provider."],
        }

    indicator_result = _indicator_result(
        symbol, timeframe, rows, data_status=data_status, provider=provider, warnings=warnings
    )
    indicators = indicator_result.get("indicators", {})
    structure = market_structure(rows)
    fvgs = detect_fvgs(rows, symbol, timeframe)
    order_blocks = detect_order_blocks(rows, symbol, timeframe)
    bounds = dealing_range(rows)

    buy = evaluate_trading_setup(
        symbol,
        timeframe,
        "BUY",
        rows,
        warnings,
        provider,
        data_status,
        min_rr=_env_float("PAPER_MIN_RR", 1.5),
        max_risk_percent=_env_float("PAPER_MAX_RISK_PERCENT", 1.0),
        precomputed_indicators=indicators,
        precomputed_structure=structure,
        precomputed_fvgs=fvgs,
        precomputed_order_blocks=order_blocks,
    )
    sell = evaluate_trading_setup(
        symbol,
        timeframe,
        "SELL",
        rows,
        warnings,
        provider,
        data_status,
        min_rr=_env_float("PAPER_MIN_RR", 1.5),
        max_risk_percent=_env_float("PAPER_MAX_RISK_PERCENT", 1.0),
        precomputed_indicators=indicators,
        precomputed_structure=structure,
        precomputed_fvgs=fvgs,
        precomputed_order_blocks=order_blocks,
    )

    statuses = {buy.get("status"), sell.get("status")}
    if statuses & {"BUY_CANDIDATE", "SELL_CANDIDATE"}:
        primary_status = "WATCHLIST"
    elif "WATCHLIST" in statuses:
        primary_status = "WATCHLIST"
    else:
        primary_status = "NO_SETUP"

    recent_candles = [
        {
            "timestamp": c.get("timestamp"),
            "open": c.get("open"),
            "high": c.get("high"),
            "low": c.get("low"),
            "close": c.get("close"),
            "volume": c.get("volume"),
            "instrument": c.get("instrument"),
            "timeframe": c.get("timeframe"),
        }
        for c in rows[-24:]
    ]

    current_price = float(rows[-1]["close"]) if rows else None
    age_seconds = None
    if rows:
        try:
            age_seconds = int((_now() - _parse_ts(rows[-1]["timestamp"])).total_seconds())
        except Exception:
            age_seconds = None

    # Quote and candle freshness are reported independently so the assistant can
    # never mistake a fresh quote for a fresh candle series (or vice versa).
    quote = _status_quote(symbol, bool(market_data.status(symbol).get("configured")))
    quote_fresh = _quote_freshness(symbol, quote)
    candle_fresh = _candle_freshness(timeframe, rows)

    return {
        "instrument": symbol,
        "symbol": symbol,
        "timeframe": timeframe,
        "provider": provider,
        "data_status": data_status,
        "current_price": current_price,
        "data_age_seconds": age_seconds,
        "quote": quote_fresh,
        "candle_data": candle_fresh,
        "data_freshness": {
            "quote_status": quote_fresh["status"],
            "candle_status": candle_fresh["status"],
            "quote_age_seconds": quote_fresh["age_seconds"],
            "candle_age_seconds": candle_fresh["age_seconds"],
            "candle_count": candle_fresh["candle_count"],
            "candles_sufficient": candle_fresh["candle_count"] >= 50,
        },
        "recent_candles": recent_candles,
        "indicators": indicators,
        "strategy": {
            "status": primary_status,
            "buy_setup": _setup_summary(buy),
            "sell_setup": _setup_summary(sell),
        },
        "market_structure": _bounded_structure(structure),
        "dealing_range": bounds,
        "premium_discount": _premium_discount(bounds, current_price),
        "invalidation_levels": _invalidation_levels(structure, current_price),
        "fvgs": [
            _trim_zone(z, ("direction", "lower", "upper", "status", "created_at"))
            for z in fvgs[:10]
        ],
        "order_blocks": [
            _trim_zone(
                z,
                (
                    "direction",
                    "lower",
                    "upper",
                    "status",
                    "created_at",
                    "confirmed_at",
                    "mitigated",
                    "mitigation_status",
                ),
            )
            for z in order_blocks[:10]
        ],
        "warnings": warnings[:8],
    }


def _build_chat_market_context(
    intent: str, symbol: str, timeframe: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Attach only the live facts required for the latest question."""
    if intent == "market_status":
        session = market_session(symbol, provider_session=_provider_market_session(symbol))
        cached_quote = _status_quote(symbol, bool(market_data.status(symbol).get("configured")))
        context = {
            "kind": "market_session",
            "instrument": symbol,
            "market_session": session,
            "market_data": {
                "provider": _provider_id(symbol),
                "connection_status": _connection_status(symbol),
                "data_status": _data_status(symbol),
                "quote_freshness": _quote_freshness(symbol, cached_quote),
                "candle_freshness": "not queried for a session question",
            },
        }
        return context, session, market_session_summary(session)

    if intent == "current_price":
        try:
            quote = dict(market_data.get_latest_quote(symbol, timeframe))
            _last_market_success[symbol] = _now()
            error = None
        except ProviderError as exc:
            quote = None
            error = exc.message
        except Exception:
            quote = None
            error = "The market-data provider did not return a quote."
        quote_freshness = _quote_freshness(symbol, quote)
        provider = str((quote or {}).get("provider") or _provider_id(symbol))
        data_status = str((quote or {}).get("data_status") or _data_status(symbol))
        context = {
            "kind": "current_quote",
            "instrument": symbol,
            "provider": provider,
            "data_status": data_status,
            "connection_status": _connection_status(symbol),
            "quote": quote_freshness,
            "quote_currency": (quote or {}).get("quote_currency"),
            "unit": (quote or {}).get("unit"),
            "is_real_provider": provider.upper() != "DEMO",
            "quote_is_current_live": (
                provider.upper() != "DEMO"
                and data_status.upper() == "LIVE"
                and quote_freshness.get("status") == "FRESH"
            ),
            "error": error,
        }
        return context, None, None

    if intent == "candle_status":
        return _build_market_context(symbol, timeframe), None, None

    if intent == "provider_status":
        status = market_data.status(symbol)
        return {
            "kind": "provider_status",
            "instrument": symbol,
            "provider": status.get("provider") or _provider_id(symbol),
            "configured": bool(status.get("configured")),
            "connection_status": _connection_status(symbol),
            "data_status": _data_status(symbol),
            "quote_freshness": _quote_freshness(
                symbol, _status_quote(symbol, bool(status.get("configured")))
            ),
            "candle_freshness": "not checked; ask about candle freshness for candle diagnostics",
        }, None, None

    if intent in {
        "chart_analysis",
        "current_indicators",
        "current_smc",
        "setup_evaluation",
        "risk_levels",
    }:
        return _build_market_context(symbol, timeframe), None, None
    return None, None, None


def _build_setup_review_prompt(result: dict[str, Any]) -> str:
    summary = {
        "instrument": result.get("instrument") or result.get("symbol"),
        "timeframe": result.get("timeframe"),
        "side": result.get("side"),
        "status": result.get("status"),
        "decision": result.get("decision"),
        "direction": result.get("direction"),
        "strategy": result.get("strategy") or result.get("setup_type"),
        "current_price": result.get("current_price"),
        "quote_price": result.get("quote_price"),
        "entry_price": result.get("entry_price"),
        "entry_zone": result.get("entry_zone"),
        "stop_loss": result.get("stop_loss"),
        "take_profit_1": result.get("take_profit_1"),
        "take_profit_2": result.get("take_profit_2"),
        "take_profit_3": result.get("take_profit_3"),
        "risk_reward_ratio": result.get("risk_reward_ratio"),
        "data_status": result.get("data_status"),
        "market_data_status": result.get("market_data_status"),
        "supporting_indicators": result.get("supporting_indicators"),
        "conditions_satisfied": result.get("conditions_satisfied"),
        "conditions_not_satisfied": result.get("conditions_not_satisfied"),
        "invalidation_conditions": result.get("invalidation_conditions"),
    }
    return (
        "CRITICAL RULE FOR AI: You are an explainer and reviewer ONLY. "
        "The deterministic backend strategy engine is the authoritative source for all numerical "
        "prices, entries, stop-loss, take-profit targets, and risk/reward ratios. "
        "You must NEVER invent, alter, recalculate, or suggest different numerical trading levels. "
        "Do not change numeric levels under any circumstance; comment ONLY on market context, "
        "SMC/ICT confluence, structural validity, risk factors, and invalidation rules.\n"
        + json.dumps(summary, separators=(",", ":"), default=str)
    )


_AI_REVIEW_EXECUTOR = None

def _get_ai_executor():
    global _AI_REVIEW_EXECUTOR
    if _AI_REVIEW_EXECUTOR is None:
        from concurrent.futures import ThreadPoolExecutor
        _AI_REVIEW_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai_review")
    return _AI_REVIEW_EXECUTOR

def _ai_review_for_setup(result: dict[str, Any]) -> str | None:
    provider, key, model = active_ai_config()
    if not key or not model:
        return (
            "AI review unavailable: the AI provider is not configured. "
            "Deterministic levels and risk bounds are unaffected."
        )
    prompt = _build_setup_review_prompt(result)
    try:
        from concurrent.futures import TimeoutError as FutureTimeout
        def _invoke() -> str:
            if provider == "gemini":
                return _gemini_generate(prompt)[0]
            return _openai_generate(prompt, key, model)[0]

        future = _get_ai_executor().submit(_invoke)
        return future.result(timeout=30.0)
    except FutureTimeout:
        return (
            "AI review deferred: model generation exceeded 30s deadline. "
            "Deterministic levels and risk bounds remain in effect."
        )
    except AIServiceError as exc:
        _record_ai_failure(exc)
        return f"AI review unavailable: {exc.message}"
    except Exception:
        return (
            "AI review unavailable: the AI provider request failed; "
            "deterministic levels remain in effect."
        )


def _gemini_chat(
    message: str,
    context: dict | None,
    history: list[ChatMessage],
    *,
    intent: str,
    symbol: str,
    timeframe: str,
) -> tuple[str, str]:
    prompt = _build_prompt(
        message, context, history, intent=intent, symbol=symbol, timeframe=timeframe
    )
    text, model_used = _gemini_generate(prompt, history=history)
    record_verification(
        "VERIFIED",
        f"A real Google Gemini API request succeeded using {model_used}.",
        provider="gemini",
    )
    return text, model_used


def _openai_chat(
    message: str,
    context: dict | None,
    history: list[ChatMessage],
    key: str,
    model: str,
    *,
    intent: str,
    symbol: str,
    timeframe: str,
) -> tuple[str, str]:
    try:
        import openai
    except Exception as exc:  # pragma: no cover - missing optional dependency
        raise AIServiceError(
            "PROVIDER_UNAVAILABLE",
            "The OpenAI SDK is not installed in the backend environment.",
            http_status=503,
            provider="openai",
        ) from exc

    prompt = _build_prompt(
        message, context, history, intent=intent, symbol=symbol, timeframe=timeframe
    )
    input_messages = [
        {
            "role": "assistant" if item.role == "assistant" else "user",
            "content": item.text,
        }
        for item in history[-8:]
    ]
    input_messages.append({"role": "user", "content": prompt})
    client = openai.OpenAI(api_key=key, timeout=20.0)
    try:
        response = client.responses.create(
            model=model,
            instructions=SYSTEM_PROMPT,
            input=input_messages,
            store=False,
        )
    except AIServiceError:
        raise
    except Exception as exc:
        raise _openai_service_error(exc, model) from exc
    text = getattr(response, "output_text", None)
    text = text if isinstance(text, str) else ""
    record_verification(
        "VERIFIED",
        f"A real OpenAI API request succeeded using {model}.",
        provider="openai",
    )
    return text, model


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------


@app.exception_handler(AIServiceError)
async def _aiservice_error_handler(_request, exc: AIServiceError) -> JSONResponse:
    _record_ai_failure(exc)
    payload = exc.as_error_payload()
    headers = {"X-AI-Error-Code": str(payload.get("error", {}).get("code", "AI_ERROR"))}
    if exc.retry_after_seconds:
        try:
            headers["Retry-After"] = str(int(exc.retry_after_seconds))
        except (TypeError, ValueError):
            pass
    return JSONResponse(status_code=exc.http_status, content=payload, headers=headers)


# ---------------------------------------------------------------------------
# Health, instruments, market data
# ---------------------------------------------------------------------------


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    ai = get_ai_status()
    status = market_data.status("XAUUSD")
    return {
        "status": "ok",
        "ai_configured": bool(ai.get("configured")),
        "ai_verified": bool(ai.get("verified")),
        # Canonical assistant state is served by /api/v1/ai/status; health mirrors
        # it read-only so a single source of truth can be asserted anywhere.
        "ai_state": ai.get("ai_state"),
        "data_provider": status.get("provider"),
        "data_status": _data_status("XAUUSD"),
        "instrument": SUPPORTED_SYMBOLS[0],
        "live_trading_enabled": False,
    }


def _instrument_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in market_data.get_instruments():
        symbol = str(row.get("symbol", "")).upper()
        # Single-instrument deployment: never advertise a market the app cannot
        # request from a provider.
        if not symbol or symbol not in SUPPORTED_SYMBOLS:
            continue
        status = market_data.status(symbol)
        quote = _status_quote(symbol, bool(status.get("configured")))
        price = quote.get("price") if quote else None
        rows.append(
            {
                "symbol": symbol,
                "name": SUPPORTED_SYMBOL_NAME,
                "asset_class": row.get("asset_class") or "Unknown",
                "provider": status.get("provider") or row.get("provider"),
                "provider_symbol": row.get("provider_symbol"),
                "exchange": row.get("exchange"),
                "exchange_timezone": row.get("exchange_timezone") or _exchange_timezone(symbol),
                "price_precision": row.get("price_precision"),
                "base": row.get("base"),
                "quote": row.get("quote"),
                "data_status": _data_status(symbol),
                "connection_status": _connection_status(symbol),
                "configured": bool(status.get("configured")),
                # "streaming" means a live push socket is actually delivering
                # ticks - never "the quote happens to be fresh". A REST provider
                # (XAUS, TwelveData, DEMO) has no push stream, so the frontend
                # must keep polling instead of waiting for ticks that never come.
                "streaming": _connection_status(symbol) == "STREAMING",
                "last_update": _last_market_updates.get(symbol),
                "price": price,
                "quote_available": price is not None,
                "message": status.get("message") or row.get("data_status") or "DEMO",
            }
        )
    return rows


@app.get("/api/v1/instruments")
@app.get("/api/v1/market-data/instruments")
def instruments() -> list[dict[str, Any]]:
    return _instrument_rows()


def _market_data_payload(
    symbol: str,
    timeframe: str,
    rows: list[dict],
    warnings: list[str],
    *,
    provider: str | None = None,
    data_status: str | None = None,
) -> dict[str, Any]:
    """Serialise a candle series *and* its own freshness.

    ``data_status`` describes the market/quote feed (see ``_data_status``); it is
    the same status the header uses and is deliberately kept for back-compat. A
    candle series has a separate lifetime, so ``candle_freshness`` is attached
    here as well: a fresh spot quote must never make a stale OHLC series look
    live, and a caller can read the newest bar's real timestamp and age without
    inferring it from the quote.
    """
    provider = provider or _provider_id(symbol)
    data_status = data_status or _data_status(symbol)
    return {
        "symbol": symbol,
        "instrument": symbol,
        "timeframe": timeframe,
        "provider": provider,
        "data_source": provider,
        "data_status": data_status,
        "candle_freshness": _candle_freshness(timeframe, rows),
        "exchange_timezone": _exchange_timezone(symbol),
        "last_successful_timestamp": _last_successful_timestamp(rows),
        "warnings": warnings,
        "candles": rows,
    }


@app.get("/api/v1/market-data/candles")
def market_candles(
    symbol: str = "XAUUSD",
    timeframe: str = "15m",
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    rows, warnings = _candles(symbol, timeframe, limit)
    return _market_data_payload(symbol, timeframe, rows, warnings)


@app.get("/api/v1/market-data/history")
def market_history(
    symbol: str = "XAUUSD",
    timeframe: str = "15m",
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    rows, warnings = _candles(symbol, timeframe, limit, start, end)
    return _market_data_payload(symbol, timeframe, rows, warnings)


@app.get("/api/v1/market-data/quote")
def market_quote(symbol: str = "XAUUSD", timeframe: str = "15m") -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    try:
        quote = dict(market_data.get_latest_quote(symbol, timeframe))
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _last_market_success[symbol] = _now()
    quote["data_status"] = _data_status(symbol)
    return quote


def _market_status_message(
    symbol: str,
    connection_status: str,
    stream: dict[str, Any],
) -> str:
    """Honest, human-readable market-data message shared by the status and
    session endpoints.

    The provider exposes a static placeholder ("realtime stream has not been
    verified") that must never be the last word once a real request outcome is
    known, and must never contradict the resolved connection state. Recent
    provider errors keep their concrete message.
    """
    # The shared upstream stream is a single connection (free plan = 1). When the
    # provider reports its limit is reached, say so honestly - including the live
    # connection count when the provider exposes it.
    if stream.get("limit_reached"):
        active, maximum = stream.get("active_conn"), stream.get("max_conn")
        detail = f" (active {active} of {maximum})" if active is not None and maximum is not None else ""
        return (
            "SiftingIO stream connection limit reached" + detail
            + ". Waiting to reconnect safely; historical candles remain available."
        )
    if connection_status == "STREAMING":
        return "Realtime price updates are being received from the provider stream."
    if connection_status == "CONNECTED":
        return "Provider reachable: the latest data request succeeded."
    if connection_status == "CONFIGURED":
        return "Credentials are configured; no provider request has been recorded yet."
    error = _market_data_errors.get(symbol)
    if error and connection_status in {"QUOTA_EXCEEDED", "RATE_LIMITED", "ERROR", "STALE"}:
        return error[1]
    return market_data.status(symbol).get("message") or ""


@app.get("/api/v1/market-data/status")
def market_status(symbol: str = "XAUUSD") -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    status = market_data.status(symbol)
    data_status = _data_status(symbol)

    price = None
    last_data_timestamp = None
    freshness_seconds = None
    # Rendering status must never spend a REST credit: use the in-memory quote
    # cache for configured providers (DEMO stays free). A status poll therefore
    # cannot exhaust credits on its own. Connection state comes from the real
    # candle/quote request outcomes recorded elsewhere.
    quote = _status_quote(symbol, bool(status.get("configured")))
    if quote:
        price = quote.get("price")
        last_data_timestamp = quote.get("timestamp")
    if last_data_timestamp:
        try:
            freshness_seconds = int((_now() - _parse_ts(last_data_timestamp)).total_seconds())
        except Exception:
            freshness_seconds = None

    connection_status = _connection_status(symbol)
    quote_status = _quote_freshness(symbol, quote)["status"]
    # The shared upstream stream is a single connection (free plan = 1). When the
    # provider reports its limit is reached, say so honestly - including the live
    # connection count when the provider exposes it.
    stream = _stream_info()
    message = _market_status_message(symbol, connection_status, stream)

    return {
        "symbol": symbol,
        "provider": status.get("provider"),
        "configured": bool(status.get("configured")),
        "connection_status": connection_status,
        "data_status": data_status,
        # Streaming requires a live push socket ("STREAMING"), not merely a fresh
        # REST quote ("LIVE"): a REST-only provider must never tell the frontend
        # to stop polling for quotes/candles.
        "streaming": connection_status == "STREAMING",
        "last_update": _last_market_updates.get(symbol),
        "last_data_timestamp": last_data_timestamp,
        "freshness_seconds": freshness_seconds,
        "price": price,
        "quote_available": price is not None,
        "quote_status": quote_status,
        "quote_source": (quote or {}).get("price_source") if quote else None,
        "sources": _data_sources(symbol, quote),
        "available_timeframes": _available_timeframes(symbol),
        "timeframe_note": _timeframe_note(symbol),
        "message": message,
        "stream": stream,
        "retryable": not bool(stream.get("limit_reached")),
    }


@app.get("/api/v1/diagnostics/candles")
def diagnose_candles(symbol: str = "XAUUSD", timeframe: str = "5m") -> dict[str, Any]:
    """Prove where one candle's timestamp comes from, end to end.

    Traces the raw provider value through UTC normalisation to the value the
    chart is handed, reports the real candle and quote ages separately, and
    shows whether the candles endpoint, the analysis snapshot, the indicator
    context and the chart series all agree on the same instant. Read-only and
    secret-free; a field that cannot be determined is ``null`` with the reason in
    ``notes``.
    """
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    return _candle_timestamp_diagnostics(symbol, timeframe)


@app.get("/api/v1/market-data/diagnostics")
def market_diagnostics(symbol: str = "XAUUSD", timeframe: str = "15m") -> dict[str, Any]:
    """Trace the quote vs candle provenance for one instrument+timeframe.

    Returns the same snapshot that is written to the ``trademind.market``
    logger: provider, requested symbol/timeframe, latest quote (price, time,
    age), latest candle (open time, close, age, forming/closed) and whether the
    quote and candle series come from different upstream feeds. No keys, tokens
    or secrets are included.
    """
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    return _price_diagnostics(symbol, timeframe)


@app.get("/api/v1/indicators")
def indicators_endpoint(symbol: str = "XAUUSD", timeframe: str = "15m") -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    rows, warnings = _candles(symbol, timeframe, DEFAULT_LIMIT)
    return _indicator_result(symbol, timeframe, rows, warnings=warnings)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _analysis_base(symbol: str, timeframe: str, rows: list[dict], warnings: list[str]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "instrument": symbol,
        "timeframe": timeframe,
        "provider": _provider_id(symbol),
        "data_status": _data_status(symbol),
        "candle_timestamp": _last_successful_timestamp(rows),
        "warnings": warnings,
    }


@app.get("/api/v1/analysis/structure")
def analysis_structure(symbol: str = "XAUUSD", timeframe: str = "15m") -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    rows, warnings = _candles(symbol, timeframe, DEFAULT_LIMIT)
    structure = market_structure(rows)
    return {**_analysis_base(symbol, timeframe, rows, warnings), **structure}


@app.get("/api/v1/analysis/fvgs")
def analysis_fvgs(symbol: str = "XAUUSD", timeframe: str = "15m") -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    rows, warnings = _candles(symbol, timeframe, DEFAULT_LIMIT)
    zones = detect_fvgs(rows, symbol, timeframe)
    return {
        **_analysis_base(symbol, timeframe, rows, warnings),
        "zones": zones,
        "rule": (
            "A fair value gap is a three-candle imbalance: the wick range left between "
            "candle one and candle three that price has not yet fully retraced."
        ),
    }


@app.get("/api/v1/analysis/order-blocks")
def analysis_order_blocks(symbol: str = "XAUUSD", timeframe: str = "15m") -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    rows, warnings = _candles(symbol, timeframe, DEFAULT_LIMIT)
    zones = detect_order_blocks(rows, symbol, timeframe)
    methodology = {**ORDER_BLOCK_METHODOLOGY, "boundary": "full", "break_window": 3}
    return {
        **_analysis_base(symbol, timeframe, rows, warnings),
        "zones": zones,
        "rule": (
            "A block is confirmed when an aligned BOS/CHoCH from an already-confirmed "
            "pivot occurs on the displacement candle or within the break window."
        ),
        "methodology": methodology,
    }


@app.get("/api/v1/analysis/snapshot")
def analysis_snapshot(symbol: str = "XAUUSD", timeframe: str = "15m") -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    rows, warnings = _candles(symbol, timeframe, CHART_HISTORY_LIMIT)
    provider = _provider_id(symbol)
    data_status = _data_status(symbol)
    indicator_result = _indicator_result(
        symbol, timeframe, rows, data_status=data_status, provider=provider, warnings=warnings
    )
    # Surface indicator availability on the snapshot itself so the UI can explain
    # why an indicator reads "Unavailable" (for example EMA-50/MACD on a
    # daily-only plan with about 21 bars) instead of showing a blank value.
    warnings = list(dict.fromkeys([*warnings, *indicator_result.get("warnings", [])]))
    structure = market_structure(rows)
    fvgs = detect_fvgs(rows, symbol, timeframe)
    order_blocks = detect_order_blocks(rows, symbol, timeframe)
    quote = None
    try:
        quote = dict(market_data.get_latest_quote(symbol, timeframe))
    except Exception:
        cached = market_data.get_cached_quote(symbol)
        if cached:
            quote = dict(cached)
    setup = _evaluate_from_rows(symbol, timeframe, "BUY", rows, warnings, quote=quote)
    return {
        "symbol": symbol,
        "instrument": symbol,
        "timeframe": timeframe,
        "provider": provider,
        "data_source": provider,
        "data_status": data_status,
        "quote": quote,
        "exchange_timezone": _exchange_timezone(symbol),
        "last_successful_timestamp": _last_successful_timestamp(rows),
        # Candle freshness is separate from the (quote-driven) data_status so the
        # UI can show a live quote beside a stale candle series honestly.
        "candle_freshness": _candle_freshness(timeframe, rows),
        "warnings": warnings,
        "candles": rows,
        "indicator_result": indicator_result,
        "structure": structure,
        "fvgs": fvgs,
        "order_blocks": order_blocks,
        "order_block_methodology": {
            **ORDER_BLOCK_METHODOLOGY,
            "boundary": "full",
            "break_window": 3,
        },
        "setup": setup,
    }


@app.get("/api/v1/analysis/setup")
def analysis_setup(symbol: str = "XAUUSD", timeframe: str = "15m") -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = _validate_timeframe(timeframe)
    return _evaluate_setup_structured(symbol, timeframe, "BUY")


@app.get("/api/v1/analysis/evaluate")
@app.post("/api/v1/analysis/evaluate")
async def analysis_evaluate(
    request: Request,
    symbol: str | None = None,
    timeframe: str | None = None,
    side: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
    req_symbol = str(body.get("symbol") or symbol or "XAUUSD").strip()
    req_timeframe = str(body.get("timeframe") or timeframe or "15m").strip()
    req_side = str(body.get("side") or side or "BUY").strip()

    valid_symbol = _validate_symbol(req_symbol, status_code=422)
    valid_timeframe = _validate_timeframe(req_timeframe, status_code=422)
    valid_side = _validate_side(req_side, status_code=422)

    result = _evaluate_setup_structured(valid_symbol, valid_timeframe, valid_side)
    result["ai_review"] = _ai_review_for_setup(result)
    return result


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


@app.post("/api/v1/risk/position-size")
def risk_position_size(request: PositionSizeRequest) -> dict[str, Any]:
    max_risk = _env_float("PAPER_MAX_RISK_PERCENT", 1.0)
    if request.risk_percent > max_risk:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Requested risk {request.risk_percent}% exceeds the configured "
                f"{max_risk}% maximum risk per trade. Lower risk_percent or raise "
                "PAPER_MAX_RISK_PERCENT."
            ),
        )
    side = request.side
    if side == "BUY" and request.stop_loss >= request.entry:
        raise HTTPException(
            status_code=400,
            detail="For a BUY setup the stop loss must be below the entry price.",
        )
    if side == "SELL" and request.stop_loss <= request.entry:
        raise HTTPException(
            status_code=400,
            detail="For a SELL setup the stop loss must be above the entry price.",
        )
    distance = abs(request.entry - request.stop_loss)
    if distance <= 0 or request.unit_value <= 0:
        raise HTTPException(
            status_code=400,
            detail="entry, stop_loss, and unit_value must define a positive risk distance.",
        )
    equity = Decimal(str(request.equity))
    risk_amount = equity * Decimal(str(request.risk_percent)) / Decimal("100")
    quantity = risk_amount / (Decimal(str(distance)) * Decimal(str(request.unit_value)))
    quantity = quantity.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "simulated": True,
        "risk_amount": str(_d8(risk_amount)),
        "quantity": str(quantity),
        "warning": "Simulated position sizing only; no live order is placed or routed.",
    }


# ---------------------------------------------------------------------------
# Paper (simulated) trading
# ---------------------------------------------------------------------------


def _contract_size(symbol: str) -> float:
    return float(INSTRUMENT_SPECS.get(symbol, {}).get("contract_size", 1) or 1)


def _quote_currency(symbol: str) -> str:
    spec = INSTRUMENT_SPECS.get(symbol)
    if spec and spec.get("quote_currency"):
        return str(spec["quote_currency"])
    return str(SYMBOLS.get(symbol, {}).get("quote") or "USD")


def _paper_quote(symbol: str) -> dict:
    try:
        return market_data.get_latest_quote(symbol)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _paper_fill_price(price: float, side: str) -> float:
    adjustment = price * _slippage_bps() / 10000.0
    return price + adjustment if side == "BUY" else price - adjustment


def _apply_fill(db, order: PaperOrderRecord, fill_price: float) -> None:
    symbol = order.symbol
    signed = float(order.quantity) if order.side == "BUY" else -float(order.quantity)
    existing = db.get(PaperPositionRecord, symbol)
    if existing is None:
        position = PaperPositionRecord(
            symbol=symbol,
            quote_currency=_quote_currency(symbol),
            unit_name=str(INSTRUMENT_SPECS.get(symbol, {}).get("unit_name", "units")),
            signed_quantity=_d6(signed),
            average_price=_d8(fill_price),
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            realized_pnl=_d8(0),
            fees_paid=_d8(order.fees),
            updated_at=_now(),
        )
        db.add(position)
        return
    old_qty = float(existing.signed_quantity)
    new_qty = old_qty + signed
    if abs(new_qty) < 1e-12:
        db.delete(existing)
        return
    average = (
        old_qty * float(existing.average_price) + signed * float(fill_price)
    ) / new_qty
    existing.signed_quantity = _d6(new_qty)
    existing.average_price = _d8(average)
    if order.stop_loss is not None:
        existing.stop_loss = order.stop_loss
    if order.take_profit is not None:
        existing.take_profit = order.take_profit
    existing.fees_paid = _d8(float(existing.fees_paid) + float(order.fees))
    existing.updated_at = _now()


def _close_position(db, position: PaperPositionRecord, exit_price: float, status: str) -> None:
    symbol = position.symbol
    quantity = float(position.signed_quantity)
    contract = _contract_size(symbol)
    gross = (exit_price - float(position.average_price)) * quantity * contract
    fee = abs(quantity) * contract * exit_price * _fee_rate()
    closing = PaperOrderRecord(
        id=uuid.uuid4().hex,
        symbol=symbol,
        side="SELL" if quantity > 0 else "BUY",
        quantity=_d6(abs(quantity)),
        order_type="MARKET",
        status=status,
        average_price=_d8(exit_price),
        stop_loss=None,
        take_profit=None,
        fees=_d8(fee),
        slippage=_d8(0),
        realized_pnl=_d8(gross),
        timestamp=_now(),
        label="SIMULATED",
    )
    db.add(closing)
    db.delete(position)


def _paper_process(db) -> dict[str, float | None]:
    pending = db.query(PaperOrderRecord).filter(PaperOrderRecord.status == "PENDING").all()
    open_positions = db.query(PaperPositionRecord).all()
    symbols = {order.symbol for order in pending} | {position.symbol for position in open_positions}

    quotes: dict[str, float | None] = {}
    for symbol in symbols:
        try:
            quotes[symbol] = float(market_data.get_latest_quote(symbol)["price"])
        except Exception:
            quotes[symbol] = None

    # Fill marketable resting limit orders.
    for order in pending:
        price = quotes.get(order.symbol)
        if price is None:
            continue
        entry = float(order.average_price)
        marketable = (order.side == "BUY" and price <= entry) or (
            order.side == "SELL" and price >= entry
        )
        if not marketable:
            continue
        fill_price = min(entry, price) if order.side == "BUY" else max(entry, price)
        order.status = "SIMULATED_FILLED"
        order.average_price = _d8(fill_price)
        _apply_fill(db, order, fill_price)
    db.flush()

    # Close open positions that have reached a stop or target.
    for position in db.query(PaperPositionRecord).all():
        price = quotes.get(position.symbol)
        if price is None:
            continue
        quantity = float(position.signed_quantity)
        stop = float(position.stop_loss) if position.stop_loss is not None else None
        target = float(position.take_profit) if position.take_profit is not None else None
        if quantity > 0:
            if stop is not None and price <= stop:
                _close_position(db, position, stop, "SIMULATED_STOP_LOSS_FILLED")
            elif target is not None and price >= target:
                _close_position(db, position, target, "SIMULATED_TAKE_PROFIT_FILLED")
        else:
            if stop is not None and price >= stop:
                _close_position(db, position, stop, "SIMULATED_STOP_LOSS_FILLED")
            elif target is not None and price <= target:
                _close_position(db, position, target, "SIMULATED_TAKE_PROFIT_FILLED")
    db.commit()
    return quotes


def _position_dict(position: PaperPositionRecord, mark_price: float | None) -> dict[str, Any]:
    quantity = float(position.signed_quantity)
    mark = mark_price if mark_price is not None else float(position.average_price)
    unrealized = (mark - float(position.average_price)) * quantity * _contract_size(position.symbol)
    return {
        "symbol": position.symbol,
        "side": "BUY" if quantity > 0 else "SELL",
        "quantity": f"{abs(quantity):.6f}",
        "unit": position.unit_name,
        "quote_currency": position.quote_currency,
        "average_entry": str(_d8(position.average_price)),
        "mark_price": str(_d8(mark)),
        "realized_pnl": str(_d8(position.realized_pnl)),
        "unrealized_pnl": str(_d8(unrealized)),
        "stop_loss": str(_d8(position.stop_loss)) if position.stop_loss is not None else None,
        "take_profit": str(_d8(position.take_profit)) if position.take_profit is not None else None,
        "label": "SIMULATED",
    }


def _paper_account(db, quotes: dict[str, float | None]) -> dict[str, Any]:
    orders = db.query(PaperOrderRecord).order_by(PaperOrderRecord.timestamp).all()
    positions = db.query(PaperPositionRecord).all()
    order_dicts = [order.as_dict() for order in orders]
    position_dicts = [_position_dict(position, quotes.get(position.symbol)) for position in positions]

    currencies = {_quote_currency(order.symbol) for order in orders}
    currencies |= {position.quote_currency for position in positions}

    initial = _initial_balance()
    bps = _slippage_bps()
    accounts: dict[str, Any] = {}
    for currency in sorted(currencies):
        realized = sum(
            float(order.realized_pnl)
            for order in orders
            if _quote_currency(order.symbol) == currency
        )
        fees = sum(
            float(order.fees) for order in orders if _quote_currency(order.symbol) == currency
        )
        unrealized = sum(
            (float(quotes.get(position.symbol) or position.average_price) - float(position.average_price))
            * float(position.signed_quantity)
            * _contract_size(position.symbol)
            for position in positions
            if position.quote_currency == currency
        )
        equity = initial + realized + unrealized - fees
        accounts[currency] = {
            "quote_currency": currency,
            "initial_balance": str(_d8(initial)),
            "equity": str(_d8(equity)),
            "available_simulated_funds": str(_d8(equity)),
            "realized_pnl": str(_d8(realized)),
            "unrealized_pnl": str(_d8(unrealized)),
            "fees": str(_d8(fees)),
            "slippage_model_bps": str(bps),
        }

    return {
        "label": "SIMULATED",
        "orders": order_dicts,
        "positions": position_dicts,
        "accounts_by_currency": accounts,
        "fees_model": {"rate": str(_fee_rate()), "slippage_bps": str(bps)},
        "accounting_status": "SIMULATED",
    }


@app.post("/api/v1/paper/orders")
def create_paper_order(request: PaperOrderRequest) -> dict[str, Any]:
    symbol = _validate_symbol(request.symbol)
    side = request.side
    quote = _paper_quote(symbol)
    price = float(quote["price"])

    if request.stop_loss is not None:
        stop = float(request.stop_loss)
        if side == "BUY" and stop >= price:
            raise HTTPException(
                status_code=400,
                detail="For a BUY order the stop loss must be below the current price.",
            )
        if side == "SELL" and stop <= price:
            raise HTTPException(
                status_code=400,
                detail="For a SELL order the stop loss must be above the current price.",
            )
    if request.take_profit is not None:
        target = float(request.take_profit)
        if side == "BUY" and target <= price:
            raise HTTPException(
                status_code=400,
                detail="For a BUY order the take profit must be above the current price.",
            )
        if side == "SELL" and target >= price:
            raise HTTPException(
                status_code=400,
                detail="For a SELL order the take profit must be below the current price.",
            )

    quantity = Decimal(str(request.quantity))
    contract = _contract_size(symbol)

    if request.order_type == "LIMIT":
        order = PaperOrderRecord(
            id=uuid.uuid4().hex,
            symbol=symbol,
            side=side,
            quantity=_d6(quantity),
            order_type="LIMIT",
            status="PENDING",
            average_price=_d8(request.entry_price),
            stop_loss=_d8(request.stop_loss) if request.stop_loss is not None else None,
            take_profit=_d8(request.take_profit) if request.take_profit is not None else None,
            fees=_d8(0),
            slippage=_d8(0),
            realized_pnl=_d8(0),
            timestamp=_now(),
            label="SIMULATED",
        )
        with SessionLocal() as db:
            db.add(order)
            db.commit()
            return order.as_dict()

    fill_price = _paper_fill_price(price, side)
    slippage = abs(fill_price - price) * float(quantity) * contract
    fees = float(quantity) * contract * fill_price * _fee_rate()
    order = PaperOrderRecord(
        id=uuid.uuid4().hex,
        symbol=symbol,
        side=side,
        quantity=_d6(quantity),
        order_type="MARKET",
        status="SIMULATED_FILLED",
        average_price=_d8(fill_price),
        stop_loss=_d8(request.stop_loss) if request.stop_loss is not None else None,
        take_profit=_d8(request.take_profit) if request.take_profit is not None else None,
        fees=_d8(fees),
        slippage=_d8(slippage),
        realized_pnl=_d8(0),
        timestamp=_now(),
        label="SIMULATED",
    )
    with SessionLocal() as db:
        db.add(order)
        db.commit()
        _apply_fill(db, order, fill_price)
        db.commit()
        return order.as_dict()


@app.get("/api/v1/paper/history")
def paper_history() -> dict[str, Any]:
    with SessionLocal() as db:
        quotes = _paper_process(db)
        return _paper_account(db, quotes)


# ---------------------------------------------------------------------------
# Broker / integration catalogues and market session
# ---------------------------------------------------------------------------


@app.get("/api/v1/brokers")
def brokers() -> list[dict[str, Any]]:
    international = os.getenv("INTERNATIONAL_DATA_PROVIDER", "").strip().upper()
    key_present = bool(
        (
            os.getenv("INTERNATIONAL_DATA_API_KEY")
            or os.getenv("SIFTING_API_KEY")
            or os.getenv("TWELVE_DATA_API_KEY", "")
        ).strip()
    )
    xaus_selected = international in {"XAUS", "XAUS.COM", "XAUSCOM", "XAUS_API"}
    gold_configured = key_present and international in {"GOLD", "GOLDPRICE", "GOLDPRICE_DEV", "GOLD_API"}
    xaus_configured = international in {"XAUS", "XAUS.COM", "XAUSCOM", "XAUS_API"}
    return [
        {
            "name": "DEMO Market Data",
            "integration_type": "market_data",
            "authentication": "none",
            "supported_instruments": list(SUPPORTED_SYMBOLS),
            "market_data_operations": ["historical_candles", "latest_quote"],
            "account_operations": [],
            "order_operations": [],
            "required_configuration": [],
            "status": "CONNECTED",
            "documentation_url": None,
            "live_execution": False,
        },
        {
            "name": "xaus.com (XAUS Gold API)",
            "integration_type": "market_data",
            "authentication": "none",
            "supported_instruments": list(SUPPORTED_SYMBOLS),
            "market_data_operations": ["historical_candles", "latest_quote"],
            "account_operations": [],
            "order_operations": [],
            "required_configuration": ["INTERNATIONAL_DATA_PROVIDER=xaus"],
            "status": "CONNECTED" if xaus_configured else "NOT_CONFIGURED",
            "documentation_url": "https://xaus.com/api/",
            "live_execution": False,
        },
        {
            "name": "goldprice.dev (Gold API)",
            "integration_type": "market_data",
            "authentication": "api_key",
            "supported_instruments": list(SUPPORTED_SYMBOLS),
            "market_data_operations": ["historical_candles", "latest_quote"],
            "account_operations": [],
            "order_operations": [],
            "required_configuration": [
                "INTERNATIONAL_DATA_PROVIDER=gold",
                "INTERNATIONAL_DATA_API_KEY",
            ],
            "status": "CONFIGURED" if gold_configured else "NOT_CONFIGURED",
            "documentation_url": "https://goldprice.dev/docs",
            "live_execution": False,
        },
    ]


@app.get("/api/v1/integrations/tradingview")
def tradingview_integration() -> dict[str, Any]:
    return {
        "status": "NOT_CONFIGURED",
        "integration_type": "charting_widget",
        "chart_widget_enabled": False,
        "account_authenticated": False,
        "account_data_access": False,
        "capabilities": [
            "Embeddable chart widget for visual reference only",
            "Follows the instrument/timeframe selected in TradeMind AI",
        ],
        "limitations": [
            "No TradingView account authentication is performed.",
            "No TradingView account data is accessed or stored.",
            "No order routing or trading through TradingView.",
        ],
        "documentation_url": "https://www.tradingview.com/widget-docs/",
        "link_url": "https://www.tradingview.com/chart/",
    }


def _provider_market_session(symbol: str) -> dict[str, Any] | None:
    """Return the provider's documented market-session payload, if it has one.

    SiftingIO publishes a market-hours catalog for its 23 venues (equity
    exchanges, forex and crypto), but that catalog has no commodities/metals
    market, so XAUUSD has no documented session and this stays ``None``. A
    provider that *does* expose documented session data is preferred and its
    session is then reported as provider-verified.
    """

    try:
        provider = market_data.provider_for(symbol)
    except ProviderError:
        return None
    getter = getattr(provider, "get_market_session", None)
    if not callable(getter):
        return None
    try:
        return getter(symbol) or None
    except Exception:
        return None


@app.get("/api/v1/market/session")
def market_session_endpoint(symbol: str = "XAUUSD") -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    session = market_session(symbol, provider_session=_provider_market_session(symbol))
    status = market_data.status(symbol)
    quote = _status_quote(symbol, bool(status.get("configured")))
    price_available = bool(quote and quote.get("price") is not None)
    last_data_timestamp = quote.get("timestamp") if quote else None
    freshness_seconds = None
    if last_data_timestamp:
        try:
            freshness_seconds = int((_now() - _parse_ts(last_data_timestamp)).total_seconds())
        except Exception:
            freshness_seconds = None
    # Use the same honest, resolved connection state as /market-data/status - the
    # provider's static "DISCONNECTED" placeholder must never leak into a payload
    # for an instrument that is genuinely reachable.
    stream = _stream_info()
    connection_status = _connection_status(symbol)
    data_status = _data_status(symbol)
    data = {
        "provider": status.get("provider"),
        "configured": bool(status.get("configured")),
        "connection_status": connection_status,
        "data_status": data_status,
        "streaming": connection_status == "STREAMING",
        "quote_available": price_available,
        "last_update": _last_market_updates.get(symbol),
        "last_data_timestamp": last_data_timestamp,
        "freshness_seconds": freshness_seconds,
        "message": _market_status_message(symbol, connection_status, stream),
    }
    return {
        "symbol": symbol,
        # Required session contract (STEP 3): a single, consistent shape the UI
        # can rely on. ``session`` carries the full deterministic detail.
        "session_status": session.get("status"),
        "source": session.get("session_source"),
        "checked_at": session.get("utc_time"),
        "next_open": session.get("next_open"),
        "next_close": session.get("next_close"),
        "timezone": session.get("venue_timezone"),
        "explanation": session.get("reason"),
        "market_status": session.get("status"),
        "session_summary": market_session_summary(session),
        "session": session,
        "data": data,
    }


# ---------------------------------------------------------------------------
# AI endpoints
# ---------------------------------------------------------------------------


@app.get("/api/v1/ai/status")
def ai_status() -> dict[str, Any]:
    return get_ai_status()


@app.post("/api/v1/ai/verify")
def ai_verify() -> dict[str, Any]:
    return verify_ai_configuration()


@app.post("/api/v1/ai/chat")
def ai_chat(request: ChatRequest) -> dict[str, Any]:
    provider, key, model = active_ai_config()
    symbol = _validate_symbol(request.symbol or "XAUUSD")
    timeframe = _validate_timeframe(request.timeframe or "15m")
    intent = classify_chat_intent(request.message)
    history = _normalized_chat_history(request.history, request.message)

    if _requests_live_analysis(request.message) and not _live_data_available(symbol):
        raise HTTPException(
            status_code=409,
            detail=(
                "Live market analysis is unavailable: this deployment is serving "
                "synthetic DEMO market data. Configure a verified real-time provider "
                "before requesting live analysis."
            ),
        )

    if not key or not model:
        required = (
            "GEMINI_API_KEY and GEMINI_MODEL"
            if provider == "gemini"
            else "OPENAI_API_KEY and OPENAI_MODEL"
        )
        raise HTTPException(
            status_code=503,
            detail=(
                f"The {provider.capitalize()} AI provider is not configured. "
                f"Set {required} in the backend environment and run /api/v1/ai/verify."
            ),
        )

    context: dict[str, Any] | None = None
    session_info: dict[str, Any] | None = None
    session_summary: str | None = None
    if request.include_market_context:
        try:
            context, session_info, session_summary = _build_chat_market_context(
                intent, symbol, timeframe
            )
        except (ProviderError, ValueError) as exc:
            # Keep the request usable, but make the data failure explicit. The
            # model is never given old chart data as a substitute for a failed fetch.
            context = {
                "kind": intent,
                "instrument": symbol,
                "timeframe": timeframe,
                "data_status": _data_status(symbol),
                "error": getattr(exc, "message", None) or str(exc),
            }
        except Exception:
            context = {
                "kind": intent,
                "instrument": symbol,
                "timeframe": timeframe,
                "data_status": _data_status(symbol),
                "error": "Relevant backend market context could not be loaded.",
            }

    if provider == "gemini":
        text, model_used = _gemini_chat(
            request.message,
            context,
            history,
            intent=intent,
            symbol=symbol,
            timeframe=timeframe,
        )
    else:
        text, model_used = _openai_chat(
            request.message,
            context,
            history,
            key,
            model,
            intent=intent,
            symbol=symbol,
            timeframe=timeframe,
        )

    return {
        "provider": "Gemini" if provider == "gemini" else "OpenAI",
        "reply": text,
        "model": model_used,
        "data_status": _data_status(symbol),
        "symbol": symbol,
        "timeframe": timeframe,
        "intent": intent,
        "context_included": context is not None,
        "context": context,
        # Single source of truth for the assistant status: a genuinely successful
        # provider request refreshed the verification timestamp, so the same
        # canonical state is returned here instead of leaving the UI to guess.
        "ai_status": get_ai_status(),
        "market_session": session_info,
        "session_summary": session_summary,
    }


# ---------------------------------------------------------------------------
# Live market-data WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws/market-data")
async def ws_market_data(websocket: WebSocket) -> None:
    await websocket.accept()
    symbol = (websocket.query_params.get("symbol") or "XAUUSD").upper()
    if symbol not in SUPPORTED_SYMBOLS:
        await websocket.send_json({"type": "error", "message": "Unsupported instrument", "retryable": False})
        await websocket.close()
        return

    status = market_data.status(symbol)
    data_status = _data_status(symbol)
    stream = _stream_info()
    await websocket.send_json(
        {
            "type": "status",
            "symbol": symbol,
            "provider": status.get("provider"),
            "connection_status": _connection_status(symbol),
            "data_status": data_status,
            "streaming": _connection_status(symbol) == "STREAMING",
            "message": _market_status_message(symbol, _connection_status(symbol), stream),
            "stream": stream,
            "retryable": not bool(stream.get("limit_reached")),
            "quote_available": _status_quote(symbol, bool(status.get("configured"))) is not None,
        }
    )

    provider = None
    try:
        provider = market_data.provider_for(symbol)
    except ProviderError:
        provider = None

    if provider is not None and provider is not market_data.demo and market_data.supports_streaming(symbol):
        await _relay_provider_stream(websocket, symbol, provider)
        return

    # DEMO connections emit no synthetic ticks: hold the socket open until the
    # client disconnects.
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except WebSocketDisconnect:
        return
    except Exception:
        return


async def _relay_provider_stream(websocket: WebSocket, symbol: str, provider: Any) -> None:
    """Relay the shared provider stream to one browser socket.

    The upstream SiftingIO connection is a single shared stream owned by the
    provider (see ``SiftingProvider``), so this only fans one subscriber's ticks
    out to this browser - it never opens a provider connection itself. A
    disconnect watcher runs alongside the tick pump so a closed browser socket
    releases its subscription immediately instead of lingering until the next
    (possibly far-off) tick, which would pin the shared upstream open. Returns
    once the browser socket is gone.
    """

    async def pump() -> None:
        try:
            async for tick in provider.stream_quotes(symbol):
                # Guard the pipeline against a replayed/cached snapshot arriving
                # after a newer tick: keep the latest-tick store monotonic in the
                # provider's own time and never let a stale frame regress the
                # value the frontend sees. A genuine newer tick still flows
                # through untouched.
                stamp = tick.get("timestamp")
                if not _tick_is_newest(symbol, stamp):
                    continue
                _last_market_updates[symbol] = stamp
                # Retain the last validated tick so the status/quote endpoints can
                # report the latest genuine price (with its own timestamp and
                # freshness) even when no REST quote has been fetched.
                if tick.get("price") is not None:
                    _last_ticks[symbol] = tick
                await websocket.send_json(tick)
        except ProviderError as exc:
            _market_data_errors[symbol] = (_now(), str(exc), getattr(exc, "status", "ERROR"))
            try:
                await websocket.send_json(
                    {
                        "type": "error",
                        "symbol": symbol,
                        "message": str(exc),
                        "retryable": getattr(exc, "retryable", True),
                    }
                )
            except Exception:
                return
        except WebSocketDisconnect:
            return
        except Exception:
            return

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        pump_task.cancel()

