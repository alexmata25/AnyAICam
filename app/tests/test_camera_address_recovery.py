"""Camera address recovery (VMS 1.2.7, camera_address_recovery.py).

The scenario that motivated it (Ryzen, 2026-10-12): after a power outage two
driveway cameras came back from DHCP on new addresses (.44 -> .43 and
.70 -> .69) and the VMS retried the old ones forever. These tests drive the
real recovery worker against a simulated LAN: devices with ONVIF UUIDs, mDNS
names and RTSP credentials that move between addresses. The local database is
seeded through the real cloud->edge sync (edge_camera_sync), so the "stale
cloud sync" and "administrator change" cases exercise the real overwrite.
"""
import json
import socket
import sqlite3
import threading
from urllib.parse import urlsplit, unquote

import pytest

import camera_address_recovery as car
import edge_camera_sync
from appliance_protocol import encrypt_camera_credentials
from database_backend import override_target
from partner_db import connection, initialize_database

KEY = "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY="
RIGHT = "urn:uuid:fd430000-f7d4-11b4-83a1-142ffd60a285"
LEFT = "urn:uuid:d45ec000-f254-11b4-839a-142ffd606e23"
OTHER = "urn:uuid:a1348000-043b-11b4-826b-142ffda08360"
PATH = "/Streaming/Channels/101?transportmode=unicast&profile=Profile_1"
IDENTITY = {"appliance_id": "appl-ryzen", "cloud_id": "AIC-RYZEN0001", "credential": "c", "customer_id": "cust-1", "site_id": "site-1"}


# ------------------------------------------------------------- simulated LAN

class Device:
    def __init__(self, device_key, mdns, hardware="CMIP1042W-28MA", username="admin", password="pw-right", video=True, onvif=True):
        self.device_key, self.mdns, self.hardware = device_key, mdns, hardware
        self.username, self.password, self.video, self.onvif = username, password, video, onvif


class Lan:
    """ip -> Device. Records every credentialed contact so tests can prove a
    credential never reached a device that was not identified first."""

    def __init__(self):
        self.hosts: dict[str, Device] = {}
        self.credential_contacts: list[tuple[str, str]] = []
        self.sweeps = 0
        self.extra_sightings: list[car.Sighting] = []

    def probe(self, ip, timeout):
        device = self.hosts.get(ip)
        if not device or not device.onvif:
            return None
        return car.Sighting(ip=ip, device_key=device.device_key, hardware=device.hardware, xaddr_hosts=(ip,))

    def sweep(self, ips, timeout):
        self.sweeps += 1
        found = [self.probe(ip, timeout) for ip in ips if ip in self.hosts]
        return [s for s in found if s] + list(self.extra_sightings)

    def mdns(self, ip, timeout):
        device = self.hosts.get(ip)
        return device.mdns if device else None

    def verify(self, url, timeout):
        parsed = urlsplit(url)
        self.credential_contacts.append((parsed.hostname, unquote(parsed.username or "")))
        device = self.hosts.get(parsed.hostname)
        if not device:
            return False, {"reason": "stream_not_opened"}
        if (unquote(parsed.username or ""), unquote(parsed.password or "")) != (device.username, device.password):
            return False, {"reason": "stream_not_opened"}
        if not device.video:
            return False, {"reason": "no_video_stream"}
        return True, {"codec": "h264", "width": 2560, "height": 1440}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeProcess:
    def __init__(self, url):
        self.args = ["ffmpeg", "-rtsp_transport", "tcp", "-i", url, "-c", "copy", "out.mp4"]
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True


# ------------------------------------------------------------------ fixtures

@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "recovery.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", KEY)
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: IDENTITY)
    monkeypatch.setattr(car, "ENABLED", True)
    car.reset_state()
    with override_target(sqlite_path=str(path)):
        yield path
    car.reset_state()


def cloud_camera(camera_id, number, name, device_key, host, **extra):
    return {"id": camera_id, "name": name, "camera_number": number, "status": "configured",
            "device_key": device_key, "onvif_endpoint": f"rtsp://{host}:554{PATH}",
            "resolution": "4mp", "recording_mode": None, "people_counting_enabled": 0,
            "local_recording_mode": "event", "local_recording_pre_roll_seconds": 10, **extra}


