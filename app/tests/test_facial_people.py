"""AAC enrollment/watchlist DB service -- Phase 1.

Mirrors test_camera_people_counting_entitlement.py's own established
pattern: override_target(sqlite_path=...) before import, partner_db.
initialize_database() (which also runs db_migrations.apply_migrations(),
creating the new facial_* tables), then real SQLite-backed assertions.
No mocked database anywhere in this file -- every assertion below is
against a real, migrated SQLite database, and only synthetic
embeddings (plain tuples of floats) stand in for real face data.
"""

import pytest

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_facial_people_import.db"):
    import facial_people
    from partner_db import connection, initialize_database


NOW = "2026-09-08T00:00:00"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_facial_people.db"


@pytest.fixture()
def db(db_path):
    with override_target(sqlite_path=str(db_path)):
        initialize_database()
        with connection() as conn:
            conn.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-1','p1','C1','c1@example.test','active','real',?)", (NOW,))
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-2','p1','C2','c2@example.test','active','real',?)", (NOW,))
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Site 1',?)", (NOW,))
        with connection() as conn:
            yield conn


# --------------------------------------------------------------- enrollment


def test_enroll_person_creates_active_person(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    person = facial_people.get_person(db, customer_id="cust-1", person_id=person_id)
    assert person["display_name"] == "Alice"
    assert person["status"] == "active"


def test_enroll_person_requires_display_name(db):
    with pytest.raises(ValueError):
        facial_people.enroll_person(db, customer_id="cust-1", display_name="   ", now=NOW)


def test_enroll_person_stores_external_reference_and_notes(db):
    person_id = facial_people.enroll_person(
        db, customer_id="cust-1", display_name="Bob", external_reference="EMP-42", notes="Warehouse staff", now=NOW
    )
    person = facial_people.get_person(db, customer_id="cust-1", person_id=person_id)
    assert person["external_reference"] == "EMP-42"
    assert person["notes"] == "Warehouse staff"


def test_get_person_is_none_for_wrong_customer(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    assert facial_people.get_person(db, customer_id="cust-2", person_id=person_id) is None


def test_list_people_scopes_to_customer(db):
    facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.enroll_person(db, customer_id="cust-2", display_name="Zed", now=NOW)
    people = facial_people.list_people(db, customer_id="cust-1")
    assert [p["display_name"] for p in people] == ["Alice"]


def test_list_people_search_matches_name(db):
    facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice Anderson", now=NOW)
    facial_people.enroll_person(db, customer_id="cust-1", display_name="Bob Brown", now=NOW)
    people = facial_people.list_people(db, customer_id="cust-1", search="Anderson")
    assert len(people) == 1
    assert people[0]["display_name"] == "Alice Anderson"


def test_update_person_changes_display_name(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    assert facial_people.update_person(db, customer_id="cust-1", person_id=person_id, display_name="Alicia", now=NOW)
    assert facial_people.get_person(db, customer_id="cust-1", person_id=person_id)["display_name"] == "Alicia"


def test_update_person_can_disable_status(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.update_person(db, customer_id="cust-1", person_id=person_id, status="disabled", now=NOW)
    assert facial_people.get_person(db, customer_id="cust-1", person_id=person_id)["status"] == "disabled"


def test_update_person_rejects_unknown_status(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    with pytest.raises(ValueError):
        facial_people.update_person(db, customer_id="cust-1", person_id=person_id, status="deleted", now=NOW)


def test_update_person_returns_false_for_wrong_customer(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    assert facial_people.update_person(db, customer_id="cust-2", person_id=person_id, display_name="Eve", now=NOW) is False


# --------------------------------------------------------------- reference images / embeddings


def test_add_reference_image_creates_embedding(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    embedding_id = facial_people.add_reference_image(
        db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW
    )
    assert embedding_id is not None
    images = facial_people.list_reference_images(db, customer_id="cust-1", person_id=person_id)
    assert len(images) == 1


def test_multiple_reference_images_per_person(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(0.0, 1.0), engine="haar_intensity", engine_version="1", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(0.5, 0.5), engine="haar_intensity", engine_version="1", now=NOW)
    images = facial_people.list_reference_images(db, customer_id="cust-1", person_id=person_id)
    assert len(images) == 3


def test_add_reference_image_returns_none_for_wrong_customer(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    result = facial_people.add_reference_image(
        db, customer_id="cust-2", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW
    )
    assert result is None


def test_delete_reference_image_removes_one_embedding(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    keep = facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    remove = facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(0.0, 1.0), engine="haar_intensity", engine_version="1", now=NOW)
    assert facial_people.delete_reference_image(db, customer_id="cust-1", person_id=person_id, embedding_id=remove)
    remaining = facial_people.list_reference_images(db, customer_id="cust-1", person_id=person_id)
    assert [image["id"] for image in remaining] == [keep]


def test_enrolled_embeddings_for_matching_excludes_disabled_people(db):
    active = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    disabled = facial_people.enroll_person(db, customer_id="cust-1", display_name="Bob", now=NOW)
    facial_people.update_person(db, customer_id="cust-1", person_id=disabled, status="disabled", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=active, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=disabled, embedding=(0.0, 1.0), engine="haar_intensity", engine_version="1", now=NOW)
    enrolled = facial_people.enrolled_embeddings_for_matching(db, customer_id="cust-1", engine="haar_intensity")
    assert [e.person_id for e in enrolled] == [active]


def test_enrolled_embeddings_for_matching_never_crosses_tenants(db):
    """The core tenant-isolation guarantee for the live matching path:
    cust-2's enrolled embeddings must never appear when matching is
    scoped to cust-1, even though both customers exist in the very
    same database."""
    person_1 = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    person_2 = facial_people.enroll_person(db, customer_id="cust-2", display_name="Mallory", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_1, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-2", person_id=person_2, embedding=(0.0, 1.0), engine="haar_intensity", engine_version="1", now=NOW)
    enrolled = facial_people.enrolled_embeddings_for_matching(db, customer_id="cust-1", engine="haar_intensity")
    assert [e.person_id for e in enrolled] == [person_1]


# --------------------------------------------------------------- deletion / retention


def test_delete_person_hard_deletes_embeddings(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_reference_image(db, customer_id="cust-1", person_id=person_id, embedding=(1.0, 0.0), engine="haar_intensity", engine_version="1", now=NOW)
    assert facial_people.delete_person(db, customer_id="cust-1", person_id=person_id)
    row = db.execute("SELECT COUNT(*) AS n FROM facial_embeddings WHERE person_id=?", (person_id,)).fetchone()
    assert row["n"] == 0


def test_delete_person_removes_the_person_row(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.delete_person(db, customer_id="cust-1", person_id=person_id)
    assert facial_people.get_person(db, customer_id="cust-1", person_id=person_id) is None


def test_delete_person_removes_watchlist_membership(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    watchlist_id = facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", now=NOW)
    facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist_id, person_id=person_id, now=NOW)
    facial_people.delete_person(db, customer_id="cust-1", person_id=person_id)
    members = facial_people.list_watchlist_members(db, customer_id="cust-1", watchlist_id=watchlist_id)
    assert members == []


def test_delete_person_is_idempotent_false_on_second_call(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    assert facial_people.delete_person(db, customer_id="cust-1", person_id=person_id) is True
    assert facial_people.delete_person(db, customer_id="cust-1", person_id=person_id) is False


def test_delete_person_wrong_customer_does_not_delete(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    assert facial_people.delete_person(db, customer_id="cust-2", person_id=person_id) is False
    assert facial_people.get_person(db, customer_id="cust-1", person_id=person_id) is not None


# --------------------------------------------------------------- watchlists


def test_create_watchlist_defaults_to_alert_classification(db):
    watchlist_id = facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", now=NOW)
    watchlist = facial_people.get_watchlist(db, customer_id="cust-1", watchlist_id=watchlist_id)
    assert watchlist["classification"] == "alert"


def test_create_watchlist_rejects_unknown_classification(db):
    with pytest.raises(ValueError):
        facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", classification="bogus", now=NOW)


def test_list_watchlists_scopes_to_customer(db):
    facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", now=NOW)
    facial_people.create_watchlist(db, customer_id="cust-2", name="VIP", now=NOW)
    watchlists = facial_people.list_watchlists(db, customer_id="cust-1")
    assert [w["name"] for w in watchlists] == ["Banned"]


def test_add_watchlist_member_rejects_cross_tenant_person(db):
    watchlist_id = facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", now=NOW)
    foreign_person = facial_people.enroll_person(db, customer_id="cust-2", display_name="Mallory", now=NOW)
    added = facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist_id, person_id=foreign_person, now=NOW)
    assert added is False
    assert facial_people.list_watchlist_members(db, customer_id="cust-1", watchlist_id=watchlist_id) == []


def test_add_watchlist_member_rejects_cross_tenant_watchlist(db):
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    foreign_watchlist = facial_people.create_watchlist(db, customer_id="cust-2", name="VIP", now=NOW)
    added = facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=foreign_watchlist, person_id=person_id, now=NOW)
    assert added is False


def test_add_watchlist_member_is_idempotent(db):
    watchlist_id = facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", now=NOW)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    assert facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist_id, person_id=person_id, now=NOW)
    assert facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist_id, person_id=person_id, now=NOW)
    members = facial_people.list_watchlist_members(db, customer_id="cust-1", watchlist_id=watchlist_id)
    assert len(members) == 1


def test_remove_watchlist_member(db):
    watchlist_id = facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", now=NOW)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist_id, person_id=person_id, now=NOW)
    facial_people.remove_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist_id, person_id=person_id)
    assert facial_people.list_watchlist_members(db, customer_id="cust-1", watchlist_id=watchlist_id) == []


def test_watchlisted_person_ids_scopes_to_customer(db):
    watchlist_id = facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", now=NOW)
    person_1 = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    person_2 = facial_people.enroll_person(db, customer_id="cust-2", display_name="Mallory", now=NOW)
    facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist_id, person_id=person_1, now=NOW)
    ids = facial_people.watchlisted_person_ids(db, customer_id="cust-1")
    assert ids == frozenset({person_1})
    assert person_2 not in ids


def test_delete_watchlist_removes_memberships_not_people(db):
    watchlist_id = facial_people.create_watchlist(db, customer_id="cust-1", name="Banned", now=NOW)
    person_id = facial_people.enroll_person(db, customer_id="cust-1", display_name="Alice", now=NOW)
    facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist_id, person_id=person_id, now=NOW)
    facial_people.delete_watchlist(db, customer_id="cust-1", watchlist_id=watchlist_id)
    assert facial_people.get_person(db, customer_id="cust-1", person_id=person_id) is not None
    assert facial_people.get_watchlist(db, customer_id="cust-1", watchlist_id=watchlist_id) is None


# --------------------------------------------------------------- settings


def test_get_settings_returns_defaults_when_unset(db):
    settings = facial_people.get_settings(db, customer_id="cust-1")
    assert settings["min_confidence"] == 0.6
    assert settings["unknown_person_events_enabled"] is True
    assert settings["debounce_seconds"] == 30


def test_update_settings_persists_changes(db):
    facial_people.update_settings(db, customer_id="cust-1", min_confidence=0.8, now=NOW)
    settings = facial_people.get_settings(db, customer_id="cust-1")
    assert settings["min_confidence"] == 0.8


def test_update_settings_partial_update_keeps_other_fields(db):
    facial_people.update_settings(db, customer_id="cust-1", min_confidence=0.8, now=NOW)
    facial_people.update_settings(db, customer_id="cust-1", debounce_seconds=60, now=NOW)
    settings = facial_people.get_settings(db, customer_id="cust-1")
    assert settings["min_confidence"] == 0.8
    assert settings["debounce_seconds"] == 60


def test_settings_are_scoped_per_customer(db):
    facial_people.update_settings(db, customer_id="cust-1", min_confidence=0.9, now=NOW)
    other = facial_people.get_settings(db, customer_id="cust-2")
    assert other["min_confidence"] == 0.6  # cust-2 unaffected, still the default
