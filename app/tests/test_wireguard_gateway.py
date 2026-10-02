"""WireGuard gateway (Phase B, app/wireguard_gateway/) coverage: the
peer-reconciliation diff logic, the proxy's plain-HTTP forwarding, and
the real-provider's exact `wg` CLI argv shape -- all against
MockWireGuardInterfaceProvider or a fake runner/loopback server, never
a real interface. No test in this file starts a real WireGuard
interface or calls `wg` for real.
"""

import http.server
import threading
from datetime import datetime

import pytest

from database_backend import override_target
from partner_db import connection, initialize_database, password_hash
from wireguard_gateway.gateway_service import _active_peer_rows, reconcile_once
from wireguard_gateway.interface_provider import (
    GatewayPeer,
    MockWireGuardInterfaceProvider,
    SystemWireGuardInterfaceProvider,
)
from wireguard_gateway.proxy import GatewayProxyError, forward
from wireguard_gateway.reconciler import desired_peers_from_rows, reconcile
from wireguard_remote import enroll_peer, revoke_peer

# --------------------------------------------------------------- reconciler


def test_reconcile_adds_a_desired_peer_missing_from_the_interface():
    provider = MockWireGuardInterfaceProvider()
    result = reconcile({"pub-a": "10.70.0.2/32"}, provider)
    assert result.added == ["pub-a"]
    assert result.removed == []
    assert provider.current_peers() == {"pub-a": "10.70.0.2/32"}


def test_reconcile_removes_a_live_peer_no_longer_desired():
    provider = MockWireGuardInterfaceProvider()
    provider.seed("pub-stale", "10.70.0.3/32")
    result = reconcile({}, provider)
    assert result.removed == ["pub-stale"]
    assert provider.current_peers() == {}


def test_reconcile_is_a_no_op_when_already_correct():
    provider = MockWireGuardInterfaceProvider()
    provider.seed("pub-a", "10.70.0.2/32")
    result = reconcile({"pub-a": "10.70.0.2/32"}, provider)
    assert result.added == []
    assert result.removed == []
    assert len(provider.add_calls) == 0
    assert len(provider.remove_calls) == 0


def test_reconcile_fixes_a_peer_present_with_the_wrong_allowed_ip():
    """A peer whose live allowed_ip doesn't match the database's own
    tunnel_address (should never happen in practice -- the tunnel
    address assignment is immutable per row -- but if it ever did, the
    live interface must be corrected, not left stale)."""
    provider = MockWireGuardInterfaceProvider()
    provider.seed("pub-a", "10.70.0.99/32")
    result = reconcile({"pub-a": "10.70.0.2/32"}, provider)
    assert result.added == ["pub-a"]
    assert provider.current_peers() == {"pub-a": "10.70.0.2/32"}


def test_desired_peers_from_rows_scopes_each_row_to_its_own_slash_32():
    rows = [
        {"public_key": "pub-a", "tunnel_address": "10.70.0.2"},
        {"public_key": "pub-b", "tunnel_address": "10.70.0.3"},
    ]
    assert desired_peers_from_rows(rows) == {"pub-a": "10.70.0.2/32", "pub-b": "10.70.0.3/32"}


# --------------------------------------------------------- gateway_service (DB-backed)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_wireguard_gateway.db"


def _seed(db_path):
    now = datetime.now().isoformat()
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active',?)", (now,))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main',?)", (now,))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))


def test_reconcile_once_adds_a_real_enrolled_peer_from_the_database(db_path):
    _seed(db_path)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            peer = enroll_peer(db, appliance_id="appl-a", customer_id="cust-a", public_key="p" * 43 + "=", now=datetime.now())
        provider = MockWireGuardInterfaceProvider()
        reconcile_once(provider)
    assert provider.current_peers() == {peer["public_key"]: f"{peer['tunnel_address']}/32"}


def test_reconcile_once_removes_a_revoked_peer_on_the_next_pass(db_path):
    _seed(db_path)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            peer = enroll_peer(db, appliance_id="appl-a", customer_id="cust-a", public_key="q" * 43 + "=", now=datetime.now())
        provider = MockWireGuardInterfaceProvider()
        reconcile_once(provider)  # first pass: added
        with connection() as db:
            revoke_peer(db, peer_id=peer["id"], reason="test", now=datetime.now())
        reconcile_once(provider)  # second pass: must now be removed
    assert provider.current_peers() == {}


