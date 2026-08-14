"""Phase 3 (docs/AI_HANDOFF.md §8) unit tests for app/live_relay_uploader.py,
the appliance-side live-segment watcher/uploader.

No real network/AWS/filesystem-outside-tempdir access is ever made: boto3 and
urllib.request.urlopen are mocked in every test that would otherwise touch
them. This module imports nothing from main.py/appliance_cloud.py/partner_db
(it is a standalone client of the Phase 2 control-plane endpoints), so none
of the ANYAICAM_PARTNER_DB import-order workarounds used elsewhere in this
test suite are needed here.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import live_relay_uploader as relay  # noqa: E402


def _iso(delta_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def _valid_response(**overrides) -> dict:
    response = {
        "bucket": "anyaicam2026",
        "key_prefix": "live/customer-1/site-1/appliance-1/camera-1/",
        "credentials": {
            "access_key_id": "AKIAEXAMPLE",
            "secret_access_key": "s3cr3t-value",
            "session_token": "tok3n-value",
            "expiration": _iso(900),
        },
    }
    response.update(overrides)
    return response


class ResetStateMixin:
    """Every test gets a clean slate of the module's in-memory state."""

    def setUp(self):
        super().setUp()
        relay._active_cameras.clear()
        relay._sessions.clear()
        relay._uploaded_segments.clear()
        relay._next_sequence.clear()
        relay._last_applied_relay_state.clear()


