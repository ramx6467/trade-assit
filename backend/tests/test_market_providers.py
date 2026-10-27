from app.market_data.demo import DemoMarketDataProvider
from app.market_data.providers import KiteProvider, MarketDataService, ProviderError, TwelveDataProvider


def test_demo_provider_supports_requested_chart_timeframes_and_explicit_demo_status():
    provider = DemoMarketDataProvider()
    for timeframe in ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"):
        rows = provider.get_historical_candles("EURUSD", timeframe, 4)
        assert len(rows) == 4
        assert all(row["data_source"] == "DEMO" and row["volume"] is None for row in rows)


def test_provider_credentials_missing_are_clear_and_never_fall_back_to_demo(monkeypatch):
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "TWELVE_DATA")
    monkeypatch.delenv("INTERNATIONAL_DATA_API_KEY", raising=False)
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    provider = MarketDataService()
    status = provider.status("EURUSD")
    assert status["data_status"] == "ERROR"
    assert "INTERNATIONAL_DATA_API_KEY" in status["message"]
    try:
        provider.get_historical_candles("EURUSD", "1m")
        assert False, "Missing credentials must not return demo rows."
    except ProviderError:
        pass


def test_twelve_data_service_reads_requested_environment_variable(monkeypatch):
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "twelvedata")
    monkeypatch.setenv("INTERNATIONAL_DATA_API_KEY", "unit-test-secret")
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    provider = MarketDataService()
    assert isinstance(provider.international, TwelveDataProvider)
    assert provider.status("EURUSD")["configured"] is True


def test_twelve_data_maps_supported_symbols_and_normalizes_provider_rows(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        is_success = True

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "ok", "values": [
                {"datetime": "2026-10-16 10:01:00", "open": "1.1", "high": "1.2", "low": "1.0", "close": "1.15"},
                {"datetime": "2026-10-16 10:00:00", "open": "1.0", "high": "1.2", "low": "0.9", "close": "1.1"},
            ]}

    def get(url, *, params, headers, timeout):
        captured.update(params)
        captured["authorization_header"] = headers["Authorization"].startswith("apikey ")
        return Response()

    monkeypatch.setattr("app.market_data.providers.httpx.get", get)
    provider = TwelveDataProvider("unit-test-secret")
    rows = provider.get_historical_candles("EURUSD", "1m", 2)
    assert captured["symbol"] == "EUR/USD" and captured["timezone"] == "UTC"
    assert captured["interval"] == "1min" and "apikey" not in captured
    assert captured["authorization_header"] is True
    assert rows[0]["timestamp"] == "2026-10-16T10:00:00Z"
    assert rows[0]["data_source"] == "TWELVE_DATA" and rows[0]["volume"] is None


def test_twelve_data_rejects_unsupported_symbol_and_provider_error_without_leaking_details(monkeypatch):
    provider = TwelveDataProvider("unit-test-secret")
    try:
        provider.get_historical_candles("UNKNOWN", "1m")
        assert False
    except ProviderError as exc:
        assert "UNKNOWN" in str(exc)

    class Response:
        status_code = 200
        is_success = True

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "error", "message": "secret and internal request details"}

    monkeypatch.setattr("app.market_data.providers.httpx.get", lambda *a, **k: Response())
    try:
        provider.get_historical_candles("EURUSD", "1m")
        assert False
    except ProviderError as exc:
        assert "secret" not in str(exc)


def test_twelve_data_classifies_auth_rate_limit_and_subscription_errors():
    assert "authentication failed" in str(TwelveDataProvider._error(401, {"code": 401})).lower()
    assert "credit limit" in str(TwelveDataProvider._error(429, {"code": 429})).lower()
    assert "subscription" in str(TwelveDataProvider._error(200, {"code": 403})).lower()


def test_twelve_data_rejects_unsupported_interval_instead_of_guessing():
    try:
        TwelveDataProvider("unit-test-secret").get_historical_candles("EURUSD", "3m", 10)
        assert False, "Unsupported intervals must be rejected rather than fabricated."
    except ProviderError as exc:
        assert "does not support 3m" in str(exc)


def test_kite_alias_mapping_uses_provider_catalog_without_network():
    provider = KiteProvider("test-key", "test-token")
    provider._instruments = lambda: {"NSE:NIFTY 50": ("NSE", 256265), "NSE:NIFTY BANK": ("NSE", 260105)}
    assert provider.instrument_token("NIFTY") == ("NSE", 256265)
    assert provider.instrument_token("BANKNIFTY") == ("NSE", 260105)
    try:
        provider.instrument_token("MISSING")
        assert False
    except ProviderError as exc:
        assert "Unsupported Kite instrument" in str(exc)


