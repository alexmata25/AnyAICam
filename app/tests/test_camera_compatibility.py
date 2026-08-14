"""Unit tests for app/edge/camera_compatibility.py, the reusable camera/IP-device
compatibility evaluation engine.

Pure module under test -- no network, no database, no FastAPI. These tests
call evaluate_camera_compatibility()/evaluate_scan_results() directly with
plain dicts, exactly as a future caller (e.g. a customer-facing pre-purchase
compatibility checker, not built here) would.
"""

import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from edge.camera_compatibility import (  # noqa: E402
    APPROVED,
    NOT_SUPPORTED,
    PARTIALLY_SUPPORTED,
    evaluate_camera_compatibility,
    evaluate_scan_results,
)


def _codes(result):
    return {reason.code for reason in result.reasons}


class DecisionTableTests(unittest.TestCase):
    """Every cell of the approved, corrected 3x3 tri-state decision table."""

    # A fixed manufacturer/model is supplied throughout this class so that
    # manufacturer_unknown/model_unknown reason codes (covered separately in
    # RequiredScenarioTests) never leak into these rtsp/onvif-only assertions.
    _KNOWN_DEVICE = {"manufacturer": "TestCam", "model": "X1"}

    def test_rtsp_true_onvif_true_is_approved(self):
        result = evaluate_camera_compatibility({**self._KNOWN_DEVICE, "rtsp_supported": True, "onvif_supported": True})
        self.assertEqual(result.status, APPROVED)
        self.assertEqual(_codes(result), {"rtsp_confirmed", "onvif_confirmed"})

    def test_rtsp_true_onvif_false_is_partially_supported(self):
        result = evaluate_camera_compatibility({**self._KNOWN_DEVICE, "rtsp_supported": True, "onvif_supported": False})
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)
        self.assertEqual(_codes(result), {"rtsp_confirmed", "onvif_unsupported"})

    def test_rtsp_true_onvif_none_is_partially_supported(self):
        result = evaluate_camera_compatibility({**self._KNOWN_DEVICE, "rtsp_supported": True, "onvif_supported": None})
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)
        self.assertEqual(_codes(result), {"rtsp_confirmed", "onvif_unconfirmed"})

    def test_rtsp_false_onvif_true_is_partially_supported_benefit_of_doubt(self):
        result = evaluate_camera_compatibility({**self._KNOWN_DEVICE, "rtsp_supported": False, "onvif_supported": True})
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)
        self.assertEqual(_codes(result), {"rtsp_unsupported", "onvif_confirmed"})

    def test_rtsp_false_onvif_false_is_the_only_not_supported_case(self):
        result = evaluate_camera_compatibility({**self._KNOWN_DEVICE, "rtsp_supported": False, "onvif_supported": False})
        self.assertEqual(result.status, NOT_SUPPORTED)
        self.assertEqual(_codes(result), {"rtsp_unsupported", "onvif_unsupported", "no_supported_video_transport"})

    def test_rtsp_false_onvif_none_is_partially_supported_not_not_supported(self):
        # The corrected rule: RTSP positively absent but ONVIF merely unconfirmed
        # (not positively absent) is not enough affirmative evidence for NOT_SUPPORTED.
        result = evaluate_camera_compatibility({**self._KNOWN_DEVICE, "rtsp_supported": False, "onvif_supported": None})
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)
        self.assertEqual(_codes(result), {"rtsp_unsupported", "onvif_unconfirmed"})

    def test_rtsp_none_onvif_true_is_partially_supported(self):
        result = evaluate_camera_compatibility({**self._KNOWN_DEVICE, "rtsp_supported": None, "onvif_supported": True})
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)
        self.assertEqual(_codes(result), {"rtsp_unconfirmed", "onvif_confirmed"})

    def test_rtsp_none_onvif_false_is_partially_supported_not_not_supported(self):
        result = evaluate_camera_compatibility({**self._KNOWN_DEVICE, "rtsp_supported": None, "onvif_supported": False})
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)
        self.assertEqual(_codes(result), {"rtsp_unconfirmed", "onvif_unsupported"})

    def test_rtsp_none_onvif_none_is_partially_supported_not_not_supported(self):
        # Nothing confirmed either way -- "could not prove support" must never
        # become "proved unsupported".
        result = evaluate_camera_compatibility({**self._KNOWN_DEVICE, "rtsp_supported": None, "onvif_supported": None})
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)
        self.assertEqual(_codes(result), {"rtsp_unconfirmed", "onvif_unconfirmed"})


