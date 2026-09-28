"""Playback (2026-09-28, reported on the real Living Room):
1. "Duplicate recordings with the exact same timestamp": analytics that
   reuse another event's clip (PPE on a person event) each had their own
   media row, so Playback listed one card per EVENT -- the same clip two
   to four times. Now one card per clip, represented by its owning event.
2. Event-clip deep links (email, timeline) loaded the right clip but left
   it paused at 0:00: browsers refuse unmuted autoplay without a user
   gesture and the event player never retried muted."""
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import main
from database_backend import override_target
from partner_db import initialize_database
from test_playback_recording_event_clip_union import _seed_base_tenant, _seed_event_clip


def _child_clip(conn, camera_id, event_id, parent_id, event_type, started_at, ended_at, s3_key):
    _seed_event_clip(conn, camera_id, event_id, event_type, started_at, ended_at, s3_key=s3_key)
    conn.execute("UPDATE detection_events SET parent_detection_event_id=? WHERE id=?", (parent_id, event_id))
    conn.commit()


def test_a_clip_shared_by_several_events_is_one_playback_card(tmp_path, monkeypatch):
    db_path = tmp_path / "playback_cards.db"
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn, "cam-a", 1)
        start, end = "2026-09-27T04:00:21.121862", "2026-09-27T04:00:31.121862"
        _child_clip(conn, "cam-a", "ppe-1", None, "ppe", start, end, "events/motion_0ae9.mp4")  # child row first
        _seed_event_clip(conn, "cam-a", "person-1", "person", start, end, s3_key="events/motion_0ae9.mp4")
        conn.execute("UPDATE detection_events SET parent_detection_event_id='person-1' WHERE id='ppe-1'")
        _child_clip(conn, "cam-a", "ppe-2", "person-1", "ppe", start, end, "events/motion_0ae9.mp4")
        _seed_event_clip(conn, "cam-a", "car-1", "car", "2026-09-27T04:05:00", "2026-09-27T04:05:10", s3_key="events/motion_other.mp4")
        conn.commit()
        items = main._event_clips_overlapping_utc_range("cam-a", "2026-09-27T00:00:00", "2026-09-28T00:00:00")
    assert [(i["id"], i["name"]) for i in items] == [("person-1", "Person event clip"), ("car-1", "Car event clip")]


def test_distinct_clips_at_the_same_second_are_both_kept(tmp_path, monkeypatch):
    db_path = tmp_path / "playback_cards2.db"
    monkeypatch.setattr(main, "RECORDINGS_FOLDER", tmp_path / "recordings")
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn, "cam-a", 1)
        _seed_event_clip(conn, "cam-a", "e1", "person", "2026-09-27T04:00:21", "2026-09-27T04:00:31", s3_key="events/a.mp4")
        _seed_event_clip(conn, "cam-a", "e2", "car", "2026-09-27T04:00:21", "2026-09-27T04:00:31", s3_key="events/b.mp4")
        items = main._event_clips_overlapping_utc_range("cam-a", "2026-09-27T00:00:00", "2026-09-28T00:00:00")
    assert {i["id"] for i in items} == {"e1", "e2"}


# ---------------------------------------------------------------- event clip autoplay

_HARNESS = r"""
global.window = globalThis;
__PLAYER__
function run(unmutedAllowed, mutedAllowed) {
  const status = { textContent: '' };
  const listeners = {};
  const video = {
    muted: false, paused: true, src: '',
    pause() { this.paused = true; },
    load() {},
    play() {
      const ok = this.muted ? mutedAllowed : unmutedAllowed;
      if (ok) { this.paused = false; return Promise.resolve(); }
      const e = new Error('blocked'); e.name = 'NotAllowedError'; return Promise.reject(e);
    },
    addEventListener(n, f) { listeners[n] = f; }, removeEventListener() {},
  };
  const player = AnyAiCamEventMedia.player({
    video, status,
    fetcher: async () => ({ ok: true, status: 200, json: async () => ({ url: 'https://example.com/clip.mp4' }) }),
    later: (fn, ms) => 0, clear: () => {},
  });
  return player.start('cam-1', 'ev-1', true).then(() => new Promise(r => setTimeout(r, 30))).then(() => ({ muted: video.muted, playing: !video.paused, status: status.textContent }));
}
Promise.all([run(true, true), run(false, true), run(false, false)]).then(r => console.log(JSON.stringify(r)));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_event_clip_autoplay_retries_muted_when_the_browser_blocks_sound(tmp_path):
    player = (Path(__file__).parents[1] / "static" / "event_media.js").read_text(encoding="utf-8")
    script = tmp_path / "autoplay.js"
    script.write_text(_HARNESS.replace("__PLAYER__", player), encoding="utf-8")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    allowed, blocked_sound, blocked_all = json.loads(result.stdout.strip().splitlines()[-1])
    assert allowed["playing"] and not allowed["muted"]                      # normal case unchanged
    assert blocked_sound["playing"] and blocked_sound["muted"]              # arriving from an email link
    assert "muted" in blocked_sound["status"]
    assert not blocked_all["playing"] and "press Play" in blocked_all["status"]
