"""Windows 1.3.2 (Blue): camera Reconnect (camera_address_recovery) and recording checks run a bare `ffprobe` (CAM-10a).

On Windows that works only because (1) the pinned FFmpeg archive ships ffprobe.exe in the same folder as ffmpeg.exe,
(2) install-runtime.ps1 copies that whole folder into runtime\\tools\\ffmpeg, and (3) service-launcher.ps1 puts that
folder first on the VMS process PATH. The agent itself never runs ffmpeg/ffprobe.
"""
import re
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
ARCHIVE = WIN / "vendor" / "ffmpeg-8.1.2-essentials_build.zip"


def read(name):
    return (WIN / name).read_text(encoding="utf-8")


class ReconnectFfprobeTests(unittest.TestCase):
    def test_the_vms_launcher_puts_the_bundled_ffmpeg_folder_first_on_path(self):
        self.assertRegex(read("service-launcher.ps1"),
                         r"\$env:PATH = \(Join-Path \$installRoot 'runtime\\tools\\ffmpeg'\) \+ ';' \+ \$env:PATH")

    def test_install_runtime_copies_every_file_next_to_ffmpeg_exe(self):
        text = read("install-runtime.ps1")
        self.assertIn("$ffmpegRoot = Join-Path $InstallRoot 'runtime\\tools\\ffmpeg'", text)
        self.assertIn("Copy-Item -Path (Join-Path $ffmpeg.Directory.FullName '*') -Destination $ffmpegRoot", text)

    def test_the_build_pins_the_archive_that_is_installed(self):
        pinned = re.search(r"'ffmpeg-8\.1\.2-essentials_build\.zip'\s*=\s*'([0-9a-f]{64})'", read("build.ps1"))
        self.assertIsNotNone(pinned)
        self.assertIn('vendor\\ffmpeg-8.1.2-essentials_build.zip', read("AnyAiCam-VMS.iss"))

    @unittest.skipUnless(ARCHIVE.exists(), "vendored FFmpeg archive not present (it is fetched for a build)")
    def test_the_pinned_archive_ships_ffprobe_beside_ffmpeg(self):
        import hashlib
        pinned = re.search(r"'ffmpeg-8\.1\.2-essentials_build\.zip'\s*=\s*'([0-9a-f]{64})'", read("build.ps1")).group(1)
        digest = hashlib.sha256()
        with ARCHIVE.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        self.assertEqual(digest.hexdigest(), pinned)
        with zipfile.ZipFile(ARCHIVE) as archive:
            folders = {}
            for name in archive.namelist():
                folders.setdefault(name.rsplit("/", 1)[-1], set()).add(name.rsplit("/", 1)[0])
        self.assertEqual(folders["ffmpeg.exe"], folders["ffprobe.exe"])


if __name__ == "__main__":
    unittest.main()
