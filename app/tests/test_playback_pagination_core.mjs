// Playback "Load older recordings" pagination reliability (2026-09-14
// Playback phase): tests for the two real, non-hypothetical client-side
// defects found and fixed in the exact deployed source --
//
// 1. fetchClipsMetadata() previously returned [] for BOTH a genuine
//    fetch failure (network error / non-2xx response) and a confirmed,
//    successful, empty result. handleLoadOlderClick() then treated any
//    [] as "no older recordings exist," permanently hiding the Load
//    older button for the rest of the session after a single transient
//    error -- indistinguishable from genuinely reaching the end of a
//    customer's history. Fixed: null now means "failed, retryable";
//    [] now means "confirmed empty, this is the end."
//
// 2. handleLoadOlderClick() read `selectedCameraId` again AFTER its own
//    `await fetchClipsMetadata(...)` had already suspended the handler
//    -- nothing else on the page blocks switching to a different camera
//    tile while that fetch is in flight. A camera switch mid-fetch
//    wrote camera A's older page into camera B's cache entry and
//    rendered it under camera B's tile. Fixed: the camera id is
//    captured once into a local constant before the first await, and
//    every use after the await reads that captured value, guarded by
//    an explicit stale-selection check before touching the visible UI
//    -- the same pattern this page already uses in
//    renderAvailableDates()/loadRecordingsForDate().
//
// This extracts and evaluates the EXACT deployed source text between
// the PAGINATION_FETCH/PAGINATION_CORE/PAGINATION_ENSURE markers in
// main.py (undoing only the Python f-string brace-doubling {{ -> {,
// }} -> } the real page is rendered with) -- same extraction idiom as
// test_playback_segment_chaining_core.mjs -- so a real behavioral
// regression in the shipped pagination logic is guaranteed to be
// caught here, not just in a reimplementation of what it's supposed
// to do.
//
// The three extracted slices share module-level state (selectedCameraId,
// recordingsByCamera, recordingsLoaded, visibleRecordingCount,
// loadOlderButton, renderClipList, debugLog, fetch) exactly as they do
// in the real page's own single <script> closure -- this harness
// declares stand-ins for all of them, in the same combined function
// body as the extracted slices, so the extracted code closes over
// these test-controlled stand-ins precisely as it closes over the real
// ones in production.
//
// Usage: node test_playback_pagination_core.mjs /path/to/main.py

import fs from 'node:fs';
import assert from 'node:assert/strict';

const mainPyPath = process.argv[2];
if (!mainPyPath) {
  console.error('usage: node test_playback_pagination_core.mjs <path-to-main.py>');
  process.exit(2);
}
const source = fs.readFileSync(mainPyPath, 'utf8');

function extract(startMarker, endMarker) {
  const startIdx = source.indexOf(startMarker);
  const endIdx = source.indexOf(endMarker);
  if (startIdx === -1 || endIdx === -1 || endIdx < startIdx) {
    console.error(`FAIL: could not locate markers ${startMarker} / ${endMarker} in ${mainPyPath}`);
    process.exit(1);
  }
  let slice = source.slice(startIdx + startMarker.length, endIdx);
  return slice.replace(/\{\{/g, '{').replace(/\}\}/g, '}');
}

const fetchCore = extract('// === PAGINATION_FETCH_START ===', '// === PAGINATION_FETCH_END ===');
let paginationCore = extract('// === PAGINATION_CORE_START ===', '// === PAGINATION_CORE_END ===');
let ensureCore = extract('// === PAGINATION_ENSURE_START ===', '// === PAGINATION_ENSURE_END ===');

// These two slices embed one real Python f-string interpolation each
// (single-brace, resolved by Python at render time -- the raw .py text
// still literally contains the placeholder, unlike the {{ }} JS-literal
// braces the regex above already unescapes). Substitute the constant's
// own real, currently-deployed value so this test evaluates exactly
// what a real customer's browser receives, not a syntactically-broken
// placeholder.
const limitMatch = source.match(/CUSTOMER_PLAYBACK_INITIAL_LIMIT\s*=\s*(\d+)/);
if (!limitMatch) {
  console.error('FAIL: could not find CUSTOMER_PLAYBACK_INITIAL_LIMIT in main.py');
  process.exit(1);
}
const initialLimit = limitMatch[1];
paginationCore = paginationCore.replaceAll('{CUSTOMER_PLAYBACK_INITIAL_LIMIT}', initialLimit);
ensureCore = ensureCore.replaceAll('{CUSTOMER_PLAYBACK_INITIAL_LIMIT}', initialLimit);

