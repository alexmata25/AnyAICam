// Executed behavioral test of the Voice Call screen's End call while Talk
// is active (2026-09-30). Runs the ACTUAL shipped sources -- live_view_page
// _TALK_MIC_JS (wireTalkMic) and aac_voice_call _CALL_CONTROLS_JS, both
// passed in as file paths by the pytest wrapper -- against fake DOM,
// fetch, getUserMedia, WebSocket and AudioContext objects this file
// controls.
//
// Usage: node voice_call_end_talk.test.mjs <talk_mic.js> <call_controls.js>

import { readFileSync } from "node:fs";

const talkSource = readFileSync(process.argv[2], "utf8");
const callSource = readFileSync(process.argv[3], "utf8");

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

class FakeWebSocket {
  constructor(url) {
    this.url = url; this.readyState = 0; this.sent = []; this.closed = false;
    FakeWebSocket.instances.push(this);
  }
  send(data) { if (this.closed) throw new Error("send after close"); this.sent.push(data); }
  close() { if (this.closed) return; this.closed = true; this.readyState = 3; }
  triggerOpen() { if (this.closed) return; this.readyState = 1; if (this.onopen) this.onopen(); }
}
FakeWebSocket.OPEN = 1;

class FakeAudioContext {
  constructor() { this.sampleRate = 48000; this.destination = {}; this.closed = false; FakeAudioContext.last = this; }
  createMediaStreamSource(stream) { return { stream, connect() {}, disconnect() {} }; }
  createScriptProcessor() { const p = { onaudioprocess: null, connect() {}, disconnect() {} }; FakeAudioContext.processor = p; return p; }
  createGain() { return { gain: { value: 1 }, connect() {}, disconnect() {} }; }
  close() { this.closed = true; return Promise.resolve(); }
}

function makeStream(label) {
  const track = { label, readyState: "live", stopped: false, stop() { this.stopped = true; this.readyState = "ended"; } };
  const stream = {
    _tracks: [track],
    getTracks() { return this._tracks; },
    getAudioTracks() { return this._tracks; },
    clone() { const c = makeStream(label + "-clone"); streams.push(c); return c; },
  };
  return stream;
}

function makeElement(id) {
  return {
    id, disabled: false, hidden: false, textContent: "", _listeners: {},
    classList: (() => { const set = new Set(); return { add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c) }; })(),
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    setPointerCapture() {}, releasePointerCapture() {}, setAttribute() {},
  };
}

function fire(el, type, event = {}) {
  const e = { pointerId: 1, preventDefault() {}, stopPropagation() {}, ...event };
  (el._listeners[type] || []).forEach((fn) => { const r = fn(e); if (r && r.catch) r.catch(() => {}); });
}

async function flush() {
  for (let i = 0; i < 6; i++) await Promise.resolve();
  await new Promise((r) => setImmediate(r));
}

function setGlobal(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
}

let fetchCalls, gumCalls, toasts, streams, els, mic;

function setup({ phone = false, initiallyOver = false, autoFetch = true } = {}) {
  fetchCalls = []; gumCalls = []; toasts = []; streams = [];
  FakeWebSocket.instances = [];
  els = {};
  for (const id of ["voice-call-answer", "voice-call-end", "voice-call-state", "voice-call-ended", "live-view-video", "live-view-mute"]) els[id] = makeElement(id);
  els["voice-call-ended"].hidden = !initiallyOver;
  mic = makeElement("talk-mic-cam-1");
  setGlobal("document", {
    getElementById: (id) => els[id] || null,
    querySelectorAll: (sel) => (sel === ".talk-mic" ? [mic] : []),
  });
  setGlobal("fetch", (url, opts) => {
    const rec = { url, opts, d: deferred() };
    fetchCalls.push(rec);
    if (autoFetch) {
      if (url.endsWith("/talk/start")) rec.d.resolve({ ok: true, status: 200, json: () => Promise.resolve({ session_id: "sess-1" }) });
      else rec.d.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    }
    return rec.d.promise;
  });
  setGlobal("navigator", { mediaDevices: { getUserMedia: () => { const d = deferred(); gumCalls.push(d); return d.promise; } } });
  setGlobal("WebSocket", FakeWebSocket);
  setGlobal("location", { protocol: "https:", host: "app.test" });
  setGlobal("showToast", (m) => toasts.push(m));
  const win = { AudioContext: FakeAudioContext, _listeners: {}, addEventListener(t, f) { (this._listeners[t] = this._listeners[t] || []).push(f); } };
  if (phone) win.matchMedia = () => ({ matches: true });
  setGlobal("window", win);
  const wireTalkMic = new Function(`${talkSource}\nreturn wireTalkMic;`)();
  wireTalkMic(mic, "cam-1");
  new Function("eventId", "callInitiallyOver", callSource)("evt-1", initiallyOver);
}

const calls = (suffix) => fetchCalls.filter((c) => c.url.endsWith(suffix));

// Answer, grant the microphone, then start Talk and let it go live.
async function answerAndTalk({ phone = false } = {}) {
  fire(els["voice-call-answer"], "click");
  await flush();
  const callMic = makeStream("call-mic"); streams.push(callMic);
  gumCalls[0].resolve(callMic);
  await flush();
  fire(mic, "pointerdown");
  await flush();
  const ws = FakeWebSocket.instances[0];
  if (!ws) throw new Error("talk WebSocket was not opened");
  ws.triggerOpen();
  FakeAudioContext.processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array([0.1, -0.1]) } });
  if (ws.sent.length !== 1) throw new Error("audio should be flowing before End");
  return { ws, callMic, talkStream: streams.find((s) => s._tracks[0].label === "call-mic-clone") };
}

