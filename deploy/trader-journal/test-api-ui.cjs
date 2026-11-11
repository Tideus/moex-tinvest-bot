'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.listeners={};this.disabled=false;this.textContent='';this.attributes={};}
  append(...nodes){this.children.push(...nodes);if(this.tag==='select'&&!this.value&&this.children.length)this.value=this.children[0].value;}
  replaceChildren(...nodes){this.children=[];if(this.tag==='select')this.value='';this.append(...nodes);}
  get firstChild(){return this.children[0];}
  setAttribute(name,value){this.attributes[name]=value;}
  getAttribute(name){return this.attributes[name];}
  focus(){this.ownerDocument.activeElement=this;}
  scrollIntoView(){this.scrolled=true;}
  click(){return this.listeners.click?.();}
  addEventListener(event,fn){this.listeners[event]=fn;}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function harness(extra){
  const nodes=new Map(),calls=[],cancellations=[],blobs=[],document={activeElement:null};let catalogLoads=0;
  const create=tag=>Object.assign(new Element(tag),{ownerDocument:document});
  const get=id=>{if(!nodes.has(id))nodes.set(id,create(['strategy','mode','cost','symbol','direction','outcome','operationSymbol'].includes(id)?'select':'div'));return nodes.get(id);};
  get('mode').value='day';get('cost').value='gross';
  const metadata={schema:'trader-journal-api-v1',generation:'abc',generated_at:new Date().toISOString(),import:{errors:[]}};
  const streams=['intraday','long'].map(strategy=>({id:strategy+':hash',strategy,environment:'sandbox',dates:['2026-11-11']}));
  const stats={gross:'1',net:'0.9',closures:1,account:{},summary:{},by_symbol:[]};
  const bundle=p=>({...metadata,stream:{...streams.find(s=>s.id===p.stream),periods:{'2026-11':stats,'2026-11-11':stats},warnings:[],positions:{},observed_at:metadata.generated_at},overview:streams.map(s=>({...s,stats})),operation_quality:{missing:0,zero:0},operation_symbols:['SBER','YDEX']});
  const loader={cancelPending:kind=>cancellations.push(kind||'all'),loadReport:async()=>{catalogLoads++;return {...metadata,streams:streams.map(s=>({...s}))};},loadJSON:async(kind,p)=>{
    calls.push({kind,p:{...p}});if(extra){const override=extra(kind,p,bundle);if(override)return override;}
    return kind==='summary'?bundle(p):{...metadata,items:kind==='signals'?[{at:'2026-11-11T08:00Z',symbol:p.cursor?'SECOND':'FIRST',reasons:[],side:'buy'}]:kind==='operations'?[{at:'2026-11-11T08:00Z',symbol:p.symbol||'SBER',side:'BUY',quantity:'2',gross:'-200',price:'100',price_source:'broker',commission:'0'}]:[],total:kind==='signals'?101:kind==='operations'?1:0,period_total:kind==='operations'?2:0,next_cursor:kind==='signals'&&!p.cursor?'page2':null};
  }};
  Object.assign(document,{getElementById:get,createElement:create,createElementNS:(_,tag)=>create(tag)});
  vm.runInNewContext(fs.readFileSync(require.resolve('./journal.js'),'utf8'),{JournalModel:require('./journal-model.js'),JournalLoader:loader,document,setTimeout,clearTimeout,Blob,URL:{createObjectURL:blob=>{blobs.push(blob);return 'blob:test';},revokeObjectURL(){}}});
  return {get,calls,loader,cancellations,document,metadata,blobs,get catalogLoads(){return catalogLoads;}};
}
test('initial screen fetches operations visibly; signals are lazy and paginated',async()=>{
  const h=harness();await tick();
  assert.deepEqual(h.calls.map(x=>x.kind),['summary','trades','operations']);
  assert.equal(h.get('reload').disabled,false);assert.match(h.get('periodTitle').textContent,/INTRADAY/);
  h.get('signalsDetails').open=true;h.get('signalsDetails').listeners.toggle();await tick();
  assert.match(h.get('signalPage').textContent,/101.*страница 1/);
  await h.get('signalNext').listeners.click();
  assert.equal(h.calls.at(-1).p.cursor,'page2');assert.match(h.get('signalPage').textContent,/страница 2/);
  await h.get('signalPrev').listeners.click();assert.equal(h.calls.at(-1).p.cursor,'');
});

