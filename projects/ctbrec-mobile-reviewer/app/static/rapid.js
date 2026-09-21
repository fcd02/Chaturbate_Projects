/* CTBRec Mobile Reviewer 2.8 rapid/offline layer.
 * Keeps the iPhone UI latency-first: prepared queues open instantly, submits are
 * atomically persisted to the PC action queue without task polling, and offline
 * packs use IndexedDB + Cache Storage for later sync.
 */

state.offlineMode = false;
state.offlineBundles = [];
state.offlineHistory = [];
state.pendingModelTasks = new Map();
state.preparedQueuesByModel = new Map();

const RAPID_DB = 'ctbrec-mobile-v27';
const RAPID_DB_VERSION = 1;
const OFFLINE_CACHE = 'ctbrec-offline-mosaics-v27';

function rapidDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(RAPID_DB, RAPID_DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains('bundles')) db.createObjectStore('bundles', { keyPath: 'work_id' });
      if (!db.objectStoreNames.contains('outbox')) db.createObjectStore('outbox', { keyPath: 'client_action_id' });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function idbAll(storeName) {
  const db = await rapidDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readonly');
    const request = tx.objectStore(storeName).getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
    tx.oncomplete = () => db.close();
  });
}

async function idbPut(storeName, value) {
  const db = await rapidDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    tx.objectStore(storeName).put(value);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { const err = tx.error; db.close(); reject(err); };
  });
}

async function idbDelete(storeName, key) {
  const db = await rapidDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    tx.objectStore(storeName).delete(key);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { const err = tx.error; db.close(); reject(err); };
  });
}

async function updateOfflineStatus() {
  try {
    const [bundles, outbox] = await Promise.all([idbAll('bundles'), idbAll('outbox')]);
    const status = $('offline-status');
    if (status) {
      status.textContent = `${bundles.length} offline chunk${bundles.length === 1 ? '' : 's'} cached • ${outbox.length} decision${outbox.length === 1 ? '' : 's'} waiting to sync${navigator.onLine ? '' : ' • OFFLINE'}`;
    }
    if ($('open-offline-pack')) $('open-offline-pack').textContent = `Offline pack (${bundles.length})`;
    if ($('sync-offline-button')) $('sync-offline-button').textContent = outbox.length ? `Sync queued (${outbox.length})` : 'Sync queued';
  } catch (_) {}
}

async function registerRapidServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  try { await navigator.serviceWorker.register('/service-worker.js', { scope: '/' }); }
  catch (error) { console.warn('Service worker registration failed', error); }
}

function initializeSelectionsFromCurrent(current) {
  const priorFrameCutMode = Boolean(state.frameCutMode);
  state.lastFileSelectionIndex = null;
  state.lastFrameSelectionIndex = null;
  state.keep = new Set((current.draft?.keep_indices || []).map(Number));
  state.recoverSelected = new Set((current.draft?.restore_indices || []).map(Number));
  state.decisions = current.draft?.decisions || {};
  state.frameDecisions = current.draft?.frame_decisions || {};
  const draftHasGranularity = Object.prototype.hasOwnProperty.call(current.draft || {}, 'frame_cut_mode');
  state.frameCutMode = current.mode === 'review' && (draftHasGranularity ? Boolean(current.draft.frame_cut_mode) : priorFrameCutMode);
  const defaultDelete = current.mode === 'cleanup' && !Array.isArray(current.draft?.delete_indices)
    ? (current.chunk.files || []).map(file => Number(file.index))
    : (current.draft?.delete_indices || []).map(Number);
  state.deleteSelected = new Set(defaultDelete);
  if (current.mode === 'review') {
    for (const file of current.chunk.files || []) {
      if (!state.decisions[String(file.index)]) state.decisions[String(file.index)] = 'Leave for review';
    }
  }
  // Never carry any selection/Shift anchor across chunks/models. Reconstruct
  // only this chunk's saved draft (Back can still restore its own draft).
  if (typeof syncTouchedFromSelections === 'function') syncTouchedFromSelections();
  else state.touched = new Set();
}