def sync_from_cloud(monkeypatch, cameras):
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get",
                        lambda path, appliance_id, credential: {"cameras": cameras, "configuration_version": "v"})
    result = edge_camera_sync.sync_provisioned_cameras()
    assert result["status"] == "ok", result


def seed_credentials(device_key, username, password):
    with connection() as db:
        db.execute(
            "INSERT INTO pending_camera_credentials(device_key,encrypted_blob,created_at) VALUES(?,?,?)",
            (device_key, encrypt_camera_credentials(username, password), "2026-10-01T00:00:00"),
        )


@pytest.fixture()
def driveway(db_path, monkeypatch):
    """Two driveway cameras provisioned at .44 and .70, as on the Ryzen."""
    seed_credentials(RIGHT, "admin", "pw-right")
    seed_credentials(LEFT, "admin", "pw-left")
    cloud = [cloud_camera("dc7a226120", 2, "Driveway Right", RIGHT, "192.168.0.44"),
             cloud_camera("5c689a0c0e", 3, "Driveway Left", LEFT, "192.168.0.70")]
    sync_from_cloud(monkeypatch, cloud)
    lan = Lan()
    lan.hosts["192.168.0.44"] = Device(RIGHT, "cmip1042w-28ma20250929aawrgg6611454")
    lan.hosts["192.168.0.70"] = Device(LEFT, "cmip1042w-28ma20250922aawrgg2918806", password="pw-left")
    clock = Clock()
    alerts = []
    processes = []
    streaming = set()
    deps = car.Dependencies(probe=lan.probe, sweep=lan.sweep, mdns_name=lan.mdns, verify_stream=lan.verify,
                            is_streaming=lambda n: n in streaming, processes=lambda: processes,
                            alert=alerts.append, clock=clock)
    return {"lan": lan, "clock": clock, "alerts": alerts, "processes": processes, "streaming": streaming,
            "deps": deps, "cloud": cloud, "db_path": db_path}


def rows(db_path, query, params=()):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(query, params)]
    finally:
        con.close()


def camera_row(db_path, camera_id):
    return rows(db_path, "SELECT * FROM cameras WHERE id=?", (camera_id,))[0]


def events(db_path, camera_id=None):
    query = "SELECT event, detail FROM camera_address_recovery_events" + (" WHERE camera_id=?" if camera_id else "") + " ORDER BY created_at, rowid"
    return [(r["event"], json.loads(r["detail"])) for r in rows(db_path, query, (camera_id,) if camera_id else ())]


def endpoint(camera_id, s, lan, clock):
    cam = camera_row(s["db_path"], camera_id)
    return car.stream_endpoint(cam["id"], cam["device_key"], cam["onvif_endpoint"], cam["camera_number"], probe=lan.probe, clock=clock)


def move(lan, old, new):
    lan.hosts[new] = lan.hosts.pop(old)


def passes(s, count, step=car.CHECK_INTERVAL_SECONDS):
    out = []
    for _ in range(count):
        out.append(car.run_pass(s["deps"]))
        s["clock"].advance(step)
    return out


# --------------------------------------------------------------- unit: helpers

def test_replace_host_keeps_scheme_port_path_and_query():
    assert car.replace_host(f"rtsp://192.168.0.44:554{PATH}", "192.168.0.43") == f"rtsp://192.168.0.43:554{PATH}"
    assert car.replace_host("rtsp://10.0.0.5/live", "10.0.0.9") == "rtsp://10.0.0.9/live"