function assert(cond, msg) { if (!cond) throw new Error(msg); }

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

for (const phone of [false, true]) {
  const mode = phone ? "phone (tap to talk)" : "desktop (press and hold)";

  test(`${mode}: End call while Talk is live stops Talk, the microphone and transmission`, async () => {
    setup({ phone });
    const { ws, callMic, talkStream } = await answerAndTalk({ phone });
    fire(els["voice-call-end"], "click");
    await flush();
    assert(ws.closed, "the talk WebSocket to the camera must be closed");
    assert(talkStream && talkStream._tracks[0].stopped, "the Talk microphone track must be stopped");
    assert(callMic._tracks[0].stopped, "the call's own microphone must be released");
    assert(FakeAudioContext.last.closed, "the audio pipeline must be closed");
    const before = ws.sent.length;
    FakeAudioContext.processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array([0.2]) } });
    assert(ws.sent.length === before, "no audio may be sent after End");
    assert(!mic.classList.contains("active") && !mic.classList.contains("live"), "the mic must no longer show as talking");
    assert(els["voice-call-answer"].disabled && els["voice-call-end"].disabled, "Answer and End must be disabled");
    assert(mic.disabled, "the mic must be disabled after the call ends");
    assert(els["voice-call-ended"].hidden === false, "'Call ended' must be visible");
    assert(els["voice-call-state"].textContent === "ended", "state must read ended");
    assert(calls("/end").length === 1, "the server must be told exactly once");
    assert(toasts.includes("Call ended."), "the user must be told the call ended");
  });

  test(`${mode}: repeated End presses are harmless`, async () => {
    setup({ phone });
    await answerAndTalk({ phone });
    for (let i = 0; i < 11; i++) fire(els["voice-call-end"], "click");
    await flush();
    assert(calls("/end").length === 1, `expected one /end request, got ${calls("/end").length}`);
    assert(toasts.filter((t) => t === "Call ended.").length === 1, "only one 'Call ended' message");
  });

  test(`${mode}: the mic cannot start Talk again after End`, async () => {
    setup({ phone });
    await answerAndTalk({ phone });
    fire(els["voice-call-end"], "click");
    await flush();
    const starts = calls("/talk/start").length;
    fire(mic, "pointerdown");
    await flush();
    assert(calls("/talk/start").length === starts, "no new /talk/start after the call ended");
  });
}

test("End while /talk/start is still in flight: no WebSocket, orphan session released", async () => {
  setup({ autoFetch: false });
  fire(els["voice-call-answer"], "click");
  fetchCalls[0].d.resolve({ ok: true, json: () => Promise.resolve({}) });
  await flush();
  gumCalls[0].resolve(makeStream("call-mic"));
  await flush();
  fire(mic, "pointerdown");
  await flush();
  const start = calls("/talk/start")[0];
  fire(els["voice-call-end"], "click");
  start.d.resolve({ ok: true, json: () => Promise.resolve({ session_id: "sess-late" }) });
  await flush();
  assert(FakeWebSocket.instances.length === 0, "no WebSocket may open for a call that already ended");
  assert(calls("/talk/sessions/sess-late/stop").length === 1, "the late session must be released once");
});

test("End while the Answer microphone prompt is open: the granted mic is stopped, not kept", async () => {
  setup();
  fire(els["voice-call-answer"], "click");
  await flush();
  fire(els["voice-call-end"], "click");
  const late = makeStream("late-mic");
  gumCalls[0].resolve(late);
  await flush();
  assert(late._tracks[0].stopped, "a microphone granted after End must be stopped immediately");
  assert(!window.aacCallMicStream, "no call microphone may be kept after End");
});

test("End still ends the call on the phone when the server cannot be reached", async () => {
  setup({ autoFetch: false });
  fire(els["voice-call-end"], "click");
  calls("/end")[0].d.reject(new Error("offline"));
  await flush();
  assert(els["voice-call-end"].disabled && els["voice-call-ended"].hidden === false, "ended locally regardless");
  assert(toasts.some((t) => t.includes("could not be saved")), "the user is told it could not be saved");
});

test("A call that is already over opens with Answer, End and the mic disabled", async () => {
  setup({ initiallyOver: true });
  fire(els["voice-call-answer"], "click");
  fire(els["voice-call-end"], "click");
  await flush();
  assert(els["voice-call-answer"].disabled && els["voice-call-end"].disabled && mic.disabled, "controls disabled");
  assert(fetchCalls.length === 0, "no requests for a call that is already over");
});

test("Live View is unchanged: registering the stop hook does nothing on its own", async () => {
  setup();
  assert(Array.isArray(window.anyaicamTalkStops) && window.anyaicamTalkStops.length === 1, "one stop hook registered per mic");
  window.anyaicamTalkStops[0]();
  await flush();
  assert(fetchCalls.length === 0 && FakeWebSocket.instances.length === 0, "calling the hook while idle sends nothing");
});

let failed = 0;
for (const t of tests) {
  try { await t.fn(); console.log(`PASS ${t.name}`); }
  catch (e) { failed++; console.log(`FAIL ${t.name}: ${e.message}`); }
}
console.log(`${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
