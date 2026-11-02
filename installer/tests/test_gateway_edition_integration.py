"""Isolated edition validation and real transport entry points, no VMS imports."""
import ast
import __future__
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'app'))
import gateway_edition


class GatewayIntegrationTests(unittest.TestCase):
    def test_real_installer_mode_block_keeps_existing_modes_and_refuses_switch(self):
        runtime = (ROOT / 'installer/windows/install-runtime.ps1').read_text(encoding='utf-8')
        block = runtime[runtime.index("$savedMode = ''"):runtime.index('# What /version reports')]
        script = """
        $ErrorActionPreference = 'Stop'
        $decide = {
            param($values, $hadEnvironmentFile, $ProductMode)
            %s
            return $values
        }
        foreach ($mode in @('local','hybrid')) {
            $v = & $decide @{} $false $mode
            if ($v['ANYAICAM_PRODUCT_MODE_BOOTSTRAP'] -cne $mode) { throw 'new bootstrap lost' }
            $v = & $decide @{'ANYAICAM_PRODUCT_MODE'=$mode} $true ''
            if ($v['ANYAICAM_PRODUCT_MODE'] -cne $mode) { throw 'saved mode lost' }
        }
        $refused = $false
        try { & $decide @{'ANYAICAM_PRODUCT_MODE'='local'} $true 'hybrid' } catch { $refused = $true }
        if (-not $refused) { throw 'mode switch accepted' }
        $v = & $decide @{} $true ''
        if ($v.Count -ne 0) { throw 'legacy mode changed' }
        """ % block
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_event_upload_keeps_local_capture_and_entitlement_boundaries(self):
        tree = ast.parse((ROOT / 'app/event_media_uploader.py').read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'upload_motion_event_media')
        for edition, opted_in, camera_mode in [
            ('local_gateway', True, 'motion'),
            ('cloud_gateway', False, 'motion'),
            ('cloud_gateway', True, 'disabled'),
            ('cloud_gateway', True, 'motion'),
        ]:
            with self.subTest(edition=edition, opted_in=opted_in, mode=camera_mode), patch.dict(os.environ, {
                gateway_edition.EDITION_ENV: edition,
                'ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED': str(opted_in).lower(),
            }):
                recording = Mock()
                recording._camera_identity.return_value = {'cloud_recording_mode': camera_mode, 'camera_id': 'c1'}
                recording._ensure_session.return_value = None
                outbox = Mock()
                namespace = {
                    'EVENT_MEDIA_CAPTURE_ENABLED': True, 'EVENT_MEDIA_UPLOAD_ENABLED': True,
                    '_edition_upload_allowed': lambda: gateway_edition.video_transport_allowed('event'),
                    '_event_camera_authorized': lambda number, identity: True,
                    '_event_upload_session': recording._ensure_session,
                    '_safe_recording_url': lambda value: value,
                    '_local_path_from_recording_url': lambda value: Path('fake.mp4') if value else None,
                    'event_media_outbox': outbox, 'recording_upload': recording, 'logger': Mock(),
                }
                exec(compile(ast.Module(body=[function], type_ignores=[]), 'event_media_uploader.py', 'exec', flags=__future__.annotations.compiler_flag), namespace)
                result = namespace['upload_motion_event_media'](event_id='e1', camera_number=1,
                    event_start=datetime(2026, 11, 1), event_end=datetime(2026, 11, 1, 0, 0, 10),
                    clip_url='/recordings/clips/motion/motion_e1.mp4', thumbnail_url=None, already_classified=True)
                outbox.put.assert_called_once()
                if edition == 'local_gateway' or not opted_in:
                    self.assertTrue(result)
                    recording._refresh_camera_map.assert_not_called()
                else:
                    self.assertFalse(result)
                    if camera_mode == 'disabled':
                        recording._ensure_session.assert_not_called()
                    else:
                        recording._ensure_session.assert_called_once_with(1, 'c1')

    def test_installer_preserves_and_validates_saved_selection(self):
        helper = ROOT / 'installer/windows/gateway-edition.ps1'
        script = """
        $ErrorActionPreference = 'Stop'
        . '%s'
        foreach ($edition in @('full_vms','local_gateway','cloud_gateway')) {
            $v = @{}; Set-GatewayEdition $v $edition
            if ($v['ANYAICAM_GATEWAY_EDITION'] -cne $edition) { throw 'selection not saved' }
            Set-GatewayEdition $v ''
            if ($v.Count -ne 1) { throw 'unexpected flags written' }
            $refused = $false
            try { Set-GatewayEdition $v 'invalid' } catch { $refused = $true }
            if (-not $refused) { throw 'invalid selection accepted' }
        }
        $v = @{'ANYAICAM_GATEWAY_EDITION'='local_gateway'}
        $refused = $false
        try { Set-GatewayEdition $v 'cloud_gateway' } catch { $refused = $true }
        if (-not $refused) { throw 'edition switch accepted' }
        $v = @{}; Set-GatewayEdition $v ''
        if ($v.Count -ne 0) { throw 'legacy upgrade changed' }
        $v = @{'ANYAICAM_GATEWAY_EDITION'='broken'}
        $refused = $false
        try { Set-GatewayEdition $v '' } catch { $refused = $true }
        if (-not $refused) { throw 'invalid saved edition accepted' }
        """ % helper
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_gateway_upload_entry_points_reject_before_accessing_client_or_file(self):
        for source, name, args in [
            ('recording_uploader.py', '_upload_recording', (Mock(), {}, None, None, 1)),
            ('live_relay_uploader.py', '_upload_segment', ({}, None)),
            ('main.py', 'upload_cloud_recording_job', ({},)),
        ]:
            tree = ast.parse((ROOT / 'app' / source).read_text(encoding='utf-8'))
            function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            for arg in function.args.args:
                arg.annotation = None
            function.returns = None
            namespace = {}
            exec(compile(ast.Module(body=[function], type_ignores=[]), source, 'exec'), namespace)
            for edition in ('local_gateway', 'cloud_gateway'):
                with self.subTest(source=source, edition=edition), patch.dict(os.environ, {gateway_edition.EDITION_ENV: edition}):
                    with self.assertRaises(PermissionError):
                        namespace[name](*args)

    def test_installer_wires_edition_before_service_changes(self):
        source = (ROOT / 'installer/windows/AnyAiCam-VMS.iss').read_text(encoding='utf-8')
        preflight = source.split('function PrepareToInstall', 1)[1]
        self.assertLess(preflight.index('GatewayEditionProblem()'), preflight.index('ProductModeProblem()'))
        self.assertIn('Source: "gateway-edition.ps1"', source)
        self.assertIn('-GatewayEdition "\' + GatewayEditionChoice()', source)
        runtime = (ROOT / 'installer/windows/install-runtime.ps1').read_text(encoding='utf-8')
        self.assertLess(runtime.index('Set-GatewayEdition -Values'), runtime.index('[IO.File]::WriteAllLines($environmentFile'))


if __name__ == '__main__':
    unittest.main()