class RequiredScenarioTests(unittest.TestCase):
    """The scenarios explicitly required for this feature."""

    def test_onvif_compatible_camera_with_required_capabilities_is_approved(self):
        result = evaluate_camera_compatibility(
            {"manufacturer": "Hikvision", "model": "DS-2CD2143G0-I", "onvif_supported": True, "rtsp_supported": True}
        )
        self.assertEqual(result.status, APPROVED)

    def test_camera_detected_but_rtsp_incomplete_is_partially_supported(self):
        result = evaluate_camera_compatibility(
            {"manufacturer": "Dahua", "model": "IPC-HDW2831T", "onvif_supported": True, "rtsp_supported": None}
        )
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)

    def test_clearly_incompatible_device_is_not_supported(self):
        # e.g. a printer or unrelated LAN host that a caller nonetheless asked
        # this engine to evaluate -- discovery.py itself would never surface
        # such a host (see module docstring), but the engine must still
        # classify it correctly if asked.
        result = evaluate_camera_compatibility({"rtsp_supported": False, "onvif_supported": False})
        self.assertEqual(result.status, NOT_SUPPORTED)

    def test_unknown_manufacturer_and_model_are_informational_only(self):
        approved = evaluate_camera_compatibility({"rtsp_supported": True, "onvif_supported": True})
        self.assertEqual(approved.status, APPROVED)
        self.assertIn("manufacturer_unknown", _codes(approved))
        self.assertIn("model_unknown", _codes(approved))
        self.assertEqual(approved.manufacturer, "Unknown")
        self.assertEqual(approved.model, "Unknown")

    def test_discovery_py_unknown_string_default_is_treated_the_same_as_missing(self):
        # discovery.py's _scope_value() emits the literal string "Unknown", not
        # None, when it can't parse a manufacturer/model out of ONVIF scopes.
        result = evaluate_camera_compatibility(
            {"manufacturer": "Unknown", "model": "Unknown", "rtsp_supported": True, "onvif_supported": True}
        )
        self.assertIn("manufacturer_unknown", _codes(result))
        self.assertIn("model_unknown", _codes(result))

    def test_onvif_timeout_or_failure_does_not_crash_evaluation(self):
        # A None onvif_supported (never confirmed either way) must be handled
        # gracefully, not raise.
        result = evaluate_camera_compatibility({"rtsp_supported": True, "onvif_supported": None})
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)

    def test_rtsp_probe_failure_does_not_crash_evaluation(self):
        result = evaluate_camera_compatibility({"rtsp_supported": None, "onvif_supported": True})
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)

    def test_wifi_camera_gets_identical_verdict_to_equivalent_wired_camera(self):
        wired = evaluate_camera_compatibility(
            {"manufacturer": "Axis", "model": "M3045-V", "onvif_supported": True, "rtsp_supported": True, "transport": "wired"}
        )
        wifi = evaluate_camera_compatibility(
            {"manufacturer": "Axis", "model": "M3045-V", "onvif_supported": True, "rtsp_supported": True, "transport": "wifi"}
        )
        self.assertEqual(wired.status, wifi.status)
        self.assertEqual(_codes(wired), _codes(wifi))
        self.assertEqual(wired.manufacturer, wifi.manufacturer)
        self.assertEqual(wired.model, wifi.model)
        self.assertEqual(wired.transport, "wired")
        self.assertEqual(wifi.transport, "wifi")


