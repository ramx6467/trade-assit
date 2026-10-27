"""Explicit, intentionally simple candidate detections; not objective market truth."""
from __future__ import annotations
import hashlib
import math
from datetime import datetime

def detect_fvgs(candles: list[dict], instrument: str = "UNKNOWN", timeframe: str = "UNKNOWN",
                as_of: str | None = None) -> list[dict]:
    if as_of is not None:
        cutoff = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            raise ValueError("as_of timestamp must include a timezone")
        candles = [row for row in candles if datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) <= cutoff]
    zones = []
    for i in range(2, len(candles)):
        left, current = candles[i - 2], candles[i]
        if current["low"] > left["high"]:
            direction, lower, upper = "bullish", left["high"], current["low"]
        elif current["high"] < left["low"]:
            direction, lower, upper = "bearish", current["high"], left["low"]
        else:
            continue
        source_timestamps = [candles[j]["timestamp"] for j in (i - 2, i - 1, i)]
        identity = f"{instrument}|{timeframe}|{direction}|{source_timestamps[0]}|{source_timestamps[2]}"
        inval_cond = (
            f"Candle close below FVG lower edge {lower}"
            if direction == "bullish"
            else f"Candle close above FVG upper edge {upper}"
        )
        zones.append({
            "id": hashlib.sha256(identity.encode()).hexdigest()[:20],
            "instrument": instrument,
            "timeframe": timeframe,
            "data_source": current.get("data_source", "unknown"),
            "direction": direction,
            "lower": lower,
            "upper": upper,
            "created_at": current["timestamp"],
            "source_candle_timestamps": source_timestamps,
            "mitigation_status": "unmitigated",
            "invalidation_status": "valid",
            "invalidation_timestamp": None,
            "invalidation_condition": inval_cond,
            "status": "unmitigated",
        })
    # Full-fill rule: bullish zones are mitigated at/below the lower edge;
    # bearish zones at/above the upper edge. Only later candles are considered.
    for zone in zones:
        idx = next(i for i, row in enumerate(candles) if row["timestamp"] == zone["created_at"])
        later = candles[idx + 1:]
        for candle in later:
            if zone["direction"] == "bullish":
                if candle["close"] < zone["lower"]:
                    zone["invalidation_status"] = "invalidated"
                    zone["invalidation_timestamp"] = candle["timestamp"]
                if candle["low"] <= zone["lower"]:
                    zone["mitigation_status"] = "fully_mitigated"
                    zone["status"] = "mitigated"
            else:
                if candle["close"] > zone["upper"]:
                    zone["invalidation_status"] = "invalidated"
                    zone["invalidation_timestamp"] = candle["timestamp"]
                if candle["high"] >= zone["upper"]:
                    zone["mitigation_status"] = "fully_mitigated"
                    zone["status"] = "mitigated"
    return zones


def swings(candles: list[dict], left: int = 2, right: int = 2, window: int | None = None) -> dict:
    if window is not None:
        left = window
        right = window
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        high, low = candles[i]["high"], candles[i]["low"]
        if all(high > candles[j]["high"] for j in range(i-left, i+right+1) if j != i):
            highs.append({"index": i, "confirmed_at": candles[i+right]["timestamp"], "price": high})
        if all(low < candles[j]["low"] for j in range(i-left, i+right+1) if j != i):
            lows.append({"index": i, "confirmed_at": candles[i+right]["timestamp"], "price": low})
    return {"swing_highs": highs, "swing_lows": lows}


