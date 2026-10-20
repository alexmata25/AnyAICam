"""Windows 1.3.2 (Blue): Software Update never offers or queues the Linux release for a Windows PC.

A Windows PC's agent reports platform "windows" in its heartbeat (test_windows_platform_heartbeat_132.py); the cloud
stores it in appliances.platform; Settings -> System shows "run the new AnyAiCam setup on that computer" instead of
Install, and an install request for that PC is refused. The Windows agent refuses install_update anyway
(windows.UPDATES_UNSUPPORTED), so an offered Install could only end as a failed update.
"""
import sqlite3

from test_pricing_ff_commission import OWNER, _cookie, db_path, portal  # noqa: F401 -- fixtures
from test_software_update_owner import _appliances, _install, published  # noqa: F401 -- fixtures


def _mark_windows(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE appliances SET platform='windows' WHERE id='appl-1'")
    conn.commit()
    conn.close()


def test_a_windows_pc_is_never_offered_the_linux_release(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path)
    before = client.get("/api/customer/software-update", cookies=_cookie(*OWNER)).json()["appliances"][0]
    assert (before["update_available"], before["platform"]) == (True, "linux")
    _mark_windows(db_path)
    after = client.get("/api/customer/software-update", cookies=_cookie(*OWNER)).json()["appliances"][0]
    assert (after["update_available"], after["platform"]) == (False, "windows")
    refused = _install(client)
    assert refused.status_code == 409
    assert "run the new AnyAiCam setup on that computer" in refused.json()["detail"]
    page = client.get("/settings/system", cookies=_cookie(*OWNER)).text
    assert "a.platform==='windows'" in page and "download and run the new AnyAiCam setup" in page
    assert "__WINDOWS_MESSAGE__" not in page