class LoadApplianceIdentityTests(ResetStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-live-relay-credential-")
        self._credential_file = Path(self._tempdir.name) / "credential.json"
        self._patch = patch.object(relay, "CREDENTIAL_FILE", self._credential_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tempdir.cleanup()
        super().tearDown()

    def test_missing_file_returns_none(self):
        self.assertIsNone(relay._load_appliance_identity())

    def test_malformed_json_returns_none(self):
        self._credential_file.write_text("{not json", encoding="utf-8")
        self.assertIsNone(relay._load_appliance_identity())

    def test_non_dict_json_returns_none(self):
        self._credential_file.write_text(json.dumps(["appliance-1", "credential-value"]), encoding="utf-8")
        self.assertIsNone(relay._load_appliance_identity())

    def test_missing_keys_return_none(self):
        self._credential_file.write_text(json.dumps({"credential_id": "abc"}), encoding="utf-8")
        self.assertIsNone(relay._load_appliance_identity())

    def test_blank_values_return_none(self):
        self._credential_file.write_text(
            json.dumps({"appliance_id": "  ", "credential": ""}), encoding="utf-8"
        )
        self.assertIsNone(relay._load_appliance_identity())

    def test_valid_file_returns_stripped_identity(self):
        self._credential_file.write_text(
            json.dumps({"appliance_id": " appliance-1 ", "credential": " secret-cred "}), encoding="utf-8"
        )
        self.assertEqual(relay._load_appliance_identity(), ("appliance-1", "secret-cred"))


class PlaylistSegmentDiscoveryTests(ResetStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-live-relay-hls-")
        self.hls_folder = Path(self._tempdir.name)

    def tearDown(self):
        self._tempdir.cleanup()
        super().tearDown()

    def _write_playlist(self, camera_number: int, lines: list[str]) -> None:
        (self.hls_folder / f"camera{camera_number}.m3u8").write_text("\n".join(lines), encoding="utf-8")

    def test_missing_playlist_returns_empty_list(self):
        self.assertEqual(relay._playlist_segment_names(self.hls_folder, 1), [])

    def test_valid_segment_entries_are_accepted(self):
        self._write_playlist(1, ["#EXTM3U", "#EXTINF:2.0,", "camera1_000001.ts", "camera1_000002.ts"])
        self.assertEqual(
            relay._playlist_segment_names(self.hls_folder, 1),
            ["camera1_000001.ts", "camera1_000002.ts"],
        )

    def test_blank_lines_and_comments_are_skipped(self):
        self._write_playlist(1, ["#EXTM3U", "", "   ", "#EXTINF:2.0,", "camera1_1.ts"])
        self.assertEqual(relay._playlist_segment_names(self.hls_folder, 1), ["camera1_1.ts"])

    def test_path_traversal_entry_is_stripped_to_a_bare_name_and_then_rejected(self):
        # "../../etc/passwd.ts" -> Path(...).name == "passwd.ts", which then fails
        # the camera1-prefix pattern and is rejected outright -- never accepted,
        # and never able to reference anything outside hls_folder.
        self._write_playlist(1, ["../../etc/passwd.ts"])
        self.assertEqual(relay._playlist_segment_names(self.hls_folder, 1), [])

    def test_wrong_extension_is_rejected(self):
        self._write_playlist(1, ["camera1_1.mp4"])
        self.assertEqual(relay._playlist_segment_names(self.hls_folder, 1), [])

    def test_unexpected_characters_are_rejected(self):
        self._write_playlist(1, ["camera1_evil_script.ts"])
        self.assertEqual(relay._playlist_segment_names(self.hls_folder, 1), [])

    def test_entry_for_a_different_camera_prefix_is_rejected(self):
        self._write_playlist(1, ["camera2_1.ts"])
        self.assertEqual(relay._playlist_segment_names(self.hls_folder, 1), [])

    def test_invalid_entries_are_logged_not_silently_dropped(self):
        self._write_playlist(1, ["camera1_evil.ts"])
        with self.assertLogs("anyaicam.live_relay_uploader", level="WARNING") as captured:
            relay._playlist_segment_names(self.hls_folder, 1)
        self.assertTrue(any("playlist_entry_rejected" in line for line in captured.output))

    def test_pending_segments_excludes_already_uploaded(self):
        self._write_playlist(1, ["camera1_1.ts", "camera1_2.ts"])
        (self.hls_folder / "camera1_1.ts").write_text("data", encoding="utf-8")
        (self.hls_folder / "camera1_2.ts").write_text("data", encoding="utf-8")
        pending = relay._pending_segments(self.hls_folder, 1, already_uploaded={"camera1_1.ts"})
        self.assertEqual([path.name for path in pending], ["camera1_2.ts"])

    def test_pending_segments_excludes_a_symlink_resolving_outside_hls_folder(self):
        outside = tempfile.TemporaryDirectory(prefix="anyaicam-live-relay-outside-")
        self.addCleanup(outside.cleanup)
        secret = Path(outside.name) / "secret.txt"
        secret.write_text("not a segment", encoding="utf-8")
        self._write_playlist(1, ["camera1_1.ts"])
        os.symlink(secret, self.hls_folder / "camera1_1.ts")
        with self.assertLogs("anyaicam.live_relay_uploader", level="WARNING") as captured:
            pending = relay._pending_segments(self.hls_folder, 1, already_uploaded=set())
        self.assertEqual(pending, [])
        self.assertTrue(any("segment_path_outside_hls_folder" in line for line in captured.output))


class RememberUploadedTests(ResetStateMixin, unittest.TestCase):
    def test_bounded_to_max_tracked_segments(self):
        for index in range(relay.MAX_TRACKED_SEGMENTS_PER_CAMERA + 10):
            relay._remember_uploaded(1, f"camera1_{index}.ts")
        tracked = relay._uploaded_segments[1]
        self.assertEqual(len(tracked), relay.MAX_TRACKED_SEGMENTS_PER_CAMERA)
        # Oldest entries are the ones trimmed; the most recent one is always kept.
        self.assertEqual(tracked[-1], f"camera1_{relay.MAX_TRACKED_SEGMENTS_PER_CAMERA + 9}.ts")

    def test_a_remembered_segment_is_not_returned_as_pending_again(self):
        with tempfile.TemporaryDirectory(prefix="anyaicam-live-relay-dedup-") as tempdir:
            hls_folder = Path(tempdir)
            (hls_folder / "camera1.m3u8").write_text("camera1_1.ts", encoding="utf-8")
            (hls_folder / "camera1_1.ts").write_text("data", encoding="utf-8")
            relay._remember_uploaded(1, "camera1_1.ts")
            pending = relay._pending_segments(hls_folder, 1, already_uploaded=set(relay._uploaded_segments[1]))
            self.assertEqual(pending, [])


class SessionExpiresSoonTests(unittest.TestCase):
    def test_missing_key_is_treated_as_expired(self):
        self.assertTrue(relay._session_expires_soon({}))

    def test_non_string_value_is_treated_as_expired(self):
        self.assertTrue(relay._session_expires_soon({"expires_at": 12345}))

    def test_blank_string_is_treated_as_expired(self):
        self.assertTrue(relay._session_expires_soon({"expires_at": "   "}))

    def test_malformed_string_is_treated_as_expired(self):
        self.assertTrue(relay._session_expires_soon({"expires_at": "not-a-timestamp"}))

    def test_timezone_naive_value_is_treated_as_expired(self):
        naive = (datetime.now() + timedelta(seconds=900)).isoformat()
        self.assertTrue(relay._session_expires_soon({"expires_at": naive}))

    def test_expiring_within_margin_is_treated_as_expired(self):
        soon = _iso(relay.SESSION_RENEW_MARGIN_SECONDS - 1)
        self.assertTrue(relay._session_expires_soon({"expires_at": soon}))

    def test_far_future_aware_timestamp_is_not_expired(self):
        later = _iso(relay.SESSION_RENEW_MARGIN_SECONDS + 600)
        self.assertFalse(relay._session_expires_soon({"expires_at": later}))


class EnsureSessionTests(ResetStateMixin, unittest.TestCase):
    def test_valid_response_is_cached_and_returned(self):
        with patch.object(relay, "_control_plane_request", return_value=_valid_response()) as mock_request:
            session = relay._ensure_session(1, "camera-1")
        self.assertIsNotNone(session)
        self.assertEqual(session["bucket"], "anyaicam2026")
        self.assertEqual(session["key_prefix"], "live/customer-1/site-1/appliance-1/camera-1/")
        mock_request.assert_called_once_with("/api/appliance/live/camera-1/session", {})

    def test_cached_session_is_reused_without_a_second_request(self):
        with patch.object(relay, "_control_plane_request", return_value=_valid_response()) as mock_request:
            relay._ensure_session(1, "camera-1")
            relay._ensure_session(1, "camera-1")
        mock_request.assert_called_once()

    def test_expiring_session_triggers_a_renewal_request(self):
        soon_expiring = _valid_response()
        soon_expiring["credentials"]["expiration"] = _iso(1)
        with patch.object(relay, "_control_plane_request", return_value=soon_expiring) as mock_request:
            relay._ensure_session(1, "camera-1")
            relay._ensure_session(1, "camera-1")
        self.assertEqual(mock_request.call_count, 2)

    def _assert_rejected(self, response, reason_fragment):
        with patch.object(relay, "_control_plane_request", return_value=response):
            with self.assertLogs("anyaicam.live_relay_uploader", level="WARNING") as captured:
                session = relay._ensure_session(1, "camera-1")
        self.assertIsNone(session)
        self.assertNotIn(1, relay._sessions)
        self.assertTrue(any(reason_fragment in line for line in captured.output))
        return captured.output

    def test_non_dict_response_is_rejected(self):
        self._assert_rejected(None, "reason=not_a_dict")

    def test_credentials_not_a_dict_is_rejected(self):
        self._assert_rejected(_valid_response(credentials="nope"), "reason=credentials_not_a_dict")

    def test_missing_access_key_id_is_rejected(self):
        response = _valid_response()
        response["credentials"]["access_key_id"] = ""
        self._assert_rejected(response, "reason=missing_access_key_id")

    def test_missing_secret_access_key_is_rejected(self):
        response = _valid_response()
        del response["credentials"]["secret_access_key"]
        self._assert_rejected(response, "reason=missing_secret_access_key")

    def test_missing_session_token_is_rejected(self):
        response = _valid_response()
        response["credentials"]["session_token"] = "   "
        self._assert_rejected(response, "reason=missing_session_token")

    def test_missing_expiration_is_rejected(self):
        response = _valid_response()
        response["credentials"]["expiration"] = ""
        self._assert_rejected(response, "reason=missing_expiration")

    def test_missing_bucket_is_rejected(self):
        response = _valid_response(bucket="")
        self._assert_rejected(response, "reason=missing_bucket")

    def test_missing_key_prefix_is_rejected(self):
        response = _valid_response(key_prefix="")
        self._assert_rejected(response, "reason=missing_key_prefix")

    def test_rejection_log_never_contains_credential_values(self):
        response = _valid_response()
        response["credentials"]["access_key_id"] = ""
        output = self._assert_rejected(response, "reason=missing_access_key_id")
        joined = "\n".join(output)
        self.assertNotIn("s3cr3t-value", joined)
        self.assertNotIn("tok3n-value", joined)
        self.assertNotIn("AKIAEXAMPLE", joined)


class ControlPlaneRequestTests(ResetStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-live-relay-control-plane-")
        self._credential_file = Path(self._tempdir.name) / "credential.json"
        self._credential_file.write_text(
            json.dumps({"appliance_id": "appliance-1", "credential": "cred-value"}), encoding="utf-8"
        )
        self._patches = [
            patch.object(relay, "CREDENTIAL_FILE", self._credential_file),
            patch.object(relay, "CLOUD_URL", "https://cloud.example.com"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tempdir.cleanup()
        super().tearDown()

    def test_no_identity_returns_none_without_a_network_call(self):
        with patch.object(relay, "CREDENTIAL_FILE", Path(self._tempdir.name) / "missing.json"), \
                patch("live_relay_uploader.urllib.request.urlopen") as mock_urlopen:
            result = relay._control_plane_request("/api/appliance/live/camera-1/session", {})
        self.assertIsNone(result)
        mock_urlopen.assert_not_called()

    def test_no_cloud_url_returns_none_without_a_network_call(self):
        with patch.object(relay, "CLOUD_URL", ""), \
                patch("live_relay_uploader.urllib.request.urlopen") as mock_urlopen:
            result = relay._control_plane_request("/api/appliance/live/camera-1/session", {})
        self.assertIsNone(result)
        mock_urlopen.assert_not_called()

    def test_request_carries_the_expected_headers_url_and_body(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"status": "accepted"}).encode()
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = False
        with patch("live_relay_uploader.urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            result = relay._control_plane_request(
                "/api/appliance/live/camera-1/segment-available", {"segment_key": "k", "sequence": 3}
            )
        self.assertEqual(result, {"status": "accepted"})
        sent_request = mock_urlopen.call_args.args[0]
        self.assertEqual(sent_request.full_url, "https://cloud.example.com/api/appliance/live/camera-1/segment-available")
        self.assertEqual(sent_request.get_header("Authorization"), "Bearer cred-value")
        self.assertEqual(sent_request.get_header("X-appliance-id"), "appliance-1")
        self.assertIn("X-request-timestamp", sent_request.headers)
        self.assertIn("X-request-nonce", sent_request.headers)
        self.assertEqual(json.loads(sent_request.data.decode()), {"segment_key": "k", "sequence": 3})

    def test_http_error_returns_none(self):
        import io
        import urllib.error

        http_error = urllib.error.HTTPError(url="u", code=403, msg="Forbidden", hdrs=None, fp=io.BytesIO(b""))
        self.addCleanup(http_error.close)
        with patch("live_relay_uploader.urllib.request.urlopen", side_effect=http_error):
            result = relay._control_plane_request("/api/appliance/live/camera-1/session", {})
        self.assertIsNone(result)

    def test_unreachable_host_returns_none(self):
        import urllib.error

        with patch("live_relay_uploader.urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            result = relay._control_plane_request("/api/appliance/live/camera-1/session", {})
        self.assertIsNone(result)


class RelayActiveFlagTests(ResetStateMixin, unittest.TestCase):
    def test_defaults_to_inactive(self):
        self.assertFalse(relay.is_relay_active(1))
        self.assertEqual(relay.active_camera_numbers(), [])

    def test_set_active_then_inactive(self):
        relay.set_relay_active(1, "camera-1", active=True)
        self.assertTrue(relay.is_relay_active(1))
        self.assertEqual(relay.active_camera_numbers(), [1])
        relay.set_relay_active(1, "camera-1", active=False)
        self.assertFalse(relay.is_relay_active(1))
        self.assertEqual(relay.active_camera_numbers(), [])

    def test_deactivating_a_camera_drops_its_cached_session(self):
        relay._sessions[1] = {"bucket": "b", "key_prefix": "p", "credentials": {}, "expires_at": _iso(900)}
        relay.set_relay_active(1, "camera-1", active=False)
        self.assertNotIn(1, relay._sessions)


class UploadSegmentTests(ResetStateMixin, unittest.TestCase):
    def test_uploads_with_the_sessions_scoped_credentials_and_correct_key(self):
        with tempfile.TemporaryDirectory(prefix="anyaicam-live-relay-upload-") as tempdir:
            local_path = Path(tempdir) / "camera1_1.ts"
            local_path.write_bytes(b"segment-bytes")
            session = {
                "bucket": "anyaicam2026",
                "key_prefix": "live/customer-1/site-1/appliance-1/camera-1/",
                "credentials": {
                    "access_key_id": "AKIAEXAMPLE",
                    "secret_access_key": "s3cr3t-value",
                    "session_token": "tok3n-value",
                },
            }
            mock_client = MagicMock()
            mock_boto3 = MagicMock()
            mock_boto3.client.return_value = mock_client
            with patch.object(relay, "boto3", mock_boto3), patch.object(relay, "AWS_REGION", "us-east-1"):
                segment_key = relay._upload_segment(session, local_path)

        self.assertEqual(segment_key, "live/customer-1/site-1/appliance-1/camera-1/camera1_1.ts")
        mock_boto3.client.assert_called_once_with(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="AKIAEXAMPLE",
            aws_secret_access_key="s3cr3t-value",
            aws_session_token="tok3n-value",
        )
        mock_client.upload_file.assert_called_once_with(
            str(local_path), "anyaicam2026", segment_key, ExtraArgs={"ContentType": "video/mp2t"}
        )

    def test_raises_when_boto3_is_unavailable(self):
        with patch.object(relay, "boto3", None):
            with self.assertRaises(RuntimeError):
                relay._upload_segment({"credentials": {}}, Path("irrelevant.ts"))


class RelayCameraOnceTests(ResetStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-live-relay-camera-once-")
        self.hls_folder = Path(self._tempdir.name)
        (self.hls_folder / "camera1.m3u8").write_text("camera1_1.ts\ncamera1_2.ts", encoding="utf-8")
        (self.hls_folder / "camera1_1.ts").write_bytes(b"a")
        (self.hls_folder / "camera1_2.ts").write_bytes(b"b")

    def tearDown(self):
        self._tempdir.cleanup()
        super().tearDown()

    def test_no_session_available_uploads_nothing(self):
        with patch.object(relay, "_ensure_session", return_value=None) as mock_ensure, \
                patch.object(relay, "_upload_segment") as mock_upload:
            relay._relay_camera_once(self.hls_folder, 1, "camera-1")
        mock_ensure.assert_called_once_with(1, "camera-1")
        mock_upload.assert_not_called()

    def test_each_segment_is_uploaded_once_and_notified(self):
        session = {"bucket": "b", "key_prefix": "live/c/s/a/camera-1/", "credentials": {}, "expires_at": _iso(900)}
        with patch.object(relay, "_ensure_session", return_value=session), \
                patch.object(relay, "_upload_segment", side_effect=lambda s, p: s["key_prefix"] + p.name) as mock_upload, \
                patch.object(relay, "_control_plane_request", return_value={"status": "accepted"}) as mock_notify:
            relay._relay_camera_once(self.hls_folder, 1, "camera-1")

        self.assertEqual(mock_upload.call_count, 2)
        self.assertEqual(mock_notify.call_count, 2)
        first_call = mock_notify.call_args_list[0]
        self.assertEqual(first_call.args[0], "/api/appliance/live/camera-1/segment-available")
        self.assertEqual(first_call.args[1]["segment_key"], "live/c/s/a/camera-1/camera1_1.ts")
        self.assertEqual(first_call.args[1]["sequence"], 0)
        second_call = mock_notify.call_args_list[1]
        self.assertEqual(second_call.args[1]["sequence"], 1)

        # A second pass over the same playlist must not re-upload either segment.
        with patch.object(relay, "_ensure_session", return_value=session), \
                patch.object(relay, "_upload_segment") as mock_upload_again:
            relay._relay_camera_once(self.hls_folder, 1, "camera-1")
        mock_upload_again.assert_not_called()

    def test_a_failed_upload_is_dropped_not_retried(self):
        session = {"bucket": "b", "key_prefix": "live/c/s/a/camera-1/", "credentials": {}, "expires_at": _iso(900)}
        with patch.object(relay, "_ensure_session", return_value=session), \
                patch.object(relay, "_upload_segment", side_effect=RuntimeError("network blip")), \
                patch.object(relay, "_control_plane_request") as mock_notify:
            with self.assertLogs("anyaicam.live_relay_uploader", level="WARNING") as captured:
                relay._relay_camera_once(self.hls_folder, 1, "camera-1")
        mock_notify.assert_not_called()
        self.assertTrue(any("segment_upload_failed" in line for line in captured.output))

        # The failed segment must not be retried on the next pass.
        with patch.object(relay, "_ensure_session", return_value=session), \
                patch.object(relay, "_upload_segment") as mock_upload_again:
            relay._relay_camera_once(self.hls_folder, 1, "camera-1")
        mock_upload_again.assert_not_called()

    def test_upload_failure_never_logs_credential_values(self):
        session = {
            "bucket": "b",
            "key_prefix": "live/c/s/a/camera-1/",
            "credentials": {"access_key_id": "AKIAEXAMPLE", "secret_access_key": "s3cr3t-value", "session_token": "tok3n-value"},
            "expires_at": _iso(900),
        }
        with patch.object(relay, "_ensure_session", return_value=session), \
                patch.object(relay, "_upload_segment", side_effect=RuntimeError("boom")):
            with self.assertLogs("anyaicam.live_relay_uploader", level="WARNING") as captured:
                relay._relay_camera_once(self.hls_folder, 1, "camera-1")
        joined = "\n".join(captured.output)
        self.assertNotIn("s3cr3t-value", joined)
        self.assertNotIn("tok3n-value", joined)
        self.assertNotIn("AKIAEXAMPLE", joined)


class LiveRelayWorkerDisabledTests(ResetStateMixin, unittest.IsolatedAsyncioTestCase):
    async def _run_briefly_then_cancel(self):
        task = None
        try:
            import asyncio

            task = asyncio.create_task(relay.live_relay_worker(Path("/nonexistent")))
            await asyncio.sleep(0)
        finally:
            if task:
                task.cancel()
                with self.assertRaises(BaseException):
                    await task

    async def test_cloud_role_never_starts_relaying(self):
        with patch.object(relay, "RUNTIME_ROLE", "cloud"), patch.object(relay, "LIVE_RELAY_ENABLED", True):
            await self._run_briefly_then_cancel()
        self.assertEqual(relay.live_relay_state["worker_status"], "disabled")

    async def test_disabled_flag_never_starts_relaying_even_on_edge(self):
        with patch.object(relay, "RUNTIME_ROLE", "edge"), patch.object(relay, "LIVE_RELAY_ENABLED", False):
            await self._run_briefly_then_cancel()
        self.assertEqual(relay.live_relay_state["worker_status"], "disabled")


class RelayCommandReconciliationTests(ResetStateMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self._tempdir = tempfile.TemporaryDirectory(prefix="anyaicam-live-relay-commands-")
        self._commands_file = Path(self._tempdir.name) / "live_relay_commands.json"
        self._patch = patch.object(relay, "RELAY_COMMANDS_FILE", self._commands_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tempdir.cleanup()
        super().tearDown()

    def _write_desired_state(self, state: dict) -> None:
        self._commands_file.parent.mkdir(parents=True, exist_ok=True)
        self._commands_file.write_text(json.dumps(state), encoding="utf-8")

    # --- basic activation/deactivation ---

    def test_activates_the_requested_camera(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": True}})
        relay._reconcile_relay_commands()
        self.assertTrue(relay.is_relay_active(5))
        self.assertEqual(relay.active_camera_numbers(), [5])

    def test_deactivates_when_desired_state_says_inactive(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": True}})
        relay._reconcile_relay_commands()
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": False}})
        relay._reconcile_relay_commands()
        self.assertFalse(relay.is_relay_active(5))

    def test_unrelated_cameras_are_never_touched(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": True}})
        relay._reconcile_relay_commands()
        self.assertFalse(relay.is_relay_active(6))
        self.assertFalse(relay.is_relay_active(1))

    def test_missing_file_is_a_no_op_and_preserves_default_off(self):
        relay._reconcile_relay_commands()
        self.assertEqual(relay.active_camera_numbers(), [])

    # --- malformed/invalid state ---

    def test_malformed_camera_number_key_is_skipped_and_does_not_raise(self):
        self._write_desired_state({"not-a-number": {"camera_id": "cam-x", "active": True}})
        relay._reconcile_relay_commands()
        self.assertEqual(relay.active_camera_numbers(), [])

    def test_one_malformed_entry_does_not_block_a_valid_transition(self):
        self._write_desired_state({
            "5": {"camera_id": "cam-5", "active": True},
            "bad": {"camera_id": "cam-bad", "active": True},
            "6": "not-a-dict",
        })
        relay._reconcile_relay_commands()
        self.assertTrue(relay.is_relay_active(5))
        self.assertFalse(relay.is_relay_active(6))

    def test_non_dict_json_state_is_treated_as_empty(self):
        self._commands_file.parent.mkdir(parents=True, exist_ok=True)
        self._commands_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        relay._reconcile_relay_commands()
        self.assertEqual(relay.active_camera_numbers(), [])

    def test_corrupted_json_state_is_treated_as_empty(self):
        self._commands_file.parent.mkdir(parents=True, exist_ok=True)
        self._commands_file.write_text("{not valid json", encoding="utf-8")
        relay._reconcile_relay_commands()
        self.assertEqual(relay.active_camera_numbers(), [])

    # --- idempotency ---

    def test_unchanged_active_state_does_not_repeatedly_invoke_set_relay_active(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": True}})
        with patch.object(relay, "set_relay_active") as mock_set:
            relay._reconcile_relay_commands()
            relay._reconcile_relay_commands()
            relay._reconcile_relay_commands()
        mock_set.assert_called_once_with(5, "cam-5", True)

    def test_true_to_false_invokes_exactly_one_transition(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": True}})
        relay._reconcile_relay_commands()
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": False}})
        with patch.object(relay, "set_relay_active") as mock_set:
            relay._reconcile_relay_commands()
        mock_set.assert_called_once_with(5, "cam-5", False)

    def test_camera_id_change_while_active_deactivates_old_then_activates_new(self):
        self._write_desired_state({"5": {"camera_id": "cam-5-old", "active": True}})
        relay._reconcile_relay_commands()
        self._write_desired_state({"5": {"camera_id": "cam-5-new", "active": True}})
        with patch.object(relay, "set_relay_active") as mock_set:
            relay._reconcile_relay_commands()
        self.assertEqual(
            mock_set.call_args_list,
            [unittest.mock.call(5, "cam-5-old", False), unittest.mock.call(5, "cam-5-new", True)],
        )

    # --- removal / disappearance handling ---

    def test_active_camera_then_entry_removed_deactivates_exactly_once(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": True}})
        relay._reconcile_relay_commands()
        self._write_desired_state({})
        with patch.object(relay, "set_relay_active") as mock_set:
            relay._reconcile_relay_commands()
        mock_set.assert_called_once_with(5, "cam-5", False)

    def test_removed_entry_is_also_removed_from_last_applied_state(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": True}})
        relay._reconcile_relay_commands()
        self.assertIn(5, relay._last_applied_relay_state)
        self._write_desired_state({})
        relay._reconcile_relay_commands()
        self.assertNotIn(5, relay._last_applied_relay_state)

    def test_non_bool_active_value_is_rejected(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": "true"}})
        relay._reconcile_relay_commands()
        self.assertEqual(relay.active_camera_numbers(), [])

    def test_active_camera_then_file_missing_deactivates_exactly_once(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": True}})
        relay._reconcile_relay_commands()
        self._commands_file.unlink()
        with patch.object(relay, "set_relay_active") as mock_set:
            relay._reconcile_relay_commands()
        mock_set.assert_called_once_with(5, "cam-5", False)

    def test_repeated_empty_or_missing_state_does_not_repeat_deactivate(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": True}})
        relay._reconcile_relay_commands()
        self._commands_file.unlink()
        relay._reconcile_relay_commands()
        with patch.object(relay, "set_relay_active") as mock_set:
            relay._reconcile_relay_commands()
            relay._reconcile_relay_commands()
        mock_set.assert_not_called()

    def test_malformed_entry_does_not_accidentally_deactivate_an_unrelated_valid_camera(self):
        self._write_desired_state({
            "5": {"camera_id": "cam-5", "active": True},
            "6": {"camera_id": "cam-6", "active": True},
        })
        relay._reconcile_relay_commands()
        self.assertTrue(relay.is_relay_active(5))
        self.assertTrue(relay.is_relay_active(6))
        self._write_desired_state({
            "5": {"camera_id": "cam-5", "active": "not-a-bool"},
            "6": {"camera_id": "cam-6", "active": True},
        })
        with patch.object(relay, "set_relay_active") as mock_set:
            relay._reconcile_relay_commands()
        # camera 5's now-malformed entry is treated as absent -> deactivated
        # exactly once; camera 6's unchanged valid entry triggers no call at all.
        mock_set.assert_called_once_with(5, "cam-5", False)

    # --- wiring into the worker loop ---

    async def test_worker_disabled_path_never_calls_reconcile(self):
        with patch.object(relay, "RUNTIME_ROLE", "cloud"), \
                patch.object(relay, "LIVE_RELAY_ENABLED", True), \
                patch.object(relay, "_reconcile_relay_commands") as mock_reconcile:
            task = asyncio.create_task(relay.live_relay_worker(Path("/nonexistent")))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(BaseException):
                await task
        mock_reconcile.assert_not_called()

    async def test_worker_enabled_path_reconciles_desired_state_end_to_end(self):
        self._write_desired_state({"5": {"camera_id": "cam-5", "active": True}})
        with patch.object(relay, "RUNTIME_ROLE", "edge"), \
                patch.object(relay, "LIVE_RELAY_ENABLED", True), \
                patch.object(relay, "SCAN_SECONDS", 0.01), \
                patch.object(relay, "_relay_camera_once"):
            task = asyncio.create_task(relay.live_relay_worker(Path("/nonexistent")))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(BaseException):
                await task
        self.assertTrue(relay.is_relay_active(5))


class RelayCommandValidatorTests(unittest.TestCase):
    def test_valid_camera_number_is_accepted(self):
        self.assertEqual(relay._validate_camera_number(5), 5)
        self.assertEqual(relay._validate_camera_number("5"), 5)

    def test_boolean_camera_number_is_rejected(self):
        self.assertIsNone(relay._validate_camera_number(True))
        self.assertIsNone(relay._validate_camera_number(False))

    def test_out_of_range_camera_number_is_rejected(self):
        for bad in (0, -1, 257, 10000):
            with self.subTest(bad=bad):
                self.assertIsNone(relay._validate_camera_number(bad))

    def test_non_numeric_camera_number_is_rejected(self):
        self.assertIsNone(relay._validate_camera_number("not-a-number"))
        self.assertIsNone(relay._validate_camera_number(None))

    def test_valid_camera_id_is_accepted(self):
        self.assertEqual(relay._validate_camera_id("cam-5"), "cam-5")

    def test_blank_camera_id_is_rejected(self):
        self.assertIsNone(relay._validate_camera_id("   "))
        self.assertIsNone(relay._validate_camera_id(""))

    def test_oversized_camera_id_is_rejected(self):
        self.assertIsNone(relay._validate_camera_id("x" * 129))

    def test_non_string_camera_id_is_rejected(self):
        self.assertIsNone(relay._validate_camera_id(12345))
        self.assertIsNone(relay._validate_camera_id(None))


if __name__ == "__main__":
    unittest.main()
