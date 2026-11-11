'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.listeners={};this.disabled=false;this.textContent='';}
  append(...nodes){this.children.push(...nodes);if(this.tag==='select'&&!this.value&&this.children.length)this.value=this.children[0].value;}
  replaceChildren(...nodes){this.children=[];if(this.tag==='select')this.value='';this.append(...nodes);}
  get firstChild(){return this.children[0];}
  setAttribute(){}
  addEventListener(event,fn){this.listeners[event]=fn;}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function harness(extra){
  const nodes=new Map(),calls=[],cancellations=[];
  const get=id=>{if(!nodes.has(id))nodes.set(id,new Element(['strategy','mode','cost','symbol','direction','outcome'].includes(id)?'select':'div'));return nodes.get(id);};
  get('mode').value='day';get('cost').value='gross';
  const metadata={schema:'trader-journal-api-v1',generation:'abc',generated_at:new Date().toISOString(),import:{errors:[]}};
  const streams=['intraday','long'].map(strategy=>({id:strategy+':hash',strategy,environment:'sandbox',dates:['2026-11-11']}));
  const stats={gross:'1',net:'0.9',closures:1,account:{},summary:{},by_symbol:[]};
  const bundle=p=>({...metadata,stream:{...streams.find(s=>s.id===p.stream),periods:{'2026-11':stats,'2026-11-11':stats},warnings:[],positions:{},observed_at:metadata.generated_at},overview:streams.map(s=>({...s,stats})),operation_quality:{missing:0,zero:0}});
  const loader={cancelPending:kind=>cancellations.push(kind||'all'),loadReport:async()=>({...metadata,streams:streams.map(s=>({...s}))}),loadJSON:async(kind,p)=>{
    calls.push({kind,p:{...p}});if(extra){const override=extra(kind,p,bundle);if(override)return override;}
    return kind==='summary'?bundle(p):{...metadata,items:kind==='signals'?[{at:'2026-11-11T08:00Z',symbol:p.cursor?'SECOND':'FIRST',reasons:[],side:'buy'}]:[],total:kind==='signals'?101:0,period_total:0,next_cursor:kind==='signals'&&!p.cursor?'page2':null};
  }};
  vm.runInNewContext(fs.readFileSync(require.resolve('./journal.js'),'utf8'),{JournalModel:require('./journal-model.js'),JournalLoader:loader,document:{getElementById:get,createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag)},setTimeout,clearTimeout});
  return {get,calls,loader,cancellations};
}
test('initial screen fetches summary and trades only; signals are lazy and paginated',async()=>{
  const h=harness();await tick();
  assert.deepEqual(h.calls.map(x=>x.kind),['summary','trades']);
  assert.equal(h.get('reload').disabled,false);assert.match(h.get('periodTitle').textContent,/INTRADAY/);
  h.get('signalsDetails').open=true;h.get('signalsDetails').listeners.toggle();await tick();
  assert.match(h.get('signalPage').textContent,/101.*страница 1/);
  await h.get('signalNext').listeners.click();
  assert.equal(h.calls.at(-1).p.cursor,'page2');assert.match(h.get('signalPage').textContent,/страница 2/);
  await h.get('signalPrev').listeners.click();assert.equal(h.calls.at(-1).p.cursor,'');
});
test('late strategy request cannot overwrite newer selected strategy',async()=>{
  let resolveLong;
  const h=harness((kind,p,bundle)=>kind==='summary'&&p.stream.startsWith('long')?new Promise(resolve=>{resolveLong=()=>resolve(bundle(p));}):null);
  await tick();h.get('strategy').value='long:hash';const pending=h.get('strategy').listeners.change();
  await tick();h.get('strategy').value='intraday:hash';await h.get('strategy').listeners.change();
  resolveLong();await pending;
  assert.match(h.get('periodTitle').textContent,/INTRADAY/);assert.equal(h.get('strategy').value,'intraday:hash');
  assert.equal(h.cancellations.at(-1),'all');
});
test('filter change resets server pagination and preserves selected cost',async()=>{
  const h=harness();await tick();h.get('cost').value='net';h.get('outcome').value='unknown';
  await h.get('outcome').listeners.change();
  assert.equal(h.calls.at(-1).p.cost,'net');assert.equal(h.calls.at(-1).p.outcome,'unknown');assert.equal(h.calls.at(-1).p.cursor,'');
  assert.equal(h.get('pnlHeading').textContent,'Результат ₽ · net');
  assert.equal(h.cancellations.at(-1),'trades');
});

test('period label updates before a slow response and cancelled view is not an error',async()=>{
  let reject;
  const h=harness((kind,p)=>kind==='summary'&&p.period==='2026-11'?new Promise((_,r)=>{reject=r;}):null);
  await tick();h.get('mode').value='month';const task=h.get('mode').listeners.change();await tick();
  assert.equal(h.get('dateLabel').textContent,'Месяц');
  h.get('mode').value='day';await h.get('mode').listeners.change();
  reject(Object.assign(new Error('replaced'),{name:'AbortError'}));await task;
  assert.equal(h.get('dateLabel').textContent,'Дата');assert.notEqual(h.get('notice').className,'error');
});

test('empty refreshed catalog clears old data and disables detail requests',async()=>{
  const h=harness();await tick();
  h.loader.loadReport=async()=>({schema:'trader-journal-api-v1',generation:'empty',streams:[]});
  await h.get('reload').listeners.click();
  assert.equal(h.get('download').disabled,true);assert.equal(h.get('loadEquity').disabled,true);
  assert.equal(h.get('metrics').children.length,0);
  const count=h.calls.length;await h.get('cost').listeners.change();assert.equal(h.calls.length,count);
});
