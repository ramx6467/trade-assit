from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import PaperOrderRecord
import json
import sys
import types
from decimal import Decimal

client = TestClient(app)


def _current_chat_context(openai_call):
    assert openai_call["instructions"]
    latest_turn = openai_call["input"][-1]["content"]
    marker = "Relevant backend facts for this question (authoritative; use only fields present here):\n"
    assert marker in latest_turn
    context_text = latest_turn.split(marker, 1)[1].split("\n\n", 1)[0]
    return json.loads(context_text)

def test_health_and_demo_candles():
    assert client.get('/api/v1/health').json()['live_trading_enabled'] is False
    ai_status = client.get('/api/v1/ai/status').json()
    assert 'configured' in ai_status and 'verified' in ai_status
    assert 'api_key' not in ai_status and 'OPENAI_API_KEY' not in ai_status
    data = client.get('/api/v1/market-data/candles').json()
    assert data['provider'] == 'DEMO'
    assert len(data['candles']) == 240
    assert all(x['high'] >= max(x['open'], x['close'], x['low']) for x in data['candles'])
    assert all(x['instrument'] == 'XAUUSD' and x['timeframe'] == '15m' and x['data_source'] == 'DEMO' for x in data['candles'])
    assert all(x['volume'] is None and x['volume_type'] == 'unavailable' for x in data['candles'])
    status = client.get('/api/v1/market-data/status?symbol=XAUUSD').json()
    assert status['data_status'] == 'DEMO' and status['streaming'] is False
    assert not any('key' in key.lower() or 'secret' in key.lower() for key in status)
    history = client.get('/api/v1/market-data/history?symbol=XAUUSD&timeframe=3m&limit=10').json()
    assert history['data_status'] == 'DEMO' and len(history['candles']) == 10
    assert history['candles'][0]['source'] == 'DEMO' and history['candles'][0]['is_realtime'] is False
    indicators = client.get('/api/v1/indicators?symbol=XAUUSD&timeframe=15m').json()
    assert indicators['indicators']['vwap'] is None
    assert indicators['candle_timestamp'] == data['last_successful_timestamp']
    assert indicators['series']['ema20'][-1]['time'] == data['last_successful_timestamp']
    assert indicators['data_status'] == 'DEMO'

def test_candle_payload_reports_candle_freshness_separately_from_the_quote():
    data = client.get('/api/v1/market-data/candles?symbol=XAUUSD&timeframe=5m').json()
    freshness = data['candle_freshness']
    assert freshness['latest_candle_timestamp'] == data['last_successful_timestamp']
    assert freshness['bar_seconds'] == 300
    assert freshness['stale_threshold_seconds'] == 1200
    assert freshness['status'] in {'FRESH', 'STALE', 'UNAVAILABLE'}


def test_candle_and_quote_freshness_are_independent():
    from datetime import datetime, timedelta, timezone
    from app.main import _candle_freshness, _quote_freshness
    now = datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)
    # Friday's last closed 5m bar, read two days later: stale candles...
    candle_state = _candle_freshness('5m', [{'timestamp': '2026-10-16T20:55:00Z'}], now)
    assert candle_state['status'] == 'STALE'
    assert candle_state['age_seconds'] == int((now - datetime(2026, 10, 16, 20, 55, tzinfo=timezone.utc)).total_seconds())
    # ...do not make a quote observed a moment ago anything but fresh.
    quote_state = _quote_freshness('XAUUSD', {
        'price': 4166.1,
        'timestamp': (now - timedelta(seconds=20)).isoformat().replace('+00:00', 'Z'),
    }, now)
    assert quote_state['status'] == 'FRESH'
    assert quote_state['price'] == 4166.1


def test_selected_symbol_and_timeframe_are_propagated_to_market_and_analysis():
    for symbol, timeframe in [('XAUUSD','1h'),('XAUUSD','5m')]:
        market = client.get(f'/api/v1/market-data/candles?symbol={symbol}&timeframe={timeframe}').json()
        analysis = client.get(f'/api/v1/indicators?symbol={symbol}&timeframe={timeframe}').json()
        assert market['instrument'] == symbol and market['timeframe'] == timeframe
        assert all(c['instrument']==symbol and c['timeframe']==timeframe for c in market['candles'])
        assert analysis['instrument'] == symbol and analysis['timeframe'] == timeframe

def test_structure_and_order_block_endpoints_return_scoped_rules():
    structure = client.get('/api/v1/analysis/structure?symbol=XAUUSD&timeframe=1h').json()
    order_blocks = client.get('/api/v1/analysis/order-blocks?symbol=XAUUSD&timeframe=1h').json()
    assert structure['instrument'] == order_blocks['instrument'] == 'XAUUSD'
    assert structure['timeframe'] == order_blocks['timeframe'] == '1h'
    assert 'close beyond' in structure['rules']['break_confirmation']
    assert 'aligned BOS/CHoCH' in order_blocks['rule']

