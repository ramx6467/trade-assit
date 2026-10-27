from app.analysis.smc import detect_fvgs, swings, market_structure, detect_order_blocks

def c(t, o, h, l, close):
    return {"timestamp": str(t), "open": o, "high": h, "low": l, "close": close}

def test_detect_bullish_gap_boundaries_and_future_fill():
    rows = [c(1, 8, 10, 7, 9), c(2, 10, 12, 9, 11), c(3, 13, 15, 11, 14), c(4, 14, 15, 10, 12)]
    zones = detect_fvgs(rows)
    assert zones[0]["direction"] == "bullish"
    assert (zones[0]["lower"], zones[0]["upper"]) == (10, 11)
    assert zones[0]["status"] == "mitigated"
    assert zones[0]["mitigation_status"] == "fully_mitigated"
    assert zones[0]["source_candle_timestamps"] == ["1", "2", "3"]
    assert zones[0]["instrument"] == "UNKNOWN"

def test_detect_bearish_gap_and_no_gap():
    rows = [c(1, 12, 14, 11, 13), c(2, 10, 13, 8, 9), c(3, 7, 9, 6, 8)]
    zones = detect_fvgs(rows)
    assert zones[0]["direction"] == "bearish"
    assert (zones[0]["lower"], zones[0]["upper"]) == (9, 11)
    assert zones[0]["status"] == "unmitigated"
    assert detect_fvgs([c(1,1,2,0,1),c(2,1,2,0,1),c(3,1,2,0,1)]) == []

def test_fvg_id_and_instrument_timeframe_scope_are_stable():
    rows = [c(f"2026-01-01T00:0{i}:00Z", *values) for i, values in enumerate([(8,10,7,9),(10,12,9,11),(13,15,11,14)])]
    a = detect_fvgs(rows, "EURUSD", "1m")[0]
    b = detect_fvgs(rows, "EURUSD", "1m")[0]
    other = detect_fvgs(rows, "XAUUSD", "1m")[0]
    assert a["id"] == b["id"] and a["id"] != other["id"]
    assert a["source_candle_timestamps"] == [row["timestamp"] for row in rows]
    assert a["data_source"] == "unknown"

def test_fvg_historical_as_of_does_not_use_later_mitigation():
    rows = [c(f"2026-01-01T00:0{i}:00Z", *values) for i, values in enumerate([(8,10,7,9),(10,12,9,11),(13,15,11,14),(14,15,9,10)])]
    current = detect_fvgs(rows, "EURUSD", "1m", as_of="2026-01-01T00:02:00Z")
    full = detect_fvgs(rows, "EURUSD", "1m")
    assert current[0]["status"] == "unmitigated"
    assert current[0]["source_candle_timestamps"] == [row["timestamp"] for row in rows[:3]]
    assert full[0]["status"] == "mitigated"

def test_swings_confirm_only_after_right_bars():
    rows = [c(i, 0, h, l, 0) for i,(h,l) in enumerate([(2,1),(3,0),(6,-1),(4,0),(3,1)])]
    result = swings(rows, left=1, right=1)
    assert result["swing_highs"] == [{"index":2,"confirmed_at":"3","price":6}]

def test_structure_break_waits_for_confirmed_pivot_and_is_one_shot():
    rows = [c(f"t{i}", *values) for i, values in enumerate([(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,11,8,10),(10,13,9,12),(12,14,11,13)])]
    result = market_structure(rows, left=1, right=1)
    assert result["breaks"] == [{"id":"BOS:high:1:5","direction":"bullish","type":"BOS","level":12,
        "pivot_index":1,"pivot_timestamp":"t1","confirmed_at":"t5","index":5}]
    assert result["rules"]["break_confirmation"] == "close beyond most recent confirmed pivot"

