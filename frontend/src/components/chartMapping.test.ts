import {describe,expect,it} from 'vitest';
import {chartTime,mapCandle,mapIndicatorPoint,matchingZones,mergeCandlesByTime,priceSourceMismatch,resolveChartPriceLine} from './chartMapping';
import {buildZoneRects} from './zoneRects';
import type {Candle} from '../types';

const candle=(timestamp:string,close:number,open=close,high=close,low=close):Candle=>({
  timestamp,open,high,low,close,volume:null,volume_type:'unavailable',instrument:'XAUUSD',timeframe:'5m',data_source:'XAUS',
});

describe('chart object conversion',()=>{
 it('maps UTC candles and indicator points to epoch seconds',()=>{
  const candle:Candle={timestamp:'2026-10-16T10:00:00Z',open:10,high:12,low:9,close:11,volume:null,volume_type:'unavailable',instrument:'EURUSD',timeframe:'15m',data_source:'DEMO'};
  expect(mapCandle(candle)).toEqual({time:1792144800,open:10,high:12,low:9,close:11});
  expect(mapIndicatorPoint({time:candle.timestamp,value:11})).toEqual({time:1792144800,value:11});
 });
 it('rejects invalid chart timestamps',()=>expect(()=>chartTime('not-a-time')).toThrow('Invalid chart timestamp'));
 it('filters drawings to the selected instrument and timeframe',()=>{
  const zones=[
   {id:'eur15',instrument:'EURUSD',timeframe:'15m',direction:'bullish',lower:1,upper:2,status:'unmitigated',created_at:'2026-10-16T10:00:00Z'},
   {id:'xau15',instrument:'XAUUSD',timeframe:'15m',direction:'bearish',lower:1,upper:2,status:'unmitigated',created_at:'2026-10-16T10:00:00Z'},
   {id:'eur1h',instrument:'EURUSD',timeframe:'1h',direction:'bullish',lower:1,upper:2,status:'unmitigated',created_at:'2026-10-16T10:00:00Z'},
  ];
  expect(matchingZones(zones,'EURUSD','15m').map(x=>x.id)).toEqual(['eur15']);
 });
 it('draws the last-price line from the validated quote, not the candle close',()=>{
  // XAUS serves a spot quote (4166.10) and an exchange chart feed (close
  // 4181.10): the header and the chart line must both use the quote.
  expect(resolveChartPriceLine(4166.10,4181.10)).toBe(4166.10);
  // With no quote in hand the newest candle close is the only honest fallback.
  expect(resolveChartPriceLine(null,4181.10)).toBe(4181.10);
  expect(resolveChartPriceLine(undefined,undefined)).toBeNull();
  expect(resolveChartPriceLine(NaN,4181.10)).toBe(4181.10);
 });
 it('flags a quote/candle source divergence instead of hiding it',()=>{
  const mismatch=priceSourceMismatch(4166.10,4181.10);
  expect(mismatch.mismatch).toBe(true);
  expect(mismatch.difference).toBeCloseTo(-15.0,5);
  expect(priceSourceMismatch(4181.10,4181.10).mismatch).toBe(false);
  expect(priceSourceMismatch(4181.0,4181.2).mismatch).toBe(false); // inside tolerance
  expect(priceSourceMismatch(null,4181.10)).toEqual({mismatch:false,difference:null});
 });
 it('keeps 5m candles ordered and deduplicated by UTC timestamp',()=>{
  const current=[candle('2026-10-16T10:05:00Z',2),candle('2026-10-16T10:10:00Z',3)];
  const older=[candle('2026-10-16T10:00:00Z',1),candle('2026-10-16T10:05:00Z',9)]; // 10:05 repeated
  const merged=mergeCandlesByTime(older,current);
  expect(merged.map(c=>c.timestamp)).toEqual([
   '2026-10-16T10:00:00Z','2026-10-16T10:05:00Z','2026-10-16T10:10:00Z',
  ]);
  // The repeated bar appears once, and the later (current) copy wins.
  expect(merged[1].close).toBe(2);
 });
 it('preserves each candle genuine OHLC close (no quote substitution)',()=>{
  const source=candle('2026-10-16T10:05:00Z',4181.10,4185.0,4187.3,4181.10);
  const mapped=mapCandle(source);
  expect(mapped.open).toBe(4185.0);
  expect(mapped.high).toBe(4187.3);
  expect(mapped.low).toBe(4181.10);
  expect(mapped.close).toBe(4181.10);
  // Mapping is pure: the source candle is not mutated.
  expect(source.close).toBe(4181.10);
  // The last-price line follows the quote even though the candle close differs.
  expect(resolveChartPriceLine(4166.10,mapped.close)).toBe(4166.10);
 });
 it('aligns indicators and SMC overlays to the exact candle timestamps',()=>{
  const source=candle('2026-10-16T10:05:00Z',11);
  const epoch=chartTime(source.timestamp);
  // Indicators, candles and overlays share one time base - no extra tz offset.
  expect(mapCandle(source).time).toBe(epoch);
  expect(mapIndicatorPoint({time:source.timestamp,value:11}).time).toBe(epoch);
  const rects=buildZoneRects({
   zones:[{id:'z1',instrument:'XAUUSD',timeframe:'5m',direction:'bullish',lower:10,upper:12,status:'unmitigated',created_at:source.timestamp}],
   orderBlocks:[],
   drawings:{fvgs:true,orderBlocks:false},
   symbol:'XAUUSD',
   timeframe:'5m',
   candleTimes:new Set([epoch]),
  });
  expect(rects).toHaveLength(1);
  expect(rects[0].startTime).toBe(epoch);
 });
 it('never adds a timezone offset to stored candle timestamps',()=>{
  const iso='2026-10-16T20:00:00Z';
  const epoch=Math.floor(Date.parse(iso)/1000);
  expect(chartTime(iso)).toBe(epoch);
  expect(mapCandle(candle(iso,1)).time).toBe(epoch);
 });
});
