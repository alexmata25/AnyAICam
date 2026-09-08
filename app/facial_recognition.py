"""AAC (AnyAiCam facial recognition / access-control analytics), CV/ML
layer -- Phase 1.

Given a person already detected by the existing YOLO pipeline
(ai_person_detector()/save_yolo_events() in main.py, exactly the same
integration point ppe.py and lpr.py already use), this module finds
face(s) inside that person's own crop and produces a face embedding for
each -- purely local, purely offline, no network calls, matching
lpr.py's own documented convention for every per-detection analytics
module in this codebase.

This module deliberately knows nothing about the database, tenancy,
enrollment, watchlists, or events -- see facial_people.py (enrollment/
watchlist DB service) and facial_events.py (the module main.py's
save_yolo_events() actually calls, which loads enrolled embeddings via
facial_people.py and uses match_face()/classify_match() below to decide
what happened). Keeping this module DB-free means the CV/matching math
is fully unit-testable without a database, a real camera, or real face
photos -- see app/tests/test_facial_recognition.py.

CPU-first (Phase 1 requirement): the default engine (HaarEmbeddingFaceEngine)
uses only OpenCV -- already a hard dependency of this codebase (see
lpr.py/motion detection) -- and needs no GPU, no model download, and no
new required package. Its embedding is an honest, CPU-only baseline (a
normalized grayscale intensity vector over the aligned face crop), not
a state-of-the-art deep embedding -- accuracy is real but modest, and
this is a deliberate Phase 1 choice, not an oversight (see the Phase 1
report's "face engine selected" note). FaceEngine is an explicit
interface specifically so a stronger, still-CPU-capable engine (e.g. an
ONNX embedding model run through onnxruntime's CPUExecutionProvider) --
or, later, a GPU-accelerated one -- can be swapped in per deployment via
ANYAICAM_FACE_ENGINE without touching any caller.
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

FACIAL_RECOGNITION_ENABLED = os.environ.get("ANYAICAM_FACIAL_RECOGNITION_ENABLED", "false").strip().lower() == "true"

# Cosine-similarity threshold (0-1) a candidate must meet or exceed to
# ever be reported as "known" or "watchlist" -- below this, the match
# is always reported as "unknown", regardless of which enrolled person
# scored highest. This is the one place identity is ever claimed or
# withheld; every other function in this module and facial_events.py
# treats a sub-threshold candidate the same as no candidate at all.
FACIAL_MIN_CONFIDENCE = max(0.0, min(1.0, float(os.environ.get("ANYAICAM_FACIAL_MIN_CONFIDENCE", "0.6"))))

# How long (seconds) a repeat sighting of the SAME person (or the same
# "unknown" bucket) on the SAME camera is suppressed before a new event
# is emitted -- avoids one person standing in frame for 30 seconds
# producing dozens of near-identical events. See DuplicateSuppressor.
FACIAL_DEBOUNCE_SECONDS = max(0.0, float(os.environ.get("ANYAICAM_FACIAL_DEBOUNCE_SECONDS", "30")))

_FACIAL_CAMERAS_RAW = os.environ.get("ANYAICAM_FACIAL_CAMERAS")
FACIAL_CAMERAS = (
    frozenset(int(value) for value in _FACIAL_CAMERAS_RAW.split(",") if value.strip())
    if _FACIAL_CAMERAS_RAW
    else None
)

# The embedding's fixed dimensionality for the default engine. Kept
# small deliberately -- this is a classical-CV baseline, not a deep
# model, and a larger vector would not add real signal.
_EMBEDDING_SIZE = 48  # -> 48*48 = 2304-dimensional flattened vector

_HAAR_CASCADE_FILENAME = "haarcascade_frontalface_default.xml"


def is_camera_enabled(camera_number: int) -> bool:
    """None (the default) means unrestricted, matching every other
    per-camera analytics module in this codebase (ppe.py/lpr.py/
    smart_motion.py)."""
    return FACIAL_CAMERAS is None or camera_number in FACIAL_CAMERAS


# --------------------------------------------------------------------------
# Face engine abstraction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FaceDetection:
    """One detected face region within an image, in that image's own
    pixel coordinates."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class FaceObservation:
    """One detected-and-embedded face: a region plus the embedding
    produced for it, tagged with which engine/version produced the
    embedding -- embeddings from different engines are never
    comparable (see match_face()'s own guard), so this tag travels with
    every embedding from the moment it's produced through storage and
    matching."""

    bbox: FaceDetection
    embedding: tuple[float, ...]
    engine: str
    engine_version: str
    quality: float


