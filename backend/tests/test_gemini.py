"""Unit and integration tests for Google Gemini AI provider integration."""
from __future__ import annotations

import json
import sys
import time
import types
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
import pytest

from app.main import app
from app import main
from app.ai_config import (
    get_ai_status,
    active_ai_config,
    classify_gemini_error,
    verify_gemini_configuration,
)

client = TestClient(app)


def _install_mock_gemini(monkeypatch, responder):
    """Install a small SDK double and return the captured generation requests."""
    captured = []

    class MockModels:
        def generate_content(self, model=None, contents=None, config=None):
            call = {"model": model, "contents": contents, "config": config}
            captured.append(call)
            result = responder(call)
            if isinstance(result, Exception):
                raise result
            return types.SimpleNamespace(text=result)

    class MockClient:
        def __init__(self, api_key=None):
            self.models = MockModels()

    mock_genai = types.ModuleType("google.genai")
    mock_genai.Client = MockClient
    mock_types = types.ModuleType("google.genai.types")
    mock_types.GenerateContentConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
    mock_types.Content = lambda role, parts: types.SimpleNamespace(role=role, parts=parts)
    mock_types.Part = types.SimpleNamespace(from_text=lambda text: types.SimpleNamespace(text=text))
    mock_genai.types = mock_types
    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)
    return captured


