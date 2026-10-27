import {describe,expect,it,vi} from 'vitest';
import {ZonePrimitive} from './zonePrimitive';
import type {ZoneRect} from './zoneRects';

const fvg:ZoneRect={id:'fvg-1',kind:'fvg',direction:'bullish',lower:1.1000,upper:1.1010,startTime:1000,endTime:null,label:'Bullish FVG',status:'unmitigated',mitigated:false};
const ob:ZoneRect={id:'ob-1',kind:'ob',direction:'bearish',lower:1.0990,upper:1.1020,startTime:1000,endTime:null,label:'Bearish OB',status:'confirmed',mitigated:false};
const obInvalidated:ZoneRect={...ob,id:'ob-2',label:'Bearish OB · invalidated',status:'invalidated'};

interface HarnessOptions{paneWidth?:number;paneHeight?:number;timeMap?:Record<number,number|null>;priceMap?:Record<number,number|null>;}

function harness(zones:ZoneRect[],options:HarnessOptions={}){
  const paneWidth=options.paneWidth??800;
  const paneHeight=options.paneHeight??400;
  const timeMap=options.timeMap??{1000:100};
  const priceMap=options.priceMap??{1.1010:50,1.1000:150,1.0990:50,1.1020:150};
  const ctx:any={fillRect:vi.fn(),strokeRect:vi.fn(),fillText:vi.fn(),setLineDash:vi.fn()};
  const target:any={useMediaCoordinateSpace:vi.fn((callback:(scope:unknown)=>unknown)=>callback({context:ctx,mediaSize:{width:paneWidth,height:paneHeight}}))};
  const chart:any={timeScale:()=>({timeToCoordinate:(time:number)=>(time in timeMap?timeMap[time]:null)})};
  const series:any={priceToCoordinate:(price:number)=>(price in priceMap?priceMap[price]:null)};
  const requestUpdate=vi.fn();
  const primitive=new ZonePrimitive(zones);
  primitive.attached({chart,series,requestUpdate,horzScaleBehavior:{} as any} as any);
  const draw=()=>primitive.paneViews()[0].renderer()!.draw(target);
  return {primitive,ctx,target,requestUpdate,draw};
}

describe('ZonePrimitive',()=>{
  it('renders in the background layer so candles stay visible',()=>{
    const {primitive}=harness([fvg]);
    expect(primitive.paneViews()[0].zOrder?.()).toBe('bottom');
    expect(primitive.paneViews()[0].renderer()).not.toBeNull();
  });
  it('fills a rectangle spanning both price boundaries and extending to the right pane edge',()=>{
    const {ctx,draw}=harness([fvg]);
    draw();
    expect(ctx.fillRect).toHaveBeenCalledTimes(1);
    expect(ctx.fillRect).toHaveBeenCalledWith(100,50,700,100);
    expect(ctx.strokeRect).toHaveBeenCalledTimes(1);
    expect(ctx.fillText).toHaveBeenCalledWith('Bullish FVG',104,53);
  });
  it('uses the provided right boundary when the zone is not open-ended',()=>{
    const {ctx,draw}=harness([{...fvg,endTime:300}],{timeMap:{1000:100,300:300}});
    draw();
    expect(ctx.fillRect).toHaveBeenCalledWith(100,50,200,100);
  });
  it('draws FVG and order blocks with their own colours',()=>{
    const {ctx,draw}=harness([fvg,ob]);
    draw();
    expect(ctx.fillRect).toHaveBeenCalledTimes(2);
  });
  it('dashes invalidated order blocks and draws active ones solid',()=>{
    const {ctx,draw}=harness([ob,obInvalidated]);
    draw();
    expect(ctx.fillRect).toHaveBeenCalledTimes(2);
    expect(ctx.setLineDash).toHaveBeenCalledWith([4,3]);
    expect(ctx.setLineDash).toHaveBeenCalledWith([]);
  });
  it('clips to a resized pane instead of using stale coordinates',()=>{
    const {ctx,draw}=harness([fvg],{paneWidth:200,timeMap:{1000:100}});
    draw();
    expect(ctx.fillRect).toHaveBeenCalledWith(100,50,100,100);
  });
  it('skips zones scrolled fully off-screen',()=>{
    const {ctx,draw}=harness([fvg],{timeMap:{1000:900}});
    draw();
    expect(ctx.fillRect).not.toHaveBeenCalled();
  });
  it('skips zones whose price boundaries are off-screen',()=>{
    const {ctx,draw}=harness([fvg],{priceMap:{1.1010:null,1.1000:null}});
    draw();
    expect(ctx.fillRect).not.toHaveBeenCalled();
  });
  it('replaces the zone set on setZones and requests a redraw',()=>{
    const {primitive,requestUpdate,ctx,draw}=harness([fvg]);
    primitive.setZones([ob]);
    expect(requestUpdate).toHaveBeenCalledTimes(1);
    expect(primitive.zones).toEqual([ob]);
    draw();
    expect(ctx.fillRect).toHaveBeenCalledTimes(1);
  });
  it('does not duplicate overlays after repeated updates',()=>{
    const {primitive,ctx,draw}=harness([fvg]);
    primitive.setZones([fvg]);
    primitive.setZones([fvg]);
    draw();
    expect(ctx.fillRect).toHaveBeenCalledTimes(1);
  });
  it('clears every zone when drawings are turned off',()=>{
    const {primitive,ctx,target,draw}=harness([fvg,ob]);
    primitive.clear();
    expect(primitive.zones).toEqual([]);
    draw();
    expect(target.useMediaCoordinateSpace).not.toHaveBeenCalled();
    expect(ctx.fillRect).not.toHaveBeenCalled();
  });
  it('labels short zones just above the box instead of overflowing it',()=>{
    const {ctx,draw}=harness([fvg],{priceMap:{1.1010:100,1.1000:106}});
    draw();
    expect(ctx.fillRect).toHaveBeenCalledWith(100,100,700,6);
    expect(ctx.fillText).toHaveBeenCalledWith('Bullish FVG',104,98);
    expect(ctx.textBaseline).toBe('bottom');
  });
  it('does not label zones too narrow to be readable',()=>{
    const {ctx,draw}=harness([{...fvg,endTime:200}],{timeMap:{1000:100,200:150}});
    draw();
    expect(ctx.fillRect).toHaveBeenCalledTimes(1);
    expect(ctx.fillText).not.toHaveBeenCalled();
  });
  it('stops drawing after detach (chart cleanup)',()=>{
    const {primitive,target,draw}=harness([fvg]);
    primitive.detached();
    draw();
    expect(target.useMediaCoordinateSpace).not.toHaveBeenCalled();
    expect(primitive.chart).toBeNull();
    expect(primitive.series).toBeNull();
  });
});
