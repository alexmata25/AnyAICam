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

import concurrent.futures
import concurrent.futures.process
import json
import os
import re
from dataclasses import fields
from datetime import datetime, timedelta
from typing import Protocol, get_args

from aaco import AacoCommand, Clarification, Operation

LOCAL_LLM_ENABLED = os.environ.get("ANYAICAM_AACO_LOCAL_LLM_ENABLED", "false").strip().lower() == "true"
LOCAL_LLM_MODEL_PATH = os.environ.get("ANYAICAM_AACO_LLM_MODEL_PATH", "").strip()
LOCAL_LLM_MAX_TOKENS = 100
LOCAL_LLM_TIMEOUT_SECONDS = 8

# 2026-09-19: a real staging stress test crashed llama.cpp itself with a
# native GGML_ASSERT failure -- a C-level assert() that calls abort(),
# which terminates the entire OS process it runs in. A ThreadPoolExecutor
# (this module's first attempt at bounding inference) cannot protect
# against that: threads share one process, so a native abort() in any
# thread kills the whole process -- the portal included, along with
# every other customer's in-flight request, not just the one asking
# AACO something. A ProcessPoolExecutor is the actual fix: the model
# only ever runs in a separate OS process, so a native crash there ends
# that worker process alone. concurrent.futures detects the dead worker
# and raises BrokenProcessPool on the pending future -- handled in
# _generate() identically to every other InterpreterUnavailable cause.
#
# Important, found only by actually testing a real crash rather than
# assuming: once a ProcessPoolExecutor's pool is broken, the SAME
# executor instance stays broken forever -- concurrent.futures does not
# self-heal it, and every subsequent .submit() on it raises
# BrokenProcessPool immediately, with no new worker ever started.
# _generate() therefore explicitly discards a broken executor (see its
# own BrokenProcessPool handling) so the *next* call gets a fresh one
# from here -- without that, a single crash would silently and
# permanently disable local-AI interpretation for the rest of this
# process's uptime (safe -- it would still always fall back to the
# deterministic grammar -- but needlessly degraded until a full portal
# restart). Created lazily, never at import time, so importing this
# module -- which happens unconditionally, local LLM enabled or not --
# never spawns a process.
_PROCESS_EXECUTOR: concurrent.futures.ProcessPoolExecutor | None = None


def _get_process_executor() -> concurrent.futures.ProcessPoolExecutor:
    global _PROCESS_EXECUTOR
    if _PROCESS_EXECUTOR is None:
        _PROCESS_EXECUTOR = concurrent.futures.ProcessPoolExecutor(max_workers=1)
    return _PROCESS_EXECUTOR


def _discard_broken_process_executor() -> None:
    global _PROCESS_EXECUTOR
    _PROCESS_EXECUTOR = None


def _run_inference_in_subprocess(model_path: str, system_prompt: str, text: str, max_tokens: int) -> str:
    """Runs entirely inside the worker process -- module-level and only
    plain, picklable arguments in and a plain string out, as
    ProcessPoolExecutor requires. Loads the model fresh the first time
    this specific worker process is asked to do anything, then caches
    it in THIS PROCESS's own module state so a warm model is reused
    across calls as long as the worker keeps running; any crash simply
    means the next call gets a brand new worker (and pays the load cost
    once more), never a resurrected, possibly-corrupted one."""
    global _subprocess_model
    if _subprocess_model is None:
        from llama_cpp import Llama
        _subprocess_model = Llama(model_path=model_path, n_ctx=768, n_threads=os.cpu_count() or 2, verbose=False)
    completion = _subprocess_model.create_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        max_tokens=max_tokens, temperature=0.0,
    )
    return completion["choices"][0]["message"]["content"]


# Lives only inside whatever worker process _run_inference_in_subprocess
# actually executes in -- never touched by, or meaningful in, the main
# portal process itself.
_subprocess_model = None