def test_gemini_config_defaults_and_env(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    provider, key, model = active_ai_config()
    assert provider == "gemini"
    assert key is None
    assert model == "gemini-flash-latest"

    status = get_ai_status()
    assert status["provider"] == "gemini"
    assert status["configured"] is False
    assert status["verification_status"] == "NOT_CONFIGURED"
    assert "api_key" not in status and "GEMINI_API_KEY" not in status


def test_gemini_configured_connecting_status(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-secret-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")

    status = get_ai_status()
    assert status["configured"] is True
    assert status["model_configured"] is True
    assert status["verified"] is False
    assert status["verification_status"] == "CONNECTING"
    assert "test-gemini-secret-key" not in json.dumps(status)


def test_gemini_config_strips_quotes(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", '"AIzaSyCleanTestKey"')
    monkeypatch.setenv("GEMINI_MODEL", "'gemini-flash-latest'")

    provider, key, model = active_ai_config()
    assert provider == "gemini"
    assert key == "AIzaSyCleanTestKey"
    assert model == "gemini-flash-latest"


def test_gemini_verification_success(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    mock_client_cls = MagicMock()
    mock_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "OK"
    mock_instance.models.generate_content.return_value = mock_response
    mock_client_cls.return_value = mock_instance

    mock_genai = types.ModuleType("google.genai")
    mock_genai.Client = mock_client_cls
    mock_types = types.ModuleType("google.genai.types")
    mock_types.GenerateContentConfig = MagicMock()
    mock_types.Content = MagicMock()
    mock_types.Part = MagicMock()
    mock_genai.types = mock_types

    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    # The parent `google` package caches the `genai` submodule attribute, so
    # `from google import genai` / `import google.genai` still resolve the real
    # SDK unless the attribute is patched too.
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)

    res = client.post("/api/v1/ai/verify")
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "gemini"
    assert data["verified"] is True
    assert data["verification_status"] == "VERIFIED"
    assert "test-gemini-key" not in res.text


def test_gemini_error_classification_invalid_key():
    class MockAuthError(Exception):
        code = 401
        status = "UNAUTHENTICATED"
        message = "API key not valid. Please pass a valid API key."
        details = {}

    status, msg = classify_gemini_error(MockAuthError())
    assert status == "INVALID_KEY"
    assert "verify gemini_api_key" in msg.lower()

    class MockPermError(Exception):
        code = 403
        status = "PERMISSION_DENIED"
        message = "Permission denied on resource."
        details = {}

    status, msg = classify_gemini_error(MockPermError())
    assert status == "INVALID_KEY"


def test_gemini_error_classification_model_unavailable():
    class MockNotFoundError(Exception):
        code = 404
        status = "NOT_FOUND"
        message = "models/gemini-unsupported-model is not found"
        details = {}

    status, msg = classify_gemini_error(MockNotFoundError())
    assert status == "MODEL_UNAVAILABLE"
    assert "gemini_model" in msg.lower()


def test_gemini_error_classification_rate_limit_vs_quota():
    # Regular rate limit (requests per minute)
    class MockRateLimitError(Exception):
        code = 429
        status = "RESOURCE_EXHAUSTED"
        message = "Rate limit exceeded. Please wait before retrying."
        details = {"error": {"message": "Rate limit exceeded"}}

    status, msg = classify_gemini_error(MockRateLimitError())
    assert status == "RATE_LIMITED"
    assert "rate limit" in msg.lower()

    # Quota exhaustion
    class MockQuotaExceededError(Exception):
        code = 429
        status = "RESOURCE_EXHAUSTED"
        message = "You have exceeded your current quota, please check your plan and billing details."
        details = {"error": {"message": "Quota exceeded"}}

    status, msg = classify_gemini_error(MockQuotaExceededError())
    assert status == "QUOTA_EXCEEDED"
    assert "quota" in msg.lower()


def test_gemini_chat_request_flow(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    captured_calls = []

    class MockClient:
        def __init__(self, api_key=None):
            assert api_key == "test-gemini-key"
            self.models = MagicMock()
            def mock_gen(*args, **kwargs):
                captured_calls.append(kwargs)
                resp = MagicMock()
                resp.text = "Gemini analysis: EUR/USD is currently forming an order block."
                return resp
            self.models.generate_content.side_effect = mock_gen

    mock_genai = types.ModuleType("google.genai")
    mock_genai.Client = MockClient
    mock_types = types.ModuleType("google.genai.types")
    mock_types.GenerateContentConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
    mock_types.Content = lambda role, parts: types.SimpleNamespace(role=role, parts=parts)
    mock_types.Part = types.SimpleNamespace(from_text=lambda text: types.SimpleNamespace(text=text))
    mock_genai.types = mock_types

    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    # The parent `google` package caches the `genai` submodule attribute, so
    # `from google import genai` / `import google.genai` still resolve the real
    # SDK unless the attribute is patched too.
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)

    res = client.post(
        "/api/v1/ai/chat",
        json={"message": "Analyze XAUUSD setup", "symbol": "XAUUSD", "timeframe": "15m", "include_market_context": True},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["provider"] == "Gemini"
    assert "order block" in data["reply"]
    assert len(captured_calls) == 1
    # Check that market context was passed
    call_kwargs = captured_calls[0]
    contents = call_kwargs["contents"]
    assert any("XAUUSD" in getattr(p, "text", "") for c in contents for p in getattr(c, "parts", []))


def test_chat_routes_simple_and_educational_questions_without_chart_data(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(main, "_build_market_context", lambda *_: pytest.fail("chart context should not be fetched"))

    def responder(call):
        prompt = call["contents"][-1].parts[0].text
        if "Request type: greeting" in prompt:
            return "Hi! What would you like to know about trading?"
        if "Request type: education" in prompt:
            return "An FVG is a three-candle price imbalance where the first and third candles do not overlap." 
        return "Unexpected request"

    captured = _install_mock_gemini(monkeypatch, responder)
    hello = client.post("/api/v1/ai/chat", json={"message": "Hi"}).json()
    fvg = client.post(
        "/api/v1/ai/chat",
        json={"message": "What is an FVG?", "history": [
            {"role": "user", "text": "Hi"},
            {"role": "assistant", "text": hello["reply"]},
        ]},
    ).json()

    assert hello["intent"] == "greeting" and hello["context_included"] is False
    assert hello["reply"].startswith("Hi!")
    assert fvg["intent"] == "education" and fvg["context_included"] is False
    assert "three-candle" in fvg["reply"]
    assert len(captured) == 2
    for call in captured:
        assert call["config"].system_instruction == main.SYSTEM_PROMPT
        assert "MARKET STRUCTURE;" not in call["config"].system_instruction


def test_market_status_uses_session_facts_without_chart_report(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    session = {
        "symbol": "XAUUSD", "status": "CLOSED", "status_label": "CLOSED",
        "schedule_verified": True, "session_source": "documented",
        "reason": "The daily maintenance break is in progress.",
        "venue_timezone": "America/New_York", "next_open": "2026-10-26T22:00:00Z",
    }
    monkeypatch.setattr(main, "market_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(main, "_provider_market_session", lambda _symbol: None)
    monkeypatch.setattr(main, "_build_market_context", lambda *_: pytest.fail("full chart snapshot should not be fetched"))
    captured = _install_mock_gemini(
        monkeypatch,
        lambda call: "XAU/USD is closed for the documented daily maintenance break. The next reference-feed open is shown in the session data.",
    )

    response = client.post("/api/v1/ai/chat", json={"message": "Is the market open or closed?"})
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "market_status"
    assert body["market_session"]["status"] == "CLOSED"
    assert body["context"]["kind"] == "market_session"
    assert "candle_freshness" in body["context"]["market_data"]
    prompt = captured[0]["contents"][-1].parts[0].text
    assert '"status":"CLOSED"' in prompt
    assert "recent_candles" not in prompt and "order_blocks" not in prompt
    assert "XAU/USD is closed" in body["reply"]


def test_current_price_routes_a_real_quote_and_marks_demo_or_stale_data(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    quote = {
        "symbol": "XAUUSD", "price": 2345.67,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": "XAUS", "price_source": "xaus.com",
        "quote_currency": "USD", "unit": "per troy ounce", "data_status": "LIVE",
    }
    monkeypatch.setattr(main.market_data, "get_latest_quote", lambda *_args, **_kwargs: quote)
    monkeypatch.setattr(main, "_connection_status", lambda _symbol: "CONNECTED")
    captured = _install_mock_gemini(monkeypatch, lambda _call: "The latest XAU/USD quote is $2,345.67 per troy ounce, from XAUS; it is fresh.")

    response = client.post("/api/v1/ai/chat", json={"message": "What is the current gold price?"})
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "current_price"
    assert body["context"]["quote"]["price"] == 2345.67
    assert body["context"]["quote_is_current_live"] is True
    assert body["context"]["quote_currency"] == "USD"
    assert "2345.67" in captured[0]["contents"][-1].parts[0].text


def test_chart_and_buy_questions_get_selected_context_and_preserve_stale_warning(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    context = {
        "instrument": "XAUUSD", "timeframe": "5m", "provider": "XAUS",
        "data_status": "LIVE", "data_freshness": {"quote_status": "FRESH", "candle_status": "STALE"},
        "strategy": {"status": "DATA_STALE", "buy_setup": {"status": "DATA_STALE"}, "sell_setup": {"status": "DATA_STALE"}},
        "recent_candles": [], "indicators": {},
    }
    seen = []
    monkeypatch.setattr(main, "_build_market_context", lambda symbol, timeframe: seen.append((symbol, timeframe)) or context)

    def responder(call):
        prompt = call["contents"][-1].parts[0].text
        if "Request type: chart_analysis" in prompt:
            return "The selected XAUUSD 5m candles are stale, so I cannot make a current chart assessment."
        return "No justified BUY setup: the candle series is stale, so wait for refreshed data."

    captured = _install_mock_gemini(monkeypatch, responder)
    chart = client.post("/api/v1/ai/chat", json={"message": "Analyze the current chart", "symbol": "XAUUSD", "timeframe": "5m"})
    buy = client.post("/api/v1/ai/chat", json={"message": "Should I buy now?", "symbol": "XAUUSD", "timeframe": "5m"})

    assert chart.status_code == buy.status_code == 200
    assert chart.json()["intent"] == "chart_analysis"
    assert buy.json()["intent"] == "setup_evaluation"
    assert seen == [("XAUUSD", "5m"), ("XAUUSD", "5m")]
    assert "stale" in chart.json()["reply"].lower()
    assert "No justified BUY setup" in buy.json()["reply"]
    assert all('"candle_status":"STALE"' in c["contents"][-1].parts[0].text for c in captured)


def test_gemini_history_is_role_ordered_and_latest_question_is_not_duplicated(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    captured = _install_mock_gemini(monkeypatch, lambda _call: "Hi! What would you like to know about trading?")
    response = client.post("/api/v1/ai/chat", json={
        "message": "Hi",
        "include_market_context": False,
        "history": [
            {"role": "user", "text": "Analyze the old chart"},
            {"role": "assistant", "text": "Old report: BUY, FVG, order block"},
            {"role": "user", "text": "Hi"},
        ],
    })
    assert response.status_code == 200
    call = captured[0]
    assert [item.role for item in call["contents"]] == ["user", "model", "user"]
    assert "Old report: BUY" in call["contents"][1].parts[0].text
    assert "Latest user message (answer this message, not an earlier one):\nHi" in call["contents"][-1].parts[0].text
    assert call["config"].system_instruction == main.SYSTEM_PROMPT
    assert response.json()["reply"].startswith("Hi!")
    assert response.json()["context_included"] is False


def test_empty_chat_message_is_rejected_and_provider_failure_has_no_stale_reply(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    assert client.post("/api/v1/ai/chat", json={"message": "   "}).status_code == 422

    class InvalidCredential(Exception):
        code = 401

    captured = _install_mock_gemini(monkeypatch, lambda _call: InvalidCredential("unauthorized"))
    failed = client.post("/api/v1/ai/chat", json={
        "message": "What is the current RSI?", "include_market_context": False,
        "history": [
            {"role": "user", "text": "Analyze the chart"},
            {"role": "assistant", "text": "This is the old answer that must not be reused."},
        ],
    })
    assert failed.status_code >= 400
    assert "reply" not in failed.json()
    assert "This is the old answer" not in failed.text
    assert len(captured) == 1


def test_gemini_trade_analysis_evaluation(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    class MockClient:
        def __init__(self, api_key=None):
            self.models = MagicMock()
            resp = MagicMock()
            resp.text = "Confluence confirms the deterministic stop-loss and take-profit bounds."
            self.models.generate_content.return_value = resp

    mock_genai = types.ModuleType("google.genai")
    mock_genai.Client = MockClient
    mock_types = types.ModuleType("google.genai.types")
    mock_types.GenerateContentConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
    mock_types.Content = lambda role, parts: types.SimpleNamespace(role=role, parts=parts)
    mock_types.Part = types.SimpleNamespace(from_text=lambda text: types.SimpleNamespace(text=text))
    mock_genai.types = mock_types

    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    # The parent `google` package caches the `genai` submodule attribute, so
    # `from google import genai` / `import google.genai` still resolve the real
    # SDK unless the attribute is patched too.
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)

    res = client.get("/api/v1/analysis/evaluate?symbol=XAUUSD&timeframe=15m&side=BUY")
    assert res.status_code == 200
    data = res.json()
    assert "ai_review" in data
    assert "confluence confirms" in data["ai_review"].lower()
    # Ensure numerical levels were evaluated deterministically
    assert "entry" in data
    assert "stop_loss" in data
    assert "take_profit" in data


def test_gemini_quota_exhaustion_in_chat(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    class MockQuotaExhausted(Exception):
        code = 429
        status = "RESOURCE_EXHAUSTED"
        message = "Quota exceeded for quota metric 'Generate Content API requests' and limit 'Generate Content API requests per day'"
        details = {"error": {"message": "Quota exceeded"}}

    class MockClient:
        def __init__(self, api_key=None):
            self.models = MagicMock()
            self.models.generate_content.side_effect = MockQuotaExhausted()

    mock_genai = types.ModuleType("google.genai")
    mock_genai.Client = MockClient
    mock_types = types.ModuleType("google.genai.types")
    mock_types.GenerateContentConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
    mock_types.Content = lambda role, parts: types.SimpleNamespace(role=role, parts=parts)
    mock_types.Part = types.SimpleNamespace(from_text=lambda text: types.SimpleNamespace(text=text))
    mock_genai.types = mock_types

    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    # The parent `google` package caches the `genai` submodule attribute, so
    # `from google import genai` / `import google.genai` still resolve the real
    # SDK unless the attribute is patched too.
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)

    res = client.post("/api/v1/ai/chat", json={"message": "Hello"})
    assert res.status_code == 502
    assert "quota has been exhausted" in res.json()["error"]["message"].lower()
    status = client.get("/api/v1/ai/status").json()
    assert status["verification_status"] == "QUOTA_EXCEEDED"


def test_no_openai_calls_when_ai_provider_is_gemini(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    # Even if an OpenAI key exists in the environment
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-secret")

    # Mock OpenAI to fail if called
    mock_openai = types.ModuleType("openai")
    mock_openai.OpenAI = MagicMock(side_effect=AssertionError("OpenAI must NOT be called when AI_PROVIDER is gemini"))
    monkeypatch.setitem(sys.modules, "openai", mock_openai)

    # Mock Gemini
    mock_genai = types.ModuleType("google.genai")
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Gemini processed the request without touching OpenAI."
    mock_client.models.generate_content.return_value = mock_response
    mock_genai.Client = MagicMock(return_value=mock_client)
    mock_types = types.ModuleType("google.genai.types")
    mock_types.GenerateContentConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
    mock_types.Content = lambda role, parts: types.SimpleNamespace(role=role, parts=parts)
    mock_types.Part = types.SimpleNamespace(from_text=lambda text: types.SimpleNamespace(text=text))
    mock_genai.types = mock_types

    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    # The parent `google` package caches the `genai` submodule attribute, so
    # `from google import genai` / `import google.genai` still resolve the real
    # SDK unless the attribute is patched too.
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)

    res = client.post("/api/v1/ai/chat", json={"message": "Can I take sell trade now?", "symbol": "XAUUSD", "timeframe": "15m"})
    assert res.status_code == 200
    assert res.json()["provider"] == "Gemini"
    mock_openai.OpenAI.assert_not_called()


def test_chat_endpoint_receives_complete_trading_context(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")

    captured_prompt = []

    mock_genai = types.ModuleType("google.genai")
    mock_client = MagicMock()
    def mock_generate(**kwargs):
        for c in kwargs.get("contents", []):
            for p in getattr(c, "parts", []):
                captured_prompt.append(getattr(p, "text", ""))
        resp = MagicMock()
        resp.text = "Answer based on context."
        return resp
    mock_client.models.generate_content.side_effect = mock_generate
    mock_genai.Client = MagicMock(return_value=mock_client)
    mock_types = types.ModuleType("google.genai.types")
    mock_types.GenerateContentConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
    mock_types.Content = lambda role, parts: types.SimpleNamespace(role=role, parts=parts)
    mock_types.Part = types.SimpleNamespace(from_text=lambda text: types.SimpleNamespace(text=text))
    mock_genai.types = mock_types

    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    # The parent `google` package caches the `genai` submodule attribute, so
    # `from google import genai` / `import google.genai` still resolve the real
    # SDK unless the attribute is patched too.
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)

    res = client.post(
        "/api/v1/ai/chat",
        json={"message": "Can I take sell trade now?", "symbol": "XAUUSD", "timeframe": "15m", "include_market_context": True},
    )
    assert res.status_code == 200
    full_prompt_text = " ".join(captured_prompt)
    assert "XAUUSD" in full_prompt_text
    assert "15m" in full_prompt_text
    assert "buy_setup" in full_prompt_text
    assert "sell_setup" in full_prompt_text
    assert "vwap_status" in full_prompt_text
    assert "current_price" in full_prompt_text
    assert "data_age_seconds" in full_prompt_text


def test_chat_failure_updates_verification_status(monkeypatch):
    """A failed chat request must refresh the cached verification state so a
    previously green 'Verified' indicator does not persist after quota is
    exhausted."""
    from app.ai_config import record_verification, get_ai_status

    # Seed a stale VERIFIED state (simulating a prior successful request).
    record_verification("VERIFIED", "A real Google Gemini API request succeeded.", provider="gemini")
    assert get_ai_status()["verification_status"] == "VERIFIED"

    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    mock_genai = types.ModuleType("google.genai")
    mock_client = MagicMock()

    class QuotaError(Exception):
        code = 429
        status = "RESOURCE_EXHAUSTED"
        message = "Quota exceeded"

    mock_client.models.generate_content.side_effect = QuotaError()
    mock_genai.Client = MagicMock(return_value=mock_client)
    mock_types = types.ModuleType("google.genai.types")
    mock_types.GenerateContentConfig = lambda **kw: types.SimpleNamespace(**kw)
    mock_types.Content = lambda role, parts: types.SimpleNamespace(role=role, parts=parts)
    mock_types.Part = types.SimpleNamespace(from_text=lambda **kw: types.SimpleNamespace(text=kw.get("text", "")))
    mock_genai.types = mock_types

    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)

    res = client.post(
        "/api/v1/ai/chat",
        json={"message": "Hi", "symbol": "XAUUSD", "timeframe": "15m", "include_market_context": False, "history": []},
    )
    assert res.status_code == 502
    assert "quota" in res.json()["error"]["message"].lower()
    # The cached verification state must now reflect the failure, not the stale success.
    assert get_ai_status()["verification_status"] == "QUOTA_EXCEEDED"


def test_verified_status_expires_after_ttl(monkeypatch):
    """VERIFIED reflects a real request, not a permanent health guarantee. After
    the TTL elapses the status must degrade so a subsequent failure is visible."""
    from app import ai_config as ac
    from app.ai_config import record_verification, get_ai_status

    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    record_verification("VERIFIED", "A real Google Gemini API request succeeded.", provider="gemini")
    assert get_ai_status()["verification_status"] == "VERIFIED"

    # Backdate the verified timestamp past the TTL.
    with ac._verification_lock:
        ac._verification["verified_expires_at"] = time.time() - 1

    status = get_ai_status()
    assert status["verification_status"] == "CONNECTING"
    assert status["verified"] is False
    assert "expired" in status["message"].lower()


def test_status_contract_distinguishes_error_categories(monkeypatch):
    """The status endpoint must not collapse every failure into one label."""
    from app.ai_config import record_verification, get_ai_status

    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    for label, expected in (
        ("INVALID_KEY", "INVALID_KEY"),
        ("MODEL_UNAVAILABLE", "MODEL_UNAVAILABLE"),
        ("QUOTA_EXCEEDED", "QUOTA_EXCEEDED"),
        ("RATE_LIMITED", "RATE_LIMITED"),
        ("ERROR", "ERROR"),
    ):
        record_verification(label, f"{label} detail", error_code=label, provider="gemini")
        assert get_ai_status()["verification_status"] == expected, label


# ---------------------------------------------------------------------------
# Centralized generation service: quota parsing, retry bounds, and fallback.
# All provider calls are mocked; no real Gemini quota is consumed.
# ---------------------------------------------------------------------------


def _install_fake_genai(monkeypatch, handler):
    """Install a fake google.genai whose Client delegates to `handler`."""
    class FakeModels:
        def generate_content(self, model=None, contents=None, config=None):
            return handler(model, contents, config)

    class FakeClient:
        def __init__(self, api_key=None):
            self.models = FakeModels()

    mock_genai = types.ModuleType("google.genai")
    mock_genai.Client = FakeClient
    mock_types = types.ModuleType("google.genai.types")
    mock_types.GenerateContentConfig = lambda **kw: types.SimpleNamespace(**kw)
    mock_types.Content = lambda role, parts: types.SimpleNamespace(role=role, parts=parts)
    mock_types.Part = types.SimpleNamespace(from_text=lambda text: types.SimpleNamespace(text=text))
    mock_genai.types = mock_types

    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)


def _ok_response(text="OK"):
    return types.SimpleNamespace(text=text, candidates=[types.SimpleNamespace(finish_reason="STOP")])


def _empty_response():
    return types.SimpleNamespace(text="", candidates=[types.SimpleNamespace(finish_reason="MAX_TOKENS")])


def _daily_quota_error(model="gemini-flash-latest"):
    class QuotaExceededError(Exception):
        code = 429
        status = "RESOURCE_EXHAUSTED"
        message = (
            "You exceeded your current quota, please check your plan and billing details. "
            f"Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: {model}"
        )
        details = {
            "error": {
                "code": 429,
                "message": "quota",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                     "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                                     "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                     "quotaDimensions": {"location": "global", "model": model},
                                     "quotaValue": "20"}]},
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "58311s"},
                ],
            }
        }

    return QuotaExceededError()


def _per_minute_rate_error():
    class RateError(Exception):
        code = 429
        status = "RESOURCE_EXHAUSTED"
        message = "Rate limit exceeded. Quota exceeded for metric: generate_content_free_tier_requests"
        details = {
            "error": {"code": 429, "message": "rate", "status": "RESOURCE_EXHAUSTED", "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                                 "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                                 "quotaDimensions": {"location": "global", "model": "gemini-flash-latest"},
                                 "quotaValue": "15"}]},
            ]},
        }

    return RateError()


