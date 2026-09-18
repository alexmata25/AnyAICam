# WireGuard direct remote-connectivity plan

Written 2026-09-17, before any implementation, per explicit instruction to
audit first. Every claim below about existing behavior was verified by
reading the actual current source on `reconcile/golden-foundation-20260911`
at `cf8e724`, not assumed or inferred from a doc.

## 1. Goal, restated precisely

Add a fourth live-view transport -- WireGuard -- so a remote customer's
browser session can reach a camera on their appliance (Ryzen or a future
supported BYO PC) without the AWS S3/CloudFront relay carrying the media,
when WireGuard is available and healthy. LAN viewing, WebRTC/MediaMTX P2P,
and the AWS relay are all preserved exactly as they work today; WireGuard is
additive, and its absence or failure must never be visible to a customer as
anything other than "the existing paths handled it," per the fail-safe
requirement below.

## 2. Audit findings

### 2a. Existing live-view transports and their real racing behavior

Three transports exist today, in `app/live_view_p2p.py`, `app/webrtc_publisher.py`,
`app/live_relay_uploader.py`, `app/live_view_sessions.py`, `app/live_playlist.py`,
`app/live_view_page.py`:

- **LAN/local**: served directly by the appliance's own local HLS
  (`local_live_hls.py`) when the browser is on the same network; out of
  scope for this plan entirely -- it already bypasses cloud/relay/P2P.
- **WebRTC P2P** (`live_view_p2p.py` + `webrtc_publisher.py`): the browser
  and the appliance's local MediaMTX process (spawned and owned by
  `webrtc_publisher.py`, never a systemd unit -- see that module's own
  design note) negotiate a direct WebRTC session. Signaling (SDP
  offer/answer, trickle ICE) is relayed through the existing authenticated
  cloud control plane -- `live_view_p2p.py` registers customer-side routes
  under `partner_identity()` cookie auth and appliance-side routes under
  `authenticate_appliance()` bearer+nonce auth, the same two authentication
  boundaries every other customer/appliance interaction in this codebase
  already uses. **No new auth mechanism was invented for P2P, and none
  should be invented for WireGuard either** -- this is the load-bearing
  precedent for §4 below. The actual media never touches FastAPI or any
  cloud process; MediaMTX's WHEP endpoint (`webrtc_publisher.py`'s
  `whep_offer()`) does real RTSP-in/WebRTC-out with the browser's ICE
  candidates trickled to it directly. STUN-only by default
  (`ANYAICAM_LIVE_STUN_SERVERS`); TURN is a deliberately-deferred config
  addition, not yet enabled.
- **AWS relay** (`live_relay_uploader.py` + `live_playlist.py` +
  `live_cdn_signing.py`, cloud side in `appliance_cloud.py`): the appliance
  uploads local HLS `.ts` segments to S3 under a short-lived, per-session
  STS credential (`_ensure_session()`), and the browser reads a
  CloudFront-signed HLS playlist. Pure control-plane/data-plane separation:
  FastAPI never touches a media byte.

