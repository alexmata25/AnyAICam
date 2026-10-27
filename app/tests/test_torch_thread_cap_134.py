"""W-4 (Windows 1.3.4 parity, 2026-10-27): PyTorch's intra-op thread cap must
apply whichever YOLO model loads first.

`torch.set_num_threads(2)` ran only inside main.get_yolo_model(). It is
process-global, so the PPE and plate models were covered only when the base
model happened to load first. If PPE or LPR loaded first, one inference
could claim every core, next to the recorders and live streams. Every
loader now calls inference_threads.limit_torch_threads() before loading.
"""
import sys
import types

import pytest


class _FakeTorch(types.ModuleType):
    def __init__(self):
        super().__init__("torch")
        self.calls = []

    def set_num_threads(self, count):
        self.calls.append(count)


@pytest.fixture
def fake_torch(monkeypatch):
    import inference_threads

    torch = _FakeTorch()
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(inference_threads, "_applied", False)
    return torch


def _fake_yolo(path):
    return types.SimpleNamespace(path=path)


def test_ppe_loading_first_caps_the_threads(monkeypatch, fake_torch):
    import ppe

    ppe.reset_state()
    monkeypatch.setattr(ppe, "YOLO", _fake_yolo)
    assert ppe._get_model() is not None
    assert fake_torch.calls == [2]
    ppe.reset_state()


def test_plate_model_loading_first_caps_the_threads(monkeypatch, tmp_path, fake_torch):
    import lpr
    import ultralytics_privacy

    model = tmp_path / "plate.pt"
    model.write_bytes(b"pt")
    monkeypatch.setattr(lpr, "LPR_PLATE_MODEL_PATH", str(model))
    monkeypatch.setattr(lpr, "_plate_model", None)
    monkeypatch.setattr(lpr, "_plate_model_failed", False)
    monkeypatch.setattr(ultralytics_privacy, "load_yolo_class", lambda: _fake_yolo)
    assert lpr._get_plate_model() is not None
    assert fake_torch.calls == [2]


def test_the_cap_is_applied_once_per_process(fake_torch):
    import inference_threads

    inference_threads.limit_torch_threads()
    inference_threads.limit_torch_threads()
    assert fake_torch.calls == [2]


def test_the_cap_is_configurable_and_never_below_one(monkeypatch, fake_torch):
    import inference_threads

    monkeypatch.setenv("ANYAICAM_TORCH_THREADS", "0")
    inference_threads.limit_torch_threads()
    assert fake_torch.calls == [1]


def test_without_torch_nothing_happens(monkeypatch):
    import inference_threads

    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setattr(inference_threads, "_applied", False)
    inference_threads.limit_torch_threads()  # no error
