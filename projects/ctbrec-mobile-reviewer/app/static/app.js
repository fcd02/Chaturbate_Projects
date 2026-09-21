const state = {
  mode: 'original',
  drives: new Set(),
  allDrives: [],
  models: [],
  queueId: null,
  current: null,
  keep: new Set(),
  decisions: {},
  frameCutMode: false,
  frameDecisions: {},
  deleteSelected: new Set(),
  recoverSelected: new Set(),
  touched: new Set(),
  deletionModelsSelected: new Set(),
  previewItems: [],
  previewIndex: 0,
  queueTimer: null,
  queueReturnView: 'models-view',
  decisionMode: 'DELETE',
  draftTimer: null,
  statusTimer: null,
  settingsReturnView: 'models-view',
  deletePreview: null,
  deletionFilteredNames: [],
  backgroundTimer: null,
  sortingHeartbeatTimer: null,
  lastModelName: '',
  modelListScrollY: 0,
  openingModelName: '',
  lastDeletionCatalogUpdatedAt: '',
  lastCatalogUpdatedAt: '',
  modelAdminModel: '',
  modelAdminLoading: false,
  modelCache: new Map(),
  modelLoadSeq: 0,
  modelLastSuccessAt: 0,
  modelLastError: '',
  modelRenderLimit: 120,
  modelRenderStep: 120,
  modelListObserver: null,
  modelListKey: '',
  recuNeedsVerificationPrompted: false,
  frameTilesCache: [],
  frameTilesByFile: new Map(),
  frameTilesByIndex: new Map(),
  frameTilesSignature: '',
  initRetryTimer: null,
  modelRetryTimer: null,
  settingsLoaded: false,
  settingsRevision: '',
  modelSort: 'largest',
  randomMinGb: '',
  randomMaxGb: '',
  randomSeed: 1,
  lastFileSelectionIndex: null,
  lastFrameSelectionIndex: null,
  shiftKeyDown: false,
  previewSourceScrubbing: false,
};

const $ = (id) => document.getElementById(id);

function effectiveMode() {
  return ['easy', 'hidden'].includes(state.mode) ? 'original' : state.mode;
}
function currentModelFilter() {
  if (state.mode === 'easy') return 'easy';
  if (state.mode === 'hidden') return 'hidden';
  return '';
}

// Shared HTML escaping for any UI that deliberately builds small HTML snippets.
// Keep this in app.js so later classic scripts (rapid.js) can use the same helper.
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[character]);
}

const MODEL_SORT_PREF_KEY = 'ctbrec_model_sort_v2150';
const BYTES_PER_GB = 1024 ** 3;

function loadModelSortPrefs() {
  try {
    const raw = JSON.parse(localStorage.getItem(MODEL_SORT_PREF_KEY) || '{}');
    if (['largest', 'random', 'avg_chunk', 'ts_size'].includes(raw.sort)) state.modelSort = raw.sort;
    if (raw.minGb !== undefined && raw.minGb !== null) state.randomMinGb = String(raw.minGb);
    if (raw.maxGb !== undefined && raw.maxGb !== null) state.randomMaxGb = String(raw.maxGb);
    const seed = Number(raw.seed);
    if (Number.isFinite(seed) && seed > 0) state.randomSeed = Math.floor(seed);
    else state.randomSeed = Math.floor(Date.now() % 2147483647) || 1;
  } catch (_) {
    state.randomSeed = Math.floor(Date.now() % 2147483647) || 1;
  }
}

function saveModelSortPrefs() {
  try {
    localStorage.setItem(MODEL_SORT_PREF_KEY, JSON.stringify({
      sort: state.modelSort, minGb: state.randomMinGb, maxGb: state.randomMaxGb, seed: state.randomSeed,
    }));
  } catch (_) {}
}

function modelSortSupported() {
  return !['deletion', 'cleanup'].includes(state.mode);
}

function currentModelSort() {
  return modelSortSupported() ? state.modelSort : 'largest';
}

function deterministicRandomRank(name) {
  // Stable FNV-style hash: renders/searches/catalog refreshes do not reshuffle.
  let hash = (2166136261 ^ (state.randomSeed >>> 0)) >>> 0;
  const text = String(name || '').toLowerCase();
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash >>> 0;
}

function parsedSizeBoundGb(value) {
  const text = String(value ?? '').trim();
  if (!text) return null;
  const number = Number(text);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function sortedFilteredModels() {
  const query = $('model-search').value.trim().toLowerCase();
  const sortMode = currentModelSort();
  let rows = state.models.filter(model => !query || String(model.name || '').toLowerCase().includes(query));
  if (sortMode === 'random') {
    const minGb = parsedSizeBoundGb(state.randomMinGb);
    const maxGb = parsedSizeBoundGb(state.randomMaxGb);
    rows = rows.filter(model => {
      const bytes = Number(model.bytes || 0);
      if (minGb !== null && bytes < minGb * BYTES_PER_GB) return false;
      if (maxGb !== null && bytes > maxGb * BYTES_PER_GB) return false;
      return true;
    });
    rows.sort((a, b) => deterministicRandomRank(a.name) - deterministicRandomRank(b.name) || String(a.name).localeCompare(String(b.name)));
  } else if (sortMode === 'ts_size') {
    rows = rows.filter(model => !model.ts_stats_known || Number(model.ts_bytes || 0) > 0);
    rows.sort((a, b) => {
      const aKnown = Boolean(a.ts_stats_known);
      const bKnown = Boolean(b.ts_stats_known);
      if (aKnown !== bKnown) return aKnown ? -1 : 1;
      if (aKnown && bKnown) {
        const delta = Number(b.ts_bytes || 0) - Number(a.ts_bytes || 0);
        if (delta) return delta;
      }
      return Number(b.bytes || 0) - Number(a.bytes || 0) || String(a.name).localeCompare(String(b.name));
    });
  } else if (sortMode === 'avg_chunk') {
    rows.sort((a, b) => {
      const aKnown = Boolean(a.chunk_stats_known);
      const bKnown = Boolean(b.chunk_stats_known);
      if (aKnown !== bKnown) return aKnown ? -1 : 1;
      if (aKnown && bKnown) {
        const delta = Number(b.avg_chunk_bytes || 0) - Number(a.avg_chunk_bytes || 0);
        if (delta) return delta;
      }
      return Number(b.bytes || 0) - Number(a.bytes || 0) || String(a.name).localeCompare(String(b.name));
    });
  } else {
    rows.sort((a, b) => Number(b.bytes || 0) - Number(a.bytes || 0) || String(a.name).localeCompare(String(b.name)));
  }
  return rows;
}

function renderModelSortControls() {
  const supported = modelSortSupported();
  $('model-sort-controls').classList.toggle('hidden', !supported);
  const sortMode = currentModelSort();
  document.querySelectorAll('#model-sort-picker button[data-model-sort]').forEach(button => {
    button.classList.toggle('active', button.dataset.modelSort === sortMode);
  });
  $('random-sort-controls').classList.toggle('hidden', !supported || sortMode !== 'random');
  $('avg-chunk-sort-note').classList.toggle('hidden', !supported || sortMode !== 'avg_chunk');
  $('ts-sort-note').classList.toggle('hidden', !supported || sortMode !== 'ts_size');
  $('random-min-size-gb').value = state.randomMinGb;
  $('random-max-size-gb').value = state.randomMaxGb;
  if (!supported) {
    $('model-sort-label').textContent = 'Largest total ↓';
    return;
  }
  if (sortMode === 'random') {
    const minGb = parsedSizeBoundGb(state.randomMinGb);
    const maxGb = parsedSizeBoundGb(state.randomMaxGb);
    const rangeBits = [];
    if (minGb !== null) rangeBits.push(`≥${minGb}`);
    if (maxGb !== null) rangeBits.push(`≤${maxGb}`);
    $('model-sort-label').textContent = rangeBits.length ? `Random • ${rangeBits.join(' / ')} GB` : 'Random';
  } else if (sortMode === 'ts_size') {
    const known = state.models.filter(model => model.ts_stats_known).length;
    const withTs = state.models.filter(model => model.ts_stats_known && Number(model.ts_bytes || 0) > 0).length;
    $('model-sort-label').textContent = 'TS-only size ↓';
    $('ts-sort-note').textContent = known === state.models.length
      ? `${withTs.toLocaleString()} model(s) contain .TS recordings. Only those models are shown, ranked by .TS bytes; total model size remains visible on each card.`
      : `${known.toLocaleString()} of ${state.models.length.toLocaleString()} model(s) have per-extension size totals. Tap Rescan once to populate exact TS bytes for the older cached catalog; unknown models remain listed afterward until that scan completes.`;
  } else if (sortMode === 'avg_chunk') {
    const known = state.models.filter(model => model.chunk_stats_known).length;
    $('model-sort-label').textContent = 'Avg chunk size ↓';
    $('avg-chunk-sort-note').textContent = `${known.toLocaleString()} of ${state.models.length.toLocaleString()} model(s) have exact persisted chunk metadata. Those are ordered by average chunk size; models not indexed yet follow by total size. This mode never scans disks or generates mosaics.`;
  } else {
    $('model-sort-label').textContent = 'Largest total ↓';
  }
}
const views = ['login-view', 'models-view', 'review-view', 'queue-view', 'settings-view', 'done-view'];

function showView(id) {
  views.forEach(v => $(v).classList.toggle('hidden', v !== id));
  if (id !== 'review-view') {
    clearTimeout(state.statusTimer);
    stopSortingHeartbeat();
  } else {
    startSortingHeartbeat();
  }
  if (id !== 'queue-view') clearTimeout(state.queueTimer);
}

function startSortingHeartbeat() {
  clearInterval(state.sortingHeartbeatTimer);
  const ping = () => api('/api/sorting/heartbeat', { method: 'POST', body: JSON.stringify({ active: true, queue_id: state.queueId || '' }) }).catch(() => {});
  ping();
  state.sortingHeartbeatTimer = setInterval(ping, 12000);
}

function stopSortingHeartbeat() {
  clearInterval(state.sortingHeartbeatTimer);
  state.sortingHeartbeatTimer = null;
  api('/api/sorting/heartbeat', { method: 'POST', body: JSON.stringify({ active: false }) }).catch(() => {});
}

function toast(message, duration = 3200) {
  const el = $('toast');
  el.textContent = message;
  el.classList.remove('hidden');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add('hidden'), duration);
}

class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = Number(status || 0);
  }
}

function pairedDeviceToken() {
  try { return localStorage.getItem('ctbrec_device_token') || ''; }
  catch (_) { return ''; }
}

