"""SiftingIO shared-stream tests.

These lock in the fix for the "WebSocket closed (max connections)" bug: the free
plan allows a single concurrent upstream WebSocket, so the backend must own one
shared connection that is multiplexed to every browser client and symbol - never
one connection per subscriber, per timeframe, or per HTTP request.

Every test injects a fake ``websockets.connect``, so no live provider call is
made and no SiftingIO credits are consumed.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.market_data import providers as providers_module
from app.market_data.providers import ProviderError, SiftingProvider


def run(coro):
    return asyncio.run(coro)


async def _wait(predicate, timeout: float = 1.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return bool(predicate())


_CLOSE = object()


class _FakeWS:
    def __init__(self, server):
        self.server = server
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        payload = json.loads(message)
        self.sent.append(payload)
        self.server.sent.append(payload)

    async def recv(self):
        frame = await self.server.outbound.get()
        if frame is _CLOSE:
            raise providers_module.websockets.exceptions.ConnectionClosedError(None, None)
        return frame


class _FakeConnectContext:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc):
        self.ws.closed = True
        return False


class _FakeServer:
    """Stands in for ``websockets.connect`` and the provider's server side."""

    def __init__(self):
        self.clients: list[_FakeWS] = []
        self.sent: list[dict] = []
        self.connect_count = 0
        self.outbound: asyncio.Queue = asyncio.Queue()

    def connect(self, *args, **kwargs):
        self.connect_count += 1
        ws = _FakeWS(self)
        self.clients.append(ws)
        return _FakeConnectContext(ws)

    def push(self, frame) -> None:
        self.outbound.put_nowait(frame)

    def push_json(self, payload: dict) -> None:
        self.outbound.put_nowait(json.dumps(payload))

    def current(self) -> _FakeWS:
        return self.clients[-1]

    def frames(self, op: str) -> list[dict]:
        return [f for f in self.sent if f.get("op") == op]


def _install(monkeypatch, server) -> None:
    """Point the provider module's ``websockets`` at the fake server."""

    class Namespace:
        exceptions = providers_module.websockets.exceptions
        connect = server.connect

    monkeypatch.setattr(providers_module, "websockets", Namespace)


def _hub(server, **overrides):
    provider = SiftingProvider("test-key")
    SiftingProvider.reset_stream_hubs()
    hub = provider._stream_hub()
    hub.PING_INTERVAL = 999.0  # default quiet unless a test overrides it
    for key, value in overrides.items():
        setattr(hub, key, value)
    return hub


def test_single_shared_connection_groups_subscriptions_by_product(monkeypatch):
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server)

    async def scenario():
        await hub.subscribe("EURUSD")
        await hub.subscribe("GBPUSD")
        await hub.subscribe("XAUUSD")
        assert await _wait(lambda: len([f for f in server.sent if f.get("op") == "subscribe"]) == 2)
        assert server.connect_count == 1  # one upstream connection despite three symbols
        subs = [f for f in server.sent if f.get("op") == "subscribe"]
        forex = next(f for f in subs if f["product"] == "fx")
        gold = next(f for f in subs if f["product"] == "com")
        assert forex["symbols"] == ["EURUSD", "GBPUSD"]  # grouped onto one frame
        assert gold["symbols"] == ["XAUUSD"]
        await hub.aclose()

    run(scenario())


def test_ticks_route_to_the_matching_symbol(monkeypatch):
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server)

    async def scenario():
        eur = await hub.subscribe("EURUSD")
        xau = await hub.subscribe("XAUUSD")
        assert await _wait(lambda: len([f for f in server.sent if f.get("op") == "subscribe"]) == 2)
        stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        server.push_json({"f": "tick", "s": "EURUSD", "p": "1.12388", "t": stamp})
        server.push_json({"f": "tick", "s": "XAUUSD", "p": "2650.5", "t": stamp})
        server.push_json({"f": "tick", "s": "GBPUSD", "p": "1.27", "t": stamp})  # not subscribed
        eur_tick = await asyncio.wait_for(eur.get(), 1.0)
        xau_tick = await asyncio.wait_for(xau.get(), 1.0)
        assert eur_tick["symbol"] == "EURUSD" and eur_tick["price"] == 1.12388
        assert eur_tick["data_status"] == "LIVE" and eur_tick["provider"] == "SIFTING"
        assert xau_tick["symbol"] == "XAUUSD" and xau_tick["price"] == 2650.5
        assert eur.empty() and xau.empty()  # the GBPUSD tick was not routed anywhere
        await hub.aclose()

    run(scenario())


def test_duplicate_subscription_is_prevented(monkeypatch):
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server)

    async def scenario():
        await hub.subscribe("EURUSD")
        await hub.subscribe("EURUSD")  # second browser client, same symbol
        assert await _wait(lambda: any(f.get("op") == "subscribe" for f in server.sent))
        await asyncio.sleep(0.05)
        subs = [f for f in server.sent if f.get("op") == "subscribe"]
        assert len(subs) == 1  # never subscribed the same symbol twice
        assert subs[0]["symbols"] == ["EURUSD"]
        await hub.aclose()

    run(scenario())