function applyOnlineCurrent(current, resetSelections = true) {
  state.offlineMode = false;
  const previousSignature = String(state.current?.chunk?.signature || '');
  const nextSignature = String(current?.chunk?.signature || '');
  const chunkChanged = Boolean(nextSignature && nextSignature !== previousSignature);
  state.current = current;
  state.frameTilesSignature = '';
  state.frameTilesCache = [];
  state.frameTilesByFile = new Map();
  state.frameTilesByIndex = new Map();
  if (current.done) {
    if (typeof releaseReviewDom === 'function') releaseReviewDom();
    $('done-back-button').disabled = !current.can_back;
    showView('done-view');
    return;
  }
  if (resetSelections) initializeSelectionsFromCurrent(current);
  showView('review-view');
  renderCurrent();
  if (chunkChanged && typeof scrollReviewToTop === 'function') scrollReviewToTop();
  scheduleStatusPoll();
}

function offlineCurrent(bundle, index, total) {
  return {
    done: false,
    offline: true,
    mode: bundle.mode,
    model: bundle.model,
    remaining: total - index,
    initial_count: total,
    can_back: state.offlineHistory.length > 0,
    notes: ['Stored locally on this iPhone.'],
    draft: {},
    recu: bundle.recu || { status: 'offline-cache', error: '', segments: {}, unmatched: [] },
    prefetch: { state: 'offline', message: 'Offline pack — no PC work is required to view this mosaic.' },
    mosaic_status: { state: 'ready', message: 'Cached on this iPhone.' },
    action_queue: { pending: 0, blocked: 0, last_message: 'Offline decisions will sync later.' },
    library_background: { state: 'offline', message: 'PC status unavailable while offline.' },
    chunk: bundle.chunk,
    work_id: bundle.work_id,
    work_token: bundle.work_token,
  };
}

async function renderOfflineBundle(resetSelections = true) {
  if (!state.offlineBundles.length) {
    state.offlineMode = false;
    showView('models-view');
    toast('No offline mosaics remain.');
    await updateOfflineStatus();
    return;
  }
  state.offlineMode = true;
  const bundle = state.offlineBundles[0];
  state.current = offlineCurrent(bundle, 0, state.offlineBundles.length);
  if (resetSelections) initializeSelectionsFromCurrent(state.current);
  showView('review-view');
  renderCurrent();
  $('review-meta').textContent = `OFFLINE • ${bundle.chunk.size} • ${bundle.chunk.file_count} segments • ${state.offlineBundles.length} cached chunks remain`;
  $('back-button').textContent = '← Offline pack';
  $('prefetch-panel').classList.add('hidden');
  $('action-queue-panel').classList.add('hidden');
  $('ready-hopper').classList.add('hidden');
  if (bundle.mode === 'original') {
    $('recu-status-text').textContent = 'Offline: kink markers are limited to whatever was already cached when this pack was downloaded.';
  }
}

async function enterOfflinePack() {
  try {
    const bundles = await idbAll('bundles');
    bundles.sort((a, b) => String(a.downloaded_at || '').localeCompare(String(b.downloaded_at || '')));
    if (!bundles.length) { toast('No offline mosaics are downloaded yet.', 5000); return; }
    state.offlineBundles = bundles;
    state.offlineHistory = [];
    state.queueId = null;
    await renderOfflineBundle(true);
  } catch (error) { toast(`Offline pack error: ${error.message}`, 6000); }
}

state.offlinePackSelected = new Set();
state.offlinePackRenderLimit = 160;
state.offlinePackObserver = null;

function offlinePackReadyModels() {
  // v2.8: every model in the current Originals/Review drive scope is eligible.
  // ready_chunks is only a speed hint; choosing an unready model promotes its
  // missing mosaics to highest PC priority instead of hiding it from the user.
  return (state.models || [])
    .slice()
    .sort((a,b) => Number(b.bytes || 0) - Number(a.bytes || 0) || Number(b.ready_chunks || 0) - Number(a.ready_chunks || 0));
}

