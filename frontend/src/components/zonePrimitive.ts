// Lightweight Charts v5 series primitive that paints FVG / order-block zones as
// filled rectangles. Coordinates are resolved from the live chart on every draw,
// so zones stay locked to their price/time boundaries while panning, zooming,
// resizing and scrolling.
import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  Time,
} from 'lightweight-charts';
import {clipZonePixels, zoneStyle, type ZoneRect} from './zoneRects';

type DrawTarget = Parameters<IPrimitivePaneRenderer['draw']>[0];

const MIN_LABEL_WIDTH = 64;
const MIN_LABEL_HEIGHT = 12;

class ZonePrimitiveRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly host: ZonePrimitive) {}

  draw(target: DrawTarget): void {
    const chart = this.host.chart;
    const series = this.host.series;
    const zones = this.host.zones;
    if (!chart || !series || zones.length === 0) return;

    const timeScale = chart.timeScale();

    target.useMediaCoordinateSpace(scope => {
      const ctx = scope.context;
      const paneWidth = scope.mediaSize.width;
      const paneHeight = scope.mediaSize.height;

      for (const zone of zones) {
        const start = timeScale.timeToCoordinate(zone.startTime as Time);
        const end = zone.endTime === null ? null : timeScale.timeToCoordinate(zone.endTime as Time);
        const upper = series.priceToCoordinate(zone.upper);
        const lower = series.priceToCoordinate(zone.lower);

        const pixels = clipZonePixels({
          x1: start === null ? null : Number(start),
          x2: end === null ? null : Number(end),
          yUpper: upper === null ? null : Number(upper),
          yLower: lower === null ? null : Number(lower),
          paneWidth,
          paneHeight,
        });
        if (!pixels) continue;

        const style = zoneStyle(zone.kind, zone.direction, zone.status);
        ctx.fillStyle = style.fill;
        ctx.fillRect(pixels.x, pixels.y, pixels.width, pixels.height);

        ctx.strokeStyle = style.border;
        ctx.lineWidth = 1;
        ctx.setLineDash(style.dash ?? []);
        ctx.strokeRect(pixels.x + 0.5, pixels.y + 0.5, Math.max(0, pixels.width - 1), Math.max(0, pixels.height - 1));
        ctx.setLineDash([]);

        if (pixels.width < MIN_LABEL_WIDTH) continue;

        // Tall zones get the label inside; short zones get it just outside so it
        // stays inside the pane instead of overflowing a thin box.
        const inside = pixels.height >= MIN_LABEL_HEIGHT;
        const roomAbove = pixels.y >= 14;
        const roomBelow = paneHeight - (pixels.y + pixels.height) >= 14;
        if (!inside && !roomAbove && !roomBelow) continue;
        ctx.fillStyle = style.border;
        ctx.font = '10px sans-serif';
        if (inside) {
          ctx.textBaseline = 'top';
          ctx.fillText(zone.label, pixels.x + 4, pixels.y + 3);
        } else if (roomAbove) {
          ctx.textBaseline = 'bottom';
          ctx.fillText(zone.label, pixels.x + 4, pixels.y - 2);
        } else {
          ctx.textBaseline = 'top';
          ctx.fillText(zone.label, pixels.x + 4, pixels.y + pixels.height + 2);
        }
      }
    });
  }
}

class ZonePaneView implements IPrimitivePaneView {
  private readonly rendererInstance: ZonePrimitiveRenderer;

  constructor(host: ZonePrimitive) {
    this.rendererInstance = new ZonePrimitiveRenderer(host);
  }

  // Behind the candles so the translucent fill never hides price action.
  zOrder(): PrimitivePaneViewZOrder {
    return 'bottom';
  }

  renderer(): IPrimitivePaneRenderer | null {
    return this.rendererInstance;
  }
}

export class ZonePrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApiBase<Time> | null = null;
  series: ISeriesApi<'Candlestick', Time> | null = null;
  zones: ZoneRect[];
  private requestUpdate: (() => void) | null = null;
  private readonly views: ZonePaneView[];

  constructor(zones: ZoneRect[] = []) {
    this.zones = zones;
    this.views = [new ZonePaneView(this)];
  }

  setZones(zones: ZoneRect[]): void {
    this.zones = zones;
    this.requestUpdate?.();
  }

  clear(): void {
    this.setZones([]);
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart;
    this.series = param.series as ISeriesApi<'Candlestick', Time>;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
    this.requestUpdate = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }
}
