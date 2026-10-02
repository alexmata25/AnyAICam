# AACO capability inventory & action-map design (2026-09-19)

Read-only inventory first, as requested, before any implementation
below. Goal: confirm what "natural language -> intent/action selection
-> strict typed VMS action -> existing authorization -> existing VMS
function" already looks like in this codebase, find the real gaps, and
scope the smallest safe increment -- not a rewrite.

## 1. Customer-facing VMS capabilities that actually exist today

| Capability | Real backend entry point | Authorization already enforced |
|---|---|---|
| Live view a camera | `/customer/cameras/{id}/live`, `_customer_live_cameras()` | `can_live` (owner: all; viewer: `customer_camera_permissions.can_live`) |
| Playback at/near a specific time | `/playback` page, `_customer_recording_rows()` | `can_playback` scope, customer-scoped camera list (`_customer_playback_cameras`) |
| Relative playback navigation ("go back N minutes") | same playback path, offset from current context | same `can_playback` |
| Event search (bounded time range) | `/investigate`, `_customer_detection_events()` | customer_owner: all; customer_viewer: cameras with `can_playback=1` |
| Event-type filtering | Investigate/dashboard JS: `motion`, `person`, `vehicle` (car/truck/bus/motorcycle/bicycle/vehicle), `lpr`/`plate`, `people_counting`, `intrusion` | same as event search |
| "Previous event" navigation | Investigate context carry-over | same as event search |
| Camera/fleet status ("which cameras are offline") | existing dashboard camera list | customer-scoped camera list only |
| Door unlock | `door_access._authorized_door_camera()` + `relay_control` + `record_door_access_event(trigger_type=...)` | its own strict, fail-closed `can_unlock` check -- deliberately not inferred from `can_live`/`can_playback` |
| Push-to-talk ("talk-down") | `/api/customer/cameras/{id}/talk/start` + a live WebSocket audio session (`talk_sessions.py`) | `can_talk` (viewer) / implicit owner access, `talk_down_supported` camera capability |
| PTZ (pan/tilt/zoom) camera control | **does not exist anywhere in this codebase** -- confirmed by an exhaustive case-insensitive search of `app/` for `ptz` | n/a |

## 2. What "natural language -> typed action -> authorization" already is, today

This architecture is **not new work** — it was built and merged in the
AACO local-LLM phase (`app/aaco_llm.py`, `docs/aaco-local-llm-design.md`)
and already matches the shape requested:

```
customer text -> NaturalAacoLanguageAdapter.parse()
                    -> LlamaCppInterpreter.interpret()   [natural language -> intent]
                         -> raw JSON -> _validate_ai_command()  [the ONLY security boundary]
                              -> AacoCommand (one of aaco.Operation's 7 typed values) | Clarification
                    -> aaco.execute()                     [strict typed action -> existing authz -> existing VMS fn]
                         -> _ClassicAacoBoundary.<method>()  [unmodified can_live/can_playback/can_unlock checks]
```

`aaco.Operation` is the existing closed "action" enum:
`live_view | playback | event_search | camera_status |
playback_navigation | event_navigation | unlock_door`. This already
*is* the "approved typed AACO/VMS action" list the new request asks
for — the model (or the regex grammar) can only ever select one of
these seven values; `_validate_ai_command()` rejects anything else,
including an operation the model invents. Nothing about this pass
needs a new orchestration layer — the orchestration layer already
exists and is already fail-closed.

## 3. Real gaps found (not assumptions — verified against the actual code)

1. **Event-type allow-list is narrower than the VMS's real taxonomy.**
   `aaco_llm._ALLOWED_EVENT_TYPES` and `aaco.py`'s regex grammar only
   ever accept `person`/`vehicle`/`car`. The Investigate/dashboard UI
   itself already filters on six categories: `motion`, `person`,
   `vehicle`, `lpr`, `people_counting`, `intrusion`. A customer asking
   "show me license plate events" or "any intrusion alerts?" cannot be
   served today even though the underlying `search_events` boundary
   method is already generic enough to filter on any of them — it just
   compares `event.get("event_type") != normalized_type`. **This is a
   safe, additive widening**, not a new capability.

