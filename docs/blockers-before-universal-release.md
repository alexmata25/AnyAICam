# Blockers before universal release

Tracked issues that must be resolved before AnyAiCam-VMS ships as a
single universal release across all appliances (Ryzen, Samsung, future
mini PCs). Each entry records what was found, the evidence, and the
direction of the fix -- not an implementation, unless noted.

## Edge appliances must not maintain independently authoritative customer passwords

**Found:** 2026-08-28, while unblocking live camera streaming on the
Samsung appliance.

**Problem:** `RUNTIME_ROLE=edge` appliances (confirmed on Samsung) run
the full application stack locally, including a fully self-contained,
independently-writable `partner_users` table in their own local SQLite
database. `POST /api/partner-login` (`app/partner_portal.py`) never
delegates or checks against any cloud identity service -- it is 100%
local. This means the same customer email can end up with **different
password hashes on different appliances**, silently, with no sync or
reconciliation.

**Confirmed in practice:** a customer (`alexmata25@gmail.com`) reset
their password successfully at `app.anyaicam.com` (a separate,
Cloudflare-fronted production environment) and could log in there, but
the identical login against Samsung's local `/api/partner-login`
returned `403` / `reason: invalid` -- because Samsung's local
`password_reset_tokens` table had **zero rows, total**, proving the
reset never touched Samsung's database at all. The two environments'
`partner_users` rows for the same email had simply drifted apart.

**Direction for the real fix:** this codebase already has a working
precedent for exactly this shape of problem -- the appliance's own
identity is cloud-delegated (`appliance_identity.py`: signed manifest,
cloud-delegated auth, revocation reconciliation), not locally
authoritative. Customer authentication for `RUNTIME_ROLE=edge` should
follow the same pattern: the edge role must stop treating its local
`partner_users` table as authoritative for customer login, and instead
verify/delegate against the canonical cloud identity service, with any
local data retained only as a controlled cache for offline/local
operation -- never a second, independently-writable password store.

**Explicitly not done today:** as a strictly-scoped development
unblock (approved separately), Samsung's local `password_hash` for
this one account was synchronized to match the working cloud password,
via the application's own `password_hash()` helper, with an
before/after verification that email, role, `customer_id`,
`approved`/`account_status`, and `identity_grants` were all otherwise
unchanged. That is a one-account, one-appliance patch, not a fix for
the underlying architecture -- the next appliance provisioned, or the
next password reset on any existing one, will hit the exact same
divergence until the delegation redesign above is implemented.

## Password-reset tokens are logged in plaintext via GET query strings

**Found:** 2026-08-29, while diagnosing repeated "token invalid/expired"
reports for an administrator password reset on Samsung.

**Problem:** the reset link built by `POST /api/password-reset/request`
(`app/cloud_features.py`) carries the raw, single-use token as a `GET`
query parameter (`/reset-password?token=...` and
`/customer-reset-password?token=...`). Uvicorn's default access-log
format logs the full request line for every request, including the
query string. The token therefore ends up in plaintext in the
container's log history the moment the reset link is opened -- before
the token is ever submitted to `POST /api/password-reset/complete`
(which is a `POST`, and does not appear in access logs with its body).

**Confirmed in practice:** while reading `docker logs anyaicam-vms`
during this session's own diagnosis of a *different* issue, a
`GET /reset-password?token=<43-character token>` line appeared with
the token in plaintext. That specific token had already been consumed
by the time it was seen and posed no further risk, but the underlying
logging behavior is unconditional and applies to every reset link,
for every account, every time.

**Direction for the real fix:** the token must not travel as a `GET`
query parameter at all, or access logging must be configured to
redact query strings for these two routes specifically. Options include
(a) having the reset-password page perform a client-side redirect that
moves the token from the URL into a `POST` body or a short-lived,
httponly cookie before rendering the form, so the token is never part
of a logged `GET` request line, or (b) a custom Uvicorn/Starlette
access-log formatter that masks `token=` query values for
`/reset-password` and `/customer-reset-password` specifically. No
change has been made yet -- this is scoped as its own fix, not bundled
into unrelated work.

