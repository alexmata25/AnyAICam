"""Dashboard on phones (2026-09-30): the AI activity summary panel was 418px wide on a
390px phone because its single-column layout used a plain 1fr, which cannot shrink
below its content's minimum width."""
from pathlib import Path

import main


def test_dashboard_intelligence_single_column_can_shrink():
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert "@media(max-width:1050px){.dashboard-intelligence{grid-template-columns:minmax(0,1fr)}" in source
    assert "@media(max-width:1050px){.dashboard-intelligence{grid-template-columns:1fr}" not in source
