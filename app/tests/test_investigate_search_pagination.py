"""Regression coverage for Investigate's query-driven, server-paginated
search (2026-09-23 product decision).

Root cause this replaces: Investigate's initial page load embedded
every event _customer_investigate_events() returned (bounded to
<=2500, but still all embedded in one response) into the rendered
page, then filtered entirely client-side. Confirmed live on
portal-staging: a real customer's Investigate page embedded 2270
<article> cards -- 3.1MB of HTML, 34k+ DOM nodes -- in a single page
load. _customer_investigate_search() (main.py) replaces this: the
page's initial load now embeds only a small, recent default page
(INVESTIGATE_DEFAULT_EMBED_LIMIT), and every filter (event type,
camera, free-text, date range) plus every further page ("Load more")
is applied in SQL and fetched through GET /api/customer/investigate/
search, never embedded wholesale. Nothing about the underlying
detection_events data changes -- only what a single request queries,
embeds, and returns.

Same fixture idiom as test_customer_investigate_and_alerts.py /
test_investigate_reliability.py: imports `main` (Windows-native Python
only), every test redirects to a throwaway sqlite file via
override_target() before seeding or querying anything.

Camera-count-agnostic: fleets of 2, 6, and 23 cameras (none matching
the 5-camera Ryzen pilot) are exercised across pagination/filter
coverage.
"""

import sqlite3

import pytest

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_investigate_search_pagination.db"


class _FakeRequest:
    pass


def _owner_identity(customer_id="cust-1"):
    return {"role": "customer_owner", "customer_id": customer_id, "email": "owner@example.com"}


def _viewer_identity(customer_id, email):
    return {"role": "customer_viewer", "customer_id": customer_id, "email": email}


def _seed_tenant(conn, customer_id="cust-1", site_id="site-1", appliance_id="appl-1", cloud_id="AIC-TEST"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, "partner-1", f"Customer {customer_id}", f"{customer_id}@example.com", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Main", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)",
        (appliance_id, customer_id, site_id, cloud_id, "2026-01-01"),
    )


def _seed_camera(conn, camera_id, customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_number=1, name="Front Door"):
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, site_id, appliance_id, camera_number, name, "2026-01-01"),
    )


def _seed_event(conn, event_id, camera_id, event_type, timestamp, *, customer_id="cust-1", site_id="site-1", appliance_id="appl-1"):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,"
        "event_type,confidence,object_count,detections_json,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, customer_id, site_id, appliance_id, camera_id, f"local-{event_id}",
         event_type, 0.9, 1, None, timestamp, "2026-01-01T00:00:00"),
    )


def _seed_fleet_with_events(conn, customer_id, camera_count, events_per_camera, *, event_type="motion"):
    """customer_id's own camera_count-camera fleet, events_per_camera
    events on each camera, each with a distinguishable timestamp so
    ordering/pagination can be asserted precisely."""
    _seed_tenant(conn, customer_id, f"site-{customer_id}", f"appl-{customer_id}", f"AIC-{customer_id}")
    total = 0
    for cam_index in range(1, camera_count + 1):
        camera_id = f"{customer_id}-cam-{cam_index}"
        _seed_camera(conn, camera_id, customer_id, f"site-{customer_id}", f"appl-{customer_id}", cam_index, f"Camera {cam_index}")
        for event_index in range(events_per_camera):
            total += 1
            _seed_event(
                conn, f"{customer_id}-evt-{total:04d}", camera_id, event_type,
                f"2026-08-{(total % 27) + 1:02d}T{(total % 23):02d}:{(total % 59):02d}:00",
                customer_id=customer_id, site_id=f"site-{customer_id}", appliance_id=f"appl-{customer_id}",
            )
    return total


# =============================================================== initial page embed is bounded, not a bulk dump


@pytest.mark.parametrize("camera_count,events_per_camera", [(2, 40), (6, 20)])
def test_default_embed_is_bounded_to_the_default_limit_not_everything(monkeypatch, db_path, camera_count, events_per_camera):
    monkeypatch.setattr(main, "INVESTIGATE_DEFAULT_EMBED_LIMIT", 25)
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        total_seeded = _seed_fleet_with_events(conn, "cust-1", camera_count, events_per_camera)
        conn.commit()
        assert total_seeded > 25, "test setup must seed more than the bounded default to be meaningful"
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main._customer_investigate_search(_FakeRequest(), limit=25, offset=0)
    assert result is not None
    assert len(result["events"]) == 25
    assert result["total"] == total_seeded
    assert result["has_more"] is True


