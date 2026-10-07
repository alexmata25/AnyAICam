"""Starts the AnyAiCam appliance agent from <install root>\\agent (2026-10-07).

The bundled Python is the embeddable distribution: its ._pth file fixes
sys.path, so the agent package next to this file is added explicitly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from anyaicam_agent.service import main  # noqa: E402

if __name__ == '__main__':
    main()
