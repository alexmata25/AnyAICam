"""Windows 1.3.2 (Blue): the cloud records a Windows PC's platform from its authenticated heartbeat.

Only the Windows agent sends "platform" (metrics.collect); the Linux appliance never does, so its column stays NULL.
Only the exact value "windows" is stored, and a later heartbeat without it never clears it (COALESCE, like
wireguard_status).
"""
from database_backend import override_target
from partner_db import connection
from test_appliance_cloud_wireguard_status import _heartbeat, _seeded, client, db_path  # noqa: F401 -- fixtures


def _platform(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute("SELECT platform FROM appliances WHERE id='appl-1'").fetchone()["platform"]


def test_a_linux_heartbeat_leaves_platform_unset(client, db_path):
    _seeded(db_path)
    assert _heartbeat(client).status_code == 200
    assert _platform(db_path) is None


def test_a_windows_heartbeat_is_recorded_and_kept_and_unknown_values_are_ignored(client, db_path):
    _seeded(db_path)
    assert _heartbeat(client, platform="<b>linux</b>").status_code == 200
    assert _platform(db_path) is None
    assert _heartbeat(client, platform="windows").status_code == 200
    assert _platform(db_path) == "windows"
    assert _heartbeat(client).status_code == 200                       # an omission never clears it
    assert _platform(db_path) == "windows"
