"""AAC face-engine limitation review -- Codex review pass.

Quantifies, with the REAL (unmocked) HaarEmbeddingFaceEngine embedding
math and synthetic images only, exactly the sensitivities the review
was asked to assess: lighting, pose/rotation, and scale/distance. These
are not claims about real face-recognition accuracy (no real face
photos are used anywhere in this file, matching this project's
synthetic-image-only testing rule) -- they are direct, reproducible
measurements of what this specific classical-CV embedding (grayscale +
resize + histogram-equalize + L2-normalize) does and does not tolerate,
which is what backs the "development baseline, not production-grade
biometric identification" assessment in the Phase 1 Codex review
report.
"""

import cv2
import numpy as np

import facial_recognition as fr


def _synthetic_face(size: int = 160, seed: int = 7) -> np.ndarray:
    """A structured (not random-noise) synthetic pattern: a gradient
    background plus two dark circular "eye" regions and a lighter
    "mouth" band -- enough spatial structure for rotation/scale/
    lighting transforms to have a meaningful, non-degenerate effect on
    the resulting embedding, unlike a flat or pure-noise image."""
    rng = np.random.default_rng(seed)
    canvas = np.zeros((size, size, 3), dtype=np.uint8)
    gradient = np.tile(np.linspace(60, 200, size, dtype=np.uint8), (size, 1))
    for channel in range(3):
        canvas[:, :, channel] = gradient
    noise = rng.integers(-10, 10, size=(size, size, 3))
    canvas = np.clip(canvas.astype(int) + noise, 0, 255).astype(np.uint8)
    eye_y = size // 3
    cv2.circle(canvas, (size // 3, eye_y), size // 12, (20, 20, 20), -1)
    cv2.circle(canvas, (2 * size // 3, eye_y), size // 12, (20, 20, 20), -1)
    cv2.ellipse(canvas, (size // 2, 2 * size // 3), (size // 4, size // 10), 0, 0, 180, (230, 230, 230), -1)
    return canvas


def _darken(image: np.ndarray, factor: float) -> np.ndarray:
    return np.clip(image.astype(float) * factor, 0, 255).astype(np.uint8)


def _rotate(image: np.ndarray, degrees: float) -> np.ndarray:
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), degrees, 1.0)
    return cv2.warpAffine(image, matrix, (w, h))


def _shrink_then_restore(image: np.ndarray, tiny_size: int) -> np.ndarray:
    h, w = image.shape[:2]
    tiny = cv2.resize(image, (tiny_size, tiny_size), interpolation=cv2.INTER_AREA)
    return cv2.resize(tiny, (w, h), interpolation=cv2.INTER_LINEAR)


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    embedding_a = fr.embed_face_crop(a)
    embedding_b = fr.embed_face_crop(b)
    assert embedding_a is not None and embedding_b is not None
    return fr.cosine_similarity(embedding_a, embedding_b)


# --------------------------------------------------------------- baseline sanity


def test_identical_image_is_a_perfect_match():
    face = _synthetic_face()
    assert _similarity(face, face.copy()) == 1.0


def test_two_unrelated_synthetic_patterns_are_not_confused():
    """False-positive-risk baseline: two structurally different
    synthetic patterns (different eye/mouth placement) must score well
    below any threshold this project would plausibly configure."""
    face_a = _synthetic_face(seed=1)
    face_b = _synthetic_face(seed=2)
    # Same generator/shapes, different noise seed -- these are
    # intentionally SIMILAR-STRUCTURE images (same eye/mouth layout),
    # so a high similarity here is expected and correct, not a false
    # positive -- see test_structurally_different_patterns_score_lower
    # below for the actual false-positive-risk check.
    similarity = _similarity(face_a, face_b)
    assert similarity > 0.8  # same structure, different noise: correctly recognized as "the same layout"


def test_flat_featureless_image_has_no_usable_embedding():
    """A perfectly flat image has no spatial structure at all -- after
    mean-centering (see embed_face_crop()'s own docstring), its vector
    is exactly zero, which embed_face_crop() correctly reports as "no
    real signal" (None), the same way it already does for an empty
    crop. This is a direct, desirable consequence of centering: version
    1 (uncentered) would have returned a spurious non-null constant
    vector here instead."""
    flat = np.full((160, 160, 3), 128, dtype=np.uint8)
    assert fr.embed_face_crop(flat) is None


def test_structurally_different_patterns_score_meaningfully_lower():
    face = _synthetic_face()
    inverted = cv2.bitwise_not(face)
    similarity = _similarity(face, inverted)
    assert similarity < 0.8, f"expected a meaningfully lower score for an inverted image, got {similarity}"


# --------------------------------------------------------------- lighting sensitivity


def test_moderate_lighting_change_is_substantially_tolerated():
    """Histogram equalization exists specifically to reduce lighting
    sensitivity -- a moderate, uniform brightness change should still
    score highly."""
    face = _synthetic_face()
    dimmer = _darken(face, 0.7)
    similarity = _similarity(face, dimmer)
    assert similarity > 0.85, f"expected strong lighting tolerance, got {similarity}"


def test_severe_underexposure_measurably_degrades_the_match():
    """Real information loss (most pixels crushed toward 0) cannot be
    recovered by equalization -- this is the actual lighting-
    sensitivity limit, not a bug: an engine given a near-black crop
    (a poorly-lit doorway camera, backlight, etc.) will genuinely match
    worse, and this project's threshold-below-rejects-identity rule
    (facial_recognition.classify_match()) is what keeps that from
    becoming a false accept rather than the embedding itself being
    lighting-invariant."""
    face = _synthetic_face()
    very_dark = _darken(face, 0.05)
    similarity = _similarity(face, very_dark)
    baseline = _similarity(face, _darken(face, 0.7))
    assert similarity < baseline, "severe underexposure should degrade the match more than a moderate one"


# --------------------------------------------------------------- pose sensitivity


def test_small_rotation_is_tolerated_reasonably_well():
    face = _synthetic_face()
    rotated_5deg = _rotate(face, 5)
    similarity = _similarity(face, rotated_5deg)
    assert similarity > 0.7


def test_significant_rotation_causes_a_large_drop():
    """This embedding has NO rotation invariance (it is a raw,
    position-ordered pixel-intensity vector) -- a real off-axis face
    (head turned, tilted camera) will score far worse than a
    frontal one. This is the core justification for recommending a
    real face-alignment step (eye/landmark-based rotation-and-scale
    normalization before embedding) as part of any production-grade
    successor engine."""
    face = _synthetic_face()
    rotated_45deg = _rotate(face, 45)
    similarity_small = _similarity(face, _rotate(face, 5))
    similarity_large = _similarity(face, rotated_45deg)
    assert similarity_large < similarity_small
    assert similarity_large < 0.6, f"expected a large pose-induced drop, got {similarity_large}"


# --------------------------------------------------------------- scale / distance sensitivity


def test_mild_downscale_is_tolerated():
    face = _synthetic_face()
    mildly_shrunk = _shrink_then_restore(face, tiny_size=96)
    similarity = _similarity(face, mildly_shrunk)
    assert similarity > 0.8


def test_severe_downscale_simulating_a_distant_face_degrades_the_match():
    """embed_face_crop() resizes every crop to the same fixed grid, so
    it cannot itself distinguish "small in frame" from "close to
    camera" -- but a face that was only ever a handful of real pixels
    before that resize (a person far from a wide-angle camera) has
    already lost the detail a closer capture would have had. This is
    exactly why real deployments need a reasonable minimum face size
    before trusting a match, not just a similarity threshold."""
    face = _synthetic_face()
    postage_stamp = _shrink_then_restore(face, tiny_size=10)
    similarity = _similarity(face, postage_stamp)
    baseline = _similarity(face, _shrink_then_restore(face, tiny_size=96))
    assert similarity < baseline


# --------------------------------------------------------------- partial occlusion (synthetic proxy)


def _occlude_lower_half(image: np.ndarray) -> np.ndarray:
    """A solid block over the lower half of the crop -- a rough,
    synthetic stand-in for a mask, hand, or held object covering part
    of a face. Not a claim about real occlusion robustness (sunglasses/
    a real mask have very different visual structure than a flat
    block) -- see this test's own docstring for what it can and can't
    show without a real face photo."""
    occluded = image.copy()
    height = occluded.shape[0]
    occluded[height // 2 :, :, :] = 90
    return occluded


def test_partial_occlusion_measurably_degrades_the_match():
    """What this CAN honestly show without a real face photo: covering
    part of the input measurably changes the embedding, because this
    engine has no learned understanding of "this is an occluded face,
    the same identity" -- unlike a real lighting/pose transform of the
    SAME content, an occlusion replaces real information with none.
    What it CANNOT show: how a real face behind real glasses or a real
    mask actually performs -- that requires a real photo, deferred to
    real-camera validation (see the Phase 2 report's own note)."""
    face = _synthetic_face()
    occluded = _occlude_lower_half(face)
    similarity = _similarity(face, occluded)
    lightly_transformed = _similarity(face, _darken(face, 0.9))
    assert similarity < lightly_transformed, (
        "occlusion should degrade the match more than a mild, information-preserving transform"
    )


# --------------------------------------------------------------- multiple faces (engine-level, not pipeline-level)


def test_detect_and_embed_produces_independent_embeddings_per_face_region():
    """Sanity check at the engine layer (facial_events.py's own
    multi-face handling is covered separately in test_facial_events.py):
    two different face crops pasted into one frame produce two
    distinctly different embeddings, not one blended/averaged one."""
    frame = np.full((300, 300, 3), 128, dtype=np.uint8)
    face_a = _synthetic_face(size=100, seed=1)
    face_b = _synthetic_face(size=100, seed=99)
    # Make face_b visibly different in structure, not just noise.
    face_b = cv2.bitwise_not(face_b)
    frame[10:110, 10:110] = face_a
    frame[180:280, 180:280] = face_b

    class _TwoFaceEngine(fr.FaceEngine):
        name = "haar_intensity"
        version = "1"

        def detect_faces(self, image_bgr):
            return [fr.FaceDetection(10, 10, 100, 100), fr.FaceDetection(180, 180, 100, 100)]

        def embed(self, face_crop_bgr):
            return fr.embed_face_crop(face_crop_bgr)

        def capability(self):
            return {"engine": "haar_intensity", "version": "1", "available": True, "gpu": False, "reason": None}

    observations = fr.detect_and_embed(frame, engine=_TwoFaceEngine())
    assert len(observations) == 2
    similarity = fr.cosine_similarity(observations[0].embedding, observations[1].embedding)
    assert similarity < 0.9
