"""AAC production-capable CPU face engines -- Phase 2/5.

Two engines live here, sharing one detection+alignment pipeline
(`_YuNetAlignedOnnxEngine`) and differing only in which embedding model
backs them:

  OnnxFaceEngine    -- SFace (OpenCV Zoo). Phase 4's real-camera
                       impostor test (test_facial_recognition_ui.py's
                       own history aside -- see the Phase 4 report)
                       measured severe genuine/impostor overlap with
                       this model: at every threshold from 0.55-0.75 the
                       false-accept rate for a real, consenting second
                       person stayed at 51% or higher. SFace is kept
                       available ONLY as a fallback/test engine -- never
                       select it for anything resembling access control.

  ArcFaceOnnxEngine -- LResNet100E-IR ("ArcFace", ONNX Model Zoo,
                       Apache-2.0 license). Added in Phase 5 specifically
                       because SFace's separation was measured, not
                       assumed, to be inadequate. See this module's own
                       docstring section below for full model details.

HaarEmbeddingFaceEngine (facial_recognition.py) remains the always-on,
dependency-free development/test fallback for both.

Shared pipeline:
  1. Detection + 5-point landmarks: OpenCV's own YuNet
     (cv2.FaceDetectorYN) -- reuses cv2, already a hard dependency of
     this codebase; no new package for this step.
  2. Alignment: OpenCV's own cv2.FaceRecognizerSF.alignCrop(), a real
     landmark-based affine warp to a canonical 112x112 pose (not a
     plain resize). This same aligner is used for BOTH embedding models
     below -- alignCrop() warps to the same standard 112x112
     insightface/ArcFace-family face template SFace itself was trained
     against, and ArcFace's own reference preprocessing (see the ONNX
     Model Zoo's own arcface_inference.ipynb) targets that identical
     template. This is a documented-convention assumption, not an
     empirically cross-validated one the way SFace's raw preprocessing
     was in Phase 2 (there is no OpenCV-bundled ArcFace reference
     implementation to diff against) -- flagged here explicitly rather
     than silently assumed correct.
  3. Embedding: the selected ONNX model, run directly through
     onnxruntime (an OPTIONAL dependency, imported lazily -- core VMS
     startup never requires it), never through cv2's own dnn backend,
     so GPU acceleration is a provider swap (ANYAICAM_ONNX_PROVIDERS)
     away, not a rewrite.

======================================================================
ArcFaceOnnxEngine model details (Phase 5)
======================================================================

  Model:            LResNet100E-IR ("ArcFace"), ResNet-100 backbone,
                     INT8-quantized variant (arcfaceresnet100-11-int8.onnx)
  Source:           ONNX Model Zoo, https://github.com/onnx/models/tree/main/validated/vision/body_analysis/arcface
                     (official ONNX project repository; converted from
                     the original InsightFace/MXNet training artifacts)
  License:           Apache 2.0, per that model directory's own README.md
                     ("## License \n Apache 2.0") -- distinct from, and
                     NOT to be confused with, InsightFace's OWN current
                     model zoo (buffalo_l, w600k_r50, etc.), which is
                     licensed for non-commercial research use only. This
                     specific ONNX Model Zoo artifact was deliberately
                     chosen over the more commonly-referenced InsightFace
                     buffalo_l models FOR this reason -- verify this
                     remains accurate before any commercial redistribution
                     decision; this is Claude's reading of that file at
                     the time of this session, not a substitute for the
                     user's own legal review.
  Original paper:    "ArcFace: Additive Angular Margin Loss for Deep
                     Face Recognition" (Deng et al., 2018)
  Trained on:        Refined MS-Celeb-1M (~3.8M images, ~85,000 identities)
  Published accuracy (from the model card, not independently
  re-verified by this project): LFW 99.80% (int8) / CFP-FF 99.83% /
  AgeDB-30 97.87%
  Input:             tensor "data", shape (1, 3, 112, 112), float32,
                     RGB channel order, raw 0-255 range (no external
                     mean/std scaling -- matches the ONNX Model Zoo's
                     own arcface_inference.ipynb, which converts
                     BGR->RGB and transposes to CHW with no separate
                     normalization step before session.run())
  Output:            tensor "fc1", shape (1, 512), float32 -- L2-
                     normalized to unit length by this engine after
                     inference (matching the reference notebook's own
                     `sklearn.preprocessing.normalize(embedding)` step)
  Embedding dim:     512 (vs. SFace's 128)
  File size:         ~62.7 MiB (int8-quantized; the fp32 variant,
                     arcfaceresnet100-8.onnx, is ~249 MiB and NOT used
                     here -- the ONNX Model Zoo's own model card claims
                     "0% accuracy drop ratio" for the int8 conversion
                     with a "1.78x" CPU performance improvement over
                     fp32, making it the clear CPU-first choice; this
                     claim is the model card's own, not independently
                     re-verified by this project)
  Expected CPU cost: See the Phase 5 real-camera performance report for
                     measured latency on real Ryzen appliance hardware
                     under real production load -- materially higher
                     per-face than SFace's much smaller network, as
                     expected for a ResNet-100-class model; do not
                     assume dev-machine or synthetic numbers transfer.
  Integrity:         SHA-256-pinned below, exactly like YuNet/SFace --
                     never loaded if the downloaded file doesn't match.

Neither model is bundled in this git repository: _ensure_model()
downloads each lazily, on first real use, into ANYAICAM_AAC_MODEL_DIR,
matching ppe.py's own YOLO(PPE_MODEL_NAME) precedent -- no binary model
weights are ever committed as part of this codebase. A production
deployment that wants to avoid any first-use download latency/network
dependency can pre-populate ANYAICAM_AAC_MODEL_DIR with these exact,
hash-verified files ahead of time; _ensure_model() treats an already-
present, hash-matching file as already fetched.

onnxruntime is an OPTIONAL dependency, exactly like lpr.py's own
pytesseract: imported lazily inside _load(), never at module import
time. When it (or the model files, or network access) aren't
available, capability() reports that honestly and
facial_recognition.get_engine() falls back to HaarEmbeddingFaceEngine.

No CUDA/GPU dependency is required or assumed: the default PyPI
`onnxruntime` wheel and this module's own default provider list
(["CPUExecutionProvider"]) are CPU-only. A deployment that installs
`onnxruntime-gpu` instead and sets ANYAICAM_ONNX_PROVIDERS=
CUDAExecutionProvider,CPUExecutionProvider gets GPU acceleration with
no code change here.
"""

