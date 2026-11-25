// Job history. Keep each row and its thumbnail mounted while progress changes.
import { $, show, el, formatRelative, statusLabel, jobLabel } from "./utils.js";
import { localize, message as m, setAttr, setText, t } from "./i18n.js";
import * as api from "./api.js";

const ACTIVE = new Set(["queued", "running", "waiting_user", "cancel_requested", "uploading"]);
const STOP = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>';
const RERUN = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 9.4h5M3 19.6v-5h5m-5 5l3-3a8.2 8.2 0 0014-3.7M4 9.9a8.2 8.2 0 0113.8-3.7L21 9.4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

export function initHistory({ onOpen, onRerun, onDeleted, onUpdated }) {
  const list = $("historyList");
  const more = $("historyMore");
  const deleteSelected = $("historyDeleteSelected"), deleteAll = $("historyDeleteAll");
  const selected = new Set(), rows = new Map();
  const tabMine = $("historyTabMine"), tabPool = $("historyTabPool");
  let view = "mine";
  let current = null, busy = null, refreshVersion = 0, refreshing = false, pendingRefresh = false;
  let deletable = 0, through = null, nextCursor = null, loadingMore = false;
  let polling = true, timer = null;

  function updateActions() {
    show("historyActions", view === "mine");
    $("historySelectedCount").textContent = selected.size;
    deleteSelected.disabled = busy !== null || selected.size === 0;
    deleteAll.disabled = busy !== null || deletable === 0;
    show(more, view === "mine" && !!nextCursor);
    more.disabled = loadingMore || busy !== null;
    setText(more, m(loadingMore ? "history.loading_more" : "history.more"));
  }

  function setView(next) {
    if (view === next) return;
    view = next;
    selected.clear();
    for (const entry of rows.values()) entry.row.remove();
    rows.clear();
    through = nextCursor = null;
    deletable = 0;
    clearTimeout(timer);
    tabMine.classList.toggle("active", view === "mine");
    tabPool.classList.toggle("active", view === "pool");
    setAttr(tabMine, "aria-selected", view === "mine");
    setAttr(tabPool, "aria-selected", view === "pool");
    setError("");
    setText($("historySummary"), "");
    show("historyEmpty", false);
    updateActions();
    void refresh();
  }

  tabMine.addEventListener("click", () => setView("mine"));
  tabPool.addEventListener("click", () => setView("pool"));

  function setError(msg) {
    setText($("historyError"), msg);
    show("historyError", !!msg);
  }

  function createRow(job) {
    const row = el("div", "run"), check = el("input", "run-select"), open = el("button", "run-open");
    const thumb = el("span", "run-thumb"), image = el("img"), info = el("div", "run-info");
    const name = el("div", "run-name", job.filename), sub = el("div", "run-sub");
    const tag = el("span", "badge"), action = el("button", "icon-btn");
    const { id } = job;
    const entry = { row, check, open, image, name, sub, tag, action, status: null, active: null, person: undefined, failedThumb: false };
    row.dataset.job = id;
    check.type = "checkbox";
    check.addEventListener("change", () => {
      if (check.checked) selected.add(id); else selected.delete(id);
      updateActions();
    });
    open.type = action.type = "button";
    open.addEventListener("click", () => onOpen(id));
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.addEventListener("load", () => { entry.failedThumb = false; thumb.classList.add("ready"); });
    image.addEventListener("error", () => { entry.failedThumb = true; thumb.classList.remove("ready"); });
    action.addEventListener("click", async () => {
      if (busy !== null) return;
      const stopping = entry.active;
      busy = stopping ? `stop:${id}` : id;
      setError("");
      void refresh();
      try { await (stopping ? api.cancelJob(id) : onRerun(id)); }
      catch (err) { setError(err.message ? err : m(stopping ? "job.stop_failed" : "job.rerun_failed")); }
      finally { busy = null; void refresh(); }
    });
    info.append(name, sub);
    thumb.append(image);
    open.append(thumb, info, tag);
    row.append(check, open, action);
    rows.set(id, entry);
    return entry;
  }

  function updateRow(entry, job, now) {
    const { row, check, open, image, name, sub, tag, action } = entry;
    const active = ACTIVE.has(job.status);
    const person = job.selected_person ?? null;
    if (entry.person !== person || (entry.status !== job.status && entry.failedThumb)) {
      entry.person = person;
      image.src = `${api.thumbnailUrl(job.id)}?person=${person ?? ""}&stage=${job.status}`;
    }
    const label = () => jobLabel(job);
    setText(name, label);
    setAttr(open, "title", label);
    setAttr(check, "aria-label", m("history.check", { name: label }));
    if (entry.active !== active) action.innerHTML = active ? STOP : RERUN;
    entry.status = job.status;
    entry.active = active;
    row.classList.toggle("current", job.id === current);
    check.checked = selected.has(job.id);
    check.disabled = active || busy !== null;
    const summary = () => job.recovered ? t("history.recovered") : [
      ...(job.source === 'mcp' ? ['MCP'] : []),
      formatRelative(job.created, now),
      ...(job.params.postprocess ? [
        t(job.params.correction_preset === "strong" ? "correction.lock_strong" : "correction.lock_normal"),
        t(job.params.correction_floor_m == null ? "correction.floor_auto" : "correction.floor_manual"),
      ] : []),
      ...(job.params.crossing_opt ? [
        t(job.crossing_opt_used === false ? "crossing.fallback" : "crossing.active"),
      ] : []),
      `CFG ${job.params.cfg_scale}`, t("history.step_count", { count: job.params.steps }),
    ].join(" · ");
    setText(sub, summary);
    setAttr(sub, "title", summary);
    tag.className = `badge ${job.status}`;
    setText(tag, job.status === "running" ? `${job.progress}%` : () => statusLabel(job.status));
    const actionLabel = m(active ? (job.status === "cancel_requested" ? "job.cancel_requested" : "history.stop") : "progress.rerun");
    setAttr(action, "title", actionLabel);
    setAttr(action, "aria-label", () => `${localize(actionLabel)}: ${label()}`);
    action.disabled = busy !== null || job.status === "cancel_requested";
    action.classList.toggle("spin", busy === job.id);
  }

  function wireAction(entry, id) {
    entry.action.addEventListener("click", async () => {
      if (busy !== null) return;
      const stopping = entry.active;
      busy = stopping ? `stop:${id}` : id;
      setError("");
      void refresh();
      try { await (stopping ? api.cancelJob(id) : onRerun(id)); }
      catch (err) { setError(err.message ? err : m(stopping ? "job.stop_failed" : "job.rerun_failed")); }
      finally { busy = null; void refresh(); }
    });
  }

  function createPoolRow(job) {
    const row = el("div", "run"), open = el("button", "run-open");
    const thumb = el("span", "run-thumb"), image = el("img"), info = el("div", "run-info");
    const name = el("div", "run-name", job.filename), sub = el("div", "run-sub");
    const tag = el("span", "badge"), action = el("button", "icon-btn");
    const { id } = job;
    const entry = { row, check: null, open, image, name, sub, tag, action, pool: true,
                    mine: !!job.mine, status: null, active: null, person: undefined, failedThumb: false };
    row.dataset.job = id;
    open.type = action.type = "button";
    open.addEventListener("click", () => { if (entry.mine) onOpen(id); });
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.addEventListener("load", () => { entry.failedThumb = false; thumb.classList.add("ready"); });
    image.addEventListener("error", () => { entry.failedThumb = true; thumb.classList.remove("ready"); });
    wireAction(entry, id);
    info.append(name, sub);
    thumb.append(image);
    open.append(thumb, info, tag);
    row.append(open, action);
    rows.set(id, entry);
    return entry;
  }

  function updatePoolRow(entry, job, now) {
    const { row, open, image, name, sub, tag, action } = entry;
    const active = ACTIVE.has(job.status);
    const person = job.person_thumbs?.[0] ?? null;
    if (entry.person !== person) {
      entry.person = person;
      image.src = api.poolThumbnailUrl(job.id, person);
    }
    entry.mine = !!job.mine;
    setText(name, job.filename);
    setAttr(open, "title", entry.mine ? job.filename : `${job.filename} — ${m("pool.readonly")}`);
    open.disabled = !entry.mine;
    row.classList.toggle("current", job.id === current);
    const bits = [job.owner_name || job.owner_id, formatRelative(job.created, now)];
    if (job.queue_position) bits.push(t("pool.queue_position", { count: job.queue_position }));
    if (job.rating?.score) bits.push(`★ ${job.rating.score}`);
    const summary = bits.filter(Boolean).join(" · ");
    setText(sub, summary);
    setAttr(sub, "title", summary);
    tag.className = `badge ${job.status}`;
    setText(tag, job.status === "running" ? `${job.progress}%` : () => statusLabel(job.status));
    if (entry.active !== active) action.innerHTML = active ? STOP : RERUN;
    entry.status = job.status;
    entry.active = active;
    show(action, entry.mine);
    action.disabled = busy !== null || job.status === "cancel_requested";
    const actionLabel = m(active ? (job.status === "cancel_requested" ? "job.cancel_requested" : "history.stop") : "progress.rerun");
    setAttr(action, "title", actionLabel);
    setAttr(action, "aria-label", () => `${localize(actionLabel)}: ${job.filename}`);
  }

  function applyPool(data) {
    const { jobs, now } = data;
    const visible = new Set(jobs.map((job) => job.id));
    for (const [id, entry] of rows) {
      if (!visible.has(id)) { entry.row.remove(); rows.delete(id); }
    }
    setText($("historySummary"), jobs.length ? `· ${jobs.length}` : "");
    show("historyEmpty", jobs.length === 0);
    jobs.forEach((job, i) => {
      const entry = rows.get(job.id) ?? createPoolRow(job);
      updatePoolRow(entry, job, now);
      if (list.children[i] !== entry.row) list.insertBefore(entry.row, list.children[i] ?? null);
    });
    updateActions();
    return jobs;
  }

  function applyWindow(data) {
    const { jobs, now } = data;
    nextCursor = data.next_cursor;
    const visible = new Set(jobs.map((job) => job.id));
    for (const id of selected) if (!visible.has(id)) selected.delete(id);
    for (const [id, entry] of rows) {
      if (!visible.has(id)) { entry.row.remove(); rows.delete(id); }
    }
    deletable = data.deletable_count;
    const total = data.total ?? jobs.length;
    setText($("historySummary"), total ? `· ${jobs.length < total ? `${jobs.length}/` : ""}${total}` : "");
    show("historyEmpty", jobs.length === 0);
    jobs.forEach((job, i) => {
      const entry = rows.get(job.id) ?? createRow(job);
      updateRow(entry, job, now);
      if (list.children[i] !== entry.row) list.insertBefore(entry.row, list.children[i] ?? null);
    });
    updateActions();
    onUpdated?.(jobs, polling);
    return jobs;
  }

  async function refresh() {
    if (loadingMore || refreshing) { pendingRefresh = true; return; }
    clearTimeout(timer);
    refreshing = true;
    const version = ++refreshVersion;
    try {
      if (view === "pool") {
        const data = await api.getPool();
        if (version !== refreshVersion) return;
        applyPool(data);
        return;
      }
      const data = await api.getJobs({ through });
      if (version !== refreshVersion) return data.jobs;
      const jobs = applyWindow(data);
      return jobs;
    } catch (error) {
      setError(m('history.load_failed', { detail: error }));
    } finally {
      refreshing = false;
      if (pendingRefresh && !loadingMore) { pendingRefresh = false; void refresh(); }
      else scheduleRefresh();
    }
  }

  async function loadMore() {
    if (view !== "mine" || !nextCursor || loadingMore || busy !== null) return;
    setError("");
    loadingMore = true;
    clearTimeout(timer);
    refreshVersion++;
    updateActions();
    try {
      const page = await api.getJobs({ before: nextCursor });
      // Reconcile the entire opened window so newly inserted or deleted jobs
      // cannot make history skip a row across the pagination boundary.
      const boundary = page.cursor ?? through;
      const data = await api.getJobs({ through: boundary });
      through = boundary;
      applyWindow(data);
    } catch (error) {
      setError(m("history.load_failed", { detail: error }));
    } finally {
      loadingMore = false;
      updateActions();
      if (pendingRefresh) { pendingRefresh = false; void refresh(); }
      else scheduleRefresh();
    }
  }

  more.addEventListener("click", loadMore);
  function scheduleRefresh() {
    clearTimeout(timer);
    if (polling && !refreshing && !loadingMore) timer = setTimeout(refresh, 2000);
  }
  window.addEventListener('flowhmr:server-update', event => {
    if (event.detail.kind === 'resync') {
      polling = false;
      clearTimeout(timer);
    }
    if (['jobs', 'resync'].includes(event.detail.kind)) void refresh();
  });
  window.addEventListener('flowhmr:sync-disconnected', () => {
    if (polling) return;
    polling = true;
    scheduleRefresh();
  });
  window.addEventListener('pagehide', () => clearTimeout(timer));
  $("historyRefresh").addEventListener("click", refresh);
  deleteSelected.addEventListener("click", async () => {
    const ids = [...selected];
    if (ids.length && window.confirm(t("history.delete_confirm", { count: ids.length }))) await removeRuns(ids);
  });
  deleteAll.addEventListener("click", async () => {
    if (window.confirm(t("history.clear_confirm"))) await removeRuns();
  });

  async function removeRuns(ids) {
    setError("");
    busy = "delete";
    updateActions();
    try {
      const result = await api.deleteJobs(ids);
      selected.clear();
      if (result.skipped.length) setError(m("history.delete_result", { deleted: result.deleted.length, skipped: result.skipped.length }));
      onDeleted(result.deleted);
    } catch (err) { setError(err.message ? err : m("history.delete_failed")); }
    finally { busy = null; await refresh(); }
  }

  return { refresh, setCurrent(id) {
    current = id;
    for (const [jobId, entry] of rows) entry.row.classList.toggle("current", jobId === id);
  } };
}
