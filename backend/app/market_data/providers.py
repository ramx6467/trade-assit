"""Server-side market-data adapters. Credentials are read only from the backend env."""
from __future__ import annotations

import csv
import asyncio
import io
import json
import os
import re
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import certifi
import websockets
import websockets.exceptions

from app.config import BACKEND_ENV_FILE  # noqa: F401 - ensure backend .env is loaded
from app.market_data.aggregate import aggregate_candles
from app.market_data.demo import DemoMarketDataProvider, SYMBOLS
from app.market_data.normalize import (
    TIMEFRAME_SECONDS,
    DataInvalidError,
    PriceScaleMismatchError,
    normalize_candles,
    validate_price_scale,
    validate_xauusd_candles,
)

TIMEFRAMES = {
    "1m": ("1min", "minute"), "2m": ("2m", "2minute"), "3m": ("3min", "3minute"),
    "5m": ("5min", "5minute"), "15m": ("15min", "15minute"),
    "30m": ("30min", "30minute"), "1h": ("1h", "60minute"),
    "2h": ("2h", "2hour"),
    "4h": ("4h", "60minute"), "1d": ("1day", "day"), "1w": ("1week", "day"),
}

# Explicit Twelve Data intervals: the existing demo/Kite 3m interval is not
# supported by Twelve Data's documented time_series interval list.
TWELVE_DATA_INTERVALS = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "4h": "4h", "1d": "1day", "1w": "1week",
}

# SiftingIO interval enum (identical across every /v1/hist/* bars endpoint).
# It has no 3m or 4h interval, so those chart frames are derived honestly from a
# finer supported interval by the service layer (see AGGREGATION_PLANS).
SIFTING_INTERVALS = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "1d": "1d", "1w": "1w",
}

# Intervals a provider cannot serve directly, but which we can build honestly by
# explicitly aggregating a supported, finer interval. ``{provider: {target:
# (source, factor)}}`` The provider still rejects the derived interval itself
# (it must never silently substitute data); the aggregation happens in the
# service layer, from real source candles only.
AGGREGATION_PLANS: dict[str, dict[str, tuple[str, int]]] = {
    "TWELVE_DATA": {"3m": ("1m", 3)},
    "SIFTING": {"3m": ("1m", 3), "4h": ("1h", 4)},
    # XAUS serves 1m, 5m, 15m, 30m, 1h and 1d natively. 3m is built from 1m, and
    # 2h/4h from the native 1h series, from real XAUS candles only.
    "XAUS": {"3m": ("1m", 3), "2h": ("1h", 2), "4h": ("1h", 4)},
}


