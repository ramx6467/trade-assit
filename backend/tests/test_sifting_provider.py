"""SiftingIO adapter tests.

Every HTTP call is mocked, so this suite consumes no live SiftingIO requests.
The live integration was verified separately against the real API.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.market_data.providers import (
    AGGREGATION_PLANS,
    SIFTING_INTERVALS,
    MarketDataService,
    ProviderError,
    SiftingProvider,
)


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _bar(iso: str, o=1.0, h=1.2, low=0.9, c=1.1, v=100) -> dict:
    return {"t": _ms(iso), "o": o, "h": h, "l": low, "c": c, "v": v}


def _response(status_code: int, payload: dict):
    class Response:
        is_success = 200 <= status_code < 300

        def json(self):
            return payload

    Response.status_code = status_code
    return Response()


def test_service_reads_sifting_provider_and_generic_key(monkeypatch):
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "sifting")
    monkeypatch.setenv("INTERNATIONAL_DATA_API_KEY", "unit-test-secret")
    monkeypatch.delenv("SIFTING_API_KEY", raising=False)
    service = MarketDataService()
    assert isinstance(service.international, SiftingProvider)
    status = service.status("EURUSD")
    assert status["configured"] is True and status["provider"] == "SIFTING"


def test_service_accepts_sifting_api_key_alias(monkeypatch):
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "SIFTING")
    monkeypatch.setenv("INTERNATIONAL_DATA_API_KEY", "")
    monkeypatch.setenv("SIFTING_API_KEY", "alias-secret")
    service = MarketDataService()
    assert isinstance(service.international, SiftingProvider)


def test_missing_key_is_reported_and_never_falls_back_to_demo(monkeypatch):
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "SIFTING")
    monkeypatch.setenv("INTERNATIONAL_DATA_API_KEY", "")
    monkeypatch.delenv("SIFTING_API_KEY", raising=False)
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    service = MarketDataService()
    assert service.status("EURUSD")["data_status"] == "ERROR"
    try:
        service.get_historical_candles("EURUSD", "1m")
        assert False, "SiftingIO must not silently return DEMO rows."
    except ProviderError as exc:
        assert "INTERNATIONAL_DATA_API_KEY" in str(exc)


def test_unsupported_provider_name_is_rejected(monkeypatch):
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "somethingelse")
    monkeypatch.setenv("INTERNATIONAL_DATA_API_KEY", "unit-test-secret")
    service = MarketDataService()
    try:
        service.get_historical_candles("EURUSD", "1m")
        assert False
    except ProviderError as exc:
        assert "Unsupported INTERNATIONAL_DATA_PROVIDER" in str(exc)


def test_bars_request_maps_venue_headers_interval_and_normalizes(monkeypatch):
    captured: dict = {}

    class Response:
        status_code = 200
        is_success = True

        def json(self):
            return {"data": [
                {"t": _ms("2026-10-16T10:02:00Z"), "o": 1.2, "h": 1.3, "l": 1.15, "c": 1.25, "v": 999},
                {"t": _ms("2026-10-16T10:00:00Z"), "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": 500},
                {"t": _ms("2026-10-16T10:01:00Z"), "o": 1.1, "h": 1.25, "l": 1.0, "c": 1.2, "v": 700},
            ]}

    def get(url, *, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return Response()

    monkeypatch.setattr("app.market_data.providers.httpx.get", get)
    provider = SiftingProvider("unit-test-secret")
    rows = provider.get_historical_candles("EURUSD", "1m", 3)

    assert captured["url"] == "https://api.sifting.io/v1/hist/forex/EURUSD/bars"
    assert captured["params"]["interval"] == "1m"
    assert captured["params"]["order"] == "desc"  # plain request = most recent first
    assert captured["params"]["limit"] == 3
    assert "start" in captured["params"] and "end" in captured["params"]
    assert captured["headers"]["X-API-Key"] == "unit-test-secret"
    assert captured["headers"]["Accept-Encoding"] == "gzip"
    assert "unit-test-secret" not in captured["url"]
    # Rows are returned oldest-first, tagged, and never carry tick count as volume.
    assert [row["timestamp"] for row in rows] == [
        "2026-10-16T10:00:00Z", "2026-10-16T10:01:00Z", "2026-10-16T10:02:00Z",
    ]
    assert rows[0]["data_source"] == "SIFTING" and rows[0]["volume"] is None
    assert rows[0]["volume_type"] == "unavailable"


def test_xauusd_uses_commodities_venue(monkeypatch):
    captured: dict = {}

    class Response:
        status_code = 200
        is_success = True

        def json(self):
            return {"data": [{"t": _ms("2026-10-16T00:00:00Z"), "o": 4100.0, "h": 4110.0, "l": 4090.0, "c": 4105.0, "v": 10}]}

    monkeypatch.setattr("app.market_data.providers.httpx.get",
                        lambda url, *, params, headers, timeout: (captured.update(url=url), Response())[1])
    SiftingProvider("k").get_historical_candles("XAUUSD", "1d", 1)
    assert "/v1/hist/commodities/XAUUSD/bars" in captured["url"]


def test_sifting_provider_reports_no_market_session_for_spot_metals():
    # SiftingIO's documented market-hours catalog (23 venues) covers equity
    # exchanges, forex and crypto only - there is no commodities/metals market -
    # so XAUUSD has no provider-documented schedule and must stay
    # SCHEDULE_UNKNOWN rather than receive an invented metals calendar.
    provider = SiftingProvider("k")
    assert provider.get_market_session("XAUUSD") is None
    # FX keeps its own documented venue convention, so the hook stays empty here.
    assert provider.get_market_session("EURUSD") is None


def test_provider_rejects_unsupported_interval_and_symbol():
    provider = SiftingProvider("k")
    for timeframe in ("3m", "4h"):
        try:
            provider.get_historical_candles("EURUSD", timeframe, 10)
            assert False, "SiftingIO does not serve 3m/4h directly."
        except ProviderError as exc:
            assert "does not support" in str(exc)
    try:
        provider.get_historical_candles("UNKNOWN", "1m")
        assert False
    except ProviderError as exc:
        assert "UNKNOWN" in str(exc)


def test_empty_window_raises_instead_of_fabricating(monkeypatch):
    monkeypatch.setattr("app.market_data.providers.httpx.get",
                        lambda *a, **k: _response(200, {"data": [], "meta": {"symbol": "EURUSD"}}))
    try:
        SiftingProvider("k").get_historical_candles("EURUSD", "1h", 10)
        assert False
    except ProviderError as exc:
        assert "no candles" in str(exc)


def test_error_classification_is_safe_and_code_specific():
    assert "authentication failed" in str(SiftingProvider._error(401, {"error": "unauthorized"})).lower()
    assert SiftingProvider._error(404, {"error": "not_found"}).code == 404
    gzip_err = SiftingProvider._error(406, {"error": "gzip_required"})
    assert gzip_err.status == "ERROR" and "gzip" in str(gzip_err).lower()
    rate = SiftingProvider._error(429, {"error": "rate_limit_exceeded"})
    assert rate.status == "RATE_LIMITED" and rate.retryable is True
    quota = SiftingProvider._error(429, {"error": "quota exhausted"})
    assert quota.status == "QUOTA_EXCEEDED" and quota.retryable is False
    # Provider messages must not echo arbitrary upstream detail verbatim.
    secret = SiftingProvider._error(500, {"error": "internal", "message": "secret backend detail"})
    assert "secret backend detail" not in str(secret)


def test_service_builds_3m_and_4h_from_real_supported_candles(monkeypatch):
    requested: list[str] = []

    class Response:
        status_code = 200
        is_success = True

        def __init__(self, interval):
            self._interval = interval

        def json(self):
            if self._interval == "1m":
                rows = [_ms(f"2026-10-16T10:0{i}:00Z") for i in range(6)]
            else:  # 1h
                rows = [_ms(f"2026-10-16T0{i}:00:00Z") for i in range(4)]
            return {"data": [
                {"t": t, "o": 1.0 + 0.01 * i, "h": 1.5, "l": 0.9, "c": 1.2, "v": 1}
                for i, t in enumerate(rows)
            ]}

    def _capture(url, *, params, headers, timeout):
        captured["intervals"].append(params["interval"])
        return Response(params["interval"])

    captured = {"intervals": []}
    monkeypatch.setattr("app.market_data.providers.httpx.get", _capture)
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "SIFTING")
    monkeypatch.setenv("INTERNATIONAL_DATA_API_KEY", "unit-test-secret")
    service = MarketDataService()

    rows_3m = service.get_historical_candles("EURUSD", "3m", 10)
    rows_4h = service.get_historical_candles("EURUSD", "4h", 10)

    # The provider is only ever asked for intervals it genuinely supports.
    assert captured["intervals"] == ["1m", "1h"]
    assert [row["timestamp"] for row in rows_3m] == ["2026-10-16T10:00:00Z", "2026-10-16T10:03:00Z"]
    assert all(row["timeframe"] == "3m" and row["data_source"] == "SIFTING" for row in rows_3m)
    assert [row["timestamp"] for row in rows_4h] == ["2026-10-16T00:00:00Z"]
    assert rows_4h[0]["timeframe"] == "4h"


def test_latest_quote_parses_last_trade(monkeypatch):
    class Response:
        status_code = 200
        is_success = True

        def json(self):
            return {"s": "EURUSD", "p": "1.12388", "P": "81850", "t": _ms("2026-10-17T16:24:00Z")}

    captured = {}
    monkeypatch.setattr("app.market_data.providers.httpx.get",
                        lambda url, *, params, headers, timeout: (captured.update(url=url), Response())[1])
    quote = SiftingProvider("k").get_latest_quote("EURUSD", "15m")
    assert "/v1/last/trade/forex/EURUSD" in captured["url"]
    assert quote["price"] == 1.12388 and quote["provider"] == "SIFTING"
    assert quote["timestamp"] == "2026-10-17T16:24:00Z"
    assert quote["streaming"] is False


def test_stale_tape_falls_back_to_newest_completed_bar(monkeypatch):
    calls = {"n": 0}

    def get(url, *, params, headers, timeout):
        calls["n"] += 1
        if "/v1/last/trade/" in url:
            return _response(503, {"error": "stale_snapshot", "last_t": 1, "server_now": 2})
        return _response(200, {"data": [{"t": _ms("2026-10-17T16:20:00Z"), "o": 1.1, "h": 1.11, "l": 1.09, "c": 1.105, "v": 3}]})

    monkeypatch.setattr("app.market_data.providers.httpx.get", get)
    quote = SiftingProvider("k").get_latest_quote("EURUSD", "1m")
    assert quote["price"] == 1.105
    assert quote["timestamp"] == "2026-10-17T16:20:00Z"
    assert "1-minute bar" in quote["note"]
    assert calls["n"] == 2