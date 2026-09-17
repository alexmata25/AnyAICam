"""AAC relay abstraction -- Phase 1. Pure logic, no hardware, no
database: MockRelayProvider, RelayRequest validation, cooldown/
debounce, dry-run safety, and RelayRule matching (rule_applies())."""

import pytest

import relay_control
from relay_control import (
    MockRelayProvider,
    RelayRequest,
    RelayRule,
    VALID_CHANNELS,
    build_request,
    rule_applies,
)


# --------------------------------------------------------------- RelayRequest


def test_valid_channels_are_exactly_one_two_three():
    assert VALID_CHANNELS == (1, 2, 3)


@pytest.mark.parametrize("channel", [0, 4, -1, 99])
def test_invalid_channel_is_rejected(channel):
    with pytest.raises(ValueError):
        RelayRequest(channel=channel)


@pytest.mark.parametrize("channel", [1, 2, 3])
def test_valid_channel_is_accepted(channel):
    request = RelayRequest(channel=channel)
    assert request.channel == channel


def test_non_positive_pulse_ms_is_rejected():
    with pytest.raises(ValueError):
        RelayRequest(channel=1, pulse_ms=0)


# --------------------------------------------------------------- MockRelayProvider


def test_mock_provider_reports_no_hardware_connected():
    provider = MockRelayProvider()
    capability = provider.capability()
    assert capability["hardware_connected"] is False
    assert capability["provider"] == "mock"


def test_dry_run_request_never_activates():
    provider = MockRelayProvider()
    result = provider.trigger(RelayRequest(channel=1, dry_run=True))
    assert result.activated is False
    assert result.dry_run is True


def test_non_dry_run_request_activates_channel_1():
    provider = MockRelayProvider(cooldown_seconds=0)
    result = provider.trigger(RelayRequest(channel=1, dry_run=False))
    assert result.activated is True
    assert result.channel == 1


def test_non_dry_run_request_activates_channel_2():
    provider = MockRelayProvider(cooldown_seconds=0)
    result = provider.trigger(RelayRequest(channel=2, dry_run=False))
    assert result.activated is True
    assert result.channel == 2


def test_non_dry_run_request_activates_channel_3():
    provider = MockRelayProvider(cooldown_seconds=0)
    result = provider.trigger(RelayRequest(channel=3, dry_run=False))
    assert result.activated is True
    assert result.channel == 3


def test_cooldown_suppresses_immediate_repeat_trigger():
    clock = iter([100.0, 100.5, 100.9]).__next__
    provider = MockRelayProvider(cooldown_seconds=10, clock=clock)
    first = provider.trigger(RelayRequest(channel=1, dry_run=False))
    second = provider.trigger(RelayRequest(channel=1, dry_run=False))
    assert first.activated is True
    assert second.activated is False
    assert second.suppressed_reason == "cooldown"


def test_cooldown_expires_after_the_configured_window():
    times = iter([100.0, 200.0])
    provider = MockRelayProvider(cooldown_seconds=10, clock=lambda: next(times))
    first = provider.trigger(RelayRequest(channel=1, dry_run=False))
    second = provider.trigger(RelayRequest(channel=1, dry_run=False))
    assert first.activated is True
    assert second.activated is True


def test_cooldown_is_independent_per_channel():
    provider = MockRelayProvider(cooldown_seconds=1000, clock=lambda: 100.0)
    first = provider.trigger(RelayRequest(channel=1, dry_run=False))
    second = provider.trigger(RelayRequest(channel=2, dry_run=False))
    assert first.activated is True
    assert second.activated is True


def test_dry_run_request_never_starts_a_cooldown():
    provider = MockRelayProvider(cooldown_seconds=1000, clock=lambda: 100.0)
    provider.trigger(RelayRequest(channel=1, dry_run=True))
    real = provider.trigger(RelayRequest(channel=1, dry_run=False))
    assert real.activated is True


def test_every_call_is_recorded_for_test_assertions():
    provider = MockRelayProvider()
    provider.trigger(RelayRequest(channel=1, dry_run=True))
    provider.trigger(RelayRequest(channel=2, dry_run=True))
    assert len(provider.calls) == 2
    assert len(provider.results) == 2


def test_reset_clears_cooldown_and_recorded_calls():
    provider = MockRelayProvider(cooldown_seconds=1000, clock=lambda: 100.0)
    provider.trigger(RelayRequest(channel=1, dry_run=False))
    provider.reset()
    assert provider.calls == []
    result = provider.trigger(RelayRequest(channel=1, dry_run=False))
    assert result.activated is True  # cooldown state was cleared, not still active


def test_mock_provider_has_no_hardware_io_attribute_surface():
    """There is no serial/GPIO/socket handle anywhere on the mock --
    this is a structural guard against ever accidentally wiring a real
    I/O primitive into the "mock" used by tests and dev by default."""
    provider = MockRelayProvider()
    forbidden = {"serial", "gpio", "socket", "port", "device"}
    assert not (set(vars(provider).keys()) & forbidden)


