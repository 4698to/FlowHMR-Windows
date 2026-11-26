/* FlowHMR admin console frontend (no build step). */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const STATUS_LABEL = {
  done: '已完成', running: '运行中', queued: '排队中', waiting_user: '待选人', uploading: '上传中',
  failed: '失败', cancelled: '已取消', cancel_requested: '取消中',
};
const ACTIVE_STATUSES = new Set(['running', 'queued', 'waiting_user', 'uploading', 'cancel_requested']);
const RATING_TAGS = { quality: '动作质量', hands: '手部', physics: '物理', camera: '相机', clothing: '服装', other: '其他' };

const state = {
  jobs: [], paused: false, stats: null, queue: null, users: [], storage: null,
  audit: [], auditOffset: 0, auditTotal: 0, tab: 'jobs', search: '', status: '',
  eventSource: null, pollTimer: null, reconnectTimer: null, refreshTimer: null, toastTimer: null,
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, className, onClick) {
  const node = el('button', className, label);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
}

function fmtAgo(ts) {
  if (!ts) return '—';
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

function fmtBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[index]}`;
}

function toast(message, isError = false) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.remove('hidden');
  node.classList.toggle('err', isError);
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => node.classList.add('hidden'), 2800);
}

function showError(error) {
  toast(error && error.message ? error.message : String(error), true);
}

async function api(path, options = {}) {
  const method = options.method || 'GET';
  const headers = { ...(options.headers || {}) };
  let body;
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    headers['X-FlowHMR-Admin'] = '1';
    body = JSON.stringify(options.body === undefined ? {} : options.body);
  }
  const response = await fetch(path, { method, headers, body, credentials: 'same-origin' });
  let data = {};
  try { data = await response.json(); } catch (error) { /* empty body */ }
  if (response.status === 401) {
    showLogin('登录已过期，请重新输入管理员密钥。');
    throw new Error('需要管理员登录');
  }
  if (!response.ok) {
    throw new Error(data.message || data.error || `请求失败（${response.status}）`);
  }
  return data;
}

function post(path, body) {
  return api(path, { method: 'POST', body });
}

/* ------------------------------------------------------------------ session */
function showLogin(message) {
  stopPolling();
  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  $('#appView').classList.add('hidden');
  $('#loginView').classList.remove('hidden');
  $('#loginError').classList.toggle('hidden', !message);
  $('#loginError').textContent = message || '';
  setTimeout(() => $('#loginKey').focus(), 30);
}

function enterApp() {
  $('#loginView').classList.add('hidden');
  $('#appView').classList.remove('hidden');
  startEvents();
  refreshActive().catch(showError);
}

async function boot() {
  try {
    const response = await fetch('/api/admin/session', { credentials: 'same-origin' });
    if (response.status === 404) {
      showLogin('管理后台未启用：启动服务时请提供 --admin-key 或设置 FLOWHMR_ADMIN_KEY。');
      return;
    }
    const data = await response.json();
    if (data.authenticated) enterApp(); else showLogin();
  } catch (error) {
    showLogin(`无法连接服务：${error.message}`);
  }
}

/* ------------------------------------------------------------------ events */
function setConnection(ok) {
  const badge = $('#connState');
  badge.textContent = ok ? '实时' : '轮询';
  badge.classList.toggle('ok', ok);
}

function startEvents() {
  if (state.eventSource) return;
  try {
    state.eventSource = new EventSource('/api/ui/events');
  } catch (error) {
    startPolling();
    return;
  }
  state.eventSource.addEventListener('connected', () => { setConnection(true); stopPolling(); });
  state.eventSource.addEventListener('update', () => scheduleRefresh());
  state.eventSource.onopen = () => { setConnection(true); stopPolling(); };
  state.eventSource.onerror = () => {
    setConnection(false);
    if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
    startPolling();
    if (!state.reconnectTimer) {
      state.reconnectTimer = setTimeout(() => {
        state.reconnectTimer = null;
        stopPolling();
        startEvents();
      }, 20000);
    }
  };
}

function startPolling() {
  setConnection(false);
  if (state.pollTimer) return;
  state.pollTimer = setInterval(() => { refreshActive(true).catch(() => {}); }, 2000);
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

function scheduleRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => { refreshActive(true).catch(() => {}); }, 600);
}

/* ------------------------------------------------------------------ views */
async function refreshActive(fromEvent = false) {
  await loadPool();
  if (state.tab === 'queue') await loadQueue();
  else if (state.tab === 'users' && !fromEvent) await loadUsers();
  else if (state.tab === 'storage' && !fromEvent) await loadStorage();
  else if (state.tab === 'audit' && !fromEvent) await loadAudit(true);
}

function switchTab(tab) {
  state.tab = tab;
  $$('#tabs button').forEach((node) => node.classList.toggle('active', node.dataset.tab === tab));
  $$('[data-panel]').forEach((node) => node.classList.toggle('hidden', node.dataset.panel !== tab));
  refreshActive().catch(showError);
}

function updatePauseBadge() {
  $('#pauseBadge').classList.toggle('hidden', !state.paused);
  $('#btnPause').textContent = state.paused ? '恢复派发' : '暂停派发';
  $('#btnPause').classList.toggle('danger', state.paused);
}

function renderKpis(pool) {
  const stats = pool.stats || {};
  const cards = [
    ['运行中', pool.running ? 1 : 0, pool.running ? `任务 ${String(pool.running).slice(0, 8)}` : '空闲'],
    ['排队中', pool.queued || 0, state.paused ? '派发已暂停' : '单 worker 串行'],
    ['今日完成', stats.done_today || 0, `累计 ${stats.total || 0} 个任务`],
    ['失败', stats.failed || 0, stats.ratings ? `${stats.ratings} 条评分` : '暂无评分'],
    ['平均评分', stats.average_rating ? `${stats.average_rating} / 5` : '—', '已完成任务均值'],
    ['引擎', pool.models_loaded ? '已加载' : '未加载', 'GPU 模型常驻'],
  ];
  const wrap = $('#kpis');
  wrap.textContent = '';
  for (const [label, value, sub] of cards) {
    const card = el('div', 'kpi');
    card.append(el('div', 'label', label), el('div', 'value', String(value)), el('div', 'sub', sub));
    wrap.append(card);
  }
}

function stars(score) {
  const value = Number(score) || 0;
  const wrap = el('span', 'stars');
  for (let index = 1; index <= 5; index += 1) {
    wrap.append(el('span', index <= value ? '' : 'off', index <= value ? '★' : '☆'));
  }
  return wrap;
}

function statusNode(status) {
  const wrap = el('span', `status s-${status || 'unknown'}`);
  wrap.textContent = STATUS_LABEL[status] || status || '—';
  return wrap;
}

function fileCell(job) {
  const wrap = el('div', 'cell-file');
  wrap.append(el('b', '', job.filename || job.id));
  wrap.append(el('span', '', job.id));
  return wrap;
}

function progressCell(value) {
  const wrap = el('div', 'progress');
  const bar = el('span', 'bar');
  const fill = el('i');
  fill.style.width = `${Math.max(0, Math.min(100, Number(value) || 0))}%`;
  bar.append(fill);
  wrap.append(bar, el('span', '', `${Number(value) || 0}%`));
  return wrap;
}

function matchesJob(job) {
  if (state.status === 'active' && !ACTIVE_STATUSES.has(job.status)) return false;
  if (state.status && state.status !== 'active' && job.status !== state.status) return false;
  const query = state.search.trim().toLowerCase();
  if (!query) return true;
  return [job.filename, job.owner_name, job.owner_id, job.id]
    .some((value) => String(value || '').toLowerCase().includes(query));
}

function renderJobs() {
  const body = $('#jobTable tbody');
  body.textContent = '';
  const rows = state.jobs.filter(matchesJob);
  $('#jobCount').textContent = `${rows.length} / ${state.jobs.length} 个任务`;
  for (const job of rows) {
    const tr = el('tr');
    const statusTd = el('td');
    statusTd.append(statusNode(job.status));
    if (job.person_count > 1) {
      statusTd.append(el('div', 'muted', `${job.person_done || 0}/${job.person_count} 人`));
    }
    tr.append(statusTd);
    const fileTd = el('td');
    fileTd.append(fileCell(job));
    tr.append(fileTd);
    const owner = el('td');
    owner.append(el('div', '', job.owner_name || job.owner_id || '—'));
    owner.append(el('div', 'muted', job.owner_id || ''));
    tr.append(owner);
    tr.append(el('td', '', job.client_ip || '—'));
    tr.append(el('td', 'num', String(job.num_frames || '—')));
    const ratingTd = el('td');
    if (job.rating && job.rating.score) {
      ratingTd.append(stars(job.rating.score));
      if (job.rating.comment) ratingTd.title = job.rating.comment;
    } else {
      ratingTd.append(el('span', 'muted', '未评分'));
    }
    tr.append(ratingTd);
    const progressTd = el('td', 'num');
    progressTd.append(progressCell(job.progress));
    tr.append(progressTd);
    tr.append(el('td', 'num', job.queue_position ? String(job.queue_position) : '—'));
    const created = el('td');
    created.append(el('div', '', fmtAgo(job.created)));
    created.append(el('div', 'muted', fmtTime(job.created)));
    tr.append(created);

    const actions = el('td', 'actions-col');
    const wrap = el('div', 'row-actions');
    wrap.append(button('详情', 'mini ghost', () => openDrawer(job)));
    if (job.status === 'done') {
      // Deep link into the main viewer; the admin cookie lets it load another owner's task.
      const view = el('a', 'btn', '3D 浏览');
      view.href = `/?job=${encodeURIComponent(job.id)}`;
      view.target = '_blank';
      view.rel = 'noopener';
      view.title = '在主界面 3D 视口查看该任务';
      wrap.append(view);
    }
    if (['done', 'failed', 'cancelled'].includes(job.status)) {
      wrap.append(button('下载', 'btn', () => openDownloads(job)));
    }
    if (['done', 'failed', 'cancelled'].includes(job.status)) {
      wrap.append(button('重跑', 'mini ghost', () => rerunJob(job)));
    }
    wrap.append(button('删除', 'mini ghost danger', () => deleteJob(job)));
    actions.append(wrap);
    tr.append(actions);
    body.append(tr);
  }
}

async function loadPool() {
  const data = await api('/api/admin/pool');
  state.jobs = data.jobs || [];
  state.paused = !!data.paused;
  state.stats = data.stats || null;
  renderKpis(data);
  renderJobs();
  updatePauseBadge();
}

async function rerunJob(job) {
  try {
    await post(`/api/jobs/${encodeURIComponent(job.id)}/rerun`, {});
    toast('已提交重跑任务');
    await loadPool();
  } catch (error) {
    showError(error);
  }
}

async function deleteJob(job) {
  const word = String(job.filename || job.id).slice(-6);
  const ok = await confirmDanger({
    title: '删除任务',
    message: `将删除任务「${job.filename || job.id}」及其全部产物。\n运行中的任务会在当前阶段的安全点停止后再删除。`,
    word,
    okText: '删除',
  });
  if (!ok) return;
  try {
    const result = await api(`/api/admin/jobs/${encodeURIComponent(job.id)}`, { method: 'DELETE' });
    if (result.deferred && result.deferred.length) {
      toast(`已请求停止，将在安全点后删除（${result.deferred.length} 个任务）`);
    } else {
      toast(`已删除 ${result.deleted.length} 个任务`);
    }
    await loadPool();
  } catch (error) {
    showError(error);
  }
}

/* ------------------------------------------------------------------ queue */
async function loadQueue() {
  const data = await api('/api/admin/queue');
  state.queue = data;
  state.paused = !!data.paused;
  updatePauseBadge();
  const running = $('#queueRunning');
  running.textContent = '';
  if (data.running) {
    const card = el('div', 'running-card');
    const info = el('div');
    info.append(el('div', 'title', data.running.filename || data.running.job_id));
    info.append(el('div', 'meta',
      `${data.running.owner_name || data.running.owner} · ${STATUS_LABEL[data.running.status] || data.running.status} · ${data.running.progress || 0}%`));
    card.append(info);
    card.append(button('强制取消', 'mini danger', () => cancelRunning(data.running.job_id)));
    running.append(card);
  } else {
    const card = el('div', 'running-card');
    card.append(el('span', 'muted', state.paused ? '派发已暂停，当前没有运行中的任务。' : '当前没有运行中的任务。'));
    running.append(card);
  }
  $('#queueLimits').textContent = `单用户上限 ${data.limits.owner} · 总队上限 ${data.limits.total}`;
  const body = $('#queueTable tbody');
  body.textContent = '';
  if (!data.items.length) {
    const tr = el('tr');
    const td = el('td', 'muted', '队列为空');
    td.colSpan = 6;
    tr.append(td);
    body.append(tr);
    return;
  }
  for (const item of data.items) {
    const tr = el('tr');
    tr.append(el('td', 'num', String(item.position)));
    const jobTd = el('td');
    jobTd.append(el('div', '', item.filename || item.job_id));
    jobTd.append(el('div', 'muted', item.job_id));
    tr.append(jobTd);
    tr.append(el('td', '', item.owner_name || item.owner));
    tr.append(el('td', 'num', String(item.priority)));
    const statusTd = el('td');
    statusTd.append(statusNode(item.status || 'queued'));
    tr.append(statusTd);
    const actions = el('td', 'actions-col');
    const wrap = el('div', 'row-actions');
    wrap.append(button('置顶', 'mini ghost', () => queueAction('promote', item.job_id)));
    wrap.append(button('降级', 'mini ghost', () => queueAction('demote', item.job_id)));
    wrap.append(button('取消', 'mini ghost danger', () => cancelQueued(item)));
    actions.append(wrap);
    tr.append(actions);
    body.append(tr);
  }
}

async function queueAction(action, jobId, body = {}) {
  try {
    await post('/api/admin/queue', { action, job_id: jobId, ...body });
    toast(action === 'promote' ? '已置顶' : action === 'demote' ? '已降级' : '操作完成');
    await refreshActive(true);
  } catch (error) {
    showError(error);
  }
}

async function cancelQueued(item) {
  const ok = await confirmDanger({
    title: '取消排队任务',
    message: `将把「${item.filename || item.job_id}」移出队列并标记为已取消。`,
    okText: '取消任务',
  });
  if (ok) await queueAction('cancel', item.job_id);
}

async function cancelRunning(jobId) {
  const ok = await confirmDanger({
    title: '强制取消运行中任务',
    message: '任务会在当前阶段的安全点退出，可能需要等待几十秒。',
    word: '取消',
    okText: '强制取消',
  });
  if (ok) await queueAction('cancel_running', jobId);
}

async function togglePause() {
  try {
    const data = await post('/api/admin/queue', { action: state.paused ? 'resume' : 'pause' });
    state.paused = !!data.paused;
    updatePauseBadge();
    toast(state.paused ? '已暂停派发（运行中的任务会继续）' : '已恢复派发');
    await refreshActive(true);
  } catch (error) {
    showError(error);
  }
}

async function clearQueue() {
  const ok = await confirmDanger({
    title: '清空排队',
    message: '所有排队中的任务都会被移除并标记为已取消；运行中的任务不受影响。',
    word: '清空',
    okText: '清空队列',
  });
  if (!ok) return;
  try {
    await post('/api/admin/queue', { action: 'clear' });
    toast('已清空排队');
    await refreshActive(true);
  } catch (error) {
    showError(error);
  }
}

/* ------------------------------------------------------------------ users */
async function loadUsers() {
  const data = await api('/api/admin/users');
  state.users = data.users || [];
  $('#userCount').textContent = `${state.users.length} 位用户`;
  const body = $('#userTable tbody');
  body.textContent = '';
  for (const user of state.users) {
    const tr = el('tr');
    const nameTd = el('td');
    const nameInput = el('input');
    nameInput.type = 'text';
    nameInput.value = user.name || '';
    nameInput.maxLength = 24;
    nameInput.style.width = '130px';
    if (user.platform) {
      nameInput.readOnly = true;
      nameInput.title = '昵称由参考库平台管理';
    }
    nameTd.append(nameInput);
    tr.append(nameTd);
    tr.append(el('td', 'muted', user.uid));
    tr.append(el('td', '', user.platform ? (user.code || '—') : '本地'));
    const rolesTd = el('td');
    if (user.platform) {
      rolesTd.textContent = (user.roles || []).join('、') || '—';
    } else {
      rolesTd.append(el('span', 'muted', '—'));
    }
    tr.append(rolesTd);
    const ipTd = el('td');
    ipTd.append(el('div', '', user.last_ip || '—'));
    if (user.shared_ips && user.shared_ips.length) {
      ipTd.append(el('div', 'muted', `与其他账号共享：${user.shared_ips.join(', ')}`));
    }
    tr.append(ipTd);
    const seen = el('td');
    seen.append(el('div', '', fmtAgo(user.last_seen)));
    seen.append(el('div', 'muted', fmtTime(user.last_seen)));
    tr.append(seen);
    tr.append(el('td', 'num', String(user.jobs || 0)));
    tr.append(el('td', 'num', String(user.queued || 0)));
    tr.append(el('td', 'num', fmtBytes(user.disk_bytes)));
    const statusTd = el('td');
    statusTd.append(el('span', `badge ${user.disabled ? '' : 'ok'}`, user.disabled ? '已禁用' : '正常'));
    tr.append(statusTd);
    const actions = el('td', 'actions-col');
    const wrap = el('div', 'row-actions');
    if (!user.platform) {
      wrap.append(button('保存昵称', 'mini ghost', () => updateUser(user.uid, { name: nameInput.value })));
    }
    wrap.append(button(user.disabled ? '启用' : '禁用', `mini ghost ${user.disabled ? '' : 'danger'}`,
      () => toggleUserDisabled(user)));
    if (user.platform) {
      wrap.append(button('强制下线', 'mini ghost danger', () => forceLogout(user)));
    }
    actions.append(wrap);
    tr.append(actions);
    body.append(tr);
  }
}

async function updateUser(uid, changes) {
  try {
    await api(`/api/admin/users/${encodeURIComponent(uid)}`, { method: 'PATCH', body: changes });
    toast('已更新用户');
    await loadUsers();
  } catch (error) {
    showError(error);
  }
}

async function toggleUserDisabled(user) {
  if (!user.disabled) {
    const ok = await confirmDanger({
      title: '禁用用户',
      message: `禁用「${user.name || user.uid}」后，其提交与重跑任务会返回 403；已完成任务保留。`,
      okText: '禁用',
    });
    if (!ok) return;
  }
  await updateUser(user.uid, { disabled: !user.disabled });
}

async function forceLogout(user) {
  const ok = await confirmDanger({
    title: '强制下线',
    message: `将立即注销「${user.name || user.uid}」在所有浏览器上的登录会话；小程序端不受影响。`,
    okText: '强制下线',
  });
  if (!ok) return;
  try {
    const result = await post(`/api/admin/users/${encodeURIComponent(user.uid)}/logout`);
    toast(`已注销 ${result.sessions || 0} 个会话`);
    await loadUsers();
  } catch (error) {
    showError(error);
  }
}

/* ------------------------------------------------------------------ storage */
async function loadStorage() {
  const data = await api('/api/admin/storage');
  state.storage = data;
  const summary = $('#storageSummary');
  summary.textContent = '';
  const cards = [
    ['总占用', fmtBytes(data.total_bytes)],
    ['任务产物', fmtBytes(data.job_bytes)],
    ['索引与日志', fmtBytes(data.meta_bytes)],
    ['任务数量', String(data.job_count)],
    ['孤立目录', String((data.orphans || []).length)],
  ];
  for (const [label, value] of cards) {
    const block = el('div');
    block.append(el('div', 'label', label), el('div', 'value', value));
    summary.append(block);
  }
  const ownerBody = $('#storageOwnerTable tbody');
  ownerBody.textContent = '';
  for (const row of data.by_owner || []) {
    const tr = el('tr');
    tr.append(el('td', '', `${row.name} (${row.owner})`));
    tr.append(el('td', 'num', String(row.jobs)));
    tr.append(el('td', 'num', fmtBytes(row.bytes)));
    ownerBody.append(tr);
  }
  const statusBody = $('#storageStatusTable tbody');
  statusBody.textContent = '';
  for (const [status, row] of Object.entries(data.by_status || {})) {
    const tr = el('tr');
    tr.append(el('td', '', STATUS_LABEL[status] || status));
    tr.append(el('td', 'num', String(row.jobs)));
    tr.append(el('td', 'num', fmtBytes(row.bytes)));
    statusBody.append(tr);
  }
  const orphanBody = $('#storageOrphanTable tbody');
  orphanBody.textContent = '';
  if (!(data.orphans || []).length) {
    const tr = el('tr');
    const td = el('td', 'muted', '无');
    td.colSpan = 2;
    tr.append(td);
    orphanBody.append(tr);
  }
  for (const orphan of data.orphans || []) {
    const tr = el('tr');
    tr.append(el('td', '', orphan.id));
    tr.append(el('td', 'num', fmtBytes(orphan.bytes)));
    orphanBody.append(tr);
  }
}

async function cleanup(action) {
  const body = { action };
  if (action === 'finished_older') {
    body.days = Number($('#cleanupDays').value) || 30;
  }
  const ok = await confirmDanger({
    title: '清理存储',
    message: action === 'orphans'
      ? '将删除工作目录中不属于任何任务的文件夹。'
      : action === 'legacy'
        ? '将删除全部历史（升级前）任务及其产物。'
        : `将删除 ${body.days} 天前完成的全部任务及其产物。`,
    word: '清理',
    okText: '执行清理',
  });
  if (!ok) return;
  try {
    const result = await post('/api/admin/storage/cleanup', body);
    toast(`已删除 ${(result.deleted || []).length} 个任务，释放 ${fmtBytes(result.freed_bytes)}`);
    await loadStorage();
    await loadPool();
  } catch (error) {
    showError(error);
  }
}

/* ------------------------------------------------------------------ audit */
async function loadAudit(reset) {
  if (reset) state.auditOffset = 0;
  const params = new URLSearchParams({
    limit: '100',
    offset: String(state.auditOffset),
    action: $('#auditAction').value.trim(),
    result: $('#auditResult').value,
  });
  const data = await api(`/api/admin/audit?${params.toString()}`);
  state.audit = reset ? data.entries : state.audit.concat(data.entries);
  state.auditOffset += data.entries.length;
  state.auditTotal = data.total;
  $('#auditCount').textContent = `显示 ${state.audit.length} / ${state.auditTotal} 条`;
  $('#btnAuditMore').classList.toggle('hidden', state.audit.length >= state.auditTotal);
  const body = $('#auditTable tbody');
  body.textContent = '';
  for (const row of state.audit) {
    const tr = el('tr');
    tr.append(el('td', '', fmtTime(row.ts)));
    tr.append(el('td', '', row.action || '—'));
    tr.append(el('td', 'muted', row.target || '—'));
    tr.append(el('td', '', row.result || '—'));
    tr.append(el('td', 'muted', row.ip || '—'));
    tr.append(el('td', 'muted', row.detail || ''));
    body.append(tr);
  }
}

/* ------------------------------------------------------------------ drawer */
function openDrawer(job) {
  $('#drawerTitle').textContent = job.filename || job.id;
  const body = $('#drawerBody');
  body.textContent = '';

  const kv = el('dl', 'kv');
  const rows = [
    ['任务 ID', job.id],
    ['状态', STATUS_LABEL[job.status] || job.status],
    ['属主', `${job.owner_name || ''} (${job.owner_id || ''})`],
    ['来源 IP', job.client_ip || '—'],
    ['帧数', job.num_frames || '—'],
    ['创建时间', fmtTime(job.created)],
    ['完成时间', fmtTime(job.finished)],
    ['排队位置', job.queue_position || '—'],
    ['评分', job.rating && job.rating.score ? `${job.rating.score} / 5` : '未评分'],
    ['评分标签', (job.rating && job.rating.tags || []).map((tag) => RATING_TAGS[tag] || tag).join('、') || '—'],
    ['备注', (job.rating && job.rating.comment) || '—'],
  ];
  for (const [key, value] of rows) {
    kv.append(el('dt', '', key));
    kv.append(el('dd', '', String(value)));
  }
  body.append(kv);

  if (job.stages && job.stages.length) {
    body.append(el('h3', '', '阶段进度'));
    const list = el('div', 'stage-list');
    for (const stage of job.stages) {
      const row = el('div', 'stage-row');
      row.append(el('span', '', stage.label || stage.key || '阶段'));
      const bar = el('span', 'bar');
      const fill = el('i');
      fill.style.width = `${Math.max(0, Math.min(100, Number(stage.progress) || 0))}%`;
      bar.append(fill);
      row.append(bar);
      row.append(el('span', 'muted', `${STATUS_LABEL[stage.status] || stage.status || ''} ${stage.progress || 0}%`));
      list.append(row);
    }
    body.append(list);
  }

  const thumbs = job.person_thumbs || [];
  if (job.id) {
    body.append(el('h3', '', '画面缩略图'));
    const wrap = el('div', 'thumbs');
    if (thumbs.length) {
      for (const person of thumbs) {
        const img = el('img');
        img.src = `/api/pool/${encodeURIComponent(job.id)}/thumbnail.jpg?person=${encodeURIComponent(person)}`;
        img.alt = `人物 ${person}`;
        img.loading = 'lazy';
        wrap.append(img);
      }
    } else {
      const img = el('img');
      img.src = `/api/pool/${encodeURIComponent(job.id)}/thumbnail.jpg`;
      img.alt = '缩略图';
      img.loading = 'lazy';
      wrap.append(img);
    }
    body.append(wrap);
  }

  body.append(el('h3', '', '生成参数'));
  const pre = el('pre', 'json');
  pre.textContent = JSON.stringify(job.params || {}, null, 2);
  body.append(pre);

  if (job.error) {
    body.append(el('h3', '', '错误'));
    body.append(el('pre', 'json', job.error));
  }

  $('#drawer').classList.remove('hidden');
  $('#drawerScrim').classList.remove('hidden');
  $('#drawer').setAttribute('aria-hidden', 'false');
}

function closeDrawer() {
  $('#drawer').classList.add('hidden');
  $('#drawerScrim').classList.add('hidden');
  $('#drawer').setAttribute('aria-hidden', 'true');
}

/* ------------------------------------------------------------------ confirm */
function confirmDanger({ title, message, word = '', okText = '确认' }) {
  return new Promise((resolve) => {
    const dialog = $('#confirmDialog');
    const input = $('#confirmWordInput');
    const wrap = $('#confirmWordWrap');
    $('#confirmTitle').textContent = title;
    $('#confirmMessage').textContent = message;
    wrap.classList.toggle('hidden', !word);
    $('#confirmWordHint').textContent = word;
    input.value = '';
    const okButton = $('#confirmOk');
    okButton.textContent = okText;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      dialog.close();
      resolve(value);
    };
    okButton.onclick = () => {
      if (word && input.value !== word) {
        input.classList.add('bad');
        input.focus();
        return;
      }
      finish(true);
    };
    $('#confirmCancel').onclick = () => finish(false);
    dialog.oncancel = (event) => { event.preventDefault(); finish(false); };
    dialog.showModal();
    setTimeout(() => { if (word) input.focus(); else okButton.focus(); }, 30);
  });
}

/* ------------------------------------------------------------------ downloads */
async function openDownloads(job) {
  const dialog = $('#downloadDialog');
  const list = $('#downloadOptions');
  list.textContent = '';
  $('#downloadTarget').textContent = `${job.filename || job.id} · ${job.id}`;
  let detail = job;
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(job.id)}`, { credentials: 'same-origin' });
    if (response.status === 401) {
      showLogin('登录已过期，请重新输入管理员密钥。');
      return;
    }
    if (response.ok) detail = await response.json();
  } catch (error) { /* fall back to the pool entry */ }
  const done = detail.status === 'done';
  const base = `/api/jobs/${encodeURIComponent(job.id)}`;
  const formats = [
    { label: 'NPZ（SMPL-H 动作）', href: `${base}/npz`, ok: done },
    {
      label: 'FBX（FlowHMR 源骨架）', href: `${base}/fbx?mesh=1&first_pose=none`, pose: true,
      ok: done && Boolean(detail.has_fbx),
    },
    { label: 'VMD（MMD 动作）', href: `${base}/vmd`, ok: done && Boolean(detail.has_vmd) },
    { label: 'VRMA（VRM 动画）', href: `${base}/vrma`, ok: done && Boolean(detail.has_vrma) },
    { label: '预览视频（30fps）', href: `${base}/video?source=preview`, ok: Boolean(detail.has_preview_video) },
    { label: '原视频', href: `${base}/video?source=original`, ok: true },
  ];
  const available = formats.filter((format) => format.ok);
  for (const format of available) {
    const link = el('a', 'btn', format.label);
    link.href = format.href;
    link.download = '';
    list.append(link);
    if (format.pose) list.append(firstPoseRow(link, base));
  }
  if (!available.length) list.append(el('p', 'muted', '该任务暂无可下载产物。'));
  dialog.showModal();
}

