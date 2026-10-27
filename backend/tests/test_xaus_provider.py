"""XAUS (xaus.com) adapter tests.

Every network call is mocked: these tests pin the *parsing and honesty rules*
of the adapter (what it accepts, what it rejects, what it refuses to invent).
The real service is exercised separately by the live verification run, because
mocked responses are never evidence that a live feed works.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.market_data.providers import MarketDataService, ProviderError, XausProvider

SPOT_URL = "https://xaus.com/api/v1/spot"
CHART_URL = "https://xaus.com/api/v1/chart"


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


BASE_GOLD = 2600.0


def _point(iso: str, o, h, l, c, v=100):
    actual_o = BASE_GOLD + o if o < 500 else o
    actual_h = BASE_GOLD + h if h < 500 else h
    actual_l = BASE_GOLD + l if l < 500 else l
    actual_c = BASE_GOLD + c if c < 500 else c
    return {"t": _epoch(iso), "o": actual_o, "h": actual_h, "l": actual_l, "c": actual_c, "v": v}


class _Response:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload


def _patch_chart(monkeypatch, points, *, interval, calls=None, status=200):
    def get(url, *, params=None, headers=None, timeout=None):
        assert url == CHART_URL
        if calls is not None:
            calls.append(dict(params or {}))
        return _Response(
            {"symbol": "xau", "interval": interval, "range": (params or {}).get("range"),
             "points": points, "data_state": {"status": "fresh"}},
            status_code=status,
        )

    monkeypatch.setattr("app.market_data.providers.httpx.get", get)


def _patch_spot(monkeypatch, payload, *, status=200):
    def get(url, *, params=None, headers=None, timeout=None):
        assert url == SPOT_URL
        assert params == {"compact": 1}
        return _Response(payload, status_code=status)

    monkeypatch.setattr("app.market_data.providers.httpx.get", get)


# --- keyless selection ------------------------------------------------------


def test_xaus_is_keyless_and_needs_no_api_key(monkeypatch):
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "xaus")
    monkeypatch.setenv("INTERNATIONAL_DATA_API_KEY", "")
    service = MarketDataService()
    assert isinstance(service.international, XausProvider)
    status = service.status("XAUUSD")
    assert status["configured"] is True
    assert "no API key" in status["message"]
    # No WebSocket is invented: xaus.com is REST only.
    assert service.supports_streaming("XAUUSD") is False


def test_xaus_advertises_every_requested_timeframe():
    frames = XausProvider().available_timeframes()
    for expected in ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"):
        assert expected in frames


def test_xaus_rejects_unsupported_symbol_and_interval():
    provider = XausProvider()
    try:
        provider.get_historical_candles("EURUSD", "1m", 10)
        assert False, "Only XAUUSD may be served."
    except ProviderError as exc:
        assert "EURUSD is not supported" in str(exc)
    try:
        # 2h is not a native /chart interval; the provider must refuse it and
        # let the service layer aggregate it instead.
        provider.get_historical_candles("XAUUSD", "2h", 10)
        assert False, "Derived intervals must not be requested from the provider."
    except ProviderError as exc:
        assert "2h" in str(exc)


def test_xaus_rejects_a_silently_substituted_interval(monkeypatch):
    # The live API answers an unsupported interval with HTTP 200 and interval
    # "1d"; that fallback must never be accepted as the requested series.
    _patch_chart(monkeypatch, [_point("2026-10-16T00:00:00Z", 1, 2, 0.5, 1.5)], interval="1d")
    try:
        XausProvider().get_historical_candles("XAUUSD", "1h", 10)
        assert False, "A substituted interval must raise."
    except ProviderError as exc:
        assert "does not serve" in str(exc)


def test_xaus_rejects_invalid_ohlc(monkeypatch):
    bad = [_point("2026-10-16T10:00:00Z", o=10, h=9, l=8, c=9.5)]  # high < close
    _patch_chart(monkeypatch, bad, interval="1m")
    try:
        XausProvider().get_historical_candles("XAUUSD", "1m", 10)
        assert False, "Inconsistent OHLC must be rejected."
    except ProviderError as exc:
        assert "invalid" in str(exc).lower()


def test_xaus_drops_the_still_forming_intraday_bar(monkeypatch):
    rows_in = [
        _point("2026-10-16T10:00:00Z", 1, 2, 0.5, 1.5),
        _point("2026-10-16T10:01:00Z", 1.5, 2, 1, 1.8),
        # Forming bar stamped at the last tick second, not the bar open.
        {"t": _epoch("2026-10-16T10:02:00Z") + 59, "o": 1.8, "h": 1.9, "l": 1.7, "c": 1.85, "v": 5},
    ]
    _patch_chart(monkeypatch, rows_in, interval="1m")
    rows = XausProvider().get_historical_candles("XAUUSD", "1m", 10)
    assert [r["timestamp"] for r in rows] == ["2026-10-16T10:00:00Z", "2026-10-16T10:01:00Z"]


def test_xaus_normalizes_daily_bars_to_the_utc_date_and_dedupes(monkeypatch):
    # Daily bars arrive at venue-local midnights (04:00Z, 05:00Z, 14:30Z across
    # DST and early closes) and the forming bar repeats the last date.
    rows_in = [
        _point("2025-11-26T05:00:00Z", 1, 2, 0.5, 1.5),
        _point("2025-11-28T14:30:00Z", 1.5, 2, 1, 1.8),
        _point("2025-12-01T05:00:00Z", 1.8, 2, 1, 1.9),
        _point("2025-12-01T20:59:59Z", 1.8, 2.2, 1, 2.1),  # same date, forming
    ]
    _patch_chart(monkeypatch, rows_in, interval="1d")
    rows = XausProvider().get_historical_candles("XAUUSD", "1d", 10)
    assert [r["timestamp"] for r in rows] == [
        "2025-11-26T00:00:00Z",
        "2025-11-28T00:00:00Z",
        "2025-12-01T00:00:00Z",
    ]
    # The newest values for that date win (the settled/forming bar is preferred).
    assert rows[-1]["close"] == BASE_GOLD + 2.1


def test_xaus_weekly_bars_keep_only_mondays(monkeypatch):
    rows_in = [
        _point("2026-10-05T04:00:00Z", 1, 2, 0.5, 1.5),   # Monday
        _point("2026-10-12T04:00:00Z", 1.5, 2, 1, 1.8),   # Monday
        _point("2026-10-16T20:59:59Z", 1.8, 2, 1, 1.9),   # forming, a Friday
    ]
    _patch_chart(monkeypatch, rows_in, interval="1wk")
    rows = XausProvider().get_historical_candles("XAUUSD", "1w", 10)
    # Only the two Monday bars survive; the still-forming Friday point is dropped.
    assert [r["timestamp"] for r in rows] == ["2026-10-05T00:00:00Z", "2026-10-12T00:00:00Z"]


def test_xaus_range_ladder_falls_back_when_a_window_is_refused(monkeypatch):
    calls = []

    def get(url, *, params=None, headers=None, timeout=None):
        calls.append(dict(params or {}))
        if params["range"] == "1d":
            return _Response({"error": "upstream unavailable"}, status_code=503)
        return _Response({
            "symbol": "xau", "interval": "1m", "range": params["range"],
            "points": [_point("2026-10-16T10:00:00Z", 1, 2, 0.5, 1.5)],
        })

    monkeypatch.setattr("app.market_data.providers.httpx.get", get)
    rows = XausProvider().get_historical_candles("XAUUSD", "1m", 10)
    assert [c["range"] for c in calls] == ["1d", "5d"]
    assert rows and rows[-1]["close"] == BASE_GOLD + 1.5


def test_xaus_classifies_rate_limit_and_upstream_outage():
    assert XausProvider._error(429, {}).status == "RATE_LIMITED"
    outage = XausProvider._error(503, {"data_state": {"status": "unavailable"}})
    assert outage.status == "UNAVAILABLE"
    assert outage.retryable is True
    assert XausProvider._error(400, {"error": "bad interval"}).status == "ERROR"


def test_xaus_quote_parses_price_and_observation_time(monkeypatch):
    seen = {}

    def get(url, *, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["headers"] = headers
        return _Response({
            "spot_usd_oz": 4166.1,
            "price_as_of": "2026-10-18T07:53:01.769Z",
            "updated_at": "2026-10-18T07:53:01.769Z",
            "stale": False,
            "price_source": "gold-api.com",
            "data_state": {"status": "fresh", "as_of": "2026-10-18T07:53:01.769Z", "source": "upstream"},
        })

    monkeypatch.setattr("app.market_data.providers.httpx.get", get)
    quote = XausProvider().get_latest_quote("XAUUSD", "15m")
    assert seen["url"] == SPOT_URL and seen["params"] == {"compact": 1}
    assert "Authorization" not in seen["headers"]  # keyless: nothing to leak
    assert quote["price"] == 4166.1
    assert quote["timestamp"] == "2026-10-18T07:53:01.769Z"
    assert quote["is_stale"] is False and quote["data_status"] == "LIVE"
    assert quote["unit"] == "troy_ounce" and quote["quote_currency"] == "USD"


def test_xaus_quote_is_never_promoted_past_the_provider_state(monkeypatch):
    monkeypatch.setattr(
        "app.market_data.providers.httpx.get",
        lambda *a, **k: _Response({
            "spot_usd_oz": 4000.0,
            "price_as_of": "2026-10-16T20:59:00Z",
            "stale": True,
            "data_state": {"status": "stale"},
        }),
    )
    quote = XausProvider().get_latest_quote("XAUUSD", "15m")
    assert quote["is_stale"] is True and quote["data_status"] == "STALE"


def test_xaus_quote_without_a_provider_timestamp_is_stale(monkeypatch):
    monkeypatch.setattr(
        "app.market_data.providers.httpx.get",
        lambda *a, **k: _Response({"spot_usd_oz": 4000.0, "data_state": {"status": "fresh"}}),
    )
    quote = XausProvider().get_latest_quote("XAUUSD", "15m")
    assert quote["timestamp"] is None and quote["is_stale"] is True


def test_xaus_quote_rejects_missing_or_non_positive_price(monkeypatch):
    monkeypatch.setattr(
        "app.market_data.providers.httpx.get",
        lambda *a, **k: _Response({"data_state": {"status": "fresh"}}),
    )
    try:
        XausProvider().get_latest_quote("XAUUSD", "15m")
        assert False
    except ProviderError as exc:
        assert "no usable price" in str(exc)
    monkeypatch.setattr(
        "app.market_data.providers.httpx.get",
        lambda *a, **k: _Response({"spot_usd_oz": 0, "price_as_of": "2026-10-18T07:53:01Z"}),
    )
    try:
        XausProvider().get_latest_quote("XAUUSD", "15m")
        assert False
    except ProviderError as exc:
        assert "non-positive" in str(exc)


def test_xaus_rejects_a_millisecond_timestamp_instead_of_misreading_it(monkeypatch):
    # A milliseconds epoch (~1.7e12) is out of range for a *seconds* value. It
    # must be rejected, never silently divided and re-stamped as the wrong bar.
    ms = _epoch("2026-10-16T10:00:00Z") * 1000
    _patch_chart(monkeypatch, [{"t": ms, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}], interval="1m")
    try:
        XausProvider().get_historical_candles("XAUUSD", "1m", 10)
        assert False, "A millisecond timestamp must not be accepted as seconds."
    except ProviderError as exc:
        assert "out-of-range" in str(exc).lower() or "timestamp" in str(exc).lower()


def test_xaus_sorts_out_of_order_points(monkeypatch):
    _patch_chart(monkeypatch, [
        _point("2026-10-16T10:05:00Z", 1.5, 2, 1, 1.8),
        _point("2026-10-16T10:00:00Z", 1, 2, 0.5, 1.5),
    ], interval="5m")
    rows = XausProvider().get_historical_candles("XAUUSD", "5m", 10)
    assert [r["timestamp"] for r in rows] == ["2026-10-16T10:00:00Z", "2026-10-16T10:05:00Z"]


def test_xaus_dedupes_a_repeated_bar_keeping_the_newest(monkeypatch):
    # The same 5m bar repeated with an updated close: the newest values win and
    # the bar appears exactly once.
    _patch_chart(monkeypatch, [
        _point("2026-10-16T10:00:00Z", 1, 2, 0.5, 1.5),
        _point("2026-10-16T10:00:00Z", 1, 2.2, 0.5, 1.9),
    ], interval="5m")
    rows = XausProvider().get_historical_candles("XAUUSD", "5m", 10)
    assert [r["timestamp"] for r in rows] == ["2026-10-16T10:00:00Z"]
    assert rows[0]["close"] == BASE_GOLD + 1.9 and rows[0]["high"] == BASE_GOLD + 2.2


def test_xaus_serves_5m_natively_without_aggregation(monkeypatch):
    """5m is a native /chart interval, so it is requested directly.

    The service must ask xaus.com for interval=5m and pass the native OHLC
    through untouched, never substituting an aggregated 1m series.
    """
    calls = []
    _patch_chart(
        monkeypatch,
        [
            _point("2026-10-16T10:00:00Z", 1.0, 1.2, 0.9, 1.1),
            _point("2026-10-16T10:05:00Z", 1.1, 1.3, 1.0, 1.25),
        ],
        interval="5m",
        calls=calls,
    )
    rows = XausProvider().get_historical_candles("XAUUSD", "5m", 2)
    assert [c["interval"] for c in calls] == ["5m"]
    assert [r["timestamp"] for r in rows] == ["2026-10-16T10:00:00Z", "2026-10-16T10:05:00Z"]
    # Native OHLC is preserved exactly - a bucket's close is the feed's close.
    assert (rows[0]["open"], rows[0]["high"], rows[0]["low"], rows[0]["close"]) == (BASE_GOLD + 1.0, BASE_GOLD + 1.2, BASE_GOLD + 0.9, BASE_GOLD + 1.1)
    assert rows[-1]["close"] == BASE_GOLD + 1.25


def test_xaus_service_builds_3m_from_real_1m_candles(monkeypatch):
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "xaus")
    minutes = [
        _point("2026-10-16T10:00:00Z", 1.0, 1.2, 0.9, 1.1),
        _point("2026-10-16T10:01:00Z", 1.1, 1.3, 1.0, 1.2),
        _point("2026-10-16T10:02:00Z", 1.2, 1.4, 1.1, 1.3),
        _point("2026-10-16T10:03:00Z", 1.3, 1.5, 1.2, 1.4),
        _point("2026-10-16T10:04:00Z", 1.4, 1.6, 1.3, 1.5),
        _point("2026-10-16T10:05:00Z", 1.5, 1.7, 1.4, 1.6),
    ]
    _patch_chart(monkeypatch, minutes, interval="1m")
    rows = MarketDataService().get_historical_candles("XAUUSD", "3m", 2)
    assert [r["timestamp"] for r in rows] == ["2026-10-16T10:00:00Z", "2026-10-16T10:03:00Z"]
    first = rows[0]
    # OHLC of an aggregated bucket: first open, max high, min low, last close.
    assert (first["open"], first["high"], first["low"], first["close"]) == (BASE_GOLD + 1.0, BASE_GOLD + 1.4, BASE_GOLD + 0.9, BASE_GOLD + 1.3)
    assert first["volume"] == 300
    assert first["volume_type"] == "exchange"


def test_xaus_service_drops_incomplete_aggregation_buckets(monkeypatch):
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "xaus")
    hours = [
        _point("2026-10-16T10:00:00Z", 1, 2, 0.5, 1.5),
        _point("2026-10-16T11:00:00Z", 1.5, 2, 1, 1.6),
        _point("2026-10-16T12:00:00Z", 1.6, 2, 1, 1.7),
        # 13:00 is missing -> the 12:00-16:00 4h bucket is incomplete.
        _point("2026-10-16T14:00:00Z", 1.7, 2, 1, 1.8),
        _point("2026-10-16T15:00:00Z", 1.8, 2, 1, 1.9),
    ]
    _patch_chart(monkeypatch, hours, interval="60m")
    rows = MarketDataService().get_historical_candles("XAUUSD", "4h", 5)
    # Only the complete 12:00 UTC bucket (12:00,13:00,14:00,15:00) qualifies and
    # it is missing 13:00, so nothing is emitted - never a padded candle.
    assert rows == []


def _weekly_mondays(iso: str, count: int) -> list:
    """``count`` genuine Monday weekly bars ending at ``iso`` (ascending)."""
    last = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    stamps = [last - timedelta(weeks=offset) for offset in range(count)]
    return [
        _point(stamp.isoformat().replace("+00:00", "Z"), 1, 2, 0.5, 1.5)
        for stamp in reversed(stamps)
    ]


def _monthly_firsts(start: str, end: str) -> list:
    """Month-start bars from ``start`` to ``end`` inclusive (mostly not Mondays)."""
    cursor = datetime.fromisoformat(start.replace("Z", "+00:00"))
    stop = datetime.fromisoformat(end.replace("Z", "+00:00"))
    out = []
    while cursor <= stop:
        out.append(_point(cursor.isoformat().replace("+00:00", "Z"), 1, 2, 0.5, 1.5))
        month = cursor.month + 1
        year = cursor.year + (month - 1) // 12
        cursor = cursor.replace(year=year, month=(month - 1) % 12 + 1)
    return out


def test_xaus_widest_weekly_window_never_costs_the_newest_bar(monkeypatch):
    """Asking for *more* weekly bars must not return an *older* weekly series.

    xaus.com answers range=max with interval=1wk but a coarse series (~269 points
    over 26 years) whose newest Monday is months older than the 5-year window's.
    Fast-forwarding the ladder straight to that rung therefore replaced a current
    weekly series with an ancient one - purely as a side effect of the requested
    limit. A wider window may only ever add history.
    """
    dense = _weekly_mondays("2026-10-12T04:00:00Z", 60)
    coarse = _monthly_firsts("2000-09-01T04:00:00Z", "2026-06-01T04:00:00Z")
    # The coarse payload carries more raw points yet fewer usable weekly bars.
    assert len(coarse) > len(dense)
    coarse_mondays = [
        p for p in coarse if datetime.fromtimestamp(p["t"], tz=timezone.utc).weekday() == 0
    ]
    assert max(p["t"] for p in coarse_mondays) == _epoch("2026-06-01T04:00:00Z")

    calls = []

    def get(url, *, params=None, headers=None, timeout=None):
        calls.append(params["range"])
        points = dense if params["range"] == "5y" else coarse
        return _Response({
            "symbol": "xau", "interval": "1wk", "range": params["range"],
            "points": points, "data_state": {"status": "fresh"},
        })

    monkeypatch.setattr("app.market_data.providers.httpx.get", get)
    rows = XausProvider().get_historical_candles("XAUUSD", "1w", 1000)
    assert [r["timestamp"] for r in rows] == [
        r["timestamp"] for r in XausProvider()._rows_from_points(dense, "1w")
    ]
    assert rows[-1]["timestamp"] == "2026-10-12T00:00:00Z"
    # The coarse window was still consulted (so it is genuinely rejected, not skipped).
    assert "max" in calls
    assert calls[0] == "5y"
