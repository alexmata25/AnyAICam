// Playback timeline scrubbing (2026-09-15): tests for the pure, DOM-free
// scrub-resolution core embedded in _render_customer_playback()'s own
// <script> (see main.py, between the "// === SCRUB_CORE_START ===" and
// "// === SCRUB_CORE_END ===" markers) -- same extraction idiom as
// test_playback_segment_chaining_core.mjs / test_playback_date_navigation.mjs.
//
// This extracts and evaluates the EXACT deployed source text (undoing
// only the Python f-string brace-doubling {{ -> {, }} -> } the real
// page is rendered with), not a reimplementation, so a real behavioral
// change to the shipped scrub-resolution logic is guaranteed to be
// caught here.
//
// Usage: node test_playback_timeline_scrubbing_core.mjs /path/to/main.py

import fs from 'node:fs';
import assert from 'node:assert/strict';

const mainPyPath = process.argv[2];
if (!mainPyPath) {
  console.error('usage: node test_playback_timeline_scrubbing_core.mjs <path-to-main.py>');
  process.exit(2);
}
const source = fs.readFileSync(mainPyPath, 'utf8');

const startMarker = '// === SCRUB_CORE_START ===';
const endMarker = '// === SCRUB_CORE_END ===';
const startIdx = source.indexOf(startMarker);
const endIdx = source.indexOf(endMarker);
if (startIdx === -1 || endIdx === -1 || endIdx < startIdx) {
  console.error(`FAIL: could not locate SCRUB_CORE markers in ${mainPyPath}`);
  process.exit(1);
}
let coreSource = source.slice(startIdx, endIdx);
coreSource = coreSource.replace(/\{\{/g, '{').replace(/\}\}/g, '}');

const sandboxModule = { exports: {} };
const loader = new Function(
  'module',
  'exports',
  coreSource + '\nmodule.exports = { timelineFractionToLocalMs, coveringClipAt, resolveScrubTarget };',
);
loader(sandboxModule, sandboxModule.exports);
const { timelineFractionToLocalMs, coveringClipAt, resolveScrubTarget } = sandboxModule.exports;

// Mirrors the real page's own playbackDate(): naive (no Z/offset)
// strings are UTC; a number is always epoch-ms. Injected here exactly
// as the real call sites inject the real playbackDate(), rather than
// the core closing over any outside function -- see resolveScrubTarget's
// own comment for why.
function parseDate(value) {
  if (value === null || value === undefined || value === '') return new Date(NaN);
  if (typeof value === 'number') return new Date(value);
  const text = String(value);
  const hasZone = /Z$|[+-]\d{2}:\d{2}$/.test(text);
  return new Date(hasZone ? text : text + 'Z');
}

function clip(id, start, end) {
  return { id, start, end, name: id };
}

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

// --- timelineFractionToLocalMs: pure fraction -> local-day-relative epoch-ms ---

test('fraction 0 is exactly local midnight of the given day', () => {
  const ms = timelineFractionToLocalMs('2026-09-15', 0);
  const d = new Date(ms);
  assert.equal(d.getFullYear(), 2026);
  assert.equal(d.getMonth(), 8); // 0-indexed: September
  assert.equal(d.getDate(), 15);
  assert.equal(d.getHours(), 0);
  assert.equal(d.getMinutes(), 0);
  assert.equal(d.getSeconds(), 0);
});

test('fraction 0.5 is local noon of the given day', () => {
  const ms = timelineFractionToLocalMs('2026-09-15', 0.5);
  const d = new Date(ms);
  assert.equal(d.getDate(), 15);
  assert.equal(d.getHours(), 12);
});

test('a fraction just under 1 lands just before the next local midnight, never on it', () => {
  const ms = timelineFractionToLocalMs('2026-09-15', 0.999999);
  const d = new Date(ms);
  assert.equal(d.getDate(), 15);
  assert.equal(d.getHours(), 23);
});

test('out-of-range fractions are clamped to [0,1], never extrapolated past the day', () => {
  const negative = timelineFractionToLocalMs('2026-09-15', -0.5);
  const zero = timelineFractionToLocalMs('2026-09-15', 0);
  assert.equal(negative, zero);
  const over = timelineFractionToLocalMs('2026-09-15', 1.5);
  const one = timelineFractionToLocalMs('2026-09-15', 1);
  assert.equal(over, one);
});

// --- coveringClipAt: strict covering only, no "nearest" leniency ---

test('a timestamp inside a real recording resolves to that recording', () => {
  const clips = [clip('a', '2026-09-15T14:00:00', '2026-09-15T14:05:00')];
  const target = parseDate('2026-09-15T14:02:30').getTime();
  const covering = coveringClipAt(clips, target, parseDate);
  assert.equal(covering && covering.id, 'a');
});

test('a timestamp exactly at a recording boundary (start or end) still covers -- inclusive both ends', () => {
  const clips = [clip('a', '2026-09-15T14:00:00', '2026-09-15T14:05:00')];
  assert.equal(coveringClipAt(clips, parseDate('2026-09-15T14:00:00').getTime(), parseDate).id, 'a');
  assert.equal(coveringClipAt(clips, parseDate('2026-09-15T14:05:00').getTime(), parseDate).id, 'a');
});

