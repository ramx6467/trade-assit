"""Tests for the deterministic market-session service.

All datetimes are explicit and timezone-aware so daylight-saving behaviour is
verified rather than assumed. No provider or AI call is made.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.market_hours import (
    CLOSED,
    HOLIDAY,
    INSTRUMENT_MARKETS,
    OPEN,
    PRE_OPEN,
    SCHEDULE_UNKNOWN,
    market_session,
    market_session_summary,
)


def utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(timezone.utc)


def test_unknown_instrument_is_schedule_unknown():
    session = market_session("FOOBAR", now=utc("2026-10-16T12:00:00+00:00"))
    assert session["status"] == SCHEDULE_UNKNOWN
    assert session["status_label"] == "SCHEDULE UNVERIFIED"
    assert session["session_source"] == "unverified"
    assert session["schedule_verified"] is False
    assert market_session_summary(session) == "SCHEDULE UNVERIFIED"


def test_forex_closed_on_saturday_and_open_on_weekdays():
    assert market_session("EURUSD", now=utc("2026-10-17T12:00:00+00:00"))["status"] == CLOSED
    assert market_session("EURUSD", now=utc("2026-10-14T12:00:00+00:00"))["status"] == OPEN


def test_forex_weekend_boundary_uses_new_york_time_with_dst():
    # Oct 2026: New York is on EDT (UTC-4), so 17:00 NY == 21:00 UTC.
    assert market_session("EURUSD", now=utc("2026-10-18T20:59:00+00:00"))["status"] == CLOSED
    assert market_session("EURUSD", now=utc("2026-10-18T21:00:00+00:00"))["status"] == OPEN
    # Friday close: 17:00 NY == 21:00 UTC.
    assert market_session("EURUSD", now=utc("2026-10-16T20:59:00+00:00"))["status"] == OPEN
    assert market_session("EURUSD", now=utc("2026-10-16T21:00:00+00:00"))["status"] == CLOSED


def test_forex_winter_boundary_shifts_with_dst():
    # Jan 2026: New York is on EST (UTC-5), so 17:00 NY == 22:00 UTC.
    assert market_session("EURUSD", now=utc("2026-01-18T21:59:00+00:00"))["status"] == CLOSED
    assert market_session("EURUSD", now=utc("2026-01-18T22:00:00+00:00"))["status"] == OPEN


def test_forex_reports_active_global_sessions():
    # 12:00 UTC is an active London/New York overlap on a weekday.
    session = market_session("EURUSD", now=utc("2026-10-14T12:00:00+00:00"))
    assert "London" in session["active_sessions"]
    assert "New York" in session["active_sessions"]
    assert any(w["name"] == "Tokyo" for w in session["sessions"])


def test_no_session_is_active_while_the_weekend_market_is_closed():
    # Saturday midday UTC falls inside the London/NY local window, but the OTC
    # market is closed, so no centre may be reported as actively trading.
    session = market_session("EURUSD", now=utc("2026-10-17T12:00:00+00:00"))
    assert session["status"] == CLOSED
    assert session["active_sessions"] == []
    assert all(w["status"] == CLOSED for w in session["sessions"])


def test_gold_reference_feed_open_on_a_weekday_session():
    session = market_session("XAUUSD", now=utc("2026-10-14T16:00:00+00:00"))  # Wed 12:00 EDT
    assert session["status"] == OPEN
    assert session["status_label"] == "OPEN"
    assert session["schedule_verified"] is True
    assert session["session_source"] == "documented"
    assert session["session_basis"] == "reference_feed"
    assert session["next_open"] is None
    assert session["next_close"] == "2026-10-14T21:00:00Z"  # 17:00 EDT, daily break


def test_gold_reference_feed_closed_during_the_daily_maintenance_break():
    session = market_session("XAUUSD", now=utc("2026-10-28T21:30:00+00:00"))  # Wed 17:30 EDT
    assert session["status"] == CLOSED
    assert session["next_open"] == "2026-10-28T22:00:00Z"  # 18:00 EDT reopen
    assert session["next_close"] is None


def test_gold_reference_feed_closed_for_the_weekend():
    saturday = market_session("XAUUSD", now=utc("2026-10-17T12:00:00+00:00"))
    assert saturday["status"] == CLOSED
    assert saturday["is_weekend"] is True
    assert saturday["next_open"] == "2026-10-18T22:00:00Z"  # Sunday 18:00 EDT

    sunday_preopen = market_session("XAUUSD", now=utc("2026-10-18T20:00:00+00:00"))
    assert sunday_preopen["status"] == CLOSED
    assert sunday_preopen["next_open"] == "2026-10-18T22:00:00Z"

    sunday_open = market_session("XAUUSD", now=utc("2026-10-18T22:00:00+00:00"))
    assert sunday_open["status"] == OPEN

    friday_close = market_session("XAUUSD", now=utc("2026-10-16T21:00:00+00:00"))
    assert friday_close["status"] == CLOSED
    assert friday_close["next_open"] == "2026-10-18T22:00:00Z"


def test_gold_reference_feed_handles_daylight_saving_transitions():
    # US DST ends on 2026-11-01. New York is EDT (UTC-4) before it and EST
    # (UTC-5) after, so the same UTC instant sits inside or outside the session
    # depending on the offset zoneinfo applies.
    before_dst = market_session("XAUUSD", now=utc("2026-10-28T21:30:00+00:00"))  # Wed 17:30 EDT
    assert before_dst["status"] == CLOSED
    assert before_dst["next_open"] == "2026-10-28T22:00:00Z"  # 18:00 EDT

    after_dst = market_session("XAUUSD", now=utc("2026-11-04T21:30:00+00:00"))  # Wed 16:30 EST
    assert after_dst["status"] == OPEN
    assert after_dst["next_close"] == "2026-11-04T22:00:00Z"  # 17:00 EST


def test_expected_open_closed_and_unknown_states():
    # Verified open (gold weekday, FX weekday), verified closed (FX Saturday),
    # unknown (an instrument with no schedule definition at all).
    assert market_session("XAUUSD", now=utc("2026-10-14T16:00:00+00:00"))["status"] == OPEN
    assert market_session("EURUSD", now=utc("2026-10-17T12:00:00+00:00"))["status"] == CLOSED
    unknown = market_session("FOOBAR", now=utc("2026-10-14T12:00:00+00:00"))
    assert unknown["status"] == SCHEDULE_UNKNOWN
    assert unknown["status_label"] == "SCHEDULE UNVERIFIED"
    assert unknown["session_source"] == "unverified"
    assert unknown["next_open"] is None and unknown["next_close"] is None


def test_provider_documented_session_is_preferred_and_marked_verified():
    session = market_session(
        "XAUUSD",
        now=utc("2026-10-17T12:00:00+00:00"),  # a Saturday for the reference rule
        provider_session={
            "status": "OPEN",
            "reason": "Broker calendar reports an open spot-metals session.",
            "venue_local_time": "2026-10-17T08:00:00-04:00",
        },
    )
    assert session["status"] == OPEN
    assert session["status_label"] == "OPEN"
    assert session["schedule_verified"] is True
    assert session["session_source"] == "provider"
    assert "Broker calendar" in session["reason"]


def test_malformed_provider_session_falls_back_to_the_schedule_service():
    session = market_session(
        "EURUSD",
        now=utc("2026-10-14T12:00:00+00:00"),
        provider_session={"status": "BOGUS"},
    )
    assert session["status"] == OPEN
    assert session["session_source"] == "documented"


def test_a_verified_schedule_must_declare_a_documented_source():
    # Requirement 6: opening hours may only be reported as verified when they are
    # backed by an explicit, external documented source - never a bare constant.
    for symbol, spec in INSTRUMENT_MARKETS.items():
        if spec["schedule_verified"]:
            assert spec["schedule_source"], f"{symbol} is marked verified without a documented source"


def test_unverified_schedule_never_yields_open_or_closed(monkeypatch):
    # No built-in instrument is unverified any more, so register a synthetic one
    # to prove the guard still holds: an assumed calendar is never presented as
    # an OPEN or CLOSED market fact.
    spec = {
        "market": "TEST",
        "asset_class": "Test",
        "venue": "Synthetic test venue",
        "venue_timezone": "UTC",
        "schedule": "FX_24_5",
        "schedule_verified": False,
        "schedule_source": None,
        "schedule_note": "Synthetic unverified schedule for the guard test.",
    }
    monkeypatch.setitem(INSTRUMENT_MARKETS, "ZZTEST", spec)
    open_looking = market_session("ZZTEST", now=utc("2026-10-14T12:00:00+00:00"))   # would look OPEN
    closed_looking = market_session("ZZTEST", now=utc("2026-10-17T12:00:00+00:00"))  # would look CLOSED
    for session in (open_looking, closed_looking):
        assert session["status"] == SCHEDULE_UNKNOWN
        assert session["status_label"] == "SCHEDULE UNVERIFIED"
        assert session["schedule_verified"] is False
        assert session["session_source"] == "unverified"
        assert session["schedule_note"]
        assert session["next_open"] is None and session["next_close"] is None


def test_nse_session_boundaries():
    # 2026-10-16 is a Friday. IST is UTC+5:30.
    assert market_session("NIFTY", now=utc("2026-10-16T03:00:00+00:00"))["status"] == CLOSED   # 08:30 IST
    assert market_session("NIFTY", now=utc("2026-10-16T03:30:00+00:00"))["status"] == PRE_OPEN  # 09:00 IST
    assert market_session("NIFTY", now=utc("2026-10-16T04:00:00+00:00"))["status"] == OPEN      # 09:30 IST
    assert market_session("NIFTY", now=utc("2026-10-16T09:59:00+00:00"))["status"] == OPEN      # 15:29 IST
    assert market_session("NIFTY", now=utc("2026-10-16T10:01:00+00:00"))["status"] == CLOSED    # 15:31 IST


def test_nse_closed_on_weekend():
    session = market_session("NIFTY", now=utc("2026-10-17T05:00:00+00:00"))  # Saturday
    assert session["status"] == CLOSED
    assert session["is_weekend"] is True


def test_nse_holiday_when_provided():
    session = market_session(
        "NIFTY",
        now=utc("2026-10-16T05:00:00+00:00"),  # Friday, 10:30 IST
        holidays={"2026-10-16"},
    )
    assert session["status"] == HOLIDAY
    assert session["holiday_calendar"] == "provided"


def test_holiday_calendar_is_not_verified_by_default():
    session = market_session("NIFTY", now=utc("2026-10-16T05:00:00+00:00"))
    assert session["holiday_calendar"] == "not_verified"


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError):
        market_session("EURUSD", now=datetime(2026, 10, 14, 12, 0, 0))


def test_session_is_independent_of_data_availability():
    # The session result contains only calendar facts; there is no quote,
    # freshness or connectivity field, so an open market cannot be implied by
    # fresh data and a closed market cannot be implied by a quiet feed.
    session = market_session("EURUSD", now=utc("2026-10-17T12:00:00+00:00"))
    assert session["status"] == CLOSED
    assert "price" not in session and "quote" not in session
    assert "data_status" not in session and "connection_status" not in session
    assert "streaming" not in session and "freshness_seconds" not in session
