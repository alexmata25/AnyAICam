"""The permanent, non-secret Claim ID of an installation.

A one-way value derived from the installer UUID. It names an installation and
proves nothing: linking also needs the one-time code from that PC and a signed-in
customer owner. It must never reveal the UUID, because claim/begin accepts any
unclaimed UUID with a caller-chosen device secret. Deliberately not the Linux
label code (a secret, 12 characters) nor an admin cloud_id (AIC-).

The app/claim_id.py module is the same derivation; the two share
test vectors because the agent does not import the app.
"""
from __future__ import annotations

import hashlib
import re

CLAIM_ID_PREFIX = 'anyaicam-claim-id-v1:'
ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'  # Crockford base32, as LABEL_ALPHABET
CLAIM_ID_PATTERN = re.compile(r'^CID-[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}$')
_LOOKALIKES = str.maketrans({'O': '0', 'I': '1', 'L': '1'})
_DEVICE_UUID = re.compile(r'^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.IGNORECASE)
_DEVICE_PATH = re.compile(r'(/api/appliance(?:-legacy)?/)([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?=[/?#\s"]|$)', re.IGNORECASE)


def _check(values: list[int]) -> str:
    return ALPHABET[sum(values) % 32]


def claim_id_for(device_id: str) -> str:
    digest = hashlib.sha256((CLAIM_ID_PREFIX + device_id.strip().lower()).encode('ascii')).digest()
    number = int.from_bytes(digest[:10], 'big') >> 5  # the first 75 bits
    values = [(number >> (5 * (14 - index))) & 31 for index in range(15)]
    text = ''.join(ALPHABET[value] for value in values) + _check(values)
    return 'CID-' + '-'.join(text[start:start + 4] for start in range(0, 16, 4))


def normalize_claim_id(text: str) -> str | None:
    """The canonical form of a Claim ID as a person may type it, or None when
    it is malformed or its check character is wrong."""
    compact = re.sub(r'[\s-]', '', str(text or '')).upper().translate(_LOOKALIKES)
    if len(compact) == 19 and compact.startswith('C1D'):  # the look-alike rule also rewrites the prefix
        compact = compact[3:]
    if len(compact) != 16 or any(ch not in ALPHABET for ch in compact):
        return None
    if _check([ALPHABET.index(ch) for ch in compact[:15]]) != compact[15]:
        return None
    return 'CID-' + '-'.join(compact[start:start + 4] for start in range(0, 16, 4))


def display_cloud_id(value):
    """What a log or status line shows for this PC: its Claim ID, never the UUID.
    One-way, so never use the result to call the cloud."""
    return claim_id_for(value) if isinstance(value, str) and _DEVICE_UUID.match(value.strip()) else value


def mask_device_path(text: str) -> str:
    """A log line with the device UUID in an appliance route shown as the Claim ID."""
    return _DEVICE_PATH.sub(lambda match: match.group(1) + claim_id_for(match.group(2)), text)
