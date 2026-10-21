"""Requirement #11 (VMS 1.2.7): test-account cleanup tool (test_account_cleanup.py).

Throwaway SQLite databases with the real schema (partner_db.initialize_database + every migration). Covers the
classification (P/A/B/C), protected and appliance-linked refusals, stale/tampered plans, backup before write,
rollback on error, the tombstone releasing the email (a later purchase by that email is NOT attached to the closed
account), hard delete, idempotency and the audit rows. No network, no Stripe, no AWS.
"""
import hashlib
import json
import sqlite3
from datetime import timedelta

import pytest

import customer_entitlements
import test_account_cleanup as tac
from database_backend import override_target
from partner_db import initialize_database

NOW = "2026-10-01T00:00:00"
OWNER = "amata@anyaicam.com"
FOUNDER = "alexmata25@gmail.com"


def _sha(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _insert(conn, table, **values):
    keys = list(values)
    conn.execute(f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' * len(keys))})", [values[k] for k in keys])


def _customer(conn, cid, email, *, status="active"):
    _insert(conn, "customers", id=cid, partner_id="p", name="Test " + cid, email=email, status=status, created_at=NOW)


def _user(conn, uid, email, cid, *, role="customer_owner"):
    _insert(conn, "partner_users", id=uid, email=email, role=role, password_hash="pbkdf2_sha256$1$00$00",
            approved=1, customer_id=cid, created_at=NOW)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for name in ("ANYAICAM_PLATFORM_OWNER_EMAIL", "ANYAICAM_ADMIN_EMAIL", "ANYAICAM_FOUNDER_LPR_CUSTOMER_IDS"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "cleanup.db"
    with override_target(sqlite_path=path):
        initialize_database()
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        _insert(conn, "partners", id="p", name="P", approval_status="approved", created_at=NOW)
        # P: the owner admin account and the Founder
        _customer(conn, "cust-owner", OWNER)
        _user(conn, "user-owner", OWNER, "cust-owner")
        _customer(conn, "4efaf5153f00aa", FOUNDER)
        # A: login user, session, site, registration request only
        _customer(conn, "cust-plain", "plain.tester@example.test")
        _user(conn, "user-plain", "plain.tester@example.test", "cust-plain")
        _insert(conn, "user_sessions", id="sess-plain", user_id="user-plain", email="plain.tester@example.test",
                role="customer_owner", session_type="web", created_at=NOW, expires_at="2099-01-01")
        _insert(conn, "sites", id="site-plain", customer_id="cust-plain", name="Home", created_at=NOW)
        _insert(conn, "customer_registration_requests", id="reg-plain", display_name="Plain",
                email="plain.tester@example.test", password_hash="x", status="approved", customer_id="cust-plain",
                user_id="user-plain", requested_at=NOW)
        # B: a Stripe TEST-mode plan purchase
        _customer(conn, "cust-paid", "paid.tester@example.test")
        _user(conn, "user-paid", "paid.tester@example.test", "cust-paid")
        _insert(conn, "user_sessions", id="sess-paid", user_id="user-paid", email="paid.tester@example.test",
                role="customer_owner", session_type="web", created_at=NOW, expires_at="2099-01-01")
        _insert(conn, "customer_entitlements", id="ent-paid", customer_id="cust-paid", product="vms_license",
                camera_slot_quantity=4, status="active", stripe_checkout_session_id="cs_test_abc", created_at=NOW,
                updated_at=NOW)
        # C: an appliance
        _customer(conn, "cust-box", "box.owner@example.test")
        _insert(conn, "sites", id="site-box", customer_id="cust-box", name="Box", created_at=NOW)
        _insert(conn, "appliances", id="app-1", customer_id="cust-box", site_id="site-box", cloud_id="AIC-TEST0001",
                created_at=NOW)
        # staff user with no customer
        _user(conn, "user-staff", "tech@example.test", None, role="technician")
        conn.commit()
        conn.close()
        yield path


def _approved(*accounts, **extra):
    return {"format": 1, "approved_by": "owner", "approved_at": "2026-10-20", "accounts": list(accounts), **extra}


def _entry(cid, cls, email, **extra):
    return {"customer_id": cid, "expected_class": cls, "email_sha256": _sha(email), **extra}


def _db_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------- classification
def test_inventory_classifies_every_account_and_writes_nothing(env):
    before = _db_sha(env)
    with override_target(sqlite_path=env):
        inv = tac.build_inventory()
    assert _db_sha(env) == before
    classes = {c["customer_id"]: c["class"] for c in inv["customers"]}
    assert classes == {"cust-owner": "P", "4efaf5153f00aa": "P", "cust-plain": "A", "cust-paid": "B", "cust-box": "C"}
    assert inv["summary"]["by_class"] == {"A": 1, "B": 1, "C": 1, "P": 2}
    users = {u["id"]: u["class"] for u in inv["users"]}
    assert users["user-staff"] == "P" and users["user-plain"] == "A"
    founder = next(c for c in inv["customers"] if c["customer_id"] == "4efaf5153f00aa")
    assert any("Founder" in r for r in founder["reasons"])
    box = next(c for c in inv["customers"] if c["customer_id"] == "cust-box")
    assert box["dependencies"]["appliances.customer_id"] == 1
    text = json.dumps(inv)
    for email in (OWNER, FOUNDER, "plain.tester@example.test", "password_hash", "pbkdf2"):
        assert email not in text


def test_inventory_unmasks_only_an_approved_list_match(env):
    approved = tac.load_approved(_approved(_entry("cust-plain", "A", "plain.tester@example.test"),
                                           {"customer_id": "cust-paid", "expected_class": "B", "email_sha256": "0" * 64}))
    with override_target(sqlite_path=env):
        inv = tac.build_inventory(approved)
    shown = {c["customer_id"]: c["email"] for c in inv["customers"]}
    assert shown["cust-plain"] == "plain.tester@example.test"
    assert shown["cust-paid"].startswith("pa***@")  # wrong hash: stays masked
    assert shown["cust-owner"].startswith("am***@")


def test_inventory_never_creates_a_missing_database(tmp_path):
    missing = tmp_path / "nope.db"
    with override_target(sqlite_path=missing), pytest.raises(tac.Refused):
        tac.build_inventory()
    assert not missing.exists()


def test_platform_owner_and_founder_env_are_protected(env, monkeypatch):
    monkeypatch.setenv("ANYAICAM_PLATFORM_OWNER_EMAIL", "PLAIN.tester@example.test")
    monkeypatch.setenv("ANYAICAM_FOUNDER_LPR_CUSTOMER_IDS", "x,cust-paid")
    with override_target(sqlite_path=env):
        inv = tac.build_inventory()
    classes = {c["customer_id"]: c["class"] for c in inv["customers"]}
    assert classes["cust-plain"] == "P" and classes["cust-paid"] == "P"


# ---------------------------------------------------------------------------------------------- plan refusals
def _plan(env, *accounts, **kwargs):
    with override_target(sqlite_path=env):
        return tac.build_plan(_approved(*accounts), **kwargs)


@pytest.mark.parametrize("entry", [
    _entry("cust-owner", "B", OWNER),
    _entry("cust-owner", "A", OWNER),
    _entry("4efaf5153f00aa", "A", FOUNDER),
])
def test_plan_refuses_protected_owner_and_founder(env, entry):
    with pytest.raises(tac.Refused) as refused:
        _plan(env, entry)
    assert "PROTECTED" in " ".join(refused.value.details)


def test_plan_refuses_appliance_linked_without_flag_and_reason(env):
    entry = _entry("cust-box", "C", "box.owner@example.test")
    with pytest.raises(tac.Refused) as refused:
        _plan(env, entry)
    assert "appliance-linked" in " ".join(refused.value.details)
    with pytest.raises(tac.Refused) as refused:
        _plan(env, entry, allow_appliance_linked=True, appliance_reason="short")
    assert "reason" in " ".join(refused.value.details)
    plan = _plan(env, entry, allow_appliance_linked=True, appliance_reason="owner: Samsung bench box, retired")
    assert plan["entries"][0]["method"] == "tombstone"
    assert not any(s["table"] in ("appliances", "sites") for s in plan["entries"][0]["steps"])


def test_plan_refuses_class_mismatch_wrong_email_unknown_id_and_is_all_or_nothing(env, tmp_path):
    good = _entry("cust-plain", "A", "plain.tester@example.test")
    for bad, words in [(_entry("cust-paid", "A", "paid.tester@example.test"), "live class B"),
                       (_entry("cust-paid", "B", "someone.else@example.test"), "email hash"),
                       (_entry("cust-ghost", "A", "ghost@example.test"), "no such customer")]:
        with pytest.raises(tac.Refused) as refused:
            _plan(env, good, bad)
        assert words in " ".join(refused.value.details)
    out = tmp_path / "plan.json"
    with override_target(sqlite_path=env):
        assert tac.main(["plan", "--approved", str(_write(tmp_path / "a.json", _approved(
            good, _entry("cust-ghost", "A", "x@example.test")))), "--output", str(out)]) == 2
    assert not out.exists()


def test_plan_refuses_a_malformed_or_duplicate_list():
    entry = _entry("cust-plain", "A", "plain.tester@example.test")
    for data in [_approved(entry, entry), _approved({**entry, "extra": 1}), {"format": 1, "accounts": [entry]},
                 _approved(), _approved({**entry, "expected_class": "P"})]:
        with pytest.raises(tac.Refused):
            tac.load_approved(data)


def test_plan_refuses_billing_not_marked_test_and_live_evidence(env):
    conn = sqlite3.connect(env)
    conn.execute("UPDATE customer_entitlements SET stripe_checkout_session_id=NULL WHERE id='ent-paid'")
    conn.commit()
    entry = _entry("cust-paid", "B", "paid.tester@example.test")
    with pytest.raises(tac.Refused) as refused:
        _plan(env, entry)
    assert "customer_entitlements:ent-paid" in " ".join(refused.value.details)
    plan = _plan(env, {**entry, "test_billing_rows": ["customer_entitlements:ent-paid"]})  # owner attests it
    assert plan["entries"][0]["attested_test_billing_rows"] == ["customer_entitlements:ent-paid"]
    conn.execute("UPDATE customer_entitlements SET stripe_checkout_session_id='cs_live_real' WHERE id='ent-paid'")
    conn.commit()
    conn.close()
    with pytest.raises(tac.Refused) as refused:  # live evidence: an attestation never overrides it
        _plan(env, {**entry, "test_billing_rows": ["customer_entitlements:ent-paid"]})
    assert "LIVE Stripe" in " ".join(refused.value.details)


def test_plan_refuses_a_live_mode_stripe_binding(env):
    conn = sqlite3.connect(env)
    _insert(conn, "stripe_customer_mode_bindings", customer_id="cust-paid", livemode=1, stripe_customer_id="cus_live",
            created_at=NOW)
    conn.commit()
    conn.close()
    with pytest.raises(tac.Refused) as refused:
        _plan(env, _entry("cust-paid", "B", "paid.tester@example.test"))
    assert "LIVE Stripe" in " ".join(refused.value.details)


def test_plan_lists_exact_row_changes_and_writes_nothing(env):
    before = _db_sha(env)
    plan = _plan(env, _entry("cust-plain", "A", "plain.tester@example.test"),
                 _entry("cust-paid", "B", "paid.tester@example.test"))
    assert _db_sha(env) == before and plan["dry_run"] is True
    steps = {e["customer_id"]: [(s["op"], s["table"], s["rows"]) for s in e["steps"]] for e in plan["entries"]}
    assert steps["cust-plain"] == [("delete", "user_sessions", 1), ("delete", "customer_registration_requests", 1),
                                   ("delete", "partner_users", 1), ("delete", "sites", 1), ("delete", "customers", 1)]
    assert ("update", "customers", 1) in steps["cust-paid"] and ("update", "user_sessions", 1) in steps["cust-paid"]
    assert not any(t in ("customer_entitlements",) for _, t, _ in steps["cust-paid"])
    assert plan["confirm_token"] == "APPLY-" + plan["plan_sha256"][:16]
    assert "paid.tester@example.test" not in json.dumps(plan)
    assert "sess-plain" not in json.dumps(plan)  # a session id is a bearer value: shown only as a hash


# ---------------------------------------------------------------------------------------------- apply
def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _apply(env, plan, tmp_path, **kwargs):
    kwargs.setdefault("confirm", plan["confirm_token"])
    kwargs.setdefault("actor", "owner-run@example.test")
    kwargs.setdefault("backup_dir", tmp_path / "backups")
    with override_target(sqlite_path=env):
        return tac.apply_plan(plan, **kwargs)


def _both(env):
    return _plan(env, _entry("cust-plain", "A", "plain.tester@example.test"),
                 _entry("cust-paid", "B", "paid.tester@example.test"))


def test_apply_refuses_stale_tampered_wrong_token_or_changed_data(env, tmp_path):
    plan = _both(env)
    before = _db_sha(env)
    created = tac.datetime.fromisoformat(plan["created_at"])
    with pytest.raises(tac.Refused, match="stale"):
        _apply(env, plan, tmp_path, now=created + timedelta(minutes=31))
    with pytest.raises(tac.Refused, match="confirm token"):
        _apply(env, plan, tmp_path, confirm="APPLY-0000000000000000")
    tampered = json.loads(json.dumps(plan))
    tampered["entries"][0]["steps"][0]["rows"] = 99
    with pytest.raises(tac.Refused, match="modified"):
        _apply(env, tampered, tmp_path)
    conn = sqlite3.connect(env)  # the account signs in again after the plan was reviewed
    _insert(conn, "user_sessions", id="sess-new", user_id="user-paid", email="paid.tester@example.test",
            role="customer_owner", session_type="web", created_at=NOW, expires_at="2099-01-01")
    conn.commit()
    conn.close()
    before = _db_sha(env)
    with pytest.raises(tac.Refused, match="stale plan"):
        _apply(env, plan, tmp_path)
    assert _db_sha(env) == before
    assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").iterdir())


def test_apply_requires_a_backup_and_makes_a_verified_one_first(env, tmp_path):
    plan = _both(env)
    before = _db_sha(env)
    with pytest.raises(tac.Refused, match="backup-dir"):
        _apply(env, plan, tmp_path, backup_dir=None)
    assert _db_sha(env) == before
    result = _apply(env, plan, tmp_path)
    backup = tac.Path(result["backup"]["path"])
    assert backup.is_file() and result["backup"]["integrity_check"] == "ok"
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == result["backup"]["sha256"]
    # the backup holds the pre-change state
    assert _rows(backup, "SELECT email,status FROM customers WHERE id='cust-paid'") == [
        {"email": "paid.tester@example.test", "status": "active"}]
    assert _rows(backup, "SELECT id FROM customers WHERE id='cust-plain'") == [{"id": "cust-plain"}]


def test_postgresql_apply_refuses_without_a_verified_pg_dump(env, tmp_path, monkeypatch):
    plan = _both(env)
    monkeypatch.setenv("ANYAICAM_DATABASE_BACKEND", "postgresql")
    with pytest.raises(tac.Refused, match="pg_dump"):
        tac.apply_plan(plan, confirm=plan["confirm_token"], actor="owner", backup_dir=tmp_path)
    dump = tmp_path / "old.dump"
    dump.write_bytes(b"PGDMP")
    created = tac.datetime.fromisoformat(plan["created_at"])
    import os
    os.utime(dump, (created.timestamp() - 3600, created.timestamp() - 3600))
    with pytest.raises(tac.Refused, match="older than the plan"):
        tac._pg_backup(dump, created)
    os.utime(dump, None)
    assert tac._pg_backup(dump, created)["sha256"] == hashlib.sha256(b"PGDMP").hexdigest()


def test_apply_rolls_everything_back_on_error(env, tmp_path, monkeypatch):
    plan = _both(env)
    before = _rows(env, "SELECT * FROM customers ORDER BY id"), _rows(env, "SELECT * FROM partner_users ORDER BY id")
    real = tac.execute_step
    calls = []

    def failing(db, step, now_iso):
        calls.append(step["table"])
        if len(calls) == 4:
            raise RuntimeError("disk full (simulated)")
        return real(db, step, now_iso)

    monkeypatch.setattr(tac, "execute_step", failing)
    with pytest.raises(RuntimeError, match="simulated"):
        _apply(env, plan, tmp_path)
    after = _rows(env, "SELECT * FROM customers ORDER BY id"), _rows(env, "SELECT * FROM partner_users ORDER BY id")
    assert after == before and len(calls) == 4
    assert _rows(env, "SELECT COUNT(*) AS n FROM test_account_closures") == [{"n": 0}]
    assert _rows(env, "SELECT COUNT(*) AS n FROM audit_logs WHERE action LIKE 'test_account_cleanup%'") == [{"n": 0}]


def test_a_post_check_failure_also_rolls_back(env, tmp_path, monkeypatch):
    plan = _both(env)
    before = _db_rows_snapshot(env)

    def broken(*args, **kwargs):
        raise RuntimeError("an account outside the plan changed (simulated)")

    monkeypatch.setattr(tac, "_verify_outcome", broken)
    with pytest.raises(RuntimeError):
        _apply(env, plan, tmp_path)
    assert _db_rows_snapshot(env) == before


def _db_rows_snapshot(path):
    return {t: _rows(path, f"SELECT * FROM {t} ORDER BY rowid") for t in
            ("customers", "partner_users", "user_sessions", "sites", "customer_registration_requests", "audit_logs",
             "test_account_closures")}


def test_tombstone_releases_the_email_so_a_purchase_is_not_attached(env, tmp_path, monkeypatch):
    plan = _plan(env, _entry("cust-paid", "B", "paid.tester@example.test"))
    result = _apply(env, plan, tmp_path)
    assert result["status"] == "applied"
    customer = _rows(env, "SELECT email,status FROM customers WHERE id='cust-paid'")[0]
    assert customer["status"] == "closed_test" and customer["email"].endswith("@tombstone.invalid")
    user = _rows(env, "SELECT email,account_status,approved,password_hash FROM partner_users WHERE id='user-paid'")[0]
    assert user["account_status"] == "revoked" and user["approved"] == 0 and user["email"].endswith("@tombstone.invalid")
    assert _rows(env, "SELECT revoked_at FROM user_sessions WHERE id='sess-paid'")[0]["revoked_at"]
    # billing history is kept unchanged
    assert _rows(env, "SELECT status,camera_slot_quantity FROM customer_entitlements WHERE id='ent-paid'") == [
        {"status": "active", "camera_slot_quantity": 4}]
    # nothing holds the email any more, even for a lookup with no status filter
    for table in ("customers", "partner_users", "customer_registration_requests"):
        assert _rows(env, f"SELECT 1 FROM {table} WHERE lower(email)='paid.tester@example.test'") == []
    with override_target(sqlite_path=env):
        assert customer_entitlements.find_customer_by_email("paid.tester@example.test") is None
        import direct_onboarding
        conn = sqlite3.connect(env)
        assert not direct_onboarding._email_in_use(conn, "paid.tester@example.test")
        conn.close()
        from partner_db import authenticate_detailed
        assert authenticate_detailed("paid.tester@example.test", "anything")[0] is None
        # a real purchase by that email (guest checkout, no authoritative id) is NOT attached to the closed account
        monkeypatch.setitem(customer_entitlements.PRICE_ID_VMS_LICENSE_MAP, "price_req11",
                            {"product": "vms_license", "camera_slot_maximum": 8, "billing_type": "one_time"})
        outcome = customer_entitlements.sync_entitlement_from_stripe_event({
            "id": "evt_req11", "type": "checkout.session.completed", "livemode": False,
            "data": {"object": {"id": "cs_test_req11", "mode": "payment", "payment_status": "paid", "amount_total": 0,
                                "customer_email": "paid.tester@example.test",
                                "metadata": {"anyaicam_stripe_price_id": "price_req11"}}}})
    assert outcome["status"] == "pending_link_created", outcome
    assert _rows(env, "SELECT camera_slot_quantity FROM customer_entitlements WHERE customer_id='cust-paid'") == [
        {"camera_slot_quantity": 4}]


def test_hard_delete_removes_only_the_class_a_account(env, tmp_path):
    others = {t: _rows(env, f"SELECT * FROM {t} WHERE customer_id<>'cust-plain' ORDER BY rowid")
              for t in ("partner_users", "sites", "appliances", "customer_entitlements")}
    plan = _plan(env, _entry("cust-plain", "A", "plain.tester@example.test"))
    result = _apply(env, plan, tmp_path)
    assert result["accounts"] == [{"customer_id": "cust-plain", "class": "A", "method": "delete", "rows_changed": 5}]
    for table, column in (("customers", "id"), ("partner_users", "customer_id"), ("sites", "customer_id"),
                          ("customer_registration_requests", "customer_id")):
        assert _rows(env, f"SELECT 1 FROM {table} WHERE {column}='cust-plain'") == []
    assert _rows(env, "SELECT 1 FROM user_sessions WHERE user_id='user-plain'") == []
    assert _rows(env, "PRAGMA foreign_key_check") == []
    assert others == {t: _rows(env, f"SELECT * FROM {t} WHERE customer_id<>'cust-plain' ORDER BY rowid")
                      for t in others}
    with override_target(sqlite_path=env):
        import direct_onboarding
        conn = sqlite3.connect(env)
        assert not direct_onboarding._email_in_use(conn, "plain.tester@example.test")
        conn.close()


def test_apply_is_idempotent_and_writes_audit_rows(env, tmp_path):
    plan = _both(env)
    first = _apply(env, plan, tmp_path)
    assert first["status"] == "applied"
    audit = _rows(env, "SELECT actor_email,actor_role,action,entity_id,details_json FROM audit_logs "
                       "WHERE action LIKE 'test_account_cleanup%' ORDER BY id")
    assert [(a["action"], a["entity_id"]) for a in audit] == [
        ("test_account_cleanup.delete", "cust-plain"), ("test_account_cleanup.tombstone", "cust-paid"),
        ("test_account_cleanup.apply", plan["plan_sha256"])]
    assert all(a["actor_email"] == "owner-run@example.test" and a["actor_role"] == tac.ACTOR_ROLE for a in audit)
    details = json.loads(audit[1]["details_json"])
    assert details["plan_sha256"] == plan["plan_sha256"] and details["email_sha256"] == _sha("paid.tester@example.test")
    assert details["backup_sha256"] == first["backup"]["sha256"]
    assert "paid.tester@example.test" not in json.dumps(audit)
    closures = _rows(env, "SELECT customer_id,cleanup_class,method,closed_by FROM test_account_closures ORDER BY customer_id")
    assert closures == [{"customer_id": "cust-paid", "cleanup_class": "B", "method": "tombstone", "closed_by": "owner-run@example.test"},
                        {"customer_id": "cust-plain", "cleanup_class": "A", "method": "delete", "closed_by": "owner-run@example.test"}]
    snapshot = _db_rows_snapshot(env)
    again = _apply(env, plan, tmp_path)
    assert again["status"] == "already_applied" and again["changes"] == 0
    assert _db_rows_snapshot(env) == snapshot
    # and a new plan for a closed account is refused
    with pytest.raises(tac.Refused) as refused:
        _plan(env, _entry("cust-paid", "B", "paid.tester@example.test"))
    assert "already closed" in " ".join(refused.value.details) or "differs" in " ".join(refused.value.details)


def test_cli_end_to_end_dry_run_by_default(env, tmp_path, capsys):
    approved = _write(tmp_path / "approved.json", _approved(_entry("cust-plain", "A", "plain.tester@example.test")))
    plan_file = tmp_path / "plan.json"
    before = _db_sha(env)
    assert tac.main(["--sqlite-path", str(env), "inventory"]) == 0
    assert tac.main(["--sqlite-path", str(env), "plan", "--approved", str(approved), "--output", str(plan_file)]) == 0
    assert _db_sha(env) == before  # inventory and plan never write
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    capsys.readouterr()
    assert tac.main(["--sqlite-path", str(env), "apply", "--plan", str(plan_file), "--confirm", "nope",
                     "--actor", "owner", "--backup-dir", str(tmp_path / "b")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    assert _db_sha(env) == before
    assert tac.main(["--sqlite-path", str(env), "apply", "--plan", str(plan_file), "--confirm", plan["confirm_token"],
                     "--actor", "owner", "--backup-dir", str(tmp_path / "b")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"


def test_migration_adds_the_closures_table_idempotently(env):
    from db_migrations import apply_migrations
    with override_target(sqlite_path=env):
        apply_migrations()
        apply_migrations()
    columns = [r["name"] for r in _rows(env, "PRAGMA table_info(test_account_closures)")]
    assert columns[:3] == ["customer_id", "cleanup_class", "method"]
    assert _rows(env, "SELECT version FROM schema_migrations WHERE version='20261020_test_account_closures'")
