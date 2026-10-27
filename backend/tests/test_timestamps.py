"""Canonical-timestamp conversion coverage.

The chart, the analysis context and the AI assistant must all resolve to *one*
UTC instant per candle, no matter which wire format the provider used. These
tests pin the single conversion helper and prove that:

* epoch seconds, epoch milliseconds, ISO-8601 UTC and ISO-8601-with-offset all
  land on the same instant;
* a naive (offset-less) string is *rejected* rather than silently assumed to be
  UTC or local;
* conversion is idempotent, so a value can never be double-converted;
* IST is a *display* rendering of the same instant, never a second timestamp.
"""
from datetime import datetime, timezone

import pytest

from app.main import _detect_raw_timestamp_format, _display_in, _iso_utc, _raw_instant_to_utc

# Friday 2026-10-16 16:55 ET - the last closed 5m bar of the week - is
# 2026-10-16T20:55:00Z, and renders as Saturday 2026-10-17 02:25 IST because IST
# is UTC+05:30. That shift is arithmetic, not a bug.
LAST_FRIDAY_BAR = datetime(2026, 10, 16, 20, 55, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "raw, expected_format",
    [
        (1792184100, "unix_epoch_seconds"),
        (1792184100000, "unix_epoch_milliseconds"),
        (1792184100.0, "unix_epoch_seconds"),
        ("2026-10-16T20:55:00Z", "iso8601_utc"),
        ("2026-10-16T20:55:00.000Z", "iso8601_utc"),
        ("2026-10-16T16:55:00-04:00", "iso8601_explicit_offset"),
        ("2026-10-17T02:25:00+05:30", "iso8601_explicit_offset"),
        ("2026-10-16T20:55:00+00:00", "iso8601_explicit_offset"),
    ],
)
def test_every_supported_raw_format_resolves_to_one_utc_instant(raw, expected_format):
    assert _detect_raw_timestamp_format(raw) == expected_format
    assert _raw_instant_to_utc(raw) == LAST_FRIDAY_BAR


def test_naive_and_missing_timestamps_are_rejected_not_assumed():
    # "2026-10-16T20:55:00" could mean UTC, IST or New York. Guessing would
    # invent a bar that the provider never sent, so it is refused instead.
    assert _detect_raw_timestamp_format("2026-10-16T20:55:00") == "iso8601_without_offset"
    assert _raw_instant_to_utc("2026-10-16T20:55:00") is None
    assert _detect_raw_timestamp_format(None) == "missing"
    assert _raw_instant_to_utc(None) is None
    assert _raw_instant_to_utc("not-a-timestamp") is None
    # A boolean is never a timestamp, even though bool is an int subclass.
    assert _raw_instant_to_utc(True) is None


def test_milliseconds_are_never_read_as_seconds():
    ms = 1792184100000
    # Read as seconds the same digits denote the year ~58762 (unrepresentable);
    # the magnitude rule keeps them on the same instant as their seconds spelling.
    assert _raw_instant_to_utc(ms) == _raw_instant_to_utc(ms // 1000) == LAST_FRIDAY_BAR
    assert (_raw_instant_to_utc(ms).year, _raw_instant_to_utc(ms).hour) == (2026, 20)
    # The seconds/milliseconds boundary itself must not flip.
    assert _detect_raw_timestamp_format(99_999_999_999) == "unix_epoch_seconds"
    assert _detect_raw_timestamp_format(100_000_000_000) == "unix_epoch_milliseconds"


def test_conversion_is_idempotent_so_a_value_is_never_double_converted():
    once = _raw_instant_to_utc("2026-10-16T20:55:00Z")
    twice = _raw_instant_to_utc(_iso_utc(once))
    thrice = _raw_instant_to_utc(_iso_utc(twice))
    assert once == twice == thrice == LAST_FRIDAY_BAR


def test_ist_is_a_display_rendering_of_the_same_instant():
    # The exact string a screenshot shows: it is the Friday bar, read on the IST
    # calendar. Same instant, different calendar day.
    assert _display_in(LAST_FRIDAY_BAR, "Asia/Kolkata") == "2026-10-17 02:25:00 IST"
    assert _iso_utc(LAST_FRIDAY_BAR) == "2026-10-16T20:55:00Z"
    # A different display zone must not change the instant, only its wording.
    assert _display_in(LAST_FRIDAY_BAR, "America/New_York") == "2026-10-16 16:55:00 EDT"
    assert _display_in(LAST_FRIDAY_BAR, "UTC") == "2026-10-16 20:55:00 UTC"


def test_display_helper_never_invents_a_value_for_missing_input():
    assert _display_in(None, "Asia/Kolkata") is None
    assert _iso_utc(None) is None
    # An unusable zone degrades to null rather than to a wrong time.
    assert _display_in(LAST_FRIDAY_BAR, "Not/AZone") is None


@pytest.mark.parametrize(
    "instant, expected",
    [
        # Friday/Saturday: the ET Friday close crosses midnight in IST...
        (datetime(2026, 10, 16, 20, 55, tzinfo=timezone.utc), "2026-10-17 02:25:00 IST"),
        # ...but a mid-session bar stays on the same IST date.
        (datetime(2026, 10, 16, 6, 30, tzinfo=timezone.utc), "2026-10-16 12:00:00 IST"),
        # Midnight UTC is already 05:30 IST on the *next* day.
        (datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc), "2026-10-16 05:30:00 IST"),
        # 18:30 UTC is exactly midnight IST the next day.
        (datetime(2026, 10, 16, 18, 30, tzinfo=timezone.utc), "2026-10-17 00:00:00 IST"),
        # Sunday 18:00 ET (Monday 03:30 IST) - the weekly gold re-open.
        (datetime(2026, 10, 18, 22, 0, tzinfo=timezone.utc), "2026-10-19 03:30:00 IST"),
    ],
)
def test_ist_boundaries_including_midnight_and_the_weekend_reopen(instant, expected):
    assert _display_in(instant, "Asia/Kolkata") == expected
