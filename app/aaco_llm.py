"""Local-only natural-language layer on top of AACO's strict command
boundary (app/aaco.py). See docs/aaco-local-llm-design.md for the full
model/runtime audit and rationale.

Architecture, matching the user's own diagram exactly: local text ->
LocalLlmInterpreter (this module) -> a strict, closed-schema validator
-> the SAME AacoCommand/Clarification types app/aaco.py already
defines -> the existing, unmodified execute()/VmsBoundary path. This
module never calls execute(), never touches a VmsBoundary, never opens
a database connection, and never imports door_access/relay_control/
partner_db -- it has no way to reach a VMS action directly even if
every safety check below were somehow bypassed, because it simply has
no import of anything that could perform one.

Two independent safety layers, deliberately not merged into one:

1. The model itself is asked (via the prompt) to emit ONLY one fixed
   JSON shape. This is a prompting convention, not a security
   boundary -- a model can hallucinate anything.
2. _validate_ai_command() is the actual security boundary: it re-parses
   the model's raw text as JSON and accepts a result ONLY if every
   field matches the exact same closed shape aaco.AacoCommand itself
   enforces (operation must be one of aaco.Operation's own literal
   values, camera_id must match the exact token grammar
   aaco._camera_token()/DeterministicLanguageAdapter already produce,
   start/end must be real, in-range datetimes, event_type/
   offset_minutes must be one of a small allowed set). Any deviation
   -- an unknown operation, an extra key, a camera_id that isn't
   shaped like camera-<n> or camera-name:<text>, a timestamp centuries
   away, a string where a number belongs -- is treated exactly like a
   parse failure: Clarification, never a best-effort guess. This is
   the layer that makes "AI output must validate against the existing
   allowed AACO operations/schema" true regardless of what the model
   emits, including a deliberately adversarial prompt injection
   embedded in the user's own command text.

NaturalAacoLanguageAdapter is the orchestration layer register_aaco_
routes() actually uses in place of a bare DeterministicLanguageAdapter
when local inference is enabled. It tries the LLM first (matching the
user's own "local AI interpretation" step), but ALWAYS also tries the
existing deterministic regex grammar before giving up -- an exact
fixed phrase ("Show Camera 4") must keep working byte-for-byte the
same even if the model is disabled, unavailable, mid-outage, or simply
gets that one phrase wrong. Preserving the regex grammar as a
deterministic fallback means every existing test in test_aaco.py stays
correct with zero changes, and the whole natural-language layer can be
disabled in one env var with no other code change.

No external/cloud LLM call exists anywhere in this module -- see
LlamaCppInterpreter._generate(), the only place inference happens, and
its own docstring for why it can only ever load a local GGUF file."""
from __future__ import annotations

import json
import os
import re
from dataclasses import fields
from datetime import datetime, timedelta
from typing import Protocol, get_args

from aaco import AacoCommand, Clarification, Operation

LOCAL_LLM_ENABLED = os.environ.get("ANYAICAM_AACO_LOCAL_LLM_ENABLED", "false").strip().lower() == "true"
LOCAL_LLM_MODEL_PATH = os.environ.get("ANYAICAM_AACO_LLM_MODEL_PATH", "").strip()
LOCAL_LLM_MAX_TOKENS = 200
LOCAL_LLM_TIMEOUT_SECONDS = 8

_ALLOWED_OPERATIONS = frozenset(get_args(Operation))
_ALLOWED_EVENT_TYPES = frozenset({"person", "vehicle", "car"})
_CAMERA_TOKEN = re.compile(r"^camera-(?:\d{1,4}|name:[a-z0-9 &'_-]{1,80})$")
_MAX_OFFSET_MINUTES = 24 * 60
_MAX_LOOKBACK = timedelta(days=365)
_MAX_LOOKAHEAD = timedelta(days=1)
_ALLOWED_COMMAND_KEYS = {"operation", "camera_id", "start", "end", "event_type", "offset_minutes"}


class InterpreterUnavailable(Exception):
    """Raised whenever local inference cannot even be attempted (feature
    flag off, model file missing, runtime import/load failure, or the
    model timed out/crashed). NaturalAacoLanguageAdapter treats this
    identically to "the model declined to answer" -- fall back to the
    deterministic grammar -- never a user-facing error of its own."""


