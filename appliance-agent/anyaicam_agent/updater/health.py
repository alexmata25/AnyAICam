"""RDM-2 (device-side integration, Group 2F): the real health_check
implementation for UpdateStateMachine.

health_check() is called exactly once, synchronously, by
resume_if_pending() during startup -- BEFORE heartbeat/camera-sync/
command-polling ever run (see service.py's resolve_update_state()).
Per the existing, unchanged Group 6 contract (state_machine.py):
health_check=None is treated as unconditionally unhealthy, and any
exception health_check() raises is caught by the caller and treated as
"not healthy". This module does not rely on that catch-all as its own
safety net -- every failure path below returns False explicitly, and
make_health_check()'s returned callable is wrapped in one final
defensive try/except so it can NEVER raise, full stop.

"Healthy" requires BOTH:
  1. Minimal local self-checks -- essential filesystem/state
     accessibility only. Deliberately NOT disk-space percentage
     thresholds or any other policy-heavy judgment call (out of scope
     for this group) -- just "can this device still read/write its own
     state directory at all", which is where pending_validation.json,
     update_history.db, credential.json, and offline_queue.db all live.
  2. A successful AUTHENTICATED cloud probe: GET /api/appliance/commands
     -- already used every normal poll cycle (service.py's
     poll_commands()), read-only, no new cloud-side work needed for this
     group. If the device cannot prove portal/auth connectivity within
     the bounded retry budget below, this returns False and the
     existing fail-closed rollback behavior stands -- matching this
     project's established fail-closed philosophy elsewhere (verify.py,
     source.py, has_unresolved_activation()'s OSError propagation). A
     broken update that silently breaks portal.py/auth headers is
     exactly the kind of regression this check exists to catch.

Retry budget: a hard TOTAL wall-clock cap of ~5 seconds, at most 2
attempts. Each attempt's OWN timeout is explicitly recomputed from
whatever remains of that total budget (capped at a short per-attempt
maximum), not a flat constant reused on every attempt regardless of
elapsed time -- a flat per-attempt value would only ever bound how long
a SINGLE attempt can take, never the actual TOTAL wall-clock time this
function can consume, since the deadline check alone only decides
whether a next attempt starts at all, not how long that next attempt is
then allowed to run for. PortalClient's normal 20-second default
timeout must never be allowed to govern this startup probe --
resume_if_pending() (and therefore this call) blocks the entire agent's
startup, so an unbounded probe here would stall heartbeat/camera-sync/
command-polling right along with it.
"""

from pathlib import Path
from time import time as _default_now

_TOTAL_BUDGET_SECONDS = 5.0
_MAX_ATTEMPTS = 2
_PER_ATTEMPT_TIMEOUT_SECONDS = 2.0


def _local_checks_pass(config) -> bool:
    """Minimal, non-policy essential filesystem/state accessibility
    check: can this device still read/write its own state directory?
    Not a disk-space threshold, not a camera-level check -- deliberately
    out of scope for this group."""
    try:
        state_dir = Path(config.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".health_check_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _cloud_probe_succeeds(client, now) -> bool:
    deadline = now() + _TOTAL_BUDGET_SECONDS
    original_timeout = client.timeout
    try:
        attempts = 0
        while attempts < _MAX_ATTEMPTS:
            remaining = deadline - now()
            if remaining <= 0:
                break
            attempts += 1
            # Explicitly capped to whatever remains of the TOTAL budget,
            # not a flat constant reused on every attempt -- a slow
            # first attempt must shorten how long a second attempt is
            # then allowed to run for, or the real total could exceed
            # _TOTAL_BUDGET_SECONDS even with each attempt individually
            # staying under _PER_ATTEMPT_TIMEOUT_SECONDS.
            client.timeout = min(_PER_ATTEMPT_TIMEOUT_SECONDS, remaining)
            try:
                client.request("GET", "/api/appliance/commands")
                return True
            except Exception:  # noqa: BLE001 -- PortalError, timeout, or anything else: try again if budget allows
                continue
        return False
    finally:
        client.timeout = original_timeout


def make_health_check(config, client, now=_default_now):
    """Returns a health_check callable for UpdateStateMachine, bound to
    this specific config/client. `now` is injectable (defaults to
    time.time) purely for deterministic testing of the retry/timeout
    budget -- production callers never need to pass it."""

    def health_check() -> bool:
        try:
            if not _local_checks_pass(config):
                return False
            return _cloud_probe_succeeds(client, now)
        except Exception:  # noqa: BLE001 -- final defensive net: this callable must NEVER raise
            return False

    return health_check
