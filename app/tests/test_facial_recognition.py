"""AAC CV/matching layer -- Phase 1. Pure logic: embeddings, cosine
similarity, best-match selection, known/unknown/watchlist
classification, and duplicate suppression/debounce. Uses only
synthetic numpy images -- no real face photos, no camera, no relay --
matching this project's "synthetic/test facial images only" testing
requirement.

detect_and_embed()'s own detection step is exercised through an
injected FakeEngine (defined below) rather than the real Haar cascade,
so these tests never depend on a drawn/synthetic image happening to
look enough like a face for Haar to fire -- exactly why
facial_recognition.detect_and_embed() accepts an injectable `engine`
in the first place (see that function's own docstring). The real
default engine's own availability is checked separately, at the bottom
of this file, without depending on real face detection succeeding.
"""

import numpy as np
import pytest

import facial_recognition as fr


def _image(seed: int, size: int = 80):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(size, size, 3), dtype=np.uint8)


# --------------------------------------------------------------- embeddings


def test_embed_face_crop_returns_none_for_empty_crop():
    empty = np.zeros((0, 0, 3), dtype=np.uint8)
    assert fr.embed_face_crop(empty) is None


def test_embed_face_crop_returns_none_for_none_input():
    assert fr.embed_face_crop(None) is None


def test_embed_face_crop_is_unit_length():
    embedding = fr.embed_face_crop(_image(1))
    assert embedding is not None
    norm = float(np.linalg.norm(np.asarray(embedding)))
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_embed_face_crop_is_deterministic():
    image = _image(42)
    first = fr.embed_face_crop(image.copy())
    second = fr.embed_face_crop(image.copy())
    assert first == second


def test_embed_face_crop_distinguishes_different_images():
    a = fr.embed_face_crop(_image(1))
    b = fr.embed_face_crop(_image(2))
    assert a != b
    assert fr.cosine_similarity(a, b) < 0.999


# --------------------------------------------------------------- cosine_similarity


def test_cosine_similarity_identical_vectors_is_one():
    vector = (0.6, 0.8)
    assert fr.cosine_similarity(vector, vector) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert fr.cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)


def test_cosine_similarity_opposite_vectors_is_negative_one():
    assert fr.cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(-1.0)


def test_cosine_similarity_empty_vector_is_zero():
    assert fr.cosine_similarity((), (1.0, 0.0)) == 0.0


def test_cosine_similarity_mismatched_length_is_zero():
    assert fr.cosine_similarity((1.0, 0.0), (1.0, 0.0, 0.0)) == 0.0


# --------------------------------------------------------------- match_face


def test_match_face_with_no_enrolled_embeddings_returns_none():
    assert fr.match_face((1.0, 0.0), [], engine="haar_intensity") is None


def test_match_face_picks_the_best_scoring_person():
    embedding = (1.0, 0.0)
    enrolled = [
        fr.EnrolledEmbedding(person_id="alice", embedding=(1.0, 0.0), engine="haar_intensity"),
        fr.EnrolledEmbedding(person_id="bob", embedding=(0.0, 1.0), engine="haar_intensity"),
    ]
    best = fr.match_face(embedding, enrolled, engine="haar_intensity")
    assert best.person_id == "alice"
    assert best.similarity == pytest.approx(1.0)


def test_match_face_takes_a_persons_best_reference_not_an_average():
    """A person enrolled with one great reference image and one poor
    one must be matched on the great one -- averaging would dilute a
    genuinely strong match with noise from an unrelated angle/lighting
    reference."""
    embedding = (1.0, 0.0)
    enrolled = [
        fr.EnrolledEmbedding(person_id="alice", embedding=(1.0, 0.0), engine="haar_intensity"),
        fr.EnrolledEmbedding(person_id="alice", embedding=(0.0, 1.0), engine="haar_intensity"),
    ]
    best = fr.match_face(embedding, enrolled, engine="haar_intensity")
    assert best.person_id == "alice"
    assert best.similarity == pytest.approx(1.0)


def test_match_face_excludes_embeddings_from_a_different_engine():
    """A Haar-intensity embedding and a (hypothetical) different
    engine's embedding live in unrelated vector spaces -- comparing
    them would be meaningless, so match_face() must never do it."""
    embedding = (1.0, 0.0)
    enrolled = [fr.EnrolledEmbedding(person_id="alice", embedding=(1.0, 0.0), engine="some_future_engine")]
    assert fr.match_face(embedding, enrolled, engine="haar_intensity") is None


# --------------------------------------------------------------- classify_match