test('filters changed during slow summary are used by the first page',async()=>{
  let release;
  const h=harness((kind,p,bundle)=>kind==='summary'?new Promise(resolve=>{release=()=>resolve(bundle(p));}):null);
  await tick();assert.equal(h.get('direction').disabled,true);assert.equal(h.get('operationSymbol').disabled,true);
  h.get('direction').value='SHORT';h.get('outcome').value='loss';await h.get('outcome').listeners.change();
  assert.equal(h.calls.filter(c=>c.kind==='trades').length,0);
  release();await tick();
  const request=h.calls.find(c=>c.kind==='trades');assert.equal(request.p.direction,'SHORT');assert.equal(request.p.outcome,'loss');
  assert.equal(h.get('direction').disabled,false);
});

test('operation filter includes securities without closures and is server-side',async()=>{
  const h=harness();await tick();
  assert.deepEqual(h.get('symbol').children.map(x=>x.value),['']);
  assert.deepEqual(h.get('operationSymbol').children.map(x=>x.value),['','SBER','YDEX']);
  h.get('operationSymbol').value='YDEX';await h.get('operationSymbol').listeners.change();
  assert.equal(h.calls.at(-1).kind,'operations');assert.equal(h.calls.at(-1).p.symbol,'YDEX');assert.equal(h.calls.at(-1).p.cursor,'');
  assert.equal(h.get('operations').children[0].children[0].textContent,'YDEX');
  assert.match(h.get('operationCount').textContent,/по бумаге 1.*за период 2/);
  assert.equal(h.get('operationCards').children.length,1);
});

test('loading, error, retry and empty operations never claim an undefined count',async()=>{
  let fail=true,reject;
  const h=harness((kind)=>kind==='operations'&&fail?new Promise((_,r)=>{reject=r;}):null);
  await tick();assert.match(h.get('operationStatus').textContent,/Загружаем/);assert.doesNotMatch(h.get('operationCount').textContent,/undefined/);
  assert.doesNotMatch(h.get('operations').children[0].children[0].textContent,/Нет записей/);
  reject(new Error('Temporary network failure'));await tick();
  assert.match(h.get('operationStatus').textContent,/Не удалось/);assert.equal(h.get('operationRetry').hidden,false);
  fail=false;await h.get('operationRetry').listeners.click();
  assert.equal(h.get('operationStatus').textContent,'');assert.equal(h.get('operationRetry').hidden,true);assert.notEqual(h.get('notice').className,'error');
  h.loader.loadJSON=async()=>({...h.metadata,items:[],total:0,period_total:2,next_cursor:null});
  await h.get('operationSymbol').listeners.change();assert.match(h.get('operationStatus').textContent,/Операций по этому фильтру.*нет/);
  assert.match(h.get('operationCount').textContent,/по бумаге 0/);
});

test('success in one section cannot erase another section error',async()=>{
  const h=harness(kind=>kind==='signals'?Promise.reject(new Error('signal unavailable')):null);await tick();
  h.get('signalsDetails').open=true;h.get('signalsDetails').listeners.toggle();await tick();
  await h.get('operationSymbol').listeners.change();assert.match(h.get('notice').textContent,/signal unavailable/);
});

test('calendar exposes selection, focuses operations and preserves keyboard focus on redraw',async()=>{
  const h=harness();await tick();
  let day=h.get('calendar').children.find(node=>node.attributes['data-calendar-day']==='2026-11-11');
  assert.equal(day.attributes['aria-current'],'date');assert.equal(day.attributes['aria-pressed'],'true');
  await day.listeners.click();assert.equal(h.document.activeElement,h.get('operationsTitle'));assert.equal(h.get('operationsPanel').scrolled,true);
  day=h.get('calendar').children.find(node=>node.attributes['data-calendar-day']==='2026-11-11');day.focus();
  await h.get('cost').listeners.change();assert.equal(h.document.activeElement.attributes['data-calendar-day'],'2026-11-11');
});

const snapshotError=()=>Object.assign(new Error('Snapshot changed'),{name:'JournalSnapshotChangedError'});
test('409 reloads complete snapshot once, preserves filters and resets cursors',async()=>{
  let conflict=false;
  const h=harness((kind,p)=>conflict&&p.generation==='abc'?Promise.reject(snapshotError()):null);await tick();
  h.get('direction').value='SHORT';h.get('outcome').value='loss';h.get('operationSymbol').value='YDEX';
  conflict=true;h.metadata.generation='next';
  await h.get('operationSymbol').listeners.change();
  assert.equal(h.catalogLoads,2);assert.equal(h.get('direction').value,'SHORT');assert.equal(h.get('operationSymbol').value,'YDEX');
  const recovered=h.calls.filter(c=>c.p.generation==='next');
  assert.deepEqual(recovered.map(c=>c.kind),['summary','trades','operations']);
  assert.equal(recovered.find(c=>c.kind==='trades').p.outcome,'loss');
  assert.equal(recovered.find(c=>c.kind==='operations').p.cursor,'');assert.notEqual(h.get('notice').className,'error');
});

