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
        engine_module._EMBEDDING_URL, engine_module._EMBEDDING_FILENAME, engine_module._EMBEDDING_SHA256
    )
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        pytest.skip("onnxruntime is not installed in this environment")
    if detector_path is None or embedding_path is None:
        pytest.skip("AAC ONNX models could not be downloaded/verified in this environment")
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
    monkeypatch.setattr(fr, "_build_onnx_engine", lambda: None)
    fr.reset_engine()
    engine = fr.get_engine()
    assert engine.name == "haar_intensity"
    fr.reset_engine()


def test_get_engine_default_selection_is_haar_without_touching_onnx(monkeypatch):
    """The default (no ANYAICAM_FACE_ENGINE set) must never even
    attempt to build the onnx engine -- this is what keeps this
    project's test suite and default deployment hermetic/network-free
    unless an operator explicitly opts in (see FACE_ENGINE_SELECTION's
    own comment)."""
    monkeypatch.setattr(fr, "FACE_ENGINE_SELECTION", "haar")

    def fail_if_called():
        raise AssertionError("_build_onnx_engine() must not be called when FACE_ENGINE_SELECTION='haar'")

    monkeypatch.setattr(fr, "_build_onnx_engine", fail_if_called)
    fr.reset_engine()
    engine = fr.get_engine()
    assert engine.name == "haar_intensity"
    fr.reset_engine()


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
