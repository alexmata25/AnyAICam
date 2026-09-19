// Real, executed behavioral test of the mobile Playback page's
// scheduleMobileEventPoll()/isMobileEventPending() state machine (P0 #5
// remediation round 2, 2026-09-05, Codex second review). String-content
// assertions on the rendered HTML can prove the constants and markup
// exist, but they cannot exercise the actual async retry/cancellation/
// deadline timing that the original bugs depended on -- only running
// the real code with controllable timing can (same rationale as
// talk_mic_lifecycle.test.mjs and desktop_event_poll.test.mjs, this
// suite's own siblings).
//
// This file runs the ACTUAL extracted source (passed in as a file path
// argv[2], always extracted live from a real _render_customer_playback()
// render by the pytest wrapper that invokes this -- never a hand-copied
// duplicate that could drift from what ships) under Node, with a fake
// fetch/AbortController/setTimeout/Date.now/playbackDate/
// renderMobileRecentEvents this file fully controls the timing of.
//
// Usage: node mobile_event_poll.test.mjs /path/to/extracted_snippet.js
// Exit code 0 on all-pass, 1 on any failure (with details on stdout).

import { readFileSync } from "node:fs";
new Function(readFileSync(new URL('../../static/event_media.js',import.meta.url),'utf8'))();
import { setImmediate as realSetImmediate } from "node:timers";

const sourcePath = process.argv[2];
if (!sourcePath) {
  console.error("usage: node mobile_event_poll.test.mjs <path-to-js-source>");
  process.exit(2);
}
const snippetSource = readFileSync(sourcePath, "utf8");

// ---------------------------------------------------------------- fake clock

// Round 2 fix (2026-09-05, Codex second review): the fake timer queue
// now also advances a mocked Date.now() by each flushed timer's own
// delay -- round 1's fake clock never moved between synchronous
// flushes, so a deadlineAt = Date.now() + 120000 real elapsed-time
// deadline could never actually be exercised deterministically (every
// flush looked instantaneous). Advancing fakeNow in lockstep with
// which timer fires is what lets a test prove "settles once real
// elapsed time crosses the deadline" as a genuinely different claim
// from "settles once N attempts have run".
let fakeNow = Date.now();
Date.now = () => fakeNow;

let timerIdSeq = 1;
let pendingTimers = new Map(); // id -> { fn, ms }
function fakeSetTimeout(fn, ms) {
  const id = timerIdSeq++;
  pendingTimers.set(id, { fn, ms: ms || 0 });
  return id;
}
function fakeClearTimeout(id) {
  pendingTimers.delete(id);
}
async function settle() {
  // A real macrotask boundary (not just a handful of microtask ticks)
  // guarantees every pending .then()/await in a chain has had a chance
  // to run -- including ones this test can't predict the exact length
  // of -- while a fetch this test deliberately never resolves (see
  // neverResolves()) is left exactly as unresolved as a real in-flight
  // request would be.
  await new Promise((resolve) => realSetImmediate(resolve));
}
async function flushOneTimer() {
  const entries = [...pendingTimers.entries()];
  if (entries.length === 0) return false;
  const [id, { fn, ms }] = entries[0];
  pendingTimers.delete(id);
  fakeNow += ms;
  // Deliberately not awaited directly: a timer callback whose fetch
  // never resolves (see neverResolves()) must not hang this helper
  // forever -- settle() below drains everything that's actually ready
  // to run instead.
  const result = fn();
  if (result && typeof result.catch === "function") result.catch(() => {});
  await settle();
  return true;
}

globalThis.setTimeout = fakeSetTimeout;
globalThis.clearTimeout = fakeClearTimeout;

// playbackDate() itself is already covered elsewhere (it's the
// Playback page's own established timestamp helper) -- a plain Date
// parse is a faithful enough stand-in for these tests, which are about
// the polling state machine built on top of it, not that parsing.
globalThis.playbackDate = (timestamp) => new Date(timestamp);

class FakeAbortController {
  constructor() {
    this.signal = { aborted: false, _listeners: [] };
  }
  abort() {
    if (this.signal.aborted) return;
    this.signal.aborted = true;
    this.signal._listeners.forEach((fn) => fn());
  }
}
globalThis.AbortController = FakeAbortController;

