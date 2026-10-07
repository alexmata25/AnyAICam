"""Native Windows support for the appliance agent (2026-10-07).

On Windows the agent runs as the `AnyAiCamAgent` service (WinSW, LocalSystem)
next to the `AnyAiCamVMS` service, both installed by installer/windows. The
Linux agent's platform pieces have no Windows equivalent, so the few call
sites that need them branch here when IS_WINDOWS; on Linux nothing in this
module is ever used and every Linux path is unchanged.

  * metrics: CPU, memory and uptime from the Win32 API (no /proc).
  * discovery: this PC's private IPv4 networks and its ARP table (no `ip`
    command, no /proc/net/arp).
  * privileged actions: on Linux an unprivileged agent writes a marker and a
    separate root watcher acts on it. On Windows there is no watcher; the
    agent service itself performs a FIXED allowlist -- restart the VMS service,
    restart the agent service -- through the WinSW wrappers the installer put
    in <install root>\\service. Rebooting the PC and installing software
    updates are refused: a Windows install is the customer's own PC, and
    Windows updates arrive as a new signed setup, never through the Linux
    release channel.
"""
from __future__ import annotations

import ctypes
import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = os.name == 'nt'

# action type -> (WinSW wrapper in <install root>\service, WinSW command).
# `restart!` is WinSW's self-restart: it restarts the service from a detached
# process, so the agent can restart its own service without being killed
# half-way through the request.
SERVICE_ACTIONS = {
    'restart_vms': ('AnyAiCamVMS.exe', 'restart'),
    'restart_agent': ('AnyAiCamAgent.exe', 'restart!'),
}
UNSUPPORTED_ACTIONS = {
    # commands.execute('reboot_appliance') requests the 'reboot' action
    'reboot': 'Rebooting the computer is not supported on Windows (it is the customer\'s own PC).',
    'install_update': 'Software updates are not installed remotely on Windows; install the new AnyAiCam setup instead.',
}
UPDATES_UNSUPPORTED = UNSUPPORTED_ACTIONS['install_update']


def install_root() -> Path:
    """<install root> from the bundled interpreter's own location
    (<install root>\\runtime\\python\\python.exe) -- never from the
    environment, so nothing outside the install can redirect the actions."""
    return Path(sys.executable).resolve().parent.parent.parent


def run_privileged_action(action_type: str, *, root: Path | None = None, run=subprocess.run) -> tuple[str, dict, str]:
    """The Windows counterpart of commands._queue_privileged_action()'s
    marker + root watcher: same (status, result, error) shape."""
    if action_type in UNSUPPORTED_ACTIONS:
        return 'failed', {}, UNSUPPORTED_ACTIONS[action_type]
    if action_type not in SERVICE_ACTIONS:
        return 'failed', {}, f'{action_type} is not an action this Windows agent performs.'
    wrapper, verb = SERVICE_ACTIONS[action_type]
    executable = (root or install_root()) / 'service' / wrapper
    if not executable.is_file():
        return 'failed', {}, f'{wrapper} is not installed.'
    try:
        result = run([str(executable), verb], capture_output=True, text=True, timeout=120, check=False,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as error:
        return 'failed', {}, f'Could not run {wrapper} {verb}: {error}'
    if result.returncode != 0:
        return 'failed', {}, f'{wrapper} {verb} exited with {result.returncode}.'
    return 'completed', {'requested': True, 'action': action_type}, ''


# --------------------------------------------------------------- metrics
class _FileTime(ctypes.Structure):
    _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_uint32)]

    def value(self) -> int:
        return (self.high << 32) | self.low


class _MemoryStatus(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint32), ('memory_load', ctypes.c_uint32),
                ('total_physical', ctypes.c_uint64), ('available_physical', ctypes.c_uint64),
                ('total_page_file', ctypes.c_uint64), ('available_page_file', ctypes.c_uint64),
                ('total_virtual', ctypes.c_uint64), ('available_virtual', ctypes.c_uint64),
                ('available_extended_virtual', ctypes.c_uint64)]


def _system_times() -> tuple[int, int]:
    idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError('GetSystemTimes failed')
    # kernel time includes idle time
    return kernel.value() + user.value(), idle.value()


def cpu_percent(sample: float = .15) -> float:
    try:
        total1, idle1 = _system_times(); time.sleep(sample); total2, idle2 = _system_times()
        return round(100 * (1 - (idle2 - idle1) / max(1, total2 - total1)), 1)
    except (OSError, AttributeError, ValueError):
        return 0.0


def memory_percent() -> float:
    try:
        status = _MemoryStatus(); status.length = ctypes.sizeof(_MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0.0
        return round(100 * (1 - status.available_physical / max(1, status.total_physical)), 1)
    except (OSError, AttributeError):
        return 0.0


def uptime_seconds() -> int:
    try:
        tick = ctypes.windll.kernel32.GetTickCount64
        tick.restype = ctypes.c_uint64
        return int(tick() // 1000)
    except (OSError, AttributeError):
        return 0


# --------------------------------------------------------------- discovery
def local_ipv4_addresses() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


def private_networks(addresses: list[str] | None = None) -> list[ipaddress.IPv4Network]:
    """Each private, non-loopback, non-link-local IPv4 address of this PC as
    its /24 -- the same at-most-/24 scan scope the Linux agent uses
    (Windows reports no prefix length through the standard library)."""
    networks = []
    for value in local_ipv4_addresses() if addresses is None else addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.is_private and not address.is_loopback and not address.is_link_local:
            network = ipaddress.ip_network(f'{address}/24', strict=False)
            if network not in networks:
                networks.append(network)
    return networks


_ARP_LINE = re.compile(r'^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\s+\w+')


def parse_arp(output: str) -> dict[str, str]:
    """`arp -a` lines -> {ip: 'aa:bb:cc:dd:ee:ff'} (the Linux table's format)."""
    table = {}
    for line in (output or '').splitlines():
        match = _ARP_LINE.match(line)
        if match:
            table[match.group(1)] = match.group(2).replace('-', ':').lower()
    return table


def arp_table(run=subprocess.run) -> dict[str, str]:
    try:
        result = run(['arp', '-a'], capture_output=True, text=True, timeout=5, check=False,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_arp(result.stdout)


def service_state(name: str, run=subprocess.run) -> str:
    """'running' / 'stopped' / ... from `sc query`, for anyaicam-status."""
    try:
        output = run(['sc', 'query', name], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    match = re.search(r'STATE\s*:\s*\d+\s+(\w+)', output or '')
    return match.group(1).lower() if match else 'not installed'
