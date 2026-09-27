// Executed behavioural test of window.aacoAttachVoice() -- the one voice
// input implementation shared by the floating AACO panel and the /aaco
// workspace (aaco_web._AACO_CLIENT_CORE_JS, extracted live by the pytest
// wrapper, never a hand copy). A fake SpeechRecognition lets each test
// drive results, errors and session ends in any order -- including the
// stop-then-quickly-start race a string check cannot exercise.
//
// Usage: node aaco_voice.test.mjs <path-to-extracted-core-js>

import { readFileSync } from "node:fs";

const source = readFileSync(process.argv[2], "utf8");

class FakeRecognition {
  constructor() {
    FakeRecognition.instances.push(this);
    this.started = false; this.stopped = false;
    this.onresult = null; this.onerror = null; this.onend = null;
  }
  start() { if (FakeRecognition.throwOnStart) throw new Error("InvalidStateError"); this.started = true; }
  stop() { this.stopped = true; }
  result(text, isFinal) {
    const alt = { transcript: text }; const r = [alt]; r.isFinal = isFinal;
    this.onresult && this.onresult({ results: [r] });
  }
  error(code) { this.onerror && this.onerror({ error: code }); }
  end() { this.onend && this.onend(); }
}
FakeRecognition.instances = [];
FakeRecognition.throwOnStart = false;

function makeElement() {
  const classes = new Set(); const attrs = {}; const listeners = {};
  return {
    disabled: true, title: "", textContent: "", value: "",
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    setAttribute: (k, v) => { attrs[k] = String(v); }, getAttribute: k => attrs[k],
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    fire: t => (listeners[t] || []).forEach(fn => fn({})),
  };
}

let env;
function setup({ supported = true, secure = true } = {}) {
  FakeRecognition.instances = []; FakeRecognition.throwOnStart = false;
  const windowListeners = {};
  const win = {
    isSecureContext: secure,
    addEventListener: (t, fn) => { (windowListeners[t] = windowListeners[t] || []).push(fn); },
  };
  if (supported) win.webkitSpeechRecognition = FakeRecognition;
  const setGlobal = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
  setGlobal("window", win);
  setGlobal("navigator", { language: "en-US" });
  setGlobal("fetch", () => { throw new Error("voice code must never call fetch itself"); });
  setGlobal("Event", class { constructor(t, o) { this.type = t; Object.assign(this, o || {}); } });
  new Function(source)();  // defines window.aacoSubmitCommand / window.aacoAttachVoice
  const form = makeElement(); form.submitted = [];
  const input = makeElement(); const status = makeElement(); const mic = makeElement();
  form.requestSubmit = () => form.submitted.push(input.value);
  win.aacoAttachVoice(form, input, status, mic);
  env = { win, form, input, status, mic, windowListeners, latest: () => FakeRecognition.instances.at(-1) };
  return env;
}
const listening = () => env.mic.getAttribute("aria-pressed") === "true" && env.mic.classList.contains("aaco-mic-listening");

const tests = [];
const test = (name, fn) => tests.push({ name, fn });
function assert(cond, msg) { if (!cond) throw new Error(msg); }

test("unsupported browser: mic stays disabled, typing unaffected", () => {
  const { mic } = setup({ supported: false });
  assert(mic.disabled === true, "mic must stay disabled without SpeechRecognition");
});

test("arbitrary spoken sentence goes into the same input and the same form submit", () => {
  const { mic, form, input, status } = setup();
  mic.fire("click");
  assert(listening(), "tapping the mic starts listening");
  const s = env.latest();
  s.result("who came to the front door after lunch", false);
  assert(status.textContent.includes("who came to the front door after lunch"), "interim transcript shown");
  assert(form.submitted.length === 0, "interim results are never submitted");
  s.result("who came to the front door after lunch yesterday", true);
  assert(form.submitted.length === 1 && form.submitted[0] === "who came to the front door after lunch yesterday", "final transcript submitted verbatim, once");
  assert(input.value === "who came to the front door after lunch yesterday", "transcript fills the typed-command box");
  assert(!listening(), "listening state cleared after the final result");
});