class ProviderError(ValueError):
    """Safe provider-facing error suitable for an API response."""

    def __init__(self, message: str, *, retryable: bool = False, status: str = "ERROR", code: int | None = None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status
        self.code = code


def _http_error(exc: Exception) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("Market-data provider timed out; retry shortly.", retryable=True, status="RATE_LIMITED")
    if isinstance(exc, httpx.NetworkError):
        return ProviderError("Market-data provider is unreachable; check network access.", retryable=True, status="DISCONNECTED")
    return ProviderError("Market-data provider returned an error; verify credentials, entitlement, symbol, and limits.")


class TwelveDataProvider:
    provider_id = "TWELVE_DATA"
    symbols = {"EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "XAUUSD": "XAU/USD"}

    # In-memory TTL caches to protect strict Twelve Data minute/daily credit budgets
    _candle_cache: dict[tuple, tuple[float, list[dict]]] = {}
    _quote_cache: dict[str, tuple[float, dict]] = {}
    _cache_lock = threading.Lock()
    # One lock per cache key so simultaneous identical requests share a single
    # provider call instead of each spending a credit (request deduplication).
    _inflight: dict[tuple, threading.Lock] = {}
    CANDLE_CACHE_TTL = 120.0  # 120s reuse for historical candles
    QUOTE_CACHE_TTL = 30.0    # 30s reuse for REST quotes

    def __init__(self, api_key: str):
        self.api_key = api_key

    @property
    def headers(self) -> dict[str, str]:
        # Twelve Data recommends the Authorization header. Keep credentials out
        # of request URLs and all browser-facing responses.
        return {"Authorization": f"apikey {self.api_key}", "Accept": "application/json"}

    @staticmethod
    def _error(status_code: int, payload: Any) -> ProviderError:
        payload = payload if isinstance(payload, dict) else {}
        code = payload.get("code")
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        message = str(payload.get("message", "")).lower()
        if status_code == 401 or code == 401 or any(word in message for word in ("api key", "apikey", "unauthorized", "authentication")):
            return ProviderError("Twelve Data authentication failed. Check the backend API key.", status="ERROR", code=401)
        if status_code == 429 or code == 429 or "rate limit" in message or "credit" in message:
            if any(term in message for term in ("run out of api credits", "credits were used", "credit limit", "credits", "daily limit", "quota")):
                return ProviderError(
                    "Twelve Data API credit limit reached or credits exhausted. Wait for credits to reset or upgrade your plan.",
                    retryable=False,
                    status="QUOTA_EXCEEDED",
                    code=429,
                )
            return ProviderError(
                "Twelve Data rate or API-credit limit reached (e.g. 8 requests/min). Requests are temporarily paused.",
                retryable=True,
                status="RATE_LIMITED",
                code=429,
            )
        if status_code == 403 or code == 403 or any(word in message for word in ("subscription", "plan", "entitlement", "permission", "access denied")):
            return ProviderError("Twelve Data denied this instrument or interval under the current subscription.", status="ERROR", code=403)
        if any(word in message for word in ("symbol", "instrument")):
            return ProviderError("Twelve Data does not provide this instrument or it is unavailable under the current subscription.", status="ERROR")
        if code is not None:
            return ProviderError(f"Twelve Data returned provider error {code}; check the requested data and subscription.", status="ERROR", code=code)
        return ProviderError("Twelve Data returned an unsuccessful or malformed response.", status="ERROR")

    def _get_json(self, endpoint: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            response = httpx.get(f"https://api.twelvedata.com/{endpoint}",
                                 params=params, headers=self.headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError("Twelve Data request timed out; retry shortly.", retryable=True, status="RATE_LIMITED") from exc
        except httpx.NetworkError as exc:
            raise ProviderError("Twelve Data is unreachable; check backend network access.", retryable=True, status="DISCONNECTED") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("Twelve Data request failed before a response was received.", status="ERROR") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("Twelve Data returned malformed JSON.", status="ERROR") from exc
        if not response.is_success or not isinstance(payload, dict) or payload.get("status") == "error":
            raise self._error(response.status_code, payload)
        return payload

    @staticmethod
    def _timestamp(value: Any, timeframe: str, meta: dict[str, Any]) -> str:
        if not isinstance(value, str):
            raise ProviderError("Twelve Data candle has no valid timestamp.")
        try:
            parsed = datetime.fromisoformat(value.strip().replace(" ", "T", 1).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProviderError("Twelve Data candle has an invalid timestamp.") from exc
        if parsed.tzinfo is None:
            # Intraday calls request UTC. Daily and weekly endpoints ignore the
            # timezone argument and return the instrument's exchange-local time.
            zone_name = "UTC" if timeframe not in {"1d", "1w"} else str(meta.get("exchange_timezone") or "UTC")
            try:
                parsed = parsed.replace(tzinfo=ZoneInfo(zone_name))
            except ZoneInfoNotFoundError as exc:
                raise ProviderError("Twelve Data returned an unknown exchange timezone.") from exc
        return parsed.isoformat().replace("+00:00", "Z")

    def get_instruments(self) -> list[dict[str, Any]]:
        tz_map = {"EURUSD": "UTC", "GBPUSD": "Europe/London", "XAUUSD": "America/New_York"}
        return [{"symbol": s, "provider_symbol": p, "name": s, "asset_class": "Forex / Metals",
                 "exchange": "Twelve Data composite feed", "exchange_timezone": tz_map.get(s, "UTC"),
                 "provider": self.provider_id,
                 "data_delay": "REST candle data; WebSocket freshness is reported separately",
                 "data_status": "DISCONNECTED"} for s, p in self.symbols.items()]

    def _key_lock(self, key: tuple) -> threading.Lock:
        with self._cache_lock:
            lock = self._inflight.get(key)
            if lock is None:
                lock = threading.Lock()
                self._inflight[key] = lock
            return lock

    @staticmethod
    def _slice_rows(rows: list[dict], limit: int, ranged: bool) -> list[dict]:
        if ranged:
            return [dict(r) for r in rows]
        return [dict(r) for r in rows[-limit:]]

    def _cached_rows(self, key: tuple, limit: int, ranged: bool, now_ts: float) -> list[dict] | None:
        with self._cache_lock:
            entry = self._candle_cache.get(key)
        if entry is None:
            return None
        cached_time, cached_rows = entry
        if now_ts - cached_time >= self.CANDLE_CACHE_TTL:
            return None
        # A ranged request is served in full; a plain request is served only when
        # the cached set already covers the requested depth.
        if ranged or len(cached_rows) >= limit:
            return self._slice_rows(cached_rows, limit, ranged)
        return None

    def get_historical_candles(self, symbol: str, timeframe: str, limit: int = 240,
                               start: str | None = None, end: str | None = None) -> list[dict]:
        if symbol not in self.symbols:
            raise ProviderError(f"{symbol} is not supported by Twelve Data adapter.")
        if timeframe not in TWELVE_DATA_INTERVALS:
            supported = ", ".join(TWELVE_DATA_INTERVALS)
            raise ProviderError(f"Twelve Data does not support {timeframe}; supported chart intervals are {supported}.")
        if not 1 <= limit <= 5000:
            raise ProviderError("Twelve Data outputsize must be between 1 and 5000.")

        # The cache key deliberately excludes ``limit``: the same symbol and
        # interval requested at depth 240 (evaluate/indicators/AI) and depth 1000
        # (chart snapshot) must share one provider call instead of two.
        cache_key = (symbol, timeframe, start, end)
        ranged = bool(start and end)
        now_ts = time.time()
        cached = self._cached_rows(cache_key, limit, ranged, now_ts)
        if cached is not None:
            return cached

        # Deduplicate simultaneous identical requests: a burst of callers spends
        # a single credit, and the rest reuse the freshly cached result.
        with self._key_lock(cache_key):
            now_ts = time.time()
            cached = self._cached_rows(cache_key, limit, ranged, now_ts)
            if cached is not None:
                return cached

            params: dict[str, Any] = {"symbol": self.symbols[symbol],
                                      "interval": TWELVE_DATA_INTERVALS[timeframe], "format": "JSON"}
            if timeframe not in {"1d", "1w"}:
                params["timezone"] = "UTC"
            if start:
                params["start_date"] = start
            if end:
                params["end_date"] = end
            if not ranged:
                params["outputsize"] = limit
            try:
                payload = self._get_json("time_series", params, timeout=15)
            except ProviderError:
                # If rate limited or quota exceeded, serve the last good candles
                # for this exact key rather than failing outright.
                with self._cache_lock:
                    entry = self._candle_cache.get(cache_key)
                if entry is not None:
                    return self._slice_rows(entry[1], limit, ranged)
                raise

            values = payload.get("values")
            if not isinstance(values, list) or not values:
                raise ProviderError("Twelve Data returned no candles for this symbol, interval, or subscription.")
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            candles = []
            for row in values:
                raw_vol = row.get("volume")
                volume_val = None
                volume_type = "unavailable"
                if raw_vol is not None:
                    try:
                        p_vol = float(raw_vol)
                        if p_vol > 0:
                            volume_val = p_vol
                            volume_type = "exchange"
                    except (ValueError, TypeError):
                        volume_val = None
                        volume_type = "unavailable"

                candles.append({
                    "timestamp": self._timestamp(row.get("datetime"), timeframe, meta),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": volume_val,
                    "volume_type": volume_type,
                })
            candles.sort(key=lambda row: row["timestamp"])
            normalized, _ = normalize_candles(candles, instrument=symbol, timeframe=timeframe,
                                              data_source=self.provider_id)
            with self._cache_lock:
                self._candle_cache[cache_key] = (now_ts, normalized)
            return normalized

    def get_latest_quote(self, symbol: str, timeframe: str = "15m") -> dict:
        if symbol not in self.symbols:
            raise ProviderError(f"{symbol} is not supported by Twelve Data adapter.")
        now_ts = time.time()
        with self._cache_lock:
            if symbol in self._quote_cache:
                cached_time, cached_quote = self._quote_cache[symbol]
                if now_ts - cached_time < self.QUOTE_CACHE_TTL:
                    return dict(cached_quote)

        try:
            payload = self._get_json("price", {"symbol": self.symbols[symbol]}, timeout=10)
            price = float(payload["price"])
        except (KeyError, TypeError, ValueError) as exc:
            with self._cache_lock:
                if symbol in self._quote_cache:
                    return dict(self._quote_cache[symbol][1])
            raise ProviderError("Twelve Data returned a quote without a valid price.") from exc
        except ProviderError:
            with self._cache_lock:
                if symbol in self._quote_cache:
                    return dict(self._quote_cache[symbol][1])
            raise

        if not (price > 0 and price < float("inf")):
            raise ProviderError("Twelve Data returned a non-positive or non-finite quote.")
        quote = {"symbol": symbol, "instrument": symbol, "timeframe": timeframe, "price": price,
                "timestamp": datetime.now(timezone.utc).isoformat(), "provider": self.provider_id,
                "data_source": self.provider_id, "data_status": "DELAYED", "streaming": False,
                "note": "Latest available REST price; this is polling data, not a WebSocket stream."}
        with self._cache_lock:
            self._quote_cache[symbol] = (now_ts, quote)
        return quote

    def get_cached_quote(self, symbol: str) -> dict | None:
        """Return the last quote seen in memory without any provider call.

        Status/instrument views must not spend a REST credit just to render a
        price they already have; they use this and show ``None`` when unknown.
        """
        if symbol not in self.symbols:
            return None
        with self._cache_lock:
            entry = self._quote_cache.get(symbol)
        return dict(entry[1]) if entry else None

    async def stream_quotes(self, symbol: str):
        url = "wss://ws.twelvedata.com/v1/quotes/price?apikey=" + self.api_key
        tls = ssl.create_default_context(cafile=certifi.where())
        try:
            connection = websockets.connect(url, ssl=tls, open_timeout=15, ping_interval=20, ping_timeout=20)
            async with connection as ws:
                await ws.send(json.dumps({"action": "subscribe", "params": {"symbols": self.symbols[symbol]}}))
                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=10)
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"action": "heartbeat"}))
                        continue
                    try:
                        payload = json.loads(message)
                        if payload.get("event") == "error" or payload.get("status") == "error":
                            raise self._error(int(payload.get("code") or 0), payload)
                        if payload.get("event") == "subscribe-status":
                            failures = payload.get("fails") or []
                            failed_symbol = any(isinstance(item, dict) and item.get("symbol", "").upper() == self.symbols[symbol].upper() for item in failures)
                            if payload.get("status") == "error" or failed_symbol:
                                detail = next((item for item in failures if isinstance(item, dict) and item.get("symbol", "").upper() == self.symbols[symbol].upper()), payload)
                                raise self._error(int(detail.get("code") or payload.get("code") or 0), {"message": detail.get("message", detail.get("code", "subscription rejected"))})
                            continue
                        if payload.get("event") != "price":
                            continue
                        price = float(payload["price"])
                        if not (price > 0 and price < float("inf")):
                            continue
                    except ProviderError:
                        raise
                    except (ValueError, TypeError, KeyError):
                        continue
                    # Twelve Data price events provide UNIX seconds and a price.
                    stamp = payload.get("timestamp")
                    if isinstance(stamp, (int, float)):
                        stamp = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                    elif not isinstance(stamp, str):
                        stamp = datetime.now(timezone.utc).isoformat()
                    else:
                        try:
                            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(tzinfo=timezone.utc)
                            stamp = parsed.astimezone(timezone.utc).isoformat()
                        except ValueError:
                            continue
                    yield {"type": "price", "symbol": symbol, "price": price, "timestamp": stamp,
                           "provider": self.provider_id, "data_status": "LIVE", "data_source": self.provider_id}
        except ssl.SSLCertVerificationError as exc:
            raise ProviderError("Twelve Data WebSocket TLS certificate verification failed in the backend.") from exc
        except websockets.exceptions.InvalidStatus as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", 0)
            raise self._error(status_code, {}) from exc
        except websockets.exceptions.ConnectionClosed as exc:
            reason = getattr(getattr(exc, "rcvd", None), "reason", "") or ""
            raise self._error(0, {"message": reason}) from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise ProviderError("Twelve Data WebSocket connection failed; check network access and realtime subscription entitlement.", retryable=True) from exc


class _StreamLimitReached(Exception):
    """SiftingIO refused a stream connection because the plan's limit is reached."""

    def __init__(self, safe_message: str):
        super().__init__(safe_message)
        self.safe_message = safe_message


class _StreamUnavailable(Exception):
    """The SiftingIO stream dropped and should be retried with backoff."""

    def __init__(self, safe_message: str, code: str = "stream_error"):
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.code = code


class _SiftingStreamHub:
    """A single shared SiftingIO WebSocket, multiplexed to many subscribers.

    SiftingIO's free plan allows **one** concurrent WebSocket connection. Opening
    one upstream connection per browser client, symbol, or timeframe exhausts that
    limit (the ``max_connections`` error). Instead, every consumer (the FastAPI
    ``/ws/market-data`` handler) attaches an :class:`asyncio.Queue` to this hub and
    the hub owns exactly one upstream connection:

      * one connection no matter how many symbols or browser clients are active;
      * subscriptions grouped by SiftingIO product (``fx`` for EURUSD/GBPUSD,
        ``com`` for XAUUSD) and never sent twice;
      * subscriptions restored automatically after a reconnect;
      * ``{"op":"ping"}`` every :data:`PING_INTERVAL` seconds (only client ->
        server frames reset SiftingIO's idle timer);
      * exponential reconnect backoff with a capped delay;
      * a long cooldown after a ``max_connections`` rejection so it never hammers
        the provider's connection limit;
      * clean shutdown when the last subscriber leaves or the app stops.

    The API key and the full connection URL are never logged or exposed.
    """
    PING_INTERVAL = 30.0
    BACKOFF_START = 1.0
    BACKOFF_MAX = 60.0
    LIMIT_COOLDOWN = 60.0
    QUEUE_SIZE = 2048

    def __init__(self, provider: "SiftingProvider"):
        self.provider = provider
        self._queues: dict[str, set[asyncio.Queue]] = {}
        self._task: asyncio.Task | None = None
        self._subscribed: set[str] = set()
        self._closing = False
        self._auth: dict[str, Any] = {}
        self.last_error: str | None = None
        self.last_error_code: str | None = None
        self.limit_reached = False
        self.connected = False
        # Set when a subscriber is added/removed so the connection loop wakes up
        # and reconciles subscriptions immediately instead of waiting up to a
        # full ping interval inside recv().
        self._wakeup = asyncio.Event()

    # -- diagnostics (safe: no key, no URL) ---------------------------------
    def diagnostics(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "tier": self._auth.get("tier"),
            "max_conn": self._auth.get("max_conn"),
            "max_subs": self._auth.get("max_subs"),
            "active_conn": self._auth.get("active_conn"),
            "symbols": sorted(self._queues),
            "limit_reached": self.limit_reached,
            "last_error": self.last_error,
            "last_error_code": self.last_error_code,
        }

    # ---- subscriber lifecycle (single event loop; no lock required) ----
    async def subscribe(self, symbol: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self._queues.setdefault(symbol, set()).add(queue)
        self._wakeup.set()  # reconcile a new subscription without waiting for recv
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        return queue

    async def unsubscribe(self, symbol: str, queue: asyncio.Queue) -> None:
        subs = self._queues.get(symbol)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                self._queues.pop(symbol, None)
        self._wakeup.set()  # reconcile a removed subscription promptly
        if not self._queues:
            await self._stop_task()

    async def _stop_task(self) -> None:
        task, self._task = self._task, None
        self._subscribed = set()
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def aclose(self) -> None:
        """Stop the shared connection cleanly (application shutdown)."""
        self._closing = True
        self._queues.clear()
        await self._stop_task()

    # ---- upstream connection loop ----
    async def _run(self) -> None:
        backoff = self.BACKOFF_START
        while not self._closing and self._queues:
            try:
                await self._connect_and_pump()
                backoff = self.BACKOFF_START
            except asyncio.CancelledError:
                raise
            except _StreamLimitReached as exc:
                # The plan's connection limit is the whole point of this hub:
                # wait out a long cooldown instead of reconnecting in a tight
                # loop. REST history keeps working meanwhile.
                self.connected = False
                self.limit_reached = True
                self.last_error = exc.safe_message
                self.last_error_code = "max_connections"
                await asyncio.sleep(self.LIMIT_COOLDOWN)
            except _StreamUnavailable as exc:
                self.connected = False
                self.last_error = exc.safe_message
                self.last_error_code = exc.code
                if exc.code == "auth_failed":
                    # Bad key / not entitled: retrying will not help.
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.BACKOFF_MAX)
            except Exception:  # pragma: no cover - defensive network guard
                self.connected = False
                self.last_error = "SiftingIO stream connection failed; reconnecting with backoff."
                self.last_error_code = "stream_error"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.BACKOFF_MAX)
        self.connected = False

    async def _connect_and_pump(self) -> None:
        url = f"{self.provider.WS_BASE}?key={self.provider.api_key}"
        tls = ssl.create_default_context(cafile=certifi.where())
        self._subscribed = set()
        try:
            async with websockets.connect(url, ssl=tls, open_timeout=15, ping_interval=None, max_size=2 ** 20) as ws:
                self.connected = True
                self.limit_reached = False
                # A successful (re)connect clears a stale failure so diagnostics
                # never claim the stream is still failing while it is healthy.
                self.last_error = None
                self.last_error_code = None
                last_ping = asyncio.get_event_loop().time()
                while not self._closing and self._queues:
                    await self._reconcile_subscriptions(ws)
                    now = asyncio.get_event_loop().time()
                    if now - last_ping >= self.PING_INTERVAL:
                        await ws.send(json.dumps({"op": "ping"}))
                        last_ping = now
                    raw = await self._next_frame(ws)
                    if raw is not None:
                        self._handle_frame(raw)
        except websockets.exceptions.InvalidStatus as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", 0)
            if status_code in (401, 403):
                raise _StreamUnavailable("SiftingIO rejected the stream connection (unauthorized or not entitled to this data).", "auth_failed")
            raise _StreamLimitReached("SiftingIO refused a new stream connection (connection or rate limit reached).")
        except websockets.exceptions.ConnectionClosed as exc:
            reason = getattr(getattr(exc, "rcvd", None), "reason", "") or ""
            raise _StreamUnavailable(f"SiftingIO stream closed ({reason or 'connection closed'}); reconnecting.", "closed")
        except (OSError, asyncio.TimeoutError) as exc:
            raise _StreamUnavailable("SiftingIO stream connection failed; reconnecting with backoff.", "network")
        finally:
            self.connected = False
            self._subscribed = set()

    async def _next_frame(self, ws) -> Any:
        """Wait for the next upstream frame, or return ``None`` to re-loop.

        Returns ``None`` on the ping interval (so the caller can send a
        keepalive) and as soon as a subscriber is added or removed (so a new
        symbol is subscribed at once rather than after up to one ping interval).
        Any available frame is returned untouched.
        """
        recv = asyncio.ensure_future(ws.recv())
        wake = asyncio.ensure_future(self._wakeup.wait())
        try:
            done, pending = await asyncio.wait(
                {recv, wake}, timeout=self.PING_INTERVAL,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            recv.cancel()
            wake.cancel()
            raise
        finally:
            for task in (recv, wake):
                if not task.done():
                    task.cancel()
        if self._wakeup.is_set():
            self._wakeup.clear()
        if recv.done() and not recv.cancelled():
            return recv.result()
        return None

    def _products(self, symbols) -> dict[str, set[str]]:
        groups: dict[str, set[str]] = {}
        for symbol in symbols:
            product = self.provider.stream_products.get(symbol)
            if product:
                groups.setdefault(product, set()).add(symbol)
        return groups

    async def _reconcile_subscriptions(self, ws) -> None:
        """Subscribe/unsubscribe only the delta; never duplicate a subscription."""
        wanted = set(self._queues)
        added = wanted - self._subscribed
        removed = self._subscribed - wanted
        for product, symbols in sorted(self._products(added).items()):
            await ws.send(json.dumps({"op": "subscribe", "product": product, "symbols": sorted(symbols)}))
        for product, symbols in sorted(self._products(removed).items()):
            await ws.send(json.dumps({"op": "unsubscribe", "product": product, "symbols": sorted(symbols)}))
        self._subscribed = wanted

    def _handle_frame(self, raw) -> None:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        frame = payload.get("f")
        if frame == "ack":
            if payload.get("op") == "auth":
                for key in ("tier", "max_conn", "max_subs", "active_conn"):
                    if key in payload:
                        self._auth[key] = payload[key]
            return
        if frame == "tick":
            self._route_tick(payload)
            return
        if frame == "error":
            code = str(payload.get("code") or "")
            self.last_error_code = code or None
            if code in {"max_connections", "max_subscriptions"}:
                self.limit_reached = True
                self.last_error = "SiftingIO reports the plan's stream connection/subscription limit is reached."
                raise _StreamLimitReached(self.last_error)
            if code in {"auth_failed", "auth_required", "auth_timeout"}:
                self.last_error = "SiftingIO stream authentication failed; verify INTERNATIONAL_DATA_API_KEY."
                raise _StreamUnavailable(self.last_error, "auth_failed")
            self.last_error = "SiftingIO stream reported an error."
            return
        # "pong" and any unknown frame types are ignored.

    def _route_tick(self, payload: dict) -> None:
        symbol = str(payload.get("s") or "").upper()
        queues = self._queues.get(symbol)
        if not queues:
            return
        tick = self.provider.tick_from_payload(payload, symbol)
        if tick is None:
            return
        for queue in list(queues):
            try:
                queue.put_nowait(tick)
            except asyncio.QueueFull:
                # A slow browser consumer must never stall the shared upstream
                # connection; drop only that consumer's oldest queued tick.
                try:
                    queue.get_nowait()
                    queue.put_nowait(tick)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


class SiftingProvider:
    """SiftingIO market-data adapter (REST bars/quotes + WebSocket price stream).

    Implements the documented SiftingIO REST API (https://sifting.io/docs):

      * Base URL ``https://api.sifting.io`` with an ``X-API-Key`` header.
      * Historical bars ``GET /v1/hist/{venue}/{symbol}/bars``. The heavy
        ``/hist/*`` endpoints require ``Accept-Encoding: gzip`` (HTTP 406
        ``gzip_required`` otherwise).
      * Live last trade ``GET /v1/last/trade/{venue}/{symbol}``.
      * Price stream ``wss://stream.sifting.io/ws/v1?key=...``.

    Honesty notes:

      * Bar ``v`` is the historical source's *reported tick count*, explicitly
        "not traded currency volume" per the docs. It is therefore never mapped
        to a candle's ``volume`` field, so VWAP stays honestly unavailable
        instead of being computed from a number that is not volume.
      * Bars are validated by ``normalize_candles`` and never padded; a request
        that returns no bars raises instead of inventing them.
      * Credentials stay in the backend: the REST key is sent only as a request
        header and never appears in a URL, log line, or API response.
    """
    provider_id = "SIFTING"
    # Chart symbol -> (SiftingIO REST venue, SiftingIO symbol). XAUUSD is served
    # under both the forex and commodities venues; the documented FX pair shape
    # (^[A-Za-z]{6}$) covers it and the WS "com" product carries its live tape.
    symbols = {
        "EURUSD": ("forex", "EURUSD"),
        "GBPUSD": ("forex", "GBPUSD"),
        "XAUUSD": ("commodities", "XAUUSD"),
    }
    # Chart symbol -> WebSocket product (verified against the live stream).
    stream_products = {"EURUSD": "fx", "GBPUSD": "fx", "XAUUSD": "com"}

    REST_BASE = "https://api.sifting.io"
    WS_BASE = "wss://stream.sifting.io/ws/v1"
    PAGE_LIMIT = 2000  # documented /hist/* maximum bars per page
    # A tick is only labelled LIVE while its provider timestamp is fresh. The
    # stream replays the last trade when a symbol is first subscribed, so on a
    # closed market that replay must be labelled STALE - never shown as live.
    LIVE_TICK_MAX_AGE = 90.0

    _candle_cache: dict[tuple, tuple[float, list[dict]]] = {}
    _quote_cache: dict[str, tuple[float, dict]] = {}
    _cache_lock = threading.Lock()
    # One lock per cache key so simultaneous identical requests share a single
    # provider call instead of each spending a request (request deduplication).
    _inflight: dict[tuple, threading.Lock] = {}
    CANDLE_CACHE_TTL = 120.0
    QUOTE_CACHE_TTL = 30.0

    def __init__(self, api_key: str):
        self.api_key = api_key

    @property
    def headers(self) -> dict[str, str]:
        # gzip is mandatory for the /hist/* endpoints; httpx transparently
        # decompresses the response body.
        return {"X-API-Key": self.api_key, "Accept-Encoding": "gzip", "Accept": "application/json"}

    @staticmethod
    def _error(status_code: int, payload: dict[str, Any]) -> ProviderError:
        raw_error = payload.get("error") if isinstance(payload, dict) else None
        message = payload.get("message") if isinstance(payload, dict) else None
        text = f"{raw_error or ''} {message or ''}".strip().lower()
        if status_code == 406 or "gzip_required" in text:
            return ProviderError("SiftingIO rejected the request: this endpoint requires gzip encoding (backend configuration issue).", status="ERROR")
        if status_code == 401 or "unauthorized" in text:
            return ProviderError("SiftingIO authentication failed; verify INTERNATIONAL_DATA_API_KEY.", status="ERROR", code=401)
        if status_code == 403 or "entitled" in text or "subscription" in text:
            return ProviderError("SiftingIO denied this instrument or market under the current subscription.", status="ERROR", code=403)
        if status_code == 429 or "rate_limit" in text:
            if any(word in text for word in ("quota", "exhausted", "budget")):
                return ProviderError("SiftingIO call quota is exhausted for the current plan. Wait for it to reset or upgrade.", retryable=False, status="QUOTA_EXCEEDED", code=429)
            return ProviderError("SiftingIO rate limit reached; requests are temporarily paused.", retryable=True, status="RATE_LIMITED", code=429)
        if status_code == 404:
            return ProviderError("SiftingIO does not cover this symbol, interval, or range.", status="ERROR", code=404)
        if status_code == 503 and "stale_snapshot" in text:
            return ProviderError("SiftingIO has no fresh tick for this symbol right now.", retryable=True, status="STALE", code=503)
        if status_code == 503:
            return ProviderError("SiftingIO upstream is temporarily unavailable; retry shortly.", retryable=True, status="RATE_LIMITED", code=503)
        if status_code == 400:
            return ProviderError("SiftingIO rejected the request parameters; check the symbol, interval, and date range.", status="ERROR", code=400)
        return ProviderError("SiftingIO returned an unsuccessful or malformed response.", status="ERROR")

    def _get_json(self, path: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self.REST_BASE}{path}", params=params, headers=self.headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError("SiftingIO request timed out; retry shortly.", retryable=True, status="RATE_LIMITED") from exc
        except httpx.NetworkError as exc:
            raise ProviderError("SiftingIO is unreachable; check backend network access.", retryable=True, status="DISCONNECTED") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("SiftingIO request failed before a response was received.", status="ERROR") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("SiftingIO returned malformed JSON.", status="ERROR") from exc
        if not response.is_success:
            raise self._error(response.status_code, payload if isinstance(payload, dict) else {})
        if not isinstance(payload, dict):
            raise ProviderError("SiftingIO returned a malformed response.", status="ERROR")
        return payload

    @staticmethod
    def _timestamp(value: Any) -> str:
        """Convert a SiftingIO epoch-millisecond bar/tick time to a UTC ISO instant."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProviderError("SiftingIO record has no valid timestamp.")
        try:
            dt = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise ProviderError("SiftingIO record has an invalid timestamp.") from exc
        return dt.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _optional_timestamp(value: Any) -> str | None:
        try:
            return SiftingProvider._timestamp(value)
        except ProviderError:
            return None

    @staticmethod
    def _parse_bound(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _bound_str(value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def get_instruments(self) -> list[dict[str, Any]]:
        tz_map = {"EURUSD": "UTC", "GBPUSD": "Europe/London", "XAUUSD": "America/New_York"}
        return [{"symbol": symbol, "provider_symbol": provider_symbol, "name": symbol,
                 "asset_class": "Forex / Metals", "exchange": "SiftingIO aggregated feed",
                 "exchange_timezone": tz_map.get(symbol, "UTC"), "provider": self.provider_id,
                 "data_status": "DISCONNECTED"}
                for symbol, (_venue, provider_symbol) in self.symbols.items()]

    def get_market_session(self, symbol: str) -> dict[str, Any] | None:
        """SiftingIO publishes no market-session data for spot metals.

        Its documented market-hours catalog (``GET /v1/fnd/markets``, 23 venues)
        covers equity exchanges, ``forex`` and ``crypto`` only: there is no
        commodities/metals market, and the commodities documentation states that
        live updates "follow the schedules and publication cadence of the
        underlying commodity sources" without publishing a calendar. XAUUSD
        therefore has no provider-documented schedule, so this returns ``None``
        and the session service reports ``SCHEDULE_UNKNOWN`` rather than inventing
        a gold calendar. FX and NSE keep their own documented venue conventions.
        """
        return None

    def _key_lock(self, cache_key: tuple) -> threading.Lock:
        with self._cache_lock:
            lock = self._inflight.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                self._inflight[cache_key] = lock
            return lock

    def _cached_rows(self, cache_key: tuple, limit: int, ranged: bool, now_ts: float) -> list[dict] | None:
        with self._cache_lock:
            entry = self._candle_cache.get(cache_key)
        if entry is None:
            return None
        cached_time, cached_rows = entry
        if now_ts - cached_time >= self.CANDLE_CACHE_TTL:
            return None
        if ranged or len(cached_rows) >= limit:
            return list(cached_rows) if ranged else list(cached_rows)[-limit:]
        return None

    def get_historical_candles(self, symbol: str, timeframe: str, limit: int = 240,
                               start: str | None = None, end: str | None = None) -> list[dict]:
        if symbol not in self.symbols:
            raise ProviderError(f"{symbol} is not supported by the SiftingIO adapter.")
        if timeframe not in SIFTING_INTERVALS:
            supported = ", ".join(SIFTING_INTERVALS)
            raise ProviderError(f"SiftingIO does not support {timeframe}; supported chart intervals are {supported}.")
        if not 1 <= int(limit) <= 5000:
            raise ProviderError("SiftingIO limit must be between 1 and 5000.")
        target = int(limit)
        venue, provider_symbol = self.symbols[symbol]

        # Cache key deliberately excludes ``limit`` so a shallow and a deep
        # request for the same symbol/interval/range share one provider call.
        cache_key = (symbol, timeframe, start, end)
        ranged = bool(start and end)
        now_ts = time.time()
        cached = self._cached_rows(cache_key, target, ranged, now_ts)
        if cached is not None:
            return cached

        with self._key_lock(cache_key):
            now_ts = time.time()
            cached = self._cached_rows(cache_key, target, ranged, now_ts)
            if cached is not None:
                return cached

            step = TIMEFRAME_SECONDS[timeframe]
            end_dt = self._parse_bound(end) or datetime.now(timezone.utc)
            start_dt = self._parse_bound(start)
            if start_dt is None:
                # ``start`` is required by the API and only acts as a lower bound;
                # widen it generously so weekend/holiday gaps still yield ``limit``
                # most-recent bars. The response is always capped by ``limit``.
                window = min(step * target * 8, 60 * 60 * 24 * 365 * 50)
                start_dt = end_dt - timedelta(seconds=window)
            if start_dt >= end_dt:
                raise ProviderError("SiftingIO history range is invalid: start must be before end.")

            base_params: dict[str, Any] = {
                "end": self._bound_str(end_dt),
                "interval": SIFTING_INTERVALS[timeframe],
                # Ranged history is served oldest-first (charting order); a plain
                # request asks for the most recent bars first, then we sort.
                "order": "asc" if ranged else "desc",
            }
            if ranged:
                base_params["start"] = self._bound_str(start_dt)

            rows: list[dict] = []
            cursor: str | None = None
            max_pages = max(1, (target + self.PAGE_LIMIT - 1) // self.PAGE_LIMIT)
            for _page in range(max_pages):
                page_params = dict(base_params)
                page_params["limit"] = min(self.PAGE_LIMIT, target - len(rows))
                if cursor:
                    page_params["cursor"] = cursor
                    page_params.pop("start", None)  # a cursor overrides start
                elif not ranged:
                    page_params["start"] = self._bound_str(start_dt)
                payload = self._get_json(
                    f"/v1/hist/{venue}/{provider_symbol}/bars", page_params, timeout=15
                )
                values = payload.get("data")
                if not isinstance(values, list) or not values:
                    break
                rows.extend(values)
                meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                cursor = meta.get("next_cursor") if isinstance(meta.get("next_cursor"), str) else None
                if not cursor or len(rows) >= target:
                    break

            if not rows:
                raise ProviderError("SiftingIO returned no candles for this symbol, interval, or range.")
            candles = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ProviderError("SiftingIO returned a malformed candle.")
                candles.append({
                    "timestamp": self._timestamp(row.get("t")),
                    "open": row.get("o"),
                    "high": row.get("h"),
                    "low": row.get("l"),
                    "close": row.get("c"),
                    # ``v`` is a reported tick count, never traded volume.
                    "volume": None,
                    "volume_type": "unavailable",
                })
            candles.sort(key=lambda row: row["timestamp"])
            deduped: list[dict] = []
            seen: set[str] = set()
            for row in candles:
                if row["timestamp"] in seen:
                    continue
                seen.add(row["timestamp"])
                deduped.append(row)
            normalized, _ = normalize_candles(deduped, instrument=symbol, timeframe=timeframe,
                                              data_source=self.provider_id)
            with self._cache_lock:
                self._candle_cache[cache_key] = (now_ts, normalized)
            return normalized

    def get_latest_quote(self, symbol: str, timeframe: str = "15m") -> dict:
        if symbol not in self.symbols:
            raise ProviderError(f"{symbol} is not supported by the SiftingIO adapter.")
        now_ts = time.time()
        with self._cache_lock:
            entry = self._quote_cache.get(symbol)
            if entry and now_ts - entry[0] < self.QUOTE_CACHE_TTL:
                return dict(entry[1])

        venue, provider_symbol = self.symbols[symbol]
        note = "Latest trade from the SiftingIO REST tape; polling data, not a WebSocket stream."
        payload: dict | None = None
        try:
            payload = self._get_json(f"/v1/last/trade/{venue}/{provider_symbol}", {}, timeout=10)
        except ProviderError as exc:
            if exc.status != "STALE":
                raise
            # The tape is momentarily stale (HTTP 503 stale_snapshot). Fall back
            # to the newest completed 1-minute bar close - genuine data, labelled.
            rows = self.get_historical_candles(symbol, "1m", 1)
            if not rows:
                raise
            price, stamp = float(rows[-1]["close"]), rows[-1]["timestamp"]
            note = "SiftingIO tape had no fresh tick; using the newest completed 1-minute bar close."

        if payload is not None:
            try:
                price = float(payload.get("p"))
            except (TypeError, ValueError) as exc:
                raise ProviderError("SiftingIO returned a quote without a usable price.", status="ERROR") from exc
            stamp = self._optional_timestamp(payload.get("t"))

        if not isfinite(price) or price <= 0:
            raise ProviderError("SiftingIO returned a non-positive or invalid price.", status="ERROR")
        quote = {"symbol": symbol, "instrument": symbol, "timeframe": timeframe, "price": price,
                 "timestamp": stamp or datetime.now(timezone.utc).isoformat(), "provider": self.provider_id,
                 "data_source": self.provider_id, "data_status": "DELAYED", "streaming": False, "note": note}
        with self._cache_lock:
            self._quote_cache[symbol] = (now_ts, quote)
        return quote

    def get_cached_quote(self, symbol: str) -> dict | None:
        """Return the last quote seen in memory without any provider call."""
        if symbol not in self.symbols:
            return None
        with self._cache_lock:
            entry = self._quote_cache.get(symbol)
        return dict(entry[1]) if entry else None

    @classmethod
    def tick_from_payload(cls, payload: dict[str, Any], symbol: str) -> dict[str, Any] | None:
        """Build a validated live tick from a SiftingIO ``tick`` frame.

        Returns ``None`` for anything that is not a usable price so an error
        object or malformed frame is never routed as market data.
        """
        try:
            price = float(payload.get("p"))
        except (TypeError, ValueError):
            return None
        if not isfinite(price) or price <= 0:
            return None
        # The provider timestamp is authoritative freshness. If it is missing or
        # unparsable, do not invent one from the local clock: a locally stamped
        # price would look "fresh" forever and could be shown as LIVE. Drop the
        # frame instead of fabricating a tick.
        stamp = cls._optional_timestamp(payload.get("t"))
        if stamp is None:
            return None
        tick_time = cls._parse_bound(stamp)
        if tick_time is None:
            return None
        # SiftingIO replays the last trade when a client subscribes, so a tick is
        # only "LIVE" when its own provider timestamp is fresh. An old replay is
        # reported as STALE rather than being presented as a current price.
        age = (datetime.now(timezone.utc) - tick_time).total_seconds()
        status = "STALE" if age > cls.LIVE_TICK_MAX_AGE else "LIVE"
        return {"type": "price", "symbol": symbol, "price": price, "timestamp": stamp,
                "provider": cls.provider_id, "data_status": status, "data_source": cls.provider_id}

    # One shared upstream stream per API key. The free plan allows a single
    # concurrent WebSocket, so this must never become one connection per symbol,
    # per browser client, or per timeframe.
    _stream_hubs: dict[str, "_SiftingStreamHub"] = {}
    _stream_hubs_lock = threading.Lock()

    def _stream_hub(self) -> "_SiftingStreamHub":
        with SiftingProvider._stream_hubs_lock:
            hub = SiftingProvider._stream_hubs.get(self.api_key)
            if hub is None:
                hub = _SiftingStreamHub(self)
                SiftingProvider._stream_hubs[self.api_key] = hub
            return hub

    @classmethod
    def reset_stream_hubs(cls) -> None:
        """Drop the cached shared streams (tests / process restart)."""
        with cls._stream_hubs_lock:
            cls._stream_hubs.clear()

    @classmethod
    def stream_diagnostics(cls) -> dict[str, Any]:
        """Safe stream diagnostics (no API key, no connection URL)."""
        with cls._stream_hubs_lock:
            hubs = list(cls._stream_hubs.values())
        if not hubs:
            return {"connected": False, "tier": None, "max_conn": None, "max_subs": None,
                    "active_conn": None, "symbols": [], "limit_reached": False,
                    "last_error": None, "last_error_code": None}
        return hubs[0].diagnostics()

    async def stream_quotes(self, symbol: str):
        """Yield live ticks for ``symbol`` from the one shared upstream stream.

        Every call multiplexes onto the *same* SiftingIO connection through the
        hub; callers never open an upstream connection themselves. Multiple
        symbols and multiple browser clients share it, and subscriptions are
        restored automatically after a reconnect.
        """
        if symbol not in self.symbols:
            raise ProviderError(f"{symbol} is not supported by the SiftingIO adapter.")
        hub = self._stream_hub()
        queue = await hub.subscribe(symbol)
        try:
            while True:
                yield await queue.get()
        finally:
            await hub.unsubscribe(symbol, queue)


class KiteProvider:
    """Zerodha Kite Connect historical/quote adapter (daily access token required)."""
    provider_id = "ZERODHA_KITE"
    # The index instruments are exact display-symbol mappings; NSE stocks use NSE:<symbol>.
    aliases = {"NIFTY": ("NSE", "NIFTY 50"), "BANKNIFTY": ("NSE", "NIFTY BANK")}

    def __init__(self, api_key: str, access_token: str):
        self.api_key, self.access_token = api_key, access_token

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Kite-Version": "3", "Authorization": f"token {self.api_key}:{self.access_token}"}

    @lru_cache(maxsize=1)
    def _instruments(self) -> dict[str, tuple[str, int]]:
        try:
            response = httpx.get("https://api.kite.trade/instruments", headers=self.headers, timeout=30)
            response.raise_for_status()
            rows = csv.DictReader(io.StringIO(response.text))
            return {f"{r['exchange']}:{r['tradingsymbol']}": (r["exchange"], int(r["instrument_token"])) for r in rows}
        except Exception as exc:
            raise _http_error(exc) from exc

    def instrument_token(self, symbol: str) -> tuple[str, int]:
        exchange, trading_symbol = self.aliases.get(symbol, ("NSE", symbol))
        item = self._instruments().get(f"{exchange}:{trading_symbol}")
        if item is None:
            raise ProviderError(f"Unsupported Kite instrument: {symbol}. Confirm it is listed in the current NSE instrument catalog.")
        return item

    def get_instruments(self) -> list[dict[str, Any]]:
        try:
            catalog = self._instruments()
        except ProviderError:
            return []
        rows = [{"symbol": symbol, "provider_symbol": f"{exchange}:{name}", "name": name,
                 "asset_class": "Indian index", "exchange": exchange, "exchange_timezone": "Asia/Kolkata",
                 "provider": self.provider_id, "data_status": "DISCONNECTED"}
                for symbol, (exchange, name) in self.aliases.items() if f"{exchange}:{name}" in catalog]
        rows.extend({"symbol": key.split(":", 1)[1], "provider_symbol": key, "name": key.split(":", 1)[1],
                     "asset_class": "Indian equity / index", "exchange": key.split(":", 1)[0],
                     "exchange_timezone": "Asia/Kolkata", "provider": self.provider_id, "data_status": "DISCONNECTED"}
                    for key in catalog if key.startswith("NSE:") and key.split(":", 1)[1] not in {n for _, n in self.aliases.values()})
        return rows

    def get_historical_candles(self, symbol: str, timeframe: str, limit: int = 240,
                               start: str | None = None, end: str | None = None) -> list[dict]:
        if timeframe not in TIMEFRAMES:
            raise ProviderError(f"Unsupported timeframe: {timeframe}")
        exchange, token = self.instrument_token(symbol)
        interval = TIMEFRAMES[timeframe][1]
        now = datetime.now(timezone.utc)
        to_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else now
        from_dt = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else to_dt.replace(microsecond=0)
        if not start:
            # API limit and interval semantics are left to provider; fetch a safe bounded window.
            from datetime import timedelta
            from_dt = to_dt - timedelta(seconds={"1m": 60, "3m": 180, "5m": 300, "15m": 900,
                "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}[timeframe] * limit * 2)
        # Kite exposes 60-minute and daily bars, so form 4h and 1w candles locally.
        fetch_limit = limit * (5 if timeframe == "4h" else 7 if timeframe == "1w" else 1)
        if not start:
            from datetime import timedelta
            from_dt = to_dt - timedelta(seconds={"1m": 60, "3m": 180, "5m": 300, "15m": 900,
                "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}[timeframe] * fetch_limit * 2)
        url = f"https://api.kite.trade/instruments/historical/{token}/{interval}"
        from zoneinfo import ZoneInfo
        local_from = from_dt.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
        local_to = to_dt.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
        try:
            response = httpx.get(url, headers=self.headers,
                params={"from": local_from, "to": local_to, "oi": "false"}, timeout=20)
            response.raise_for_status()
            rows = response.json().get("data", {}).get("candles", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise _http_error(exc) from exc

        def _kite_stamp(val: Any) -> str:
            dt = datetime.fromisoformat(str(val).strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            return dt.isoformat()

        if timeframe in {"4h", "1w"}:
            groups: dict[tuple, list] = {}
            for row in rows:
                stamp_str = _kite_stamp(row[0])
                stamp = datetime.fromisoformat(stamp_str)
                if timeframe == "1w":
                    key = (stamp.isocalendar().year, stamp.isocalendar().week)
                else:
                    # NSE regular session begins at 09:15 IST; preserve that session anchor.
                    minutes = stamp.hour * 60 + stamp.minute - 555
                    key = (stamp.date(), max(0, minutes // 240))
                groups.setdefault(key, []).append((stamp_str, row))
            aggregated = []
            for _, batch in sorted(groups.items()):
                if timeframe == "1w":
                    first = datetime.fromisoformat(batch[0][0])
                    monday = first.date().fromordinal(first.date().toordinal() - first.weekday())
                    stamp = datetime.combine(monday, first.timetz()).isoformat()
                else:
                    stamp = batch[0][0]
                volumes = [float(item[1][5]) for item in batch if len(item[1]) > 5 and item[1][5] is not None]
                aggregated.append({"timestamp": stamp, "open": batch[0][1][1], "high": max(float(r[1][2]) for r in batch),
                    "low": min(float(r[1][3]) for r in batch), "close": batch[-1][1][4],
                    "volume": sum(volumes) if volumes else None,
                    "volume_type": "exchange_traded" if volumes else "unavailable"})
            candle_rows = aggregated[-limit:]
        else:
            candle_rows = [{"timestamp": _kite_stamp(r[0]), "open": r[1], "high": r[2], "low": r[3],
                "close": r[4], "volume": r[5] if len(r) > 5 else None,
                "volume_type": "exchange_traded" if len(r) > 5 else "unavailable"} for r in rows[-limit:]]
        normalized, _ = normalize_candles(candle_rows, instrument=symbol, timeframe=timeframe, data_source=self.provider_id)
        return normalized

    def get_latest_quote(self, symbol: str, timeframe: str = "15m") -> dict:
        exchange, _ = self.instrument_token(symbol)
        _, trading_symbol = self.aliases.get(symbol, ("NSE", symbol))
        instrument = f"{exchange}:{trading_symbol}"
        try:
            response = httpx.get("https://api.kite.trade/quote/ltp", headers=self.headers,
                params={"i": instrument}, timeout=10)
            response.raise_for_status()
            payload = response.json()["data"][instrument]
            price = float(payload["last_price"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise _http_error(exc) from exc
        return {"symbol": symbol, "instrument": symbol, "timeframe": timeframe, "price": price,
                "timestamp": datetime.now(timezone.utc).isoformat(), "provider": self.provider_id,
                "data_source": self.provider_id, "data_status": "DISCONNECTED",
                "note": "REST quote received; realtime stream is not active."}

    async def stream_quotes(self, symbol: str):
        _, token = self.instrument_token(symbol)
        url = f"wss://ws.kite.trade?api_key={self.api_key}&access_token={self.access_token}"
        async with websockets.connect(url, open_timeout=15, ping_interval=20, ping_timeout=20, max_size=None) as ws:
            await ws.send(json.dumps({"a": "subscribe", "v": [token]}))
            await ws.send(json.dumps({"a": "mode", "v": ["ltp", [token]]}))
            async for packet in ws:
                if isinstance(packet, str):
                    try:
                        message = json.loads(packet)
                    except ValueError:
                        continue
                    if message.get("type") == "error":
                        raise ProviderError("Kite rejected the realtime subscription; check access-token expiry and data entitlement.")
                    continue
                if not isinstance(packet, bytes) or len(packet) < 2:
                    continue
                count = int.from_bytes(packet[:2], "big")
                offset = 2
                for _ in range(count):
                    if offset + 2 > len(packet):
                        break
                    size = int.from_bytes(packet[offset:offset + 2], "big")
                    offset += 2
                    tick = packet[offset:offset + size]
                    offset += size
                    # LTP/ltpc packets are 8 bytes: uint32 instrument token and price paise.
                    if len(tick) >= 8 and int.from_bytes(tick[:4], "big") == token:
                        price = int.from_bytes(tick[4:8], "big") / 100
                        yield {"type": "price", "symbol": symbol, "price": price,
                               "timestamp": datetime.now(timezone.utc).isoformat(),
                               "provider": self.provider_id, "data_status": "LIVE", "data_source": self.provider_id}


# goldprice.dev ("Gold API") canonical intervals. The adapter accepts exactly
# these provider interval names; the app's ``3m`` and ``1w`` frames are not
# offered because the provider cannot serve them and they cannot be aggregated
# down from a coarser feed.
GOLD_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d")

# Documented plan gating for ``/v1/bars``. Verified live: a Free key reports tier
# ``free_2026`` and answers 403 ``plan_gated`` for every intraday interval, so
# daily bars are the only history a Free/Basic key can request, Plus adds 1h/4h,
# and Pro unlocks the full intraday set. Until the tier is learned from a real
# response the adapter advertises only the interval every key can serve, so a
# timeframe is never claimed before the provider has actually confirmed it.
GOLD_TIER_INTERVALS: dict[str, tuple[str, ...]] = {
    "free": ("1d",), "free_2026": ("1d",), "basic": ("1d",),
    "plus": ("1d", "1h", "4h"),
    "pro": GOLD_INTERVALS, "legacy": GOLD_INTERVALS,
}
GOLD_DEFAULT_INTERVALS: tuple[str, ...] = ("1d",)


class GoldProvider:
    """XAU/USD spot adapter for goldprice.dev (the ``gold`` provider).

    Verified against the live API with the configured key:

      * ``GET /v1/prices?symbol=XAU-USD-SPOT`` and ``GET /v1/spot/XAU-USD-SPOT``
        return a decimal ``price`` string, an ``is_stale`` flag, and a
        ``computed_at`` observation timestamp.
      * ``GET /v1/bars?symbol=&interval=&from=&to=&limit=`` returns real OHLCV
        bars newest-first with ``is_closed`` and ``volume`` (``null`` for spot).
        Intraday intervals are plan-gated per ``GOLD_TIER_INTERVALS``.

    Honesty notes:

      * A price is only reported fresh when the provider's own ``computed_at``
        is recent; ``is_stale`` is never overridden and a missing timestamp is
        never replaced by the local clock.
      * Bars are validated by ``normalize_candles`` and never padded. A
        plan-gated interval raises a clear, non-retryable error instead of
        falling back to a coarser series or fabricated candles.
      * The credential stays in the backend: it is sent only as an
        ``Authorization`` header and never appears in a URL or response.
    """

    provider_id = "GOLD"
    symbols = {"XAUUSD": "XAU-USD-SPOT"}
    REST_BASE = "https://api.goldprice.dev/v1"
    # A short quote TTL collapses overlapping frontend/status polls into one
    # upstream call, keeping a 10/min key well inside its limit. Bars change
    # slowly and reuse for longer.
    CANDLE_CACHE_TTL = 120.0
    QUOTE_CACHE_TTL = 30.0
    QUOTE_TIMEOUT = 10.0
    BAR_TIMEOUT = 20.0
    MAX_LIMIT = 1000

    _candle_cache: dict[tuple, tuple[float, list[dict]]] = {}
    _quote_cache: dict[str, tuple[float, dict]] = {}
    _inflight: dict[tuple, threading.Lock] = {}
    _cache_lock = threading.Lock()

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._tier: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    @staticmethod
    def _error(status_code: int, payload: Any) -> ProviderError:
        data = payload if isinstance(payload, dict) else {}
        code = str(data.get("error") or "").lower()
        message = str(data.get("message") or "").strip()
        tier = data.get("tier")
        if status_code == 401 or code == "invalid_api_key":
            return ProviderError(
                "goldprice.dev authentication failed. Check the backend INTERNATIONAL_DATA_API_KEY.",
                status="ERROR", code=401,
            )
        if status_code == 403 or code == "plan_gated":
            detail = message or "this symbol, interval, or date range is outside the current plan."
            suffix = f" (plan {tier})" if tier else ""
            return ProviderError(f"goldprice.dev plan limit: {detail}{suffix}", status="PLAN_GATED", code=403)
        if status_code == 404 or code in {"invalid_symbol", "not_found"}:
            return ProviderError("goldprice.dev does not cover this symbol or interval.", status="ERROR", code=404)
        if status_code == 429:
            if code == "quota_exceeded":
                return ProviderError(
                    "goldprice.dev monthly quota exhausted. Wait for the quota to reset or upgrade the plan.",
                    retryable=False, status="QUOTA_EXCEEDED", code=429,
                )
            retry_after = data.get("retry_after_seconds")
            hint = f" Retry in about {retry_after}s." if isinstance(retry_after, (int, float)) else ""
            return ProviderError(f"goldprice.dev rate limit reached.{hint}", retryable=True, status="RATE_LIMITED", code=429)
        if status_code in (500, 502, 503, 504):
            return ProviderError(
                "goldprice.dev is temporarily unavailable; retry shortly.",
                retryable=True, status="RATE_LIMITED", code=status_code,
            )
        if status_code == 400:
            return ProviderError(
                "goldprice.dev rejected the request parameters; check the symbol, interval, and date range.",
                status="ERROR", code=400,
            )
        return ProviderError("goldprice.dev returned an unsuccessful or malformed response.", status="ERROR")

    def _get_json(self, path: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self.REST_BASE}{path}", params=params, headers=self.headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError("goldprice.dev request timed out; retry shortly.", retryable=True, status="RATE_LIMITED") from exc
        except httpx.NetworkError as exc:
            raise ProviderError("goldprice.dev is unreachable; check backend network access.", retryable=True, status="DISCONNECTED") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("goldprice.dev request failed before a response was received.", status="ERROR") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("goldprice.dev returned malformed JSON.", status="ERROR") from exc
        if not response.is_success:
            raise self._error(response.status_code, payload)
        if not isinstance(payload, dict):
            raise ProviderError("goldprice.dev returned a malformed response.", status="ERROR")
        return payload

    @staticmethod
    def _iso_utc(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_bound(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _plan_window_start(error: ProviderError) -> str | None:
        """Extract a plan's lower history bound from a ``plan_gated`` message.

        The provider answers a too-wide request with a concrete lower bound (for
        example "can request history from 2026-09-18 onward"). Returning it lets
        the caller retry once with the plan's own window instead of guessing.
        """
        match = re.search(r"from (\d{4}-\d{2}-\d{2})", str(error))
        return match.group(1) if match else None

    def get_instruments(self) -> list[dict[str, Any]]:
        return [{
            "symbol": symbol, "provider_symbol": provider_symbol,
            "name": "XAU/USD \u2014 Gold", "asset_class": "Metals",
            "exchange": "goldprice.dev aggregated spot feed",
            "exchange_timezone": "America/New_York", "provider": self.provider_id,
            "data_status": "DISCONNECTED",
        } for symbol, provider_symbol in self.symbols.items()]

    def get_market_session(self, symbol: str) -> dict[str, Any] | None:
        """goldprice.dev publishes no documented trading calendar.

        Its documented API surface (prices, bars, settlements, ratios, account)
        exposes no market-hours catalog, so spot gold has no provider-verified
        schedule here and this returns ``None``. The session service then reports
        ``SCHEDULE_UNKNOWN`` ("SCHEDULE UNVERIFIED") rather than inventing a
        metals calendar.
        """
        return None

    def available_timeframes(self) -> list[str]:
        """Intervals the configured key's plan can actually serve.

        Defaults to the daily-only set until a real ``/v1/bars`` response has
        revealed the plan, so no timeframe is advertised before the provider has
        confirmed the key can serve it.
        """
        intervals = GOLD_TIER_INTERVALS.get(self._tier or "", GOLD_DEFAULT_INTERVALS)
        return [tf for tf in TIMEFRAME_SECONDS if tf in intervals]

    def _key_lock(self, cache_key: tuple) -> threading.Lock:
        with self._cache_lock:
            lock = self._inflight.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                self._inflight[cache_key] = lock
            return lock

    def _cached_rows(self, cache_key: tuple, limit: int, ranged: bool, now_ts: float) -> list[dict] | None:
        with self._cache_lock:
            entry = self._candle_cache.get(cache_key)
        if entry is None:
            return None
        cached_time, cached_rows = entry
        if now_ts - cached_time >= self.CANDLE_CACHE_TTL:
            return None
        if ranged or len(cached_rows) >= limit:
            return list(cached_rows) if ranged else list(cached_rows)[-limit:]
        return None

    def _fetch_bars(self, symbol: str, timeframe: str, limit: int,
                    start: str | None, end: str | None) -> list[dict]:
        step = TIMEFRAME_SECONDS[timeframe]
        end_dt = self._parse_bound(end) or datetime.now(timezone.utc)
        start_dt = self._parse_bound(start)
        if start_dt is None:
            window = min(step * limit * 2, 60 * 60 * 24 * 400)
            start_dt = end_dt - timedelta(seconds=window)
        if start_dt >= end_dt:
            raise ProviderError("goldprice.dev history range is invalid: start must be before end.")
        params = {
            "symbol": self.symbols[symbol],
            "interval": timeframe,
            "from": start_dt.date().isoformat(),
            "to": end_dt.date().isoformat(),
            "limit": min(int(limit), self.MAX_LIMIT),
        }
        try:
            payload = self._get_json("/bars", params, self.BAR_TIMEOUT)
        except ProviderError as exc:
            # A Free/Basic key that asks for a window older than its retention
            # gets a plan_gated error that names the earliest allowed date. Clamp
            # to that documented bound and retry once rather than guessing.
            clamped = self._plan_window_start(exc)
            if getattr(exc, "status", "") != "PLAN_GATED" or not clamped:
                raise
            params["from"] = clamped
            payload = self._get_json("/bars", params, self.BAR_TIMEOUT)

        meta = payload.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("tier"), str):
            self._tier = str(meta["tier"]).strip().lower()

        bars = payload.get("bars")
        if not isinstance(bars, list):
            raise ProviderError("goldprice.dev returned no bar series.", status="ERROR")
        rows: list[dict[str, Any]] = []
        for bar in bars:
            if not isinstance(bar, dict):
                continue
            stamp = self._iso_utc(bar.get("bar_start"))
            if stamp is None:
                continue
            try:
                rows.append({
                    "timestamp": stamp,
                    "open": float(bar["open"]), "high": float(bar["high"]),
                    "low": float(bar["low"]), "close": float(bar["close"]),
                    "volume": bar.get("volume"),
                })
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError("goldprice.dev returned a bar with invalid OHLC values.", status="ERROR") from exc
        # The API returns newest-first; validation requires strictly increasing UTC.
        rows.sort(key=lambda row: row["timestamp"])
        normalized, _warnings = normalize_candles(
            rows, instrument=symbol, timeframe=timeframe, data_source=self.provider_id
        )
        return normalized[-limit:]

    def get_historical_candles(self, symbol: str, timeframe: str, limit: int = 240,
                               start: str | None = None, end: str | None = None) -> list[dict]:
        if symbol not in self.symbols:
            raise ProviderError(f"{symbol} is not supported by the goldprice.dev adapter.")
        if timeframe not in GOLD_INTERVALS:
            supported = ", ".join(GOLD_INTERVALS)
            raise ProviderError(
                f"goldprice.dev does not support the {timeframe} interval; supported intervals are {supported}."
            )
        if not 1 <= int(limit) <= 5000:
            raise ProviderError("goldprice.dev limit must be between 1 and 5000.")
        target = int(limit)
        cache_key = (symbol, timeframe, start, end)
        ranged = bool(start and end)
        now_ts = time.time()
        cached = self._cached_rows(cache_key, target, ranged, now_ts)
        if cached is not None:
            return cached
        with self._key_lock(cache_key):
            now_ts = time.time()
            cached = self._cached_rows(cache_key, target, ranged, now_ts)
            if cached is not None:
                return cached
            rows = self._fetch_bars(symbol, timeframe, target, start, end)
            with self._cache_lock:
                self._candle_cache[cache_key] = (time.time(), list(rows))
            return rows

    def _quote_from_payload(self, payload: dict[str, Any], symbol: str, timeframe: str) -> dict[str, Any]:
        try:
            price = float(payload.get("price"))
        except (TypeError, ValueError) as exc:
            raise ProviderError("goldprice.dev returned a quote with no usable price.", status="ERROR") from exc
        if not isfinite(price) or price <= 0:
            raise ProviderError("goldprice.dev returned a non-positive gold price.", status="ERROR")
        stamp = self._iso_utc(payload.get("computed_at"))
        provider_stale = bool(payload.get("is_stale"))
        # A quote with no provider timestamp cannot be shown as fresh: a locally
        # stamped price would look LIVE forever. Treat it as STALE instead.
        stale = provider_stale or stamp is None
        return {
            "symbol": symbol, "instrument": symbol, "timeframe": timeframe,
            "price": price, "timestamp": stamp, "is_stale": stale,
            "quote_currency": payload.get("quote_currency") or "USD",
            "unit": payload.get("unit") or "troy_ounce",
            "provider": self.provider_id, "data_source": self.provider_id,
            "data_status": "STALE" if stale else "LIVE",
        }

    def get_latest_quote(self, symbol: str, timeframe: str = "15m") -> dict:
        if symbol not in self.symbols:
            raise ProviderError(f"{symbol} is not supported by the goldprice.dev adapter.")
        now_ts = time.time()
        with self._cache_lock:
            entry = self._quote_cache.get(symbol)
        if entry and now_ts - entry[0] < self.QUOTE_CACHE_TTL:
            return dict(entry[1])
        with self._key_lock(("quote", symbol)):
            now_ts = time.time()
            with self._cache_lock:
                entry = self._quote_cache.get(symbol)
            if entry and now_ts - entry[0] < self.QUOTE_CACHE_TTL:
                return dict(entry[1])
            payload = self._get_json(f"/spot/{self.symbols[symbol]}", {}, self.QUOTE_TIMEOUT)
            quote = self._quote_from_payload(payload, symbol, timeframe)
            with self._cache_lock:
                self._quote_cache[symbol] = (time.time(), dict(quote))
            return quote

    def get_cached_quote(self, symbol: str) -> dict | None:
        """Return the last quote seen in memory without any provider call."""
        if symbol not in self.symbols:
            return None
        with self._cache_lock:
            entry = self._quote_cache.get(symbol)
        return dict(entry[1]) if entry else None


XAUS_BASE = "https://xaus.com"
XAUS_SYMBOL = "xau"
# Requested timeframes this adapter can genuinely serve. Every one of them is
# either a native /chart interval or built by the service layer from a native
# interval (see AGGREGATION_PLANS["XAUS"]); nothing here is assumed.
XAUS_TIMEFRAMES = ("1m", "2m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w")
# Timeframe -> the interval token POSTed to /api/v1/chart. Only intervals the
# provider actually serves natively appear here; the aggregated frames (3m, 2h,
# 4h) are never requested from the provider directly.
XAUS_NATIVE_INTERVALS = {
    "1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d", "1w": "1wk",
}
# Ascending /chart ranges that are safe for each native interval. Intraday ranges
# are capped where the upstream window is exceeded (verified: 60m + 3mo answers
# 503 "upstream unavailable"), and a shortfall walks one rung up rather than
# guessing.
XAUS_RANGE_LADDER = {
    "1m": ("1d", "5d"),
    "2m": ("1d", "5d", "1mo"),
    "5m": ("1d", "5d", "1mo"),
    "15m": ("1d", "5d", "1mo"),
    "30m": ("1d", "5d", "1mo"),
    "60m": ("1d", "5d", "1mo", "3mo", "1y"),
    "1d": ("1y", "2y", "5y"),
    "1wk": ("5y", "max"),
}
XAUS_RANGE_DAYS = {"1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "1y": 365, "2y": 730, "5y": 1825, "max": 4000}
# Intraday sessions cover roughly 5/7 of the calendar once the daily settlement
# break and weekends are removed, daily bars about 5/7 as well, while weekly bars
# are contiguous. Used only to pick a starting rung so a request does not walk
# the whole ladder.
XAUS_INTRADAY_DENSITY = 0.70
XAUS_DAILY_DENSITY = 0.69
XAUS_WEEKLY_DENSITY = 1.0


class XausProvider:
    """XAU/USD gold adapter for xaus.com (the ``xaus`` provider).

    Verified live against https://xaus.com/api/openapi.json (v1.5):

      * ``GET /api/v1/spot?compact=1`` returns ``spot_usd_oz`` plus
        ``price_as_of`` and a canonical ``data_state`` object
        (``{status, as_of, source, age_seconds}``).
      * ``GET /api/v1/chart?symbol=xau&interval=&range=`` returns real OHLCV
        points ``{t, o, h, l, c, v}`` with ``t`` in epoch seconds and echoes the
        interval it actually served.
      * ``GET /api/v1/history`` serves daily closes without an open price, and
        ``GET /api/v1/intraday`` serves first-party 2-minute price samples with
        no OHLC, so neither is used to build candles.

    The provider is keyless: no registration, no API key, GET only, CORS open
    and no enforced rate limit (fair use). Nothing about it needs to be hidden
    from a browser, but the adapter still runs server-side so the app has a
    single market-data path.

    Provider quirks verified against the live service and handled here:

      * ``/chart`` silently falls back to daily output for an unsupported
        interval (``3m``/``2h``/``4h`` are answered HTTP 200 with
        ``interval: "1d"``). A response is therefore only accepted when the
        echoed interval matches the request, so a coarser series can never be
        passed off as a finer one.
      * The final point of every intraday series is the still-forming bar and is
        stamped with the last tick second (``20:59:59`` for a 1-minute series)
        rather than the bar open. It is dropped, never snapped to a time it did
        not have.
      * Daily points are stamped at the venue's local midnight (``04:00Z``, or
        ``05:00Z`` outside US summer time, and ``14:30Z`` on early closes), so
        their raw spacing is not a whole number of days. Each daily bar is
        re-stamped at ``00:00Z`` of its own UTC trading date, which is the daily
        convention the rest of the app already uses; the trading date itself is
        never altered.
      * ``data_state.age_seconds`` reads 0 on ``/chart`` regardless of bar age,
        so candle freshness is always measured from the newest point's own
        timestamp.

    Honesty notes:

      * ``data_state.status`` is authoritative and a reported price is never
        promoted past it. ``price_as_of`` is the upstream observation time, so
        freshness is measured from it and never from a local clock stamp.
      * Unsupported intervals raise; nothing is padded, substituted or invented.
        The derived 3m/2h/4h series are built only from real XAUS candles.
    """

    provider_id = "XAUS"
    requires_key = False
    symbols = {"XAUUSD": XAUS_SYMBOL}
    REST_BASE = XAUS_BASE
    # Provenance: XAUS aggregates two upstreams, so the quote and the candle
    # series are labelled separately rather than presented as one feed.
    quote_source_detail = "xaus.com /api/v1/spot spot reference (upstream: gold-api.com)"
    quote_source_name = "gold-api.com"
    candle_source_detail = "xaus.com /api/v1/chart OHLCV chart feed (exchange/futures volume)"
    GOLD_API_BASE = "https://api.gold-api.com"
    CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache", "xaus")
    # /spot is edge-cached for about a minute upstream, so a short local TTL
    # collapses overlapping frontend and status polls without adding staleness.
    QUOTE_CACHE_TTL = 20.0
    CANDLE_CACHE_TTL = 90.0
    QUOTE_TIMEOUT = 10.0
    BAR_TIMEOUT = 20.0
    MAX_LIMIT = 5000

    _candle_cache: dict[tuple, tuple[float, list[dict]]] = {}
    _quote_cache: dict[str, tuple[float, dict]] = {}
    _inflight: dict[tuple, threading.Lock] = {}
    _cache_lock = threading.Lock()

    def _save_persistent_cache(self, key: tuple, rows: list[dict]) -> None:
        # Prevent test fixtures from polluting the persistent runtime cache
        if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("TRADEMIND_TEST_ENV") == "1":
            return
        try:
            # Enforce price-scale consistency: never persist incompatible prices
            validate_xauusd_candles(rows)
            os.makedirs(self.CACHE_DIR, exist_ok=True)
            symbol, timeframe, start, end = key
            safe_name = f"{symbol}_{timeframe}_{start or 'none'}_{end or 'none'}.json"
            filepath = os.path.join(self.CACHE_DIR, safe_name)
            with open(filepath, "w") as f:
                json.dump({"saved_at": time.time(), "rows": rows}, f)
        except Exception:
            pass

    def _load_persistent_cache(self, key: tuple, limit: int, ranged: bool) -> list[dict] | None:
        try:
            symbol, timeframe, start, end = key
            safe_name = f"{symbol}_{timeframe}_{start or 'none'}_{end or 'none'}.json"
            filepath = os.path.join(self.CACHE_DIR, safe_name)
            if not os.path.exists(filepath):
                safe_name = f"{symbol}_{timeframe}_none_none.json"
                filepath = os.path.join(self.CACHE_DIR, safe_name)
            if os.path.exists(filepath):
                with open(filepath, "r") as f:
                    data = json.load(f)
                    rows = data.get("rows")
                    if isinstance(rows, list) and rows:
                        # Validate price-scale and OHLC consistency of cached candles
                        try:
                            validate_xauusd_candles(rows)
                        except Exception:
                            # Contaminated or incompatible cache: discard and remove
                            try:
                                os.remove(filepath)
                            except Exception:
                                pass
                            return None
                        return list(rows) if ranged else list(rows)[-limit:]
        except Exception:
            pass
        return None

    def __init__(self, api_key: str = ""):
        # Keyless by design: the argument exists only so this adapter is a
        # drop-in replacement for the other international providers.
        self.api_key = api_key or ""

    @property
    def headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "User-Agent": "TradeMindAI/1.0 (+https://xaus.com)"}

    @staticmethod
    def _error(status_code: int, payload: Any) -> ProviderError:
        data = payload if isinstance(payload, dict) else {}
        message = str(data.get("error") or data.get("message") or data.get("detail") or "").strip()
        state = data.get("data_state") if isinstance(data.get("data_state"), dict) else {}
        if status_code == 429:
            return ProviderError(
                "xaus.com asked clients to slow down (fair-use limit). Retry shortly.",
                retryable=True, status="RATE_LIMITED", code=429,
            )
        if status_code in (500, 502, 503, 504) or state.get("status") == "unavailable":
            return ProviderError(
                "xaus.com has no real gold price to serve right now (upstream unavailable and nothing cached).",
                retryable=True, status="UNAVAILABLE", code=status_code,
            )
        if status_code in (400, 422):
            return ProviderError(
                f"xaus.com rejected the request parameters{': ' + message if message else '.'}",
                status="ERROR", code=status_code,
            )
        if status_code == 404:
            return ProviderError("xaus.com does not cover this symbol or interval.", status="ERROR", code=404)
        return ProviderError(
            f"xaus.com returned an unsuccessful or malformed response{': ' + message if message else '.'}",
            status="ERROR", code=status_code,
        )

    def _get_json(self, path: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self.REST_BASE}{path}", params=params, headers=self.headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError("xaus.com request timed out; retry shortly.", retryable=True, status="RATE_LIMITED") from exc
        except httpx.NetworkError as exc:
            raise ProviderError("xaus.com is unreachable; check backend network access.", retryable=True, status="DISCONNECTED") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("xaus.com request failed before a response was received.", status="ERROR") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("xaus.com returned malformed JSON.", status="ERROR") from exc
        if not response.is_success:
            raise self._error(response.status_code, payload)
        if not isinstance(payload, dict):
            raise ProviderError("xaus.com returned a malformed response.", status="ERROR")
        return payload

    @staticmethod
    def _iso_utc(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        # Normalise to millisecond precision so the stamp round-trips cleanly.
        if dt.microsecond:
            return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _parse_bound(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _key_lock(self, key: tuple) -> threading.Lock:
        with self._cache_lock:
            lock = self._inflight.get(key)
            if lock is None:
                lock = threading.Lock()
                self._inflight[key] = lock
            return lock

    def _cached_rows(self, key: tuple, limit: int, ranged: bool, now_ts: float) -> list[dict] | None:
        with self._cache_lock:
            entry = self._candle_cache.get(key)
        if entry is None:
            return None
        cached_time, cached_rows = entry
        if now_ts - cached_time >= self.CANDLE_CACHE_TTL:
            return None
        if ranged or len(cached_rows) >= limit:
            return list(cached_rows) if ranged else list(cached_rows)[-limit:]
        return None

    def get_instruments(self) -> list[dict[str, Any]]:
        return [{
            "symbol": "XAUUSD", "provider_symbol": XAUS_SYMBOL,
            "name": "XAU/USD \u2014 Gold", "asset_class": "Metals",
            "exchange": "xaus.com aggregated OTC gold feed",
            "exchange_timezone": "America/New_York", "price_precision": 2,
            "base": "XAU", "quote": "USD",
            "provider": self.provider_id, "data_status": "DISCONNECTED",
        }]

    def get_market_session(self, symbol: str) -> dict[str, Any] | None:
        """XAUS publishes no market-hours calendar, so none is invented.

        Returning ``None`` makes the session layer report SCHEDULE_UNKNOWN,
        which stays independent of quote freshness: an open session is never
        inferred from a successful request and a closed one never makes a price
        stale on its own.
        """
        return None

    def available_timeframes(self) -> list[str]:
        """Every requested timeframe this account genuinely serves.

        All of them are free of plan gating: XAUS has no paid tiers, so nothing
        here is restricted. The list still comes from real capability (native
        intervals plus honest aggregation) rather than from the request.
        """
        return [frame for frame in XAUS_TIMEFRAMES if frame in TIMEFRAME_SECONDS]

    def _starting_range(self, native: str, needed: int) -> str:
        ladder = XAUS_RANGE_LADDER[native]
        step = TIMEFRAME_SECONDS[[tf for tf, token in XAUS_NATIVE_INTERVALS.items() if token == native][0]]
        if native == "1wk":
            density = XAUS_WEEKLY_DENSITY
        elif native == "1d":
            density = XAUS_DAILY_DENSITY
        else:
            density = XAUS_INTRADAY_DENSITY
        for rng in ladder:
            estimate = XAUS_RANGE_DAYS.get(rng, 0) * 86400 / step * density
            if estimate >= needed:
                return rng
        return ladder[-1]

    def _fetch_points(self, timeframe: str, native: str, needed: int) -> list[dict]:
        """Fetch the newest ``needed`` native points by walking the range ladder.

        A wider window exists only to supply *more* history going back in time,
        so it may never cost a *newer* bar. xaus.com answers its widest weekly
        window with a coarse series that still claims ``interval=1wk`` - about
        269 points spanning 26 years - whose newest usable (Monday) bar is months
        older than the 5-year window's. Fast-forwarding the ladder to that rung
        therefore made the weekly series *stale as a side effect of asking for
        more bars*. The ladder is now always entered one rung below the estimated
        starting rung, so a wider window is compared against a denser, more
        recent one, and a rung that regresses recency or coverage is discarded
        rather than silently replacing the better series.
        """
        ladder = XAUS_RANGE_LADDER[native]
        start = max(0, ladder.index(self._starting_range(native, needed)) - 1)
        best: list[dict] = []
        best_usable = 0
        best_newest: str | None = None
        last_error: ProviderError | None = None
        for rng in ladder[start:]:
            try:
                payload = self._get_json(
                    "/api/v1/chart",
                    {"symbol": XAUS_SYMBOL, "range": rng, "interval": native},
                    self.BAR_TIMEOUT,
                )
            except ProviderError as exc:
                # A wider window can exceed the upstream limit; keep whatever a
                # narrower rung already returned and try the next rung.
                last_error = exc
                continue
            served = str(payload.get("interval") or "").strip()
            if served and served != native:
                # HTTP 200 with a substituted interval. Never accept a coarser
                # series as if it were the interval that was asked for.
                raise ProviderError(
                    f"xaus.com does not serve the {timeframe} interval (it answered with {served}); "
                    "no substitute series is used.",
                    status="ERROR", code=200,
                )
            points = payload.get("points")
            if not isinstance(points, list):
                raise ProviderError("xaus.com returned no candle series.", status="ERROR")
            if points:
                # Score a rung by the series it *normalises to*, which is what
                # the caller actually receives, not by its raw point count: a
                # coarse payload can carry more points yet fewer usable bars.
                usable = self._rows_from_points(points, timeframe)
                newest = usable[-1]["timestamp"] if usable else None
                if native in {"1m", "2m", "5m", "15m", "30m", "60m"} and best:
                    # For intraday series, merge narrower/fresher points (e.g. 1d) with
                    # wider historical points (e.g. 5d/1mo) so recency is never sacrificed
                    # for depth, and depth is never sacrificed for recency.
                    merged_by_t = {p["t"]: p for p in points if isinstance(p, dict) and "t" in p}
                    for p in best:
                        if isinstance(p, dict) and "t" in p:
                            merged_by_t[p["t"]] = p  # fresher points take precedence
                    candidate_points = sorted(merged_by_t.values(), key=lambda x: x["t"])
                    candidate_usable = self._rows_from_points(candidate_points, timeframe)
                    candidate_newest = candidate_usable[-1]["timestamp"] if candidate_usable else None
                    if candidate_newest and (best_newest is None or candidate_newest >= best_newest):
                        best = candidate_points
                        best_usable = len(candidate_usable)
                        best_newest = candidate_newest
                else:
                    if newest is not None and (best_newest is None or newest >= best_newest):
                        if newest > (best_newest or "") or len(usable) > best_usable:
                            best, best_usable, best_newest = points, len(usable), newest
            if best_usable >= needed:
                break
        if not best:
            if last_error is not None:
                raise last_error
            raise ProviderError(
                f"xaus.com returned no {timeframe} candles for XAU/USD.", status="UNAVAILABLE"
            )
        return best

    def _rows_from_points(self, points: list[dict], timeframe: str) -> list[dict]:
        """Convert raw XAUS points into validated-row candidates.

        OHLC relationships and finite/positive prices are enforced downstream by
        ``normalize_candles``; this only reshapes timestamps, drops the
        still-forming intraday bar and de-duplicates daily dates.
        """
        step = TIMEFRAME_SECONDS[timeframe]
        rows: dict[str, dict[str, Any]] = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            try:
                seconds = int(point["t"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError("xaus.com returned a candle with an invalid timestamp.", status="ERROR") from exc
            try:
                stamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OverflowError, OSError, ValueError) as exc:
                raise ProviderError("xaus.com returned a candle with an out-of-range timestamp.", status="ERROR") from exc
            if timeframe == "1d":
                # Daily bars carry a venue-local midnight; normalise to 00:00Z of
                # the same UTC trading date (never a different date). An
                # in-progress daily bar shares its date with the settled bar, so
                # the dict below keeps the newest values for that one date.
                stamp = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
            elif timeframe == "1w":
                # Weekly bars are stamped at Monday 00:00 venue-local time. Keep
                # only the Monday bar for each week (the still-forming point is
                # stamped with a Friday tick time) and normalise to 00:00Z.
                if stamp.weekday() != 0:
                    continue
                stamp = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
            elif seconds % step != 0:
                # The still-forming bar, stamped with the last tick second.
                continue
            try:
                volume = point.get("v")
                rows[stamp.isoformat().replace("+00:00", "Z")] = {
                    "timestamp": stamp.isoformat().replace("+00:00", "Z"),
                    "open": point["o"], "high": point["h"],
                    "low": point["l"], "close": point["c"],
                    "volume": float(volume) if volume is not None else None,
                    # xaus.com is a multi-asset OHLCV feed; the gold series
                    # carries real exchange (futures) volume, which is why VWAP
                    # is available here and stays UNAVAILABLE for price-only
                    # feeds.
                    "volume_type": "exchange" if volume is not None else "unavailable",
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError("xaus.com returned a candle with invalid OHLCV values.", status="ERROR") from exc
        return sorted(rows.values(), key=lambda row: row["timestamp"])

    def _fetch_series(self, symbol: str, timeframe: str, native: str, limit: int,
                      start: str | None, end: str | None) -> list[dict]:
        points = self._fetch_points(timeframe, native, limit)
        rows = self._rows_from_points(points, timeframe)
        start_dt = self._parse_bound(start)
        end_dt = self._parse_bound(end)
        if start_dt is not None:
            rows = [row for row in rows if datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) >= start_dt]
        if end_dt is not None:
            rows = [row for row in rows if datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) <= end_dt]
        if not rows:
            raise ProviderError(
                f"xaus.com returned no {timeframe} candles for {symbol} in the requested window.",
                status="UNAVAILABLE",
            )
        cached_q = self.get_cached_quote(symbol)
        quote_price = float(cached_q["price"]) if cached_q and cached_q.get("price") else None
        try:
            normalized, _warnings = normalize_candles(
                rows, instrument=symbol, timeframe=timeframe, data_source=self.provider_id, quote_price=quote_price
            )
        except PriceScaleMismatchError as exc:
            raise ProviderError(f"xaus.com returned candles with PRICE_SCALE_MISMATCH: {exc}", status="PRICE_SCALE_MISMATCH") from exc
        except DataInvalidError as exc:
            raise ProviderError(f"xaus.com returned candles with DATA_INVALID: {exc}", status="DATA_INVALID") from exc
        except ValueError as exc:
            raise ProviderError(f"xaus.com returned invalid {timeframe} candles: {exc}", status="ERROR") from exc
        return normalized[-limit:]

    def get_historical_candles(self, symbol: str, timeframe: str, limit: int = 240,
                               start: str | None = None, end: str | None = None) -> list[dict]:
        if symbol not in self.symbols:
            raise ProviderError(f"{symbol} is not supported by the xaus.com adapter.")
        native = XAUS_NATIVE_INTERVALS.get(timeframe)
        if native is None:
            supported = ", ".join(XAUS_TIMEFRAMES)
            raise ProviderError(
                f"xaus.com does not serve the {timeframe} interval directly; available intervals are {supported}."
            )
        if not 1 <= int(limit) <= self.MAX_LIMIT:
            raise ProviderError(f"xaus.com limit must be between 1 and {self.MAX_LIMIT}.")
        target = int(limit)
        cache_key = (symbol, timeframe, start, end)
        ranged = bool(start and end)
        now_ts = time.time()
        cached = self._cached_rows(cache_key, target, ranged, now_ts)
        if cached is not None:
            return cached
        with self._key_lock(cache_key):
            now_ts = time.time()
            cached = self._cached_rows(cache_key, target, ranged, now_ts)
            if cached is not None:
                return cached
            try:
                rows = self._fetch_series(symbol, timeframe, native, target, start, end)
                with self._cache_lock:
                    self._candle_cache[cache_key] = (time.time(), list(rows))
                self._save_persistent_cache(cache_key, rows)
                return rows
            except ProviderError as exc:
                # If upstream outage/network failure, retain valid cached history according to cache policy
                if getattr(exc, "status", None) in {"UNAVAILABLE", "DISCONNECTED", "RATE_LIMITED"}:
                    with self._cache_lock:
                        entry = self._candle_cache.get(cache_key)
                    if entry is not None and entry[1]:
                        cached_rows = entry[1]
                        try:
                            validate_xauusd_candles(cached_rows)
                            return list(cached_rows) if ranged else list(cached_rows)[-target:]
                        except Exception:
                            pass
                    disk_cached = self._load_persistent_cache(cache_key, target, ranged)
                    if disk_cached is not None and disk_cached:
                        with self._cache_lock:
                            self._candle_cache[cache_key] = (0.0, list(disk_cached))
                        return disk_cached
                raise exc

    def _quote_from_payload(self, payload: dict[str, Any], symbol: str, timeframe: str) -> dict[str, Any]:
        nested = payload.get("xau") if isinstance(payload.get("xau"), dict) else {}
        raw_price = payload.get("spot_usd_oz")
        if raw_price is None:
            raw_price = nested.get("price")
        if raw_price is None:
            raw_price = payload.get("price")
        try:
            price = float(raw_price)
        except (TypeError, ValueError) as exc:
            raise ProviderError("xaus.com returned a quote with no usable price.", status="UNAVAILABLE") from exc
        if not isfinite(price) or price <= 0:
            raise ProviderError("xaus.com returned a non-positive gold price.", status="ERROR")
        try:
            validate_price_scale([price], instrument=symbol)
        except (PriceScaleMismatchError, DataInvalidError) as exc:
            raise ProviderError(f"xaus.com returned out-of-scale gold price ({price}): {exc}", status="PRICE_SCALE_MISMATCH") from exc

        state = payload.get("data_state") if isinstance(payload.get("data_state"), dict) else {}
        provider_state = str(state.get("status") or "").strip().lower()
        stamp = self._iso_utc(payload.get("price_as_of") or state.get("as_of") or payload.get("updated_at") or payload.get("updatedAt"))
        age: float | None = None
        if stamp is not None:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                age = None
        # The provider's own state decides stale/unavailable; a missing
        # observation timestamp or a provider-flagged stale payload is never
        # promoted to LIVE.
        stale = bool(payload.get("stale")) or provider_state in {"stale", "unavailable"} or stamp is None
        return {
            "symbol": symbol, "instrument": symbol, "timeframe": timeframe,
            "price": price, "timestamp": stamp, "is_stale": stale,
            "quote_currency": "USD", "unit": "troy_ounce",
            "provider": self.provider_id, "data_source": self.provider_id,
            "data_status": "STALE" if stale else "LIVE",
            "provider_data_state": provider_state or ("fresh" if not stale else "stale"),
            "observation_age_seconds": round(age, 1) if age is not None else None,
            "price_source": payload.get("price_source") or state.get("source") or "gold-api.com",
            "price_as_of": stamp,
        }

    def get_latest_quote(self, symbol: str, timeframe: str = "15m") -> dict:
        if symbol not in self.symbols:
            raise ProviderError(f"{symbol} is not supported by the xaus.com adapter.")
        now_ts = time.time()
        with self._cache_lock:
            entry = self._quote_cache.get(symbol)
        if entry and now_ts - entry[0] < self.QUOTE_CACHE_TTL:
            return dict(entry[1])
        with self._key_lock(("quote", symbol)):
            with self._cache_lock:
                entry = self._quote_cache.get(symbol)
            if entry and time.time() - entry[0] < self.QUOTE_CACHE_TTL:
                return dict(entry[1])

            payload = None
            last_err: ProviderError | None = None
            try:
                payload = self._get_json("/api/v1/spot", {"compact": 1}, self.QUOTE_TIMEOUT)
            except ProviderError as exc:
                last_err = exc

            if payload is None:
                # Upstream fallback: query gold-api.com (the documented upstream spot reference for XAUS)
                try:
                    r = httpx.get(
                        f"{self.GOLD_API_BASE}/price/XAU",
                        headers={"Accept": "application/json", "User-Agent": "TradeMindAI/1.0 (+https://gold-api.com)"},
                        timeout=self.QUOTE_TIMEOUT,
                    )
                    if r.is_success:
                        raw = r.json()
                        if isinstance(raw, dict) and "price" in raw:
                            payload = raw
                            payload["price_source"] = "gold-api.com"
                except Exception:
                    pass

            if payload is None:
                # A failed quote request must never overwrite a previously valid quote with null or zero
                with self._cache_lock:
                    cached_entry = self._quote_cache.get(symbol)
                if cached_entry:
                    stale_quote = dict(cached_entry[1])
                    stale_quote["is_stale"] = True
                    stale_quote["data_status"] = "STALE"
                    return stale_quote
                if last_err is not None:
                    raise last_err
                raise ProviderError(
                    "xaus.com has no real gold price to serve right now (upstream unavailable and nothing cached).",
                    retryable=True, status="UNAVAILABLE", code=503,
                )

            quote = self._quote_from_payload(payload, symbol, timeframe)
            with self._cache_lock:
                self._quote_cache[symbol] = (time.time(), dict(quote))
            return quote

    def get_cached_quote(self, symbol: str) -> dict | None:
        """Return the last quote seen in memory without any provider request."""
        if symbol not in self.symbols:
            return None
        with self._cache_lock:
            entry = self._quote_cache.get(symbol)
        return dict(entry[1]) if entry else None


class MarketDataService:
    """Routes by instrument, with explicit DEMO fallback only when no provider is selected."""
    provider_id = "DEMO"

    # Accepted spellings of the configured international provider. SIFTING_API_KEY
    # is an accepted alias for the key value (INTERNATIONAL_DATA_API_KEY stays the
    # primary slot the app already documents).
    _PROVIDER_ALIASES = {
        "TWELVEDATA": "TWELVE_DATA", "TWELVE_DATA": "TWELVE_DATA", "TWELVE_DATA_IO": "TWELVE_DATA",
        "SIFTING": "SIFTING", "SIFTINGIO": "SIFTING", "SIFTING_IO": "SIFTING", "SIFTING.IO": "SIFTING",
        "GOLD": "GOLD", "GOLDPRICE": "GOLD", "GOLDPRICE_DEV": "GOLD", "GOLD_API": "GOLD",
        "XAUS": "XAUS", "XAUS.COM": "XAUS", "XAUSCOM": "XAUS", "XAUS_API": "XAUS",
    }
    _SUPPORTED_INTERNATIONAL = ("XAUS", "GOLD", "TWELVE_DATA", "SIFTING")

    def __init__(self):
        self.demo = DemoMarketDataProvider()
        raw_name = os.getenv("INTERNATIONAL_DATA_PROVIDER", "").strip().upper()
        self.international_provider_name = self._PROVIDER_ALIASES.get(raw_name, raw_name)
        self.international = None
        # Prefer the generic setting; accept SIFTING_API_KEY and the legacy
        # TWELVE_DATA_API_KEY as interchangeable key slots.
        key = (
            os.getenv("INTERNATIONAL_DATA_API_KEY")
            or os.getenv("SIFTING_API_KEY")
            or os.getenv("TWELVE_DATA_API_KEY", "")
        ).strip()
        if self.international_provider_name == "XAUS":
            # xaus.com is keyless and unlimited (fair use), so it is selected
            # even with no key configured. Every other provider still requires
            # one.
            self.international = XausProvider(key)
        elif key and self.international_provider_name == "GOLD":
            self.international = GoldProvider(key)
        elif key and self.international_provider_name == "TWELVE_DATA":
            self.international = TwelveDataProvider(key)
        elif key and self.international_provider_name == "SIFTING":
            self.international = SiftingProvider(key)
        self.indian_provider_name = os.getenv("INDIAN_MARKET_PROVIDER", "").strip().upper()
        self.indian = None
        if self.indian_provider_name in {"ZERODHA", "KITE", "ZERODHA_KITE"}:
            api_key = os.getenv("ZERODHA_API_KEY", "").strip()
            access_token = os.getenv("ZERODHA_ACCESS_TOKEN", "").strip()
            if api_key and access_token:
                self.indian = KiteProvider(api_key, access_token)

    def provider_for(self, symbol: str):
        if symbol in {"EURUSD", "GBPUSD", "XAUUSD"} and self.international_provider_name:
            if self.international_provider_name not in self._SUPPORTED_INTERNATIONAL:
                supported = " or ".join(self._SUPPORTED_INTERNATIONAL)
                raise ProviderError(f"Unsupported INTERNATIONAL_DATA_PROVIDER. Configure {supported}, or clear the provider setting.")
            if self.international is None:
                raise ProviderError("INTERNATIONAL_DATA_PROVIDER is selected but INTERNATIONAL_DATA_API_KEY is missing.")
            return self.international
        if symbol == "NIFTY" or symbol == "BANKNIFTY" or (symbol.isalpha() and symbol.isupper() and symbol not in {"EURUSD", "GBPUSD", "XAUUSD"}):
            if self.indian_provider_name:
                if self.indian_provider_name not in {"ZERODHA", "KITE", "ZERODHA_KITE"}:
                    raise ProviderError("Unsupported INDIAN_MARKET_PROVIDER. Configure ZERODHA_KITE or clear the provider setting.")
                if self.indian is None:
                    raise ProviderError("Indian provider is selected but ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are required.")
                return self.indian
        return self.demo

    def get_instruments(self) -> list[dict]:
        by_symbol = {row["symbol"]: row for row in self.demo.get_instruments()}
        for provider in (self.international, self.indian):
            if provider is not None:
                for row in provider.get_instruments():
                    by_symbol[row["symbol"]] = {**by_symbol.get(row["symbol"], {}), **row}
        return list(by_symbol.values())

    def get_historical_candles(self, symbol: str, timeframe: str, limit: int = 240,
                               start: str | None = None, end: str | None = None) -> list[dict]:
        provider = self.provider_for(symbol)
        plan = AGGREGATION_PLANS.get(provider.provider_id, {}).get(timeframe)
        if plan is not None:
            # Build a derived interval (e.g. Twelve Data 3m) from a supported
            # finer interval (1m). The provider is asked only for the source
            # interval it genuinely supports; incomplete buckets are dropped and
            # never padded (see aggregate_candles).
            source_timeframe, factor = plan
            source_limit = min(int(limit) * factor, 5000)
            source_rows = provider.get_historical_candles(symbol, source_timeframe, source_limit, start, end)
            target_rows, _warnings = aggregate_candles(
                source_rows,
                source_timeframe=source_timeframe,
                target_timeframe=timeframe,
                instrument=symbol,
                data_source=provider.provider_id,
            )
            return target_rows[-int(limit):]
        if provider is self.demo:
            return provider.get_historical_candles(symbol, timeframe, limit)
        return provider.get_historical_candles(symbol, timeframe, limit, start, end)

    def get_latest_quote(self, symbol: str, timeframe: str = "15m") -> dict:
        return self.provider_for(symbol).get_latest_quote(symbol, timeframe)

    def get_cached_quote(self, symbol: str) -> dict | None:
        """Best-effort cached quote; never performs a provider request."""
        try:
            provider = self.provider_for(symbol)
        except ProviderError:
            return None
        getter = getattr(provider, "get_cached_quote", None)
        if getter is None:
            return None
        return getter(symbol)

    def get_market_snapshot(self, symbol: str = "XAUUSD", timeframe: str = "15m", limit: int = 240) -> dict[str, Any]:
        """Authoritative market snapshot combining validated quote and candles."""
        symbol = str(symbol).strip().upper()
        provider = self.provider_for(symbol)
        warnings: list[str] = []

        candles = []
        try:
            candles = self.get_historical_candles(symbol, timeframe, limit=limit)
        except ProviderError as exc:
            warnings.append(f"Historical market data unavailable: {exc}")
        except Exception as exc:
            warnings.append(f"Historical market data unavailable: {exc}")
        quote = None
        try:
            quote = dict(self.get_latest_quote(symbol, timeframe))
        except Exception as exc:
            cached = self.get_cached_quote(symbol)
            if cached and cached.get("price") is not None:
                quote = dict(cached)
                quote["is_stale"] = True
                warnings.append(f"Live quote refresh deferred ({exc}); serving latest cached quote.")
            elif candles:
                last_c = candles[-1]
                quote = {
                    "symbol": symbol,
                    "instrument": symbol,
                    "timeframe": timeframe,
                    "price": float(last_c["close"]),
                    "timestamp": last_c["timestamp"],
                    "is_stale": True,
                    "quote_currency": "USD",
                    "unit": "troy_ounce",
                    "provider": provider.provider_id,
                    "data_source": provider.provider_id,
                    "data_status": "STALE",
                    "observation_age_seconds": None,
                }
                warnings.append("Live quote unavailable; falling back to latest candle close.")

        now_dt = datetime.now(timezone.utc)
        quote_age_seconds: float | None = None
        quote_ts = quote.get("timestamp") if quote else None
        if quote_ts:
            try:
                q_dt = datetime.fromisoformat(str(quote_ts).replace("Z", "+00:00"))
                quote_age_seconds = max(0.0, (now_dt - q_dt).total_seconds())
            except Exception:
                quote_age_seconds = None

        is_quote_stale = bool(quote.get("is_stale")) if quote else True
        if quote_age_seconds is not None and quote_age_seconds > 180:
            is_quote_stale = True

        quote_status = "STALE" if is_quote_stale else "FRESH"
        if provider is self.demo:
            data_status = "DEMO"
        else:
            data_status = "STALE" if is_quote_stale else "LIVE"

        step = TIMEFRAME_SECONDS.get(timeframe, 900)
        last_candle_ts = candles[-1]["timestamp"] if candles else None
        candle_age: float | None = None
        if last_candle_ts:
            try:
                c_dt = datetime.fromisoformat(str(last_candle_ts).replace("Z", "+00:00"))
                candle_age = max(0.0, (now_dt - c_dt).total_seconds())
            except Exception:
                candle_age = None

        candle_stale = (candle_age is None) or (candle_age > step * 4)
        candle_freshness = {
            "status": "UNAVAILABLE" if not candles else ("STALE" if candle_stale else "FRESH"),
            "latest_candle_timestamp": last_candle_ts,
            "age_seconds": round(candle_age, 1) if candle_age is not None else None,
            "bar_seconds": step,
            "stale_threshold_seconds": step * 4,
            "candle_count": len(candles),
        }

        quote_freshness = {
            "status": quote_status,
            "age_seconds": round(quote_age_seconds, 1) if quote_age_seconds is not None else None,
            "timestamp": quote_ts,
            "is_stale": is_quote_stale,
        }

        return {
            "symbol": symbol,
            "instrument": symbol,
            "timeframe": timeframe,
            "provider": provider.provider_id,
            "data_source": provider.provider_id,
            "data_status": data_status,
            "quote": quote,
            "quote_price": float(quote["price"]) if quote and quote.get("price") is not None else None,
            "quote_timestamp": quote_ts,
            "quote_age_seconds": round(quote_age_seconds, 1) if quote_age_seconds is not None else None,
            "quote_status": quote_status,
            "quote_freshness": quote_freshness,
            "candles": candles,
            "candle_freshness": candle_freshness,
            "warnings": warnings,
        }

    def status(self, symbol: str = "XAUUSD") -> dict:
        try:
            provider = self.provider_for(symbol)
            configured = provider is not self.demo
            if not configured:
                message = "Synthetic demonstration data is active."
            elif not getattr(provider, "requires_key", True):
                message = ("This provider needs no API key; live updates are verified from the provider's own "
                           "observation timestamps.")
            else:
                message = "Credentials are configured; realtime stream has not been verified."
            return {"symbol": symbol, "provider": provider.provider_id,
                    "configured": configured, "connection_status": "DISCONNECTED",
                    "data_status": "DEMO" if not configured else "DISCONNECTED",
                    "streaming": False, "last_update": None, "message": message}
        except ProviderError as exc:
            return {"symbol": symbol, "provider": self.international_provider_name or self.indian_provider_name,
                    "configured": False, "connection_status": "ERROR", "data_status": "ERROR",
                    "streaming": False, "last_update": None, "message": str(exc)}

    def stream_diagnostics(self) -> dict[str, Any]:
        """Safe diagnostics for the shared live-stream connection (no secrets)."""
        getter = getattr(self.international, "stream_diagnostics", None)
        if callable(getter):
            try:
                return getter() or {}
            except Exception:
                return {}
        return {}

    def available_timeframes(self, symbol: str) -> list[str] | None:
        """Timeframes the configured provider can genuinely serve for ``symbol``.

        Returns ``None`` when the provider exposes no capability list (the DEMO
        feed and the older adapters serve the app's full frame set). The GOLD
        adapter reports only the intervals its key's plan can actually request,
        so callers never advertise a timeframe the provider would reject.
        """
        try:
            provider = self.provider_for(symbol)
        except ProviderError:
            return None
        getter = getattr(provider, "available_timeframes", None)
        if not callable(getter):
            return None
        try:
            frames = getter()
        except Exception:
            return None
        return list(frames) if frames else None

    def supports_streaming(self, symbol: str) -> bool:
        """Whether the provider for ``symbol`` can push live quotes over a socket."""
        try:
            provider = self.provider_for(symbol)
        except ProviderError:
            return False
        return callable(getattr(provider, "stream_quotes", None))


async def close_market_streams() -> None:
    """Close every shared provider stream cleanly (application shutdown).

    Releasing the upstream SiftingIO connection on shutdown matters because the
    free plan allows only one concurrent connection; a leaked connection would
    make the next start hit ``max_connections`` until the provider expires it.
    """
    with SiftingProvider._stream_hubs_lock:
        hubs = list(SiftingProvider._stream_hubs.values())
    for hub in hubs:
        try:
            await hub.aclose()
        except Exception:  # pragma: no cover - shutdown must never raise
            pass
