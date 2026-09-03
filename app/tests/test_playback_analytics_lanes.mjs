// Playback analytics-marker lanes (2026-09-03): tests for the pure,
// DOM-free lane-index core embedded in _render_customer_playback()'s
// own <script> (see main.py, between the "// === LANE_CORE_START ==="
// and "// === LANE_CORE_END ===" markers) -- same extraction idiom as
// test_playback_segment_chaining.mjs, alongside this one.
//
// This extracts and evaluates the EXACT deployed source text (undoing
// only the Python f-string brace-doubling {{ -> {, }} -> } the real
// page is rendered with), not a reimplementation, so a real behavioral
// change to the shipped lane logic is guaranteed to be caught here.
// eventLaneTop() is written to touch nothing but its own argument --
// no DOM, no fetch, no global page state -- specifically so it can be
// tested this way, in plain Node, with no browser/jsdom.
//
// Usage: node test_playback_analytics_lanes.mjs /path/to/main.py

import fs from 'node:fs';
import assert from 'node:assert/strict';

const mainPyPath = process.argv[2];
if (!mainPyPath) {
  console.error('usage: node test_playback_analytics_lanes.mjs <path-to-main.py>');
  process.exit(2);
}
const source = fs.readFileSync(mainPyPath, 'utf8');

const startMarker = '// === LANE_CORE_START ===';
const endMarker = '// === LANE_CORE_END ===';
const startIdx = source.indexOf(startMarker);
const endIdx = source.indexOf(endMarker);
if (startIdx === -1 || endIdx === -1 || endIdx < startIdx) {
  console.error(`FAIL: could not locate LANE_CORE markers in ${mainPyPath}`);
  process.exit(1);
}
let coreSource = source.slice(startIdx, endIdx);
coreSource = coreSource.replace(/\{\{/g, '{').replace(/\}\}/g, '}');

const sandboxModule = { exports: {} };
const loader = new Function(
  'module',
  'exports',
  coreSource + '\nmodule.exports = { EVENT_LANE_ORDER, EVENT_LANE_HEIGHT_PX, EVENT_LANE_GAP_PX, eventLaneTop };',
);
loader(sandboxModule, sandboxModule.exports);
const { EVENT_LANE_ORDER, EVENT_LANE_HEIGHT_PX, EVENT_LANE_GAP_PX, eventLaneTop } = sandboxModule.exports;

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test('lane order is fixed: motion, person, vehicle, lpr, people_counting, intrusion', () => {
  assert.deepEqual(EVENT_LANE_ORDER, ['motion', 'person', 'vehicle', 'lpr', 'people_counting', 'intrusion']);
});

test('motion, person, and vehicle get distinct tops', () => {
  const motion = eventLaneTop('motion');
  const person = eventLaneTop('person');
  const vehicle = eventLaneTop('vehicle');
  assert.notEqual(motion, person);
  assert.notEqual(person, vehicle);
  assert.notEqual(motion, vehicle);
});

test('every named category gets a distinct, strictly increasing top -- no two lanes overlap', () => {
  const tops = EVENT_LANE_ORDER.map((c) => parseInt(eventLaneTop(c), 10));
  const sorted = [...tops].sort((a, b) => a - b);
  assert.deepEqual(tops, sorted, 'lane tops must already be in increasing order');
  assert.equal(new Set(tops).size, tops.length, 'every category must get a distinct top');
  for (let i = 1; i < tops.length; i++) {
    assert.ok(tops[i] - tops[i - 1] >= EVENT_LANE_HEIGHT_PX, `lane ${i - 1} and lane ${i} must not overlap vertically`);
  }
});

test('an unknown/unclassified category (null, or a type with no dedicated lane) falls into its own dedicated fallback lane', () => {
  const fallbackTop = EVENT_LANE_ORDER.length * (EVENT_LANE_HEIGHT_PX + EVENT_LANE_GAP_PX);
  assert.equal(eventLaneTop(null), fallbackTop + 'px');
  assert.equal(eventLaneTop('ppe'), fallbackTop + 'px');
  assert.equal(eventLaneTop(undefined), fallbackTop + 'px');
  // The fallback lane must not collide with any real category's own lane.
  for (const category of EVENT_LANE_ORDER) {
    assert.notEqual(eventLaneTop(category), fallbackTop + 'px');
  }
});

test('two different unknown categories land in the exact same fallback lane, not overlapping each other either', () => {
  assert.equal(eventLaneTop('ppe'), eventLaneTop('some_future_category'));
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
