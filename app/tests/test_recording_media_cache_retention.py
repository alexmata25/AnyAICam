"""Playback cache age limit (ANYAICAM_RECORDING_MEDIA_CACHE_MAX_AGE_DAYS):
off by default; when set (staging: 3 days) it removes only old *.mp4
remuxes directly inside the cache folder -- never other files, nested
folders, or anything outside the cache."""
import os
import time

import main


def _touch(path, age_days, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def _setup(tmp_path, monkeypatch, max_age_days):
    cache = tmp_path / "recordings" / "_media_cache"
    monkeypatch.setattr(main, "RECORDING_MEDIA_CACHE_FOLDER", cache)
    monkeypatch.setattr(main, "RECORDING_MEDIA_CACHE_MAX_AGE_SECONDS", max_age_days * 86400)
    files = {
        "old_clip": _touch(cache / "old.mp4", 5),
        "new_clip": _touch(cache / "new.mp4", 1),
        "old_json": _touch(cache / "index.json", 30),
        "nested_old_clip": _touch(cache / "sub" / "nested.mp4", 30),
        "db": _touch(tmp_path / "recordings" / "anyaicam.db", 30),
        "outside_clip": _touch(tmp_path / "recordings" / "keep.mp4", 30),
    }
    return files


def test_age_limit_removes_only_old_cached_clips(tmp_path, monkeypatch):
    files = _setup(tmp_path, monkeypatch, 3)
    main._evict_recording_media_cache_if_full()
    assert not files["old_clip"].exists()
    for name in ("new_clip", "old_json", "nested_old_clip", "db", "outside_clip"):
        assert files[name].exists(), name


def test_off_by_default_keeps_everything_under_the_size_cap(tmp_path, monkeypatch):
    files = _setup(tmp_path, monkeypatch, 0)
    main._evict_recording_media_cache_if_full()
    assert all(path.exists() for path in files.values())