function firstPoseRow(link, base) {
  const row = el('label', 'download-option');
  row.append(el('span', '', 'FBX 参考姿势'));
  const select = el('select');
  for (const [value, text] of [['none', '不添加'], ['t', 'T Pose'], ['a', 'A Pose']]) {
    const option = el('option', '', text);
    option.value = value;
    select.append(option);
  }
  select.addEventListener('change', () => {
    link.href = `${base}/fbx?mesh=1&first_pose=${select.value}`;
  });
  row.append(select);
  return row;
}

/* ------------------------------------------------------------------ wiring */
function wire() {
  $('#loginForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const key = $('#loginKey').value;
    if (!key) return;
    $('#loginError').classList.add('hidden');
    try {
      await post('/api/admin/login', { key });
      $('#loginKey').value = '';
      enterApp();
    } catch (error) {
      $('#loginError').textContent = error.message;
      $('#loginError').classList.remove('hidden');
    }
  });
  $('#btnRefresh').addEventListener('click', () => refreshActive().catch(showError));
  $('#btnPause').addEventListener('click', togglePause);
  $('#btnClear').addEventListener('click', clearQueue);
  $('#btnLogout').addEventListener('click', async () => {
    try { await post('/api/admin/logout'); } catch (error) { /* session may already be gone */ }
    showLogin();
  });
  $('#tabs').addEventListener('click', (event) => {
    const target = event.target.closest('button[data-tab]');
    if (target) switchTab(target.dataset.tab);
  });
  let searchTimer = null;
  $('#jobSearch').addEventListener('input', (event) => {
    state.search = event.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(renderJobs, 150);
  });
  $('#jobStatus').addEventListener('change', (event) => {
    state.status = event.target.value;
    renderJobs();
  });
  $('#drawerClose').addEventListener('click', closeDrawer);
  $('#drawerScrim').addEventListener('click', closeDrawer);
  $('#downloadClose').addEventListener('click', () => $('#downloadDialog').close());
  $('#downloadOptions').addEventListener('click', (event) => {
    if (event.target.closest('a')) $('#downloadDialog').close();
  });
  $('#downloadDialog').addEventListener('click', (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
  $('#btnCleanupOld').addEventListener('click', () => cleanup('finished_older'));
  $('#btnCleanupLegacy').addEventListener('click', () => cleanup('legacy'));
  $('#btnCleanupOrphans').addEventListener('click', () => cleanup('orphans'));
  $('#btnAuditFilter').addEventListener('click', () => loadAudit(true).catch(showError));
  $('#auditAction').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') loadAudit(true).catch(showError);
  });
  $('#auditResult').addEventListener('change', () => loadAudit(true).catch(showError));
  $('#btnAuditMore').addEventListener('click', () => loadAudit(false).catch(showError));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('#drawer').classList.contains('hidden')) closeDrawer();
  });
}

wire();
boot();