test("first silence retries once automatically, second silence explains", () => {
  const { mic, status } = setup();
  mic.fire("click");
  const first = env.latest();
  first.error("no-speech");
  assert(FakeRecognition.instances.length === 2, "a retry session is started");
  first.end();  // the first session's end must not drop the listening state
  assert(listening(), "still listening during the retry");
  env.latest().error("no-speech");
  assert(!listening(), "stops after the retry also hears nothing");
  assert(status.textContent.startsWith("No audio detected"), `message: ${status.textContent}`);
});

test("RACE: stop then quickly start -- the old session's late end does not kill the new one", () => {
  const { mic } = setup();
  mic.fire("click");
  const old = env.latest();
  mic.fire("click");          // user taps stop
  assert(old.stopped, "stop() called on the first session");
  mic.fire("click");          // ...and immediately starts again
  const fresh = env.latest();
  assert(fresh !== old && fresh.started, "a new session started");
  old.end();                  // late onend from the first session
  old.error("aborted");       // and a late aborted error
  assert(listening(), "the new session must still be listening");
  assert(!fresh.stopped, "the new session must not be stopped by the old session's events");
});

test("a late final result from a replaced session is not submitted", () => {
  const { mic, form } = setup();
  mic.fire("click"); const old = env.latest();
  mic.fire("click"); mic.fire("click");
  old.result("stale words", true);
  assert(form.submitted.length === 0, "stale session's transcript must be ignored");
});

test("tap to finish: a final result arriving after the user's stop is still sent", () => {
  const { mic, form } = setup();
  mic.fire("click"); const s = env.latest();
  mic.fire("click");  // user taps stop mid-sentence; the browser then delivers the final result
  s.result("show the front door camera", true);
  assert(form.submitted.length === 1 && form.submitted[0] === "show the front door camera", "finished sentence is submitted");
});

for (const [code, expected] of [
  ["not-allowed", "Microphone permission was denied. Type your command instead."],
  ["permission-denied", "Microphone permission was denied. Type your command instead."],
  ["service-not-allowed", "Speech recognition is blocked in this browser. Type your command instead."],
  ["audio-capture", "No microphone was found. Connect one, or type your command instead."],
  ["network", "Speech recognition needs an internet connection in this browser. Type your command instead."],
  ["language-not-supported", "Voice input is unavailable right now. Type your command instead."],
]) {
  test(`error ${code}: clear message, listening stopped, mic reusable`, () => {
    const { mic, status } = setup();
    mic.fire("click"); env.latest().error(code);
    assert(status.textContent === expected, `got: ${status.textContent}`);
    assert(!listening(), "listening state cleared");
    mic.fire("click");
    assert(listening() && FakeRecognition.instances.length === 2, "mic can be used again after an error");
  });
}

test("denied on a plain-HTTP page explains HTTPS/localhost", () => {
  const { mic, status } = setup({ secure: false });
  mic.fire("click"); env.latest().error("not-allowed");
  assert(status.textContent.includes("HTTPS") && status.textContent.includes("localhost"), `got: ${status.textContent}`);
});

test("start() throwing is reported and leaves the mic idle", () => {
  const { mic, status } = setup();
  FakeRecognition.throwOnStart = true;
  mic.fire("click");
  assert(status.textContent === "Voice input could not start. Type your command instead.", `got: ${status.textContent}`);
  assert(!listening(), "not left in the listening state");
});

test("leaving the page stops an active session", () => {
  const { mic, windowListeners } = setup();
  mic.fire("click"); const s = env.latest();
  (windowListeners.pagehide || []).forEach(fn => fn());
  assert(s.stopped && !listening(), "pagehide stops listening");
});

const failures = [];
for (const { name, fn } of tests) {
  try { fn(); console.log(`PASS  ${name}`); }
  catch (e) { failures.push(name); console.log(`FAIL  ${name}\n      ${e.message}`); }
}
console.log(`\n${tests.length - failures.length}/${tests.length} passed`);
process.exit(failures.length ? 1 : 0);
