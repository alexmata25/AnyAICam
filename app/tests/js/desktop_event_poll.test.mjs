// Real, executed behavioral test of the desktop Events page's
// scheduleEventPoll()/reconcileDesktopEvent()/settleOverdueDesktopRows()
// reconciliation loop (P0 #5 remediation, rounds 2-5 -- round 3,
// 2026-09-05, fixed a real P0 blocker ChatGPT Work's own staging QA
// found: round 2's scheduleEventPoll() permanently stopped for good
// the moment nothing was processing, so a page loaded with zero
// pending rows could never discover an event created afterward without
// a manual reload. Round 3 added a slow discovery cadence
// (EVENT_SLOW_POLL_INTERVAL_MS) that never permanently stops, switching
// to the existing fast cadence the moment something is actually
// processing. Round 4, 2026-09-05 (Codex final review on round 3):
// round 3's "only one timer/fetch in flight" guard checked only
// eventPollState.timer, which is nulled out at the very top of the
// timer callback -- before the fetch itself starts -- so a second
// scheduleEventPoll() call landing during an in-flight request could
// still schedule a second, independent timer/fetch pair. Fixed with an
// explicit inFlight flag. This also rewrites several tests that
// previously drove behavior by calling internal helpers
// (reconcileDesktopEvent()) directly instead of through the real
// scheduleEventPoll() fetch callback, and fixes the fake timer queue
// itself to fire in chronological (fireAt) order rather than insertion
// order -- required for a test to be able to expose a timer that was
// wrongly scheduled during an active fetch at all: a stray short-delay
// timer inserted after a long-delay one would previously never get a
// chance to fire before it, silently hiding exactly the race this
// suite exists to catch. Round 5, 2026-09-05 (staging QA root-cause
// trace): Array.prototype.forEach() does not catch a callback's own
// exception -- it propagates immediately and skips every remaining,
// not-yet-visited element. Since a poll response batch is reversed
// (oldest-of-this-batch first) before iterating, a single bad EXISTING
// event anywhere in that batch could silently block every genuinely
// NEW event ordered after it from ever being reconciled -- forever, on
// every subsequent poll, since the outer catch swallowed it identically
// to an ordinary network failure, with nothing logged anywhere. Round 5
// wraps each event's own reconciliation in its own try/catch (so one
// bad event only skips itself, logging its id and the error) and splits
// the outer catch so AbortError/network failures stay quiet exactly as
// before, but any other, truly unexpected exception is now surfaced via
// console.error instead of silently swallowed.
//
// String-content assertions on the rendered HTML can prove the
// constants and markup exist, but they cannot exercise the actual
// async retry/timeout/deadline/DOM-mutation behavior these bugs
// depended on -- only running the real code with controllable
// fetch/AbortController/timers/DOM can (same rationale as this
// suite's siblings, mobile_event_poll.test.mjs and
// talk_mic_lifecycle.test.mjs).
//
// This file runs the ACTUAL extracted source (two file paths in
// argv[2..3]: the shared prelude defining rows/apply/wireEventThumbPlayer,
// then the polling functions themselves -- both always extracted live
// from a real _render_customer_events() render by the pytest wrapper
// that invokes this, never a hand-copied duplicate that could drift
// from what ships) under a small hand-rolled DOM implementation
// (deliberately not a general CSS engine or a new project dependency
// such as jsdom -- this codebase's own established testing philosophy
// is dependency-light; only the exact selector shapes this code
// actually uses are supported: #id, tag, .class, [attr], [attr="value"],
// the descendant combinator, and :checked).
//
// Usage: node desktop_event_poll.test.mjs <prelude.js> <poll_snippet.js>
// Exit code 0 on all-pass, 1 on any failure (with details on stdout).

import { readFileSync } from "node:fs";
new Function(readFileSync(new URL('../../static/event_media.js',import.meta.url),'utf8'))();
import { setImmediate as realSetImmediate } from "node:timers";

const preludePath = process.argv[2];
const snippetPath = process.argv[3];
if (!preludePath || !snippetPath) {
  console.error("usage: node desktop_event_poll.test.mjs <prelude.js> <poll_snippet.js>");
  process.exit(2);
}
// Both files are extracted from within the SAME real (function(){...})();
// IIFE in the rendered page -- the prelude begins with its opening
// "(function(){" and the snippet ends with its closing "})();" (plus
// the real code's own auto-start `scheduleEventPoll();` call just
// before that). Stripped here so the combined source below is flat,
// top-level code inside `new Function(...)`'s own body instead of
// being sealed inside a closure the trailing `return {...}` statement
// couldn't see into -- and so this suite's own explicit
// mod.scheduleEventPoll() calls are the only thing that ever starts a
// poll cycle, never an auto-start racing against test setup.
const preludeSource = readFileSync(preludePath, "utf8").replace(/^\(function\(\)\{\s*/, "");
const snippetSource = readFileSync(snippetPath, "utf8").replace(/\s*scheduleEventPoll\(\);\s*\}\)\(\);\s*$/, "");

// ---------------------------------------------------------------- fake clock

let fakeNow = Date.now();
Date.now = () => fakeNow;

let timerIdSeq = 1;
let pendingTimers = new Map(); // id -> { fn, ms, fireAt }
function fakeSetTimeout(fn, ms) {
  const id = timerIdSeq++;
  const delay = ms || 0;
  pendingTimers.set(id, { fn, ms: delay, fireAt: fakeNow + delay });
  return id;
}
function fakeClearTimeout(id) {
  pendingTimers.delete(id);
}
globalThis.setTimeout = fakeSetTimeout;
globalThis.clearTimeout = fakeClearTimeout;

