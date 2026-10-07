# Headless label claim (2026-10-07)

Goal: an AnyAiCam appliance ships **unclaimed**; the customer needs only
**power + Ethernet**, then claims it from their own phone or computer —
no monitor, keyboard, terminal, desktop or OS password.

Builds on the existing self-service claim exchange
(`app/appliance_claims.py`, docs/non-interactive-activation-*.md): device
polls outbound, owner confirms in the portal, device redeems a one-time
proof for its permanent credential. What was missing for a screenless box:
the claim only ran from `anyaicam-setup --claim`, which showed its code on
the appliance's own terminal.

## Flow

1. **Factory imaging** — `sudo ./install.sh --portal-url=https://app.anyaicam.com [--product-mode=…]`
   * `identity_provision` (unchanged) creates the UUIDv4 `appliance_id`.
   * `claim_label_provision` (new) creates a 12-character Crockford base32
     claim code (60 bits). Plaintext only in
     `/var/lib/anyaicam-label/claim-label.txt` (root, 0700 dir / 0600 file),
     with the QR link and the appliance ID. The agent gets only
     `/etc/anyaicam/label_claim.json` = `{"version":1,"verifier":sha256("anyaicam-label-claim-v1:"+code)}`.
     Kept across repair/reinstall; the verifier is rewritten from the
     root-only file every run.
   * `cloud_portal_provision` (new) sets `ANYAICAM_PORTAL_URL` and
     `ANYAICAM_AGENT_MODE=production` in `agent.env` (other lines kept).
     `--portal-url` must be https and not local; it is checked before the
     install changes anything.
2. **Label** — print from `claim-label.txt`:
   `Claim code: XXXX-XXXX-XXXX` (text) and a QR code of
   `https://app.anyaicam.com/claim#label=XXXX-XXXX-XXXX`.
   (`sudo cat /var/lib/anyaicam-label/claim-label.txt`; support can reprint
   a lost label from the same file.)
3. **Customer plugs it in** — `anyaicam-agent.service` starts with no
   credential. `headless_claim.HeadlessClaim` (new, called from
   `_await_activation`) opens a claim carrying the label verifier and keeps
   one open (a claim lasts 15 min; an expired one is replaced at once).
4. **Customer claims it** — scans the QR (or goes to `app.anyaicam.com/claim`
   and types the code), signs in or creates an account, picks a site,
   confirms. `/claim` moves the code from the URL fragment into the tab's
   sessionStorage and strips it from the address bar before sign-in; the
   fragment is never sent to a server.
5. **Appliance activates itself** — next poll returns the proof; the agent
   redeems it, runs the same `_finish_enrollment()` as the terminal flow
   (without the camera-discovery prompt), restarts the VMS with the cloud
   URL, and the service continues into normal operation. Cameras, live view,
   recording and playback are then managed from the portal.

## Security

* Possession of the box **and** its label is the right to claim it (router /
  camera setup-code model). Guessing: 60-bit code, `claim_portal_limiter`
  (30/min per account), and only appliances that are online with a pending
  claim can match.
* At rest: the appliance holds only the verifier; the cloud only
  `password_hash(verifier)` (PBKDF2-SHA256, 310k). The code is never logged
  (installer, agent or cloud) and never put in a URL path or query.
* Unchanged: device possession secret, encrypted proof, single-use proof,
  credential retry-recovery, UUIDv4 device IDs, rate limits, the terminal
  `--claim` and admin activation-token flows.
* The agent only claims with an https portal that is not its own VMS, and
  discards a saved claim that belongs to another portal or device.

## Tests (all offline)

* `app/tests/test_appliance_label_claims.py` — code normalization, verifier
  formula, begin/resume/lookup/confirm/complete by label, wrong/expired codes,
  customer-owner and site checks, no code in logs, `/claim` page headers.
* `appliance-agent/tests/test_headless_claim.py` — eligibility, full flow,
  expiry replacement, resume after reboot (with proof), lost completion
  retried with the same proof, refused completion, local enrollment failure
  keeps the proof, foreign claim discarded, 409 waits, backoff, never raises,
  no secrets logged, service integration with a fake clock.
* `installer/tests/test_claim_label_installer.py` — code format/uniformity,
  verifier matches the cloud, repair keeps the label, QR link, privacy modes
  (Linux), symlink refusal, `--portal-url` validation and agent.env update.

## Not in this slice

* Label printing hardware/template (the file has everything a printer needs).
* A local "status" page on the appliance (optional convenience).
* On-device keypair for claim requests (design doc Phase 2).
* Cloud deployment of this branch (needs the normal staging → production
  runbook and owner approval).