_ALLOWED_OPERATIONS = frozenset(get_args(Operation))
# "car" stays accepted (not just "vehicle") because the model may still
# emit it -- normalized downstream at the VMS boundary, same as before.
# The other four match the real category taxonomy the Investigate page
# already filters on (see main.py's _aaco_event_category()), not a new
# invented set.
_ALLOWED_EVENT_TYPES = frozenset({"person", "vehicle", "car", "motion", "lpr", "people_counting", "intrusion"})
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
Fields: operation (required), camera_id ("camera-<number>" or "camera-name:<lowercase name>"), start, end (ISO 8601, no timezone), event_type ("person", "vehicle", "motion", "lpr", "people_counting", or "intrusion"), offset_minutes (positive integer).
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

    def __init__(self, model_path: str | None = None, *, max_tokens: int = LOCAL_LLM_MAX_TOKENS, timeout_seconds: float = LOCAL_LLM_TIMEOUT_SECONDS, infer_fn=_run_inference_in_subprocess, executor: concurrent.futures.Executor | None = None):
        self.model_path = model_path or LOCAL_LLM_MODEL_PATH
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        # infer_fn/executor are overridable only so tests can exercise
        # real timeout and real crash-isolation behavior (a genuine
        # subprocess that sleeps, or one that deliberately exits) without
        # needing an actual multi-hundred-MB GGUF file -- production
        # code always uses the defaults: the real subprocess inference
        # function, on the module's own lazily-created process pool.
        self._infer_fn = infer_fn
        self._executor = executor

    def _load(self):
        # Cheap, fast-failing precondition check ONLY -- confirms a
        # model file exists and llama-cpp-python is importable in THIS
        # (the caller's) process, without ever constructing a real Llama
        # instance here. The real model load happens inside the worker
        # process, in _run_inference_in_subprocess, the first time it is
        # actually asked to infer -- there is no live model object in
        # the parent process to hold onto or to hand to a subprocess
        # (a loaded Llama instance holds native memory/mmap state that
        # cannot be pickled across a process boundary in the first
        # place). Kept as its own method/behavior because two existing
        # call sites (and tests) rely on a fast, in-process
        # InterpreterUnavailable for "no model configured" without
        # paying subprocess start-up cost just to discover that.
        if not self.model_path or not os.path.isfile(self.model_path):
            raise InterpreterUnavailable(f"No local AACO language model file at {self.model_path!r}.")
        # The package is only actually needed by the real subprocess
        # function -- an infer_fn substituted for testing never touches
        # llama_cpp at all, so requiring it importable here too would
        # force every test of timeout/crash behavior to have the real,
        # optional, multi-hundred-MB package installed for no reason.
        if self._infer_fn is _run_inference_in_subprocess:
            try:
                import llama_cpp  # noqa: F401
            except ImportError as error:
                raise InterpreterUnavailable("llama-cpp-python is not installed.") from error

    def _generate(self, text: str) -> str:
        # 2026-09-19 staging validation found this MUST be the chat-
        # completion API, not a raw single-string completion: Qwen2.5-
        # Instruct GGUF models are fine-tuned specifically against the
        # ChatML template, and calling them as a bare text completion
        # (the previous implementation) measured a real 0% validator
        # pass rate across every test phrase, including trivial ones
        # like "Which cameras are down?", plus 38-77 second latency per
        # request (the model rambling to the full token budget instead
        # of recognizing a natural stopping point). Switching to
        # create_chat_completion() with the exact same _SYSTEM_PROMPT
        # as the system role measured correct structured JSON output in
        # 3.5-8.3 seconds on the same hardware -- the prompt content
        # was never the problem, only how it was submitted to the model.
        #
        # 2026-09-19: LOCAL_LLM_TIMEOUT_SECONDS existed as a constant but
        # was never actually enforced, AND a real staging stress test
        # crashed llama.cpp with a native GGML_ASSERT failure (which
        # calls abort(), terminating the whole process). Both problems
        # share one fix: run inference in a separate OS process (see
        # _run_inference_in_subprocess and the module-level executor
        # above) and bound the wait with future.result(timeout=...). A
        # slow generation times out without ever blocking this request
        # -- or, since this call used to happen synchronously inside an
        # async route with no executor of its own, every other
        # concurrent customer on this single-worker portal -- for more
        # than self.timeout_seconds. A crashing generation only takes
        # down its own worker process; concurrent.futures raises
        # BrokenProcessPool here, handled identically to every other
        # failure below, and transparently gives the next call a fresh
        # worker.
        self._load()
        using_shared_executor = self._executor is None
        executor = self._executor if self._executor is not None else _get_process_executor()
        future = executor.submit(self._infer_fn, self.model_path, _SYSTEM_PROMPT, text, self.max_tokens)
        try:
            return future.result(timeout=self.timeout_seconds)
        except concurrent.futures.TimeoutError as error:
            raise InterpreterUnavailable(f"Local AACO language model inference exceeded the {self.timeout_seconds}s timeout.") from error
        except concurrent.futures.process.BrokenProcessPool as error:
            if using_shared_executor:
                _discard_broken_process_executor()
            raise InterpreterUnavailable(f"Local AACO language model inference process crashed: {error}") from error
        except Exception as error:
            raise InterpreterUnavailable(f"Local AACO language model inference failed: {error}") from error

    def interpret(self, text: str, *, now: datetime, context: dict | None = None) -> AacoCommand | Clarification:
        raw_text = self._generate(text).strip()
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
