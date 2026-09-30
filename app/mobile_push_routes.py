"""Customer-authenticated native push device lifecycle, separate from legacy Web Push."""
import secrets
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from partner_db import connection
from notification_settings_page import _resolve_customer_identity, _camera_context


class Device(BaseModel):
    installation_id: str = Field(min_length=16, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    platform: Literal['android', 'ios', 'web']
    token: str = Field(min_length=20, max_length=4096, pattern=r'^\S+$')
    enabled: bool = True


def user_context(request, db):
    identity = _resolve_customer_identity(request)
    if not identity:
        raise HTTPException(401, 'Customer sign-in required.')
    user_id, _ = _camera_context(db, identity)
    user = db.execute("SELECT id FROM partner_users WHERE id=? AND customer_id=? AND approved=1 AND account_status='active'", (user_id, identity['customer_id'])).fetchone()
    if not user:
        raise HTTPException(403, 'Active customer account required.')
    return user_id, identity['customer_id']


def register_routes(app):
    @app.get('/api/mobile/push/config')
    def web_config():
        from mobile_push_provider import web_configuration
        return web_configuration()

    @app.get('/api/mobile/push/firebase-config.js')
    def worker_config():
        import json
        from fastapi.responses import Response
        from mobile_push_provider import web_configuration
        config = web_configuration()
        if not config['available']:
            raise HTTPException(503, 'Browser push is not configured.')
        return Response('self.ANYAICAM_FIREBASE_CONFIG=' + json.dumps(config['firebase']) + ';', media_type='application/javascript', headers={'Cache-Control': 'no-store'})

    @app.get('/mobile-push-sw.js')
    def push_service_worker():
        from pathlib import Path
        from fastapi.responses import FileResponse
        return FileResponse(Path(__file__).parent / 'static' / 'mobile-push-sw.js', media_type='application/javascript', headers={'Cache-Control': 'no-cache'})

    @app.put('/api/mobile/push/devices')
    def register_device(payload: Device, request: Request):
        with connection() as db:
            user_id, customer_id = user_context(request, db)
            # Never silently transfer another account's token to this user.
            occupied = db.execute('SELECT user_id,installation_id FROM mobile_push_devices WHERE token=?', (payload.token,)).fetchone()
            if occupied and (occupied['user_id'] != user_id or occupied['installation_id'] != payload.installation_id):
                raise HTTPException(409, 'Device token is already registered; unregister it before switching accounts.')
            try:
                db.execute('INSERT INTO mobile_push_devices(id,user_id,customer_id,installation_id,platform,token,enabled,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,installation_id) DO UPDATE SET customer_id=excluded.customer_id,platform=excluded.platform,token=excluded.token,enabled=excluded.enabled,updated_at=excluded.updated_at',
                    (secrets.token_hex(16), user_id, customer_id, payload.installation_id, payload.platform, payload.token, int(payload.enabled), datetime.now(timezone.utc).isoformat()))
            except Exception as exc:
                import sqlite3
                if isinstance(exc, sqlite3.IntegrityError) or getattr(exc, 'sqlstate', None) == '23505':
                    raise HTTPException(409, 'Device registration changed concurrently; refresh the device list and retry.') from None
                raise
            item = db.execute('SELECT id,platform,enabled FROM mobile_push_devices WHERE user_id=? AND installation_id=?', (user_id, payload.installation_id)).fetchone()
        from mobile_push_provider import configured
        return {'device': dict(item), 'provider_configured': configured(), 'delivery_verified': False}

    @app.get('/api/mobile/push/devices')
    def list_devices(request: Request):
        with connection() as db:
            user_id, customer_id = user_context(request, db)
            devices = db.execute('SELECT id,installation_id,platform,enabled,updated_at FROM mobile_push_devices WHERE user_id=? AND customer_id=?', (user_id, customer_id)).fetchall()
        return {'devices': [dict(d) for d in devices]}

    @app.get('/api/mobile/push/deliveries')
    def delivery_status(request: Request):
        with connection() as db:
            user_id, customer_id = user_context(request, db)
            records = db.execute('SELECT p.notification_id,p.device_id,p.status,p.attempt,p.error,p.created_at FROM mobile_push_outbox p JOIN notifications n ON n.id=p.notification_id WHERE n.user_id=? AND n.customer_id=? ORDER BY p.created_at DESC LIMIT 100', (user_id, customer_id)).fetchall()
        return {'deliveries': [dict(r) for r in records], 'sent_means': 'accepted_by_provider_not_confirmed_on_device'}

    @app.delete('/api/mobile/push/devices/{device_id}')
    def unregister_device(device_id: str, request: Request):
        with connection() as db:
            user_id, customer_id = user_context(request, db)
            device = db.execute('SELECT id FROM mobile_push_devices WHERE id=? AND user_id=? AND customer_id=?', (device_id, user_id, customer_id)).fetchone()
            if not device:
                raise HTTPException(404, 'Device not found.')
            db.execute('DELETE FROM mobile_push_outbox WHERE device_id=?', (device_id,))
            db.execute('DELETE FROM mobile_push_devices WHERE id=?', (device_id,))
        return {'status': 'unregistered'}

    @app.get('/api/mobile/push/notifications/{notification_id}')
    def open_notification(notification_id: str, request: Request):
        # No arbitrary URL from the push payload is ever used as a destination.
        from notification_email import event_path
        with connection() as db:
            user_id, customer_id = user_context(request, db)
            n = db.execute('SELECT * FROM notifications WHERE id=? AND user_id=? AND customer_id=?', (notification_id, user_id, customer_id)).fetchone()
            if not n:
                raise HTTPException(404, 'Notification not found.')
            # Check camera visibility independently of current delivery preferences.
            from camera_access import authorized_camera_ids
            u = db.execute('SELECT role,camera_access_mode FROM partner_users WHERE id=?', (user_id,)).fetchone()
            if n['camera_id'] and n['camera_id'] not in authorized_camera_ids(db,user_id=user_id,customer_id=customer_id,role=u['role'],access_mode=u['camera_access_mode'] or 'selected'):
                raise HTTPException(404, 'Notification not found.')
            context = dict(n)
        return {'notification_id': notification_id, 'path': event_path(context)}
