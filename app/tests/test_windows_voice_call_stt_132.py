"""Windows 1.3.2 (Blue, Codex FAIL on ef93b503): Voice Call speech-to-text with the bundled Vosk model.

No Voice Call or AACO code changes: these tests run the unchanged app/aac_voice_call_listen.py.
* Runtime discovery: on Windows the default STT_MODEL_PATH is the folder the setup installs
  (%ProgramData%\\AnyAiCam\\models\\stt\\vosk-model-small-en-us-0.15); on Linux it stays /opt/anyaicam-stt-model/...
* Transcription: the pinned model (from the vendored archive, verified against its pin and the per-file manifest)
  transcribes a recorded spoken answer (fixtures/voice-call-answer-package-16k.wav, 16 kHz mono PCM, made with the
  Windows speech synthesizer) through VoskTranscriber and through listen_once.
* Missing model: the transcriber is unavailable and listen_once skips. Corrupted model: listen_once fails soft
  (None, no exception), as it already does on Linux.
"""
import hashlib
import importlib
import os
import shutil
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import aac_voice_call_listen as listen
import platform_paths

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "installer" / "windows" / "vendor" / "vosk-model-small-en-us-0.15.zip"
MANIFEST = ROOT / "installer" / "windows" / "vosk-model-small-en-us-0.15.sha256"
FIXTURE = Path(__file__).parent / "fixtures" / "voice-call-answer-package-16k.wav"
NAME = "vosk-model-small-en-us-0.15"

try:
    import vosk  # noqa: F401
    HAVE_VOSK = True
except ImportError:
    HAVE_VOSK = False

needs_model = pytest.mark.skipif(not (HAVE_VOSK and ARCHIVE.exists()),
                                 reason="needs the vosk wheel and the vendored pinned model archive")


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory):
    """The pinned model, extracted exactly as the setup verifies it (archive pin + every file's SHA-256)."""
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() == lines[0].split("  ", 1)[0]
    expected = dict(reversed(line.split("  ", 1)) for line in lines[1:])
    root = tmp_path_factory.mktemp("stt")
    with zipfile.ZipFile(ARCHIVE) as archive:
        archive.extractall(root)
    found = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    assert found == expected
    return root / NAME


def _pcm():
    with wave.open(str(FIXTURE), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        return audio.readframes(audio.getnframes())


def test_the_fixture_is_the_committed_recording():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == "46ef059e8146b3e798f3cfbe4cf3d88e7237a32d7ad116d636427a0eaa2040ae"
    assert listen.speech_present(_pcm())                 # passes the same speech gate a real visitor answer must


def test_default_model_location_matches_the_setup_on_windows_and_is_unchanged_on_linux(monkeypatch):
    monkeypatch.delenv("ANYAICAM_STT_MODEL", raising=False)
    root = Path(r"C:\ProgramData\AnyAiCam")
    assert platform_paths.native_path(f"/opt/anyaicam-stt-model/{NAME}", "models", "stt", NAME, windows=True, root=root) \
        == str(root / "models" / "stt" / NAME)
    assert platform_paths.native_path(f"/opt/anyaicam-stt-model/{NAME}", "models", "stt", NAME, windows=False) \
        == f"/opt/anyaicam-stt-model/{NAME}"
    if os.name == "nt":
        try:
            reloaded = importlib.reload(listen)
            assert Path(reloaded.STT_MODEL_PATH) == platform_paths._windows_program_data() / "AnyAiCam" / "models" / "stt" / NAME
        finally:
            monkeypatch.undo()
            importlib.reload(listen)


@needs_model
def test_the_bundled_model_transcribes_a_spoken_answer(model_dir):
    transcriber = listen.VoskTranscriber(str(model_dir))
    assert transcriber.available()
    transcript = transcriber.transcribe(_pcm(), listen.SAMPLE_RATE)
    assert transcript is not None and transcript.text == "i have a package for you"
    assert transcript.engine == "vosk-small-en-us-0.15" and 0.5 <= transcript.confidence <= 1.0


@needs_model
def test_listen_once_returns_the_answer_end_to_end(model_dir, tmp_path):
    got = listen.listen_once(camera_number=1, window_start=datetime.now(timezone.utc), buffer_root=tmp_path,
                             transcriber=listen.VoskTranscriber(str(model_dir)), capture=lambda d, s, n: _pcm())
    assert got is not None and "package" in got.text


def test_without_the_model_listening_is_skipped_and_reported(tmp_path, monkeypatch):
    missing = str(tmp_path / NAME)                      # e.g. the model folder deleted after the install
    monkeypatch.setattr(listen, "STT_MODEL_PATH", missing)
    monkeypatch.setattr(listen.VoskTranscriber.__init__, "__defaults__", (missing,))
    monkeypatch.setattr(listen, "_transcriber", None)
    assert listen.VoskTranscriber().available() is False
    assert listen.get_transcriber() is None
    assert listen.listen_once(camera_number=1, window_start=datetime.now(timezone.utc), buffer_root=tmp_path,
                              capture=lambda d, s, n: _pcm()) is None
    assert listen.capability()["stt_engine"] is None and listen.capability()["stt_model_present"] is False


@needs_model
def test_a_corrupted_model_fails_soft(model_dir, tmp_path):
    broken = tmp_path / NAME
    shutil.copytree(model_dir, broken)
    (broken / "am" / "final.mdl").write_bytes(b"corrupt")
    transcriber = listen.VoskTranscriber(str(broken))
    assert transcriber.available()                      # the folder exists, so loading is attempted...
    got = listen.listen_once(camera_number=1, window_start=datetime.now(timezone.utc), buffer_root=tmp_path,
                             transcriber=transcriber, capture=lambda d, s, n: _pcm())
    assert got is None                                  # ...and fails soft: no transcript, no exception
