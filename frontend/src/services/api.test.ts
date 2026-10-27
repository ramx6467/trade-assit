import {afterEach, describe, expect, it, vi} from 'vitest';
import {api} from './api';

// The AI chat request is the only place the frontend used to block forever.
// These tests exercise the request layer directly (App.test.tsx mocks the whole
// module), covering the structured backend error contract and timeouts.
function jsonResponse(body: unknown, init?: {status?: number; headers?: Record<string, string>}) {
  const status = init?.status ?? 200;
  const headers = init?.headers ?? {};
  return {
    ok: status < 400,
    status,
    headers: {get: (name: string) => (Object.prototype.hasOwnProperty.call(headers, name) ? headers[name] : null)},
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('AI request error handling', () => {
  it('surfaces the structured backend error body, code and retry hint', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse(
        {
          success: false,
          error: {code: 'GEMINI_QUOTA_EXHAUSTED', message: 'The Gemini API daily quota has been exhausted.', retryable: false, retry_after_seconds: 58311},
        },
        {status: 502, headers: {'X-AI-Error-Code': 'GEMINI_QUOTA_EXHAUSTED', 'Retry-After': '58311'}},
      ),
    ));

    await expect(api.chat('hi', 'EURUSD', '15m', false)).rejects.toMatchObject({
      message: 'The Gemini API daily quota has been exhausted.',
      code: 'GEMINI_QUOTA_EXHAUSTED',
      retryAfterSeconds: 58311,
    });
  });

  it('keeps working with the plain detail contract used by non-AI errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({detail: 'Unsupported instrument BAD.'}, {status: 400}),
    ));

    await expect(api.chat('hi', 'BAD', '15m', false)).rejects.toMatchObject({
      message: 'Unsupported instrument BAD.',
    });
  });

  it('reports a reachable error when the backend cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    await expect(api.chat('hi', 'EURUSD', '15m', false)).rejects.toMatchObject({
      code: 'NETWORK_ERROR',
    });
    await expect(api.chat('hi', 'EURUSD', '15m', false)).rejects.toThrow(/could not reach the backend/i);
  });

  it('aborts the AI request and reports a timeout instead of waiting forever', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn().mockImplementation((_url: string, init?: RequestInit) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name: 'AbortError'})));
      }),
    ));

    const pending = api.chat('hi', 'EURUSD', '15m', false);
    const assertion = expect(pending).rejects.toMatchObject({code: 'AI_TIMEOUT'});
    await vi.advanceTimersByTimeAsync(60000);
    await assertion;
  });

  it('returns the parsed body for a successful request', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({reply: 'answer', data_status: 'DEMO'}),
    ));

    await expect(api.chat('hi', 'EURUSD', '15m', false)).resolves.toMatchObject({reply: 'answer'});
  });
});

describe('historical candle requests', () => {
  it('builds a bounded history request with limit and an exclusive end boundary', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({candles: []}));
    vi.stubGlobal('fetch', fetchMock);

    await api.candleHistory('EURUSD', '15m', {limit: 1000, end: '2026-10-16T10:00:00Z'});

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/v1/market-data/history?');
    const query = new URLSearchParams(url.split('?')[1]);
    expect(query.get('symbol')).toBe('EURUSD');
    expect(query.get('timeframe')).toBe('15m');
    expect(query.get('limit')).toBe('1000');
    expect(query.get('end')).toBe('2026-10-16T10:00:00Z');
    expect(query.get('start')).toBeNull();
  });

  it('omits optional range parameters when not supplied', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({candles: []}));
    vi.stubGlobal('fetch', fetchMock);

    await api.candleHistory('XAUUSD', '1h');

    const query = new URLSearchParams(String(fetchMock.mock.calls[0][0]).split('?')[1]);
    expect(query.get('symbol')).toBe('XAUUSD');
    expect(query.get('timeframe')).toBe('1h');
    expect(query.get('limit')).toBeNull();
    expect(query.get('end')).toBeNull();
  });
});