def test_render_customer_investigate_embeds_only_the_default_limit(monkeypatch, db_path):
    monkeypatch.setattr(main, "INVESTIGATE_DEFAULT_EMBED_LIMIT", 10)
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_fleet_with_events(conn, "cust-1", 2, 20)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main.investigation_page(_FakeRequest())
    assert result.count('"id":') == 10  # 10 event objects embedded in the loadedEvents JSON, not 40
    assert "let currentTotal=40;" in result  # true total (40) still reported even though only 10 are embedded
    assert "let currentHasMore=true;" in result


# =============================================================== pagination: offset/limit/has_more/total are all correct


def test_pagination_walks_every_event_exactly_once_across_pages(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        total_seeded = _seed_fleet_with_events(conn, "cust-1", 23, 3)  # 69 events, unusual fleet size
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))

        seen_ids = set()
        offset = 0
        page_size = 20
        pages_fetched = 0
        while True:
            page = main._customer_investigate_search(_FakeRequest(), limit=page_size, offset=offset)
            pages_fetched += 1
            assert pages_fetched < 20, "pagination did not terminate -- has_more/offset bookkeeping is broken"
            for event in page["events"]:
                assert event["id"] not in seen_ids, "the same event was returned on two different pages"
                seen_ids.add(event["id"])
            offset += len(page["events"])
            if not page["has_more"]:
                assert page["events"] == [] or len(page["events"]) < page_size or offset >= page["total"]
                break
    assert len(seen_ids) == total_seeded == 69


