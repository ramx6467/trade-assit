"""Comprehensive evidence-based trade planning and setup evaluation engine.

Implements deterministic evaluation for the setup states BUY_CANDIDATE,
SELL_CANDIDATE, WATCHLIST, WAITING_FOR_CONFIRMATION, INVALIDATED, NO_SETUP,
DATA_UNAVAILABLE, and DATA_STALE.
Calculates tick-aware entry, stop-loss, take-profit 1/2/3, per-target
risk/reward, risk budget, and instrument-specific position sizing.

Every level is derived from actual OHLC candles, confirmed market structure,
indicators, and explicit risk-management rules. No price is fabricated and the
AI never sets or edits a numeric level.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from app.analysis.indicators import ema, rsi, macd, atr
from app.analysis.smc import market_structure, detect_fvgs, detect_order_blocks
from app.market_data.demo import SYMBOLS

TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
}

INSTRUMENT_SPECS: dict[str, dict[str, Any]] = {
    "EURUSD": {
        "contract_size": 100000,
        "quote_currency": "USD",
        "unit_name": "standard lots (100k)",
        "pip_size": 0.0001,
        "pip_value_per_lot": 10.0,
        "min_quantity": 0.01,
        "step_size": 0.01,
        "price_precision": 5,
        "tick_size": 0.00001,
    },
    "GBPUSD": {
        "contract_size": 100000,
        "quote_currency": "USD",
        "unit_name": "standard lots (100k)",
        "pip_size": 0.0001,
        "pip_value_per_lot": 10.0,
        "min_quantity": 0.01,
        "step_size": 0.01,
        "price_precision": 5,
        "tick_size": 0.00001,
    },
    "XAUUSD": {
        "contract_size": 100,  # 1 lot = 100 oz
        "quote_currency": "USD",
        "unit_name": "lots (100 oz)",
        "pip_size": 0.01,
        "pip_value_per_lot": 1.0,
        "min_quantity": 0.01,
        "step_size": 0.01,
        "price_precision": 2,
        "tick_size": 0.01,
    },
    "NIFTY": {
        "contract_size": 25,
        "quote_currency": "INR",
        "unit_name": "contracts (1 lot = 25)",
        "pip_size": 0.05,
        "pip_value_per_lot": 1.25,
        "min_quantity": 25,
        "step_size": 25,
        "price_precision": 2,
        "tick_size": 0.05,
    },
    "BANKNIFTY": {
        "contract_size": 15,
        "quote_currency": "INR",
        "unit_name": "contracts (1 lot = 15)",
        "pip_size": 0.05,
        "pip_value_per_lot": 0.75,
        "min_quantity": 15,
        "step_size": 15,
        "price_precision": 2,
        "tick_size": 0.05,
    },
}


def round_tick(price: float, tick: float, precision: int) -> float:
    """Round price to the nearest tick and instrument precision."""
    if tick <= 0:
        return round(price, precision)
    return round(round(price / tick) * tick, precision)


def validate_candles(candles: list[dict]) -> list[str]:
    """Return a list of data-quality problems found in the supplied candles.

    This is a defensive, read-only check that runs before evaluation. Real
    provider data is already normalised by ``app.market_data.normalize``; this
    guards direct callers and catches out-of-order, duplicated or inconsistent
    OHLC rows. The strings returned are descriptive; an empty list means clean.
    """
    problems: list[str] = []
    seen: set[str] = set()
    prev_ts: str | None = None
    for i, candle in enumerate(candles):
        if not isinstance(candle, dict):
            problems.append(f"candle[{i}] is not a mapping")
            continue
        try:
            o = float(candle["open"])
            h = float(candle["high"])
            l = float(candle["low"])
            c = float(candle["close"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"candle[{i}] has missing or non-numeric OHLC values")
            continue
        if not all(math.isfinite(v) for v in (o, h, l, c)):
            problems.append(f"candle[{i}] has non-finite OHLC values")
            continue
        if h < l:
            problems.append(f"candle[{i}] high {h} is below low {l}")
        if h < max(o, c) or l > min(o, c):
            problems.append(f"candle[{i}] OHLC values are internally inconsistent")
        ts = candle.get("timestamp")
        if ts is not None:
            ts_str = str(ts)
            if ts_str in seen:
                problems.append(f"duplicate candle timestamp {ts_str}")
            seen.add(ts_str)
            if prev_ts is not None and ts_str < prev_ts:
                problems.append("candles are not in chronological order")
            prev_ts = ts_str
    return problems


def _plan_take_profits(
    side: str,
    entry: float,
    stop: float,
    structure: dict,
    blocks: list[dict],
    tick: float,
    precision: int,
    fvgs: list[dict] | None = None,
) -> list[float]:
    """Select up to three take-profit levels from real structure, then R multiples.

    Each rung N is anchored to its R-multiple (1.5R, 2.0R, 3.0R) and may snap
    outward to the nearest real structural level that falls inside that rung's
    risk band ([N-th R, (N+1)-th R), with the final rung open-ended). When no
    structural level qualifies, the R-multiple itself is used. No level is
    invented beyond these two documented sources, and the ladder is always
    monotonic in the trade direction.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return []

    candidates: list[float] = []
    if side == "BUY":
        for s in structure.get("swing_highs", []) or []:
            try:
                lvl = float(s.get("price"))
            except (TypeError, ValueError):
                continue
            if lvl > entry:
                candidates.append(lvl)
        for b in blocks or []:
            if str(b.get("direction", "")).lower() != "bearish":
                continue
            try:
                lvl = float(b.get("lower"))
            except (TypeError, ValueError):
                continue
            if lvl > entry:
                candidates.append(lvl)
        for cluster in structure.get("resistance_clusters", []) or []:
            try:
                lvl = float(cluster.get("level"))
            except (TypeError, ValueError):
                continue
            if lvl > entry:
                candidates.append(lvl)
        for pool in structure.get("buy_side_liquidity", []) or []:
            try:
                lvl = float(pool.get("level"))
            except (TypeError, ValueError):
                continue
            if lvl > entry:
                candidates.append(lvl)
        for f in fvgs or []:
            if str(f.get("direction", "")).lower() != "bearish":
                continue
            try:
                lvl = float(f.get("lower"))
            except (TypeError, ValueError):
                continue
            if lvl > entry:
                candidates.append(lvl)
    else:
        for s in structure.get("swing_lows", []) or []:
            try:
                lvl = float(s.get("price"))
            except (TypeError, ValueError):
                continue
            if lvl < entry:
                candidates.append(lvl)
        for b in blocks or []:
            if str(b.get("direction", "")).lower() != "bullish":
                continue
            try:
                lvl = float(b.get("upper"))
            except (TypeError, ValueError):
                continue
            if lvl < entry:
                candidates.append(lvl)
        for cluster in structure.get("support_clusters", []) or []:
            try:
                lvl = float(cluster.get("level"))
            except (TypeError, ValueError):
                continue
            if lvl < entry:
                candidates.append(lvl)
        for pool in structure.get("sell_side_liquidity", []) or []:
            try:
                lvl = float(pool.get("level"))
            except (TypeError, ValueError):
                continue
            if lvl < entry:
                candidates.append(lvl)
        for f in fvgs or []:
            if str(f.get("direction", "")).lower() != "bullish":
                continue
            try:
                lvl = float(f.get("upper"))
            except (TypeError, ValueError):
                continue
            if lvl < entry:
                candidates.append(lvl)

    # Sort structural candidates nearest-first and de-duplicate on the tick grid.
    candidates = sorted(
        {round_tick(c, tick, precision) for c in candidates},
        reverse=(side == "SELL"),
    )

    # Each rung is anchored to its R-multiple (1.5R, 2.0R, 3.0R) and may snap
    # outward to the nearest real structural level that falls inside that rung's
    # risk band [N-th R, (N+1)-th R). Banding keeps the ladder strictly monotonic
    # and stops one far structural level from consuming all the nearer rungs.
    bands = ((1.5, 2.0), (2.0, 3.0), (3.0, None))
    ladder: list[float] = []
    prev = entry
    for lo, hi in bands:
        base_lo = round_tick(
            entry + risk * lo if side == "BUY" else entry - risk * lo,
            tick,
            precision,
        )
        base_hi = None if hi is None else round_tick(
            entry + risk * hi if side == "BUY" else entry - risk * hi,
            tick,
            precision,
        )
        pick = base_lo
        for lvl in candidates:  # already sorted nearest-first for the side
            if side == "BUY":
                in_band = lvl >= base_lo and (base_hi is None or lvl < base_hi)
                if in_band and lvl > prev:
                    pick = lvl
                    break
            else:
                in_band = lvl <= base_lo and (base_hi is None or lvl > base_hi)
                if in_band and lvl < prev:
                    pick = lvl
                    break
        ladder.append(pick)
        prev = pick
    return ladder


