from datetime import datetime, timezone

import pytest

from app.market_data.demo import candles
from app.market_data.normalize import normalize_candles


def row(timestamp="2026-10-16T10:00:00Z", **overrides):
    return {"timestamp": timestamp, "open": 10, "high": 12, "low": 9, "close": 11,
            "volume": None, "volume_type": "unavailable", **overrides}


def test_demo_candles_are_boundary_aligned_completed_and_have_no_fabricated_volume():
    result = candles("XAUUSD", "15m", 10)
    times = [datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")) for item in result]
    assert all(int(item.timestamp()) % 900 == 0 for item in times)
    assert all((b - a).total_seconds() == 900 for a, b in zip(times, times[1:]))
    current_bucket = int(datetime.now(timezone.utc).timestamp()) // 900 * 900
    assert int(times[-1].timestamp()) < current_bucket
    assert all(item["volume"] is None and item["volume_type"] == "unavailable" for item in result)


def test_normalizer_adds_shared_market_identity_and_utc_metadata():
    normalized, warnings = normalize_candles([row("2026-10-16T10:00:00+02:00")], instrument="EURUSD", timeframe="1h", data_source="DEMO")
    assert normalized[0]["timestamp"] == "2026-10-16T08:00:00Z"
    assert (normalized[0]["instrument"], normalized[0]["timeframe"], normalized[0]["data_source"]) == ("EURUSD", "1h", "DEMO")
    assert warnings == []

def test_normalizer_does_not_guess_volume_type():
    bar = row(volume=10)
    del bar["volume_type"]
    normalized, _ = normalize_candles([bar], instrument="EURUSD", timeframe="1m", data_source="TEST")
    assert normalized[0]["volume_type"] == "unverified"

@pytest.mark.parametrize("rows,fragment", [
    ([row(), row()], "Duplicate"),
    ([row("2026-10-16T10:00:30Z")], "aligned"),
    ([row("2026-10-16T10:00:00")], "timezone"),
    ([row(high=8)], "high is inconsistent"),
    ([row(open=float("nan"))], "finite and positive"),
    ([row(timestamp="2026-10-16T10:00:00Z"), row(timestamp="2026-10-16T09:59:00Z")], "chronological"),
])
def test_normalizer_rejects_invalid_or_misaligned_candles(rows, fragment):
    with pytest.raises(ValueError, match=fragment):
        normalize_candles(rows, instrument="EURUSD", timeframe="1m", data_source="TEST")


@pytest.mark.parametrize("timeframe,timestamp", [
    ("2m", "2026-10-16T10:57:00Z"),   # odd minute: not a 2m bar open
    ("2h", "2026-10-16T11:00:00Z"),   # odd hour: not a 2h bar open
    ("4h", "2026-10-16T02:00:00Z"),   # off a four-hour boundary
])
def test_normalizer_rejects_misaligned_derived_timeframes(timeframe, timestamp):
    """2m/2h/4h previously fell through with only a seconds check."""
    with pytest.raises(ValueError, match="aligned"):
        normalize_candles([row(timestamp)], instrument="EURUSD", timeframe=timeframe, data_source="TEST")


@pytest.mark.parametrize("timeframe,timestamp", [
    ("2m", "2026-10-16T10:58:00Z"),
    ("2h", "2026-10-16T10:00:00Z"),
    ("4h", "2026-10-16T12:00:00Z"),
])
def test_normalizer_accepts_aligned_derived_timeframes(timeframe, timestamp):
    normalized, _ = normalize_candles([row(timestamp)], instrument="EURUSD", timeframe=timeframe, data_source="TEST")
    assert normalized[0]["timestamp"] == timestamp


def test_normalizer_reports_gaps_without_filling_them():
    rows = [row("2026-10-16T10:00:00Z"), row("2026-10-16T10:03:00Z")]
    normalized, warnings = normalize_candles(rows, instrument="EURUSD", timeframe="1m", data_source="TEST")
    assert len(normalized) == 2
    assert warnings == ["Missing 2 candle(s) after 2026-10-16T10:00:00+00:00"]
