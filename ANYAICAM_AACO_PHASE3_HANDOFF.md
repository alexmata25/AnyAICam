# AACO Phase 3 Handoff

## Source

- Worktree: `C:\Users\Alejandro Mata\OneDrive\Desktop\AnyAiCam-AACO-command-engine`
- Branch: `feature/aaco-command-engine`
- Starting Phase 3 SHA: `4b6c3e0bc8eac2bdc6cdcfb47844c4acdc0e41ef`
- Phase 3 implementation SHA: `5efb66edf15915c04659f0c78a3256ad40f1f530`

## Customer-facing delivery

`/aaco` is now a responsive customer operator workspace. It has an
on-demand command box, example-command buttons, a visible conversation of the
customer request and AACO result, loading, empty, clarification, and error
states, plus a narrow current-context indicator. It remains usable on mobile
without creating a second tab-based VMS.

Opening the page still makes **no** historical-event, thumbnail, recording,
media, timeline, or clip request. The browser reaches `/api/aaco/command`
only after a customer submits a command. Search produces bounded metadata and
does not call the existing clip-creation endpoint.

## Approved command coverage

The deterministic, fail-closed grammar now handles:

- `Show Camera 1`
- `Show the front entrance` (exact authorized friendly camera name)
- `Show Camera 2 from 3:15 yesterday`
- `Show Camera 4 yesterday at 3:30 PM`
- `Go back 20 minutes` / supported word-number variants
- `Show person events from the last 2 hours`
- `Which cameras are offline?`
- `Show previous event`
- `Return to live`

Unqualified `Show the camera`, invalid times, unknown/destructive text, and
context-dependent commands without a safe context return a clarification or a
generic authorization denial. No customer-facing command becomes arbitrary
database, filesystem, shell, camera, S3, AWS, or API access.

## Context and VMS integration

Only a strict context shape can be returned to the server: AACO camera token,
optional playback timestamp, and optional selected-event timestamp. It is
convenience state, never authorization. Every command independently crosses
the Phase 1 structured command boundary and Classic's existing tenant/camera
checks.

- Live uses Classic's existing `can_live` authorization and focused Live URL.
- Playback uses the existing `can_playback` scope and bounded recording
  metadata resolver before linking to Classic Playback. If no existing
  recording is found, AACO clearly says playback media is unavailable.
- Named cameras are exact friendly-name matches within the authenticated
  customer's existing authorized lists.
- Event search and previous-event navigation expose authorized metadata only;
  neither path creates a clip or presigns/storage-bypasses media.
- `Return to live` and `Go back` use the currently selected, server-rechecked
  camera context.

The remaining Phase 2 limitation remains: Classic's current event helper
materializes customer-scoped candidates before AACO applies a time/type filter.
This happens only after an explicit command. A later shared bounded server-side
event search API is the right improvement; AACO must not create a separate
storage/query pipeline as a workaround.

## Tests

Passed:

```text
pytest -q app/tests/test_aaco.py app/tests/test_aaco_web.py
7 passed

PYTHONPATH=app pytest -q \
  app/tests/test_final_tenant_isolation_reaudit.py \
  app/tests/test_customer_playback_date_api.py \
  app/tests/test_customer_camera_status_health_states.py
25 passed
```

The AACO tests cover zero page-open VMS calls; controlled Live, Playback,
event search, status, name-based camera resolution, playback navigation,
previous-event navigation, return-to-live, clarification, malformed input,
unauthenticated access, cross-tenant denial, and no search-to-clip path.

The relevant Classic and tenant-isolation regression suite passed. The known
unrelated multi-appliance Live test failures recorded in the Phase 2 handoff
were not changed or treated as AACO regressions.

## Next step

Do not begin Phase 4 automatically. A future phase should expand natural
language only by mapping into the same approved schema and wait for the
underlying Classic Live/Playback pilot before any appliance-relevant release.

No staging deployment occurred. AWS, Ryzen, Samsung, recording IAM, Live
Relay, Playback infrastructure, and Claude's Camera-1 Playback pilot were not
modified. Classic VMS routes and navigation remain intact.
