"""Edge-local camera provisioning, credential validation, and preview capture."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape


CONFIG_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(value: str) -> str:
    name = " ".join(str(value).split())
    if not 2 <= len(name) <= 80:
        raise ValueError("Camera name must be between 2 and 80 characters.")
    return name


def private_camera_url(value: str, schemes: set[str]) -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme.lower() not in schemes or not parsed.hostname:
        raise ValueError(f"Camera URL must use {', '.join(sorted(schemes))}.")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise ValueError("Camera URLs must use literal private IP addresses.") from error
    if address.version == 4:
        allowed = any(address in block for block in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("169.254.0.0/16"),
        ))
    else:
        allowed = address in ipaddress.ip_network("fc00::/7") or address.is_link_local
    if not allowed:
        raise ValueError("Camera URL is outside the Edge private network.")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))


def credentialed_rtsp_url(value: str, username: str, password: str) -> str:
    safe = private_camera_url(value, {"rtsp"})
    parsed = urlsplit(safe)
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    netloc = (auth + "@" if auth else "") + host
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def _fraction(value: str) -> float:
    try:
        numerator, denominator = str(value).split("/", 1)
        return round(float(numerator) / float(denominator), 2) if float(denominator) else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


class CameraConfigurationStore:
    """Persistent secrets stay on the Edge and are redacted from every API view."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("cameras"), list):
                return value
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": CONFIG_VERSION, "updated_at": None, "cameras": []}

    def safe_view(self) -> dict:
        value = self.load()
        return {
            **value,
            "cameras": [self.redact(camera) for camera in value["cameras"]],
        }

    @staticmethod
    def redact(camera: dict) -> dict:
        safe = {
            key: value for key, value in camera.items()
            if key not in {"username", "password"}
        }
        for key, schemes in (("rtsp_url", {"rtsp"}), ("onvif_url", {"http", "https"})):
            if safe.get(key):
                try:
                    safe[key] = private_camera_url(str(safe[key]), schemes)
                except ValueError:
                    safe[key] = ""
        return safe | {
            "credentials_configured": bool(camera.get("username")),
            "password_configured": bool(camera.get("password")),
        }

    def get(self, camera_id: str) -> dict | None:
        return next((item for item in self.load()["cameras"] if item.get("id") == camera_id), None)

    def by_number(self, camera_number: int) -> dict | None:
        return next(
            (item for item in self.load()["cameras"] if int(item.get("camera_number") or 0) == camera_number and item.get("enabled", True)),
            None,
        )

    def save_camera(self, camera: dict) -> dict:
        current = self.load()
        now = utc_now()
        prior = next((item for item in current["cameras"] if item.get("id") == camera["id"]), {})
        record = {
            **prior,
            **camera,
            "created_at": prior.get("created_at") or now,
            "updated_at": now,
        }
        current["cameras"] = [item for item in current["cameras"] if item.get("id") != record["id"]]
        current["cameras"].append(record)
        current["cameras"].sort(key=lambda item: (int(item.get("camera_number") or 0), str(item.get("id"))))
        current["updated_at"] = now
        self._save(current)
        return self.redact(record)

    def rename(self, camera_id: str, name: str) -> dict:
        record = self.get(camera_id)
        if not record:
            raise KeyError(camera_id)
        record["name"] = normalize_name(name)
        return self.save_camera(record)

    def _save(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)


class CameraProbeService:
    def __init__(self, runner=subprocess.run, opener=urlopen):
        self.runner = runner
        self.opener = opener

    def validate_rtsp(self, rtsp_url: str, username: str = "", password: str = "") -> dict:
        stream_url = credentialed_rtsp_url(rtsp_url, username, password)
        try:
            result = self.runner(
                [
                    "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                    "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,bit_rate:format=bit_rate",
                    "-of", "json", stream_url,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"valid": False, "error": "RTSP validation timed out or FFprobe is unavailable."}
        if result.returncode:
            return {"valid": False, "error": "RTSP authentication or stream validation failed."}
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return {"valid": False, "error": "FFprobe returned invalid stream metadata."}
        video = next((item for item in payload.get("streams", []) if item.get("codec_type") == "video"), None)
        if not video:
            return {"valid": False, "error": "RTSP endpoint did not expose a video stream."}
        fps = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
        bitrate = int(video.get("bit_rate") or payload.get("format", {}).get("bit_rate") or 0)
        return {
            "valid": True,
            "codec": str(video.get("codec_name") or "unknown")[:32],
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "fps": fps,
            "bitrate_bps": bitrate,
        }

    def validate_onvif(self, onvif_url: str, username: str, password: str) -> dict:
        endpoint = private_camera_url(onvif_url, {"http", "https"})
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = secrets.token_bytes(16)
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
        nonce_text = base64.b64encode(nonce).decode()
        envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"><s:Header><wsse:Security><wsse:UsernameToken><wsse:Username>{xml_escape(username)}</wsse:Username><wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password><wsse:Nonce>{nonce_text}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header><s:Body><GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>'''.encode()
        request = Request(endpoint, data=envelope, method="POST", headers={"Content-Type": 'application/soap+xml; charset="utf-8"'})
        try:
            with self.opener(request, timeout=8) as response:
                payload = response.read(1024 * 1024)
            root = ET.fromstring(payload)
        except (OSError, ValueError, ET.ParseError):
            return {"valid": False, "error": "ONVIF credential validation failed."}
        values = {}
        for element in root.iter():
            name = element.tag.rsplit("}", 1)[-1]
            if name in {"Manufacturer", "Model", "FirmwareVersion", "SerialNumber"} and element.text:
                values[name.lower()] = element.text.strip()[:120]
        if not values:
            return {"valid": False, "error": "ONVIF device information was not returned."}
        return {"valid": True, **values}

    def thumbnail(self, rtsp_url: str, username: str = "", password: str = "") -> bytes:
        stream_url = credentialed_rtsp_url(rtsp_url, username, password)
        try:
            result = self.runner(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", stream_url, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                capture_output=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Camera preview timed out or FFmpeg is unavailable.") from error
        if result.returncode or not result.stdout:
            raise RuntimeError("Camera did not return a preview frame.")
        return result.stdout
