"""Phase 0 characterization tests (docs/AI_HANDOFF.md §8).

Proves that Phase 0 does not alter local recording output, the existing
cloud_upload_worker discovery/queueing behavior, or the current S3
recording key/path convention. No real ffmpeg process is spawned (
subprocess.Popen is mocked) and no real AWS call is made (boto3.client is
mocked) -- every filesystem-touching path constant is redirected to a
temporary directory for the duration of each test.

Run from the `app` directory, per docs/MODULARIZATION.md's existing
convention:
    python -m unittest tests.test_recording_cloud_upload_characterization -v
"""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class StartRecordingFfmpegCharacterizationTests(unittest.TestCase):
    """Locks down start_recording()'s current FFmpeg invocation (app/main.py)."""

    CAMERA_NUMBER = 96

    def setUp(self):
        self._env_backup = {
            key: os.environ.get(key)
            for key in (
                f"CAMERA{self.CAMERA_NUMBER}_HOST",
                f"CAMERA{self.CAMERA_NUMBER}_USERNAME",
                f"CAMERA{self.CAMERA_NUMBER}_PASSWORD",
            )
        }
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_HOST"] = "10.20.30.41"
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_USERNAME"] = "svc"
        os.environ[f"CAMERA{self.CAMERA_NUMBER}_PASSWORD"] = "secret"
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-phase0-recordings-")
        self._recordings_patch = patch.object(main, "RECORDINGS_FOLDER", Path(self._tempdir.name))
        self._recordings_patch.start()

    def tearDown(self):
        self._recordings_patch.stop()
        self._tempdir.cleanup()
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_recording_command_is_stream_copy_segmented_mkv(self):
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            main.start_recording(self.CAMERA_NUMBER)
            self.assertEqual(mock_popen.call_count, 1)
            (command,), _kwargs = mock_popen.call_args

        self.assertEqual(command[0], "ffmpeg")
        self.assertIn(main.camera_url(self.CAMERA_NUMBER), command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-b:a") + 1], "96k")
        self.assertEqual(command[command.index("-f") + 1], "segment")
        self.assertEqual(command[command.index("-segment_time") + 1], "300")
        self.assertTrue(command[-1].endswith(".mkv"))
        self.assertIn(f"camera{self.CAMERA_NUMBER}", command[-1])

    def test_recording_folder_is_created_under_recordings_folder(self):
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            main.start_recording(self.CAMERA_NUMBER)

        expected_folder = Path(self._tempdir.name) / f"camera{self.CAMERA_NUMBER}"
        self.assertTrue(expected_folder.is_dir())


class CloudRecordingS3KeyCharacterizationTests(unittest.TestCase):
    """cloud_recording_s3_key() (app/main.py) is a pure function -- no
    mocking needed. Locks down today's S3 key convention for uploaded
    recordings: {S3_PREFIX}/recordings/camera{N}/{YYYY}/{MM}/{DD}/{filename}
    """

    def test_key_uses_prefix_recordings_camera_and_date_from_mtime(self):
        with tempfile.TemporaryDirectory(prefix="anyaicam-phase0-s3key-") as folder:
            recording = Path(folder) / "camera3_2026-01-02_03-04-05.mkv"
            recording.write_bytes(b"fake")

            with patch.object(main, "S3_PREFIX", "anyaicam-test"):
                key = main.cloud_recording_s3_key(recording, camera_number=3)

            self.assertTrue(key.startswith("anyaicam-test/recordings/camera3/"))
            self.assertTrue(key.endswith("/camera3_2026-01-02_03-04-05.mkv"))

    def test_key_omits_prefix_segment_when_s3_prefix_is_empty(self):
        with tempfile.TemporaryDirectory(prefix="anyaicam-phase0-s3key-") as folder:
            recording = Path(folder) / "camera1_clip.mkv"
            recording.write_bytes(b"fake")

            with patch.object(main, "S3_PREFIX", ""):
                key = main.cloud_recording_s3_key(recording, camera_number=1)

            self.assertTrue(key.startswith("recordings/camera1/"))


class ScanRecordingsForCloudUploadCharacterizationTests(unittest.TestCase):
    """scan_recordings_for_cloud_upload() (app/main.py) only ever touches the
    local filesystem and the in-memory/queue-file job list -- it never calls
    AWS itself (that happens later, inside upload_cloud_recording_job(),
    characterized separately below). This locks down its current gating and
    discovery behavior.
    """

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-phase0-scan-")
        recordings_folder = Path(self._tempdir.name)
        backup_folder = recordings_folder / "backups"
        backup_folder.mkdir(parents=True, exist_ok=True)
        queue_file = recordings_folder / "cloud_upload_queue.json"
        index_file = recordings_folder / "cloud_recording_index.json"

        self._patches = [
            patch.object(main, "RECORDINGS_FOLDER", recordings_folder),
            patch.object(main, "BACKUP_FOLDER", backup_folder),
            patch.object(main, "CLOUD_UPLOAD_QUEUE_FILE", queue_file),
            patch.object(main, "CLOUD_RECORDING_INDEX_FILE", index_file),
            patch.object(main, "cloud_upload_queue", []),
            # Isolates "is a completed file discovered" from the unrelated
            # CLOUD_UPLOAD_MIN_FILE_AGE_SECONDS threshold, which is a
            # separately-named, separately-configured constant this test
            # does not need to hold fixed to characterize discovery/queueing.
            patch.object(main, "CLOUD_UPLOAD_MIN_FILE_AGE_SECONDS", 0),
        ]
        for p in self._patches:
            p.start()
        self._recordings_folder = recordings_folder

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tempdir.cleanup()

    def test_disabled_upload_does_not_queue_anything(self):
        (self._recordings_folder / "camera2_clip.mkv").write_bytes(b"fake")
        with patch.object(main, "CLOUD_UPLOAD_ENABLED", False):
            added = main.scan_recordings_for_cloud_upload()
        self.assertEqual(added, 0)
        self.assertEqual(main.cloud_upload_queue, [])

    def test_enabled_upload_queues_discovered_mkv_and_mp4_files(self):
        (self._recordings_folder / "camera2_clip.mkv").write_bytes(b"fake-mkv")
        (self._recordings_folder / "camera2_clip.mp4").write_bytes(b"fake-mp4")

        with patch.object(main, "CLOUD_UPLOAD_ENABLED", True):
            added = main.scan_recordings_for_cloud_upload()

        self.assertEqual(added, 2)
        self.assertEqual(len(main.cloud_upload_queue), 2)
        queued_names = {Path(job["path"]).name for job in main.cloud_upload_queue}
        self.assertEqual(queued_names, {"camera2_clip.mkv", "camera2_clip.mp4"})
        for job in main.cloud_upload_queue:
            self.assertEqual(job["status"], "queued")
            self.assertIn("recordings/camera2/", job["s3_key"])

    def test_files_under_backup_folder_are_skipped(self):
        (self._recordings_folder / "backups" / "old_backup.mkv").write_bytes(b"fake")

        with patch.object(main, "CLOUD_UPLOAD_ENABLED", True):
            added = main.scan_recordings_for_cloud_upload()

        self.assertEqual(added, 0)


class UploadCloudRecordingJobCharacterizationTests(unittest.TestCase):
    """upload_cloud_recording_job() (app/main.py) is the only function in
    this pipeline that talks to AWS. boto3.client is mocked here so this
    test never makes a real network call, while still characterizing the
    exact upload_file() call shape (bucket, key, content type, storage
    class, SSE) the current S3 recording path relies on.
    """

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-phase0-upload-")
        self._recording = Path(self._tempdir.name) / "camera4_clip.mkv"
        self._recording.write_bytes(b"fake-recording-bytes")

        index_file = Path(self._tempdir.name) / "cloud_recording_index.json"
        self._patches = [
            patch.object(main, "S3_BUCKET", "anyaicam-test-bucket"),
            patch.object(main, "AWS_REGION", "us-east-1"),
            patch.object(main, "CLOUD_UPLOAD_STORAGE_CLASS", "STANDARD"),
            patch.object(main, "CLOUD_UPLOAD_SSE", "AES256"),
            patch.object(main, "CLOUD_UPLOAD_KMS_KEY_ID", ""),
            patch.object(main, "CLOUD_UPLOAD_DELETE_LOCAL", False),
            patch.object(main, "CLOUD_RECORDING_INDEX_FILE", index_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tempdir.cleanup()

    def test_upload_calls_s3_with_expected_bucket_key_and_encryption(self):
        job = {
            "id": "job-1",
            "path": str(self._recording),
            "camera": 4,
            "s3_key": "anyaicam/recordings/camera4/2026/01/02/camera4_clip.mkv",
        }
        recording_bytes = self._recording.read_bytes()
        expected_digest = hashlib.sha256(recording_bytes).hexdigest()
        expected_size = len(recording_bytes)

        mock_client = MagicMock()
        # sha256_file() is exercised for real here (Phase 0.1): app/main.py
        # was missing `import hashlib`, which made every real call to
        # sha256_file() raise NameError -- i.e. the actual recording->S3
        # pipeline crashed on every upload once
        # ANYAICAM_CLOUD_UPLOAD_ENABLED=true. That's now fixed (a one-line
        # `import hashlib` added to app/main.py, no other production
        # behavior changed), so this test no longer needs to mock around it.
        # Only boto3/network stays mocked -- no real AWS call is made.
        with patch.object(main.boto3, "client", return_value=mock_client) as mock_client_factory:
            record = main.upload_cloud_recording_job(job)

        mock_client_factory.assert_called_once_with("s3", region_name="us-east-1")
        mock_client.upload_file.assert_called_once()
        kwargs = mock_client.upload_file.call_args.kwargs

        self.assertEqual(kwargs["Bucket"], "anyaicam-test-bucket")
        self.assertEqual(kwargs["Key"], job["s3_key"])
        self.assertEqual(kwargs["Filename"], str(self._recording))
        self.assertEqual(kwargs["ExtraArgs"]["ContentType"], "video/x-matroska")
        self.assertEqual(kwargs["ExtraArgs"]["StorageClass"], "STANDARD")
        self.assertEqual(kwargs["ExtraArgs"]["ServerSideEncryption"], "AES256")

        self.assertEqual(record["s3_bucket"], "anyaicam-test-bucket")
        self.assertEqual(record["s3_key"], job["s3_key"])
        self.assertEqual(record["s3_uri"], f"s3://anyaicam-test-bucket/{job['s3_key']}")
        self.assertFalse(record["local_deleted"])

        # Real sha256_file() output (Phase 0.1: hashlib is now imported in
        # app/main.py) -- proves the fix actually resolves the digest/size
        # rather than merely silencing the NameError.
        self.assertEqual(record["sha256"], expected_digest)
        self.assertEqual(record["size_bytes"], expected_size)


if __name__ == "__main__":
    unittest.main()
