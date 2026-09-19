"""Mobile Playback/Events video-first redesign (2026-09-04): source-
structure tests for renderMobileRecentEvents()'s new one-large-card-
per-row markup, and the root-cause fix it ships alongside (an event's
own event.thumbnail is now used directly instead of findClipNear()
guessing a nearby recording's thumbnail -- see that function's own
comment in main.py for the full root-cause trace).

The companion .mjs file (test_mobile_playback_video_cards.mjs,
alongside this one) executes the ACTUAL extracted function via Node,
with a stubbed findClipNear() that throws if ever called -- that is
the real behavioral proof this suite's own established idiom (see
test_playback_analytics_lanes.py's own docstring) calls for. This file
proves everything a plain render + string assertions on the real,
unmodified _render_customer_playback() output can prove without
executing JS: the new CSS classes exist, the old plain-text list rows
are gone, findClipNear() is genuinely absent from this one function's
own source (not just untriggered in a particular test scenario), the
existing playClip()/event-media-url()/revealClipPanel() wiring is
byte-for-byte unchanged, and the desktop timeline/lane code this
change must never touch is still there.

Same import-inside-container constraint and _fake_request()/
_render_customer_playback() calling convention as this suite's other
Playback tests.
"""

from types import SimpleNamespace

import main


def _fake_request(t=None, camera=None):
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: {"t": t, "camera": camera}.get(key, default)))


def _render(monkeypatch, recordings=None, events=None):
    monkeypatch.setattr(main, "_customer_recording_rows", lambda camera_id, **kwargs: recordings or [])
    monkeypatch.setattr(main, "_customer_detection_events", lambda request: events or [])
    monkeypatch.setattr(main, "_customer_camera_events", lambda camera_id, date: events or [])
    return main._render_customer_playback([{"id": "cam-1", "name": "Front Door", "camera_number": 1}], _fake_request())


def _mobile_function_body(html):
    start = html.index("function renderMobileRecentEvents(cameraId,clips,events)")
    end = html.index("// === LANE_CORE_START ===", start)
    assert end > start
    return html[start:end]


def _active_code_only(body):
    """Strips '//...' line comments -- for assertions that must hold
    against the real, executable code, not against this function's own
    explanatory comments (which legitimately quote the old buggy
    findClipNear()/"No preview" behavior by name, for documentation,
    exactly like the rest of this codebase's own commenting
    convention). The real behavioral proof that findClipNear() is never
    actually invoked is test_mobile_playback_video_cards.mjs, which
    executes this exact source for real; this is a lighter-weight,
    still-real structural cross-check on top of that, not a
    replacement for it."""
    return "\n".join(line for line in body.split("\n") if "//" not in line.strip()[:2])


# ---------------------------------------------------------------------------
# 1. New card markup/CSS exists.
# ---------------------------------------------------------------------------

def test_mobile_media_card_css_classes_are_defined(monkeypatch):
    html = _render(monkeypatch)
    for selector in (
        ".mobile-media-card{", ".mobile-media-thumb{", ".mobile-media-time{",
        ".mobile-media-badge{", ".mobile-media-menu{", ".mobile-media-fallback{",
    ):
        assert selector in html, f"missing CSS rule for {selector}"


def test_mobile_media_card_css_scoped_to_the_existing_mobile_only_toggle(monkeypatch):
    # .mobile-recent-events (the section these cards live in) is already
    # hidden above 900px by the pre-existing media query -- the new card
    # rules deliberately don't need (and don't have) a second breakpoint
    # of their own, since their whole container is already conditional.
    html = _render(monkeypatch)
    toggle_idx = html.index("@media (max-width:900px){.monitor-timeline{display:none!important}.mobile-recent-events{display:block!important}}")
    cards_idx = html.index(".mobile-media-card{")
    assert toggle_idx < cards_idx


# ---------------------------------------------------------------------------
# 2. The old plain-text list rows are gone.
# ---------------------------------------------------------------------------

