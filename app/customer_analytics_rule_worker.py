"""Edge execution for the tenant-safe customer_analytics_rules table
(2026-09-21) -- the real evaluator this session's own runtime-path
trace (docs/intrusion-line-crossing-customer-ui-gap.md) documented as
missing. Deliberately its own module, not folded into main.py's
people_counting_worker() or people_counting.py: per explicit
instruction, this feature stays semantically separate from People
Counting even though it reuses analytics_rules_engine.py's generic
tracker/geometry primitives -- people_counting.py's own PeopleCounter
(a single-line-only tracker) is a completely different, untouched
implementation, and this module shares no state with it.

Reuses, rather than reinvents:

- analytics_rules_engine.py's update_tracker()/evaluate_rules() (ported
  2026-09-21, unmodified, from the divergent, unmerged
  analytics-rules-foundation-20260821 branch) for the actual per-track
  geometry/dwell/crossing state machine -- see that module's own
  docstring for the full history and for why "one lingering person" is
  already handled there (a dwell-timer fires an intrusion event at
  most once per continuous dwell; a line-crossing event only fires on
  an actual confirmed side flip, with a small refire floor against
  boundary jitter) rather than needing a second debounce layer here.
- recording_uploader._camera_identity()'s existing camera_number ->
  camera_id cache -- the same one people_counting_worker()/lpr/ppe
  already read for their own per-camera config -- to resolve which
  camera_id's rules to load, so this module adds no new cloud
  round-trip of its own.
- main.py's detect_objects_frame() (the exact same YOLO call every
  other detector already makes, under the same ai_inference_semaphore
  -- see people_counting_worker()'s own 2026-09-22 comment on why that
  lock is mandatory) and append_analytics_event()/linked_recording_
  for()/AnalyticsEventModel. These are all deferred (function-local)
  imports of `main`: this module is imported BY main.py at task-
  startup time (see main.py's own startup block), so importing `main`
  at THIS module's top level would be circular -- main.py has not
  finished defining itself yet at the point it reaches that import.

Rules are read from the LOCAL customer_analytics_rules table -- the
exact same tenant-scoped schema the cloud customer portal writes to
(customer_analytics_rules.py), mirrored onto this appliance by
edge_camera_sync.py's own analytics-rules reconciliation (see that
module's own docstring for the cloud -> appliance delivery path) --
never the legacy, non-tenant-safe analytics_rules.json file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from urllib.parse import quote

import analytics_rules_engine
import recording_uploader
from partner_db import connection

logger = logging.getLogger("anyaicam.customer_analytics_rule_worker")

# Appliance-wide master switch, mirroring PEOPLE_COUNTING_ENABLED's own
# established shape exactly -- a separate per-camera check (does this
# camera have any enabled rule at all) still gates actual work below.
CUSTOMER_ANALYTICS_RULES_ENABLED = os.environ.get("CUSTOMER_ANALYTICS_RULES_ENABLED", "false").strip().lower() == "true"
# Faster than the ordinary 5s AI_DETECTION_INTERVAL_SECONDS, same
# reasoning as PEOPLE_COUNTING_INTERVAL_SECONDS: reliable line/zone
# tracking needs closer-spaced samples than plain presence detection.
CUSTOMER_ANALYTICS_RULE_INTERVAL_SECONDS = max(0.5, float(os.environ.get("CUSTOMER_ANALYTICS_RULE_INTERVAL_SECONDS", "1.5")))


def load_rules_for_camera(camera_id: str) -> list[dict]:
    """Loads this camera's enabled rules from the LOCAL mirror table,
    translated into analytics_rules_engine's own AnalyticsRuleModel-
    shaped rule dict (analytic_type/direction/geometry/confidence_
    threshold) -- the exact shape evaluate_rules() already expects,
    since that module was ported unmodified from a branch built
    against the legacy rule shape and still speaks it natively (see
    that module's own docstring). A disabled row is excluded here
    rather than trusted to evaluate_rules()'s own enabled check, since
    edge_camera_sync.py's reconciliation only ever mirrors enabled=1
    rows down in the first place -- this is a second, defensive layer,
    not the only one."""
    with connection() as db:
        raw_rules = db.execute(
            "SELECT id,rule_type,name,direction,geometry_json,enabled FROM customer_analytics_rules WHERE camera_id=?",
            (camera_id,),
        ).fetchall()
    rules = []
    for row in raw_rules:
        if not row["enabled"]:
            continue
        try:
            geometry = json.loads(row["geometry_json"])
        except (TypeError, ValueError):
            continue
        rules.append({
            "id": row["id"],
            "analytic_type": row["rule_type"],
            "name": row["name"],
            "direction": row["direction"] or "both",
            "confidence_threshold": 0.0,
            "enabled": True,
            "geometry": geometry,
        })
    return rules


def event_type_for(analytic_type: str) -> str:
    """A flat "line_crossing" (never suffixed by direction) -- this is
    not a new value invented here: main.py's own Investigate/analytics
    filter dropdowns (#investigation-type, #analytics-type) already
    ship a "Line crossing" -> value="line_crossing" option (confirmed
    by grep -- it was already there, simply unreachable because nothing
    ever produced this event_type before this worker existed).
    Deliberately distinct from People Counting's own people_counting_
    in/out (which encodes direction into event_type) -- the two
    features share tracking/geometry primitives but must never be
    conflated in Events/Investigate, per explicit instruction. This
    event's own `direction` field (set separately by the caller)
    already carries inbound/outbound -- encoding it a second time into
    event_type would only reintroduce the exact ambiguity avoided by
    keeping this value flat and matching the pre-existing dropdown
    option exactly."""
    return "line_crossing" if analytic_type == "line_crossing" else "intrusion"


def save_rule_event_thumbnail(camera_number: int, frame, now: datetime, tag: str) -> str | None:
    """Duplicated (not imported), same as every other deliberate small
    duplication in this codebase -- mirrors people_counting_worker()'s
    own identical one-frame-per-cycle thumbnail block exactly (2026-
    09-22's fix there: this event type must not be the one analytics
    event with no thumbnail at all)."""
    from main import AI_THUMBNAILS_FOLDER
    import cv2

    try:
        day_folder = AI_THUMBNAILS_FOLDER / now.strftime("%Y-%m-%d")
        day_folder.mkdir(parents=True, exist_ok=True)
        thumbnail_filename = f"camera{camera_number}_{now.strftime('%H-%M-%S')}_{tag}_{uuid.uuid4().hex[:12]}.jpg"
        thumbnail_path = day_folder / thumbnail_filename
        if cv2.imwrite(str(thumbnail_path), frame):
            return f"/recordings/media/ai/{now.strftime('%Y-%m-%d')}/{quote(thumbnail_filename)}"
    except Exception as error:
        logger.warning("customer_analytics_rule_worker.thumbnail_save_failed camera=%s error=%s", camera_number, error)
    return None


def persist_rule_event(camera_number: int, fired: dict, now: datetime, thumbnail_url: str | None) -> dict:
    """Builds and appends one real AnalyticsEventModel row, carrying
    the correct camera/timestamp/rule metadata and linking to the
    short Event-mode recording exactly the way every other analytics
    event type in this codebase already does (linked_recording_for()),
    so it appears in Events/Investigate through the SAME existing
    read path (analytics_events()/append_analytics_event()) -- no
    Investigate-page-specific code was needed for this to render
    correctly. Returns the persisted record dict (used by tests and
    for logging; the caller doesn't otherwise need it)."""
    from main import AnalyticsEventModel, append_analytics_event, linked_recording_for

    analytic_type = fired["analytic_type"]
    direction = fired.get("direction")
    label = "Line Crossing" if analytic_type == "line_crossing" else "Intrusion Zone"
    record = AnalyticsEventModel(
        camera=camera_number,
        site="home",
        rule_name=f"{label} ({fired.get('zone_name') or 'unnamed rule'})",
        event_type=event_type_for(analytic_type),
        direction=direction,
        confidence=float(fired.get("confidence") or 0.0),
        thumbnail=thumbnail_url,
        linked_recording=linked_recording_for(camera_number, now),
        mock=False,
    ).model_dump(mode="json")
    record["rule_id"] = fired["rule_id"]
    record["track_id"] = fired.get("track_id")
    append_analytics_event(record)
    return record


async def customer_analytics_rule_worker(camera_number: int) -> None:
    """One task per camera, spawned only when CUSTOMER_ANALYTICS_RULES_
    ENABLED -- within that, this specific camera only does anything
    once it resolves to a real camera_id AND that camera_id has at
    least one enabled rule; otherwise it idles harmlessly, re-checking
    every cycle so a rule saved/edited/deleted through the customer
    portal and synced down by edge_camera_sync.py takes effect without
    an appliance restart, exactly like people_counting_worker()'s own
    live-rule-reload behavior."""
    from main import ai_inference_semaphore, detect_objects_frame

    while True:
        try:
            identity = recording_uploader._camera_identity(camera_number)
            camera_id = identity.get("camera_id") if identity else None
            rules = load_rules_for_camera(camera_id) if camera_id else []
            if rules:
                async with ai_inference_semaphore:
                    result = await asyncio.to_thread(detect_objects_frame, camera_number)
                if result.get("ok"):
                    frame = result.get("frame")
                    raw_detections = result.get("detections", [])
                    tracked = analytics_rules_engine.update_tracker(camera_number, raw_detections)
                    if frame is not None:
                        frame_height, frame_width = frame.shape[0], frame.shape[1]
                        fired = analytics_rules_engine.evaluate_rules(
                            camera_number, tracked, rules, frame_width, frame_height, now=time.monotonic()
                        )
                        if fired:
                            now = datetime.now()
                            thumbnail_url = save_rule_event_thumbnail(camera_number, frame, now, "analytics_rule")
                            for event in fired:
                                persist_rule_event(camera_number, event, now, thumbnail_url)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("customer_analytics_rule_worker.cycle_failed camera=%s error=%s", camera_number, error)
        await asyncio.sleep(CUSTOMER_ANALYTICS_RULE_INTERVAL_SECONDS)