def test_ping_is_sent_on_a_wall_clock_cadence(monkeypatch):
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server, PING_INTERVAL=0.05)

    async def scenario():
        await hub.subscribe("EURUSD")
        assert await _wait(lambda: any(f.get("op") == "ping" for f in server.sent))
        await hub.aclose()

    run(scenario())


def test_ack_records_plan_limits_safely(monkeypatch):
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server)

    async def scenario():
        await hub.subscribe("EURUSD")
        assert await _wait(lambda: server.connect_count == 1)
        server.push_json({"f": "ack", "op": "auth", "tier": "free",
                          "max_conn": 1, "max_subs": 10, "active_conn": 1})
        assert await _wait(lambda: hub.diagnostics()["max_conn"] == 1)
        info = hub.diagnostics()
        assert info["tier"] == "free" and info["max_subs"] == 10 and info["active_conn"] == 1
        assert "key" not in json.dumps(info).lower()  # no secret ever leaks into diagnostics
        await hub.aclose()

    run(scenario())


def test_max_connections_backs_off_without_a_tight_loop(monkeypatch):
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server, LIMIT_COOLDOWN=0.3, BACKOFF_START=0.02)

    async def scenario():
        await hub.subscribe("EURUSD")
        assert await _wait(lambda: server.connect_count == 1)
        server.push_json({"f": "error", "code": "max_connections"})
        assert await _wait(lambda: hub.diagnostics()["limit_reached"])
        await asyncio.sleep(0.1)
        assert server.connect_count == 1  # still cooling down, not hammering the provider
        # ... but it does recover afterwards instead of giving up forever.
        assert await _wait(lambda: server.connect_count >= 2, 1.5)
        await hub.aclose()

    run(scenario())


def test_auth_failure_stops_retrying(monkeypatch):
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server, BACKOFF_START=0.02)

    async def scenario():
        await hub.subscribe("EURUSD")
        assert await _wait(lambda: server.connect_count == 1)
        server.push_json({"f": "error", "code": "auth_failed"})
        assert await _wait(lambda: hub.diagnostics()["last_error_code"] == "auth_failed")
        await asyncio.sleep(0.15)
        assert server.connect_count == 1  # a bad key is not retried in a loop
        await hub.aclose()

    run(scenario())


def test_reconnect_restores_subscriptions(monkeypatch):
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server, BACKOFF_START=0.02, BACKOFF_MAX=0.05)

    async def scenario():
        await hub.subscribe("EURUSD")
        assert await _wait(lambda: server.connect_count == 1)
        assert await _wait(lambda: any(f.get("op") == "subscribe" for f in server.sent))
        server.push(_CLOSE)  # simulate the provider dropping the connection
        assert await _wait(lambda: server.connect_count >= 2, 1.5)
        assert await _wait(lambda: len([f for f in server.sent if f.get("op") == "subscribe"]) >= 2)
        subs = [f for f in server.sent if f.get("op") == "subscribe"]
        assert subs[1]["symbols"] == ["EURUSD"]  # resubscribed after reconnect
        await hub.aclose()

    run(scenario())


def test_invalid_ticks_are_rejected_never_routed(monkeypatch):
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server)
    provider = SiftingProvider("test-key")

    assert provider.tick_from_payload({"p": "not-a-number"}, "EURUSD") is None
    assert provider.tick_from_payload({}, "EURUSD") is None
    assert provider.tick_from_payload({"p": "nan"}, "EURUSD") is None
    assert provider.tick_from_payload({"p": "0"}, "EURUSD") is None
    assert provider.tick_from_payload({"p": "-3"}, "EURUSD") is None
    good = provider.tick_from_payload({"p": "1.5", "t": int(datetime.now(timezone.utc).timestamp() * 1000)}, "EURUSD")
    assert good is not None and good["price"] == 1.5

    async def scenario():
        eur = await hub.subscribe("EURUSD")
        assert await _wait(lambda: server.connect_count == 1)
        server.push_json({"f": "tick", "s": "EURUSD", "p": "oops"})  # malformed price
        server.push_json({"f": "pong"})  # pongs are ignored, not routed
        server.push_json({"f": "tick", "s": "EURUSD", "p": "-1"})
        await asyncio.sleep(0.1)
        assert eur.empty()
        await hub.aclose()

    run(scenario())


def test_old_tick_is_labeled_stale_not_live():
    """A replayed last-trade on a closed market must never be shown as LIVE."""
    provider = SiftingProvider("test-key")
    old_ms = int((datetime.now(timezone.utc).timestamp() - 3600) * 1000)
    stale = provider.tick_from_payload({"p": "1.5", "t": old_ms}, "EURUSD")
    assert stale is not None and stale["data_status"] == "STALE"

    fresh_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fresh = provider.tick_from_payload({"p": "1.5", "t": fresh_ms}, "EURUSD")
    assert fresh is not None and fresh["data_status"] == "LIVE"