def calculate_position_size(
    symbol: str,
    equity: Decimal,
    risk_percent: float,
    entry: float,
    stop_loss: float,
    user_unit_value: float | None = None,
) -> dict[str, Any] | None:
    """Calculate deterministic position size according to instrument specs.

    If contract specifications or conversion details are unknown, returns an explicit
    notice instead of inventing quantities.
    """
    spec = INSTRUMENT_SPECS.get(symbol)
    if not spec:
        return {
            "quantity": None,
            "unit": "units",
            "quote_currency": "UNKNOWN",
            "equity_assumption": str(equity),
            "risk_percent": risk_percent,
            "risk_amount": None,
            "risk_per_unit": None,
            "note": "Position size unavailable: contract or conversion details required",
        }

    distance = abs(entry - stop_loss)
    if distance <= 0:
        return None

    quote = spec["quote_currency"]
    risk_amount = round(float(equity) * (risk_percent / 100.0), 2)

    # Use user-supplied unit value if explicitly provided and positive
    if user_unit_value is not None and user_unit_value > 0:
        raw_qty = risk_amount / (distance * user_unit_value)
        qty = round(raw_qty, 2)
        return {
            "quantity": qty,
            "unit": f"units (multiplier {user_unit_value})",
            "quote_currency": quote,
            "equity_assumption": str(equity),
            "risk_percent": risk_percent,
            "risk_amount": risk_amount,
            "risk_per_unit": round(distance * user_unit_value, 4),
            "note": f"Custom unit sizing: {risk_percent}% risk ({quote} {risk_amount}) across {distance:.5f} price distance.",
        }

    contract_size = spec["contract_size"]
    loss_per_contract = distance * contract_size

    if loss_per_contract <= 0:
        return None

    if spec["step_size"] < 1:
        # Forex / Gold decimal lot sizing (min 0.01 lot)
        raw_lots = round(risk_amount / loss_per_contract, 4)
        step = spec["step_size"]
        lots = max(spec["min_quantity"], round(round(raw_lots / step) * step, 2))
        return {
            "quantity": lots,
            "unit": spec["unit_name"],
            "quote_currency": quote,
            "equity_assumption": str(equity),
            "risk_percent": risk_percent,
            "risk_amount": risk_amount,
            "risk_per_unit": round(distance, 5),
            "contract_size": contract_size,
            "note": f"Risk budget {quote} {risk_amount:.2f} ({risk_percent}% of {equity:,.0f}). Sized at {lots} {spec['unit_name']}.",
        }
    else:
        # Discrete contract sizing (e.g. NIFTY: loss_per_contract = distance * contract_size)
        raw_lots = round(risk_amount / loss_per_contract, 4)
        num_lots = max(1, int(round(raw_lots)))
        contracts = num_lots * int(spec["step_size"])
        return {
            "quantity": contracts,
            "unit": f"{spec['unit_name']} ({num_lots} lot{'s' if num_lots != 1 else ''})",
            "quote_currency": quote,
            "equity_assumption": str(equity),
            "risk_percent": risk_percent,
            "risk_amount": risk_amount,
            "risk_per_unit": round(distance, 2),
            "contract_size": contract_size,
            "note": f"Risk budget {quote} {risk_amount:.2f} ({risk_percent}% of {equity:,.0f}). Sized at {contracts} contracts ({num_lots} lots).",
        }


