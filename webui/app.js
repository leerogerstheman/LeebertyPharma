'use strict';

/* PharmaCrawler · 网页版前端
 *
 * 抓取是长任务：后端把 CrawlSession 的 progress_cb 存进有界缓冲，这里轮询
 * /api/crawl/status。只在「正在跑」的时候轮询，停下来就不打扰服务器。
 */

const $ = (id) => document.getElementById(id);

const state = {
  meta: null,
  running: false,
  poll: 0,
  rows: [],
};

/* ------------------------------------------------------------ 小工具 -- */

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast' + (kind === 'err' ? ' err' : '');
  el.textContent = msg;
  $('toastHost').appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 8000 : 4000);
}

async function api(path, body) {
  const opt = body === undefined
    ? undefined
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok || !data || data.ok === false) {
    throw new Error((data && data.error) || (res.status + ' ' + res.statusText));
  }
  return data;
}

function text(id, value) { const el = $(id); if (el) el.textContent = value; }

/* ------------------------------------------------------------ 页签 -- */

function wireTabs() {
  for (const t of document.querySelectorAll('.tab')) {
    t.onclick = () => {
      for (const x of document.querySelectorAll('.tab')) x.classList.toggle('active', x === t);
      const name = t.dataset.tab;
      for (const p of document.querySelectorAll('.panel')) {
        p.classList.toggle('active', p.id === 'panel-' + name);
      }
      if (name === 'library') loadStats();
    };
  }
}

/* ------------------------------------------------------------ 元信息 -- */

function renderDatasets(meta) {
  const box = $('dsList');
  box.innerHTML = '';
  const defaults = new Set(meta.defaultDatasets || []);
  for (const ep of meta.endpoints) {
    const label = document.createElement('label');
    label.className = 'ds-item' + (defaults.has(ep.key) ? ' on' : '');
    label.title = ep.note || '';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = ep.key;
    cb.checked = defaults.has(ep.key);
    cb.onchange = () => label.classList.toggle('on', cb.checked);
    const span = document.createElement('span');
    span.textContent = ep.label;
    const small = document.createElement('small');
    small.textContent = ep.key;
    label.append(cb, span, small);
    box.appendChild(label);
  }
}

function selectedDatasets() {
  return [...document.querySelectorAll('#dsList input:checked')].map((c) => c.value);
}

function renderConfig(cfg) {
  text('credChip', state.meta.credentials || '');
  $('credChip').classList.toggle('warn', !cfg.hasOpenfdaKey);
  text('rangeChip', '时间范围 ' + (state.meta.dateRange || '不限'));
  text('ver', 'v' + state.meta.version);

  $('sRps').value = cfg.requestsPerSecond ?? '';
  $('sConc').value = cfg.concurrency ?? '';
  $('sTimeout').value = cfg.timeout ?? '';
  $('sRetry').value = cfg.maxRetries ?? '';
  $('sSegment').value = cfg.segmentStrategy || 'auto';
  $('sMax').value = cfg.maxRecordsPerRun ?? 0;
  $('sFrom').value = cfg.dateFrom || '';
  $('sTo').value = cfg.dateTo || '';
  $('sProxy').value = cfg.proxy || '';
  $('sEmail').value = cfg.pubmedEmail || '';
  $('sSort').value = cfg.pubmedSort || '';
  $('sReldays').value = cfg.pubmedReldays || '';
  $('maxInput').value = cfg.maxRecordsPerRun || 0;

  const fmts = $('formats');
  fmts.innerHTML = '';
  for (const [key, on] of Object.entries(cfg.export || {})) {
    const b = document.createElement('button');
    b.className = 'btn' + (on ? ' btn-primary' : '');
    b.textContent = key.toUpperCase();
    b.title = on ? '配置里默认开启' : '配置里默认关闭';
    b.onclick = () => doExport(key);
    fmts.appendChild(b);
  }
}

/* ------------------------------------------------------------ 抓取 -- */

function crawlPayload() {
  return {
    datasets: selectedDatasets(),
    allDatasets: $('allDs').checked,
    search: $('searchInput').value,
    term: $('termInput').value,
    sort: $('sortInput').value,
    maxRecords: parseInt($('maxInput').value, 10) || 0,
  };
}

async function doPlan() {
  try {
    const d = await api('/api/plan', crawlPayload());
    $('log').textContent = '抓取计划（共 ' + d.plan.length + ' 个任务）：\n'
      + d.plan.map((p, i) => '  ' + (i + 1) + '. ' + JSON.stringify(p)).join('\n');
    toast('计划已生成，共 ' + d.plan.length + ' 个任务');
  } catch (err) {
    toast('生成计划失败：' + err.message, 'err');
  }
}

