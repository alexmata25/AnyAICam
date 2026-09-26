"""AAC CPU performance -- Codex review pass.

Safe synthetic benchmarks only (random-noise numpy arrays, no real
camera, no real face photos) measuring the two real CPU costs in the
Phase 1 pipeline: embed_face_crop() (fixed-size regardless of input
crop size) and HaarEmbeddingFaceEngine.detect_faces() (cost scales with
input image size). These numbers are from whatever machine runs this
test suite -- NOT the actual target appliance hardware (Ryzen/Samsung)
-- and are reported in the Phase 1 Codex review as directional
estimates only, never as a claimed final camera-count capacity; see
that report's own "CPU benchmark" section for the full interpretation
and caveats.

Assertions here are deliberately generous upper bounds (10-50x looser
than the actual measured numbers on a normal development machine) --
this is a regression guard against a catastrophic slowdown (e.g. an
accidental O(n^2) change), not a tight performance contract that could
flake on slower CI hardware.
"""

import time

import numpy as np

import facial_recognition as fr


def _timeit(fn, iterations: int) -> float:
    for _ in range(min(5, iterations)):
        fn()  # warm up (first call may pay one-time import/cache costs)
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    return (time.perf_counter() - start) / iterations


def _noise_image(size: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(size, size, 3), dtype=np.uint8)


def test_embed_face_crop_is_fast_regardless_of_crop_size():
    """embed_face_crop() resizes to a fixed 48x48 grid before doing any
    real work, so its cost should be roughly constant across input
    sizes -- this also indirectly documents that property."""
    for size in (48, 160, 400):
        crop = _noise_image(size)
        per_call = _timeit(lambda: fr.embed_face_crop(crop), iterations=50)
        assert per_call < 0.05, f"embed_face_crop() at {size}x{size} took {per_call*1000:.2f} ms/call, expected < 50 ms"


def test_haar_detection_scales_with_image_size_but_stays_bounded():
    engine = fr.HaarEmbeddingFaceEngine()
    per_call_small = _timeit(lambda: engine.detect_faces(_noise_image(150)), iterations=20)
    per_call_large = _timeit(lambda: engine.detect_faces(_noise_image(500)), iterations=10)
    assert per_call_small < 0.2, f"Haar detection at 150x150 took {per_call_small*1000:.2f} ms/call, expected < 200 ms"
    assert per_call_large < 1.0, f"Haar detection at 500x500 took {per_call_large*1000:.2f} ms/call, expected < 1000 ms"
    # Larger images should cost at least as much as smaller ones -- a
    # sanity check on the measurement itself, not a strict scaling law.
    assert per_call_large >= per_call_small


def test_full_pipeline_on_a_typical_person_crop_completes_quickly():
    """A ~300x300 crop is a reasonable stand-in for a YOLO person
    bounding box at typical camera resolutions -- this measures the
    realistic per-detection cost save_yolo_events()'s AAC hook pays,
    including the (usual, real-world) case where Haar finds no face at
    all in a given crop."""
    frame = _noise_image(300)
    engine = fr.HaarEmbeddingFaceEngine()
    per_call = _timeit(lambda: fr.detect_and_embed(frame, engine=engine), iterations=20)
    assert per_call < 0.5, f"detect_and_embed() at 300x300 took {per_call*1000:.2f} ms/call, expected < 500 ms"