def market_structure(candles: list[dict], left: int = 2, right: int = 2, tolerance_pct: float = 0.1, window: int | None = None) -> dict:
    """Confirmed-pivot BOS/CHoCH/MSS, one-shot sweeps, HH/HL/LH/LL, BSL/SSL, and simple pivot clusters.

    A break is a candle close through the latest confirmed pivot. The pivot
    only becomes eligible on its `confirmed_at` candle. First break is BOS;
    a break against the current direction is CHoCH. When a break occurs with
    measured displacement, it is classified as a Market Structure Shift (MSS).
    Sweeps are wick crosses which close back on the original side.
    Cluster tolerance is a percentage.
    """
    if tolerance_pct < 0:
        raise ValueError("tolerance_pct must be non-negative")
    if window is not None:
        left = window
        right = window
    pivots = swings(candles, left, right)

    # 1. Classify Swing Highs / Lows into HH, HL, LH, LL
    hh, hl, lh, ll = [], [], [], []
    for idx, sh in enumerate(pivots["swing_highs"]):
        if idx > 0:
            prev = pivots["swing_highs"][idx - 1]
            if sh["price"] > prev["price"]:
                hh.append({**sh, "type": "HH", "prior_price": prev["price"]})
            elif sh["price"] < prev["price"]:
                lh.append({**sh, "type": "LH", "prior_price": prev["price"]})
        else:
            hh.append({**sh, "type": "HIGH", "prior_price": None})

    for idx, sl in enumerate(pivots["swing_lows"]):
        if idx > 0:
            prev = pivots["swing_lows"][idx - 1]
            if sl["price"] > prev["price"]:
                hl.append({**sl, "type": "HL", "prior_price": prev["price"]})
            elif sl["price"] < prev["price"]:
                ll.append({**sl, "type": "LL", "prior_price": prev["price"]})
        else:
            ll.append({**sl, "type": "LOW", "prior_price": None})

    # 2. Objective displacement measurement
    from app.analysis.indicators import atr
    highs_arr = [float(c["high"]) for c in candles]
    lows_arr = [float(c["low"]) for c in candles]
    closes_arr = [float(c["close"]) for c in candles]
    atr_vals = atr(highs_arr, lows_arr, closes_arr, 14) if len(candles) >= 14 else [None] * len(candles)

    displacements = []
    for i in range(1, len(candles)):
        c = candles[i]
        body = abs(float(c["close"]) - float(c["open"]))
        rng = float(c["high"]) - float(c["low"])
        c_atr = atr_vals[i - 1] if atr_vals[i - 1] is not None else (rng if rng > 0 else 1.0)
        if rng > 0 and body >= 1.0 * c_atr and (body / rng) >= 0.50:
            displacements.append({
                "index": i,
                "timestamp": c["timestamp"],
                "direction": "bullish" if c["close"] > c["open"] else "bearish",
                "body": round(body, 5),
                "atr": round(c_atr, 5),
                "multiplier": round(body / c_atr, 2) if c_atr > 0 else 1.0,
            })
    displacement_indices = {d["index"]: d for d in displacements}

    by_confirmation: dict[int, list[tuple[str, dict]]] = {}
    for kind, entries in (("high", pivots["swing_highs"]), ("low", pivots["swing_lows"])):
        for pivot in entries:
            confirm_index = pivot["index"] + right
            by_confirmation.setdefault(confirm_index, []).append((kind, pivot))
    latest = {"high": None, "low": None}
    consumed: set[tuple[str, int]] = set()
    swept: set[tuple[str, int]] = set()
    bias: str | None = None
    breaks, sweeps = [], []
    mss_list = []

    for i, candle in enumerate(candles):
        for kind, pivot in by_confirmation.get(i, []):
            latest[kind] = pivot
        for kind, direction, crossed in (("high", "bullish", candle["close"]), ("low", "bearish", candle["close"])):
            pivot = latest[kind]
            if pivot is None or (kind, pivot["index"]) in consumed:
                continue
            crossed_level = crossed > pivot["price"] if kind == "high" else crossed < pivot["price"]
            if crossed_level:
                event_type = "CHoCH" if bias is not None and bias != direction else "BOS"
                # Check for displacement on this candle or adjacent candle
                has_displacement = i in displacement_indices
                disp_info = displacement_indices.get(i)
                break_item = {
                    "id": f"{event_type}:{kind}:{pivot['index']}:{i}",
                    "direction": direction,
                    "type": event_type,
                    "level": pivot["price"],
                    "pivot_index": pivot["index"],
                    "pivot_timestamp": candles[pivot["index"]]["timestamp"],
                    "confirmed_at": candle["timestamp"],
                    "index": i,
                }
                breaks.append(break_item)
                if event_type == "CHoCH" and has_displacement:
                    mss_list.append({
                        **break_item,
                        "type": "MSS",
                        "is_mss": True,
                        "displacement": disp_info,
                    })
                consumed.add((kind, pivot["index"]))
                bias = direction
            else:
                wick_crossed = candle["high"] > pivot["price"] if kind == "high" else candle["low"] < pivot["price"]
                closed_back = candle["close"] <= pivot["price"] if kind == "high" else candle["close"] >= pivot["price"]
                sweep_key = (kind, pivot["index"])
                if wick_crossed and closed_back and sweep_key not in swept:
                    sweeps.append({"direction": "bearish" if kind == "high" else "bullish",
                        "level": pivot["price"], "pivot_timestamp": candles[pivot["index"]]["timestamp"],
                        "timestamp": candle["timestamp"], "index": i})
                    swept.add(sweep_key)

    def clusters(entries: list[dict], label: str) -> list[dict]:
        grouped: list[list[dict]] = []
        for item in entries:
            for group in grouped:
                center = sum(x["price"] for x in group) / len(group)
                if abs(item["price"] - center) <= abs(center) * tolerance_pct / 100:
                    group.append(item)
                    break
            else:
                grouped.append([item])
        return [{"type": label, "price": sum(x["price"] for x in group) / len(group),
                 "touches": len(group), "timestamps": [candles[x["index"]]["timestamp"] for x in group]}
                for group in grouped if len(group) >= 2]

    support_clusters = clusters(pivots["swing_lows"], "support")
    resistance_clusters = clusters(pivots["swing_highs"], "resistance")

    # 3. Buy-side liquidity (BSL) and Sell-side liquidity (SSL) pools
    bsl = []
    for sh in pivots["swing_highs"]:
        is_swept = any(s["level"] == sh["price"] and s["direction"] == "bearish" for s in sweeps)
        is_broken = any(b["level"] == sh["price"] and b["direction"] == "bullish" for b in breaks)
        bsl.append({
            "type": "BSL",
            "level": sh["price"],
            "pivot_timestamp": candles[sh["index"]]["timestamp"],
            "confirmed_at": sh["confirmed_at"],
            "status": "broken" if is_broken else "swept" if is_swept else "active",
        })

    ssl = []
    for sl in pivots["swing_lows"]:
        is_swept = any(s["level"] == sl["price"] and s["direction"] == "bullish" for s in sweeps)
        is_broken = any(b["level"] == sl["price"] and b["direction"] == "bearish" for b in breaks)
        ssl.append({
            "type": "SSL",
            "level": sl["price"],
            "pivot_timestamp": candles[sl["index"]]["timestamp"],
            "confirmed_at": sl["confirmed_at"],
            "status": "broken" if is_broken else "swept" if is_swept else "active",
        })

    structural_bias = bias.upper() if bias else ("BULLISH" if len(hh) > len(lh) else "BEARISH" if len(ll) > len(hl) else "NEUTRAL")

    return {
        **pivots,
        "higher_highs": hh,
        "higher_lows": hl,
        "lower_highs": lh,
        "lower_lows": ll,
        "breaks": breaks,
        "market_structure_shifts": mss_list,
        "liquidity_sweeps": sweeps,
        "buy_side_liquidity": bsl,
        "sell_side_liquidity": ssl,
        "support_clusters": support_clusters,
        "resistance_clusters": resistance_clusters,
        "equal_highs": resistance_clusters,
        "equal_lows": support_clusters,
        "displacements": displacements,
        "structural_bias": structural_bias,
        "rules": {
            "pivot_left": left, "pivot_right": right,
            "break_confirmation": "close beyond most recent confirmed pivot",
            "first_or_continuation_break": "BOS",
            "opposite_direction_break": "CHoCH",
            "market_structure_shift": "CHoCH confirmed with displacement body >= 1.0x ATR",
            "liquidity_sweep": "wick beyond confirmed pivot and close back across it",
            "cluster_tolerance_pct": tolerance_pct,
        },
    }


