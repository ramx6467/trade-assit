import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from './App';
import { api } from './services/api';

vi.mock('./components/PriceChart', () => ({
  default: () => <div role="img" aria-label="demo chart" />
}));

vi.mock('./services/api', () => ({
  api: {
    health: vi.fn().mockResolvedValue({ status: 'ok', ai_configured: false, ai_verified: false }),
    aiStatus: vi.fn().mockResolvedValue({ configured: false, model_configured: false, verified: false, verification_status: 'NOT_CONFIGURED', verified_at: null, message: 'Missing configuration', model: null }),
    verifyAi: vi.fn().mockResolvedValue({ configured: true, model_configured: true, verified: true, verification_status: 'VERIFIED', verified_at: '2026-10-16T10:00:00Z', message: 'Verified', model: 'test-model' }),
    marketStatus: vi.fn().mockResolvedValue({
      symbol: 'XAUUSD',
      provider: 'DEMO',
      configured: false,
      connection_status: 'DISCONNECTED',
      data_status: 'DEMO',
      streaming: false,
      last_update: null,
      message: 'Synthetic demonstration data is active.',
      available_timeframes: ['1m', '2m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '1d', '1w'],
    }),
    quote: vi.fn().mockRejectedValue(new Error('quote polling not configured in this test')),
    marketSession: vi.fn().mockResolvedValue({
      symbol: 'XAUUSD',
      market_status: 'OPEN',
      session_summary: 'OPEN - weekday',
      session: {
        symbol: 'XAUUSD', market: 'FOREX_OTC', asset_class: 'Forex', venue: 'OTC', venue_timezone: 'UTC',
        schedule: 'FX_24_5', schedule_verified: true, schedule_source: 'Standard', schedule_note: null,
        status: 'OPEN', status_label: 'OPEN', session_source: 'documented', reason: 'Open', is_weekend: false,
        utc_time: '2026-10-16T12:00:00Z', venue_time: '2026-10-16T12:00:00+00:00', venue_time_label: '12:00 UTC',
        sessions: [], active_sessions: ['London'], next_open: null, next_close: null, holiday_calendar: 'not_verified'
      },
      data: { provider: 'DEMO', configured: false, connection_status: 'DISCONNECTED', data_status: 'DEMO', streaming: false, quote_available: false, last_update: null, last_data_timestamp: null, freshness_seconds: null, message: null }
    }),
    snapshot: vi.fn().mockResolvedValue({
      symbol: 'XAUUSD', instrument: 'XAUUSD', timeframe: '5m', provider: 'DEMO', data_source: 'DEMO', data_status: 'DEMO',
      last_successful_timestamp: '2026-10-16T10:15:00Z', warnings: [],
      candles: [
        { timestamp: '2026-10-16T10:00:00Z', open: 2650, high: 2655, low: 2648, close: 2652, volume: 100, volume_type: 'tick', instrument: 'XAUUSD', timeframe: '5m', data_source: 'DEMO' },
        { timestamp: '2026-10-16T10:05:00Z', open: 2652, high: 2658, low: 2651, close: 2656, volume: 120, volume_type: 'tick', instrument: 'XAUUSD', timeframe: '5m', data_source: 'DEMO' },
      ],
      indicator_result: {
        symbol: 'XAUUSD', timeframe: '5m', candle_timestamp: '2026-10-16T10:05:00Z',
        indicators: { ema9: 2651, ema20: 2650, ema50: 2645, ema200: 2630, sma20: 2650, rsi14: 55, macd: { line: 1.2, signal: 0.8, histogram: 0.4 }, atr14: 3.5, bollinger20: { middle: 2650, upper: 2660, lower: 2640 }, vwap: 2652 },
        series: { ema20: [], ema50: [], vwap: [] }
      },
      fvgs: [], order_blocks: [], structure: { support_clusters: [], resistance_clusters: [], breaks: [], liquidity_sweeps: [] },
      setup: { status: 'WATCHLIST', explanation: 'Demo setup', conditions_satisfied: [], conditions_not_satisfied: [] }
    }),
    candles: vi.fn().mockResolvedValue({
      symbol: 'XAUUSD', instrument: 'XAUUSD', timeframe: '5m', provider: 'DEMO', data_source: 'DEMO', data_status: 'DEMO',
      last_successful_timestamp: '2026-10-16T10:05:00Z', warnings: [],
      candles: []
    }),
    candleHistory: vi.fn().mockResolvedValue({
      symbol: 'XAUUSD', instrument: 'XAUUSD', timeframe: '5m', provider: 'DEMO', data_source: 'DEMO', data_status: 'DEMO',
      last_successful_timestamp: '2026-10-16T10:05:00Z', warnings: [], candles: []
    }),
    indicators: vi.fn().mockResolvedValue({
      symbol: 'XAUUSD', timeframe: '5m', candle_timestamp: '2026-10-16T10:05:00Z',
      indicators: { ema9: 2651, ema20: 2650, ema50: 2645, ema200: 2630, sma20: 2650, rsi14: 55, macd: { line: 1.2, signal: 0.8, histogram: 0.4 }, atr14: 3.5, bollinger20: { middle: 2650, upper: 2660, lower: 2640 }, vwap: 2652 },
      series: { ema20: [], ema50: [], vwap: [] }
    }),
    fvgs: vi.fn().mockResolvedValue({ zones: [], rule: 'demo' }),
    structure: vi.fn().mockResolvedValue({ support_clusters: [], resistance_clusters: [], breaks: [], liquidity_sweeps: [] }),
    orderBlocks: vi.fn().mockResolvedValue({ zones: [], rule: 'demo' }),
    setup: vi.fn().mockResolvedValue({ status: 'WATCHLIST', explanation: 'Demo', conditions_satisfied: [], conditions_not_satisfied: [] }),
    history: vi.fn().mockResolvedValue({ label: 'SIMULATED', orders: [], positions: [], accounts_by_currency: { USD: { initial_balance: '100000', equity: '100000', available_simulated_funds: '100000', realized_pnl: '0', unrealized_pnl: '0', fees: '0', slippage_model_bps: '0.5', quote_currency: 'USD' } }, fees_model: { rate: '0', slippage_bps: '0.5' }, accounting_status: 'demo' }),
    brokers: vi.fn().mockResolvedValue([]),
    tradingView: vi.fn().mockResolvedValue({ status: 'AVAILABLE_AS_LINK_ONLY', integration_type: 'Website link', chart_widget_enabled: false, account_authenticated: false, account_data_access: false, capabilities: [], limitations: [], documentation_url: 'https://www.tradingview.com', link_url: 'https://www.tradingview.com' }),
    evaluate: vi.fn().mockResolvedValue({
      symbol: 'XAUUSD', instrument: 'XAUUSD', timeframe: '5m', market_data_status: 'DEMO', source: 'DEMO',
      analysis_timestamp: '2026-10-16T10:05:00Z', evaluation_timestamp: '2026-10-16T10:05:00Z', candle_timestamp: '2026-10-16T10:05:00Z',
      warnings: [], direction: 'NO_SETUP', setup_type: null, current_price: 2652, data_freshness_seconds: 0,
      entry_price: null, entry_zone: null, entry_trigger: null, stop_loss: null, take_profit_1: null, take_profit_2: null,
      risk_reward_ratio: null, max_risk_percent: 1, risk_amount: null, position_sizing_example: null,
      supporting_indicators: { ema20: 2650, ema50: 2645, atr14: 3.5, candle_timestamp: '2026-10-16T10:05:00Z' },
      supporting_smart_money_concepts: [], invalidation_condition: null, reasons: [], risks: [],
      status: 'NO_SETUP', conditions_satisfied: [], conditions_not_satisfied: ['Bullish BOS confirmation required'],
      explanation: 'No confirmed 5m setup present. Trend is neutral and awaiting break of structure.',
      ai_review: null, side: 'BUY', market_bias: 'NEUTRAL', entry: null, take_profit: null, risk_reward: null,
      provider: 'DEMO', data_status: 'DEMO', invalidation_conditions: []
    }),
    positionSize: vi.fn(),
    order: vi.fn().mockResolvedValue({ id: 'PAPER-0001' }),
    chat: vi.fn().mockResolvedValue({ reply: 'Chat response', data_status: 'DEMO' }),
  }
}));

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.clearAllMocks();
});