class TransportPassthroughTests(unittest.TestCase):
    def test_transport_defaults_to_unknown_when_absent(self):
        result = evaluate_camera_compatibility({"rtsp_supported": True, "onvif_supported": True})
        self.assertEqual(result.transport, "unknown")

    def test_transport_defaults_to_unknown_for_an_unrecognized_value(self):
        result = evaluate_camera_compatibility({"rtsp_supported": True, "onvif_supported": True, "transport": "bluetooth"})
        self.assertEqual(result.transport, "unknown")

    def test_transport_is_never_inferred_from_other_fields(self):
        # Two devices differing ONLY in whatever fields might tempt an inference
        # (manufacturer, model, ip-like fields aren't even part of the contract)
        # must not cause any transport value to be invented.
        result = evaluate_camera_compatibility(
            {"manufacturer": "TP-Link", "model": "Tapo C210", "onvif_supported": True, "rtsp_supported": True}
        )
        self.assertEqual(result.transport, "unknown")

    def test_transport_never_changes_status_or_reasons(self):
        base = {"rtsp_supported": False, "onvif_supported": None}
        without_transport = evaluate_camera_compatibility(base)
        with_wired = evaluate_camera_compatibility({**base, "transport": "wired"})
        with_wifi = evaluate_camera_compatibility({**base, "transport": "wifi"})
        with_garbage = evaluate_camera_compatibility({**base, "transport": "carrier-pigeon"})
        for other in (with_wired, with_wifi, with_garbage):
            self.assertEqual(without_transport.status, other.status)
            self.assertEqual(_codes(without_transport), _codes(other))


class DefensiveInputTests(unittest.TestCase):
    """Malformed/unexpected input must never raise -- a bad record must not
    block evaluation of that device or any other."""

    def test_non_dict_capabilities_input_does_not_raise(self):
        result = evaluate_camera_compatibility(None)
        self.assertEqual(result.status, PARTIALLY_SUPPORTED)

    def test_non_bool_rtsp_value_is_treated_as_unknown_not_guessed(self):
        for bogus in (1, 0, "true", "false", "yes", [], {}):
            with self.subTest(bogus=bogus):
                result = evaluate_camera_compatibility({"rtsp_supported": bogus, "onvif_supported": True})
                self.assertEqual(result.status, PARTIALLY_SUPPORTED)
                self.assertIn("rtsp_unconfirmed", _codes(result))

    def test_non_string_manufacturer_and_model_become_unknown(self):
        result = evaluate_camera_compatibility(
            {"manufacturer": 12345, "model": ["a", "list"], "rtsp_supported": True, "onvif_supported": True}
        )
        self.assertEqual(result.manufacturer, "Unknown")
        self.assertEqual(result.model, "Unknown")


class NoCredentialLeakageTests(unittest.TestCase):
    def test_output_contract_has_no_credential_shaped_fields(self):
        result = evaluate_camera_compatibility(
            {"rtsp_supported": True, "onvif_supported": True, "manufacturer": "Reolink", "model": "RLC-810A"}
        )
        payload = result.as_dict()
        for forbidden in ("rtsp_url", "username", "password", "credential", "secret"):
            self.assertNotIn(forbidden, payload)

    def test_reason_messages_never_contain_an_rtsp_url(self):
        for rtsp in (True, False, None):
            for onvif in (True, False, None):
                result = evaluate_camera_compatibility({"rtsp_supported": rtsp, "onvif_supported": onvif})
                for reason in result.reasons:
                    self.assertNotIn("rtsp://", reason.message)


