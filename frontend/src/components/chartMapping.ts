import type {Candle} from '../types';

export type ChartZone={id:string;instrument:string;timeframe:string;direction:string;lower:number;upper:number;status:string;created_at:string};
export function chartTime(timestamp:string):number {
 const value=Math.floor(Date.parse(timestamp)/1000);
 if(!Number.isFinite(value))throw new Error(`Invalid chart timestamp: ${timestamp}`);
 return value;
}
export function mapCandle(candle:Candle){return {time:chartTime(candle.timestamp),open:candle.open,high:candle.high,low:candle.low,close:candle.close};}
export function mapIndicatorPoint(point:{time:string;value:number}){return {time:chartTime(point.time),value:point.value};}
export function matchingZones(zones:ChartZone[],instrument:string,timeframe:string){return zones.filter(zone=>zone.instrument===instrument&&zone.timeframe===timeframe);}

// Merge an older page of candles with the current series. Candles are keyed by
// their UTC timestamp, so a repeated bar collapses to one entry (the later copy
// wins) and the result is returned in chronological order. Timestamps are never
// rewritten, so candle ordering and accuracy are preserved exactly.
export function mergeCandlesByTime(older:Candle[],current:Candle[]):Candle[]{
  const byTime=new Map<number,Candle>();
  for(const candle of [...older,...current]) byTime.set(chartTime(candle.timestamp),candle);
  return [...byTime.values()].sort((a,b)=>chartTime(a.timestamp)-chartTime(b.timestamp));
}

// The chart's "last price" line must agree with the quote shown in the header.
// A provider can serve the quote and the OHLCV candles from *different*
// upstreams (XAUS: a spot quote vs an exchange chart feed), so the two numbers
// can legitimately differ. We therefore:
//   1. resolve the price line from the same validated quote the header uses,
//      falling back to the newest candle close only when no quote is available;
//   2. surface the divergence instead of hiding it.
export const QUOTE_MISMATCH_TOLERANCE = 0.5;

export function resolveChartPriceLine(
  quotePrice:number|null|undefined,
  lastCandleClose:number|null|undefined,
):number|null{
  if(quotePrice!=null&&Number.isFinite(quotePrice))return quotePrice;
  if(lastCandleClose!=null&&Number.isFinite(lastCandleClose))return lastCandleClose;
  return null;
}

export function priceSourceMismatch(
  quotePrice:number|null|undefined,
  lastCandleClose:number|null|undefined,
):{mismatch:boolean;difference:number|null}{
  if(quotePrice==null||lastCandleClose==null||!Number.isFinite(quotePrice)||!Number.isFinite(lastCandleClose)){
    return {mismatch:false,difference:null};
  }
  const difference=quotePrice-lastCandleClose;
  return {mismatch:Math.abs(difference)>QUOTE_MISMATCH_TOLERANCE,difference};
}
