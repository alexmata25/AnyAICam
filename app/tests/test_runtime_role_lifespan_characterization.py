"""Phase 0 characterization tests (docs/AI_HANDOFF.md §8).

Locks down lifespan()'s current ANYAICAM_RUNTIME_ROLE gating in app/main.py:
a "cloud" role process must never schedule the local camera FFmpeg
supervisors, while "edge"/"combined" roles must preserve today's behavior
of starting one live + one recording supervisor per configured camera.

This exercises the REAL lifespan() control flow (no production code is
modified) with only its leaf worker coroutines replaced by no-ops, so
nothing here spawns a real ffmpeg process, touches the filesystem, or
calls AWS -- entering/exiting the context manager only ever awaits mocked,
already-resolved coroutines.

Run from the `app` directory, per docs/MODULARIZATION.md's existing
convention:
    python -m unittest tests.test_runtime_role_lifespan_characterization -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# See test_live_stream_ffmpeg_characterization.py for why ANYAICAM_PARTNER_DB
# is set explicitly here rather than left unset: `main` freezes it for the
# rest of the process on first import, so whichever test file imports it
# first in a shared discovery run must not point it at a real project path.
os.environ.setdefault("ANYAICAM_DATABASE_BACKEND", "sqlite")
os.environ.setdefault(
    "ANYAICAM_PARTNER_DB",
    str(Path(tempfile.gettempdir()) / "anyaicam-phase0-characterization-test.db"),
)
os.environ.setdefault("ANYAICAM_ENV", "development")
os.environ.setdefault("ANYAICAM_RUNTIME_ROLE", "edge")

import main  # noqa: E402  (path setup must happen first)


class RuntimeRoleLifespanCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    async def _run_lifespan_with(self, runtime_role):
        patchers = [
            patch.object(main, "RUNTIME_ROLE", runtime_role),
            patch.object(main, "process_supervisor", new=AsyncMock(return_value=None)),
            patch.object(main, "motion_detector", new=AsyncMock(return_value=None)),
            patch.object(main, "ai_person_detector", new=AsyncMock(return_value=None)),
            patch.object(main, "health_monitor", new=AsyncMock(return_value=None)),
            patch.object(main, "retention_worker", new=AsyncMock(return_value=None)),
            patch.object(main, "cloud_upload_worker_placeholder", new=AsyncMock(return_value=None)),
        ]
        for patcher in patchers:
            patcher.start()
        try:
            async with main.lifespan(main.app):
                pass
            # Capture the mock while the patch is still active -- reading
            # main.process_supervisor after patcher.stop() would return the
            # real function again, not the mock that recorded the calls.
            supervisor = main.process_supervisor
        finally:
            for patcher in patchers:
                patcher.stop()
        return supervisor

    async def test_cloud_role_does_not_start_local_camera_supervisors(self):
        supervisor = await self._run_lifespan_with("cloud")
        supervisor.assert_not_called()

    async def test_edge_role_starts_a_live_and_recording_supervisor_per_camera(self):
        supervisor = await self._run_lifespan_with("edge")
        self.assertEqual(supervisor.call_count, main.CAMERA_COUNT * 2)
        modes = {call.args[1] for call in supervisor.call_args_list}
        self.assertEqual(modes, {"live", "recording"})

    async def test_combined_role_starts_a_live_and_recording_supervisor_per_camera(self):
        supervisor = await self._run_lifespan_with("combined")
        self.assertEqual(supervisor.call_count, main.CAMERA_COUNT * 2)
        modes = {call.args[1] for call in supervisor.call_args_list}
        self.assertEqual(modes, {"live", "recording"})


if __name__ == "__main__":
    unittest.main()
