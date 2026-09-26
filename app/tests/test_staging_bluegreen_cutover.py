"""Staging blue/green cutover without a Caddy 503 gap (2026-09-26):
deploy/staging_bluegreen_cutover.py. The live container keeps serving until
the candidate holds the aliases AND the public route (through Caddy) answers
/health and /version with 200 and the candidate's build; only then is it
stopped -- while still attached, so connections close cleanly -- detached,
and kept as the rollback. The existing safety gate and sprawl check still
run. Docker, HTTP and the clock are simulated; no real containers."""

import importlib.util
import json
import random
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "staging_bluegreen_cutover.py"
_spec = importlib.util.spec_from_file_location("staging_bluegreen_cutover", _SCRIPT)
cut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cut)

OLD, NEW = "a" * 40, "b" * 40


class World:
    """A tiny Docker + Caddy: the public route serves whichever running
    containers hold the aliases (each request picks one, as Caddy's DNS
    lookup of the alias does), 503 when none do."""

    def __init__(self, *, gate="CUTOVER_OK", candidate_healthy=True, public_broken_after_stop=0, caddy_reaches_candidate=True):
        self.containers = {"portal-old": {"running": True, "attached": True, "aliases": True, "build": OLD}}
        self.events, self.gate, self.candidate_healthy = [], gate, candidate_healthy
        self.public_broken_after_stop = public_broken_after_stop  # seconds of 503 after the live container stops
        self.caddy_reaches_candidate = caddy_reaches_candidate
        self.now, self.stopped_live_at, self.pick = 0.0, None, random.Random(7)

    # docker / scripts
    def run(self, args):
        if args[0] != "docker":
            script = Path(args[1]).name
            self.events.append(("script", script))
            if script == "verify_cutover_safety.py":
                return (0 if self.gate == "CUTOVER_OK" else 1), self.gate
            return 0, "CONTAINER_SPRAWL_OK: only the live container is running."
        verb, rest = args[1], args[2:]
        if verb == "run":
            name = rest[rest.index("--name") + 1]
            self.containers[name] = {"running": True, "attached": True, "aliases": False, "build": NEW}
            self.events.append(("run", name))
        elif verb == "exec":
            return (0, '{"status":"ok"}') if self.candidate_healthy else (1, "")
        elif verb == "network":
            name = rest[-1]
            box = self.containers.get(name)
            if box is None:
                return 1, "no such container"
            if rest[0] == "disconnect":
                box["attached"] = box["aliases"] = False
            else:
                box["attached"], box["aliases"] = True, "--alias" in rest
            self.events.append((rest[0], name))
        elif verb in ("stop", "start"):
            name = rest[0]
            if name not in self.containers:
                return 1, "no such container"
            self.containers[name]["running"] = verb == "start"
            if verb == "stop" and name == "portal-old":
                self.stopped_live_at = self.now
            self.events.append((verb, name, self.containers[name]["attached"]))
        elif verb == "rename":
            if rest[0] not in self.containers:
                return 1, "no such container"
            self.containers[rest[1]] = self.containers.pop(rest[0])
            self.events.append(("rename", rest[0], rest[1]))
        return 0, ""

    # the public route through Caddy
    def http_get(self, url):
        if self.stopped_live_at is not None and self.now - self.stopped_live_at < self.public_broken_after_stop:
            return 503, ""
        serving = [b for n, b in self.containers.items() if b["running"] and b["aliases"]
                   and (self.caddy_reaches_candidate or b["build"] != NEW)]
        if not serving:
            return 503, ""
        box = self.pick.choice(serving)
        return 200, json.dumps({"status": "ok", "build_id": box["build"]})

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now


def _cutover(world, **overrides):
    options = dict(live="portal-old", candidate="portal-new", image="deploy-portal:new", build_id=NEW,
                   run=world.run, http_get=world.http_get, sleep=world.sleep, clock=world.clock, log=lambda *_: None)
    options.update(overrides)
    return cut.Cutover(**options)


def _index(events, event):
    return events.index(event)


def test_live_keeps_serving_until_the_public_route_serves_the_candidate():
    world = World()
    job = _cutover(world)
    public_checks = []
    real_get = world.http_get
    world_get = lambda url: public_checks.append((len(world.events), url)) or real_get(url)
    job.http_get = world_get
    assert job.execute() == 0
    events = world.events
    attached = _index(events, ("connect", "portal-new"))
    stopped = _index(events, ("stop", "portal-old", True))  # stopped WHILE still attached
    assert _index(events, ("script", "verify_cutover_safety.py")) < attached < stopped
    # The public route (health AND version) was checked after the candidate got the aliases and before the stop.
    assert any(attached < at <= stopped and "/health" in url for at, url in public_checks)
    assert any(attached < at <= stopped and "/version" in url for at, url in public_checks)
    assert _index(events, ("disconnect", "portal-old")) > stopped
    assert ("rename", "portal-old", job.rollback_name) in events
    assert events[-1] == ("script", "check_container_sprawl.py")
    assert job.rollback_name.startswith("portal-old-pre-new-") and job.rollback_name.endswith("-rollback")
    # Never a moment without an upstream: the public route answered 200 throughout.
    assert all(real_get("x")[0] == 200 for _ in range(3))