def dealing_range(candles: list[dict], left: int = 2, right: int = 2, window: int | None = None) -> dict | None:
    """Highest confirmed swing high to lowest confirmed swing low (the dealing range).

    Returns the range boundaries, the equilibrium midpoint, premium/discount zones,
    optimal trade entry (OTE), and the timestamps of the two extremes.
    Returns ``None`` when either side has no confirmed pivot.
    """
    if window is not None:
        left = window
        right = window
    pivots = swings(candles, left, right)
    highs = pivots["swing_highs"]
    lows = pivots["swing_lows"]
    if not highs or not lows:
        return None
    top = max(highs, key=lambda p: p["price"])
    bottom = min(lows, key=lambda p: p["price"])
    high, low = float(top["price"]), float(bottom["price"])
    if high <= low:
        return None
    rng = high - low
    eq = (high + low) / 2.0
    return {
        "high": high,
        "low": low,
        "range": rng,
        "equilibrium": eq,
        "premium_zone": {"lower": eq, "upper": high},
        "discount_zone": {"lower": low, "upper": eq},
        "optimal_trade_entry": {
            "lower": round(low + rng * 0.618, 5),
            "upper": round(low + rng * 0.786, 5),
        },
        "high_confirmed_at": candles[top["index"] + right]["timestamp"] if top["index"] + right < len(candles) else None,
        "low_confirmed_at": candles[bottom["index"] + right]["timestamp"] if bottom["index"] + right < len(candles) else None,
    }


