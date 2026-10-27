// Pure chart-viewport helpers. Kept free of chart-library imports so the
// initial zoom, refit decision and height clamping can be unit tested.

/** Candles shown in the initial view - a TradingView-like comfortable zoom. */
export const INITIAL_VISIBLE_BARS = 140;
/** Empty bars kept to the right of the newest candle (TradingView whitespace). */
export const RIGHT_OFFSET_BARS = 6;
/** Smallest usable chart height in pixels. */
export const MIN_CHART_HEIGHT = 220;

export interface LogicalRange {
  from: number;
  to: number;
}

/**
 * The initial visible logical range: the most recent `visibleBars` candles,
 * plus `rightOffset` bars of whitespace on the right.
 *
 * Bar *count* (not a fixed pixel width) is chosen, so candle spacing adapts to
 * the pane width instead of being pinned to a pixel size on every screen.
 */
export function initialVisibleLogicalRange(
  barCount: number,
  visibleBars = INITIAL_VISIBLE_BARS,
  rightOffset = RIGHT_OFFSET_BARS,
): LogicalRange {
  const count = Math.max(0, Math.floor(barCount));
  const visible = Math.max(1, Math.floor(visibleBars));
  const offset = Math.max(0, Math.floor(rightOffset));
  const from = Math.max(0, count - visible);
  const to = Math.max(from, count - 1 + offset);
  return {from, to};
}

/**
 * Whether the chart should re-anchor its viewport to the latest bars.
 *
 * True only for the first data or a genuine symbol/timeframe change. A live
 * quote update must preserve the user's zoom and scroll position, so this stays
 * false for them (never call `fitContent` on every tick).
 */
export function shouldRefitChart(args: {
  hasFitted: boolean;
  symbolChanged: boolean;
  timeframeChanged: boolean;
}): boolean {
  return !args.hasFitted || args.symbolChanged || args.timeframeChanged;
}

/** Clamps a measured element height to the smallest usable chart height. */
export function clampChartHeight(height: number | null | undefined, minimum = MIN_CHART_HEIGHT): number {
  const value = Number(height);
  if (!Number.isFinite(value)) return minimum;
  return Math.max(minimum, Math.floor(value));
}