test('continuous 409 stops after one catalog restart without a retry loop',async()=>{
  const h=harness(kind=>kind==='summary'?Promise.reject(snapshotError()):null);await tick();await tick();
  assert.equal(h.catalogLoads,2);assert.equal(h.calls.length,2);assert.equal(h.get('reload').disabled,false);assert.equal(h.get('notice').className,'error');
  assert.match(h.get('operationStatus').textContent,/Не удалось/);
});
test('simultaneous conflicts share one restart and late old pages cannot overwrite it',async()=>{
  let conflict=false,releaseOld;
  const h=harness((kind,p)=>{
    if(conflict&&p.generation==='abc')return kind==='operations'?new Promise(resolve=>{releaseOld=()=>resolve({schema:'trader-journal-api-v1',generation:'abc',items:[{symbol:'OLD',side:'BUY'}],total:1,period_total:1});}):Promise.reject(snapshotError());
    return null;
  });await tick();
  conflict=true;h.metadata.generation='next';
  const obsolete=h.get('operationSymbol').listeners.change();const restart=h.get('cost').listeners.change();
  await restart;releaseOld();await obsolete;
  assert.equal(h.catalogLoads,2);assert.equal(h.get('operations').children[0].children[0].textContent,'SBER');
  assert.notEqual(h.get('notice').className,'error');
});

test('operations pagination retries the failed next page and resets on symbol change',async()=>{
  let failNext=true;
  const h=harness((kind,p)=>{
    if(kind!=='operations')return null;
    if(p.cursor&&failNext)return Promise.reject(new Error('next page failed'));
    return Promise.resolve({schema:'trader-journal-api-v1',generation:'abc',items:[{symbol:p.cursor?'SECOND':p.symbol||'FIRST',side:'SELL'}],total:101,period_total:101,next_cursor:p.cursor?null:'next'});
  });await tick();
  await h.get('operationNext').listeners.click();assert.match(h.get('operationStatus').textContent,/предыдущая страница/);
  failNext=false;await h.get('operationRetry').listeners.click();assert.equal(h.calls.at(-1).p.cursor,'next');assert.match(h.get('operationPage').textContent,/страница 2/);
  h.get('operationSymbol').value='YDEX';await h.get('operationSymbol').listeners.change();assert.equal(h.calls.at(-1).p.cursor,'');assert.match(h.get('operationPage').textContent,/страница 1/);
});

test('fresh report does not conceal a stale broker snapshot',async()=>{
  const h=harness((kind,p,bundle)=>{
    if(kind!=='summary')return null;
    const result=bundle(p);result.stream.observed_at='2020-01-01T00:00Z';return Promise.resolve(result);
  });await tick();assert.equal(h.get('notice').className,'error');assert.match(h.get('notice').textContent,/Свежесть снимка счёта не подтверждена/);
});

test('transport failures are not hidden by automatic retries',async()=>{
  const h=harness(kind=>kind==='summary'?Promise.reject(new Error('Network timeout')):null);await tick();
  assert.equal(h.catalogLoads,1);assert.equal(h.calls.length,1);assert.match(h.get('notice').textContent,/Network timeout/);
});

test('CSV preserves negative numeric cells while protecting free text',async()=>{
  const trade={symbol:'=HYPERLINK("bad")',direction:'LONG',opened_at:'2026-11-11T07:00Z',closed_at:'2026-11-11T08:00Z',quantity:'1',entry:'100',exit:'98',gross:'-2',fees:'1',net:'-3',return_pct:'-2',net_return_pct:'-3'};
  const h=harness(kind=>kind==='trades'?Promise.resolve({schema:'trader-journal-api-v1',generation:'abc',items:[trade],total:1,period_total:1,next_cursor:null}):null);await tick();
  await h.get('download').listeners.click();const csv=await h.blobs[0].text();
  assert.match(csv,/;"-2";"1";"-3";"-2";"-3"/);assert.doesNotMatch(csv,/"'-[23]"/);assert.match(csv,/"'=HYPERLINK/);
});

test('operations are outside collapsed details directly after calendar in HTML',()=>{
  const html=fs.readFileSync(require.resolve('./index.html'),'utf8');
  assert.ok(html.indexOf('id="calendar"')<html.indexOf('id="operationsPanel"'));
  assert.ok(html.indexOf('id="operationsPanel"')<html.indexOf('id="equity"'));
  assert.doesNotMatch(html,/<details[^>]*id="operations/);assert.match(html,/id="operationsTitle" tabindex="-1"/);
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