from __future__ import annotations

import hashlib
import os
import threading
import urllib.request
from pathlib import Path

from facial_recognition import FaceDetection, FaceEngine

MODEL_CACHE_DIR = Path(os.environ.get("ANYAICAM_AAC_MODEL_DIR", "/app/models/aac"))

# Pinned provenance: URL, filename, and the exact SHA-256 of the file at
# that URL as verified during development (see this module's own
# docstring). A future model update requires deliberately updating BOTH
# the URL and the hash together -- never just the URL -- so a silently-
# changed upstream file is always caught, not trusted blindly.
_DETECTOR_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
_DETECTOR_FILENAME = "face_detection_yunet_2023mar.onnx"
_DETECTOR_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"

_SFACE_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
_SFACE_FILENAME = "face_recognition_sface_2021dec.onnx"
_SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"

_ARCFACE_URL = "https://media.githubusercontent.com/media/onnx/models/main/validated/vision/body_analysis/arcface/model/arcfaceresnet100-11-int8.onnx"
_ARCFACE_FILENAME = "arcfaceresnet100-11-int8.onnx"
_ARCFACE_SHA256 = "c625ca68a422418c48aa84f73341337e0a92b111f327909005d1eec07c95f936"

DETECTION_SCORE_THRESHOLD = max(0.0, min(1.0, float(os.environ.get("ANYAICAM_ONNX_DETECTION_THRESHOLD", "0.6"))))
DETECTION_NMS_THRESHOLD = max(0.0, min(1.0, float(os.environ.get("ANYAICAM_ONNX_NMS_THRESHOLD", "0.3"))))

_ONNX_PROVIDERS_RAW = os.environ.get("ANYAICAM_ONNX_PROVIDERS", "CPUExecutionProvider")
ONNX_PROVIDERS = [value.strip() for value in _ONNX_PROVIDERS_RAW.split(",") if value.strip()]

ALIGNED_FACE_SIZE = 112  # both SFace and ArcFace require this fixed input size -- not configurable.
_YUNET_DETECTOR_INPUT_SIZE = (320, 320)  # YuNet's own internal working resolution for the full-frame pass.


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_model(url: str, filename: str, expected_sha256: str) -> Path | None:
    """Lazy download + integrity check. Returns the local path, or None
    if it could not be obtained/verified -- never raises, matching this
    module's overall exception-safety contract. Never loads (or leaves
    on disk under its real name) a file whose hash doesn't match --
    this is the one guard against ever silently using an unverified
    model, whether from corruption or a tampered/substituted source."""
    try:
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    destination = MODEL_CACHE_DIR / filename
    if destination.exists():
        try:
            if _sha256(destination) == expected_sha256:
                return destination
        except OSError:
            pass
        destination.unlink(missing_ok=True)  # stale/corrupt -- fall through to re-download
    temp_path = destination.with_suffix(destination.suffix + ".part")
    try:
        urllib.request.urlretrieve(url, temp_path)
        if _sha256(temp_path) != expected_sha256:
            temp_path.unlink(missing_ok=True)
            return None
        temp_path.replace(destination)
    except OSError:
        temp_path.unlink(missing_ok=True)
        return None
    return destination


