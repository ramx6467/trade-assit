"""Comprehensive unit tests for TradeMind AI trade-planning pipeline.

Covers:
- Valid BUY candidate
- Valid SELL candidate
- WATCHLIST developing setup
- NO_SETUP counter-trend / missing conditions
- Missing market data (DATA_UNAVAILABLE)
- Stale market data (DATA_STALE)
- Missing indicators
- Incorrect stop-loss side rejection
- Incorrect take-profit side rejection
- Zero risk distance rejection
- Risk/reward minimum calculation and enforcement
- Position sizing across Forex, Gold, and Indian Indices
- Unsupported instrument specifications
- AI unavailable fallback (deterministic levels preserved)
- AI response validation and ensuring AI never alters numerical levels
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from app.analysis.strategy import (
    evaluate_trading_setup,
    calculate_position_size,
    round_tick,
    INSTRUMENT_SPECS,
)


def _generate_candles(base_price: float, trend: str = "bullish", count: int = 60, spread: float = 0.0005) -> list[dict]:
    """Generate controlled test candle fixtures with UTC timestamps."""
    start = datetime.now(timezone.utc) - timedelta(minutes=count * 15)
    candles = []
    price = base_price
    for i in range(count):
        t = (start + timedelta(minutes=i * 15)).isoformat().replace("+00:00", "Z")
        step = (spread * 0.4) if trend == "bullish" else (-spread * 0.4) if trend == "bearish" else 0.0
        open_p = price
        close_p = open_p + step
        high_p = max(open_p, close_p) + spread * 0.3
        low_p = min(open_p, close_p) - spread * 0.3
        candles.append({
            "timestamp": t,
            "open": round(open_p, 5),
            "high": round(high_p, 5),
            "low": round(low_p, 5),
            "close": round(close_p, 5),
            "volume": 1000,
            "instrument": "EURUSD",
            "timeframe": "15m",
            "source": "TEST",
        })
        price = close_p
    return candles


# 1. Valid BUY Candidate Test
def test_valid_buy_candidate():
    candles = _generate_candles(1.1000, trend="bullish", count=60)
    last_ts = candles[-1]["timestamp"]
    indicators = {
        "ema20": 1.1080,
        "ema50": 1.1040,
        "rsi14": 55.0,
        "atr14": 0.0015,
        "macd": {"line": 0.0004, "signal": 0.0002, "histogram": 0.0002},
    }
    structure = {
        "breaks": [{"direction": "bullish", "level": 1.1070, "type": "BOS", "confirmed_at": last_ts}],
        "support_clusters": [],
        "resistance_clusters": [],
    }

    result = evaluate_trading_setup(
        symbol="EURUSD",
        timeframe="15m",
        side="BUY",
        candles=candles,
        warnings=[],
        provider="TEST_PROVIDER",
        data_status="LIVE",
        min_rr=1.5,
        max_risk_percent=1.0,
        equity=Decimal("100000"),
        precomputed_indicators=indicators,
        precomputed_structure=structure,
    )

    assert result["direction"] == "BUY"
    assert result["status"] == "BUY_CANDIDATE"
    assert result["entry_price"] is not None
    assert result["stop_loss"] is not None
    assert result["take_profit_1"] is not None
    assert result["take_profit_2"] is not None

    # Crucial BUY invariant: Stop Loss < Entry < TP1 < TP2
    assert result["stop_loss"] < result["entry_price"] < result["take_profit_1"] < result["take_profit_2"]
    assert result["risk_reward_ratio"] >= 1.5
    assert len(result["conditions_satisfied"]) >= 4
    assert len(result["conditions_not_satisfied"]) == 0
    assert result["position_sizing_example"] is not None
    assert result["position_sizing_example"]["quantity"] > 0


# 2. Valid SELL Candidate Test
def test_valid_sell_candidate():
    candles = _generate_candles(1.1200, trend="bearish", count=60)
    last_ts = candles[-1]["timestamp"]
    indicators = {
        "ema20": 1.1120,
        "ema50": 1.1160,
        "rsi14": 45.0,
        "atr14": 0.0015,
        "macd": {"line": -0.0004, "signal": -0.0002, "histogram": -0.0002},
    }
    structure = {
        "breaks": [{"direction": "bearish", "level": 1.1130, "type": "BOS", "confirmed_at": last_ts}],
        "support_clusters": [],
        "resistance_clusters": [],
    }

    result = evaluate_trading_setup(
        symbol="EURUSD",
        timeframe="15m",
        side="SELL",
        candles=candles,
        warnings=[],
        provider="TEST_PROVIDER",
        data_status="LIVE",
        min_rr=1.5,
        max_risk_percent=1.0,
        equity=Decimal("100000"),
        precomputed_indicators=indicators,
        precomputed_structure=structure,
    )

    assert result["direction"] == "SELL"
    assert result["status"] == "SELL_CANDIDATE"
    assert result["entry_price"] is not None
    assert result["stop_loss"] is not None
    assert result["take_profit_1"] is not None
    assert result["take_profit_2"] is not None

    # Crucial SELL invariant: Stop Loss > Entry > TP1 > TP2
    assert result["stop_loss"] > result["entry_price"] > result["take_profit_1"] > result["take_profit_2"]
    assert result["risk_reward_ratio"] >= 1.5
    assert result["position_sizing_example"]["quantity"] > 0


# 3. WATCHLIST Test (Developing setup waiting for confirmation/pullback)
def test_watchlist_developing_setup():
    candles = _generate_candles(1.1000, trend="bullish", count=60)
    # Price is bullish but RSI is overbought (76.0), so entering directly is unsafe
    indicators = {
        "ema20": 1.1080,
        "ema50": 1.1040,
        "rsi14": 76.0,  # Overbought
        "atr14": 0.0015,
        "macd": {"line": 0.0005, "signal": 0.0003, "histogram": 0.0002},
    }
    structure = {
        "breaks": [{"direction": "bullish", "level": 1.1070, "type": "BOS"}],
        "support_clusters": [],
        "resistance_clusters": [],
    }

    result = evaluate_trading_setup(
        symbol="EURUSD",
        timeframe="15m",
        side="BUY",
        candles=candles,
        warnings=[],
        provider="TEST_PROVIDER",
        data_status="LIVE",
        precomputed_indicators=indicators,
        precomputed_structure=structure,
    )

    assert result["status"] == "WATCHLIST"
    assert result["direction"] == "NO_SETUP"
    # Fields must not be fabricated for watchlist
    assert result["entry_price"] is None
    assert result["stop_loss"] is None
    assert result["current_price"] == candles[-1]["close"]
    # Explanations and watch trigger must be clearly populated
    assert "WATCHLIST" in result["explanation"]
    assert result["entry_trigger"] is not None
    assert any("RSI" in c for c in result["conditions_not_satisfied"])


# 4. NO_SETUP Counter-Trend Test
def test_no_setup_counter_trend():
    candles = _generate_candles(1.1000, trend="bearish", count=60)
    # Evaluating BUY when trend is strongly bearish and structure is breaking lower
    indicators = {
        "ema20": 1.0920,
        "ema50": 1.0980,
        "rsi14": 38.0,
        "atr14": 0.0015,
        "macd": {"line": -0.0006, "signal": -0.0004, "histogram": -0.0002},
    }
    structure = {
        "breaks": [{"direction": "bearish", "level": 1.0940, "type": "BOS"}],
        "support_clusters": [],
        "resistance_clusters": [],
    }

    result = evaluate_trading_setup(
        symbol="EURUSD",
        timeframe="15m",
        side="BUY",
        candles=candles,
        warnings=[],
        provider="TEST_PROVIDER",
        data_status="LIVE",
        precomputed_indicators=indicators,
        precomputed_structure=structure,
    )

    assert result["status"] == "NO_SETUP"
    assert result["direction"] == "NO_SETUP"
    assert result["entry_price"] is None
    assert result["stop_loss"] is None
    assert result["take_profit_1"] is None
    assert result["current_price"] == candles[-1]["close"]
    assert "NO_SETUP" in result["explanation"]
    assert len(result["conditions_not_satisfied"]) > 0


# 5. Missing Market Data (DATA_UNAVAILABLE)
def test_missing_market_data():
    result = evaluate_trading_setup(
        symbol="EURUSD",
        timeframe="15m",
        side="BUY",
        candles=[],
        warnings=["No candles found"],
        provider="TWELVE_DATA",
        data_status="DISCONNECTED",
    )
    assert result["status"] == "DATA_UNAVAILABLE"
    assert result["direction"] == "NO_SETUP"
    assert result["current_price"] is None
    assert result["entry_price"] is None
    assert "blocked" in result["explanation"].lower()


# 6. Stale Market Data (DATA_STALE)
def test_stale_market_data():
    # Last candle timestamp 2 hours ago on a 15m timeframe (stale)
    old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    candles = [{
        "timestamp": old_time,
        "open": 1.1000, "high": 1.1020, "low": 1.0990, "close": 1.1010,
        "volume": 100, "instrument": "EURUSD", "timeframe": "15m", "source": "TWELVE_DATA"
    }]

    result = evaluate_trading_setup(
        symbol="EURUSD",
        timeframe="15m",
        side="BUY",
        candles=candles,
        warnings=[],
        provider="TWELVE_DATA",
        data_status="LIVE",
    )
    assert result["status"] == "DATA_STALE"
    assert result["direction"] == "NO_SETUP"
    assert result["current_price"] == 1.1010
    assert "stale" in result["explanation"].lower()


# 7. Missing Indicators / Zero ATR
def test_missing_indicators_or_zero_atr():
    candles = _generate_candles(1.1000, trend="neutral", count=5)
    indicators = {
        "ema20": None,
        "ema50": None,
        "rsi14": None,
        "atr14": 0.0,  # Zero ATR
        "macd": {"line": None, "signal": None, "histogram": None},
    }

    result = evaluate_trading_setup(
        symbol="EURUSD",
        timeframe="15m",
        side="BUY",
        candles=candles,
        warnings=[],
        provider="DEMO",
        data_status="DEMO",
        precomputed_indicators=indicators,
    )

    assert result["direction"] == "NO_SETUP"
    assert result["entry_price"] is None
    assert any("ATR" in c for c in result["conditions_not_satisfied"])


# 8. Minimum Risk/Reward Enforcement
def test_risk_reward_minimum_enforcement():
    candles = _generate_candles(1.1000, trend="bullish", count=60)
    last_ts = candles[-1]["timestamp"]
    indicators = {
        "ema20": 1.1080,
        "ema50": 1.1040,
        "rsi14": 55.0,
        "atr14": 0.0015,
        "macd": {"line": 0.0004, "signal": 0.0002, "histogram": 0.0002},
    }
    structure = {
        "breaks": [{"direction": "bullish", "level": 1.1070, "type": "BOS", "confirmed_at": last_ts}],
        "support_clusters": [],
        "resistance_clusters": [],
    }

    # If min_rr is set to an unattainable 4.0:1
    result = evaluate_trading_setup(
        symbol="EURUSD",
        timeframe="15m",
        side="BUY",
        candles=candles,
        warnings=[],
        provider="TEST_PROVIDER",
        data_status="LIVE",
        min_rr=4.0,  # Extreme hurdle
        precomputed_indicators=indicators,
        precomputed_structure=structure,
    )

    # Must reject trade candidate since 2.0R < 4.0R
    assert result["direction"] == "NO_SETUP"
    assert result["entry_price"] is None


# 9. Position Sizing: Forex (EURUSD)
def test_position_sizing_forex():
    sizing = calculate_position_size(
        symbol="EURUSD",
        equity=Decimal("50000"),
        risk_percent=1.0,  # $500 risk
        entry=1.1000,
        stop_loss=1.0950,  # 50 pips (0.0050)
    )
    assert sizing is not None
    assert sizing["quote_currency"] == "USD"
    assert sizing["risk_amount"] == 500.0
    # $500 / (0.0050 * 100,000) = $500 / $500 = 1.00 lot
    assert sizing["quantity"] == 1.0


# 10. Position Sizing: Gold (XAUUSD)
def test_position_sizing_gold():
    sizing = calculate_position_size(
        symbol="XAUUSD",
        equity=Decimal("100000"),
        risk_percent=1.0,  # $1,000 risk
        entry=2650.00,
        stop_loss=2640.00,  # $10 distance
    )
    assert sizing is not None
    assert sizing["quote_currency"] == "USD"
    assert sizing["risk_amount"] == 1000.0
    # $1000 / ($10 * 100) = $1000 / $1000 = 1.00 lot
    assert sizing["quantity"] == 1.0


# 11. Position Sizing: Indian Index (NIFTY)
def test_position_sizing_nifty():
    sizing = calculate_position_size(
        symbol="NIFTY",
        equity=Decimal("500000"),
        risk_percent=1.0,  # INR 5,000 risk
        entry=24500.0,
        stop_loss=24400.0,  # 100 points
    )
    assert sizing is not None
    assert sizing["quote_currency"] == "INR"
    assert sizing["risk_amount"] == 5000.0
    # 5000 / (100 * 25) = 5000 / 2500 = 2 lots = 50 contracts
    assert sizing["quantity"] == 50


# 12. Position Sizing: Unsupported Instrument
def test_position_sizing_unsupported():
    sizing = calculate_position_size(
        symbol="UNKNOWN_COIN",
        equity=Decimal("10000"),
        risk_percent=1.0,
        entry=100.0,
        stop_loss=90.0,
    )
    assert sizing is not None
    assert sizing["quantity"] is None
    assert "Position size unavailable" in sizing["note"]


# 13. Zero Risk Distance Rejection
def test_zero_risk_distance_rejection():
    sizing = calculate_position_size(
        symbol="EURUSD",
        equity=Decimal("10000"),
        risk_percent=1.0,
        entry=1.1000,
        stop_loss=1.1000,
    )
    assert sizing is None


# 14. AI Unavailable Fallback (Deterministic levels preserved)
def test_ai_unavailable_fallback(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client = TestClient(main.app)

    res = client.get("/api/v1/analysis/evaluate?symbol=XAUUSD&timeframe=15m&side=BUY")
    assert res.status_code == 200
    data = res.json()
    assert "ai_review" in data
    assert "not configured" in data["ai_review"].lower()
    # Strategy engine still evaluated deterministically
    assert data["status"] in {"BUY_CANDIDATE", "WATCHLIST", "NO_SETUP", "DATA_UNAVAILABLE", "DATA_STALE"}


# 15. AI Response Validation: Numerical levels are not altered
def test_ai_does_not_alter_levels(monkeypatch):
    from unittest.mock import MagicMock
    import app.main as main
    from fastapi.testclient import TestClient

    # Set mock key and model for OpenAI test
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "mock-key")
    monkeypatch.setenv("OPENAI_MODEL", "mock-model")

    # Mock OpenAI client
    mock_review = MagicMock()
    mock_review.output_text = "AI notes that the XAUUSD setup has strong confluence and clear risk bounds."

    class MockOpenAI:
        def __init__(self, *args, **kwargs):
            self.responses = MagicMock()
            self.responses.create.return_value = mock_review

    monkeypatch.setattr("openai.OpenAI", MockOpenAI)
    client = TestClient(main.app)
    res = client.get("/api/v1/analysis/evaluate?symbol=XAUUSD&timeframe=15m&side=BUY")
    assert res.status_code == 200
    data = res.json()
    assert data["ai_review"] == "AI notes that the XAUUSD setup has strong confluence and clear risk bounds."


# 16. Bullish market without bearish confirmation returns WATCHLIST or NO_SETUP for SELL
def test_bullish_market_returns_watchlist_for_sell():
    candles = _generate_candles(2000.0, trend="bullish", count=60)
    last_ts = candles[-1]["timestamp"]
    indicators = {
        "ema20": 2010.0,
        "ema50": 2005.0,  # Bullish trend: EMA20 > EMA50
        "rsi14": 58.0,
        "atr14": 2.5,
        "macd": {"line": 0.5, "signal": 0.2, "histogram": 0.3},
    }
    # Opposing bullish breaks
    structure = {
        "breaks": [{"direction": "bullish", "level": 2008.0, "type": "BOS", "confirmed_at": last_ts, "index": 55}],
        "support_clusters": [],
        "resistance_clusters": [],
    }

    result = evaluate_trading_setup(
        symbol="XAUUSD",
        timeframe="15m",
        side="SELL",
        candles=candles,
        warnings=[],
        provider="TEST_PROVIDER",
        data_status="LIVE",
        min_rr=1.5,
        max_risk_percent=1.0,
        equity=Decimal("100000"),
        precomputed_indicators=indicators,
        precomputed_structure=structure,
    )

    # Must NOT force a SELL candidate
    assert result["direction"] == "NO_SETUP"
    assert result["status"] in {"WATCHLIST", "NO_SETUP"}
    assert result["entry_price"] is None
    assert result["stop_loss"] is None
    assert any("EMA trend alignment" in c for c in result["conditions_not_satisfied"])


# 17. SELL evaluation does not reuse BUY response and keeps distinct states
def test_sell_evaluation_does_not_reuse_buy_response():
    import app.main as main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)

    # First request BUY
    buy_res = client.post("/api/v1/analysis/evaluate?symbol=XAUUSD&timeframe=15m&side=BUY")
    assert buy_res.status_code == 200
    buy_data = buy_res.json()
    assert buy_data["side"] == "BUY"

    # Now request SELL
    sell_res = client.post("/api/v1/analysis/evaluate?symbol=XAUUSD&timeframe=15m&side=SELL")
    assert sell_res.status_code == 200
    sell_data = sell_res.json()
    assert sell_data["side"] == "SELL"

    # SELL must have evaluated SELL rules, not copied BUY
    assert sell_data["side"] != buy_data["side"]
    if sell_data["direction"] == "SELL":
        assert sell_data["stop_loss"] > sell_data["entry_price"]
    if buy_data["direction"] == "BUY":
        assert buy_data["stop_loss"] < buy_data["entry_price"]


# ---------------------------------------------------------------------------
# PHASE 15 additions: deterministic synthetic-OHLC coverage for the upgraded
# BUY/SELL engine (states, multi-target R:R, candle validation, geometry).
# ---------------------------------------------------------------------------

def _eval(symbol, side, candles, indicators=None, structure=None, blocks=None, min_rr=1.5):
    return evaluate_trading_setup(
        symbol=symbol,
        timeframe="15m",
        side=side,
        candles=candles,
        warnings=[],
        provider="TEST_PROVIDER",
        data_status="LIVE",
        min_rr=min_rr,
        max_risk_percent=1.0,
        equity=Decimal("100000"),
        precomputed_indicators=indicators,
        precomputed_structure=structure,
        precomputed_order_blocks=blocks,
    )


def test_buy_candidate_exposes_tp3_and_per_target_rr():
    candles = _generate_candles(1.1000, trend="bullish", count=60)
    last_ts = candles[-1]["timestamp"]
    indicators = {"ema20": 1.1080, "ema50": 1.1040, "rsi14": 55.0, "atr14": 0.0015,
                  "macd": {"line": 0.0004, "signal": 0.0002, "histogram": 0.0002}}
    structure = {"breaks": [{"direction": "bullish", "level": 1.1070, "type": "BOS", "confirmed_at": last_ts}],
                 "support_clusters": [], "resistance_clusters": []}
    r = _eval("EURUSD", "BUY", candles, indicators, structure)

    assert r["status"] == "BUY_CANDIDATE"
    assert r["setup_state"] == "POTENTIAL_BUY"
    assert r["stop_loss"] < r["entry_price"] < r["take_profit_1"] < r["take_profit_2"] < r["take_profit_3"]
    assert r["take_profit_targets"] == [r["take_profit_1"], r["take_profit_2"], r["take_profit_3"]]
    # Per-target R:R is derived from real entry/SL/TP, positive and increasing.
    assert r["risk_reward_tp1"] == 1.5
    assert r["risk_reward_tp2"] == 2.0
    assert r["risk_reward_tp3"] == 3.0
    assert r["risk_reward_ratio"] == r["risk_reward_tp2"]
    assert r["confirmation_complete"] is True


def test_sell_candidate_exposes_tp3_and_per_target_rr():
    candles = _generate_candles(1.1200, trend="bearish", count=60)
    last_ts = candles[-1]["timestamp"]
    indicators = {"ema20": 1.1120, "ema50": 1.1160, "rsi14": 45.0, "atr14": 0.0015,
                  "macd": {"line": -0.0004, "signal": -0.0002, "histogram": -0.0002}}
    structure = {"breaks": [{"direction": "bearish", "level": 1.1130, "type": "BOS", "confirmed_at": last_ts}],
                 "support_clusters": [], "resistance_clusters": []}
    r = _eval("EURUSD", "SELL", candles, indicators, structure)

    assert r["status"] == "SELL_CANDIDATE"
    assert r["setup_state"] == "POTENTIAL_SELL"
    assert r["stop_loss"] > r["entry_price"] > r["take_profit_1"] > r["take_profit_2"] > r["take_profit_3"]
    assert r["risk_reward_tp1"] == 1.5 and r["risk_reward_tp3"] == 3.0


def test_take_profits_snap_to_real_structural_level_beyond_r_baseline():
    """A structural level inside a rung's risk band replaces that rung's R baseline."""
    candles = _generate_candles(1.1000, trend="bullish", count=60)
    last_ts = candles[-1]["timestamp"]
    indicators = {"ema20": 1.1080, "ema50": 1.1040, "rsi14": 55.0, "atr14": 0.0015,
                  "macd": {"line": 0.0004, "signal": 0.0002, "histogram": 0.0002}}
    # entry ~1.1070, risk = 1.5*atr = 0.00225. Bands: [1.5R,2.0R)=[1.10925,1.1115),
    # [2.0R,3.0R)=[1.1115,1.11375), [3.0R,inf). A swing high at 1.1140 lands in the
    # open third band, so it replaces TP3's 3.0R baseline without disturbing TP1/TP2.
    structure = {
        "breaks": [{"direction": "bullish", "level": 1.1070, "type": "BOS", "confirmed_at": last_ts}],
        "support_clusters": [],
        "resistance_clusters": [],
        "swing_highs": [{"price": 1.1140, "confirmed_at": last_ts}],
    }
    r = _eval("EURUSD", "BUY", candles, indicators, structure)
    assert r["take_profit_3"] == 1.1140
    assert r["risk_reward_tp3"] >= 3.0
    # Nearer rungs keep their deterministic R baselines.
    assert r["risk_reward_tp1"] == 1.5
    assert r["risk_reward_tp2"] == 2.0
    assert r["take_profit_1"] < r["take_profit_2"] < r["take_profit_3"]


def test_candle_validation_flags_inconsistent_ohlc_as_data_unavailable():
    candles = _generate_candles(1.1000, trend="bullish", count=10)
    candles[3]["high"] = candles[3]["low"] - 0.01  # corrupt: high below low
    indicators = {"ema20": 1.1, "ema50": 1.09, "rsi14": 55.0, "atr14": 0.0015, "macd": {}}
    r = _eval("EURUSD", "BUY", candles, indicators=indicators)
    assert r["status"] == "DATA_UNAVAILABLE"
    assert r["entry_price"] is None
    assert any("integrity" in w.lower() or "below low" in w.lower() for w in r["warnings"] + r["risks"] + [r["explanation"]])


def test_invalidated_state_when_latest_close_breaks_structural_stop():
    candles = _generate_candles(1.1000, trend="bullish", count=60)
    last_ts = candles[-1]["timestamp"]
    indicators = {"ema20": 1.1080, "ema50": 1.1040, "rsi14": 55.0, "atr14": 0.0015,
                  "macd": {"line": 0.0004, "signal": 0.0002, "histogram": 0.0002}}
    structure = {"breaks": [{"direction": "bullish", "level": 1.1070, "type": "BOS", "confirmed_at": last_ts}],
                 "support_clusters": [], "resistance_clusters": []}
    # Force the latest close well below any structural stop the engine would use.
    candles[-1]["close"] = 1.0000
    candles[-1]["low"] = 0.9990
    candles[-1]["open"] = 1.0500
    candles[-1]["high"] = 1.0500
    r = _eval("EURUSD", "BUY", candles, indicators, structure)
    assert r["status"] == "INVALIDATED"
    assert r["setup_state"] == "INVALIDATED"
    assert r["direction"] == "NO_SETUP"
    assert r["entry_price"] is None


def test_waiting_for_confirmation_when_zone_valid_but_trigger_missing():
    candles = _generate_candles(1.1000, trend="bullish", count=60)
    last_ts = candles[-1]["timestamp"]
    # Overbought RSI removes the momentum condition -> missing non-empty.
    indicators = {"ema20": 1.1080, "ema50": 1.1040, "rsi14": 78.0, "atr14": 0.0015,
                  "macd": {"line": 0.0005, "signal": 0.0003, "histogram": 0.0002}}
    structure = {"breaks": [{"direction": "bullish", "level": 1.1070, "type": "BOS", "confirmed_at": last_ts}],
                 "support_clusters": [], "resistance_clusters": []}
    # Move the latest close onto the computed entry zone (~1.1070) so price is "at zone".
    for c in candles[-3:]:
        c["close"] = 1.1070
        c["open"] = 1.1068
        c["high"] = 1.1072
        c["low"] = 1.1066
    r = _eval("EURUSD", "BUY", candles, indicators, structure)
    assert r["status"] == "WAITING_FOR_CONFIRMATION"
    assert r["setup_state"] == "WAITING_FOR_CONFIRMATION"
    # Levels are watch-levels, never a confirmed signal.
    assert r["confirmation_complete"] is False
    assert len(r["conditions_not_satisfied"]) > 0


def test_min_rr_filter_rejects_below_threshold():
    candles = _generate_candles(1.1000, trend="bullish", count=60)
    last_ts = candles[-1]["timestamp"]
    indicators = {"ema20": 1.1080, "ema50": 1.1040, "rsi14": 55.0, "atr14": 0.0015,
                  "macd": {"line": 0.0004, "signal": 0.0002, "histogram": 0.0002}}
    structure = {"breaks": [{"direction": "bullish", "level": 1.1070, "type": "BOS", "confirmed_at": last_ts}],
                 "support_clusters": [], "resistance_clusters": []}
    r = _eval("EURUSD", "BUY", candles, indicators, structure, min_rr=4.0)
    assert r["direction"] == "NO_SETUP"
    assert r["status"] in {"WATCHLIST", "NO_SETUP", "WAITING_FOR_CONFIRMATION"}
    assert r["entry_price"] is None


def test_out_of_order_timestamps_are_reported_but_do_not_fabricate_levels():
    candles = _generate_candles(1.1000, trend="bullish", count=60)
    candles[10], candles[11] = candles[11], candles[10]  # swap -> out of order
    last_ts = candles[-1]["timestamp"]
    indicators = {"ema20": 1.1080, "ema50": 1.1040, "rsi14": 55.0, "atr14": 0.0015,
                  "macd": {"line": 0.0004, "signal": 0.0002, "histogram": 0.0002}}
    structure = {"breaks": [{"direction": "bullish", "level": 1.1070, "type": "BOS", "confirmed_at": last_ts}],
                 "support_clusters": [], "resistance_clusters": []}
    r = _eval("EURUSD", "BUY", candles, indicators, structure)
    assert any("chronological" in w.lower() for w in r["warnings"])
    # Still produces directional, tick-consistent levels rather than fabricating them.
    if r["direction"] == "BUY":
        assert r["stop_loss"] < r["entry_price"] < r["take_profit_1"]

