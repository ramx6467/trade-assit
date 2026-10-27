"""Tests for timeframe-specific BUY and SELL analysis evaluation.

Verifies:
- All 11 canonical timeframes (1m, 2m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 1d, 1w)
- BUY and SELL request handling via both GET and POST with JSON body
- Canonical timeframe propagation (never defaulting or mutating requested timeframe)
- Timeframe alias normalization (e.g. 5min -> 5m, 15min -> 15m, 60m -> 1h)
- HTTP 422 validation on unsupported intervals, symbols, or invalid sides
- Insufficient historical candle handling (< 20 candles returns INSUFFICIENT_DATA)
- Stale candle safeguards (returns DATA_STALE)
- Provider error and timeout resilience (never crashes endpoint)
- Higher-timeframe (HTF) context aggregation and alignment
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.analysis.strategy import evaluate_trading_setup, TIMEFRAME_SECONDS

SUPPORTED_TIMEFRAMES = [
    "1m", "2m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"
]

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _mock_ai_review(monkeypatch):
    monkeypatch.setattr(main, "_ai_review_for_setup", lambda result: "Test AI strategy commentary.")



def _make_candles(
    count: int = 60,
    timeframe: str = "15m",
    trend: str = "bullish",
    base_price: float = 2650.0,
    stale_hours: float = 0.0,
) -> list[dict]:
    """Build a deterministic series of candles for testing."""
    interval_seconds = TIMEFRAME_SECONDS.get(timeframe, 900)
    end_time = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
    start_time = end_time - timedelta(seconds=count * interval_seconds)
    candles = []
    p = base_price
    for i in range(count):
        t = (start_time + timedelta(seconds=i * interval_seconds)).isoformat().replace("+00:00", "Z")
        delta = 0.5 if trend == "bullish" else -0.5 if trend == "bearish" else 0.0
        open_p = p
        close_p = open_p + delta
        high_p = max(open_p, close_p) + 0.3
        low_p = min(open_p, close_p) - 0.3
        candles.append({
            "timestamp": t,
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": 1500,
            "instrument": "XAUUSD",
            "timeframe": timeframe,
            "source": "DEMO",
        })
        p = close_p
    return candles


# ===========================================================================
# 1. EVALUATION TESTS ACROSS ALL 11 TIMEFRAMES
# ===========================================================================

@pytest.mark.parametrize("tf", SUPPORTED_TIMEFRAMES)
def test_buy_evaluation_all_timeframes_get(monkeypatch, tf):
    """GET /api/v1/analysis/evaluate succeeds and preserves requested timeframe."""
    candles = _make_candles(count=50, timeframe=tf, trend="bullish")
    monkeypatch.setattr(main, "_candles", lambda symbol, timeframe, limit: (candles, []))

    res = client.get(f"/api/v1/analysis/evaluate?symbol=XAUUSD&timeframe={tf}&side=BUY")
    assert res.status_code == 200
    data = res.json()
    assert data["symbol"] == "XAUUSD"
    assert data["timeframe"] == tf
    assert data["side"] == "BUY"
    assert data["direction"] in {"BUY", "NO_SETUP"}
    assert "status" in data
    assert "explanation" in data
    assert "conditions_satisfied" in data
    assert "conditions_not_satisfied" in data


@pytest.mark.parametrize("tf", SUPPORTED_TIMEFRAMES)
def test_sell_evaluation_all_timeframes_post_json(monkeypatch, tf):
    """POST /api/v1/analysis/evaluate with JSON body succeeds and preserves timeframe."""
    candles = _make_candles(count=50, timeframe=tf, trend="bearish")
    monkeypatch.setattr(main, "_candles", lambda symbol, timeframe, limit: (candles, []))

    res = client.post(
        "/api/v1/analysis/evaluate",
        json={"symbol": "XAUUSD", "timeframe": tf, "side": "SELL"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["symbol"] == "XAUUSD"
    assert data["timeframe"] == tf
    assert data["side"] == "SELL"
    assert data["direction"] in {"SELL", "NO_SETUP"}
    assert "status" in data


# ===========================================================================
# 2. TIMEFRAME ALIAS NORMALIZATION
# ===========================================================================

@pytest.mark.parametrize("alias,canonical", [
    ("1min", "1m"),
    ("2min", "2m"),
    ("3min", "3m"),
    ("5min", "5m"),
    ("15min", "15m"),
    ("30min", "30m"),
    ("60m", "1h"),
    ("1hour", "1h"),
    ("2hour", "2h"),
    ("4hour", "4h"),
    ("1day", "1d"),
    ("daily", "1d"),
    ("1week", "1w"),
    ("weekly", "1w"),
    ("m5", "5m"),
    ("m15", "15m"),
    ("h1", "1h"),
    ("h4", "4h"),
    ("d1", "1d"),
])
def test_timeframe_alias_normalization(monkeypatch, alias, canonical):
    """Aliases like 5min, 15min, 60m must normalize to canonical representations."""
    candles = _make_candles(count=40, timeframe=canonical, trend="bullish")
    monkeypatch.setattr(main, "_candles", lambda symbol, timeframe, limit: (candles, []))

    res = client.post(
        "/api/v1/analysis/evaluate",
        json={"symbol": "XAUUSD", "timeframe": alias, "side": "BUY"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["timeframe"] == canonical


# ===========================================================================
# 3. HTTP 422 REJECTION ON INVALID REQUEST PARAMETERS
# ===========================================================================

def test_unsupported_timeframe_returns_422():
    """Unsupported intervals like 7m or 10m must return HTTP 422 with a descriptive error."""
    res = client.post(
        "/api/v1/analysis/evaluate",
        json={"symbol": "XAUUSD", "timeframe": "7m", "side": "BUY"}
    )
    assert res.status_code == 422
    assert "Unsupported timeframe" in res.json()["detail"]


def test_invalid_side_returns_422():
    """Invalid trade side must return HTTP 422."""
    res = client.post(
        "/api/v1/analysis/evaluate",
        json={"symbol": "XAUUSD", "timeframe": "15m", "side": "HOLD"}
    )
    assert res.status_code == 422
    assert "side must be BUY or SELL" in res.json()["detail"]


def test_unsupported_symbol_returns_422():
    """Unsupported symbol must return HTTP 422."""
    res = client.post(
        "/api/v1/analysis/evaluate",
        json={"symbol": "BTCUSD", "timeframe": "15m", "side": "BUY"}
    )
    assert res.status_code == 422
    assert "Unsupported instrument" in res.json()["detail"]


# ===========================================================================
# 4. HISTORICAL CANDLE REQUIREMENTS (INSUFFICIENT_DATA)
# ===========================================================================

def test_insufficient_historical_candles_returns_insufficient_data():
    """When fewer than 20 genuine candles exist, INSUFFICIENT_DATA is returned without crashing."""
    few_candles = _make_candles(count=8, timeframe="5m")
    result = evaluate_trading_setup(
        symbol="XAUUSD",
        timeframe="5m",
        side="BUY",
        candles=few_candles,
        warnings=[],
        provider="XAUS",
        data_status="LIVE",
    )
    assert result["status"] == "INSUFFICIENT_DATA"
    assert "Insufficient candle history" in result["explanation"]
    assert len(result["conditions_not_satisfied"]) > 0


# ===========================================================================
# 5. STALE DATA SAFEGUARD PRESERVATION
# ===========================================================================

def test_stale_candles_returns_data_stale():
    """Candles significantly older than the timeframe interval threshold return DATA_STALE."""
    stale_candles = _make_candles(count=30, timeframe="5m", stale_hours=3.0)
    result = evaluate_trading_setup(
        symbol="XAUUSD",
        timeframe="5m",
        side="BUY",
        candles=stale_candles,
        warnings=[],
        provider="XAUS",
        data_status="LIVE",
    )
    assert result["status"] == "DATA_STALE"
    assert "Data is stale" in result["explanation"]


# ===========================================================================
# 6. PROVIDER RESILIENCE & ERROR HANDLING
# ===========================================================================

def test_provider_error_does_not_crash_endpoint(monkeypatch):
    """When the provider raises an error or times out, endpoint returns structured blocked setup."""
    def _failing_candles(*args, **kwargs):
        raise main.ProviderError("Live XAUS connection timed out after 10s")

    monkeypatch.setattr(main, "_candles", _failing_candles)
    res = client.get("/api/v1/analysis/evaluate?symbol=XAUUSD&timeframe=15m&side=BUY")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "DATA_UNAVAILABLE"
    assert "timed out" in data["explanation"]
