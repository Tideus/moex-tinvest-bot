'use strict';
// Pure presentation rules: no broker access or trading decisions.
(function (root) {
  // Reuse ICU state: rebuilding this for each signal can consume hundreds of MB.
  const dayFormatter = new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'});
  const statuses = {planned:'Включён в план',filter_passed:'Фильтр пройден',filter_rejected:'Не прошёл фильтр',entry_rejected:'Вход отклонён',not_evaluated:'Вход не оценивался',filled:'Исполнено',accepted:'Принято брокером',cancelled:'Отменено',rejected:'Отклонено брокером',partial:'Частично исполнено'};
  const reasons = {price_move:'Недостаточный импульс цены',trade_imbalance:'Нет подтверждения потоком сделок',order_flow:'Нет подтверждения потоком заявок',book_imbalance:'Не подходит дисбаланс стакана',cost_edge:'Ожидаемый эффект ниже порога после расходов',spread_bbo:'Слишком широкий спред',daily_entry_limit:'Достигнут дневной лимит входов',position_limit:'Достигнут лимит позиций',force_flat:'Окно обязательного закрытия',cash:'Недостаточно свободных средств',existing_position:'Позиция уже открыта',short_disabled:'Шорт запрещён настройками',outside_entry_window:'Вне времени новых входов',duplicate_signal:'Повторный сигнал',missing_history:'Не хватает истории',stale_history:'История устарела',ttl_expired:'Истёк срок лимитной заявки — штатная отмена',operation_coverage_missing:'Нет подтверждённого полного диапазона брокерских операций',baseline_missing:'Нет подтверждённой начальной базы',operation_coverage_gap:'Есть пробелы в покрытии истории операций',broker_operation_range:'Диапазон операций подтверждён брокерской выгрузкой'};
  function numeric(value) {
    if (!['string','number'].includes(typeof value) || String(value).trim() === '') return null;
    const result = Number(value); return Number.isFinite(result) ? result : null;
  }
  function localDay(value) {
    const at = new Date(value); if (!Number.isFinite(at.getTime())) return '';
    return dayFormatter.format(at);
  }
  function inPeriod(value, period) { return /^\d{4}-\d{2}(?:-\d{2})?$/.test(period) && localDay(value).startsWith(period); }
  function tradePercent(trade, cost) {
    if (cost === 'gross') return numeric(trade.return_pct);
    if (trade.net_return_pct !== undefined) return numeric(trade.net_return_pct);
    const net=numeric(trade.net),entry=numeric(trade.entry),quantity=numeric(trade.quantity);
    return net !== null && entry > 0 && quantity > 0 ? net/(entry*quantity)*100 : null;
  }
  function selectTrades(trades, period, symbol, direction, outcome, cost) {
    return trades.filter(t => { const pnl=numeric(t[cost]); return inPeriod(t.closed_at,period) && (!symbol||t.symbol===symbol) && (!direction||t.direction===direction) && (!outcome || (outcome==='win'&&pnl!==null&&pnl>0) || (outcome==='loss'&&pnl!==null&&pnl<0) || (outcome==='flat'&&pnl===0) || (outcome==='unknown'&&pnl===null)); });
  }
  function changePeriod(value, mode, dates) {
    const latest=[...dates].sort().at(-1)||localDay(new Date()),base=value||latest;
    if(mode==='month')return base.slice(0,7); if(base.length===10)return base;
    return [...dates].filter(d=>d.startsWith(base)).sort().at(-1)||base+'-01';
  }
  function movePeriod(value,mode,amount) {
    if(!/^\d{4}-\d{2}(?:-\d{2})?$/.test(value))return value;
    const [year,month,day=1]=value.split('-').map(Number);
    const date=mode==='month'?new Date(Date.UTC(year,month-1+amount,1)):new Date(Date.UTC(year,month-1,day+amount));
    return date.toISOString().slice(0,mode==='month'?7:10);
  }
  function equityCoordinates(points) {
    const valid=points.filter(p=>numeric(p.equity)!==null&&Number.isFinite(Date.parse(p.at))).sort((a,b)=>Date.parse(a.at)-Date.parse(b.at));
    if(valid.length<2)return null;
    const values=valid.map(p=>Number(p.equity)),min=Math.min(...values),max=Math.max(...values),start=Date.parse(valid[0].at),end=Date.parse(valid.at(-1).at);
    if(end<=start)return null;
    return {min,max,start:valid[0].at,end:valid.at(-1).at,points:valid.map(p=>[10+580*(Date.parse(p.at)-start)/(end-start),max===min?85:150-130*(Number(p.equity)-min)/(max-min)])};
  }
  function csvCell(value) { return '"'+String(value??'').replace(/^[\s]*[=+@-]/,"'$&").replaceAll('"','""')+'"'; }
  // Numeric columns are validated separately: a loss must remain a number in CSV.
  function csvNumber(value) {
    const text=String(value??'').trim().replace(/^\+/,'');
    return numeric(value)!==null && /^-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(text) ? '"'+text+'"' : '""';
  }
  function operationPrice(operation) {
    if(operation.price!==undefined)return numeric(operation.price);
    const gross=numeric(operation.gross),quantity=numeric(operation.quantity);
    return gross!==null&&quantity>0?Math.abs(gross)/quantity:null;
  }
  function priceCoordinates(points,markers,interval){
    const valid=points.filter(p=>numeric(p.close)>0&&Number.isFinite(Date.parse(p.at))).sort((a,b)=>Date.parse(a.at)-Date.parse(b.at));
    const fills=markers.filter(m=>numeric(m.price)>0&&Number.isFinite(Date.parse(m.at)));
    const all=[...valid.map(p=>({at:p.at,price:p.close})),...fills];if(!all.length)return null;
    const values=all.map(p=>Number(p.price)),min=Math.min(...values),max=Math.max(...values),span=max-min||Math.max(0.01,min*0.002);
    let start=Math.min(...all.map(p=>Date.parse(p.at))),end=Math.max(...all.map(p=>Date.parse(p.at)));if(start===end){start-=60000;end+=60000;}
    const xy=(at,price)=>[55+550*(Date.parse(at)-start)/(end-start),210-170*(Number(price)-min)/(span)];
    const segments=[];let previous=null;
    for(const p of valid){if(!previous||Date.parse(p.at)-Date.parse(previous.at)>interval*60000*1.1)segments.push([]);segments.at(-1).push(xy(p.at,p.close));previous=p;}
    return {min,max,start:new Date(start).toISOString(),end:new Date(end).toISOString(),segments,markers:fills.map(m=>{const [x,y]=xy(m.at,m.price);return {...m,x,y};})};
  }
  function rationale(value){return String(value||'').replace('intraday momentum','Внутридневной импульс').replace('intraday mandatory flat','Обязательное закрытие перед концом сессии').replace('intraday managed exit','Закрытие intraday').replace('direction=long','Направление: LONG').replace('direction=short','Направление: SHORT').replace('stop_loss:','Порог убытка: ').replace('take_profit:','Цель прибыли: ').replace('max_holding','Предельное время удержания').replace('force_flat','Обязательное закрытие перед концом сессии').replace(/(?:momentum|move)=(-?\d+(?:\.\d+)?)/g,(_,v)=>'Изменение цены '+(Number(v)*100).toFixed(2)+'%').replace(/(?:trade|order|book)=(-?\d+(?:\.\d+)?)/g,(all,v)=>({'trade':'Дисбаланс сделок','order':'Поток заявок','book':'Дисбаланс стакана'}[all.split('=')[0]])+' '+Number(v).toFixed(3)).replaceAll('bps',' б.п.');}
  const api={numeric,localDay,inPeriod,tradePercent,selectTrades,changePeriod,movePeriod,equityCoordinates,priceCoordinates,csvCell,csvNumber,operationPrice,rationale,status:value=>statuses[value]||value||'Неизвестно',reason:value=>reasons[value]||'Причина: '+value};
  if(typeof module!=='undefined'&&module.exports)module.exports=api; else root.JournalModel=api;
})(globalThis);
