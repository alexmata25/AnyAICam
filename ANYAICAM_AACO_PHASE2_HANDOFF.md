# AACO Phase 2 Handoff

## Source

- Worktree: `C:\Users\Alejandro Mata\OneDrive\Desktop\AnyAiCam-AACO-command-engine`
- Branch: `feature/aaco-command-engine`
- Phase 1 base: `2672fb48a5fe78978ee3b6d6307e172327361195`
- Phase 1 final: `e0a80d83c36e115951e17b9b00f3b4ac73a7c0ff`
- Phase 2 implementation: `77a36b1e9639fbca78f53fe712206bdd659e7f18`

## Delivered interface

`GET /aaco` renders a lightweight customer-only operator workspace. It makes
no event, timeline, thumbnail, recording, media, or clip request when the page
opens. The only browser VMS request is `POST /api/aaco/command` after a user
submits text. The server logs the page-open and structured command operation
without logging raw command text.

The command endpoint accepts only `{command, context}` JSON, checks customer
authentication, parses through `app/aaco.py`, and returns either a
clarification or a constrained Live, Playback, event-search, or camera-status
presentation. Unknown, destructive, malformed, and ambiguous input fails
closed. The browser uses text nodes for returned labels/status rather than
inserting server values as HTML.

## Classic VMS reuse

`_ClassicAacoBoundary` in `app/main.py` is the only production bridge:

- **Live:** resolves the friendly camera number through Classic's established
  customer Live authorization, then deep-links to its existing focused Live
  page. AACO creates no Live session or relay.
- **Playback:** resolves through Classic's playback authorization and makes a
  bounded existing-recording metadata lookup near the requested time. It then
  deep-links to `/playback`; AACO does not presign media or create a clip.
- **Event search:** reuses Classic's customer-scoped event representation,
  filters only the requested command range/type for presentation, and returns
  at most 100 metadata rows. It never calls the clip-creation endpoint.
- **Status:** reuses Classic's customer camera list and existing status
  handler.

The common Phase-1 camera gate accepts an identity only when the camera is
available through an existing authorized customer VMS surface. Each operation
then applies its specific Classic rule: Live uses `can_live`; Playback and
event search use `can_playback`. Existing Classic routes independently repeat
their authorization when their deep links are opened.

## Commands demonstrated by tests

- `Show Camera 4`
- `Show Camera 4 yesterday at 3:30 PM`
- `Show person events from the last 2 hours`
- `Which cameras are offline?`
- `Go back twenty minutes` after server-validated playback context

## Tests

Passed:

```text
pytest -q app/tests/test_aaco.py app/tests/test_aaco_web.py
6 passed

PYTHONPATH=app pytest -q \
  app/tests/test_final_tenant_isolation_reaudit.py \
  app/tests/test_customer_playback_date_api.py \
  app/tests/test_customer_camera_status_health_states.py
25 passed
```

The AACO tests prove no VMS call on page open; controlled Live, Playback,
event-search, status, and playback-context handoff; no clip path for search;
malformed/destructive/ambiguous rejection; unauthenticated denial; and
cross-tenant camera denial.

An additional `test_camera_multi_appliance_isolation.py` run reported two
existing Live-view assertions failing (old-appliance landing returned 303 and
unscoped Live omitted older Camera 1–5 labels). Phase 2 only appends isolated
AACO registration and does not modify Live code; this unrelated signal was not
changed or resolved in this branch.

## Current limitations and Phase 3 recommendation

The Classic event helper currently materializes customer-scoped candidates
before AACO applies its requested time/type filter. This happens only after a
command, never at page open, but a future shared bounded server-side event
search API should accept the time/type filters directly. Do not add an AACO
storage path as a workaround.

Phase 3 should add more natural-language forms only by mapping them to the
same strict command schema, use a shared bounded event-search service when it
exists, and validate the real Classic Live/Playback pilot before considering
any appliance release. Do not deploy AACO to Ryzen merely because this cloud
interface exists.

## Deployment state

No staging deployment occurred. AWS, Ryzen, Samsung, Live Relay, Playback
infrastructure, recording IAM, and Claude's Camera-1 Playback pilot were not
modified. Classic navigation and endpoints were preserved. PR #15 remains
outside this branch.
