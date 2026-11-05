"""Signed premium grants are rechecked after inference; Basic Motion stays independent."""
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.tests.test_gateway_licensing import load_functions

FLAGS = {'smart_motion':'smart_motion_enabled', 'people_counting':'people_counting_enabled',
         'lpr':'lpr_enabled', 'ppe':'ppe_enabled', 'facial_recognition':'facial_recognition_enabled'}


class GatewayDetectionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.grants = set()
        stub = SimpleNamespace(camera_analytic_grants=lambda:{'c1':set(self.grants)})
        modules = patch.dict(sys.modules, {'appliance_entitlements':stub})
        modules.start(); self.addCleanup(modules.stop)
        env = patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION':'local_gateway'})
        env.start(); self.addCleanup(env.stop)
        self.ns = {'RUNTIME_ROLE':'edge', '_PREMIUM_FIELDS':FLAGS}
        load_functions('recording_uploader.py', {'_entitled_identity'}, self.ns)
        self.identity = {'camera_id':'c1', 'site_id':'site1', 'local_recording_mode':'event', 'motion_enabled':True,
                         **{flag:True for flag in FLAGS.values()}}

    def test_unsigned_camera_flags_never_enable_premium_detection(self):
        result = self.ns['_entitled_identity'](self.identity)
        self.assertTrue(result['motion_enabled'])
        self.assertEqual(result['local_recording_mode'], 'event')
        self.assertFalse(any(result[flag] for flag in FLAGS.values()))
        self.assertTrue(self.identity['ppe_enabled'])  # cached input is not mutated

    def test_cloud_edition_keeps_basic_motion_and_denies_unsigned_premium(self):
        with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': 'cloud_gateway'}):
            result = self.ns['_entitled_identity'](self.identity)
            self.assertTrue(result['motion_enabled'])
            self.assertEqual(result['local_recording_mode'], 'event')
            self.assertFalse(any(result[flag] for flag in FLAGS.values()))

    def test_cloud_premium_grant_is_camera_specific_and_rechecked(self):
        with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION': 'cloud_gateway'}):
            self.grants.add('ppe')
            allowed = self.ns['_entitled_identity'](self.identity)
            self.assertTrue(allowed['ppe_enabled'])
            self.assertFalse(allowed['smart_motion_enabled'])
            self.assertFalse(self.ns['_entitled_identity'](
                {**self.identity, 'camera_id': 'other'})['ppe_enabled'])
            self.grants.clear()
            revoked = self.ns['_entitled_identity'](self.identity)
            self.assertFalse(revoked['ppe_enabled'])
            self.assertTrue(revoked['motion_enabled'])

    def test_grants_are_camera_specific_and_independent_of_remote_subscription(self):
        self.grants.add('ppe')
        self.assertTrue(self.ns['_entitled_identity'](self.identity)['ppe_enabled'])
        self.assertFalse(self.ns['_entitled_identity']({**self.identity,'camera_id':'other'})['ppe_enabled'])
        self.assertFalse(self.ns['_entitled_identity'](self.identity)['smart_motion_enabled'])

    def test_gateway_combined_role_cannot_bypass_signed_analytics_gate(self):
        self.ns['RUNTIME_ROLE'] = 'combined'
        self.assertFalse(self.ns['_entitled_identity'](self.identity)['ppe_enabled'])
        with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION':'full_vms'}):
            self.assertIs(self.ns['_entitled_identity'](self.identity), self.identity)

    def analytics(self):
        ns = {'recording_uploader':SimpleNamespace(_entitled_identity=self.ns['_entitled_identity']),
              '_analytic_globally_enabled':lambda flag, camera:True, 'AI_DETECTOR_ANALYTIC_FLAGS':tuple(FLAGS.values())}
        load_functions('main.py', {'effective_analytics'}, ns)
        return ns

    def test_stale_inference_identity_is_rechecked_at_consumption(self):
        self.grants.add('smart_motion')
        identity = self.ns['_entitled_identity'](self.identity)
        ns = self.analytics()
        self.assertEqual(ns['effective_analytics'](identity, 1), ['smart_motion_enabled'])
        self.grants.clear()
        self.assertEqual(ns['effective_analytics'](identity, 1), [])

    def test_entitlement_reader_failure_fails_closed(self):
        with patch.dict(sys.modules, {'appliance_entitlements':SimpleNamespace(camera_analytic_grants=Mock(side_effect=ValueError('invalid grant')))}):
            self.assertEqual(self.analytics()['effective_analytics'](self.identity, 1), [])

    def test_revoked_result_creates_no_thumbnail_event_or_clip(self):
        ns = self.analytics()
        ns['recording_uploader']._camera_identity = lambda camera:self.identity
        load_functions('main.py', {'save_yolo_events'}, ns)
        # Missing frame/model globals would fail if the revoked result reached
        # any of the event/thumbnail/clip machinery.
        self.assertEqual(ns['save_yolo_events'](1, {'detections':[{'class_name':'person'}], 'frame':object()}), [])
