import type { IndicatorSeries, Indicators, MarketData } from '../types';
export type Quote={symbol:string;instrument:string;timeframe:string;price:number;timestamp:string|null;is_stale:boolean;provider:string;data_source:string;data_status:string;quote_currency?:string;unit?:string};
export type SetupEvaluation={symbol:string;instrument:string;timeframe:string;market_data_status:string;source:string;analysis_timestamp:string;evaluation_timestamp:string;candle_timestamp:string|null;warnings:string[];direction:'BUY'|'SELL'|'NO_SETUP';setup_type:'MARKET_ENTRY'|'PULLBACK_ENTRY'|'BREAKOUT_ENTRY'|'RETEST_ENTRY'|null;current_price:number|null;data_freshness_seconds:number|null;entry_price:number|null;entry_zone:{lower:number;upper:number}|null;entry_trigger:string|null;stop_loss:number|null;take_profit_1:number|null;take_profit_2:number|null;take_profit_3?:number|null;take_profit_targets?:number[];risk_reward_ratio:number|null;risk_reward_tp1?:number|null;risk_reward_tp2?:number|null;risk_reward_tp3?:number|null;stop_basis?:string|null;setup_state?:string|null;state?:string|null;entry_type?:string|null;entry_trigger_conditions?:string[];trigger_candle_timestamp?:string|null;max_risk_percent:number;risk_amount:number|null;position_sizing_example:Record<string,unknown>|null;position_sizing_unavailable_reason?:string|null;supporting_indicators:Record<string,unknown>;supporting_smart_money_concepts:string[];invalidation_condition:string|null;reasons:string[];risks:string[];status:string;conditions_satisfied:string[];conditions_not_satisfied:string[];next_condition_to_watch?:string|null;explanation:string;ai_review:string|null;side:'BUY'|'SELL';market_bias:string;entry:number|null;take_profit:number|null;risk_reward:number|null;confirmation_complete?:boolean|null;invalidation_boundary?:Record<string,unknown>|null;liquidity_sweeps?:Record<string,unknown>[];late_entry?:boolean|null;late_entry_reason?:string|null;already_invalidated?:boolean|null;current_price_vs_entry?:Record<string,unknown>|null;provider:string;data_status:string;invalidation_conditions:string[]};
export type ApiError=Error&{code?:string;retryAfterSeconds?:number|null};
export type AiStatus={configured:boolean;model_configured:boolean;verified:boolean;verification_status:string;verified_at:string|null;last_success_at?:string|null;message:string;model:string|null;provider?:string;error_code?:string|null;retry_after_seconds?:number|null;state?:string;label?:string;ai_state?:string;ai_state_label?:string;in_flight?:boolean;verification_lifetime_seconds?:number;checked_at?:string;checks?:{configuration:{present:boolean;provider:string;model:string|null};provider_connectivity:{verified:boolean;verified_at:string|null;verified_expires_at:number|null;last_success_at:string|null};generation:{last_success_at:string|null;recent:boolean};quota:{exhausted:boolean;rate_limited:boolean;error_code:string|null;retry_after_seconds:number|null;message:string|null}}};
export type MarketSessionData={symbol:string;market_status:string;session_summary:string;session:{symbol:string;market:string;asset_class:string;venue:string|null;venue_timezone:string|null;schedule:string|null;schedule_verified:boolean;schedule_source:string|null;schedule_note:string|null;session_basis?:string|null;status:string;status_label:string;session_source:string;reason:string;is_weekend:boolean|null;utc_time:string;venue_time:string|null;venue_time_label:string|null;sessions:{name:string;timezone:string;status:string;local_time:string;window_local:string}[];active_sessions:string[];next_open:string|null;next_close:string|null;holiday_calendar:string};
// Explicit session contract (STEP 3): the same fields are mirrored at the top
// level of the payload so the UI never has to dig into ``session`` to read them.
session_status?:string;source?:string;checked_at?:string;next_open?:string|null;next_close?:string|null;timezone?:string;explanation?:string;
data:{provider:string|null;configured:boolean|null;connection_status:string|null;data_status:string|null;streaming:boolean|null;quote_available:boolean;last_update:string|null;last_data_timestamp:string|null;freshness_seconds:number|null;message:string|null}};
// Quote and candle feeds are described separately: a provider can aggregate two
// upstreams (XAUS: a spot quote and an exchange OHLCV chart feed), so the UI
// labels each price with the source it actually came from.
export type MarketSources={provider:string;quote:{provider:string;source:string|null;detail:string|null;as_of:string|null};candles:{provider:string;detail:string|null}};
type ApiRequestOptions=RequestInit&{timeoutMs?:number};
// Interactive AI generation is bounded by the backend deadline
// (GEMINI_TOTAL_DEADLINE_SECONDS, default 30s). The client waits a little
// longer so a slow backend response is preferred over a premature abort, but it
// still never waits forever.
const AI_REQUEST_TIMEOUT_MS=60000;
// In production (GitHub Pages), the frontend is served from a different origin
// than the backend. VITE_API_BASE_URL must be set to the backend's public HTTPS
// URL (e.g. https://your-backend.onrender.com). In local development it is left
// empty so relative API paths go through the Vite dev proxy to the backend.
const API_BASE_URL=import.meta.env.VITE_API_BASE_URL||'';
const request=async<T>(url:string,init?:ApiRequestOptions):Promise<T>=>{
  const fullUrl=API_BASE_URL?`${API_BASE_URL}${url}`:url;
 const {timeoutMs,...rest}=init||{};
 const controller=new AbortController();
 const external=rest.signal;
 if(external){if(external.aborted)controller.abort();else external.addEventListener('abort',()=>controller.abort(),{once:true});}
 const timer=timeoutMs?window.setTimeout(()=>controller.abort(),timeoutMs):undefined;
 let r:Response;
 try{
  r=await fetch(fullUrl,{...rest,signal:controller.signal});
 }catch(error){
  const aborted=(error as Error|undefined)?.name==='AbortError'||controller.signal.aborted;
  const err=new Error(aborted
   ?`The request exceeded ${Math.round((timeoutMs??0)/1000)}s and was cancelled. The AI provider may be slow; please try again.`
   :'Could not reach the backend service. Confirm the FastAPI server is running on port 8000 and try again.') as ApiError;
  err.code=aborted?'AI_TIMEOUT':'NETWORK_ERROR';
  err.retryAfterSeconds=null;
  throw err;
 }finally{if(timer!==undefined)window.clearTimeout(timer)}
 if(!r.ok){
  const e=await r.json().catch(()=>({} as {detail?:unknown;error?:{code?:string;message?:string}}));
  const detail=typeof e?.detail==='string'?e.detail:typeof (e?.detail as {message?:string}|undefined)?.message==='string'?(e.detail as {message:string}).message:typeof e?.error?.message==='string'?e.error.message:'Request failed';
  const err=new Error(detail) as ApiError;
  const code=r.headers.get('X-AI-Error-Code')||(typeof e?.error?.code==='string'?e.error.code:undefined);
  if(code)err.code=code;
  const retryAfter=r.headers.get('Retry-After');
  err.retryAfterSeconds=retryAfter!=null&&retryAfter!==''?Number(retryAfter):null;
  throw err;
 }
 return r.json();
};
export const api={
 health:()=>request<{status:string;ai_configured:boolean;ai_verified:boolean;data_provider:string;data_status:string}>('/api/v1/health'),
 marketStatus:(symbol:string)=>request<{symbol:string;provider:string;configured:boolean;connection_status:string;data_status:string;streaming:boolean;last_update:string|null;last_data_timestamp:string|null;freshness_seconds:number|null;price:number|null;quote_available:boolean;quote_status?:string;quote_source?:string|null;sources?:MarketSources;available_timeframes:string[];timeframe_note:string;message:string}>(`/api/v1/market-data/status?symbol=${encodeURIComponent(symbol)}`),
 quote:(symbol:string,timeframe:string)=>request<{symbol:string;instrument:string;timeframe:string;price:number;timestamp:string|null;is_stale:boolean;provider:string;data_source:string;data_status:string;quote_currency?:string;unit?:string}>(`/api/v1/market-data/quote?${new URLSearchParams({symbol,timeframe})}`),
 marketSession:(symbol:string)=>request<MarketSessionData>(`/api/v1/market/session?symbol=${encodeURIComponent(symbol)}`),
 marketInstruments:()=>request<{symbol:string;name:string;asset_class:string;provider:string;provider_symbol?:string;data_status:string;connection_status:string;configured:boolean;streaming:boolean;last_update:string|null;price:number|null;quote_available:boolean;message:string}[]>('/api/v1/market-data/instruments'),
 aiStatus:()=>request<AiStatus>('/api/v1/ai/status'),
 verifyAi:()=>request<AiStatus>('/api/v1/ai/verify',{method:'POST',timeoutMs:AI_REQUEST_TIMEOUT_MS}),
 snapshot:(symbol:string,timeframe:string)=>request<MarketData&{indicator_result:{symbol:string;timeframe:string;indicators:Indicators;series:IndicatorSeries;candle_timestamp:string|null};candle_freshness?:{status:string;latest_candle_timestamp:string|null;age_seconds:number|null;bar_seconds:number|null;stale_threshold_seconds:number|null;candle_count:number};fvgs:{id:string;instrument:string;timeframe:string;direction:string;lower:number;upper:number;status:string;created_at:string;source_candle_timestamps:string[]}[];structure:{support_clusters:{price:number;touches:number}[];resistance_clusters:{price:number;touches:number}[];breaks:{id:string;direction:string;type:string;level:number;confirmed_at:string}[];liquidity_sweeps:{direction:string;level:number;timestamp:string}[]};order_blocks:{id:string;instrument:string;timeframe:string;direction:string;lower:number;upper:number;status:string;created_at:string;confirmed_at:string|null}[];setup:{status:string;explanation:string;conditions_satisfied:string[];conditions_not_satisfied:string[]}}>(`/api/v1/analysis/snapshot?${new URLSearchParams({symbol,timeframe})}`),
 candles:(symbol:string,timeframe:string)=>request<MarketData>(`/api/v1/market-data/candles?${new URLSearchParams({symbol,timeframe})}`),
  candleHistory:(symbol:string,timeframe:string,opts?:{limit?:number;start?:string;end?:string})=>{
    const params:Record<string,string>={symbol,timeframe};
    if(opts?.limit)params.limit=String(opts.limit);
    if(opts?.start)params.start=opts.start;
    if(opts?.end)params.end=opts.end;
    return request<MarketData>(`/api/v1/market-data/history?${new URLSearchParams(params)}`);
  },
 indicators:(symbol:string,timeframe:string)=>request<{symbol:string;instrument:string;timeframe:string;provider:string;indicators:Indicators;series:IndicatorSeries;data_status:string;candle_timestamp:string|null;calculated_at:string;warnings:string[]}>(`/api/v1/indicators?${new URLSearchParams({symbol,timeframe})}`),
 fvgs:(symbol:string,timeframe:string)=>request<{zones:{id:string;instrument:string;timeframe:string;direction:string;lower:number;upper:number;status:string;created_at:string;source_candle_timestamps:string[]}[];rule:string}>(`/api/v1/analysis/fvgs?${new URLSearchParams({symbol,timeframe})}`),
 structure:(symbol:string,timeframe:string)=>request<{support_clusters:{price:number;touches:number}[];resistance_clusters:{price:number;touches:number}[];breaks:{id:string;direction:string;type:string;level:number;confirmed_at:string}[];liquidity_sweeps:{direction:string;level:number;timestamp:string}[]}>(`/api/v1/analysis/structure?${new URLSearchParams({symbol,timeframe})}`),
 orderBlocks:(symbol:string,timeframe:string)=>request<{zones:{id:string;instrument:string;timeframe:string;direction:string;lower:number;upper:number;status:string;created_at:string;confirmed_at:string|null}[];rule:string}>(`/api/v1/analysis/order-blocks?${new URLSearchParams({symbol,timeframe})}`),
 setup:(symbol:string,timeframe:string)=>request<{status:string;explanation:string;conditions_satisfied:string[];conditions_not_satisfied:string[]}>(`/api/v1/analysis/setup?symbol=${symbol}&timeframe=${timeframe}`),
 evaluate:(symbol:string,timeframe:string,side:'BUY'|'SELL',signal?:AbortSignal)=>request<SetupEvaluation>('/api/v1/analysis/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol,timeframe,side}),signal,timeoutMs:45000}),
 positionSize:(body:unknown)=>request<{simulated:boolean;risk_amount:string;quantity:string;warning:string}>('/api/v1/risk/position-size',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
 brokers:()=>request<{name:string;integration_type:string;authentication:string;supported_instruments:string[];market_data_operations:string[];account_operations:string[];order_operations:string[];required_configuration:string[];status:string;documentation_url:string|null;live_execution:boolean}[]>('/api/v1/brokers'),
 tradingView:()=>request<{status:string;integration_type:string;chart_widget_enabled:boolean;account_authenticated:boolean;account_data_access:boolean;capabilities:string[];limitations:string[];documentation_url:string;link_url:string}>('/api/v1/integrations/tradingview'),
 history:()=>request<{label:string;orders:{id:string;symbol:string;side:string;quantity:string;status:string;average_price:string;timestamp:string;fees?:string;slippage?:string;realized_pnl?:string;stop_loss?:string|null;take_profit?:string|null}[];positions:{symbol:string;side:string;quantity:string;unit:string;quote_currency:string;average_entry:string;mark_price:string;realized_pnl:string;unrealized_pnl:string;stop_loss:string|null;take_profit:string|null;label:string}[];accounts_by_currency:Record<string,{initial_balance:string;equity:string;available_simulated_funds:string;realized_pnl:string;unrealized_pnl:string;fees:string;slippage_model_bps:string;quote_currency:string}>;fees_model:{rate:string;slippage_bps:string};accounting_status:string}>('/api/v1/paper/history'),
 order:(body:unknown)=>request<{id:string;status:string;average_price:string}>('/api/v1/paper/orders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
 chat:(message:string,symbol:string,timeframe:string,include_market_context=true,history:{role:'user'|'assistant';text:string}[]=[])=>request<{reply:string;data_status:string;context?:unknown;ai_status?:AiStatus;market_session?:MarketSessionData['session'];session_summary?:string}>('/api/v1/ai/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message,symbol,timeframe,include_market_context,history}),timeoutMs:AI_REQUEST_TIMEOUT_MS}),
};