def test_has_more_is_false_exactly_when_every_matching_row_was_returned(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_fleet_with_events(conn, "cust-1", 2, 5)  # 10 events
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        exact_page = main._customer_investigate_search(_FakeRequest(), limit=10, offset=0)
        short_page = main._customer_investigate_search(_FakeRequest(), limit=11, offset=0)
        partial_page = main._customer_investigate_search(_FakeRequest(), limit=9, offset=0)
    assert exact_page["has_more"] is False
    assert short_page["has_more"] is False
    assert partial_page["has_more"] is True


# =============================================================== filters run in SQL, not just client-side


def test_event_type_filter_matches_exactly(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_event(conn, "evt-person", "cam-1", "person", "2026-08-01T00:00:00")
        _seed_event(conn, "evt-motion", "cam-1", "motion", "2026-08-01T00:01:00")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main._customer_investigate_search(_FakeRequest(), event_type="person", limit=50, offset=0)
    assert {event["id"] for event in result["events"]} == {"evt-person"}
    assert result["total"] == 1


def test_vehicle_filter_widens_to_every_vehicle_subtype(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_event(conn, "evt-car", "cam-1", "car", "2026-08-01T00:00:00")
        _seed_event(conn, "evt-truck", "cam-1", "truck", "2026-08-01T00:01:00")
        _seed_event(conn, "evt-person", "cam-1", "person", "2026-08-01T00:02:00")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main._customer_investigate_search(_FakeRequest(), event_type="vehicle", limit=50, offset=0)
    assert {event["id"] for event in result["events"]} == {"evt-car", "evt-truck"}


def test_camera_filter_matches_only_that_camera(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-a", camera_number=1, name="Camera A")
        _seed_camera(conn, "cam-b", camera_number=2, name="Camera B")
        _seed_event(conn, "evt-a", "cam-a", "motion", "2026-08-01T00:00:00")
        _seed_event(conn, "evt-b", "cam-b", "motion", "2026-08-01T00:01:00")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main._customer_investigate_search(_FakeRequest(), camera_id="cam-a", limit=50, offset=0)
    assert {event["id"] for event in result["events"]} == {"evt-a"}


def test_date_range_filter_excludes_events_outside_the_window(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-1")
        _seed_event(conn, "evt-early", "cam-1", "motion", "2026-08-01T00:00:00")
        _seed_event(conn, "evt-inside", "cam-1", "motion", "2026-08-05T00:00:00")
        _seed_event(conn, "evt-late", "cam-1", "motion", "2026-08-10T00:00:00")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main._customer_investigate_search(
            _FakeRequest(), from_ts="2026-08-03T00:00:00", to_ts="2026-08-07T00:00:00", limit=50, offset=0,
        )
    assert {event["id"] for event in result["events"]} == {"evt-inside"}


def test_free_text_query_matches_event_type_and_camera_name(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-drive", camera_number=1, name="Driveway")
        _seed_camera(conn, "cam-bed", camera_number=2, name="Bedroom")
        _seed_event(conn, "evt-truck-drive", "cam-drive", "truck", "2026-08-01T00:00:00")
        _seed_event(conn, "evt-truck-bed", "cam-bed", "truck", "2026-08-01T00:01:00")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main._customer_investigate_search(_FakeRequest(), query_text="truck driveway", limit=50, offset=0)
    assert {event["id"] for event in result["events"]} == {"evt-truck-drive"}


def test_combined_filters_are_all_applied_together(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-a", camera_number=1, name="Camera A")
        _seed_camera(conn, "cam-b", camera_number=2, name="Camera B")
        _seed_event(conn, "match", "cam-a", "car", "2026-08-05T00:00:00")
        _seed_event(conn, "wrong-camera", "cam-b", "car", "2026-08-05T00:00:00")
        _seed_event(conn, "wrong-type", "cam-a", "person", "2026-08-05T00:00:00")
        _seed_event(conn, "wrong-date", "cam-a", "car", "2026-09-01T00:00:00")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        result = main._customer_investigate_search(
            _FakeRequest(), event_type="car", camera_id="cam-a",
            from_ts="2026-08-01T00:00:00", to_ts="2026-08-31T00:00:00", limit=50, offset=0,
        )
    assert {event["id"] for event in result["events"]} == {"match"}


# =============================================================== authorization: tenant isolation, viewer camera scoping, identity contract


def test_never_leaks_another_customers_events_across_pages(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_fleet_with_events(conn, "cust-mine", 2, 10)
        _seed_fleet_with_events(conn, "cust-theirs", 2, 10)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-mine"))
        result = main._customer_investigate_search(_FakeRequest(), limit=50, offset=0)
    assert result["total"] == 20
    assert all(event["id"].startswith("cust-mine-") for event in result["events"])


def test_viewer_without_permission_on_a_camera_never_sees_its_events(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_tenant(conn)
        _seed_camera(conn, "cam-granted", camera_number=1)
        _seed_camera(conn, "cam-not-granted", camera_number=2)
        _seed_event(conn, "evt-granted", "cam-granted", "motion", "2026-08-01T00:00:00")
        _seed_event(conn, "evt-not-granted", "cam-not-granted", "motion", "2026-08-01T00:01:00")
        conn.execute(
            "INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at) "
            "VALUES('viewer-1','partner-1','viewer@example.com','Viewer','customer_viewer','x',1,'cust-1','2026-01-01')"
        )
        conn.execute("INSERT INTO customer_camera_permissions(user_id,camera_id,can_playback) VALUES('viewer-1','cam-granted',1)")
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _viewer_identity("cust-1", "viewer@example.com"))
        result = main._customer_investigate_search(_FakeRequest(), limit=50, offset=0)
    assert {event["id"] for event in result["events"]} == {"evt-granted"}


def test_non_customer_identity_returns_none(monkeypatch):
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    assert main._customer_investigate_search(_FakeRequest(), limit=50, offset=0) is None


def test_api_route_returns_403_for_non_customer_identity(monkeypatch):
    monkeypatch.setattr(partner_portal, "partner_identity", lambda request: None)
    with pytest.raises(main.HTTPException) as excinfo:
        main.customer_investigate_search_api(_FakeRequest())
    assert excinfo.value.status_code == 403


def test_api_route_shapes_events_for_the_client_and_reports_pagination_fields(monkeypatch, db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_fleet_with_events(conn, "cust-1", 2, 3)
        conn.commit()
        monkeypatch.setattr(partner_portal, "partner_identity", lambda request: _owner_identity("cust-1"))
        payload = main.customer_investigate_search_api(_FakeRequest(), offset=0)
    assert payload["total"] == 6
    assert payload["has_more"] is False
    assert payload["offset"] == 0
    assert payload["limit"] == main.INVESTIGATE_SEARCH_PAGE_SIZE
    assert len(payload["events"]) == 6
    first = payload["events"][0]
    assert set(first) == {"id", "camera_id", "camera", "site", "timestamp", "event_type", "thumbnail", "recording", "live", "confidence", "plate", "color", "rule", "review"}