def test_classify_match_none_candidate_is_unknown():
    state, candidate = fr.classify_match(None, threshold=0.6, watchlist_person_ids=frozenset())
    assert state == "unknown"
    assert candidate is None


def test_classify_match_below_threshold_is_unknown_never_claims_identity():
    candidate = fr.MatchCandidate(person_id="alice", similarity=0.59)
    state, accepted = fr.classify_match(candidate, threshold=0.6, watchlist_person_ids=frozenset())
    assert state == "unknown"
    assert accepted is None  # identity must never be attached below threshold


def test_classify_match_at_threshold_boundary_is_accepted():
    candidate = fr.MatchCandidate(person_id="alice", similarity=0.6)
    state, accepted = fr.classify_match(candidate, threshold=0.6, watchlist_person_ids=frozenset())
    assert state == "known"
    assert accepted.person_id == "alice"


def test_classify_match_above_threshold_not_on_watchlist_is_known():
    candidate = fr.MatchCandidate(person_id="alice", similarity=0.95)
    state, accepted = fr.classify_match(candidate, threshold=0.6, watchlist_person_ids=frozenset({"bob"}))
    assert state == "known"
    assert accepted.person_id == "alice"


def test_classify_match_above_threshold_on_watchlist_is_watchlist():
    candidate = fr.MatchCandidate(person_id="alice", similarity=0.95)
    state, accepted = fr.classify_match(candidate, threshold=0.6, watchlist_person_ids=frozenset({"alice"}))
    assert state == "watchlist"
    assert accepted.person_id == "alice"


# --------------------------------------------------------------- debounce


def test_is_duplicate_first_sighting_is_never_a_duplicate():
    assert fr.is_duplicate(None, now=100.0, debounce_seconds=30) is False


def test_is_duplicate_within_window_is_a_duplicate():
    assert fr.is_duplicate(100.0, now=110.0, debounce_seconds=30) is True


def test_is_duplicate_exactly_at_window_boundary_is_not_a_duplicate():
    assert fr.is_duplicate(100.0, now=130.0, debounce_seconds=30) is False


def test_is_duplicate_zero_debounce_never_suppresses():
    assert fr.is_duplicate(100.0, now=100.0, debounce_seconds=0) is False


def test_duplicate_suppressor_suppresses_repeat_within_window():
    clock = iter([100.0, 105.0]).__next__
    suppressor = fr.DuplicateSuppressor(debounce_seconds=30, clock=clock)
    assert suppressor.check_and_record(("cam-1", "alice")) is True
    assert suppressor.check_and_record(("cam-1", "alice")) is False


def test_duplicate_suppressor_allows_after_window_elapses():
    clock = iter([100.0, 200.0]).__next__
    suppressor = fr.DuplicateSuppressor(debounce_seconds=30, clock=clock)
    assert suppressor.check_and_record(("cam-1", "alice")) is True
    assert suppressor.check_and_record(("cam-1", "alice")) is True


def test_duplicate_suppressor_keys_are_independent():
    suppressor = fr.DuplicateSuppressor(debounce_seconds=30, clock=lambda: 100.0)
    assert suppressor.check_and_record(("cam-1", "alice")) is True
    assert suppressor.check_and_record(("cam-1", "unknown")) is True
    assert suppressor.check_and_record(("cam-2", "alice")) is True


def test_duplicate_suppressor_reset_clears_state():
    suppressor = fr.DuplicateSuppressor(debounce_seconds=1000, clock=lambda: 100.0)
    suppressor.check_and_record(("cam-1", "alice"))
    suppressor.reset()
    assert suppressor.check_and_record(("cam-1", "alice")) is True


def test_suppressed_call_does_not_reset_the_window():
    times = iter([100.0, 105.0, 110.0])
    suppressor = fr.DuplicateSuppressor(debounce_seconds=30, clock=lambda: next(times))
    assert suppressor.check_and_record(("cam-1", "alice")) is True  # t=100, recorded
    assert suppressor.check_and_record(("cam-1", "alice")) is False  # t=105, suppressed, last-seen stays 100
    assert suppressor.check_and_record(("cam-1", "alice")) is False  # t=110, still within 30s of t=100


# --------------------------------------------------------------- camera scoping


def test_is_camera_enabled_default_is_unrestricted(monkeypatch):
    monkeypatch.setattr(fr, "FACIAL_CAMERAS", None)
    assert fr.is_camera_enabled(1) is True
    assert fr.is_camera_enabled(99) is True


