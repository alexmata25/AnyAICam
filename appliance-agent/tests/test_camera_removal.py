"""Camera removal on the agent (2026-10-08). A camera the customer removed
arrives in the configuration with status "removed" (no device_key, no
camera_number). The agent forgets its binding and leaves it out of binding,
media-URI resolution and its own camera report. Without this, re-adding the
same physical camera -- the cloud creates a new camera for it -- was refused
("already bound to another cloud camera"): the removed camera is still in
the configuration, so its binding counted as a live collision."""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anyaicam_agent import service
from anyaicam_agent.camera_binding import (CameraBindingStore, auto_bind_discovered_cameras,
                                           split_removed_cameras)
from anyaicam_agent.config import AgentConfig

FRONT = {'id': 'cam-1', 'name': 'Front door', 'camera_number': 1, 'device_key': 'urn:uuid:1111', 'status': 'configured'}
FRONT_REMOVED = {'id': 'cam-1', 'name': 'Front door', 'camera_number': None, 'device_key': None, 'status': 'removed'}
FRONT_READDED = {'id': 'cam-3', 'name': 'Front door', 'camera_number': 3, 'device_key': 'urn:uuid:1111', 'status': 'configured'}
GARAGE = {'id': 'cam-2', 'name': 'Garage', 'camera_number': 2, 'device_key': 'urn:uuid:2222', 'status': 'configured'}
DISCOVERED = [{'device_key': 'urn:uuid:1111', 'mac_address': '14:2f:fd:a2:f6:af', 'ip': '192.168.0.38'},
              {'device_key': 'urn:uuid:2222', 'mac_address': '14:2f:fd:a2:f6:b0', 'ip': '192.168.0.39'}]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = CameraBindingStore(Path(self._tmp.name) / 'bindings.json')
        auto_bind_discovered_cameras([FRONT, GARAGE], DISCOVERED, self.store)

    def test_forget_drops_only_the_named_bindings_and_is_idempotent(self):
        self.assertEqual(self.store.forget(['cam-1']), 1)
        self.assertEqual([b['cloud_camera_id'] for b in self.store.bindings()], ['cam-2'])
        self.assertEqual(self.store.forget(['cam-1', '', '  ']), 0)
        self.assertEqual(self.store.forget(['cam-unknown']), 0)

    def test_split_removed_cameras(self):
        active, removed = split_removed_cameras([FRONT_REMOVED, GARAGE, 'junk', {'status': 'removed'}, {**GARAGE, 'id': 'cam-9', 'status': 'Removed'}])
        self.assertEqual(active, [GARAGE])
        self.assertEqual(removed, ['cam-1', 'cam-9'])

    def test_without_forgetting_a_re_added_camera_cannot_bind(self):
        # The defect: the removed camera's binding still claims the device.
        bound = auto_bind_discovered_cameras([FRONT_REMOVED, FRONT_READDED, GARAGE], DISCOVERED, self.store)
        self.assertNotIn('cam-3', bound)

    def test_after_forgetting_the_re_added_camera_binds(self):
        active, removed = split_removed_cameras([FRONT_REMOVED, FRONT_READDED, GARAGE])
        self.store.forget(removed)
        bound = auto_bind_discovered_cameras(active, DISCOVERED, self.store)
        self.assertEqual(bound, ['cam-3'])
        by_id = {b['cloud_camera_id']: b for b in self.store.bindings()}
        self.assertEqual(set(by_id), {'cam-2', 'cam-3'})
        self.assertEqual(by_id['cam-3']['camera_number'], 3)


class SyncConfigurationTests(unittest.TestCase):
    """service.ApplianceAgent.sync_configuration() end to end, cloud mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        folder = Path(self._tmp.name)
        self.config = AgentConfig(cloud_id='AIC-1', portal_url='https://portal.example', state_dir=str(folder),
                                  config_dir=str(folder), log_dir=str(folder))
        self.agent = service.ApplianceAgent(self.config)
        self.agent.log = logging.getLogger('test.camera_removal')
        self.agent.discovered_store.save_scan(DISCOVERED)
        self.agent.resolve_media_uris = mock.Mock()

    def sync(self, cameras):
        with mock.patch.object(self.agent.client, 'request', return_value={'cameras': cameras}):
            self.agent.sync_configuration()

    def reported(self):
        return {c.get('cloud_camera_id') or c.get('id') for c in json.loads(self.config.cameras_file.read_text(encoding='utf-8'))}

    def test_removal_forgets_the_binding_and_drops_the_camera_from_the_report(self):
        self.sync([FRONT, GARAGE])
        self.assertEqual({b['cloud_camera_id'] for b in self.agent.binding_store.bindings()}, {'cam-1', 'cam-2'})
        self.sync([FRONT_REMOVED, GARAGE])
        self.assertEqual({b['cloud_camera_id'] for b in self.agent.binding_store.bindings()}, {'cam-2'})
        self.assertNotIn('cam-1', self.reported())
        self.assertEqual(self.agent.resolve_media_uris.call_args[0][0], [GARAGE])  # removed never resolved

    def test_the_same_device_re_added_as_a_new_camera_binds(self):
        self.sync([FRONT, GARAGE])
        self.sync([FRONT_REMOVED, FRONT_READDED, GARAGE])
        by_id = {b['cloud_camera_id']: b for b in self.agent.binding_store.bindings()}
        self.assertEqual(set(by_id), {'cam-2', 'cam-3'})
        self.assertEqual(by_id['cam-3']['device_key'], 'urn:uuid:1111')

    def test_repeated_syncs_are_stable(self):
        self.sync([FRONT, GARAGE])
        for _ in range(3):
            self.sync([FRONT_REMOVED, GARAGE])
        self.assertEqual({b['cloud_camera_id'] for b in self.agent.binding_store.bindings()}, {'cam-2'})


if __name__ == '__main__':
    unittest.main()
