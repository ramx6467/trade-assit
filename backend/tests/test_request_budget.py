"""Request-budget tests: deduplication, cache sharing, and credit-free status.

These lock in the fixes that stop the app from spending Twelve Data credits on
repeated or purely presentational requests. Every provider call is mocked, so
running this suite consumes no live API credits.
"""
from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.market_data.providers import ProviderError, TwelveDataProvider

client = TestClient(app)


def _values(interval_minutes: int, count: int, start=(10, 0)) -> list[dict]:
    hour, minute = start
    rows = []
    for index in range(count):
        total = hour * 60 + minute + index * interval_minutes
        hh, mm = divmod(total, 60)
        rows.append({
            "datetime": f"2026-10-16 {hh:02d}:{mm:02d}:00",
            "open": "1.0", "high": "1.2", "low": "0.9", "close": "1.1",
        })
    return rows


def test_candle_cache_is_shared_across_requested_depths():
    """One symbol/interval must not be fetched twice just for a different depth."""
    provider = TwelveDataProvider("fake-key")
    calls = {"n": 0}

    def mock_get_json(endpoint, params, timeout):
        calls["n"] += 1
        return {"status": "ok", "meta": {}, "values": _values(15, 3)}

    provider._get_json = mock_get_json

    deep = provider.get_historical_candles("EURUSD", "15m", 3)
    assert calls["n"] == 1 and len(deep) == 3

    # A shallower request reuses the same cached set (no new credit).
    shallow = provider.get_historical_candles("EURUSD", "15m", 1)
    assert calls["n"] == 1 and len(shallow) == 1
    assert shallow[0]["timestamp"] == deep[-1]["timestamp"]

    # A deeper request than the cache holds must refresh.
    provider.get_historical_candles("EURUSD", "15m", 5)
    assert calls["n"] == 2


def test_simultaneous_identical_requests_share_one_provider_call():
    """Concurrent identical requests are deduplicated to a single credit."""
    provider = TwelveDataProvider("fake-key")
    calls = {"n": 0}

    def mock_get_json(endpoint, params, timeout):
        calls["n"] += 1
        time.sleep(0.15)  # hold the in-flight lock so peers actually queue
        return {"status": "ok", "meta": {}, "values": _values(1, 2)}

    provider._get_json = mock_get_json
    results: list[list[dict]] = []

    def worker():
        results.append(provider.get_historical_candles("EURUSD", "1m", 2))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls["n"] == 1
    assert len(results) == 6 and all(len(rows) == 2 for rows in results)


def test_status_endpoint_does_not_spend_a_quote_credit(monkeypatch):
    """Rendering status must never issue a provider quote request."""
    quote_calls = {"n": 0}
    monkeypatch.setattr(main.market_data, "status", lambda sym: {
        "symbol": sym, "provider": "TWELVE_DATA", "configured": True,
        "connection_status": "CONNECTED", "data_status": "DELAYED",
        "streaming": False, "message": "configured"})

    def boom(*args, **kwargs):
        quote_calls["n"] += 1
        raise AssertionError("status must not fetch a live quote")

    monkeypatch.setattr(main.market_data, "get_latest_quote", boom)

    response = client.get("/api/v1/market-data/status?symbol=XAUUSD")
    assert response.status_code == 200
    assert quote_calls["n"] == 0


def test_instrument_rows_do_not_fetch_quotes_for_configured_providers(monkeypatch):
    """The Markets list must not issue one quote request per instrument."""
    quote_calls = {"n": 0}
    monkeypatch.setattr(main.market_data, "get_instruments", lambda: [
        {"symbol": "XAUUSD", "name": "XAU/USD"},
        {"symbol": "GBPUSD", "name": "GBP/USD"},  # no longer supported: dropped, never quoted
    ])
    monkeypatch.setattr(main.market_data, "status", lambda sym: {
        "symbol": sym, "provider": "GOLD", "configured": True,
        "connection_status": "CONNECTED", "data_status": "DELAYED",
        "streaming": False, "message": "configured"})

    def boom(*args, **kwargs):
        quote_calls["n"] += 1
        raise AssertionError("instrument rows must not fetch live quotes")

    monkeypatch.setattr(main.market_data, "get_latest_quote", boom)

    rows = main._instrument_rows()
    assert [row["symbol"] for row in rows] == ["XAUUSD"]
    assert quote_calls["n"] == 0
    assert all(row["price"] is None for row in rows)


def test_provider_failure_returns_explicit_error_not_stale_cache(monkeypatch):
    """A provider failure surfaces as an explicit error, never as stale candles.

    Serving a stale cache would let indicators and BUY/SELL evaluation run on
    data the provider just failed to refresh, which is exactly the "error
    candles" behaviour we must not produce.
    """
    monkeypatch.setattr(main, "_market_data_errors", {})

    def fail(*args, **kwargs):
        raise ProviderError("quota", status="QUOTA_EXCEEDED", code=429)

    monkeypatch.setattr(main.market_data, "get_historical_candles", fail)

    rows, warnings = main._candles("EURUSD", "15m", 2)
    assert rows == []
    assert any("Historical market data unavailable" in warning for warning in warnings)
