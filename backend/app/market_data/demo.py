from datetime import datetime, timedelta, timezone
import math
from random import Random
from app.market_data.normalize import normalize_candles

SYMBOLS = {
    "EURUSD": {"name": "Euro / US Dollar", "asset_class": "Forex", "exchange": "DEMO FX", "exchange_timezone": "UTC", "price_precision": 5, "base": "EUR", "quote": "USD", "paper_unit": "1 EUR demo unit"},
    "XAUUSD": {"name": "XAU/USD — Gold", "asset_class": "Metals", "exchange": "DEMO FX", "exchange_timezone": "America/New_York", "price_precision": 2, "base": "XAU", "quote": "USD", "paper_unit": "1 XAU demo unit (not a broker lot)"},
    "GBPUSD": {"name": "British Pound / US Dollar", "asset_class": "Forex", "exchange": "DEMO FX", "exchange_timezone": "Europe/London", "price_precision": 5, "base": "GBP", "quote": "USD", "paper_unit": "1 GBP demo unit"},
    "NIFTY": {"name": "NIFTY 50", "asset_class": "Index", "exchange": "DEMO NSE", "exchange_timezone": "Asia/Kolkata", "price_precision": 2, "base": None, "quote": "INR", "paper_unit": "1 index-point demo unit (not an exchange contract)"},
    "BANKNIFTY": {"name": "NIFTY Bank", "asset_class": "Index", "exchange": "DEMO NSE", "exchange_timezone": "Asia/Kolkata", "price_precision": 2, "base": None, "quote": "INR", "paper_unit": "1 index-point demo unit (not an exchange contract)"},
}
FRAMES = {"1m": 60, "2m": 120, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400, "1w": 604800}

def candles(symbol: str = "EURUSD", timeframe: str = "15m", limit: int = 240) -> list[dict]:
    if symbol not in SYMBOLS or timeframe not in FRAMES:
        raise ValueError("Unsupported demo instrument or timeframe")
    rng = Random(f"{symbol}:{timeframe}")
    start_price = {"EURUSD": 1.084, "GBPUSD": 1.27, "XAUUSD": 2342.0, "NIFTY": 22450.0, "BANKNIFTY": 48000.0}[symbol]
    precision = SYMBOLS[symbol]["price_precision"]
    step = FRAMES[timeframe]
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    # Candle timestamps denote bar opens. Exclude the currently forming bar.
    if timeframe == "1w":
        current = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        current_monday = current - timedelta(days=current.weekday())
        last_completed_open = int((current_monday - timedelta(days=7)).timestamp())
    else:
        last_completed_open = (now_epoch // step) * step - step
    out = []
    previous = start_price
    for i in range(limit):
        drift = math.sin(i / 13) * start_price * 0.00016
        move = rng.uniform(-1, 1) * start_price * 0.00044 + drift
        op = previous
        close = max(0.0001, op + move)
        wick = abs(rng.uniform(0.05, 0.6) * start_price * 0.0003)
        hi, lo = max(op, close) + wick, min(op, close) - wick
        stamp = datetime.fromtimestamp(last_completed_open - step * (limit - i - 1), tz=timezone.utc)
        out.append({"timestamp": stamp.isoformat().replace("+00:00", "Z"), "open": round(op, precision), "high": round(hi, precision), "low": round(lo, precision), "close": round(close, precision), "volume": None, "volume_type": "unavailable"})
        previous = close
    return out

class DemoMarketDataProvider:
    """Deterministic synthetic data provider. Never represents live market access."""
    provider_id = "DEMO"

    def get_instruments(self) -> list[dict]:
        return [{"symbol": symbol, **meta, "provider": self.provider_id, "data_status": "DEMO"} for symbol, meta in SYMBOLS.items()]

    def get_historical_candles(self, symbol: str, timeframe: str, limit: int = 240) -> list[dict]:
        rows, _ = normalize_candles(candles(symbol, timeframe, limit), instrument=symbol,
                                    timeframe=timeframe, data_source=self.provider_id)
        return rows

    def get_latest_quote(self, symbol: str, timeframe: str = "15m") -> dict:
        row = self.get_historical_candles(symbol, timeframe, 1)[-1]
        return {"symbol": symbol, "instrument": symbol, "timeframe": timeframe, "price": row["close"],
                "timestamp": row["timestamp"], "provider": self.provider_id, "data_source": self.provider_id,
                "data_status": "DEMO"}