async function api(path, options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  const { timeoutMs: requestedTimeout, maxAttempts: requestedAttempts, noAbort = false, ...fetchOptions } = options;
  const defaultAttempts = method === 'GET' ? 3 : 1;
  const maxAttempts = method === 'GET'
    ? Math.max(1, Math.min(3, Number(requestedAttempts || defaultAttempts)))
    : 1;
  let lastError = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const timeoutMs = requestedTimeout === 0 ? 0 : Number(requestedTimeout || (method === 'GET' ? 18000 : 30000));
    const controller = noAbort || timeoutMs <= 0 ? null : new AbortController();
    let timedOut = false;
    const timer = controller ? setTimeout(() => {
      timedOut = true;
      try { controller.abort('timeout'); } catch (_) { controller.abort(); }
    }, timeoutMs) : null;
    const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
    try {
      const response = await fetch(path, {
        ...fetchOptions,
        method,
        headers,
        ...(controller ? { signal: controller.signal } : {}),
        credentials: 'same-origin',
        cache: method === 'GET' && path.startsWith('/api/') ? 'no-store' : fetchOptions.cache,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new ApiError(data.error || `Request failed (${response.status})`, response.status);
      return data;
    } catch (rawError) {
      const error = timedOut
        ? new ApiError(`Request timed out after ${Math.round(timeoutMs / 1000)} seconds.`, 408)
        : rawError;
      lastError = error;
      const retryableStatus = [0, 408, 429, 502, 503, 504].includes(Number(error?.status || 0));
      const retryable = method === 'GET' && attempt < maxAttempts && (retryableStatus || error instanceof TypeError);
      if (!retryable) throw error;
      await new Promise(resolve => setTimeout(resolve, 220 * attempt));
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
  throw lastError || new Error('Request failed.');
}

async function recoverPairedSession() {
  const token = pairedDeviceToken();
  if (!token) return false;
  try {
    const result = await api('/api/auth/recover', {
      method: 'POST',
      body: JSON.stringify({ device_token: token }),
      timeoutMs: 7000,
    });
    return Boolean(result?.authenticated || result?.ok);
  } catch (_) {
    return false;
  }
}

function setBusy(show, title = 'Working…', text = '', current = 0, total = 0) {
  $('busy').classList.toggle('hidden', !show);
  $('busy-title').textContent = title;
  $('busy-text').textContent = text || 'Please keep this page open.';
  const pct = total > 0 ? Math.max(4, Math.min(100, current / total * 100)) : 18;
  $('busy-progress').style.width = `${pct}%`;
}

async function pollTask(taskId, title, options = {}) {
  const blocking = options.blocking !== false;
  if (blocking) setBusy(true, title, 'Starting…');
  while (true) {
    const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    if (blocking) setBusy(true, title, task.progress || task.status, task.current || 0, task.total || 0);
    if (typeof options.onProgress === 'function') options.onProgress(task);
    if (task.status === 'done') { if (blocking) setBusy(false); return task.result || {}; }
    if (task.status === 'error') { if (blocking) setBusy(false); throw new Error(task.error || 'Task failed.'); }
    if (task.status === 'cancelled') { if (blocking) setBusy(false); throw new Error(task.progress || 'Task cancelled.'); }
    await new Promise(resolve => setTimeout(resolve, 900));
  }
}

async function init() {
  loadModelSortPrefs();
  clearTimeout(state.initRetryTimer);
  clearTimeout(state.modelRetryTimer);

  let auth = null;
  try {
    auth = await api('/api/auth', { timeoutMs: 5000, maxAttempts: 2 });
  } catch (error) {
    // A transport outage is not an auth failure. Keep any already-rendered model
    // list intact and retry; do not throw the user back to the PIN screen.
    showView('models-view');
    if (!state.models.length) $('model-count').textContent = 'Reconnecting…';
    $('catalog-note').textContent = `PC temporarily unreachable — retrying automatically. ${error?.message || ''}`;
    state.initRetryTimer = setTimeout(() => init().catch(() => {}), 2500);
    return;
  }

  if (!auth.authenticated && pairedDeviceToken()) {
    const healed = await recoverPairedSession();
    if (healed) {
      try { auth = await api('/api/auth', { timeoutMs: 5000, maxAttempts: 1 }); }
      catch (_) { auth = { authenticated: true }; }
    }
  }

  if (!auth.authenticated) {
    showView('login-view');
    $('login-error').textContent = '';
    return;
  }

  if (auth.device_token) {
    try { localStorage.setItem('ctbrec_device_token', auth.device_token); } catch (_) {}
  }
  showView('models-view');

  // Bootstrap and catalog are deliberately independent. A transient bootstrap
  // failure must never prevent the model list from loading. With no drive filter
  // yet, /api/catalog safely means "all drives".
  let bootstrapSucceeded = false;
  try {
    const bootstrap = await api('/api/bootstrap', { timeoutMs: 12000, maxAttempts: 2 });
    bootstrapSucceeded = true;
    state.allDrives = bootstrap.drives || [];
    if (!state.drives.size) state.drives = new Set(state.allDrives);
    else state.drives = new Set([...state.drives].filter(value => state.allDrives.includes(value)));
    if (!state.drives.size) state.drives = new Set(state.allDrives);
    renderDrivePicker();
    updateCatalogNote(bootstrap.catalog_status);
    state.lastDeletionCatalogUpdatedAt = bootstrap.catalog_status?.updated_at || '';
    state.lastCatalogUpdatedAt = bootstrap.catalog_status?.updated_at || '';
    renderModeExplanation();
    renderBackgroundOverview({
      library_background: bootstrap.library_background || {},
      non_nsfw_background: bootstrap.non_nsfw_background || {},
      background_scheduler: bootstrap.background_scheduler || {},
      action_queue: bootstrap.action_queue || {},
    });
  } catch (error) {
    renderModeExplanation();
    $('catalog-note').textContent = `Connected, but background status is still loading. Model list will continue independently. ${error?.message || ''}`;
  }

  try {
    await loadModels({ force: true, autoRetry: true });
  } catch (_) {
    // loadModels schedules its own bounded foreground retry and keeps a cached
    // list visible if one exists.
  }
  scheduleBackgroundStatusPoll();

  if (!bootstrapSucceeded) {
    state.initRetryTimer = setTimeout(async () => {
      try {
        const bootstrap = await api('/api/bootstrap', { timeoutMs: 12000, maxAttempts: 2 });
        state.allDrives = bootstrap.drives || [];
        if (!state.drives.size) state.drives = new Set(state.allDrives);
        renderDrivePicker();
        updateCatalogNote(bootstrap.catalog_status);
        renderBackgroundOverview({
          library_background: bootstrap.library_background || {},
          non_nsfw_background: bootstrap.non_nsfw_background || {},
          background_scheduler: bootstrap.background_scheduler || {},
          action_queue: bootstrap.action_queue || {},
        });
      } catch (_) {
        state.initRetryTimer = setTimeout(() => init().catch(() => {}), 4000);
      }
    }, 2500);
  }
}

function actionQueueText(status = {}) {
  const pending = Number(status.pending || 0);
  const retrying = Number(status.retrying || 0);
  const failed = Number(status.failed || status.blocked || 0);
  if (!pending && !failed) return status.last_message || 'No sorting instructions are waiting.';
  const parts = [];
  if (pending) parts.push(`${pending} queued/running`);
  if (retrying) parts.push(`${retrying} retrying`);
  if (failed) parts.push(`${failed} failed/stopped`);
  return `${parts.join(' • ')}${status.last_error ? ` • ${status.last_error}` : ''}`;
}

function libraryStatusText(status = {}) {
  const stateText = status.state || 'idle';
  const model = status.current_model ? ` • ${status.current_model}` : '';
  const progress = status.total_models ? ` • model ${status.model_index || 0}/${status.total_models}` : '';
  const counts = (status.generated || status.reused || status.errors)
    ? ` • generated ${status.generated || 0}, reused ${status.reused || 0}, errors ${status.errors || 0}`
    : '';
  return `${stateText}${model}${progress}${counts} — ${status.message || 'No status yet.'}`;
}

function nsfwStatusText(status = {}) {
  const bits = [status.state || 'idle'];
  if (status.current_model) bits.push(status.current_model);
  if (status.total_models) bits.push(`model ${status.model_index || 0}/${status.total_models}`);
  if (status.candidate_size || status.candidate_bytes) bits.push(`safe candidates ${status.candidate_size || formatBytes(status.candidate_bytes)}`);
  if (status.message) bits.push(status.message);
  if (status.detector_installed === false) bits.push('NudeNet not installed');
  return bits.join(' • ');
}

function renderBackgroundOverview(data = {}) {
  const library = data.library_background || {};
  const nsfw = data.non_nsfw_background || {};
  const scheduler = data.background_scheduler || {};
  const actions = data.action_queue || {};
  const backgroundOrder = scheduler.priority === 'cleanup_first' ? 'Non-NSFW before whole-library mosaics' : 'whole-library mosaics before Non-NSFW';
  const el = $('models-background-status');
  if (el) el.textContent = `Priority: requested model mosaics → queued moves/deletions → ${backgroundOrder} | Mosaics: ${libraryStatusText(library)} | Non-NSFW: ${nsfwStatusText(nsfw)} | Decisions: ${actionQueueText(actions)}`;
}

async function refreshBackgroundStatus() {
  try {
    const data = await api('/api/background-status');
    renderBackgroundOverview(data);
    const catalogUpdated = data.catalog_status?.updated_at || '';
    if (catalogUpdated && catalogUpdated !== state.lastCatalogUpdatedAt) {
      state.lastCatalogUpdatedAt = catalogUpdated;
      if (state.mode === 'deletion') state.lastDeletionCatalogUpdatedAt = catalogUpdated;
      // A completed disk scan is authoritative for displayed model sizes in every
      // tab, not only the deletion tab.  Invalidate the phone's short-lived list
      // cache and refresh in place without resetting the user's scroll window.
      invalidateModelCaches();
      if (!$('models-view').classList.contains('hidden')) {
        await loadModels({ force: true, resetWindow: false });
      }
    }
    if (state.current && !state.current.done) {
      state.current.library_background = data.library_background || {};
      state.current.non_nsfw_background = data.non_nsfw_background || {};
      state.current.background_scheduler = data.background_scheduler || {};
      state.current.action_queue = data.action_queue || {};
      renderActionQueue();
      if (state.current.mode === 'cleanup') renderPrefetch();
    }
  } catch (_) {}
}

function scheduleBackgroundStatusPoll() {
  clearTimeout(state.backgroundTimer);
  const delay = document.hidden ? 30000 : 5000;
  state.backgroundTimer = setTimeout(async () => {
    // Hidden iOS PWAs are aggressively suspended. Avoid useless wakeups and
    // server work while the user is not looking at the app; the foreground
    // visibility handler below refreshes immediately on return.
    if (!document.hidden) await refreshBackgroundStatus();
    scheduleBackgroundStatusPoll();
  }, delay);
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    refreshBackgroundStatus().catch(() => {});
    scheduleBackgroundStatusPoll();
    if (state.current && !state.current.done) refreshCurrentMetadata().catch(() => {});
  } else {
    scheduleBackgroundStatusPoll();
  }
});

function updateCatalogNote(status) {
  if (!status) return;
  $('catalog-note').textContent = `${status.progress || ''}${status.updated_at ? ` Updated ${status.updated_at}.` : ''}`;
}

function renderModeExplanation() {
  const holder = $('mode-explanation');
  holder.classList.toggle('danger-mode', state.mode === 'deletion');
  if (state.mode === 'original') {
    holder.innerHTML = '<strong>Originals → Review</strong><p class="muted">Tap segments to keep. Everything else moves safely into MARKED_FOR_DELETION.</p>';
  } else if (state.mode === 'easy') {
    holder.innerHTML = '<strong>Easy Sort</strong><p class="muted">Only models tagged <code>CTBRec Sorter EZ Sort: yes</code>. Sorting behavior is identical to Originals, and the model-order controls below still apply.</p>';
  } else if (state.mode === 'hidden') {
    holder.innerHTML = '<strong>Hidden models</strong><p class="muted">Temporarily hidden from ordinary phone lists. Open a model to sort it or use Model controls to unhide it.</p>';
  } else if (state.mode === 'review') {
    holder.innerHTML = '<strong>Review → final folders</strong><p class="muted">Route reviewed files into Cumshots, Misc Hot Scenes, or MARKED_FOR_DELETION.</p>';
  } else if (state.mode === 'cleanup') {
    holder.innerHTML = '<strong>Non-NSFW cleanup</strong><p class="muted">Shows only whole recordings whose sampled frames contained no explicit-nudity detection. Models are ranked by candidate bytes, largest first; all candidates start selected for the safe deletion queue.</p>';
  } else {
    holder.innerHTML = '<strong>Deletion queue + recovery</strong><p class="muted">Use the checkbox to select whole models for permanent deletion, or tap <em>Inspect / restore</em> to rescue individual recordings. Exact move history is restored when available; older ambiguous files are safely returned to a canonical same-drive Review folder.</p>';
  }
  renderModelSortControls();
}

function renderDrivePicker() {
  const holder = $('drive-picker'); holder.innerHTML = '';
  const all = document.createElement('button');
  all.className = `chip ${state.drives.size === state.allDrives.length ? 'active' : ''}`;
  all.textContent = 'All';
  all.onclick = () => { state.drives = new Set(state.allDrives); renderDrivePicker(); loadModels().catch(error => toast(error.message, 4500)); };
  holder.appendChild(all);
  for (const drive of state.allDrives) {
    const button = document.createElement('button');
    button.className = `chip ${state.drives.has(drive) ? 'active' : ''}`;
    button.textContent = drive;
    button.onclick = () => {
      if (state.drives.has(drive)) state.drives.delete(drive); else state.drives.add(drive);
      if (state.drives.size === 0) state.drives.add(drive);
      renderDrivePicker(); loadModels().catch(error => toast(error.message, 4500));
    };
    holder.appendChild(button);
  }
}

function modelRequestKey(mode = state.mode, drivesSet = state.drives) {
  const drives = [...drivesSet].map(value => String(value).toUpperCase()).sort().join(',');
  const filter = mode === 'easy' ? 'easy' : (mode === 'hidden' ? 'hidden' : '');
  const effective = ['easy', 'hidden'].includes(mode) ? 'original' : mode;
  return `${effective}|${filter}|${drives}`;
}

function invalidateModelCaches() {
  state.modelCache.clear();
}

function applyModelPayload(data, key, resetWindow = true) {
  if (key !== modelRequestKey()) return false;
  state.models = data.models || [];
  state.modelListKey = key;
  renderModelSortControls();
  if (state.mode === 'deletion' && data.status?.updated_at) state.lastDeletionCatalogUpdatedAt = data.status.updated_at;
  const available = new Set(state.models.map(model => model.name));
  state.deletionModelsSelected = new Set([...state.deletionModelsSelected].filter(name => available.has(name)));
  updateCatalogNote(data.status);
  renderModels(resetWindow);
  return true;
}

async function loadModels(options = {}) {
  clearTimeout(state.modelRetryTimer);
  const modeAtStart = state.mode;
  const key = modelRequestKey(modeAtStart, state.drives);
  const seq = ++state.modelLoadSeq;

  const cached = state.modelCache.get(key);
  if (cached) {
    applyModelPayload(cached, key, options.resetWindow !== false);
    if (!options.force && Date.now() - Number(cached._client_cached_at || 0) < 8000) return;
  } else if (state.modelListKey !== key) {
    // A new uncached tab must not display the previous tab's rows. Do not clear
    // a same-tab list merely because its refresh failed.
    state.models = [];
    state.modelListKey = key;
    renderModels(true);
    $('model-count').textContent = 'Loading…';
  } else if (!state.models.length) {
    $('model-count').textContent = 'Loading…';
  }

  const effective = ['easy', 'hidden'].includes(modeAtStart) ? 'original' : modeAtStart;
  const filter = modeAtStart === 'easy' ? 'easy' : (modeAtStart === 'hidden' ? 'hidden' : '');
  const drives = [...state.drives].join(',');
  const startedAt = performance.now();
  try {
    // IMPORTANT v2.13.3: model-list GETs deliberately use no AbortController.
    // Safari can surface self/timeout aborts as "signal is aborted without reason".
    // Stale responses are already made harmless by modelLoadSeq + request-key checks,
    // so cancellation is unnecessary and was itself a field regression source.
    const data = await api(`/api/catalog?mode=${encodeURIComponent(effective)}&filter=${encodeURIComponent(filter)}&drives=${encodeURIComponent(drives)}`, {
      timeoutMs: 0,
      maxAttempts: 1,
      noAbort: true,
    });
    if (seq !== state.modelLoadSeq || key !== modelRequestKey()) return;
    data._client_cached_at = Date.now();
    data._client_elapsed_ms = Math.round(performance.now() - startedAt);
    state.modelLastSuccessAt = Date.now();
    state.modelLastError = '';
    state.modelCache.set(key, data);
    while (state.modelCache.size > 8) state.modelCache.delete(state.modelCache.keys().next().value);
    applyModelPayload(data, key, options.resetWindow !== false);
  } catch (error) {
    // If the user switched tabs while this request was in flight, its result is
    // obsolete. Silently discard it rather than surfacing a reconnect warning.
    if (seq !== state.modelLoadSeq || key !== modelRequestKey()) return;
    state.modelLastError = String(error?.message || error || 'Unknown model-list error');
    if (cached || (state.modelListKey === key && state.models.length)) {
      $('catalog-note').textContent = `Showing the last loaded list while the PC reconnects. ${state.modelLastError}`;
    } else {
      $('model-count').textContent = 'Reconnecting…';
      updateCatalogNote({ progress: `Model list temporarily unavailable — retrying automatically. ${state.modelLastError}` });
    }
    if (options.autoRetry !== false && seq === state.modelLoadSeq && key === modelRequestKey()) {
      state.modelRetryTimer = setTimeout(() => loadModels({ force: true, autoRetry: true, resetWindow: false }).catch(() => {}), 2500);
    }
    throw error;
  }
}

function renderModels(resetWindow = false) {
  if (resetWindow) state.modelRenderLimit = state.modelRenderStep;
  if (state.modelListObserver) { state.modelListObserver.disconnect(); state.modelListObserver = null; }
  renderModelSortControls();
  const filtered = sortedFilteredModels();
  state.deletionFilteredNames = filtered.map(model => model.name);
  const visibleCount = Math.min(filtered.length, state.modelRenderLimit);
  $('model-count').textContent = `${filtered.length.toLocaleString()} model${filtered.length === 1 ? '' : 's'}${visibleCount < filtered.length ? ` • showing ${visibleCount.toLocaleString()}` : ''}`;
  $('deletion-model-actions').classList.toggle('hidden', state.mode !== 'deletion');
  renderDeletionSelectionSummary();
  const list = $('model-list'); list.innerHTML = '';
  const fragment = document.createDocumentFragment();
  for (const model of filtered.slice(0, visibleCount)) {
    if (state.mode === 'deletion') {
      const card = document.createElement('div');
      const selected = state.deletionModelsSelected.has(model.name);
      card.className = `model-card deletion-model-card ${selected ? 'selected-for-deletion' : ''}`;
      card.dataset.modelName = model.name;
      card.innerHTML = '<button class="delete-model-toggle"></button><button class="delete-model-inspect"><span class="model-name"></span><span class="model-size"></span><span class="model-sub"></span></button>';
      const toggle = card.querySelector('.delete-model-toggle');
      toggle.textContent = selected ? '✓' : '';
      toggle.setAttribute('aria-label', `${selected ? 'Unselect' : 'Select'} ${model.name} for permanent deletion`);
      toggle.onclick = () => {
        if (state.deletionModelsSelected.has(model.name)) state.deletionModelsSelected.delete(model.name);
        else state.deletionModelsSelected.add(model.name);
        renderModels(false);
      };
      const inspect = card.querySelector('.delete-model-inspect');
      inspect.querySelector('.model-name').textContent = model.name;
      inspect.querySelector('.model-size').textContent = model.size;
      inspect.querySelector('.model-sub').textContent = `${model.folder_count} marked folder${model.folder_count === 1 ? '' : 's'} • ${model.drives.join(', ')} • Inspect / restore →`;
      inspect.onclick = () => openDeletionModel(model.name);
      fragment.appendChild(card);
      continue;
    }
    const card = document.createElement('button');
    card.className = 'model-card';
    card.dataset.modelName = model.name;
    card.innerHTML = '<span class="model-name"></span><span class="model-size"></span><span class="model-sub"></span>';
    card.querySelector('.model-name').textContent = model.name;
    card.querySelector('.model-size').textContent = model.size;
    if (state.mode === 'cleanup') {
      card.querySelector('.model-sub').textContent = `${model.candidate_files || 0} candidate recording${Number(model.candidate_files || 0) === 1 ? '' : 's'} • ${model.sample_count || 0} samples • ${model.drives.join(', ')}`;
    } else {
      const ready = Number(model.ready_chunks || 0);
      const avgDetail = currentModelSort() === 'avg_chunk'
        ? (model.chunk_stats_known ? ` • avg chunk ${model.avg_chunk_size} across ${model.chunk_count} chunk${Number(model.chunk_count) === 1 ? '' : 's'}` : ' • avg chunk pending model indexing')
        : '';
      const tsDetail = currentModelSort() === 'ts_size'
        ? (model.ts_stats_known ? ` • TS ${model.ts_size || '0 B'} • total ${model.size}` : ' • TS size pending Rescan')
        : '';
      card.querySelector('.model-sub').textContent = `${model.folder_count} folder${model.folder_count === 1 ? '' : 's'} • ${model.drives.join(', ')} • ${ready ? `READY • ${ready} mosaic${ready === 1 ? '' : 's'}` : 'tap = open + generate first mosaic'}${avgDetail}${tsDetail}`;
    }
    card.onclick = () => openModel(model.name);
    fragment.appendChild(card);
  }
  list.appendChild(fragment);
  if (visibleCount < filtered.length) {
    const sentinel = document.createElement('div');
    sentinel.className = 'model-list-sentinel muted small';
    sentinel.textContent = 'Loading more models…';
    list.appendChild(sentinel);
    state.modelListObserver = new IntersectionObserver(entries => {
      if (!entries.some(entry => entry.isIntersecting)) return;
      state.modelRenderLimit = Math.min(filtered.length, state.modelRenderLimit + state.modelRenderStep);
      renderModels(false);
    }, { rootMargin: '800px 0px' });
    state.modelListObserver.observe(sentinel);
  }
}

async function openDeletionModel(name) {
  try {
    const created = await api('/api/queues/load', {
      method: 'POST', body: JSON.stringify({ mode: 'deletion', model: name, drives: [...state.drives] }),
    });
    const result = await pollTask(created.task_id, `Preparing deletion recovery for ${name}`);
    state.queueId = result.queue_id;
    await loadCurrent(true);
  } catch (error) { toast(error.message, 6000); }
}

function renderDeletionSelectionSummary() {
  const selectedModels = state.models.filter(model => state.deletionModelsSelected.has(model.name));
  const bytes = selectedModels.reduce((sum, model) => sum + Number(model.bytes || 0), 0);
  $('deletion-selection-summary').textContent = `${selectedModels.length} model${selectedModels.length === 1 ? '' : 's'} selected${bytes ? ` • ${formatBytes(bytes)}` : ''}`;
  $('delete-selected-models').disabled = selectedModels.length === 0;
}

async function previewSelectedDeletionModels() {
  try {
    const preview = await api('/api/deletion-models/preview', {
      method: 'POST',
      body: JSON.stringify({ models: [...state.deletionModelsSelected], drives: [...state.drives] }),
    });
    state.deletePreview = preview;
    $('delete-modal-summary').textContent = `${preview.model_count} selected model(s), ${preview.file_count} file(s), ${preview.size}.`;
    $('delete-phrase').value = '';
    $('delete-modal').classList.remove('hidden');
    setTimeout(() => $('delete-phrase').focus(), 100);
  } catch (error) { toast(error.message, 6000); }
}

async function openModel(name, options = {}) {
  if (!name || state.openingModelName) return;
  cancelPendingDraftSave();
  state.openingModelName = name;
  const openingCard = [...document.querySelectorAll('#model-list [data-model-name]')].find(el => String(el.dataset.modelName || '').toLowerCase() === String(name).toLowerCase());
  if (openingCard) { openingCard.classList.add('opening-model'); openingCard.setAttribute('aria-busy', 'true'); }
  toast(`Opening ${name}…`, 1400);
  if (!options.preserveOrigin) {
    state.lastModelName = name;
    state.modelListScrollY = window.scrollY;
  }
  try {
    // v2.14.9: never auto-hop away from the model the user tapped. The server
    // returns a queue/view even when no mosaic is ready yet, then prepares only
    // this model's current mosaic on demand. No client-side 12-second abort.
    const result = await api('/api/queues/open-fast', {
      method: 'POST',
      timeoutMs: 0,
      body: JSON.stringify({ mode: effectiveMode(), model: name, drives: [...state.drives] }),
    });
    if (!result.queue_id) throw new Error(result.reason || `Could not open ${name}.`);
    state.queueId = result.queue_id;
    if (typeof applyOnlineCurrent === 'function' && result.current) applyOnlineCurrent(result.current, true);
    else await loadCurrent(true);
    const ready = Number(result.ready_chunks || 0);
    const recovered = Number(result.recovered_existing_mosaics || 0);
    if (result.pending || !ready) {
      toast(`${name} is open. Its first sortable mosaic is being prepared now; automatic look-ahead will fill behind it.`, 6500);
    } else {
      const recoveryNote = recovered > 0 ? ` Reindexed ${recovered} additional existing mosaic${recovered === 1 ? '' : 's'} from disk.` : '';
      toast(`${name}: opened ${ready} ready mosaic${ready === 1 ? '' : 's'}.${recoveryNote} Automatic look-ahead will keep the configured ready buffer filled.`, recovered > 0 ? 6000 : 4600);
    }
  } catch (error) {
    toast(error.message, 6000);
  } finally {
    if (openingCard) { openingCard.classList.remove('opening-model'); openingCard.removeAttribute('aria-busy'); }
    if (state.openingModelName === name) state.openingModelName = '';
  }
}

async function nextReadyModel() {
  if (!state.current?.model) return;
  if (effectiveMode() === 'deletion') { toast('Use Models to choose another deletion-recovery model.'); return; }
  const currentName = state.current.model;
  const currentIndex = state.models.findIndex(model => model.name.toLowerCase() === currentName.toLowerCase());
  let candidate = null;
  if (currentIndex >= 0) {
    candidate = state.models.slice(currentIndex + 1).find(model => Number(model.ready_chunks || 0) > 0) || null;
  }
  if (!candidate && !['easy','hidden'].includes(state.mode)) {
    try {
      const drives = [...state.drives].join(',');
      const data = await api(`/api/ready-suggestions?mode=${encodeURIComponent(effectiveMode())}&drives=${encodeURIComponent(drives)}&exclude=${encodeURIComponent(currentName)}&limit=10000`);
      const ready = new Map((data.suggestions || []).map(row => [String(row.name).toLowerCase(), row]));
      if (currentIndex >= 0) {
        const row = state.models.slice(currentIndex + 1).find(model => ready.has(model.name.toLowerCase()));
        if (row) candidate = row;
      }
      if (!candidate && data.suggestions?.length) candidate = data.suggestions[0];
    } catch (_) {}
  }
  if (!candidate?.name) {
    toast('No other model currently has a ready mosaic. Tap the model you want instead; its first mosaic will generate on demand.', 6000);
    return;
  }
  state.queueId = null;
  state.current = null;
  await openModel(candidate.name, { preserveOrigin: true });
}

function releaseReviewDom() {
  // iOS Safari retains decoded image surfaces even after the view is hidden.
  // A tall 120-tile mosaic can represent tens of MB decoded, so explicitly
  // release finished review DOM when the queue is abandoned/completed.
  const mosaicList = $('mosaic-list');
  if (mosaicList) {
    mosaicList.querySelectorAll('img').forEach(img => { try { img.removeAttribute('src'); } catch (_) {} });
    mosaicList.replaceChildren();
  }
  const files = $('file-list'); if (files) files.replaceChildren();
  state.frameTilesSignature = ''; state.frameTilesCache = []; state.frameTilesByFile = new Map(); state.frameTilesByIndex = new Map();
}

async function returnToModels() {
  cancelPendingDraftSave();
  const anchor = state.current?.model || state.lastModelName;
  releaseReviewDom();
  state.queueId = null;
  state.current = null;
  showView('models-view');
  const cached = state.modelCache.get(modelRequestKey());
  if (cached) applyModelPayload(cached, modelRequestKey(), false);
  else { state.models = []; renderModels(true); $('model-count').textContent = 'Loading…'; }
  if (anchor) {
    const index = state.models.findIndex(row => String(row.name || '').toLowerCase() === String(anchor).toLowerCase());
    if (index >= 0 && index >= state.modelRenderLimit) {
      state.modelRenderLimit = Math.min(state.models.length, index + state.modelRenderStep);
      renderModels(false);
    }
  }
  requestAnimationFrame(() => {
    if (anchor) {
      const card = [...document.querySelectorAll('#model-list [data-model-name]')].find(el => String(el.dataset.modelName || '').toLowerCase() === String(anchor).toLowerCase());
      if (card) { card.scrollIntoView({ block: 'center', behavior: 'auto' }); return; }
    }
    window.scrollTo({ top: state.modelListScrollY || 0, behavior: 'auto' });
  });
  // Refresh after paint; never hold navigation on a network/catalog request.
  loadModels({ resetWindow: false }).catch(() => {});
}

function scrollReviewToTop() {
  // Sorting is thumb-first: when a new chunk replaces the previous mosaic,
  // reset the viewport instead of leaving the user at the old scroll position.
  // Same-chunk metadata refreshes do not call this helper.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const review = $('review-view');
    if (review && typeof review.scrollIntoView === 'function') {
      try { review.scrollIntoView({ block: 'start', behavior: 'auto' }); } catch (_) {}
    }
    try { window.scrollTo({ top: 0, left: 0, behavior: 'auto' }); } catch (_) { window.scrollTo(0, 0); }
  }));
}