def evaluate_trading_setup(
    symbol: str,
    timeframe: str,
    side: Literal["BUY", "SELL"],
    candles: list[dict],
    warnings: list[str],
    provider: str,
    data_status: str,
    min_rr: float = 1.5,
    max_risk_percent: float = 1.0,
    equity: Decimal = Decimal("100000"),
    user_unit_value: float | None = None,
    precomputed_indicators: dict[str, Any] | None = None,
    precomputed_structure: dict[str, Any] | None = None,
    precomputed_fvgs: list[dict] | None = None,
    precomputed_order_blocks: list[dict] | None = None,
    quote: dict[str, Any] | None = None,
    htf_candles: list[dict] | None = None,
    htf_timeframe: str | None = None,
) -> dict[str, Any]:
    """Execute complete deterministic trade evaluation pipeline.

    Evaluates:
    - Freshness / data integrity
    - Authoritative quote snapshot
    - Indicators (EMA20, EMA50, RSI14, MACD, ATR14)
    - Smart Money Concepts (BOS, CHoCH, MSS, FVGs, Order Blocks, Liquidity Sweeps, BSL/SSL)
    - Multi-timeframe structural alignment
    - Trend pullback and breakout/retest setup setups
    - Correct directional side for stop-loss and take-profits
    - Risk/reward validation (>= min_rr)
    - Position sizing per instrument
    - Explanations, satisfied & missing conditions, and next conditions to watch
    """
    symbol = symbol.upper()
    now = datetime.now(timezone.utc)
    spec = INSTRUMENT_SPECS.get(symbol, {
        "price_precision": 5 if "USD" in symbol else 2,
        "tick_size": 0.00001 if "USD" in symbol else 0.01,
        "quote_currency": "USD",
        "unit_name": "units",
    })
    precision = spec["price_precision"]
    tick = spec["tick_size"]
    expected_side = side

    # Extract validated quote if provided
    quote_price: float | None = None
    quote_ts: str | None = None
    quote_age_seconds: float | None = None
    if quote and isinstance(quote, dict):
        try:
            qp = float(quote.get("price"))
            if math.isfinite(qp) and qp > 0:
                quote_price = qp
        except (TypeError, ValueError):
            pass
        quote_ts = quote.get("timestamp")
        age_val = quote.get("observation_age_seconds")
        if age_val is not None:
            try:
                quote_age_seconds = float(age_val)
            except (TypeError, ValueError):
                pass
        elif quote_ts:
            try:
                qdt = datetime.fromisoformat(str(quote_ts).replace("Z", "+00:00"))
                quote_age_seconds = max(0.0, (now - qdt).total_seconds())
            except Exception:
                pass

    # 1. Validate candles availability
    if not candles:
        return _make_empty_response(
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            provider=provider,
            data_status=data_status,
            status="DATA_UNAVAILABLE",
            explanation="Setup evaluation blocked: no candle data available for this instrument and timeframe.",
            missing=["Valid candle history"],
            max_risk=max_risk_percent,
            warnings=warnings,
            quote_price=quote_price,
            quote_ts=quote_ts,
            quote_age_seconds=quote_age_seconds,
        )

    # 1b. Validate candle integrity (defensive; provider data is pre-normalised)
    data_problems = validate_candles(candles)
    fatal_problems = [
        p for p in data_problems
        if ("below low" in p) or ("non-finite" in p) or ("internally inconsistent" in p)
        or ("non-numeric" in p) or ("missing or" in p)
    ]
    if fatal_problems:
        return _make_empty_response(
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            provider=provider,
            data_status=data_status,
            status="DATA_UNAVAILABLE",
            explanation=(
                "Setup evaluation blocked: candle data failed integrity checks ("
                + "; ".join(fatal_problems[:3]) + ")."
            ),
            missing=["Valid, internally consistent candle history"],
            max_risk=max_risk_percent,
            warnings=warnings + data_problems,
            quote_price=quote_price,
            quote_ts=quote_ts,
            quote_age_seconds=quote_age_seconds,
        )
    if data_problems:
        warnings = warnings + [p for p in data_problems if p not in warnings]

    last_candle = candles[-1]
    candle_close = float(last_candle["close"])
    current_price = quote_price if quote_price is not None else candle_close

    # 2. Check freshness
    seconds = TIMEFRAME_SECONDS.get(timeframe, 900)
    candle_time = datetime.fromisoformat(last_candle["timestamp"].replace("Z", "+00:00"))
    age = (now - candle_time).total_seconds() if candle_time else None
    freshness_seconds = max(0, int(age)) if age is not None else None

    # Stale condition: non-DEMO data older than 4 intervals
    is_stale = False
    if provider != "DEMO" and age is not None and age > seconds * 4:
        is_stale = True

    if is_stale:
        return _make_empty_response(
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            provider=provider,
            data_status=data_status,
            status="DATA_STALE",
            explanation=f"Data is stale: last candle received {freshness_seconds}s ago (exceeds {seconds * 4}s threshold). Live trade evaluation requires fresh quotes.",
            missing=["Fresh real-time feed updates"],
            max_risk=max_risk_percent,
            warnings=warnings + ["Feed timestamp is significantly behind wall clock."],
            current_price=current_price,
            indicators=precomputed_indicators,
            smc=[],
            market_bias="NEUTRAL",
            freshness_seconds=freshness_seconds,
            quote_price=quote_price,
            quote_ts=quote_ts,
            quote_age_seconds=quote_age_seconds,
            candle_ts=last_candle.get("timestamp"),
        )

    # Validate minimum candle history for reliable indicator and structure computation
    if not precomputed_indicators and len(candles) < 20:
        return _make_empty_response(
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            provider=provider,
            data_status=data_status,
            status="INSUFFICIENT_DATA",
            explanation=f"Insufficient candle history for {timeframe}: received {len(candles)} candle(s), minimum 20 required for indicator calculation and market structure analysis.",
            missing=[f"Minimum 20 historical candles for {timeframe} (received {len(candles)})"],
            max_risk=max_risk_percent,
            warnings=warnings + [f"Insufficient historical data: received {len(candles)} of 20 minimum bars."],
            current_price=current_price,
            quote_price=quote_price,
            quote_ts=quote_ts,
            quote_age_seconds=quote_age_seconds,
            candle_ts=last_candle.get("timestamp"),
            freshness_seconds=freshness_seconds,
        )

    # 3. Calculate technical indicators (or use precomputed)
    if precomputed_indicators:
        indicators_dict = dict(precomputed_indicators)
        ema20_val = indicators_dict.get("ema20")
        ema50_val = indicators_dict.get("ema50")
        rsi_val = indicators_dict.get("rsi14")
        atr_val = indicators_dict.get("atr14")
        macd_val = indicators_dict.get("macd")
    else:
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        closes = [float(c["close"]) for c in candles]

        ema20_series = ema(closes, 20) if len(closes) >= 20 else []
        ema50_series = ema(closes, 50) if len(closes) >= 50 else []
        rsi_series = rsi(closes, 14) if len(closes) >= 15 else []
        atr_series = atr(highs, lows, closes, 14) if len(closes) >= 14 else []
        macd_res = macd(closes) if len(closes) >= 35 else {"line": [None] * len(closes), "signal": [None] * len(closes), "histogram": [None] * len(closes)}

        ema20_val = ema20_series[-1] if ema20_series else None
        ema50_val = ema50_series[-1] if ema50_series else None
        rsi_val = rsi_series[-1] if rsi_series else None
        atr_val = atr_series[-1] if atr_series else None
        macd_val = {
            "line": macd_res["line"][-1] if macd_res["line"] else None,
            "signal": macd_res["signal"][-1] if macd_res["signal"] else None,
            "histogram": macd_res["histogram"][-1] if macd_res["histogram"] else None,
        }
        indicators_dict = {
            "ema20": ema20_val,
            "ema50": ema50_val,
            "rsi14": rsi_val,
            "atr14": atr_val,
            "macd": macd_val,
            "candle_timestamp": last_candle["timestamp"],
        }

    # 4. Analyze Smart Money Concepts & Structure (or use precomputed)
    structure = precomputed_structure if precomputed_structure is not None else market_structure(candles)
    all_fvgs = precomputed_fvgs if precomputed_fvgs is not None else detect_fvgs(candles, instrument=symbol, timeframe=timeframe)
    all_blocks = precomputed_order_blocks if precomputed_order_blocks is not None else detect_order_blocks(candles, instrument=symbol, timeframe=timeframe)

    expected_dir = "bullish" if side == "BUY" else "bearish"
    opposing_dir = "bearish" if side == "BUY" else "bullish"

    # Recent structure breaks in the past 25 candles or on current candle
    recent_breaks = [
        b for b in structure.get("breaks", [])
        if b.get("direction") == expected_dir and (
            b.get("confirmed_at") == last_candle["timestamp"]
            or "index" not in b
            or b.get("index", 0) >= max(0, len(candles) - 25)
        )
    ]
    recent_opposing_breaks = [
        b for b in structure.get("breaks", [])
        if b.get("direction") == opposing_dir and (
            b.get("confirmed_at") == last_candle["timestamp"]
            or ("index" in b and b.get("index", 0) >= max(0, len(candles) - 15))
        )
    ]

    # Active order blocks and FVGs
    active_obs = [b for b in all_blocks if b.get("direction") == expected_dir and b.get("status") == "confirmed"]
    active_fvgs = [z for z in all_fvgs if z.get("direction") == expected_dir and z.get("status") == "unmitigated"]

    smc_summary = [f"{b.get('type', 'BOS')} {b.get('direction', expected_dir)} at {b.get('level', '')} (confirmed {b.get('confirmed_at', 'confirmed')})" for b in recent_breaks[-3:]]
    smc_summary += [f"{z.get('direction', expected_dir)} FVG {z.get('lower', '')}–{z.get('upper', '')} ({z.get('status', 'unmitigated')})" for z in active_fvgs[-3:]]
    smc_summary += [f"{z.get('direction', expected_dir)} order block {z.get('lower', '')}–{z.get('upper', '')}" for z in active_obs[-3:]]
    recent_sweeps = (structure.get("liquidity_sweeps") or [])[-3:]
    smc_summary += [f"liquidity sweep {s.get('direction', '')} at {s.get('level', '')} ({s.get('timestamp', s.get('pivot_timestamp', 'recent'))})" for s in recent_sweeps]

    # Market bias determination
    if ema20_val is not None and ema50_val is not None:
        market_bias = "BULLISH" if ema20_val > ema50_val else "BEARISH" if ema20_val < ema50_val else "NEUTRAL"
    else:
        market_bias = "NEUTRAL"

    # 5. Evaluate conditions for BUY and SELL
    conditions: dict[str, bool] = {}
    conditions_detail: dict[str, str] = {}

    # A. Trend Alignment
    trend_aligned = False
    if ema20_val is not None and ema50_val is not None:
        trend_aligned = (ema20_val > ema50_val) if side == "BUY" else (ema20_val < ema50_val)
    conditions["EMA trend alignment"] = trend_aligned
    conditions_detail["EMA trend alignment"] = (
        f"EMA20 ({ema20_val:.5f}) {'above' if side == 'BUY' else 'below'} EMA50 ({ema50_val:.5f})"
        if ema20_val and ema50_val else "EMA20/50 unavailable"
    )

    # B. Momentum / RSI Condition
    # For BUY: healthy momentum is RSI between 35 and 68. Overbought > 70 requires cooldown. Oversold < 30 in downtrend is counter-trend.
    # For SELL: healthy momentum is RSI between 32 and 65. Oversold < 30 requires bounce.
    rsi_ok = False
    if rsi_val is not None:
        if side == "BUY":
            rsi_ok = 35.0 <= rsi_val <= 68.0
        else:
            rsi_ok = 32.0 <= rsi_val <= 65.0
    conditions["RSI momentum condition"] = rsi_ok
    conditions_detail["RSI momentum condition"] = (
        f"RSI14 is {rsi_val:.1f} (acceptable zone {35 if side == 'BUY' else 32}–{68 if side == 'BUY' else 65})"
        if rsi_val is not None else "RSI unavailable"
    )

    # C. Positive Volatility / ATR
    atr_ok = atr_val is not None and atr_val > 0 and (atr_val >= tick)
    conditions["Positive ATR volatility"] = bool(atr_ok)
    conditions_detail["Positive ATR volatility"] = f"ATR14 = {atr_val:.5f}" if atr_val else "ATR unavailable"

    # D. Market Structure Support / SMC
    # Confirmed BOS/CHoCH in recent 25 candles, OR aligned active order block or FVG
    has_smc_structure = bool(recent_breaks) or bool(active_obs) or bool(active_fvgs)
    conditions["Confirmed structure alignment"] = has_smc_structure
    conditions_detail["Confirmed structure alignment"] = (
        f"{len(recent_breaks)} confirmed {expected_dir} break(s), {len(active_obs)} OB(s), {len(active_fvgs)} FVG(s)"
        if has_smc_structure else f"No recent confirmed {expected_dir} structure break or active zone"
    )

    # E. No Conflicting Major Opposing Break
    no_opposing_conflict = len(recent_opposing_breaks) == 0 or (
        recent_breaks and recent_breaks[-1]["index"] > recent_opposing_breaks[-1]["index"]
    )
    conditions["No conflicting opposing structure"] = bool(no_opposing_conflict)
    conditions_detail["No conflicting opposing structure"] = (
        "Market structure direction is consistent" if no_opposing_conflict
        else f"Recent opposing {opposing_dir} structure break detected"
    )

    # F. Multi-timeframe structural alignment (when HTF data is supplied)
    htf_alignment = "ALIGNED"
    htf_bias = "NEUTRAL"
    if htf_candles and len(htf_candles) >= 15:
        try:
            htf_closes = [float(c["close"]) for c in htf_candles]
            htf_e20 = ema(htf_closes, 20) if len(htf_closes) >= 20 else []
            htf_e50 = ema(htf_closes, 50) if len(htf_closes) >= 50 else []
            htf_struct = market_structure(htf_candles)
            if htf_struct.get("structural_bias") and htf_struct["structural_bias"] != "NEUTRAL":
                htf_bias = htf_struct["structural_bias"]
            elif htf_e20 and htf_e50:
                htf_bias = "BULLISH" if htf_e20[-1] > htf_e50[-1] else "BEARISH" if htf_e20[-1] < htf_e50[-1] else "NEUTRAL"
            elif htf_e20:
                htf_bias = "BULLISH" if (htf_closes[-1] >= htf_e20[-1]) else "BEARISH"
            else:
                htf_bias = "BULLISH" if htf_closes[-1] > htf_closes[0] else "BEARISH" if htf_closes[-1] < htf_closes[0] else "NEUTRAL"

            if side == "BUY" and htf_bias == "BEARISH":
                htf_alignment = "CONFLICTING"
            elif side == "SELL" and htf_bias == "BULLISH":
                htf_alignment = "CONFLICTING"
            else:
                htf_alignment = "ALIGNED"
        except Exception:
            htf_alignment = "UNKNOWN"

        if htf_alignment != "UNKNOWN":
            conditions["Higher-timeframe alignment"] = (htf_alignment == "ALIGNED")
            conditions_detail["Higher-timeframe alignment"] = (
                f"HTF ({htf_timeframe or 'HTF'}) bias is {htf_bias} ({'aligned' if htf_alignment == 'ALIGNED' else 'conflicting'})"
            )

    if structure is not None:
        structure["higher_timeframe_alignment"] = {
            "timeframe": htf_timeframe,
            "bias": htf_bias,
            "status": htf_alignment,
        }

    satisfied = [k for k, v in conditions.items() if v]
    missing = [k for k, v in conditions.items() if not v]

    # Check for provider notice
    risks: list[str] = list(warnings)
    if provider == "DEMO":
        risks.append("Evaluation running on synthetic DEMO candles; levels are model simulations.")
    risks.append(f"Maximum paper risk budget is {max_risk_percent:g}% of assumed {spec['quote_currency']} equity; min R/R is {min_rr:g}:1.")

    # 6. Trade Candidate Synthesis & Level Calculations
    # Actionable setups evaluated, derived from real candles:
    #   1. Structure Breakout & Retest (RETEST_ENTRY)
    #   2. Order Block Retest (ORDER_BLOCK_ENTRY)
    #   3. FVG Retest (FVG_ENTRY)
    #   4. Trend Pullback & Continuation (PULLBACK_ENTRY)
    entry: float | None = None
    stop: float | None = None
    setup_type: str | None = None
    trigger: str | None = None
    invalidation: str | None = None
    stop_basis: str | None = None

    if atr_val is not None and atr_val > 0:
        if side == "BUY":
            if recent_breaks:
                setup_type = "RETEST_ENTRY"
                entry = round_tick(float(recent_breaks[-1]["level"]), tick, precision)
                trigger = f"Wait for price to retest the confirmed {expected_dir} structure level {entry} (zone {round_tick(entry - tick, tick, precision)}–{round_tick(entry + tick, tick, precision)}); place no market order before the retest."
                stop = round_tick(entry - max(1.5 * float(atr_val), tick), tick, precision)
                stop_basis = "1.5x ATR below the retested structure level"
                invalidation = f"Candle close through stop {stop}, or an opposing confirmed structure break."
            elif active_obs and any(b.get("lower") is not None for b in active_obs):
                best_ob = active_obs[-1]
                ob_upper = float(best_ob["upper"])
                ob_lower = float(best_ob["lower"])
                if ob_lower - 0.5 * atr_val <= current_price <= ob_upper + 0.5 * atr_val:
                    setup_type = "ORDER_BLOCK_ENTRY"
                    entry = round_tick(min(current_price, ob_upper), tick, precision)
                    stop = round_tick(ob_lower - max(1.0 * float(atr_val), tick), tick, precision)
                    stop_basis = f"1.0x ATR below confirmed bullish order block ({ob_lower})"
                    trigger = f"Wait for test of bullish order block zone {ob_lower}–{ob_upper}."
                    invalidation = f"Candle close below order block stop {stop}."
            elif active_fvgs and any(f.get("lower") is not None for f in active_fvgs):
                best_fvg = active_fvgs[-1]
                fvg_upper = float(best_fvg["upper"])
                fvg_lower = float(best_fvg["lower"])
                if fvg_lower - 0.5 * atr_val <= current_price <= fvg_upper + 0.5 * atr_val:
                    setup_type = "FVG_ENTRY"
                    entry = round_tick(min(current_price, fvg_upper), tick, precision)
                    stop = round_tick(fvg_lower - max(1.0 * float(atr_val), tick), tick, precision)
                    stop_basis = f"1.0x ATR below bullish FVG lower boundary ({fvg_lower})"
                    trigger = f"Wait for fill/retest of bullish FVG zone {fvg_lower}–{fvg_upper}."
                    invalidation = f"Candle close below FVG stop {stop}."
            if setup_type is None and trend_aligned and ema20_val and current_price >= ema20_val - 0.5 * atr_val:
                setup_type = "PULLBACK_ENTRY"
                entry = round_tick(max(current_price, ema20_val), tick, precision)
                stop = round_tick(min(ema50_val - 0.2 * atr_val, entry - max(1.5 * float(atr_val), tick)), tick, precision)
                stop_basis = "below max(EMA50 - 0.2xATR, entry - 1.5xATR)"
                trigger = f"Trend continuation entry on pullback confirmation at {entry}; require a bullish candle close above EMA20."
                invalidation = f"Candle close below stop-loss {stop}, or a confirmed bearish structure break."

        else:  # SELL Setup
            if recent_breaks:
                setup_type = "RETEST_ENTRY"
                entry = round_tick(float(recent_breaks[-1]["level"]), tick, precision)
                trigger = f"Wait for price to retest the confirmed {expected_dir} structure level {entry} (zone {round_tick(entry - tick, tick, precision)}–{round_tick(entry + tick, tick, precision)}); place no market order before the retest."
                stop = round_tick(entry + max(1.5 * float(atr_val), tick), tick, precision)
                stop_basis = "1.5x ATR above the retested structure level"
                invalidation = f"Candle close through stop {stop}, or an opposing confirmed structure break."
            elif active_obs and any(b.get("upper") is not None for b in active_obs):
                best_ob = active_obs[-1]
                ob_upper = float(best_ob["upper"])
                ob_lower = float(best_ob["lower"])
                if ob_lower - 0.5 * atr_val <= current_price <= ob_upper + 0.5 * atr_val:
                    setup_type = "ORDER_BLOCK_ENTRY"
                    entry = round_tick(max(current_price, ob_lower), tick, precision)
                    stop = round_tick(ob_upper + max(1.0 * float(atr_val), tick), tick, precision)
                    stop_basis = f"1.0x ATR above confirmed bearish order block ({ob_upper})"
                    trigger = f"Wait for test of bearish order block zone {ob_lower}–{ob_upper}."
                    invalidation = f"Candle close above order block stop {stop}."
            elif active_fvgs and any(f.get("upper") is not None for f in active_fvgs):
                best_fvg = active_fvgs[-1]
                fvg_upper = float(best_fvg["upper"])
                fvg_lower = float(best_fvg["lower"])
                if fvg_lower - 0.5 * atr_val <= current_price <= fvg_upper + 0.5 * atr_val:
                    setup_type = "FVG_ENTRY"
                    entry = round_tick(max(current_price, fvg_lower), tick, precision)
                    stop = round_tick(fvg_upper + max(1.0 * float(atr_val), tick), tick, precision)
                    stop_basis = f"1.0x ATR above bearish FVG upper boundary ({fvg_upper})"
                    trigger = f"Wait for fill/retest of bearish FVG zone {fvg_lower}–{fvg_upper}."
                    invalidation = f"Candle close above FVG stop {stop}."
            if setup_type is None and trend_aligned and ema20_val and current_price <= ema20_val + 0.5 * atr_val:
                setup_type = "PULLBACK_ENTRY"
                entry = round_tick(min(current_price, ema20_val), tick, precision)
                stop = round_tick(max(ema50_val + 0.2 * atr_val, entry + max(1.5 * float(atr_val), tick)), tick, precision)
                stop_basis = "above max(EMA50 + 0.2xATR, entry + 1.5xATR)"
                trigger = f"Trend continuation entry on pullback confirmation at {entry}; require bearish candle close below EMA20."
                invalidation = f"Candle close above stop-loss {stop}, or a confirmed bullish structure break."

    # 7. Structural take-profit ladder, geometry, and risk/reward validation
    tp1 = tp2 = tp3 = None
    rr = rr_tp1 = rr_tp2 = rr_tp3 = None
    risk_distance = abs(entry - stop) if (entry is not None and stop is not None) else 0.0
    if entry is not None and stop is not None and risk_distance > 0:
        planned = _plan_take_profits(side, entry, stop, structure, all_blocks, tick, precision, fvgs=all_fvgs)
        tp1 = planned[0] if len(planned) > 0 else None
        tp2 = planned[1] if len(planned) > 1 else None
        tp3 = planned[2] if len(planned) > 2 else None
        if tp1 is not None:
            rr_tp1 = round(abs(tp1 - entry) / risk_distance, 2)
        if tp2 is not None:
            rr_tp2 = round(abs(tp2 - entry) / risk_distance, 2)
        if tp3 is not None:
            rr_tp3 = round(abs(tp3 - entry) / risk_distance, 2)
        rr = rr_tp2 if rr_tp2 is not None else rr_tp1

    def _all_finite(*vals: float | None) -> bool:
        return all(v is not None and isinstance(v, (int, float)) and math.isfinite(v) for v in vals)

    valid_geometry = False
    if entry is not None and stop is not None and tp1 is not None and rr is not None:
        if side == "BUY":
            valid_geometry = stop < entry < tp1
            if tp2 is not None:
                valid_geometry = valid_geometry and tp1 < tp2
            if tp3 is not None:
                valid_geometry = valid_geometry and (tp2 is None or tp2 < tp3)
        else:
            valid_geometry = stop > entry > tp1
            if tp2 is not None:
                valid_geometry = valid_geometry and tp1 > tp2
            if tp3 is not None:
                valid_geometry = valid_geometry and (tp2 is None or tp2 > tp3)
        valid_geometry = valid_geometry and _all_finite(entry, stop, tp1, rr) and rr >= min_rr

    # 7b. Entry zone from the computed entry (tick- and ATR-aware buffer)
    entry_zone = None
    if entry is not None:
        zone_buffer = max(tick * 2, round_tick((atr_val or 0) * 0.1, tick, precision))
        entry_zone = {
            "lower": round(entry - zone_buffer, precision),
            "upper": round(entry + zone_buffer, precision),
        }

    # 8. Setup state classification
    # INVALIDATED              : the latest closed candle already failed the structural stop
    # POTENTIAL_BUY/SELL       : every condition satisfied plus valid geometry and R:R
    # WAITING_FOR_CONFIRMATION : price is at the valid zone but a required trigger is unmet
    # WATCHLIST                : a relevant area/early condition exists but rules are unmet
    # NO_SETUP                 : nothing qualifies in the available data
    late_entry = False
    late_entry_reason = None
    if entry is not None and entry_zone is not None and atr_val:
        buffer = max(tick * 3, atr_val * 0.25)
        if side == "BUY" and current_price > entry_zone["upper"] + buffer:
            late_entry = True
        if side == "SELL" and current_price < entry_zone["lower"] - buffer:
            late_entry = True
        if late_entry:
            late_entry_reason = (
                f"Price ({current_price}) has moved beyond the validated entry zone "
                f"({entry_zone['lower']}–{entry_zone['upper']}); the original entry trigger is stale and a "
                f"fresh confirmation is required before entering."
            )

    already_invalidated = False
    if valid_geometry and stop is not None:
        last_close = float(candles[-1]["close"])
        if side == "BUY" and last_close < stop:
            already_invalidated = True
        if side == "SELL" and last_close > stop:
            already_invalidated = True

    if already_invalidated:
        direction = "NO_SETUP"
        status = "INVALIDATED"
        setup_state = "INVALIDATED"
        stopped_at = stop
        explanation = (
            f"INVALIDATED: the latest closed candle ({float(candles[-1]['close'])}) is already beyond the "
            f"structural stop {stopped_at}; this {side} setup has failed its invalidation rule."
        )
        entry = stop = tp1 = tp2 = tp3 = rr = rr_tp1 = rr_tp2 = rr_tp3 = None
        entry_zone = None
        setup_type = None
        invalidation = f"Setup already invalidated by a close beyond {stopped_at}."
        trigger = f"No {side} entry. Wait for a fresh confirmed structure shift before re-evaluating."
    elif valid_geometry and len(missing) == 0:
        direction = side
        status = f"{side}_CANDIDATE"
        setup_state = "POTENTIAL_BUY" if side == "BUY" else "POTENTIAL_SELL"
        if late_entry and late_entry_reason:
            risks.append(late_entry_reason)
        explanation = (
            f"Valid {side} setup identified based on {setup_type.replace('_', ' ').lower()}: "
            f"trend alignment, confirmed market structure, healthy momentum, and defensible "
            f"risk/reward ({rr:.2f}:1 to TP2)."
        )
    elif trend_aligned or has_smc_structure:
        # A relevant trend/structure exists, but entry confirmation or R:R is missing.
        direction = "NO_SETUP"
        waiting_for_confirm = False
        if entry_zone is not None and missing:
            zone_lo = entry_zone["lower"] - tick * 3
            zone_hi = entry_zone["upper"] + tick * 3
            if zone_lo <= current_price <= zone_hi:
                waiting_for_confirm = True
        if waiting_for_confirm:
            # Levels are kept as the deterministic zone to watch, but this is not a signal.
            status = "WAITING_FOR_CONFIRMATION"
            setup_state = "WAITING_FOR_CONFIRMATION"
            explanation = (
                f"WAITING_FOR_CONFIRMATION: price is interacting with the valid {side} zone "
                f"({entry_zone['lower']}–{entry_zone['upper']}) but the entry trigger is not yet complete "
                f"(missing: {', '.join(missing) if missing else 'trigger confirmation'}). Do not treat this as a confirmed signal."
            )
        else:
            status = "WATCHLIST"
            setup_state = "WATCHLIST"
            explanation = (
                f"WATCHLIST: {side} setup is developing. Prevailing {market_bias.lower()} bias is present, "
                f"but entry conditions are not yet fully confirmed (missing: {', '.join(missing) if missing else 'trigger confirmation'}). "
                f"Monitor price action for a valid pullback or confirmation before entry."
            )
            entry = stop = tp1 = tp2 = tp3 = rr = rr_tp1 = rr_tp2 = rr_tp3 = None
            entry_zone = None
            setup_type = None
            trigger = (
                f"Watch for a confirmed {expected_dir} structure break or a healthy pullback into the "
                f"{round_tick(current_price * 0.998 if side == 'BUY' else current_price * 1.002, tick, precision)} zone."
            )
            invalidation = f"Invalidated if price closes against trend beyond EMA50 ({round_tick(ema50_val, tick, precision) if ema50_val else 'key support'})."
    else:
        direction = "NO_SETUP"
        status = "NO_SETUP"
        setup_state = "NO_SETUP"
        explanation = (
            f"NO_SETUP: Current market structure and indicators do not support a {side} setup. "
            f"Market bias is {market_bias} (missing: {', '.join(missing)}). "
            f"Entering a {side} trade here would be counter-trend or lack statistical edge."
        )
        entry = stop = tp1 = tp2 = tp3 = rr = rr_tp1 = rr_tp2 = rr_tp3 = None
        entry_zone = None
        setup_type = None
        trigger = f"No {side} action. Wait for structure shift (CHoCH) and EMA20/50 realignment before looking for entries."
        invalidation = "N/A - no active trade candidate."

    # 9. Position Sizing
    risk_amount = round(float(equity) * (max_risk_percent / 100.0), 2)
    position_sizing: dict[str, Any] | None = None
    if entry is not None and stop is not None:
        position_sizing = calculate_position_size(
            symbol=symbol,
            equity=equity,
            risk_percent=max_risk_percent,
            entry=entry,
            stop_loss=stop,
            user_unit_value=user_unit_value,
        )

    decision: str
    if status == f"{side}_CANDIDATE":
        decision = f"{side}_SETUP"
    elif status in ("WAITING_FOR_CONFIRMATION", "WATCHLIST"):
        decision = "WATCHLIST"
    elif status == "DATA_STALE":
        decision = "DATA_STALE"
    elif status == "DATA_UNAVAILABLE":
        decision = "INSUFFICIENT_DATA"
    else:
        decision = "NO_SETUP"

    return {
        "symbol": symbol,
        "instrument": symbol,
        "timeframe": timeframe,
        "decision": decision,
        "market_bias": market_bias,
        "data_status": data_status,
        "quote_price": quote_price if quote_price is not None else current_price,
        "quote_timestamp_utc": quote_ts,
        "quote_age_seconds": quote_age_seconds if quote_age_seconds is not None else freshness_seconds,
        "latest_candle_timestamp_utc": last_candle.get("timestamp"),
        "strategy": setup_type,
        "entry": entry,
        "entry_zone": entry_zone,
        "stop_loss": stop,
        "take_profits": [t for t in (tp1, tp2, tp3) if t is not None],
        "risk_reward": [
            {"target": f"TP{i+1}", "price": t, "rr": r_val}
            for i, (t, r_val) in enumerate([(tp1, rr_tp1), (tp2, rr_tp2), (tp3, rr_tp3)])
            if t is not None and r_val is not None
        ],
        "structure": structure,
        "liquidity": {
            "buy_side_liquidity": structure.get("buy_side_liquidity", []) if structure else [],
            "sell_side_liquidity": structure.get("sell_side_liquidity", []) if structure else [],
            "liquidity_sweeps": structure.get("liquidity_sweeps", []) if structure else [],
            "equal_highs": structure.get("equal_highs", []) if structure else [],
            "equal_lows": structure.get("equal_lows", []) if structure else [],
        },
        "fvgs": active_fvgs,
        "order_blocks": active_obs,
        "indicators": indicators_dict,
        "supporting_conditions": [f"{k}: {conditions_detail.get(k, 'ok')}" for k in satisfied],
        "conflicting_conditions": [f"{k}: {conditions_detail.get(k, 'unmet')}" for k in missing],
        "invalidation_conditions": [invalidation] if invalidation else [],
        "warnings": warnings,
        # Legacy fields
        "market_data_status": data_status,
        "source": provider,
        "analysis_timestamp": now.isoformat(),
        "evaluation_timestamp": now.isoformat(),
        "candle_timestamp": last_candle["timestamp"],
        "direction": direction,
        "setup_type": setup_type,
        "current_price": current_price,
        "data_freshness_seconds": freshness_seconds,
        "entry_price": entry,
        "entry_trigger": trigger,
        "entry_type": setup_type,
        "entry_trigger_conditions": [trigger] if trigger else [],
        "trigger_candle_timestamp": last_candle["timestamp"] if setup_type else None,
        "confirmation_complete": bool(direction in ("BUY", "SELL") and valid_geometry and len(missing) == 0),
        "stop_basis": stop_basis,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "take_profit_3": tp3,
        "take_profit_targets": [t for t in (tp1, tp2, tp3) if t is not None],
        "risk_reward_ratio": rr,
        "risk_reward_tp1": rr_tp1,
        "risk_reward_tp2": rr_tp2,
        "risk_reward_tp3": rr_tp3,
        "max_risk_percent": max_risk_percent,
        "risk_amount": risk_amount if direction != "NO_SETUP" else None,
        "position_sizing_example": position_sizing,
        "supporting_indicators": indicators_dict,
        "supporting_smart_money_concepts": smc_summary,
        "invalidation_condition": invalidation,
        "invalidation_boundary": (
            {"level": stop, "basis": stop_basis}
            if stop is not None and valid_geometry
            else None
        ),
        "liquidity_sweeps": structure.get("liquidity_sweeps", []) if structure else [],
        "reasons": satisfied,
        "risks": risks,
        "status": status,
        "setup_state": setup_state,
        "state": setup_state,
        "conditions_satisfied": [f"{k}: {conditions_detail.get(k, 'ok')}" for k in satisfied],
        "conditions_not_satisfied": [f"{k}: {conditions_detail.get(k, 'unmet')}" for k in missing],
        "explanation": explanation,
        "side": side,
        "take_profit": tp2,
        "late_entry": late_entry,
        "late_entry_reason": late_entry_reason,
        "already_invalidated": already_invalidated,
        "current_price_vs_entry": {
            "current_price": current_price,
            "entry_price": entry,
            "distance": round(entry - current_price, precision + 2) if entry is not None else None,
        },
        "provider": provider,
    }