async function settle() {
  await new Promise((resolve) => realSetImmediate(resolve));
}
// Round 4 fix: fires whichever pending timer has the earliest fireAt
// (the absolute instant it was scheduled to go off), not whichever was
// inserted into the Map first. A real browser's timer queue is
// chronological, not insertion-ordered -- a wrongly-scheduled 4000ms
// timer inserted AFTER an already-pending 8000ms one must still fire
// first, exactly as it would for real. Ties (identical fireAt) fall
// back to insertion order (ascending id) purely for determinism.
async function flushOneTimer() {
  const entries = [...pendingTimers.entries()];
  if (entries.length === 0) return false;
  entries.sort((a, b) => a[1].fireAt - b[1].fireAt || a[0] - b[0]);
  const [id, { fn, fireAt }] = entries[0];
  pendingTimers.delete(id);
  fakeNow = Math.max(fakeNow, fireAt);
  const result = fn();
  if (result && typeof result.catch === "function") result.catch(() => {});
  await settle();
  return true;
}
async function flushAllTimers(max = 500) {
  let n = 0;
  while (await flushOneTimer()) {
    n++;
    if (n > max) throw new Error("too many timer flushes -- possible infinite loop");
  }
}

// ---------------------------------------------------------------- minimal fake DOM

class FakeElement {
  contains(other) { for(let e=other;e;e=e.parentNode)if(e===this)return true;return false; }
  constructor(tagName) {
    this.tagName = String(tagName || "DIV").toUpperCase();
    this._attrs = new Map();
    this.children = [];
    this.parentNode = null;
    this._innerHTML = "";
    this._listeners = {};
    this.hidden = false;
    this.checked = false;
    const self = this;
    this.dataset = new Proxy(
      {},
      {
        set(target, prop, value) {
          target[prop] = String(value);
          const attrName = "data-" + String(prop).replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
          self._attrs.set(attrName, String(value));
          return true;
        },
        get(target, prop) {
          return target[prop];
        },
      }
    );
  }
  setAttribute(name, value) {
    this._attrs.set(name, String(value));
    if (name.startsWith("data-")) {
      const camel = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[camel] = String(value);
    }
  }
  getAttribute(name) {
    return this._attrs.has(name) ? this._attrs.get(name) : null;
  }
  get id() {
    return this._attrs.get("id") || "";
  }
  set id(value) {
    this._attrs.set("id", String(value));
  }
  get classList() {
    const self = this;
    const classes = () => (self._attrs.get("class") || "").split(/\s+/).filter(Boolean);
    return {
      add: (c) => self._attrs.set("class", [...new Set([...classes(), c])].join(" ")),
      remove: (c) => self._attrs.set("class", classes().filter((x) => x !== c).join(" ")),
      contains: (c) => classes().includes(c),
    };
  }
  get innerHTML() {
    return this._innerHTML;
  }
  set innerHTML(value) {
    this._innerHTML = value;
    // This test suite never needs real HTML->element parsing for the
    // cells it inspects (.event-thumbnail-cell/.event-action-cell
    // content is asserted as a raw string, exactly like the sibling
    // Python content-assertion suite already covers markup shape) --
    // only reconcileDesktopEvent()'s own dataset/attribute mutations
    // and DOM structure (rows, insertion order, media-state) need to
    // be real.
    this.children = [];
  }
  get textContent() {
    return this._innerHTML.replace(/<[^>]*>/g, "");
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  insertBefore(newNode, referenceNode) {
    newNode.parentNode = this;
    const idx = referenceNode ? this.children.indexOf(referenceNode) : -1;
    if (idx === -1) this.children.push(newNode);
    else this.children.splice(idx, 0, newNode);
    return newNode;
  }
  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx !== -1) this.children.splice(idx, 1);
    child.parentNode = null;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  get firstChild() {
    return this.children[0] || null;
  }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (matches(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
  querySelectorAll(selector) {
    return queryAll(this, selector);
  }
  querySelector(selector) {
    return queryAll(this, selector)[0] || null;
  }
}

// ---- tiny selector engine: #id, tag, .class, [attr], [attr="value"], ":checked", descendant combinator ----

function parseCompound(compound) {
  const conditions = [];
  const re = /(#[\w-]+)|(\.[\w-]+)|(\[[^\]]+\])|(:checked)|([a-zA-Z][\w-]*)/g;
  let m;
  while ((m = re.exec(compound))) {
    // Bind fresh locals each iteration -- `m` itself is reassigned by
    // the loop, so a closure capturing `m` directly would see
    // whatever `m` holds when it's later INVOKED (null, once the loop
    // has exited), not the match that was current when the closure
    // was created.
    const idSel = m[1], classSel = m[2], attrSel = m[3], checkedSel = m[4], tagSel = m[5];
    if (idSel) conditions.push((el) => el.id === idSel.slice(1));
    else if (classSel) conditions.push((el) => el.classList.contains(classSel.slice(1)));
    else if (attrSel) {
      const inner = attrSel.slice(1, -1);
      const eq = inner.match(/^([\w-]+)="([^"]*)"$/);
      if (eq) {
        const [, attrName, attrValue] = eq;
        conditions.push((el) => el.getAttribute(attrName) === attrValue);
      } else {
        conditions.push((el) => el.getAttribute(inner) !== null);
      }
    } else if (checkedSel) conditions.push((el) => el.checked === true);
    else if (tagSel) conditions.push((el) => el.tagName === tagSel.toUpperCase());
  }
  return (el) => conditions.every((cond) => cond(el));
}

function matches(el, selector) {
  const compounds = selector.trim().split(/\s+/);
  return parseCompound(compounds[compounds.length - 1])(el);
}

function flatten(el, acc) {
  acc.push(el);
  el.children.forEach((c) => flatten(c, acc));
  return acc;
}

function queryAll(root, selector) {
  const compounds = selector.trim().split(/\s+/).map(parseCompound);
  const all = flatten(root, []).filter((el) => el !== root || root === globalDocumentRoot);
  // Descendant-combinator matching: an element matches if it satisfies
  // the LAST compound and has some ancestor chain satisfying the
  // preceding compounds in order. Good enough for this code's own
  // fixed selector shapes (max two or three compounds, always simple
  // descendant relationships, never siblings/children combinators).
  return all.filter((el) => {
    if (!compounds[compounds.length - 1](el)) return false;
    let idx = compounds.length - 2;
    let node = el.parentNode;
    while (idx >= 0 && node) {
      if (compounds[idx](node)) idx--;
      node = node.parentNode;
    }
    return idx < 0;
  });
}

const globalDocumentRoot = new FakeElement("DOCUMENT");
const idRegistry = new Map();

globalThis.document = {
  getElementById: (id) => idRegistry.get(id) || null,
  querySelectorAll: (selector) => queryAll(globalDocumentRoot, selector),
  querySelector: (selector) => queryAll(globalDocumentRoot, selector)[0] || null,
  createElement: (tag) => new FakeElement(tag),
  addEventListener: () => {},
};
// Round 5: CSS.escape() is a genuine call reconcileDesktopEvent() makes
// for EVERY event (new or already-known) via its own tr[data-event-id=...]
// lookup -- a real, controllable dependency point, not a hand-inserted
// hook, so a sentinel id can legitimately force a single event's own
// reconciliation to throw without touching the real extracted source at
// all. Any other id behaves exactly as CSS.escape actually does.
const THROW_ON_EVENT_ID = "__p05_round5_throw_sentinel__";
globalThis.CSS = {
  escape: (s) => {
    if (s === THROW_ON_EVENT_ID) throw new Error("simulated reconciliation failure for " + s);
    return String(s).replace(/["\\]/g, "\\$&");
  },
};

// Round 5: captures console.error calls instead of letting them print,
// so tests can assert exactly what (and how many times) the real code
// logged -- both the per-event id+error (item requirement) and the
// outer unexpected-exception case, while still distinguishing "nothing
// was logged" (the expected/quiet AbortError and network-failure paths)
// from a real assertion failure.
let capturedConsoleErrors = [];
const realConsoleError = console.error.bind(console);
console.error = (...args) => {
  capturedConsoleErrors.push(args);
};

function makeNamedElement(tag, id) {
  const el = new FakeElement(tag);
  if (id) {
    el.id = id;
    idRegistry.set(id, el);
  }
  return el;
}

// ---------------------------------------------------------------- test fixture DOM

function buildFixture() {
  idRegistry.clear();
  globalDocumentRoot.children = [];

  const search = makeNamedElement("input", "events-search");
  search.value = "";
  const filters = makeNamedElement("div", "events-camera-filters");
  const table = makeNamedElement("table", "events-table");
  const tbody = new FakeElement("tbody");
  table.appendChild(tbody);

  globalDocumentRoot.appendChild(search);
  globalDocumentRoot.appendChild(filters);
  globalDocumentRoot.appendChild(table);

  const countPill1 = new FakeElement("span");
  countPill1.setAttribute("class", "pill event-count-pill");
  countPill1.dataset.count = "1";
  countPill1._innerHTML = "1 event(s)";
  const countPill2 = new FakeElement("span");
  countPill2.setAttribute("class", "health-detail event-count-pill");
  countPill2.dataset.count = "1";
  countPill2._innerHTML = "1 event(s)";
  globalDocumentRoot.appendChild(countPill1);
  globalDocumentRoot.appendChild(countPill2);

  return { tbody, countPill1, countPill2 };
}

function addRow(tbody, { id, camera = "1", type = "person", timestamp, hasClip = false, mediaState }) {
  const row = new FakeElement("tr");
  row.dataset.eventCamera = camera;
  row.dataset.eventType = type;
  row.dataset.eventId = id;
  row.dataset.eventTimestamp = timestamp;
  row.dataset.eventHasClip = hasClip ? "1" : "0";
  row.dataset.mediaState = mediaState;
  const thumbCell = new FakeElement("td");
  thumbCell.setAttribute("class", "event-thumbnail-cell");
  thumbCell._innerHTML = mediaState === "processing" ? '<span class="event-thumb-pending">Processing…</span>' : "—";
  const actionCell = new FakeElement("td");
  actionCell.setAttribute("class", "event-action-cell");
  actionCell._innerHTML =
    mediaState === "processing"
      ? '<span class="download event-action-pending" aria-disabled="true" title="x">Playback</span>'
      : '<a class="download" href="/playback">Playback</a>';
  row.appendChild(thumbCell);
  row.appendChild(actionCell);
  tbody.appendChild(row);
  return row;
}

function isoAgo(ms) {
  return new Date(Date.now() - ms).toISOString();
}

// ---------------------------------------------------------------- fetch fakes

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

function resetFakes() {
  fetchQueue = [];
  fetchCalls = [];
  pendingTimers = new Map();
  capturedConsoleErrors = [];
}

// ---------------------------------------------------------------- load real source

const factory = new Function(
  `${preludeSource}\n${snippetSource}\nreturn { reconcileDesktopEvent, settleOverdueDesktopRows, scheduleEventPoll, mediaStateFor, isEventPending, anyDesktopRowStillProcessing };`
);

// Round 3: inspects the single currently-pending timer's own delay --
// EVENT_FAST_POLL_INTERVAL_MS (4000) while something is processing,
// EVENT_SLOW_POLL_INTERVAL_MS (15000) while settled/idle -- to assert
// which cadence scheduleEventPoll() actually chose, without needing to
// export the constants themselves.
function pendingTimerDelay() {
  const entries = [...pendingTimers.values()];
  if (entries.length !== 1) throw new Error(`expected exactly 1 pending timer, found ${entries.length}`);
  return entries[0].ms;
}

function pillCounts(countPill1, countPill2) {
  return [parseInt(countPill1.dataset.count, 10), parseInt(countPill2.dataset.count, 10)];
}

function freshEvent(id, { hasClip = false, ageMs = 0, camera = "cam-1", confidence = 0.9 } = {}) {
  return {
    id,
    camera: 1,
    camera_id: camera,
    camera_name: "Front Door",
    event_type: "person",
    timestamp: isoAgo(ageMs),
    confidence,
    thumbnail: hasClip ? "/thumb.jpg" : null,
    has_event_clip: hasClip,
  };
}

// ---------------------------------------------------------------- tests

const tests = [];
function test(name, fn) {
  tests.push({ name, fn });
}

test("transient fetch exception is retried, not a permanent stop", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  addRow(tbody, { id: "ev-1", timestamp: isoAgo(5000), mediaState: "processing" });
  const mod = factory();
  mod.scheduleEventPoll();
  fetchQueue.push(() => {
    throw new TypeError("network down");
  });
  await flushOneTimer();
  if (fetchCalls.length !== 1) throw new Error("expected 1 fetch call");
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-1", { hasClip: true, ageMs: 5000 })] }));
  const flushed = await flushOneTimer();
  if (!flushed) throw new Error("expected a retry to be scheduled after the exception");
  if (fetchCalls.length !== 2) throw new Error("expected a second fetch call");
  const row = tbody.querySelector('tr[data-event-id="ev-1"]');
  if (row.dataset.mediaState !== "ready") throw new Error("expected the row to become ready after the retry succeeded");
});

test("HTTP 500 is retried, not a permanent stop", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  addRow(tbody, { id: "ev-2", timestamp: isoAgo(5000), mediaState: "processing" });
  const mod = factory();
  mod.scheduleEventPoll();
  fetchQueue.push(jsonResponse(500, {}));
  await flushOneTimer();
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-2", { hasClip: true, ageMs: 5000 })] }));
  const flushed = await flushOneTimer();
  if (!flushed) throw new Error("expected a retry after the 500");
  const row = tbody.querySelector('tr[data-event-id="ev-2"]');
  if (row.dataset.mediaState !== "ready") throw new Error("expected the row to become ready after recovering from 500");
});

test("a never-resolving fetch is bounded by its own timeout and retried", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  addRow(tbody, { id: "ev-3", timestamp: isoAgo(5000), mediaState: "processing" });
  const mod = factory();
  mod.scheduleEventPoll();
  fetchQueue.push(neverResolves());
  await flushOneTimer(); // starts the hung fetch
  if (fetchCalls.length !== 1) throw new Error("expected the fetch to have started");
  const row = tbody.querySelector('tr[data-event-id="ev-3"]');
  if (row.dataset.mediaState !== "processing") throw new Error("must not change state while the fetch is still hung");
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-3", { hasClip: true, ageMs: 5000 })] }));
  const flushedTimeout = await flushOneTimer(); // the fetch's own internal timeout fires, aborting it
  if (!flushedTimeout) throw new Error("expected the fetch's own timeout to be queued");
  const flushedRetry = await flushOneTimer(); // finally reschedules
  if (!flushedRetry) throw new Error("expected a retry after the timeout-abort");
  if (row.dataset.mediaState !== "ready") throw new Error("expected the row to become ready after the retry succeeded");
});

test("real elapsed-time deadline settles rows regardless of attempt count", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  addRow(tbody, { id: "ev-4", timestamp: isoAgo(0), mediaState: "processing" });
  const mod = factory();
  mod.scheduleEventPoll();
  // 5 attempts that each hang and get timeout-aborted: 5 * (4000
  // interval + 8000 timeout) = 60000ms of real elapsed time, for only
  // 5 attempts -- proving the deadline is driven by elapsed time, not
  // an attempt counter (which round 1 would have required exactly 30
  // attempts to reach regardless of how long each one took).
  for (let i = 0; i < 5; i++) {
    fetchQueue.push(neverResolves());
    await flushOneTimer(); // starts the fetch
    await flushOneTimer(); // its own timeout fires
  }
  // Continue with quick still-pending responses until elapsed time
  // crosses the 120s deadline -- 60000ms already elapsed, 15 more
  // attempts at 4000ms each reaches 120000ms, for 20 total attempts,
  // well under the old 30-attempt ceiling.
  for (let i = 0; i < 15; i++) {
    fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-4", { hasClip: false, ageMs: 0 })] }));
    await flushOneTimer();
  }
  const row = tbody.querySelector('tr[data-event-id="ev-4"]');
  if (row.dataset.mediaState !== "unavailable") {
    throw new Error(`expected the row to be settled once real elapsed time reached the deadline (attempts used: 20, got state=${row.dataset.mediaState})`);
  }
});