test('a timestamp in a genuine gap between two recordings resolves to null -- never snaps to the nearest one', () => {
  const clips = [
    clip('a', '2026-09-15T14:00:00', '2026-09-15T14:05:00'),
    clip('b', '2026-09-15T15:00:00', '2026-09-15T15:05:00'),
  ];
  // One second after 'a' ends, deep inside the real 55-minute gap.
  const target = parseDate('2026-09-15T14:05:01').getTime();
  assert.equal(coveringClipAt(clips, target, parseDate), null);
});

test('a timestamp one second before the very first recording of the day resolves to null, not the first recording', () => {
  const clips = [clip('a', '2026-09-15T08:00:00', '2026-09-15T08:05:00')];
  const target = parseDate('2026-09-15T07:59:59').getTime();
  assert.equal(coveringClipAt(clips, target, parseDate), null);
});

test('an empty clip list is always a gap', () => {
  assert.equal(coveringClipAt([], parseDate('2026-09-15T14:00:00').getTime(), parseDate), null);
});

// --- resolveScrubTarget: the full click/drag decision, offset math ---
//
// timelineFractionToLocalMs() deliberately builds its day-start via the
// TEST RUNNER's own local timezone (new Date(y,m-1,d,...), exactly
// matching what a real customer's browser does), while parseDate()
// deliberately treats every clip.start/end as UTC (exactly matching how
// the real recordings catalog stores them) -- two different, both
// intentional, conventions. So a clip fixture's start/end must be
// derived FROM a real epoch-ms value (via toISOString(), then trimmed
// to the naive/no-Z shape the real catalog actually uses) rather than a
// hand-typed clock string assumed to already be UTC -- otherwise these
// tests would only pass by coincidence on a UTC+0 machine. This mirrors
// exactly what a real appliance really does: it writes real UTC instants,
// however they're expressed.
function utcNaive(epochMs) {
  return new Date(epochMs).toISOString().slice(0, 19);
}

test('resolves the correct clip and a correct in-clip offset', () => {
  const dayStartMs = timelineFractionToLocalMs('2026-09-15', 0);
  const clipStartMs = dayStartMs + 14 * 3600 * 1000; // local 14:00:00
  const clipEndMs = clipStartMs + 5 * 60 * 1000; // 5-minute recording
  const clips = [clip('a', utcNaive(clipStartMs), utcNaive(clipEndMs))];
  // 14:02:30 local -- 2m30s into the clip.
  const fraction = (14 * 3600 + 2 * 60 + 30) / 86400;
  const result = resolveScrubTarget(clips, '2026-09-15', fraction, parseDate);
  assert.equal(result.covering && result.covering.id, 'a');
  assert.equal(result.offsetSeconds, 150);
});

test('a gap resolves covering:null and offsetSeconds:null -- never a stale/guessed offset', () => {
  const dayStartMs = timelineFractionToLocalMs('2026-09-15', 0);
  const clipStartMs = dayStartMs + 14 * 3600 * 1000;
  const clipEndMs = clipStartMs + 5 * 60 * 1000;
  const clips = [clip('a', utcNaive(clipStartMs), utcNaive(clipEndMs))];
  const fraction = (20 * 3600) / 86400; // 20:00 local, well outside the one clip
  const result = resolveScrubTarget(clips, '2026-09-15', fraction, parseDate);
  assert.equal(result.covering, null);
  assert.equal(result.offsetSeconds, null);
  assert.equal(typeof result.targetMs, 'number'); // the target instant itself is still always resolvable, for the playhead's own position
});

test('resolves correctly against a different file when the fraction crosses into it -- automatic file transition, no manual selection', () => {
  const dayStartMs = timelineFractionToLocalMs('2026-09-15', 0);
  const aStartMs = dayStartMs + 14 * 3600 * 1000;
  const aEndMs = aStartMs + 5 * 60 * 1000;
  const bStartMs = aEndMs; // exactly back-to-back
  const bEndMs = bStartMs + 5 * 60 * 1000;
  const clips = [
    clip('a', utcNaive(aStartMs), utcNaive(aEndMs)),
    clip('b', utcNaive(bStartMs), utcNaive(bEndMs)),
  ];
  const inFirst = resolveScrubTarget(clips, '2026-09-15', (14 * 3600 + 1 * 60) / 86400, parseDate);
  assert.equal(inFirst.covering.id, 'a');
  const inSecond = resolveScrubTarget(clips, '2026-09-15', (14 * 3600 + 7 * 60) / 86400, parseDate);
  assert.equal(inSecond.covering.id, 'b');
  assert.equal(inSecond.offsetSeconds, 120); // 2 minutes into clip 'b'
});

test('offsetSeconds is never negative even at the exact covering boundary', () => {
  const dayStartMs = timelineFractionToLocalMs('2026-09-15', 0);
  const clipStartMs = dayStartMs + 14 * 3600 * 1000;
  const clipEndMs = clipStartMs + 5 * 60 * 1000;
  const clips = [clip('a', utcNaive(clipStartMs), utcNaive(clipEndMs))];
  const result = resolveScrubTarget(clips, '2026-09-15', (14 * 3600) / 86400, parseDate);
  assert.equal(result.offsetSeconds, 0);
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
