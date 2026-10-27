"""Deterministic market-session service.

Session state is a calendar fact computed from timezone-aware datetimes
(``zoneinfo`` handles daylight-saving transitions). It is deliberately kept
separate from market-data availability: a market can be open while the quote
feed is disconnected, and a provider can return a recent quote while the
market is closed (for example during a weekend or holiday). Nothing here asks
the AI or the data provider whether a market is open.

Verified vs unverified schedules
--------------------------------
Only a schedule that is objectively documented may produce an OPEN or CLOSED
verdict, and an unverified schedule is never guessed into one:

* provider   - the data provider publishes documented market-session data for
  the instrument; it is preferred when present (see ``provider_session``).
* documented - a schedule that is objectively published for the venue is
  encoded in ``INSTRUMENT_MARKETS`` and marked ``schedule_verified``: ``NSE_EQ``
  (NSE regular equity-market hours), ``FX_24_5`` (the standard over-the-
  counter foreign-exchange convention) and ``METALS_OTC_REF`` (the documented
  OTC spot-gold / COMEX Globex reference-feed clock followed by XAU/USD).
* unverified - no reliable schedule is available. The state is reported as
  ``SCHEDULE_UNKNOWN`` ("SCHEDULE UNVERIFIED"); it is never labelled CLOSED - or
  OPEN - merely because the calendar could not be confirmed.

The XAU/USD entry is a *reference-feed* session: TradeMind AI has no configured
broker, so it reports the documented OTC spot-gold clock that the aggregated
reference feed follows (Sunday 18:00 - Friday 17:00 America/New_York with the
daily 17:00-18:00 ET maintenance break). It is explicitly labelled
``session_basis: "reference_feed"`` and never presented as the executable hours
of a specific broker or liquidity provider.

Holidays are never invented: when no verified holiday calendar is configured the
status is computed from the weekly schedule only and ``holiday_calendar`` is
reported as ``"not_verified"``. Session state is a calendar fact and stays
independent of quote freshness and WebSocket connectivity; those are computed
elsewhere and never feed into this module.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

MARKET_HOURS_VERSION = "3.1"

OPEN = "OPEN"
CLOSED = "CLOSED"
PRE_OPEN = "PRE_OPEN"
HOLIDAY = "HOLIDAY"
SCHEDULE_UNKNOWN = "SCHEDULE_UNKNOWN"

# Deterministic calendar rules.
FX_24_5 = "FX_24_5"
NSE_EQ = "NSE_EQ"
METALS_OTC_REF = "METALS_OTC_REF"

# Human-facing labels. ``SCHEDULE_UNKNOWN`` is the machine state; the UI reports
# it as "SCHEDULE UNVERIFIED" so an assumed or missing calendar is never read as
# a confirmed fact.
STATUS_LABELS: dict[str, str] = {
    OPEN: "OPEN",
    CLOSED: "CLOSED",
    PRE_OPEN: "PRE_OPEN",
    HOLIDAY: "HOLIDAY",
    SCHEDULE_UNKNOWN: "SCHEDULE UNVERIFIED",
}

_NY = "America/New_York"
_IST = "Asia/Kolkata"

# Instrument -> venue/market definition. `schedule` selects the deterministic
# calendar rule; `schedule_verified` states whether the rule is objectively
# published for this instrument/venue rather than assumed.
INSTRUMENT_MARKETS: dict[str, dict[str, Any]] = {
    "EURUSD": {
        "market": "FOREX_OTC",
        "asset_class": "Forex",
        "venue": "OTC interbank foreign exchange",
        "venue_timezone": "UTC",
        "schedule": "FX_24_5",
        "schedule_verified": True,
        "schedule_source": "Standard OTC foreign-exchange convention (Sun 17:00 - Fri 17:00 America/New_York).",
        "schedule_note": None,
    },
    "GBPUSD": {
        "market": "FOREX_OTC",
        "asset_class": "Forex",
        "venue": "OTC interbank foreign exchange",
        "venue_timezone": "UTC",
        "schedule": "FX_24_5",
        "schedule_verified": True,
        "schedule_source": "Standard OTC foreign-exchange convention (Sun 17:00 - Fri 17:00 America/New_York).",
        "schedule_note": None,
    },
    "XAUUSD": {
        "market": "METALS_OTC",
        "asset_class": "Metals",
        "venue": "OTC spot gold reference feed (broker-specific executable hours)",
        "venue_timezone": _NY,
        "schedule": "METALS_OTC_REF",
        "schedule_verified": True,
        "schedule_source": (
            "Documented OTC spot-gold reference-feed clock: opens Sunday 18:00 "
            "and closes Friday 17:00 America/New_York, with the daily 17:00-18:00 "
            "ET maintenance break shared with the COMEX Globex gold session."
        ),
        "schedule_note": (
            "Reference-feed session (OTC spot gold convention). A specific broker "
            "or liquidity provider may quote different executable hours."
        ),
        "session_basis": "reference_feed",
    },
    "NIFTY": {
        "market": "NSE",
        "asset_class": "Index",
        "venue": "National Stock Exchange of India",
        "venue_timezone": _IST,
        "schedule": "NSE_EQ",
        "schedule_verified": True,
        "schedule_source": "NSE regular equity-market hours (Mon-Fri, pre-open 09:00-09:15 IST, regular 09:15-15:30 IST).",
        "schedule_note": None,
    },
    "BANKNIFTY": {
        "market": "NSE",
        "asset_class": "Index",
        "venue": "National Stock Exchange of India",
        "venue_timezone": _IST,
        "schedule": "NSE_EQ",
        "schedule_verified": True,
        "schedule_source": "NSE regular equity-market hours (Mon-Fri, pre-open 09:00-09:15 IST, regular 09:15-15:30 IST).",
        "schedule_note": None,
    },
}

# Global FX cash-session windows, expressed in each centre's local time. These
# describe when a session is active; they do not change whether the OTC market
# as a whole is open (that is governed by the FX_24_5 weekly rule).
FX_SESSIONS: tuple[dict[str, str], ...] = (
    {"name": "Sydney", "timezone": "Australia/Sydney", "start": "07:00", "end": "16:00"},
    {"name": "Tokyo", "timezone": "Asia/Tokyo", "start": "09:00", "end": "18:00"},
    {"name": "London", "timezone": "Europe/London", "start": "08:00", "end": "17:00"},
    {"name": "New York", "timezone": _NY, "start": "08:00", "end": "17:00"},
)

# No verified holiday calendar is bundled. Callers (or tests) may pass explicit
# holiday dates; anything not provided is reported as unverified rather than
# guessed.
VERIFIED_HOLIDAYS: dict[str, set[str]] = {}


def _parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _in_window(local_dt: datetime, start: str, end: str) -> bool:
    """Return True when the local time falls in [start, end), supporting overnight windows."""
    begin, finish = _parse_hhmm(start), _parse_hhmm(end)
    current = local_dt.time()
    if begin <= finish:
        return begin <= current < finish
    return current >= begin or current < finish


def _session_windows(local_dt: datetime, market_open: bool) -> list[dict[str, Any]]:
    """Report each global FX cash session's open/closed state and local time.

    A cash centre can only be trading when the OTC market as a whole is open, so
    weekends (and the pre-open Sunday gap) report every session as CLOSED rather
    than showing a local-time window that no one is actually trading.
    """
    windows: list[dict[str, Any]] = []
    for spec in FX_SESSIONS:
        zone = ZoneInfo(spec["timezone"])
        local = local_dt.astimezone(zone)
        windows.append({
            "name": spec["name"],
            "timezone": spec["timezone"],
            "status": OPEN if (market_open and _in_window(local, spec["start"], spec["end"])) else CLOSED,
            "local_time": local.isoformat(),
            "window_local": f"{spec['start']}-{spec['end']}",
        })
    return windows


def _fx_24_5_state(now_utc: datetime) -> dict[str, Any]:
    """Weekly FX open/close rule anchored on America/New_York (DST-aware).

    The OTC market opens Sunday 17:00 and closes Friday 17:00 New York time.
    """
    ny = ZoneInfo(_NY)
    local = now_utc.astimezone(ny)
    weekday = local.weekday()  # Monday=0 ... Sunday=6
    now_time = local.time()
    weekday_open = {0, 1, 2, 3}  # Mon-Thu: open all day
    if weekday in weekday_open:
        is_open = True
        reason = "The OTC foreign-exchange market is open on a weekday."
    elif weekday == 4:  # Friday
        is_open = now_time < time(17, 0)
        reason = "The OTC market closes at 17:00 New York time on Fridays." if is_open else "Friday trading has closed for the week (after 17:00 New York time)."
    elif weekday == 5:  # Saturday
        is_open = False
        reason = "The OTC foreign-exchange market is closed on Saturdays."
    else:  # Sunday
        is_open = now_time >= time(17, 0)
        reason = "The OTC market reopens Sunday 17:00 New York time." if is_open else "The OTC market has not reopened yet (opens Sunday 17:00 New York time)."

    next_open = None
    next_close = None
    if is_open:
        days_to_friday = (4 - weekday) % 7
        close_day = local.replace(hour=17, minute=0, second=0, microsecond=0) + timedelta(days=days_to_friday)
        if weekday == 4 and now_time >= time(17, 0):
            close_day += timedelta(days=7)
        next_close = close_day.astimezone(timezone.utc)
    else:
        # Next Sunday 17:00 New York time.
        days_to_sunday = (6 - weekday) % 7
        open_day = local.replace(hour=17, minute=0, second=0, microsecond=0) + timedelta(days=days_to_sunday)
        if days_to_sunday == 0 and now_time >= time(17, 0):
            open_day += timedelta(days=7)
        next_open = open_day.astimezone(timezone.utc)

    return {
        "status": OPEN if is_open else CLOSED,
        "is_weekend": weekday >= 5,
        "reason": reason,
        "next_open": next_open.isoformat().replace("+00:00", "Z") if next_open else None,
        "next_close": next_close.isoformat().replace("+00:00", "Z") if next_close else None,
        "venue_local_time": local.isoformat(),
    }


_METALS_OPEN = time(18, 0)   # Sunday weekly reopen / daily maintenance-break reopen (ET)
_METALS_CLOSE = time(17, 0)  # daily maintenance break / Friday weekly close (ET)


def _metals_otc_ref_state(now_utc: datetime) -> dict[str, Any]:
    """Documented OTC spot-gold reference-feed clock, anchored on New York time.

    The aggregated gold reference feed follows the standard OTC spot/COMEX
    Globex clock: it opens Sunday 18:00, closes Friday 17:00 and pauses every
    trading day for the 17:00-18:00 America/New_York maintenance break. Because
    the rule is anchored on ``America/New_York``, ``zoneinfo`` applies the
    correct EDT/EST offset and the weekly boundaries stay DST-correct.
    """
    ny = ZoneInfo(_NY)
    local = now_utc.astimezone(ny)
    weekday = local.weekday()  # Monday=0 ... Sunday=6
    now_time = local.time()

    if weekday == 5:  # Saturday
        is_open = False
        reason = "The gold reference feed is closed for the weekend (Saturday)."
    elif weekday == 6:  # Sunday
        is_open = now_time >= _METALS_OPEN
        reason = (
            "The gold reference feed reopened Sunday 18:00 New York time."
            if is_open
            else "The gold reference feed has not reopened yet (opens Sunday 18:00 New York time)."
        )
    elif weekday == 4:  # Friday
        is_open = now_time < _METALS_CLOSE
        reason = (
            "The gold reference feed is open and closes at 17:00 New York time on Fridays."
            if is_open
            else "The gold reference feed has closed for the week (after 17:00 New York time on Friday)."
        )
    else:  # Monday-Thursday: 24h except the daily maintenance break
        is_open = now_time < _METALS_CLOSE or now_time >= _METALS_OPEN
        reason = (
            "The gold reference feed is open (daily maintenance break is 17:00-18:00 New York time)."
            if is_open
            else "The gold reference feed is in its daily 17:00-18:00 New York time maintenance break."
        )

    next_open = None
    next_close = None
    if is_open:
        close_day = local.replace(hour=17, minute=0, second=0, microsecond=0)
        if close_day <= local:
            close_day += timedelta(days=1)
        next_close = close_day.astimezone(timezone.utc)
    else:
        open_day = local.replace(hour=18, minute=0, second=0, microsecond=0)
        if open_day <= local:
            open_day += timedelta(days=1)
        for _ in range(8):
            if open_day.weekday() in {6, 0, 1, 2, 3} and open_day > local:
                break
            open_day += timedelta(days=1)
        next_open = open_day.astimezone(timezone.utc)

    return {
        "status": OPEN if is_open else CLOSED,
        "is_weekend": weekday >= 5,
        "reason": reason,
        "next_open": next_open.isoformat().replace("+00:00", "Z") if next_open else None,
        "next_close": next_close.isoformat().replace("+00:00", "Z") if next_close else None,
        "venue_local_time": local.isoformat(),
    }


def _nse_state(now_utc: datetime, holidays: set[str]) -> dict[str, Any]:
    ist = ZoneInfo(_IST)
    local = now_utc.astimezone(ist)
    day = local.date()
    day_key = day.isoformat()
    pre_open_start = time(9, 0)
    regular_start = time(9, 15)
    regular_end = time(15, 30)
    now_time = local.time()

    if day_key in holidays:
        status = HOLIDAY
        reason = "The exchange is closed for a scheduled holiday."
        next_open = None
    elif local.weekday() >= 5:
        status = CLOSED
        reason = "The exchange is closed on weekends."
        next_open = _next_nse_open(local, holidays)
    elif now_time < pre_open_start:
        status = CLOSED
        reason = "The exchange has not opened yet (pre-open starts 09:00 IST)."
        next_open = local.replace(hour=9, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    elif now_time < regular_start:
        status = PRE_OPEN
        reason = "The exchange is in its pre-open session (09:00-09:15 IST)."
        next_open = local.replace(hour=9, minute=15, second=0, microsecond=0).astimezone(timezone.utc)
    elif now_time <= regular_end:
        status = OPEN
        reason = "The exchange is in its regular trading session."
        next_open = None
    else:
        status = CLOSED
        reason = "The exchange regular session has closed (after 15:30 IST)."
        next_open = _next_nse_open(local, holidays)

    next_close = None
    if status in {OPEN, PRE_OPEN}:
        next_close = local.replace(hour=15, minute=30, second=0, microsecond=0).astimezone(timezone.utc)

    return {
        "status": status,
        "is_weekend": local.weekday() >= 5,
        "reason": reason,
        "next_open": next_open.isoformat().replace("+00:00", "Z") if next_open else None,
        "next_close": next_close.isoformat().replace("+00:00", "Z") if next_close else None,
        "venue_local_time": local.isoformat(),
    }


def _next_nse_open(local: datetime, holidays: set[str]) -> datetime:
    """Find the next weekday that is not a configured holiday."""
    candidate = local
    for _ in range(10):
        candidate = (candidate + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
        if candidate.weekday() >= 5:
            continue
        if candidate.date().isoformat() in holidays:
            continue
        return candidate.astimezone(timezone.utc)
    return (local + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0).astimezone(timezone.utc)


def _status_label(status: str) -> str:
    """Return the display label for a machine session status."""
    return STATUS_LABELS.get(status, status)


def _provider_core(provider_session: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a provider-documented session into the internal core shape.

    Returns ``None`` when the payload is missing or does not carry a usable
    status, so callers fall back to the deterministic schedule instead of
    trusting an empty or malformed provider response.
    """
    if not isinstance(provider_session, dict):
        return None
    status = str(provider_session.get("status") or "").upper()
    if status not in {OPEN, CLOSED, PRE_OPEN, HOLIDAY}:
        return None
    return {
        "status": status,
        "reason": provider_session.get("reason")
        or "Reported by the data provider's documented market-session data.",
        "is_weekend": provider_session.get("is_weekend"),
        "next_open": provider_session.get("next_open"),
        "next_close": provider_session.get("next_close"),
        "venue_local_time": provider_session.get("venue_local_time"),
    }


