import type {Time} from 'lightweight-charts';

// Display timezones offered by the chart toolbar. These are valid IANA zone
// names; ``EXCHANGE`` resolves to the instrument/provider venue zone.
export type TimezoneMode = 'Asia/Kolkata' | 'Etc/UTC' | 'America/New_York' | 'Europe/London' | 'EXCHANGE';

export interface TimezoneOption {
  value: TimezoneMode;
  label: string;
}

/** Single source of truth for the toolbar selector (and its tests). */
export const TIMEZONE_OPTIONS: TimezoneOption[] = [
  {value: 'Asia/Kolkata', label: 'Asia/Kolkata (IST)'},
  {value: 'Etc/UTC', label: 'UTC (GMT)'},
  {value: 'America/New_York', label: 'New York (ET)'},
  {value: 'Europe/London', label: 'London (GMT/BST)'},
  {value: 'EXCHANGE', label: 'Exchange / Provider'},
];

export interface ExchangeInfo {
  tz: string;
  label: string;
  short: string;
}

export const INSTRUMENT_EXCHANGE_TZ: Record<string, ExchangeInfo> = {
  // Single-instrument deployment: the only instrument TradeMind AI serves.
  XAUUSD: {tz: 'America/New_York', label: 'Exchange (New York · ET)', short: 'ET'},
};

export const DEFAULT_TIMEZONE_MODE: TimezoneMode = 'Asia/Kolkata';

export function getExchangeInfo(symbol: string, backendTz?: string | null): ExchangeInfo {
  if (backendTz && backendTz.trim()) {
    const tz = backendTz.trim();
    if (tz === 'Asia/Kolkata') return {tz, label: 'Exchange (NSE · IST)', short: 'IST'};
    if (tz === 'America/New_York') return {tz, label: 'Exchange (New York · ET)', short: 'ET'};
    if (tz === 'Europe/London') return {tz, label: 'Exchange (London · GMT/BST)', short: 'LDN'};
    if (tz === 'UTC') return {tz, label: 'Exchange (UTC)', short: 'UTC'};
    return {tz, label: `Exchange (${tz})`, short: tz.split('/').pop() || tz};
  }
  return INSTRUMENT_EXCHANGE_TZ[symbol.toUpperCase()] || {tz: 'UTC', label: 'Exchange (UTC)', short: 'UTC'};
}

export function resolveTimezone(mode: TimezoneMode, symbol: string, backendTz?: string | null): string {
  if (mode === 'EXCHANGE') return getExchangeInfo(symbol, backendTz).tz;
  // The remaining modes are already valid IANA zone names.
  return mode;
}

/**
 * Whether a timeframe is an intraday interval (minutes or hours).
 *
 * The chart's time axis only shows clock labels for intraday intervals, so this
 * drives ``timeScale.timeVisible``. Getting it wrong is what made a 5-minute
 * chart collapse to repeating day labels: with ``timeVisible:false``
 * lightweight-charts emits only day-level tick marks.
 */
const INTRADAY_UNITS = /^\d+(m|h)$/;
export function isIntradayTimeframe(timeframe: string): boolean {
  // Case-sensitive: lowercase 'm'/'h' are minutes/hours, so '1M' (month) and
  // '1W'/'1D' stay date-based even if such a token were ever introduced.
  return INTRADAY_UNITS.test(timeframe.trim());
}

export function getTimezoneShortLabel(tz: string, date: Date = new Date()): string {
  if (tz === 'Asia/Kolkata') return 'IST';
  if (tz === 'UTC' || tz === 'Etc/UTC' || tz === 'Etc/GMT' || tz === 'GMT') return 'UTC';
  try {
    const parts = new Intl.DateTimeFormat('en-US', {timeZone: tz, timeZoneName: 'short'}).formatToParts(date);
    const tzPart = parts.find(p => p.type === 'timeZoneName');
    if (tzPart?.value) return tzPart.value;
  } catch {}
  return tz.split('/').pop() || tz;
}

