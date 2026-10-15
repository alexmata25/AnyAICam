from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import io
import json

import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request

import appliance_identity
import aws_cost_dashboard as costs
import main
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database, password_hash


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(main, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(main, "RUNTIME_ROLE", "cloud")
    for name in ("ANYAICAM_APPLIANCE_ID", "ANYAICAM_APPLIANCE_CLOUD_ID", "ANYAICAM_APPLIANCE_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    with override_target(sqlite_path=tmp_path / "aws-cost-test.db"):
        initialize_database()
        appliance_identity.reset_cloud_identity_backend_for_tests()
        # Invoke the registered synchronous route endpoints directly. This
        # exercises routing/auth/data behavior without starting the application's
        # camera and background-worker lifespan or opening Windows socketpairs.
        class DirectRouteClient:
            def get(self, path, *, cookies=None):
                route = next((item for item in main.app.routes if getattr(item, "path", None) == path and "GET" in getattr(item, "methods", set())), None)
                if route is None:
                    raise AssertionError(f"GET route not registered: {path}")
                headers = []
                if cookies:
                    cookie = "; ".join(f"{name}={value}" for name, value in cookies.items())
                    headers.append((b"cookie", cookie.encode("latin-1")))
                scope = {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "GET", "scheme": "http", "path": path,
                    "raw_path": path.encode("ascii"), "query_string": b"", "root_path": "",
                    "headers": headers, "client": ("127.0.0.1", 12345),
                    "server": ("testserver", 80), "app": main.app,
                }
                try:
                    result = route.endpoint(Request(scope))
                except HTTPException as error:
                    return _DirectResponse(error.status_code, {"detail": error.detail})
                if isinstance(result, Response):
                    return _DirectResponse(result.status_code, result.body)
                return _DirectResponse(200, result)

        class _DirectResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                if isinstance(payload, bytes):
                    self.text = payload.decode("utf-8", errors="replace")
                    self._json = None
                elif isinstance(payload, str):
                    self.text = payload
                    self._json = None
                else:
                    self._json = payload
                    self.text = json.dumps(payload)

            def json(self):
                if self._json is not None:
                    return self._json
                return json.loads(self.text)

        yield DirectRouteClient()
        appliance_identity.reset_cloud_identity_backend_for_tests()


def _cookies(email="owner@example.test", scope="global"):
    with connection() as db:
        now = "2026-10-14T00:00:00"
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner-cost", "Cost Test", "approved", "real", now))
        user_id = "user-" + email.replace("@", "-")
        db.execute(
            "INSERT OR IGNORE INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, "partner-cost", email, "Owner", "administrator", password_hash("test-password-123"), 1, now),
        )
        appliance_identity.create_grant(db, user_id=user_id, role="administrator", scope_type=scope, scope_id=None if scope == "global" else "partner-cost", granted_by="test")
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, "administrator", None, None, None)}


def _seed_customer_and_camera(camera_count=1):
    now = "2026-10-01T00:00:00"
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("p-cost", "Partner", "approved", "real", now))
        db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,company,email,phone,status,trial_status,billing_status,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("c-cost", "p-cost", "Cost customer", None, "c@example.test", None, "active", None, "active", "real", now))
        db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("s-cost", "c-cost", "Test site", now))
        db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,appliance_type,created_at) VALUES(?,?,?,?,?,?)", ("a-cost", "c-cost", "s-cost", "cloud-cost", "linux", now))
        for index in range(camera_count):
            db.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,name,resolution,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (f"cam-{index:03}", "c-cost", "s-cost", "a-cost", f"Camera {index}", "1080p", "active", now))


