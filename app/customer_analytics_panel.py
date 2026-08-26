"""Focused Live View's switchable per-camera analytics row.

Entitlements are per-camera, not per-site: analytics_subscriptions (the
existing site/customer-scoped table) remains the billing/commercial
record of what a customer purchased, but camera_analytics_entitlements
(camera_id, analytic_key) is the new, separate table that actually gates
what a given camera shows -- a customer with 10 cameras at one site can
have LPR on exactly 2 of them, People Counting on 4 others, and nothing on
the rest. Nothing in this module or its caller infers "purchased at the
site" into "enabled on every camera at that site" -- see
assign_entitlement()/remove_entitlement() for the only way a camera's row
in that table changes.

Everything here except assign_entitlement()/remove_entitlement() (which
take an explicit db connection, matching camera_mapping.py's established
dependency-light pattern in this codebase) is pure, DB/FastAPI-free logic,
fully unit-testable, for two things app/live_view_page.py's customer-facing
routes call:

1. Which analytics are enabled for one specific camera (see
   ANALYTIC_LABELS for the exact set the UI pill row can ever render, so
   an unrecognized/future entitlement key never produces a broken pill).
2. Formatting the "most recent useful results" panel for whichever
   analytic pill is selected, from raw detection_events rows.
"""
from __future__ import annotations

import json
from typing import Any

# analytic_key (as stored in analytics_subscriptions, matching the exact
# addon checkbox values Wizard A's "What you're buying" step already
# submits) -> (display label, the detection_events.event_type values that
# belong to it). Smart Motion covers plain motion plus the person/vehicle
# classifications a Smart Motion subscription already includes -- it is
# not just raw pixel-difference motion (see punch-list item 5's note on
# analytics-vs-motion triggers).
ANALYTIC_LABELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "smart_motion": ("Smart Motion", ("motion", "person", "vehicle")),
    "people_counting": ("People Counting", ("people_counting",)),
    "lpr": ("LPR", ("lpr",)),
    "ppe": ("PPE", ("ppe",)),
}

# Never show an empty bar: this pin exists so ANALYTIC_LABELS additions
# (a future analytic) don't need a second edit somewhere else.
ANALYTIC_KEYS = tuple(ANALYTIC_LABELS.keys())


def enabled_analytics(entitlement_rows: list[dict]) -> list[str]:
    """entitlement_rows: camera_analytics_entitlements rows (dicts with
    at least 'analytic_key' and 'status') already scoped to ONE specific
    camera_id by the caller's SQL WHERE clause -- never a whole site's or
    customer's rows at once, which is exactly the per-site-not-per-camera
    behavior this table replaces. This function only decides which of
    those *statuses* count as "enabled" and filters to keys the UI
    actually knows how to render (ANALYTIC_LABELS), in a stable,
    deterministic order (not insertion order, which can vary by when each
    entitlement was assigned) so the pill row doesn't reorder itself
    between page loads."""
    active_keys = {
        row["analytic_key"]
        for row in entitlement_rows
        if row.get("status") != "cancelled" and row.get("analytic_key") in ANALYTIC_LABELS
    }
    return [key for key in ANALYTIC_KEYS if key in active_keys]


def analytics_row_state(subscription_rows: list[dict]) -> list[dict]:
    """The Focused Live View's analytics row must never be dead space
    (see the punch-list report): every camera shows all ANALYTIC_KEYS,
    each flagged enabled=True (real results) or enabled=False (an
    upgrade-opportunity card) -- never an empty row for a camera with
    nothing purchased yet."""
    enabled = set(enabled_analytics(subscription_rows))
    return [
        {"key": key, "label": ANALYTIC_LABELS[key][0], "enabled": key in enabled}
        for key in ANALYTIC_KEYS
    ]