class NaturalLanguageInterpreter(Protocol):
    """What any local-inference backend must implement. Deliberately
    the smallest possible surface: one text in, one AacoCommand or
    Clarification out (already fully validated by the interpreter's
    own call to _validate_ai_command() before returning) -- swapping
    llama.cpp for a different local runtime later never touches
    NaturalAacoLanguageAdapter or app/aaco.py."""

    def interpret(self, text: str, *, now: datetime, context: dict | None) -> AacoCommand | Clarification: ...


def _parse_iso(value: object, *, now: datetime) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    if parsed < now - _MAX_LOOKBACK or parsed > now + _MAX_LOOKAHEAD:
        return None
    return parsed


def _validate_ai_command(raw: object, *, now: datetime) -> AacoCommand | Clarification:
    """The actual security boundary (see module docstring). Returns a
    Clarification for anything that does not exactly match the closed
    shape below -- never raises, never partially trusts a value, never
    lets an unrecognized key or operation through. A future different
    local model, or a deliberately adversarial command embedding
    prompt-injection text, is subject to this exact same check; the
    validator has no notion of "which backend produced this"."""
    fallback = Clarification("I couldn't safely understand that request. Try naming a camera, an event type, or a time range directly.")
    if not isinstance(raw, dict) or not raw or set(raw) - _ALLOWED_COMMAND_KEYS:
        return fallback

    # Deliberately no model-authored clarification text is ever
    # surfaced to the user -- an "operation" the model invents (like a
    # self-declared "clarification") is just one more value not in
    # _ALLOWED_OPERATIONS below, and falls through to the exact same
    # fixed, non-model-generated fallback message every other
    # rejection reaches. This keeps every piece of text a customer
    # ever sees from this path fully within this codebase's own
    # control, never model output verbatim.
    operation = raw.get("operation")
    if not isinstance(operation, str) or operation not in _ALLOWED_OPERATIONS:
        return fallback

    camera_id = raw.get("camera_id")
    if camera_id is not None:
        if not isinstance(camera_id, str) or not _CAMERA_TOKEN.fullmatch(camera_id):
            return fallback

    event_type = raw.get("event_type")
    if event_type is not None and (not isinstance(event_type, str) or event_type not in _ALLOWED_EVENT_TYPES):
        return fallback

    offset_minutes = raw.get("offset_minutes")
    if offset_minutes is not None:
        if not isinstance(offset_minutes, int) or isinstance(offset_minutes, bool) or not (0 < offset_minutes <= _MAX_OFFSET_MINUTES):
            return fallback

    start = end = None
    if raw.get("start") is not None:
        start = _parse_iso(raw["start"], now=now)
        if start is None:
            return fallback
    if raw.get("end") is not None:
        end = _parse_iso(raw["end"], now=now)
        if end is None:
            return fallback
    if start is not None and end is not None and end < start:
        return fallback

    # Every operation-specific requirement aaco.execute() itself
    # enforces is re-checked here too -- a command that would raise
    # ValueError/PermissionError deep inside execute() is caught here
    # first and turned into a clarification instead, so the model
    # never gets to trigger an exception path meant for a caller bug.
    if operation in {"live_view", "playback", "playback_navigation", "event_navigation", "unlock_door"} and not camera_id:
        return fallback
    if operation in {"playback", "playback_navigation", "event_search"} and (start is None or end is None):
        return fallback
    if operation == "event_navigation" and end is None:
        return fallback
    if operation == "camera_status" and any(raw.get(key) is not None for key in ("camera_id", "start", "end", "event_type", "offset_minutes")):
        return fallback

    known_fields = {field.name for field in fields(AacoCommand)}
    kwargs = {key: value for key, value in {
        "camera_id": camera_id, "start": start, "end": end,
        "event_type": event_type, "offset_minutes": offset_minutes,
    }.items() if key in known_fields}
    return AacoCommand(operation, **kwargs)


_SYSTEM_PROMPT = """You translate one customer sentence about their security cameras into exactly one JSON object, nothing else.

Allowed "operation" values: live_view, playback, event_search, camera_status, playback_navigation, event_navigation, unlock_door.
Fields: operation (required), camera_id ("camera-<number>" or "camera-name:<lowercase name>"), start, end (ISO 8601, no timezone), event_type ("person" or "vehicle"), offset_minutes (positive integer).
Never invent a camera name, door, or event not mentioned. Never answer with anything except one JSON object using only the fields above. If the request is unclear, unsafe, or not about live view/playback/events/camera status/unlocking a door, answer with {"operation":"none"} -- do not guess.

Examples:
"AACO, could you show me the front door?" -> {"operation":"live_view","camera_id":"camera-name:front door"}
"Were there any people at the front entrance in the last hour?" -> {"operation":"event_search","event_type":"person","camera_id":"camera-name:front entrance","start":"<now-1h>","end":"<now>"}
"Which of my cameras are down?" -> {"operation":"camera_status"}
"AACO, open the front door for me." -> {"operation":"unlock_door","camera_id":"camera-name:front door"}
"""


