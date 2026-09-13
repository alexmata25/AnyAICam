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

from anyaicam_agent.camera_binding import CameraBindingStore, LocalVmsStatusReader, auto_bind_discovered_cameras, reconcile_cloud_cameras


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


# --------------------------------------------------------------------------
# Orphan-binding self-healing (2026-09-13): confirmed live on Ryzen
# (AIC-C814766E) -- an appliance re-enrolled under a brand-new customer/
# cloud_id kept its PRIOR identity's camera_bindings.json entries forever
# (nothing ever cleared them; see reenrollment.coordinated_reenroll()'s
# own fix for the other half of this), permanently blocking a real,
# currently-valid camera from ever claiming the same physical MAC/
# camera_number. auto_bind_discovered_cameras() now tells bind() the
# current, authoritative set of cloud_camera_ids so it can tell a
# genuinely stale binding (its own cloud_camera_id isn't in that set at
# all) apart from a real, still-relevant collision -- only the former may
# ever be silently superseded.


class OrphanBindingSelfHealingTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.store = CameraBindingStore(Path(self._tmpdir.name) / 'bindings.json')

    def test_orphan_binding_is_superseded_when_its_cloud_camera_no_longer_exists(self):
        # Old identity's camera occupied this MAC/camera_number...
        auto_bind_discovered_cameras([_cloud_camera(cloud_id='old-cam-1', camera_number=1)], [_discovered()], self.store)
        # ...then the appliance is re-enrolled: 'old-cam-1' is gone from
        # the cloud's own configuration entirely, replaced by a genuinely
        # new cloud camera at the same camera_number, same physical MAC.
        bound = auto_bind_discovered_cameras([_cloud_camera(cloud_id='new-cam-1', camera_number=1)], [_discovered()], self.store)
        self.assertEqual(bound, ['new-cam-1'])
        bindings = self.store.bindings()
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]['cloud_camera_id'], 'new-cam-1')
        self.assertEqual(bindings[0]['camera_number'], 1)

    def test_active_conflicting_binding_still_blocks_a_different_camera(self):
        """The other half of the same fix: a binding is only ever
        superseded when its own cloud_camera_id is genuinely gone -- two
        currently valid cloud cameras must never be allowed to silently
        share one physical camera."""
        auto_bind_discovered_cameras([_cloud_camera(cloud_id='cam-1', camera_number=1)], [_discovered()], self.store)
        # 'cam-1' is STILL part of the current cloud configuration this
        # cycle (both passed together) -- a second, different camera
        # trying to claim the same MAC/camera_number must be refused.
        bound = auto_bind_discovered_cameras(
            [_cloud_camera(cloud_id='cam-1', camera_number=1), _cloud_camera(cloud_id='cam-2', camera_number=1, device_key='urn:uuid:2222')],
            [_discovered(), _discovered(device_key='urn:uuid:2222', mac='14:2f:fd:a2:f6:af')],
            self.store,
        )
        self.assertNotIn('cam-2', bound)
        bindings = {item['cloud_camera_id']: item for item in self.store.bindings()}
        self.assertEqual(set(bindings), {'cam-1'})

    def test_direct_bind_call_without_valid_ids_keeps_the_original_conservative_behavior(self):
        """A caller that never supplies valid_cloud_camera_ids (the
        parameter's default) must see the exact pre-fix behavior --
        always a hard collision, never a silent supersede."""
        self.store.bind('old-cam-1', 1, '14:2f:fd:a2:f6:af')
        with self.assertRaises(ValueError):
            self.store.bind('new-cam-1', 1, '14:2f:fd:a2:f6:af')

    def test_camera_1_real_scenario_stale_identity_no_longer_blocks_the_new_customers_camera(self):
        """The exact real-world case this fix closes: Camera 1
        (AIC-C814766E, device_key ...a2f6af) confirmed live -- the same
        physical device's MAC was already bound to an orphaned camera id
        from a fully-released, prior customer identity ('7e34833a37'),
        permanently blocking the new customer's own camera
        ('dfba6a63ec') from ever binding to camera_number 1."""
        old_cloud_id, new_cloud_id = '7e34833a37', 'dfba6a63ec'
        device_key, mac = 'urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af', '14:2f:fd:a2:f6:af'
        discovered = [_discovered(device_key=device_key, mac=mac, ip='192.168.0.38')]
        # Simulates the pre-existing stale state: bound under the old
        # identity, which is no longer part of the current configuration.
        auto_bind_discovered_cameras([_cloud_camera(cloud_id=old_cloud_id, camera_number=1, device_key=device_key)], discovered, self.store)
        bound = auto_bind_discovered_cameras([_cloud_camera(cloud_id=new_cloud_id, camera_number=1, device_key=device_key)], discovered, self.store)
        self.assertEqual(bound, [new_cloud_id])
        bindings = self.store.bindings()
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]['cloud_camera_id'], new_cloud_id)
        self.assertEqual(bindings[0]['mac_address'], mac)


# --------------------------------------------------------------------------
# Diagnostic narrowing (2026-09-13): reconcile_cloud_cameras() must
# distinguish "we can see this physical device but something is blocking
# the bind" from "genuinely never seen on this network at all" -- both
# looked identical (generic 'camera_not_bound') before this, hiding a
# real, actionable condition from cloud-side diagnostics.


class ReconcileCloudCamerasDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.hls = Path(self._tmpdir.name) / 'hls'
        self.recordings = Path(self._tmpdir.name) / 'recordings'
        self.hls.mkdir()
        self.recordings.mkdir()
        self.status_reader = LocalVmsStatusReader(self.hls, self.recordings)

    def test_unbound_camera_with_discovery_evidence_reports_binding_conflict_not_generic(self):
        cloud_camera = _cloud_camera(cloud_id='dfba6a63ec', camera_number=1, device_key='urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af')
        discovered = [_discovered(device_key='urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af')]
        reconciled = reconcile_cloud_cameras([cloud_camera], discovered, [], self.status_reader)
        self.assertEqual(reconciled[0]['last_error'], 'camera_binding_conflict')

    def test_unbound_camera_never_discovered_still_reports_generic_camera_not_bound(self):
        cloud_camera = _cloud_camera(cloud_id='dfba6a63ec', camera_number=1, device_key='urn:uuid:never-seen')
        reconciled = reconcile_cloud_cameras([cloud_camera], [], [], self.status_reader)
        self.assertEqual(reconciled[0]['last_error'], 'camera_not_bound')

    def test_bound_camera_with_a_live_stream_still_reports_correctly(self):
        """The diagnostic addition must never interfere with the
        existing, already-working bound-and-streaming path."""
        (self.hls / 'camera1.m3u8').write_text('#EXTM3U')
        cloud_camera = _cloud_camera(cloud_id='dfba6a63ec', camera_number=1, device_key='urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af')
        discovered = [_discovered(device_key='urn:uuid:b3464000-5074-11b4-82cc-142ffda2f6af')]
        binding = [{'cloud_camera_id': 'dfba6a63ec', 'camera_number': 1, 'mac_address': '14:2f:fd:a2:f6:af'}]
        reconciled = reconcile_cloud_cameras([cloud_camera], discovered, binding, self.status_reader)
        self.assertTrue(reconciled[0]['online'])
        self.assertIsNone(reconciled[0]['last_error'])


if __name__ == '__main__':
    unittest.main()