def test_probe_match_uses_the_endpoint_reference_not_message_ids():
    reply = (
        '<?xml version="1.0"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
        '<e:Header><w:MessageID>uuid:11111111-1111-1111-1111-111111111111</w:MessageID>'
        '<w:RelatesTo>uuid:22222222-2222-2222-2222-222222222222</w:RelatesTo></e:Header>'
        '<e:Body><d:ProbeMatches><d:ProbeMatch><w:EndpointReference><w:Address>urn:uuid:FD430000-F7D4-11B4-83A1-142FFD60A285</w:Address></w:EndpointReference>'
        '<d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/hardware/CMIP1042W-28MA</d:Scopes>'
        '<d:XAddrs>http://192.168.0.43/onvif/device_service http://[fe80::1]/onvif/device_service</d:XAddrs>'
        '</d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>'
    )
    sighting = car.parse_probe_match("192.168.0.43", reply)
    assert sighting.device_key == RIGHT
    assert sighting.hardware == "CMIP1042W-28MA"
    assert "192.168.0.43" in sighting.xaddr_hosts
    assert car.parse_probe_match("192.168.0.43", "<not xml") is None


def _ptr_response(name):
    import struct
    header = struct.pack(">HHHHHH", 0, 0x8400, 1, 1, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode() for p in "43.0.168.192.in-addr.arpa".split(".")) + b"\x00"
    target = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 12, 1, 120, len(target)) + target
    return header + q + struct.pack(">HH", 12, 1) + answer


def test_ptr_answer_is_parsed_to_the_camera_hostname():
    assert car.parse_ptr_answer(_ptr_response("CMIP1042W-28MA20250929AAWRGG6611454.local")) == "cmip1042w-28ma20250929aawrgg6611454"
    assert car.parse_ptr_answer(b"\x00" * 5) is None


def test_real_udp_probe_and_mdns_round_trip_on_loopback():
    """The default transports over real sockets: a fake camera on 127.0.0.1
    answering a directed Probe on 3702 and a unicast mDNS query on 5353."""
    probe_reply = (
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><e:Body><d:ProbeMatches><d:ProbeMatch>'
        f'<w:EndpointReference><w:Address>{RIGHT}</w:Address></w:EndpointReference>'
        '<d:XAddrs>http://127.0.0.1/onvif/device_service</d:XAddrs></d:ProbeMatch></d:ProbeMatches></e:Body></e:Envelope>'
    ).encode()
    servers = []
    try:
        for port, reply in ((car.WS_DISCOVERY_PORT, probe_reply), (car.MDNS_PORT, _ptr_response("CMIP1042W-28MA20250929AAWRGG6611454.local"))):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                pytest.skip(f"UDP port {port} not bindable here")
            sock.settimeout(3)
            servers.append(sock)

            def serve(s=sock, r=reply):
                try:
                    _, addr = s.recvfrom(9000)
                    s.sendto(r, addr)
                except OSError:
                    pass
            threading.Thread(target=serve, daemon=True).start()
        assert car.default_probe("127.0.0.1", 2).device_key == RIGHT
        assert car.mdns_reverse_name("127.0.0.1", 2) == "cmip1042w-28ma20250929aawrgg6611454"
    finally:
        for sock in servers:
            sock.close()


# -------------------------------------------------- the driveway incident

def test_healthy_cameras_learn_secondary_identity_and_change_nothing(driveway):
    s = driveway
    passes(s, 1)
    evidence = {r["camera_id"]: r for r in rows(s["db_path"], "SELECT * FROM camera_identity_evidence")}
    assert evidence["dc7a226120"]["mdns_name"] == "cmip1042w-28ma20250929aawrgg6611454"
    assert evidence["dc7a226120"]["onvif_hardware"] == "CMIP1042W-28MA"
    assert rows(s["db_path"], "SELECT * FROM camera_address_overrides") == []
    assert s["lan"].credential_contacts == []
    assert s["lan"].sweeps == 0


