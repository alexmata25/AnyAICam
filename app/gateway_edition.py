"""Edition defaults for the shared VMS; not a feature or entitlement gate.

Selecting an edition never changes recording, retention, analytics or licensing.
Existing installations without a selection retain Full VMS behavior.
"""
import os

EDITION_ENV = "ANYAICAM_GATEWAY_EDITION"
EDITIONS = ("full_vms", "local_gateway", "cloud_gateway")


def current_edition() -> str:
    value = os.environ.get(EDITION_ENV, "").strip().lower()
    if not value:
        return "full_vms"
    if value not in EDITIONS:
        raise ValueError(f"Invalid {EDITION_ENV}: expected one of {', '.join(EDITIONS)}")
    return value


def default_product_mode() -> str:
    """Lowest-priority mode default; cloud configuration can still upgrade it."""
    return {"full_vms": "", "local_gateway": "local", "cloud_gateway": "hybrid"}[current_edition()]