test("a final failed request exactly at the deadline still settles, never leaves Processing… indefinitely", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  addRow(tbody, { id: "ev-5", timestamp: isoAgo(0), mediaState: "processing" });
  const mod = factory();
  mod.scheduleEventPoll();
  // 29 quick pending attempts (116000ms elapsed), then one final
  // attempt that fails right as the deadline is crossed.
  for (let i = 0; i < 29; i++) {
    fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-5", { hasClip: false, ageMs: 0 })] }));
    await flushOneTimer();
  }
  fetchQueue.push(() => {
    throw new TypeError("final failure at the deadline");
  });
  await flushOneTimer(); // this attempt fails
  await flushOneTimer(); // finally reschedules, but the deadline check now settles instead
  const row = tbody.querySelector('tr[data-event-id="ev-5"]');
  if (row.dataset.mediaState !== "unavailable") {
    throw new Error("a final failure right at the deadline must still settle the row, never leave it Processing… forever");
  }
});

test("a ready event is reconciled in place, ordering/count issues aside", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  addRow(tbody, { id: "ev-6", timestamp: isoAgo(5000), mediaState: "processing" });
  const mod = factory();
  mod.scheduleEventPoll();
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-6", { hasClip: true, ageMs: 5000 })] }));
  await flushOneTimer();
  const row = tbody.querySelector('tr[data-event-id="ev-6"]');
  if (row.dataset.mediaState !== "ready") throw new Error("expected ev-6 to reconcile to ready");
  if (tbody.children.length !== 1) throw new Error("must not create a duplicate row for an existing event id");
});

