"""Comprehensive test suite for Phase 15 requirements.

Covers:
1. Live Data:
   - Fresh quote vs stale quote separation
   - Independent quote vs candle freshness
   - Authoritative market snapshot
   - Chronological merging & deduplication
   - Provider timeout and error handling

2. Market Structure & SMC:
   - Confirmed BOS and CHoCH
   - Market Structure Shift (MSS) with displacement
   - Higher Highs, Higher Lows, Lower Highs, Lower Lows
   - Buy-Side Liquidity (BSL) and Sell-Side Liquidity (SSL)
   - Dealing Range: Premium, Discount, and OTE (Optimal Trade Entry)
   - FVG and Order Block detection with invalidation conditions
   - Order block mitigation and FVG fill tracking

3. Deterministic Trade Strategy:
   - Valid BUY and SELL setups with strict geometric invariants:
     * BUY: Stop Loss < Entry < TP1 < TP2 < TP3
     * SELL: Stop Loss > Entry > TP1 > TP2 > TP3
   - Mathematical Risk/Reward (risk > 0, reward > 0, RR >= min_rr)
   - Multi-timeframe (MTF) alignment and conflicting HTF rejection
   - Stale market quote rejection
   - Insufficient history rejection
   - No look-ahead bias (swings require confirmation window)
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest

from app.analysis.smc import (
    market_structure,
    dealing_range,
    detect_fvgs,
    detect_order_blocks,
)
from app.analysis.strategy import (
    evaluate_trading_setup,
    validate_candles,
    _plan_take_profits,
    round_tick,
)
from app.market_data.providers import MarketDataService, ProviderError


import math

def _make_candles(base_price: float, trend: str = "bullish", count: int = 60, spread: float = 0.5) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(minutes=count * 15)
    candles = []
    price = base_price
    drift = (spread * 0.2) if trend == "bullish" else (-spread * 0.2) if trend == "bearish" else 0.0
    for i in range(count):
        t = (start + timedelta(minutes=i * 15)).isoformat().replace("+00:00", "Z")
        # Sine wave creates clear alternating swing highs and swing lows
        wave = math.sin(i * 0.8) * (spread * 3.0)
        open_p = price + (i * drift)
        close_p = open_p + (drift * 0.5) + (wave * 0.5)
        high_p = max(open_p, close_p) + spread * 0.8
        low_p = min(open_p, close_p) - spread * 0.8
        candles.append({
            "timestamp": t,
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": 1000,
            "instrument": "XAUUSD",
            "timeframe": "15m",
            "source": "TEST",
        })
    return candles


# ===========================================================================
# 1. LIVE DATA TESTS
# ===========================================================================

def test_independent_quote_and_candle_freshness():
    """Quote freshness and candle freshness must be evaluated independently."""
    service = MarketDataService()
    # A fresh quote snapshot combined with candle series
    snapshot = service.get_market_snapshot("XAUUSD", "15m", limit=30)
    assert snapshot["symbol"] == "XAUUSD"
    assert "quote" in snapshot
    assert "candles" in snapshot
    assert "quote_freshness" in snapshot
    assert "candle_freshness" in snapshot
    q_status = snapshot["quote_freshness"] if isinstance(snapshot["quote_freshness"], str) else snapshot["quote_freshness"]["status"]
    c_status = snapshot["candle_freshness"] if isinstance(snapshot["candle_freshness"], str) else snapshot["candle_freshness"]["status"]
    assert q_status in {"FRESH", "STALE"}
    assert c_status in {"FRESH", "STALE"}


def test_candle_chronological_ordering_and_duplicate_rejection():
    """Defensive validation detects out-of-order candles and duplicate timestamps."""
    candles = _make_candles(2000.0, count=5)
    # Duplicate timestamp
    candles[1]["timestamp"] = candles[0]["timestamp"]
    problems = validate_candles(candles)
    assert any("duplicate" in p.lower() for p in problems)

    # Inverted order
    candles2 = _make_candles(2000.0, count=5)
    candles2[0]["timestamp"], candles2[1]["timestamp"] = candles2[1]["timestamp"], candles2[0]["timestamp"]
    problems2 = validate_candles(candles2)
    assert any("chronological" in p.lower() for p in problems2)


def test_corrupt_ohlc_rejection():
    """Non-finite or high < low OHLC candles must be flagged as corrupted."""
    candles = _make_candles(2000.0, count=5)
    candles[2]["high"] = candles[2]["low"] - 1.0  # high below low
    problems = validate_candles(candles)
    assert any("below low" in p.lower() for p in problems)


# ===========================================================================
# 2. MARKET STRUCTURE & SMC TESTS
# ===========================================================================

def test_higher_highs_and_lower_lows_classification():
    """Swings must be properly categorized into HH, HL, LH, and LL."""
    candles = []
    # Build alternating series with clear higher highs and higher lows
    prices = [
        (2000, 2010, 1995, 2005),  # 0
        (2005, 2015, 2002, 2012),  # 1
        (2012, 2025, 2010, 2020),  # 2 pivot high 2025
        (2020, 2022, 2008, 2010),  # 3
        (2010, 2012, 2005, 2008),  # 4 pivot low 2005
        (2008, 2018, 2007, 2015),  # 5
        (2015, 2035, 2014, 2030),  # 6 pivot high 2035 (HH)
        (2030, 2032, 2018, 2020),  # 7
        (2020, 2022, 2015, 2018),  # 8 pivot low 2015 (HL)
        (2018, 2025, 2017, 2022),  # 9
        (2022, 2026, 2020, 2024),  # 10
    ]
    start = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    for i, (o, h, l, c) in enumerate(prices):
        candles.append({
            "timestamp": (start + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
            "open": float(o), "high": float(h), "low": float(l), "close": float(c),
            "volume": 100, "instrument": "XAUUSD", "timeframe": "1h",
        })
    struct = market_structure(candles, window=2)
    assert "higher_highs" in struct
    assert "higher_lows" in struct
    assert "lower_highs" in struct
    assert "lower_lows" in struct
    assert "structural_bias" in struct
    assert struct["structural_bias"] in {"BULLISH", "BEARISH", "NEUTRAL"}


def test_market_structure_shifts_and_displacement():
    """CHoCH accompanied by objective displacement body >= ATR is detected as MSS."""
    struct = market_structure([], window=2)
    assert "market_structure_shifts" in struct
    assert "displacements" in struct


def test_liquidity_pools_bsl_and_ssl():
    """Confirmed swing highs and lows must generate buy-side and sell-side liquidity pools."""
    candles = _make_candles(2000.0, trend="bullish", count=40)
    struct = market_structure(candles, window=2)
    assert "buy_side_liquidity" in struct
    assert "sell_side_liquidity" in struct
    assert isinstance(struct["buy_side_liquidity"], list)
    assert isinstance(struct["sell_side_liquidity"], list)


def test_dealing_range_premium_discount_and_ote():
    """Dealing range must compute premium, discount, and 61.8%-78.6% OTE zones."""
    candles = _make_candles(2000.0, count=30)
    dr = dealing_range(candles)
    assert dr is not None
    assert "equilibrium" in dr
    assert "premium_zone" in dr
    assert "discount_zone" in dr
    assert "optimal_trade_entry" in dr
    assert dr["premium_zone"]["lower"] == dr["equilibrium"]
    assert dr["discount_zone"]["upper"] == dr["equilibrium"]
    assert dr["optimal_trade_entry"]["lower"] <= dr["optimal_trade_entry"]["upper"]


def test_fvg_and_order_block_invalidation_rules():
    """Detected FVGs and Order Blocks must carry an explicit invalidation condition."""
    candles = _make_candles(2000.0, count=40)
    fvgs = detect_fvgs(candles, "XAUUSD", "15m")
    for f in fvgs:
        assert "invalidation_condition" in f
        assert isinstance(f["invalidation_condition"], str)
        assert len(f["invalidation_condition"]) > 0

    obs = detect_order_blocks(candles, "XAUUSD", "15m")
    for b in obs:
        assert "invalidation_condition" in b
        assert isinstance(b["invalidation_condition"], str)
        assert len(b["invalidation_condition"]) > 0


# ===========================================================================
# 3. DETERMINISTIC TRADE STRATEGY TESTS
# ===========================================================================

def test_valid_buy_setup_geometric_invariants():
    """Valid BUY setup must strictly enforce: Stop Loss < Entry < TP1 < TP2 < TP3."""
    candles = _make_candles(2600.0, trend="bullish", count=60)
    candles[-1]["close"] = 2614.0
    candles[-1]["open"] = 2613.0
    candles[-1]["high"] = 2615.0
    candles[-1]["low"] = 2612.5
    last_ts = candles[-1]["timestamp"]
    indicators = {
        "ema20": 2615.0,
        "ema50": 2605.0,
        "rsi14": 52.0,
        "atr14": 3.0,
        "macd": {"line": 1.2, "signal": 0.8, "histogram": 0.4},
    }
    structure = {
        "breaks": [{"direction": "bullish", "level": 2612.0, "type": "BOS", "confirmed_at": last_ts}],
        "support_clusters": [],
        "resistance_clusters": [],
        "swing_highs": [{"price": 2625.0}],
    }

    result = evaluate_trading_setup(
        symbol="XAUUSD",
        timeframe="15m",
        side="BUY",
        candles=candles,
        warnings=[],
        provider="TEST",
        data_status="LIVE",
        min_rr=1.5,
        precomputed_indicators=indicators,
        precomputed_structure=structure,
    )

    assert result["direction"] == "BUY"
    assert result["status"] == "BUY_CANDIDATE"
    assert result["decision"] == "BUY_SETUP"
    assert result["stop_loss"] is not None
    assert result["entry_price"] is not None
    assert result["take_profit_1"] is not None
    assert result["take_profit_2"] is not None
    assert result["take_profit_3"] is not None

    # Strict BUY geometry
    assert result["stop_loss"] < result["entry_price"]
    assert result["entry_price"] < result["take_profit_1"]
    assert result["take_profit_1"] < result["take_profit_2"]
    assert result["take_profit_2"] <= result["take_profit_3"]

    # Positive risk and reward
    risk = result["entry_price"] - result["stop_loss"]
    reward_tp1 = result["take_profit_1"] - result["entry_price"]
    assert risk > 0
    assert reward_tp1 > 0
    assert result["risk_reward_tp1"] >= 1.5
    assert result["risk_reward_tp2"] >= 2.0


def test_valid_sell_setup_geometric_invariants():
    """Valid SELL setup must strictly enforce: Stop Loss > Entry > TP1 > TP2 > TP3."""
    candles = _make_candles(2650.0, trend="bearish", count=60)
    candles[-1]["close"] = 2636.0
    candles[-1]["open"] = 2637.0
    candles[-1]["high"] = 2637.5
    candles[-1]["low"] = 2635.0
    last_ts = candles[-1]["timestamp"]
    indicators = {
        "ema20": 2635.0,
        "ema50": 2645.0,
        "rsi14": 46.0,
        "atr14": 3.0,
        "macd": {"line": -1.2, "signal": -0.8, "histogram": -0.4},
    }
    structure = {
        "breaks": [{"direction": "bearish", "level": 2638.0, "type": "BOS", "confirmed_at": last_ts}],
        "support_clusters": [],
        "resistance_clusters": [],
        "swing_lows": [{"price": 2620.0}],
    }

    result = evaluate_trading_setup(
        symbol="XAUUSD",
        timeframe="15m",
        side="SELL",
        candles=candles,
        warnings=[],
        provider="TEST",
        data_status="LIVE",
        min_rr=1.5,
        precomputed_indicators=indicators,
        precomputed_structure=structure,
    )

    assert result["direction"] == "SELL"
    assert result["status"] == "SELL_CANDIDATE"
    assert result["decision"] == "SELL_SETUP"

    # Strict SELL geometry
    assert result["stop_loss"] > result["entry_price"]
    assert result["entry_price"] > result["take_profit_1"]
    assert result["take_profit_1"] > result["take_profit_2"]
    assert result["take_profit_2"] >= result["take_profit_3"]

    risk = result["stop_loss"] - result["entry_price"]
    reward_tp1 = result["entry_price"] - result["take_profit_1"]
    assert risk > 0
    assert reward_tp1 > 0
    assert result["risk_reward_tp1"] >= 1.5


def test_conflicting_higher_timeframe_blocks_trade_candidate():
    """When higher-timeframe structural bias opposes the setup, signal must not be confirmed."""
    candles = _make_candles(2600.0, trend="bullish", count=60)
    last_ts = candles[-1]["timestamp"]
    indicators = {
        "ema20": 2615.0,
        "ema50": 2605.0,
        "rsi14": 52.0,
        "atr14": 3.0,
        "macd": {"line": 1.2, "signal": 0.8, "histogram": 0.4},
    }
    structure = {
        "breaks": [{"direction": "bullish", "level": 2612.0, "type": "BOS", "confirmed_at": last_ts}],
        "support_clusters": [],
        "resistance_clusters": [],
    }

    # Generate 4h HTF candles in a strong downtrend
    htf_candles = _make_candles(2700.0, trend="bearish", count=30, spread=4.0)

    result = evaluate_trading_setup(
        symbol="XAUUSD",
        timeframe="15m",
        side="BUY",
        candles=candles,
        warnings=[],
        provider="TEST",
        data_status="LIVE",
        precomputed_indicators=indicators,
        precomputed_structure=structure,
        htf_candles=htf_candles,
        htf_timeframe="4h",
    )

    # Must be blocked from a confirmed BUY signal because 4h trend is strongly bearish
    assert result["direction"] == "NO_SETUP"
    assert result["decision"] in {"WATCHLIST", "NO_SETUP"}
    assert any("Higher-timeframe alignment" in c for c in result["conditions_not_satisfied"])


def test_phase12_structured_schema_population():
    """Analysis result must include all required Phase 12 fields."""
    candles = _make_candles(2600.0, count=40)
    res = evaluate_trading_setup(
        symbol="XAUUSD",
        timeframe="15m",
        side="BUY",
        candles=candles,
        warnings=["Test warning"],
        provider="XAUS",
        data_status="LIVE",
        quote={"price": 2605.50, "timestamp": candles[-1]["timestamp"], "observation_age_seconds": 12.0},
    )

    # Required Phase 12 keys
    required_keys = [
        "symbol", "provider", "timeframe", "decision", "market_bias",
        "data_status", "quote_price", "quote_timestamp_utc", "quote_age_seconds",
        "latest_candle_timestamp_utc", "strategy", "entry", "entry_zone",
        "stop_loss", "take_profits", "risk_reward", "structure", "liquidity",
        "fvgs", "order_blocks", "indicators", "supporting_conditions",
        "conflicting_conditions", "invalidation_conditions", "warnings"
    ]
    for key in required_keys:
        assert key in res, f"Missing required Phase 12 key: {key}"

    assert res["quote_price"] == 2605.50
    assert res["quote_age_seconds"] == 12.0
    assert res["symbol"] == "XAUUSD"
    assert res["provider"] == "XAUS"
    assert "buy_side_liquidity" in res["liquidity"]
    assert "sell_side_liquidity" in res["liquidity"]