# --------------------------------------------------------------- rule_applies()


def _rule(**overrides) -> RelayRule:
    defaults = dict(
        id="rule-1",
        trigger_type="known_person",
        channel=1,
        pulse_ms=3000,
        cooldown_seconds=10,
        dry_run=True,
        enabled=True,
        min_confidence=0.6,
        watchlist_id=None,
        person_id=None,
    )
    defaults.update(overrides)
    return RelayRule(**defaults)


def test_disabled_rule_never_applies():
    rule = _rule(enabled=False)
    assert not rule_applies(rule, match_state="known", confidence=0.99, matched_person_id="p1", matched_watchlist_id=None)


def test_known_person_rule_applies_only_to_known_state():
    rule = _rule(trigger_type="known_person")
    assert rule_applies(rule, match_state="known", confidence=0.9, matched_person_id="p1", matched_watchlist_id=None)
    assert not rule_applies(rule, match_state="unknown", confidence=0.9, matched_person_id=None, matched_watchlist_id=None)
    assert not rule_applies(rule, match_state="watchlist", confidence=0.9, matched_person_id="p1", matched_watchlist_id="w1")


def test_confidence_below_rule_minimum_never_applies():
    rule = _rule(trigger_type="known_person", min_confidence=0.9)
    assert not rule_applies(rule, match_state="known", confidence=0.89, matched_person_id="p1", matched_watchlist_id=None)


def test_confidence_at_exactly_rule_minimum_applies():
    rule = _rule(trigger_type="known_person", min_confidence=0.9)
    assert rule_applies(rule, match_state="known", confidence=0.9, matched_person_id="p1", matched_watchlist_id=None)


def test_watchlist_allow_rule_requires_matching_watchlist_id():
    rule = _rule(trigger_type="watchlist_allow", watchlist_id="w1")
    assert rule_applies(rule, match_state="watchlist", confidence=0.9, matched_person_id="p1", matched_watchlist_id="w1")
    assert not rule_applies(rule, match_state="watchlist", confidence=0.9, matched_person_id="p1", matched_watchlist_id="w2")


def test_specific_person_rule_requires_matching_person_id():
    rule = _rule(trigger_type="specific_person", person_id="p1")
    assert rule_applies(rule, match_state="known", confidence=0.9, matched_person_id="p1", matched_watchlist_id=None)
    assert not rule_applies(rule, match_state="known", confidence=0.9, matched_person_id="p2", matched_watchlist_id=None)


def test_unknown_trigger_type_never_applies():
    rule = _rule(trigger_type="something_new")
    assert not rule_applies(rule, match_state="known", confidence=0.9, matched_person_id="p1", matched_watchlist_id=None)


def test_build_request_preserves_dry_run_from_rule():
    dry_rule = _rule(dry_run=True)
    live_rule = _rule(dry_run=False)
    assert build_request(dry_rule, reason="test").dry_run is True
    assert build_request(live_rule, reason="test").dry_run is False


def test_build_request_uses_rule_channel_and_pulse():
    rule = _rule(channel=2, pulse_ms=1500)
    request = build_request(rule, reason="test")
    assert request.channel == 2
    assert request.pulse_ms == 1500


# ----------------------------------------------- process-wide provider singleton


@pytest.fixture(autouse=True)
def _reset_provider_singleton():
    relay_control.reset_provider()
    yield
    relay_control.reset_provider()


def test_facial_access_control_enabled_defaults_to_false(monkeypatch):
    """Existing dormant behavior (identity recorded, access rules never
    evaluated) must stay the default unless an operator explicitly opts
    in -- matches every other ANYAICAM_*_ENABLED flag in this codebase."""
    monkeypatch.delenv("ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED", raising=False)
    import importlib
    importlib.reload(relay_control)
    assert relay_control.FACIAL_ACCESS_CONTROL_ENABLED is False
    importlib.reload(relay_control)  # restore a clean module for later tests


def test_get_provider_returns_the_same_instance_so_cooldown_state_persists():
    first = relay_control.get_provider()
    second = relay_control.get_provider()
    assert first is second


def test_get_provider_is_always_a_mock_regardless_of_the_flag(monkeypatch):
    """The flag only decides whether the authorization chain runs at
    all -- it can never select a hardware-backed provider, because none
    exists in this codebase yet."""
    monkeypatch.setattr(relay_control, "FACIAL_ACCESS_CONTROL_ENABLED", True)
    assert isinstance(relay_control.get_provider(), relay_control.MockRelayProvider)


def test_reset_provider_clears_the_singleton():
    first = relay_control.get_provider()
    relay_control.reset_provider()
    second = relay_control.get_provider()
    assert first is not second
