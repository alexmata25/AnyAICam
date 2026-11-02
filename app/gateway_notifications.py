"""Bridge existing Gateway Basic Motion alerts into the local customer inbox.

Reuses recipient permissions, deduplication and read-state tables. No cloud
synchronization or external delivery is required for this inbox.
"""
import os


def publish_local_motion(alert: dict) -> int:
    from gateway_edition import current_edition
    if current_edition() == "full_vms" or os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge") not in {"edge", "combined"}:
        return 0
    if alert.get("event_type") != "motion" or type(alert.get("camera")) is not int:
        return 0
    event_id = alert.get("event_id") or alert.get("id")
    if not event_id:
        return 0
    from partner_db import connection
    from vms_capacity import camera_licensed
    from notification_engine import event_dedupe_key, fanout_appliance_event
    with connection() as db:
        camera = db.execute("SELECT id,customer_id,site_id,appliance_id FROM cameras WHERE camera_number=? AND COALESCE(status,'')!='removed'", (alert["camera"],)).fetchone()
        numbers = [row["camera_number"] for row in db.execute("SELECT camera_number FROM cameras WHERE camera_number IS NOT NULL AND COALESCE(status,'')!='removed' ORDER BY camera_number").fetchall()]
    if not camera or not camera_licensed(alert["camera"], numbers):
        return 0
    event = {**alert, 'id': str(event_id), 'camera_id': camera['id'],
             'dedupe_key': event_dedupe_key(camera['id'], str(event_id))}
    return fanout_appliance_event(dict(camera), event, local_only=True)
