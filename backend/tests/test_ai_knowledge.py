"""Tests for topic-scoped trading-knowledge retrieval."""
from __future__ import annotations

from app.ai_knowledge import (
    TOPICS,
    knowledge_for_query,
    select_topics,
    topic_notes,
)


def test_all_topics_have_title_keywords_and_notes():
    assert TOPICS
    for topic_id, topic in TOPICS.items():
        assert topic["title"], topic_id
        assert topic["keywords"], topic_id
        assert topic["notes"], topic_id


def test_smc_question_selects_smc_topic():
    topics = select_topics("What is a fair value gap and how do I use an order block?")
    assert "smart_money_concepts" in topics


def test_market_open_question_selects_timings_topic():
    topics = select_topics("Is the market open right now for NIFTY?")
    assert "market_timings" in topics


def test_rsi_question_selects_technical_analysis():
    topics = select_topics("How does RSI divergence work with MACD?")
    assert "technical_analysis" in topics


def test_risk_question_selects_risk_management():
    topics = select_topics("How should I calculate position sizing and risk per trade?")
    assert "risk_management" in topics


def test_unrelated_question_returns_no_topics():
    assert select_topics("hello there") == []


def test_knowledge_is_scoped_and_compact():
    text = knowledge_for_query("Explain liquidity sweeps and stop hunts")
    assert "Smart Money Concepts" in text
    # A scoped answer must not dump the entire encyclopedia.
    assert "Risk management" not in text


def test_topic_notes_renders_bullets():
    text = topic_notes(["candlesticks"])
    assert "Candlestick" in text
    assert text.count("- ") >= 3


def test_limit_is_respected():
    topics = select_topics("forex rsi risk market open candle", limit=2)
    assert len(topics) <= 2
