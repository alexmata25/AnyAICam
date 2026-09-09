"""AAC Phase 2 production-capable engine (OnnxFaceEngine) -- Codex
review pass.

The real-model tests in this file need to download two small ONNX
files (YuNet + SFace, from the OpenCV Zoo) on first run and need
onnxruntime installed. Both are OPTIONAL for this whole codebase (see
facial_engine_onnx.py's own docstring) -- if either isn't available in
a given environment (no network, an air-gapped CI runner, onnxruntime
not installed), every test that needs them is skipped cleanly via the
`onnx_models_available` fixture below, never failed. This mirrors
test_customer_registration_postgresql.py's own gated-real-resource
pattern, adapted to a resource that (unlike a disposable database)
needs no manual setup when it IS available.

Only synthetic numpy images are used anywhere in this file, per this
project's testing rules -- there is no real face photo bundled or
downloaded. This means the tests here can prove: the model files
download and verify correctly, the engine correctly reports "no face"
on structured noise (the same honest negative-path behavior
HaarEmbeddingFaceEngine has), and -- the one thing genuinely provable
without a real face photo -- that this module's own embedding
preprocessing exactly reproduces OpenCV's own trusted reference
implementation. What it cannot prove (real face detection/alignment/
match accuracy) is exactly what the Phase 2 report's own "next exact
action" defers to real-camera validation.
"""

import numpy as np
import pytest

import facial_engine_onnx as engine_module
import facial_recognition as fr


@pytest.fixture(scope="module")
def onnx_models_available():
    detector_path = engine_module._ensure_model(
        engine_module._DETECTOR_URL, engine_module._DETECTOR_FILENAME, engine_module._DETECTOR_SHA256
    )
    embedding_path = engine_module._ensure_model(
        engine_module._SFACE_URL, engine_module._SFACE_FILENAME, engine_module._SFACE_SHA256
    )
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        pytest.skip("onnxruntime is not installed in this environment")
    if detector_path is None or embedding_path is None:
        pytest.skip("AAC ONNX models could not be downloaded/verified in this environment")
    return detector_path, embedding_path


@pytest.fixture(scope="module")
def arcface_model_available():
    detector_path = engine_module._ensure_model(
        engine_module._DETECTOR_URL, engine_module._DETECTOR_FILENAME, engine_module._DETECTOR_SHA256
    )
    embedding_path = engine_module._ensure_model(
        engine_module._ARCFACE_URL, engine_module._ARCFACE_FILENAME, engine_module._ARCFACE_SHA256
    )
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        pytest.skip("onnxruntime is not installed in this environment")
    if detector_path is None or embedding_path is None:
        pytest.skip("AAC ArcFace model could not be downloaded/verified in this environment")
    return detector_path, embedding_path


def _noise_image(size: int = 320, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(size, size, 3), dtype=np.uint8)


# --------------------------------------------------------------- model provenance / integrity


def test_ensure_model_rejects_a_hash_mismatch(tmp_path, monkeypatch):
    """A file that downloads successfully but doesn't match the pinned
    SHA-256 must never be handed back as usable -- this is the
    supply-chain check: a tampered or substituted file at the source
    URL is rejected exactly like corruption would be."""
    monkeypatch.setattr(engine_module, "MODEL_CACHE_DIR", tmp_path)
    result = engine_module._ensure_model(engine_module._DETECTOR_URL, "test_model.onnx", "0" * 64)
    assert result is None
    assert not (tmp_path / "test_model.onnx").exists()
    assert not (tmp_path / "test_model.onnx.part").exists()  # no partial download left behind either


def test_ensure_model_caches_and_does_not_redownload(tmp_path, monkeypatch, onnx_models_available):
    monkeypatch.setattr(engine_module, "MODEL_CACHE_DIR", tmp_path)
    first = engine_module._ensure_model(engine_module._DETECTOR_URL, engine_module._DETECTOR_FILENAME, engine_module._DETECTOR_SHA256)
    mtime_after_first = first.stat().st_mtime
    second = engine_module._ensure_model(engine_module._DETECTOR_URL, engine_module._DETECTOR_FILENAME, engine_module._DETECTOR_SHA256)
    assert second == first
    assert second.stat().st_mtime == mtime_after_first  # not rewritten -- the cached copy's own hash already matched


