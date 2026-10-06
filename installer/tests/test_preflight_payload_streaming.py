"""Installer self-integrity verification hashes large payload members in chunks."""
import builtins
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PREFLIGHT = Path(__file__).resolve().parents[1] / "01-preflight.sh"
CHUNK = 1024 * 1024


def _embedded_verifier():
    source = PREFLIGHT.read_text(encoding="utf-8")
    start = source.index("import hashlib, json, os, sys", source.index("verify_installer_payload"))
    end = source.index("\nPYEOF", start)
    return source[start:end]


class PreflightPayloadStreamingTests(unittest.TestCase):
    def _run_verifier(self, root, manifest, *, tracking=False):
        code = _embedded_verifier()
        sizes = []
        real_open = builtins.open

        class Reader:
            def __init__(self, handle):
                self.handle = handle

            def read(self, size=-1):
                sizes.append(size)
                if size <= 0 or size > CHUNK:
                    raise AssertionError(f"payload read requested {size} bytes")
                return self.handle.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self.handle.__exit__(*exc)

        def tracked_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            return Reader(handle) if tracking and mode == "rb" else handle

        output = io.StringIO()
        with mock.patch("builtins.open", side_effect=tracked_open), \
             mock.patch.object(sys, "argv", ["preflight", str(manifest), str(root)]), \
             contextlib.redirect_stdout(output):
            exec(compile(code, str(PREFLIGHT), "exec"), {})
        return output.getvalue(), sizes

    def test_large_payload_hash_uses_bounded_reads_and_keeps_digest_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "payload.bin"
            data = b"x" * (3 * CHUNK + 17)
            payload.write_bytes(data)
            manifest = root / "artifact-files.json"
            manifest.write_text(json.dumps([{"path": payload.name, "sha256": hashlib.sha256(data).hexdigest()}]))

            output, sizes = self._run_verifier(root, manifest, tracking=True)

            self.assertEqual(output.strip(), "1 files verified")
            self.assertTrue(sizes)
            self.assertLessEqual(max(sizes), CHUNK)

    def test_streamed_hash_still_rejects_a_changed_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "payload.bin").write_bytes(b"changed")
            manifest = root / "artifact-files.json"
            manifest.write_text(json.dumps([{"path": "payload.bin", "sha256": "0" * 64}]))

            with self.assertRaises(SystemExit) as raised:
                self._run_verifier(root, manifest)

            self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
