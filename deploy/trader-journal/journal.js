'use strict';
const M = JournalModel;
const timeFormatter = new Intl.DateTimeFormat('ru-RU', {timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
let report, shown = [], signalPage = 0;
const $ = id => document.getElementById(id);
const money = v => M.numeric(v) === null ? '—' : Number(v).toLocaleString('ru-RU', {maximumFractionDigits: 2});
const signed = (v, precision=2) => M.numeric(v) === null ? '—' : (Number(v)>0?'+':'') + Number(v).toLocaleString('ru-RU',{maximumFractionDigits:precision});
const color = v => M.numeric(v) === null ? '' : Number(v) > 0 ? 'pos' : Number(v) < 0 ? 'neg' : '';
const time = v => v && Number.isFinite(Date.parse(v)) ? timeFormatter.format(new Date(v)) : '—';
const direction = v => ({BUY:'Покупка',SELL:'Продажа',buy:'Лонг',sell:'Шорт',LONG:'Лонг',SHORT:'Шорт'}[v] || '—');
function element(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function table(id, rows, highlighted = []) {
  const body = $(id); body.replaceChildren();
  if (!rows.length) { const td=element('td','Нет записей за выбранный период','empty'); td.colSpan=12; const tr=element('tr'); tr.append(td); body.append(tr); return; }
  for (const row of rows) {
    const tr=element('tr');
    row.forEach((cell,i) => tr.append(element('td',cell,highlighted.includes(i)&&String(cell).startsWith('+')?'pos':highlighted.includes(i)&&String(cell).startsWith('-')?'neg':'')));
    body.append(tr);
  }
}
function metric(label,value,detail,cls='') {
  const box=element('div',undefined,'metric');
  box.append(element('small',label),element('strong',value,cls),element('small',detail)); $('metrics').append(box);
}
function current() { return report?.streams.find(s => s.id === $('strategy').value); }
function dates() { return current()?.dates || []; }
function overview(period,cost) {
  $('overview').replaceChildren();
  for (const stream of report.streams) {
    const p=stream.periods[period], button=element('button',undefined,'strategy-card'+(stream.id===$('strategy').value?' selected':''));
    button.setAttribute('aria-pressed',String(stream.id===$('strategy').value));
    button.append(element('span',stream.strategy.toUpperCase()+' · '+stream.environment.toUpperCase(),'eyebrow'),element('strong',signed(p?.[cost])+' ₽',color(p?.[cost])),element('small',p?p.closures+' закрытий · счёт '+signed(p.account?.return_pct,4)+'% net':'Нет данных за этот период'));
    button.addEventListener('click',()=>{$('strategy').value=stream.id;resetDetails();draw();}); $('overview').append(button);
  }
}
function calendar(stream,period,cost) {
  const month=period.slice(0,7),cal=$('calendar'); cal.replaceChildren(); $('monthTitle').textContent=month;
  for(const w of ['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС'])cal.append(element('span',w,'weekday'));
  const [y,m]=month.split('-').map(Number),pad=(new Date(Date.UTC(y,m-1,1)).getUTCDay()+6)%7;
  for(let i=0;i<pad;i++)cal.append(element('span'));
  for(let day=1;day<=new Date(Date.UTC(y,m,0)).getUTCDate();day++) {
    const d=month+'-'+String(day).padStart(2,'0'),p=stream.periods[d],b=element('button',String(day),'day'+(d===period?' active':''));
    b.append(element('strong',p?signed(p[cost]):'нет данных',color(p?.[cost]))); b.disabled=!p;
    b.setAttribute('aria-label',d+': '+(p?signed(p[cost])+' рублей':'нет данных'));
    b.addEventListener('click',()=>{$('mode').value='day';$('date').type='date';$('date').value=d;resetDetails();draw();}); cal.append(b);
  }
}
function equity(points,account) {
  const box=$('equity');box.replaceChildren(); const chart=M.equityCoordinates(points);
  if(chart) {
    const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');
    svg.setAttribute('viewBox','0 0 600 170');svg.setAttribute('role','img');svg.setAttribute('aria-label','Капитал счёта по времени снимков');
    const line=document.createElementNS(ns,'polyline');line.setAttribute('points',chart.points.map(p=>p.join(',')).join(' '));line.setAttribute('fill','none');line.setAttribute('stroke','#b5e990');line.setAttribute('stroke-width','2');svg.append(line);box.append(svg);
    const range=element('div',undefined,'range');range.append(element('span','Мин. '+money(chart.min)+' ₽'),element('span','Макс. '+money(chart.max)+' ₽'));box.append(range);
    const times=element('div',undefined,'range');times.append(element('span',time(chart.start)),element('span',time(chart.end)));box.append(times);
  } else box.append(element('p','Недостаточно снимков для графика','empty'));
  $('window').textContent=account?.window?'Окно оценки: '+account.window.map(time).join(' → ')+(account.coverage?' · операции покрыты выгрузкой брокера':' · полнота операций не подтверждена'):'Нет полного окна оценки. Доходность счёта не рассчитана.';
}
function detailTables(stream,period,cost) {
  const all=stream.trades.filter(t=>M.inPeriod(t.closed_at,period)),oldSymbol=$('symbol').value;
  $('symbol').replaceChildren(element('option','Все бумаги'));$('symbol').firstChild.value='';
  for(const symbol of [...new Set(all.map(t=>t.symbol))].sort()){const o=element('option',symbol);o.value=symbol;$('symbol').append(o);}
  if(all.some(t=>t.symbol===oldSymbol))$('symbol').value=oldSymbol;
  shown=M.selectTrades(stream.trades,period,$('symbol').value,$('direction').value,$('outcome').value,cost);
  $('tradeCount').textContent='Показано '+shown.length+' из '+all.length+' закрытий. CSV содержит только отфильтрованные строки.';
  $('pnlHeading').textContent='Результат ₽ · '+cost;$('pctHeading').textContent='% входа · '+cost;
  table('trades',shown.map(t=>[t.symbol,direction(t.direction),time(t.opened_at),time(t.closed_at),money(t.quantity),money(t.entry),money(t.exit),signed(t[cost]),signed(M.tradePercent(t,cost),4)+' %']),[7,8]);
  const ops=stream.operations.filter(o=>M.inPeriod(o.at,period)).sort((a,b)=>b.at.localeCompare(a.at));$('operationCount').textContent='· '+ops.length;
  table('operations',ops.slice(0,100).map(o=>[time(o.at),o.symbol,direction(o.side),money(o.quantity),money(o.gross),money(o.commission)]));
  const signals=stream.signals.filter(o=>M.inPeriod(o.at,period)).sort((a,b)=>b.at.localeCompare(a.at));signalPage=Math.min(signalPage,Math.max(0,Math.ceil(signals.length/100)-1));
  table('signals',signals.slice(signalPage*100,(signalPage+1)*100).map(o=>[time(o.at),o.symbol,direction(o.side),M.status(o.status),(o.reasons||[]).map(M.reason).join('; ')||'—']));
  $('signalPage').textContent='Записей: '+signals.length+' · страница '+(signalPage+1)+' из '+Math.max(1,Math.ceil(signals.length/100));
  $('signalPrev').disabled=signalPage===0;$('signalNext').disabled=(signalPage+1)*100>=signals.length;
}
function draw() {
  const s=current();if(!s)return;const period=$('date').value,cost=$('cost').value;
  if(!/^\d{4}-\d{2}(?:-\d{2})?$/.test(period))return;
  const stats=s.periods[period],a=stats?.account,summary=stats?.summary?.[cost];
  $('dateLabel').textContent=$('mode').value==='month'?'Месяц':'Дата';$('periodTitle').textContent=s.strategy.toUpperCase()+' / '+period;
  overview(period,cost);$('metrics').replaceChildren();
  metric('Закрытия · '+(cost==='gross'?'без комиссий':'после комиссий'),signed(stats?.[cost])+' ₽','Не включает открытые позиции',color(stats?.[cost]));
  metric('Доходность счёта · net',signed(a?.return_pct,4)+' %','Modified Dietz · указанное ниже окно',color(a?.return_pct));
  metric('Прибыльных закрытий',money(summary?.win_rate_pct)+' %',(summary?.wins??'—')+' в плюс / '+(summary?.losses??'—')+' в минус / '+(summary?.flat??'—')+' без результата');
  metric('Profit factor',summary?.profit_factor_state==='no_losses'?'Нет убытков':money(summary?.profit_factor),'Прибыль / модуль убытков · FIFO-части');
  const points=s.equity.filter(p=>M.inPeriod(p.at,period));calendar(s,period,cost);equity(points,a);
  $('monthlyPanel').hidden=$('mode').value!=='month';const days=s.dates.filter(d=>d.startsWith(period.slice(0,7)));
  $('monthlyCoverage').textContent=days.length?'Есть записи за '+days[0]+' — '+days.at(-1)+'. Это не подтверждение полного месяца. Закрытия и доходность счёта используют разные основания расчёта.':'Нет истории за выбранный месяц.';
  table('monthlyRows',days.map(d=>{const p=s.periods[d],m=p.summary?.[cost];return[d,p.closures,(m?.wins??'—')+' / '+(m?.losses??'—')+' / '+(m?.flat??'—'),signed(p[cost]),signed(p.account?.return_pct,4)+' %',p.account?.coverage?'Подтверждено в окне':'Недостаточно данных'];}),[3,4]);
  table('symbols',(stats?.by_symbol||[]).map(p=>[p.symbol,p.closures,p[cost].wins??'—',p[cost].losses??'—',signed(p[cost].pnl),money(p[cost].fees)]),[4]);detailTables(s,period,cost);
  $('positions').replaceChildren();
  for(const [symbol,p] of Object.entries(s.positions))if(Number(p.units)){const row=element('div',undefined,'position');row.append(element('strong',symbol),element('span',money(p.units)+' шт. · оценка '+money(p.value)+' ₽'),element('span',signed(p.unrealized_gross)+' ₽ / '+signed(p.return_pct)+' %',color(p.unrealized_gross)));$('positions').append(row);}
  if(!$('positions').children.length)$('positions').append(element('p','На последнем срезе позиций нет.','empty'));
  $('positions').append(element('p','Текущий срез: '+time(s.observed_at)+' · переоценка до комиссий, не относится к выбранному прошлому периоду.','hint'));
  $('quality').replaceChildren();for(const w of s.warnings)$('quality').append(element('p',w,'neg'));
  if(!s.warnings.length)$('quality').append(element('p','✓ Контроль количеств пройден. Покрытие периода указано отдельно.','pos'));
  const ops=s.operations.filter(o=>M.inPeriod(o.at,period)),missing=ops.filter(o=>M.numeric(o.commission)===null).length,zero=ops.filter(o=>M.numeric(o.commission)===0).length;
  if(missing)$('quality').append(element('p',missing+' операций без известной комиссии: net может быть недоступен.','neg'));
  if(zero)$('quality').append(element('p',zero+' операций с нулевой комиссией в источнике. Учитывайте это при сравнении с PROD.','warn'));
  $('quality').append(element('p','Капитал выбранного периода: '+money(points.at(-1)?.equity)+' ₽ · '+time(points.at(-1)?.at),'hint'));
  if(Date.now()-Date.parse(s.observed_at)>20*60*1000)$('quality').append(element('p','Снимок этого счёта старше 20 минут.','warn'));
  $('updated').textContent='Сформировано '+time(report.generated_at)+' МСК';
}
function resetDetails(){signalPage=0;$('symbol').value='';$('direction').value='';$('outcome').value='';}
async function load(){
  if ($('reload').disabled) return;
  $('reload').disabled=true;
  $('notice').className='';
  try{
    const data=await JournalLoader.loadReport({onStage:stage=>{
      $('notice').textContent=(stage==='request'?'Подключаемся к журналу…':'Получаем данные журнала…')+' Ожидание — не более 25 секунд.';
    }});
    report=data;const previous=$('strategy').value;$('strategy').replaceChildren();
    for(const s of report.streams){const o=element('option',s.strategy.toUpperCase()+' · '+s.environment.toUpperCase()+' · '+s.id.slice(-4));o.value=s.id;$('strategy').append(o);}
    if(report.streams.some(s=>s.id===previous))$('strategy').value=previous;
    if(!$('date').value)$('date').value=M.changePeriod('',$('mode').value,dates());
    const errors=report.import?.errors||[],stale=Date.now()-Date.parse(report.generated_at)>20*60*1000;
    $('notice').className=errors.length||stale?'error':'';
    $('notice').textContent=errors.length?'Есть ошибки импорта: '+errors.length+'. Не используйте неполные итоги.':stale?'Выгрузка устарела: '+time(report.generated_at)+' МСК.':'История обновляется каждые 5 минут · интерфейс только для анализа, без отправки заявок.';
    if(report.demo)$('notice').textContent='ДЕМОНСТРАЦИЯ: синтетические данные для проверки интерфейса, не реальные сделки.';
    if(!report.streams.length){$('notice').textContent='Нет импортированной истории. Демо-сделки не подставляются.';shown=[];for(const id of ['overview','metrics','calendar','equity','positions','quality'])$(id).replaceChildren();for(const id of ['trades','operations','signals','symbols','monthlyRows'])table(id,[]);}else draw();
  }catch(error){$('notice').className='error';$('notice').textContent='Не удалось обновить журнал: '+error.message+'.'+(report?' Показан предыдущий отчёт — он может быть устаревшим.':' Данные пока не загружены.');}
  finally{$('reload').disabled=false;}
}
for(const id of ['strategy','date'])$(id).addEventListener('change',()=>{resetDetails();draw();});
for(const id of ['cost','symbol','direction','outcome'])$(id).addEventListener('change',draw);
$('signalPrev').addEventListener('click',()=>{signalPage--;draw();});$('signalNext').addEventListener('click',()=>{signalPage++;draw();});
$('mode').addEventListener('change',()=>{const value=M.changePeriod($('date').value,$('mode').value,dates());$('date').type=$('mode').value==='month'?'month':'date';$('date').value=value;resetDetails();draw();});
for(const [id,amount] of [['periodPrev',-1],['periodNext',1]])$(id).addEventListener('click',()=>{$('date').value=M.movePeriod($('date').value,$('mode').value,amount);resetDetails();draw();});
$('reload').addEventListener('click',load);$('printReport').addEventListener('click',()=>window.print());
$('download').addEventListener('click',()=>{
  const rows=[['symbol','direction','opened_at','closed_at','quantity','entry','exit','gross','fees','net','gross_return_pct','net_return_pct'],...shown.map(t=>[t.symbol,t.direction,t.opened_at,t.closed_at,t.quantity,t.entry,t.exit,t.gross,t.fees,t.net,t.return_pct,M.tradePercent(t,'net')])];
  const blob=new Blob(['\ufeff'+rows.map(r=>r.map(M.csvCell).join(';')).join('\r\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=element('a');a.href=url;a.download='journal-'+(current()?.strategy||'unknown')+'-'+$('date').value+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
load();
