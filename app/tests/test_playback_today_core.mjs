// Playback "which day" + mobile refresh (2026-09-25): real Node execution of
// the exact deployed source between the PLAYBACK_TODAY_CORE markers in
// main.py (undoing only the f-string brace doubling), with a fake clock and
// fake timers. Run once per timezone by test_playback_today_core_js.py:
//   node test_playback_today_core.mjs <main.py> <expected local day at NOW_ISO>
import fs from 'node:fs';
import assert from 'node:assert/strict';

const [mainPath, expectedToday] = process.argv.slice(2);
const source = fs.readFileSync(mainPath, 'utf8').replace(/\r\n/g, '\n');
const start = source.indexOf('// === PLAYBACK_TODAY_CORE_START ===');
const end = source.indexOf('// === PLAYBACK_TODAY_CORE_END ===');
assert.ok(start > 0 && end > start, 'PLAYBACK_TODAY_CORE markers not found in main.py');
const core = source.slice(start, end).replaceAll('{{', '{').replaceAll('}}', '}');
const { createPlaybackDayController, playbackLocalDay, PLAYBACK_MOBILE_REFRESH_MS } =
  new Function(`${core}; return { createPlaybackDayController, playbackLocalDay, PLAYBACK_MOBILE_REFRESH_MS };`)();

const NOW_ISO = '2026-09-26T03:30:00Z';  // evening of the 25th in the Americas, the 26th in Asia/Pacific

function harness({ mobile, now = new Date(NOW_ISO), serverClips = [] } = {}) {
  const h = {
    now, mobile, hidden: false, timers: new Map(), nextTimer: 1,
    loads: [], refreshes: [], clipsSeen: [], dayChanges: [], server: serverClips, refreshResult: null,
  };
  h.controller = createPlaybackDayController({
    now: () => h.now,
    isMobile: () => h.mobile,
    isHidden: () => h.hidden,
    loadDay: (date, info) => h.loads.push([date, info.reason]),
    refreshDay: async date => { h.refreshes.push(date); return h.refreshResult ? h.refreshResult(date) : [...h.server]; },
    onClips: (date, clips) => h.clipsSeen.push([date, clips.map(c => c.id)]),
    onDayChanged: date => h.dayChanges.push(date),
    setTimer: (fn, ms) => { const id = h.nextTimer++; h.timers.set(id, { fn, ms }); return id; },
    clearTimer: id => h.timers.delete(id),
  });
  h.fire = async () => {  // run the one pending refresh timer
    assert.equal(h.timers.size, 1, 'exactly one refresh timer should be pending');
    const [id, timer] = [...h.timers.entries()][0];
    h.timers.delete(id);
    await timer.fn();
  };
  h.pendingMs = () => [...h.timers.values()][0]?.ms;
  return h;
}
const clip = (id, end = '2026-09-26T03:00:00') => ({ id, end });
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('timezone: the day is the viewer local calendar day, not UTC', () => {
  assert.equal(playbackLocalDay(new Date(NOW_ISO)), expectedToday);
});

test('first open defaults to today (desktop and mobile)', () => {
  for (const mobile of [false, true]) {
    const h = harness({ mobile });
    assert.equal(h.controller.open(), expectedToday);
    assert.deepEqual(h.loads, [[expectedToday, 'open']]);
  }
});

test('desktop: a manually selected historical date stays selected, across refresh ticks and midnight', async () => {
  const h = harness({ mobile: false });
  h.controller.open();
  h.controller.selectDate('2026-09-20');
  assert.equal(h.controller.state.manual, true);
  for (let i = 0; i < 5; i++) await h.fire();
  h.now = new Date(h.now.getTime() + 36 * 3600 * 1000);  // well past local midnight
  await h.fire();
  assert.equal(h.controller.state.viewingDate, '2026-09-20');
  assert.deepEqual(h.refreshes, []);  // desktop never polls
  assert.deepEqual(h.loads.map(l => l[0]), [expectedToday, '2026-09-20']);
});

test('mobile: a stale restored day (not one the customer picked) rolls to today', async () => {
  const h = harness({ mobile: true });
  h.controller.open();
  h.controller.noteLoaded('2026-08-01', []);  // e.g. a day restored from an earlier session
  await h.fire();
  assert.equal(h.controller.state.viewingDate, expectedToday);
  assert.deepEqual(h.dayChanges, [expectedToday]);
  assert.deepEqual(h.refreshes, [expectedToday]);
});

