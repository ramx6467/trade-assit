"""Concise, retrievable trading knowledge for the AI assistant.

A single giant system prompt would waste tokens on every request and put
pressure on the provider quota. Instead, knowledge is grouped into topics and
only the topics relevant to a specific question are attached to that request.

This module is descriptive reference material only. It never contains live
prices, indicator values, market sessions, or trade signals: those always come
from the deterministic backend calculations.
"""
from __future__ import annotations

import re
from typing import Iterable

# Each topic is a short, factual reference note. Keywords drive retrieval.
TOPICS: dict[str, dict] = {
    "trading_fundamentals": {
        "title": "Trading fundamentals",
        "keywords": [
            "forex", "xauusd", "gold", "stocks", "indices", "futures", "options",
            "crypto", "spot", "derivatives", "bid", "ask", "spread", "slippage",
            "leverage", "margin", "market order", "limit order", "stop order",
            "stop-loss", "position sizing", "risk-to-reward", "risk reward",
        ],
        "notes": [
            "Spot markets settle the asset itself; derivatives (futures/options) derive value from an underlying and usually carry expiry, margin, and funding mechanics.",
            "Bid is the price at which you can sell, ask is the price at which you can buy; the spread is ask minus bid and is a real transaction cost.",
            "Slippage is the difference between the expected and executed price, typically worse in fast or thin markets.",
            "Leverage increases exposure per unit of margin and magnifies both gains and losses; margin is the collateral required to hold a position.",
            "Market orders prioritise execution and accept price uncertainty; limit orders control price but may not fill; stop orders trigger once a level trades.",
            "Position sizing should be derived from the account risk budget and the distance to the stop, not from a desire for a certain profit.",
            "Risk-to-reward compares potential loss (entry to stop) with potential gain (entry to target); it is a planning metric, not a probability of success.",
        ],
    },
    "candlesticks": {
        "title": "Candlestick anatomy and patterns",
        "keywords": [
            "candle", "candlestick", "wick", "body", "doji", "hammer", "shooting star",
            "engulfing", "pin bar", "morning star", "evening star", "harami",
        ],
        "notes": [
            "A candle has four values: open, high, low, close. The body spans open-to-close; the wicks/ shadows show the session's extremes.",
            "Long wicks show rejection where price was accepted only briefly; a large body shows directional conviction.",
            "Bullish/bearish engulfing: the current body fully covers the prior opposite body, suggesting a shift in control.",
            "Hammer/pin bar: long lower wick with a small body, a rejection of lower prices; shooting star is the bearish mirror.",
            "Doji: open and close are near-equal, signalling indecision rather than a directional signal on its own.",
            "Candlestick patterns gain meaning only with context: location (support/resistance, liquidity), trend, and confirmation on the next candle close.",
        ],
    },
    "technical_analysis": {
        "title": "Technical analysis and indicators",
        "keywords": [
            "support", "resistance", "trendline", "channel", "breakout", "false breakout",
            "trend", "higher high", "higher low", "lower high", "lower low",
            "ema", "sma", "moving average", "rsi", "macd", "bollinger", "atr", "adx",
            "vwap", "volume", "momentum", "volatility", "multi-timeframe", "multi timeframe",
            "overbought", "oversold", "crossover",
        ],
        "notes": [
            "Support is an area where demand previously exceeded supply; resistance is the opposite. Treat them as zones, not exact lines.",
            "A breakout is a close beyond a level; without follow-through it can be a false break that traps late entries.",
            "Trend is a sequence of higher highs and higher lows (up) or lower highs and lower lows (down); sideways price is a range.",
            "Moving averages smooth price: EMA reacts faster than SMA. Common lengths (9/20/50/100/200) are conventions, not fixed rules.",
            "RSI is a bounded momentum oscillator: high values indicate strong recent gains, not an automatic reversal. Divergence is a warning, never a guarantee.",
            "MACD compares two EMAs and plots a line, a signal line, and a histogram; crossovers and histogram slope describe momentum shifts.",
            "Bollinger Bands place a moving average between volatility envelopes; band touch is common in trends and is not by itself a reversal signal.",
            "ATR measures average range and is useful for sizing stops and comparing volatility across instruments; ADX estimates trend strength, not direction.",
            "VWAP is a volume-weighted average price. It is only meaningful when genuine traded volume exists; without reliable volume it must be reported unavailable.",
            "Volume confirms participation; rising price with falling volume is weaker evidence than rising price with rising volume.",
            "Multi-timeframe analysis aligns the higher-timeframe trend with a lower-timeframe entry; conflicting timeframes usually mean 'wait'.",
        ],
    },
    "market_structure": {
        "title": "Market structure",
        "keywords": [
            "market structure", "swing high", "swing low", "pivot", "bos",
            "break of structure", "choch", "change of character", "mss",
            "market structure shift", "internal structure", "external structure",
        ],
        "notes": [
            "Swing highs and lows are confirmed pivots; a pivot is only confirmed after later candles fail to exceed it.",
            "Break of Structure (BOS) continues the prevailing trend when price closes beyond the prior swing in the trend direction.",
            "Change of Character (CHoCH) / Market Structure Shift (MSS) is the first opposing break that questions the prior trend.",
            "Internal structure is lower-degree noise inside a swing; external structure is the higher-degree trend that matters more.",
            "Structural signals should be confirmed on candle close to reduce repainting; an unconfirmed break is only potential.",
        ],
    },
    "smart_money_concepts": {
        "title": "Smart Money Concepts (SMC)",
        "keywords": [
            "smc", "smart money", "liquidity", "bsl", "ssl", "buy-side liquidity",
            "sell-side liquidity", "equal highs", "equal lows", "liquidity sweep",
            "stop hunt", "fvg", "fair value gap", "imbalance", "order block",
            "breaker", "mitigation", "premium", "discount", "equilibrium",
            "displacement", "inducement", "dealing range", "ote", "optimal trade entry",
        ],
        "notes": [
            "Buy-side liquidity rests above highs and sell-side liquidity below lows; price often moves toward these pools.",
            "Equal highs/lows are clusters of similar extremes that tend to hold resting orders.",
            "A liquidity sweep takes out a prior extreme then reverses; 'stop hunt' describes the same footprint but must not be stated as intent that cannot be proven from data.",
            "A Fair Value Gap (FVG) is a three-candle imbalance where the middle candle's range leaves an untraded gap; an inverse FVG forms when price trades through it and it flips role.",
            "A bullish order block is commonly the last down candle before an impulsive up move; a bearish order block is the mirror. Mitigation is price returning to test the block.",
            "A breaker block is an order block that fails and flips polarity; a mitigation block sits inside a swing before an opposing push.",
            "Premium/discount: split a dealing range at equilibrium (50%); favour sells in premium and buys in discount for mean-reversion entries.",
            "Displacement is an energetic, imbalanced move that often precedes a gap; inducement is a minor trap move that lures early entries before the real level.",
            "Optimal Trade Entry (OTE) is a retracement band methodology (commonly the 0.62-0.79 retracement area) - a framework, never a guarantee.",
            "A dealing range is the swing high-to-low span used to judge premium/discount and target context.",
        ],
    },
    "strategies": {
        "title": "Trading strategies",
        "keywords": [
            "strategy", "trend following", "breakout and retest", "retest", "range trading",
            "pullback", "mean reversion", "reversal", "london session", "new york session",
            "overlap", "session strategy", "confluence", "setup", "backtest",
        ],
        "notes": [
            "Trend-following: enter with the established structure, ideally on a pullback, and trail risk behind structure.",
            "Breakout-and-retest: enter after price closes beyond a level and then retests it as new support/resistance.",
            "Range trading: buy near range support and sell near range resistance, accepting that ranges eventually break.",
            "Pullback entries: wait for a controlled retracement against trend into a value area (FVG/order block) rather than chasing.",
            "Liquidity sweep plus structure confirmation: sweep a level, then wait for a BOS/CHoCH before entering.",
            "Session strategies: London often sets the day's range; the London/New York overlap concentrates volatility; New York can reverse earlier moves.",
            "Confluence: combine independent evidence (structure, level, indicator, session). More independent agreement raises confidence; it never removes risk.",
            "Define invalidation first. If there is no clear invalidation, there is no trade. No-setup is a valid and often preferable outcome.",
        ],
    },
    "risk_management": {
        "title": "Risk management",
        "keywords": [
            "risk management", "risk per trade", "daily loss", "drawdown",
            "consecutive losses", "correlated", "overtrading", "journal", "take profit",
            "target", "equity", "exposure", "news risk", "stop placement",
        ],
        "notes": [
            "Risk per trade is the loss if the stop is hit; a common guideline is a small fixed percentage of equity, but the exact number is a personal choice, not a rule.",
            "Position size = (equity x risk%) / (stop distance x value per unit).",
            "Risk-to-reward should be evaluated with the realistic probability of the setup, not treated as an expected value on its own.",
            "A maximum daily loss limit and a consecutive-loss pause protect the account from tilt and from correlated bets.",
            "Correlated positions (for example two USD-short trades) compound the same risk even though they look like separate trades.",
            "Higher leverage reduces the price move needed to lose the account; spreads and slippage grow relative to targets at high leverage.",
            "Scheduled news and data releases can gap price beyond a stop; reduce size or stand aside around them.",
            "Keep a journal of entry rationale, outcome, and emotion; review it to find repeatable edges and recurring mistakes.",
        ],
    },
    "market_timings": {
        "title": "Market timings and sessions",
        "keywords": [
            "market open", "market closed", "is the market open", "trading hours",
            "session", "sydney", "tokyo", "london", "new york", "overlap",
            "daylight saving", "dst", "holiday", "weekend", "nse", "nifty hours",
            "ist", "gmt", "utc", "timezone", "time zone",
        ],
        "notes": [
            "Forex is over-the-counter and trades roughly 24 hours a day, five days a week, from Sunday evening to Friday evening in New York time.",
            "The four cash sessions are Sydney, Tokyo, London, and New York; the London/New York overlap is typically the most liquid.",
            "Daylight-saving changes shift session boundaries in UTC; always convert from the venue's local timezone rather than a fixed UTC offset.",
            "Exchange-traded instruments (such as NSE indices) follow strict local exchange hours and holidays, unlike OTC forex.",
            "Never infer that a market is open from recent quotes or candles: use the deterministic market-session result, which is independent of data availability.",
            "An individual broker's spot-metals or CFD schedule may differ from the standard convention; if the venue schedule is not verified, say so.",
        ],
    },
}


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def select_topics(message: str, limit: int = 3) -> list[str]:
    """Return the topic ids most relevant to a user message.

    Scoring is a simple, deterministic keyword overlap. A multi-word keyword
    that appears as a phrase scores higher than an isolated token so that, for
    example, "order block" beats a stray "block".
    """
    text = (message or "").lower()
    tokens = _tokenize(text)
    scored: list[tuple[int, str]] = []
    for topic_id, topic in TOPICS.items():
        score = 0
        for keyword in topic["keywords"]:
            keyword_lower = keyword.lower()
            if " " in keyword_lower:
                if keyword_lower in text:
                    score += 3
            elif keyword_lower in tokens:
                score += 2
        if score:
            scored.append((score, topic_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [topic_id for _score, topic_id in scored[:limit]]


def topic_notes(topic_ids: Iterable[str]) -> str:
    """Render the selected topics as a compact reference block."""
    blocks: list[str] = []
    for topic_id in topic_ids:
        topic = TOPICS.get(topic_id)
        if not topic:
            continue
        bullets = "\n".join(f"- {note}" for note in topic["notes"])
        blocks.append(f"## {topic['title']}\n{bullets}")
    return "\n\n".join(blocks)


def knowledge_for_query(message: str, limit: int = 3) -> str:
    """Return concise reference notes for the topics relevant to a question."""
    return topic_notes(select_topics(message, limit=limit))