export function timeToDate(time: Time | string | number | Date | null | undefined): Date | null {
  if (time == null) return null;
  if (time instanceof Date) return isNaN(time.getTime()) ? null : time;
  if (typeof time === 'number') {
    // Check if seconds or milliseconds
    const ms = time < 1e11 ? time * 1000 : time;
    const d = new Date(ms);
    return isNaN(d.getTime()) ? null : d;
  }
  if (typeof time === 'string') {
    const d = new Date(time);
    return isNaN(d.getTime()) ? null : d;
  }
  if (typeof time === 'object' && 'year' in time && 'month' in time && 'day' in time) {
    const d = new Date(Date.UTC(time.year, time.month - 1, time.day));
    return isNaN(d.getTime()) ? null : d;
  }
  return null;
}

export function formatTimeInTimezone(
  dateOrTimestamp: Time | string | number | Date | null | undefined,
  timezone: string,
  includeSeconds = false
): string {
  const date = timeToDate(dateOrTimestamp);
  if (!date) return '—';
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      hour: '2-digit',
      minute: '2-digit',
      second: includeSeconds ? '2-digit' : undefined,
      hour12: true,
    }).format(date);
  } catch {
    return date.toUTCString();
  }
}

export function formatDateInTimezone(
  dateOrTimestamp: Time | string | number | Date | null | undefined,
  timezone: string
): string {
  const date = timeToDate(dateOrTimestamp);
  if (!date) return '—';
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    }).format(date);
  } catch {
    return date.toISOString().slice(0, 10);
  }
}

export function formatDateTimeInTimezone(
  dateOrTimestamp: Time | string | number | Date | null | undefined,
  timezone: string,
  includeSeconds = true
): string {
  const date = timeToDate(dateOrTimestamp);
  if (!date) return '—';
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: includeSeconds ? '2-digit' : undefined,
      hour12: true,
    }).format(date);
  } catch {
    return date.toISOString();
  }
}

/**
 * Formats time for the chart crosshair label on the time axis.
 */
export function formatChartCrosshairTime(time: Time | number, timezone: string): string {
  const date = timeToDate(time);
  if (!date) return '';
  try {
    const dStr = new Intl.DateTimeFormat('en-GB', {
      timeZone: timezone,
      day: '2-digit',
      month: 'short',
      year: 'numeric',
    }).format(date);
    const tStr = new Intl.DateTimeFormat('en-GB', {
      timeZone: timezone,
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(date);
    return `${dStr}  ${tStr}`;
  } catch {
    return String(time);
  }
}

/**
 * Formats tick marks along the horizontal time scale axis in the selected
 * timezone.
 *
 * ``lightweight-charts`` decides which ``TickMarkType`` to emit from the chart's
 * own time scale. When ``timeScale.timeVisible`` is true, intraday ticks arrive
 * as ``TickMarkType.Time`` (and a ``DayOfMonth`` tick at each day boundary); when
 * it is false the scale collapses to day-level marks only. So an intraday chart
 * must have ``timeVisible: true`` for these labels to appear at all.
 *
 * Labels stay short (<= ~8 chars) as the library requires.
 */
export function formatChartTickMark(
  time: Time | number,
  tickMarkType: number,
  timezone: string
): string | null {
  const date = timeToDate(time);
  if (!date) return null;
  try {
    // TickMarkType enum: 0 = Year, 1 = Month, 2 = DayOfMonth, 3 = Time, 4 = TimeWithSeconds
    switch (tickMarkType) {
      case 0: // Year
        return new Intl.DateTimeFormat('en-US', {timeZone: timezone, year: 'numeric'}).format(date);
      case 1: // Month
        return new Intl.DateTimeFormat('en-US', {timeZone: timezone, month: 'short'}).format(date);
      case 2: // DayOfMonth -> the day-change marker (for example "Oct 17").
        return new Intl.DateTimeFormat('en-US', {timeZone: timezone, month: 'short', day: 'numeric'}).format(date);
      case 3: // Time
        return new Intl.DateTimeFormat('en-GB', {
          timeZone: timezone,
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
        }).format(date);
      case 4: // TimeWithSeconds
        return new Intl.DateTimeFormat('en-GB', {
          timeZone: timezone,
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false,
        }).format(date);
      default:
        return null;
    }
  } catch {
    return null;
  }
}
