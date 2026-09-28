"""Phase 6b (docs/AI_HANDOFF.md Sec 8): dynamic, per-request HLS playlist
generation for the live-relay path -- serves a customer-authenticated
.m3u8 built from the current live_manifest_store state, with each segment
URI a freshly CloudFront-signed URL (live_cdn_signing.py). No media bytes
ever pass through this module or FastAPI -- only tiny playlist text.

Reuses, never redesigns: appliance_cloud.live_manifest_store (Phase 2),
camera_mapping.resolve_camera_number (Phase 6a), appliance_protocol's
live_relay_s3_prefix (Phase 1/2), live_cdn_signing.sign_segment_url
(Phase 6b). Does not touch the Phase 2/3/4 relay/uploader implementation,
does not add customer live start/stop routes (Phase 6c), does not use
live_view_sessions (Phase 6c), and does not build any frontend surface
(Phase 6d) -- this endpoint exists and is testable, but nothing in this
repo calls it yet, and get_configured_signer() (live_cdn_signing.py)
unconditionally returns None until a future phase creates real CloudFront
key material -- so every real request to this route fails closed with 503
until then, by construction.
"""

import logging
import os
import time
from datetime import datetime
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, FileResponse
from cloud_config import settings
from local_live_hls import require_local_camera, local_playlist, segment_path

from appliance_cloud import live_manifest_store
from appliance_protocol import live_relay_s3_prefix
from camera_mapping import resolve_camera_number
from live_cdn_signing import get_configured_signer, sign_segment_url
from live_relay_idle_sweep import record_relay_viewer_activity
from partner_db import connection
from partner_portal import partner_identity

logger = logging.getLogger('anyaicam.live_playlist')

STALE_MANIFEST_SECONDS = 30
SEGMENT_TARGET_DURATION = 2  # must match start_live_stream()'s FFmpeg -hls_time;
# duplicated as a constant rather than imported from main.py, which would be
# a circular import (main.py is what will register this module's routes).

CLOUDFRONT_KEY_PAIR_ID_ENV = 'ANYAICAM_CLOUDFRONT_KEY_PAIR_ID'
CLOUDFRONT_URL_ENV = 'ANYAICAM_CLOUDFRONT_URL'


def _cloudfront_base_url() -> str:
    return os.environ.get(CLOUDFRONT_URL_ENV, '').strip().rstrip('/')


def _segment_filename(segment_key, expected_prefix) -> str | None:
    """Strips the camera's own authorized prefix off a stored manifest
    key. Returns None (never raises) if segment_key isn't a string, or
    doesn't start with the exact prefix for this camera -- the caller
    treats None as "malformed, drop this entry", the same isolation
    principle already used for the write-side prefix check in
    appliance_cloud.live_relay_segment_available(). The remaining filename
    shape itself is validated by sign_segment_url() below -- not
    duplicated here."""
    if not isinstance(segment_key, str) or not segment_key.startswith(expected_prefix):
        return None
    return segment_key[len(expected_prefix):]


def _valid_sequence(value) -> int | None:
    """A segment entry with a missing/invalid sequence is malformed and
    dropped -- never defaulted to 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def render_playlist(
    manifest: dict,
    *,
    expected_prefix: str,
    cloudfront_base_url: str,
    customer_id: str,
    site_id: str,
    appliance_id: str,
    camera_id: str,
    key_id: str,
    rsa_signer: Callable[[bytes], bytes],
) -> str:
    """Renders the playlist text from already-fetched manifest state. Each
    stored segment entry is validated and signed independently -- one
    malformed or unsignable entry is skipped, never fatal to the render
    (batch isolation, mirroring the evaluate_scan_results() precedent).
    Ordering is the manifest's own stored (arrival) order, not re-sorted by
    `sequence` -- see app/live_cdn_signing.py's module docstring and
    docs/AI_HANDOFF.md's Phase 6b design notes for why."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{SEGMENT_TARGET_DURATION}"]

    rendered: list[tuple[int, str]] = []
    for entry in manifest.get("segments", []):
        if not isinstance(entry, dict):
            continue
        sequence = _valid_sequence(entry.get("sequence"))
        filename = _segment_filename(entry.get("key"), expected_prefix)
        if sequence is None or filename is None:
            logger.warning("live_playlist.malformed_manifest_entry camera_id=%s", camera_id)
            continue
        try:
            signed_url = sign_segment_url(
                cloudfront_base_url=cloudfront_base_url,
                customer_id=customer_id,
                site_id=site_id,
                appliance_id=appliance_id,
                camera_id=camera_id,
                segment_filename=filename,
                key_id=key_id,
                rsa_signer=rsa_signer,
            )
        except ValueError:
            logger.warning("live_playlist.unsignable_segment_filename camera_id=%s", camera_id)
            continue
        rendered.append((sequence, signed_url))

    media_sequence = rendered[0][0] if rendered else 0
    lines.append(f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}")
    for _sequence, signed_url in rendered:
        lines.append(f"#EXTINF:{SEGMENT_TARGET_DURATION}.0,")
        lines.append(signed_url)
    # No #EXT-X-ENDLIST -- this is a live, potentially-still-active stream;
    # emitting it would incorrectly tell hls.js the stream has ended.
    return "\n".join(lines) + "\n"


