"""Small deterministic indicator implementations; outputs align to input indices."""
from __future__ import annotations
from typing import Optional
import numpy as np


def _values(values: list[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1 or len(arr) == 0 or not np.isfinite(arr).all():
        raise ValueError("Input must be a non-empty list of finite numbers")
    return arr


def ema(values: list[float], period: int) -> list[Optional[float]]:
    a = _values(values)
    if period < 1:
        raise ValueError("period must be positive")
    out: list[Optional[float]] = [None] * len(a)
    if len(a) < period:
        return out
    # SMA seed at index period-1; subsequent values use the standard EMA recurrence.
    current = float(a[:period].mean())
    out[period - 1] = current
    alpha = 2.0 / (period + 1)
    for i in range(period, len(a)):
        current = alpha * float(a[i]) + (1 - alpha) * current
        out[i] = current
    return out


def sma(values: list[float], period: int) -> list[Optional[float]]:
    a = _values(values)
    if period < 1:
        raise ValueError("period must be positive")
    return [None if i + 1 < period else float(a[i + 1 - period:i + 1].mean()) for i in range(len(a))]


def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    a = _values(values)
    if period < 1:
        raise ValueError("period must be positive")
    out: list[Optional[float]] = [None] * len(a)
    if len(a) <= period:
        return out
    changes = np.diff(a)
    gains = np.maximum(changes, 0)
    losses = np.maximum(-changes, 0)
    avg_gain, avg_loss = float(gains[:period].mean()), float(losses[:period].mean())
    def score(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)
    out[period] = score(avg_gain, avg_loss)
    for i in range(period + 1, len(a)):
        avg_gain = (avg_gain * (period - 1) + float(gains[i - 1])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i - 1])) / period
        out[i] = score(avg_gain, avg_loss)
    return out


