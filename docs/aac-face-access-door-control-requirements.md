# AAC Face Access -- Door/Relay Control Requirements (2026-09-17)

Recorded verbatim (organized) from the user's own explicit product specification, per their instruction: "Add this requirement to the AnyAiCam Facial Recognition / Face Access milestone... Preserve this as a required Face Access feature of the AnyAiCam VMS and integrate it with the existing facial-recognition, notification, live-view, two-way-audio and access-control architecture rather than creating a separate standalone system."

This document is the specification. It does not claim everything below is built -- see `PROJECT_CHECKPOINT.md`'s own dated entry for what has actually been implemented against it, and what remains.

## Audit-first instruction

"Audit and recover the existing preserved facial-recognition/AACO/access-control work first rather than rebuilding it." -- already done as of the 2026-09-17 merge of `claude/aac-facial-recognition-phase1` (via `integration/facial-recognition-on-reconciliation-20260916`) into `reconcile/golden-foundation-20260911`. That work already provides: `facial_people`/`facial_embeddings`/`facial_watchlists`/`facial_events`/`facial_rules`/`facial_settings` tables, a real (classical-CV, pluggable) face engine, `relay_control.py`'s logical relay/access-control model (channel, pulse, cooldown, dry-run) with a `MockRelayProvider`, and an opt-in (default off) live-detection hook. This requirement builds on that foundation, not a new one.

## 1. Camera Settings -- Door/Access Assignment

Each camera must have an Access Control / Face Access ON/OFF toggle in Camera Settings. When enabled, that camera becomes associated with a physical controlled door, configurable:

- Door name (e.g. "Front Door")
- Camera associated with that door
- Relay/output associated with that door
- Relay activation duration
- Appropriate access permissions

Example: Camera 1 is renamed "Front Door", Face Access is enabled, and it is mapped to Relay 1 -- the VMS now knows the Front Door camera can control that physical door.

## 2. Live Camera Tile

For cameras configured for door access, add an "Unlock Door" button directly on the live camera tile alongside existing controls (Settings/gear, Talk/two-way audio when supported). Do NOT show Unlock on cameras not mapped to an access-control relay.

Pressing Unlock sends the command through the configured access-control/relay integration to activate the configured dry-contact relay for the configured duration, allowing the door strike/maglock/access hardware to release.

## 3. Facial Recognition Access Modes

Three face/access behaviors:

1. **Recognized + authorized for automatic entry.** If Facial Recognition identifies a person explicitly authorized for that particular door and the current permitted time/schedule, the VMS may automatically activate the configured relay and grant entry. Record identity, camera, door, time, authorization decision, relay action, and associated event/media in the audit history.
2. **Recognized person without automatic-entry authorization.** Do NOT auto-unlock. Notify the customer (e.g. "John is at the Front Door"). Tapping the notification deep-links directly into that camera's live view, where the customer can see the person, talk (when supported), and press Unlock Door.
3. **Unknown person.** Do NOT auto-unlock. Send "Unknown person at Front Door", deep-linking to the Front Door live tile the same way, for visual verification / talk / manual unlock.

This supports a friend arriving at an apartment entrance: the resident is alerted, opens the live Front Door camera, confirms who is there, and manually unlocks.

## 4. Notification Security

Push notifications deep-link to the authenticated VMS camera/door view. Email may contain a secure link into the authenticated app and correct camera view -- **never a direct unauthenticated door-unlock action in an email**. Authentication and authorization must still be checked at the moment the Unlock command is executed (never trusted from the notification/link itself).

## 5. Security / Audit Requirements

Every automatic or manual unlock attempt must be auditable: customer/user or recognized identity, camera, door, relay/output, timestamp, automatic vs. manual, authorization result, success/failure.

**Fail closed.** An unknown face, recognition failure, missing authorization, unavailable relay, expired session, or communication failure must never cause an automatic unlock.

## Integration constraint

Build on the existing facial-recognition, notification, live-view, two-way-audio, and access-control architecture -- never a separate, parallel system.
