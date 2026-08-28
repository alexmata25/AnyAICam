"""Regression coverage for the second half of the confirmed-live Samsung
camera_not_bound gap: CameraBindingStore.bind() existed but was never
called from anywhere in the agent -- app/appliance_cloud.py's
appliance_configuration() route now includes device_key, and cloud-side
provisioning now assigns a camera_number (see app/appliance_cloud.py's
appliance_submit_provisioning() and app/tests/
test_camera_discovery_provisioning.py's new camera_number tests), but
without something on the agent side actually writing bindings.json, the
appliance's own reconcile_cloud_cameras() would still report
last_error='camera_not_bound' forever.

auto_bind_discovered_cameras() closes that gap using only data the
appliance already has locally (this cycle's /api/appliance/configuration
response, and its own already-populated DiscoveredCameraStore) -- no
rediscovery, no re-provisioning, no operator action. device_key is the
sole matching key between a cloud camera and a discovered physical
candidate, consistent with every other identity decision in this module
(see test_camera_binding_discovery_store.py)."""

import tempfile
import unittest
from pathlib import Path

from anyaicam_agent.camera_binding import CameraBindingStore, auto_bind_discovered_cameras


def _cloud_camera(cloud_id='cam-1', camera_number=1, device_key='urn:uuid:1111', **overrides):
    base = {'id': cloud_id, 'name': 'Camera', 'camera_number': camera_number, 'device_key': device_key, 'status': 'configured'}
    base.update(overrides)
    return base


def _discovered(device_key='urn:uuid:1111', mac='14:2f:fd:a2:f6:af', **overrides):
    base = {'device_key': device_key, 'mac_address': mac, 'ip': '192.168.0.38'}
    base.update(overrides)
    return base


class AutoBindDiscoveredCamerasTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.store = CameraBindingStore(Path(self._tmpdir.name) / 'bindings.json')

    # ---- the core gap this closes

    def test_successful_provisioning_creates_the_binding(self):
        bound = auto_bind_discovered_cameras([_cloud_camera()], [_discovered()], self.store)
        self.assertEqual(bound, ['cam-1'])
        bindings = self.store.bindings()
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]['cloud_camera_id'], 'cam-1')
        self.assertEqual(bindings[0]['camera_number'], 1)
        self.assertEqual(bindings[0]['mac_address'], '14:2f:fd:a2:f6:af')

    def test_retrying_does_not_create_duplicates(self):
        for _ in range(3):
            auto_bind_discovered_cameras([_cloud_camera()], [_discovered()], self.store)
        self.assertEqual(len(self.store.bindings()), 1)

    def test_retrying_with_unchanged_data_does_not_rewrite_the_binding(self):
        """Once correctly bound, repeated cycles must be true no-ops --
        no churned approved_at timestamp on every heartbeat."""
        auto_bind_discovered_cameras([_cloud_camera()], [_discovered()], self.store)
        first_approved_at = self.store.bindings()[0]['approved_at']
        bound_again = auto_bind_discovered_cameras([_cloud_camera()], [_discovered()], self.store)
        self.assertEqual(bound_again, [], 'an already-correct binding must not be reported as (re-)bound')
        self.assertEqual(self.store.bindings()[0]['approved_at'], first_approved_at)

    def test_existing_bindings_for_other_cameras_remain_stable(self):
        auto_bind_discovered_cameras([_cloud_camera(cloud_id='cam-1', device_key='urn:uuid:1111')],
                                      [_discovered(device_key='urn:uuid:1111', mac='14:2f:fd:a2:f6:af')], self.store)
        auto_bind_discovered_cameras([_cloud_camera(cloud_id='cam-2', camera_number=2, device_key='urn:uuid:2222')],
                                      [_discovered(device_key='urn:uuid:2222', mac='14:2f:fd:60:6e:23')], self.store)
        bindings = {item['cloud_camera_id']: item for item in self.store.bindings()}
        self.assertEqual(set(bindings), {'cam-1', 'cam-2'})
        self.assertEqual(bindings['cam-1']['camera_number'], 1)
        self.assertEqual(bindings['cam-2']['camera_number'], 2)

    def test_device_key_is_the_matching_identity_not_ip_or_name(self):
        """A discovered candidate with a totally different device_key
        must never be bound to a cloud camera just because other fields
        happen to line up."""
        cloud_camera = _cloud_camera(device_key='urn:uuid:aaaa')
        discovered = _discovered(device_key='urn:uuid:bbbb')  # deliberately mismatched
        bound = auto_bind_discovered_cameras([cloud_camera], [discovered], self.store)
        self.assertEqual(bound, [])
        self.assertEqual(self.store.bindings(), [])

    def test_a_binding_that_changed_camera_number_is_updated_not_duplicated(self):
        auto_bind_discovered_cameras([_cloud_camera(camera_number=1)], [_discovered()], self.store)
        auto_bind_discovered_cameras([_cloud_camera(camera_number=5)], [_discovered()], self.store)
        bindings = self.store.bindings()
        self.assertEqual(len(bindings), 1, 'must replace the stale binding, never keep both')
        self.assertEqual(bindings[0]['camera_number'], 5)

    # ---- nothing to bind yet

    def test_camera_with_no_camera_number_yet_is_not_bound(self):
        bound = auto_bind_discovered_cameras([_cloud_camera(camera_number=None)], [_discovered()], self.store)
        self.assertEqual(bound, [])
        self.assertEqual(self.store.bindings(), [])

    def test_cloud_camera_not_yet_seen_on_this_appliances_network_is_not_bound(self):
        """A camera_number was assigned, but this appliance's own
        discovery scan hasn't (yet) seen a matching physical device --
        must not fabricate a binding out of nothing."""
        bound = auto_bind_discovered_cameras([_cloud_camera()], [], self.store)
        self.assertEqual(bound, [])
        self.assertEqual(self.store.bindings(), [])

    def test_discovered_candidate_with_unresolved_mac_is_not_bound(self):
        bound = auto_bind_discovered_cameras([_cloud_camera()], [_discovered(mac='Unknown')], self.store)
        self.assertEqual(bound, [])
        self.assertEqual(self.store.bindings(), [])


if __name__ == '__main__':
    unittest.main()