def test_freshness_status_requires_recent_provider_tick(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import main

    class ConfiguredProvider:
        def status(self, symbol):
            return {"symbol": symbol, "provider": "TWELVE_DATA", "configured": True, "data_status": "DISCONNECTED"}

    monkeypatch.setattr(main, "market_data", ConfiguredProvider())
    recent = datetime.now(timezone.utc).isoformat()
    monkeypatch.setitem(main._last_market_updates, "XAUUSD", recent)
    assert main._data_status("XAUUSD") == "LIVE"
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    monkeypatch.setitem(main._last_market_updates, "XAUUSD", stale)
    assert main._data_status("XAUUSD") == "STALE"


def test_twelve_data_service_builds_3m_from_real_supported_1m_candles(monkeypatch):
    monkeypatch.setenv("INTERNATIONAL_DATA_PROVIDER", "TWELVE_DATA")
    monkeypatch.setenv("INTERNATIONAL_DATA_API_KEY", "unit-test-secret")
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    requested_intervals = []

    class Response:
        status_code = 200
        is_success = True

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "ok", "values": [
                {"datetime": "2026-10-16 10:00:00", "open": "1.0", "high": "1.2", "low": "0.9", "close": "1.1"},
                {"datetime": "2026-10-16 10:01:00", "open": "1.1", "high": "1.3", "low": "1.05", "close": "1.25"},
                {"datetime": "2026-10-16 10:02:00", "open": "1.25", "high": "1.4", "low": "1.2", "close": "1.3"},
                {"datetime": "2026-10-16 10:03:00", "open": "1.3", "high": "1.35", "low": "1.1", "close": "1.15"},
                {"datetime": "2026-10-16 10:04:00", "open": "1.15", "high": "1.5", "low": "1.14", "close": "1.45"},
                {"datetime": "2026-10-16 10:05:00", "open": "1.45", "high": "1.6", "low": "1.4", "close": "1.55"},
            ]}

    def get(url, *, params, headers, timeout):
        requested_intervals.append(params["interval"])
        return Response()

    monkeypatch.setattr("app.market_data.providers.httpx.get", get)
    service = MarketDataService()
    rows = service.get_historical_candles("EURUSD", "3m", 10)

    # The provider is only ever asked for the interval it genuinely supports.
    assert requested_intervals == ["1min"]
    assert [row["timestamp"] for row in rows] == ["2026-10-16T10:00:00Z", "2026-10-16T10:03:00Z"]
    assert all(row["timeframe"] == "3m" and row["data_source"] == "TWELVE_DATA" for row in rows)
    assert (rows[0]["open"], rows[0]["high"], rows[0]["low"], rows[0]["close"]) == (1.0, 1.4, 0.9, 1.3)


def test_connection_status_reflects_real_request_outcomes_not_a_configured_key(monkeypatch):
    from datetime import datetime, timezone
    from app import main

    class ConfiguredProvider:
        def status(self, symbol):
            return {"symbol": symbol, "provider": "TWELVE_DATA", "configured": True,
                    "connection_status": "DISCONNECTED", "data_status": "DISCONNECTED"}

    monkeypatch.setattr(main, "market_data", ConfiguredProvider())
    monkeypatch.setattr(main, "_last_market_updates", {})
    monkeypatch.setattr(main, "_market_data_errors", {})
    monkeypatch.setattr(main, "_last_market_success", {})

    # Configured credentials alone must never read as DISCONNECTED.
    assert main._connection_status("EURUSD") == "CONFIGURED"
    # A successful REST fetch is the only thing that proves CONNECTED.
    main._last_market_success["EURUSD"] = datetime.now(timezone.utc)
    assert main._connection_status("EURUSD") == "CONNECTED"
    # A recent provider error is reported instead of a blanket DISCONNECTED.
    main._market_data_errors["EURUSD"] = (datetime.now(timezone.utc), "credits exhausted", "QUOTA_EXCEEDED")
    assert main._connection_status("EURUSD") == "QUOTA_EXCEEDED"
    # A fresh stream tick is the stronger signal.
    main._last_market_updates["EURUSD"] = datetime.now(timezone.utc).isoformat()
    assert main._connection_status("EURUSD") == "STREAMING"


