"""Short-lived Local Gateway remote access lease.

A lease is accepted only in the process receiving its nonce-bound response.
Nothing is restored from disk after restart. Local camera capacity is unrelated.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import threading
import time

LEASE_TYPE = "anyaicam.local_gateway_remote_access"
LEASE_VERSION = 1
MAX_VALIDITY = timedelta(hours=24)
_CLOCK_TOLERANCE = timedelta(seconds=2)
_lock = threading.RLock()
_lease = None
_nonce = None
_wall_anchor = None
_mono_anchor = None


def verify_lease(document, public_keys, *, identity, nonce, now=None):
    """Verify signature, exact identity, fresh request nonce, and strict deadline."""
    import appliance_identity
    import appliance_entitlements
    if not isinstance(document, dict) or not public_keys:
        return "missing"
    if not isinstance(nonce, str) or len(nonce) < 16:
        return "bad_nonce"
    bound = appliance_entitlements._bound_identity(identity)
    if bound is None:
        return "not_activated"
    body = {key: value for key, value in document.items() if key != "signature"}
    try:
        appliance_identity.verify_signed_body(body, document.get("signature") or {}, public_keys=public_keys)
    except Exception:
        return "bad_signature"
    if body.get("type") != LEASE_TYPE or type(body.get("version")) is not int or body["version"] != LEASE_VERSION:
        return "unsupported"
    if any(body.get(key) != value for key, value in bound.items()):
        return "identity_mismatch"
    if body.get("request_nonce") != nonce:
        return "nonce_mismatch"
    issued = appliance_entitlements._parse_time(body.get("issued_at"))
    expires = appliance_entitlements._parse_time(body.get("expires_at"))
    now = now or datetime.now(timezone.utc)
    if issued is None or expires is None or expires <= issued or expires - issued > MAX_VALIDITY:
        return "bad_time"
    if issued > now or now >= expires:
        return "expired_or_future"
    return None


def _verify_trusted(document, nonce, *, now=None, for_new_grant=True):
    import appliance_entitlements
    return appliance_entitlements._verify_trusted(
        document, verify_lease, purpose="features", for_new_grant=for_new_grant,
        identity=appliance_entitlements._local_identity(), nonce=nonce, now=now)


def clear():
    global _lease, _nonce, _wall_anchor, _mono_anchor
    with _lock:
        _lease = _nonce = _wall_anchor = _mono_anchor = None


def accept(document, nonce, *, now=None, monotonic=None):
    """Accept a nonce-bound verified response. Invalid responses revoke locally."""
    global _lease, _nonce, _wall_anchor, _mono_anchor
    wall = now or datetime.now(timezone.utc)
    tick = time.monotonic() if monotonic is None else monotonic
    with _lock:
        try:
            reason = _verify_trusted(document, nonce, now=wall, for_new_grant=True)
            if reason:
                clear()
                return False
            # During one boot a rollback cannot be hidden by a new response.
            if _wall_anchor is not None:
                expected = _wall_anchor + timedelta(seconds=tick - _mono_anchor)
                if tick < _mono_anchor or wall < expected - _CLOCK_TOLERANCE:
                    clear()
                    return False
                wall = max(wall, expected)
            _lease, _nonce = deepcopy(document), nonce
            _wall_anchor, _mono_anchor = wall, tick
            return True
        except Exception:
            clear()
            return False


def allowed(*, now=None, monotonic=None):
    """Recheck expiry, signer trust and the monotonic clock on each request."""
    with _lock:
        if _lease is None:
            return False
        wall = now or datetime.now(timezone.utc)
        tick = time.monotonic() if monotonic is None else monotonic
        try:
            if tick < _mono_anchor:
                clear()
                return False
            expected = _wall_anchor + timedelta(seconds=tick - _mono_anchor)
            if wall < expected - _CLOCK_TOLERANCE:
                clear()
                return False
            effective = max(wall, expected)
            if _verify_trusted(_lease, _nonce, now=effective, for_new_grant=False):
                clear()
                return False
            return True
        except Exception:
            clear()
            return False