2. **Live View doesn't act on playback/events results — confirmed bug.**
   `live_view_page.py`'s `_AACO_LIVE_RESULT_JS` only inserts a manual
   "Open Playback" / "Open in Investigate" `<a>` link into a box the
   customer must separately notice and click; unlike the `live` case
   (which already auto-scrolls/highlights with no click required), a
   playback or event-search result never actually navigates anywhere
   on its own. This is exactly the bug the request describes and is
   fixed below.

3. **PTZ does not exist.** There is no pan/tilt/zoom control anywhere
   in this VMS — no route, no camera column, no relay/serial
   integration, nothing. AACO cannot "reuse an existing PTZ API"
   because none exists. Building PTZ camera control from scratch is a
   real, separately-scoped hardware/protocol feature (ONVIF or
   vendor-specific PTZ commands, new authorization rules, new camera
   capability metadata) — **not implemented in this pass**, and not
   safe to fake with a typed action that has no real backend to call.
   AACO will say a PTZ request "isn't supported yet," per the
   request's own explicit fallback rule, rather than guess or silently
   no-op.

4. **Talk-down is real but is not a sensible discrete "action."**
   Every other AACO operation is a single fire-and-forget call
   (unlock a door, open a playback link, run a search). Talk-down is
   an *interactive, continuous* press-and-hold audio session — the
   customer's own live microphone audio is the entire content of the
   action; there is nothing for a JSON command to carry except "start
   the session," and a customer already does that by holding the
   existing mic button. Adding an `AacoCommand` operation for this
   would authorize starting a session but could never carry out what
   the customer actually wants said, so it would not reduce any real
   hardcoding. **Recommendation: not modeled as an `aaco.Operation` in
   this pass.** If wanted later, the safe shape is UI-only — like
   `live_view` already does — scrolling to and focusing the matching
   tile's existing talk button, never opening a session on the
   customer's behalf.

## 4. Smallest safe increment implemented this pass

- Widen the event-type allow-list (grammar regex, `_validate_ai_command`,
  the LLM system prompt's documented values, and `_ClassicAacoBoundary
  .search_events`'s own normalization comment) to the real six-category
  taxonomy: `person`, `vehicle` (car/truck/bus/motorcycle/bicycle/
  vehicle all normalize to it, unchanged), `motion`, `lpr`,
  `people_counting`, `intrusion`.
- Fix `_AACO_LIVE_RESULT_JS` so a `playback` or `events` result
  actually navigates the browser to the authorized `href` AACO already
  returned (`window.location.assign(...)`), instead of leaving an
  unclicked link behind — the same "no second click required" standard
  `live` already meets. The status message the customer already sees
  (`body.message`) is shown for a brief moment before navigating so the
  transition is not silent/confusing.
- No change to `aaco.Operation`'s shape, no new VMS route, no change to
  `execute()`'s authorization logic, no PTZ, no talk-down action.
  `DeterministicLanguageAdapter` (the fallback) is untouched in
  structure — only its event-type alternation is widened to match.

## 5. What stays exactly as-is, on purpose

- Every authorization check (`can_live`, `can_playback`, `can_unlock`,
  tenant/customer scoping) is reused completely unmodified — this pass
  touches zero authorization code.
- The AI/local-LLM path (`aaco_llm.py`) still cannot reach a shell, SQL
  connection, filesystem path, relay, or arbitrary URL — it has no
  import of any module that could (confirmed unchanged: no new imports
  added to `aaco_llm.py`).
- `_validate_ai_command()` remains the single security boundary; the
  widened event-type set is added to its own explicit allow-list, not
  a loosened check.
- An unsupported request (PTZ, or anything else outside the seven
  operations) still returns the existing fixed clarification message,
  never a guess.
