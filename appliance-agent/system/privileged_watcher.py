#!/usr/bin/env python3
"""RDM4 privileged watcher for AnyAiCam reboot / VMS-restart requests.

Runs as root, triggered by anyaicam-privileged-watcher.path whenever a
marker file appears under the agent-writable pending_actions directory
(see AgentConfig.pending_actions_dir). This script -- not the
unprivileged anyaicam-agent process -- is the only thing on this
device with the actual privilege to reboot the host or touch Docker;
its own systemd unit intentionally has NONE of anyaicam-agent.service's
NoNewPrivileges/ProtectSystem/CapabilityBoundingSet restrictions
because it needs real root -- but its INPUT is deliberately reduced to
almost nothing, to compensate: it reads ONLY a marker's `type` field
and looks it up in the hardcoded DISPATCH table below. It never reads,
constructs, or executes a command, argument, path, or container name
out of the marker's content, and never falls back to a shell. Adding a
new privileged action means adding a new line to DISPATCH and
re-reviewing this file -- it can never mean trusting new input.

--dry-run prints what would run instead of running it, and never
deletes the marker -- this is what lets tests and CI prove the
marker-to-action mapping without ever rebooting a host or touching
Docker.
"""
import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PENDING_DIR = Path('/var/lib/anyaicam/pending_actions')
GRACE_SECONDS = 10  # re-checked below: a deliberate pause before acting,
                     # not just a fast path -- see _await_grace_period().

# The ONLY actions this script can ever take, full stop. A marker whose
# `type` isn't a key here is ignored (logged, marker left in place) --
# never guessed at, never passed to a shell.
#
# restart_vms is deliberately `docker compose ... up -d`, NOT
# `docker restart anyaicam-vms` -- confirmed live on a fresh appliance
# install: `docker restart` reuses the container's already-baked-in
# environment from whenever it was last created and never re-reads
# /etc/anyaicam/vms.env, so setup_wizard.py's own ANYAICAM_CLOUD_URL
# write (the entire reason this action gets queued after activation)
# would silently never take effect. `docker compose up -d` diffs the
# resolved service config (env_file included) against the running
# container and recreates it only when something actually changed --
# confirmed live to pick up a vms.env edit within one call. --project-
# directory is explicit rather than relying on this process's inherited
# working directory, which this fixed-argv/no-shell design must never
# depend on.
#
# restart_agent: queued by setup_wizard.py's _finish_enrollment() after
# a successful first-time enrollment or re-enrollment. anyaicam-setup is
# documented to run as the unprivileged `anyaicam` system user (see
# appliance-agent/scripts/install.sh), which owns /etc/anyaicam and
# /var/lib/anyaicam but has no authority to restart a system unit and no
# interactive desktop session for polkit to prompt through -- confirmed
# live on Ryzen (2026-09-12) as a hung/failed CalledProcessError from a
# bare, unprivileged `systemctl restart`. This is the same root-owned,
# fixed-argv path restart_vms already uses, not a new privilege grant.
# Unlike restart_vms, a failure to queue or run this is never fatal to
# enrollment -- see restart_service()'s own docstring in setup_wizard.py
# for why anyaicam-agent.service does not actually need this restart to
# pick up a freshly written credential.json.
# wireguard_interface_up/down (docs/wireguard-remote-connectivity-plan.md
# Sec 5 step 3, Sec 16): the appliance-side half of WireGuard direct
# remote connectivity. `wg-quick` accepts either a bare interface name
# (looks up /etc/wireguard/<name>.conf) or a full config-file path --
# the literal path below is the SECOND form, deliberately pointed at
# config_dir/wireguard/wg0.conf (AgentConfig.wireguard_conf_file, see
# config.py) rather than the OS default /etc/wireguard/wg0.conf,
# specifically so the unprivileged anyaicam-agent process -- which
# already owns config_dir, same as agent.json/credential.json -- can
# write the config's content itself (see wireguard.py's own
# save_wg_conf()) without ever needing write access to /etc/wireguard/.
# This is the exact same shape restart_vms already established above:
# a fixed, hardcoded literal argv/path that itself reads a config file
# from a well-known location the unprivileged process wrote ahead of
# queuing the action -- the marker's own content is still never read
# for either action, only its `type`. Neither entry has been queued or
# executed against any real device by anything in this codebase yet --
# see setup_wizard.py's own ANYAICAM_WIREGUARD_ENABLED gate (unset by
# default) and this project's own standing "no real network/interface
# change on the Ryzen without separate explicit authorization" rule.
DISPATCH = {
    'reboot': ['systemctl', 'reboot'],
    'restart_vms': ['docker', 'compose', '--project-directory', '/opt/anyaicam', 'up', '-d'],
    'restart_agent': ['systemctl', 'restart', 'anyaicam-agent.service'],
    'wireguard_interface_up': ['wg-quick', 'up', '/etc/anyaicam/wireguard/wg0.conf'],
    'wireguard_interface_down': ['wg-quick', 'down', '/etc/anyaicam/wireguard/wg0.conf'],
}