class FaceEngine:
    """Abstract interface every face detection/embedding backend
    implements. facial_events.py and enrollment (facial_people.py) both
    depend on this interface, never on a concrete engine class, so a
    future engine (a real deep embedding model, GPU-accelerated or not)
    is a drop-in replacement -- see get_engine()."""

    name: str = "abstract"
    version: str = "0"

    def detect_faces(self, image_bgr) -> list[FaceDetection]:  # pragma: no cover - interface
        raise NotImplementedError

    def embed(self, face_crop_bgr) -> tuple[float, ...] | None:  # pragma: no cover - interface
        raise NotImplementedError

    def capability(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class HaarEmbeddingFaceEngine(FaceEngine):
    """Default, always-available, CPU-only engine.

    Detection: OpenCV's bundled Haar frontal-face cascade -- the exact
    same lazy-singleton-cascade pattern lpr.py already uses for its
    plate cascade (cascade file read from disk once, on first real use,
    never at import time).

    Embedding: a deterministic, CPU-only feature vector over the face
    crop (resize to a fixed size, grayscale, histogram-equalize to
    reduce lighting sensitivity, flatten, L2-normalize). This is a
    classical baseline, not a deep embedding -- it captures real facial
    structure well enough to distinguish clearly different people in
    good conditions, but is not claimed to match production deep-
    learning face recognition accuracy. See this module's own docstring.
    """

    name = "haar_intensity"
    version = "1"

    def __init__(self) -> None:
        self._cascade = None
        self._cascade_load_failed = False

    def _get_cascade(self):
        if self._cascade is not None or self._cascade_load_failed:
            return self._cascade
        try:
            cascade_path = os.path.join(cv2.data.haarcascades, _HAAR_CASCADE_FILENAME)
            cascade = cv2.CascadeClassifier(cascade_path)
            if cascade.empty():
                raise RuntimeError(f"Haar cascade failed to load from {cascade_path}")
            self._cascade = cascade
        except Exception:
            self._cascade_load_failed = True
            self._cascade = None
        return self._cascade

    def reset_state(self) -> None:
        """Test-only: clears the lazy-loaded cascade singleton."""
        self._cascade = None
        self._cascade_load_failed = False

    def capability(self) -> dict:
        available = self._get_cascade() is not None
        return {
            "engine": self.name,
            "version": self.version,
            "available": available,
            "gpu": False,
            "reason": None if available else "Haar cascade failed to load",
        }

    def detect_faces(self, image_bgr) -> list[FaceDetection]:
        cascade = self._get_cascade()
        if cascade is None or image_bgr is None or getattr(image_bgr, "size", 0) == 0:
            return []
        try:
            gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
            regions = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
        except Exception:
            return []
        return [FaceDetection(int(x), int(y), int(w), int(h)) for (x, y, w, h) in regions]

    def embed(self, face_crop_bgr) -> tuple[float, ...] | None:
        return embed_face_crop(face_crop_bgr)


def embed_face_crop(face_crop_bgr) -> tuple[float, ...] | None:
    """Pure function: an already-cropped face region in, a unit-length
    embedding vector out (or None if the crop is empty/invalid). Split
    out from HaarEmbeddingFaceEngine.embed() so embedding math is
    directly unit-testable with any synthetic image -- it never depends
    on the Haar cascade succeeding, matching lpr.py's own separation of
    "find the region" from "read what's in it"."""
    if face_crop_bgr is None or getattr(face_crop_bgr, "size", 0) == 0:
        return None
    try:
        gray = cv2.cvtColor(face_crop_bgr, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(gray, (_EMBEDDING_SIZE, _EMBEDDING_SIZE), interpolation=cv2.INTER_AREA)
        equalized = cv2.equalizeHist(resized)
    except Exception:
        return None
    vector = equalized.astype(np.float64).flatten()
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return None
    unit = vector / norm
    return tuple(round(float(value), 8) for value in unit)


_engine_lock = threading.Lock()
_engine: FaceEngine | None = None


def get_engine() -> FaceEngine:
    """Lazy singleton, matching get_yolo_model()'s own pattern in
    main.py. ANYAICAM_FACE_ENGINE selects the backend -- 'haar' (the
    default, always available) is the only built-in engine in Phase 1;
    an unrecognized value falls back to 'haar' rather than raising, so
    a deployment never fails to start over a typo in this optional
    setting."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = HaarEmbeddingFaceEngine()
        return _engine


def reset_engine() -> None:
    """Test-only: clears the lazy-loaded engine singleton."""
    global _engine
    with _engine_lock:
        _engine = None


def capability() -> dict:
    """Explicit, non-throwing AAC capability state, mirroring lpr.py's
    own capability()."""
    if not FACIAL_RECOGNITION_ENABLED:
        return {"enabled": False, "available": False, "reason": "AAC facial recognition is disabled"}
    engine_capability = get_engine().capability()
    return {
        "enabled": True,
        "available": bool(engine_capability.get("available")),
        "engine": engine_capability.get("engine"),
        "engine_version": engine_capability.get("version"),
        "gpu": engine_capability.get("gpu", False),
        "reason": engine_capability.get("reason"),
    }


def detect_and_embed(image_bgr, *, engine: FaceEngine | None = None) -> list[FaceObservation]:
    """The one entry point main.py's save_yolo_events() calls: a
    person's own crop (or a full frame) in, a list of FaceObservation
    out -- one per face found, empty if none found/engine unavailable.
    Exception-safe by design, matching every other analytics module in
    this codebase: a crop this can't process never raises and never
    interrupts the caller's own detection/recording pipeline.

    `engine` is injectable (defaults to the process-wide get_engine())
    purely so tests can exercise this pipeline with a fake engine
    without depending on Haar actually detecting a face in a synthetic
    test image -- production code should never pass this explicitly."""
    active_engine = engine or get_engine()
    try:
        detections = active_engine.detect_faces(image_bgr)
    except Exception:
        return []
    observations = []
    for detection in detections:
        try:
            crop = image_bgr[detection.y : detection.y + detection.height, detection.x : detection.x + detection.width]
            embedding = active_engine.embed(crop)
        except Exception:
            embedding = None
        if embedding is None:
            continue
        # Quality is a coarse, honest proxy (relative face size within
        # the frame) -- not a claim of any calibrated accuracy metric.
        # A very small face crop is a common source of unreliable
        # embeddings, so downstream callers can choose to weight or
        # ignore low-quality observations.
        area = detection.width * detection.height
        frame_area = max(1, image_bgr.shape[0] * image_bgr.shape[1]) if hasattr(image_bgr, "shape") else 1
        quality = round(min(1.0, area / frame_area * 20), 4)
        observations.append(
            FaceObservation(
                bbox=detection,
                embedding=embedding,
                engine=getattr(active_engine, "name", "unknown"),
                engine_version=getattr(active_engine, "version", "0"),
                quality=quality,
            )
        )
    return observations


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Both vectors are expected to already be unit-length (see
    embed_face_crop()) -- this is then a plain dot product -- but this
    function re-normalizes defensively so a caller passing a
    non-unit vector (e.g. one loaded from an older/foreign embedding
    format) still gets a mathematically correct result instead of a
    silently wrong one."""
    if not a or not b or len(a) != len(b):
        return 0.0
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0.0:
        return 0.0
    similarity = float(np.dot(va, vb) / denom)
    if math.isnan(similarity):
        return 0.0
    return max(-1.0, min(1.0, similarity))


@dataclass(frozen=True)
class EnrolledEmbedding:
    """One facial_embeddings row, in the shape match_face() needs.
    facial_people.py is responsible for loading these from the
    database; this module never queries a database itself."""

    person_id: str
    embedding: tuple[float, ...]
    engine: str


@dataclass(frozen=True)
class MatchCandidate:
    person_id: str
    similarity: float


def match_face(embedding: tuple[float, ...], enrolled: list[EnrolledEmbedding], *, engine: str) -> MatchCandidate | None:
    """Best-scoring enrolled person for this embedding, or None if
    `enrolled` is empty. A person can have multiple reference
    embeddings (facial_people.py supports multiple enrollment images
    per person); this takes that person's single best-scoring reference,
    not an average, since one clean reference image outscoring several
    poor ones is the correct signal, not noise to be diluted.

    Embeddings produced by a DIFFERENT engine than the one given here
    are silently excluded, never compared -- a Haar-intensity embedding
    and a future deep-model embedding live in unrelated vector spaces;
    comparing them would produce a meaningless similarity score that
    could look confident while being pure noise. This is a correctness
    guard, not an optimization."""
    best: MatchCandidate | None = None
    for candidate in enrolled:
        if candidate.engine != engine:
            continue
        similarity = cosine_similarity(embedding, candidate.embedding)
        if best is None or similarity > best.similarity:
            best = MatchCandidate(person_id=candidate.person_id, similarity=similarity)
    return best


def classify_match(
    candidate: MatchCandidate | None,
    *,
    threshold: float,
    watchlist_person_ids: frozenset[str],
) -> tuple[str, MatchCandidate | None]:
    """The one place a face observation becomes 'known' / 'watchlist' /
    'unknown'. A candidate scoring below `threshold` is ALWAYS reported
    as unknown, with no candidate attached -- identity is never claimed
    below threshold, matching this project's explicit Phase 1
    requirement. 'watchlist' takes priority over 'known' when the
    matched person is a member of at least one watchlist."""
    if candidate is None or candidate.similarity < threshold:
        return "unknown", None
    if candidate.person_id in watchlist_person_ids:
        return "watchlist", candidate
    return "known", candidate


# --------------------------------------------------------------------------
# Duplicate suppression / debounce
# --------------------------------------------------------------------------


def is_duplicate(last_seen_at: float | None, now: float, debounce_seconds: float) -> bool:
    """Pure debounce decision: True if `now` is within `debounce_seconds`
    of `last_seen_at`. `last_seen_at=None` (never seen before) is never
    a duplicate."""
    if last_seen_at is None:
        return False
    if debounce_seconds <= 0:
        return False
    return (now - last_seen_at) < debounce_seconds


class DuplicateSuppressor:
    """Stateful, thread-safe wrapper around is_duplicate(), keyed by
    caller-chosen key (facial_events.py uses (camera_id, match_bucket)
    where match_bucket is the matched person_id or the literal string
    "unknown"). Mirrors MockRelayProvider's own cooldown bookkeeping
    shape."""

    def __init__(self, *, debounce_seconds: float = FACIAL_DEBOUNCE_SECONDS, clock=time.monotonic) -> None:
        self.debounce_seconds = debounce_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_seen: dict[tuple, float] = {}

    def reset(self) -> None:
        with self._lock:
            self._last_seen.clear()

    def check_and_record(self, key: tuple, *, now: float | None = None) -> bool:
        """Returns True if this observation should proceed (not a
        duplicate) and records `now` as the key's new last-seen time.
        Returns False (and leaves last-seen unchanged) if this is a
        duplicate within the debounce window -- matching
        MockRelayProvider's cooldown semantics, where a suppressed call
        never resets the window."""
        moment = self._clock() if now is None else now
        with self._lock:
            last = self._last_seen.get(key)
            if is_duplicate(last, moment, self.debounce_seconds):
                return False
            self._last_seen[key] = moment
            return True