async function loadCurrent(resetSelections = true) {
  if (!state.queueId) return;
  const previousSignature = String(state.current?.chunk?.signature || '');
  const current = await api(`/api/queues/${encodeURIComponent(state.queueId)}/current`);
  const nextSignature = String(current?.chunk?.signature || '');
  const chunkChanged = Boolean(nextSignature && nextSignature !== previousSignature);
  state.current = current;
  state.frameTilesSignature = ''; state.frameTilesCache = []; state.frameTilesByFile = new Map(); state.frameTilesByIndex = new Map();
  if (current.done) {
    releaseReviewDom();
    $('done-back-button').disabled = !current.can_back;
    showView('done-view');
    return;
  }
  showView('review-view');
  if (resetSelections) {
    const priorFrameCutMode = Boolean(state.frameCutMode);
    state.lastFileSelectionIndex = null;
    state.lastFrameSelectionIndex = null;
    state.touched = new Set();
    state.keep = new Set((current.draft?.keep_indices || []).map(Number));
    state.recoverSelected = new Set((current.draft?.restore_indices || []).map(Number));
    state.decisions = current.draft?.decisions || {};
    const draftHasGranularity = Object.prototype.hasOwnProperty.call(current.draft || {}, 'frame_cut_mode');
    state.frameCutMode = current.mode === 'review' && (draftHasGranularity ? Boolean(current.draft.frame_cut_mode) : priorFrameCutMode);
    state.frameDecisions = current.draft?.frame_decisions || {};
    const defaultDelete = current.mode === 'cleanup' && !Array.isArray(current.draft?.delete_indices)
      ? current.chunk.files.map(file => Number(file.index))
      : (current.draft?.delete_indices || []).map(Number);
    state.deleteSelected = new Set(defaultDelete);
    if (current.mode === 'review') {
      for (const file of current.chunk.files) {
        if (!state.decisions[String(file.index)]) state.decisions[String(file.index)] = 'Leave for review';
      }
    }
    syncTouchedFromSelections();
  }
  renderCurrent();
  if (chunkChanged) scrollReviewToTop();
  scheduleStatusPoll();
}

async function refreshCurrentMetadata() {
  if (!state.queueId || !state.current || state.current.done) return;
  try {
    const previous = state.current;
    const status = await api(`/api/queues/${encodeURIComponent(state.queueId)}/status`);
    if (status.done || status.chunk_signature !== previous.chunk?.signature) {
      await loadCurrent(true);
      return;
    }
    const previousRecu = JSON.stringify(previous.recu?.segments || {});
    const previousMosaicState = String(previous.mosaic_status?.state || '');
    state.current = {
      ...previous,
      recu: status.recu || previous.recu,
      prefetch: status.prefetch || previous.prefetch,
      mosaic_status: status.mosaic_status || previous.mosaic_status,
      action_queue: status.action_queue || previous.action_queue,
      library_background: status.library_background || previous.library_background,
      non_nsfw_background: status.non_nsfw_background || previous.non_nsfw_background,
      background_scheduler: status.background_scheduler || previous.background_scheduler,
      remaining: Number(status.remaining ?? previous.remaining),
      can_back: Boolean(status.can_back),
    };
    const recuChanged = previousRecu !== JSON.stringify(state.current.recu?.segments || {});
    const previousRecuStatus = String(previous.recu?.status || '');
    const nextRecuStatus = String(state.current.recu?.status || '');
    if (nextRecuStatus === 'needs_verification' && previousRecuStatus !== 'needs_verification' && !state.recuNeedsVerificationPrompted) {
      state.recuNeedsVerificationPrompted = true;
      toast('Recu needs re-authentication. Open Mobile Settings → Recu, complete verification in Chrome, then capture the session.', 9000);
    } else if (nextRecuStatus !== 'needs_verification') {
      state.recuNeedsVerificationPrompted = false;
    }
    if (recuChanged && ['original','review'].includes(state.current.mode)) {
      for (const file of state.current.chunk?.files || []) file.kinks = listRecuForFile(state.current.recu, file.index);
    }
    const nowMosaicState = String(state.current.mosaic_status?.state || '');
    if (!previous.chunk?.parts?.length && previousMosaicState !== 'ready' && nowMosaicState === 'ready') {
      await loadCurrent(false);
      return;
    }
    renderCurrentBackgroundOnly({ recu: recuChanged, files: recuChanged });
    scheduleStatusPoll();
  } catch (_) {
    scheduleStatusPoll();
  }
}

function listRecuForFile(recu, index) {
  const segments = recu?.segments || {};
  return Array.isArray(segments[String(index)]) ? segments[String(index)] : [];
}

function renderCurrentBackgroundOnly(options = {}) {
  const c = state.current;
  if (!c || c.done) return;
  renderRecu();
  renderPrefetch();
  renderActionQueue();
  renderBackgroundOverview({
    library_background: c.library_background || {},
    non_nsfw_background: c.non_nsfw_background || {},
    background_scheduler: c.background_scheduler || {},
    action_queue: c.action_queue || {},
  });
  if (options.recu || options.files) {
    renderKinkSummary();
    updateMosaicKinksInPlace();
  }
  if (options.files) renderFiles();
  const waitingText = document.querySelector('#mosaic-list .mosaic-waiting p');
  if (waitingText) waitingText.textContent = c.mosaic_status?.message || 'This request has priority over whole-library background work. The page will update automatically.';
}

function scheduleStatusPoll() {
  clearTimeout(state.statusTimer);
  if (!state.current || state.current.done) return;
  const recuStatus = state.current.recu?.status;
  const prefetchState = state.current.prefetch?.state;
  const mosaicState = state.current.mosaic_status?.state;
  const actionPending = Number(state.current.action_queue?.pending || 0) > 0;
  const nsfwRunning = state.current.mode === 'cleanup' && ['running', 'waiting', 'paused'].includes(state.current.non_nsfw_background?.state);
  const needsPoll = ['loading'].includes(recuStatus)
    || ['running', 'waiting_idle', 'paused_active'].includes(prefetchState)
    || ['queued', 'running'].includes(mosaicState)
    || actionPending
    || nsfwRunning;
  if (needsPoll) state.statusTimer = setTimeout(refreshCurrentMetadata, 2200);
}

function renderReviewGranularity() {
  const c = state.current;
  if (!c || c.mode !== 'review') return;
  const available = Boolean(c.chunk?.frame_cut_available);
  if (state.frameCutMode && !available) state.frameCutMode = false;
  const frameButton = document.querySelector('#review-granularity button[data-granularity="frames"]');
  const fileButton = document.querySelector('#review-granularity button[data-granularity="files"]');
  if (frameButton) frameButton.disabled = !available;
  if (frameButton) frameButton.classList.toggle('active', state.frameCutMode);
  if (fileButton) fileButton.classList.toggle('active', !state.frameCutMode);
  $('review-frame-cut-help').classList.toggle('hidden', !state.frameCutMode);
  const unavailable = $('review-frame-cut-unavailable');
  unavailable.classList.toggle('hidden', available);
  unavailable.textContent = available ? '' : (c.chunk?.frame_cut_unavailable_reason || 'Regenerate this mosaic once to enable exact Frame Cut timestamps.');
  $('review-instruction-title').textContent = state.frameCutMode
    ? 'Frame Cut: choose where kept footage goes, then tap individual sampled frames.'
    : 'Whole segments: choose a destination, then tap numbered source segments.';
  $('mosaic-list').classList.toggle('review-frame-cut', state.frameCutMode);
  const deleteDecision = document.querySelector('#decision-mode button[data-decision="DELETE"]');
  if (deleteDecision) deleteDecision.textContent = state.frameCutMode ? 'Delete / unselect' : 'DELETE';
  if ($('clear-review-selections')) $('clear-review-selections').textContent = state.frameCutMode ? 'Clear all frame selections' : 'Clear segment choices';
  if ($('submit-button')) $('submit-button').textContent = state.frameCutMode ? 'Queue precise cuts & next' : 'Submit destinations & next';
}

function renderCurrent() {
  const c = state.current;
  $('review-model').textContent = c.model;
  const pendingModel = String(c.chunk?.signature || '') === '__pending__';
  const reviewed = pendingModel ? 1 : (c.initial_count - c.remaining + 1);
  $('review-meta').textContent = pendingModel
    ? `Preparing first mosaic • ${c.chunk.size}`
    : `Chunk ${reviewed}/${c.initial_count} • ${c.chunk.size} • ${c.chunk.file_count} segments`;
  $('previous-button').disabled = pendingModel || !c.can_back;
  $('prefetch-button').disabled = pendingModel;
  $('regenerate-button').disabled = pendingModel;
  $('rebuild-chunk-button').disabled = pendingModel;
  $('skip-button').disabled = pendingModel;
  $('preview-selected-button').disabled = pendingModel;
  $('submit-button').disabled = pendingModel;
  $('next-model-button').classList.toggle('hidden', c.mode === 'deletion');
  $('original-instructions').classList.toggle('hidden', c.mode !== 'original');
  $('review-instructions').classList.toggle('hidden', c.mode !== 'review');
  $('deletion-instructions').classList.toggle('hidden', c.mode !== 'deletion');
  $('cleanup-instructions').classList.toggle('hidden', c.mode !== 'cleanup');
  if (c.mode === 'original') $('submit-button').textContent = 'Submit KEEP choices & next';
  else if (c.mode === 'review') $('submit-button').textContent = state.frameCutMode ? 'Queue precise cuts & next' : 'Submit destinations & next';
  else if (c.mode === 'cleanup') $('submit-button').textContent = 'Queue selected for deletion & next';
  else $('submit-button').textContent = 'Queue selected restores & next';
  renderReviewGranularity();
  renderRecu();
  renderPrefetch();
  renderActionQueue();
  renderBackgroundOverview({
    library_background: c.library_background || {},
    non_nsfw_background: c.non_nsfw_background || {},
    background_scheduler: c.background_scheduler || {},
    action_queue: c.action_queue || {},
  });
  renderMosaics();
  renderKinkSummary();
  renderSummary();
  renderFiles();
  // Never block mosaic rendering on CTBRec config discovery/read/write.
  setTimeout(() => loadModelAdminForCurrent(), 0);
}

function fileByIndex(index) {
  return state.current?.chunk?.files?.find(file => Number(file.index) === Number(index));
}

function reviewFrameTiles() {
  if (!state.current || state.current.mode !== 'review') return [];
  const signature = String(state.current.chunk?.signature || '');
  if (state.frameTilesSignature === signature) return state.frameTilesCache;
  const tiles = [];
  const byFile = new Map();
  const byIndex = new Map();
  for (const part of state.current.chunk?.parts || []) {
    for (const tile of part.tiles || []) {
      if (tile.frame_index === undefined || tile.chunk_seconds === undefined || tile.interval_end_seconds === undefined) continue;
      tiles.push(tile);
      byIndex.set(Number(tile.frame_index), tile);
      const key = Number(tile.file_index);
      if (!byFile.has(key)) byFile.set(key, []);
      byFile.get(key).push(tile);
    }
  }
  tiles.sort((a, b) => Number(a.frame_index) - Number(b.frame_index));
  state.frameTilesSignature = signature;
  state.frameTilesCache = tiles;
  state.frameTilesByFile = byFile;
  state.frameTilesByIndex = byIndex;
  return tiles;
}

