"""Reusable camera/IP-device compatibility evaluation engine.

Extends the existing camera discovery pipeline (appliance-agent's
discovery.scan() -> POST /api/appliance/{cloud_id}/scan-jobs/{job_id} ->
app.appliance_cloud.secure_scan_results()) with a compatibility verdict for
each discovered device. This module does not change how discovery itself
probes the network -- see discovery.py, which is untouched.

This module is deliberately pure: no network I/O, no database access, no
FastAPI/HTTP dependency, stdlib only. That is what makes it reusable by a
future caller that never ran live discovery at all -- e.g. a customer-facing
pre-purchase compatibility checker (explicitly not built here) that would
supply capability facts from a spec sheet instead of a live probe. Given the
same capability facts, this module always returns the same verdict,
regardless of the source of those facts.

Decision model
---------------
AnyAiCam's *only* currently-supported video transport is RTSP --
app/main.py's camera_url()/start_live_stream()/start_recording() all build
and consume an rtsp:// URL; there is no other ingest path today. ONVIF is
how discovery finds and identifies a device -- a preferred/supporting
capability, not itself a video transport AnyAiCam consumes at runtime.

Every capability fact is tri-state: True (confirmed present), False
(confirmed absent -- a check actually ran and the capability was not
there), or None (unknown / not confirmed). NOT_SUPPORTED requires
*affirmative* evidence of incompatibility -- "we could not prove support"
must never be silently promoted to "we proved it is unsupported". NOT_SUPPORTED
is only reachable when rtsp_supported is False (positively checked, absent)
AND onvif_supported is also False (positively checked, absent): both
signals were actually probed and neither indicates any usable capability.
In every other case where rtsp_supported is not True, the result is
PARTIALLY_SUPPORTED, never NOT_SUPPORTED -- either the capability is
genuinely unconfirmed, or there is a countervailing signal (a confirmed
ONVIF device almost always also supports RTSP, so a False RTSP probe
against it is more likely a transient/port issue than genuine
incompatibility).

Transport (wired vs. Wi-Fi) is passthrough-only. ONVIF/IP discovery cannot
reliably reveal physical connection medium, so this module never infers it
from any other field -- it only ever echoes back whatever the caller
supplied, defaulting to "unknown". Transport never participates in the
status/reason decision: identical capabilities always produce an identical
verdict regardless of transport.

Nothing in this module's input or output contract includes a credential,
password, or RTSP URL -- only booleans, short identification strings, and
a transport label. There is structurally nothing sensitive for this module
to log or return.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

APPROVED = "APPROVED"
PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
NOT_SUPPORTED = "NOT_SUPPORTED"

_KNOWN_TRANSPORTS = {"wired", "wifi"}
_UNKNOWN_TEXT_VALUES = {"", "unknown"}

_REASON_MESSAGES = {
    "rtsp_confirmed": "RTSP video transport was reachable and responded.",
    "rtsp_unconfirmed": "RTSP support could not be confirmed; it was not verified as available or unavailable.",
    "rtsp_unsupported": "RTSP was checked and did not respond -- the device's only currently-supported video transport is unavailable.",
    "onvif_confirmed": "ONVIF discovery/identification was confirmed.",
    "onvif_unconfirmed": "ONVIF support could not be confirmed.",
    "onvif_unsupported": "ONVIF was checked and the device did not respond to it.",
    "no_supported_video_transport": "No currently-supported video transport (RTSP) was confirmed available, and no other capability offsets that.",
    "manufacturer_unknown": "Manufacturer could not be identified from discovery data.",
    "model_unknown": "Model could not be identified from discovery data.",
    "evaluation_error": "Compatibility could not be evaluated for this device.",
}


def _tri_state(value: Any) -> bool | None:
    """Coerce arbitrary input defensively to True/False/None. Never raises --
    a malformed capability fact from any caller must not crash evaluation
    for that device, or any other device in the same batch. Only a literal
    bool is ever treated as a confirmed signal; anything else (missing,
    wrong type, unexpected value) is treated as unknown rather than guessed."""
    if isinstance(value, bool):
        return value
    return None


def _normalized_text(value: Any) -> str:
    if not isinstance(value, str):
        return "Unknown"
    stripped = value.strip()
    if stripped.lower() in _UNKNOWN_TEXT_VALUES:
        return "Unknown"
    return stripped


def _normalized_transport(value: Any) -> str:
    """Passthrough only -- never inferred. Defaults to "unknown" for
    anything that isn't literally "wired" or "wifi" (case-insensitive)."""
    if isinstance(value, str) and value.strip().lower() in _KNOWN_TRANSPORTS:
        return value.strip().lower()
    return "unknown"


@dataclass(frozen=True, slots=True)
class CompatibilityReason:
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def _reason(code: str) -> CompatibilityReason:
    return CompatibilityReason(code=code, message=_REASON_MESSAGES[code])


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    status: str
    reasons: tuple[CompatibilityReason, ...]
    manufacturer: str
    model: str
    transport: str

    def as_dict(self) -> dict:
        return {
            "compatibility_status": self.status,
            "compatibility_reasons": [reason.as_dict() for reason in self.reasons],
            "manufacturer": self.manufacturer,
            "model": self.model,
            "transport": self.transport,
        }


