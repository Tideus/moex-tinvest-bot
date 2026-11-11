'use strict';
// A deadline covers both response headers AND the complete JSON body.
(function (root) {
  async function loadJSON(route, params = {}, {fetchImpl = root.fetch.bind(root), timeoutMs = 25000, onStage = () => {}} = {}) {
    if (!['catalog','summary','trades','signals','operations','equity','orders','health'].includes(route)) throw new Error('Неизвестный раздел журнала');
    const controller = new AbortController();
    let timer;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        const error = new Error('Сервер не передал полный отчёт за '+Math.ceil(timeoutMs/1000)+' с. Проверьте соединение и нажмите «Обновить»');
        error.name = 'JournalTimeoutError';
        reject(error);
        controller.abort();
      }, timeoutMs);
    });
    const request = async () => {
      onStage('request');
      const query = new URLSearchParams(params).toString();
      const response = await fetchImpl('api/'+route+(query?'?'+query:''), {cache:'no-store', credentials:'same-origin', signal:controller.signal});
      if (response.status === 409) throw new Error('История обновилась. Нажмите «Обновить», чтобы получить согласованный срез');
      if (!response.ok) throw new Error(response.status===401 ? 'Вход истёк. Обновите страницу и войдите снова' : 'Ошибка получения отчёта: HTTP '+response.status);
      if (!controller.signal.aborted) onStage('body');
      const data = await response.json();
      if (data?.schema !== 'trader-journal-api-v1' || typeof data.generation !== 'string' || (route==='catalog'&&!Array.isArray(data.streams))) throw new Error('Версия формата отчёта не поддерживается');
      return data;
    };
    try { return await Promise.race([request(), deadline]); }
    finally { clearTimeout(timer); }
  }
  const api = {loadJSON, loadReport: options => loadJSON('catalog', {}, options)};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.JournalLoader = api;
})(globalThis);
