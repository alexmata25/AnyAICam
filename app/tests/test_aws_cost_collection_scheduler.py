"""Cloud-only daily AWS cost collection: gating, once-per-day claim, failure
classification, persisted status, and the dashboard empty state.

Every AWS client here is a fake injected through sys.modules["boto3"]; no test
reaches AWS or the network.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

import aws_cost_collection_scheduler as scheduler
import aws_cost_dashboard as costs
from partner_db import connection
from test_aws_cost_dashboard import _cookies, client  # noqa: F401 -- fixtures

NOW = datetime(2026, 10, 20, 6, 0, 5, tzinfo=timezone.utc)

CE_RESPONSE = {
    "ResultsByTime": [{
        "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-20"}, "Estimated": True,
        "Groups": [
            {"Keys": ["Amazon Simple Storage Service", "Usage"], "Metrics": {"UnblendedCost": {"Amount": "12.34", "Unit": "USD"}}},
            {"Keys": ["Amazon Elastic Compute Cloud - Compute", "Usage"], "Metrics": {"UnblendedCost": {"Amount": "40.00", "Unit": "USD"}}},
        ],
    }],
}


class FakeCostExplorer:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return CE_RESPONSE


class FakeS3:
    def __init__(self, lifecycle_error=None):
        self.lifecycle_error = lifecycle_error

    def get_bucket_lifecycle_configuration(self, Bucket):
        if self.lifecycle_error is not None:
            raise self.lifecycle_error
        return {"Rules": [{"ID": "expire-30", "Status": "Enabled", "Filter": {"Prefix": "recordings/"}, "Expiration": {"Days": 30}}]}

    def get_object(self, **_kwargs):  # pragma: no cover - inventory not configured in these tests
        raise AssertionError("no S3 object read expected")


def _access_denied(operation="GetCostAndUsage"):
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "User: arn:aws:sts::123:assumed-role/x is not authorized"}}, operation)


@pytest.fixture()
def aws(monkeypatch):
    """Enable the cloud gate and install a fake boto3."""
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    monkeypatch.setenv("ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED", "true")
    monkeypatch.setenv("ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED", "true")
    monkeypatch.delenv("ANYAICAM_RECORDING_S3_BUCKET", raising=False)
    monkeypatch.delenv("ANYAICAM_COST_INVENTORY_MANIFEST_S3", raising=False)
    monkeypatch.delenv("ANYAICAM_COST_SNAPSHOT_SCHEDULE_HOUR_UTC", raising=False)
    fakes = SimpleNamespace(ce=FakeCostExplorer(), s3=FakeS3(), created=[])

    def factory(service, **_kwargs):
        fakes.created.append(service)
        return {"ce": fakes.ce, "s3": fakes.s3}[service]

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=factory))
    return fakes


def _rows(sql):
    with connection() as db:
        return [dict(row) for row in db.execute(sql).fetchall()]


# --- gating -----------------------------------------------------------------

@pytest.mark.parametrize("role", ["edge", "combined", "", "EDGE"])
def test_edge_and_combined_runtimes_never_collect(client, aws, role):
    enabled, reason = scheduler.scheduler_gate(role)
    assert enabled is False and "cloud runtime" in reason
    result = scheduler.run_daily_collection(runtime_role=role, now=NOW)
    assert result["status"] == "disabled"
    assert aws.created == [] and aws.ce.calls == []
    assert _rows("SELECT * FROM aws_cost_collection_runs") == []


@pytest.mark.parametrize("unset", ["ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED", "ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED"])
def test_flags_off_means_no_run(client, aws, monkeypatch, unset):
    monkeypatch.setenv(unset, "false")
    enabled, reason = scheduler.scheduler_gate("cloud")
    assert enabled is False and unset in reason
    assert scheduler.run_daily_collection(runtime_role="cloud", now=NOW)["status"] == "disabled"
    assert aws.created == []
    assert _rows("SELECT * FROM aws_cost_collection_runs") == []


def test_worker_exits_without_sleeping_when_gated_off(aws, monkeypatch):
    monkeypatch.setenv("ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED", "")
    events, sleeps = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    asyncio.run(scheduler.aws_cost_collection_worker("cloud", lambda event, **fields: events.append((event, fields)), sleep=fake_sleep))
    asyncio.run(scheduler.aws_cost_collection_worker("edge", lambda event, **fields: events.append((event, fields)), sleep=fake_sleep))
    assert sleeps == [] and aws.created == []
    assert [event for event, _ in events] == ["aws_cost.collection_scheduler_disabled"] * 2


def test_main_starts_the_worker_only_through_the_cloud_gate():
    from pathlib import Path
    source = Path(scheduler.__file__).with_name("main.py").read_text(encoding="utf-8")
    assert "aws_cost_collection_scheduler.aws_cost_collection_worker(RUNTIME_ROLE, structured_log)" in source
    assert "if aws_cost_collection_scheduler.scheduler_gate(RUNTIME_ROLE)[0]" in source
    assert "aws_cost_collection_task.cancel()" in source


# --- schedule ---------------------------------------------------------------

def test_schedule_hour_default_override_and_invalid(monkeypatch):
    monkeypatch.delenv("ANYAICAM_COST_SNAPSHOT_SCHEDULE_HOUR_UTC", raising=False)
    assert scheduler.schedule_hour_utc() == 6
    monkeypatch.setenv("ANYAICAM_COST_SNAPSHOT_SCHEDULE_HOUR_UTC", "13")
    assert scheduler.schedule_hour_utc() == 13
    for bad in ("24", "-1", "six"):
        monkeypatch.setenv("ANYAICAM_COST_SNAPSHOT_SCHEDULE_HOUR_UTC", bad)
        assert scheduler.schedule_hour_utc() == 6
    assert scheduler.next_run_at(datetime(2026, 10, 20, 5, 59, tzinfo=timezone.utc), 6) == datetime(2026, 10, 20, 6, tzinfo=timezone.utc)
    assert scheduler.next_run_at(datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc), 6) == datetime(2026, 10, 21, 6, tzinfo=timezone.utc)


def test_worker_runs_after_startup_delay_then_sleeps_until_next_hour(client, aws, monkeypatch):
    monkeypatch.setenv("ANYAICAM_COST_SNAPSHOT_STARTUP_DELAY_SECONDS", "120")
    sleeps, events = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    clock = lambda: datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)  # noqa: E731
    asyncio.run(scheduler.aws_cost_collection_worker("cloud", lambda event, **fields: events.append((event, fields)), sleep=fake_sleep, clock=clock, max_iterations=1))
    assert sleeps == [120]
    assert len(aws.ce.calls) == 2  # RECORD_TYPE + USAGE_TYPE, one page each
    finished = [fields for event, fields in events if event == "aws_cost.collection_finished"]
    assert finished and finished[0]["status"] == "succeeded"


# --- once per day -------------------------------------------------------------

def test_second_worker_same_day_does_not_collect_again(client, aws):
    first = scheduler.run_daily_collection(runtime_role="cloud", now=NOW)
    second = scheduler.run_daily_collection(runtime_role="cloud", now=NOW.replace(hour=18))
    assert first["status"] == "succeeded"
    assert second["status"] == "already_claimed"
    assert len(aws.ce.calls) == 2
    assert len(_rows("SELECT * FROM aws_cost_collection_runs")) == 1
    # The next UTC day is a new claim.
    third = scheduler.run_daily_collection(runtime_role="cloud", now=NOW.replace(day=21))
    assert third["status"] == "succeeded"
    assert len(_rows("SELECT * FROM aws_cost_collection_runs")) == 2


def test_running_claim_blocks_until_stale_then_can_be_reclaimed(client):
    day = NOW.date()
    token = scheduler.claim_daily_run(day, trigger="schedule", now=NOW)
    assert token
    assert scheduler.claim_daily_run(day, trigger="schedule", now=NOW.replace(hour=7)) is None
    later = NOW.replace(hour=10)  # > 3h: the first worker died mid-run
    reclaimed = scheduler.claim_daily_run(day, trigger="schedule", now=later)
    assert reclaimed and reclaimed != token


def test_failed_day_is_not_retried_by_schedule_but_manual_retry_is_allowed(client, aws):
    aws.ce.error = _access_denied()
    assert scheduler.run_daily_collection(runtime_role="cloud", now=NOW)["status"] == "failed"
    assert scheduler.run_daily_collection(runtime_role="cloud", now=NOW.replace(hour=7))["status"] == "already_claimed"
    aws.ce.error = None  # administrator granted ce:GetCostAndUsage
    manual = scheduler.run_daily_collection(trigger="manual", runtime_role="cloud", now=NOW.replace(hour=8), retry_failed=True)
    assert manual["status"] == "succeeded"
    row = _rows("SELECT status,trigger_source,error_code FROM aws_cost_collection_runs")[0]
    assert row == {"status": "succeeded", "trigger_source": "manual", "error_code": None}


# --- results and status -------------------------------------------------------

def test_access_denied_is_a_permission_status_never_a_zero_cost(client, aws):
    aws.ce.error = _access_denied()
    result = scheduler.run_daily_collection(runtime_role="cloud", now=NOW)
    assert result["status"] == "failed"
    step = result["steps"]["cost_explorer"]
    assert step["error_category"] == "permission"
    assert step["error_code"] == "AccessDeniedException"
    assert "ce:GetCostAndUsage" in step["message"]
    assert "arn:aws" not in step["message"]  # no request internals stored
    assert _rows("SELECT * FROM aws_cost_monthly_snapshots") == []
    run = _rows("SELECT status,error_category,error_code,message FROM aws_cost_collection_runs")[0]
    assert run["status"] == "failed" and run["error_category"] == "permission"
    overview = scheduler.collection_overview(now=NOW)
    assert "FAILED: AccessDeniedException — ask an AWS administrator to grant ce:GetCostAndUsage" in overview["summary_text"]
    assert overview["empty_state"].startswith("No AWS cost data is stored yet")
    assert "ce:GetCostAndUsage" in overview["empty_state"]


def test_plain_permission_error_and_missing_credentials_are_classified():
    assert scheduler.classify_error(PermissionError("AccessDenied")) == ("permission", "AccessDenied")
    assert scheduler.classify_error(NoCredentialsError()) == ("credentials", "NoCredentialsError")
    assert scheduler.classify_error(ClientError({"Error": {"Code": "ThrottlingException"}}, "GetCostAndUsage")) == ("aws_error", "ThrottlingException")


def test_successful_run_persists_snapshots_and_status(client, aws, monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "recordings-bucket")
    result = scheduler.run_daily_collection(runtime_role="cloud", now=NOW)
    assert result["status"] == "succeeded"
    assert result["steps"]["cost_explorer"] == {"status": "succeeded", "rows": 4}
    assert result["steps"]["lifecycle"] == {"status": "succeeded", "rules": 1}
    assert result["steps"]["inventory"]["status"] == "skipped"
    amounts = {row["service"]: row["amount_usd"] for row in _rows("SELECT service,amount_usd FROM aws_cost_monthly_snapshots WHERE dimension_name='RECORD_TYPE'")}
    assert amounts == {"Amazon Simple Storage Service": pytest.approx(12.34), "Amazon Elastic Compute Cloud - Compute": pytest.approx(40.0)}
    assert len(_rows("SELECT * FROM aws_lifecycle_rule_snapshots")) == 1
    assert aws.ce.calls[0]["TimePeriod"] == {"Start": "2026-09-01", "End": "2026-10-20"}
    overview = scheduler.collection_overview(now=NOW)
    assert overview["last_run"]["status"] == "succeeded"
    assert overview["summary_text"].startswith("Last collection: ") and " OK." in overview["summary_text"]
    assert overview["empty_state"] is None


def test_lifecycle_denied_while_cost_explorer_works_is_partial(client, aws, monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "recordings-bucket")
    aws.s3.lifecycle_error = ClientError({"Error": {"Code": "AccessDenied"}}, "GetBucketLifecycleConfiguration")
    result = scheduler.run_daily_collection(runtime_role="cloud", now=NOW)
    assert result["status"] == "partial"
    assert "s3:GetLifecycleConfiguration" in result["steps"]["lifecycle"]["message"]
    assert _rows("SELECT * FROM aws_lifecycle_rule_snapshots") == []
    assert "PARTIAL" in scheduler.collection_overview(now=NOW)["summary_text"]


def test_unexpected_crash_is_contained(client, aws, monkeypatch):
    monkeypatch.setattr(scheduler, "_collect", lambda _day: (_ for _ in ()).throw(KeyError("boom")))
    result = scheduler.run_daily_collection(runtime_role="cloud", now=NOW)
    assert result["status"] == "failed"
    assert _rows("SELECT status FROM aws_cost_collection_runs")[0]["status"] == "failed"


def test_worker_survives_errors(aws, monkeypatch):
    monkeypatch.setattr(scheduler, "run_daily_collection", lambda **_kw: (_ for _ in ()).throw(RuntimeError("db down")))
    events = []

    async def fake_sleep(_seconds):
        return None

    asyncio.run(scheduler.aws_cost_collection_worker("cloud", lambda event, **fields: events.append((event, fields)), sleep=fake_sleep, max_iterations=2))
    assert [event for event, _ in events].count("aws_cost.collection_worker_error") == 2


def test_cli_manual_run_records_status(client, aws, capsys):
    import aws_cost_snapshot_collector as collector
    assert collector.main(["--run-daily-collection"]) == 0
    assert '"status": "succeeded"' in capsys.readouterr().out
    assert _rows("SELECT trigger_source FROM aws_cost_collection_runs")[0]["trigger_source"] == "manual"


def test_inventory_manifest_template_tries_run_date_then_previous_day(monkeypatch):
    monkeypatch.setenv("ANYAICAM_COST_INVENTORY_MANIFEST_S3", "s3://inv/recordings/cfg/{date}T01-00Z/manifest.json")
    assert scheduler._inventory_manifest_candidates(NOW.date()) == [
        ("inv", "recordings/cfg/2026-10-20T01-00Z/manifest.json"),
        ("inv", "recordings/cfg/2026-10-19T01-00Z/manifest.json"),
    ]


# --- dashboard empty state ----------------------------------------------------

def test_dashboard_empty_state_when_collection_disabled(client, monkeypatch):
    monkeypatch.setenv("ANYAICAM_PLATFORM_OWNER_EMAIL", "owner@example.test")
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    monkeypatch.delenv("ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED", raising=False)
    cookies = _cookies()
    data = client.get("/api/admin/aws-costs/summary", cookies=cookies).json()
    assert data["collection"]["scheduler_enabled"] is False
    assert "Collection is disabled on this server" in data["collection"]["empty_state"]
    assert "Last collection: never run." in data["collection"]["summary_text"]
    assert data["billing"]["current_month"] is None  # no fabricated figures
    page = client.get("/admin/aws-costs", cookies=cookies)
    assert page.status_code == 200
    assert "No AWS cost data is stored yet, so no figures are shown." in page.text
    assert 'id="aws-cost-collection"' in page.text


def test_dashboard_empty_state_enabled_not_yet_run_and_after_failure(client, aws, monkeypatch):
    monkeypatch.setenv("ANYAICAM_PLATFORM_OWNER_EMAIL", "owner@example.test")
    cookies = _cookies()
    data = client.get("/api/admin/aws-costs/summary", cookies=cookies).json()
    assert "Collection is enabled but has not run yet" in data["collection"]["empty_state"]
    aws.ce.error = _access_denied()
    scheduler.run_daily_collection(runtime_role="cloud")
    page = client.get("/admin/aws-costs", cookies=cookies)
    assert "FAILED: AccessDeniedException — ask an AWS administrator to grant ce:GetCostAndUsage" in page.text


def test_dashboard_still_owner_only(client, monkeypatch):
    monkeypatch.setenv("ANYAICAM_PLATFORM_OWNER_EMAIL", "owner@example.test")
    assert client.get("/admin/aws-costs", cookies=_cookies("someone@example.test")).status_code == 403
    assert client.get("/api/admin/aws-costs/summary").status_code in {401, 403}


def test_render_page_escapes_status_text():
    html = costs.render_page({"summary_text": "<script>x</script>", "empty_state": "a & b"})
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html and "a &amp; b" in html


def test_iam_policy_draft_is_least_privilege_and_marked_not_applied():
    import json
    from pathlib import Path
    path = Path(scheduler.__file__).resolve().parent.parent / "docs" / "aws-cost-collector-iam-policy.DRAFT-NOT-APPLIED.json"
    policy = json.loads(path.read_text(encoding="utf-8"))
    actions = {(s["Action"], s["Resource"]) for s in policy["Statement"]}
    assert all(s["Effect"] == "Allow" for s in policy["Statement"])
    assert {action for action, _ in actions} == {"ce:GetCostAndUsage", "s3:GetLifecycleConfiguration", "s3:GetObject"}
    assert ("ce:GetCostAndUsage", "*") in actions
    assert all(resource.startswith("arn:aws:s3:::REPLACE_WITH_") for action, resource in actions if action.startswith("s3:"))
