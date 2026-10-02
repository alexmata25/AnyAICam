#!/usr/bin/env python3
"""Pre-cutover safety check for staging (and production) portal deploys.

Written 2026-09-17 after a real incident: a deploy mounted the database
volume at the wrong container-internal path (/app/recordings/db and
/app/data-config instead of the real /app/data and
/opt/anyaicam/data/config). The new container started cleanly, passed
its own health check (/version, /health both return 200 regardless of
what's in the database), and was cut over to live traffic -- where it
silently ran against an empty, unpersisted SQLite file. Every customer
login and every real appliance call (heartbeat, config sync, live relay)
failed closed with 401/403 for roughly 45 minutes before a human noticed
and reported it. Nothing in the existing deploy process would have
caught this automatically: build succeeded, container started, health
endpoints returned 200 -- the only thing wrong was which data the
container could actually see, and that was never checked.

This script is the fix: it never assumes what "enough data" looks like.
Instead it reads the same handful of core tables from BOTH the
currently-live container and the new candidate, and refuses cutover if
the candidate's counts are lower than the live container's -- exactly,
and only, the failure signature of this incident. It does not replace
the existing health check; it is an additional, mandatory gate run
AFTER the candidate is up and healthy but BEFORE `docker network
connect --alias portal --alias vms` (or the production equivalent) is
run against it.

Run this ON THE HOST the containers run on (it shells out to `docker
exec`), the same way every other deploy step in this project already
does via SSM RunShellScript -- this script has no AWS/network
dependency of its own, just Docker CLI access to both containers.

Usage:
    python3 verify_cutover_safety.py --live <container-name> --candidate <container-name> [--force]

Exit code 0 and "CUTOVER_OK" means proceed. Exit code 1 and
"CUTOVER_BLOCKED" means stop -- do not connect the candidate to the
live network alias. --force is for the one legitimate case this
comparison can't handle (the very first deploy of a brand-new
environment, where there is no live container to compare against yet);
it is never appropriate for a routine redeploy.
"""

import argparse
import json
import subprocess
import sys

# Deliberately the same small set of tables the incident's own symptoms
# pointed at (customer login, appliance auth, camera data) -- not every
# table in the schema. Kept short and fast: this runs on every single
# deploy, not just when something is suspected wrong.
CHECK_TABLES = ("partner_users", "appliances", "customers", "cameras")

_PROBE_SCRIPT = """
import sys, json
sys.path.insert(0, "/app")
from partner_db import connection
out = {}
for table in %r:
    try:
        with connection() as db:
            out[table] = db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
    except Exception:
        out[table] = None
print(json.dumps(out))
""" % (CHECK_TABLES,)


def _table_counts(container: str) -> dict | None:
    try:
        result = subprocess.run(
            ["docker", "exec", container, "python3", "-c", _PROBE_SCRIPT],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as error:
        print(f"CUTOVER_BLOCKED: could not read table counts from container '{container}': {error}")
        return None
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        print(f"CUTOVER_BLOCKED: unreadable output from container '{container}': {result.stdout!r}")
        return None


def check(live_container: str, candidate_container: str, force: bool = False) -> int:
    candidate_counts = _table_counts(candidate_container)
    if candidate_counts is None:
        print("CUTOVER_BLOCKED: candidate container's database is unreadable.")
        return 1

    live_counts = _table_counts(live_container)
    if live_counts is None:
        if force:
            print("CUTOVER_OK (forced): live container unreadable, proceeding without comparison. "
                  "This should only ever happen on a brand-new environment's very first deploy.")
            return 0
        print("CUTOVER_BLOCKED: live container's database is unreadable and --force was not given. "
              "If this is genuinely the first deploy of a new environment, re-run with --force.")
        return 1

    print(f"live ({live_container}):      {json.dumps(live_counts)}")
    print(f"candidate ({candidate_container}): {json.dumps(candidate_counts)}")

    regressions = []
    for table in CHECK_TABLES:
        live_n = live_counts.get(table)
        candidate_n = candidate_counts.get(table)
        if live_n is None or candidate_n is None:
            regressions.append(f"{table}: unreadable (live={live_n}, candidate={candidate_n})")
        elif candidate_n < live_n:
            regressions.append(f"{table}: live={live_n} candidate={candidate_n} (DROP of {live_n - candidate_n})")

    if regressions:
        print("CUTOVER_BLOCKED: candidate database has fewer rows than the live container:")
        for line in regressions:
            print(f"  - {line}")
        print(
            "This is exactly the failure mode of the 2026-09-16 staging incident "
            "(database volume mounted at the wrong container-internal path -> "
            "candidate silently ran against an empty database). Do not connect this "
            "candidate to the live network alias. Compare mount paths first:\n"
            f"  docker inspect {live_container} --format '{{{{range .Mounts}}}}{{{{.Source}}}} -> {{{{.Destination}}}}{{{{println}}}}{{{{end}}}}'\n"
            f"  docker inspect {candidate_container} --format '{{{{range .Mounts}}}}{{{{.Source}}}} -> {{{{.Destination}}}}{{{{println}}}}{{{{end}}}}'"
        )
        return 1

    print("CUTOVER_OK: candidate database state is at least as complete as the live container.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", required=True, help="Currently-live container name (already holding the network alias).")
    parser.add_argument("--candidate", required=True, help="New candidate container name, not yet connected with the live alias.")
    parser.add_argument("--force", action="store_true", help="Proceed even if the live container's database can't be read -- only for a brand-new environment's first-ever deploy.")
    args = parser.parse_args()
    return check(args.live, args.candidate, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
