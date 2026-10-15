"""Opt-in, read-only AWS cost snapshot collection.

The collector consumes an S3 Inventory CSV already delivered to a local
protected path and optionally calls read-only Cost Explorer/lifecycle APIs.
It never lists recording objects, reads recording contents, or writes to AWS.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from database_backend import column_names, connect
from recording_credentials import recording_s3_prefix
from aws_cost_dashboard import aggregate_inventory_rows, normalize_cost_explorer_response, resolve_recording_context


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated_snapshot_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError("Snapshot date must be an ISO calendar date (YYYY-MM-DD).") from error
    return parsed.isoformat()


def camera_prefix_mapping() -> dict[str, dict]:
    camera_columns = column_names("cameras")
    optional = [column for column in ("cloud_recording_mode", "local_recording_mode") if column in camera_columns]
    selected = "c.id,c.customer_id,c.site_id,c.appliance_id" + "".join(f",c.{column}" for column in optional)
    selected += ",cp.retention_days AS policy_retention_days"
    selected += ",(SELECT p.recording_mode FROM plans p WHERE p.customer_id=c.customer_id ORDER BY p.created_at DESC LIMIT 1) AS legacy_recording_mode"
    selected += ",(SELECT p.retention_days FROM plans p WHERE p.customer_id=c.customer_id ORDER BY p.created_at DESC LIMIT 1) AS legacy_retention_days"
    selected += ",v2.plan_key AS paid_plan_key,v2.status AS paid_plan_status,v2.cloud_event_storage,v2.cloud_event_retention_days"
    with connect() as db:
        rows = db.execute(
            f"SELECT {selected} FROM cameras c "
            "LEFT JOIN customer_cloud_policy cp ON cp.customer_id=c.customer_id "
            "LEFT JOIN camera_plan_entitlements_v2 v2 ON v2.customer_id=c.customer_id "
            "WHERE c.customer_id IS NOT NULL AND c.site_id IS NOT NULL AND c.appliance_id IS NOT NULL"
        ).fetchall()
    mapping: dict[str, dict] = {}
    for raw in rows:
        row = dict(raw)
        camera_id = str(row.get("id") or "")
        if not camera_id:
            continue
        prefix = recording_s3_prefix(str(row["customer_id"]), str(row["site_id"]), str(row["appliance_id"]), camera_id)
        context = resolve_recording_context(row)
        mapping[prefix] = {
            "customer_id": str(row["customer_id"]), "site_id": str(row["site_id"]),
            "appliance_id": str(row["appliance_id"]), "camera_id": camera_id,
            "recording_mode": context["recording_mode"], "retention_days": context["retention_days"],
        }
    return mapping


def import_inventory_csv(path: str | Path, schema: list[str], snapshot_date: str, *, source: str = "s3_inventory", expected_bucket: str | None = None) -> list[dict]:
    if not schema or len(set(schema)) != len(schema) or not {"Bucket", "Key", "Size"}.issubset(set(schema)):
        raise ValueError("Inventory schema must uniquely include Bucket, Key, and Size.")
    expected_bucket = str(expected_bucket or os.getenv("ANYAICAM_RECORDING_S3_BUCKET") or "").strip()
    if not expected_bucket:
        raise RuntimeError("The expected recording bucket is not configured; refusing to import unverified Inventory rows.")
    snapshot_date = _validated_snapshot_date(snapshot_date)
    opener = gzip.open if str(path).lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream, fieldnames=schema)
        aggregates = aggregate_inventory_rows(rows, camera_prefix_mapping(), expected_bucket=expected_bucket)
    captured_at = _now()
    with connect() as db:
        for row in aggregates:
            db.execute(
                "INSERT INTO aws_camera_usage_snapshots(snapshot_date,customer_id,site_id,appliance_id,camera_id,storage_class,bytes,object_count,oldest_upload_at,newest_upload_at,recording_mode,retention_days,source,captured_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(snapshot_date,camera_id,storage_class,source) DO UPDATE SET "
                "customer_id=excluded.customer_id,site_id=excluded.site_id,appliance_id=excluded.appliance_id,bytes=excluded.bytes,object_count=excluded.object_count,"
                "oldest_upload_at=excluded.oldest_upload_at,newest_upload_at=excluded.newest_upload_at,recording_mode=excluded.recording_mode,retention_days=excluded.retention_days,captured_at=excluded.captured_at",
                (snapshot_date,row["customer_id"],row["site_id"],row["appliance_id"],row["camera_id"],row["storage_class"],row["bytes"],row["object_count"],row["oldest_upload_at"],row["newest_upload_at"],row["recording_mode"],row["retention_days"],source,captured_at),
            )
    return aggregates


def import_inventory_manifest_s3(client, source_bucket: str, manifest_key: str, *, snapshot_date: str | None = None) -> list[dict]:
    """Read one known S3 Inventory manifest and its CSV shards, without LIST.

    Shards are streamed through owner-only temporary files, integrity checked
    against the Inventory manifest MD5, parsed, and removed immediately. No
    recording object is fetched. Exact manifest paths make s3:ListBucket
    unnecessary for this collector.
    """
    import datetime as _dt
    if not source_bucket or not manifest_key or manifest_key.rsplit("/", 1)[-1] != "manifest.json":
        raise ValueError("An exact S3 Inventory manifest.json key is required.")
    manifest_parent = manifest_key.rsplit("/", 1)[0] if "/" in manifest_key else ""
    if "/" not in manifest_parent:
        raise ValueError("S3 Inventory manifest key is outside the expected report directory structure.")
    # AWS stores each dated manifest under <config-id>/<timestamp>/ while
    # report shards are siblings under <config-id>/data/.
    report_parent = manifest_parent.rsplit("/", 1)[0]
    manifest_response = client.get_object(Bucket=source_bucket, Key=manifest_key)
    manifest_bytes = manifest_response["Body"].read()
    checksum_key = (manifest_parent + "/" if manifest_parent else "") + "manifest.checksum"
    try:
        checksum_response = client.get_object(Bucket=source_bucket, Key=checksum_key)
        expected_manifest_md5 = checksum_response["Body"].read().decode("ascii").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", expected_manifest_md5):
            raise ValueError("S3 Inventory manifest checksum is malformed.")
        if hashlib.md5(manifest_bytes).hexdigest() != expected_manifest_md5:
            raise ValueError("S3 Inventory manifest checksum mismatch.")
    except Exception as error:
        if isinstance(error, ValueError):
            raise
        # Do not accept a manifest when its companion checksum cannot be read.
        raise RuntimeError("S3 Inventory manifest checksum is unavailable.") from error
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    expected_recording_bucket = str(os.getenv("ANYAICAM_RECORDING_S3_BUCKET") or "").strip()
    if not expected_recording_bucket or str(manifest.get("sourceBucket") or "") != expected_recording_bucket:
        raise ValueError("S3 Inventory manifest source bucket does not match the configured recording bucket.")
    expected_destination = f"arn:aws:s3:::{source_bucket}"
    if str(manifest.get("destinationBucket") or "") != expected_destination:
        raise ValueError("S3 Inventory manifest destination bucket does not match the requested Inventory bucket.")
    if str(manifest.get("fileFormat") or "").upper() != "CSV":
        raise ValueError("Only CSV S3 Inventory reports are supported by this collector.")
    schema = [item.strip() for item in str(manifest.get("fileSchema") or "").split(",") if item.strip()]
    if not {"Bucket", "Key", "Size"}.issubset(set(schema)):
        raise ValueError("S3 Inventory manifest schema is missing Bucket, Key, or Size.")
    if snapshot_date is None:
        timestamp = int(manifest.get("creationTimestamp") or 0)
        if timestamp <= 0:
            raise ValueError("S3 Inventory manifest creation timestamp is invalid.")
        snapshot_date = _dt.datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).date().isoformat()
    else:
        snapshot_date = _validated_snapshot_date(snapshot_date)
    prefix_map = camera_prefix_mapping()
    aggregates: list[dict] = []
    for shard in manifest.get("files") or []:
        shard_key = str(shard.get("key") or "")
        expected_prefix = report_parent + "/data/"
        if not shard_key.startswith(expected_prefix) or not shard_key.endswith(".csv.gz"):
            raise ValueError("S3 Inventory shard is outside the manifest data directory or has an unsupported format.")
        response = client.get_object(Bucket=source_bucket, Key=shard_key)
        expected_md5 = str(shard.get("MD5checksum") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", expected_md5):
            raise ValueError("S3 Inventory shard checksum is missing or malformed.")
        with tempfile.TemporaryDirectory(prefix="anyaicam-inventory-") as temp_dir:
            try:
                os.chmod(temp_dir, 0o700)
            except OSError:
                pass
            local_path = Path(temp_dir) / "inventory.csv.gz"
            digest = hashlib.md5()
            with open(local_path, "wb") as output:
                body = response["Body"]
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected_md5:
                raise ValueError("S3 Inventory shard checksum mismatch.")
            with gzip.open(local_path, "rt", encoding="utf-8-sig", newline="") as report:
                shard_rows = csv.DictReader(report, fieldnames=schema)
                aggregates.extend(aggregate_inventory_rows(shard_rows, prefix_map, expected_bucket=expected_recording_bucket))
    # Consolidate shards that report the same camera/storage class.
    merged: dict[tuple, dict] = {}
    for row in aggregates:
        key = (row["customer_id"],row["site_id"],row["appliance_id"],row["camera_id"],row["storage_class"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(row)
            continue
        existing["bytes"] += row["bytes"]
        existing["object_count"] += row["object_count"]
        dates = [value for value in (existing.get("oldest_upload_at"),row.get("oldest_upload_at")) if value]
        existing["oldest_upload_at"] = min(dates) if dates else None
        dates = [value for value in (existing.get("newest_upload_at"),row.get("newest_upload_at")) if value]
        existing["newest_upload_at"] = max(dates) if dates else None
    captured_at = _now()
    with connect() as db:
        for row in merged.values():
            db.execute(
                "INSERT INTO aws_camera_usage_snapshots(snapshot_date,customer_id,site_id,appliance_id,camera_id,storage_class,bytes,object_count,oldest_upload_at,newest_upload_at,recording_mode,retention_days,source,captured_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(snapshot_date,camera_id,storage_class,source) DO UPDATE SET "
                "customer_id=excluded.customer_id,site_id=excluded.site_id,appliance_id=excluded.appliance_id,bytes=excluded.bytes,object_count=excluded.object_count,"
                "oldest_upload_at=excluded.oldest_upload_at,newest_upload_at=excluded.newest_upload_at,recording_mode=excluded.recording_mode,retention_days=excluded.retention_days,captured_at=excluded.captured_at",
                (snapshot_date,row["customer_id"],row["site_id"],row["appliance_id"],row["camera_id"],row["storage_class"],row["bytes"],row["object_count"],row["oldest_upload_at"],row["newest_upload_at"],row["recording_mode"],row["retention_days"],"s3_inventory",captured_at),
            )
    return list(merged.values())


def persist_cost_explorer_response(response: dict, dimension_name: str = "RECORD_TYPE") -> list[dict]:
    normalized = normalize_cost_explorer_response(response, dimension_name=dimension_name)
    captured_at = _now()
    with connect() as db:
        for row in normalized:
            db.execute(
                "INSERT INTO aws_cost_monthly_snapshots(month,service,dimension_name,dimension_value,amount_usd,estimated,source,captured_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(month,service,dimension_name,dimension_value,source) DO UPDATE SET amount_usd=excluded.amount_usd,estimated=excluded.estimated,captured_at=excluded.captured_at",
                (row["month"],row["service"],row["dimension_name"],row["dimension_value"],row["amount_usd"],int(row["estimated"]),"cost_explorer",captured_at),
            )
    return normalized


def persist_lifecycle_rules(bucket: str, rules: list[dict], captured_at: str | None = None, source: str = "s3_get_lifecycle_configuration") -> int:
    captured_at = captured_at or _now()
    normalized = rules or [{"ID": "__no_lifecycle_configuration__", "Status": "Disabled", "Prefix": "", "Filter": {}}]
    with connect() as db:
        for rule in normalized:
            filter_data = rule.get("Filter") or {}
            prefix = rule.get("Prefix")
            if prefix is None and isinstance(filter_data, dict):
                prefix = filter_data.get("Prefix")
            expiration = rule.get("Expiration") or {}
            days = expiration.get("Days") if isinstance(expiration, dict) else None
            db.execute(
                "INSERT INTO aws_lifecycle_rule_snapshots(snapshot_at,bucket_name,rule_id,status,prefix,expiration_days,filter_json,source) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(snapshot_at,bucket_name,rule_id,source) DO UPDATE SET status=excluded.status,prefix=excluded.prefix,expiration_days=excluded.expiration_days,filter_json=excluded.filter_json",
                (captured_at,bucket,str(rule.get("ID") or "unnamed"),str(rule.get("Status") or "Disabled"),prefix,int(days) if days is not None else None,json.dumps(filter_data,sort_keys=True),source),
            )
    return len(normalized)


def refresh_cost_explorer(today: date | None = None) -> int:
    if os.getenv("ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED", "").strip().lower() != "true":
        raise RuntimeError("Cost Explorer collection is disabled; enable it only with an approved read-only AWS role.")
    import boto3
    today = today or date.today()
    previous_month_start = date(today.year - (1 if today.month == 1 else 0), 12 if today.month == 1 else today.month - 1, 1)
    client = boto3.client("ce", region_name="us-east-1")
    stored = 0
    for dimension in ("RECORD_TYPE", "USAGE_TYPE"):
        token = None
        while True:
            args = {
                "TimePeriod": {"Start": previous_month_start.isoformat(), "End": today.isoformat()},
                "Granularity": "MONTHLY",
                "Metrics": ["UnblendedCost"],
                "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"},{"Type": "DIMENSION", "Key": dimension}],
            }
            if token:
                args["NextPageToken"] = token
            response = client.get_cost_and_usage(**args)
            stored += len(persist_cost_explorer_response(response, dimension_name=dimension))
            token = response.get("NextPageToken")
            if not token:
                break
    return stored


def refresh_lifecycle(bucket: str) -> int:
    if os.getenv("ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED", "").strip().lower() != "true":
        raise RuntimeError("Lifecycle collection is disabled; enable it only with an approved read-only AWS role.")
    import boto3
    from botocore.exceptions import ClientError
    region = os.getenv("AWS_REGION", "us-east-1")
    client = boto3.client("s3", region_name=region)
    try:
        response = client.get_bucket_lifecycle_configuration(Bucket=bucket)
        rules = list(response.get("Rules") or [])
    except ClientError as error:
        code = str((error.response.get("Error") or {}).get("Code") or "")
        if code not in {"NoSuchLifecycleConfiguration", "NoSuchLifecycle"}:
            raise
        rules = []
    return persist_lifecycle_rules(bucket, rules)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest read-only AWS billing and S3 Inventory snapshots.")
    parser.add_argument("--inventory-csv", type=Path)
    parser.add_argument("--inventory-schema", help="Comma-separated S3 Inventory column names from its manifest")
    parser.add_argument("--inventory-manifest-s3", help="Exact s3://bucket/key for an existing Inventory manifest; no bucket listing is used")
    parser.add_argument("--snapshot-date")
    parser.add_argument("--refresh-cost-explorer", action="store_true")
    parser.add_argument("--refresh-lifecycle", action="store_true")
    parser.add_argument("--bucket", default=os.getenv("ANYAICAM_RECORDING_S3_BUCKET", ""))
    args = parser.parse_args(argv)
    if os.getenv("ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED", "").strip().lower() != "true":
        parser.error("collector is disabled; set ANYAICAM_COST_SNAPSHOT_COLLECTOR_ENABLED=true for an approved scheduled run")
    actions = 0
    try:
        if args.inventory_csv:
            if not args.inventory_schema:
                parser.error("--inventory-schema is required with --inventory-csv")
            if not os.getenv("ANYAICAM_RECORDING_S3_BUCKET", "").strip():
                parser.error("ANYAICAM_RECORDING_S3_BUCKET must identify the expected source bucket before importing Inventory data")
            snapshot = args.snapshot_date or date.today().isoformat()
            imported = import_inventory_csv(args.inventory_csv, [name.strip() for name in args.inventory_schema.split(",")], snapshot, expected_bucket=os.getenv("ANYAICAM_RECORDING_S3_BUCKET"))
            print(f"inventory_camera_storage_aggregates={len(imported)} snapshot_date={snapshot}")
            actions += 1
        if args.inventory_manifest_s3:
            if not os.getenv("ANYAICAM_COST_DASHBOARD_AWS_READ_ENABLED", "").strip().lower() == "true":
                parser.error("S3 Inventory S3 reads are disabled; enable only with an approved read-only AWS role")
            if not args.inventory_manifest_s3.startswith("s3://") or "/" not in args.inventory_manifest_s3[5:]:
                parser.error("--inventory-manifest-s3 must be s3://bucket/key")
            source_bucket, manifest_key = args.inventory_manifest_s3[5:].split("/", 1)
            import boto3
            client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))
            imported = import_inventory_manifest_s3(client, source_bucket, manifest_key, snapshot_date=args.snapshot_date)
            snapshot = args.snapshot_date or date.today().isoformat()
            print(f"inventory_camera_storage_aggregates={len(imported)} snapshot_date={snapshot or 'from-manifest'}")
            actions += 1
        if args.refresh_cost_explorer:
            count = refresh_cost_explorer()
            print(f"cost_explorer_rows={count}")
            actions += 1
        if args.refresh_lifecycle:
            if not args.bucket:
                parser.error("--bucket is required for lifecycle collection")
            count = refresh_lifecycle(args.bucket)
            print(f"lifecycle_rules={count}")
            actions += 1
        if actions == 0:
            parser.error("select at least one data source")
        return 0
    except Exception as error:
        # Exceptions are summarized without including AWS request internals or
        # any environment-sourced credentials.
        print(f"snapshot_collection_failed={type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
