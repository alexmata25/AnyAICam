"""video-pipeline P2b (2026-10-11): ANYAICAM_LIVE_HLS_MODE=auto (the new
default) runs the live HLS encoder on demand only while no always-on
consumer of the HLS output remains; otherwise it behaves exactly like
"always"."""
import os
import time

import pytest

import live_hls_demand
import main


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _restore():
    yield
    live_hls_demand.configure(mode="always")
    live_hls_demand.set_auto_policy(main._live_hls_auto_policy, log=print)


def test_default_mode_is_auto_and_unknown_is_always(monkeypatch):
    monkeypatch.delenv("ANYAICAM_LIVE_HLS_MODE", raising=False)
    assert live_hls_demand.configured_mode() == "auto"
    assert live_hls_demand.configured_mode("bogus") == "always"
    assert live_hls_demand.configured_mode("on-demand") == "on_demand"


def test_auto_without_a_free_path_is_exactly_always():
    clock = Clock()
    live_hls_demand.configure(mode="auto", idle_seconds=60, clock=clock)
    live_hls_demand.set_auto_policy(lambda: False)
    assert not live_hls_demand.on_demand()
    assert live_hls_demand.wanted(1)                    # encoder runs with no demand at all
    clock.now += 10_000
    assert live_hls_demand.wanted(1)
    live_hls_demand.set_auto_policy(None)
    assert not live_hls_demand.on_demand() and live_hls_demand.wanted(1)


def test_auto_goes_on_demand_with_a_grace_period_for_running_encoders():
    clock = Clock()
    logs = []
    live_hls_demand.configure(mode="auto", idle_seconds=60, clock=clock)
    live_hls_demand.set_auto_policy(lambda: True, log=logs.append)
    assert live_hls_demand.on_demand()
    assert any("on demand" in line for line in logs)
    assert live_hls_demand.wanted(1)                    # just switched: a full idle period first
    clock.now += 61
    assert not live_hls_demand.wanted(1)                # then idle cameras stop
    live_hls_demand.touch(1, "http")
    assert live_hls_demand.wanted(1)                    # a viewer starts it again
    live_hls_demand.hold(2, "motion")
    clock.now += 1000
    assert live_hls_demand.wanted(2) and not live_hls_demand.wanted(1)


def test_auto_records_demand_while_effectively_always_and_rechecks_policy():
    clock = Clock()
    decision = {"value": False}
    calls = []

    def policy():
        calls.append(clock.now)
        return decision["value"]

    live_hls_demand.configure(mode="auto", idle_seconds=60, clock=clock)
    live_hls_demand.set_auto_policy(policy)
    assert not live_hls_demand.on_demand()
    live_hls_demand.touch(3, "http")                     # recorded although always-on now
    assert live_hls_demand.state(3)["seconds_since_demand"] == 0.0
    decision["value"] = True
    clock.now += 5
    assert not live_hls_demand.on_demand()              # cached for AUTO_RECHECK_SECONDS
    clock.now += live_hls_demand.AUTO_RECHECK_SECONDS
    assert live_hls_demand.on_demand() and len(calls) == 2
    decision["value"] = False
    clock.now += live_hls_demand.AUTO_RECHECK_SECONDS
    assert not live_hls_demand.on_demand() and live_hls_demand.wanted(9)   # back to always


def test_policy_exception_means_always():
    live_hls_demand.configure(mode="auto", clock=Clock())
    live_hls_demand.set_auto_policy(lambda: 1 / 0)
    assert not live_hls_demand.on_demand()


def test_alive_file_only_written_when_effectively_on_demand(tmp_path):
    clock = Clock()
    live_hls_demand.configure(mode="auto", clock=clock)
    live_hls_demand.set_auto_policy(lambda: False)
    live_hls_demand.write_alive(tmp_path, 4)
    assert not (tmp_path / "camera4.alive").exists()
    live_hls_demand.set_auto_policy(lambda: True)
    live_hls_demand.write_alive(tmp_path, 4)
    assert (tmp_path / "camera4.alive").exists()


def test_always_mode_is_unchanged():
    live_hls_demand.configure(mode="always", clock=Clock())
    live_hls_demand.set_auto_policy(lambda: True)
    assert not live_hls_demand.on_demand()
    live_hls_demand.touch(1)
    assert live_hls_demand.state(1)["seconds_since_demand"] is None


# ------------------------------------------------------------- main's policy

def test_main_policy_requires_non_hls_readers_and_a_fresh_agent_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "HLS_FOLDER", tmp_path)
    monkeypatch.setattr(main.ai_frames, "mode", "auto")
    monkeypatch.setattr(main._motion_source_selector, "mode", "auto")
    monkeypatch.setattr(main, "MOTION_DETECTION_ENABLED", True)
    assert main._live_hls_auto_policy() is False          # no agent marker: an older agent
    marker = tmp_path / live_hls_demand.AGENT_MARKER
    marker.touch()
    assert main._live_hls_auto_policy() is True
    monkeypatch.setattr(main.ai_frames, "mode", "hls")
    assert main._live_hls_auto_policy() is False          # AI reads HLS: always-on consumer
    monkeypatch.setattr(main.ai_frames, "mode", "substream")
    monkeypatch.setattr(main._motion_source_selector, "mode", "hls")
    assert main._live_hls_auto_policy() is False          # motion reads HLS
    monkeypatch.setattr(main, "MOTION_DETECTION_ENABLED", False)
    assert main._live_hls_auto_policy() is True
    old = time.time() - live_hls_demand.AGENT_MARKER_FRESH_SECONDS - 60
    os.utime(marker, (old, old))
    assert main._live_hls_auto_policy() is False          # agent stopped / rolled back


def test_agent_writes_the_marker_rate_limited(tmp_path):
    from anyaicam_agent.camera_binding import LocalVmsStatusReader
    reader = LocalVmsStatusReader(tmp_path, tmp_path / "rec")
    reader.status(1, now=1000.0)
    marker = tmp_path / live_hls_demand.AGENT_MARKER
    assert marker.exists() and LocalVmsStatusReader.ALIVE_SUPPORT_MARKER == live_hls_demand.AGENT_MARKER
    os.utime(marker, (1, 1))
    reader.status(2, now=1010.0)
    assert marker.stat().st_mtime == 1                      # not rewritten within a minute
    reader.status(2, now=1100.0)
    assert marker.stat().st_mtime > 1
    missing = LocalVmsStatusReader(tmp_path / "nope", tmp_path / "rec")
    missing.status(1)                                      # no HLS folder: no crash, nothing created
    assert not (tmp_path / "nope").exists()
