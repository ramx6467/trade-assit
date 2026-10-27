import {describe,expect,it} from 'vitest';
import {
  INITIAL_VISIBLE_BARS,
  MIN_CHART_HEIGHT,
  RIGHT_OFFSET_BARS,
  clampChartHeight,
  initialVisibleLogicalRange,
  shouldRefitChart,
} from './chartView';

describe('chart viewport helpers', () => {
  it('shows the most recent bars plus right whitespace on first load', () => {
    const range = initialVisibleLogicalRange(240, 140, 6);
    // Bars 100..239 are visible, then 6 bars of empty space on the right.
    expect(range.from).toBe(100);
    expect(range.to).toBe(245);
  });

  it('never goes negative and includes every bar for a short series', () => {
    expect(initialVisibleLogicalRange(3, 140, 6)).toEqual({from: 0, to: 8});
    expect(initialVisibleLogicalRange(0, 140, 6)).toEqual({from: 0, to: 5});
  });

  it('refits only on first data or a symbol/timeframe change', () => {
    expect(shouldRefitChart({hasFitted: false, symbolChanged: false, timeframeChanged: false})).toBe(true);
    expect(shouldRefitChart({hasFitted: true, symbolChanged: true, timeframeChanged: false})).toBe(true);
    expect(shouldRefitChart({hasFitted: true, symbolChanged: false, timeframeChanged: true})).toBe(true);
    // A live quote tick changes the candles but not symbol/timeframe: the user's
    // zoom and scroll position must be preserved (no fitContent per tick).
    expect(shouldRefitChart({hasFitted: true, symbolChanged: false, timeframeChanged: false})).toBe(false);
  });

  it('clamps the chart height for very small panes', () => {
    expect(clampChartHeight(740)).toBe(740);
    expect(clampChartHeight(50)).toBe(MIN_CHART_HEIGHT);
    expect(clampChartHeight(undefined)).toBe(MIN_CHART_HEIGHT);
    expect(clampChartHeight(Number.NaN)).toBe(MIN_CHART_HEIGHT);
  });

  it('uses sensible spacing defaults', () => {
    expect(INITIAL_VISIBLE_BARS).toBeGreaterThan(50);
    expect(RIGHT_OFFSET_BARS).toBeGreaterThan(0);
    expect(MIN_CHART_HEIGHT).toBeGreaterThan(0);
  });
});
