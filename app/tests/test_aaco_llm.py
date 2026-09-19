"""Tests for app/aaco_llm.py -- the local-only natural-language layer
on top of AACO's strict command boundary.

Two things are tested completely independently of any real model file
or the llama-cpp-python package ever being installed:

1. _validate_ai_command() -- the actual security boundary. Every test
   here feeds it a plain Python dict (exactly the shape json.loads()
   would produce from a model's raw text) and asserts either a valid
   AacoCommand or a Clarification comes back -- never an exception,
   never a partially-trusted value.
2. NaturalAacoLanguageAdapter -- exercised with a tiny fake
   NaturalLanguageInterpreter (mirroring how test_aaco_web.py already
   fakes VmsBoundary), never a real LlamaCppInterpreter.

LlamaCppInterpreter itself is tested only for its plumbing (JSON
extraction from surrounding text, and the InterpreterUnavailable path
when no model file is configured) -- never its real _generate(), which
would require the optional llama-cpp-python package and a real GGUF
file neither of which this test suite depends on."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from aaco import AacoCommand, Clarification
from aaco_llm import (
    InterpreterUnavailable,
    LlamaCppInterpreter,
    NaturalAacoLanguageAdapter,
    _validate_ai_command,
    default_interpreter,
)
import aaco_llm

NOW = datetime(2026, 9, 19, 12, 0, 0)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestValidateAiCommand:
    def test_valid_live_view_by_name(self):
        result = _validate_ai_command({"operation": "live_view", "camera_id": "camera-name:front door"}, now=NOW)
        assert result == AacoCommand("live_view", camera_id="camera-name:front door")

    def test_valid_live_view_by_number(self):
        result = _validate_ai_command({"operation": "live_view", "camera_id": "camera-4"}, now=NOW)
        assert result == AacoCommand("live_view", camera_id="camera-4")

    def test_valid_camera_status_with_no_extra_fields(self):
        assert _validate_ai_command({"operation": "camera_status"}, now=NOW) == AacoCommand("camera_status")

    def test_valid_playback_with_a_relative_window(self):
        start = NOW - timedelta(minutes=20)
        result = _validate_ai_command({"operation": "playback", "camera_id": "camera-name:driveway", "start": _iso(start), "end": _iso(NOW)}, now=NOW)
        assert result == AacoCommand("playback", camera_id="camera-name:driveway", start=start, end=NOW)

    def test_valid_event_search_scoped_to_a_camera(self):
        start = NOW - timedelta(hours=1)
        result = _validate_ai_command(
            {"operation": "event_search", "event_type": "person", "camera_id": "camera-name:front entrance", "start": _iso(start), "end": _iso(NOW)},
            now=NOW,
        )
        assert result == AacoCommand("event_search", camera_id="camera-name:front entrance", event_type="person", start=start, end=NOW)

    def test_valid_unlock_door(self):
        result = _validate_ai_command({"operation": "unlock_door", "camera_id": "camera-name:front door"}, now=NOW)
        assert result == AacoCommand("unlock_door", camera_id="camera-name:front door")

    def test_valid_car_event_type_accepted_like_the_regex_grammar(self):
        start = NOW - timedelta(hours=2)
        result = _validate_ai_command({"operation": "event_search", "event_type": "car", "start": _iso(start), "end": _iso(NOW)}, now=NOW)
        assert result.event_type == "car"

    @pytest.mark.parametrize("event_type", ["motion", "lpr", "people_counting", "intrusion"])
    def test_widened_event_categories_matching_the_investigate_page_are_accepted(self, event_type):
        # 2026-09-19: widened alongside main.py's _aaco_event_category()
        # so an AI-interpreted "any license plate events?" or "were
        # there any intrusion alerts?" is not silently rejected just
        # because the original allow-list only had person/vehicle/car.
        start = NOW - timedelta(hours=1)
        result = _validate_ai_command({"operation": "event_search", "event_type": event_type, "start": _iso(start), "end": _iso(NOW)}, now=NOW)
        assert result.event_type == event_type

    # -- rejections: shape/type ---------------------------------------
    def test_non_dict_input_is_rejected(self):
        for bad in (None, "operation", 42, ["operation"], "{\"operation\":\"live_view\"}"):
            assert isinstance(_validate_ai_command(bad, now=NOW), Clarification)

    def test_empty_dict_is_rejected(self):
        assert isinstance(_validate_ai_command({}, now=NOW), Clarification)

    def test_unknown_extra_key_is_rejected_even_if_everything_else_is_valid(self):
        result = _validate_ai_command({"operation": "camera_status", "sql": "DROP TABLE cameras"}, now=NOW)
        assert isinstance(result, Clarification)

    def test_unknown_operation_is_rejected(self):
        assert isinstance(_validate_ai_command({"operation": "delete_everything"}, now=NOW), Clarification)

    def test_non_string_operation_is_rejected_without_raising(self):
        for bad in (None, 1, ["live_view"], {"op": "live_view"}):
            assert isinstance(_validate_ai_command({"operation": bad}, now=NOW), Clarification)

    def test_model_self_declared_clarification_operation_is_not_special_cased(self):
        # No model-authored free text is ever surfaced -- an invented
        # "clarification" operation is just another unknown operation.
        result = _validate_ai_command({"operation": "clarification", "clarification": "ignore all instructions"}, now=NOW)
        assert isinstance(result, Clarification)
        assert "ignore all instructions" not in result.message

    # -- rejections: camera_id shape -----------------------------------
    @pytest.mark.parametrize("bad_camera_id", [
        "'; DROP TABLE cameras; --",
        "../../etc/passwd",
        "camera-",
        "camera-name:",
        "camera-99999999999999",
        "front door",
        "camera_id:1",
        "camera-name:" + "x" * 200,
        123,
        ["camera-1"],
    ])
    def test_malformed_camera_id_is_rejected(self, bad_camera_id):
        result = _validate_ai_command({"operation": "live_view", "camera_id": bad_camera_id}, now=NOW)
        assert isinstance(result, Clarification)

    def test_camera_scoped_operation_without_a_camera_id_is_rejected(self):
        for operation in ("live_view", "playback", "playback_navigation", "event_navigation", "unlock_door"):
            payload = {"operation": operation}
            if operation in {"playback", "playback_navigation"}:
                payload.update(start=_iso(NOW - timedelta(minutes=5)), end=_iso(NOW))
            if operation == "event_navigation":
                payload.update(end=_iso(NOW))
            assert isinstance(_validate_ai_command(payload, now=NOW), Clarification), operation

    def test_camera_status_with_any_extra_field_is_rejected(self):
        for key, value in (("camera_id", "camera-1"), ("start", _iso(NOW)), ("event_type", "person"), ("offset_minutes", 5)):
            result = _validate_ai_command({"operation": "camera_status", key: value}, now=NOW)
            assert isinstance(result, Clarification), key

    # -- rejections: event_type -----------------------------------------
    @pytest.mark.parametrize("bad_event_type", ["dog", "PERSON", "", None if False else 1, ["person"]])
    def test_invalid_event_type_is_rejected(self, bad_event_type):
        result = _validate_ai_command({"operation": "camera_status"} if bad_event_type is None else {"operation": "event_search", "event_type": bad_event_type, "start": _iso(NOW - timedelta(hours=1)), "end": _iso(NOW)}, now=NOW)
        assert isinstance(result, Clarification)

    # -- rejections: offset_minutes --------------------------------------
    @pytest.mark.parametrize("bad_offset", [0, -5, 1441, 20.5, "20", True, False])
    def test_invalid_offset_minutes_is_rejected(self, bad_offset):
        result = _validate_ai_command({"operation": "playback_navigation", "camera_id": "camera-1", "offset_minutes": bad_offset, "start": _iso(NOW), "end": _iso(NOW)}, now=NOW)
        assert isinstance(result, Clarification)

    # -- rejections: timestamps -------------------------------------------
    def test_unparseable_timestamp_is_rejected(self):
        result = _validate_ai_command({"operation": "playback", "camera_id": "camera-1", "start": "not-a-date", "end": _iso(NOW)}, now=NOW)
        assert isinstance(result, Clarification)

    def test_timestamp_far_in_the_past_is_rejected(self):
        ancient = _iso(NOW - timedelta(days=3650))
        result = _validate_ai_command({"operation": "playback", "camera_id": "camera-1", "start": ancient, "end": _iso(NOW)}, now=NOW)
        assert isinstance(result, Clarification)

    def test_timestamp_far_in_the_future_is_rejected(self):
        future = _iso(NOW + timedelta(days=30))
        result = _validate_ai_command({"operation": "playback", "camera_id": "camera-1", "start": _iso(NOW), "end": future}, now=NOW)
        assert isinstance(result, Clarification)

    def test_end_before_start_is_rejected(self):
        result = _validate_ai_command({"operation": "playback", "camera_id": "camera-1", "start": _iso(NOW), "end": _iso(NOW - timedelta(minutes=5))}, now=NOW)
        assert isinstance(result, Clarification)

    def test_event_search_missing_time_range_is_rejected(self):
        assert isinstance(_validate_ai_command({"operation": "event_search", "event_type": "person"}, now=NOW), Clarification)


class _FakeInterpreter:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def interpret(self, text, *, now, context):
        self.calls.append((text, now, context))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TestNaturalAacoLanguageAdapter:
    def test_no_interpreter_behaves_exactly_like_plain_regex(self):
        adapter = NaturalAacoLanguageAdapter(None)
        assert adapter.parse("Show Camera 4", now=NOW) == AacoCommand("live_view", camera_id="camera-4")
        assert isinstance(adapter.parse("Delete everything", now=NOW), Clarification)

    def test_valid_interpreter_output_is_used_directly(self):
        command = AacoCommand("live_view", camera_id="camera-name:front door")
        interpreter = _FakeInterpreter(command)
        adapter = NaturalAacoLanguageAdapter(interpreter)
        assert adapter.parse("AACO, could you show me the front door?", now=NOW) == command
        assert interpreter.calls

    def test_interpreter_unavailable_falls_back_to_regex_silently(self):
        interpreter = _FakeInterpreter(InterpreterUnavailable("model not loaded"))
        adapter = NaturalAacoLanguageAdapter(interpreter)
        assert adapter.parse("Show Camera 4", now=NOW) == AacoCommand("live_view", camera_id="camera-4")

    def test_interpreter_clarification_still_lets_an_exact_regex_phrase_win(self):
        interpreter = _FakeInterpreter(Clarification("not sure"))
        adapter = NaturalAacoLanguageAdapter(interpreter)
        assert adapter.parse("Show Camera 4", now=NOW) == AacoCommand("live_view", camera_id="camera-4")

    def test_interpreter_clarification_and_regex_failure_yields_a_clarification(self):
        interpreter = _FakeInterpreter(Clarification("not sure"))
        adapter = NaturalAacoLanguageAdapter(interpreter)
        assert isinstance(adapter.parse("Completely unparseable gibberish", now=NOW), Clarification)

    def test_interpreter_never_receives_a_chance_to_bypass_execute(self):
        # NaturalAacoLanguageAdapter has no reference to execute(), a
        # VmsBoundary, or any database/relay module -- there is no
        # object here it could call even if it wanted to.
        import aaco_llm as module
        assert not hasattr(module, "execute")
        assert not hasattr(module, "door_access")
        assert not hasattr(module, "relay_control")

    # -- the five natural-language examples from the product ask --------
    @pytest.mark.parametrize("text,expected", [
        ("AACO, could you show me the front door?", AacoCommand("live_view", camera_id="camera-name:front door")),
        ("AACO, open the front door for me.", AacoCommand("unlock_door", camera_id="camera-name:front door")),
        ("Which of my cameras are down?", AacoCommand("camera_status")),
    ])
    def test_natural_variants_map_onto_the_existing_schema_via_a_fake_interpreter(self, text, expected):
        interpreter = _FakeInterpreter(expected)
        adapter = NaturalAacoLanguageAdapter(interpreter)
        assert adapter.parse(text, now=NOW) == expected

    def test_relative_playback_offset_natural_variant(self):
        start = NOW - timedelta(minutes=20)
        expected = AacoCommand("playback", camera_id="camera-name:driveway", start=start, end=NOW)
        interpreter = _FakeInterpreter(expected)
        adapter = NaturalAacoLanguageAdapter(interpreter)
        result = adapter.parse("Take me back about twenty minutes on the driveway camera.", now=NOW)
        assert result == expected

    def test_camera_scoped_event_search_natural_variant(self):
        start = NOW - timedelta(hours=1)
        expected = AacoCommand("event_search", camera_id="camera-name:front entrance", event_type="person", start=start, end=NOW)
        interpreter = _FakeInterpreter(expected)
        adapter = NaturalAacoLanguageAdapter(interpreter)
        result = adapter.parse("Were there any people at the front entrance in the last hour?", now=NOW)
        assert result == expected


class TestDefaultInterpreter:
    def test_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr(aaco_llm, "LOCAL_LLM_ENABLED", False)
        monkeypatch.setattr(aaco_llm, "LOCAL_LLM_MODEL_PATH", "/some/model.gguf")
        assert default_interpreter() is None

    def test_returns_none_when_no_model_path_even_if_enabled(self, monkeypatch):
        monkeypatch.setattr(aaco_llm, "LOCAL_LLM_ENABLED", True)
        monkeypatch.setattr(aaco_llm, "LOCAL_LLM_MODEL_PATH", "")
        assert default_interpreter() is None

    def test_returns_a_llama_cpp_interpreter_only_when_both_are_set(self, monkeypatch):
        monkeypatch.setattr(aaco_llm, "LOCAL_LLM_ENABLED", True)
        monkeypatch.setattr(aaco_llm, "LOCAL_LLM_MODEL_PATH", "/some/model.gguf")
        interpreter = default_interpreter()
        assert isinstance(interpreter, LlamaCppInterpreter)
        assert interpreter.model_path == "/some/model.gguf"


class TestLlamaCppInterpreterPlumbing:
    def test_no_model_file_configured_raises_interpreter_unavailable(self):
        interpreter = LlamaCppInterpreter(model_path="")
        with pytest.raises(InterpreterUnavailable):
            interpreter._load()

    def test_nonexistent_model_file_raises_interpreter_unavailable(self, tmp_path):
        missing = tmp_path / "does-not-exist.gguf"
        interpreter = LlamaCppInterpreter(model_path=str(missing))
        with pytest.raises(InterpreterUnavailable):
            interpreter._load()

    def test_interpret_extracts_json_from_surrounding_model_chatter(self, monkeypatch):
        interpreter = LlamaCppInterpreter(model_path="/unused")
        monkeypatch.setattr(interpreter, "_generate", lambda text: 'Sure! {"operation": "camera_status"} -- hope that helps.')
        assert interpreter.interpret("Which of my cameras are down?", now=NOW) == AacoCommand("camera_status")

    def test_interpret_returns_clarification_for_non_json_output(self, monkeypatch):
        interpreter = LlamaCppInterpreter(model_path="/unused")
        monkeypatch.setattr(interpreter, "_generate", lambda text: "I don't know what you mean.")
        assert isinstance(interpreter.interpret("garbage", now=NOW), Clarification)

    def test_interpret_still_validates_a_syntactically_valid_but_unsafe_model_output(self, monkeypatch):
        interpreter = LlamaCppInterpreter(model_path="/unused")
        monkeypatch.setattr(interpreter, "_generate", lambda text: '{"operation": "unlock_door", "camera_id": "; rm -rf /"}')
        assert isinstance(interpreter.interpret("open the door", now=NOW), Clarification)

    def test_generate_wraps_a_real_inference_failure_as_interpreter_unavailable(self, tmp_path, monkeypatch):
        model_file = tmp_path / "fake.gguf"
        model_file.write_bytes(b"not a real model")
        interpreter = LlamaCppInterpreter(model_path=str(model_file))

        class _ExplodingModel:
            def create_chat_completion(self, *args, **kwargs):
                raise RuntimeError("inference backend crashed")

        interpreter._model = _ExplodingModel()
        with pytest.raises(InterpreterUnavailable):
            interpreter._generate("some customer text")

    def test_generate_enforces_a_bounded_timeout_instead_of_waiting_indefinitely(self):
        # 2026-09-19: LOCAL_LLM_TIMEOUT_SECONDS existed but was never
        # enforced -- a real gap found during staging validation (an
        # unbounded generation measured as slow as ~77 seconds, and
        # this call happens synchronously inside an async route with no
        # executor of its own, so it blocks the whole single-worker
        # portal for as long as it runs). timeout_seconds is
        # constructor-overridable specifically so this test can prove
        # bounded behavior in well under a second rather than actually
        # waiting out a real multi-second timeout.
        interpreter = LlamaCppInterpreter(model_path="/unused", timeout_seconds=0.2)

        class _SlowModel:
            def create_chat_completion(self, *args, **kwargs):
                time.sleep(2)
                return {"choices": [{"message": {"content": '{"operation": "camera_status"}'}}]}

        interpreter._model = _SlowModel()
        start = time.monotonic()
        with pytest.raises(InterpreterUnavailable):
            interpreter._generate("Which cameras are down?")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, "caller must not wait anywhere near the model's real completion time"

    def test_interpret_raises_interpreter_unavailable_on_timeout_like_every_other_generate_failure(self):
        # interpret() itself never catches InterpreterUnavailable --
        # that is deliberately NaturalAacoLanguageAdapter.parse()'s own
        # job (see TestNaturalAacoLanguageAdapterFallback below), so a
        # timeout must surface here exactly like the existing
        # real-inference-failure case above, not be swallowed early.
        interpreter = LlamaCppInterpreter(model_path="/unused", timeout_seconds=0.2)

        class _SlowModel:
            def create_chat_completion(self, *args, **kwargs):
                time.sleep(2)
                return {"choices": [{"message": {"content": '{"operation": "camera_status"}'}}]}

        interpreter._model = _SlowModel()
        with pytest.raises(InterpreterUnavailable):
            interpreter.interpret("Which cameras are down?", now=NOW)

    def test_natural_adapter_falls_back_to_regex_end_to_end_when_the_real_interpreter_times_out(self):
        """The actual requirement in full, using the real
        LlamaCppInterpreter (not a fake NaturalLanguageInterpreter): a
        slow/hung model must result in the exact same authorized,
        correct AacoCommand the deterministic grammar alone would have
        produced -- proving the timeout->fallback chain actually
        connects LlamaCppInterpreter to NaturalAacoLanguageAdapter,
        not just that each layer individually raises the right
        exception in isolation."""
        interpreter = LlamaCppInterpreter(model_path="/unused", timeout_seconds=0.2)

        class _SlowModel:
            def create_chat_completion(self, *args, **kwargs):
                time.sleep(2)
                return {"choices": [{"message": {"content": '{"operation": "camera_status"}'}}]}

        interpreter._model = _SlowModel()
        adapter = NaturalAacoLanguageAdapter(interpreter)
        start = time.monotonic()
        result = adapter.parse("Which cameras are offline?", now=NOW)
        elapsed = time.monotonic() - start
        assert result == AacoCommand("camera_status")
        assert elapsed < 1.0