**Racing/selection is entirely client-side and best-effort, not a
server-side decision.** `live_view_sessions.py`'s `POST
/api/customer/cameras/{camera_id}/live/start` creates one
`live_view_sessions` row and, in the same transaction, queues the existing
`start_live_relay` appliance command -- the relay path *always* starts,
unconditionally. The browser (per `webrtc_publisher.py`'s own module
docstring, describing `live_view_page.py`'s `attemptLiveP2P()`) races a
bounded P2P attempt (`ANYAICAM_LIVE_P2P_TIMEOUT_MS`, default 4000ms)
against the relay's own playlist-polling start, and reports the outcome
back via `POST .../transport-outcome` (`transport` one of `p2p`/`relay`/
`failed`) purely for instrumentation -- **this report is explicitly never
trusted for authorization**, per that route's own docstring. A real,
measured incident (documented in `webrtc_publisher.py`'s module docstring)
is directly relevant precedent: an earlier non-trickle P2P design lost the
race to relay almost every time because of client-side ICE-gathering
latency, not appliance-side slowness -- confirming that **whichever new
transport is added must not add any synchronous wait to a path that would
otherwise already be showing video**, which is exactly the "never
intentionally delay a faster working path" requirement in this task's
directive.

**Conclusion for WireGuard's own racing design (§9)**: it must join this
exact same client-side race, as a fourth bounded attempt with its own
timeout floor, never as a change to when the always-on relay command is
queued.

### 2b. MediaMTX publisher (appliance side)

`webrtc_publisher.py` spawns MediaMTX as a child process of the VMS
process itself (not a systemd unit), binds MediaMTX's control API (`:9997`)
and WebRTC HTTP signaling (`:8889`) to `127.0.0.1` only, and only the actual
WebRTC media (UDP/ICE via `webrtcLocalUDPAddress`) reaches the internet --
carrying no credentials, only the video track a viewer is already
authorized to see. This loopback-only control-surface convention is
directly reusable for WireGuard: nothing about a WireGuard tunnel's own
local interface needs to be reachable from the LAN either.

### 2c. Customer portal / session & auth model

Every customer-facing route in this codebase uses `partner_identity()`
(cookie-session auth, `partner_portal.py`), then re-checks camera-level
authorization per route (`customer_owner` implicit, `customer_viewer` needs
an explicit permission row -- the `can_live`/`can_talk`/`can_settings`/
`can_unlock` family in `customer_camera_permissions`, most recently
extended for Face Access, commit `7d1afc9`). **This is the single most
important existing security boundary WireGuard must never bypass** -- see
§8.

### 2d. Appliance agent / control-plane communication

`appliance-agent/anyaicam_agent/`: `config.py` defines the on-disk identity
contract every appliance-side worker in this codebase already reuses
(confirmed identical in `webrtc_publisher.py`, `live_relay_uploader.py`,
`recording_uploader.py`) -- `credential.json` under `ANYAICAM_STATE_DIR`
(default `/var/lib/anyaicam`), written once by
`anyaicam_agent.config.save_credential()` (mode 0600, atomic
write-then-rename), read by every worker independently rather than shared
via import (an established, explicit scope decision, not an oversight --
see `live_relay_uploader.py`'s own docstring). Every appliance-to-cloud
call (`appliance_cloud.authenticate_appliance()`) requires
`X-Appliance-ID` + a bearer credential (verified against
`appliance_credentials.credential_hash`, hashed, revocable via
`revoked_at`) + a timestamp + a single-use nonce (`appliance_request_nonces`,
replay-rejected). **This exact channel is the correct, existing mechanism
for WireGuard public-key enrollment** -- no new appliance identity or
credential system should be invented; a WireGuard enrollment call is just
one more authenticated route on this same bearer channel (§5).

Privileged, root-requiring actions on the appliance never run inside the
unprivileged `anyaicam-agent` process (its own systemd unit has
`NoNewPrivileges`/`ProtectSystem=strict`/a narrow `CapabilityBoundingSet`).
Instead `appliance-agent/system/privileged_watcher.py` runs as root,
triggered by a `systemd .path` unit watching `pending_actions_dir`
(`AgentConfig.pending_actions_dir`, under `state_dir`), and only ever acts
on a fixed, hardcoded `DISPATCH` dict keyed by a marker's `type` field --
it "never reads, constructs, or executes a command, argument, path, or
container name out of the marker's content" (its own docstring). Real
WireGuard interface management (`wg-quick up`, or equivalent) requires
`CAP_NET_ADMIN`/root, exactly the same privilege class `reboot` and
`restart_vms` already require -- **this is the correct, existing mechanism
for the appliance-side WireGuard interface lifecycle**, not a new
privileged process or a relaxation of `anyaicam-agent`'s own sandboxing.
This is Phase B/C work (§12), explicitly out of scope for this pass.

### 2e. Provisioning / enrollment flow (the enrollment precedent)

`app/appliance_claims.py` (QR/pull-based self-service claim,
`docs/non-interactive-activation-design.md`) and the admin-driven
`POST /api/appliance/activate` path in `appliance_cloud.py` both terminate
in the same place: one `INSERT INTO appliances(...)` row plus an
`appliance_credentials` row. `appliance-agent/anyaicam_agent/reenrollment.py`
handles the hardware-replacement case (`coordinated_reenroll()`) by
rewriting the same `credential.json` under a fresh activation, preserving
`appliance_id`. **WireGuard enrollment should hook this exact event** (a
completed first-enroll or re-enroll) rather than being a separate,
customer-visible setup step -- see §5, §11.

### 2f. Networking assumptions already in this codebase

No STUN/TURN-equivalent rendezvous exists for anything other than WebRTC's
own ICE (STUN-only today, per §2a). The AWS relay solves NAT/firewall
traversal for video today the simplest way possible: **the appliance is
always the one that initiates an outbound connection** (S3 PutObject calls,
and every control-plane poll), so no inbound port ever needs to be opened
on a customer's router for anything in this codebase today. **This is the
key precedent for WireGuard's own endpoint-discovery design (§7)**: the
hard "two NAT'd peers find each other" problem this task's directive flags
does not actually need solving for an appliance-to-cloud tunnel, because
the appliance is always the initiator against one fixed, known cloud
endpoint -- exactly like every other appliance-to-cloud call in this
codebase already is.

### 2g. Installer / update flow

`installer/06-deploy-vms.sh`'s `deploy_vms()` mirrors the release payload
onto `VMS_INSTALL_ROOT` via `rsync -a --delete`, excluding `recordings/`,
`data/config/`, and `.env` -- **and a real, documented incident
(`docs/PROJECT_CHECKPOINT.md`'s 2026-09-17 "LPR/PPE repair-install
verified" entry) is exactly the failure class to avoid repeating**: an
ordinary VMS-only repair silently deleted the MediaMTX binary because
`mediamtx/` wasn't in that rsync's exclude list, and `10-install-mediamtx.sh`'s
own no-payload-no-op path never got a chance to notice or restore what had
already been removed. **Any WireGuard config/key directory living under
`VMS_INSTALL_ROOT` must be added to that same rsync exclude list from the
first commit that creates it**, not discovered the same way after a second
real incident. `10-install-mediamtx.sh` is also the direct template for
`11-install-wireguard.sh` (§12): opt-in at build time via
`build_release_installer.py`, checksum-verified, installs a binary/package
without starting or wiring anything, changes nothing about existing
behavior on a rebuild that doesn't include the new payload.
`installer/uninstall.sh` preserves identity/config/credentials by default
and only wipes `CONFIG_DIR`/`state_dir` under `--purge-all` -- WireGuard's
own keys, living under those same directories (§6), get this exact
preserve-by-default/purge-on-request behavior for free, with no separate
uninstall logic needed beyond removing the interface/systemd unit.

### 2h. Windows / BYO-PC implications

`RUNTIME_ROLE` (`edge`/`cloud`/`combined`) is a pure application-level
concept -- the VMS container runs identically regardless of host OS, *if*
Docker is available. **Every installer script under `installer/` is Bash,
assumes systemd, and assumes Linux paths (`/opt/anyaicam`, `/etc/anyaicam`,
`/var/lib/anyaicam`)** -- there is no Windows-native or WSL2 installer path
anywhere in this repository today. Running the VMS software on a
customer-owned Windows PC at all is therefore **an existing, currently
unsolved prerequisite this plan depends on, not something this plan
introduces or can silently assume**. WireGuard itself has an official,
well-documented Windows implementation (`wireguard.exe`, a named-tunnel
Windows service model, functionally equivalent to `wg-quick` on Linux) --
the WireGuard side of a future Windows agent is solvable, but only once a
Windows installer/service path for the VMS+agent stack itself exists.
**Flagged explicitly, not resolved here**: whether/when BYO Windows PC
support becomes a real, shipped install target is a product decision
outside this plan's scope; this plan's schema and protocol design (§5-§11)
are deliberately host-OS-agnostic so they need no rework whenever that
decision is made, but the actual Windows agent-side interface manager is
new work this plan does not schedule.

## 3. Core architecture decision: where the tunnel terminates

**The WireGuard tunnel connects the appliance (or BYO PC) to a new,
cloud-side WireGuard gateway component -- never the browser.** This follows
directly from §2a-§2c: every existing transport already separates
"how the browser is authenticated and authorized" (cookie session +
per-camera permission check, unchanged) from "how the media/signaling
actually moves" (WebRTC ICE, or S3/CloudFront). WireGuard becomes a new
option for the second half only:

- A new, small, always-on cloud-side process (the **WireGuard gateway**) on
  existing cloud infrastructure holds one WireGuard interface with one
  `AllowedIPs`-scoped peer entry per enrolled appliance (§9 covers why this
  must be strictly per-peer, not a shared subnet route).
- Once an appliance's tunnel is up, its already-loopback-bound local
  surfaces (MediaMTX's WHEP endpoint today; the VMS API/local HLS
  elsewhere) become reachable from the gateway over the tunnel's private
  address -- **still never exposed beyond the gateway itself**, exactly
  the same "loopback-only control surface, only media crosses the network"
  shape `webrtc_publisher.py` already established for P2P.
- The **browser's own contract does not change at all.** It keeps talking
  to the existing, trusted cloud portal over ordinary HTTPS with its
  existing cookie session -- for a WireGuard-carried session specifically,
  the gateway proxies the appliance's WHEP/HLS endpoint the same way the
  browser already reaches MediaMTX's WHEP endpoint indirectly today (via
  signaling relay) or the relay's CloudFront-signed playlist. The browser
  never receives a WireGuard key, a tunnel address, or any other capability
  that would let it reach anything beyond the one authorized camera stream
  it already requested -- satisfying "the browser must never gain
  unrestricted network access merely because a WireGuard tunnel exists"
  as a structural property of the design, not a policy promise layered on
  top.
- This makes WireGuard, from the browser's perspective, **indistinguishable
  in kind from the existing relay path** -- both are "the cloud fetches
  media from the appliance and hands it to an already-authorized browser
  session" -- the only difference is which network path the cloud side uses
  to reach the appliance (a WireGuard tunnel vs. an S3 upload the appliance
  itself pushed). This is deliberate: it means `live_view_sessions.py`'s
  existing authorization model (§2c) needs zero changes to accommodate a
  fourth transport; only the transport-selection/racing layer in
  `live_view_page.py`/`live_view_p2p.py`'s sibling module gains a new
  option.

## 4. Authentication / authorization

WireGuard's own cryptographic peer authentication (a fixed public key per
peer) proves *which appliance* is on the other end of a given tunnel --
necessary, but by itself it says nothing about *which customer/camera* a
particular browser session is allowed to reach through it, exactly the same
gap this codebase's own AACO work already named for its own context object
("convenience state, never authorization" -- `ANYAICAM_AACO_PHASE3_HANDOFF.md`
equivalent language). **The existing `live_view_sessions.py`/
`_authorized_camera()` checks (§2c) run unchanged, every time, regardless
of transport** -- a WireGuard tunnel being up is never itself a grant of
access to anything; it only ever changes how the cloud gateway reaches
already-authorized bytes on behalf of an already-authorized session.

## 5. Automatic enrollment

Piggybacks on the existing appliance identity lifecycle (§2d, §2e), not a
new parallel credential system:

1. On a completed first-enroll or re-enroll (the same event
   `reenrollment.py`'s `coordinated_reenroll()` already handles), the
   appliance generates a WireGuard keypair **locally** (§6) and submits
   only the public key to a new authenticated route on the existing
   `authenticate_appliance()` bearer channel --
   `POST /api/appliance/wireguard/enroll`, same header contract
   (`X-Appliance-ID`, bearer credential, timestamp, nonce) as every other
   appliance route in `appliance_cloud.py`.
2. The cloud side assigns this peer a tunnel address (§9) from the pool
   scoped to that customer's appliance(s), stores the public key +
   address, and returns the gateway's own public key + fixed endpoint
   (host:port) + the assigned tunnel address -- everything the appliance
   needs to bring its own WireGuard interface up, with **no private key
   ever crossing this call in either direction**.
3. The actual local interface bring-up on the appliance is Phase B (§12):
   the agent process (unprivileged) writes the returned config to a
   pending-actions-style marker, and a new `privileged_watcher.py`
   `DISPATCH` entry (root-owned, same fixed-argv-only shape as
   `restart_vms`/`reboot`) applies it -- never done by the enrollment call
   itself or by any code running as `anyaicam-agent`.
4. No customer-visible setup step exists in the base flow -- WireGuard
   enrollment happens automatically alongside activation, the same way
   `credential.json` itself is written without the customer doing anything
   beyond confirming their claim in the portal.

## 6. Key generation and storage

- **Private keys are generated on-device and never transmitted, in either
  direction, at any point.** The cloud control plane only ever sees and
  stores a public key -- structurally impossible to leak a private key
  through the enrollment API's own request/response contract, not merely a
  policy choice.
- **On-device storage** follows `config.py`'s own established convention
  exactly: a new `wireguard_identity.json` (private key + assigned tunnel
  address + gateway public key/endpoint) under `config_dir`
  (`/etc/anyaicam`), written with the same atomic temp-file-then-rename +
  `os.chmod(..., 0o600)` pattern `save_credential()`/`save_claim_state()`
  already use -- `config_dir` (not `state_dir`) because this is provisioned
  trust material analogous to `trusted_public_key_file`, not routine
  runtime state.
- **Cloud-side storage** is public keys only, in a new table (§10) --
  structurally the same "this table only ever holds a hash/public value,
  never a secret" shape `appliance_credentials.credential_hash` already
  establishes for the bearer-credential case, except here there isn't even
  a hash to protect: a WireGuard public key is not sensitive by design.
- Never logged: any code path that would log a marker/request/response
  touching this feature must redact the private key the same explicit way
  `live_relay_uploader.py`'s `_invalid()` helper already redacts AWS
  credentials from its own warning logs ("never the response body, which
  may contain live AWS credentials").

## 7. Endpoint discovery

**Solved by direction, not by a rendezvous protocol** (§2f): the appliance
is always the initiator, dialing the gateway's one fixed, stable
endpoint (a DNS name or static IP on existing cloud infrastructure,
analogous to `ANYAICAM_CLOUD_URL` today) -- this needs no discovery
mechanism on the gateway side at all, and no discovery mechanism on the
appliance side beyond "where is the gateway," which is returned once at
enrollment (§5) and can be refreshed the same way `ANYAICAM_CLOUD_URL`
itself is configured today (an installer-time/env value, not something the
appliance has to search for). Browser-side endpoint discovery does not
exist as a separate problem at all, because the browser never becomes a
WireGuard peer (§3) -- it only ever needs to know the ordinary cloud portal
URL it already uses today.

## 8. NAT / firewall behavior

Because the appliance always initiates outbound UDP to the gateway's one
fixed public port, **no inbound port ever needs to be opened on a
customer's home router**, mirroring the AWS relay's own zero-configuration
posture (§2f). WireGuard's `PersistentKeepalive` (a standard, built-in
setting, not custom code) keeps the NAT mapping alive between real traffic
bursts so the tunnel survives idle periods without the appliance needing to
re-dial. The one real requirement this places on the customer's network:
outbound UDP to the gateway's port must not be blocked -- true of the
overwhelming majority of consumer/small-business routers/firewalls by
default (this is a strictly easier requirement than what WebRTC's own ICE
already has to negotiate around today for P2P).

## 9. Tunnel addressing

A dedicated `/16` outside every common home-LAN default range
(`192.168.0.0/16`, `10.0.0.0/8`'s common `/24` defaults, `172.16.0.0/12`) --
proposed `10.70.0.0/16`, configurable via
`ANYAICAM_WIREGUARD_TUNNEL_CIDR`, matching this codebase's existing
convention of an env-configurable default rather than a hardcoded literal
(e.g. `ANYAICAM_LIVE_STUN_SERVERS`). The gateway holds a fixed address in
that range (`10.70.0.1`); each enrolled appliance/BYO-PC gets one `/32`
assigned at enrollment (§5), stored on its peer row (§10). **Critically,
each peer's `AllowedIPs` on the gateway's own WireGuard config is scoped to
exactly that appliance's own `/32`, never the whole `/16`** -- this is what
makes §11 (multi-appliance isolation) a structural property of the
WireGuard config itself rather than something the application layer has to
separately enforce; two different customers' appliances can never route to
each other's tunnel address even if the application layer had a bug,
because WireGuard's own peer-routing table would refuse the packet.
Collision with a customer's own LAN subnet is structurally not a concern:
the WireGuard interface is its own virtual NIC with its own address space,
entirely independent of whatever the appliance's real LAN-facing interface
is using, the same way a VPN client's tunnel adapter never conflicts with
the host's other interfaces today.

## 10. Data model (this pass's actual implementation target, §13)

New table (migration `20260917_wireguard_remote_connectivity` in
`app/db_migrations.py`, same append-only `MIGRATIONS` list convention every
other schema change in this codebase already uses):

```sql
CREATE TABLE IF NOT EXISTS appliance_wireguard_peers(
    id TEXT PRIMARY KEY,
    appliance_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    public_key TEXT NOT NULL,
    tunnel_address TEXT NOT NULL,
    gateway_public_key TEXT NOT NULL,
    gateway_endpoint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'enrolled',
    last_handshake_at TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_reason TEXT,
    FOREIGN KEY(appliance_id) REFERENCES appliances(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wireguard_peers_public_key ON appliance_wireguard_peers(public_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wireguard_peers_tunnel_address ON appliance_wireguard_peers(tunnel_address) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_wireguard_peers_appliance ON appliance_wireguard_peers(appliance_id,status);
```

Directly modeled on `appliance_credentials` (§2d): multiple rows per
appliance are allowed (never enforced to exactly one), rows are never
deleted on revocation (`revoked_at`/`revoked_reason` set instead, matching
this project's own "never rewrite history" convention already applied to
`audit_logs` and to the Ryzen-claim-cleanup entry's own `appliance_claims`
handling), and only an *active* (`revoked_at IS NULL`) tunnel address needs
to be unique -- a revoked peer's old address can be safely reassigned to a
replacement device later. `status` is one of `enrolled` / `active` /
`revoked` / `failed` -- `active` is set by the (later, Phase B/C) heartbeat
path once a real handshake is observed, not by the enrollment call itself,
matching the same "don't invent readiness the appliance hasn't actually
reported" discipline `live_view_sessions.py`'s own states/docstring
already documents ("No 'ready'/'failed' state... left entirely to the
frontend").

## 11. Key rotation / revocation

Rotation: generate a new keypair on-device, enroll it via the same route
as §5 (a second, additional row, not a replace-in-place) while the
previous key remains valid, then the gateway/agent switch the active
config to the new key and the previous row is explicitly revoked
(`revoked_at`/`revoked_reason='rotated'`) -- mirrors `appliance_credentials`'
own established multi-row-during-transition shape exactly, so a brief
overlap window during rotation is normal, not a bug.

Revocation (compromise, decommission, hardware replacement -- §12):
`revoked_at` set, `status='revoked'`, and the gateway's own WireGuard
config has that peer removed (`wg set wg0 peer <public_key> remove` or the
gateway process's own equivalent) -- this is the one action that actually
matters for security; the DB row alone does nothing until the gateway's
live config reflects it. **A revoked peer's tunnel address is never reused
by a live peer simultaneously** (enforced by the partial unique index
above), only after the old row's `revoked_at` is set.

## 12. Customer / appliance replacement

Hooks the same `reenrollment.py` `coordinated_reenroll()` event named in
§2e/§5: a re-enrollment revokes the old appliance's WireGuard peer row(s)
(§11) and enrolls a fresh keypair for the new hardware under the same
`appliance_id`, exactly mirroring how `credential.json` itself is already
rewritten on re-enrollment. No new replacement-specific code path is
needed -- WireGuard enrollment simply rides the existing one.

## 13. Multi-appliance customers

Structural, not policy (§9): each appliance's `AllowedIPs` on the gateway
scopes exactly to its own `/32`, so two appliances belonging to the same
customer -- or different customers -- can never see each other's tunnel
traffic, the same isolation guarantee already required of (and already
correctly built for) every other per-appliance credential/scope
in this codebase (`appliance_credentials`, `identity_grants`, per-camera
permissions).

## 14. RDM status / diagnostics

A new heartbeat field, following the exact precedent of the existing
`local_storage_manager.py` -> agent heartbeat -> RDM-visible-surface chain
already documented in `config.py`'s own comment for local storage state
("an HTTP call... polling GET /api/appliance/local-storage-state... NOT a
shared state file" -- `state_dir` is read-only inside the VMS container, so
cross-process handoff on the appliance is always an HTTP call, never a
shared file, for exactly the same reason WireGuard's own tunnel-health
signal must be). The agent's own periodic heartbeat gains a
`wireguard_status` field (`disabled`/`enrolling`/`active`/`degraded`/
`failed`, plus `last_handshake_at`) sourced from a real local check (the
WireGuard interface's own handshake timestamp, once Phase B exists) --
surfaced in the same customer-facing appliance health surface
`appliance_camera_status`/`appliance_health_history` already populate,
never a second, disconnected status system.

## 15. Reconnect / recovery

Mostly free from WireGuard's own protocol design: it is connectionless at
the transport level (UDP, with cryptographic handshakes re-established on
demand), so an appliance reboot or an ISP IP change is handled by the
appliance's interface simply re-initiating its next handshake against the
gateway's fixed endpoint -- no custom reconnect logic needed on the
appliance side beyond "the interface comes up and tries," matching how
every other appliance-side worker in this codebase already tolerates a
transient control-plane outage (`_control_plane_get()`'s "never clobber
previous state on failure" convention, reused verbatim across
`webrtc_publisher.py`/`live_relay_uploader.py`/`recording_uploader.py`). A
temporary tunnel outage must never affect LAN viewing, P2P, or relay --
structurally guaranteed by §3's separation (nothing about the other three
transports depends on WireGuard's state at all).

## 16. Uninstall / cleanup

Follows `installer/uninstall.sh`'s existing preserve-by-default /
purge-on-request shape (§2g) with no new logic beyond: stop and disable the
WireGuard interface/systemd unit (mirrors how the VMS/agent units are
already stopped), and let the existing `CONFIG_DIR`/`state_dir` purge path
remove `wireguard_identity.json` the same way it already removes
`credential.json` and `claim_state.json` under `--purge-all` -- no separate
secure-delete step, matching this codebase's existing `unlink()`-only
convention for every other credential file (`clear_claim_state()`).

## 17. Telemetry

Extends the existing, already-designed-for-this transport-outcome
convention (§2a's `POST .../transport-outcome`, `transport` currently
`p2p`/`relay`/`failed`) with a new value, `wireguard` -- the browser
reports which transport actually served the frames, exactly as it already
does for P2P vs. relay, with the same "never trusted for authorization"
caveat. Server-side, `appliance_wireguard_peers.status`/`last_handshake_at`
(§10, §14) gives an independent, appliance-reported signal for
enrollment/tunnel health separate from what any one browser session
observed -- letting a future dashboard distinguish "the tunnel was up but
this one session still chose relay" from "the tunnel itself was down."
Neither signal ever carries a private key, a camera credential, or any
other secret -- both are transport-name/timestamp/status enums only, the
same shape `transport-outcome` already uses today.

## 18. Fail-safe behavior

Structural consequence of §3 and §15, not a separate mechanism to build:
because WireGuard is one bounded, racing attempt alongside P2P (§2a) and
the relay command is unconditionally always queued regardless of any other
transport's state (`live_view_sessions.py`'s `start_live_view()`, unchanged
by this plan), a WireGuard enrollment failure, tunnel-establishment
failure, endpoint-discovery non-event (the appliance simply never having
enrolled), or health-check failure all reduce to exactly one case from the
browser's perspective: "this attempt didn't produce a result before its
own timeout," identical in effect to today's P2P timeout already falling
back to relay. No new fallback logic needs to be written; the existing
race's own bounded-timeout-per-transport shape already generalizes to a
fourth participant.

## 19. Explicit open items for a product decision (not resolved here)

- **BYO Windows PC as a VMS host** (§2h) is a real, currently-unsolved
  prerequisite this plan's later phases (a Windows agent-side WireGuard
  interface manager) would depend on -- whether/when that becomes a
  shipped target is outside this plan's authority to decide.
- **Whether the cloud-side WireGuard gateway is a new standalone process/
  instance, or a role added to existing cloud infrastructure** is a real
  infrastructure decision (new compute, new open UDP port, new operational
  surface to monitor) that this plan deliberately does not make -- it is
  exactly the kind of "is this an acceptable new piece of infrastructure"
  call this task's own directive named as something to bring back to the
  user rather than decide autonomously. This plan's schema/protocol design
  (§5-§13) does not depend on which choice is made.

## 20. Implementation phases

- **Phase A (this document)**: audit + plan. Done, commit `debf048`.
- **Phase A2 (`222e255`)**: the data model (§10), the enrollment/key-
  management route and its authorization/tenant-isolation/revocation
  logic (§5, §11), and real tests for tenant isolation, authorization,
  key handling (a private key is never server-side, never returned by
  any API, never logged), revocation, fallback, reconnect-tolerance,
  and failure cases. No live network/Ryzen dependency; nothing in this
  phase starts a real WireGuard interface anywhere.
- **Phase B (this pass)**: DONE at the source/design level; explicitly
  NOT deployed or executed against any real interface. Scope actually
  built:
  - **Gateway placement decision, resolved** (§19's open item): the
    SAME already-built `deploy-portal` image, run as a SECOND
    container on the SAME existing `deploy_default` network on the
    SAME existing staging EC2 instance (`i-0a082abd812929bb4`) --
    chosen as the safest option because it needs zero new image, zero
    new build pipeline, and zero new compute instance; the only real
    new infrastructure it will ever need is one new UDP port on that
    instance's security group plus `--cap-add=NET_ADMIN` on that one
    container. Full reasoning: `app/wireguard_gateway/gateway_service.py`'s
    own module docstring; template service block (not applied to real
    staging): `deploy/docker-compose.staging.example.yml`.
  - **The gateway itself** (`app/wireguard_gateway/`): a
    `WireGuardInterfaceProvider` abstraction (mirrors
    `relay_control.py`'s `RelayProvider`/`MockRelayProvider` split) --
    `MockWireGuardInterfaceProvider` (every test uses this),
    `SystemWireGuardInterfaceProvider` (real `wg` CLI argv, defined and
    argv-shape-tested with a fake runner, never instantiated against a
    real interface by anything in this pass); a pure `reconciler.py`
    diffing the database's active peers against the interface's live
    peers; a plain-HTTP `proxy.py` with zero WireGuard-specific code
    (testable against an ordinary loopback server); `gateway_service.py`
    ties them together as a real, never-started process entry point.
  - **Privileged-action design** (§2d, §5 step 3): two new
    `privileged_watcher.py` DISPATCH entries,
    `wireguard_interface_up`/`down`, both a fixed `wg-quick`
    argv pointed at a hardcoded literal path
    (`/etc/anyaicam/wireguard/wg0.conf`) that the UNPRIVILEGED agent
    process (which already owns `config_dir`) writes the real config
    content to ahead of queuing the action -- the same fixed-argv-
    reads-a-well-known-path shape `restart_vms` already established.
    Dedicated argv-shape tests in
    `appliance-agent/tests/test_wireguard_privileged_actions.py`;
    every existing DISPATCH-wide safety test (no-shell-metacharacters,
    marker-content-never-trusted) automatically covers these two new
    entries.
  - **Appliance-agent integration**: `config.py` gained
    `wireguard_identity_file`/`wireguard_conf_file` +
    `load_wireguard_identity()`/`save_wireguard_identity()` (same
    atomic-write+0600 shape as `credential.json`); a new
    `wireguard.py` module (`generate_keypair()`, `render_wg_conf()`,
    `enroll_wireguard()`); `portal.py` gained
    `PortalClient.wireguard_enroll()` on the existing authenticated
    channel; `setup_wizard.py`'s `_finish_enrollment()` gained a
    WireGuard enrollment step, gated behind `ANYAICAM_WIREGUARD_ENABLED`
    (unset everywhere today) and always non-fatal on failure (matches
    `restart_service()`'s own established precedent) -- a genuinely
    fresh enrollment gets `replace_existing=False`; a re-enrollment
    (hardware replacement) gets `replace_existing=True`, rotating the
    device's WireGuard identity the same way every other identity
    field already rotates on `coordinated_reenroll()`.
  - **Telemetry** (§17): `transport-outcome` now accepts `'wireguard'`
    alongside `p2p`/`relay`/`failed`; the heartbeat gained
    `wireguard_status`/`wireguard_last_handshake_at` (new `appliances`
    columns, COALESCE-on-omission, same shape as `storage_state`), fed
    by a new best-effort local check in the agent's own `metrics.py`
    (`disabled`/`enrolled` only -- confirming a live handshake needs a
    privileged `wg show` call, deferred to Phase C once a real
    interface exists to query).
  - **Fail-safe**: proven, not just asserted -- every new code path is
    behind the `ANYAICAM_WIREGUARD_ENABLED` flag (agent side) or fails
    closed with a clean 503 when the gateway is unconfigured (cloud
    side, unchanged from Phase A2); a dedicated regression test proves
    a failed WireGuard enrollment never aborts or rolls back the
    overall (already-succeeded) appliance activation.
  - **What remains explicitly undone**: no real WireGuard interface
    was brought up anywhere; the gateway process was never started;
    nothing was deployed to Ryzen or staging. The next real step
    (bringing up `wg0` on a real box) is the exact privileged/network
    action this phase's own directive reserves for separate, explicit
    authorization -- see the coordinator's own report for the precise
    command that step would run.
- **Phase C (this pass, partial -- staging verification only)**: the gateway
  process, enrollment route, and reconciler proven live on staging end to
  end -- still zero real WireGuard interface anywhere. See §21. Still
  future within "Phase C": the portal-side UI/diagnostics surface, the
  live-view transport-racing integration (§3, §17) actually wired into
  `live_view_page.py`, `installer/11-install-wireguard.sh` (§2g), and the
  gateway's own `proxy.py` wired into a real customer-facing route.
- **Phase D (future)**: real Ryzen/staging rollout, feature-flagged off by
  default the same way `ANYAICAM_LIVE_P2P_ENABLED` shipped, with its own
  explicit go/no-go the same way every other live-infrastructure change in
  this project has required.

## 21. Phase C staging verification (this pass)

Deployed `c5612b9` to staging (`portal-c5612b9`, recreated in place from
`portal-d971110` via the established `verify_cutover_safety.py` /
`check_container_sprawl.py`-gated cutover -- `CUTOVER_OK`, then
`CONTAINER_SPRAWL_OK`, zero-downtime-equivalent, old container preserved
stopped as `portal-d971110-pre-wireguard-gateway`, never deleted). Exact
commit confirmed byte-for-byte: `sha256sum` of `wireguard_remote.py` and
`wireguard_gateway/gateway_service.py` inside the running container
matches `git show c5612b9:...` exactly.

**New this pass**: `gateway_service.py` gained an env-gated provider
switch, `ANYAICAM_WIREGUARD_GATEWAY_PROVIDER=mock` (unset/anything else
keeps the real `SystemWireGuardInterfaceProvider` default unchanged) --
the one deploy-time switch safe to flip on staging before a real
interface exists anywhere. A second container, `wireguard-gateway-c5612b9`,
was started from the SAME already-built image with this flag set,
`--cap-add=NET_ADMIN` and the UDP port deliberately omitted -- confirmed
in its own startup log ("no real interface will ever be touched by this
process"). 9 new tests for the switch itself. Full regression: 91
failed/2747 passed/24 skipped -- established baseline, zero new
regressions.

**Real, live evidence, not "should work"**:
- Enrollment: a genuine disposable synthetic appliance identity (never
  the real pilot customer -- `e2e/scripts/provision_wireguard_test_
  harness.py`, torn down after) called the real, public
  `POST /api/appliance/wireguard/enroll` over real HTTPS. 200 with a real
  assigned `tunnel_address` (`10.70.0.2`); an identical re-call was
  idempotent (same address, no duplicate row); a wrong credential got a
  real 403; a malformed key got a real 400.
- Reconciliation: the gateway's own log, within one real 30s poll cycle,
  printed `WireGuard gateway reconciled: +1 -0 peers` -- it read the real
  database over the real network and drove the mock provider to match,
  with zero interface access.
- Telemetry: a real heartbeat carrying `wireguard_status:"enrolled"` was
  accepted (200) and confirmed, by direct DB read, actually persisted on
  the `appliances` row -- the full cloud-side telemetry path, proven, not
  just unit-tested.
- Fallback: 23 real Playwright browser tests (`test_live_view_p2p.py`,
  `test_live_view.py`, `test_camera_status.py`, `test_dashboard.py`) ran
  against this exact deployment, gateway container and all, and all
  passed -- WebRTC P2P and the existing transport-racing/live-view paths
  are provably unaffected by the gateway's mere presence.
- Data safety: `PRAGMA integrity_check: ok` and every row count
  (`partners`/`customers`/`sites`/`appliances`/`partner_users`/`cameras`)
  identical before and after, both before and after the disposable
  harness's own teardown; `appliance_wireguard_peers` (created fresh by
  this deploy's own migration) ended at exactly 0 rows.
- Resources: the gateway container uses ~10MB RAM idle; staging's memory/
  disk headroom is materially unchanged (2.3GB available, 2.4GB disk
  free) -- no repeat of the documented OOM/disk-exhaustion incidents this
  project has already had.

**Ryzen, read-only inspection only (per this project's own standing
Ryzen access model)**: `wireguard-tools` is confirmed **not installed**
on the real Ryzen appliance (`which wg wg-quick` empty, package absent).
The currently-running `anyaicam-agent.service` predates every commit in
this WireGuard effort -- it has no `wireguard.py`, no
`ANYAICAM_WIREGUARD_ENABLED` gate, and the currently-installed
`privileged_watcher.py` has no `wireguard_interface_up`/`down` DISPATCH
entries yet. This means the real chain to a live Ryzen tunnel is, in
order: (1) install `wireguard-tools` (sudo, zero network/interface
change by itself); (2) redeploy the updated appliance-agent code via the
established release-installer pipeline (sudo, the same repair-install
process already used for LPR/PPE and Face Access); (3) set
`ANYAICAM_WIREGUARD_ENABLED=true` in the agent's own config; (4) stand up
a REAL (non-mock) cloud gateway with a real UDP endpoint reachable from
the internet -- a separate, its-own-authorization infrastructure step
this pass deliberately did not take; only then does a real `wg-quick up`
do anything meaningful. Step (1) is the first of these that needs sudo/
root on Ryzen at all, and is this pass's own stopping point -- see the
coordinator's own report for the exact command.