let fetchQueue = [];
let fetchCalls = [];
globalThis.fetch = (url, options) => {
  fetchCalls.push({ url, signal: options && options.signal });
  const responder = fetchQueue.shift();
  return new Promise((resolve, reject) => {
    const signal = options && options.signal;
    if (signal) {
      signal._listeners.push(() => {
        const err = new Error("The operation was aborted.");
        err.name = "AbortError";
        reject(err);
      });
    }
    if (!responder) {
      reject(new Error("no fake fetch response queued for " + url));
      return;
    }
    Promise.resolve(responder()).then(resolve, reject);
  });
};

function jsonResponse(status, body) {
  return () => ({ ok: status >= 200 && status < 300, status, json: async () => body });
}
function neverResolves() {
  return () => new Promise(() => {});
}

let renderCalls = [];
globalThis.renderMobileRecentEvents = (cameraId, clips, events, options) => {
  renderCalls.push({ cameraId, clips, events: events.map((e) => ({ ...e })), options });
  mod.scheduleMobileEventPoll(cameraId,clips,events);
};

// The real camera-tile click handler updates this module-level
// variable AND calls stopMobileEventPoll() immediately, in that order
// -- see main.py's own click handler. Tests simulate a camera switch
// by doing exactly the same two things, in the same order, rather than
// calling scheduleMobileEventPoll() for the new camera directly (which
// would only exercise scheduleMobileEventPoll()'s own lazy mismatch
// detection, not the click handler's immediate cancellation this round
// of fixes actually added).
globalThis.selectedCameraId = null;

function resetFakes() {
  fetchQueue = [];
  fetchCalls = [];
  renderCalls = [];
  pendingTimers = new Map();
  globalThis.selectedCameraId = null;
}

// ---- load the real snippet, exposing what the tests need to drive ----
const factory = new Function(
  `${snippetSource}\nreturn { scheduleMobileEventPoll, stopMobileEventPoll, isMobileEventPending };`
);
const mod = factory();

function makeEvent(id, { hasClip = false, ageMs = 0 } = {}) {
  return {
    id,
    has_event_clip: hasClip,
    timestamp: new Date(Date.now() - ageMs).toISOString(),
  };
}

function selectCamera(cameraId) {
  // Mirrors the real camera-tile click handler exactly: update the
  // page's own selection state, then immediately stop the previous
  // camera's poll session -- never waiting for scheduleMobileEventPoll()
  // to notice the mismatch on its own next invocation.
  globalThis.selectedCameraId = cameraId;
  mod.stopMobileEventPoll();
}

// ---------------------------------------------------------------- tests

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("transient fetch exception does not permanently stop polling", async () => {
  resetFakes();
  selectCamera("cam-1");
  mod.scheduleMobileEventPoll("cam-1", [], [makeEvent("evt-1")]);
  fetchQueue.push(() => { throw new TypeError("network down"); });
  await flushOneTimer();
  if (fetchCalls.length !== 1) throw new Error(`expected 1 fetch call, got ${fetchCalls.length}`);
  if (renderCalls.length !== 1) throw new Error("failed fetch must still reconcile expiration");
  fetchQueue.push(jsonResponse(200, { events: [makeEvent("evt-1", { hasClip: true })] }));
  const flushed = await flushOneTimer();
  if (!flushed) throw new Error("polling did not reschedule after a transient network error");
  if (fetchCalls.length !== 2) throw new Error("expected a retry fetch after the transient error");
  if (renderCalls.length !== 2) throw new Error("expected a render after the successful retry");
});

test("HTTP 500 is retried, not treated as a permanent stop", async () => {
  resetFakes();
  selectCamera("cam-2");
  mod.scheduleMobileEventPoll("cam-2", [], [makeEvent("evt-2")]);
  fetchQueue.push(jsonResponse(500, {}));
  await flushOneTimer();
  if (renderCalls.length !== 1) throw new Error("failed HTTP request must still reconcile expiration");
  fetchQueue.push(jsonResponse(200, { events: [makeEvent("evt-2", { hasClip: true })] }));
  const flushed = await flushOneTimer();
  if (!flushed) throw new Error("polling did not reschedule after a 500 response");
  if (fetchCalls.length !== 2) throw new Error("expected a retry fetch after the 500");
  if (renderCalls.length !== 2) throw new Error("expected a render after recovering from 500");
});