function frameDecision(frameIndex) {
  return state.frameDecisions[String(Number(frameIndex))] || 'DELETE';
}

function frameSelectionStats() {
  const counts = { 'Leave for review': 0, Cumshots: 0, 'Misc Hot Scenes': 0 };
  let keptSeconds = 0;
  let selectedFrames = 0;
  const tiles = reviewFrameTiles();
  for (const tile of tiles) {
    const decision = frameDecision(tile.frame_index);
    if (counts[decision] === undefined) continue;
    counts[decision]++;
    selectedFrames++;
    keptSeconds += Math.max(0, Number(tile.interval_end_seconds || 0) - Number(tile.chunk_seconds || 0));
  }
  return { counts, keptSeconds, selectedFrames, frameCount: tiles.length };
}

function formatClockSeconds(value) {
  let seconds = Math.max(0, Math.round(Number(value || 0)));
  const hours = Math.floor(seconds / 3600); seconds %= 3600;
  const minutes = Math.floor(seconds / 60); seconds %= 60;
  return hours ? `${hours}:${String(minutes).padStart(2,'0')}:${String(seconds).padStart(2,'0')}` : `${minutes}:${String(seconds).padStart(2,'0')}`;
}

function renderRecu() {
  const panel = $('recu-panel');
  if (!['original','review'].includes(state.current.mode)) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  const recu = state.current.recu || {};
  const segments = recu.segments || {};
  const markerCount = Object.values(segments).reduce((sum, rows) => sum + rows.length, 0);
  const titles = {
    ready: `Recu kinks: ${markerCount} matched`,
    empty: 'Recu: no kink markers found',
    loading: 'Recu: loading kink markers…',
    needs_verification: 'Recu verification needed',
    disabled: 'Recu markers disabled',
    not_started: 'Recu not started',
  };
  $('recu-status-title').textContent = titles[recu.status] || `Recu: ${recu.status || 'unknown'}`;
  $('recu-status-text').textContent = recu.error || (recu.status === 'ready'
    ? 'Kink badges are shown on the matching numbered segments.'
    : recu.status === 'needs_verification'
      ? 'Open Mobile Settings, verify through the dedicated Chrome session, and capture it.'
      : 'The PC is using the verified Recu browser/session in the background.');
  $('recu-refresh-button').textContent = recu.status === 'needs_verification' ? 'Settings' : 'Refresh';
}

function renderPrefetch() {
  if (state.current.mode === 'cleanup') {
    $('prefetch-title').textContent = 'Non-NSFW detector / mosaic worker';
    $('prefetch-status-text').textContent = nsfwStatusText(state.current.non_nsfw_background || {});
    $('prefetch-now-button').textContent = 'Run detector now';
    $('prefetch-button').textContent = 'Scan';
    $('regenerate-button').classList.add('hidden');
    return;
  }
  $('prefetch-title').textContent = 'Automatic Look-ahead';
  $('prefetch-now-button').textContent = 'Fill now';
  $('prefetch-button').textContent = 'Fill';
  $('regenerate-button').classList.remove('hidden');
  const status = state.current.prefetch || {};
  $('prefetch-status-text').textContent = status.message || 'While this model stays open, the configured number of future mosaics is kept ready automatically.';
}

function renderActionQueue() {
  const status = state.current?.action_queue || {};
  const el = $('action-queue-status-text');
  if (el) el.textContent = actionQueueText(status);
  const retry = $('retry-blocked-actions');
  if (retry) retry.classList.toggle('hidden', Number(status.failed || status.blocked || 0) === 0);
}

function renderKinkSummary() {
  const holder = $('kink-summary'); holder.innerHTML = '';
  if (!['original','review'].includes(state.current.mode)) { holder.classList.add('hidden'); return; }
  const segments = state.current.recu?.segments || {};
  const entries = Object.entries(segments).sort((a, b) => Number(a[0]) - Number(b[0]));
  if (!entries.length) { holder.classList.add('hidden'); return; }
  holder.classList.remove('hidden');
  for (const [index, markers] of entries) {
    const chip = document.createElement('span'); chip.className = 'kink-chip';
    const labels = [...new Set(markers.map(m => `${m.emoji || '🔥'} ${m.name}`))];
    chip.textContent = `#${Number(index) + 1}: ${labels.join(', ')}`;
    holder.appendChild(chip);
  }
}

function renderMosaics() {
  const list = $('mosaic-list');
  // Explicitly detach old mosaic image sources before replacing a chunk. Mobile
  // Safari can otherwise retain large decoded JPEG surfaces longer than the DOM
  // nodes themselves, which compounds memory pressure during long sort sessions.
  list.querySelectorAll('img').forEach(img => { try { img.removeAttribute('src'); } catch (_) {} });
  list.replaceChildren();
  list.classList.toggle('cleanup-continuous', state.current.mode === 'cleanup');
  list.classList.toggle('review-frame-cut', state.current.mode === 'review' && state.frameCutMode);
  const parts = state.current.chunk.parts || [];
  if (!parts.length) {
    const waiting = document.createElement('div');
    waiting.className = 'panel mosaic-waiting';
    const status = state.current.mosaic_status || {};
    waiting.innerHTML = '<div class="spinner small-spinner"></div><div><strong>Mosaic is being prepared on the PC</strong><p class="muted"></p></div>';
    waiting.querySelector('p').textContent = status.message || 'This request has priority over whole-library background work. The page will update automatically.';
    list.appendChild(waiting);
    return;
  }
  for (const [partPosition, part] of parts.entries()) {
    const wrap = document.createElement('div'); wrap.className = `mosaic-wrap ${state.current.mode === 'cleanup' ? 'cleanup-mosaic-part' : ''}`;
    const img = document.createElement('img');
    img.src = `${part.url}?v=${encodeURIComponent(state.current.chunk.signature)}`;
    img.alt = 'Interactive mosaic';
    img.decoding = 'async';
    img.loading = partPosition === 0 ? 'eager' : 'lazy';
    img.fetchPriority = partPosition === 0 ? 'high' : 'low';
    wrap.appendChild(img);
    for (const tile of part.tiles) {
      const hit = document.createElement('button');
      const frameMode = state.current.mode === 'review' && state.frameCutMode && tile.frame_index !== undefined;
      hit.className = `tile-hit ${frameMode ? frameTileClass(tile.frame_index) : tileClass(tile.file_index)}`;
      hit.dataset.number = tile.number;
      hit.dataset.badge = frameMode ? `F${Number(tile.frame_index) + 1}` : tile.number;
      hit.dataset.fileIndex = tile.file_index;
      if (tile.frame_index !== undefined) hit.dataset.frameIndex = tile.frame_index;
      hit.style.left = `${tile.left}%`; hit.style.top = `${tile.top}%`;
      hit.style.width = `${tile.width}%`; hit.style.height = `${tile.height}%`;
      const file = fileByIndex(tile.file_index);
      const kinks = file?.kinks || [];
      if (kinks.length) {
        hit.classList.add('has-kink');
        const badge = document.createElement('span'); badge.className = 'kink-badge';
        const labels = [...new Set(kinks.map(m => `${m.emoji || '🔥'} ${m.name}`))];
        badge.textContent = labels.join(' • ');
        hit.appendChild(badge);
      }
      hit.setAttribute('aria-label', frameMode
        ? `Frame ${Number(tile.frame_index) + 1} at ${formatClockSeconds(tile.chunk_seconds)}`
        : `Segment ${tile.number}${kinks.length ? `, ${kinks.map(k => k.name).join(', ')}` : ''}`);
      hit.onpointerdown = (event) => rememberPointerShift(hit, event);
      hit.onclick = (event) => frameMode ? selectFrame(tile.frame_index, event) : selectFile(tile.file_index, event);
      wrap.appendChild(hit);
    }
    list.appendChild(wrap);
  }
}

function updateMosaicKinksInPlace() {
  if (!state.current?.chunk?.parts?.length) return;
  document.querySelectorAll('#mosaic-list .tile-hit').forEach(hit => {
    const index = Number(hit.dataset.fileIndex);
    const file = fileByIndex(index);
    const kinks = file?.kinks || [];
    hit.classList.toggle('has-kink', Boolean(kinks.length));
    const existing = hit.querySelector('.kink-badge');
    if (existing) existing.remove();
    if (kinks.length) {
      const badge = document.createElement('span');
      badge.className = 'kink-badge';
      const labels = [...new Set(kinks.map(m => `${m.emoji || '🔥'} ${m.name}`))];
      badge.textContent = labels.join(' • ');
      hit.appendChild(badge);
    }
    const number = hit.dataset.number || '';
    const frameIndex = hit.dataset.frameIndex;
    const frame = frameIndex !== undefined ? reviewFrameTiles().find(tile => Number(tile.frame_index) === Number(frameIndex)) : null;
    const baseLabel = frame && state.current.mode === 'review' && state.frameCutMode
      ? `Frame ${Number(frameIndex) + 1} at ${formatClockSeconds(frame.chunk_seconds)}, source segment ${number}`
      : `Segment ${number}`;
    hit.setAttribute('aria-label', `${baseLabel}${kinks.length ? `, ${kinks.map(k => k.name).join(', ')}` : ''}`);
  });
}

function frameTileClass(frameIndex) {
  const value = frameDecision(frameIndex);
  if (value === 'Cumshots') return 'cumshot frame-selected';
  if (value === 'Misc Hot Scenes') return 'misc frame-selected';
  if (value === 'Leave for review') return 'leave frame-selected';
  return 'frame-delete';
}

function tileClass(index) {
  if (state.current.mode === 'original') return state.keep.has(index) ? 'keep' : '';
  if (state.current.mode === 'deletion') return state.recoverSelected.has(index) ? 'restore' : '';
  if (state.current.mode === 'cleanup') return state.deleteSelected.has(index) ? 'cleanup-delete' : 'cleanup-keep';
  const value = state.decisions[String(index)] || 'Leave for review';
  if (value === 'Cumshots') return 'cumshot';
  if (value === 'Misc Hot Scenes') return 'misc';
  if (value === 'DELETE') return 'delete';
  return 'leave';
}

function isNonDefaultSelection(index) {
  index = Number(index);
  if (!state.current) return false;
  if (state.current.mode === 'original') return state.keep.has(index);
  if (state.current.mode === 'review') return (state.decisions[String(index)] || 'Leave for review') !== 'Leave for review';
  if (state.current.mode === 'deletion') return state.recoverSelected.has(index);
  if (state.current.mode === 'cleanup') return !state.deleteSelected.has(index);
  return false;
}

function syncTouchedForIndex(index) {
  index = Number(index);
  if (isNonDefaultSelection(index)) state.touched.add(index); else state.touched.delete(index);
}

function syncTouchedFromSelections() {
  state.touched = new Set();
  for (const file of state.current?.chunk?.files || []) {
    if (isNonDefaultSelection(Number(file.index))) state.touched.add(Number(file.index));
  }
}

function orderedFileIndices() {
  return (state.current?.chunk?.files || []).map(file => Number(file.index));
}

function orderedFrameIndices() {
  return reviewFrameTiles().map(tile => Number(tile.frame_index));
}

function shiftRangeRequested(event = null) {
  // Some desktop/PWA click paths lose modifier flags between pointerdown and
  // click. Track Shift globally and remember it on the exact hit target too.
  const pointerShift = event?.currentTarget?.dataset?.shiftPointer === '1';
  return Boolean(event?.shiftKey || state.shiftKeyDown || pointerShift);
}

function rememberPointerShift(element, event) {
  if (!element) return;
  element.dataset.shiftPointer = (event?.shiftKey || state.shiftKeyDown) ? '1' : '0';
}

function inclusiveRangeBetween(ordered, anchor, target) {
  const start = ordered.indexOf(Number(anchor));
  const end = ordered.indexOf(Number(target));
  if (start < 0 || end < 0) return [Number(target)];
  const low = Math.min(start, end);
  const high = Math.max(start, end);
  return ordered.slice(low, high + 1);
}

function refreshFrameSelectionVisual(frameIndex) {
  document.querySelectorAll(`#mosaic-list .tile-hit[data-frame-index="${frameIndex}"]`).forEach(hit => {
    const hasKink = hit.classList.contains('has-kink');
    hit.className = `tile-hit ${frameTileClass(frameIndex)}${hasKink ? ' has-kink' : ''}`;
  });
}

function setFrameSelected(frameIndex, selected = true, decision = state.decisionMode) {
  const key = String(Number(frameIndex));
  if (!selected || decision === 'DELETE') delete state.frameDecisions[key];
  else state.frameDecisions[key] = decision;
}

function selectFrame(frameIndex, event = null) {
  if (!state.current || state.current.mode !== 'review' || !state.frameCutMode) return;
  frameIndex = Number(frameIndex);
  const useRange = Boolean(shiftRangeRequested(event) && state.lastFrameSelectionIndex !== null);
  const changedFrames = useRange
    ? inclusiveRangeBetween(orderedFrameIndices(), state.lastFrameSelectionIndex, frameIndex)
    : [frameIndex];

  if (useRange) {
    const anchorDecision = frameDecision(state.lastFrameSelectionIndex);
    const shouldSelect = state.decisionMode !== 'DELETE' && anchorDecision !== 'DELETE';
    for (const index of changedFrames) setFrameSelected(index, shouldSelect, state.decisionMode);
  } else {
    const key = String(frameIndex);
    const current = frameDecision(frameIndex);
    const target = state.decisionMode;
    if (target === 'DELETE' || current === target) delete state.frameDecisions[key];
    else state.frameDecisions[key] = target;
  }

  state.lastFrameSelectionIndex = frameIndex;
  for (const index of changedFrames) refreshFrameSelectionVisual(index);
  renderSummary();
  reviewFrameTiles();
  const changedFiles = new Set(changedFrames.map(index => state.frameTilesByIndex.get(index)?.file_index).filter(v => v !== undefined).map(Number));
  for (const fileIndex of changedFiles) updateFileRowState(fileIndex);
  scheduleDraft();
}

function setFileSelected(index, selected = true, decision = state.decisionMode) {
  index = Number(index);
  if (state.current.mode === 'original') {
    if (selected) state.keep.add(index); else state.keep.delete(index);
  } else if (state.current.mode === 'review') {
    state.decisions[String(index)] = selected ? decision : 'Leave for review';
  } else if (state.current.mode === 'deletion') {
    const file = fileByIndex(index);
    if (selected && file?.restore_available) state.recoverSelected.add(index);
    else state.recoverSelected.delete(index);
  } else {
    if (selected) state.deleteSelected.add(index); else state.deleteSelected.delete(index);
  }
}

function refreshFileSelectionVisual(index) {
  document.querySelectorAll(`.tile-hit[data-file-index="${index}"]`).forEach(hit => {
    const hasKink = hit.classList.contains('has-kink');
    hit.className = `tile-hit ${tileClass(index)}${hasKink ? ' has-kink' : ''}`;
  });
  updateFileRowState(index);
}

function selectFile(index, event = null) {
  index = Number(index);
  const useRange = Boolean(shiftRangeRequested(event) && state.lastFileSelectionIndex !== null);
  const changedFiles = useRange
    ? inclusiveRangeBetween(orderedFileIndices(), state.lastFileSelectionIndex, index)
    : [index];

  if (useRange) {
    const anchor = Number(state.lastFileSelectionIndex);
    let shouldSelect = false;
    if (state.current.mode === 'original') shouldSelect = state.keep.has(anchor);
    else if (state.current.mode === 'review') shouldSelect = (state.decisions[String(anchor)] || 'Leave for review') !== 'Leave for review' && state.decisionMode !== 'DELETE';
    else if (state.current.mode === 'deletion') shouldSelect = state.recoverSelected.has(anchor);
    else shouldSelect = state.deleteSelected.has(anchor);
    for (const fileIndex of changedFiles) setFileSelected(fileIndex, shouldSelect, state.decisionMode);
  } else if (state.current.mode === 'original') {
    if (state.keep.has(index)) state.keep.delete(index); else state.keep.add(index);
  } else if (state.current.mode === 'review') {
    const key = String(index);
    const currentDecision = state.decisions[key] || 'Leave for review';
    // Tapping an already-selected destination again truly untaps it. This is
    // important for Preview: only current non-default choices are previewed.
    state.decisions[key] = currentDecision === state.decisionMode ? 'Leave for review' : state.decisionMode;
  } else if (state.current.mode === 'deletion') {
    const file = fileByIndex(index);
    if (!file?.restore_available) { toast(file?.restore_reason || 'No safe restore target was found for this file.', 6000); return; }
    if (state.recoverSelected.has(index)) state.recoverSelected.delete(index); else state.recoverSelected.add(index);
  } else {
    if (state.deleteSelected.has(index)) state.deleteSelected.delete(index); else state.deleteSelected.add(index);
  }

  state.lastFileSelectionIndex = index;
  for (const fileIndex of changedFiles) {
    syncTouchedForIndex(fileIndex);
    refreshFileSelectionVisual(fileIndex);
  }
  renderSummary(); scheduleDraft();
}

