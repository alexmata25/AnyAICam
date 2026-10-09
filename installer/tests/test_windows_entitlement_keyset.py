"""Windows entitlement keyset installation (installer/windows/
entitlement-keyset.ps1, 2026-10-08): the build packages only a keyset that
matches an independently obtained digest and holds public keys only; a signed
build cannot leave it out; the setup checks it again with the agent's own
validator, installs it Administrators-owned with a protected ACL after the
data folder's permissions, and lets the VMS's own check accept it. The
service launcher keeps the licensing state files in the protected data
folder. Nothing here installs anything or needs administrator rights."""
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
SCRIPT = WIN / "entitlement-keyset.ps1"


def read(name):
    return (WIN / name).read_text(encoding="utf-8")


def canonical(keys):
    return (json.dumps({"keys": dict(sorted(keys.items()))}, indent=2, sort_keys=True) + "\n").encode("ascii")


def ps(command):
    return subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                          capture_output=True, text=True, timeout=180)


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


PUBLIC = base64.b64encode(bytes(range(32))).decode()


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-keyset-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def keyset(self, data):
        path = self.tmp / "keys.json"
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()

    def check(self, path, digest):
        return ps(f". {quote(SCRIPT)}; Test-KeysetPackaging {quote(path)} {quote(digest)}; 'OK'")

    def test_a_matching_public_keyset_is_packaged(self):
        path, digest = self.keyset(canonical({"cloud-2026": PUBLIC}))
        result = self.check(path, digest)
        self.assertEqual(result.stdout.strip(), "OK", result.stderr)

    def test_refusals(self):
        path, digest = self.keyset(canonical({"cloud-2026": PUBLIC}))
        cases = {
            "does not match the given digest": (path, "0" * 64),
            "64 lowercase hex": (path, digest.upper()),
            "not found": (self.tmp / "missing.json", digest),
        }
        for message, (p, d) in cases.items():
            result = self.check(p, d)
            self.assertNotEqual(result.returncode, 0, message)
            self.assertIn(message, result.stderr + result.stdout, message)
        for data, message in ((b'{"keys": {"k": "-----BEGIN PRIVATE KEY-----"}}', "private key material"),
                              (b'{"keys": {}, "extra": 1}', 'exactly {"keys"'),
                              (b'{"keys": {}}', 'exactly {"keys"')):
            p, d = self.keyset(data)
            result = self.check(p, d)
            self.assertNotEqual(result.returncode, 0, message)
            self.assertIn(message, result.stderr + result.stdout)

    def test_the_setup_refuses_before_changing_anything(self):
        data_root = self.tmp / "AnyAiCam"
        data_root.mkdir()
        path, digest = self.keyset(canonical({"cloud-2026": PUBLIC}))
        call = (f". {quote(SCRIPT)}; Install-EntitlementKeyset -DataRoot {quote(data_root)} -Source {{0}} -Sha256 {{1}} "
                f"-Python {quote(sys.executable)} -AgentRoot {quote(ROOT / 'appliance-agent')} -AppRoot {quote(ROOT / 'app')}")
        for source, sha, message in ((path, "0" * 64, "does not match this release"),
                                     (self.tmp / "missing.json", digest, "does not contain it"),
                                     (path, "abc", "malformed entitlement keyset digest")):
            result = ps(call.format(quote(source), quote(sha)))
            self.assertNotEqual(result.returncode, 0, message)
            self.assertIn(message, result.stderr + result.stdout)
        # Not canonical (the agent's own validator) -> refused.
        loose, loose_digest = self.keyset(json.dumps({"keys": {"cloud-2026": PUBLIC}}).encode())
        result = ps(call.format(quote(loose), quote(loose_digest)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a canonical public keyset", result.stderr + result.stdout)
        self.assertFalse((data_root / "trust").exists())  # nothing was created

    def test_a_release_without_a_keyset_keeps_or_warns(self):
        data_root = self.tmp / "AnyAiCam"
        (data_root / "trust").mkdir(parents=True)
        call = f". {quote(SCRIPT)}; Install-EntitlementKeyset -DataRoot {quote(data_root)} -Source '' -Sha256 '' -Python x -AgentRoot x -AppRoot x"
        self.assertIn("No entitlement-signing keyset is provisioned", ps(call).stdout)
        (data_root / "trust" / "entitlement_signing_keys.json").write_text("{}")
        self.assertIn("left unchanged", ps(call).stdout)


class WiringTests(unittest.TestCase):
    def test_build_packages_the_keyset_and_a_signed_build_requires_it(self):
        build = read("build.ps1")
        self.assertIn("[string]$EntitlementKeyset = ''", build)
        self.assertIn("Test-KeysetPackaging $EntitlementKeyset $EntitlementKeysetSha256", build)
        self.assertIn("} elseif ($Sign -or $AzureSign) {\n    throw 'A signed build must carry the entitlement-signing keyset", build)
        self.assertIn("if (Test-Path -LiteralPath $keysetOut) { Remove-Item -LiteralPath $keysetOut -Force }", build)  # never a stale one
        self.assertLess(build.index('$compilerArguments += "/DEntitlementKeysetSha256=$keysetSha"'), build.index("& $iscc @compilerArguments"))

    def test_setup_ships_and_passes_the_keyset(self):
        iss = read("AnyAiCam-VMS.iss")
        self.assertIn('#ifndef EntitlementKeysetSha256\n  #define EntitlementKeysetSha256 ""', iss)
        self.assertIn('Source: "entitlement-keyset.ps1"; DestDir: "{app}\\installer"; Flags: ignoreversion', iss)
        self.assertIn('Source: "output\\entitlement_signing_keys.json"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist', iss)
        self.assertIn("-EntitlementKeyset \"' + ExpandConstant('{tmp}\\entitlement_signing_keys.json') + '\" -EntitlementKeysetSha256 \"{#EntitlementKeysetSha256}\"", iss)

    def test_installed_after_the_data_folder_acl_and_before_the_services(self):
        runtime = read("install-runtime.ps1")
        acl = runtime.index("& icacls.exe $DataRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'")
        install = runtime.index("Install-EntitlementKeyset -DataRoot $DataRoot")
        self.assertLess(acl, install)
        self.assertLess(runtime.index("anyaicam_agent.service, anyaicam_agent.windows"), install)  # bundled Python ready
        self.assertLess(install, runtime.index("installer\\cloud-link.ps1')"))

    def test_the_installed_acl_and_final_check(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$TrustedOwnerSid = 'S-1-5-32-544'", text)
        self.assertIn('& icacls.exe $Path /inheritance:r /grant:r @grants /C /Q', text)
        self.assertIn('@("*${SystemSid}:(OI)(CI)F", "*${TrustedOwnerSid}:(OI)(CI)F")', text)
        self.assertIn("& icacls.exe $DataRoot /setowner", text)                     # a pre-created folder is taken over
        self.assertIn("if (Test-ReparsePoint $trust) { [IO.Directory]::Delete($trust, $false) }", text)  # link removed, target untouched
        self.assertIn("release_checks.validate_entitlement_keyset", text)
        self.assertIn("w.anchor_problem(w.anchor_file()) or 'OK'", text)             # the VMS's own check
        self.assertLess(text.index("validate_entitlement_keyset"), text.index("Set-TrustAcl $trust -Folder"))

    def test_the_launcher_keeps_licensing_state_in_the_protected_folder(self):
        launcher = read("service-launcher.ps1")
        imported = launcher.index("[Environment]::SetEnvironmentVariable($parts[0], $parts[1], 'Process')")
        for name, file in (("ANYAICAM_VMS_CAPACITY_FILE", "vms_capacity.json"),
                           ("ANYAICAM_PRODUCT_MODE_STATE_FILE", "product_mode.json"),
                           ("ANYAICAM_FEATURE_ENTITLEMENTS_FILE", "feature_entitlements.json")):
            line = f"$env:{name} = Join-Path $dataRoot 'config\\{file}'"
            self.assertIn(line, launcher)
            self.assertLess(imported, launcher.index(line))  # vms.env cannot move them


if __name__ == "__main__":
    unittest.main()