def test_active_peer_rows_excludes_revoked_rows(db_path):
    _seed(db_path)
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            live = enroll_peer(db, appliance_id="appl-a", customer_id="cust-a", public_key="r" * 43 + "=", now=datetime.now())
            dead = enroll_peer(db, appliance_id="appl-a", customer_id="cust-a", public_key="s" * 43 + "=", now=datetime.now())
            revoke_peer(db, peer_id=dead["id"], reason="test", now=datetime.now())
            rows = _active_peer_rows(db)
    assert {row["public_key"] for row in rows} == {live["public_key"]}


def test_run_forever_never_raises_when_reconciliation_fails(monkeypatch):
    """A DB/provider error on one cycle must never crash the gateway
    process -- it logs and retries next cycle, matching every other
    long-running worker's own resilience convention in this codebase."""
    from wireguard_gateway import gateway_service

    def _boom(_provider):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(gateway_service, "reconcile_once", _boom)
    slept = []
    gateway_service.run_forever(MockWireGuardInterfaceProvider(), interval=0, sleep=slept.append, stop_after=2)
    assert len(slept) == 1  # slept between the 2 iterations, not after the last one


# --------------------------------------------------------------- proxy


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/boom":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"appliance-side error")
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"hello from appliance")

    def log_message(self, *args):
        pass  # keep test output quiet


@pytest.fixture()
def loopback_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)


def test_forward_reaches_a_real_loopback_server_and_returns_its_body(loopback_server):
    port = loopback_server.server_address[1]
    status, body = forward("127.0.0.1", port, "/whep")
    assert status == 200
    assert body == b"hello from appliance"


def test_forward_passes_through_an_appliance_side_http_error_without_raising(loopback_server):
    port = loopback_server.server_address[1]
    status, body = forward("127.0.0.1", port, "/boom")
    assert status == 503
    assert body == b"appliance-side error"


def test_forward_raises_gateway_proxy_error_when_unreachable():
    with pytest.raises(GatewayProxyError):
        forward("127.0.0.1", 1, "/whep", timeout=1)  # port 1: nothing listening


def test_forward_rejects_a_path_missing_its_leading_slash():
    with pytest.raises(ValueError):
        forward("127.0.0.1", 8080, "whep")


# --------------------------------------------------- SystemWireGuardInterfaceProvider argv shape


class _FakeRunner:
    """Records every invocation; never a real subprocess. Mirrors how
    test_rdm4_privileged_actions.py proves DISPATCH argv shape with a
    mocked subprocess.run, applied here to the real-provider's own
    fixed-shape `wg` CLI calls."""

    def __init__(self, dump_stdout=""):
        self.calls = []
        self.dump_stdout = dump_stdout

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))

        class _Result:
            pass

        result = _Result()
        result.stdout = self.dump_stdout
        return result


def test_system_provider_add_peer_uses_the_exact_expected_wg_set_argv():
    runner = _FakeRunner()
    provider = SystemWireGuardInterfaceProvider(interface="wg0", runner=runner)
    provider.add_peer(GatewayPeer(public_key="pub-a", allowed_ip="10.70.0.2/32"))
    assert runner.calls[0][0] == ["wg", "set", "wg0", "peer", "pub-a", "allowed-ips", "10.70.0.2/32"]
    assert runner.calls[0][1]["check"] is True


def test_system_provider_remove_peer_uses_the_exact_expected_wg_set_argv():
    runner = _FakeRunner()
    provider = SystemWireGuardInterfaceProvider(interface="wg0", runner=runner)
    provider.remove_peer("pub-a")
    assert runner.calls[0][0] == ["wg", "set", "wg0", "peer", "pub-a", "remove"]


