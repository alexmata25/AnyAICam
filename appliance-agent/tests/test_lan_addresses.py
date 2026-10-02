"""Host LAN addresses for WebRTC ICE (lan_addresses.py)."""
import json
import os
import stat

from anyaicam_agent import lan_addresses as la


def _iface(name, *addresses, up=True):
    return {"ifname": name, "flags": ["BROADCAST", "UP"] if up else ["BROADCAST"], "operstate": "UP" if up else "DOWN",
            "addr_info": [{"family": "inet", "local": a, "prefixlen": 24} for a in addresses]}


# A realistic host: wired LAN, Wi-Fi, Tailscale, the cloud WireGuard tunnel,
# Docker + compose bridges, a veth, libvirt, loopback, link-local, a public
# address and a down interface.
HOST = [
    _iface("lo", "127.0.0.1"),
    _iface("eno1", "192.168.0.228"),
    _iface("wlp3s0", "10.0.5.3"),
    _iface("tailscale0", "100.77.253.28"),
    _iface("wg0", "10.70.0.2"),
    _iface("docker0", "172.17.0.1"),
    _iface("br-b1501c4eead6", "172.19.0.1"),
    _iface("veth12ab", "172.18.0.5"),
    _iface("virbr0", "192.168.122.1"),
    _iface("enp4s0", "203.0.113.9", "169.254.10.2"),
    _iface("enp5s0", "192.168.50.10", up=False),
]


def test_only_real_lan_addresses_are_selected():
    assert la.select_addresses(HOST, include_tailscale=False) == ["192.168.0.228", "10.0.5.3"]


def test_tailscale_is_optional():
    assert la.select_addresses(HOST, include_tailscale=True) == ["192.168.0.228", "10.0.5.3", "100.77.253.28"]


def test_a_cgnat_address_on_a_non_tailscale_interface_is_never_advertised():
    assert la.select_addresses([_iface("eth0", "100.100.1.1")], include_tailscale=True) == []


def test_ipv6_and_garbage_are_ignored():
    iface = {"ifname": "eth0", "flags": ["UP"], "addr_info": [{"family": "inet6", "local": "fd00::1"}, {"family": "inet", "local": "not-an-ip"}]}
    assert la.select_addresses([iface]) == []


def test_publish_writes_only_on_change_and_is_world_readable(tmp_path):
    assert la.publish(tmp_path, interfaces=HOST, now=100) == ["192.168.0.228", "10.0.5.3"]
    path = tmp_path / "lan_addresses.json"
    first = json.loads(path.read_text())
    assert first == {"addresses": ["192.168.0.228", "10.0.5.3"], "updated_at": 100}
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
    la.publish(tmp_path, interfaces=HOST, now=200)
    assert json.loads(path.read_text())["updated_at"] == 100  # unchanged set -> not rewritten
    la.publish(tmp_path, interfaces=[_iface("eno1", "192.168.0.99")], now=300)  # DHCP moved the host
    assert json.loads(path.read_text()) == {"addresses": ["192.168.0.99"], "updated_at": 300}


def test_no_lan_interface_writes_an_empty_list(tmp_path):
    la.publish(tmp_path, interfaces=[_iface("lo", "127.0.0.1"), _iface("docker0", "172.17.0.1")], now=1)
    assert json.loads((tmp_path / "lan_addresses.json").read_text())["addresses"] == []
