"""Phase 6b (docs/AI_HANDOFF.md Sec 8): dynamic, per-request HLS playlist
generation for the live-relay path -- serves a customer-authenticated
.m3u8 built from the current live_manifest_store state, with each segment
URI a freshly CloudFront-signed URL (live_cdn_signing.py). No media bytes
ever pass through this module or FastAPI -- only tiny playlist text.

Reuses, never redesigns: appliance_cloud.live_manifest_store (Phase 2),
camera_mapping.resolve_camera_number (Phase 6a), appliance_protocol's
live_relay_s3_prefix (Phase 1/2), live_cdn_signing.sign_segment_url
(Phase 6b). Does not touch the Phase 2/3/4 relay/uploader implementation.

CloudFront/S3 requires real AWS infrastructure (a CloudFront distribution,
a signing key in Secrets Manager, an S3 relay bucket) that, as of this
writing, is provisioned in none of this repo's env templates (.env,
aws.env both leave ANYAICAM_CLOUDFRONT_URL empty/placeholder, and no
ANYAICAM_CLOUDFRONT_KEY_PAIR_ID or ANYAICAM_CLOUDFRONT_SIGNING_KEY_* var
exists anywhere) -- get_configured_signer() (live_cdn_signing.py)
unconditionally returns None until that infrastructure exists, so this
was always meant to fail closed with 503 for a deployment that hasn't
done that provisioning. It remains the intended path for a future
cloud/remote-relay deployment and is left fully intact below.

An edge (or combined) appliance showing its OWN cameras to its OWN
customers has no need for any of that, though: process_supervisor(...,
"live") (main.py) already keeps a local FFmpeg HLS process alive per
camera continuously, independent of any viewer, writing
HLS_FOLDER/camera{N}.m3u8 + its .ts segments -- the exact same files the
partner/admin Live View already plays successfully via the open
/static/hls mount. When CloudFront isn't configured, this route now
serves THAT local playlist instead, rewriting each segment reference to
this module's own new, customer-scoped, authenticated segment route
(never the open /static/hls mount, and never a customer_id/camera_id a
caller doesn't already own) rather than 503ing forever. No RTSP/ONVIF/
FFmpeg code is touched by this -- it only ever reads .m3u8/.ts files
main.py's existing, unchanged live-view pipeline already produces.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

from appliance_cloud import live_manifest_store
from appliance_protocol import live_relay_s3_prefix
from camera_mapping import resolve_camera_number
from live_cdn_signing import get_configured_signer, sign_segment_url
from partner_db import connection
from partner_portal import partner_identity

logger = logging.getLogger('anyaicam.live_playlist')

STALE_MANIFEST_SECONDS = 30
SEGMENT_TARGET_DURATION = 2  # must match start_live_stream()'s FFmpeg -hls_time;
# duplicated as a constant rather than imported from main.py, which would be
# a circular import (main.py is what will register this module's routes).

CLOUDFRONT_KEY_PAIR_ID_ENV = 'ANYAICAM_CLOUDFRONT_KEY_PAIR_ID'
CLOUDFRONT_URL_ENV = 'ANYAICAM_CLOUDFRONT_URL'

# Same literal path as main.py's own HLS_FOLDER -- duplicated, not imported,
# for the identical circular-import reason SEGMENT_TARGET_DURATION above
# already is: main.py imports this module to register its routes. This is
# the one place on disk start_live_stream()'s FFmpeg process (main.py) and
# this module's local-fallback playlist rendering both have to agree on.
HLS_FOLDER = Path('/app/static/hls')


def _cloudfront_base_url() -> str:
    return os.environ.get(CLOUDFRONT_URL_ENV, '').strip().rstrip('/')


def _local_segment_pattern(camera_number: int) -> re.Pattern:
    """Same shape as live_relay_uploader.py's own per-camera-number segment
    filename check (`^camera{N}[0-9_.\\-]*\\.ts$`) -- kept as a literal
    duplicate rather than imported so this module never depends on the
    S3-relay-only uploader module just to validate a filename pattern."""
    return re.compile(rf'^camera{camera_number}[0-9_.\-]*\.ts$')


def render_local_playlist(camera_number: int, camera_id: str) -> str | None:
    """Reads HLS_FOLDER/camera{N}.m3u8 -- the same file
    process_supervisor(camera_number, "live") (main.py) already keeps
    continuously current for as long as that camera's local FFmpeg process
    is alive, whether or not any customer is currently watching -- and
    rewrites every segment line into this module's own authenticated,
    per-customer/per-camera-scoped segment route instead of a bare
    filename, so knowing a segment's name is never enough to fetch it
    without also passing this camera's own authorization check.

    Returns None when there is nothing live to serve yet: the file doesn't
    exist, can't be read, or its mtime is older than STALE_MANIFEST_SECONDS
    (the local FFmpeg process died or was never started for this camera
    number -- an untouched, pre-existing main.py concern, not something
    this function tries to diagnose further). Callers render an
    empty-but-valid playlist for that case rather than an error response,
    so the browser's existing poll loop (live_view_page.py) just keeps
    waiting instead of treating a cold-start race as a permanent failure --
    exactly the same "keep polling" outcome an empty real manifest already
    produces on the CloudFront path below.

    A segment line that doesn't match this exact camera's own
    "camera{N}...ts" filename shape is dropped (logged, not fatal) --
    mirrors live_relay_uploader.py's identical batch-isolation principle
    for its own read of this same file shape."""
    manifest_path = HLS_FOLDER / f'camera{camera_number}.m3u8'
    try:
        mtime = manifest_path.stat().st_mtime
        text = manifest_path.read_text(encoding='utf-8')
    except OSError:
        return None
    if (time.time() - mtime) > STALE_MANIFEST_SECONDS:
        return None

    pattern = _local_segment_pattern(camera_number)
    encoded_camera_id = quote(camera_id, safe='')
    lines = ['#EXTM3U']
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith('#'):
            if stripped != '#EXTM3U':
                lines.append(stripped)
            continue
        name = Path(stripped).name  # discard any directory component defensively
        if not pattern.fullmatch(name):
            logger.warning('live_playlist.local_segment_rejected camera_id=%s entry=%r', camera_id, stripped)
            continue
        lines.append(f'/api/customer/cameras/{encoded_camera_id}/live/segments/{quote(name, safe="")}')
    return '\n'.join(lines) + '\n'


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


def register_live_playlist_routes(app: FastAPI) -> None:
    def customer_owner(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get('role') != 'customer_owner':
            raise HTTPException(status_code=403, detail='Customer owner permission required.')
        return identity

    def _authorized_customer_camera(db, request: Request, camera_id: str) -> tuple[dict, int]:
        """Camera lookup + can_live permission check + relay-slot resolution,
        shared by both routes this function registers (the playlist route
        and the local-segment route below). Unlike this module's usual
        convention of duplicating this exact check across separate MODULES
        (see the top-of-file docstrings on live_view_page.py/
        live_view_sessions.py for why that cross-module duplication is
        deliberate), duplicating it a third time inside this one file would
        only risk these two routes drifting out of sync with each other for
        no isolation benefit -- so here, and only here, it's shared."""
        identity = customer_owner(request)
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
            'SELECT id FROM partner_users WHERE email=?',
            (identity['email'],),
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

    @app.get('/api/customer/cameras/{camera_id}/live/playlist.m3u8')
    def live_playlist(request: Request, camera_id: str) -> Response:
        with connection() as db:
            camera, camera_number = _authorized_customer_camera(db, request, camera_id)

        rsa_signer = get_configured_signer()
        key_id = os.environ.get(CLOUDFRONT_KEY_PAIR_ID_ENV, '').strip()
        cloudfront_base_url = _cloudfront_base_url()

        if rsa_signer is not None and key_id and cloudfront_base_url:
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
        else:
            # No CloudFront/S3 relay configured (the normal, expected state
            # for an edge/combined appliance serving its own cameras to its
            # own customers -- see module docstring) -- serve the local HLS
            # output main.py's own live-view pipeline already produces for
            # this camera instead of failing closed forever.
            playlist_text = render_local_playlist(camera_number, camera_id) or (
                '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n'
            )

        return Response(
            content=playlist_text,
            media_type='application/vnd.apple.mpegurl',
            headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
        )

    @app.get('/api/customer/cameras/{camera_id}/live/segments/{segment_name}')
    def live_segment(request: Request, camera_id: str, segment_name: str) -> FileResponse:
        """Serves exactly one local .ts segment for one camera this identity
        is authorized to view live -- the customer-scoped counterpart to the
        open /static/hls mount main.py's partner/admin Live View already
        uses, never the mount itself, so a customer session can never reach
        another camera's (or another customer's) segment merely by knowing
        or guessing a filename. Only ever reached via render_local_playlist()
        above's own rewritten URLs; this route re-validates independently
        rather than trusting that a request actually came from there."""
        with connection() as db:
            _camera, camera_number = _authorized_customer_camera(db, request, camera_id)

        # segment_name arrives as a single FastAPI path segment (no "/" can
        # survive routing into it uninterpreted), but Path(...).name plus an
        # exact resolve()-and-compare-parent check below is kept anyway as
        # the same belt-and-suspenders defense live_relay_uploader.py's own
        # local-segment handling already applies to this identical file
        # naming convention -- never trust a single layer for path safety.
        name = Path(segment_name).name
        if not _local_segment_pattern(camera_number).fullmatch(name):
            raise HTTPException(status_code=404, detail='Segment not found.')

        candidate = HLS_FOLDER / name
        try:
            resolved = candidate.resolve()
        except OSError:
            raise HTTPException(status_code=404, detail='Segment not found.')
        if resolved.parent != HLS_FOLDER.resolve() or not resolved.is_file():
            raise HTTPException(status_code=404, detail='Segment not found.')

        return FileResponse(
            resolved,
            media_type='video/mp2t',
            headers={'Cache-Control': 'no-store'},
        )