def test_order_block_requires_displacement_and_confirmed_structure_break():
    values=[(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,10,7,8),(8,11,8,10),(10,14,9,13),(13,14,12,13)]
    rows=[c(f"t{i}",*v) for i,v in enumerate(values)]
    zones=detect_order_blocks(rows,instrument="EURUSD",timeframe="1m",lookback=5,atr_period=2,
        displacement_atr=.8,pivot_left=1,pivot_right=1)
    confirmed=[z for z in zones if z["status"]=="confirmed"]
    assert len(confirmed)==1
    assert confirmed[0]["direction"]=="bullish"
    assert (confirmed[0]["lower"],confirmed[0]["upper"])==(7,10)
    assert confirmed[0]["created_at"]=="t3" and confirmed[0]["confirmed_at"]=="t5"
    before_break=detect_order_blocks(rows[:5],instrument="EURUSD",timeframe="1m",lookback=5,
        atr_period=2,displacement_atr=.8,pivot_left=1,pivot_right=1)
    assert all(z["status"]!="confirmed" for z in before_break)

def test_order_block_invalidation_uses_only_later_close_through_distal_edge():
    values=[(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,10,7,8),(8,11,8,10),(10,14,9,13),(13,14,6,6.5)]
    rows=[c(f"t{i}",*v) for i,v in enumerate(values)]
    zones=detect_order_blocks(rows,instrument="EURUSD",timeframe="1m",lookback=5,atr_period=2,
        displacement_atr=.8,pivot_left=1,pivot_right=1)
    assert any(z["status"]=="invalidated" and z["invalidation_timestamp"]=="t6" for z in zones)

_KW = dict(lookback=5, atr_period=2, displacement_atr=.8, pivot_left=1, pivot_right=1)

def _rows(values, prefix="t"):
    return [c(f"{prefix}{i}", *v) for i, v in enumerate(values)]

def test_order_block_detects_a_single_valid_bearish_block():
    rows = _rows([(10,12,9,11),(9,10,7,8),(8,9,7.5,8.5),(9,11,8.5,10.5),(10.5,11.5,10,11),(10,10.5,5,6),(6,7,5.5,6.5)])
    zones = detect_order_blocks(rows, instrument="XAUUSD", timeframe="5m", **_KW)
    confirmed = [z for z in zones if z["status"] == "confirmed"]
    assert len(confirmed) == 1
    assert confirmed[0]["direction"] == "bearish"
    # Full range of the originating bullish candle (t4): low 10, high 11.5.
    assert (confirmed[0]["lower"], confirmed[0]["upper"]) == (10, 11.5)
    assert confirmed[0]["created_at"] == "t4" and confirmed[0]["confirmed_at"] == "t5"
    assert confirmed[0]["instrument"] == "XAUUSD" and confirmed[0]["timeframe"] == "5m"

def test_order_block_detects_multiple_distinct_bullish_blocks():
    first = [(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,10,7,8),(8,11,8,10),(10,14,9,13),(13,14,12,13)]
    second = [(13,15,12.5,14),(14,16,13,15),(15,15.5,13.5,14),(14,14.5,13,13.5),(13.5,18,13,17),(17,18,16,16.5)]
    zones = detect_order_blocks(_rows(first + second), instrument="EURUSD", timeframe="1m", **_KW)
    confirmed = [z for z in zones if z["status"] == "confirmed"]
    assert len(confirmed) == 2
    assert {z["created_at"] for z in confirmed} == {"t3", "t10"}
    assert [(z["lower"], z["upper"]) for z in confirmed] == [(7, 10), (13, 14.5)]
    assert all(z["direction"] == "bullish" for z in confirmed)

def test_order_block_detects_mixed_directions_independently():
    rows = _rows([(9,10,8,9),(9,11,8.5,10.5),(10.5,10.9,10,10.6),(10.6,10.8,9.2,9.4),(9.4,10.3,9.3,10.2),
                  (10.2,13,10,12.8),(12.8,13,12,12.2),(12.2,12.6,11.8,12.4),(12.4,13.4,12.3,13.3),(13.3,13.4,10.5,10.7)])
    zones = detect_order_blocks(rows, instrument="EURUSD", timeframe="1m", **_KW)
    confirmed = [z for z in zones if z["status"] == "confirmed"]
    assert len(confirmed) == 2
    by_dir = {z["direction"]: z for z in confirmed}
    assert (by_dir["bullish"]["lower"], by_dir["bullish"]["upper"]) == (9.2, 10.8)
    assert (by_dir["bearish"]["lower"], by_dir["bearish"]["upper"]) == (12.3, 13.4)

