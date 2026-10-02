"""Shared test helpers for automatic physical Face Access (2026-10-02).

ApprovedEngine stands in for the access-approved ArcFace engine without
loading its model files: it IS an ArcFaceOnnxEngine (so provenance checks
see the real class, name and version), but returns a fixed face and
embedding. Nothing here touches hardware."""
import facial_engine_onnx
import facial_recognition as fr
import face_access_guard


class ApprovedEngine(facial_engine_onnx.ArcFaceOnnxEngine):
    def __init__(self, vector=(1.0, 0.0), faces: int = 1, available: bool = True):  # no model download/load
        self.vector = vector
        self.faces = faces
        self.available = available

    def detect_faces(self, image_bgr):
        return [fr.FaceDetection(index * 150, 0, 10, 10) for index in range(self.faces)]

    def embed(self, face_crop_bgr):
        return self.vector

    def capability(self):
        return {"engine": self.name, "version": self.version, "available": self.available, "gpu": False, "reason": None}


class HaarNamedEngine(fr.FaceEngine):
    """The development engine (Haar) -- what get_engine() falls back to."""
    name = "haar_intensity"
    version = "1"

    def __init__(self, vector=(1.0, 0.0)):
        self.vector = vector

    def detect_faces(self, image_bgr):
        return [fr.FaceDetection(0, 0, 10, 10)]

    def embed(self, face_crop_bgr):
        return self.vector

    def capability(self):
        return {"engine": self.name, "version": self.version, "available": True, "gpu": False, "reason": None}


def observation(engine=None, *, quality=0.5, vector=(1.0, 0.0), width=10, height=10):
    engine = engine or ApprovedEngine(vector)
    return fr.FaceObservation(bbox=fr.FaceDetection(0, 0, width, height), embedding=tuple(vector),
                              engine=engine.name, engine_version=engine.version, quality=quality)


def enable_physical_face_access(monkeypatch, *, environment="production", role="edge"):
    """Everything automatic physical Face Access needs from configuration:
    the dedicated flag, the general Face Access flags, a production
    environment and an explicitly approved appliance runtime role."""
    import relay_control
    monkeypatch.setenv(face_access_guard.PHYSICAL_UNLOCK_ENV, "true")
    monkeypatch.setattr(fr, "FACIAL_RECOGNITION_ENABLED", True)
    monkeypatch.setattr(relay_control, "FACIAL_ACCESS_CONTROL_ENABLED", True)
    if environment is None:
        monkeypatch.delenv("ANYAICAM_ENV", raising=False)
    else:
        monkeypatch.setenv("ANYAICAM_ENV", environment)
    if role is None:
        monkeypatch.delenv("ANYAICAM_RUNTIME_ROLE", raising=False)
    else:
        monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", role)