def test_owner_only_summary_and_access_audit(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_PLATFORM_OWNER_EMAIL", "owner@example.test")
    cookies = _cookies()
    page = client.get("/admin/aws-costs", cookies=cookies)
    assert page.status_code == 200
    assert "AWS Cost Dashboard" in page.text
    response = client.get("/api/admin/aws-costs/summary", cookies=cookies)
    assert response.status_code == 200
    assert response.json()["billing"]["current_month"] is None
    with connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='aws_cost_dashboard.view'").fetchone()["n"] == 2


def test_snapshot_migration_is_registered_and_idempotent(client):
    from db_migrations import apply_migrations

    migration_id = "20261015_aws_cost_dashboard_snapshots"
    expected_tables = {
        "aws_cost_monthly_snapshots",
        "aws_camera_usage_snapshots",
        "aws_lifecycle_rule_snapshots",
    }
    apply_migrations()
    with connection() as db:
        versions = {
            row["version"]
            for row in db.execute("SELECT version FROM schema_migrations").fetchall()
        }
        tables = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert migration_id in versions
    assert expected_tables <= tables


def test_non_owner_global_admin_is_denied(client, monkeypatch):
    monkeypatch.setenv("ANYAICAM_PLATFORM_OWNER_EMAIL", "owner@example.test")
    response = client.get("/api/admin/aws-costs/summary", cookies=_cookies("another-admin@example.test"))
    assert response.status_code == 403


def test_partner_admin_and_missing_owner_configuration_fail_closed(client, monkeypatch):
    monkeypatch.setenv("ANYAICAM_PLATFORM_OWNER_EMAIL", "owner@example.test")
    assert client.get("/admin/aws-costs", cookies=_cookies("owner@example.test", scope="partner")).status_code == 403
    monkeypatch.delenv("ANYAICAM_PLATFORM_OWNER_EMAIL")
    assert client.get("/api/admin/aws-costs/summary", cookies=_cookies()).status_code == 403
    assert client.get("/api/admin/aws-costs/summary").status_code == 403


def test_revoked_global_grant_loses_dashboard_access_immediately(client, monkeypatch):
    monkeypatch.setenv("ANYAICAM_PLATFORM_OWNER_EMAIL", "owner@example.test")
    cookies = _cookies()
    assert client.get("/api/admin/aws-costs/summary", cookies=cookies).status_code == 200
    with connection() as db:
        db.execute("UPDATE identity_grants SET revoked_at=? WHERE scope_type='global'", ("2026-10-14T01:00:00",))
    assert client.get("/api/admin/aws-costs/summary", cookies=cookies).status_code == 403


def test_customer_and_camera_costs_are_estimates_and_missing_values_are_null(client, monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "test-recordings")
    _seed_customer_and_camera(camera_count=2)
    captured = "2026-10-14T00:00:00+00:00"
    with connection() as db:
        db.execute("INSERT INTO aws_cost_monthly_snapshots(month,service,dimension_name,dimension_value,amount_usd,estimated,source,captured_at) VALUES(?,?,?,?,?,?,?,?)", ("2026-10", "Amazon Simple Storage Service", "RECORD_TYPE", "Usage", 2.5, 1, "cost_explorer", captured))
        db.execute("INSERT INTO aws_cost_monthly_snapshots(month,service,dimension_name,dimension_value,amount_usd,estimated,source,captured_at) VALUES(?,?,?,?,?,?,?,?)", ("2026-10", "Tax", "RECORD_TYPE", "Tax", 0.2, 1, "cost_explorer", captured))
        db.execute("INSERT INTO aws_camera_usage_snapshots(snapshot_date,customer_id,site_id,appliance_id,camera_id,storage_class,bytes,object_count,oldest_upload_at,newest_upload_at,recording_mode,retention_days,source,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("2026-10-14", "c-cost", "s-cost", "a-cost", "cam-000", "STANDARD", 10_000_000_000, 17, "2026-10-01T00:00:00Z", "2026-10-14T00:00:00Z", "Hybrid", 30, "s3_inventory", captured))
        db.execute("INSERT INTO aws_lifecycle_rule_snapshots(snapshot_at,bucket_name,rule_id,status,prefix,expiration_days,filter_json,source) VALUES(?,?,?,?,?,?,?,?)", (captured, "test-recordings", "rule-30", "Enabled", "recordings/c-cost/s-cost/a-cost/cam-000/", 30, "{}", "test"))
    data = costs.dashboard_summary(date(2026, 10, 14))
    bill = data["billing"]["current_month"]
    assert bill["status"] == "estimated"
    assert bill["total_usd"] == pytest.approx(2.7)
    assert bill["tax_usd"] == pytest.approx(0.2)
    assert data["attribution"]["customer_level_billed_costs"] is False
    measured = next(row for row in data["cameras"] if row["camera_id"] == "cam-000")
    assert measured["decimal_gb"] == pytest.approx(10)
    assert measured["object_or_version_count"] == 17
    assert measured["estimated_monthly_storage_usd"] == pytest.approx(0.23)
    assert measured["recording_mode"] == "Hybrid"
    assert measured["retention"]["policy_status"] == "matches"
    assert measured["retention"]["actual_deletion_verified"] is False
    unmeasured = next(row for row in data["cameras"] if row["camera_id"] == "cam-001")
    assert unmeasured["decimal_gb"] is None
    assert unmeasured["object_or_version_count"] is None
    assert data["customers"][0]["measured_camera_count"] == 1
    assert data["customers"][0]["storage_coverage_complete"] is False
    assert data["customers"][0]["estimated_storage_monthly_usd"] is None
    assert data["customers"][0]["known_measured_storage_estimate_usd"] == pytest.approx(0.23)
    assert data["customers"][0]["attributable_request_and_transfer_usd"] is None


def test_dashboard_database_failure_is_reported_without_fake_values(client, monkeypatch):
    monkeypatch.setenv("ANYAICAM_PLATFORM_OWNER_EMAIL", "owner@example.test")
    monkeypatch.setattr(costs, "dashboard_summary", lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")))
    response = client.get("/api/admin/aws-costs/summary", cookies=_cookies())
    assert response.status_code == 503
    assert response.json()["detail"] == "Stored AWS cost snapshots are unavailable."


def test_storage_estimate_never_prices_unknown_classes_as_zero():
    result = costs.estimate_storage_cost({"STANDARD": 1_000_000_000, "GLACIER": 2_000_000_000})
    assert result["monthly_cost_usd"] is None
    assert result["unpriced_storage_classes"] == ["GLACIER"]
    assert result["class_costs_usd"]["GLACIER"] is None


def test_projection_calculation_and_input_validation():
    result = costs.calculate_projection(4, 30, 1.2, 0.023, 10, 0.005, 3, 0.0004)
    assert result["retained_gb"] == pytest.approx(144)
    assert result["monthly_storage_usd"] == pytest.approx(3.312)
    assert result["monthly_request_usd"] == pytest.approx(0.006144)
    with pytest.raises(ValueError):
        costs.calculate_projection(0, 30, 1, 0.023)
    with pytest.raises(ValueError):
        costs.calculate_projection(1, 10, 1, 0.023)
    with pytest.raises(ValueError):
        costs.calculate_projection(1, 30, float("nan"), 0.023)


def test_inventory_aggregation_uses_exact_prefix_and_ignores_delete_markers():
    prefix_a = "recordings/customer/site/appliance/camera-a/"
    prefix_b = "recordings/customer/site/appliance/camera-b/"
    mappings = {
        prefix_a: {"customer_id": "customer", "site_id": "site", "appliance_id": "appliance", "camera_id": "camera-a"},
        prefix_b: {"customer_id": "customer", "site_id": "site", "appliance_id": "appliance", "camera_id": "camera-b"},
    }
    rows = [
        {"Key": prefix_a + "one.mp4", "Size": "100", "StorageClass": "STANDARD", "LastModifiedDate": "2026-10-10T00:00:00Z"},
        {"Key": prefix_a + "two.mp4", "Size": "200", "StorageClass": "STANDARD_IA", "LastModifiedDate": "2026-10-11T00:00:00Z", "IsLatest": "false"},
        {"Key": prefix_a + "deleted", "Size": "0", "StorageClass": "STANDARD", "IsDeleteMarker": "true"},
        {"Key": prefix_b + "three.mp4", "Size": "300", "StorageClass": "STANDARD", "LastModifiedDate": "2026-10-12T00:00:00Z"},
        {"Key": "recordings/customer/site/appliance/camera-abc/other.mp4", "Size": "999"},
    ]
    result = costs.aggregate_inventory_rows(rows, mappings)
    indexed = {(row["camera_id"], row["storage_class"]): row for row in result}
    assert indexed[("camera-a", "STANDARD")]["bytes"] == 100
    assert indexed[("camera-a", "STANDARD_IA")]["object_count"] == 1
    assert indexed[("camera-b", "STANDARD")]["bytes"] == 300
    assert len(result) == 3
    filtered = costs.aggregate_inventory_rows(rows, mappings, expected_bucket="expected-recordings")
    assert filtered == []


def test_cost_explorer_estimated_vs_finalized_is_preserved():
    response = {"ResultsByTime": [
        {"TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"}, "Estimated": False,
         "Groups": [{"Keys": ["Amazon S3", "Usage"], "Metrics": {"UnblendedCost": {"Amount": "3.80", "Unit": "USD"}}}]},
        {"TimePeriod": {"Start": "2026-10-01", "End": "2026-10-14"}, "Estimated": True,
         "Groups": [{"Keys": ["Amazon S3", "Usage"], "Metrics": {"UnblendedCost": {"Amount": "1.41", "Unit": "USD"}}}]},
    ]}
    rows = costs.normalize_cost_explorer_response(response)
    assert rows[0]["month"] == "2026-09"
    assert rows[0]["estimated"] is False
    assert rows[0]["amount_usd"] == pytest.approx(3.8)
    assert rows[1]["estimated"] is True


def test_retention_mismatch_and_old_upload_are_warnings_not_deletion_proof():
    result = costs.evaluate_retention(7, "recordings/c/s/a/c/", [{"status": "Enabled", "prefix": "recordings/c/s/a/c/", "expiration_days": 30}], "2026-01-01T00:00:00Z", datetime(2026, 2, 10, tzinfo=timezone.utc))
    assert result["policy_status"] == "mismatch"
    assert result["old_upload_beyond_lifecycle_plus_48h_observed"] is True
    assert result["actual_deletion_verified"] is False


def test_cost_explorer_and_inventory_aws_failures_do_not_fabricate_data(monkeypatch):
    monkeypatch.delenv("ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED", raising=False)
    from aws_cost_snapshot_collector import refresh_cost_explorer
    with pytest.raises(RuntimeError, match="disabled"):
        refresh_cost_explorer(date(2026, 10, 14))


def test_inventory_manifest_collector_reads_only_named_reports_and_verifies_checksums(client, monkeypatch, tmp_path):
    import aws_cost_snapshot_collector as collector
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "recordings-bucket")
    # The managed Windows sandbox restricts the system temporary directory;
    # keep collector scratch data inside pytest's writable isolated directory.
    monkeypatch.setattr(collector.tempfile, "tempdir", str(tmp_path))
    prefix = "recordings/c/s/a/cam/"
    monkeypatch.setattr(collector, "camera_prefix_mapping", lambda: {prefix: {"customer_id": "c", "site_id": "s", "appliance_id": "a", "camera_id": "cam"}})
    schema = "Bucket,Key,Size,LastModifiedDate,StorageClass"
    compressed = gzip.compress(f"recordings-bucket,{prefix}clip.mp4,512,2026-10-13T00:00:00Z,STANDARD\n".encode())
    manifest = {
        "fileFormat": "CSV", "sourceBucket": "recordings-bucket", "destinationBucket": "arn:aws:s3:::inventory-bucket", "fileSchema": schema, "creationTimestamp": "1791936000000",
        "files": [{"key": "reports/config/data/data-1.csv.gz", "MD5checksum": hashlib.md5(compressed).hexdigest()}],
    }
    manifest_bytes = json.dumps(manifest).encode()
    objects = {
        "reports/config/2026-10-13T00-00Z/manifest.json": manifest_bytes,
        "reports/config/2026-10-13T00-00Z/manifest.checksum": hashlib.md5(manifest_bytes).hexdigest().encode(),
        "reports/config/data/data-1.csv.gz": compressed,
    }

    class FakeClient:
        def __init__(self): self.keys = []
        def get_object(self, Bucket, Key):
            self.keys.append(Key)
            if Key not in objects: raise AssertionError(f"unexpected object read: {Key}")
            return {"Body": io.BytesIO(objects[Key])}

    fake = FakeClient()
    result = collector.import_inventory_manifest_s3(fake, "inventory-bucket", "reports/config/2026-10-13T00-00Z/manifest.json", snapshot_date="2026-10-13")
    assert result[0]["bytes"] == 512
    assert result[0]["camera_id"] == "cam"
    assert fake.keys == ["reports/config/2026-10-13T00-00Z/manifest.json", "reports/config/2026-10-13T00-00Z/manifest.checksum", "reports/config/data/data-1.csv.gz"]


def test_inventory_manifest_bad_checksum_is_rejected_before_import(client, monkeypatch):
    import aws_cost_snapshot_collector as collector
    manifest_bytes = json.dumps({"fileFormat": "CSV", "fileSchema": "Key,Size", "files": []}).encode()

    class FakeClient:
        def get_object(self, Bucket, Key):
            body = b"wrong" if Key.endswith("manifest.checksum") else manifest_bytes
            return {"Body": io.BytesIO(body)}

    with pytest.raises(ValueError, match="checksum"):
        collector.import_inventory_manifest_s3(FakeClient(), "inventory", "prefix/config/date/manifest.json")


def test_cost_explorer_client_error_is_not_converted_to_zero(monkeypatch):
    import sys
    from types import SimpleNamespace
    from aws_cost_snapshot_collector import refresh_cost_explorer
    monkeypatch.setenv("ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED", "true")

    class BrokenClient:
        def get_cost_and_usage(self, **_kwargs):
            raise PermissionError("AccessDenied")

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda *args, **kwargs: BrokenClient()))
    with pytest.raises(PermissionError):
        refresh_cost_explorer(date(2026, 10, 14))


