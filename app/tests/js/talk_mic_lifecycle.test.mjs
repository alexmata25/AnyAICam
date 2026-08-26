// Real, executed behavioral test of wireTalkMic()'s async pointer
// lifecycle -- the production bug this fixes (a stale start() resuming
// after stop() had already cleared sessionId, opening a WebSocket at
// /sessions/null/audio) is a race between concurrent promise chains,
// which no amount of string-content assertion on the generated HTML
// can catch. This file runs the ACTUAL _TALK_MIC_JS source (passed in
// as a file path argv[2], always extracted live from live_view_page.py
// by the pytest wrapper that invokes this -- never a hand-copied
// duplicate that could drift from what ships) under Node, with fake
// fetch/getUserMedia/WebSocket/AudioContext implementations the test
// fully controls the timing of, to deterministically reproduce every
// interleaving the original bug depended on.
//
// Usage: node talk_mic_lifecycle.test.mjs /path/to/extracted_talk_mic_js.js
// Exit code 0 on all-pass, 1 on any failure (with details on stdout).

import { readFileSync } from "node:fs";

const sourcePath = process.argv[2];
if (!sourcePath) {
  console.error("usage: node talk_mic_lifecycle.test.mjs <path-to-js-source>");
  process.exit(2);
}
const wireTalkMicSource = readFileSync(sourcePath, "utf8");

// ---------------------------------------------------------------- fakes

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

// Every URL ever passed to the fake WebSocket constructor across the
// entire test run, never reset between scenarios -- the final blanket
// safety-net assertion scans this.
const allConstructedWsUrls = [];

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    allConstructedWsUrls.push(url);
    this.readyState = FakeWebSocket.CONNECTING;
    this.onopen = null;
    this.onclose = null;
    this.onerror = null;
    this.sent = [];
    this.closed = false;
    FakeWebSocket.instances.push(this);
  }
  send(data) { this.sent.push(data); }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = FakeWebSocket.CLOSED;
  }
  // Test helper only -- simulates the real handshake completing. Real
  // browsers never fire onopen for a socket close()d while CONNECTING,
  // so this mirrors that by refusing to open an already-closed socket.
  triggerOpen() {
    if (this.closed) return;
    this.readyState = FakeWebSocket.OPEN;
    if (this.onopen) this.onopen();
  }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.instances = [];

class FakeAudioContext {
  constructor() { this.sampleRate = 48000; this.destination = {}; this.closed = false; }
  createMediaStreamSource(stream) { return { stream, connect() {}, disconnect() {} }; }
  createScriptProcessor() { return { onaudioprocess: null, connect() {}, disconnect() {} }; }
  createGain() { return { gain: { value: 1 }, connect() {}, disconnect() {} }; }
  close() { this.closed = true; return Promise.resolve(); }
}

function makeMediaStream() {
  const track = { stopped: false, stop() { this.stopped = true; } };
  return { _tracks: [track], getTracks() { return this._tracks; } };
}

function makeButton() {
  const classes = new Set();
  return {
    disabled: false,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
    },
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    _captureLog: [],
    setPointerCapture(pointerId) { this._captureLog.push(["set", pointerId]); },
    releasePointerCapture(pointerId) { this._captureLog.push(["release", pointerId]); },
  };
}

function triggerListener(target, type, event) {
  const fns = target._listeners[type] || [];
  fns.forEach((fn) => fn(event));
}

// Fake PointerEvent: records whether preventDefault()/stopPropagation()
// were called, without needing a real DOM Event implementation.
function makeFakePointerEvent(pointerId) {
  const event = {
    pointerId,
    defaultPrevented: false,
    propagationStopped: false,
    preventDefault() { event.defaultPrevented = true; },
    stopPropagation() { event.propagationStopped = true; },
  };
  return event;
}

function fakeStartResponse(sessionId, ok = true) {
  return { ok, json: () => Promise.resolve({ session_id: sessionId }) };
}

// Lets pending microtask chains (and one macrotask boundary, to be
// safe across the multiple sequential awaits inside start()) fully
// settle before the test inspects state.
async function flush() {
  for (let i = 0; i < 5; i++) await Promise.resolve();
  await new Promise((r) => setImmediate(r));
}

// Per-scenario mutable fixtures, reset by resetAll().
let fetchCalls, gumCalls, toastMessages, window_, button;

// Node has its own built-in (getter-only, as of Node 21+) `navigator`
// global -- plain assignment throws, so every global this harness
// overrides goes through defineProperty with configurable+writable set,
// to be robust regardless of which of these a given Node version
// happens to predefine.
function setGlobal(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
}

