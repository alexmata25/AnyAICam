# What stands between here and "ships preinstalled, customer only activates"

Requested alongside the Samsung installation runbook. This is an
assessment based on what this session (and the two blockers already on
record in `docs/blockers-before-universal-release.md`, written before
this session, not re-discovered by it) found -- not a new redesign
proposal, and nothing here has been changed.

## The single biggest blocker: activation requires a terminal session

`anyaicam-setup` (`appliance-agent/anyaicam_agent/setup_wizard.py`) is
the *only* way to activate an appliance, and it is a fully interactive
Python CLI (`input()`/`getpass()` prompts for portal URL, mode, Cloud
ID, activation token, and a discovery-run confirmation) with no
non-interactive or scripted mode of any kind. "Arrives preinstalled,
customer only activates" implies the customer does this step
themselves, without SSH access or a terminal -- today that is not
possible; activation requires someone with a shell on the box. Closing
this gap means either a local web-based activation flow (a captive-
portal-style page reachable from a phone on first boot) or a companion
app that drives the same underlying activation call, and is real
product work, not a config change.

## Already-documented, not yet fixed (found before this session)

Both from `docs/blockers-before-universal-release.md`, directly
relevant to a customer-facing product and worth restating here rather
than assuming they're forgotten:

- **Edge appliances maintain independently-authoritative customer
  passwords.** A customer's login on one appliance can silently diverge
  from the same account on another, or from the cloud-side password, with
  no sync/reconciliation. For a customer who might eventually have more
  than one appliance, or who resets their password anywhere other than
  the one box they happen to be talking to, this is a real, confirmed
  problem, not a hypothetical.
- **Password-reset tokens are logged in plaintext** via `GET` query
  strings, unconditionally, in every container's access log.

## Found this session

- **`GET /ready` never reports true on a plain edge appliance.**
  `startup_self_test()`'s `configuration_valid` check requires AWS
  production settings (region, S3, Secrets Manager, HTTPS enforcement)
  that an edge-role appliance never has by design. Confirmed live on
  two separate disposable EC2 installs -- this is not
  environment-specific. If any future fleet-monitoring or support
  tooling is built to key off `/ready`, it will show every edge
  appliance as permanently not-ready. `GET /health` (liveness) is
  unaffected and is what this session's own validation relied on
  instead. Not fixed in this pass -- it's an `app/main.py` readiness-
  semantics question (should edge role skip AWS-production checks
  entirely?), not an installer concern, and deserves its own scoped
  decision rather than a side-effect fix here.
- **Per-camera cloud eligibility is admin-set, not entitlement-driven.**
  `cameras.cloud_recording_mode` (this session's Phase 1 fix) is
  correctly *enforced*, but nothing yet derives it automatically from
  what a customer actually purchased -- an administrator sets it
  manually per camera today. For self-service "customer buys Cloud
  Motion, camera behavior follows automatically" to work, this needs to
  connect to whatever plan/entitlement record exists once the Cloud
  Motion product itself is built (`docs/cloud-product-architecture-plan.md`).
- **The privileged-watcher mechanism was never actually deployable
  before this session's fixes** (packaging gap, plus the systemd
  start-rate-limit defect found during live validation). Both are now
  fixed and validated end to end on disposable EC2 -- listed here only
  so "was this ever a blocker" has a clear, dated answer: yes, until
  this session; not anymore, pending the same fix reaching Samsung.

## Manufacturing/operational considerations (not defects, but relevant to "at scale")

- **The installer is internet-dependent at install time** (live `apt`,
  Docker's own repo, PyPI for `cryptography`/`cffi`/etc.). Fine for a
  single appliance installed once before shipping; worth a deliberate
  decision for real manufacturing volume -- either accept every unit
  needing internet access during factory imaging, or move to a
  pre-built golden image flashed at scale instead of running the full
  installer per unit.
- **Factory-reset / re-provisioning a returned unit** is now a
  reasonably solid story as of this session's `uninstall.sh --purge-all`
  fix (also removes the `anyaicam` system user, so the box genuinely
  returns to a `clean` install-state -- confirmed by a new automated
  test, not yet by a live re-image). Worth a dedicated live test before
  relying on it operationally.
- **No factory-time non-interactive identity/config seeding path
  exists** beyond what's described above for activation specifically --
  worth deciding, when the time comes, whether any *other* first-boot
  configuration (network, camera pre-discovery, branding) needs a
  similar non-interactive path.

## What is explicitly NOT a blocker, based on this session's findings

- The core install/uninstall/reinstall/repair workflow itself: reviewed
  in full this session, one real defect found and fixed (stale
  privileged-watcher systemd units surviving uninstall), otherwise
  sound and validated live twice.
- Docker/container health, cloud-URL configuration, and the credential-
  mount security model: all directly validated end to end on real
  infrastructure this session.
- Suspend/hibernate: already fixed and confirmed effective for the
  specific incident that caused it (Samsung's own GNOME idle-suspend
  policy).
