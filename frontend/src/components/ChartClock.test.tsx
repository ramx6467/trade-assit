import {afterEach,describe,expect,it,vi} from 'vitest';
import {act,cleanup,render,screen} from '@testing-library/react';
import ChartClock,{formatUtcOffset} from './ChartClock';

describe('ChartClock (chart footer clock)',()=>{
 afterEach(()=>{cleanup();vi.useRealTimers()});

 it('ticks once per second in the selected timezone and clears its timer on unmount',()=>{
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-18T12:00:00Z'));
  const clearSpy=vi.spyOn(window,'clearInterval');
  const {unmount}=render(<ChartClock timezone="Asia/Kolkata"/>);
  // 12:00 UTC is 17:30 in Asia/Kolkata (UTC+05:30).
  expect(screen.getByText('17:30:00')).toBeInTheDocument();
  expect(screen.getByText('IST')).toBeInTheDocument();

  act(()=>{vi.advanceTimersByTime(1000)});
  expect(screen.getByText('17:30:01')).toBeInTheDocument();
  expect(screen.queryByText('17:30:00')).toBeNull();

  unmount();
  expect(clearSpy).toHaveBeenCalled();
  clearSpy.mockRestore();
 });

 it('reports the timezone abbreviation and UTC offset for each supported zone',()=>{
  const winter = new Date('2026-01-14T12:00:00Z');
  expect(formatUtcOffset(winter,'UTC')).toBe('UTC');
  expect(formatUtcOffset(winter,'Etc/UTC')).toBe('UTC');
  expect(formatUtcOffset(winter,'Asia/Kolkata')).toContain('UTC+05:30');
  expect(formatUtcOffset(winter,'America/New_York')).toContain('UTC-05:00');
  expect(formatUtcOffset(new Date('2026-07-14T12:00:00Z'),'America/New_York')).toContain('UTC-04:00');
  // London is GMT in winter and BST (UTC+01:00) in summer.
  expect(formatUtcOffset(winter,'Europe/London')).toBe('UTC');
  expect(formatUtcOffset(new Date('2026-07-14T12:00:00Z'),'Europe/London')).toContain('UTC+01:00');
 });

 it('restarts its timer when the display timezone changes',()=>{
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-18T12:00:00Z'));
  const {rerender}=render(<ChartClock timezone="Asia/Kolkata"/>);
  expect(screen.getByText('17:30:00')).toBeInTheDocument();
  rerender(<ChartClock timezone="UTC"/>);
  expect(screen.getByText('12:00:00')).toBeInTheDocument();
  expect(screen.getAllByText('UTC').length).toBeGreaterThanOrEqual(2);
 });
});
