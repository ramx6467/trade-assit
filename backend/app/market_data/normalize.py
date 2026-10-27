"""Canonical candle validation shared by providers and analysis routes.

Timestamps are stored as UTC instants. Missing bars are reported, never filled.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite

TIMEFRAME_SECONDS = {"1m": 60, "2m": 120, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400, "1w": 604800}
PRICE_FIELDS = ("open", "high", "low", "close")

# Realistic price-scale bounds for supported instruments (in quoted currency).
# XAU/USD is Gold spot USD per troy ounce. Gold historically trades $250-$5000+
# (currently ~$4,144/oz). Prices like 0.50-2.20 are EUR/USD or arbitrary mock
# values, completely invalid and dangerous for XAU/USD.
INSTRUMENT_PRICE_BOUNDS: dict[str, dict[str, float]] = {
    "XAUUSD": {"min": 500.0, "max": 20000.0},
}


class PriceScaleMismatchError(ValueError):
    """Raised when candle or quote prices diverge from the instrument scale or quote scale."""
    status = "PRICE_SCALE_MISMATCH"


class DataInvalidError(ValueError):
    """Raised when candle or quote fields fail validation."""
    status = "DATA_INVALID"


def validate_price_scale(
    prices: list[float],
    *,
    instrument: str,
    quote_price: float | None = None,
) -> None:
    """Validate that prices fall within the instrument's realistic price scale
    and are consistent with the current live quote when available."""
    normalized_sym = instrument.upper().replace("/", "").replace("-", "")
    bounds = INSTRUMENT_PRICE_BOUNDS.get(normalized_sym)

    for p in prices:
        if not isfinite(p) or p <= 0:
            raise DataInvalidError(f"Price must be finite and positive: {p}")
        if bounds is not None:
            if p < bounds["min"] or p > bounds["max"]:
                raise PriceScaleMismatchError(
                    f"Price {p:.4f} is outside the valid price scale for {instrument} "
                    f"(${bounds['min']:.0f}–${bounds['max']:.0f}). Incompatible instrument or unit."
                )
        if quote_price is not None and isfinite(quote_price) and quote_price > 0:
            ratio = p / quote_price
            # Market can fluctuate between candle updates, but prices must refer
            # to the same instrument on the same scale (ratio within 0.25x to 4.0x).
            if ratio < 0.25 or ratio > 4.0:
                raise PriceScaleMismatchError(
                    f"Price {p:.4f} diverges by {ratio:.4f}x from the live quote {quote_price:.2f}; "
                    f"PRICE_SCALE_MISMATCH between {instrument} quote and candle feed."
                )


def validate_xauusd_candles(
    rows: list[dict],
    *,
    timeframe: str | None = None,
    quote_price: float | None = None,
) -> None:
    """Validate a list of candles specifically for XAU/USD gold consistency.

    Verifies:
      - Finite numeric positive prices
      - Correct OHLC ordering: high >= max(open, close), low <= min(open, close), high >= low
      - Valid instrument price scale ($500-$20,000/oz)
      - Consistency with live quote if provided
      - Valid UTC timestamps in strictly chronological order without duplicates
    """
    if not rows:
        return
    seen_ts: set[datetime] = set()
    prior_ts: datetime | None = None

    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise DataInvalidError(f"Candle {i} must be a dictionary object")
        ts_raw = raw.get("timestamp")
        if not ts_raw:
            raise DataInvalidError(f"Candle {i} has no timestamp")
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise DataInvalidError(f"Candle {i} has an invalid timestamp: {ts_raw}") from exc
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise DataInvalidError(f"Candle {i} timestamp must include a timezone")
        ts_utc = ts.astimezone(timezone.utc)
        if ts_utc in seen_ts:
            raise DataInvalidError(f"Duplicate candle timestamp: {ts_raw}")
        if prior_ts is not None and ts_utc <= prior_ts:
            raise DataInvalidError(f"Candles must be in strictly chronological order (candle {i} {ts_raw} <= {prior_ts})")
        seen_ts.add(ts_utc)
        prior_ts = ts_utc

        try:
            o, h, l, c = float(raw["open"]), float(raw["high"]), float(raw["low"]), float(raw["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataInvalidError(f"Candle {i} has missing or non-numeric OHLC values") from exc

        if not all(isfinite(v) and v > 0 for v in (o, h, l, c)):
            raise DataInvalidError(f"Candle {i} prices must be finite and positive")
        if h < max(o, c) or h < l:
            raise DataInvalidError(f"Candle {i} high ({h}) must be >= open ({o}), close ({c}), and low ({l})")
        if l > min(o, c) or l > h:
            raise DataInvalidError(f"Candle {i} low ({l}) must be <= open ({o}), close ({c}), and high ({h})")

        # Validate price scale
        validate_price_scale([o, h, l, c], instrument="XAUUSD", quote_price=quote_price)


def normalize_candles(
    rows: list[dict],
    *,
    instrument: str,
    timeframe: str,
    data_source: str,
    quote_price: float | None = None,
) -> tuple[list[dict], list[str]]:
    """Validate, UTC-normalize and tag candles; reject corrupt or mixed data."""
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    normalized: list[dict] = []
    warnings: list[str] = []
    seen: set[datetime] = set()
    step = TIMEFRAME_SECONDS[timeframe]
    prior: datetime | None = None
    for index, raw in enumerate(rows):
        try:
            timestamp = datetime.fromisoformat(str(raw["timestamp"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise DataInvalidError(f"Candle {index} has an invalid timestamp") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise DataInvalidError(f"Candle {index} timestamp must include a timezone")
        # Validate exchange/provider wall-clock bar boundaries before converting to UTC.
        # Markets such as NSE open at 09:15 local time, so their valid candles are not
        # necessarily aligned to a UTC epoch boundary.
        local = timestamp
        timestamp = timestamp.astimezone(timezone.utc)
        # Derive the boundary rule from each timeframe's own step so *every*
        # intraday frame is checked, not just the ones that used to be listed by
        # hand: a 2m bar on an odd minute, a 2h bar on an odd hour or a 4h bar off
        # a four-hour boundary is now rejected instead of being plotted at a time
        # the market never traded. Daily bars keep the provider's venue-local
        # midnight (they are deliberately *not* forced onto 00:00 UTC here, which
        # would shift the trading date in some display zones); weekly bars must
        # fall on a Monday.
        aligned = (local.second == 0 and local.microsecond == 0)
        if step < 86400:
            aligned = aligned and (local.hour * 3600 + local.minute * 60) % step == 0
        elif timeframe == "1w":
            aligned = aligned and local.weekday() == 0
        if not aligned:
            raise DataInvalidError(f"Candle {index} timestamp is not aligned to {timeframe} UTC boundaries")
        if timestamp in seen:
            raise DataInvalidError(f"Duplicate candle timestamp: {timestamp.isoformat()}")
        if prior is not None:
            elapsed = (timestamp - prior).total_seconds()
            if elapsed <= 0:
                raise DataInvalidError("Candles must be in strictly chronological order")
            if elapsed % step != 0:
                raise DataInvalidError(f"Candle spacing is inconsistent with {timeframe}")
            if elapsed > step:
                warnings.append(f"Missing {int(elapsed // step) - 1} candle(s) after {prior.isoformat()}")
        seen.add(timestamp)
        prior = timestamp
        try:
            prices = {key: float(raw[key]) for key in PRICE_FIELDS}
        except (KeyError, TypeError, ValueError) as exc:
            raise DataInvalidError(f"Candle {index} has missing or invalid OHLC values") from exc
        if not all(isfinite(value) and value > 0 for value in prices.values()):
            raise DataInvalidError(f"Candle {index} prices must be finite and positive")
        if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
            raise DataInvalidError(f"Candle {index} high is inconsistent with OHLC")
        if prices["low"] > min(prices["open"], prices["close"], prices["high"]):
            raise DataInvalidError(f"Candle {index} low is inconsistent with OHLC")

        # Validate price scale against instrument bounds and live quote
        validate_price_scale(list(prices.values()), instrument=instrument, quote_price=quote_price)

        volume = raw.get("volume")
        if volume is not None:
            try:
                volume = float(volume)
            except (TypeError, ValueError) as exc:
                raise DataInvalidError(f"Candle {index} volume is invalid") from exc
            if not isfinite(volume) or volume < 0:
                raise DataInvalidError(f"Candle {index} volume must be finite and non-negative")
        normalized.append({
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            **prices,
            "volume": volume,
            "instrument": instrument,
            "timeframe": timeframe,
            "source": data_source,
            "data_source": data_source,
            "is_realtime": False,
            "volume_type": raw.get("volume_type", "unavailable" if volume is None else "unverified"),
        })
    return normalized, warnings
