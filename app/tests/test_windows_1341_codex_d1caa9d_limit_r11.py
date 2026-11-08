"""Test gap from the Codex read-only review of d1caa9d (PASS WITH NOTES, one Low test gap), owner-approved 2026-11-08.

The r11 target-limit test still passed with the 64-target limit removed, because its redirect chain reached the 20-hop
limit first. These tests use redirect trees that fan out (each Location has two or three distinct interpretations), so
only the target limit can end them, and they count the router calls: the classification never routes more than 64
targets, a tree of exactly 64 that reaches no reset is not the reset, and one target more is.
"""
from types import SimpleNamespace

import pytest

import main

LIMIT = 64
# A Location whose interpretations (as supplied, dot segments removed, %2E dot segments removed) give 1, 2 or 3 targets.
LOCATIONS = {1: "/t{n}/x", 2: "/t{n}/%2e/x", 3: "/t{n}/./%2e/x"}


@pytest.fixture(autouse=True)
def _isolated_recordings(tmp_path, monkeypatch):
    # Never read or write the shared recordings root (C:\app\recordings).
    from recordings_isolation import isolate_recordings
    return isolate_recordings(monkeypatch, tmp_path)


def test_recordings_derived_paths_are_isolated_for_this_module(_isolated_recordings):
    from recordings_isolation import assert_isolated
    assert_isolated(_isolated_recordings)


def _scope(path, method="PUT"):
    """A request scope as the server builds it: the path already percent-decoded once."""
    return {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "scheme": "http",
            "server": ("testserver", 80), "headers": [(b"host", b"testserver")]}


def _redirect_tree(monkeypatch, fanouts, reset_at=None):
    """Replace the router: the n-th call (from 1) redirects to len-distinct new targets given by fanouts[n - 1], calls
    past the list reach no route, and call reset_at reaches the reset. Every target is new, so none is deduplicated.
    Routing more than LIMIT targets fails the test at once instead of following the tree without end."""
    calls = []

    def router(scope):
        calls.append(scope["path"])
        n = len(calls)
        assert n <= LIMIT, f"routed {n} targets, more than the {LIMIT}-target limit"
        if n == reset_at:
            return "route", SimpleNamespace(endpoint=main.change_user_password)
        if n <= len(fanouts):
            return "redirect", "http://testserver" + LOCATIONS[fanouts[n - 1]].format(n=n)
        return "none", None

    monkeypatch.setattr(main, "_router_outcome", router)
    return calls


def test_the_limit_is_the_documented_one():
    assert main._PASSWORD_RESET_REDIRECT_PATHS == LIMIT
    assert main._PASSWORD_RESET_REDIRECT_HOPS == 20


@pytest.mark.parametrize("fanout", [2, 3])
def test_an_endless_redirect_tree_routes_at_most_the_limit_and_is_treated_as_the_reset(monkeypatch, fanout):
    calls = _redirect_tree(monkeypatch, [fanout] * 10 ** 6)
    assert main._request_reaches_password_reset(_scope("/start")) is True
    assert len(calls) == LIMIT
    assert len(set(calls)) == LIMIT  # every routed target was a distinct one


@pytest.mark.parametrize("fanouts", [[2] * 31 + [1], [3] * 21], ids=["fanout-2", "fanout-3"])
def test_a_tree_of_exactly_the_limit_that_reaches_no_reset_is_not_the_reset(monkeypatch, fanouts):
    calls = _redirect_tree(monkeypatch, fanouts)
    assert 1 + sum(fanouts) == LIMIT
    assert main._request_reaches_password_reset(_scope("/start")) is False
    assert len(calls) == LIMIT


@pytest.mark.parametrize("fanouts", [[2] * 32, [3] * 21 + [1]], ids=["fanout-2", "fanout-3"])
def test_one_target_more_than_the_limit_is_treated_as_the_reset_without_routing_it(monkeypatch, fanouts):
    calls = _redirect_tree(monkeypatch, fanouts)
    assert 1 + sum(fanouts) == LIMIT + 1
    assert main._request_reaches_password_reset(_scope("/start")) is True
    assert len(calls) == LIMIT


def test_a_reset_at_the_last_target_within_the_limit_is_found(monkeypatch):
    calls = _redirect_tree(monkeypatch, [2] * 31 + [1], reset_at=LIMIT)
    assert main._request_reaches_password_reset(_scope("/start")) is True
    assert len(calls) == LIMIT


def test_a_tree_within_both_limits_stays_ordinary(monkeypatch):
    calls = _redirect_tree(monkeypatch, [2] * 10)
    assert main._request_reaches_password_reset(_scope("/start")) is False
    assert len(calls) == 21
