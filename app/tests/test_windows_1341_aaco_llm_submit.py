"""aaco_llm: a pool that is already broken when submit() is called is handled like a crash seen through result()
(independent Codex review of R0 77070be, 2026-11-06). Local inference is opt-in and not packaged on Windows; this
pins the robustness gap for opt-in configurations. No worker process is started here."""
import concurrent.futures

import pytest

import aaco_llm
from aaco_llm import InterpreterUnavailable, LlamaCppInterpreter


def _never_called(*_args):
    raise AssertionError("inference must not run")


class _BrokenOnSubmit:
    def submit(self, *_args, **_kwargs):
        raise concurrent.futures.process.BrokenProcessPool("pool already broken")


def test_a_broken_shared_pool_at_submit_is_unavailable_and_discarded(tmp_path, monkeypatch):
    model_file = tmp_path / "fake.gguf"
    model_file.write_bytes(b"not a real model")
    broken = _BrokenOnSubmit()
    monkeypatch.setattr(aaco_llm, "_PROCESS_EXECUTOR", broken)
    interpreter = LlamaCppInterpreter(model_path=str(model_file), infer_fn=_never_called, timeout_seconds=5)
    with pytest.raises(InterpreterUnavailable, match="crashed"):
        interpreter._generate("Which cameras are down?")
    assert aaco_llm._PROCESS_EXECUTOR is None  # the next call builds a fresh pool


def test_a_broken_pool_at_submit_falls_back_to_the_regex_grammar(tmp_path, monkeypatch):
    from datetime import datetime
    model_file = tmp_path / "fake.gguf"
    model_file.write_bytes(b"not a real model")
    monkeypatch.setattr(aaco_llm, "_PROCESS_EXECUTOR", _BrokenOnSubmit())
    adapter = aaco_llm.NaturalAacoLanguageAdapter(
        LlamaCppInterpreter(model_path=str(model_file), infer_fn=_never_called, timeout_seconds=5))
    adapter.parse("Which cameras are down?", now=datetime.now())  # must not raise
