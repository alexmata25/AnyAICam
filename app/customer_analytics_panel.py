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
from datetime import datetime, timezone
from typing import Any

# analytic_key (as stored in analytics_subscriptions, matching the exact
# addon checkbox values Wizard A's "What you're buying" step already
# submits) -> (display label, the detection_events.event_type values that
# belong to it). Smart Motion covers plain motion plus the person/vehicle
# classifications a Smart Motion subscription already includes -- it is
# not just raw pixel-difference motion (see punch-list item 5's note on
# analytics-vs-motion triggers).
ANALYTIC_LABELS: dict[str, tuple[str, tuple[str, ...]]] = {
    # Event types are the values actually STORED in detection_events
    # (2026-09-24 fix): the edge stores the specific vehicle class YOLO
    # produced (car/truck/bus/...), Smart Motion's own "smart_motion"
    # events, one "people_counting_in"/"people_counting_out" row per line
    # crossing, and LPR reads as "plate" -- the previous generic values
    # ("vehicle", "people_counting", "lpr") are only notification-side
    # aliases, so these summaries were always empty for real data.
    # Customer-drawn intrusion zones / alert lines run on Smart Motion's
    # person/vehicle detections and require its entitlement
    # (customer_analytics_rule_worker.camera_rules_entitled), so their
    # events belong to its summary.
    "smart_motion": ("Smart Motion", ("motion", "smart_motion", "person", "vehicle", "car", "truck", "bus", "motorcycle", "bicycle",
                                      "intrusion", "line_crossing")),
    "people_counting": ("People Counting", ("people_counting", "people_counting_in", "people_counting_out")),
    "lpr": ("LPR", ("lpr", "plate")),
    "ppe": ("PPE", ("ppe",)),
    # AAC (facial recognition / access-control), Phase 1: gated per-camera
    # through this exact same camera_analytics_entitlements mechanism as
    # every other analytic -- see facial_events.py's record_facial_events(),
    # which refuses to run at all for a camera without an active
    # 'facial_recognition' row here, and facial_people.py/facial_recognition_ui.py
    # for enrollment/watchlist management.
    "facial_recognition": ("Facial Recognition", ("facial_recognition",)),
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


class LicenseLimitExceeded(Exception):
    """Raised by assign_entitlement() when the site has not purchased
    enough camera-seats of this analytic (analytics_subscriptions.
    licensed_quantity) to cover one more camera. Billing authority always
    wins: direct DB state must never let a camera entitlement exist beyond
    what was actually purchased."""


def licensed_quantity_exceeded(*, licensed_quantity: int, currently_entitled_count: int, already_entitled: bool) -> bool:
    """Pure limit check: True if assigning would exceed the purchased
    seat count. already_entitled=True (re-assigning/refreshing a camera
    that already has this analytic) never counts against the limit --
    only a genuinely NEW camera-seat does."""
    if already_entitled:
        return False
    return currently_entitled_count >= licensed_quantity


# 2026-09-16: maps each real, appliance-enforceable analytic_key to the
# matching cameras.<analytic>_enabled column (db_migrations.py) that
# recording_uploader._refresh_camera_map() exposes to the appliance and
# lpr.is_camera_enabled()/ppe.is_camera_enabled()/smart_motion's own
# caller in main.py/people_counting_worker() actually read. Before this,
# assign_entitlement()/remove_entitlement() only ever wrote
# camera_analytics_entitlements -- correct for the customer-facing Live
# View pill and this table's own RDM UI, but never reached the
# appliance at all, so toggling an entitlement off through RDM never
# actually stopped the feature from running on Ryzen. Only the 4 keys
# below have a real per-camera appliance enforcement column; any other
# key in ANALYTIC_LABELS is written to camera_analytics_entitlements
# only, exactly as before -- unaffected by this change.
_APPLIANCE_ENFORCEMENT_COLUMN: dict[str, str] = {
    "smart_motion": "smart_motion_enabled",
    "people_counting": "people_counting_enabled",
    "lpr": "lpr_enabled",
    "ppe": "ppe_enabled",
}


def assign_entitlement(db, camera_id: str, analytic_key: str, *, now: str) -> None:
    """Turns on one analytic for exactly one camera -- never touches any
    other camera, even another one at the same site or owned by the same
    customer. Idempotent: assigning an already-active entitlement just
    refreshes updated_at.

    Enforces the billing/subscription record as the authority on capacity
    (see LicenseLimitExceeded): a site that purchased 2 LPR licenses can
    never have LPR enabled on a 3rd camera through this function, no
    matter how it's called."""
    if analytic_key not in ANALYTIC_LABELS:
        raise ValueError(f"Unknown analytic_key: {analytic_key!r}")

    camera = db.execute("SELECT site_id, customer_id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    if not camera:
        raise LookupError(f"Camera not found: {camera_id!r}")
    site_id, customer_id = camera["site_id"], camera["customer_id"]

    subscription = db.execute(
        "SELECT licensed_quantity FROM analytics_subscriptions "
        "WHERE customer_id=? AND (site_id=? OR site_id IS NULL) AND analytic_key=? AND status!='cancelled' "
        "ORDER BY site_id IS NULL LIMIT 1",
        (customer_id, site_id, analytic_key),
    ).fetchone()
    licensed_quantity = subscription["licensed_quantity"] if subscription else 0

    already_entitled = db.execute(
        "SELECT 1 FROM camera_analytics_entitlements WHERE camera_id=? AND analytic_key=? AND status='active'",
        (camera_id, analytic_key),
    ).fetchone() is not None

    currently_entitled_count = db.execute(
        "SELECT COUNT(*) AS n FROM camera_analytics_entitlements e JOIN cameras c ON c.id=e.camera_id "
        "WHERE c.site_id=? AND e.analytic_key=? AND e.status='active'",
        (site_id, analytic_key),
    ).fetchone()["n"]

    if licensed_quantity_exceeded(
        licensed_quantity=licensed_quantity,
        currently_entitled_count=currently_entitled_count,
        already_entitled=already_entitled,
    ):
        if not licensed_quantity:
            raise LicenseLimitExceeded(
                f"{ANALYTIC_LABELS[analytic_key][0]} is not part of your plan for this site yet. "
                f"Add it from My subscription, then assign it to this camera."
            )
        raise LicenseLimitExceeded(
            f"{ANALYTIC_LABELS[analytic_key][0]} is licensed for {licensed_quantity} camera(s) at this site; "
            f"that limit is already in use."
        )

    db.execute(
        "INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) "
        "VALUES(?,?,'active',?,?) "
        "ON CONFLICT(camera_id,analytic_key) DO UPDATE SET status='active',updated_at=excluded.updated_at",
        (camera_id, analytic_key, now, now),
    )
    column = _APPLIANCE_ENFORCEMENT_COLUMN.get(analytic_key)
    if column:
        db.execute(f"UPDATE cameras SET {column}=1 WHERE id=?", (camera_id,))


def remove_entitlement(db, camera_id: str, analytic_key: str, *, now: str) -> None:
    """Turns off one analytic for exactly one camera. Soft-remove (status
    set to 'cancelled', row kept) rather than DELETE, matching
    analytics_subscriptions' own convention elsewhere in this codebase --
    enabled_analytics() already treats status='cancelled' as not enabled."""
    db.execute(
        "UPDATE camera_analytics_entitlements SET status='cancelled',updated_at=? WHERE camera_id=? AND analytic_key=?",
        (now, camera_id, analytic_key),
    )
    column = _APPLIANCE_ENFORCEMENT_COLUMN.get(analytic_key)
    if column:
        db.execute(f"UPDATE cameras SET {column}=0 WHERE id=?", (camera_id,))


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
    "facial_recognition": {
        "description": "Recognize enrolled people at this camera, flag watchlist matches, "
                        "and log unknown faces.",
        "benefits": [
            "Match against enrolled employees/known visitors",
            "Watchlist alerts for flagged individuals",
            "Full facial-event history with confidence and thumbnails",
        ],
    },
}


def event_types_for_analytic(analytic_key: str) -> tuple[str, ...]:
    entry = ANALYTIC_LABELS.get(analytic_key)
    if not entry:
        raise ValueError(f"Unknown analytic_key: {analytic_key!r}")
    return entry[1]


def _parse_detections(raw: Any) -> dict:
    """detections_json as a dict. The cloud stores the synced payload's
    `detections` LIST (e.g. PPE's [{hard_hat_present, safety_vest_present}]
    or facial recognition's [{match_state, matched_person_name, ...}]);
    its first dict entry is the one summary-relevant record. Anything
    else unparseable is an empty dict, never an error."""
    parsed = raw
    if isinstance(raw, str):
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(parsed, list):
        parsed = next((item for item in parsed if isinstance(item, dict)), {})
    return parsed if isinstance(parsed, dict) else {}


def epoch_ms(raw_timestamp) -> int | None:
    """Naive-UTC event timestamp -> epoch milliseconds (same conversion as
    main._naive_utc_timestamp_to_epoch_ms), so the browser formats it in
    the viewer's own timezone instead of showing a raw ISO string."""
    try:
        dt = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


EVENT_TYPE_LABELS = {
    "motion": "Motion", "smart_motion": "Smart Motion", "ppe": "PPE", "plate": "License plate", "lpr": "License plate",
    "people_counting": "People count", "people_counting_in": "Entry", "people_counting_out": "Exit",
    "facial_recognition": "Face", "aac_voice_call": "Voice call", "line_crossing": "Line crossing",
}


EVENT_TYPE_MESSAGES = {
    "people_counting_in": "Person entered", "people_counting_out": "Person left",
    "plate": "License plate read", "lpr": "License plate read",
}


def event_type_message(event_type) -> str:
    """Default one-line alert message for an event type ('PPE detected',
    'Person entered')."""
    key = str(event_type or "").strip().lower()
    return EVENT_TYPE_MESSAGES.get(key) or f"{event_type_label(key)} detected"


def event_type_label(event_type) -> str:
    """Customer-facing short name for a stored event type (2026-09-25):
    'PPE' not 'Ppe', 'Entry' not 'People Counting In'. Unknown types fall
    back to the previous title-casing."""
    key = str(event_type or "").strip().lower()
    return EVENT_TYPE_LABELS.get(key) or (key.replace("_", " ").title() if key else "Event")


MOTION_EVENT_TYPES = frozenset({"motion", "smart_motion"})


def real_confidence(event_type, value):
    """A detection confidence only when it is one: a 0-1 probability from an
    object/face/plate model. Motion rows store a raw motion score (e.g.
    25.4) and PPE/people-counting rows a placeholder 0.0 -- neither may be
    shown to a customer as a percentage."""
    if value is None or str(event_type or "").lower() in MOTION_EVENT_TYPES:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 1 else None


def event_ref(item: dict) -> dict:
    """What the Live analytics panel needs to link a result to its event
    (2026-09-25): the stored detection_events id, a viewer-local-ready
    time, and whether the event already has a clip / thumbnail
    (detection_event_media, joined by the summary route). Existing data
    only -- nothing new is stored."""
    return {
        "event_id": item.get("id"),
        "timestamp_ms": epoch_ms(item.get("event_timestamp")),
        "has_clip": bool(item.get("has_clip")),
        "has_thumbnail": bool(item.get("has_thumbnail")),
    }


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
            **event_ref(item),
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
            "event_type": item.get("event_type"),
            **event_ref(item),
        }
        for item in events[:10]
    ]
    # Line-crossing events are one row per crossing, direction in the
    # event_type (people_counting_in/_out). Entries/exits are counted over
    # the rows the caller fetched (the most recent crossings), and the
    # estimated occupancy is entries - exits over that same window, never
    # negative. A legacy aggregate row that carries its own entries/exits
    # in detections_json still wins, unchanged.
    crossings_in = sum(1 for item in events if item.get("event_type") == "people_counting_in")
    crossings_out = sum(1 for item in events if item.get("event_type") == "people_counting_out")
    if crossings_in or crossings_out:
        entries, exits = crossings_in, crossings_out
        latest_count = max(entries - exits, 0)
    else:
        entries, exits = latest_detections.get("entries"), latest_detections.get("exits")
        latest_count = latest.get("object_count")
    return {
        "latest_count": latest_count,
        "entries": entries,
        "exits": exits,
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
            "status": _ppe_status(_parse_detections(item.get("detections_json"))),
            "timestamp": item.get("event_timestamp"),
            "confidence": None,  # PPE rows carry a placeholder 0.0
            **event_ref(item),
        }
        for item in events[:10]
    ]
    return {
        "latest_status": _ppe_status(latest_detections),
        "latest_timestamp": latest.get("event_timestamp"),
        "recent": recent,
    }