**Explicitly not done:** no logging configuration was changed; no
token-delivery mechanism was altered. The already-consumed token seen
during diagnosis was never repeated or written anywhere outside the
container's own existing log history.

## Camera dbc5f83343 (Samsung) is blocked at ONVIF authorization, not at credentials or protocol choice

**Found:** 2026-08-28, while resolving `cameras.onvif_endpoint` for
camera `dbc5f83343` (device_key `urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af`,
appliance-assigned `camera_number=1`) so `camera_url()` could locate it.

**Problem:** `anyaicam-agent`'s automatic ONVIF resolution
(`onvif_media.py`, run during every provisioning job and every
periodic sweep) has never once succeeded for this camera. Every
attempt logged in `anyaicam-agent.service` over a 3+ hour window,
including the exact attempt for provisioning job `2efb97815bc6`
(`13:36:15` job received -> `13:37:04`
`onvif_media.not_resolved_during_provisioning ... status=auth_required`),
ends the same way: the device's ONVIF Device/Media service challenges
for authentication and then rejects the credentials, using the exact
same clean, verified-correct camera credentials that already
authenticate this same camera's RTSP stream (RTSP-level DESCRIBE auth
confirmed working per the `36ae0a3` fix).

**Confirmed in practice:** two independent, read-only ONVIF-standard
auth mechanisms were tested directly against the camera's
`http://192.168.0.38/onvif/device_service` (`GetDeviceInformation`,
read-only), both using the same stored credentials, decrypted
in-memory only and never logged:
- WS-Security UsernameToken (PasswordDigest) -- the mechanism
  `onvif_media.py` already implements -- rejected (`auth_required`)
  on every provisioning/sweep attempt.
- Raw HTTP Digest (RFC 2617) against the camera's own advertised
  challenge (`realm="IP Camera(GC887)"`, `qop="auth"`) -- tried both
  the qop-correct response and the legacy non-qop response (a known
  firmware bug class) -- both rejected with an identical generic 401.

Both mechanisms failing identically, with credentials already proven
correct for this same camera's RTSP stream, means the ONVIF/web
service on this device is rejecting these credentials at the
device/account level (e.g. ONVIF disabled, or gated behind a separate
dedicated ONVIF user/permission set from the RTSP-stream account) --
not a WS-Security-vs-HTTP-Digest client compatibility gap.

**Do not classify this as a VMS/client-code bug unless later evidence
proves otherwise.** No auth-mechanism change was made to
`onvif_media.py`; there is nothing demonstrated broken in the client
to fix. No alternate/guessed credentials were tried against the
camera, and no camera setting, network configuration, or credential
was changed.

**Direction for the real fix:** requires physical/on-LAN access to the
camera's own admin web UI (`192.168.0.38`) to check: ONVIF
enabled/disabled, whether a dedicated ONVIF user exists (vs. only an
RTSP-stream account), that user's ONVIF/admin/media permissions, and
the camera's configured ONVIF authentication mode. Once corrected on
the device side, the existing, unchanged resolution path (WS-Security
first, per `onvif_media.py`) should succeed on its own on the next
provisioning/sweep cycle -- `GetProfiles` -> `GetStreamUri` ->
persisted `onvif_endpoint` -> `camera_url(1)` -> FFmpeg -> Live View.

**Explicitly not done:** no camera settings, networking, or
credentials were changed; no RTSP URL was hard-coded as a workaround.

## Camera 1 (Samsung, device_key `urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af`) is blocked at RTSP-level credential verification -- distinct from the ONVIF authorization blocker above