ORDER_BLOCK_METHODOLOGY = {
    "boundary_rule": "Zone boundaries come from the originating candle: 'full' uses its high/low, 'body' uses its open/close (normalised so upper > lower).",
    "origin_rule": "The originating candle is the last opposing-colour candle within `lookback` bars before the displacement candle.",
    "displacement_rule": "A displacement candle body must be >= `displacement_atr` x the previous ATR(`atr_period`).",
    "confirmation_rule": "A confirmed (active) block also requires an aligned structure break (BOS/CHoCH) from a pivot that was already confirmed before the displacement, occurring on the displacement candle or within `break_window` bars after it.",
    "mitigation_rule": "'partially_mitigated' when a later candle trades into the zone; 'fully_mitigated' when a later candle trades through the distal edge; 'invalidated' when a later candle closes through the distal edge (takes precedence over mitigation).",
    "note": "One explicit, configurable mechanical methodology; not a claim about institutional orders or a prediction.",
}


def _ordered_candles(candles: list[dict]) -> list[dict]:
    """Chronological candles with finite, structurally valid OHLC.

    Timestamps are sorted when every row parses as ISO-8601; otherwise the
    caller's order is trusted (provider data is already chronological).
    """
    parsed: list[tuple[datetime, dict]] = []
    sortable = True
    for row in candles:
        try:
            parsed.append((datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")), row))
        except (KeyError, TypeError, ValueError):
            sortable = False
            break
    ordered = [row for _, row in sorted(parsed, key=lambda item: item[0])] if sortable else list(candles)
    clean = []
    for row in ordered:
        try:
            values = [float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])]
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in values) and values[1] >= values[2]:
            clean.append(row)
    return clean


def _opposing_candle(rows: list[dict], index: int, lookback: int, direction: str) -> int | None:
    """Nearest prior candle of the opposite colour within `lookback` bars."""
    want_bearish = direction == "bullish"
    for j in range(index - 1, max(-1, index - lookback - 1), -1):
        is_bearish = rows[j]["close"] < rows[j]["open"]
        if is_bearish is want_bearish:
            return j
    return None


def _zone_bounds(origin: dict, boundary: str) -> tuple[float, float]:
    if boundary == "body":
        return (min(origin["open"], origin["close"]), max(origin["open"], origin["close"]))
    return (origin["low"], origin["high"])


def _aligned_break(breaks_by_index: dict[int, list[dict]], index: int, direction: str,
                   break_window: int) -> dict | None:
    """First same-direction break on the displacement candle or within the window.

    The broken pivot must have been confirmed before the displacement, which
    prevents look-ahead and rejects a break of a pivot that formed inside the
    impulse itself.
    """
    for k in range(index, index + break_window + 1):
        for item in breaks_by_index.get(k, []):
            if item["direction"] == direction and item["pivot_index"] < index:
                return item
    return None


def _apply_mitigation_and_invalidation(zones: list[dict], rows: list[dict]) -> None:
    """Resolve each zone's lifecycle using only candles after its confirmation."""
    for zone in zones:
        if zone["status"] != "confirmed":
            zone["mitigated"] = False
            zone["mitigation_status"] = "unconfirmed"
            continue
        touched = False
        traversed = False
        for m in range(zone["break_index"] + 1, len(rows)):
            later = rows[m]
            if zone["direction"] == "bullish":
                invalid = later["close"] < zone["lower"]
                touch = later["low"] <= zone["upper"]
                full = later["low"] <= zone["lower"]
            else:
                invalid = later["close"] > zone["upper"]
                touch = later["high"] >= zone["lower"]
                full = later["high"] >= zone["upper"]
            if invalid:
                zone["status"] = "invalidated"
                zone["invalidation_timestamp"] = later["timestamp"]
                break
            touched = touched or touch
            traversed = traversed or full
        zone["mitigated"] = touched
        zone["mitigation_status"] = "fully_mitigated" if traversed else "partially_mitigated" if touched else "unmitigated"


