import {useEffect,useRef,useState} from 'react';
import type {FormEvent} from 'react';
import {Activity, AlertTriangle, ArrowDownRight, ArrowUpRight, BarChart3, Bell, Bot, CircleHelp, Command, Globe, LayoutDashboard, Maximize2, Minimize2, PanelLeftClose, PanelLeftOpen, Plus, Send, Settings, ShieldCheck, Sparkles, X} from 'lucide-react';
import PriceChart from './components/PriceChart';
import ChartClock from './components/ChartClock';
import AIChatWorkspace,{type ChatConversation,type ChatMessage} from './components/AIChatWorkspace';
import MarkdownView from './components/MarkdownView';
import {mergeCandlesByTime,priceSourceMismatch} from './components/chartMapping';
import {api} from './services/api';
import type {Candle,IndicatorSeries,Indicators,MarketData} from './types';
import type {SetupEvaluation} from './services/api';
import type {MarketSessionData,MarketSources} from './services/api';
import {
  type TimezoneMode,
  DEFAULT_TIMEZONE_MODE,
  TIMEZONE_OPTIONS,
  resolveTimezone,
  getExchangeInfo,
  getTimezoneShortLabel,
  isIntradayTimeframe,
  formatTimeInTimezone,
  formatDateInTimezone,
  formatDateTimeInTimezone,
} from './utils/timezone';
import './analysis.css';