function resetAll() {
  fetchCalls = [];
  gumCalls = [];
  toastMessages = [];
  FakeWebSocket.instances = [];

  setGlobal("fetch", (url, opts) => {
    const rec = { url, opts, deferred: deferred() };
    fetchCalls.push(rec);
    return rec.deferred.promise;
  });
  setGlobal("navigator", {
    mediaDevices: {
      getUserMedia: () => {
        const d = deferred();
        gumCalls.push(d);
        return d.promise;
      },
    },
  });
  setGlobal("WebSocket", FakeWebSocket);
  setGlobal("location", { protocol: "https:", host: "app.anyaicam.test" });
  setGlobal("showToast", (msg) => { toastMessages.push(msg); });
  window_ = { AudioContext: FakeAudioContext, webkitAudioContext: FakeAudioContext, _listeners: {} };
  window_.addEventListener = function (type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); };
  setGlobal("window", window_);

  button = makeButton();
  const factory = new Function(`${wireTalkMicSource}\nreturn wireTalkMic;`);
  const wireTalkMic = factory();
  wireTalkMic(button, "cam-1");
}

function fetchCallsMatching(suffix) {
  return fetchCalls.filter((c) => c.url.endsWith(suffix));
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// ---------------------------------------------------------------- scenarios

const tests = [];
function test(name, fn) { tests.push({ name, fn }); }

test("rapid tap: release before /talk/start even resolves", async () => {
  resetAll();
  triggerListener(button, "pointerdown");
  assert(fetchCallsMatching("/talk/start").length === 1, "expected one /talk/start call");
  triggerListener(button, "pointerup");
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-rapid"));
  await flush();
  assert(FakeWebSocket.instances.length === 0, "no WebSocket should ever be constructed for a superseded press");
  assert(fetchCallsMatching("/talk/sessions/sess-rapid/stop").length === 1, "the orphaned session must be cleaned up exactly once");
});

test("normal 1-second hold: full happy path, clean release after WS open", async () => {
  resetAll();
  triggerListener(button, "pointerdown");
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-normal"));
  await flush();
  gumCalls[0].resolve(makeMediaStream());
  await flush();
  assert(FakeWebSocket.instances.length === 1, "expected exactly one WebSocket to be constructed");
  const ws = FakeWebSocket.instances[0];
  assert(ws.url.includes("/talk/sessions/sess-normal/audio"), `unexpected WS url: ${ws.url}`);
  assert(!ws.url.includes("/sessions/null/"), `WS url must never contain a null session id: ${ws.url}`);
  ws.triggerOpen();
  assert(button.classList.contains("active"), "button should be marked active once the WS is actually open");
  triggerListener(button, "pointerup");
  assert(!button.classList.contains("active"), "active class must be cleared on release");
  assert(ws.closed, "the WebSocket must be closed on release");
  assert(fetchCallsMatching("/talk/sessions/sess-normal/stop").length === 0, "no orphan-cleanup POST is needed once the WS was already open -- the WS route's own disconnect handling covers it");
});

test("release during REST fetch (before /talk/start resolves)", async () => {
  resetAll();
  triggerListener(button, "pointerdown");
  triggerListener(button, "pointerup");
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-fetch-race"));
  await flush();
  assert(FakeWebSocket.instances.length === 0, "no WebSocket for a press released mid-fetch");
  assert(fetchCallsMatching("/talk/sessions/sess-fetch-race/stop").length === 1, "the session created after release must still be cleaned up");
});

test("release during getUserMedia (mid permission prompt)", async () => {
  resetAll();
  triggerListener(button, "pointerdown");
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-gum-race"));
  await flush();
  triggerListener(button, "pointerup");
  await flush();
  assert(fetchCallsMatching("/talk/sessions/sess-gum-race/stop").length === 1, "stop() must immediately release the REST session created just before getUserMedia");
  const lateStream = makeMediaStream();
  gumCalls[0].resolve(lateStream);
  await flush();
  assert(lateStream._tracks[0].stopped, "a mic stream that arrives after release must have its tracks stopped immediately");
  assert(FakeWebSocket.instances.length === 0, "no WebSocket for a press released mid-getUserMedia");
  assert(fetchCallsMatching("/talk/sessions/sess-gum-race/stop").length === 1, "no duplicate stop POST once start() itself also notices the staleness");
});

test("permission prompt delay: nothing happens prematurely while still held", async () => {
  resetAll();
  triggerListener(button, "pointerdown");
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-delay"));
  await flush();
  await flush(); // extra idle ticks with the permission prompt still pending
  assert(FakeWebSocket.instances.length === 0, "must still be waiting on the permission prompt");
  assert(fetchCallsMatching("/stop").length === 0, "a still-held press must not be cleaned up just because getUserMedia is slow");
  gumCalls[0].resolve(makeMediaStream());
  await flush();
  assert(FakeWebSocket.instances.length === 1);
  assert(FakeWebSocket.instances[0].url.includes("sess-delay"));
});

test("permission denied: session released, held resets for the next press", async () => {
  resetAll();
  triggerListener(button, "pointerdown");
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-denied"));
  await flush();
  gumCalls[0].reject(new Error("NotAllowedError"));
  await flush();
  assert(FakeWebSocket.instances.length === 0);
  assert(fetchCallsMatching("/talk/sessions/sess-denied/stop").length === 1, "a denied prompt must still release the REST session it created");
  assert(toastMessages.length === 1, "expected a user-facing toast on permission denial");
  triggerListener(button, "pointerdown");
  assert(fetchCallsMatching("/talk/start").length === 2, "held must have reset so a fresh press can start a new session");
});

test("pointercancel during getUserMedia behaves exactly like pointerup", async () => {
  resetAll();
  triggerListener(button, "pointerdown");
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-cancel"));
  await flush();
  triggerListener(button, "pointercancel");
  await flush();
  assert(fetchCallsMatching("/talk/sessions/sess-cancel/stop").length === 1);
  const lateStream = makeMediaStream();
  gumCalls[0].resolve(lateStream);
  await flush();
  assert(lateStream._tracks[0].stopped);
  assert(FakeWebSocket.instances.length === 0);
});

test("pointerleave during getUserMedia behaves exactly like pointerup", async () => {
  resetAll();
  triggerListener(button, "pointerdown");
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-leave"));
  await flush();
  triggerListener(button, "pointerleave");
  await flush();
  assert(fetchCallsMatching("/talk/sessions/sess-leave/stop").length === 1);
  const lateStream = makeMediaStream();
  gumCalls[0].resolve(lateStream);
  await flush();
  assert(lateStream._tracks[0].stopped);
  assert(FakeWebSocket.instances.length === 0);
});

test("pagehide during getUserMedia behaves exactly like pointerup", async () => {
  resetAll();
  triggerListener(button, "pointerdown");
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-pagehide"));
  await flush();
  triggerListener(window_, "pagehide");
  await flush();
  assert(fetchCallsMatching("/talk/sessions/sess-pagehide/stop").length === 1);
  const lateStream = makeMediaStream();
  gumCalls[0].resolve(lateStream);
  await flush();
  assert(lateStream._tracks[0].stopped);
  assert(FakeWebSocket.instances.length === 0);
});

test("repeated press/release cycles leave no cross-cycle leakage", async () => {
  resetAll();
  for (let i = 0; i < 3; i++) {
    triggerListener(button, "pointerdown");
    const startCall = fetchCallsMatching("/talk/start")[i];
    startCall.deferred.resolve(fakeStartResponse(`sess-cycle-${i}`));
    await flush();
    gumCalls[i].resolve(makeMediaStream());
    await flush();
    assert(FakeWebSocket.instances.length === i + 1, `expected ${i + 1} WebSocket(s) constructed by cycle ${i}`);
    const ws = FakeWebSocket.instances[i];
    assert(ws.url.includes(`sess-cycle-${i}`));
    ws.triggerOpen();
    assert(button.classList.contains("active"));
    triggerListener(button, "pointerup");
    assert(!button.classList.contains("active"));
    assert(ws.closed);
  }
  assert(fetchCallsMatching("/stop").length === 0, "every cycle here released after its WS was open, so no orphan-cleanup POSTs are expected");
  assert(fetchCallsMatching("/talk/start").length === 3);
});

test("a newer press is never interfered with by a stale prior start()", async () => {
  resetAll();
  // Press 1: gets as far as its own getUserMedia await, then is released.
  triggerListener(button, "pointerdown");
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-stale"));
  await flush();
  triggerListener(button, "pointerup");
  await flush();
  assert(fetchCallsMatching("/talk/sessions/sess-stale/stop").length === 1);

  // Press 2 begins immediately (held was reset by press 1's stop()).
  triggerListener(button, "pointerdown");
  assert(fetchCallsMatching("/talk/start").length === 2, "a new press must be able to start right after the prior one's release");
  // Index by matching /talk/start calls specifically, not raw fetchCalls
  // position -- press 1's own orphan-cleanup /stop POST (fired by its
  // stop()) already occupies a fetchCalls slot in between the two
  // /talk/start calls.
  fetchCallsMatching("/talk/start")[1].deferred.resolve(fakeStartResponse("sess-fresh"));
  await flush();

  // Press 2's permission grant arrives first...
  const freshStream = makeMediaStream();
  gumCalls[1].resolve(freshStream);
  await flush();
  assert(FakeWebSocket.instances.length === 1, "only the newer press should ever open a WebSocket");
  assert(FakeWebSocket.instances[0].url.includes("sess-fresh"));
  assert(!freshStream._tracks[0].stopped);

  // ...then press 1's stale permission grant finally resolves late.
  const staleStream = makeMediaStream();
  gumCalls[0].resolve(staleStream);
  await flush();
  assert(FakeWebSocket.instances.length === 1, "the stale press must not construct a second WebSocket even after its own getUserMedia resolves");
  assert(staleStream._tracks[0].stopped, "the stale press's late-arriving mic stream must still be stopped");
});

test("no WebSocket url ever contains a null or empty session id, across every scenario run above", async () => {
  for (const url of allConstructedWsUrls) {
    assert(!/\/sessions\/(null|undefined)?\/audio/.test(url), `a WebSocket was constructed with a missing session id: ${url}`);
  }
  assert(allConstructedWsUrls.length > 0, "sanity check: earlier scenarios should have constructed at least one real WebSocket");
});

// ---------------------------------------------------------------- touch/pointer-conflict hardening (real mobile bug: video pauses on mic press, resumes on release)

test("pointerdown prevents default, stops propagation, and captures the pointer", () => {
  resetAll();
  const event = makeFakePointerEvent(7);
  triggerListener(button, "pointerdown", event);
  assert(event.defaultPrevented, "pointerdown must call preventDefault()");
  assert(event.propagationStopped, "pointerdown must call stopPropagation()");
  assert(
    button._captureLog.some(([action, id]) => action === "set" && id === 7),
    `expected setPointerCapture(7) to have been called, got: ${JSON.stringify(button._captureLog)}`
  );
});

test("pointerup prevents default, stops propagation, and releases the pointer capture", async () => {
  resetAll();
  triggerListener(button, "pointerdown", makeFakePointerEvent(3));
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-touch"));
  await flush();
  gumCalls[0].resolve(makeMediaStream());
  await flush();
  FakeWebSocket.instances[0].triggerOpen();

  const upEvent = makeFakePointerEvent(3);
  triggerListener(button, "pointerup", upEvent);
  assert(upEvent.defaultPrevented, "pointerup must call preventDefault()");
  assert(upEvent.propagationStopped, "pointerup must call stopPropagation()");
  assert(
    button._captureLog.some(([action, id]) => action === "release" && id === 3),
    `expected releasePointerCapture(3) to have been called, got: ${JSON.stringify(button._captureLog)}`
  );
});

test("pointercancel and pointerleave also prevent default and stop propagation", async () => {
  for (const releaseType of ["pointercancel", "pointerleave"]) {
    resetAll();
    triggerListener(button, "pointerdown", makeFakePointerEvent(9));
    fetchCalls[0].deferred.resolve(fakeStartResponse(`sess-${releaseType}`));
    await flush();
    const releaseEvent = makeFakePointerEvent(9);
    triggerListener(button, releaseType, releaseEvent);
    assert(releaseEvent.defaultPrevented, `${releaseType} must call preventDefault()`);
    assert(releaseEvent.propagationStopped, `${releaseType} must call stopPropagation()`);
  }
});

test("internal stop() calls with no event at all (ws.onclose, pagehide) never throw", async () => {
  resetAll();
  triggerListener(button, "pointerdown", makeFakePointerEvent(1));
  fetchCalls[0].deferred.resolve(fakeStartResponse("sess-noevent"));
  await flush();
  gumCalls[0].resolve(makeMediaStream());
  await flush();
  const ws = FakeWebSocket.instances[0];
  ws.triggerOpen();
  // Simulates ws.onclose firing stop() with no event argument at all --
  // must not throw, and must still fully tear the session down.
  ws.onclose();
  assert(!button.classList.contains("active"), "stop() with no event must still clear the active state");
});

// ---------------------------------------------------------------- runner

const failures = [];
for (const { name, fn } of tests) {
  try {
    await fn();
    console.log(`PASS  ${name}`);
  } catch (err) {
    failures.push({ name, err });
    console.log(`FAIL  ${name}`);
    console.log(`      ${err.stack || err}`);
  }
}

console.log(`\n${tests.length - failures.length}/${tests.length} passed`);
process.exit(failures.length ? 1 : 0);