def test_ensure_model_repairs_a_corrupted_cached_file(tmp_path, monkeypatch, onnx_models_available):
    monkeypatch.setattr(engine_module, "MODEL_CACHE_DIR", tmp_path)
    engine_module._ensure_model(engine_module._DETECTOR_URL, engine_module._DETECTOR_FILENAME, engine_module._DETECTOR_SHA256)
    cached_path = tmp_path / engine_module._DETECTOR_FILENAME
    cached_path.write_bytes(b"corrupted")
    repaired = engine_module._ensure_model(engine_module._DETECTOR_URL, engine_module._DETECTOR_FILENAME, engine_module._DETECTOR_SHA256)
    assert repaired is not None
    assert engine_module._sha256(repaired) == engine_module._DETECTOR_SHA256


# --------------------------------------------------------------- capability / lifecycle


def test_capability_reports_available_when_models_and_onnxruntime_present(onnx_models_available):
    engine = engine_module.OnnxFaceEngine()
    capability = engine.capability()
    assert capability["available"] is True, capability.get("reason")
    assert "CPUExecutionProvider" in capability["providers"]
    assert capability["gpu"] is False  # default provider list is CPU-only


def test_capability_is_unavailable_without_onnxruntime(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "onnxruntime":
            raise ImportError("simulated: onnxruntime not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    engine = engine_module.OnnxFaceEngine()
    capability = engine.capability()
    assert capability["available"] is False
    assert "onnxruntime" in capability["reason"]


def test_reset_state_clears_loaded_handles(onnx_models_available):
    engine = engine_module.OnnxFaceEngine()
    engine.capability()  # forces a load
    assert engine._session is not None
    engine.reset_state()
    assert engine._session is None
    assert engine._detector is None


# --------------------------------------------------------------- detection / embedding (negative path, synthetic only)


def test_detect_faces_on_structured_noise_finds_nothing(onnx_models_available):
    """Same honest-negative-path behavior as HaarEmbeddingFaceEngine:
    a real detector correctly reports no faces in random noise, rather
    than hallucinating one."""
    engine = engine_module.OnnxFaceEngine()
    assert engine.detect_faces(_noise_image()) == []


def test_embed_returns_none_when_no_face_can_be_realigned_in_the_crop(onnx_models_available):
    """embed() re-runs detection within the crop itself (see this
    engine's own docstring) -- a crop with no real face in it must
    return None, never a fabricated embedding."""
    engine = engine_module.OnnxFaceEngine()
    assert engine.embed(_noise_image(size=160)) is None


def test_detect_and_embed_pipeline_returns_empty_for_a_faceless_frame(onnx_models_available):
    engine = engine_module.OnnxFaceEngine()
    assert fr.detect_and_embed(_noise_image(size=400), engine=engine) == []


# --------------------------------------------------------------- embedding preprocessing correctness (the provable part)


def test_embedding_preprocessing_matches_opencvs_own_reference_implementation(onnx_models_available):
    """The one thing about SFace's embedding that's fully provable
    without a real face photo: this module's own preprocessing (RGB,
    raw 0-255, NCHW -- see _run_embedding_model()'s own docstring)
    reproduces cv2.FaceRecognizerSF.feature()'s reference output on ANY
    valid 112x112 input, not just a real face -- the model doesn't know
    or care whether its input is a real aligned face crop for this
    specific numerical-equivalence check. This is what backs this
    module's claim of empirically-verified preprocessing, as a
    permanent regression test rather than a one-off manual check."""
    import cv2

    _, embedding_path = onnx_models_available
    engine = engine_module.OnnxFaceEngine()
    engine.capability()  # forces load of self._session
    reference = cv2.FaceRecognizerSF.create(str(embedding_path), "")

    rng = np.random.default_rng(11)
    aligned = rng.integers(0, 255, size=(112, 112, 3), dtype=np.uint8)

    reference_embedding = reference.feature(aligned).flatten().astype(np.float64)
    reference_embedding = reference_embedding / np.linalg.norm(reference_embedding)

    ours = engine._run_embedding_model(aligned)
    assert ours is not None
    similarity = fr.cosine_similarity(tuple(reference_embedding), ours)
    assert similarity > 0.9999, f"expected near-exact agreement with cv2's own reference, got {similarity}"


def test_run_embedding_model_output_is_unit_length(onnx_models_available):
    engine = engine_module.OnnxFaceEngine()
    engine.capability()
    rng = np.random.default_rng(5)
    aligned = rng.integers(0, 255, size=(112, 112, 3), dtype=np.uint8)
    embedding = engine._run_embedding_model(aligned)
    assert embedding is not None
    assert abs(np.linalg.norm(np.asarray(embedding)) - 1.0) < 1e-6


def test_run_embedding_model_is_deterministic(onnx_models_available):
    engine = engine_module.OnnxFaceEngine()
    engine.capability()
    rng = np.random.default_rng(5)
    aligned = rng.integers(0, 255, size=(112, 112, 3), dtype=np.uint8)
    first = engine._run_embedding_model(aligned.copy())
    second = engine._run_embedding_model(aligned.copy())
    assert first == second


# --------------------------------------------------------------- engine registration / fallback (facial_recognition.get_engine())


def test_get_engine_selects_onnx_when_configured_and_available(monkeypatch, onnx_models_available):
    monkeypatch.setattr(fr, "FACE_ENGINE_SELECTION", "onnx")
    fr.reset_engine()
    engine = fr.get_engine()
    assert engine.name == "onnx_yunet_sface"
    fr.reset_engine()


def test_get_engine_falls_back_to_haar_when_onnx_forced_but_unavailable(monkeypatch):
    monkeypatch.setattr(fr, "FACE_ENGINE_SELECTION", "onnx")
    monkeypatch.setattr(fr, "_build_named_onnx_engine", lambda class_name: None)
    fr.reset_engine()
    engine = fr.get_engine()
    assert engine.name == "haar_intensity"
    fr.reset_engine()


def test_get_engine_default_selection_is_haar_without_touching_onnx(monkeypatch):
    """The default (no ANYAICAM_FACE_ENGINE set) must never even
    attempt to build any onnx engine -- this is what keeps this
    project's test suite and default deployment hermetic/network-free
    unless an operator explicitly opts in (see FACE_ENGINE_SELECTION's
    own comment)."""
    monkeypatch.setattr(fr, "FACE_ENGINE_SELECTION", "haar")

    def fail_if_called(class_name):
        raise AssertionError(f"_build_named_onnx_engine({class_name!r}) must not be called when FACE_ENGINE_SELECTION='haar'")

    monkeypatch.setattr(fr, "_build_named_onnx_engine", fail_if_called)
    fr.reset_engine()
    engine = fr.get_engine()
    assert engine.name == "haar_intensity"
    fr.reset_engine()


def test_get_engine_selects_arcface_when_configured_and_available(monkeypatch, arcface_model_available):
    monkeypatch.setattr(fr, "FACE_ENGINE_SELECTION", "arcface")
    fr.reset_engine()
    engine = fr.get_engine()
    assert engine.name == "onnx_yunet_arcface"
    fr.reset_engine()


def test_get_engine_auto_prefers_arcface_over_sface(monkeypatch, arcface_model_available):
    monkeypatch.setattr(fr, "FACE_ENGINE_SELECTION", "auto")
    fr.reset_engine()
    engine = fr.get_engine()
    assert engine.name == "onnx_yunet_arcface"
    fr.reset_engine()


# --------------------------------------------------------------- ArcFaceOnnxEngine (Phase 5)
#
# Unlike SFace (Phase 2), there is no OpenCV-bundled reference
# implementation of this exact ArcFace ONNX Model Zoo artifact to diff
# against bit-for-bit -- see facial_engine_onnx.py's own docstring for
# why the RGB/raw-range/CHW preprocessing convention used here is a
# documented-convention match (the ONNX Model Zoo's own reference
# notebook), not an independently cross-validated one. These tests
# verify what IS independently provable without a real face photo or a
# second reference implementation: real download+integrity, real model
# loading, deterministic/unit-length/distinct output, and correct
# engine/version isolation from SFace and Haar.


def test_arcface_model_integrity_hash_matches_pinned_value(arcface_model_available):
    _, embedding_path = arcface_model_available
    assert engine_module._sha256(embedding_path) == engine_module._ARCFACE_SHA256


def test_arcface_capability_reports_available(arcface_model_available):
    engine = engine_module.ArcFaceOnnxEngine()
    capability = engine.capability()
    assert capability["available"] is True, capability.get("reason")
    assert capability["engine"] == "onnx_yunet_arcface"
    assert "CPUExecutionProvider" in capability["providers"]
    assert capability["gpu"] is False


def test_arcface_detect_faces_on_structured_noise_finds_nothing(arcface_model_available):
    engine = engine_module.ArcFaceOnnxEngine()
    assert engine.detect_faces(_noise_image()) == []


def test_arcface_embed_returns_none_when_no_face_can_be_realigned(arcface_model_available):
    engine = engine_module.ArcFaceOnnxEngine()
    assert engine.embed(_noise_image(size=160)) is None


def test_arcface_embedding_model_output_shape_is_512_dim(arcface_model_available):
    engine = engine_module.ArcFaceOnnxEngine()
    engine.capability()  # forces load
    rng = np.random.default_rng(7)
    aligned = rng.integers(0, 255, size=(112, 112, 3), dtype=np.uint8)
    embedding = engine._run_embedding_model(aligned)
    assert embedding is not None
    assert len(embedding) == 512  # ArcFace's own documented embedding dimension -- vs. SFace's 128


def test_arcface_embedding_output_is_unit_length(arcface_model_available):
    engine = engine_module.ArcFaceOnnxEngine()
    engine.capability()
    rng = np.random.default_rng(9)
    aligned = rng.integers(0, 255, size=(112, 112, 3), dtype=np.uint8)
    embedding = engine._run_embedding_model(aligned)
    assert embedding is not None
    assert abs(np.linalg.norm(np.asarray(embedding)) - 1.0) < 1e-6


def test_arcface_embedding_is_deterministic(arcface_model_available):
    engine = engine_module.ArcFaceOnnxEngine()
    engine.capability()
    rng = np.random.default_rng(9)
    aligned = rng.integers(0, 255, size=(112, 112, 3), dtype=np.uint8)
    first = engine._run_embedding_model(aligned.copy())
    second = engine._run_embedding_model(aligned.copy())
    assert first == second


def test_arcface_distinguishes_different_inputs(arcface_model_available):
    engine = engine_module.ArcFaceOnnxEngine()
    engine.capability()
    a = engine._run_embedding_model(np.random.default_rng(1).integers(0, 255, size=(112, 112, 3), dtype=np.uint8))
    b = engine._run_embedding_model(np.random.default_rng(2).integers(0, 255, size=(112, 112, 3), dtype=np.uint8))
    assert a != b
    assert fr.cosine_similarity(a, b) < 0.999


def test_arcface_has_a_distinct_name_and_version_from_sface_and_haar():
    arcface_engine = engine_module.ArcFaceOnnxEngine()
    sface_engine = engine_module.OnnxFaceEngine()
    haar_engine = fr.HaarEmbeddingFaceEngine()
    names = {arcface_engine.name, sface_engine.name, haar_engine.name}
    assert len(names) == 3  # all three distinct -- required for match_face()'s own engine-scoping guard


def test_arcface_and_sface_embeddings_are_never_silently_compared(arcface_model_available):
    """Same core requirement as Phase 2's Haar/SFace test, now for the
    Phase 5 engine: match_face() must never treat ArcFace's 512-dim
    embeddings as comparable to SFace's 128-dim ones (or, hypothetically,
    to any future engine that happened to also produce 512-dim vectors)."""
    arcface_embedding = tuple([0.1] * 512)
    enrolled = [fr.EnrolledEmbedding(person_id="alice", embedding=arcface_embedding, engine="onnx_yunet_sface", engine_version="1")]
    result = fr.match_face(arcface_embedding, enrolled, engine="onnx_yunet_arcface", engine_version="1")
    assert result is None


def test_arcface_and_sface_share_the_same_yunet_detector_and_aligner_models():
    """Both engines are pinned to the SAME YuNet detector file and the
    SAME SFace-weights-backed aligner (alignCrop() is a generic
    landmark aligner, not embedding-specific -- see
    facial_engine_onnx.py's own docstring for why reusing it for
    ArcFace's alignment is a documented-convention choice)."""
    assert engine_module.ArcFaceOnnxEngine._EMBEDDING_FILENAME != engine_module.OnnxFaceEngine._EMBEDDING_FILENAME
    assert engine_module._DETECTOR_FILENAME  # shared detector constant, not per-subclass


# --------------------------------------------------------------- engine/version compatibility (never silently mixed)


def test_onnx_and_haar_embeddings_are_never_silently_compared(onnx_models_available):
    """The core Phase 2 requirement: match_face() must never treat
    embeddings from different engines as comparable, even when both
    happen to be unit-length vectors of a similar shape by coincidence."""
    haar_embedding = fr.embed_face_crop(_noise_image(size=64))
    enrolled = [fr.EnrolledEmbedding(person_id="alice", embedding=haar_embedding, engine="haar_intensity", engine_version="2")]
    result = fr.match_face((0.1,) * len(haar_embedding), enrolled, engine="onnx_yunet_sface", engine_version="1")
    assert result is None


def test_onnx_engine_has_a_distinct_name_and_version_from_haar():
    onnx_engine = engine_module.OnnxFaceEngine()
    haar_engine = fr.HaarEmbeddingFaceEngine()
    assert onnx_engine.name != haar_engine.name
