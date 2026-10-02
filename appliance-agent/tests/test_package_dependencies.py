import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python <3.11 fallback, matches requires-python floor
    import tomli as tomllib

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class PackageDependencyTests(unittest.TestCase):
    # Regression coverage for the appliance-agent clean-install
    # release blocker found in live Phase 4 validation: the
    # anyaicam-agent systemd service entrypoint crashed with
    # ModuleNotFoundError: No module named 'cryptography' because
    # pyproject.toml declared dependencies = [] despite
    # updater/verify.py importing cryptography unconditionally, and
    # that import is reachable from service.py at process start
    # (service -> updater.factory -> updater.state_machine ->
    # updater.verify). Both checks below must pass together: the
    # manifest declaring the dependency is necessary but not
    # sufficient proof -- a fresh install actually working is what
    # matters.

    def test_pyproject_declares_cryptography(self):
        with open(PACKAGE_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        dependencies = data["project"]["dependencies"]
        self.assertIn(
            "cryptography",
            [dep.split(">")[0].split("=")[0].split("<")[0].strip() for dep in dependencies],
            "pyproject.toml must declare cryptography as a runtime dependency -- "
            "updater/verify.py imports it unconditionally and that import is "
            "reachable from the anyaicam-agent service entrypoint at process start.",
        )

    def test_fresh_install_entrypoint_imports_successfully(self):
        # Builds a real, throwaway venv and does a real `pip install
        # <package_root>` -- exactly reproducing the failure mode found
        # on the disposable Ubuntu 24.04 host -- then confirms the
        # actual anyaicam-agent entrypoint imports without error.
        # Skips gracefully (does not fail) if this environment has no
        # network access or no venv module, rather than papering over
        # either with a false pass.
        with tempfile.TemporaryDirectory() as tmp:
            venv_dir = Path(tmp) / "venv"
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                self.skipTest(f"venv module unavailable in this environment: {result.stderr}")

            venv_python = venv_dir / "bin" / "python"
            install = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "--no-cache-dir", "-q", str(PACKAGE_ROOT)],
                capture_output=True, text=True, timeout=300,
            )
            if install.returncode != 0 and (
                "Could not fetch URL" in install.stderr or "Network is unreachable" in install.stderr
            ):
                self.skipTest(f"no network access to PyPI in this environment: {install.stderr[-500:]}")
            self.assertEqual(
                install.returncode, 0,
                f"fresh `pip install {PACKAGE_ROOT}` into a clean venv failed:\n{install.stderr}",
            )

            check = subprocess.run(
                [str(venv_python), "-c", "from anyaicam_agent.service import main"],
                capture_output=True, text=True,
            )
            self.assertEqual(
                check.returncode, 0,
                "the anyaicam-agent entrypoint (anyaicam_agent.service:main) failed to "
                f"import after a fresh install:\n{check.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
