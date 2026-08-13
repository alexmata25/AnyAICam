import asyncio
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
import secrets
import smtplib
import ssl
from enum import Enum
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote, urlencode
from email.message import EmailMessage
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError, URLError

try:
    import boto3
    from boto3.s3.transfer import TransferConfig
except ImportError:
    boto3 = None
    TransferConfig = None

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from passlib.context import CryptContext

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception

try:
    import cv2
    from ultralytics import YOLO
except ImportError:
    cv2 = None
    YOLO = None

HLS_FOLDER = Path("/app/static/hls")
RECORDINGS_FOLDER = Path("/app/recordings")
CLIPS_FOLDER = RECORDINGS_FOLDER / "clips"
MEDIA_FOLDER = RECORDINGS_FOLDER / "media"
SALES_TRAINING_FOLDER = MEDIA_FOLDER / "sales-training"
SALES_TRAINING_LIBRARY_FILE = RECORDINGS_FOLDER / "sales_training_library.json"
INCIDENTS_FILE = RECORDINGS_FOLDER / "incidents.json"
LOGIN_FAILURES_FILE = RECORDINGS_FOLDER / "login_failures.json"
RATE_LIMITS_FILE = RECORDINGS_FOLDER / "rate_limits.json"
OBSERVABILITY_RETENTION_FILE = RECORDINGS_FOLDER / "observability_retention.json"
SNAPSHOTS_FOLDER = MEDIA_FOLDER / "snapshots"
MOTION_THUMBNAILS_FOLDER = MEDIA_FOLDER / "motion"
AI_THUMBNAILS_FOLDER = MEDIA_FOLDER / "ai"
MOTION_EVENTS_FILE = RECORDINGS_FOLDER / "motion_events.jsonl"
EVENT_SETTINGS_FILE = RECORDINGS_FOLDER / "event_settings.json"
ALERT_RULES_FILE = RECORDINGS_FOLDER / "alert_rules.json"
IN_APP_ALERTS_FILE = RECORDINGS_FOLDER / "in_app_alerts.jsonl"
NOTIFICATION_SETTINGS_FILE = RECORDINGS_FOLDER / "notification_settings.json"
PUSH_SUBSCRIPTIONS_FILE = RECORDINGS_FOLDER / "push_subscriptions.json"
PARTNER_CUSTOMERS_FILE = RECORDINGS_FOLDER / "partner_customers.json"
PARTNER_QUOTES_FILE = RECORDINGS_FOLDER / "partner_quotes.json"
PARTNER_INSTALLATIONS_FILE = RECORDINGS_FOLDER / "partner_installations.json"
ANALYTICS_RULES_FILE = RECORDINGS_FOLDER / "analytics_rules.json"
ANALYTICS_EVENTS_FILE = RECORDINGS_FOLDER / "analytics_events.json"
EVENT_REVIEWS_FILE = RECORDINGS_FOLDER / "event_reviews.json"
INVESTIGATION_CASES_FILE = RECORDINGS_FOLDER / "investigation_cases.json"
EVIDENCE_LEDGER_FILE = RECORDINGS_FOLDER / "evidence_ledger.json"
EVIDENCE_HASHES_FILE = RECORDINGS_FOLDER / "evidence_hashes.json"
INCIDENT_REPORTS_FILE = RECORDINGS_FOLDER / "incident_reports.json"
NOTIFICATION_RULES_FILE = RECORDINGS_FOLDER / "notification_rules.json"
NOTIFICATION_DELIVERIES_FILE = RECORDINGS_FOLDER / "notification_deliveries.json"
MOBILE_DEVICES_FILE = RECORDINGS_FOLDER / "mobile_devices.json"
MOBILE_PAIRING_CODES_FILE = RECORDINGS_FOLDER / "mobile_pairing_codes.json"
RELEASE_CHECKS_FILE = RECORDINGS_FOLDER / "release_checks.json"
MAINTENANCE_STATE_FILE = RECORDINGS_FOLDER / "maintenance_state.json"
BACKUP_JOBS_FILE = RECORDINGS_FOLDER / "backup_jobs.json"
BACKUP_RESTORE_FILE = RECORDINGS_FOLDER / "backup_restore_history.json"
LICENSE_STATE_FILE = RECORDINGS_FOLDER / "license_state.json"
LICENSE_HISTORY_FILE = RECORDINGS_FOLDER / "license_history.json"
LICENSE_ACKNOWLEDGEMENTS_FILE = RECORDINGS_FOLDER / "license_acknowledgements.json"
BILLING_ACCOUNTS_FILE = RECORDINGS_FOLDER / "billing_accounts.json"
BILLING_INVOICES_FILE = RECORDINGS_FOLDER / "billing_invoices.json"
BILLING_EVENTS_FILE = RECORDINGS_FOLDER / "billing_events.json"
SUBSCRIPTION_REQUESTS_FILE = RECORDINGS_FOLDER / "subscription_requests.json"
BILLING_SUPPORT_TICKETS_FILE = RECORDINGS_FOLDER / "billing_support_tickets.json"
PAYMENT_SESSIONS_FILE = RECORDINGS_FOLDER / "payment_sessions.json"
PAYMENT_WEBHOOK_EVENTS_FILE = RECORDINGS_FOLDER / "payment_webhook_events.json"
ONBOARDING_PROFILES_FILE = RECORDINGS_FOLDER / "onboarding_profiles.json"
ONBOARDING_ACTIVITY_FILE = RECORDINGS_FOLDER / "onboarding_activity.json"
CLOUD_UPLOAD_QUEUE_FILE = RECORDINGS_FOLDER / "cloud_upload_queue.json"
CLOUD_RECORDING_INDEX_FILE = RECORDINGS_FOLDER / "cloud_recording_index.json"
BACKUP_FOLDER = RECORDINGS_FOLDER / "backups"
USERS_FILE = RECORDINGS_FOLDER / "users.json"
USER_INVITES_FILE = RECORDINGS_FOLDER / "user_invites.json"
AUDIT_LOG_FILE = RECORDINGS_FOLDER / "audit_log.jsonl"
SESSIONS_FILE = RECORDINGS_FOLDER / "sessions.json"
SESSION_COOKIE_NAME = "anyaicam_session"
SESSION_MAX_AGE_SECONDS = int(os.environ.get("ANYAICAM_SESSION_HOURS", "12")) * 3600
MAX_LOGIN_ATTEMPTS = int(os.environ.get("ANYAICAM_MAX_LOGIN_ATTEMPTS", "5"))
LOCKOUT_MINUTES = int(os.environ.get("ANYAICAM_LOCKOUT_MINUTES", "15"))
SECURE_COOKIES = os.environ.get("ANYAICAM_SECURE_COOKIES", "true").lower() == "true"
BACKUPS_FOLDER = RECORDINGS_FOLDER / "backups"
RESTORE_STAGING_FOLDER = RECORDINGS_FOLDER / "restore_staging"
APP_VERSION = os.environ.get("ANYAICAM_VERSION", "0.9.0")
BUILD_ID = os.environ.get("ANYAICAM_BUILD_ID", "local")
PUBLIC_BASE_URL = os.environ.get("ANYAICAM_PUBLIC_URL", "").rstrip("/")
PHONE_ACCESS_URL = os.environ.get("ANYAICAM_PHONE_URL", "").strip().rstrip("/")
DEPLOYMENT_ENV = os.environ.get("ANYAICAM_ENV", "local").strip().lower()
RUNTIME_ROLE = os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "")).strip()
DATABASE_URL = os.environ.get("ANYAICAM_DATABASE_URL", "").strip()
S3_BUCKET = os.environ.get("ANYAICAM_S3_BUCKET", "").strip()
S3_PREFIX = os.environ.get("ANYAICAM_S3_PREFIX", "anyaicam").strip().strip("/")
SES_SENDER = os.environ.get("ANYAICAM_SES_SENDER", "").strip()
SECRETS_MANAGER_SECRET_ID = os.environ.get("ANYAICAM_SECRETS_SECRET_ID", "").strip()
CLOUDFRONT_URL = os.environ.get("ANYAICAM_CLOUDFRONT_URL", "").strip().rstrip("/")
CLOUD_UPLOAD_ENABLED = os.environ.get("ANYAICAM_CLOUD_UPLOAD_ENABLED", "false").lower() == "true"
CLOUD_UPLOAD_MAX_RETRIES = max(1, int(os.environ.get("ANYAICAM_CLOUD_UPLOAD_MAX_RETRIES", "5")))
CLOUD_UPLOAD_RETRY_SECONDS = max(5, int(os.environ.get("ANYAICAM_CLOUD_UPLOAD_RETRY_SECONDS", "60")))
CLOUD_UPLOAD_SCAN_SECONDS = max(10, int(os.environ.get("ANYAICAM_CLOUD_UPLOAD_SCAN_SECONDS", "30")))
CLOUD_UPLOAD_MIN_FILE_AGE_SECONDS = max(30, int(os.environ.get("ANYAICAM_CLOUD_UPLOAD_MIN_FILE_AGE_SECONDS", "90")))
CLOUD_UPLOAD_DELETE_LOCAL = os.environ.get("ANYAICAM_CLOUD_UPLOAD_DELETE_LOCAL", "false").lower() == "true"
CLOUD_UPLOAD_STORAGE_CLASS = os.environ.get("ANYAICAM_CLOUD_UPLOAD_STORAGE_CLASS", "STANDARD").strip().upper()
CLOUD_UPLOAD_SSE = os.environ.get("ANYAICAM_CLOUD_UPLOAD_SSE", "AES256").strip()
CLOUD_UPLOAD_KMS_KEY_ID = os.environ.get("ANYAICAM_CLOUD_UPLOAD_KMS_KEY_ID", "").strip()
CLOUD_UPLOAD_MULTIPART_THRESHOLD_MB = max(8, int(os.environ.get("ANYAICAM_CLOUD_UPLOAD_MULTIPART_THRESHOLD_MB", "64")))
FORCE_HTTPS = os.environ.get(
    "ANYAICAM_FORCE_HTTPS",
    "true" if DEPLOYMENT_ENV == "production" else "false",
).lower() == "true"
TRUST_PROXY_HEADERS = os.environ.get("ANYAICAM_TRUST_PROXY_HEADERS", "true").lower() == "true"
ALLOWED_ORIGINS = [
    item.strip().rstrip("/")
    for item in os.environ.get(
        "ANYAICAM_ALLOWED_ORIGINS",
        PUBLIC_BASE_URL or "http://localhost:8000",
    ).split(",")
    if item.strip()
]
WEB_CONCURRENCY = max(1, int(os.environ.get("WEB_CONCURRENCY", "1")))
RELEASE_CANDIDATE = os.environ.get("ANYAICAM_RELEASE_CANDIDATE", "true").lower() == "true"
SUPPORT_BUNDLE_MAX_LOG_BYTES = max(
    1024,
    int(os.environ.get("ANYAICAM_SUPPORT_BUNDLE_MAX_LOG_BYTES", "1048576")),
)
BACKUP_RETENTION_COUNT = max(
    1,
    int(os.environ.get("ANYAICAM_BACKUP_RETENTION_COUNT", "10")),
)
BACKUP_INCLUDE_RECORDINGS = os.environ.get(
    "ANYAICAM_BACKUP_INCLUDE_RECORDINGS",
    "false",
).lower() == "true"
BACKUP_INCLUDE_HLS = os.environ.get(
    "ANYAICAM_BACKUP_INCLUDE_HLS",
    "false",
).lower() == "true"
SMTP_HOST = os.environ.get("ANYAICAM_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("ANYAICAM_SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("ANYAICAM_SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("ANYAICAM_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("ANYAICAM_SMTP_FROM", SMTP_USERNAME).strip()
SMTP_USE_TLS = os.environ.get("ANYAICAM_SMTP_USE_TLS", "true").lower() == "true"
VAPID_PUBLIC_KEY = os.environ.get("ANYAICAM_VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("ANYAICAM_VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.environ.get("ANYAICAM_VAPID_SUBJECT", "mailto:admin@localhost").strip()
STARTED_AT = datetime.now()
application_logger = logging.getLogger("anyaicam")


def structured_log(event: str, level: str = "info", **fields) -> None:
    payload = {
        "timestamp": datetime.now().isoformat(),
        "service": "anyaicam-vms",
        "environment": DEPLOYMENT_ENV,
        "runtime_role": RUNTIME_ROLE,
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "event": event,
        **fields,
    }
    message = json.dumps(payload, default=str, separators=(",", ":"))
    getattr(application_logger, level.lower(), application_logger.info)(message)


CAMERA_COUNT = 4

DEFAULT_LICENSE_PLAN = os.environ.get(
    "ANYAICAM_LICENSE_PLAN",
    "professional",
).strip().lower()
DEFAULT_LICENSE_CAMERA_LIMIT = max(
    1,
    int(os.environ.get("ANYAICAM_LICENSE_CAMERA_LIMIT", str(CAMERA_COUNT))),
)
DEFAULT_LICENSE_TRIAL_DAYS = max(
    0,
    int(os.environ.get("ANYAICAM_LICENSE_TRIAL_DAYS", "30")),
)
LICENSE_GRACE_DAYS = max(
    0,
    int(os.environ.get("ANYAICAM_LICENSE_GRACE_DAYS", "14")),
)
LICENSE_ENFORCEMENT_MODE = os.environ.get(
    "ANYAICAM_LICENSE_ENFORCEMENT_MODE",
    "warning",
).strip().lower()
if LICENSE_ENFORCEMENT_MODE not in {"off", "warning"}:
    LICENSE_ENFORCEMENT_MODE = "warning"

BILLING_PROVIDER = os.environ.get(
    "ANYAICAM_BILLING_PROVIDER",
    "manual",
).strip().lower()
BILLING_CURRENCY = os.environ.get(
    "ANYAICAM_BILLING_CURRENCY",
    "USD",
).strip().upper()
BILLING_WEBHOOK_SECRET_NAME = os.environ.get(
    "ANYAICAM_BILLING_WEBHOOK_SECRET_NAME",
    "",
).strip()

STRIPE_SECRET_KEY = os.environ.get("ANYAICAM_STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("ANYAICAM_STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_STARTER = os.environ.get("ANYAICAM_STRIPE_PRICE_STARTER", "").strip()
STRIPE_PRICE_PROFESSIONAL = os.environ.get("ANYAICAM_STRIPE_PRICE_PROFESSIONAL", "").strip()
STRIPE_PRICE_ENTERPRISE = os.environ.get("ANYAICAM_STRIPE_PRICE_ENTERPRISE", "").strip()
STRIPE_PRICE_IDS = {
    "starter": STRIPE_PRICE_STARTER,
    "professional": STRIPE_PRICE_PROFESSIONAL,
    "enterprise": STRIPE_PRICE_ENTERPRISE,
}
STRIPE_API_BASE = os.environ.get(
    "ANYAICAM_STRIPE_API_BASE",
    "https://api.stripe.com",
).strip().rstrip("/")
STRIPE_WEBHOOK_TOLERANCE_SECONDS = max(
    60,
    int(os.environ.get("ANYAICAM_STRIPE_WEBHOOK_TOLERANCE_SECONDS", "300")),
)
PARTNER_COMMISSION_PERCENT = max(
    0.0,
    min(100.0, float(os.environ.get("ANYAICAM_PARTNER_COMMISSION_PERCENT", "10"))),
)


HLS_FOLDER.mkdir(parents=True, exist_ok=True)
RECORDINGS_FOLDER.mkdir(parents=True, exist_ok=True)
CLIPS_FOLDER.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_FOLDER.mkdir(parents=True, exist_ok=True)
SALES_TRAINING_FOLDER.mkdir(parents=True, exist_ok=True)
MOTION_THUMBNAILS_FOLDER.mkdir(parents=True, exist_ok=True)
AI_THUMBNAILS_FOLDER.mkdir(parents=True, exist_ok=True)
BACKUPS_FOLDER.mkdir(parents=True, exist_ok=True)
RESTORE_STAGING_FOLDER.mkdir(parents=True, exist_ok=True)

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
MOTION_DETECTION_ENABLED = os.environ.get("MOTION_DETECTION_ENABLED", "true").lower() == "true"
MOTION_THRESHOLD = float(os.environ.get("MOTION_THRESHOLD", "12"))
MOTION_COOLDOWN_SECONDS = int(os.environ.get("MOTION_COOLDOWN_SECONDS", "15"))
AI_PERSON_DETECTION_ENABLED = os.environ.get("AI_PERSON_DETECTION_ENABLED", "true").lower() == "true"
AI_DETECTION_INTERVAL_SECONDS = max(2, int(os.environ.get("AI_DETECTION_INTERVAL_SECONDS", "5")))
AI_PERSON_COOLDOWN_SECONDS = max(5, int(os.environ.get("AI_PERSON_COOLDOWN_SECONDS", "30")))
YOLO_MODEL_NAME = os.environ.get("ANYAICAM_YOLO_MODEL", "yolov8n.pt")
YOLO_CONFIDENCE = float(os.environ.get("ANYAICAM_YOLO_CONFIDENCE", "0.35"))
YOLO_IMAGE_SIZE = int(os.environ.get("ANYAICAM_YOLO_IMAGE_SIZE", "640"))
YOLO_DEVICE = os.environ.get("ANYAICAM_YOLO_DEVICE", "cpu")
YOLO_ALLOWED_CLASSES = {
    item.strip().lower()
    for item in os.environ.get(
        "ANYAICAM_YOLO_CLASSES",
        "person,car,truck,bus,motorcycle,bicycle,dog,cat,bird,backpack,suitcase",
    ).split(",")
    if item.strip()
}
ffmpeg_processes: list[subprocess.Popen] = []
camera_process_state = {
    camera_number: {"live": "starting", "recording": "starting"}
    for camera_number in range(1, CAMERA_COUNT + 1)
}
camera_reconnect_counts = {camera_number: 0 for camera_number in range(1, CAMERA_COUNT + 1)}
health_issues: dict[str, dict] = {}
health_alert_times: dict[str, float] = {}
ai_detection_state = {
    camera_number: {
        "status": "starting",
        "last_checked": None,
        "last_detection": None,
        "detections": 0,
        "error": None,
    }
    for camera_number in range(1, CAMERA_COUNT + 1)
}
ai_person_last_event = {
    camera_number: 0.0 for camera_number in range(1, CAMERA_COUNT + 1)
}
yolo_model = None
yolo_model_lock = None


class HealthState(str, Enum):
    online = "online"
    offline = "offline"
    warning = "warning"


class SiteModel(BaseModel):
    id: str
    name: str
    camera_ids: list[int] = Field(default_factory=list)
    health: HealthState = HealthState.online


class CameraModel(BaseModel):
    id: int
    name: str
    site_id: str = "home"
    enabled: bool = True
    health: HealthState = HealthState.offline


class UserModel(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    display_name: str = Field(min_length=2, max_length=120)
    email: str = Field(default="", max_length=200)
    role: str = "view-only"
    enabled: bool = True
    super_admin: bool = False
    site_ids: list[str] = Field(default_factory=lambda: ["home"])
    camera_ids: list[int] = Field(default_factory=list)
    password_hash: str = ""
    invitation_status: str = "active"
    failed_login_attempts: int = 0
    locked_until: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    last_login: datetime | None = None


class UserUpdateModel(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=120)
    email: str | None = Field(default=None, max_length=200)
    role: str | None = None
    enabled: bool | None = None
    super_admin: bool | None = None
    site_ids: list[str] | None = None
    camera_ids: list[int] | None = None


class UserCreateModel(BaseModel):
    display_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=10, max_length=256)
    role: str = "view-only"
    enabled: bool = True
    super_admin: bool = False
    site_ids: list[str] = Field(default_factory=lambda: ["home"])
    camera_ids: list[int] = Field(default_factory=list)


class BusinessRegistrationModel(BaseModel):
    display_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=10, max_length=256)
    requested_role: str


class UserInviteCreateModel(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    display_name: str = Field(default="", max_length=120)
    role: str = "view-only"
    super_admin: bool = False
    all_cameras: bool = True
    camera_ids: list[int] = Field(default_factory=list)


class UserInviteAcceptModel(BaseModel):
    token: str
    display_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=10, max_length=256)


class PasswordChangeModel(BaseModel):
    password: str = Field(min_length=10, max_length=256)


class NotificationSettingsModel(BaseModel):
    email_enabled: bool = False
    push_enabled: bool = False
    recipient_email: str = ""
    event_types: list[str] = Field(
        default_factory=lambda: [
            "person", "car", "truck", "bus", "motorcycle",
            "motion", "stream_offline", "recording_stopped",
            "low_disk_space", "login_failure",
        ]
    )
    quiet_hours_enabled: bool = False
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"


class PushSubscriptionModel(BaseModel):
    endpoint: str
    keys: dict
    user_agent: str = ""


class AuditEntryModel(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: datetime = Field(default_factory=datetime.now)
    user_id: str
    user_name: str
    role: str
    action: str
    resource: str
    detail: str = ""
    device: str = "Web browser"
    outcome: str = "success"


class AnalyticsModel(BaseModel):
    id: str
    camera_id: int
    analytics_type: str
    enabled: bool = False


class AlertModel(BaseModel):
    id: str
    camera_id: int | None = None
    alert_type: str
    created_at: datetime
    acknowledged: bool = False


class NotificationModel(BaseModel):
    id: str
    alert_id: str
    channel: str
    delivered: bool = False


class MotionEventModel(BaseModel):
    id: str
    camera: int
    start_time: datetime
    end_time: datetime
    confidence: float
    thumbnail: str | None = None
    site: str = "home"
    linked_recording: str | None = None
    event_type: str = "motion"


class MotionZoneModel(BaseModel):
    name: str = "Full frame"
    x: float = Field(default=0, ge=0, le=1)
    y: float = Field(default=0, ge=0, le=1)
    width: float = Field(default=1, gt=0, le=1)
    height: float = Field(default=1, gt=0, le=1)


class EventSettingsModel(BaseModel):
    camera: int = Field(ge=1, le=CAMERA_COUNT)
    enabled: bool = True
    sensitivity: int = Field(default=60, ge=1, le=100)
    minimum_duration_seconds: int = Field(default=2, ge=1, le=60)
    cooldown_seconds: int = Field(default=15, ge=0, le=3600)
    zones: list[MotionZoneModel] = Field(default_factory=lambda: [MotionZoneModel()])


class AlertRuleModel(BaseModel):
    camera: int = Field(ge=1, le=CAMERA_COUNT)
    enabled: bool = True
    event_types: list[str] = Field(default_factory=lambda: ["motion"])
    schedule_start: str = "00:00"
    schedule_end: str = "23:59"
    delivery_methods: list[str] = Field(default_factory=lambda: ["in_app"])


class PartnerCustomerModel(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    status: str = "active"
    site_name: str = "Primary site"
    created_at: datetime = Field(default_factory=datetime.now)



class PartnerQuoteCreateModel(BaseModel):
    customer_id: str
    quote_name: str = "AnyAiCam proposal"
    deployment_type: str = "software"
    camera_quantity: int = Field(default=1, ge=1, le=128)
    resolution: str = "2mp"
    recording_mode: str = "motion"
    retention_days: int = Field(default=7, ge=1, le=365)
    analytics: list[str] = Field(default_factory=list)
    hardware_items: list[dict] = Field(default_factory=list)
    notes: str = ""
    status: str = "draft"


class PartnerQuoteUpdateModel(BaseModel):
    quote_name: str | None = None
    deployment_type: str | None = None
    camera_quantity: int | None = Field(default=None, ge=1, le=128)
    resolution: str | None = None
    recording_mode: str | None = None
    retention_days: int | None = Field(default=None, ge=1, le=365)
    analytics: list[str] | None = None
    hardware_items: list[dict] | None = None
    notes: str | None = None
    status: str | None = None



class PartnerInstallationCreateModel(BaseModel):
    customer_id: str
    quote_id: str = ""
    installer_name: str = ""
    deployment_type: str = "software"
    cloud_id: str = ""
    expected_camera_count: int = Field(default=1, ge=1, le=128)
    checklist: dict = Field(default_factory=dict)
    notes: str = ""
    status: str = "not_started"


class PartnerInstallationUpdateModel(BaseModel):
    quote_id: str | None = None
    installer_name: str | None = None
    deployment_type: str | None = None
    cloud_id: str | None = None
    expected_camera_count: int | None = Field(default=None, ge=1, le=128)
    checklist: dict | None = None
    notes: str | None = None
    status: str | None = None



class IncidentCreateModel(BaseModel):
    title: str
    severity: str = "medium"
    category: str = "operations"
    source: str = "manual"
    description: str = ""
    assigned_to: str = ""
    status: str = "open"


class IncidentUpdateModel(BaseModel):
    title: str | None = None
    severity: str | None = None
    category: str | None = None
    source: str | None = None
    description: str | None = None
    assigned_to: str | None = None
    status: str | None = None
    resolution: str | None = None


class ObservabilityRetentionModel(BaseModel):
    audit_days: int = Field(default=365, ge=7, le=3650)
    security_days: int = Field(default=365, ge=7, le=3650)
    health_days: int = Field(default=180, ge=7, le=3650)
    incident_days: int = Field(default=730, ge=30, le=3650)


class AnalyticsRuleModel(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    camera: int = Field(ge=1, le=CAMERA_COUNT)
    site: str = "home"
    name: str = Field(min_length=2, max_length=120)
    analytic_type: str
    enabled: bool = True
    direction: str = "both"
    sensitivity: int = Field(default=60, ge=1, le=100)
    confidence_threshold: float = Field(default=0.6, ge=0, le=1)
    schedule_start: str = "00:00"
    schedule_end: str = "23:59"
    retention_days: int = Field(default=30, ge=1, le=3650)
    alerts_enabled: bool = True
    geometry: list[dict] = Field(default_factory=list)


class AnalyticsEventModel(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    camera: int
    site: str = "home"
    rule_name: str
    event_type: str
    direction: str | None = None
    timestamp: datetime = Field(default_factory=datetime.now)
    confidence: float
    thumbnail: str | None = None
    linked_recording: str | None = None
    plate_number: str | None = None
    vehicle_color: str | None = None
    vehicle_image: str | None = None
    plate_crop: str | None = None
    mock: bool = True


class EventReviewModel(BaseModel):
    event_id: str
    acknowledged: bool = False
    bookmarked: bool = False
    false_positive: bool = False
    tags: list[str] = Field(default_factory=list)
    notes: str = ""

class InvestigationCaseCreateModel(BaseModel):
    title: str
    description: str = ""
    priority: str = "normal"
    status: str = "open"
    assigned_to: str = ""
    tags: list[str] = []
    notes: str = ""
    event_ids: list[str] = []


class InvestigationCaseUpdateModel(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: str | None = None
    status: str | None = None
    assigned_to: str | None = None
    tags: list[str] | None = None
    notes: str | None = None
    event_ids: list[str] | None = None

class EvidenceVerifyModel(BaseModel):
    evidence_id: str


class EvidenceActionModel(BaseModel):
    evidence_id: str
    action: str
    case_id: str = ""
    event_id: str = ""
    details: str = ""

class IncidentReportCreateModel(BaseModel):
    case_id: str
    title: str = ""
    incident_summary: str = ""
    investigator_observations: str = ""
    recommended_follow_up: str = ""


class IncidentReportUpdateModel(BaseModel):
    title: str | None = None
    incident_summary: str | None = None
    investigator_observations: str | None = None
    recommended_follow_up: str | None = None
    status: str | None = None

class NotificationRuleCreateModel(BaseModel):
    name: str
    enabled: bool = True
    event_types: list[str] = []
    camera_ids: list[int] = []
    minimum_confidence: float = 0.0
    severity: str = "normal"
    channels: list[str] = []
    recipients: list[str] = []
    quiet_hours_start: str = ""
    quiet_hours_end: str = ""
    cooldown_seconds: int = 300


class NotificationRuleUpdateModel(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    event_types: list[str] | None = None
    camera_ids: list[int] | None = None
    minimum_confidence: float | None = None
    severity: str | None = None
    channels: list[str] | None = None
    recipients: list[str] | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    cooldown_seconds: int | None = None


class NotificationTestModel(BaseModel):
    rule_id: str

class MobilePairingCreateModel(BaseModel):
    device_name: str = "Mobile device"


class MobilePairingClaimModel(BaseModel):
    code: str
    device_name: str
    platform: str = "web"
    push_subscription: dict = {}


class MobileDeviceUpdateModel(BaseModel):
    device_name: str | None = None
    notifications_enabled: bool | None = None
    revoked: bool | None = None

class MaintenanceModeModel(BaseModel):
    enabled: bool
    message: str = "Scheduled maintenance is in progress."

class BackupCreateModel(BaseModel):
    label: str = ""
    include_recordings: bool = False
    include_hls: bool = False


class BackupRestoreModel(BaseModel):
    backup_id: str
    confirm: str

class LicenseUpdateModel(BaseModel):
    plan: str
    camera_limit: int
    status: str = "active"
    trial_ends_at: str = ""
    subscription_ends_at: str = ""
    features: list[str] = []
    customer_reference: str = ""
    notes: str = ""


class LicenseValidationModel(BaseModel):
    camera_count: int | None = None

class LicenseAcknowledgementModel(BaseModel):
    warning_code: str
    note: str = ""


class LicenseFeatureCheckModel(BaseModel):
    feature: str

class BillingAccountUpdateModel(BaseModel):
    customer_name: str
    billing_email: str
    company_name: str = ""
    external_customer_id: str = ""
    payment_status: str = "current"
    billing_cycle: str = "monthly"
    next_billing_date: str = ""
    notes: str = ""


class BillingInvoiceCreateModel(BaseModel):
    account_id: str
    description: str
    amount_cents: int
    due_date: str = ""
    status: str = "draft"


class BillingInvoiceUpdateModel(BaseModel):
    status: str
    paid_at: str = ""
    external_invoice_id: str = ""
    notes: str = ""

class SubscriptionRequestCreateModel(BaseModel):
    request_type: str
    requested_plan: str = ""
    requested_camera_limit: int | None = None
    reason: str = ""


class SubscriptionRequestUpdateModel(BaseModel):
    status: str
    admin_note: str = ""


class BillingSupportTicketCreateModel(BaseModel):
    subject: str
    message: str
    priority: str = "normal"


class BillingSupportTicketUpdateModel(BaseModel):
    status: str
    admin_note: str = ""

class StripeCheckoutCreateModel(BaseModel):
    plan: str
    quantity: int = 1


class StripePortalCreateModel(BaseModel):
    return_path: str = "/subscription-portal"

class OnboardingProfileUpdateModel(BaseModel):
    organization_name: str = ""
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    site_name: str = ""
    site_address: str = ""
    timezone_name: str = ""
    deployment_type: str = "edge"
    expected_camera_count: int = 1
    camera_manufacturer: str = ""
    cloud_recording_requested: bool = False
    retention_days_requested: int = 30
    edge_device_name: str = ""
    edge_device_platform: str = "windows"
    cloud_id: str = ""
    cloud_id_device_type: str = "software_bridge"
    team_emails: list[str] = []
    terms_accepted: bool = False
    notes: str = ""


class OnboardingStepModel(BaseModel):
    step: str
    completed: bool


class OnboardingAdminUpdateModel(BaseModel):
    status: str
    admin_note: str = ""
    assigned_to: str = ""


class CloudIdAdminReviewModel(BaseModel):
    decision: str
    note: str = ""

class CloudUploadQueueModel(BaseModel):
    path: str
    camera_number: int | None = None


class CloudUploadRetryModel(BaseModel):
    job_id: str
















class NaturalSearchModel(BaseModel):
    query: str = Field(min_length=2, max_length=500)


class SnapshotRequest(BaseModel):
    camera: int = Field(ge=1, le=CAMERA_COUNT)
    image_data: str
    site: str = "home"


class ClipRequest(BaseModel):
    camera: int = Field(ge=1, le=CAMERA_COUNT)
    start_time: datetime
    end_time: datetime


clip_jobs: dict[str, dict] = {}
clip_tasks: set[asyncio.Task] = set()
motion_event_lock = asyncio.Lock()

ROLE_PERMISSIONS = {
    # ANY AI CAM business portal roles.
    "administrator": {
        "view_live", "view_events", "view_analytics", "view_sites",
        "manage_users", "manage_settings", "view_audit", "export_media",
        "acknowledge_alerts",
    },
    "support_admin": {
        "view_live", "view_events", "view_analytics", "view_sites",
        "manage_users", "manage_settings", "view_audit", "export_media",
        "acknowledge_alerts",
    },
    "partner_admin": {
        "view_sites", "export_media",
    },
    "partner_sales": {
        "view_sites", "export_media",
    },
    "customer_owner": {
        "view_live", "view_events", "view_analytics", "view_sites",
        "export_media", "acknowledge_alerts",
    },
    "customer_viewer": {
        "view_live", "view_events", "view_analytics", "view_sites",
    },
    "admin": {
        "view_live", "view_events", "view_analytics", "view_sites",
        "manage_users", "manage_settings", "view_audit", "export_media",
        "acknowledge_alerts",
    },
    "read-only": {
        "view_live", "view_events", "view_analytics", "view_sites",
        "export_media",
    },
    "view-only": {
        "view_live", "view_events", "view_analytics", "view_sites",
    },
    "live-only": {
        "view_live", "view_sites",
    },
    "none": set(),
    # Legacy roles remain valid for existing installations.
    "installer": {
        "view_live", "view_events", "view_analytics", "view_sites",
        "manage_settings", "view_audit",
    },
    "operator": {
        "view_live", "view_events", "view_analytics", "view_sites",
        "export_media", "acknowledge_alerts",
    },
    "viewer": {"view_live", "view_events", "view_sites"},
}
VALID_ROLES = set(ROLE_PERMISSIONS)


password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
session_serializer = URLSafeTimedSerializer(
    os.environ.get("ANYAICAM_PORTAL_SECRET", secrets.token_urlsafe(48)),
    salt="anyaicam-vms-session",
)


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return password_context.verify(password, password_hash)
    except (ValueError, TypeError):
        return False


def default_users() -> list[dict]:
    admin_email = os.environ.get("ANYAICAM_ADMIN_EMAIL", "admin@local").strip().lower()
    admin_password = os.environ.get("ANYAICAM_ADMIN_PASSWORD", "")
    return [
        UserModel(
            id="local-admin",
            display_name="Local Administrator",
            email=admin_email,
            role="administrator",
            enabled=True,
            super_admin=True,
            site_ids=["home"],
            camera_ids=list(range(1, CAMERA_COUNT + 1)),
            password_hash=hash_password(admin_password) if admin_password else "",
        ).model_dump(mode="json")
    ]


def migrate_users(users: list[dict]) -> tuple[list[dict], bool]:
    changed = False
    admin_email = os.environ.get("ANYAICAM_ADMIN_EMAIL", "admin@local").strip().lower()
    admin_password = os.environ.get("ANYAICAM_ADMIN_PASSWORD", "")
    for user in users:
        user.setdefault("password_hash", "")
        user.setdefault("failed_login_attempts", 0)
        user.setdefault("locked_until", None)
        user.setdefault("site_ids", ["home"])
        user.setdefault("camera_ids", [])
        user.setdefault("super_admin", user.get("role") in {"admin", "administrator", "support_admin"})
        user.setdefault("invitation_status", "active")
        if user.get("id") == "local-admin":
            if not user.get("email"):
                user["email"] = admin_email
                changed = True
            if not user.get("password_hash") and admin_password:
                user["password_hash"] = hash_password(admin_password)
                changed = True
    return users, changed


def load_users() -> list[dict]:
    try:
        if USERS_FILE.exists():
            data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                users, changed = migrate_users(data)
                if changed:
                    save_users(users)
                return users
    except (OSError, json.JSONDecodeError):
        pass
    users = default_users()
    try:
        save_users(users)
    except OSError:
        pass
    return users


def save_users(users: list[dict]) -> None:
    temporary = USERS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(users, indent=2), encoding="utf-8")
    temporary.replace(USERS_FILE)



def load_user_invites() -> list[dict]:
    try:
        if USER_INVITES_FILE.exists():
            data = json.loads(USER_INVITES_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_user_invites(invites: list[dict]) -> None:
    temporary = USER_INVITES_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(invites, indent=2), encoding="utf-8")
    temporary.replace(USER_INVITES_FILE)


def invite_base_url(request: Request | None = None) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://localhost:8000"


def send_user_invitation_email(invite: dict, request: Request | None = None) -> tuple[bool, str]:
    if not SMTP_HOST or not SMTP_FROM:
        return False, "SMTP is not configured."
    invite_url = f"{invite_base_url(request)}/accept-invite?token={quote(invite['token'])}"
    message = EmailMessage()
    message["Subject"] = "You have been invited to AnyAiCam"
    message["From"] = SMTP_FROM
    message["To"] = invite["email"]
    message.set_content(
        "\n".join(
            [
                "You have been invited to access AnyAiCam.",
                "",
                f"Permission level: {invite['role']}",
                f"Camera access: {'All cameras' if invite.get('all_cameras') else ', '.join('Camera ' + str(item) for item in invite.get('camera_ids', []))}",
                "",
                f"Create your account: {invite_url}",
                "",
                f"This invitation expires {invite['expires_at']}.",
            ]
        )
    )
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls(context=context)
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
        return True, "Invitation email sent."
    except (OSError, smtplib.SMTPException) as error:
        return False, str(error)


def load_sessions() -> dict:
    try:
        if SESSIONS_FILE.exists():
            data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_sessions(sessions: dict) -> None:
    temporary = SESSIONS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    temporary.replace(SESSIONS_FILE)


def create_session(user_id: str, remember_me: bool = False) -> str:
    session_id = secrets.token_urlsafe(32)
    sessions = load_sessions()
    max_age = 30 * 24 * 3600 if remember_me else SESSION_MAX_AGE_SECONDS
    sessions[session_id] = {
        "user_id": user_id,
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(seconds=max_age)).isoformat(),
    }
    save_sessions(sessions)
    return session_serializer.dumps({"session_id": session_id})


def destroy_session(token: str | None) -> None:
    if not token:
        return
    try:
        payload = session_serializer.loads(token, max_age=30 * 24 * 3600)
    except (BadSignature, SignatureExpired):
        return
    session_id = payload.get("session_id")
    sessions = load_sessions()
    if session_id in sessions:
        sessions.pop(session_id, None)
        save_sessions(sessions)


def authenticated_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        payload = session_serializer.loads(token, max_age=30 * 24 * 3600)
    except (BadSignature, SignatureExpired):
        return None
    session_id = payload.get("session_id")
    session = load_sessions().get(session_id)
    if not session:
        return None
    try:
        if datetime.fromisoformat(session["expires_at"]) <= datetime.now():
            destroy_session(token)
            return None
    except (KeyError, TypeError, ValueError):
        return None
    user = next(
        (
            item for item in load_users()
            if item.get("id") == session.get("user_id") and item.get("enabled", True)
        ),
        None,
    )
    return user


def current_user(request: Request) -> dict:
    return authenticated_user(request) or {
        "id": "anonymous",
        "display_name": "Unauthenticated",
        "email": "",
        "role": "viewer",
        "enabled": False,
        "site_ids": [],
        "camera_ids": [],
    }


def has_permission(user: dict, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(user.get("role", "viewer"), set())


def user_camera_ids(user: dict) -> list[int]:
    """Return the cameras available to this user without changing permissions."""
    raw_camera_ids = user.get("camera_ids") or []

    # Existing user records use an empty camera list to mean all cameras.
    if user.get("super_admin") or user.get("role") == "admin" or not raw_camera_ids:
        return list(range(1, CAMERA_COUNT + 1))

    allowed = []
    for camera_id in raw_camera_ids:
        try:
            camera_number = int(camera_id)
        except (TypeError, ValueError):
            continue
        if 1 <= camera_number <= CAMERA_COUNT:
            allowed.append(camera_number)

    return sorted(set(allowed))


def permission_denied_page(title: str, active: str, permission: str) -> str:
    content = (
        f'<header class="topbar"><div><p class="eyebrow">Access control</p>'
        f'<h1>{escape(title)}</h1></div></header>'
        f'<section class="panel"><div class="empty">'
        f'Your current role does not include <strong>{escape(permission)}</strong>. '
        f'Ask an administrator to update your access.</div></section>'
    )
    return page_shell(title, active, content)


def append_audit_entry(entry: dict) -> None:
    with AUDIT_LOG_FILE.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(entry, separators=(",", ":")) + "\n")


def record_audit(
    request: Request,
    action: str,
    resource: str,
    detail: str = "",
    outcome: str = "success",
) -> None:
    user = current_user(request)
    entry = AuditEntryModel(
        user_id=user.get("id", "unknown"),
        user_name=user.get("display_name", "Unknown user"),
        role=user.get("role", "viewer"),
        action=action,
        resource=resource,
        detail=detail,
        device=request.headers.get("user-agent", "Web browser")[:160],
        outcome=outcome,
    )
    try:
        append_audit_entry(entry.model_dump(mode="json"))
    except OSError:
        pass


def load_audit_entries(limit: int = 1000) -> list[dict]:
    entries: list[dict] = []
    try:
        if AUDIT_LOG_FILE.exists():
            for line in AUDIT_LOG_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    entries.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    return entries[:max(1, min(limit, 5000))]




CONFIG_BACKUP_FILES = [
    EVENT_SETTINGS_FILE,
    ALERT_RULES_FILE,
    PARTNER_CUSTOMERS_FILE,
    ANALYTICS_RULES_FILE,
    ANALYTICS_EVENTS_FILE,
    EVENT_REVIEWS_FILE,
    USERS_FILE,
    USER_INVITES_FILE,
    AUDIT_LOG_FILE,
    IN_APP_ALERTS_FILE,
    NOTIFICATION_SETTINGS_FILE,
    PUSH_SUBSCRIPTIONS_FILE,
    MOTION_EVENTS_FILE,
]


def configuration_issues() -> list[dict]:
    issues: list[dict] = []
    required_global = ["ANYAICAM_ADMIN_EMAIL", "ANYAICAM_ADMIN_PASSWORD", "ANYAICAM_PORTAL_SECRET"]
    for key in required_global:
        value = os.environ.get(key, "")
        if not value.strip():
            issues.append({"key": key, "severity": "critical", "message": f"{key} is missing."})
        elif value != value.strip():
            issues.append({"key": key, "severity": "warning", "message": f"{key} has leading or trailing spaces."})

    for camera in range(1, CAMERA_COUNT + 1):
        for suffix in ("HOST", "USERNAME", "PASSWORD"):
            key = f"CAMERA{camera}_{suffix}"
            if not os.environ.get(key, "").strip():
                issues.append({"key": key, "severity": "critical", "message": f"{key} is missing."})

    if RETENTION_DAYS < 1:
        issues.append({"key": "RETENTION_DAYS", "severity": "critical", "message": "RETENTION_DAYS must be at least 1."})

    max_recording_gb = os.environ.get("MAX_RECORDING_GB", "")
    if max_recording_gb:
        try:
            if float(max_recording_gb) <= 0:
                raise ValueError
        except ValueError:
            issues.append({"key": "MAX_RECORDING_GB", "severity": "warning", "message": "MAX_RECORDING_GB must be a positive number."})

    if DEPLOYMENT_ENV not in {"local", "staging", "production"}:
        issues.append({
            "key": "ANYAICAM_ENV",
            "severity": "critical",
            "message": "ANYAICAM_ENV must be local, staging, or production.",
        })
    if RUNTIME_ROLE not in {"edge", "cloud", "combined"}:
        issues.append({
            "key": "ANYAICAM_RUNTIME_ROLE",
            "severity": "critical",
            "message": "ANYAICAM_RUNTIME_ROLE must be edge, cloud, or combined.",
        })
    if DEPLOYMENT_ENV in {"staging", "production"}:
        cloud_checks = cloud_configuration_snapshot()
        for key in cloud_checks["missing_cloud_requirements"]:
            issues.append({
                "key": key,
                "severity": "warning" if DEPLOYMENT_ENV == "staging" else "critical",
                "message": f"AWS deployment requirement is not configured: {key}.",
            })
        if DEPLOYMENT_ENV == "production" and not SECURE_COOKIES:
            issues.append({
                "key": "ANYAICAM_SECURE_COOKIES",
                "severity": "critical",
                "message": "Secure cookies must be enabled in production.",
            })
        if DEPLOYMENT_ENV == "production" and not FORCE_HTTPS:
            issues.append({
                "key": "ANYAICAM_FORCE_HTTPS",
                "severity": "critical",
                "message": "HTTPS enforcement must be enabled in production.",
            })


    return issues


def load_json_file(path: Path, default: dict) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError):
        pass
    return default


def save_json_file(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)

def load_cloud_upload_queue() -> list[dict]:
    queue = load_json_file(CLOUD_UPLOAD_QUEUE_FILE, [])
    return queue if isinstance(queue, list) else []


def save_cloud_upload_queue(queue: list[dict]) -> None:
    save_json_file(CLOUD_UPLOAD_QUEUE_FILE, queue[-10000:])


def load_cloud_recording_index() -> dict:
    index = load_json_file(CLOUD_RECORDING_INDEX_FILE, {})
    return index if isinstance(index, dict) else {}


def save_cloud_recording_index(index: dict) -> None:
    save_json_file(CLOUD_RECORDING_INDEX_FILE, index)


cloud_upload_queue: list[dict] = load_cloud_upload_queue()
cloud_upload_state = {
    "enabled": CLOUD_UPLOAD_ENABLED,
    "worker_status": "disabled" if not CLOUD_UPLOAD_ENABLED else "starting",
    "queued": 0, "uploading": 0, "uploaded": 0, "failed": 0,
    "last_upload_at": None, "last_scan_at": None,
    "last_error": None, "sdk_available": boto3 is not None,
}


def refresh_cloud_upload_state() -> None:
    for status in ("queued", "uploading", "uploaded", "failed"):
        cloud_upload_state[status] = sum(
            item.get("status") == status for item in cloud_upload_queue
        )


def cloud_configuration_snapshot() -> dict:
    checks = {
        "environment": DEPLOYMENT_ENV,
        "runtime_role": RUNTIME_ROLE,
        "aws_region_configured": bool(AWS_REGION),
        "database_configured": bool(DATABASE_URL),
        "s3_configured": bool(S3_BUCKET),
        "s3_sdk_available": boto3 is not None,
        "ses_configured": bool(SES_SENDER or (SMTP_HOST and SMTP_FROM)),
        "secrets_manager_configured": bool(SECRETS_MANAGER_SECRET_ID),
        "public_url_configured": bool(PUBLIC_BASE_URL),
        "cloudfront_configured": bool(CLOUDFRONT_URL),
        "force_https": FORCE_HTTPS,
        "secure_cookies": SECURE_COOKIES,
        "cloud_upload_enabled": CLOUD_UPLOAD_ENABLED,
        "cloud_delete_local": CLOUD_UPLOAD_DELETE_LOCAL,
        "cloud_storage_class": CLOUD_UPLOAD_STORAGE_CLASS,
        "cloud_server_side_encryption": CLOUD_UPLOAD_SSE or None,
    }
    required = {
        "aws_region_configured": checks["aws_region_configured"],
        "database_configured": checks["database_configured"],
        "s3_configured": checks["s3_configured"],
        "public_url_configured": checks["public_url_configured"],
        "secrets_manager_configured": checks["secrets_manager_configured"],
    }
    checks["cloud_foundation_ready"] = all(required.values())
    checks["cloud_recording_ready"] = bool(
        CLOUD_UPLOAD_ENABLED and S3_BUCKET and AWS_REGION and boto3 is not None
    )
    checks["missing_cloud_requirements"] = [
        key for key, configured in required.items() if not configured
    ]
    if CLOUD_UPLOAD_ENABLED and boto3 is None:
        checks["missing_cloud_requirements"].append("boto3_dependency")
    return checks


def cloud_recording_camera_number(path: Path) -> int | None:
    match = re.search(r"camera(\d+)", str(path), re.IGNORECASE)
    if not match:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= CAMERA_COUNT else None


def cloud_recording_s3_key(path: Path, camera_number: int | None) -> str:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        modified = datetime.now()
    prefix = f"{S3_PREFIX}/" if S3_PREFIX else ""
    camera = f"camera{camera_number}" if camera_number else path.parent.name
    return f"{prefix}recordings/{camera}/{modified:%Y/%m/%d}/{path.name}"


def enqueue_cloud_upload(path: Path, camera_number: int | None = None) -> dict:
    resolved = str(path.resolve())
    existing = next(
        (job for job in cloud_upload_queue
         if job.get("path") == resolved
         and job.get("status") in {"queued", "uploading", "uploaded"}),
        None,
    )
    if existing:
        return existing
    stat = path.stat()
    camera = camera_number or cloud_recording_camera_number(path)
    job = {
        "id": uuid.uuid4().hex,
        "path": resolved,
        "camera": camera,
        "s3_bucket": S3_BUCKET,
        "s3_key": cloud_recording_s3_key(path, camera),
        "status": "queued",
        "attempts": 0,
        "max_retries": CLOUD_UPLOAD_MAX_RETRIES,
        "size_bytes": stat.st_size,
        "created_at": datetime.now().isoformat(),
        "next_attempt_at": datetime.now().isoformat(),
        "last_error": "",
    }
    cloud_upload_queue.append(job)
    save_cloud_upload_queue(cloud_upload_queue)
    refresh_cloud_upload_state()
    structured_log("cloud_upload.queued", job_id=job["id"], path=resolved, s3_key=job["s3_key"])
    return job


def enqueue_cloud_upload_placeholder(path: Path, camera_number: int | None = None) -> dict:
    return enqueue_cloud_upload(path, camera_number)


def scan_recordings_for_cloud_upload() -> int:
    if not CLOUD_UPLOAD_ENABLED:
        return 0
    cutoff = time.time() - CLOUD_UPLOAD_MIN_FILE_AGE_SECONDS
    known = {
        str(job.get("path") or "") for job in cloud_upload_queue
        if job.get("status") in {"queued", "uploading", "uploaded"}
    }
    known.update(
        str(item.get("local_path") or "")
        for item in load_cloud_recording_index().values()
    )
    added = 0
    for suffix in ("*.mkv", "*.mp4"):
        for path in RECORDINGS_FOLDER.rglob(suffix):
            if BACKUP_FOLDER in path.parents:
                continue
            try:
                stat = path.stat()
                resolved = str(path.resolve())
            except OSError:
                continue
            if stat.st_size <= 0 or stat.st_mtime > cutoff or resolved in known:
                continue
            enqueue_cloud_upload(path, cloud_recording_camera_number(path))
            known.add(resolved)
            added += 1
    cloud_upload_state["last_scan_at"] = datetime.now().isoformat()
    return added


def upload_cloud_recording_job(job: dict) -> dict:
    if boto3 is None:
        raise RuntimeError("boto3 is not installed. Add boto3 to requirements.txt.")
    if not S3_BUCKET or not AWS_REGION:
        raise RuntimeError("S3 bucket and AWS region must be configured.")
    path = Path(str(job.get("path") or ""))
    if not path.exists():
        raise FileNotFoundError(f"Recording file is missing: {path}")
    digest, size = sha256_file(path)
    extra = {
        "ContentType": "video/x-matroska" if path.suffix.lower() == ".mkv" else "video/mp4",
        "StorageClass": CLOUD_UPLOAD_STORAGE_CLASS,
        "Metadata": {"anyaicam-job-id": str(job.get("id") or ""), "anyaicam-camera": str(job.get("camera") or "")},
    }
    if CLOUD_UPLOAD_KMS_KEY_ID:
        extra.update({"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": CLOUD_UPLOAD_KMS_KEY_ID})
    elif CLOUD_UPLOAD_SSE:
        extra["ServerSideEncryption"] = CLOUD_UPLOAD_SSE
    client = boto3.client("s3", region_name=AWS_REGION)
    kwargs = {"Filename": str(path), "Bucket": S3_BUCKET, "Key": job["s3_key"], "ExtraArgs": extra}
    if TransferConfig is not None:
        kwargs["Config"] = TransferConfig(
            multipart_threshold=CLOUD_UPLOAD_MULTIPART_THRESHOLD_MB * 1024 * 1024,
            multipart_chunksize=16 * 1024 * 1024,
            max_concurrency=2,
            use_threads=True,
        )
    client.upload_file(**kwargs)
    uploaded_at = datetime.now().isoformat()
    record = {
        "id": job["id"], "camera": job.get("camera"), "local_path": str(path.resolve()),
        "file_name": path.name, "size_bytes": size, "sha256": digest,
        "s3_bucket": S3_BUCKET, "s3_key": job["s3_key"],
        "s3_uri": f"s3://{S3_BUCKET}/{job['s3_key']}",
        "cloudfront_url": f"{CLOUDFRONT_URL}/{quote(job['s3_key'])}" if CLOUDFRONT_URL else "",
        "uploaded_at": uploaded_at, "storage_class": CLOUD_UPLOAD_STORAGE_CLASS,
        "local_deleted": False,
    }
    index = load_cloud_recording_index()
    index[record["id"]] = record
    save_cloud_recording_index(index)
    if CLOUD_UPLOAD_DELETE_LOCAL:
        path.unlink(missing_ok=True)
        record["local_deleted"] = True
        index[record["id"]] = record
        save_cloud_recording_index(index)
    return record


async def cloud_upload_worker() -> None:
    if not CLOUD_UPLOAD_ENABLED:
        cloud_upload_state["worker_status"] = "disabled"
        while True:
            await asyncio.sleep(3600)
    cloud_upload_state["worker_status"] = "running" if boto3 is not None else "dependency_missing"
    structured_log("cloud_upload.worker_started", status=cloud_upload_state["worker_status"], bucket=S3_BUCKET or None)
    while True:
        try:
            scan_recordings_for_cloud_upload()
            now = datetime.now()
            job = next(
                (item for item in cloud_upload_queue
                 if item.get("status") in {"queued", "failed"}
                 and int(item.get("attempts") or 0) < int(item.get("max_retries") or CLOUD_UPLOAD_MAX_RETRIES)
                 and (not item.get("next_attempt_at") or datetime.fromisoformat(str(item["next_attempt_at"])) <= now)),
                None,
            )
            if not job:
                refresh_cloud_upload_state()
                await asyncio.sleep(CLOUD_UPLOAD_SCAN_SECONDS)
                continue
            job["status"] = "uploading"
            job["attempts"] = int(job.get("attempts") or 0) + 1
            save_cloud_upload_queue(cloud_upload_queue)
            refresh_cloud_upload_state()
            try:
                record = await asyncio.to_thread(upload_cloud_recording_job, job)
                job.update({"status": "uploaded", "uploaded_at": record["uploaded_at"], "sha256": record["sha256"], "s3_uri": record["s3_uri"], "last_error": ""})
                cloud_upload_state["last_upload_at"] = record["uploaded_at"]
                cloud_upload_state["last_error"] = None
                structured_log("cloud_upload.completed", job_id=job["id"], s3_uri=record["s3_uri"], size_bytes=record["size_bytes"])
            except Exception as error:
                job["status"] = "failed"
                job["last_error"] = str(error)
                delay = CLOUD_UPLOAD_RETRY_SECONDS * (2 ** max(0, job["attempts"] - 1))
                job["next_attempt_at"] = (datetime.now() + timedelta(seconds=delay)).isoformat()
                cloud_upload_state["last_error"] = str(error)
                structured_log("cloud_upload.failed", level="error", job_id=job["id"], error=str(error))
            save_cloud_upload_queue(cloud_upload_queue)
            refresh_cloud_upload_state()
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            cloud_upload_state["last_error"] = str(error)
            await asyncio.sleep(CLOUD_UPLOAD_RETRY_SECONDS)


async def cloud_upload_worker_placeholder() -> None:
    await cloud_upload_worker()


refresh_cloud_upload_state()


def startup_self_test() -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, critical: bool = False) -> None:
        checks.append(
            {
                "name": name,
                "ok": ok,
                "detail": detail,
                "critical": critical,
            }
        )

    try:
        RECORDINGS_FOLDER.mkdir(parents=True, exist_ok=True)
        probe = RECORDINGS_FOLDER / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        add("recordings_writable", True, str(RECORDINGS_FOLDER), True)
    except OSError as error:
        add("recordings_writable", False, str(error), True)

    try:
        disk = shutil.disk_usage(RECORDINGS_FOLDER)
        add("storage_available", disk.free > 512 * 1024 * 1024, f"{disk.free / (1024**3):.1f} GB free", True)
    except OSError as error:
        add("storage_available", False, str(error), True)

    ffmpeg_path = shutil.which("ffmpeg")
    add("ffmpeg_available", bool(ffmpeg_path), ffmpeg_path or "ffmpeg not found", True)

    issues = configuration_issues()
    critical_config = [issue for issue in issues if issue["severity"] == "critical"]
    add(
        "configuration_valid",
        not critical_config,
        f"{len(issues)} issue(s), {len(critical_config)} critical",
        True,
    )

    return {
        "ok": all(item["ok"] or not item["critical"] for item in checks),
        "checks": checks,
        "configuration_issues": issues,
        "checked_at": datetime.now().isoformat(),
    }


def readiness_snapshot() -> dict:
    self_test = startup_self_test()
    statuses = camera_status().get("cameras", [])
    online = sum(1 for camera in statuses if camera.get("online"))
    recording = sum(1 for camera in statuses if camera.get("recording") == "running")
    cloud = cloud_configuration_snapshot()

    if RUNTIME_ROLE == "cloud":
        role_ready = cloud["cloud_foundation_ready"]
    elif RUNTIME_ROLE == "combined":
        role_ready = recording > 0 and cloud["cloud_foundation_ready"]
    else:
        role_ready = recording > 0

    ready = self_test["ok"] and role_ready
    return {
        "ready": ready,
        "environment": DEPLOYMENT_ENV,
        "runtime_role": RUNTIME_ROLE,
        "self_test": self_test,
        "cloud": cloud,
        "cloud_upload": dict(cloud_upload_state),
        "cameras_online": online,
        "cameras_total": CAMERA_COUNT,
        "recording_workers": recording,
        "checked_at": datetime.now().isoformat(),
    }


def backup_manifest() -> dict:
    return {
        "product": "AnyAiCam VMS",
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "created_at": datetime.now().isoformat(),
        "retention_days": RETENTION_DAYS,
        "camera_count": CAMERA_COUNT,
        "files": [path.name for path in CONFIG_BACKUP_FILES if path.exists()],
    }


def create_configuration_backup() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = BACKUPS_FOLDER / f"anyaicam_config_{timestamp}.zip"
    manifest = backup_manifest()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for source in CONFIG_BACKUP_FILES:
            if source.exists() and source.is_file():
                archive.write(source, arcname=f"config/{source.name}")
    return destination


def list_backups() -> list[dict]:
    backups = []
    for path in sorted(BACKUPS_FOLDER.glob("*.zip"), reverse=True):
        try:
            backups.append(
                {
                    "name": path.name,
                    "size_mb": round(path.stat().st_size / (1024**2), 2),
                    "created_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                    "url": f"/api/operations/backups/{quote(path.name)}",
                }
            )
        except OSError:
            continue
    return backups


def restore_configuration_backup(archive_path: Path) -> dict:
    restored: list[str] = []
    skipped: list[str] = []
    allowed = {path.name: path for path in CONFIG_BACKUP_FILES}

    with zipfile.ZipFile(archive_path, "r") as archive:
        for member in archive.infolist():
            if member.is_dir() or not member.filename.startswith("config/"):
                continue
            name = Path(member.filename).name
            target = allowed.get(name)
            if not target:
                skipped.append(name)
                continue
            payload = archive.read(member)
            temporary = target.with_suffix(target.suffix + ".restore")
            temporary.write_bytes(payload)
            temporary.replace(target)
            restored.append(name)

    return {"restored": restored, "skipped": skipped}


def diagnostics_snapshot() -> dict:
    metrics = system_metrics()
    readiness = readiness_snapshot()
    uptime_seconds = max(0, int((datetime.now() - STARTED_AT).total_seconds()))
    return {
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": uptime_seconds,
        "readiness": readiness,
        "metrics": metrics,
        "camera_status": camera_status(),
        "health_issues": list(health_issues.values()),
        "backups": list_backups(),
        "configuration_issues": configuration_issues(),
        "checked_at": datetime.now().isoformat(),
    }


def camera_url(camera_number: int) -> str:
    host = os.environ[f"CAMERA{camera_number}_HOST"]
    username = quote(os.environ[f"CAMERA{camera_number}_USERNAME"], safe="")
    password = quote(os.environ[f"CAMERA{camera_number}_PASSWORD"], safe="")
    path = os.environ.get(
        f"CAMERA{camera_number}_PATH", "/Streaming/Channels/101"
    )
    return f"rtsp://{username}:{password}@{host}:554{path}"


def start_live_stream(camera_number: int) -> subprocess.Popen:
    output_file = str(HLS_FOLDER / f"camera{camera_number}.m3u8")
    command = [
        "ffmpeg", "-rtsp_transport", "tcp", "-i", camera_url(camera_number),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "48000",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
        "-hls_flags", "delete_segments+append_list", output_file,
    ]
    return subprocess.Popen(command)


def start_recording(camera_number: int) -> subprocess.Popen:
    camera_folder = RECORDINGS_FOLDER / f"camera{camera_number}"
    camera_folder.mkdir(parents=True, exist_ok=True)
    output_pattern = str(
        camera_folder / f"camera{camera_number}_%Y-%m-%d_%H-%M-%S.mkv"
    )
    command = [
        "ffmpeg", "-rtsp_transport", "tcp", "-i", camera_url(camera_number),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
        "-f", "segment", "-segment_time", "300",
        "-reset_timestamps", "1", "-strftime", "1", output_pattern,
    ]
    return subprocess.Popen(command)


def delete_expired_recordings() -> None:
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    deleted_count = 0
    for recording_file in RECORDINGS_FOLDER.rglob("*.mkv"):
        try:
            if datetime.fromtimestamp(recording_file.stat().st_mtime) < cutoff:
                recording_file.unlink(missing_ok=True)
                deleted_count += 1
        except OSError as error:
            print(f"Could not inspect or delete {recording_file}: {error}")
    if deleted_count:
        print(f"Retention cleanup deleted {deleted_count} expired recording(s).")
    if MOTION_EVENTS_FILE.exists():
        retained_events = []
        for event in load_motion_events():
            try:
                event_time = event.get("start_time") or event.get("timestamp")
                if datetime.fromisoformat(event_time) >= cutoff:
                    retained_events.append(json.dumps(event, separators=(",", ":")))
            except (KeyError, TypeError, ValueError):
                continue
        try:
            MOTION_EVENTS_FILE.write_text(
                "\n".join(retained_events) + ("\n" if retained_events else ""),
                encoding="utf-8",
            )
        except OSError as error:
            print(f"Could not prune motion events: {error}")
    for thumbnail in MOTION_THUMBNAILS_FOLDER.rglob("*.jpg"):
        try:
            if datetime.fromtimestamp(thumbnail.stat().st_mtime) < cutoff:
                thumbnail.unlink(missing_ok=True)
        except OSError:
            continue


async def retention_worker() -> None:
    while True:
        delete_expired_recordings()
        await asyncio.sleep(3600)


async def process_supervisor(camera_number: int, mode: str) -> None:
    """Keep one camera worker alive and retry when a camera is unavailable."""
    starter = start_live_stream if mode == "live" else start_recording
    while True:
        camera_process_state[camera_number][mode] = "connecting"
        process = starter(camera_number)
        ffmpeg_processes.append(process)
        camera_process_state[camera_number][mode] = "running"
        return_code = await asyncio.to_thread(process.wait)
        camera_reconnect_counts[camera_number] += 1
        camera_process_state[camera_number][mode] = "retrying"
        print(
            f"Camera {camera_number} {mode} process exited with "
            f"code {return_code}; retrying in 10 seconds."
        )
        await asyncio.sleep(10)


def load_motion_events() -> list[dict]:
    if not MOTION_EVENTS_FILE.exists():
        return []
    events = []
    try:
        for line in MOTION_EVENTS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return events


LICENSE_PLAN_FEATURES = {
    "starter": [
        "live_view",
        "playback",
        "alerts",
    ],
    "professional": [
        "live_view",
        "playback",
        "alerts",
        "smart_search",
        "investigations",
        "incident_reports",
        "mobile_devices",
    ],
    "enterprise": [
        "live_view",
        "playback",
        "alerts",
        "smart_search",
        "investigations",
        "incident_reports",
        "mobile_devices",
        "enterprise_notifications",
        "evidence_integrity",
        "backup_restore",
        "aws_foundation",
    ],
}


def default_license_state() -> dict:
    now = datetime.now()
    trial_ends_at = (
        now + timedelta(days=DEFAULT_LICENSE_TRIAL_DAYS)
    ).isoformat() if DEFAULT_LICENSE_TRIAL_DAYS else ""
    plan = (
        DEFAULT_LICENSE_PLAN
        if DEFAULT_LICENSE_PLAN in LICENSE_PLAN_FEATURES
        else "professional"
    )
    return {
        "plan": plan,
        "status": "trial" if DEFAULT_LICENSE_TRIAL_DAYS else "active",
        "camera_limit": DEFAULT_LICENSE_CAMERA_LIMIT,
        "trial_ends_at": trial_ends_at,
        "subscription_ends_at": "",
        "features": list(LICENSE_PLAN_FEATURES[plan]),
        "customer_reference": "",
        "notes": "",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "updated_by": "system",
    }


def load_license_state() -> dict:
    state = load_json_file(LICENSE_STATE_FILE, {})
    if not isinstance(state, dict) or not state:
        state = default_license_state()
        save_json_file(LICENSE_STATE_FILE, state)
    return state


def save_license_state(state: dict) -> None:
    save_json_file(LICENSE_STATE_FILE, state)


def load_license_history() -> list[dict]:
    history = load_json_file(LICENSE_HISTORY_FILE, [])
    return history if isinstance(history, list) else []


def save_license_history(history: list[dict]) -> None:
    save_json_file(LICENSE_HISTORY_FILE, history)


ONBOARDING_STEPS = [
    "organization",
    "site",
    "deployment",
    "cameras",
    "cloud_recording",
    "team",
    "terms",
    "review",
]


def load_onboarding_profiles() -> dict:
    profiles = load_json_file(ONBOARDING_PROFILES_FILE, {})
    return profiles if isinstance(profiles, dict) else {}


def save_onboarding_profiles(profiles: dict) -> None:
    save_json_file(ONBOARDING_PROFILES_FILE, profiles)


def load_onboarding_activity() -> list[dict]:
    activity = load_json_file(ONBOARDING_ACTIVITY_FILE, [])
    return activity if isinstance(activity, list) else []


def save_onboarding_activity(activity: list[dict]) -> None:
    save_json_file(ONBOARDING_ACTIVITY_FILE, activity[-2000:])


def append_onboarding_activity(
    *,
    user: dict,
    profile_id: str,
    action: str,
    details: str = "",
) -> dict:
    entry = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now().isoformat(),
        "profile_id": profile_id,
        "action": action,
        "details": details,
        "user_id": user.get("id"),
        "user_name": user.get("display_name") or user.get("email") or "Unknown user",
    }
    activity = load_onboarding_activity()
    activity.append(entry)
    save_onboarding_activity(activity)
    return entry



def normalize_cloud_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]", "", str(value or "").strip()).upper()
    if normalized and not 4 <= len(normalized) <= 64:
        raise HTTPException(
            status_code=400,
            detail="Cloud ID must contain 4 to 64 letters, numbers, hyphens, or underscores.",
        )
    return normalized


def find_cloud_id_owner(cloud_id: str, exclude_user_id: str = "") -> dict | None:
    normalized = normalize_cloud_id(cloud_id)
    if not normalized:
        return None
    for profile in load_onboarding_profiles().values():
        if str(profile.get("user_id") or "") == str(exclude_user_id or ""):
            continue
        if str(profile.get("cloud_id") or "").strip().upper() == normalized:
            return profile
    return None


def onboarding_profile_for_user(user: dict) -> dict:
    profiles = load_onboarding_profiles()
    user_id = str(user.get("id") or "")
    if user_id in profiles:
        return profiles[user_id]

    now = datetime.now().isoformat()
    profile = {
        "id": user_id or uuid.uuid4().hex,
        "user_id": user_id,
        "user_email": user.get("email") or "",
        "organization_name": "",
        "contact_name": user.get("display_name") or "",
        "contact_email": user.get("email") or "",
        "contact_phone": "",
        "site_name": "",
        "site_address": "",
        "timezone_name": "",
        "deployment_type": "edge",
        "expected_camera_count": 1,
        "camera_manufacturer": "",
        "cloud_recording_requested": False,
        "retention_days_requested": 30,
        "edge_device_name": "",
        "edge_device_platform": "windows",
        "cloud_id": "",
        "cloud_id_device_type": "software_bridge",
        "cloud_id_status": "not_submitted",
        "cloud_id_submitted_at": "",
        "cloud_id_reviewed_at": "",
        "cloud_id_reviewed_by": "",
        "cloud_id_admin_note": "",
        "team_emails": [],
        "terms_accepted": False,
        "notes": "",
        "completed_steps": [],
        "status": "in_progress",
        "admin_note": "",
        "assigned_to": "",
        "created_at": now,
        "updated_at": now,
        "submitted_at": "",
    }
    profiles[user_id] = profile
    save_onboarding_profiles(profiles)
    return profile


def onboarding_progress(profile: dict) -> dict:
    completed = set(profile.get("completed_steps") or [])
    percent = round((len(completed) / len(ONBOARDING_STEPS)) * 100)
    missing = [step for step in ONBOARDING_STEPS if step not in completed]
    return {
        "completed_count": len(completed),
        "total_count": len(ONBOARDING_STEPS),
        "percent": percent,
        "missing_steps": missing,
        "ready_to_submit": (
            "organization" in completed
            and "site" in completed
            and "deployment" in completed
            and "cameras" in completed
            and "terms" in completed
        ),
    }


def onboarding_owner_matches(profile: dict, user: dict) -> bool:
    return (
        profile.get("user_id") == user.get("id")
        or has_permission(user, "manage_settings")
    )


def load_payment_sessions() -> dict:
    sessions = load_json_file(PAYMENT_SESSIONS_FILE, {})
    return sessions if isinstance(sessions, dict) else {}


def save_payment_sessions(sessions: dict) -> None:
    save_json_file(PAYMENT_SESSIONS_FILE, sessions)


def load_payment_webhook_events() -> dict:
    events = load_json_file(PAYMENT_WEBHOOK_EVENTS_FILE, {})
    return events if isinstance(events, dict) else {}


def save_payment_webhook_events(events: dict) -> None:
    save_json_file(PAYMENT_WEBHOOK_EVENTS_FILE, events)


def stripe_price_map() -> dict[str, str]:
    return {
        "starter": STRIPE_PRICE_STARTER,
        "professional": STRIPE_PRICE_PROFESSIONAL,
        "enterprise": STRIPE_PRICE_ENTERPRISE,
    }


def stripe_configuration_snapshot() -> dict:
    prices = stripe_price_map()
    return {
        "provider": "stripe",
        "secret_key_configured": bool(STRIPE_SECRET_KEY),
        "webhook_secret_configured": bool(STRIPE_WEBHOOK_SECRET),
        "public_base_url_configured": bool(PUBLIC_BASE_URL),
        "prices": {
            name: bool(value)
            for name, value in prices.items()
        },
        "checkout_ready": bool(
            STRIPE_SECRET_KEY
            and PUBLIC_BASE_URL
            and any(prices.values())
        ),
        "webhook_ready": bool(STRIPE_WEBHOOK_SECRET),
        "live_charging_enabled": bool(
            STRIPE_SECRET_KEY
            and STRIPE_WEBHOOK_SECRET
            and PUBLIC_BASE_URL
        ),
    }


def stripe_api_post(path: str, fields: list[tuple[str, str]]) -> dict:
    if not STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail="Stripe is not configured. Add ANYAICAM_STRIPE_SECRET_KEY.",
        )

    body = urlencode(fields).encode("utf-8")
    request = UrlRequest(
        f"{STRIPE_API_BASE}{path}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": f"AnyAiCam-VMS/{APP_VERSION}",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            error_data = json.loads(payload)
            message = (
                error_data.get("error", {}).get("message")
                or "Stripe request failed."
            )
        except json.JSONDecodeError:
            message = "Stripe request failed."
        structured_log(
            "stripe.api_error",
            level="error",
            status_code=exc.code,
            path=path,
            message=message,
        )
        raise HTTPException(status_code=502, detail=message) from exc
    except (URLError, TimeoutError, OSError) as exc:
        structured_log(
            "stripe.network_error",
            level="error",
            path=path,
            error=str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail="Could not connect to Stripe.",
        ) from exc


def parse_stripe_signature(header_value: str) -> tuple[int, list[str]]:
    timestamp = 0
    signatures = []
    for part in str(header_value or "").split(","):
        key, separator, value = part.strip().partition("=")
        if not separator:
            continue
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = 0
        elif key == "v1":
            signatures.append(value)
    return timestamp, signatures


def verify_stripe_webhook_signature(
    payload: bytes,
    signature_header: str,
) -> bool:
    if not STRIPE_WEBHOOK_SECRET:
        return False

    timestamp, signatures = parse_stripe_signature(signature_header)
    if not timestamp or not signatures:
        return False

    if abs(int(time.time()) - timestamp) > STRIPE_WEBHOOK_TOLERANCE_SECONDS:
        return False

    signed_payload = (
        str(timestamp).encode("utf-8")
        + b"."
        + payload
    )
    expected = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return any(
        hmac.compare_digest(expected, signature)
        for signature in signatures
    )


def stripe_customer_id_for_account(account: dict) -> str:
    return str(account.get("external_customer_id") or "").strip()


def sync_stripe_customer_to_account(
    account: dict,
    customer_id: str,
    user: dict | None = None,
) -> dict:
    account_id = str(account.get("id") or "primary")
    accounts = load_billing_accounts()
    stored = accounts.get(account_id, account)
    stored["external_customer_id"] = customer_id
    stored["updated_at"] = datetime.now().isoformat()
    if user:
        stored["updated_by"] = user.get("id")
    accounts[account_id] = stored
    save_billing_accounts(accounts)
    return stored


def update_license_from_stripe_subscription(
    subscription: dict,
    *,
    event_type: str,
) -> None:
    state = load_license_state()
    status_map = {
        "active": "active",
        "trialing": "trial",
        "past_due": "past_due",
        "unpaid": "past_due",
        "canceled": "inactive",
        "incomplete": "past_due",
        "incomplete_expired": "expired",
        "paused": "suspended",
    }
    stripe_status = str(subscription.get("status") or "")
    state["status"] = status_map.get(
        stripe_status,
        state.get("status", "active"),
    )

    current_period_end = subscription.get("current_period_end")
    if current_period_end:
        try:
            state["subscription_ends_at"] = datetime.fromtimestamp(
                int(current_period_end)
            ).isoformat()
        except (TypeError, ValueError, OSError):
            pass

    state["stripe_subscription_id"] = str(subscription.get("id") or "")
    state["stripe_customer_id"] = str(subscription.get("customer") or "")
    state["updated_at"] = datetime.now().isoformat()
    state["updated_by"] = "stripe_webhook"
    save_license_state(state)
    structured_log(
        "stripe.license_synced",
        event_type=event_type,
        stripe_status=stripe_status,
        license_status=state.get("status"),
    )


def record_stripe_webhook_event(event: dict) -> bool:
    event_id = str(event.get("id") or "")
    if not event_id:
        return False
    events = load_payment_webhook_events()
    if event_id in events:
        return False
    events[event_id] = {
        "id": event_id,
        "type": event.get("type"),
        "created": event.get("created"),
        "received_at": datetime.now().isoformat(),
        "livemode": bool(event.get("livemode")),
    }
    save_payment_webhook_events(events)
    return True


def process_stripe_webhook_event(event: dict) -> None:
    event_type = str(event.get("type") or "")
    data_object = event.get("data", {}).get("object", {})
    if not isinstance(data_object, dict):
        return

    if event_type == "checkout.session.completed":
        session_id = str(data_object.get("id") or "")
        sessions = load_payment_sessions()
        record = sessions.get(session_id, {})
        record.update({
            "status": "completed",
            "payment_status": data_object.get("payment_status"),
            "customer_id": data_object.get("customer"),
            "subscription_id": data_object.get("subscription"),
            "completed_at": datetime.now().isoformat(),
        })
        if session_id:
            sessions[session_id] = record
            save_payment_sessions(sessions)

        account_id = str(
            data_object.get("client_reference_id")
            or record.get("account_id")
            or ""
        )
        if account_id:
            accounts = load_billing_accounts()
            account = accounts.get(account_id)
            if account and data_object.get("customer"):
                sync_stripe_customer_to_account(
                    account,
                    str(data_object.get("customer")),
                )

    elif event_type in {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }:
        update_license_from_stripe_subscription(
            data_object,
            event_type=event_type,
        )

    elif event_type in {
        "invoice.paid",
        "invoice.payment_failed",
        "invoice.payment_action_required",
    }:
        external_id = str(data_object.get("id") or "")
        customer_id = str(data_object.get("customer") or "")
        accounts = load_billing_accounts()
        account_id = ""
        for candidate_id, account in accounts.items():
            if stripe_customer_id_for_account(account) == customer_id:
                account_id = candidate_id
                account["payment_status"] = (
                    "current"
                    if event_type == "invoice.paid"
                    else "past_due"
                )
                account["updated_at"] = datetime.now().isoformat()
                account["updated_by"] = "stripe_webhook"
                accounts[candidate_id] = account
                break
        save_billing_accounts(accounts)

        invoices = load_billing_invoices()
        local_invoice = next(
            (
                item
                for item in invoices.values()
                if item.get("external_invoice_id") == external_id
            ),
            None,
        )
        if not local_invoice:
            invoice_id = uuid.uuid4().hex
            local_invoice = {
                "id": invoice_id,
                "account_id": account_id,
                "description": "Stripe subscription invoice",
                "amount_cents": int(data_object.get("amount_due") or 0),
                "currency": str(
                    data_object.get("currency")
                    or BILLING_CURRENCY
                ).upper(),
                "due_date": "",
                "status": "paid" if event_type == "invoice.paid" else "past_due",
                "external_invoice_id": external_id,
                "paid_at": (
                    datetime.now().isoformat()
                    if event_type == "invoice.paid"
                    else ""
                ),
                "notes": "",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "created_by": "stripe_webhook",
            }
            invoices[invoice_id] = local_invoice
        else:
            local_invoice["status"] = (
                "paid" if event_type == "invoice.paid" else "past_due"
            )
            local_invoice["paid_at"] = (
                datetime.now().isoformat()
                if event_type == "invoice.paid"
                else local_invoice.get("paid_at", "")
            )
            local_invoice["updated_at"] = datetime.now().isoformat()
        save_billing_invoices(invoices)


def load_subscription_requests() -> dict:
    requests = load_json_file(SUBSCRIPTION_REQUESTS_FILE, {})
    return requests if isinstance(requests, dict) else {}


def save_subscription_requests(requests: dict) -> None:
    save_json_file(SUBSCRIPTION_REQUESTS_FILE, requests)


def load_billing_support_tickets() -> dict:
    tickets = load_json_file(BILLING_SUPPORT_TICKETS_FILE, {})
    return tickets if isinstance(tickets, dict) else {}


def save_billing_support_tickets(tickets: dict) -> None:
    save_json_file(BILLING_SUPPORT_TICKETS_FILE, tickets)


def billing_account_for_user(user: dict) -> dict:
    accounts = load_billing_accounts()
    user_email = str(user.get("email") or "").strip().lower()

    for account in accounts.values():
        if str(account.get("billing_email") or "").strip().lower() == user_email:
            return account

    if "primary" in accounts:
        return accounts["primary"]

    return {
        "id": "unassigned",
        "customer_name": user.get("display_name") or user.get("email") or "Customer",
        "billing_email": user.get("email") or "",
        "company_name": "",
        "external_customer_id": "",
        "payment_status": "not_configured",
        "billing_cycle": "manual",
        "next_billing_date": "",
        "notes": "",
    }


def invoices_for_billing_account(account_id: str) -> list[dict]:
    invoices = [
        invoice
        for invoice in load_billing_invoices().values()
        if invoice.get("account_id") == account_id
    ]
    invoices.sort(
        key=lambda item: item.get("updated_at", item.get("created_at", "")),
        reverse=True,
    )
    return invoices


def subscription_request_owner_matches(record: dict, user: dict) -> bool:
    return (
        record.get("user_id") == user.get("id")
        or has_permission(user, "manage_settings")
    )


def billing_ticket_owner_matches(record: dict, user: dict) -> bool:
    return (
        record.get("user_id") == user.get("id")
        or has_permission(user, "manage_settings")
    )


def customer_cloud_usage_snapshot(user: dict) -> dict:
    recordings = [
        path for path in RECORDINGS_FOLDER.rglob("*")
        if path.is_file()
        and path.parent != BACKUP_FOLDER
        and path.suffix.lower() in {".mp4", ".mkv", ".jpg", ".jpeg", ".png"}
    ]
    local_bytes = sum(path.stat().st_size for path in recordings)
    edge_status = "online" if RUNTIME_ROLE == "edge" else "cloud"
    return {
        "local_storage_bytes": local_bytes,
        "recording_file_count": len(recordings),
        "s3_configured": bool(S3_BUCKET),
        "s3_bucket": S3_BUCKET,
        "s3_prefix": S3_PREFIX,
        "cloudfront_configured": bool(CLOUDFRONT_URL),
        "cloudfront_url": CLOUDFRONT_URL,
        "edge_status": edge_status,
        "edge_runtime_role": RUNTIME_ROLE,
        "deployment_environment": DEPLOYMENT_ENV,
        "registered_mobile_devices": len(
            [
                device
                for device in load_mobile_devices().values()
                if device.get("user_id") == user.get("id")
                and not device.get("revoked")
            ]
        ),
        "configured_cameras": CAMERA_COUNT,
        "ai_processing_status": "configured" if AI_ENABLED else "disabled",
        "cloud_upload_status": cloud_upload_state.get("worker_status", "disabled"),
    }


def load_billing_accounts() -> dict:
    accounts = load_json_file(BILLING_ACCOUNTS_FILE, {})
    return accounts if isinstance(accounts, dict) else {}


def save_billing_accounts(accounts: dict) -> None:
    save_json_file(BILLING_ACCOUNTS_FILE, accounts)


def load_billing_invoices() -> dict:
    invoices = load_json_file(BILLING_INVOICES_FILE, {})
    return invoices if isinstance(invoices, dict) else {}


def save_billing_invoices(invoices: dict) -> None:
    save_json_file(BILLING_INVOICES_FILE, invoices)


def load_billing_events() -> list[dict]:
    events = load_json_file(BILLING_EVENTS_FILE, [])
    return events if isinstance(events, list) else []


def save_billing_events(events: list[dict]) -> None:
    save_json_file(BILLING_EVENTS_FILE, events[-2000:])


def append_billing_event(
    *,
    action: str,
    user: dict,
    account_id: str = "",
    invoice_id: str = "",
    details: str = "",
) -> dict:
    event = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "account_id": account_id,
        "invoice_id": invoice_id,
        "details": details,
        "user_id": user.get("id"),
        "user_name": user.get("display_name") or user.get("email") or "Unknown user",
    }
    events = load_billing_events()
    events.append(event)
    save_billing_events(events)
    return event


def billing_summary() -> dict:
    accounts = load_billing_accounts()
    invoices = load_billing_invoices()
    open_invoices = [
        item for item in invoices.values()
        if item.get("status") in {"draft", "open", "past_due"}
    ]
    outstanding_cents = sum(
        int(item.get("amount_cents") or 0)
        for item in open_invoices
        if item.get("status") in {"open", "past_due"}
    )
    return {
        "provider": BILLING_PROVIDER,
        "currency": BILLING_CURRENCY,
        "account_count": len(accounts),
        "invoice_count": len(invoices),
        "open_invoice_count": len(open_invoices),
        "outstanding_cents": outstanding_cents,
        "webhook_secret_configured": bool(BILLING_WEBHOOK_SECRET_NAME),
        "live_charging_enabled": False,
    }


def load_license_acknowledgements() -> list[dict]:
    acknowledgements = load_json_file(LICENSE_ACKNOWLEDGEMENTS_FILE, [])
    return acknowledgements if isinstance(acknowledgements, list) else []


def save_license_acknowledgements(acknowledgements: list[dict]) -> None:
    save_json_file(LICENSE_ACKNOWLEDGEMENTS_FILE, acknowledgements[-500:])


def parse_license_datetime(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def feature_entitlement(feature: str, snapshot: dict | None = None) -> dict:
    license_data = snapshot or license_snapshot()
    normalized = feature.strip().lower()
    entitled = normalized in set(license_data.get("available_features") or [])
    return {
        "feature": normalized,
        "entitled": entitled,
        "enforcement_mode": LICENSE_ENFORCEMENT_MODE,
        "blocked": False,
        "message": (
            "Feature is licensed."
            if entitled
            else "Feature is not included in the current plan. Warning-only enforcement is active."
        ),
    }


def license_enforcement_snapshot(camera_count: int | None = None) -> dict:
    state = load_license_state()
    current_camera_count = CAMERA_COUNT if camera_count is None else max(0, int(camera_count))
    now = datetime.now()

    trial_end = parse_license_datetime(state.get("trial_ends_at", ""))
    subscription_end = parse_license_datetime(state.get("subscription_ends_at", ""))
    applicable_end = subscription_end or trial_end
    days_past_due = 0
    days_remaining = None
    in_grace_period = False

    if applicable_end:
        delta = applicable_end - now
        days_remaining = max(0, delta.days)
        if delta.total_seconds() < 0:
            days_past_due = max(1, abs(delta.days))
            in_grace_period = days_past_due <= LICENSE_GRACE_DAYS

    camera_limit = max(1, int(state.get("camera_limit") or 1))
    camera_overage = max(0, current_camera_count - camera_limit)
    status = str(state.get("status") or "inactive")
    expired = bool(applicable_end and applicable_end <= now)
    payment_attention = status in {"past_due", "suspended", "expired", "inactive"}

    warnings = []
    if camera_overage:
        warnings.append({
            "code": "camera_limit_exceeded",
            "severity": "high",
            "message": (
                f"{current_camera_count} cameras are configured, but the license permits "
                f"{camera_limit}. No cameras are disabled in warning mode."
            ),
        })
    if expired and in_grace_period:
        warnings.append({
            "code": "license_grace_period",
            "severity": "high",
            "message": (
                f"The license has expired and is within the {LICENSE_GRACE_DAYS}-day grace period. "
                f"{max(0, LICENSE_GRACE_DAYS - days_past_due)} grace days remain."
            ),
        })
    elif expired:
        warnings.append({
            "code": "license_expired",
            "severity": "critical",
            "message": "The license has expired. Existing VMS functionality remains available in warning mode.",
        })
    elif payment_attention:
        warnings.append({
            "code": f"license_{status}",
            "severity": "high",
            "message": f"License status is {status.replace('_', ' ')}. Existing functionality remains available.",
        })

    entitled_features = set(
        state.get("features")
        or LICENSE_PLAN_FEATURES.get(state.get("plan"), [])
    )
    known_features = sorted(set(LICENSE_PLAN_FEATURES["enterprise"]))
    missing_features = [
        feature for feature in known_features
        if feature not in entitled_features
    ]

    valid = not camera_overage and not expired and status in {"active", "trial"}
    grace_valid = in_grace_period and not camera_overage

    return {
        **state,
        "current_camera_count": current_camera_count,
        "camera_limit": camera_limit,
        "camera_overage": camera_overage,
        "expired": expired,
        "in_grace_period": in_grace_period,
        "grace_days": LICENSE_GRACE_DAYS,
        "days_past_due": days_past_due,
        "days_remaining": days_remaining,
        "valid": valid,
        "grace_valid": grace_valid,
        "operational": True,
        "blocked": False,
        "enforcement_mode": LICENSE_ENFORCEMENT_MODE,
        "warnings": warnings,
        "missing_features": missing_features,
        "available_features": sorted(entitled_features),
        "checked_at": now.isoformat(),
    }


def license_warning_banner() -> str:
    if LICENSE_ENFORCEMENT_MODE == "off":
        return ""
    snapshot = license_enforcement_snapshot()
    warnings = snapshot.get("warnings") or []
    if not warnings:
        return ""
    message = " ".join(item.get("message", "") for item in warnings[:2])
    return (
        '<div class="license-warning-banner" role="status">'
        '<strong>License attention:</strong> '
        + escape(message)
        + ' <a href="/license-management">Review licensing</a></div>'
    )


def license_snapshot(camera_count: int | None = None) -> dict:
    snapshot = license_enforcement_snapshot(camera_count)
    return {
        **snapshot,
        "over_camera_limit": snapshot.get("camera_overage", 0) > 0,
    }


def record_license_history(
    *,
    user: dict,
    action: str,
    previous: dict,
    current: dict,
    details: str = "",
) -> None:
    history = load_license_history()
    history.append({
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "user_id": user.get("id"),
        "user_name": user.get("display_name") or user.get("email"),
        "previous": previous,
        "current": current,
        "details": details,
    })
    save_license_history(history[-500:])


def load_backup_jobs() -> dict:
    jobs = load_json_file(BACKUP_JOBS_FILE, {})
    return jobs if isinstance(jobs, dict) else {}


def save_backup_jobs(jobs: dict) -> None:
    save_json_file(BACKUP_JOBS_FILE, jobs)


def load_backup_restore_history() -> list[dict]:
    history = load_json_file(BACKUP_RESTORE_FILE, [])
    return history if isinstance(history, list) else []


def save_backup_restore_history(history: list[dict]) -> None:
    save_json_file(BACKUP_RESTORE_FILE, history)


def backup_source_files(
    include_recordings: bool = False,
    include_hls: bool = False,
) -> list[Path]:
    files = []
    protected_files = [
        USERS_FILE,
        SESSIONS_FILE,
        AUDIT_LOG_FILE,
        INVITATIONS_FILE,
        EVENT_REVIEWS_FILE,
        INVESTIGATION_CASES_FILE,
        EVIDENCE_LEDGER_FILE,
        EVIDENCE_HASHES_FILE,
        INCIDENT_REPORTS_FILE,
        NOTIFICATION_RULES_FILE,
        NOTIFICATION_DELIVERIES_FILE,
        MOBILE_DEVICES_FILE,
        MOBILE_PAIRING_CODES_FILE,
        RELEASE_CHECKS_FILE,
        MAINTENANCE_STATE_FILE,
    ]
    for path in protected_files:
        if path.exists() and path.is_file():
            files.append(path)

    if include_recordings and RECORDINGS_FOLDER.exists():
        files.extend(
            path for path in RECORDINGS_FOLDER.rglob("*")
            if path.is_file()
            and path.parent != BACKUP_FOLDER
            and path.suffix.lower() in {".mp4", ".mkv", ".jpg", ".jpeg", ".png", ".json"}
        )

    if include_hls and HLS_FOLDER.exists():
        files.extend(
            path for path in HLS_FOLDER.rglob("*")
            if path.is_file() and path.suffix.lower() in {".m3u8", ".ts"}
        )

    unique = []
    seen = set()
    for path in files:
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def backup_arcname(path: Path) -> str:
    try:
        return str(path.relative_to(Path("/app")))
    except ValueError:
        return f"external/{path.name}"


def prune_backup_files(jobs: dict) -> None:
    completed = sorted(
        (
            job for job in jobs.values()
            if job.get("status") == "complete"
            and job.get("file_path")
        ),
        key=lambda item: item.get("created_at", ""),
        reverse=True,
    )
    for old_job in completed[BACKUP_RETENTION_COUNT:]:
        path = Path(str(old_job.get("file_path") or ""))
        if path.exists() and path.is_file():
            path.unlink(missing_ok=True)
        old_job["status"] = "pruned"
        old_job["pruned_at"] = datetime.now().isoformat()


def create_backup_archive(
    *,
    label: str,
    include_recordings: bool,
    include_hls: bool,
    user: dict,
) -> dict:
    BACKUP_FOLDER.mkdir(parents=True, exist_ok=True)
    jobs = load_backup_jobs()
    backup_id = uuid.uuid4().hex
    now = datetime.now()
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label.strip())[:40].strip("_")
    suffix = f"_{safe_label}" if safe_label else ""
    file_name = f"anyaicam_backup_{now.strftime('%Y%m%d_%H%M%S')}{suffix}.zip"
    file_path = BACKUP_FOLDER / file_name

    source_files = backup_source_files(
        include_recordings=include_recordings,
        include_hls=include_hls,
    )

    manifest = {
        "product": "AnyAiCam VMS",
        "backup_id": backup_id,
        "created_at": now.isoformat(),
        "created_by": user.get("id"),
        "created_by_name": user.get("display_name") or user.get("email"),
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "environment": DEPLOYMENT_ENV,
        "runtime_role": RUNTIME_ROLE,
        "include_recordings": include_recordings,
        "include_hls": include_hls,
        "files": [],
    }

    with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source_files:
            arcname = backup_arcname(path)
            archive.write(path, arcname=arcname)
            digest, size = sha256_file(path)
            manifest["files"].append(
                {
                    "archive_name": arcname,
                    "source_path": str(path),
                    "size_bytes": size,
                    "sha256": digest,
                }
            )
        archive.writestr(
            "backup_manifest.json",
            json.dumps(manifest, indent=2, default=str),
        )

    archive_hash, archive_size = sha256_file(file_path)
    job = {
        "id": backup_id,
        "label": label.strip(),
        "status": "complete",
        "file_name": file_name,
        "file_path": str(file_path),
        "sha256": archive_hash,
        "size_bytes": archive_size,
        "file_count": len(manifest["files"]),
        "include_recordings": include_recordings,
        "include_hls": include_hls,
        "created_at": now.isoformat(),
        "created_by": user.get("id"),
        "created_by_name": user.get("display_name") or user.get("email"),
    }
    jobs[backup_id] = job
    prune_backup_files(jobs)
    save_backup_jobs(jobs)
    return job


def verify_backup_job(job: dict) -> dict:
    path = Path(str(job.get("file_path") or ""))
    if not path.exists() or not path.is_file():
        return {"status": "missing", "message": "Backup archive is missing."}
    current_hash, current_size = sha256_file(path)
    verified = (
        hmac.compare_digest(current_hash, str(job.get("sha256") or ""))
        and current_size == int(job.get("size_bytes") or 0)
    )
    return {
        "status": "verified" if verified else "modified",
        "current_sha256": current_hash,
        "current_size_bytes": current_size,
        "message": "Backup verified." if verified else "Backup archive was modified.",
    }


def validate_backup_archive(path: Path) -> dict:
    if not path.exists() or not path.is_file():
        return {"valid": False, "message": "Backup archive was not found."}
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = set(archive.namelist())
            if "backup_manifest.json" not in names:
                return {"valid": False, "message": "Backup manifest is missing."}
            manifest = json.loads(archive.read("backup_manifest.json"))
            bad_member = archive.testzip()
            if bad_member:
                return {
                    "valid": False,
                    "message": f"Archive member failed integrity check: {bad_member}",
                }
            return {
                "valid": True,
                "manifest": manifest,
                "message": "Backup archive is valid.",
            }
    except (zipfile.BadZipFile, json.JSONDecodeError, OSError) as exc:
        return {"valid": False, "message": f"Backup validation failed: {exc}"}


def load_maintenance_state() -> dict:
    state = load_json_file(
        MAINTENANCE_STATE_FILE,
        {
            "enabled": False,
            "message": "Scheduled maintenance is in progress.",
            "updated_at": None,
            "updated_by": None,
        },
    )
    return state if isinstance(state, dict) else {"enabled": False}


def save_maintenance_state(state: dict) -> None:
    save_json_file(MAINTENANCE_STATE_FILE, state)


def release_readiness_snapshot() -> dict:
    issues = configuration_issues()
    critical = [item for item in issues if item.get("severity") == "critical"]
    warnings = [item for item in issues if item.get("severity") == "warning"]

    required_paths = {
        "recordings_folder": RECORDINGS_FOLDER,
        "static_folder": STATIC_FOLDER,
        "hls_folder": HLS_FOLDER,
    }
    path_checks = {
        name: {
            "exists": path.exists(),
            "writable": os.access(path, os.W_OK) if path.exists() else False,
            "path": str(path),
        }
        for name, path in required_paths.items()
    }

    route_checks = {
        "health": True,
        "ready": True,
        "version": True,
        "login": True,
        "playback": True,
        "investigate": True,
        "cases": True,
        "incident_reports": True,
        "notification_rules": True,
        "mobile_devices": True,
    }

    checks = {
        "release_candidate": RELEASE_CANDIDATE,
        "environment": DEPLOYMENT_ENV,
        "runtime_role": RUNTIME_ROLE,
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "critical_issues": critical,
        "warnings": warnings,
        "paths": path_checks,
        "routes": route_checks,
        "maintenance": load_maintenance_state(),
        "cloud": cloud_configuration_snapshot(),
        "ready": not critical and all(
            item.get("exists") and item.get("writable")
            for item in path_checks.values()
        ),
        "checked_at": datetime.now().isoformat(),
    }
    save_json_file(RELEASE_CHECKS_FILE, checks)
    return checks


def safe_tail(path: Path, max_bytes: int) -> str:
    if not path.exists() or not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def build_support_bundle(user: dict) -> bytes:
    readiness = release_readiness_snapshot()
    maintenance = load_maintenance_state()
    bundle = io.BytesIO()

    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "release_readiness.json",
            json.dumps(readiness, indent=2, default=str),
        )
        archive.writestr(
            "maintenance_state.json",
            json.dumps(maintenance, indent=2, default=str),
        )
        archive.writestr(
            "version.json",
            json.dumps(
                {
                    "product": "AnyAiCam VMS",
                    "version": APP_VERSION,
                    "build_id": BUILD_ID,
                    "environment": DEPLOYMENT_ENV,
                    "runtime_role": RUNTIME_ROLE,
                    "generated_at": datetime.now().isoformat(),
                    "generated_by": user.get("email") or user.get("id"),
                },
                indent=2,
            ),
        )
        archive.writestr(
            "configuration_issues.json",
            json.dumps(configuration_issues(), indent=2, default=str),
        )
        archive.writestr(
            "cloud_configuration.json",
            json.dumps(cloud_configuration_snapshot(), indent=2, default=str),
        )
        archive.writestr(
            "camera_status.json",
            json.dumps(camera_status(), indent=2, default=str),
        )
        archive.writestr(
            "notification_channel_status.json",
            json.dumps(notification_channel_status(), indent=2, default=str),
        )
        archive.writestr(
            "mobile_device_count.json",
            json.dumps({"count": len(load_mobile_devices())}, indent=2),
        )

    return bundle.getvalue()


def load_mobile_devices() -> dict:
    devices = load_json_file(MOBILE_DEVICES_FILE, {})
    return devices if isinstance(devices, dict) else {}


def save_mobile_devices(devices: dict) -> None:
    save_json_file(MOBILE_DEVICES_FILE, devices)


def load_mobile_pairing_codes() -> dict:
    codes = load_json_file(MOBILE_PAIRING_CODES_FILE, {})
    return codes if isinstance(codes, dict) else {}


def save_mobile_pairing_codes(codes: dict) -> None:
    save_json_file(MOBILE_PAIRING_CODES_FILE, codes)


def cleanup_mobile_pairing_codes(codes: dict) -> dict:
    now = datetime.now()
    cleaned = {}
    for code, record in codes.items():
        try:
            expires_at = datetime.fromisoformat(str(record.get("expires_at")))
        except (TypeError, ValueError):
            continue
        if expires_at > now and not record.get("claimed"):
            cleaned[code] = record
    return cleaned


def create_mobile_pairing_code(user: dict, device_name: str) -> dict:
    codes = cleanup_mobile_pairing_codes(load_mobile_pairing_codes())
    code = f"{secrets.randbelow(1000000):06d}"
    while code in codes:
        code = f"{secrets.randbelow(1000000):06d}"
    now = datetime.now()
    record = {
        "code": code,
        "user_id": user.get("id"),
        "user_email": user.get("email"),
        "device_name": device_name.strip() or "Mobile device",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "claimed": False,
    }
    codes[code] = record
    save_mobile_pairing_codes(codes)
    return record


def mobile_device_owner_matches(device: dict, user: dict) -> bool:
    return (
        device.get("user_id") == user.get("id")
        or user.get("role") == "admin"
        or user.get("super_admin")
    )


def load_notification_rules() -> dict:
    rules = load_json_file(NOTIFICATION_RULES_FILE, {})
    return rules if isinstance(rules, dict) else {}


def save_notification_rules(rules: dict) -> None:
    save_json_file(NOTIFICATION_RULES_FILE, rules)


def load_notification_deliveries() -> list[dict]:
    deliveries = load_json_file(NOTIFICATION_DELIVERIES_FILE, [])
    return deliveries if isinstance(deliveries, list) else []


def save_notification_deliveries(deliveries: list[dict]) -> None:
    save_json_file(NOTIFICATION_DELIVERIES_FILE, deliveries)


def notification_channel_status() -> dict:
    return {
        "email": bool(SMTP_HOST and SMTP_FROM),
        "push": bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY),
        "sms": False,
        "webhook": False,
        "in_app": True,
    }


def append_notification_delivery(
    *,
    rule: dict,
    channel: str,
    recipient: str,
    status: str,
    message: str,
    event_id: str = "",
) -> dict:
    delivery = {
        "id": uuid.uuid4().hex,
        "rule_id": rule.get("id"),
        "rule_name": rule.get("name"),
        "channel": channel,
        "recipient": recipient,
        "status": status,
        "message": message,
        "event_id": event_id,
        "created_at": datetime.now().isoformat(),
    }
    deliveries = load_notification_deliveries()
    deliveries.append(delivery)
    save_notification_deliveries(deliveries[-2000:])
    return delivery


def simulate_notification_delivery(rule: dict, user: dict) -> list[dict]:
    deliveries = []
    available = notification_channel_status()
    recipients = rule.get("recipients") or [user.get("email") or "current-user"]
    for channel in rule.get("channels", []):
        ready = available.get(channel, False)
        for recipient in recipients:
            status = "delivered" if channel == "in_app" else ("configured" if ready else "not_configured")
            deliveries.append(
                append_notification_delivery(
                    rule=rule,
                    channel=channel,
                    recipient=recipient,
                    status=status,
                    message="Test notification recorded." if status != "not_configured" else f"{channel} delivery is not configured.",
                )
            )
    return deliveries


def load_incident_reports() -> dict:
    reports = load_json_file(INCIDENT_REPORTS_FILE, {})
    return reports if isinstance(reports, dict) else {}


def save_incident_reports(reports: dict) -> None:
    save_json_file(INCIDENT_REPORTS_FILE, reports)


def build_incident_report_draft(case: dict, events: list[dict], user: dict) -> dict:
    ordered_events = sorted(
        events,
        key=lambda event: str(
            event.get("timestamp")
            or event.get("start_time")
            or event.get("event_timestamp")
            or ""
        ),
    )

    timeline = []
    people = 0
    vehicles = 0
    for event in ordered_events:
        event_type = str(event.get("event_type") or "event").lower()
        if event_type == "person":
            people += 1
        if event_type in {"vehicle", "car", "truck", "bus", "motorcycle", "bicycle"}:
            vehicles += 1
        timeline.append({
            "event_id": str(event.get("id") or ""),
            "camera": event.get("camera") or event.get("camera_id"),
            "timestamp": str(
                event.get("timestamp")
                or event.get("start_time")
                or event.get("event_timestamp")
                or ""
            ),
            "event_type": event_type,
            "confidence": event.get("confidence", event.get("score")),
            "notes": "",
        })

    summary_parts = [
        f"Case: {case.get('title', 'Untitled case')}.",
        f"Status: {case.get('status', 'open')}.",
        f"Priority: {case.get('priority', 'normal')}.",
        f"Evidence events reviewed: {len(timeline)}.",
    ]
    if people:
        summary_parts.append(f"Person detections: {people}.")
    if vehicles:
        summary_parts.append(f"Vehicle detections: {vehicles}.")

    return {
        "title": f"Incident Report — {case.get('title', 'Untitled Case')}",
        "incident_summary": " ".join(summary_parts),
        "timeline": timeline,
        "people_count": people,
        "vehicle_count": vehicles,
        "evidence_reviewed": list(case.get("event_ids", [])),
        "investigator_observations": "",
        "recommended_follow_up": "",
        "ai_generated": True,
        "approved": False,
        "status": "draft",
        "author_id": user.get("id"),
        "author_name": user.get("display_name") or user.get("email") or "Unknown user",
    }


def load_evidence_ledger() -> list[dict]:
    ledger = load_json_file(EVIDENCE_LEDGER_FILE, [])
    return ledger if isinstance(ledger, list) else []


def save_evidence_ledger(entries: list[dict]) -> None:
    save_json_file(EVIDENCE_LEDGER_FILE, entries)


def load_evidence_hashes() -> dict:
    hashes = load_json_file(EVIDENCE_HASHES_FILE, {})
    return hashes if isinstance(hashes, dict) else {}


def save_evidence_hashes(records: dict) -> None:
    save_json_file(EVIDENCE_HASHES_FILE, records)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def request_device_context(request: Request) -> dict:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded_for.split(",")[0].strip() if forwarded_for else ""
    if not client_ip and request.client:
        client_ip = request.client.host
    return {
        "ip_address": client_ip,
        "user_agent": request.headers.get("user-agent", ""),
        "host": request.headers.get("host", ""),
    }


def append_evidence_ledger(
    *,
    request: Request,
    user: dict,
    action: str,
    evidence_id: str,
    case_id: str = "",
    event_id: str = "",
    file_path: str = "",
    hash_value: str = "",
    details: str = "",
) -> dict:
    entry = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "evidence_id": evidence_id,
        "case_id": case_id,
        "event_id": event_id,
        "file_path": file_path,
        "sha256": hash_value,
        "details": details,
        "user_id": user.get("id"),
        "user_name": user.get("display_name") or user.get("email") or "Unknown user",
        **request_device_context(request),
    }
    ledger = load_evidence_ledger()
    ledger.append(entry)
    save_evidence_ledger(ledger)
    return entry


def register_evidence_file(
    *,
    request: Request,
    user: dict,
    path: Path,
    evidence_id: str,
    case_id: str = "",
    event_id: str = "",
    action: str = "exported",
) -> dict:
    sha256_value, size = sha256_file(path)
    now = datetime.now().isoformat()
    records = load_evidence_hashes()
    record = {
        "evidence_id": evidence_id,
        "case_id": case_id,
        "event_id": event_id,
        "file_path": str(path),
        "file_name": path.name,
        "sha256": sha256_value,
        "size_bytes": size,
        "created_at": now,
        "created_by": user.get("id"),
        "created_by_name": user.get("display_name") or user.get("email") or "Unknown user",
    }
    records[evidence_id] = record
    save_evidence_hashes(records)
    append_evidence_ledger(
        request=request,
        user=user,
        action=action,
        evidence_id=evidence_id,
        case_id=case_id,
        event_id=event_id,
        file_path=str(path),
        hash_value=sha256_value,
        details=f"Registered {path.name} ({size} bytes).",
    )
    return record


def resolve_evidence_path(record: dict) -> Path:
    return Path(str(record.get("file_path") or ""))


def verify_evidence_record(
    *,
    request: Request,
    user: dict,
    evidence_id: str,
) -> dict:
    records = load_evidence_hashes()
    record = records.get(evidence_id)
    if not record:
        return {"status": "missing", "message": "Evidence record was not found."}

    path = resolve_evidence_path(record)
    if not path.exists() or not path.is_file():
        append_evidence_ledger(
            request=request,
            user=user,
            action="verified_missing",
            evidence_id=evidence_id,
            case_id=str(record.get("case_id") or ""),
            event_id=str(record.get("event_id") or ""),
            file_path=str(path),
            hash_value=str(record.get("sha256") or ""),
            details="Evidence file is missing.",
        )
        return {"status": "missing", "record": record, "message": "Evidence file is missing."}

    current_hash, current_size = sha256_file(path)
    verified = (
        hmac.compare_digest(current_hash, str(record.get("sha256") or ""))
        and current_size == int(record.get("size_bytes") or 0)
    )
    result_status = "verified" if verified else "modified"
    append_evidence_ledger(
        request=request,
        user=user,
        action=f"verified_{result_status}",
        evidence_id=evidence_id,
        case_id=str(record.get("case_id") or ""),
        event_id=str(record.get("event_id") or ""),
        file_path=str(path),
        hash_value=current_hash,
        details=f"Expected {record.get('sha256')}; current {current_hash}.",
    )
    return {
        "status": result_status,
        "record": record,
        "current_sha256": current_hash,
        "current_size_bytes": current_size,
        "message": "Evidence verified." if verified else "Evidence has been modified.",
    }


def load_investigation_cases() -> dict:
    return load_json_file(INVESTIGATION_CASES_FILE, {})


def save_investigation_cases(cases: dict) -> None:
    save_json_file(INVESTIGATION_CASES_FILE, cases)


def case_history_entry(action: str, user: dict, details: str = "") -> dict:
    return {
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "user_id": user.get("id"),
        "user_name": user.get("display_name") or user.get("email") or "Unknown user",
        "details": details,
    }


def default_event_settings(camera_number: int) -> dict:
    return EventSettingsModel(camera=camera_number).model_dump()


def get_event_settings(camera_number: int) -> EventSettingsModel:
    all_settings = load_json_file(EVENT_SETTINGS_FILE, {})
    return EventSettingsModel.model_validate(
        all_settings.get(str(camera_number), default_event_settings(camera_number))
    )


def get_alert_rule(camera_number: int) -> AlertRuleModel:
    rules = load_json_file(ALERT_RULES_FILE, {})
    return AlertRuleModel.model_validate(
        rules.get(str(camera_number), AlertRuleModel(camera=camera_number).model_dump())
    )


def linked_recording_for(camera_number: int, event_time: datetime) -> str | None:
    camera_folder = RECORDINGS_FOLDER / f"camera{camera_number}"
    for source in sorted(camera_folder.glob("*.mkv"), reverse=True):
        source_start = recording_start(source, camera_number)
        if source_start and source_start <= event_time < source_start + timedelta(minutes=5):
            offset = max(0, int((event_time - source_start).total_seconds()))
            return f"/recordings/camera{camera_number}/{quote(source.name)}#t={offset}"
    return None


def alert_rule_is_active(rule: AlertRuleModel, occurred_at: datetime) -> bool:
    current = occurred_at.strftime("%H:%M")
    if rule.schedule_start <= rule.schedule_end:
        return rule.schedule_start <= current <= rule.schedule_end
    return current >= rule.schedule_start or current <= rule.schedule_end



def default_notification_settings() -> dict:
    return NotificationSettingsModel().model_dump()


def load_notification_settings() -> dict:
    try:
        if NOTIFICATION_SETTINGS_FILE.exists():
            data = json.loads(NOTIFICATION_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return NotificationSettingsModel.model_validate(data).model_dump()
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return default_notification_settings()


def save_notification_settings(settings: dict) -> None:
    temporary = NOTIFICATION_SETTINGS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temporary.replace(NOTIFICATION_SETTINGS_FILE)


def load_push_subscriptions() -> list[dict]:
    try:
        if PUSH_SUBSCRIPTIONS_FILE.exists():
            data = json.loads(PUSH_SUBSCRIPTIONS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_push_subscriptions(subscriptions: list[dict]) -> None:
    temporary = PUSH_SUBSCRIPTIONS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(subscriptions, indent=2), encoding="utf-8")
    temporary.replace(PUSH_SUBSCRIPTIONS_FILE)


def quiet_hours_active(settings: dict, now: datetime | None = None) -> bool:
    if not settings.get("quiet_hours_enabled"):
        return False
    now = now or datetime.now()
    current = now.strftime("%H:%M")
    start = settings.get("quiet_start", "22:00")
    end = settings.get("quiet_end", "07:00")
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def notification_event_allowed(alert: dict, settings: dict) -> bool:
    event_type = str(alert.get("event_type", "")).lower()
    allowed = {str(item).lower() for item in settings.get("event_types", [])}
    return event_type in allowed and not quiet_hours_active(settings)


def alert_link(alert: dict) -> str:
    camera = alert.get("camera")
    event_id = alert.get("event_id")
    if event_id:
        path = f"/events?event_id={quote(str(event_id))}"
    elif camera:
        path = f"/camera/{int(camera)}"
    else:
        path = "/dashboard"
    return f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else path


def send_email_alert(alert: dict, settings: dict) -> tuple[bool, str]:
    recipient = settings.get("recipient_email", "").strip()
    if not recipient:
        return False, "Recipient email is not configured."
    if not SMTP_HOST or not SMTP_FROM:
        return False, "SMTP is not configured."

    message = EmailMessage()
    event_type = str(alert.get("event_type", "alert")).replace("_", " ").title()
    message["Subject"] = f"AnyAiCam: {event_type}"
    message["From"] = SMTP_FROM
    message["To"] = recipient
    message.set_content(
        "\n".join(
            [
                alert.get("message", "AnyAiCam alert"),
                "",
                f"Event type: {alert.get('event_type', 'unknown')}",
                f"Camera: {alert.get('camera') or 'System'}",
                f"Time: {alert.get('timestamp', datetime.now().isoformat())}",
                f"Open: {alert_link(alert)}",
            ]
        )
    )

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls(context=context)
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
        return True, "Email sent."
    except (OSError, smtplib.SMTPException) as error:
        return False, str(error)


def send_push_alert(alert: dict) -> tuple[int, list[str]]:
    if webpush is None:
        return 0, ["pywebpush is not installed."]
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return 0, ["VAPID keys are not configured."]

    subscriptions = load_push_subscriptions()
    delivered = 0
    errors: list[str] = []
    retained: list[dict] = []
    payload = json.dumps(
        {
            "title": "AnyAiCam Alert",
            "body": alert.get("message", "New AnyAiCam event"),
            "url": alert_link(alert),
            "event_type": alert.get("event_type"),
            "camera": alert.get("camera"),
        }
    )

    for subscription in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription["endpoint"],
                    "keys": subscription["keys"],
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
            delivered += 1
            retained.append(subscription)
        except WebPushException as error:
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            if status_code not in {404, 410}:
                retained.append(subscription)
                errors.append(str(error)[:200])
        except Exception as error:
            retained.append(subscription)
            errors.append(str(error)[:200])

    if retained != subscriptions:
        save_push_subscriptions(retained)
    return delivered, errors


def dispatch_external_notifications(alert: dict) -> None:
    settings = load_notification_settings()
    if not notification_event_allowed(alert, settings):
        return
    if settings.get("email_enabled"):
        ok, detail = send_email_alert(alert, settings)
        print(f"Email alert {'sent' if ok else 'failed'}: {detail}")
    if settings.get("push_enabled"):
        delivered, errors = send_push_alert(alert)
        print(f"Push alerts delivered: {delivered}")
        for error in errors[:3]:
            print(f"Push alert error: {error}")


def append_in_app_alert(alert: dict) -> None:
    with IN_APP_ALERTS_FILE.open("a", encoding="utf-8") as alert_file:
        alert_file.write(json.dumps(alert, separators=(",", ":")) + "\n")
    dispatch_external_notifications(alert)


def append_motion_event(line: str) -> None:
    with MOTION_EVENTS_FILE.open("a", encoding="utf-8") as event_file:
        event_file.write(line)


async def create_motion_thumbnail(
    camera_number: int, event_id: str, frame: bytes, occurred_at: datetime
) -> str | None:
    day_folder = MOTION_THUMBNAILS_FOLDER / occurred_at.strftime("%Y-%m-%d")
    day_folder.mkdir(parents=True, exist_ok=True)
    filename = f"camera{camera_number}_{occurred_at.strftime('%H-%M-%S')}_{event_id[:8]}.jpg"
    output_path = day_folder / filename
    manifest = HLS_FOLDER / f"camera{camera_number}.m3u8"

    # Motion analysis stays grayscale for low CPU use, but the saved event
    # thumbnail is captured from the live HLS stream so it keeps full color.
    if manifest.exists():
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-loglevel", "error", "-y",
                "-i", str(manifest),
                "-frames:v", "1", "-vf", "scale=640:-2",
                "-q:v", "3", str(output_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
            if process.returncode == 0 and output_path.exists():
                return f"/recordings/media/motion/{occurred_at.strftime('%Y-%m-%d')}/{quote(filename)}"
        except OSError as error:
            print(f"Could not create color motion thumbnail: {error}")

    # Fallback: preserve event creation even if the live color stream is
    # temporarily unavailable. This fallback image is grayscale.
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo",
            "-pixel_format", "gray", "-video_size", "160x90", "-i", "pipe:0",
            "-frames:v", "1", "-q:v", "4", str(output_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.communicate(frame)
        if process.returncode == 0 and output_path.exists():
            return f"/recordings/media/motion/{occurred_at.strftime('%Y-%m-%d')}/{quote(filename)}"
    except OSError as error:
        print(f"Could not create fallback motion thumbnail: {error}")
    return None


async def store_motion_event(
    camera_number: int,
    start_time: datetime,
    end_time: datetime,
    score: float,
    frame: bytes,
) -> None:
    event_id = uuid.uuid4().hex
    thumbnail = await create_motion_thumbnail(camera_number, event_id, frame, start_time)
    event = MotionEventModel(
        id=event_id,
        camera=camera_number,
        start_time=start_time,
        end_time=end_time,
        confidence=min(99, max(1, round((score / max(MOTION_THRESHOLD, 1)) * 50, 1))),
        thumbnail=thumbnail,
        linked_recording=linked_recording_for(camera_number, start_time),
    )
    line = event.model_dump_json() + "\n"
    async with motion_event_lock:
        await asyncio.to_thread(append_motion_event, line)
        rule = get_alert_rule(camera_number)
        if (
            rule.enabled
            and "motion" in rule.event_types
            and "in_app" in rule.delivery_methods
            and alert_rule_is_active(rule, start_time)
        ):
            await asyncio.to_thread(
                append_in_app_alert,
                {
                    "id": uuid.uuid4().hex,
                    "event_id": event.id,
                    "camera": camera_number,
                    "site": "home",
                    "event_type": "motion",
                    "timestamp": start_time.isoformat(),
                    "message": f"Motion detected on Camera {camera_number}",
                    "read": False,
                },
            )
    print(f"Motion detected on Camera {camera_number} (confidence {event.confidence:.1f}%).")


async def motion_detector(camera_number: int) -> None:
    frame_size = 160 * 90
    last_event_time = 0.0
    previous_frame: bytes | None = None
    active_start: datetime | None = None
    active_score = 0.0
    active_frame: bytes | None = None
    still_frames = 0
    motion_frames = 0
    manifest = HLS_FOLDER / f"camera{camera_number}.m3u8"
    while True:
        if not manifest.exists():
            await asyncio.sleep(5)
            continue
        process = None
        try:
            command = [
                "ffmpeg", "-loglevel", "error", "-fflags", "nobuffer",
                "-i", str(manifest), "-an", "-vf", "fps=1,scale=160:90,format=gray",
                "-f", "rawvideo", "pipe:1",
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if process.stdout is None:
                raise RuntimeError("Motion detector could not read video frames.")
            while True:
                frame = await process.stdout.readexactly(frame_size)
                if previous_frame is not None:
                    settings = get_event_settings(camera_number)
                    if not settings.enabled:
                        previous_frame = frame
                        await asyncio.sleep(1)
                        continue
                    total_difference = 0
                    changed_pixels = 0
                    compared_pixels = 0
                    for index, (current, previous) in enumerate(zip(frame, previous_frame)):
                        pixel_x = (index % 160) / 160
                        pixel_y = (index // 160) / 90
                        in_zone = any(
                            zone.x <= pixel_x <= zone.x + zone.width
                            and zone.y <= pixel_y <= zone.y + zone.height
                            for zone in settings.zones
                        )
                        if not in_zone:
                            continue
                        difference = abs(current - previous)
                        total_difference += difference
                        compared_pixels += 1
                        if difference >= 20:
                            changed_pixels += 1
                    motion_score = total_difference / max(compared_pixels, 1)
                    changed_ratio = changed_pixels / max(compared_pixels, 1)
                    now = time.monotonic()
                    effective_threshold = MOTION_THRESHOLD * (1.5 - settings.sensitivity / 100)
                    motion_detected = (
                        motion_score >= effective_threshold and changed_ratio >= 0.08
                    )
                    if motion_detected:
                        if active_start is None and now - last_event_time >= settings.cooldown_seconds:
                            active_start = datetime.now()
                            active_score = motion_score
                            active_frame = frame
                            motion_frames = 1
                        elif active_start is not None and motion_score > active_score:
                            active_score = motion_score
                            active_frame = frame
                            motion_frames += 1
                        elif active_start is not None:
                            motion_frames += 1
                        still_frames = 0
                    elif active_start is not None:
                        still_frames += 1
                        if still_frames >= 3:
                            if motion_frames >= settings.minimum_duration_seconds:
                                await store_motion_event(
                                    camera_number,
                                    active_start,
                                    datetime.now(),
                                    active_score,
                                    active_frame or frame,
                                )
                            active_start = None
                            active_score = 0.0
                            active_frame = None
                            still_frames = 0
                            motion_frames = 0
                            last_event_time = now
                previous_frame = frame
        except asyncio.IncompleteReadError:
            previous_frame = None
        except (OSError, RuntimeError) as error:
            print(f"Camera {camera_number} motion detector retrying: {error}")
        finally:
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
        await asyncio.sleep(5)



def append_analytics_event(event: dict) -> None:
    events = load_json_list(ANALYTICS_EVENTS_FILE)
    events.append(event)
    events.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    save_json_list(ANALYTICS_EVENTS_FILE, events[:5000])


def get_yolo_model():
    global yolo_model
    if YOLO is None:
        raise RuntimeError("Ultralytics YOLO is not installed.")
    if yolo_model is None:
        print(f"Loading YOLO model: {YOLO_MODEL_NAME} on {YOLO_DEVICE}")
        yolo_model = YOLO(YOLO_MODEL_NAME)
    return yolo_model


def detect_objects_frame(camera_number: int) -> dict:
    if cv2 is None or YOLO is None:
        return {
            "ok": False,
            "error": "Ultralytics YOLO or OpenCV is not installed.",
            "detections": [],
        }

    manifest = HLS_FOLDER / f"camera{camera_number}.m3u8"
    if not manifest.exists():
        return {
            "ok": False,
            "error": "Live HLS stream is not ready.",
            "detections": [],
        }

    capture = cv2.VideoCapture(str(manifest))
    try:
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, frame = capture.read()
    finally:
        capture.release()

    if not ok or frame is None:
        return {
            "ok": False,
            "error": "Could not read a live frame.",
            "detections": [],
        }

    model = get_yolo_model()
    results = model.predict(
        source=frame,
        conf=YOLO_CONFIDENCE,
        imgsz=YOLO_IMAGE_SIZE,
        device=YOLO_DEVICE,
        verbose=False,
    )

    detections = []
    for result in results:
        names = result.names
        boxes = result.boxes
        if boxes is None:
            continue
        for box in boxes:
            class_id = int(box.cls[0].item())
            class_name = str(names.get(class_id, class_id)).lower()
            if class_name not in YOLO_ALLOWED_CLASSES:
                continue
            confidence = float(box.conf[0].item())
            x1, y1, x2, y2 = [
                int(value) for value in box.xyxy[0].tolist()
            ]
            detections.append(
                {
                    "class_id": class_id,
                    "class_name": class_name,
                    "confidence": round(confidence, 4),
                    "x": x1,
                    "y": y1,
                    "width": max(1, x2 - x1),
                    "height": max(1, y2 - y1),
                }
            )

    return {
        "ok": True,
        "frame": frame,
        "detections": detections,
        "error": None,
    }


def save_yolo_events(camera_number: int, result: dict) -> list[dict]:
    detections = result.get("detections", [])
    frame = result.get("frame")
    if not detections or frame is None or cv2 is None:
        return []

    now = datetime.now()
    day_folder = AI_THUMBNAILS_FOLDER / now.strftime("%Y-%m-%d")
    day_folder.mkdir(parents=True, exist_ok=True)
    event_group_id = uuid.uuid4().hex[:12]
    filename = (
        f"camera{camera_number}_{now.strftime('%H-%M-%S')}_"
        f"{event_group_id}.jpg"
    )
    output_path = day_folder / filename

    annotated = frame.copy()
    class_colors = {
        "person": (67, 209, 204),
        "car": (255, 177, 74),
        "truck": (255, 122, 89),
        "bus": (207, 140, 255),
        "motorcycle": (85, 170, 255),
        "bicycle": (95, 221, 126),
        "dog": (181, 139, 99),
        "cat": (214, 165, 255),
        "bird": (255, 215, 92),
        "backpack": (147, 168, 255),
        "suitcase": (211, 151, 96),
    }

    for detection in detections:
        x = detection["x"]
        y = detection["y"]
        width = detection["width"]
        height = detection["height"]
        class_name = detection["class_name"]
        confidence = float(detection["confidence"])
        color = class_colors.get(class_name, (67, 209, 204))
        cv2.rectangle(
            annotated,
            (x, y),
            (x + width, y + height),
            color,
            2,
        )
        cv2.putText(
            annotated,
            f"{class_name.title()} {confidence * 100:.0f}%",
            (x, max(20, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )

    if not cv2.imwrite(str(output_path), annotated):
        return []

    thumbnail_url = (
        f"/recordings/media/ai/{now.strftime('%Y-%m-%d')}/"
        f"{quote(filename)}"
    )
    linked_recording = linked_recording_for(camera_number, now)
    saved_events = []

    grouped: dict[str, list[dict]] = {}
    for detection in detections:
        grouped.setdefault(detection["class_name"], []).append(detection)

    for class_name, class_detections in grouped.items():
        best_confidence = max(
            float(item["confidence"]) for item in class_detections
        )
        event = AnalyticsEventModel(
            id=uuid.uuid4().hex[:12],
            camera=camera_number,
            site="home",
            rule_name=f"Local YOLO {class_name} detection",
            event_type=class_name,
            timestamp=now,
            confidence=round(best_confidence, 4),
            thumbnail=thumbnail_url,
            linked_recording=linked_recording,
            mock=False,
        ).model_dump(mode="json")
        event["object_count"] = len(class_detections)
        event["detections"] = class_detections
        append_analytics_event(event)
        saved_events.append(event)

    return saved_events


async def ai_person_detector(camera_number: int) -> None:
    state = ai_detection_state[camera_number]
    if cv2 is None or YOLO is None:
        state.update(
            status="unavailable",
            error="Ultralytics YOLO is not installed.",
            last_checked=datetime.now().isoformat(),
        )
        return

    state["status"] = "loading"
    try:
        await asyncio.to_thread(get_yolo_model)
    except Exception as error:
        state.update(
            status="error",
            error=f"Could not load {YOLO_MODEL_NAME}: {error}"[:240],
            last_checked=datetime.now().isoformat(),
        )
        return

    state["status"] = "running"
    while True:
        try:
            result = await asyncio.to_thread(
                detect_objects_frame,
                camera_number,
            )
            state["last_checked"] = datetime.now().isoformat()
            state["error"] = result.get("error")
            state["status"] = "running" if result.get("ok") else "waiting"
            detections = result.get("detections", [])
            now_monotonic = time.monotonic()
            if (
                detections
                and now_monotonic - ai_person_last_event[camera_number]
                >= AI_PERSON_COOLDOWN_SECONDS
            ):
                events = await asyncio.to_thread(
                    save_yolo_events,
                    camera_number,
                    result,
                )
                if events:
                    ai_person_last_event[camera_number] = now_monotonic
                    state["last_detection"] = events[0]["timestamp"]
                    state["detections"] += sum(
                        int(event.get("object_count", 1))
                        for event in events
                    )
                    for event in events:
                        event_type = event["event_type"]
                        object_count = int(event.get("object_count", 1))
                        await asyncio.to_thread(
                            append_in_app_alert,
                            {
                                "id": uuid.uuid4().hex,
                                "event_id": event["id"],
                                "camera": camera_number,
                                "site": "home",
                                "event_type": event_type,
                                "timestamp": event["timestamp"],
                                "message": (
                                    f"{object_count} {event_type}"
                                    f"{'s' if object_count != 1 else ''} "
                                    f"detected on Camera {camera_number}"
                                ),
                                "read": False,
                            },
                        )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.update(
                status="error",
                error=str(error)[:240],
                last_checked=datetime.now().isoformat(),
            )
            print(
                f"Camera {camera_number} YOLO detector retrying: {error}"
            )

        await asyncio.sleep(AI_DETECTION_INTERVAL_SECONDS)


async def health_monitor() -> None:
    while True:
        now = time.time()
        detected: dict[str, dict] = {}
        statuses = camera_status().get("cameras", [])
        for status in statuses:
            camera_number = status["camera"]
            if not status["online"]:
                detected[f"camera-{camera_number}-offline"] = {
                    "type": "stream_offline", "camera": camera_number,
                    "message": f"Camera {camera_number} stream is offline.", "severity": "warning",
                }
            if camera_process_state[camera_number]["recording"] != "running":
                detected[f"camera-{camera_number}-recording"] = {
                    "type": "recording_stopped", "camera": camera_number,
                    "message": f"Camera {camera_number} recording worker stopped.", "severity": "critical",
                }
            if camera_reconnect_counts[camera_number] >= 3:
                detected[f"camera-{camera_number}-reconnects"] = {
                    "type": "reconnect_failures", "camera": camera_number,
                    "message": f"Camera {camera_number} has repeatedly failed to reconnect.", "severity": "warning",
                }
        metrics = system_metrics()
        if metrics["cpu_percent"] >= 85:
            detected["system-high-cpu"] = {
                "type": "high_cpu", "camera": None,
                "message": f"System CPU usage is high ({metrics['cpu_percent']}%).", "severity": "warning",
            }
        if metrics["storage_percent"] >= 90 or metrics["storage_free_gb"] < 10:
            detected["system-low-storage"] = {
                "type": "low_disk_space", "camera": None,
                "message": f"Storage is low ({metrics['storage_free_gb']} GB free).", "severity": "critical",
            }
        health_issues.clear()
        for issue_id, issue in detected.items():
            issue.update(id=issue_id, timestamp=datetime.now().isoformat())
            health_issues[issue_id] = issue
            if now - health_alert_times.get(issue_id, 0) >= 300:
                await asyncio.to_thread(
                    append_in_app_alert,
                    {
                        "id": uuid.uuid4().hex, "event_id": None,
                        "camera": issue["camera"], "site": "home",
                        "event_type": issue["type"], "timestamp": issue["timestamp"],
                        "message": issue["message"], "read": False,
                    },
                )
                health_alert_times[issue_id] = now
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ffmpeg_processes
    structured_log("startup.begin", web_concurrency=WEB_CONCURRENCY)
    ffmpeg_processes = []
    supervisor_tasks = []
    motion_tasks = []
    ai_tasks = []

    if RUNTIME_ROLE in {"edge", "combined"}:
        for camera_number in range(1, CAMERA_COUNT + 1):
            supervisor_tasks.append(
                asyncio.create_task(process_supervisor(camera_number, "live"))
            )
            supervisor_tasks.append(
                asyncio.create_task(process_supervisor(camera_number, "recording"))
            )
        if MOTION_DETECTION_ENABLED:
            motion_tasks = [
                asyncio.create_task(motion_detector(camera_number))
                for camera_number in range(1, CAMERA_COUNT + 1)
            ]
        if AI_PERSON_DETECTION_ENABLED:
            ai_tasks = [
                asyncio.create_task(ai_person_detector(camera_number))
                for camera_number in range(1, CAMERA_COUNT + 1)
            ]

    health_task = (
        asyncio.create_task(health_monitor())
        if RUNTIME_ROLE in {"edge", "combined"} else None
    )
    retention_task = (
        asyncio.create_task(retention_worker())
        if RUNTIME_ROLE in {"edge", "combined"} else None
    )
    cloud_upload_task = asyncio.create_task(cloud_upload_worker_placeholder())

    structured_log(
        "startup.complete",
        cloud=cloud_configuration_snapshot(),
        upload_worker=cloud_upload_state["worker_status"],
    )
    try:
        yield
    finally:
        structured_log("shutdown.begin")
        if retention_task:
            retention_task.cancel()
        for task in supervisor_tasks + motion_tasks + ai_tasks:
            task.cancel()
        if health_task:
            health_task.cancel()
        cloud_upload_task.cancel()

        pending = []
        if retention_task:
            pending.append(retention_task)
        pending.extend(supervisor_tasks)
        pending.extend(motion_tasks)
        pending.extend(ai_tasks)
        if health_task:
            pending.append(health_task)
        pending.append(cloud_upload_task)
        await asyncio.gather(*pending, return_exceptions=True)

        for process in ffmpeg_processes:
            if process.poll() is None:
                process.terminate()
        for process in ffmpeg_processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        structured_log("shutdown.complete")


from cloud_config import configure_logging, settings as cloud_settings
from cloud_security import ProductionSecurityMiddleware

configure_logging()
cloud_settings.validate()
REQUEST_CONTEXT: ContextVar[Request | None] = ContextVar(
    "anyaicam_request_context",
    default=None,
)

app = FastAPI(title="AnyAiCam VMS", lifespan=lifespan)

@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    token = REQUEST_CONTEXT.set(request)
    try:
        return await call_next(request)
    finally:
        REQUEST_CONTEXT.reset(token)


@app.middleware("http")
async def maintenance_mode_middleware(request: Request, call_next):
    state = load_maintenance_state()
    exempt_paths = {
        "/health",
        "/ready",
        "/version",
        "/login",
        "/logout",
        "/release-readiness",
        "/api/release-readiness",
        "/api/maintenance",
        "/api/support-bundle",
    }
    if (
        state.get("enabled")
        and request.url.path not in exempt_paths
        and not request.url.path.startswith("/static/")
    ):
        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset="utf-8"><title>Maintenance</title></head>
            <body style="font-family:Arial;background:#0d1522;color:white;display:grid;place-items:center;min-height:100vh">
            <main style="max-width:560px;padding:28px;border:1px solid #2a3b55;border-radius:14px;background:#151f30">
            <h1>AnyAiCam maintenance</h1><p>{escape(str(state.get("message") or "Scheduled maintenance is in progress."))}</p>
            </main></body></html>""",
            status_code=503,
            headers={"Retry-After": "300"},
        )
    return await call_next(request)


@app.middleware("http")
async def forwarded_https_middleware(request: Request, call_next):
    forwarded_proto = (
        request.headers.get("x-forwarded-proto", request.url.scheme)
        if TRUST_PROXY_HEADERS else request.url.scheme
    )
    if (
        FORCE_HTTPS
        and forwarded_proto != "https"
        and request.url.path not in {"/health", "/ready", "/version"}
    ):
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if host:
            secure_url = f"https://{host}{request.url.path}"
            if request.url.query:
                secure_url += f"?{request.url.query}"
            return RedirectResponse(secure_url, status_code=307)

    response = await call_next(request)
    if forwarded_proto == "https":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    return response
app.add_middleware(ProductionSecurityMiddleware)
if cloud_settings.deployed:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=cloud_settings.trusted_hosts)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")
app.mount("/recordings", StaticFiles(directory="/app/recordings"), name="recordings")


PUBLIC_PATH_PREFIXES = (
    "/login",
    "/accept-invite",
    "/health",
    "/ready",
    "/version",
    "/static/",
    # Stripe must reach this route without an AnyAiCam browser session.
    # Signature verification inside the route remains mandatory.
    "/api/payments/stripe/webhook",
)


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    path = request.url.path
    if path in {"/favicon.ico"} or any(path == prefix or path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES):
        return await call_next(request)

    user = authenticated_user(request)
    if user:
        request.state.user = user
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse(
            {"status": "error", "message": "Authentication required."},
            status_code=401,
        )
    next_url = quote(path + (f"?{request.url.query}" if request.url.query else ""), safe="/?=&")
    return RedirectResponse(f"/login?next={next_url}", status_code=303)



PARTNER_PORTAL_ROLES = {"partner_admin", "partner_sales", "installer"}
CUSTOMER_PORTAL_ROLES = {"customer_owner", "customer_viewer"}
ADMIN_PORTAL_ROLES = {"administrator", "support_admin", "admin"}


PUBLIC_BUSINESS_REGISTRATION_ROLES = {
    "customer_owner",
    "partner_sales",
    "installer",
    "administrator",
}


def is_master_admin(user: dict | None) -> bool:
    if not user:
        return False
    return bool(
        user.get("id") == "local-admin"
        or user.get("super_admin") is True
    )


def portal_destination_for_user(user: dict | None) -> str:
    """Return the existing portal destination for the authenticated role."""
    role = str((user or {}).get("role") or "").strip().lower()
    if role == "installer":
        return "/partner-installations"
    if role in PARTNER_PORTAL_ROLES:
        return "/partner-sales"
    if role in CUSTOMER_PORTAL_ROLES:
        return "/customer-portal"
    if role in ADMIN_PORTAL_ROLES:
        return "/admin-portal"
    return "/"


def safe_login_destination(user: dict, requested_path: str) -> str:
    """Honor a safe next path unless it points into a portal the role cannot use."""
    destination = (
        requested_path
        if requested_path.startswith("/") and not requested_path.startswith("//")
        else "/"
    )
    role = str(user.get("role") or "").strip().lower()

    if destination in {"", "/"}:
        return portal_destination_for_user(user)

    if destination.startswith((
        "/partner-sales", "/partner-quotes", "/partner/quotes/",
        "/partner-installations", "/partner-performance", "/partner/",
    )):
        return destination if role in PARTNER_PORTAL_ROLES else portal_destination_for_user(user)

    if destination.startswith("/admin-"):
        return destination if role in ADMIN_PORTAL_ROLES else portal_destination_for_user(user)

    if destination.startswith("/customer-portal"):
        return destination if role in CUSTOMER_PORTAL_ROLES else portal_destination_for_user(user)

    return destination


def partner_page_authorization_response(request: Request) -> Response | None:
    """
    Preserve require_partner_access for partner authorization, but convert its
    browser-facing HTTPException into a role-appropriate portal redirect or the
    normal styled access-denied page.
    """
    try:
        require_partner_access(request)
        return None
    except HTTPException:
        user = current_user(request)
        destination = portal_destination_for_user(user)
        role = str(user.get("role") or "").strip().lower()

        if destination not in {"/", "/partner-sales"}:
            return RedirectResponse(destination, status_code=303)

        return HTMLResponse(
            permission_denied_page(
                "Partner portal",
                "partner-sales",
                "partner authorization",
            ),
            status_code=403,
        )


def auth_form_script() -> str:
    return """<script>
const nativeFetch=window.fetch.bind(window);
window.fetch=(input,options={})=>{const method=(options.method||'GET').toUpperCase(),sameOrigin=typeof input==='string'?(!input.startsWith('http://')&&!input.startsWith('https://')):input.url.startsWith(location.origin);if(sameOrigin&&['POST','PUT','PATCH','DELETE'].includes(method)){const csrf=document.cookie.split('; ').find(item=>item.startsWith('anyaicam_csrf='));if(csrf){let token=decodeURIComponent(csrf.split('=').slice(1).join('='));token=token.replace(/^"(.*)"$/,'$1');options.headers={...(options.headers||{}),'X-CSRF-Token':token}}}return nativeFetch(input,options)};
document.addEventListener('submit',async event=>{const form=event.target;if(!(form instanceof HTMLFormElement)||!form.matches('.auth-form'))return;event.preventDefault();const submitter=event.submitter;const data=new FormData(form);if(submitter&&submitter.name)data.append(submitter.name,submitter.value);const body=new URLSearchParams();for(const [name,value] of data){if(typeof value==='string')body.append(name,value)}if(submitter)submitter.disabled=true;try{const response=await window.fetch(form.action,{method:(form.method||'POST').toUpperCase(),body});if(response.redirected){location.assign(response.url);return}const html=await response.text();document.open();document.write(html);document.close()}catch(error){if(submitter)submitter.disabled=false;throw error}});
</script>"""


def login_page_html(error: str = "", next_url: str = "/", message: str = "") -> str:
    safe_error = f'<div class="auth-error">{escape(error)}</div>' if error else ""
    safe_message = f'<div class="auth-success">{escape(message)}</div>' if message else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · AnyAiCam</title><style>{STYLES}</style></head><body><main class="auth-page"><section class="auth-card"><img class="auth-logo" src="/static/brand-icon.png" alt="AnyAiCam"><h1>Sign in</h1><p class="auth-subtitle">Secure customer, salesperson, installer, and administrator portal access</p>{safe_error}{safe_message}<form class="auth-form" method="post" action="/login"><input type="hidden" name="next_url" value="{escape(next_url)}"><label>Email<input name="email" type="email" autocomplete="username" required autofocus></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><label class="auth-remember"><input name="remember_me" type="checkbox" value="true"><span>Keep me signed in for 30 days</span></label><button class="action-button" type="submit">Sign in</button></form><div style="margin-top:16px;text-align:center"><a class="compact-button" href="/customer-register">Create customer account</a></div><div class="auth-footer">New accounts remain pending until the master administrator approves them.</div></section></main>{auth_form_script()}</body></html>"""



def customer_register_page_html(error: str = "", message: str = "") -> str:
    safe_error = f'<div class="auth-error">{escape(error)}</div>' if error else ""
    safe_message = f'<div class="auth-success">{escape(message)}</div>' if message else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Create customer account · AnyAiCam</title><style>{STYLES}</style></head><body><main class="auth-page"><section class="auth-card"><img class="auth-logo" src="/static/brand-icon.png" alt="AnyAiCam"><h1>Create customer account</h1><p class="auth-subtitle">Request secure access to your ANY AI CAM customer portal.</p>{safe_error}{safe_message}<form class="auth-form" method="post" action="/customer-register"><label>Full name<input name="display_name" minlength="2" maxlength="120" required autofocus></label><label>Email<input name="email" type="email" autocomplete="email" required></label><label>Create password<input name="password" type="password" minlength="10" autocomplete="new-password" required></label><button class="action-button" type="submit">Submit customer account request</button></form><div style="margin-top:16px;text-align:center"><a class="compact-button" href="/login">Already approved? Sign in</a></div><div class="auth-footer">Your request remains pending until the master administrator approves it. After approval, signing in sends you directly to your customer VMS portal.</div></section></main>{auth_form_script()}</body></html>"""


def register_page_html(error: str = "", message: str = "") -> str:
    safe_error = f'<div class="auth-error">{escape(error)}</div>' if error else ""
    safe_message = f'<div class="auth-success">{escape(message)}</div>' if message else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Create account · AnyAiCam</title><style>{STYLES}</style></head><body><main class="auth-page"><section class="auth-card"><img class="auth-logo" src="/static/brand-icon.png" alt="AnyAiCam"><h1>Create account</h1><p class="auth-subtitle">Submit an account request for master-administrator approval.</p>{safe_error}{safe_message}<form class="auth-form" method="post" action="/register"><label>Full name<input name="display_name" minlength="2" maxlength="120" required autofocus></label><label>Email<input name="email" type="email" autocomplete="email" required></label><label>Create password<input name="password" type="password" minlength="10" autocomplete="new-password" required></label><label>Account type<select name="requested_role" required><option value="customer_owner">Customer</option><option value="partner_sales">Salesperson</option><option value="installer">Installer</option><option value="administrator">Administrator</option></select></label><button class="action-button" type="submit">Submit account request</button></form><div style="margin-top:16px;text-align:center"><a class="compact-button" href="/login">Back to sign in</a></div><div class="auth-footer">Submitting a request does not grant access. The protected master administrator must approve it first.</div></section></main>{auth_form_script()}</body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> HTMLResponse:
    user = authenticated_user(request)
    if user:
        return RedirectResponse(
            safe_login_destination(user, next),
            status_code=303,
        )
    return HTMLResponse(login_page_html(next_url=next))



@app.get("/customer-register", response_class=HTMLResponse)
def customer_register_page(request: Request) -> HTMLResponse:
    if authenticated_user(request):
        return RedirectResponse(
            portal_destination_for_user(current_user(request)),
            status_code=303,
        )
    return HTMLResponse(customer_register_page_html())


@app.post("/customer-register", response_class=HTMLResponse)
def customer_register_submit(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    normalized_name = display_name.strip()
    normalized_email = email.strip().lower()

    if len(normalized_name) < 2:
        return HTMLResponse(
            customer_register_page_html("Enter your full name."),
            status_code=422,
        )
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", normalized_email):
        return HTMLResponse(
            customer_register_page_html("Enter a valid email address."),
            status_code=422,
        )
    if len(password) < 10:
        return HTMLResponse(
            customer_register_page_html(
                "Password must contain at least 10 characters."
            ),
            status_code=422,
        )

    users = load_users()
    existing = next(
        (
            item for item in users
            if item.get("email", "").strip().lower() == normalized_email
        ),
        None,
    )
    if existing:
        if existing.get("invitation_status") == "pending":
            return HTMLResponse(
                customer_register_page_html(
                    message="Your customer account request is already pending approval."
                ),
                status_code=200,
            )
        return HTMLResponse(
            customer_register_page_html(
                "That email is already registered. Use the sign-in page."
            ),
            status_code=409,
        )

    payload = UserModel(
        display_name=normalized_name,
        email=normalized_email,
        role="customer_owner",
        enabled=False,
        super_admin=False,
        site_ids=["home"],
        camera_ids=[],
        password_hash=hash_password(password),
        invitation_status="pending",
    ).model_dump(mode="json")
    payload["requested_at"] = datetime.now().isoformat()
    payload["registration_source"] = "public_customer_registration"
    users.append(payload)
    save_users(users)

    append_audit_entry(
        AuditEntryModel(
            user_id=payload["id"],
            user_name=normalized_name,
            role="customer_owner",
            action="customer_account_request",
            resource=f"user:{payload['id']}",
            detail="Requested customer portal access from the public website.",
            device=request.headers.get("user-agent", "Web browser")[:160],
            outcome="pending",
        ).model_dump(mode="json")
    )

    return HTMLResponse(
        login_page_html(
            message=(
                "Your customer account request was submitted. "
                "After the master administrator approves it, sign in to open your VMS portal."
            )
        ),
        status_code=200,
    )


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request) -> HTMLResponse:
    if authenticated_user(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(register_page_html())


@app.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    requested_role: str = Form(...),
) -> HTMLResponse:
    normalized_name = display_name.strip()
    normalized_email = email.strip().lower()
    normalized_role = requested_role.strip().lower()

    if len(normalized_name) < 2:
        return HTMLResponse(register_page_html("Enter your full name."), status_code=422)
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", normalized_email):
        return HTMLResponse(register_page_html("Enter a valid email address."), status_code=422)
    if len(password) < 10:
        return HTMLResponse(register_page_html("Password must contain at least 10 characters."), status_code=422)
    if normalized_role not in PUBLIC_BUSINESS_REGISTRATION_ROLES:
        return HTMLResponse(register_page_html("Choose a valid account type."), status_code=422)

    users = load_users()
    existing = next(
        (
            item for item in users
            if item.get("email", "").strip().lower() == normalized_email
        ),
        None,
    )
    if existing:
        if existing.get("invitation_status") == "pending":
            return HTMLResponse(
                register_page_html(
                    message="That account request is already pending approval."
                ),
                status_code=200,
            )
        return HTMLResponse(
            register_page_html("That email is already registered."),
            status_code=409,
        )

    payload = UserModel(
        display_name=normalized_name,
        email=normalized_email,
        role=normalized_role,
        enabled=False,
        super_admin=False,
        site_ids=["home"],
        camera_ids=[],
        password_hash=hash_password(password),
        invitation_status="pending",
    ).model_dump(mode="json")
    payload["requested_at"] = datetime.now().isoformat()
    users.append(payload)
    save_users(users)

    append_audit_entry(
        AuditEntryModel(
            user_id=payload["id"],
            user_name=normalized_name,
            role=normalized_role,
            action="business_account_request",
            resource=f"user:{payload['id']}",
            detail=f"Requested role {normalized_role}.",
            device=request.headers.get("user-agent", "Web browser")[:160],
            outcome="pending",
        ).model_dump(mode="json")
    )

    return HTMLResponse(
        login_page_html(
            message="Your account request was submitted. Wait for master-administrator approval before signing in."
        ),
        status_code=200,
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    remember_me: str | None = Form(default=None),
    next_url: str = Form(default="/"),
):
    normalized_email = email.strip().lower()
    users = load_users()
    user = next((item for item in users if item.get("email", "").strip().lower() == normalized_email), None)

    if user and not user.get("enabled", True) and user.get("invitation_status") == "pending":
        return HTMLResponse(
            login_page_html(
                "Your account request is still pending master-administrator approval.",
                next_url,
            ),
            status_code=403,
        )

    if not user or not user.get("enabled", True):
        append_audit_entry(
            AuditEntryModel(
                user_id=user.get("id", "unknown") if user else "unknown",
                user_name=user.get("display_name", normalized_email) if user else normalized_email,
                role=user.get("role", "unknown") if user else "unknown",
                action="login",
                resource="session",
                detail="Invalid credentials or disabled account.",
                device=request.headers.get("user-agent", "Web browser")[:160],
                outcome="denied",
            ).model_dump(mode="json")
        )
        return HTMLResponse(login_page_html("Invalid email or password.", next_url), status_code=401)

    locked_until = user.get("locked_until")
    if locked_until:
        try:
            lock_time = datetime.fromisoformat(locked_until)
            if lock_time > datetime.now():
                return HTMLResponse(
                    login_page_html(
                        f"Account locked until {lock_time.strftime('%Y-%m-%d %H:%M')}.",
                        next_url,
                    ),
                    status_code=423,
                )
        except (TypeError, ValueError):
            user["locked_until"] = None

    if not verify_password(password, user.get("password_hash", "")):
        user["failed_login_attempts"] = int(user.get("failed_login_attempts", 0)) + 1
        if user["failed_login_attempts"] >= MAX_LOGIN_ATTEMPTS:
            user["locked_until"] = (
                datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)
            ).isoformat()
            user["failed_login_attempts"] = 0
        save_users(users)
        append_audit_entry(
            AuditEntryModel(
                user_id=user.get("id", "unknown"),
                user_name=user.get("display_name", normalized_email),
                role=user.get("role", "unknown"),
                action="login",
                resource="session",
                detail="Invalid password.",
                device=request.headers.get("user-agent", "Web browser")[:160],
                outcome="denied",
            ).model_dump(mode="json")
        )
        return HTMLResponse(login_page_html("Invalid email or password.", next_url), status_code=401)

    user["failed_login_attempts"] = 0
    user["locked_until"] = None
    user["last_login"] = datetime.now().isoformat()
    save_users(users)

    token = create_session(user["id"], remember_me == "true")
    append_audit_entry(
        AuditEntryModel(
            user_id=user["id"],
            user_name=user.get("display_name", normalized_email),
            role=user.get("role", "viewer"),
            action="login",
            resource="session",
            detail="Login successful.",
            device=request.headers.get("user-agent", "Web browser")[:160],
            outcome="success",
        ).model_dump(mode="json")
    )
    destination = safe_login_destination(user, next_url)
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=30 * 24 * 3600 if remember_me == "true" else SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=SECURE_COOKIES,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request):
    user = authenticated_user(request)
    destroy_session(request.cookies.get(SESSION_COOKIE_NAME))
    if user:
        append_audit_entry(
            AuditEntryModel(
                user_id=user["id"],
                user_name=user.get("display_name", "User"),
                role=user.get("role", "viewer"),
                action="logout",
                resource="session",
                detail="Logout successful.",
                device=request.headers.get("user-agent", "Web browser")[:160],
                outcome="success",
            ).model_dump(mode="json")
        )
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response




STYLES = """
.sidebar{display:flex;flex-direction:column}.nav{flex:1}.sidebar-auth{margin-top:auto;padding:10px 5px 4px}.sidebar-logout{width:100%;min-height:54px;display:flex;align-items:center;justify-content:center;gap:7px;padding:10px 6px;border:1px solid rgba(255,255,255,.2);border-radius:9px;background:#24292c;color:#fff;font:inherit;font-weight:750;cursor:pointer}.sidebar-logout:hover,.sidebar-logout:focus-visible{background:#343b3f;border-color:var(--brand);outline:none}.sidebar-logout-icon{font-size:18px;line-height:1}@media(max-width:760px){.sidebar-auth{display:none}}:root{color-scheme:dark;--bg:#0a0d12;--panel:#121720;--panel2:#181e28;--line:#27303d;--text:#f4f7fb;--muted:#8f9baa;--accent:#47d7ac;--blue:#70a5ff;--danger:#ff6b6b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}.shell{min-height:100vh;display:grid;grid-template-columns:230px 1fr}.sidebar{position:sticky;top:0;height:100vh;padding:24px 16px;border-right:1px solid var(--line);background:#0e1218}.brand{display:flex;align-items:center;gap:11px;padding:0 9px 28px;font-weight:750;letter-spacing:-.02em}.brand-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:var(--accent);color:#07110e;font-size:18px}.nav{display:grid;gap:6px}.nav a{display:flex;align-items:center;gap:12px;padding:12px 13px;border-radius:10px;color:var(--muted);text-decoration:none;font-weight:650}.nav a:hover{background:var(--panel2);color:var(--text)}.nav a.active{background:#193329;color:#7ee8c7}.sidebar-foot{position:absolute;left:16px;right:16px;bottom:22px;padding:13px;border:1px solid var(--line);border-radius:12px;color:var(--muted);font-size:12px}.sidebar-foot strong{display:block;color:var(--text);font-size:13px;margin-bottom:3px}.content{min-width:0;padding:30px 34px 50px;max-width:1600px;width:100%;margin:0 auto}.topbar{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:26px}.eyebrow{margin:0 0 5px;color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}h1{margin:0;font-size:clamp(25px,3vw,34px);letter-spacing:-.035em}h2{margin:0;font-size:18px}.clock{color:var(--muted);font-size:13px}.summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:24px}.stat{padding:16px 18px;border:1px solid var(--line);border-radius:14px;background:var(--panel)}.stat-label{display:block;color:var(--muted);font-size:12px;margin-bottom:7px}.stat-value{font-size:18px;font-weight:750}.dot{display:inline-block;width:8px;height:8px;margin-right:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px rgba(71,215,172,.1)}.section-head{display:flex;align-items:end;justify-content:space-between;margin:0 0 14px}.section-head p{margin:0;color:var(--muted);font-size:13px}.camera-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.camera-card{overflow:hidden;border:1px solid var(--line);border-radius:16px;background:var(--panel)}.camera-view{position:relative;display:grid;place-items:center;aspect-ratio:16/9;background:#080a0e;overflow:hidden}.camera-view video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#080a0e}.camera-placeholder{text-align:center;color:var(--muted);padding:20px}.camera-placeholder .signal{display:block;margin:0 auto 12px;font-size:25px}.camera-placeholder strong{display:block;color:#cad2dd;margin-bottom:4px}.live-badge{position:absolute;z-index:2;top:12px;left:12px;padding:6px 9px;border-radius:8px;background:rgba(8,10,14,.78);font-size:11px;font-weight:800;letter-spacing:.08em}.live-badge::before{content:"";display:inline-block;width:7px;height:7px;margin-right:6px;border-radius:50%;background:var(--danger)}.camera-meta{display:flex;justify-content:space-between;gap:15px;padding:14px 16px}.camera-name{font-weight:700}.camera-state{color:var(--muted);font-size:12px}.camera-state.ready{color:var(--accent)}.library-toolbar{display:flex;gap:10px;margin-bottom:18px;overflow:auto}.filter{border:1px solid var(--line);border-radius:999px;background:transparent;color:var(--muted);padding:8px 13px;cursor:pointer;white-space:nowrap}.filter.active{background:var(--text);border-color:var(--text);color:#0b0e13}.camera-section{margin-bottom:28px}.recording-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:12px}.clip{overflow:hidden;border:1px solid var(--line);border-radius:14px;background:var(--panel)}.clip video{display:block;width:100%;aspect-ratio:16/9;background:#07090c}.clip-body{padding:13px}.clip-time{font-weight:700;font-size:14px}.clip-meta{margin:5px 0 12px;color:var(--muted);font-size:12px}.download{color:var(--blue);text-decoration:none;font-size:13px;font-weight:700}.empty{padding:28px;border:1px dashed var(--line);border-radius:14px;color:var(--muted);text-align:center;background:rgba(18,23,32,.5)}.mobile-nav{display:none}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}@media(max-width:980px){.recording-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:760px){.shell{display:block}.sidebar{display:none}.content{padding:23px 16px 90px}.clock{display:none}.summary{grid-template-columns:1fr}.camera-grid,.recording-grid{grid-template-columns:1fr}.mobile-nav{position:fixed;z-index:20;display:grid;grid-template-columns:1fr 1fr;left:12px;right:12px;bottom:12px;padding:6px;border:1px solid var(--line);border-radius:15px;background:rgba(18,23,32,.94);backdrop-filter:blur(14px)}.mobile-nav a{text-align:center;padding:10px;border-radius:10px;color:var(--muted);text-decoration:none;font-weight:700;font-size:13px}.mobile-nav a.active{background:#193329;color:#7ee8c7}}
.brand{display:flex;align-items:center;padding:0 9px 28px}.brand-logo{display:block;width:74px;height:52px;object-fit:contain;object-position:left center}
.date-filter{height:36px;padding:0 12px;border:1px solid var(--line);border-radius:999px;background:transparent;color:var(--text);font:inherit;color-scheme:dark}
.layout-controls{display:flex;gap:5px}.layout-button{min-width:34px;height:32px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--muted);cursor:pointer}.layout-button.active{border-color:#627083;background:var(--panel2);color:var(--text)}.camera-grid[data-layout="1"]{grid-template-columns:minmax(0,1fr)}.camera-grid[data-layout="4"]{grid-template-columns:repeat(2,minmax(0,1fr))}.camera-grid[data-layout="9"]{grid-template-columns:repeat(3,minmax(0,1fr))}.camera-grid[data-layout="16"]{grid-template-columns:repeat(4,minmax(0,1fr))}.analytics-overlay{position:absolute;z-index:3;inset:14%;border:2px dashed rgba(65,216,207,.8);border-radius:8px;color:#71ede5;display:none;place-items:center;font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;background:rgba(65,216,207,.04)}.analytics-overlay.visible{display:grid}@media(max-width:1100px){.camera-grid[data-layout="9"],.camera-grid[data-layout="16"]{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:760px){.camera-grid[data-layout]{grid-template-columns:1fr}.layout-controls{display:none}}
.nav-group-label{padding:18px 13px 7px;color:#596575;font-size:10px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.camera-tools{display:flex;gap:4px;padding:8px 10px;border-top:1px solid var(--line);overflow-x:auto}.camera-tool{flex:0 0 auto;display:grid;place-items:center;width:34px;height:32px;border:0;border-radius:8px;background:transparent;color:var(--muted);cursor:pointer;font-size:15px}.camera-tool:hover{background:var(--panel2);color:var(--text)}.toast{position:fixed;z-index:50;right:20px;bottom:20px;padding:12px 16px;border:1px solid var(--line);border-radius:10px;background:#1a202a;color:var(--text);box-shadow:0 12px 35px rgba(0,0,0,.35);opacity:0;transform:translateY(10px);pointer-events:none;transition:.2s}.toast.show{opacity:1;transform:none}.health-grid{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:24px}.panel{padding:19px;border:1px solid var(--line);border-radius:16px;background:var(--panel)}.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}.health-list,.activity-list{display:grid;gap:2px}.health-row,.activity-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:13px 0;border-top:1px solid var(--line)}.health-row:first-child,.activity-row:first-child{border-top:0}.health-name{font-weight:650}.health-detail,.activity-time{color:var(--muted);font-size:12px}.pill{padding:5px 8px;border-radius:999px;background:#193329;color:#7ee8c7;font-size:11px;font-weight:750}.pill.wait{background:#302a1d;color:#f0ca72}.storage-bar{height:9px;margin:15px 0 8px;border-radius:99px;background:#252d38;overflow:hidden}.storage-bar span{display:block;width:18%;height:100%;border-radius:inherit;background:linear-gradient(90deg,#bd2a8b,#41d8cf)}.feature-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.feature-card{min-height:150px;padding:18px;border:1px solid var(--line);border-radius:15px;background:var(--panel)}.feature-icon{display:grid;place-items:center;width:38px;height:38px;margin-bottom:24px;border-radius:10px;background:#242b37;color:#cbd4df}.feature-card p{margin:7px 0 0;color:var(--muted);font-size:13px;line-height:1.45}.settings-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.setting-link{display:flex;align-items:center;justify-content:space-between;padding:17px;border:1px solid var(--line);border-radius:13px;background:var(--panel);color:var(--text);text-decoration:none}.setting-link span{color:var(--muted)}.coming{display:inline-block;margin-top:14px;color:var(--blue);font-size:11px;font-weight:750;text-transform:uppercase;letter-spacing:.08em}@media(max-width:980px){.feature-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.health-grid{grid-template-columns:1fr}}@media(max-width:760px){.feature-grid,.settings-list{grid-template-columns:1fr}.toast{left:16px;right:16px;bottom:82px}}
/* Branded surveillance workspace */
:root{--brand:#43d1cc;--brand-soft:#b9f8f3;--brand-action:#4b4de2;--workspace-top:#174f5f;--workspace-bottom:#424858;--rail:#131515;--surface:#192234;--surface2:#283244;--accent:var(--brand);--panel:var(--surface);--panel2:var(--surface2);--line:#4a5769}
body{background:var(--workspace-bottom)}.shell{grid-template-columns:112px minmax(0,1fr);background:linear-gradient(180deg,var(--workspace-top),var(--workspace-bottom))}.sidebar{z-index:20;width:112px;padding:18px 6px;background:var(--rail);border:0;overflow-y:auto;scrollbar-width:none}.brand{justify-content:center;padding:0 5px 22px}.brand-logo{width:72px;height:68px;object-position:center}.nav{gap:3px}.nav a{min-height:75px;display:flex;flex-direction:column;justify-content:center;gap:5px;padding:8px 4px;border-radius:0;text-align:center;font-size:12px;line-height:1.08;color:#f6f7f8}.nav a:hover{background:#24292c}.nav a.active{background:var(--brand);color:#102a30}.nav-icon{font-size:25px;line-height:1}.sidebar-foot{
    display:block;
    position:static;
    margin-top:18px;
    padding:8px;
}.content{max-width:none;padding:24px 34px 48px}.eyebrow{color:var(--brand-soft)}.stat,.panel,.feature-card,.setting-link,.camera-card,.clip{background:rgba(24,33,50,.94);border-color:rgba(170,196,207,.18);box-shadow:0 7px 20px rgba(7,12,20,.12)}.camera-card{border-radius:7px}.camera-tools{background:#121a28}.filter.active,.layout-button.active{background:var(--brand-action);border-color:var(--brand-action);color:white}.download{color:#8df0ea}.pill{background:#315c5d;color:#9ff7f1}.workspace-tabs{display:grid;grid-template-columns:repeat(3,1fr);margin-bottom:24px;padding:7px;border-radius:9px;background:#182234}.workspace-tab{padding:12px;border:0;border-radius:7px;background:transparent;color:#f5f7fb;text-align:center;font:inherit;font-weight:700}.workspace-tab.active{background:var(--brand-soft);color:#15343d}.live-workspace,.playback-workspace{display:grid;grid-template-columns:280px minmax(0,1fr);gap:22px}.camera-picker{align-self:start;min-height:420px;padding:14px;border-radius:12px;background:#172134;box-shadow:0 7px 20px rgba(7,12,20,.2)}.picker-head{padding:12px 15px;border-radius:999px;background:var(--brand-action);font-weight:750}.picker-search{width:100%;margin:18px 0 12px;padding:10px 13px;border:1px solid #b8c2cc;border-radius:999px;background:transparent;color:white}.picker-camera{display:flex;align-items:center;gap:10px;padding:12px 8px;color:#eef2f6}.picker-camera input{accent-color:var(--brand)}.work-area{min-width:0}.action-button{padding:10px 17px;border:0;border-radius:999px;background:var(--brand-action);color:white;font:inherit;font-weight:700}.ghost-button{padding:10px 17px;border:1px solid #c0cad3;border-radius:999px;background:transparent;color:white;font:inherit}.data-table{width:100%;border-collapse:collapse;background:rgba(25,34,51,.92)}.data-table th{padding:15px;text-align:left;background:#161827}.data-table td{padding:15px;border-top:1px solid #596473;color:#eef2f4}.empty-stage{min-height:420px;display:grid;place-items:center;text-align:center;color:#cbd5dc;font-size:22px;font-weight:700}.timeline-shell{margin-top:18px;padding:18px;border-radius:12px;background:#171a2a}.timeline-controls{display:flex;justify-content:center;gap:20px;font-size:24px;color:#9ea7b5}.timeline-track{height:54px;margin-top:14px;border-top:2px solid #b5bec7;background:repeating-linear-gradient(90deg,transparent 0 24px,rgba(255,255,255,.35) 25px 26px)}@media(max-width:900px){.live-workspace,.playback-workspace{grid-template-columns:1fr}.camera-picker{min-height:auto}.shell{grid-template-columns:86px minmax(0,1fr)}.sidebar{width:86px}.content{padding:20px}.nav a{min-height:68px;font-size:11px}}@media(max-width:760px){.shell{display:block}.sidebar{display:none}.content{padding:18px 14px 92px}.mobile-nav{grid-template-columns:repeat(4,1fr)}}
"""

STYLES += """
.launch-summary{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;margin-bottom:16px}.launch-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.launch-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.launch-stat strong{font-size:23px}.launch-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(340px,1fr);gap:16px}.launch-stack{display:grid;gap:14px}.launch-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.launch-card h2,.launch-card h3{margin-top:0}.launch-checks{display:grid;gap:9px}.launch-check{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:start;padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.launch-check strong{display:block}.launch-check small{color:var(--muted);line-height:1.45}.launch-icon{width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#25334a;font-weight:900}.launch-icon.ok{background:#20433f;color:#84f0e5}.launch-icon.warn{background:#44391f;color:#f0cd79}.launch-icon.fail{background:#48252a;color:#ff9ca7}.launch-note{padding:12px;border-left:3px solid var(--brand);border-radius:0 9px 9px 0;background:#111827;color:#cbd7df;font-size:11px;line-height:1.55}.launch-actions{display:flex;gap:8px;flex-wrap:wrap}.launch-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font-size:10px;font-weight:800}.launch-verdict{padding:18px;border-radius:14px;text-align:center;background:#111827}.launch-verdict strong{display:block;font-size:32px;margin-bottom:6px}.launch-verdict.go{border:1px solid #315d53;color:#a8f5df}.launch-verdict.no-go{border:1px solid #7b3540;color:#ffb2bd}.launch-progress{height:11px;border-radius:999px;background:#0d1522;overflow:hidden;margin:10px 0}.launch-progress span{display:block;height:100%;background:var(--brand-action)}.launch-runbook{counter-reset:launchstep;display:grid;gap:9px}.launch-step{position:relative;padding:12px 12px 12px 44px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.launch-step:before{counter-increment:launchstep;content:counter(launchstep);position:absolute;left:12px;top:12px;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#25334a;font-weight:900}.launch-step strong{display:block;margin-bottom:4px}.launch-step small{color:var(--muted);line-height:1.5}@media(max-width:1300px){.launch-summary{grid-template-columns:repeat(4,minmax(0,1fr))}}@media(max-width:980px){.launch-grid{grid-template-columns:1fr}.launch-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.launch-summary{grid-template-columns:1fr}.launch-check{grid-template-columns:auto 1fr}}.prod-summary{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:16px}.prod-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.prod-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.prod-stat strong{font-size:23px}.prod-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(340px,1fr);gap:16px}.prod-stack{display:grid;gap:14px}.prod-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.prod-card h2,.prod-card h3{margin-top:0}.prod-checks{display:grid;gap:9px}.prod-check{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:start;padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.prod-check strong{display:block}.prod-check small{color:var(--muted);line-height:1.45}.prod-icon{width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#25334a;font-weight:900}.prod-icon.ok{background:#20433f;color:#84f0e5}.prod-icon.warn{background:#44391f;color:#f0cd79}.prod-icon.fail{background:#48252a;color:#ff9ca7}.prod-note{padding:12px;border-left:3px solid var(--brand);border-radius:0 9px 9px 0;background:#111827;color:#cbd7df;font-size:11px;line-height:1.55}.prod-actions{display:flex;gap:8px;flex-wrap:wrap}.prod-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font-size:10px;font-weight:800}.prod-code{padding:12px;border-radius:9px;background:#0c1320;border:1px solid rgba(170,196,207,.14);overflow:auto;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:10px;line-height:1.55;white-space:pre-wrap}@media(max-width:1200px){.prod-summary{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:900px){.prod-grid{grid-template-columns:1fr}.prod-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.prod-summary{grid-template-columns:1fr}.prod-check{grid-template-columns:auto 1fr}}.ir-summary{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;margin-bottom:16px}.ir-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.ir-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.ir-stat strong{font-size:23px}.ir-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(340px,1fr);gap:16px}.ir-stack{display:grid;gap:14px}.ir-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.ir-card h2,.ir-card h3{margin-top:0}.ir-toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.ir-toolbar input,.ir-toolbar select{min-height:38px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit}.ir-list{display:grid;gap:8px}.ir-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:start;padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.ir-row strong{display:block}.ir-row small{color:var(--muted);line-height:1.45}.ir-dot{width:11px;height:11px;border-radius:50%;margin-top:4px;background:#76869a}.ir-dot.low{background:#4bd58b}.ir-dot.medium{background:#f2c96c}.ir-dot.high,.ir-dot.critical{background:#ff6577}.ir-actions{display:flex;gap:8px;flex-wrap:wrap}.ir-actions a,.ir-actions button{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;font-size:10px;font-weight:800;cursor:pointer}.ir-actions .primary{background:var(--brand-action);border-color:var(--brand-action)}.ir-editor label{display:grid;gap:6px;margin:9px 0;font-size:11px;font-weight:800}.ir-editor input,.ir-editor select,.ir-editor textarea{width:100%;min-height:40px;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit}.ir-editor textarea{min-height:95px;resize:vertical}.ir-feedback{margin-top:10px;padding:10px;border-radius:8px;background:#111827;color:#cbd7df;font-size:11px}.ir-feedback.success{background:#203c36;color:#a8f5df}.ir-feedback.error{background:#3a2025;color:#ffb2bd}.ir-note{padding:12px;border-left:3px solid var(--brand);border-radius:0 9px 9px 0;background:#111827;color:#cbd7df;font-size:11px;line-height:1.55}.ir-metric{display:grid;grid-template-columns:1fr auto;gap:8px;padding:10px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.ir-metric small{color:var(--muted)}@media(max-width:1300px){.ir-summary{grid-template-columns:repeat(4,minmax(0,1fr))}}@media(max-width:980px){.ir-grid{grid-template-columns:1fr}.ir-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.ir-summary{grid-template-columns:1fr}.ir-row{grid-template-columns:auto 1fr}}.training-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}.training-card{border:1px solid rgba(170,196,207,.18);border-radius:13px;overflow:hidden;background:#182234}.training-preview{aspect-ratio:16/9;background:#111827;display:grid;place-items:center;overflow:hidden}.training-preview img{width:100%;height:100%;object-fit:cover}.training-file-icon{font-size:48px;color:#7dd8d5}.training-body{padding:14px}.training-body h3{margin:10px 0 6px}.training-body p{color:var(--muted);line-height:1.5;min-height:42px}.training-body small{color:var(--muted)}.training-actions{display:flex;gap:8px;margin-top:12px}.training-actions a{display:inline-flex;align-items:center;justify-content:center;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:#fff;text-decoration:none;font-size:10px;font-weight:800}.training-admin form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.training-admin label{display:grid;gap:6px;font-weight:800}.training-admin textarea{min-height:90px}.training-admin button,.training-admin details{grid-column:1/-1}.not-configured{padding:16px;border:1px dashed rgba(170,196,207,.4);border-radius:12px;background:#111827;color:#aab8c5}.not-configured strong{display:block;color:#fff;margin-bottom:5px}@media(max-width:700px){.training-admin form{grid-template-columns:1fr}}.dr-summary{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:16px}.dr-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.dr-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.dr-stat strong{font-size:23px}.dr-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:16px}.dr-stack{display:grid;gap:14px}.dr-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.dr-card h2,.dr-card h3{margin-top:0}.dr-list{display:grid;gap:9px}.dr-row{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(120px,.6fr) auto;gap:12px;align-items:center;padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.dr-row strong{display:block}.dr-row small{color:var(--muted);line-height:1.45}.dr-actions{display:flex;gap:8px;flex-wrap:wrap}.dr-actions a,.dr-actions button{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;font-size:10px;font-weight:800;cursor:pointer}.dr-actions .primary{background:var(--brand-action);border-color:var(--brand-action)}.dr-note{padding:12px;border-left:3px solid var(--brand);border-radius:0 9px 9px 0;background:#111827;color:#cbd7df;font-size:11px;line-height:1.55}.dr-checklist{display:grid;gap:9px}.dr-check{padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.dr-check strong{display:block;margin-bottom:4px}.dr-check small{color:var(--muted);line-height:1.5}.dr-progress{height:10px;border-radius:999px;background:#0d1522;overflow:hidden;margin:10px 0}.dr-progress span{display:block;height:100%;background:var(--brand-action)}@media(max-width:1200px){.dr-summary{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:900px){.dr-grid{grid-template-columns:1fr}.dr-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.dr-summary{grid-template-columns:1fr}.dr-row{grid-template-columns:1fr}}.obs-summary{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:16px}.obs-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.obs-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.obs-stat strong{font-size:23px}.obs-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:16px}.obs-stack{display:grid;gap:14px}.obs-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.obs-card h2,.obs-card h3{margin-top:0}.obs-list{display:grid;gap:9px}.obs-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:start;padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.obs-row strong{display:block}.obs-row small{color:var(--muted);line-height:1.45}.obs-dot{width:12px;height:12px;border-radius:50%;margin-top:4px;background:#6f7f91}.obs-dot.ok{background:#4bd58b}.obs-dot.warn{background:#f2c96c}.obs-dot.fail{background:#ff6577}.obs-actions{display:flex;gap:8px;flex-wrap:wrap}.obs-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font-size:10px;font-weight:800}.obs-note{padding:12px;border-left:3px solid var(--brand);border-radius:0 9px 9px 0;background:#111827;color:#cbd7df;font-size:11px;line-height:1.55}.obs-runbook{counter-reset:runbook}.obs-step{position:relative;padding:12px 12px 12px 44px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.obs-step:before{counter-increment:runbook;content:counter(runbook);position:absolute;left:12px;top:12px;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#25334a;font-weight:900}.obs-step strong{display:block;margin-bottom:4px}.obs-step small{color:var(--muted);line-height:1.5}@media(max-width:1200px){.obs-summary{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:900px){.obs-grid{grid-template-columns:1fr}.obs-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.obs-summary{grid-template-columns:1fr}.obs-row{grid-template-columns:auto 1fr}}.enterprise-summary{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:16px}.enterprise-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.enterprise-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.enterprise-stat strong{font-size:23px}.enterprise-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:16px}.enterprise-stack{display:grid;gap:14px}.enterprise-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.enterprise-card h2,.enterprise-card h3{margin-top:0}.enterprise-checks{display:grid;gap:9px}.enterprise-check{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:start;padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.enterprise-check strong{display:block}.enterprise-check small{color:var(--muted);line-height:1.45}.enterprise-icon{width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:#25334a;font-weight:900}.enterprise-icon.ok{background:#20433f;color:#84f0e5}.enterprise-icon.warn{background:#44391f;color:#f0cd79}.enterprise-icon.fail{background:#48252a;color:#ff9ca7}.enterprise-progress{height:11px;border-radius:999px;background:#0d1522;overflow:hidden;margin:10px 0}.enterprise-progress span{display:block;height:100%;background:var(--brand-action)}.enterprise-actions{display:flex;gap:8px;flex-wrap:wrap}.enterprise-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font-size:10px;font-weight:800}.enterprise-note{padding:12px;border-left:3px solid var(--brand);border-radius:0 9px 9px 0;background:#111827;color:#cbd7df;font-size:11px;line-height:1.55}@media(max-width:1200px){.enterprise-summary{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:900px){.enterprise-grid{grid-template-columns:1fr}.enterprise-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.enterprise-summary{grid-template-columns:1fr}.enterprise-check{grid-template-columns:auto 1fr}}.partner-performance-summary{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:16px}.partner-performance-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.partner-performance-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.partner-performance-stat strong{font-size:23px}.partner-performance-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:16px}.partner-performance-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.partner-performance-card h2,.partner-performance-card h3{margin-top:0}.partner-performance-list{display:grid;gap:9px}.partner-performance-row{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(100px,.7fr) minmax(100px,.7fr);gap:12px;align-items:center;padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.partner-performance-row strong{display:block}.partner-performance-row small{color:var(--muted);line-height:1.45}.partner-performance-bar{height:10px;border-radius:999px;background:#0d1522;overflow:hidden;margin-top:8px}.partner-performance-bar span{display:block;height:100%;background:var(--brand-action)}.partner-performance-note{padding:12px;border-left:3px solid var(--brand);border-radius:0 9px 9px 0;background:#111827;color:#cbd7df;font-size:11px;line-height:1.55}.partner-performance-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.partner-performance-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font-size:10px;font-weight:800}@media(max-width:1200px){.partner-performance-summary{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:900px){.partner-performance-grid{grid-template-columns:1fr}.partner-performance-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.partner-performance-summary{grid-template-columns:1fr}.partner-performance-row{grid-template-columns:1fr}}.install-summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:16px}.install-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.install-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.install-stat strong{font-size:23px}.install-layout{display:grid;grid-template-columns:minmax(0,2fr) minmax(340px,1fr);gap:16px}.install-list{display:grid;gap:10px}.install-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.install-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.install-meta{margin-top:6px;color:var(--muted);font-size:10px;line-height:1.55}.install-progress{height:9px;border-radius:999px;background:#0d1522;overflow:hidden;margin-top:10px}.install-progress span{display:block;height:100%;background:var(--brand-action)}.install-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:11px}.install-actions button,.install-actions a{min-height:36px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;font-size:10px;font-weight:800;cursor:pointer}.install-actions .primary{background:var(--brand-action);border-color:var(--brand-action)}.install-editor{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234;position:sticky;top:18px}.install-editor label{display:grid;gap:6px;margin:10px 0;font-size:11px;font-weight:800}.install-editor input,.install-editor select,.install-editor textarea{width:100%;min-height:40px;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit}.install-editor textarea{min-height:95px;resize:vertical}.install-checklist{display:grid;gap:8px}.install-check{display:flex!important;align-items:flex-start;gap:8px;margin:0!important;padding:10px;border:1px solid rgba(170,196,207,.14);border-radius:8px;background:#111827;font-weight:700!important}.install-feedback{margin-top:10px;padding:10px;border-radius:8px;background:#111827;color:#cbd7df;font-size:11px}.install-feedback.success{background:#203c36;color:#a8f5df}.install-feedback.error{background:#3a2025;color:#ffb2bd}@media(max-width:1000px){.install-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.install-layout{grid-template-columns:1fr}.install-editor{position:static}}@media(max-width:620px){.install-summary{grid-template-columns:1fr}.install-head{display:block}}.quote-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:16px}.quote-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.quote-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.quote-stat strong{font-size:23px}.quote-layout{display:grid;grid-template-columns:minmax(0,2fr) minmax(340px,1fr);gap:16px}.quote-list{display:grid;gap:10px}.quote-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.quote-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.quote-meta{margin-top:6px;color:var(--muted);font-size:10px;line-height:1.55}.quote-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:11px}.quote-actions button,.quote-actions a{min-height:36px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;font-size:10px;font-weight:800;cursor:pointer}.quote-actions .primary{background:var(--brand-action);border-color:var(--brand-action)}.quote-editor{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234;position:sticky;top:18px}.quote-editor label{display:grid;gap:6px;margin:10px 0;font-size:11px;font-weight:800}.quote-editor input,.quote-editor select,.quote-editor textarea{width:100%;min-height:40px;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit}.quote-editor textarea{min-height:95px;resize:vertical}.quote-analytics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.quote-analytics label{display:flex!important;align-items:center;gap:7px;margin:0!important;padding:9px;border:1px solid rgba(170,196,207,.14);border-radius:8px;background:#111827}.quote-total{margin-top:12px;padding:13px;border-radius:10px;background:#111827}.quote-total strong{display:block;font-size:24px;color:var(--brand)}.quote-feedback{margin-top:10px;padding:10px;border-radius:8px;background:#111827;color:#cbd7df;font-size:11px}.quote-feedback.success{background:#203c36;color:#a8f5df}.quote-feedback.error{background:#3a2025;color:#ffb2bd}@media(max-width:1000px){.quote-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.quote-layout{grid-template-columns:1fr}.quote-editor{position:static}}@media(max-width:620px){.quote-summary,.quote-analytics{grid-template-columns:1fr}.quote-head{display:block}}.partner-sales-summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:16px}.partner-sales-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.partner-sales-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.partner-sales-stat strong{font-size:23px}.partner-sales-layout{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:16px}.partner-sales-list{display:grid;gap:10px}.partner-sales-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.partner-sales-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.partner-sales-meta{margin-top:6px;color:var(--muted);font-size:10px;line-height:1.55}.partner-sales-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:11px}.partner-sales-actions button,.partner-sales-actions a{min-height:36px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;font-size:10px;font-weight:800;cursor:pointer}.partner-sales-actions .primary{background:var(--brand-action);border-color:var(--brand-action)}.partner-sales-detail{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234;position:sticky;top:18px}.partner-sales-detail label{display:grid;gap:6px;margin:10px 0;font-size:11px;font-weight:800}.partner-sales-detail input,.partner-sales-detail select{width:100%;min-height:40px;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit}.partner-sales-feedback{margin-top:10px;padding:10px;border-radius:8px;background:#111827;color:#cbd7df;font-size:11px}.partner-sales-feedback.success{background:#203c36;color:#a8f5df}.partner-sales-feedback.error{background:#3a2025;color:#ffb2bd}.partner-pipeline{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:12px}.partner-pipeline-step{padding:10px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827;text-align:center}.partner-pipeline-step strong{display:block}.partner-pipeline-step small{color:var(--muted)}@media(max-width:1000px){.partner-sales-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.partner-sales-layout{grid-template-columns:1fr}.partner-sales-detail{position:static}}@media(max-width:650px){.partner-sales-summary,.partner-pipeline{grid-template-columns:1fr}.partner-sales-head{display:block}}.support-summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:16px}.support-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.support-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.support-stat strong{font-size:23px}.support-layout{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:16px}.support-list{display:grid;gap:10px}.support-ticket{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.support-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.support-meta{margin-top:6px;color:var(--muted);font-size:10px;line-height:1.55}.support-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:11px}.support-actions button,.support-actions a{min-height:36px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;font-size:10px;font-weight:800;cursor:pointer}.support-actions .primary{background:var(--brand-action);border-color:var(--brand-action)}.support-detail{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234;position:sticky;top:18px}.support-detail label{display:grid;gap:6px;margin:10px 0;font-size:11px;font-weight:800}.support-detail input,.support-detail select,.support-detail textarea{width:100%;min-height:40px;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit}.support-detail textarea{min-height:120px;resize:vertical}.support-feedback{margin-top:10px;padding:10px;border-radius:8px;background:#111827;color:#cbd7df;font-size:11px}.support-feedback.success{background:#203c36;color:#a8f5df}.support-feedback.error{background:#3a2025;color:#ffb2bd}.audit-mini{display:grid;gap:8px;margin-top:10px}.audit-mini-row{padding:10px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.audit-mini-row strong{display:block}.audit-mini-row small{color:var(--muted);line-height:1.45}@media(max-width:1000px){.support-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.support-layout{grid-template-columns:1fr}.support-detail{position:static}}@media(max-width:620px){.support-summary{grid-template-columns:1fr}.support-head{display:block}}.activation-summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:16px}.activation-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.activation-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.activation-stat strong{font-size:23px}.activation-layout{display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:16px}.activation-list{display:grid;gap:10px}.activation-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.activation-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.activation-meta{margin-top:6px;color:var(--muted);font-size:10px;line-height:1.55}.activation-progress{height:9px;border-radius:999px;background:#0d1522;overflow:hidden;margin-top:10px}.activation-progress span{display:block;height:100%;background:var(--brand-action)}.activation-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:11px}.activation-actions button,.activation-actions a{min-height:36px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;font-size:10px;font-weight:800;cursor:pointer}.activation-actions .primary{background:var(--brand-action);border-color:var(--brand-action)}.activation-checklist{display:grid;gap:9px}.activation-check{padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.activation-check strong{display:block;margin-bottom:3px}.activation-check small{color:var(--muted);line-height:1.45}.activation-feedback{margin-top:10px;padding:10px;border-radius:8px;background:#111827;color:#cbd7df;font-size:11px}.activation-feedback.success{background:#203c36;color:#a8f5df}.activation-feedback.error{background:#3a2025;color:#ffb2bd}@media(max-width:1050px){.activation-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.activation-layout{grid-template-columns:1fr}}@media(max-width:620px){.activation-summary{grid-template-columns:1fr}.activation-head{display:block}}.admin-customer-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:16px}.admin-customer-list{display:grid;gap:10px}.admin-customer-item{padding:14px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.admin-customer-top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.admin-customer-meta{margin-top:6px;color:var(--muted);font-size:10px;line-height:1.5}.admin-customer-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:11px}.admin-customer-actions button,.admin-customer-actions a{min-height:36px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;font-size:10px;font-weight:800;cursor:pointer}.admin-customer-actions .danger{border-color:#7b3540;background:#3a2025;color:#ffb2bd}.admin-customer-actions .success{border-color:#315d53;background:#203c36;color:#a8f5df}.admin-customer-detail{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234;position:sticky;top:18px}.admin-customer-detail label{display:grid;gap:6px;margin:10px 0;font-size:11px;font-weight:800}.admin-customer-detail input,.admin-customer-detail select,.admin-customer-detail textarea{width:100%;min-height:40px;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit}.admin-customer-detail textarea{min-height:110px;resize:vertical}.admin-customer-feedback{margin-top:10px;padding:10px;border-radius:8px;background:#111827;color:#cbd7df;font-size:11px}.admin-customer-feedback.success{background:#203c36;color:#a8f5df}.admin-customer-feedback.error{background:#3a2025;color:#ffb2bd}@media(max-width:900px){.admin-customer-grid{grid-template-columns:1fr}.admin-customer-detail{position:static}}.admin-command-summary{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:16px}.admin-command-stat{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.admin-command-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:7px}.admin-command-stat strong{font-size:23px}.admin-command-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:16px}.admin-command-stack{display:grid;gap:14px}.admin-command-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.admin-command-card h2,.admin-command-card h3{margin-top:0}.admin-command-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:11px}.admin-command-actions{display:flex;gap:8px;flex-wrap:wrap}.admin-command-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font-size:11px;font-weight:800}.admin-command-list{display:grid;gap:8px}.admin-command-row{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(130px,.7fr) auto;gap:12px;align-items:center;padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.admin-command-row strong{display:block}.admin-command-meta{color:var(--muted);font-size:10px;line-height:1.5;margin-top:3px}.admin-command-badge{display:inline-flex;padding:5px 8px;border-radius:999px;background:#223047;color:#cfe8ef;font-size:9px;font-weight:850;text-transform:uppercase}.admin-command-badge.active,.admin-command-badge.current,.admin-command-badge.completed,.admin-command-badge.resolved{background:#20433f;color:#84f0e5}.admin-command-badge.failed,.admin-command-badge.past_due,.admin-command-badge.suspended,.admin-command-badge.declined{background:#48252a;color:#ff9ca7}.admin-command-badge.pending,.admin-command-badge.open,.admin-command-badge.in_progress,.admin-command-badge.trial{background:#44391f;color:#f0cd79}.admin-privacy-note{padding:12px;border-left:3px solid var(--brand);border-radius:0 9px 9px 0;background:#111827;color:#cbd7df;font-size:11px;line-height:1.55}@media(max-width:1200px){.admin-command-summary{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:900px){.admin-command-grid{grid-template-columns:1fr}.admin-command-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.admin-command-summary{grid-template-columns:1fr}.admin-command-row{grid-template-columns:1fr}}.cloud-recording-summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:14px}.cloud-recording-layout{display:grid;grid-template-columns:minmax(300px,.8fr) minmax(0,2.2fr);gap:16px}.cloud-recording-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.cloud-recording-grid{display:grid;gap:10px}.cloud-recording-row{padding:12px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.cloud-recording-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.cloud-recording-meta{margin-top:7px;color:var(--muted);font-size:11px;line-height:1.55}.cloud-recording-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.cloud-recording-actions button{min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit;cursor:pointer}.cloud-recording-badge{padding:5px 8px;border-radius:999px;background:#223047;color:#cfe8ef;font-size:10px;font-weight:800;text-transform:uppercase}.cloud-config-list{display:grid;gap:8px}.cloud-config-row{display:flex;justify-content:space-between;gap:12px;padding:9px;border:1px solid rgba(170,196,207,.14);border-radius:8px;background:#111827;font-size:11px}@media(max-width:1000px){.cloud-recording-layout{grid-template-columns:1fr}.cloud-recording-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}.onboarding-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}.onboarding-layout{display:grid;grid-template-columns:minmax(260px,.7fr) minmax(0,2.3fr);gap:16px}.onboarding-steps{display:grid;gap:8px;align-self:start;position:sticky;top:18px}.onboarding-step{display:flex;align-items:center;justify-content:space-between;padding:10px;border:1px solid rgba(170,196,207,.16);border-radius:9px;background:#151f30}.onboarding-step.complete{border-color:rgba(63,189,141,.5)}.onboarding-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.onboarding-form{display:grid;gap:12px}.onboarding-form label{display:grid;gap:6px;color:var(--muted);font-size:11px}.onboarding-form input,.onboarding-form select,.onboarding-form textarea{width:100%;padding:10px;border:1px solid var(--line);border-radius:9px;background:#0d1522;color:white;font:inherit}.onboarding-form textarea{min-height:90px;resize:vertical}.onboarding-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.onboarding-actions button{min-height:38px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit;cursor:pointer}.onboarding-progress{height:12px;border-radius:999px;background:#0d1522;overflow:hidden}.onboarding-progress>span{display:block;height:100%;background:var(--brand-action)}.onboarding-status{display:inline-flex;padding:5px 8px;border-radius:999px;background:#223047;color:#cfe8ef;font-size:10px;font-weight:800;text-transform:uppercase}.onboarding-admin-grid{display:grid;gap:12px}.onboarding-admin-card{padding:14px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}@media(max-width:900px){.onboarding-layout{grid-template-columns:1fr}.onboarding-steps{position:static}.onboarding-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}.payment-layout{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(0,2.1fr);gap:16px}.payment-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.payment-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}.payment-form label{display:grid;gap:6px;margin:11px 0;color:var(--muted);font-size:11px}.payment-form input,.payment-form select{width:100%;padding:10px;border:1px solid var(--line);border-radius:9px;background:#0d1522;color:white;font:inherit}.payment-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.payment-actions button{min-height:38px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit;cursor:pointer}.payment-readiness{display:grid;gap:8px}.payment-readiness-row{padding:10px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.payment-warning{padding:11px;border:1px solid #d7b25f;border-radius:9px;background:rgba(215,178,95,.12);color:#ffe4a8}.payment-table{width:100%;border-collapse:collapse}.payment-table th,.payment-table td{padding:9px;border-bottom:1px solid rgba(170,196,207,.14);text-align:left;font-size:11px}@media(max-width:900px){.payment-layout{grid-template-columns:1fr}.payment-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}.subscription-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}.subscription-layout{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,.8fr);gap:16px}.subscription-stack{display:grid;gap:14px}.subscription-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.subscription-card h2,.subscription-card h3{margin-top:0}.subscription-meta{color:var(--muted);font-size:11px;line-height:1.55}.subscription-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.subscription-actions button,.subscription-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;cursor:pointer}.subscription-table{width:100%;border-collapse:collapse}.subscription-table th,.subscription-table td{padding:9px;border-bottom:1px solid rgba(170,196,207,.14);text-align:left;font-size:11px}.subscription-form label{display:grid;gap:6px;margin:11px 0;color:var(--muted);font-size:11px}.subscription-form input,.subscription-form select,.subscription-form textarea{width:100%;padding:10px;border:1px solid var(--line);border-radius:9px;background:#0d1522;color:white;font:inherit}.subscription-form textarea{min-height:90px;resize:vertical}.subscription-status{display:inline-flex;padding:5px 8px;border-radius:999px;background:#223047;color:#cfe8ef;font-size:10px;font-weight:800;text-transform:uppercase}.usage-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.usage-item{padding:10px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}@media(max-width:1000px){.subscription-layout{grid-template-columns:1fr}}@media(max-width:800px){.subscription-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}.billing-layout{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(0,2.1fr);gap:16px}.billing-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.billing-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}.billing-form label{display:grid;gap:6px;margin:11px 0;color:var(--muted);font-size:11px}.billing-form input,.billing-form select,.billing-form textarea{width:100%;padding:10px;border:1px solid var(--line);border-radius:9px;background:#0d1522;color:white;font:inherit}.billing-form textarea{min-height:90px;resize:vertical}.billing-grid{display:grid;gap:12px}.billing-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.billing-meta{margin-top:8px;color:var(--muted);font-size:11px;line-height:1.55}.billing-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.billing-actions button{min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit;cursor:pointer}.billing-badge{padding:5px 8px;border-radius:999px;background:#223047;color:#cfe8ef;font-size:10px;font-weight:800;text-transform:uppercase}.billing-table{width:100%;border-collapse:collapse}.billing-table th,.billing-table td{padding:9px;border-bottom:1px solid rgba(170,196,207,.14);text-align:left;font-size:11px}@media(max-width:900px){.billing-layout{grid-template-columns:1fr}.billing-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}.license-warning-banner{margin:0 0 14px;padding:11px 14px;border:1px solid #d7b25f;border-radius:10px;background:rgba(215,178,95,.14);color:#ffe7ad;font-size:12px;line-height:1.5}.license-warning-banner a{color:#fff;text-decoration:underline;font-weight:800}.license-enforcement-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}.license-warning-list{display:grid;gap:8px}.license-warning-row{padding:10px;border-left:3px solid #d7b25f;border-radius:0 8px 8px 0;background:#111827}.license-feature-table{width:100%;border-collapse:collapse}.license-feature-table th,.license-feature-table td{padding:9px;border-bottom:1px solid rgba(170,196,207,.14);text-align:left;font-size:11px}@media(max-width:900px){.license-enforcement-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}.license-layout{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(0,2.1fr);gap:16px}.license-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.license-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}.license-form label{display:grid;gap:6px;margin:11px 0;color:var(--muted);font-size:11px}.license-form input,.license-form select,.license-form textarea{width:100%;padding:10px;border:1px solid var(--line);border-radius:9px;background:#0d1522;color:white;font:inherit}.license-form textarea{min-height:90px;resize:vertical}.license-feature-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.license-feature{padding:9px;border:1px solid rgba(170,196,207,.14);border-radius:8px;background:#111827}.license-history{display:grid;gap:8px}.license-history-row{padding:10px;border-left:3px solid var(--brand);background:#111827;border-radius:0 8px 8px 0}.license-status{display:inline-flex;padding:6px 9px;border-radius:999px;background:#223047;font-size:11px;font-weight:800;text-transform:uppercase}.license-status.valid{background:rgba(63,189,141,.18);color:#8ff0c5}.license-status.invalid{background:rgba(255,100,119,.18);color:#ffb6c1}@media(max-width:900px){.license-layout{grid-template-columns:1fr}.license-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}.backup-layout{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(0,2.1fr);gap:16px}.backup-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.backup-grid{display:grid;gap:12px}.backup-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.backup-meta{margin-top:8px;color:var(--muted);font-size:11px;line-height:1.55}.backup-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.backup-actions button,.backup-actions a{min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;cursor:pointer;display:inline-flex;align-items:center}.backup-status{padding:5px 8px;border-radius:999px;background:#223047;color:#cfe8ef;font-size:10px;font-weight:800;text-transform:uppercase}.restore-warning{padding:11px;border:1px solid #d7b25f;border-radius:9px;background:rgba(215,178,95,.12);color:#ffe4a8}@media(max-width:900px){.backup-layout{grid-template-columns:1fr}}.release-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:14px}.release-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.release-card h3{margin-top:0}.release-status{font-size:22px;font-weight:900}.release-status.ready{color:#78e0b4}.release-status.warning{color:#ffd080}.release-status.blocked{color:#ff91a1}.release-list{display:grid;gap:8px}.release-row{padding:10px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.release-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.release-actions button,.release-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:38px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;cursor:pointer}@media(max-width:900px){.release-grid{grid-template-columns:1fr}}.mobile-device-layout{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(0,2.1fr);gap:16px}.mobile-device-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.pairing-code{display:grid;place-items:center;min-height:120px;font-size:42px;font-weight:900;letter-spacing:.18em;border:1px dashed var(--line);border-radius:12px;background:#0d1522}.mobile-device-grid{display:grid;gap:12px}.mobile-device-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.mobile-device-meta{margin-top:8px;color:var(--muted);font-size:11px;line-height:1.55}.mobile-device-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.mobile-device-actions button{min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit;cursor:pointer}.mobile-badge{padding:5px 8px;border-radius:999px;background:#223047;color:#cfe8ef;font-size:10px;font-weight:800;text-transform:uppercase}@media(max-width:900px){.mobile-device-layout{grid-template-columns:1fr}}.notification-layout{display:grid;grid-template-columns:minmax(300px,.9fr) minmax(0,2.1fr);gap:16px}.notification-rule-form{align-self:start;position:sticky;top:18px;padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:14px;background:#151f30}.notification-rule-form label{display:grid;gap:6px;margin:11px 0;color:var(--muted);font-size:11px}.notification-rule-form input,.notification-rule-form select{width:100%;height:40px;padding:0 10px;border:1px solid var(--line);border-radius:9px;background:#0d1522;color:white;font:inherit}.notification-rule-form select[multiple]{height:120px;padding:8px}.notification-grid{display:grid;gap:12px}.notification-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.notification-card-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.notification-card h3{margin:0}.notification-badges{display:flex;gap:7px;flex-wrap:wrap}.notification-badge{padding:5px 8px;border-radius:999px;background:#223047;color:#cfe8ef;font-size:10px;font-weight:800;text-transform:uppercase}.notification-meta{margin-top:8px;color:var(--muted);font-size:11px;line-height:1.55}.notification-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.notification-actions button{min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit;cursor:pointer}.delivery-table{width:100%;border-collapse:collapse}.delivery-table th,.delivery-table td{padding:9px;border-bottom:1px solid rgba(170,196,207,.14);text-align:left;font-size:11px}.channel-status{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin-bottom:14px}.channel-status .stat{padding:10px}@media(max-width:900px){.notification-layout{grid-template-columns:1fr}.notification-rule-form{position:static}.channel-status{grid-template-columns:repeat(2,minmax(0,1fr))}}.report-layout{display:grid;grid-template-columns:minmax(280px,.8fr) minmax(0,2.2fr);gap:16px}.report-list{display:grid;gap:10px}.report-card{padding:14px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:#182234}.report-editor{display:grid;gap:12px}.report-editor label{display:grid;gap:6px;color:var(--muted);font-size:11px}.report-editor input,.report-editor textarea{width:100%;padding:10px;border:1px solid var(--line);border-radius:9px;background:#0d1522;color:white;font:inherit}.report-editor textarea{min-height:120px;resize:vertical}.report-status{display:inline-flex;padding:5px 8px;border-radius:999px;background:#223047;font-size:10px;font-weight:800;text-transform:uppercase}.report-warning{padding:10px;border:1px solid #d7b25f;border-radius:9px;background:rgba(215,178,95,.12);color:#ffe4a8}.report-timeline{display:grid;gap:8px}.report-timeline-row{padding:10px;border-left:3px solid var(--brand);background:#111827;border-radius:0 8px 8px 0}@media(max-width:900px){.report-layout{grid-template-columns:1fr}}.custody-grid{display:grid;grid-template-columns:minmax(280px,.8fr) minmax(0,2.2fr);gap:16px}.custody-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.custody-status{display:inline-flex;align-items:center;gap:7px;padding:6px 9px;border-radius:999px;font-size:11px;font-weight:800;text-transform:uppercase}.custody-status.verified{background:rgba(63,189,141,.18);color:#8ff0c5}.custody-status.modified{background:rgba(255,100,119,.18);color:#ffb6c1}.custody-status.missing{background:rgba(255,190,80,.18);color:#ffd69a}.custody-ledger{display:grid;gap:8px}.custody-entry{padding:10px;border-left:3px solid var(--brand);background:#111827;border-radius:0 8px 8px 0}.custody-meta{color:var(--muted);font-size:11px;line-height:1.5}.custody-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.custody-actions button,.custody-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;cursor:pointer}@media(max-width:900px){.custody-grid{grid-template-columns:1fr}}.case-layout{display:grid;grid-template-columns:minmax(280px,.8fr) minmax(0,2.2fr);gap:16px}.case-form{align-self:start;position:sticky;top:18px;padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:14px;background:#151f30}.case-form label{display:grid;gap:6px;margin:11px 0;color:var(--muted);font-size:11px}.case-form input,.case-form select,.case-form textarea{width:100%;padding:10px;border:1px solid var(--line);border-radius:9px;background:#0d1522;color:white;font:inherit}.case-form textarea{min-height:100px;resize:vertical}.case-grid{display:grid;gap:12px}.case-card{padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.case-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.case-head h3{margin:0}.case-badges{display:flex;gap:7px;flex-wrap:wrap}.case-badge{padding:5px 8px;border-radius:999px;background:#223047;color:#cfe8ef;font-size:10px;font-weight:800;text-transform:uppercase}.case-meta{margin-top:8px;color:var(--muted);font-size:11px;line-height:1.5}.case-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.case-actions button,.case-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;cursor:pointer}.case-detail{display:grid;gap:14px}.case-events{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.case-event{padding:11px;border:1px solid rgba(170,196,207,.16);border-radius:10px;background:#111827}.case-history{display:grid;gap:8px}.case-history-row{padding:10px;border-left:3px solid var(--brand);background:#111827;border-radius:0 8px 8px 0}@media(max-width:900px){.case-layout{grid-template-columns:1fr}.case-form{position:static}}@media(max-width:700px){.case-events{grid-template-columns:1fr}}.investigation-shell{display:grid;grid-template-columns:minmax(260px,.8fr) minmax(0,2.2fr);gap:16px}.investigation-filters{align-self:start;position:sticky;top:18px;padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:14px;background:#151f30}.investigation-filters label{display:grid;gap:6px;margin:11px 0;color:var(--muted);font-size:11px}.investigation-filters input,.investigation-filters select{width:100%;height:40px;padding:0 10px;border:1px solid var(--line);border-radius:9px;background:#0d1522;color:white;font:inherit}.investigation-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.investigation-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}.investigation-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:13px}.investigation-card{overflow:hidden;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#182234}.investigation-thumb{position:relative;aspect-ratio:16/9;background:#080d15;display:grid;place-items:center;overflow:hidden}.investigation-thumb img{width:100%;height:100%;object-fit:cover}.investigation-placeholder{color:var(--muted);text-align:center}.investigation-badge{position:absolute;top:9px;left:9px;padding:5px 8px;border-radius:999px;background:rgba(8,13,21,.82);font-size:10px;font-weight:800;text-transform:capitalize}.investigation-body{padding:13px}.investigation-title{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.investigation-title h3{margin:0;font-size:15px;text-transform:capitalize}.investigation-meta{margin-top:5px;color:var(--muted);font-size:11px;line-height:1.5}.investigation-card-actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin-top:12px}.investigation-card-actions button,.investigation-card-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:34px;padding:7px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;text-decoration:none;font:inherit;font-size:10px;cursor:pointer}.investigation-card-actions .primary{background:var(--brand-action);border-color:var(--brand-action)}.investigation-empty{padding:38px;border:1px dashed var(--line);border-radius:12px;text-align:center;color:var(--muted)}.evidence-panel{margin-top:15px;padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:#151f30}@media(max-width:1150px){.investigation-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:900px){.investigation-shell{grid-template-columns:1fr}.investigation-filters{position:static}.investigation-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:650px){.investigation-grid,.investigation-summary{grid-template-columns:1fr}}.phone-connect-grid{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(280px,.8fr);gap:16px}.phone-connect-card{padding:18px;border:1px solid rgba(170,196,207,.18);border-radius:14px;background:#182234}.phone-url{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px;border:1px solid var(--line);border-radius:10px;background:#0d1420;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all}.phone-checklist{display:grid;gap:9px;margin-top:12px}.phone-check{display:flex;gap:9px;align-items:flex-start;padding:10px;border:1px solid rgba(170,196,207,.14);border-radius:9px;background:#111827}.phone-status{display:grid;gap:8px}.phone-status-row{display:flex;justify-content:space-between;gap:12px;padding:10px;border-bottom:1px solid rgba(170,196,207,.12)}@media(max-width:900px){.phone-connect-grid{grid-template-columns:1fr}}.timeline-tooltip{width:300px!important;pointer-events:none!important}.timeline-tooltip video{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:8px;background:#000;margin-bottom:8px}.timeline-tooltip .hover-preview-fallback{display:grid;place-items:center;width:100%;aspect-ratio:16/9;border-radius:8px;background:#080d15;color:var(--muted);margin-bottom:8px}.timeline-tooltip .hover-preview-title{font-weight:800;text-transform:capitalize;margin-bottom:3px}.timeline-tooltip .hover-preview-meta{color:var(--muted);font-size:11px;line-height:1.45}.monitor-work{height:auto!important;max-height:none!important;overflow:visible!important}.monitor-grid{height:auto!important;min-height:0!important;overflow:visible!important;grid-auto-rows:minmax(220px,auto)!important}.monitor-grid[data-layout="1"]{grid-template-columns:minmax(0,1fr)!important}.monitor-grid[data-layout="4"]{grid-template-columns:repeat(2,minmax(420px,1fr))!important}.monitor-grid[data-layout="9"]{grid-template-columns:repeat(3,minmax(280px,1fr))!important}.monitor-grid[data-layout="16"]{grid-template-columns:repeat(4,minmax(220px,1fr))!important}.monitor-tile{min-height:220px!important}.monitor-video{height:auto!important;min-height:220px!important;aspect-ratio:16/9!important}.monitor-timeline{height:auto!important;min-height:285px!important;max-height:none!important;overflow:auto!important;position:relative!important}@media(max-width:1250px){.monitor-grid[data-layout="4"]{grid-template-columns:repeat(2,minmax(320px,1fr))!important}.monitor-grid[data-layout="9"],.monitor-grid[data-layout="16"]{grid-template-columns:repeat(2,minmax(300px,1fr))!important}}@media(max-width:760px){.monitor-grid[data-layout]{grid-template-columns:1fr!important}.monitor-tile,.monitor-video{min-height:190px!important}}.monitor-timeline{display:block!important;visibility:visible!important;opacity:1!important;min-height:300px!important;height:auto!important;overflow:auto!important}#timeline-hours{display:grid!important;grid-template-columns:repeat(13,1fr)!important;min-height:20px!important;color:#9aa7b7!important}#multi-camera-timeline{display:block!important;min-height:150px!important}.timeline-row{display:grid!important;grid-template-columns:120px minmax(0,1fr)!important;gap:12px!important;align-items:center!important;min-height:34px!important}.timeline-lane{display:block!important;position:relative!important;height:28px!important;background:repeating-linear-gradient(90deg,rgba(255,255,255,.13) 0 1px,transparent 1px calc(100% / 24))!important;border-radius:7px!important}.monitor-js-error{margin:10px 0;padding:10px;border:1px solid #ff6477;border-radius:9px;background:rgba(255,100,119,.12);color:#ffd9de}.monitor-work{display:flex!important;flex-direction:column!important;height:calc(100vh - 105px)!important;max-height:calc(100vh - 105px)!important;overflow:hidden!important}.monitor-toolbar{flex:0 0 auto}.monitor-grid{flex:1 1 auto!important;min-height:220px!important;max-height:none!important;overflow:hidden!important}.monitor-timeline{display:block!important;flex:0 0 285px!important;height:285px!important;min-height:285px!important;max-height:285px!important;overflow:auto!important;margin-top:12px!important;position:relative!important;background:#111827!important;border:1px solid rgba(170,196,207,.25)!important;z-index:20!important}#multi-camera-timeline{display:block!important;min-height:80px!important}.timeline-hours{display:grid!important;min-height:18px!important}@media(max-width:1050px){.monitor-work{height:auto!important;max-height:none!important;overflow:visible!important}.monitor-grid{min-height:0!important}.monitor-timeline{height:auto!important;min-height:285px!important;max-height:none!important;flex:auto!important;overflow:auto!important}}.monitor-work{display:grid;grid-template-rows:auto minmax(280px,1fr) auto auto;min-height:calc(100vh - 110px);max-height:calc(100vh - 110px);overflow:hidden}.monitor-grid{min-height:0;height:100%;overflow:hidden}.monitor-tile{min-height:0}.monitor-video{height:100%;min-height:0;aspect-ratio:auto}.monitor-timeline{margin-top:12px;max-height:310px;overflow:auto;position:relative;z-index:4;box-shadow:0 -10px 30px rgba(7,12,20,.22)}#create-clip{max-height:210px;overflow:auto}@media(max-width:1050px){.monitor-work{max-height:none;min-height:auto;grid-template-rows:auto auto auto auto;overflow:visible}.monitor-grid{height:auto}.monitor-video{aspect-ratio:16/9;height:auto}.monitor-timeline{max-height:none;position:static}}.event-segment.selected{outline:2px solid #fff;outline-offset:2px;z-index:7;box-shadow:0 0 0 4px rgba(255,255,255,.14)}.selected-event-summary{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:10px 0;padding:11px 13px;border:1px solid rgba(94,210,209,.35);border-radius:10px;background:rgba(24,52,67,.48);color:#e8fbfa}.selected-event-summary[hidden]{display:none}.selected-event-summary small{color:#a9c6ca}.timeline-empty{padding:24px;border:1px dashed var(--line);border-radius:10px;color:var(--muted);text-align:center}.monitor-recording-mode{outline:2px solid #ffb45c!important}.monitor-recording-mode .monitor-video-label::after{content:" · RECORDED";color:#ffcf91}.monitor-shell{display:grid;grid-template-columns:250px minmax(0,1fr);gap:16px}.monitor-picker{padding:14px;border:1px solid rgba(170,196,207,.18);border-radius:14px;background:#121b2a;align-self:start;position:sticky;top:18px}.monitor-search{width:100%;height:40px;padding:0 12px;border:1px solid var(--line);border-radius:999px;background:#0d1522;color:white;font:inherit;margin-bottom:12px}.monitor-camera-list{display:grid;gap:6px}.monitor-camera-item{display:flex;align-items:center;gap:9px;padding:10px;border:1px solid transparent;border-radius:9px;color:white;background:transparent;cursor:pointer;text-align:left;font:inherit}.monitor-camera-item:hover,.monitor-camera-item.active{background:#24344a;border-color:rgba(94,210,209,.45)}.monitor-camera-dot{width:9px;height:9px;border-radius:50%;background:#ff6477;flex:0 0 auto}.monitor-camera-dot.online{background:#4bd58b}.monitor-work{min-width:0}.monitor-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}.monitor-toolbar-group{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.monitor-toolbar button,.monitor-toolbar select,.monitor-toolbar input{height:38px;padding:0 11px;border:1px solid var(--line);border-radius:9px;background:#111827;color:white;font:inherit}.monitor-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.monitor-grid[data-layout="1"]{grid-template-columns:1fr}.monitor-grid[data-layout="4"]{grid-template-columns:repeat(2,minmax(0,1fr))}.monitor-grid[data-layout="9"]{grid-template-columns:repeat(3,minmax(0,1fr))}.monitor-grid[data-layout="16"]{grid-template-columns:repeat(4,minmax(0,1fr))}.monitor-tile{overflow:hidden;border:1px solid rgba(170,196,207,.18);border-radius:10px;background:#101827;cursor:pointer}.monitor-tile.active{outline:2px solid var(--brand);outline-offset:1px}.monitor-video{position:relative;aspect-ratio:16/9;background:#070b12;display:grid;place-items:center}.monitor-video video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#070b12}.monitor-video-label{position:absolute;left:9px;bottom:8px;z-index:2;padding:5px 8px;border-radius:7px;background:rgba(5,10,18,.8);font-size:11px;font-weight:750}.monitor-timeline{margin-top:14px;padding:15px;border:1px solid rgba(170,196,207,.18);border-radius:14px;background:#111827}.timeline-hours{display:grid;grid-template-columns:repeat(13,1fr);padding-left:132px;color:#8f9baa;font-size:10px;margin-bottom:7px}.timeline-hours span:last-child{text-align:right}.timeline-row{display:grid;grid-template-columns:120px minmax(0,1fr);gap:12px;align-items:center;margin:8px 0}.timeline-camera-name{font-size:12px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.timeline-lane{position:relative;height:30px;border-radius:8px;background:repeating-linear-gradient(90deg,rgba(255,255,255,.12) 0 1px,transparent 1px calc(100% / 24));overflow:visible}.recording-segment,.event-segment{position:absolute;border-radius:6px;min-width:3px;cursor:pointer}.recording-segment{top:19px;height:6px;background:#e8eef6;opacity:.62}.event-segment{top:5px;height:10px}.event-motion{background:#8a63ff}.event-person{background:#ffe66d}.event-vehicle,.event-car,.event-truck,.event-bus,.event-motorcycle{background:#ff8a34}.event-plate,.event-lpr{background:#4d9dff}.event-people_counting,.event-occupancy{background:#56d98c}.event-intrusion,.event-line_crossing{background:#ff5f70}.event-bookmark{background:#ffd43b}.timeline-cursor{position:absolute;top:-5px;bottom:-5px;width:2px;background:#ff4966;z-index:5;pointer-events:none}.timeline-tooltip{position:fixed;z-index:80;width:240px;padding:10px;border:1px solid var(--line);border-radius:10px;background:#101827;color:white;box-shadow:0 18px 45px rgba(0,0,0,.45);pointer-events:none;display:none}.timeline-tooltip img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:7px;margin-bottom:8px}.timeline-tooltip small{color:var(--muted)}.event-legend{display:flex;flex-wrap:wrap;gap:11px;margin-top:12px;color:#cbd4df;font-size:11px}.event-legend span{display:flex;align-items:center;gap:5px}.legend-dot{width:9px;height:9px;border-radius:50%}.monitor-filters{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.monitor-filter.active{background:var(--brand-action);border-color:var(--brand-action)}@media(max-width:1050px){.monitor-shell{grid-template-columns:1fr}.monitor-picker{position:static}.monitor-camera-list{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:760px){.monitor-grid[data-layout]{grid-template-columns:1fr}.timeline-row{grid-template-columns:84px minmax(0,1fr);gap:7px}.timeline-hours{padding-left:91px}.monitor-camera-list{grid-template-columns:1fr}}.camera-health-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:18px}.camera-health-stat{padding:17px;border:1px solid rgba(170,196,207,.18);border-radius:14px;background:rgba(24,33,50,.94)}.camera-health-stat span{display:block;color:var(--muted);font-size:11px;margin-bottom:7px}.camera-health-stat strong{font-size:23px}.camera-health-table-wrap{overflow:auto;border:1px solid rgba(170,196,207,.18);border-radius:14px;background:rgba(24,33,50,.94)}.camera-health-table{width:100%;min-width:980px;border-collapse:collapse}.camera-health-table th,.camera-health-table td{padding:13px 12px;border-bottom:1px solid rgba(170,196,207,.14);text-align:left;vertical-align:middle}.camera-health-table th{background:#151d2b;color:var(--muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase}.camera-health-table tr:last-child td{border-bottom:0}.health-state{display:inline-flex;align-items:center;gap:7px;padding:5px 8px;border-radius:999px;font-size:10px;font-weight:850;text-transform:uppercase}.health-state::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}.health-state.online{background:#20433f;color:#84f0e5}.health-state.offline{background:#48252a;color:#ff9ca7}.health-state.warning{background:#44391f;color:#f0cd79}.health-code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;color:#cbd5df}.health-issues-list{display:grid;gap:9px;margin-top:15px}.health-issue-card{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px;border:1px solid rgba(170,196,207,.16);border-radius:11px;background:rgba(13,20,32,.46)}.health-issue-card strong{display:block;margin-bottom:3px}.health-refresh-note{color:var(--muted);font-size:11px}@media(max-width:900px){.camera-health-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:560px){.camera-health-summary{grid-template-columns:1fr}}.smart-search-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:18px}.smart-search-card{overflow:hidden;border:1px solid rgba(170,196,207,.18);border-radius:14px;background:rgba(24,33,50,.94);box-shadow:0 7px 20px rgba(7,12,20,.12)}.smart-search-media{position:relative;display:block;width:100%;aspect-ratio:16/9;padding:0;border:0;background:#080d15;color:white;cursor:pointer;overflow:hidden}.smart-search-media img{width:100%;height:100%;object-fit:cover}.smart-search-placeholder{height:100%;display:grid;place-content:center;gap:7px;text-align:center;color:var(--muted)}.smart-search-placeholder strong{color:white;font-size:17px}.smart-search-placeholder span{font-size:34px;color:var(--brand)}.smart-search-play{position:absolute;left:12px;bottom:12px;padding:7px 10px;border-radius:999px;background:rgba(5,10,18,.84);font-size:11px;font-weight:800}.smart-search-body{padding:14px}.smart-search-heading{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.smart-search-heading h3{margin:0 0 4px;font-size:16px}.smart-search-confidence{padding:5px 8px;border-radius:999px;background:#315c5d;color:#9ff7f1;font-size:10px;font-weight:800;white-space:nowrap}.smart-search-meta{margin-top:6px;color:var(--muted);font-size:11px;line-height:1.5}.smart-search-detail{margin-top:10px;font-size:12px}.smart-search-actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin-top:14px}.smart-search-action{display:inline-flex;align-items:center;justify-content:center;min-height:34px;padding:7px;border:1px solid rgba(170,196,207,.25);border-radius:8px;background:transparent;color:white;text-decoration:none;font:inherit;font-size:10px;font-weight:750;cursor:pointer}.smart-search-action:hover,.smart-search-action:focus-visible{border-color:var(--brand);background:rgba(67,209,204,.1);outline:none}.smart-search-action[disabled]{opacity:.45;cursor:not-allowed}.smart-preview-dialog{width:min(900px,calc(100% - 24px));padding:0;border:1px solid var(--line);border-radius:15px;background:#141d2c;color:white;box-shadow:0 30px 90px rgba(0,0,0,.62)}.smart-preview-dialog::backdrop{background:rgba(0,0,0,.8)}.smart-preview-body{padding:20px}.smart-preview-video{display:block;width:100%;max-height:65vh;border-radius:10px;background:black}.smart-preview-empty{padding:70px 20px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:10px}.smart-preview-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}@media(max-width:1100px){.smart-search-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.smart-search-grid{grid-template-columns:1fr}}.today-activity-card{display:grid;grid-template-columns:minmax(220px,1.2fr) minmax(0,2.8fr);gap:18px;align-items:center;margin-bottom:20px;padding:20px;border:1px solid rgba(170,196,207,.18);border-radius:16px;background:linear-gradient(135deg,rgba(25,34,51,.98),rgba(40,50,68,.92));box-shadow:0 10px 28px rgba(7,12,20,.16)}.today-activity-intro h2{margin:4px 0 7px;font-size:22px}.today-activity-intro p{margin:0;color:var(--muted);font-size:13px;line-height:1.45}.today-activity-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.today-activity-link{display:grid;gap:4px;min-height:92px;padding:14px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:rgba(12,19,31,.48);color:white;text-decoration:none}.today-activity-link:hover,.today-activity-link:focus-visible{border-color:var(--brand);background:rgba(24,40,54,.72);outline:none}.today-activity-link strong{font-size:25px;line-height:1}.today-activity-link span{font-size:12px;font-weight:750}.today-activity-link small{color:var(--muted);font-size:10px}@media(max-width:1100px){.today-activity-card{grid-template-columns:1fr}.today-activity-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:700px){.today-activity-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}.live-page-header{margin-bottom:18px}.live-camera-head{margin-bottom:14px}.account-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.account-grid .panel{min-width:0}@media(max-width:760px){.mobile-nav{grid-template-columns:repeat(5,minmax(0,1fr))}.mobile-nav a{padding:9px 2px;font-size:11px}.account-grid{grid-template-columns:1fr}}
.clip-form{display:grid;grid-template-columns:160px repeat(2,minmax(210px,1fr)) auto;gap:12px;align-items:end}.clip-form label{display:grid;gap:7px;color:var(--muted);font-size:12px}.clip-form select,.clip-form input{height:42px;padding:0 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:var(--text);font:inherit;color-scheme:dark}.clip-job{margin-top:16px}.clip-job[hidden]{display:none}@media(max-width:900px){.clip-form{grid-template-columns:1fr 1fr}.clip-form .action-button{height:42px}}@media(max-width:600px){.clip-form{grid-template-columns:1fr}}
.portal-tabs{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:4px;margin-bottom:24px;padding:7px;border-radius:9px;background:#182234;overflow-x:auto}.portal-tab{padding:13px;border:0;border-radius:7px;background:transparent;color:#f4f7fb;font:inherit;font-weight:700;white-space:nowrap;cursor:pointer}.portal-tab.active{background:var(--brand);color:#15343d}.portal-workspace{min-height:620px;padding:30px;border-radius:12px;background:rgba(25,34,51,.82)}.portal-panel[hidden]{display:none}.portal-actions{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:20px}.portal-search-row{display:grid;grid-template-columns:minmax(240px,1fr) auto;gap:20px;align-items:center}.portal-search{width:100%;height:46px;padding:0 16px;border:1px solid #d2dae1;border-radius:999px;background:transparent;color:white;font:inherit}.status-filters{display:flex;gap:17px;font-weight:700}.customer-list{display:grid;gap:10px;margin-top:24px}.customer-row{display:grid;grid-template-columns:1.5fr 1.5fr .8fr .8fr;gap:18px;align-items:center;padding:17px;border:1px solid rgba(255,255,255,.1);border-radius:9px;background:#1b2638}.customer-row small{color:var(--muted)}dialog.partner-dialog{width:min(520px,calc(100% - 28px));padding:0;border:1px solid var(--line);border-radius:14px;background:#192234;color:white;box-shadow:0 30px 80px rgba(0,0,0,.55)}dialog.partner-dialog::backdrop{background:rgba(0,0,0,.7)}.dialog-body{padding:24px}.dialog-form{display:grid;gap:14px}.dialog-form label{display:grid;gap:7px;color:var(--muted)}.dialog-form input,.dialog-form select{height:43px;padding:0 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit}.dialog-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:8px}@media(max-width:800px){.portal-workspace{padding:18px}.portal-search-row,.customer-row{grid-template-columns:1fr}.portal-actions{align-items:flex-start;flex-direction:column}.portal-tabs{grid-template-columns:repeat(6,150px)}}
.rule-layout{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(300px,.7fr);gap:18px}.rule-stage{position:relative;aspect-ratio:16/9;overflow:hidden;border:1px solid var(--line);border-radius:10px;background:#080a0e}.rule-stage video{width:100%;height:100%;object-fit:contain}.rule-stage canvas{position:absolute;inset:0;width:100%;height:100%;cursor:crosshair}.rule-form{display:grid;gap:13px}.rule-form label{display:grid;gap:6px;color:var(--muted);font-size:12px}.rule-form input,.rule-form select{height:40px;padding:0 9px;border:1px solid var(--line);border-radius:7px;background:#111827;color:white}.chart{height:220px;display:flex;align-items:flex-end;gap:10px;padding:20px;border-left:1px solid #8190a0;border-bottom:1px solid #8190a0}.chart-bar{flex:1;min-width:24px;border-radius:5px 5px 0 0;background:linear-gradient(180deg,var(--brand),#4b4de2);position:relative}.chart-bar span{position:absolute;bottom:-22px;left:50%;transform:translateX(-50%);font-size:10px;color:var(--muted)}.mock-banner{padding:11px 14px;margin-bottom:16px;border:1px solid #d0a84b;border-radius:8px;background:#3a3020;color:#ffe1a0;font-size:13px}@media(max-width:950px){.rule-layout{grid-template-columns:1fr}}
"""

STYLES += """
.dashboard-camera-section{margin-bottom:24px}.dashboard-camera-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.dashboard-camera-card{display:block;overflow:hidden;border:1px solid rgba(170,196,207,.2);border-radius:15px;background:rgba(24,33,50,.96);color:var(--text);text-decoration:none;box-shadow:0 10px 26px rgba(7,12,20,.18);transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}.dashboard-camera-card:hover{transform:translateY(-2px);border-color:rgba(67,209,204,.7);box-shadow:0 14px 34px rgba(7,12,20,.3)}.dashboard-camera-card.offline{border-color:rgba(255,107,107,.3)}.dashboard-camera-preview{position:relative;aspect-ratio:16/9;overflow:hidden;background:#080a0e}.dashboard-camera-preview video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .25s ease}.dashboard-camera-preview video.ready{opacity:1}.dashboard-camera-placeholder{position:absolute;inset:0;display:grid;place-content:center;gap:6px;padding:20px;text-align:center;color:var(--muted)}.dashboard-camera-placeholder[hidden]{display:none}.dashboard-camera-placeholder .signal{font-size:25px}.dashboard-camera-placeholder strong{color:#dce4ec}.dashboard-live-badge,.dashboard-rec-badge{position:absolute;z-index:2;top:12px;padding:6px 9px;border-radius:999px;background:rgba(8,10,14,.82);font-size:10px;font-weight:850;letter-spacing:.1em}.dashboard-live-badge{left:12px;color:#7ee8c7}.dashboard-live-badge::before{content:"";display:inline-block;width:7px;height:7px;margin-right:6px;border-radius:50%;background:var(--accent)}.dashboard-live-badge.wait{color:#ffd48a}.dashboard-live-badge.wait::before{background:#f0b84b}.dashboard-rec-badge{right:12px;color:#ff8c8c}.dashboard-rec-badge.inactive{color:var(--muted)}.dashboard-camera-info{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:15px 16px}.dashboard-camera-name{font-size:15px;font-weight:800}.dashboard-camera-detail{margin-top:4px;color:var(--muted);font-size:12px}.dashboard-open-icon{font-size:20px;color:var(--brand-soft)}@media(max-width:900px){.dashboard-camera-grid{grid-template-columns:1fr}}@media(max-width:760px){.dashboard-camera-grid{gap:12px}.dashboard-camera-info{padding:13px 14px}}
"""

STYLES += """
.dashboard-events-section{margin-bottom:24px}.dashboard-event-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.dashboard-event-card{overflow:hidden;border:1px solid rgba(170,196,207,.2);border-radius:14px;background:rgba(24,33,50,.96);color:var(--text);text-decoration:none;box-shadow:0 9px 24px rgba(7,12,20,.16);transition:transform .18s ease,border-color .18s ease}.dashboard-event-card:hover{transform:translateY(-2px);border-color:rgba(67,209,204,.65)}.dashboard-event-image{position:relative;aspect-ratio:16/9;overflow:hidden;background:#090d14}.dashboard-event-image img{width:100%;height:100%;object-fit:cover;display:block}.dashboard-event-fallback{position:absolute;inset:0;display:grid;place-content:center;text-align:center;color:var(--muted);background:linear-gradient(145deg,#111827,#1d293a)}.dashboard-event-fallback strong{color:#dce4ec}.dashboard-event-type{position:absolute;left:10px;top:10px;padding:6px 9px;border-radius:999px;background:rgba(8,10,14,.82);color:#9ff7f1;font-size:10px;font-weight:850;letter-spacing:.08em;text-transform:uppercase}.dashboard-event-camera{position:absolute;right:10px;top:10px;padding:6px 9px;border-radius:999px;background:rgba(8,10,14,.82);color:#fff;font-size:10px;font-weight:800}.dashboard-event-body{padding:13px 14px}.dashboard-event-title{font-weight:800}.dashboard-event-meta{display:flex;justify-content:space-between;gap:10px;margin-top:6px;color:var(--muted);font-size:12px}.dashboard-event-actions{display:flex;gap:8px;margin-top:12px}.dashboard-event-action{flex:1;padding:8px 10px;border:1px solid rgba(255,255,255,.15);border-radius:8px;color:#d9f9f6;text-align:center;font-size:12px;font-weight:750}.dashboard-event-action.primary{background:var(--brand-action);border-color:var(--brand-action);color:white}.dashboard-event-empty{grid-column:1/-1}.dashboard-event-refresh{color:var(--muted);font-size:12px}@media(max-width:1100px){.dashboard-event-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:760px){.dashboard-event-grid{grid-template-columns:1fr}}
"""



STYLES += """
.dashboard-intelligence{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(320px,.75fr);gap:16px;margin-bottom:24px}.ai-summary-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.ai-summary-card{padding:15px;border:1px solid rgba(170,196,207,.16);border-radius:12px;background:rgba(10,15,25,.28)}.ai-summary-label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.ai-summary-value{display:block;margin-top:7px;font-size:25px;font-weight:850}.ai-summary-detail{display:block;margin-top:5px;color:var(--muted);font-size:11px}.alert-stack{display:grid;gap:9px}.alert-card{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:11px;align-items:start;padding:12px;border:1px solid rgba(170,196,207,.15);border-radius:11px;background:rgba(10,15,25,.28);color:var(--text);text-decoration:none}.alert-card:hover{border-color:rgba(67,209,204,.55)}.alert-icon{display:grid;place-items:center;width:31px;height:31px;border-radius:9px;background:#2d2a1f;color:#ffd27d;font-weight:900}.alert-card.critical .alert-icon{background:#3a2024;color:#ff9a9a}.alert-message{font-weight:720;font-size:13px;line-height:1.35}.alert-meta{margin-top:4px;color:var(--muted);font-size:11px}.alert-severity{padding:4px 7px;border-radius:999px;background:#302a1d;color:#f0ca72;font-size:10px;font-weight:800;text-transform:uppercase}.alert-card.critical .alert-severity{background:#3c2025;color:#ffaaaa}.alert-empty{padding:20px;border:1px dashed rgba(170,196,207,.2);border-radius:11px;color:var(--muted);text-align:center}.dashboard-alert-count{display:inline-grid;place-items:center;min-width:23px;height:23px;padding:0 7px;border-radius:999px;background:#3c2025;color:#ffb2b2;font-size:11px;font-weight:850}.activity-bars{display:flex;align-items:end;gap:6px;height:86px;margin-top:14px;padding-top:8px;border-bottom:1px solid rgba(170,196,207,.22)}.activity-bar{flex:1;min-width:10px;border-radius:4px 4px 0 0;background:linear-gradient(180deg,var(--brand),var(--brand-action));position:relative}.activity-bar span{position:absolute;bottom:-19px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--muted)}.intelligence-note{margin-top:23px;color:var(--muted);font-size:11px}@media(max-width:1050px){.dashboard-intelligence{grid-template-columns:1fr}.ai-summary-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:700px){.ai-summary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.alert-card{grid-template-columns:auto minmax(0,1fr)}.alert-severity{grid-column:2;justify-self:start}}
"""


STYLES += """
.analytics-dashboard-grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(300px,.6fr);gap:16px;margin-bottom:18px}.analytics-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:18px}.analytics-kpi{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:rgba(24,33,50,.94)}.analytics-kpi-label{color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase}.analytics-kpi-value{display:block;margin-top:7px;font-size:26px;font-weight:850}.analytics-kpi-detail{display:block;margin-top:4px;color:var(--muted);font-size:11px}.analytics-chart{display:flex;align-items:end;gap:5px;height:180px;padding:18px 8px 28px;border-bottom:1px solid rgba(170,196,207,.22)}.analytics-chart-column{flex:1;min-width:8px;position:relative;border-radius:5px 5px 0 0;background:linear-gradient(180deg,var(--brand),var(--brand-action))}.analytics-chart-column span{position:absolute;left:50%;bottom:-22px;transform:translateX(-50%);color:var(--muted);font-size:9px}.analytics-chart-column strong{position:absolute;left:50%;top:-18px;transform:translateX(-50%);font-size:9px}.analytics-type-list,.analytics-camera-list{display:grid;gap:10px}.analytics-progress-row{display:grid;grid-template-columns:105px minmax(0,1fr) 38px;gap:10px;align-items:center;font-size:12px}.analytics-progress-track{height:8px;overflow:hidden;border-radius:999px;background:#273142}.analytics-progress-track span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--brand),var(--brand-action))}.analytics-search-panel{margin-bottom:18px}.analytics-search-grid{display:grid;grid-template-columns:1.1fr repeat(5,minmax(120px,.55fr)) auto;gap:9px}.analytics-search-grid input,.analytics-search-grid select{height:42px;padding:0 11px;border:1px solid var(--line);border-radius:9px;background:#111827;color:white;font:inherit;color-scheme:dark}.analytics-results{display:grid;gap:9px}.analytics-result{display:grid;grid-template-columns:90px minmax(0,1fr) auto;gap:13px;align-items:center;padding:11px;border:1px solid rgba(170,196,207,.14);border-radius:11px;background:rgba(10,15,25,.24)}.analytics-result-thumb{width:90px;aspect-ratio:16/9;object-fit:cover;border-radius:8px;background:#0a0e15}.analytics-result-title{font-weight:780}.analytics-result-meta{margin-top:4px;color:var(--muted);font-size:11px}.analytics-result-actions{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}.analytics-pill{padding:5px 8px;border-radius:999px;background:#315c5d;color:#9ff7f1;font-size:10px;font-weight:800}.analytics-demo-note{padding:10px 13px;border:1px solid #d0a84b;border-radius:9px;background:#3a3020;color:#ffe1a0;font-size:12px}.analytics-seven-day{display:grid;grid-template-columns:repeat(7,1fr);gap:7px;align-items:end;height:130px;padding-top:18px}.analytics-day{position:relative;min-height:6px;border-radius:6px 6px 0 0;background:linear-gradient(180deg,#70a5ff,var(--brand-action))}.analytics-day span{position:absolute;left:50%;bottom:-22px;transform:translateX(-50%);font-size:9px;color:var(--muted)}.analytics-day strong{position:absolute;left:50%;top:-18px;transform:translateX(-50%);font-size:9px}@media(max-width:1200px){.analytics-search-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.analytics-dashboard-grid{grid-template-columns:1fr}.analytics-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.analytics-search-grid,.analytics-kpis{grid-template-columns:1fr}.analytics-result{grid-template-columns:72px minmax(0,1fr)}.analytics-result-thumb{width:72px}.analytics-result-actions{grid-column:1/-1;justify-content:flex-start}}
"""



STYLES += """
.site-summary-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px;margin-bottom:18px}.site-summary-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:rgba(24,33,50,.94)}.site-summary-label{display:block;color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase}.site-summary-value{display:block;margin-top:7px;font-size:25px;font-weight:850}.site-summary-detail{display:block;margin-top:4px;color:var(--muted);font-size:11px}.site-monitor-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(320px,.65fr);gap:16px;margin-bottom:18px}.site-map{position:relative;min-height:520px;overflow:hidden;border:1px solid rgba(170,196,207,.18);border-radius:15px;background:linear-gradient(145deg,#101827,#202c3e)}.site-map-floor{position:absolute;inset:28px;display:grid;grid-template-columns:1.2fr .8fr;grid-template-rows:1fr .85fr;gap:12px}.site-room{position:relative;border:2px solid rgba(207,226,234,.32);border-radius:10px;background:rgba(13,20,31,.34);padding:14px;color:#dce7ee;font-size:12px;font-weight:750}.site-room.large{grid-row:1/3}.site-room-label{opacity:.72}.site-camera-marker{position:absolute;display:grid;place-items:center;width:39px;height:39px;border:0;border-radius:50%;background:var(--brand);color:#102a30;font-weight:900;cursor:pointer;box-shadow:0 0 0 7px rgba(67,209,204,.13),0 10px 22px rgba(0,0,0,.32);transition:transform .18s ease,background .18s ease}.site-camera-marker:hover{transform:scale(1.08)}.site-camera-marker.offline{background:#d79b4b;color:#22180c;box-shadow:0 0 0 7px rgba(215,155,75,.14),0 10px 22px rgba(0,0,0,.32)}.site-camera-marker[data-camera="1"]{left:14%;top:18%}.site-camera-marker[data-camera="2"]{left:57%;top:19%}.site-camera-marker[data-camera="3"]{left:58%;top:66%}.site-camera-marker[data-camera="4"]{left:27%;top:71%}.site-map-legend{position:absolute;left:18px;bottom:16px;display:flex;gap:13px;padding:8px 11px;border-radius:999px;background:rgba(7,11,18,.78);color:var(--muted);font-size:10px}.site-map-legend span::before{content:"";display:inline-block;width:8px;height:8px;margin-right:5px;border-radius:50%;background:var(--brand)}.site-map-legend span:last-child::before{background:#d79b4b}.site-list{display:grid;gap:10px}.site-card{padding:14px;border:1px solid rgba(170,196,207,.15);border-radius:12px;background:rgba(10,15,25,.26)}.site-card.active{border-color:rgba(67,209,204,.65);box-shadow:inset 3px 0 0 var(--brand)}.site-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.site-card-name{font-weight:820}.site-card-meta{margin-top:5px;color:var(--muted);font-size:11px}.site-card-status{padding:5px 8px;border-radius:999px;background:#315c5d;color:#9ff7f1;font-size:10px;font-weight:850;text-transform:uppercase}.site-card-status.warning{background:#302a1d;color:#f0ca72}.site-card-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-top:12px}.site-card-stat{padding:9px;border-radius:9px;background:rgba(255,255,255,.035)}.site-card-stat strong{display:block;font-size:15px}.site-card-stat span{color:var(--muted);font-size:9px;text-transform:uppercase}.site-camera-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.site-camera-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px;border:1px solid rgba(170,196,207,.14);border-radius:10px;background:rgba(10,15,25,.22);color:var(--text);text-decoration:none}.site-camera-row:hover{border-color:rgba(67,209,204,.55)}.site-camera-state{color:#9ff7f1;font-size:10px;font-weight:800}.site-camera-state.offline{color:#f0ca72}.site-refresh{color:var(--muted);font-size:11px}@media(max-width:1100px){.site-monitor-grid{grid-template-columns:1fr}.site-summary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.site-summary-grid,.site-camera-list{grid-template-columns:1fr}.site-map{min-height:420px}.site-map-floor{inset:18px}.site-card-stats{grid-template-columns:1fr}}
"""


STYLES += """
.rbac-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px;margin-bottom:18px}.rbac-summary-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:rgba(24,33,50,.94)}.rbac-summary-card span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.rbac-summary-card strong{display:block;margin-top:7px;font-size:25px}.user-toolbar,.audit-toolbar{display:flex;gap:9px;flex-wrap:wrap;align-items:center}.user-toolbar input,.user-toolbar select,.audit-toolbar input,.audit-toolbar select,.user-dialog input,.user-dialog select{height:42px;padding:0 11px;border:1px solid var(--line);border-radius:9px;background:#111827;color:white;font:inherit;color-scheme:dark}.user-grid{display:grid;gap:10px}.user-row{display:grid;grid-template-columns:minmax(180px,1.2fr) minmax(130px,.65fr) minmax(150px,.75fr) minmax(180px,1fr) auto;gap:12px;align-items:center;padding:14px;border:1px solid rgba(170,196,207,.15);border-radius:11px;background:rgba(10,15,25,.25)}.user-name{font-weight:800}.user-email,.user-access{margin-top:4px;color:var(--muted);font-size:11px}.role-badge,.status-badge{display:inline-flex;padding:5px 8px;border-radius:999px;font-size:10px;font-weight:850;text-transform:uppercase}.role-badge.admin{background:#3b2858;color:#d8baff}.role-badge.installer{background:#263d57;color:#a9d5ff}.role-badge.operator{background:#315c5d;color:#9ff7f1}.role-badge.viewer{background:#343b47;color:#d0d7e0}.status-badge.enabled{background:#315c5d;color:#9ff7f1}.status-badge.disabled{background:#3c2025;color:#ffaaaa}.user-actions{display:flex;gap:7px;justify-content:flex-end}.compact-button{padding:7px 10px;border:1px solid rgba(255,255,255,.18);border-radius:8px;background:transparent;color:white;cursor:pointer}.compact-button.danger{border-color:#7d3b45;color:#ffb1b8}.permission-matrix{width:100%;border-collapse:collapse}.permission-matrix th,.permission-matrix td{padding:12px;border-top:1px solid rgba(170,196,207,.14);text-align:center}.permission-matrix th:first-child,.permission-matrix td:first-child{text-align:left}.permission-yes{color:#8df0ea;font-weight:900}.permission-no{color:#687483}.audit-table-wrap{overflow:auto}.audit-outcome{font-weight:800}.audit-outcome.success{color:#8df0ea}.audit-outcome.denied,.audit-outcome.failed{color:#ffaaaa}.audit-detail{max-width:420px;color:var(--muted);font-size:11px}.user-dialog{width:min(580px,calc(100% - 28px));padding:0;border:1px solid var(--line);border-radius:14px;background:#192234;color:white}.user-dialog::backdrop{background:rgba(0,0,0,.72)}.user-dialog-body{padding:23px}.user-form{display:grid;grid-template-columns:1fr 1fr;gap:13px}.user-form label{display:grid;gap:6px;color:var(--muted);font-size:12px}.user-form .full{grid-column:1/-1}.checkbox-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.checkbox-grid label{display:flex;align-items:center;gap:6px;padding:9px;border:1px solid rgba(170,196,207,.14);border-radius:8px;color:white}.dialog-actions{display:flex;justify-content:flex-end;gap:9px;margin-top:17px}@media(max-width:1050px){.rbac-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.user-row{grid-template-columns:1fr 1fr}.user-actions{justify-content:flex-start}}@media(max-width:700px){.rbac-summary,.user-row,.user-form{grid-template-columns:1fr}.checkbox-grid{grid-template-columns:repeat(2,1fr)}.user-form .full{grid-column:auto}}
"""


STYLES += """
.ops-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px;margin-bottom:18px}.ops-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:rgba(24,33,50,.94)}.ops-label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.ops-value{display:block;margin-top:7px;font-size:24px;font-weight:850}.ops-detail{display:block;margin-top:4px;color:var(--muted);font-size:11px}.ops-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr);gap:16px;margin-bottom:18px}.check-list,.backup-list{display:grid;gap:9px}.check-row,.backup-row{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px;border:1px solid rgba(170,196,207,.14);border-radius:10px;background:rgba(10,15,25,.24)}.check-row strong,.backup-row strong{display:block}.check-row small,.backup-row small{color:var(--muted)}.check-state{padding:5px 8px;border-radius:999px;font-size:10px;font-weight:850;text-transform:uppercase}.check-state.ok{background:#315c5d;color:#9ff7f1}.check-state.fail{background:#3c2025;color:#ffaaaa}.check-state.warn{background:#302a1d;color:#f0ca72}.ops-actions{display:flex;gap:9px;flex-wrap:wrap}.ops-code{padding:12px;border-radius:10px;background:#0d1420;color:#b7c5d2;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;overflow:auto}.restore-zone{padding:15px;border:1px dashed rgba(170,196,207,.28);border-radius:11px}.restore-zone input{display:block;width:100%;margin:10px 0;color:var(--muted)}.ops-warning{padding:11px 13px;border:1px solid #d0a84b;border-radius:9px;background:#3a3020;color:#ffe1a0;font-size:12px}@media(max-width:1050px){.ops-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.ops-grid{grid-template-columns:1fr}}@media(max-width:700px){.ops-summary{grid-template-columns:1fr}}
"""


STYLES += """
.auth-page{min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at top,#1d6070,#111827 58%,#080b10)}.auth-card{width:min(460px,100%);padding:30px;border:1px solid rgba(170,196,207,.22);border-radius:18px;background:rgba(19,27,42,.96);box-shadow:0 30px 90px rgba(0,0,0,.48)}.auth-logo{display:block;width:92px;height:72px;object-fit:contain;margin:0 auto 14px}.auth-card h1{text-align:center}.auth-subtitle{text-align:center;color:var(--muted);margin:8px 0 24px}.auth-form{display:grid;gap:14px}.auth-form label{display:grid;gap:7px;color:var(--muted);font-size:12px}.auth-form input[type=email],.auth-form input[type=password]{height:46px;padding:0 13px;border:1px solid var(--line);border-radius:10px;background:#0f1724;color:white;font:inherit}.auth-remember{display:flex!important;grid-template-columns:auto 1fr;align-items:center;gap:8px!important;color:#dce6ee!important}.auth-error{padding:11px;border:1px solid #7d3b45;border-radius:9px;background:#3c2025;color:#ffb5bc;font-size:12px}.auth-footer{text-align:center;margin-top:18px;color:var(--muted);font-size:11px}.session-user-chip{display:flex;align-items:center;gap:8px;padding:8px 11px;border:1px solid rgba(255,255,255,.14);border-radius:999px;color:white;text-decoration:none;font-size:12px}.password-field{grid-column:1/-1}
"""


STYLES += """
.camera-tools.polished{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;padding:11px;background:#121a28}.camera-action{display:flex;align-items:center;justify-content:center;gap:6px;min-height:38px;padding:8px 7px;border:1px solid rgba(170,196,207,.16);border-radius:9px;background:rgba(255,255,255,.025);color:#dce7ee;text-decoration:none;font:inherit;font-size:11px;font-weight:750;cursor:pointer}.camera-action:hover{border-color:rgba(67,209,204,.55);background:rgba(67,209,204,.08);color:#a7faf4}.camera-action.active{background:#315c5d;color:#9ff7f1;border-color:#4f7f80}.camera-action:disabled{opacity:.45;cursor:not-allowed}.camera-action-icon{font-size:16px;line-height:1}.camera-control-note{padding:8px 12px;border-top:1px solid rgba(170,196,207,.12);background:#101725;color:var(--muted);font-size:10px;text-align:center}@media(max-width:650px){.camera-tools.polished{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""


STYLES += """
.ai-engine-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px;margin-bottom:18px}.ai-engine-card{padding:16px;border:1px solid rgba(170,196,207,.18);border-radius:13px;background:rgba(24,33,50,.94)}.ai-engine-card span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.ai-engine-card strong{display:block;margin-top:7px;font-size:23px}.ai-camera-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.ai-camera-card{padding:15px;border:1px solid rgba(170,196,207,.15);border-radius:12px;background:rgba(10,15,25,.25)}.ai-camera-head{display:flex;justify-content:space-between;gap:12px}.ai-state{padding:5px 8px;border-radius:999px;background:#315c5d;color:#9ff7f1;font-size:10px;font-weight:850;text-transform:uppercase}.ai-state.waiting,.ai-state.starting,.ai-state.loading{background:#302a1d;color:#f0ca72}.ai-state.error,.ai-state.unavailable{background:#3c2025;color:#ffaaaa}.ai-engine-note{padding:13px;border:1px solid rgba(170,196,207,.18);border-radius:10px;background:#101725;color:var(--muted);font-size:12px;line-height:1.5}.ai-recent-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.ai-recent-card{overflow:hidden;border:1px solid rgba(170,196,207,.17);border-radius:12px;background:rgba(10,15,25,.25);color:var(--text);text-decoration:none}.ai-recent-card img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#080a0e}.ai-recent-body{padding:12px}.ai-recent-meta{margin-top:5px;color:var(--muted);font-size:11px}@media(max-width:1000px){.ai-engine-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.ai-recent-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.ai-engine-summary,.ai-camera-grid,.ai-recent-grid{grid-template-columns:1fr}}
"""


STYLES += """
.notification-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(320px,.75fr);gap:16px}.notification-form{display:grid;gap:14px}.notification-form label{display:grid;gap:7px;color:var(--muted);font-size:12px}.notification-form input,.notification-form select{height:42px;padding:0 11px;border:1px solid var(--line);border-radius:9px;background:#111827;color:white;font:inherit;color-scheme:dark}.event-check-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.event-check-grid label{display:flex;align-items:center;gap:7px;padding:9px;border:1px solid rgba(170,196,207,.14);border-radius:8px;color:white}.remote-url{padding:14px;border-radius:10px;background:#0d1420;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all}.push-status{padding:12px;border:1px solid rgba(170,196,207,.17);border-radius:10px;background:rgba(10,15,25,.25)}@media(max-width:900px){.notification-grid{grid-template-columns:1fr}}@media(max-width:700px){.event-check-grid{grid-template-columns:1fr}}
"""


STYLES += """
.enterprise-tabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding:8px;border-radius:12px;background:#182236;margin-bottom:18px}.enterprise-tab{padding:12px;border-radius:9px;text-align:center;color:var(--muted);font-weight:750}.enterprise-tab.active{background:#75ced0;color:#071219}.enterprise-user-table{width:100%;border-collapse:collapse}.enterprise-user-table th,.enterprise-user-table td{padding:14px 12px;border-bottom:1px solid rgba(170,196,207,.18);text-align:left}.enterprise-user-table th{color:var(--muted);font-size:11px;text-transform:uppercase}.invite-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,.8fr);gap:18px}.permission-levels{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.permission-option{padding:12px;border:1px solid rgba(170,196,207,.18);border-radius:10px;text-align:center}.permission-option input{display:block;margin:8px auto}.camera-access-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.pending-badge{display:inline-flex;padding:5px 8px;border-radius:999px;background:#3d321b;color:#f0c86d;font-size:10px;font-weight:850;text-transform:uppercase}.active-badge{display:inline-flex;padding:5px 8px;border-radius:999px;background:#20433f;color:#84f0e5;font-size:10px;font-weight:850;text-transform:uppercase}@media(max-width:950px){.invite-layout{grid-template-columns:1fr}.permission-levels{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:650px){.enterprise-tabs,.permission-levels,.camera-access-grid{grid-template-columns:1fr}.enterprise-user-table{font-size:12px}}
"""

NAV_ITEMS = [
    ("live", "/", "▣", "Live"),
    ("events", "/events", "⌁", "Events"),
    ("alerts", "/alerts", "♢", "Smart alerts"),
    ("playback", "/playback", "◴", "Playback"),
    ("media", "/media", "▧", "Media"),
    ("dashboard", "/dashboard", "◉", "Dashboard"),
    ("admin-portal", "/admin-portal", "★", "Administrator portal"),
    ("admin-customers", "/admin-customers", "♙", "Customer accounts"),
    ("admin-activation", "/admin-activation", "⇢", "Activation operations"),
    ("admin-support", "/admin-support", "?", "Support & compliance"),
    ("settings", "/settings", "⚒", "Settings"),
    ("operations", "/operations", "⚙", "Operations"),
    ("camera-health", "/camera-health", "♡", "Camera health"),
    ("audit", "/audit-logs", "▤", "Audit logs"),
    ("analytics", "/analytics", "⌕", "Analytics"),
    ("investigate", "/investigate", "⌕", "Investigate"),
    ("cases", "/investigation-cases", "▤", "Cases"),
    ("reports", "/incident-reports", "✎", "Incident reports"),
    ("enterprise-notifications", "/enterprise-notifications", "✦", "Notification rules"),
    ("release-readiness", "/release-readiness", "✓", "Release readiness"),
    ("backup-restore", "/backup-restore", "↻", "Backup & restore"),
    ("license-management", "/license-management", "$", "Licensing"),
    ("license-enforcement", "/license-enforcement", "!", "License enforcement"),
    ("billing-operations", "/billing-operations", "¤", "Billing"),
    ("subscription-portal", "/subscription-portal", "◈", "My subscription"),
    ("subscription-admin", "/subscription-admin", "◆", "Subscriptions"),
    ("payment-setup", "/payment-setup", "¢", "Payments"),
    ("customer-onboarding", "/customer-onboarding", "→", "Onboarding"),
    ("cloud-id", "/cloud-id", "☁", "Cloud ID"),
    ("onboarding-admin", "/onboarding-admin", "⇢", "Onboarding admin"),
    ("cloud-recording", "/cloud-recording", "☁", "Cloud recording"),
    ("evidence", "/evidence-integrity", "✓", "Evidence integrity"),
    ("ai-detection", "/ai-detection", "◎", "AI detection"),
    ("sites", "/sites-management", "⌖", "Sites"),
    ("partner", "/partner", "▣", "Partner portal"),
    ("setup", "/setup", "+", "Setup wizard"),
    ("subscription", "/subscription", "≡", "Subscription"),
    ("business-users", "/business-users", "♛", "Business users"),
    ("users", "/users", "♙", "Camera sharing users"),
    ("pricing", "/pricing", "$", "Pricing"),
    ("appliances", "/partner/appliance-dashboard", "▤", "Appliances"),
    ("partner-sales", "/partner-sales", "↗", "Partner sales"),
    ("partner-quotes", "/partner-quotes", "$", "Partner quotes"),
    ("partner-install", "/partner-installations", "⚒", "Installations"),
    ("partner-performance", "/partner-performance", "◈", "Performance"),
    ("enterprise-readiness", "/enterprise-readiness", "◇", "Enterprise readiness"),
    ("enterprise-observability", "/enterprise-observability", "◎", "Observability"),
    ("enterprise-dr", "/enterprise-dr", "↻", "Backup & recovery"),
    ("enterprise-incident", "/enterprise-incident", "⚠", "Incidents & logs"),
    ("production-config", "/production-config", "⚙", "Production configuration"),
    ("launch-readiness", "/launch-readiness", "✓", "Launch readiness"),
    ("go-live", "/go-live", "▶", "Go Live"),
    ("branding", "/branding", "◇", "Branding"),
    ("phone", "/phone-connect", "▯", "Phone access"),
    ("mobile-devices", "/mobile-devices", "▣", "Mobile devices"),
    ("help", "/help", "?", "Help"),
]


def navigation_keys_for_role(role: str) -> set[str] | None:
    role = str(role or "").strip().lower()

    if role in ADMIN_PORTAL_ROLES:
        return None  # Administrators see every navigation item.

    if role == "installer":
        return {
            "partner-install", "media", "help",
        }

    if role in PARTNER_PORTAL_ROLES:
        return {
            "partner", "partner-sales", "partner-quotes", "partner-install",
            "partner-performance", "media", "pricing", "help",
        }

    if role in CUSTOMER_PORTAL_ROLES:
        return {
            "live", "events", "alerts", "playback", "media", "dashboard",
            "settings", "subscription-portal", "customer-onboarding",
            "cloud-id", "phone", "mobile-devices", "help",
        }

    # Existing VMS camera-sharing roles continue using permission-based menus.
    permission_map = {
        "live": "view_live",
        "events": "view_events",
        "alerts": "view_events",
        "playback": "view_events",
        "media": "export_media",
        "dashboard": "view_sites",
        "camera-health": "view_sites",
        "analytics": "view_analytics",
        "settings": "manage_settings",
        "users": "manage_users",
        "audit": "view_audit",
    }
    permissions = ROLE_PERMISSIONS.get(role, set())
    visible = {"help", "phone"}
    for key, permission in permission_map.items():
        if permission in permissions:
            visible.add(key)
    return visible


def page_shell(title: str, active: str, content: str, scripts: str = "") -> str:
    request = REQUEST_CONTEXT.get()
    shell_user = current_user(request) if request is not None else None
    shell_role = str((shell_user or {}).get("role") or "").strip().lower()
    allowed_keys = navigation_keys_for_role(shell_role)

    visible_nav_items = [
        item for item in NAV_ITEMS
        if allowed_keys is None or item[0] in allowed_keys
    ]
    navigation = "".join(
        f'<a class="{"active" if key == active else ""}" href="{url}"><span class="nav-icon">{icon}</span><span>{label}</span></a>'
        for key, url, icon, label in visible_nav_items
    )

    if shell_role == "installer":
        mobile_items = [
            ("partner-install", "/partner-installations", "Installations"),
            ("media", "/media", "Install guides"),
            ("help", "/help", "Help"),
        ]
    elif shell_role in PARTNER_PORTAL_ROLES:
        mobile_items = [
            ("partner-sales", "/partner-sales", "Sales"),
            ("partner-quotes", "/partner-quotes", "Quotes"),
            ("partner-performance", "/partner-performance", "Performance"),
            ("media", "/media", "Training"),
            ("help", "/help", "Help"),
        ]
    elif shell_role in CUSTOMER_PORTAL_ROLES:
        mobile_items = [
            ("live", "/", "Cameras"),
            ("alerts", "/alerts", "Alerts"),
            ("playback", "/playback", "Playback"),
            ("dashboard", "/dashboard", "Dashboard"),
            ("settings", "/settings", "Account"),
        ]
    else:
        mobile_items = [
            ("live", "/", "Cameras"),
            ("alerts", "/alerts", "Alerts"),
            ("investigate", "/investigate", "Investigate"),
            ("dashboard", "/dashboard", "Dashboard"),
            ("sites", "/sites-management", "Sites"),
            ("phone", "/phone-connect", "Phone"),
            ("business-users", "/business-users", "Business users"),
        ]
    mobile = "".join(
        f'<a class="{"active" if key == active else ""}" href="{url}">{label}</a>'
        for key, url, label in mobile_items
    )
    if cloud_settings.staging:
        content='<div class="mock-banner" role="status"><strong>STAGING ENVIRONMENT</strong> · Test data and services only</div>'+content
    content = license_warning_banner() + content
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0a0d12"><link rel="icon" type="image/png" href="/static/brand-icon.png"><title>{escape(title)} · AnyAiCam</title><style>{STYLES}</style></head><body><div class="shell"><aside class="sidebar"><div class="brand"><img class="brand-logo" src="/static/brand-icon.png" alt="AnyAiCam"></div><nav class="nav" aria-label="Primary">{navigation}</nav><form class="sidebar-auth" method="post" action="/logout"><button class="sidebar-logout" type="submit" aria-label="Log out of AnyAiCam"><span class="sidebar-logout-icon" aria-hidden="true">↪</span><span>Log out</span></button></form></aside><main class="content">{content}</main></div><nav class="mobile-nav" aria-label="Mobile">{mobile}</nav><div class="toast" id="toast" role="status"></div>{scripts}<script>const nativeFetch=window.fetch.bind(window);window.fetch=(input,options={{}})=>{{const method=(options.method||'GET').toUpperCase(),sameOrigin=typeof input==='string'?(!input.startsWith('http://')&&!input.startsWith('https://')):input.url.startsWith(location.origin);if(sameOrigin&&['POST','PUT','PATCH','DELETE'].includes(method)){{const csrf=document.cookie.split('; ').find(item=>item.startsWith('anyaicam_csrf='));if(csrf){{let token=decodeURIComponent(csrf.split('=').slice(1).join('='));token=token.replace(/^"(.*)"$/,'$1');options.headers={{...(options.headers||{{}}),'X-CSRF-Token':token}}}}}}return nativeFetch(input,options)}};function showToast(message){{const toast=document.getElementById('toast');toast.textContent=message;toast.classList.add('show');clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>toast.classList.remove('show'),3200)}}function comingSoon(label){{showToast(/saved|error|failed|no live/i.test(label)?label:label+' is ready for a future update.')}}document.addEventListener('DOMContentLoaded',()=>{{const activeTab=document.querySelector('.sidebar .nav a.active');if(activeTab){{requestAnimationFrame(()=>activeTab.scrollIntoView({{block:'center',inline:'nearest',behavior:'auto'}}));}}const mobileActive=document.querySelector('.mobile-nav a.active');if(mobileActive){{requestAnimationFrame(()=>mobileActive.scrollIntoView({{block:'nearest',inline:'center',behavior:'auto'}}));}}}});</script></body></html>"""


from business_portal import register_business_routes
from pricing_portal import register_pricing_routes
from partner_portal import register_partner_routes
from partner_portal import require_partner_access
from partner_workspace import register_partner_workspace_routes, render_partner_workspace
from appliance_cloud import register_appliance_cloud_routes
from cloud_features import register_cloud_feature_routes
from website_partner import register_website_partner_routes
from customer_platform import register_customer_platform_routes
from pwa_routes import register_pwa_routes
from mobile_notifications import register_mobile_notification_routes

register_business_routes(app, page_shell)
register_pricing_routes(app, page_shell)
register_partner_routes(app, page_shell)
register_partner_workspace_routes(app, page_shell)
register_appliance_cloud_routes(app, page_shell)
register_cloud_feature_routes(app, page_shell)
register_website_partner_routes(app, page_shell)
register_customer_platform_routes(
    app,
    page_shell=page_shell,
    current_user=current_user,
    user_camera_ids=user_camera_ids,
    load_license_state=load_license_state,
    record_audit=record_audit,
    is_master_admin=is_master_admin,
)
register_pwa_routes(
    app,
    page_shell=page_shell,
    current_user=current_user,
)
register_mobile_notification_routes(
    app,
    current_user=current_user,
    record_audit=record_audit,
)


@app.get("/camera/{camera_number}", response_class=HTMLResponse)
def camera_detail(camera_number: int) -> str:
    if camera_number < 1 or camera_number > CAMERA_COUNT:
        raise HTTPException(status_code=404, detail="Camera not found")
    content = f"""<header class="topbar"><div><p class="eyebrow">Camera detail</p><h1>Camera {camera_number}</h1></div><a class="ghost-button" href="/">Back to cameras</a></header><section class="panel"><div class="camera-view" style="border-radius:10px"><video id="detail-video" controls muted playsinline></video><div class="camera-placeholder" id="detail-placeholder"><span class="signal">◉</span><strong>Waiting for live hardware</strong><small>The controls remain available while this camera is offline.</small></div></div><div class="camera-tools" style="justify-content:center"><button class="camera-tool" onclick="comingSoon('Microphone requires compatible camera hardware')" title="Microphone">◖</button><button class="camera-tool" id="detail-mute" title="Mute">♩</button><button class="camera-tool" onclick="comingSoon('Snapshot uses the live camera frame')" title="Snapshot">◉</button><button class="camera-tool" onclick="document.getElementById('detail-video').requestFullscreen()" title="Full screen">⛶</button><a class="camera-tool" href="/playback" title="Playback">◴</a></div></section><div class="workspace-tabs" style="margin-top:18px"><button class="workspace-tab active">Live</button><a class="workspace-tab" href="/playback" style="text-decoration:none">Playback</a><a class="workspace-tab" href="/analytics" style="text-decoration:none">Analytics · Demo</a></div>"""
    scripts = f"""<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>const video=document.getElementById('detail-video'),placeholder=document.getElementById('detail-placeholder'),source='/hls/camera{camera_number}.m3u8';if(Hls.isSupported()){{const hls=new Hls();hls.loadSource(source);hls.attachMedia(video);hls.on(Hls.Events.MANIFEST_PARSED,()=>{{placeholder.hidden=true;video.play().catch(()=>{{}})}})}}else if(video.canPlayType('application/vnd.apple.mpegurl')){{video.src=source;video.addEventListener('loadedmetadata',()=>{{placeholder.hidden=true;video.play().catch(()=>{{}})}})}}document.getElementById('detail-mute').addEventListener('click',e=>{{video.muted=!video.muted;e.currentTarget.textContent=video.muted?'♩':'♫';showToast(video.muted?'Camera muted':'Camera audio enabled')}});</script>"""
    return page_shell(f"Camera {camera_number}", "live", content, scripts)



@app.get("/health")
def health_endpoint() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "AnyAiCam VMS",
            "version": APP_VERSION,
            "build_id": BUILD_ID,
            "environment": DEPLOYMENT_ENV,
            "runtime_role": RUNTIME_ROLE,
            "uptime_seconds": max(0, int((datetime.now() - STARTED_AT).total_seconds())),
            "checked_at": datetime.now().isoformat(),
        }
    )


@app.get("/ready")
def ready_endpoint() -> JSONResponse:
    snapshot = readiness_snapshot()
    return JSONResponse(snapshot, status_code=200 if snapshot["ready"] else 503)


@app.get("/version")
def version_endpoint() -> dict:
    return {
        "product": "AnyAiCam VMS",
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "environment": DEPLOYMENT_ENV,
        "runtime_role": RUNTIME_ROLE,
        "aws_region": AWS_REGION or None,
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": max(0, int((datetime.now() - STARTED_AT).total_seconds())),
    }



@app.get("/api/operations/deployment")
def deployment_configuration(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    return {
        "deployment": cloud_configuration_snapshot(),
        "cloud_upload": dict(cloud_upload_state),
        "queue_depth": len(cloud_upload_queue),
        "web_concurrency": WEB_CONCURRENCY,
    }


@app.get("/api/operations/diagnostics")
def operations_diagnostics(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        record_audit(request, "view", "operations", "Permission denied", "denied")
        return {"status": "error", "message": "Administrator or installer permission required."}
    return {"status": "complete", "diagnostics": diagnostics_snapshot()}


@app.post("/api/operations/backups")
def create_backup_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        record_audit(request, "create", "backup", "Permission denied", "denied")
        return {"status": "error", "message": "Administrator or installer permission required."}
    try:
        backup = create_configuration_backup()
    except (OSError, zipfile.BadZipFile) as error:
        record_audit(request, "create", "backup", str(error), "failed")
        return {"status": "error", "message": f"Backup failed: {error}"}
    record_audit(request, "create", f"backup:{backup.name}", "Configuration backup created.")
    return {
        "status": "complete",
        "message": "Configuration backup created.",
        "backup": {
            "name": backup.name,
            "url": f"/api/operations/backups/{quote(backup.name)}",
        },
    }


@app.get("/api/operations/backups")
def backups_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return {"status": "error", "message": "Administrator or installer permission required."}
    return {"status": "complete", "backups": list_backups()}


@app.get("/api/operations/backups/{backup_name}")
def download_backup(request: Request, backup_name: str):
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return JSONResponse({"status": "error", "message": "Permission denied."}, status_code=403)
    safe_name = Path(backup_name).name
    backup = BACKUPS_FOLDER / safe_name
    if not backup.exists() or backup.suffix.lower() != ".zip":
        return JSONResponse({"status": "error", "message": "Backup not found."}, status_code=404)
    record_audit(request, "download", f"backup:{safe_name}", "Configuration backup downloaded.")
    return FileResponse(backup, media_type="application/zip", filename=safe_name)


@app.post("/api/operations/restore")
async def restore_backup_api(request: Request, backup_file: UploadFile = File(...)) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_users"):
        record_audit(request, "restore", "configuration", "Permission denied", "denied")
        return {"status": "error", "message": "Administrator permission required."}

    safe_name = Path(backup_file.filename or "restore.zip").name
    if not safe_name.lower().endswith(".zip"):
        return {"status": "error", "message": "Restore file must be a ZIP archive."}

    destination = RESTORE_STAGING_FOLDER / f"{uuid.uuid4().hex}_{safe_name}"
    try:
        payload = await backup_file.read()
        if not payload or len(payload) > 100 * 1024 * 1024:
            return {"status": "error", "message": "Restore archive is empty or larger than 100 MB."}
        destination.write_bytes(payload)
        if not zipfile.is_zipfile(destination):
            return {"status": "error", "message": "Restore archive is not a valid ZIP file."}
        result = restore_configuration_backup(destination)
    except (OSError, zipfile.BadZipFile) as error:
        record_audit(request, "restore", "configuration", str(error), "failed")
        return {"status": "error", "message": f"Restore failed: {error}"}
    finally:
        destination.unlink(missing_ok=True)

    record_audit(
        request,
        "restore",
        "configuration",
        f"Restored files: {', '.join(result['restored']) or 'none'}",
    )
    return {
        "status": "complete",
        "message": "Configuration restored. Restart the container to reload all settings.",
        **result,
    }


@app.get("/operations", response_class=HTMLResponse)
def operations_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page("Operations", "operations", "manage_settings")

    content = """
    <header class="topbar">
        <div><p class="eyebrow">Production reliability</p><h1>Health & recovery</h1></div>
        <div class="ops-actions">
            <a class="ghost-button" href="/health" target="_blank">Health JSON</a>
            <a class="ghost-button" href="/ready" target="_blank">Readiness JSON</a>
            <button class="action-button" id="create-backup" type="button">Create backup</button>
        </div>
    </header>

    <section class="ops-summary">
        <article class="ops-card"><span class="ops-label">Service</span><strong class="ops-value" id="ops-service">Checking…</strong><span class="ops-detail">Docker health endpoint</span></article>
        <article class="ops-card"><span class="ops-label">Readiness</span><strong class="ops-value" id="ops-ready">Checking…</strong><span class="ops-detail">Workers and configuration</span></article>
        <article class="ops-card"><span class="ops-label">Version</span><strong class="ops-value" id="ops-version">—</strong><span class="ops-detail" id="ops-build">Build</span></article>
        <article class="ops-card"><span class="ops-label">Uptime</span><strong class="ops-value" id="ops-uptime">—</strong><span class="ops-detail">Since container startup</span></article>
    </section>

    <section class="ops-grid">
        <article class="panel">
            <div class="panel-head"><h2>Startup self-test</h2><span class="health-detail" id="ops-checked">Loading…</span></div>
            <div class="check-list" id="ops-checks"><div class="empty">Running diagnostics…</div></div>
        </article>
        <article class="panel">
            <div class="panel-head"><h2>Configuration issues</h2><span class="health-detail">Secrets are never displayed</span></div>
            <div class="check-list" id="ops-config"><div class="empty">Checking configuration…</div></div>
        </article>
    </section>

    <section class="ops-grid">
        <article class="panel">
            <div class="panel-head"><h2>Configuration backups</h2><span class="health-detail">Settings and metadata only</span></div>
            <div class="backup-list" id="backup-list"><div class="empty">Loading backups…</div></div>
        </article>
        <article class="panel">
            <div class="panel-head"><h2>Restore configuration</h2><span class="health-detail">Administrator only</span></div>
            <div class="ops-warning">A restore replaces configuration files. Create a new backup first and restart the container after restoring.</div>
            <form class="restore-zone" id="restore-form">
                <strong>Select a backup ZIP</strong>
                <input id="restore-file" name="backup_file" type="file" accept=".zip" required>
                <button class="action-button" type="submit">Restore backup</button>
            </form>
        </article>
    </section>

    <section class="panel">
        <div class="panel-head"><h2>Recovery notes</h2><span class="health-detail">Deployment checklist</span></div>
        <div class="ops-code">1. Keep .env outside source control.
2. Back up the recordings volume separately.
3. Download a configuration backup before every upgrade.
4. Use docker compose pull/build, then verify /health and /ready.
5. Roll back by restoring the previous image and configuration backup.</div>
    </section>
    """

    scripts = """
    <script>
    function formatUptime(seconds) {
        const days = Math.floor(seconds / 86400);
        const hours = Math.floor((seconds % 86400) / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        return `${days}d ${hours}h ${minutes}m`;
    }

    function safe(value) {
        const node = document.createElement('span');
        node.textContent = value ?? '';
        return node.innerHTML;
    }

    async function refreshOperations() {
        const response = await fetch('/api/operations/diagnostics', {cache: 'no-store'});
        const data = await response.json();
        if (data.status !== 'complete') {
            showToast(data.message);
            return;
        }
        const diagnostics = data.diagnostics;
        document.getElementById('ops-service').textContent = 'Healthy';
        document.getElementById('ops-ready').textContent = diagnostics.readiness.ready ? 'Ready' : 'Starting';
        document.getElementById('ops-version').textContent = diagnostics.version;
        document.getElementById('ops-build').textContent = `Build ${diagnostics.build_id}`;
        document.getElementById('ops-uptime').textContent = formatUptime(diagnostics.uptime_seconds);
        document.getElementById('ops-checked').textContent = new Date(diagnostics.checked_at).toLocaleString();

        const checks = diagnostics.readiness.self_test.checks;
        document.getElementById('ops-checks').innerHTML = checks.map(check => `
            <div class="check-row">
                <div><strong>${safe(check.name.replaceAll('_', ' '))}</strong><small>${safe(check.detail)}</small></div>
                <span class="check-state ${check.ok ? 'ok' : 'fail'}">${check.ok ? 'Pass' : 'Fail'}</span>
            </div>`).join('');

        const issues = diagnostics.configuration_issues;
        document.getElementById('ops-config').innerHTML = issues.length ? issues.map(issue => `
            <div class="check-row">
                <div><strong>${safe(issue.key)}</strong><small>${safe(issue.message)}</small></div>
                <span class="check-state ${issue.severity === 'critical' ? 'fail' : 'warn'}">${safe(issue.severity)}</span>
            </div>`).join('') : '<div class="check-row"><div><strong>Configuration valid</strong><small>No issues detected.</small></div><span class="check-state ok">Pass</span></div>';

        renderBackups(diagnostics.backups);
    }

    function renderBackups(backups) {
        const list = document.getElementById('backup-list');
        list.innerHTML = backups.length ? backups.map(backup => `
            <div class="backup-row">
                <div><strong>${safe(backup.name)}</strong><small>${backup.size_mb} MB · ${new Date(backup.created_at).toLocaleString()}</small></div>
                <a class="download" href="${backup.url}">Download</a>
            </div>`).join('') : '<div class="empty">No backups created yet.</div>';
    }

    document.getElementById('create-backup').addEventListener('click', async () => {
        const response = await fetch('/api/operations/backups', {method: 'POST'});
        const data = await response.json();
        showToast(data.message);
        if (data.status === 'complete') refreshOperations();
    });

    document.getElementById('restore-form').addEventListener('submit', async event => {
        event.preventDefault();
        if (!confirm('Restore this configuration backup?')) return;
        const file = document.getElementById('restore-file').files[0];
        if (!file) return;
        const form = new FormData();
        form.append('backup_file', file);
        const response = await fetch('/api/operations/restore', {method: 'POST', body: form});
        const data = await response.json();
        showToast(data.message);
        if (data.status === 'complete') refreshOperations();
    });

    refreshOperations();
    setInterval(refreshOperations, 30000);
    </script>
    """
    record_audit(request, "view", "operations", "Opened production health and recovery.")
    return page_shell("Operations", "operations", content, scripts)


@app.get("/api/cameras/status")
def camera_status() -> dict:
    now = time.time()
    cameras = []
    for camera_number in range(1, CAMERA_COUNT + 1):
        manifest = HLS_FOLDER / f"camera{camera_number}.m3u8"
        manifest_age = None
        if manifest.exists():
            try:
                manifest_age = max(0, round(now - manifest.stat().st_mtime))
            except OSError:
                pass
        streaming = manifest_age is not None and manifest_age < 15
        cameras.append(
            {
                "camera": camera_number,
                "online": streaming,
                "stream": "online" if streaming else camera_process_state[camera_number]["live"],
                "recording": camera_process_state[camera_number]["recording"],
                "last_stream_update_seconds": manifest_age,
                "reconnects": camera_reconnect_counts[camera_number],
            }
        )
    return {"cameras": cameras, "checked_at": datetime.now().isoformat()}


def system_metrics() -> dict:
    cpu_count = os.cpu_count() or 1
    try:
        one_minute_load = os.getloadavg()[0]
        cpu_percent = min(100, round((one_minute_load / cpu_count) * 100, 1))
    except (AttributeError, OSError):
        cpu_percent = 0
    memory_percent = 0.0
    try:
        memory_values = {}
        with Path("/proc/meminfo").open(encoding="utf-8") as memory_file:
            for line in memory_file:
                key, value = line.split(":", 1)
                memory_values[key] = int(value.strip().split()[0])
        total = memory_values.get("MemTotal", 0)
        available = memory_values.get("MemAvailable", 0)
        if total:
            memory_percent = round(((total - available) / total) * 100, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        disk = shutil.disk_usage(RECORDINGS_FOLDER)
        storage_percent = round((disk.used / disk.total) * 100, 1) if disk.total else 0
        storage_free_gb = round(disk.free / (1024**3), 1)
    except OSError:
        storage_percent, storage_free_gb = 0, 0
    return {
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "storage_percent": storage_percent,
        "storage_free_gb": storage_free_gb,
        "checked_at": datetime.now().isoformat(),
    }


@app.get("/api/system/metrics")
def metrics_api() -> dict:
    return system_metrics()


@app.get("/api/events")
def events_api(camera: int | None = None, limit: int = 100) -> dict:
    safe_limit = max(1, min(limit, 500))
    events = load_motion_events()
    if camera is not None:
        events = [event for event in events if event.get("camera") == camera]
    events.sort(
        key=lambda event: event.get("start_time", event.get("timestamp", "")),
        reverse=True,
    )
    return {"events": events[:safe_limit], "motion_detection": MOTION_DETECTION_ENABLED}


@app.post("/api/snapshots")
def save_snapshot(request: SnapshotRequest) -> dict:
    if not request.image_data.startswith("data:image/png;base64,"):
        return {"status": "error", "message": "Snapshot must be a PNG image."}
    safe_site = slugify(request.site) or "home"
    try:
        image_bytes = base64.b64decode(request.image_data.split(",", 1)[1], validate=True)
    except (binascii.Error, IndexError):
        return {"status": "error", "message": "Snapshot image data is invalid."}
    if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
        return {"status": "error", "message": "Snapshot is empty or larger than 20 MB."}
    captured_at = datetime.now()
    day = captured_at.strftime("%Y-%m-%d")
    target_folder = SNAPSHOTS_FOLDER / safe_site / f"camera{request.camera}" / day
    target_folder.mkdir(parents=True, exist_ok=True)
    filename = f"camera{request.camera}_{captured_at.strftime('%Y-%m-%d_%H-%M-%S')}.png"
    target_path = target_folder / filename
    try:
        target_path.write_bytes(image_bytes)
    except OSError as error:
        return {"status": "error", "message": f"Could not save snapshot: {error}"}
    url = f"/recordings/media/snapshots/{safe_site}/camera{request.camera}/{day}/{quote(filename)}"
    return {
        "status": "complete", "message": "Snapshot saved to the media library.",
        "url": url, "filename": filename, "camera": request.camera,
        "site": safe_site, "captured_at": captured_at.isoformat(),
    }


def media_library_items() -> list[dict]:
    items = []
    for snapshot in SNAPSHOTS_FOLDER.rglob("*.png"):
        try:
            relative = snapshot.relative_to(SNAPSHOTS_FOLDER)
            site, camera_name, day = relative.parts[:3]
            captured_at = datetime.fromtimestamp(snapshot.stat().st_mtime)
            items.append({
                "type": "snapshot", "site": site, "camera": camera_name.removeprefix("camera"),
                "date": day, "timestamp": captured_at.isoformat(),
                "url": "/recordings/media/snapshots/" + "/".join(quote(part) for part in relative.parts),
                "name": snapshot.name,
            })
        except (OSError, ValueError, IndexError):
            continue
    for clip in CLIPS_FOLDER.glob("*.mp4"):
        try:
            modified_at = datetime.fromtimestamp(clip.stat().st_mtime)
            camera_match = re.match(r"camera(\d+)_", clip.name)
            items.append({
                "type": "clip", "site": "home",
                "camera": camera_match.group(1) if camera_match else "",
                "date": modified_at.strftime("%Y-%m-%d"), "timestamp": modified_at.isoformat(),
                "url": f"/recordings/clips/{quote(clip.name)}", "name": clip.name,
            })
        except OSError:
            continue
    items.sort(key=lambda item: item["timestamp"], reverse=True)
    return items


@app.get("/api/media")
def media_api() -> dict:
    return {"items": media_library_items()}


@app.get("/api/cameras/{camera_number}/event-settings")
def read_event_settings(camera_number: int) -> dict:
    if not 1 <= camera_number <= CAMERA_COUNT:
        return {"status": "error", "message": "Camera not found."}
    return {"status": "complete", "settings": get_event_settings(camera_number).model_dump()}


@app.put("/api/cameras/{camera_number}/event-settings")
def update_event_settings(camera_number: int, settings: EventSettingsModel) -> dict:
    if camera_number != settings.camera:
        return {"status": "error", "message": "Camera number does not match the request."}
    all_settings = load_json_file(EVENT_SETTINGS_FILE, {})
    all_settings[str(camera_number)] = settings.model_dump()
    try:
        save_json_file(EVENT_SETTINGS_FILE, all_settings)
    except OSError as error:
        return {"status": "error", "message": f"Could not save event settings: {error}"}
    return {"status": "complete", "message": "Motion settings saved.", "settings": settings.model_dump()}


@app.get("/api/cameras/{camera_number}/alert-rule")
def read_alert_rule(camera_number: int) -> dict:
    if not 1 <= camera_number <= CAMERA_COUNT:
        return {"status": "error", "message": "Camera not found."}
    return {"status": "complete", "rule": get_alert_rule(camera_number).model_dump()}


@app.put("/api/cameras/{camera_number}/alert-rule")
def update_alert_rule(camera_number: int, rule: AlertRuleModel) -> dict:
    if camera_number != rule.camera:
        return {"status": "error", "message": "Camera number does not match the request."}
    rules = load_json_file(ALERT_RULES_FILE, {})
    rules[str(camera_number)] = rule.model_dump()
    try:
        save_json_file(ALERT_RULES_FILE, rules)
    except OSError as error:
        return {"status": "error", "message": f"Could not save alert rule: {error}"}
    return {"status": "complete", "message": "Alert rule saved.", "rule": rule.model_dump()}


@app.get("/api/alerts")
def in_app_alerts(limit: int = 100) -> dict:
    alerts = []
    try:
        if IN_APP_ALERTS_FILE.exists():
            for line in IN_APP_ALERTS_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    alerts.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    alerts.sort(key=lambda alert: alert.get("timestamp", ""), reverse=True)
    return {"alerts": alerts[:max(1, min(limit, 500))]}


@app.get("/api/health/issues")
def health_issue_api() -> dict:
    return {"issues": list(health_issues.values()), "checked_at": datetime.now().isoformat()}


def load_partner_customers() -> list[dict]:
    try:
        if PARTNER_CUSTOMERS_FILE.exists():
            data = json.loads(PARTNER_CUSTOMERS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_partner_customers(customers: list[dict]) -> None:
    temporary = PARTNER_CUSTOMERS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(customers, indent=2), encoding="utf-8")
    temporary.replace(PARTNER_CUSTOMERS_FILE)


def load_json_list(path: Path) -> list[dict]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_json_list(path: Path, items: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(items, indent=2), encoding="utf-8")
    temporary.replace(path)


def mock_analytics_events() -> list[dict]:
    now = datetime.now()
    samples = [
        ("person", 1, None, None, "entry"),
        ("vehicle", 2, None, "silver", "inbound"),
        ("plate", 2, "ANY123", "silver", "inbound"),
        ("line_crossing", 3, None, None, "entry"),
        ("intrusion", 4, None, None, None),
        ("plate", 1, "CAM742", "black", "outbound"),
    ]
    events = []
    for index, (event_type, camera, plate, color, direction) in enumerate(samples):
        timestamp = now - timedelta(minutes=index * 37 + 8)
        events.append(AnalyticsEventModel(
            camera=camera, rule_name=f"Mock {event_type.replace('_', ' ').title()}",
            event_type=event_type, direction=direction, timestamp=timestamp,
            confidence=round(0.72 + index * 0.035, 2), plate_number=plate,
            vehicle_color=color, linked_recording=linked_recording_for(camera, timestamp),
            mock=True,
        ).model_dump(mode="json"))
    return events


def analytics_events() -> list[dict]:
    stored = load_json_list(ANALYTICS_EVENTS_FILE)
    return stored if stored else mock_analytics_events()


@app.get("/api/partner/customers")
def partner_customers_api(request: Request) -> dict:
    require_partner_access(request)
    return {"customers": load_partner_customers()}


@app.post("/api/partner/customers")
def create_partner_customer(request: Request, customer: PartnerCustomerModel) -> dict:
    require_partner_access(request)
    if customer.status not in {"active", "pending_installation", "trial", "suspended", "cancelled"}:
        return {"status": "error", "message": "Unsupported customer status."}
    customers = load_partner_customers()
    if any(item.get("email", "").lower() == customer.email.lower() for item in customers):
        return {"status": "error", "message": "A customer with this email already exists."}
    customers.append(customer.model_dump(mode="json"))
    try:
        save_partner_customers(customers)
    except OSError as error:
        return {"status": "error", "message": f"Could not save customer: {error}"}
    return {"status": "complete", "message": "Customer added.", "customer": customers[-1]}



@app.get("/api/ai/status")
def ai_detection_status() -> dict:
    recent_people = [
        event
        for event in analytics_events()
        if event.get("event_type") in YOLO_ALLOWED_CLASSES
        and not event.get("mock", False)
    ]
    recent_people.sort(key=lambda event: event.get("timestamp", ""), reverse=True)
    return {
        "enabled": AI_PERSON_DETECTION_ENABLED,
        "engine": f"Ultralytics YOLO ({YOLO_MODEL_NAME})",
        "opencv_available": cv2 is not None and YOLO is not None,
        "interval_seconds": AI_DETECTION_INTERVAL_SECONDS,
        "cooldown_seconds": AI_PERSON_COOLDOWN_SECONDS,
        "confidence_threshold": YOLO_CONFIDENCE,
        "allowed_classes": sorted(YOLO_ALLOWED_CLASSES),
        "cameras": [
            {"camera": camera_number, **ai_detection_state[camera_number]}
            for camera_number in range(1, CAMERA_COUNT + 1)
        ],
        "recent_events": recent_people[:12],
        "checked_at": datetime.now().isoformat(),
    }


@app.get("/ai-detection", response_class=HTMLResponse)
def ai_detection_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        return permission_denied_page("AI detection", "ai-detection", "view_analytics")

    camera_cards = "".join(
        (
            f'<article class="ai-camera-card"><div class="ai-camera-head">'
            f'<div><strong>Camera {camera}</strong>'
            f'<div class="health-detail" id="ai-detail-{camera}">Loading detector status...</div></div>'
            f'<span class="ai-state starting" id="ai-state-{camera}">Starting</span></div>'
            f'<div class="health-detail" style="margin-top:12px">'
            f'Detections: <strong id="ai-count-{camera}">0</strong><br>'
            f'Last detection: <span id="ai-last-{camera}">None</span></div></article>'
        )
        for camera in range(1, CAMERA_COUNT + 1)
    )

    content = f'''
    <header class="topbar">
        <div><p class="eyebrow">Local computer vision</p><h1>YOLO object detection</h1></div>
        <a class="ghost-button" href="/analytics">Open analytics</a>
    </header>
    <section class="ai-engine-summary">
        <article class="ai-engine-card"><span>Engine</span><strong id="ai-engine">Loading...</strong></article>
        <article class="ai-engine-card"><span>Status</span><strong id="ai-enabled">Loading...</strong></article>
        <article class="ai-engine-card"><span>Scan interval</span><strong id="ai-interval">-</strong></article>
        <article class="ai-engine-card"><span>Recent detections</span><strong id="ai-total">-</strong></article>
    </section>
    <section class="panel" style="margin-bottom:18px">
        <div class="panel-head"><div><h2>Camera workers</h2><div class="health-detail">Local multi-class object detection on live HLS frames.</div></div><span class="health-detail" id="ai-refreshed">Loading...</span></div>
        <div class="ai-camera-grid">{camera_cards}</div>
    </section>
    <section class="panel" style="margin-bottom:18px">
        <div class="panel-head"><h2>Recent object detections</h2><span class="health-detail">Saved to Analytics and Alerts</span></div>
        <div class="ai-recent-grid" id="ai-recent"><div class="empty">Waiting for detections...</div></div>
    </section>
    <div class="ai-engine-note">This phase uses a local Ultralytics YOLO model for people, vehicles, animals, backpacks, and suitcases. Video remains on this VMS. The first run may take longer while the model weights are downloaded.</div>
    '''

    scripts = '''
    <script>
    function safe(value){const node=document.createElement('span');node.textContent=value??'';return node.innerHTML;}
    async function refreshAiStatus(){
        const response=await fetch('/api/ai/status',{cache:'no-store'});
        const data=await response.json();
        document.getElementById('ai-engine').textContent=data.engine;
        document.getElementById('ai-enabled').textContent=data.enabled&&data.opencv_available?'Running':data.opencv_available?'Disabled':'Unavailable';
        document.getElementById('ai-interval').textContent=`${data.interval_seconds}s`;
        document.getElementById('ai-total').textContent=data.recent_events.length;
        document.getElementById('ai-refreshed').textContent=`Updated ${new Date(data.checked_at).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'})}`;
        data.cameras.forEach(camera=>{
            const state=document.getElementById(`ai-state-${camera.camera}`);
            const detail=document.getElementById(`ai-detail-${camera.camera}`);
            document.getElementById(`ai-count-${camera.camera}`).textContent=camera.detections;
            document.getElementById(`ai-last-${camera.camera}`).textContent=camera.last_detection?new Date(camera.last_detection).toLocaleString():'None';
            state.textContent=camera.status;
            state.className=`ai-state ${camera.status}`;
            detail.textContent=camera.error||`Checks every ${data.interval_seconds} seconds`;
        });
        const recent=document.getElementById('ai-recent');
        recent.innerHTML=data.recent_events.length?data.recent_events.map(event=>`
            <a class="ai-recent-card" href="${event.linked_recording||`/camera/${event.camera}`}">
                ${event.thumbnail?`<img src="${event.thumbnail}" alt="${safe(event.event_type)} detected on Camera ${event.camera}">`:'<div class="empty">No thumbnail</div>'}
                <div class="ai-recent-body"><strong>Camera ${event.camera} ${safe(event.event_type)}</strong>
                <div class="ai-recent-meta">${new Date(event.timestamp).toLocaleString()} · ${Math.round(Number(event.confidence||0)*100)}% confidence</div></div>
            </a>`).join(''):'<div class="empty">No real object detections yet.</div>';
    }
    refreshAiStatus();setInterval(refreshAiStatus,10000);
    </script>
    '''
    record_audit(request, "view", "ai_detection", "Opened local YOLO object detection.")
    return page_shell("AI detection", "ai-detection", content, scripts)



@app.get("/api/notifications/settings")
def notification_settings_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return {"status": "error", "message": "Permission denied."}
    return {
        "status": "complete",
        "settings": load_notification_settings(),
        "smtp_configured": bool(SMTP_HOST and SMTP_FROM),
        "push_configured": bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and webpush),
        "vapid_public_key": VAPID_PUBLIC_KEY,
        "subscription_count": len(load_push_subscriptions()),
        "public_url": PUBLIC_BASE_URL,
    }


@app.put("/api/notifications/settings")
def update_notification_settings(
    request: Request,
    settings: NotificationSettingsModel,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        record_audit(request, "update", "notification_settings", "Permission denied", "denied")
        return {"status": "error", "message": "Permission denied."}
    save_notification_settings(settings.model_dump())
    record_audit(request, "update", "notification_settings", "Notification preferences updated.")
    return {"status": "complete", "message": "Notification settings saved."}


@app.post("/api/notifications/test-email")
def test_email_notification(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return {"status": "error", "message": "Permission denied."}
    settings = load_notification_settings()
    ok, detail = send_email_alert(
        {
            "event_type": "test",
            "camera": None,
            "timestamp": datetime.now().isoformat(),
            "message": "This is a test email from AnyAiCam.",
        },
        settings,
    )
    record_audit(
        request,
        "test",
        "email_notification",
        detail,
        "success" if ok else "failed",
    )
    return {"status": "complete" if ok else "error", "message": detail}


@app.post("/api/notifications/test-push")
def test_push_notification(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return {"status": "error", "message": "Permission denied."}
    delivered, errors = send_push_alert(
        {
            "event_type": "test",
            "camera": None,
            "timestamp": datetime.now().isoformat(),
            "message": "This is a test push notification from AnyAiCam.",
        }
    )
    message = f"Delivered to {delivered} device(s)."
    if errors:
        message += " " + errors[0]
    return {
        "status": "complete" if delivered else "error",
        "message": message,
    }


@app.post("/api/notifications/push-subscriptions")
def save_push_subscription(
    request: Request,
    subscription: PushSubscriptionModel,
) -> dict:
    subscriptions = load_push_subscriptions()
    payload = subscription.model_dump()
    payload["user_id"] = current_user(request).get("id")
    payload["created_at"] = datetime.now().isoformat()
    existing = next(
        (
            item for item in subscriptions
            if item.get("endpoint") == subscription.endpoint
        ),
        None,
    )
    if existing:
        existing.update(payload)
    else:
        subscriptions.append(payload)
    save_push_subscriptions(subscriptions)
    record_audit(request, "create", "push_subscription", "Push enabled on a device.")
    return {"status": "complete", "message": "Push notifications enabled on this device."}


@app.delete("/api/notifications/push-subscriptions")
def delete_push_subscription(request: Request, endpoint: str) -> dict:
    subscriptions = load_push_subscriptions()
    remaining = [
        item for item in subscriptions
        if item.get("endpoint") != endpoint
    ]
    save_push_subscriptions(remaining)
    record_audit(request, "delete", "push_subscription", "Push disabled on a device.")
    return {"status": "complete", "message": "Push notifications disabled on this device."}


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page("Notifications", "notifications", "manage_settings")

    event_types = [
        "person", "car", "truck", "bus", "motorcycle", "bicycle",
        "dog", "cat", "bird", "backpack", "suitcase", "motion",
        "stream_offline", "recording_stopped", "low_disk_space",
    ]
    event_checks = "".join(
        f'<label><input type="checkbox" name="event_type" value="{event_type}">{event_type.replace("_", " ").title()}</label>'
        for event_type in event_types
    )
    remote_url = PUBLIC_BASE_URL or "Set ANYAICAM_PUBLIC_URL in .env"

    content = f"""
    <header class="topbar">
        <div><p class="eyebrow">Remote access</p><h1>Email & push notifications</h1></div>
        <a class="ghost-button" href="/alerts">Open alerts</a>
    </header>

    <section class="notification-grid">
        <article class="panel">
            <div class="panel-head"><h2>Notification preferences</h2><span class="health-detail">Administrator settings</span></div>
            <form class="notification-form" id="notification-form">
                <label><span><input id="email-enabled" type="checkbox"> Enable email alerts</span></label>
                <label>Alert recipient<input id="recipient-email" type="email" placeholder="you@example.com"></label>
                <label><span><input id="push-enabled" type="checkbox"> Enable browser push alerts</span></label>
                <div><span class="health-detail">Event types</span><div class="event-check-grid">{event_checks}</div></div>
                <label><span><input id="quiet-enabled" type="checkbox"> Enable quiet hours</span></label>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
                    <label>Quiet start<input id="quiet-start" type="time" value="22:00"></label>
                    <label>Quiet end<input id="quiet-end" type="time" value="07:00"></label>
                </div>
                <button class="action-button" type="submit">Save notification settings</button>
            </form>
        </article>

        <div style="display:grid;gap:16px">
            <article class="panel">
                <div class="panel-head"><h2>Phone access</h2><span class="health-detail">Open this on your phone</span></div>
                <div class="remote-url">{escape(remote_url)}</div>
                <p class="health-detail">Your phone must be connected to the same LAN or your private Tailscale network. For secure cookies, use an HTTPS address.</p>
            </article>
            <article class="panel">
                <div class="panel-head"><h2>Push on this device</h2><span class="health-detail" id="push-count">Checking...</span></div>
                <div class="push-status" id="push-status">Push permission has not been checked.</div>
                <div class="ops-actions" style="margin-top:12px">
                    <button class="action-button" id="enable-push" type="button">Enable push</button>
                    <button class="ghost-button" id="test-push" type="button">Test push</button>
                    <button class="ghost-button" id="test-email" type="button">Test email</button>
                </div>
            </article>
        </div>
    </section>
    """

    scripts = """
    <script>
    let notificationMeta=null;
    function urlBase64ToUint8Array(base64String){
        const padding='='.repeat((4-base64String.length%4)%4);
        const base64=(base64String+padding).replace(/-/g,'+').replace(/_/g,'/');
        const raw=atob(base64);
        return Uint8Array.from([...raw].map(char=>char.charCodeAt(0)));
    }
    async function loadNotificationSettings(){
        const response=await fetch('/api/notifications/settings',{cache:'no-store'});
        const data=await response.json();
        if(data.status!=='complete'){showToast(data.message);return;}
        notificationMeta=data;
        const settings=data.settings;
        document.getElementById('email-enabled').checked=settings.email_enabled;
        document.getElementById('recipient-email').value=settings.recipient_email||'';
        document.getElementById('push-enabled').checked=settings.push_enabled;
        document.getElementById('quiet-enabled').checked=settings.quiet_hours_enabled;
        document.getElementById('quiet-start').value=settings.quiet_start;
        document.getElementById('quiet-end').value=settings.quiet_end;
        document.querySelectorAll('[name=event_type]').forEach(input=>{
            input.checked=settings.event_types.includes(input.value);
        });
        document.getElementById('push-count').textContent=`${data.subscription_count} device(s)`;
        document.getElementById('push-status').textContent=
            data.push_configured?'Push server is configured.':'Add VAPID keys to .env before enabling push.';
    }
    document.getElementById('notification-form').addEventListener('submit',async event=>{
        event.preventDefault();
        const payload={
            email_enabled:document.getElementById('email-enabled').checked,
            push_enabled:document.getElementById('push-enabled').checked,
            recipient_email:document.getElementById('recipient-email').value,
            event_types:[...document.querySelectorAll('[name=event_type]:checked')].map(input=>input.value),
            quiet_hours_enabled:document.getElementById('quiet-enabled').checked,
            quiet_start:document.getElementById('quiet-start').value,
            quiet_end:document.getElementById('quiet-end').value,
        };
        const response=await fetch('/api/notifications/settings',{
            method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
        });
        const data=await response.json();showToast(data.message);
    });
    document.getElementById('test-email').addEventListener('click',async()=>{
        const response=await fetch('/api/notifications/test-email',{method:'POST'});
        const data=await response.json();showToast(data.message);
    });
    document.getElementById('test-push').addEventListener('click',async()=>{
        const response=await fetch('/api/notifications/test-push',{method:'POST'});
        const data=await response.json();showToast(data.message);
    });
    document.getElementById('enable-push').addEventListener('click',async()=>{
        if(!notificationMeta?.vapid_public_key){showToast('VAPID public key is not configured.');return;}
        if(!('serviceWorker' in navigator)||!('PushManager' in window)){showToast('This browser does not support Web Push.');return;}
        const permission=await Notification.requestPermission();
        if(permission!=='granted'){showToast('Push permission was not granted.');return;}
        const registration=await navigator.serviceWorker.register('/static/push-sw.js');
        let subscription=await registration.pushManager.getSubscription();
        if(!subscription){
            subscription=await registration.pushManager.subscribe({
                userVisibleOnly:true,
                applicationServerKey:urlBase64ToUint8Array(notificationMeta.vapid_public_key),
            });
        }
        const payload=subscription.toJSON();
        payload.user_agent=navigator.userAgent;
        const response=await fetch('/api/notifications/push-subscriptions',{
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
        });
        const data=await response.json();showToast(data.message);loadNotificationSettings();
    });
    loadNotificationSettings();
    </script>
    """
    record_audit(request, "view", "notifications", "Opened notification settings.")
    return page_shell("Notifications", "notifications", content, scripts)


@app.get("/api/analytics/rules")
def analytics_rules_api(camera: int | None = None) -> dict:
    rules = load_json_list(ANALYTICS_RULES_FILE)
    if camera is not None:
        rules = [rule for rule in rules if rule.get("camera") == camera]
    return {"rules": rules}


@app.post("/api/analytics/rules")
def create_analytics_rule(rule: AnalyticsRuleModel) -> dict:
    rules = load_json_list(ANALYTICS_RULES_FILE)
    existing_index = next((index for index, item in enumerate(rules) if item.get("id") == rule.id), None)
    payload = rule.model_dump(mode="json")
    if existing_index is None:
        rules.append(payload)
    else:
        rules[existing_index] = payload
    try:
        save_json_list(ANALYTICS_RULES_FILE, rules)
    except OSError as error:
        return {"status": "error", "message": f"Could not save analytic rule: {error}"}
    return {"status": "complete", "message": "Analytic rule saved.", "rule": payload}


@app.get("/api/analytics/events")
def analytics_event_search(
    event_type: str | None = None,
    camera: int | None = None,
    site: str | None = None,
    plate: str | None = None,
    color: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    events = analytics_events()
    if event_type:
        events = [event for event in events if event.get("event_type") == event_type]
    if camera:
        events = [event for event in events if event.get("camera") == camera]
    if site:
        events = [event for event in events if event.get("site", "").lower() == site.lower()]
    if plate:
        events = [event for event in events if plate.lower() in (event.get("plate_number") or "").lower()]
    if color:
        events = [event for event in events if color.lower() in (event.get("vehicle_color") or "").lower()]
    if date_from:
        events = [event for event in events if event.get("timestamp", "")[:10] >= date_from]
    if date_to:
        events = [event for event in events if event.get("timestamp", "")[:10] <= date_to]
    events.sort(key=lambda event: event.get("timestamp", ""), reverse=True)
    return {"events": events, "mock_data": not ANALYTICS_EVENTS_FILE.exists()}


@app.get("/api/analytics/summary")
def analytics_summary_api() -> dict:
    now = datetime.now()
    events = analytics_events()

    def parse_time(event: dict) -> datetime | None:
        raw = event.get("timestamp") or event.get("start_time")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    valid_events = [(event, parse_time(event)) for event in events]
    valid_events = [(event, stamp) for event, stamp in valid_events if stamp is not None]
    last_24_start = now - timedelta(hours=24)
    last_7_start = now - timedelta(days=7)
    today_key = now.strftime("%Y-%m-%d")
    recent_24 = [(event, stamp) for event, stamp in valid_events if stamp >= last_24_start]
    recent_7 = [(event, stamp) for event, stamp in valid_events if stamp >= last_7_start]

    type_counts: dict[str, int] = {}
    camera_counts = {str(camera): 0 for camera in range(1, CAMERA_COUNT + 1)}
    confidence_values: list[float] = []
    for event, _ in recent_7:
        event_type = str(event.get("event_type", "unknown"))
        type_counts[event_type] = type_counts.get(event_type, 0) + 1
        camera_key = str(event.get("camera", ""))
        if camera_key in camera_counts:
            camera_counts[camera_key] += 1
        try:
            confidence = float(event.get("confidence", 0))
            confidence_values.append(confidence * 100 if confidence <= 1 else confidence)
        except (TypeError, ValueError):
            pass

    hourly_counts = [0] * 24
    for _, stamp in recent_24:
        hours_ago = int((now - stamp).total_seconds() // 3600)
        if 0 <= hours_ago < 24:
            hourly_counts[23 - hours_ago] += 1

    seven_day = []
    for days_ago in range(6, -1, -1):
        day = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        seven_day.append({
            "day": day,
            "label": (now - timedelta(days=days_ago)).strftime("%a"),
            "count": sum(1 for _, stamp in recent_7 if stamp.strftime("%Y-%m-%d") == day),
        })

    peak_hour = max(range(24), key=lambda hour: hourly_counts[hour]) if hourly_counts else 0
    active_camera = max(camera_counts, key=camera_counts.get) if camera_counts else "1"
    today_count = sum(1 for _, stamp in valid_events if stamp.strftime("%Y-%m-%d") == today_key)
    average_confidence = round(sum(confidence_values) / len(confidence_values), 1) if confidence_values else 0

    return {
        "total_events": len(events),
        "today_count": today_count,
        "last_24_count": len(recent_24),
        "last_7_count": len(recent_7),
        "average_confidence": average_confidence,
        "peak_hour": peak_hour,
        "active_camera": int(active_camera),
        "type_counts": type_counts,
        "camera_counts": camera_counts,
        "hourly_counts": hourly_counts,
        "seven_day": seven_day,
        "mock_data": not ANALYTICS_EVENTS_FILE.exists(),
        "checked_at": now.isoformat(),
    }


@app.post("/api/analytics/natural-search")
def natural_analytics_search(request: NaturalSearchModel) -> dict:
    query = request.query.lower()
    filters: dict[str, str | int] = {}
    for event_type in ["person", "vehicle", "plate", "line_crossing", "intrusion"]:
        if event_type.replace("_", " ") in query:
            filters["event_type"] = event_type
    camera_match = re.search(r"camera\s*(\d+)", query)
    if camera_match:
        filters["camera"] = int(camera_match.group(1))
    color_match = next((color for color in ["black", "white", "silver", "red", "blue", "gray"] if color in query), None)
    if color_match:
        filters["color"] = color_match
    plate_match = re.search(r"plate\s+([a-z0-9-]{3,10})", query)
    if plate_match and plate_match.group(1) not in {"from", "with", "camera"}:
        filters["plate"] = plate_match.group(1).upper()
    return {"filters": filters, "experimental": True, "message": "Experimental search translated into structured filters."}


@app.put("/api/analytics/events/{event_id}/review")
def review_analytics_event(event_id: str, review: EventReviewModel) -> dict:
    if review.event_id != event_id:
        return {"status": "error", "message": "Event ID does not match the request."}
    reviews = load_json_file(EVENT_REVIEWS_FILE, {})
    reviews[event_id] = review.model_dump()
    try:
        save_json_file(EVENT_REVIEWS_FILE, reviews)
    except OSError as error:
        return {"status": "error", "message": f"Could not save review: {error}"}
    return {"status": "complete", "message": "Event review saved.", "review": reviews[event_id]}


def recording_start(clip: Path, camera_number: int) -> datetime | None:
    prefix = f"camera{camera_number}_"
    try:
        return datetime.strptime(
            clip.stem.removeprefix(prefix), "%Y-%m-%d_%H-%M-%S"
        )
    except ValueError:
        return None


async def build_manual_clip(
    job_id: str, camera_number: int, start_time: datetime, end_time: datetime
) -> None:
    job = clip_jobs[job_id]
    list_file = CLIPS_FOLDER / f".{job_id}.txt"
    try:
        job.update(status="working", progress=10, message="Finding recordings…")
        camera_folder = RECORDINGS_FOLDER / f"camera{camera_number}"
        candidates = []
        for source in sorted(camera_folder.glob("*.mkv")):
            source_start = recording_start(source, camera_number)
            if source_start and source_start < end_time and source_start + timedelta(minutes=5) > start_time:
                candidates.append((source_start, source))
        if not candidates:
            raise ValueError("No completed recordings cover the selected time range.")
        first_start = candidates[0][0]
        offset_seconds = max(0, (start_time - first_start).total_seconds())
        duration_seconds = (end_time - start_time).total_seconds()
        list_file.write_text(
            "".join(f"file '{source.as_posix()}'\n" for _, source in candidates),
            encoding="utf-8",
        )
        output_name = (
            f"camera{camera_number}_{start_time.strftime('%Y-%m-%d_%H-%M-%S')}"
            f"_to_{end_time.strftime('%H-%M-%S')}.mp4"
        )
        output_path = CLIPS_FOLDER / output_name
        job.update(status="working", progress=35, message="Creating video clip…")
        command = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-ss", str(offset_seconds), "-t", str(duration_seconds), "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-movflags", "+faststart",
            str(output_path),
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace")[-600:]
            raise RuntimeError(f"Video processing failed. {error_text}")
        job.update(
            status="complete",
            progress=100,
            message="Clip created successfully.",
            url=f"/recordings/clips/{quote(output_name)}",
            filename=output_name,
        )
    except (OSError, ValueError, RuntimeError) as error:
        job.update(status="error", progress=0, message=str(error))
    finally:
        list_file.unlink(missing_ok=True)


@app.post("/api/clips")
async def create_clip(request: ClipRequest) -> dict:
    start_time = request.start_time.replace(tzinfo=None)
    end_time = request.end_time.replace(tzinfo=None)
    duration = end_time - start_time
    if duration <= timedelta(0):
        return {"status": "error", "message": "End time must be after start time."}
    if duration > timedelta(hours=1):
        return {"status": "error", "message": "Manual clips are limited to one hour."}
    if end_time > datetime.now() + timedelta(minutes=1):
        return {"status": "error", "message": "The selected end time cannot be in the future."}
    job_id = uuid.uuid4().hex
    clip_jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "progress": 2,
        "message": "Clip queued…",
    }
    task = asyncio.create_task(
        build_manual_clip(job_id, request.camera, start_time, end_time)
    )
    clip_tasks.add(task)
    task.add_done_callback(clip_tasks.discard)
    return clip_jobs[job_id]


@app.get("/api/clips/{job_id}")
def clip_status(job_id: str) -> dict:
    return clip_jobs.get(
        job_id,
        {"id": job_id, "status": "error", "progress": 0, "message": "Clip job not found."},
    )



@app.get("/enterprise-readiness", response_class=HTMLResponse)
def enterprise_deployment_readiness_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Enterprise readiness",
            "enterprise-readiness",
            "manage_settings",
        )

    cloud = cloud_configuration_snapshot()
    readiness = readiness_snapshot()
    diagnostics = diagnostics_snapshot()
    backups = list_backups()
    release_checks = load_json_file(RELEASE_CHECKS_FILE, {})
    maintenance = load_json_file(MAINTENANCE_STATE_FILE, {})
    license_state = load_license_state()

    checks = [
        {
            "name": "Production environment selected",
            "ok": DEPLOYMENT_ENV == "production",
            "detail": f"Current environment: {DEPLOYMENT_ENV}",
            "critical": True,
        },
        {
            "name": "AWS region configured",
            "ok": bool(AWS_REGION),
            "detail": AWS_REGION or "AWS_REGION is missing.",
            "critical": True,
        },
        {
            "name": "Database configured",
            "ok": bool(DATABASE_URL),
            "detail": "External database configured." if DATABASE_URL else "ANYAICAM_DATABASE_URL is missing.",
            "critical": True,
        },
        {
            "name": "S3 recording bucket configured",
            "ok": bool(S3_BUCKET),
            "detail": S3_BUCKET or "ANYAICAM_S3_BUCKET is missing.",
            "critical": True,
        },
        {
            "name": "Cloud recording worker ready",
            "ok": bool(cloud.get("cloud_recording_ready")),
            "detail": str(cloud.get("cloud_recording_ready")),
            "critical": False,
        },
        {
            "name": "Secrets Manager configured",
            "ok": bool(SECRETS_MANAGER_SECRET_ID),
            "detail": SECRETS_MANAGER_SECRET_ID or "ANYAICAM_SECRETS_SECRET_ID is missing.",
            "critical": True,
        },
        {
            "name": "Public HTTPS URL configured",
            "ok": bool(PUBLIC_BASE_URL and PUBLIC_BASE_URL.startswith("https://")),
            "detail": PUBLIC_BASE_URL or "ANYAICAM_PUBLIC_URL is missing.",
            "critical": True,
        },
        {
            "name": "Secure cookies enabled",
            "ok": bool(SECURE_COOKIES),
            "detail": f"Secure cookies: {SECURE_COOKIES}",
            "critical": True,
        },
        {
            "name": "HTTPS enforcement enabled",
            "ok": bool(FORCE_HTTPS),
            "detail": f"Force HTTPS: {FORCE_HTTPS}",
            "critical": True,
        },
        {
            "name": "Backups available",
            "ok": bool(backups),
            "detail": f"{len(backups)} backup archive(s) available.",
            "critical": False,
        },
        {
            "name": "Single-worker safety",
            "ok": WEB_CONCURRENCY == 1,
            "detail": f"WEB_CONCURRENCY={WEB_CONCURRENCY}. JSON-backed local state is safest with one worker.",
            "critical": True,
        },
        {
            "name": "Enterprise license features",
            "ok": str(license_state.get("plan") or "").lower() == "enterprise",
            "detail": f"Current plan: {license_state.get('plan') or 'unknown'}",
            "critical": False,
        },
    ]

    passed = sum(bool(item["ok"]) for item in checks)
    critical_failures = sum(not item["ok"] and item["critical"] for item in checks)
    score = round((passed / max(1, len(checks))) * 100)

    check_rows = []
    for item in checks:
        icon_class = "ok" if item["ok"] else ("fail" if item["critical"] else "warn")
        icon_text = "✓" if item["ok"] else ("!" if item["critical"] else "•")
        status_text = "Ready" if item["ok"] else ("Required" if item["critical"] else "Recommended")
        check_rows.append(
            f'''<div class="enterprise-check">
              <div class="enterprise-icon {icon_class}">{icon_text}</div>
              <div><strong>{escape(item["name"])}</strong><small>{escape(str(item["detail"]))}</small></div>
              <span class="admin-command-badge {"active" if item["ok"] else "pending"}">{status_text}</span>
            </div>'''
        )

    content = f'''<header class="topbar"><div><p class="eyebrow">Enterprise phase</p><h1>Deployment readiness</h1></div><span class="pill">Read-only assessment</span></header>
    <div class="enterprise-note"><strong>Safety boundary:</strong> this page evaluates configuration and operational readiness only. It does not create AWS resources, migrate the database, change DNS, alter Stripe, or enable production traffic.</div>
    <section class="enterprise-summary" style="margin-top:16px">
      <article class="enterprise-stat"><span>Readiness score</span><strong>{score}%</strong></article>
      <article class="enterprise-stat"><span>Checks passed</span><strong>{passed}/{len(checks)}</strong></article>
      <article class="enterprise-stat"><span>Critical gaps</span><strong>{critical_failures}</strong></article>
      <article class="enterprise-stat"><span>Backups</span><strong>{len(backups)}</strong></article>
      <article class="enterprise-stat"><span>Runtime role</span><strong>{escape(RUNTIME_ROLE)}</strong></article>
      <article class="enterprise-stat"><span>App ready</span><strong>{"Yes" if readiness.get("ready") else "No"}</strong></article>
    </section>
    <section class="enterprise-grid">
      <div class="enterprise-stack">
        <article class="enterprise-card">
          <h2>Production-readiness checklist</h2>
          <div class="enterprise-progress"><span style="width:{score}%"></span></div>
          <div class="enterprise-checks">{"".join(check_rows)}</div>
        </article>
        <article class="enterprise-card">
          <h2>Recommended deployment sequence</h2>
          <div class="enterprise-checks">
            <div class="enterprise-check"><div class="enterprise-icon">1</div><div><strong>Create isolated staging</strong><small>Use a separate database, S3 prefix, Stripe test account, secrets and public URL.</small></div><span></span></div>
            <div class="enterprise-check"><div class="enterprise-icon">2</div><div><strong>Move state out of local JSON</strong><small>Migrate users, sessions, billing, onboarding, partner data and audit records into a managed database before multi-instance scaling.</small></div><span></span></div>
            <div class="enterprise-check"><div class="enterprise-icon">3</div><div><strong>Deploy edge-to-cloud securely</strong><small>Keep camera RTSP credentials on the customer edge. Send events, health and authorized recordings through encrypted outbound connections.</small></div><span></span></div>
            <div class="enterprise-check"><div class="enterprise-icon">4</div><div><strong>Enable observability</strong><small>Centralize logs, metrics, alarms, backup verification and incident-response procedures.</small></div><span></span></div>
            <div class="enterprise-check"><div class="enterprise-icon">5</div><div><strong>Run acceptance testing</strong><small>Verify authentication, portal isolation, Stripe webhooks, licenses, Cloud IDs, retention, restore and failure recovery before production launch.</small></div><span></span></div>
          </div>
        </article>
      </div>
      <aside class="enterprise-stack">
        <article class="enterprise-card">
          <h2>Cloud foundation</h2>
          <p><b>Foundation ready:</b> {"Yes" if cloud.get("cloud_foundation_ready") else "No"}</p>
          <p><b>Recording ready:</b> {"Yes" if cloud.get("cloud_recording_ready") else "No"}</p>
          <p><b>SDK available:</b> {"Yes" if cloud.get("s3_sdk_available") else "No"}</p>
          <p><b>Missing:</b> {escape(", ".join(cloud.get("missing_cloud_requirements") or []) or "None")}</p>
          <div class="enterprise-actions"><a href="/cloud-recording">Cloud recording</a><a href="/operations">Operations</a></div>
        </article>
        <article class="enterprise-card">
          <h2>Operational state</h2>
          <p><b>Release candidate:</b> {RELEASE_CANDIDATE}</p>
          <p><b>Maintenance:</b> {escape(str(maintenance.get("enabled", False)))}</p>
          <p><b>Diagnostics checked:</b> {escape(str(diagnostics.get("checked_at") or "—"))}</p>
          <p><b>Release checks:</b> {len(release_checks) if isinstance(release_checks, dict) else 0}</p>
          <div class="enterprise-actions"><a href="/release-readiness">Release readiness</a><a href="/diagnostics">Diagnostics</a></div>
        </article>
        <article class="enterprise-card">
          <h2>Enterprise tools</h2>
          <div class="enterprise-actions">
            <a href="/backup-restore">Backup & restore</a>
            <a href="/audit-logs">Audit logs</a>
            <a href="/admin-support">Support center</a>
            <a href="/license-management">Licensing</a>
            <a href="/payment-setup">Stripe setup</a>
          </div>
        </article>
      </aside>
    </section>'''

    return page_shell(
        "Enterprise readiness",
        "enterprise-readiness",
        content,
    )



@app.get("/enterprise-observability", response_class=HTMLResponse)
def enterprise_observability_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Enterprise observability",
            "enterprise-observability",
            "manage_settings",
        )

    metrics = system_metrics()
    readiness = readiness_snapshot()
    diagnostics = diagnostics_snapshot()
    audit_entries = load_audit_entries(60)
    notification_deliveries = load_notification_deliveries()
    recent_health = list(health_issues.values())
    cloud_state = dict(cloud_upload_state)

    failed_audit = [
        item for item in audit_entries
        if str(item.get("outcome") or "").lower() == "failed"
    ]
    recent_notifications = sorted(
        notification_deliveries,
        key=lambda item: str(item.get("created_at") or item.get("timestamp") or ""),
        reverse=True,
    )[:12]
    failed_notifications = [
        item for item in notification_deliveries
        if str(item.get("status") or item.get("outcome") or "").lower()
        in {"failed", "error", "undelivered"}
    ]
    cameras = camera_status().get("cameras", [])
    online_cameras = sum(bool(item.get("online")) for item in cameras)
    recording_cameras = sum(
        str(item.get("recording") or "").lower() == "running"
        for item in cameras
    )
    uptime_seconds = max(0, int((datetime.now() - STARTED_AT).total_seconds()))
    uptime_hours = uptime_seconds / 3600

    def status_badge(value: object) -> str:
        status = str(value or "unknown").strip().lower()
        badge_class = (
            "active"
            if status in {"ok", "ready", "running", "online", "success", "complete", "completed", "delivered"}
            else "failed"
            if status in {"failed", "error", "offline", "critical"}
            else "pending"
        )
        return f'<span class="admin-command-badge {badge_class}">{escape(status.replace("_", " "))}</span>'

    health_rows = []
    for item in recent_health[:12]:
        severity = str(item.get("severity") or item.get("status") or "warning").lower()
        dot_class = "fail" if severity in {"critical", "failed", "offline"} else "warn"
        health_rows.append(
            f'''<div class="obs-row">
              <span class="obs-dot {dot_class}"></span>
              <div><strong>{escape(str(item.get("title") or item.get("issue") or item.get("type") or "Health issue"))}</strong>
              <small>{escape(str(item.get("message") or item.get("detail") or item.get("last_error") or ""))}<br>
              {escape(str(item.get("timestamp") or item.get("created_at") or ""))}</small></div>
              {status_badge(severity)}
            </div>'''
        )

    audit_rows = []
    for item in audit_entries[:12]:
        outcome = str(item.get("outcome") or "unknown").lower()
        dot_class = "ok" if outcome == "success" else "fail" if outcome == "failed" else "warn"
        audit_rows.append(
            f'''<div class="obs-row">
              <span class="obs-dot {dot_class}"></span>
              <div><strong>{escape(str(item.get("action") or "Audit activity"))}</strong>
              <small>{escape(str(item.get("user_name") or item.get("user_id") or "System"))}
              · {escape(str(item.get("resource") or ""))}<br>
              {escape(str(item.get("timestamp") or ""))}</small></div>
              {status_badge(outcome)}
            </div>'''
        )

    notification_rows = []
    for item in recent_notifications:
        outcome = str(item.get("status") or item.get("outcome") or "unknown").lower()
        dot_class = "ok" if outcome in {"sent", "delivered", "success", "complete"} else "fail" if outcome in {"failed", "error"} else "warn"
        notification_rows.append(
            f'''<div class="obs-row">
              <span class="obs-dot {dot_class}"></span>
              <div><strong>{escape(str(item.get("channel") or item.get("rule_name") or "Notification"))}</strong>
              <small>{escape(str(item.get("recipient") or item.get("destination") or ""))}
              · {escape(str(item.get("event_type") or item.get("message") or ""))}<br>
              {escape(str(item.get("created_at") or item.get("timestamp") or ""))}</small></div>
              {status_badge(outcome)}
            </div>'''
        )

    content = f'''<header class="topbar"><div><p class="eyebrow">Enterprise phase</p><h1>Observability & incident response</h1></div><span class="pill">Read-only operations</span></header>
    <div class="obs-note"><strong>Operational boundary:</strong> this page consolidates existing health, audit, notification, cloud-upload and readiness information. It does not install an external monitoring service, change camera behavior, or modify production infrastructure.</div>
    <section class="obs-summary" style="margin-top:16px">
      <article class="obs-stat"><span>Uptime</span><strong>{uptime_hours:.1f}h</strong></article>
      <article class="obs-stat"><span>Cameras online</span><strong>{online_cameras}/{len(cameras)}</strong></article>
      <article class="obs-stat"><span>Recording workers</span><strong>{recording_cameras}</strong></article>
      <article class="obs-stat"><span>Active health issues</span><strong>{len(recent_health)}</strong></article>
      <article class="obs-stat"><span>Failed audit events</span><strong>{len(failed_audit)}</strong></article>
      <article class="obs-stat"><span>Failed notifications</span><strong>{len(failed_notifications)}</strong></article>
    </section>
    <section class="obs-grid">
      <div class="obs-stack">
        <article class="obs-card">
          <h2>Current health issues</h2>
          <div class="obs-list">{"".join(health_rows) or '<div class="empty">No active health issues.</div>'}</div>
        </article>
        <article class="obs-card">
          <h2>Recent audit activity</h2>
          <div class="obs-list">{"".join(audit_rows) or '<div class="empty">No audit activity.</div>'}</div>
        </article>
        <article class="obs-card">
          <h2>Notification delivery</h2>
          <div class="obs-list">{"".join(notification_rows) or '<div class="empty">No notification-delivery records.</div>'}</div>
        </article>
      </div>
      <aside class="obs-stack">
        <article class="obs-card">
          <h2>Runtime status</h2>
          <p><b>Application ready:</b> {"Yes" if readiness.get("ready") else "No"}</p>
          <p><b>Environment:</b> {escape(DEPLOYMENT_ENV)}</p>
          <p><b>Runtime role:</b> {escape(RUNTIME_ROLE)}</p>
          <p><b>Cloud upload worker:</b> {escape(str(cloud_state.get("worker_status") or "disabled"))}</p>
          <p><b>Cloud queue:</b> {escape(str(cloud_state.get("queued") or 0))} queued · {escape(str(cloud_state.get("failed") or 0))} failed</p>
          <p><b>Last cloud error:</b> {escape(str(cloud_state.get("last_error") or "None"))}</p>
          <div class="obs-actions"><a href="/operations">Operations</a><a href="/diagnostics">Diagnostics</a><a href="/cloud-recording">Cloud recording</a></div>
        </article>
        <article class="obs-card">
          <h2>Incident-response runbook</h2>
          <div class="obs-runbook">
            <div class="obs-step"><strong>Confirm the impact</strong><small>Identify the affected portal, customer, camera group, payment flow or cloud service. Avoid broad changes before the scope is known.</small></div>
            <div class="obs-step"><strong>Preserve evidence</strong><small>Save logs, audit records, timestamps, screenshots and configuration snapshots before restarting or changing services.</small></div>
            <div class="obs-step"><strong>Contain safely</strong><small>Disable only the affected integration or account. Do not weaken authentication, portal isolation or Stripe verification.</small></div>
            <div class="obs-step"><strong>Recover from a known state</strong><small>Use the latest verified backup or last working file, then run syntax, login, portal, recording and webhook tests.</small></div>
            <div class="obs-step"><strong>Document the resolution</strong><small>Record cause, impact, changes, validation results and follow-up work in the audit and support workflow.</small></div>
          </div>
        </article>
        <article class="obs-card">
          <h2>Operational tools</h2>
          <div class="obs-actions">
            <a href="/enterprise-readiness">Deployment readiness</a>
            <a href="/release-readiness">Release checks</a>
            <a href="/backup-restore">Backup & restore</a>
            <a href="/audit-logs">Audit logs</a>
            <a href="/admin-support">Support center</a>
            <a href="/api/support-bundle">Support bundle</a>
          </div>
        </article>
      </aside>
    </section>'''

    return page_shell(
        "Enterprise observability",
        "enterprise-observability",
        content,
    )



@app.get("/enterprise-dr", response_class=HTMLResponse)
def enterprise_backup_disaster_recovery_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Backup & disaster recovery",
            "enterprise-dr",
            "manage_settings",
        )

    backups = list_backups()
    backup_jobs = load_json_file(BACKUP_JOBS_FILE, [])
    restore_history = load_json_file(BACKUP_RESTORE_FILE, [])
    diagnostics = diagnostics_snapshot()
    readiness = readiness_snapshot()

    if not isinstance(backup_jobs, list):
        backup_jobs = []
    if not isinstance(restore_history, list):
        restore_history = []

    latest_backup = backups[0] if backups else {}
    latest_backup_age_hours = None
    if latest_backup.get("created_at"):
        try:
            latest_backup_age_hours = max(
                0,
                (datetime.now() - datetime.fromisoformat(str(latest_backup["created_at"]))).total_seconds() / 3600,
            )
        except (TypeError, ValueError):
            latest_backup_age_hours = None

    backup_fresh = latest_backup_age_hours is not None and latest_backup_age_hours <= 24
    restore_tested = any(
        str(item.get("status") or "").lower() in {"complete", "completed", "success"}
        for item in restore_history
    )
    offsite_ready = bool(S3_BUCKET and AWS_REGION)
    config_only = not BACKUP_INCLUDE_RECORDINGS and not BACKUP_INCLUDE_HLS

    checks = [
        ("Recent backup available", bool(backups), f"{len(backups)} backup archive(s) available."),
        ("Latest backup is less than 24 hours old", backup_fresh, f"Age: {latest_backup_age_hours:.1f} hours." if latest_backup_age_hours is not None else "No valid backup timestamp."),
        ("Restore has been tested", restore_tested, f"{len(restore_history)} restore-history record(s)."),
        ("Offsite storage configured", offsite_ready, "S3 and AWS region are configured." if offsite_ready else "Configure S3 and AWS region for offsite copies."),
        ("Backup retention configured", BACKUP_RETENTION_COUNT >= 3, f"Retention count: {BACKUP_RETENTION_COUNT}."),
        ("Recordings excluded from config backup", config_only, f"Include recordings: {BACKUP_INCLUDE_RECORDINGS}; include HLS: {BACKUP_INCLUDE_HLS}."),
    ]
    passed = sum(ok for _, ok, _ in checks)
    score = round((passed / len(checks)) * 100)

    backup_rows = []
    for item in backups[:12]:
        backup_rows.append(
            f'''<div class="dr-row">
              <div><strong>{escape(str(item.get("name") or "Backup"))}</strong>
              <small>{escape(str(item.get("created_at") or "—"))}</small></div>
              <div><strong>{escape(str(item.get("size_mb") or 0))} MB</strong><small>Archive size</small></div>
              <div class="dr-actions"><a href="{escape(str(item.get("url") or "#"), quote=True)}">Download</a></div>
            </div>'''
        )

    restore_rows = []
    for item in sorted(
        restore_history,
        key=lambda row: str(row.get("created_at") or row.get("timestamp") or ""),
        reverse=True,
    )[:10]:
        restore_rows.append(
            f'''<div class="dr-row">
              <div><strong>{escape(str(item.get("backup_id") or item.get("backup_name") or "Restore"))}</strong>
              <small>{escape(str(item.get("created_at") or item.get("timestamp") or "—"))}</small></div>
              <div><strong>{escape(str(item.get("status") or "unknown"))}</strong><small>Restore status</small></div>
              <div></div>
            </div>'''
        )

    check_rows = []
    for name, ok, detail in checks:
        check_rows.append(
            f'''<div class="dr-check"><strong>{"✓" if ok else "!"} {escape(name)}</strong><small>{escape(detail)}</small></div>'''
        )

    content = f'''<header class="topbar"><div><p class="eyebrow">Enterprise phase</p><h1>Backup & disaster recovery</h1></div><span class="pill">Recovery planning</span></header>
    <div class="dr-note"><strong>Safety boundary:</strong> this page reviews backup and recovery readiness and links to the existing backup tools. It does not automatically restore data, overwrite active configuration, or create cloud infrastructure.</div>
    <section class="dr-summary" style="margin-top:16px">
      <article class="dr-stat"><span>Recovery score</span><strong>{score}%</strong></article>
      <article class="dr-stat"><span>Backups</span><strong>{len(backups)}</strong></article>
      <article class="dr-stat"><span>Latest age</span><strong>{f"{latest_backup_age_hours:.1f}h" if latest_backup_age_hours is not None else "—"}</strong></article>
      <article class="dr-stat"><span>Restore tested</span><strong>{"Yes" if restore_tested else "No"}</strong></article>
      <article class="dr-stat"><span>Retention count</span><strong>{BACKUP_RETENTION_COUNT}</strong></article>
      <article class="dr-stat"><span>App ready</span><strong>{"Yes" if readiness.get("ready") else "No"}</strong></article>
    </section>
    <section class="dr-grid">
      <div class="dr-stack">
        <article class="dr-card">
          <h2>Available backups</h2>
          <div class="dr-list">{"".join(backup_rows) or '<div class="empty">No backup archives are available.</div>'}</div>
        </article>
        <article class="dr-card">
          <h2>Restore history</h2>
          <div class="dr-list">{"".join(restore_rows) or '<div class="empty">No restore tests have been recorded.</div>'}</div>
        </article>
      </div>
      <aside class="dr-stack">
        <article class="dr-card">
          <h2>Recovery readiness</h2>
          <div class="dr-progress"><span style="width:{score}%"></span></div>
          <div class="dr-checklist">{"".join(check_rows)}</div>
        </article>
        <article class="dr-card">
          <h2>Recommended recovery sequence</h2>
          <div class="dr-checklist">
            <div class="dr-check"><strong>1. Preserve the active system</strong><small>Capture logs, diagnostics, audit records, and a fresh configuration backup before making recovery changes.</small></div>
            <div class="dr-check"><strong>2. Validate the backup</strong><small>Check archive integrity, manifest, version, and expected configuration files before restoration.</small></div>
            <div class="dr-check"><strong>3. Restore into staging first</strong><small>Never test an unverified backup directly against the production system.</small></div>
            <div class="dr-check"><strong>4. Test critical paths</strong><small>Verify authentication, portal isolation, Stripe webhooks, Cloud IDs, recording, playback, and notifications.</small></div>
            <div class="dr-check"><strong>5. Document recovery</strong><small>Record the incident, selected backup, restore result, validation, and follow-up actions.</small></div>
          </div>
        </article>
        <article class="dr-card">
          <h2>Recovery tools</h2>
          <div class="dr-actions">
            <a href="/backup-restore">Backup & restore</a>
            <a href="/operations">Operations</a>
            <a href="/diagnostics">Diagnostics</a>
            <a href="/enterprise-observability">Observability</a>
            <a href="/api/support-bundle">Support bundle</a>
          </div>
          <p class="health-detail" style="margin-top:10px">Diagnostics checked: {escape(str(diagnostics.get("checked_at") or "—"))}</p>
        </article>
      </aside>
    </section>'''

    return page_shell(
        "Backup & disaster recovery",
        "enterprise-dr",
        content,
    )



INCIDENT_STATUSES = {"open", "investigating", "contained", "monitoring", "resolved", "closed"}
INCIDENT_SEVERITIES = {"low", "medium", "high", "critical"}


def load_incidents() -> list[dict]:
    data = load_json_file(INCIDENTS_FILE, [])
    return data if isinstance(data, list) else []


def save_incidents(items: list[dict]) -> None:
    save_json_file(INCIDENTS_FILE, items[-5000:])


def load_observability_retention() -> dict:
    return load_json_file(
        OBSERVABILITY_RETENTION_FILE,
        {
            "audit_days": 365,
            "security_days": 365,
            "health_days": 180,
            "incident_days": 730,
        },
    )


def save_observability_retention(settings: dict) -> None:
    save_json_file(OBSERVABILITY_RETENTION_FILE, settings)


def observability_admin(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user


def login_security_snapshot() -> dict:
    failures = load_json_file(LOGIN_FAILURES_FILE, {})
    rate_limits = load_json_file(RATE_LIMITS_FILE, {})
    failure_count = 0
    locked_accounts = 0
    if isinstance(failures, dict):
        for record in failures.values():
            if isinstance(record, dict):
                failure_count += int(record.get("count") or record.get("attempts") or 0)
                if record.get("locked_until"):
                    locked_accounts += 1
    rate_limit_events = 0
    if isinstance(rate_limits, dict):
        for record in rate_limits.values():
            if isinstance(record, dict):
                rate_limit_events += int(record.get("count") or record.get("hits") or 0)
    return {
        "failed_logins": failure_count,
        "locked_accounts": locked_accounts,
        "rate_limit_events": rate_limit_events,
        "failure_records": failures if isinstance(failures, dict) else {},
        "rate_limit_records": rate_limits if isinstance(rate_limits, dict) else {},
    }


@app.get("/enterprise-incident", response_class=HTMLResponse)
def enterprise_incident_response_page(request: Request) -> str:
    user = observability_admin(request)
    audit_entries = load_audit_entries(500)
    incidents = load_incidents()
    retention = load_observability_retention()
    security = login_security_snapshot()
    health_items = list(health_issues.values())
    notifications = load_notification_deliveries()
    metrics = system_metrics()
    readiness = readiness_snapshot()

    open_incidents = [item for item in incidents if str(item.get("status") or "").lower() not in {"resolved", "closed"}]
    critical_incidents = [item for item in open_incidents if str(item.get("severity") or "").lower() == "critical"]
    failed_audit = [item for item in audit_entries if str(item.get("outcome") or "").lower() == "failed"]
    failed_notifications = [item for item in notifications if str(item.get("status") or item.get("outcome") or "").lower() in {"failed", "error", "undelivered"}]

    incident_rows = []
    for item in sorted(incidents, key=lambda value: str(value.get("updated_at") or value.get("created_at") or ""), reverse=True):
        incident_rows.append(
            f'''<div class="ir-row" data-search="{escape((" ".join([str(item.get("title") or ""),str(item.get("category") or ""),str(item.get("status") or ""),str(item.get("assigned_to") or "")])).lower(), quote=True)}" data-severity="{escape(str(item.get("severity") or "medium"), quote=True)}" data-status="{escape(str(item.get("status") or "open"), quote=True)}">
              <span class="ir-dot {escape(str(item.get("severity") or "medium"), quote=True)}"></span>
              <div><strong>{escape(str(item.get("title") or "Incident"))}</strong>
              <small>{escape(str(item.get("category") or "operations"))} · {escape(str(item.get("status") or "open").replace("_", " "))} · Assigned: {escape(str(item.get("assigned_to") or "Unassigned"))}<br>
              {escape(str(item.get("description") or ""))}</small></div>
              <button class="incident-edit" data-id="{escape(str(item.get("id") or ""), quote=True)}">Manage</button>
            </div>'''
        )

    audit_rows = []
    for item in audit_entries[:80]:
        searchable = " ".join([
            str(item.get("action") or ""),
            str(item.get("user_name") or item.get("user_id") or ""),
            str(item.get("resource") or ""),
            str(item.get("outcome") or ""),
        ]).lower()
        audit_rows.append(
            f'''<div class="ir-row audit-log-row" data-search="{escape(searchable, quote=True)}" data-outcome="{escape(str(item.get("outcome") or "unknown").lower(), quote=True)}">
              <span class="ir-dot {"high" if str(item.get("outcome") or "").lower()=="failed" else "low"}"></span>
              <div><strong>{escape(str(item.get("action") or "Audit activity"))}</strong>
              <small>{escape(str(item.get("user_name") or item.get("user_id") or "System"))} · {escape(str(item.get("resource") or ""))}<br>{escape(str(item.get("timestamp") or ""))}</small></div>
              <span class="admin-command-badge {"failed" if str(item.get("outcome") or "").lower()=="failed" else "active"}">{escape(str(item.get("outcome") or "unknown"))}</span>
            </div>'''
        )

    content = f'''<header class="topbar"><div><p class="eyebrow">Enterprise phase</p><h1>Observability & incident response</h1></div><span class="pill">Centralized operations</span></header>
    <div class="ir-note"><strong>Privacy boundary:</strong> this center contains logs, health, security, notifications, incidents and performance data only. It does not expose customer video or camera credentials.</div>
    <section class="ir-summary" style="margin-top:16px">
      <article class="ir-stat"><span>Open incidents</span><strong>{len(open_incidents)}</strong></article>
      <article class="ir-stat"><span>Critical incidents</span><strong>{len(critical_incidents)}</strong></article>
      <article class="ir-stat"><span>Failed logins</span><strong>{security["failed_logins"]}</strong></article>
      <article class="ir-stat"><span>Locked accounts</span><strong>{security["locked_accounts"]}</strong></article>
      <article class="ir-stat"><span>Rate-limit events</span><strong>{security["rate_limit_events"]}</strong></article>
      <article class="ir-stat"><span>Failed audit events</span><strong>{len(failed_audit)}</strong></article>
      <article class="ir-stat"><span>Failed notifications</span><strong>{len(failed_notifications)}</strong></article>
    </section>
    <section class="ir-grid">
      <div class="ir-stack">
        <article class="ir-card">
          <h2>Incident management</h2>
          <div class="ir-toolbar">
            <input id="incident-search" placeholder="Search incidents">
            <select id="incident-severity-filter"><option value="">All severities</option><option>low</option><option>medium</option><option>high</option><option>critical</option></select>
            <select id="incident-status-filter"><option value="">All statuses</option>{"".join(f'<option>{status}</option>' for status in sorted(INCIDENT_STATUSES))}</select>
            <button id="incident-new">New incident</button>
          </div>
          <div class="ir-list" id="incident-list">{"".join(incident_rows) or '<div class="empty">No incidents recorded.</div>'}</div>
        </article>
        <article class="ir-card">
          <h2>Centralized audit logs</h2>
          <div class="ir-toolbar">
            <input id="audit-search" placeholder="Search user, action, resource, or outcome">
            <select id="audit-outcome-filter"><option value="">All outcomes</option><option value="success">Success</option><option value="failed">Failed</option></select>
          </div>
          <div class="ir-list">{"".join(audit_rows) or '<div class="empty">No audit records found.</div>'}</div>
        </article>
      </div>
      <aside class="ir-stack">
        <article class="ir-card ir-editor">
          <h2>Incident editor</h2>
          <form id="incident-form">
            <input id="incident-id" type="hidden">
            <label>Title<input id="incident-title" required></label>
            <label>Severity<select id="incident-severity"><option>low</option><option selected>medium</option><option>high</option><option>critical</option></select></label>
            <label>Category<select id="incident-category"><option>operations</option><option>security</option><option>authentication</option><option>billing</option><option>cloud</option><option>camera health</option></select></label>
            <label>Status<select id="incident-status">{"".join(f'<option>{status}</option>' for status in sorted(INCIDENT_STATUSES))}</select></label>
            <label>Assigned to<input id="incident-assigned"></label>
            <label>Description<textarea id="incident-description"></textarea></label>
            <label>Resolution<textarea id="incident-resolution"></textarea></label>
            <div class="ir-actions"><button class="primary" type="submit">Save incident</button><button id="incident-resolve" type="button">Resolve</button></div>
            <div class="ir-feedback" id="incident-feedback">Create or select an incident.</div>
          </form>
        </article>
        <article class="ir-card">
          <h2>Performance & health</h2>
          <div class="ir-list">
            <div class="ir-metric"><span>Application ready<small>Current readiness state</small></span><strong>{"Yes" if readiness.get("ready") else "No"}</strong></div>
            <div class="ir-metric"><span>CPU<small>System utilization</small></span><strong>{escape(str(metrics.get("cpu_percent") or metrics.get("cpu") or "—"))}</strong></div>
            <div class="ir-metric"><span>Memory<small>System utilization</small></span><strong>{escape(str(metrics.get("memory_percent") or metrics.get("memory") or "—"))}</strong></div>
            <div class="ir-metric"><span>Disk<small>System utilization</small></span><strong>{escape(str(metrics.get("disk_percent") or metrics.get("disk") or "—"))}</strong></div>
            <div class="ir-metric"><span>Active health issues<small>Current unresolved items</small></span><strong>{len(health_items)}</strong></div>
          </div>
        </article>
        <article class="ir-card ir-editor">
          <h2>Data retention policies</h2>
          <form id="retention-form">
            <label>Audit logs (days)<input id="retention-audit" type="number" min="7" max="3650" value="{retention.get("audit_days",365)}"></label>
            <label>Security logs (days)<input id="retention-security" type="number" min="7" max="3650" value="{retention.get("security_days",365)}"></label>
            <label>Health history (days)<input id="retention-health" type="number" min="7" max="3650" value="{retention.get("health_days",180)}"></label>
            <label>Incidents (days)<input id="retention-incident" type="number" min="30" max="3650" value="{retention.get("incident_days",730)}"></label>
            <button class="primary" type="submit">Save retention</button>
            <div class="ir-feedback" id="retention-feedback">Retention settings control policy only; automatic deletion is not enabled by this phase.</div>
          </form>
        </article>
        <article class="ir-card">
          <h2>Exportable reports</h2>
          <div class="ir-actions">
            <a href="/api/observability/export?report=audit">Audit CSV</a>
            <a href="/api/observability/export?report=incidents">Incidents CSV</a>
            <a href="/api/observability/export?report=security">Security CSV</a>
            <a href="/api/observability/export?report=health">Health CSV</a>
          </div>
        </article>
      </aside>
    </section>'''

    scripts = '''
    <script>
    const incidents=JSON.parse(document.getElementById('incident-data').textContent||'[]');
    const f=id=>document.getElementById(id);
    function filterIncidents(){
      const query=f('incident-search').value.trim().toLowerCase();
      const severity=f('incident-severity-filter').value;
      const status=f('incident-status-filter').value;
      document.querySelectorAll('#incident-list .ir-row').forEach(row=>{
        row.hidden=Boolean((query&&!row.dataset.search.includes(query))||(severity&&row.dataset.severity!==severity)||(status&&row.dataset.status!==status));
      });
    }
    function filterAudit(){
      const query=f('audit-search').value.trim().toLowerCase();
      const outcome=f('audit-outcome-filter').value;
      document.querySelectorAll('.audit-log-row').forEach(row=>{
        row.hidden=Boolean((query&&!row.dataset.search.includes(query))||(outcome&&row.dataset.outcome!==outcome));
      });
    }
    f('incident-search').oninput=filterIncidents;f('incident-severity-filter').onchange=filterIncidents;f('incident-status-filter').onchange=filterIncidents;
    f('audit-search').oninput=filterAudit;f('audit-outcome-filter').onchange=filterAudit;

    function clearIncident(){
      f('incident-id').value='';f('incident-title').value='';f('incident-severity').value='medium';f('incident-category').value='operations';f('incident-status').value='open';f('incident-assigned').value='';f('incident-description').value='';f('incident-resolution').value='';
      f('incident-feedback').className='ir-feedback';f('incident-feedback').textContent='Ready to create a new incident.';
    }
    f('incident-new').onclick=clearIncident;
    document.querySelectorAll('.incident-edit').forEach(button=>button.onclick=()=>{
      const item=incidents.find(entry=>String(entry.id)===String(button.dataset.id));
      if(!item)return;
      f('incident-id').value=item.id||'';f('incident-title').value=item.title||'';f('incident-severity').value=item.severity||'medium';f('incident-category').value=item.category||'operations';f('incident-status').value=item.status||'open';f('incident-assigned').value=item.assigned_to||'';f('incident-description').value=item.description||'';f('incident-resolution').value=item.resolution||'';
      f('incident-feedback').textContent='Incident loaded.';
    });
    async function saveIncident(statusOverride=null){
      const editing=Boolean(f('incident-id').value);
      const payload={title:f('incident-title').value,severity:f('incident-severity').value,category:f('incident-category').value,status:statusOverride||f('incident-status').value,assigned_to:f('incident-assigned').value,description:f('incident-description').value,resolution:f('incident-resolution').value,source:'manual'};
      const url=editing?`/api/incidents/${encodeURIComponent(f('incident-id').value)}`:'/api/incidents';
      const response=await fetch(url,{method:editing?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result=await response.json();if(!response.ok)throw new Error(result.detail||result.message||'Could not save incident.');
      location.reload();
    }
    f('incident-form').onsubmit=event=>{event.preventDefault();saveIncident().catch(error=>{f('incident-feedback').className='ir-feedback error';f('incident-feedback').textContent=error.message})};
    f('incident-resolve').onclick=()=>saveIncident('resolved').catch(error=>{f('incident-feedback').className='ir-feedback error';f('incident-feedback').textContent=error.message});

    f('retention-form').onsubmit=async event=>{
      event.preventDefault();
      const response=await fetch('/api/observability/retention',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({audit_days:Number(f('retention-audit').value),security_days:Number(f('retention-security').value),health_days:Number(f('retention-health').value),incident_days:Number(f('retention-incident').value)})});
      const result=await response.json();
      f('retention-feedback').className='ir-feedback '+(response.ok?'success':'error');
      f('retention-feedback').textContent=result.message||result.detail||'Retention settings saved.';
    };
    </script>'''

    incident_data = '<script id="incident-data" type="application/json">' + escape(json.dumps(incidents)) + '</script>'
    return page_shell("Observability & incident response", "enterprise-incident", content, incident_data + scripts)


@app.post("/api/incidents")
def create_incident(request: Request, payload: IncidentCreateModel) -> dict:
    user = observability_admin(request)
    if payload.severity not in INCIDENT_SEVERITIES or payload.status not in INCIDENT_STATUSES:
        raise HTTPException(status_code=400, detail="Unsupported incident severity or status.")
    incident = {
        "id": uuid.uuid4().hex[:14],
        **payload.model_dump(),
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "created_by": user.get("display_name") or user.get("email") or "Administrator",
        "resolution": "",
    }
    items = load_incidents()
    items.append(incident)
    save_incidents(items)
    record_audit(request, "incident.created", "incident:" + incident["id"], incident["title"])
    return {"status": "complete", "message": "Incident created.", "incident": incident}


@app.put("/api/incidents/{incident_id}")
def update_incident(incident_id: str, request: Request, payload: IncidentUpdateModel) -> dict:
    observability_admin(request)
    items = load_incidents()
    incident = next((item for item in items if str(item.get("id")) == incident_id), None)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")
    updates = payload.model_dump(exclude_none=True)
    if updates.get("severity") and updates["severity"] not in INCIDENT_SEVERITIES:
        raise HTTPException(status_code=400, detail="Unsupported incident severity.")
    if updates.get("status") and updates["status"] not in INCIDENT_STATUSES:
        raise HTTPException(status_code=400, detail="Unsupported incident status.")
    incident.update(updates)
    incident["updated_at"] = datetime.now().isoformat()
    if incident.get("status") in {"resolved", "closed"} and not incident.get("resolved_at"):
        incident["resolved_at"] = datetime.now().isoformat()
    save_incidents(items)
    record_audit(request, "incident.updated", "incident:" + incident_id, str(incident.get("status") or "updated"))
    return {"status": "complete", "message": "Incident updated.", "incident": incident}


@app.put("/api/observability/retention")
def update_observability_retention(request: Request, payload: ObservabilityRetentionModel) -> dict:
    observability_admin(request)
    settings = payload.model_dump()
    settings["updated_at"] = datetime.now().isoformat()
    save_observability_retention(settings)
    record_audit(request, "observability.retention_updated", "observability:retention", json.dumps(settings))
    return {"status": "complete", "message": "Retention policy settings saved.", "settings": settings}


@app.get("/api/observability/export")
def export_observability_report(request: Request, report: str = "audit") -> Response:
    observability_admin(request)
    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output)

    if report == "incidents":
        writer.writerow(["id", "title", "severity", "category", "status", "assigned_to", "created_at", "updated_at", "resolution"])
        for item in load_incidents():
            writer.writerow([item.get("id"), item.get("title"), item.get("severity"), item.get("category"), item.get("status"), item.get("assigned_to"), item.get("created_at"), item.get("updated_at"), item.get("resolution")])
    elif report == "security":
        snapshot = login_security_snapshot()
        writer.writerow(["metric", "value"])
        writer.writerow(["failed_logins", snapshot["failed_logins"]])
        writer.writerow(["locked_accounts", snapshot["locked_accounts"]])
        writer.writerow(["rate_limit_events", snapshot["rate_limit_events"]])
    elif report == "health":
        writer.writerow(["title", "severity", "message", "timestamp"])
        for item in health_issues.values():
            writer.writerow([item.get("title") or item.get("issue"), item.get("severity") or item.get("status"), item.get("message") or item.get("detail"), item.get("timestamp") or item.get("created_at")])
    else:
        writer.writerow(["timestamp", "user", "action", "resource", "outcome", "detail"])
        for item in load_audit_entries(5000):
            writer.writerow([item.get("timestamp"), item.get("user_name") or item.get("user_id"), item.get("action"), item.get("resource"), item.get("outcome"), item.get("detail")])

    file_name = "anyaicam_" + report + "_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )



@app.get("/production-config", response_class=HTMLResponse)
def production_environment_configuration_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Production configuration",
            "production-config",
            "manage_settings",
        )

    cloud = cloud_configuration_snapshot()
    readiness = readiness_snapshot()
    backups = list_backups()
    license_state = load_license_state()

    checks = [
        {"name": "Production environment mode", "ok": DEPLOYMENT_ENV == "production", "detail": f"ANYAICAM_ENV={DEPLOYMENT_ENV}", "critical": True},
        {"name": "Public HTTPS URL", "ok": bool(PUBLIC_BASE_URL and PUBLIC_BASE_URL.startswith("https://")), "detail": PUBLIC_BASE_URL or "ANYAICAM_PUBLIC_URL is not configured.", "critical": True},
        {"name": "Secure cookies", "ok": bool(SECURE_COOKIES), "detail": f"ANYAICAM_SECURE_COOKIES={SECURE_COOKIES}", "critical": True},
        {"name": "HTTPS enforcement", "ok": bool(FORCE_HTTPS), "detail": f"ANYAICAM_FORCE_HTTPS={FORCE_HTTPS}", "critical": True},
        {"name": "External database", "ok": bool(DATABASE_URL), "detail": "Configured." if DATABASE_URL else "ANYAICAM_DATABASE_URL is missing.", "critical": True},
        {"name": "AWS region", "ok": bool(AWS_REGION), "detail": AWS_REGION or "AWS_REGION is missing.", "critical": True},
        {"name": "S3 bucket", "ok": bool(S3_BUCKET), "detail": S3_BUCKET or "ANYAICAM_S3_BUCKET is missing.", "critical": True},
        {"name": "Secrets Manager", "ok": bool(SECRETS_MANAGER_SECRET_ID), "detail": "Configured." if SECRETS_MANAGER_SECRET_ID else "ANYAICAM_SECRETS_SECRET_ID is missing.", "critical": True},
        {"name": "Stripe live configuration", "ok": bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET), "detail": "Secret key and webhook secret are configured." if (STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET) else "Stripe secret key or webhook secret is missing.", "critical": True},
        {"name": "Stripe price IDs", "ok": bool(STRIPE_PRICE_IDS.get("starter") and STRIPE_PRICE_IDS.get("professional") and STRIPE_PRICE_IDS.get("enterprise")), "detail": "Starter, Professional and Enterprise price IDs are configured.", "critical": True},
        {"name": "Email delivery", "ok": bool(SMTP_HOST and SMTP_FROM), "detail": "SMTP host and sender are configured." if (SMTP_HOST and SMTP_FROM) else "SMTP host or sender is missing.", "critical": True},
        {"name": "Cloud recording foundation", "ok": bool(cloud.get("cloud_recording_ready")), "detail": "Cloud recording worker is ready." if cloud.get("cloud_recording_ready") else "Cloud recording is not fully configured.", "critical": False},
        {"name": "Verified backups", "ok": bool(backups), "detail": f"{len(backups)} backup archive(s) available.", "critical": True},
        {"name": "Production license", "ok": str(license_state.get("status") or "").lower() in {"active", "current"}, "detail": f"Plan: {license_state.get('plan') or 'unknown'}; status: {license_state.get('status') or 'unknown'}", "critical": False},
        {"name": "Single-worker local-state safety", "ok": WEB_CONCURRENCY == 1 or bool(DATABASE_URL), "detail": f"WEB_CONCURRENCY={WEB_CONCURRENCY}. External database configured: {bool(DATABASE_URL)}", "critical": True},
    ]

    passed = sum(bool(item["ok"]) for item in checks)
    critical_gaps = sum(not item["ok"] and item["critical"] for item in checks)
    score = round((passed / max(1, len(checks))) * 100)

    rows = []
    for item in checks:
        icon_class = "ok" if item["ok"] else ("fail" if item["critical"] else "warn")
        icon = "✓" if item["ok"] else ("!" if item["critical"] else "•")
        label = "Ready" if item["ok"] else ("Required" if item["critical"] else "Recommended")
        rows.append(
            f'<div class="prod-check"><div class="prod-icon {icon_class}">{icon}</div>'
            f'<div><strong>{escape(item["name"])}</strong><small>{escape(str(item["detail"]))}</small></div>'
            f'<span class="admin-command-badge {"active" if item["ok"] else "pending"}">{label}</span></div>'
        )

    environment_template = (
        "# Production environment template\n"
        "ANYAICAM_ENV=production\n"
        "ANYAICAM_PUBLIC_URL=https://your-domain.example\n"
        "ANYAICAM_SECURE_COOKIES=true\n"
        "ANYAICAM_FORCE_HTTPS=true\n"
        "ANYAICAM_DATABASE_URL=postgresql://...\n"
        "AWS_REGION=us-east-1\n"
        "ANYAICAM_S3_BUCKET=...\n"
        "ANYAICAM_SECRETS_SECRET_ID=...\n"
        "ANYAICAM_STRIPE_SECRET_KEY=...\n"
        "ANYAICAM_STRIPE_WEBHOOK_SECRET=...\n"
        "ANYAICAM_STRIPE_PRICE_STARTER=...\n"
        "ANYAICAM_STRIPE_PRICE_PROFESSIONAL=...\n"
        "ANYAICAM_STRIPE_PRICE_ENTERPRISE=...\n"
        "ANYAICAM_SMTP_HOST=...\n"
        "ANYAICAM_SMTP_FROM=...\n"
        "WEB_CONCURRENCY=1\n"
    )

    content = f"""<header class="topbar"><div><p class="eyebrow">Production phase</p><h1>Production environment configuration</h1></div><span class="pill">Validation only</span></header>
    <div class="prod-note"><strong>Safety boundary:</strong> this page validates configuration but never displays secret values, creates AWS resources, changes DNS, or writes production environment variables.</div>
    <section class="prod-summary" style="margin-top:16px">
      <article class="prod-stat"><span>Configuration score</span><strong>{score}%</strong></article>
      <article class="prod-stat"><span>Checks passed</span><strong>{passed}/{len(checks)}</strong></article>
      <article class="prod-stat"><span>Critical gaps</span><strong>{critical_gaps}</strong></article>
      <article class="prod-stat"><span>Backups</span><strong>{len(backups)}</strong></article>
      <article class="prod-stat"><span>Environment</span><strong>{escape(DEPLOYMENT_ENV)}</strong></article>
      <article class="prod-stat"><span>App ready</span><strong>{"Yes" if readiness.get("ready") else "No"}</strong></article>
    </section>
    <section class="prod-grid">
      <div class="prod-stack">
        <article class="prod-card"><h2>Production configuration checklist</h2><div class="prod-checks">{"".join(rows)}</div></article>
        <article class="prod-card"><h2>Deployment order</h2><div class="prod-checks">
          <div class="prod-check"><div class="prod-icon">1</div><div><strong>Prepare staging</strong><small>Use separate database, S3 bucket or prefix, Stripe test mode, secrets and public URL.</small></div><span></span></div>
          <div class="prod-check"><div class="prod-icon">2</div><div><strong>Configure managed services</strong><small>Database, S3, Secrets Manager, email delivery, HTTPS termination and centralized logging.</small></div><span></span></div>
          <div class="prod-check"><div class="prod-icon">3</div><div><strong>Load production secrets</strong><small>Inject them through the deployment platform or Secrets Manager. Never commit them to Git.</small></div><span></span></div>
          <div class="prod-check"><div class="prod-icon">4</div><div><strong>Run migrations and smoke tests</strong><small>Validate authentication, role isolation, Stripe webhooks, Cloud IDs, recording, playback, alerts and backups.</small></div><span></span></div>
          <div class="prod-check"><div class="prod-icon">5</div><div><strong>Freeze feature changes</strong><small>Only release-blocking corrections are permitted after the production launch checklist begins.</small></div><span></span></div>
        </div></article>
      </div>
      <aside class="prod-stack">
        <article class="prod-card"><h2>Environment template</h2><div class="prod-code">{escape(environment_template)}</div><div class="prod-note" style="margin-top:10px">This template contains placeholders only. Do not paste real production secrets into ChatGPT or commit them to the repository.</div></article>
        <article class="prod-card"><h2>Production tools</h2><div class="prod-actions">
          <a href="/enterprise-readiness">Enterprise readiness</a>
          <a href="/enterprise-observability">Observability</a>
          <a href="/enterprise-dr">Backup & recovery</a>
          <a href="/enterprise-incident">Incidents & logs</a>
          <a href="/payment-setup">Stripe setup</a>
          <a href="/release-readiness">Release readiness</a>
        </div></article>
      </aside>
    </section>"""

    return page_shell("Production configuration", "production-config", content)



@app.get("/launch-readiness", response_class=HTMLResponse)
def final_production_launch_readiness_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Launch readiness",
            "launch-readiness",
            "manage_settings",
        )

    readiness = readiness_snapshot()
    diagnostics = diagnostics_snapshot()
    cloud = cloud_configuration_snapshot()
    backups = list_backups()
    audit_entries = load_audit_entries(300)
    incidents = load_incidents()
    accounts = list(load_billing_accounts().values())
    payment_sessions = list(load_payment_sessions().values())
    notifications = load_notification_deliveries()
    cameras = camera_status().get("cameras", [])
    license_state = load_license_state()

    failed_audit = [item for item in audit_entries if str(item.get("outcome") or "").lower() == "failed"]
    open_incidents = [item for item in incidents if str(item.get("status") or "").lower() not in {"resolved", "closed"}]
    critical_incidents = [item for item in open_incidents if str(item.get("severity") or "").lower() == "critical"]
    failed_notifications = [item for item in notifications if str(item.get("status") or item.get("outcome") or "").lower() in {"failed", "error", "undelivered"}]
    completed_payments = [item for item in payment_sessions if str(item.get("payment_status") or "").lower() in {"paid", "complete", "completed"}]
    online_cameras = sum(bool(item.get("online")) for item in cameras)
    recording_cameras = sum(str(item.get("recording") or "").lower() == "running" for item in cameras)

    launch_users = load_users()
    launch_roles = {
        str(item.get("role") or "").strip().lower()
        for item in launch_users
        if item.get("enabled", True)
    }
    role_checks = [
        bool({"administrator", "support_admin", "admin"} & launch_roles),
        bool({"customer_owner", "customer_viewer"} & launch_roles),
        bool({"partner_admin", "partner_sales", "installer"} & launch_roles),
    ]

    checks = [
        {"group": "End-to-end", "name": "Application readiness", "ok": bool(readiness.get("ready")), "detail": str(readiness.get("detail") or readiness.get("reason") or "Readiness snapshot complete."), "critical": True},
        {"group": "End-to-end", "name": "Diagnostics completed", "ok": bool(diagnostics.get("checked_at")), "detail": f"Last checked: {diagnostics.get('checked_at') or 'Never'}", "critical": True},
        {"group": "Roles", "name": "Customer, administrator and partner roles exist", "ok": all(role_checks), "detail": "Required portal role definitions are present.", "critical": True},
        {"group": "Roles", "name": "Portal isolation audit coverage", "ok": bool(audit_entries), "detail": f"{len(audit_entries)} recent audit entries available.", "critical": True},
        {"group": "Security", "name": "HTTPS and secure cookies", "ok": bool(FORCE_HTTPS and SECURE_COOKIES), "detail": f"Force HTTPS: {FORCE_HTTPS}; secure cookies: {SECURE_COOKIES}", "critical": True},
        {"group": "Security", "name": "Origin protection configured", "ok": bool(ALLOWED_ORIGINS), "detail": f"{len(ALLOWED_ORIGINS)} allowed origin(s).", "critical": True},
        {"group": "Security", "name": "No critical open incidents", "ok": len(critical_incidents) == 0, "detail": f"{len(critical_incidents)} critical open incident(s).", "critical": True},
        {"group": "Security", "name": "Failed audit events reviewed", "ok": len(failed_audit) == 0, "detail": f"{len(failed_audit)} failed audit event(s) in recent history.", "critical": False},
        {"group": "Payments", "name": "Stripe production secrets configured", "ok": bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET), "detail": "Stripe secret key and webhook secret are configured.", "critical": True},
        {"group": "Payments", "name": "Stripe product prices configured", "ok": bool(STRIPE_PRICE_IDS.get("starter") and STRIPE_PRICE_IDS.get("professional") and STRIPE_PRICE_IDS.get("enterprise")), "detail": "All three production price IDs are configured.", "critical": True},
        {"group": "Payments", "name": "Controlled transaction evidence", "ok": bool(completed_payments), "detail": f"{len(completed_payments)} completed payment session(s) recorded.", "critical": True},
        {"group": "Email", "name": "SMTP delivery configured", "ok": bool(SMTP_HOST and SMTP_FROM), "detail": "SMTP host and sender are configured.", "critical": True},
        {"group": "Email", "name": "Notification delivery healthy", "ok": len(failed_notifications) == 0, "detail": f"{len(failed_notifications)} failed notification delivery record(s).", "critical": False},
        {"group": "Cloud", "name": "Cloud foundation ready", "ok": bool(cloud.get("cloud_foundation_ready")), "detail": "Cloud foundation readiness snapshot.", "critical": True},
        {"group": "Cloud", "name": "Cloud recording ready", "ok": bool(cloud.get("cloud_recording_ready")), "detail": "Cloud recording readiness snapshot.", "critical": False},
        {"group": "Cameras", "name": "At least one camera is online", "ok": online_cameras > 0, "detail": f"{online_cameras} of {len(cameras)} camera(s) online.", "critical": False},
        {"group": "Cameras", "name": "Recording workers active", "ok": recording_cameras > 0, "detail": f"{recording_cameras} recording worker(s) running.", "critical": False},
        {"group": "Backup", "name": "Production backup available", "ok": bool(backups), "detail": f"{len(backups)} backup archive(s) available.", "critical": True},
        {"group": "Backup", "name": "Restore workflow exists", "ok": bool(BACKUP_RESTORE_FILE), "detail": "Restore history and workflow storage are configured.", "critical": True},
        {"group": "Performance", "name": "External database configured", "ok": bool(DATABASE_URL), "detail": "Managed database configured." if DATABASE_URL else "External database is not configured.", "critical": True},
        {"group": "Performance", "name": "Worker model safe", "ok": WEB_CONCURRENCY == 1 or bool(DATABASE_URL), "detail": f"WEB_CONCURRENCY={WEB_CONCURRENCY}; external database: {bool(DATABASE_URL)}", "critical": True},
        {"group": "Licensing", "name": "Production license active", "ok": str(license_state.get("status") or "").lower() in {"active", "current"}, "detail": f"Plan: {license_state.get('plan') or 'unknown'}; status: {license_state.get('status') or 'unknown'}", "critical": True},
        {"group": "Operations", "name": "No unresolved release-blocking incidents", "ok": len(open_incidents) == 0, "detail": f"{len(open_incidents)} open incident(s).", "critical": False},
        {"group": "Operations", "name": "Customer billing accounts present", "ok": bool(accounts), "detail": f"{len(accounts)} billing account(s) available for validation.", "critical": False},
    ]

    passed = sum(bool(item["ok"]) for item in checks)
    critical_failures = [item for item in checks if not item["ok"] and item["critical"]]
    warnings = [item for item in checks if not item["ok"] and not item["critical"]]
    score = round((passed / max(1, len(checks))) * 100)
    go_live = len(critical_failures) == 0

    groups = {}
    for item in checks:
        groups.setdefault(item["group"], []).append(item)

    grouped_html = []
    for group_name, group_checks in groups.items():
        rows = []
        for item in group_checks:
            icon_class = "ok" if item["ok"] else ("fail" if item["critical"] else "warn")
            icon = "✓" if item["ok"] else ("!" if item["critical"] else "•")
            label = "Passed" if item["ok"] else ("Blocker" if item["critical"] else "Warning")
            rows.append(
                f'<div class="launch-check"><div class="launch-icon {icon_class}">{icon}</div>'
                f'<div><strong>{escape(item["name"])}</strong><small>{escape(str(item["detail"]))}</small></div>'
                f'<span class="admin-command-badge {"active" if item["ok"] else "pending"}">{label}</span></div>'
            )
        grouped_html.append(f'<article class="launch-card"><h2>{escape(group_name)}</h2><div class="launch-checks">{"".join(rows)}</div></article>')

    blocker_rows = "".join(
        f'<div class="launch-check"><div class="launch-icon fail">!</div><div><strong>{escape(item["name"])}</strong><small>{escape(str(item["detail"]))}</small></div><span class="admin-command-badge failed">Blocker</span></div>'
        for item in critical_failures
    )
    warning_rows = "".join(
        f'<div class="launch-check"><div class="launch-icon warn">•</div><div><strong>{escape(item["name"])}</strong><small>{escape(str(item["detail"]))}</small></div><span class="admin-command-badge pending">Warning</span></div>'
        for item in warnings
    )

    content = f"""<header class="topbar"><div><p class="eyebrow">Final production phase</p><h1>Launch readiness</h1></div><span class="pill">Go / no-go</span></header>
    <div class="launch-note"><strong>Release boundary:</strong> this page evaluates launch readiness. It does not perform a live Stripe transaction, change production secrets, alter DNS, migrate infrastructure, or approve launch automatically.</div>
    <section class="launch-summary" style="margin-top:16px">
      <article class="launch-stat"><span>Readiness score</span><strong>{score}%</strong></article>
      <article class="launch-stat"><span>Checks passed</span><strong>{passed}/{len(checks)}</strong></article>
      <article class="launch-stat"><span>Release blockers</span><strong>{len(critical_failures)}</strong></article>
      <article class="launch-stat"><span>Warnings</span><strong>{len(warnings)}</strong></article>
      <article class="launch-stat"><span>Open incidents</span><strong>{len(open_incidents)}</strong></article>
      <article class="launch-stat"><span>Backups</span><strong>{len(backups)}</strong></article>
      <article class="launch-stat"><span>Completed payments</span><strong>{len(completed_payments)}</strong></article>
    </section>
    <section class="launch-grid">
      <div class="launch-stack">
        {"".join(grouped_html)}
      </div>
      <aside class="launch-stack">
        <article class="launch-card">
          <div class="launch-verdict {"go" if go_live else "no-go"}">
            <strong>{"GO" if go_live else "NO-GO"}</strong>
            <span>{"All critical launch checks passed." if go_live else f"{len(critical_failures)} critical blocker(s) must be resolved before launch."}</span>
          </div>
          <div class="launch-progress"><span style="width:{score}%"></span></div>
        </article>
        <article class="launch-card">
          <h2>Release blockers</h2>
          <div class="launch-checks">{blocker_rows or '<div class="empty">No critical release blockers.</div>'}</div>
        </article>
        <article class="launch-card">
          <h2>Non-blocking warnings</h2>
          <div class="launch-checks">{warning_rows or '<div class="empty">No warnings.</div>'}</div>
        </article>
        <article class="launch-card">
          <h2>Controlled go-live sequence</h2>
          <div class="launch-runbook">
            <div class="launch-step"><strong>Load live production values</strong><small>Add real values to the deployment environment or Secrets Manager. Never place secrets in source code.</small></div>
            <div class="launch-step"><strong>Run one live transaction</strong><small>Use a controlled customer account. Verify Stripe payment, webhook, invoice, license activation, cancellation or refund.</small></div>
            <div class="launch-step"><strong>Verify communication</strong><small>Confirm activation, temporary-password, order-confirmation, billing, reminder and partner-commission emails.</small></div>
            <div class="launch-step"><strong>Create final backup</strong><small>Capture the final production configuration and verify the staging restore before traffic is enabled.</small></div>
            <div class="launch-step"><strong>Freeze feature development</strong><small>Only release-blocking fixes are permitted until launch and post-launch validation are complete.</small></div>
            <div class="launch-step"><strong>Approve and monitor launch</strong><small>Enable production traffic with rollback available. Watch payments, incidents, health, logs and onboarding closely.</small></div>
          </div>
        </article>
        <article class="launch-card">
          <h2>Launch tools</h2>
          <div class="launch-actions">
            <a href="/production-config">Production configuration</a>
            <a href="/enterprise-incident">Incidents & logs</a>
            <a href="/enterprise-dr">Backup & recovery</a>
            <a href="/payment-setup">Stripe setup</a>
            <a href="/admin-support">Support center</a>
            <a href="/release-readiness">Release checks</a>
          </div>
        </article>
      </aside>
    </section>"""

    return page_shell(
        "Launch readiness",
        "launch-readiness",
        content,
    )



@app.get("/go-live", response_class=HTMLResponse)
def go_live_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Go Live",
            "go-live",
            "manage_settings",
        )

    readiness = readiness_snapshot()
    cloud = cloud_configuration_snapshot()
    backups = list_backups()
    incidents = load_incidents()
    payment_sessions = list(load_payment_sessions().values())
    notifications = load_notification_deliveries()

    open_critical_incidents = [
        item for item in incidents
        if str(item.get("status") or "").lower() not in {"resolved", "closed"}
        and str(item.get("severity") or "").lower() == "critical"
    ]
    completed_payments = [
        item for item in payment_sessions
        if str(item.get("payment_status") or "").lower()
        in {"paid", "complete", "completed"}
    ]
    failed_notifications = [
        item for item in notifications
        if str(item.get("status") or item.get("outcome") or "").lower()
        in {"failed", "error", "undelivered"}
    ]

    checks = [
        {
            "name": "Application readiness",
            "ok": bool(readiness.get("ready")),
            "detail": "Core readiness checks passed."
            if readiness.get("ready")
            else "One or more core readiness checks are incomplete.",
        },
        {
            "name": "Production environment",
            "ok": DEPLOYMENT_ENV == "production",
            "detail": f"ANYAICAM_ENV={DEPLOYMENT_ENV}",
        },
        {
            "name": "Public HTTPS URL",
            "ok": bool(PUBLIC_BASE_URL and PUBLIC_BASE_URL.startswith("https://")),
            "detail": PUBLIC_BASE_URL or "Public URL is not configured.",
        },
        {
            "name": "Secure cookies and HTTPS",
            "ok": bool(SECURE_COOKIES and FORCE_HTTPS),
            "detail": f"Secure cookies: {SECURE_COOKIES}; force HTTPS: {FORCE_HTTPS}",
        },
        {
            "name": "Stripe live secrets",
            "ok": bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET),
            "detail": "Stripe key and webhook secret are configured."
            if STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET
            else "Stripe key or webhook secret is missing.",
        },
        {
            "name": "Stripe prices",
            "ok": all(STRIPE_PRICE_IDS.values()),
            "detail": "Starter, Professional, and Enterprise price IDs are configured."
            if all(STRIPE_PRICE_IDS.values())
            else "One or more Stripe price IDs are missing.",
        },
        {
            "name": "Controlled payment evidence",
            "ok": bool(completed_payments),
            "detail": f"{len(completed_payments)} completed payment session(s) recorded.",
        },
        {
            "name": "Cloud foundation",
            "ok": bool(cloud.get("cloud_foundation_ready")),
            "detail": "AWS/cloud foundation is ready."
            if cloud.get("cloud_foundation_ready")
            else "Cloud foundation is incomplete.",
        },
        {
            "name": "Backup available",
            "ok": bool(backups),
            "detail": f"{len(backups)} backup archive(s) available.",
        },
        {
            "name": "No critical incidents",
            "ok": not open_critical_incidents,
            "detail": f"{len(open_critical_incidents)} critical open incident(s).",
        },
        {
            "name": "Notification delivery",
            "ok": not failed_notifications,
            "detail": f"{len(failed_notifications)} failed delivery record(s).",
        },
    ]

    ready_to_launch = all(item["ok"] for item in checks)
    passed = sum(item["ok"] for item in checks)
    score = round((passed / max(1, len(checks))) * 100)

    rows = []
    for item in checks:
        state = "ok" if item["ok"] else "fail"
        icon = "✓" if item["ok"] else "!"
        rows.append(
            f'<div class="launch-check">'
            f'<div class="launch-icon {state}">{icon}</div>'
            f'<div><strong>{escape(item["name"])}</strong>'
            f'<small>{escape(str(item["detail"]))}</small></div>'
            f'<span class="admin-command-badge {"active" if item["ok"] else "failed"}">'
            f'{"Passed" if item["ok"] else "Blocker"}</span></div>'
        )

    content = f"""
    <header class="topbar">
      <div><p class="eyebrow">Production control</p><h1>Go Live</h1></div>
      <span class="pill">Master administrator</span>
    </header>

    <div class="launch-note">
      <strong>Safety boundary:</strong>
      This page does not expose secrets, modify DNS, charge a customer, or turn on
      production traffic automatically. It gives the master administrator a final
      release decision and controlled runbook.
    </div>

    <section class="launch-summary" style="margin-top:16px">
      <article class="launch-stat"><span>Launch score</span><strong>{score}%</strong></article>
      <article class="launch-stat"><span>Checks passed</span><strong>{passed}/{len(checks)}</strong></article>
      <article class="launch-stat"><span>Environment</span><strong>{escape(DEPLOYMENT_ENV)}</strong></article>
      <article class="launch-stat"><span>Backups</span><strong>{len(backups)}</strong></article>
      <article class="launch-stat"><span>Payments</span><strong>{len(completed_payments)}</strong></article>
      <article class="launch-stat"><span>Critical incidents</span><strong>{len(open_critical_incidents)}</strong></article>
      <article class="launch-stat"><span>Verdict</span><strong>{"GO" if ready_to_launch else "NO-GO"}</strong></article>
    </section>

    <section class="launch-grid">
      <div class="launch-stack">
        <article class="launch-card">
          <h2>Final release gates</h2>
          <div class="launch-checks">{"".join(rows)}</div>
        </article>
      </div>

      <aside class="launch-stack">
        <article class="launch-card">
          <div class="launch-verdict {"go" if ready_to_launch else "no-go"}">
            <strong>{"GO" if ready_to_launch else "NO-GO"}</strong>
            <span>
              {"All final release gates passed."
               if ready_to_launch
               else "Resolve every blocker before enabling production traffic."}
            </span>
          </div>
          <div class="launch-progress"><span style="width:{score}%"></span></div>
        </article>

        <article class="launch-card">
          <h2>Final launch runbook</h2>
          <div class="launch-runbook">
            <div class="launch-step"><strong>Freeze the build</strong><small>Back up the release and stop adding non-critical features.</small></div>
            <div class="launch-step"><strong>Load secrets privately</strong><small>Add AWS, Stripe, SMTP, database, and portal secrets through the deployment environment.</small></div>
            <div class="launch-step"><strong>Run controlled validation</strong><small>Test every role, one payment, one webhook, one notification, cameras, playback, and backup restore.</small></div>
            <div class="launch-step"><strong>Publish the secure portal</strong><small>Enable the production HTTPS URL and verify signin.html reaches the VMS login.</small></div>
            <div class="launch-step"><strong>Enable production traffic</strong><small>Proceed only after the verdict is GO and rollback remains available.</small></div>
            <div class="launch-step"><strong>Monitor the first 24 hours</strong><small>Watch incidents, failed logins, payments, webhooks, email delivery, cameras, and storage.</small></div>
          </div>
        </article>

        <article class="launch-card">
          <h2>Related pages</h2>
          <div class="launch-actions">
            <a href="/production-config">Production configuration</a>
            <a href="/launch-readiness">Launch readiness</a>
            <a href="/release-readiness">Release checks</a>
            <a href="/enterprise-incident">Incidents & logs</a>
            <a href="/enterprise-dr">Backup & recovery</a>
          </div>
        </article>
      </aside>
    </section>
    """

    return page_shell("Go Live", "go-live", content)


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    camera_cards = "".join(
        f"""<article class="camera-card">
        <div class="camera-view">
            <div class="camera-placeholder" id="placeholder{n}">
                <span class="signal">◌</span>
                <strong>Waiting for stream</strong>
                <span>The camera will reconnect automatically.</span>
            </div>
            <span class="live-badge">LIVE</span>
            <video id="camera{n}" autoplay muted controls playsinline aria-label="Camera {n} live stream"></video>
            <div class="analytics-overlay" id="overlay{n}">Analytics overlay</div>
        </div>
        <div class="camera-meta">
            <span class="camera-name">Camera {n}</span>
            <span class="camera-state" id="state{n}">Connecting…</span>
        </div>
        <div class="camera-tools polished" aria-label="Camera {n} controls">
            <button class="camera-action" type="button" title="Enter fullscreen" onclick="fullscreenCamera({n})">
                <span class="camera-action-icon">⛶</span><span>Fullscreen</span>
            </button>
            <button class="camera-action" id="pause{n}" type="button" title="Pause or resume this live view" onclick="toggleLivePlayback({n})">
                <span class="camera-action-icon">Ⅱ</span><span id="pause-label{n}">Pause</span>
            </button>
            <button class="camera-action" type="button" title="Save a snapshot from the current live frame" onclick="captureSnapshot({n})">
                <span class="camera-action-icon">▣</span><span>Snapshot</span>
            </button>
            <a class="camera-action" title="Open recordings for Camera {n}" href="/playback?camera={n}">
                <span class="camera-action-icon">▶</span><span>Playback</span>
            </a>
            <a class="camera-action" title="Open the dedicated Camera {n} page" href="/camera/{n}">
                <span class="camera-action-icon">↗</span><span>Open</span>
            </a>
            <button class="camera-action" id="audio{n}" type="button" title="Mute or unmute browser audio" onclick="toggleCameraAudio({n})">
                <span class="camera-action-icon">◖</span><span id="audio-label{n}">Unmute</span>
            </button>
            <a class="camera-action" title="Open camera and recording settings" href="/settings">
                <span class="camera-action-icon">⚙</span><span>Settings</span>
            </a>
            <button class="camera-action" id="analytics{n}" type="button" title="Show or hide the analytics overlay" onclick="toggleAnalytics({n})">
                <span class="camera-action-icon">◇</span><span>Analytics</span>
            </button>
        </div>
        <div class="camera-control-note">Live controls affect only this browser view. Audio requires an enabled camera microphone; recording continues in the background.</div>
        </article>"""
        for n in range(1, CAMERA_COUNT + 1)
    )
    content = f"""<header class="topbar live-page-header"><div><p class="eyebrow">Live</p><h1>Cameras</h1></div><div class="clock" id="clock"></div></header><div class="section-head live-camera-head"><div><h2>My cameras</h2></div><div class="layout-controls" aria-label="Grid layout"><button class="layout-button" data-layout="1" title="1-camera grid">1</button><button class="layout-button active" data-layout="4" title="4-camera grid">4</button><button class="layout-button" data-layout="9" title="9-camera grid">9</button><button class="layout-button" data-layout="16" title="16-camera grid">16</button></div></div><section class="camera-grid" id="camera-grid" data-layout="4">{camera_cards}</section>"""
    scripts = """<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>
const clock=document.getElementById('clock');function tick(){clock.textContent=new Intl.DateTimeFormat(undefined,{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date())}tick();setInterval(tick,30000);
function setState(n,text,ready=false){const state=document.getElementById(`state${n}`);state.textContent=text;state.classList.toggle('ready',ready);document.getElementById(`placeholder${n}`).style.display=ready?'none':'block'}
function captureSnapshot(n){const video=document.getElementById(`camera${n}`);if(video.readyState<2||!video.videoWidth){comingSoon(`Camera ${n} has no live frame to capture`);return}try{const canvas=document.createElement('canvas'),context=canvas.getContext('2d'),now=new Date();canvas.width=video.videoWidth;canvas.height=video.videoHeight;context.drawImage(video,0,0,canvas.width,canvas.height);const stamp=`Camera ${n} · ${now.toLocaleDateString()} ${now.toLocaleTimeString()}`;context.font=`600 ${Math.max(18,Math.round(canvas.width/45))}px system-ui`;const padding=16,textWidth=context.measureText(stamp).width,boxHeight=Math.max(48,Math.round(canvas.height/12));context.fillStyle='rgba(0,0,0,.68)';context.fillRect(0,canvas.height-boxHeight,Math.min(canvas.width,textWidth+padding*2),boxHeight);context.fillStyle='#fff';context.textBaseline='middle';context.fillText(stamp,padding,canvas.height-boxHeight/2);canvas.toBlob(blob=>{if(!blob){comingSoon('Snapshot failed');return}const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`camera${n}_${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}_${String(now.getHours()).padStart(2,'0')}-${String(now.getMinutes()).padStart(2,'0')}-${String(now.getSeconds()).padStart(2,'0')}.png`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);comingSoon(`Camera ${n} snapshot saved`)},'image/png')}catch(error){comingSoon(`Snapshot error: ${error.message}`)}}
async function captureSnapshot(n){const video=document.getElementById(`camera${n}`);if(video.readyState<2||!video.videoWidth){showToast(`Camera ${n} has no live frame to capture.`);return}showToast(`Saving Camera ${n} snapshot…`);try{const canvas=document.createElement('canvas'),context=canvas.getContext('2d'),now=new Date();canvas.width=video.videoWidth;canvas.height=video.videoHeight;context.drawImage(video,0,0,canvas.width,canvas.height);const stamp=`Home · Camera ${n} · ${now.toLocaleDateString()} ${now.toLocaleTimeString()}`,fontSize=Math.max(18,Math.round(canvas.width/45)),boxHeight=Math.max(48,Math.round(canvas.height/12));context.font=`600 ${fontSize}px system-ui`;context.fillStyle='rgba(0,0,0,.72)';context.fillRect(0,canvas.height-boxHeight,Math.min(canvas.width,context.measureText(stamp).width+32),boxHeight);context.fillStyle='#fff';context.textBaseline='middle';context.fillText(stamp,16,canvas.height-boxHeight/2);const imageData=canvas.toDataURL('image/png'),response=await fetch('/api/snapshots',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({camera:n,site:'home',image_data:imageData})}),result=await response.json();if(result.status!=='complete')throw new Error(result.message||'Snapshot could not be saved.');const link=document.createElement('a');link.href=result.url;link.download=result.filename;link.click();showToast('Snapshot saved and downloaded.')}catch(error){showToast(`Snapshot error: ${error.message}`)}}
function fullscreenCamera(n){
    const video=document.getElementById(`camera${n}`);
    const target=video.closest('.camera-view')||video;
    if(document.fullscreenElement){document.exitFullscreen?.();return}
    target.requestFullscreen?.().catch(()=>showToast('Fullscreen is not available in this browser.'));
}
function toggleLivePlayback(n){
    const video=document.getElementById(`camera${n}`);
    const label=document.getElementById(`pause-label${n}`);
    const button=document.getElementById(`pause${n}`);
    if(video.paused){
        video.play().then(()=>{label.textContent='Pause';button.classList.remove('active');showToast(`Camera ${n} live view resumed.`)}).catch(()=>showToast(`Camera ${n} could not resume yet.`));
    }else{
        video.pause();
        label.textContent='Resume';
        button.classList.add('active');
        showToast(`Camera ${n} live view paused. Recording is still running.`);
    }
}
function toggleCameraAudio(n){
    const video=document.getElementById(`camera${n}`);
    const label=document.getElementById(`audio-label${n}`);
    const button=document.getElementById(`audio${n}`);
    video.muted=!video.muted;
    label.textContent=video.muted?'Unmute':'Mute';
    button.classList.toggle('active',!video.muted);
    if(video.muted){
        showToast(`Camera ${n} audio muted.`);
    }else{
        video.volume=1;
        video.play().catch(()=>{});
        showToast(`Camera ${n} audio enabled. If silent, verify that the camera microphone is enabled.`);
    }
}
function toggleAnalytics(n){
    const overlay=document.getElementById(`overlay${n}`);
    const button=document.getElementById(`analytics${n}`);
    overlay.classList.toggle('visible');
    button.classList.toggle('active',overlay.classList.contains('visible'));
    showToast(`Camera ${n} analytics overlay ${overlay.classList.contains('visible')?'shown':'hidden'}.`);
}
function connectCamera(n){const video=document.getElementById(`camera${n}`),source=`/static/hls/camera${n}.m3u8`;video.addEventListener('playing',()=>{setState(n,'Streaming',true);const label=document.getElementById(`pause-label${n}`);const button=document.getElementById(`pause${n}`);if(label)label.textContent='Pause';if(button)button.classList.remove('active')});video.addEventListener('pause',()=>{const label=document.getElementById(`pause-label${n}`);const button=document.getElementById(`pause${n}`);if(label)label.textContent='Resume';if(button)button.classList.add('active')});video.addEventListener('waiting',()=>setState(n,'Reconnecting…'));if(window.Hls&&Hls.isSupported()){const hls=new Hls({liveSyncDurationCount:2,liveMaxLatencyDurationCount:5});hls.loadSource(source);hls.attachMedia(video);hls.on(Hls.Events.ERROR,(_,data)=>{if(data.fatal)setState(n,'Waiting for camera')})}else if(video.canPlayType('application/vnd.apple.mpegurl')){video.src=source}else{setState(n,'Browser not supported')}}for(let n=1;n<=4;n++)connectCamera(n);
const grid=document.getElementById('camera-grid');const savedLayout=localStorage.getItem('camera-layout')||'4';function setLayout(layout){grid.dataset.layout=layout;document.querySelectorAll('.layout-button').forEach(button=>button.classList.toggle('active',button.dataset.layout===layout));localStorage.setItem('camera-layout',layout)}document.querySelectorAll('.layout-button').forEach(button=>button.addEventListener('click',()=>setLayout(button.dataset.layout)));setLayout(savedLayout);
</script>"""
    return page_shell("Live view", "live", content, scripts)



@app.get("/api/dashboard/intelligence")
def dashboard_intelligence_api() -> dict:
    now = datetime.now()
    today = now.date()

    motion_events = load_motion_events()
    events_today = []
    hourly_counts = [0] * 24
    camera_counts = {camera_number: 0 for camera_number in range(1, CAMERA_COUNT + 1)}
    for event in motion_events:
        raw_time = event.get("start_time") or event.get("timestamp")
        try:
            occurred_at = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            if occurred_at.tzinfo is not None:
                occurred_at = occurred_at.astimezone().replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        if occurred_at.date() == today:
            events_today.append(event)
            hourly_counts[occurred_at.hour] += 1
            camera_number = event.get("camera")
            if camera_number in camera_counts:
                camera_counts[camera_number] += 1

    analytic_items = analytics_events()
    analytic_mock = bool(analytic_items) and all(item.get("mock", False) for item in analytic_items)
    analytic_counts = {
        "person": 0,
        "vehicle": 0,
        "plate": 0,
        "intrusion": 0,
        "line_crossing": 0,
    }
    for event in analytic_items:
        event_type = str(event.get("event_type", ""))
        if event_type in analytic_counts:
            analytic_counts[event_type] += 1

    alerts = in_app_alerts(limit=100).get("alerts", [])
    unread_alerts = [alert for alert in alerts if not alert.get("read", False)]
    active_issues = list(health_issues.values())
    alert_rows = []
    for issue in active_issues:
        alert_rows.append({
            "message": issue.get("message", "System issue"),
            "severity": issue.get("severity", "warning"),
            "type": issue.get("type", "system"),
            "camera": issue.get("camera"),
            "timestamp": issue.get("timestamp", now.isoformat()),
            "href": f'/camera/{issue.get("camera")}' if issue.get("camera") else "/dashboard",
        })
    for alert in unread_alerts[:8]:
        alert_rows.append({
            "message": alert.get("message", "New alert"),
            "severity": "warning",
            "type": alert.get("event_type", "alert"),
            "camera": alert.get("camera"),
            "timestamp": alert.get("timestamp", now.isoformat()),
            "href": f'/camera/{alert.get("camera")}' if alert.get("camera") else "/alerts",
        })

    most_active_camera = max(camera_counts, key=camera_counts.get) if any(camera_counts.values()) else None
    return {
        "alerts": alert_rows[:8],
        "unread_alert_count": len(unread_alerts),
        "active_issue_count": len(active_issues),
        "events_today": len(events_today),
        "most_active_camera": most_active_camera,
        "most_active_camera_events": camera_counts.get(most_active_camera, 0) if most_active_camera else 0,
        "analytics": analytic_counts,
        "analytics_mock": analytic_mock,
        "hourly_activity": hourly_counts,
        "checked_at": now.isoformat(),
    }


@app.get("/camera-health", response_class=HTMLResponse)
def camera_health_page() -> str:
    rows = []
    for camera_number in range(1, CAMERA_COUNT + 1):
        host = escape(os.environ.get(f"CAMERA{camera_number}_HOST", "Not configured"))
        path = escape(
            os.environ.get(
                f"CAMERA{camera_number}_PATH",
                "/Streaming/Channels/101",
            )
        )
        rows.append(
            f"""<tr id="camera-health-row-{camera_number}">
              <td><strong>Camera {camera_number}</strong></td>
              <td class="health-code">{host}</td>
              <td class="health-code">{path}</td>
              <td><span class="health-state warning" id="camera-health-live-{camera_number}">Checking</span></td>
              <td><span class="health-state warning" id="camera-health-recording-{camera_number}">Checking</span></td>
              <td><span class="health-state warning" id="camera-health-ai-{camera_number}">Checking</span></td>
              <td id="camera-health-age-{camera_number}">—</td>
              <td id="camera-health-reconnects-{camera_number}">{camera_reconnect_counts.get(camera_number, 0)}</td>
              <td><a class="download" href="/camera/{camera_number}">Open</a></td>
            </tr>"""
        )

    content = f"""<header class="topbar">
      <div><p class="eyebrow">Operations</p><h1>Camera health</h1></div>
      <button class="action-button" id="refresh-camera-health" type="button">Refresh now</button>
    </header>

    <section class="camera-health-summary">
      <div class="camera-health-stat"><span>Cameras online</span><strong id="health-online-count">—</strong></div>
      <div class="camera-health-stat"><span>Recording workers</span><strong id="health-recording-count">—</strong></div>
      <div class="camera-health-stat"><span>Active health issues</span><strong id="health-issue-count">—</strong></div>
      <div class="camera-health-stat"><span>Storage free</span><strong id="health-storage-free">—</strong></div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div><h2>Camera status</h2><div class="health-refresh-note" id="camera-health-updated">Refreshing every 15 seconds</div></div>
        <a class="download" href="/operations">Open system operations</a>
      </div>
      <div class="camera-health-table-wrap">
        <table class="camera-health-table">
          <thead><tr>
            <th>Camera</th><th>IP address</th><th>Stream path</th><th>Live</th>
            <th>Recording</th><th>AI</th><th>Last stream update</th><th>Reconnects</th><th></th>
          </tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>

    <section class="panel" style="margin-top:18px">
      <div class="panel-head"><div><h2>Active issues</h2><div class="health-detail">Offline streams, recording failures, reconnect problems, CPU, and storage warnings.</div></div></div>
      <div class="health-issues-list" id="camera-health-issues"><div class="empty">Checking health issues…</div></div>
    </section>"""

    scripts = """
    <script>
    const CAMERA_COUNT=__CAMERA_COUNT__;

    function stateClass(value,onlineValues=[]){
      return onlineValues.includes(String(value).toLowerCase())?'online':
        ['retrying','connecting','starting','waiting','warning'].includes(String(value).toLowerCase())?'warning':'offline';
    }
    function updateState(element,value,onlineValues=[]){
      const normalized=String(value??'unknown');
      element.className='health-state '+stateClass(normalized,onlineValues);
      element.textContent=normalized.replaceAll('_',' ');
    }
    function issueMarkup(issue){
      const severity=String(issue.severity||'warning').toLowerCase();
      return `<article class="health-issue-card">
        <div><strong>${issue.message||'Health issue'}</strong>
        <div class="health-detail">${String(issue.type||'issue').replaceAll('_',' ')}${issue.camera?` · Camera ${issue.camera}`:' · System'}</div></div>
        <span class="health-state ${severity==='critical'?'offline':'warning'}">${severity}</span>
      </article>`;
    }

    async function refreshCameraHealth(){
      const button=document.getElementById('refresh-camera-health');
      button.disabled=true;
      button.textContent='Refreshing…';
      try{
        const [cameraResponse,issueResponse,metricResponse,aiResponse]=await Promise.all([
          fetch('/api/cameras/status',{cache:'no-store'}),
          fetch('/api/health/issues',{cache:'no-store'}),
          fetch('/api/system/metrics',{cache:'no-store'}),
          fetch('/api/ai/status',{cache:'no-store'}).catch(()=>null)
        ]);
        const cameraData=await cameraResponse.json();
        const issueData=await issueResponse.json();
        const metrics=await metricResponse.json();
        const aiData=aiResponse?await aiResponse.json():{};

        const cameras=cameraData.cameras||[];
        const issues=issueData.issues||[];
        const aiStates=aiData.cameras||aiData.status||[];

        let onlineCount=0,recordingCount=0;
        cameras.forEach(camera=>{
          const number=Number(camera.camera);
          if(camera.online)onlineCount++;
          if(String(camera.recording).toLowerCase()==='running')recordingCount++;

          updateState(
            document.getElementById(`camera-health-live-${number}`),
            camera.online?'online':camera.stream,
            ['online','running']
          );
          updateState(
            document.getElementById(`camera-health-recording-${number}`),
            camera.recording,
            ['running','online']
          );
          const ai=Array.isArray(aiStates)
            ? aiStates.find(item=>Number(item.camera)===number)
            : aiStates[String(number)]||{};
          updateState(
            document.getElementById(`camera-health-ai-${number}`),
            ai?.status||'unknown',
            ['running','online','ready']
          );
          const age=camera.last_stream_update_seconds;
          document.getElementById(`camera-health-age-${number}`).textContent=
            age===null||age===undefined?'No recent update':`${age}s ago`;
          if(camera.reconnects!==undefined){
            document.getElementById(`camera-health-reconnects-${number}`).textContent=camera.reconnects;
          }
        });

        document.getElementById('health-online-count').textContent=`${onlineCount}/${cameras.length||CAMERA_COUNT}`;
        document.getElementById('health-recording-count').textContent=`${recordingCount}/${cameras.length||CAMERA_COUNT}`;
        document.getElementById('health-issue-count').textContent=issues.length;
        document.getElementById('health-storage-free').textContent=`${metrics.storage_free_gb??'—'} GB`;

        document.getElementById('camera-health-issues').innerHTML=
          issues.length?issues.map(issueMarkup).join(''):'<div class="empty">No active health issues.</div>';
        document.getElementById('camera-health-updated').textContent=
          'Last refreshed '+new Date().toLocaleTimeString()+' · automatic every 15 seconds';
      }catch(error){
        showToast('Could not refresh camera health: '+error.message);
      }finally{
        button.disabled=false;
        button.textContent='Refresh now';
      }
    }

    document.getElementById('refresh-camera-health').addEventListener('click',refreshCameraHealth);
    refreshCameraHealth();
    setInterval(refreshCameraHealth,15000);
    </script>
    """.replace("__CAMERA_COUNT__", str(CAMERA_COUNT))
    return page_shell("Camera health", "camera-health", content, scripts)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    clips = sorted(
        RECORDINGS_FOLDER.rglob("*.mkv"),
        key=lambda clip: clip.stat().st_mtime,
        reverse=True,
    )
    try:
        disk = shutil.disk_usage(RECORDINGS_FOLDER)
        used_percent = round((disk.used / disk.total) * 100) if disk.total else 0
        free_gb = disk.free / (1024**3)
        storage_text = f"{free_gb:.1f} GB free"
    except OSError:
        used_percent, storage_text = 0, "Storage unavailable"

    camera_cards = "".join(
        f"""<a class="dashboard-camera-card" href="/camera/{camera_number}" id="dashboard-camera-{camera_number}">
        <div class="dashboard-camera-preview">
            <video id="dashboard-video-{camera_number}" muted playsinline preload="metadata"></video>
            <div class="dashboard-camera-placeholder" id="dashboard-placeholder-{camera_number}">
                <span class="signal">◉</span>
                <strong>Connecting to Camera {camera_number}</strong>
                <small>Live preview will appear when the stream is ready.</small>
            </div>
            <span class="dashboard-live-badge wait" id="dashboard-live-{camera_number}">Connecting</span>
            <span class="dashboard-rec-badge" id="dashboard-rec-{camera_number}">● REC</span>
        </div>
        <div class="dashboard-camera-info">
            <div>
                <div class="dashboard-camera-name">Camera {camera_number}</div>
                <div class="dashboard-camera-detail" id="dashboard-detail-{camera_number}">Checking stream and recording status…</div>
            </div>
            <span class="dashboard-open-icon" aria-hidden="true">↗</span>
        </div>
        </a>"""
        for camera_number in range(1, CAMERA_COUNT + 1)
    )

    recent_events = load_motion_events()
    recent_events.sort(
        key=lambda event: event.get("start_time", event.get("timestamp", "")),
        reverse=True,
    )

    def render_event_card(event: dict) -> str:
        camera_number = escape(str(event.get("camera", "?")))
        event_type = escape(str(event.get("event_type", "motion")).replace("_", " ").title())
        timestamp = escape(str(event.get("start_time", event.get("timestamp", ""))))
        confidence = event.get("confidence")
        confidence_text = f"{confidence}% confidence" if confidence is not None else "Event detected"
        thumbnail = event.get("thumbnail")
        linked_recording = event.get("linked_recording") or "/playback"
        image = (
            f'<img src="{escape(str(thumbnail))}" alt="{event_type} on Camera {camera_number}" loading="lazy">'
            if thumbnail
            else '<div class="dashboard-event-fallback"><strong>No thumbnail</strong><span>Preview unavailable</span></div>'
        )
        snapshot_action = (
            f'<a class="dashboard-event-action" href="{escape(str(thumbnail))}" target="_blank" rel="noopener">Snapshot</a>'
            if thumbnail
            else '<span class="dashboard-event-action">No snapshot</span>'
        )
        return (
            '<article class="dashboard-event-card">'
            f'<div class="dashboard-event-image">{image}'
            f'<span class="dashboard-event-type">{event_type}</span>'
            f'<span class="dashboard-event-camera">Camera {camera_number}</span></div>'
            '<div class="dashboard-event-body">'
            f'<div class="dashboard-event-title">{event_type} detected</div>'
            f'<div class="dashboard-event-meta"><span>{escape(confidence_text)}</span><time datetime="{timestamp}">{timestamp[:19].replace("T", " ")}</time></div>'
            '<div class="dashboard-event-actions">'
            f'<a class="dashboard-event-action primary" href="{escape(str(linked_recording))}">Play recording</a>'
            f'{snapshot_action}</div></div></article>'
        )

    event_cards = "".join(render_event_card(event) for event in recent_events[:6])
    if not event_cards:
        event_cards = '<div class="empty dashboard-event-empty">Recent events will appear here after motion is detected.</div>'

    issue_rows = "".join(
        f'<div class="health-row"><div><div class="health-name">{escape(issue["message"])}</div>'
        f'<div class="health-detail">{escape(issue["type"].replace("_", " ").title())}</div></div>'
        f'<span class="pill wait">{escape(issue["severity"].title())}</span></div>'
        for issue in health_issues.values()
    ) or '<div class="empty">No active health issues.</div>'

    content = f"""<header class="topbar"><div><p class="eyebrow">System overview</p><h1>Dashboard</h1></div><a class="action-button" href="/">Open live view</a></header>
    <section class="today-activity-card" aria-labelledby="today-activity-title">
        <div class="today-activity-intro">
            <p class="eyebrow">Today</p>
            <h2 id="today-activity-title">Today’s activity</h2>
            <p>A quick view of camera activity, detections, alerts, and system status. Select any metric to open the related page.</p>
        </div>
        <div class="today-activity-grid">
            <a class="today-activity-link" href="/events"><strong id="today-events-count">—</strong><span>Events</span><small>Motion and detections</small></a>
            <a class="today-activity-link" href="/analytics"><strong id="today-people-count">—</strong><span>People</span><small>AI detections</small></a>
            <a class="today-activity-link" href="/analytics"><strong id="today-vehicles-count">—</strong><span>Vehicles</span><small>Cars, trucks and more</small></a>
            <a class="today-activity-link" href="/alerts"><strong id="today-alerts-count">—</strong><span>Alerts</span><small>Unread and active issues</small></a>
            <a class="today-activity-link" href="/"><strong id="today-cameras-count">—</strong><span>Cameras online</span><small>Live camera status</small></a>
        </div>
    </section>
    <section class="summary">
        <div class="stat"><span class="stat-label">System health</span><span class="stat-value"><span class="dot"></span>VMS running</span></div>
        <div class="stat"><span class="stat-label">Recording</span><span class="stat-value">Continuous</span></div>
        <div class="stat"><span class="stat-label">Saved clips</span><span class="stat-value">{len(clips)}</span></div>
        <div class="stat"><span class="stat-label">CPU</span><span class="stat-value" id="cpu-metric">Checking…</span></div>
        <div class="stat"><span class="stat-label">Memory</span><span class="stat-value" id="memory-metric">Checking…</span></div>
        <div class="stat"><span class="stat-label">Storage available</span><span class="stat-value" id="storage-metric">{storage_text}</span></div>
    </section>
    <section class="dashboard-camera-section">
        <div class="section-head"><div><h2>Live cameras</h2><p>Click any camera to open its full live view.</p></div><span class="health-detail" id="dashboard-camera-summary">Checking cameras…</span></div>
        <div class="dashboard-camera-grid">{camera_cards}</div>
    </section>
    <section class="dashboard-events-section">
        <div class="section-head"><div><h2>Recent events</h2><p>Latest motion and analytics activity with color previews.</p></div><div><span class="dashboard-event-refresh" id="dashboard-event-refresh">Auto-refreshing every 15 seconds</span> · <a class="download" href="/events">View all</a></div></div>
        <div class="dashboard-event-grid" id="dashboard-event-grid">{event_cards}</div>
    </section>
    <section class="dashboard-intelligence">
        <div class="panel">
            <div class="panel-head"><div><h2>AI activity summary</h2><div class="health-detail" id="ai-data-note">Loading activity…</div></div><a class="download" href="/analytics">Open analytics</a></div>
            <div class="ai-summary-grid">
                <div class="ai-summary-card"><span class="ai-summary-label">Motion today</span><span class="ai-summary-value" id="ai-motion-count">—</span><span class="ai-summary-detail">Detected by motion engine</span></div>
                <div class="ai-summary-card"><span class="ai-summary-label">People</span><span class="ai-summary-value" id="ai-person-count">—</span><span class="ai-summary-detail">Analytics detections</span></div>
                <div class="ai-summary-card"><span class="ai-summary-label">Vehicles</span><span class="ai-summary-value" id="ai-vehicle-count">—</span><span class="ai-summary-detail">Analytics detections</span></div>
                <div class="ai-summary-card"><span class="ai-summary-label">License plates</span><span class="ai-summary-value" id="ai-plate-count">—</span><span class="ai-summary-detail">LPR events</span></div>
                <div class="ai-summary-card"><span class="ai-summary-label">Intrusions</span><span class="ai-summary-value" id="ai-intrusion-count">—</span><span class="ai-summary-detail">Rule matches</span></div>
                <div class="ai-summary-card"><span class="ai-summary-label">Most active</span><span class="ai-summary-value" id="ai-active-camera">—</span><span class="ai-summary-detail" id="ai-active-detail">No events today</span></div>
            </div>
            <div class="activity-bars" id="ai-activity-bars" aria-label="24-hour event activity"></div>
            <div class="intelligence-note">Activity bars show motion events by hour for the current day.</div>
        </div>
        <div class="panel">
            <div class="panel-head"><div><h2>Smart alerts</h2><div class="health-detail">Issues and unread notifications</div></div><span class="dashboard-alert-count" id="dashboard-alert-count">0</span></div>
            <div class="alert-stack" id="dashboard-alert-stack"><div class="alert-empty">Loading alerts…</div></div>
            <div style="margin-top:14px"><a class="download" href="/alerts">View all alerts</a></div>
        </div>
    </section>
    <section class="health-grid">
        <div class="panel"><div class="panel-head"><h2>Health issues</h2><span class="health-detail">Automatic monitoring</span></div>{issue_rows}</div>
        <div class="panel"><div class="panel-head"><h2>Storage</h2><span class="health-detail">Local</span></div><div class="stat-value">{storage_text}</div><div class="storage-bar"><span id="storage-bar-value" style="width:{used_percent}%"></span></div><div class="health-detail" id="storage-detail">{used_percent}% of disk in use · {RETENTION_DAYS}-day retention</div></div>
    </section>"""

    scripts = """<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>
const dashboardPlayers = new Map();
function attachDashboardStream(cameraNumber){
    const video=document.getElementById(`dashboard-video-${cameraNumber}`);
    const placeholder=document.getElementById(`dashboard-placeholder-${cameraNumber}`);
    const source=`/hls/camera${cameraNumber}.m3u8`;
    if(!video)return;
    const showVideo=()=>{placeholder.hidden=true;video.classList.add('ready');video.play().catch(()=>{})};
    const showPlaceholder=()=>{placeholder.hidden=false;video.classList.remove('ready')};
    if(window.Hls&&Hls.isSupported()){
        const existing=dashboardPlayers.get(cameraNumber);if(existing)existing.destroy();
        const hls=new Hls({liveSyncDurationCount:2,maxBufferLength:8});
        dashboardPlayers.set(cameraNumber,hls);hls.loadSource(source);hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED,showVideo);
        hls.on(Hls.Events.ERROR,(_,data)=>{if(data.fatal)showPlaceholder()});
    }else if(video.canPlayType('application/vnd.apple.mpegurl')){
        video.src=source;video.addEventListener('loadedmetadata',showVideo,{once:true});video.addEventListener('error',showPlaceholder,{once:true});
    }
}
for(let cameraNumber=1;cameraNumber<=4;cameraNumber++)attachDashboardStream(cameraNumber);
async function updateDashboard(){
    try{
        const [statusResponse,metricsResponse,intelligenceResponse]=await Promise.all([
            fetch('/api/cameras/status',{cache:'no-store'}),
            fetch('/api/system/metrics',{cache:'no-store'}),
            fetch('/api/dashboard/intelligence',{cache:'no-store'})
        ]);
        const statusData=await statusResponse.json();const metrics=await metricsResponse.json();const intelligence=await intelligenceResponse.json();
        let onlineCount=0;
        statusData.cameras.forEach(camera=>{
            const live=document.getElementById(`dashboard-live-${camera.camera}`);
            const rec=document.getElementById(`dashboard-rec-${camera.camera}`);
            const detail=document.getElementById(`dashboard-detail-${camera.camera}`);
            const card=document.getElementById(`dashboard-camera-${camera.camera}`);
            if(camera.online)onlineCount++;
            live.textContent=camera.online?'LIVE':'OFFLINE';live.classList.toggle('wait',!camera.online);
            rec.classList.toggle('inactive',camera.recording!=='running');
            detail.textContent=camera.online
                ? `Online · ${camera.recording==='running'?'Recording active':'Recording '+camera.recording}`
                : `Stream ${camera.stream} · recording ${camera.recording}`;
            card.classList.toggle('offline',!camera.online);
        });
        document.getElementById('dashboard-camera-summary').textContent=`${onlineCount} of ${statusData.cameras.length} cameras online`;
        document.getElementById('today-cameras-count').textContent=`${onlineCount}/${statusData.cameras.length}`;
        document.getElementById('cpu-metric').textContent=metrics.cpu_percent+'%';
        document.getElementById('memory-metric').textContent=metrics.memory_percent+'%';
        document.getElementById('storage-metric').textContent=metrics.storage_free_gb+' GB';
        document.getElementById('storage-bar-value').style.width=metrics.storage_percent+'%';
        document.getElementById('storage-detail').textContent=metrics.storage_percent+'% of disk in use · retention active';
        renderIntelligence(intelligence);
    }catch(error){document.getElementById('dashboard-camera-summary').textContent='Status service unavailable';}
}
function formatAlertTime(value){const parsed=new Date(value);if(Number.isNaN(parsed.getTime()))return '';return parsed.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'})}
function renderIntelligence(data){
    const eventsToday=data.events_today??0;
    const peopleToday=data.analytics?.person??0;
    const vehiclesToday=data.analytics?.vehicle??0;
    const alertsToday=(data.unread_alert_count||0)+(data.active_issue_count||0);
    document.getElementById('today-events-count').textContent=eventsToday;
    document.getElementById('today-people-count').textContent=peopleToday;
    document.getElementById('today-vehicles-count').textContent=vehiclesToday;
    document.getElementById('today-alerts-count').textContent=alertsToday;
    document.getElementById('ai-motion-count').textContent=eventsToday;
    document.getElementById('ai-person-count').textContent=peopleToday;
    document.getElementById('ai-vehicle-count').textContent=vehiclesToday;
    document.getElementById('ai-plate-count').textContent=data.analytics?.plate??0;
    document.getElementById('ai-intrusion-count').textContent=data.analytics?.intrusion??0;
    const active=document.getElementById('ai-active-camera'),detail=document.getElementById('ai-active-detail');
    active.textContent=data.most_active_camera?`Camera ${data.most_active_camera}`:'—';
    detail.textContent=data.most_active_camera?`${data.most_active_camera_events} event${data.most_active_camera_events===1?'':'s'} today`:'No events today';
    document.getElementById('ai-data-note').textContent=data.analytics_mock?'Analytics counters currently use demo data':'Live analytics and motion activity';
    const bars=document.getElementById('ai-activity-bars');bars.replaceChildren();const values=data.hourly_activity||[];const max=Math.max(1,...values);
    values.forEach((value,hour)=>{const bar=document.createElement('div');bar.className='activity-bar';bar.style.height=`${Math.max(3,(value/max)*100)}%`;bar.title=`${hour}:00 · ${value} event${value===1?'':'s'}`;if(hour%4===0){const label=document.createElement('span');label.textContent=String(hour).padStart(2,'0');bar.appendChild(label)}bars.appendChild(bar)});
    const stack=document.getElementById('dashboard-alert-stack');stack.replaceChildren();const alerts=data.alerts||[];
    document.getElementById('dashboard-alert-count').textContent=String((data.unread_alert_count||0)+(data.active_issue_count||0));
    if(!alerts.length){const empty=document.createElement('div');empty.className='alert-empty';empty.textContent='No active alerts. Your system looks healthy.';stack.appendChild(empty);return}
    alerts.slice(0,6).forEach(alert=>{const link=document.createElement('a');link.href=alert.href||'/alerts';link.className=`alert-card ${alert.severity==='critical'?'critical':''}`;const icon=document.createElement('span');icon.className='alert-icon';icon.textContent=alert.severity==='critical'?'!':'⚠';const body=document.createElement('div');const message=document.createElement('div');message.className='alert-message';message.textContent=alert.message||'System alert';const meta=document.createElement('div');meta.className='alert-meta';meta.textContent=`${(alert.type||'alert').replaceAll('_',' ')} · ${formatAlertTime(alert.timestamp)}`;body.append(message,meta);const severity=document.createElement('span');severity.className='alert-severity';severity.textContent=alert.severity||'warning';link.append(icon,body,severity);stack.appendChild(link)});
}
function eventTimestamp(event){return event.start_time||event.timestamp||''}
function relativeTime(value){const parsed=new Date(value);if(Number.isNaN(parsed.getTime()))return 'Time unavailable';const seconds=Math.max(0,Math.floor((Date.now()-parsed.getTime())/1000));if(seconds<60)return `${seconds}s ago`;const minutes=Math.floor(seconds/60);if(minutes<60)return `${minutes}m ago`;const hours=Math.floor(minutes/60);if(hours<24)return `${hours}h ago`;return `${Math.floor(hours/24)}d ago`}
function buildEventCard(event){
    const card=document.createElement('article');card.className='dashboard-event-card';
    const imageWrap=document.createElement('div');imageWrap.className='dashboard-event-image';
    if(event.thumbnail){const image=document.createElement('img');image.src=event.thumbnail;image.alt=`${event.event_type||'Motion'} on Camera ${event.camera||'?'}`;image.loading='lazy';imageWrap.appendChild(image)}else{const fallback=document.createElement('div');fallback.className='dashboard-event-fallback';fallback.innerHTML='<strong>No thumbnail</strong><span>Preview unavailable</span>';imageWrap.appendChild(fallback)}
    const type=document.createElement('span');type.className='dashboard-event-type';type.textContent=(event.event_type||'motion').replaceAll('_',' ');imageWrap.appendChild(type);
    const camera=document.createElement('span');camera.className='dashboard-event-camera';camera.textContent=`Camera ${event.camera||'?'}`;imageWrap.appendChild(camera);
    const body=document.createElement('div');body.className='dashboard-event-body';
    const title=document.createElement('div');title.className='dashboard-event-title';title.textContent=`${(event.event_type||'Motion').replaceAll('_',' ')} detected`;body.appendChild(title);
    const meta=document.createElement('div');meta.className='dashboard-event-meta';const confidence=document.createElement('span');confidence.textContent=event.confidence!=null?`${event.confidence}% confidence`:'Event detected';const time=document.createElement('time');time.dateTime=eventTimestamp(event);time.textContent=relativeTime(eventTimestamp(event));meta.append(confidence,time);body.appendChild(meta);
    const actions=document.createElement('div');actions.className='dashboard-event-actions';const play=document.createElement('a');play.className='dashboard-event-action primary';play.href=event.linked_recording||'/playback';play.textContent='Play recording';actions.appendChild(play);if(event.thumbnail){const snapshot=document.createElement('a');snapshot.className='dashboard-event-action';snapshot.href=event.thumbnail;snapshot.target='_blank';snapshot.rel='noopener';snapshot.textContent='Snapshot';actions.appendChild(snapshot)}else{const missing=document.createElement('span');missing.className='dashboard-event-action';missing.textContent='No snapshot';actions.appendChild(missing)}body.appendChild(actions);card.append(imageWrap,body);return card
}
async function updateRecentEvents(){const grid=document.getElementById('dashboard-event-grid'),status=document.getElementById('dashboard-event-refresh');try{const response=await fetch('/api/events?limit=6',{cache:'no-store'});const data=await response.json();grid.replaceChildren();if(!data.events.length){const empty=document.createElement('div');empty.className='empty dashboard-event-empty';empty.textContent='Recent events will appear here after motion is detected.';grid.appendChild(empty)}else{data.events.forEach(event=>grid.appendChild(buildEventCard(event)))}status.textContent=`Updated ${new Date().toLocaleTimeString([], {hour:'numeric',minute:'2-digit'})}`}catch(error){status.textContent='Event refresh unavailable'}}
updateDashboard();updateRecentEvents();setInterval(updateDashboard,10000);setInterval(updateRecentEvents,15000);
setInterval(()=>{for(let cameraNumber=1;cameraNumber<=4;cameraNumber++){const video=document.getElementById(`dashboard-video-${cameraNumber}`);if(!video||video.readyState<2)attachDashboardStream(cameraNumber)}},15000);
</script>"""
    return page_shell("Dashboard", "dashboard", content, scripts)



def load_sales_training_library() -> list[dict]:
    data = load_json_file(SALES_TRAINING_LIBRARY_FILE, [])
    return data if isinstance(data, list) else []


def save_sales_training_library(items: list[dict]) -> None:
    save_json_file(SALES_TRAINING_LIBRARY_FILE, items[-5000:])


def sales_training_admin(user: dict) -> bool:
    return str(user.get("role") or "").lower() in {"admin", "administrator", "support_admin"}


@app.get("/media", response_class=HTMLResponse)
def media_library(request: Request) -> str:
    user = current_user(request)
    items = sorted(
        load_sales_training_library(),
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    is_admin = sales_training_admin(user)
    categories = sorted({str(item.get("category") or "General") for item in items})
    category_options = "".join(
        '<option value="' + escape(category, quote=True) + '">' + escape(category) + '</option>'
        for category in categories
    )

    cards = []
    for item in items:
        item_id = str(item.get("id") or "")
        title = str(item.get("title") or item.get("file_name") or "Training resource")
        description = str(item.get("description") or "Description not configured.")
        category = str(item.get("category") or "General")
        audience = str(item.get("audience") or "Sales & training")
        if str(item.get("content_type") or "").startswith("image/"):
            preview = '<img src="/api/media-library/' + escape(item_id, quote=True) + '/file" alt="' + escape(title, quote=True) + '">'
        else:
            preview = '<div class="training-file-icon">▧</div>'
        search_text = " ".join([title, description, category]).lower()
        cards.append(
            '<article class="training-card" data-category="' + escape(category, quote=True) + '" data-search="' + escape(search_text, quote=True) + '">'
            '<div class="training-preview">' + preview + '</div>'
            '<div class="training-body">'
            '<span class="admin-command-badge active">' + escape(category) + '</span>'
            '<h3>' + escape(title) + '</h3>'
            '<p>' + escape(description) + '</p>'
            '<small>' + escape(audience) + ' · ' + escape(str(item.get("created_at") or "")) + '</small>'
            '<div class="training-actions">'
            '<a href="/api/media-library/' + escape(item_id, quote=True) + '/file" target="_blank">Open</a>'
            '<a href="/api/media-library/' + escape(item_id, quote=True) + '/file" download>Download</a>'
            '</div></div></article>'
        )

    admin_panel = ""
    if is_admin:
        admin_panel = (
            '<section class="panel training-admin"><h2>Upload sales or training content</h2>'
            '<form action="/api/media-library/upload" method="post" enctype="multipart/form-data">'
            '<label>Title<input name="title" required></label>'
            '<label>Category<input name="category" list="training-categories" value="General" required></label>'
            '<datalist id="training-categories">' + category_options + '</datalist>'
            '<label>Audience<select name="audience"><option>Sales</option><option>Training</option><option>Customer education</option><option>Internal</option></select></label>'
            '<label>Description<textarea name="description" placeholder="What should the viewer learn or use this for?"></textarea></label>'
            '<label>File<input name="file" type="file" required></label>'
            '<button class="primary" type="submit">Upload resource</button>'
            '<details><summary>Library settings</summary><p>Categories are created from uploads. Approval workflow and external content providers are not configured.</p></details>'
            '</form></section>'
        )

    empty_state = (
        '<div class="not-configured"><strong>Sales & training library — Not configured</strong>'
        '<span>No approved resources have been uploaded yet.</span></div>'
    )
    content = (
        '<header class="topbar"><div><p class="eyebrow">Sales enablement</p><h1>Sales & training library</h1></div>'
        '<span class="pill">' + ("Admin upload" if is_admin else "Read only") + '</span></header>'
        '<div class="admin-privacy-note"><strong>Library boundary:</strong> this page contains approved sales and training resources, not customer recordings or evidence.</div>'
        + admin_panel +
        '<section class="panel"><div class="library-toolbar">'
        '<select class="date-filter" id="training-category"><option value="">All categories</option>' + category_options + '</select>'
        '<input class="date-filter" id="training-search" placeholder="Search titles, categories, or descriptions">'
        '<button class="filter" id="clear-training">Clear</button>'
        '</div></section>'
        '<section class="training-grid" id="training-grid">' + ("".join(cards) if cards else empty_state) + '</section>'
        '<div class="not-configured" id="training-no-results" hidden><strong>No matching resources</strong><span>Change the category or search text.</span></div>'
    )
    scripts = (
        '<script>'
        "const category=document.getElementById('training-category');"
        "const search=document.getElementById('training-search');"
        "const cards=[...document.querySelectorAll('.training-card')];"
        "const noResults=document.getElementById('training-no-results');"
        "function filterTraining(){const query=search.value.trim().toLowerCase();let visible=0;"
        "cards.forEach(card=>{const show=(!category.value||card.dataset.category===category.value)&&(!query||card.dataset.search.includes(query));card.hidden=!show;if(show)visible++});"
        "noResults.hidden=visible>0||cards.length===0}"
        "category.addEventListener('change',filterTraining);search.addEventListener('input',filterTraining);"
        "document.getElementById('clear-training').onclick=()=>{category.value='';search.value='';filterTraining()};"
        '</script>'
    )
    return page_shell("Sales & training library", "media", content, scripts)


@app.post("/api/media-library/upload")
async def upload_sales_training_resource(
    request: Request,
    title: str = Form(...),
    category: str = Form("General"),
    audience: str = Form("Sales"),
    description: str = Form(""),
    file: UploadFile = File(...),
) -> RedirectResponse:
    user = current_user(request)
    if not sales_training_admin(user):
        return RedirectResponse("/media", status_code=303)
    allowed_extensions = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".mp4", ".mov", ".png", ".jpg", ".jpeg", ".webp", ".zip"}
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed_extensions:
        return RedirectResponse("/media?upload=unsupported", status_code=303)
    item_id = uuid.uuid4().hex[:16]
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file.filename or ("resource" + suffix)).name)
    stored_name = item_id + "_" + safe_name
    destination = SALES_TRAINING_FOLDER / stored_name
    content_bytes = await file.read()
    destination.write_bytes(content_bytes)
    items = load_sales_training_library()
    item = {
        "id": item_id,
        "title": title.strip(),
        "category": category.strip() or "General",
        "audience": audience.strip() or "Sales",
        "description": description.strip(),
        "file_name": safe_name,
        "stored_name": stored_name,
        "content_type": file.content_type or "application/octet-stream",
        "size_bytes": len(content_bytes),
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "uploaded_by": user.get("display_name") or user.get("email") or "Administrator",
    }
    items.append(item)
    save_sales_training_library(items)
    record_audit(request, "media.training_uploaded", "training_resource:" + item_id, item["title"])
    return RedirectResponse("/media?upload=complete", status_code=303)


@app.get("/api/media-library/{item_id}/file")
def sales_training_resource_file(item_id: str, request: Request) -> Response:
    item = next((entry for entry in load_sales_training_library() if str(entry.get("id")) == item_id), None)
    if not item:
        return JSONResponse({"detail": "Training resource not found."}, status_code=404)
    path = SALES_TRAINING_FOLDER / str(item.get("stored_name") or "")
    if not path.exists():
        return JSONResponse({"detail": "Training resource file is not configured."}, status_code=404)
    return FileResponse(
        path,
        media_type=str(item.get("content_type") or "application/octet-stream"),
        filename=str(item.get("file_name") or path.name),
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics() -> str:
    camera_options = "".join(
        f'<option value="{camera}">Camera {camera}</option>'
        for camera in range(1, CAMERA_COUNT + 1)
    )
    content = f"""
    <header class="topbar">
        <div>
            <p class="eyebrow">Intelligent video</p>
            <h1>AI analytics dashboard</h1>
        </div>
        <a class="action-button" href="/analytics/vehicle-search">Open smart search</a>
    </header>

    <div class="analytics-demo-note" id="analytics-data-note">
        Loading analytics data source…
    </div>

    <section class="analytics-kpis" style="margin-top:18px">
        <article class="analytics-kpi"><span class="analytics-kpi-label">Events today</span><strong class="analytics-kpi-value" id="analytics-today">—</strong><span class="analytics-kpi-detail">All analytic types</span></article>
        <article class="analytics-kpi"><span class="analytics-kpi-label">Last 24 hours</span><strong class="analytics-kpi-value" id="analytics-24h">—</strong><span class="analytics-kpi-detail">Rolling activity window</span></article>
        <article class="analytics-kpi"><span class="analytics-kpi-label">Average confidence</span><strong class="analytics-kpi-value" id="analytics-confidence">—</strong><span class="analytics-kpi-detail">Across the last seven days</span></article>
        <article class="analytics-kpi"><span class="analytics-kpi-label">Most active camera</span><strong class="analytics-kpi-value" id="analytics-camera">—</strong><span class="analytics-kpi-detail" id="analytics-peak">Peak hour loading…</span></article>
    </section>

    <section class="analytics-dashboard-grid">
        <article class="panel">
            <div class="panel-head"><h2>24-hour activity</h2><span class="health-detail">Hourly event volume</span></div>
            <div class="analytics-chart" id="analytics-hourly-chart"></div>
        </article>
        <article class="panel">
            <div class="panel-head"><h2>Events by type</h2><a class="download" href="/analytics/vehicle-search">Search all</a></div>
            <div class="analytics-type-list" id="analytics-type-list"></div>
        </article>
    </section>

    <section class="analytics-dashboard-grid">
        <article class="panel">
            <div class="panel-head"><h2>Seven-day trend</h2><span class="health-detail">Daily detections</span></div>
            <div class="analytics-seven-day" id="analytics-seven-day"></div>
        </article>
        <article class="panel">
            <div class="panel-head"><h2>Camera comparison</h2><span class="health-detail">Last seven days</span></div>
            <div class="analytics-camera-list" id="analytics-camera-list"></div>
        </article>
    </section>

    <section class="panel analytics-search-panel">
        <div class="panel-head"><h2>Analytics search</h2><span class="health-detail">Filter live results</span></div>
        <div class="analytics-search-grid">
            <input id="analytics-natural" placeholder="Try: silver vehicles on camera 2">
            <select id="analytics-type"><option value="">All types</option><option value="person">Person</option><option value="vehicle">Vehicle</option><option value="plate">Plate</option><option value="line_crossing">Line crossing</option><option value="intrusion">Intrusion</option></select>
            <select id="analytics-search-camera"><option value="">All cameras</option>{camera_options}</select>
            <input id="analytics-color" placeholder="Vehicle color">
            <input id="analytics-plate" placeholder="Plate number">
            <input id="analytics-date" type="date">
            <button class="action-button" id="analytics-search-button">Search</button>
        </div>
        <div class="health-detail" id="analytics-search-status" style="margin-top:10px">Search is ready.</div>
    </section>

    <section class="panel">
        <div class="panel-head"><h2>Matching events</h2><span class="health-detail" id="analytics-result-count">0 results</span></div>
        <div class="analytics-results" id="analytics-results"><div class="empty">Run a search to view analytics events.</div></div>
    </section>

    <section style="margin-top:18px">
        <div class="section-head"><div><h2>Configure analytics</h2><p>Rules and specialized modules</p></div></div>
        <div class="feature-grid">{"".join(f'<article class="feature-card"><div class="feature-icon">✦</div><h2>{escape(name)}</h2><p>{escape(description)}</p><a class="download" href="/analytics/{slugify(name)}">Configure module</a></article>' for name, description in ANALYTICS_FEATURES)}</div>
    </section>
    """

    scripts = """
    <script>
    const typeLabels={person:'Person',vehicle:'Vehicle',plate:'License plate',line_crossing:'Line crossing',intrusion:'Intrusion',motion:'Motion'};
    const esc=value=>String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#039;'}[char]));
    function progressRows(target,data,labeler){const entries=Object.entries(data),max=Math.max(1,...entries.map(([,value])=>value));target.innerHTML=entries.length?entries.sort((a,b)=>b[1]-a[1]).map(([key,value])=>`<div class="analytics-progress-row"><span>${esc(labeler(key))}</span><div class="analytics-progress-track"><span style="width:${Math.max(3,value/max*100)}%"></span></div><strong>${value}</strong></div>`).join(''):'<div class="empty">No analytics data.</div>'}
    async function loadAnalyticsSummary(){try{const response=await fetch('/api/analytics/summary',{cache:'no-store'}),data=await response.json();document.getElementById('analytics-today').textContent=data.today_count;document.getElementById('analytics-24h').textContent=data.last_24_count;document.getElementById('analytics-confidence').textContent=data.average_confidence+'%';document.getElementById('analytics-camera').textContent='Camera '+data.active_camera;document.getElementById('analytics-peak').textContent='Peak hour: '+String(data.peak_hour).padStart(2,'0')+':00';document.getElementById('analytics-data-note').textContent=data.mock_data?'Demo analytics data is active. Install a compatible detection model to populate real person, vehicle, plate, and intrusion events.':'Live analytics event data is active.';const hourly=document.getElementById('analytics-hourly-chart'),maxHourly=Math.max(1,...data.hourly_counts);hourly.innerHTML=data.hourly_counts.map((value,index)=>`<div class="analytics-chart-column" style="height:${Math.max(3,value/maxHourly*100)}%"><strong>${value||''}</strong><span>${index%3===0?String(index).padStart(2,'0'):''}</span></div>`).join('');progressRows(document.getElementById('analytics-type-list'),data.type_counts,key=>typeLabels[key]||key.replaceAll('_',' '));progressRows(document.getElementById('analytics-camera-list'),data.camera_counts,key=>'Camera '+key);const dayMax=Math.max(1,...data.seven_day.map(day=>day.count));document.getElementById('analytics-seven-day').innerHTML=data.seven_day.map(day=>`<div class="analytics-day" style="height:${Math.max(4,day.count/dayMax*100)}%"><strong>${day.count}</strong><span>${esc(day.label)}</span></div>`).join('')}catch(error){document.getElementById('analytics-data-note').textContent='Analytics summary is temporarily unavailable.'}}
    async function translateNaturalQuery(){const query=document.getElementById('analytics-natural').value.trim();if(!query)return;const response=await fetch('/api/analytics/natural-search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query})}),result=await response.json(),filters=result.filters||{};if(filters.event_type)document.getElementById('analytics-type').value=filters.event_type;if(filters.camera)document.getElementById('analytics-search-camera').value=filters.camera;if(filters.color)document.getElementById('analytics-color').value=filters.color;if(filters.plate)document.getElementById('analytics-plate').value=filters.plate}
    function renderResults(events){const target=document.getElementById('analytics-results');document.getElementById('analytics-result-count').textContent=events.length+' result'+(events.length===1?'':'s');if(!events.length){target.innerHTML='<div class="empty">No events match those filters.</div>';return}target.innerHTML=events.slice(0,100).map(event=>{const title=typeLabels[event.event_type]||String(event.event_type||'Event').replaceAll('_',' '),confidence=Number(event.confidence||0),percent=Math.round(confidence<=1?confidence*100:confidence),stamp=String(event.timestamp||event.start_time||'').replace('T',' ').slice(0,19),image=event.thumbnail?`<img class="analytics-result-thumb" src="${esc(event.thumbnail)}" alt="${esc(title)}">`:'<div class="analytics-result-thumb"></div>',clip=event.linked_recording?`<a class="download" href="${esc(event.linked_recording)}">Open clip</a>`:'<span class="health-detail">No clip</span>';return `<article class="analytics-result">${image}<div><div class="analytics-result-title">${esc(title)} · Camera ${esc(event.camera)}</div><div class="analytics-result-meta">${esc(stamp)} · ${percent}% confidence${event.plate_number?' · Plate '+esc(event.plate_number):''}${event.vehicle_color?' · '+esc(event.vehicle_color):''}</div></div><div class="analytics-result-actions"><span class="analytics-pill">${esc(event.site||'home')}</span>${clip}<a class="download" href="/camera/${esc(event.camera)}">Camera</a></div></article>`}).join('')}
    async function searchAnalytics(){const status=document.getElementById('analytics-search-status');status.textContent='Searching…';await translateNaturalQuery();const params=new URLSearchParams(),type=document.getElementById('analytics-type').value,camera=document.getElementById('analytics-search-camera').value,color=document.getElementById('analytics-color').value.trim(),plate=document.getElementById('analytics-plate').value.trim(),date=document.getElementById('analytics-date').value;if(type)params.set('event_type',type);if(camera)params.set('camera',camera);if(color)params.set('color',color);if(plate)params.set('plate',plate);if(date){params.set('date_from',date);params.set('date_to',date)}const response=await fetch('/api/analytics/events?'+params.toString(),{cache:'no-store'}),data=await response.json();renderResults(data.events||[]);status.textContent=data.mock_data?'Results use demo analytics data.':'Results use live analytics data.'}
    document.getElementById('analytics-search-button').addEventListener('click',searchAnalytics);document.getElementById('analytics-natural').addEventListener('keydown',event=>{if(event.key==='Enter')searchAnalytics()});loadAnalyticsSummary();searchAnalytics();setInterval(loadAnalyticsSummary,30000);
    </script>
    """
    return page_shell("AI analytics", "analytics", content, scripts)


def analytics_rule_builder(analytic_type: str) -> str:
    label = "Line crossing" if analytic_type == "line_crossing" else "Intrusion"
    camera_options = "".join(f'<option value="{n}">Camera {n}</option>' for n in range(1, CAMERA_COUNT + 1))
    content = f"""<header class="topbar"><div><p class="eyebrow">Analytics rule</p><h1>{label}</h1></div><a class="download" href="/analytics">All analytics</a></header><div class="mock-banner">Rule configuration is real. Event generation remains mock until a compatible object-detection model is installed.</div><section class="rule-layout"><div class="panel"><div class="panel-head"><h2>Draw {"a line" if analytic_type == "line_crossing" else "an intrusion zone"}</h2><button class="ghost-button" id="clear-rule">Clear drawing</button></div><div class="rule-stage"><video id="rule-preview" autoplay muted playsinline></video><canvas id="rule-canvas" width="1280" height="720"></canvas></div><p class="health-detail">Click {"two points" if analytic_type == "line_crossing" else "three or more points"} over the camera image.</p></div><form class="panel rule-form" id="analytic-rule-form"><label>Rule name<input id="rule-name" value="{label} rule" required></label><label>Camera<select id="rule-camera">{camera_options}</select></label><label>Direction<select id="rule-direction"><option value="both">Both directions</option><option value="inbound">Inbound / Entry</option><option value="outbound">Outbound / Exit</option></select></label><label>Sensitivity<input id="rule-sensitivity" type="range" min="1" max="100" value="60"></label><label>Confidence threshold<input id="rule-confidence" type="number" min="0" max="1" step="0.05" value="0.60"></label><label>Active from<input id="rule-start" type="time" value="00:00"></label><label>Active until<input id="rule-end" type="time" value="23:59"></label><label>Retention days<input id="rule-retention" type="number" min="1" max="3650" value="30"></label><label><input id="rule-alerts" type="checkbox" checked> Create in-app alerts</label><label><input id="rule-enabled" type="checkbox" checked> Rule enabled</label><button class="action-button" type="submit">Save rule</button></form></section>"""
    scripts = f"""<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>const type='{analytic_type}',canvas=document.getElementById('rule-canvas'),context=canvas.getContext('2d'),points=[];function draw(){{context.clearRect(0,0,canvas.width,canvas.height);if(!points.length)return;context.strokeStyle='#42e4dc';context.fillStyle='rgba(66,228,220,.16)';context.lineWidth=5;context.beginPath();points.forEach((point,index)=>index?context.lineTo(point.x*canvas.width,point.y*canvas.height):context.moveTo(point.x*canvas.width,point.y*canvas.height));if(type==='intrusion'&&points.length>2){{context.closePath();context.fill()}}context.stroke();points.forEach(point=>{{context.beginPath();context.arc(point.x*canvas.width,point.y*canvas.height,8,0,Math.PI*2);context.fillStyle='#fff';context.fill()}})}}canvas.addEventListener('click',event=>{{const rect=canvas.getBoundingClientRect();if(type==='line_crossing'&&points.length>=2)points.length=0;points.push({{x:(event.clientX-rect.left)/rect.width,y:(event.clientY-rect.top)/rect.height}});draw()}});document.getElementById('clear-rule').addEventListener('click',()=>{{points.length=0;draw()}});const camera=document.getElementById('rule-camera'),video=document.getElementById('rule-preview');function connectPreview(){{const source=`/static/hls/camera${{camera.value}}.m3u8`;if(window.ruleHls)window.ruleHls.destroy();if(Hls.isSupported()){{window.ruleHls=new Hls();window.ruleHls.loadSource(source);window.ruleHls.attachMedia(video)}}else video.src=source}}camera.addEventListener('change',connectPreview);connectPreview();document.getElementById('analytic-rule-form').addEventListener('submit',async event=>{{event.preventDefault();if(points.length<(type==='line_crossing'?2:3)){{showToast('Draw the required line or zone first.');return}}const payload={{camera:Number(camera.value),name:document.getElementById('rule-name').value,analytic_type:type,enabled:document.getElementById('rule-enabled').checked,direction:document.getElementById('rule-direction').value,sensitivity:Number(document.getElementById('rule-sensitivity').value),confidence_threshold:Number(document.getElementById('rule-confidence').value),schedule_start:document.getElementById('rule-start').value,schedule_end:document.getElementById('rule-end').value,retention_days:Number(document.getElementById('rule-retention').value),alerts_enabled:document.getElementById('rule-alerts').checked,geometry:points}},response=await fetch('/api/analytics/rules',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}}),result=await response.json();showToast(result.message)}});</script>"""
    return page_shell(label, "analytics", content, scripts)


def lpr_analytics_page() -> str:
    events = [event for event in analytics_events() if event.get("event_type") == "plate"]
    rows = "".join(f'<tr class="lpr-row" data-plate="{escape(event.get("plate_number") or "")}" data-camera="{event.get("camera")}" data-date="{event.get("timestamp", "")[:10]}"><td>{escape((event.get("timestamp") or "").replace("T", " ")[:19])}</td><td><strong>{escape(event.get("plate_number") or "Unreadable")}</strong></td><td>{escape(event.get("vehicle_color") or "Unknown")}</td><td>{float(event.get("confidence", 0))*100:.0f}%</td><td>Camera {event.get("camera")}</td><td><a class="download" href="{event.get("linked_recording") or '#'}">Linked clip</a></td></tr>' for event in events)
    content = f"""<header class="topbar"><div><p class="eyebrow">Modular analytic</p><h1>License plate recognition</h1></div></header><div class="mock-banner">Mock detection data: plate-search storage and filtering are functional; live OCR requires an installed LPR model.</div><section class="panel"><div class="library-toolbar"><input class="portal-search" id="plate-search" placeholder="Search full or partial plate"><select class="date-filter" id="plate-camera"><option value="">All cameras</option>{''.join(f'<option value="{n}">Camera {n}</option>' for n in range(1,CAMERA_COUNT+1))}</select><input class="date-filter" id="plate-from" type="date"><input class="date-filter" id="plate-to" type="date"></div><table class="data-table"><thead><tr><th>Time</th><th>Plate</th><th>Vehicle</th><th>Confidence</th><th>Camera</th><th>Clip</th></tr></thead><tbody>{rows or '<tr><td colspan="6">No plate records.</td></tr>'}</tbody></table></section>"""
    scripts = """<script>const lprRows=[...document.querySelectorAll('.lpr-row')],plateSearch=document.getElementById('plate-search'),plateCamera=document.getElementById('plate-camera'),plateFrom=document.getElementById('plate-from'),plateTo=document.getElementById('plate-to');function filterPlates(){const query=plateSearch.value.toUpperCase();lprRows.forEach(row=>row.hidden=!row.dataset.plate.toUpperCase().includes(query)||(plateCamera.value&&row.dataset.camera!==plateCamera.value)||(plateFrom.value&&row.dataset.date<plateFrom.value)||(plateTo.value&&row.dataset.date>plateTo.value))}[plateSearch,plateCamera,plateFrom,plateTo].forEach(input=>input.addEventListener('input',filterPlates));</script>"""
    return page_shell("License plate recognition", "analytics", content, scripts)


def people_analytics_page(title: str) -> str:
    hourly = [1, 2, 1, 3, 5, 7, 10, 8, 6, 9, 4, 2]
    bars = "".join(f'<div class="chart-bar" style="height:{value*8}%"><span>{index+8}:00</span></div>' for index, value in enumerate(hourly))
    summaries = "".join(f'<article class="stat"><span class="stat-label">Camera {n}</span><span class="stat-value">{(n*7)+3} entries</span><div class="health-detail">{n*4+1} exits · {n+2} current</div></article>' for n in range(1,CAMERA_COUNT+1))
    content = f"""<header class="topbar"><div><p class="eyebrow">Occupancy intelligence</p><h1>{escape(title)}</h1></div></header><div class="mock-banner">Mock analytics totals: charts and summaries are demonstrations until a person-detection model is installed.</div><section class="summary"><div class="stat"><span class="stat-label">Current occupancy</span><span class="stat-value">14</span></div><div class="stat"><span class="stat-label">Today entries</span><span class="stat-value">72</span></div><div class="stat"><span class="stat-label">Today exits</span><span class="stat-value">58</span></div></section><section class="panel"><div class="panel-head"><h2>Hourly people totals</h2><span class="pill wait">Mock data</span></div><div class="chart">{bars}</div></section><section class="summary" style="margin-top:28px">{summaries}</section>"""
    return page_shell(title, "analytics", content)


def smart_search_page() -> str:
    events = analytics_events()
    cards = []
    for event in events:
        event_id = escape(str(event.get("id", "")), quote=True)
        event_type = str(event.get("event_type", "event"))
        title = escape(event_type.replace("_", " ").title())
        camera = int(event.get("camera") or 0)
        site = escape(str(event.get("site", "home")))
        timestamp = escape(
            str(event.get("timestamp") or event.get("start_time") or "")
            .replace("T", " ")[:19]
        )
        confidence = float(event.get("confidence", 0) or 0)
        confidence_percent = round(confidence * 100 if confidence <= 1 else confidence)
        color = escape(str(event.get("vehicle_color") or ""))
        plate = escape(str(event.get("plate_number") or ""))
        detail = plate or color or "No additional details"
        thumbnail = str(event.get("thumbnail") or "")
        recording = str(event.get("linked_recording") or "")
        thumbnail_html = (
            f'<img src="{escape(thumbnail, quote=True)}" alt="{title} detected on Camera {camera}">'
            if thumbnail
            else (
                '<div class="smart-search-placeholder">'
                f'<span>◉</span><strong>{title}</strong><small>Camera {camera}</small>'
                '</div>'
            )
        )
        recording_attr = escape(recording, quote=True)
        disabled = "" if recording else " disabled"
        download_html = (
            f'<a class="smart-search-action" href="{recording_attr}" download>Download</a>'
            if recording
            else '<button class="smart-search-action" type="button" disabled>Download</button>'
        )
        playback_html = (
            f'<a class="smart-search-action" href="{recording_attr}">Playback</a>'
            if recording
            else '<a class="smart-search-action" href="/playback">Playback</a>'
        )
        card_html = f'''<article class="smart-search-card search-event"
                data-id="{event_id}"
                data-type="{escape(event_type, quote=True)}"
                data-camera="{camera}"
                data-site="{escape(str(event.get("site", "home")), quote=True)}"
                data-color="{escape(str(event.get("vehicle_color") or ""), quote=True)}"
                data-plate="{escape(str(event.get("plate_number") or ""), quote=True)}"
                data-date="{escape(str(event.get("timestamp", ""))[:10], quote=True)}"
                data-recording="{recording_attr}"
                data-title="{escape(title, quote=True)}">
              <button class="smart-search-media preview-event" type="button"{disabled}>
                {thumbnail_html}
                <span class="smart-search-play">▶ Preview</span>
              </button>
              <div class="smart-search-body">
                <div class="smart-search-heading">
                  <div><h3>{title}</h3><div class="smart-search-meta">{timestamp} · Camera {camera} · {site}</div></div>
                  <span class="smart-search-confidence">{confidence_percent}%</span>
                </div>
                <div class="smart-search-detail">{detail}</div>
                <div class="smart-search-actions">
                  <button class="smart-search-action review-action" type="button" data-action="bookmarked">Bookmark</button>
                  {playback_html}
                  {download_html}
                  <button class="smart-search-action share-event" type="button"{disabled}>Share</button>
                  <a class="smart-search-action" href="/camera/{camera}">Live camera</a>
                  <button class="smart-search-action review-action" type="button" data-action="false_positive">False positive</button>
                </div>
              </div>
            </article>'''
        cards.append(card_html)

    camera_options = "".join(
        f'<option value="{n}">Camera {n}</option>'
        for n in range(1, CAMERA_COUNT + 1)
    )
    cards_html = "".join(cards) or '<div class="empty">No Smart Search events are available.</div>'

    content = f'''<header class="topbar"><div><p class="eyebrow">Experimental discovery</p><h1>Smart search</h1></div></header>
    <div class="mock-banner">Mock detection data may be shown. Filters, thumbnails, reviews, playback links, downloads, and sharing use the existing event records.</div>
    <section class="panel">
      <label>Natural-language search (experimental)
        <div class="portal-search-row">
          <input class="portal-search" id="natural-search" placeholder="Example: silver vehicles on camera 2">
          <button class="action-button" id="translate-search">Translate</button>
        </div>
      </label>
      <div class="health-detail" id="translated-filters"></div>
      <div class="library-toolbar" style="margin-top:18px">
        <select class="date-filter" id="search-type">
          <option value="">All event types</option>
          <option value="person">Person</option>
          <option value="vehicle">Vehicle</option>
          <option value="car">Car</option>
          <option value="truck">Truck</option>
          <option value="plate">Plate</option>
          <option value="line_crossing">Line crossing</option>
          <option value="intrusion">Intrusion</option>
        </select>
        <select class="date-filter" id="search-camera"><option value="">All cameras</option>{camera_options}</select>
        <input class="date-filter" id="search-color" placeholder="Color">
        <input class="date-filter" id="search-plate" placeholder="Plate">
        <input class="date-filter" id="search-date" type="date">
      </div>
      <div class="health-detail" id="search-result-count" style="margin-top:10px">{len(events)} event(s)</div>
      <div class="smart-search-grid" id="smart-search-grid">{cards_html}</div>
    </section>

    <dialog class="smart-preview-dialog" id="smart-preview-dialog">
      <div class="smart-preview-body">
        <div class="panel-head">
          <div><p class="eyebrow">Event preview</p><h2 id="smart-preview-title">Smart Search event</h2></div>
          <button class="smart-search-action" id="close-smart-preview" type="button">Close</button>
        </div>
        <video class="smart-preview-video" id="smart-preview-video" controls playsinline hidden></video>
        <div class="smart-preview-empty" id="smart-preview-empty">No browser-compatible preview is attached to this event. Use Open in playback or Download.</div>
        <div class="smart-preview-actions">
          <a class="smart-search-action" id="smart-preview-playback" href="/playback">Open in playback</a>
          <a class="smart-search-action" id="smart-preview-download" href="#" download>Download clip</a>
          <button class="smart-search-action" id="smart-preview-share" type="button">Share</button>
        </div>
      </div>
    </dialog>'''

    scripts = '''
    <script>
    const searchCards=[...document.querySelectorAll('.search-event')],
      type=document.getElementById('search-type'),
      camera=document.getElementById('search-camera'),
      color=document.getElementById('search-color'),
      plate=document.getElementById('search-plate'),
      date=document.getElementById('search-date'),
      resultCount=document.getElementById('search-result-count'),
      previewDialog=document.getElementById('smart-preview-dialog'),
      previewVideo=document.getElementById('smart-preview-video'),
      previewEmpty=document.getElementById('smart-preview-empty'),
      previewTitle=document.getElementById('smart-preview-title'),
      previewPlayback=document.getElementById('smart-preview-playback'),
      previewDownload=document.getElementById('smart-preview-download'),
      previewShare=document.getElementById('smart-preview-share');

    function filterEvents(){
      let visible=0;
      searchCards.forEach(card=>{
        const hidden=(type.value&&card.dataset.type!==type.value)
          ||(camera.value&&card.dataset.camera!==camera.value)
          ||(color.value&&!card.dataset.color.toLowerCase().includes(color.value.toLowerCase()))
          ||(plate.value&&!card.dataset.plate.toLowerCase().includes(plate.value.toLowerCase()))
          ||(date.value&&card.dataset.date!==date.value);
        card.hidden=hidden;
        if(!hidden)visible++;
      });
      resultCount.textContent=visible+' event'+(visible===1?'':'s');
    }
    [type,camera,color,plate,date].forEach(input=>input.addEventListener('input',filterEvents));

    document.getElementById('translate-search').addEventListener('click',async()=>{
      const query=document.getElementById('natural-search').value.trim();
      if(!query){showToast('Enter a Smart Search query.');return}
      const response=await fetch('/api/analytics/natural-search',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({query})
      });
      const result=await response.json(),filters=result.filters||{};
      document.getElementById('translated-filters').textContent='Experimental filters: '+JSON.stringify(filters);
      if(filters.event_type)type.value=filters.event_type;
      if(filters.camera)camera.value=filters.camera;
      if(filters.color)color.value=filters.color;
      if(filters.plate)plate.value=filters.plate;
      filterEvents();
    });

    function openPreview(card){
      const recording=card.dataset.recording||'';
      previewTitle.textContent=card.dataset.title+' · Camera '+card.dataset.camera;
      previewPlayback.href=recording||'/playback';
      previewDownload.href=recording||'#';
      previewDownload.hidden=!recording;
      previewShare.dataset.url=recording;
      previewVideo.pause();
      previewVideo.removeAttribute('src');
      previewVideo.load();
      if(recording){
        previewVideo.src=recording;
        previewVideo.hidden=false;
        previewEmpty.hidden=true;
      }else{
        previewVideo.hidden=true;
        previewEmpty.hidden=false;
      }
      previewDialog.showModal();
    }

    document.querySelectorAll('.preview-event').forEach(button=>{
      button.addEventListener('click',()=>openPreview(button.closest('.search-event')));
    });
    document.getElementById('close-smart-preview').addEventListener('click',()=>{
      previewVideo.pause();
      previewDialog.close();
    });

    async function shareEvent(card){
      const recording=card.dataset.recording||'';
      const shareUrl=recording?new URL(recording,location.origin).href:location.href;
      const shareData={
        title:card.dataset.title+' · Camera '+card.dataset.camera,
        text:'AnyAiCam Smart Search event',
        url:shareUrl
      };
      try{
        if(navigator.share)await navigator.share(shareData);
        else{
          await navigator.clipboard.writeText(shareUrl);
          showToast('Event link copied.');
        }
      }catch(error){
        if(error.name!=='AbortError')showToast('Could not share this event.');
      }
    }
    document.querySelectorAll('.share-event').forEach(button=>{
      button.addEventListener('click',()=>shareEvent(button.closest('.search-event')));
    });
    previewShare.addEventListener('click',async()=>{
      const url=previewShare.dataset.url?new URL(previewShare.dataset.url,location.origin).href:location.href;
      try{
        if(navigator.share)await navigator.share({title:previewTitle.textContent,url});
        else{await navigator.clipboard.writeText(url);showToast('Event link copied.')}
      }catch(error){
        if(error.name!=='AbortError')showToast('Could not share this event.');
      }
    });

    document.querySelectorAll('.review-action').forEach(button=>button.addEventListener('click',async()=>{
      const card=button.closest('.search-event'),
        action=button.dataset.action,
        payload={event_id:card.dataset.id,acknowledged:false,bookmarked:false,false_positive:false,tags:[],notes:''};
      payload[action]=true;
      const response=await fetch(`/api/analytics/events/${card.dataset.id}/review`,{
        method:'PUT',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
      });
      const result=await response.json();
      showToast(result.message);
      if(action==='bookmarked'&&result.status==='complete')button.textContent='Bookmarked';
    }));
    filterEvents();
    </script>'''
    return page_shell("Smart search", "analytics", content, scripts)



@app.get("/investigate", response_class=HTMLResponse)
def investigation_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        return permission_denied_page("Investigate", "investigate", "view_analytics")

    reviews = load_json_file(EVENT_REVIEWS_FILE, {})
    raw_events = load_motion_events() + analytics_events()
    allowed_cameras = set(user_camera_ids(user))
    normalized = []

    for event in raw_events:
        try:
            camera = int(event.get("camera") or event.get("camera_id") or 0)
        except (TypeError, ValueError):
            continue
        if camera not in allowed_cameras:
            continue

        event_id = str(event.get("id") or uuid.uuid4().hex[:10])
        timestamp = str(
            event.get("timestamp")
            or event.get("start_time")
            or event.get("event_timestamp")
            or ""
        )
        normalized.append({
            "id": event_id,
            "camera": camera,
            "site": str(event.get("site") or "home"),
            "timestamp": timestamp,
            "end_time": str(event.get("end_time") or timestamp),
            "event_type": str(event.get("event_type") or "motion").lower(),
            "thumbnail": str(event.get("thumbnail") or ""),
            "recording": str(event.get("linked_recording") or ""),
            "confidence": event.get("confidence", event.get("score")),
            "plate": str(event.get("plate_number") or ""),
            "color": str(event.get("vehicle_color") or ""),
            "rule": str(event.get("rule_name") or ""),
            "review": reviews.get(event_id, {}),
        })

    normalized.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    investigation_data = json.dumps(normalized[:500], default=str)
    camera_options = "".join(
        f'<option value="{camera}">Camera {camera}</option>'
        for camera in sorted(allowed_cameras)
    )

    content = f"""<header class="topbar"><div><p class="eyebrow">AI event investigation</p><h1>Investigate</h1></div></header>
    <section class="investigation-shell">
      <aside class="investigation-filters">
        <h2>Search evidence</h2>
        <label>Natural-language search<input id="investigation-query" placeholder="Example: red truck on camera 2 yesterday"></label>
        <label>Event type<select id="investigation-type"><option value="">All event types</option><option value="motion">Motion</option><option value="person">Person</option><option value="vehicle">Vehicle</option><option value="car">Car</option><option value="truck">Truck</option><option value="plate">License plate</option><option value="line_crossing">Line crossing</option><option value="intrusion">Intrusion</option></select></label>
        <label>Camera<select id="investigation-camera"><option value="">All cameras</option>{camera_options}</select></label>
        <label>Vehicle or clothing color<input id="investigation-color" placeholder="red, blue, silver"></label>
        <label>License plate<input id="investigation-plate" placeholder="Plate text"></label>
        <label>From<input id="investigation-from" type="datetime-local"></label>
        <label>To<input id="investigation-to" type="datetime-local"></label>
        <div class="investigation-actions"><button class="action-button" id="run-investigation" type="button">Search</button><button class="ghost-button" id="clear-investigation" type="button">Clear</button></div>
      </aside>
      <div>
        <section class="investigation-summary">
          <div class="stat"><span class="stat-label">Results</span><span class="stat-value" id="investigation-result-count">0</span></div>
          <div class="stat"><span class="stat-label">People</span><span class="stat-value" id="investigation-people-count">0</span></div>
          <div class="stat"><span class="stat-label">Vehicles</span><span class="stat-value" id="investigation-vehicle-count">0</span></div>
          <div class="stat"><span class="stat-label">Bookmarked</span><span class="stat-value" id="investigation-bookmark-count">0</span></div>
        </section>
        <div class="investigation-grid" id="investigation-grid"></div>
        <section class="evidence-panel">
          <div class="panel-head"><div><h2>Evidence export</h2><div class="health-detail">Select results and export a JSON evidence manifest. Video export continues to use existing playback and clip tools.</div></div></div>
          <div class="investigation-actions"><button id="select-visible-evidence" type="button">Select visible results</button><button id="clear-evidence-selection" type="button">Clear selection</button><button class="action-button" id="export-evidence" type="button">Export manifest</button><button class="ghost-button" id="create-case-from-evidence" type="button">Create case</button></div>
        </section>
      </div>
    </section>"""

    scripts = f"""<script>
    const investigationEvents={investigation_data};
    const selectedEvidence=new Set();
    const queryInput=document.getElementById('investigation-query');
    const typeInput=document.getElementById('investigation-type');
    const cameraInput=document.getElementById('investigation-camera');
    const colorInput=document.getElementById('investigation-color');
    const plateInput=document.getElementById('investigation-plate');
    const fromInput=document.getElementById('investigation-from');
    const toInput=document.getElementById('investigation-to');
    const grid=document.getElementById('investigation-grid');

    function isVehicle(type){{return ['car','truck','bus','motorcycle','bicycle','vehicle'].includes(type)}}
    function naturalMatches(event,query){{
      if(!query)return true;
      const combined=[event.event_type,event.camera,event.site,event.color,event.plate,event.rule,event.timestamp,event.review?.notes,(event.review?.tags||[]).join(' ')].join(' ').toLowerCase();
      return query.toLowerCase().split(/\\s+/).filter(Boolean).every(token=>combined.includes(token));
    }}
    function visibleEvents(){{
      const from=fromInput.value?new Date(fromInput.value):null;
      const to=toInput.value?new Date(toInput.value):null;
      return investigationEvents.filter(event=>{{
        const stamp=event.timestamp?new Date(event.timestamp):null;
        const requested=typeInput.value;
        return (!requested||event.event_type===requested||(requested==='vehicle'&&isVehicle(event.event_type)))
          &&(!cameraInput.value||String(event.camera)===cameraInput.value)
          &&(!colorInput.value||String(event.color||'').toLowerCase().includes(colorInput.value.toLowerCase()))
          &&(!plateInput.value||String(event.plate||'').toLowerCase().includes(plateInput.value.toLowerCase()))
          &&(!from||!stamp||stamp>=from)
          &&(!to||!stamp||stamp<=to)
          &&naturalMatches(event,queryInput.value.trim());
      }});
    }}
    function card(event){{
      const confidence=event.confidence==null?'—':Math.round(Number(event.confidence)*(Number(event.confidence)<=1?100:1))+'%';
      const thumb=event.thumbnail?`<img src="${{event.thumbnail}}" alt="${{event.event_type}} event">`:'<div class="investigation-placeholder">No thumbnail</div>';
      return `<article class="investigation-card" data-event-id="${{event.id}}">
        <div class="investigation-thumb">${{thumb}}<span class="investigation-badge">${{event.event_type.replaceAll('_',' ')}}</span></div>
        <div class="investigation-body">
          <div class="investigation-title"><h3>${{event.event_type.replaceAll('_',' ')}}</h3><label><input class="evidence-checkbox" type="checkbox" ${{selectedEvidence.has(event.id)?'checked':''}}> Evidence</label></div>
          <div class="investigation-meta">Camera ${{event.camera}} · ${{String(event.timestamp||'').replace('T',' ').slice(0,19)}}<br>Confidence ${{confidence}}${{event.color?` · ${{event.color}}`:''}}${{event.plate?` · ${{event.plate}}`:''}}</div>
          <div class="investigation-card-actions"><a class="primary" href="${{event.recording||'/playback'}}">Playback</a><button class="bookmark-investigation" type="button">${{event.review?.bookmarked?'Bookmarked':'Bookmark'}}</button><a href="/camera/${{event.camera}}">Live camera</a></div>
        </div>
      </article>`;
    }}
    function render(){{
      const results=visibleEvents();
      grid.innerHTML=results.map(card).join('')||'<div class="investigation-empty">No events matched this investigation.</div>';
      document.getElementById('investigation-result-count').textContent=results.length;
      document.getElementById('investigation-people-count').textContent=results.filter(event=>event.event_type==='person').length;
      document.getElementById('investigation-vehicle-count').textContent=results.filter(event=>isVehicle(event.event_type)).length;
      document.getElementById('investigation-bookmark-count').textContent=results.filter(event=>event.review?.bookmarked).length;

      grid.querySelectorAll('.investigation-card').forEach(cardElement=>{{
        const event=investigationEvents.find(item=>item.id===cardElement.dataset.eventId);
        cardElement.querySelector('.evidence-checkbox').addEventListener('change',change=>{{
          if(change.target.checked)selectedEvidence.add(event.id);else selectedEvidence.delete(event.id);
        }});
        cardElement.querySelector('.bookmark-investigation').addEventListener('click',async click=>{{
          const payload={{event_id:event.id,acknowledged:Boolean(event.review?.acknowledged),bookmarked:true,false_positive:Boolean(event.review?.false_positive),tags:event.review?.tags||[],notes:event.review?.notes||''}};
          const response=await fetch(`/api/analytics/events/${{event.id}}/review`,{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
          const result=await response.json();
          if(!response.ok||result.status!=='complete')return showToast(result.message||'Bookmark failed.');
          event.review=result.review;click.currentTarget.textContent='Bookmarked';render();showToast('Event bookmarked.');
        }});
      }});
    }}
    function clearFilters(){{[queryInput,typeInput,cameraInput,colorInput,plateInput,fromInput,toInput].forEach(input=>input.value='');render()}}
    document.getElementById('run-investigation').addEventListener('click',render);
    document.getElementById('clear-investigation').addEventListener('click',clearFilters);
    queryInput.addEventListener('keydown',event=>{{if(event.key==='Enter')render()}});
    document.getElementById('select-visible-evidence').addEventListener('click',()=>{{visibleEvents().forEach(event=>selectedEvidence.add(event.id));render()}});
    document.getElementById('clear-evidence-selection').addEventListener('click',()=>{{selectedEvidence.clear();render()}});
    document.getElementById('create-case-from-evidence').addEventListener('click',async()=>{{
      const selected=investigationEvents.filter(event=>selectedEvidence.has(event.id));
      if(!selected.length)return showToast('Select at least one event first.');
      const title=prompt('Case title');
      if(!title)return;
      const response=await fetch('/api/investigation-cases',{{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{
          title,
          description:queryInput.value.trim(),
          priority:'normal',
          status:'open',
          assigned_to:'',
          tags:[],
          notes:'',
          event_ids:selected.map(event=>event.id)
        }})
      }});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not create case.');
      showToast('Case created.');location.href='/investigation-cases';
    }});

    document.getElementById('export-evidence').addEventListener('click',()=>{{
      const selected=investigationEvents.filter(event=>selectedEvidence.has(event.id));
      if(!selected.length)return showToast('Select at least one event first.');
      const manifest={{product:'AnyAiCam VMS',exported_at:new Date().toISOString(),query:queryInput.value.trim(),filters:{{event_type:typeInput.value,camera:cameraInput.value,color:colorInput.value,plate:plateInput.value,from:fromInput.value,to:toInput.value}},events:selected}};
      const blob=new Blob([JSON.stringify(manifest,null,2)],{{type:'application/json'}});
      const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='anyaicam_evidence_'+new Date().toISOString().replace(/[:.]/g,'-')+'.json';document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(link.href);
    }});
    render();
    </script>"""
    return page_shell("Investigate", "investigate", content, scripts)



@app.get("/api/investigation-cases")
def investigation_cases_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")
    cases = load_investigation_cases()
    return {
        "cases": sorted(
            cases.values(),
            key=lambda item: item.get("updated_at", item.get("created_at", "")),
            reverse=True,
        )
    }


@app.post("/api/investigation-cases")
def create_investigation_case(
    payload: InvestigationCaseCreateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")

    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Case title is required.")

    now = datetime.now().isoformat()
    case_id = uuid.uuid4().hex
    case = {
        "id": case_id,
        "title": title,
        "description": payload.description.strip(),
        "priority": payload.priority if payload.priority in {"low", "normal", "high", "critical"} else "normal",
        "status": payload.status if payload.status in {"open", "in_review", "closed", "archived"} else "open",
        "assigned_to": payload.assigned_to.strip(),
        "tags": sorted(set(tag.strip() for tag in payload.tags if tag.strip())),
        "notes": payload.notes.strip(),
        "event_ids": list(dict.fromkeys(payload.event_ids)),
        "created_at": now,
        "updated_at": now,
        "created_by": user.get("id"),
        "history": [
            case_history_entry(
                "case_created",
                user,
                f"Created with {len(payload.event_ids)} evidence event(s).",
            )
        ],
    }
    cases = load_investigation_cases()
    cases[case_id] = case
    save_investigation_cases(cases)
    append_audit_log(
        "investigation.case_created",
        user=user,
        details={"case_id": case_id, "title": title},
    )
    return {"status": "complete", "case": case, "message": "Investigation case created."}


@app.get("/api/investigation-cases/{case_id}")
def investigation_case_detail_api(case_id: str, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")
    case = load_investigation_cases().get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Investigation case not found.")
    return {"case": case}


@app.put("/api/investigation-cases/{case_id}")
def update_investigation_case(
    case_id: str,
    payload: InvestigationCaseUpdateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")

    cases = load_investigation_cases()
    case = cases.get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Investigation case not found.")

    changes = []
    for field in (
        "title", "description", "priority", "status",
        "assigned_to", "tags", "notes", "event_ids",
    ):
        value = getattr(payload, field)
        if value is None:
            continue
        if field == "title":
            value = value.strip()
            if not value:
                raise HTTPException(status_code=400, detail="Case title is required.")
        elif field in {"description", "assigned_to", "notes"}:
            value = value.strip()
        elif field == "priority" and value not in {"low", "normal", "high", "critical"}:
            raise HTTPException(status_code=400, detail="Invalid priority.")
        elif field == "status" and value not in {"open", "in_review", "closed", "archived"}:
            raise HTTPException(status_code=400, detail="Invalid status.")
        elif field == "tags":
            value = sorted(set(tag.strip() for tag in value if tag.strip()))
        elif field == "event_ids":
            value = list(dict.fromkeys(value))
        if case.get(field) != value:
            case[field] = value
            changes.append(field)

    case["updated_at"] = datetime.now().isoformat()
    case.setdefault("history", []).append(
        case_history_entry(
            "case_updated",
            user,
            "Updated: " + ", ".join(changes) if changes else "No field changes.",
        )
    )
    cases[case_id] = case
    save_investigation_cases(cases)
    append_audit_log(
        "investigation.case_updated",
        user=user,
        details={"case_id": case_id, "fields": changes},
    )
    return {"status": "complete", "case": case, "message": "Investigation case updated."}


@app.get("/api/investigation-cases/{case_id}/export")
def export_investigation_case(case_id: str, request: Request) -> Response:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")

    case = load_investigation_cases().get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Investigation case not found.")

    event_lookup = {}
    for event in load_motion_events() + analytics_events():
        event_id = str(event.get("id") or "")
        if event_id:
            event_lookup[event_id] = event

    manifest = {
        "product": "AnyAiCam VMS",
        "exported_at": datetime.now().isoformat(),
        "exported_by": {
            "id": user.get("id"),
            "name": user.get("display_name") or user.get("email"),
        },
        "case": case,
        "events": [
            event_lookup[event_id]
            for event_id in case.get("event_ids", [])
            if event_id in event_lookup
        ],
    }
    filename = f"anyaicam_case_{case_id[:8]}.json"
    return Response(
        json.dumps(manifest, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )



@app.get("/api/evidence")
def evidence_records_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")
    records = list(load_evidence_hashes().values())
    records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {"evidence": records}


@app.get("/api/evidence/ledger")
def evidence_ledger_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")
    return {"ledger": list(reversed(load_evidence_ledger()))}


@app.post("/api/evidence/verify")
def verify_evidence_api(payload: EvidenceVerifyModel, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")
    return verify_evidence_record(
        request=request,
        user=user,
        evidence_id=payload.evidence_id,
    )


@app.post("/api/evidence/action")
def evidence_action_api(payload: EvidenceActionModel, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")
    entry = append_evidence_ledger(
        request=request,
        user=user,
        action=payload.action.strip() or "viewed",
        evidence_id=payload.evidence_id,
        case_id=payload.case_id,
        event_id=payload.event_id,
        details=payload.details,
    )
    return {"status": "complete", "entry": entry}


@app.get("/api/investigation-cases/{case_id}/custody-package")
def investigation_case_custody_package(case_id: str, request: Request) -> Response:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")

    case = load_investigation_cases().get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Investigation case not found.")

    event_lookup = {
        str(event.get("id")): event
        for event in load_motion_events() + analytics_events()
        if event.get("id")
    }
    buffer = io.BytesIO()
    evidence_manifest = []
    package_id = uuid.uuid4().hex

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        case_events = [
            event_lookup[event_id]
            for event_id in case.get("event_ids", [])
            if event_id in event_lookup
        ]
        archive.writestr(
            "case_manifest.json",
            json.dumps(
                {
                    "product": "AnyAiCam VMS",
                    "package_id": package_id,
                    "exported_at": datetime.now().isoformat(),
                    "exported_by": {
                        "id": user.get("id"),
                        "name": user.get("display_name") or user.get("email"),
                    },
                    "case": case,
                    "events": case_events,
                },
                indent=2,
                default=str,
            ),
        )

        for index, event in enumerate(case_events, start=1):
            event_id = str(event.get("id") or "")
            for kind, raw_path in (
                ("thumbnail", str(event.get("thumbnail") or "")),
                ("recording", str(event.get("linked_recording") or "")),
            ):
                if not raw_path.startswith("/"):
                    continue
                source_path = Path("/app") / raw_path.lstrip("/")
                if not source_path.exists() or not source_path.is_file():
                    continue
                archive_name = f"{kind}s/{index:03d}_{source_path.name}"
                archive.write(source_path, archive_name)
                file_hash, file_size = sha256_file(source_path)
                evidence_id = f"{package_id}:{event_id}:{kind}"
                evidence_manifest.append(
                    {
                        "evidence_id": evidence_id,
                        "event_id": event_id,
                        "case_id": case_id,
                        "archive_name": archive_name,
                        "source_path": str(source_path),
                        "sha256": file_hash,
                        "size_bytes": file_size,
                    }
                )
                append_evidence_ledger(
                    request=request,
                    user=user,
                    action="exported_to_case_package",
                    evidence_id=evidence_id,
                    case_id=case_id,
                    event_id=event_id,
                    file_path=str(source_path),
                    hash_value=file_hash,
                    details=archive_name,
                )

        ledger = [
            entry
            for entry in load_evidence_ledger()
            if entry.get("case_id") == case_id
        ]
        archive.writestr(
            "hash_manifest.json",
            json.dumps(evidence_manifest, indent=2, default=str),
        )
        archive.writestr(
            "chain_of_custody.json",
            json.dumps(ledger, indent=2, default=str),
        )
        archive.writestr(
            "chain_of_custody.txt",
            "\n".join(
                f"{item.get('timestamp')} | {item.get('action')} | "
                f"{item.get('user_name')} | {item.get('evidence_id')} | "
                f"{item.get('ip_address')}"
                for item in ledger
            ),
        )

    package_path = RECORDINGS_FOLDER / f"custody_package_{case_id[:8]}_{package_id[:8]}.zip"
    package_path.write_bytes(buffer.getvalue())
    register_evidence_file(
        request=request,
        user=user,
        path=package_path,
        evidence_id=package_id,
        case_id=case_id,
        action="case_package_exported",
    )

    return FileResponse(
        package_path,
        media_type="application/zip",
        filename=package_path.name,
    )


@app.get("/investigation-cases/{case_id}/custody", response_class=HTMLResponse)
def chain_of_custody_report(case_id: str, request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        return permission_denied_page("Chain of custody", "cases", "view_analytics")

    case = load_investigation_cases().get(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Investigation case not found.")

    entries = [
        entry
        for entry in load_evidence_ledger()
        if entry.get("case_id") == case_id
    ]
    rows = "".join(
        f"<tr><td>{escape(str(item.get('timestamp', '')))}</td>"
        f"<td>{escape(str(item.get('action', '')).replace('_', ' '))}</td>"
        f"<td>{escape(str(item.get('user_name', '')))}</td>"
        f"<td>{escape(str(item.get('evidence_id', '')))}</td>"
        f"<td>{escape(str(item.get('ip_address', '')))}</td>"
        f"<td>{escape(str(item.get('sha256', '')))}</td></tr>"
        for item in entries
    )
    content = f"""<header class="topbar"><div><p class="eyebrow">Evidence integrity</p><h1>Chain of custody</h1></div><button onclick="window.print()">Print</button></header>
    <section class="panel">
      <h2>{escape(case.get('title', 'Case'))}</h2>
      <p><strong>Case ID:</strong> {escape(case_id)}</p>
      <p><strong>Status:</strong> {escape(case.get('status', 'open'))}</p>
      <table><thead><tr><th>Time</th><th>Action</th><th>User</th><th>Evidence</th><th>IP</th><th>SHA-256</th></tr></thead><tbody>{rows or '<tr><td colspan="6">No custody events recorded.</td></tr>'}</tbody></table>
    </section>"""
    return page_shell("Chain of custody", "cases", content)


@app.get("/evidence-integrity", response_class=HTMLResponse)
def evidence_integrity_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        return permission_denied_page("Evidence integrity", "cases", "view_analytics")

    content = """<header class="topbar"><div><p class="eyebrow">Evidence integrity</p><h1>Verify evidence</h1></div></header>
    <section class="custody-grid">
      <article class="custody-card"><h2>Evidence records</h2><div id="evidence-records">Loading…</div></article>
      <article class="custody-card"><h2>Chain-of-custody ledger</h2><div class="custody-ledger" id="custody-ledger">Loading…</div></article>
    </section>"""
    scripts = """<script>
    const recordsHost=document.getElementById('evidence-records');
    const ledgerHost=document.getElementById('custody-ledger');

    function escEvidence(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    async function loadEvidence(){
      const response=await fetch('/api/evidence');
      const data=await response.json();
      recordsHost.innerHTML=(data.evidence||[]).map(item=>`
        <div class="case-card">
          <strong>${escEvidence(item.file_name||item.evidence_id)}</strong>
          <div class="custody-meta">${escEvidence(item.evidence_id)}<br>${escEvidence(item.sha256)}<br>${item.size_bytes||0} bytes</div>
          <div class="custody-actions"><button onclick="verifyEvidence('${item.evidence_id}')">Verify Evidence</button></div>
          <div id="verify-${item.evidence_id}"></div>
        </div>`).join('')||'<div class="empty">No registered evidence yet.</div>';
    }

    async function loadLedger(){
      const response=await fetch('/api/evidence/ledger');
      const data=await response.json();
      ledgerHost.innerHTML=(data.ledger||[]).slice(0,200).map(item=>`
        <div class="custody-entry"><strong>${escEvidence(item.action.replaceAll('_',' '))}</strong>
        <div class="custody-meta">${escEvidence(item.timestamp)} · ${escEvidence(item.user_name)} · ${escEvidence(item.ip_address)}<br>${escEvidence(item.evidence_id)}${item.case_id?' · Case '+escEvidence(item.case_id):''}</div></div>`).join('')||'<div class="empty">No ledger records.</div>';
    }

    async function verifyEvidence(id){
      const response=await fetch('/api/evidence/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({evidence_id:id})});
      const data=await response.json();
      const host=document.getElementById(`verify-${id}`);
      host.innerHTML=`<span class="custody-status ${data.status}">${data.status}</span><div class="custody-meta">${escEvidence(data.message||'')}</div>`;
      loadLedger();
    }

    loadEvidence();loadLedger();
    </script>"""
    return page_shell("Evidence integrity", "cases", content, scripts)



@app.get("/api/incident-reports")
def incident_reports_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")
    reports = list(load_incident_reports().values())
    reports.sort(key=lambda item: item.get("updated_at", item.get("created_at", "")), reverse=True)
    return {"reports": reports}


@app.post("/api/incident-reports")
def create_incident_report(payload: IncidentReportCreateModel, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")

    case = load_investigation_cases().get(payload.case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Investigation case not found.")

    event_lookup = {
        str(event.get("id")): event
        for event in load_motion_events() + analytics_events()
        if event.get("id")
    }
    events = [
        event_lookup[event_id]
        for event_id in case.get("event_ids", [])
        if event_id in event_lookup
    ]
    draft = build_incident_report_draft(case, events, user)
    if payload.title.strip():
        draft["title"] = payload.title.strip()
    if payload.incident_summary.strip():
        draft["incident_summary"] = payload.incident_summary.strip()
    if payload.investigator_observations.strip():
        draft["investigator_observations"] = payload.investigator_observations.strip()
    if payload.recommended_follow_up.strip():
        draft["recommended_follow_up"] = payload.recommended_follow_up.strip()

    now = datetime.now().isoformat()
    report_id = uuid.uuid4().hex
    report = {
        "id": report_id,
        "case_id": payload.case_id,
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "revisions": [],
        **draft,
    }
    reports = load_incident_reports()
    reports[report_id] = report
    save_incident_reports(reports)
    return {"status": "complete", "report": report, "message": "Incident report draft created."}


@app.get("/api/incident-reports/{report_id}")
def incident_report_detail(report_id: str, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")
    report = load_incident_reports().get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Incident report not found.")
    return {"report": report}


@app.put("/api/incident-reports/{report_id}")
def update_incident_report(
    report_id: str,
    payload: IncidentReportUpdateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")

    reports = load_incident_reports()
    report = reports.get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Incident report not found.")

    report.setdefault("revisions", []).append({
        "version": report.get("version", 1),
        "saved_at": datetime.now().isoformat(),
        "saved_by": user.get("id"),
        "snapshot": {
            "title": report.get("title", ""),
            "incident_summary": report.get("incident_summary", ""),
            "investigator_observations": report.get("investigator_observations", ""),
            "recommended_follow_up": report.get("recommended_follow_up", ""),
            "status": report.get("status", "draft"),
        },
    })

    for field in (
        "title",
        "incident_summary",
        "investigator_observations",
        "recommended_follow_up",
        "status",
    ):
        value = getattr(payload, field)
        if value is not None:
            if field == "status" and value not in {"draft", "approved", "archived"}:
                raise HTTPException(status_code=400, detail="Invalid report status.")
            report[field] = value.strip() if isinstance(value, str) else value

    report["approved"] = report.get("status") == "approved"
    report["version"] = int(report.get("version", 1)) + 1
    report["updated_at"] = datetime.now().isoformat()
    report["updated_by"] = user.get("id")
    reports[report_id] = report
    save_incident_reports(reports)
    return {"status": "complete", "report": report, "message": "Incident report saved."}


@app.get("/api/incident-reports/{report_id}/export/{format_name}")
def export_incident_report(
    report_id: str,
    format_name: str,
    request: Request,
) -> Response:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        raise HTTPException(status_code=403, detail="Analytics permission is required.")
    report = load_incident_reports().get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Incident report not found.")

    if format_name == "json":
        return Response(
            json.dumps(report, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="incident_report_{report_id[:8]}.json"'},
        )
    if format_name == "txt":
        timeline = "\n".join(
            f"{item.get('timestamp')} | Camera {item.get('camera')} | {item.get('event_type')} | {item.get('confidence')}"
            for item in report.get("timeline", [])
        )
        body = (
            f"{report.get('title')}\n\n"
            f"AI-GENERATED DRAFT: {report.get('ai_generated')}\n"
            f"Status: {report.get('status')}\n"
            f"Version: {report.get('version')}\n\n"
            f"Incident Summary\n{report.get('incident_summary','')}\n\n"
            f"Timeline of Events\n{timeline}\n\n"
            f"Investigator Observations\n{report.get('investigator_observations','')}\n\n"
            f"Recommended Follow-Up\n{report.get('recommended_follow_up','')}\n"
        )
        return Response(
            body,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="incident_report_{report_id[:8]}.txt"'},
        )
    raise HTTPException(status_code=400, detail="Unsupported export format.")


@app.get("/incident-reports/{report_id}/print", response_class=HTMLResponse)
def print_incident_report(report_id: str, request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        return permission_denied_page("Incident report", "reports", "view_analytics")
    report = load_incident_reports().get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Incident report not found.")

    timeline_rows = "".join(
        f"<tr><td>{escape(str(item.get('timestamp','')))}</td>"
        f"<td>{escape(str(item.get('camera','')))}</td>"
        f"<td>{escape(str(item.get('event_type','')))}</td>"
        f"<td>{escape(str(item.get('confidence','')))}</td></tr>"
        for item in report.get("timeline", [])
    )
    content = f"""<header class="topbar"><div><p class="eyebrow">Incident report</p><h1>{escape(report.get('title','Report'))}</h1></div><button onclick="window.print()">Print</button></header>
    <section class="panel">
      <div class="report-warning">AI-generated draft. Human review and approval are required.</div>
      <p><strong>Status:</strong> {escape(report.get('status','draft'))}</p>
      <p><strong>Version:</strong> {escape(str(report.get('version',1)))}</p>
      <h2>Incident summary</h2><p>{escape(report.get('incident_summary',''))}</p>
      <h2>Timeline of events</h2><table><thead><tr><th>Time</th><th>Camera</th><th>Type</th><th>Confidence</th></tr></thead><tbody>{timeline_rows}</tbody></table>
      <h2>People and vehicles involved</h2><p>People detections: {report.get('people_count',0)} · Vehicle detections: {report.get('vehicle_count',0)}</p>
      <h2>Investigator observations</h2><p>{escape(report.get('investigator_observations',''))}</p>
      <h2>Recommended follow-up</h2><p>{escape(report.get('recommended_follow_up',''))}</p>
    </section>"""
    return page_shell("Incident report", "reports", content)


@app.get("/incident-reports", response_class=HTMLResponse)
def incident_reports_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        return permission_denied_page("Incident reports", "reports", "view_analytics")

    case_options = "".join(
        f'<option value="{escape(case_id)}">{escape(case.get("title","Untitled case"))}</option>'
        for case_id, case in load_investigation_cases().items()
    )
    content = f"""<header class="topbar"><div><p class="eyebrow">AI-assisted reporting</p><h1>Incident reports</h1></div></header>
    <section class="report-layout">
      <form class="case-form" id="report-create-form">
        <h2>Create draft</h2>
        <div class="report-warning">Reports are AI-generated drafts and must be reviewed before approval.</div>
        <label>Case<select id="report-case" required><option value="">Select case</option>{case_options}</select></label>
        <label>Title<input id="report-title" placeholder="Optional custom title"></label>
        <button class="action-button" type="submit">Generate draft</button>
      </form>
      <div>
        <div class="report-list" id="report-list"><div class="empty">Loading reports…</div></div>
        <section class="panel report-editor" id="report-editor" hidden></section>
      </div>
    </section>"""

    scripts = """<script>
    const reportList=document.getElementById('report-list');
    const reportEditor=document.getElementById('report-editor');
    let reports=[];

    function escReport(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    function renderList(){
      reportList.innerHTML=reports.map(report=>`
        <article class="report-card">
          <div class="case-head"><div><h3>${escReport(report.title)}</h3><div class="case-meta">Case ${escReport(report.case_id)} · Version ${report.version}</div></div><span class="report-status">${escReport(report.status)}</span></div>
          <div class="case-actions"><button onclick="openReport('${report.id}')">Open</button><a href="/incident-reports/${report.id}/print">Print</a><a href="/api/incident-reports/${report.id}/export/json">JSON</a><a href="/api/incident-reports/${report.id}/export/txt">Text</a></div>
        </article>`).join('')||'<div class="empty">No incident reports yet.</div>';
    }

    async function loadReports(){
      const response=await fetch('/api/incident-reports');
      const data=await response.json();
      reports=data.reports||[];
      renderList();
    }

    async function openReport(id){
      const response=await fetch(`/api/incident-reports/${id}`);
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not open report.');
      const report=data.report;
      reportEditor.hidden=false;
      reportEditor.innerHTML=`
        <div class="panel-head"><div><p class="eyebrow">AI-generated draft</p><h2>${escReport(report.title)}</h2></div><span class="report-status">${escReport(report.status)}</span></div>
        <div class="report-warning">Do not approve until a human reviewer confirms the facts.</div>
        <label>Title<input id="edit-report-title" value="${escReport(report.title)}"></label>
        <label>Incident summary<textarea id="edit-report-summary">${escReport(report.incident_summary||'')}</textarea></label>
        <label>Investigator observations<textarea id="edit-report-observations">${escReport(report.investigator_observations||'')}</textarea></label>
        <label>Recommended follow-up<textarea id="edit-report-follow-up">${escReport(report.recommended_follow_up||'')}</textarea></label>
        <div><h3>Timeline of events</h3><div class="report-timeline">${(report.timeline||[]).map(item=>`<div class="report-timeline-row"><strong>${escReport(item.timestamp)}</strong><div class="case-meta">Camera ${escReport(item.camera)} · ${escReport(item.event_type)} · ${escReport(item.confidence??'—')}</div></div>`).join('')||'<div class="empty">No events.</div>'}</div></div>
        <div class="case-actions"><button class="action-button" onclick="saveReport('${report.id}','draft')">Save draft</button><button onclick="saveReport('${report.id}','approved')">Approve</button><button onclick="saveReport('${report.id}','draft')">Reopen</button><button onclick="saveReport('${report.id}','archived')">Archive</button></div>`;
      reportEditor.scrollIntoView({behavior:'smooth',block:'start'});
    }

    async function saveReport(id,status){
      const payload={
        title:document.getElementById('edit-report-title').value,
        incident_summary:document.getElementById('edit-report-summary').value,
        investigator_observations:document.getElementById('edit-report-observations').value,
        recommended_follow_up:document.getElementById('edit-report-follow-up').value,
        status
      };
      const response=await fetch(`/api/incident-reports/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not save report.');
      showToast(data.message);await loadReports();openReport(id);
    }

    document.getElementById('report-create-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const payload={case_id:document.getElementById('report-case').value,title:document.getElementById('report-title').value};
      const response=await fetch('/api/incident-reports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not create report.');
      showToast(data.message);event.currentTarget.reset();await loadReports();openReport(data.report.id);
    });

    loadReports();
    </script>"""
    return page_shell("Incident reports", "reports", content, scripts)



@app.get("/api/notification-rules")
def notification_rules_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    rules = list(load_notification_rules().values())
    rules.sort(key=lambda item: item.get("updated_at", item.get("created_at", "")), reverse=True)
    return {"rules": rules, "channels": notification_channel_status()}


@app.post("/api/notification-rules")
def create_notification_rule(payload: NotificationRuleCreateModel, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Rule name is required.")
    now = datetime.now().isoformat()
    rule_id = uuid.uuid4().hex
    rule = {
        "id": rule_id,
        "name": name,
        "enabled": payload.enabled,
        "event_types": sorted(set(item.strip().lower() for item in payload.event_types if item.strip())),
        "camera_ids": sorted(set(int(item) for item in payload.camera_ids if 1 <= int(item) <= CAMERA_COUNT)),
        "minimum_confidence": max(0.0, min(1.0, float(payload.minimum_confidence))),
        "severity": payload.severity if payload.severity in {"low", "normal", "high", "critical"} else "normal",
        "channels": sorted(set(item for item in payload.channels if item in {"email", "push", "sms", "webhook", "in_app"})),
        "recipients": sorted(set(item.strip() for item in payload.recipients if item.strip())),
        "quiet_hours_start": payload.quiet_hours_start.strip(),
        "quiet_hours_end": payload.quiet_hours_end.strip(),
        "cooldown_seconds": max(0, int(payload.cooldown_seconds)),
        "created_at": now,
        "updated_at": now,
        "created_by": user.get("id"),
    }
    rules = load_notification_rules()
    rules[rule_id] = rule
    save_notification_rules(rules)
    return {"status": "complete", "rule": rule, "message": "Notification rule created."}


@app.put("/api/notification-rules/{rule_id}")
def update_notification_rule(rule_id: str, payload: NotificationRuleUpdateModel, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    rules = load_notification_rules()
    rule = rules.get(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Notification rule not found.")
    for field in ("name","enabled","event_types","camera_ids","minimum_confidence","severity","channels","recipients","quiet_hours_start","quiet_hours_end","cooldown_seconds"):
        value = getattr(payload, field)
        if value is None:
            continue
        if field == "name":
            value = value.strip()
            if not value:
                raise HTTPException(status_code=400, detail="Rule name is required.")
        elif field == "event_types":
            value = sorted(set(item.strip().lower() for item in value if item.strip()))
        elif field == "camera_ids":
            value = sorted(set(int(item) for item in value if 1 <= int(item) <= CAMERA_COUNT))
        elif field == "minimum_confidence":
            value = max(0.0, min(1.0, float(value)))
        elif field == "severity" and value not in {"low","normal","high","critical"}:
            raise HTTPException(status_code=400, detail="Invalid severity.")
        elif field == "channels":
            value = sorted(set(item for item in value if item in {"email","push","sms","webhook","in_app"}))
        elif field == "recipients":
            value = sorted(set(item.strip() for item in value if item.strip()))
        elif field in {"quiet_hours_start","quiet_hours_end"}:
            value = value.strip()
        elif field == "cooldown_seconds":
            value = max(0, int(value))
        rule[field] = value
    rule["updated_at"] = datetime.now().isoformat()
    rule["updated_by"] = user.get("id")
    rules[rule_id] = rule
    save_notification_rules(rules)
    return {"status":"complete","rule":rule,"message":"Notification rule updated."}


@app.post("/api/notification-rules/test")
def test_notification_rule(payload: NotificationTestModel, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    rule = load_notification_rules().get(payload.rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Notification rule not found.")
    return {"status":"complete","deliveries":simulate_notification_delivery(rule,user),"message":"Notification test completed."}


@app.get("/api/notification-deliveries")
def notification_deliveries_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    return {"deliveries": list(reversed(load_notification_deliveries()))[:500]}


@app.get("/enterprise-notifications", response_class=HTMLResponse)
def enterprise_notifications_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page("Enterprise notifications","enterprise-notifications","manage_settings")
    camera_options = "".join(f'<option value="{camera}">Camera {camera}</option>' for camera in range(1,CAMERA_COUNT+1))
    content = f"""<header class="topbar"><div><p class="eyebrow">Rules and delivery</p><h1>Enterprise notifications</h1></div></header>
    <section class="channel-status" id="channel-status"></section>
    <section class="notification-layout">
      <form class="notification-rule-form" id="notification-rule-form">
        <h2>Create rule</h2>
        <label>Rule name<input id="notification-name" required placeholder="Critical person detection"></label>
        <label>Event types<select id="notification-event-types" multiple><option value="motion">Motion</option><option value="person">Person</option><option value="vehicle">Vehicle</option><option value="plate">License plate</option><option value="intrusion">Intrusion</option><option value="line_crossing">Line crossing</option></select></label>
        <label>Cameras<select id="notification-cameras" multiple>{camera_options}</select></label>
        <label>Minimum confidence<input id="notification-confidence" type="number" min="0" max="1" step="0.05" value="0.5"></label>
        <label>Severity<select id="notification-severity"><option value="low">Low</option><option value="normal" selected>Normal</option><option value="high">High</option><option value="critical">Critical</option></select></label>
        <label>Channels<select id="notification-channels" multiple><option value="in_app">In-app</option><option value="email">Email</option><option value="push">Push</option><option value="sms">SMS placeholder</option><option value="webhook">Webhook placeholder</option></select></label>
        <label>Recipients<input id="notification-recipients" placeholder="email@example.com, team@example.com"></label>
        <label>Quiet hours start<input id="notification-quiet-start" type="time"></label>
        <label>Quiet hours end<input id="notification-quiet-end" type="time"></label>
        <label>Cooldown seconds<input id="notification-cooldown" type="number" min="0" value="300"></label>
        <label><input id="notification-enabled" type="checkbox" checked> Enabled</label>
        <button class="action-button" type="submit">Create rule</button>
      </form>
      <div><div class="notification-grid" id="notification-grid"><div class="empty">Loading rules…</div></div>
        <section class="panel"><div class="panel-head"><div><h2>Delivery log</h2><div class="health-detail">SMS and webhook remain placeholders until providers are configured.</div></div></div><div id="notification-deliveries"><div class="empty">Loading delivery history…</div></div></section>
      </div>
    </section>"""
    scripts = """<script>
    const ruleGrid=document.getElementById('notification-grid'),deliveryHost=document.getElementById('notification-deliveries'),channelHost=document.getElementById('channel-status');let notificationRules=[];
    function escNotification(value){return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]))}
    function selectedValues(id){return [...document.getElementById(id).selectedOptions].map(option=>option.value)}
    function renderChannels(channels){channelHost.innerHTML=Object.entries(channels).map(([name,ready])=>`<div class="stat"><span class="stat-label">${escNotification(name.replaceAll('_',' '))}</span><span class="stat-value">${ready?'Ready':'Not configured'}</span></div>`).join('')}
    function renderRules(){ruleGrid.innerHTML=notificationRules.map(rule=>`<article class="notification-card"><div class="notification-card-head"><div><h3>${escNotification(rule.name)}</h3><div class="notification-meta">Events: ${(rule.event_types||[]).map(escNotification).join(', ')||'All'}<br>Cameras: ${(rule.camera_ids||[]).join(', ')||'All'} · Confidence: ${Math.round((rule.minimum_confidence||0)*100)}%</div></div><div class="notification-badges"><span class="notification-badge">${escNotification(rule.severity)}</span><span class="notification-badge">${rule.enabled?'enabled':'disabled'}</span></div></div><div class="notification-meta">Channels: ${(rule.channels||[]).map(escNotification).join(', ')||'None'}<br>Recipients: ${(rule.recipients||[]).map(escNotification).join(', ')||'Current user'}<br>Quiet hours: ${escNotification(rule.quiet_hours_start||'—')}–${escNotification(rule.quiet_hours_end||'—')} · Cooldown: ${rule.cooldown_seconds||0}s</div><div class="notification-actions"><button onclick="testRule('${rule.id}')">Send test</button><button onclick="toggleRule('${rule.id}',${!rule.enabled})">${rule.enabled?'Disable':'Enable'}</button></div></article>`).join('')||'<div class="empty">No notification rules yet.</div>'}
    async function loadRules(){const response=await fetch('/api/notification-rules'),data=await response.json();notificationRules=data.rules||[];renderChannels(data.channels||{});renderRules()}
    async function loadDeliveries(){const response=await fetch('/api/notification-deliveries'),data=await response.json();deliveryHost.innerHTML=`<table class="delivery-table"><thead><tr><th>Time</th><th>Rule</th><th>Channel</th><th>Recipient</th><th>Status</th></tr></thead><tbody>${(data.deliveries||[]).map(item=>`<tr><td>${escNotification((item.created_at||'').replace('T',' ').slice(0,19))}</td><td>${escNotification(item.rule_name)}</td><td>${escNotification(item.channel)}</td><td>${escNotification(item.recipient)}</td><td>${escNotification(item.status)}</td></tr>`).join('')||'<tr><td colspan="5">No delivery records.</td></tr>'}</tbody></table>`}
    async function testRule(id){const response=await fetch('/api/notification-rules/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rule_id:id})}),data=await response.json();if(!response.ok)return showToast(data.detail||'Test failed.');showToast(data.message);loadDeliveries()}
    async function toggleRule(id,enabled){const response=await fetch(`/api/notification-rules/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})}),data=await response.json();if(!response.ok)return showToast(data.detail||'Update failed.');showToast(data.message);loadRules()}
    document.getElementById('notification-rule-form').addEventListener('submit',async event=>{event.preventDefault();const payload={name:document.getElementById('notification-name').value,enabled:document.getElementById('notification-enabled').checked,event_types:selectedValues('notification-event-types'),camera_ids:selectedValues('notification-cameras').map(Number),minimum_confidence:Number(document.getElementById('notification-confidence').value||0),severity:document.getElementById('notification-severity').value,channels:selectedValues('notification-channels'),recipients:document.getElementById('notification-recipients').value.split(',').map(value=>value.trim()).filter(Boolean),quiet_hours_start:document.getElementById('notification-quiet-start').value,quiet_hours_end:document.getElementById('notification-quiet-end').value,cooldown_seconds:Number(document.getElementById('notification-cooldown').value||0)};const response=await fetch('/api/notification-rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),data=await response.json();if(!response.ok)return showToast(data.detail||'Could not create rule.');showToast(data.message);event.currentTarget.reset();document.getElementById('notification-enabled').checked=true;loadRules()});
    loadRules();loadDeliveries();
    </script>"""
    return page_shell("Enterprise notifications","enterprise-notifications",content,scripts)



@app.get("/api/mobile/devices")
def mobile_devices_api(request: Request) -> dict:
    user = current_user(request)
    devices = [
        device
        for device in load_mobile_devices().values()
        if mobile_device_owner_matches(device, user)
    ]
    devices.sort(key=lambda item: item.get("last_seen_at", item.get("created_at", "")), reverse=True)
    return {"devices": devices}


@app.post("/api/mobile/pairing")
def create_mobile_pairing(
    payload: MobilePairingCreateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    record = create_mobile_pairing_code(user, payload.device_name)
    return {
        "status": "complete",
        "pairing": record,
        "message": "Pairing code created. It expires in 10 minutes.",
    }


@app.post("/api/mobile/pairing/claim")
def claim_mobile_pairing(
    payload: MobilePairingClaimModel,
    request: Request,
) -> dict:
    code = payload.code.strip()
    codes = cleanup_mobile_pairing_codes(load_mobile_pairing_codes())
    record = codes.get(code)
    if not record:
        raise HTTPException(status_code=400, detail="Pairing code is invalid or expired.")

    device_id = uuid.uuid4().hex
    now = datetime.now().isoformat()
    device = {
        "id": device_id,
        "user_id": record.get("user_id"),
        "user_email": record.get("user_email"),
        "device_name": payload.device_name.strip() or record.get("device_name") or "Mobile device",
        "platform": payload.platform.strip().lower() or "web",
        "push_subscription": payload.push_subscription,
        "notifications_enabled": bool(payload.push_subscription),
        "revoked": False,
        "created_at": now,
        "last_seen_at": now,
        "paired_from_ip": request_device_context(request).get("ip_address", ""),
        "user_agent": request.headers.get("user-agent", ""),
    }
    devices = load_mobile_devices()
    devices[device_id] = device
    save_mobile_devices(devices)

    record["claimed"] = True
    record["claimed_at"] = now
    record["device_id"] = device_id
    codes[code] = record
    save_mobile_pairing_codes(codes)

    return {
        "status": "complete",
        "device": device,
        "message": "Mobile device paired.",
    }


@app.put("/api/mobile/devices/{device_id}")
def update_mobile_device(
    device_id: str,
    payload: MobileDeviceUpdateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    devices = load_mobile_devices()
    device = devices.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Mobile device not found.")
    if not mobile_device_owner_matches(device, user):
        raise HTTPException(status_code=403, detail="You cannot manage this device.")

    if payload.device_name is not None:
        device["device_name"] = payload.device_name.strip() or device.get("device_name")
    if payload.notifications_enabled is not None:
        device["notifications_enabled"] = payload.notifications_enabled
    if payload.revoked is not None:
        device["revoked"] = payload.revoked
    device["updated_at"] = datetime.now().isoformat()
    devices[device_id] = device
    save_mobile_devices(devices)
    return {"status": "complete", "device": device, "message": "Mobile device updated."}


@app.post("/api/mobile/devices/{device_id}/heartbeat")
def mobile_device_heartbeat(device_id: str, request: Request) -> dict:
    devices = load_mobile_devices()
    device = devices.get(device_id)
    if not device or device.get("revoked"):
        raise HTTPException(status_code=404, detail="Active mobile device not found.")
    device["last_seen_at"] = datetime.now().isoformat()
    device["last_ip"] = request_device_context(request).get("ip_address", "")
    devices[device_id] = device
    save_mobile_devices(devices)
    return {"status": "complete", "server_time": datetime.now().isoformat()}


@app.get("/mobile-pair", response_class=HTMLResponse)
def mobile_pair_page(request: Request) -> str:
    content = """<header class="topbar"><div><p class="eyebrow">Phone pairing</p><h1>Connect this device</h1></div></header>
    <section class="panel">
      <p>Enter the six-digit pairing code shown in AnyAiCam on your trusted computer.</p>
      <form class="case-form" id="mobile-claim-form">
        <label>Pairing code<input id="mobile-claim-code" inputmode="numeric" maxlength="6" required placeholder="000000"></label>
        <label>Device name<input id="mobile-claim-name" required placeholder="My iPhone"></label>
        <label>Platform<select id="mobile-claim-platform"><option value="web">Mobile web</option><option value="ios">iPhone</option><option value="android">Android</option></select></label>
        <button class="action-button" type="submit">Pair device</button>
      </form>
      <div id="mobile-claim-result"></div>
    </section>"""
    scripts = """<script>
    document.getElementById('mobile-claim-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const response=await fetch('/api/mobile/pairing/claim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        code:document.getElementById('mobile-claim-code').value,
        device_name:document.getElementById('mobile-claim-name').value,
        platform:document.getElementById('mobile-claim-platform').value,
        push_subscription:{}
      })});
      const data=await response.json();
      const host=document.getElementById('mobile-claim-result');
      if(!response.ok){host.innerHTML=`<div class="mock-banner">${data.detail||'Pairing failed.'}</div>`;return}
      localStorage.setItem('anyaicam-mobile-device-id',data.device.id);
      host.innerHTML=`<div class="mock-banner">Paired successfully. Device ID: ${data.device.id}</div>`;
    });
    </script>"""
    return page_shell("Connect phone", "phone", content, scripts)


@app.get("/mobile-devices", response_class=HTMLResponse)
def mobile_devices_page(request: Request) -> str:
    user = current_user(request)
    content = """<header class="topbar"><div><p class="eyebrow">Mobile security</p><h1>Mobile devices</h1></div><a class="action-button" href="/mobile-pair">Open pairing page</a></header>
    <section class="mobile-device-layout">
      <article class="mobile-device-card">
        <h2>Create pairing code</h2>
        <p class="health-detail">Codes expire after 10 minutes and can only be used once.</p>
        <label>Device name<input id="pairing-device-name" placeholder="My iPhone"></label>
        <button class="action-button" id="create-pairing-code" type="button">Generate code</button>
        <div class="pairing-code" id="pairing-code">------</div>
        <div class="mobile-device-meta" id="pairing-expiry"></div>
      </article>
      <div class="mobile-device-grid" id="mobile-device-grid"><div class="empty">Loading devices…</div></div>
    </section>"""
    scripts = """<script>
    const deviceGrid=document.getElementById('mobile-device-grid');
    function escMobile(value){return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]))}
    async function loadDevices(){
      const response=await fetch('/api/mobile/devices'),data=await response.json();
      deviceGrid.innerHTML=(data.devices||[]).map(device=>`<article class="mobile-device-card">
        <div class="mobile-device-head"><div><h3>${escMobile(device.device_name)}</h3><div class="mobile-device-meta">${escMobile(device.platform)} · ${escMobile(device.user_email||'')}<br>Last seen: ${escMobile((device.last_seen_at||'Never').replace('T',' ').slice(0,19))}</div></div><span class="mobile-badge">${device.revoked?'revoked':'active'}</span></div>
        <div class="mobile-device-actions"><button onclick="toggleNotifications('${device.id}',${!device.notifications_enabled})">${device.notifications_enabled?'Disable alerts':'Enable alerts'}</button><button onclick="revokeDevice('${device.id}',${!device.revoked})">${device.revoked?'Restore':'Revoke'}</button></div>
      </article>`).join('')||'<div class="empty">No paired mobile devices.</div>';
    }
    async function toggleNotifications(id,value){await fetch(`/api/mobile/devices/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({notifications_enabled:value})});loadDevices()}
    async function revokeDevice(id,value){await fetch(`/api/mobile/devices/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({revoked:value})});loadDevices()}
    document.getElementById('create-pairing-code').addEventListener('click',async()=>{
      const response=await fetch('/api/mobile/pairing',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_name:document.getElementById('pairing-device-name').value||'Mobile device'})});
      const data=await response.json();
      document.getElementById('pairing-code').textContent=data.pairing.code;
      document.getElementById('pairing-expiry').textContent='Expires: '+data.pairing.expires_at.replace('T',' ').slice(0,19);
    });
    loadDevices();
    </script>"""
    return page_shell("Mobile devices", "mobile-devices", content, scripts)



@app.get("/api/release-readiness")
def release_readiness_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    return release_readiness_snapshot()


@app.put("/api/maintenance")
def update_maintenance_mode(
    payload: MaintenanceModeModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")

    state = {
        "enabled": payload.enabled,
        "message": payload.message.strip() or "Scheduled maintenance is in progress.",
        "updated_at": datetime.now().isoformat(),
        "updated_by": user.get("id"),
        "updated_by_name": user.get("display_name") or user.get("email"),
    }
    save_maintenance_state(state)
    structured_log(
        "maintenance.updated",
        enabled=payload.enabled,
        updated_by=user.get("id"),
    )
    return {"status": "complete", "maintenance": state, "message": "Maintenance mode updated."}


@app.get("/api/support-bundle")
def download_support_bundle(request: Request) -> Response:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")

    content = build_support_bundle(user)
    filename = f"anyaicam_support_bundle_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    structured_log(
        "support_bundle.created",
        user_id=user.get("id"),
        filename=filename,
    )
    return Response(
        content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/release-readiness", response_class=HTMLResponse)
def release_readiness_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Release readiness",
            "release-readiness",
            "manage_settings",
        )

    content = """<header class="topbar"><div><p class="eyebrow">Production hardening</p><h1>Release readiness</h1></div></header>
    <section class="release-grid" id="release-summary"></section>
    <section class="panel">
      <div class="panel-head"><div><h2>Readiness checks</h2><div class="health-detail">Review configuration, storage, deployment, and maintenance state before production release.</div></div></div>
      <div class="release-list" id="release-checks"><div class="empty">Running checks…</div></div>
      <div class="release-actions">
        <button id="refresh-release-checks" type="button">Run checks</button>
        <a href="/api/support-bundle">Download support bundle</a>
      </div>
    </section>
    <section class="panel">
      <div class="panel-head"><div><h2>Maintenance mode</h2><div class="health-detail">Use only for planned work. Login and health endpoints remain available.</div></div></div>
      <label><input id="maintenance-enabled" type="checkbox"> Enable maintenance mode</label>
      <label>Message<input id="maintenance-message" value="Scheduled maintenance is in progress."></label>
      <div class="release-actions"><button class="action-button" id="save-maintenance" type="button">Save maintenance setting</button></div>
    </section>"""

    scripts = """<script>
    const summary=document.getElementById('release-summary');
    const checks=document.getElementById('release-checks');

    function escRelease(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    async function loadReadiness(){
      const response=await fetch('/api/release-readiness');
      const data=await response.json();
      const status=data.ready?'ready':((data.critical_issues||[]).length?'blocked':'warning');
      summary.innerHTML=`
        <article class="release-card"><h3>Release status</h3><div class="release-status ${status}">${status}</div></article>
        <article class="release-card"><h3>Environment</h3><div class="release-status">${escRelease(data.environment)}</div><div class="health-detail">${escRelease(data.runtime_role)}</div></article>
        <article class="release-card"><h3>Build</h3><div class="release-status">${escRelease(data.version)}</div><div class="health-detail">${escRelease(data.build_id)}</div></article>`;

      const rows=[];
      (data.critical_issues||[]).forEach(item=>rows.push(`<div class="release-row"><strong>Critical</strong><div class="health-detail">${escRelease(item.message)}</div></div>`));
      (data.warnings||[]).forEach(item=>rows.push(`<div class="release-row"><strong>Warning</strong><div class="health-detail">${escRelease(item.message)}</div></div>`));
      Object.entries(data.paths||{}).forEach(([name,item])=>rows.push(`<div class="release-row"><strong>${escRelease(name.replaceAll('_',' '))}</strong><div class="health-detail">${item.exists&&item.writable?'Ready':'Check required'} · ${escRelease(item.path)}</div></div>`));
      checks.innerHTML=rows.join('')||'<div class="release-row"><strong>All checks passed.</strong></div>';

      document.getElementById('maintenance-enabled').checked=Boolean(data.maintenance?.enabled);
      document.getElementById('maintenance-message').value=data.maintenance?.message||'Scheduled maintenance is in progress.';
    }

    document.getElementById('refresh-release-checks').addEventListener('click',loadReadiness);
    document.getElementById('save-maintenance').addEventListener('click',async()=>{
      const response=await fetch('/api/maintenance',{
        method:'PUT',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          enabled:document.getElementById('maintenance-enabled').checked,
          message:document.getElementById('maintenance-message').value
        })
      });
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not update maintenance mode.');
      showToast(data.message);loadReadiness();
    });

    loadReadiness();
    </script>"""
    return page_shell(
        "Release readiness",
        "release-readiness",
        content,
        scripts,
    )



@app.get("/api/backups")
def backups_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    jobs = list(load_backup_jobs().values())
    jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {
        "backups": jobs,
        "retention_count": BACKUP_RETENTION_COUNT,
        "include_recordings_default": BACKUP_INCLUDE_RECORDINGS,
        "include_hls_default": BACKUP_INCLUDE_HLS,
    }


@app.post("/api/backups")
def create_backup(
    payload: BackupCreateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    job = create_backup_archive(
        label=payload.label,
        include_recordings=payload.include_recordings,
        include_hls=payload.include_hls,
        user=user,
    )
    structured_log(
        "backup.created",
        backup_id=job.get("id"),
        size_bytes=job.get("size_bytes"),
        file_count=job.get("file_count"),
        user_id=user.get("id"),
    )
    return {"status": "complete", "backup": job, "message": "Backup created."}


@app.post("/api/backups/{backup_id}/verify")
def verify_backup(backup_id: str, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    job = load_backup_jobs().get(backup_id)
    if not job:
        raise HTTPException(status_code=404, detail="Backup not found.")
    result = verify_backup_job(job)
    structured_log(
        "backup.verified",
        backup_id=backup_id,
        result=result.get("status"),
        user_id=user.get("id"),
    )
    return result


@app.get("/api/backups/{backup_id}/download")
def download_backup(backup_id: str, request: Request) -> FileResponse:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    job = load_backup_jobs().get(backup_id)
    if not job:
        raise HTTPException(status_code=404, detail="Backup not found.")
    path = Path(str(job.get("file_path") or ""))
    if not path.exists():
        raise HTTPException(status_code=404, detail="Backup archive is missing.")
    structured_log(
        "backup.downloaded",
        backup_id=backup_id,
        user_id=user.get("id"),
    )
    return FileResponse(
        path,
        media_type="application/zip",
        filename=job.get("file_name") or path.name,
    )


@app.post("/api/backups/restore")
def restore_backup(
    payload: BackupRestoreModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    if payload.confirm.strip() != "RESTORE":
        raise HTTPException(
            status_code=400,
            detail='Type RESTORE to confirm validation.',
        )

    job = load_backup_jobs().get(payload.backup_id)
    if not job:
        raise HTTPException(status_code=404, detail="Backup not found.")

    path = Path(str(job.get("file_path") or ""))
    validation = validate_backup_archive(path)
    history = load_backup_restore_history()
    record = {
        "id": uuid.uuid4().hex,
        "backup_id": payload.backup_id,
        "requested_at": datetime.now().isoformat(),
        "requested_by": user.get("id"),
        "requested_by_name": user.get("display_name") or user.get("email"),
        "validation": validation,
        "status": "validated_only" if validation.get("valid") else "failed",
        "message": (
            "Backup validated. Automatic overwrite is intentionally disabled in Phase 22."
            if validation.get("valid")
            else validation.get("message")
        ),
    }
    history.append(record)
    save_backup_restore_history(history[-500:])
    structured_log(
        "backup.restore_validation",
        backup_id=payload.backup_id,
        valid=validation.get("valid"),
        user_id=user.get("id"),
    )
    return {
        "status": "complete" if validation.get("valid") else "failed",
        "restore": record,
        "message": record["message"],
    }


@app.get("/api/backups/restore-history")
def backup_restore_history_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    return {"history": list(reversed(load_backup_restore_history()))[:200]}


@app.get("/backup-restore", response_class=HTMLResponse)
def backup_restore_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Backup and restore",
            "backup-restore",
            "manage_settings",
        )

    content = """<header class="topbar"><div><p class="eyebrow">Operational resilience</p><h1>Backup and restore</h1></div></header>
    <section class="backup-layout">
      <form class="backup-card" id="backup-create-form">
        <h2>Create backup</h2>
        <label>Backup label<input id="backup-label" placeholder="Before production update"></label>
        <label><input id="backup-recordings" type="checkbox"> Include recordings and snapshots</label>
        <label><input id="backup-hls" type="checkbox"> Include temporary HLS files</label>
        <div class="restore-warning">Recordings and HLS files may make the archive very large. Configuration and investigation records are always included.</div>
        <button class="action-button" type="submit">Create backup</button>
      </form>
      <div>
        <div class="backup-grid" id="backup-grid"><div class="empty">Loading backups…</div></div>
        <section class="panel">
          <div class="panel-head"><div><h2>Restore validation history</h2><div class="health-detail">Phase 22 validates backup integrity. It does not automatically overwrite the live system.</div></div></div>
          <div id="restore-history"><div class="empty">Loading restore history…</div></div>
        </section>
      </div>
    </section>"""

    scripts = """<script>
    const backupGrid=document.getElementById('backup-grid');
    const restoreHistory=document.getElementById('restore-history');

    function escBackup(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    async function loadBackups(){
      const response=await fetch('/api/backups');
      const data=await response.json();
      document.getElementById('backup-recordings').checked=Boolean(data.include_recordings_default);
      document.getElementById('backup-hls').checked=Boolean(data.include_hls_default);
      backupGrid.innerHTML=(data.backups||[]).map(item=>`
        <article class="backup-card">
          <div class="backup-head"><div><h3>${escBackup(item.file_name||item.label||item.id)}</h3><div class="backup-meta">${escBackup(item.created_at?.replace('T',' ').slice(0,19))}<br>${item.file_count||0} files · ${item.size_bytes||0} bytes<br>${escBackup(item.sha256||'')}</div></div><span class="backup-status">${escBackup(item.status)}</span></div>
          <div class="backup-actions"><a href="/api/backups/${item.id}/download">Download</a><button onclick="verifyBackup('${item.id}')">Verify</button><button onclick="validateRestore('${item.id}')">Validate restore</button></div>
          <div id="backup-result-${item.id}"></div>
        </article>`).join('')||'<div class="empty">No backups yet.</div>';
    }

    async function loadRestoreHistory(){
      const response=await fetch('/api/backups/restore-history');
      const data=await response.json();
      restoreHistory.innerHTML=(data.history||[]).map(item=>`<div class="release-row"><strong>${escBackup(item.status)}</strong><div class="backup-meta">${escBackup(item.requested_at?.replace('T',' ').slice(0,19))} · ${escBackup(item.requested_by_name)}<br>${escBackup(item.message)}</div></div>`).join('')||'<div class="empty">No restore validations yet.</div>';
    }

    async function verifyBackup(id){
      const response=await fetch(`/api/backups/${id}/verify`,{method:'POST'});
      const data=await response.json();
      document.getElementById(`backup-result-${id}`).innerHTML=`<div class="mock-banner">${escBackup(data.status)} — ${escBackup(data.message)}</div>`;
    }

    async function validateRestore(id){
      const confirmValue=prompt('Type RESTORE to validate this backup for recovery.');
      if(confirmValue!=='RESTORE')return;
      const response=await fetch('/api/backups/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({backup_id:id,confirm:confirmValue})});
      const data=await response.json();
      showToast(data.message||'Restore validation complete.');
      loadRestoreHistory();
    }

    document.getElementById('backup-create-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const payload={
        label:document.getElementById('backup-label').value,
        include_recordings:document.getElementById('backup-recordings').checked,
        include_hls:document.getElementById('backup-hls').checked
      };
      const response=await fetch('/api/backups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Backup failed.');
      showToast(data.message);event.currentTarget.reset();loadBackups();
    });

    loadBackups();loadRestoreHistory();
    </script>"""
    return page_shell(
        "Backup and restore",
        "backup-restore",
        content,
        scripts,
    )



@app.get("/api/license")
def license_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    return {
        "license": license_snapshot(),
        "plans": LICENSE_PLAN_FEATURES,
        "history": list(reversed(load_license_history()))[:100],
    }


@app.put("/api/license")
def update_license(
    payload: LicenseUpdateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")

    plan = payload.plan.strip().lower()
    if plan not in LICENSE_PLAN_FEATURES:
        raise HTTPException(status_code=400, detail="Invalid license plan.")
    if payload.status not in {"trial", "active", "past_due", "suspended", "expired", "inactive"}:
        raise HTTPException(status_code=400, detail="Invalid license status.")

    previous = load_license_state()
    now = datetime.now().isoformat()
    state = {
        "plan": plan,
        "status": payload.status,
        "camera_limit": max(1, int(payload.camera_limit)),
        "trial_ends_at": payload.trial_ends_at.strip(),
        "subscription_ends_at": payload.subscription_ends_at.strip(),
        "features": sorted(
            set(
                item.strip()
                for item in (
                    payload.features
                    or LICENSE_PLAN_FEATURES[plan]
                )
                if item.strip()
            )
        ),
        "customer_reference": payload.customer_reference.strip(),
        "notes": payload.notes.strip(),
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "updated_by": user.get("id"),
        "updated_by_name": user.get("display_name") or user.get("email"),
    }
    save_license_state(state)
    record_license_history(
        user=user,
        action="license_updated",
        previous=previous,
        current=state,
        details=f"Plan {previous.get('plan')} → {plan}",
    )
    structured_log(
        "license.updated",
        plan=plan,
        status=state["status"],
        camera_limit=state["camera_limit"],
        user_id=user.get("id"),
    )
    return {
        "status": "complete",
        "license": license_snapshot(),
        "message": "License settings updated.",
    }


@app.post("/api/license/validate")
def validate_license(
    payload: LicenseValidationModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    snapshot = license_snapshot(payload.camera_count)
    structured_log(
        "license.validated",
        valid=snapshot.get("valid"),
        current_camera_count=snapshot.get("current_camera_count"),
        camera_limit=snapshot.get("camera_limit"),
        user_id=user.get("id"),
    )
    return {
        "status": "complete",
        "license": snapshot,
        "message": (
            "License is valid."
            if snapshot.get("valid")
            else "License requires attention."
        ),
    }


@app.get("/license-management", response_class=HTMLResponse)
def license_management_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "License management",
            "license-management",
            "manage_settings",
        )

    content = """<header class="topbar"><div><p class="eyebrow">Commercial foundation</p><h1>License management</h1></div></header>
    <section class="license-summary" id="license-summary"></section>
    <section class="license-layout">
      <form class="license-card license-form" id="license-form">
        <h2>Subscription settings</h2>
        <label>Plan<select id="license-plan"><option value="starter">Starter</option><option value="professional">Professional</option><option value="enterprise">Enterprise</option></select></label>
        <label>Status<select id="license-status"><option value="trial">Trial</option><option value="active">Active</option><option value="past_due">Past due</option><option value="suspended">Suspended</option><option value="expired">Expired</option><option value="inactive">Inactive</option></select></label>
        <label>Camera limit<input id="license-camera-limit" type="number" min="1"></label>
        <label>Trial ends<input id="license-trial-end" type="datetime-local"></label>
        <label>Subscription ends<input id="license-subscription-end" type="datetime-local"></label>
        <label>Customer reference<input id="license-customer-reference" placeholder="CRM or billing customer ID"></label>
        <label>Notes<textarea id="license-notes" placeholder="Internal licensing notes"></textarea></label>
        <button class="action-button" type="submit">Save license</button>
      </form>
      <div>
        <article class="license-card">
          <div class="panel-head"><div><h2>Licensed features</h2><div class="health-detail">Phase 23 records commercial entitlements. It does not disable existing features yet.</div></div></div>
          <div class="license-feature-grid" id="license-features"></div>
          <div class="case-actions"><button id="validate-license" type="button">Validate license</button></div>
          <div id="license-validation-result"></div>
        </article>
        <article class="license-card">
          <h2>License history</h2>
          <div class="license-history" id="license-history"><div class="empty">Loading history…</div></div>
        </article>
      </div>
    </section>"""

    scripts = """<script>
    const summary=document.getElementById('license-summary');
    const featuresHost=document.getElementById('license-features');
    const historyHost=document.getElementById('license-history');
    let licensePlans={};
    let currentLicense={};

    function escLicense(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    function toLocalInput(value){
      return value?String(value).slice(0,16):'';
    }

    function selectedFeatures(){
      return [...document.querySelectorAll('.license-feature input:checked')].map(input=>input.value);
    }

    function renderFeatures(plan,selected){
      const available=licensePlans[plan]||[];
      const all=[...new Set(Object.values(licensePlans).flat())].sort();
      featuresHost.innerHTML=all.map(feature=>`<label class="license-feature"><input type="checkbox" value="${escLicense(feature)}" ${selected.includes(feature)?'checked':''}> ${escLicense(feature.replaceAll('_',' '))}</label>`).join('');
    }

    function renderLicense(data){
      currentLicense=data.license||{};
      licensePlans=data.plans||{};
      const statusClass=currentLicense.valid?'valid':'invalid';
      summary.innerHTML=`
        <article class="stat"><span class="stat-label">Status</span><span class="stat-value"><span class="license-status ${statusClass}">${currentLicense.valid?'valid':'attention'}</span></span></article>
        <article class="stat"><span class="stat-label">Plan</span><span class="stat-value">${escLicense(currentLicense.plan)}</span></article>
        <article class="stat"><span class="stat-label">Cameras</span><span class="stat-value">${currentLicense.current_camera_count}/${currentLicense.camera_limit}</span></article>
        <article class="stat"><span class="stat-label">Days remaining</span><span class="stat-value">${currentLicense.days_remaining??'—'}</span></article>`;

      document.getElementById('license-plan').value=currentLicense.plan||'professional';
      document.getElementById('license-status').value=currentLicense.status||'active';
      document.getElementById('license-camera-limit').value=currentLicense.camera_limit||1;
      document.getElementById('license-trial-end').value=toLocalInput(currentLicense.trial_ends_at);
      document.getElementById('license-subscription-end').value=toLocalInput(currentLicense.subscription_ends_at);
      document.getElementById('license-customer-reference').value=currentLicense.customer_reference||'';
      document.getElementById('license-notes').value=currentLicense.notes||'';
      renderFeatures(currentLicense.plan,currentLicense.available_features||[]);

      historyHost.innerHTML=(data.history||[]).map(item=>`<div class="license-history-row"><strong>${escLicense(item.action.replaceAll('_',' '))}</strong><div class="health-detail">${escLicense((item.timestamp||'').replace('T',' ').slice(0,19))} · ${escLicense(item.user_name||'')}<br>${escLicense(item.details||'')}</div></div>`).join('')||'<div class="empty">No license history yet.</div>';
    }

    async function loadLicense(){
      const response=await fetch('/api/license');
      const data=await response.json();
      renderLicense(data);
    }

    document.getElementById('license-plan').addEventListener('change',event=>{
      renderFeatures(event.target.value,licensePlans[event.target.value]||[]);
    });

    document.getElementById('license-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const payload={
        plan:document.getElementById('license-plan').value,
        status:document.getElementById('license-status').value,
        camera_limit:Number(document.getElementById('license-camera-limit').value||1),
        trial_ends_at:document.getElementById('license-trial-end').value,
        subscription_ends_at:document.getElementById('license-subscription-end').value,
        customer_reference:document.getElementById('license-customer-reference').value,
        notes:document.getElementById('license-notes').value,
        features:selectedFeatures()
      };
      const response=await fetch('/api/license',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not save license.');
      showToast(data.message);loadLicense();
    });

    document.getElementById('validate-license').addEventListener('click',async()=>{
      const response=await fetch('/api/license/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({camera_count:null})});
      const data=await response.json();
      const value=data.license||{};
      document.getElementById('license-validation-result').innerHTML=`<div class="mock-banner">${escLicense(data.message)} Cameras: ${value.current_camera_count}/${value.camera_limit}. Status: ${escLicense(value.status)}.</div>`;
    });

    loadLicense();
    </script>"""
    return page_shell(
        "License management",
        "license-management",
        content,
        scripts,
    )



@app.get("/api/license/enforcement")
def license_enforcement_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    return {
        "enforcement": license_enforcement_snapshot(),
        "acknowledgements": list(reversed(load_license_acknowledgements()))[:100],
    }


@app.post("/api/license/feature-check")
def license_feature_check(
    payload: LicenseFeatureCheckModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    result = feature_entitlement(payload.feature)
    structured_log(
        "license.feature_checked",
        feature=result.get("feature"),
        entitled=result.get("entitled"),
        enforcement_mode=LICENSE_ENFORCEMENT_MODE,
        user_id=user.get("id"),
    )
    return {"status": "complete", "result": result}


@app.post("/api/license/acknowledge")
def acknowledge_license_warning(
    payload: LicenseAcknowledgementModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")

    warning_code = payload.warning_code.strip()
    if not warning_code:
        raise HTTPException(status_code=400, detail="Warning code is required.")

    acknowledgement = {
        "id": uuid.uuid4().hex,
        "warning_code": warning_code,
        "note": payload.note.strip(),
        "timestamp": datetime.now().isoformat(),
        "user_id": user.get("id"),
        "user_name": user.get("display_name") or user.get("email"),
    }
    acknowledgements = load_license_acknowledgements()
    acknowledgements.append(acknowledgement)
    save_license_acknowledgements(acknowledgements)
    record_license_history(
        user=user,
        action="warning_acknowledged",
        previous=load_license_state(),
        current=load_license_state(),
        details=f"Acknowledged warning: {warning_code}",
    )
    return {
        "status": "complete",
        "acknowledgement": acknowledgement,
        "message": "License warning acknowledged.",
    }


@app.get("/license-enforcement", response_class=HTMLResponse)
def license_enforcement_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "License enforcement",
            "license-enforcement",
            "manage_settings",
        )

    content = """<header class="topbar"><div><p class="eyebrow">Warning-only controls</p><h1>License enforcement</h1></div></header>
    <section class="license-enforcement-grid" id="license-enforcement-summary"></section>
    <section class="panel">
      <div class="panel-head"><div><h2>Current warnings</h2><div class="health-detail">Phase 24 never disables cameras or existing functionality. It records warnings and grace-period status only.</div></div></div>
      <div class="license-warning-list" id="license-enforcement-warnings"><div class="empty">Loading…</div></div>
    </section>
    <section class="panel">
      <div class="panel-head"><div><h2>Feature entitlements</h2><div class="health-detail">Unavailable plan features are reported but remain accessible in warning mode.</div></div></div>
      <div id="license-feature-entitlements"></div>
    </section>
    <section class="panel">
      <div class="panel-head"><div><h2>Acknowledgement history</h2></div></div>
      <div class="license-history" id="license-acknowledgements"><div class="empty">Loading…</div></div>
    </section>"""

    scripts = """<script>
    const summary=document.getElementById('license-enforcement-summary');
    const warningsHost=document.getElementById('license-enforcement-warnings');
    const featureHost=document.getElementById('license-feature-entitlements');
    const acknowledgementsHost=document.getElementById('license-acknowledgements');

    function escEnforcement(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    async function acknowledgeWarning(code){
      const note=prompt('Optional acknowledgement note')||'';
      const response=await fetch('/api/license/acknowledge',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({warning_code:code,note})
      });
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not acknowledge warning.');
      showToast(data.message);loadEnforcement();
    }

    async function loadEnforcement(){
      const response=await fetch('/api/license/enforcement');
      const data=await response.json();
      const value=data.enforcement||{};
      summary.innerHTML=`
        <article class="stat"><span class="stat-label">Mode</span><span class="stat-value">${escEnforcement(value.enforcement_mode)}</span></article>
        <article class="stat"><span class="stat-label">Operational</span><span class="stat-value">${value.operational?'Yes':'No'}</span></article>
        <article class="stat"><span class="stat-label">Cameras</span><span class="stat-value">${value.current_camera_count}/${value.camera_limit}</span></article>
        <article class="stat"><span class="stat-label">Grace days</span><span class="stat-value">${value.in_grace_period?Math.max(0,value.grace_days-value.days_past_due):'—'}</span></article>`;

      warningsHost.innerHTML=(value.warnings||[]).map(item=>`
        <div class="license-warning-row"><strong>${escEnforcement(item.code.replaceAll('_',' '))}</strong>
        <div class="health-detail">${escEnforcement(item.message)}</div>
        <div class="case-actions"><button onclick="acknowledgeWarning('${escEnforcement(item.code)}')">Acknowledge</button></div></div>`).join('')||'<div class="release-row"><strong>No active license warnings.</strong></div>';

      const available=new Set(value.available_features||[]);
      const all=[...new Set([...(value.available_features||[]),...(value.missing_features||[])])].sort();
      featureHost.innerHTML=`<table class="license-feature-table"><thead><tr><th>Feature</th><th>Entitlement</th><th>Enforcement</th></tr></thead><tbody>${all.map(feature=>`<tr><td>${escEnforcement(feature.replaceAll('_',' '))}</td><td>${available.has(feature)?'Licensed':'Not included'}</td><td>Warning only</td></tr>`).join('')}</tbody></table>`;

      acknowledgementsHost.innerHTML=(data.acknowledgements||[]).map(item=>`
        <div class="license-history-row"><strong>${escEnforcement(item.warning_code.replaceAll('_',' '))}</strong>
        <div class="health-detail">${escEnforcement((item.timestamp||'').replace('T',' ').slice(0,19))} · ${escEnforcement(item.user_name||'')}<br>${escEnforcement(item.note||'')}</div></div>`).join('')||'<div class="empty">No warnings acknowledged yet.</div>';
    }

    loadEnforcement();
    </script>"""
    return page_shell(
        "License enforcement",
        "license-enforcement",
        content,
        scripts,
    )



@app.get("/api/billing")
def billing_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    accounts = list(load_billing_accounts().values())
    invoices = list(load_billing_invoices().values())
    accounts.sort(key=lambda item: item.get("updated_at", item.get("created_at", "")), reverse=True)
    invoices.sort(key=lambda item: item.get("updated_at", item.get("created_at", "")), reverse=True)
    return {
        "summary": billing_summary(),
        "accounts": accounts,
        "invoices": invoices,
        "events": list(reversed(load_billing_events()))[:200],
    }


@app.put("/api/billing/accounts/{account_id}")
def update_billing_account(
    account_id: str,
    payload: BillingAccountUpdateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")

    if payload.payment_status not in {"current", "past_due", "suspended", "cancelled"}:
        raise HTTPException(status_code=400, detail="Invalid payment status.")
    if payload.billing_cycle not in {"monthly", "annual", "manual"}:
        raise HTTPException(status_code=400, detail="Invalid billing cycle.")

    accounts = load_billing_accounts()
    previous = accounts.get(account_id, {})
    now = datetime.now().isoformat()
    account = {
        "id": account_id,
        "customer_name": payload.customer_name.strip(),
        "billing_email": payload.billing_email.strip(),
        "company_name": payload.company_name.strip(),
        "external_customer_id": payload.external_customer_id.strip(),
        "payment_status": payload.payment_status,
        "billing_cycle": payload.billing_cycle,
        "next_billing_date": payload.next_billing_date.strip(),
        "notes": payload.notes.strip(),
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "updated_by": user.get("id"),
    }
    if not account["customer_name"] or not account["billing_email"]:
        raise HTTPException(status_code=400, detail="Customer name and billing email are required.")

    accounts[account_id] = account
    save_billing_accounts(accounts)
    append_billing_event(
        action="billing_account_updated",
        user=user,
        account_id=account_id,
        details=f"Payment status: {account['payment_status']}",
    )
    return {"status": "complete", "account": account, "message": "Billing account saved."}


@app.post("/api/billing/invoices")
def create_billing_invoice(
    payload: BillingInvoiceCreateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    if payload.status not in {"draft", "open", "paid", "past_due", "void"}:
        raise HTTPException(status_code=400, detail="Invalid invoice status.")
    if payload.amount_cents < 0:
        raise HTTPException(status_code=400, detail="Invoice amount cannot be negative.")

    if payload.account_id not in load_billing_accounts():
        raise HTTPException(status_code=404, detail="Billing account not found.")

    invoice_id = uuid.uuid4().hex
    now = datetime.now().isoformat()
    invoice = {
        "id": invoice_id,
        "account_id": payload.account_id,
        "description": payload.description.strip(),
        "amount_cents": int(payload.amount_cents),
        "currency": BILLING_CURRENCY,
        "due_date": payload.due_date.strip(),
        "status": payload.status,
        "external_invoice_id": "",
        "paid_at": now if payload.status == "paid" else "",
        "notes": "",
        "created_at": now,
        "updated_at": now,
        "created_by": user.get("id"),
    }
    invoices = load_billing_invoices()
    invoices[invoice_id] = invoice
    save_billing_invoices(invoices)
    append_billing_event(
        action="invoice_created",
        user=user,
        account_id=payload.account_id,
        invoice_id=invoice_id,
        details=f"{invoice['amount_cents']} {invoice['currency']} cents",
    )
    return {"status": "complete", "invoice": invoice, "message": "Invoice created."}


@app.put("/api/billing/invoices/{invoice_id}")
def update_billing_invoice(
    invoice_id: str,
    payload: BillingInvoiceUpdateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    if payload.status not in {"draft", "open", "paid", "past_due", "void"}:
        raise HTTPException(status_code=400, detail="Invalid invoice status.")

    invoices = load_billing_invoices()
    invoice = invoices.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found.")

    invoice["status"] = payload.status
    invoice["paid_at"] = payload.paid_at.strip() or (
        datetime.now().isoformat() if payload.status == "paid" else invoice.get("paid_at", "")
    )
    invoice["external_invoice_id"] = payload.external_invoice_id.strip()
    invoice["notes"] = payload.notes.strip()
    invoice["updated_at"] = datetime.now().isoformat()
    invoice["updated_by"] = user.get("id")
    invoices[invoice_id] = invoice
    save_billing_invoices(invoices)
    append_billing_event(
        action="invoice_updated",
        user=user,
        account_id=invoice.get("account_id", ""),
        invoice_id=invoice_id,
        details=f"Status: {payload.status}",
    )
    return {"status": "complete", "invoice": invoice, "message": "Invoice updated."}


@app.get("/billing-operations", response_class=HTMLResponse)
def billing_operations_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Billing operations",
            "billing-operations",
            "manage_settings",
        )

    content = """<header class="topbar"><div><p class="eyebrow">Subscription operations</p><h1>Billing operations</h1></div></header>
    <section class="billing-summary" id="billing-summary"></section>
    <section class="billing-layout">
      <div>
        <form class="billing-card billing-form" id="billing-account-form">
          <h2>Billing account</h2>
          <label>Account ID<input id="billing-account-id" value="primary" required></label>
          <label>Customer name<input id="billing-customer-name" required></label>
          <label>Billing email<input id="billing-email" type="email" required></label>
          <label>Company<input id="billing-company"></label>
          <label>External customer ID<input id="billing-external-customer"></label>
          <label>Payment status<select id="billing-payment-status"><option value="current">Current</option><option value="past_due">Past due</option><option value="suspended">Suspended</option><option value="cancelled">Cancelled</option></select></label>
          <label>Billing cycle<select id="billing-cycle"><option value="monthly">Monthly</option><option value="annual">Annual</option><option value="manual">Manual</option></select></label>
          <label>Next billing date<input id="billing-next-date" type="date"></label>
          <label>Notes<textarea id="billing-notes"></textarea></label>
          <button class="action-button" type="submit">Save billing account</button>
        </form>
        <form class="billing-card billing-form" id="invoice-form">
          <h2>Create invoice</h2>
          <label>Account<select id="invoice-account" required></select></label>
          <label>Description<input id="invoice-description" required></label>
          <label>Amount<input id="invoice-amount" type="number" min="0" step="0.01" required></label>
          <label>Due date<input id="invoice-due-date" type="date"></label>
          <label>Status<select id="invoice-status"><option value="draft">Draft</option><option value="open">Open</option><option value="paid">Paid</option><option value="past_due">Past due</option><option value="void">Void</option></select></label>
          <button class="action-button" type="submit">Create invoice</button>
        </form>
      </div>
      <div>
        <div class="billing-grid" id="billing-accounts"><div class="empty">Loading accounts…</div></div>
        <section class="billing-card">
          <h2>Invoices</h2>
          <div id="billing-invoices"><div class="empty">Loading invoices…</div></div>
        </section>
        <section class="billing-card">
          <h2>Billing activity</h2>
          <div id="billing-events"><div class="empty">Loading activity…</div></div>
        </section>
      </div>
    </section>"""

    scripts = """<script>
    const summaryHost=document.getElementById('billing-summary');
    const accountsHost=document.getElementById('billing-accounts');
    const invoicesHost=document.getElementById('billing-invoices');
    const eventsHost=document.getElementById('billing-events');
    const accountSelect=document.getElementById('invoice-account');

    function escBilling(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    function money(cents,currency){
      return new Intl.NumberFormat(undefined,{style:'currency',currency:currency||'USD'}).format((Number(cents)||0)/100);
    }

    async function loadBilling(){
      const response=await fetch('/api/billing');
      const data=await response.json();
      const value=data.summary||{};
      summaryHost.innerHTML=`
        <article class="stat"><span class="stat-label">Provider</span><span class="stat-value">${escBilling(value.provider)}</span></article>
        <article class="stat"><span class="stat-label">Accounts</span><span class="stat-value">${value.account_count||0}</span></article>
        <article class="stat"><span class="stat-label">Open invoices</span><span class="stat-value">${value.open_invoice_count||0}</span></article>
        <article class="stat"><span class="stat-label">Outstanding</span><span class="stat-value">${money(value.outstanding_cents,value.currency)}</span></article>`;

      const accounts=data.accounts||[];
      accountSelect.innerHTML=accounts.map(item=>`<option value="${escBilling(item.id)}">${escBilling(item.customer_name)} (${escBilling(item.id)})</option>`).join('');
      accountsHost.innerHTML=accounts.map(item=>`
        <article class="billing-card">
          <div class="billing-head"><div><h3>${escBilling(item.customer_name)}</h3><div class="billing-meta">${escBilling(item.billing_email)}<br>${escBilling(item.company_name||'')} · ${escBilling(item.billing_cycle)}<br>Next billing: ${escBilling(item.next_billing_date||'—')}</div></div><span class="billing-badge">${escBilling(item.payment_status)}</span></div>
        </article>`).join('')||'<div class="empty">No billing accounts.</div>';

      invoicesHost.innerHTML=`<table class="billing-table"><thead><tr><th>Description</th><th>Amount</th><th>Status</th><th>Due</th><th>Action</th></tr></thead><tbody>${(data.invoices||[]).map(item=>`<tr><td>${escBilling(item.description)}</td><td>${money(item.amount_cents,item.currency)}</td><td>${escBilling(item.status)}</td><td>${escBilling(item.due_date||'—')}</td><td><button onclick="markInvoicePaid('${item.id}')">Mark paid</button></td></tr>`).join('')||'<tr><td colspan="5">No invoices.</td></tr>'}</tbody></table>`;

      eventsHost.innerHTML=(data.events||[]).map(item=>`<div class="license-history-row"><strong>${escBilling(item.action.replaceAll('_',' '))}</strong><div class="billing-meta">${escBilling((item.timestamp||'').replace('T',' ').slice(0,19))} · ${escBilling(item.user_name||'')}<br>${escBilling(item.details||'')}</div></div>`).join('')||'<div class="empty">No billing events.</div>';
    }

    async function markInvoicePaid(id){
      const response=await fetch(`/api/billing/invoices/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'paid',paid_at:new Date().toISOString(),external_invoice_id:'',notes:''})});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not update invoice.');
      showToast(data.message);loadBilling();
    }

    document.getElementById('billing-account-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const accountId=document.getElementById('billing-account-id').value.trim();
      const payload={
        customer_name:document.getElementById('billing-customer-name').value,
        billing_email:document.getElementById('billing-email').value,
        company_name:document.getElementById('billing-company').value,
        external_customer_id:document.getElementById('billing-external-customer').value,
        payment_status:document.getElementById('billing-payment-status').value,
        billing_cycle:document.getElementById('billing-cycle').value,
        next_billing_date:document.getElementById('billing-next-date').value,
        notes:document.getElementById('billing-notes').value
      };
      const response=await fetch(`/api/billing/accounts/${encodeURIComponent(accountId)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not save billing account.');
      showToast(data.message);loadBilling();
    });

    document.getElementById('invoice-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const payload={
        account_id:document.getElementById('invoice-account').value,
        description:document.getElementById('invoice-description').value,
        amount_cents:Math.round(Number(document.getElementById('invoice-amount').value||0)*100),
        due_date:document.getElementById('invoice-due-date').value,
        status:document.getElementById('invoice-status').value
      };
      const response=await fetch('/api/billing/invoices',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not create invoice.');
      showToast(data.message);event.currentTarget.reset();loadBilling();
    });

    loadBilling();
    </script>"""
    return page_shell(
        "Billing operations",
        "billing-operations",
        content,
        scripts,
    )



@app.get("/api/subscription-portal")
def subscription_portal_api(request: Request) -> dict:
    user = current_user(request)
    account = billing_account_for_user(user)
    license_data = license_enforcement_snapshot()
    invoices = invoices_for_billing_account(str(account.get("id") or ""))
    requests = [
        item
        for item in load_subscription_requests().values()
        if subscription_request_owner_matches(item, user)
    ]
    tickets = [
        item
        for item in load_billing_support_tickets().values()
        if billing_ticket_owner_matches(item, user)
    ]
    requests.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    tickets.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return {
        "account": account,
        "license": license_data,
        "invoices": invoices,
        "requests": requests,
        "tickets": tickets,
        "usage": customer_cloud_usage_snapshot(user),
    }


@app.post("/api/subscription-requests")
def create_subscription_request(
    payload: SubscriptionRequestCreateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if payload.request_type not in {
        "plan_upgrade",
        "camera_increase",
        "enterprise_license",
        "cloud_recording",
    }:
        raise HTTPException(status_code=400, detail="Invalid subscription request type.")
    if payload.requested_plan and payload.requested_plan not in LICENSE_PLAN_FEATURES:
        raise HTTPException(status_code=400, detail="Invalid requested plan.")
    if payload.requested_camera_limit is not None and payload.requested_camera_limit < 1:
        raise HTTPException(status_code=400, detail="Camera limit must be positive.")

    request_id = uuid.uuid4().hex
    now = datetime.now().isoformat()
    record = {
        "id": request_id,
        "user_id": user.get("id"),
        "user_name": user.get("display_name") or user.get("email"),
        "user_email": user.get("email"),
        "request_type": payload.request_type,
        "requested_plan": payload.requested_plan.strip().lower(),
        "requested_camera_limit": payload.requested_camera_limit,
        "reason": payload.reason.strip(),
        "status": "pending",
        "admin_note": "",
        "created_at": now,
        "updated_at": now,
    }
    records = load_subscription_requests()
    records[request_id] = record
    save_subscription_requests(records)
    structured_log(
        "subscription.request_created",
        request_id=request_id,
        request_type=payload.request_type,
        user_id=user.get("id"),
    )
    return {
        "status": "complete",
        "request": record,
        "message": "Subscription request submitted.",
    }


@app.put("/api/subscription-requests/{request_id}")
def update_subscription_request(
    request_id: str,
    payload: SubscriptionRequestUpdateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    if payload.status not in {"pending", "approved", "declined", "completed"}:
        raise HTTPException(status_code=400, detail="Invalid request status.")

    records = load_subscription_requests()
    record = records.get(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="Subscription request not found.")
    record["status"] = payload.status
    record["admin_note"] = payload.admin_note.strip()
    record["updated_at"] = datetime.now().isoformat()
    record["updated_by"] = user.get("id")
    records[request_id] = record
    save_subscription_requests(records)
    return {
        "status": "complete",
        "request": record,
        "message": "Subscription request updated.",
    }


@app.post("/api/billing-support-tickets")
def create_billing_support_ticket(
    payload: BillingSupportTicketCreateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not payload.subject.strip() or not payload.message.strip():
        raise HTTPException(status_code=400, detail="Subject and message are required.")
    if payload.priority not in {"low", "normal", "high"}:
        raise HTTPException(status_code=400, detail="Invalid ticket priority.")

    ticket_id = uuid.uuid4().hex
    now = datetime.now().isoformat()
    ticket = {
        "id": ticket_id,
        "user_id": user.get("id"),
        "user_name": user.get("display_name") or user.get("email"),
        "user_email": user.get("email"),
        "subject": payload.subject.strip(),
        "message": payload.message.strip(),
        "priority": payload.priority,
        "status": "open",
        "admin_note": "",
        "created_at": now,
        "updated_at": now,
    }
    tickets = load_billing_support_tickets()
    tickets[ticket_id] = ticket
    save_billing_support_tickets(tickets)
    return {
        "status": "complete",
        "ticket": ticket,
        "message": "Billing support ticket opened.",
    }


@app.put("/api/billing-support-tickets/{ticket_id}")
def update_billing_support_ticket(
    ticket_id: str,
    payload: BillingSupportTicketUpdateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    if payload.status not in {"open", "in_progress", "resolved", "closed"}:
        raise HTTPException(status_code=400, detail="Invalid ticket status.")

    tickets = load_billing_support_tickets()
    ticket = tickets.get(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Billing support ticket not found.")
    ticket["status"] = payload.status
    ticket["admin_note"] = payload.admin_note.strip()
    ticket["updated_at"] = datetime.now().isoformat()
    ticket["updated_by"] = user.get("id")
    tickets[ticket_id] = ticket
    save_billing_support_tickets(tickets)
    return {
        "status": "complete",
        "ticket": ticket,
        "message": "Billing support ticket updated.",
    }


@app.get("/api/customer-invoices/{invoice_id}/download")
def download_customer_invoice(invoice_id: str, request: Request) -> Response:
    user = current_user(request)
    invoice = load_billing_invoices().get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    account = billing_account_for_user(user)
    if (
        invoice.get("account_id") != account.get("id")
        and not has_permission(user, "manage_settings")
    ):
        raise HTTPException(status_code=403, detail="You cannot access this invoice.")

    body = (
        f"AnyAiCam VMS Invoice\\n"
        f"Invoice ID: {invoice.get('id')}\\n"
        f"Account ID: {invoice.get('account_id')}\\n"
        f"Description: {invoice.get('description')}\\n"
        f"Amount: {invoice.get('amount_cents')} {invoice.get('currency')} cents\\n"
        f"Status: {invoice.get('status')}\\n"
        f"Due date: {invoice.get('due_date')}\\n"
        f"Paid at: {invoice.get('paid_at')}\\n"
    )
    return Response(
        body,
        media_type="text/plain",
        headers={
            "Content-Disposition": (
                f'attachment; filename="invoice_{invoice_id[:8]}.txt"'
            )
        },
    )


@app.get("/subscription-portal", response_class=HTMLResponse)
def subscription_portal_page(request: Request) -> str:
    user = current_user(request)
    content = """<header class="topbar"><div><p class="eyebrow">Customer self-service</p><h1>Subscription portal</h1></div></header>
    <section class="subscription-summary" id="subscription-summary"></section>
    <section class="subscription-layout">
      <div class="subscription-stack">
        <article class="subscription-card">
          <div class="panel-head"><div><h2>Subscription and license</h2><div class="subscription-meta">View plan, license status, cameras, renewal, and billing condition.</div></div></div>
          <div id="subscription-license"></div>
        </article>
        <article class="subscription-card">
          <h2>Invoices and payment history</h2>
          <div id="subscription-invoices"><div class="empty">Loading invoices…</div></div>
        </article>
        <article class="subscription-card">
          <h2>Cloud and edge usage</h2>
          <div class="usage-grid" id="subscription-usage"></div>
        </article>
        <article class="subscription-card">
          <h2>My requests</h2>
          <div id="subscription-request-history"><div class="empty">Loading requests…</div></div>
        </article>
        <article class="subscription-card">
          <h2>Billing support</h2>
          <div id="billing-ticket-history"><div class="empty">Loading tickets…</div></div>
        </article>
      </div>
      <div class="subscription-stack">
        <article class="subscription-card">
          <h2>Manage payment</h2>
          <div class="subscription-meta">Start a Stripe subscription or manage an existing Stripe billing account.</div>
          <div class="subscription-actions"><button id="customer-start-checkout" type="button">Subscribe or upgrade</button><button id="customer-open-payment-portal" type="button">Manage payment method</button></div>
        </article>
        <form class="subscription-card subscription-form" id="subscription-request-form">
          <h2>Request a change</h2>
          <label>Request type<select id="subscription-request-type"><option value="plan_upgrade">Plan upgrade</option><option value="camera_increase">Additional cameras</option><option value="enterprise_license">Enterprise licensing</option><option value="cloud_recording">Cloud recording</option></select></label>
          <label>Requested plan<select id="subscription-request-plan"><option value="">No plan selected</option><option value="starter">Starter</option><option value="professional">Professional</option><option value="enterprise">Enterprise</option></select></label>
          <label>Requested camera limit<input id="subscription-request-cameras" type="number" min="1"></label>
          <label>Reason<textarea id="subscription-request-reason" placeholder="Tell us what you need"></textarea></label>
          <button class="action-button" type="submit">Submit request</button>
        </form>
        <form class="subscription-card subscription-form" id="billing-ticket-form">
          <h2>Open billing ticket</h2>
          <label>Subject<input id="billing-ticket-subject" required></label>
          <label>Priority<select id="billing-ticket-priority"><option value="low">Low</option><option value="normal" selected>Normal</option><option value="high">High</option></select></label>
          <label>Message<textarea id="billing-ticket-message" required></textarea></label>
          <button class="action-button" type="submit">Open ticket</button>
        </form>
      </div>
    </section>"""

    scripts = """<script>
    const summaryHost=document.getElementById('subscription-summary');
    const licenseHost=document.getElementById('subscription-license');
    const invoicesHost=document.getElementById('subscription-invoices');
    const usageHost=document.getElementById('subscription-usage');
    const requestsHost=document.getElementById('subscription-request-history');
    const ticketsHost=document.getElementById('billing-ticket-history');

    function escSubscription(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    function money(cents,currency){
      return new Intl.NumberFormat(undefined,{style:'currency',currency:currency||'USD'}).format((Number(cents)||0)/100);
    }

    function bytes(value){
      const number=Number(value)||0;
      if(number<1024)return number+' B';
      if(number<1024**2)return (number/1024).toFixed(1)+' KB';
      if(number<1024**3)return (number/1024**2).toFixed(1)+' MB';
      return (number/1024**3).toFixed(2)+' GB';
    }

    async function loadSubscriptionPortal(){
      const response=await fetch('/api/subscription-portal');
      const data=await response.json();
      const account=data.account||{},license=data.license||{},usage=data.usage||{};
      summaryHost.innerHTML=`
        <article class="stat"><span class="stat-label">Plan</span><span class="stat-value">${escSubscription(license.plan||'—')}</span></article>
        <article class="stat"><span class="stat-label">License</span><span class="stat-value">${escSubscription(license.status||'—')}</span></article>
        <article class="stat"><span class="stat-label">Cameras</span><span class="stat-value">${license.current_camera_count||0}/${license.camera_limit||0}</span></article>
        <article class="stat"><span class="stat-label">Billing</span><span class="stat-value">${escSubscription(account.payment_status||'not configured')}</span></article>`;

      licenseHost.innerHTML=`<div class="usage-grid">
        <div class="usage-item"><strong>Customer</strong><div class="subscription-meta">${escSubscription(account.customer_name||'')}</div></div>
        <div class="usage-item"><strong>Company</strong><div class="subscription-meta">${escSubscription(account.company_name||'—')}</div></div>
        <div class="usage-item"><strong>Billing cycle</strong><div class="subscription-meta">${escSubscription(account.billing_cycle||'manual')}</div></div>
        <div class="usage-item"><strong>Next billing date</strong><div class="subscription-meta">${escSubscription(account.next_billing_date||'—')}</div></div>
        <div class="usage-item"><strong>License operation</strong><div class="subscription-meta">${license.operational?'Operational':'Attention required'}</div></div>
        <div class="usage-item"><strong>Grace period</strong><div class="subscription-meta">${license.in_grace_period?'Active':'Not active'}</div></div>
      </div>`;

      invoicesHost.innerHTML=`<table class="subscription-table"><thead><tr><th>Description</th><th>Amount</th><th>Status</th><th>Due</th><th></th></tr></thead><tbody>${(data.invoices||[]).map(invoice=>`<tr><td>${escSubscription(invoice.description)}</td><td>${money(invoice.amount_cents,invoice.currency)}</td><td>${escSubscription(invoice.status)}</td><td>${escSubscription(invoice.due_date||'—')}</td><td><a href="/api/customer-invoices/${invoice.id}/download">Download</a></td></tr>`).join('')||'<tr><td colspan="5">No invoices available.</td></tr>'}</tbody></table>`;

      usageHost.innerHTML=`
        <div class="usage-item"><strong>Local recordings</strong><div class="subscription-meta">${usage.recording_file_count||0} files · ${bytes(usage.local_storage_bytes)}</div></div>
        <div class="usage-item"><strong>S3 cloud storage</strong><div class="subscription-meta">${usage.s3_configured?'Configured':'Not configured'}</div></div>
        <div class="usage-item"><strong>Cloud playback</strong><div class="subscription-meta">${usage.cloudfront_configured?'Configured':'Placeholder'}</div></div>
        <div class="usage-item"><strong>Edge appliance</strong><div class="subscription-meta">${escSubscription(usage.edge_status)} · ${escSubscription(usage.edge_runtime_role)}</div></div>
        <div class="usage-item"><strong>Connected cameras</strong><div class="subscription-meta">${usage.configured_cameras||0}</div></div>
        <div class="usage-item"><strong>AI processing</strong><div class="subscription-meta">${escSubscription(usage.ai_processing_status)}</div></div>
        <div class="usage-item"><strong>Mobile devices</strong><div class="subscription-meta">${usage.registered_mobile_devices||0}</div></div>
        <div class="usage-item"><strong>Cloud recording</strong><div class="subscription-meta">${escSubscription(usage.cloud_upload_status)}</div></div>`;

      requestsHost.innerHTML=(data.requests||[]).map(item=>`<div class="license-history-row"><strong>${escSubscription(item.request_type.replaceAll('_',' '))}</strong><span class="subscription-status">${escSubscription(item.status)}</span><div class="subscription-meta">${escSubscription((item.created_at||'').replace('T',' ').slice(0,19))}<br>${escSubscription(item.reason||'')}${item.admin_note?'<br>Admin: '+escSubscription(item.admin_note):''}</div></div>`).join('')||'<div class="empty">No subscription requests.</div>';
      ticketsHost.innerHTML=(data.tickets||[]).map(item=>`<div class="license-history-row"><strong>${escSubscription(item.subject)}</strong><span class="subscription-status">${escSubscription(item.status)}</span><div class="subscription-meta">${escSubscription(item.priority)} · ${escSubscription((item.created_at||'').replace('T',' ').slice(0,19))}<br>${escSubscription(item.message)}${item.admin_note?'<br>Support: '+escSubscription(item.admin_note):''}</div></div>`).join('')||'<div class="empty">No billing support tickets.</div>';
    }

    document.getElementById('customer-start-checkout').addEventListener('click',async()=>{
      const plan=prompt('Choose plan: starter, professional, or enterprise','professional');
      if(!plan)return;
      const response=await fetch('/api/payments/checkout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan:plan.toLowerCase(),quantity:1})});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not start checkout.');
      window.location.href=data.checkout_url;
    });

    document.getElementById('customer-open-payment-portal').addEventListener('click',async()=>{
      const response=await fetch('/api/payments/customer-portal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({return_path:'/subscription-portal'})});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not open payment portal.');
      window.location.href=data.portal_url;
    });

    document.getElementById('subscription-request-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const cameraValue=document.getElementById('subscription-request-cameras').value;
      const response=await fetch('/api/subscription-requests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        request_type:document.getElementById('subscription-request-type').value,
        requested_plan:document.getElementById('subscription-request-plan').value,
        requested_camera_limit:cameraValue?Number(cameraValue):null,
        reason:document.getElementById('subscription-request-reason').value
      })});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not submit request.');
      showToast(data.message);event.currentTarget.reset();loadSubscriptionPortal();
    });

    document.getElementById('billing-ticket-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const response=await fetch('/api/billing-support-tickets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        subject:document.getElementById('billing-ticket-subject').value,
        priority:document.getElementById('billing-ticket-priority').value,
        message:document.getElementById('billing-ticket-message').value
      })});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not open ticket.');
      showToast(data.message);event.currentTarget.reset();loadSubscriptionPortal();
    });

    loadSubscriptionPortal();
    </script>"""
    return page_shell(
        "Subscription portal",
        "subscription-portal",
        content,
        scripts,
    )



@app.get("/admin-portal", response_class=HTMLResponse)
def administrator_portal_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Administrator portal",
            "admin-portal",
            "manage_settings",
        )

    accounts = list(load_billing_accounts().values())
    invoices = list(load_billing_invoices().values())
    requests = list(load_subscription_requests().values())
    tickets = list(load_billing_support_tickets().values())
    onboarding_profiles = list(load_onboarding_profiles().values())
    onboarding_activity = load_onboarding_activity()
    payment_sessions = list(load_payment_sessions().values())
    audit_entries = load_audit_entries(12)
    license_state = load_license_state()
    cloud_index = list(load_cloud_recording_index().values())

    active_accounts = sum(
        str(item.get("payment_status", "")).lower() in {"current", "active", "trial"}
        for item in accounts
    )
    payment_attention = sum(
        str(item.get("payment_status", "")).lower()
        in {"past_due", "failed", "declined", "suspended"}
        for item in accounts
    )
    open_requests = sum(
        str(item.get("status", "")).lower()
        in {"pending", "open", "in_progress"}
        for item in requests
    )
    open_tickets = sum(
        str(item.get("status", "")).lower()
        not in {"resolved", "closed", "complete", "completed"}
        for item in tickets
    )
    onboarding_in_progress = sum(
        str(item.get("status", "")).lower()
        not in {"complete", "completed", "approved", "active"}
        for item in onboarding_profiles
    )
    completed_payments = sum(
        str(item.get("payment_status", "")).lower() in {"paid", "complete", "completed"}
        for item in payment_sessions
    )

    def badge(value: object) -> str:
        status = str(value or "unknown").strip().lower()
        return (
            f'<span class="admin-command-badge {escape(status, quote=True)}">'
            f'{escape(status.replace("_", " "))}</span>'
        )

    account_rows = []
    for account in sorted(
        accounts,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )[:10]:
        account_rows.append(
            '<div class="admin-command-row">'
            f'<div><strong>{escape(str(account.get("company_name") or account.get("customer_name") or "Customer"))}</strong>'
            f'<div class="admin-command-meta">{escape(str(account.get("billing_email") or "No billing email"))}'
            f'<br>Customer ID: {escape(str(account.get("external_customer_id") or account.get("id") or "—"))}</div></div>'
            f'<div>{badge(account.get("payment_status"))}</div>'
            '<div class="admin-command-actions"><a href="/billing-operations">Open billing</a></div>'
            '</div>'
        )

    request_rows = []
    for item in sorted(
        requests,
        key=lambda entry: str(entry.get("created_at") or ""),
        reverse=True,
    )[:8]:
        request_rows.append(
            '<div class="admin-command-row">'
            f'<div><strong>{escape(str(item.get("user_name") or item.get("user_email") or "Customer"))}</strong>'
            f'<div class="admin-command-meta">{escape(str(item.get("request_type") or "Request").replace("_", " "))}'
            f' · {escape(str(item.get("requested_plan") or "No plan"))}'
            f'<br>{escape(str(item.get("reason") or ""))}</div></div>'
            f'<div>{badge(item.get("status"))}</div>'
            '<div class="admin-command-actions"><a href="/subscription-admin">Review</a></div>'
            '</div>'
        )

    onboarding_rows = []
    for profile in sorted(
        onboarding_profiles,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )[:8]:
        progress = onboarding_progress(profile)
        onboarding_rows.append(
            '<div class="admin-command-row">'
            f'<div><strong>{escape(str(profile.get("organization_name") or profile.get("contact_name") or profile.get("user_email") or "Onboarding customer"))}</strong>'
            f'<div class="admin-command-meta">{progress["percent"]}% complete'
            f' · {escape(str(profile.get("deployment_type") or "edge"))}'
            f' · {escape(str(profile.get("expected_camera_count") or 0))} camera(s)</div></div>'
            f'<div>{badge(profile.get("status"))}</div>'
            '<div class="admin-command-actions"><a href="/onboarding-admin">Open</a></div>'
            '</div>'
        )

    audit_rows = []
    for item in audit_entries[:8]:
        audit_rows.append(
            '<div class="admin-command-row">'
            f'<div><strong>{escape(str(item.get("action") or "Activity"))}</strong>'
            f'<div class="admin-command-meta">{escape(str(item.get("user_name") or item.get("user_id") or "System"))}'
            f' · {escape(str(item.get("resource") or ""))}'
            f'<br>{escape(str(item.get("timestamp") or ""))}</div></div>'
            f'<div>{badge(item.get("outcome"))}</div>'
            '<div class="admin-command-actions"><a href="/audit-logs">Audit log</a></div>'
            '</div>'
        )

    content = f"""<header class="topbar"><div><p class="eyebrow">Business operations</p><h1>Administrator portal</h1></div><span class="pill">No customer video access</span></header>
    <div class="admin-privacy-note"><strong>Privacy boundary:</strong> this portal manages customer accounts, billing, licenses, onboarding, appliances and support records. It does not expose customer camera video. Any future support-video access must be explicit, time-limited and audited.</div>
    <section class="admin-command-summary" style="margin-top:16px">
      <article class="admin-command-stat"><span>Customer accounts</span><strong>{len(accounts)}</strong></article>
      <article class="admin-command-stat"><span>Active/current</span><strong>{active_accounts}</strong></article>
      <article class="admin-command-stat"><span>Payment attention</span><strong>{payment_attention}</strong></article>
      <article class="admin-command-stat"><span>Open requests</span><strong>{open_requests}</strong></article>
      <article class="admin-command-stat"><span>Open support</span><strong>{open_tickets}</strong></article>
      <article class="admin-command-stat"><span>Onboarding</span><strong>{onboarding_in_progress}</strong></article>
    </section>
    <section class="admin-command-grid">
      <div class="admin-command-stack">
        <article class="admin-command-card">
          <div class="admin-command-head"><div><h2>Customer and billing overview</h2><div class="admin-command-meta">Account information only—no cameras.</div></div><div class="admin-command-actions"><a href="/billing-operations">Billing operations</a><a href="/license-management">Licensing</a></div></div>
          <div class="admin-command-list">{"".join(account_rows) or '<div class="empty">No billing accounts found.</div>'}</div>
        </article>
        <article class="admin-command-card">
          <div class="admin-command-head"><h2>Subscription and expansion requests</h2><div class="admin-command-actions"><a href="/subscription-admin">Manage requests</a></div></div>
          <div class="admin-command-list">{"".join(request_rows) or '<div class="empty">No subscription requests.</div>'}</div>
        </article>
        <article class="admin-command-card">
          <div class="admin-command-head"><h2>Customer onboarding</h2><div class="admin-command-actions"><a href="/onboarding-admin">Onboarding administration</a></div></div>
          <div class="admin-command-list">{"".join(onboarding_rows) or '<div class="empty">No onboarding profiles.</div>'}</div>
        </article>
      </div>
      <aside class="admin-command-stack">
        <article class="admin-command-card"><h2>Operations snapshot</h2>
          <div class="settings-list">
            <div class="setting-link"><div><strong>License plan</strong><div class="health-detail">{escape(str(license_state.get("plan") or "—"))} · {escape(str(license_state.get("status") or "—"))}</div></div><a href="/license-management">Open</a></div>
            <div class="setting-link"><div><strong>Invoices</strong><div class="health-detail">{len(invoices)} invoice record(s)</div></div><a href="/billing-operations">Open</a></div>
            <div class="setting-link"><div><strong>Stripe checkout confirmations</strong><div class="health-detail">{completed_payments} completed payment session(s)</div></div><a href="/payment-setup">Open</a></div>
            <div class="setting-link"><div><strong>Cloud recordings</strong><div class="health-detail">{len(cloud_index)} indexed upload(s)</div></div><a href="/cloud-recording">Open</a></div>
            <div class="setting-link"><div><strong>Onboarding activity</strong><div class="health-detail">{len(onboarding_activity)} recorded activity item(s)</div></div><a href="/onboarding-admin">Open</a></div>
          </div>
        </article>
        <article class="admin-command-card">
          <div class="admin-command-head"><h2>Recent administrative activity</h2><div class="admin-command-actions"><a href="/audit-logs">View all</a></div></div>
          <div class="admin-command-list">{"".join(audit_rows) or '<div class="empty">No audit activity.</div>'}</div>
        </article>
        <article class="admin-command-card"><h2>Administrator tools</h2>
          <div class="admin-command-actions">
            <a href="/subscription-admin">Subscriptions</a>
            <a href="/billing-operations">Billing</a>
            <a href="/onboarding-admin">Onboarding</a>
            <a href="/business-users">Business users</a>
            <a href="/partner">Partner portal</a>
            <a href="/operations">Operations</a>
            <a href="/release-readiness">Release readiness</a>
          </div>
        </article>
      </aside>
    </section>"""
    return page_shell(
        "Administrator portal",
        "admin-portal",
        content,
    )



def send_payment_reminder_email(account: dict) -> tuple[bool, str]:
    recipient = str(account.get("billing_email") or "").strip()
    if not recipient:
        return False, "This customer does not have a billing email."
    if not SMTP_HOST or not SMTP_FROM:
        return False, "SMTP is not configured."
    company = str(account.get("company_name") or account.get("customer_name") or "Customer")
    message = EmailMessage()
    message["Subject"] = "AnyAiCam payment reminder"
    message["From"] = SMTP_FROM
    message["To"] = recipient
    message.set_content(
        "\n".join([
            f"Hello {company},",
            "",
            "This is a reminder that your AnyAiCam account requires billing attention.",
            "Please sign in to your customer portal to review your subscription, payment method, and invoices.",
            "",
            f"Customer portal: {invite_base_url()}/customer-portal",
            "",
            "If you have already resolved the issue, no further action is required.",
        ])
    )
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls(context=context)
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
        return True, "Payment reminder sent."
    except (OSError, smtplib.SMTPException) as error:
        return False, str(error)


@app.get("/admin-customers", response_class=HTMLResponse)
def administrator_customer_accounts_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page("Customer accounts", "admin-customers", "manage_settings")

    accounts = list(load_billing_accounts().values())
    requests = list(load_subscription_requests().values())
    onboarding_profiles = load_onboarding_profiles()

    def account_status(value: object) -> str:
        status = str(value or "unknown").strip().lower()
        return f'<span class="admin-command-badge {escape(status, quote=True)}">{escape(status.replace("_", " "))}</span>'

    account_cards = []
    for account in sorted(accounts, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True):
        account_id = str(account.get("id") or "")
        external_id = str(account.get("external_customer_id") or "")
        profile = onboarding_profiles.get(external_id) or onboarding_profiles.get(account_id) or {}
        related_requests = [
            item for item in requests
            if str(item.get("account_id") or item.get("customer_reference") or "") in {account_id, external_id}
            or str(item.get("user_email") or "").lower() == str(account.get("billing_email") or "").lower()
        ]
        account_cards.append(
            f'''<article class="admin-customer-item">
              <div class="admin-customer-top">
                <div>
                  <strong>{escape(str(account.get("company_name") or account.get("customer_name") or "Customer"))}</strong>
                  <div class="admin-customer-meta">
                    {escape(str(account.get("billing_email") or "No billing email"))}<br>
                    Customer ID: {escape(external_id or account_id or "—")} · Cycle: {escape(str(account.get("billing_cycle") or "—"))}<br>
                    Onboarding: {escape(str(profile.get("status") or "not started"))} · Open requests: {len(related_requests)}
                  </div>
                </div>
                {account_status(account.get("payment_status"))}
              </div>
              <div class="admin-customer-actions">
                <button class="admin-select-customer" type="button"
                  data-id="{escape(account_id, quote=True)}"
                  data-name="{escape(str(account.get("customer_name") or ""), quote=True)}"
                  data-company="{escape(str(account.get("company_name") or ""), quote=True)}"
                  data-email="{escape(str(account.get("billing_email") or ""), quote=True)}"
                  data-status="{escape(str(account.get("payment_status") or "current"), quote=True)}"
                  data-cycle="{escape(str(account.get("billing_cycle") or "monthly"), quote=True)}"
                  data-next="{escape(str(account.get("next_billing_date") or ""), quote=True)}"
                  data-notes="{escape(str(account.get("notes") or ""), quote=True)}">Manage</button>
                <button class="admin-reminder-customer" type="button" data-id="{escape(account_id, quote=True)}">Send payment reminder</button>
                <a href="/billing-operations">Billing history</a>
                <a href="/onboarding-admin">Onboarding</a>
              </div>
            </article>'''
        )

    content = f'''<header class="topbar"><div><p class="eyebrow">Administrator portal</p><h1>Customer account operations</h1></div><span class="pill">Account data only</span></header>
    <div class="admin-privacy-note"><strong>Privacy boundary:</strong> these controls manage billing and service status only. Customer video is not available from this page.</div>
    <section class="admin-customer-grid" style="margin-top:16px">
      <div class="admin-customer-list">{"".join(account_cards) or '<div class="empty">No customer billing accounts found.</div>'}</div>
      <aside class="admin-customer-detail">
        <h2>Manage customer</h2>
        <div class="health-detail">Select a customer account from the list.</div>
        <form id="admin-customer-form">
          <input id="admin-customer-id" type="hidden">
          <label>Customer name<input id="admin-customer-name"></label>
          <label>Company<input id="admin-customer-company"></label>
          <label>Billing email<input id="admin-customer-email" type="email"></label>
          <label>Payment status
            <select id="admin-customer-status">
              <option value="current">Current</option><option value="trial">Trial</option>
              <option value="past_due">Past due</option><option value="suspended">Suspended</option>
              <option value="cancelled">Cancelled</option>
            </select>
          </label>
          <label>Billing cycle<select id="admin-customer-cycle"><option value="monthly">Monthly</option><option value="annual">Annual</option></select></label>
          <label>Next billing date<input id="admin-customer-next" type="date"></label>
          <label>Internal notes<textarea id="admin-customer-notes"></textarea></label>
          <div class="admin-customer-actions">
            <button class="success" type="submit">Save account</button>
            <button class="danger" id="admin-customer-suspend" type="button">Suspend</button>
            <button class="success" id="admin-customer-reactivate" type="button">Reactivate</button>
          </div>
          <div class="admin-customer-feedback" id="admin-customer-feedback">No changes submitted.</div>
        </form>
      </aside>
    </section>'''

    scripts = '''
    <script>
    const customerForm=document.getElementById('admin-customer-form');
    const feedback=document.getElementById('admin-customer-feedback');
    const field=id=>document.getElementById(id);
    function selectCustomer(button){
      field('admin-customer-id').value=button.dataset.id||'';
      field('admin-customer-name').value=button.dataset.name||'';
      field('admin-customer-company').value=button.dataset.company||'';
      field('admin-customer-email').value=button.dataset.email||'';
      field('admin-customer-status').value=button.dataset.status||'current';
      field('admin-customer-cycle').value=button.dataset.cycle||'monthly';
      field('admin-customer-next').value=button.dataset.next||'';
      field('admin-customer-notes').value=button.dataset.notes||'';
      feedback.className='admin-customer-feedback';
      feedback.textContent='Customer loaded. Review and save changes.';
    }
    document.querySelectorAll('.admin-select-customer').forEach(button=>button.onclick=()=>selectCustomer(button));
    async function saveCustomer(statusOverride=null){
      const accountId=field('admin-customer-id').value;
      if(!accountId){feedback.className='admin-customer-feedback error';feedback.textContent='Select a customer first.';return}
      const payload={
        customer_name:field('admin-customer-name').value,
        company_name:field('admin-customer-company').value,
        billing_email:field('admin-customer-email').value,
        external_customer_id:accountId,
        payment_status:statusOverride||field('admin-customer-status').value,
        billing_cycle:field('admin-customer-cycle').value,
        next_billing_date:field('admin-customer-next').value,
        notes:field('admin-customer-notes').value
      };
      feedback.className='admin-customer-feedback';feedback.textContent='Saving…';
      try{
        const response=await fetch(`/api/billing/accounts/${encodeURIComponent(accountId)}`,{
          method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
        });
        const result=await response.json();
        if(!response.ok)throw new Error(result.detail||result.message||'Could not update account.');
        feedback.className='admin-customer-feedback success';feedback.textContent=result.message||'Customer account updated.';
        setTimeout(()=>location.reload(),700);
      }catch(error){feedback.className='admin-customer-feedback error';feedback.textContent=error.message}
    }
    customerForm.onsubmit=event=>{event.preventDefault();saveCustomer()};
    field('admin-customer-suspend').onclick=()=>saveCustomer('suspended');
    field('admin-customer-reactivate').onclick=()=>saveCustomer('current');
    document.querySelectorAll('.admin-reminder-customer').forEach(button=>button.onclick=async()=>{
      button.disabled=true;
      try{
        const response=await fetch(`/api/admin/customers/${encodeURIComponent(button.dataset.id)}/payment-reminder`,{method:'POST'});
        const result=await response.json();
        if(!response.ok)throw new Error(result.detail||result.message||'Could not send reminder.');
        showToast(result.message);
      }catch(error){showToast(error.message)}
      finally{button.disabled=false}
    });
    </script>'''
    return page_shell("Customer accounts", "admin-customers", content, scripts)


@app.put("/api/billing/accounts/{account_id}")
async def update_billing_account_from_admin(account_id: str, request: Request, payload: BillingAccountUpdateModel) -> JSONResponse:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return JSONResponse({"detail": "Administrator access is required."}, status_code=403)
    accounts = load_billing_accounts()
    if account_id not in accounts:
        return JSONResponse({"detail": "Billing account not found."}, status_code=404)
    existing = accounts[account_id]
    existing.update(payload.model_dump())
    existing["id"] = account_id
    existing["updated_at"] = datetime.now().isoformat()
    existing["updated_by"] = user.get("display_name") or user.get("email") or "Administrator"
    accounts[account_id] = existing
    save_billing_accounts(accounts)
    record_audit(request, "billing.account_updated", f"billing_account:{account_id}", f"Payment status set to {existing.get('payment_status')}.")
    return JSONResponse({"status": "complete", "message": "Customer billing account updated.", "account": existing})


@app.post("/api/admin/customers/{account_id}/payment-reminder")
async def send_admin_payment_reminder(account_id: str, request: Request) -> JSONResponse:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return JSONResponse({"detail": "Administrator access is required."}, status_code=403)
    account = load_billing_accounts().get(account_id)
    if not account:
        return JSONResponse({"detail": "Billing account not found."}, status_code=404)
    sent, message = send_payment_reminder_email(account)
    record_audit(request, "billing.payment_reminder", f"billing_account:{account_id}", message, "success" if sent else "failed")
    return JSONResponse({"status": "complete" if sent else "error", "message": message}, status_code=200 if sent else 503)



@app.get("/admin-activation", response_class=HTMLResponse)
def administrator_activation_operations_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page("Activation operations", "admin-activation", "manage_settings")

    profiles = list(load_onboarding_profiles().values())
    activity = load_onboarding_activity()
    cloud = cloud_configuration_snapshot()

    submitted = sum(str(profile.get("status") or "").lower() in {"submitted", "review", "pending_review"} for profile in profiles)
    approved = sum(str(profile.get("status") or "").lower() in {"approved", "active", "completed", "complete"} for profile in profiles)
    cloud_requested = sum(bool(profile.get("cloud_recording_requested")) for profile in profiles)
    software_deployments = sum(str(profile.get("deployment_type") or "").lower() == "software" for profile in profiles)
    appliance_deployments = sum(str(profile.get("deployment_type") or "").lower() in {"edge", "appliance", "device"} for profile in profiles)

    def badge(value: object) -> str:
        status = str(value or "in_progress").strip().lower()
        return f'<span class="admin-command-badge {escape(status, quote=True)}">{escape(status.replace("_", " "))}</span>'

    profile_cards = []
    for profile in sorted(profiles, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True):
        progress = onboarding_progress(profile)
        profile_id = str(profile.get("id") or profile.get("user_id") or "")
        organization = str(profile.get("organization_name") or profile.get("contact_name") or profile.get("user_email") or "Customer")
        completed = set(profile.get("completed_steps") or [])
        ready = progress.get("ready_to_submit", False)
        checklist = [
            ("Organization", "organization" in completed),
            ("Site", "site" in completed),
            ("Deployment", "deployment" in completed),
            ("Cameras", "cameras" in completed),
            ("Terms", "terms" in completed),
        ]
        checklist_text = " · ".join(("✓ " if done else "○ ") + label for label, done in checklist)
        profile_cards.append(
            f'''<article class="activation-card">
              <div class="activation-head">
                <div>
                  <strong>{escape(organization)}</strong>
                  <div class="activation-meta">
                    {escape(str(profile.get("contact_email") or profile.get("user_email") or "No email"))}<br>
                    Deployment: {escape(str(profile.get("deployment_type") or "edge"))}
                    · Cameras: {escape(str(profile.get("expected_camera_count") or 0))}
                    · Cloud recording: {"Requested" if profile.get("cloud_recording_requested") else "No"}<br>
                    {escape(checklist_text)}
                  </div>
                </div>
                {badge(profile.get("status"))}
              </div>
              <div class="activation-progress"><span style="width:{progress.get("percent", 0)}%"></span></div>
              <div class="activation-actions">
                <button class="activation-select" type="button"
                  data-id="{escape(profile_id, quote=True)}"
                  data-name="{escape(organization, quote=True)}"
                  data-status="{escape(str(profile.get("status") or "in_progress"), quote=True)}"
                  data-note="{escape(str(profile.get("admin_note") or ""), quote=True)}"
                  data-assigned="{escape(str(profile.get("assigned_to") or ""), quote=True)}"
                  data-ready="{"true" if ready else "false"}">Review</button>
                <a href="/onboarding-admin">Full onboarding record</a>
                <a href="/partner/appliance-dashboard">Appliance dashboard</a>
              </div>
            </article>'''
        )

    cloud_status = "Ready" if cloud.get("cloud_foundation_ready") else "Configuration incomplete"
    content = f'''<header class="topbar"><div><p class="eyebrow">Administrator portal</p><h1>Activation operations</h1></div><span class="pill">No customer video access</span></header>
    <div class="admin-privacy-note"><strong>Activation boundary:</strong> this page reviews account readiness and deployment handoff. It does not expose customer camera streams or credentials.</div>
    <section class="activation-summary" style="margin-top:16px">
      <article class="activation-stat"><span>Onboarding profiles</span><strong>{len(profiles)}</strong></article>
      <article class="activation-stat"><span>Pending review</span><strong>{submitted}</strong></article>
      <article class="activation-stat"><span>Approved/active</span><strong>{approved}</strong></article>
      <article class="activation-stat"><span>Cloud requested</span><strong>{cloud_requested}</strong></article>
      <article class="activation-stat"><span>Device / software</span><strong>{appliance_deployments} / {software_deployments}</strong></article>
    </section>
    <section class="activation-layout">
      <div class="activation-list">{"".join(profile_cards) or '<div class="empty">No onboarding profiles found.</div>'}</div>
      <aside class="admin-customer-detail">
        <h2>Activation review</h2>
        <div class="health-detail">Select a customer to update onboarding status and assignment.</div>
        <form id="activation-review-form">
          <input id="activation-profile-id" type="hidden">
          <label>Customer<input id="activation-customer-name" disabled></label>
          <label>Status
            <select id="activation-status">
              <option value="in_progress">In progress</option>
              <option value="submitted">Submitted</option>
              <option value="approved">Approved for activation</option>
              <option value="active">Active</option>
              <option value="needs_attention">Needs attention</option>
              <option value="cancelled">Cancelled</option>
            </select>
          </label>
          <label>Assigned administrator<input id="activation-assigned"></label>
          <label>Administrator note<textarea id="activation-note"></textarea></label>
          <div class="activation-actions">
            <button class="primary" type="submit">Save review</button>
            <button id="activation-approve" type="button">Approve for activation</button>
          </div>
          <div class="activation-feedback" id="activation-feedback">No profile selected.</div>
        </form>

        <h2 style="margin-top:22px">Activation checklist</h2>
        <div class="activation-checklist">
          <div class="activation-check"><strong>1. Paid plan confirmed</strong><small>Stripe webhook or approved manual billing record must confirm the plan before licenses are activated.</small></div>
          <div class="activation-check"><strong>2. Deployment selected</strong><small>Customer chooses software-only or AnyAiCam appliance deployment.</small></div>
          <div class="activation-check"><strong>3. Cloud ID assigned</strong><small>Use the existing appliance dashboard to provision or verify the Cloud ID.</small></div>
          <div class="activation-check"><strong>4. Camera entitlement verified</strong><small>Camera count, resolution, recording mode, retention and analytics must match the active subscription.</small></div>
          <div class="activation-check"><strong>5. Customer completes discovery</strong><small>The customer scans and adds cameras from their own authorized setup workflow.</small></div>
        </div>

        <h2 style="margin-top:22px">Cloud readiness</h2>
        <div class="activation-check"><strong>{escape(cloud_status)}</strong><small>Missing: {escape(", ".join(cloud.get("missing_cloud_requirements") or []) or "None")}</small></div>
        <div class="activation-actions"><a href="/partner/appliance-dashboard">Open appliance dashboard</a><a href="/cloud-recording">Cloud recording</a></div>
        <div class="health-detail" style="margin-top:10px">{len(activity)} onboarding activity record(s) are available for audit review.</div>
      </aside>
    </section>'''

    scripts = '''
    <script>
    const profileId=document.getElementById('activation-profile-id');
    const customerName=document.getElementById('activation-customer-name');
    const statusField=document.getElementById('activation-status');
    const assignedField=document.getElementById('activation-assigned');
    const noteField=document.getElementById('activation-note');
    const feedback=document.getElementById('activation-feedback');

    document.querySelectorAll('.activation-select').forEach(button=>button.onclick=()=>{
      profileId.value=button.dataset.id||'';
      customerName.value=button.dataset.name||'';
      statusField.value=button.dataset.status||'in_progress';
      assignedField.value=button.dataset.assigned||'';
      noteField.value=button.dataset.note||'';
      feedback.className='activation-feedback';
      feedback.textContent=button.dataset.ready==='true'
        ? 'This profile has the minimum onboarding steps needed for activation review.'
        : 'This profile is incomplete. Review the missing onboarding steps before approval.';
    });

    async function saveActivation(statusOverride=null){
      if(!profileId.value){
        feedback.className='activation-feedback error';
        feedback.textContent='Select a customer profile first.';
        return;
      }
      feedback.className='activation-feedback';
      feedback.textContent='Saving activation review…';
      try{
        const response=await fetch(`/api/onboarding/admin/${encodeURIComponent(profileId.value)}`,{
          method:'PUT',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            status:statusOverride||statusField.value,
            admin_note:noteField.value,
            assigned_to:assignedField.value
          })
        });
        const result=await response.json();
        if(!response.ok)throw new Error(result.detail||result.message||'Could not update onboarding status.');
        feedback.className='activation-feedback success';
        feedback.textContent='Activation review updated.';
        setTimeout(()=>location.reload(),700);
      }catch(error){
        feedback.className='activation-feedback error';
        feedback.textContent=error.message;
      }
    }

    document.getElementById('activation-review-form').onsubmit=event=>{
      event.preventDefault();
      saveActivation();
    };
    document.getElementById('activation-approve').onclick=()=>saveActivation('approved');
    </script>'''

    return page_shell("Activation operations", "admin-activation", content, scripts)



@app.get("/admin-support", response_class=HTMLResponse)
def administrator_support_compliance_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page("Support & compliance", "admin-support", "manage_settings")

    tickets = list(load_billing_support_tickets().values())
    audit_entries = load_audit_entries(100)
    accounts = list(load_billing_accounts().values())

    open_tickets = sum(
        str(item.get("status") or "").lower() not in {"resolved", "closed", "complete", "completed"}
        for item in tickets
    )
    high_priority = sum(
        str(item.get("priority") or "").lower() == "high"
        and str(item.get("status") or "").lower() not in {"resolved", "closed", "complete", "completed"}
        for item in tickets
    )
    failed_audit = sum(str(item.get("outcome") or "").lower() == "failed" for item in audit_entries)
    payment_attention = sum(
        str(item.get("payment_status") or "").lower() in {"past_due", "failed", "declined", "suspended"}
        for item in accounts
    )
    unique_users = len({str(item.get("user_id") or "") for item in audit_entries if item.get("user_id")})

    def badge(value: object) -> str:
        status = str(value or "unknown").strip().lower()
        return f'<span class="admin-command-badge {escape(status, quote=True)}">{escape(status.replace("_", " "))}</span>'

    ticket_cards = []
    for ticket in sorted(tickets, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True):
        ticket_id = str(ticket.get("id") or "")
        ticket_cards.append(
            f'''<article class="support-ticket">
              <div class="support-head">
                <div>
                  <strong>{escape(str(ticket.get("subject") or "Billing support"))}</strong>
                  <div class="support-meta">
                    {escape(str(ticket.get("user_name") or ticket.get("user_email") or "Customer"))}
                    · Priority: {escape(str(ticket.get("priority") or "normal"))}<br>
                    {escape(str(ticket.get("message") or ""))}<br>
                    Opened: {escape(str(ticket.get("created_at") or "—"))}
                  </div>
                </div>
                {badge(ticket.get("status"))}
              </div>
              <div class="support-actions">
                <button class="support-select" type="button"
                  data-id="{escape(ticket_id, quote=True)}"
                  data-subject="{escape(str(ticket.get("subject") or ""), quote=True)}"
                  data-status="{escape(str(ticket.get("status") or "open"), quote=True)}"
                  data-note="{escape(str(ticket.get("admin_note") or ""), quote=True)}">Review ticket</button>
                <a href="/audit-logs">Audit history</a>
              </div>
            </article>'''
        )

    audit_rows = []
    for item in audit_entries[:12]:
        audit_rows.append(
            f'''<div class="audit-mini-row">
              <strong>{escape(str(item.get("action") or "Activity"))}</strong>
              <small>{escape(str(item.get("user_name") or item.get("user_id") or "System"))}
              · {escape(str(item.get("resource") or ""))}
              · {escape(str(item.get("outcome") or "unknown"))}<br>
              {escape(str(item.get("timestamp") or ""))}</small>
            </div>'''
        )

    content = f'''<header class="topbar"><div><p class="eyebrow">Administrator portal</p><h1>Support & compliance</h1></div><span class="pill">Audited account operations</span></header>
    <div class="admin-privacy-note"><strong>Privacy boundary:</strong> this page contains support, billing, and audit records only. It does not expose customer video or camera credentials.</div>
    <section class="support-summary" style="margin-top:16px">
      <article class="support-stat"><span>Support tickets</span><strong>{len(tickets)}</strong></article>
      <article class="support-stat"><span>Open tickets</span><strong>{open_tickets}</strong></article>
      <article class="support-stat"><span>High priority</span><strong>{high_priority}</strong></article>
      <article class="support-stat"><span>Payment attention</span><strong>{payment_attention}</strong></article>
      <article class="support-stat"><span>Failed audit events</span><strong>{failed_audit}</strong></article>
    </section>
    <section class="support-layout">
      <div class="support-list">{"".join(ticket_cards) or '<div class="empty">No billing-support tickets found.</div>'}</div>
      <aside class="support-detail">
        <h2>Resolve support ticket</h2>
        <form id="support-review-form">
          <input id="support-ticket-id" type="hidden">
          <label>Subject<input id="support-ticket-subject" disabled></label>
          <label>Status
            <select id="support-ticket-status">
              <option value="open">Open</option>
              <option value="in_progress">In progress</option>
              <option value="waiting_customer">Waiting on customer</option>
              <option value="resolved">Resolved</option>
              <option value="closed">Closed</option>
            </select>
          </label>
          <label>Administrator note<textarea id="support-ticket-note"></textarea></label>
          <div class="support-actions">
            <button class="primary" type="submit">Save ticket</button>
            <button id="support-resolve-ticket" type="button">Mark resolved</button>
          </div>
          <div class="support-feedback" id="support-feedback">No ticket selected.</div>
        </form>

        <h2 style="margin-top:22px">Recent audit activity</h2>
        <div class="health-detail">{len(audit_entries)} audit item(s), {unique_users} unique user(s).</div>
        <div class="audit-mini">{"".join(audit_rows) or '<div class="empty">No audit activity.</div>'}</div>
        <div class="support-actions"><a href="/audit-logs">Open complete audit log</a><a href="/api/support-bundle">Download support bundle</a></div>
      </aside>
    </section>'''

    scripts = '''
    <script>
    const supportId=document.getElementById('support-ticket-id');
    const supportSubject=document.getElementById('support-ticket-subject');
    const supportStatus=document.getElementById('support-ticket-status');
    const supportNote=document.getElementById('support-ticket-note');
    const supportFeedback=document.getElementById('support-feedback');

    document.querySelectorAll('.support-select').forEach(button=>button.onclick=()=>{
      supportId.value=button.dataset.id||'';
      supportSubject.value=button.dataset.subject||'';
      supportStatus.value=button.dataset.status||'open';
      supportNote.value=button.dataset.note||'';
      supportFeedback.className='support-feedback';
      supportFeedback.textContent='Ticket loaded. Review and save changes.';
    });

    async function saveSupportTicket(statusOverride=null){
      if(!supportId.value){
        supportFeedback.className='support-feedback error';
        supportFeedback.textContent='Select a ticket first.';
        return;
      }
      supportFeedback.className='support-feedback';
      supportFeedback.textContent='Saving ticket…';
      try{
        const response=await fetch(`/api/billing-support-tickets/${encodeURIComponent(supportId.value)}`,{
          method:'PUT',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            status:statusOverride||supportStatus.value,
            admin_note:supportNote.value
          })
        });
        const result=await response.json();
        if(!response.ok)throw new Error(result.detail||result.message||'Could not update ticket.');
        supportFeedback.className='support-feedback success';
        supportFeedback.textContent=result.message||'Support ticket updated.';
        setTimeout(()=>location.reload(),700);
      }catch(error){
        supportFeedback.className='support-feedback error';
        supportFeedback.textContent=error.message;
      }
    }

    document.getElementById('support-review-form').onsubmit=event=>{
      event.preventDefault();
      saveSupportTicket();
    };
    document.getElementById('support-resolve-ticket').onclick=()=>saveSupportTicket('resolved');
    </script>'''

    return page_shell("Support & compliance", "admin-support", content, scripts)


@app.get("/subscription-admin", response_class=HTMLResponse)
def subscription_admin_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Subscription administration",
            "subscription-admin",
            "manage_settings",
        )

    content = """<header class="topbar"><div><p class="eyebrow">Customer subscription management</p><h1>Subscription administration</h1></div></header>
    <section class="subscription-stack">
      <article class="subscription-card"><h2>Upgrade and expansion requests</h2><div id="admin-subscription-requests"><div class="empty">Loading…</div></div></article>
      <article class="subscription-card"><h2>Billing support tickets</h2><div id="admin-billing-tickets"><div class="empty">Loading…</div></div></article>
    </section>"""

    scripts = """<script>
    const requestsHost=document.getElementById('admin-subscription-requests');
    const ticketsHost=document.getElementById('admin-billing-tickets');
    function escAdmin(value){return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]))}
    async function loadAdmin(){
      const response=await fetch('/api/subscription-portal');
      const data=await response.json();
      requestsHost.innerHTML=(data.requests||[]).map(item=>`<div class="subscription-card"><strong>${escAdmin(item.user_name)} — ${escAdmin(item.request_type.replaceAll('_',' '))}</strong><div class="subscription-meta">Plan: ${escAdmin(item.requested_plan||'—')} · Cameras: ${item.requested_camera_limit??'—'}<br>${escAdmin(item.reason||'')}</div><div class="subscription-actions"><button onclick="updateRequest('${item.id}','approved')">Approve</button><button onclick="updateRequest('${item.id}','declined')">Decline</button><button onclick="updateRequest('${item.id}','completed')">Complete</button></div></div>`).join('')||'<div class="empty">No subscription requests.</div>';
      ticketsHost.innerHTML=(data.tickets||[]).map(item=>`<div class="subscription-card"><strong>${escAdmin(item.user_name)} — ${escAdmin(item.subject)}</strong><div class="subscription-meta">${escAdmin(item.priority)} · ${escAdmin(item.message)}</div><div class="subscription-actions"><button onclick="updateTicket('${item.id}','in_progress')">In progress</button><button onclick="updateTicket('${item.id}','resolved')">Resolve</button><button onclick="updateTicket('${item.id}','closed')">Close</button></div></div>`).join('')||'<div class="empty">No billing tickets.</div>';
    }
    async function updateRequest(id,status){const note=prompt('Admin note')||'';const response=await fetch(`/api/subscription-requests/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status,admin_note:note})});const data=await response.json();if(!response.ok)return showToast(data.detail||'Update failed.');showToast(data.message);loadAdmin()}
    async function updateTicket(id,status){const note=prompt('Support note')||'';const response=await fetch(`/api/billing-support-tickets/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status,admin_note:note})});const data=await response.json();if(!response.ok)return showToast(data.detail||'Update failed.');showToast(data.message);loadAdmin()}
    loadAdmin();
    </script>"""
    return page_shell(
        "Subscription administration",
        "subscription-admin",
        content,
        scripts,
    )



@app.get("/api/payments/configuration")
def payment_configuration_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(
            status_code=403,
            detail="Settings permission is required.",
        )
    sessions = list(load_payment_sessions().values())
    sessions.sort(
        key=lambda item: item.get("created_at", ""),
        reverse=True,
    )
    return {
        "configuration": stripe_configuration_snapshot(),
        "sessions": sessions[:100],
    }


@app.post("/api/payments/checkout")
def create_stripe_checkout(
    payload: StripeCheckoutCreateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    plan = payload.plan.strip().lower()
    if plan not in LICENSE_PLAN_FEATURES:
        raise HTTPException(status_code=400, detail="Invalid plan.")

    price_id = stripe_price_map().get(plan, "")
    if not price_id:
        raise HTTPException(
            status_code=503,
            detail=f"Stripe price is not configured for {plan}.",
        )
    if not PUBLIC_BASE_URL:
        raise HTTPException(
            status_code=503,
            detail="ANYAICAM_PUBLIC_URL is required for Stripe Checkout.",
        )

    account = billing_account_for_user(user)
    account_id = str(account.get("id") or "primary")
    quantity = max(1, min(100, int(payload.quantity)))
    fields = [
        ("mode", "subscription"),
        ("success_url", f"{PUBLIC_BASE_URL}/subscription-portal?payment=success&session_id={{CHECKOUT_SESSION_ID}}"),
        ("cancel_url", f"{PUBLIC_BASE_URL}/subscription-portal?payment=cancelled"),
        ("client_reference_id", account_id),
        ("line_items[0][price]", price_id),
        ("line_items[0][quantity]", str(quantity)),
        ("metadata[anyaicam_account_id]", account_id),
        ("metadata[anyaicam_user_id]", str(user.get("id") or "")),
        ("metadata[anyaicam_plan]", plan),
        ("subscription_data[metadata][anyaicam_account_id]", account_id),
        ("subscription_data[metadata][anyaicam_plan]", plan),
        ("allow_promotion_codes", "true"),
    ]
    customer_id = stripe_customer_id_for_account(account)
    if customer_id:
        fields.append(("customer", customer_id))
    elif account.get("billing_email") or user.get("email"):
        fields.append((
            "customer_email",
            str(account.get("billing_email") or user.get("email")),
        ))

    session = stripe_api_post("/v1/checkout/sessions", fields)
    session_id = str(session.get("id") or "")
    checkout_url = str(session.get("url") or "")
    if not session_id or not checkout_url:
        raise HTTPException(
            status_code=502,
            detail="Stripe did not return a Checkout Session URL.",
        )

    sessions = load_payment_sessions()
    sessions[session_id] = {
        "id": session_id,
        "account_id": account_id,
        "user_id": user.get("id"),
        "user_email": user.get("email"),
        "plan": plan,
        "quantity": quantity,
        "status": str(session.get("status") or "open"),
        "payment_status": str(session.get("payment_status") or "unpaid"),
        "checkout_url": checkout_url,
        "created_at": datetime.now().isoformat(),
        "livemode": bool(session.get("livemode")),
    }
    save_payment_sessions(sessions)
    structured_log(
        "stripe.checkout_created",
        session_id=session_id,
        account_id=account_id,
        plan=plan,
        quantity=quantity,
        user_id=user.get("id"),
    )
    return {
        "status": "complete",
        "session_id": session_id,
        "checkout_url": checkout_url,
        "message": "Stripe Checkout Session created.",
    }


@app.post("/api/payments/customer-portal")
def create_stripe_customer_portal(
    payload: StripePortalCreateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not PUBLIC_BASE_URL:
        raise HTTPException(
            status_code=503,
            detail="ANYAICAM_PUBLIC_URL is required.",
        )
    account = billing_account_for_user(user)
    customer_id = stripe_customer_id_for_account(account)
    if not customer_id:
        raise HTTPException(
            status_code=400,
            detail="This billing account does not have a Stripe customer ID yet.",
        )

    return_path = payload.return_path.strip()
    if not return_path.startswith("/") or return_path.startswith("//"):
        return_path = "/subscription-portal"

    portal = stripe_api_post(
        "/v1/billing_portal/sessions",
        [
            ("customer", customer_id),
            ("return_url", f"{PUBLIC_BASE_URL}{return_path}"),
        ],
    )
    portal_url = str(portal.get("url") or "")
    if not portal_url:
        raise HTTPException(
            status_code=502,
            detail="Stripe did not return a customer portal URL.",
        )
    structured_log(
        "stripe.customer_portal_created",
        customer_id=customer_id,
        account_id=account.get("id"),
        user_id=user.get("id"),
    )
    return {
        "status": "complete",
        "portal_url": portal_url,
        "message": "Stripe customer portal created.",
    }


@app.post("/api/payments/stripe/webhook")
async def stripe_webhook(request: Request) -> dict:
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    if not verify_stripe_webhook_signature(payload, signature):
        structured_log(
            "stripe.webhook_rejected",
            level="warning",
            reason="invalid_signature",
        )
        raise HTTPException(
            status_code=400,
            detail="Invalid Stripe webhook signature.",
        )

    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid webhook payload.",
        ) from exc

    if not record_stripe_webhook_event(event):
        return {
            "status": "complete",
            "duplicate": True,
        }

    process_stripe_webhook_event(event)
    structured_log(
        "stripe.webhook_processed",
        event_id=event.get("id"),
        event_type=event.get("type"),
        livemode=bool(event.get("livemode")),
    )
    return {
        "status": "complete",
        "event_id": event.get("id"),
        "event_type": event.get("type"),
    }


@app.get("/payment-setup", response_class=HTMLResponse)
def payment_setup_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Payment setup",
            "payment-setup",
            "manage_settings",
        )

    content = """<header class="topbar"><div><p class="eyebrow">Stripe integration</p><h1>Payment setup</h1></div></header>
    <section class="payment-summary" id="payment-summary"></section>
    <section class="payment-layout">
      <form class="payment-card payment-form" id="payment-test-form">
        <h2>Create Checkout Session</h2>
        <div class="payment-warning">Use Stripe test keys and test prices until production verification is complete. Secret keys must remain in environment variables.</div>
        <label>Plan<select id="payment-plan"><option value="starter">Starter</option><option value="professional" selected>Professional</option><option value="enterprise">Enterprise</option></select></label>
        <label>Quantity<input id="payment-quantity" type="number" min="1" max="100" value="1"></label>
        <button class="action-button" type="submit">Open Stripe Checkout</button>
        <div class="payment-actions"><button id="open-stripe-portal" type="button">Open customer portal</button></div>
      </form>
      <div>
        <article class="payment-card">
          <h2>Configuration readiness</h2>
          <div class="payment-readiness" id="payment-readiness"></div>
        </article>
        <article class="payment-card">
          <h2>Checkout sessions</h2>
          <div id="payment-sessions"><div class="empty">Loading sessions…</div></div>
        </article>
      </div>
    </section>"""

    scripts = """<script>
    const summaryHost=document.getElementById('payment-summary');
    const readinessHost=document.getElementById('payment-readiness');
    const sessionsHost=document.getElementById('payment-sessions');

    function escPayment(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    async function loadPaymentConfiguration(){
      const response=await fetch('/api/payments/configuration');
      const data=await response.json();
      const config=data.configuration||{};
      summaryHost.innerHTML=`
        <article class="stat"><span class="stat-label">Checkout</span><span class="stat-value">${config.checkout_ready?'Ready':'Setup'}</span></article>
        <article class="stat"><span class="stat-label">Webhook</span><span class="stat-value">${config.webhook_ready?'Ready':'Setup'}</span></article>
        <article class="stat"><span class="stat-label">Public URL</span><span class="stat-value">${config.public_base_url_configured?'Ready':'Missing'}</span></article>
        <article class="stat"><span class="stat-label">Live charging</span><span class="stat-value">${config.live_charging_enabled?'Enabled':'Disabled'}</span></article>`;

      const rows=[
        ['Secret key',config.secret_key_configured],
        ['Webhook secret',config.webhook_secret_configured],
        ['Public base URL',config.public_base_url_configured],
        ['Starter price',config.prices?.starter],
        ['Professional price',config.prices?.professional],
        ['Enterprise price',config.prices?.enterprise]
      ];
      readinessHost.innerHTML=rows.map(([name,ready])=>`<div class="payment-readiness-row"><strong>${escPayment(name)}</strong><div class="health-detail">${ready?'Configured':'Not configured'}</div></div>`).join('');

      sessionsHost.innerHTML=`<table class="payment-table"><thead><tr><th>Created</th><th>Plan</th><th>Status</th><th>Payment</th><th>Mode</th></tr></thead><tbody>${(data.sessions||[]).map(item=>`<tr><td>${escPayment((item.created_at||'').replace('T',' ').slice(0,19))}</td><td>${escPayment(item.plan)}</td><td>${escPayment(item.status)}</td><td>${escPayment(item.payment_status)}</td><td>${item.livemode?'Live':'Test'}</td></tr>`).join('')||'<tr><td colspan="5">No Checkout Sessions created.</td></tr>'}</tbody></table>`;
    }

    document.getElementById('payment-test-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const response=await fetch('/api/payments/checkout',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          plan:document.getElementById('payment-plan').value,
          quantity:Number(document.getElementById('payment-quantity').value||1)
        })
      });
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not create Checkout Session.');
      window.location.href=data.checkout_url;
    });

    document.getElementById('open-stripe-portal').addEventListener('click',async()=>{
      const response=await fetch('/api/payments/customer-portal',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({return_path:'/subscription-portal'})
      });
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not open customer portal.');
      window.location.href=data.portal_url;
    });

    loadPaymentConfiguration();
    </script>"""
    return page_shell(
        "Payment setup",
        "payment-setup",
        content,
        scripts,
    )



@app.get("/api/onboarding")
def onboarding_api(request: Request) -> dict:
    user = current_user(request)
    profile = onboarding_profile_for_user(user)
    return {
        "profile": profile,
        "progress": onboarding_progress(profile),
        "steps": ONBOARDING_STEPS,
        "activity": [
            item
            for item in reversed(load_onboarding_activity())
            if item.get("profile_id") == profile.get("id")
        ][:100],
    }


@app.put("/api/onboarding")
def update_onboarding_profile(
    payload: OnboardingProfileUpdateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    profile = onboarding_profile_for_user(user)
    profiles = load_onboarding_profiles()

    if payload.deployment_type not in {"edge", "cloud", "hybrid"}:
        raise HTTPException(status_code=400, detail="Invalid deployment type.")
    if payload.edge_device_platform not in {"windows", "linux", "macos", "other"}:
        raise HTTPException(status_code=400, detail="Invalid edge device platform.")
    if payload.expected_camera_count < 1:
        raise HTTPException(status_code=400, detail="Expected camera count must be positive.")
    if payload.retention_days_requested < 1:
        raise HTTPException(status_code=400, detail="Retention days must be positive.")
    if payload.cloud_id_device_type not in {"hardware_adapter", "software_bridge"}:
        raise HTTPException(status_code=400, detail="Invalid Cloud ID device type.")

    normalized_cloud_id = normalize_cloud_id(payload.cloud_id)
    existing_owner = find_cloud_id_owner(
        normalized_cloud_id,
        exclude_user_id=str(profile.get("user_id") or ""),
    ) if normalized_cloud_id else None
    if existing_owner:
        raise HTTPException(
            status_code=409,
            detail="That Cloud ID is already assigned to another customer account.",
        )

    previous_cloud_id = str(profile.get("cloud_id") or "")
    cloud_id_changed = normalized_cloud_id != previous_cloud_id

    profile.update({
        "organization_name": payload.organization_name.strip(),
        "contact_name": payload.contact_name.strip(),
        "contact_email": payload.contact_email.strip(),
        "contact_phone": payload.contact_phone.strip(),
        "site_name": payload.site_name.strip(),
        "site_address": payload.site_address.strip(),
        "timezone_name": payload.timezone_name.strip(),
        "deployment_type": payload.deployment_type,
        "expected_camera_count": int(payload.expected_camera_count),
        "camera_manufacturer": payload.camera_manufacturer.strip(),
        "cloud_recording_requested": payload.cloud_recording_requested,
        "retention_days_requested": int(payload.retention_days_requested),
        "edge_device_name": payload.edge_device_name.strip(),
        "edge_device_platform": payload.edge_device_platform,
        "cloud_id": normalized_cloud_id,
        "cloud_id_device_type": payload.cloud_id_device_type,
        "team_emails": sorted(set(
            item.strip().lower()
            for item in payload.team_emails
            if item.strip()
        )),
        "terms_accepted": payload.terms_accepted,
        "notes": payload.notes.strip(),
        "updated_at": datetime.now().isoformat(),
    })
    if cloud_id_changed:
        profile["cloud_id_status"] = "pending" if normalized_cloud_id else "not_submitted"
        profile["cloud_id_submitted_at"] = datetime.now().isoformat() if normalized_cloud_id else ""
        profile["cloud_id_reviewed_at"] = ""
        profile["cloud_id_reviewed_by"] = ""
        profile["cloud_id_admin_note"] = ""

    profiles[str(profile.get("user_id") or profile.get("id"))] = profile
    save_onboarding_profiles(profiles)
    append_onboarding_activity(
        user=user,
        profile_id=str(profile.get("id")),
        action="profile_updated",
        details="Customer onboarding profile updated.",
    )
    return {
        "status": "complete",
        "profile": profile,
        "progress": onboarding_progress(profile),
        "message": "Onboarding profile saved.",
    }


@app.put("/api/onboarding/step")
def update_onboarding_step(
    payload: OnboardingStepModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if payload.step not in ONBOARDING_STEPS:
        raise HTTPException(status_code=400, detail="Invalid onboarding step.")

    profile = onboarding_profile_for_user(user)
    completed = set(profile.get("completed_steps") or [])
    if payload.completed:
        completed.add(payload.step)
    else:
        completed.discard(payload.step)
    profile["completed_steps"] = sorted(completed)
    profile["updated_at"] = datetime.now().isoformat()

    profiles = load_onboarding_profiles()
    profiles[str(profile.get("user_id") or profile.get("id"))] = profile
    save_onboarding_profiles(profiles)
    append_onboarding_activity(
        user=user,
        profile_id=str(profile.get("id")),
        action="step_completed" if payload.completed else "step_reopened",
        details=payload.step,
    )
    return {
        "status": "complete",
        "profile": profile,
        "progress": onboarding_progress(profile),
        "message": "Onboarding progress updated.",
    }


@app.post("/api/onboarding/submit")
def submit_onboarding(request: Request) -> dict:
    user = current_user(request)
    profile = onboarding_profile_for_user(user)
    progress = onboarding_progress(profile)
    if not progress.get("ready_to_submit"):
        raise HTTPException(
            status_code=400,
            detail="Complete organization, site, deployment, cameras, and terms before submitting.",
        )

    profile["status"] = "submitted"
    profile["submitted_at"] = datetime.now().isoformat()
    profile["updated_at"] = profile["submitted_at"]

    profiles = load_onboarding_profiles()
    profiles[str(profile.get("user_id") or profile.get("id"))] = profile
    save_onboarding_profiles(profiles)
    append_onboarding_activity(
        user=user,
        profile_id=str(profile.get("id")),
        action="onboarding_submitted",
        details="Customer submitted onboarding for review.",
    )
    structured_log(
        "onboarding.submitted",
        profile_id=profile.get("id"),
        user_id=user.get("id"),
        expected_camera_count=profile.get("expected_camera_count"),
        deployment_type=profile.get("deployment_type"),
    )
    return {
        "status": "complete",
        "profile": profile,
        "message": "Onboarding submitted for review.",
    }


@app.get("/api/onboarding/admin")
def onboarding_admin_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    profiles = list(load_onboarding_profiles().values())
    profiles.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return {
        "profiles": [
            {**profile, "progress": onboarding_progress(profile)}
            for profile in profiles
        ],
        "activity": list(reversed(load_onboarding_activity()))[:300],
    }


@app.put("/api/onboarding/admin/{profile_id}")
def update_onboarding_admin(
    profile_id: str,
    payload: OnboardingAdminUpdateModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    if payload.status not in {
        "in_progress",
        "submitted",
        "approved",
        "needs_changes",
        "completed",
        "cancelled",
    }:
        raise HTTPException(status_code=400, detail="Invalid onboarding status.")

    profiles = load_onboarding_profiles()
    key = next(
        (
            candidate_key
            for candidate_key, profile in profiles.items()
            if str(profile.get("id")) == profile_id
        ),
        "",
    )
    if not key:
        raise HTTPException(status_code=404, detail="Onboarding profile not found.")

    profile = profiles[key]
    profile["status"] = payload.status
    profile["admin_note"] = payload.admin_note.strip()
    profile["assigned_to"] = payload.assigned_to.strip()
    profile["updated_at"] = datetime.now().isoformat()
    profile["updated_by"] = user.get("id")
    if payload.status == "completed":
        completed = set(profile.get("completed_steps") or [])
        completed.update(ONBOARDING_STEPS)
        profile["completed_steps"] = sorted(completed)
        profile["completed_at"] = datetime.now().isoformat()

    profiles[key] = profile
    save_onboarding_profiles(profiles)
    append_onboarding_activity(
        user=user,
        profile_id=profile_id,
        action="admin_status_updated",
        details=f"Status changed to {payload.status}. {payload.admin_note.strip()}",
    )
    return {
        "status": "complete",
        "profile": profile,
        "message": "Onboarding status updated.",
    }



@app.get("/api/cloud-id")
def cloud_id_api(request: Request) -> dict:
    user = current_user(request)
    profile = onboarding_profile_for_user(user)
    return {
        "cloud_id": profile.get("cloud_id", ""),
        "device_type": profile.get("cloud_id_device_type", "software_bridge"),
        "status": profile.get("cloud_id_status", "not_submitted"),
        "submitted_at": profile.get("cloud_id_submitted_at", ""),
        "reviewed_at": profile.get("cloud_id_reviewed_at", ""),
        "admin_note": profile.get("cloud_id_admin_note", ""),
        "customer_email": profile.get("user_email", user.get("email", "")),
    }


@app.post("/api/cloud-id")
async def submit_cloud_id(request: Request) -> dict:
    user = current_user(request)
    if str(user.get("role") or "").lower() not in CUSTOMER_PORTAL_ROLES:
        raise HTTPException(status_code=403, detail="Customer portal access is required.")

    payload = await request.json()
    cloud_id = normalize_cloud_id(payload.get("cloud_id", ""))
    device_type = str(payload.get("device_type") or "software_bridge").strip().lower()
    if not cloud_id:
        raise HTTPException(status_code=400, detail="Enter the Cloud ID from your adapter or software bridge.")
    if device_type not in {"hardware_adapter", "software_bridge"}:
        raise HTTPException(status_code=400, detail="Choose hardware adapter or software bridge.")

    profile = onboarding_profile_for_user(user)
    owner = find_cloud_id_owner(cloud_id, exclude_user_id=str(user.get("id") or ""))
    if owner:
        raise HTTPException(
            status_code=409,
            detail="That Cloud ID is already assigned to another customer account.",
        )

    profile.update({
        "cloud_id": cloud_id,
        "cloud_id_device_type": device_type,
        "cloud_id_status": "pending",
        "cloud_id_submitted_at": datetime.now().isoformat(),
        "cloud_id_reviewed_at": "",
        "cloud_id_reviewed_by": "",
        "cloud_id_admin_note": "",
        "updated_at": datetime.now().isoformat(),
    })
    profiles = load_onboarding_profiles()
    profiles[str(profile.get("user_id") or profile.get("id"))] = profile
    save_onboarding_profiles(profiles)
    append_onboarding_activity(
        user=user,
        profile_id=str(profile.get("id")),
        action="cloud_id_submitted",
        details=f"Submitted {device_type.replace('_', ' ')} Cloud ID {cloud_id}.",
    )
    record_audit(
        request,
        "submit",
        f"cloud-id:{cloud_id}",
        "Customer submitted Cloud ID for administrator verification.",
    )
    return {
        "status": "complete",
        "message": "Cloud ID submitted for master-administrator verification.",
        "cloud_id": cloud_id,
        "cloud_id_status": "pending",
    }


@app.post("/api/cloud-id/admin/{profile_id}")
def review_cloud_id(
    profile_id: str,
    payload: CloudIdAdminReviewModel,
    request: Request,
) -> dict:
    user = current_user(request)
    if not is_master_admin(user):
        raise HTTPException(status_code=403, detail="Master administrator access is required.")
    decision = payload.decision.strip().lower()
    if decision not in {"verified", "rejected"}:
        raise HTTPException(status_code=400, detail="Decision must be verified or rejected.")

    profiles = load_onboarding_profiles()
    key = next(
        (
            item_key
            for item_key, profile in profiles.items()
            if str(profile.get("id") or "") == profile_id
        ),
        "",
    )
    if not key:
        raise HTTPException(status_code=404, detail="Customer profile not found.")

    profile = profiles[key]
    if not profile.get("cloud_id"):
        raise HTTPException(status_code=400, detail="No Cloud ID has been submitted.")

    profile["cloud_id_status"] = decision
    profile["cloud_id_reviewed_at"] = datetime.now().isoformat()
    profile["cloud_id_reviewed_by"] = user.get("id")
    profile["cloud_id_admin_note"] = payload.note.strip()
    profile["updated_at"] = datetime.now().isoformat()
    profiles[key] = profile
    save_onboarding_profiles(profiles)

    append_onboarding_activity(
        user=user,
        profile_id=profile_id,
        action=f"cloud_id_{decision}",
        details=payload.note.strip() or f"Cloud ID marked {decision}.",
    )
    record_audit(
        request,
        decision,
        f"cloud-id:{profile.get('cloud_id')}",
        f"Cloud ID {decision} for {profile.get('user_email', '')}.",
    )
    return {
        "status": "complete",
        "message": f"Cloud ID {decision}.",
        "profile": profile,
    }


@app.get("/cloud-id", response_class=HTMLResponse)
def cloud_id_page(request: Request) -> str:
    user = current_user(request)
    role = str(user.get("role") or "").lower()

    if role in CUSTOMER_PORTAL_ROLES:
        content = """<header class="topbar"><div><p class="eyebrow">Device ownership</p><h1>Connect your Cloud ID</h1></div></header>
        <section class="panel">
          <div class="panel-head"><div><h2>Assign your adapter or software bridge</h2><div class="health-detail">Enter the Cloud ID shown on the ANY AI CAM hardware adapter or software bridge. This attaches that device to your customer account after administrator verification.</div></div></div>
          <form id="cloud-id-form" class="user-form" style="margin-top:18px">
            <label>Device type<select id="cloud-id-device-type"><option value="software_bridge">Windows software bridge</option><option value="hardware_adapter">Hardware cloud adapter</option></select></label>
            <label>Cloud ID<input id="cloud-id-value" maxlength="64" placeholder="Example: ACME-7F42-19B8" autocomplete="off" required></label>
            <div class="dialog-actions full"><button class="action-button" type="submit">Submit Cloud ID</button></div>
          </form>
          <div id="cloud-id-status" style="margin-top:18px"></div>
        </section>
        <section class="panel" style="margin-top:18px">
          <h2>Where to find it</h2>
          <div class="settings-list">
            <div class="setting-link"><div><strong>Software bridge</strong><div class="health-detail">Open the ANY AI CAM software bridge on the customer computer and copy the Cloud ID displayed in its activation section.</div></div></div>
            <div class="setting-link"><div><strong>Hardware adapter</strong><div class="health-detail">Use the Cloud ID printed on the adapter label or shown on its setup screen.</div></div></div>
          </div>
        </section>"""

        scripts = """<script>
        const cloudForm=document.getElementById('cloud-id-form');
        const statusHost=document.getElementById('cloud-id-status');
        function cloudEsc(value){return String(value??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
        function cloudStatus(data){
          const labels={not_submitted:'Not submitted',pending:'Pending verification',verified:'Verified and connected',rejected:'Rejected — update and resubmit'};
          const cls=data.status==='verified'?'active-badge':data.status==='rejected'?'pending-badge':'warning-badge';
          statusHost.innerHTML=`<div class="setting-link"><div><strong>Current Cloud ID: ${cloudEsc(data.cloud_id||'None')}</strong><div class="health-detail">${cloudEsc(data.device_type||'').replaceAll('_',' ')}<br>${cloudEsc(data.admin_note||'')}</div></div><span class="${cls}">${labels[data.status]||data.status}</span></div>`;
          document.getElementById('cloud-id-value').value=data.cloud_id||'';
          document.getElementById('cloud-id-device-type').value=data.device_type||'software_bridge';
        }
        async function loadCloudId(){
          const response=await fetch('/api/cloud-id');
          cloudStatus(await response.json());
        }
        cloudForm.addEventListener('submit',async event=>{
          event.preventDefault();
          const response=await fetch('/api/cloud-id',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({
              cloud_id:document.getElementById('cloud-id-value').value,
              device_type:document.getElementById('cloud-id-device-type').value
            })
          });
          const data=await response.json();
          if(!response.ok)return showToast(data.detail||'Could not submit Cloud ID.');
          showToast(data.message);loadCloudId();
        });
        loadCloudId();
        </script>"""
        return page_shell("Cloud ID", "cloud-id", content, scripts)

    if not is_master_admin(user):
        return permission_denied_page("Cloud ID", "cloud-id", "customer or master administrator access")

    content = """<header class="topbar"><div><p class="eyebrow">Device verification</p><h1>Customer Cloud IDs</h1></div></header>
    <section class="panel"><div class="panel-head"><div><h2>Pending and assigned devices</h2><div class="health-detail">Verify that each Cloud ID belongs to the correct customer before activation.</div></div><button id="refresh-cloud-ids" class="compact-button" type="button">Refresh</button></div>
    <div class="audit-table-wrap"><table class="enterprise-user-table"><thead><tr><th>Customer</th><th>Cloud ID</th><th>Device</th><th>Status</th><th>Submitted</th><th>Actions</th></tr></thead><tbody id="cloud-id-admin-rows"><tr><td colspan="6"><div class="empty">Loading Cloud IDs…</div></td></tr></tbody></table></div></section>"""

    scripts = """<script>
    const cloudRows=document.getElementById('cloud-id-admin-rows');
    function adminCloudEsc(value){return String(value??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}
    async function reviewCloud(profileId,decision){
      const note=prompt(decision==='verified'?'Optional verification note:':'Reason for rejection:')||'';
      const response=await fetch(`/api/cloud-id/admin/${profileId}`,{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({decision,note})
      });
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not update Cloud ID.');
      showToast(data.message);loadCloudIds();
    }
    async function loadCloudIds(){
      const response=await fetch('/api/onboarding/admin');
      const data=await response.json();
      const profiles=(data.profiles||[]).filter(p=>p.cloud_id);
      cloudRows.innerHTML=profiles.length?profiles.map(p=>`<tr>
        <td><strong>${adminCloudEsc(p.organization_name||p.contact_name||p.user_email)}</strong><br><span class="health-detail">${adminCloudEsc(p.user_email)}</span></td>
        <td><strong>${adminCloudEsc(p.cloud_id)}</strong></td>
        <td>${adminCloudEsc((p.cloud_id_device_type||'').replaceAll('_',' '))}</td>
        <td>${adminCloudEsc(p.cloud_id_status||'pending')}</td>
        <td>${adminCloudEsc((p.cloud_id_submitted_at||'').replace('T',' ').slice(0,19))}</td>
        <td><div class="row-actions"><button class="compact-button" onclick="reviewCloud('${p.id}','verified')">Verify</button><button class="compact-button danger" onclick="reviewCloud('${p.id}','rejected')">Reject</button></div></td>
      </tr>`).join(''):'<tr><td colspan="6"><div class="empty">No Cloud IDs submitted yet.</div></td></tr>';
    }
    document.getElementById('refresh-cloud-ids').addEventListener('click',loadCloudIds);
    loadCloudIds();
    </script>"""
    return page_shell("Cloud ID administration", "cloud-id", content, scripts)


@app.get("/customer-onboarding", response_class=HTMLResponse)
def customer_onboarding_page(request: Request) -> str:
    user = current_user(request)
    content = """<header class="topbar"><div><p class="eyebrow">Customer setup</p><h1>Self-service onboarding</h1></div></header>
    <section class="onboarding-summary" id="onboarding-summary"></section>
    <section class="onboarding-layout">
      <aside class="onboarding-steps" id="onboarding-steps"></aside>
      <div>
        <form class="onboarding-card onboarding-form" id="onboarding-form">
          <h2>Organization and deployment</h2>
          <div class="case-events">
            <label>Organization name<input id="onboarding-organization"></label>
            <label>Primary contact<input id="onboarding-contact-name"></label>
            <label>Contact email<input id="onboarding-contact-email" type="email"></label>
            <label>Contact phone<input id="onboarding-contact-phone"></label>
            <label>Site name<input id="onboarding-site-name"></label>
            <label>Site address<input id="onboarding-site-address"></label>
            <label>Timezone<input id="onboarding-timezone" placeholder="America/Chicago"></label>
            <label>Deployment type<select id="onboarding-deployment"><option value="edge">Edge</option><option value="cloud">Cloud</option><option value="hybrid">Hybrid</option></select></label>
            <label>Expected cameras<input id="onboarding-camera-count" type="number" min="1" value="1"></label>
            <label>Camera manufacturer<input id="onboarding-camera-manufacturer"></label>
            <label>Edge device name<input id="onboarding-edge-name"></label>
            <label>Edge platform<select id="onboarding-edge-platform"><option value="windows">Windows</option><option value="linux">Linux</option><option value="macos">macOS</option><option value="other">Other</option></select></label>
            <label>Cloud ID<input id="onboarding-cloud-id" maxlength="64" placeholder="Adapter or software bridge Cloud ID"></label>
            <label>Cloud ID device<select id="onboarding-cloud-id-device"><option value="software_bridge">Windows software bridge</option><option value="hardware_adapter">Hardware cloud adapter</option></select></label>
            <label>Retention days requested<input id="onboarding-retention" type="number" min="1" value="30"></label>
            <label>Team emails<input id="onboarding-team-emails" placeholder="user@example.com, team@example.com"></label>
          </div>
          <label><input id="onboarding-cloud-recording" type="checkbox"> Request cloud recording</label>
          <label><input id="onboarding-terms" type="checkbox"> I accept the onboarding and service terms</label>
          <label>Notes<textarea id="onboarding-notes"></textarea></label>
          <div class="onboarding-actions"><button class="action-button" type="submit">Save onboarding</button><button id="submit-onboarding" type="button">Submit for review</button></div>
        </form>
        <section class="onboarding-card">
          <h2>Activity</h2>
          <div id="onboarding-activity"><div class="empty">Loading activity…</div></div>
        </section>
      </div>
    </section>"""

    scripts = """<script>
    const summaryHost=document.getElementById('onboarding-summary');
    const stepsHost=document.getElementById('onboarding-steps');
    const activityHost=document.getElementById('onboarding-activity');
    let onboardingSteps=[];

    function escOnboarding(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    function markStep(step,completed){
      return fetch('/api/onboarding/step',{
        method:'PUT',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({step,completed})
      }).then(response=>response.json());
    }

    function setValue(id,value){document.getElementById(id).value=value??''}

    async function loadOnboarding(){
      const response=await fetch('/api/onboarding');
      const data=await response.json();
      const profile=data.profile||{},progress=data.progress||{};
      onboardingSteps=data.steps||[];
      summaryHost.innerHTML=`
        <article class="stat"><span class="stat-label">Status</span><span class="stat-value">${escOnboarding(profile.status)}</span></article>
        <article class="stat"><span class="stat-label">Progress</span><span class="stat-value">${progress.percent||0}%</span></article>
        <article class="stat"><span class="stat-label">Deployment</span><span class="stat-value">${escOnboarding(profile.deployment_type)}</span></article>
        <article class="stat"><span class="stat-label">Expected cameras</span><span class="stat-value">${profile.expected_camera_count||0}</span></article>`;

      stepsHost.innerHTML=`<div class="onboarding-card"><h3>Setup checklist</h3><div class="onboarding-progress"><span style="width:${progress.percent||0}%"></span></div></div>`+
        onboardingSteps.map(step=>`<label class="onboarding-step ${(profile.completed_steps||[]).includes(step)?'complete':''}"><span>${escOnboarding(step.replaceAll('_',' '))}</span><input class="onboarding-step-checkbox" data-step="${step}" type="checkbox" ${(profile.completed_steps||[]).includes(step)?'checked':''}></label>`).join('');

      stepsHost.querySelectorAll('.onboarding-step-checkbox').forEach(input=>{
        input.addEventListener('change',async()=>{await markStep(input.dataset.step,input.checked);loadOnboarding()});
      });

      setValue('onboarding-organization',profile.organization_name);
      setValue('onboarding-contact-name',profile.contact_name);
      setValue('onboarding-contact-email',profile.contact_email);
      setValue('onboarding-contact-phone',profile.contact_phone);
      setValue('onboarding-site-name',profile.site_name);
      setValue('onboarding-site-address',profile.site_address);
      setValue('onboarding-timezone',profile.timezone_name);
      setValue('onboarding-deployment',profile.deployment_type||'edge');
      setValue('onboarding-camera-count',profile.expected_camera_count||1);
      setValue('onboarding-camera-manufacturer',profile.camera_manufacturer);
      setValue('onboarding-edge-name',profile.edge_device_name);
      setValue('onboarding-edge-platform',profile.edge_device_platform||'windows');
      setValue('onboarding-cloud-id',profile.cloud_id);
      setValue('onboarding-cloud-id-device',profile.cloud_id_device_type||'software_bridge');
      setValue('onboarding-retention',profile.retention_days_requested||30);
      setValue('onboarding-team-emails',(profile.team_emails||[]).join(', '));
      document.getElementById('onboarding-cloud-recording').checked=Boolean(profile.cloud_recording_requested);
      document.getElementById('onboarding-terms').checked=Boolean(profile.terms_accepted);
      setValue('onboarding-notes',profile.notes);

      activityHost.innerHTML=(data.activity||[]).map(item=>`<div class="license-history-row"><strong>${escOnboarding(item.action.replaceAll('_',' '))}</strong><div class="health-detail">${escOnboarding((item.timestamp||'').replace('T',' ').slice(0,19))} · ${escOnboarding(item.user_name||'')}<br>${escOnboarding(item.details||'')}</div></div>`).join('')||'<div class="empty">No onboarding activity yet.</div>';
    }

    document.getElementById('onboarding-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const payload={
        organization_name:document.getElementById('onboarding-organization').value,
        contact_name:document.getElementById('onboarding-contact-name').value,
        contact_email:document.getElementById('onboarding-contact-email').value,
        contact_phone:document.getElementById('onboarding-contact-phone').value,
        site_name:document.getElementById('onboarding-site-name').value,
        site_address:document.getElementById('onboarding-site-address').value,
        timezone_name:document.getElementById('onboarding-timezone').value,
        deployment_type:document.getElementById('onboarding-deployment').value,
        expected_camera_count:Number(document.getElementById('onboarding-camera-count').value||1),
        camera_manufacturer:document.getElementById('onboarding-camera-manufacturer').value,
        cloud_recording_requested:document.getElementById('onboarding-cloud-recording').checked,
        retention_days_requested:Number(document.getElementById('onboarding-retention').value||30),
        edge_device_name:document.getElementById('onboarding-edge-name').value,
        edge_device_platform:document.getElementById('onboarding-edge-platform').value,
        cloud_id:document.getElementById('onboarding-cloud-id').value,
        cloud_id_device_type:document.getElementById('onboarding-cloud-id-device').value,
        team_emails:document.getElementById('onboarding-team-emails').value.split(',').map(value=>value.trim()).filter(Boolean),
        terms_accepted:document.getElementById('onboarding-terms').checked,
        notes:document.getElementById('onboarding-notes').value
      };
      const response=await fetch('/api/onboarding',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not save onboarding.');
      showToast(data.message);loadOnboarding();
    });

    document.getElementById('submit-onboarding').addEventListener('click',async()=>{
      const response=await fetch('/api/onboarding/submit',{method:'POST'});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not submit onboarding.');
      showToast(data.message);loadOnboarding();
    });

    loadOnboarding();
    </script>"""
    return page_shell(
        "Customer onboarding",
        "customer-onboarding",
        content,
        scripts,
    )


@app.get("/onboarding-admin", response_class=HTMLResponse)
def onboarding_admin_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page(
            "Onboarding administration",
            "onboarding-admin",
            "manage_settings",
        )

    content = """<header class="topbar"><div><p class="eyebrow">Customer activation</p><h1>Onboarding administration</h1></div></header>
    <section class="onboarding-admin-grid" id="onboarding-admin-grid"><div class="empty">Loading onboarding profiles…</div></section>"""
    scripts = """<script>
    const host=document.getElementById('onboarding-admin-grid');
    function escAdminOnboarding(value){return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]))}
    async function loadAdminOnboarding(){
      const response=await fetch('/api/onboarding/admin');
      const data=await response.json();
      host.innerHTML=(data.profiles||[]).map(profile=>`<article class="onboarding-admin-card">
        <div class="case-head"><div><h3>${escAdminOnboarding(profile.organization_name||profile.user_email||'Customer')}</h3><div class="health-detail">${escAdminOnboarding(profile.site_name||'No site')} · ${escAdminOnboarding(profile.deployment_type)} · ${profile.expected_camera_count||0} cameras<br>${profile.progress?.percent||0}% complete</div></div><span class="onboarding-status">${escAdminOnboarding(profile.status)}</span></div>
        <div class="onboarding-actions"><button onclick="updateProfile('${profile.id}','approved')">Approve</button><button onclick="updateProfile('${profile.id}','needs_changes')">Needs changes</button><button onclick="updateProfile('${profile.id}','completed')">Complete</button><button onclick="updateProfile('${profile.id}','cancelled')">Cancel</button></div>
      </article>`).join('')||'<div class="empty">No onboarding profiles.</div>';
    }
    async function updateProfile(id,status){
      const note=prompt('Admin note')||'';
      const assigned=prompt('Assign to')||'';
      const response=await fetch(`/api/onboarding/admin/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status,admin_note:note,assigned_to:assigned})});
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not update onboarding.');
      showToast(data.message);loadAdminOnboarding();
    }
    loadAdminOnboarding();
    </script>"""
    return page_shell(
        "Onboarding administration",
        "onboarding-admin",
        content,
        scripts,
    )



@app.get("/api/cloud-recording")
def cloud_recording_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    refresh_cloud_upload_state()
    index = list(load_cloud_recording_index().values())
    index.sort(key=lambda item: item.get("uploaded_at", ""), reverse=True)
    return {"configuration": cloud_configuration_snapshot(), "state": dict(cloud_upload_state), "queue": list(reversed(cloud_upload_queue))[:500], "recordings": index[:500]}


@app.post("/api/cloud-recording/scan")
def cloud_recording_scan(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    added = scan_recordings_for_cloud_upload()
    refresh_cloud_upload_state()
    return {"status": "complete", "added": added, "state": dict(cloud_upload_state), "message": f"Cloud recording scan queued {added} file(s)."}


@app.post("/api/cloud-recording/queue")
def cloud_recording_queue_file(payload: CloudUploadQueueModel, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    path = Path(payload.path)
    try:
        resolved = path.resolve()
        resolved.relative_to(RECORDINGS_FOLDER.resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Only files inside the recordings folder can be queued.")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Recording file was not found.")
    job = enqueue_cloud_upload(resolved, payload.camera_number)
    return {"status": "complete", "job": job, "message": "Recording queued for cloud upload."}


@app.post("/api/cloud-recording/retry")
def cloud_recording_retry(payload: CloudUploadRetryModel, request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        raise HTTPException(status_code=403, detail="Settings permission is required.")
    job = next((item for item in cloud_upload_queue if item.get("id") == payload.job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Upload job not found.")
    if job.get("status") == "uploaded":
        raise HTTPException(status_code=400, detail="Uploaded jobs do not need retrying.")
    job.update({"status": "queued", "attempts": 0, "last_error": "", "next_attempt_at": datetime.now().isoformat()})
    save_cloud_upload_queue(cloud_upload_queue)
    refresh_cloud_upload_state()
    return {"status": "complete", "job": job, "message": "Upload job queued for retry."}


@app.get("/cloud-recording", response_class=HTMLResponse)
def cloud_recording_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page("Cloud recording", "cloud-recording", "manage_settings")
    content = """<header class="topbar"><div><p class="eyebrow">Amazon S3</p><h1>Cloud recording</h1></div></header>
    <section class="cloud-recording-summary" id="cloud-recording-summary"></section>
    <section class="cloud-recording-layout"><div class="cloud-recording-card"><h2>Configuration</h2><div class="cloud-config-list" id="cloud-recording-config"></div><div class="cloud-recording-actions"><button id="cloud-recording-scan" type="button">Scan recordings now</button></div></div>
    <div><section class="cloud-recording-card"><h2>Upload queue</h2><div class="cloud-recording-grid" id="cloud-recording-queue"><div class="empty">Loading queue…</div></div></section>
    <section class="cloud-recording-card"><h2>Uploaded recordings</h2><div class="cloud-recording-grid" id="cloud-recording-index"><div class="empty">Loading cloud index…</div></div></section></div></section>"""
    scripts = """<script>
    const summaryHost=document.getElementById('cloud-recording-summary'),configHost=document.getElementById('cloud-recording-config'),queueHost=document.getElementById('cloud-recording-queue'),indexHost=document.getElementById('cloud-recording-index');
    function escCloud(value){return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]))}
    function fileSize(value){const n=Number(value)||0;if(n<1024)return n+' B';if(n<1024**2)return(n/1024).toFixed(1)+' KB';if(n<1024**3)return(n/1024**2).toFixed(1)+' MB';return(n/1024**3).toFixed(2)+' GB'}
    async function retryUpload(id){const r=await fetch('/api/cloud-recording/retry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:id})}),d=await r.json();if(!r.ok)return showToast(d.detail||'Could not retry upload.');showToast(d.message);loadCloudRecording()}
    async function loadCloudRecording(){const r=await fetch('/api/cloud-recording'),d=await r.json(),s=d.state||{},c=d.configuration||{};
      summaryHost.innerHTML=`<article class="stat"><span class="stat-label">Worker</span><span class="stat-value">${escCloud(s.worker_status)}</span></article><article class="stat"><span class="stat-label">Queued</span><span class="stat-value">${s.queued||0}</span></article><article class="stat"><span class="stat-label">Uploading</span><span class="stat-value">${s.uploading||0}</span></article><article class="stat"><span class="stat-label">Uploaded</span><span class="stat-value">${s.uploaded||0}</span></article><article class="stat"><span class="stat-label">Failed</span><span class="stat-value">${s.failed||0}</span></article>`;
      const rows=[['Upload enabled',c.cloud_upload_enabled],['S3 bucket',c.s3_configured],['AWS region',c.aws_region_configured],['AWS SDK',c.s3_sdk_available],['Cloud recording ready',c.cloud_recording_ready],['Delete local',c.cloud_delete_local]];
      configHost.innerHTML=rows.map(([n,v])=>`<div class="cloud-config-row"><span>${escCloud(n)}</span><strong>${v?'Yes':'No'}</strong></div>`).join('')+`<div class="cloud-config-row"><span>Storage class</span><strong>${escCloud(c.cloud_storage_class||'')}</strong></div>`;
      queueHost.innerHTML=(d.queue||[]).map(j=>`<div class="cloud-recording-row"><div class="cloud-recording-head"><div><strong>${escCloud((j.path||'').split(/[\\\\/]/).pop())}</strong><div class="cloud-recording-meta">Camera ${escCloud(j.camera||'—')} · ${fileSize(j.size_bytes)} · Attempt ${j.attempts||0}/${j.max_retries||0}<br>${escCloud(j.s3_key||'')}</div></div><span class="cloud-recording-badge">${escCloud(j.status)}</span></div>${j.last_error?`<div class="cloud-recording-meta">${escCloud(j.last_error)}</div>`:''}${j.status==='failed'?`<div class="cloud-recording-actions"><button onclick="retryUpload('${j.id}')">Retry</button></div>`:''}</div>`).join('')||'<div class="empty">No upload jobs.</div>';
      indexHost.innerHTML=(d.recordings||[]).map(i=>`<div class="cloud-recording-row"><div class="cloud-recording-head"><div><strong>${escCloud(i.file_name)}</strong><div class="cloud-recording-meta">Camera ${escCloud(i.camera||'—')} · ${fileSize(i.size_bytes)}<br>${escCloud(i.s3_uri)}<br>SHA-256: ${escCloud(i.sha256)}</div></div><span class="cloud-recording-badge">uploaded</span></div></div>`).join('')||'<div class="empty">No cloud recordings uploaded yet.</div>';
    }
    document.getElementById('cloud-recording-scan').addEventListener('click',async()=>{const r=await fetch('/api/cloud-recording/scan',{method:'POST'}),d=await r.json();if(!r.ok)return showToast(d.detail||'Cloud scan failed.');showToast(d.message);loadCloudRecording()});
    loadCloudRecording();setInterval(loadCloudRecording,15000);
    </script>"""
    return page_shell("Cloud recording", "cloud-recording", content, scripts)


@app.get("/investigation-cases", response_class=HTMLResponse)
def investigation_cases_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "view_analytics"):
        return permission_denied_page("Cases", "cases", "view_analytics")

    content = """<header class="topbar"><div><p class="eyebrow">Investigation workflow</p><h1>Cases</h1></div><a class="action-button" href="/investigate">Open Investigate</a></header>
    <section class="case-layout">
      <form class="case-form" id="case-create-form">
        <h2>Create case</h2>
        <label>Title<input id="case-title" required placeholder="Example: Loading dock theft"></label>
        <label>Description<textarea id="case-description" placeholder="Case summary"></textarea></label>
        <label>Priority<select id="case-priority"><option value="low">Low</option><option value="normal" selected>Normal</option><option value="high">High</option><option value="critical">Critical</option></select></label>
        <label>Status<select id="case-status"><option value="open">Open</option><option value="in_review">In review</option><option value="closed">Closed</option><option value="archived">Archived</option></select></label>
        <label>Assigned investigator<input id="case-assigned" placeholder="Name or email"></label>
        <label>Tags<input id="case-tags" placeholder="theft, vehicle, overnight"></label>
        <label>Notes<textarea id="case-notes" placeholder="Investigator notes"></textarea></label>
        <label>Evidence event IDs<textarea id="case-event-ids" placeholder="Paste event IDs, one per line"></textarea></label>
        <button class="action-button" type="submit">Create case</button>
      </form>
      <div>
        <div class="case-grid" id="case-grid"><div class="empty">Loading cases…</div></div>
        <section class="panel case-detail" id="case-detail" hidden></section>
      </div>
    </section>"""

    scripts = """<script>
    const caseGrid=document.getElementById('case-grid');
    const caseDetail=document.getElementById('case-detail');
    let cases=[];

    function escCase(value){
      return String(value??'').replace(/[&<>\"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char]));
    }

    function renderCases(){
      caseGrid.innerHTML=cases.map(item=>`
        <article class="case-card" data-case-id="${item.id}">
          <div class="case-head">
            <div><h3>${escCase(item.title)}</h3><div class="case-meta">${escCase(item.description||'No description')}</div></div>
            <div class="case-badges"><span class="case-badge">${escCase(item.priority)}</span><span class="case-badge">${escCase(item.status.replaceAll('_',' '))}</span></div>
          </div>
          <div class="case-meta">Assigned: ${escCase(item.assigned_to||'Unassigned')} · Evidence: ${(item.event_ids||[]).length} · Updated: ${escCase((item.updated_at||'').replace('T',' ').slice(0,19))}</div>
          <div class="case-actions"><button class="open-case" type="button">Open case</button><a href="/api/investigation-cases/${item.id}/export">Manifest</a><a href="/api/investigation-cases/${item.id}/custody-package">Custody ZIP</a><a href="/investigation-cases/${item.id}/custody">Custody report</a></div>
        </article>`).join('')||'<div class="empty">No investigation cases yet.</div>';

      caseGrid.querySelectorAll('.open-case').forEach(button=>{
        button.addEventListener('click',()=>openCase(button.closest('.case-card').dataset.caseId));
      });
    }

    async function loadCases(){
      const response=await fetch('/api/investigation-cases');
      const data=await response.json();
      cases=data.cases||[];
      renderCases();
    }

    function renderCaseDetail(item){
      caseDetail.hidden=false;
      caseDetail.innerHTML=`
        <div class="panel-head"><div><p class="eyebrow">Case detail</p><h2>${escCase(item.title)}</h2></div><a class="ghost-button" href="/api/investigation-cases/${item.id}/export">Export</a></div>
        <div class="case-events">
          <label>Title<input id="edit-case-title" value="${escCase(item.title)}"></label>
          <label>Assigned<input id="edit-case-assigned" value="${escCase(item.assigned_to||'')}"></label>
          <label>Priority<select id="edit-case-priority">${['low','normal','high','critical'].map(value=>`<option value="${value}" ${item.priority===value?'selected':''}>${value}</option>`).join('')}</select></label>
          <label>Status<select id="edit-case-status">${['open','in_review','closed','archived'].map(value=>`<option value="${value}" ${item.status===value?'selected':''}>${value.replaceAll('_',' ')}</option>`).join('')}</select></label>
        </div>
        <label>Description<textarea id="edit-case-description">${escCase(item.description||'')}</textarea></label>
        <label>Tags<input id="edit-case-tags" value="${escCase((item.tags||[]).join(', '))}"></label>
        <label>Notes<textarea id="edit-case-notes">${escCase(item.notes||'')}</textarea></label>
        <label>Evidence event IDs<textarea id="edit-case-events">${escCase((item.event_ids||[]).join('\\n'))}</textarea></label>
        <div class="case-actions"><button class="action-button" id="save-case" type="button">Save changes</button></div>
        <div><h3>Activity history</h3><div class="case-history">${(item.history||[]).slice().reverse().map(history=>`<div class="case-history-row"><strong>${escCase(history.action.replaceAll('_',' '))}</strong><div class="case-meta">${escCase(history.user_name)} · ${escCase((history.timestamp||'').replace('T',' ').slice(0,19))}<br>${escCase(history.details||'')}</div></div>`).join('')||'<div class="empty">No history.</div>'}</div></div>`;

      document.getElementById('save-case').addEventListener('click',()=>saveCase(item.id));
      caseDetail.scrollIntoView({behavior:'smooth',block:'start'});
    }

    async function openCase(caseId){
      const response=await fetch(`/api/investigation-cases/${caseId}`);
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not open case.');
      renderCaseDetail(data.case);
    }

    async function saveCase(caseId){
      const payload={
        title:document.getElementById('edit-case-title').value,
        description:document.getElementById('edit-case-description').value,
        priority:document.getElementById('edit-case-priority').value,
        status:document.getElementById('edit-case-status').value,
        assigned_to:document.getElementById('edit-case-assigned').value,
        tags:document.getElementById('edit-case-tags').value.split(',').map(value=>value.trim()).filter(Boolean),
        notes:document.getElementById('edit-case-notes').value,
        event_ids:document.getElementById('edit-case-events').value.split(/\\s+/).filter(Boolean)
      };
      const response=await fetch(`/api/investigation-cases/${caseId}`,{
        method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
      });
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not save case.');
      showToast(data.message);await loadCases();renderCaseDetail(data.case);
    }

    document.getElementById('case-create-form').addEventListener('submit',async event=>{
      event.preventDefault();
      const payload={
        title:document.getElementById('case-title').value,
        description:document.getElementById('case-description').value,
        priority:document.getElementById('case-priority').value,
        status:document.getElementById('case-status').value,
        assigned_to:document.getElementById('case-assigned').value,
        tags:document.getElementById('case-tags').value.split(',').map(value=>value.trim()).filter(Boolean),
        notes:document.getElementById('case-notes').value,
        event_ids:document.getElementById('case-event-ids').value.split(/\\s+/).filter(Boolean)
      };
      const response=await fetch('/api/investigation-cases',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
      });
      const data=await response.json();
      if(!response.ok)return showToast(data.detail||'Could not create case.');
      event.currentTarget.reset();showToast(data.message);await loadCases();renderCaseDetail(data.case);
    });

    loadCases();
    </script>"""
    return page_shell("Cases", "cases", content, scripts)


@app.get("/analytics/{analytics_slug}", response_class=HTMLResponse)
def analytics_detail(analytics_slug: str) -> str:
    if analytics_slug in {"line-crossing", "intrusion"}:
        return analytics_rule_builder(analytics_slug.replace("-", "_"))
    if analytics_slug == "license-plate-recognition":
        return lpr_analytics_page()
    if analytics_slug in {"people-counting", "occupancy"}:
        return people_analytics_page("People counting" if analytics_slug == "people-counting" else "Occupancy")
    if analytics_slug == "vehicle-search":
        return smart_search_page()
    match = next(
        ((name, description) for name, description in ANALYTICS_FEATURES if slugify(name) == analytics_slug),
        None,
    )
    if match is None:
        return page_shell("Analytics module", "analytics", '<div class="empty">Analytics module not found.</div>')
    name, description = match
    content = f"""<header class="topbar"><div><p class="eyebrow">AI analytics</p><h1>{escape(name)}</h1></div><a class="download" href="/analytics">Back to analytics</a></header><section class="panel"><div class="panel-head"><h2>Module setup</h2><span class="pill wait">Coming soon</span></div><p class="health-detail">{escape(description)}</p><div class="health-list"><div class="health-row"><div><div class="health-name">Camera assignment</div><div class="health-detail">Choose cameras that will use this analytic.</div></div><span>Not configured</span></div><div class="health-row"><div><div class="health-name">Detection zones</div><div class="health-detail">Draw regions or lines over a camera image.</div></div><span>Not configured</span></div><div class="health-row"><div><div class="health-name">Event notifications</div><div class="health-detail">Choose when and how alerts are delivered.</div></div><span>Not configured</span></div></div></section>"""
    return page_shell(name, "analytics", content)


@app.get("/alerts", response_class=HTMLResponse)
def alerts() -> str:
    cameras = "".join(f'<label class="picker-camera"><input type="checkbox" checked> Camera {n}</label>' for n in range(1, CAMERA_COUNT + 1))
    events = sorted(load_motion_events(), key=lambda event: event.get("start_time") or event.get("timestamp", ""), reverse=True)[:20]
    alert_cards = []
    for event in events:
        event_time = event.get("start_time") or event.get("timestamp", "")
        thumbnail = f'<img src="{event["thumbnail"]}" alt="Motion" style="width:120px;aspect-ratio:16/9;object-fit:cover;border-radius:7px">' if event.get("thumbnail") else '<div class="feature-icon">⌁</div>'
        alert_cards.append(f'<article class="feature-card">{thumbnail}<h2>Motion · Camera {event.get("camera", "—")}</h2><p>{escape(event_time.replace("T", " ")[:19])} · {float(event.get("confidence", event.get("score", 0))):.1f}% confidence</p><a class="download" href="/playback">Review playback</a></article>')
    health_alerts = [alert for alert in in_app_alerts(30)["alerts"] if alert.get("event_type") != "motion"]
    for alert in health_alerts:
        alert_cards.insert(0, f'<article class="feature-card"><div class="feature-icon">!</div><h2>{escape(alert.get("event_type", "health").replace("_", " ").title())}</h2><p>{escape(alert.get("message", "System health alert"))}</p><span class="coming">In-app alert</span></article>')
    alert_body = "".join(alert_cards) or '<div class="empty-stage">No alerts yet.<br>Motion and health alerts will appear here.</div>'
    content = f"""<header class="topbar"><div><p class="eyebrow">Event center</p><h1>Smart alerts</h1></div><div><button class="ghost-button" onclick="comingSoon('Setup guide')">Setup guide</button> <button class="action-button" onclick="comingSoon('New alert rule')">＋ New alert</button></div></header><div class="playback-workspace"><aside class="camera-picker"><div class="picker-head">▣ Cameras ({CAMERA_COUNT})</div><input class="picker-search" type="search" placeholder="Search"><div>{cameras}</div></aside><section class="work-area"><div class="panel-head"><h2>Recent motion alerts</h2><span class="pill">{len(events)} event(s)</span></div><div class="feature-grid">{alert_body}</div></section></div>"""
    return page_shell("Alerts", "alerts", content)


@app.get("/events", response_class=HTMLResponse)
def events() -> str:
    cameras = "".join(f'<label class="picker-camera"><input type="checkbox" checked> Camera {n}</label>' for n in range(1, CAMERA_COUNT + 1))
    recent_events = sorted(
        load_motion_events(), key=lambda event: event.get("start_time") or event.get("timestamp", ""), reverse=True
    )[:100]
    event_rows = []
    for event in recent_events:
        try:
            occurred_at = datetime.fromisoformat(event.get("start_time") or event["timestamp"])
            timestamp_label = occurred_at.strftime("%b %d, %Y · %I:%M:%S %p")
        except (KeyError, TypeError, ValueError):
            timestamp_label = "Unknown time"
        thumbnail = f'<img src="{event["thumbnail"]}" alt="Motion thumbnail" style="width:96px;aspect-ratio:16/9;object-fit:cover">' if event.get("thumbnail") else "—"
        confidence = float(event.get("confidence", event.get("score", 0)))
        event_rows.append(f'<tr data-event-camera="{event.get("camera", "")}"><td>{escape(timestamp_label)}</td><td>Camera {event.get("camera", "—")}</td><td>{thumbnail}</td><td><span class="pill">Motion</span></td><td>{confidence:.1f}%</td><td><a class="download" href="/playback">Review footage</a></td></tr>')
    event_body = "".join(event_rows) or '<tr><td colspan="6"><div class="empty-stage">Waiting for motion events.<br>Events appear after cameras are online.</div></td></tr>'
    detector_status = "Active" if MOTION_DETECTION_ENABLED else "Disabled"
    content = f"""<header class="topbar"><div><p class="eyebrow">Recorded activity</p><h1>Events</h1></div><div><span class="pill">Motion detection: {detector_status}</span></div></header><div class="playback-workspace"><aside class="camera-picker"><div class="picker-head">▣ Cameras ({CAMERA_COUNT})</div><input class="picker-search" type="search" placeholder="Search"><div>{cameras}</div></aside><section class="work-area"><div class="panel-head"><h2>Recent motion</h2><span class="health-detail">{len(recent_events)} event(s)</span></div><table class="data-table"><thead><tr><th>Start time</th><th>Camera</th><th>Thumbnail</th><th>Type</th><th>Confidence</th><th>Action</th></tr></thead><tbody>{event_body}</tbody></table></section></div>"""
    return page_shell("Events", "events", content)



@app.get("/accept-invite", response_class=HTMLResponse)
def accept_invite_page(token: str = "") -> HTMLResponse:
    try:
        payload = session_serializer.loads(
            token,
            salt="anyaicam-user-invite",
            max_age=7 * 24 * 3600,
        )
    except (BadSignature, SignatureExpired):
        return HTMLResponse(
            "<h1>Invitation expired or invalid</h1><p>Ask your administrator to send a new invitation.</p>",
            status_code=400,
        )
    email = escape(str(payload.get("email", "")))
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Accept invitation · AnyAiCam</title><style>{STYLES}</style></head>
        <body><main class="auth-page"><section class="auth-card"><img class="auth-logo" src="/static/brand-icon.png"><h1>Accept invitation</h1><p class="auth-subtitle">{email}</p>
        <form class="auth-form" id="accept-form">
        <input type="hidden" id="invite-token" value="{escape(token)}">
        <label>Display name<input id="invite-name" required minlength="2"></label>
        <label>Create password<input id="invite-password" type="password" required minlength="10"></label>
        <button class="action-button" type="submit">Create account</button>
        </form><div class="auth-footer" id="invite-message"></div></section></main>
        <script>
        document.getElementById('accept-form').addEventListener('submit', async event => {{
            event.preventDefault();
            const response = await fetch('/api/user-invites/accept', {{
                method:'POST', headers:{{'Content-Type':'application/json'}},
                body:JSON.stringify({{
                    token:document.getElementById('invite-token').value,
                    display_name:document.getElementById('invite-name').value,
                    password:document.getElementById('invite-password').value
                }})
            }});
            const data = await response.json();
            document.getElementById('invite-message').textContent = data.message;
            if(data.status === 'complete') setTimeout(() => location.href='/login', 1200);
        }});
        </script></body></html>"""
    )


@app.post("/api/user-invites/accept")
def accept_user_invite(payload: UserInviteAcceptModel) -> dict:
    try:
        token_payload = session_serializer.loads(
            payload.token,
            salt="anyaicam-user-invite",
            max_age=7 * 24 * 3600,
        )
    except (BadSignature, SignatureExpired):
        return {"status": "error", "message": "Invitation expired or invalid."}

    invite_id = token_payload.get("invite_id")
    invites = load_user_invites()
    invite = next(
        (item for item in invites if item.get("id") == invite_id and item.get("status") == "pending"),
        None,
    )
    if not invite:
        return {"status": "error", "message": "Invitation is no longer available."}

    users = load_users()
    if any(item.get("email", "").lower() == invite["email"].lower() for item in users):
        return {"status": "error", "message": "An account already exists for this email."}

    users.append(
        UserModel(
            display_name=payload.display_name,
            email=invite["email"],
            role=invite["role"],
            enabled=True,
            super_admin=invite.get("super_admin", False),
            site_ids=["home"],
            camera_ids=[] if invite.get("all_cameras") else invite.get("camera_ids", []),
            password_hash=hash_password(payload.password),
            invitation_status="active",
        ).model_dump(mode="json")
    )
    save_users(users)
    invite["status"] = "accepted"
    invite["accepted_at"] = datetime.now().isoformat()
    save_user_invites(invites)
    return {"status": "complete", "message": "Account created. You can now sign in."}



@app.get("/api/user-invites")
def user_invites_api(request: Request) -> dict:
    actor = current_user(request)
    if not has_permission(actor, "manage_users"):
        return {"status": "error", "message": "Administrator permission required."}
    invites = load_user_invites()
    now = datetime.now()
    changed = False
    for invite in invites:
        if invite.get("status") == "pending":
            try:
                if datetime.fromisoformat(invite["expires_at"]) < now:
                    invite["status"] = "expired"
                    changed = True
            except (KeyError, TypeError, ValueError):
                pass
    if changed:
        save_user_invites(invites)
    return {"status": "complete", "invites": invites}


@app.post("/api/user-invites")
def create_user_invite(request: Request, payload: UserInviteCreateModel) -> dict:
    actor = current_user(request)
    if not has_permission(actor, "manage_users"):
        return {"status": "error", "message": "Administrator permission required."}
    if payload.role not in VALID_ROLES:
        return {"status": "error", "message": "Unsupported permission level."}
    email = payload.email.strip().lower()
    users = load_users()
    invites = load_user_invites()
    if any(item.get("email", "").lower() == email for item in users):
        return {"status": "error", "message": "That email already has an account."}
    if any(item.get("email", "").lower() == email and item.get("status") == "pending" for item in invites):
        return {"status": "error", "message": "A pending invitation already exists."}

    invite_id = uuid.uuid4().hex[:12]
    token = session_serializer.dumps(
        {"invite_id": invite_id, "email": email},
        salt="anyaicam-user-invite",
    )
    invite = {
        "id": invite_id,
        "email": email,
        "display_name": payload.display_name.strip(),
        "role": "admin" if payload.super_admin else payload.role,
        "super_admin": payload.super_admin,
        "all_cameras": payload.all_cameras,
        "camera_ids": [] if payload.all_cameras else sorted(set(payload.camera_ids)),
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(days=7)).isoformat(),
        "created_by": actor.get("id"),
        "token": token,
    }
    invites.append(invite)
    save_user_invites(invites)
    email_ok, email_detail = send_user_invitation_email(invite, request)
    record_audit(
        request,
        "create",
        "user_invite",
        f"Invited {email}: {email_detail}",
        "success" if email_ok else "failed",
    )
    return {
        "status": "complete",
        "message": email_detail if email_ok else f"Invitation saved, but email failed: {email_detail}",
        "invite": invite,
    }


@app.post("/api/user-invites/{invite_id}/resend")
def resend_user_invite(request: Request, invite_id: str) -> dict:
    actor = current_user(request)
    if not has_permission(actor, "manage_users"):
        return {"status": "error", "message": "Administrator permission required."}
    invites = load_user_invites()
    invite = next((item for item in invites if item.get("id") == invite_id), None)
    if not invite:
        return {"status": "error", "message": "Invitation not found."}
    invite["status"] = "pending"
    invite["expires_at"] = (datetime.now() + timedelta(days=7)).isoformat()
    invite["token"] = session_serializer.dumps(
        {"invite_id": invite["id"], "email": invite["email"]},
        salt="anyaicam-user-invite",
    )
    save_user_invites(invites)
    ok, detail = send_user_invitation_email(invite, request)
    return {"status": "complete" if ok else "error", "message": detail}


@app.delete("/api/user-invites/{invite_id}")
def cancel_user_invite(request: Request, invite_id: str) -> dict:
    actor = current_user(request)
    if not has_permission(actor, "manage_users"):
        return {"status": "error", "message": "Administrator permission required."}
    invites = load_user_invites()
    invite = next((item for item in invites if item.get("id") == invite_id), None)
    if not invite:
        return {"status": "error", "message": "Invitation not found."}
    invite["status"] = "cancelled"
    invite["cancelled_at"] = datetime.now().isoformat()
    save_user_invites(invites)
    return {"status": "complete", "message": "Invitation cancelled."}


@app.post("/api/business-users/{user_id}/approve")
def approve_business_user(request: Request, user_id: str) -> dict:
    actor = current_user(request)
    if not is_master_admin(actor):
        record_audit(
            request,
            "approve",
            f"user:{user_id}",
            "Master administrator permission required.",
            "denied",
        )
        return {
            "status": "error",
            "message": "Master administrator permission required.",
        }

    users = load_users()
    target = next((item for item in users if item.get("id") == user_id), None)
    if not target:
        return {"status": "error", "message": "Account request not found."}
    if target.get("id") == "local-admin":
        return {"status": "error", "message": "The master account is already active."}

    target["enabled"] = True
    target["invitation_status"] = "active"
    target["approved_at"] = datetime.now().isoformat()
    target["approved_by"] = actor.get("id", "local-admin")
    target["failed_login_attempts"] = 0
    target["locked_until"] = None
    save_users(users)

    record_audit(
        request,
        "approve",
        f"user:{user_id}",
        f"Approved {target.get('email', '')} as {target.get('role', '')}.",
    )
    return {
        "status": "complete",
        "message": "Account approved. The user can now sign in.",
    }


@app.get("/api/users")
def users_api(request: Request) -> dict:
    user = current_user(request)
    if not has_permission(user, "manage_users"):
        record_audit(request, "view", "users", "Permission denied", "denied")
        return {"status": "error", "message": "Administrator permission required."}
    return {
        "status": "complete",
        "users": [
            {key: value for key, value in item.items() if key != "password_hash"}
            for item in load_users()
        ],
        "current_user": user,
        "roles": sorted(VALID_ROLES),
        "permissions": {role: sorted(values) for role, values in ROLE_PERMISSIONS.items()},
    }


@app.post("/api/users")
def create_user(request: Request, new_user: UserCreateModel) -> dict:
    actor = current_user(request)
    if not has_permission(actor, "manage_users"):
        record_audit(request, "create", "user", "Permission denied", "denied")
        return {"status": "error", "message": "Administrator permission required."}
    if new_user.role not in VALID_ROLES:
        return {"status": "error", "message": "Unsupported role."}
    users = load_users()
    if any(
        item.get("email", "").lower() == new_user.email.lower()
        and new_user.email
        for item in users
    ):
        return {"status": "error", "message": "That email is already in use."}
    payload = UserModel(
        display_name=new_user.display_name,
        email=new_user.email.strip().lower(),
        role=new_user.role,
        enabled=new_user.enabled,
        super_admin=new_user.super_admin,
        site_ids=new_user.site_ids,
        camera_ids=new_user.camera_ids,
        password_hash=hash_password(new_user.password),
    ).model_dump(mode="json")
    users.append(payload)
    save_users(users)
    record_audit(
        request,
        "create",
        f"user:{payload['id']}",
        f"Created {new_user.display_name} with role {new_user.role}.",
    )
    return {"status": "complete", "message": "User created.", "user": {key: value for key, value in payload.items() if key != "password_hash"}}


@app.put("/api/users/{user_id}")
def update_user(request: Request, user_id: str, update: UserUpdateModel) -> dict:
    actor = current_user(request)
    if not has_permission(actor, "manage_users"):
        record_audit(request, "update", f"user:{user_id}", "Permission denied", "denied")
        return {"status": "error", "message": "Administrator permission required."}
    users = load_users()
    target = next((item for item in users if item.get("id") == user_id), None)
    if not target:
        return {"status": "error", "message": "User not found."}
    changes = update.model_dump(exclude_none=True)
    if changes.get("role") and changes["role"] not in VALID_ROLES:
        return {"status": "error", "message": "Unsupported role."}
    if user_id == "local-admin" and changes.get("enabled") is False:
        return {"status": "error", "message": "The bootstrap administrator cannot be disabled."}
    target.update(changes)
    save_users(users)
    record_audit(
        request,
        "update",
        f"user:{user_id}",
        "Updated fields: " + ", ".join(sorted(changes)),
    )
    return {"status": "complete", "message": "User updated.", "user": {key: value for key, value in target.items() if key != "password_hash"}}


@app.put("/api/users/{user_id}/password")
def change_user_password(
    request: Request,
    user_id: str,
    change: PasswordChangeModel,
) -> dict:
    actor = current_user(request)
    if not has_permission(actor, "manage_users"):
        record_audit(request, "update_password", f"user:{user_id}", "Permission denied", "denied")
        return {"status": "error", "message": "Administrator permission required."}
    users = load_users()
    target = next((item for item in users if item.get("id") == user_id), None)
    if not target:
        return {"status": "error", "message": "User not found."}
    target["password_hash"] = hash_password(change.password)
    target["failed_login_attempts"] = 0
    target["locked_until"] = None
    save_users(users)
    record_audit(request, "update_password", f"user:{user_id}", "Password reset by administrator.")
    return {"status": "complete", "message": "Password updated."}


@app.delete("/api/users/{user_id}")
def delete_user(request: Request, user_id: str) -> dict:
    actor = current_user(request)
    users = load_users()
    target = next((item for item in users if item.get("id") == user_id), None)
    target_role = str((target or {}).get("role") or "").lower()
    if target_role in (
        PUBLIC_BUSINESS_REGISTRATION_ROLES
        | {"partner_admin", "customer_viewer", "support_admin"}
    ) and not is_master_admin(actor):
        record_audit(
            request,
            "delete",
            f"user:{user_id}",
            "Master administrator permission required.",
            "denied",
        )
        return {
            "status": "error",
            "message": "Master administrator permission required.",
        }
    if not has_permission(actor, "manage_users"):
        record_audit(request, "delete", f"user:{user_id}", "Permission denied", "denied")
        return {"status": "error", "message": "Administrator permission required."}
    if user_id == "local-admin":
        return {"status": "error", "message": "The bootstrap administrator cannot be deleted."}
    remaining = [item for item in users if item.get("id") != user_id]
    if len(remaining) == len(users):
        return {"status": "error", "message": "User not found."}
    save_users(remaining)
    record_audit(request, "delete", f"user:{user_id}", "User removed.")
    return {"status": "complete", "message": "User deleted."}



@app.get("/api/audit-logs")
def audit_logs_api(
    request: Request,
    action: str | None = None,
    role: str | None = None,
    outcome: str | None = None,
    query: str | None = None,
    limit: int = 250,
) -> dict:
    user = current_user(request)
    if not has_permission(user, "view_audit"):
        record_audit(request, "view", "audit_logs", "Permission denied", "denied")
        return {"status": "error", "message": "Audit permission required."}
    entries = load_audit_entries(limit=1000)
    if action:
        entries = [item for item in entries if item.get("action") == action]
    if role:
        entries = [item for item in entries if item.get("role") == role]
    if outcome:
        entries = [item for item in entries if item.get("outcome") == outcome]
    if query:
        needle = query.lower()
        entries = [
            item for item in entries
            if needle in json.dumps(item).lower()
        ]
    return {"status": "complete", "entries": entries[:max(1, min(limit, 1000))]}



@app.get("/business-users", response_class=HTMLResponse)
def business_users_page(request: Request) -> str:
    actor = current_user(request)
    if not is_master_admin(actor):
        return permission_denied_page(
            "Business users",
            "business-users",
            "master administrator access",
        )

    content = """
    <header class="topbar">
      <div>
        <p class="eyebrow">Business identity</p>
        <h1>Master-controlled business accounts</h1>
      </div>
      <span class="pill">Separate from camera sharing</span>
    </header>

    <section class="panel" style="margin-bottom:18px">
      <div class="panel-head">
        <div>
          <h2>Create a business portal login</h2>
          <div class="health-detail">
            Your protected master account can create, approve, and delete administrator, customer, salesperson, and installer accounts.
            They are separate from family, installer, and viewer camera-sharing invitations.
          </div>
        </div>
      </div>

      <form id="business-user-form" class="user-form" style="margin-top:14px">
        <label>Display name<input id="business-name" required minlength="2"></label>
        <label>Email<input id="business-email" type="email" required></label>
        <label>Password<input id="business-password" type="password" minlength="10" required></label>
        <label>Business role
          <select id="business-role">
            <option value="administrator">Administrator</option>
            <option value="partner_sales">Salesperson</option>
            <option value="installer">Installer</option>
            <option value="customer_owner">Customer owner</option>
            <option value="customer_viewer">Customer viewer</option>
          </select>
        </label>
        <div class="dialog-actions full">
          <button class="action-button" type="submit">Create portal account</button>
        </div>
      </form>
      <div id="business-user-message" class="health-detail" style="margin-top:12px"></div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>Business portal accounts</h2>
          <div class="health-detail">Administrator, salesperson, and customer identities only.</div>
        </div>
        <button class="compact-button" id="refresh-business-users" type="button">Refresh</button>
      </div>
      <div class="audit-table-wrap">
        <table class="enterprise-user-table">
          <thead>
            <tr><th>Name</th><th>Email</th><th>Business role</th><th>Portal</th><th>Status</th><th></th></tr>
          </thead>
          <tbody id="business-user-rows">
            <tr><td colspan="6"><div class="empty">Loading accounts...</div></td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel" style="margin-top:18px">
      <h2>Role boundaries</h2>
      <div class="settings-list">
        <div class="setting-link"><div><strong>Administrator</strong><div class="health-detail">Full business and VMS administration.</div></div><span>/admin-portal</span></div>
        <div class="setting-link"><div><strong>Salesperson</strong><div class="health-detail">Partner sales, quotes, commissions, and training.</div></div><span>/partner-sales</span></div>
        <div class="setting-link"><div><strong>Installer</strong><div class="health-detail">Assigned installations and approved installation guides.</div></div><span>/partner-installations</span></div>
        <div class="setting-link"><div><strong>Customer</strong><div class="health-detail">Their cameras, events, alerts, playback, subscription, and account.</div></div><span>/customer-portal</span></div>
      </div>
    </section>
    """

    scripts = """
    <script>
    const businessRoles = new Set([
      'administrator','support_admin','partner_admin','partner_sales',
      'customer_owner','customer_viewer','installer'
    ]);

    function businessSafe(value) {
      const span = document.createElement('span');
      span.textContent = value ?? '';
      return span.innerHTML;
    }

    function businessPortal(role) {
      if (['administrator','support_admin','admin'].includes(role)) return '/admin-portal';
      if (role === 'installer') return '/partner-installations';
      if (['partner_admin','partner_sales'].includes(role)) return '/partner-sales';
      if (['customer_owner','customer_viewer'].includes(role)) return '/customer-portal';
      return '/';
    }

    function businessRoleLabel(role) {
      const labels = {
        administrator:'Administrator',
        support_admin:'Support administrator',
        partner_admin:'Sales administrator',
        partner_sales:'Salesperson',
        installer:'Installer',
        customer_owner:'Customer owner',
        customer_viewer:'Customer viewer'
      };
      return labels[role] || role;
    }

    async function loadBusinessUsers() {
      const response = await fetch('/api/users');
      const result = await response.json();
      const rows = (result.users || []).filter(user => businessRoles.has(user.role));
      document.getElementById('business-user-rows').innerHTML = rows.length
        ? rows.map(user => `<tr>
          <td><strong>${businessSafe(user.display_name)}</strong></td>
          <td>${businessSafe(user.email)}</td>
          <td>${businessSafe(businessRoleLabel(user.role))}</td>
          <td><a href="${businessPortal(user.role)}">${businessPortal(user.role)}</a></td>
          <td><span class="${user.enabled ? 'active-badge' : 'pending-badge'}">${user.enabled ? 'Active' : 'Disabled'}</span></td>
          <td>${user.id === 'local-admin'
            ? '<span class="health-detail">Protected master</span>'
            : (!user.enabled && user.invitation_status === 'pending'
              ? `<div class="row-actions"><button class="compact-button" onclick="approveBusinessUser('${user.id}')">Approve</button><button class="compact-button danger" onclick="deleteBusinessUser('${user.id}')">Reject</button></div>`
              : `<button class="compact-button danger" onclick="deleteBusinessUser('${user.id}')">Delete</button>`)}</td>
        </tr>`).join('')
        : '<tr><td colspan="6"><div class="empty">No business portal accounts found.</div></td></tr>';
    }

    async function approveBusinessUser(userId) {
      if (!confirm('Approve this account request?')) return;
      const response = await fetch(`/api/business-users/${userId}/approve`, {method:'POST'});
      const result = await response.json();
      showToast(result.message || 'Account updated.');
      if (result.status === 'complete') await loadBusinessUsers();
    }

    async function deleteBusinessUser(userId) {
      if (!confirm('Delete this business portal account?')) return;
      const response = await fetch(`/api/users/${userId}`, {method:'DELETE'});
      const result = await response.json();
      showToast(result.message || 'Account updated.');
      if (result.status === 'complete') await loadBusinessUsers();
    }

    document.getElementById('business-user-form').addEventListener('submit', async event => {
      event.preventDefault();
      const role = document.getElementById('business-role').value;
      const payload = {
        display_name: document.getElementById('business-name').value.trim(),
        email: document.getElementById('business-email').value.trim(),
        password: document.getElementById('business-password').value,
        role,
        enabled: true,
        super_admin: role === 'administrator',
        site_ids: ['home'],
        camera_ids: role.startsWith('customer_') ? [] : []
      };

      const response = await fetch('/api/users', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
      });
      const result = await response.json();
      const message = document.getElementById('business-user-message');
      message.textContent = result.message || '';
      if (result.status === 'complete') {
        event.target.reset();
        await loadBusinessUsers();
      }
    });

    document.getElementById('refresh-business-users').addEventListener('click', loadBusinessUsers);
    loadBusinessUsers();
    </script>
    """

    return page_shell(
        "Business users",
        "business-users",
        content,
        scripts,
    )


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_users"):
        return permission_denied_page("Users", "users", "manage_users")

    camera_checks = "".join(
        f'<label><input type="checkbox" name="invite_camera" value="{camera}"> Camera {camera}</label>'
        for camera in range(1, CAMERA_COUNT + 1)
    )
    content = f"""
    <header class="topbar">
        <div><p class="eyebrow">Camera sharing</p><h1>Camera-sharing users and permissions</h1></div>
        <button class="action-button" id="open-invite" type="button">Invite user</button>
    </header>

    <nav class="enterprise-tabs">
        <a class="enterprise-tab" href="/settings">Account details</a>
        <a class="enterprise-tab active" href="/users">Camera sharing users</a>
        <a class="enterprise-tab" href="/audit-logs">Audit logs</a>
    </nav>

    <section class="panel" style="margin-bottom:18px">
        <div class="panel-head"><div><h2>Users</h2><div class="health-detail">Camera access and permission levels for active accounts.</div></div><input id="directory-search" type="search" placeholder="Search users"></div>
        <div class="audit-table-wrap">
            <table class="enterprise-user-table">
                <thead><tr><th>Email</th><th>Super admin</th><th>Permission level</th><th>Cameras</th><th>Status</th><th></th></tr></thead>
                <tbody id="active-users"><tr><td colspan="6"><div class="empty">Loading users...</div></td></tr></tbody>
            </table>
        </div>
    </section>

    <section class="panel">
        <div class="panel-head"><div><h2>Pending invites</h2><div class="health-detail">Invitations expire after seven days.</div></div></div>
        <div class="audit-table-wrap">
            <table class="enterprise-user-table">
                <thead><tr><th>Email</th><th>Permission level</th><th>Cameras</th><th>Expires</th><th>Status</th><th></th></tr></thead>
                <tbody id="pending-invites"><tr><td colspan="6"><div class="empty">Loading invitations...</div></td></tr></tbody>
            </table>
        </div>
    </section>

    <dialog class="user-dialog" id="invite-dialog">
        <div class="user-dialog-body">
            <div class="panel-head"><h2>Invite user</h2><button class="compact-button" id="close-invite" type="button">Close</button></div>
            <form class="user-form" id="invite-form">
                <label>Email<input id="invite-email" type="email" required></label>
                <label>Display name<input id="invite-display-name" placeholder="Optional"></label>
                <label class="full"><span><input id="invite-super-admin" type="checkbox"> Super Admin</span></label>
                <div class="full">
                    <span class="health-detail">Permission level</span>
                    <div class="permission-levels">
                        <label class="permission-option">Admin<input name="invite-role" type="radio" value="admin"></label>
                        <label class="permission-option">Read-only<input name="invite-role" type="radio" value="read-only"></label>
                        <label class="permission-option">View-only<input name="invite-role" type="radio" value="view-only" checked></label>
                        <label class="permission-option">Live-only<input name="invite-role" type="radio" value="live-only"></label>
                        <label class="permission-option">None<input name="invite-role" type="radio" value="none"></label>
                    </div>
                </div>
                <label class="full"><span><input id="invite-all-cameras" type="checkbox" checked> Grant access to all cameras</span></label>
                <div class="full" id="individual-camera-box" hidden>
                    <span class="health-detail">Individual cameras</span>
                    <div class="camera-access-grid">{camera_checks}</div>
                </div>
                <div class="dialog-actions full"><button class="ghost-button" id="cancel-invite" type="button">Cancel</button><button class="action-button" type="submit">Send invitation</button></div>
            </form>
        </div>
    </dialog>
    """

    scripts = """
    <script>
    let activeUsers=[];
    let pendingInvites=[];
    const inviteDialog=document.getElementById('invite-dialog');
    function safe(value){const node=document.createElement('span');node.textContent=value??'';return node.innerHTML;}
    function cameraLabel(item){
        return (item.camera_ids||[]).length ? item.camera_ids.map(id=>`Camera ${id}`).join(', ') : 'All cameras';
    }
    function renderUsers(){
        const needle=document.getElementById('directory-search').value.trim().toLowerCase();
        const users=activeUsers.filter(user=>!needle||`${user.display_name} ${user.email} ${user.role}`.toLowerCase().includes(needle));
        document.getElementById('active-users').innerHTML=users.length?users.map(user=>`
            <tr>
                <td><strong>${safe(user.email||user.display_name)}</strong><div class="health-detail">${safe(user.display_name)}</div></td>
                <td>${user.super_admin?'Yes':'No'}</td>
                <td>${safe(user.role)}</td>
                <td>${safe(cameraLabel(user))}</td>
                <td><span class="${user.enabled?'active-badge':'pending-badge'}">${user.enabled?'Active':'Disabled'}</span></td>
                <td><button class="compact-button danger" onclick="deleteUser('${user.id}')">Delete</button></td>
            </tr>`).join(''):'<tr><td colspan="6"><div class="empty">No users found.</div></td></tr>';
    }
    function renderInvites(){
        document.getElementById('pending-invites').innerHTML=pendingInvites.length?pendingInvites.map(invite=>`
            <tr>
                <td>${safe(invite.email)}</td>
                <td>${safe(invite.role)}</td>
                <td>${invite.all_cameras?'All cameras':safe((invite.camera_ids||[]).map(id=>`Camera ${id}`).join(', '))}</td>
                <td>${new Date(invite.expires_at).toLocaleString()}</td>
                <td><span class="pending-badge">${safe(invite.status)}</span></td>
                <td><button class="compact-button" onclick="resendInvite('${invite.id}')">Resend</button> <button class="compact-button danger" onclick="cancelInvite('${invite.id}')">Cancel</button></td>
            </tr>`).join(''):'<tr><td colspan="6"><div class="empty">No pending invites.</div></td></tr>';
    }
    async function loadDirectory(){
        const [usersResponse,invitesResponse]=await Promise.all([
            fetch('/api/users',{cache:'no-store'}),
            fetch('/api/user-invites',{cache:'no-store'})
        ]);
        const usersData=await usersResponse.json();
        const invitesData=await invitesResponse.json();
        activeUsers=usersData.status==='complete'?usersData.users:[];
        pendingInvites=invitesData.status==='complete'?invitesData.invites.filter(item=>item.status==='pending'):[];
        renderUsers();renderInvites();
    }
    window.deleteUser=async id=>{
        if(!confirm('Delete this user?'))return;
        const response=await fetch(`/api/users/${id}`,{method:'DELETE'});
        const data=await response.json();showToast(data.message);
        if(data.status==='complete')loadDirectory();
    };
    window.resendInvite=async id=>{
        const response=await fetch(`/api/user-invites/${id}/resend`,{method:'POST'});
        const data=await response.json();showToast(data.message);
        if(data.status==='complete')loadDirectory();
    };
    window.cancelInvite=async id=>{
        if(!confirm('Cancel this invitation?'))return;
        const response=await fetch(`/api/user-invites/${id}`,{method:'DELETE'});
        const data=await response.json();showToast(data.message);loadDirectory();
    };
    document.getElementById('invite-form').addEventListener('submit',async event=>{
        event.preventDefault();
        const allCameras=document.getElementById('invite-all-cameras').checked;
        const payload={
            email:document.getElementById('invite-email').value,
            display_name:document.getElementById('invite-display-name').value,
            role:document.querySelector('[name=invite-role]:checked').value,
            super_admin:document.getElementById('invite-super-admin').checked,
            all_cameras:allCameras,
            camera_ids:allCameras?[]:[...document.querySelectorAll('[name=invite_camera]:checked')].map(input=>Number(input.value))
        };
        const response=await fetch('/api/user-invites',{
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
        });
        const data=await response.json();showToast(data.message);
        if(data.status==='complete'){inviteDialog.close();event.target.reset();document.getElementById('invite-all-cameras').checked=true;document.getElementById('individual-camera-box').hidden=true;loadDirectory();}
    });
    document.getElementById('open-invite').addEventListener('click',()=>inviteDialog.showModal());
    document.getElementById('close-invite').addEventListener('click',()=>inviteDialog.close());
    document.getElementById('cancel-invite').addEventListener('click',()=>inviteDialog.close());
    document.getElementById('invite-all-cameras').addEventListener('change',event=>document.getElementById('individual-camera-box').hidden=event.target.checked);
    document.getElementById('invite-super-admin').addEventListener('change',event=>{
        if(event.target.checked)document.querySelector('[name=invite-role][value=admin]').checked=true;
    });
    document.getElementById('directory-search').addEventListener('input',renderUsers);
    loadDirectory();
    </script>
    """
    record_audit(request, "view", "users", "Opened enterprise user management.")
    return page_shell("Users", "users", content, scripts)


@app.get("/audit-logs", response_class=HTMLResponse)
def audit_logs(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "view_audit"):
        return permission_denied_page("Audit logs", "audit", "view_audit")
    content = """
    <header class="topbar">
        <div><p class="eyebrow">Accountability</p><h1>Audit logs</h1></div>
        <div class="audit-toolbar">
            <input id="audit-query" type="search" placeholder="Search activity">
            <select id="audit-action"><option value="">All actions</option><option>create</option><option>update</option><option>delete</option><option>view</option><option>switch_session</option></select>
            <select id="audit-role"><option value="">All roles</option><option>admin</option><option>installer</option><option>operator</option><option>viewer</option></select>
            <select id="audit-outcome"><option value="">All outcomes</option><option>success</option><option>denied</option><option>failed</option></select>
        </div>
    </header>
    <section class="panel">
        <div class="panel-head"><h2>Recorded system actions</h2><span class="health-detail" id="audit-count">Loading…</span></div>
        <div class="audit-table-wrap">
            <table class="data-table"><thead><tr><th>Time</th><th>User</th><th>Role</th><th>Action</th><th>Resource</th><th>Detail</th><th>Outcome</th></tr></thead><tbody id="audit-body"></tbody></table>
        </div>
    </section>
    """
    scripts = """
    <script>
    const body = document.getElementById('audit-body');
    function safe(value) {
        const node = document.createElement('span');
        node.textContent = value ?? '';
        return node.innerHTML;
    }
    async function loadAudit() {
        const params = new URLSearchParams();
        ['query','action','role','outcome'].forEach(key => {
            const value = document.getElementById(`audit-${key}`).value.trim();
            if (value) params.set(key, value);
        });
        const response = await fetch(`/api/audit-logs?${params}`, {cache: 'no-store'});
        const data = await response.json();
        if (data.status !== 'complete') {
            body.innerHTML = `<tr><td colspan="7">${safe(data.message)}</td></tr>`;
            return;
        }
        document.getElementById('audit-count').textContent = `${data.entries.length} action(s)`;
        body.innerHTML = data.entries.length ? data.entries.map(entry => `
            <tr>
                <td>${new Date(entry.timestamp).toLocaleString()}</td>
                <td>${safe(entry.user_name)}</td>
                <td><span class="role-badge ${safe(entry.role)}">${safe(entry.role)}</span></td>
                <td>${safe(entry.action)}</td>
                <td>${safe(entry.resource)}</td>
                <td><div class="audit-detail">${safe(entry.detail)}</div></td>
                <td><span class="audit-outcome ${safe(entry.outcome)}">${safe(entry.outcome)}</span></td>
            </tr>`).join('') : '<tr><td colspan="7">No matching audit entries.</td></tr>';
    }
    document.querySelectorAll('.audit-toolbar input,.audit-toolbar select').forEach(control => {
        control.addEventListener('input', loadAudit);
        control.addEventListener('change', loadAudit);
    });
    loadAudit();
    setInterval(loadAudit, 30000);
    </script>
    """
    record_audit(request, "view", "audit_logs", "Opened audit log page.")
    return page_shell("Audit logs", "audit", content, scripts)



def site_monitoring_summary() -> dict:
    statuses = camera_status().get("cameras", [])
    motion_events = load_motion_events()
    today = datetime.now().date()
    events_today = 0
    for event in motion_events:
        raw_time = event.get("start_time") or event.get("timestamp")
        try:
            if raw_time and datetime.fromisoformat(raw_time).date() == today:
                events_today += 1
        except (TypeError, ValueError):
            continue

    online_cameras = sum(1 for status in statuses if status.get("online"))
    recording_cameras = sum(
        1 for status in statuses if status.get("recording") == "running"
    )
    active_alerts = len(health_issues)
    metrics = system_metrics()

    sites = [
        {
            "id": "home",
            "name": "Home",
            "status": "online" if online_cameras == CAMERA_COUNT else "warning",
            "camera_count": CAMERA_COUNT,
            "online_cameras": online_cameras,
            "recording_cameras": recording_cameras,
            "events_today": events_today,
            "active_alerts": active_alerts,
            "storage_free_gb": metrics.get("storage_free_gb", 0),
            "cameras": statuses,
        }
    ]

    for customer in load_partner_customers():
        site_name = customer.get("site_name") or customer.get("name") or "Customer site"
        sites.append(
            {
                "id": customer.get("id", slugify(site_name)),
                "name": site_name,
                "status": "pending",
                "camera_count": 0,
                "online_cameras": 0,
                "recording_cameras": 0,
                "events_today": 0,
                "active_alerts": 0,
                "storage_free_gb": None,
                "cameras": [],
            }
        )

    return {
        "sites": sites,
        "site_count": len(sites),
        "camera_count": CAMERA_COUNT,
        "online_cameras": online_cameras,
        "recording_cameras": recording_cameras,
        "events_today": events_today,
        "active_alerts": active_alerts,
        "checked_at": datetime.now().isoformat(),
    }


@app.get("/api/sites/summary")
def sites_summary_api() -> dict:
    return site_monitoring_summary()


@app.get("/sites", response_class=HTMLResponse)
@app.get("/sites-management", response_class=HTMLResponse)
def sites() -> str:
    camera_markers = "".join(
        f'<button class="site-camera-marker" data-camera="{camera}" '
        f'title="Open Camera {camera}">{camera}</button>'
        for camera in range(1, CAMERA_COUNT + 1)
    )
    camera_rows = "".join(
        f'<a class="site-camera-row" href="/camera/{camera}">'
        f'<div><strong>Camera {camera}</strong>'
        f'<div class="health-detail" id="site-camera-detail-{camera}">Checking stream…</div></div>'
        f'<span class="site-camera-state" id="site-camera-state-{camera}">Checking</span>'
        f'</a>'
        for camera in range(1, CAMERA_COUNT + 1)
    )

    content = f"""
    <header class="topbar">
        <div>
            <p class="eyebrow">Locations</p>
            <h1>Site monitoring</h1>
        </div>
        <div>
            <a class="ghost-button" href="/analytics">View analytics</a>
            <a class="action-button" href="/setup">Add site</a>
        </div>
    </header>

    <section class="site-summary-grid">
        <article class="site-summary-card"><span class="site-summary-label">Sites</span><strong class="site-summary-value" id="site-count">—</strong><span class="site-summary-detail">Monitored locations</span></article>
        <article class="site-summary-card"><span class="site-summary-label">Cameras online</span><strong class="site-summary-value" id="site-online-cameras">—</strong><span class="site-summary-detail" id="site-camera-total">Across all sites</span></article>
        <article class="site-summary-card"><span class="site-summary-label">Recording</span><strong class="site-summary-value" id="site-recording-cameras">—</strong><span class="site-summary-detail">Active recording workers</span></article>
        <article class="site-summary-card"><span class="site-summary-label">Active alerts</span><strong class="site-summary-value" id="site-active-alerts">—</strong><span class="site-summary-detail">Needs attention</span></article>
    </section>

    <section class="site-monitor-grid">
        <article class="panel">
            <div class="panel-head">
                <div><h2>Home floor plan</h2><div class="health-detail">Click a camera marker to open its live view.</div></div>
                <span class="site-refresh" id="site-refresh">Loading status…</span>
            </div>
            <div class="site-map">
                <div class="site-map-floor">
                    <div class="site-room large"><span class="site-room-label">Main area</span></div>
                    <div class="site-room"><span class="site-room-label">Front area</span></div>
                    <div class="site-room"><span class="site-room-label">Rear area</span></div>
                </div>
                {camera_markers}
                <div class="site-map-legend"><span>Online</span><span>Needs attention</span></div>
            </div>
        </article>

        <article class="panel">
            <div class="panel-head"><h2>All sites</h2><span class="health-detail">Live status</span></div>
            <div class="site-list" id="site-list">
                <div class="empty">Loading sites…</div>
            </div>
        </article>
    </section>

    <section class="panel">
        <div class="panel-head">
            <h2>Home cameras</h2>
            <a class="download" href="/">Open live workspace</a>
        </div>
        <div class="site-camera-list">{camera_rows}</div>
    </section>
    """

    scripts = """
    <script>
    function createSiteCard(site) {
        const card = document.createElement('article');
        card.className = 'site-card' + (site.id === 'home' ? ' active' : '');
        const statusClass = site.status === 'online' ? '' : ' warning';
        const storageText = site.storage_free_gb == null
            ? 'Not connected'
            : `${site.storage_free_gb} GB free`;
        card.innerHTML = `
            <div class="site-card-head">
                <div>
                    <div class="site-card-name">${site.name}</div>
                    <div class="site-card-meta">${site.camera_count} camera(s) · ${storageText}</div>
                </div>
                <span class="site-card-status${statusClass}">${site.status}</span>
            </div>
            <div class="site-card-stats">
                <div class="site-card-stat"><strong>${site.online_cameras}/${site.camera_count}</strong><span>Online</span></div>
                <div class="site-card-stat"><strong>${site.events_today}</strong><span>Events today</span></div>
                <div class="site-card-stat"><strong>${site.active_alerts}</strong><span>Alerts</span></div>
            </div>`;
        return card;
    }

    async function updateSites() {
        const refresh = document.getElementById('site-refresh');
        try {
            const response = await fetch('/api/sites/summary', {cache: 'no-store'});
            const data = await response.json();
            document.getElementById('site-count').textContent = data.site_count;
            document.getElementById('site-online-cameras').textContent = data.online_cameras;
            document.getElementById('site-camera-total').textContent = `of ${data.camera_count} configured cameras`;
            document.getElementById('site-recording-cameras').textContent = data.recording_cameras;
            document.getElementById('site-active-alerts').textContent = data.active_alerts;

            const list = document.getElementById('site-list');
            list.replaceChildren(...data.sites.map(createSiteCard));

            const home = data.sites.find(site => site.id === 'home');
            if (home) {
                home.cameras.forEach(camera => {
                    const marker = document.querySelector(`.site-camera-marker[data-camera="${camera.camera}"]`);
                    const state = document.getElementById(`site-camera-state-${camera.camera}`);
                    const detail = document.getElementById(`site-camera-detail-${camera.camera}`);
                    if (marker) marker.classList.toggle('offline', !camera.online);
                    if (state) {
                        state.textContent = camera.online ? 'Online' : 'Reconnecting';
                        state.classList.toggle('offline', !camera.online);
                    }
                    if (detail) {
                        detail.textContent = camera.online
                            ? `Stream active · recording ${camera.recording}`
                            : `Stream ${camera.stream} · recording ${camera.recording}`;
                    }
                });
            }
            refresh.textContent = `Updated ${new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})}`;
        } catch (error) {
            refresh.textContent = 'Site status unavailable';
        }
    }

    document.querySelectorAll('.site-camera-marker').forEach(marker => {
        marker.addEventListener('click', () => {
            location.href = `/camera/${marker.dataset.camera}`;
        });
    });

    updateSites();
    setInterval(updateSites, 15000);
    </script>
    """
    return page_shell("Sites", "sites", content, scripts)


SETTINGS_CATEGORIES = [
    ("Cameras", "Camera names, streams, and connection health"),
    ("Events & alerts", "Motion zones, sensitivity, schedules, and delivery"),
    ("Recording", "Schedules, quality, segmentation, and retention"),
    ("Analytics", "Detection types, zones, and sensitivity"),
    ("Notifications", "Alert delivery and quiet hours"),
    ("Users", "Accounts, roles, and permissions"),
    ("Network", "Remote access and connectivity"),
    ("Storage", "Local storage and future cloud backup"),
    ("System", "VMS status, updates, and diagnostics"),
    ("Integrations", "Future APIs and connected services"),
]


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page("Settings", "settings", "manage_settings")
    record_audit(request, "view", "settings", "Opened settings overview.")
    links = "".join(
        f'<a class="setting-link" href="/settings/{slugify(name)}"><div><strong>{escape(name)}</strong><div class="health-detail">{escape(description)}</div></div><span>›</span></a>'
        for name, description in SETTINGS_CATEGORIES
    )
    content = f"""<header class="topbar"><div><p class="eyebrow">Configuration</p><h1>Settings</h1></div></header><section class="settings-list">{links}</section>"""
    return page_shell("Settings", "settings", content)


@app.get("/settings/{settings_slug}", response_class=HTMLResponse)
def settings_detail(settings_slug: str, request: Request) -> str:
    user = current_user(request)
    if not has_permission(user, "manage_settings"):
        return permission_denied_page("Settings", "settings", "manage_settings")
    record_audit(request, "view", f"settings:{settings_slug}", "Opened settings section.")
    if settings_slug == "events-alerts":
        camera_options = "".join(f'<option value="{n}">Camera {n}</option>' for n in range(1, CAMERA_COUNT + 1))
        content = f"""<header class="topbar"><div><p class="eyebrow">Settings</p><h1>Events & alerts</h1></div><a class="download" href="/settings">All settings</a></header><section class="health-grid"><form class="panel" id="motion-settings-form"><div class="panel-head"><h2>Motion detection</h2><label><input id="motion-enabled" type="checkbox"> Enabled</label></div><label>Camera<select id="event-camera">{camera_options}</select></label><label>Sensitivity <strong id="sensitivity-value">60</strong><input id="motion-sensitivity" type="range" min="1" max="100" value="60"></label><label>Minimum event duration<input id="minimum-duration" type="number" min="1" max="60" value="2"> seconds</label><label>Cooldown<input id="motion-cooldown" type="number" min="0" max="3600" value="15"> seconds</label><h2>Motion zone</h2><div class="settings-list"><label>X<input id="zone-x" type="number" min="0" max="1" step="0.01" value="0"></label><label>Y<input id="zone-y" type="number" min="0" max="1" step="0.01" value="0"></label><label>Width<input id="zone-width" type="number" min="0.01" max="1" step="0.01" value="1"></label><label>Height<input id="zone-height" type="number" min="0.01" max="1" step="0.01" value="1"></label></div><button class="action-button" type="submit">Save motion settings</button></form><form class="panel" id="alert-rule-form"><div class="panel-head"><h2>Alert workflow</h2><label><input id="alerts-enabled" type="checkbox"> Enabled</label></div><div class="health-row"><span>Event types</span><label><input id="alert-motion" type="checkbox" checked> Motion</label><label><input id="alert-person" type="checkbox"> Person</label><label><input id="alert-vehicle" type="checkbox"> Vehicle</label></div><label>Active from<input id="schedule-start" type="time" value="00:00"></label><label>Active until<input id="schedule-end" type="time" value="23:59"></label><div class="health-row"><span>Delivery</span><label><input id="delivery-in-app" type="checkbox" checked> In-app</label><label title="Coming soon"><input type="checkbox" disabled> Push</label><label title="Coming soon"><input type="checkbox" disabled> Email</label><label title="Coming soon"><input type="checkbox" disabled> SMS</label></div><button class="action-button" type="submit">Save alert rule</button></form></section>"""
        scripts = """<script>const camera=document.getElementById('event-camera'),sensitivity=document.getElementById('motion-sensitivity');sensitivity.addEventListener('input',()=>document.getElementById('sensitivity-value').textContent=sensitivity.value);async function loadEventConfiguration(){const [settingsResponse,ruleResponse]=await Promise.all([fetch(`/api/cameras/${camera.value}/event-settings`),fetch(`/api/cameras/${camera.value}/alert-rule`)]),settings=(await settingsResponse.json()).settings,rule=(await ruleResponse.json()).rule;document.getElementById('motion-enabled').checked=settings.enabled;sensitivity.value=settings.sensitivity;document.getElementById('sensitivity-value').textContent=settings.sensitivity;document.getElementById('minimum-duration').value=settings.minimum_duration_seconds;document.getElementById('motion-cooldown').value=settings.cooldown_seconds;const zone=settings.zones[0]||{x:0,y:0,width:1,height:1};['x','y','width','height'].forEach(key=>document.getElementById(`zone-${key}`).value=zone[key]);document.getElementById('alerts-enabled').checked=rule.enabled;document.getElementById('alert-motion').checked=rule.event_types.includes('motion');document.getElementById('alert-person').checked=rule.event_types.includes('person');document.getElementById('alert-vehicle').checked=rule.event_types.includes('vehicle');document.getElementById('schedule-start').value=rule.schedule_start;document.getElementById('schedule-end').value=rule.schedule_end;document.getElementById('delivery-in-app').checked=rule.delivery_methods.includes('in_app')}camera.addEventListener('change',loadEventConfiguration);document.getElementById('motion-settings-form').addEventListener('submit',async event=>{event.preventDefault();const payload={camera:Number(camera.value),enabled:document.getElementById('motion-enabled').checked,sensitivity:Number(sensitivity.value),minimum_duration_seconds:Number(document.getElementById('minimum-duration').value),cooldown_seconds:Number(document.getElementById('motion-cooldown').value),zones:[{name:'Primary zone',x:Number(document.getElementById('zone-x').value),y:Number(document.getElementById('zone-y').value),width:Number(document.getElementById('zone-width').value),height:Number(document.getElementById('zone-height').value)}]},response=await fetch(`/api/cameras/${camera.value}/event-settings`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=await response.json();showToast(result.message)});document.getElementById('alert-rule-form').addEventListener('submit',async event=>{event.preventDefault();const types=['motion','person','vehicle'].filter(type=>document.getElementById(`alert-${type}`).checked),payload={camera:Number(camera.value),enabled:document.getElementById('alerts-enabled').checked,event_types:types,schedule_start:document.getElementById('schedule-start').value,schedule_end:document.getElementById('schedule-end').value,delivery_methods:document.getElementById('delivery-in-app').checked?['in_app']:[]},response=await fetch(`/api/cameras/${camera.value}/alert-rule`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=await response.json();showToast(result.message)});loadEventConfiguration();</script>"""
        return page_shell("Events & alerts settings", "settings", content, scripts)
    match = next(
        ((name, description) for name, description in SETTINGS_CATEGORIES if slugify(name) == settings_slug),
        None,
    )
    if match is None:
        return page_shell("Settings", "settings", '<div class="empty">Settings category not found.</div>')
    name, description = match
    content = f"""<header class="topbar"><div><p class="eyebrow">Settings</p><h1>{escape(name)}</h1></div><a class="download" href="/settings">All settings</a></header><section class="panel"><div class="panel-head"><h2>{escape(description)}</h2><span class="pill wait">Read-only preview</span></div><div class="empty">Controls for {escape(name.lower())} will be connected here without changing the existing environment file or recording configuration.</div></section>"""
    return page_shell(f"{name} settings", "settings", content)



@app.get("/phone-connect", response_class=HTMLResponse)
def phone_connect(request: Request) -> str:
    base_url = PHONE_ACCESS_URL or str(request.base_url).rstrip("/")
    local_warning = (
        "This address uses localhost. A phone cannot reach localhost on the Samsung laptop. "
        "Set ANYAICAM_PHONE_URL to the Samsung LAN or Tailscale address."
        if "localhost" in base_url or "127.0.0.1" in base_url
        else ""
    )
    content = f"""<header class="topbar"><div><p class="eyebrow">Secure mobile access</p><h1>Connect your phone</h1></div></header>
    <section class="phone-connect-grid">
      <article class="phone-connect-card">
        <h2>Phone address</h2>
        <p class="health-detail">Open this exact address on your phone. Keep the same address for login, cameras, alerts, and playback.</p>
        <div class="phone-url"><span id="phone-url">{escape(base_url)}</span><button class="ghost-button" id="copy-phone-url" type="button">Copy</button></div>
        {f'<div class="mock-banner" style="margin-top:12px">{escape(local_warning)}</div>' if local_warning else ''}
        <div class="phone-checklist">
          <div class="phone-check"><strong>1</strong><span>Connect the phone and Samsung laptop to the same Wi-Fi, or connect both devices to Tailscale.</span></div>
          <div class="phone-check"><strong>2</strong><span>Open the address above in Safari or Chrome and sign in with the existing account.</span></div>
          <div class="phone-check"><strong>3</strong><span>Use the browser menu to add AnyAiCam to the phone home screen.</span></div>
          <div class="phone-check"><strong>4</strong><span>Open Notifications and enable push alerts after VAPID keys are configured.</span></div>
        </div>
      </article>
      <article class="phone-connect-card">
        <h2>Connection checks</h2>
        <div class="phone-status">
          <div class="phone-status-row"><span>Login</span><strong>Existing secure login</strong></div>
          <div class="phone-status-row"><span>Camera stream</span><strong>/static/hls</strong></div>
          <div class="phone-status-row"><span>Session cookie</span><strong>{'Secure' if SECURE_COOKIES else 'Local HTTP mode'}</strong></div>
          <div class="phone-status-row"><span>Push server</span><strong>{'Configured' if VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY else 'Needs VAPID keys'}</strong></div>
          <div class="phone-status-row"><span>Public URL</span><strong>{'Configured' if PUBLIC_BASE_URL else 'Local/Tailscale only'}</strong></div>
        </div>
        <div class="dialog-actions">
          <a class="action-button" href="/">Open cameras</a>
          <a class="ghost-button" href="/alerts">Open alerts</a>
          <a class="ghost-button" href="/playback">Open playback</a>
        </div>
      </article>
    </section>"""
    scripts = """<script>
    document.getElementById('copy-phone-url').addEventListener('click',async()=>{
      const value=document.getElementById('phone-url').textContent.trim();
      try{await navigator.clipboard.writeText(value);showToast('Phone address copied.')}
      catch(error){showToast('Copy failed. Press and hold the address to copy it.')}
    });
    </script>"""
    return page_shell("Phone access", "phone", content, scripts)


@app.get("/help", response_class=HTMLResponse)
def help_page() -> str:
    content = """<header class="topbar"><div><p class="eyebrow">Support</p><h1>Help</h1></div></header><section class="feature-grid"><article class="feature-card"><div class="feature-icon">?</div><h2>Getting started</h2><p>Use Live view to monitor cameras and Playback to review completed five-minute recordings.</p></article><article class="feature-card"><div class="feature-icon">⌁</div><h2>Remote access</h2><p>Use the same page through your private Tailscale address while the home computer and VMS are running.</p></article><article class="feature-card"><div class="feature-icon">!</div><h2>Camera offline</h2><p>The interface remains available when cameras are unreachable and reconnects after the VMS restarts.</p></article></section>"""
    return page_shell("Help", "help", content)




def load_partner_quotes() -> list[dict]:
    quotes = load_json_file(PARTNER_QUOTES_FILE, [])
    return quotes if isinstance(quotes, list) else []


def save_partner_quotes(quotes: list[dict]) -> None:
    save_json_file(PARTNER_QUOTES_FILE, quotes[-5000:])


def partner_quote_price(quote: dict) -> dict:
    pricing = {
        "2mp": {
            "motion": {2: 7.99, 7: 8.09, 14: 8.39, 30: 8.99},
            "continuous": {2: 8.99, 7: 9.79, 14: 10.89, 30: 11.99},
        },
        "4mp": {
            "motion": {2: 8.19, 7: 8.59, 14: 9.19, 30: 10.09},
            "continuous": {2: 9.49, 7: 11.09, 14: 13.29, 30: 14.79},
        },
        "8mp": {
            "motion": {2: 8.49, 7: 9.09, 14: 9.89, 30: 11.29},
            "continuous": {2: 9.99, 7: 12.39, 14: 15.69, 30: 17.59},
        },
    }
    analytics_prices = {
        "smart_motion": 1.79,
        "people_counting": 10.99,
        "lpr": 18.99,
        "ppe_detection": 17.99,
    }
    resolution = str(quote.get("resolution") or "2mp").lower()
    mode = str(quote.get("recording_mode") or "motion").lower()
    retention = int(quote.get("retention_days") or 7)
    available = pricing.get(resolution, pricing["2mp"]).get(mode, pricing["2mp"]["motion"])
    closest_retention = min(available, key=lambda value: abs(value - retention))
    base_per_camera = available[closest_retention]
    analytics_per_camera = sum(
        analytics_prices.get(str(item).lower(), 0)
        for item in quote.get("analytics") or []
    )
    quantity = max(1, int(quote.get("camera_quantity") or 1))
    hardware_total = sum(
        max(0, int(item.get("quantity") or 0))
        * max(0, int(item.get("unit_price_cents") or 0))
        for item in quote.get("hardware_items") or []
        if isinstance(item, dict)
    )
    monthly_cents = round((base_per_camera + analytics_per_camera) * quantity * 100)
    return {
        "base_per_camera_cents": round(base_per_camera * 100),
        "analytics_per_camera_cents": round(analytics_per_camera * 100),
        "monthly_cents": monthly_cents,
        "hardware_total_cents": hardware_total,
        "first_payment_estimate_cents": monthly_cents + hardware_total,
        "currency": "USD",
    }


@app.get("/partner-sales", response_class=HTMLResponse)
def partner_sales_command_center(request: Request) -> Response:
    authorization_response = partner_page_authorization_response(request)
    if authorization_response is not None:
        return authorization_response
    customers = load_partner_customers()

    total = len(customers)
    active = sum(item.get("status") == "active" for item in customers)
    pending = sum(item.get("status") == "pending_installation" for item in customers)
    trials = sum(item.get("status") == "trial" for item in customers)
    attention = sum(item.get("status") in {"suspended", "cancelled"} for item in customers)

    def badge(value: object) -> str:
        status = str(value or "active").strip().lower()
        return f'<span class="admin-command-badge {escape(status, quote=True)}">{escape(status.replace("_", " "))}</span>'

    cards = []
    for customer in sorted(
        customers,
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    ):
        cards.append(
            f'''<article class="partner-sales-card">
              <div class="partner-sales-head">
                <div>
                  <strong>{escape(str(customer.get("name") or "Customer"))}</strong>
                  <div class="partner-sales-meta">
                    {escape(str(customer.get("email") or "No email"))}<br>
                    Site: {escape(str(customer.get("site_name") or "Primary site"))}
                    · Customer ID: {escape(str(customer.get("id") or "—"))}<br>
                    Created: {escape(str(customer.get("created_at") or "—"))}
                  </div>
                </div>
                {badge(customer.get("status"))}
              </div>
              <div class="partner-sales-actions">
                <button class="partner-select-customer" type="button"
                  data-id="{escape(str(customer.get("id") or ""), quote=True)}"
                  data-name="{escape(str(customer.get("name") or ""), quote=True)}"
                  data-email="{escape(str(customer.get("email") or ""), quote=True)}"
                  data-site="{escape(str(customer.get("site_name") or "Primary site"), quote=True)}"
                  data-status="{escape(str(customer.get("status") or "active"), quote=True)}">Manage</button>
                <a href="/partner">Open partner workspace</a>
                <a href="/onboarding">Start onboarding</a>
                <a href="/partner/appliance-dashboard">Appliance handoff</a>
              </div>
            </article>'''
        )

    content = f'''<header class="topbar"><div><p class="eyebrow">Partner portal</p><h1>Sales command center</h1></div><span class="pill">Assigned customer workflow</span></header>
    <div class="admin-privacy-note"><strong>Partner boundary:</strong> this page supports customer creation, sales status, onboarding and appliance handoff. It does not expose customer camera video, administrator-only billing controls, or unrelated customers.</div>
    <section class="partner-sales-summary" style="margin-top:16px">
      <article class="partner-sales-stat"><span>Total customers</span><strong>{total}</strong></article>
      <article class="partner-sales-stat"><span>Active</span><strong>{active}</strong></article>
      <article class="partner-sales-stat"><span>Pending installation</span><strong>{pending}</strong></article>
      <article class="partner-sales-stat"><span>Trials</span><strong>{trials}</strong></article>
      <article class="partner-sales-stat"><span>Needs attention</span><strong>{attention}</strong></article>
    </section>
    <section class="partner-sales-layout">
      <div class="partner-sales-list">{"".join(cards) or '<div class="empty">No partner customers found.</div>'}</div>
      <aside class="partner-sales-detail">
        <h2>Customer pipeline</h2>
        <form id="partner-sales-form">
          <input id="partner-sales-id" type="hidden">
          <label>Customer name<input id="partner-sales-name" required></label>
          <label>Email<input id="partner-sales-email" type="email" required></label>
          <label>Primary site<input id="partner-sales-site" value="Primary site" required></label>
          <label>Status
            <select id="partner-sales-status">
              <option value="trial">Trial</option>
              <option value="active">Active</option>
              <option value="pending_installation">Pending installation</option>
              <option value="suspended">Suspended</option>
              <option value="cancelled">Cancelled</option>
            </select>
          </label>
          <div class="partner-sales-actions">
            <button class="primary" type="submit">Save customer</button>
            <button id="partner-sales-new" type="button">New customer</button>
          </div>
          <div class="partner-sales-feedback" id="partner-sales-feedback">Create a customer or select one from the list.</div>
        </form>

        <h2 style="margin-top:22px">Sales workflow</h2>
        <div class="partner-pipeline">
          <div class="partner-pipeline-step"><strong>1</strong><small>Create customer</small></div>
          <div class="partner-pipeline-step"><strong>2</strong><small>Select plan</small></div>
          <div class="partner-pipeline-step"><strong>3</strong><small>Complete onboarding</small></div>
          <div class="partner-pipeline-step"><strong>4</strong><small>Assign Cloud ID</small></div>
        </div>
        <div class="partner-sales-actions">
          <a href="/partner">Partner workspace</a>
          <a href="/onboarding">Customer onboarding</a>
          <a href="/partner/appliance-dashboard">Cloud ID / appliance</a>
        </div>
      </aside>
    </section>'''

    scripts = '''
    <script>
    const salesId=document.getElementById('partner-sales-id');
    const salesName=document.getElementById('partner-sales-name');
    const salesEmail=document.getElementById('partner-sales-email');
    const salesSite=document.getElementById('partner-sales-site');
    const salesStatus=document.getElementById('partner-sales-status');
    const salesFeedback=document.getElementById('partner-sales-feedback');

    function clearSalesForm(){
      salesId.value='';
      salesName.value='';
      salesEmail.value='';
      salesSite.value='Primary site';
      salesStatus.value='trial';
      salesFeedback.className='partner-sales-feedback';
      salesFeedback.textContent='Ready to create a new customer.';
    }

    document.querySelectorAll('.partner-select-customer').forEach(button=>button.onclick=()=>{
      salesId.value=button.dataset.id||'';
      salesName.value=button.dataset.name||'';
      salesEmail.value=button.dataset.email||'';
      salesSite.value=button.dataset.site||'Primary site';
      salesStatus.value=button.dataset.status||'active';
      salesFeedback.className='partner-sales-feedback';
      salesFeedback.textContent='Customer loaded. Update the pipeline status or account details.';
    });
    document.getElementById('partner-sales-new').onclick=clearSalesForm;

    document.getElementById('partner-sales-form').onsubmit=async event=>{
      event.preventDefault();
      salesFeedback.className='partner-sales-feedback';
      salesFeedback.textContent='Saving customer…';
      const payload={
        name:salesName.value,
        email:salesEmail.value,
        site_name:salesSite.value,
        status:salesStatus.value
      };
      try{
        const editing=Boolean(salesId.value);
        const url=editing
          ? `/api/partner/customers/${encodeURIComponent(salesId.value)}`
          : '/api/partner/customers';
        const response=await fetch(url,{
          method:editing?'PUT':'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify(payload)
        });
        const result=await response.json();
        if(!response.ok||result.status==='error')throw new Error(result.detail||result.message||'Could not save customer.');
        salesFeedback.className='partner-sales-feedback success';
        salesFeedback.textContent=result.message||'Customer saved.';
        setTimeout(()=>location.reload(),700);
      }catch(error){
        salesFeedback.className='partner-sales-feedback error';
        salesFeedback.textContent=error.message;
      }
    };
    </script>'''

    return page_shell("Partner sales", "partner-sales", content, scripts)


@app.put("/api/partner/customers/{customer_id}")
def update_partner_customer(
    customer_id: str,
    request: Request,
    customer: PartnerCustomerModel,
) -> dict:
    require_partner_access(request)
    if customer.status not in {"active", "pending_installation", "trial", "suspended", "cancelled"}:
        return {"status": "error", "message": "Unsupported customer status."}
    customers = load_partner_customers()
    existing = next((item for item in customers if item.get("id") == customer_id), None)
    if not existing:
        return {"status": "error", "message": "Partner customer not found."}
    duplicate = next(
        (
            item for item in customers
            if item.get("id") != customer_id
            and str(item.get("email") or "").lower() == customer.email.lower()
        ),
        None,
    )
    if duplicate:
        return {"status": "error", "message": "A customer with this email already exists."}
    existing.update({
        "name": customer.name,
        "email": customer.email,
        "status": customer.status,
        "site_name": customer.site_name,
        "updated_at": datetime.now().isoformat(),
    })
    try:
        save_partner_customers(customers)
    except OSError as error:
        return {"status": "error", "message": f"Could not save customer: {error}"}
    record_audit(
        request,
        "partner.customer_updated",
        f"partner_customer:{customer_id}",
        f"Status set to {customer.status}.",
    )
    return {"status": "complete", "message": "Partner customer updated.", "customer": existing}




PARTNER_INSTALLATION_STATUSES = {
    "not_started",
    "scheduled",
    "in_progress",
    "waiting_customer",
    "ready_for_activation",
    "completed",
    "cancelled",
}


def load_partner_installations() -> list[dict]:
    records = load_json_file(PARTNER_INSTALLATIONS_FILE, [])
    return records if isinstance(records, list) else []


def save_partner_installations(records: list[dict]) -> None:
    save_json_file(PARTNER_INSTALLATIONS_FILE, records[-5000:])


def installation_progress(record: dict) -> int:
    checklist = record.get("checklist") or {}
    required = [
        "plan_verified",
        "deployment_confirmed",
        "cloud_id_assigned",
        "network_ready",
        "camera_entitlement_verified",
        "camera_discovery_completed",
        "customer_handoff_completed",
    ]
    complete = sum(bool(checklist.get(item)) for item in required)
    return round((complete / len(required)) * 100)


@app.get("/partner-quotes", response_class=HTMLResponse)
def partner_quote_builder_page(request: Request) -> Response:
    authorization_response = partner_page_authorization_response(request)
    if authorization_response is not None:
        return authorization_response
    customers = load_partner_customers()
    quotes = load_partner_quotes()
    customer_map = {str(item.get("id")): item for item in customers}

    draft_count = sum(str(item.get("status") or "").lower() == "draft" for item in quotes)
    sent_count = sum(str(item.get("status") or "").lower() == "sent" for item in quotes)
    accepted_count = sum(str(item.get("status") or "").lower() == "accepted" for item in quotes)
    total_pipeline = sum(partner_quote_price(item)["first_payment_estimate_cents"] for item in quotes)

    def badge(value: object) -> str:
        status = str(value or "draft").strip().lower()
        return f'<span class="admin-command-badge {escape(status, quote=True)}">{escape(status.replace("_", " "))}</span>'

    cards = []
    for quote_item in sorted(quotes, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True):
        customer = customer_map.get(str(quote_item.get("customer_id"))) or {}
        totals = partner_quote_price(quote_item)
        quote_json = escape(json.dumps(quote_item), quote=True)
        cards.append(
            f'''<article class="quote-card">
              <div class="quote-head">
                <div>
                  <strong>{escape(str(quote_item.get("quote_name") or "AnyAiCam proposal"))}</strong>
                  <div class="quote-meta">
                    Customer: {escape(str(customer.get("name") or quote_item.get("customer_id") or "Unknown"))}<br>
                    {escape(str(quote_item.get("camera_quantity") or 1))} camera(s)
                    · {escape(str(quote_item.get("resolution") or "2mp").upper())}
                    · {escape(str(quote_item.get("retention_days") or 7))} days
                    · {escape(str(quote_item.get("deployment_type") or "software"))}<br>
                    Monthly: ${totals["monthly_cents"]/100:.2f}
                    · Hardware: ${totals["hardware_total_cents"]/100:.2f}
                  </div>
                </div>
                {badge(quote_item.get("status"))}
              </div>
              <div class="quote-actions">
                <button class="quote-edit" data-quote="{quote_json}" type="button">Edit</button>
                <a href="/partner/quotes/{escape(str(quote_item.get("id") or ""), quote=True)}">Customer view</a>
                <button class="quote-send" data-id="{escape(str(quote_item.get("id") or ""), quote=True)}" type="button">Mark sent</button>
              </div>
            </article>'''
        )

    customer_options = "".join(
        f'<option value="{escape(str(item.get("id") or ""), quote=True)}">{escape(str(item.get("name") or item.get("email") or "Customer"))}</option>'
        for item in customers
    )

    content = f'''<header class="topbar"><div><p class="eyebrow">Partner portal</p><h1>Quote builder</h1></div><span class="pill">No card data collected</span></header>
    <div class="admin-privacy-note"><strong>Sales boundary:</strong> quotes estimate services and hardware. Payment is completed later through secure Stripe Checkout; the partner portal never receives raw card numbers.</div>
    <section class="quote-summary" style="margin-top:16px">
      <article class="quote-stat"><span>Total quotes</span><strong>{len(quotes)}</strong></article>
      <article class="quote-stat"><span>Draft</span><strong>{draft_count}</strong></article>
      <article class="quote-stat"><span>Sent / accepted</span><strong>{sent_count} / {accepted_count}</strong></article>
      <article class="quote-stat"><span>Pipeline estimate</span><strong>${total_pipeline/100:.0f}</strong></article>
    </section>
    <section class="quote-layout">
      <div class="quote-list">{"".join(cards) or '<div class="empty">No partner quotes found.</div>'}</div>
      <aside class="quote-editor">
        <h2>Build a quote</h2>
        <form id="quote-form">
          <input id="quote-id" type="hidden">
          <label>Customer<select id="quote-customer" required>{customer_options}</select></label>
          <label>Quote name<input id="quote-name" value="AnyAiCam proposal" required></label>
          <label>Deployment<select id="quote-deployment"><option value="software">Software only</option><option value="appliance">AnyAiCam appliance</option></select></label>
          <label>Camera licenses<input id="quote-cameras" type="number" min="1" max="128" value="4" required></label>
          <label>Resolution<select id="quote-resolution"><option value="2mp">2MP / 1080p</option><option value="4mp">4MP</option><option value="8mp">8MP / 4K</option></select></label>
          <label>Recording mode<select id="quote-recording"><option value="motion">Motion events</option><option value="continuous">24/7 continuous</option></select></label>
          <label>Retention<select id="quote-retention"><option value="2">2 days</option><option value="7" selected>7 days</option><option value="14">14 days</option><option value="30">30 days</option></select></label>
          <h3>Analytics</h3>
          <div class="quote-analytics">
            <label><input class="quote-analytic" value="smart_motion" type="checkbox"> Smart Motion</label>
            <label><input class="quote-analytic" value="people_counting" type="checkbox"> People Counting</label>
            <label><input class="quote-analytic" value="lpr" type="checkbox"> License Plate Recognition</label>
            <label><input class="quote-analytic" value="ppe_detection" type="checkbox"> PPE Detection</label>
          </div>
          <label>Hardware total<input id="quote-hardware" type="number" min="0" step="0.01" value="0"></label>
          <label>Notes<textarea id="quote-notes"></textarea></label>
          <label>Status<select id="quote-status"><option value="draft">Draft</option><option value="sent">Sent</option><option value="accepted">Accepted</option><option value="declined">Declined</option><option value="expired">Expired</option></select></label>
          <div class="quote-total"><small>Estimated first payment</small><strong id="quote-estimate">$0.00</strong><span id="quote-monthly">$0.00 monthly</span></div>
          <div class="quote-actions"><button class="primary" type="submit">Save quote</button><button id="quote-new" type="button">New quote</button></div>
          <div class="quote-feedback" id="quote-feedback">Choose a customer and configure the proposal.</div>
        </form>
      </aside>
    </section>'''

    scripts = '''
    <script>
    const quoteField=id=>document.getElementById(id);
    const quoteFeedback=quoteField('quote-feedback');
    const quotePricing={
      '2mp':{motion:{2:7.99,7:8.09,14:8.39,30:8.99},continuous:{2:8.99,7:9.79,14:10.89,30:11.99}},
      '4mp':{motion:{2:8.19,7:8.59,14:9.19,30:10.09},continuous:{2:9.49,7:11.09,14:13.29,30:14.79}},
      '8mp':{motion:{2:8.49,7:9.09,14:9.89,30:11.29},continuous:{2:9.99,7:12.39,14:15.69,30:17.59}}
    };
    const quoteAnalytics={smart_motion:1.79,people_counting:10.99,lpr:18.99,ppe_detection:17.99};

    function selectedAnalytics(){return [...document.querySelectorAll('.quote-analytic:checked')].map(item=>item.value)}
    function updateQuoteEstimate(){
      const cameras=Math.max(1,Number(quoteField('quote-cameras').value)||1);
      const resolution=quoteField('quote-resolution').value;
      const recording=quoteField('quote-recording').value;
      const retention=Number(quoteField('quote-retention').value);
      const base=quotePricing[resolution][recording][retention];
      const addons=selectedAnalytics().reduce((sum,item)=>sum+(quoteAnalytics[item]||0),0);
      const monthly=(base+addons)*cameras;
      const hardware=Math.max(0,Number(quoteField('quote-hardware').value)||0);
      quoteField('quote-monthly').textContent='$'+monthly.toFixed(2)+' monthly';
      quoteField('quote-estimate').textContent='$'+(monthly+hardware).toFixed(2);
    }

    function clearQuote(){
      quoteField('quote-id').value='';
      quoteField('quote-name').value='AnyAiCam proposal';
      quoteField('quote-deployment').value='software';
      quoteField('quote-cameras').value='4';
      quoteField('quote-resolution').value='2mp';
      quoteField('quote-recording').value='motion';
      quoteField('quote-retention').value='7';
      quoteField('quote-hardware').value='0';
      quoteField('quote-notes').value='';
      quoteField('quote-status').value='draft';
      document.querySelectorAll('.quote-analytic').forEach(item=>item.checked=false);
      quoteFeedback.className='quote-feedback';
      quoteFeedback.textContent='Ready to create a new quote.';
      updateQuoteEstimate();
    }

    document.querySelectorAll('.quote-edit').forEach(button=>button.onclick=()=>{
      const quote=JSON.parse(button.dataset.quote);
      quoteField('quote-id').value=quote.id||'';
      quoteField('quote-customer').value=quote.customer_id||'';
      quoteField('quote-name').value=quote.quote_name||'AnyAiCam proposal';
      quoteField('quote-deployment').value=quote.deployment_type||'software';
      quoteField('quote-cameras').value=quote.camera_quantity||1;
      quoteField('quote-resolution').value=quote.resolution||'2mp';
      quoteField('quote-recording').value=quote.recording_mode||'motion';
      quoteField('quote-retention').value=quote.retention_days||7;
      quoteField('quote-hardware').value=((quote.hardware_items||[]).reduce((sum,item)=>sum+(Number(item.quantity||0)*Number(item.unit_price_cents||0)),0)/100).toFixed(2);
      quoteField('quote-notes').value=quote.notes||'';
      quoteField('quote-status').value=quote.status||'draft';
      document.querySelectorAll('.quote-analytic').forEach(item=>item.checked=(quote.analytics||[]).includes(item.value));
      quoteFeedback.textContent='Quote loaded for editing.';
      updateQuoteEstimate();
    });

    document.querySelectorAll('.quote-send').forEach(button=>button.onclick=async()=>{
      button.disabled=true;
      try{
        const response=await fetch(`/api/partner/quotes/${encodeURIComponent(button.dataset.id)}`,{
          method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'sent'})
        });
        const result=await response.json();
        if(!response.ok)throw new Error(result.detail||result.message||'Could not mark quote as sent.');
        location.reload();
      }catch(error){showToast(error.message);button.disabled=false}
    });

    document.querySelectorAll('#quote-form input,#quote-form select').forEach(item=>item.addEventListener('input',updateQuoteEstimate));
    quoteField('quote-new').onclick=clearQuote;
    quoteField('quote-form').onsubmit=async event=>{
      event.preventDefault();
      const hardwareAmount=Math.max(0,Number(quoteField('quote-hardware').value)||0);
      const payload={
        customer_id:quoteField('quote-customer').value,
        quote_name:quoteField('quote-name').value,
        deployment_type:quoteField('quote-deployment').value,
        camera_quantity:Number(quoteField('quote-cameras').value),
        resolution:quoteField('quote-resolution').value,
        recording_mode:quoteField('quote-recording').value,
        retention_days:Number(quoteField('quote-retention').value),
        analytics:selectedAnalytics(),
        hardware_items:hardwareAmount?[{name:'Hardware package',quantity:1,unit_price_cents:Math.round(hardwareAmount*100)}]:[],
        notes:quoteField('quote-notes').value,
        status:quoteField('quote-status').value
      };
      quoteFeedback.className='quote-feedback';quoteFeedback.textContent='Saving quote…';
      try{
        const editing=Boolean(quoteField('quote-id').value);
        const url=editing?`/api/partner/quotes/${encodeURIComponent(quoteField('quote-id').value)}`:'/api/partner/quotes';
        const response=await fetch(url,{method:editing?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        const result=await response.json();
        if(!response.ok)throw new Error(result.detail||result.message||'Could not save quote.');
        quoteFeedback.className='quote-feedback success';quoteFeedback.textContent=result.message||'Quote saved.';
        setTimeout(()=>location.reload(),700);
      }catch(error){quoteFeedback.className='quote-feedback error';quoteFeedback.textContent=error.message}
    };
    updateQuoteEstimate();
    </script>'''

    return page_shell("Partner quotes", "partner-quotes", content, scripts)


@app.get("/partner/quotes/{quote_id}", response_class=HTMLResponse)
def partner_customer_quote_page(quote_id: str, request: Request) -> Response:
    authorization_response = partner_page_authorization_response(request)
    if authorization_response is not None:
        return authorization_response
    quote_item = next((item for item in load_partner_quotes() if item.get("id") == quote_id), None)
    if not quote_item:
        return page_shell("Quote not found", "partner-quotes", '<div class="empty">Quote not found.</div>')
    customer = next((item for item in load_partner_customers() if item.get("id") == quote_item.get("customer_id")), {})
    totals = partner_quote_price(quote_item)
    content = f'''<header class="topbar"><div><p class="eyebrow">Customer proposal</p><h1>{escape(str(quote_item.get("quote_name") or "AnyAiCam proposal"))}</h1></div></header>
    <section class="panel">
      <h2>{escape(str(customer.get("name") or "Customer"))}</h2>
      <p>{escape(str(quote_item.get("camera_quantity") or 1))} camera license(s), {escape(str(quote_item.get("resolution") or "2mp").upper())}, {escape(str(quote_item.get("recording_mode") or "motion"))}, {escape(str(quote_item.get("retention_days") or 7))} days retention.</p>
      <p><b>Analytics:</b> {escape(", ".join(quote_item.get("analytics") or []) or "None")}</p>
      <p><b>Estimated monthly service:</b> ${totals["monthly_cents"]/100:.2f}</p>
      <p><b>Estimated hardware:</b> ${totals["hardware_total_cents"]/100:.2f}</p>
      <p><b>Estimated first payment:</b> ${totals["first_payment_estimate_cents"]/100:.2f}</p>
      <p>{escape(str(quote_item.get("notes") or ""))}</p>
      <div class="admin-privacy-note">Payment is completed through secure Stripe Checkout after the customer accepts the proposal. AnyAiCam and the partner portal do not store raw card details.</div>
      <div class="quote-actions"><a href="/partner-quotes">Back to quotes</a><a href="/partner">Continue onboarding</a></div>
    </section>'''
    return page_shell("Customer quote", "partner-quotes", content)


@app.post("/api/partner/quotes")
def create_partner_quote(request: Request, payload: PartnerQuoteCreateModel) -> dict:
    require_partner_access(request)
    customers = load_partner_customers()
    if not any(item.get("id") == payload.customer_id for item in customers):
        return {"status": "error", "message": "Partner customer not found."}
    if payload.status not in {"draft", "sent", "accepted", "declined", "expired"}:
        return {"status": "error", "message": "Unsupported quote status."}
    quote_item = {
        "id": uuid.uuid4().hex[:12],
        **payload.model_dump(),
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    quote_item["pricing"] = partner_quote_price(quote_item)
    quotes = load_partner_quotes()
    quotes.append(quote_item)
    save_partner_quotes(quotes)
    record_audit(request, "partner.quote_created", f"partner_quote:{quote_item['id']}", payload.quote_name)
    return {"status": "complete", "message": "Partner quote created.", "quote": quote_item}


@app.put("/api/partner/quotes/{quote_id}")
def update_partner_quote(quote_id: str, request: Request, payload: PartnerQuoteUpdateModel) -> dict:
    require_partner_access(request)
    quotes = load_partner_quotes()
    quote_item = next((item for item in quotes if item.get("id") == quote_id), None)
    if not quote_item:
        return {"status": "error", "message": "Partner quote not found."}
    updates = payload.model_dump(exclude_none=True)
    if updates.get("status") and updates["status"] not in {"draft", "sent", "accepted", "declined", "expired"}:
        return {"status": "error", "message": "Unsupported quote status."}
    quote_item.update(updates)
    quote_item["updated_at"] = datetime.now().isoformat()
    quote_item["pricing"] = partner_quote_price(quote_item)
    save_partner_quotes(quotes)
    record_audit(request, "partner.quote_updated", f"partner_quote:{quote_id}", f"Status: {quote_item.get('status')}")
    return {"status": "complete", "message": "Partner quote updated.", "quote": quote_item}



@app.get("/partner-installations", response_class=HTMLResponse)
def partner_installations_page(request: Request) -> Response:
    authorization_response = partner_page_authorization_response(request)
    if authorization_response is not None:
        return authorization_response

    customers = load_partner_customers()
    quotes = load_partner_quotes()
    installations = load_partner_installations()
    customer_map = {str(item.get("id")): item for item in customers}
    quote_map = {str(item.get("id")): item for item in quotes}

    not_started = sum(item.get("status") == "not_started" for item in installations)
    scheduled = sum(item.get("status") == "scheduled" for item in installations)
    active = sum(item.get("status") in {"in_progress", "waiting_customer"} for item in installations)
    ready = sum(item.get("status") == "ready_for_activation" for item in installations)
    complete = sum(item.get("status") == "completed" for item in installations)

    def badge(value: object) -> str:
        status = str(value or "not_started").strip().lower()
        return f'<span class="admin-command-badge {escape(status, quote=True)}">{escape(status.replace("_", " "))}</span>'

    cards = []
    for record in sorted(
        installations,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    ):
        customer = customer_map.get(str(record.get("customer_id"))) or {}
        quote_item = quote_map.get(str(record.get("quote_id"))) or {}
        progress = installation_progress(record)
        record_json = escape(json.dumps(record), quote=True)
        cards.append(
            f'''<article class="install-card">
              <div class="install-head">
                <div>
                  <strong>{escape(str(customer.get("name") or record.get("customer_id") or "Customer"))}</strong>
                  <div class="install-meta">
                    Installer: {escape(str(record.get("installer_name") or "Unassigned"))}<br>
                    Deployment: {escape(str(record.get("deployment_type") or "software"))}
                    · Cameras: {escape(str(record.get("expected_camera_count") or 1))}
                    · Cloud ID: {escape(str(record.get("cloud_id") or "Not assigned"))}<br>
                    Quote: {escape(str(quote_item.get("quote_name") or record.get("quote_id") or "None"))}
                  </div>
                </div>
                {badge(record.get("status"))}
              </div>
              <div class="install-progress"><span style="width:{progress}%"></span></div>
              <div class="install-actions">
                <button class="install-edit" data-record="{record_json}" type="button">Manage installation</button>
                <a href="/partner/appliance-dashboard">Cloud ID / appliance</a>
                <a href="/partner-sales">Customer record</a>
              </div>
            </article>'''
        )

    customer_options = "".join(
        f'<option value="{escape(str(item.get("id") or ""), quote=True)}">{escape(str(item.get("name") or item.get("email") or "Customer"))}</option>'
        for item in customers
    )
    quote_options = '<option value="">No quote selected</option>' + "".join(
        f'<option value="{escape(str(item.get("id") or ""), quote=True)}">{escape(str(item.get("quote_name") or item.get("id") or "Quote"))}</option>'
        for item in quotes
    )

    content = f'''<header class="topbar"><div><p class="eyebrow">Partner portal</p><h1>Installation & activation handoff</h1></div><span class="pill">No camera credentials stored</span></header>
    <div class="admin-privacy-note"><strong>Installation boundary:</strong> this workflow tracks readiness and handoff. Camera usernames and passwords must remain in the customer-authorized discovery process and are not stored here.</div>
    <section class="install-summary" style="margin-top:16px">
      <article class="install-stat"><span>Not started</span><strong>{not_started}</strong></article>
      <article class="install-stat"><span>Scheduled</span><strong>{scheduled}</strong></article>
      <article class="install-stat"><span>In progress</span><strong>{active}</strong></article>
      <article class="install-stat"><span>Ready to activate</span><strong>{ready}</strong></article>
      <article class="install-stat"><span>Completed</span><strong>{complete}</strong></article>
    </section>
    <section class="install-layout">
      <div class="install-list">{"".join(cards) or '<div class="empty">No installation records found.</div>'}</div>
      <aside class="install-editor">
        <h2>Installation record</h2>
        <form id="install-form">
          <input id="install-id" type="hidden">
          <label>Customer<select id="install-customer" required>{customer_options}</select></label>
          <label>Quote<select id="install-quote">{quote_options}</select></label>
          <label>Installer<input id="install-installer"></label>
          <label>Deployment<select id="install-deployment"><option value="software">Software only</option><option value="appliance">AnyAiCam appliance</option></select></label>
          <label>Cloud ID<input id="install-cloud-id" placeholder="Assigned appliance or software Cloud ID"></label>
          <label>Expected cameras<input id="install-camera-count" type="number" min="1" max="128" value="4"></label>
          <label>Status
            <select id="install-status">
              <option value="not_started">Not started</option>
              <option value="scheduled">Scheduled</option>
              <option value="in_progress">In progress</option>
              <option value="waiting_customer">Waiting on customer</option>
              <option value="ready_for_activation">Ready for activation</option>
              <option value="completed">Completed</option>
              <option value="cancelled">Cancelled</option>
            </select>
          </label>
          <h3>Checklist</h3>
          <div class="install-checklist">
            <label class="install-check"><input class="install-check-value" data-key="plan_verified" type="checkbox"> Paid plan and license entitlement verified</label>
            <label class="install-check"><input class="install-check-value" data-key="deployment_confirmed" type="checkbox"> Software or appliance deployment confirmed</label>
            <label class="install-check"><input class="install-check-value" data-key="cloud_id_assigned" type="checkbox"> Cloud ID assigned and checked</label>
            <label class="install-check"><input class="install-check-value" data-key="network_ready" type="checkbox"> Customer network ready</label>
            <label class="install-check"><input class="install-check-value" data-key="camera_entitlement_verified" type="checkbox"> Camera count and analytics entitlement verified</label>
            <label class="install-check"><input class="install-check-value" data-key="camera_discovery_completed" type="checkbox"> Customer-authorized camera discovery completed</label>
            <label class="install-check"><input class="install-check-value" data-key="customer_handoff_completed" type="checkbox"> Customer handoff and login test completed</label>
          </div>
          <label>Notes<textarea id="install-notes"></textarea></label>
          <div class="install-actions"><button class="primary" type="submit">Save installation</button><button id="install-new" type="button">New record</button></div>
          <div class="install-feedback" id="install-feedback">Create or select an installation record.</div>
        </form>
      </aside>
    </section>'''

    scripts = '''
    <script>
    const installField=id=>document.getElementById(id);
    const installFeedback=installField('install-feedback');

    function checklistPayload(){
      const result={};
      document.querySelectorAll('.install-check-value').forEach(item=>result[item.dataset.key]=item.checked);
      return result;
    }

    function clearInstallation(){
      installField('install-id').value='';
      installField('install-quote').value='';
      installField('install-installer').value='';
      installField('install-deployment').value='software';
      installField('install-cloud-id').value='';
      installField('install-camera-count').value='4';
      installField('install-status').value='not_started';
      installField('install-notes').value='';
      document.querySelectorAll('.install-check-value').forEach(item=>item.checked=false);
      installFeedback.className='install-feedback';
      installFeedback.textContent='Ready to create a new installation record.';
    }

    document.querySelectorAll('.install-edit').forEach(button=>button.onclick=()=>{
      const record=JSON.parse(button.dataset.record);
      installField('install-id').value=record.id||'';
      installField('install-customer').value=record.customer_id||'';
      installField('install-quote').value=record.quote_id||'';
      installField('install-installer').value=record.installer_name||'';
      installField('install-deployment').value=record.deployment_type||'software';
      installField('install-cloud-id').value=record.cloud_id||'';
      installField('install-camera-count').value=record.expected_camera_count||1;
      installField('install-status').value=record.status||'not_started';
      installField('install-notes').value=record.notes||'';
      document.querySelectorAll('.install-check-value').forEach(item=>item.checked=Boolean((record.checklist||{})[item.dataset.key]));
      installFeedback.className='install-feedback';
      installFeedback.textContent='Installation loaded for editing.';
    });

    installField('install-new').onclick=clearInstallation;
    installField('install-form').onsubmit=async event=>{
      event.preventDefault();
      const payload={
        customer_id:installField('install-customer').value,
        quote_id:installField('install-quote').value,
        installer_name:installField('install-installer').value,
        deployment_type:installField('install-deployment').value,
        cloud_id:installField('install-cloud-id').value,
        expected_camera_count:Number(installField('install-camera-count').value),
        checklist:checklistPayload(),
        notes:installField('install-notes').value,
        status:installField('install-status').value
      };
      installFeedback.className='install-feedback';
      installFeedback.textContent='Saving installation…';
      try{
        const editing=Boolean(installField('install-id').value);
        const url=editing?`/api/partner/installations/${encodeURIComponent(installField('install-id').value)}`:'/api/partner/installations';
        const response=await fetch(url,{
          method:editing?'PUT':'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify(payload)
        });
        const result=await response.json();
        if(!response.ok||result.status==='error')throw new Error(result.detail||result.message||'Could not save installation.');
        installFeedback.className='install-feedback success';
        installFeedback.textContent=result.message||'Installation saved.';
        setTimeout(()=>location.reload(),700);
      }catch(error){
        installFeedback.className='install-feedback error';
        installFeedback.textContent=error.message;
      }
    };
    </script>'''

    return page_shell("Partner installations", "partner-install", content, scripts)


@app.post("/api/partner/installations")
def create_partner_installation(request: Request, payload: PartnerInstallationCreateModel) -> dict:
    require_partner_access(request)
    if payload.status not in PARTNER_INSTALLATION_STATUSES:
        return {"status": "error", "message": "Unsupported installation status."}
    if not any(item.get("id") == payload.customer_id for item in load_partner_customers()):
        return {"status": "error", "message": "Partner customer not found."}
    record = {
        "id": uuid.uuid4().hex[:12],
        **payload.model_dump(),
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    records = load_partner_installations()
    records.append(record)
    save_partner_installations(records)
    record_audit(request, "partner.installation_created", f"partner_installation:{record['id']}", f"Customer: {payload.customer_id}")
    return {"status": "complete", "message": "Installation record created.", "installation": record}


@app.put("/api/partner/installations/{installation_id}")
def update_partner_installation(
    installation_id: str,
    request: Request,
    payload: PartnerInstallationUpdateModel,
) -> dict:
    require_partner_access(request)
    records = load_partner_installations()
    record = next((item for item in records if item.get("id") == installation_id), None)
    if not record:
        return {"status": "error", "message": "Installation record not found."}
    updates = payload.model_dump(exclude_none=True)
    if updates.get("status") and updates["status"] not in PARTNER_INSTALLATION_STATUSES:
        return {"status": "error", "message": "Unsupported installation status."}
    record.update(updates)
    record["updated_at"] = datetime.now().isoformat()
    save_partner_installations(records)
    record_audit(request, "partner.installation_updated", f"partner_installation:{installation_id}", f"Status: {record.get('status')}")
    return {"status": "complete", "message": "Installation record updated.", "installation": record}



@app.get("/partner-performance", response_class=HTMLResponse)
def partner_performance_center(request: Request) -> Response:
    authorization_response = partner_page_authorization_response(request)
    if authorization_response is not None:
        return authorization_response

    customers = load_partner_customers()
    quotes = load_partner_quotes()
    installations = load_partner_installations()
    customer_map = {str(item.get("id")): item for item in customers}

    accepted_quotes = [
        item for item in quotes
        if str(item.get("status") or "").lower() == "accepted"
    ]
    sent_quotes = [
        item for item in quotes
        if str(item.get("status") or "").lower() == "sent"
    ]
    completed_installations = [
        item for item in installations
        if str(item.get("status") or "").lower() == "completed"
    ]
    active_installations = [
        item for item in installations
        if str(item.get("status") or "").lower()
        in {"scheduled", "in_progress", "waiting_customer", "ready_for_activation"}
    ]

    accepted_monthly_cents = sum(
        partner_quote_price(item)["monthly_cents"] for item in accepted_quotes
    )
    accepted_hardware_cents = sum(
        partner_quote_price(item)["hardware_total_cents"] for item in accepted_quotes
    )
    estimated_commission_cents = round(
        (accepted_monthly_cents + accepted_hardware_cents)
        * (PARTNER_COMMISSION_PERCENT / 100)
    )
    conversion_rate = (
        round((len(accepted_quotes) / max(1, len(quotes))) * 100)
        if quotes else 0
    )
    completion_rate = (
        round((len(completed_installations) / max(1, len(installations))) * 100)
        if installations else 0
    )

    customer_rows = []
    for customer in sorted(customers, key=lambda item: str(item.get("created_at") or ""), reverse=True):
        customer_id = str(customer.get("id") or "")
        customer_quotes = [item for item in quotes if str(item.get("customer_id") or "") == customer_id]
        customer_installations = [
            item for item in installations if str(item.get("customer_id") or "") == customer_id
        ]
        accepted_value = sum(
            partner_quote_price(item)["first_payment_estimate_cents"]
            for item in customer_quotes
            if str(item.get("status") or "").lower() == "accepted"
        )
        latest_installation = (
            sorted(
                customer_installations,
                key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
                reverse=True,
            )[0]
            if customer_installations else {}
        )
        customer_rows.append(
            f'''<div class="partner-performance-row">
              <div>
                <strong>{escape(str(customer.get("name") or "Customer"))}</strong>
                <small>{escape(str(customer.get("email") or "No email"))}<br>
                Quotes: {len(customer_quotes)} · Installations: {len(customer_installations)}
                · Status: {escape(str(customer.get("status") or "active").replace("_", " "))}</small>
              </div>
              <div><strong>${accepted_value/100:.2f}</strong><small>Accepted value</small></div>
              <div><strong>{escape(str(latest_installation.get("status") or "Not started").replace("_", " "))}</strong><small>Installation</small></div>
            </div>'''
        )

    quote_rows = []
    for quote_item in sorted(
        quotes,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )[:10]:
        totals = partner_quote_price(quote_item)
        customer = customer_map.get(str(quote_item.get("customer_id"))) or {}
        status = str(quote_item.get("status") or "draft")
        quote_rows.append(
            f'''<div class="partner-performance-row">
              <div><strong>{escape(str(quote_item.get("quote_name") or "AnyAiCam proposal"))}</strong>
              <small>{escape(str(customer.get("name") or quote_item.get("customer_id") or "Customer"))}
              · {escape(status.replace("_", " "))}</small></div>
              <div><strong>${totals["monthly_cents"]/100:.2f}</strong><small>Monthly</small></div>
              <div><strong>${totals["hardware_total_cents"]/100:.2f}</strong><small>Hardware</small></div>
            </div>'''
        )

    content = f'''<header class="topbar"><div><p class="eyebrow">Partner portal</p><h1>Performance & commission center</h1></div><span class="pill">Estimated only</span></header>
    <div class="partner-performance-note"><strong>Commission boundary:</strong> values shown here are planning estimates based on accepted quotes and the configured {PARTNER_COMMISSION_PERCENT:.1f}% estimate rate. This page does not issue payouts, modify Stripe, or expose administrator-only financial controls.</div>
    <section class="partner-performance-summary" style="margin-top:16px">
      <article class="partner-performance-stat"><span>Customers</span><strong>{len(customers)}</strong></article>
      <article class="partner-performance-stat"><span>Quotes sent</span><strong>{len(sent_quotes)}</strong></article>
      <article class="partner-performance-stat"><span>Quotes accepted</span><strong>{len(accepted_quotes)}</strong></article>
      <article class="partner-performance-stat"><span>Conversion</span><strong>{conversion_rate}%</strong></article>
      <article class="partner-performance-stat"><span>Active installs</span><strong>{len(active_installations)}</strong></article>
      <article class="partner-performance-stat"><span>Completed installs</span><strong>{len(completed_installations)}</strong></article>
    </section>
    <section class="partner-performance-grid">
      <div style="display:grid;gap:14px">
        <article class="partner-performance-card">
          <h2>Customer pipeline</h2>
          <div class="partner-performance-list">{"".join(customer_rows) or '<div class="empty">No partner customers found.</div>'}</div>
        </article>
        <article class="partner-performance-card">
          <h2>Recent quote performance</h2>
          <div class="partner-performance-list">{"".join(quote_rows) or '<div class="empty">No partner quotes found.</div>'}</div>
        </article>
      </div>
      <aside style="display:grid;gap:14px">
        <article class="partner-performance-card">
          <h2>Revenue estimate</h2>
          <p><b>Accepted monthly service:</b> ${accepted_monthly_cents/100:.2f}</p>
          <p><b>Accepted hardware:</b> ${accepted_hardware_cents/100:.2f}</p>
          <p><b>Estimated commission:</b> ${estimated_commission_cents/100:.2f}</p>
          <div class="partner-performance-bar"><span style="width:{conversion_rate}%"></span></div>
          <small class="health-detail">Quote conversion: {conversion_rate}%</small>
        </article>
        <article class="partner-performance-card">
          <h2>Installation completion</h2>
          <div class="partner-performance-bar"><span style="width:{completion_rate}%"></span></div>
          <p><b>{completion_rate}%</b> of installation records are complete.</p>
          <p class="health-detail">Completed: {len(completed_installations)} · Active: {len(active_installations)} · Total: {len(installations)}</p>
        </article>
        <article class="partner-performance-card">
          <h2>Partner tools</h2>
          <div class="partner-performance-actions">
            <a href="/partner-sales">Customer pipeline</a>
            <a href="/partner-quotes">Quote builder</a>
            <a href="/partner-installations">Installations</a>
            <a href="/partner/appliance-dashboard">Cloud ID / appliances</a>
          </div>
        </article>
      </aside>
    </section>'''

    return page_shell(
        "Partner performance",
        "partner-performance",
        content,
    )


@app.get("/partner", response_class=HTMLResponse)
def partner_portal(request: Request):
    return render_partner_workspace(request, page_shell)
    customers = load_partner_customers()
    customer_rows = []
    for customer in customers:
        searchable = f'{customer.get("name", "")} {customer.get("email", "")} {customer.get("id", "")}'.lower()
        customer_rows.append(f'<article class="customer-row" data-customer-status="{escape(customer.get("status", "active"))}" data-search="{escape(searchable, quote=True)}"><div><strong>{escape(customer.get("name", "Unnamed customer"))}</strong><br><small>{escape(customer.get("site_name", "Primary site"))}</small></div><div>{escape(customer.get("email", ""))}<br><small>ID: {escape(customer.get("id", ""))}</small></div><div><span class="pill">{escape(customer.get("status", "active").title())}</span></div><div><a class="download" href="/sites">Manage site</a></div></article>')
    customer_body = "".join(customer_rows) or '<div class="empty" id="no-customers">No active customers found.</div>'
    active_count = sum(1 for customer in customers if customer.get("status") == "active")
    tabs = [("getting-started", "Getting started"), ("partner-details", "Partner details"), ("customers", "Customers"), ("materials", "Materials"), ("pricing", "Pricing"), ("adapters", "Cloud adapters")]
    tab_buttons = "".join(f'<button class="portal-tab {"active" if key == "customers" else ""}" data-portal-tab="{key}">{label}</button>' for key, label in tabs)
    content = f"""<header class="topbar"><div><p class="eyebrow">Business management</p><h1>Partner portal</h1></div><span class="pill">Local partner workspace</span></header><nav class="portal-tabs" aria-label="Partner portal">{tab_buttons}</nav><section class="portal-workspace"><div class="portal-panel" data-portal-panel="getting-started" hidden><h2>Getting started</h2><div class="feature-grid" style="margin-top:20px"><article class="feature-card"><div class="feature-icon">1</div><h2>Add a customer</h2><p>Create a customer record and assign their primary site.</p></article><article class="feature-card"><div class="feature-icon">2</div><h2>Connect cameras</h2><p>Use the Sites and Cameras settings to manage customer equipment.</p></article><article class="feature-card"><div class="feature-icon">3</div><h2>Monitor health</h2><p>Use Dashboard and Alerts to track customer systems.</p></article></div></div><div class="portal-panel" data-portal-panel="partner-details" hidden><h2>Partner details</h2><div class="settings-list" style="margin-top:20px"><div class="setting-link"><div><strong>Partner name</strong><div class="health-detail">AnyAiCam local partner</div></div></div><div class="setting-link"><div><strong>Portal type</strong><div class="health-detail">Self-hosted VMS management</div></div></div></div></div><div class="portal-panel" data-portal-panel="customers"><div class="portal-actions"><h2><span id="customer-count">{active_count}</span> customers</h2><button class="action-button" id="add-customer">Add new customer</button></div><div class="portal-search-row"><input class="portal-search" id="customer-search" placeholder="Search by name, email, or customer ID…"><div class="status-filters"><label><input type="radio" name="customer-status" value="active" checked> Active</label><label><input type="radio" name="customer-status" value="cancelled"> Cancelled</label><label><input type="radio" name="customer-status" value="all"> All</label></div></div><div class="customer-list" id="customer-list">{customer_body}</div><div class="empty" id="customer-no-results" hidden>No customers match this search.</div></div><div class="portal-panel" data-portal-panel="materials" hidden><h2>Sales and support materials</h2><div class="feature-grid" style="margin-top:20px"><article class="feature-card"><div class="feature-icon">▤</div><h2>Installation guide</h2><p>Deployment documentation will be stored here.</p><span class="coming">Coming soon</span></article><article class="feature-card"><div class="feature-icon">▧</div><h2>Brand assets</h2><p>Logos and customer-facing materials.</p><span class="coming">Coming soon</span></article></div></div><div class="portal-panel" data-portal-panel="pricing" hidden><h2>Pricing</h2><div class="empty-stage">Partner pricing and service plans will appear here.</div></div><div class="portal-panel" data-portal-panel="adapters" hidden><h2>Cloud adapters</h2><div class="customer-list"><article class="customer-row"><div><strong>AnyAiCam VMS</strong><br><small>Local software adapter</small></div><div>Docker container<br><small>Port 8000</small></div><div><span class="pill">Connected</span></div><div><a class="download" href="/dashboard">View health</a></div></article></div></div></section><dialog class="partner-dialog" id="customer-dialog"><div class="dialog-body"><div class="panel-head"><h2>Add new customer</h2><button class="ghost-button" type="button" id="close-customer-dialog">Close</button></div><form class="dialog-form" id="customer-form"><label>Customer name<input id="new-customer-name" required minlength="2"></label><label>Email<input id="new-customer-email" type="email" required></label><label>Primary site<input id="new-customer-site" value="Primary site" required></label><label>Status<select id="new-customer-status"><option value="active">Active</option><option value="cancelled">Cancelled</option></select></label><div class="dialog-actions"><button class="action-button" type="submit">Add customer</button></div></form></div></dialog>"""
    scripts = """<script>const portalTabs=document.querySelectorAll('[data-portal-tab]'),portalPanels=document.querySelectorAll('[data-portal-panel]');portalTabs.forEach(tab=>tab.addEventListener('click',()=>{portalTabs.forEach(item=>item.classList.remove('active'));portalPanels.forEach(panel=>panel.hidden=panel.dataset.portalPanel!==tab.dataset.portalTab);tab.classList.add('active')}));const dialog=document.getElementById('customer-dialog');document.getElementById('add-customer').addEventListener('click',()=>dialog.showModal());document.getElementById('close-customer-dialog').addEventListener('click',()=>dialog.close());const search=document.getElementById('customer-search'),rows=[...document.querySelectorAll('[data-customer-status]')],noResults=document.getElementById('customer-no-results');function filterCustomers(){const status=document.querySelector('[name=customer-status]:checked').value,query=search.value.trim().toLowerCase();let visible=0;rows.forEach(row=>{const matchStatus=status==='all'||row.dataset.customerStatus===status,matchSearch=query.length<3||row.dataset.search.includes(query),show=matchStatus&&matchSearch;row.hidden=!show;if(show)visible++});noResults.hidden=visible>0;document.getElementById('customer-count').textContent=visible}search.addEventListener('input',filterCustomers);document.querySelectorAll('[name=customer-status]').forEach(input=>input.addEventListener('change',filterCustomers));document.getElementById('customer-form').addEventListener('submit',async event=>{event.preventDefault();const payload={name:document.getElementById('new-customer-name').value,email:document.getElementById('new-customer-email').value,site_name:document.getElementById('new-customer-site').value,status:document.getElementById('new-customer-status').value},response=await fetch('/api/partner/customers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=await response.json();if(result.status==='complete'){showToast(result.message);setTimeout(()=>location.reload(),700)}else{showToast(result.message)}});filterCustomers();</script>"""
    return page_shell("Partner portal", "partner", content, scripts)


def recording_details(clip: Path, camera_number: int) -> tuple[str, str]:
    prefix = f"camera{camera_number}_"
    timestamp = clip.stem.removeprefix(prefix)
    try:
        recorded_at = datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S")
        label = recorded_at.strftime("%b %d, %Y · %I:%M %p")
    except ValueError:
        label = timestamp.replace("_", " ")
    size_mb = clip.stat().st_size / (1024 * 1024)
    return label, f"{size_mb:.1f} MB · 5-minute segment"



@app.get("/playback", response_class=HTMLResponse)
def playback() -> str:
    camera_numbers = list(range(1, CAMERA_COUNT + 1))
    today = datetime.now().strftime("%Y-%m-%d")

    recordings_by_camera: dict[int, list[dict]] = {camera: [] for camera in camera_numbers}
    for camera in camera_numbers:
        folder = RECORDINGS_FOLDER / f"camera{camera}"
        for clip in sorted(folder.glob("*.mkv"), key=lambda item: item.stat().st_mtime):
            started = recording_start(clip, camera)
            if not started:
                continue
            recordings_by_camera[camera].append({
                "start": started.isoformat(),
                "end": (started + timedelta(minutes=5)).isoformat(),
                "url": f"/recordings/camera{camera}/{quote(clip.name)}",
                "name": clip.name,
            })

    normalized_events: list[dict] = []
    for event in load_motion_events() + analytics_events():
        try:
            camera = int(event.get("camera") or event.get("camera_id") or 0)
        except (TypeError, ValueError):
            continue
        if camera not in recordings_by_camera:
            continue
        timestamp = event.get("start_time") or event.get("timestamp") or event.get("event_timestamp")
        if not timestamp:
            continue
        event_type = str(event.get("event_type") or "motion").lower()
        if event_type in {"car", "truck", "bus", "motorcycle", "bicycle"}:
            display_type, filter_type = event_type, "vehicle"
        elif event_type in {"plate", "license_plate"}:
            display_type, filter_type = "lpr", "lpr"
        elif event_type in {"people_count", "people-counting"}:
            display_type, filter_type = "people_counting", "people_counting"
        else:
            display_type, filter_type = event_type, event_type
        normalized_events.append({
            "id": str(event.get("id") or uuid.uuid4().hex[:10]),
            "camera": camera,
            "timestamp": str(timestamp),
            "end_time": str(event.get("end_time") or timestamp),
            "event_type": display_type,
            "filter_type": filter_type,
            "thumbnail": str(event.get("thumbnail") or ""),
            "recording": str(event.get("linked_recording") or ""),
            "confidence": event.get("confidence", event.get("score")),
        })

    monitor_data = json.dumps({
        "today": today,
        "cameras": camera_numbers,
        "recordings": recordings_by_camera,
        "events": normalized_events,
    })

    camera_buttons = "".join(
        f'<button class="monitor-camera-item active" data-camera="{camera}"><span class="monitor-camera-dot" id="monitor-dot-{camera}"></span><span>Camera {camera}</span></button>'
        for camera in camera_numbers
    )
    tiles = "".join(
        f'<article class="monitor-tile{" active" if camera == 1 else ""}" data-monitor-camera="{camera}"><div class="monitor-video"><video id="monitor-video-{camera}" muted playsinline></video><span class="monitor-video-label">Camera {camera}</span></div></article>'
        for camera in camera_numbers
    )
    camera_options = "".join(f'<option value="{camera}">Camera {camera}</option>' for camera in camera_numbers)
    timeline_rows = "".join(
        f'<div class="timeline-row" data-static-camera="{camera}">'
        f'<div class="timeline-camera-name">Camera {camera}</div>'
        f'<div class="timeline-lane"><div class="timeline-cursor" style="left:50%"></div></div>'
        f'</div>'
        for camera in camera_numbers
    )

    content = f'''<style id="playback-layout-fix">
    /* Playback-only layout correction. Keeps video, controls, and timeline
       in separate document sections instead of forcing them into one viewport. */
    .monitor-shell {{
      display:grid!important;
      grid-template-columns:210px minmax(0,1fr)!important;
      gap:18px!important;
      align-items:start!important;
    }}
    .monitor-picker {{
      position:sticky!important;
      top:16px!important;
      width:auto!important;
      padding:14px!important;
    }}
    .monitor-work {{
      display:block!important;
      height:auto!important;
      min-height:0!important;
      max-height:none!important;
      overflow:visible!important;
      min-width:0!important;
    }}
    .monitor-toolbar {{
      display:flex!important;
      align-items:center!important;
      justify-content:space-between!important;
      gap:10px!important;
      flex-wrap:wrap!important;
      margin-bottom:12px!important;
    }}
    .monitor-toolbar-group {{
      display:flex!important;
      align-items:center!important;
      gap:7px!important;
      flex-wrap:wrap!important;
    }}
    .monitor-toolbar button,
    .monitor-toolbar select,
    .monitor-toolbar input {{
      min-height:38px!important;
      height:38px!important;
      padding:0 11px!important;
      white-space:nowrap!important;
    }}
    .monitor-grid {{
      display:grid!important;
      height:auto!important;
      min-height:0!important;
      max-height:none!important;
      overflow:visible!important;
      gap:12px!important;
      grid-auto-rows:auto!important;
    }}
    .monitor-grid[data-layout="1"] {{
      grid-template-columns:minmax(0,1fr)!important;
    }}
    .monitor-grid[data-layout="4"] {{
      grid-template-columns:repeat(2,minmax(0,1fr))!important;
    }}
    .monitor-grid[data-layout="9"] {{
      grid-template-columns:repeat(3,minmax(0,1fr))!important;
    }}
    .monitor-grid[data-layout="16"] {{
      grid-template-columns:repeat(4,minmax(0,1fr))!important;
    }}
    .monitor-tile {{
      min-height:0!important;
      height:auto!important;
    }}
    .monitor-video {{
      width:100%!important;
      height:auto!important;
      min-height:0!important;
      aspect-ratio:16/9!important;
    }}
    .monitor-timeline {{
      display:block!important;
      position:relative!important;
      margin-top:18px!important;
      padding:16px!important;
      height:auto!important;
      min-height:330px!important;
      max-height:none!important;
      flex:none!important;
      overflow-x:auto!important;
      overflow-y:visible!important;
      z-index:1!important;
    }}
    .monitor-timeline > .monitor-toolbar {{
      padding-bottom:10px!important;
      border-bottom:1px solid rgba(170,196,207,.14)!important;
    }}
    .monitor-filters {{
      display:flex!important;
      gap:7px!important;
      flex-wrap:wrap!important;
      margin:12px 0!important;
    }}
    #timeline-hours,
    #multi-camera-timeline {{
      min-width:820px!important;
    }}
    .timeline-hours {{
      padding-left:112px!important;
    }}
    .timeline-row {{
      grid-template-columns:100px minmax(700px,1fr)!important;
      gap:12px!important;
      min-height:40px!important;
      margin:7px 0!important;
    }}
    .timeline-lane {{
      height:30px!important;
    }}
    #create-clip {{
      max-height:none!important;
      overflow:visible!important;
      margin-top:18px!important;
    }}
    @media (max-width:1180px) {{
      .monitor-shell {{
        grid-template-columns:180px minmax(0,1fr)!important;
      }}
      .monitor-grid[data-layout="9"],
      .monitor-grid[data-layout="16"] {{
        grid-template-columns:repeat(2,minmax(0,1fr))!important;
      }}
    }}
    @media (max-width:900px) {{
      .monitor-shell {{
        grid-template-columns:1fr!important;
      }}
      .monitor-picker {{
        position:static!important;
      }}
      .monitor-camera-list {{
        grid-template-columns:repeat(2,minmax(0,1fr))!important;
      }}
      .monitor-grid[data-layout] {{
        grid-template-columns:1fr!important;
      }}
    }}
    @media (max-width:600px) {{
      .monitor-camera-list {{
        grid-template-columns:1fr!important;
      }}
      .monitor-toolbar,
      .monitor-toolbar-group {{
        align-items:stretch!important;
      }}
      .monitor-toolbar button,
      .monitor-toolbar select,
      .monitor-toolbar input {{
        flex:1 1 auto!important;
      }}
    }}
    </style>
    <header class="topbar"><div><p class="eyebrow">Live and recorded video</p><h1>Monitor</h1><p class="health-detail">Live cameras above · timeline always visible below</p></div></header>
    <section class="monitor-shell">
      <aside class="monitor-picker">
        <input class="monitor-search" id="monitor-search" placeholder="Search cameras">
        <div class="panel-head"><h2>Cameras</h2><label><input id="select-all-cameras" type="checkbox" checked> All</label></div>
        <div class="monitor-camera-list">{camera_buttons}</div>
      </aside>
      <div class="monitor-work">
        <div class="monitor-toolbar">
          <div class="monitor-toolbar-group"><strong>Layout</strong><button class="monitor-layout" data-layout="1">1</button><button class="monitor-layout active" data-layout="4">4</button><button class="monitor-layout" data-layout="9">9</button><button class="monitor-layout" data-layout="16">16</button></div>
          <div class="monitor-toolbar-group"><button id="previous-day">&lt;</button><input id="monitor-date" type="date" value="{today}"><button id="next-day">&gt;</button><button id="today-button">Today</button><select id="monitor-zoom"><option value="24">24 hr</option><option value="12">12 hr</option><option value="6">6 hr</option><option value="1">1 hr</option></select><select id="monitor-speed"><option value="1">1x</option><option value="2">2x</option><option value="4">4x</option><option value="8">8x</option></select></div>
        </div>
        <div class="monitor-grid" id="monitor-grid" data-layout="4">{tiles}</div>
        <section class="monitor-timeline">
          <div class="panel-head"><div><p class="eyebrow">Recorded activity</p><h2>Timeline</h2></div></div>
          <div class="monitor-toolbar">
            <div class="monitor-toolbar-group"><button id="skip-back">Back 10</button><button id="timeline-play">Play</button><button id="skip-forward">Forward 10</button><select id="timeline-camera">{camera_options}</select></div>
            <div class="monitor-toolbar-group"><button id="download-selected">Download</button><button id="share-selected">Share</button><a class="ghost-button" href="#create-clip">Create clip</a><button id="bookmark-selected">Bookmark</button></div>
          </div>
          <div class="monitor-filters"><button class="monitor-filter active" data-filter="all">All</button><button class="monitor-filter active" data-filter="motion">Motion</button><button class="monitor-filter active" data-filter="person">Person</button><button class="monitor-filter active" data-filter="vehicle">Vehicle</button><button class="monitor-filter active" data-filter="lpr">License plate</button><button class="monitor-filter active" data-filter="people_counting">People count</button><button class="monitor-filter active" data-filter="intrusion">Intrusion</button></div>
          <div class="selected-event-summary" id="selected-event-summary" hidden><div><strong id="selected-event-title">Selected event</strong><br><small id="selected-event-detail"></small></div><div class="monitor-toolbar-group"><button id="open-selected-recording">Open recording</button><button id="clear-selected-event">Clear</button></div></div>
          <div class="timeline-hours" id="timeline-hours"><span>00:00</span><span>02:00</span><span>04:00</span><span>06:00</span><span>08:00</span><span>10:00</span><span>12:00</span><span>14:00</span><span>16:00</span><span>18:00</span><span>20:00</span><span>22:00</span><span>24:00</span></div>
          <div id="multi-camera-timeline">{timeline_rows}</div>
          <noscript><div class="timeline-empty">JavaScript is required for live video and interactive timeline events.</div></noscript>
          <div class="event-legend"><span><i class="legend-dot event-motion"></i>Motion</span><span><i class="legend-dot event-person"></i>Person</span><span><i class="legend-dot event-vehicle"></i>Vehicle</span><span><i class="legend-dot event-lpr"></i>License plate</span><span><i class="legend-dot event-people_counting"></i>People count</span><span><i class="legend-dot event-intrusion"></i>Intrusion</span><span><i class="legend-dot" style="background:#e8eef6"></i>Recording</span></div>
        </section>
        <section class="panel" id="create-clip" style="margin-top:14px">
          <div class="panel-head"><div><h2>Create manual clip</h2><div class="health-detail">Create a clip from completed recordings.</div></div></div>
          <form id="clip-form" class="clip-form"><label>Camera<select id="clip-camera">{camera_options}</select></label><label>Start time<input id="clip-start" type="datetime-local" required></label><label>End time<input id="clip-end" type="datetime-local" required></label><button class="action-button" type="submit">Create clip</button></form>
          <div id="clip-job" class="clip-job" hidden><div class="health-detail" id="clip-message">Preparing...</div><div class="storage-bar"><span id="clip-progress" style="width:0%"></span></div></div>
        </section>
      </div>
    </section>
    <div class="timeline-tooltip" id="timeline-tooltip"></div>'''

    scripts = f'''<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>
const monitorData = {monitor_data};
const videos = new Map();
const selectedCameras = new Set(monitorData.cameras || []);
const filters = new Set(['motion','person','vehicle','lpr','people_counting','intrusion']);
let layout = 4;
let selectedEvent = null;
let currentSecond = 43200;

function reportMonitorError(error) {{
  console.error('Monitor initialization failed:', error);
  const timeline = document.querySelector('.monitor-timeline');
  if (timeline && !document.querySelector('.monitor-js-error')) {{
    timeline.insertAdjacentHTML(
      'afterbegin',
      '<div class="monitor-js-error">The interactive monitor could not fully initialize. The 24-hour timeline remains visible below. Open the browser console for details.</div>'
    );
  }}
}}

function attachStream(camera) {{
  const video = document.getElementById(`monitor-video-${{camera}}`);
  if (!video) return;
  const source = `/static/hls/camera${{camera}}.m3u8`;
  try {{
    if (window.Hls && Hls.isSupported()) {{
      const hls = new Hls();
      videos.set(camera, hls);
      hls.loadSource(source);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {{}}));
      hls.on(Hls.Events.ERROR, (_, data) => console.warn(`Camera ${{camera}} HLS error`, data));
    }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
      video.src = source;
      video.addEventListener('loadedmetadata', () => video.play().catch(() => {{}}), {{once:true}});
    }}
  }} catch (error) {{
    console.warn(`Camera ${{camera}} initialization failed`, error);
  }}
}}

function setLayout(value) {{
  layout = Number(value) || 4;
  const grid = document.getElementById('monitor-grid');
  if (grid) grid.dataset.layout = String(layout);
  document.querySelectorAll('.monitor-layout').forEach(button => {{
    button.classList.toggle('active', Number(button.dataset.layout) === layout);
  }});
  let shown = 0;
  document.querySelectorAll('.monitor-tile').forEach(tile => {{
    const camera = Number(tile.dataset.monitorCamera);
    const visible = selectedCameras.has(camera) && shown < layout;
    tile.hidden = !visible;
    if (visible) shown += 1;
  }});
}}

function dayStart() {{
  const value = document.getElementById('monitor-date')?.value || monitorData.today;
  return new Date(`${{value}}T00:00:00`);
}}

function secondsFromDay(value) {{
  const result = (new Date(value) - dayStart()) / 1000;
  return Math.max(0, Math.min(86400, result));
}}

function renderTimeline() {{
  const host = document.getElementById('multi-camera-timeline');
  if (!host) return;
  host.innerHTML = '';
  const dateValue = document.getElementById('monitor-date')?.value || monitorData.today;
  const cameras = (monitorData.cameras || []).filter(camera => selectedCameras.has(Number(camera)));

  if (!cameras.length) {{
    host.innerHTML = '<div class="timeline-empty">Select at least one camera to show its timeline.</div>';
    return;
  }}

  let visibleItems = 0;
  cameras.forEach(camera => {{
    const row = document.createElement('div');
    row.className = 'timeline-row';

    const name = document.createElement('div');
    name.className = 'timeline-camera-name';
    name.textContent = `Camera ${{camera}}`;

    const lane = document.createElement('div');
    lane.className = 'timeline-lane';

    const recordings = monitorData.recordings?.[String(camera)] || monitorData.recordings?.[camera] || [];
    recordings.forEach(recording => {{
      if (!String(recording.start || '').startsWith(dateValue)) return;
      const start = secondsFromDay(recording.start);
      const end = Math.max(start + 1, secondsFromDay(recording.end));
      const segment = document.createElement('a');
      segment.className = 'recording-segment';
      segment.href = recording.url;
      segment.style.left = `${{(start / 86400) * 100}}%`;
      segment.style.width = `${{Math.max(0.35, ((end - start) / 86400) * 100)}}%`;
      segment.title = recording.name || 'Recording';
      lane.appendChild(segment);
      visibleItems += 1;
    }});

    (monitorData.events || []).forEach(event => {{
      if (Number(event.camera) !== Number(camera)) return;
      if (!String(event.timestamp || '').startsWith(dateValue)) return;
      if (!filters.has(event.filter_type) && !filters.has(event.event_type)) return;
      const start = secondsFromDay(event.timestamp);
      const end = Math.max(start + 4, secondsFromDay(event.end_time || event.timestamp));
      const segment = document.createElement('button');
      segment.type = 'button';
      segment.className = `event-segment event-${{event.event_type}}`;
      segment.style.left = `${{(start / 86400) * 100}}%`;
      segment.style.width = `${{Math.max(0.5, ((end - start) / 86400) * 100)}}%`;
      segment.title = `${{event.event_type}} · Camera ${{camera}} · ${{String(event.timestamp).replace('T',' ').slice(0,19)}}`;
      segment.addEventListener('mouseenter', mouse => showTimelinePreview(mouse, event));
      segment.addEventListener('mousemove', moveTimelinePreview);
      segment.addEventListener('mouseleave', hideTimelinePreview);
      segment.addEventListener('click', () => {{
        selectedEvent = event;
        currentSecond = start;
        document.querySelectorAll('.event-segment').forEach(item => item.classList.remove('selected'));
        segment.classList.add('selected');
        const summary = document.getElementById('selected-event-summary');
        if (summary) {{
          summary.hidden = false;
          document.getElementById('selected-event-title').textContent =
            String(event.event_type).replaceAll('_',' ');
          document.getElementById('selected-event-detail').textContent =
            `Camera ${{event.camera}} · ${{String(event.timestamp).replace('T',' ').slice(0,19)}}`;
        }}
        renderCursor();
      }});
      lane.appendChild(segment);
      visibleItems += 1;
    }});

    const cursor = document.createElement('div');
    cursor.className = 'timeline-cursor';
    cursor.style.left = `${{(currentSecond / 86400) * 100}}%`;
    lane.appendChild(cursor);

    row.append(name, lane);
    host.appendChild(row);
  }});

  if (!visibleItems) {{
    host.insertAdjacentHTML(
      'beforeend',
      '<div class="timeline-empty">No recordings or selected event types were found for this date. The 24-hour camera tracks are still shown above.</div>'
    );
  }}
}}

function renderCursor() {{
  document.querySelectorAll('.timeline-cursor').forEach(cursor => {{
    cursor.style.left = `${{(currentSecond / 86400) * 100}}%`;
  }});
}}

async function refreshCameraStatus() {{
  try {{
    const response = await fetch('/api/cameras/status', {{cache:'no-store'}});
    if (!response.ok) return;
    const data = await response.json();
    (data.cameras || []).forEach(camera => {{
      document.getElementById(`monitor-dot-${{camera.camera}}`)
        ?.classList.toggle('online', Boolean(camera.online));
    }});
  }} catch (error) {{
    console.warn('Camera status refresh failed', error);
  }}
}}


const timelineTooltip = document.getElementById('timeline-tooltip');
let tooltipVideo = null;

function previewRecordingUrl(recording) {{
  if (!recording) return '';
  try {{
    const url = new URL(recording, location.origin);
    url.hash = '';
    return url.href;
  }} catch (error) {{
    return '';
  }}
}}

function showTimelinePreview(mouse, event) {{
  if (!timelineTooltip) return;

  const confidence = event.confidence == null
    ? ''
    : ` · ${{Math.round(Number(event.confidence) * (Number(event.confidence) <= 1 ? 100 : 1))}}%`;
  const stamp = String(event.timestamp || '').replace('T',' ').slice(0,19);
  const recordingUrl = previewRecordingUrl(event.recording);

  let media = '';
  if (recordingUrl) {{
    media = `<video muted playsinline preload="metadata" src="${{recordingUrl}}"></video>`;
  }} else if (event.thumbnail) {{
    media = `<img src="${{event.thumbnail}}" alt="Event preview">`;
  }} else {{
    media = '<div class="hover-preview-fallback">No preview available</div>';
  }}

  timelineTooltip.innerHTML =
    media +
    `<div class="hover-preview-title">${{String(event.event_type || 'event').replaceAll('_',' ')}}</div>` +
    `<div class="hover-preview-meta">Camera ${{event.camera}} · ${{stamp}}${{confidence}}</div>`;

  timelineTooltip.style.display = 'block';
  moveTimelinePreview(mouse);

  tooltipVideo = timelineTooltip.querySelector('video');
  if (tooltipVideo) {{
    tooltipVideo.addEventListener('loadedmetadata', () => {{
      try {{
        const source = new URL(event.recording, location.origin);
        const match = source.hash.match(/t=(\d+(?:\.\d+)?)/);
        if (match) tooltipVideo.currentTime = Number(match[1]);
      }} catch (error) {{}}
      tooltipVideo.play().catch(() => {{}});
    }}, {{once:true}});
    tooltipVideo.addEventListener('error', () => {{
      if (event.thumbnail) {{
        const image = document.createElement('img');
        image.src = event.thumbnail;
        image.alt = 'Event preview';
        timelineTooltip.querySelector('video')?.replaceWith(image);
      }}
    }}, {{once:true}});
  }}
}}

function moveTimelinePreview(mouse) {{
  if (!timelineTooltip || timelineTooltip.style.display === 'none') return;
  const left = Math.min(window.innerWidth - 320, mouse.clientX + 16);
  const top = Math.min(window.innerHeight - 240, mouse.clientY + 16);
  timelineTooltip.style.left = `${{Math.max(8, left)}}px`;
  timelineTooltip.style.top = `${{Math.max(8, top)}}px`;
}}

function hideTimelinePreview() {{
  if (tooltipVideo) {{
    tooltipVideo.pause();
    tooltipVideo.removeAttribute('src');
    tooltipVideo.load();
    tooltipVideo = null;
  }}
  if (timelineTooltip) {{
    timelineTooltip.style.display = 'none';
    timelineTooltip.innerHTML = '';
  }}
}}


function initializeMonitor() {{
  (monitorData.cameras || []).forEach(attachStream);

  document.querySelectorAll('.monitor-layout').forEach(button => {{
    button.addEventListener('click', () => setLayout(button.dataset.layout));
  }});

  document.querySelectorAll('.monitor-camera-item').forEach(button => {{
    button.addEventListener('click', () => {{
      const camera = Number(button.dataset.camera);
      if (selectedCameras.has(camera)) selectedCameras.delete(camera);
      else selectedCameras.add(camera);
      button.classList.toggle('active', selectedCameras.has(camera));
      renderTimeline();
      setLayout(layout);
    }});
  }});

  document.getElementById('select-all-cameras')?.addEventListener('change', event => {{
    selectedCameras.clear();
    if (event.target.checked) (monitorData.cameras || []).forEach(camera => selectedCameras.add(Number(camera)));
    document.querySelectorAll('.monitor-camera-item').forEach(button => {{
      button.classList.toggle('active', selectedCameras.has(Number(button.dataset.camera)));
    }});
    renderTimeline();
    setLayout(layout);
  }});

  document.getElementById('monitor-date')?.addEventListener('change', renderTimeline);
  document.getElementById('previous-day')?.addEventListener('click', () => changeDay(-1));
  document.getElementById('next-day')?.addEventListener('click', () => changeDay(1));
  document.getElementById('today-button')?.addEventListener('click', () => {{
    document.getElementById('monitor-date').value = monitorData.today;
    renderTimeline();
  }});

  document.querySelectorAll('.monitor-filter').forEach(button => {{
    button.addEventListener('click', () => {{
      const filter = button.dataset.filter;
      if (filter === 'all') {{
        const enable = !button.classList.contains('active');
        filters.clear();
        if (enable) ['motion','person','vehicle','lpr','people_counting','intrusion'].forEach(item => filters.add(item));
        document.querySelectorAll('.monitor-filter').forEach(item => item.classList.toggle('active', enable));
      }} else {{
        if (filters.has(filter)) filters.delete(filter);
        else filters.add(filter);
        button.classList.toggle('active', filters.has(filter));
      }}
      renderTimeline();
    }});
  }});

  document.getElementById('clear-selected-event')?.addEventListener('click', () => {{
    selectedEvent = null;
    document.getElementById('selected-event-summary').hidden = true;
    document.querySelectorAll('.event-segment').forEach(item => item.classList.remove('selected'));
  }});

  document.getElementById('open-selected-recording')?.addEventListener('click', () => {{
    if (!selectedEvent?.recording) return showToast('This event does not have a recording attached.');
    location.href = selectedEvent.recording;
  }});

  document.getElementById('download-selected')?.addEventListener('click', () => {{
    if (!selectedEvent?.recording) return showToast('Select an event with a recording first.');
    const link = document.createElement('a');
    link.href = selectedEvent.recording;
    link.download = '';
    document.body.appendChild(link);
    link.click();
    link.remove();
  }});

  document.getElementById('share-selected')?.addEventListener('click', async () => {{
    if (!selectedEvent?.recording) return showToast('Select an event with a recording first.');
    const url = new URL(selectedEvent.recording, location.origin).href;
    if (navigator.share) await navigator.share({{title:'AnyAiCam event', url}});
    else {{
      await navigator.clipboard.writeText(url);
      showToast('Event link copied.');
    }}
  }});

  document.getElementById('skip-back')?.addEventListener('click', () => {{
    currentSecond = Math.max(0, currentSecond - 10);
    renderCursor();
  }});
  document.getElementById('skip-forward')?.addEventListener('click', () => {{
    currentSecond = Math.min(86400, currentSecond + 10);
    renderCursor();
  }});

  setLayout(4);
  renderTimeline();
  refreshCameraStatus();
  setInterval(refreshCameraStatus, 15000);
}}

function changeDay(delta) {{
  const input = document.getElementById('monitor-date');
  const date = new Date(`${{input.value}}T12:00:00`);
  date.setDate(date.getDate() + delta);
  input.value = date.toISOString().slice(0,10);
  renderTimeline();
}}

document.addEventListener('DOMContentLoaded', () => {{
  try {{
    initializeMonitor();
  }} catch (error) {{
    reportMonitorError(error);
  }}
}});
</script>'''
    return page_shell("Monitor", "playback", content, scripts)
