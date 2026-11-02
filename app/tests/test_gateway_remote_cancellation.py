"""Gateway cancellation uses authenticated configuration, never a local licence override."""
import asyncio
from contextlib import contextmanager
import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import threading
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from app.tests.test_gateway_licensing import load_functions
import product_mode as pm


class GatewayRemoteCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        state = patch.object(pm, 'STATE_FILE', Path(self.temp.name) / 'mode.json')
        state.start(); self.addCleanup(state.stop)
        env = patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION':'cloud_gateway',
            pm.PRODUCT_MODE_ENV:'hybrid', pm.BOOTSTRAP_MODE_ENV:'hybrid', 'ANYAICAM_LIVE_P2P_ENABLED':'true'})
        env.start(); self.addCleanup(env.stop)
        denied = patch.object(pm, '_GATEWAY_REMOTE_DENIED', False)
        denied.start(); self.addCleanup(denied.stop)

    def test_bootstrap_and_local_licence_do_not_enable_remote_service(self):
        self.assertEqual(pm.current_mode(), 'hybrid')
        self.assertFalse(pm.remote_access_authorized())
        self.assertFalse(pm._resolve('ANYAICAM_LIVE_P2P_ENABLED', True))

    def test_non_object_mode_cache_cannot_crash_startup_or_configuration_recovery(self):
        for value in (None, [], 'hybrid', 1, True):
            with self.subTest(value=value):
                pm.STATE_FILE.write_text(json.dumps(value))
                with patch.dict(os.environ, {pm.PRODUCT_MODE_ENV:''}):
                    self.assertEqual(pm.current_mode(), 'hybrid')  # bootstrap only
                    self.assertFalse(pm.remote_access_authorized())
                    with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION':'full_vms', pm.BOOTSTRAP_MODE_ENV:''}):
                        self.assertEqual(pm.current_mode(), '')  # legacy default
                self.assertTrue(pm.apply_configuration_mode({'product_mode':'local'}))
                self.assertEqual(json.loads(pm.STATE_FILE.read_text()), {'mode':'local'})

    def test_login_offline_grace_boundary_is_independent_of_remote_mode(self):
        ns = {'timedelta':timedelta, 'get_ttl_config':lambda:{'offline_grace_hours':72}}
        load_functions('appliance_identity.py', {'session_within_offline_grace'}, ns)
        accepted = datetime(2026, 11, 1, tzinfo=timezone.utc)
        check = ns['session_within_offline_grace']
        self.assertTrue(check(last_verified_at=accepted, now=accepted+timedelta(hours=72)))
        self.assertFalse(check(last_verified_at=accepted, now=accepted+timedelta(hours=72, microseconds=1)))
        self.assertFalse(pm.remote_access_authorized())

    def test_mode_change_publishes_complete_state_at_replace_boundary(self):
        pm.persist_mode('hybrid')
        replace = os.replace
        observed = []
        def publish(source, target):
            observed.append((pm._read_persisted_mode(), json.loads(Path(source).read_text())))
            replace(source, target)
        with patch.object(pm.os, 'replace', side_effect=publish):
            self.assertTrue(pm.persist_mode('local'))
        self.assertEqual(observed, [('hybrid', {'mode':'local'})])
        self.assertEqual(pm._read_persisted_mode(), 'local')
        self.assertFalse(pm.remote_access_authorized())
        self.assertEqual(list(pm.STATE_FILE.parent.glob('*.tmp')), [])

    def test_failed_mode_flush_preserves_last_accepted_state(self):
        pm.persist_mode('local')
        with patch.object(pm.os, 'fsync', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):
                pm.persist_mode('hybrid')
        self.assertEqual(pm._read_persisted_mode(), 'local')
        self.assertFalse(pm.remote_access_authorized())
        self.assertEqual(list(pm.STATE_FILE.parent.glob('*.tmp')), [])

    def test_failed_mode_replace_preserves_last_accepted_state(self):
        pm.persist_mode('local')
        with patch.object(pm.os, 'replace', side_effect=PermissionError('fixture replace refusal')):
            with self.assertRaises(PermissionError):
                pm.persist_mode('hybrid')
        self.assertEqual(pm._read_persisted_mode(), 'local')
        self.assertFalse(pm.remote_access_authorized())
        self.assertEqual(list(pm.STATE_FILE.parent.glob('*.tmp')), [])

    def test_failed_cancellation_denies_remote_before_io_and_preserves_lan_live_view(self):
        ns = {'asyncio':asyncio, 'load_maintenance_state':lambda:{'enabled':False},
              'JSONResponse':lambda **kwargs:kwargs}
        load_functions('main.py', {'maintenance_mode_middleware'}, ns)
        async def serve(request):
            return 'local HLS reached'
        for edition in ('local_gateway', 'cloud_gateway'):
            for operation in ('fsync', 'replace'):
                with self.subTest(edition=edition, operation=operation), patch.dict(os.environ, {
                        'ANYAICAM_GATEWAY_EDITION':edition,
                        'ANYAICAM_WIREGUARD_GATEWAY_ADDRESS':'10.70.0.1'}):
                    pm.apply_configuration_mode({'product_mode':'hybrid'})
                    def failed_io(*args):
                        self.assertFalse(pm.remote_access_authorized())
                        raise OSError('fixture cancellation write failure')
                    with patch.object(pm.os, operation, side_effect=failed_io):
                        with self.assertRaises(OSError):
                            pm.apply_configuration_mode({'product_mode':''})
                    self.assertEqual(pm._read_persisted_mode(), 'hybrid')
                    self.assertFalse(pm.remote_access_authorized())
                    self.assertFalse(pm._resolve('ANYAICAM_LIVE_P2P_ENABLED', True))
                    remote = SimpleNamespace(client=SimpleNamespace(host='10.70.0.1'))
                    self.assertEqual(asyncio.run(ns['maintenance_mode_middleware'](remote, serve))['status_code'], 403)
                    local = SimpleNamespace(client=SimpleNamespace(host='192.168.1.50'),
                                            url=SimpleNamespace(path='/static/hls/camera1.m3u8'))
                    self.assertEqual(asyncio.run(ns['maintenance_mode_middleware'](local, serve)), 'local HLS reached')
                    with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION':'full_vms'}):
                        self.assertTrue(pm.remote_access_authorized())
                        self.assertTrue(pm._resolve('ANYAICAM_LIVE_P2P_ENABLED', False))

    def test_failed_cancellation_requires_successful_explicit_cloud_grant_to_reopen(self):
        pm.apply_configuration_mode({'product_mode':'hybrid'})
        with patch.object(pm.os, 'replace', side_effect=OSError('fixture replace failure')):
            with self.assertRaises(OSError):
                pm.apply_configuration_mode({'product_mode':'local'})
        pm.apply_configuration_mode({})
        pm.persist_mode('hybrid')  # direct cache use is not an authenticated reactivation
        self.assertFalse(pm.remote_access_authorized())
        pm.apply_configuration_mode({'product_mode':'invalid'})
        pm.persist_mode('hybrid')
        self.assertFalse(pm.remote_access_authorized())
        pm.persist_mode('local')
        with patch.object(pm.os, 'replace', side_effect=OSError('fixture fresh grant failure')):
            with self.assertRaises(OSError):
                pm.apply_configuration_mode({'product_mode':'hybrid'})
        self.assertFalse(pm.remote_access_authorized())
        pm.apply_configuration_mode({'product_mode':'hybrid'})
        self.assertTrue(pm.remote_access_authorized())

    def test_failed_cancellation_preserves_licensed_camera_source_and_local_playback(self):
        import vms_capacity
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("CREATE TABLE cameras(id,name,camera_number,former_camera_number); CREATE TABLE recordings(id,camera_id,s3_key,status); INSERT INTO cameras VALUES('c1','Test',1,NULL); INSERT INTO recordings VALUES('r1','c1','prefix/camera1_test.mkv','available');")
        @contextmanager
        def connection():
            yield db
        root = Path(self.temp.name) / 'recordings'
        (root / 'camera1').mkdir(parents=True)
        (root / 'camera1/camera1_test.mkv').write_bytes(b'fixture')
        cloud = Mock(side_effect=AssertionError('local playback must not request cloud URL'))
        ns = {'Path':Path, 'RECORDINGS_FOLDER':root, '_presigned_recording_url':cloud,
              'get_camera_numbers':lambda:[1], '_provisioned_camera_stream':lambda number:{
                  'rtsp_url':'rtsp://test-camera/stream', 'username':'', 'password':''},
              'credentialed_rtsp_url':lambda url, user, password:url,
              'CameraNotLicensedError':PermissionError, 'CameraNotConfiguredError':LookupError}
        load_functions('main.py', {'camera_url', '_customer_recording_url', '_customer_local_recording_path'}, ns)
        for edition in ('local_gateway', 'cloud_gateway'):
            with self.subTest(edition=edition), patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION':edition}):
                capacity = vms_capacity._record({'camera_slot_quantity':0, 'vms_license_capacity':1}, 'signed')
                pm.apply_configuration_mode({'product_mode':'hybrid'})
                with patch.object(pm.os, 'replace', side_effect=OSError('fixture cancellation failure')):
                    with self.assertRaises(OSError):
                        pm.apply_configuration_mode({'product_mode':''})
                self.assertFalse(pm.remote_access_authorized())
                with patch.object(vms_capacity, 'load_capacity', return_value=capacity), \
                        patch.dict(sys.modules, {'partner_db':SimpleNamespace(connection=connection)}):
                    self.assertEqual(ns['camera_url'](1), 'rtsp://test-camera/stream')
                    with self.assertRaises(PermissionError):
                        ns['camera_url'](2)
                    self.assertEqual(ns['_customer_recording_url']('c1', 'r1'), '/api/customer/recordings/c1/r1/local')
                cloud.assert_not_called()

    def test_existing_renewal_grace_deadline_is_not_redefined(self):
        ns = {'datetime':datetime, 'GRACE_PERIOD':timedelta(days=7)}
        load_functions('billing_status.py', {'grace_ends_at'}, ns)
        check = ns['grace_ends_at']
        self.assertEqual(check('2026-11-01T00:00:00+00:00'), datetime(2026,11,8,tzinfo=timezone.utc))
        self.assertIsNone(check(None))
        self.assertIsNone(check('invalid'))

    def test_only_active_per_camera_plan_supplies_remote_mode(self):
        current = {'status':'active', 'plan_key':'fixture'}
        ns = {'entitlement_for_customer':lambda customer:current,
              'PLANS':{'fixture':{'mode':'hybrid'}}}
        load_functions('per_camera_billing.py', {'product_mode'}, ns)
        self.assertEqual(ns['product_mode']('fixture-customer'), 'hybrid')
        for status in ('suspended', 'canceled', 'incomplete'):
            current['status'] = status
            self.assertEqual(ns['product_mode']('fixture-customer'), '')

    def test_wireguard_cancellation_preserves_lan_and_full_vms(self):
        with patch.dict(os.environ, {'ANYAICAM_WIREGUARD_GATEWAY_ADDRESS':'10.70.0.1'}):
            self.assertTrue(pm.gateway_tunnel_request_denied('10.70.0.1'))
            self.assertFalse(pm.gateway_tunnel_request_denied('192.168.1.50'))
            self.assertFalse(pm.gateway_tunnel_request_denied('127.0.0.1'))
            pm.apply_configuration_mode({'product_mode':'hybrid'})
            self.assertFalse(pm.gateway_tunnel_request_denied('10.70.0.1'))
            pm.apply_configuration_mode({'product_mode':''})
            self.assertTrue(pm.gateway_tunnel_request_denied('10.70.0.1'))
            with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION':'full_vms'}):
                self.assertFalse(pm.gateway_tunnel_request_denied('10.70.0.1'))

    def test_cancelled_tunnel_stops_before_static_hls_or_maintenance_io(self):
        ns = {}
        load_functions('main.py', {'maintenance_mode_middleware'}, ns)
        ns['JSONResponse'] = lambda **kwargs: kwargs
        ns['asyncio'] = asyncio
        ns['load_maintenance_state'] = Mock(side_effect=AssertionError('must not read maintenance state'))
        call_next = Mock(side_effect=AssertionError('must not serve static media'))
        request = SimpleNamespace(client=SimpleNamespace(host='10.70.0.1'))
        with patch.dict(os.environ, {'ANYAICAM_WIREGUARD_GATEWAY_ADDRESS':'10.70.0.1'}):
            response = asyncio.run(ns['maintenance_mode_middleware'](request, call_next))
        self.assertEqual(response['status_code'], 403)
        call_next.assert_not_called()

    def test_mapped_ipv4_tunnel_cannot_bypass_cancellation(self):
        with patch.dict(os.environ, {'ANYAICAM_WIREGUARD_GATEWAY_ADDRESS':'10.70.0.1'}):
            self.assertTrue(pm.gateway_tunnel_request_denied('::ffff:10.70.0.1'))
            self.assertFalse(pm.gateway_tunnel_request_denied('::ffff:192.168.1.50'))
            pm.apply_configuration_mode({'product_mode':'hybrid'})
            self.assertFalse(pm.gateway_tunnel_request_denied('::ffff:10.70.0.1'))

    def test_configured_ipv6_gateway_matches_only_its_peer(self):
        for gateway, peer in [('::ffff:10.70.0.1', '10.70.0.1'), ('fd70::1', 'fd70:0:0:0:0:0:0:1')]:
            with self.subTest(gateway=gateway), patch.dict(os.environ, {'ANYAICAM_WIREGUARD_GATEWAY_ADDRESS':gateway}):
                self.assertTrue(pm.gateway_tunnel_request_denied(peer))
                self.assertFalse(pm.gateway_tunnel_request_denied('fd70::2'))

    def test_explicit_cancel_overrules_admin_mode_and_feature_override(self):
        self.assertTrue(pm.apply_configuration_mode({'product_mode':'hybrid'}))
        self.assertTrue(pm.remote_access_authorized())
        self.assertTrue(pm._resolve('ANYAICAM_LIVE_P2P_ENABLED', False))
        self.assertTrue(pm.apply_configuration_mode({'product_mode':''}))
        self.assertEqual(pm.current_mode(), 'hybrid')  # administrative mode still retained
        self.assertFalse(pm.remote_access_authorized())
        self.assertFalse(pm._resolve('ANYAICAM_LIVE_P2P_ENABLED', True))

    def test_old_configuration_omission_is_not_confused_with_explicit_cancellation(self):
        pm.apply_configuration_mode({'product_mode':'hybrid'})
        self.assertFalse(pm.apply_configuration_mode({}))
        self.assertTrue(pm.remote_access_authorized())
        for value in (None, 'unknown', 'local'):
            pm.apply_configuration_mode({'product_mode':'hybrid'})
            pm.apply_configuration_mode({'product_mode':value})
            self.assertFalse(pm.remote_access_authorized())

    def test_reactivation_and_admin_disable(self):
        pm.apply_configuration_mode({'product_mode':'local'})
        pm.apply_configuration_mode({'product_mode':'hybrid'})
        self.assertTrue(pm.remote_access_authorized())
        with patch.dict(os.environ, {pm.PRODUCT_MODE_ENV:'local'}):
            self.assertFalse(pm.remote_access_authorized())
        with patch.dict(os.environ, {'ANYAICAM_LIVE_P2P_ENABLED':'false'}):
            self.assertFalse(pm._resolve('ANYAICAM_LIVE_P2P_ENABLED', True))

    def test_full_vms_retains_legacy_override_and_empty_mode_behavior(self):
        with patch.dict(os.environ, {'ANYAICAM_GATEWAY_EDITION':'full_vms'}):
            pm.persist_mode('hybrid')
            self.assertFalse(pm.apply_configuration_mode({'product_mode':''}))
            self.assertTrue(pm._resolve('ANYAICAM_LIVE_P2P_ENABLED', False))

    def test_malformed_mode_state_fails_closed(self):
        pm.STATE_FILE.write_text('[]', encoding='utf-8')
        self.assertFalse(pm.remote_access_authorized())

    def publisher(self):
        response = Mock(); response.status = 204
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        http = Mock(return_value=response)
        ns = {'product_mode':pm, 'MEDIAMTX_WEBRTC_BASE':'http://127.0.0.1:8889',
              '_lock':threading.Lock(), '_whep_sessions':{}, '_known_paths':{'c1'},
              'logger':Mock(), 'urllib':SimpleNamespace(request=SimpleNamespace(Request=urllib.request.Request, urlopen=http), error=urllib.error),
              'whep_offer':Mock(return_value=('http://127.0.0.1:8889/c1/whep/token', 'answer')),
              '_control_plane_post':Mock(), '_forward_client_ice_candidate':Mock()}
        load_functions('webrtc_publisher.py', {'_gateway_runtime','_remote_access_allowed','_close_remote_whep_sessions','_handle_pending_signal'}, ns)
        return ns, http

    def test_cancel_closes_only_remote_session_and_denies_new_offer(self):
        ns, http = self.publisher()
        ns['_whep_sessions']['s1'] = 'http://127.0.0.1:8889/c1/whep/token'
        pm.apply_configuration_mode({'product_mode':'local'})
        ns['_handle_pending_signal'](Mock(), {'session_id':'s2','camera_id':'c1','kind':'offer','payload':{'sdp':'offer'}})
        self.assertEqual(ns['_whep_sessions'], {})
        self.assertEqual(ns['_known_paths'], {'c1'})  # LAN paths unchanged
        self.assertEqual(http.call_args.args[0].method, 'DELETE')
        ns['whep_offer'].assert_not_called(); ns['_control_plane_post'].assert_not_called()

    def test_cancel_during_offer_never_publishes_answer(self):
        pm.apply_configuration_mode({'product_mode':'hybrid'})
        ns, http = self.publisher()
        def offer(*args):
            pm.apply_configuration_mode({'product_mode':'local'})
            return 'http://127.0.0.1:8889/c1/whep/token', 'answer'
        ns['whep_offer'].side_effect = offer
        ns['_handle_pending_signal'](Mock(), {'session_id':'s1','camera_id':'c1','kind':'offer','payload':{'sdp':'offer'}})
        ns['_control_plane_post'].assert_not_called()
        http.assert_called_once(); self.assertEqual(ns['_whep_sessions'], {})

    def test_failed_close_is_retained_for_retry_and_foreign_origin_is_not_contacted(self):
        ns, http = self.publisher()
        ns['_whep_sessions'] = {'s1':'http://127.0.0.1:8889/c1/whep/token', 'foreign':'https://untrusted.invalid/token'}
        http.side_effect = OSError('mock unavailable')
        ns['_close_remote_whep_sessions']()
        self.assertEqual(len(ns['_whep_sessions']), 2)
        self.assertEqual(http.call_count, 1)
        http.side_effect = None
        ns['_close_remote_whep_sessions']()
        self.assertEqual(set(ns['_whep_sessions']), {'foreign'})

    def test_malformed_session_location_does_not_block_other_session_teardown(self):
        ns, http = self.publisher()
        ns['_whep_sessions'] = {'invalid':'http://[invalid/capability-secret',
                                'valid':'http://127.0.0.1:8889/c1/whep/token'}
        ns['_close_remote_whep_sessions']()
        self.assertEqual(set(ns['_whep_sessions']), {'invalid'})
        http.assert_called_once()
        self.assertNotIn('capability-secret', str(ns['logger'].mock_calls))

    def test_guard_closes_sessions_independently_of_blocked_control_poll(self):
        class TickComplete(Exception): pass
        async def to_thread(function): function()
        async def sleep(delay): raise TickComplete()
        close = Mock()
        ns = {'asyncio':SimpleNamespace(to_thread=to_thread, sleep=sleep),
              '_remote_access_allowed':lambda:False, '_close_remote_whep_sessions':close, 'SCAN_SECONDS':1}
        load_functions('webrtc_publisher.py', {'_remote_access_guard'}, ns)
        with self.assertRaises(TickComplete): asyncio.run(ns['_remote_access_guard']())
        close.assert_called_once()

    def test_guard_retries_unexpected_cleanup_failure_without_logging_capabilities(self):
        class TickComplete(BaseException): pass
        ticks = []
        async def to_thread(function): function()
        async def sleep(delay):
            ticks.append(delay)
            if len(ticks) == 2: raise TickComplete()
        close = Mock(side_effect=[RuntimeError('http://local/capability-secret'), None])
        log = Mock()
        ns = {'asyncio':SimpleNamespace(to_thread=to_thread, sleep=sleep, CancelledError=asyncio.CancelledError),
              '_remote_access_allowed':lambda:False, '_close_remote_whep_sessions':close,
              'SCAN_SECONDS':1, 'logger':log}
        load_functions('webrtc_publisher.py', {'_remote_access_guard'}, ns)
        with self.assertRaises(TickComplete): asyncio.run(ns['_remote_access_guard']())
        self.assertEqual(close.call_count, 2)
        self.assertEqual(ticks, [1, 1])
        log.warning.assert_called_once_with('webrtc_publisher.remote_session_cleanup_retry')
        self.assertNotIn('capability-secret', str(log.mock_calls))

    def test_guard_cancellation_propagates_without_retry(self):
        async def to_thread(function): raise asyncio.CancelledError()
        sleep = Mock()
        ns = {'asyncio':SimpleNamespace(to_thread=to_thread, sleep=sleep, CancelledError=asyncio.CancelledError),
              '_remote_access_allowed':lambda:False, '_close_remote_whep_sessions':Mock(),
              'SCAN_SECONDS':1, 'logger':Mock()}
        load_functions('webrtc_publisher.py', {'_remote_access_guard'}, ns)
        with self.assertRaises(asyncio.CancelledError): asyncio.run(ns['_remote_access_guard']())
        sleep.assert_not_called()
        ns['logger'].warning.assert_not_called()

    def test_dormant_gateway_worker_can_activate_without_restarting_vms(self):
        class TickComplete(BaseException): pass
        allowed = [False]
        sleeps = []
        async def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) > 1: raise TickComplete()
            allowed[0] = True
        async def guard(): pass
        async def gather(*args, **kwargs): return []
        guard_task = Mock()
        def create_task(coroutine):
            coroutine.close()  # no background task in this isolated test
            return guard_task
        start = Mock(side_effect=TickComplete())
        stop = Mock()
        ns = {'RUNTIME_ROLE':'edge', 'LIVE_P2P_ENABLED':False, 'SCAN_SECONDS':1,
              '_gateway_runtime':lambda:True, '_remote_access_allowed':lambda:allowed[0],
              '_remote_access_guard':guard, '_ensure_mediamtx_running':start,
              'stop_mediamtx':stop, 'webrtc_publisher_state':{}, 'logger':Mock(),
              'asyncio':SimpleNamespace(sleep=sleep, create_task=create_task, gather=gather, CancelledError=asyncio.CancelledError)}
        load_functions('webrtc_publisher.py', {'webrtc_publisher_worker'}, ns)
        with self.assertRaises(TickComplete): asyncio.run(ns['webrtc_publisher_worker'](Mock()))
        self.assertEqual(sleeps, [1])
        start.assert_called_once()
        guard_task.cancel.assert_called_once()

    def test_cancelled_tunnel_token_is_refused_before_file_or_secret_lookup(self):
        class Refused(Exception):
            def __init__(self, status_code, detail): self.status_code = status_code
        pm.apply_configuration_mode({'product_mode':'local'})
        ns = {'HTTPException':Refused}
        load_functions('main.py', {'appliance_media_fetch_endpoint'}, ns)
        with self.assertRaises(Refused) as error:
            ns['appliance_media_fetch_endpoint']('/recordings/test.mp4', 9999999999, 'old-token')
        self.assertEqual(error.exception.status_code, 403)