def test_dashboard_handles_a_multi_camera_inventory_snapshot_without_n_plus_one_expectations(client):
    _seed_customer_and_camera(camera_count=80)
    captured = "2026-10-14T00:00:00+00:00"
    with connection() as db:
        for index in range(80):
            db.execute("INSERT INTO aws_camera_usage_snapshots(snapshot_date,customer_id,site_id,appliance_id,camera_id,storage_class,bytes,object_count,oldest_upload_at,newest_upload_at,recording_mode,retention_days,source,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("2026-10-14", "c-cost", "s-cost", "a-cost", f"cam-{index:03}", "STANDARD", 1000, 1, None, None, None, None, "s3_inventory", captured))
    data = costs.dashboard_summary(date(2026, 10, 14))
    assert len(data["cameras"]) == 80
    assert data["data_quality"]["camera_usage_rows"] == 80
    assert data["customers"][0]["camera_count"] == 80


def test_daily_growth_and_removed_camera_inventory_remain_visible(client):
    _seed_customer_and_camera(camera_count=1)
    captured = "2026-10-14T00:00:00+00:00"
    with connection() as db:
        for camera_id, day, size in (("cam-000", "2025-01-01", 1_000_000_000), ("cam-000", "2026-10-12", 10_000_000_000), ("cam-000", "2026-10-14", 12_000_000_000), ("cam-removed", "2026-10-14", 1_000_000_000)):
            db.execute(
                "INSERT INTO aws_camera_usage_snapshots(snapshot_date,customer_id,site_id,appliance_id,camera_id,storage_class,bytes,object_count,oldest_upload_at,newest_upload_at,recording_mode,retention_days,source,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, "c-cost", "s-cost", "a-cost", camera_id, "STANDARD", size, 2, "2026-10-01T00:00:00Z", "2026-10-14T00:00:00Z", "Hybrid", 30, "s3_inventory", captured),
            )
    data = costs.dashboard_summary(date(2026, 10, 14))
    current = next(row for row in data["cameras"] if row["camera_id"] == "cam-000")
    removed = next(row for row in data["cameras"] if row["camera_id"] == "cam-removed")
    assert current["storage_growth_since_previous_snapshot_bytes"] == 2_000_000_000
    assert current["storage_growth_period_days"] == 2
    assert len(current["daily_storage_history"]) == 3
    assert current["daily_storage_history"][0]["snapshot_date"] == "2025-01-01"
    assert removed["registered"] is False
    assert removed["decimal_gb"] == pytest.approx(1)
    customer = next(row for row in data["customers"] if row["customer_id"] == "c-cost")
    assert customer["camera_count"] == 1
    assert customer["stored_camera_prefix_count"] == 2
    assert customer["known_storage_decimal_gb"] == pytest.approx(13)
    site = next(row for row in data["sites"] if row["site_id"] == "s-cost")
    assert site["camera_count"] == 1
    assert site["stored_camera_prefix_count"] == 2


def test_inventory_mapping_uses_authoritative_recording_and_retention_settings(client):
    _seed_customer_and_camera(camera_count=1)
    with connection() as db:
        db.execute("UPDATE cameras SET cloud_recording_mode='motion',local_recording_mode='continuous' WHERE id='cam-000'")
        db.execute("INSERT INTO customer_cloud_policy(customer_id,retention_days,updated_at,updated_by) VALUES(?,?,?,?)", ("c-cost", 14, "2026-10-14T00:00:00Z", "test"))
    from aws_cost_snapshot_collector import camera_prefix_mapping
    mapping = camera_prefix_mapping()["recordings/c-cost/s-cost/a-cost/cam-000/"]
    assert mapping["recording_mode"] == "Hybrid"
    assert mapping["retention_days"] == 14


def test_active_local_plan_does_not_report_hybrid_retention_from_stale_camera_mode():
    context = costs.resolve_recording_context({
        "paid_plan_key": "basic_local", "paid_plan_status": "active",
        "cloud_event_storage": True, "cloud_recording_mode": "motion",
        "policy_retention_days": 14,
    })
    assert context == {"recording_mode": "Local", "retention_days": None}


def test_inventory_manifest_rejects_shards_outside_inventory_data_prefix(monkeypatch):
    import aws_cost_snapshot_collector as collector
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "recordings-bucket")
    manifest_bytes = json.dumps({
        "fileFormat": "CSV", "sourceBucket": "recordings-bucket", "destinationBucket": "arn:aws:s3:::inventory-bucket", "fileSchema": "Bucket,Key,Size", "creationTimestamp": "1791936000000",
        "files": [{"key": "recordings/private.mp4", "MD5checksum": hashlib.md5(b"x").hexdigest()}],
    }).encode()

    class FakeClient:
        def __init__(self): self.keys = []
        def get_object(self, Bucket, Key):
            self.keys.append(Key)
            if Key == "reports/config/2026-10-13T00-00Z/manifest.json":
                body = manifest_bytes
            elif Key == "reports/config/2026-10-13T00-00Z/manifest.checksum":
                body = hashlib.md5(manifest_bytes).hexdigest().encode()
            else:
                raise AssertionError("collector attempted to read a non-inventory object")
            return {"Body": io.BytesIO(body)}

    fake = FakeClient()
    with pytest.raises(ValueError, match="outside the manifest data directory"):
        collector.import_inventory_manifest_s3(fake, "inventory-bucket", "reports/config/2026-10-13T00-00Z/manifest.json", snapshot_date="2026-10-13")
    assert fake.keys == ["reports/config/2026-10-13T00-00Z/manifest.json", "reports/config/2026-10-13T00-00Z/manifest.checksum"]


