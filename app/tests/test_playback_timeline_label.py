"""Playback timeline camera label (2026-09-30): "Driveway Right — 2026-09-29" on one
line in a 120px column (84px on phones) was always clipped with an ellipsis."""
from pathlib import Path

import main

SOURCE = Path(main.__file__).read_text(encoding="utf-8")
DASH = chr(92) + "u2014"


def test_label_wraps_instead_of_clipping():
    assert ".timeline-camera-name{font-size:12px;font-weight:750;line-height:1.25;white-space:normal;overflow-wrap:anywhere}" in SOURCE
    assert ".timeline-camera-name small{display:block" in SOURCE
    assert "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.timeline" not in SOURCE


def test_name_and_date_are_separate_text_nodes_with_a_full_tooltip():
    assert "timelineLabel.replaceChildren(document.createTextNode(name),when)" in SOURCE
    assert "when.textContent=date" in SOURCE
    assert "timelineLabel.title=`${{name}} " + DASH + " ${{date}}`" in SOURCE
    assert "timelineLabel.innerHTML" not in SOURCE   # a camera name is never markup