// Round 4 (Codex final review, item 3): previously this test drove
// ordering by calling mod.reconcileDesktopEvent() and tbody.insertBefore()
// directly, bypassing scheduleEventPoll()'s own fetch-success handler --
// the actual code responsible for iterating a DESC batch and inserting
// each row in the right place. Rewritten to go through the real
// callback: one poll response returns three brand-new events in the
// same newest-first order the real endpoint uses, and the assertion is
// on the DOM order that callback itself produced.
test("multiple new events in one poll response preserve newest-first order, via the real poll callback", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  tbody.children = []; // start with zero existing rows -- every event below is genuinely new
  const mod = factory();
  mod.scheduleEventPoll();
  const oldest = freshEvent("ev-old", { hasClip: true, ageMs: 30000 });
  const middle = freshEvent("ev-mid", { hasClip: true, ageMs: 20000 });
  const newest = freshEvent("ev-new", { hasClip: true, ageMs: 10000 });
  // The real endpoint returns DESC (newest first) -- that's the exact
  // shape scheduleEventPoll()'s own fetch handler is written to expect.
  fetchQueue.push(jsonResponse(200, { events: [newest, middle, oldest] }));
  await flushOneTimer();
  const ids = tbody.children.map((r) => r.dataset.eventId);
  if (ids.join(",") !== "ev-new,ev-mid,ev-old") {
    throw new Error(`expected newest-first order ev-new,ev-mid,ev-old but got ${ids.join(",")}`);
  }
});