def market_session(
    symbol: str,
    *,
    now: datetime | None = None,
    holidays: Iterable[str] | None = None,
    provider_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the deterministic market-session state for an instrument.

    ``now`` must be timezone-aware (or None for the current UTC instant) so the
    DST-correct conversion is unambiguous. ``holidays`` is an optional iterable
    of ``YYYY-MM-DD`` strings; an empty/absent set is reported as an unverified
    holiday calendar instead of being silently treated as "no holidays".

    ``provider_session`` is an optional payload holding the data provider's own
    documented market-session data. When it carries a usable status the provider
    is trusted and reported as the source; otherwise the documented schedule
    below decides the state.

    A schedule that is neither provider-documented nor objectively published is
    reported as ``SCHEDULE_UNKNOWN`` - never OPEN or CLOSED - so an assumed
    calendar is never presented as a fact.
    """
    sym = symbol.upper()
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        raise ValueError("market_session requires a timezone-aware datetime")

    utc_iso = now_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    spec = INSTRUMENT_MARKETS.get(sym)
    if spec is None:
        return {
            "symbol": sym,
            "market": "UNKNOWN",
            "asset_class": "Unknown",
            "venue": None,
            "venue_timezone": None,
            "schedule": None,
            "schedule_verified": False,
            "session_source": "unverified",
            "schedule_source": None,
            "schedule_note": "No verified trading-schedule definition exists for this instrument.",
            "session_basis": None,
            "status": SCHEDULE_UNKNOWN,
            "status_label": _status_label(SCHEDULE_UNKNOWN),
            "reason": "The trading schedule for this instrument is not verified.",
            "is_weekend": None,
            "utc_time": utc_iso,
            "venue_time": None,
            "venue_time_label": None,
            "sessions": [],
            "active_sessions": [],
            "next_open": None,
            "next_close": None,
            "holiday_calendar": "not_verified",
        }

    provided_holidays = set(holidays) if holidays is not None else None
    if provided_holidays is not None:
        holidays_set = provided_holidays
        holiday_calendar = "provided"
    else:
        holidays_set = set(VERIFIED_HOLIDAYS.get(spec["schedule"], set()))
        holiday_calendar = "verified" if holidays_set else "not_verified"

    venue_zone = ZoneInfo(spec["venue_timezone"]) if spec.get("venue_timezone") else timezone.utc
    venue_local = now_utc.astimezone(venue_zone)

    provider_core = _provider_core(provider_session) if provider_session else None

    # A schedule may only be treated as verified when it declares an external,
    # documented source. A bare "verified" flag with no source is an assumption,
    # so it is downgraded rather than presented as a market fact (requirement 6).
    documented_verified = bool(spec["schedule_verified"] and spec["schedule_source"])

    # Today's actual defect: an unverified calendar was still run through the
    # reference rule and reported as CLOSED. A default/assumed schedule must
    # never be presented as a market fact, so report the state as unknown.
    if provider_core is None and not documented_verified:
        return {
            "symbol": sym,
            "market": spec["market"],
            "asset_class": spec["asset_class"],
            "venue": spec["venue"],
            "venue_timezone": spec["venue_timezone"],
            "schedule": spec["schedule"],
            "schedule_verified": False,
            "session_source": "unverified",
            "schedule_source": spec["schedule_source"],
            "schedule_note": spec["schedule_note"],
            "session_basis": spec.get("session_basis"),
            "status": SCHEDULE_UNKNOWN,
            "status_label": _status_label(SCHEDULE_UNKNOWN),
            "reason": spec["schedule_note"]
            or "The trading schedule for this instrument is not verified.",
            "is_weekend": None,
            "utc_time": utc_iso,
            "venue_time": venue_local.isoformat(),
            "venue_time_label": f"{venue_local.strftime('%Y-%m-%d %H:%M %Z')}",
            "sessions": [],
            "active_sessions": [],
            "next_open": None,
            "next_close": None,
            "holiday_calendar": holiday_calendar,
        }

    if provider_core is not None:
        schedule_verified = True
        session_source = "provider"
        core = provider_core
    else:
        schedule_verified = documented_verified
        session_source = "documented"
        if spec["schedule"] == NSE_EQ:
            core = _nse_state(now_utc, holidays_set)
        elif spec["schedule"] == METALS_OTC_REF:
            core = _metals_otc_ref_state(now_utc)
        else:
            core = _fx_24_5_state(now_utc)

    if spec["schedule"] == NSE_EQ:
        sessions: list[dict[str, Any]] = []
        active: list[str] = []
    else:
        sessions = _session_windows(now_utc, market_open=core["status"] == OPEN)
        active = [w["name"] for w in sessions if w["status"] == OPEN]

    # A provider payload may omit its venue-local clock; fall back to the venue
    # timezone computed above rather than inventing a time.
    venue_time = core.get("venue_local_time") or venue_local.isoformat()

    return {
        "symbol": sym,
        "market": spec["market"],
        "asset_class": spec["asset_class"],
        "venue": spec["venue"],
        "venue_timezone": spec["venue_timezone"],
        "schedule": spec["schedule"],
        "schedule_verified": schedule_verified,
        "session_source": session_source,
        "schedule_source": spec["schedule_source"],
        "schedule_note": spec["schedule_note"],
        "session_basis": spec.get("session_basis"),
        "status": core["status"],
        "status_label": _status_label(core["status"]),
        "reason": core["reason"],
        "is_weekend": core.get("is_weekend"),
        "utc_time": utc_iso,
        "venue_time": venue_time,
        "venue_time_label": f"{venue_local.strftime('%Y-%m-%d %H:%M %Z')}",
        "sessions": sessions,
        "active_sessions": active,
        "next_open": core.get("next_open"),
        "next_close": core.get("next_close"),
        "holiday_calendar": holiday_calendar,
    }


def market_session_summary(session: dict[str, Any]) -> str:
    """One-line human summary suitable for logs or the AI context."""
    status = session.get("status", SCHEDULE_UNKNOWN)
    label = session.get("status_label") or _status_label(status)
    if status == SCHEDULE_UNKNOWN:
        return label
    return f"{label} - {session.get('reason', '')}".strip()