def test_order_block_endpoint_exposes_methodology_and_rich_zones():
    payload = client.get('/api/v1/analysis/order-blocks?symbol=XAUUSD&timeframe=15m').json()
    method = payload['methodology']
    assert method['boundary'] == 'full' and method['break_window'] == 3
    assert 'confirmation_rule' in method and 'mitigation_rule' in method
    zones = payload['zones']
    assert isinstance(zones, list)
    for z in zones:
        assert z['instrument'] == 'XAUUSD' and z['timeframe'] == '15m'
        assert z['upper'] > z['lower']
        assert z['status'] in {'confirmed', 'candidate', 'invalidated'}
        assert z['boundary_methodology'] == 'full'
        assert z['mitigated'] in (True, False)
        assert z['mitigation_status'] in {'unconfirmed', 'unmitigated', 'partially_mitigated', 'fully_mitigated'}

def test_snapshot_order_blocks_carry_lifecycle_metadata():
    snapshot = client.get('/api/v1/analysis/snapshot?symbol=XAUUSD&timeframe=15m').json()
    assert 'order_block_methodology' in snapshot
    for z in snapshot['order_blocks']:
        assert z['status'] in {'confirmed', 'candidate', 'invalidated'}
        assert z['upper'] > z['lower'] and z['created_at']

def test_snapshot_uses_one_candle_set_for_every_analysis_result():
    snapshot=client.get('/api/v1/analysis/snapshot?symbol=XAUUSD&timeframe=5m').json()
    timestamp=snapshot['last_successful_timestamp']
    assert timestamp==snapshot['indicator_result']['candle_timestamp']
    assert all(c['instrument']=='XAUUSD' and c['timeframe']=='5m' for c in snapshot['candles'])
    assert all(z['instrument']=='XAUUSD' and z['timeframe']=='5m' for z in snapshot['fvgs'])
    assert all(z['instrument']=='XAUUSD' and z['timeframe']=='5m' for z in snapshot['order_blocks'])
    assert snapshot['setup']['candle_timestamp']==timestamp
    quote = client.get('/api/v1/market-data/quote?symbol=XAUUSD&timeframe=15m').json()
    assert quote['provider'] == 'DEMO' and quote['data_status'] == 'DEMO'

def test_invalid_instrument_and_ai_missing_config(monkeypatch):
    assert client.get('/api/v1/market-data/candles?symbol=BAD').status_code == 400
    monkeypatch.setenv('AI_PROVIDER', 'openai')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    response = client.post('/api/v1/ai/chat', json={'message':'Analyze this'})
    assert response.status_code == 503
    assert 'OPENAI_API_KEY' in response.json()['detail']

def test_live_analysis_is_refused_for_demo_provider():
    response = client.post('/api/v1/ai/chat', json={'message':'Give me live analysis for EURUSD'})
    assert response.status_code == 409
    assert 'synthetic DEMO' in response.json()['detail']

def test_demo_websocket_reports_demo_and_does_not_emit_fake_ticks():
    with client.websocket_connect('/ws/market-data?symbol=XAUUSD') as socket:
        status = socket.receive_json()
        assert status['type'] == 'status'
        assert status['data_status'] == 'DEMO'
        assert status['streaming'] is False
        assert 'price' not in status

def test_questions_discussing_live_data_are_not_misclassified_as_live_requests(monkeypatch):
    monkeypatch.setenv('AI_PROVIDER', 'openai')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    response = client.post('/api/v1/ai/chat', json={'message':'Explain why this is not live market data'})
    assert response.status_code == 503
    assert 'not configured' in response.json()['detail']