test('mobile: a picked date stays (no live refresh); picking today resumes the 5 s refresh', async () => {
  const h = harness({ mobile: true });
  h.controller.open();
  assert.equal(h.controller.selectDate('2026-09-01'), '2026-09-01');  // 2026-09-25: phones have the calendar
  assert.equal(h.controller.state.manual, true);
  await h.fire(); await h.fire();
  assert.equal(h.controller.state.viewingDate, '2026-09-01');
  assert.deepEqual(h.refreshes, []);
  assert.deepEqual(h.dayChanges, []);
  h.controller.selectDate(expectedToday);
  assert.equal(h.controller.state.manual, false);
  await h.fire();
  assert.deepEqual(h.refreshes, [expectedToday]);
  assert.deepEqual(h.loads.map(l => l[0]), [expectedToday, '2026-09-01', expectedToday]);
});

test('mobile refreshes every 5 seconds', async () => {
  assert.equal(PLAYBACK_MOBILE_REFRESH_MS, 5000);
  const h = harness({ mobile: true });
  h.controller.open();
  assert.equal(h.pendingMs(), 5000);
  for (let i = 0; i < 3; i++) { await h.fire(); assert.equal(h.pendingMs(), 5000); }
  assert.equal(h.refreshes.length, 3);
});

test('new recordings appear without a reload; an unchanged list is not re-rendered', async () => {
  const h = harness({ mobile: true, serverClips: [clip('a')] });
  h.controller.open();
  h.controller.noteLoaded(expectedToday, [clip('a')]);  // what the initial load rendered
  await h.fire();
  assert.deepEqual(h.clipsSeen, []);  // nothing new
  h.server = [clip('a'), clip('b')];
  await h.fire();
  assert.deepEqual(h.clipsSeen, [[expectedToday, ['a', 'b']]]);
  h.server = [clip('a'), clip('b', '2026-09-26T03:05:00')];  // a growing segment counts as new
  await h.fire();
  assert.equal(h.clipsSeen.length, 2);
});

test('active playback is not interrupted: refresh never uses the player-resetting load path', async () => {
  const h = harness({ mobile: true, serverClips: [clip('a')] });
  h.controller.open();
  for (let i = 0; i < 10; i++) { h.server = [...h.server, clip(`n${i}`)]; await h.fire(); }
  assert.deepEqual(h.loads, [[expectedToday, 'open']]);  // only the first open ever loaded the player
  assert.equal(h.clipsSeen.length, 10);
});

test('midnight rollover moves mobile to the new day without a reload', async () => {
  const local = new Date(2026, 8, 25, 23, 59, 58);  // 23:59:58 local, in whatever TZ this run uses
  const h = harness({ mobile: true, now: local });
  h.controller.open();
  assert.equal(h.controller.state.viewingDate, '2026-09-25');
  h.now = new Date(2026, 8, 26, 0, 0, 3);
  await h.fire();
  assert.equal(h.controller.state.viewingDate, '2026-09-26');
  assert.deepEqual(h.dayChanges, ['2026-09-26']);
  assert.deepEqual(h.refreshes, ['2026-09-26']);
  assert.deepEqual(h.loads, [['2026-09-25', 'open']]);  // no reload, no player reset
});

test('a hidden tab does not poll; becoming visible refreshes at once', async () => {
  const h = harness({ mobile: true });
  h.controller.open();
  h.hidden = true;
  await h.fire(); await h.fire();
  assert.deepEqual(h.refreshes, []);
  h.hidden = false;
  await h.controller.wake();
  assert.deepEqual(h.refreshes, [expectedToday]);
  assert.equal(h.timers.size, 1);  // still exactly one timer after wake
});

test('overlapping refreshes never double-fetch; a camera switch mid-fetch is dropped', async () => {
  const h = harness({ mobile: true });
  h.controller.open();
  let release;
  h.refreshResult = () => new Promise(resolve => { release = resolve; });
  const first = h.controller.wake();
  await h.controller.wake();  // while the first is in flight
  assert.equal(h.refreshes.length, 1);
  release(null);  // the page returns null when the camera changed during the fetch
  await first;
  assert.deepEqual(h.clipsSeen, []);
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log(`ok - ${name}`); }
  catch (error) { failed++; console.log(`FAIL - ${name}\n${error.stack}`); }
}
console.log(`${tests.length - failed}/${tests.length} passed (TZ=${process.env.TZ || 'default'})`);
process.exit(failed ? 1 : 0);