function clearCurrentSelections() {
  if (!state.current) return;
  state.lastFileSelectionIndex = null;
  state.lastFrameSelectionIndex = null;
  state.touched.clear();
  if (state.current.mode === 'original') {
    state.keep.clear();
  } else if (state.current.mode === 'review') {
    if (state.frameCutMode) state.frameDecisions = {};
    else {
      state.decisions = {};
      for (const file of state.current.chunk?.files || []) state.decisions[String(file.index)] = 'Leave for review';
    }
  } else if (state.current.mode === 'deletion') {
    state.recoverSelected.clear();
  } else if (state.current.mode === 'cleanup') {
    state.deleteSelected.clear();
  }
  renderMosaics();
  renderSummary();
  renderFiles();
  scheduleDraft();
}

function renderSummary() {
  const holder = $('selection-summary'); holder.innerHTML = '';
  const make = (text, cls) => {
    const el = document.createElement('span'); el.className = `summary-chip ${cls}`;
    el.textContent = text; holder.appendChild(el);
  };
  if (state.current.mode === 'original') {
    const chosen = [...state.keep].sort((a,b) => a-b).map(v => v + 1);
    make(`KEEP ${chosen.length}: ${chosen.length ? chosen.join(', ') : 'none'}`, 'keep');
    make(`Move to delete queue ${state.current.chunk.file_count - chosen.length}`, 'delete');
  } else if (state.current.mode === 'review') {
    if (state.frameCutMode) {
      const stats = frameSelectionStats();
      make(`KEEP ${stats.selectedFrames}/${stats.frameCount} frame intervals • ${formatClockSeconds(stats.keptSeconds)}`, 'keep');
      make(`Review ${stats.counts['Leave for review']}`, 'leave');
      make(`Cumshots ${stats.counts.Cumshots}`, 'cumshot');
      make(`Misc ${stats.counts['Misc Hot Scenes']}`, 'misc');
      make(`All unselected footage → deletion`, 'delete');
    } else {
      const counts = { 'Leave for review': 0, DELETE: 0, Cumshots: 0, 'Misc Hot Scenes': 0 };
      Object.values(state.decisions).forEach(value => { if (counts[value] !== undefined) counts[value]++; });
      make(`Leave ${counts['Leave for review']}`, 'leave');
      make(`Delete queue ${counts.DELETE}`, 'delete');
      make(`Cumshots ${counts.Cumshots}`, 'cumshot');
      make(`Misc ${counts['Misc Hot Scenes']}`, 'misc');
    }
  } else if (state.current.mode === 'cleanup') {
    const selected = [...state.deleteSelected];
    const bytes = selected.reduce((sum, index) => sum + Number(fileByIndex(index)?.bytes || 0), 0);
    make(`DELETE QUEUE ${selected.length}`, 'cleanup-delete');
    make(`KEEP / reviewed ${state.current.chunk.file_count - selected.length}`, 'cleanup-keep');
    if (bytes) make(formatBytes(bytes), 'cleanup-delete');
  } else {
    const selected = [...state.recoverSelected];
    const bytes = selected.reduce((sum, index) => sum + Number(fileByIndex(index)?.bytes || 0), 0);
    make(`SEND BACK ${selected.length}`, 'restore');
    make(`Remain in deletion ${state.current.chunk.file_count - selected.length}`, 'delete');
    if (bytes) make(formatBytes(bytes), 'restore');
  }
}

function formatBytes(value) {
  let amount = Number(value || 0);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit++; }
  return unit === 0 ? `${Math.round(amount)} B` : `${amount.toFixed(2)} ${units[unit]}`;
}

function fileStateText(file) {
  if (!state.current) return '';
  if (state.current.mode === 'original') return state.keep.has(file.index) ? 'KEEP' : 'DELETE QUEUE';
  if (state.current.mode === 'review') {
    if (state.frameCutMode) {
      reviewFrameTiles();
      const frames = state.frameTilesByFile.get(Number(file.index)) || [];
      const kept = frames.filter(tile => frameDecision(tile.frame_index) !== 'DELETE').length;
      return `${kept}/${frames.length} sampled intervals kept • ORIGINAL → DELETION after verified cuts`;
    }
    return state.decisions[String(file.index)] || 'Leave for review';
  }
  if (state.current.mode === 'cleanup') return state.deleteSelected.has(file.index) ? 'DELETE QUEUE' : 'KEEP / REVIEWED';
  return state.recoverSelected.has(file.index) ? 'SEND BACK' : (file.restore_available ? 'REMAIN IN DELETION' : 'RESTORE TARGET UNKNOWN');
}

function updateFileRowState(index) {
  const row = document.querySelector(`#file-list .file-row[data-file-index="${Number(index)}"]`);
  const file = fileByIndex(Number(index));
  if (!row || !file) return;
  const target = row.querySelector('.file-state');
  if (target) target.textContent = fileStateText(file);
}

function renderFiles() {
  const holder = $('file-list'); holder.innerHTML = '';
  for (const file of state.current.chunk.files) {
    const row = document.createElement('div'); row.className = 'file-row'; row.dataset.fileIndex = String(file.index);
    const stateText = fileStateText(file);
    row.innerHTML = '<strong></strong><span class="file-name"></span><span class="file-state"></span><button class="file-preview secondary compact">▶</button>';
    row.querySelector('strong').textContent = `#${file.number}`;
    row.querySelector('.file-name').textContent = `${file.name} • ${file.size}`;
    row.querySelector('.file-state').textContent = stateText;
    if (state.current.mode === 'deletion') {
      const target = document.createElement('span'); target.className = 'file-kinks';
      target.textContent = file.restore_available ? `Return → ${file.restore_target}` : `⚠ ${file.restore_reason || 'No safe restore target'}`;
      row.appendChild(target);
    }
    if (file.kinks?.length) {
      const kinkLine = document.createElement('span'); kinkLine.className = 'file-kinks';
      kinkLine.textContent = [...new Set(file.kinks.map(k => `${k.emoji || '🔥'} ${k.name} (${k.location})`))].join(' • ');
      row.appendChild(kinkLine);
    }
    row.onpointerdown = (event) => rememberPointerShift(row, event);
    row.onclick = (event) => {
      if (state.current.mode === 'review' && state.frameCutMode) {
        toast('Frame Cut is active — tap individual mosaic frames. Use ▶ here only to preview the full source recording.', 3500);
        return;
      }
      selectFile(file.index, event);
    };
    const preview = row.querySelector('.file-preview');
    preview.onclick = (event) => { event.stopPropagation(); openVideoPreview([Number(file.index)]); };
    holder.appendChild(row);
  }
}

function selectedIndicesForPreview() {
  if (!state.current) return [];
  if (state.current.mode === 'review' && state.frameCutMode) {
    const chosen = new Set(
      reviewFrameTiles()
        .filter(tile => frameDecision(tile.frame_index) !== 'DELETE')
        .map(tile => Number(tile.file_index))
    );
    return [...chosen].filter(index => fileByIndex(index)).sort((a,b) => a-b);
  }
  // Preview is intentionally scoped to CURRENT, non-default choices in this
  // exact chunk. It never falls back to model/chunk history.
  syncTouchedFromSelections();
  return [...state.touched].filter(index => fileByIndex(index)).sort((a,b) => a-b);
}

async function openVideoPreview(indices = null) {
  if (state.offlineMode) { toast('Video preview streams from the PC and is unavailable while you are offline. Your cached mosaics remain fully sortable.'); return; }
  const chosen = (indices || selectedIndicesForPreview()).map(Number);
  if (!chosen.length) { toast('Tap one or more mosaic segments first, or use the ▶ button beside a file.'); return; }
  try {
    const data = await api(`/api/queues/${encodeURIComponent(state.queueId)}/preview`, {
      method: 'POST', body: JSON.stringify({ indices: chosen }),
    });
    state.previewItems = data.items || [];
    state.previewIndex = 0;
    if (!state.previewItems.length) throw new Error('No previewable files were returned.');
    $('preview-modal').classList.remove('hidden');
    playPreviewItem(0, false);
  } catch (error) { toast(error.message, 6000); }
}

function isDesktopPreviewClient() {
  const ua = String(navigator.userAgent || '');
  // A user can open the dashboard on the recording PC through its Tailscale
  // hostname rather than literal localhost. The browser cannot expose physical
  // host identity to JavaScript, so desktop Chrome/Edge should always try the
  // original-file Range endpoint first. On the recording PC that is a direct
  // disk -> loopback/proxy -> browser path with zero ffmpeg work; on another
  // desktop it is still a zero-transcode original-file stream.
  return !/(iPhone|iPad|iPod|Android|Mobile)/i.test(ua);
}

function previewDiagnosticEvent(item, event, data = {}) {
  if (!item?.diagnostic_id || state.offlineMode) return;
  api('/api/preview-diagnostics/event', {
    method: 'POST',
    body: JSON.stringify({ diagnostic_id: item.diagnostic_id, event, data }),
  }).catch(() => {});
}

function mediaErrorPayload(video) {
  const error = video?.error;
  return {
    code: Number(error?.code || 0),
    message: String(error?.message || ''),
    networkState: Number(video?.networkState || 0),
    readyState: Number(video?.readyState || 0),
    currentTime: Number(video?.currentTime || 0),
    duration: Number.isFinite(Number(video?.duration)) ? Number(video.duration) : null,
    currentSrc: String(video?.currentSrc || ''),
  };
}

function previewStreamUrl(item, stage, absoluteStart = 0) {
  const base = String(item.stream_url_base || '').trim();
  if (!base) return stage === 'compatibility' ? item.compatibility_url : item.remux_url;
  const params = new URLSearchParams();
  params.set('start', String(Math.max(0, Number(absoluteStart || 0))));
  if (stage === 'compatibility') params.set('transcode', '1');
  if (item.diagnostic_id) params.set('diag', item.diagnostic_id);
  return `${base}?${params.toString()}`;
}

function previewUsesSourceTimeline(stage) {
  return stage === 'remux' || stage === 'compatibility';
}

function previewAbsoluteSeconds(video, fallback = 0) {
  if (!video) return Math.max(0, Number(fallback || 0));
  const stage = String(video.dataset.previewStage || 'direct');
  const offset = previewUsesSourceTimeline(stage) ? Math.max(0, Number(video.dataset.streamOffset || 0)) : 0;
  const current = Number(video.currentTime);
  return Math.max(0, offset + (Number.isFinite(current) ? current : Number(fallback || 0)));
}

function previewReachedSourceEnd(item, video, fallbackStart = 0) {
  const duration = Math.max(0, Number(item?.duration || 0));
  const absolute = previewAbsoluteSeconds(video, fallbackStart);
  const tolerance = duration > 0 ? Math.max(2.0, Math.min(12.0, duration * 0.01)) : 0;
  return {
    duration, absolute, tolerance,
    // Unknown duration must never be interpreted as "source finished"; doing
    // so would skip to the next selected recording when a fallback pipe dies.
    finished: duration > 0 && absolute >= Math.max(0, duration - tolerance),
  };
}

function updatePreviewSourceControls(item, video, requestedValue = null) {
  const controls = $('preview-source-controls');
  const slider = $('preview-source-timeline');
  const label = $('preview-source-time');
  const play = $('preview-source-play');
  const stage = String(video?.dataset?.previewStage || 'direct');
  const duration = Math.max(0, Number(item?.duration || 0));
  const enabled = previewUsesSourceTimeline(stage) && duration > 0;
  controls.classList.toggle('hidden', !enabled);
  if (video) video.controls = !enabled;
  if (!enabled) return;
  slider.min = '0';
  slider.max = String(duration);
  slider.step = duration > 10800 ? '1' : '0.25';
  const raw = requestedValue === null ? previewAbsoluteSeconds(video, Number(video?.dataset?.absoluteStart || 0)) : Number(requestedValue || 0);
  const value = Math.max(0, Math.min(duration, raw));
  if (!state.previewSourceScrubbing || requestedValue !== null) slider.value = String(value);
  const displayed = state.previewSourceScrubbing && requestedValue === null ? Number(slider.value || value) : value;
  label.textContent = `${formatClockSeconds(displayed)} / ${formatClockSeconds(duration)}`;
  play.textContent = video && !video.paused && !video.ended ? '❚❚' : '▶';
}

function seekCurrentPreviewToAbsolute(targetSeconds, autoplay = true) {
  const item = state.previewItems[state.previewIndex];
  if (!item) return;
  const video = $('preview-video');
  const stage = String(video.dataset.previewStage || 'direct');
  const duration = Math.max(0, Number(item.duration || 0));
  const safe = Math.max(0, duration > 0 ? Math.min(Number(targetSeconds || 0), Math.max(0, duration - 0.05)) : Number(targetSeconds || 0));
  previewDiagnosticEvent(item, 'source_timeline_seek', { stage, absoluteSeconds: safe, autoplay: Boolean(autoplay) });
  if (previewUsesSourceTimeline(stage)) {
    playPreviewItem(state.previewIndex, stage === 'compatibility', { stage, startSeconds: safe, autoplay });
    return;
  }
  try {
    video._ctbProgrammaticSeek = true;
    video.currentTime = safe;
    if (autoplay) video.play().catch(() => {});
  } catch (_) {}
}

