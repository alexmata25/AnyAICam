"""AAC production-capable CPU face engine -- Phase 2.

OnnxFaceEngine adds real face alignment and a stronger embedding on top
of the FaceEngine interface facial_recognition.py already defines
(HaarEmbeddingFaceEngine remains available as the always-on development/
test fallback -- see that module's own docstring). It is selected via
ANYAICAM_FACE_ENGINE (see facial_recognition.get_engine()), never
imported unconditionally by facial_recognition.py itself, so importing
that module -- and this codebase's core startup -- never requires
onnxruntime or a network connection.

Pipeline:
  1. Detection + 5-point landmarks: OpenCV's own YuNet
     (cv2.FaceDetectorYN) -- this reuses cv2, already a hard dependency
     of this codebase, and needs no new package for this step.
  2. Alignment: OpenCV's own cv2.FaceRecognizerSF.alignCrop(), a real
     landmark-based affine warp to a canonical 112x112 pose -- not a
     plain resize. This is the actual fix for the pose/rotation
     sensitivity Phase 1's Codex review measured and flagged as
     unresolved by normalization alone.
  3. Embedding: the SFace ONNX model, run directly through onnxruntime
     (an OPTIONAL dependency -- see below), not through cv2's own dnn
     backend, so a deployment that wants GPU acceleration is a provider
     swap (ANYAICAM_ONNX_PROVIDERS) away, not a rewrite. Preprocessing
     (RGB channel order, raw 0-255 float32, NCHW) was verified, during
     Phase 2 development, to reproduce cv2.FaceRecognizerSF.feature()'s
     own reference embedding bit-for-bit (cosine similarity
     0.999999999997 on a controlled test input) -- this is not a guess
     at SFace's preprocessing, it is empirically confirmed against
     OpenCV's own trusted implementation.

Model provenance: both models come from the OpenCV Zoo
(https://github.com/opencv/opencv_zoo, Apache-2.0 license) -- YuNet
(face_detection_yunet_2023mar.onnx) and SFace
(face_recognition_sface_2021dec.onnx). Neither is bundled in this git
repository: _ensure_model() downloads each lazily, on first real use,
into ANYAICAM_AAC_MODEL_DIR, exactly matching ppe.py's own
YOLO(PPE_MODEL_NAME) precedent for "the model is fetched by name at
runtime, never committed as a binary blob." Every download's SHA-256 is
checked against a hash pinned in this module (computed directly from
the exact files fetched during Phase 2 development) before it is ever
loaded into an inference session; a mismatch (corruption OR a tampered/
substituted file at the source) is rejected and deleted, never used --
a supply-chain integrity check, not merely a corruption check.

onnxruntime is an OPTIONAL dependency, exactly like lpr.py's own
pytesseract: imported lazily inside _load(), never at module import
time. When it (or the model files, or network access) aren't
available, capability() reports that honestly and
facial_recognition.get_engine() falls back to HaarEmbeddingFaceEngine
-- see that function's own docstring for the fallback policy.

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
# that URL as verified during Phase 2 development (see this module's
# own docstring). A future model update requires deliberately updating
# BOTH the URL and the hash together -- never just the URL -- so a
# silently-changed upstream file is always caught, not trusted blindly.
_DETECTOR_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
_DETECTOR_FILENAME = "face_detection_yunet_2023mar.onnx"
_DETECTOR_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"

_EMBEDDING_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
_EMBEDDING_FILENAME = "face_recognition_sface_2021dec.onnx"
_EMBEDDING_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"

DETECTION_SCORE_THRESHOLD = max(0.0, min(1.0, float(os.environ.get("ANYAICAM_ONNX_DETECTION_THRESHOLD", "0.6"))))
DETECTION_NMS_THRESHOLD = max(0.0, min(1.0, float(os.environ.get("ANYAICAM_ONNX_NMS_THRESHOLD", "0.3"))))

_ONNX_PROVIDERS_RAW = os.environ.get("ANYAICAM_ONNX_PROVIDERS", "CPUExecutionProvider")
ONNX_PROVIDERS = [value.strip() for value in _ONNX_PROVIDERS_RAW.split(",") if value.strip()]

ALIGNED_FACE_SIZE = 112  # SFace's own fixed required input size -- not configurable.
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
    module's overall exception-safety contract."""
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


class OnnxFaceEngine(FaceEngine):
    """Production-capable engine: YuNet detection+landmarks, real
    landmark-based alignment (cv2.FaceRecognizerSF.alignCrop()), SFace
    embedding run through onnxruntime directly.

    embed() re-runs YuNet on the already-cropped face region it's given
    (rather than reusing landmarks from the earlier full-frame detect_
    faces() call) so it stays a pure function of its one argument,
    exactly like HaarEmbeddingFaceEngine.embed()/embed_face_crop() --
    the FaceEngine.embed(face_crop_bgr) interface carries no id or
    landmark data alongside the crop, so re-localizing landmarks within
    the crop's own, already-isolated coordinate space is the correct,
    self-contained way to get a real alignment without changing that
    shared interface. This costs one extra (small, crop-sized) detector
    pass per face -- see the Phase 2 performance report for the
    measured cost of this tradeoff."""

    name = "onnx_yunet_sface"
    version = "1"

    def __init__(self) -> None:
        self._detector = None
        self._aligner = None  # cv2.FaceRecognizerSF -- used only for alignCrop(), never for its own .feature()
        self._session = None  # onnxruntime.InferenceSession over the SAME SFace weights, run directly
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
            embedding_path = _ensure_model(_EMBEDDING_URL, _EMBEDDING_FILENAME, _EMBEDDING_SHA256)
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
                aligner = cv2.FaceRecognizerSF.create(str(embedding_path), "")
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
        was verified during Phase 2 development to reproduce
        cv2.FaceRecognizerSF.feature()'s own output bit-for-bit (cosine
        similarity ~1.0 on a controlled test input) -- see this
        module's own docstring."""
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
