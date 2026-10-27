import {describe,expect,it} from 'vitest';
import {buildZoneRects,clipZonePixels,zoneStyle,type ZoneSource} from './zoneRects';

const t=(value:string)=>Math.floor(Date.parse(value)/1000);
const T1=t('2026-10-16T10:00:00Z');
const T2=t('2026-10-16T10:15:00Z');
const T3=t('2026-10-16T10:30:00Z');
const TIMES=new Set([T1,T2,T3]);
const RECENT=Array.from({length:8},(_,i)=>t('2026-10-16T10:00:00Z')+i*900);

const makeFvg=(over:Partial<ZoneSource>={}):ZoneSource=>({
  id:'fvg-1',instrument:'EURUSD',timeframe:'15m',direction:'bullish',lower:1.1000,upper:1.1010,status:'unmitigated',created_at:'2026-10-16T10:30:00Z',...over,
});
const makeOb=(over:Partial<ZoneSource>={}):ZoneSource=>({
  id:'ob-1',instrument:'EURUSD',timeframe:'15m',direction:'bearish',lower:1.0990,upper:1.1020,status:'confirmed',created_at:'2026-10-16T10:15:00Z',...over,
});

const base={drawings:{fvgs:true,orderBlocks:true},symbol:'EURUSD',timeframe:'15m',candleTimes:TIMES};

describe('buildZoneRects',()=>{
  it('keeps the detected bullish FVG upper/lower boundaries and anchors the left edge to the pattern candle',()=>{
    const [rect]=buildZoneRects({...base,zones:[makeFvg()],orderBlocks:[]});
    expect(rect).toMatchObject({kind:'fvg',direction:'bullish',lower:1.1000,upper:1.1010,startTime:T3,endTime:null,label:'Bullish FVG'});
  });
  it('keeps the detected bearish FVG upper/lower boundaries',()=>{
    const [rect]=buildZoneRects({...base,zones:[makeFvg({direction:'bearish',lower:1.0980,upper:1.0995})],orderBlocks:[]});
    expect(rect).toMatchObject({direction:'bearish',lower:1.0980,upper:1.0995,label:'Bearish FVG'});
  });
  it('uses the full candle range for bullish order blocks',()=>{
    const [rect]=buildZoneRects({...base,zones:[],orderBlocks:[makeOb({direction:'bullish',lower:1.0950,upper:1.0975})]});
    expect(rect).toMatchObject({kind:'ob',direction:'bullish',lower:1.0950,upper:1.0975,startTime:T2,label:'Bullish OB'});
  });
  it('uses the full candle range for bearish order blocks',()=>{
    const [rect]=buildZoneRects({...base,zones:[],orderBlocks:[makeOb()]});
    expect(rect).toMatchObject({kind:'ob',direction:'bearish',lower:1.0990,upper:1.1020,label:'Bearish OB'});
  });
  it('extends active zones into future space by leaving the right boundary open',()=>{
    const rects=buildZoneRects({...base,zones:[makeFvg()],orderBlocks:[makeOb()]});
    expect(rects).toHaveLength(2);
    expect(rects.every(r=>r.endTime===null)).toBe(true);
  });
  it('skips invalid or zero-width zones',()=>{
    const rects=buildZoneRects({...base,zones:[makeFvg({upper:1.1000})],orderBlocks:[makeOb({lower:1.1020,upper:1.1020})]});
    expect(rects).toHaveLength(0);
  });
  it('skips zones whose origin candle is not part of the loaded candle data',()=>{
    const rects=buildZoneRects({...base,zones:[makeFvg({created_at:'2026-10-16T11:00:00Z'})],orderBlocks:[]});
    expect(rects).toHaveLength(0);
  });
  it('skips mitigated FVGs but renders every order-block lifecycle state',()=>{
    const rects=buildZoneRects({...base,
      zones:[makeFvg({status:'mitigated'})],
      orderBlocks:[makeOb({status:'confirmed'}),makeOb({id:'ob-c',status:'candidate'}),makeOb({id:'ob-i',status:'invalidated'})]});
    expect(rects).toHaveLength(3);
    expect(rects.every(r=>r.kind==='ob')).toBe(true);
  });
  it('labels and carries the lifecycle status of each order block',()=>{
    const rects=buildZoneRects({...base,zones:[],orderBlocks:[
      makeOb({status:'confirmed'}),makeOb({id:'ob-i',status:'invalidated'}),makeOb({id:'ob-c',status:'candidate'})]});
    expect(rects.map(r=>({status:r.status,label:r.label}))).toEqual([
      {status:'confirmed',label:'Bearish OB'},
      {status:'invalidated',label:'Bearish OB · invalidated'},
      {status:'candidate',label:'Bearish OB · candidate'},
    ]);
  });
  it('prioritises active order blocks over candidates when the display budget is exceeded',()=>{
    const many=Array.from({length:8},(_,i)=>makeOb({id:`ob-${i}`,status:'candidate',created_at:new Date(RECENT[i]*1000).toISOString()}));
    const rects=buildZoneRects({...base,candleTimes:new Set(RECENT),zones:[],
      orderBlocks:[makeOb({status:'confirmed',created_at:new Date(RECENT[0]*1000).toISOString()}),...many],
      maxOrderBlocks:3});
    expect(rects.map(r=>r.status)).toEqual(['confirmed','candidate','candidate']);
  });
  it('only draws the zones belonging to the selected instrument and timeframe',()=>{
    const rects=buildZoneRects({...base,
      zones:[makeFvg(),makeFvg({id:'xau',instrument:'XAUUSD'}),makeFvg({id:'m5',timeframe:'5m'})],
      orderBlocks:[]});
    expect(rects.map(r=>r.id)).toEqual(['fvg-1']);
  });
  it('honours the FVG and Order Block toggles independently',()=>{
    const fvgsOnly=buildZoneRects({...base,drawings:{fvgs:true,orderBlocks:false},zones:[makeFvg()],orderBlocks:[makeOb()]});
    expect(fvgsOnly.map(r=>r.kind)).toEqual(['fvg']);
    const obsOnly=buildZoneRects({...base,drawings:{fvgs:false,orderBlocks:true},zones:[makeFvg()],orderBlocks:[makeOb()]});
    expect(obsOnly.map(r=>r.kind)).toEqual(['ob']);
    const none=buildZoneRects({...base,drawings:{fvgs:false,orderBlocks:false},zones:[makeFvg()],orderBlocks:[makeOb()]});
    expect(none).toHaveLength(0);
  });
  it('does not accumulate zones across repeated toggles',()=>{
    const on=buildZoneRects({...base,drawings:{fvgs:true,orderBlocks:true},zones:[makeFvg()],orderBlocks:[makeOb()]});
    buildZoneRects({...base,drawings:{fvgs:false,orderBlocks:false},zones:[makeFvg()],orderBlocks:[makeOb()]});
    const onAgain=buildZoneRects({...base,drawings:{fvgs:true,orderBlocks:true},zones:[makeFvg()],orderBlocks:[makeOb()]});
    expect(onAgain).toEqual(on);
    expect(onAgain).toHaveLength(2);
  });
  it('draws only the most recent active zones up to the display budget',()=>{
    const many=Array.from({length:8},(_,i)=>makeFvg({id:`fvg-${i}`,created_at:new Date(RECENT[i]*1000).toISOString()}));
    const rects=buildZoneRects({...base,candleTimes:new Set(RECENT),zones:many,orderBlocks:[]});
    expect(rects).toHaveLength(6);
    expect(rects.map(r=>r.id)).toEqual(['fvg-2','fvg-3','fvg-4','fvg-5','fvg-6','fvg-7']);
  });
  it('honours an explicit display budget',()=>{
    const many=Array.from({length:8},(_,i)=>makeFvg({id:`fvg-${i}`,created_at:new Date(RECENT[i]*1000).toISOString()}));
    const rects=buildZoneRects({...base,candleTimes:new Set(RECENT),zones:many,orderBlocks:[],maxFvgs:3});
    expect(rects.map(r=>r.id)).toEqual(['fvg-5','fvg-6','fvg-7']);
  });
});

