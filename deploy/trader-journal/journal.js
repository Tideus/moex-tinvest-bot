'use strict';
let report, shown = [], signalPage = 0;
const $ = id => document.getElementById(id);
const money = v => v === null || v === undefined ? '—' : Number(v).toLocaleString('ru-RU',{maximumFractionDigits:2});
const localDay = at => new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(at));
const time = at => at ? new Date(at).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—';
function element(tag,text,cls){const e=document.createElement(tag); if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function signed(v){return v===null||v===undefined?'—':(Number(v)>0?'+':'')+money(v);}
function color(v){return Number(v)>0?'pos':Number(v)<0?'neg':'';}
function table(id, rows){const body=$(id);body.replaceChildren();if(!rows.length){const td=element('td','Нет записей за выбранный период');td.colSpan=12;body.append(element('tr'));body.firstChild.append(td);return;}for(const row of rows){const tr=element('tr');for(const cell of row)tr.append(element('td',cell));body.append(tr);}}
function metric(label,value,detail,cls=''){const box=element('div',undefined,'metric');box.append(element('small',label),element('strong',value,cls),element('small',detail));$('metrics').append(box);}
function draw(){
 const s=report.streams.find(x=>x.id===$('strategy').value);if(!s)return;
 const period=$('date').value, month=period.slice(0,7), cost=$('cost').value;
 const stats=s.periods[period], a=stats?.account;
 const filter=at=>localDay(at).startsWith(period);
 $('metrics').replaceChildren();
 metric('Закрытия · '+(cost==='gross'?'без комиссий':'после комиссий'),signed(stats?.[cost])+' ₽','Не включает переоценку открытых позиций',color(stats?.[cost]));
 metric('Доходность счёта · net',signed(a?.return_pct)+' %','Modified Dietz · только указанное окно',color(a?.return_pct));
 metric('Частичных закрытий',stats?.closures??'—','Положительных до комиссий: '+(stats?.wins??'—'));
 const points=s.equity.filter(x=>filter(x.at));
 metric('Капитал на последний срез',money(points.at(-1)?.equity)+' ₽',time(points.at(-1)?.at));
 $('monthTitle').textContent=month;
 const cal=$('calendar');cal.replaceChildren();for(const w of ['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС'])cal.append(element('span',w,'weekday'));
 const [y,m]=month.split('-').map(Number);const pad=(new Date(Date.UTC(y,m-1,1)).getUTCDay()+6)%7;
 for(let i=0;i<pad;i++)cal.append(element('span'));
 for(let day=1;day<=new Date(Date.UTC(y,m,0)).getUTCDate();day++){const d=month+'-'+String(day).padStart(2,'0');const p=s.periods[d];const b=element('button',String(day),'day'+(d===period?' active':''));b.append(element('strong',p?signed(p[cost]):'нет данных',color(p?.[cost])));b.disabled=!p;b.addEventListener('click',()=>{$('mode').value='day';$('date').type='date';$('date').value=d;draw();});cal.append(b);}
 $('equity').replaceChildren();
 if(points.length>1){const vals=points.map(p=>Number(p.equity)),lo=Math.min(...vals),hi=Math.max(...vals);const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 600 170');svg.setAttribute('role','img');svg.setAttribute('aria-label','Капитал счёта по фактическим снимкам');const line=document.createElementNS(svg.namespaceURI,'polyline');line.setAttribute('points',vals.map((v,i)=>`${10+i*580/(vals.length-1)},${150-(v-lo)/(hi-lo||1)*130}`).join(' '));line.setAttribute('fill','none');line.setAttribute('stroke','#b5e990');line.setAttribute('stroke-width','2');svg.append(line);$('equity').append(svg);const r=element('div',undefined,'range');r.append(element('span',money(lo)+' ₽'),element('span',money(hi)+' ₽'));$('equity').append(r);}else $('equity').append(element('p','Недостаточно снимков для графика','hint'));
 $('window').textContent=a?.window?'Окно оценки: '+a.window.map(time).join(' → ')+(a.coverage?' · операции сверены с выгрузкой брокера':' · полнота операций не подтверждена'):'Нет полного окна оценки. Доходность счёта не рассчитана.';
 shown=s.trades.filter(t=>filter(t.closed_at));
 table('trades',shown.map(t=>[t.symbol,t.direction,time(t.opened_at),time(t.closed_at),money(t.quantity),money(t.entry),money(t.exit),signed(t[cost]),signed(t.return_pct)+' %']));
 table('operations',s.operations.filter(o=>filter(o.at)).map(o=>[time(o.at),o.symbol,o.side,money(o.quantity),money(o.gross),money(o.commission)]));
 const signals=s.signals.filter(o=>filter(o.at)).sort((a,b)=>b.at.localeCompare(a.at));
 signalPage=Math.min(signalPage,Math.max(0,Math.ceil(signals.length/100)-1));
 table('signals',signals.slice(signalPage*100,(signalPage+1)*100).map(o=>[time(o.at),o.symbol,o.side??'—',o.status,(o.reasons??[]).join(', ')]));
 $('signalPage').textContent=`Записей: ${signals.length} · страница ${signalPage+1} из ${Math.max(1,Math.ceil(signals.length/100))}`;
 $('signalPrev').disabled=signalPage===0;$('signalNext').disabled=(signalPage+1)*100>=signals.length;
 $('positions').replaceChildren();for(const [symbol,p] of Object.entries(s.positions))if(Number(p.units))$('positions').append(element('p',symbol+' · '+money(p.units)+' шт. · оценка '+money(p.value)+' ₽ · переоценка '+signed(p.unrealized_gross)+' ₽ / '+signed(p.return_pct)+' %',color(p.unrealized_gross)));if(!$('positions').children.length)$('positions').append(element('p','На последнем срезе позиций нет.'));$('positions').append(element('p','Текущий срез, не исторический: '+time(s.observed_at)+' · переоценка до комиссий','hint'));
 $('quality').replaceChildren();for(const w of s.warnings)$('quality').append(element('p',w,'neg'));if(!s.warnings.length)$('quality').append(element('p','Контроль количеств пройден. Полнота периода указана отдельно.'));$('quality').append(element('p','Контур: '+s.environment.toUpperCase()+' · Стратегия: '+s.strategy.toUpperCase()));
 $('updated').textContent='Сформировано '+time(report.generated_at)+' МСК';
}
async function load(){try{const r=await fetch('data.json',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);const data=await r.json();if(data.schema!=='trader-journal-v1')throw Error('Версия формата не поддерживается');report=data;const previous=$('strategy').value;$('strategy').replaceChildren();for(const s of report.streams){const o=element('option',s.strategy.toUpperCase()+' · '+s.environment.toUpperCase()+' · '+s.id.slice(-4));o.value=s.id;$('strategy').append(o);}if(report.streams.some(s=>s.id===previous))$('strategy').value=previous;
 if(!$('date').value){const days=report.streams.flatMap(s=>s.dates).sort();$('date').value=days.at(-1)??localDay(new Date());}
 const errors=report.import?.errors??[];const stale=Date.now()-Date.parse(report.generated_at)>20*60*1000;
 $('notice').className=errors.length||stale?'error':'';$('notice').textContent=errors.length?'Есть ошибки импорта: '+errors.length+'. Не используйте неполные итоги.':stale?'Данные устарели: последнее обновление '+time(report.generated_at)+' МСК.':'Данные загружены · заявки из интерфейса отправить невозможно.';
 if(!report.streams.length)$('notice').textContent='Нет импортированной истории. Демо-сделки не подставляются.';draw();}catch(e){$('notice').className='error';$('notice').textContent='Не удалось обновить журнал: '+e.message+'. Ранее загруженные данные могут быть устаревшими.';}}
for(const id of ['strategy','date','cost'])$(id).addEventListener('change',draw);
$('signalPrev').addEventListener('click',()=>{signalPage--;draw();});
$('signalNext').addEventListener('click',()=>{signalPage++;draw();});
$('mode').addEventListener('change',()=>{const v=$('date').value;const monthly=$('mode').value==='month';$('date').type=monthly?'month':'date';$('date').value=monthly?v.slice(0,7):(v.length===7?v+'-01':v);draw();});
$('reload').addEventListener('click',load);
$('download').addEventListener('click',()=>{const rows=[['symbol','direction','opened_at','closed_at','quantity','entry','exit','gross','fees','net','return_pct'],...shown.map(t=>[t.symbol,t.direction,t.opened_at,t.closed_at,t.quantity,t.entry,t.exit,t.gross,t.fees,t.net,t.return_pct])];const escape=v=>'"'+String(v??'').replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';const blob=new Blob(['\ufeff'+rows.map(r=>r.map(escape).join(';')).join('\r\n')],{type:'text/csv;charset=utf-8'});const url=URL.createObjectURL(blob);const a=element('a');a.href=url;a.download='journal-'+$('date').value+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
load();
