'use strict';
const M = JournalModel;
const timeFormatter = new Intl.DateTimeFormat('ru-RU', {timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
let report, shown = [], pages = {}, viewEpoch = 0, operationQuality = {}, operationSymbols = [], equityLoaded = false, viewReady = false, requestErrors = {}, catalogLoadId = 0;
const sections=[['trades','trade'],['signals','signal'],['operations','operation']];
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
function table(id, rows, highlighted = [], emptyMessage='Нет записей за выбранный период') {
  const body = $(id); body.replaceChildren();
  if (!rows.length) { const td=element('td',emptyMessage,'empty'); td.colSpan=12; const tr=element('tr'); tr.append(td); body.append(tr); return; }
  for (const row of rows) {
    const tr=element('tr');
    row.forEach((cell,i) => {const td=element('td',typeof cell==='object'?undefined:cell,highlighted.includes(i)&&String(cell).startsWith('+')?'pos':highlighted.includes(i)&&String(cell).startsWith('-')?'neg':'');if(typeof cell==='object')td.append(cell);tr.append(td);});
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
    const p=stream.periods?.[period], button=element('button',undefined,'strategy-card'+(stream.id===$('strategy').value?' selected':''));
    button.setAttribute('aria-pressed',String(stream.id===$('strategy').value));
    button.append(element('span',stream.strategy.toUpperCase()+' · '+stream.environment.toUpperCase(),'eyebrow'),element('strong',signed(p?.[cost])+' ₽',color(p?.[cost])),element('small',p?p.closures+' закрытий · счёт '+signed(p.account?.return_pct,4)+'% net':'Нет данных за этот период'));
    button.addEventListener('click',()=>{$('strategy').value=stream.id;refreshView();}); $('overview').append(button);
  }
}
function calendar(stream,period,cost) {
  const focusedDay=document.activeElement?.getAttribute?.('data-calendar-day');
  const month=period.slice(0,7),cal=$('calendar'); cal.replaceChildren(); $('monthTitle').textContent=month;
  for(const w of ['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС'])cal.append(element('span',w,'weekday'));
  const [y,m]=month.split('-').map(Number),pad=(new Date(Date.UTC(y,m-1,1)).getUTCDay()+6)%7;
  for(let i=0;i<pad;i++)cal.append(element('span'));
  for(let day=1;day<=new Date(Date.UTC(y,m,0)).getUTCDate();day++) {
    const d=month+'-'+String(day).padStart(2,'0'),p=stream.periods[d],b=element('button',String(day),'day'+(d===period?' active':''));
    b.append(element('strong',p?signed(p[cost]):'нет данных',color(p?.[cost]))); b.disabled=!p;
    b.setAttribute('aria-label',d+': '+(p?signed(p[cost])+' рублей':'нет данных'));
    b.setAttribute('data-calendar-day',d);b.setAttribute('aria-pressed',String(d===period));
    if(d===period)b.setAttribute('aria-current','date');
    b.addEventListener('click',()=>{$('mode').value='day';$('date').type='date';$('date').value=d;return refreshView({focusOperations:true});}); cal.append(b);
    if(d===focusedDay)b.focus?.({preventScroll:true});
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
function symbolOptions(id,symbols) {
  const select=$(id),previous=select.value;
  select.replaceChildren(element('option','Все бумаги'));select.firstChild.value='';
  for(const symbol of symbols){const o=element('option',symbol);o.value=symbol;select.append(o);}
  if(symbols.includes(previous))select.value=previous;
}
function sectionState(kind) {
  const page=pages[kind];
  if(page?.busy)return page.items?'Загружаем страницу… Пока показана предыдущая.':'Загружаем записи…';
  if(page?.error)return 'Не удалось загрузить записи. '+(page.items?'Показана предыдущая страница. ':'')+'Повторите запрос или обновите журнал.';
  return page?.items?'':'Данные ещё не загружены.';
}
function operationPriceText(operation) {
  const value=M.operationPrice(operation);
  return value===null?'—':value.toLocaleString('ru-RU',{maximumFractionDigits:6})+(operation.price_source==='broker'?'':' ≈');
}
function operationFeeText(operation,unit='') {
  return money(operation.commission)+unit+(operation.commission_source==='legacy_unverified'?' · не подтверждена':'');
}
function operationCards(operations) {
  const box=$('operationCards');box.replaceChildren();
  for(const operation of operations){
    const card=element('article',undefined,'operation-card'),header=element('div',undefined,'operation-card-head');
    header.append(paperButton(operation.symbol,{kind:'operation',at:operation.at,side:operation.side}),element('span',direction(operation.side)+' · '+operation.side,'operation-side'));
    card.append(header,element('p',time(operation.at)+' МСК','hint'));
    const values=element('dl');
    for(const [label,value] of [['Количество',money(operation.quantity)+' шт.'],['Цена',operationPriceText(operation)+' ₽'],['Сумма',money(operation.gross)+' ₽'],['Комиссия',operationFeeText(operation,' ₽')]])values.append(element('dt',label),element('dd',value));
    card.append(values);box.append(card);
  }
}
function detailTables(stream,period,cost) {
  symbolOptions('symbol',(stream.periods[period]?.by_symbol||[]).map(t=>t.symbol));
  symbolOptions('operationSymbol',operationSymbols);
  shown=pages.trades?.items||[];
  $('tradeCount').textContent=pages.trades?.items?'На странице '+shown.length+' · по фильтру '+pages.trades.total+' · за период '+pages.trades.period_total+'. CSV выгружает все строки по фильтру.':'';
  $('pnlHeading').textContent='Результат ₽ · '+cost;$('pctHeading').textContent='% входа · '+cost;
  table('trades',shown.map(t=>[paperButton(t.symbol,{kind:'closed',at:t.closed_at,opened_at:t.opened_at,quantity:t.quantity,entry:t.entry,exit:t.exit}),direction(t.direction),time(t.opened_at),time(t.closed_at),money(t.quantity),money(t.entry),money(t.exit),signed(t[cost]),signed(M.tradePercent(t,cost),4)+' %']),[7,8],sectionState('trades')||'Закрытий по этому фильтру нет. Покупки и продажи смотрите в «Операциях» выше.');
  const ops=pages.operations?.items||[];
  $('operationsTitle').textContent=($('mode').value==='month'?'Операции месяца · ':'Операции дня · ')+period;
  $('operationCount').textContent=pages.operations?.items?'На странице '+ops.length+' · по бумаге '+pages.operations.total+' · за период '+pages.operations.period_total:'';
  table('operations',ops.map(o=>[paperButton(o.symbol,{kind:'operation',at:o.at,side:o.side}),direction(o.side)+' · '+o.side,time(o.at),money(o.quantity),operationPriceText(o),money(o.gross),operationFeeText(o)]),[],sectionState('operations')||'Операций по этому фильтру за выбранный период нет.');
  operationCards(ops);
  const signals=pages.signals?.items||[];
  table('signals',signals.map(o=>[time(o.at),o.symbol,direction(o.side),M.status(o.status),(o.reasons||[]).map(M.reason).join('; ')||'—']),[],sectionState('signals')||'Сигналов за выбранный период нет.');
  for(const [kind,prefix] of sections){
    const page=pages[kind];
    $(prefix+'Status').textContent=sectionState(kind)||(!page?.items?.length?(kind==='operations'?'Операций по этому фильтру за выбранный период нет.':kind==='trades'?'Закрытий нет. Покупки и продажи смотрите выше.':'Сигналов за выбранный период нет.'):'');
    $(prefix+'Status').className='section-status'+(page?.error?' error':'');
    $(prefix+'Retry').hidden=!page?.error;$(prefix+'Retry').disabled=!!page?.busy;
    $(kind).setAttribute('aria-busy',String(!!page?.busy));
    $(prefix+'Page').textContent=page?.items?'Записей: '+page.total+' · страница '+(page.index+1):'Данные ещё не загружены';
    $(prefix+'Prev').disabled=!page||page.busy||page.index===0;
    $(prefix+'Next').disabled=!page||page.busy||!page.next_cursor;
  }
}
function draw() {
  const s=current();if(!viewReady||!s?.periods)return;const period=$('date').value,cost=$('cost').value;
  if(!/^\d{4}-\d{2}(?:-\d{2})?$/.test(period))return;
  const stats=s.periods[period],a=stats?.account,summary=stats?.summary?.[cost];
  $('dateLabel').textContent=$('mode').value==='month'?'Месяц':'Дата';$('periodTitle').textContent=s.strategy.toUpperCase()+' / '+period;
  overview(period,cost);$('metrics').replaceChildren();
  const coverage=stats?.trade_history_coverage||s.trade_history_coverage;
  metric('Известные закрытия · '+(cost==='gross'?'без комиссий':'после комиссий'),signed(stats?.[cost])+' ₽',coverage?.status==='verified'?'Полнота истории подтверждена · без открытых позиций':'Полнота истории закрытий не подтверждена',color(stats?.[cost]));
  metric('Доходность счёта · net',signed(a?.return_pct,4)+' %','Modified Dietz · указанное ниже окно',color(a?.return_pct));
  metric('Прибыльных закрытий',money(summary?.win_rate_pct)+' %',(summary?.wins??'—')+' в плюс / '+(summary?.losses??'—')+' в минус / '+(summary?.flat??'—')+' без результата');
  metric('Profit factor',summary?.profit_factor_state==='no_losses'?'Нет убытков':money(summary?.profit_factor),'Прибыль / модуль убытков · FIFO-части');
  const points=s.equity||[];calendar(s,period,cost);equity(points,a);
  if(!equityLoaded)$('equity').textContent='График загружается отдельно по кнопке «Показать график».';
  $('monthlyPanel').hidden=$('mode').value!=='month';const days=s.dates.filter(d=>d.startsWith(period.slice(0,7)));
  $('monthlyCoverage').textContent=days.length?'Есть записи за '+days[0]+' — '+days.at(-1)+'. Это не подтверждение полного месяца. Закрытия и доходность счёта используют разные основания расчёта.':'Нет истории за выбранный месяц.';
  table('monthlyRows',days.map(d=>{const p=s.periods[d],m=p.summary?.[cost];return[d,p.closures,(m?.wins??'—')+' / '+(m?.losses??'—')+' / '+(m?.flat??'—'),signed(p[cost]),signed(p.account?.return_pct,4)+' %',p.account?.coverage?'Подтверждено в окне':'Недостаточно данных'];}),[3,4]);
  table('symbols',(stats?.by_symbol||[]).map(p=>[paperButton(p.symbol),p.closures,p[cost].wins??'—',p[cost].losses??'—',signed(p[cost].pnl),money(p[cost].fees)]),[4]);detailTables(s,period,cost);
  $('positions').replaceChildren();
  for(const [symbol,p] of Object.entries(s.positions))if(Number(p.units)){const row=element('div',undefined,'position');row.append(element('strong',symbol),element('span',money(p.units)+' шт. · оценка '+money(p.value)+' ₽'),element('span',signed(p.unrealized_gross)+' ₽ / '+signed(p.return_pct)+' %',color(p.unrealized_gross)));$('positions').append(row);}
  if(!$('positions').children.length)$('positions').append(element('p','На последнем срезе позиций нет.','empty'));
  $('positions').append(element('p','Текущий срез: '+time(s.observed_at)+' · переоценка до комиссий, не относится к выбранному прошлому периоду.','hint'));
  $('quality').replaceChildren();for(const w of s.warnings)$('quality').append(element('p',w,'neg'));
  if(!s.warnings.length)$('quality').append(element('p','✓ Контроль количеств пройден. Покрытие периода указано отдельно.','pos'));
  const {missing=0,zero=0}=operationQuality;
  if(missing)$('quality').append(element('p',missing+' операций без известной комиссии: net может быть недоступен.','neg'));
  if(zero)$('quality').append(element('p',zero+' операций с нулевой комиссией в источнике. Учитывайте это при сравнении с PROD.','warn'));
  if(operationQuality.legacy)$('quality').append(element('p',operationQuality.legacy+' операций со старой неподтверждённой комиссией. Ноль в этих записях не считается подтверждённой бесплатной сделкой.','warn'));
  if(coverage?.status!=='verified')$('quality').append(element('p','Показана сумма известных закрытий. Полнота истории не подтверждена'+(coverage?.reason?': '+M.reason(coverage.reason):'.'),'warn'));
  for(const diagnostic of report.import?.diagnostics||[])$('quality').append(element('p',diagnostic,'warn'));
  if(equityLoaded)$('quality').append(element('p','Капитал выбранного периода: '+money(points.at(-1)?.equity)+' ₽ · '+time(points.at(-1)?.at),'hint'));
  if(Date.now()-Date.parse(s.observed_at)>20*60*1000)$('quality').append(element('p','Снимок этого счёта старше 20 минут.','warn'));
  $('updated').textContent='Сформировано '+time(report.generated_at)+' МСК';
}
function resetDetails(preserveFilters=false){
  closeCard();
  pages={};equityLoaded=false;requestErrors={};operationSymbols=[];
  if(!preserveFilters)for(const id of ['symbol','direction','outcome','operationSymbol'])$(id).value='';
}
function controlsReady(ready){
  for(const id of ['cost','symbol','direction','outcome','operationSymbol','download','loadEquity'])$(id).disabled=!ready;
}
function clearView(message){
  for(const id of ['overview','metrics','calendar','equity','positions','quality','operationCards'])$(id).replaceChildren();
  for(const id of ['trades','operations','signals','symbols','monthlyRows'])table(id,[],[],message);
  for(const [,prefix] of sections){
    $(prefix+'Status').textContent=message;$(prefix+'Status').className='section-status';$(prefix+'Retry').hidden=true;
    $(prefix+'Prev').disabled=true;$(prefix+'Next').disabled=true;$(prefix+'Page').textContent='';
  }
  $('tradeCount').textContent='';$('operationCount').textContent='';
  $('monthlyPanel').hidden=$('mode').value!=='month';$('monthTitle').textContent=$('date').value.slice(0,7);
  $('periodTitle').textContent=(current()?.strategy?.toUpperCase()||'Результат')+' / '+$('date').value;
  $('operationsTitle').textContent=($('mode').value==='month'?'Операции месяца · ':'Операции дня · ')+$('date').value;
}
function parameters(kind){
  const p={stream:$('strategy').value,period:$('date').value,generation:report.generation};
  if(kind==='trades')Object.assign(p,{symbol:$('symbol').value,direction:$('direction').value,outcome:$('outcome').value,cost:$('cost').value});
  if(kind==='operations')p.symbol=$('operationSymbol').value;
  return p;
}
function errorNotice(error,kind='view'){
  if(error.name==='AbortError')return;
  requestErrors[kind]=error;readyNotice();
}
function readyNotice(){
  const error=Object.values(requestErrors)[0];
  if(error){$('notice').className='error';$('notice').textContent='Не удалось обновить журнал: '+error.message+'. Данные этого запроса не загружены; уже показанные сведения могут быть устаревшими.';return;}
  if(!report)return;
  const errors=report.import?.errors||[],stale=Date.now()-Date.parse(report.generated_at)>20*60*1000;
  const sourceAge=Date.now()-Date.parse(current()?.observed_at),sourceStale=!Number.isFinite(sourceAge)||sourceAge>20*60*1000||sourceAge < -60000;
  $('notice').className=errors.length||stale||sourceStale?'error':'';
  $('notice').textContent=report.demo?'ДЕМОНСТРАЦИЯ: синтетические данные, не реальные сделки.':errors.length?'Есть ошибки импорта: '+errors.length+'. Не используйте неполные итоги.':stale?'Выгрузка устарела: '+time(report.generated_at)+' МСК.':sourceStale?'Свежесть снимка счёта не подтверждена: '+time(current()?.observed_at)+' МСК. Новая сборка журнала не обновляет брокерские данные.':'Сводка и страницы из SQLite · единый срез данных · только чтение.';
}
async function recoverSnapshot(error,epoch,allowRecovery){
  if(error.name!=='JournalSnapshotChangedError'||epoch!==viewEpoch||!allowRecovery)return false;
  // Only a snapshot conflict gets one full restart. Transport failures are never retried here.
  await load({recovering:true,allowRecovery:false,preserveFilters:true});
  return true;
}
async function refreshView({preserveFilters=false,focusOperations=false,allowRecovery=true}={}){
  const epoch=++viewEpoch;viewReady=false;resetDetails(preserveFilters);
  JournalLoader.cancelPending?.();
  $('dateLabel').textContent=$('mode').value==='month'?'Месяц':'Дата';
  clearView('Загружаем выбранный период…');controlsReady(false);
  if(focusOperations)$('operationsTitle').focus?.({preventScroll:true});
  if(!current())return false;
  if(!/^\d{4}-\d{2}(?:-\d{2})?$/.test($('date').value)){
    clearView('Выберите дату или месяц.');$('notice').textContent='Выберите дату или месяц для просмотра.';return false;
  }
  $('notice').className='';$('notice').textContent='Получаем сводку выбранного периода…';
  try{
    const bundle=await JournalLoader.loadJSON('summary',parameters('summary'));
    if(epoch!==viewEpoch)return false;
    for(const item of bundle.overview){const s=report.streams.find(x=>x.id===item.id);s.periods={[$('date').value]:item.stats};}
    Object.assign(current(),bundle.stream,{equity:[]});operationQuality=bundle.operation_quality||{};operationSymbols=bundle.operation_symbols||[];
    viewReady=true;draw();readyNotice();controlsReady(true);
    if(focusOperations)$('operationsPanel').scrollIntoView?.({block:'start'});
    // Read filters after summary, so changes made during its request cannot be lost.
    const pending=[fetchPage('trades',0,false,{allowRecovery}),fetchPage('operations',0,false,{allowRecovery})];
    if($('signalsDetails').open)pending.push(fetchPage('signals',0,false,{allowRecovery}));
    await Promise.all(pending);
    return epoch===viewEpoch&&viewReady;
  }catch(error){
    if(epoch!==viewEpoch)return false;
    if(await recoverSnapshot(error,epoch,allowRecovery))return viewReady;
    if(epoch!==viewEpoch)return false;
    JournalLoader.cancelPending?.();clearView('Не удалось загрузить период. Нажмите «Обновить».');errorNotice(error);return false;
  }
}
async function fetchPage(kind,step=0,reset=false,{allowRecovery=true}={}){
  const epoch=viewEpoch;
  if(!viewReady||!current()?.periods)return;
  if(reset){delete pages[kind];JournalLoader.cancelPending?.(kind);}
  let page=pages[kind];if(page?.busy)return;
  if(!page)pages[kind]=page={index:0,cursors:['']};
  const index=page.index+step,cursor=step>0?page.next_cursor:page.cursors[index]||'';
  if(index<0||(step>0&&!cursor))return;
  page.busy=true;page.error=null;page.retryStep=step;delete requestErrors[kind];draw();readyNotice();
  try{
    const result=await JournalLoader.loadJSON(kind,{...parameters(kind),cursor,limit:kind==='trades'?'50':'100'});
    if(epoch!==viewEpoch||pages[kind]!==page)return;
    page.cursors[index]=cursor;Object.assign(page,result,{index,busy:false,error:null});delete requestErrors[kind];draw();readyNotice();
  }catch(error){
    if(epoch===viewEpoch&&pages[kind]===page){
      if(await recoverSnapshot(error,epoch,allowRecovery))return;
      if(epoch!==viewEpoch||pages[kind]!==page)return;
      if(error.name!=='AbortError'){page.error=error;errorNotice(error,kind);}
    }
  }
  finally{page.busy=false;if(epoch===viewEpoch&&pages[kind]===page)draw();}
}
async function allRows(kind,params,epoch){
  const result=[];let cursor='';
  do{
    const page=await JournalLoader.loadJSON(kind,{...params,cursor,limit:'100'});
    if(epoch!==viewEpoch)throw new Error('Выбран другой период. Повторите запрос');
    if(page.total>20000)throw new Error('Более 20 000 строк. Выберите меньший период');
    result.push(...page.items);cursor=page.next_cursor;
  }while(cursor);
  return result;
}
async function load({recovering=false,allowRecovery=true,preserveFilters=true}={}){
  if ($('reload').disabled&&!recovering) return false;
  const loadId=++catalogLoadId;
  const epoch=++viewEpoch;viewReady=false;JournalLoader.cancelPending?.();
  resetDetails(preserveFilters);controlsReady(false);clearView(recovering?'История обновилась. Загружаем новый согласованный срез…':'Загружаем журнал…');
  $('reload').disabled=true;
  $('notice').className='';
  try{
    const data=await JournalLoader.loadReport({onStage:stage=>{
      $('notice').textContent=(stage==='request'?'Подключаемся к журналу…':'Получаем данные журнала…')+' Ожидание — не более 25 секунд.';
    }});
    if(epoch!==viewEpoch)return false;
    report=data;const previous=$('strategy').value;$('strategy').replaceChildren();
    for(const s of report.streams){const o=element('option',s.strategy.toUpperCase()+' · '+s.environment.toUpperCase()+' · '+s.id.slice(-4));o.value=s.id;$('strategy').append(o);}
    if(report.streams.some(s=>s.id===previous))$('strategy').value=previous;
    if(!$('date').value)$('date').value=M.changePeriod('',$('mode').value,dates());
    if(!report.streams.length){++viewEpoch;viewReady=false;resetDetails();controlsReady(false);clearView('Нет импортированной истории.');$('notice').textContent='Нет импортированной истории. Демо-сделки не подставляются.';shown=[];return false;}
    return await refreshView({preserveFilters,allowRecovery});
  }catch(error){if(epoch===viewEpoch){clearView('Не удалось загрузить журнал. Нажмите «Обновить».');errorNotice(error);}return false;}
  finally{if(loadId===catalogLoadId)$('reload').disabled=false;}
}
for(const id of ['strategy','date'])$(id).addEventListener('change',refreshView);
for(const id of ['cost','symbol','direction','outcome'])$(id).addEventListener('change',()=>fetchPage('trades',0,true));
$('operationSymbol').addEventListener('change',()=>fetchPage('operations',0,true));
for(const [kind,prefix] of sections){
  for(const [suffix,step] of [['Prev',-1],['Next',1]])$(prefix+suffix).addEventListener('click',()=>fetchPage(kind,step));
  $(prefix+'Retry').addEventListener('click',()=>fetchPage(kind,pages[kind]?.retryStep||0));
}
$('signalsDetails').addEventListener('toggle',()=>{if($('signalsDetails').open&&!pages.signals?.items)void fetchPage('signals');});
$('mode').addEventListener('change',()=>{const value=M.changePeriod($('date').value,$('mode').value,dates());$('date').type=$('mode').value==='month'?'month':'date';$('date').value=value;return refreshView();});
for(const [id,amount] of [['periodPrev',-1],['periodNext',1]])$(id).addEventListener('click',()=>{$('date').value=M.movePeriod($('date').value,$('mode').value,amount);return refreshView();});
$('reload').addEventListener('click',load);$('printReport').addEventListener('click',()=>window.print());
async function loadEquity(allowRecovery=true){
  if(!viewReady)return;
  const epoch=viewEpoch;$('loadEquity').disabled=true;
  try{const points=await allRows('equity',parameters('equity'),epoch);current().equity=points;equityLoaded=true;delete requestErrors.equity;draw();readyNotice();}
  catch(error){if(epoch===viewEpoch){if(await recoverSnapshot(error,epoch,allowRecovery)){if(viewReady)return loadEquity(false);}else errorNotice(error,'equity');}}
  finally{if(epoch===viewEpoch)$('loadEquity').disabled=!viewReady;}
}
async function exportRows(allowRecovery=true){
  if(!viewReady)return;
  const epoch=viewEpoch,params=parameters('trades');$('download').disabled=true;
  try{
  const selected=await allRows('trades',params,epoch);
  if(JSON.stringify(params)!==JSON.stringify(parameters('trades')))throw new Error('Фильтр изменился. Повторите экспорт');
  const rows=[['symbol','direction','opened_at','closed_at','quantity','entry','exit','gross','fees','net','gross_return_pct','net_return_pct'],...selected.map(t=>[t.symbol,t.direction,t.opened_at,t.closed_at,t.quantity,t.entry,t.exit,t.gross,t.fees,t.net,t.return_pct,M.tradePercent(t,'net')])];
  const blob=new Blob(['\ufeff'+rows.map((r,row)=>r.map((value,column)=>row>0&&column>=4?M.csvNumber(value):M.csvCell(value)).join(';')).join('\r\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=element('a');a.href=url;a.download='journal-'+(current()?.strategy||'unknown')+'-'+$('date').value+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  delete requestErrors.export;readyNotice();
  }catch(error){if(epoch===viewEpoch){if(await recoverSnapshot(error,epoch,allowRecovery)){if(viewReady)return exportRows(false);}else errorNotice(error,'export');}}
  finally{if(epoch===viewEpoch)$('download').disabled=!viewReady;}
}
$('loadEquity').addEventListener('click',()=>loadEquity());$('download').addEventListener('click',()=>exportRows());
let cardState=null,cardRequest=0;
function paperButton(symbol,anchor=null){
  const button=element('button',symbol+' ↗','paper-link');button.setAttribute('aria-label','Карточка '+symbol);
  button.addEventListener('click',()=>openCard(symbol,anchor));return button;
}
function closeCard(){++cardRequest;JournalLoader.cancelPending?.('detail');cardState=null;if($('tradeDialog').open)$('tradeDialog').close();}
async function openCard(symbol,anchor=null){
  cardState={symbol,anchor,offset:0,ref:'',epoch:viewEpoch,stream:$('strategy').value,period:$('date').value};
  $('cardTitle').textContent=symbol+' · карточка сделки';$('cardScope').textContent=(report.demo?'ДЕМО · ':'')+current().strategy.toUpperCase()+' · '+current().environment.toUpperCase()+' · '+cardState.period+' · МСК';
  if(!$('tradeDialog').open)$('tradeDialog').showModal();await loadCard();
}
function renderReason(box,label,reason){
  box.append(element('h3',label),element('p','Точная причина не подтверждена связью с брокерской операцией.','hint'));
  for(const signal of reason?.context||[])box.append(element('p','Контекст, не доказанная причина · '+time(signal.at)+' · '+M.status(signal.status)+' · '+(signal.reasons||[]).map(M.reason).join('; '),'hint'));
  for(const order of reason?.order_context||[]){const p=element('p','Заявка соседнего цикла '+time(order.at)+' (связь с исполнением не подтверждена): '+M.rationale(order.rationale),'hint');box.append(p);}
}
function drawPriceChart(box,card){
  const chart=M.priceCoordinates(card.chart?.points||[],card.markers||[],card.chart?.interval||1);
  if(!chart){box.append(element('p','Нет подтверждённых цен для графика.','empty'));return;}
  const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 640 260');svg.setAttribute('role','img');svg.setAttribute('aria-label','Рыночная цена '+card.symbol+' и отдельные точки брокерских операций');
  const shape=(name,attrs)=>{const e=document.createElementNS(ns,name);for(const [key,value] of Object.entries(attrs))e.setAttribute(key,String(value));svg.append(e);return e;};
  for(const [value,y] of [[chart.min,210],[chart.max,40]]){shape('line',{x1:55,x2:625,y1:y,y2:y,stroke:'#30403a'});const text=shape('text',{x:2,y:y-4,fill:'#a6b5aa','font-size':12});text.textContent=money(value);}
  for(const segment of chart.segments)if(segment.length>1)shape('polyline',{points:segment.map(p=>p.join(',')).join(' '),fill:'none',stroke:'#b5e990','stroke-width':2});
  for(const segment of chart.segments)if(segment.length===1)shape('circle',{cx:segment[0][0],cy:segment[0][1],r:2,fill:'#b5e990'});
  for(const marker of chart.markers){const dot=shape('circle',{cx:marker.x,cy:marker.y,r:6,fill:marker.side==='BUY'?'#8bbfff':'#ffbd7d',stroke:'#101615','stroke-width':2});const title=document.createElementNS(ns,'title');title.textContent=marker.label+' · '+marker.side+' · '+time(marker.at)+' · '+money(marker.price)+' ₽';dot.append(title);const text=shape('text',{x:Math.min(570,Math.max(55,marker.x)),y:marker.y<55?marker.y+22:marker.y-12,fill:'#e6eee8','font-size':12});text.textContent=marker.label;}
  box.append(svg,element('p',time(chart.start)+' → '+time(chart.end)+' МСК · ось цены: ₽','hint'));
}
function renderCard(data){
  const box=$('cardContent');box.replaceChildren();const card=data.card;
  if(!card){box.append(element('p','Нет операций или закрытий этой бумаги в выбранном периоде.','empty'));return;}
  if(card.kind==='closed'){
    box.append(element('h3',direction(card.direction)+' · закрыто '+money(card.quantity)+' шт.'));
    const result=element('div',undefined,'card-result');result.append(element('strong',signed(card.gross)+' ₽ / '+signed(card.return_pct,4)+' %',color(card.gross)),element('p','Без комиссий · процент от стоимости входа'));
    result.append(element('p','После комиссий: '+signed(card.net)+' ₽ / '+signed(card.net_return_pct,4)+' % · комиссии '+money(card.fees)+' ₽',color(card.net)));box.append(result);
    box.append(element('p','Аналитическое FIFO-закрытие (возможно часть позиции). Цена входа/выхода — средняя из суммы и количества исполнения; не цена лимита.','hint'));
  }else{
    const op=card.operation;box.append(element('h3',direction(op.side)+' · '+money(op.quantity)+' шт.'),element('p',time(op.at)+' · цена '+operationPriceText(op)+' ₽ · сумма '+money(op.gross)+' ₽'));
    box.append(element('p','Операция не равна закрытой сделке. Её прибыль не рассчитывается отдельно. Выберите FIFO-закрытие в списке, если оно есть.','hint'));
  }
  drawPriceChart(box,card);
  for(const marker of card.markers)box.append(element('p',marker.label+' · '+direction(marker.side)+' · '+time(marker.at)+' МСК · '+money(marker.quantity)+' шт. × '+operationPriceText({price:marker.price,price_source:'broker'})+' ₽','marker-'+marker.side.toLowerCase()));
  const sources=card.chart.sources||[],bad=sources.filter(s=>s.status!=='ok');
  box.append(element('p',report.demo?'Синтетический пример графика и исполнений.':('Линия: закрытия '+card.chart.interval+'-минутных свечей MOEX ISS / TQBR. Публичные данные могут задерживаться. Точки: брокерские операции. Разрывы не заполняются.'),'hint'));
  if(!card.chart.points.length||bad.length||card.chart.truncated)box.append(element('p','Рыночный график неполон: '+(bad.map(s=>s.day+' '+({pending:'ожидает сбора',empty:'источник не вернул свечи',error:'ошибка источника'}[s.status]||s.status)+(s.error?' ('+s.error+')':'')).join('; ')||'нет свечей в окне')+(card.chart.truncated?' · достигнут предел точек/истории':''),'warn'));
  if(sources.some(s=>s.retrieved_at))box.append(element('p','Цены получены: '+sources.filter(s=>s.retrieved_at).map(s=>time(s.retrieved_at)).filter((v,i,a)=>a.indexOf(v)===i).join(', ')+' МСК','hint'));
  if(card.kind==='closed'){renderReason(box,'Причина входа',card.entry_reason);renderReason(box,'Причина выхода',card.exit_reason);}else renderReason(box,'Причина '+(card.operation.side==='BUY'?'покупки':'продажи'),card.operation_reason);
  if(data.position&&Number(data.position.units))box.append(element('p','Отдельно: текущая позиция '+money(data.position.units)+' шт. · открытый результат '+signed(data.position.unrealized_gross)+' ₽ / '+signed(data.position.return_pct)+' % (без комиссий), срез '+time(data.observed_at)+'. Это не результат выбранного прошлого дня.','hint'));
}
async function loadCard(){
  if(!cardState)return;const state=cardState,request=++cardRequest;JournalLoader.cancelPending?.('detail');
  $('cardContent').replaceChildren();$('cardStatus').textContent='Загружаем карточку…';$('cardRetry').hidden=true;
  for(const id of ['cardSelect','cardPrev','cardNext'])$(id).disabled=true;
  try{
    const data=await JournalLoader.loadJSON('detail',{stream:state.stream,period:state.period,generation:report.generation,symbol:state.symbol,ref:state.ref,offset:String(state.offset),anchor:state.anchor?JSON.stringify(state.anchor):''});
    if(cardState!==state||request!==cardRequest||state.epoch!==viewEpoch)return;
    state.anchor=null;state.ref=data.card?.ref||'';
    $('cardSelect').replaceChildren();for(const o of data.options){const option=element('option',(o.kind==='closed'?'Закрытие · ':'Операция · ')+time(o.at)+(o.quantity?' · '+money(o.quantity)+' шт.':''));option.value=o.ref;$('cardSelect').append(option);}
    if(data.card)$('cardSelect').value=data.card.ref;state.next=data.next_offset;
    $('cardSelect').disabled=!data.options.length;$('cardPrev').disabled=state.offset===0;$('cardNext').disabled=data.next_offset===null;
    $('cardCount').textContent='Записей '+data.total+' · страница '+(state.offset/50+1);$('cardStatus').textContent='Только чтение · единый срез журнала';renderCard(data);
  }catch(error){if(cardState!==state||request!==cardRequest||error.name==='AbortError')return;
    $('cardStatus').textContent=error.name==='JournalSnapshotChangedError'?'История обновилась. Закройте карточку, нажмите «Обновить» и откройте её снова.':'Карточка не загружена: '+error.message;
    $('cardRetry').hidden=error.name==='JournalSnapshotChangedError';
  }
}
$('cardClose').addEventListener('click',closeCard);$('tradeDialog').addEventListener('close',closeCard);
$('cardRetry').addEventListener('click',loadCard);
$('cardSelect').addEventListener('change',()=>{if(cardState){cardState.ref=$('cardSelect').value;return loadCard();}});
for(const [id,step] of [['cardPrev',-50],['cardNext',50]])$(id).addEventListener('click',()=>{if(cardState){cardState.offset+=step;cardState.ref='';return loadCard();}});
load();