function playPreviewItem(index, compatibility = false, options = {}) {
  if (!state.previewItems.length) return;
  state.previewIndex = Math.max(0, Math.min(state.previewItems.length - 1, Number(index)));
  const item = state.previewItems[state.previewIndex];
  const video = $('preview-video');
  if (video._ctbFallbackTimer) clearTimeout(video._ctbFallbackTimer);
  if (video._ctbSeekRestartTimer) clearTimeout(video._ctbSeekRestartTimer);
  video._ctbFallbackTimer = null;
  video._ctbSeekRestartTimer = null;
  video.pause();
  video.removeAttribute('src');
  try { video.load(); } catch (_) {}
  const autoplay = options.autoplay !== false;
  video.dataset.previewTerminalFailure = '0';

  const nativeHls = Boolean(
    item.hls_url &&
    (video.canPlayType('application/vnd.apple.mpegurl') || video.canPlayType('application/x-mpegURL'))
  );
  const requestedStart = Math.max(0, Number(options.startSeconds ?? item.start_seconds ?? 0));
  const forcedStage = String(options.stage || '');
  // Desktop direct playback is worthwhile only for browser-native containers.
  // Raw MPEG-TS may carry H.264/AAC yet Chromium can reject the container
  // outright. In that case use the cached zero-reencode seekable MP4 wrapper.
  const desktopDiskDirect = isDesktopPreviewClient() && !compatibility && !forcedStage && Boolean(item.local_disk_url) && Boolean(item.direct);
  const usingHls = !desktopDiskDirect && !compatibility && !forcedStage && nativeHls;
  const direct = (desktopDiskDirect || item.direct) && !compatibility && !usingHls && !forcedStage;
  const preferSeekable = !compatibility && !forcedStage && !usingHls && !direct && Boolean(item.seekable_url);
  const stage = forcedStage || (compatibility ? 'compatibility' : (desktopDiskDirect ? 'direct' : (usingHls ? 'hls' : (direct ? 'direct' : (preferSeekable ? 'seekable' : 'remux')))));
  const streamOffset = (stage === 'remux' || stage === 'compatibility') ? requestedStart : 0;

  video.preload = 'metadata';
  video.playsInline = true;
  video.dataset.previewStage = stage;
  video.dataset.streamOffset = String(streamOffset);
  video.dataset.absoluteStart = String(requestedStart);
  video._ctbProgrammaticSeek = false;
  if (stage === 'direct') video.src = desktopDiskDirect ? item.local_disk_url : item.url;
  else if (stage === 'hls') video.src = item.hls_url;
  else if (stage === 'seekable') video.src = item.seekable_url;
  else video.src = previewStreamUrl(item, stage, requestedStart);

  $('preview-modal-title').textContent = `#${item.number} ${item.name}`;
  $('preview-meta').textContent = `${item.size} • ${state.previewIndex + 1}/${state.previewItems.length}`;
  $('preview-position').textContent = `${state.previewIndex + 1} / ${state.previewItems.length}`;
  $('preview-previous').disabled = state.previewIndex <= 0;
  $('preview-next').disabled = state.previewIndex >= state.previewItems.length - 1;
  const compatibilityButton = $('preview-compatibility');
  compatibilityButton.classList.remove('hidden');
  compatibilityButton.disabled = stage === 'compatibility';
  compatibilityButton.textContent = stage === 'compatibility' ? 'Compatibility active' : 'Force compatibility now';
  $('preview-export-diagnostics').disabled = !item.diagnostic_id;
  state.previewSourceScrubbing = false;
  updatePreviewSourceControls(item, video, requestedStart);

  if (stage === 'hls') {
    $('preview-note').textContent = item.recu_start
      ? `Seekable iPhone stream. Starting at the first matching Recu kink (${Math.round(requestedStart)}s). You can scrub anywhere in the full recording.`
      : 'Seekable iPhone stream — drag the video timeline anywhere in the full recording; the PC only converts the segment you request.';
  } else if (stage === 'direct') {
    $('preview-note').textContent = item.recu_start
      ? `Direct original-file playback; starting at ${Math.round(requestedStart)}s. Browser Range reads only the bytes it needs.`
      : 'Direct playback from the original disk file — zero ffmpeg/transcode; browser Range reads only the bytes it needs.';
  } else if (stage === 'seekable') {
    $('preview-note').textContent = 'Preparing/using a cached zero-reencode seekable MP4 wrapper. The recording is not re-encoded; once ready, scrubbing uses normal byte-range playback like a native MP4.';
  } else if (stage === 'remux') {
    $('preview-note').textContent = 'Fast zero-reencode streaming remux. The timeline below is the original recording timeline; scrub normally and Reviewer restarts the backend at that exact source time.';
  } else {
    $('preview-note').textContent = 'Compatibility playback is active automatically. The timeline below remains the original recording timeline; scrub normally and Reviewer handles the backend restart invisibly.';
  }

  previewDiagnosticEvent(item, 'stage_start', { stage, requestedStart, desktopDiskDirect, nativeHls, src: video.src });

  const clearPreviewFallbackTimer = () => {
    if (video._ctbFallbackTimer) clearTimeout(video._ctbFallbackTimer);
    video._ctbFallbackTimer = null;
  };
  const armPreviewFallbackTimer = (ms = 12000, reason = 'timeout') => {
    clearPreviewFallbackTimer();
    video._ctbFallbackTimer = setTimeout(() => previewFailover(reason), ms);
  };
  const restartAt = (target, restartStage = stage) => {
    const duration = Number(item.duration || 0);
    const safe = Math.max(0, Number.isFinite(duration) && duration > 0 ? Math.min(Number(target || 0), Math.max(0, duration - 0.25)) : Number(target || 0));
    previewDiagnosticEvent(item, 'server_seek_restart', { fromStage: stage, toStage: restartStage, absoluteSeconds: safe });
    playPreviewItem(state.previewIndex, restartStage === 'compatibility', { stage: restartStage, startSeconds: safe });
  };
  const previewFailover = (reason = 'error') => {
    clearPreviewFallbackTimer();
    const currentStage = String(video.dataset.previewStage || stage);
    const absolute = previewAbsoluteSeconds(video, requestedStart);
    previewDiagnosticEvent(item, 'failover', { fromStage: currentStage, reason, targetSeconds: absolute, media: mediaErrorPayload(video) });
    if ((currentStage === 'direct' || currentStage === 'hls') && item.seekable_url) {
      $('preview-note').textContent = `Native playback ${reason.includes('timeout') ? 'stalled' : 'was not usable'}; preparing a seekable zero-reencode MP4 wrapper at ${formatClockSeconds(absolute)}…`;
      restartAt(absolute, 'seekable');
      return;
    }
    if (currentStage === 'seekable' && item.compatibility_url) {
      $('preview-note').textContent = `Seekable zero-reencode playback was not usable; trying compatibility playback automatically at ${formatClockSeconds(absolute)}…`;
      restartAt(absolute, 'compatibility');
      return;
    }
    if (currentStage === 'remux' && item.compatibility_url) {
      $('preview-note').textContent = `Fast streaming remux was not usable; trying compatibility playback automatically at ${formatClockSeconds(absolute)}…`;
      restartAt(absolute, 'compatibility');
      return;
    }
    video.dataset.previewTerminalFailure = '1';
    previewDiagnosticEvent(item, 'terminal_playback_failure', { stage: currentStage, reason, absoluteSeconds: absolute, media: mediaErrorPayload(video) });
    updatePreviewSourceControls(item, video);
    $('preview-note').textContent = 'Reviewer automatically tried every available playback method for this recording and none remained playable. This file stays selected here instead of skipping to the next recording. Export Preview diagnostics and send me the ZIP.';
  };

  let appliedDirectStart = stage !== 'direct' && stage !== 'hls' && stage !== 'seekable';
  const applyDirectStart = () => {
    if (appliedDirectStart) return;
    const target = requestedStart;
    if (!Number.isFinite(target) || target <= 0) { appliedDirectStart = true; return; }
    try {
      const duration = Number(video.duration);
      const safeTarget = Number.isFinite(duration) && duration > 0 ? Math.min(target, Math.max(0, duration - 0.25)) : target;
      video._ctbProgrammaticSeek = true;
      video.currentTime = safeTarget;
      appliedDirectStart = true;
    } catch (_) {}
  };

  video.onloadedmetadata = () => {
    previewDiagnosticEvent(item, 'loadedmetadata', mediaErrorPayload(video));
    // If the server could not rapidly probe a trustworthy source duration,
    // native direct/HLS metadata is authoritative. Never copy the duration of
    // a remux/compatibility pipe because that timeline starts at its offset.
    if ((stage === 'direct' || stage === 'hls' || stage === 'seekable') && Number.isFinite(Number(video.duration)) && Number(video.duration) > 0) {
      item.duration = Number(video.duration);
      item.duration_known = true;
    }
    // Do NOT clear the startup watchdog here. Some broken/unsupported files
    // expose metadata but never decode a playable frame; v2.15.4 then waited
    // forever until the user manually chose Compatibility.
    applyDirectStart();
    updatePreviewSourceControls(item, video);
    if (autoplay) video.play().catch(() => { $('preview-note').textContent += ' Tap ▶ if autoplay is blocked.'; });
  };
  video.oncanplay = () => {
    clearPreviewFallbackTimer();
    applyDirectStart();
    updatePreviewSourceControls(item, video);
    previewDiagnosticEvent(item, 'canplay', mediaErrorPayload(video));
  };
  video.onplaying = () => {
    clearPreviewFallbackTimer();
    updatePreviewSourceControls(item, video);
    previewDiagnosticEvent(item, 'playing', mediaErrorPayload(video));
  };
  video.onerror = () => previewFailover('media-error');
  video.onwaiting = () => {
    previewDiagnosticEvent(item, 'waiting', mediaErrorPayload(video));
    if (!video.paused) armPreviewFallbackTimer(stage === 'direct' ? 6500 : 10000, 'waiting-timeout');
  };
  video.onstalled = () => {
    previewDiagnosticEvent(item, 'stalled', mediaErrorPayload(video));
    if (!video.paused) armPreviewFallbackTimer(stage === 'direct' ? 6500 : 10000, 'stall-timeout');
  };
  video.onseeking = () => {
    if (video._ctbProgrammaticSeek) return;
    previewDiagnosticEvent(item, 'seeking', mediaErrorPayload(video));
    if (stage === 'direct' || stage === 'hls' || stage === 'seekable') armPreviewFallbackTimer(6000, 'seek-timeout');
  };
  video.onseeked = () => {
    clearPreviewFallbackTimer();
    const programmatic = Boolean(video._ctbProgrammaticSeek);
    video._ctbProgrammaticSeek = false;
    updatePreviewSourceControls(item, video);
    previewDiagnosticEvent(item, programmatic ? 'programmatic_seeked' : 'seeked', mediaErrorPayload(video));
  };
  video.ontimeupdate = () => updatePreviewSourceControls(item, video);
  video.onplay = () => updatePreviewSourceControls(item, video);
  video.onpause = () => updatePreviewSourceControls(item, video);
  video.onended = () => {
    clearPreviewFallbackTimer();
    updatePreviewSourceControls(item, video);
    const endState = previewReachedSourceEnd(item, video, requestedStart);
    previewDiagnosticEvent(item, endState.finished ? 'source_finished' : 'premature_stream_end', {
      stage: String(video.dataset.previewStage || stage), absoluteSeconds: endState.absolute, sourceDuration: endState.duration, tolerance: endState.tolerance
    });
    if (!endState.finished) {
      previewFailover('premature-ended');
      return;
    }
    if (video.dataset.previewTerminalFailure === '1') return;
    if (state.previewIndex + 1 < state.previewItems.length) playPreviewItem(state.previewIndex + 1, false);
  };
  video.load();
  armPreviewFallbackTimer(stage === 'hls' ? 10000 : (stage === 'direct' ? 8000 : (stage === 'seekable' ? 30000 : (stage === 'remux' ? 12000 : 20000))), 'startup-timeout');
}

function closeVideoPreview() {
  const video = $('preview-video');
  if (video._ctbFallbackTimer) clearTimeout(video._ctbFallbackTimer);
  video._ctbFallbackTimer = null;
  if (video._ctbSeekRestartTimer) clearTimeout(video._ctbSeekRestartTimer);
  video._ctbSeekRestartTimer = null;
  video.pause(); video.removeAttribute('src'); video.controls = true; video.load();
  state.previewSourceScrubbing = false;
  $('preview-source-controls').classList.add('hidden');
  state.previewItems = [];
  $('preview-modal').classList.add('hidden');
}

async function exportCurrentPreviewDiagnostics() {
  const item = state.previewItems[state.previewIndex];
  if (!item?.diagnostic_id) { toast('Re-open this file preview first so a diagnostic session can be captured.'); return; }
  try {
    previewDiagnosticEvent(item, 'diagnostic_export_requested', mediaErrorPayload($('preview-video')));
    const response = await fetch(`/api/preview-diagnostics/export?id=${encodeURIComponent(item.diagnostic_id)}`, { cache: 'no-store' });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { detail = (await response.json()).error || detail; } catch (_) {}
      throw new Error(detail);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `CTBRec_Preview_Diagnostics_${item.diagnostic_id.slice(0,12)}.zip`;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast('Preview diagnostics ZIP exported. It contains no video bytes, but it can contain model/file names and local paths.', 6500);
  } catch (error) { toast(`Could not export preview diagnostics: ${error.message}`, 7000); }
}

function draftPayload() {
  if (!state.current) return {};
  if (state.current.mode === 'original') return { keep_indices: [...state.keep] };
  if (state.current.mode === 'review') {
    if (state.frameCutMode) return { frame_cut_mode: true, frame_decisions: state.frameDecisions };
    return { frame_cut_mode: false, decisions: state.decisions };
  }
  if (state.current.mode === 'deletion') return { restore_indices: [...state.recoverSelected] };
  return { delete_indices: [...state.deleteSelected] };
}

function draftPayloadForCurrentChunk() {
  return {
    ...draftPayload(),
    _chunk_signature: String(state.current?.chunk?.signature || ''),
  };
}

function cancelPendingDraftSave() {
  clearTimeout(state.draftTimer);
  state.draftTimer = null;
}

function scheduleDraft() {
  cancelPendingDraftSave();
  const queueId = state.queueId;
  const chunkSignature = String(state.current?.chunk?.signature || '');
  const payload = draftPayloadForCurrentChunk();
  state.draftTimer = setTimeout(async () => {
    state.draftTimer = null;
    if (!queueId || !chunkSignature) return;
    try {
      await api(`/api/queues/${encodeURIComponent(queueId)}/draft`, {
        method: 'POST', body: JSON.stringify(payload),
      });
    } catch (_) {}
  }, 250);
}

