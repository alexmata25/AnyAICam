"""One PyTorch thread cap for every local AI model (W-4, 2026-10-27).

torch.set_num_threads() is process-global. It used to run only when the
base person/vehicle model loaded, so a PPE or plate model that loaded
first ran with PyTorch's default (every core), next to the recorders and
live streams. Every YOLO loader calls limit_torch_threads() first.
"""
from __future__ import annotations

import os
import threading

DEFAULT_TORCH_THREADS = 2

_applied = False
_lock = threading.Lock()


def limit_torch_threads() -> None:
    """Cap PyTorch's intra-op threads once per process
    (ANYAICAM_TORCH_THREADS, default 2, at least 1). No-op without torch."""
    global _applied
    if _applied:
        return
    with _lock:
        if _applied:
            return
        try:
            import torch
        except ImportError:
            return
        if torch is None:
            return
        try:
            count = int(os.environ.get("ANYAICAM_TORCH_THREADS", DEFAULT_TORCH_THREADS))
        except ValueError:
            count = DEFAULT_TORCH_THREADS
        torch.set_num_threads(max(1, count))
        _applied = True
