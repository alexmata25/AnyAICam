"""Small PowerShell config/JSON comparisons only; never inspects an installed VMS."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from installer.tests.test_windows_acceptance_tools_1341 import BASE_STATE

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / 'installer/windows'


class GatewayConfigurationTests(unittest.TestCase):
    def test_fresh_install_modes_and_saved_configuration(self):
        script = """
        $ErrorActionPreference = 'Stop'
        . '%s'
        foreach ($edition in @('local_gateway','cloud_gateway')) {
            $expected = if ($edition -eq 'local_gateway') { 'local' } else { 'hybrid' }
            if ((Resolve-GatewayProductMode $edition '' $false) -cne $expected) { throw 'missing default' }
            if ((Resolve-GatewayProductMode $edition $expected $false) -cne $expected) { throw 'explicit mode lost' }
            $opposite = if ($expected -eq 'local') { 'hybrid' } else { 'local' }
            $refused = $false
            try { Resolve-GatewayProductMode $edition $opposite $false } catch { $refused = $true }
            if (-not $refused) { throw 'conflicting fresh-install mode accepted' }
            if ((Resolve-GatewayProductMode $edition $opposite $true) -cne $opposite) { throw 'existing mode changed' }
            $values = [ordered]@{'ANYAICAM_GATEWAY_EDITION'=$edition;'RETENTION_DAYS'='19';'ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED'='false'}
            Set-GatewayEdition $values ''
            if ($values.Count -ne 3 -or $values['RETENTION_DAYS'] -cne '19' -or $values['ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED'] -cne 'false') { throw 'saved settings changed' }
        }
        if ((Resolve-GatewayProductMode 'full_vms' 'local' $false) -cne 'local') { throw 'full VMS local changed' }
        if ((Resolve-GatewayProductMode 'full_vms' 'hybrid' $false) -cne 'hybrid') { throw 'full VMS hybrid changed' }
        if ((Resolve-GatewayProductMode '' '' $true) -cne '') { throw 'legacy upgrade changed' }
        """ % (WIN / 'gateway-edition.ps1')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_lifecycle_compare_detects_edition_upload_and_retention_changes(self):
        before = copy.deepcopy(BASE_STATE)
        public = before['data']['vms_env_public']
        public.update(ANYAICAM_GATEWAY_EDITION='local_gateway', ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED='false', RETENTION_DAYS='19')
        before['data']['vms_env_keys'] = list(public)
        for key, value, expected in [(None, None, 0), ('ANYAICAM_GATEWAY_EDITION', 'cloud_gateway', 1),
            ('ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED', 'true', 1), ('RETENTION_DAYS', '7', 1)]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temp:
                after = copy.deepcopy(before)
                if key:
                    after['data']['vms_env_public'][key] = value
                b, a, report = (Path(temp) / name for name in ('before.json', 'after.json', 'result.json'))
                b.write_text(json.dumps(before), encoding='utf-8')
                a.write_text(json.dumps(after), encoding='utf-8')
                result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                    str(WIN / 'acceptance/Compare-AnyAiCamInstallState.ps1'), '-Before', str(b), '-After', str(a),
                    '-Step', 'Repair', '-OutputPath', str(report)], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                data = json.loads(report.read_text(encoding='utf-8-sig'))
                if key:
                    failed = [item for item in data['checks'] if not item['passed']]
                    self.assertEqual(len(failed), 1)
                    self.assertIn(key, failed[0]['detail'])

    def test_snapshot_allowlist_records_boundaries_without_credentials(self):
        source = (WIN / 'acceptance/Get-AnyAiCamInstallState.ps1').read_text(encoding='utf-8')
        allowlist = source.split('$PublicEnvKeys = @(', 1)[1].split(')', 1)[0]
        for key in ('ANYAICAM_GATEWAY_EDITION', 'ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED', 'ANYAICAM_RECORDING_UPLOAD_ENABLED',
                    'ANYAICAM_LIVE_RELAY_ENABLED', 'ANYAICAM_CLOUD_UPLOAD_ENABLED', 'RETENTION_DAYS'):
            self.assertIn("'" + key + "'", allowlist)
        for secret in ('ANYAICAM_CAMERA_CREDENTIAL_KEY', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'):
            self.assertNotIn(secret, allowlist)


if __name__ == '__main__':
    unittest.main()