def _make_empty_response(
    symbol: str,
    timeframe: str,
    side: Literal["BUY", "SELL"],
    provider: str,
    data_status: str,
    status: str,
    explanation: str,
    missing: list[str],
    max_risk: float,
    warnings: list[str],
    current_price: float | None = None,
    indicators: dict[str, Any] | None = None,
    smc: list[str] | None = None,
    market_bias: str = "NEUTRAL",
    freshness_seconds: int | None = None,
    quote_price: float | None = None,
    quote_ts: str | None = None,
    quote_age_seconds: float | None = None,
    candle_ts: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    decision = (
        "INSUFFICIENT_DATA"
        if status in ("DATA_UNAVAILABLE", "INSUFFICIENT_DATA")
        else "DATA_STALE"
        if status in ("DATA_STALE", "STALE_DATA")
        else "NO_SETUP"
    )
    return {
        "symbol": symbol,
        "instrument": symbol,
        "timeframe": timeframe,
        "decision": decision,
        "market_bias": market_bias,
        "data_status": data_status,
        "quote_price": quote_price if quote_price is not None else current_price,
        "quote_timestamp_utc": quote_ts,
        "quote_age_seconds": quote_age_seconds if quote_age_seconds is not None else freshness_seconds,
        "latest_candle_timestamp_utc": candle_ts,
        "strategy": None,
        "entry": None,
        "entry_zone": None,
        "stop_loss": None,
        "take_profits": [],
        "risk_reward": [],
        "structure": {},
        "liquidity": {
            "buy_side_liquidity": [],
            "sell_side_liquidity": [],
            "liquidity_sweeps": [],
            "equal_highs": [],
            "equal_lows": [],
        },
        "fvgs": [],
        "order_blocks": [],
        "indicators": indicators or {"ema20": None, "ema50": None, "rsi14": None, "atr14": None, "macd": None, "candle_timestamp": None},
        "supporting_conditions": [],
        "conflicting_conditions": missing,
        "invalidation_conditions": [],
        "warnings": warnings,
        # Legacy fields
        "market_data_status": data_status,
        "source": provider,
        "analysis_timestamp": now.isoformat(),
        "evaluation_timestamp": now.isoformat(),
        "candle_timestamp": candle_ts,
        "direction": "NO_SETUP",
        "setup_type": None,
        "current_price": current_price,
        "data_freshness_seconds": freshness_seconds,
        "entry_price": None,
        "entry_trigger": None,
        "entry_type": None,
        "entry_trigger_conditions": [],
        "trigger_candle_timestamp": None,
        "confirmation_complete": False,
        "stop_basis": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "take_profit_3": None,
        "take_profit_targets": [],
        "risk_reward_ratio": None,
        "risk_reward_tp1": None,
        "risk_reward_tp2": None,
        "risk_reward_tp3": None,
        "max_risk_percent": max_risk,
        "risk_amount": None,
        "position_sizing_example": None,
        "supporting_indicators": indicators or {"ema20": None, "ema50": None, "rsi14": None, "atr14": None, "macd": None, "candle_timestamp": None},
        "supporting_smart_money_concepts": smc or [],
        "invalidation_condition": None,
        "invalidation_boundary": None,
        "liquidity_sweeps": [],
        "reasons": [],
        "risks": warnings,
        "status": status,
        "setup_state": status,
        "state": status,
        "conditions_satisfied": [],
        "conditions_not_satisfied": missing,
        "explanation": explanation,
        "ai_review": None,
        "side": side,
        "take_profit": None,
        "late_entry": False,
        "late_entry_reason": None,
        "already_invalidated": False,
        "current_price_vs_entry": {
            "current_price": current_price,
            "entry_price": None,
            "distance": None,
        },
        "provider": provider,
    }
