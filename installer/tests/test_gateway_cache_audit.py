from pathlib import Path
import hashlib
import tempfile
import unittest

from tools.audit_windows_gateway_cache import audit, bounded_match, inventory, requirements


class GatewayCacheAuditTests(unittest.TestCase):
    def test_package_normalization_and_cpu_versions(self):
        self.assertEqual(requirements('typing_extensions==4.16.0\n# comment'), {'typing-extensions': '4.16.0'})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            win = root / 'installer/windows'
            win.mkdir(parents=True)
            (win / 'requirements-windows.txt').write_text('torch==2.5.1+cpu\n')
            (win / 'build.ps1').write_text("$pinned = [ordered]@{\n'dep.zip' = '" + 'b' * 64 + "'\n}")
            (win / 'wheels.lock.sha256').write_text('a' * 64 + '  torch-2.5.1+cpu-cp312-cp312-win_amd64.whl\n')
            for name in ('requirements.txt', 'requirements-cpu.txt', 'requirements-push.txt'):
                (root / name).write_text('torch==2.5.1\n')
            report = audit(root, [root / 'missing'])
            self.assertEqual(report['windows_manifest_mismatches'], 0)
            self.assertEqual(report['top_level_mismatches'], 0)
            self.assertEqual(report['requirements_without_wheel'], [])
            self.assertEqual(report['copied_files'], 0)

    def test_exact_filename_does_not_imply_approved_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'p.whl'
            path.write_bytes(b'fake')
            found, scans = inventory([Path(folder)], {'p.whl'})
            self.assertEqual(found['p.whl'], [str(path)])
            self.assertFalse(scans[0]['truncated'])
            self.assertEqual(bounded_match(path, hashlib.sha256(b'fake').hexdigest()), 'hash_matches')
            self.assertEqual(bounded_match(path, '0' * 64), 'hash_mismatch')
            self.assertEqual(bounded_match(path, '0' * 64, max_bytes=1), 'hash_deferred_size_limit')

    def test_depth_limit_is_reported_as_partial_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'nested').mkdir()
            _, scans = inventory([Path(folder)], set(), max_depth=0)
            self.assertTrue(scans[0]['truncated'])