class _YuNetAlignedOnnxEngine(FaceEngine):
    """Shared detection+alignment+embedding-forward-pass machinery for
    every YuNet-aligned ONNX embedding engine in this module. Subclasses
    (OnnxFaceEngine, ArcFaceOnnxEngine) only need to set `name`,
    `version`, and the three `_EMBEDDING_*` class attributes -- the
    embedding forward pass itself (_run_embedding_model) reads the
    session's own input name dynamically, so it needs no per-subclass
    override at all: the two engines differ only in which weights are
    loaded into `self._session`, not in how they're called.

    embed() re-runs YuNet on the already-cropped face region it's given
    (rather than reusing landmarks from an earlier full-frame detect_
    faces() call) so it stays a pure function of its one argument,
    exactly like HaarEmbeddingFaceEngine.embed()/embed_face_crop() --
    the FaceEngine.embed(face_crop_bgr) interface carries no id or
    landmark data alongside the crop. This costs one extra (small,
    crop-sized) detector pass per face."""

    name = "abstract_yunet_aligned"
    version = "0"
    _EMBEDDING_URL: str = ""
    _EMBEDDING_FILENAME: str = ""
    _EMBEDDING_SHA256: str = ""

    def __init__(self) -> None:
        self._detector = None
        self._aligner = None  # cv2.FaceRecognizerSF -- used only for alignCrop(), never for its own .feature()
        self._session = None  # onnxruntime.InferenceSession over this subclass's own embedding weights
        self._load_failed = False
        self._unavailable_reason: str | None = None
        self._lock = threading.Lock()

    def reset_state(self) -> None:
        """Test-only: clears every lazy-loaded handle."""
        with self._lock:
            self._detector = None
            self._aligner = None
            self._session = None
            self._load_failed = False
            self._unavailable_reason = None

    def _load(self) -> bool:
        if self._detector is not None and self._session is not None:
            return True
        if self._load_failed:
            return False
        with self._lock:
            if self._detector is not None and self._session is not None:
                return True
            if self._load_failed:
                return False
            try:
                import cv2
            except ImportError as error:
                self._load_failed = True
                self._unavailable_reason = f"opencv unavailable: {error}"
                return False
            try:
                import onnxruntime as ort
            except ImportError as error:
                self._load_failed = True
                self._unavailable_reason = f"onnxruntime is not installed (optional dependency): {error}"
                return False
            detector_path = _ensure_model(_DETECTOR_URL, _DETECTOR_FILENAME, _DETECTOR_SHA256)
            embedding_path = _ensure_model(self._EMBEDDING_URL, self._EMBEDDING_FILENAME, self._EMBEDDING_SHA256)
            if detector_path is None or embedding_path is None:
                self._load_failed = True
                self._unavailable_reason = "AAC ONNX model download/integrity check failed"
                return False
            try:
                detector = cv2.FaceDetectorYN.create(
                    str(detector_path),
                    "",
                    _YUNET_DETECTOR_INPUT_SIZE,
                    score_threshold=DETECTION_SCORE_THRESHOLD,
                    nms_threshold=DETECTION_NMS_THRESHOLD,
                )
                # alignCrop() is a generic landmark-based aligner, not
                # tied to SFace's own weights -- loading it against the
                # SFace model file here is just how cv2's API happens to
                # expose that function; it is reused unchanged for
                # ArcFace's own alignment too (see this module's own
                # docstring for why that's a documented-convention
                # assumption, not an independently verified one).
                aligner_path = _ensure_model(_SFACE_URL, _SFACE_FILENAME, _SFACE_SHA256)
                if aligner_path is None:
                    raise RuntimeError("could not obtain the shared aligner model")
                aligner = cv2.FaceRecognizerSF.create(str(aligner_path), "")
                session = ort.InferenceSession(str(embedding_path), providers=ONNX_PROVIDERS)
            except Exception as error:
                self._load_failed = True
                self._unavailable_reason = f"model load failed: {error}"
                return False
            self._detector = detector
            self._aligner = aligner
            self._session = session
            return True

    def capability(self) -> dict:
        available = self._load()
        providers = list(self._session.get_providers()) if self._session is not None else []
        return {
            "engine": self.name,
            "version": self.version,
            "available": available,
            "gpu": any(marker in provider for provider in providers for marker in ("CUDA", "DML", "ROCM", "TensorRT")),
            "providers": providers,
            "reason": None if available else self._unavailable_reason,
        }

    def detect_faces(self, image_bgr) -> list[FaceDetection]:
        if image_bgr is None or getattr(image_bgr, "size", 0) == 0 or not self._load():
            return []
        height, width = image_bgr.shape[:2]
        try:
            self._detector.setInputSize((width, height))
            _, faces = self._detector.detect(image_bgr)
        except Exception:
            return []
        if faces is None:
            return []
        detections = []
        for row in faces:
            x, y, w, h = row[0], row[1], row[2], row[3]
            detections.append(FaceDetection(int(max(0, x)), int(max(0, y)), int(max(1, w)), int(max(1, h))))
        return detections

    def _best_detection_row(self, crop_bgr):
        """Runs YuNet on an already-isolated face crop and returns the
        single best (highest-score) raw detection row (bbox + 5-point
        landmarks, in the CROP's own coordinate space) that
        FaceRecognizerSF.alignCrop() needs -- or None if no face is
        (re-)found within the crop."""
        height, width = crop_bgr.shape[:2]
        self._detector.setInputSize((width, height))
        _, faces = self._detector.detect(crop_bgr)
        if faces is None or len(faces) == 0:
            return None
        # Score is column index 14 in YuNet's own output row layout.
        return max(faces, key=lambda row: row[14])

    def embed(self, face_crop_bgr) -> tuple[float, ...] | None:
        if face_crop_bgr is None or getattr(face_crop_bgr, "size", 0) == 0 or not self._load():
            return None
        try:
            row = self._best_detection_row(face_crop_bgr)
            if row is None:
                # A face was found in the parent frame (that's why
                # embed() was called at all) but re-localizing it
                # within the isolated crop failed -- e.g. the crop's
                # own boundary cut off too much context. Returning None
                # here (no embedding, no event) is the honest outcome;
                # this function never guesses an alignment.
                return None
            aligned = self._aligner.alignCrop(face_crop_bgr, row)
            embedding = self._run_embedding_model(aligned)
        except Exception:
            return None
        return embedding

    def _run_embedding_model(self, aligned_bgr) -> tuple[float, ...] | None:
        """aligned_bgr: a 112x112 BGR image, already aligned by
        alignCrop(). Preprocessing (BGR->RGB, raw 0-255 float32, NCHW)
        matches both SFace's own empirically-verified convention
        (Phase 2: bit-for-bit match against cv2.FaceRecognizerSF.
        feature(), cosine similarity ~1.0) and ArcFace's own documented
        reference preprocessing (the ONNX Model Zoo's own
        arcface_inference.ipynb) -- both models, despite being trained
        independently, use the identical RGB/raw-range/CHW convention,
        which is why this one method serves both subclasses unchanged."""
        import numpy as np

        if aligned_bgr is None or getattr(aligned_bgr, "size", 0) == 0:
            return None
        rgb = aligned_bgr[:, :, ::-1]
        blob = rgb.astype("float32").transpose(2, 0, 1)[None, :, :, :]
        input_name = self._session.get_inputs()[0].name
        raw_output = self._session.run(None, {input_name: blob})[0]
        vector = np.asarray(raw_output, dtype=np.float64).flatten()
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            return None
        unit = vector / norm
        return tuple(round(float(value), 8) for value in unit)


