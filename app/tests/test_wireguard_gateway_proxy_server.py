"""wireguard_gateway/proxy_server.py (2026-09-18): the internal, docker-
network-only HTTP server the portal container calls to fetch appliance
bytes over the tunnel. Tests run a real ThreadingHTTPServer bound to
127.0.0.1 on an ephemeral port (never 0.0.0.0/a fixed port, so this test
suite can never collide with a real gateway process) and drive it with
real HTTP requests -- proxy.forward() itself is monkeypatched so no real
network/tunnel is involved.
"""

import urllib.error
import urllib.request

import pytest

from wireguard_gateway import proxy_server


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("ANYAICAM_WIREGUARD_TUNNEL_CIDR", "10.70.0.0/16")
    monkeypatch.setenv("ANYAICAM_WIREGUARD_GATEWAY_PROXY_PORTS", "8000")
    instance = proxy_server.start(bind_host="127.0.0.1", bind_port=0)
    port = instance.server_address[1]
    yield f"http://127.0.0.1:{port}"
    instance.shutdown()


def _get(base_url, path):
    request = urllib.request.Request(f"{base_url}{path}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_unknown_path_is_404(server):
    status, _ = _get(server, "/not-the-right-path")
    assert status == 404


def test_tunnel_address_outside_cidr_rejected(server, monkeypatch):
    monkeypatch.setattr(proxy_server, "forward", lambda *a, **k: (200, b"should never be called"))
    status, body = _get(server, "/appliance-fetch?tunnel=8.8.8.8&port=8000&path=/health")
    assert status == 400
    assert b"tunnel address" in body


def test_port_not_in_allowlist_rejected(server, monkeypatch):
    monkeypatch.setattr(proxy_server, "forward", lambda *a, **k: (200, b"should never be called"))
    status, body = _get(server, "/appliance-fetch?tunnel=10.70.0.2&port=22&path=/health")
    assert status == 400
    assert b"allow-list" in body


def test_path_must_start_with_slash(server, monkeypatch):
    monkeypatch.setattr(proxy_server, "forward", lambda *a, **k: (200, b"should never be called"))
    status, body = _get(server, "/appliance-fetch?tunnel=10.70.0.2&port=8000&path=etc/passwd")
    assert status == 400
    assert b"path must start with" in body


def test_successful_forward_returns_the_appliance_bytes(server, monkeypatch):
    seen = {}

    def _fake_forward(tunnel_address, port, path, **kwargs):
        seen["args"] = (tunnel_address, port, path)
        return 200, b"real-video-bytes"

    monkeypatch.setattr(proxy_server, "forward", _fake_forward)
    status, body = _get(server, "/appliance-fetch?tunnel=10.70.0.2&port=8000&path=/static/hls/camera1.m3u8")
    assert status == 200
    assert body == b"real-video-bytes"
    assert seen["args"] == ("10.70.0.2", 8000, "/static/hls/camera1.m3u8")


def test_gateway_proxy_error_becomes_502(server, monkeypatch):
    def _raise(*a, **k):
        raise proxy_server.GatewayProxyError("simulated: tunnel down")

    monkeypatch.setattr(proxy_server, "forward", _raise)
    status, body = _get(server, "/appliance-fetch?tunnel=10.70.0.2&port=8000&path=/static/hls/camera1.m3u8")
    assert status == 502
    assert b"tunnel down" in body


def test_tunnel_address_allowed_helper():
    from ipaddress import ip_network
    cidr = ip_network("10.70.0.0/16")
    assert proxy_server.tunnel_address_allowed("10.70.0.2", cidr)
    assert not proxy_server.tunnel_address_allowed("10.71.0.2", cidr)
    assert not proxy_server.tunnel_address_allowed("not-an-ip", cidr)