function renderOfflinePackModels(resetWindow = false) {
  const holder = $('offline-pack-models');
  if (!holder) return;
  const rows = offlinePackReadyModels();
  if (resetWindow) state.offlinePackRenderLimit = 160;
  if (state.offlinePackObserver) { state.offlinePackObserver.disconnect(); state.offlinePackObserver = null; }
  holder.innerHTML = '';
  if (!rows.length) {
    holder.innerHTML = '<p class="muted">No models are in the current drive scope. Rescan disks if recordings recently changed.</p>';
    return;
  }
  const visible = rows.slice(0, Math.min(rows.length, state.offlinePackRenderLimit));
  const fragment = document.createDocumentFragment();
  for (const model of visible) {
    const button = document.createElement('button');
    const selected = state.offlinePackSelected.has(model.name);
    const ready = Number(model.ready_chunks || 0);
    button.className = `offline-model-option ${selected ? 'selected' : ''}`;
    button.innerHTML = `<span><strong>${escapeHtml(model.name)}</strong><small>${escapeHtml(model.size || '')}</small></span><span>${ready ? `${ready} ready` : 'will prepare'}</span>`;
    button.onclick = () => {
      if (state.offlinePackSelected.has(model.name)) state.offlinePackSelected.delete(model.name);
      else state.offlinePackSelected.add(model.name);
      button.classList.toggle('selected', state.offlinePackSelected.has(model.name));
    };
    fragment.appendChild(button);
  }
  holder.appendChild(fragment);
  if (visible.length < rows.length) {
    const sentinel = document.createElement('div');
    sentinel.className = 'model-list-sentinel muted small';
    sentinel.textContent = `Showing ${visible.length.toLocaleString()} of ${rows.length.toLocaleString()} — scroll for more`;
    holder.appendChild(sentinel);
    state.offlinePackObserver = new IntersectionObserver(entries => {
      if (!entries.some(entry => entry.isIntersecting)) return;
      state.offlinePackRenderLimit = Math.min(rows.length, state.offlinePackRenderLimit + 160);
      renderOfflinePackModels(false);
    }, { root: holder, rootMargin: '600px 0px' });
    state.offlinePackObserver.observe(sentinel);
  }
}

async function openOfflinePackBuilder() {
  if (state.mode === 'deletion' || state.mode === 'cleanup') {
    toast(state.mode === 'cleanup'
      ? 'Offline packs currently contain Originals/Review work. Cleanup mosaics remain PC-connected.'
      : 'Deletion recovery requires a connection to the PC so restore paths can be revalidated.');
    return;
  }
  try {
    const settings = await api('/api/settings');
    const speed = settings.speed_mode || {};
    $('offline-pack-limit').value = speed.offline_pack_chunks || 12;
    $('offline-pack-max-mb').value = speed.offline_pack_max_mb || 750;
    const rows = offlinePackReadyModels();
    const alreadyReady = rows.filter(row => Number(row.ready_chunks || 0) > 0);
    const defaults = (alreadyReady.length ? alreadyReady : rows).slice(0, Math.min(3, rows.length));
    state.offlinePackSelected = new Set(defaults.map(row => row.name));
    renderOfflinePackModels(true);
    $('offline-pack-modal').classList.remove('hidden');
  } catch (error) { toast(`Could not open offline-pack builder: ${error.message}`, 6500); }
}

async function cachePreparedOfflinePack(pack, selected) {
  try {
    if (!pack?.bundles?.length) {
      toast('The selected models did not produce any eligible offline mosaics. Check PC Queue for preparation errors.', 7000);
      await updateOfflineStatus();
      return;
    }
    if (navigator.storage?.persist) { try { await navigator.storage.persist(); } catch (_) {} }
    const cache = await caches.open(OFFLINE_CACHE);
    let completed = 0;
    let images = 0;
    $('offline-status').textContent = `PC preparation complete • downloading ${pack.count} chunk(s) from ${selected.length} selected model(s)…`;
    for (const bundle of pack.bundles) {
      for (const part of bundle.chunk.parts || []) {
        const response = await fetch(part.url, { cache: 'no-store' });
        if (!response.ok) throw new Error(`Could not cache mosaic (${response.status})`);
        await cache.put(part.url, response.clone());
        images++;
      }
      bundle.downloaded_at = new Date().toISOString();
      await idbPut('bundles', bundle);
      completed++;
      $('offline-status').textContent = `Downloaded ${completed}/${pack.count} chunk(s) • ${images} mosaic image(s)…`;
    }
    toast(`Offline pack ready: ${completed} chunk(s) from your selected models, about ${pack.estimated_download_size}.`, 6500);
    await updateOfflineStatus();
  } catch (error) {
    toast(`Offline download failed after PC preparation: ${error.message}`, 7000);
    await updateOfflineStatus();
  }
}

