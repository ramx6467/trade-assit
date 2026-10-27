// Pure helpers that turn backend FVG / order-block records into drawable rectangles.
// Kept free of chart-library imports so the geometry can be unit tested.

export type ZoneKind = 'fvg' | 'ob';
export type ZoneDirection = 'bullish' | 'bearish';

export interface ZoneSource {
  id: string;
  instrument: string;
  timeframe: string;
  direction: string;
  lower: number;
  upper: number;
  status: string;
  created_at: string;
  /** Optional lifecycle flag from the backend (price returned into the zone). */
  mitigated?: boolean;
}

export interface ZoneRect {
  id: string;
  kind: ZoneKind;
  direction: ZoneDirection;
  lower: number;
  upper: number;
  /** Epoch seconds of the pattern/origin candle (left boundary). */
  startTime: number;
  /** Epoch seconds of the right boundary, or null to extend to the chart's right edge. */
  endTime: number | null;
  label: string;
  /** Detection lifecycle status (order blocks: confirmed | invalidated | candidate). */
  status: string;
  /** True when a later candle traded back into the zone (it is no longer pristine). */
  mitigated: boolean;
}

export interface ZoneRectOptions {
  zones: ZoneSource[];
  orderBlocks: ZoneSource[];
  drawings: {fvgs: boolean; orderBlocks: boolean};
  symbol: string;
  timeframe: string;
  /** Epoch seconds of every candle currently loaded on the chart. */
  candleTimes: Set<number>;
  maxFvgs?: number;
  maxOrderBlocks?: number;
}

/**
 * Display budget for how many active zones to render.
 * This is a rendering limit only — detection still comes from the backend and
 * every drawn zone keeps its own real boundaries.
 *
 * Order blocks are shown across their full detected lifecycle so the chart never
 * collapses to a single box when several zones were found: active blocks are
 * drawn solid, invalidated blocks are drawn dimmed/dashed, and unconfirmed
 * candidates are drawn faintest/dotted. When more than `maxOrderBlocks` qualify,
 * the most relevant are kept: active first, then invalidated, then candidates,
 * each most-recent-first (see `OB_STATUS_RANK`).
 */
export const MAX_FVG_ZONES = 6;
export const MAX_ORDER_BLOCK_ZONES = 8;

/** Rendering priority for order-block lifecycle states (lower = kept first). */
const OB_STATUS_RANK: Record<string, number> = {confirmed: 0, invalidated: 1, candidate: 2};

function orderBlockLabel(status: string): string {
  if (status === 'invalidated') return 'OB · invalidated';
  if (status === 'candidate') return 'OB · candidate';
  return 'OB';
}

function parseEpochSeconds(timestamp: string): number | null {
  const value = Math.floor(Date.parse(timestamp) / 1000);
  return Number.isFinite(value) ? value : null;
}

function isValidRange(lower: number, upper: number): boolean {
  return Number.isFinite(lower) && Number.isFinite(upper) && upper > lower;
}

/**
 * Builds the rectangles for the Fair Value Gaps and Order Blocks that belong to
 * the selected instrument and timeframe.
 *
 * Rules:
 *  - FVGs use their detected `lower`/`upper` and are shown only while `unmitigated`.
 *  - Order blocks use the originating candle's full high/low range. All detected
 *    lifecycle states are eligible so multiple zones stay visible: `confirmed`
 *    (active), `invalidated` (distal edge closed through) and `candidate`
 *    (displacement without an aligned structure break yet). Each is styled
 *    distinctly by the renderer.
 *  - Zones are anchored to their `created_at` candle and extend into future space.
 *  - Zero-width/invalid ranges and zones without a matching candle are skipped.
 */