def _server_error():
    class ServerError(Exception):
        code = 503
        status = "UNAVAILABLE"
        message = "The model is overloaded. Please try again later."

    return ServerError()


def test_parse_gemini_daily_quota_details():
    from app.ai_config import parse_gemini_error_details

    info = parse_gemini_error_details(_daily_quota_error("gemini-3.8-flash"))
    assert info["quota_id"] == "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    assert info["quota_value"] == "20"
    assert info["model"] == "gemini-3.8-flash"
    assert info["retry_delay_seconds"] == 58311.0
    assert info["is_daily"] is True


def test_structured_daily_quota_is_quota_exhausted_and_minute_is_rate_limited():
    status, msg = classify_gemini_error(_daily_quota_error())
    assert status == "QUOTA_EXCEEDED"
    assert "quota has been exhausted" in msg.lower()

    status, msg = classify_gemini_error(_per_minute_rate_error())
    assert status == "RATE_LIMITED"
    assert "rate limit" in msg.lower()


def test_service_does_not_retry_or_fallback_on_daily_quota(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)

    calls: list[str] = []
    slept: list[float] = []

    def handler(model, contents, config):
        calls.append(model)
        raise _daily_quota_error(model)

    _install_fake_genai(monkeypatch, handler)

    from app.ai_config import gemini_generate_content, AIServiceError

    with pytest.raises(AIServiceError) as excinfo:
        gemini_generate_content("hi", types.SimpleNamespace(), models=["gemini-flash-latest"], sleep=slept.append)

    assert excinfo.value.status == "QUOTA_EXCEEDED"
    assert excinfo.value.retryable is False
    assert excinfo.value.retry_after_seconds == 58311.0
    assert calls == ["gemini-flash-latest"]  # exactly one attempt
    assert slept == []  # never waits on a daily quota