def detect_order_blocks(candles: list[dict], instrument: str = "UNKNOWN", timeframe: str = "UNKNOWN",
                        lookback: int = 5, atr_period: int = 14, displacement_atr: float = 1.0,
                        pivot_left: int = 2, pivot_right: int = 2, boundary: str = "full",
                        break_window: int = 3) -> list[dict]:
    """Array of every qualifying order block in the supplied candle history.

    Each entry is the last opposing-colour candle within `lookback` bars before a
    displacement candle whose body is >= `displacement_atr` x the previous ATR.
    A block is *confirmed* (active) when an aligned structure break from an
    already-confirmed pivot occurs on the displacement candle or within
    `break_window` bars after it; otherwise it stays a *candidate*.

    All parameters are configurable methodology choices. This returns the whole
    list rather than a single zone, keeps bullish and bearish zones separate,
    deduplicates repeated detections of the same originating candle, and applies
    mitigation/invalidation using only candles that existed after confirmation
    (no look-ahead). It is one explicit mechanical definition, not a claim about
    institutional orders.
    """
    from app.analysis.indicators import atr
    if lookback < 1 or atr_period < 1 or displacement_atr <= 0:
        raise ValueError("lookback and ATR period must be positive; displacement multiplier must be positive")
    if break_window < 0:
        raise ValueError("break_window must be non-negative")
    if boundary not in {"full", "body"}:
        raise ValueError("boundary must be 'full' or 'body'")
    rows = _ordered_candles(candles)
    if len(rows) < max(atr_period + 1, 5):
        return []
    atr_values = atr([x["high"] for x in rows], [x["low"] for x in rows], [x["close"] for x in rows], atr_period)
    structure = market_structure(rows, left=pivot_left, right=pivot_right)
    breaks_by_index: dict[int, list[dict]] = {}
    for item in structure["breaks"]:
        breaks_by_index.setdefault(item["index"], []).append(item)
    zones: dict[str, dict] = {}
    for i in range(1, len(rows)):
        prior_atr = atr_values[i - 1]
        if prior_atr is None:
            continue
        bar = rows[i]
        direction = "bullish" if bar["close"] > bar["open"] else "bearish" if bar["close"] < bar["open"] else None
        if direction is None or abs(bar["close"] - bar["open"]) < prior_atr * displacement_atr:
            continue
        candidate_index = _opposing_candle(rows, i, lookback, direction)
        if candidate_index is None:
            continue
        origin = rows[candidate_index]
        lower, upper = _zone_bounds(origin, boundary)
        if not (upper > lower):
            continue
        event = _aligned_break(breaks_by_index, i, direction, break_window)
        identity = f"{instrument}|{timeframe}|{direction}|{origin['timestamp']}|{boundary}"
        zone_id = hashlib.sha256(identity.encode()).hexdigest()[:20]
        existing = zones.get(zone_id)
        if existing is not None and not (event and existing["status"] == "candidate"):
            continue
        inval_cond = (
            f"Candle close below order block lower edge {lower}"
            if direction == "bullish"
            else f"Candle close above order block upper edge {upper}"
        )
        zones[zone_id] = {
            "id": zone_id, "instrument": instrument, "timeframe": timeframe,
            "data_source": origin.get("data_source", "unknown"), "direction": direction,
            "lower": lower, "upper": upper, "boundary_methodology": boundary,
            "created_at": origin["timestamp"], "origin_index": candidate_index,
            "origin_candle_direction": "bearish" if origin["close"] < origin["open"] else "bullish",
            "displacement_at": bar["timestamp"], "displacement_index": i,
            "displacement_body": abs(bar["close"] - bar["open"]), "displacement_atr": prior_atr,
            "displacement_multiplier": displacement_atr,
            "break_id": event["id"] if event else None, "break_type": event["type"] if event else None,
            "break_level": event["level"] if event else None, "break_index": event["index"] if event else None,
            "pivot_timestamp": event["pivot_timestamp"] if event else None,
            "confirmed_at": event["confirmed_at"] if event else None,
            "structure_break_id": event["id"] if event else None,
            "status": "confirmed" if event else "candidate",
            "invalidation_timestamp": None,
            "invalidation_condition": inval_cond,
            "mitigated": False, "mitigation_status": "unconfirmed",
            "higher_timeframe_confirmation": None,
        }
    result = list(zones.values())
    _apply_mitigation_and_invalidation(result, rows)
    return result
