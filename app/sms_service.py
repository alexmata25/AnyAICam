"""SMS delivery backend for the Notifications settings page -- mirrors
email_service.py's own existing ABC/PreviewX/get_x_service() shape
exactly, so both channels behave the same way for an unconfigured
deployment and the same way once a real provider is wired.

No SMS credential is ever a literal in this file or any caller of it --
ANYAICAM_TWILIO_ACCOUNT_SID/ANYAICAM_TWILIO_AUTH_TOKEN/
ANYAICAM_TWILIO_FROM_NUMBER are read from the environment, at call
time, by TwilioSms alone (matching SMTPEmail's own settings.smtp_*
convention). ANYAICAM_SMS_BACKEND defaults to 'preview' -- the same
default email_backend already uses -- so a fresh deployment never
silently claims to have sent a real text message; get_sms_service()
only ever returns TwilioSms once an operator has explicitly set
ANYAICAM_SMS_BACKEND=twilio (and only if the three Twilio settings are
all actually present -- otherwise it fails closed to Preview so a
half-configured environment still gives an honest, inspectable result
instead of a confusing runtime error the first time a customer clicks
"Test SMS")."""
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from base64 import b64encode
from datetime import datetime
from pathlib import Path

from cloud_config import settings

SMS_TYPES = {"notification_test", "appliance_alert"}
# E.164-ish: a leading + and 8-15 digits. Deliberately loose (real
# validation belongs to the provider) -- this only rejects obviously
# malformed input before it ever reaches a paid API call.
PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")


def is_valid_phone(value: str) -> bool:
    return bool(PHONE_PATTERN.match((value or "").strip()))


class SmsBackend(ABC):
    @abstractmethod
    def send(self, message_type: str, to: str, body: str) -> dict: ...


class PreviewSms(SmsBackend):
    """Local development / unconfigured-deployment default -- writes
    what would have been sent to a local preview file and returns
    status='preview', the same honest non-claim PreviewEmail already
    makes for the email channel. Never reports status='sent'."""

    def __init__(self, root=None):
        self.root = Path(root or settings.sms_preview_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def send(self, message_type: str, to: str, body: str) -> dict:
        if message_type not in SMS_TYPES:
            raise ValueError("Unsupported SMS type.")
        identifier = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        record = {
            "id": identifier, "type": message_type, "to": to, "body": body,
            "created_at": datetime.now().isoformat(), "status": "preview",
        }
        (self.root / f"{identifier}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record


class UnavailableSms(SmsBackend):
    """ANYAICAM_SMS_BACKEND=twilio was requested but the required
    Twilio settings aren't all present -- fails closed and honest
    rather than either silently falling back to Preview (which would
    make a misconfigured production deployment look like it's working)
    or raising an unhandled exception a customer's "Test SMS" click
    would surface as a raw 500."""

    def send(self, message_type: str, to: str, body: str) -> dict:
        return {
            "type": message_type, "to": to, "status": "unavailable",
            "created_at": datetime.now().isoformat(),
            "detail": "SMS delivery is not configured yet.",
        }


class TwilioSms(SmsBackend):
    """Real delivery via Twilio's Messages REST API, using only the
    Python standard library (no added dependency) -- a single HTTP
    Basic-authenticated POST, exactly what the twilio SDK itself does
    under the hood. account_sid/auth_token/from_number are read from
    settings (itself reading the environment) at construction time,
    never hardcoded."""

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number

    def send(self, message_type: str, to: str, body: str) -> dict:
        if message_type not in SMS_TYPES:
            raise ValueError("Unsupported SMS type.")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        payload = urllib.parse.urlencode({"To": to, "From": self.from_number, "Body": body}).encode()
        credentials = b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode()
        request = urllib.request.Request(
            url, data=payload,
            headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15, context=ssl.create_default_context()) as response:
                response.read()
            return {"type": message_type, "to": to, "status": "sent", "created_at": datetime.now().isoformat()}
        except urllib.error.HTTPError as error:
            return {
                "type": message_type, "to": to, "status": "failed",
                "created_at": datetime.now().isoformat(), "detail": f"Twilio rejected the request ({error.code}).",
            }
        except OSError as error:
            return {
                "type": message_type, "to": to, "status": "failed",
                "created_at": datetime.now().isoformat(), "detail": f"SMS delivery failed: {error}",
            }


def get_sms_service() -> SmsBackend:
    if settings.sms_backend == "twilio":
        if settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number:
            return TwilioSms(settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number)
        return UnavailableSms()
    return PreviewSms()