export function buildZoneRects(options: ZoneRectOptions): ZoneRect[] {
  const {
    zones,
    orderBlocks,
    drawings,
    symbol,
    timeframe,
    candleTimes,
    maxFvgs = MAX_FVG_ZONES,
    maxOrderBlocks = MAX_ORDER_BLOCK_ZONES,
  } = options;

  const rects: ZoneRect[] = [];

  const add = (kind: ZoneKind, source: ZoneSource, label: string) => {
    const lower = Number(source.lower);
    const upper = Number(source.upper);
    if (!isValidRange(lower, upper)) return;
    const startTime = parseEpochSeconds(source.created_at);
    if (startTime === null || !candleTimes.has(startTime)) return;
    const direction: ZoneDirection = source.direction === 'bullish' ? 'bullish' : 'bearish';
    rects.push({
      id: source.id,
      kind,
      direction,
      lower,
      upper,
      startTime,
      endTime: null,
      label: `${direction === 'bullish' ? 'Bullish' : 'Bearish'} ${label}`,
      status: source.status,
      mitigated: Boolean(source.mitigated),
    });
  };

  if (drawings.fvgs) {
    const active = zones
      .filter(zone => zone.instrument === symbol && zone.timeframe === timeframe && zone.status === 'unmitigated')
      .slice(-maxFvgs);
    for (const zone of active) add('fvg', zone, 'FVG');
  }

  if (drawings.orderBlocks) {
    const eligible = orderBlocks
      .filter(zone => zone.instrument === symbol && zone.timeframe === timeframe && OB_STATUS_RANK[zone.status] !== undefined)
      .sort((a, b) => {
        const byStatus = OB_STATUS_RANK[a.status] - OB_STATUS_RANK[b.status];
        if (byStatus !== 0) return byStatus;
        return (parseEpochSeconds(b.created_at) ?? 0) - (parseEpochSeconds(a.created_at) ?? 0);
      })
      .slice(0, maxOrderBlocks);
    for (const zone of eligible) add('ob', zone, orderBlockLabel(zone.status));
  }

  return rects;
}

export interface ZoneStyle {
  fill: string;
  border: string;
  /** Optional canvas dash pattern (empty/undefined = solid border). */
  dash?: number[];
}

/**
 * Translucent green/red fills with a visible border; OB hues differ from FVG so
 * the two remain distinguishable. Order-block lifecycle states use the same hue
 * at different strengths so active zones read clearly while invalidated and
 * candidate zones stay visible but subdued.
 */
export function zoneStyle(kind: ZoneKind, direction: ZoneDirection, status?: string): ZoneStyle {
  if (kind === 'fvg') {
    return direction === 'bullish'
      ? {fill: 'rgba(40, 201, 151, 0.16)', border: 'rgba(64, 214, 168, 0.65)'}
      : {fill: 'rgba(237, 100, 113, 0.16)', border: 'rgba(240, 139, 145, 0.65)'};
  }
  if (status === 'invalidated') {
    return direction === 'bullish'
      ? {fill: 'rgba(43, 159, 125, 0.07)', border: 'rgba(75, 196, 160, 0.45)', dash: [4, 3]}
      : {fill: 'rgba(168, 99, 106, 0.07)', border: 'rgba(206, 120, 128, 0.45)', dash: [4, 3]};
  }
  if (status === 'candidate') {
    return direction === 'bullish'
      ? {fill: 'rgba(43, 159, 125, 0.04)', border: 'rgba(75, 196, 160, 0.30)', dash: [1, 3]}
      : {fill: 'rgba(168, 99, 106, 0.04)', border: 'rgba(206, 120, 128, 0.30)', dash: [1, 3]};
  }
  return direction === 'bullish'
    ? {fill: 'rgba(43, 159, 125, 0.20)', border: 'rgba(75, 196, 160, 0.80)'}
    : {fill: 'rgba(168, 99, 106, 0.20)', border: 'rgba(206, 120, 128, 0.80)'};
}

export interface ZonePixels {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ZonePixelInput {
  /** Left/right x of the zone in CSS pixels, or null when the time is unknown. */
  x1: number | null;
  /** Right x in CSS pixels, or null to extend to the pane's right edge. */
  x2: number | null;
  /** Vertical coordinates of the upper/lower price boundaries, or null when out of range. */
  yUpper: number | null;
  yLower: number | null;
  paneWidth: number;
  paneHeight: number;
}

/**
 * Converts zone boundaries into a clipped on-screen rectangle in CSS pixels.
 * Returns null when the zone is degenerate or lies fully outside the pane
 * (which is what happens while panning, zooming or after a resize).
 */
export function clipZonePixels(input: ZonePixelInput): ZonePixels | null {
  const {x1, x2, yUpper, yLower, paneWidth, paneHeight} = input;
  if (x1 === null || yUpper === null || yLower === null) return null;

  const rightEdge = x2 === null ? paneWidth : x2;
  const left = Math.max(0, Math.min(x1, rightEdge));
  const right = Math.min(paneWidth, Math.max(x1, rightEdge));
  if (right - left <= 0) return null;

  const top = Math.max(0, Math.min(yUpper, yLower));
  const bottom = Math.min(paneHeight, Math.max(yUpper, yLower));
  if (bottom - top <= 0) return null;

  return {x: left, y: top, width: right - left, height: bottom - top};
}