// Round 4 (Codex final review, item 4): previously this test only
// called mod.reconcileDesktopEvent() once against a row that was never
// actually inserted by a real poll. Rewritten to drive two genuine,
// sequential polls through scheduleEventPoll(): the first discovers
// event A, the second returns A again (already-known, must update in
// place) plus a genuinely new event B -- the DOM must end up with
// exactly one row for each.
test("duplicate event IDs across real polls never create a second row", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  tbody.children = [];
  const mod = factory();
  mod.scheduleEventPoll();
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-7a", { hasClip: false, ageMs: 0 })] }));
  await flushOneTimer(); // first poll discovers ev-7a as Processing
  if (tbody.children.length !== 1) throw new Error("expected exactly 1 row after the first poll");
  fetchQueue.push(
    jsonResponse(200, {
      events: [freshEvent("ev-7b", { hasClip: true, ageMs: 1000 }), freshEvent("ev-7a", { hasClip: true, ageMs: 4000 })],
    })
  );
  await flushOneTimer(); // second poll: ev-7a again (now ready) + genuinely new ev-7b
  const ids = tbody.children.map((r) => r.dataset.eventId);
  if (ids.filter((id) => id === "ev-7a").length !== 1) throw new Error(`expected exactly one ev-7a row, got ids=${ids.join(",")}`);
  if (ids.filter((id) => id === "ev-7b").length !== 1) throw new Error(`expected exactly one ev-7b row, got ids=${ids.join(",")}`);
  if (tbody.children.length !== 2) throw new Error(`expected exactly 2 rows total, got ${tbody.children.length}`);
  const rowA = tbody.querySelector('tr[data-event-id="ev-7a"]');
  if (rowA.dataset.mediaState !== "ready") throw new Error("expected ev-7a to have updated in place to ready, not been replaced");
});

// Round 4 (Codex final review, item 5): visible counts asserted through
// the real polling callback -- must increment exactly once per unique
// newly-inserted event, and never for a duplicate/already-known id
// reappearing in a later poll response.
test("visible event counts increment once per unique new event and never for duplicates, via the real poll callback", async () => {
  resetFakes();
  const { tbody, countPill1, countPill2 } = buildFixture();
  tbody.children = [];
  countPill1.dataset.count = "0";
  countPill1._innerHTML = "0 event(s)";
  countPill2.dataset.count = "0";
  countPill2._innerHTML = "0 event(s)";
  const mod = factory();
  mod.scheduleEventPoll();
  fetchQueue.push(
    jsonResponse(200, {
      events: [freshEvent("ev-c2", { hasClip: true, ageMs: 1000 }), freshEvent("ev-c1", { hasClip: true, ageMs: 2000 })],
    })
  );
  await flushOneTimer(); // 2 genuinely new events
  let [c1, c2] = pillCounts(countPill1, countPill2);
  if (c1 !== 2 || c2 !== 2) throw new Error(`expected both count pills at 2 after 2 new events, got ${c1}/${c2}`);
  fetchQueue.push(
    jsonResponse(200, {
      events: [freshEvent("ev-c3", { hasClip: true, ageMs: 500 }), freshEvent("ev-c1", { hasClip: true, ageMs: 2000 }), freshEvent("ev-c2", { hasClip: true, ageMs: 1000 })],
    })
  );
  await flushOneTimer(); // 1 genuinely new event (ev-c3) + 2 duplicates (ev-c1, ev-c2) reconciled in place
  [c1, c2] = pillCounts(countPill1, countPill2);
  if (c1 !== 3 || c2 !== 3) {
    throw new Error(`expected both count pills at 3 after 1 more unique event (duplicates must not increment), got ${c1}/${c2}`);
  }
  if (tbody.children.length !== 3) throw new Error(`expected exactly 3 rows total, got ${tbody.children.length}`);
});