def test_is_camera_enabled_respects_explicit_scope(monkeypatch):
    monkeypatch.setattr(fr, "FACIAL_CAMERAS", frozenset({1, 2}))
    assert fr.is_camera_enabled(1) is True
    assert fr.is_camera_enabled(3) is False


# --------------------------------------------------------------- capability()


def test_capability_reports_disabled_when_flag_is_off(monkeypatch):
    monkeypatch.setattr(fr, "FACIAL_RECOGNITION_ENABLED", False)
    result = fr.capability()
    assert result == {"enabled": False, "available": False, "reason": "AAC facial recognition is disabled"}


def test_capability_reports_engine_details_when_enabled(monkeypatch):
    monkeypatch.setattr(fr, "FACIAL_RECOGNITION_ENABLED", True)
    result = fr.capability()
    assert result["enabled"] is True
    assert "engine" in result and "gpu" in result


# --------------------------------------------------------------- detect_and_embed (injected engine)


class _FakeEngine(fr.FaceEngine):
    name = "fake"
    version = "1"

    def __init__(self, detections):
        self._detections = detections

    def detect_faces(self, image_bgr):
        return self._detections

    def embed(self, face_crop_bgr):
        return fr.embed_face_crop(face_crop_bgr)

    def capability(self):
        return {"engine": self.name, "version": self.version, "available": True, "gpu": False, "reason": None}


class _RaisingEngine(fr.FaceEngine):
    name = "raising"
    version = "1"

    def detect_faces(self, image_bgr):
        raise RuntimeError("boom")

    def embed(self, face_crop_bgr):
        raise RuntimeError("boom")

    def capability(self):
        return {"engine": self.name, "version": self.version, "available": False, "gpu": False, "reason": "boom"}


def test_detect_and_embed_returns_one_observation_per_detection():
    frame = _image(7, size=100)
    engine = _FakeEngine([fr.FaceDetection(0, 0, 40, 40), fr.FaceDetection(50, 50, 40, 40)])
    observations = fr.detect_and_embed(frame, engine=engine)
    assert len(observations) == 2
    assert all(observation.engine == "fake" for observation in observations)


def test_detect_and_embed_returns_empty_list_when_no_faces_detected():
    frame = _image(7, size=100)
    engine = _FakeEngine([])
    assert fr.detect_and_embed(frame, engine=engine) == []


def test_detect_and_embed_never_raises_when_engine_detection_fails():
    frame = _image(7, size=100)
    assert fr.detect_and_embed(frame, engine=_RaisingEngine()) == []


def test_detect_and_embed_skips_detections_that_fail_to_embed():
    frame = _image(7, size=100)
    # A detection box entirely outside the frame produces an empty crop,
    # so embed() (via embed_face_crop()) returns None and it's skipped
    # rather than raising or producing a garbage observation.
    engine = _FakeEngine([fr.FaceDetection(1000, 1000, 10, 10)])
    assert fr.detect_and_embed(frame, engine=engine) == []


def test_detect_and_embed_quality_is_between_zero_and_one():
    frame = _image(7, size=100)
    engine = _FakeEngine([fr.FaceDetection(0, 0, 40, 40)])
    observations = fr.detect_and_embed(frame, engine=engine)
    assert 0.0 <= observations[0].quality <= 1.0


# --------------------------------------------------------------- engine singleton + real default engine


def test_get_engine_returns_the_same_instance_until_reset():
    fr.reset_engine()
    first = fr.get_engine()
    second = fr.get_engine()
    assert first is second
    fr.reset_engine()
    assert fr.get_engine() is not first


def test_default_engine_is_haar_intensity():
    fr.reset_engine()
    engine = fr.get_engine()
    assert engine.name == "haar_intensity"


def test_real_default_engine_cascade_loads_successfully():
    """This is the one test that exercises the REAL, unmocked default
    engine end to end -- confirms OpenCV's bundled Haar cascade
    actually loads in this environment (a genuine CPU-only dependency
    already required by this codebase), not just that the abstraction
    layer around it is correct."""
    engine = fr.HaarEmbeddingFaceEngine()
    capability = engine.capability()
    assert capability["available"] is True, capability.get("reason")


def test_real_default_engine_returns_empty_list_for_a_blank_image():
    """A blank/synthetic image should not spuriously report a
    detected face -- proves the real Haar cascade is actually being
    consulted (not just always returning something)."""
    engine = fr.HaarEmbeddingFaceEngine()
    blank = np.zeros((200, 200, 3), dtype=np.uint8)
    assert engine.detect_faces(blank) == []