def register_live_playlist_routes(app: FastAPI, *, hls_folder=None, local_identity=lambda: None) -> None:
    def customer_owner(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
            raise HTTPException(status_code=403, detail='Customer owner permission required.')
        return identity

    def authorized_camera(request: Request, camera_id: str):
        identity = customer_owner(request)
        with connection() as db:
            camera = db.execute(
                'SELECT * FROM cameras WHERE id=? AND customer_id=?',
                (camera_id, identity['customer_id']),
            ).fetchone()
            if not camera:
                raise HTTPException(status_code=404, detail='Camera not found.')

            # partner_identity()'s session-cookie payload carries email/role/
            # customer_id/partner_id/session_id/expires -- it does not carry
            # partner_users.id, which customer_camera_permissions.user_id is
            # keyed on (same resolution notification_engine.py's caller
            # already performs before querying this same table).
            user = db.execute(
                'SELECT id FROM partner_users WHERE lower(email)=lower(?) AND customer_id=?',
                (identity['email'],identity['customer_id']),
            ).fetchone()
            if not user:
                raise HTTPException(status_code=403, detail='Customer owner permission required.')

            if identity.get('role') != 'customer_owner':
                permission = db.execute(
                    'SELECT can_live FROM customer_camera_permissions WHERE user_id=? AND camera_id=?',
                    (user['id'], camera_id),
                ).fetchone()
                if not permission or not permission['can_live']:
                    raise HTTPException(status_code=403, detail='Not authorized to view this camera live.')

            camera_number = resolve_camera_number(db, camera_id, camera['appliance_id'], identity['customer_id'])

        if camera_number is None:
            raise HTTPException(status_code=409, detail='Camera has no assigned relay slot; live view is unavailable.')

        return dict(camera), camera_number

    def local_context(request, camera_id):
        camera, number = authorized_camera(request,camera_id)
        if hls_folder is None or _cloudfront_base_url() or os.environ.get(CLOUDFRONT_KEY_PAIR_ID_ENV,'').strip():
            raise HTTPException(status_code=503,detail='Local live view is unavailable.')
        with connection() as db:
            require_local_camera(camera,local_identity(),settings.runtime_role,db)
        return camera,number

    @app.get('/api/customer/cameras/{camera_id}/live/segments/{segment_name}')
    def live_segment(request: Request,camera_id: str,segment_name: str):
        camera,number=local_context(request,camera_id)
        return FileResponse(segment_path(hls_folder,number,segment_name),media_type='video/mp2t',headers={'Cache-Control':'no-store'})

    # One still frame from the camera's live stream (2026-09-28), for the
    # Smart Rules drawing canvas. Capturing from the browser's <video> is
    # fragile -- nothing until the relay's first segment decodes, and some
    # browsers (Safari's native HLS) draw it black -- so the canvas can ask
    # the server instead: the newest relay segment (cloud) or local HLS
    # segment (appliance), decoded to one JPEG. Same tenant/permission
    # check as the live playlist.
    def _ts_to_jpeg(ts_bytes: bytes) -> bytes | None:
        import subprocess
        try:
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "mpegts", "-i", "pipe:0", "-frames:v", "1",
                 "-f", "image2", "-vcodec", "mjpeg", "-q:v", "3", "pipe:1"],
                input=ts_bytes, capture_output=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout or None

    @app.get('/api/customer/cameras/{camera_id}/live/still.jpg')
    def live_still(request: Request, camera_id: str) -> Response:
        camera, camera_number = authorized_camera(request, camera_id)
        segment_bytes = None
        rsa_signer = get_configured_signer()
        key_id = os.environ.get(CLOUDFRONT_KEY_PAIR_ID_ENV, '').strip()
        cloudfront_base_url = _cloudfront_base_url()
        if rsa_signer is not None and key_id and cloudfront_base_url:
            manifest = live_manifest_store.manifest_for(camera_id)
            updated_at = manifest.get('updated_at')
            if updated_at is not None and (time.time() - updated_at) <= STALE_MANIFEST_SECONDS:
                playlist_text = render_playlist(
                    manifest,
                    expected_prefix=live_relay_s3_prefix(camera['customer_id'], camera['site_id'], camera['appliance_id'], camera_id),
                    cloudfront_base_url=cloudfront_base_url, customer_id=camera['customer_id'], site_id=camera['site_id'],
                    appliance_id=camera['appliance_id'], camera_id=camera_id, key_id=key_id, rsa_signer=rsa_signer,
                )
                urls = [line for line in playlist_text.splitlines() if line.startswith('http')]
                if urls:
                    import requests
                    try:
                        fetched = requests.get(urls[-1], timeout=10)
                        if fetched.ok:
                            segment_bytes = fetched.content
                    except requests.RequestException:
                        segment_bytes = None
        else:
            camera, camera_number = local_context(request, camera_id)
            import re as _re
            from pathlib import Path as _Path
            folder = _Path(hls_folder)
            pattern = _re.compile(rf'camera{int(camera_number)}_[0-9]+[.]ts')
            segments = sorted((p for p in folder.glob(f'camera{int(camera_number)}_*.ts') if pattern.fullmatch(p.name)),
                              key=lambda p: p.stat().st_mtime)
            for candidate in reversed(segments[-3:-1] or segments[-1:]):  # a finished segment, not the one being written
                try:
                    segment_bytes = candidate.read_bytes()
                    break
                except OSError:
                    continue
        jpeg = _ts_to_jpeg(segment_bytes) if segment_bytes else None
        if not jpeg:
            raise HTTPException(status_code=503, detail='No live frame is available yet. Open the live view and try again in a few seconds.')
        return Response(content=jpeg, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    @app.get('/api/customer/cameras/{camera_id}/live/playlist.m3u8')
    def live_playlist(request: Request, camera_id: str) -> Response:
        camera,camera_number=authorized_camera(request,camera_id)
        rsa_signer = get_configured_signer()
        key_id = os.environ.get(CLOUDFRONT_KEY_PAIR_ID_ENV, '').strip()
        cloudfront_base_url = _cloudfront_base_url()
        if rsa_signer is None or not key_id or not cloudfront_base_url:
            camera,camera_number=local_context(request,camera_id)
            return Response(local_playlist(hls_folder,camera_number,camera_id,STALE_MANIFEST_SECONDS),
                            media_type='application/vnd.apple.mpegurl',headers={'Cache-Control':'no-store'})

        # Relay viewer demand (2026-09-24): this fetch IS the "someone is
        # watching relay video" signal -- HLS players re-fetch the live
        # playlist every segment. Refreshes this viewer's session heartbeat
        # and, if the relay was idle-stopped while they were away, queues
        # it to start again (see live_relay_idle_sweep.py). Never allowed
        # to fail the playlist response itself.
        try:
            viewer = partner_identity(request) or {}
            with connection() as db:
                record_relay_viewer_activity(
                    db, camera_id=camera_id, customer_id=camera['customer_id'],
                    requested_by=str(viewer.get('email') or ''), now=datetime.now(),
                )
        except Exception:
            logger.exception('live_playlist.relay_viewer_activity_failed camera_id=%s', camera_id)

        expected_prefix = live_relay_s3_prefix(camera['customer_id'], camera['site_id'], camera['appliance_id'], camera_id)
        manifest = live_manifest_store.manifest_for(camera_id)
        updated_at = manifest.get('updated_at')
        if updated_at is not None and (time.time() - updated_at) > STALE_MANIFEST_SECONDS:
            manifest = {"segments": [], "updated_at": updated_at}

        playlist_text = render_playlist(
            manifest,
            expected_prefix=expected_prefix,
            cloudfront_base_url=cloudfront_base_url,
            customer_id=camera['customer_id'],
            site_id=camera['site_id'],
            appliance_id=camera['appliance_id'],
            camera_id=camera_id,
            key_id=key_id,
            rsa_signer=rsa_signer,
        )
        return Response(
            content=playlist_text,
            media_type='application/vnd.apple.mpegurl',
            headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
        )
