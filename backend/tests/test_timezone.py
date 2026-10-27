from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pytest

from app.market_data.normalize import normalize_candles
from app.market_data.demo import DemoMarketDataProvider, SYMBOLS
from app.main import _exchange_timezone


def test_exchange_timezone_mapping():
    assert _exchange_timezone("NIFTY") == "Asia/Kolkata"
    assert _exchange_timezone("BANKNIFTY") == "Asia/Kolkata"
    assert _exchange_timezone("XAUUSD") == "America/New_York"
    assert _exchange_timezone("GBPUSD") == "Europe/London"
    assert _exchange_timezone("EURUSD") == "UTC"
    assert SYMBOLS["NIFTY"]["exchange_timezone"] == "Asia/Kolkata"


def test_ist_candle_normalization_without_double_conversion():
    # NSE opens at 09:15 IST (03:45 UTC).
    # A 15-minute candle for NSE at 09:15 IST has timestamp "2026-10-16T09:15:00+05:30".
    ist_stamp = "2026-10-16T09:15:00+05:30"
    raw_row = {
        "timestamp": ist_stamp,
        "open": 22450.0,
        "high": 22480.0,
        "low": 22440.0,
        "close": 22475.0,
        "volume": None,
        "volume_type": "unavailable",
    }
    normalized, warnings = normalize_candles([raw_row], instrument="NIFTY", timeframe="15m", data_source="TEST")
    assert len(normalized) == 1
    # Check that it converted correctly to UTC (09:15 IST - 5:30 = 03:45 UTC):
    assert normalized[0]["timestamp"] == "2026-10-16T03:45:00Z"

    # Converting the normalized UTC timestamp back to IST display must produce 09:15 IST exactly:
    utc_dt = datetime.fromisoformat(normalized[0]["timestamp"].replace("Z", "+00:00"))
    ist_dt = utc_dt.astimezone(ZoneInfo("Asia/Kolkata"))
    assert ist_dt.hour == 9 and ist_dt.minute == 15
    assert warnings == []


def test_date_boundary_transition_utc_to_ist():
    # 20:00 UTC on 2026-10-16 is 01:30 AM on 2026-10-17 in Asia/Kolkata (+05:30)
    utc_dt = datetime(2026, 10, 16, 20, 0, 0, tzinfo=timezone.utc)
    ist_dt = utc_dt.astimezone(ZoneInfo("Asia/Kolkata"))
    assert ist_dt.day == 17
    assert ist_dt.hour == 1
    assert ist_dt.minute == 30

    # 22:00 UTC on New Year's Eve (2026-12-31) is 03:30 AM on 2027-01-01 in IST
    ny_utc = datetime(2026, 12, 31, 22, 0, 0, tzinfo=timezone.utc)
    ny_ist = ny_utc.astimezone(ZoneInfo("Asia/Kolkata"))
    assert ny_ist.year == 2027
    assert ny_ist.month == 1
    assert ny_ist.day == 1
    assert ny_ist.hour == 3
    assert ny_ist.minute == 30


def test_daylight_saving_transitions_for_exchange_instruments():
    # Gold (XAUUSD) COMEX New York: EDT (UTC-4) in Summer, EST (UTC-5) in Winter
    summer_utc = datetime(2026, 7, 15, 14, 0, 0, tzinfo=timezone.utc)
    winter_utc = datetime(2026, 1, 15, 14, 0, 0, tzinfo=timezone.utc)

    ny_tz = ZoneInfo("America/New_York")
    summer_ny = summer_utc.astimezone(ny_tz)
    winter_ny = winter_utc.astimezone(ny_tz)

    assert summer_ny.hour == 10  # 14:00 - 4 = 10:00 EDT
    assert winter_ny.hour == 9   # 14:00 - 5 = 09:00 EST

    # London (GBPUSD): BST (UTC+1) in Summer, GMT (UTC+0) in Winter
    lon_tz = ZoneInfo("Europe/London")
    summer_lon = summer_utc.astimezone(lon_tz)
    winter_lon = winter_utc.astimezone(lon_tz)

    assert summer_lon.hour == 15  # 14:00 + 1 = 15:00 BST
    assert winter_lon.hour == 14  # 14:00 + 0 = 14:00 GMT


def test_demo_candles_keep_chronological_order_and_utc_instants():
    provider = DemoMarketDataProvider()
    rows = provider.get_historical_candles("NIFTY", "15m", 20)
    assert len(rows) == 20
    times = [datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) for r in rows]
    # Verify strictly monotonic increasing
    for t1, t2 in zip(times, times[1:]):
        assert t2 > t1
        assert (t2 - t1).total_seconds() == 900
