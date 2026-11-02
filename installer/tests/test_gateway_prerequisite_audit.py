"""Prerequisite inventory tests use tiny fixtures; no build tools are executed."""
from pathlib import Path
import tempfile
import unittest

from tools.audit_windows_gateway_prerequisites import audit, locked_wheels, pinned_inputs


class PrerequisiteAuditTests(unittest.TestCase):
    def test_lock_rejects_traversal_duplicates_and_empty_input(self):
        pin = 'a' * 64
        for value in ('', f'{pin}  ../outside.whl', f'{pin}  a.whl\n{pin}  a.whl', 'not-a-pin  a.whl'):
            with self.assertRaises(ValueError):
                locked_wheels(value)
        self.assertEqual(locked_wheels(f'{pin}  test.whl\n'), ['test.whl'])

    def test_pins_are_derived_from_build_script(self):
        source = "$pinned = [ordered]@{\n'a.zip' = '" + 'b' * 64 + "'\n}"
        self.assertEqual(pinned_inputs(source), ['a.zip'])

    def test_presence_does_not_claim_integrity_or_build_readiness(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            win = root / 'installer/windows'
            win.mkdir(parents=True)
            (win / 'build.ps1').write_text("$pinned = [ordered]@{\n'a.zip' = '" + 'b' * 64 + "'\n}")
            (win / 'wheels.lock.sha256').write_text('c' * 64 + '  a.whl\n')
            report = audit(root, env={'LOCALAPPDATA': str(root)}, which=lambda name: None)
            self.assertIn('inno_setup_7', report['missing_dependencies'])
            self.assertIn('build_git', report['missing_dependencies'])
            self.assertEqual(report['checks']['vendor_inputs']['missing'], ['a.zip'])
            self.assertEqual(report['checks']['wheels']['missing'], ['a.whl'])
            compiler = root / 'Programs/Inno Setup 7/ISCC.exe'
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b'not an executable')
            report = audit(root, env={'LOCALAPPDATA': str(root)}, which=lambda name: None)
            self.assertNotIn('inno_setup_7', report['missing_dependencies'])
            self.assertEqual(report['build_readiness'], 'NOT_VALIDATED')
            self.assertFalse(report['checks']['vendor_inputs']['hashes_verified'])
            self.assertFalse(report['r430_resource_window']['approval_requested'])
