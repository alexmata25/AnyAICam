"""Third-party rules 1.3.4.1 must keep: Tesseract stays, its notices and FFmpeg's
ship, and the plate-detection model, whose licence is unresolved, stays out."""
from pathlib import Path

WINDOWS = Path(__file__).resolve().parents[2] / 'installer' / 'windows'
SETUP = (WINDOWS / 'AnyAiCam-VMS.iss').read_text(encoding='utf-8')
DEPENDENCIES = (WINDOWS / 'DEPENDENCIES.md').read_text(encoding='utf-8')


def test_tesseract_stays_under_apache_2():
    row = next(line for line in DEPENDENCIES.splitlines() if line.startswith('| Tesseract OCR engine bundle'))
    assert 'Apache-2.0' in row and 'Tesseract 5.5.2' in row


def test_the_opencv_ffmpeg_dll_is_declared_and_its_notice_ships():
    assert 'opencv_videoio_ffmpeg4100_64.dll' in DEPENDENCIES
    assert 'Source: "notices\\opencv-ffmpeg-NOTICE.txt"; DestDir: "{app}\\licenses"' in SETUP
    notice = (WINDOWS / 'notices' / 'opencv-ffmpeg-NOTICE.txt').read_text(encoding='utf-8')
    assert 'LGPLv2.1' in notice and 'libvpx' in notice and 'libaom' in notice


def test_the_source_offer_is_marked_unfinished_until_the_mirror_exists():
    assert '[PENDING' in (WINDOWS / 'notices' / 'opencv-ffmpeg-NOTICE.txt').read_text(encoding='utf-8')


def test_the_plate_detection_model_is_not_in_the_installer_inputs():
    for name in ('AnyAiCam-VMS.iss', 'requirements-windows.txt', 'DEPENDENCIES.md'):
        assert 'license-plate-finetune' not in (WINDOWS / name).read_text(encoding='utf-8'), name
