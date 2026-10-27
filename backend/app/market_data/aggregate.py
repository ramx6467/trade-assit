"""Deterministic OHLC aggregation for timeframes a provider cannot serve directly.

This module builds a coarser candle series strictly from real, already-validated
finer-grained candles that the provider *does* support. It never pads a bucket
with synthetic prices: a target candle is emitted only when its bucket contains a
complete, contiguous set of source candles. Incomplete buckets are reported
through ``warnings`` and omitted rather than guessed.

The provider itself must keep rejecting intervals it does not support (so it can
never silently substitute or fabricate data); callers that want a derived
timeframe opt in explicitly by choosing a source interval and aggregating here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.market_data.normalize import TIMEFRAME_SECONDS, normalize_candles


def _parse(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def aggregate_candles(
    rows: list[dict],
    *,
    source_timeframe: str,
    target_timeframe: str,
    instrument: str,
    data_source: str,
) -> tuple[list[dict], list[str]]:
    """Aggregate ``source_timeframe`` candles into ``target_timeframe`` candles.

    Returns ``(normalized_target_rows, warnings)``. Only complete buckets are
    emitted; every incomplete bucket produces a warning and no candle.
    """
    if source_timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported source timeframe: {source_timeframe}")
    if target_timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported target timeframe: {target_timeframe}")
    source_step = TIMEFRAME_SECONDS[source_timeframe]
    target_step = TIMEFRAME_SECONDS[target_timeframe]
    if target_step <= source_step or target_step % source_step != 0:
        raise ValueError(
            f"Cannot aggregate {source_timeframe} into {target_timeframe}; the "
            "target interval must be a whole multiple of the source interval."
        )
    factor = target_step // source_step

    parsed: list[tuple[datetime, dict]] = []
    for row in rows:
        stamp = _parse(row.get("timestamp"))
        if stamp is not None:
            parsed.append((stamp, row))
    parsed.sort(key=lambda item: item[0])

    buckets: dict[int, list[tuple[datetime, dict]]] = {}
    for stamp, row in parsed:
        bucket = int(stamp.timestamp()) // target_step * target_step
        buckets.setdefault(bucket, []).append((stamp, row))

    warnings: list[str] = []
    aggregated: list[dict] = []
    for bucket in sorted(buckets):
        members = buckets[bucket]
        start = datetime.fromtimestamp(bucket, tz=timezone.utc)
        expected = [start + timedelta(seconds=i * source_step) for i in range(factor)]
        if len(members) != factor or [stamp for stamp, _ in members] != expected:
            warnings.append(
                f"Incomplete {target_timeframe} coverage for {start.isoformat()}: "
                f"expected {factor} contiguous {source_timeframe} candles, found {len(members)}."
            )
            continue
        volumes = [row.get("volume") for _, row in members]
        volume: float | None = None
        volume_type = "unavailable"
        if all(value is not None for value in volumes):
            try:
                volume = float(sum(float(value) for value in volumes))
                volume_type = str(members[0][1].get("volume_type") or "unverified")
            except (TypeError, ValueError):
                volume = None
                volume_type = "unavailable"
        aggregated.append({
            "timestamp": start.isoformat().replace("+00:00", "Z"),
            "open": float(members[0][1]["open"]),
            "high": max(float(row["high"]) for _, row in members),
            "low": min(float(row["low"]) for _, row in members),
            "close": float(members[-1][1]["close"]),
            "volume": volume,
            "volume_type": volume_type,
        })

    if not aggregated:
        return [], warnings + [
            f"No complete {target_timeframe} candles could be built from "
            f"{len(parsed)} {source_timeframe} source candle(s)."
        ]

    normalized, normalize_warnings = normalize_candles(
        aggregated, instrument=instrument, timeframe=target_timeframe, data_source=data_source
    )
    return normalized, warnings + normalize_warnings
