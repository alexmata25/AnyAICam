// Real, executed behavioral test of the mobile Playback page's
// scheduleMobileEventPoll()/isMobileEventPending() state machine (P0 #5
// remediation, 2026-09-05, Codex review). String-content assertions on
// the rendered HTML can prove the constants and markup exist, but they
// cannot exercise the actual async retry/cancellation/deadline timing
// that the original bugs depended on -- only running the real code with
// controllable timing can (same rationale as talk_mic_lifecycle.test.mjs,
// this suite's own sibling).
//
// This file runs the ACTUAL extracted source (passed in as a file path
// argv[2], always extracted live from a real _render_customer_playback()
// render by the pytest wrapper that invokes this -- never a hand-copied
// duplicate that could drift from what ships) under Node, with a fake
// fetch/AbortController/setTimeout/playbackDate/renderMobileRecentEvents
// this file fully controls the timing and observes the calls of.
//
// Usage: node mobile_event_poll.test.mjs /path/to/extracted_snippet.js
// Exit code 0 on all-pass, 1 on any failure (with details on stdout).

import { readFileSync } from "node:fs";
import { setImmediate as realSetImmediate } from "node:timers";

const sourcePath = process.argv[2];
if (!sourcePath) {
  console.error("usage: node mobile_event_poll.test.mjs <path-to-js-source>");
  process.exit(2);
}
const snippetSource = readFileSync(sourcePath, "utf8");

// ---------------------------------------------------------------- fakes

// Deterministic fake timer queue -- no real wall-clock waiting, and each
// pending timer is flushed one at a time under the test's own control so
// interleavings (e.g. "switch camera while a fetch is still in flight")
// are reproducible instead of racy.
let timerIdSeq = 1;
let pendingTimers = new Map();
function fakeSetTimeout(fn) {
  const id = timerIdSeq++;
  pendingTimers.set(id, fn);
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
  const [id, fn] = entries[0];
  pendingTimers.delete(id);
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
};

function resetFakes() {
  fetchQueue = [];
  fetchCalls = [];
  renderCalls = [];
  pendingTimers = new Map();
}

// ---- load the real snippet, exposing what the tests need to drive ----
const factory = new Function(
  `${snippetSource}\nreturn { scheduleMobileEventPoll, isMobileEventPending };`
);
const mod = factory();

function makeEvent(id, { hasClip = false, ageMs = 0 } = {}) {
  return {
    id,
    has_event_clip: hasClip,
    timestamp: new Date(Date.now() - ageMs).toISOString(),
  };
}

// ---------------------------------------------------------------- tests

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("transient fetch exception does not permanently stop polling", async () => {
  resetFakes();
  mod.scheduleMobileEventPoll("cam-1", [], [makeEvent("evt-1")]);
  fetchQueue.push(() => { throw new TypeError("network down"); });
  await flushOneTimer();
  if (fetchCalls.length !== 1) throw new Error(`expected 1 fetch call, got ${fetchCalls.length}`);
  if (renderCalls.length !== 0) throw new Error("must not render on a thrown fetch");
  fetchQueue.push(jsonResponse(200, { events: [makeEvent("evt-1", { hasClip: true })] }));
  const flushed = await flushOneTimer();
  if (!flushed) throw new Error("polling did not reschedule after a transient network error");
  if (fetchCalls.length !== 2) throw new Error("expected a retry fetch after the transient error");
  if (renderCalls.length !== 1) throw new Error("expected a render after the successful retry");
});

test("HTTP 500 is retried, not treated as a permanent stop", async () => {
  resetFakes();
  mod.scheduleMobileEventPoll("cam-2", [], [makeEvent("evt-2")]);
  fetchQueue.push(jsonResponse(500, {}));
  await flushOneTimer();
  if (renderCalls.length !== 0) throw new Error("must not render on a non-2xx response");
  fetchQueue.push(jsonResponse(200, { events: [makeEvent("evt-2", { hasClip: true })] }));
  const flushed = await flushOneTimer();
  if (!flushed) throw new Error("polling did not reschedule after a 500 response");
  if (fetchCalls.length !== 2) throw new Error("expected a retry fetch after the 500");
  if (renderCalls.length !== 1) throw new Error("expected a render after recovering from 500");
});

test("switching cameras aborts the in-flight request for the old one", async () => {
  resetFakes();
  mod.scheduleMobileEventPoll("cam-a", [], [makeEvent("evt-a")]);
  fetchQueue.push(neverResolves());
  await flushOneTimer(); // starts the in-flight fetch for cam-a; it never resolves on its own
  const staleSignal = fetchCalls[0].signal;
  if (staleSignal.aborted) throw new Error("test setup error: signal aborted before camera switch");
  mod.scheduleMobileEventPoll("cam-b", [], [makeEvent("evt-b")]);
  if (!staleSignal.aborted) {
    throw new Error("switching cameras must abort the previous camera's in-flight request");
  }
});

test("a stale response from a superseded camera never repaints the newly selected one", async () => {
  resetFakes();
  mod.scheduleMobileEventPoll("cam-a", [], [makeEvent("evt-a")]);
  let resolveStale;
  fetchQueue.push(() => new Promise((resolve) => { resolveStale = resolve; }));
  await flushOneTimer();
  mod.scheduleMobileEventPoll("cam-b", [], [makeEvent("evt-b")]);
  // The old (aborted) request's underlying promise finally "arrives"
  // anyway -- a real network response racing the abort is possible,
  // not just the AbortError path.
  resolveStale({ ok: true, status: 200, json: async () => ({ events: [makeEvent("evt-a", { hasClip: true })] }) });
  await settle();
  if (renderCalls.some((c) => c.cameraId === "cam-a")) {
    throw new Error("a stale response from the old camera must never repaint anything");
  }
});

test("polling duration is aligned to the full 120s window (30 attempts at a 4s interval)", async () => {
  resetFakes();
  const events = [makeEvent("evt-3")];
  mod.scheduleMobileEventPoll("cam-3", [], events);
  for (let i = 0; i < 30; i++) {
    fetchQueue.push(jsonResponse(200, { events: [makeEvent("evt-3")] }));
    const flushed = await flushOneTimer();
    if (!flushed) throw new Error(`expected attempt ${i + 1}/30 to still be scheduled`);
    // In production, renderMobileRecentEvents() itself re-invokes
    // scheduleMobileEventPoll() after every successful render to
    // continue the chain (see that function's own tail call) -- the
    // spy standing in for it here doesn't, so the test re-arms it
    // explicitly, exactly mirroring what the real render path does.
    mod.scheduleMobileEventPoll("cam-3", [], events);
  }
  // By now 30 attempts (the full 120s/4s window) have already run --
  // the deadline check inside the call above should have fired a
  // settled render instead of scheduling attempt 31.
  const settledCall = renderCalls[renderCalls.length - 1];
  if (!settledCall || !settledCall.options || !settledCall.options.settled) {
    throw new Error("expected a settled=true render pass once the 120s deadline was reached");
  }
});

test("media arriving between 61s and 120s is still picked up, not cut off at the old 60s ceiling", async () => {
  resetFakes();
  const events = [makeEvent("evt-4")];
  mod.scheduleMobileEventPoll("cam-4", [], events);
  // 16 attempts * 4s = 64s -- past the OLD ~60s/15-attempt ceiling,
  // still well inside the real 120s window. Each iteration re-arms via
  // scheduleMobileEventPoll() itself, exactly what renderMobileRecentEvents()
  // does for real after every successful render.
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