async function downloadOfflinePackConfigured() {
  const selected = [...state.offlinePackSelected];
  if (!selected.length) { toast('Choose at least one model.'); return; }
  try {
    const limit = Math.max(1, Math.min(100, Number($('offline-pack-limit').value || 12)));
    const maxMb = Math.max(50, Number($('offline-pack-max-mb').value || 750));
    $('offline-pack-download-confirm').disabled = true;
    $('offline-pack-download-confirm').textContent = 'Prioritizing…';
    const created = await api('/api/offline/prepare', {
      method: 'POST',
      body: JSON.stringify({
        mode: effectiveMode(),
        drives: [...state.drives],
        models: selected,
        limit,
        max_mb: maxMb,
      }),
    });
    state.offlinePackTaskId = created.task_id;
    $('offline-pack-modal').classList.add('hidden');
    $('offline-status').textContent = `Highest priority on PC: preparing offline work for ${selected.length} selected model(s). You can keep sorting while this runs.`;
    toast('Offline-pack preparation was promoted above background mosaics/Non-NSFW work. Keep using the app; download starts automatically when ready.', 7000);
    silentPollTask(created.task_id, result => {
      state.offlinePackTaskId = null;
      cachePreparedOfflinePack(result, selected);
      loadModels().catch(() => {});
    });
  } catch (error) {
    toast(`Could not start offline preparation: ${error.message}`, 7000);
  } finally {
    $('offline-pack-download-confirm').disabled = false;
    $('offline-pack-download-confirm').textContent = 'Prepare + download selected models';
  }
}

async function downloadOfflinePack() { return openOfflinePackBuilder(); }

async function syncOfflineOutbox(silent = false) {
  if (!navigator.onLine) { if (!silent) toast('No connection yet. Decisions remain safely stored on this iPhone.'); return; }
  let outbox;
  try { outbox = await idbAll('outbox'); }
  catch (error) { if (!silent) toast(error.message); return; }
  outbox.sort((a,b) => String(a.created_at).localeCompare(String(b.created_at)));
  let synced = 0;
  for (const item of outbox) {
    try {
      await api('/api/offline/sync', { method: 'POST', body: JSON.stringify(item) });
      await idbDelete('outbox', item.client_action_id);
      try {
        const cache = await caches.open(OFFLINE_CACHE);
        for (const url of item.part_urls || []) await cache.delete(url);
      } catch (_) {}
      synced++;
    } catch (error) {
      item.last_error = error.message;
      await idbPut('outbox', item);
      if (!silent) toast(`Sync paused: ${error.message}`, 7000);
      break;
    }
  }
  if (synced && !silent) toast(`Synced ${synced} offline decision${synced === 1 ? '' : 's'} into the durable PC queue.`, 5000);
  await updateOfflineStatus();
}