test("a never-resolving fetch is bounded by its own timeout and retried, not hung forever", async () => {
  resetFakes();
  selectCamera("cam-5");
  mod.scheduleMobileEventPoll("cam-5", [], [makeEvent("evt-5")]);
  fetchQueue.push(neverResolves());
  await flushOneTimer(); // starts the fetch; it never resolves on its own
  if (fetchCalls.length !== 1) throw new Error("expected the fetch to have started");
  if (renderCalls.length !== 0) throw new Error("must not render while the fetch is still hung");
  fetchQueue.push(jsonResponse(200, { events: [makeEvent("evt-5", { hasClip: true })] }));
  const flushedTimeout = await flushOneTimer(); // the fetch's own internal timeout fires, aborting it
  if (!flushedTimeout) throw new Error("expected the fetch's own timeout to be queued");
  const flushedRetry = await flushOneTimer(); // finally reschedules
  if (!flushedRetry) throw new Error("expected a retry to be scheduled after the fetch timeout");
  if (renderCalls.length !== 2) throw new Error("expected a render after the retry succeeded");
});

test("camera A's response arriving after switching to camera B must not repaint (immediate cancellation + selectedCameraId guard)", async () => {
  resetFakes();
  // Camera A poll in flight.
  selectCamera("cam-a");
  mod.scheduleMobileEventPoll("cam-a", [], [makeEvent("evt-a")]);
  let resolveA;
  fetchQueue.push(() => new Promise((resolve) => { resolveA = resolve; }));
  await flushOneTimer(); // camera A's fetch is now in flight, unresolved
  const staleSignal = fetchCalls[0].signal;
  if (staleSignal.aborted) throw new Error("test setup error: camera A's signal aborted too early");

  // -> user clicks camera B. The real click handler updates
  // selectedCameraId AND calls stopMobileEventPoll() in the same
  // synchronous step -- immediately, not lazily on the next poll call.
  selectCamera("cam-b");
  if (!staleSignal.aborted) {
    throw new Error("switching to camera B must immediately abort camera A's in-flight request");
  }

  // -> camera B's own initial load is delayed (e.g. still fetching its
  // own recordings/date data) -- scheduleMobileEventPoll for cam-b
  // hasn't been called yet at this point, matching a real slow
  // renderCamera() for the newly selected camera.

  // -> camera A's original (aborted) response finally arrives anyway --
  // a real network response racing the abort is possible, not just the
  // AbortError path, which is exactly why the cameraId!==selectedCameraId
  // guard exists as a second, independent line of defense.
  resolveA({ ok: true, status: 200, json: async () => ({ events: [makeEvent("evt-a", { hasClip: true })] }) });
  await settle();
  if (renderCalls.some((c) => c.cameraId === "cam-a")) {
    throw new Error("camera A's response must never repaint the UI after switching to camera B");
  }
});

test("real elapsed-time deadline settles regardless of attempt count (not an attempt-count ceiling)", async () => {
  resetFakes();
  selectCamera("cam-6");
  // Each re-arm call below passes a freshly-timestamped event (mirroring
  // what renderMobileRecentEvents()'s real tail call always does with
  // the latest merged data) -- never a stale array fixed at test start,
  // which would let the event's OWN age-based pending window and the
  // poll's deadline race each other at the same elapsed-time mark and
  // make this test ambiguous about which one actually fired.
  const originalEvent=makeEvent("evt-6");
  const freshEvents = () => [originalEvent];
  mod.scheduleMobileEventPoll("cam-6", [], freshEvents());
  // 5 attempts that each hang and get timeout-aborted: 5 * (4000
  // interval + 8000 timeout) = 60000ms of real elapsed time for only 5
  // attempts -- proving the deadline is driven by elapsed time, not an
  // attempt counter (which round 1 would have required exactly 30
  // attempts to reach regardless of how long each one took).
  for (let i = 0; i < 5; i++) {
    fetchQueue.push(neverResolves());
    await flushOneTimer(); // starts the fetch
    await flushOneTimer(); // its own timeout fires
    mod.scheduleMobileEventPoll("cam-6", [], freshEvents());
  }
  // Continue with quick still-pending responses until elapsed time
  // crosses the 120s deadline -- 60000ms already elapsed, 15 more
  // attempts at 4000ms each reaches 120000ms, for 20 total attempts,
  // well under the old 30-attempt ceiling.
  for (let i = 0; i < 15; i++) {
    fetchQueue.push(jsonResponse(200, { events: freshEvents() }));
    await flushOneTimer();
    mod.scheduleMobileEventPoll("cam-6", [], freshEvents());
  }
  const settledCall = renderCalls[renderCalls.length - 1];
  if (!settledCall || mod.isMobileEventPending(settledCall.events[0])) {
    throw new Error(`expected settled=true once real elapsed time reached the deadline (used 20 attempts, not 30)`);
  }
});

