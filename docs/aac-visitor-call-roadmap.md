# AAC Visitor Call (AAC VC) — roadmap (2026-09-19)

**Status: not implemented, and deliberately not started.** This is a
design placeholder only, requested explicitly to sit on the roadmap
after AACO itself is completed and stabilized -- AAC VC belongs to the
same speech/language-engine family as AACO and should reuse it, not
duplicate it, but building it now would mean designing on top of an
AACO surface (local-LLM activation, the capability/action architecture,
the floating assistant widget) that is still actively changing this
same session. Nothing in this document should be read as authorization
to begin implementation.

## Goal

When someone approaches a designated entrance/front-door camera and
says something like "Hello," "Is anybody home?," "I'm here,"
"Delivery," or "Can someone let me in?," AAC VC should recognize that
speech as a **visitor event** -- not merely record audio the way a
camera's motion/audio detection already might.

## Intended workflow

1. Detect speech at a designated visitor/entrance camera.
2. Use the AACO language engine to determine visitor intent.
3. Create an AAC Visitor Call event.
4. Notify the owner/user.
5. Open the correct live camera automatically for that owner.
6. Allow immediate two-way audio/talkdown.
7. If that entrance has AAC access control, give the authorized owner
   the *option* to unlock the door after verifying the visitor --
   never automatically.
8. Record/audit the visitor event, notification, conversation-related
   actions, and any unlock action.

## Why this reuses AACO rather than becoming a second AI system

AACO's already-built architecture (`app/aaco.py`'s strict
`AacoCommand`/`Clarification` boundary, `app/aaco_llm.py`'s
`NaturalAacoLanguageAdapter`/`LlamaCppInterpreter`/
`_validate_ai_command()` validator, and the widened capability/action
model) is exactly the "natural language -> intent -> strict typed
action -> existing authorization -> existing VMS function" pipeline
AAC VC also needs. The language-understanding step ("is this speech a
greeting/delivery/request-for-entry, and does it map to a known safe
action?") is the same *kind* of problem AACO already solves for typed/
spoken customer commands; AAC VC should be a new **trigger surface**
and a new **event/notification layer** on top of that same engine, not
a second, separately-built interpreter.

## What is genuinely new here (not already built by AACO)

AACO today is always **customer-initiated**: a portal user types or
taps the mic and AACO responds once, synchronously, inside an
authenticated session. AAC VC is different in a way that matters for
design, not just scale:

- **Passive/ambient trigger, not a customer request.** Something has
  to listen at the camera (continuously, or gated by
  motion/proximity) and decide *when* speech is present and worth
  interpreting at all -- a new audio-ingestion/trigger pipeline off an
  entrance camera's existing two-way-audio hardware, not a new AI
  model. This is the piece that doesn't exist yet in any form.
- **A new event type**, not a customer-issued `AacoCommand`. A
  detected visitor utterance isn't the customer asking AACO to do
  something; it's an unauthenticated visitor's speech being classified
  by AACO's language layer into an intent (greeting / delivery /
  request-for-entry / unclear), which then *creates* a first-class
  "AAC Visitor Call" event and drives a notification -- the owner is
  the one who eventually acts, through their own already-authenticated
  session.
- **Notification delivery** (push/SMS/email to the owner) tied to that
  event -- reusing whatever notification plumbing this VMS already has
  for alerts, not a new channel.
- **Auto-opening the correct live camera** for the owner when they
  respond to the notification -- likely the same `live_view`
  `AacoCommand`/deep-link shape AACO already produces, just triggered
  by the event instead of a typed phrase.
- **A configurable "is this an entrance/visitor camera" flag** per
  camera, analogous to the existing `door_access_enabled`/
  `talk_down_supported` capability columns.

## Requirements carried forward into any future design

- Reuse AACO's language/speech architecture -- never a second,
  independently-built AI system.
- Support configurable entrance cameras (opt-in per camera, not
  every camera).
- Integrate with the *existing* two-way audio/talkdown feature --
  never a new audio pipeline of its own.
- Integrate with Face Access/access control where applicable.
- **Never unlock automatically merely because someone asks verbally.**
  This is a hard boundary, not a tunable default: a visitor saying
  "let me in" must only ever produce a *notification and an option*
  for the authenticated owner -- the owner's own affirmative action is
  what reaches `unlock_door`, exactly as AACO's existing `unlock_door`
  operation already requires an authenticated identity today. AAC VC
  must never give unauthenticated visitor speech a path to
  `execute()`/`unlock_door` of any kind.
- Door unlock must continue through the existing authorization,
  permissions, relay control, and audit logging
  (`door_access._authorized_door_camera()`, `relay_control`,
  `record_door_access_event()`) -- completely unmodified, exactly as
  AACO's own `unlock_door` operation already does.
- Preserve tenant isolation throughout: a visitor event, its
  notification, and any resulting action must stay scoped to the one
  customer/tenant that owns that camera, with zero cross-tenant
  visibility.
- Support delivery/person-at-door style alerts as one of the
  recognized visitor-intent categories, not a separate feature.

## Open design questions for whenever this is actually scoped

- Where the new event type lives relative to `aaco.Operation` (a
  sibling type entirely, since it's system-detected rather than
  user-issued, rather than a new literal value squeezed into the
  existing customer-command enum).
- What triggers the ambient listening window at the camera (motion,
  proximity/doorbell press, or always-on with its own speech-activity
  detector) -- a real hardware/bandwidth/privacy tradeoff, not just a
  software one.
- How "unclear" visitor speech is handled -- almost certainly a
  generic "someone is at the door" alert with the raw camera/live link
  and no invented intent, mirroring AACO's own fail-closed
  `Clarification` behavior for commands it can't safely parse.
