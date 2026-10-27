import {afterEach,describe,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import AIChatWorkspace,{type ChatConversation,type ChatMessage} from './AIChatWorkspace';

const conversation:ChatConversation={id:'c1',title:'New conversation',updatedAt:'2026-10-25T10:00:00Z',messages:[]};
function renderChat(messages:ChatMessage[]=[],busy=false){
 const onSend=vi.fn();
 render(<AIChatWorkspace conversations={[conversation]} activeId="c1" messages={messages} busy={busy}
  status="UNVERIFIED" statusMessage="Not verified" configured={true} verified={false} verifying={false}
  provider="gemini" symbol="XAUUSD" timeframe="15m" timeframes={['5m','15m']}
  onNew={()=>{}} onSelect={()=>{}} onClear={()=>{}} onVerify={()=>{}} onSend={onSend} onBack={()=>{}}/>);
 return onSend;
}

afterEach(cleanup);

describe('AI chat input and response rendering',()=>{
 it('submits the latest trimmed input exactly once with the selected chart context',()=>{
  const onSend=renderChat();
  fireEvent.change(screen.getByLabelText('Message TradeMind AI'),{target:{value:'  Analyze the current chart  '}});
  fireEvent.click(screen.getByRole('button',{name:/send/i}));
  expect(onSend).toHaveBeenCalledTimes(1);
  expect(onSend).toHaveBeenCalledWith('Analyze the current chart','XAUUSD','15m',true);
  expect(screen.getByLabelText('Message TradeMind AI')).toHaveValue('');
 });

 it('shows a failed current request as an error, not as a prior assistant answer',()=>{
  renderChat([
   {role:'user',text:'First question'},
   {role:'assistant',text:'Earlier answer'},
   {role:'user',text:'Second question'},
   {role:'assistant',text:'Gemini request failed',failed:true},
  ]);
  expect(screen.getByText('Earlier answer')).toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('Request failed');
  expect(screen.getByRole('alert')).toHaveTextContent('Gemini request failed');
 });

 it('does not submit an empty draft or submit again while a response is pending',()=>{
  const onSend=renderChat([],true);
  fireEvent.click(screen.getByRole('button',{name:/send/i}));
  expect(onSend).not.toHaveBeenCalled();
  expect(screen.getByRole('button',{name:/send/i})).toBeDisabled();
 });
});
