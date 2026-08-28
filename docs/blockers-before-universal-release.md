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
