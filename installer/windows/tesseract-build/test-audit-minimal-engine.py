"""Policy regression tests using real synthetic normal/delay PE import tables."""
import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("audit", Path(__file__).with_name("audit-minimal-engine.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def binary(normal=(), delay=(), machine=0x8664):
    data = bytearray(4096)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x84, machine, 1)
    struct.pack_into("<H", data, 0x94, 240)
    struct.pack_into("<H", data, 0x98, 0x20B)
    struct.pack_into("<IIII", data, 0x98 + 240 + 8, 3584, 0x1000, 3584, 512)
    string = 2048
    for names, directory, cursor, width, field in ((normal, 1, 512, 20, 12), (delay, 13, 1024, 32, 4)):
        if names:
            struct.pack_into("<I", data, 0x98 + 112 + directory * 8, cursor - 512 + 0x1000)
        for name in names:
            struct.pack_into("<I", data, cursor + field, string - 512 + 0x1000)
            encoded = name.encode("ascii") + b"\0"
            data[string:string + len(encoded)] = encoded
            string += len(encoded)
            cursor += width
    return bytes(data)


class ClosureTests(unittest.TestCase):
    def test_recursive_case_insensitive_closure(self):
        needed, system = audit.closure(binary(["ONE.DLL"]), {
            "one.dll": binary(["two.dll", "KERNEL32.dll"]), "two.dll": binary()})
        self.assertEqual(needed, {"one.dll", "two.dll"})
        self.assertEqual(system, {"kernel32.dll"})

    def test_delay_load_dependency_is_included(self):
        self.assertEqual(audit.closure(binary(delay=["late.dll"]), {"late.dll": binary()})[0], {"late.dll"})

    def test_transitive_forbidden_dependency_fails(self):
        with self.assertRaisesRegex(ValueError, "Forbidden dependency"):
            audit.closure(binary(["one.dll"]), {"one.dll": binary(delay=["libiconv-2.dll"])})

    def test_unresolved_dependency_fails(self):
        with self.assertRaisesRegex(ValueError, "Unresolved dependency"):
            audit.closure(binary(["unknown.dll"]), {})

    def test_x86_provider_fails(self):
        with self.assertRaisesRegex(ValueError, "AMD64"):
            audit.closure(binary(["one.dll"]), {"one.dll": binary(machine=0x14C)})

    def test_debug_runtime_is_not_a_release_system_dependency(self):
        with self.assertRaisesRegex(ValueError, "Unresolved dependency: vcruntime140d.dll"):
            audit.closure(binary(["vcruntime140d.dll"]), {})

    def test_malformed_binary_fails(self):
        with self.assertRaises(ValueError):
            audit.closure(b"not an executable", {})

    def test_existing_audit_evidence_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "dependency-audit.json"
            report.write_text("retained evidence")
            with patch("sys.argv", ["audit", "--exe", "unused", "--inputs", "unused",
                                    "--output", str(root / "engine")]):
                with self.assertRaisesRegex(ValueError, "preserve prior evidence"):
                    audit.main()
            self.assertEqual(report.read_text(), "retained evidence")
            self.assertFalse((root / "engine").exists())


if __name__ == "__main__":
    unittest.main()
