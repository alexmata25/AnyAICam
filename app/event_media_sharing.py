"""Which already-captured clip a PPE, Facial Recognition or People Counting
result shows (2026-09-26).

Those results used to reach the cloud with no media at all: PPE and Facial
Recognition are derived from the same person detection, in the same YOLO
scan, as the event that owns that scan's clip (save_yolo_events()), and
People Counting saves its crossing frame locally -- but none of them ever
registered media, so their Analytics workspaces could only say "No preview
available".

The rule, per camera:

- Every event that owns a clip (the YOLO scan's clip-owning class event, or
  a Facial Recognition / People Counting event that had to build its own)
  is registered here with that clip's own window -- compute_clip_window():
  5 s pre-roll + 5 s post-roll around the detection.
- A result REUSES a clip only when that clip's window contains the result's
  own moment, on the same camera. That window is the whole matching
  tolerance; nothing "nearby" counts. When several windows contain the
  moment (possible only where a Facial Recognition / People Counting clip
  and a YOLO clip overlap) the most recently registered wins -- every
  candidate truthfully recorded the moment, so none is the wrong event.
- PPE always has one: it only runs in scans that build a clip, and it is
  linked to that very scan's clip owner.
- Facial Recognition and People Counting build their own clip only when no
  registered clip covers the moment (Facial Recognition is debounced to one
  event per person per 30 s, People Counting fires per crossing, so this is
  bounded); a second result in the same moment then reuses that clip.

The link travels as the result's local ``media_parent_event_id`` (forwarded
by analytics_sync.py as ``parent_local_event_id``); the cloud re-verifies
camera, appliance, tenant, allowed parent type, and that the moment lies in
the parent's registered clip window before copying the parent's media row
(appliance_cloud.py analytics_event_media_shared()). Nothing is re-encoded
or re-uploaded for a reused clip.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

import event_media_outbox

logger = logging.getLogger("anyaicam.event_media_sharing")

# Registered owners are only useful while a new result could still fall in
# their window; keep a little longer than any window for late callers.
KEEP_SECONDS = 120
MAX_OWNERS_PER_CAMERA = 16

PENDING, REGISTERED, FAILED = "pending", "registered", "failed"


class ClipOwners:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_camera: dict[int, list[dict]] = {}
        self._by_id: dict[str, dict] = {}

    def reset(self) -> None:
        with self._lock:
            self._by_camera.clear()
            self._by_id.clear()

    def register(self, camera_number: int, owner_id: str, start: datetime, end: datetime) -> None:
        entry = {"owner_id": owner_id, "camera": camera_number, "start": start, "end": end,
                 "state": PENDING, "children": []}
        with self._lock:
            current = self._by_camera.get(camera_number, []) + [entry]
            fresh = [item for item in current if item["end"] >= end - timedelta(seconds=KEEP_SECONDS)]
            kept = fresh[-MAX_OWNERS_PER_CAMERA:]
            for dropped in (item for item in current if item not in kept):
                # A pending owner stays reachable until its upload reports
                # back (finish()), so its queued results are still delivered.
                if dropped["state"] != PENDING:
                    self._by_id.pop(dropped["owner_id"], None)
            self._by_camera[camera_number] = kept
            self._by_id[owner_id] = entry

    def covering(self, camera_number: int, moment: datetime) -> str | None:
        with self._lock:
            for item in reversed(self._by_camera.get(camera_number, [])):
                if item["start"] <= moment <= item["end"] and item["state"] != FAILED:
                    return item["owner_id"]
        return None

    def attach(self, owner_id: str, child_id: str) -> str:
        """PENDING: queued until the owner's upload finishes. REGISTERED:
        the caller registers the child now. FAILED / unknown owner: the
        caller hands it to deliver_after_failure()."""
        with self._lock:
            entry = self._by_id.get(owner_id)
            if entry is None:
                return FAILED
            if entry["state"] == PENDING:
                entry["children"].append(child_id)
            return entry["state"]

    def finish(self, owner_id: str, registered: bool) -> list[str]:
        with self._lock:
            entry = self._by_id.get(owner_id)
            if entry is None:
                return []
            entry["state"] = REGISTERED if registered else FAILED
            children, entry["children"] = entry["children"], []
            if entry not in self._by_camera.get(entry["camera"], []):
                self._by_id.pop(owner_id, None)  # already pruned from its camera
        return children


owners = ClipOwners()


def link(event: dict, owner_id: str | None) -> None:
    """Mark a local analytics event (before it is appended/synced) as
    reusing owner_id's clip."""
    if owner_id and owner_id != event.get("id"):
        event["media_parent_event_id"] = owner_id


def register_child_now(child_id: str, camera_number: int, owner_id: str) -> bool:
    from event_media_uploader import register_shared_event_media
    try:
        return register_shared_event_media(event_id=child_id, camera_number=camera_number,
                                           parent_local_event_id=owner_id)
    except Exception as error:  # never let one child break the others
        logger.warning("event_media_sharing.register_failed child=%s owner=%s error=%s", child_id, owner_id, error)
        return False


def deliver_after_failure(child_id: str, camera_number: int, owner_id: str) -> None:
    """The owner's upload did not complete. If it is queued for retry (its
    job is still in the outbox), queue the child behind it; otherwise the
    owner will never have media and neither will the child -- say so and
    leave it without media rather than invent any."""
    if not any(job.get("event_id") == owner_id and job.get("kind") != "shared" for job in event_media_outbox.load()):
        logger.info("event_media_sharing.owner_without_media child=%s owner=%s", child_id, owner_id)
        return
    from event_media_uploader import RETRY_SECONDS
    event_media_outbox.put({
        "event_id": child_id, "camera_number": camera_number, "kind": "shared",
        "parent_local_event_id": owner_id,
        "next_attempt_at": (datetime.now(timezone.utc) + timedelta(seconds=RETRY_SECONDS)).isoformat(),
    })


def attach_child(owner_id: str, child_id: str, camera_number: int) -> None:
    """Never blocks the caller (a detection thread or the event loop):
    queued behind a pending owner, or delivered on a background thread."""
    state = owners.attach(owner_id, child_id)
    if state == PENDING:
        return
    deliver = register_child_now if state == REGISTERED else deliver_after_failure
    threading.Thread(target=deliver, args=(child_id, camera_number, owner_id), daemon=True,
                     name=f"event-media-share-{child_id}").start()


def should_build_own_clip(camera_number: int) -> bool:
    """A Facial Recognition / People Counting clip is only worth encoding
    when it can be kept: event media capture/upload is on and, for upload,
    the camera is in Hybrid ('motion') cloud recording -- the same gates
    upload_motion_event_media() applies after the fact."""
    import event_media_uploader
    import recording_uploader
    if not (event_media_uploader.EVENT_MEDIA_CAPTURE_ENABLED or event_media_uploader.EVENT_MEDIA_UPLOAD_ENABLED):
        return False
    if not event_media_uploader.EVENT_MEDIA_UPLOAD_ENABLED:
        return True
    identity = recording_uploader._camera_identity(camera_number)
    return bool(identity and identity.get("cloud_recording_mode") == "motion")


def owner_finished(owner_id: str, camera_number: int, registered: bool) -> None:
    """Blocking -- call from the owner's upload task, after its upload."""
    for child_id in owners.finish(owner_id, registered):
        if registered:
            register_child_now(child_id, camera_number, owner_id)
        else:
            deliver_after_failure(child_id, camera_number, owner_id)