describe('zoneStyle',()=>{
  it('uses green fills for bullish zones and red fills for bearish zones',()=>{
    expect(zoneStyle('fvg','bullish').fill).toContain('40, 201, 151');
    expect(zoneStyle('fvg','bearish').fill).toContain('237, 100, 113');
    expect(zoneStyle('ob','bullish').fill).toContain('43, 159, 125');
    expect(zoneStyle('ob','bearish').fill).toContain('168, 99, 106');
  });
  it('keeps FVG and order-block styles distinguishable',()=>{
    expect(zoneStyle('fvg','bullish')).not.toEqual(zoneStyle('ob','bullish'));
  });
});

describe('clipZonePixels',()=>{
  it('produces a filled rectangle spanning both price boundaries',()=>{
    expect(clipZonePixels({x1:100,x2:300,yUpper:50,yLower:150,paneWidth:800,paneHeight:400})).toEqual({x:100,y:50,width:200,height:100});
  });
  it('extends to the pane right edge when the right boundary is open',()=>{
    expect(clipZonePixels({x1:100,x2:null,yUpper:50,yLower:150,paneWidth:800,paneHeight:400})).toEqual({x:100,y:50,width:700,height:100});
  });
  it('clips zones scrolled off the left edge instead of drifting',()=>{
    expect(clipZonePixels({x1:-250,x2:300,yUpper:50,yLower:150,paneWidth:800,paneHeight:400})?.x).toBe(0);
  });
  it('returns null when the zone is scrolled fully off-screen to the right',()=>{
    expect(clipZonePixels({x1:900,x2:null,yUpper:50,yLower:150,paneWidth:800,paneHeight:400})).toBeNull();
  });
  it('returns null when both price boundaries are outside the pane after a zoom',()=>{
    expect(clipZonePixels({x1:100,x2:300,yUpper:-80,yLower:-20,paneWidth:800,paneHeight:400})).toBeNull();
  });
  it('returns null for a zero-width zone or missing coordinates',()=>{
    expect(clipZonePixels({x1:200,x2:200,yUpper:50,yLower:150,paneWidth:800,paneHeight:400})).toBeNull();
    expect(clipZonePixels({x1:null,x2:300,yUpper:50,yLower:150,paneWidth:800,paneHeight:400})).toBeNull();
    expect(clipZonePixels({x1:100,x2:300,yUpper:null,yLower:150,paneWidth:800,paneHeight:400})).toBeNull();
  });
});
