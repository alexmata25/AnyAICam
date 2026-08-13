"""Phase 2 (docs/AI_HANDOFF.md §8): small rolling per-camera live-segment manifest.

Records "segment available" notifications from appliances so a future phase
(frontend wiring, §8 implementation phase 6) can build a servable playlist
from the last few segment keys per camera. This module never touches media
bytes -- only tiny JSON metadata (a segment's S3 key and sequence number).

Loading/creating the backing file is deliberately lazy (deferred to first
record_segment()/manifest_for() call, not __init__) so that simply importing
this module -- which happens whenever appliance_cloud.py is imported, and so
whenever main.py is imported -- never touches the filesystem on its own.
"""

import json
import time
from pathlib import Path
from threading import Lock

MAX_SEGMENTS_PER_CAMERA = 5  # mirrors the local HLS list size already used in start_live_stream()


class LiveManifestStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = Lock()
        self._state: dict | None = None

    def _ensure_loaded(self) -> None:
        if self._state is not None:
            return
        try:
            self._state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._state = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def record_segment(self, camera_id: str, segment_key: str, sequence: int | None = None) -> dict:
        with self._lock:
            self._ensure_loaded()
            entry = self._state.setdefault(camera_id, {"segments": [], "updated_at": None})
            entry["segments"] = [item for item in entry["segments"] if item["key"] != segment_key]
            entry["segments"].append({"key": segment_key, "sequence": sequence, "received_at": time.time()})
            entry["segments"] = entry["segments"][-MAX_SEGMENTS_PER_CAMERA:]
            entry["updated_at"] = time.time()
            self._save()
            return dict(entry)

    def manifest_for(self, camera_id: str) -> dict:
        with self._lock:
            self._ensure_loaded()
            return dict(self._state.get(camera_id, {"segments": [], "updated_at": None}))
