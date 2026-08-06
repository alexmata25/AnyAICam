import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _bool(name, default=False):
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("ANYAICAM_ENV", "development").lower()
    app_secrets: list[str] = field(
        default_factory=lambda: [
            item
            for item in os.getenv(
                "ANYAICAM_APP_SECRETS",
                "local-development-secret-change-me",
            ).split(",")
            if item
        ]
    )
    database_backend: str = os.getenv("ANYAICAM_DATABASE_BACKEND", "sqlite").lower()
    database_url: str = os.getenv("ANYAICAM_DATABASE_URL", "")
    sqlite_path: str = os.getenv(
        "ANYAICAM_PARTNER_DB",
        "/app/recordings/partner_portal.db",
    )
    public_portal_url: str = os.getenv(
        "ANYAICAM_PUBLIC_PORTAL_URL",
        "http://localhost:8000",
    )
    appliance_api_url: str = os.getenv(
        "ANYAICAM_APPLIANCE_API_URL",
        "http://localhost:8000",
    )
    public_website_url: str = os.getenv(
        "ANYAICAM_PUBLIC_WEBSITE_URL",
        "http://localhost:8000",
    )
    portal_url: str = os.getenv(
        "ANYAICAM_PORTAL_URL",
        "http://localhost:8000",
    )
    api_base_url: str = os.getenv(
        "ANYAICAM_API_BASE_URL",
        "http://localhost:8000/api/v1",
    )
    password_reset_url: str = os.getenv(
        "ANYAICAM_PASSWORD_RESET_URL",
        "http://localhost:8000/reset-password",
    )
    invitation_url: str = os.getenv(
        "ANYAICAM_INVITATION_URL",
        "http://localhost:8000/partner.html",
    )
    appliance_activation_url: str = os.getenv(
        "ANYAICAM_APPLIANCE_ACTIVATION_URL",
        "http://localhost:8000/api/appliance/activate",
    )
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            value.strip().rstrip("/")
            for value in os.getenv(
                "ANYAICAM_ALLOWED_ORIGINS",
                "http://localhost:8000",
            ).split(",")
            if value.strip()
        ]
    )
    trusted_hosts: list[str] = field(
        default_factory=lambda: [
            value.strip()
            for value in os.getenv(
                "ANYAICAM_TRUSTED_HOSTS",
                "localhost,127.0.0.1,testserver",
            ).split(",")
            if value.strip()
        ]
    )
    development_partner_url: str = os.getenv(
        "ANYAICAM_DEVELOPMENT_PARTNER_URL",
        "http://localhost:8000/partner.html",
    )
    development_customer_url: str = os.getenv(
        "ANYAICAM_DEVELOPMENT_CUSTOMER_URL",
        "http://localhost:8000/customer-login.html",
    )
    staging_partner_url: str = os.getenv(
        "ANYAICAM_STAGING_PARTNER_URL",
        "https://portal-staging.anyaicam.com/partner.html",
    )
    staging_customer_url: str = os.getenv(
        "ANYAICAM_STAGING_CUSTOMER_URL",
        "https://portal-staging.anyaicam.com/customer-login.html",
    )
    production_partner_url: str = os.getenv(
        "ANYAICAM_PRODUCTION_PARTNER_URL",
        "https://portal.anyaicam.com/partner.html",
    )
    production_customer_url: str = os.getenv(
        "ANYAICAM_PRODUCTION_CUSTOMER_URL",
        "https://portal.anyaicam.com/customer-login.html",
    )
    storage_backend: str = os.getenv("ANYAICAM_STORAGE_BACKEND", "local").lower()
    local_storage_root: str = os.getenv(
        "ANYAICAM_LOCAL_STORAGE_ROOT",
        "/app/recordings/storage",
    )
    s3_endpoint: str = os.getenv("ANYAICAM_S3_ENDPOINT", "")
    s3_bucket: str = os.getenv("ANYAICAM_S3_BUCKET", "")
    s3_region: str = os.getenv("ANYAICAM_S3_REGION", "us-east-1")
    email_backend: str = os.getenv("ANYAICAM_EMAIL_BACKEND", "preview").lower()
    email_preview_dir: str = os.getenv(
        "ANYAICAM_EMAIL_PREVIEW_DIR",
        "/app/recordings/email-preview",
    )
    smtp_host: str = os.getenv("ANYAICAM_SMTP_HOST", "")
    smtp_port: int = _int("ANYAICAM_SMTP_PORT", 587)
    smtp_username: str = os.getenv("ANYAICAM_SMTP_USERNAME", "")
    smtp_password: str = os.getenv("ANYAICAM_SMTP_PASSWORD", "")
    email_from: str = os.getenv("ANYAICAM_EMAIL_FROM", "no-reply@localhost")
    log_level: str = os.getenv("ANYAICAM_LOG_LEVEL", "INFO").upper()
    log_format: str = os.getenv("ANYAICAM_LOG_FORMAT", "text").lower()
    https_only: bool = _bool("ANYAICAM_HTTPS_ONLY", False)
    secure_cookies: bool = _bool("ANYAICAM_SECURE_COOKIES", False)
    cookie_domain: str = os.getenv("ANYAICAM_COOKIE_DOMAIN", "").strip()
    csrf_enabled: bool = _bool("ANYAICAM_CSRF_ENABLED", False)
    login_attempt_limit: int = _int("ANYAICAM_LOGIN_ATTEMPT_LIMIT", 5)
    login_lockout_minutes: int = _int("ANYAICAM_LOGIN_LOCKOUT_MINUTES", 15)
    media_retention_days: int = _int("ANYAICAM_MEDIA_RETENTION_DAYS", 365)
    audit_retention_days: int = _int("ANYAICAM_AUDIT_RETENTION_DAYS", 2555)

    @property
    def production(self):
        return self.environment == "production"

    @property
    def staging(self):
        return self.environment == "staging"

    @property
    def deployed(self):
        return self.environment in {"staging", "production"}

    @property
    def partner_login_url(self):
        return {
            "local": self.development_partner_url,
            "development": self.development_partner_url,
            "staging": self.staging_partner_url,
            "production": self.production_partner_url,
        }[self.environment]

    @property
    def customer_login_url(self):
        return {
            "local": self.development_customer_url,
            "development": self.development_customer_url,
            "staging": self.staging_customer_url,
            "production": self.production_customer_url,
        }[self.environment]

    def validate(self):
        errors = []

        if self.environment not in {
            "local",
            "development",
            "staging",
            "production",
        }:
            errors.append(
                "ANYAICAM_ENV must be local, development, staging, or production."
            )

        if self.database_backend not in {"sqlite", "postgresql"}:
            errors.append("Database backend must be sqlite or postgresql.")

        if self.database_backend == "postgresql" and not self.database_url:
            errors.append(
                "ANYAICAM_DATABASE_URL is required for PostgreSQL."
            )

        if self.storage_backend == "s3" and (
            not self.s3_endpoint or not self.s3_bucket
        ):
            errors.append(
                "S3 endpoint and bucket are required when S3 storage is enabled."
            )

        if self.email_backend == "smtp" and not self.smtp_host:
            errors.append(
                "SMTP host is required when SMTP email is enabled."
            )

        urls = {
            "public website": self.public_website_url,
            "portal": self.portal_url,
            "API": self.api_base_url,
            "password reset": self.password_reset_url,
            "invitation": self.invitation_url,
            "appliance activation": self.appliance_activation_url,
            "partner login": self.partner_login_url,
            "customer login": self.customer_login_url,
        }

        if self.deployed:
            if not self.secure_cookies or not self.csrf_enabled:
                errors.append(
                    "Staging and production require secure cookies and CSRF protection."
                )

            if any(urlparse(value).scheme != "https" for value in urls.values()):
                errors.append(
                    "All staging and production public URLs must use HTTPS."
                )

            if any(
                origin.startswith("http://")
                for origin in self.allowed_origins
            ):
                errors.append(
                    "Staging and production allowed origins must use HTTPS."
                )

        if self.production:
            if not self.https_only:
                errors.append("Production requires HTTPS-only mode.")

            if (
                urlparse(self.partner_login_url).scheme != "https"
                or urlparse(self.customer_login_url).scheme != "https"
            ):
                errors.append(
                    "Production partner and customer login URLs must use HTTPS."
                )

            if any(
                secret in {
                    "local-development-secret-change-me",
                    "REPLACE_CURRENT_SECRET",
                    "replace-current-secret",
                }
                or len(secret) < 32
                for secret in self.app_secrets
            ):
                errors.append(
                    "Replace default or short application secrets in production."
                )

        if errors:
            raise RuntimeError("Configuration error: " + " ".join(errors))


settings = Settings()


def configure_logging():
    if settings.log_format == "json":
        class JsonFormatter(logging.Formatter):
            def format(self, record):
                import json
                import time

                return json.dumps(
                    {
                        "timestamp": time.time(),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": record.getMessage(),
                    }
                )

        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logging.basicConfig(
            level=settings.log_level,
            handlers=[handler],
        )
    else:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
