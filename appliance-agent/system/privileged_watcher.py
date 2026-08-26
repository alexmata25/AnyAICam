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
DISPATCH = {
    'reboot': ['systemctl', 'reboot'],
    'restart_vms': ['docker', 'restart', 'anyaicam-vms'],
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
