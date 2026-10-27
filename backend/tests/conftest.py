import os

import pytest

# Isolate tests from the developer's configured or default local database.
os.environ["DATABASE_URL"] = "sqlite://"
# Keep the suite's default-provider tests independent from private local .env
# configuration; provider-specific tests set their own credentials explicitly.
for key in (
    "INTERNATIONAL_DATA_PROVIDER",
    "INTERNATIONAL_DATA_API_KEY",
    "TWELVE_DATA_API_KEY",
    "SIFTING_API_KEY",
):
    os.environ[key] = ""

from app.database import Base, engine
from app import models  # noqa: F401,E402

Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _freeze_env_reload(monkeypatch):
    """Keep the app's live .env re-read from undoing the suite's env isolation.

    ``app.ai_config.reload_env_if_modified`` refills any environment key that is
    present-but-empty from the developer's real backend/.env. The suite blanks the
    provider keys above on purpose, so the re-read would otherwise leak a
    configured provider into tests that expect none.
    """
    from app import ai_config

    monkeypatch.setattr(ai_config, "reload_env_if_modified", lambda: None)


@pytest.fixture(autouse=True)
def _reset_ai_verification_state():
    """Each test starts with a clean AI verification state.

    A real provider failure (quota, invalid key, model unavailable) recorded by
    one test must not leak into a later test that sets its own credentials.
    """
    from app.ai_config import reset_verification_state
    reset_verification_state()
    yield


@pytest.fixture(autouse=True)
def _reset_provider_caches():
    """Keep the provider TTL/request-dedup caches from leaking across tests.

    The caches are class-level (deliberately shared across provider instances in
    production), so without this a test that populates candles would change the
    behaviour of a later test that expects a real provider call.
    """
    from app.market_data.providers import GoldProvider, SiftingProvider, TwelveDataProvider, XausProvider
    import shutil

    def _clear() -> None:
        for provider in (TwelveDataProvider, SiftingProvider, GoldProvider, XausProvider):
            provider._candle_cache.clear()
            provider._quote_cache.clear()
            provider._inflight.clear()
        # Also clear XausProvider's persistent disk cache
        if XausProvider.CACHE_DIR and os.path.exists(XausProvider.CACHE_DIR):
            shutil.rmtree(XausProvider.CACHE_DIR, ignore_errors=True)
        # The shared live-stream hub holds an asyncio task bound to a test loop;
        # drop it so a later test starts from a clean, disconnected stream.
        SiftingProvider.reset_stream_hubs()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _reset_last_valid_ticks():
    """Keep the retained last-valid-tick store from leaking across tests.

    A stream tick recorded by one test must not feed a later test's status or
    instrument rows (the app-level store is intentionally shared in production).
    """
    from app import main

    main._last_ticks.clear()
    yield
    main._last_ticks.clear()
