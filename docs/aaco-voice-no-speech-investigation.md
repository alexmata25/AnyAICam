# AACO voice input: "no-speech" investigation (2026-09-19)

## Report

A real staging browser session found typed AACO commands worked but
voice did not. Temporary in-page diagnostics (commit
`89ed94dc84dd0d56b0179ce6ceea2b940417f397`, since removed) captured the
exact failure from a real customer browser:

- Speech API supported: yes
- Mic button: enabled
- Listening started: yes
- Last error: `no-speech`
- Transcript received: none

## What this rules out

- **Not a `Permissions-Policy` block.** Verified via a real authenticated
  fetch to `/customer-live` that the permissive header
  (`camera=(self), microphone=(self)` from `app/cloud_security.py`) wins
  over the restrictive `setdefault` in `app/main.py`. If the policy had
  blocked the microphone, `recognition.start()` would never have reached
  `onstart` at all.
- **Not unsupported browser / missing API.** `window.SpeechRecognition`
  (or the `webkit` variant) was present and constructible.
- **Not a denied permission.** Chrome (and other browsers implementing
  this API) never fires `onstart` unless the microphone permission was
  already granted. `onstart` fired, so permission was not the blocker.
- **Not a JS syntax/deploy defect.** The shipped script was verified with
  `node --check` before and after this fix.

## What this points to

With permission, support, and session start all confirmed, the only
remaining failure mode is that the recognition session ended (its own
internal silence/no-speech timeout, which page script cannot lengthen)
before it registered any usable audio. That is consistent with either:

1. A real but ordinary timing gap between clicking the mic and the user
   actually starting to speak, with no feedback in between to reassure
   them anything was heard, or
2. A genuine OS/browser microphone **input selection** problem on the
   customer's machine (wrong default recording device, an unmuted-but-
   effectively-silent input, or the input gain set too low) -- the
   browser layer cannot distinguish this case from (1). A working
   permission grant only proves the browser was *allowed* to use a
   microphone, not that the specific device selected as the system
   default is actually producing audio.

## Fix applied (small, client-side, `app/aaco_web.py`)

1. `recognition.interimResults = true` (was `false`): partial words now
   appear in the status line the instant the recognizer hears anything,
   giving the fastest possible signal that audio is or isn't reaching
   it.
2. Exactly one silent, automatic retry on a *first* `no-speech` error,
   before reporting failure -- directly covers "the window ended before
   the user started talking."
3. If `no-speech` recurs after that retry, the final message now says so
   plainly and points at the likely cause: *"Check that the correct
   microphone is selected and unmuted in your system sound settings."*

The temporary diagnostics block (`show_voice_diagnostics`) that produced
the report above has been removed from the shipped customer UI now that
its purpose -- identifying the exact failure -- is done.

## What was deliberately not done

Per explicit scope: no new speech-to-text architecture (no
`getUserMedia` + local/server STT) was built. If `no-speech` still
recurs for a customer after this fix, it should be treated as a
known, environment-level (OS/browser microphone selection) limitation
of browser-native `SpeechRecognition`, not an AACO defect -- and any
decision to invest in a fallback STT pipeline remains a separate,
explicitly gated decision.