test("media arriving between 61s and 120s is still picked up, not cut off at the old 60s ceiling", async () => {
  resetFakes();
  selectCamera("cam-4");
  const events = [makeEvent("evt-4")];
  mod.scheduleMobileEventPoll("cam-4", [], events);
  // 16 attempts * 4s = 64s -- past the OLD ~60s/15-attempt ceiling,
  // still well inside the real 120s window.
  for (let i = 0; i < 15; i++) {
    fetchQueue.push(jsonResponse(200, { events: [makeEvent("evt-4")] }));
    await flushOneTimer();
    mod.scheduleMobileEventPoll("cam-4", [], events);
  }
  fetchQueue.push(jsonResponse(200, { events: [makeEvent("evt-4", { hasClip: true })] }));
  await flushOneTimer();
  const readyCall = renderCalls[renderCalls.length - 1];
  if (!readyCall || !readyCall.events[0].has_event_clip) {
    throw new Error("an event that becomes ready after the old 60s ceiling must still be picked up");
  }
});

test("a final failed request exactly at the deadline still settles, never leaves Processing… indefinitely", async () => {
  resetFakes();
  selectCamera("cam-7");
  const originalEvent=makeEvent("evt-7");
  const freshEvents = () => [originalEvent];
  mod.scheduleMobileEventPoll("cam-7", [], freshEvents());
  for (let i = 0; i < 29; i++) {
    fetchQueue.push(jsonResponse(200, { events: freshEvents() }));
    await flushOneTimer();
    mod.scheduleMobileEventPoll("cam-7", [], freshEvents());
  }
  fetchQueue.push(() => { throw new TypeError("final failure at the deadline"); });
  await flushOneTimer();
  mod.scheduleMobileEventPoll("cam-7", [], freshEvents());
  const settledCall = renderCalls[renderCalls.length - 1];
  if (!settledCall || mod.isMobileEventPending(settledCall.events[0])) {
    throw new Error("a final failure right at the deadline must still settle, never leave Processing… forever");
  }
});

test("isMobileEventPending: has_event_clip is never pending regardless of age", () => {
  if (mod.isMobileEventPending(makeEvent("x", { hasClip: true, ageMs: 0 }))) {
    throw new Error("an event with a real clip must never be reported pending");
  }
});

test("isMobileEventPending: a fresh clipless event is pending, an old one is not", () => {
  if (!mod.isMobileEventPending(makeEvent("x", { hasClip: false, ageMs: 1000 }))) {
    throw new Error("a 1s-old clipless event should still be pending");
  }
  if (mod.isMobileEventPending(makeEvent("x", { hasClip: false, ageMs: 200000 }))) {
    throw new Error("a 200s-old clipless event should no longer be pending");
  }
});

// ---------------------------------------------------------------- runner

let failures = [];
for (const { name, fn } of tests) {
  try {
    await fn();
    console.log(`PASS  ${name}`);
  } catch (error) {
    failures.push(name);
    console.log(`FAIL  ${name}`);
    console.log(`      ${(error && error.stack) || error}`);
  }
}
console.log(`\n${tests.length - failures.length}/${tests.length} passed`);
process.exit(failures.length ? 1 : 0);
