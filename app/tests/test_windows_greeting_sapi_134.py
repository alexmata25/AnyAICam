"""Windows 1.3.4 voice parity (2026-10-27): the AAC Voice Call greeting is spoken with Windows' own speech engine.

Linux speaks the greeting with espeak-ng (in the release image). A Windows install has no espeak-ng, so
_default_provider() fell back to the MOCK provider: the cloud saw greetings "delivered" while the visitor heard
nothing. On Windows the real provider now synthesizes with System.Speech (SAPI, part of Windows; nothing bundled),
through the in-box Windows PowerShell, as raw 22,050 Hz 16-bit mono PCM -- the format eSpeak gives -- so the
loudness levelling and the ISAPI camera relay are unchanged. The text reaches PowerShell on stdin, never on its
command line. A synthesis failure is reported (tts_unavailable), never a silent mock.
"""
import array
import math
import shutil
import subprocess
import sys

import pytest

import aac_voice_call_greeting as greeting


class _Runner:
    def __init__(self, stdout=b"\x10\x00" * 22050):
        self.calls, self.stdout = [], stdout

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=self.stdout, stderr=b"")


def test_sapi_receives_the_text_on_stdin_never_on_its_command_line():
    runner = _Runner()
    text = "Hello'; Remove-Item C:\\ -Recurse; '"
    pcm, rate = greeting.synthesize_speech_sapi(text, runner=runner)
    args, kwargs = runner.calls[0]
    assert text not in " ".join(args)
    assert kwargs["input"] == text.encode("utf-8")
    assert args[0].lower().endswith("powershell.exe") and "-NonInteractive" in args
    assert (len(pcm), rate) == (44100, 22050)


def test_sapi_text_is_capped_like_espeak():
    runner = _Runner()
    greeting.synthesize_speech_sapi("x" * 2000, runner=runner)
    assert len(runner.calls[0][1]["input"]) == 500


def test_sapi_failure_raises_so_the_provider_reports_tts_unavailable():
    def failing(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr=b"no voice")

    with pytest.raises(RuntimeError):
        greeting.synthesize_speech_sapi("Hello", runner=failing)
    with pytest.raises(RuntimeError):
        greeting.synthesize_speech_sapi("Hello", runner=_Runner(stdout=b""))


def test_words_per_minute_maps_to_the_sapi_rate_range():
    assert greeting._sapi_rate(180) == 0
    assert greeting._sapi_rate(150) < 0
    assert greeting._sapi_rate(10_000) == 10 and greeting._sapi_rate(0) == -10


def test_windows_auto_selects_the_real_provider_with_sapi(monkeypatch):
    monkeypatch.setenv("ANYAICAM_GREETING_AUDIO_PROVIDER", "auto")
    monkeypatch.setattr(greeting, "WINDOWS", True)
    monkeypatch.setattr(greeting.shutil, "which", lambda name: None)
    provider = greeting._default_provider()
    assert isinstance(provider, greeting.IsapiTtsGreetingProvider)
    assert provider.capability()["tts_engine"] == "windows-sapi"


def test_espeak_wins_where_it_is_installed(monkeypatch):
    monkeypatch.setattr(greeting, "WINDOWS", True)
    monkeypatch.setattr(greeting.shutil, "which", lambda name: "/usr/bin/espeak-ng")
    assert greeting._tts_engine() == "espeak-ng"


def test_linux_without_espeak_still_gets_the_mock(monkeypatch):
    monkeypatch.setenv("ANYAICAM_GREETING_AUDIO_PROVIDER", "auto")
    monkeypatch.setattr(greeting, "WINDOWS", False)
    monkeypatch.setattr(greeting.shutil, "which", lambda name: None)
    assert isinstance(greeting._default_provider(), greeting.MockGreetingAudioProvider)


def test_the_provider_reports_a_sapi_failure_as_not_delivered(monkeypatch):
    def broken(text):
        raise RuntimeError("no SAPI voice")

    provider = greeting.IsapiTtsGreetingProvider(
        synthesize=broken, camera_lookup=lambda cid: {"talk_down_supported": 1},
        talk_active=lambda cid: False, auth_cooldown=lambda cid: 0, background=False)
    result = provider.speak(greeting.GreetingRequest(camera_id="cam-1", customer_id="cust", event_id="e", text="Hello"))
    assert (result.delivered, result.suppressed_reason) == (False, "tts_unavailable")


def _sapi_has_a_voice() -> bool:
    if sys.platform != "win32" or not shutil.which("powershell.exe"):
        return False
    probe = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices().Count"],
        capture_output=True, text=True, timeout=60)
    return probe.returncode == 0 and probe.stdout.strip().isdigit() and int(probe.stdout.strip()) > 0


@pytest.mark.skipif(not _sapi_has_a_voice(), reason="needs Windows with an installed SAPI voice")
def test_real_sapi_speaks_audible_speech_in_the_relay_format():
    pcm, rate = greeting.synthesize_speech_sapi("Hello, thank you for visiting. How can I help you?")
    assert rate == 22050
    seconds = len(pcm) / 2 / rate
    assert 1.0 < seconds < 15.0
    samples = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    loud = [value for value in samples if abs(value) > 300]
    assert len(loud) > rate * 0.3  # at least ~0.3 s of non-silent speech
    levelled = array.array("h", greeting.level_greeting_pcm(pcm, "medium"))
    speech = [value for value in levelled if abs(value) > 300]
    rms_dbfs = 20 * math.log10(math.sqrt(sum(v * v for v in speech) / len(speech)) / 32768)
    assert -31.0 < rms_dbfs < -25.0  # the "medium" target (-28 dBFS), as for eSpeak
