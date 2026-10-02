// Playback automatic segment chaining (2026-09-03 reconciliation): tests
// for the pure, DOM-free chaining-planner core embedded in
// _render_customer_playback()'s own <script> (see main.py, between the
// "// === CHAIN_CORE_START ===" and "// === CHAIN_CORE_END ===" markers)
// -- same extraction idiom as test_playback_analytics_lanes.mjs /
// test_playback_date_navigation.mjs.
//
// Reconciled from the accepted Samsung implementation, but deliberately
// narrower than Samsung's own CHAIN_CORE: this branch's playClip() uses
// a synchronous recordingMediaUrl() (no fetch, no await), unlike
// Samsung's async fetchClipUrl(), so there is no async URL-resolution
// gap for _createPlayRequestSequencer() to guard here -- only
// _planNextChainedClip() (the actual chain-target decision, always
// synchronous, always over the already-loaded currentClips page) was
// reconciled. See test_stale_ended_event_cannot_hijack_a_newer_manual_
// selection in test_playback_segment_chaining.py for why that's safe:
// a video element that has already had its src reassigned by a newer
// manual/date/camera action cannot fire 'ended' for the old content
// afterward, by the <video> element's own spec.
//
// This extracts and evaluates the EXACT deployed source text (undoing
// only the Python f-string brace-doubling {{ -> {, }} -> } the real
// page is rendered with), not a reimplementation, so a real behavioral
// change to the shipped chaining logic is guaranteed to be caught here.
//
// Usage: node test_playback_segment_chaining_core.mjs /path/to/main.py

import fs from 'node:fs';
import assert from 'node:assert/strict';

const mainPyPath = process.argv[2];
if (!mainPyPath) {
  console.error('usage: node test_playback_segment_chaining_core.mjs <path-to-main.py>');
  process.exit(2);
}
const source = fs.readFileSync(mainPyPath, 'utf8');

const startMarker = '// === CHAIN_CORE_START ===';
const endMarker = '// === CHAIN_CORE_END ===';
const startIdx = source.indexOf(startMarker);
const endIdx = source.indexOf(endMarker);
if (startIdx === -1 || endIdx === -1 || endIdx < startIdx) {
  console.error(`FAIL: could not locate CHAIN_CORE markers in ${mainPyPath}`);
  process.exit(1);
}
let coreSource = source.slice(startIdx, endIdx);
coreSource = coreSource.replace(/\{\{/g, '{').replace(/\}\}/g, '}');

const sandboxModule = { exports: {} };
const loader = new Function(
  'module',
  'exports',
  coreSource + '\nmodule.exports = { _planNextChainedClip, CHAIN_GAP_TOLERANCE_SECONDS };',
);
loader(sandboxModule, sandboxModule.exports);
const { _planNextChainedClip, CHAIN_GAP_TOLERANCE_SECONDS } = sandboxModule.exports;