def camera_entitlement_rows(db, camera_id: str) -> list[dict]:
    """The one place that reads camera_analytics_entitlements for a
    single camera -- callers (both the live_view_page.py route and this
    module's own tests) get rows already scoped to exactly one camera_id,
    never a whole site's or customer's entitlements at once."""
    rows = db.execute(
        "SELECT analytic_key, status FROM camera_analytics_entitlements WHERE camera_id=?",
        (camera_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def assign_entitlement(db, camera_id: str, analytic_key: str, *, now: str) -> None:
    """Turns on one analytic for exactly one camera -- never touches any
    other camera, even another one at the same site or owned by the same
    customer. Idempotent: assigning an already-active entitlement just
    refreshes updated_at."""
    if analytic_key not in ANALYTIC_LABELS:
        raise ValueError(f"Unknown analytic_key: {analytic_key!r}")
    db.execute(
        "INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) "
        "VALUES(?,?,'active',?,?) "
        "ON CONFLICT(camera_id,analytic_key) DO UPDATE SET status='active',updated_at=excluded.updated_at",
        (camera_id, analytic_key, now, now),
    )


def remove_entitlement(db, camera_id: str, analytic_key: str, *, now: str) -> None:
    """Turns off one analytic for exactly one camera. Soft-remove (status
    set to 'cancelled', row kept) rather than DELETE, matching
    analytics_subscriptions' own convention elsewhere in this codebase --
    enabled_analytics() already treats status='cancelled' as not enabled."""
    db.execute(
        "UPDATE camera_analytics_entitlements SET status='cancelled',updated_at=? WHERE camera_id=? AND analytic_key=?",
        (now, camera_id, analytic_key),
    )


UPGRADE_CARD_CONTENT: dict[str, dict] = {
    "smart_motion": {
        "description": "Smarter event filtering that reduces nuisance clips from trees, "
                        "shadows, and other background noise, and focuses on people, "
                        "vehicles, and relevant movement.",
        "benefits": [
            "Fewer junk motion clips from trees/leaves and lighting changes",
            "Focused on people, vehicles, and relevant movement",
            "Shorter, more useful event review",
        ],
    },
    "people_counting": {
        "description": "Count entries and exits at this camera and track traffic trends "
                        "over time.",
        "benefits": [
            "Count entries/exits automatically",
            "Track traffic trends over time",
            "Useful for business occupancy or activity review",
        ],
    },
    "lpr": {
        "description": "Automatically detect and log license plates seen by this camera.",
        "benefits": [
            "Detect license plates automatically",
            "Search recorded video by plate number",
            "Get alerts for specific vehicles",
            "Best for driveways, gates, and parking areas",
        ],
    },
    "ppe": {
        "description": "Detect personal protective equipment compliance in view of this "
                        "camera.",
        "benefits": [
            "Detect safety gear compliance",
            "Useful for job sites, warehouses, and industrial areas",
        ],
    },
}


def event_types_for_analytic(analytic_key: str) -> tuple[str, ...]:
    entry = ANALYTIC_LABELS.get(analytic_key)
    if not entry:
        raise ValueError(f"Unknown analytic_key: {analytic_key!r}")
    return entry[1]


def _parse_detections(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def summarize_lpr(events: list[dict]) -> dict:
    """events: detection_events rows for event_type='lpr', most recent
    first. Never assumes a specific detections_json shape beyond
    best-effort key lookups -- a camera/model that doesn't populate a
    given field just shows it as unknown, not an error."""
    if not events:
        return {"latest_plate": None, "latest_timestamp": None, "latest_confidence": None, "recent": []}
    latest = events[0]
    latest_detections = _parse_detections(latest.get("detections_json"))
    recent = [
        {
            "plate": _parse_detections(item.get("detections_json")).get("plate"),
            "timestamp": item.get("event_timestamp"),
            "confidence": item.get("confidence"),
        }
        for item in events[:10]
    ]
    return {
        "latest_plate": latest_detections.get("plate"),
        "latest_timestamp": latest.get("event_timestamp"),
        "latest_confidence": latest.get("confidence"),
        "recent": recent,
    }


def summarize_people_counting(events: list[dict]) -> dict:
    if not events:
        return {"latest_count": None, "entries": None, "exits": None, "latest_timestamp": None, "recent": []}
    latest = events[0]
    latest_detections = _parse_detections(latest.get("detections_json"))
    recent = [
        {
            "count": item.get("object_count"),
            "timestamp": item.get("event_timestamp"),
        }
        for item in events[:10]
    ]
    return {
        "latest_count": latest.get("object_count"),
        "entries": latest_detections.get("entries"),
        "exits": latest_detections.get("exits"),
        "latest_timestamp": latest.get("event_timestamp"),
        "recent": recent,
    }


def summarize_ppe(events: list[dict]) -> dict:
    if not events:
        return {"latest_status": None, "latest_timestamp": None, "recent": []}
    latest = events[0]
    latest_detections = _parse_detections(latest.get("detections_json"))
    recent = [
        {
            "status": _parse_detections(item.get("detections_json")).get("status", item.get("event_type")),
            "timestamp": item.get("event_timestamp"),
        }
        for item in events[:10]
    ]
    return {
        "latest_status": latest_detections.get("status", "violation" if latest.get("event_type") == "ppe" else None),
        "latest_timestamp": latest.get("event_timestamp"),
        "recent": recent,
    }


def summarize_smart_motion(events: list[dict]) -> dict:
    recent = [
        {
            "event_type": item.get("event_type"),
            "timestamp": item.get("event_timestamp"),
            "confidence": item.get("confidence"),
            "thumbnail": _parse_detections(item.get("detections_json")).get("thumbnail"),
        }
        for item in events[:10]
    ]
    return {
        "latest_timestamp": events[0].get("event_timestamp") if events else None,
        "recent": recent,
    }


SUMMARIZERS = {
    "smart_motion": summarize_smart_motion,
    "people_counting": summarize_people_counting,
    "lpr": summarize_lpr,
    "ppe": summarize_ppe,
}


def summarize(analytic_key: str, events: list[dict]) -> dict:
    summarizer = SUMMARIZERS.get(analytic_key)
    if not summarizer:
        raise ValueError(f"Unknown analytic_key: {analytic_key!r}")
    return summarizer(events)