async function submitOfflineCurrent() {
  const bundle = state.offlineBundles[0];
  if (!bundle) return;
  const item = {
    client_action_id: (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`),
    work_token: bundle.work_token,
    work_id: bundle.work_id,
    part_urls: (bundle.chunk.parts || []).map(part => part.url),
    model: bundle.model,
    mode: bundle.mode,
    draft: draftPayload(),
    created_at: new Date().toISOString(),
  };
  await idbPut('outbox', item);
  await idbDelete('bundles', bundle.work_id);
  state.offlineHistory.push({ kind: 'submit', bundle, client_action_id: item.client_action_id });
  state.offlineBundles.shift();
  await updateOfflineStatus();
  // A decision is durable on the iPhone before this screen advances.
  await renderOfflineBundle(true);
  syncOfflineOutbox(true).catch(() => {});
}

async function skipOfflineCurrent() {
  if (state.offlineBundles.length <= 1) { toast('This is the only offline chunk left.'); return; }
  const skipped = state.offlineBundles.shift();
  state.offlineBundles.push(skipped);
  state.offlineHistory.push({ kind: 'skip', bundle: skipped });
  await renderOfflineBundle(true);
}

async function backOffline() {
  const entry = state.offlineHistory.pop();
  if (!entry) { toast('No previous offline chunk in this session.'); return; }
  if (entry.kind === 'submit') {
    // Back is guaranteed only while the decision has not synced to the PC.
    const outbox = await idbAll('outbox');
    if (!outbox.some(item => item.client_action_id === entry.client_action_id)) {
      state.offlineHistory.push(entry);
      toast('That decision already synced to the PC. Reconnect and use the normal Back button to undo it safely.', 7000);
      return;
    }
    await idbDelete('outbox', entry.client_action_id);
    await idbPut('bundles', entry.bundle);
  }
  state.offlineBundles = state.offlineBundles.filter(bundle => bundle.work_id !== entry.bundle.work_id);
  state.offlineBundles.unshift(entry.bundle);
  await updateOfflineStatus();
  await renderOfflineBundle(true);
}

async function readySuggestions(excludeModel = '') {
  if (!navigator.onLine || state.offlineMode || state.mode === 'deletion') return [];
  const drives = [...state.drives].join(',');
  try {
    const data = await api(`/api/ready-suggestions?mode=${encodeURIComponent(effectiveMode())}&drives=${encodeURIComponent(drives)}&exclude=${encodeURIComponent(excludeModel)}&limit=8`);
    return data.suggestions || [];
  } catch (_) { return []; }
}

async function refreshReadyHopper() {
  const holder = $('ready-hopper');
  const list = $('ready-hopper-list');
  if (!holder || !list || state.offlineMode || !state.current || state.current.chunk?.parts?.length) {
    if (holder) holder.classList.add('hidden');
    return;
  }
  const suggestions = await readySuggestions(state.current.model);
  list.innerHTML = '';
  if (!suggestions.length) { holder.classList.add('hidden'); return; }
  holder.classList.remove('hidden');
  for (const row of suggestions) {
    const button = document.createElement('button');
    button.className = 'ready-jump';
    button.innerHTML = `<strong>${row.name}</strong><span>${row.ready_chunks} ready chunk${row.ready_chunks === 1 ? '' : 's'} • ${row.ready_size}</span>`;
    button.onclick = () => openModel(row.name);
    list.appendChild(button);
  }
}

const baseRenderMosaicsRapid = renderMosaics;
renderMosaics = function renderMosaicsRapid() {
  baseRenderMosaicsRapid();
  if (!state.offlineMode) refreshReadyHopper().catch(() => {});
};

const baseRenderModelsRapid = renderModels;
renderModels = function renderModelsRapid(resetWindow = false) {
  baseRenderModelsRapid(resetWindow);
  const rapid = $('rapid-tools');
  if (rapid) rapid.classList.toggle('hidden', state.mode === 'deletion' || state.mode === 'cleanup');
  // v2.13: never rescan the entire 4k+ model array once per visible card.
  // Resolve each progressively-rendered card by its data-model-name in O(1).
  const byName = new Map((state.models || []).map(model => [String(model.name || '').toLowerCase(), model]));
  document.querySelectorAll('#model-list .model-card[data-model-name]').forEach(card => {
    const model = byName.get(String(card.dataset.modelName || '').toLowerCase());
    if (!model || !Number(model.ready_chunks || 0)) return;
    const sub = card.querySelector('.model-sub');
    if (sub && !sub.textContent.toLowerCase().includes('ready')) sub.textContent += ` • ${model.ready_chunks} ready`;
    card.classList.add('has-ready-work');
  });
  updateOfflineStatus();
};

async function silentPollTask(taskId, onDone) {
  while (true) {
    try {
      const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
      if (task.status === 'done') { onDone?.(task.result || {}); return; }
      if (task.status === 'error') { toast(task.error || 'Background preparation failed.', 6000); return; }
      if (task.status === 'cancelled') { toast(task.progress || 'Background task cancelled.', 4500); return; }
    } catch (_) { return; }
    await new Promise(resolve => setTimeout(resolve, 1200));
  }
}

openDeletionModel = async function openDeletionModelRapid(name) {
  state.offlineMode = false;
  const key = `deletion:${name}`;
  try {
    const cachedQueue = state.preparedQueuesByModel.get(key);
    if (cachedQueue) {
      state.queueId = cachedQueue;
      state.preparedQueuesByModel.delete(key);
      await loadCurrent(true);
      return;
    }
    if (state.pendingModelTasks.has(key)) {
      toast(`${name} recovery mosaics are still being prepared on the PC. You can keep browsing/selecting deletion models meanwhile.`, 5000);
      return;
    }
    const created = await api('/api/queues/load', {
      method: 'POST', body: JSON.stringify({ mode: 'deletion', model: name, drives: [...state.drives] }),
    });
    state.pendingModelTasks.set(key, created.task_id);
    toast(`Preparing ${name} recovery mosaics on the PC. The phone stays free; tap it again when it reports ready.`, 6500);
    silentPollTask(created.task_id, result => {
      state.pendingModelTasks.delete(key);
      if (result.queue_id) state.preparedQueuesByModel.set(key, result.queue_id);
      toast(`${name} deletion recovery is ready — tap Inspect / restore again to open it.`, 6500);
    });
  } catch (error) { state.pendingModelTasks.delete(key); toast(error.message, 6000); }
};

openModel = async function openModelRapid(name, options = {}) {
  if (effectiveMode() === 'deletion' || !name || state.openingModelName) return;
  cancelPendingDraftSave();
  state.offlineMode = false;
  $('back-button').textContent = '← Models';
  state.openingModelName = name;
  if (!options.preserveOrigin) {
    state.lastModelName = name;
    state.modelListScrollY = window.scrollY;
  }
  try {
    const result = await api('/api/queues/open-fast', {
      method: 'POST', timeoutMs: 0,
      body: JSON.stringify({ mode: effectiveMode(), model: name, drives: [...state.drives] }),
    });
    if (!result.queue_id) throw new Error(result.reason || `Could not open ${name}.`);
    state.queueId = result.queue_id;
    applyOnlineCurrent(result.current, true);
    const ready = Number(result.ready_chunks || 0);
    if (result.pending || !ready) {
      toast(`${name} is open. Its first mosaic is generating now; automatic look-ahead will fill the configured ready buffer for this model only.`, 6500);
    } else {
      toast(`${name}: ${ready} ready mosaic${ready === 1 ? '' : 's'} opened. Automatic look-ahead will keep the configured ready buffer filled while this model stays open.`, 4800);
    }
  } catch (error) {
    toast(error.message, 6000);
  } finally {
    if (state.openingModelName === name) state.openingModelName = '';
  }
};

submitCurrent = async function submitCurrentRapid() {
  if (state.offlineMode) {
    try { await submitOfflineCurrent(); } catch (error) { toast(error.message, 6000); }
    return;
  }
  try {
    cancelPendingDraftSave();
    const result = await api(`/api/queues/${encodeURIComponent(state.queueId)}/submit-fast`, {
      method: 'POST', body: JSON.stringify(draftPayloadForCurrentChunk()),
    });
    if (result.message) toast(result.message, 2600);
    applyOnlineCurrent(result.current, true);
  } catch (error) { toast(error.message, 6000); }
};

skipCurrent = async function skipCurrentRapid() {
  if (state.offlineMode) { await skipOfflineCurrent(); return; }
  try {
    cancelPendingDraftSave();
    const result = await api(`/api/queues/${encodeURIComponent(state.queueId)}/skip-fast`, { method: 'POST', body: '{}' });
    if (result.message) toast(result.message, 2200);
    applyOnlineCurrent(result.current, true);
  } catch (error) { toast(error.message, 5000); }
};

backPrevious = async function backPreviousRapid() {
  if (state.offlineMode) { await backOffline(); return; }
  if (!state.queueId) return;
  try {
    cancelPendingDraftSave();
    const result = await api(`/api/queues/${encodeURIComponent(state.queueId)}/back-fast`, { method: 'POST', body: '{}' });
    if (result.message) toast(result.message, 4500);
    applyOnlineCurrent(result.current, true);
  } catch (error) { toast(error.message, 6000); }
};

previewSelectedDeletionModels = async function previewSelectedDeletionModelsRapid() {
  try {
    const preview = await api('/api/deletion-models/preview', {
      method: 'POST', body: JSON.stringify({ models: [...state.deletionModelsSelected], drives: [...state.drives] }),
    });
    state.deletePreview = preview;
    $('delete-modal-summary').textContent = `${preview.model_count} selected model(s), about ${preview.size} from the latest disk catalog. Exact files will be enumerated later by the PC queue.`;
    $('delete-phrase').value = '';
    $('delete-modal').classList.remove('hidden');
    setTimeout(() => $('delete-phrase').focus(), 100);
  } catch (error) { toast(error.message, 6000); }
};

confirmPermanentDelete = async function confirmPermanentDeleteRapid() {
  if (!state.deletePreview) return;
  try {
    const result = await api('/api/delete-confirm', {
      method: 'POST',
      body: JSON.stringify({ confirmation_token: state.deletePreview.confirmation_token, phrase: $('delete-phrase').value }),
    });
    $('delete-modal').classList.add('hidden');
    const previewKind = state.deletePreview.kind || (state.deletePreview.model_count ? 'models' : 'files');
    state.deletePreview = null;
    if (result.queued && previewKind === 'models') {
      toast(result.message || 'Permanent deletion queued on the PC.', 5000);
      state.deletionModelsSelected.clear();
      await loadModels();
      await refreshBackgroundStatus();
      return;
    }
    // Legacy file-level deletion path remains supported.
    if (result.task_id) {
      const done = await pollTask(result.task_id, 'Permanently deleting selected files');
      toast(done.message || 'Permanent deletion complete.', 5000);
      if (previewKind === 'models' || done.kind === 'models') { state.deletionModelsSelected.clear(); showView('models-view'); await loadModels(); }
      else await loadCurrent(true);
    }
  } catch (error) { toast(error.message, 7000); }
};

regenerate = async function regenerateRapid() {
  if (state.current?.mode === 'cleanup') { toast('Cleanup mosaics are rebuilt by the detector pass so their classification and image stay in sync. Tap Scan instead.', 5500); return; }
  if (state.offlineMode) { toast('Redo is unavailable offline because it requires FFmpeg on the PC.'); return; }
  try {
    await api(`/api/queues/${encodeURIComponent(state.queueId)}/regenerate`, { method: 'POST', body: '{}' });
    toast('Redo queued on the PC. You can switch to another ready model instead of waiting.', 5000);
    setTimeout(() => refreshCurrentMetadata(), 900);
  } catch (error) { toast(error.message, 5000); }
};

generateAhead = async function generateAheadRapid() {
  if (state.offlineMode) { toast('This mosaic is already cached offline.'); return; }
  try {
    if (state.current?.mode === 'cleanup') {
      await api('/api/non-nsfw/run', { method: 'POST', body: '{}' });
      toast('Non-NSFW detector pass queued. Existing prepared cleanup models remain sortable now.', 5000);
      setTimeout(() => refreshCurrentMetadata(), 900);
      return;
    }
    await api(`/api/queues/${encodeURIComponent(state.queueId)}/prefetch`, { method: 'POST', body: '{}' });
    toast('Future mosaic generation queued. Keep sorting anything already ready.', 4500);
    setTimeout(() => refreshCurrentMetadata(), 900);
  } catch (error) { toast(error.message, 5000); }
};

refreshRecu = async function refreshRecuRapid() {
  if (state.offlineMode) { toast('Recu refresh requires a connection to the PC.'); return; }
  const status = state.current?.recu?.status;
  if (status === 'needs_verification' || status === 'disabled') { await openSettings('review-view'); return; }
  try {
    await api(`/api/queues/${encodeURIComponent(state.queueId)}/recu-refresh`, { method: 'POST', body: '{}' });
    toast('Recu refresh queued in the background.', 3500);
    setTimeout(() => refreshCurrentMetadata(), 1200);
  } catch (error) { toast(error.message, 5000); }
};

// Add speed settings without duplicating the base settings form logic.
const baseFillSettingsRapid = fillSettings;
fillSettings = function fillSettingsRapid(data) {
  baseFillSettingsRapid(data);
  const speed = data.speed_mode || {};
  if ($('speed-ready-suggestions')) $('speed-ready-suggestions').value = speed.ready_suggestions ?? 8;
  if ($('speed-offline-chunks')) $('speed-offline-chunks').value = speed.offline_pack_chunks ?? 12;
  if ($('speed-offline-max-mb')) $('speed-offline-max-mb').value = speed.offline_pack_max_mb ?? 750;
};
const baseSettingsPayloadRapid = settingsPayload;
settingsPayload = function settingsPayloadRapid() {
  const payload = baseSettingsPayloadRapid();
  payload.speed_mode = {
    ready_suggestions: Number($('speed-ready-suggestions')?.value || 8),
    offline_pack_chunks: Number($('speed-offline-chunks')?.value || 12),
    offline_pack_max_mb: Number($('speed-offline-max-mb')?.value || 750),
  };
  return payload;
};

// Replace captured event-handler references with the rapid versions.
$('submit-button').onclick = submitCurrent;
$('skip-button').onclick = skipCurrent;
$('previous-button').onclick = backPrevious;
$('done-back-button').onclick = backPrevious;
$('regenerate-button').onclick = regenerate;
$('prefetch-button').onclick = generateAhead;
$('prefetch-now-button').onclick = generateAhead;
$('recu-refresh-button').onclick = refreshRecu;
$('delete-selected-models').onclick = previewSelectedDeletionModels;
$('delete-confirm').onclick = confirmPermanentDelete;
$('download-offline-pack').onclick = openOfflinePackBuilder;
$('offline-pack-close').onclick = () => $('offline-pack-modal').classList.add('hidden');
$('offline-pack-select-all').onclick = () => { state.offlinePackSelected = new Set(offlinePackReadyModels().map(row => row.name)); renderOfflinePackModels(); };
$('offline-pack-clear').onclick = () => { state.offlinePackSelected.clear(); renderOfflinePackModels(); };
$('offline-pack-download-confirm').onclick = downloadOfflinePackConfigured;
$('open-offline-pack').onclick = enterOfflinePack;
$('sync-offline-button').onclick = () => syncOfflineOutbox(false);

// Disk rescans are also fire-and-continue in Rapid mode. The existing catalog
// remains usable while the PC refreshes it; the user never has to watch the scan.
$('rescan-button').onclick = async () => {
  if (state.offlineMode) { toast('Disk rescan requires a connection to the PC.'); return; }
  try {
    const created = await api('/api/catalog/rescan', { method: 'POST', body: '{}' });
    toast('Disk rescan queued on the PC. The current catalog stays usable.', 4500);
    const taskId = created.task_id;
    if (taskId) {
      const watch = async () => {
        try {
          const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
          if (task.status === 'done') { await loadModels(); return; }
          if (task.status === 'error') { toast(`Background rescan failed: ${task.error || 'unknown error'}`, 6500); return; }
          if (task.status === 'cancelled') { toast('Background rescan cancelled; the previous catalog is still usable.', 4500); return; }
          setTimeout(watch, 1800);
        } catch (_) { setTimeout(watch, 3000); }
      };
      setTimeout(watch, 1800);
    }
  } catch (error) { toast(error.message, 5000); }
};

// Offline Back-to-models should stay within the cached pack when possible.
const baseBackModels = $('back-button').onclick;
$('back-button').onclick = async () => {
  if (state.offlineMode) {
    state.offlineMode = false;
    state.current = null;
    $('back-button').textContent = '← Models';
    try { showView('models-view'); await updateOfflineStatus(); }
    catch (_) { await enterOfflinePack(); }
    return;
  }
  baseBackModels?.();
};

window.addEventListener('online', () => { updateOfflineStatus(); syncOfflineOutbox(true).catch(() => {}); });
window.addEventListener('offline', () => updateOfflineStatus());

registerRapidServiceWorker();
updateOfflineStatus();
setTimeout(() => syncOfflineOutbox(true).catch(() => {}), 1200);
// If the shell loaded from the service worker but the PC/Tailscale endpoint is
// unavailable, automatically fall back to the locally cached pack.
setTimeout(async () => {
  const loginVisible = !$('login-view').classList.contains('hidden');
  const networkish = !navigator.onLine || /fetch|network|offline|failed/i.test($('login-error').textContent || '');
  if (loginVisible && networkish) {
    const bundles = await idbAll('bundles').catch(() => []);
    if (bundles.length) enterOfflinePack();
  }
}, 800);
if (state.models?.length) renderModels();
