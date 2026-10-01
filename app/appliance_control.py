"""Real-time commands from the cloud to an appliance (2026-10-01).

Uses the appliance's existing authenticated control WebSocket
(talk_audio_relay: /api/appliance/talk/channel), which every appliance keeps
open, instead of the agent's command queue (polled about once a minute --
far too slow for a door).

- send(): fire-and-forget, e.g. "facial_directory_changed" so a revoked
  person stops being able to open doors now, not at the next 5-minute sync.
- request(): send and wait for the matching "<type>_result" (by request_id),
  e.g. "door_unlock" from the customer portal.

Callable from worker threads (FastAPI sync routes): coroutines run on the
event loop that owns the WebSocket. Never raises; a disconnected appliance
or a timeout is reported as such, and a door is then left closed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading

logger = logging.getLogger("anyaicam.appliance_control")

_loop: asyncio.AbstractEventLoop | None = None
_pending: dict[str, tuple[str, asyncio.Future]] = {}
_lock = threading.Lock()


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def _channel(appliance_id: str):
    import talk_audio_relay
    return talk_audio_relay._appliance_channels.get(appliance_id)


def connected(appliance_id: str | None) -> bool:
    return bool(appliance_id) and _channel(appliance_id) is not None


async def _send_async(appliance_id: str, message: dict) -> bool:
    channel = _channel(appliance_id)
    if channel is None:
        return False
    try:
        await channel.send_text(json.dumps(message))
        return True
    except Exception as error:  # a dropped socket is a "not sent", never an exception to the caller
        logger.warning("appliance_control.send_failed appliance_id=%s type=%s error=%s", appliance_id, message.get("type"), type(error).__name__)
        return False


def send(appliance_id: str, message: dict) -> bool:
    if _loop is None or not connected(appliance_id):
        return False
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _loop:
        _loop.create_task(_send_async(appliance_id, message))
        return True
    try:
        return asyncio.run_coroutine_threadsafe(_send_async(appliance_id, message), _loop).result(timeout=5)
    except Exception:
        return False


async def _request_async(appliance_id: str, message: dict, timeout: float) -> dict | None:
    request_id = message.setdefault("request_id", secrets.token_hex(12))
    future = asyncio.get_running_loop().create_future()
    with _lock:
        _pending[request_id] = (appliance_id, future)
    try:
        if not await _send_async(appliance_id, message):
            return None
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        return {"status": "timeout"}
    finally:
        with _lock:
            _pending.pop(request_id, None)


def request(appliance_id: str, message: dict, *, timeout: float = 10.0) -> dict | None:
    """The appliance's result dict, {"status": "timeout"}, or None when the
    appliance is not connected (nothing was sent)."""
    if _loop is None or not connected(appliance_id):
        return None
    try:
        return asyncio.run_coroutine_threadsafe(_request_async(appliance_id, dict(message), timeout), _loop).result(timeout=timeout + 2)
    except Exception:
        return {"status": "timeout"}


def resolve(appliance_id: str, message: dict) -> bool:
    """Called on the event loop for every appliance message; True when it
    was a result for a pending request from THIS appliance."""
    request_id = message.get("request_id")
    if not isinstance(request_id, str):
        return False
    with _lock:
        entry = _pending.get(request_id)
    if not entry or entry[0] != appliance_id:  # never accept another appliance's answer
        return False
    if not entry[1].done():
        entry[1].set_result(message)
    return True


def notify_facial_directory_changed(customer_id: str) -> int:
    """Ask every connected appliance of this customer to re-sync faces and
    door grants now. Returns how many were told."""
    from partner_db import rows
    told = 0
    for appliance in rows("SELECT id FROM appliances WHERE customer_id=?", (customer_id,)):
        told += bool(send(appliance["id"], {"type": "facial_directory_changed"}))
    return told
