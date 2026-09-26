#!/usr/bin/env python3
"""Staging portal blue/green cutover, with no Caddy 503 gap (2026-09-26).

Why this exists. On 2026-09-26 a gated cutover (verify_cutover_safety.py
said CUTOVER_OK) still left staging answering 503 "no upstreams available"
for ~2 minutes (14:58:08-15:00:03 UTC, 466 requests). The hand-run
procedure detached the live container from the Docker network *before*
stopping it. Caddy's active health checker (every 30 s, against the
`portal`/`vms` alias) still held a keep-alive connection to the old
container's address; with that interface gone, nothing ever closed the
connection, the next checks hung until they timed out ("request
canceled"), and Caddy marked its only upstream down until a later check
passed.

The order here fixes that, and never stops the live container before the
new one is proven through the real public route:

1. Start the candidate on the network WITHOUT the aliases (no traffic) and
   wait until it is healthy from inside.
2. The existing safety gate, verify_cutover_safety.py (unchanged): anything
   but CUTOVER_OK stops here with the live container untouched.
3. Give the candidate the aliases while the live container keeps them --
   both serve, so there is no moment without an upstream.
4. Through Caddy (the public URL): /health and /version must return 200 and
   the candidate's build id. Until that happens the live container keeps
   running; if it never happens the candidate is taken back out and the
   live container is left exactly as it was.
5. Only then stop the previous container WHILE IT IS STILL ATTACHED, so its
   connections close cleanly (Caddy's idle connections get a FIN instead of
   hanging), then detach it and keep it, stopped, as the rollback container.
6. Watch the public route for longer than one Caddy health interval. A
   brief blip is reported; if the public route stays down past the grace
   period, roll back automatically (restart the previous container with the
   aliases, take the candidate out).
7. The existing sprawl check, check_container_sprawl.py (unchanged).

Run on the staging host (Docker CLI + the gate scripts), e.g. via SSM:

    python3 deploy/staging_bluegreen_cutover.py --live portal-cc2404d \\
        --candidate portal-5de927e --image deploy-portal:5de927e \\
        --build-id 5de927e8d379e198840e013c1956bcd14a12500b

Exit 0 with CUTOVER_COMPLETE, or 1 with CUTOVER_ABORTED / CUTOVER_ROLLED_BACK
(the reason is printed). Application behaviour is not involved at all.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent
STAGING_MOUNTS = (
    "/var/lib/anyaicam-staging/db:/app/data",
    "/var/lib/anyaicam-staging/recordings:/app/recordings",
    "/var/lib/anyaicam-staging/hls:/app/static/hls",
    "/var/lib/anyaicam-staging/data-config:/opt/anyaicam/data/config",
)
APP_COMMAND = ("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers")
# Cloudflare in front of staging answers Python's default "Python-urllib/x"
# agent with 403 (error 1010) -- found on the first real run, where every
# public check was refused and the cutover (correctly) aborted.
USER_AGENT = "AnyAiCam-cutover-check/1.0"


class Cutover:
    def __init__(self, *, live: str, candidate: str, image: str, build_id: str,
                 public_url: str = "https://portal-staging.anyaicam.com",
                 host_header: str = "portal-staging.anyaicam.com",
                 network: str = "deploy_default", aliases: tuple[str, ...] = ("portal", "vms"),
                 env_file: str = "/etc/anyaicam-staging/vms-staging.env",
                 mounts: tuple[str, ...] = STAGING_MOUNTS,
                 candidate_timeout: float = 120, public_timeout: float = 180, public_successes: int = 3,
                 watch_seconds: float = 45, outage_grace: float = 70, poll: float = 2,
                 run=None, http_get=None, sleep=time.sleep, clock=time.monotonic, log=print):
        self.live, self.candidate, self.image, self.build_id = live, candidate, image, build_id
        self.public_url, self.host_header = public_url.rstrip("/"), host_header
        self.network, self.aliases, self.env_file, self.mounts = network, aliases, env_file, mounts
        self.candidate_timeout, self.public_timeout, self.public_successes = candidate_timeout, public_timeout, public_successes
        self.watch_seconds, self.outage_grace, self.poll = watch_seconds, outage_grace, poll
        self.run = run or self._run
        self.http_get = http_get or self._http_get
        self.sleep, self.clock, self.log = sleep, clock, log
        self.rollback_name = f"{live}-pre-{candidate.removeprefix('portal-')}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-rollback"

    # ------------------------------------------------------------ I/O
    @staticmethod
    def _run(args: list[str]) -> tuple[int, str]:
        done = subprocess.run(args, capture_output=True, text=True, timeout=600, check=False)
        return done.returncode, (done.stdout or "") + (done.stderr or "")

    @staticmethod
    def _http_get(url: str) -> tuple[int, str]:
        request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return response.status, response.read(4096).decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, ""
        except Exception:
            return 0, ""

    def docker(self, *args: str, check: bool = True) -> str:
        code, output = self.run(["docker", *args])
        if check and code != 0:
            raise RuntimeError(f"docker {' '.join(args[:3])} failed: {output.strip()[:300]}")
        return output

    # ------------------------------------------------------------ checks
    def candidate_healthy(self) -> bool:
        probe = ("import urllib.request;print(urllib.request.urlopen(urllib.request.Request("
                 f"'http://127.0.0.1:8000/health',headers={{'Host':'{self.host_header}'}}),timeout=3).read().decode())")
        code, output = self.run(["docker", "exec", self.candidate, "python3", "-c", probe])
        return code == 0 and '"ok"' in output

    def public_answers(self) -> dict[str, str | None]:
        """Per endpoint, the build id Caddy's public route answered with a
        200 right now (None for any other status or an unreadable body).
        While both containers hold the aliases each request may reach
        either, so the two endpoints are judged separately."""
        nonce = uuid.uuid4().hex[:8]  # never an edge-cached answer
        answers = {}
        for path in ("/health", "/version"):
            status, body = self.http_get(f"{self.public_url}{path}?cutover={nonce}")
            try:
                answers[path] = json.loads(body).get("build_id") if status == 200 else None
            except (ValueError, AttributeError):
                answers[path] = None
        return answers

    def public_build(self) -> str | None:
        """The one build both endpoints answer with 200, else None."""
        answers = self.public_answers()
        return answers["/health"] if answers["/health"] and answers["/health"] == answers["/version"] else None

    def wait(self, condition, timeout: float) -> bool:
        deadline = self.clock() + timeout
        while True:
            if condition():
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(self.poll)

    # ------------------------------------------------------------ steps
    def attach(self, name: str) -> None:
        self.docker("network", "disconnect", self.network, name, check=False)
        alias_args = [arg for alias in self.aliases for arg in ("--alias", alias)]
        self.docker("network", "connect", *alias_args, self.network, name)

    def retire(self, name: str, new_name: str) -> None:
        """Stop while still attached (connections close cleanly), then detach."""
        self.docker("stop", name)
        self.docker("network", "disconnect", self.network, name, check=False)
        self.docker("rename", name, new_name)

    def abort(self, reason: str, suffix: str, *, attached: bool = False) -> int:
        self.log(f"CUTOVER_ABORTED: {reason} -- live container {self.live} left serving, untouched.")
        self.docker("stop", self.candidate, check=False)
        if attached:
            self.docker("network", "disconnect", self.network, self.candidate, check=False)
        self.docker("rename", self.candidate, f"{self.candidate}-{suffix}", check=False)
        return 1

    def execute(self) -> int:
        # 1. candidate, no aliases
        self.docker("run", "-d", "--name", self.candidate, "--restart", "unless-stopped", "--network", self.network,
                    "--env-file", self.env_file, "-e", f"ANYAICAM_BUILD_ID={self.build_id}",
                    *[arg for mount in self.mounts for arg in ("-v", mount)],
                    "-w", "/app", self.image, *APP_COMMAND)
        if not self.wait(self.candidate_healthy, self.candidate_timeout):
            return self.abort("candidate never became healthy", "failed")
        self.log(f"candidate {self.candidate} healthy")

        # 2. the existing safety gate
        code, gate = self.run([sys.executable, str(DEPLOY_DIR / "verify_cutover_safety.py"),
                               "--live", self.live, "--candidate", self.candidate])
        self.log(gate.strip())
        if code != 0 or "CUTOVER_OK" not in gate:
            return self.abort("safety gate did not return CUTOVER_OK", "blocked")

        # 3. both hold the aliases
        self.attach(self.candidate)
        self.log(f"{self.candidate} attached with aliases {', '.join(self.aliases)}; {self.live} still serving")

        # 4. proven through Caddy before the live container is touched
        seen = {"/health": 0, "/version": 0}

        def public_serves_candidate() -> bool:
            for path, build in self.public_answers().items():
                seen[path] += build == self.build_id
            return min(seen.values()) >= self.public_successes

        if not self.wait(public_serves_candidate, self.public_timeout):
            return self.abort(f"public route never served build {self.build_id[:12]} with /health and /version 200",
                              "unreachable", attached=True)
        self.log(f"public /health and /version serve {self.build_id[:12]} through Caddy")

        # 5. retire the previous container, kept as the rollback
        self.retire(self.live, self.rollback_name)
        self.log(f"{self.live} stopped while attached, detached, kept as {self.rollback_name}")

        # 6. watch past a Caddy health interval; roll back only on a real outage
        start, down_since, blips = self.clock(), None, 0
        while self.clock() - start < self.watch_seconds or down_since is not None:
            if self.public_build() == self.build_id:
                down_since = None
            else:
                blips += 1
                down_since = down_since if down_since is not None else self.clock()
                if self.clock() - down_since >= self.outage_grace:
                    return self.rollback()
            self.sleep(self.poll)
        if blips:
            self.log(f"WARNING: {blips} public check(s) failed during the watch, recovered without rollback")

        # 7. the existing sprawl check
        _code, sprawl = self.run([sys.executable, str(DEPLOY_DIR / "check_container_sprawl.py"), "--live", self.candidate])
        self.log(sprawl.strip())
        self.log(f"CUTOVER_COMPLETE: {self.candidate} live on {self.build_id}; rollback: {self.rollback_name}")
        return 0

    def rollback(self) -> int:
        self.log(f"public route down for {self.outage_grace:.0f}s after cutover -- rolling back to {self.rollback_name}")
        self.docker("rename", self.rollback_name, self.live, check=False)
        self.attach(self.live)
        self.docker("start", self.live, check=False)
        self.retire(self.candidate, f"{self.candidate}-rolled-back")
        restored = self.wait(lambda: self.public_build() not in (None, self.build_id), self.public_timeout)
        self.log(f"CUTOVER_ROLLED_BACK: {self.live} {'serving again' if restored else 'restarted, public route NOT yet confirmed'}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--live", required=True, help="Currently-live portal container (holds the aliases).")
    parser.add_argument("--candidate", required=True, help="Name for the new container, e.g. portal-<commit>.")
    parser.add_argument("--image", required=True, help="Built image, e.g. deploy-portal:<commit>.")
    parser.add_argument("--build-id", required=True, help="Full commit the candidate must report on /health and /version.")
    parser.add_argument("--public-url", default="https://portal-staging.anyaicam.com")
    parser.add_argument("--watch-seconds", type=float, default=45, help="Post-cutover public watch (> Caddy's 30 s health interval).")
    args = parser.parse_args()
    return Cutover(live=args.live, candidate=args.candidate, image=args.image, build_id=args.build_id,
                   public_url=args.public_url, watch_seconds=args.watch_seconds).execute()


if __name__ == "__main__":
    sys.exit(main())