function clip(id, start, end) {
  return { id, start, end, name: id };
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test('gap tolerance is 10 seconds, matching the accepted Samsung measurement', () => {
  assert.equal(CHAIN_GAP_TOLERANCE_SECONDS, 10);
});

// --- Adjacent segments chain automatically ---
test('exact back-to-back segments chain', () => {
  const clips = [
    clip('a', '2026-08-30T14:47:18', '2026-08-30T14:52:18'),
    clip('b', '2026-08-30T14:52:18', '2026-08-30T14:57:18'),
  ];
  const next = _planNextChainedClip(clips, clips[0]);
  assert.equal(next && next.id, 'b');
});

// --- Accepted small-gap tolerance still chains ---
test('a 9s gap (within the 10s tolerance) still chains', () => {
  const clips = [
    clip('a', '2026-08-30T14:47:18', '2026-08-30T14:52:18'),
    clip('b', '2026-08-30T14:52:27', '2026-08-30T14:57:27'), // +9s gap
  ];
  const next = _planNextChainedClip(clips, clips[0]);
  assert.equal(next && next.id, 'b');
});

test('exactly 10s gap (the boundary itself) still chains', () => {
  const clips = [
    clip('a', '2026-08-30T14:47:18', '2026-08-30T14:52:18'),
    clip('b', '2026-08-30T14:52:28', '2026-08-30T14:57:28'), // +10s gap
  ];
  const next = _planNextChainedClip(clips, clips[0]);
  assert.equal(next && next.id, 'b');
});

test('a small overlap (restart-era duplicate, within tolerance) still chains', () => {
  const clips = [
    clip('a', '2026-08-30T14:47:18', '2026-08-30T14:52:18'),
    clip('b', '2026-08-30T14:52:12', '2026-08-30T14:57:12'), // -6s overlap
  ];
  const next = _planNextChainedClip(clips, clips[0]);
  assert.equal(next && next.id, 'b');
});

// --- Large gaps do not chain ---
test('an 11s gap (just past tolerance) does not chain', () => {
  const clips = [
    clip('a', '2026-08-30T14:47:18', '2026-08-30T14:52:18'),
    clip('b', '2026-08-30T14:52:29', '2026-08-30T14:57:29'), // +11s gap
  ];
  const next = _planNextChainedClip(clips, clips[0]);
  assert.equal(next, null);
});

test('a genuine 286s outage-sized gap does not chain', () => {
  const clips = [
    clip('a', '2026-08-30T14:47:18', '2026-08-30T14:52:18'),
    clip('b', '2026-08-30T14:57:04', '2026-08-30T15:02:04'), // +286s gap
  ];
  const next = _planNextChainedClip(clips, clips[0]);
  assert.equal(next, null);
});

// --- End of final segment stops cleanly ---
test('the last loaded clip has nothing to chain into -- returns null, not an error', () => {
  const clips = [
    clip('a', '2026-08-30T14:47:18', '2026-08-30T14:52:18'),
    clip('b', '2026-08-30T14:52:18', '2026-08-30T14:57:18'),
  ];
  const next = _planNextChainedClip(clips, clips[1]);
  assert.equal(next, null);
});

test('an empty clip list stops cleanly', () => {
  const next = _planNextChainedClip([], clip('a', '2026-08-30T14:47:18', '2026-08-30T14:52:18'));
  assert.equal(next, null);
});

test('a null/undefined ended clip stops cleanly (no manual selection yet)', () => {
  const clips = [clip('a', '2026-08-30T14:47:18', '2026-08-30T14:52:18')];
  assert.equal(_planNextChainedClip(clips, null), null);
  assert.equal(_planNextChainedClip(clips, undefined), null);
});

test('an ended clip not present in currentClips (stale/already-superseded page) stops cleanly', () => {
  const clips = [clip('a', '2026-08-30T14:47:18', '2026-08-30T14:52:18')];
  const next = _planNextChainedClip(clips, clip('ghost', '2026-08-29T00:00:00', '2026-08-29T00:05:00'));
  assert.equal(next, null);
});

// --- Chaining works on historical dates ---
test('chains correctly for clips loaded from a historical (non-today) date', () => {
  const clips = [
    clip('a', '2026-08-21T09:00:00', '2026-08-21T09:05:00'),
    clip('b', '2026-08-21T09:05:00', '2026-08-21T09:10:00'),
    clip('c', '2026-08-21T09:10:03', '2026-08-21T09:15:03'), // +3s gap
  ];
  const first = _planNextChainedClip(clips, clips[0]);
  assert.equal(first && first.id, 'b');
  const second = _planNextChainedClip(clips, first);
  assert.equal(second && second.id, 'c');
  assert.equal(_planNextChainedClip(clips, second), null);
});

let failed = 0;
for (const { name, fn } of tests) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL - ${name}`);
    console.log(`  ${error.message}`);
  }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed === 0 ? 0 : 1);