def test_both_driveway_cameras_recover_after_dhcp_moves_them(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)  # learn identity while healthy
    before = {cid: camera_row(s["db_path"], cid) for cid in ("dc7a226120", "5c689a0c0e")}
    creds_before = rows(s["db_path"], "SELECT * FROM camera_credentials ORDER BY camera_id")
    move(lan, "192.168.0.44", "192.168.0.43")
    move(lan, "192.168.0.70", "192.168.0.69")
    outcomes = passes(s, 2)
    assert outcomes[0] == {"dc7a226120": "unanswered", "5c689a0c0e": "unanswered"}
    assert outcomes[1] == {"dc7a226120": "recovered", "5c689a0c0e": "recovered"}
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.43:554{PATH}"
    assert endpoint("5c689a0c0e", s, lan, s["clock"]) == f"rtsp://192.168.0.69:554{PATH}"
    # Nothing about the cameras themselves changed: same rows, same
    # credentials, same recording settings, same identity.
    for cid, row in before.items():
        assert camera_row(s["db_path"], cid) == row
    assert rows(s["db_path"], "SELECT * FROM camera_credentials ORDER BY camera_id") == creds_before
    # Credentials were used only against the identified new addresses.
    assert sorted(lan.credential_contacts) == [("192.168.0.43", "admin"), ("192.168.0.69", "admin")]
    recovered = [d for e, d in events(s["db_path"], "dc7a226120") if e == "recovered"][0]
    assert recovered["previous_host"] == "192.168.0.44" and recovered["new_host"] == "192.168.0.43"
    assert recovered["verified_by"] == ["onvif_uuid", "mdns_name", "onvif_hardware", "rtsp_stream"]
    assert s["alerts"] == []  # automatic recovery succeeded: no customer notification


def test_recovered_address_reaches_the_real_stream_url_with_the_stored_credentials(driveway):
    import main
    s = driveway
    passes(s, 1)
    move(s["lan"], "192.168.0.44", "192.168.0.43")
    passes(s, 2)
    import camera_address_recovery
    original = camera_address_recovery.default_probe
    camera_address_recovery.default_probe = s["lan"].probe
    try:
        stream = main._provisioned_camera_stream(2)
    finally:
        camera_address_recovery.default_probe = original
    assert stream["rtsp_url"] == f"rtsp://192.168.0.43:554{PATH}"
    assert (stream["username"], stream["password"]) == ("admin", "pw-right")


def test_running_streams_to_the_old_address_are_stopped_after_recovery(driveway):
    s = driveway
    passes(s, 1)
    stale = FakeProcess(f"rtsp://admin:pw-right@192.168.0.44:554{PATH}")
    unrelated = FakeProcess(f"rtsp://admin:pw-left@192.168.0.70:554{PATH}")
    s["processes"].extend([stale, unrelated])
    move(s["lan"], "192.168.0.44", "192.168.0.43")
    passes(s, 2)
    assert stale.terminated and not unrelated.terminated


# ------------------------------------------- a different camera at the old IP

