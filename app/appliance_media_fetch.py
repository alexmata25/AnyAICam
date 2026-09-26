"""Short-lived, HMAC-signed capability tokens for one specific local media
file (2026-09-19) -- the real fix for the "/recordings/* requires a local
edge admin session" finding from this same day's live staging test: a
naive fetch of /recordings/... over the WireGuard tunnel got redirected to
Ryzen's own "Local emergency recovery sign-in" page, and (a real bug,
fixed alongside this) the caller counted that HTML page's HTTP 200 as a
successful video fetch.

This module is the shared crypto both ends use -- the cloud portal mints
a token (it holds the raw per-appliance secret; see appliance_wireguard_
peers.media_fetch_secret), the appliance's own new /api/appliance/
media-fetch route (main.py) verifies it locally, no database round-trip
needed on the appliance side. Deliberately narrow: a token authorizes
fetching exactly one path, for a few seconds, never a directory, never
"this customer/camera" in the abstract -- there is no browsing capability
here at all, unlike the raw StaticFiles /recordings mount this replaces
for the WireGuard path specifically. General/unauthenticated LAN access to
/recordings/* is completely unchanged (still gated by authentication_
middleware exactly as before) -- this module never touches that gate.

The secret itself is a plain shared HMAC key, not a hash: unlike
appliance_credentials.credential_hash (deliberately one-way, since the
cloud only ever needs to verify a bearer value the appliance already
holds), the cloud here needs to actively SIGN outgoing requests, which
requires holding the real key, not a hash of it -- the same reason ANY
symmetric-key scheme needs both sides to hold the same raw value. Real
provisioning of this secret onto a specific real Ryzen appliance is its
own separate, later, explicitly-authorized step -- this module's own
mint()/verify() functions are exercised entirely with synthetic test
secrets until then, exactly the same "build and test the crypto/plumbing
before any real device is enrolled" discipline this whole WireGuard
effort has followed for every prior phase.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

# A network round trip (cloud -> gateway -> appliance) plus normal clock
# skew between the staging host and a real Ryzen box in the field --
# short enough that a leaked/logged URL is useless within seconds, long
# enough that it never expires mid-flight under realistic conditions.
TOKEN_TTL_SECONDS = int(os.environ.get("ANYAICAM_MEDIA_FETCH_TOKEN_TTL_SECONDS", "60"))

# Where the appliance itself keeps its own copy of the shared secret --
# same atomic-write+0600 convention as credential.json/wireguard_identity.json
# (see appliance-agent/anyaicam_agent/config.py), read here only, never
# written by this module (provisioning is a separate, later concern).
MEDIA_FETCH_SECRET_FILE = Path(
    os.environ.get("ANYAICAM_MEDIA_FETCH_SECRET_FILE", "/etc/anyaicam/media_fetch_secret.json")
)

# The minimum plausible size for a real video response -- rejects an
# HTML error/login page outright before any of the more specific checks
# below even run. Deliberately small (this is a floor, not a realistic
# clip-size estimate) so it never rejects a genuinely tiny real clip.
MIN_PLAUSIBLE_VIDEO_BYTES = 1024

# The ISO base media file format ("ftyp" box) every real .mp4 this
# codebase produces starts with, within the first handful of bytes --
# recording_uploader.py/event_media_uploader.py both produce real MP4
# containers, never raw MPEG-TS, for event clips. A cheap, real
# content-sniff that a login page's HTML can never satisfy, independent
# of (and never a replacement for) the token check itself.
_MP4_SIGNATURE = b"ftyp"


def _sign(secret: str, local_relative_path: str, expires: int) -> str:
    message = f"{local_relative_path}:{expires}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def mint(secret: str, local_relative_path: str, *, now: float | None = None) -> tuple[int, str]:
    """Returns (expires_epoch, token) for one exact path -- called only on
    the cloud side, only once real customer/camera/event/media
    authorization (main.py's own _customer_authorized_camera_id() plus the
    detection_event_media join) has already succeeded."""
    expires = int((now if now is not None else time.time()) + TOKEN_TTL_SECONDS)
    return expires, _sign(secret, local_relative_path, expires)


def verify(secret: str, local_relative_path: str, expires: int, token: str, *, now: float | None = None) -> bool:
    """Called only on the appliance side, against the SAME local_relative_
    path the request is actually asking for -- a token minted for one path
    is never valid for another, even before expiry, since the path itself
    is part of the signed message. Uses hmac.compare_digest, never a plain
    ==, for the same timing-attack reason every other credential
    comparison in this codebase already does."""
    if (now if now is not None else time.time()) > expires:
        return False
    return hmac.compare_digest(_sign(secret, local_relative_path, expires), token)


def load_local_secret() -> str | None:
    """The appliance's own read of its half of the shared secret. None
    (never an exception) if this appliance was never provisioned with one
    yet -- every real appliance today, until the separate, later
    enrollment step this module's own docstring describes -- so every
    verify() call downstream simply fails closed, the same "no
    surprise-enabled behavior" default every other opt-in feature in this
    codebase already uses."""
    try:
        data = json.loads(MEDIA_FETCH_SECRET_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("secret")
    return value if isinstance(value, str) and value else None


def response_looks_like_real_video(body: bytes, *, expected_size: int | None = None) -> bool:
    """The real fix for the exact false-positive this module exists
    because of: an HTML login page returned HTTP 200 and was counted as a
    successful video fetch. Never trusts a status code alone -- checks the
    bytes themselves. expected_size, when known (detection_event_media.
    size_bytes), is compared with a tolerance (network/remux differences
    are real and small; an HTML error page is never merely "5% off")."""
    if len(body) < MIN_PLAUSIBLE_VIDEO_BYTES:
        return False
    head = body[:64]
    stripped = head.lstrip()
    if stripped[:1] in (b"<", b"{"):
        return False
    if _MP4_SIGNATURE not in head:
        return False
    if expected_size:
        tolerance = max(MIN_PLAUSIBLE_VIDEO_BYTES, int(expected_size * 0.05))
        if abs(len(body) - expected_size) > tolerance:
            return False
    return True