// Round 4 (Codex final review, item 6): the actual no-overlap
// invariant, driven behaviorally through the real callback rather than
// only inspecting internal state. A fetch is left genuinely hanging;
// scheduleEventPoll() is called again while it's still in flight (the
// exact window round 3's state.timer-only guard was blind to, since
// state.timer is nulled before the fetch starts); then the fake clock
// is advanced using chronological (fireAt-order) flushing far enough
// that any wrongly-scheduled second cadence timer would already have
// fired and started a second fetch, before the original request is
// ever resolved or timed out.
test("no-overlap: scheduleEventPoll() called again mid-flight never starts a second fetch, even once a wrongly-scheduled timer's own delay would have elapsed", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  addRow(tbody, { id: "ev-inflight", timestamp: isoAgo(5000), mediaState: "processing" }); // fast (4000ms) cadence
  const mod = factory();
  mod.scheduleEventPoll();
  if (pendingTimerDelay() !== 4000) throw new Error("expected the fast cadence while a row is processing");
  fetchQueue.push(neverResolves());
  await flushOneTimer(); // fires the cadence timer; the fetch starts and hangs (state.timer is now null, inFlight true)
  if (fetchCalls.length !== 1) throw new Error("expected exactly 1 fetch to have started");
  // The exact window round 3 was blind to: call scheduleEventPoll()
  // again while the fetch above is still unresolved.
  mod.scheduleEventPoll();
  mod.scheduleEventPoll();
  if (pendingTimers.size !== 1) {
    throw new Error(
      `expected exactly 1 pending timer (the fetch's own 8000ms timeout) while a request is in flight, found ${pendingTimers.size} -- a second scheduleEventPoll() call scheduled an extra timer during an active fetch`
    );
  }
  // Advance the clock in chronological order. If the bug were present,
  // a wrongly-scheduled 4000ms cadence timer would fire here, BEFORE
  // the fetch's own 8000ms timeout, and start a second concurrent
  // fetch.
  await flushOneTimer(); // the fetch's own 8000ms timeout aborts it
  if (fetchCalls.length !== 1) {
    throw new Error("a second fetch was started before the original in-flight request was ever resolved or timed out -- no-overlap invariant violated");
  }
  // The original request has now genuinely finished (timed out).
  // Normal scheduling must resume, exactly once.
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-inflight", { hasClip: true, ageMs: 5000 })] }));
  const resumed = await flushOneTimer();
  if (!resumed) throw new Error("expected normal scheduling to resume after the in-flight request finally settled");
  if (fetchCalls.length !== 2) throw new Error(`expected exactly one retry fetch after the timeout, got ${fetchCalls.length} total calls`);
  const row = tbody.querySelector('tr[data-event-id="ev-inflight"]');
  if (row.dataset.mediaState !== "ready") throw new Error("expected the row to reconcile normally once polling resumed");
});

// ------------------------------------------------- round 5: per-event fault isolation (staging QA root-cause trace)

