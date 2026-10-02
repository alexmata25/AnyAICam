#!/usr/bin/env python3
"""Post-cutover container-sprawl safety check for staging (and production)
portal deploys.

Written 2026-09-17 after a real incident: the staging host has only 3.7GB
RAM, and this project's own rollback convention -- rename an old container
instead of deleting it, so a bad deploy can always be reversed in one step
-- had been silently misapplied, across many consecutive deploys that same
day, as "rename AND leave running" instead of "rename, then stop." 8
redundant portal-* containers (~300MB each, ~2.4GB combined) ended up
running simultaneously with no functional purpose -- none of them serving
live traffic, all of them just idling -- and the real Linux OOM killer
eventually killed the actual live container (a real SIGKILL, exit 137) to
reclaim memory, causing a genuine customer- and appliance-facing 502
outage ("no upstreams available"). The container was restored, but a
second and third real OOM kill happened later the same day for the exact
same underlying reason (old rollback containers left running), including
one after a restart interrupted the original cleanup -- proof that a
verbal "stop, don't leave running" standing practice, without an
automated check, does not reliably survive an interruption. Nothing in
the existing deploy process checked, after a cutover, whether more than
the intended handful of portal containers were still running.

This script is the fix: run it after every cutover (and any time
suspected). It reads the real `docker ps` list on the host and refuses
(CONTAINER_SPRAWL_BLOCKED, exit 1) if more than `--max-rollback` non-live
portal-* containers are currently RUNNING. Stopped/exited containers are
always fine -- this project's own convention is rename-not-delete, and a
stopped container costs no RAM. The default max is 1: the single
immediate one-step-back rollback a fresh cutover is allowed to leave
running for the transition window, matching this project's own documented
standing practice. Anything beyond that means old rollbacks are being
left running instead of stopped, exactly the pattern that caused the
2026-09-17 outage.

Run this ON THE HOST the containers run on (it shells out to `docker
ps`), the same way verify_cutover_safety.py already does -- no AWS/
network dependency of its own.

Usage:
    python3 check_container_sprawl.py --live <container-name> [--prefix portal] [--max-rollback 1]

Exit 0 and "CONTAINER_SPRAWL_OK" means fine. Exit 1 and
"CONTAINER_SPRAWL_BLOCKED" means stop the extra containers
(`docker stop <name>` -- never delete, this project's rollback containers
stay available stopped) before considering the deploy done.
"""

import argparse
import subprocess
import sys


def _running_containers(prefix: str) -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    return [name for name in result.stdout.splitlines() if name.startswith(prefix)]


def check(running: list[str], live_container: str, max_rollback: int = 1) -> int:
    if live_container not in running:
        print(
            f"CONTAINER_SPRAWL_BLOCKED: designated live container '{live_container}' "
            f"is not in the running list {running} -- nothing is actually serving traffic."
        )
        return 1

    extra = [name for name in running if name != live_container]
    if len(extra) > max_rollback:
        print(
            f"CONTAINER_SPRAWL_BLOCKED: {len(extra)} non-live portal container(s) are "
            f"RUNNING, more than the allowed {max_rollback}:"
        )
        for name in extra:
            print(f"  - {name}")
        print(
            "Stop the extra ones (docker stop <name> -- never delete) before considering "
            "this deploy done. This is exactly the accumulation pattern that caused the "
            "real 2026-09-17 staging OOM outage."
        )
        return 1

    if extra:
        print(f"CONTAINER_SPRAWL_OK: live container running, plus {len(extra)} immediate rollback (within the allowed {max_rollback}): {extra}")
    else:
        print("CONTAINER_SPRAWL_OK: only the live container is running.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", required=True, help="Currently-live container name (already holding the network alias).")
    parser.add_argument("--prefix", default="portal", help="Name prefix identifying portal deploy containers (default: 'portal').")
    parser.add_argument("--max-rollback", type=int, default=1, help="Max non-live portal-prefixed containers allowed to still be RUNNING (default: 1).")
    args = parser.parse_args()
    running = _running_containers(args.prefix)
    return check(running, args.live, max_rollback=args.max_rollback)


if __name__ == "__main__":
    sys.exit(main())
