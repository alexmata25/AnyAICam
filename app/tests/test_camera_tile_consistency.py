"""Camera-tile consistency pass (2026-09-25): Playback's video uses the same
media card as the Live detail page -- video on top, the shared dark
.camera-tools action strip directly beneath it, both inside one rounded
.panel card. Recording actions (download/share/bookmark) live in that
strip; timeline, dates, clip navigation and page-level controls stay
outside the card. Presentation only: the buttons keep their ids, type,
disabled-until-selected state and labels, so the page's existing JS
(bound by id) drives them exactly as before."""
import re
from pathlib import Path
from types import SimpleNamespace

import main

APP = Path(__file__).resolve().parents[1]
MEDIA_ACTIONS = ("download-selected", "share-selected", "bookmark-selected")
PAGE_LEVEL_CONTROLS = ("skip-back", "timeline-play", "skip-forward", "create-clip", "browse-recordings",
                       "playback-date-input")


def _render(monkeypatch):
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kwargs: [])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: [])
    monkeypatch.setattr(main, "_customer_camera_events", lambda camera_id, date: [])
    request = SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: None))
    return main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], request)


def _media_card(html: str) -> str:
    start = html.index('<div class="panel playback-media-card">')
    end = html.index('</section>', start)
    return html[start:end]


def _timeline_section(html: str) -> str:
    start = html.index('<section class="monitor-timeline" id="playback-monitor-timeline"')
    return html[start:html.index('</section>', start)]


def test_recording_actions_are_inside_the_media_card_under_the_video(monkeypatch):
    card = _media_card(_render(monkeypatch))
    video_at = card.index('<video id="playback-video"')
    strip_at = card.index('<div class="camera-tools" id="playback-media-tools"')
    assert video_at < strip_at
    for button_id in MEDIA_ACTIONS:
        at = card.index(f'<button id="{button_id}" type="button" disabled class="camera-tool"')
        assert at > strip_at, button_id


def test_recording_actions_are_no_longer_in_the_timeline_toolbar(monkeypatch):
    html = _render(monkeypatch)
    timeline = _timeline_section(html)
    for button_id in MEDIA_ACTIONS:
        assert f'id="{button_id}"' not in timeline, button_id
        assert html.count(f'id="{button_id}"') == 1, button_id  # moved, not duplicated


def test_page_level_navigation_stays_outside_the_media_card(monkeypatch):
    html = _render(monkeypatch)
    card = _media_card(html)
    for control_id in PAGE_LEVEL_CONTROLS:
        assert f'id="{control_id}"' not in card, control_id
        assert f'id="{control_id}"' in html, control_id


def test_media_card_reuses_the_live_card_shell(monkeypatch):
    """Same shared classes as live_view_page.py's detail card: .panel >
    .camera-view (video) + .camera-tools strip of .camera-tool buttons."""
    card = _media_card(_render(monkeypatch))
    assert '<div class="camera-view playback-view" id="playback-view-frame"' in card
    assert 'role="toolbar" aria-label="Recording actions"' in card
    assert card.count('class="camera-tool"') == len(MEDIA_ACTIONS)


def test_actions_keep_accessible_names_and_live_glyphs(monkeypatch):
    card = _media_card(_render(monkeypatch))
    for button_id, label, glyph in (("download-selected", "Download", "⬇"), ("share-selected", "Share", "↗"),
                                    ("bookmark-selected", "Bookmark", "◈")):
        match = re.search(rf'<button id="{button_id}"[^>]*>([^<]*)</button>', card)
        assert match and match.group(1) == glyph, button_id
        assert f'aria-label="{label}"' in match.group(0) and 'title="' in match.group(0), button_id


def test_mobile_touch_targets_and_keyboard_focus_are_styled(monkeypatch):
    html = _render(monkeypatch)
    assert "@media(max-width:900px){.playback-media-card .camera-tool{width:44px;height:40px;font-size:17px}}" in html
    assert ".playback-media-card .camera-tool:focus-visible{outline:2px solid" in html


def test_card_is_not_hidden_on_mobile_while_the_timeline_is(monkeypatch):
    """The timeline section (and so its old toolbar) is display:none at
    <=900px; the media card has no such rule, so the actions are now
    reachable on a phone too."""
    html = _render(monkeypatch)
    assert ".monitor-timeline{display:none!important}" in html
    assert not re.search(r"\.playback-media-card[^{]*\{[^}]*display:none", html)


def test_existing_handlers_still_bind_the_same_ids(monkeypatch):
    html = _render(monkeypatch)
    assert "const downloadButton=document.getElementById('download-selected');" in html
    assert "const shareButton=document.getElementById('share-selected');" in html
    assert "downloadButton.addEventListener('click'" in html
    assert "shareButton.addEventListener('click'" in html
    # Refreshes never rebuild the video element or the card: nothing in the
    # page script writes the card's or the frame's innerHTML.
    assert "viewFrame.innerHTML" not in html
    assert "playback-media-tools').innerHTML" not in html


def test_playback_shows_no_button_for_a_feature_it_does_not_have(monkeypatch):
    """Talk-down is a Live-only capability; Playback must not grow a mic."""
    card = _media_card(_render(monkeypatch))
    assert "talk-mic" not in card and "Press and hold to talk" not in card


def test_live_detail_card_is_unchanged():
    source = (APP / "live_view_page.py").read_text(encoding="utf-8")
    for fragment in ('<button class="camera-tool" id="live-view-mute" title="Mute" aria-label="Mute">♪</button>',
                     "const muteButton=document.getElementById('live-view-mute');"):
        assert fragment in source
    css = (APP / "main.py").read_text(encoding="utf-8")
    assert ".camera-tools{display:flex;gap:4px;padding:8px 10px;border-top:1px solid var(--line);overflow-x:auto}" in css
    assert ".camera-tool{flex:0 0 auto;display:grid;place-items:center;width:34px;height:32px;" in css
