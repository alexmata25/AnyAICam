#!/usr/bin/env node
// Real execution (not string assertions) of the ACTUAL shipped
// renderMobileRecentEvents() card-building logic, extracted directly
// from main.py between its own function boundary and the
// mobileList.innerHTML=... assignment (see the extraction in this
// suite's own runner script) -- same idiom this project already
// established in test_playback_analytics_lanes.mjs/test_playback_
// segment_chaining_core.mjs for pure/DOM-light front-end logic.
//
// Proves, against the real code, not a paraphrase of it:
//   - an event with a real event.thumbnail renders that exact URL
//   - findClipNear() is never called anywhere in this function (the
//     stub below throws if it ever is -- this is the actual regression
//     test for the root-cause bug: the old code called findClipNear()
//     to guess an event's preview from a nearby recording instead of
//     using the event's own already-correct thumbnail field)
//   - an event with no thumbnail gets the compact "<Type> · No clip
//     available" fallback, never a large empty box
//   - a recording (clip) row still renders from clip.id, independent
//     of any of the above
//
// Usage: node test_mobile_playback_video_cards.mjs <path-to-main.py>

import assert from "node:assert/strict";
import fs from "node:fs";

const mainPyPath = process.argv[2];
if (!mainPyPath) {
  console.error("usage: node test_mobile_playback_video_cards.mjs <path-to-main.py>");
  process.exit(2);
}

const src = fs.readFileSync(mainPyPath, "utf8");
const startMarker = "  function renderMobileRecentEvents(cameraId,clips,events){{";
// Two separate single-line needles (not one multi-line string) so this
// survives either LF or CRLF line endings in main.py.
const endNeedleLine1 = "mobileList.innerHTML=(clipRows+eventRows)||";
const endNeedleLine2 = "No recent activity for this camera.</div>';";

const start = src.indexOf(startMarker);
if (start === -1) throw new Error("renderMobileRecentEvents() not found in main.py");
const line1Idx = src.indexOf(endNeedleLine1, start);
if (line1Idx === -1) throw new Error("mobileList.innerHTML assignment not found after renderMobileRecentEvents()");
const line2Idx = src.indexOf(endNeedleLine2, line1Idx);
if (line2Idx === -1) throw new Error("end-of-function fallback text not found after the innerHTML assignment");
const end = line2Idx + endNeedleLine2.length;

const jsSource =
  src
    .slice(start, end)
    .replaceAll("{{", "{")
    .replaceAll("}}", "}") +
  "\n  }"; // closes the function body -- extraction deliberately stops
           // mid-body, right after the innerHTML assignment, before the
           // click-wiring code that needs a real DOM's querySelectorAll.

let passed = 0;
function ok(label) {
  passed++;
  console.log(`ok - ${label}`);
}

function run(scenarioName, { clips, events, filterCategoryImpl }) {
  const mobileListEl = { innerHTML: null };
  const documentStub = {
    getElementById(id) {
      if (id === "mobile-recent-events-list") return mobileListEl;
      return null;
    },
  };
  const EVENT_COLORS = {
    motion: "#f0b94d", person: "#4d9ef0", vehicle: "#a06df0",
    lpr: "#3dbfae", people_counting: "#4dcf7a", intrusion: "#f0554d",
  };
  const activeFilters = new Set(["motion", "person", "vehicle", "lpr", "people_counting", "intrusion"]);
  function filterCategory(eventType) {
    return filterCategoryImpl ? filterCategoryImpl(eventType) : (EVENT_COLORS[eventType] ? eventType : eventType);
  }
  function playbackDate(value) {
    return new Date(typeof value === "number" ? value : String(value) + (String(value).endsWith("Z") ? "" : "Z"));
  }
  function findClipNear() {
    throw new Error(`findClipNear() must never be called by renderMobileRecentEvents() (scenario: ${scenarioName})`);
  }

  const fn = new Function(
    "document", "EVENT_COLORS", "activeFilters", "filterCategory", "playbackDate", "findClipNear",
    jsSource + "\nreturn renderMobileRecentEvents;"
  )(documentStub, EVENT_COLORS, activeFilters, filterCategory, playbackDate, findClipNear);

  fn("cam-1", clips, events);
  return mobileListEl.innerHTML;
}

// --------------------------------------------------------------- an event with a real thumbnail

{
  const html = run("thumbnail-present", {
    clips: [],
    events: [{ id: "evt-1", event_type: "person", timestamp: "2026-09-04T18:00:00Z", has_event_clip: true, thumbnail: "/api/customer/events/cam-1/evt-1/thumbnail" }],
  });
  assert.match(html, /<img class="mobile-media-thumb" src="\/api\/customer\/events\/cam-1\/evt-1\/thumbnail"/);
  assert.doesNotMatch(html, /No clip available/);
  assert.match(html, /mobile-media-badge[^>]*>Person</);
  ok("an event with event.thumbnail renders that exact thumbnail URL");
}

// --------------------------------------------------------------- findClipNear() never called

{
  // The stubbed findClipNear() above throws immediately if invoked --
  // reaching this line at all (for a scenario that, under the old
  // buggy code, WOULD have called it for every single event row) is
  // itself the proof.
  run("findClipNear-guard", {
    clips: [{ id: "rec-1", start: "2026-09-04T17:55:00Z", end: "2026-09-04T18:00:00Z" }],
    events: [
      { id: "evt-1", event_type: "person", timestamp: "2026-09-04T18:00:00Z", has_event_clip: true, thumbnail: "/api/customer/events/cam-1/evt-1/thumbnail" },
      { id: "evt-2", event_type: "vehicle", timestamp: "2026-09-04T18:05:00Z", has_event_clip: false, thumbnail: null },
    ],
  });
  ok("findClipNear() is never called as the event-thumbnail source, with or without a nearby recording present");
}

// --------------------------------------------------------------- no media -> compact fallback, not a big empty box

{
  const html = run("no-thumbnail", {
    clips: [],
    events: [{ id: "evt-2", event_type: "vehicle", timestamp: "2026-09-04T18:05:00Z", has_event_clip: false, thumbnail: null }],
  });
  assert.match(html, /mobile-media-fallback">Vehicle · No clip available</);
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /No preview/);
  assert.doesNotMatch(html, /Analytics only/);
  ok("an event genuinely without media gets the compact fallback, not a large empty preview box");
}

// --------------------------------------------------------------- recording (clip) row still renders

{
  const html = run("clip-row", {
    clips: [{ id: "rec-42", start: "2026-09-04T18:00:00Z", end: "2026-09-04T18:05:00Z" }],
    events: [],
  });
  assert.match(html, /data-mobile-clip role="button" tabindex="0"/);
  assert.match(html, /src="\/api\/customer\/recordings\/cam-1\/rec-42\/thumbnail"/);
  assert.match(html, /5 min/);
  ok("a recording still renders its own thumbnail card with a real duration");
}

// --------------------------------------------------------------- unfiltered/unsupported categories don't crash and don't call findClipNear either

{
  run("unsupported-category", {
    clips: [],
    events: [{ id: "evt-3", event_type: "smart_motion", timestamp: "2026-09-04T18:10:00Z", has_event_clip: false, thumbnail: null }],
  });
  ok("an unsupported/filtered-out category is handled without ever reaching findClipNear() either");
}

console.log(`\n${passed}/${passed} passed`);
