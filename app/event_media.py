"""Readiness depends on clip metadata, never on preview availability."""
from datetime import datetime, timezone

def media_state(has_clip,timestamp,now=None):
    if has_clip: return 'ready'
    try:
        detected=datetime.fromisoformat(str(timestamp).replace('Z','+00:00'))
        if detected.tzinfo is None: detected=detected.replace(tzinfo=timezone.utc)
        age=((now or datetime.now(timezone.utc))-detected).total_seconds()
    except (ValueError,TypeError,OverflowError): return 'unavailable'
    return 'processing' if 0<=age<120 else 'unavailable'