def test_connection_status_is_demo_when_no_provider_is_configured(monkeypatch):
    from app import main

    class DemoProvider:
        def status(self, symbol):
            return {"symbol": symbol, "provider": "DEMO", "configured": False, "data_status": "DEMO"}

    monkeypatch.setattr(main, "market_data", DemoProvider())
    assert main._connection_status("EURUSD") == "DEMO"


def test_status_reports_last_valid_stream_tick_without_spending_a_quote_credit(monkeypatch):
    """The status payload must expose the last genuine stream price.

    Regression: snapshot ticks populated ``last_update`` but the tick price was
    discarded, so the UI showed "price unavailable" even though a real provider
    quote existed. Rendering status must still never spend a REST credit.
    """
    from datetime import datetime, timedelta, timezone

    from app import main

    class ConfiguredProvider:
        def status(self, symbol):
            return {"symbol": symbol, "provider": "SIFTING", "configured": True,
                    "connection_status": "DISCONNECTED", "data_status": "DISCONNECTED"}

        def get_cached_quote(self, symbol):
            return None  # no REST quote has ever been fetched

        def available_timeframes(self, symbol):
            return None  # no capability list: the app's full frame set applies

    monkeypatch.setattr(main, "market_data", ConfiguredProvider())
    monkeypatch.setattr(main, "_last_market_updates", {})
    monkeypatch.setattr(main, "_market_data_errors", {})
    monkeypatch.setattr(main, "_last_market_success", {})

    stamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    main._last_ticks["XAUUSD"] = {
        "type": "price", "symbol": "XAUUSD", "price": 2342.21, "timestamp": stamp,
        "provider": "SIFTING", "data_status": "STALE", "data_source": "SIFTING",
    }
    main._last_market_updates["XAUUSD"] = stamp

    status = main.market_status("XAUUSD")
    assert status["price"] == 2342.21
    assert status["quote_available"] is True
    assert status["data_status"] == "STALE"  # closed-market replay, never LIVE
    assert status["streaming"] is False
    assert status["last_data_timestamp"] == stamp
    assert status["freshness_seconds"] is not None and status["freshness_seconds"] > 0

    main._last_ticks.pop("XAUUSD", None)


def test_replayed_older_tick_cannot_overwrite_a_newer_tick():
    """A re-subscribe replay of an older trade must not regress the price.

    SiftingIO resends its cached last trade on every subscribe, so the pipeline
    compares by the provider's own timestamp instead of arrival order.
    """
    from datetime import datetime, timedelta, timezone

    from app import main

    now = datetime.now(timezone.utc)
    main._last_ticks["XAUUSD"] = {
        "type": "price", "price": 4164.0,
        "timestamp": (now - timedelta(minutes=5)).isoformat(),
    }
    try:
        assert main._tick_is_newest("XAUUSD", now.isoformat()) is True
        assert main._tick_is_newest("XAUUSD", (now - timedelta(minutes=10)).isoformat()) is False
        assert main._tick_is_newest("XAUUSD", (now - timedelta(minutes=5)).isoformat()) is True
        assert main._tick_is_newest("XAUUSD", None) is False
        assert main._tick_is_newest("XAUUSD", "") is False
    finally:
        main._last_ticks.pop("XAUUSD", None)


