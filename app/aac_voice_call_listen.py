"""AAC Voice Call -- visitor response capture and speech-to-text on the
edge (2026-09-28).

After the greeting finishes, the visitor's answer is taken from audio the
appliance is ALREADY recording: the camera's rolling pre-event buffer
(recordings/cameraN/_event_buffer/*.mkv) carries the camera microphone as
an audio track (the Front Door records 8 kHz AAC). So listening needs no
extra camera session, no extra login (no lockout risk) and no network.

Pipeline: wait for the listening window to pass -> cut exactly that
window out of the buffer segments with ffmpeg (16 kHz mono PCM) -> skip
silence with an energy gate -> offline speech-to-text (Vosk, Apache-2.0,
a small English model pinned by checksum and baked into the image) ->
hand the transcript to the caller, which forwards it to whoever owns the
session (the cloud in Hybrid mode, the local coordinator otherwise). The
cloud's aac_voice_call.record_visitor_utterance() classifies intent and
decides escalation; nothing here ever touches door/relay code.

Fails soft everywhere: no audio track, no buffer, no engine, silence ->
no transcript, and the homeowner still has the live view and the call.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger("anyaicam.aac_voice_call_listen")

LISTEN_ENABLED = os.environ.get("ANYAICAM_VOICE_CALL_LISTEN_ENABLED", "true").strip().lower() == "true"
LISTEN_SECONDS = max(2.0, float(os.environ.get("ANYAICAM_VOICE_CALL_LISTEN_SECONDS", "8")))
LISTEN_START_DELAY_SECONDS = max(0.0, float(os.environ.get("ANYAICAM_VOICE_CALL_LISTEN_DELAY_SECONDS", "0.7")))
BUFFER_FLUSH_SECONDS = max(0.0, float(os.environ.get("ANYAICAM_VOICE_CALL_BUFFER_FLUSH_SECONDS", "2.5")))
SPEECH_MIN_RMS = max(0, int(os.environ.get("ANYAICAM_VOICE_CALL_SPEECH_MIN_RMS", "300")))
SPEECH_MIN_ACTIVE_FRACTION = float(os.environ.get("ANYAICAM_VOICE_CALL_SPEECH_MIN_ACTIVE", "0.08"))
STT_MODEL_PATH = os.environ.get("ANYAICAM_STT_MODEL", "/opt/anyaicam-stt-model/vosk-model-small-en-us-0.15")
SAMPLE_RATE = 16000


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float
    engine: str


class Transcriber(Protocol):
    def transcribe(self, pcm16: bytes, sample_rate: int) -> Transcript | None: ...


# ---------------------------------------------------------------- audio capture

def _segment_start(path: Path) -> datetime | None:
    """buf<N>_YYYY-mm-dd_HH-MM-SS.mkv -> its start time (UTC, like the name)."""
    try:
        stamp = path.stem.split("_", 1)[1]
        return datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


def capture_window(buffer_dir: str | Path, start: datetime, seconds: float, *, runner=subprocess.run) -> bytes | None:
    """16 kHz mono PCM16 of [start, start+seconds] from the buffer
    segments, or None (no buffer, no audio track, ffmpeg missing)."""
    if shutil.which("ffmpeg") is None and runner is subprocess.run:
        return None
    try:
        segments = sorted(p for p in Path(buffer_dir).glob("*.mkv") if _segment_start(p) is not None)
    except OSError:
        return None
    start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end = start.timestamp() + seconds
    covering = []
    for index, path in enumerate(segments):
        seg_start = _segment_start(path).timestamp()
        seg_end = _segment_start(segments[index + 1]).timestamp() if index + 1 < len(segments) else float("inf")
        if seg_start < end and seg_end > start.timestamp():
            covering.append(path)
    if not covering:
        return None
    offset = max(0.0, start.timestamp() - _segment_start(covering[0]).timestamp())
    # The concat list must be a real file: piped on stdin, ffmpeg resolves
    # every entry relative to "pipe:" and cannot open the segments
    # (found on the real Front Door buffer).
    import tempfile

    listing = "".join(f"file '{p.resolve().as_posix()}'\n" for p in covering)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write(listing)
        list_path = handle.name
    try:
        result = runner(
            ["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_path,
             "-ss", f"{offset:.2f}", "-t", f"{seconds:.2f}", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass
    pcm = getattr(result, "stdout", b"") or b""
    return pcm if len(pcm) >= SAMPLE_RATE else None  # under half a second is not an answer


def speech_present(pcm16: bytes, *, frame_ms: int = 30) -> bool:
    """Energy gate: enough 30 ms frames above SPEECH_MIN_RMS to be worth
    transcribing (keeps the recognizer off silence and steady hum)."""
    import array

    samples = array.array("h", pcm16[: len(pcm16) - len(pcm16) % 2])
    step = SAMPLE_RATE * frame_ms // 1000
    frames = active = 0
    for i in range(0, len(samples) - step + 1, step):
        chunk = samples[i : i + step]
        rms = (sum(s * s for s in chunk) / step) ** 0.5
        frames += 1
        active += rms >= SPEECH_MIN_RMS
    return frames > 0 and active / frames >= SPEECH_MIN_ACTIVE_FRACTION


# ---------------------------------------------------------------- speech-to-text

class VoskTranscriber:
    """Offline recognizer; the model loads once, lazily."""

    def __init__(self, model_path: str = STT_MODEL_PATH) -> None:
        self.model_path = model_path
        self._model = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        try:
            import vosk  # noqa: F401
        except ImportError:
            return False
        return Path(self.model_path).is_dir()

    def _get_model(self):
        with self._lock:
            if self._model is None:
                import vosk

                vosk.SetLogLevel(-1)
                self._model = vosk.Model(self.model_path)
            return self._model

    def transcribe(self, pcm16: bytes, sample_rate: int) -> Transcript | None:
        import vosk

        recognizer = vosk.KaldiRecognizer(self._get_model(), sample_rate)
        recognizer.SetWords(True)
        step = sample_rate  # feed one second at a time
        for i in range(0, len(pcm16), step * 2):
            recognizer.AcceptWaveform(pcm16[i : i + step * 2])
        result = json.loads(recognizer.FinalResult() or "{}")
        text = str(result.get("text") or "").strip()
        if not text:
            return None
        words = result.get("result") or []
        confidence = sum(float(w.get("conf", 0.0)) for w in words) / len(words) if words else 0.0
        return Transcript(text=text, confidence=round(confidence, 3), engine="vosk-small-en-us-0.15")


_transcriber: Transcriber | None = None
_transcriber_lock = threading.Lock()


def get_transcriber() -> Transcriber | None:
    global _transcriber
    with _transcriber_lock:
        if _transcriber is None:
            candidate = VoskTranscriber()
            if not candidate.available():
                return None
            _transcriber = candidate
        return _transcriber


def capability() -> dict:
    transcriber = get_transcriber()
    return {"listen_enabled": LISTEN_ENABLED, "stt_engine": "vosk" if transcriber else None,
            "stt_model_present": Path(STT_MODEL_PATH).is_dir(), "listen_seconds": LISTEN_SECONDS}


# ---------------------------------------------------------------- orchestration

def listen_once(*, camera_number: int, window_start: datetime, buffer_root: str | Path,
                transcriber: Transcriber | None = None, capture=capture_window,
                seconds: float = LISTEN_SECONDS) -> Transcript | None:
    """Capture and transcribe one listening window (call after it has passed)."""
    transcriber = transcriber or get_transcriber()
    if transcriber is None:
        logger.info("voice_call_listen camera=%s skipped=stt_unavailable", camera_number)
        return None
    pcm = capture(Path(buffer_root) / f"camera{camera_number}" / "_event_buffer", window_start, seconds)
    if not pcm:
        logger.info("voice_call_listen camera=%s skipped=no_audio", camera_number)
        return None
    if not speech_present(pcm):
        logger.info("voice_call_listen camera=%s skipped=silence", camera_number)
        return None
    try:
        transcript = transcriber.transcribe(pcm, SAMPLE_RATE)
    except Exception as error:
        logger.warning("voice_call_listen camera=%s stt_failed=%s", camera_number, type(error).__name__)
        return None
    logger.info("voice_call_listen camera=%s transcript_chars=%s", camera_number, len(transcript.text) if transcript else 0)
    return transcript


def schedule_listen(*, camera_number: int, greeting_seconds: float, buffer_root: str | Path,
                    on_transcript: Callable[[Transcript], None], now: Callable[[], datetime] | None = None,
                    sleep: Callable[[float], None] = time.sleep, background: bool = True,
                    transcriber: Transcriber | None = None, capture=capture_window) -> bool:
    """After the greeting plays, listen for LISTEN_SECONDS and pass any
    transcript to on_transcript. Returns False when listening is off."""
    if not LISTEN_ENABLED:
        return False
    clock = now or (lambda: datetime.now(timezone.utc))
    window_start = datetime.fromtimestamp(clock().timestamp() + max(0.0, greeting_seconds) + LISTEN_START_DELAY_SECONDS, tz=timezone.utc)

    def run():
        wait = window_start.timestamp() + LISTEN_SECONDS + BUFFER_FLUSH_SECONDS - clock().timestamp()
        if wait > 0:
            sleep(wait)
        transcript = listen_once(camera_number=camera_number, window_start=window_start, buffer_root=buffer_root,
                                 transcriber=transcriber, capture=capture)
        if transcript is not None:
            try:
                on_transcript(transcript)
            except Exception as error:
                logger.warning("voice_call_listen camera=%s forward_failed=%s", camera_number, type(error).__name__)

    if background:
        threading.Thread(target=run, name=f"voice-call-listen-{camera_number}", daemon=True).start()
    else:
        run()
    return True