// The core fix: Array.forEach() aborts on a callback's first exception
// and never visits the remaining elements. A response batch is reversed
// (oldest-of-this-batch first) before iterating, so a bad EXISTING event
// ordered before the genuinely NEW ones used to be able to silently
// block every new event from ever appearing -- exactly the symptom
// staging QA observed. This drives a single poll response containing
// five events (in real DESC/newest-first order, exactly as the API
// returns): the newest and 2nd-newest are brand new, a duplicate of an
// already-known row sits after that, the sentinel event that fails
// reconciliation sits in the middle, and the oldest is also brand new --
// so after reversal (oldest-first iteration), the failing event has
// events both before AND after it, and two of the ones after it are
// genuinely new insertions that must not be lost.
test("one bad event mid-batch is skipped and logged; events before and after it still insert, order/duplicates/count intact", async () => {
  resetFakes();
  const { tbody, countPill1, countPill2 } = buildFixture();
  tbody.children = [];
  addRow(tbody, { id: "ev-r5-existing", camera: "1", timestamp: isoAgo(60000), hasClip: true, mediaState: "ready" });
  countPill1.dataset.count = "1";
  countPill1._innerHTML = "1 event(s)";
  countPill2.dataset.count = "1";
  countPill2._innerHTML = "1 event(s)";
  const mod = factory();
  mod.scheduleEventPoll();

  // Real API order: DESC, newest first.
  const newest = freshEvent("ev-r5-newest", { hasClip: true, ageMs: 1000 });
  const secondNewest = freshEvent("ev-r5-second", { hasClip: true, ageMs: 2000 });
  const duplicate = freshEvent("ev-r5-existing", { hasClip: true, ageMs: 60000 }); // already known -- update in place
  const failing = freshEvent(THROW_ON_EVENT_ID, { hasClip: false, ageMs: 3000 });
  const oldest = freshEvent("ev-r5-oldest", { hasClip: true, ageMs: 5000 });
  fetchQueue.push(jsonResponse(200, { events: [newest, secondNewest, duplicate, failing, oldest] }));
  await flushOneTimer();

  if (fetchCalls.length !== 1) throw new Error(`expected exactly 1 fetch call, got ${fetchCalls.length}`);

  // The failing sentinel must never produce a row of its own.
  if (tbody.querySelector(`tr[data-event-id="${THROW_ON_EVENT_ID}"]`)) {
    throw new Error("the event whose reconciliation threw must never end up with a row in the DOM");
  }

  // Every OTHER event -- including the two ordered after the failure in
  // iteration order (ev-r5-second, ev-r5-newest) -- must still be
  // inserted or updated. This is the actual regression this test exists
  // to prove: before round 5, ev-r5-second and ev-r5-newest would have
  // been silently dropped because the forEach aborted at `failing`.
  const ids = tbody.children.map((r) => r.dataset.eventId);
  // ev-r5-oldest is inserted first (right before the pre-existing row,
  // which itself never moves -- it's reconciled in place, not
  // re-inserted), then ev-r5-second and ev-r5-newest each get inserted
  // ahead of it in turn -- newest-first overall, failing sentinel
  // excluded entirely, and the pre-existing row stays last since
  // nothing new is ever ordered older than it in this batch.
  if (ids.join(",") !== "ev-r5-newest,ev-r5-second,ev-r5-oldest,ev-r5-existing") {
    throw new Error(`expected newest-first order ev-r5-newest,ev-r5-second,ev-r5-oldest,ev-r5-existing (failing sentinel excluded), got ${ids.join(",")}`);
  }
  if (tbody.children.length !== 4) throw new Error(`expected exactly 4 rows (5 events minus the 1 that failed), got ${tbody.children.length}`);

  // Duplicate handling: ev-r5-existing updated in place, never a second row.
  if (ids.filter((id) => id === "ev-r5-existing").length !== 1) throw new Error("expected exactly one ev-r5-existing row, updated in place");
  const existingRow = tbody.querySelector('tr[data-event-id="ev-r5-existing"]');
  if (existingRow.dataset.mediaState !== "ready") throw new Error("expected the duplicate to have reconciled to ready in place");

  // Count: 3 genuinely new rows inserted (newest, second-newest, oldest)
  // -- the duplicate must not increment, and the failed sentinel must
  // not increment either.
  const [c1, c2] = pillCounts(countPill1, countPill2);
  if (c1 !== 4 || c2 !== 4) throw new Error(`expected both count pills at 4 (1 pre-existing + 3 new, duplicate and failure excluded), got ${c1}/${c2}`);

  // The failing event's id and error must be logged, and nothing else
  // must have logged an error for this otherwise-successful poll.
  if (capturedConsoleErrors.length !== 1) {
    throw new Error(`expected exactly 1 console.error call (for the one failing event), got ${capturedConsoleErrors.length}`);
  }
  const [message, loggedId, loggedError] = capturedConsoleErrors[0];
  if (loggedId !== THROW_ON_EVENT_ID) throw new Error(`expected the failing event's own id to be logged, got ${loggedId}`);
  if (!(loggedError instanceof Error)) throw new Error("expected the actual error object to be logged alongside the event id");
  if (typeof message !== "string" || !message.length) throw new Error("expected a descriptive message logged alongside the event id and error");
});

// Round 5: the outer catch must distinguish "expected" failures
// (AbortError from the fetch's own timeout, and a network-level
// TypeError -- the same signature real browsers use for "Failed to
// fetch") from anything else. Both expected cases must remain quiet
// (no console.error) and must still retry/recover normally -- unchanged
// from round 4's behavior, just now explicitly asserted here rather
// than only inferred from the row settling correctly.
test("AbortError and network-failure TypeErrors both stay quiet (no console.error) and the loop still recovers normally", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  addRow(tbody, { id: "ev-r5-quiet", timestamp: isoAgo(5000), mediaState: "processing" });
  const mod = factory();
  mod.scheduleEventPoll();

  // Network-level TypeError.
  fetchQueue.push(() => {
    throw new TypeError("network down");
  });
  await flushOneTimer();
  if (capturedConsoleErrors.length !== 0) throw new Error("a network TypeError must not be logged -- it's an expected, quiet retry path");

  // AbortError via the fetch's own EVENT_FETCH_TIMEOUT_MS timeout.
  fetchQueue.push(neverResolves());
  await flushOneTimer(); // starts the hung fetch
  await flushOneTimer(); // its own internal timeout fires, aborting it
  if (capturedConsoleErrors.length !== 0) throw new Error("an AbortError from the fetch's own timeout must not be logged -- it's an expected, quiet retry path");

  // The loop must still be fully functional afterward -- not merely
  // silent, but genuinely recovered.
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-r5-quiet", { hasClip: true, ageMs: 5000 })] }));
  const recovered = await flushOneTimer();
  if (!recovered) throw new Error("expected normal scheduling to resume after both quiet failure paths");
  const row = tbody.querySelector('tr[data-event-id="ev-r5-quiet"]');
  if (row.dataset.mediaState !== "ready") throw new Error("expected the row to reconcile normally once polling recovered");
  if (capturedConsoleErrors.length !== 0) throw new Error("the successful recovery poll itself must not log anything");
});