function buildSandbox() {
  const state = {
    selectedCameraId: 'cam-1',
    recordingsByCamera: {},
    recordingsLoaded: new Set(),
    renderCalls: [],
    debugLines: [],
  };

  // One fetch response/behavior queued per call -- each test enqueues
  // exactly the sequence of outcomes it wants, in order.
  const fetchQueue = [];
  function queueFetchOk(clips) {
    fetchQueue.push(async () => ({ ok: true, json: async () => ({ clips }) }));
  }
  function queueFetchHttpError() {
    fetchQueue.push(async () => ({ ok: false, json: async () => ({}) }));
  }
  function queueFetchNetworkError() {
    fetchQueue.push(async () => { throw new Error('network down'); });
  }

  const harnessPrelude = `
    let selectedCameraId = __state.selectedCameraId;
    let recordingsByCamera = __state.recordingsByCamera;
    let recordingsLoaded = __state.recordingsLoaded;
    let visibleRecordingCount = 6;
    const loadOlderButton = {
      disabled: false, hidden: false, textContent: '',
      addEventListener(event, handler) { this._handler = handler; },
    };
    function renderClipList(cameraId, clips) {
      __state.renderCalls.push({ cameraId, clipIds: clips.map(c => c.id) });
    }
    function debugLog(msg) { __state.debugLines.push(msg); }
    async function fetch(url) {
      const next = __fetchQueue.shift();
      if (!next) throw new Error('test harness: fetch called with no queued response');
      return next();
    }
  `;

  const combined =
    harnessPrelude + '\n' +
    fetchCore + '\n' +
    paginationCore + '\n' +
    ensureCore + '\n' +
    `module.exports = {
       handleLoadOlderClick, fetchClipsMetadata, ensureClipsLoaded,
       getSelectedCameraId: () => selectedCameraId,
       setSelectedCameraId: (v) => { selectedCameraId = v; },
       getLoadOlderButton: () => loadOlderButton,
       getVisibleRecordingCount: () => visibleRecordingCount,
     };`;

  const sandboxModule = { exports: {} };
  const loader = new Function('module', 'exports', '__state', '__fetchQueue', combined);
  loader(sandboxModule, sandboxModule.exports, state, fetchQueue);

  return { api: sandboxModule.exports, state, fetchQueue, queueFetchOk, queueFetchHttpError, queueFetchNetworkError };
}