def test_system_provider_current_peers_parses_a_real_wg_show_dump_shape():
    # Real `wg show <iface> dump` output shape: first line is the
    # interface's own row (private-key, public-key, listen-port,
    # fwmark); each subsequent line is one peer (public-key, preshared-
    # key, endpoint, allowed-ips, latest-handshake, rx, tx, keepalive).
    dump = (
        "iface-priv\tiface-pub\t51820\toff\n"
        "peer-a\t(none)\t203.0.113.5:51820\t10.70.0.2/32\t1700000000\t100\t200\t25\n"
        "peer-b\t(none)\t(none)\t10.70.0.3/32\t0\t0\t0\toff\n"
    )
    runner = _FakeRunner(dump_stdout=dump)
    provider = SystemWireGuardInterfaceProvider(interface="wg0", runner=runner)
    assert provider.current_peers() == {"peer-a": "10.70.0.2/32", "peer-b": "10.70.0.3/32"}
    assert runner.calls[0][0] == ["wg", "show", "wg0", "dump"]


def test_system_provider_current_peers_ignores_the_interface_row_itself():
    dump = "iface-priv\tiface-pub\t51820\toff\n"  # no peers at all
    runner = _FakeRunner(dump_stdout=dump)
    provider = SystemWireGuardInterfaceProvider(interface="wg0", runner=runner)
    assert provider.current_peers() == {}


# --------------------------------------------------------- provider selection (env-gated)


def test_provider_from_env_defaults_to_system_when_unset(monkeypatch):
    """Unset ANYAICAM_WIREGUARD_GATEWAY_PROVIDER must keep today's real
    default -- no existing/future deploy that doesn't explicitly opt into
    the mock provider ever silently changes behavior."""
    monkeypatch.delenv("ANYAICAM_WIREGUARD_GATEWAY_PROVIDER", raising=False)
    from importlib import reload

    import wireguard_gateway.gateway_service as gateway_service_module
    reload(gateway_service_module)
    try:
        assert isinstance(gateway_service_module._provider_from_env(), SystemWireGuardInterfaceProvider)
    finally:
        reload(gateway_service_module)


def test_provider_from_env_any_other_value_is_also_system(monkeypatch):
    """Only the exact literal "mock" (case-insensitive) selects the mock
    provider -- a typo or an unrecognized value fails safe to the real
    provider's own already-inert-until-deployed default, never silently
    to the mock (which would make a real deploy silently do nothing)."""
    monkeypatch.setenv("ANYAICAM_WIREGUARD_GATEWAY_PROVIDER", "systm")
    from importlib import reload

    import wireguard_gateway.gateway_service as gateway_service_module
    reload(gateway_service_module)
    try:
        assert isinstance(gateway_service_module._provider_from_env(), SystemWireGuardInterfaceProvider)
    finally:
        reload(gateway_service_module)


def test_provider_from_env_mock_selects_mock_provider(monkeypatch):
    monkeypatch.setenv("ANYAICAM_WIREGUARD_GATEWAY_PROVIDER", "mock")
    from importlib import reload

    import wireguard_gateway.gateway_service as gateway_service_module
    reload(gateway_service_module)
    try:
        assert isinstance(gateway_service_module._provider_from_env(), MockWireGuardInterfaceProvider)
    finally:
        reload(gateway_service_module)


def test_provider_from_env_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("ANYAICAM_WIREGUARD_GATEWAY_PROVIDER", "MOCK")
    from importlib import reload

    import wireguard_gateway.gateway_service as gateway_service_module
    reload(gateway_service_module)
    try:
        assert isinstance(gateway_service_module._provider_from_env(), MockWireGuardInterfaceProvider)
    finally:
        reload(gateway_service_module)


def test_run_forever_with_no_explicit_provider_uses_env_selection(monkeypatch):
    """run_forever(provider=None) -- its own documented default path --
    must go through the same env-gated selection, not silently
    hardcode SystemWireGuardInterfaceProvider(), so the mock mode this
    file's own docstring promises ("every test passes an explicit
    MockWireGuardInterfaceProvider") is also true of a real deploy that
    sets the env var and calls run_forever() with no arguments, exactly
    as docker-compose.staging.example.yml's own command line does."""
    monkeypatch.setenv("ANYAICAM_WIREGUARD_GATEWAY_PROVIDER", "mock")
    from importlib import reload

    import wireguard_gateway.gateway_service as gateway_service_module
    reload(gateway_service_module)
    try:
        seen: list = []
        gateway_service_module.reconcile_once = lambda provider: seen.append(provider)
        gateway_service_module.run_forever(stop_after=1, sleep=lambda _: None)
        assert len(seen) == 1
        assert isinstance(seen[0], MockWireGuardInterfaceProvider)
    finally:
        reload(gateway_service_module)
