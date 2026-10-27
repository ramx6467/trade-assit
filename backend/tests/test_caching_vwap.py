"""Tests for Twelve Data caching, credit limit handling, and VWAP availability."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
import pytest

from app.main import app, _market_data_errors, _last_market_updates
from app.market_data.providers import TwelveDataProvider, ProviderError
from app.analysis.indicators import vwap

client = TestClient(app)


def test_twelve_data_candle_caching():
    provider = TwelveDataProvider("fake-key")
    call_count = 0

    def mock_get_json(endpoint, params, timeout):
        nonlocal call_count
        call_count += 1
        return {
            "status": "ok",
            "meta": {"symbol": "EUR/USD", "interval": "15min"},
            "values": [
                {"datetime": "2026-10-16 10:00:00", "open": "1.1000", "high": "1.1050", "low": "1.0990", "close": "1.1020"},
                {"datetime": "2026-10-16 10:15:00", "open": "1.1020", "high": "1.1060", "low": "1.1010", "close": "1.1040"},
            ],
        }

    provider._get_json = mock_get_json

    # First call - should invoke _get_json
    c1 = provider.get_historical_candles("EURUSD", "15m", 2)
    assert len(c1) == 2
    assert call_count == 1

    # Second call - should hit in-memory cache without calling _get_json
    c2 = provider.get_historical_candles("EURUSD", "15m", 2)
    assert len(c2) == 2
    assert call_count == 1  # Unchanged!


def test_twelve_data_quote_caching():
    provider = TwelveDataProvider("fake-key")
    call_count = 0

    def mock_get_json(endpoint, params, timeout):
        nonlocal call_count
        call_count += 1
        return {"price": "1.1035"}

    provider._get_json = mock_get_json

    q1 = provider.get_latest_quote("EURUSD", "15m")
    assert q1["price"] == 1.1035
    assert call_count == 1

    # Second call within 15s should hit cache
    q2 = provider.get_latest_quote("EURUSD", "15m")
    assert q2["price"] == 1.1035
    assert call_count == 1  # Unchanged!


def test_twelve_data_credit_exhaustion_vs_rate_limit():
    # Credit exhaustion payload
    err_quota = TwelveDataProvider._error(
        429,
        {"code": 429, "message": "You have run out of API credits for today. 800 API credits were used.", "status": "error"}
    )
    assert err_quota.status == "QUOTA_EXCEEDED"
    assert "credit limit" in str(err_quota).lower()
    assert err_quota.retryable is False

    # Rate limit payload
    err_rate = TwelveDataProvider._error(
        429,
        {"code": 429, "message": "You have reached your rate limit: 8 API requests per minute.", "status": "error"}
    )
    assert err_rate.status == "RATE_LIMITED"
    assert "rate or api-credit limit reached" in str(err_rate).lower()
    assert err_rate.retryable is True


def test_candles_provider_failure_returns_explicit_error_not_stale_cache(monkeypatch):
    """A provider failure must surface as an explicit error, never as stale candles.

    Honest data policy: no fabricated candles, no silently served cache. An empty
    series plus a clear warning keeps indicators and BUY/SELL evaluation from
    running on data the provider could not deliver.
    """
    from app.main import _candles, market_data

    # Mock market_data to simulate credit exhaustion error
    def mock_fail(*args, **kwargs):
        raise ProviderError("Twelve Data API credit limit reached or credits exhausted.", status="QUOTA_EXCEEDED", code=429)

    monkeypatch.setattr(market_data, "get_historical_candles", mock_fail)
    monkeypatch.setattr(market_data, "status", lambda sym: {
        "symbol": sym, "provider": "TWELVE_DATA", "configured": True,
        "connection_status": "CONNECTED", "data_status": "DELAYED", "streaming": False,
        "message": "Twelve Data configured",
    })

    rows, warnings = _candles("XAUUSD", "15m", 2)
    assert rows == []
    assert any("Historical market data unavailable" in w for w in warnings)

    # Check status endpoint reflects QUOTA_EXCEEDED
    st = client.get("/api/v1/market-data/status?symbol=XAUUSD").json()
    assert st["data_status"] == "QUOTA_EXCEEDED"
    assert "credits exhausted" in st["message"].lower() or "credit limit" in st["message"].lower()


def test_realtime_websocket_overrides_stale_to_live(monkeypatch):
    from app.main import market_data
    monkeypatch.setattr(market_data, "status", lambda sym: {
        "symbol": sym, "provider": "TWELVE_DATA", "configured": True,
        "connection_status": "CONNECTED", "data_status": "DELAYED", "streaming": False,
        "message": "Twelve Data configured",
    })

    # If live WebSocket ticks were recently recorded
    _last_market_updates["XAUUSD"] = datetime.now(timezone.utc).isoformat()
    _market_data_errors["XAUUSD"] = (datetime.now(timezone.utc), "REST rate limit", "RATE_LIMITED")

    st = client.get("/api/v1/market-data/status?symbol=XAUUSD").json()
    # Verified live ticks mean the stream is LIVE
    assert st["data_status"] == "LIVE"
    assert st["streaming"] is True

    # Clean up
    _last_market_updates.pop("XAUUSD", None)
    _market_data_errors.pop("XAUUSD", None)


def test_vwap_unavailable_for_instruments_without_volume():
    # Gold without volume should return UNAVAILABLE with explicit message
    res = client.get("/api/v1/indicators?symbol=XAUUSD&timeframe=15m")
    assert res.status_code == 200
    data = res.json()
    indicators = data["indicators"]
    assert indicators["vwap"] is None
    assert indicators["vwap_status"] == "UNAVAILABLE"
    assert "VWAP unavailable: this feed does not provide suitable volume data for this instrument." in indicators["vwap_message"]


def test_vwap_available_when_valid_volume_present():
    high = [10.0, 11.0, 12.0]
    low = [9.0, 9.5, 10.5]
    close = [9.5, 10.5, 11.5]
    volume = [100.0, 150.0, 200.0]
    sessions = ["2026-10-16", "2026-10-16", "2026-10-16"]

    res = vwap(high, low, close, volume, session_ids=sessions, volume_type="exchange")
    assert len(res) == 3
    assert res[0] is not None
    assert round(res[0], 2) == 9.5  # Typical price = (10 + 9 + 9.5)/3 = 9.5
    assert res[-1] is not None
    assert res[-1] > 0


def test_vwap_missing_or_malformed_volume():
    high = [10.0, 11.0, 12.0]
    low = [9.0, 9.5, 10.5]
    close = [9.5, 10.5, 11.5]
    # None and negative/malformed volume
    volume = [100.0, None, -50.0]
    sessions = ["2026-10-16", "2026-10-16", "2026-10-16"]

    res = vwap(high, low, close, volume, session_ids=sessions, volume_type="exchange")
    assert len(res) == 3
    assert res[0] == 9.5
    # Second and third bars ignore invalid volume, continuing prior cumulative
    assert res[1] == 9.5
    assert res[2] == 9.5


def test_vwap_zero_total_volume():
    high = [10.0, 11.0]
    low = [9.0, 9.5]
    close = [9.5, 10.5]
    volume = [0.0, 0.0]
    sessions = ["2026-10-16", "2026-10-16"]

    res = vwap(high, low, close, volume, session_ids=sessions, volume_type="exchange")
    assert res == [None, None]


def test_vwap_duplicate_candle_timestamp_does_not_double_count():
    high = [10.0, 10.0]
    low = [9.0, 9.0]
    close = [9.5, 9.5]
    volume = [100.0, 100.0]
    sessions = ["2026-10-16", "2026-10-16"]
    timestamps = ["2026-10-16T10:00:00Z", "2026-10-16T10:00:00Z"]

    res = vwap(high, low, close, volume, session_ids=sessions, volume_type="exchange", timestamps=timestamps)
    assert len(res) == 2
    assert res[0] == 9.5
    assert res[1] == 9.5


def test_vwap_session_reset():
    high = [10.0, 20.0]
    low = [10.0, 20.0]
    close = [10.0, 20.0]
    volume = [100.0, 100.0]
    sessions = ["day-1", "day-2"]

    res = vwap(high, low, close, volume, session_ids=sessions, volume_type="exchange")
    assert res[0] == 10.0
    assert res[1] == 20.0


def test_xauusd_vwap_returns_explicit_unavailable_status():
    res = client.get("/api/v1/indicators?symbol=XAUUSD&timeframe=15m")
    assert res.status_code == 200
    data = res.json()
    indicators = data["indicators"]
    assert indicators["vwap"] is None
    assert indicators["vwap_status"] == "UNAVAILABLE"
    assert "suitable volume data" in indicators["vwap_message"].lower()