def test_data_status_treats_any_older_replay_as_stale(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import main

    class ConfiguredProvider:
        def status(self, symbol):
            return {"symbol": symbol, "provider": "SIFTING", "configured": True,
                    "connection_status": "DISCONNECTED", "data_status": "DISCONNECTED"}

    monkeypatch.setattr(main, "market_data", ConfiguredProvider())
    monkeypatch.setattr(main, "_market_data_errors", {})
    # 150s: older than LIVE_FRESH_SECONDS (90) but newer than STALE_SECONDS (300).
    # It must still read STALE, never the provider's static DISCONNECTED.
    monkeypatch.setitem(main._last_market_updates, "EURUSD",
                        (datetime.now(timezone.utc) - timedelta(seconds=150)).isoformat())
    assert main._data_status("EURUSD") == "STALE"
    monkeypatch.delitem(main._last_market_updates, "EURUSD", raising=False)


def test_data_status_never_reports_disconnected_for_a_configured_provider(monkeypatch):
    """ ``data_status`` must describe data freshness, not connectivity.

    "DISCONNECTED" is a connectivity verdict (owned by ``_connection_status``)
    and the provider exposes it statically even while reachable, so it must
    never leak into the data-freshness field.
    """
    from datetime import datetime, timezone

    from app import main

    class ConfiguredProvider:
        def status(self, symbol):
            return {"symbol": symbol, "provider": "SIFTING", "configured": True,
                    "connection_status": "DISCONNECTED", "data_status": "DISCONNECTED"}

    monkeypatch.setattr(main, "market_data", ConfiguredProvider())
    monkeypatch.setattr(main, "_last_market_updates", {})
    monkeypatch.setattr(main, "_market_data_errors", {})
    monkeypatch.setattr(main, "_last_market_success", {})

    # Configured but nothing requested yet -> no quote exists yet.
    assert main._data_status("EURUSD") == "UNAVAILABLE"
    # A request has succeeded: reachable, but no latest quote in hand.
    main._last_market_success["EURUSD"] = datetime.now(timezone.utc)
    assert main._data_status("EURUSD") == "DELAYED"


def test_instrument_rows_expose_last_valid_stream_price(monkeypatch):
    from app import main

    monkeypatch.setattr(main.market_data, "get_instruments", lambda: [
        {"symbol": "XAUUSD", "name": "XAU/USD"},
    ])
    monkeypatch.setattr(main.market_data, "status", lambda s: {
        "symbol": s, "provider": "SIFTING", "configured": True,
        "connection_status": "CONNECTED", "data_status": "DISCONNECTED",
        "streaming": False, "message": "SIFTING configured"})
    monkeypatch.setattr(main.market_data, "get_cached_quote", lambda s: None)
    stamp = "2026-10-17T15:55:26.742000Z"
    monkeypatch.setattr(main, "_last_market_updates", {"XAUUSD": stamp})
    main._last_ticks["XAUUSD"] = {
        "type": "price", "symbol": "XAUUSD", "price": 2342.21,
        "timestamp": stamp, "provider": "SIFTING",
        "data_status": "STALE", "data_source": "SIFTING"}

    rows = {row["symbol"]: row for row in main._instrument_rows()}
    assert rows["XAUUSD"]["price"] == 2342.21
    assert rows["XAUUSD"]["quote_available"] is True
    assert rows["XAUUSD"]["data_status"] == "STALE"

    # With no retained tick and no cached REST quote the row carries no price.
    main._last_ticks.pop("XAUUSD", None)
    monkeypatch.setattr(main, "_last_market_updates", {})
    empty = {row["symbol"]: row for row in main._instrument_rows()}
    assert empty["XAUUSD"]["price"] is None
    assert empty["XAUUSD"]["quote_available"] is False


def test_fresh_rest_quote_is_live_but_never_streaming(monkeypatch):
    """A fresh REST quote must not be advertised as a live push stream.

    Regression: ``streaming`` was derived from the *data* status, so a REST-only
    provider (XAUS) reported ``streaming: true`` whenever its spot quote was
    fresh. The frontend reads ``streaming`` as "ticks are arriving, stop
    polling", so it stopped refreshing the quote and the candle snapshot. On a
    REST feed no ticks ever arrive, so the chart froze on its first snapshot
    while only the header price kept moving - exactly the "inconsistent candle
    timestamps / freshness" symptom (and a candle age that grows without bound).
    """
    from datetime import datetime, timezone

    from app import main

    class RestProvider:
        def status(self, symbol):
            return {"symbol": symbol, "provider": "XAUS", "configured": True,
                    "connection_status": "DISCONNECTED", "data_status": "DISCONNECTED"}

        def get_cached_quote(self, symbol):
            return {
                "symbol": symbol, "price": 4166.10,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "is_stale": False, "provider": "XAUS", "price_source": "gold-api.com",
            }

        def available_timeframes(self, symbol):
            return None

    monkeypatch.setattr(main, "market_data", RestProvider())
    monkeypatch.setattr(main, "_last_market_updates", {})
    monkeypatch.setattr(main, "_market_data_errors", {})
    # A REST fetch has succeeded, proving connectivity - but there is no socket.
    monkeypatch.setattr(main, "_last_market_success",
                        {"XAUUSD": datetime.now(timezone.utc)})

    status = main.market_status("XAUUSD")
    assert status["price"] == 4166.10
    assert status["data_status"] == "LIVE"       # the quote itself is genuinely fresh
    assert status["connection_status"] == "CONNECTED"
    assert status["streaming"] is False          # yet no push socket is delivering ticks

    # Only a recent live WebSocket tick may flip streaming on.
    monkeypatch.setitem(main._last_market_updates, "XAUUSD",
                        datetime.now(timezone.utc).isoformat())
    assert main.market_status("XAUUSD")["streaming"] is True