def _ppe_status(detections: dict) -> str | None:
    """'compliant' / 'violation' from the edge's own decision fields
    (hard_hat_present / safety_vest_present -- see analytics_sync.py's PPE
    special case), an explicit 'status' if a record carries one, else
    None. Previously a record without 'status' was always reported as a
    violation, including compliant ones."""
    if detections.get("status"):
        return str(detections["status"])
    if "hard_hat_present" in detections or "safety_vest_present" in detections:
        compliant = bool(detections.get("hard_hat_present")) and bool(detections.get("safety_vest_present"))
        return "compliant" if compliant else "violation"
    return None


def summarize_smart_motion(events: list[dict]) -> dict:
    recent = [
        {
            "event_type": item.get("event_type"),
            "timestamp": item.get("event_timestamp"),
            "confidence": real_confidence(item.get("event_type"), item.get("confidence")),
            "thumbnail": _parse_detections(item.get("detections_json")).get("thumbnail"),
            **event_ref(item),
        }
        for item in events[:10]
    ]
    return {
        "latest_timestamp": events[0].get("event_timestamp") if events else None,
        "recent": recent,
    }


def summarize_facial_recognition(events: list[dict]) -> dict:
    """events: detection_events rows for event_type='facial_recognition'.
    The richer per-match fields (matched person, watchlist state,
    per-face thumbnail) live in facial_events.py's own dedicated table,
    not detections_json -- this summary is only the same lightweight
    "most recent activity" shape every other analytic pill shows;
    the full Facial Events/Match detail screens (facial_recognition_ui.py)
    are the real, complete view."""
    if not events:
        return {"latest_state": None, "latest_timestamp": None, "recent": []}
    latest = events[0]
    latest_detections = _parse_detections(latest.get("detections_json"))
    recent = [
        {
            "state": _parse_detections(item.get("detections_json")).get("match_state"),
            "person": _parse_detections(item.get("detections_json")).get("matched_person_name"),
            "timestamp": item.get("event_timestamp"),
            "confidence": item.get("confidence"),
            **event_ref(item),
        }
        for item in events[:10]
    ]
    return {
        "latest_state": latest_detections.get("match_state"),
        "latest_timestamp": latest.get("event_timestamp"),
        "recent": recent,
    }


SUMMARIZERS = {
    "smart_motion": summarize_smart_motion,
    "people_counting": summarize_people_counting,
    "lpr": summarize_lpr,
    "ppe": summarize_ppe,
    "facial_recognition": summarize_facial_recognition,
}


def summarize(analytic_key: str, events: list[dict]) -> dict:
    summarizer = SUMMARIZERS.get(analytic_key)
    if not summarizer:
        raise ValueError(f"Unknown analytic_key: {analytic_key!r}")
    result = summarizer(events)
    result["latest_timestamp_ms"] = epoch_ms(result.get("latest_timestamp")) if result.get("latest_timestamp") else None
    return result