// Round 5: anything reaching the outer catch that is NEITHER an
// AbortError NOR a network-level TypeError is, by construction, not a
// per-event reconciliation failure (those are caught inside the forEach
// now) -- it's something unexpected in the surrounding fetch/parse logic
// itself (simulated here as response.json() itself throwing, e.g. on
// malformed JSON), and must be surfaced via console.error rather than
// silently swallowed alongside routine network retries.
test("an unexpected exception outside the per-event loop (e.g. malformed JSON) is logged, and polling still retries", async () => {
  resetFakes();
  buildFixture();
  const mod = factory();
  mod.scheduleEventPoll();
  fetchQueue.push(() => ({
    ok: true,
    status: 200,
    json: async () => {
      throw new Error("malformed JSON body");
    },
  }));
  const flushed = await flushOneTimer();
  if (!flushed) throw new Error("expected a retry to still be scheduled after the unexpected exception");
  if (capturedConsoleErrors.length !== 1) {
    throw new Error(`expected exactly 1 console.error call for the unexpected exception, got ${capturedConsoleErrors.length}`);
  }
  const [, loggedError] = capturedConsoleErrors[0];
  if (!(loggedError instanceof Error) || loggedError.message !== "malformed JSON body") {
    throw new Error("expected the actual unexpected error to be logged");
  }
  // And discovery genuinely still works on the very next tick -- logging
  // the failure must not have replaced normal retry behavior.
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-r5-after-unexpected", { hasClip: true, ageMs: 1000 })] }));
  await flushOneTimer();
  const tbody = document.querySelector("#events-table tbody");
  if (!tbody.querySelector('tr[data-event-id="ev-r5-after-unexpected"]')) {
    throw new Error("polling must still find a real new event after recovering from the unexpected exception");
  }
});

// ------------------------------------------------- round 3: discovery bootstrap (real P0 blocker)

test("a page that starts with zero Processing rows still schedules discovery polling", async () => {
  resetFakes();
  buildFixture(); // no addRow() calls -- zero rows, all settled/none
  const mod = factory();
  mod.scheduleEventPoll();
  if (pendingTimers.size !== 1) throw new Error("expected discovery polling to be scheduled even with nothing processing");
  if (pendingTimerDelay() !== 15000) throw new Error(`expected the slow (15000ms) discovery cadence, got ${pendingTimerDelay()}ms`);
});

test("settled polling continues indefinitely at the slow cadence -- never permanently stops", async () => {
  resetFakes();
  buildFixture();
  const mod = factory();
  mod.scheduleEventPoll();
  for (let i = 0; i < 4; i++) {
    fetchQueue.push(jsonResponse(200, { events: [] }));
    const flushed = await flushOneTimer();
    if (!flushed) throw new Error(`expected discovery polling to still be scheduled after tick ${i + 1}`);
    if (pendingTimerDelay() !== 15000) throw new Error("expected to remain on the slow cadence with nothing ever discovered");
  }
});

test("a new event appearing in the API after page load is inserted automatically, no reload needed", async () => {
  resetFakes();
  const { tbody } = buildFixture(); // starts with zero rows
  const mod = factory();
  mod.scheduleEventPoll();
  if (tbody.querySelector('tr[data-event-id="ev-new-discovery"]')) {
    throw new Error("test setup error: the row must not already exist");
  }
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-new-discovery", { hasClip: true, ageMs: 3000 })] }));
  await flushOneTimer();
  const row = tbody.querySelector('tr[data-event-id="ev-new-discovery"]');
  if (!row) throw new Error("a newly-discovered event must be inserted into the table automatically");
  if (row.dataset.mediaState !== "ready") throw new Error("expected the newly-discovered event to reconcile with its real media state");
});

test("a newly-discovered Processing event switches polling to the fast cadence", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  const mod = factory();
  mod.scheduleEventPoll();
  if (pendingTimerDelay() !== 15000) throw new Error("expected to start on the slow cadence");
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-fresh-processing", { hasClip: false, ageMs: 0 })] }));
  await flushOneTimer();
  const row = tbody.querySelector('tr[data-event-id="ev-fresh-processing"]');
  if (!row || row.dataset.mediaState !== "processing") throw new Error("expected the newly-discovered event to be tracked as processing");
  if (pendingTimerDelay() !== 4000) {
    throw new Error(`expected polling to switch to the fast (4000ms) cadence once a processing row was discovered, got ${pendingTimerDelay()}ms`);
  }
});

test("polling returns to the slow cadence once the discovered event becomes ready", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  const mod = factory();
  mod.scheduleEventPoll();
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-fresh-2", { hasClip: false, ageMs: 0 })] }));
  await flushOneTimer(); // discovered as processing -> fast cadence
  if (pendingTimerDelay() !== 4000) throw new Error("expected the fast cadence while the discovered event is still processing");
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-fresh-2", { hasClip: true, ageMs: 4000 })] }));
  await flushOneTimer(); // becomes ready
  const row = tbody.querySelector('tr[data-event-id="ev-fresh-2"]');
  if (row.dataset.mediaState !== "ready") throw new Error("expected the event to reconcile to ready");
  if (pendingTimerDelay() !== 15000) {
    throw new Error(`expected polling to return to the slow (15000ms) cadence once nothing is processing again, got ${pendingTimerDelay()}ms`);
  }
});

test("a transient error during settled (slow-cadence) polling does not permanently stop discovery", async () => {
  resetFakes();
  buildFixture();
  const mod = factory();
  mod.scheduleEventPoll();
  if (pendingTimerDelay() !== 15000) throw new Error("expected to start on the slow cadence");
  fetchQueue.push(() => {
    throw new TypeError("network down during idle discovery polling");
  });
  const flushed = await flushOneTimer();
  if (!flushed) throw new Error("a transient failure while settled must still reschedule discovery polling");
  if (pendingTimers.size !== 1) throw new Error("expected discovery polling to still be scheduled after the transient failure");
  if (pendingTimerDelay() !== 15000) throw new Error("expected to remain on the slow cadence after a transient failure with nothing discovered yet");
  // And discovery genuinely still works afterward -- not just rescheduled and stuck.
  fetchQueue.push(jsonResponse(200, { events: [freshEvent("ev-after-error", { hasClip: true, ageMs: 1000 })] }));
  await flushOneTimer();
  const { tbody } = { tbody: document.querySelector("#events-table tbody") };
  if (!tbody.querySelector('tr[data-event-id="ev-after-error"]')) {
    throw new Error("discovery must still find a real new event after recovering from a transient error");
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
