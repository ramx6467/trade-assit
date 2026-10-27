import {useEffect,useState} from 'react';
import {getTimezoneShortLabel} from '../utils/timezone';

/**
 * Live chart-footer clock.
 *
 * Renders the browser's current time formatted in the selected IANA timezone,
 * next to that timezone's abbreviation and UTC offset (for example
 * ``17:30:00 IST UTC+05:30``). It recreates its one-second timer whenever the
 * timezone changes and clears it on unmount, so it never leaks an interval and
 * never runs a second timer per render.
 *
 * The clock only *reads* the browser clock. It never touches stored candle
 * timestamps - those stay in UTC and are formatted separately by the chart.
 */
function formatClock(date: Date, timezone: string): string {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: timezone,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(date);
  } catch {
    return date.toISOString().slice(11, 19);
  }
}

export function formatUtcOffset(date: Date, timezone: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      timeZoneName: 'longOffset',
    }).formatToParts(date);
    const name = parts.find((part) => part.type === 'timeZoneName')?.value || '';
    if (!name) return 'UTC';
    // Node/Chrome report 'GMT+05:30'; present it as 'UTC+05:30' for consistency
    // with the rest of the footer. A zero offset is simply 'UTC'.
    const offset = name.replace('GMT', 'UTC');
    return /^UTC[+-]00:00$/.test(offset) ? 'UTC' : offset;
  } catch {
    return 'UTC';
  }
}

export default function ChartClock({timezone}: {timezone: string}) {
  const [now, setNow] = useState<Date>(() => new Date());

  useEffect(() => {
    setNow(new Date());
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, [timezone]);

  const label = getTimezoneShortLabel(timezone, now);
  const offset = formatUtcOffset(now, timezone);
  return (
    <span className="chart-clock" title={`Live clock in ${timezone}`}>
      <time dateTime={now.toISOString()}>{formatClock(now, timezone)}</time>
      <b>{label}</b>
      <small>{offset}</small>
    </span>
  );
}
