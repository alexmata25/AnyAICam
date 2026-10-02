"""Readiness depends on clip metadata, never on preview availability."""
from datetime import datetime, timezone

def media_state(has_clip,timestamp,now=None,status=None):
    """'ready' / 'processing' / 'unavailable'. The durable cloud status
    (detection_events.media_status, 2026-10-01) wins over the age guess:
    an event whose appliance is still building or retrying its clip stays
    'processing' however long that takes, and one reported lost is
    'unavailable' at once. Events without a status keep the old guess."""
    if has_clip: return 'ready'
    if status=='pending': return 'processing'
    if status in ('failed','available'): return 'unavailable'  # 'available' without a clip row: never claim one
    try:
        detected=datetime.fromisoformat(str(timestamp).replace('Z','+00:00'))
        if detected.tzinfo is None: detected=detected.replace(tzinfo=timezone.utc)
        age=((now or datetime.now(timezone.utc))-detected).total_seconds()
    except (ValueError,TypeError,OverflowError): return 'unavailable'
    return 'processing' if 0<=age<120 else 'unavailable'