def evaluate_camera_compatibility(capabilities: dict) -> CompatibilityResult:
    """Evaluate a single device's discovered/reported capabilities and
    return one compatibility verdict with machine-readable reasons.

    `capabilities` is a plain dict (deliberately not required to be any
    specific type -- this is what keeps the function usable by any caller,
    present or future):
      - manufacturer: str | None
      - model: str | None
      - onvif_supported: bool | None  (None = unknown/not confirmed)
      - rtsp_supported: bool | None   (None = unknown/not confirmed)
      - transport: "wired" | "wifi" | None  (passthrough only, never inferred)

    Never raises -- malformed or missing fields are treated as unknown.
    """
    if not isinstance(capabilities, dict):
        capabilities = {}

    rtsp = _tri_state(capabilities.get("rtsp_supported"))
    onvif = _tri_state(capabilities.get("onvif_supported"))
    manufacturer = _normalized_text(capabilities.get("manufacturer"))
    model = _normalized_text(capabilities.get("model"))
    transport = _normalized_transport(capabilities.get("transport"))

    reasons: list[CompatibilityReason] = []

    if rtsp is True:
        reasons.append(_reason("rtsp_confirmed"))
        if onvif is True:
            reasons.append(_reason("onvif_confirmed"))
            status = APPROVED
        elif onvif is False:
            reasons.append(_reason("onvif_unsupported"))
            status = PARTIALLY_SUPPORTED
        else:
            reasons.append(_reason("onvif_unconfirmed"))
            status = PARTIALLY_SUPPORTED
    elif rtsp is False:
        reasons.append(_reason("rtsp_unsupported"))
        if onvif is True:
            # Benefit of the doubt: a confirmed-ONVIF device almost always
            # also supports RTSP; a False RTSP probe here is more likely a
            # transient/port issue than genuine incompatibility.
            reasons.append(_reason("onvif_confirmed"))
            status = PARTIALLY_SUPPORTED
        elif onvif is False:
            # Affirmative evidence on both signals -- this is the only
            # branch that reaches NOT_SUPPORTED.
            reasons.append(_reason("onvif_unsupported"))
            reasons.append(_reason("no_supported_video_transport"))
            status = NOT_SUPPORTED
        else:
            # RTSP positively absent, but ONVIF is merely unconfirmed (not
            # positively absent) -- not enough affirmative evidence to call
            # this NOT_SUPPORTED.
            reasons.append(_reason("onvif_unconfirmed"))
            status = PARTIALLY_SUPPORTED
    else:
        # rtsp is None: unknown/unconfirmed can never by itself produce
        # NOT_SUPPORTED, regardless of onvif's state.
        reasons.append(_reason("rtsp_unconfirmed"))
        if onvif is True:
            reasons.append(_reason("onvif_confirmed"))
        elif onvif is False:
            reasons.append(_reason("onvif_unsupported"))
        else:
            reasons.append(_reason("onvif_unconfirmed"))
        status = PARTIALLY_SUPPORTED

    if manufacturer == "Unknown":
        reasons.append(_reason("manufacturer_unknown"))
    if model == "Unknown":
        reasons.append(_reason("model_unknown"))

    return CompatibilityResult(
        status=status,
        reasons=tuple(reasons),
        manufacturer=manufacturer,
        model=model,
        transport=transport,
    )


def evaluate_scan_results(results: list) -> list[dict]:
    """Batch-evaluate a discovery scan's results (the shape produced by
    appliance-agent's discovery.scan(): dicts with rtsp_support/
    onvif_support/manufacturer/model keys), attaching compatibility fields
    onto each item.

    This is the adapter for today's discovery wire format -- it translates
    discovery.py's rtsp_support/onvif_support field names into the engine's
    rtsp_supported/onvif_supported contract and calls
    evaluate_camera_compatibility(). A future caller that already has data
    in the engine's own field names (e.g. a pre-purchase checker) should
    call evaluate_camera_compatibility() directly instead of this adapter.

    One malformed/unexpected item can never affect the evaluation of any
    other: each item is evaluated independently, and an unexpected
    exception for one item falls back to a safe PARTIALLY_SUPPORTED verdict
    for that single item (never NOT_SUPPORTED -- an evaluation failure is
    not affirmative evidence of incompatibility) rather than raising and
    losing the rest of the batch.
    """
    if not isinstance(results, list):
        return []
    enriched: list[dict] = []
    for item in results:
        base = dict(item) if isinstance(item, dict) else {}
        try:
            capabilities = {
                "manufacturer": base.get("manufacturer"),
                "model": base.get("model"),
                "onvif_supported": base.get("onvif_support"),
                "rtsp_supported": base.get("rtsp_support"),
                "transport": base.get("transport"),
            }
            result = evaluate_camera_compatibility(capabilities)
            base["compatibility_status"] = result.status
            base["compatibility_reasons"] = [reason.as_dict() for reason in result.reasons]
            base["transport"] = result.transport
        except Exception:
            # Defensive: this branch should be unreachable given
            # evaluate_camera_compatibility()'s own defensiveness, but a
            # single bad record must never break the batch.
            base.setdefault("compatibility_status", PARTIALLY_SUPPORTED)
            base.setdefault("compatibility_reasons", [_reason("evaluation_error").as_dict()])
            base.setdefault("transport", "unknown")
        enriched.append(base)
    return enriched
