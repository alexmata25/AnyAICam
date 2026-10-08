"""Zero-terminal onboarding (2026-10-08): installer/14-desktop-setup.sh puts
"AnyAiCam Setup" on the appliance's desktop -- a launcher, an autostart entry
(opens the agent's local setup page at sign-in until the appliance is linked)
and an applications-menu entry. These run the real shell functions and the
real launcher against temporary paths, a fake xdg-open and a fake agent status
endpoint; they never touch the system."""
import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("TEST_BASH") or shutil.which("bash")


@unittest.skipUnless(BASH, "bash is required")
class DesktopSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-desktop-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.launcher = self.tmp / "bin" / "anyaicam-setup-page"
        self.autostart = self.tmp / "xdg" / "autostart" / "anyaicam-setup.desktop"
        self.menu = self.tmp / "applications" / "anyaicam-setup.desktop"

    def bash_path(self, path):
        return subprocess.run([BASH, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"', "_", str(path)],
                              capture_output=True, text=True).stdout.strip()

    def run_functions(self, body, url="http://127.0.0.1:8790/", expect_ok=True):
        prelude = textwrap.dedent(f'''
            set -euo pipefail
            log() {{ printf 'LOG %s\\n' "$*"; }}
            DESKTOP_SETUP_LAUNCHER='{self.bash_path(self.launcher)}'
            DESKTOP_SETUP_AUTOSTART='{self.bash_path(self.autostart)}'
            DESKTOP_SETUP_MENU='{self.bash_path(self.menu)}'
            DESKTOP_SETUP_URL='{url}'
            source ./14-desktop-setup.sh
        ''')
        result = subprocess.run([BASH, "-c", prelude + textwrap.dedent(body)], cwd=ROOT, text=True, capture_output=True)
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    # ---------------------------------------------------------------- files
    def test_installs_launcher_autostart_and_menu_entries(self):
        self.run_functions("desktop_setup_provision")
        launcher = self.launcher.read_text()
        self.assertIn("URL='http://127.0.0.1:8790/'", launcher)
        self.assertIn('exec xdg-open "$URL"', launcher)
        autostart = self.autostart.read_text()
        self.assertIn(f"Exec={self.bash_path(self.launcher)} --autostart", autostart)
        self.assertIn("NoDisplay=true", autostart)
        self.assertIn("Terminal=false", autostart)
        menu = self.menu.read_text()
        self.assertIn("Name=AnyAiCam Setup", menu)
        self.assertIn(f"Exec={self.bash_path(self.launcher)}\n", menu)
        self.assertIn("Terminal=false", menu)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.launcher.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(self.menu.stat().st_mode), 0o644)
        for text in (launcher, autostart, menu):  # nothing to copy or type, ever
            for secret in ("claim code", "Cloud ID", "activation token", "label_claim"):
                self.assertNotIn(secret.lower(), text.lower())

    def test_refuses_to_write_through_a_symlink(self):
        if os.name == "nt":
            self.skipTest("symlink semantics are checked on Linux")
        target = self.tmp / "elsewhere"
        target.write_text("untouched")
        self.launcher.parent.mkdir(parents=True)
        self.launcher.symlink_to(target)
        result = self.run_functions("desktop_setup_provision", expect_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual(target.read_text(), "untouched")

    def test_uninstall_removes_the_entries(self):
        self.run_functions("desktop_setup_provision\ndesktop_setup_remove")
        for path in (self.launcher, self.autostart, self.menu):
            self.assertFalse(path.exists(), path)

    # ---------------------------------------------------------------- the launcher itself
    def run_launcher(self, linked, *args):
        state = {"linked": linked}

        class Status(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(state).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def log_message(self, *a):
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Status)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        self.run_functions("desktop_setup_provision", url=url)
        fakes = self.tmp / "fakes"; fakes.mkdir(exist_ok=True)
        opened = self.tmp / "opened.txt"
        (fakes / "xdg-open").write_text(f'#!/bin/sh\necho "$1" >> \'{self.bash_path(opened)}\'\n', newline="\n")
        (fakes / "python3").write_text(f'#!/bin/sh\nexec \'{self.bash_path(sys.executable)}\' "$@"\n', newline="\n")
        for f in fakes.iterdir():
            f.chmod(0o755)
        result = subprocess.run([BASH, "-c", f'PATH="{self.bash_path(fakes)}:$PATH" "$0" "$@"', self.bash_path(self.launcher), *args],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return opened.read_text().split() if opened.exists() else [], url

    def test_autostart_opens_the_setup_page_while_not_linked(self):
        opened, url = self.run_launcher(False, "--autostart")
        self.assertEqual(opened, [url])

    def test_autostart_stays_quiet_once_linked(self):
        opened, _ = self.run_launcher(True, "--autostart")
        self.assertEqual(opened, [])

    def test_menu_entry_always_opens_the_page(self):
        opened, url = self.run_launcher(True)
        self.assertEqual(opened, [url])

    # ---------------------------------------------------------------- wiring
    def test_install_uninstall_and_package_wiring(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('source "$INSTALLER_DIR/14-desktop-setup.sh"', install)
        self.assertLess(install.index("    cloud_portal_provision\n"), install.index("    desktop_setup_provision\n"))
        self.assertIn('open \\"AnyAiCam Setup\\"', install)
        self.assertIn("desktop_setup_remove", (ROOT / "uninstall.sh").read_text(encoding="utf-8"))
        self.assertIn('"14-desktop-setup.sh",', (ROOT / "build_release_installer.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