def test_a_different_device_at_the_old_address_is_never_connected(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    running = FakeProcess(f"rtsp://admin:pw-right@192.168.0.44:554{PATH}")
    s["processes"].append(running)
    # Driveway Right is gone; some other camera got .44 from DHCP.
    lan.hosts.pop("192.168.0.44")
    lan.hosts["192.168.0.44"] = Device(OTHER, "cmip3382wi-28sdl20250619aawrgb7027083", hardware="CMIP3382WI-28SDL", password="pw-right")
    outcome = passes(s, 1)[0]["dc7a226120"]
    assert outcome == "not_found"
    assert running.terminated  # the stream that may have reached the wrong camera is stopped
    with pytest.raises(car.CameraIdentityHoldError):
        endpoint("dc7a226120", s, lan, s["clock"])
    assert lan.credential_contacts == []  # even though the same password would have worked there
    assert rows(s["db_path"], "SELECT * FROM camera_address_overrides") == []
    # The real camera turns up at .43: recovered, hold released.
    lan.hosts["192.168.0.43"] = Device(RIGHT, "cmip1042w-28ma20250929aawrgg6611454")
    s["clock"].advance(car.BACKOFF_SECONDS[0])
    assert passes(s, 1)[0]["dc7a226120"] == "recovered"
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.43:554{PATH}"
    assert lan.credential_contacts == [("192.168.0.43", "admin")]


def test_the_stream_gate_holds_even_before_the_worker_runs(driveway):
    """First stream start after a reboot: no worker check yet, so the gate
    probes the address itself and refuses a different device."""
    s = driveway
    lan = s["lan"]
    lan.hosts["192.168.0.44"] = Device(OTHER, "someone-else", hardware="X")
    with pytest.raises(car.CameraIdentityHoldError):
        endpoint("dc7a226120", s, lan, s["clock"])


def test_two_cameras_that_swap_addresses_each_recover_to_the_right_one(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    right, left = lan.hosts["192.168.0.44"], lan.hosts["192.168.0.70"]
    lan.hosts["192.168.0.44"], lan.hosts["192.168.0.70"] = left, right
    passes(s, 2)
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.70:554{PATH}"
    assert endpoint("5c689a0c0e", s, lan, s["clock"]) == f"rtsp://192.168.0.44:554{PATH}"
    assert sorted(lan.credential_contacts) == [("192.168.0.44", "admin"), ("192.168.0.70", "admin")]
    # each camera's own password went only to its own device
    assert lan.verify(f"rtsp://admin:pw-right@192.168.0.70:554{PATH}", 1)[0]
    assert lan.verify(f"rtsp://admin:pw-left@192.168.0.44:554{PATH}", 1)[0]


# --------------------------------------------------------- identity conflicts

def test_duplicate_identity_is_refused_and_notified_once(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    lan.hosts.pop("192.168.0.44")
    lan.hosts["192.168.0.43"] = Device(RIGHT, "cmip1042w-28ma20250929aawrgg6611454")
    lan.hosts["192.168.0.88"] = Device(RIGHT, "cmip1042w-28ma20250929aawrgg6611454")  # cloned UUID
    passes(s, 2)
    s["clock"].advance(car.BACKOFF_SECONDS[-1])
    passes(s, 2)
    assert rows(s["db_path"], "SELECT * FROM camera_address_overrides") == []
    assert lan.credential_contacts == []
    assert [a["event_type"] for a in s["alerts"]] == ["camera_recovery_failed"]
    assert "more than one device" in s["alerts"][0]["message"]
    assert any(e == "duplicate_identity" for e, _ in events(s["db_path"], "dc7a226120"))


def test_contradicting_mdns_name_is_refused_before_credentials_are_used(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)  # learns cmip1042w-28ma20250929aawrgg6611454
    lan.hosts.pop("192.168.0.44")
    lan.hosts["192.168.0.43"] = Device(RIGHT, "an-impostor-with-a-copied-uuid")
    passes(s, 2)
    assert lan.credential_contacts == []
    assert rows(s["db_path"], "SELECT * FROM camera_address_overrides") == []
    assert ("secondary_identity_mismatch", {"candidate_host": "192.168.0.43", "check": "mdns_name"}) in events(s["db_path"], "dc7a226120")
    assert len(s["alerts"]) == 1


def test_contradicting_hardware_model_is_refused(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    lan.hosts.pop("192.168.0.44")
    lan.hosts["192.168.0.43"] = Device(RIGHT, None, hardware="SOME-OTHER-MODEL")
    passes(s, 2)
    assert lan.credential_contacts == []
    assert rows(s["db_path"], "SELECT * FROM camera_address_overrides") == []


def test_a_device_answering_for_another_address_is_not_a_candidate(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    lan.hosts.pop("192.168.0.44")
    lan.extra_sightings.append(car.Sighting(ip="192.168.0.50", device_key=RIGHT, hardware="CMIP1042W-28MA", xaddr_hosts=("192.168.0.99",)))
    passes(s, 2)
    assert lan.credential_contacts == []


def test_a_failed_stream_check_changes_nothing_and_backs_off(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    move(lan, "192.168.0.44", "192.168.0.43")
    lan.hosts["192.168.0.43"].video = False
    passes(s, 2)
    assert rows(s["db_path"], "SELECT * FROM camera_address_overrides") == []
    sweeps = lan.sweeps
    passes(s, 1)  # within the 30 s backoff: no new search
    assert lan.sweeps == sweeps
    lan.hosts["192.168.0.43"].video = True
    s["clock"].advance(car.BACKOFF_SECONDS[1])
    passes(s, 1)
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.43:554{PATH}"


# ------------------------------------------------------ outages and restarts

def test_a_camera_that_is_really_offline_is_left_alone_and_notified_once(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    lan.hosts.pop("192.168.0.44")
    for _ in range(80):  # 20 minutes of passes
        passes(s, 1)
    assert rows(s["db_path"], "SELECT * FROM camera_address_overrides") == []
    assert [a["camera"] for a in s["alerts"]] == [2]
    assert "could not find it" in s["alerts"][0]["message"]
    assert lan.sweeps < 15  # backoff, not a search every 15 s
    # It comes back where it was: the incident ends, no override.
    lan.hosts["192.168.0.44"] = Device(RIGHT, "cmip1042w-28ma20250929aawrgg6611454")
    assert passes(s, 1)[0]["dc7a226120"] == "healthy"
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.44:554{PATH}"


def test_router_outage_then_new_leases(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    saved = dict(lan.hosts)
    lan.hosts.clear()  # router down: nothing answers
    passes(s, 8)
    assert rows(s["db_path"], "SELECT * FROM camera_address_overrides") == []
    lan.hosts["192.168.0.43"] = saved["192.168.0.44"]
    lan.hosts["192.168.0.69"] = saved["192.168.0.70"]
    s["clock"].advance(car.BACKOFF_SECONDS[-1])
    passes(s, 1)
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.43:554{PATH}"
    assert endpoint("5c689a0c0e", s, lan, s["clock"]) == f"rtsp://192.168.0.69:554{PATH}"


def test_recovery_survives_an_appliance_restart(driveway):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    move(lan, "192.168.0.44", "192.168.0.43")
    passes(s, 2)
    car.reset_state()  # process restart: in-memory checks and holds are gone
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.43:554{PATH}"
    assert passes(s, 1)[0]["dc7a226120"] == "healthy"


def test_recovery_works_without_internet(driveway, monkeypatch):
    s = driveway
    lan = s["lan"]

    def no_internet(*args, **kwargs):
        raise OSError("network unreachable")
    monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda *a, **k: None)
    monkeypatch.setattr(socket, "create_connection", no_internet)
    monkeypatch.setattr("urllib.request.urlopen", no_internet)
    assert edge_camera_sync.sync_provisioned_cameras()["status"] == "unreachable"
    passes(s, 1)
    move(lan, "192.168.0.44", "192.168.0.43")
    passes(s, 2)
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.43:554{PATH}"


# -------------------------------------------------------------- cloud sync

def test_stale_cloud_sync_cannot_undo_a_verified_recovery(driveway, monkeypatch):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    move(lan, "192.168.0.44", "192.168.0.43")
    passes(s, 2)
    for _ in range(3):  # the cloud keeps sending the old address every minute
        sync_from_cloud(monkeypatch, s["cloud"])
    assert camera_row(s["db_path"], "dc7a226120")["onvif_endpoint"] == f"rtsp://192.168.0.44:554{PATH}"
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.43:554{PATH}"
    assert len(rows(s["db_path"], "SELECT * FROM camera_address_overrides")) == 1


def test_an_administrator_change_in_the_cloud_supersedes_the_override(driveway, monkeypatch):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    move(lan, "192.168.0.44", "192.168.0.43")
    passes(s, 2)
    changed = [dict(s["cloud"][0], onvif_endpoint=f"rtsp://192.168.0.143:554{PATH}"), s["cloud"][1]]
    sync_from_cloud(monkeypatch, changed)
    lan.hosts["192.168.0.143"] = lan.hosts.pop("192.168.0.43")
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.143:554{PATH}"
    assert rows(s["db_path"], "SELECT * FROM camera_address_overrides") == []
    assert ("override_superseded", {"reason": "cloud_endpoint_changed", "recovered_host": "192.168.0.43", "cloud_host": "192.168.0.143"}) in events(s["db_path"], "dc7a226120")


def test_a_reprovisioned_camera_identity_drops_the_override(driveway, monkeypatch):
    s = driveway
    lan = s["lan"]
    passes(s, 1)
    move(lan, "192.168.0.44", "192.168.0.43")
    passes(s, 2)
    with connection() as db:
        db.execute("UPDATE cameras SET device_key=? WHERE id=?", (OTHER, "dc7a226120"))
    lan.hosts["192.168.0.44"] = Device(OTHER, None, hardware="X")
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.44:554{PATH}"
    assert rows(s["db_path"], "SELECT * FROM camera_address_overrides") == []


# ------------------------------------------------------ scope and safety rails

def test_a_camera_streaming_without_ws_discovery_is_never_searched(driveway):
    s = driveway
    lan = s["lan"]
    lan.hosts["192.168.0.44"].onvif = False
    s["streaming"].add(2)
    passes(s, 10)
    assert lan.sweeps == 0
    assert endpoint("dc7a226120", s, lan, s["clock"]) == f"rtsp://192.168.0.44:554{PATH}"


def test_non_onvif_device_keys_are_left_exactly_as_today(db_path, monkeypatch):
    seed_credentials("mac-0123456789abcdef0123456789abcdef", "admin", "pw")
    sync_from_cloud(monkeypatch, [cloud_camera("cam-mac", 4, "Garage", "mac-0123456789abcdef0123456789abcdef", "192.168.0.60")])
    calls = []
    result = car.stream_endpoint("cam-mac", "mac-0123456789abcdef0123456789abcdef", f"rtsp://192.168.0.60:554{PATH}", 4,
                                 probe=lambda ip, t: calls.append(ip))
    assert result == f"rtsp://192.168.0.60:554{PATH}" and calls == []
    with connection() as db:
        assert car.provisioned_cameras(db) == []


def test_disabled_flag_turns_everything_off(driveway, monkeypatch):
    s = driveway
    monkeypatch.setattr(car, "ENABLED", False)
    s["lan"].hosts["192.168.0.44"] = Device(OTHER, None)
    assert endpoint("dc7a226120", s, s["lan"], s["clock"]) == f"rtsp://192.168.0.44:554{PATH}"


def test_search_stays_on_private_networks(monkeypatch):
    monkeypatch.delenv("ANYAICAM_CAMERA_RECOVERY_NETWORKS", raising=False)
    assert [str(n) for n in car.search_networks("192.168.0.44")] == ["192.168.0.0/24"]
    assert car.search_networks("8.8.8.8") == []
    monkeypatch.setenv("ANYAICAM_CAMERA_RECOVERY_NETWORKS", "10.1.2.0/24, 8.8.8.0/24, junk")
    assert [str(n) for n in car.search_networks("192.168.0.44")] == ["10.1.2.0/24"]


def test_events_never_contain_credentials(driveway):
    s = driveway
    passes(s, 1)
    move(s["lan"], "192.168.0.44", "192.168.0.43")
    s["lan"].hosts["192.168.0.43"].video = False
    passes(s, 2)
    s["lan"].hosts["192.168.0.43"].video = True
    s["clock"].advance(car.BACKOFF_SECONDS[1])
    passes(s, 1)
    text = json.dumps(rows(s["db_path"], "SELECT * FROM camera_address_recovery_events"))
    assert "pw-right" not in text and "pw-left" not in text and "admin" not in text


def test_talk_modules_use_the_recovered_address_and_respect_the_hold(driveway, monkeypatch):
    import talk_audio_relay_client
    import talk_down_discovery
    s = driveway
    lan = s["lan"]
    monkeypatch.setattr(car, "default_probe", lan.probe)
    passes(s, 1)
    move(lan, "192.168.0.44", "192.168.0.43")
    passes(s, 2)
    assert talk_down_discovery._provisioned_camera_credentials(2) == ("192.168.0.43", "admin", "pw-right")
    assert talk_audio_relay_client._provisioned_camera_transport_credentials(2)[0] == "192.168.0.43"
    lan.hosts["192.168.0.43"] = Device(OTHER, None, hardware="X")
    car.reset_state()
    assert talk_down_discovery._provisioned_camera_credentials(2) is None


# ------------------------------------------- recording path never degrades
# (Red review 2026-10-15: _provisioned_camera_stream is the recording path;
# a failing or slow recovery must keep the configured address.)

def test_a_raising_recovery_keeps_the_configured_stream_url(driveway, monkeypatch):
    import main

    def broken(*args, **kwargs):
        raise RuntimeError("recovery bug")
    monkeypatch.setattr(car, "stream_endpoint", broken)
    stream = main._provisioned_camera_stream(2)
    assert stream["rtsp_url"] == f"rtsp://192.168.0.44:554{PATH}"
    assert (stream["username"], stream["password"]) == ("admin", "pw-right")


def test_an_os_error_inside_recovery_is_not_mistaken_for_a_hold(driveway, monkeypatch):
    import main

    def socket_trouble(*args, **kwargs):
        raise OSError("socket trouble")
    monkeypatch.setattr(car, "stream_endpoint", socket_trouble)
    assert main._provisioned_camera_stream(2)["rtsp_url"] == f"rtsp://192.168.0.44:554{PATH}"


def test_a_broken_recovery_database_keeps_the_configured_address(driveway, monkeypatch):
    def no_db():
        raise RuntimeError("database locked")
    monkeypatch.setattr(car, "_connection", no_db)
    lan = driveway["lan"]
    assert car.stream_endpoint("dc7a226120", RIGHT, f"rtsp://192.168.0.44:554{PATH}", 2, probe=lan.probe) == f"rtsp://192.168.0.44:554{PATH}"


def test_a_raising_probe_keeps_the_configured_address(driveway):
    def probe(ip, timeout):
        raise OSError("no route")
    assert car.stream_endpoint("dc7a226120", RIGHT, f"rtsp://192.168.0.44:554{PATH}", 2, probe=probe) == f"rtsp://192.168.0.44:554{PATH}"


def test_an_unanswered_address_costs_at_most_one_short_probe(driveway):
    """The real default probe against a silent address: bounded by
    GATE_PROBE_TIMEOUT_SECONDS, then cached, so the supervisor's 10 s retry
    loop does not pay it again for CHECK_FRESH_SECONDS. No sweep ever runs
    inside the stream call."""
    import time as _time
    sweeps = []
    original = car.default_sweep
    car.default_sweep = lambda *a, **k: sweeps.append(1) or []
    try:
        url = f"rtsp://192.0.2.10:554{PATH}"  # TEST-NET-1: never answers
        started = _time.monotonic()
        assert car.stream_endpoint("dc7a226120", RIGHT, url, 2) == url
        first = _time.monotonic() - started
        started = _time.monotonic()
        assert car.stream_endpoint("dc7a226120", RIGHT, url, 2) == url
        second = _time.monotonic() - started
    finally:
        car.default_sweep = original
    assert first <= car.GATE_PROBE_TIMEOUT_SECONDS + 0.3
    assert second < 0.1
    assert sweeps == []


def test_a_worker_pass_failure_on_one_camera_does_not_stop_the_others(driveway, monkeypatch):
    s = driveway
    passes(s, 1)
    move(s["lan"], "192.168.0.70", "192.168.0.69")
    original = car._attempt_recovery

    def flaky(db, camera, *args, **kwargs):
        if camera["id"] == "dc7a226120":
            raise RuntimeError("boom")
        return original(db, camera, *args, **kwargs)
    monkeypatch.setattr(car, "_attempt_recovery", flaky)
    move(s["lan"], "192.168.0.44", "192.168.0.43")
    outcomes = passes(s, 2)
    assert outcomes[1]["dc7a226120"] == "error"
    assert outcomes[1]["5c689a0c0e"] == "recovered"


def test_a_failing_recovery_never_disables_talk(driveway, monkeypatch):
    import talk_audio_relay_client
    import talk_down_discovery

    def broken(*args, **kwargs):
        raise OSError("socket trouble")
    monkeypatch.setattr(car, "stream_endpoint", broken)
    assert talk_down_discovery._provisioned_camera_credentials(2) == ("192.168.0.44", "admin", "pw-right")
    assert talk_audio_relay_client._provisioned_camera_transport_credentials(2)[0] == "192.168.0.44"
