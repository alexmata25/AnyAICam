import socket
import ssl
import urllib.request

import pytest

import main


def _set_test_network(monkeypatch):
    monkeypatch.setenv("ANYAICAM_INTERNAL_TEST_BUILD", "1")
    monkeypatch.setenv("ANYAICAM_DIRECT_TEST_CAMERA_NETWORK", "192.168.50.0/24")
    monkeypatch.setenv("ANYAICAM_DIRECT_TEST_CAMERA_INTERFACE_ADDRESS", "192.168.50.10")
    monkeypatch.setenv("ANYAICAM_DIRECT_TEST_CAMERA_INTERFACE_NETWORK", "192.168.50.0/24")


def test_credentialed_verification_target_is_limited_to_the_pinned_network(monkeypatch):
    _set_test_network(monkeypatch)
    assert main._v111_direct_test_source_address("192.168.50.22") == "192.168.50.10"
    with pytest.raises(ValueError, match="outside the pinned"):
        main._v111_direct_test_source_address("192.168.51.22")
    with pytest.raises(ValueError, match="outside the pinned"):
        main._v111_direct_test_source_address("8.8.8.8")


def test_source_bound_http_and_https_connections_pin_local_address():
    http_type, https_type = main._v111_source_bound_connection_types(
        "192.168.50.10", ssl.create_default_context()
    )
    http = http_type("192.168.50.22")
    https = https_type("192.168.50.22", context=ssl.create_default_context())
    try:
        assert http.source_address == ("192.168.50.10", 0)
        assert https.source_address == ("192.168.50.10", 0)
    finally:
        http.close()
        https.close()


def test_source_bound_opener_disables_environment_and_windows_system_proxies(monkeypatch):
    captured = {}
    original = urllib.request.build_opener

    def build_without_system_proxy(*handlers):
        explicit = [handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler)]
        captured["proxy_handlers"] = explicit
        return original(*handlers)

    monkeypatch.setattr(urllib.request, "getproxies", lambda: pytest.fail("system proxy lookup was attempted"))
    monkeypatch.setattr(urllib.request, "build_opener", build_without_system_proxy)
    opener = main._v111_source_bound_opener("192.168.50.10")
    assert len(captured["proxy_handlers"]) == 1
    assert captured["proxy_handlers"][0].proxies == {}
    assert not any(hasattr(handler, "proxy_open") for handler in opener.handlers)


def test_credentialed_rtsp_verification_binds_socket_to_test_interface(monkeypatch):
    calls = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def sendall(self, payload):
            self.payload = payload

        def recv(self, size):
            return b"RTSP/1.0 200 OK\r\nServer: synthetic-camera\r\n\r\n"

    def connect(address, timeout=None, source_address=None):
        calls.append((address, timeout, source_address))
        return FakeConnection()

    monkeypatch.setattr(socket, "create_connection", connect)
    result = main.v111_rtsp_verify(
        "192.168.50.22", "synthetic-user", "synthetic-password", source_address="192.168.50.10"
    )
    assert result["authenticated"] is True
    assert calls == [(('192.168.50.22', 554), 3, ("192.168.50.10", 0))]
