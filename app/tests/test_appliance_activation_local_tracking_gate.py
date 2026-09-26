"""appliance_activation.local_activation_tracking_applies(): the gate
added to fix a confirmed-live defect where the cloud (multi-tenant)
claim_complete()/activate_appliance() endpoints called persist_activation()
-- a single-tenant "this box's own identity" mechanism -- unconditionally.
Confirmed on anyaicam-staging (2026-09-12): a real, independent Ryzen
claim completed its database transaction successfully, but the HTTP
response was then discarded with a 409 "already activated as
AIC-C90CF0C9", a leftover local identity file from an unrelated, earlier
activation test against that same shared cloud process -- which had
nothing to do with the device actually being claimed.

Pure-function unit coverage, independent of the heavier real-HTTP
end-to-end coverage in test_claim_flow_end_to_end.py, which additionally
proves the gate's effect through the real claim_complete()/
activate_appliance() call sites.
"""
import os
import unittest
from unittest.mock import patch

from appliance_activation import local_activation_tracking_applies


class LocalActivationTrackingGateTests(unittest.TestCase):
    def test_defaults_true_when_runtime_role_is_unset(self):
        # Same env var and same "edge" default as cloud_config.Settings.
        # runtime_role and main.py's own RUNTIME_ROLE constant -- an
        # appliance that never sets ANYAICAM_RUNTIME_ROLE is an edge
        # appliance, and single-tenant identity tracking must stay on
        # for it exactly as it always has.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ANYAICAM_RUNTIME_ROLE', None)
            self.assertTrue(local_activation_tracking_applies())

    def test_true_for_edge(self):
        with patch.dict(os.environ, {'ANYAICAM_RUNTIME_ROLE': 'edge'}):
            self.assertTrue(local_activation_tracking_applies())

    def test_true_for_combined(self):
        with patch.dict(os.environ, {'ANYAICAM_RUNTIME_ROLE': 'combined'}):
            self.assertTrue(local_activation_tracking_applies())

    def test_false_for_cloud(self):
        with patch.dict(os.environ, {'ANYAICAM_RUNTIME_ROLE': 'cloud'}):
            self.assertFalse(local_activation_tracking_applies())

    def test_case_and_whitespace_insensitive(self):
        with patch.dict(os.environ, {'ANYAICAM_RUNTIME_ROLE': '  Cloud  '}):
            self.assertFalse(local_activation_tracking_applies())
        with patch.dict(os.environ, {'ANYAICAM_RUNTIME_ROLE': 'EDGE'}):
            self.assertTrue(local_activation_tracking_applies())

    def test_read_fresh_not_cached(self):
        """Must not be decided once at import time -- a single process
        (this repo's own monolith) needs to see a change without being
        restarted, and tests need to override it per case."""
        with patch.dict(os.environ, {'ANYAICAM_RUNTIME_ROLE': 'edge'}):
            self.assertTrue(local_activation_tracking_applies())
        with patch.dict(os.environ, {'ANYAICAM_RUNTIME_ROLE': 'cloud'}):
            self.assertFalse(local_activation_tracking_applies())


if __name__ == '__main__':
    unittest.main()