async function submitCurrent() {
  try {
    cancelPendingDraftSave();
    const payload = draftPayloadForCurrentChunk();
    const created = await api(`/api/queues/${encodeURIComponent(state.queueId)}/submit`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    const result = await pollTask(created.task_id, 'Saving decisions');
    if (result.message) toast(result.message);
    await loadCurrent(true);
  } catch (error) { toast(error.message, 5000); }
}

async function confirmPermanentDelete() {
  if (!state.deletePreview) return;
  try {
    const created = await api('/api/delete-confirm', {
      method: 'POST',
      body: JSON.stringify({
        confirmation_token: state.deletePreview.confirmation_token,
        phrase: $('delete-phrase').value,
      }),
    });
    $('delete-modal').classList.add('hidden');
    const result = await pollTask(created.task_id, 'Permanently deleting selected files');
    const previewKind = state.deletePreview.kind || (state.deletePreview.model_count ? 'models' : 'files');
    state.deletePreview = null;
    toast(result.message || 'Permanent deletion complete.', 5000);
    if (previewKind === 'models' || result.kind === 'models') {
      state.deletionModelsSelected.clear();
      showView('models-view');
      await loadModels();
    } else {
      await loadCurrent(true);
    }
  } catch (error) { toast(error.message, 6000); }
}

async function skipCurrent() {
  try {
    cancelPendingDraftSave();
    const created = await api(`/api/queues/${encodeURIComponent(state.queueId)}/skip`, { method: 'POST', body: '{}' });
    const result = await pollTask(created.task_id, 'Skipping chunk');
    if (result.message) toast(result.message);
    await loadCurrent(true);
  } catch (error) { toast(error.message); }
}

async function backPrevious() {
  if (!state.queueId) return;
  cancelPendingDraftSave();
  try {
    const created = await api(`/api/queues/${encodeURIComponent(state.queueId)}/back`, { method: 'POST', body: '{}' });
    const result = await pollTask(created.task_id, 'Returning to previous chunk');
    if (result.message) toast(result.message, 5000);
    await loadCurrent(true);
  } catch (error) { toast(error.message, 6000); }
}

async function regenerate() {
  try {
    const created = await api(`/api/queues/${encodeURIComponent(state.queueId)}/regenerate`, { method: 'POST', body: '{}' });
    await pollTask(created.task_id, 'Regenerating mosaic');
    await loadCurrent(false);
  } catch (error) { toast(error.message); }
}

async function rebuildCurrentChunk() {
  if (!state.queueId || !state.current?.chunk) return;
  cancelPendingDraftSave();
  const ok = confirm('Re-scan this model from disk, reconcile the current chunk against files that actually exist now, include newly discovered files that belong in the same chunk, delete stale/duplicate mosaic artifacts for this chunk, and generate one fresh authoritative mosaic? Your unsaved choices on this chunk will be cleared.');
  if (!ok) return;
  try {
    const created = await api(`/api/queues/${encodeURIComponent(state.queueId)}/rebuild-chunk`, { method: 'POST', body: '{}' });
    const result = await pollTask(created.task_id, 'Reconciling chunk files and rebuilding mosaic');
    if (result.message) toast(result.message, 7500);
    state.keep.clear(); state.decisions = {}; state.frameDecisions = {}; state.touched.clear();
    await loadCurrent(false);
  } catch (error) { toast(error.message, 7500); }
}

async function generateAhead() {
  try {
    const created = await api(`/api/queues/${encodeURIComponent(state.queueId)}/prefetch`, { method: 'POST', body: '{}' });
    const result = await pollTask(created.task_id, 'Generating future mosaics');
    toast(result.message || 'Future mosaics are ready.');
    await refreshCurrentMetadata();
  } catch (error) { toast(error.message, 5000); }
}

async function refreshRecu() {
  const status = state.current?.recu?.status;
  if (status === 'needs_verification' || status === 'disabled') {
    await openSettings('review-view');
    return;
  }
  try {
    const created = await api(`/api/queues/${encodeURIComponent(state.queueId)}/recu-refresh`, { method: 'POST', body: '{}' });
    toast('Refreshing Recu in the background — keep sorting.', 3500);
    pollTask(created.task_id, 'Refreshing Recu kinks', { blocking: false })
      .then(() => refreshCurrentMetadata())
      .catch(error => toast(error.message, 5000));
  } catch (error) { toast(error.message, 5000); }
}

let modelAdminRequestToken = 0;

function hideModelAdminPanel() {
  const panel = $('model-admin-panel');
  state.modelAdminModel = '';
  state.modelAdminLoading = false;
  if (panel) panel.classList.add('hidden');
}

async function loadModelAdminForCurrent(force = false) {
  const c = state.current;
  const panel = $('model-admin-panel');
  if (!panel || !c || !['original','review'].includes(c.mode) || !c.model) {
    hideModelAdminPanel();
    return;
  }
  if (!force && state.modelAdminModel === c.model) return;
  state.modelAdminModel = c.model;
  state.modelAdminLoading = true;
  panel.classList.remove('hidden');
  panel.classList.add('loading');
  const controls = $('model-admin-controls');
  controls.classList.remove('hidden');
  setModelAdminControlsEnabled(false);
  $('model-admin-status').textContent = `Loading live CTBRec state for ${c.model} in the background…`;
  const token = ++modelAdminRequestToken;
  const model = c.model;
  try {
    const data = await api(`/api/model-admin?model=${encodeURIComponent(model)}`);
    if (token !== modelAdminRequestToken || state.current?.model !== model) return;
    renderModelAdmin(data);
  } catch (error) {
    if (token !== modelAdminRequestToken) return;
    setModelAdminControlsEnabled(false);
    $('model-admin-status').textContent = `CTBRec controls unavailable: ${error.message}`;
  } finally {
    if (token === modelAdminRequestToken) {
      state.modelAdminLoading = false;
      panel.classList.remove('loading');
    }
  }
}

function setModelAdminControlsEnabled(enabled) {
  const ids = [
    'model-admin-priority','model-admin-priority-save','model-admin-favorite','model-admin-easy','model-admin-hide',
    'model-admin-pause','model-admin-mark-later','model-admin-force-priority','model-admin-keep-last',
    'model-admin-keep-last-save','model-admin-alias','model-admin-alias-add','model-admin-ignore',
  ];
  ids.forEach(id => { const el = $(id); if (el) el.disabled = !enabled; });
}

function renderModelAdmin(data = {}) {
  const controls = $('model-admin-controls');
  const bridge = data.bridge || {};
  const pill = $('model-admin-live-pill');
  pill.textContent = bridge.ok ? '● CTBRec LIVE — changes apply immediately' : (bridge.initializing ? '◐ CTBRec bridge connected — recorder initializing' : '○ CTBRec LIVE unavailable — native changes blocked');
  pill.classList.toggle('ok', Boolean(bridge.ok));
  pill.classList.toggle('warn', !bridge.ok);
  controls.classList.remove('hidden');
  if (!data.found) {
    setModelAdminControlsEnabled(false);
    $('model-admin-status').textContent = data.message || 'Model metadata is unavailable.';
    return;
  }
  setModelAdminControlsEnabled(true);
  const legacy = data.legacy_models_json ? ` • legacy notes: ${data.legacy_models_json} (read only)` : '';
  const liveState = data.live_found ? `${data.priority_label || data.priority_value || 'live'}${data.online ? ' • online' : ''}${data.recording ? ' • recording' : ''}` : 'not currently tracked by live CTBRec';
  $('model-admin-status').textContent = `${liveState}${legacy}${bridge.error ? ` • ${bridge.error}` : ''}`;
  const priority = String(data.priority_tier || 'unsorted');
  if ([...$('model-admin-priority').options].some(option => option.value === priority)) $('model-admin-priority').value = priority;
  $('model-admin-easy').textContent = data.easy_sort ? '✓ Easy Sort' : 'Easy Sort';
  $('model-admin-easy').classList.toggle('active', Boolean(data.easy_sort));
  $('model-admin-hide').textContent = data.hidden ? 'Unhide' : 'Hide';
  $('model-admin-pause').textContent = data.suspended ? 'Resume' : (data.suspended_mixed ? 'Pause all' : 'Pause');
  $('model-admin-mark-later').textContent = data.marked_later ? 'Record Normally' : (data.marked_later_mixed ? 'Record Later all' : 'Record Later');
  $('model-admin-force-priority').textContent = data.force_priority ? 'Normal Priority' : (data.force_priority_mixed ? 'Force Priority all' : 'Force Priority');
  $('model-admin-keep-last').value = Number(data.keep_last_minutes || 0);
  const identities = [data.model, ...(data.aliases || []).map(row => `${row.name} [${row.site}]`)];
  $('model-admin-identities').textContent = identities.filter(Boolean).length ? `Identity group: ${[...new Set(identities.filter(Boolean))].join(' • ')}` : '';
  const links = $('model-admin-search-links'); links.innerHTML = '';
  for (const row of data.search_links || []) {
    const a = document.createElement('a'); a.href = row.url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = row.label; a.className = 'button-link secondary compact'; links.appendChild(a);
  }
  $('model-admin-panel').dataset.easySort = data.easy_sort ? '1' : '0';
  $('model-admin-panel').dataset.hiddenModel = data.hidden ? '1' : '0';
  $('model-admin-panel').dataset.suspended = data.suspended ? '1' : '0';
  $('model-admin-panel').dataset.markedLater = data.marked_later ? '1' : '0';
  $('model-admin-panel').dataset.forcePriority = data.force_priority ? '1' : '0';
  const nativeIds = ['model-admin-priority','model-admin-priority-save','model-admin-favorite','model-admin-pause','model-admin-mark-later','model-admin-force-priority','model-admin-alias','model-admin-alias-add','model-admin-ignore'];
  nativeIds.forEach(id => { const el=$(id); if (el) el.disabled = !data.native_controls_available; });
}

async function updateCurrentModelAdmin(action, extra = {}) {
  const model = state.current?.model;
  if (!model) return;
  try {
    const response = await api('/api/model-admin/update', {
      method: 'POST', body: JSON.stringify({ model, action, ...extra }),
    });
    toast(response.result?.message || `${model}: CTBRec setting updated.`, 4500);
    if (response.model) renderModelAdmin(response.model);
    if (['easy_sort','hidden','priority','suspended','mark_later','force_priority','ignore'].includes(action)) {
      invalidateModelCaches();
      // Never compete with the sorting lane for a 4k-model catalog response.
      // Refresh only when the model list is actually visible.
      if (!$('models-view').classList.contains('hidden')) loadModels().catch(()=>{});
    }
    return response;
  } catch (error) {
    toast(error.message, 6500);
    throw error;
  }
}

function workProgressText(row = {}) {
  const progress = row.progress || row.message || row.state || row.status || '';
  const current = Number(row.current || row.model_index || row.chunk_index || 0);
  const total = Number(row.total || row.total_models || row.total_chunks || 0);
  return `${progress}${total > 0 ? ` • ${current}/${total}` : ''}`;
}

async function openPcQueue(returnView = null) {
  state.queueReturnView = returnView || (state.current && !state.current.done ? 'review-view' : 'models-view');
  showView('queue-view');
  await refreshPcQueue();
}

function makeWorkRow(title, subtitle, actions = []) {
  const row = document.createElement('div'); row.className = 'work-row';
  const copy = document.createElement('div'); copy.className = 'work-copy';
  copy.innerHTML = '<strong></strong><span class="muted small"></span>';
  copy.querySelector('strong').textContent = title;
  copy.querySelector('span').textContent = subtitle || '';
  row.appendChild(copy);
  if (actions.length) {
    const box = document.createElement('div'); box.className = 'work-actions';
    actions.forEach(action => { const b=document.createElement('button'); b.className=action.className||'secondary compact'; b.textContent=action.label; b.disabled=!!action.disabled; b.onclick=action.onclick; box.appendChild(b); });
    row.appendChild(box);
  }
  return row;
}

async function refreshPcQueue() {
  if ($('queue-view').classList.contains('hidden')) return;
  try {
    const data = await api('/api/work-status');
    const heavy = $('heavy-work-list'); heavy.innerHTML = '';
    const catalog = data.catalog || {};
    heavy.appendChild(makeWorkRow('Disk-size catalog', workProgressText(catalog), [
      {label:'Cancel', className:'danger-outline compact', disabled:catalog.status !== 'scanning', onclick:()=>cancelBackground('catalog')}
    ]));
    const library = data.library || {};
    heavy.appendChild(makeWorkRow('Whole-library mosaics', workProgressText(library), [
      {label:'Cancel', className:'danger-outline compact', disabled:!library.running && !['queued','waiting','running','cancelling','paused'].includes(library.state), onclick:()=>cancelBackground('library')}
    ]));
    const nsfw = data.nsfw || {};
    heavy.appendChild(makeWorkRow('Non-NSFW detector', workProgressText(nsfw), [
      {label:'Cancel', className:'danger-outline compact', disabled:!nsfw.running && !['queued','waiting','running','cancelling','paused'].includes(nsfw.state), onclick:()=>cancelBackground('nsfw')}
    ]));

    const jobs = $('durable-job-list'); jobs.innerHTML = '';
    for (const job of data.action_jobs || []) {
      const waiting = ['queued','retrying'].includes(job.status);
      jobs.appendChild(makeWorkRow(`#${job.position} ${job.label}${job.model ? ` — ${job.model}` : ''}`, `${job.status} • ${job.message || ''}${job.error ? ` • ${job.error}` : ''}`, [
        {label:'Top', disabled:!waiting, onclick:()=>reorderJob(job.id,'top')},
        {label:'↑', disabled:!waiting, onclick:()=>reorderJob(job.id,'up')},
        {label:'↓', disabled:!waiting, onclick:()=>reorderJob(job.id,'down')},
        {label:'Bottom', disabled:!waiting, onclick:()=>reorderJob(job.id,'bottom')},
        {label:'Cancel', className:'danger-outline compact', disabled:!['queued','retrying','running'].includes(job.status), onclick:()=>cancelQueueJob(job.id)},
      ]));
    }
    if (!(data.action_jobs || []).length) jobs.appendChild(makeWorkRow('No durable actions waiting', 'Sorting/deletion/restoration instructions will appear here.'));

    const tasks = $('foreground-task-list'); tasks.innerHTML = '';
    for (const task of data.tasks || []) {
      tasks.appendChild(makeWorkRow(task.label, `${task.status} • ${workProgressText(task)}`, [
        {label:'Cancel', className:'danger-outline compact', disabled:!task.cancellable, onclick:()=>cancelTask(task.id)}
      ]));
    }
    if (!(data.tasks || []).length) tasks.appendChild(makeWorkRow('No foreground tasks', ''));
  } catch (error) { toast(error.message, 5000); }
  clearTimeout(state.queueTimer);
  state.queueTimer = setTimeout(refreshPcQueue, 1600);
}

async function cancelBackground(kind) {
  try { const r=await api('/api/background/cancel',{method:'POST',body:JSON.stringify({kind})}); toast(r.message||'Cancellation requested.'); await refreshPcQueue(); }
  catch(error){ toast(error.message,5000); }
}
async function reorderJob(jobId,direction) {
  try { await api('/api/action-queue/reorder',{method:'POST',body:JSON.stringify({job_id:jobId,direction})}); await refreshPcQueue(); }
  catch(error){ toast(error.message,5000); }
}
async function cancelQueueJob(jobId) {
  try { const r=await api('/api/action-queue/cancel',{method:'POST',body:JSON.stringify({job_id:jobId})}); toast('Queue job cancellation requested.'); await refreshPcQueue(); }
  catch(error){ toast(error.message,5000); }
}
async function cancelTask(taskId) {
  try { await api('/api/tasks/cancel',{method:'POST',body:JSON.stringify({task_id:taskId})}); await refreshPcQueue(); }
  catch(error){ toast(error.message,5000); }
}

async function openSettings(returnView = 'models-view') {
  state.settingsReturnView = returnView;
  showView('settings-view');
  try {
    const settings = await api('/api/settings');
    fillSettings(settings);
  } catch (error) { toast(error.message); }
}

function fillSettings(data) {
  state.settingsRevision = String(data.settings_revision || '');
  state.settingsLoaded = Boolean(state.settingsRevision);
  if (data.config_repair_notice) toast(data.config_repair_notice, 9000);
  const library = data.library_background || {};
  $('library-bg-enabled').checked = Boolean(library.enabled);
  $('library-original-enabled').checked = Boolean(library.original_enabled);
  $('library-review-enabled').checked = Boolean(library.review_enabled);
  $('library-pause-active').checked = Boolean(library.pause_when_active);
  $('library-idle-minutes').value = library.idle_minutes ?? 10;
  $('library-rescan-minutes').value = library.rescan_minutes ?? 120;
  $('library-settings-status').textContent = libraryStatusText(data.library_status || {});
  const scheduler = data.background_scheduler || {};
  $('background-priority').value = scheduler.priority || 'mosaics_first';
  const frameCut = data.frame_cut || {};
  $('frame-cut-idle-minutes').value = frameCut.idle_minutes ?? 10;
  const keepLast = data.keep_last || {};
  $('keep-last-file').value = keepLast.file_path || 'keeplasts.txt';
  $('keep-last-gap').value = keepLast.gap_minutes ?? 30;
  $('keep-last-grace').value = keepLast.recent_write_grace_seconds ?? 120;
  $('keep-last-status').textContent = data.keep_last_status?.message || 'Keep Last status unavailable.';
  const nsfw = data.non_nsfw_cleanup || {};
  $('nsfw-enabled').checked = nsfw.enabled !== false;
  $('nsfw-pause-active').checked = nsfw.pause_when_active !== false;
  $('nsfw-idle-minutes').value = nsfw.idle_minutes ?? 10;
  $('nsfw-rescan-minutes').value = nsfw.rescan_minutes ?? 240;
  $('nsfw-model').value = nsfw.detector_model || '640m';
  $('nsfw-spacing').value = nsfw.sample_every_seconds ?? 180;
  $('nsfw-threshold').value = nsfw.confidence_threshold ?? 0.45;
  $('nsfw-borderline').value = nsfw.borderline_confidence ?? 0.20;
  $('nsfw-batch-size').value = nsfw.batch_size ?? 3;
  $('nsfw-columns').value = nsfw.columns ?? 4;
  $('nsfw-tile-width').value = nsfw.tile_width ?? 260;
  $('nsfw-max-tiles').value = nsfw.max_tiles_per_image ?? 120;
  $('nsfw-jpeg-quality').value = nsfw.jpeg_quality ?? 84;
  $('nsfw-settings-status').textContent = nsfwStatusText(data.non_nsfw_status || {});
  const bg = data.background_mosaics || {};
  const originalBg = bg.original || bg;
  const reviewBg = bg.review || bg;
  $('original-bg-enabled').checked = Boolean(originalBg.enabled);
  $('original-bg-count').value = originalBg.upcoming_count ?? 3;
  $('original-bg-idle-only').checked = Boolean(originalBg.idle_only);
  $('original-bg-idle-minutes').value = originalBg.idle_minutes ?? 10;
  $('review-bg-enabled').checked = Boolean(reviewBg.enabled);
  $('review-bg-count').value = reviewBg.upcoming_count ?? 3;
  $('review-bg-idle-only').checked = Boolean(reviewBg.idle_only);
  $('review-bg-idle-minutes').value = reviewBg.idle_minutes ?? 10;
  const mosaic = data.mosaic || {};
  $('original-spacing').value = mosaic.sample_every_seconds ?? 300;
  $('original-columns').value = mosaic.columns ?? 3;
  $('original-tile-width').value = mosaic.tile_width ?? 480;
  $('original-max-frames').value = mosaic.max_total_frames ?? 240;
  $('original-use-existing').checked = mosaic.use_existing_mosaics !== false;
  const review = data.review || {};
  $('review-spacing').value = review.sample_every_seconds ?? 180;
  $('review-columns').value = review.columns ?? 3;
  $('review-tile-width').value = review.tile_width ?? 480;
  $('review-max-frames').value = review.max_total_frames ?? 300;
  $('review-use-existing').checked = review.use_existing_mosaics !== false;
  const liveBridge = data.live_bridge || {};
  $('live-bridge-enabled').checked = liveBridge.enabled !== false;
  $('live-bridge-auto-discover').checked = liveBridge.auto_discover !== false;
  $('live-controller-config').value = liveBridge.controller_config_path || '';
  $('live-ctbrec-dir').value = liveBridge.ctbrec_dir || '';
  $('live-bridge-port').value = liveBridge.bridge_port || 8791;
  const liveStatus = data.live_bridge_status || {};
  $('live-bridge-settings-status').textContent = liveStatus.message || (liveStatus.ok ? 'CTBRec LIVE — identity verified.' : 'Live CTBRec bridge unavailable.');
  const modelAdmin = data.model_admin || {};
  $('model-admin-enabled').checked = modelAdmin.enabled !== false;
  $('model-admin-auto-discover').checked = modelAdmin.auto_discover !== false;
  $('model-admin-paths').value = Array.isArray(modelAdmin.models_json_paths) ? modelAdmin.models_json_paths.join('\n') : (modelAdmin.models_json_paths || '');
  const modelAdminStatus = data.model_admin_status || {};
  const adminFiles = modelAdminStatus.models_json_paths || modelAdminStatus.paths || [];
  $('model-admin-settings-status').textContent = modelAdminStatus.message || (adminFiles.length ? `${adminFiles.length} legacy models.json file(s) available read-only.` : 'No legacy models.json detected; sidecars and live controls still work.');
  const recu = data.recu || {};
  $('recu-enabled').checked = Boolean(recu.enabled);
  $('recu-chrome-path').value = recu.chrome_path || '';
  $('recu-base-url').value = recu.base_url || 'https://recu.me';
  $('recu-port').value = recu.browser_debug_port || 9223;
  renderRecuSettingsStatus(data.recu_status || {});
}

function renderRecuSettingsStatus(status) {
  const pieces = [];
  pieces.push(status.chrome_found ? 'Chrome found' : 'Chrome not found');
  pieces.push(status.browser_running ? 'Recu browser running' : 'Recu browser stopped');
  pieces.push(status.session_captured ? `session captured${status.captured_at ? ` ${status.captured_at}` : ''}` : 'no captured session');
  if (status.session_verified) pieces.push(`browser verified${status.validated_at ? ` ${status.validated_at}` : ''}`);
  if (status.preferred_transport && status.preferred_transport !== 'auto') {
    const labels = { cdp: 'fast CDP', cookie_http: 'fast captured-cookie HTTP', navigation: 'verified navigation fallback' };
    pieces.push(`transport: ${labels[status.preferred_transport] || status.preferred_transport}`);
  }
  if (status.cookie_names?.length) pieces.push(`cookies: ${status.cookie_names.join(', ')}`);
  $('recu-settings-status').textContent = pieces.join(' • ');
}

function settingsPayload() {
  return {
    settings_revision: state.settingsRevision,
    library_background: {
      enabled: $('library-bg-enabled').checked,
      original_enabled: $('library-original-enabled').checked,
      review_enabled: $('library-review-enabled').checked,
      pause_when_active: $('library-pause-active').checked,
      idle_minutes: Number($('library-idle-minutes').value),
      rescan_minutes: Number($('library-rescan-minutes').value),
    },
    background_scheduler: { priority: $('background-priority').value },
    frame_cut: { idle_minutes: Number($('frame-cut-idle-minutes').value) },
    keep_last: {
      file_path: $('keep-last-file').value.trim() || 'keeplasts.txt',
      gap_minutes: Number($('keep-last-gap').value),
      recent_write_grace_seconds: Number($('keep-last-grace').value),
    },
    non_nsfw_cleanup: {
      enabled: $('nsfw-enabled').checked,
      pause_when_active: $('nsfw-pause-active').checked,
      idle_minutes: Number($('nsfw-idle-minutes').value),
      rescan_minutes: Number($('nsfw-rescan-minutes').value),
      detector_model: $('nsfw-model').value,
      sample_every_seconds: Number($('nsfw-spacing').value),
      confidence_threshold: Number($('nsfw-threshold').value),
      borderline_confidence: Number($('nsfw-borderline').value),
      batch_size: Number($('nsfw-batch-size').value),
      columns: Number($('nsfw-columns').value),
      tile_width: Number($('nsfw-tile-width').value),
      max_tiles_per_image: Number($('nsfw-max-tiles').value),
      jpeg_quality: Number($('nsfw-jpeg-quality').value),
    },
    background_mosaics: {
      original: {
        enabled: $('original-bg-enabled').checked,
        upcoming_count: Number($('original-bg-count').value),
        idle_only: $('original-bg-idle-only').checked,
        idle_minutes: Number($('original-bg-idle-minutes').value),
      },
      review: {
        enabled: $('review-bg-enabled').checked,
        upcoming_count: Number($('review-bg-count').value),
        idle_only: $('review-bg-idle-only').checked,
        idle_minutes: Number($('review-bg-idle-minutes').value),
      },
    },
    mosaic: {
      sample_every_seconds: Number($('original-spacing').value),
      columns: Number($('original-columns').value),
      tile_width: Number($('original-tile-width').value),
      max_total_frames: Number($('original-max-frames').value),
      use_existing_mosaics: $('original-use-existing').checked,
    },
    review: {
      sample_every_seconds: Number($('review-spacing').value),
      columns: Number($('review-columns').value),
      tile_width: Number($('review-tile-width').value),
      max_total_frames: Number($('review-max-frames').value),
      use_existing_mosaics: $('review-use-existing').checked,
    },
    live_bridge: {
      enabled: $('live-bridge-enabled').checked,
      auto_discover: $('live-bridge-auto-discover').checked,
      controller_config_path: $('live-controller-config').value.trim(),
      ctbrec_dir: $('live-ctbrec-dir').value.trim(),
      bridge_port: Number($('live-bridge-port').value || 8791),
    },
    model_admin: {
      enabled: $('model-admin-enabled').checked,
      auto_discover: $('model-admin-auto-discover').checked,
      models_json_paths: $('model-admin-paths').value.split(/\r?\n|;/).map(v => v.trim()).filter(Boolean),
    },
    recu: {
      enabled: $('recu-enabled').checked,
      chrome_path: $('recu-chrome-path').value.trim(),
      base_url: $('recu-base-url').value.trim(),
      browser_debug_port: Number($('recu-port').value),
      auto_refresh_current: true,
    },
  };
}

async function saveSettings() {
  try {
    if (!state.settingsLoaded || !state.settingsRevision) throw new Error('Settings are still loading. Reopen Mobile Settings before saving; nothing was changed.');
    const saved = await api('/api/settings', { method: 'POST', body: JSON.stringify(settingsPayload()) });
    fillSettings(saved);
    toast('Settings saved.');
  } catch (error) { toast(error.message, 5000); }
}

async function retryBlockedActions() {
  try {
    const result = await api('/api/action-queue/retry', { method: 'POST', body: '{}' });
    toast(result.message || 'Failed/stopped actions requeued.', 5000);
    await refreshBackgroundStatus();
    if (state.queueId) await refreshCurrentMetadata();
  } catch (error) { toast(error.message, 6000); }
}

async function runNonNsfwNow() {
  try {
    const result = await api('/api/non-nsfw/run', { method: 'POST', body: '{}' });
    toast(result.message || 'Non-NSFW background pass requested.', 5000);
    await refreshBackgroundStatus();
    fillSettings(await api('/api/settings'));
  } catch (error) { toast(error.message, 6000); }
}

async function runWholeLibraryNow() {
  try {
    const result = await api('/api/library-background/run', { method: 'POST', body: '{}' });
    toast(result.message || 'Whole-library generation requested.', 5000);
    await refreshBackgroundStatus();
    fillSettings(await api('/api/settings'));
  } catch (error) { toast(error.message, 6000); }
}

async function launchRecuBrowser() {
  try {
    const result = await api('/api/recu/launch', { method: 'POST', body: '{}' });
    toast(result.message, 6000);
    fillSettings(await api('/api/settings'));
  } catch (error) { toast(error.message, 6000); }
}

async function captureRecuSession() {
  try {
    const result = await api('/api/recu/capture', { method: 'POST', body: '{}' });
    toast(result.message, 6000);
    fillSettings(await api('/api/settings'));
    if (state.queueId && ['original','review'].includes(state.current?.mode)) {
      const created = await api(`/api/queues/${encodeURIComponent(state.queueId)}/recu-refresh`, { method: 'POST', body: '{}' });
      pollTask(created.task_id, 'Loading Recu kinks', { blocking: false }).then(() => refreshCurrentMetadata()).catch(error => toast(error.message, 5000));
    }
  } catch (error) { toast(error.message, 6000); }
}

async function testRecuSession() {
  try {
    const created = await api('/api/recu/test', { method: 'POST', body: '{}' });
    const result = await pollTask(created.task_id, 'Testing Recu access');
    toast(result.message || 'Recu access works.', 5000);
    fillSettings(await api('/api/settings'));
  } catch (error) { toast(error.message, 6000); }
}

async function clearRecuSession() {
  if (!window.confirm('Clear the captured Recu cookie/session from this PC?')) return;
  try {
    const result = await api('/api/recu/clear', { method: 'POST', body: '{}' });
    toast(result.message);
    fillSettings(await api('/api/settings'));
  } catch (error) { toast(error.message); }
}

$('model-admin-refresh').onclick = () => loadModelAdminForCurrent(true);
$('model-admin-priority-save').onclick = () => updateCurrentModelAdmin('priority', { tier: $('model-admin-priority').value });
$('model-admin-favorite').onclick = () => updateCurrentModelAdmin('priority', { tier: 'favorite' });
$('model-admin-easy').onclick = () => updateCurrentModelAdmin('easy_sort', { enabled: $('model-admin-panel').dataset.easySort !== '1' });
$('model-admin-hide').onclick = () => updateCurrentModelAdmin('hidden', { enabled: $('model-admin-panel').dataset.hiddenModel !== '1' });
$('model-admin-pause').onclick = () => updateCurrentModelAdmin('suspended', { enabled: $('model-admin-panel').dataset.suspended !== '1' });
$('model-admin-mark-later').onclick = () => updateCurrentModelAdmin('mark_later', { enabled: $('model-admin-panel').dataset.markedLater !== '1' });
$('model-admin-force-priority').onclick = () => updateCurrentModelAdmin('force_priority', { enabled: $('model-admin-panel').dataset.forcePriority !== '1' });
$('model-admin-keep-last-save').onclick = () => updateCurrentModelAdmin('keep_last', { minutes: Number($('model-admin-keep-last').value || 0) });
$('model-admin-alias-add').onclick = async () => {
  const spec = $('model-admin-alias').value.trim(); if (!spec) { toast('Enter an alias first.'); return; }
  try { await updateCurrentModelAdmin('alias', { spec }); $('model-admin-alias').value = ''; await loadModelAdminForCurrent(true); } catch (_) {}
};
$('model-admin-ignore').onclick = async () => {
  const model = state.current?.model || '';
  if (!model || !confirm(`Remove ${model} from the RUNNING CTBRec instance now, remember it as ignored in Mobile Reviewer, and queue ALL matching base/_dup recording folders for MARKED_FOR_DELETION? This does not rewrite models.json.`)) return;
  try { await updateCurrentModelAdmin('ignore', { confirm: true }); await returnToModels(); } catch (_) {}
};

window.addEventListener('keydown', event => { if (event.key === 'Shift') state.shiftKeyDown = true; });
window.addEventListener('keyup', event => { if (event.key === 'Shift') state.shiftKeyDown = false; });
window.addEventListener('blur', () => { state.shiftKeyDown = false; });

$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); $('login-error').textContent = '';
  try {
    const result = await api('/api/login', { method: 'POST', body: JSON.stringify({ pin: $('pin').value }) });
    if (result.device_token) { try { localStorage.setItem('ctbrec_device_token', result.device_token); } catch (_) {} }
    await init();
  }
  catch (error) { $('login-error').textContent = error.message; }
});