type Page='Dashboard'|'Markets'|'Charts'|'TradingView'|'AI Assistant'|'Technical Analysis'|'Paper Trading'|'Backtesting'|'Alerts'|'Trading Journal'|'Broker Connections'|'Settings';
const nav:{name:Page;icon:typeof Activity}[]=[{name:'Dashboard',icon:LayoutDashboard},{name:'AI Assistant',icon:Bot},{name:'Settings',icon:Settings}];
const fmt=(v:number|null|undefined,d=5)=>v==null?'—':v.toLocaleString(undefined,{maximumFractionDigits:d});
// Indicators that cannot be computed (for example EMA-50/MACD when the plan
// only supplies ~21 daily bars) are labelled "Unavailable" instead of showing a
// dash that could be mistaken for a real value.
const indVal=(v:number|null|undefined,d=2)=>v==null?'Unavailable':fmt(v,d);
const frameSeconds:Record<string,number>={'1m':60,'3m':180,'5m':300,'15m':900,'30m':1800,'1h':3600,'4h':14400,'1d':86400,'1w':604800};
function candleBucketStart(instant:number,frame:string,_symbol?:string){const step=frameSeconds[frame];if(frame==='1w')return Math.floor((instant+259200)/step)*step-259200;return Math.floor(instant/step)*step}
const starting:Indicators={ema9:null,ema20:null,ema50:null,ema200:null,sma20:null,rsi14:null,macd:{line:null,signal:null,histogram:null},atr14:null,bollinger20:{middle:null,upper:null,lower:null},vwap:null};
// Canonical assistant states returned by the backend (/api/v1/ai/status -> ai_state).
// These are the only states the UI renders; the finer `verification_status` is
// used for the detailed message only. Configuration presence is deliberately
// shown separately from a verified provider request.
const AI_STATE_LABELS:Record<string,string>={CHECKING:'Checking…',VERIFIED:'Verified',UNVERIFIED:'Not verified',EXPIRED:'Verification expired',ERROR:'Verification error',QUOTA_EXCEEDED:'Quota exhausted',CONFIGURATION_ERROR:'Configuration error'};
const AI_FAILURE_STATES=['ERROR','QUOTA_EXCEEDED','CONFIGURATION_ERROR','BACKEND_UNAVAILABLE'];
const AI_STATUS_LABELS:Record<string,string>={VERIFIED:'Verified',CONNECTING:'Not verified',NOT_CONFIGURED:'Not configured',QUOTA_EXCEEDED:'Quota exhausted',RATE_LIMITED:'Rate limited',INVALID_KEY:'Invalid API key',MODEL_UNAVAILABLE:'Model unavailable',INVALID_REQUEST:'Invalid request',TIMEOUT:'Request timed out',PROVIDER_UNAVAILABLE:'Provider unavailable',NETWORK_ERROR:'Network error',ERROR:'Provider error',BACKEND_UNAVAILABLE:'Server unavailable'};
function aiStatusLabel(status:string){return AI_STATUS_LABELS[status]??(status?status.replaceAll('_',' '):'Unknown')}
const CHAT_STORAGE_KEY='trademind.ai.conversations.v1';
function sanitizeMessages(messages: ChatMessage[], provider = 'gemini'): ChatMessage[] {
 return (messages || []).filter(m => {
  if (!m || typeof m.text !== 'string') return false;
  if (provider === 'gemini') {
   const lower = m.text.toLowerCase();
   if (lower.includes('openai') && (lower.includes('rate limit') || lower.includes('quota'))) {
    return false;
   }
  }
  return true;
 });
}
function restoreChats():ChatConversation[]{try{const value=JSON.parse(localStorage.getItem(CHAT_STORAGE_KEY)||'[]');if(Array.isArray(value)&&value.every(x=>x&&typeof x.id==='string'&&Array.isArray(x.messages))&&value.length)return (value as ChatConversation[]).map(c=>({...c,messages:sanitizeMessages(c.messages)}));}catch{}return [{id:'initial-chat',title:'New conversation',updatedAt:new Date().toISOString(),messages:[]}]}
export default function App(){
 const [page,setPage]=useState<Page>('Dashboard'),[symbol,setSymbol]=useState('XAUUSD'),[timeframe,setTimeframe]=useState('5m'),[market,setMarket]=useState<MarketData|null>(null),[marketStatus,setMarketStatus]=useState<{provider:string;configured:boolean;connection_status:string;data_status:string;streaming:boolean;last_update:string|null;last_data_timestamp:string|null;freshness_seconds:number|null;price:number|null;quote_available:boolean;quote_status?:string;quote_source?:string|null;sources?:MarketSources;available_timeframes:string[];timeframe_note:string;message:string}>({provider:'DEMO',configured:false,connection_status:'DISCONNECTED',data_status:'DEMO',streaming:false,last_update:null,last_data_timestamp:null,freshness_seconds:null,price:null,quote_available:false,available_timeframes:['1m','2m','3m','5m','15m','30m','1h','2h','4h','1d','1w'],timeframe_note:'',message:'Synthetic demonstration data is active.'}),[ind,setInd]=useState<Indicators>(starting),[indSeries,setIndSeries]=useState<IndicatorSeries>({ema20:[],ema50:[],vwap:[]}),[fvgs,setFvgs]=useState<{id:string;instrument:string;timeframe:string;direction:string;lower:number;upper:number;status:string;created_at:string;source_candle_timestamps:string[]}[]>([]),[orderBlocks,setOrderBlocks]=useState<{id:string;instrument:string;timeframe:string;direction:string;lower:number;upper:number;status:string;created_at:string;confirmed_at:string|null}[]>([]),[structure,setStructure]=useState<{support_clusters:{price:number;touches:number}[];resistance_clusters:{price:number;touches:number}[];breaks:{id:string;direction:string;type:string;level:number;confirmed_at:string}[];liquidity_sweeps:{direction:string;level:number;timestamp:string}[]}|null>(null),[setup,setSetup]=useState<{status:string;explanation:string;conditions_satisfied:string[];conditions_not_satisfied:string[]}|null>(null),[evaluation,setEvaluation]=useState<SetupEvaluation|null>(null),[analysisBusy,setAnalysisBusy]=useState(false),[analysisError,setAnalysisError]=useState(''),[lastEvaluatedSide,setLastEvaluatedSide]=useState<'BUY'|'SELL'|null>(null),[lastAnalysisTimestamp,setLastAnalysisTimestamp]=useState<string|null>(null),[drawings,setDrawings]=useState({fvgs:true,orderBlocks:false,supportResistance:false}),[error,setError]=useState(''),[health,setHealth]=useState(false),[aiProvider,setAiProvider]=useState('gemini'),[aiConfigured,setAiConfigured]=useState(false),[aiVerified,setAiVerified]=useState(false),[aiVerificationStatus,setAiVerificationStatus]=useState('not_verified');const [aiStateLabel,setAiStateLabel]=useState('');const [aiStateKey,setAiStateKey]=useState('UNVERIFIED'),[aiVerificationMessage,setAiVerificationMessage]=useState(''),[aiVerifying,setAiVerifying]=useState(false),[chatInput,setChatInput]=useState(''),[chatBusy,setChatBusy]=useState(false),[chat,setChat]=useState<ChatMessage[]>(()=>restoreChats()[0]?.messages??[]),[orders,setOrders]=useState<{id:string;symbol:string;side:string;quantity:string;status:string;average_price:string;timestamp:string}[]>([]),[paperPositions,setPaperPositions]=useState<Awaited<ReturnType<typeof api.history>>['positions']>([]),[paperAccounts,setPaperAccounts]=useState<Awaited<ReturnType<typeof api.history>>['accounts_by_currency']>({}),[quantity,setQuantity]=useState('1'),[orderType,setOrderType]=useState<'MARKET'|'LIMIT'>('MARKET'),[entryPrice,setEntryPrice]=useState(''),[stopLoss,setStopLoss]=useState(''),[takeProfit,setTakeProfit]=useState(''),[showOrder,setShowOrder]=useState(false),[enabled,setEnabled]=useState({ema20:true,ema50:false,vwap:false});
 const [chatConversations,setChatConversations]=useState<ChatConversation[]>(restoreChats),[activeChatId,setActiveChatId]=useState(()=>restoreChats()[0]?.id??''),[setupVisible,setSetupVisible]=useState(false),[reviewSide,setReviewSide]=useState<'BUY'|'SELL'|null>(null);
 const [timezoneMode,setTimezoneMode]=useState<TimezoneMode>(DEFAULT_TIMEZONE_MODE);
 const [isMaximized,setIsMaximized]=useState(false);
 const [sidebarCollapsed,setSidebarCollapsed]=useState(false);
  const [sessionInfo,setSessionInfo]=useState<MarketSessionData|null>(null);
  const [loadingOlder,setLoadingOlder]=useState(false);
  const [historyExhausted,setHistoryExhausted]=useState(false);
  const [jumpToken,setJumpToken]=useState(0);
 const resolvedTimezone=resolveTimezone(timezoneMode,symbol,market?.exchange_timezone);
 const timezoneShort=getTimezoneShortLabel(resolvedTimezone);
 const exchangeInfo=getExchangeInfo(symbol,market?.exchange_timezone);
 const skipChatSync=useRef(false);
 const chatListRef=useRef<HTMLDivElement>(null);
 const loadingOlderRef=useRef(false);
 const loadSequence=useRef(0);const timeframeRef=useRef(timeframe);const evalSeqRef=useRef(0);const evalAbortRef=useRef<AbortController|null>(null);const loadRef=useRef<((preserve?:boolean)=>Promise<void>)|null>(null);const streamBucket=useRef<number|null>(null);
 const chatBusyRef=useRef(false);const verifyingRef=useRef(false);const chatSeqRef=useRef(0);
 useEffect(()=>{timeframeRef.current=timeframe;},[timeframe]);
 useEffect(()=>{return()=>{evalAbortRef.current?.abort();};},[]);
 useEffect(()=>{
  if(!isMaximized)return;
  const onKeyDown=(e:KeyboardEvent)=>{if(e.key==='Escape')setIsMaximized(false)};
  window.addEventListener('keydown',onKeyDown);
  return()=>window.removeEventListener('keydown',onKeyDown);
 },[isMaximized]);
 useEffect(()=>{try{localStorage.setItem(CHAT_STORAGE_KEY,JSON.stringify(chatConversations))}catch{}},[chatConversations]);
 useEffect(()=>{const el=chatListRef.current;if(el)el.scrollTop=el.scrollHeight},[chat,chatBusy]);
  async function loadOlderCandles(){
    if(loadingOlderRef.current||historyExhausted)return;
    const current=market;
    if(!current||!current.candles.length)return;
    const oldest=current.candles[0].timestamp;
    loadingOlderRef.current=true;
    setLoadingOlder(true);
    try{
      const page=await api.candleHistory(symbol,timeframe,{limit:1000,end:oldest});
      if(symbol!==current.symbol||timeframe!==current.timeframe)return;
      console.info('[chart-history] fetched older candles',{symbol,timeframe,requestedBefore:oldest,received:page.candles.length,provider:page.provider,data_status:page.data_status,warnings:page.warnings});
      const older=page.candles.filter(c=>Date.parse(c.timestamp)<Date.parse(oldest));
      if(!older.length){
        setHistoryExhausted(true);
        return;
      }
      const merged=mergeCandlesByTime(older,current.candles);
      setMarket({...current,candles:merged.slice(-5000)});
    }catch(e){
      console.warn('[chart-history] older-candle fetch failed',(e as Error).message);
      setHistoryExhausted(true);
    }finally{
      loadingOlderRef.current=false;
      setLoadingOlder(false);
    }
  }
  useEffect(()=>{if(skipChatSync.current){skipChatSync.current=false;return}setChatConversations(old=>{const now=new Date().toISOString();const found=old.some(c=>c.id===activeChatId);const current=old.find(c=>c.id===activeChatId);const firstUser=chat.find(m=>m.role==='user')?.text;const updated:ChatConversation={id:activeChatId||'initial-chat',title:firstUser?(firstUser.length>36?firstUser.slice(0,36)+'...':firstUser):(current?.title??'New conversation'),updatedAt:now,messages:chat};return found?old.map(c=>c.id===activeChatId?updated:c):[updated,...old]})},[chat]);
  const candles: Candle[] = market?.candles ?? [];
  const latest = candles.at(-1);
  const prior = candles.at(-2);
  const isDemo = marketStatus.data_status === 'DEMO';
  const isValidInstrumentPrice = (p: number | null | undefined): p is number =>
    p != null && Number.isFinite(p) && (isDemo ? p > 0 : p >= 500 && p <= 20000);
  const quotePrice = (marketStatus.price != null && Number.isFinite(marketStatus.price) && isValidInstrumentPrice(marketStatus.price))
    ? marketStatus.price
    : null;
  const lastCompletedClose = (latest?.close != null && Number.isFinite(latest.close) && isValidInstrumentPrice(latest.close))
    ? latest.close
    : null;
  const headerPrice = quotePrice ?? lastCompletedClose;

  // Calculate percentage change ONLY between two valid, comparable prices
  const previousPrice = (prior?.close != null && isValidInstrumentPrice(prior.close))
    ? prior.close
    : null;

  const isScaleCompatible = (
    headerPrice != null &&
    previousPrice != null &&
    isValidInstrumentPrice(headerPrice) &&
    previousPrice > 0 &&
    (previousPrice / headerPrice >= 0.5 && previousPrice / headerPrice <= 2.0)
  );

  const change = isScaleCompatible && headerPrice != null && previousPrice != null
    ? headerPrice - previousPrice
    : null;
  const pct = isScaleCompatible && change != null && previousPrice != null
    ? (change / previousPrice) * 100
    : null;
  // The header price and the chart's last-price line share one validated quote
  // (see PriceChart). If the quote and the candle feed disagree - XAUS serves a
  // spot quote and an exchange OHLCV chart feed from different upstreams - the
  // divergence is disclosed instead of hidden.
  const sourceMismatch=priceSourceMismatch(quotePrice,latest?.close);
  const quoteSourceDetail=marketStatus.sources?.quote?.detail??null;
  const candleSourceDetail=marketStatus.sources?.candles?.detail??null;
  // Diagnostics: record the four numbers the chart reconciles - the validated
  // quote (price + time) and the newest candle (close + time) - plus which feed
  // each came from. A quote/candle divergence is legitimate (XAUS serves a spot
  // quote and an exchange OHLCV feed) but must be traceable, never mysterious.
  useEffect(()=>{
    if(latest==null&&quotePrice==null)return;
    console.debug('[chart-diag]',{
      symbol,timeframe,
      quotePrice:quotePrice??null,
      quoteTimestamp:marketStatus.last_update??null,
      candleClose:latest?.close??null,
      candleTimestamp:latest?.timestamp??null,
      quoteVsCandle:sourceMismatch.mismatch,
      difference:sourceMismatch.difference,
      quoteSource:quoteSourceDetail,
      candleSource:candleSourceDetail,
    });
  },[symbol,timeframe,quotePrice,marketStatus.last_update,latest?.timestamp,latest?.close,sourceMismatch.mismatch,sourceMismatch.difference,quoteSourceDetail,candleSourceDetail]);
 const fallbackTimeframes=['1m','2m','3m','5m','15m','30m','1h','2h','4h','1d','1w'];
 const availableTimeframes=(marketStatus.available_timeframes&&marketStatus.available_timeframes.length)?marketStatus.available_timeframes:fallbackTimeframes;
 const timeframeKey=(marketStatus.available_timeframes||[]).join(',');
 useEffect(()=>{const list=marketStatus.available_timeframes||[];if(list.length&&!list.includes(timeframe))setTimeframe(list[0])},[timeframeKey,timeframe]);
 const load=async(preserve=false)=>{const sequence=++loadSequence.current;setError('');if(!preserve){setMarket(old=>(old&&old.symbol===symbol&&old.timeframe===timeframe?old:null))}try{const snapshot=await api.snapshot(symbol,timeframe);if(sequence!==loadSequence.current)return;const invalidCandle=snapshot.candles.some(c=>c.instrument!==symbol||c.timeframe!==timeframe||c.data_source!==snapshot.data_source);const invalidZone=snapshot.fvgs.some(z=>z.instrument!==symbol||z.timeframe!==timeframe);const invalidBlock=snapshot.order_blocks.some(z=>z.instrument!==symbol||z.timeframe!==timeframe);if(snapshot.symbol!==symbol||snapshot.timeframe!==timeframe||snapshot.indicator_result.symbol!==symbol||snapshot.indicator_result.timeframe!==timeframe||snapshot.indicator_result.candle_timestamp!==snapshot.last_successful_timestamp||invalidCandle||invalidZone||invalidBlock)throw new Error('Received mixed or mismatched instrument/timeframe analysis; results were cleared.');if(!snapshot.candles.length){setMarket(snapshot);setInd(starting);setIndSeries({ema20:[],ema50:[],vwap:[]});setFvgs([]);setOrderBlocks([]);setStructure(null);setSetup(null);throw new Error(snapshot.warnings[0]??'Historical market data is unavailable for this instrument and timeframe.')}setMarket(old=>preserve&&old&&old.symbol===snapshot.symbol&&old.timeframe===snapshot.timeframe?{...snapshot,candles:mergeCandlesByTime(old.candles,snapshot.candles)}:snapshot);setInd(snapshot.indicator_result.indicators);setIndSeries(snapshot.indicator_result.series);setFvgs(snapshot.fvgs);setSetup(snapshot.setup);setOrderBlocks(snapshot.order_blocks);setStructure(snapshot.structure);const last=snapshot.candles.at(-1);streamBucket.current=last?candleBucketStart(Math.floor(Date.parse(last.timestamp)/1000),timeframe,symbol):null}catch(e){if(sequence===loadSequence.current)setError((e as Error).message)}};useEffect(()=>{timeframeRef.current=timeframe;loadRef.current=load});
 async function refreshPaper(){try{const history=await api.history();setOrders(history.orders);setPaperPositions(history.positions);setPaperAccounts(history.accounts_by_currency)}catch{}}
 async function refreshSession(sym:string){try{const s=await api.marketSession(sym);setSessionInfo(s)}catch{setSessionInfo(null)}}
 useEffect(()=>{api.health().then(()=>{setHealth(true)}).catch(()=>{setHealth(false)});void refreshAiStatus();void refreshPaper()},[]);
 // Single source of truth: every AI state update flows through applyAiStatus,
 // which reads the canonical `ai_state` returned by the backend. No component
 // keeps an independent verification state.
 function applyAiStatus(status:{provider?:string;configured:boolean;verified:boolean;verification_status:string;message:string;ai_state?:string;ai_state_label?:string}){
  if(status.provider)setAiProvider(status.provider);
  setAiConfigured(status.configured);
  setAiVerified(status.verified);
  setAiVerificationStatus(status.verification_status);
  // Prefer the canonical state; fall back to the legacy verification_status so a
  // partially hydrated payload never displays a wrong state.
  const canonical=status.ai_state||(status.verified?'VERIFIED':status.verification_status||'UNVERIFIED');
  setAiStateKey(canonical);
  setAiStateLabel(status.ai_state_label||AI_STATE_LABELS[canonical]||'');
  setAiVerificationMessage(status.message);
 }
 async function refreshAiStatus(){try{const status=await api.aiStatus();applyAiStatus(status)}catch{setAiVerified(false);setAiVerificationStatus('BACKEND_UNAVAILABLE');setAiStateLabel('Server unavailable');setAiStateKey('BACKEND_UNAVAILABLE');setAiVerificationMessage('Backend unreachable. Make sure the FastAPI server is running on port 8000.')}}
 async function verifyAi(prov?:string){if(verifyingRef.current)return;verifyingRef.current=true;const target=prov||aiProvider||'gemini';const label=target==='gemini'?'Gemini':target==='openai'?'OpenAI':'AI';setAiVerifying(true);setAiVerificationMessage(`Checking ${label} with a real authenticated request...`);try{const status=await api.verifyAi();applyAiStatus(status)}catch(err){setAiVerified(false);setAiVerificationStatus('ERROR');setAiStateLabel('Provider error');setAiStateKey('ERROR');setAiVerificationMessage((err as Error).message)}finally{verifyingRef.current=false;setAiVerifying(false)}}
 const firstLoad = useRef<string>('');
  useEffect(()=>{
    const key = symbol + '::' + timeframe;
    if(firstLoad.current===key)return;
    firstLoad.current=key;
    setHistoryExhausted(false);
    void load(false);
  },[symbol,timeframe]);
 useEffect(()=>{setSessionInfo(null);void refreshSession(symbol);const sessionTimer=window.setInterval(()=>{if(!document.hidden)void refreshSession(symbol)},60000);return()=>clearInterval(sessionTimer)},[symbol]);
 useEffect(()=>{let active=true;let socket:WebSocket|undefined;let retry:number|undefined;let retryCount=0;let retryable=true;const pollStatus=()=>api.marketStatus(symbol).then(s=>{if(active)setMarketStatus(s)}).catch(()=>{if(active)setMarketStatus(current=>({...current,connection_status:'ERROR',data_status:'ERROR',message:'Market-data status could not be loaded.'}))});pollStatus();const statusTimer=window.setInterval(()=>{if(!document.hidden)pollStatus()},10000);
  const connect=()=>{if(!active||!retryable)return;if(socket&&(socket.readyState===WebSocket.OPEN||socket.readyState===WebSocket.CONNECTING))return;const scheme=location.protocol==='https:'?'wss':'ws';const apiBaseUrl=import.meta.env.VITE_API_BASE_URL||'';const wsBase=apiBaseUrl?apiBaseUrl.replace(/^http:/,'ws:').replace(/^https:/,'wss:'):`${scheme}://${location.host}`;socket=new WebSocket(`${wsBase}/ws/market-data?symbol=${encodeURIComponent(symbol)}`);socket.onerror=_e=>{if(active)setMarketStatus(s=>({...s,connection_status:'ERROR',message:'WebSocket connection failed. Polling for updates instead.'}))};socket.onmessage=e=>{if(!active)return;try{const event=JSON.parse(e.data);if(event.type==='error'){if(event.retryable===false)retryable=false;if(active)setMarketStatus(s=>({...s,connection_status:'ERROR',message:event.message??s.message}));return}if(event.type==='status'){if(event.retryable===false)retryable=false;setMarketStatus(s=>({...s,provider:event.provider??s.provider,configured:event.configured??s.configured,connection_status:event.connection_status??s.connection_status,data_status:event.data_status??s.data_status,quote_available:event.quote_available??s.quote_available,message:event.message??s.message}));return}if(event.type==='price'&&Number.isFinite(Number(event.price))){const stamp=new Date(event.timestamp);const bucket=candleBucketStart(Math.floor(stamp.getTime()/1000),timeframeRef.current,symbol);const timestamp=new Date(bucket*1000).toISOString();const price=Number(event.price);if(streamBucket.current===null)streamBucket.current=bucket;else if(bucket>streamBucket.current){streamBucket.current=bucket;void loadRef.current?.(true)}setMarket(old=>{if(!old)return old;const rows=[...old.candles];const tail=rows.at(-1);if(tail&&candleBucketStart(Math.floor(Date.parse(tail.timestamp)/1000),timeframeRef.current,symbol)===bucket){rows[rows.length-1]={...tail,high:Math.max(tail.high,price),low:Math.min(tail.low,price),close:price}}else rows.push({timestamp,open:price,high:price,low:price,close:price,volume:null,volume_type:'unavailable',instrument:symbol,timeframe:timeframeRef.current,data_source:event.data_source,source:event.data_source,is_realtime:true});return {...old,candles:rows.slice(-5000),data_status:event.data_status??'LIVE',last_successful_timestamp:event.timestamp}});retryCount=0;setMarketStatus(s=>({...s,provider:event.provider,connection_status:'CONNECTED',data_status:event.data_status??'LIVE',streaming:(event.data_status??'LIVE')==='LIVE',last_update:event.timestamp,price:Number(event.price),message:event.data_status==='LIVE'?'Realtime price updates are being received.':`Provider sent a ${event.data_status??'UNKNOWN'} tick at its own timestamp; no live price is claimed.`}))}}catch{}};socket.onopen=()=>{retryCount=0};socket.onerror=()=>{if(active)setMarketStatus(s=>({...s,connection_status:'ERROR',data_status:'ERROR',message:'The provider stream failed. Historical data remains unchanged.'}))};socket.onclose=()=>{if(active){setMarketStatus(s=>({...s,connection_status:'DISCONNECTED',data_status:s.data_status==='LIVE'?'STALE':s.data_status,streaming:false}));if(retryable){const delay=Math.min(30000,1000*(2**retryCount++));retry=window.setTimeout(connect,delay)}}}};
  connect();return()=>{active=false;clearInterval(statusTimer);if(retry)clearTimeout(retry);socket?.close()}
 },[symbol,marketStatus.configured]);// ticks are symbol-scoped: a timeframe change must not reopen the socket
 useEffect(()=>{if(marketStatus.streaming)return;let active=true;let inFlight=false;const pollQuote=async()=>{if(inFlight)return;inFlight=true;try{const q=await api.quote(symbol,timeframe);if(!active)return;if(!Number.isFinite(q.price))return;setMarketStatus(s=>({...s,provider:q.provider??s.provider,price:q.price,quote_available:true,last_update:q.timestamp??s.last_update,last_data_timestamp:q.timestamp??s.last_data_timestamp,data_status:q.data_status??s.data_status,message:q.is_stale?'The provider flagged its latest quote as stale; it is not presented as live.':'Latest provider quote received.'}))}catch{/* Quote poll failures must not disable the Evaluate button; the main status poll handles status reporting. */}finally{inFlight=false}};void pollQuote();const quoteTimer=window.setInterval(()=>{if(!document.hidden)void pollQuote()},30000);return()=>{active=false;clearInterval(quoteTimer)}},[symbol,timeframe,marketStatus.streaming]);
useEffect(()=>{if(marketStatus.streaming)return;let active=true;const refresh=async()=>{try{const snapshot=await api.snapshot(symbol,timeframe);if(!active)return;setError('');setMarket(old=>old&&old.symbol===snapshot.symbol&&old.timeframe===snapshot.timeframe?{...snapshot,candles:mergeCandlesByTime(old.candles,snapshot.candles)}:snapshot);setInd(snapshot.indicator_result.indicators);setIndSeries(snapshot.indicator_result.series);setFvgs(snapshot.fvgs);setSetup(snapshot.setup);setOrderBlocks(snapshot.order_blocks);setStructure(snapshot.structure);const last=snapshot.candles.at(-1);streamBucket.current=last?candleBucketStart(Math.floor(Date.parse(last.timestamp)/1000),timeframe,symbol):null}catch(err){if(active)setMarketStatus(s=>({...s,data_status:'ERROR',connection_status:'ERROR',streaming:false,message:(err as Error).message}))}};const timer=window.setInterval(()=>{if(!document.hidden)void refresh()},120000);return()=>{active=false;clearInterval(timer)}},[marketStatus.streaming,symbol,timeframe]);
 const switchPage=(p:Page)=>setPage(p);
 async function sendChat(text:string,selectedSymbol=symbol,selectedTimeframe=timeframe,includeContext=true){
  if(chatBusy||chatBusyRef.current)return;
  const cleanText=text.trim();
  if(!cleanText)return;
  chatBusyRef.current=true;
  const requestId=activeChatId;
  const seq=++chatSeqRef.current;
  const contextSymbol=selectedSymbol,contextTimeframe=selectedTimeframe;
  const history=chat.filter(m=>!m.failed).slice(-8).map(m=>({role:m.role,text:m.text}));
  const userTurn:ChatMessage={role:'user',text:cleanText,createdAt:new Date().toISOString(),contextSymbol,contextTimeframe};
  const appendToConversation=(assistantTurn:ChatMessage)=>setChatConversations(old=>old.map(c=>{
   if(c.id!==requestId)return c;
   const hasUserTurn=c.messages.some(m=>m.role==='user'&&m.createdAt===userTurn.createdAt&&m.text===userTurn.text);
   return {...c,messages:[...c.messages,...(hasUserTurn?[]:[userTurn]),assistantTurn],updatedAt:new Date().toISOString()};
  }));
  setChat(x=>[...x,userTurn]);
  setChatBusy(true);
  try{
   const ans=await api.chat(cleanText,selectedSymbol,selectedTimeframe,includeContext,history);
   if(seq!==chatSeqRef.current)return;
   if(ans.ai_status)applyAiStatus(ans.ai_status);
   const assistantTurn:ChatMessage={role:'assistant',text:ans.reply,createdAt:new Date().toISOString(),contextSymbol,contextTimeframe};
   if(activeChatId===requestId)setChat(x=>[...x,assistantTurn]);
   else appendToConversation(assistantTurn);
  }catch(err){
   if(seq!==chatSeqRef.current)return;
   const assistantTurn:ChatMessage={role:'assistant',text:(err as Error).message,createdAt:new Date().toISOString(),contextSymbol,contextTimeframe,failed:true};
   if(activeChatId===requestId)setChat(x=>[...x,assistantTurn]);
   else appendToConversation(assistantTurn);
  }finally{
   void refreshAiStatus();
   chatBusyRef.current=false;
   setChatBusy(false);
  }
 }
 async function ask(e:FormEvent){e.preventDefault();const text=chatInput.trim();if(!text||chatBusy||chatBusyRef.current)return;setChatInput('');void sendChat(text,symbol,timeframe,true)}
 function newConversation(){const id=`chat-${crypto.randomUUID()}`;const item:ChatConversation={id,title:'New conversation',updatedAt:new Date().toISOString(),messages:[]};setChatConversations(old=>[item,...old]);skipChatSync.current=true;setActiveChatId(id);setChat([])}
 function selectConversation(id:string){if(id===activeChatId)return;const found=chatConversations.find(c=>c.id===id);if(!found)return;skipChatSync.current=true;setActiveChatId(id);setChat(found.messages)}
 function clearConversation(){if(!window.confirm('Clear all messages in this conversation?'))return;setChat([])}
 async function submitOrder(side:'BUY'|'SELL'){try{await api.order({symbol,side,quantity,order_type:orderType,...(orderType==='LIMIT'?{entry_price:entryPrice}:{}),...(stopLoss?{stop_loss:stopLoss}:{}),...(takeProfit?{take_profit:takeProfit}:{})});await refreshPaper();setShowOrder(false);setReviewSide(null)}catch(e){setError((e as Error).message)}}
 async function evaluate(side:'BUY'|'SELL'){
  if(analysisBusy)return;
  setAnalysisBusy(true);
  setLastEvaluatedSide(side);
  setAnalysisError('');
  setSetupVisible(false);
  const seq=++evalSeqRef.current;
  const reqSymbol=symbol;
  const reqTf=timeframeRef.current||timeframe;
  try{
   const result=await api.evaluate(reqSymbol,reqTf,side);
   if(seq!==evalSeqRef.current)return;
   setEvaluation(result);
   setSetupVisible(false);
   setLastAnalysisTimestamp(new Date().toISOString());
  }catch(e:any){
   if(seq!==evalSeqRef.current)return;
   setAnalysisError((e as Error).message||'Evaluation request failed');
  }finally{
   if(seq===evalSeqRef.current){
    setAnalysisBusy(false);
   }
  }
 }
 function reviewSetup(){if(!evaluation||!['BUY','SELL'].includes(evaluation.direction)||evaluation.entry_price==null||evaluation.stop_loss==null||evaluation.take_profit_1==null)return;setSymbol(evaluation.symbol);setReviewSide(evaluation.direction as 'BUY'|'SELL');setOrderType('LIMIT');setEntryPrice(String(evaluation.entry_price));setStopLoss(String(evaluation.stop_loss));setTakeProfit(String(evaluation.take_profit_1));setQuantity(String(evaluation.position_sizing_example?.quantity??'1'));setShowOrder(true)}

 // Quote freshness and candle freshness are displayed separately: a fresh spot
 // quote never makes an old OHLC series fresh. The backend computes candle
 // freshness; this mirrors it and falls back to the real candle timestamps.
 const candleFresh=(()=>{const backend=market?.candle_freshness;if(backend?.status)return backend;const rows=market?.candles??[];const last=rows.length?rows[rows.length-1].timestamp:null;const bar=frameSeconds[timeframe];if(!last||!bar)return {status:'UNAVAILABLE',age_seconds:null as number|null};const age=Math.max(0,(Date.now()-Date.parse(last))/1000);return {status:age<=bar*4?'FRESH':'STALE',age_seconds:Math.round(age)};})();
 const candleStatusLabel=candleFresh.status==='FRESH'?'FRESH':candleFresh.status==='STALE'?'STALE':'UNAVAILABLE';
 const candleAgeLabel=candleFresh.age_seconds==null?'':candleFresh.age_seconds>=3600?`${Math.round(candleFresh.age_seconds/3600)}h old`:candleFresh.age_seconds>=60?`${Math.round(candleFresh.age_seconds/60)}m old`:`${Math.round(candleFresh.age_seconds)}s old`;
 const quoteStatusLabel=marketStatus.quote_status||(marketStatus.data_status==='LIVE'?'FRESH':marketStatus.data_status==='DEMO'?'DEMO':'STALE');
  const isMarketDataUnavailable=marketStatus.data_status==='ERROR'||marketStatus.data_status==='UNAVAILABLE'||(marketStatus.data_status!=='DEMO'&&candleStatusLabel==='UNAVAILABLE');
  let currentAnalysisStatus = 'READY';
  if(analysisBusy){currentAnalysisStatus='ANALYZING';}
  else if(analysisError){currentAnalysisStatus='ERROR';}
  else if(isMarketDataUnavailable){currentAnalysisStatus='DATA_UNAVAILABLE';}
  else if(candleFresh.status==='STALE'){currentAnalysisStatus='STALE_DATA';}
  else if(evaluation){
    if(evaluation.status==='VALIDATED'){currentAnalysisStatus='VALIDATED';}
    else if(evaluation.status==='BUY_CANDIDATE'||evaluation.status==='SELL_CANDIDATE'){
      if(evaluation.entry_price!=null&&evaluation.stop_loss!=null&&evaluation.take_profit_1!=null&&evaluation.risk_reward_ratio!=null&&evaluation.risk_reward_ratio>=1.5){currentAnalysisStatus='VALIDATED';}
      else{currentAnalysisStatus='WATCHLIST';}
    }else if(evaluation.status==='WATCHLIST'){currentAnalysisStatus='WATCHLIST';}
    else if(evaluation.status==='DATA_STALE'){currentAnalysisStatus='STALE_DATA';}
    else if(evaluation.status==='DATA_UNAVAILABLE'){currentAnalysisStatus='DATA_UNAVAILABLE';}
    else{currentAnalysisStatus='NO_SETUP';}
  }else{currentAnalysisStatus='NO_SETUP';}
 // One canonical assistant state, straight from the backend `ai_state`. The UI
 // never keeps a second verification state of its own: CHECKING is derived from
 // an in-flight local request or the backend's own in_flight flag, VERIFIED
 // requires a real successful provider request, EXPIRED is a neutral re-verify,
 // and the failure states are red.
 const aiStatusKey=aiStateKey||aiVerificationStatus;
 const aiStatusIsChecking=aiVerifying||aiStatusKey==='CHECKING';
 const aiStatusDisplay=aiStatusIsChecking?'Checking…':(AI_STATE_LABELS[aiStatusKey]||aiStateLabel||aiStatusLabel(aiVerificationStatus));
 const aiStatusIsFailure=!aiStatusIsChecking&&(AI_FAILURE_STATES.includes(aiStatusKey)||aiStatusKey==='BACKEND_UNAVAILABLE');
 const aiStatusIsHealthy=!aiStatusIsChecking&&aiStatusKey==='VERIFIED';
 return <div className={`app-frame ${sidebarCollapsed?'nav-collapsed':''}`}>
  <aside className="sidebar">
   <div className="brand">
    <div className="brand-mark"><Activity size={20}/></div>
    <div><b>TradeMind</b><small>AI TRADING TERMINAL</small></div>
    <button className="icon-only sidebar-collapse" aria-label={sidebarCollapsed?'Expand navigation':'Collapse navigation'} onClick={()=>setSidebarCollapsed(v=>!v)}>{sidebarCollapsed?<PanelLeftOpen size={17}/>:<PanelLeftClose size={17}/>}</button>
   </div>
   <div className="workspace-tag"><span className="green-dot"/> LOCAL WORKSPACE</div>
   <nav>{nav.map(n=><button key={n.name} className={`nav-link ${page===n.name?'active':''}`} onClick={()=>switchPage(n.name)}><n.icon size={17}/><span>{n.name}</span></button>)}</nav>
   <div className="side-bottom">
    <div className="safe-card"><div className="safe-icon"><ShieldCheck size={16}/></div><div><b>Simulation only</b><small>Live execution disabled</small></div><span className="green-dot"/></div>
   </div>
  </aside>
  <main className="main-area">
   <header className="topbar">
    <div className="crumb"><span>TradeMind AI</span><span className="slash">/</span><b>{page}</b></div>
    <div className="top-actions">
     <div className="connection"><i className={health?'green-dot':'amber-dot'}/>{health?'API connected':'API unavailable'}</div>
     <span className="top-divider"/>
     <button className="quiet-button" onClick={()=>switchPage('Settings')}><CircleHelp size={16}/><span>Help</span></button>
     <button className="user-avatar" title="Demo workspace">R</button>
    </div>
   </header>
   <div className={`page-content ${page==='AI Assistant'?'ai-page':''} ${page==='Dashboard'?'dashboard-page':''}`}>
    {page==='Dashboard'&&<header className="market-header">
     <div className="market-header-brand">
      <div className="instrument-select">
       <div className="instrument-icon">Au</div>
       <div><small>INSTRUMENT</small><b aria-label="Instrument">XAU/USD — Gold</b></div>
      </div>
      <div className="header-pills">
       <span className={`live-pill ${marketStatus.data_status.toLowerCase().replace('_','-')}`} title={`Market status: ${marketStatus.data_status}`}>
        <i className={marketStatus.data_status==='LIVE'?'green-dot':['QUOTA_EXCEEDED','ERROR'].includes(marketStatus.data_status)?'red-dot':'amber-dot'}/> {marketStatus.data_status}
       </span>
       <span className={`live-pill candle-${candleStatusLabel.toLowerCase().replace('_','-')}`} title={`Historical Candles: ${candleStatusLabel} ${candleAgeLabel}`}>
        <i className={candleStatusLabel === 'FRESH' ? 'green-dot' : candleStatusLabel === 'STALE' ? 'amber-dot' : 'red-dot'}/> CANDLES: {candleStatusLabel}
       </span>
       {sessionInfo&&<span className={`live-pill session-${sessionInfo.session.status.toLowerCase().replace('_','-')}`} title={`${sessionInfo.session.reason}${sessionInfo.session.schedule_note?(' '+sessionInfo.session.schedule_note):''} Schedule source: ${sessionInfo.session.session_source}. Holiday calendar: ${sessionInfo.session.holiday_calendar}.`}>{sessionInfo.session.status==='OPEN'?'MARKET OPEN':sessionInfo.session.status==='CLOSED'?'MARKET CLOSED':(sessionInfo.session.status==='SCHEDULE_UNKNOWN'?'SESSION STATUS UNKNOWN':(sessionInfo.session.status_label??sessionInfo.session.status))}</span>}
      </div>
     </div>
     <div className="market-price">
      <small>LAST PRICE <span className="data-tag">{quoteStatusLabel}</span></small>
      <b>{headerPrice != null ? fmt(headerPrice, 2) : '—'}</b>
      {change != null && pct != null ? (
       <span className={change >= 0 ? 'up' : 'down'}>
        {change >= 0 ? '+' : ''}{fmt(change, 2)} ({pct >= 0 ? '+' : ''}{pct.toFixed(2)}%)
       </span>
      ) : (
       <span className="neutral-tag" title="Prior comparison price unavailable or scale incompatible">N/A</span>
      )}
     </div>
     <div className="market-stat"><small>TIMEFRAME</small><div className="timeframes">{availableTimeframes.map(t=><button key={t} className={timeframe===t?'selected':''} onClick={()=>setTimeframe(t)}>{t}</button>)}</div></div>
     {!isMaximized&&<div className="timezone-select-wrap header-tz">
      <Globe size={13} className="tz-icon"/>
      <select className="timezone-select" aria-label="Display timezone" value={timezoneMode} onChange={e=>setTimezoneMode(e.target.value as TimezoneMode)}>
       {TIMEZONE_OPTIONS.map(o=><option key={o.value} value={o.value}>{o.value==='EXCHANGE'?(exchangeInfo.label?`Exchange (${exchangeInfo.label})`:'Exchange / Provider'):o.label}</option>)}
      </select>
     </div>}
     <div className="heading-actions">
      <button className="secondary-button" onClick={()=>{void load(false);void refreshSession(symbol)}}><Activity size={15}/> Refresh</button>
      {!isMaximized&&<button className="chart-tool maximize-btn" onClick={()=>setIsMaximized(true)} aria-label="Maximize market chart" title="Maximize chart"><Maximize2 size={15}/><span>Maximize</span></button>}
     </div>
     <div className="market-stat source-stat" title={marketStatus.message}>
      <small>QUOTE FRESHNESS · {marketStatus.connection_status}</small>
      <b><i className={marketStatus.data_status==='LIVE'?'green-dot':['QUOTA_EXCEEDED','ERROR'].includes(marketStatus.data_status)?'red-dot':'amber-dot'}/> {marketStatus.provider}</b>
      {quoteSourceDetail&&<small title={quoteSourceDetail}>Quote source: {marketStatus.provider} · {quoteSourceDetail}</small>}
      {candleSourceDetail&&<small title={candleSourceDetail}>Candle source: {marketStatus.provider} · {candleSourceDetail}</small>}
      <small>{marketStatus.message}</small>
      {marketStatus.timeframe_note&&<small className="timeframe-note">{marketStatus.timeframe_note}</small>}
      {marketStatus.last_update&&<small>Last update {formatTimeInTimezone(marketStatus.last_update, resolvedTimezone)} ({timezoneShort}) · {marketStatus.price==null?'price unavailable':fmt(marketStatus.price,2)}</small>}
      {sessionInfo?.session.status==='CLOSED'&&marketStatus.data_status!=='LIVE'&&<small>MARKET CLOSED · showing the last valid quote</small>}
     </div>
    </header>}
    {error&&<div className="error-banner"><AlertTriangle size={16}/><span>{error}</span><button onClick={()=>setError('')} aria-label="Dismiss"><X size={15}/></button></div>}
  {page==='Dashboard'&&<div className="dashboard-grid">
   <section className="primary-column">
     <div className={`chart-container-root ${isMaximized?'maximized':''}`}>
      {isMaximized&&(
       <div className="maximized-toolbar" aria-label="Maximized chart controls">
        <div className="maximized-left">
         <div className="maximized-brand">
          <Activity size={18}/>
          <span>TradeMind AI · Full Chart</span>
         </div>
         <div className="instrument-select">
          <div className="instrument-icon">Au</div>
          <div>
           <small>INSTRUMENT</small>
           <b aria-label="Instrument">XAU/USD — Gold</b>
          </div>
         </div>
         <div className="market-price">
          <small>LAST PRICE</small>
          <b>{headerPrice != null ? fmt(headerPrice, 2) : '—'}</b>
          {change != null && pct != null ? (
           <span className={change >= 0 ? 'up' : 'down'}>
            {change >= 0 ? '+' : ''}{fmt(change, 2)} ({pct >= 0 ? '+' : ''}{pct.toFixed(2)}%)
           </span>
          ) : (
           <span className="neutral-tag">N/A</span>
          )}
         </div>
         <div className="timeframes">
          {availableTimeframes.map(t=><button key={t} className={timeframe===t?'selected':''} onClick={()=>setTimeframe(t)}>{t}</button>)}
         </div>
        </div>
        <div className="maximized-right">
         <div className="chart-actions">
          <button className={`chart-tool ${enabled.ema20?'active':''}`} onClick={()=>setEnabled(x=>({...x,ema20:!x.ema20}))}>EMA 20</button>
          <button className={`chart-tool ${enabled.ema50?'active':''}`} onClick={()=>setEnabled(x=>({...x,ema50:!x.ema50}))}>EMA 50</button>
          <button className={`chart-tool ${enabled.vwap?'active':''}`} onClick={()=>setEnabled(x=>({...x,vwap:!x.vwap}))}>VWAP</button>
          <button className={`chart-tool ${drawings.fvgs?'active':''}`} onClick={()=>setDrawings(x=>({...x,fvgs:!x.fvgs}))}>FVGs</button>
          <button className={`chart-tool ${drawings.orderBlocks?'active':''}`} onClick={()=>setDrawings(x=>({...x,orderBlocks:!x.orderBlocks}))}>Order Blocks</button>
          <button className={`chart-tool ${drawings.supportResistance?'active':''}`} onClick={()=>setDrawings(x=>({...x,supportResistance:!x.supportResistance}))}>S/R</button>
          <button className="chart-tool" onClick={()=>setJumpToken(t=>t+1)} aria-label="Jump to latest candle" title="Scroll the chart back to the most recent candle">Latest</button>
         </div>
         <div className="timezone-select-wrap">
          <Globe size={13} className="tz-icon"/>
          <select className="timezone-select" aria-label="Display timezone" value={timezoneMode} onChange={e=>setTimezoneMode(e.target.value as TimezoneMode)}>
           {TIMEZONE_OPTIONS.map(o=><option key={o.value} value={o.value}>{o.value==='EXCHANGE'?(exchangeInfo.label?`Exchange (${exchangeInfo.label})`:'Exchange / Provider'):o.label}</option>)}
          </select>
         </div>
         <button className="chart-tool maximize-btn active" onClick={()=>setIsMaximized(false)} aria-label="Restore chart layout" title="Restore chart (Esc)">
          <Minimize2 size={15}/>
          <span>Restore</span>
          <span className="maximized-esc-hint">(Esc)</span>
         </button>
        </div>
       </div>
      )}
      {!isMaximized&&(
       <div className="section-title">
        <div>
         <h2>Live chart</h2>
         <p>Price action · {timeframe} interval · {timezoneShort}</p>
        </div>
        <div className="chart-actions">
         <button className="chart-tool active"><BarChart3 size={15}/> Candles</button>
         <button className="chart-tool" onClick={()=>setJumpToken(t=>t+1)} aria-label="Jump to latest candle" title="Scroll the chart back to the most recent candle">Latest</button>
        </div>
       </div>
      )}
      <div className="panel chart-panel">
       <div className="legend">
        <span><i className="legend-candle"/> {symbol} <b>{fmt(headerPrice,2)}</b></span>
        {enabled.ema20&&<span className="legend-blue">● EMA 20 {candles.length?indVal(ind.ema20,2):'(Unavailable)'}</span>}
        {enabled.ema50&&<span className="legend-purple">● EMA 50 {candles.length?indVal(ind.ema50,2):'(Unavailable)'}</span>}
        {enabled.vwap&&<span className="legend-orange" title={ind.vwap_message}>● VWAP {candles.length&&ind.vwap!=null&&ind.vwap_status==='AVAILABLE'?fmt(ind.vwap,2):'(Unavailable)'}</span>}
       </div>
       {candles.length?(
        <>
         {(candleStatusLabel==='STALE'||marketStatus.data_status!=='LIVE'||(market?.warnings&&market.warnings.length>0))&&(
          <div className="chart-stale-banner" role="status" style={{background:'rgba(245,158,11,0.12)',border:'1px solid rgba(245,158,11,0.3)',color:'#fbbf24',padding:'6px 12px',fontSize:'12px',display:'flex',alignItems:'center',justifyContent:'space-between',flexWrap:'wrap',gap:'6px'}}>
           <span><AlertTriangle size={13} style={{verticalAlign:'middle',marginRight:'5px'}}/><strong>{candleStatusLabel==='STALE'?'STALE DATA':'OFFLINE FEED'}:</strong> Displaying cached historical candles. Live updates are {marketStatus.data_status==='LIVE'?'active for quotes':'currently unavailable'}.</span>
           {market?.last_successful_timestamp&&<span>Latest candle: {formatDateTimeInTimezone(market.last_successful_timestamp,resolvedTimezone)}</span>}
          </div>
         )}
         <PriceChart candles={candles} symbol={symbol} timeframe={timeframe} indicators={indSeries} zones={fvgs} enabled={enabled} drawings={drawings} orderBlocks={orderBlocks} structure={structure} dataStatus={candleStatusLabel} setup={evaluation} showSetup={setupVisible} timezone={resolvedTimezone} quotePrice={quotePrice} quoteLabel={quoteSourceDetail?`Quote · ${marketStatus.provider}`:'Last price'} isMaximized={isMaximized} onReachLeftEdge={()=>void loadOlderCandles()} loadingOlder={loadingOlder} historyExhausted={historyExhausted} jumpToLatestToken={jumpToken}/>
        </>
       ):(
        <div className="empty-chart" role="status">
         <p className="empty-chart-title">{symbol} · {timeframe}</p>
         <div className="empty-chart-diagnostics" style={{maxWidth:'520px',margin:'0 auto 16px auto',textAlign:'center'}}>
          <p style={{color:'#f87171',fontWeight:600,marginBottom:'8px'}}>
           {error||(market?.warnings?.length?market.warnings.join(' '):`Historical market data unavailable for ${symbol} (${timeframe}).`)}
          </p>
          <p style={{fontSize:'12px',color:'#94a3b8',lineHeight:'1.5'}}>
           Diagnostic: Upstream provider ({marketStatus.provider||'XAUS'}) returned no candles for interval {timeframe}.
           {marketStatus.message?` Provider message: ${marketStatus.message}`:''}
          </p>
         </div>
         <button className="empty-chart-retry" onClick={()=>void load(false)}>Retry</button>
        </div>
       )}
       <div className="chart-foot">
        <span><i className={marketStatus.data_status==='LIVE'?'green-dot':'amber-dot'}/> QUOTE {marketStatus.data_status} · {marketStatus.provider} · Timezone: <b>{timezoneShort}</b></span>
        <span>Last candle ({timezoneShort}): {market?.last_successful_timestamp?formatDateTimeInTimezone(market.last_successful_timestamp,resolvedTimezone):'unavailable'}{market?.last_successful_timestamp&&isIntradayTimeframe(timeframe)&&exchangeInfo.tz!==resolvedTimezone?` (${exchangeInfo.short}: ${formatDateTimeInTimezone(market.last_successful_timestamp,exchangeInfo.tz)})`:''}</span>
        {sourceMismatch.mismatch&&<span className="source-mismatch" title="The quote and the candle series come from different upstream feeds. Neither number is altered; the chart's last-price line follows the quote.">Quote vs chart close: {fmt(sourceMismatch.difference,2)}</span>}
        <ChartClock timezone={resolvedTimezone}/>
       </div>
      </div>
     </div>
    <section className="panel analysis-controls">
     <div className="panel-heading">
      <div>
       <h3>BUY / SELL analysis</h3>
       <small>BACKEND RULE EVALUATION · {candleStatusLabel} DATA · <span className={`analysis-status-pill ${currentAnalysisStatus.toLowerCase().replace('_','-')}`}>STATUS: {currentAnalysisStatus}</span>{lastAnalysisTimestamp&&<span className="last-eval-time"> · Last evaluated: {formatTimeInTimezone(lastAnalysisTimestamp, resolvedTimezone)} ({timezoneShort})</span>}</small>
      </div>
      <button className="secondary-button compact" onClick={()=>void load()}>Analyze market</button>
     </div>
     <div className="analysis-buttons">
      <button className="buy-button" disabled={analysisBusy||isMarketDataUnavailable} onClick={()=>void evaluate('BUY')} title={isMarketDataUnavailable?'Fresh market data is unavailable':undefined}>{analysisBusy&&lastEvaluatedSide==='BUY'?'Evaluating...':'Evaluate BUY setup'}</button>
      <button className="sell-button" disabled={analysisBusy||isMarketDataUnavailable} onClick={()=>void evaluate('SELL')} title={isMarketDataUnavailable?'Fresh market data is unavailable':undefined}>{analysisBusy&&lastEvaluatedSide==='SELL'?'Evaluating...':'Evaluate SELL setup'}</button>
      <button className={`chart-tool ${drawings.fvgs?'active':''}`} onClick={()=>setDrawings(x=>({...x,fvgs:!x.fvgs}))}>Draw FVGs</button>
      <button className={`chart-tool ${drawings.orderBlocks?'active':''}`} onClick={()=>setDrawings(x=>({...x,orderBlocks:!x.orderBlocks}))}>Draw Order Blocks</button>
      <button className={`chart-tool ${drawings.supportResistance?'active':''}`} onClick={()=>setDrawings(x=>({...x,supportResistance:!x.supportResistance}))}>Draw Support/Resistance</button>
      <button className="chart-tool" onClick={()=>setDrawings({fvgs:false,orderBlocks:false,supportResistance:false})}>Clear Drawings</button>
     </div>
      {isMarketDataUnavailable&&<p className="analysis-notice-unavailable" style={{padding:'6px 10px',background:'rgba(239,68,68,0.1)',border:'1px solid rgba(239,68,68,0.3)',borderRadius:'4px',fontSize:'12px',color:'#fca5a5',margin:'8px 0'}}>Fresh market data is unavailable for {symbol} ({timeframe}). Live-data-dependent BUY/SELL evaluation is disabled.</p>}
     {analysisBusy&&<p className="analysis-loading" role="status">Evaluating setup…</p>}
     {analysisError&&<p className="analysis-error">{analysisError}</p>}
     {evaluation&&evaluation.timeframe!==timeframe&&(
      <div className="eval-timeframe-notice" style={{padding:'8px 12px',background:'rgba(255,193,7,0.12)',border:'1px solid rgba(255,193,7,0.3)',borderRadius:'6px',fontSize:'12px',marginBottom:'10px',color:'#ffc107'}}>
       Displaying {evaluation.direction} evaluation for <strong>{evaluation.timeframe}</strong>. Current chart timeframe is <strong>{timeframe}</strong>. Click Evaluate to analyze {timeframe}.
      </div>
     )}
     {evaluation&&<SetupResult evaluation={evaluation} visible={setupVisible} onShow={()=>setSetupVisible(true)} onHide={()=>setSetupVisible(false)} onClear={()=>{setSetupVisible(false);setEvaluation(null)}} onReview={reviewSetup} onRefresh={()=>{if(lastEvaluatedSide)void evaluate(lastEvaluatedSide)}} analysisBusy={analysisBusy} timezone={resolvedTimezone} timezoneShort={timezoneShort} isMarketDataUnavailable={isMarketDataUnavailable}/>}
    </section>
    <div className="panel indicator-panel">
     <div className="panel-heading"><div><h3>Technical indicators</h3><small>CALCULATED FROM {candleStatusLabel} {marketStatus.data_status} CANDLES</small></div></div>
     <div className="indicator-grid">
      <IndicatorRow name="EMA 20" value={candles.length?indVal(ind.ema20,2):'UNAVAILABLE'} color="blue" checked={enabled.ema20} onChange={()=>setEnabled({...enabled,ema20:!enabled.ema20})}/>
      <IndicatorRow name="EMA 50" value={candles.length?indVal(ind.ema50,2):'UNAVAILABLE'} color="purple" checked={enabled.ema50} onChange={()=>setEnabled({...enabled,ema50:!enabled.ema50})}/>
      <IndicatorRow name="RSI 14" value={candles.length?indVal(ind.rsi14,2):'UNAVAILABLE'} color="green"/>
      <IndicatorRow name="MACD · line / signal" value={candles.length?`${indVal(ind.macd.line,5)} / ${indVal(ind.macd.signal,5)}`:'UNAVAILABLE'} color="purple"/>
      <IndicatorRow name="Bollinger · upper / lower" value={candles.length?`${indVal(ind.bollinger20.upper,5)} / ${indVal(ind.bollinger20.lower,5)}`:'UNAVAILABLE'} color="blue"/>
      <IndicatorRow name="ATR 14" value={candles.length?indVal(ind.atr14,2):'UNAVAILABLE'} color="green"/>
      <IndicatorRow name="VWAP" value={!candles.length?'UNAVAILABLE':ind.vwap_status==='UNAVAILABLE'?'UNAVAILABLE':ind.vwap_status==='INSUFFICIENT_DATA'?'INSUFFICIENT_DATA':ind.vwap_status==='ERROR'?'ERROR':ind.vwap==null?'Unavailable':fmt(ind.vwap,2)} color="orange" checked={enabled.vwap} onChange={()=>setEnabled({...enabled,vwap:!enabled.vwap})} note={ind.vwap_status==='UNAVAILABLE'?(ind.vwap_message||'VWAP unavailable: this feed does not provide suitable volume data for this instrument.'):undefined}/>
     </div>
     {(ind.vwap_message||market?.warnings?.length)?<div className="indicator-note">{ind.vwap_message||''}{market?.warnings?.length?` ${market.warnings.join(' ')}`:''}</div>:null}
    </div>
   </section>
   <aside className="right-column">
    <div className="panel assistant-panel">
     <div className="panel-heading">
      <div className="assistant-title">
       <div className="assistant-icon"><Sparkles size={16}/></div>
       <div><h3>AI assistant</h3><small>GEMINI · CHART CONTEXT</small></div>
      </div>
      <button className="text-link" onClick={()=>switchPage('AI Assistant')}>Expand chat</button>
     </div>
     <div className="assistant-context">
      <span>CONTEXT</span>
      <b>{symbol} · {timeframe} · {marketStatus.provider} {marketStatus.data_status}</b>
      <div className="context-statuses" data-testid="context-statuses">
       <span title="Provider quote freshness">Quote <b className={quoteStatusLabel==='FRESH'||quoteStatusLabel==='LIVE'?'ok':quoteStatusLabel==='DEMO'?'demo':'warn'}>{quoteStatusLabel}</b></span>
       <span title="Historical OHLC candle freshness (independent of the quote)">Candles <b className={candleStatusLabel==='FRESH'?'ok':candleStatusLabel==='STALE'?'warn':'warn'}>{candleStatusLabel}{candleStatusLabel==='STALE'&&candleAgeLabel?` · ${candleAgeLabel}`:''}</b></span>
       <span className="offline-indicator" title={aiVerificationMessage}>AI <i className={aiStatusIsHealthy?'green-dot':aiStatusIsFailure?'red-dot':'amber-dot'}/><b className={aiStatusIsHealthy?'ok':aiStatusIsFailure?'bad':'warn'}>{aiStatusDisplay}</b></span>
      </div>
     </div>
     {isMarketDataUnavailable&&(
      <div className="assistant-outdated-notice" style={{padding:'8px 12px',background:'rgba(239,68,68,0.12)',border:'1px solid rgba(239,68,68,0.3)',borderRadius:'4px',fontSize:'12px',color:'#fca5a5',margin:'8px 12px 0 12px'}}>
       <strong>Market data unavailable:</strong> Fresh market data is currently offline. Previous assistant responses and indicator values reflect historical context and may be outdated.
      </div>
     )}
     <div className="chat-messages" ref={chatListRef}>
      {chat.filter(m=>!(aiProvider==='gemini'&&(m.text.toLowerCase().includes('openai')&&(m.text.toLowerCase().includes('quota')||m.text.toLowerCase().includes('rate limit'))))).map((m,i)=>(
       <div key={`${m.createdAt??i}-${i}`} className={`chat-message ${m.role}`}>
        {m.role==='assistant'?(
         <>
          <Sparkles size={13}/>
          <div className="chat-message-body">
           {m.contextSymbol&&(
            <div className="chat-message-context-tag" style={{fontSize:'10px',color:'#94a3b8',marginBottom:'4px'}}>
             [{m.contextSymbol} · {m.contextTimeframe||timeframe}{m.createdAt?` · ${formatDateTimeInTimezone(m.createdAt,resolvedTimezone)}`:''}{isMarketDataUnavailable?' · HISTORICAL':''}]
            </div>
           )}
           <MarkdownView content={m.text}/>
          </div>
         </>
        ):(
         <span>{m.text}</span>
        )}
       </div>
      ))}
      {chatBusy&&<div className="chat-message assistant"><span>Thinking...</span></div>}
     </div>
     <div className="quick-prompts">
      <button onClick={()=>setChatInput(`Explain the ${marketStatus.data_status.toLowerCase()} indicators for ${symbol} on ${timeframe}.`)}>Explain indicators</button>
      <button onClick={()=>setChatInput('What are the limitations of this sample data?')}>Data limitations</button>
      {!aiVerified&&<button onClick={()=>void verifyAi()} disabled={aiVerifying}>{aiVerifying?'Verifying...':`Verify ${aiProvider==='gemini'?'Gemini':aiProvider==='openai'?'OpenAI':'AI'}`}</button>}
     </div>
     <form className="chat-input" onSubmit={ask}>
      <input aria-label="Ask the AI assistant" value={chatInput} onChange={e=>setChatInput(e.target.value)} placeholder="Ask about the analysis..."/>
      <button disabled={chatBusy||!chatInput.trim()} aria-label="Send message"><Send size={14}/></button>
     </form>
     <div className="assistant-disclaimer">AI output can be wrong. Not a trade recommendation.</div>
    </div>
   </aside>
  </div>}
  {page==='Settings'&&<ModulePage page={page} symbol={symbol} setup={setup} orders={orders} positions={paperPositions} accounts={paperAccounts} dataStatus={marketStatus.data_status} candleStatus={candleStatusLabel} provider={marketStatus.provider} switchPage={switchPage} setShowOrder={setShowOrder} brokers={api.brokers} tradingView={api.tradingView} timezone={resolvedTimezone} timezoneShort={timezoneShort}/>}
  {page==='AI Assistant'&&<AIChatWorkspace conversations={chatConversations} activeId={activeChatId} messages={chat} busy={chatBusy} status={aiVerificationStatus} statusMessage={aiVerificationMessage} stateLabel={aiStatusDisplay} stateKey={aiStatusKey} aiState={aiStatusKey} configured={aiConfigured} verified={aiVerified} verifying={aiVerifying} provider={aiProvider} symbol={symbol} timeframe={timeframe} timeframes={availableTimeframes} onNew={newConversation} onSelect={selectConversation} onClear={clearConversation} onVerify={()=>void verifyAi()} onSend={(text,sym,frame,context)=>void sendChat(text,sym,frame,context)} onBack={()=>switchPage('Dashboard')} timezone={resolvedTimezone} timezoneShort={timezoneShort}/>}
  <footer><span>TradeMind AI <b>v0.1</b> · {marketStatus.data_status==='DEMO'?'Synthetic market data':`${marketStatus.provider} market data`}</span><span><i className={marketStatus.data_status==='LIVE'?'green-dot':'amber-dot'}/> {marketStatus.data_status} market data · Timezone: {timezoneShort}</span></footer></div></main>
  {showOrder&&<div className="modal-backdrop" onClick={()=>setShowOrder(false)}><div className="order-modal" role="dialog" aria-modal="true" aria-labelledby="order-title" onClick={e=>e.stopPropagation()}><div className="modal-heading"><div><span className="eyebrow">SIMULATED ORDER</span><h2 id="order-title">{reviewSide?`Review ${reviewSide} setup`:'Review paper order'}</h2></div><button className="icon-only" onClick={()=>{setShowOrder(false);setReviewSide(null)}} aria-label="Close"><X size={17}/></button></div><div className="demo-warning"><AlertTriangle size={16}/> {marketStatus.data_status==='DEMO'?'Synthetic DEMO pricing only.':`Paper-only order using ${marketStatus.provider} ${marketStatus.data_status} data; verify freshness before confirming.`} No broker will receive this order.</div><label>Instrument<b>XAU/USD — Gold</b></label><label>Order type<select disabled={!!reviewSide} value={orderType} onChange={e=>setOrderType(e.target.value as 'MARKET'|'LIMIT')}><option value="MARKET">Market · simulated immediate fill</option><option value="LIMIT">Limit · pending (no fill simulator yet)</option></select></label><label>Quantity<input type="number" min="0.000001" step="0.01" value={quantity} onChange={e=>setQuantity(e.target.value)}/></label>{orderType==='LIMIT'&&<label>Limit entry price<input type="number" min="0.000001" step="any" value={entryPrice} onChange={e=>setEntryPrice(e.target.value)} placeholder="Required for limit order"/></label>}<label>Stop-loss · optional<input type="number" min="0.000001" step="any" value={stopLoss} onChange={e=>setStopLoss(e.target.value)} placeholder="Price level"/></label><label>Take-profit · optional<input type="number" min="0.000001" step="any" value={takeProfit} onChange={e=>setTakeProfit(e.target.value)} placeholder="Price level"/></label><div className="order-preview"><span>Current {marketStatus.data_status} price</span><b>{fmt(marketStatus.price??latest?.close,2)}</b><span>Quantity</span><b>{quantity||'—'}</b>{reviewSide&&evaluation?.risk_reward_ratio&&<><span>Risk / Reward</span><b>{evaluation.risk_reward_ratio.toFixed(2)}:1</b></>}<span>Mode</span><b className="amber-text">SIMULATED</b></div><div className="order-buttons">{(!reviewSide||reviewSide==='SELL')&&<button className="sell-button" onClick={()=>void submitOrder('SELL')}><ArrowDownRight size={16}/> Confirm paper sell</button>}{(!reviewSide||reviewSide==='BUY')&&<button className="buy-button" onClick={()=>void submitOrder('BUY')}><ArrowUpRight size={16}/> Confirm paper buy</button>}</div></div></div>}
 </div>
}
function SetupResult({evaluation:e,visible,onShow,onHide,onClear,onReview,onRefresh,analysisBusy,timezone='Asia/Kolkata',timezoneShort='IST',isMarketDataUnavailable=false}:{evaluation:SetupEvaluation;visible:boolean;onShow:()=>void;onHide:()=>void;onClear:()=>void;onReview:()=>void;onRefresh?:()=>void;analysisBusy?:boolean;timezone?:string;timezoneShort?:string;isMarketDataUnavailable?:boolean}){
 const isOutdated = Boolean((isMarketDataUnavailable || e.status==='DATA_STALE' || e.status==='STALE_DATA') && (e.direction === 'BUY' || e.direction === 'SELL'));
 const isCandidate=(e.direction==='BUY'||e.direction==='SELL')&&e.entry_price!=null&&e.stop_loss!=null&&e.take_profit_1!=null;
 const precision=2;
 const currency=String(e.position_sizing_example?.quote_currency??'');
 const size=e.position_sizing_example;
 let statusClass='status-pill-neutral';
 let statusLabel='NO_SETUP';
 if(isOutdated){
  statusClass='status-pill-stale';
  statusLabel='OUTDATED ANALYSIS';
 }else if(e.status==='VALIDATED'){
  statusClass='status-pill-validated';
  statusLabel=`VALIDATED · ${e.direction} ${e.setup_type?e.setup_type.replaceAll('_',' '):'SETUP'}`;
 }else if(e.status==='BUY_CANDIDATE'||(e.direction==='BUY'&&e.status!=='WATCHLIST'&&e.status!=='NO_SETUP'&&e.status!=='DATA_STALE'&&e.status!=='DATA_UNAVAILABLE')){
  statusClass='status-pill-buy';
  statusLabel=`BUY · ${e.setup_type?e.setup_type.replaceAll('_',' '):'RETEST ENTRY'}`;
 }else if(e.status==='SELL_CANDIDATE'||(e.direction==='SELL'&&e.status!=='WATCHLIST'&&e.status!=='NO_SETUP'&&e.status!=='DATA_STALE'&&e.status!=='DATA_UNAVAILABLE')){
  statusClass='status-pill-sell';
  statusLabel=`SELL · ${e.setup_type?e.setup_type.replaceAll('_',' '):'RETEST ENTRY'}`;
 }else if(e.status==='WATCHLIST'){
  statusClass='status-pill-watch';
  statusLabel='WATCHLIST';
 }else if(e.status==='WAITING_FOR_CONFIRMATION'){
  statusClass='status-pill-watch';
  statusLabel=`WAITING FOR CONFIRMATION${e.side?` · ${e.side}`:''}`;
 }else if(e.status==='INVALIDATED'){
  statusClass='status-pill-stale';
  statusLabel='INVALIDATED';
 }else if(e.status==='DATA_STALE'||e.status==='STALE_DATA'){
  statusClass='status-pill-stale';
  statusLabel='STALE_DATA';
 }else if(e.status==='INSUFFICIENT_DATA'){
  statusClass='status-pill-watch';
  statusLabel='INSUFFICIENT_DATA';
 }else if(e.status==='UNSUPPORTED_TIMEFRAME'){
  statusClass='status-pill-stale';
  statusLabel='UNSUPPORTED_TIMEFRAME';
 }else if(e.direction==='NO_SETUP'||e.status==='NO_SETUP'||e.status==='DATA_UNAVAILABLE'){
  statusClass=e.status==='DATA_UNAVAILABLE'?'status-pill-stale':'status-pill-neutral';
  statusLabel='NO_SETUP';
 }else{
  statusClass='status-pill-neutral';
  statusLabel='NO_SETUP';
 }

 return <article className="evaluation-result structured-evaluation">
  <div className="eval-heading">
   <div style={{display:'flex',alignItems:'center',gap:'8px',flexWrap:'wrap'}}>
    <span className={`eval-badge ${statusClass}`}><span className="status-dot"/><strong>{statusLabel}</strong></span>
    <span className="eval-subhead">{e.symbol} · {e.timeframe} · {e.source} {e.market_data_status}{isOutdated?` · Recorded: ${formatDateTimeInTimezone(e.analysis_timestamp,timezone)}`:''}</span>
   </div>
   {onRefresh&&<button className="secondary-button compact" onClick={onRefresh} disabled={analysisBusy||isMarketDataUnavailable}>{analysisBusy?'Evaluating...':'Refresh analysis'}</button>}
  </div>

  {isOutdated&&(
   <div className="eval-outdated-warning" style={{padding:'8px 12px',background:'rgba(239,68,68,0.15)',border:'1px solid rgba(239,68,68,0.3)',borderRadius:'6px',fontSize:'12px',marginBottom:'10px',color:'#fca5a5'}}>
    <strong>OUTDATED ANALYSIS:</strong> Fresh market data is currently unavailable. These levels (entry, stop-loss, targets) are historical calculations from <b>{formatDateTimeInTimezone(e.analysis_timestamp,timezone)} ({timezoneShort})</b> on <b>{e.symbol} · {e.timeframe}</b> and must NOT be treated as active recommendations. Live order execution is disabled.
   </div>
  )}

  <div className="eval-summary-card">
   <p className="eval-explanation">{e.explanation}</p>
   <div className="eval-key-stats">
    <span>Current market price: <b className="eval-price">{fmt(e.current_price,precision)}</b></span>
    <span>Market bias: <b>{e.market_bias}</b></span>
    <span>Data status: <b>{e.market_data_status} ({e.source})</b></span>
    {e.data_freshness_seconds!=null&&<span>Data age: <b>{e.data_freshness_seconds}s</b></span>}
   </div>
  </div>

  {e.warnings.length>0&&(
   <div className="eval-warnings" role="note">
    <strong>Data-quality warnings</strong>
    <ul>{e.warnings.map((w,i)=><li key={`w${i}`}>{w}</li>)}</ul>
   </div>
  )}

  {e.late_entry&&<div className="setup-warning-box"><strong>Late entry:</strong> {e.late_entry_reason??'Price has moved beyond the validated entry zone; a fresh confirmation is required before entering.'}</div>}

  {isCandidate?(
   <>
    <div className="structured-levels">
     <div><small>Current market price</small><b>{fmt(e.current_price,precision)}</b></div>
     <div><small>Entry / zone</small><b className={e.direction==='BUY'?'green-text':'red-text'}>{e.entry_zone?`${fmt(e.entry_zone.lower,precision)} – ${fmt(e.entry_zone.upper,precision)}`:fmt(e.entry_price,precision)}</b></div>
     <div><small>Stop-loss</small><b className="stop-text">{fmt(e.stop_loss,precision)}</b></div>
     {e.stop_basis&&<div><small>Stop basis</small><b>{e.stop_basis}</b></div>}
     <div><small>Take-profit 1</small><b className="target-text">{fmt(e.take_profit_1,precision)}{e.risk_reward_tp1!=null?` · ${e.risk_reward_tp1.toFixed(2)}:1`:''}</b></div>
     <div><small>Take-profit 2</small><b className="target-text">{fmt(e.take_profit_2,precision)}{e.risk_reward_tp2!=null?` · ${e.risk_reward_tp2.toFixed(2)}:1`:''}</b></div>
     {e.take_profit_3!=null&&<div><small>Take-profit 3</small><b className="target-text">{fmt(e.take_profit_3,precision)}{e.risk_reward_tp3!=null?` · ${e.risk_reward_tp3.toFixed(2)}:1`:''}</b></div>}
     <div><small>Risk / reward</small><b>{e.risk_reward_ratio?`${e.risk_reward_ratio.toFixed(2)}:1`:'—'}</b></div>
     <div><small>Risk budget</small><b>{e.risk_amount==null?'—':`${currency} ${e.risk_amount} (${e.max_risk_percent}%)`}</b></div>
     <div><small>Position sizing example</small><b>{size?`${String(size.quantity)} ${String(size.unit)}`:e.position_sizing_unavailable_reason?<span title={e.position_sizing_unavailable_reason}>Position size unavailable: {e.position_sizing_unavailable_reason}</span>:'Position size unavailable: contract or conversion details required'}</b></div>
    </div>
    {e.entry_trigger&&<div className="setup-trigger-box"><strong>Entry confirmation:</strong> {e.entry_trigger}</div>}
    {e.invalidation_condition&&<div className="setup-invalidation-box"><strong>Invalidation rule:</strong> {e.invalidation_condition}</div>}
   </>
  ):(
   <div className="non-candidate-panel">
    {e.next_condition_to_watch&&<div className="monitoring-box"><b>Condition to monitor next:</b><p>{e.next_condition_to_watch}</p></div>}
    {e.invalidation_condition&&<div className="invalidation-box"><b>Setup invalidation condition:</b><p>{e.invalidation_condition}</p></div>}
   </div>
  )}

  <div className="evaluation-columns">
   <div>
    <b>Supporting evidence</b>
    <ul>
     {e.reasons.map((x,i)=><li key={`r${i}`}>{x}</li>)}
     {Object.entries(e.supporting_indicators).filter(([k,v])=>k!=='candle_timestamp'&&v!=null).map(([k,v])=><li key={k}>{k}: {typeof v==='object'?JSON.stringify(v):String(v)}</li>)}
     {e.supporting_smart_money_concepts.map((x,i)=><li key={`s${i}`}>{x}</li>)}
     {!e.reasons.length&&!e.supporting_smart_money_concepts.length&&<li>No supporting evidence passed.</li>}
    </ul>
   </div>
   <div>
    <b>Missing conditions &amp; Risks</b>
    <ul>
     {e.conditions_not_satisfied.map((x,i)=><li key={`c${i}`}>{x}</li>)}
     {e.risks.map((x,i)=><li key={`risk${i}`}>{x}</li>)}
     {!e.conditions_not_satisfied.length&&!e.risks.length&&<li>All required conditions satisfied.</li>}
    </ul>
   </div>
  </div>

  {e.ai_review&&(
   <div className="ai-review-card">
    <div className="ai-review-header"><Sparkles size={14}/><b>AI Strategy Review</b></div>
    <p className="ai-review-text">{e.ai_review}</p>
    <small className="ai-review-note">AI review provides qualitative verification and risk commentary. Trade levels are deterministically calculated and validated by backend risk rules.</small>
   </div>
  )}

  <div className="evaluation-meta">
   <span>Source: <b>{e.source}</b> ({e.market_data_status})</span>
   <span>Data freshness: <b>{e.data_freshness_seconds==null?'unknown':`${e.data_freshness_seconds}s`}</b></span>
   <span>Candle: <b>{e.supporting_indicators.candle_timestamp?formatDateTimeInTimezone(String(e.supporting_indicators.candle_timestamp),timezone):'unavailable'}</b></span>
   <span>Analyzed: <b>{formatDateTimeInTimezone(e.analysis_timestamp,timezone)} ({timezoneShort})</b></span>
  </div>

  <div className="setup-actions">
   {isCandidate&&(
    <>
     <button className="secondary-button compact" onClick={visible?onHide:onShow}>{visible?'Hide setup':'Show setup on chart'}</button>
     <button className="secondary-button compact" onClick={onClear}>Clear setup drawings</button>
     <button className="primary-button compact" disabled={isOutdated} onClick={isOutdated?undefined:onReview} title={isOutdated?'Execution disabled: Market data is unavailable':undefined}>{isOutdated?'Execution disabled (Outdated)':'Review in Paper Trading'}</button>
    </>
   )}
   {!isCandidate&&<button className="secondary-button compact" onClick={onClear}>Dismiss</button>}
  </div>
 </article>;
}
function IndicatorRow({name,value,color,checked,onChange,note}:{name:string;value:string;color:string;checked?:boolean;onChange?:()=>void;note?:string}){return <div className="indicator-row-wrap"><div className="indicator-row"><span className={`indicator-dot ${color}`}/><span className="indicator-name">{name}</span><b>{value}</b>{onChange&&<button className={`toggle ${checked?'on':''}`} onClick={onChange} aria-label={`Toggle ${name}`}><i/></button>}</div>{note&&<div className="vwap-unavailable-note">{note}</div>}</div>}
type BrokerInfo={name:string;integration_type:string;authentication:string;supported_instruments:string[];market_data_operations:string[];account_operations:string[];order_operations:string[];required_configuration:string[];status:string;documentation_url:string|null;live_execution:boolean};
function ModulePage({page,symbol,setup,orders,positions,accounts,dataStatus,candleStatus,provider,switchPage,setShowOrder,brokers,tradingView,timezone='Asia/Kolkata',timezoneShort='IST'}:{page:Page;symbol:string;dataStatus:string;candleStatus:string;provider:string;setup:{status:string;explanation:string;conditions_satisfied:string[];conditions_not_satisfied:string[]}|null;orders:{id:string;symbol:string;side:string;quantity:string;status:string;average_price:string;timestamp:string;fees?:string;slippage?:string;realized_pnl?:string}[];positions:Awaited<ReturnType<typeof api.history>>['positions'];accounts:Awaited<ReturnType<typeof api.history>>['accounts_by_currency'];switchPage:(p:Page)=>void;setShowOrder:(v:boolean)=>void;brokers:()=>Promise<BrokerInfo[]>;tradingView:()=>ReturnType<typeof api.tradingView>;timezone?:string;timezoneShort?:string}){
 type MarketInstrument=Awaited<ReturnType<typeof api.marketInstruments>>[number];const [brokerList,setBrokerList]=useState<BrokerInfo[]>([]);const [marketInstruments,setMarketInstruments]=useState<MarketInstrument[]>([]);const [tv,setTv]=useState<Awaited<ReturnType<typeof api.tradingView>>|null>(null);useEffect(()=>{if(page==='Broker Connections')brokers().then(setBrokerList);if(page==='Markets')api.marketInstruments().then(setMarketInstruments).catch(()=>setMarketInstruments([]));if(page==='TradingView')tradingView().then(setTv)},[page]);
 if(page==='TradingView')return <div className="panel module-panel"><div className="panel-heading"><div><h2>TradingView integration</h2><small>Website link only · not authenticated</small></div><span className="status-pill neutral">{tv?.status??'Loading'}</span></div><div className="analysis-row"><span>Enabled integration</span><b>{tv?.integration_type??'Checking backend status'}</b></div><div className="analysis-row"><span>Account authenticated</span><b>No</b></div><div className="analysis-row"><span>Account data access</span><b>No</b></div><p className="muted-paragraph">{tv?.limitations.join(' ')}</p><a className="secondary-button tv-link" href={tv?.link_url??'https://www.tradingview.com/'} target="_blank" rel="noreferrer">Open TradingView website</a><a className="text-link tv-doc" href={tv?.documentation_url} target="_blank" rel="noreferrer">Advanced Charts official requirements</a></div>;
 if(page==='Markets')return <div className="panel module-panel"><div className="panel-heading"><div><h2>Market instruments</h2><small>Provider routing and data freshness from the backend</small></div></div><div className="market-list">{marketInstruments.map(item=><div className="market-list-row" key={item.symbol}><b>{item.symbol}</b><span>{item.name}</span><span>{item.asset_class}</span><span>{item.quote_available&&item.price!=null?fmt(item.price,2):'— no quote'}</span><span>{item.provider_symbol??item.provider}</span><span className={item.data_status==='LIVE'?'green-text':'amber-text'}>{item.data_status}</span></div>)}</div>{!marketInstruments.length&&<p className="muted-paragraph">Instrument data is unavailable. Check the backend provider status.</p>}<p className="muted-paragraph">Provider credentials remain on the backend. DEMO is explicitly marked when an instrument uses synthetic candles.</p></div>;
 if(page==='AI Assistant')return <div className="panel module-panel"><div className="assistant-title"><div className="assistant-icon"><Sparkles size={16}/></div><h2>AI assistant</h2></div><p>Ask a question from the right-hand panel. Configure <code>GEMINI_API_KEY</code> and <code>GEMINI_MODEL</code> (or <code>OPENAI_API_KEY</code>) on the backend to enable responses.</p><button className="primary-button" onClick={()=>switchPage('Dashboard')}>Open dashboard assistant</button></div>;
 if(page==='Technical Analysis')return <div className="panel module-panel"><div className="panel-heading"><div><h2>Technical analysis</h2><small>{symbol} · algorithmic results from {provider} {candleStatus} candles · {timezoneShort}</small></div><span className="status-pill watch">{setup?.status??'Loading'}</span></div><div className="analysis-row"><span>Example strategy</span><b>Algorithmic rule evaluation · {setup?.status??'monitoring'}</b></div><div className="analysis-row"><span>Conditions met</span><b>{setup?.conditions_satisfied.join(', ')||'None'}</b></div><div className="analysis-row"><span>Conditions pending</span><b>{setup?.conditions_not_satisfied.join(', ')||'—'}</b></div><div className="analysis-row"><span>Actionable entry</span><b>None generated</b></div><p className="muted-paragraph">{setup?.explanation} Indicators and three-candle fair value gaps are calculated on the dashboard. Pivot confirmation is available through the API.</p><button className="secondary-button" onClick={()=>switchPage('Dashboard')}>Return to chart</button></div>;
 if(page==='Paper Trading')return <div className="panel module-panel"><div className="panel-heading"><div><h2>Paper trading</h2><small>SIMULATED · per-quote-currency virtual ledgers · Timezone: {timezoneShort}</small></div><button className="primary-button" onClick={()=>setShowOrder(true)}><Plus size={14}/> New simulated order</button></div><div className="paper-summary">{Object.values(accounts).map(a=><div key={a.quote_currency}><small>{a.quote_currency} EQUITY</small><b>{Number(a.equity).toFixed(2)}</b><small>Available {Number(a.available_simulated_funds).toFixed(2)} · Realized {Number(a.realized_pnl).toFixed(2)} · Open {Number(a.unrealized_pnl).toFixed(2)} · Fees {Number(a.fees).toFixed(4)} · Slip {a.slippage_model_bps} bps</small></div>)}<div><small>OPEN POSITIONS</small><b>{positions.length}</b></div><div><small>RECORDED ORDERS</small><b>{orders.length}</b></div></div><div className="panel-heading"><div><h3>Open positions</h3><small>MARKED TO {provider} {dataStatus} DATA</small></div></div>{positions.length?positions.map(p=><div className="analysis-row" key={p.symbol}><span>{p.symbol} · {p.side} {p.quantity} {p.unit}</span><b>Entry {p.average_entry} · Mark {p.mark_price} · Unrealized {p.unrealized_pnl} {p.quote_currency}</b></div>):<p className="muted-paragraph">No open positions.</p>}<div className="panel-heading trade-history-heading"><div><h3>Trade history</h3><small>SIMULATED FILLS AND PENDING ORDERS ({timezoneShort})</small></div></div>{orders.slice(0,8).map(o=><div className="analysis-row" key={o.id}><span>{o.symbol} · {o.side} {o.quantity} · {o.status} · {formatTimeInTimezone(o.timestamp, timezone)} {timezoneShort}</span><b>Fill/limit {o.average_price} · Fee {o.fees??'0'} · Slippage {o.slippage??'0'} · Realized {o.realized_pnl??'0'}</b></div>)}<p className="muted-paragraph">Each paper unit is instrument-specific (not a broker lot or exchange contract). P&amp;L is reported by quote currency without FX aggregation. Stops and targets use available provider or explicitly labeled DEMO prices; no intrabar path is inferred. Limit orders remain pending until a relevant quote crosses their limit. Fees default to zero; slippage defaults to 0.5 bps. Configure <code>PAPER_INITIAL_BALANCE</code>, <code>PAPER_FEE_RATE</code>, and <code>PAPER_SLIPPAGE_BPS</code> on the backend.</p></div>;
 if(page==='Broker Connections')return <div className="panel module-panel"><div className="panel-heading"><div><h2>Provider integrations</h2><small>Catalog from backend adapters · no live brokers connected</small></div></div>{brokerList.map(b=><div className="broker-row" key={b.name}><div className="broker-avatar">{b.name[0]}</div><div><b>{b.name}</b><small>{b.integration_type} · auth: {b.authentication} · symbols: {b.supported_instruments.join(', ')||'none configured'}</small><small>Market: {b.market_data_operations.join(', ')||'none'} · account: {b.account_operations.join(', ')||'none'} · orders: {b.order_operations.join(', ')||'none'}</small><small>Required: {b.required_configuration.join(', ')||'none'}</small></div><span className={`status-pill ${b.status==='CONNECTED'?'watch':'neutral'}`}>{b.status}</span>{b.documentation_url&&<a className="text-link" href={b.documentation_url} target="_blank" rel="noreferrer">Docs</a>}</div>)}<p className="muted-paragraph">Add Integration is unavailable until an adapter and server-side OAuth/callback flow exist. API secrets belong only on the backend. No integration can place live orders.</p></div>;
 if(page==='Backtesting')return <div className="panel module-panel"><div className="panel-heading"><div><h2>Historical backtesting</h2><small>Not implemented in this local prototype</small></div></div><div className="empty-state"><BarChart3 size={28}/><b>Backtest engine awaiting implementation</b><p>Historical performance metrics are not available. No results have been fabricated.</p></div></div>;
 if(page==='Alerts')return <div className="panel module-panel"><div className="panel-heading"><div><h2>Market alerts</h2><small>In-app alert rules · Timezone: {timezoneShort}</small></div><button className="secondary-button" disabled><Plus size={14}/> Create alert · unavailable</button></div><div className="empty-state"><Bell size={28}/><b>No alert rules</b><p>Alert evaluation and notification delivery are not implemented in this prototype. When active, alert triggers are evaluated against candle timestamps in {timezoneShort}.</p></div></div>;
 if(page==='Trading Journal')return <div className="panel module-panel"><div className="panel-heading"><div><h2>Trading journal</h2><small>Manual and simulated trade records · Display timezone: {timezoneShort}</small></div></div>{orders.length?<div className="orders-table journal-table"><div className="table-head"><span>TIME ({timezoneShort})</span><span>DATE ({timezoneShort})</span><span>ORDER ID</span><span>INSTRUMENT</span><span>SIDE</span><span>QTY</span><span>AVG PRICE</span><span>STATUS</span></div>{orders.map(o=><div className="table-row" key={o.id}><span>{formatTimeInTimezone(o.timestamp,timezone)}</span><span>{formatDateInTimezone(o.timestamp,timezone)}</span><span>{o.id}</span><b>{o.symbol}</b><span className={o.side==='BUY'?'up':'down'}>{o.side}</span><span>{o.quantity}</span><span>{o.average_price}</span><span>{o.status}</span></div>)}</div>:<div className="empty-state"><Command size={28}/><b>Journal is empty</b><p>Paper order history is shown in the bottom panel. Journal entry management is not yet implemented.</p></div>}</div>;
 return <div className="panel module-panel"><div className="panel-heading"><div><h2>Settings</h2><small>Local development configuration</small></div></div><div className="analysis-row"><span>Data mode</span><b>{provider} · {dataStatus}</b></div><div className="analysis-row"><span>Live trading</span><b className="green-text">Disabled</b></div><div className="analysis-row"><span>AI configuration</span><b>Backend environment variables</b></div><div className="analysis-row"><span>Database</span><b>Optional local SQLite configuration</b></div><p className="muted-paragraph">Never provide API keys in the browser. Configure secrets in the backend environment using the provided .env.example.</p></div>;
}