async function doStart() {
  $('startBtn').disabled = true;
  try {
    const d = await api('/api/crawl/start', crawlPayload());
    state.running = true;
    $('stopBtn').disabled = false;
    $('log').textContent = '已开始，共 ' + d.plan.length + ' 个任务…';
    startPolling();
  } catch (err) {
    $('startBtn').disabled = false;
    toast('无法开始：' + err.message, 'err');
  }
}

async function doStop() {
  $('stopBtn').disabled = true;
  try {
    const d = await api('/api/crawl/stop', {});
    toast(d.stopped ? '已请求安全停止' : d.message);
  } catch (err) {
    toast('停止失败：' + err.message, 'err');
  }
}

function startPolling() {
  clearInterval(state.poll);
  state.poll = setInterval(pollStatus, 900);
  pollStatus();
}

async function pollStatus() {
  let d;
  try {
    d = await api('/api/crawl/status');
  } catch (err) {
    return;
  }

  const c = d.counters || {};
  text('cFetched', c.fetched || 0);
  text('cStored', c.stored || 0);
  text('cSkipped', c.skipped || 0);
  text('cFailed', c.failed || 0);
  text('cElapsed', (d.elapsed || 0) + 's');

  const pct = d.total > 0 ? Math.min(100, Math.round(((c.fetched || 0) / d.total) * 100)) : 0;
  $('bar').style.width = pct + '%';

  if (d.log && d.log.length) {
    const box = $('log');
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    box.textContent = d.log.join('\n');
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  if (!d.running && state.running) {
    state.running = false;
    clearInterval(state.poll);
    $('startBtn').disabled = false;
    $('stopBtn').disabled = true;
    if (d.error) toast('抓取出错：' + d.error, 'err');
    else if (d.summary) toast('抓取结束：获取 ' + (d.summary.fetched || 0) + ' · 入库 ' + (d.summary.stored || 0));
    loadStats();
  }
}

/* ------------------------------------------------------------ 检索 -- */

function searchPayload() {
  const one = (id) => { const v = $(id).value.trim(); return v ? [v] : []; };
  return {
    keyword: $('kwInput').value,
    fieldScope: $('scopeSel').value,
    sources: one('srcSel'),
    datasets: one('dsSel'),
    years: one('yearSel'),
    dateFrom: $('fromInput').value,
    dateTo: $('toInput').value,
    hasAbstract: $('absChk').checked,
    hasDoi: $('doiChk').checked,
    matchAll: $('allChk').checked,
  };
}

async function doSearch() {
  const btn = $('searchBtn');
  btn.disabled = true;
  try {
    const d = await api('/api/search', searchPayload());
    state.rows = d.rows;
    renderRows(d.rows);
    text('hits', '命中 ' + d.total + ' 条' + (d.shown < d.total ? '（表格显示前 ' + d.shown + ' 条）' : ''));
    text('tableNote', d.total === 0 ? '没有命中。可能图库还是空的——先抓一批数据。' : '');
    fillFacets(d.facets);
  } catch (err) {
    toast('检索失败：' + err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function renderRows(rows) {
  const tb = $('rows');
  tb.innerHTML = '';
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 6; td.className = 'empty'; td.textContent = '没有命中。';
    tr.appendChild(td); tb.appendChild(tr);
    return;
  }
  for (const r of rows) {
    const tr = document.createElement('tr');

    const src = document.createElement('td');
    const b = document.createElement('span');
    b.className = 'badge'; b.textContent = r.source || '—';
    src.appendChild(b);

    const ds = document.createElement('td');
    ds.textContent = r.dataset || '—';

    const ttl = document.createElement('td');
    ttl.className = 'ttl';
    ttl.textContent = r.title || '(无标题)';

    const date = document.createElement('td');
    date.textContent = r.date || '—';

    const jr = document.createElement('td');
    jr.textContent = r.journal || '—';

    const act = document.createElement('td');
    const open = document.createElement('button');
    open.className = 'btn btn-text';
    open.textContent = '详情';
    open.onclick = () => openRecord(r.key);
    act.appendChild(open);

    tr.append(src, ds, ttl, date, jr, act);
    tb.appendChild(tr);
  }
}

function fillFacets(f) {
  const put = (sel, items) => {
    const el = $(sel);
    const keep = el.value;
    el.innerHTML = '<option value="">全部</option>';
    for (const it of items || []) {
      const o = document.createElement('option');
      o.value = it.value;
      o.textContent = it.value + ' (' + it.count + ')';
      el.appendChild(o);
    }
    if (keep) el.value = keep;
  };
  put('srcSel', f.sources);
  put('dsSel', f.datasets);
  put('yearSel', f.years);
}

async function openRecord(key) {
  try {
    const d = await api('/api/record?key=' + encodeURIComponent(key));
    text('dlgTitle', d.record.title || '记录详情');
    $('dlgBody').textContent = JSON.stringify(d.record, null, 2);
    $('dialogHost').hidden = false;
  } catch (err) {
    toast('取详情失败：' + err.message, 'err');
  }
}

function clearFilters() {
  for (const id of ['kwInput', 'fromInput', 'toInput']) $(id).value = '';
  for (const id of ['srcSel', 'dsSel', 'yearSel']) $(id).value = '';
  $('scopeSel').value = 'all';
  for (const id of ['absChk', 'doiChk', 'allChk']) $(id).checked = false;
}

async function exportResults() {
  if (!state.rows.length) { toast('先检索出一些结果'); return; }
  toast('命中结果导出走图库的「导出」按钮（导出全部入库记录）');
}

/* ------------------------------------------------------------ 图库 -- */

async function loadStats() {
  try {
    const d = await api('/api/library/stats');
    $('statsBox').textContent = JSON.stringify(d.stats, null, 2);
  } catch (err) {
    $('statsBox').textContent = '读取失败：' + err.message;
  }
}

async function doRebuild() {
  const btn = $('rebuildBtn');
  btn.disabled = true;
  text('rebuildNote', '正在重建…');
  try {
    const d = await api('/api/library/rebuild', {});
    const n = d.result.records ?? 0;
    text('rebuildNote', '完成：记录总数 ' + n);
    toast('索引与分类已重建');
    loadStats();
  } catch (err) {
    text('rebuildNote', '失败：' + err.message);
    toast('重建失败：' + err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function doExport(fmt) {
  text('exportNote', '正在导出 ' + fmt + '…');
  try {
    const d = await api('/api/export', { format: fmt });
    text('exportNote', '已导出 ' + d.name + '（' + Math.round(d.size / 1024) + ' KB）\n' + d.path);
    toast('已导出 ' + d.name);
  } catch (err) {
    text('exportNote', '失败：' + err.message);
    toast('导出失败：' + err.message, 'err');
  }
}

/* ------------------------------------------------------------ 设置 -- */

async function saveConfig() {
  const payload = {
    requestsPerSecond: $('sRps').value,
    concurrency: $('sConc').value,
    timeout: $('sTimeout').value,
    maxRetries: $('sRetry').value,
    segmentStrategy: $('sSegment').value,
    maxRecordsPerRun: $('sMax').value,
    dateFrom: $('sFrom').value,
    dateTo: $('sTo').value,
    proxy: $('sProxy').value,
    pubmedEmail: $('sEmail').value,
    pubmedSort: $('sSort').value,
    pubmedReldays: $('sReldays').value,
  };
  try {
    const d = await api('/api/config', payload);
    text('saveNote', '已保存 ' + Object.keys(d.changed).length + ' 项');
    toast('设置已保存');
    await boot(true);
  } catch (err) {
    text('saveNote', '失败：' + err.message);
    toast('保存失败：' + err.message, 'err');
  }
}

/* ------------------------------------------------------------ 启动 -- */

function wire() {
  $('startBtn').onclick = doStart;
  $('stopBtn').onclick = doStop;
  $('planBtn').onclick = doPlan;
  $('allDs').onchange = () => {
    for (const c of document.querySelectorAll('#dsList input')) {
      c.checked = $('allDs').checked;
      c.dispatchEvent(new Event('change'));
    }
  };

  $('searchBtn').onclick = doSearch;
  $('clearBtn').onclick = clearFilters;
  $('exportResultsBtn').onclick = exportResults;

  $('rebuildBtn').onclick = doRebuild;
  $('statsBtn').onclick = loadStats;

  $('saveBtn').onclick = saveConfig;

  $('dlgClose').onclick = () => { $('dialogHost').hidden = true; };
  $('dialogHost').onclick = (e) => { if (e.target === $('dialogHost')) $('dialogHost').hidden = true; };
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('dialogHost').hidden = true; });
}

async function boot(quiet) {
  try {
    state.meta = await api('/api/meta');
  } catch (err) {
    toast('无法连接后端：' + err.message, 'err');
    return;
  }
  renderDatasets(state.meta);
  renderConfig(state.meta.config);
  $('statsBox').textContent = JSON.stringify(state.meta.stats, null, 2);
  if (!quiet) {
    $('searchInput').value = '';
    $('termInput').value = '';
    $('sortInput').value = state.meta.config.pubmedSort || '';
  }
}

wireTabs();
wire();
boot();