def test_service_falls_back_when_primary_quota_exhausted(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    calls: list[str] = []

    def handler(model, contents, config):
        calls.append(model)
        if model == "gemini-flash-latest":
            raise _daily_quota_error(model)
        return _ok_response("fallback answer")

    _install_fake_genai(monkeypatch, handler)

    from app.ai_config import gemini_generate_content

    text, model = gemini_generate_content(
        "hi", types.SimpleNamespace(), models=["gemini-flash-latest", "gemini-3.1-flash-lite"], sleep=lambda _s: None
    )
    assert text == "fallback answer"
    assert model == "gemini-3.1-flash-lite"
    assert calls == ["gemini-flash-latest", "gemini-3.1-flash-lite"]


def test_service_stops_when_both_models_quota_exhausted(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    calls: list[str] = []

    def handler(model, contents, config):
        calls.append(model)
        raise _daily_quota_error(model)

    _install_fake_genai(monkeypatch, handler)

    from app.ai_config import gemini_generate_content, AIServiceError

    with pytest.raises(AIServiceError) as excinfo:
        gemini_generate_content(
            "hi", types.SimpleNamespace(), models=["gemini-flash-latest", "gemini-3.1-flash-lite"], sleep=lambda _s: None
        )
    assert excinfo.value.status == "QUOTA_EXCEEDED"
    assert calls == ["gemini-flash-latest", "gemini-3.1-flash-lite"]  # one try each, no loop


def test_service_retries_transient_5xx_then_succeeds(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    calls: list[str] = []
    slept: list[float] = []

    def handler(model, contents, config):
        calls.append(model)
        if len(calls) == 1:
            raise _server_error()
        return _ok_response("recovered")

    _install_fake_genai(monkeypatch, handler)

    from app.ai_config import gemini_generate_content

    text, model = gemini_generate_content(
        "hi", types.SimpleNamespace(), models=["gemini-flash-latest"], max_attempts=3, sleep=slept.append
    )
    assert text == "recovered"
    assert calls == ["gemini-flash-latest", "gemini-flash-latest"]
    assert len(slept) == 1 and slept[0] > 0  # bounded backoff


def test_service_does_not_fallback_on_invalid_key(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    calls: list[str] = []

    class AuthError(Exception):
        code = 401
        status = "UNAUTHENTICATED"
        message = "API key not valid. Please pass a valid API key."

    def handler(model, contents, config):
        calls.append(model)
        raise AuthError()

    _install_fake_genai(monkeypatch, handler)

    from app.ai_config import gemini_generate_content, AIServiceError

    with pytest.raises(AIServiceError) as excinfo:
        gemini_generate_content("hi", types.SimpleNamespace(), models=["primary", "fallback"], sleep=lambda _s: None)
    assert excinfo.value.status == "INVALID_KEY"
    assert calls == ["primary"]  # a different model cannot fix a bad key


def test_service_treats_empty_thinking_response_as_error(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    _install_fake_genai(monkeypatch, lambda model, contents, config: _empty_response())

    from app.ai_config import gemini_generate_content, AIServiceError

    with pytest.raises(AIServiceError) as excinfo:
        gemini_generate_content("hi", types.SimpleNamespace(), models=["m"], sleep=lambda _s: None)
    assert excinfo.value.status == "EMPTY_RESPONSE"


def test_verify_accepts_empty_text_but_valid_candidate(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)

    _install_fake_genai(monkeypatch, lambda model, contents, config: _empty_response())

    res = client.post("/api/v1/ai/verify")
    assert res.status_code == 200
    assert res.json()["verified"] is True


def test_chat_quota_error_exposes_headers_and_sanitized_message(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)

    _install_fake_genai(monkeypatch, lambda model, contents, config: (_ for _ in ()).throw(_daily_quota_error(model)))

    res = client.post("/api/v1/ai/chat", json={"message": "Hi", "symbol": "XAUUSD", "timeframe": "15m", "include_market_context": False})
    assert res.status_code == 502
    assert res.headers.get("X-AI-Error-Code") == "GEMINI_QUOTA_EXHAUSTED"
    assert res.headers.get("Retry-After") == "58311"
    body = res.json()
    assert body["success"] is False
    assert body["error"]["retryable"] is False
    assert body["error"]["retry_after_seconds"] == 58311
    detail = body["error"]["message"].lower()
    assert "quota has been exhausted" in detail
    # No credential or raw stack trace leaks into the response body.
    assert "test-key" not in res.text
    status = client.get("/api/v1/ai/status").json()
    assert status["verification_status"] == "QUOTA_EXCEEDED"
    assert status["retry_after_seconds"] == 58311


def test_chat_uses_fallback_model_and_reports_success(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite")

    def handler(model, contents, config):
        if model == "gemini-flash-latest":
            raise _daily_quota_error(model)
        return _ok_response("fallback chat reply")

    _install_fake_genai(monkeypatch, handler)

    res = client.post("/api/v1/ai/chat", json={"message": "Hi", "symbol": "XAUUSD", "timeframe": "15m", "include_market_context": False})
    assert res.status_code == 200
    assert res.json()["reply"] == "fallback chat reply"
    status = client.get("/api/v1/ai/status").json()
    assert status["verification_status"] == "VERIFIED"


# ---------------------------------------------------------------------------
# PHASE 2: honest, separated AI status signals.
# ---------------------------------------------------------------------------


def test_status_separates_configuration_connectivity_generation_and_quota(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    status = get_ai_status()
    checks = status["checks"]
    assert checks["configuration"]["present"] is True
    assert checks["configuration"]["model"] == "gemini-2.5-flash"
    assert checks["provider_connectivity"]["verified"] is False
    assert checks["generation"]["recent"] is False
    assert checks["quota"]["exhausted"] is False
    # Configuration present but not yet verified must not read as "Verified".
    assert status["state"] == "UNVERIFIED"
    assert status["label"] == "Not verified"


def test_status_reports_authenticated_after_recent_success(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    from app.ai_config import record_verification

    record_verification("VERIFIED", "A real Google Gemini API request succeeded.", provider="gemini")
    status = get_ai_status()
    assert status["state"] == "AUTHENTICATED"
    assert status["label"] == "Verified"
    assert status["checks"]["generation"]["recent"] is True
    assert status["checks"]["provider_connectivity"]["verified"] is True


def test_status_degrades_after_ttl_but_records_recent_success(monkeypatch):
    from app import ai_config as ac
    from app.ai_config import record_verification

    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    record_verification("VERIFIED", "succeeded", provider="gemini")
    with ac._verification_lock:
        ac._verification["verified_expires_at"] = time.time() - 1

    status = get_ai_status()
    # Verification has expired, so it is not "Verified"; but a real request did
    # succeed recently, so it is not misleadingly labelled "Not verified".
    assert status["verified"] is False
    assert status["state"] == "DEGRADED"
    assert status["label"].startswith("Connected")
    assert status["checks"]["generation"]["recent"] is True


def test_status_quota_takes_priority_over_prior_success(monkeypatch):
    from app.ai_config import record_verification

    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    record_verification("VERIFIED", "succeeded", provider="gemini")
    record_verification("QUOTA_EXCEEDED", "quota exhausted", error_code="QUOTA_EXCEEDED", provider="gemini", retry_after_seconds=58311.0)

    status = get_ai_status()
    assert status["state"] == "QUOTA_EXCEEDED"
    assert status["label"] == "Quota exhausted"
    assert status["checks"]["quota"]["exhausted"] is True
    assert status["checks"]["quota"]["retry_after_seconds"] == 58311.0
    # A prior success must not mask the current exhaustion.
    assert status["verified"] is False


# ---------------------------------------------------------------------------
# PHASE 6 (timeout / 5xx / network / invalid-request classification) and
# PHASE 2/3 (finite HTTP timeout + wall-clock deadline). All provider calls
# are mocked; no real Gemini quota is consumed.
# ---------------------------------------------------------------------------


def _timeout_error(code=504, name="ServerError"):
    """A gateway/timeout style failure raised by the SDK or a proxy."""
    cls = type(name, (Exception,), {"code": code, "status": "DEADLINE_EXCEEDED", "message": "The request timed out."})
    return cls()


def _network_error(name="ConnectError"):
    cls = type(name, (Exception,), {"code": None, "status": "", "message": "Connection refused."})
    return cls()


def _invalid_request_error():
    class InvalidRequest(Exception):
        code = 400
        status = "INVALID_ARGUMENT"
        message = "Request contains an invalid argument."

    return InvalidRequest()


def test_classify_separates_timeout_from_other_5xx():
    from app.ai_config import classify_gemini_error

    # A 504 gateway timeout must be a TIMEOUT, not a generic provider 5xx.
    status, msg = classify_gemini_error(_timeout_error(code=504))
    assert status == "TIMEOUT"
    assert "timeout" in msg.lower()

    # A plain 503 is a provider-unavailable error, not a timeout.
    status, msg = classify_gemini_error(_server_error())
    assert status == "PROVIDER_UNAVAILABLE"
    assert "5xx" not in msg.lower() and "temporarily unavailable" in msg.lower()


def test_classify_network_and_invalid_request_are_distinct():
    from app.ai_config import classify_gemini_error

    status, _ = classify_gemini_error(_network_error())
    assert status == "NETWORK_ERROR"

    status, msg = classify_gemini_error(_invalid_request_error())
    assert status == "INVALID_REQUEST"
    assert "invalid argument" in msg.lower()


def test_error_codes_are_distinct_for_each_category():
    from app.ai_config import AIServiceError

    codes = {
        status: AIServiceError(status, "x").as_error_payload()["error"]["code"]
        for status in ("QUOTA_EXCEEDED", "RATE_LIMITED", "INVALID_KEY", "MODEL_UNAVAILABLE", "INVALID_REQUEST", "TIMEOUT", "PROVIDER_UNAVAILABLE", "NETWORK_ERROR", "EMPTY_RESPONSE", "ERROR")
    }
    assert len(set(codes.values())) == len(codes)
    assert codes["PROVIDER_UNAVAILABLE"] == "AI_PROVIDER_UNAVAILABLE"
    assert codes["TIMEOUT"] == "AI_TIMEOUT"


def test_timeout_and_invalid_request_are_not_retried_or_faked(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)

    # An invalid request is not retryable and does not fall back to another model.
    calls: list[str] = []

    def handler_invalid(model, contents, config):
        calls.append(model)
        raise _invalid_request_error()

    _install_fake_genai(monkeypatch, handler_invalid)

    from app.ai_config import gemini_generate_content, AIServiceError

    with pytest.raises(AIServiceError) as excinfo:
        gemini_generate_content("hi", types.SimpleNamespace(), models=["gemini-flash-latest"], sleep=lambda _s: None)
    assert excinfo.value.status == "INVALID_REQUEST"
    assert excinfo.value.retryable is False
    assert calls == ["gemini-flash-latest"]


def test_provider_5xx_is_retryable_then_reported_as_unavailable(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)

    calls: list[str] = []

    def handler(model, contents, config):
        calls.append(model)
        raise _server_error()

    _install_fake_genai(monkeypatch, handler)

    from app.ai_config import gemini_generate_content, AIServiceError

    with pytest.raises(AIServiceError) as excinfo:
        gemini_generate_content(
            "hi", types.SimpleNamespace(), models=["gemini-flash-latest"], max_attempts=2, sleep=lambda _s: None
        )
    assert excinfo.value.status == "PROVIDER_UNAVAILABLE"
    assert excinfo.value.retryable is True
    assert len(calls) == 2  # bounded retries, not an infinite loop


def test_wall_clock_deadline_aborts_before_any_request(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    calls: list[str] = []

    def handler(model, contents, config):
        calls.append(model)
        return _ok_response()

    _install_fake_genai(monkeypatch, handler)

    from app.ai_config import gemini_generate_content, AIServiceError

    with pytest.raises(AIServiceError) as excinfo:
        gemini_generate_content(
            "hi", types.SimpleNamespace(), models=["a", "b"], total_deadline=-1.0, sleep=lambda _s: None
        )
    assert excinfo.value.status == "TIMEOUT"
    assert excinfo.value.http_status == 504
    assert calls == []  # the budget was already spent, so nothing is sent


def test_deadline_caps_each_http_attempt_to_the_remaining_budget(monkeypatch):
    """The wall-clock budget must be handed down to every HTTP attempt so a
    stalled request cannot be retried after the deadline has passed."""
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    captured: list[int] = []

    class FakeHttpOptions:
        def __init__(self, timeout=None):
            captured.append(timeout)

    class FakeModels:
        def generate_content(self, model=None, contents=None, config=None):
            raise _timeout_error(code=504)

    class FakeClient:
        def __init__(self, api_key=None, http_options=None):
            self.models = FakeModels()

    mock_genai = types.ModuleType("google.genai")
    mock_genai.Client = FakeClient
    mock_types = types.ModuleType("google.genai.types")
    mock_types.HttpOptions = FakeHttpOptions
    mock_genai.types = mock_types
    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)

    from app.ai_config import gemini_generate_content, AIServiceError

    with pytest.raises(AIServiceError) as excinfo:
        gemini_generate_content(
            "hi",
            types.SimpleNamespace(),
            models=["gemini-flash-latest"],
            max_attempts=3,
            base_delay=5.0,
            max_delay=20.0,
            max_total_wait=100.0,
            request_timeout_ms=600000,  # far above the deadline
            total_deadline=0.3,
            sleep=lambda _s: None,
        )

    assert excinfo.value.status == "TIMEOUT"
    assert captured  # a timeout was configured on the client
    # No single attempt may ask the SDK for more time than the whole budget.
    assert max(captured) <= 300
    # Attempts are bounded, never an unbounded retry loop.
    assert 0 < len(captured) <= 3


def _rate_limited_with_retry(retry_seconds=30.0):
    class RateLimited(Exception):
        code = 429
        status = "RESOURCE_EXHAUSTED"
        message = "Rate limit exceeded. Please retry later."
        details = {
            "error": {
                "code": 429,
                "message": "rate",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{int(retry_seconds)}s"},
                ],
            }
        }

    return RateLimited()


def test_retry_after_longer_than_budget_is_not_retried_early(monkeypatch):
    """A Retry-After beyond the interactive budget must produce a clear rate-limit
    error instead of hammering the provider early."""
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    calls: list[str] = []
    slept: list[float] = []

    def handler(model, contents, config):
        calls.append(model)
        raise _rate_limited_with_retry(30.0)

    _install_fake_genai(monkeypatch, handler)

    from app.ai_config import gemini_generate_content, AIServiceError

    with pytest.raises(AIServiceError) as excinfo:
        gemini_generate_content(
            "hi", types.SimpleNamespace(), models=["m"], max_attempts=3, max_total_wait=8.0, sleep=slept.append
        )
    assert excinfo.value.status == "RATE_LIMITED"
    assert excinfo.value.retry_after_seconds == 30.0
    assert calls == ["m"]  # no early retry
    assert slept == []  # never waited


def test_retry_after_within_budget_is_honored_then_succeeds(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    calls: list[str] = []
    slept: list[float] = []
    state = {"failed": False}

    def handler(model, contents, config):
        calls.append(model)
        if not state["failed"]:
            state["failed"] = True
            raise _rate_limited_with_retry(2.0)
        return _ok_response("recovered")

    _install_fake_genai(monkeypatch, handler)

    from app.ai_config import gemini_generate_content

    text, model = gemini_generate_content(
        "hi", types.SimpleNamespace(), models=["m"], max_attempts=2, max_total_wait=8.0, sleep=slept.append
    )
    assert text == "recovered" and model == "m"
    assert calls == ["m", "m"]
    assert len(slept) == 1 and 2.0 <= slept[0] < 2.2  # waited the provider-suggested delay, not the default backoff


def test_client_is_created_with_a_finite_http_timeout(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    captured: list[dict] = []

    class FakeHttpOptions:
        def __init__(self, timeout=None):
            self.timeout = timeout

    class FakeModels:
        def generate_content(self, model=None, contents=None, config=None):
            return _ok_response("ok")

    class FakeClient:
        def __init__(self, api_key=None, http_options=None):
            captured.append({"api_key": api_key, "http_options": http_options})
            self.models = FakeModels()

    mock_genai = types.ModuleType("google.genai")
    mock_genai.Client = FakeClient
    mock_types = types.ModuleType("google.genai.types")
    mock_types.HttpOptions = FakeHttpOptions
    mock_types.GenerateContentConfig = lambda **kw: types.SimpleNamespace(**kw)
    mock_genai.types = mock_types
    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    monkeypatch.setattr(__import__("google"), "genai", mock_genai, raising=False)

    from app.ai_config import gemini_generate_content

    gemini_generate_content("hi", types.SimpleNamespace(), models=["m"], request_timeout_ms=12345)
    assert len(captured) == 1
    http_options = captured[0]["http_options"]
    assert http_options is not None
    assert http_options.timeout == 12345


def test_timeout_env_settings_are_clamped_and_default(monkeypatch):
    from app.ai_config import gemini_request_timeout_ms, gemini_total_deadline_seconds

    monkeypatch.delenv("GEMINI_REQUEST_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("GEMINI_TOTAL_DEADLINE_SECONDS", raising=False)
    assert gemini_request_timeout_ms() == 20000
    assert gemini_total_deadline_seconds() == 30.0

    # Out-of-range and non-numeric values are clamped or ignored, never unbounded.
    monkeypatch.setenv("GEMINI_REQUEST_TIMEOUT_MS", "999999999")
    assert gemini_request_timeout_ms() == 600000
    monkeypatch.setenv("GEMINI_TOTAL_DEADLINE_SECONDS", "0")
    assert gemini_total_deadline_seconds() == 5.0
    monkeypatch.setenv("GEMINI_TOTAL_DEADLINE_SECONDS", "not-a-number")
    assert gemini_total_deadline_seconds() == 30.0


def test_chat_returns_structured_retryable_provider_error(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)

    _install_fake_genai(monkeypatch, lambda model, contents, config: (_ for _ in ()).throw(_server_error()))

    res = client.post("/api/v1/ai/chat", json={"message": "Hi", "symbol": "XAUUSD", "timeframe": "15m", "include_market_context": False})
    assert res.status_code == 502
    assert res.headers.get("X-AI-Error-Code") == "AI_PROVIDER_UNAVAILABLE"
    body = res.json()
    assert body["success"] is False
    assert body["error"]["retryable"] is True
    assert "temporarily unavailable" in body["error"]["message"].lower()
    # The misleading generic "5xx" wording must be gone and no secret must leak.
    assert "temporary provider error (5xx)" not in body["error"]["message"].lower()
    assert "test-key" not in res.text


def test_chat_invalid_request_is_not_reported_as_provider_error(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)

    _install_fake_genai(monkeypatch, lambda model, contents, config: (_ for _ in ()).throw(_invalid_request_error()))

    res = client.post("/api/v1/ai/chat", json={"message": "Hi", "symbol": "XAUUSD", "timeframe": "15m", "include_market_context": False})
    assert res.status_code == 502
    assert res.headers.get("X-AI-Error-Code") == "AI_INVALID_REQUEST"
    assert res.json()["error"]["retryable"] is False


def test_chat_timeout_is_reported_with_a_timeout_message(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)

    # Simulate an expired wall-clock budget rather than a live provider call.
    from app import ai_config as ac

    monkeypatch.setattr(ac, "gemini_total_deadline_seconds", lambda: -1.0)
    _install_fake_genai(monkeypatch, lambda model, contents, config: _ok_response("unused"))

    res = client.post("/api/v1/ai/chat", json={"message": "Hi", "symbol": "XAUUSD", "timeframe": "15m", "include_market_context": False})
    assert res.status_code == 504
    assert res.headers.get("X-AI-Error-Code") == "AI_TIMEOUT"
    assert res.json()["error"]["retryable"] is True
    assert "time budget" in res.json()["error"]["message"].lower()



# ---------------------------------------------------------------------------
# PHASE 1 (this round): canonical assistant state machine.
# ---------------------------------------------------------------------------


def test_canonical_ai_state_machine(monkeypatch):
    """Every canonical state is distinct and never conflated."""
    from app import ai_config as ac

    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("AI_VERIFICATION_LIFETIME_SECONDS", raising=False)

    # Configured but never verified -> UNVERIFIED.
    ac.reset_verification_state()
    assert ac.get_ai_status()["ai_state"] == "UNVERIFIED"

    # Missing configuration -> CONFIGURATION_ERROR.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert ac.get_ai_status()["ai_state"] == "CONFIGURATION_ERROR"
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")

    # A real success -> VERIFIED.
    ac.record_verification("VERIFIED", "succeeded", provider="gemini")
    assert ac.get_ai_status()["ai_state"] == "VERIFIED"

    # Older than the lifetime -> EXPIRED (not ERROR, not UNVERIFIED).
    with ac._verification_lock:
        ac._verification["verified_expires_at"] = time.time() - 1
    status = ac.get_ai_status()
    assert status["ai_state"] == "EXPIRED"
    assert status["ai_state_label"] == "Verification expired"
    assert status["verified"] is False

    # An in-flight real request reads as CHECKING, never EXPIRED.
    ac.set_verification_in_flight(True, "gemini")
    assert ac.get_ai_status()["ai_state"] == "CHECKING"
    ac.set_verification_in_flight(False, "gemini")

    # Quota/rate limit -> QUOTA_EXCEEDED.
    ac.record_verification("QUOTA_EXCEEDED", "quota", error_code="QUOTA_EXCEEDED", provider="gemini")
    assert ac.get_ai_status()["ai_state"] == "QUOTA_EXCEEDED"
    ac.record_verification("RATE_LIMITED", "rate", error_code="RATE_LIMITED", provider="gemini")
    assert ac.get_ai_status()["ai_state"] == "QUOTA_EXCEEDED"

    # An invalid key is invalid configuration, not a transient provider error.
    ac.record_verification("INVALID_KEY", "bad key", error_code="INVALID_KEY", provider="gemini")
    assert ac.get_ai_status()["ai_state"] == "CONFIGURATION_ERROR"

    # An unavailable model is also a configuration problem.
    ac.record_verification("MODEL_UNAVAILABLE", "no model", error_code="MODEL_UNAVAILABLE", provider="gemini")
    assert ac.get_ai_status()["ai_state"] == "CONFIGURATION_ERROR"

    # A genuine provider failure -> ERROR.
    ac.record_verification("ERROR", "boom", error_code="ERROR", provider="gemini")
    assert ac.get_ai_status()["ai_state"] == "ERROR"
    ac.record_verification("TIMEOUT", "slow", error_code="TIMEOUT", provider="gemini")
    assert ac.get_ai_status()["ai_state"] == "ERROR"

    # The lifetime is configurable and reported.
    monkeypatch.setenv("AI_VERIFICATION_LIFETIME_SECONDS", "120")
    assert ac.get_ai_status()["verification_lifetime_seconds"] == 120

    ac.reset_verification_state()


def test_ai_status_endpoint_exposes_canonical_state(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    body = client.get("/api/v1/ai/status").json()
    assert body["ai_state"] in {
        "CHECKING", "VERIFIED", "UNVERIFIED", "EXPIRED",
        "ERROR", "QUOTA_EXCEEDED", "CONFIGURATION_ERROR",
    }
    assert isinstance(body["ai_state_label"], str) and body["ai_state_label"]
    assert isinstance(body["verification_lifetime_seconds"], int)
    assert "checked_at" in body
    # The secret is never exposed.
    assert "test-gemini-key" not in str(body)


def test_successful_chat_refreshes_verification_lifetime(monkeypatch):
    """A genuinely successful provider request refreshes the verified window."""
    from app import ai_config as ac

    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    ac.reset_verification_state()
    ac.record_verification("VERIFIED", "succeeded", provider="gemini")
    with ac._verification_lock:
        ac._verification["verified_expires_at"] = time.time() - 1
    assert ac.get_ai_status()["ai_state"] == "EXPIRED"

    # A real subsequent success must move it back to VERIFIED.
    ac.record_verification("VERIFIED", "succeeded again", provider="gemini")
    status = ac.get_ai_status()
    assert status["ai_state"] == "VERIFIED"
    assert status["verified"] is True
    assert status["verified_expires_at"] > time.time()

    ac.reset_verification_state()