describe('Timeframe evaluation buttons and switching', () => {
  it('evaluates BUY setup on the selected 5m timeframe', async () => {
    render(<App />);
    await screen.findByText('Synthetic demonstration data is active.');

    // Select 5m timeframe button
    const btn5m = screen.getAllByRole('button', { name: '5m' })[0];
    fireEvent.click(btn5m);

    // Click Evaluate BUY setup
    const buyBtn = screen.getByRole('button', { name: 'Evaluate BUY setup' });
    fireEvent.click(buyBtn);

    await waitFor(() => {
      expect(api.evaluate).toHaveBeenCalledWith('XAUUSD', '5m', 'BUY');
    });

    // Check evaluation result rendered
    expect(await screen.findByText('NO_SETUP')).toBeInTheDocument();
    expect(screen.getByText('Bullish BOS confirmation required')).toBeInTheDocument();
  });

  it('evaluates SELL setup on the selected 15m timeframe', async () => {
    vi.mocked(api.evaluate).mockResolvedValueOnce({
      symbol: 'XAUUSD', instrument: 'XAUUSD', timeframe: '15m', market_data_status: 'DEMO', source: 'DEMO',
      analysis_timestamp: '2026-10-16T10:15:00Z', evaluation_timestamp: '2026-10-16T10:15:00Z', candle_timestamp: '2026-10-16T10:15:00Z',
      warnings: [], direction: 'SELL', setup_type: 'RETEST_ENTRY', current_price: 2650, data_freshness_seconds: 0,
      entry_price: 2650, entry_zone: { lower: 2649.5, upper: 2650.5 }, entry_trigger: 'Retest of 15m resistance',
      stop_loss: 2658, take_profit_1: 2638, take_profit_2: 2626, risk_reward_ratio: 2.0, max_risk_percent: 1,
      risk_amount: 1000, position_sizing_example: { quantity: 125, unit: 'oz', quote_currency: 'USD' },
      supporting_indicators: { ema20: 2648, ema50: 2655, atr14: 4.0, candle_timestamp: '2026-10-16T10:15:00Z' },
      supporting_smart_money_concepts: ['Bearish BOS on 15m'], invalidation_condition: 'Close above 2658',
      reasons: ['15m bearish trend alignment'], risks: ['High volatility'],
      status: 'SELL_CANDIDATE', conditions_satisfied: ['Bearish alignment', 'Valid R:R >= 1.5'], conditions_not_satisfied: [],
      explanation: 'Confirmed 15m SELL setup.',
      ai_review: null, side: 'SELL', market_bias: 'BEARISH', entry: 2650, take_profit: 2638, risk_reward: 2.0,
      provider: 'DEMO', data_status: 'DEMO', invalidation_conditions: ['Close above 2658']
    });

    render(<App />);
    await screen.findByText('Synthetic demonstration data is active.');

    // Select 15m timeframe
    const btn15m = screen.getAllByRole('button', { name: '15m' })[0];
    fireEvent.click(btn15m);

    // Click Evaluate SELL setup
    const sellBtn = screen.getByRole('button', { name: 'Evaluate SELL setup' });
    fireEvent.click(sellBtn);

    await waitFor(() => {
      expect(api.evaluate).toHaveBeenCalledWith('XAUUSD', '15m', 'SELL');
    });

    expect(await screen.findByText(/SELL · RETEST ENTRY/)).toBeInTheDocument();
    expect(screen.getByText('Confirmed 15m SELL setup.')).toBeInTheDocument();
  });

  it('recovers button state after an evaluation failure without leaving buttons stuck', async () => {
    vi.mocked(api.evaluate).mockRejectedValueOnce(new Error('Market data feed temporarily unavailable'));

    render(<App />);
    await screen.findByText('Synthetic demonstration data is active.');

    const buyBtn = screen.getByRole('button', { name: 'Evaluate BUY setup' });
    fireEvent.click(buyBtn);

    // Expect readable error displayed
    expect(await screen.findByText('Market data feed temporarily unavailable')).toBeInTheDocument();

    // Verify buttons are recovered and not permanently disabled
    expect(screen.getByRole('button', { name: 'Evaluate BUY setup' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Evaluate SELL setup' })).toBeEnabled();
  });

  it('shows timeframe mismatch banner when user switches timeframes after evaluation', async () => {
    vi.mocked(api.evaluate).mockResolvedValueOnce({
      symbol: 'XAUUSD', instrument: 'XAUUSD', timeframe: '5m', market_data_status: 'DEMO', source: 'DEMO',
      analysis_timestamp: '2026-10-16T10:05:00Z', evaluation_timestamp: '2026-10-16T10:05:00Z', candle_timestamp: '2026-10-16T10:05:00Z',
      warnings: [], direction: 'NO_SETUP', setup_type: null, current_price: 2652, data_freshness_seconds: 0,
      entry_price: null, entry_zone: null, entry_trigger: null, stop_loss: null, take_profit_1: null, take_profit_2: null,
      risk_reward_ratio: null, max_risk_percent: 1, risk_amount: null, position_sizing_example: null,
      supporting_indicators: { ema20: 2650, ema50: 2645, atr14: 3.5, candle_timestamp: '2026-10-16T10:05:00Z' },
      supporting_smart_money_concepts: [], invalidation_condition: null, reasons: [], risks: [],
      status: 'NO_SETUP', conditions_satisfied: [], conditions_not_satisfied: [],
      explanation: '5m evaluation completed.', ai_review: null, side: 'BUY', market_bias: 'NEUTRAL',
      entry: null, take_profit: null, risk_reward: null, provider: 'DEMO', data_status: 'DEMO', invalidation_conditions: []
    });

    render(<App />);
    await screen.findByText('Synthetic demonstration data is active.');

    // Click 5m
    const btn5m = screen.getAllByRole('button', { name: '5m' })[0];
    fireEvent.click(btn5m);

    // Evaluate BUY
    fireEvent.click(screen.getByRole('button', { name: 'Evaluate BUY setup' }));
    await waitFor(() => expect(api.evaluate).toHaveBeenCalledWith('XAUUSD', '5m', 'BUY'));

    // Switch to 1h
    const btn1h = screen.getAllByRole('button', { name: '1h' })[0];
    fireEvent.click(btn1h);

    // Verify notice explains that 5m result is displayed while chart is 1h
    expect(await screen.findByText(/Displaying NO_SETUP evaluation for/)).toBeInTheDocument();
    expect(screen.getAllByText('5m').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('1h').length).toBeGreaterThanOrEqual(2);
  });
});