def test_ai_receives_backend_market_context_without_real_api_call(monkeypatch):
    calls = {}
    class Responses:
        def create(self, **kwargs):
            calls.update(kwargs)
            return types.SimpleNamespace(output_text='This is synthetic demo data.')
    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = Responses()
    monkeypatch.setenv('AI_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(OpenAI=FakeClient))
    response = client.post('/api/v1/ai/chat', json={'message':'Explain these values', 'symbol':'XAUUSD', 'timeframe':'1h'})
    assert response.status_code == 200
    assert response.json()['data_status'] == 'DEMO'
    context = _current_chat_context(calls)
    assert context['instrument'] == 'XAUUSD' and context['timeframe'] == '1h'
    assert context['provider'] == 'DEMO' and context['data_status'] == 'DEMO'
    assert context['recent_candles'][-1]['instrument'] == 'XAUUSD'
    assert context['indicators']['vwap'] is None
    assert context['strategy']['status'] in {'WATCHLIST', 'NO_SETUP'}

def test_ai_context_is_bounded_and_de_duplicated(monkeypatch):
    """The chat payload must stay lean: bounded windows, no repeated constant
    fields per entry, and no opaque internal ids."""
    calls = {}
    class Responses:
        def create(self, **kwargs):
            calls.update(kwargs)
            return types.SimpleNamespace(output_text='ok')
    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = Responses()
    monkeypatch.setenv('AI_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(OpenAI=FakeClient))
    response = client.post('/api/v1/ai/chat', json={'message':'Analyze the current setup', 'symbol':'XAUUSD', 'timeframe':'15m'})
    assert response.status_code == 200
    context = _current_chat_context(calls)
    assert len(context['recent_candles']) <= 24
    assert len(context['fvgs']) <= 10
    assert len(context['order_blocks']) <= 10
    # Constant per-entry fields and opaque ids are omitted.
    for candle in context['recent_candles']:
        assert 'data_source' not in candle and 'is_realtime' not in candle
    for zone in context['fvgs'] + context['order_blocks']:
        assert 'id' not in zone and 'instrument' not in zone and 'data_source' not in zone
    # Indicator values are carried once under `indicators`.
    assert 'ema20' not in context and 'vwap' not in context and 'bollinger_bands' not in context
    structure = context['market_structure']
    assert len(structure['swing_highs']) <= 8 and len(structure['breaks']) <= 6
    # The whole attached context stays well under a fixed budget.
    assert len(json.dumps(context, separators=(',', ':'), default=str)) < 25000

def test_ai_provider_error_is_sanitized(monkeypatch):
    class Responses:
        def create(self, **kwargs):
            raise RuntimeError('secret credential detail')
    class FakeClient:
        def __init__(self, **kwargs): self.responses=Responses()
    monkeypatch.setenv('AI_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(OpenAI=FakeClient))
    response=client.post('/api/v1/ai/chat',json={'message':'Explain indicators','symbol':'XAUUSD','timeframe':'15m'})
    assert response.status_code == 502
    assert 'secret credential detail' not in response.text

def test_ai_verify_endpoint_makes_real_sdk_request_and_records_success(monkeypatch):
    calls = {}
    class Responses:
        def create(self, **kwargs):
            calls.update(kwargs)
            return types.SimpleNamespace(output_text='OK')
    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs['timeout'] == 20.0
            self.responses = Responses()
    monkeypatch.setenv('AI_PROVIDER', 'openai')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key')
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(OpenAI=FakeClient))
    response = client.post('/api/v1/ai/verify')
    assert response.status_code == 200
    assert response.json()['configured'] is True
    assert response.json()['verified'] is True
    assert response.json()['verification_status'] == 'VERIFIED'
    assert calls['model'] == 'test-model' and calls['store'] is False
    assert 'test-only-key' not in response.text

def test_ai_verify_endpoint_classifies_auth_model_rate_limit_and_network_errors():
    from app.main import _ai_error_status
    assert _ai_error_status(type('AuthenticationError', (Exception,), {})('x'))[0] == 'invalid_credentials'
    assert _ai_error_status(type('NotFoundError', (Exception,), {})('x'))[0] == 'unsupported_model'
    assert _ai_error_status(type('RateLimitError', (Exception,), {})('x'))[0] == 'rate_limited'
    assert _ai_error_status(type('APIConnectionError', (Exception,), {})('x'))[0] == 'network_error'

def test_ai_status_exposes_only_four_real_connection_states(monkeypatch):
    monkeypatch.setenv('AI_PROVIDER', 'openai')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    missing=client.get('/api/v1/ai/status').json()
    assert missing['verification_status']=='NOT_CONFIGURED' and not missing['verified']
    monkeypatch.setenv('OPENAI_API_KEY','status-only-test-key')
    monkeypatch.setenv('OPENAI_MODEL','status-only-test-model')
    configured=client.get('/api/v1/ai/status').json()
    assert configured['verification_status']=='CONNECTING' and configured['configured'] and not configured['verified']
    assert configured['error_code'] is None and 'test-only-key' not in str(configured)

def test_order_is_simulated_and_history_records_it():
    with SessionLocal() as db:
        db.query(PaperOrderRecord).delete()
        db.commit()
    result = client.post('/api/v1/paper/orders', json={'symbol':'XAUUSD','side':'BUY','quantity':'1'})
    assert result.status_code == 200
    assert result.json()['status'] == 'SIMULATED_FILLED'
    assert client.get('/api/v1/paper/history').json()['orders'][0]['label'] == 'SIMULATED'
    with SessionLocal() as db:
        db.query(PaperOrderRecord).filter_by(id=result.json()['id']).delete()
        db.commit()

def test_paper_market_order_persists_valid_bracket_and_rejects_wrong_side():
    price = float(client.get('/api/v1/market-data/quote?symbol=XAUUSD&timeframe=15m').json()['price'])
    stop = round(price - 50, 2)
    target = round(price + 50, 2)
    result = client.post('/api/v1/paper/orders', json={'symbol':'XAUUSD','side':'BUY','quantity':'1','stop_loss':str(stop),'take_profit':str(target)})
    assert result.status_code == 200
    assert result.json()['status'] == 'SIMULATED_FILLED'
    assert result.json()['stop_loss'] == f'{stop:.8f}'
    assert result.json()['take_profit'] == f'{target:.8f}'
    assert result.json()['label'] == 'SIMULATED'
    bad = client.post('/api/v1/paper/orders', json={'symbol':'XAUUSD','side':'BUY','quantity':'1','stop_loss':str(target)})
    assert bad.status_code == 400
    invalid_qty = client.post('/api/v1/paper/orders', json={'symbol':'XAUUSD','side':'BUY','quantity':'0'})
    assert invalid_qty.status_code == 422
    with SessionLocal() as db:
        db.query(PaperOrderRecord).filter_by(id=result.json()['id']).delete()
        db.commit()

def test_paper_limit_order_is_pending_and_requires_entry():
    price = float(client.get('/api/v1/market-data/quote?symbol=XAUUSD&timeframe=15m').json()['price'])
    pending_entry = round(price - 50, 2)
    marketable_entry = round(price + 500, 2)
    missing = client.post('/api/v1/paper/orders', json={'symbol':'XAUUSD','side':'BUY','quantity':'1','order_type':'LIMIT'})
    assert missing.status_code == 422
    result = client.post('/api/v1/paper/orders', json={'symbol':'XAUUSD','side':'BUY','quantity':'1','order_type':'LIMIT','entry_price':str(pending_entry)})
    assert result.status_code == 200
    assert result.json()['status'] == 'PENDING'
    assert result.json()['average_price'] == f'{pending_entry:.8f}'
    triggered=client.post('/api/v1/paper/orders',json={'symbol':'XAUUSD','side':'BUY','quantity':'1','order_type':'LIMIT','entry_price':str(marketable_entry)})
    filled=client.get('/api/v1/paper/history').json()
    assert next(o for o in filled['orders'] if o['id']==triggered.json()['id'])['status']=='SIMULATED_FILLED'
    from app import main
    with SessionLocal() as db:
        db.query(PaperOrderRecord).filter(PaperOrderRecord.id.in_([result.json()['id'],triggered.json()['id']])).delete(synchronize_session=False)
        db.query(main.PaperPositionRecord).delete()
        db.commit()

def test_paper_ledger_marks_positions_and_closes_on_target(monkeypatch):
    from app import main
    with SessionLocal() as db:
        db.query(PaperOrderRecord).delete()
        db.query(main.PaperPositionRecord).delete()
        db.commit()
    current = {'price': 2342.21}
    monkeypatch.setattr(main.market_data, 'get_latest_quote', lambda symbol, timeframe='15m': {'price':current['price'],'provider':'DEMO','data_status':'DEMO'})
    created=client.post('/api/v1/paper/orders',json={'symbol':'XAUUSD','side':'BUY','quantity':'2','stop_loss':'2322.21','take_profit':'2362.21'})
    assert created.status_code == 200
    assert created.json()['fees'] == '0E-8'
    assert created.json()['slippage'] != '0E-8'
    account=client.get('/api/v1/paper/history').json()
    assert account['positions'][0]['label']=='SIMULATED'
    assert account['positions'][0]['quantity']=='2.000000'
    assert 'USD' in account['accounts_by_currency']
    assert account['accounts_by_currency']['USD']['equity'] != account['accounts_by_currency']['USD']['initial_balance']
    current['price']=2400.00
    closed=client.get('/api/v1/paper/history').json()
    assert not closed['positions']
    assert any(o['status']=='SIMULATED_TAKE_PROFIT_FILLED' for o in closed['orders'])
    current['price']=2400.00
    stopped=client.post('/api/v1/paper/orders',json={'symbol':'XAUUSD','side':'BUY','quantity':'1','stop_loss':'2380.00'})
    assert stopped.status_code == 200
    current['price']=2300.00
    stop_result=client.get('/api/v1/paper/history').json()
    assert not stop_result['positions']
    assert any(o['status']=='SIMULATED_STOP_LOSS_FILLED' for o in stop_result['orders'])
    with SessionLocal() as db:
        db.query(PaperOrderRecord).delete()
        db.query(main.PaperPositionRecord).delete()
        db.commit()

def test_paper_fees_and_zero_slippage_are_configurable(monkeypatch):
    from app import main
    monkeypatch.setenv('PAPER_FEE_RATE','0.001')
    monkeypatch.setenv('PAPER_SLIPPAGE_BPS','0')
    with SessionLocal() as db:
        db.query(PaperOrderRecord).delete()
        db.query(main.PaperPositionRecord).delete()
        db.commit()
    result=client.post('/api/v1/paper/orders',json={'symbol':'XAUUSD','side':'BUY','quantity':'1'})
    assert result.status_code==200
    assert Decimal(result.json()['fees']) > 0
    assert Decimal(result.json()['slippage']) == 0
    account=client.get('/api/v1/paper/history').json()
    assert Decimal(account['accounts_by_currency']['USD']['fees']) > 0
    with SessionLocal() as db:
        db.query(PaperOrderRecord).delete()
        db.query(main.PaperPositionRecord).delete()
        db.commit()

def test_buy_sell_evaluation_is_structured_scoped_and_demo_labeled():
    for side in ('BUY','SELL'):
        result = client.get(f'/api/v1/analysis/evaluate?symbol=XAUUSD&timeframe=1h&side={side}')
        assert result.status_code == 200
        data = result.json()
        assert data['instrument'] == 'XAUUSD' and data['timeframe'] == '1h'
        assert data['side'] == side and data['data_status'] == 'DEMO'
        assert data['status'] in {'NO_SETUP','DATA_UNAVAILABLE','POTENTIAL_BUY','POTENTIAL_SELL','WATCHLIST','BUY_CANDIDATE','SELL_CANDIDATE','DATA_STALE'}
        assert data['direction'] == 'NO_SETUP' and data['entry_price'] is None and data['stop_loss'] is None
        assert data['market_data_status'] == 'DEMO' and data['source'] == 'DEMO'
        assert data['take_profit_1'] is None and data['take_profit_2'] is None
        assert data['analysis_timestamp'] and 'supporting_indicators' in data and 'risks' in data
        assert isinstance(data['conditions_satisfied'], list) and isinstance(data['conditions_not_satisfied'], list)
        assert 'invalidation_conditions' in data and data['evaluation_timestamp']

def test_fresh_provider_evaluation_calculates_tick_aware_buy_and_sell_levels(monkeypatch):
    from datetime import datetime, timezone, timedelta
    import app.main as main
    stamp=(datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat().replace('+00:00','Z')
    candle={'timestamp':stamp,'open':1.1,'high':1.12,'low':1.08,'close':1.11,'volume':None,'instrument':'EURUSD','timeframe':'15m'}
    monkeypatch.setattr(main,'_candles',lambda *args:([candle],[]))
    monkeypatch.setattr(main,'_data_status',lambda symbol:'LIVE')
    monkeypatch.setattr(main,'_provider_id',lambda symbol:'TWELVE_DATA')
    monkeypatch.setattr(main,'_indicator_result',lambda *args:{'indicators':{'ema20':1.09,'ema50':1.08,'rsi14':60,'atr14':.0002,'macd':{'line':.001,'signal':.0008}}})
    monkeypatch.setattr(main,'market_structure',lambda rows:{'breaks':[{'direction':'bullish','confirmed_at':stamp,'type':'BOS','level':1.1}]})
    monkeypatch.setattr(main,'detect_fvgs',lambda *args,**kwargs:[])
    monkeypatch.setattr(main,'detect_order_blocks',lambda *args,**kwargs:[])
    buy=main._evaluate_setup_structured('EURUSD','15m','BUY')
    assert buy['direction']=='BUY' and buy['setup_type']=='RETEST_ENTRY'
    assert buy['entry_price'] != buy['current_price']
    assert buy['stop_loss'] < buy['entry_price'] < buy['take_profit_1'] < buy['take_profit_2']
    assert buy['risk_reward_ratio']>=1.5 and buy['position_sizing_example']['quantity']>0
    candle['close']=1.09
    monkeypatch.setattr(main,'_indicator_result',lambda *args:{'indicators':{'ema20':1.11,'ema50':1.12,'rsi14':40,'atr14':.0002,'macd':{'line':-.001,'signal':-.0008}}})
    monkeypatch.setattr(main,'market_structure',lambda rows:{'breaks':[{'direction':'bearish','confirmed_at':stamp,'type':'BOS','level':1.1}]})
    sell=main._evaluate_setup_structured('EURUSD','15m','SELL')
    assert sell['direction']=='SELL' and sell['stop_loss']>sell['entry_price']>sell['take_profit_1']>sell['take_profit_2']
    monkeypatch.setenv('PAPER_MIN_RR','3')
    rejected=main._evaluate_setup_structured('EURUSD','15m','SELL')
    assert rejected['direction']=='NO_SETUP' and rejected['entry_price'] is None

def test_position_sizing_respects_configured_maximum_risk(monkeypatch):
    monkeypatch.setenv('PAPER_MAX_RISK_PERCENT','0.5')
    response=client.post('/api/v1/risk/position-size',json={'equity':'10000','risk_percent':'1','side':'BUY','entry':'10','stop_loss':'9','unit_value':'1'})
    assert response.status_code==400 and 'configured 0.5%' in response.json()['detail']

def test_provider_failure_returns_structured_blocked_setup(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main,'_candles',lambda *args:(_ for _ in ()).throw(main.ProviderError('provider request unavailable')))
    monkeypatch.setattr(main,'_provider_id',lambda symbol:'TWELVE_DATA')
    monkeypatch.setattr(main,'_data_status',lambda symbol:'ERROR')
    result=client.get('/api/v1/analysis/evaluate?symbol=XAUUSD&timeframe=15m&side=BUY')
    assert result.status_code==200
    data=result.json()
    assert data['direction']=='NO_SETUP' and data['status']=='DATA_UNAVAILABLE'
    assert data['entry_price'] is None and data['stop_loss'] is None and data['take_profit_1'] is None
    assert 'provider request unavailable' in data['risks'][0]

def test_integration_catalog_and_tradingview_status_are_honest():
    providers = client.get('/api/v1/brokers').json()
    # Single-instrument deployment: the DEMO feed plus the gold market-data
    # integrations, and none of the removed broker/market-data rows.
    assert {p['name'] for p in providers} == {
        'DEMO Market Data',
        'xaus.com (XAUS Gold API)',
        'goldprice.dev (Gold API)',
    }
    assert all(p['supported_instruments'] == ['XAUUSD'] for p in providers)
    assert all(p['status'] != 'CONNECTED' or p['name'] == 'DEMO Market Data' for p in providers)
    tv = client.get('/api/v1/integrations/tradingview').json()
    assert tv['account_authenticated'] is False and tv['account_data_access'] is False
    assert tv['chart_widget_enabled'] is False

def test_risk_requires_metadata_and_calculates_decimal_size():
    missing = client.post('/api/v1/risk/position-size', json={'equity':'10000','risk_percent':'1','entry':'10','stop_loss':'9'})
    assert missing.status_code == 422
    ok = client.post('/api/v1/risk/position-size', json={'equity':'10000','risk_percent':'1','entry':'10','stop_loss':'9','unit_value':'2'})
    assert ok.json()['quantity'] == '50.00'
    invalid_stop = client.post('/api/v1/risk/position-size', json={'equity':'10000','risk_percent':'1','entry':'10','stop_loss':'11','unit_value':'2'})
    assert invalid_stop.status_code == 400


def test_market_session_endpoint_keeps_session_and_data_separate():
    resp = client.get('/api/v1/market/session?symbol=XAUUSD')
    assert resp.status_code == 200
    body = resp.json()
    # Session is reported separately from data.
    assert body['session']['status'] in {'OPEN', 'CLOSED', 'PRE_OPEN', 'HOLIDAY', 'SCHEDULE_UNKNOWN'}
    assert body['market_status'] == body['session']['status']
    assert 'holiday_calendar' in body['session']
    # XAU/USD now reports a documented OTC spot-gold reference-feed session, so
    # the verdict is an OPEN/CLOSED calendar fact rather than an unverified one.
    assert body['session']['schedule_verified'] is True
    assert body['session']['session_source'] == 'documented'
    assert body['session']['session_basis'] == 'reference_feed'
    assert body['session']['status'] in {'OPEN', 'CLOSED'}
    # Step 3 contract: the top-level response exposes each field explicitly.
    assert body['session_status'] == body['session']['status']
    assert body['source'] == 'documented'
    assert body['checked_at'] and body['timezone'] == 'America/New_York'
    assert body['explanation']
    assert 'next_open' in body and 'next_close' in body
    # A display label and provenance are always reported alongside the machine
    # status so the UI never has to interpret an unverified calendar as fact.
    assert body['session']['status_label'] in {'OPEN', 'CLOSED', 'PRE_OPEN', 'HOLIDAY', 'SCHEDULE UNVERIFIED'}
    assert body['session']['session_source'] in {'provider', 'documented', 'unverified'}
    # Data status lives in its own object and never implies market-open state.
    assert 'data_status' in body['data']
    assert 'quote_available' in body['data']
    assert 'price' not in body['session']


def test_market_session_endpoint_rejects_unknown_instrument():
    assert client.get('/api/v1/market/session?symbol=NOPE').status_code == 400


def test_market_session_data_uses_resolved_connection_status():
    body = client.get('/api/v1/market/session?symbol=XAUUSD').json()
    # The provider exposes a static "DISCONNECTED" placeholder; the resolved
    # connection state has its own vocabulary and must never be the placeholder.
    assert body['data']['connection_status'] != 'DISCONNECTED'
    assert body['data']['connection_status'] in {
        'STREAMING', 'CONNECTED', 'CONFIGURED', 'ERROR',
        'QUOTA_EXCEEDED', 'RATE_LIMITED', 'DEMO',
    }
    # Freshness is reported as a number (or null) rather than being hardcoded.
    assert body['data']['freshness_seconds'] is None or isinstance(body['data']['freshness_seconds'], int)


def test_gold_reference_feed_session_is_verified():
    body = client.get('/api/v1/market/session?symbol=XAUUSD').json()
    assert body['session']['schedule_verified'] is True
    assert body['session']['session_source'] == 'documented'
    assert body['session']['session_basis'] == 'reference_feed'
    # The reference-feed clock only ever returns a calendar OPEN/CLOSED fact.
    assert body['session']['status'] in {'OPEN', 'CLOSED'}
    assert body['market_status'] == body['session']['status']
    assert body['session']['schedule_source']
    assert body['session']['schedule_note']


def test_market_diagnostics_reports_quote_and_candle_provenance():
    body = client.get('/api/v1/market-data/diagnostics?symbol=XAUUSD&timeframe=5m').json()
    assert body['symbol'] == 'XAUUSD'
    assert body['timeframe'] == '5m'
    assert set(body) >= {'provider', 'quote', 'candle', 'sources', 'sources_differ'}
    assert body['candle']['open_time'] is not None
    assert body['candle']['close_price'] is not None
    assert body['candle']['bar_seconds'] == 300
    assert body['candle']['state'] in {'forming', 'closed'}
    assert body['sources']['candles']['provider']
    # The diagnostics never leak a key, token or secret.
    blob = json.dumps(body).lower()
    assert 'api_key' not in blob and 'secret' not in blob and 'token' not in blob


def test_price_diagnostics_reports_quote_age_candle_state_and_divergence(monkeypatch):
    # The diagnostics must compare the *same* two numbers the UI shows: the live
    # quote and the newest candle close, with the candle's forming/closed state.
    from datetime import datetime, timezone
    from app import main

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(main, "_status_quote", lambda symbol, configured: {
        "price": 4166.1, "timestamp": now.isoformat(), "is_stale": False, "price_source": "gold-api.com",
    })
    monkeypatch.setattr(
        main, "_candles",
        lambda symbol, timeframe, limit=2, start=None, end=None: ([{
            "timestamp": "2026-10-16T20:55:00Z", "open": 4185.0, "high": 4187.3,
            "low": 4181.1, "close": 4181.1, "volume": 501.0, "volume_type": "exchange",
        }], []),
    )
    monkeypatch.setattr(main, "_data_status", lambda symbol: "DELAYED")

    body = main._price_diagnostics("XAUUSD", "5m")
    assert body["quote"]["price"] == 4166.1
    assert body["quote"]["age_seconds"] is not None and body["quote"]["age_seconds"] < 5
    assert body["candle"]["close_price"] == 4181.1
    assert body["candle"]["bar_seconds"] == 300
    assert body["candle"]["state"] == "closed"      # Friday's bar is long closed
    assert body["sources_differ"] is True
    assert abs(body["quote_minus_chart_close"] - (4166.1 - 4181.1)) < 1e-6


def test_market_status_exposes_quote_and_candle_sources_separately():
    body = client.get('/api/v1/market-data/status?symbol=XAUUSD').json()
    assert 'sources' in body
    assert body['sources']['provider'] == body['provider']
    assert set(body['sources']) == {'provider', 'quote', 'candles'}
    # The quote source is reported on its own, never conflated with the candle feed.
    assert 'source' in body['sources']['quote'] and 'detail' in body['sources']['candles']


class _ConfiguredProvider:
    """Minimal configured provider stub: reachable credentials, no quote cost."""

    def status(self, symbol):
        return {'symbol': symbol, 'provider': 'SIFTING', 'configured': True}

    def provider_for(self, symbol):
        # No get_market_session hook, so the session service falls back to the
        # documented schedule exactly as it does for the real DEMO/DISCONNECTED
        # provider path.
        return self


def test_fresh_quote_while_the_session_is_unknown_keeps_both_signals(monkeypatch):
    # A fresh quote must read as LIVE data even when the session calendar cannot
    # be verified - the two signals are independent and neither overrides the
    # other.
    from datetime import datetime, timezone
    from app import main

    monkeypatch.setattr(main, 'market_data', _ConfiguredProvider())
    monkeypatch.setitem(main._last_market_updates, 'XAUUSD', datetime.now(timezone.utc).isoformat())

    unknown = dict(main.market_session('XAUUSD'))
    unknown.update({
        'status': 'SCHEDULE_UNKNOWN',
        'status_label': 'SCHEDULE UNVERIFIED',
        'session_source': 'unverified',
        'schedule_verified': False,
    })
    monkeypatch.setattr(main, 'market_session', lambda symbol, **kwargs: unknown)

    body = client.get('/api/v1/market/session?symbol=XAUUSD').json()
    assert body['data']['data_status'] == 'LIVE'          # fresh, verified tick
    assert body['session']['status'] == 'SCHEDULE_UNKNOWN'  # independent calendar fact
    assert body['session_status'] == 'SCHEDULE_UNKNOWN'
    assert body['source'] == 'unverified'
    assert body['market_status'] == 'SCHEDULE_UNKNOWN'


def test_stale_quote_while_the_session_is_open_keeps_both_signals(monkeypatch):
    # The reverse independence check: an OPEN calendar fact must never promote a
    # stale quote to LIVE, and the stale quote must not close the session.
    from datetime import datetime, timedelta, timezone
    from app import main

    monkeypatch.setattr(main, 'market_data', _ConfiguredProvider())
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    monkeypatch.setitem(main._last_market_updates, 'XAUUSD', stale)

    open_session = dict(main.market_session('XAUUSD'))
    open_session.update({'status': 'OPEN', 'status_label': 'OPEN', 'session_source': 'documented'})
    monkeypatch.setattr(main, 'market_session', lambda symbol, **kwargs: open_session)

    body = client.get('/api/v1/market/session?symbol=XAUUSD').json()
    assert body['session']['status'] == 'OPEN'
    assert body['market_status'] == 'OPEN'
    assert body['data']['data_status'] != 'LIVE'  # stale quote is never promoted


def test_fresh_quote_while_the_session_reports_closed_keeps_both_signals(monkeypatch):
    # A session verdict and data freshness are independent: a fresh quote can
    # coexist with a CLOSED session verdict without either overriding the other.
    from datetime import datetime, timezone
    from app import main

    monkeypatch.setattr(main, 'market_data', _ConfiguredProvider())
    monkeypatch.setitem(main._last_market_updates, 'XAUUSD', datetime.now(timezone.utc).isoformat())

    closed = dict(main.market_session('XAUUSD'))
    closed.update({'status': 'CLOSED', 'status_label': 'CLOSED', 'session_source': 'documented'})
    monkeypatch.setattr(main, 'market_session', lambda symbol, **kwargs: closed)

    body = client.get('/api/v1/market/session?symbol=XAUUSD').json()
    assert body['data']['data_status'] == 'LIVE'  # fresh quote
    assert body['market_status'] == 'CLOSED'      # not conflated with freshness



def test_ai_status_exposes_separated_checks():
    body = client.get('/api/v1/ai/status').json()
    assert 'state' in body and 'label' in body
    assert set(body['checks']) >= {'configuration', 'provider_connectivity', 'generation', 'quota'}


def test_market_context_separates_quote_and_candle_freshness():
    """The AI context must expose quote freshness and candle freshness independently."""
    from app.main import _build_market_context
    ctx = _build_market_context('XAUUSD', '15m')
    assert 'quote' in ctx and 'candle_data' in ctx and 'data_freshness' in ctx
    freshness = ctx['data_freshness']
    assert freshness['quote_status'] in {'FRESH', 'STALE', 'UNAVAILABLE'}
    assert freshness['candle_status'] in {'FRESH', 'STALE', 'UNAVAILABLE'}
    # The two dimensions are reported independently, never collapsed into one.
    assert freshness['quote_status'] == ctx['quote']['status']
    assert freshness['candle_status'] == ctx['candle_data']['status']
    assert 'candle_count' in freshness
    # SMC/ICT structure the assistant is allowed to quote.
    assert 'premium_discount' in ctx
    assert 'dealing_range' in ctx
    assert 'invalidation_levels' in ctx
    assert 'equal_highs' in ctx['market_structure'] and 'equal_lows' in ctx['market_structure']


def test_candle_freshness_is_stale_independently_of_a_fresh_quote():
    """A fresh quote must never make a stale candle series read as fresh."""
    from datetime import datetime, timedelta, timezone
    from app.main import _candle_freshness, _quote_freshness

    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(hours=36)).isoformat().replace('+00:00', 'Z')
    rows = [{'timestamp': old, 'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0}]
    candles = _candle_freshness('15m', rows, now)
    assert candles['status'] == 'STALE'
    assert candles['age_seconds'] > candles['stale_threshold_seconds']

    quote = _quote_freshness('XAUUSD', {'price': 4166.1, 'timestamp': now.isoformat(), 'is_stale': False}, now)
    assert quote['status'] == 'FRESH'
    # The fresh quote does not launder the stale candles.
    assert candles['status'] == 'STALE'


def test_snapshot_reports_candle_freshness_field():
    body = client.get('/api/v1/analysis/snapshot?symbol=XAUUSD&timeframe=15m').json()
    assert 'candle_freshness' in body
    assert body['candle_freshness']['status'] in {'FRESH', 'STALE', 'UNAVAILABLE'}
    assert body['candle_freshness']['candle_count'] >= 1


def test_market_status_reports_quote_status_field():
    body = client.get('/api/v1/market-data/status?symbol=XAUUSD').json()
    assert body['quote_status'] in {'FRESH', 'STALE', 'UNAVAILABLE'}


def test_candle_diagnostics_endpoint_traces_one_instant(monkeypatch):
    """The diagnostic proves the raw -> UTC -> IST chain and component agreement.

    The synthetic provider's clock is frozen so the assertion is about the
    conversion, not about when the suite happens to run.
    """
    from datetime import datetime, timezone

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 16, 20, 59, 30, tzinfo=tz or timezone.utc)

    monkeypatch.setattr("app.market_data.demo.datetime", _Frozen)
    data = client.get("/api/v1/diagnostics/candles?symbol=XAUUSD&timeframe=5m").json()
    for field in (
        "symbol", "requested_timeframe", "provider", "server_now_utc", "server_now_ist",
        "raw_latest_timestamp", "raw_timestamp_format", "normalized_latest_timestamp_utc",
        "latest_candle_open_utc", "latest_candle_display_ist", "latest_candle_age_seconds",
        "latest_quote_timestamp_utc", "latest_quote_age_seconds", "cache_created_at_utc",
        "cache_age_seconds", "source_interval", "data_is_stale", "timestamp_validation_passed",
        "candle_freshness", "quote_freshness", "consistency",
    ):
        assert field in data, field
    assert data["symbol"] == "XAUUSD" and data["requested_timeframe"] == "5m"
    assert data["source_interval"] == "5m" and data["source_interval_kind"] == "native"
    assert data["display_timezone"] == "Asia/Kolkata"
    # 20:59:30Z is inside the still-forming 5m bar, which is excluded, so the
    # newest *completed* bar opens at 20:50:00Z - and that instant is 02:20 IST
    # the next calendar day, on Saturday.
    assert data["normalized_latest_timestamp_utc"] == "2026-10-16T20:50:00Z"
    assert data["latest_candle_open_utc"] == data["normalized_latest_timestamp_utc"]
    assert data["latest_candle_display_ist"] == "2026-10-17 02:20:00 IST"
    assert data["timestamp_validation_passed"] is True
    assert data["candle_freshness"]["latest_candle_timestamp"] == data["latest_candle_open_utc"]
    # Every component that renders the last candle resolves to that one instant.
    consistency = data["consistency"]
    assert consistency["components_resolved"] >= 3
    assert consistency["all_components_agree"] is True
    assert consistency["snapshot_last_successful_timestamp"] == "2026-10-16T20:50:00Z"
    assert consistency["chart_series_epoch_seconds"] == 1792183800
    # The synthetic feed has no wire timestamp and no cache: nulls, explained.
    assert data["raw_latest_timestamp"] is None and data["cache_created_at_utc"] is None
    assert data["raw_timestamp_format"] == "missing"
    assert data["notes"]
    # A frozen, weekend-frozen candle series is STALE, never promoted to live.
    assert data["data_is_stale"] is True
    # Diagnostics are read-only and must never expose credentials.
    blob = json.dumps(data).lower()
    assert "api_key" not in blob and "secret" not in blob and "token" not in blob
    assert client.get("/api/v1/diagnostics/candles?symbol=XAUUSD&timeframe=7m").status_code == 400