def test_order_block_returns_empty_without_displacement_and_on_short_history():
    flat = _rows([(10,10.5,9.5,10)] * 7)
    assert detect_order_blocks(flat, **_KW) == []
    assert detect_order_blocks([c(1,1,2,0,1), c(2,1,2,0,1)], **_KW) == []

def test_order_block_body_boundary_option_normalises_open_close():
    rows = _rows([(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,10,7,8),(8,11,8,10),(10,14,9,13),(13,14,12,13)])
    full = detect_order_blocks(rows, instrument="EURUSD", timeframe="1m", **_KW)
    body = detect_order_blocks(rows, instrument="EURUSD", timeframe="1m", boundary="body", **_KW)
    assert next(z for z in full if z["status"]=="confirmed")["boundary_methodology"] == "full"
    z = next(z for z in body if z["status"]=="confirmed")
    # Origin candle t3 opened 9 and closed 8 -> body boundaries normalise to 8..9.
    assert (z["lower"], z["upper"], z["boundary_methodology"]) == (8, 9, "body")
    assert z["upper"] > z["lower"]

def test_order_block_dedupes_repeated_detections_of_the_same_origin():
    # Two bullish displacement candles both reference the same origin candle t3;
    # the first confirms and the second stays a candidate -> one zone only.
    rows = _rows([(9,10,8,8.5),(8.5,12,8,11.5),(11.5,11.8,10.5,11),(11,11.2,9.8,10),(10,10.9,9.9,10.6),
                  (10.6,12.2,10.5,12.1),(12.1,13,11.9,12.9)])
    zones = detect_order_blocks(rows, instrument="EURUSD", timeframe="1m", **_KW)
    assert len(zones) == 1
    assert zones[0]["created_at"] == "t3" and zones[0]["status"] == "confirmed"

def test_order_block_mitigation_flag_when_price_returns_into_the_zone():
    rows = _rows([(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,10,7,8),(8,11,8,10),(10,14,9,13),(13,14,12,13),(10.5,13,9,12)])
    z = next(x for x in detect_order_blocks(rows, instrument="EURUSD", timeframe="1m", **_KW) if x["status"]=="confirmed")
    assert z["mitigated"] is True
    assert z["mitigation_status"] == "partially_mitigated"
    assert z["invalidation_timestamp"] is None  # traded into the zone but never closed below its low

def test_order_block_drops_invalid_ohlc_and_keeps_valid_zones():
    rows = _rows([(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,10,7,8),(8,11,8,10),(10,14,9,13),(13,14,12,13)])
    rows.append(c("tBAD_high_low", 13, 12, 13.5, 13))              # high < low -> dropped
    rows.append(c("tBAD_nan", float("nan"), 14, 12, 13))           # non-finite -> dropped
    zones = detect_order_blocks(rows, instrument="EURUSD", timeframe="1m", **_KW)
    assert any(z["status"]=="confirmed" and (z["lower"],z["upper"])==(7,10) for z in zones)
    assert all("tBAD" not in z["created_at"] for z in zones)

def test_order_block_is_scoped_to_instrument_and_timeframe_and_switches():
    rows = _rows([(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,10,7,8),(8,11,8,10),(10,14,9,13),(13,14,12,13)])
    a = detect_order_blocks(rows, instrument="EURUSD", timeframe="5m", **_KW)[0]
    b = detect_order_blocks(rows, instrument="EURUSD", timeframe="15m", **_KW)[0]
    d = detect_order_blocks(rows, instrument="XAUUSD", timeframe="5m", **_KW)[0]
    assert a["id"] != b["id"] != d["id"] and a["id"] != d["id"]  # identity switches with timeframe or instrument
    assert (b["instrument"], b["timeframe"]) == ("EURUSD", "15m")
    assert (d["instrument"], d["timeframe"]) == ("XAUUSD", "5m")

