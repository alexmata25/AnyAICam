"""Face Access production-engine packaging and the cloud/edge engine
mismatch diagnostic (2026-09-25).

The ArcFace engine (facial_engine_onnx.py) existed but was never packaged:
onnxruntime was not installed in the image and the models would have been
lazily downloaded into the bind-mounted /app. These tests keep the image
recipe, the requirements pin and the engine's own pinned model provenance
in agreement, and prove the default engine is still the Haar fallback
(switching engines is an explicit, per-deployment decision)."""
import logging
import re
from pathlib import Path

import pytest

import facial_embedding_sync
import facial_engine_onnx as fe

ROOT = Path(__file__).resolve().parents[2]
MODELS = [
    (fe._DETECTOR_URL, fe._DETECTOR_FILENAME, fe._DETECTOR_SHA256),
    (fe._SFACE_URL, fe._SFACE_FILENAME, fe._SFACE_SHA256),
    (fe._ARCFACE_URL, fe._ARCFACE_FILENAME, fe._ARCFACE_SHA256),
]


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.production"])
def test_every_image_bakes_exactly_the_pinned_models_outside_app(dockerfile):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    baked = fe.BAKED_MODEL_DIR.as_posix()
    assert not baked.startswith("/app")  # /app is bind-mounted on a real appliance
    for url, filename, sha256 in MODELS:
        assert f'-o {baked}/{filename} "{url}"' in text, (dockerfile, filename)
        assert f'echo "{sha256}  {baked}/{filename}" | sha256sum -c -' in text, (dockerfile, filename)


def test_onnxruntime_is_pinned_in_the_image_requirements():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^onnxruntime==\d+\.\d+\.\d+\s*$", requirements, re.MULTILINE)
    assert "numpy==1.26.4" in requirements  # the combination verified to load ArcFace


def test_the_default_face_engine_is_still_the_haar_fallback():
    source = (ROOT / "app" / "facial_recognition.py").read_text(encoding="utf-8")
    assert 'os.environ.get("ANYAICAM_FACE_ENGINE", "haar")' in source


def test_engine_compatibility_counts_matchable_enrollments():
    synced = [{"engine": "onnx_yunet_arcface", "engine_version": "1"}, {"engine": "haar_intensity", "engine_version": "2"}]
    result = facial_embedding_sync.engine_compatibility(synced, active=("onnx_yunet_arcface", "1"))
    assert result == {"active_engine": "onnx_yunet_arcface/1", "embeddings_for_active_engine": 1}


def test_enrollments_from_another_engine_are_flagged_not_changed(caplog):
    synced = [{"engine": "haar_intensity", "engine_version": "2", "embedding_json": "[0.1]"}] * 3
    before = [dict(item) for item in synced]
    with caplog.at_level(logging.WARNING, logger="anyaicam.facial_embedding_sync"):
        result = facial_embedding_sync.engine_compatibility(synced, active=("onnx_yunet_arcface", "1"))
    assert result["engine_mismatch"] is True and result["embeddings_for_active_engine"] == 0
    assert result["enrolled_engines"] == ["haar_intensity/2"]
    assert "no_enrollments_for_active_engine" in caplog.text
    assert synced == before


def test_no_enrollments_is_not_a_mismatch():
    assert facial_embedding_sync.engine_compatibility([], active=("x", "1")) == {"embeddings_for_active_engine": 0}
    assert facial_embedding_sync.engine_compatibility(None) == {"embeddings_for_active_engine": 0}