class LlamaCppInterpreter:
    """The one place local inference actually happens. Loads a single
    local GGUF model file via llama-cpp-python (pure CPU, no GPU/CUDA
    dependency, no network call of any kind -- see docs/aaco-local-llm-
    design.md for why this runtime/model size was chosen) and asks it
    to emit the fixed JSON shape _SYSTEM_PROMPT describes. Every
    response is passed through _validate_ai_command() before this
    method returns -- this class itself never returns unvalidated
    model output.

    Deliberately does not import llama_cpp at module scope: the
    package (and the model file ANYAICAM_AACO_LLM_MODEL_PATH points
    at) are both optional, off by default, and must never be a reason
    the rest of AACO -- or this whole application -- fails to start.
    """

    def __init__(self, model_path: str | None = None, *, max_tokens: int = LOCAL_LLM_MAX_TOKENS):
        self.model_path = model_path or LOCAL_LLM_MODEL_PATH
        self.max_tokens = max_tokens
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        if not self.model_path or not os.path.isfile(self.model_path):
            raise InterpreterUnavailable(f"No local AACO language model file at {self.model_path!r}.")
        try:
            from llama_cpp import Llama
        except ImportError as error:
            raise InterpreterUnavailable("llama-cpp-python is not installed.") from error
        try:
            self._model = Llama(model_path=self.model_path, n_ctx=768, n_threads=os.cpu_count() or 2, verbose=False)
        except Exception as error:
            raise InterpreterUnavailable(f"Local AACO language model failed to load: {error}") from error
        return self._model

    def _generate(self, prompt: str) -> str:
        model = self._load()
        try:
            completion = model(
                prompt, max_tokens=self.max_tokens, temperature=0.0,
                stop=["\n\n", "</s>"],
            )
        except Exception as error:
            raise InterpreterUnavailable(f"Local AACO language model inference failed: {error}") from error
        return completion["choices"][0]["text"]

    def interpret(self, text: str, *, now: datetime, context: dict | None = None) -> AacoCommand | Clarification:
        prompt = f'{_SYSTEM_PROMPT}\n"{text}" -> '
        raw_text = self._generate(prompt).strip()
        try:
            raw = json.loads(raw_text[raw_text.find("{"): raw_text.rfind("}") + 1] or "null")
        except (ValueError, json.JSONDecodeError):
            return Clarification("I couldn't safely understand that request. Try naming a camera, an event type, or a time range directly.")
        return _validate_ai_command(raw, now=now)


class NaturalAacoLanguageAdapter:
    """Drop-in replacement for aaco.DeterministicLanguageAdapter that
    tries local inference first and always falls back to the exact
    same regex grammar (never replaced, never modified) whenever the
    model is disabled, unavailable, or produces something that fails
    strict validation. An exact fixed phrase the regex grammar already
    handles keeps working identically whether or not local inference
    is enabled -- this class adds capability, it never removes any."""

    def __init__(self, interpreter: NaturalLanguageInterpreter | None = None):
        from aaco import DeterministicLanguageAdapter
        self._regex = DeterministicLanguageAdapter()
        self._interpreter = interpreter

    def parse(self, text: str, *, now: datetime, context: dict | None = None) -> AacoCommand | Clarification:
        if self._interpreter is not None:
            try:
                result = self._interpreter.interpret(text, now=now, context=context)
            except InterpreterUnavailable:
                result = None
            if isinstance(result, AacoCommand):
                return result
        return self._regex.parse(text, now=now, context=context)


def default_interpreter() -> NaturalLanguageInterpreter | None:
    """The interpreter register_aaco_routes() should actually use --
    None (pure regex, today's unchanged behavior) unless local
    inference is explicitly enabled AND a real model file is
    configured. Never constructs a LlamaCppInterpreter, let alone
    loads a model, when the flag is off."""
    if not LOCAL_LLM_ENABLED or not LOCAL_LLM_MODEL_PATH:
        return None
    return LlamaCppInterpreter(LOCAL_LLM_MODEL_PATH)