def test_order_block_uses_only_pivots_established_before_the_displacement():
    rows = _rows([(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,10,7,8),(8,11,8,10),(10,14,9,13),(13,14,12,13)])
    z = next(x for x in detect_order_blocks(rows, instrument="EURUSD", timeframe="1m", **_KW) if x["status"]=="confirmed")
    # The broken pivot (swing high 12 at t1) predates the displacement at t5; the
    # origin candle (t3) also predates both, so no future candle shapes the zone.
    assert z["pivot_timestamp"] == "t1" and z["pivot_timestamp"] < z["displacement_at"]
    assert z["break_index"] >= rows.index(next(r for r in rows if r["timestamp"] == z["displacement_at"]))
    assert (z["lower"], z["upper"]) == (7, 10)  # boundaries come from the earlier origin candle

def test_order_block_metadata_records_qualification_details():
    rows = _rows([(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,10,7,8),(8,11,8,10),(10,14,9,13),(13,14,12,13)])
    z = next(x for x in detect_order_blocks(rows, instrument="EURUSD", timeframe="1m", **_KW) if x["status"]=="confirmed")
    assert z["break_type"] in {"BOS", "CHoCH"} and z["break_level"] == 12
    assert z["displacement_multiplier"] == .8 and z["displacement_atr"] > 0
    assert z["origin_candle_direction"] == "bearish"  # bullish OB originates from a bearish candle
    assert z["higher_timeframe_confirmation"] is None

def test_order_blocks_are_valid_and_scoped_across_every_supported_timeframe():
    from app.market_data.demo import candles as demo_candles
    for timeframe in ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]:
        rows = demo_candles("EURUSD", timeframe, 240)
        stamps = {row["timestamp"] for row in rows}
        zones = detect_order_blocks(rows, instrument="EURUSD", timeframe=timeframe)
        for z in zones:
            assert z["timeframe"] == timeframe and z["instrument"] == "EURUSD"
            assert z["upper"] > z["lower"] and all(map(lambda v: v == v and abs(v) != float("inf"), (z["lower"], z["upper"])))
            assert z["created_at"] in stamps
            assert z["status"] in {"confirmed", "candidate", "invalidated"}
        assert all(z["timeframe"] == timeframe for z in zones)

def test_order_block_truncated_history_does_not_use_future_candles():
    # Detecting on the prefix that ends exactly at the displacement confirms the
    # zone; removing the displacement candle removes the confirmation, proving the
    # detector never uses candles beyond the data it was given.
    full = _rows([(9,10,8,9),(10,12,9,11),(10,11,8,9),(9,10,7,8),(8,11,8,10),(10,14,9,13),(13,14,12,13)])
    with_break = detect_order_blocks(full[:6], instrument="EURUSD", timeframe="1m", **_KW)
    without = detect_order_blocks(full[:5], instrument="EURUSD", timeframe="1m", **_KW)
    assert any(z["status"] == "confirmed" and (z["lower"], z["upper"]) == (7, 10) for z in with_break)
    assert all(z["status"] != "confirmed" for z in without)


def test_dealing_range_reports_bounds_equilibrium_and_provenance():
    from app.analysis.smc import dealing_range

    prices = [1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3]
    candles = [
        {
            'timestamp': f'2026-01-01T{i:02d}:00:00Z',
            'open': p, 'high': p, 'low': p, 'close': p,
        }
        for i, p in enumerate(prices)
    ]
    bounds = dealing_range(candles)
    assert bounds is not None
    assert bounds['high'] == 5
    assert bounds['low'] == 1
    assert bounds['range'] == 4
    assert bounds['equilibrium'] == 3
    assert bounds['high_confirmed_at'] is not None
    assert bounds['low_confirmed_at'] is not None


def test_market_structure_exposes_equal_highs_and_lows():
    from app.analysis.smc import market_structure

    # Two swing highs at the same price form an equal-highs cluster.
    highs = [1, 5, 1, 5, 1, 2]
    candles = [
        {'timestamp': f'2026-01-01T{i:02d}:00:00Z', 'open': p, 'high': p, 'low': p - 0.5, 'close': p}
        for i, p in enumerate(highs)
    ]
    structure = market_structure(candles)
    assert 'equal_highs' in structure and 'equal_lows' in structure
    assert structure['equal_highs'] == structure['resistance_clusters']
    assert structure['equal_lows'] == structure['support_clusters']
