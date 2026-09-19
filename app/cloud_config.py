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


# The exact untouched default allowed_origins resolves to below -- see
# Settings.effective_allowed_origins.
_DEFAULT_ALLOWED_ORIGINS = ["http://localhost:8000"]


# The exact untouched default trusted_hosts resolves to below. Compared
# by value (never by re-reading os.environ) so Settings.effective_
# trusted_hosts stays fully driven by constructor arguments in tests,
# matching every other property on this class -- an operator who
# explicitly configures ANYAICAM_TRUSTED_HOSTS to this same literal list
# is indistinguishable from -- and treated identically to -- never having
# set it at all, which is fine: both mean "no real restriction was ever
# configured."
_DEFAULT_TRUSTED_HOSTS = ["localhost", "127.0.0.1", "testserver"]


@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("ANYAICAM_ENV", "development").lower()
    # Same env var and default ("edge") app/main.py's own RUNTIME_ROLE
    # constant already uses. The real AWS/cloud deployment explicitly
    # sets ANYAICAM_RUNTIME_ROLE=cloud (aws.env, ecs-task-definition.json)
    # -- this default only ever applies to appliances that never set it,
    # i.e. edge appliances, so it cannot silently relax the real
    # internet-facing cloud deployment's production requirements.
    runtime_role: str = os.getenv("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower()
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
    # Notifications settings page (Email + SMS channels) -- sms_backend
    # mirrors email_backend's own "preview" default exactly (never a
    # live send until explicitly configured). Every credential here is
    # read from the environment at call time by sms_service.py, never a
    # literal in source; twilio_auth_token is intentionally not logged
    # or echoed anywhere this dataclass's other fields might be.
    sms_backend: str = os.getenv("ANYAICAM_SMS_BACKEND", "preview").lower()
    sms_preview_dir: str = os.getenv(
        "ANYAICAM_SMS_PREVIEW_DIR",
        "/app/recordings/sms-preview",
    )
    twilio_account_sid: str = os.getenv("ANYAICAM_TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("ANYAICAM_TWILIO_AUTH_TOKEN", "")
    twilio_from_number: str = os.getenv("ANYAICAM_TWILIO_FROM_NUMBER", "")
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
    def edge_production(self):
        """A RUNTIME_ROLE=edge appliance in ANYAICAM_ENV=production is a
        distinct security PROFILE, not a weaker version of cloud
        production: it is reached over a private LAN/Tailscale network,
        never the public internet, so it has no in-scope HTTPS
        termination boundary to require -- unlike an internet-facing
        cloud/combined production deployment, where all of that remains
        mandatory and unchanged (see validate() below: every check this
        property exempts is skipped ONLY when this is True, and strong/
        non-default application secrets are never exempted for either
        profile). Staging is deliberately unaffected by runtime_role --
        this scopes strictly to production, matching the actual ask."""
        return self.production and self.runtime_role == "edge"

    @property
    def effective_trusted_hosts(self):
        """The value actually passed to TrustedHostMiddleware (main.py).

        Cloud/combined production has a single, fixed public domain it is
        meant to serve exclusively -- Host-header validation there is a
        real, load-bearing security check and stays completely unchanged,
        whether trusted_hosts is left at its default or explicitly
        configured.

        An edge appliance has no such fixed address: it's reached over
        whatever LAN IP or Tailscale address DHCP/Tailscale happens to
        assign it, which can't be enumerated at install time the way a
        cloud domain can. Confirmed live on Samsung: the installer's own
        ANYAICAM_ENV=production stamp (this session's earlier fix) made
        TrustedHostMiddleware start rejecting the appliance's real
        Tailscale address with "Invalid host header", because
        ANYAICAM_TRUSTED_HOSTS was never set and the default
        (localhost/127.0.0.1/testserver) matches nothing an operator
        actually connects through. For edge_production specifically, with
        trusted_hosts still at that exact untouched default, this returns
        Starlette's own "*" sentinel, which disables host-header matching
        entirely -- the same private-LAN/Tailscale trust boundary
        edge_production already relies on for its other exemptions (see
        its own docstring above). The moment an operator sets
        ANYAICAM_TRUSTED_HOSTS to anything else -- on an edge appliance or
        otherwise -- that explicit value is honored exactly as configured,
        never silently widened."""
        if self.edge_production and self.trusted_hosts == _DEFAULT_TRUSTED_HOSTS:
            return ["*"]
        return self.trusted_hosts

    @property
    def effective_allowed_origins(self):
        """The value cloud_security.ProductionSecurityMiddleware actually
        checks a browser request's Origin header against, and CORS
        preflight/state-changing requests are rejected with "Origin is
        not allowed" for any Origin not in this list.

        Confirmed live on Samsung: the Admin login POST from
        http://192.168.0.165:8000/partner.html was rejected because
        ANYAICAM_ALLOWED_ORIGINS was never set and the untouched default
        (http://localhost:8000) matches no origin a browser actually
        loads the appliance's own pages from -- the exact same shape of
        bug as effective_trusted_hosts above, for the same underlying
        reason: an edge appliance has no fixed address to enumerate in
        advance, unlike a cloud deployment's single fixed public domain.

        For edge_production specifically, with allowed_origins still at
        its exact untouched default, this returns ["*"], which
        cloud_security.py's dispatch() treats as "any origin accepted" --
        the same private-LAN/Tailscale trust boundary edge_production
        already relies on for every other exemption on this class (see
        its own docstring). Cloud/combined production, staging, and any
        profile where an operator has explicitly configured
        ANYAICAM_ALLOWED_ORIGINS are all completely unaffected and keep
        exact current behavior."""
        if self.edge_production and self.allowed_origins == _DEFAULT_ALLOWED_ORIGINS:
            return ["*"]
        return self.allowed_origins

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

        # Internet-facing hardening -- unchanged from before for staging
        # and for cloud/combined production. edge_production is the only
        # exemption, and it is scoped exactly to this block: an edge
        # appliance's LAN/Tailscale reachability has no HTTPS-terminating
        # boundary in front of it to require secure cookies, CSRF
        # tokens, or HTTPS-scheme URLs against.
        if self.deployed and not self.edge_production:
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
            # Strong, non-default application secrets are required in
            # EVERY production deployment -- deliberately outside the
            # `not self.edge_production` branch below, so edge can never
            # exempt itself from this one.
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

            if not self.edge_production:
                if not self.https_only:
                    errors.append("Production requires HTTPS-only mode.")

                if (
                    urlparse(self.partner_login_url).scheme != "https"
                    or urlparse(self.customer_login_url).scheme != "https"
                ):
                    errors.append(
                        "Production partner and customer login URLs must use HTTPS."
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