log = logging.getLogger('anyaicam.privileged_watcher')


def _read_marker(path: Path):
    try:
        marker = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict): return None
    return marker


def _await_grace_period(path: Path, expected_command_id, grace_seconds: float, sleep=time.sleep):
    """Deliberate pause before acting on a reboot/restart marker -- an
    operator (or the requester itself) can cancel by deleting the
    marker file within this window. Re-reads the marker afterward:
    if it's gone, or its command_id changed (a newer request
    superseded it), treat this as cancelled and do nothing."""
    if grace_seconds > 0:
        sleep(grace_seconds)
    current = _read_marker(path)
    if current is None:
        return False  # deleted (cancelled) or became unreadable during the grace window
    return current.get('command_id') == expected_command_id


def process_marker(path: Path, dry_run: bool, grace_seconds: float = GRACE_SECONDS, sleep=time.sleep):
    """Returns the argv that was (or would be) executed, or None if the
    marker was invalid, unknown, or cancelled during its grace period."""
    marker = _read_marker(path)
    if marker is None:
        log.warning('Ignoring unreadable/malformed marker %s', path)
        return None
    command_id = marker.get('command_id')
    action_type = marker.get('type')
    # isinstance check BEFORE the DISPATCH.get() lookup below -- confirmed
    # by test: an unhashable `type` (e.g. a list, dict, or set, whether
    # malformed input or a deliberate attempt to crash the watcher) raised
    # an unhandled TypeError out of dict.get() instead of being rejected
    # the same safe way every other unknown type already is. A crashed
    # oneshot service run can leave OTHER pending markers unprocessed
    # until the next trigger and, depending on systemd's own failure
    # handling, the unit sitting in a failed state -- exactly the "unsafe
    # failure behavior" this design's docstring promises never happens.
    if not isinstance(action_type, str):
        log.warning('Ignoring marker %s with non-string type=%r', path, action_type)
        return None
    if not command_id or not isinstance(command_id, str):
        log.warning('Ignoring marker %s with missing/invalid command_id', path)
        return None
    argv = DISPATCH.get(action_type)
    if argv is None:
        log.warning('Ignoring marker %s with unknown type=%r', path, action_type)
        return None
    if not dry_run and not _await_grace_period(path, command_id, grace_seconds, sleep):
        log.info('Marker %s cancelled or superseded during grace period; taking no action', path)
        return None
    if dry_run:
        print(f'DRY RUN: would execute {argv!r} for marker type={action_type!r} command_id={command_id}')
        return argv
    log.info('Executing %r for marker type=%s command_id=%s', argv, action_type, command_id)
    subprocess.run(argv, check=False)
    try:
        path.unlink()
    except OSError:
        log.exception('Could not remove consumed marker %s', path)
    return argv


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Print the action instead of executing it; never deletes the marker.')
    parser.add_argument('--pending-dir', default=str(DEFAULT_PENDING_DIR))
    parser.add_argument('--grace-seconds', type=float, default=GRACE_SECONDS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    pending_dir = Path(args.pending_dir)
    if not pending_dir.is_dir():
        return 0
    for marker_file in sorted(pending_dir.glob('*.json')):
        process_marker(marker_file, args.dry_run, args.grace_seconds)
    return 0


if __name__ == '__main__':
    sys.exit(main())
