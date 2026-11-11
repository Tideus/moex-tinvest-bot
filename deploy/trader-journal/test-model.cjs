'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const M = require('./journal-model.js');
test('gross/net percentages do not silently mix and missing fee stays unknown', () => {
  const t={entry:'100',quantity:'10',return_pct:'1',net:'-2'};
  assert.equal(M.tradePercent(t,'gross'),1);assert.equal(M.tradePercent(t,'net'),-0.2);
  assert.equal(M.tradePercent({...t,net_return_pct:null},'net'),null);
  assert.equal(M.tradePercent({...t,net:null},'net'),null);
  assert.equal(M.tradePercent({...t,entry:'0'},'net'),null);
});
test('Moscow day and month cutoffs are independent of local timezone', () => {
  assert.equal(M.localDay('2026-10-31T21:30:00Z'),'2026-11-01');
  assert.ok(M.inPeriod('2026-10-31T21:30:00Z','2026-11'));
  assert.equal(M.inPeriod('2026-11-01T00:00Z',''),false);
  assert.equal(M.localDay('invalid'),'');
});
test('filters classify unknown and flat separately, using selected costs', () => {
  const t={symbol:'T',direction:'LONG',closed_at:'2026-11-10T12:00Z',gross:'3',net:'-1'};
  const rows=[t,{...t,symbol:'YDEX',net:null},{...t,symbol:'SBER',net:'0'}];
  assert.equal(M.selectTrades(rows,'2026-11','','','loss','net').length,1);
  assert.equal(M.selectTrades(rows,'2026-11','','','unknown','net')[0].symbol,'YDEX');
  assert.equal(M.selectTrades(rows,'2026-11','','','flat','net')[0].symbol,'SBER');
  assert.equal(M.selectTrades(rows,'2026-11','T','SHORT','','gross').length,0);
});
test('period switches pick observed day and navigation crosses years/leap days', () => {
  assert.equal(M.changePeriod('2026-11','day',['2026-11-02','2026-11-10']),'2026-11-10');
  assert.equal(M.changePeriod('2026-11-10','month',[]),'2026-11');
  assert.equal(M.movePeriod('2026-12','month',1),'2027-01');
  assert.equal(M.movePeriod('2024-03-01','day',-1),'2024-02-29');
});
test('equity chart uses elapsed time rather than equally spaced snapshots', () => {
  const chart=M.equityCoordinates([{at:'2026-11-10T00:00Z',equity:'100'},{at:'2026-11-10T01:00Z',equity:'100'},{at:'2026-11-10T10:00Z',equity:'100'}]);
  assert.equal(chart.points[1][0],68);assert.equal(chart.points[1][1],85);
  assert.equal(M.equityCoordinates([{at:'bad',equity:'NaN'}]),null);
});
test('presentation preserves unknown reason, CSV quotes and formula guards', () => {
  assert.match(M.reason('daily_entry_limit'),/дневной лимит/);
  assert.equal(M.reason('new_reason'),'Причина: new_reason');
  assert.equal(M.status('planned'),'Включён в план');
  assert.match(M.csvCell(' =1+1'),/^"'/);assert.equal(M.csvCell('a"b'),'"a""b"');
  for(const x of [null,undefined,'NaN','Infinity','',false,'   ',[],{}])assert.equal(M.numeric(x),null);
  assert.equal(M.numeric('0'),0);
});

test('large history constructs one date formatter, not one per signal', () => {
  const vm=require('node:vm'), fs=require('node:fs');
  let constructions=0;
  const context={module:{exports:{}},Intl:{DateTimeFormat:function(...args){constructions++;return new Intl.DateTimeFormat(...args);}}};
  vm.runInNewContext(fs.readFileSync(require.resolve('./journal-model.js'),'utf8'),context);
  const model=context.module.exports;
  const rows=Array.from({length:20000},(_,i)=>({at:new Date(Date.UTC(2026,10,11,0,0,i)).toISOString()}));
  assert.equal(rows.filter(r=>model.inPeriod(r.at,'2026-11-11')).length,20000);
  assert.equal(rows.filter(r=>model.inPeriod(r.at,'2026-11')).length,20000);
  assert.equal(constructions,1);
});
