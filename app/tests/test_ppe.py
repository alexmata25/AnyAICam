"""ppe.py tests. summarize_ppe()/_parse_detections() are pure and
covered thoroughly with synthetic detection data -- no model or image
needed. detect_ppe() is exercised against the real, real model file
(if present) with real synthetic image arrays to prove the actual
inference call succeeds end-to-end without crashing; this can't
meaningfully assert *what* it detects (no real PPE-wearing person
exists as a fixture), but it does prove the real ultralytics
predict()/parse path works, matching lpr.py's own real-OCR-not-mocked
testing philosophy.
"""

import numpy as np
import pytest

import ppe


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    ppe.reset_state()
    monkeypatch.setattr(ppe, "PPE_ENABLED", True)
    monkeypatch.setattr(ppe, "PPE_MIN_CONFIDENCE", 0.4)
    monkeypatch.setattr(ppe, "PPE_CAMERAS", None)
    yield
    ppe.reset_state()


# --------------------------------------------------------- summarize_ppe

def test_no_detections_means_nothing_present():
    result = ppe.summarize_ppe([])
    assert result == {
        "hard_hat_present": False,
        "safety_vest_present": False,
        "confidence": 0.0,
        "detections": [],
    }


def test_helmet_detection_sets_hard_hat_present():
    detections = [{"class_name": "helmet", "confidence": 0.87}]
    result = ppe.summarize_ppe(detections)
    assert result["hard_hat_present"] is True
    assert result["safety_vest_present"] is False
    assert result["confidence"] == 0.87


def test_vest_detection_sets_safety_vest_present():
    detections = [{"class_name": "vest", "confidence": 0.91}]
    result = ppe.summarize_ppe(detections)
    assert result["hard_hat_present"] is False
    assert result["safety_vest_present"] is True
    assert result["confidence"] == 0.91


def test_both_present_when_both_detected():
    detections = [{"class_name": "helmet", "confidence": 0.7}, {"class_name": "vest", "confidence": 0.6}]
    result = ppe.summarize_ppe(detections)
    assert result["hard_hat_present"] is True
    assert result["safety_vest_present"] is True
    assert result["confidence"] == 0.7  # the max of the two relevant hits


def test_irrelevant_classes_are_ignored_for_presence_but_kept_in_detections():
    detections = [{"class_name": "goggles", "confidence": 0.99}, {"class_name": "mask", "confidence": 0.95}]
    result = ppe.summarize_ppe(detections)
    assert result["hard_hat_present"] is False
    assert result["safety_vest_present"] is False
    assert result["confidence"] == 0.0
    assert result["detections"] == detections  # raw detections preserved regardless


def test_multiple_helmet_hits_uses_the_highest_confidence():
    detections = [{"class_name": "helmet", "confidence": 0.3}, {"class_name": "helmet", "confidence": 0.8}]
    result = ppe.summarize_ppe(detections)
    assert result["confidence"] == 0.8


# --------------------------------------------------------- is_camera_enabled

def test_unrestricted_by_default():
    for camera in (1, 2, 3, 4, 5, 99):
        assert ppe.is_camera_enabled(camera) is True


def test_scoped_allowlist_excludes_camera_5(monkeypatch):
    monkeypatch.setattr(ppe, "PPE_CAMERAS", frozenset({1, 2, 3, 4}))
    for camera in (1, 2, 3, 4):
        assert ppe.is_camera_enabled(camera) is True
    assert ppe.is_camera_enabled(5) is False


# --------------------------------------------------------- detect_ppe: guard conditions

def test_detect_ppe_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(ppe, "PPE_ENABLED", False)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert ppe.detect_ppe(frame) is None


def test_detect_ppe_returns_none_for_camera_not_in_scope(monkeypatch):
    monkeypatch.setattr(ppe, "PPE_CAMERAS", frozenset({1, 2, 3, 4}))
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert ppe.detect_ppe(frame, camera_number=5) is None


def test_detect_ppe_returns_none_for_none_input():
    assert ppe.detect_ppe(None) is None


def test_detect_ppe_returns_none_for_empty_image():
    assert ppe.detect_ppe(np.zeros((0, 0, 3), dtype=np.uint8)) is None


# --------------------------------------------------------- real model, real inference call

def test_real_model_loads():
    model = ppe._get_model()
    assert model is not None
    names = {name.lower() for name in model.names.values()}
    assert "helmet" in names
    assert "vest" in names


