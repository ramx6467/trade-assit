import pytest
from app.analysis.indicators import ema, sma, rsi, atr, vwap, bollinger_bands, macd

def test_ema_sma_seed_and_recurrence():
    assert ema([1, 2, 3, 4], 3) == [None, None, 2.0, 3.0]
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    assert ema([2, 4, 6, 8], 2) == [None, 3.0, 5.0, 7.0]

def test_rsi_rising_and_flat_edges():
    assert rsi([1, 2, 3, 4], 2)[-1] == 100
    assert rsi([4, 4, 4, 4], 2)[-1] == 50
    assert rsi([1, 2], 14) == [None, None]

def test_atr_true_range_and_warmup():
    result = atr([3, 5, 6], [1, 2, 4], [2, 4, 5], 2)
    assert result[0] is None
    assert result[1] == 2.5
    assert result[2] == 2.25

def test_vwap_requires_valid_volume():
    out = vwap([3, 4], [1, 2], [2, 3], [2, 2])
    assert out[0] == pytest.approx(2)
    assert out[1] == pytest.approx(2.5)
    assert vwap([3], [1], [2], [None]) == [None]
    assert vwap([3], [1], [2], [10], volume_type="synthetic") == [None]

def test_vwap_resets_at_supplied_session_boundary():
    result = vwap([3, 5, 9], [1, 3, 7], [2, 4, 8], [2, 2, 1], session_ids=["d1", "d1", "d2"])
    assert result == pytest.approx([2.0, 3.0, 8.0])

def test_macd_known_alignment_and_values():
    result = macd([1, 2, 3, 4, 5], fast=2, slow=3, signal=2)
    assert result["line"] == [None, None, 0.5, 0.5, 0.5]
    assert result["signal"] == [None, None, None, 0.5, 0.5]
    assert result["histogram"] == [None, None, None, 0.0, 0.0]

def test_invalid_values_rejected():
    with pytest.raises(ValueError): ema([], 3)
    with pytest.raises(ValueError): sma([1], 0)

def test_bollinger_bands_period_and_boundaries():
    result = bollinger_bands([1, 2, 3, 4], period=3, deviations=2)
    assert result['middle'][1] is None
    assert result['middle'][-1] == 3
    assert result['upper'][-1] > result['middle'][-1] > result['lower'][-1]
    reference = bollinger_bands([1, 2, 3], period=3, deviations=2)
    spread = (2 / 3) ** 0.5
    assert reference['middle'] == [None, None, 2.0]
    assert reference['upper'][-1] == pytest.approx(2 + 2 * spread)
    assert reference['lower'][-1] == pytest.approx(2 - 2 * spread)

def test_indicator_warmups_are_null_until_the_documented_seed():
    assert ema([1, 2], 3) == [None, None]
    assert rsi([1, 2], 14) == [None, None]
    assert atr([2, 3], [1, 2], [1.5, 2.5], 3) == [None, None]
    assert bollinger_bands([1, 2], period=3)['middle'] == [None, None]

def test_macd_warmup_signal_and_histogram():
    result = macd([float(i) for i in range(1, 50)])
    assert result['line'][24] is None
    assert result['line'][25] is not None
    assert result['signal'][32] is None
    assert result['signal'][33] is not None
    assert result['histogram'][-1] == pytest.approx(result['line'][-1] - result['signal'][-1])

def test_adx_warmup_and_trend_readings():
    from app.analysis.indicators import adx
    # Warmup: fewer than 2*period-1 bars yields no ADX.
    assert adx([2, 3], [1, 2], [1.5, 2.5], 3) == [None, None]
    # A steady uptrend has directional movement dominated by +DM, so the ADX
    # reaches a strong reading once the full warmup has elapsed.
    n = 60
    highs = [float(i) + 1.0 for i in range(n)]
    lows = [float(i) - 0.5 for i in range(n)]
    closes = [float(i) + 0.5 for i in range(n)]
    series = adx(highs, lows, closes, 14)
    assert len(series) == n
    assert all(v is None for v in series[:26])
    assert series[26] is not None
    assert series[-1] is not None and series[-1] > 25