def test_the_public_route_is_never_down_during_a_clean_cutover():
    world = World()
    statuses = []
    job = _cutover(world)
    job.http_get = lambda url: statuses.append(world.http_get(url)[0]) or world.http_get(url)
    assert job.execute() == 0
    assert statuses and set(statuses) == {200}


def test_gate_blocked_leaves_live_untouched():
    world = World(gate="CUTOVER_BLOCKED: candidate has fewer cameras")
    assert _cutover(world).execute() == 1
    assert world.containers["portal-old"] == {"running": True, "attached": True, "aliases": True, "build": OLD}
    assert "portal-new-blocked" in world.containers and not world.containers["portal-new-blocked"]["running"]
    assert not any(e[0] == "connect" for e in world.events)


def test_unhealthy_candidate_leaves_live_untouched():
    world = World(candidate_healthy=False)
    assert _cutover(world, candidate_timeout=10).execute() == 1
    assert world.containers["portal-old"]["running"] and world.containers["portal-old"]["aliases"]
    assert "portal-new-failed" in world.containers
    assert ("script", "verify_cutover_safety.py") not in world.events


def test_candidate_unreachable_through_caddy_is_taken_back_out_and_live_untouched():
    world = World(caddy_reaches_candidate=False)
    assert _cutover(world, public_timeout=20).execute() == 1
    live = world.containers["portal-old"]
    assert live["running"] and live["aliases"]
    assert not any(e[:2] == ("stop", "portal-old") for e in world.events)
    rejected = world.containers["portal-new-unreachable"]
    assert not rejected["running"] and not rejected["aliases"]


def test_public_serving_only_the_old_build_never_counts():
    """200s from the still-live container are not proof the candidate is reachable."""
    world = World()
    job = _cutover(world, public_timeout=20)
    job.public_answers = lambda: {"/health": OLD, "/version": OLD}
    assert job.execute() == 1
    assert world.containers["portal-old"]["running"]


def test_non_200_or_mismatched_builds_are_not_success():
    world = World()
    job = _cutover(world)
    job.http_get = lambda url: (200, json.dumps({"build_id": NEW})) if "/health" in url else (503, "")
    assert job.public_build() is None
    job.http_get = lambda url: (200, json.dumps({"build_id": NEW if "/health" in url else OLD}))
    assert job.public_build() is None
    job.http_get = lambda url: (200, "not json")
    assert job.public_build() is None


def test_a_brief_blip_after_the_stop_is_reported_not_rolled_back():
    world = World(public_broken_after_stop=6)
    logs = []
    assert _cutover(world, log=logs.append).execute() == 0
    assert any("WARNING" in line for line in logs)
    assert world.containers["portal-new"]["running"]


def test_a_real_outage_after_the_stop_rolls_back_to_the_previous_container():
    world = World(public_broken_after_stop=1000)
    job = _cutover(world, outage_grace=20, public_timeout=60)
    assert job.execute() == 1
    live = world.containers["portal-old"]
    assert live["running"] and live["aliases"]  # previous container serving again under its own name
    rolled = world.containers["portal-new-rolled-back"]
    assert not rolled["running"] and not rolled["aliases"]
    stop = next(e for e in world.events if e[:2] == ("stop", "portal-new"))
    assert stop[2] is True  # the candidate is also stopped while attached


def test_default_watch_outlasts_a_caddy_health_interval():
    assert cut.Cutover(live="l", candidate="c", image="i", build_id=NEW).watch_seconds > 30


def test_public_checks_identify_themselves_to_cloudflare(monkeypatch):
    """Cloudflare refuses Python's default agent (403, error 1010) -- the
    first real run aborted on exactly that."""
    seen = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def read(self, _n): return b'{"build_id": "x"}'

    def urlopen(request, timeout):
        seen.update({k.lower(): v for k, v in request.header_items()})
        return Response()

    monkeypatch.setattr(cut.urllib.request, "urlopen", urlopen)
    assert cut.Cutover._http_get("https://portal-staging.anyaicam.com/health")[0] == 200
    assert seen["user-agent"] == cut.USER_AGENT and "python-urllib" not in seen["user-agent"].lower()
    assert seen["cache-control"] == "no-cache"


def test_public_proof_allows_for_a_small_share_of_traffic_reaching_the_candidate():
    """On the real run Caddy sent ~10% of requests to the candidate while
    both held the aliases; the proof window must be long enough for 3 hits
    per endpoint at that rate."""
    job = cut.Cutover(live="l", candidate="c", image="i", build_id=NEW)
    assert job.public_timeout >= 180 and job.public_successes == 3