def atr(high: list[float], low: list[float], close: list[float], period: int = 14) -> list[Optional[float]]:
    h, l, c = _values(high), _values(low), _values(close)
    if len(h) != len(l) or len(h) != len(c) or period < 1:
        raise ValueError("OHLC arrays must have equal length and period must be positive")
    tr = np.empty(len(h))
    tr[0] = h[0] - l[0]
    for i in range(1, len(h)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
    out: list[Optional[float]] = [None] * len(h)
    if len(h) < period:
        return out
    current = float(tr[:period].mean())
    out[period - 1] = current
    for i in range(period, len(h)):
        current = (current * (period - 1) + float(tr[i])) / period
        out[i] = current
    return out


def vwap(
    high: list[float], low: list[float], close: list[float], volume: list[Optional[float]],
    session_ids: list[str] | None = None, volume_type: str = "exchange",
    timestamps: list[str] | None = None,
) -> list[Optional[float]]:
    """Session VWAP from typical price and real exchange/tick volume.

    Callers must explicitly mark the volume type. A session identifier (for
    example an exchange-local trading date) resets the cumulative totals.
    """
    h, l, c = _values(high), _values(low), _values(close)
    if not (len(h) == len(l) == len(c) == len(volume)):
        raise ValueError("OHLCV arrays must have equal length")
    if volume_type not in {"exchange", "tick"}:
        return [None] * len(c)
    if session_ids is not None and len(session_ids) != len(c):
        raise ValueError("session_ids must have the same length as OHLCV arrays")
    if timestamps is not None and len(timestamps) != len(c):
        raise ValueError("timestamps must have the same length as OHLCV arrays")

    result: list[Optional[float]] = []
    cumulative_pv = 0.0
    cumulative_volume = 0.0
    prior_session: str | None = None
    seen_timestamps: set[str] = set()

    for i in range(len(c)):
        session = session_ids[i] if session_ids is not None else "single-window"
        if session != prior_session:
            cumulative_pv = 0.0
            cumulative_volume = 0.0
            prior_session = session
            seen_timestamps.clear()

        ts = timestamps[i] if timestamps is not None else None
        if ts is not None and ts in seen_timestamps:
            # Duplicate candle timestamp: do not double-count in cumulative totals
            result.append(cumulative_pv / cumulative_volume if cumulative_volume > 0 else None)
            continue
        if ts is not None:
            seen_timestamps.add(ts)

        hi, lo, cl = float(h[i]), float(l[i]), float(c[i])
        vol_raw = volume[i]

        valid_vol = False
        vol_num = 0.0
        if vol_raw is not None:
            try:
                vol_num = float(vol_raw)
                if np.isfinite(vol_num) and vol_num > 0:
                    valid_vol = True
            except (ValueError, TypeError):
                valid_vol = False

        if valid_vol and np.isfinite(hi) and np.isfinite(lo) and np.isfinite(cl):
            tp = (hi + lo + cl) / 3.0
            cumulative_pv += tp * vol_num
            cumulative_volume += vol_num

        result.append(cumulative_pv / cumulative_volume if cumulative_volume > 0 else None)
    return result



def bollinger_bands(values: list[float], period: int = 20, deviations: float = 2.0) -> dict[str, list[Optional[float]]]:
    a = _values(values)
    if period < 1 or deviations <= 0:
        raise ValueError("period and deviations must be positive")
    middle: list[Optional[float]] = [None] * len(a)
    upper: list[Optional[float]] = [None] * len(a)
    lower: list[Optional[float]] = [None] * len(a)
    for i in range(period - 1, len(a)):
        window = a[i - period + 1:i + 1]
        avg = float(window.mean())
        spread = float(window.std(ddof=0)) * deviations
        middle[i], upper[i], lower[i] = avg, avg + spread, avg - spread
    return {"middle": middle, "upper": upper, "lower": lower}


def adx(high: list[float], low: list[float], close: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder's Average Directional Index (ADX) with +DI/-DI.

    Deterministic, index-aligned, and ``None`` until enough candles exist
    (``2 * period - 1`` bars). It uses only the supplied OHLC values and does
    not require volume, so it is computed whenever the history is long enough;
    callers report it as unavailable otherwise.
    """
    h, l, c = _values(high), _values(low), _values(close)
    if len(h) != len(l) or len(h) != len(c) or period < 1:
        raise ValueError("OHLC arrays must have equal length and period must be positive")
    n = len(h)
    out: list[Optional[float]] = [None] * n
    if n < 2 * period - 1:
        return out

    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        up = float(h[i] - h[i - 1])
        down = float(l[i - 1] - l[i])
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))

    def wilder(values: np.ndarray) -> list[Optional[float]]:
        smoothed: list[Optional[float]] = [None] * n
        current = float(values[:period].mean())
        smoothed[period - 1] = current
        for i in range(period, n):
            current = (current * (period - 1) + float(values[i])) / period
            smoothed[i] = current
        return smoothed

    atr_s, plus_s, minus_s = wilder(tr), wilder(plus_dm), wilder(minus_dm)

    dx: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        atr_i, plus_i, minus_i = atr_s[i], plus_s[i], minus_s[i]
        if not atr_i or plus_i is None or minus_i is None:
            continue
        di_plus = 100.0 * plus_i / atr_i
        di_minus = 100.0 * minus_i / atr_i
        denom = di_plus + di_minus
        dx[i] = 100.0 * abs(di_plus - di_minus) / denom if denom > 0 else 0.0

    first = 2 * period - 2
    current = float(np.mean([dx[i] for i in range(period - 1, first + 1)]))
    out[first] = current
    for i in range(first + 1, n):
        if dx[i] is None:
            continue
        current = (current * (period - 1) + float(dx[i])) / period
        out[i] = current
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list[Optional[float]]]:
    a = _values(values)
    if fast < 1 or slow <= fast or signal < 1:
        raise ValueError("MACD requires 0 < fast < slow and a positive signal period")
    fast_line, slow_line = ema(a.tolist(), fast), ema(a.tolist(), slow)
    line: list[Optional[float]] = [None] * len(a)
    values_for_signal: list[float] = []
    start = slow - 1
    for i in range(start, len(a)):
        value = float(fast_line[i]) - float(slow_line[i])
        line[i] = value
        values_for_signal.append(value)
    signal_values = ema(values_for_signal, signal)
    signal_line: list[Optional[float]] = [None] * len(a)
    histogram: list[Optional[float]] = [None] * len(a)
    for offset, signal_value in enumerate(signal_values):
        i = start + offset
        if signal_value is not None:
            signal_line[i] = signal_value
            histogram[i] = float(line[i]) - signal_value
    return {"line": line, "signal": signal_line, "histogram": histogram}