$('mode-picker').addEventListener('click', event => {
  const button = event.target.closest('button[data-mode]'); if (!button) return;
  state.mode = button.dataset.mode;
  document.querySelectorAll('#mode-picker button').forEach(el => el.classList.toggle('active', el === button));
  renderModeExplanation();
  const nextKey = modelRequestKey();
  const cached = state.modelCache.get(nextKey);
  if (cached) applyModelPayload(cached, nextKey, true);
  else { state.models = []; state.modelListKey = nextKey; renderModels(true); $('model-count').textContent = 'Loading…'; }
  loadModels({ autoRetry: true }).catch(error => toast(`Could not refresh this tab yet: ${error.message}`, 4500));
});

$('decision-mode').addEventListener('click', event => {
  const button = event.target.closest('button[data-decision]'); if (!button) return;
  state.decisionMode = button.dataset.decision;
  document.querySelectorAll('#decision-mode button').forEach(el => el.classList.toggle('active', el === button));
});

$('review-granularity').addEventListener('click', event => {
  const button = event.target.closest('button[data-granularity]'); if (!button || !state.current || state.current.mode !== 'review') return;
  const wantsFrames = button.dataset.granularity === 'frames';
  if (wantsFrames && !state.current.chunk?.frame_cut_available) {
    toast(state.current.chunk?.frame_cut_unavailable_reason || 'Regenerate this mosaic once before using Frame Cut.', 5500);
    return;
  }
  state.frameCutMode = wantsFrames;
  if (wantsFrames && state.decisionMode === 'DELETE') {
    state.decisionMode = 'Leave for review';
    document.querySelectorAll('#decision-mode button').forEach(el => el.classList.toggle('active', el.dataset.decision === state.decisionMode));
  }
  renderReviewGranularity();
  $('submit-button').textContent = wantsFrames ? 'Queue precise cuts & next' : 'Submit destinations & next';
  renderMosaics();
  renderSummary();
  renderFiles();
  scheduleDraft();
});

async function queueKeepLastAll() {
  if (!confirm("Queue Run Keep Last all? The PC will combine each model across drives, split recordings into sessions by the configured gap, keep the configured tail duration from every session, and move older complete files to MARKED_FOR_DELETION.")) return;
  try {
    const result = await api('/api/keep-last/run', { method: 'POST', body: '{}' });
    toast(result.message || `Keep Last queued for ${result.rule_count || 0} model(s).`, 6000);
  } catch (error) { toast(error.message, 6500); }
}


$('clear-review-selections').onclick = clearCurrentSelections;

$('model-sort-picker').addEventListener('click', event => {
  const button = event.target.closest('button[data-model-sort]');
  if (!button || !modelSortSupported()) return;
  state.modelSort = button.dataset.modelSort;
  saveModelSortPrefs();
  renderModels(true);
});
$('random-min-size-gb').addEventListener('input', event => {
  state.randomMinGb = event.target.value;
  saveModelSortPrefs();
  renderModels(true);
});
$('random-max-size-gb').addEventListener('input', event => {
  state.randomMaxGb = event.target.value;
  saveModelSortPrefs();
  renderModels(true);
});
$('random-reshuffle').onclick = () => {
  state.randomSeed = ((state.randomSeed * 1664525 + 1013904223) >>> 0) || 1;
  saveModelSortPrefs();
  renderModels(true);
};
$('model-search').addEventListener('input', () => renderModels(true));
$('rescan-button').onclick = async () => {
  try {
    const created = await api('/api/catalog/rescan', { method: 'POST', body: '{}' });
    await pollTask(created.task_id, 'Sizing disks and deletion queues');
    invalidateModelCaches();
    await loadModels();
  } catch (error) { toast(error.message); }
};
$('pc-queue-button').onclick = () => openPcQueue('models-view');
$('keep-last-button').onclick = queueKeepLastAll;
$('models-background-queue').onclick = () => openPcQueue('models-view');
$('manage-action-queue').onclick = () => openPcQueue('review-view');
$('settings-pc-queue').onclick = () => openPcQueue('settings-view');
$('queue-back-button').onclick = () => showView(state.queueReturnView || 'models-view');
$('queue-refresh-button').onclick = refreshPcQueue;
$('queue-retry-blocked').onclick = async () => { try { const r=await api('/api/action-queue/retry',{method:'POST',body:'{}'}); toast(r.message||'Requeued.'); refreshPcQueue(); } catch(e){toast(e.message);} };
$('library-cancel-button').onclick = () => cancelBackground('library');
$('nsfw-cancel-button').onclick = () => cancelBackground('nsfw');
$('preview-selected-button').onclick = () => openVideoPreview();
$('preview-close').onclick = closeVideoPreview;
$('preview-previous').onclick = () => playPreviewItem(state.previewIndex - 1, false);
$('preview-next').onclick = () => playPreviewItem(state.previewIndex + 1, false);
$('preview-compatibility').onclick = () => {
  const item = state.previewItems[state.previewIndex];
  const video = $('preview-video');
  const absolute = previewAbsoluteSeconds(video, item?.start_seconds || 0);
  playPreviewItem(state.previewIndex, true, { stage: 'compatibility', startSeconds: absolute, autoplay: !video.paused });
};
$('preview-export-diagnostics').onclick = exportCurrentPreviewDiagnostics;
$('preview-source-play').onclick = () => {
  const video = $('preview-video');
  if (video.paused || video.ended) video.play().catch(() => {});
  else video.pause();
};
$('preview-source-timeline').addEventListener('input', event => {
  state.previewSourceScrubbing = true;
  const item = state.previewItems[state.previewIndex];
  const value = Number(event.target.value || 0);
  $('preview-source-time').textContent = `${formatClockSeconds(value)} / ${formatClockSeconds(Number(item?.duration || 0))}`;
});
$('preview-source-timeline').addEventListener('change', event => {
  const video = $('preview-video');
  const value = Number(event.target.value || 0);
  const autoplay = !video.paused;
  state.previewSourceScrubbing = false;
  seekCurrentPreviewToAbsolute(value, autoplay);
});
$('settings-button').onclick = () => openSettings('models-view');
$('settings-back-button').onclick = () => showView(state.settingsReturnView || 'models-view');
$('settings-save-button').onclick = saveSettings;
$('library-run-now-button').onclick = runWholeLibraryNow;
$('nsfw-run-now-button').onclick = runNonNsfwNow;
$('keep-last-run-button').onclick = queueKeepLastAll;
$('models-background-refresh').onclick = refreshBackgroundStatus;
$('retry-blocked-actions').onclick = retryBlockedActions;
$('back-button').onclick = returnToModels;
$('done-models-button').onclick = returnToModels;
$('submit-button').onclick = submitCurrent;
$('previous-button').onclick = backPrevious;
$('next-model-button').onclick = nextReadyModel;
$('done-next-model-button').onclick = nextReadyModel;
$('done-back-button').onclick = backPrevious;
$('skip-button').onclick = skipCurrent;
$('regenerate-button').onclick = regenerate;
$('rebuild-chunk-button').onclick = rebuildCurrentChunk;
$('prefetch-button').onclick = generateAhead;
$('prefetch-now-button').onclick = generateAhead;
$('recu-refresh-button').onclick = refreshRecu;
$('select-all-delete').onclick = () => {
  state.recoverSelected = new Set(state.current.chunk.files.filter(file => file.restore_available).map(file => Number(file.index))); state.touched = new Set(state.recoverSelected);
  renderMosaics(); renderSummary(); renderFiles(); scheduleDraft();
};
$('clear-delete-selection').onclick = () => {
  state.recoverSelected.clear(); state.touched.clear(); renderMosaics(); renderSummary(); renderFiles(); scheduleDraft();
};
$('cleanup-select-all-delete').onclick = () => {
  if (!state.current || state.current.mode !== 'cleanup') return;
  state.deleteSelected = new Set(state.current.chunk.files.map(file => Number(file.index))); state.touched.clear();
  renderMosaics(); renderSummary(); renderFiles(); scheduleDraft();
};
$('cleanup-clear-delete').onclick = () => {
  state.deleteSelected.clear(); syncTouchedFromSelections(); renderMosaics(); renderSummary(); renderFiles(); scheduleDraft();
};
$('select-all-models').onclick = () => {
  state.deletionModelsSelected = new Set(state.deletionFilteredNames);
  renderModels();
};
$('clear-model-selection').onclick = () => {
  state.deletionModelsSelected.clear();
  renderModels();
};
$('delete-selected-models').onclick = previewSelectedDeletionModels;
$('delete-cancel').onclick = () => { state.deletePreview = null; $('delete-modal').classList.add('hidden'); };
$('delete-confirm').onclick = confirmPermanentDelete;
$('recu-launch-button').onclick = launchRecuBrowser;
$('recu-capture-button').onclick = captureRecuSession;
$('recu-test-button').onclick = testRecuSession;
$('recu-clear-button').onclick = clearRecuSession;

init().catch(() => {});