def test_tick_without_provider_timestamp_is_never_fabricated():
    """A price frame with no usable provider time must be dropped.

    Stamping it with the local receive time would make an arbitrarily old price
    look fresh and eligible for a LIVE status, which is exactly the trap the
    provider's replay-on-subscribe sets when a market is closed.
    """
    provider = SiftingProvider("test-key")
    assert provider.tick_from_payload({"p": "1.5"}, "EURUSD") is None
    assert provider.tick_from_payload({"p": "1.5", "t": None}, "EURUSD") is None
    assert provider.tick_from_payload({"p": "1.5", "t": "not-a-time"}, "EURUSD") is None
    assert provider.tick_from_payload({"p": "1.5", "t": ""}, "EURUSD") is None


def test_unsupported_symbol_is_rejected():
    provider = SiftingProvider("test-key")

    async def scenario():
        with pytest.raises(ProviderError):
            async for _ in provider.stream_quotes("DOGEUSD"):
                pass

    run(scenario())


def test_closing_the_last_consumer_closes_the_shared_connection(monkeypatch):
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server)

    async def scenario():
        queue = await hub.subscribe("EURUSD")
        assert await _wait(lambda: server.connect_count == 1)
        await hub.unsubscribe("EURUSD", queue)
        assert await _wait(lambda: not hub.diagnostics()["connected"])
        assert hub._task is None
        await hub.aclose()

    run(scenario())


def test_subscribing_a_second_symbol_is_reconciled_without_waiting_for_the_ping_interval(monkeypatch):
    """A new symbol must subscribe at once, not after the recv() timeout.

    The connection loop parks in ``recv()`` between frames; a late subscriber
    must wake it instead of waiting up to one ping interval for its first tick.
    """
    server = _FakeServer()
    _install(monkeypatch, server)
    hub = _hub(server, PING_INTERVAL=999.0)

    async def scenario():
        await hub.subscribe("EURUSD")
        assert await _wait(lambda: any(f.get("op") == "subscribe" for f in server.sent))
        await hub.subscribe("XAUUSD")  # the loop is already parked in recv()
        assert await _wait(
            lambda: any(f.get("op") == "subscribe" and f["product"] == "com" for f in server.sent),
            1.0,
        )
        await hub.aclose()

    run(scenario())


def test_market_data_websocket_relays_shared_stream_to_browser(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    class StubProvider:
        provider_id = "SIFTING"

        async def stream_quotes(self, symbol):
            yield {"type": "price", "symbol": symbol, "price": 1.2345,
                   "timestamp": datetime.now(timezone.utc).isoformat(),
                   "provider": "SIFTING", "data_status": "LIVE", "data_source": "SIFTING"}
            await asyncio.sleep(30)

    stub = StubProvider()
    monkeypatch.setattr(main.market_data, "provider_for", lambda symbol: stub)
    monkeypatch.setattr(main.market_data, "status", lambda symbol: {
        "symbol": symbol, "provider": "SIFTING", "configured": True,
        "connection_status": "CONNECTED", "data_status": "DISCONNECTED",
        "streaming": False, "message": "SIFTING configured"})

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/market-data?symbol=XAUUSD") as ws:
            status = ws.receive_json()
            assert status["type"] == "status"
            assert status["retryable"] is True
            tick = ws.receive_json()
            assert tick["type"] == "price"
            assert tick["symbol"] == "XAUUSD" and tick["price"] == 1.2345


def test_market_data_websocket_rejects_unknown_instrument():
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/market-data?symbol=DOGEUSD") as ws:
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["retryable"] is False


def test_provider_failure_yields_no_candles_and_no_trade_signal(monkeypatch):
    """Invalid/incomplete candles must never feed indicators or BUY/SELL signals."""
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "_market_data_errors", {})

    def fail(*args, **kwargs):
        raise ProviderError("SiftingIO is unavailable", status="ERROR")

    monkeypatch.setattr(main.market_data, "get_historical_candles", fail)
    monkeypatch.setattr(main.market_data, "status", lambda symbol: {
        "symbol": symbol, "provider": "SIFTING", "configured": True,
        "connection_status": "ERROR", "data_status": "ERROR",
        "streaming": False, "message": "SIFTING configured"})

    client = TestClient(main.app)

    snapshot = client.get("/api/v1/analysis/snapshot?symbol=XAUUSD&timeframe=15m").json()
    assert snapshot["candles"] == []
    assert any("unavailable" in w.lower() for w in snapshot["warnings"])

    setup = client.get("/api/v1/analysis/setup?symbol=XAUUSD&timeframe=15m").json()
    assert setup["status"] in {"DATA_UNAVAILABLE", "NO_SETUP"}
    assert setup["direction"] == "NO_SETUP"
    assert setup["entry_price"] is None
    assert setup["stop_loss"] is None