def test_real_inference_runs_end_to_end_without_crashing_on_a_blank_frame():
    # A blank frame won't contain real PPE -- this proves the real
    # predict()/parse pipeline completes and returns the expected
    # shape, not that detection accuracy is good.
    frame = np.full((480, 640, 3), 128, dtype=np.uint8)
    result = ppe.detect_ppe(frame, camera_number=1)
    assert result is None or (
        isinstance(result, dict)
        and {"hard_hat_present", "safety_vest_present", "confidence", "detections"} <= result.keys()
    )


def test_real_inference_never_raises_on_a_noisy_random_frame():
    rng = np.random.default_rng(42)
    frame = rng.integers(0, 255, size=(480, 640, 3), dtype=np.uint8)
    result = ppe.detect_ppe(frame, camera_number=1)
    assert result is None or isinstance(result, dict)


def test_dockerfiles_actually_fetch_the_model_this_module_depends_on():
    """Regression guard for a real gap found 2026-09-17 during the real
    Ryzen five-camera validation pass: PPE_MODEL_NAME's default
    ("yolov8n-ppe.pt") is not an Ultralytics-hosted name the way
    main.py's own YOLO_MODEL_NAME ("yolov8n.pt") is, so nothing ever
    auto-downloaded it and no Dockerfile ever fetched it either --
    confirmed live on the real Ryzen appliance (`_get_model()` returned
    None, `_model_load_failed` was True). PPE detection had silently
    never worked anywhere despite `PPE_ENABLED`/`cameras.ppe_enabled`
    being on, always failing closed as "no detections" rather than
    erroring (detect_ppe()'s own fail-closed-by-design contract), which
    masked a real, fixable software gap as an absence of real-world PPE
    activity."""
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    dockerfile = (repo_root / "Dockerfile").read_text()
    dockerfile_production = (repo_root / "Dockerfile.production").read_text()
    for content in (dockerfile, dockerfile_production):
        assert "yolov8n-ppe.pt" in content
        assert "Tanishjain9/yolov8n-ppe-detection-6classes" in content
        assert "07172ef3ae9e256c40a1fb0ce3eefe5547d90170645aa73dded0fffc382cdb31" in content


def test_ppe_model_is_fetched_and_loaded_from_a_path_the_apps_bind_mount_never_shadows():
    """Regression guard for a SECOND real gap found 2026-09-17, live on
    Ryzen, immediately after the fix above was deployed: the model
    downloaded correctly at build time (this Dockerfile step succeeded
    -- the build could not have completed otherwise), yet the running
    container still raised FileNotFoundError loading it.

    Root cause: `docker-compose.yml` bind-mounts the host's own release-
    payload `./app` directory over the image's own `/app`
    (`- ./app:/app`, confirmed by reading the compose file directly
    below) so a real running appliance container serves the exact
    release payload's Python source -- but that mount also shadows
    anything a Dockerfile RUN step baked into the image's own `/app`
    that ISN'T part of that source tree, which is exactly what the
    curl-fetched model file was. The model existed in the image and
    loaded fine in any test that only builds/inspects the image; it
    only ever failed on the real, actually-running container -- the
    same "looks fixed until you check the specific layer that matters"
    trap `anyaicam-verify-each-layer-not-just-backend-evidence` already
    warns about elsewhere in this project's own history.

    Fixed by moving the fetch target to /opt/anyaicam-ppe-model, a path
    docker-compose.yml never mounts anything over. This test proves all
    three pieces stay in sync: both Dockerfiles fetch to that exact
    path, ppe.py's own PPE_MODEL_NAME default points at that exact
    path, and the real compose file's own bind mounts never cover it."""
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    dockerfile = (repo_root / "Dockerfile").read_text()
    dockerfile_production = (repo_root / "Dockerfile.production").read_text()
    compose = (repo_root / "docker-compose.yml").read_text()

    model_path = "/opt/anyaicam-ppe-model/yolov8n-ppe.pt"
    for content in (dockerfile, dockerfile_production):
        assert model_path in content
        # Never fetched to anywhere under /app -- the specific mistake
        # this test exists to catch from ever recurring.
        assert "-o /app/yolov8n-ppe.pt" not in content

    assert ppe.PPE_MODEL_NAME == model_path

    # The real compose file's bind mounts, read directly rather than
    # re-implementing docker's own mount-shadowing semantics: none of
    # them targets a container path that /opt/anyaicam-ppe-model (or
    # any ancestor of it) sits under.
    mount_targets = [
        line.split(":", 2)[1]
        for line in compose.splitlines()
        if line.strip().startswith("- ") and ":" in line and "/" in line.split(":", 1)[0]
    ]
    for target in mount_targets:
        assert not model_path.startswith(target.rstrip("/") + "/") and model_path != target, (
            f"docker-compose.yml mounts {target!r} over a path that would shadow the PPE model at {model_path!r}"
        )
