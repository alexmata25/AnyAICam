"""AAC Voice Call is separate from ordinary Talk Down (owner decision
2026-10-05, after Codex review of 453bd1c).

AI Local and Hybrid include ordinary supported Talk Down / two-way audio. No
plan grants AAC Voice Call: it comes only from a package that grants
voice_call itself (today the Talk Down add-on, as configured), so customers
who already hold one keep it. Basic Local gets neither automatically.
"""
import re

import pytest

from database_backend import override_target
from test_my_subscription_v2_offers import (OWNER, _addon_rows, _buy, _hold, _included, _offer, _page, _plan,  # noqa: F401
                                            db_path, initialized_db, owner_client, prices, seed_customer)


def _feature(db_path, key, customer_id="cust-v2"):
    import analytics_entitlements as analytics
    from partner_db import connection
    with override_target(sqlite_path=str(db_path)), connection() as db:
        return analytics.account_wide_feature_active(db, customer_id, key)


@pytest.mark.parametrize("plan_key", ["ai_local", "hybrid"])
def test_ai_local_and_hybrid_get_ordinary_talk_down_but_not_aac_voice_call(db_path, monkeypatch, prices, plan_key):
    _plan(db_path, monkeypatch, plan_key)
    assert _feature(db_path, "talk_down") is True
    assert _feature(db_path, "voice_call") is False


def test_basic_local_gets_neither_talk_down_nor_voice_call_automatically(db_path, monkeypatch, prices):
    _plan(db_path, monkeypatch, "basic_local")
    assert _feature(db_path, "talk_down") is False and _feature(db_path, "voice_call") is False


@pytest.mark.parametrize("plan_key", ["basic_local", "ai_local", "hybrid"])
def test_a_held_voice_call_entitlement_is_still_honoured(db_path, monkeypatch, prices, plan_key):
    _plan(db_path, monkeypatch, plan_key)
    _hold(db_path, "talk_down")  # the existing add-on grants talk_down AND voice_call
    assert _feature(db_path, "voice_call") is True and _feature(db_path, "talk_down") is True
    import analytics_entitlements as analytics
    with override_target(sqlite_path=str(db_path)):
        assert "voice_call" in analytics.get_active_analytics_for_customer("cust-v2")


def test_a_legacy_plan_holder_with_voice_call_keeps_it(db_path, monkeypatch, prices):
    seed_customer(db_path)
    with override_target(sqlite_path=str(db_path)):
        from customer_entitlements import upsert_entitlement
        upsert_entitlement(customer_id="cust-v2", product="camera_slots_hybrid", camera_slot_quantity=8)
    _hold(db_path, "talk_down")
    assert _feature(db_path, "voice_call") is True


@pytest.mark.parametrize("plan_key", ["ai_local", "hybrid"])
def test_my_subscription_says_talk_down_is_included_and_never_that_voice_call_is(db_path, monkeypatch, prices, plan_key):
    _plan(db_path, monkeypatch, plan_key)
    html = _page(db_path)
    assert "Talk Down / two-way audio on supported cameras" in _included(html)
    included_section = re.search(r'id="v2-included">(.*?)</section>', html, re.S).group(1)
    assert "Voice Call" not in included_section
    assert 'data-addon-row="talk_down"' not in html  # the add-on would re-charge the included Talk Down


@pytest.mark.parametrize("plan_key", ["ai_local", "hybrid"])
def test_the_talk_down_add_on_is_not_sold_on_ai_plans_even_though_it_carries_voice_call(db_path, monkeypatch, owner_client, plan_key):
    client, calls = owner_client
    _plan(db_path, monkeypatch, plan_key)
    offer = _offer(db_path, "talk_down", plan_key)
    assert offer["state"] == "plan_overlap" and "AAC Voice Call is not sold on its own yet" in offer["reason"]
    assert _buy(client, "talk_down").status_code == 409
    assert not [c for c in calls if c[0] == "/v1/checkout/sessions"]


def test_basic_local_can_still_buy_the_talk_down_add_on_with_its_voice_call(db_path, monkeypatch, owner_client):
    client, calls = owner_client
    _plan(db_path, monkeypatch, "basic_local")
    rows = _addon_rows(_page(db_path))
    assert "Talk Down / two-way audio and AAC Voice Call" in rows["talk_down"] and 'data-addon-key="talk_down"' in rows["talk_down"]
    assert _buy(client, "talk_down").status_code == 200
