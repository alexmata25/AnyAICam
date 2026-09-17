"""AAC relay/access-control abstraction (Phase 1).

The physical AnyAiCam 3-channel relay board is not available during this
phase. Every concept a real relay integration will eventually need --
channel selection, pulse duration, debounce, cooldown, and a hard
dry-run/test mode -- is modeled here as a plain, hardware-free
interface (RelayProvider) plus one concrete implementation
(MockRelayProvider) that a later hardware milestone can sit behind
without any caller (facial_events.py, or any future access-control
consumer) changing.

Safety posture, deliberately conservative:

1. RelayProvider.trigger() NEVER touches real hardware in this phase --
   there is no hardware-backed implementation in this module at all,
   only MockRelayProvider (records the call, never raises, never
   asserts a physical line). A real GPIO/serial-backed provider is
   future work for when the board is physically available and can be
   bench-verified; adding one is explicitly out of scope here (see the
   Phase 1 report's "real relay tested: NO").
2. Every trigger request carries its own dry_run flag, and the default
   (see RelayRule/facial_rules.dry_run in the DB migration) is dry_run
   =True -- a rule must be EXPLICITLY edited to dry_run=False before a
   provider is even asked to actually pulse a channel. MockRelayProvider
   still just records either way (it has no hardware to actually pulse),
   but real providers built later must honor this flag as their one
   mandatory safety gate.
3. Channel is restricted to {1, 2, 3} -- the real board's only three
   channels -- so a bug elsewhere can never construct a request for a
   channel that doesn't physically exist.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

VALID_CHANNELS = (1, 2, 3)

# 2026-09-16: closes the gap between "the authorization-decision
# architecture exists and is fully tested" and "it is ever actually
# reached" -- record_facial_events() in facial_events.py has always
# accepted a relay_provider, but main.py's real detection hook
# (save_yolo_events()) passed None, so evaluate_access_rules() never
# ran for a real detection at all; identity matches were recorded, but
# no facial_rules row was ever evaluated against them. This flag wires
# a provider into that hook -- but even at its most "on", the provider
# constructed below is ALWAYS MockRelayProvider: there is still no
# hardware-backed RelayProvider implementation anywhere in this
# codebase, so turning this on can never energize a real relay or
# unlock a real door. It only lets the identity-match -> authorization-
# decision -> (mock) access-control-command chain run for real and be
# observed end-to-end -- exactly the "digital path first" scope, never
# the physical one. Default false: existing dormant behavior is
# unchanged unless an operator explicitly opts in.
FACIAL_ACCESS_CONTROL_ENABLED = os.environ.get("ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED", "false").strip().lower() == "true"

DEFAULT_PULSE_MS = 3000
DEFAULT_COOLDOWN_SECONDS = 10.0


@dataclass(frozen=True)
class RelayRequest:
    """One request to actuate a channel. Immutable -- a provider or test
    can inspect exactly what was asked for without it changing under it."""

    channel: int
    pulse_ms: int = DEFAULT_PULSE_MS
    reason: str = ""
    dry_run: bool = True
    requested_by: str = ""

    def __post_init__(self) -> None:
        if self.channel not in VALID_CHANNELS:
            raise ValueError(f"Unsupported relay channel: {self.channel!r}. Must be one of {VALID_CHANNELS}.")
        if self.pulse_ms <= 0:
            raise ValueError("pulse_ms must be positive.")


@dataclass(frozen=True)
class RelayResult:
    """What actually happened for one RelayRequest. `activated` is True
    only when a provider believes it actually (or, for the mock,
    notionally) pulsed the channel -- never True for a request that was
    suppressed by cooldown/debounce or that was dry-run."""

    channel: int
    activated: bool
    dry_run: bool
    suppressed_reason: str | None = None
    at: float = field(default_factory=time.monotonic)


class RelayProvider:
    """Abstract interface every relay backend (mock today, real hardware
    later) implements. Callers (facial_events.py) depend on this
    interface only -- never on MockRelayProvider directly -- so a future
    hardware provider is a drop-in replacement."""

    def trigger(self, request: RelayRequest) -> RelayResult:  # pragma: no cover - interface
        raise NotImplementedError

    def capability(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class MockRelayProvider(RelayProvider):
    """Development/test relay: records every call it receives (for
    assertions) and NEVER touches any real I/O -- there is no hardware
    to touch. Applies the same debounce/cooldown bookkeeping a real
    provider would, so higher-level logic (facial_events.py) can be
    fully exercised in tests without a physical board.

    Thread-safe: a real appliance may call trigger() from more than one
    detection worker."""

    def __init__(self, *, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS, clock=time.monotonic) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_activated_at: dict[int, float] = {}
        self.calls: list[RelayRequest] = []
        self.results: list[RelayResult] = []

    def capability(self) -> dict:
        return {
            "provider": "mock",
            "hardware_connected": False,
            "channels": list(VALID_CHANNELS),
            "cooldown_seconds": self.cooldown_seconds,
            "note": "Development/test provider only. No physical relay hardware is driven by this provider.",
        }

    def reset(self) -> None:
        """Test-only: clears cooldown state and recorded calls."""
        with self._lock:
            self._last_activated_at.clear()
            self.calls.clear()
            self.results.clear()

    def trigger(self, request: RelayRequest) -> RelayResult:
        with self._lock:
            self.calls.append(request)
            now = self._clock()
            last = self._last_activated_at.get(request.channel)
            if last is not None and (now - last) < self.cooldown_seconds:
                result = RelayResult(
                    channel=request.channel,
                    activated=False,
                    dry_run=request.dry_run,
                    suppressed_reason="cooldown",
                    at=now,
                )
                self.results.append(result)
                return result
            if request.dry_run:
                result = RelayResult(channel=request.channel, activated=False, dry_run=True, at=now)
                self.results.append(result)
                return result
            # "Activated" here means only "the mock recorded a real
            # (non-dry-run) pulse" -- still never any actual hardware
            # I/O. This is the one line a future hardware provider
            # replaces with a genuine GPIO/serial pulse.
            self._last_activated_at[request.channel] = now
            result = RelayResult(channel=request.channel, activated=True, dry_run=False, at=now)
            self.results.append(result)
            return result


@dataclass(frozen=True)
class RelayRule:
    """Pure representation of one facial_rules DB row -- decoupled from
    SQL so the "does this match trigger this rule, and how" decision is
    unit-testable without a database. facial_events.py is responsible
    for loading rows into this shape and for actually calling a
    RelayProvider with the result of evaluate_rule()."""

    id: str
    trigger_type: str  # 'known_person' | 'watchlist_allow' | 'specific_person'
    channel: int
    pulse_ms: int
    cooldown_seconds: float
    dry_run: bool
    enabled: bool
    min_confidence: float
    watchlist_id: str | None = None
    person_id: str | None = None


def rule_applies(
    rule: RelayRule,
    *,
    match_state: str,
    confidence: float,
    matched_person_id: str | None,
    matched_watchlist_id: str | None,
) -> bool:
    """Pure decision: would this rule fire for this match? Never touches
    a RelayProvider or a database -- see facial_events.evaluate_access_rules()
    for the caller that turns a True result into an actual (possibly
    dry-run) RelayRequest."""
    if not rule.enabled:
        return False
    if confidence < rule.min_confidence:
        return False
    if rule.trigger_type == "known_person":
        return match_state == "known"
    if rule.trigger_type == "watchlist_allow":
        return match_state == "watchlist" and rule.watchlist_id is not None and rule.watchlist_id == matched_watchlist_id
    if rule.trigger_type == "specific_person":
        return rule.person_id is not None and rule.person_id == matched_person_id
    return False


_provider: RelayProvider | None = None
_provider_lock = threading.Lock()


def get_provider() -> RelayProvider:
    """Process-wide lazy singleton, matching facial_recognition.get_engine()'s
    own pattern -- so cooldown/debounce state (tracked per-instance on
    MockRelayProvider) persists correctly across detections instead of
    resetting on every call. Always returns a MockRelayProvider today;
    see this module's own docstring and FACIAL_ACCESS_CONTROL_ENABLED's
    comment for why that remains true regardless of the flag's state."""
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = MockRelayProvider()
        return _provider


def reset_provider() -> None:
    """Test-only: clears the lazy-loaded provider singleton."""
    global _provider
    with _provider_lock:
        _provider = None


def build_request(rule: RelayRule, *, reason: str, requested_by: str = "aac_facial_recognition") -> RelayRequest:
    """Turns a matched rule into the RelayRequest a provider is asked to
    fulfil. rule.dry_run flows straight through -- this function never
    upgrades a dry-run rule into a real trigger; only editing the rule
    itself (an explicit administrative action, audited -- see
    facial_people.py) can do that."""
    return RelayRequest(
        channel=rule.channel,
        pulse_ms=rule.pulse_ms,
        reason=reason,
        dry_run=rule.dry_run,
        requested_by=requested_by,
    )