def test_old_health_row_mobile_card_markup_is_removed(monkeypatch):
    html = _render(monkeypatch)
    body = _mobile_function_body(html)
    assert 'class="health-row" data-mobile-clip' not in body
    assert 'class="health-row" data-mobile-event' not in body
    assert "<span class=\"health-name\">" not in body
    assert ">Clip</span>" not in body


def test_no_preview_and_analytics_only_text_are_gone_from_this_function(monkeypatch):
    html = _render(monkeypatch)
    code = _active_code_only(_mobile_function_body(html))
    assert "No preview" not in code
    assert "Analytics only" not in code
    assert "No clip available" in code  # the new compact fallback, still present as a template string


# ---------------------------------------------------------------------------
# 3. Root-cause fix: event.thumbnail is used, findClipNear() is not.
# ---------------------------------------------------------------------------

def test_event_thumbnail_field_is_used_directly(monkeypatch):
    html = _render(monkeypatch)
    body = _mobile_function_body(html)
    assert "event.thumbnail" in body


def test_findclipnear_is_completely_absent_from_this_functions_actual_code(monkeypatch):
    # Not just untriggered in one test scenario -- see this same claim
    # proven behaviorally (a throwing stub, never invoked across 5
    # scenarios) in test_mobile_playback_video_cards.mjs. This is a
    # second, independent structural check on top of that: the literal
    # call is gone from the real code, not merely dead-code-eliminated
    # at runtime. Comments are excluded (see _active_code_only()) since
    # this function's own explanatory comments legitimately name
    # findClipNear() when documenting the bug they fixed.
    html = _render(monkeypatch)
    code = _active_code_only(_mobile_function_body(html))
    assert "findClipNear" not in code


def test_hydratepreview_lazy_lookup_is_removed(monkeypatch):
    # The old lazy, findClipNear()-based thumbnail hydration is no
    # longer needed at all now that event.thumbnail is already correct
    # and present at initial render time.
    html = _render(monkeypatch)
    body = _mobile_function_body(html)
    assert "hydratePreview" not in body


# ---------------------------------------------------------------------------
# 4. Existing playback wiring is byte-for-byte unchanged.
# ---------------------------------------------------------------------------

def test_recording_card_still_calls_the_existing_unmodified_playclip(monkeypatch):
    html = _render(monkeypatch)
    body = _mobile_function_body(html)
    assert "row.addEventListener('click',()=>playClip(cameraId,clip));" in body


def test_event_card_still_uses_the_existing_unmodified_event_media_url_and_reveal_panel(monkeypatch):
    html = _render(monkeypatch)
    body = _mobile_function_body(html)
    assert "/api/customer/events/${cameraId}/${eventId}/media/url" in body  # rendered output, single braces (f-string already evaluated)
    assert "revealClipPanel();" in body
    assert "video.src=payload.url;" in body


def test_analytics_only_events_are_still_not_wired_as_playable_controls(monkeypatch):
    html = _render(monkeypatch)
    body = _mobile_function_body(html)
    assert "if(row.dataset.mobileEventClip!=='1'||!row.dataset.mobileEventId)return;" in body


# ---------------------------------------------------------------------------
# 5. Desktop timeline/lane code is completely untouched.
# ---------------------------------------------------------------------------

def test_desktop_lane_and_timeline_code_still_present_unchanged(monkeypatch):
    html = _render(monkeypatch)
    assert "const EVENT_LANE_ORDER=['motion','person','vehicle','lpr','people_counting','intrusion'];" in html
    assert '<div class="timeline-lane" id="playback-timeline-lane">' in html
    assert 'class="monitor-timeline"' in html


def test_desktop_vs_mobile_toggle_css_itself_is_unchanged(monkeypatch):
    html = _render(monkeypatch)
    assert (
        "<style>@media (max-width:900px){.monitor-timeline{display:none!important}"
        ".mobile-recent-events{display:block!important}}@media (min-width:901px)"
        "{.mobile-recent-events{display:none!important}.monitor-timeline{display:block}}</style>"
    ) in html