**Found:** 2026-08-29, provisioning this same physical device (the
customer-facing counterpart of the `dbc5f83343` ONVIF blocker above)
through the normal customer Discover -> "Add this camera" flow, after
two prerequisite fixes landed and were verified working end to end on
this same day: (1) `ANYAICAM_CAMERA_CREDENTIAL_KEY` is now provisioned
on every edge appliance (`installer/06-deploy-vms.sh`'s
`ensure_vms_env()`), and (2) the customer-setup page correctly polls a
provisioning job's real outcome instead of trusting the initial
"queued" response (`partner_workspace.py`'s `pollProvisioning()`).

**Confirmed correct going in:** discovery itself was not in question --
this device was one of 5 real candidates surfaced by completed scan
job `49508bfcaa18`, with a stable `device_key` distinguishing it from
the other 4, and the customer selected it deliberately (`name="Front
Door"`) via the normal Discover -> "Add this camera" flow.

**Problem:** the customer submitted ONVIF/RTSP credentials for this
device twice, carefully, in two separate attempts roughly an hour
apart. Both attempts reached the physical camera for real and both
were rejected identically:

| job_id | queued (UTC) | delivered to appliance | result posted | status | message |
|---|---|---|---|---|---|
| `e83e810314d7` | 18:55:23 | 18:55:58 (`count:1`) | 18:56:23 | failed | "Device rejected the provided credentials." |
| `872fbc99cef8` | 19:52:21 | 19:52:30 (`count:1`) | 19:52:55 | failed | "Device rejected the provided credentials." |

Both failures happened at the same stage: `anyaicam_agent`'s
`provisioning.py::verify_device()` -> `classify_rtsp_authentication()`,
a plain RTSP DESCRIBE challenge/retry on port 554 (RFC 2617 Digest or
Basic, whichever the camera's own `WWW-Authenticate` challenge
specifies). This runs *before*, and gates, ONVIF resolution --
`service.py::poll_provisioning()` only calls
`_resolve_media_uri_after_provisioning()` (the sole caller of
`onvif_media.py`'s `GetProfiles`/`GetStreamUri` resolution) when
`verify_device()` returns success. Since it returned failure both
times, **ONVIF was never contacted on either attempt** -- confirmed by
`/var/log/anyaicam/agent.log` on the appliance host, which has no
ONVIF-related line for either job, only "Provisioning job received."
Neither the exact RTSP response code nor the literal
`WWW-Authenticate` challenge text is logged or persisted anywhere
(`classify_rtsp_authentication()` returns only the classification
string, by design, to keep every log/DB message credential- and
device-detail-free) -- there is nothing more granular available to
inspect for either attempt.

This is a **different, earlier gate than the `dbc5f83343` ONVIF
authorization blocker above**, not a recurrence of it: that blocker is
specific to the ONVIF SOAP service (`onvif/device_service`), which
this flow never reaches while RTSP verification keeps failing first.
The original blocker's own record states RTSP-level DESCRIBE auth was
already confirmed working against this exact camera with its
verified-correct credentials (the `36ae0a3` fix) -- two credentialed
resubmissions since then failing identically at that same,
previously-working layer is new information that record doesn't
explain.

**Do not classify this as a VMS/client-code bug unless later evidence
proves otherwise.** The full pipeline this depends on -- credential
entry, encryption, single-delivery to the appliance, and RTSP
verification -- is confirmed working correctly by this same evidence:
both jobs were genuinely queued, encrypted, delivered exactly once,
verified against the real device, and reported back accurately within
seconds, and the customer-setup UI correctly reflected each outcome
(retryable "Add this camera," not a false "Added"). Nothing in this
trace points at the client. No alternate/guessed credentials were
tried, and no camera setting, network configuration, credential, or
firmware was changed.

**Direction for the real fix:** requires physical/on-LAN access to the
camera to confirm its actual RTSP-stream account username/password
(directly on the device's own admin UI, not assumed from an earlier
session) before the next attempt, since two independent, careful
submissions have now been rejected at the one layer previously proven
to accept this camera's correct credentials. While on site, this is
also the opportunity to finally check the still-open ONVIF-side items
from the `dbc5f83343` blocker above (ONVIF enabled/disabled, a
dedicated ONVIF user vs. only an RTSP account, that user's
permissions, configured ONVIF auth mode) -- if the RTSP credential
turns out to have simply changed, the ONVIF gap may still exist
underneath it once RTSP is corrected.

**Confirmed safe failure, both times:** no `cameras` row was ever
created for this device_key and no `camera_credentials` row exists --
`camera_provisioning_requests.encrypted_credentials` is cleared to
`NULL` the instant each job concludes, success or failure, and a
failed job never reaches the code path that would create either row.
Nothing was left half-committed by either attempt.

**Explicitly not done:** no camera settings, networking, credentials,
or firmware were changed; no third resubmission was requested; no
camera row was manually created.
