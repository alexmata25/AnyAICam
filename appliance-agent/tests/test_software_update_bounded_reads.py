"""Software Update reads untrusted files in bounded pieces (2026-10-06).

The root applier copied the staged package with read(MAX_UNPACKED_BYTES + 1)
-- an ~8 GiB allocation up front -- so an appliance with less free memory got
MemoryError and could never update. Untrusted files are now read and copied
in pieces of at most 1 MiB; the size limit, signature and hash checks, and the
"nothing changes unless everything verifies" behaviour are unchanged.

A simulated low-memory host: every read on a file the applier opens is
recorded, and any single read asking for more than LOW_MEMORY_LIMIT bytes
raises MemoryError, as it would on a small VM.
"""
import io
from pathlib import Path
from unittest import mock

from software_update_helpers import BUILD_A, BUILD_B
from test_software_update_root_applier import ApplierTestCase, ar

LOW_MEMORY_LIMIT = 64 * 1024 * 1024


class RecordingHandle:
    """Wraps a binary file opened for reading; records each read size."""

    def __init__(self, inner, log, fail_after=None):
        self.inner, self.log, self.fail_after = inner, log, fail_after

    def read(self, size=-1):
        self.log.append(size)
        if size is None or size < 0 or size > LOW_MEMORY_LIMIT:
            raise MemoryError(f"cannot allocate {size} bytes")
        if self.fail_after is not None and len(self.log) > self.fail_after:
            raise OSError(5, "Input/output error")
        return self.inner.read(size)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.inner.close()
        return False


class BoundedReadTestCase(ApplierTestCase):
    def apply(self, fail_after=None):
        reads, real_fdopen = [], ar.os.fdopen

        def fdopen(descriptor, mode="r", *args, **kwargs):
            handle = real_fdopen(descriptor, mode, *args, **kwargs)
            return RecordingHandle(handle, reads, fail_after) if "r" in mode and "b" in mode else handle
        with mock.patch.object(ar.os, "fdopen", fdopen):
            result = self.applier().apply_staged()
        return result, reads

    def staged_package(self, manifest) -> Path:
        return self.paths.staged / manifest["update_id"] / "package.tar.gz"

    def assert_nothing_changed(self, manifest):
        self.assertFalse(any(call[:2] == ["systemctl", "stop"] for call in self.host.calls), "the VMS was stopped")
        self.assertEqual(self.marker()["release_version"], "1.1.0")
        status, version, tree = self.serving()
        self.assertEqual((status, version["build_id"], tree["build_id"]), (200, BUILD_A, BUILD_A))
        self.assertFalse((self.paths.work / manifest["update_id"]).exists(), "root's work copy was left behind")
        self.assertFalse(self.paths.next.exists())


class BoundedReadTests(BoundedReadTestCase):
    def test_a_normal_update_on_a_low_memory_host_succeeds(self):
        self.stage()
        result, reads = self.apply()
        self.assertEqual(result["state"], "healthy", result.get("error"))
        self.assertEqual(self.marker()["vms_release_commit"], BUILD_B)
        self.assertTrue(reads)
        self.assertLessEqual(max(reads), ar._READ_CHUNK, f"a read asked for {max(reads)} bytes at once")

    def test_no_read_is_sized_by_the_maximum_package_size(self):
        """The limit is still ~8 GiB; no single request is."""
        self.assertGreaterEqual(ar.release_checks.MAX_UNPACKED_BYTES, 8 * 1024 ** 3)
        self.stage()
        _result, reads = self.apply()
        self.assertTrue(all(0 < size <= ar._READ_CHUNK for size in reads), sorted(set(reads))[-3:])

    def test_an_oversized_package_is_rejected_with_nothing_changed(self):
        manifest = self.stage()
        size = self.staged_package(manifest).stat().st_size
        with mock.patch.object(ar.release_checks, "MAX_UNPACKED_BYTES", size - 1):
            result, reads = self.apply()
        self.assertEqual(result["state"], "rejected")
        self.assertIn("unexpectedly large", result["error"])
        self.assert_nothing_changed(manifest)

    def test_a_truncated_package_is_rejected_with_nothing_changed(self):
        manifest = self.stage()
        package = self.staged_package(manifest)
        package.write_bytes(package.read_bytes()[: package.stat().st_size // 2])
        result, _reads = self.apply()
        self.assertEqual(result["state"], "rejected")
        self.assertIn("bad_hash", result["error"])
        self.assert_nothing_changed(manifest)

    def test_a_corrupted_package_is_rejected_with_nothing_changed(self):
        manifest = self.stage()
        package = self.staged_package(manifest)
        data = bytearray(package.read_bytes())
        data[len(data) // 2] ^= 0xFF  # same size, different bytes
        package.write_bytes(bytes(data))
        result, _reads = self.apply()
        self.assertEqual(result["state"], "rejected")
        self.assertIn("bad_hash", result["error"])
        self.assert_nothing_changed(manifest)

    def test_a_read_error_is_rejected_with_nothing_changed(self):
        manifest = self.stage()
        result, _reads = self.apply(fail_after=1)  # the first read works, the next fails
        self.assertEqual(result["state"], "rejected")
        self.assertIn("could not be read", result["error"])
        self.assert_nothing_changed(manifest)

    def test_an_oversized_small_file_is_rejected_without_reading_it_whole(self):
        manifest = self.stage()
        request = self.paths.staged / manifest["update_id"] / "request.json"
        request.write_bytes(b" " * (ar._MAX_SMALL_FILE + 10))
        result, reads = self.apply()
        self.assertEqual(result["state"], "rejected")
        self.assertIn("unexpectedly large", result["error"])
        self.assertLessEqual(max(reads, default=0), ar._READ_CHUNK)
        self.assert_nothing_changed(manifest)


def test_a_file_that_grows_past_the_limit_while_read_is_refused():
    """The byte count is enforced while reading, not only from the size
    reported when the file was opened."""
    import pytest
    pieces = ar._read_chunks(io.BytesIO(b"x" * 100), Path("package.tar.gz"), 60)
    with pytest.raises(ar.Failure, match="unexpectedly large"):
        list(pieces)
    assert b"".join(ar._read_chunks(io.BytesIO(b"x" * 60), Path("package.tar.gz"), 60)) == b"x" * 60


def test_reads_never_exceed_one_piece():
    log = []

    class Spy(io.BytesIO):
        def read(self, size=-1):
            log.append(size)
            return super().read(size)
    data = b"y" * (3 * ar._READ_CHUNK + 5)
    assert b"".join(ar._read_chunks(Spy(data), Path("p"), 8 * 1024 ** 3)) == data
    assert max(log) == ar._READ_CHUNK
