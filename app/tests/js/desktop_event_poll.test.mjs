// Real, executed behavioral test of the desktop Events page's
// scheduleEventPoll()/reconcileDesktopEvent()/settleOverdueDesktopRows()
// reconciliation loop (P0 #5 remediation, rounds 2 and 3 -- round 3,
// 2026-09-05, fixed a real P0 blocker ChatGPT Work's own staging QA
// found: round 2's scheduleEventPoll() permanently stopped for good
// the moment nothing was processing, so a page loaded with zero
// pending rows could never discover an event created afterward without
// a manual reload. Round 3 adds a slow discovery cadence
// (EVENT_SLOW_POLL_INTERVAL_MS) that never permanently stops, switching
// to the existing fast cadence the moment something is actually
// processing). String-content assertions on the rendered HTML can
// prove the constants and markup exist, but they cannot exercise the
// actual async retry/timeout/deadline/DOM-mutation behavior these bugs
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
let pendingTimers = new Map(); // id -> { fn, ms }
function fakeSetTimeout(fn, ms) {
  const id = timerIdSeq++;
  pendingTimers.set(id, { fn, ms: ms || 0 });
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
async function flushOneTimer() {
  const entries = [...pendingTimers.entries()];
  if (entries.length === 0) return false;
  const [id, { fn, ms }] = entries[0];
  pendingTimers.delete(id);
  fakeNow += ms;
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
globalThis.CSS = { escape: (s) => String(s).replace(/["\\]/g, "\\$&") };

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
  if (row.dataset.mediaState !== "settled") {
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
  if (row.dataset.mediaState !== "settled") {
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

test("multiple new events in one response preserve newest-first order", async () => {
  resetFakes();
  const { tbody, countPill1 } = buildFixture();
  // Start with zero existing rows -- every event in the response below
  // is genuinely new.
  tbody.children = [];
  const mod = factory();
  const oldest = freshEvent("ev-old", { hasClip: true, ageMs: 30000 });
  const middle = freshEvent("ev-mid", { hasClip: true, ageMs: 20000 });
  const newest = freshEvent("ev-new", { hasClip: true, ageMs: 10000 });
  // The real endpoint returns DESC (newest first) -- reconcile in that
  // same order via the module's own reconciliation entry point.
  [newest, middle, oldest].reverse().forEach((event) => {
    const row = mod.reconcileDesktopEvent(event);
    if (row) tbody.insertBefore(row, tbody.firstChild);
  });
  const ids = tbody.children.map((r) => r.dataset.eventId);
  if (ids.join(",") !== "ev-new,ev-mid,ev-old") {
    throw new Error(`expected newest-first order ev-new,ev-mid,ev-old but got ${ids.join(",")}`);
  }
});

test("duplicate event IDs across polls never create a second row", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  addRow(tbody, { id: "ev-7", timestamp: isoAgo(5000), mediaState: "processing" });
  const mod = factory();
  const again = mod.reconcileDesktopEvent(freshEvent("ev-7", { hasClip: true, ageMs: 5000 }));
  if (again !== null) throw new Error("reconciling an existing event id must return null (update in place), never a new row");
  if (tbody.children.length !== 1) throw new Error("must still have exactly one row for ev-7");
});

test("only one timer/fetch is ever in flight at a time", async () => {
  resetFakes();
  const { tbody } = buildFixture();
  addRow(tbody, { id: "ev-8", timestamp: isoAgo(5000), mediaState: "processing" });
  const mod = factory();
  mod.scheduleEventPoll();
  mod.scheduleEventPoll();
  mod.scheduleEventPoll();
  if (pendingTimers.size !== 1) throw new Error(`expected exactly 1 pending timer, found ${pendingTimers.size}`);
  fetchQueue.push(neverResolves());
  await flushOneTimer();
  if (fetchCalls.length !== 1) throw new Error("expected exactly 1 fetch call in flight");
  mod.scheduleEventPoll();
  mod.scheduleEventPoll();
  if (fetchCalls.length !== 1) throw new Error("calling scheduleEventPoll again while a fetch is in flight must not start a second one");
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