def test_inventory_manifest_must_identify_configured_recording_bucket(monkeypatch):
    import aws_cost_snapshot_collector as collector
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "expected-recordings")
    manifest_bytes = json.dumps({
        "fileFormat": "CSV", "sourceBucket": "different-bucket", "destinationBucket": "arn:aws:s3:::inventory-bucket", "fileSchema": "Bucket,Key,Size", "files": [],
    }).encode()

    class FakeClient:
        def get_object(self, Bucket, Key):
            body = hashlib.md5(manifest_bytes).hexdigest().encode() if Key.endswith("manifest.checksum") else manifest_bytes
            return {"Body": io.BytesIO(body)}

    with pytest.raises(ValueError, match="source bucket"):
        collector.import_inventory_manifest_s3(FakeClient(), "inventory-bucket", "reports/config/date/manifest.json")


def test_cost_explorer_rejects_non_usd_values():
    with pytest.raises(ValueError, match="non-USD"):
        costs.normalize_cost_explorer_response({"ResultsByTime": [{
            "TimePeriod": {"Start": "2026-10-01"}, "Estimated": True,
            "Groups": [{"Keys": ["Amazon S3", "Usage"], "Metrics": {"UnblendedCost": {"Amount": "2.5", "Unit": "EUR"}}}],
        }]})


def test_usage_details_without_record_type_totals_are_not_presented_as_zero():
    _seed_customer_and_camera(camera_count=1)
    with connection() as db:
        db.execute(
            "INSERT INTO aws_cost_monthly_snapshots(month,service,dimension_name,dimension_value,amount_usd,estimated,source,captured_at) VALUES(?,?,?,?,?,?,?,?)",
            ("2026-10", "Amazon Simple Storage Service", "USAGE_TYPE", "TimedStorage-ByteHrs", 1.25, 1, "cost_explorer", "2026-10-14T00:00:00Z"),
        )
    bill = costs.dashboard_summary(date(2026, 10, 14))["billing"]["current_month"]
    assert bill["status"] == "detail_only"
    assert bill["total_usd"] is None
    assert bill["s3_usd"] is None
    assert bill["usage_breakdown"][0]["amount_usd"] == pytest.approx(1.25)
