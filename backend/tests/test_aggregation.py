"""OHLC aggregation correctness (3m from 1m) and honest incomplete-bucket handling."""
from datetime import datetime, timedelta, timezone

import pytest

from app.market_data.aggregate import aggregate_candles


def _bar(minute: int, o: float, h: float, l: float, c: float, volume=None, volume_type="exchange"):
    stamp = datetime(2026, 10, 16, 10, minute, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return {"timestamp": stamp, "open": o, "high": h, "low": l, "close": c,
            "volume": volume, "volume_type": volume_type}


def _one_minute_rows():
    # Six contiguous 1m candles -> exactly two complete 3m candles.
    return [
        _bar(0, 1.0, 1.2, 0.9, 1.1),
        _bar(1, 1.1, 1.3, 1.05, 1.25),
        _bar(2, 1.25, 1.4, 1.2, 1.3),
        _bar(3, 1.3, 1.35, 1.1, 1.15),
        _bar(4, 1.15, 1.5, 1.14, 1.45),
        _bar(5, 1.45, 1.6, 1.4, 1.55),
    ]


def test_aggregate_3m_uses_real_source_ohlc():
    rows, warnings = aggregate_candles(
        _one_minute_rows(), source_timeframe="1m", target_timeframe="3m",
        instrument="EURUSD", data_source="TWELVE_DATA",
    )
    assert warnings == []
    assert [r["timestamp"] for r in rows] == ["2026-10-16T10:00:00Z", "2026-10-16T10:03:00Z"]
    first, second = rows
    assert (first["open"], first["high"], first["low"], first["close"]) == (1.0, 1.4, 0.9, 1.3)
    assert (second["open"], second["high"], second["low"], second["close"]) == (1.3, 1.6, 1.1, 1.55)
    # Aggregated candles are tagged with the requested (derived) timeframe.
    assert all(r["timeframe"] == "3m" and r["instrument"] == "EURUSD" for r in rows)


def test_aggregate_drops_incomplete_buckets_instead_of_padding():
    rows = _one_minute_rows()
    del rows[2]  # remove 10:02 -> first 3m bucket is now incomplete
    aggregated, warnings = aggregate_candles(
        rows, source_timeframe="1m", target_timeframe="3m",
        instrument="EURUSD", data_source="TWELVE_DATA",
    )
    assert [r["timestamp"] for r in aggregated] == ["2026-10-16T10:03:00Z"]
    assert any("Incomplete 3m coverage" in w for w in warnings)


def test_aggregate_returns_structured_no_coverage_message():
    aggregated, warnings = aggregate_candles(
        [_bar(0, 1.0, 1.2, 0.9, 1.1), _bar(1, 1.1, 1.3, 1.05, 1.25)],
        source_timeframe="1m", target_timeframe="3m",
        instrument="EURUSD", data_source="TWELVE_DATA",
    )
    assert aggregated == []
    assert any("No complete 3m candles" in w for w in warnings)


def test_aggregate_sums_volume_only_when_source_volume_is_present():
    complete = [_bar(0, 1.0, 1.2, 0.9, 1.1, volume=10),
                _bar(1, 1.1, 1.3, 1.05, 1.25, volume=20),
                _bar(2, 1.25, 1.4, 1.2, 1.3, volume=30)]
    rows, _ = aggregate_candles(complete, source_timeframe="1m", target_timeframe="3m",
                                instrument="EURUSD", data_source="TEST")
    assert rows[0]["volume"] == 60

    missing = list(complete)
    missing[1] = dict(missing[1], volume=None)
    rows, _ = aggregate_candles(missing, source_timeframe="1m", target_timeframe="3m",
                                instrument="EURUSD", data_source="TEST")
    assert rows[0]["volume"] is None
    assert rows[0]["volume_type"] == "unavailable"


def test_aggregate_rejects_non_multiple_target():
    with pytest.raises(ValueError):
        aggregate_candles(_one_minute_rows(), source_timeframe="5m", target_timeframe="3m",
                          instrument="EURUSD", data_source="TEST")


def test_aggregate_is_deterministic_regardless_of_input_order():
    rows = _one_minute_rows()
    forward, _ = aggregate_candles(rows, source_timeframe="1m", target_timeframe="3m",
                                   instrument="EURUSD", data_source="TEST")
    backward, _ = aggregate_candles(list(reversed(rows)), source_timeframe="1m", target_timeframe="3m",
                                    instrument="EURUSD", data_source="TEST")
    assert forward == backward
