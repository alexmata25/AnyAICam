// Playback Previous/Next Day date navigation (2026-09-03): tests for the
// pure, DOM-free date-shift core embedded in _render_customer_playback()'s
// own <script> (see main.py, between "// === DATE_NAV_CORE_START ===" and
// "// === DATE_NAV_CORE_END ===") -- same extraction idiom as
// test_playback_segment_chaining.mjs / test_playback_analytics_lanes.mjs.
//
// This extracts and evaluates the EXACT deployed source text (undoing
// only the Python f-string brace-doubling {{ -> {, }} -> } the real page
// is rendered with), not a reimplementation, so a real behavioral change
// -- including a DST regression -- is guaranteed to be caught here.
// shiftedDateString() is written to touch nothing but its own arguments
// -- no DOM, no fetch -- specifically so it can be tested this way, in
// plain Node, with no browser/jsdom. Node's own local timezone is forced
// to America/Chicago (the appliance's own APPLIANCE_TIMEZONE) via
// process.env.TZ before any Date object is constructed, so setDate()'s
// local-time DST behavior is exercised for real, not just simulated.
//
// Usage: node test_playback_date_navigation.mjs /path/to/main.py

process.env.TZ = 'America/Chicago';

import fs from 'node:fs';
import assert from 'node:assert/strict';

const mainPyPath = process.argv[2];
if (!mainPyPath) {
  console.error('usage: node test_playback_date_navigation.mjs <path-to-main.py>');
  process.exit(2);
}
const source = fs.readFileSync(mainPyPath, 'utf8');

const startMarker = '// === DATE_NAV_CORE_START ===';
const endMarker = '// === DATE_NAV_CORE_END ===';
const startIdx = source.indexOf(startMarker);
const endIdx = source.indexOf(endMarker);
if (startIdx === -1 || endIdx === -1 || endIdx < startIdx) {
  console.error(`FAIL: could not locate DATE_NAV_CORE markers in ${mainPyPath}`);
  process.exit(1);
}
let coreSource = source.slice(startIdx, endIdx);
coreSource = coreSource.replace(/\{\{/g, '{').replace(/\}\}/g, '}');

const sandboxModule = { exports: {} };
const loader = new Function(
  'module',
  'exports',
  coreSource + '\nmodule.exports = { shiftedDateString };',
);
loader(sandboxModule, sandboxModule.exports);
const { shiftedDateString } = sandboxModule.exports;

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test('next day: ordinary date', () => {
  assert.equal(shiftedDateString('2026-08-21', 1), '2026-08-22');
});

test('previous day: ordinary date', () => {
  assert.equal(shiftedDateString('2026-08-21', -1), '2026-08-20');
});

test('next day: month boundary', () => {
  assert.equal(shiftedDateString('2026-08-31', 1), '2026-09-01');
});

test('previous day: month boundary', () => {
  assert.equal(shiftedDateString('2026-09-01', -1), '2026-08-31');
});

test('next day: year boundary', () => {
  assert.equal(shiftedDateString('2026-12-31', 1), '2027-01-01');
});

test('previous day: year boundary', () => {
  assert.equal(shiftedDateString('2027-01-01', -1), '2026-12-31');
});

// 2026 America/Chicago spring-forward: local day 2026-03-08 is only 23
// real hours (2:00 AM -> 3:00 AM skipped). A fixed 86400000ms offset
// would land one hour into 2026-03-08 instead of on 2026-03-09.
test('next day across the 2026 spring-forward DST boundary', () => {
  assert.equal(shiftedDateString('2026-03-08', 1), '2026-03-09');
});
test('previous day across the 2026 spring-forward DST boundary', () => {
  assert.equal(shiftedDateString('2026-03-09', -1), '2026-03-08');
});

// 2026 America/Chicago fall-back: local day 2026-11-01 is 25 real hours
// (1:00 AM repeats). A fixed 86400000ms offset would land short of
// 2026-11-02.
test('next day across the 2026 fall-back DST boundary', () => {
  assert.equal(shiftedDateString('2026-11-01', 1), '2026-11-02');
});
test('previous day across the 2026 fall-back DST boundary', () => {
  assert.equal(shiftedDateString('2026-11-02', -1), '2026-11-01');
});

test('shifting by more than one day still lands on the correct calendar date', () => {
  assert.equal(shiftedDateString('2026-08-21', 7), '2026-08-28');
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