class EvaluateScanResultsBatchTests(unittest.TestCase):
    """The discovery.py wire-format adapter: rtsp_support/onvif_support (no
    trailing -ed) field names, list-of-dicts in, list-of-dicts out."""

    def test_translates_discovery_field_names_and_attaches_compatibility(self):
        raw = [{
            "id": "camera-192-168-1-50", "name": "Camera 192.168.1.50", "ip": "192.168.1.50",
            "manufacturer": "Hikvision", "model": "DS-2CD2143G0-I", "mac_address": "AA:BB:CC:DD:EE:FF",
            "onvif_support": True, "rtsp_support": True, "connection_status": "reachable",
            "online": True, "recording": False, "analytics": False, "last_recording_at": None, "last_error": None,
        }]
        enriched = evaluate_scan_results(raw)
        self.assertEqual(len(enriched), 1)
        item = enriched[0]
        self.assertEqual(item["compatibility_status"], APPROVED)
        self.assertEqual(item["transport"], "unknown")
        # Original discovery fields are preserved untouched.
        self.assertEqual(item["ip"], "192.168.1.50")
        self.assertEqual(item["onvif_support"], True)
        self.assertEqual(item["rtsp_support"], True)

    def test_a_clearly_incompatible_device_is_not_supported_via_the_adapter(self):
        raw = [{"id": "camera-x", "onvif_support": False, "rtsp_support": False, "manufacturer": "Unknown", "model": "Unknown"}]
        enriched = evaluate_scan_results(raw)
        self.assertEqual(enriched[0]["compatibility_status"], NOT_SUPPORTED)

    def test_multiple_cameras_where_one_failure_does_not_affect_others(self):
        # Simulate one item's evaluation raising, via monkeypatching the pure
        # engine function the adapter calls -- proves batch isolation
        # independently of how defensive the engine already is on its own.
        from unittest.mock import patch

        raw = [
            {"id": "camera-1", "rtsp_support": True, "onvif_support": True},
            {"id": "camera-2", "rtsp_support": True, "onvif_support": True},
            {"id": "camera-3", "rtsp_support": True, "onvif_support": True},
        ]

        real_evaluate = evaluate_camera_compatibility
        call_count = {"n": 0}

        def side_effect(capabilities):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated evaluation crash for camera-2")
            return real_evaluate(capabilities)

        with patch("edge.camera_compatibility.evaluate_camera_compatibility", side_effect=side_effect):
            enriched = evaluate_scan_results(raw)

        self.assertEqual(len(enriched), 3)
        self.assertEqual(enriched[0]["compatibility_status"], APPROVED)
        self.assertEqual(enriched[1]["compatibility_status"], PARTIALLY_SUPPORTED)  # the simulated failure -- safe fallback, never NOT_SUPPORTED
        self.assertEqual(enriched[1]["id"], "camera-2")
        self.assertEqual(enriched[2]["compatibility_status"], APPROVED)

    def test_a_non_dict_item_in_the_batch_is_handled_gracefully(self):
        enriched = evaluate_scan_results([{"id": "camera-1", "rtsp_support": True, "onvif_support": True}, "not-a-dict", None])
        self.assertEqual(len(enriched), 3)
        self.assertEqual(enriched[0]["compatibility_status"], APPROVED)
        self.assertEqual(enriched[1]["compatibility_status"], PARTIALLY_SUPPORTED)
        self.assertEqual(enriched[2]["compatibility_status"], PARTIALLY_SUPPORTED)

    def test_non_list_input_returns_empty_list_rather_than_raising(self):
        self.assertEqual(evaluate_scan_results(None), [])
        self.assertEqual(evaluate_scan_results("not-a-list"), [])

    def test_wifi_and_wired_devices_through_the_adapter_get_identical_status(self):
        wired = evaluate_scan_results([{"id": "cam-wired", "rtsp_support": True, "onvif_support": True, "transport": "wired"}])[0]
        wifi = evaluate_scan_results([{"id": "cam-wifi", "rtsp_support": True, "onvif_support": True, "transport": "wifi"}])[0]
        self.assertEqual(wired["compatibility_status"], wifi["compatibility_status"])
        self.assertEqual(wired["compatibility_reasons"], wifi["compatibility_reasons"])
        self.assertEqual(wired["transport"], "wired")
        self.assertEqual(wifi["transport"], "wifi")


if __name__ == "__main__":
    unittest.main()
