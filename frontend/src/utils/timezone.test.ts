import {describe,expect,it} from 'vitest';
import {
  DEFAULT_TIMEZONE_MODE,
  TIMEZONE_OPTIONS,
  formatChartCrosshairTime,
  formatChartTickMark,
  formatDateTimeInTimezone,
  formatTimeInTimezone,
  getTimezoneShortLabel,
  isIntradayTimeframe,
  resolveTimezone,
} from './timezone';

describe('Timezone utility', () => {
  it('defaults to Asia/Kolkata', () => {
    expect(DEFAULT_TIMEZONE_MODE).toBe('Asia/Kolkata');
    expect(resolveTimezone('Asia/Kolkata', 'XAUUSD')).toBe('Asia/Kolkata');
  });

  it('resolves the four IANA display zones and the exchange fallback', () => {
    expect(resolveTimezone('Etc/UTC', 'XAUUSD')).toBe('Etc/UTC');
    expect(resolveTimezone('America/New_York', 'XAUUSD')).toBe('America/New_York');
    expect(resolveTimezone('Europe/London', 'XAUUSD')).toBe('Europe/London');
    expect(resolveTimezone('EXCHANGE', 'XAUUSD')).toBe('America/New_York');
    // The exchange timezone reported by the backend wins over any local fallback.
    expect(resolveTimezone('EXCHANGE', 'XAUUSD', 'UTC')).toBe('UTC');
    // A symbol this deployment does not serve never invents an exchange schedule.
    expect(resolveTimezone('EXCHANGE', 'EURUSD')).toBe('UTC');
    expect(resolveTimezone('EXCHANGE', 'NIFTY')).toBe('UTC');
    expect(resolveTimezone('EXCHANGE', 'CUSTOM', 'Asia/Tokyo')).toBe('Asia/Tokyo');
  });

  it('offers exactly the four IANA zones plus the exchange shortcut', () => {
    const values = TIMEZONE_OPTIONS.map(o => o.value);
    expect(values).toEqual(['Asia/Kolkata', 'Etc/UTC', 'America/New_York', 'Europe/London', 'EXCHANGE']);
  });

  it('classifies intraday vs daily intervals for the time axis', () => {
    // timeVisible must be true for these or lightweight-charts collapses the
    // axis to repeating day labels (the "Oct 16" defect on a 5m chart).
    for (const tf of ['1m', '2m', '3m', '5m', '15m', '30m', '1h', '2h', '4h']) {
      expect(isIntradayTimeframe(tf)).toBe(true);
    }
    for (const tf of ['1d', '1w', '1wk', '1M']) {
      expect(isIntradayTimeframe(tf)).toBe(false);
    }
  });

  it('accurately converts 11:57 AM UTC to 5:27 PM IST (+05:30)', () => {
    const utcStamp = '2026-10-16T11:57:00Z';
    const formattedIST = formatTimeInTimezone(utcStamp, 'Asia/Kolkata');
    // Expected: 05:27 PM
    expect(formattedIST).toMatch(/0?5:27\s*PM/i);

    const formattedUTC = formatTimeInTimezone(utcStamp, 'UTC');
    // Expected: 11:57 AM
    expect(formattedUTC).toMatch(/11:57\s*AM/i);
  });

  it('handles day and date boundaries when converting between UTC and Asia/Kolkata', () => {
    // 2026-10-16 20:00:00 UTC -> 2026-10-17 01:30:00 IST (+1 day)
    const lateUtc = '2026-10-16T20:00:00Z';
    const istFull = formatDateTimeInTimezone(lateUtc, 'Asia/Kolkata');
    expect(istFull).toContain('Oct 17, 2026');
    expect(istFull).toMatch(/0?1:30:00\s*AM/i);

    const utcFull = formatDateTimeInTimezone(lateUtc, 'UTC');
    expect(utcFull).toContain('Oct 16, 2026');
    expect(utcFull).toMatch(/0?8:00:00\s*PM/i);
  });

  it('handles year boundary transitions between UTC and Asia/Kolkata', () => {
    // 2026-12-31 22:00:00 UTC -> 2027-01-01 03:30:00 IST (Next Year!)
    const yearEndUtc = '2026-12-31T22:00:00Z';
    const istYear = formatDateTimeInTimezone(yearEndUtc, 'Asia/Kolkata');
    expect(istYear).toContain('2027');
    expect(istYear).toContain('Jan 1');

    const utcYear = formatDateTimeInTimezone(yearEndUtc, 'UTC');
    expect(utcYear).toContain('2026');
    expect(utcYear).toContain('Dec 31');
  });

  it('correctly handles Daylight Saving Time (DST) for exchange-local instruments', () => {
    // New York (Gold / XAUUSD) COMEX: EDT (UTC-4) in Summer, EST (UTC-5) in Winter
    const summerUtc = '2026-07-15T14:00:00Z'; // 10:00 AM EDT
    const winterUtc = '2026-01-15T14:00:00Z'; // 09:00 AM EST

    const nySummer = formatTimeInTimezone(summerUtc, 'America/New_York');
    const nyWinter = formatTimeInTimezone(winterUtc, 'America/New_York');

    expect(nySummer).toMatch(/10:00\s*AM/i);
    expect(nyWinter).toMatch(/0?9:00\s*AM/i);

    // London (GBPUSD): BST (UTC+1) in Summer, GMT (UTC+0) in Winter
    const londonSummer = formatTimeInTimezone(summerUtc, 'Europe/London');
    const londonWinter = formatTimeInTimezone(winterUtc, 'Europe/London');

    expect(londonSummer).toMatch(/0?3:00\s*PM/i); // 14:00 UTC + 1 = 15:00 (3 PM)
    expect(londonWinter).toMatch(/0?2:00\s*PM/i); // 14:00 UTC + 0 = 14:00 (2 PM)

    // IST does not observe DST: always UTC+5:30
    const istSummer = formatTimeInTimezone(summerUtc, 'Asia/Kolkata');
    const istWinter = formatTimeInTimezone(winterUtc, 'Asia/Kolkata');
    expect(istSummer).toMatch(/0?7:30\s*PM/i); // 14:00 + 5:30 = 19:30
    expect(istWinter).toMatch(/0?7:30\s*PM/i);
  });

  it('formats chart crosshair timestamps in the selected timezone', () => {
    const epochSec = 1792151820; // 2026-10-16T11:57:00Z
    const crosshairIST = formatChartCrosshairTime(epochSec, 'Asia/Kolkata');
    expect(crosshairIST).toContain('16 Oct 2026');
    expect(crosshairIST).toContain('17:27');

    const crosshairUTC = formatChartCrosshairTime(epochSec, 'UTC');
    expect(crosshairUTC).toContain('16 Oct 2026');
    expect(crosshairUTC).toContain('11:57');
  });

  it('formats tick marks along horizontal time scale axis in selected timezone', () => {
    const epochSec = 1792151820; // 2026-10-16T11:57:00Z
    // Type 3: Time
    expect(formatChartTickMark(epochSec, 3, 'Asia/Kolkata')).toBe('17:27');
    expect(formatChartTickMark(epochSec, 3, 'UTC')).toBe('11:57');

    // Type 0: Year
    expect(formatChartTickMark(epochSec, 0, 'Asia/Kolkata')).toBe('2026');

    // Type 1: Month
    expect(formatChartTickMark(epochSec, 1, 'Asia/Kolkata')).toBe('Oct');

    // Type 2: DayOfMonth
    const tickDay = formatChartTickMark(epochSec, 2, 'Asia/Kolkata');
    expect(tickDay).toMatch(/16/);
  });

  it('renders intraday clock labels (not dates) for a 5m chart in every display zone', () => {
    const epochSec = 1792151820; // 2026-10-16T11:57:00Z
    // TickMarkType 3 = Time. An intraday axis must show HH:MM in the chosen zone.
    expect(formatChartTickMark(epochSec, 3, 'Asia/Kolkata')).toBe('17:27');       // +05:30
    expect(formatChartTickMark(epochSec, 3, 'Etc/UTC')).toBe('11:57');           // +00:00
    expect(formatChartTickMark(epochSec, 3, 'Europe/London')).toBe('12:57');     // BST +01:00 (October)
    expect(formatChartTickMark(epochSec, 3, 'America/New_York')).toBe('07:57');  // EDT -04:00 (October)
  });

  it('shows a date label at the day change and times within the same day', () => {
    // 2026-10-16T18:29:00Z = 23:59 IST; one minute later it is 00:00 on 17 Oct IST.
    const before = 1792175340;
    const atMidnight = 1792175400;
    expect(formatChartTickMark(before, 3, 'Asia/Kolkata')).toBe('23:59');
    expect(formatChartTickMark(atMidnight, 3, 'Asia/Kolkata')).toBe('00:00');
    // The day-change marker (TickMarkType 2) carries the local date.
    expect(formatChartTickMark(atMidnight, 2, 'Asia/Kolkata')).toBe('Oct 17');
    // The same instant is still 16 Oct in UTC - the date label follows the zone.
    expect(formatChartTickMark(atMidnight, 2, 'Etc/UTC')).toBe('Oct 16');
  });

  it('renders the Friday session close as Saturday IST without moving the instant', () => {
    // 2026-10-16T20:55:00Z is Friday 16:55 ET - the last closed 5m bar of the
    // trading week. It is 2026-10-17 02:25 IST because IST is UTC+05:30, so the
    // calendar day changes while the instant does not.
    const fridayClose = 1792184100;
    expect(formatChartCrosshairTime(fridayClose, 'Asia/Kolkata')).toContain('17 Oct 2026');
    expect(formatChartCrosshairTime(fridayClose, 'Asia/Kolkata')).toContain('02:25');
    expect(formatChartCrosshairTime(fridayClose, 'America/New_York')).toContain('16 Oct 2026');
    expect(formatChartCrosshairTime(fridayClose, 'America/New_York')).toContain('16:55');
    // The axis labels follow the display zone across the Friday/Saturday line.
    expect(formatChartTickMark(fridayClose, 3, 'Asia/Kolkata')).toBe('02:25');
    expect(formatChartTickMark(fridayClose, 3, 'America/New_York')).toBe('16:55');
    expect(formatChartTickMark(fridayClose, 2, 'Asia/Kolkata')).toBe('Oct 17');
    expect(formatChartTickMark(fridayClose, 2, 'America/New_York')).toBe('Oct 16');
  });

  it('keeps the Saturday/Sunday weekend boundary on the right side in each zone', () => {
    // Sunday 2026-10-18T22:00:00Z (18:00 ET) is the weekly gold re-open: still
    // Sunday in New York, already Monday in Kolkata.
    const reopen = 1792360800;
    expect(formatChartTickMark(reopen, 2, 'Asia/Kolkata')).toBe('Oct 19');
    expect(formatChartTickMark(reopen, 2, 'America/New_York')).toBe('Oct 18');
    expect(formatChartTickMark(reopen, 3, 'Asia/Kolkata')).toBe('03:30');
    // Saturday itself: 2026-10-17T18:30:00Z is already Sunday in IST.
    const saturdayEvening = 1792261800;
    expect(formatChartTickMark(saturdayEvening, 2, 'Asia/Kolkata')).toBe('Oct 18');
    expect(formatChartTickMark(saturdayEvening, 2, 'America/New_York')).toBe('Oct 17');
  });

  it('keeps tick labels DST-correct for New York and London', () => {
    const summer = 1784116800; // 2026-07-15T12:00:00Z
    const winter = 1768478400; // 2026-01-15T12:00:00Z
    expect(formatChartTickMark(summer, 3, 'America/New_York')).toBe('08:00'); // EDT -04:00
    expect(formatChartTickMark(winter, 3, 'America/New_York')).toBe('07:00'); // EST -05:00
    expect(formatChartTickMark(summer, 3, 'Europe/London')).toBe('13:00');    // BST +01:00
    expect(formatChartTickMark(winter, 3, 'Europe/London')).toBe('12:00');    // GMT +00:00
  });

  it('returns appropriate short timezone labels', () => {
    expect(getTimezoneShortLabel('Asia/Kolkata')).toBe('IST');
    expect(getTimezoneShortLabel('UTC')).toBe('UTC');
    expect(getTimezoneShortLabel('Etc/UTC')).toBe('UTC');
    // DST-aware abbreviations for the exchange zones.
    expect(getTimezoneShortLabel('America/New_York', new Date('2026-07-15T12:00:00Z'))).toMatch(/EDT|GMT-4|UTC-4/);
    expect(getTimezoneShortLabel('Europe/London', new Date('2026-01-15T12:00:00Z'))).toMatch(/GMT|UTC/);
  });
});
