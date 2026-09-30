"""Shared VMS push provider. FCM transports Android and iOS (via APNs).

Optional dependency and credentials are loaded only when explicitly enabled.
Provider errors are classified, never persisted verbatim (they may contain tokens).
"""
import os
import json
import time
from datetime import timedelta
from threading import Lock

_lock = Lock()


def configured():
    return os.getenv('ANYAICAM_MOBILE_PUSH_BACKEND', '').lower() == 'fcm' and bool(os.getenv('ANYAICAM_FCM_PROJECT_ID', '').strip())


def send(device, notification, *, ttl_seconds):
    if not configured():
        return {'status': 'unavailable', 'error': 'fcm_not_configured'}
    try:
        import firebase_admin
        from firebase_admin import messaging
        with _lock:
            try:
                app = firebase_admin.get_app('anyaicam-push')
            except ValueError:
                app = firebase_admin.initialize_app(options={
                    'projectId': os.environ['ANYAICAM_FCM_PROJECT_ID'], 'httpTimeout': 10,
                }, name='anyaicam-push')
        urgent = notification['event_type'] in {'intrusion_alarm', 'aac_voice_call'}
        # No images, plate values, visitor transcripts, or bearer links on the lock screen.
        # The authenticated client resolves this opaque notification ID on tap.
        title = {'intrusion_alarm': 'INTRUSION ALARM', 'aac_voice_call': 'Visitor Call'}.get(
            notification['event_type'], 'AnyAiCam activity')
        channel = 'anyaicam_intrusion' if notification['event_type'] == 'intrusion_alarm' else (
            'anyaicam_visitor_call' if urgent else 'anyaicam_activity')
        message = messaging.Message(
            token=device['token'],
            notification=messaging.Notification(title=title, body='Open AnyAiCam to view this alert.'),
            data={'notification_id': notification['id'], 'event_type': notification['event_type']},
            android=messaging.AndroidConfig(priority='high' if urgent else 'normal',
                ttl=timedelta(seconds=max(0, ttl_seconds)),
                notification=messaging.AndroidNotification(channel_id=channel, tag=notification['id'])),
            webpush=messaging.WebpushConfig(
                headers={'TTL': str(max(0, ttl_seconds)), 'Urgency': 'high' if urgent else 'normal'},
                notification=messaging.WebpushNotification(title=title, body='Open AnyAiCam to view this alert.',
                    tag=notification['id'], data={'notification_id': notification['id']})),
            apns=messaging.APNSConfig(headers={
                'apns-push-type': 'alert', 'apns-priority': '10' if urgent else '5',
                'apns-expiration': str(int(time.time()) + max(0, ttl_seconds)),
                'apns-collapse-id': notification['id'],
            }, payload=messaging.APNSPayload(aps=messaging.Aps(sound='default' if urgent else None))),
        )
        return {'status': 'sent', 'provider_id': messaging.send(message, app=app)}
    except ImportError:
        return {'status': 'unavailable', 'error': 'firebase_admin_not_installed'}
    except Exception as exc:
        code = getattr(exc, 'code', '')
        name = type(exc).__name__
        if name == 'UnregisteredError':
            return {'status': 'invalid_token', 'error': 'unregistered'}
        if code == 'RESOURCE_EXHAUSTED':
            return {'status': 'retry', 'error': 'provider_throttled'}
        if name == 'SenderIdMismatchError':
            return {'status': 'failed', 'error': 'token_project_mismatch'}
        if name == 'ThirdPartyAuthError':
            return {'status': 'unavailable', 'error': 'apns_credentials_missing_or_invalid'}
        if name == 'DefaultCredentialsError':
            return {'status': 'unavailable', 'error': 'application_default_credentials_unavailable'}
        if code in {'UNAVAILABLE', 'INTERNAL', 'DEADLINE_EXCEEDED'} or isinstance(exc, (TimeoutError, ConnectionError)):
            return {'status': 'retry', 'error': 'provider_transient'}
        if code == 'INVALID_ARGUMENT':
            return {'status': 'failed', 'error': 'invalid_message'}
        return {'status': 'unavailable', 'error': 'provider_configuration_or_authentication'}


def web_configuration():
    """Explicit allowlist: server credentials can never reach this response."""
    try:
        raw = json.loads(os.getenv('ANYAICAM_FIREBASE_WEB_CONFIG_JSON', '{}'))
        allowed = ('apiKey', 'appId', 'messagingSenderId', 'projectId', 'authDomain')
        config = {key: raw[key] for key in allowed if isinstance(raw.get(key), str)}
    except (ValueError, TypeError, AttributeError):
        config = {}
    key = os.getenv('ANYAICAM_FIREBASE_WEB_VAPID_PUBLIC_KEY', '').strip()
    available = bool(configured() and key and all(config.get(k) for k in ('apiKey', 'appId', 'messagingSenderId', 'projectId'))
                     and config.get('projectId') == os.getenv('ANYAICAM_FCM_PROJECT_ID'))
    return {'available': available, 'firebase': config if available else {}, 'vapid_public_key': key if available else ''}
