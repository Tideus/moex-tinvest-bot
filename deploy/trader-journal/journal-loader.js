'use strict';
// A deadline covers both response headers AND the complete JSON body.
(function (root) {
  const pending = new Map();
  function cancelPending(route) {
    for (const [controller, kind] of pending) if (!route || route === kind) controller.abort();
  }
  async function loadJSON(route, params = {}, {fetchImpl = root.fetch.bind(root), timeoutMs = 25000, onStage = () => {}} = {}) {
    if (!['catalog','summary','trades','signals','operations','equity','orders','health','detail'].includes(route)) throw new Error('Неизвестный раздел журнала');
    const controller = new AbortController();
    const requestId = root.crypto?.randomUUID?.() || Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
    let phase = 'headers';
    const diagnostic = () => '['+route+' · '+({headers:'ожидание заголовков',response:'HTTP-ответ',body:'получение JSON'}[phase])+' · ID '+requestId+']';
    pending.set(controller, route);
    let onAbort;
    const cancelled = new Promise((_, reject) => {
      onAbort = () => { const error = new Error('Запрос заменён новым'); error.name='AbortError'; reject(error); };
      controller.signal.addEventListener('abort', onAbort, {once:true});
    });
    let timer;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        const error = new Error('Запрос не завершён за '+Math.ceil(timeoutMs/1000)+' с. '+diagnostic()+'. Нажмите «Обновить»');
        error.name = 'JournalTimeoutError';
        reject(error);
        controller.abort();
      }, timeoutMs);
    });
    const request = async () => {
      onStage('request');
      const query = new URLSearchParams(params).toString();
      const response = await fetchImpl('api/'+route+(query?'?'+query:''), {cache:'no-store', credentials:'same-origin', signal:controller.signal, headers:{'X-Journal-Request-Id':requestId}});
      phase = 'response';
      if (response.status === 409) {
        const error = new Error('История обновилась. Нужен новый согласованный срез');
        error.name = 'JournalSnapshotChangedError';
        throw error;
      }
      if (!response.ok) throw new Error(response.status===401 ? 'Вход истёк. Обновите страницу и войдите снова' : 'Ошибка получения отчёта: HTTP '+response.status);
      phase = 'body';
      if (!controller.signal.aborted) onStage('body');
      const data = await response.json();
      if (data?.schema !== 'trader-journal-api-v1' || typeof data.generation !== 'string' || (route==='catalog'&&!Array.isArray(data.streams))) throw new Error('Версия формата отчёта не поддерживается');
      if (params.generation && data.generation !== params.generation) {
        const error = new Error('Ответ относится к другому срезу истории');error.name='JournalSnapshotChangedError';throw error;
      }
      return data;
    };
    try { return await Promise.race([request(), deadline, cancelled]); }
    catch (error) {
      if (error.name !== 'AbortError' && error.name !== 'JournalTimeoutError') error.message += ' '+diagnostic();
      throw error;
    }
    finally { clearTimeout(timer); controller.signal.removeEventListener('abort',onAbort); pending.delete(controller); }
  }
  const api = {loadJSON, cancelPending, loadReport: options => loadJSON('catalog', {}, options)};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.JournalLoader = api;
})(globalThis);
