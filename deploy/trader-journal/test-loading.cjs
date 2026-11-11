'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {loadReport}=require('./journal-loader.js');
const report={schema:'trader-journal-v1',streams:[],generated_at:new Date().toISOString()};
const response=data=>({ok:true,json:async()=>data});

test('successful read uses same-origin auth, no cache and reports both stages',async()=>{
  const stages=[];let signal;
  const result=await loadReport({fetchImpl:async(url,options)=>{
    assert.equal(url,'data.json');assert.equal(options.cache,'no-store');
    assert.equal(options.credentials,'same-origin');signal=options.signal;
    return response(report);
  },onStage:s=>stages.push(s)});
  assert.equal(result,report);assert.deepEqual(stages,['request','body']);assert.equal(signal.aborted,false);
});

for(const phase of ['headers','body'])test('timeout covers stuck '+phase+' and aborts request',async()=>{
  let signal;
  const pending=new Promise(()=>{});
  await assert.rejects(loadReport({timeoutMs:15,fetchImpl:async(_,o)=>{
    signal=o.signal;
    return phase==='headers'?pending:{ok:true,json:()=>pending};
  }}),e=>e.name==='JournalTimeoutError'&&/Обновить/.test(e.message));
  assert.equal(signal.aborted,true);
});

test('late completion after timeout cannot succeed, retry has fresh signal',async()=>{
  let finish,oldSignal;
  const body=new Promise(resolve=>{finish=resolve;});
  await assert.rejects(loadReport({timeoutMs:10,fetchImpl:async(_,o)=>{
    oldSignal=o.signal;return {ok:true,json:()=>body};
  }}),{name:'JournalTimeoutError'});
  finish(report);
  assert.equal(await loadReport({fetchImpl:async(_,o)=>{
    assert.notEqual(o.signal,oldSignal);assert.equal(o.signal.aborted,false);return response(report);
  }}),report);
});

test('HTTP, broken JSON, missing schema and network failures are not empty success',async()=>{
  for(const status of [401,403,503])await assert.rejects(loadReport({fetchImpl:async()=>({ok:false,status})}),status===401?/Вход истёк/:new RegExp('HTTP '+status));
  await assert.rejects(loadReport({fetchImpl:async()=>({ok:true,json:async()=>{throw new SyntaxError('truncated JSON');}})}),SyntaxError);
  for(const invalid of [null,{},[],{schema:'trader-journal-v1',streams:null}])await assert.rejects(loadReport({fetchImpl:async()=>response(invalid)}),/формата/);
  await assert.rejects(loadReport({fetchImpl:async()=>{throw new TypeError('Failed to fetch');}}),/Failed to fetch/);
});

test('UI leaves loading state after timeout and allows explicit retry',async()=>{
  const vm=require('node:vm'),fs=require('node:fs');
  class Element {
    constructor(){this.children=[];this.value='';this.disabled=false;this.listeners={};}
    append(...nodes){this.children.push(...nodes);}
    replaceChildren(...nodes){this.children=[...nodes];}
    addEventListener(name,fn){this.listeners[name]=fn;}
  }
  const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  get('mode').value='day';let fail=true;
  const context={JournalModel:require('./journal-model.js'),JournalLoader:{loadReport:options=>loadReport({...options,timeoutMs:10,fetchImpl:async()=>fail?new Promise(()=>{}):response(report)})},document:{getElementById:get,createElement:()=>new Element()},setTimeout,clearTimeout};
  vm.createContext(context);vm.runInContext(fs.readFileSync(require.resolve('./journal.js'),'utf8'),context);
  assert.equal(get('reload').disabled,true);assert.match(get('notice').textContent,/Подключаемся/);
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal(get('reload').disabled,false);assert.match(get('notice').textContent,/Данные пока не загружены/);
  fail=false;await get('reload').listeners.click();
  assert.equal(get('reload').disabled,false);assert.match(get('notice').textContent,/Нет импортированной истории/);
  fail=true;await get('reload').listeners.click();
  assert.match(get('notice').textContent,/предыдущий отчёт/);
});

test('installed HTML loads loader before UI and installer includes it',()=>{
  const fs=require('node:fs'),path=require('node:path');
  const html=fs.readFileSync(path.join(__dirname,'index.html'),'utf8');
  assert.ok(html.indexOf('src="journal-loader.js"')<html.indexOf('src="journal.js"'));
  assert.match(fs.readFileSync(path.join(__dirname,'install.sh'),'utf8'),/journal-loader\.js/);
});