class OnnxFaceEngine(_YuNetAlignedOnnxEngine):
    """SFace embedding (OpenCV Zoo, Apache-2.0). Kept available as a
    fallback/test engine only -- Phase 4's real-camera impostor test
    measured a 51%+ false-accept rate at every threshold from 0.55 to
    0.75 against one real, consenting second person. Do not select this
    engine for anything resembling access control; see
    ArcFaceOnnxEngine for the Phase 5 replacement."""

    name = "onnx_yunet_sface"
    version = "1"
    _EMBEDDING_URL = _SFACE_URL
    _EMBEDDING_FILENAME = _SFACE_FILENAME
    _EMBEDDING_SHA256 = _SFACE_SHA256


class ArcFaceOnnxEngine(_YuNetAlignedOnnxEngine):
    """ArcFace (LResNet100E-IR, ONNX Model Zoo, Apache-2.0). See this
    module's own docstring for full model provenance/license/spec
    details, and the Phase 5 report for real-camera genuine/impostor
    separation results. 512-dimensional embeddings (vs. SFace's 128) --
    engine name/version scoping in facial_recognition.match_face()
    already guarantees these are never compared against SFace or Haar
    embeddings even if all three happened to produce same-length
    vectors by coincidence."""

    name = "onnx_yunet_arcface"
    version = "1"
    _EMBEDDING_URL = _ARCFACE_URL
    _EMBEDDING_FILENAME = _ARCFACE_FILENAME
    _EMBEDDING_SHA256 = _ARCFACE_SHA256