function clip(id, start, end) {
  return { id, start, end, name: id };
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

// --------------------------------------------------------- null vs [] semantics

test('fetchClipsMetadata returns the real array on a successful, non-empty response', async () => {
  const { api, queueFetchOk } = buildSandbox();
  queueFetchOk([clip('a', '2026-08-30T00:00:00', '2026-08-30T00:05:00')]);
  const result = await api.fetchClipsMetadata('cam-1', {});
  assert.deepEqual(result.map(c => c.id), ['a']);
});

test('fetchClipsMetadata returns [] (not null) for a confirmed, successful, empty page', async () => {
  const { api, queueFetchOk } = buildSandbox();
  queueFetchOk([]);
  const result = await api.fetchClipsMetadata('cam-1', {});
  assert.deepEqual(result, []);
});

test('fetchClipsMetadata returns null (not []) for a non-2xx HTTP response', async () => {
  const { api, queueFetchHttpError } = buildSandbox();
  queueFetchHttpError();
  const result = await api.fetchClipsMetadata('cam-1', {});
  assert.equal(result, null);
});

test('fetchClipsMetadata returns null (not []) for a thrown network error', async () => {
  const { api, queueFetchNetworkError } = buildSandbox();
  queueFetchNetworkError();
  const result = await api.fetchClipsMetadata('cam-1', {});
  assert.equal(result, null);
});

// --------------------------------------------------------- handleLoadOlderClick: end of results vs. retryable failure

test('a genuine empty older page hides the button (real end of history)', async () => {
  const { api, state, queueFetchOk } = buildSandbox();
  state.recordingsByCamera['cam-1'] = [clip('a', '2026-08-30T00:00:00', '2026-08-30T00:05:00')];
  // visibleRecordingCount(6) >= clips.length(1) forces the server-fetch branch
  queueFetchOk([]);
  await api.handleLoadOlderClick();
  assert.equal(api.getLoadOlderButton().hidden, true, 'button should hide -- this really is the end of history');
});

test('a transient fetch failure leaves the button visible and enabled for a retry', async () => {
  const { api, state, queueFetchHttpError } = buildSandbox();
  state.recordingsByCamera['cam-1'] = [clip('a', '2026-08-30T00:00:00', '2026-08-30T00:05:00')];
  queueFetchHttpError();
  await api.handleLoadOlderClick();
  const button = api.getLoadOlderButton();
  assert.equal(button.hidden, false, 'a transient error must never be presented as end-of-history');
  assert.equal(button.disabled, false, 'the button must be re-enabled so the customer can simply retry');
});

test('a successful older page merges in oldest-first with no duplicates', async () => {
  const { api, state, queueFetchOk } = buildSandbox();
  state.recordingsByCamera['cam-1'] = [clip('c', '2026-08-30T00:10:00', '2026-08-30T00:15:00')];
  queueFetchOk([
    clip('a', '2026-08-30T00:00:00', '2026-08-30T00:05:00'),
    clip('b', '2026-08-30T00:05:00', '2026-08-30T00:10:00'),
  ]);
  await api.handleLoadOlderClick();
  const merged = state.recordingsByCamera['cam-1'].map(c => c.id);
  assert.deepEqual(merged, ['a', 'b', 'c']);
  assert.equal(new Set(merged).size, merged.length, 'no duplicate ids after merging an older page');
});

// --------------------------------------------------------- camera-switch race

test('switching cameras while an older-page fetch is in flight does not corrupt the newly-selected camera', async () => {
  const { api, state, fetchQueue } = buildSandbox();
  state.recordingsByCamera['cam-1'] = [clip('a1', '2026-08-30T00:00:00', '2026-08-30T00:05:00')];
  state.recordingsByCamera['cam-2'] = [clip('b1', '2026-08-30T01:00:00', '2026-08-30T01:05:00')];
  api.setSelectedCameraId('cam-1');

  // Camera 1's "older" response, but the switch to camera 2 happens
  // from inside the mocked fetch call itself -- the real-world
  // equivalent of the customer clicking a different tile while the
  // network request for the first click is still pending.
  fetchQueue.push(async () => {
    api.setSelectedCameraId('cam-2');
    return { ok: true, json: async () => ({ clips: [{ id: 'a0', start: '2026-08-29T23:55:00', end: '2026-08-30T00:00:00', name: 'a0' }] }) };
  });

  await api.handleLoadOlderClick();

  assert.equal(api.getSelectedCameraId(), 'cam-2', 'sanity: the simulated switch really took effect');
  assert.deepEqual(
    state.recordingsByCamera['cam-2'].map(c => c.id),
    ['b1'],
    "camera 2's own cache must be untouched by camera 1's in-flight older-page fetch",
  );
});

test('a stale in-flight fetch still commits its result into the ORIGINAL camera\'s own cache, not lost', async () => {
  const { api, state, fetchQueue } = buildSandbox();
  state.recordingsByCamera['cam-1'] = [clip('a1', '2026-08-30T00:00:00', '2026-08-30T00:05:00')];
  state.recordingsByCamera['cam-2'] = [clip('b1', '2026-08-30T01:00:00', '2026-08-30T01:05:00')];
  api.setSelectedCameraId('cam-1');

  fetchQueue.push(async () => {
    api.setSelectedCameraId('cam-2');
    return { ok: true, json: async () => ({ clips: [{ id: 'a0', start: '2026-08-29T23:55:00', end: '2026-08-30T00:00:00', name: 'a0' }] }) };
  });
  await api.handleLoadOlderClick();

  assert.deepEqual(
    state.recordingsByCamera['cam-1'].map(c => c.id),
    ['a0', 'a1'],
    "camera 1's own older page must still be saved for whenever it is reselected",
  );
});

// --------------------------------------------------------- ensureClipsLoaded: initial load retry semantics

test('ensureClipsLoaded caches a successful initial load and never refetches it', async () => {
  const { api, queueFetchOk, fetchQueue } = buildSandbox();
  queueFetchOk([clip('a', '2026-08-30T00:00:00', '2026-08-30T00:05:00')]);
  const first = await api.ensureClipsLoaded('cam-1');
  assert.deepEqual(first.map(c => c.id), ['a']);
  const second = await api.ensureClipsLoaded('cam-1');
  assert.deepEqual(second.map(c => c.id), ['a']);
  assert.equal(fetchQueue.length, 0, 'the second call must be served from cache, not a second fetch');
});

test('ensureClipsLoaded does not permanently cache a failed initial load, so a later retry can succeed', async () => {
  const { api, queueFetchHttpError, queueFetchOk } = buildSandbox();
  queueFetchHttpError();
  const failedAttempt = await api.ensureClipsLoaded('cam-1');
  assert.deepEqual(failedAttempt, [], 'no crash, and nothing to show yet');

  queueFetchOk([clip('a', '2026-08-30T00:00:00', '2026-08-30T00:05:00')]);
  const retried = await api.ensureClipsLoaded('cam-1');
  assert.deepEqual(
    retried.map(c => c.id),
    ['a'],
    'a failed initial load must not be mistaken for "this camera genuinely has zero recordings" forever',
  );
});

let failed = 0;
for (const { name, fn } of tests) {
  try {
    await fn();
    console.log(`ok - ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL - ${name}`);
    console.log(`  ${error.message}`);
  }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed === 0 ? 0 : 1);
