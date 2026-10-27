import {useEffect,useRef,useState} from 'react';
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  TickMarkType,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import type {Candle,IndicatorSeries} from '../types';
import type {SetupEvaluation} from '../services/api';
import {chartTime,mapCandle,mapIndicatorPoint,matchingZones,resolveChartPriceLine,type ChartZone} from './chartMapping';
import {buildZoneRects,MAX_FVG_ZONES} from './zoneRects';
import {ZonePrimitive} from './zonePrimitive';
import {INITIAL_VISIBLE_BARS,RIGHT_OFFSET_BARS,clampChartHeight,initialVisibleLogicalRange,shouldRefitChart} from './chartView';
import {formatChartCrosshairTime,formatChartTickMark,isIntradayTimeframe} from '../utils/timezone';

// OHLC readout shown in the chart's top-left corner, TradingView style.
interface CandleReadout{open:number;high:number;low:number;close:number;up:boolean;}
// TradingView prints axis/legend prices with fixed precision and no thousands
// separator (for example ``4185.00``), so match that here.
const price2=(value:number)=>value.toFixed(2);
function readoutOf(candle:Candle):CandleReadout{
  return {open:candle.open,high:candle.high,low:candle.low,close:candle.close,up:candle.close>=candle.open};
}
function sameReadout(a:CandleReadout|null,b:CandleReadout|null):boolean{
  if(a===b)return true;
  if(!a||!b)return false;
  return a.open===b.open&&a.high===b.high&&a.low===b.low&&a.close===b.close&&a.up===b.up;
}

type Zone=ChartZone;
type Structure={support_clusters:{price:number;touches:number}[];resistance_clusters:{price:number;touches:number}[]};
type Block={id:string;instrument:string;timeframe:string;direction:string;lower:number;upper:number;status:string;created_at:string;confirmed_at:string|null;mitigated?:boolean};

interface PriceChartProps {
  candles: Candle[];
  symbol: string;
  timeframe: string;
  indicators: IndicatorSeries;
  zones: Zone[];
  enabled: {ema20:boolean;ema50:boolean;vwap:boolean};
  drawings: {fvgs:boolean;orderBlocks:boolean;supportResistance:boolean};
  orderBlocks: Block[];
  structure: Structure|null;
  dataStatus?: string;
  setup?: SetupEvaluation|null;
  showSetup?: boolean;
  timezone?: string;
  // The same validated quote the header shows. The chart's horizontal
  // last-price line is drawn from this so the two never disagree.
  quotePrice?: number|null;
  quoteLabel?: string;
  isMaximized?: boolean;
  onReachLeftEdge?: () => void;
  loadingOlder?: boolean;
  historyExhausted?: boolean;
  jumpToLatestToken?: number;
}

