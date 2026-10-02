"""Host LAN addresses for WebRTC ICE (2026-09-25).

The VMS (and MediaMTX inside it) runs in a Docker bridge container, so it
can only see container addresses. A browser on the same LAN as the
appliance therefore got no reachable ICE candidate and fell back to the
cloud relay. This agent runs on the HOST, so it lists the host's real
LAN-reachable IPv4 addresses and writes them to
<state_dir>/lan_addresses.json, which the VMS mounts read-only and hands to
MediaMTX as webrtcAdditionalHosts.

Only private addresses on real network interfaces are ever written:
RFC1918 on LAN interfaces, plus the Tailscale (100.64.0.0/10) address on a
tailscale* interface when ANYAICAM_P2P_ADVERTISE_TAILSCALE=true. Loopback,
link-local, public addresses, and every Docker/bridge/veth/virtual/VPN
tunnel interface (including the WireGuard wg* gateway tunnel) are excluded.
Nothing is hardcoded: the list is recomputed every agent cycle and the file
is rewritten only when it changes.
"""
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import time
from pathlib import Path

EXCLUDED_INTERFACE_PREFIXES = (
    "lo", "docker", "br-", "veth", "virbr", "vnet", "cni", "flannel", "cali", "weave", "kube",
    "lxc", "lxd", "podman", "wg", "zt", "tun", "tap", "vxlan", "genev",
)
TAILSCALE_PREFIX = "tailscale"
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
# Explicit RFC1918 ranges (the same ones anyaicam-webrtc-firewall allows) --
# never ipaddress.is_private, which also covers documentation/benchmark
# ranges such as 203.0.113.0/24 that can be real routed addresses.
LAN_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
MAX_ADDRESSES = 8
ADVERTISE_TAILSCALE = os.environ.get("ANYAICAM_P2P_ADVERTISE_TAILSCALE", "false").strip().lower() == "true"


def select_addresses(interfaces: list[dict], *, include_tailscale: bool = ADVERTISE_TAILSCALE) -> list[str]:
    """Pure filter over `ip -j -4 addr show` output."""
    selected = []
    for interface in interfaces or []:
        name = str(interface.get("ifname") or "")
        if "UP" not in (interface.get("flags") or []) and interface.get("operstate") not in ("UP", "UNKNOWN"):
            continue
        is_tailscale = name.startswith(TAILSCALE_PREFIX)
        if not is_tailscale and name.startswith(EXCLUDED_INTERFACE_PREFIXES):
            continue
        for info in interface.get("addr_info") or []:
            if info.get("family") != "inet":
                continue
            try:
                address = ipaddress.ip_address(str(info.get("local")))
            except ValueError:
                continue
            if is_tailscale:
                if include_tailscale and address in TAILSCALE_NET:
                    selected.append(str(address))
                continue
            if any(address in net for net in LAN_NETS):
                selected.append(str(address))
    unique = list(dict.fromkeys(selected))
    return unique[:MAX_ADDRESSES]


def read_interfaces() -> list[dict]:
    try:
        output = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=5, check=False).stdout
        return json.loads(output or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def publish(state_dir: str | Path, *, interfaces: list[dict] | None = None, now: float | None = None) -> list[str]:
    """Writes <state_dir>/lan_addresses.json when the address set changes
    (atomically, world-readable for the read-only VMS mount). Returns the
    current list."""
    addresses = select_addresses(read_interfaces() if interfaces is None else interfaces)
    path = Path(state_dir) / "lan_addresses.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8")).get("addresses")
    except (OSError, ValueError, AttributeError):
        current = None
    if current != addresses:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"addresses": addresses, "updated_at": int(now if now is not None else time.time())}), encoding="utf-8")
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    return addresses
