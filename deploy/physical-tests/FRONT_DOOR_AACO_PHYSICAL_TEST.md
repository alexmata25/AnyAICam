# Front Door AAC Voice Call + AACO microphone: physical validation procedure

Prepared 2026-09-27 on the Dell. No microphone, camera or physical test has been run for this document.
AACO accepts **arbitrary natural-language** input, typed or spoken. The phrases below are samples to check coverage, not a fixed command list.

## 1. What the software guarantees (verified by tests)

**Voice input.** One shared implementation, `window.aacoAttachVoice`, now used by **both** the floating AACO widget and the full `/aaco` page:
- **The `/aaco` page gained a mic button.** It had none before, even though it said "typed or spoken".
- **Voice and typing share one path.** The spoken sentence is written into the same command box and submitted through the same form. There's no separate voice request.
- **Lifecycle:**
  - Tap to listen; tap again, or finish speaking, to stop.
  - Interim words are shown ("Hearing: …"), and only the final sentence is sent.
  - The first silence retries once automatically.
  - Leaving the page stops listening.
  - Fixed race: tapping stop and then quickly start again used to let the old session's late end kill the new session. The old session's events are now ignored.
- **Clear messages.** In every case listening stops, typing still works, and the mic can be used again:

  | Browser error | Message shown |
  |---|---|
  | Permission denied | "Microphone permission was denied…" |
  | Denied on a plain-HTTP page | "…needs a secure page: open the VMS over HTTPS or at http://localhost…" |
  | No microphone | "No microphone was found…" |
  | Speech service blocked | "Speech recognition is blocked in this browser…" |
  | No internet for the browser's speech service | "…needs an internet connection…" |
  | No audio heard | "No audio detected. Check that your microphone is unmuted…" |
  | Start failed | "Voice input could not start…" |

- **Unsupported browsers** (no Web Speech API, for example Firefox): the mic button stays disabled with a tooltip, and typing works.
- **Microphone permission policy.** VMS pages send `Permissions-Policy: camera=(self), microphone=(self)`, so the mic is allowed on VMS pages, including the same-origin Voice Call screen iframe. `main.py`'s fallback previously said `microphone=()`, which would have blocked every VMS page. It's now `self`, locked by a test.

**Arbitrary natural language** (`aaco_freeform.py`). Fixed tonight:
- "Who is at the door right now?" / "Is someone at the front door?" now opens **Front Door live view**. Before, it ran a 24-hour history search. Past-tense questions stay history searches.
- "…since noon" / "since 9 AM" / "since this morning" now means **from then until now**. Before, noon was treated as a single moment.
- "Yesterday evening/morning/afternoon/night" now means **that part of yesterday**. Before, it meant the whole day.
- "Was there a delivery / did the courier / mailman / postman come" now means **person events**. "Who rang the doorbell" now points at the **door camera**. Before, deliveries got "I didn't catch that".
- "The driveway right now" no longer selects a camera named **"Driveway Right"**; it asks which driveway.

Everything AACO understood before still parses the same (all 357 previous AACO/Voice Call tests pass).

**Authorization and security.**
- **Camera names come from the server,** limited to the customer's own authorized cameras. A client can't inject names.
- **Other tenants:** a customer asking about another tenant's Front Door gets no match, and nothing is opened or played.
- **Door unlock and talk** only ever come from the exact grammar, never from a loose reading. The door name must match **exactly** (never fuzzy): "open up for the mailman" becomes a door called "up for the mailman", which fails closed with an audited 403 and no relay call.

## 2. Preconditions

- **Deploy the fixes first.** Tonight's AACO fixes are committed on `reconcile/golden-foundation-20260911` but not deployed. The staging portal still runs f5a6d87, which has the voice race, no mic on `/aaco`, and the parsing gaps above. So a staging cutover is needed first; it's zero-downtime and gated. It doesn't affect Samsung, which is pinned to f5a6d87 and not claimed yet.
- **Browser:** Chrome or Edge on a computer or phone with a microphone. The Web Speech API in these browsers sends audio to the browser vendor's speech service, so it **needs internet access**.
- **Page:** `https://portal-staging.anyaicam.com/aaco`, which is HTTPS and therefore a secure context. On the Ryzen's own page, use `http://localhost:8000`, never the LAN IP.
- **Account:** a customer owner of the account with the Front Door camera (41dc80c85e).
- **Recent Front Door events** make the history questions meaningful. For example, walk past the Front Door camera once before the test and note the time.

## 3. Procedure

| # | Action | Expected |
|---|---|---|
| 1 | Open `/aaco`. | A 🎤 button next to "Run command", enabled (Chrome/Edge). |
| 2 | Tap 🎤 and **allow** the microphone. | Button turns red and pulses; status "Listening…". |
| 3 | Say: *"Who came to the front door this morning?"* | "Hearing: …" while speaking, then "Heard: …", then a list of Front Door person events from 05:00 until now. |
| 4 | Tap 🎤 and say: *"Is anyone at the front door right now?"* | A Front Door **live view** link or result, not a history list. |
| 5 | Say freely, in your own words, two or three questions of your choice about visitors, deliveries or motion at the Front Door. For example *"did a delivery come to the front door since noon"* or *"any movement by the front door yesterday evening"*. | Each returns the matching Front Door result, or a short clarifying question. Never "I didn't catch that" for a reasonable Front Door question. |
| 6 | **Type** one of the same questions and press Enter. | The same result as spoken. |
| 7 | Tap 🎤, then immediately tap it twice more (stop, start). Then speak. | Still listening after the quick restart, and the sentence is handled normally. |
| 8 | Tap 🎤 and stay silent for about 10 seconds. | "Still listening…" once, then "No audio detected…". Typing still works. |
| 9 | Revoke the mic permission for the site (browser site settings), then tap 🎤. | "Microphone permission was denied. Type your command instead." Typing still works. |
| 10 | Repeat 1–3 in the **floating AACO widget** on the Live page. | Same behaviour, same results. |
| 11 | Say: *"Open up for the mailman."* | No door action. Either a clarification, or "Camera is unavailable" (an audited, failed-closed attempt). The relay never fires. |
| 12 | **AAC Voice Call:** trigger a Front Door visitor call (walk up to the Front Door camera, or use the Voice Call test trigger), then open the call from the notification. | Call screen with the Front Door live video and the Talk button. Answering and ending update the call state. Two-way audio from this screen depends on Talkdown's physical validation, which is still open. |

**PASS** when steps 1–11 behave as expected and the call screen in step 12 opens with live video, answer and end.

## 4. Known limits (not defects)

- **Firefox** has no Web Speech API, so its mic stays disabled and typing works.
- Browser speech recognition needs **internet**, even on the local Ryzen page.
- **Door unlock and talk by voice** only work with the exact phrasing ("unlock the front door"), by design.
- **Talk (two-way audio) physical validation is still pending.** The last attempt never reached the server; the browser URL used is still needed.

## 5. Tests backing this

- `app/tests/test_aaco_voice_js.py` with `app/tests/js/aaco_voice.test.mjs` (15 behavioural voice scenarios under Node). A negative control confirmed that the old code fails the race tests.
- `app/tests/test_aaco_front_door_readiness.py` (31 cases).
- `app/tests/test_microphone_permissions_policy.py` (2 cases).
- The existing AACO/Voice Call suites.

Total: 391 passed, 6 skipped.