export default function PriceChart({
  candles,
  symbol,
  timeframe,
  indicators,
  zones,
  enabled,
  drawings,
  orderBlocks,
  structure,
  dataStatus='DEMO',
  setup=null,
  showSetup=false,
  timezone='Asia/Kolkata',
  quotePrice=null,
  quoteLabel='Last price',
  isMaximized=false,
  onReachLeftEdge,
  loadingOlder=false,
  historyExhausted=false,
  jumpToLatestToken=0,
}: PriceChartProps){
  const host=useRef<HTMLDivElement>(null);
  const chart=useRef<IChartApi|null>(null);
  const series=useRef<ISeriesApi<'Candlestick'>|null>(null);
  const volume=useRef<ISeriesApi<'Histogram'>|null>(null);
  const lines=useRef<Record<string,ISeriesApi<'Line'>|null>>({});
  const markers=useRef<ISeriesMarkersPluginApi<Time>|null>(null);
  const priceLines=useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([]);
  const zonesPrimitive=useRef<ZonePrimitive|null>(null);
  const tzRef=useRef(timezone);
  tzRef.current=timezone;
  // Candle under the crosshair (null = not hovering a bar, so the latest candle
  // is shown instead). Stored as a readout so identical moves do not re-render.
  const [hover,setHover]=useState<CandleReadout|null>(null);

  useEffect(()=>{
    if(!host.current)return;
    const initialHeight = clampChartHeight(host.current.clientHeight || (isMaximized ? 600 : 360));
    const initialWidth = host.current.clientWidth || 700;
    const intraday = isIntradayTimeframe(timeframe);

    const c=createChart(host.current,{
      width: initialWidth,
      height: initialHeight,
      layout:{background:{type:ColorType.Solid,color:'#0b111a'},textColor:'#8c9aab'},
      grid:{vertLines:{color:'#18232f'},horzLines:{color:'#18232f'}},
      // Right-hand price scale: readable labels, automatic scaling to the
      // visible candles, and a reserved strip at the bottom so the price axis
      // never overlaps the volume histogram.
      rightPriceScale:{
        borderColor:'#263341',
        entireTextOnly:true,
        scaleMargins:{top:0.08,bottom:0.24},
      },
      timeScale:{
        borderColor:'#263341',
        // timeVisible must be true for intraday intervals, otherwise
        // lightweight-charts emits only day-level ticks and the axis collapses
        // to a repeating date label.
        timeVisible:intraday,
        secondsVisible:timeframe==='1m',
        rightOffset:RIGHT_OFFSET_BARS,
        barSpacing:7,
        minBarSpacing:0.5,
        // Keep the visible range stable when the pane is resized.
        lockVisibleTimeRangeOnResize:true,
        fixLeftEdge:false,
        fixRightEdge:false,
        tickMarkFormatter:(time:Time,tickMarkType:TickMarkType)=>
          formatChartTickMark(time,tickMarkType,tzRef.current),
      },
      localization:{
        locale:'en-US',
        // NOTE: no localization.priceFormatter here - it would also reformat
        // the volume axis. Precise 2-dp gold prices come from the candlestick
        // series' own priceFormat below, which only affects that series' scale.
        timeFormatter:(time:Time)=>formatChartCrosshairTime(time,tzRef.current),
      },
      crosshair:{
        mode:CrosshairMode.Normal,
        vertLine:{color:'#536577',width:1,labelBackgroundColor:'#2a3542'},
        horzLine:{color:'#536577',labelBackgroundColor:'#2a3542'},
      },
      handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:true},
      handleScale:{axisPressedMouseMove:true,mouseWheel:true,pinch:true},
    });

    const s=c.addSeries(CandlestickSeries,{
      upColor:'#28c997',
      downColor:'#ed6471',
      // Subtle borders give the candles a TradingView-like definition without
      // changing the body colours; wicks stay 1px (thin) as the library draws them.
      borderVisible:true,
      borderUpColor:'#1f9e78',
      borderDownColor:'#c95060',
      wickUpColor:'#28c997',
      wickDownColor:'#ed6471',
      // Gold is quoted to two decimals (XAUS returns e.g. 4166.100098).
      priceFormat:{type:'price',precision:2,minMove:0.01},
      // The chart draws its own last-price line from the validated quote (see
      // the price-line effect below). The series' built-in line/label would sit
      // on the newest candle close and could contradict the header quote when
      // the quote and candle feeds differ, so both are disabled here.
      priceLineVisible:false,
      lastValueVisible:false,
    });

    // OHLC readout: show the hovered candle, falling back to the latest candle.
    const onCrosshair=(param:MouseEventParams<Time>)=>{
      const bar=param.time?param.seriesData.get(s) as CandlestickData<Time>|undefined:undefined;
      if(!bar){setHover(prev=>prev===null?prev:null);return;}
      const next:CandleReadout={open:Number(bar.open),high:Number(bar.high),low:Number(bar.low),close:Number(bar.close),up:Number(bar.close)>=Number(bar.open)};
      setHover(prev=>sameReadout(prev,next)?prev:next);
    };
    c.subscribeCrosshairMove(onCrosshair);

    const v=c.addSeries(HistogramSeries,{
      priceScaleId:'volume',
      priceFormat:{type:'volume'},
      priceLineVisible:false,
      lastValueVisible:false,
    });
    c.priceScale('volume').applyOptions({scaleMargins:{top:0.82,bottom:0}});

    markers.current=createSeriesMarkers(s,[]);
    lines.current={
      ema20:c.addSeries(LineSeries,{color:'#61a9ff',lineWidth:1,priceLineVisible:false,lastValueVisible:false}),
      ema50:c.addSeries(LineSeries,{color:'#b38cff',lineWidth:1,priceLineVisible:false,lastValueVisible:false}),
      vwap:c.addSeries(LineSeries,{color:'#f3a75f',lineWidth:1,priceLineVisible:false,lastValueVisible:false}),
    };

    chart.current=c;
    series.current=s;
    volume.current=v;

    // FVG / order-block zones are painted by a series primitive (filled
    // rectangles) attached once; updating its zone list triggers a redraw.
    const primitive=new ZonePrimitive();
    s.attachPrimitive(primitive);
    zonesPrimitive.current=primitive;

    const resize=new ResizeObserver(entries=>{
      if(!host.current||!chart.current)return;
      const entry=entries[0];
      const w=entry?.contentRect?.width||host.current.clientWidth||700;
      const h=entry?.contentRect?.height||host.current.clientHeight||360;
      chart.current.applyOptions({width:w,height:clampChartHeight(h)});
    });
    resize.observe(host.current);

    return()=>{
      resize.disconnect();
      c.unsubscribeCrosshairMove(onCrosshair);
      if(series.current&&zonesPrimitive.current)series.current.detachPrimitive(zonesPrimitive.current);
      zonesPrimitive.current=null;
      c.remove();
      chart.current=null;
      series.current=null;
      volume.current=null;
      markers.current=null;
      lines.current={};
      priceLines.current=[];
    };
  },[]);

  // Re-apply the time axis when the timezone or timeframe changes. Only label
  // formatting changes here - candle timestamps and the visible range are left
  // untouched, so switching timezone never shifts the data or resets the zoom.
  useEffect(()=>{
    if(!chart.current)return;
    chart.current.applyOptions({
      timeScale:{
        timeVisible: isIntradayTimeframe(timeframe),
        secondsVisible: timeframe==='1m',
        tickMarkFormatter:(time:Time,tickMarkType:TickMarkType)=>
          formatChartTickMark(time,tickMarkType,timezone),
      },
      localization:{
        timeFormatter:(time:Time)=>formatChartCrosshairTime(time,timezone),
      },
    });
  },[timezone, timeframe]);

  // Handle maximize/restore resize trigger
  useEffect(()=>{
    if(!chart.current||!host.current)return;
    const w=host.current.clientWidth||700;
    const h=host.current.clientHeight||(isMaximized?600:360);
    chart.current.applyOptions({width:w,height:Math.max(220,h)});
  },[isMaximized]);

  const hasAutoFittedRef = useRef(false);
  const prevSymbolRef = useRef(symbol);
  const prevTimeframeRef = useRef(timeframe);
  const prevFirstTimeRef = useRef<number | null>(null);

  useEffect(()=>{
    if(!series.current)return;
    const first: number | null = candles.length ? chartTime(candles[0].timestamp) : null;
    const previousFirst = prevFirstTimeRef.current;
    // Detect a prepend (older bars loaded on left-scroll) so we can keep the
    // exact bars the user was looking at in view. A symbol/timeframe change also
    // changes the first timestamp but is handled by the fitContent branch below.
    const prepended = first!==null && previousFirst!==null && first<previousFirst;
    const beforeRange = prepended ? chart.current?.timeScale().getVisibleLogicalRange() : null;
    const added = prepended && previousFirst!==null
      ? candles.findIndex(x=>chartTime(x.timestamp)===previousFirst)
      : 0;

    const data = candles.map(x=>({...mapCandle(x),time:chartTime(x.timestamp) as UTCTimestamp}));
    series.current.setData(data);
    const validVolume=candles.filter(x=>x.volume!==null&&x.volume_type!=='unavailable');
    volume.current?.applyOptions({visible:validVolume.length>0});
    volume.current?.setData(validVolume.map(x=>({
      time:chartTime(x.timestamp) as UTCTimestamp,
      value:x.volume!,
      color:x.close>=x.open?'rgba(40,201,151,.38)':'rgba(237,100,113,.38)',
    })));
    if (candles.length > 0) {
      const symbolChanged = prevSymbolRef.current !== symbol;
      const timeframeChanged = prevTimeframeRef.current !== timeframe;
      // Re-anchor only on the first data or a real symbol/timeframe change.
      // A live tick must preserve the user's zoom and scroll position, so this
      // is deliberately not a per-quote fitContent.
      if (shouldRefitChart({hasFitted:hasAutoFittedRef.current,symbolChanged,timeframeChanged})) {
        try {
          chart.current?.timeScale().setVisibleLogicalRange(
            initialVisibleLogicalRange(candles.length,INITIAL_VISIBLE_BARS,RIGHT_OFFSET_BARS),
          );
        } finally {
          hasAutoFittedRef.current = true;
        }
      } else if (prepended && beforeRange && added > 0) {
        // Shift the visible logical window by the number of bars prepended so
        // the user stays on the same candles after older history is merged in.
        chart.current?.timeScale().setVisibleLogicalRange({
          from: beforeRange.from + added,
          to: beforeRange.to + added,
        });
      }
      prevSymbolRef.current = symbol;
      prevTimeframeRef.current = timeframe;
    }
    prevFirstTimeRef.current = first;
  },[candles, symbol, timeframe]);

  // Request older candles when the user scrolls the chart to its left edge.
  // A ref guard prevents re-firing until the user moves back to the right.
  const leftEdgeArmedRef = useRef(true);
  const candlesCountRef = useRef(candles.length);
  candlesCountRef.current = candles.length;
  useEffect(()=>{
    if(!chart.current)return;
    const timeScale=chart.current.timeScale();
    const handler=(range:{from:number;to:number}|null)=>{
      if(!range)return;
      if(!onReachLeftEdge||loadingOlder||historyExhausted)return;
      // Only treat this as "scrolled to history" when the user is zoomed in
      // enough that not every loaded bar is visible; otherwise an initial
      // fitContent (which spans the whole dataset) would look like the edge.
      const visibleSpan=range.to-range.from;
      if(range.from<=2 && visibleSpan<candlesCountRef.current-3){
        if(!leftEdgeArmedRef.current)return;
        leftEdgeArmedRef.current=false;
        onReachLeftEdge();
      } else if(range.from>2){
        leftEdgeArmedRef.current=true;
      }
    };
    timeScale.subscribeVisibleLogicalRangeChange(handler);
    return ()=>timeScale.unsubscribeVisibleLogicalRangeChange(handler);
  },[onReachLeftEdge,loadingOlder,historyExhausted]);

  // Imperative "jump to latest" requested from the parent via an incrementing token.
  useEffect(()=>{
    if(!jumpToLatestToken||!chart.current)return;
    chart.current.timeScale().scrollToRealTime();
  },[jumpToLatestToken]);

  useEffect(()=>{
    for(const key of ['ema20','ema50','vwap'] as const){
      const s=lines.current[key];
      if(!s)continue;
      const active=enabled[key];
      s.applyOptions({visible:active});
      s.setData(active?indicators[key].map(p=>({...mapIndicatorPoint(p),time:chartTime(p.time) as UTCTimestamp})):[]);
    }
  },[indicators,enabled]);

  useEffect(()=>{
    if(!series.current)return;
    priceLines.current.forEach(line=>series.current?.removePriceLine(line));
    priceLines.current=[];
    const times=new Set(candles.map(c=>chartTime(c.timestamp)));
    const fvgMarkers:SeriesMarker<UTCTimestamp>[]=[];

    // One source of truth for the "last price": the validated quote the header
    // uses, falling back to the newest candle close only when no quote exists.
    const lastClose=candles.length?candles[candles.length-1].close:null;
    const priceLineValue=resolveChartPriceLine(quotePrice,lastClose);
    if(priceLineValue!==null){
      priceLines.current.push(series.current.createPriceLine({
        price:priceLineValue,
        color:'#e6edf7',
        lineWidth:1,
        lineStyle:0,
        axisLabelVisible:true,
        title:quoteLabel,
      }));
    }

    zonesPrimitive.current?.setZones(buildZoneRects({
      zones,
      orderBlocks,
      drawings,
      symbol,
      timeframe,
      candleTimes:times,
    }));

    if(drawings.fvgs){
      const recent=matchingZones(zones,symbol,timeframe).filter(z=>z.status==='unmitigated').slice(-MAX_FVG_ZONES);
      for(const z of recent){
        const at=chartTime(z.created_at);
        if(!times.has(at))continue;
        fvgMarkers.push({
          time:at as UTCTimestamp,
          position:z.direction==='bullish'?'belowBar':'aboveBar',
          shape:z.direction==='bullish'?'arrowUp':'arrowDown',
          color:z.direction==='bullish'?'#39d9a5':'#f08b91',
          size:1,
        });
      }
    }

    if(drawings.supportResistance&&structure){
      for(const level of structure.support_clusters.slice(-3)){
        priceLines.current.push(series.current.createPriceLine({
          price:level.price,
          color:'#537f71',
          lineWidth:1,
          lineStyle:3,
          axisLabelVisible:false,
          title:`Support · ${level.touches} touches`,
        }));
      }
      for(const level of structure.resistance_clusters.slice(-3)){
        priceLines.current.push(series.current.createPriceLine({
          price:level.price,
          color:'#89626a',
          lineWidth:1,
          lineStyle:3,
          axisLabelVisible:false,
          title:`Resistance · ${level.touches} touches`,
        }));
      }
    }

    if(showSetup&&setup&&setup.direction!=='NO_SETUP'&&setup.symbol===symbol&&setup.timeframe===timeframe&&setup.entry_price!==null){
      const levels:[[number|null,string,string],...[number|null,string,string][]]=[
        [setup.entry_price,setup.direction==='BUY'?'#2dd6a0':'#ef7780','ENTRY'],
        [setup.stop_loss,'#ef626e','STOP'],
        [setup.take_profit_1,'#e6b458','TP1'],
        [setup.take_profit_2,'#63a9ff','TP2'],
        [setup.take_profit_3??null,'#a58bff','TP3'],
      ];
      if(setup.entry_zone){
        levels.push([setup.entry_zone.lower,'#2dd6a0','ENTRY ZONE LOW'],[setup.entry_zone.upper,'#2dd6a0','ENTRY ZONE HIGH']);
      }
      for(const [price,color,title] of levels){
        if(price!==null)priceLines.current.push(series.current.createPriceLine({
          price,
          color,
          lineWidth:title==='ENTRY'?2:1,
          lineStyle:title==='ENTRY'?0:2,
          axisLabelVisible:true,
          title:`${setup.direction} ${title}`,
        }));
      }
      const last=candles.at(-1);
      if(last){
        const at=chartTime(last.timestamp);
        if(times.has(at))fvgMarkers.push({
          time:at as UTCTimestamp,
          position:setup.direction==='BUY'?'belowBar':'aboveBar',
          shape:setup.direction==='BUY'?'arrowUp':'arrowDown',
          color:setup.direction==='BUY'?'#2dd6a0':'#ef7780',
          text:setup.direction,
        });
      }
    }
    markers.current?.setMarkers(fvgMarkers);
  },[zones,candles,symbol,timeframe,drawings,orderBlocks,structure,setup,showSetup,quotePrice,quoteLabel]);

  // The hovered candle when the crosshair is over a bar, else the latest candle.
  const readout = hover ?? (candles.length ? readoutOf(candles[candles.length-1]) : null);

  return (
    <div className={`chart-shell ${isMaximized?'is-maximized':''}`}>
      <div className="chart-label">
       <span className="chart-label-symbol">{symbol} · {timeframe} <span>{dataStatus}</span></span>
       {readout&&(
        <span className="chart-ohlc" aria-label="Open, high, low, close">
         <em>O</em><i className={readout.up?'up':'down'}>{price2(readout.open)}</i>
         <em>H</em><i className={readout.up?'up':'down'}>{price2(readout.high)}</i>
         <em>L</em><i className={readout.up?'up':'down'}>{price2(readout.low)}</i>
         <em>C</em><i className={readout.up?'up':'down'}>{price2(readout.close)}</i>
        </span>
       )}
      </div>
      {loadingOlder&&(
        <div
          aria-live="polite"
          style={{position:'absolute',top:'34px',left:'10px',zIndex:5,padding:'3px 9px',borderRadius:'4px',background:'rgba(11,17,26,.85)',color:'#8c9aab',fontSize:'11px',border:'1px solid #263341'}}
        >Loading older candles…</div>
      )}
      <div
        ref={host}
        aria-label={`${symbol} ${timeframe} candlestick chart`}
        role="img"
        style={{width:'100%',height:'100%',minHeight:isMaximized?undefined:220}}
      />
    </div>
  );
}
