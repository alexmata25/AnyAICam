import asyncio
import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from enum import Enum
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

HLS_FOLDER = Path("/app/static/hls")
RECORDINGS_FOLDER = Path("/app/recordings")
CLIPS_FOLDER = RECORDINGS_FOLDER / "clips"
MEDIA_FOLDER = RECORDINGS_FOLDER / "media"
SNAPSHOTS_FOLDER = MEDIA_FOLDER / "snapshots"
MOTION_THUMBNAILS_FOLDER = MEDIA_FOLDER / "motion"
MOTION_EVENTS_FILE = RECORDINGS_FOLDER / "motion_events.jsonl"
EVENT_SETTINGS_FILE = RECORDINGS_FOLDER / "event_settings.json"
ALERT_RULES_FILE = RECORDINGS_FOLDER / "alert_rules.json"
IN_APP_ALERTS_FILE = RECORDINGS_FOLDER / "in_app_alerts.jsonl"
PARTNER_CUSTOMERS_FILE = RECORDINGS_FOLDER / "partner_customers.json"
ANALYTICS_RULES_FILE = RECORDINGS_FOLDER / "analytics_rules.json"
ANALYTICS_EVENTS_FILE = RECORDINGS_FOLDER / "analytics_events.json"
EVENT_REVIEWS_FILE = RECORDINGS_FOLDER / "event_reviews.json"
CAMERA_COUNT = 4

HLS_FOLDER.mkdir(parents=True, exist_ok=True)
RECORDINGS_FOLDER.mkdir(parents=True, exist_ok=True)
CLIPS_FOLDER.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_FOLDER.mkdir(parents=True, exist_ok=True)
MOTION_THUMBNAILS_FOLDER.mkdir(parents=True, exist_ok=True)

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
MOTION_DETECTION_ENABLED = os.environ.get("MOTION_DETECTION_ENABLED", "true").lower() == "true"
MOTION_THRESHOLD = float(os.environ.get("MOTION_THRESHOLD", "12"))
MOTION_COOLDOWN_SECONDS = int(os.environ.get("MOTION_COOLDOWN_SECONDS", "15"))
ffmpeg_processes: list[subprocess.Popen] = []
camera_process_state = {
    camera_number: {"live": "starting", "recording": "starting"}
    for camera_number in range(1, CAMERA_COUNT + 1)
}
camera_reconnect_counts = {camera_number: 0 for camera_number in range(1, CAMERA_COUNT + 1)}
health_issues: dict[str, dict] = {}
health_alert_times: dict[str, float] = {}


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
    id: str
    display_name: str
    role: str = "viewer"
    enabled: bool = True


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
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-tune",
        "zerolatency", "-f", "hls", "-hls_time", "2", "-hls_list_size",
        "5", "-hls_flags", "delete_segments+append_list", output_file,
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
        "-map", "0:v:0", "-an", "-c:v", "copy", "-f", "segment",
        "-segment_time", "300", "-reset_timestamps", "1", "-strftime",
        "1", output_pattern,
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


def append_in_app_alert(alert: dict) -> None:
    with IN_APP_ALERTS_FILE.open("a", encoding="utf-8") as alert_file:
        alert_file.write(json.dumps(alert, separators=(",", ":")) + "\n")


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
        print(f"Could not create motion thumbnail: {error}")
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
    ffmpeg_processes = []
    supervisor_tasks = []
    for camera_number in range(1, CAMERA_COUNT + 1):
        supervisor_tasks.append(
            asyncio.create_task(
                process_supervisor(camera_number, "live")
            )
        )
        supervisor_tasks.append(
            asyncio.create_task(
                process_supervisor(camera_number, "recording")
            )
        )
    motion_tasks = []
    if MOTION_DETECTION_ENABLED:
        motion_tasks = [
            asyncio.create_task(motion_detector(camera_number))
            for camera_number in range(1, CAMERA_COUNT + 1)
        ]
    health_task = asyncio.create_task(health_monitor())
    retention_task = asyncio.create_task(retention_worker())
    try:
        yield
    finally:
        retention_task.cancel()
        for task in supervisor_tasks:
            task.cancel()
        for task in motion_tasks:
            task.cancel()
        health_task.cancel()
        try:
            await retention_task
        except asyncio.CancelledError:
            pass
        await asyncio.gather(*supervisor_tasks, return_exceptions=True)
        await asyncio.gather(*motion_tasks, return_exceptions=True)
        await asyncio.gather(health_task, return_exceptions=True)
        for process in ffmpeg_processes:
            if process.poll() is None:
                process.terminate()
        for process in ffmpeg_processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


from cloud_config import configure_logging, settings as cloud_settings
from cloud_security import ProductionSecurityMiddleware

configure_logging()
cloud_settings.validate()
app = FastAPI(title="AnyAiCam VMS", lifespan=lifespan)
app.add_middleware(ProductionSecurityMiddleware)
if cloud_settings.deployed:
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=settings.trusted_hosts)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")
app.mount("/recordings", StaticFiles(directory="/app/recordings"), name="recordings")


STYLES = """
:root{color-scheme:dark;--bg:#0a0d12;--panel:#121720;--panel2:#181e28;--line:#27303d;--text:#f4f7fb;--muted:#8f9baa;--accent:#47d7ac;--blue:#70a5ff;--danger:#ff6b6b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}.shell{min-height:100vh;display:grid;grid-template-columns:230px 1fr}.sidebar{position:sticky;top:0;height:100vh;padding:24px 16px;border-right:1px solid var(--line);background:#0e1218}.brand{display:flex;align-items:center;gap:11px;padding:0 9px 28px;font-weight:750;letter-spacing:-.02em}.brand-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:var(--accent);color:#07110e;font-size:18px}.nav{display:grid;gap:6px}.nav a{display:flex;align-items:center;gap:12px;padding:12px 13px;border-radius:10px;color:var(--muted);text-decoration:none;font-weight:650}.nav a:hover{background:var(--panel2);color:var(--text)}.nav a.active{background:#193329;color:#7ee8c7}.sidebar-foot{position:absolute;left:16px;right:16px;bottom:22px;padding:13px;border:1px solid var(--line);border-radius:12px;color:var(--muted);font-size:12px}.sidebar-foot strong{display:block;color:var(--text);font-size:13px;margin-bottom:3px}.content{min-width:0;padding:30px 34px 50px;max-width:1600px;width:100%;margin:0 auto}.topbar{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:26px}.eyebrow{margin:0 0 5px;color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}h1{margin:0;font-size:clamp(25px,3vw,34px);letter-spacing:-.035em}h2{margin:0;font-size:18px}.clock{color:var(--muted);font-size:13px}.summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:24px}.stat{padding:16px 18px;border:1px solid var(--line);border-radius:14px;background:var(--panel)}.stat-label{display:block;color:var(--muted);font-size:12px;margin-bottom:7px}.stat-value{font-size:18px;font-weight:750}.dot{display:inline-block;width:8px;height:8px;margin-right:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px rgba(71,215,172,.1)}.section-head{display:flex;align-items:end;justify-content:space-between;margin:0 0 14px}.section-head p{margin:0;color:var(--muted);font-size:13px}.camera-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.camera-card{overflow:hidden;border:1px solid var(--line);border-radius:16px;background:var(--panel)}.camera-view{position:relative;display:grid;place-items:center;aspect-ratio:16/9;background:#080a0e;overflow:hidden}.camera-view video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#080a0e}.camera-placeholder{text-align:center;color:var(--muted);padding:20px}.camera-placeholder .signal{display:block;margin:0 auto 12px;font-size:25px}.camera-placeholder strong{display:block;color:#cad2dd;margin-bottom:4px}.live-badge{position:absolute;z-index:2;top:12px;left:12px;padding:6px 9px;border-radius:8px;background:rgba(8,10,14,.78);font-size:11px;font-weight:800;letter-spacing:.08em}.live-badge::before{content:"";display:inline-block;width:7px;height:7px;margin-right:6px;border-radius:50%;background:var(--danger)}.camera-meta{display:flex;justify-content:space-between;gap:15px;padding:14px 16px}.camera-name{font-weight:700}.camera-state{color:var(--muted);font-size:12px}.camera-state.ready{color:var(--accent)}.library-toolbar{display:flex;gap:10px;margin-bottom:18px;overflow:auto}.filter{border:1px solid var(--line);border-radius:999px;background:transparent;color:var(--muted);padding:8px 13px;cursor:pointer;white-space:nowrap}.filter.active{background:var(--text);border-color:var(--text);color:#0b0e13}.camera-section{margin-bottom:28px}.recording-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:12px}.clip{overflow:hidden;border:1px solid var(--line);border-radius:14px;background:var(--panel)}.clip video{display:block;width:100%;aspect-ratio:16/9;background:#07090c}.clip-body{padding:13px}.clip-time{font-weight:700;font-size:14px}.clip-meta{margin:5px 0 12px;color:var(--muted);font-size:12px}.download{color:var(--blue);text-decoration:none;font-size:13px;font-weight:700}.empty{padding:28px;border:1px dashed var(--line);border-radius:14px;color:var(--muted);text-align:center;background:rgba(18,23,32,.5)}.mobile-nav{display:none}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}@media(max-width:980px){.recording-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:760px){.shell{display:block}.sidebar{display:none}.content{padding:23px 16px 90px}.clock{display:none}.summary{grid-template-columns:1fr}.camera-grid,.recording-grid{grid-template-columns:1fr}.mobile-nav{position:fixed;z-index:20;display:grid;grid-template-columns:1fr 1fr;left:12px;right:12px;bottom:12px;padding:6px;border:1px solid var(--line);border-radius:15px;background:rgba(18,23,32,.94);backdrop-filter:blur(14px)}.mobile-nav a{text-align:center;padding:10px;border-radius:10px;color:var(--muted);text-decoration:none;font-weight:700;font-size:13px}.mobile-nav a.active{background:#193329;color:#7ee8c7}}
.brand{display:flex;align-items:center;padding:0 9px 28px}.brand-logo{display:block;width:74px;height:52px;object-fit:contain;object-position:left center}
.date-filter{height:36px;padding:0 12px;border:1px solid var(--line);border-radius:999px;background:transparent;color:var(--text);font:inherit;color-scheme:dark}
.layout-controls{display:flex;gap:5px}.layout-button{min-width:34px;height:32px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--muted);cursor:pointer}.layout-button.active{border-color:#627083;background:var(--panel2);color:var(--text)}.camera-grid[data-layout="1"]{grid-template-columns:minmax(0,1fr)}.camera-grid[data-layout="4"]{grid-template-columns:repeat(2,minmax(0,1fr))}.camera-grid[data-layout="9"]{grid-template-columns:repeat(3,minmax(0,1fr))}.camera-grid[data-layout="16"]{grid-template-columns:repeat(4,minmax(0,1fr))}.analytics-overlay{position:absolute;z-index:3;inset:14%;border:2px dashed rgba(65,216,207,.8);border-radius:8px;color:#71ede5;display:none;place-items:center;font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;background:rgba(65,216,207,.04)}.analytics-overlay.visible{display:grid}@media(max-width:1100px){.camera-grid[data-layout="9"],.camera-grid[data-layout="16"]{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:760px){.camera-grid[data-layout]{grid-template-columns:1fr}.layout-controls{display:none}}
.nav-group-label{padding:18px 13px 7px;color:#596575;font-size:10px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.camera-tools{display:flex;gap:4px;padding:8px 10px;border-top:1px solid var(--line);overflow-x:auto}.camera-tool{flex:0 0 auto;display:grid;place-items:center;width:34px;height:32px;border:0;border-radius:8px;background:transparent;color:var(--muted);cursor:pointer;font-size:15px}.camera-tool:hover{background:var(--panel2);color:var(--text)}.toast{position:fixed;z-index:50;right:20px;bottom:20px;padding:12px 16px;border:1px solid var(--line);border-radius:10px;background:#1a202a;color:var(--text);box-shadow:0 12px 35px rgba(0,0,0,.35);opacity:0;transform:translateY(10px);pointer-events:none;transition:.2s}.toast.show{opacity:1;transform:none}.health-grid{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:24px}.panel{padding:19px;border:1px solid var(--line);border-radius:16px;background:var(--panel)}.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}.health-list,.activity-list{display:grid;gap:2px}.health-row,.activity-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:13px 0;border-top:1px solid var(--line)}.health-row:first-child,.activity-row:first-child{border-top:0}.health-name{font-weight:650}.health-detail,.activity-time{color:var(--muted);font-size:12px}.pill{padding:5px 8px;border-radius:999px;background:#193329;color:#7ee8c7;font-size:11px;font-weight:750}.pill.wait{background:#302a1d;color:#f0ca72}.storage-bar{height:9px;margin:15px 0 8px;border-radius:99px;background:#252d38;overflow:hidden}.storage-bar span{display:block;width:18%;height:100%;border-radius:inherit;background:linear-gradient(90deg,#bd2a8b,#41d8cf)}.feature-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.feature-card{min-height:150px;padding:18px;border:1px solid var(--line);border-radius:15px;background:var(--panel)}.feature-icon{display:grid;place-items:center;width:38px;height:38px;margin-bottom:24px;border-radius:10px;background:#242b37;color:#cbd4df}.feature-card p{margin:7px 0 0;color:var(--muted);font-size:13px;line-height:1.45}.settings-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.setting-link{display:flex;align-items:center;justify-content:space-between;padding:17px;border:1px solid var(--line);border-radius:13px;background:var(--panel);color:var(--text);text-decoration:none}.setting-link span{color:var(--muted)}.coming{display:inline-block;margin-top:14px;color:var(--blue);font-size:11px;font-weight:750;text-transform:uppercase;letter-spacing:.08em}@media(max-width:980px){.feature-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.health-grid{grid-template-columns:1fr}}@media(max-width:760px){.feature-grid,.settings-list{grid-template-columns:1fr}.toast{left:16px;right:16px;bottom:82px}}
/* Branded surveillance workspace */
:root{--brand:#43d1cc;--brand-soft:#b9f8f3;--brand-action:#4b4de2;--workspace-top:#174f5f;--workspace-bottom:#424858;--rail:#131515;--surface:#192234;--surface2:#283244;--accent:var(--brand);--panel:var(--surface);--panel2:var(--surface2);--line:#4a5769}
body{background:var(--workspace-bottom)}.shell{grid-template-columns:112px minmax(0,1fr);background:linear-gradient(180deg,var(--workspace-top),var(--workspace-bottom))}.sidebar{z-index:20;width:112px;padding:18px 6px;background:var(--rail);border:0;overflow-y:auto;scrollbar-width:none}.brand{justify-content:center;padding:0 5px 22px}.brand-logo{width:72px;height:68px;object-position:center}.nav{gap:3px}.nav a{min-height:75px;display:flex;flex-direction:column;justify-content:center;gap:5px;padding:8px 4px;border-radius:0;text-align:center;font-size:12px;line-height:1.08;color:#f6f7f8}.nav a:hover{background:#24292c}.nav a.active{background:var(--brand);color:#102a30}.nav-icon{font-size:25px;line-height:1}.sidebar-foot{display:none}.content{max-width:none;padding:24px 34px 48px}.eyebrow{color:var(--brand-soft)}.stat,.panel,.feature-card,.setting-link,.camera-card,.clip{background:rgba(24,33,50,.94);border-color:rgba(170,196,207,.18);box-shadow:0 7px 20px rgba(7,12,20,.12)}.camera-card{border-radius:7px}.camera-tools{background:#121a28}.filter.active,.layout-button.active{background:var(--brand-action);border-color:var(--brand-action);color:white}.download{color:#8df0ea}.pill{background:#315c5d;color:#9ff7f1}.workspace-tabs{display:grid;grid-template-columns:repeat(3,1fr);margin-bottom:24px;padding:7px;border-radius:9px;background:#182234}.workspace-tab{padding:12px;border:0;border-radius:7px;background:transparent;color:#f5f7fb;text-align:center;font:inherit;font-weight:700}.workspace-tab.active{background:var(--brand-soft);color:#15343d}.live-workspace,.playback-workspace{display:grid;grid-template-columns:280px minmax(0,1fr);gap:22px}.camera-picker{align-self:start;min-height:420px;padding:14px;border-radius:12px;background:#172134;box-shadow:0 7px 20px rgba(7,12,20,.2)}.picker-head{padding:12px 15px;border-radius:999px;background:var(--brand-action);font-weight:750}.picker-search{width:100%;margin:18px 0 12px;padding:10px 13px;border:1px solid #b8c2cc;border-radius:999px;background:transparent;color:white}.picker-camera{display:flex;align-items:center;gap:10px;padding:12px 8px;color:#eef2f6}.picker-camera input{accent-color:var(--brand)}.work-area{min-width:0}.action-button{padding:10px 17px;border:0;border-radius:999px;background:var(--brand-action);color:white;font:inherit;font-weight:700}.ghost-button{padding:10px 17px;border:1px solid #c0cad3;border-radius:999px;background:transparent;color:white;font:inherit}.data-table{width:100%;border-collapse:collapse;background:rgba(25,34,51,.92)}.data-table th{padding:15px;text-align:left;background:#161827}.data-table td{padding:15px;border-top:1px solid #596473;color:#eef2f4}.empty-stage{min-height:420px;display:grid;place-items:center;text-align:center;color:#cbd5dc;font-size:22px;font-weight:700}.timeline-shell{margin-top:18px;padding:18px;border-radius:12px;background:#171a2a}.timeline-controls{display:flex;justify-content:center;gap:20px;font-size:24px;color:#9ea7b5}.timeline-track{height:54px;margin-top:14px;border-top:2px solid #b5bec7;background:repeating-linear-gradient(90deg,transparent 0 24px,rgba(255,255,255,.35) 25px 26px)}@media(max-width:900px){.live-workspace,.playback-workspace{grid-template-columns:1fr}.camera-picker{min-height:auto}.shell{grid-template-columns:86px minmax(0,1fr)}.sidebar{width:86px}.content{padding:20px}.nav a{min-height:68px;font-size:11px}}@media(max-width:760px){.shell{display:block}.sidebar{display:none}.content{padding:18px 14px 92px}.mobile-nav{grid-template-columns:repeat(4,1fr)}}
"""

STYLES += """
.account-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.account-grid .panel{min-width:0}@media(max-width:760px){.mobile-nav{grid-template-columns:repeat(5,minmax(0,1fr))}.mobile-nav a{padding:9px 2px;font-size:11px}.account-grid{grid-template-columns:1fr}}
.clip-form{display:grid;grid-template-columns:160px repeat(2,minmax(210px,1fr)) auto;gap:12px;align-items:end}.clip-form label{display:grid;gap:7px;color:var(--muted);font-size:12px}.clip-form select,.clip-form input{height:42px;padding:0 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:var(--text);font:inherit;color-scheme:dark}.clip-job{margin-top:16px}.clip-job[hidden]{display:none}@media(max-width:900px){.clip-form{grid-template-columns:1fr 1fr}.clip-form .action-button{height:42px}}@media(max-width:600px){.clip-form{grid-template-columns:1fr}}
.portal-tabs{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:4px;margin-bottom:24px;padding:7px;border-radius:9px;background:#182234;overflow-x:auto}.portal-tab{padding:13px;border:0;border-radius:7px;background:transparent;color:#f4f7fb;font:inherit;font-weight:700;white-space:nowrap;cursor:pointer}.portal-tab.active{background:var(--brand);color:#15343d}.portal-workspace{min-height:620px;padding:30px;border-radius:12px;background:rgba(25,34,51,.82)}.portal-panel[hidden]{display:none}.portal-actions{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:20px}.portal-search-row{display:grid;grid-template-columns:minmax(240px,1fr) auto;gap:20px;align-items:center}.portal-search{width:100%;height:46px;padding:0 16px;border:1px solid #d2dae1;border-radius:999px;background:transparent;color:white;font:inherit}.status-filters{display:flex;gap:17px;font-weight:700}.customer-list{display:grid;gap:10px;margin-top:24px}.customer-row{display:grid;grid-template-columns:1.5fr 1.5fr .8fr .8fr;gap:18px;align-items:center;padding:17px;border:1px solid rgba(255,255,255,.1);border-radius:9px;background:#1b2638}.customer-row small{color:var(--muted)}dialog.partner-dialog{width:min(520px,calc(100% - 28px));padding:0;border:1px solid var(--line);border-radius:14px;background:#192234;color:white;box-shadow:0 30px 80px rgba(0,0,0,.55)}dialog.partner-dialog::backdrop{background:rgba(0,0,0,.7)}.dialog-body{padding:24px}.dialog-form{display:grid;gap:14px}.dialog-form label{display:grid;gap:7px;color:var(--muted)}.dialog-form input,.dialog-form select{height:43px;padding:0 11px;border:1px solid var(--line);border-radius:8px;background:#111827;color:white;font:inherit}.dialog-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:8px}@media(max-width:800px){.portal-workspace{padding:18px}.portal-search-row,.customer-row{grid-template-columns:1fr}.portal-actions{align-items:flex-start;flex-direction:column}.portal-tabs{grid-template-columns:repeat(6,150px)}}
.rule-layout{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(300px,.7fr);gap:18px}.rule-stage{position:relative;aspect-ratio:16/9;overflow:hidden;border:1px solid var(--line);border-radius:10px;background:#080a0e}.rule-stage video{width:100%;height:100%;object-fit:contain}.rule-stage canvas{position:absolute;inset:0;width:100%;height:100%;cursor:crosshair}.rule-form{display:grid;gap:13px}.rule-form label{display:grid;gap:6px;color:var(--muted);font-size:12px}.rule-form input,.rule-form select{height:40px;padding:0 9px;border:1px solid var(--line);border-radius:7px;background:#111827;color:white}.chart{height:220px;display:flex;align-items:flex-end;gap:10px;padding:20px;border-left:1px solid #8190a0;border-bottom:1px solid #8190a0}.chart-bar{flex:1;min-width:24px;border-radius:5px 5px 0 0;background:linear-gradient(180deg,var(--brand),#4b4de2);position:relative}.chart-bar span{position:absolute;bottom:-22px;left:50%;transform:translateX(-50%);font-size:10px;color:var(--muted)}.mock-banner{padding:11px 14px;margin-bottom:16px;border:1px solid #d0a84b;border-radius:8px;background:#3a3020;color:#ffe1a0;font-size:13px}@media(max-width:950px){.rule-layout{grid-template-columns:1fr}}
"""

NAV_ITEMS = [
    ("live", "/", "▣", "Live"),
    ("events", "/events", "⌁", "Events"),
    ("alerts", "/alerts", "♢", "Smart alerts"),
    ("playback", "/playback", "◴", "Playback"),
    ("media", "/media", "▧", "Media"),
    ("dashboard", "/dashboard", "◉", "Dashboard"),
    ("settings", "/settings", "⚒", "Settings"),
    ("audit", "/audit-logs", "▤", "Audit logs"),
    ("analytics", "/analytics", "⌕", "Analytics"),
    ("sites", "/sites-management", "⌖", "Sites"),
    ("partner", "/partner", "▣", "Partner portal"),
    ("setup", "/setup", "+", "Setup wizard"),
    ("subscription", "/subscription", "≡", "Subscription"),
    ("users", "/users", "♙", "Users"),
    ("pricing", "/pricing", "$", "Pricing"),
    ("appliances", "/partner/appliance-dashboard", "▤", "Appliances"),
    ("branding", "/branding", "◇", "Branding"),
    ("help", "/help", "?", "Help"),
]


def page_shell(title: str, active: str, content: str, scripts: str = "") -> str:
    navigation = "".join(
        f'<a class="{"active" if key == active else ""}" href="{url}"><span class="nav-icon">{icon}</span><span>{label}</span></a>'
        for key, url, icon, label in NAV_ITEMS
    )
    mobile_items = [
        ("live", "/", "Cameras"),
        ("alerts", "/alerts", "Alerts"),
        ("dashboard", "/dashboard", "Dashboard"),
        ("sites", "/sites-management", "Sites"),
        ("users", "/users", "Account"),
    ]
    mobile = "".join(
        f'<a class="{"active" if key == active else ""}" href="{url}">{label}</a>'
        for key, url, label in mobile_items
    )
    if cloud_settings.staging:
        content='<div class="mock-banner" role="status"><strong>STAGING ENVIRONMENT</strong> · Test data and services only</div>'+content
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0a0d12"><link rel="icon" type="image/png" href="/static/brand-icon.png"><title>{escape(title)} · AnyAiCam</title><style>{STYLES}</style></head><body><div class="shell"><aside class="sidebar"><div class="brand"><img class="brand-logo" src="/static/brand-icon.png" alt="AnyAiCam"></div><nav class="nav" aria-label="Primary">{navigation}</nav><div class="sidebar-foot"><strong>Local VMS</strong>Accessible through Tailscale</div></aside><main class="content">{content}</main></div><nav class="mobile-nav" aria-label="Mobile">{mobile}</nav><div class="toast" id="toast" role="status"></div>{scripts}<script>const nativeFetch=window.fetch.bind(window);window.fetch=(input,options={{}})=>{{const method=(options.method||'GET').toUpperCase(),sameOrigin=typeof input==='string'?(!input.startsWith('http://')&&!input.startsWith('https://')):input.url.startsWith(location.origin);if(sameOrigin&&['POST','PUT','PATCH','DELETE'].includes(method)){{const csrf=document.cookie.split('; ').find(item=>item.startsWith('anyaicam_csrf='));if(csrf)options.headers={{...(options.headers||{{}}),'X-CSRF-Token':decodeURIComponent(csrf.split('=').slice(1).join('='))}}}}return nativeFetch(input,options)}};function showToast(message){{const toast=document.getElementById('toast');toast.textContent=message;toast.classList.add('show');clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>toast.classList.remove('show'),3200)}}function comingSoon(label){{showToast(/saved|error|failed|no live/i.test(label)?label:label+' is ready for a future update.')}}</script></body></html>"""


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
register_customer_platform_routes(app)
register_pwa_routes(app)
register_mobile_notification_routes(app)


@app.get("/camera/{camera_number}", response_class=HTMLResponse)
def camera_detail(camera_number: int) -> str:
    if camera_number < 1 or camera_number > CAMERA_COUNT:
        raise HTTPException(status_code=404, detail="Camera not found")
    content = f"""<header class="topbar"><div><p class="eyebrow">Camera detail</p><h1>Camera {camera_number}</h1></div><a class="ghost-button" href="/">Back to cameras</a></header><section class="panel"><div class="camera-view" style="border-radius:10px"><video id="detail-video" controls muted playsinline></video><div class="camera-placeholder" id="detail-placeholder"><span class="signal">◉</span><strong>Waiting for live hardware</strong><small>The controls remain available while this camera is offline.</small></div></div><div class="camera-tools" style="justify-content:center"><button class="camera-tool" onclick="comingSoon('Microphone requires compatible camera hardware')" title="Microphone">◖</button><button class="camera-tool" id="detail-mute" title="Mute">♩</button><button class="camera-tool" onclick="comingSoon('Snapshot uses the live camera frame')" title="Snapshot">◉</button><button class="camera-tool" onclick="document.getElementById('detail-video').requestFullscreen()" title="Full screen">⛶</button><a class="camera-tool" href="/playback" title="Playback">◴</a></div></section><div class="workspace-tabs" style="margin-top:18px"><button class="workspace-tab active">Live</button><a class="workspace-tab" href="/playback" style="text-decoration:none">Playback</a><a class="workspace-tab" href="/analytics" style="text-decoration:none">Analytics · Demo</a></div>"""
    scripts = f"""<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>const video=document.getElementById('detail-video'),placeholder=document.getElementById('detail-placeholder'),source='/hls/camera{camera_number}.m3u8';if(Hls.isSupported()){{const hls=new Hls();hls.loadSource(source);hls.attachMedia(video);hls.on(Hls.Events.MANIFEST_PARSED,()=>{{placeholder.hidden=true;video.play().catch(()=>{{}})}})}}else if(video.canPlayType('application/vnd.apple.mpegurl')){{video.src=source;video.addEventListener('loadedmetadata',()=>{{placeholder.hidden=true;video.play().catch(()=>{{}})}})}}document.getElementById('detail-mute').addEventListener('click',e=>{{video.muted=!video.muted;e.currentTarget.textContent=video.muted?'♩':'♫';showToast(video.muted?'Camera muted':'Camera audio enabled')}});</script>"""
    return page_shell(f"Camera {camera_number}", "live", content, scripts)


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
    events.sort(key=lambda event: event.get("timestamp", ""), reverse=True)
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


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    camera_cards = "".join(
        f"""<article class="camera-card"><div class="camera-view"><div class="camera-placeholder" id="placeholder{n}"><span class="signal">◌</span><strong>Waiting for stream</strong><span>The camera will reconnect automatically.</span></div><span class="live-badge">LIVE</span><video id="camera{n}" autoplay muted controls playsinline aria-label="Camera {n} live stream"></video><div class="analytics-overlay" id="overlay{n}">Analytics overlay</div></div><div class="camera-meta"><span class="camera-name">Camera {n}</span><span class="camera-state" id="state{n}">Connecting…</span></div><div class="camera-tools" aria-label="Camera {n} tools"><button class="camera-tool" title="Full screen" onclick="document.getElementById('camera{n}').requestFullscreen?.()">⛶</button><a class="camera-tool" title="Playback" href="/playback">▶</a><button class="camera-tool" title="Snapshot" onclick="captureSnapshot({n})">▣</button><button class="camera-tool" title="Download clip" onclick="location.href='/playback#create-clip'">↓</button><button class="camera-tool" title="Share clip" onclick="comingSoon('Share clip')">↗</button><button class="camera-tool" title="Audio" onclick="document.getElementById('camera{n}').muted=!document.getElementById('camera{n}').muted">◖</button><button class="camera-tool" title="Camera settings" onclick="comingSoon('Camera settings')">⚙</button><button class="camera-tool" title="Analytics overlay" onclick="document.getElementById('overlay{n}').classList.toggle('visible')">◇</button></div></article>"""
        for n in range(1, CAMERA_COUNT + 1)
    )
    content = f"""<header class="topbar"><div><p class="eyebrow">Security overview</p><h1>Live view</h1></div><div class="clock" id="clock"></div></header><section class="summary" aria-label="System summary"><div class="stat"><span class="stat-label">Recording</span><span class="stat-value"><span class="dot"></span>Enabled</span></div><div class="stat"><span class="stat-label">Retention</span><span class="stat-value">{RETENTION_DAYS} days</span></div><div class="stat"><span class="stat-label">Cameras</span><span class="stat-value">{CAMERA_COUNT} configured</span></div></section><div class="section-head"><div><h2>All cameras</h2><p>Streams reconnect automatically</p></div><div class="layout-controls" aria-label="Grid layout"><button class="layout-button" data-layout="1" title="1-camera grid">1</button><button class="layout-button active" data-layout="4" title="4-camera grid">4</button><button class="layout-button" data-layout="9" title="9-camera grid">9</button><button class="layout-button" data-layout="16" title="16-camera grid">16</button></div></div><section class="camera-grid" id="camera-grid" data-layout="4">{camera_cards}</section>"""
    scripts = """<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>
const clock=document.getElementById('clock');function tick(){clock.textContent=new Intl.DateTimeFormat(undefined,{weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date())}tick();setInterval(tick,30000);
function setState(n,text,ready=false){const state=document.getElementById(`state${n}`);state.textContent=text;state.classList.toggle('ready',ready);document.getElementById(`placeholder${n}`).style.display=ready?'none':'block'}
function captureSnapshot(n){const video=document.getElementById(`camera${n}`);if(video.readyState<2||!video.videoWidth){comingSoon(`Camera ${n} has no live frame to capture`);return}try{const canvas=document.createElement('canvas'),context=canvas.getContext('2d'),now=new Date();canvas.width=video.videoWidth;canvas.height=video.videoHeight;context.drawImage(video,0,0,canvas.width,canvas.height);const stamp=`Camera ${n} · ${now.toLocaleDateString()} ${now.toLocaleTimeString()}`;context.font=`600 ${Math.max(18,Math.round(canvas.width/45))}px system-ui`;const padding=16,textWidth=context.measureText(stamp).width,boxHeight=Math.max(48,Math.round(canvas.height/12));context.fillStyle='rgba(0,0,0,.68)';context.fillRect(0,canvas.height-boxHeight,Math.min(canvas.width,textWidth+padding*2),boxHeight);context.fillStyle='#fff';context.textBaseline='middle';context.fillText(stamp,padding,canvas.height-boxHeight/2);canvas.toBlob(blob=>{if(!blob){comingSoon('Snapshot failed');return}const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`camera${n}_${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}_${String(now.getHours()).padStart(2,'0')}-${String(now.getMinutes()).padStart(2,'0')}-${String(now.getSeconds()).padStart(2,'0')}.png`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);comingSoon(`Camera ${n} snapshot saved`)},'image/png')}catch(error){comingSoon(`Snapshot error: ${error.message}`)}}
async function captureSnapshot(n){const video=document.getElementById(`camera${n}`);if(video.readyState<2||!video.videoWidth){showToast(`Camera ${n} has no live frame to capture.`);return}showToast(`Saving Camera ${n} snapshot…`);try{const canvas=document.createElement('canvas'),context=canvas.getContext('2d'),now=new Date();canvas.width=video.videoWidth;canvas.height=video.videoHeight;context.drawImage(video,0,0,canvas.width,canvas.height);const stamp=`Home · Camera ${n} · ${now.toLocaleDateString()} ${now.toLocaleTimeString()}`,fontSize=Math.max(18,Math.round(canvas.width/45)),boxHeight=Math.max(48,Math.round(canvas.height/12));context.font=`600 ${fontSize}px system-ui`;context.fillStyle='rgba(0,0,0,.72)';context.fillRect(0,canvas.height-boxHeight,Math.min(canvas.width,context.measureText(stamp).width+32),boxHeight);context.fillStyle='#fff';context.textBaseline='middle';context.fillText(stamp,16,canvas.height-boxHeight/2);const imageData=canvas.toDataURL('image/png'),response=await fetch('/api/snapshots',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({camera:n,site:'home',image_data:imageData})}),result=await response.json();if(result.status!=='complete')throw new Error(result.message||'Snapshot could not be saved.');const link=document.createElement('a');link.href=result.url;link.download=result.filename;link.click();showToast('Snapshot saved and downloaded.')}catch(error){showToast(`Snapshot error: ${error.message}`)}}
function connectCamera(n){const video=document.getElementById(`camera${n}`),source=`/static/hls/camera${n}.m3u8`;video.addEventListener('playing',()=>setState(n,'Streaming',true));video.addEventListener('waiting',()=>setState(n,'Reconnecting…'));if(window.Hls&&Hls.isSupported()){const hls=new Hls({liveSyncDurationCount:2,liveMaxLatencyDurationCount:5});hls.loadSource(source);hls.attachMedia(video);hls.on(Hls.Events.ERROR,(_,data)=>{if(data.fatal)setState(n,'Waiting for camera')})}else if(video.canPlayType('application/vnd.apple.mpegurl')){video.src=source}else{setState(n,'Browser not supported')}}for(let n=1;n<=4;n++)connectCamera(n);
const grid=document.getElementById('camera-grid');const savedLayout=localStorage.getItem('camera-layout')||'4';function setLayout(layout){grid.dataset.layout=layout;document.querySelectorAll('.layout-button').forEach(button=>button.classList.toggle('active',button.dataset.layout===layout));localStorage.setItem('camera-layout',layout)}document.querySelectorAll('.layout-button').forEach(button=>button.addEventListener('click',()=>setLayout(button.dataset.layout)));setLayout(savedLayout);
</script>"""
    return page_shell("Live view", "live", content, scripts)


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
    health_rows = "".join(
        f'<div class="health-row"><div><div class="health-name">Camera {n}</div><div class="health-detail" id="health-detail-{n}">Checking connection…</div></div><span class="pill wait" id="health-pill-{n}">Checking</span></div>'
        for n in range(1, CAMERA_COUNT + 1)
    )
    activity = "".join(
        f'<div class="activity-row"><div><div class="health-name">Recording completed</div><div class="health-detail">{escape(clip.parent.name.replace("camera", "Camera "))}</div></div><span class="activity-time">{datetime.fromtimestamp(clip.stat().st_mtime).strftime("%I:%M %p")}</span></div>'
        for clip in clips[:5]
    ) or '<div class="empty">Recent activity will appear after a recording completes.</div>'
    content = f"""<header class="topbar"><div><p class="eyebrow">System overview</p><h1>Dashboard</h1></div></header><section class="summary"><div class="stat"><span class="stat-label">System health</span><span class="stat-value"><span class="dot"></span>VMS running</span></div><div class="stat"><span class="stat-label">Recording</span><span class="stat-value">Continuous</span></div><div class="stat"><span class="stat-label">Saved clips</span><span class="stat-value">{len(clips)}</span></div><div class="stat"><span class="stat-label">CPU</span><span class="stat-value" id="cpu-metric">Checking…</span></div><div class="stat"><span class="stat-label">Memory</span><span class="stat-value" id="memory-metric">Checking…</span></div><div class="stat"><span class="stat-label">Storage available</span><span class="stat-value" id="storage-metric">{storage_text}</span></div></section><section class="health-grid"><div class="panel"><div class="panel-head"><h2>Camera health</h2><a class="download" href="/">Open live view</a></div><div class="health-list">{health_rows}</div></div><div class="panel"><div class="panel-head"><h2>Storage</h2><span class="health-detail">Local</span></div><div class="stat-value">{storage_text}</div><div class="storage-bar"><span id="storage-bar-value" style="width:{used_percent}%"></span></div><div class="health-detail" id="storage-detail">{used_percent}% of disk in use · {RETENTION_DAYS}-day retention</div></div></section><section class="panel"><div class="panel-head"><h2>Recent activity</h2><a class="download" href="/playback">View recordings</a></div><div class="activity-list">{activity}</div></section>"""
    scripts = """<script>
async function updateHealth(){try{const response=await fetch('/api/cameras/status',{cache:'no-store'});const data=await response.json();data.cameras.forEach(camera=>{const pill=document.getElementById(`health-pill-${camera.camera}`),detail=document.getElementById(`health-detail-${camera.camera}`);pill.textContent=camera.online?'Online':'Reconnecting';pill.classList.toggle('wait',!camera.online);detail.textContent=camera.online?'Live stream active · recording monitored':`Stream ${camera.stream} · recording ${camera.recording}`})}catch(error){document.querySelectorAll('[id^=health-detail-]').forEach(item=>item.textContent='Health service unavailable')}}updateHealth();setInterval(updateHealth,10000);
async function updateMetrics(){try{const response=await fetch('/api/system/metrics',{cache:'no-store'}),data=await response.json();document.getElementById('cpu-metric').textContent=data.cpu_percent+'%';document.getElementById('memory-metric').textContent=data.memory_percent+'%';document.getElementById('storage-metric').textContent=data.storage_free_gb+' GB';document.getElementById('storage-bar-value').style.width=data.storage_percent+'%';document.getElementById('storage-detail').textContent=data.storage_percent+'% of disk in use · retention active'}catch(error){document.getElementById('cpu-metric').textContent='Unavailable';document.getElementById('memory-metric').textContent='Unavailable'}}updateMetrics();setInterval(updateMetrics,10000);
</script>"""
    issue_rows = "".join(f'<div class="health-row"><div><div class="health-name">{escape(issue["message"])}</div><div class="health-detail">{escape(issue["type"].replace("_", " ").title())}</div></div><span class="pill wait">{escape(issue["severity"].title())}</span></div>' for issue in health_issues.values()) or '<div class="empty">No active health issues.</div>'
    content += f'<section class="panel" style="margin-top:18px"><div class="panel-head"><h2>Health issues</h2><span class="health-detail">Automatic monitoring</span></div>{issue_rows}</section>'
    return page_shell("Dashboard", "dashboard", content, scripts)


@app.get("/media", response_class=HTMLResponse)
def media_library() -> str:
    items = media_library_items()
    cards = []
    for item in items:
        preview = (
            f'<img src="{item["url"]}" alt="{escape(item["name"])}" style="width:100%;aspect-ratio:16/9;object-fit:cover;background:#080a0e">'
            if item["type"] == "snapshot"
            else f'<video controls preload="metadata"><source src="{item["url"]}" type="video/mp4"></video>'
        )
        cards.append(f'<article class="clip media-item" data-site="{escape(item["site"])}" data-camera="{escape(str(item["camera"]))}" data-date="{item["date"]}" data-type="{item["type"]}">{preview}<div class="clip-body"><div class="clip-time">{escape(item["name"])}</div><div class="clip-meta">{item["type"].title()} · Site {escape(item["site"].title())} · Camera {escape(str(item["camera"]))} · {item["date"]}</div><a class="download" href="{item["url"]}" download>Download</a></div></article>')
    body = "".join(cards) or '<div class="empty" id="media-empty">No saved snapshots or exported clips yet.</div>'
    camera_options = "".join(f'<option value="{n}">Camera {n}</option>' for n in range(1, CAMERA_COUNT + 1))
    content = f"""<header class="topbar"><div><p class="eyebrow">Saved evidence</p><h1>Media library</h1></div><span class="pill">{len(items)} item(s)</span></header><section class="panel"><div class="library-toolbar"><select class="date-filter" id="media-site"><option value="">All sites</option><option value="home">Home</option></select><select class="date-filter" id="media-camera"><option value="">All cameras</option>{camera_options}</select><input class="date-filter" id="media-date" type="date" title="Filter by date"><select class="date-filter" id="media-type"><option value="">All media</option><option value="snapshot">Snapshots</option><option value="clip">Exported clips</option></select><button class="filter" id="clear-media">Clear filters</button></div></section><section class="recording-grid" id="media-grid">{body}</section><div class="empty" id="media-no-results" hidden>No media matches these filters.</div>"""
    scripts = """<script>const mediaFilters=['media-site','media-camera','media-date','media-type'].map(id=>document.getElementById(id)),mediaItems=[...document.querySelectorAll('.media-item')],noResults=document.getElementById('media-no-results');function filterMedia(){let visible=0;mediaItems.forEach(item=>{const match=(!mediaFilters[0].value||item.dataset.site===mediaFilters[0].value)&&(!mediaFilters[1].value||item.dataset.camera===mediaFilters[1].value)&&(!mediaFilters[2].value||item.dataset.date===mediaFilters[2].value)&&(!mediaFilters[3].value||item.dataset.type===mediaFilters[3].value);item.hidden=!match;if(match)visible++});noResults.hidden=visible>0||mediaItems.length===0}mediaFilters.forEach(input=>input.addEventListener('change',filterMedia));document.getElementById('clear-media').addEventListener('click',()=>{mediaFilters.forEach(input=>input.value='');filterMedia()});</script>"""
    return page_shell("Media library", "media", content, scripts)


ANALYTICS_FEATURES = [
    ("Smart alerts", "Prioritize events that need attention."),
    ("Smart motion", "Search motion events by camera and time."),
    ("Line crossing", "Detect movement across a defined boundary."),
    ("Intrusion", "Monitor activity inside protected zones."),
    ("License plate recognition", "Capture and search vehicle plates."),
    ("People counting", "Measure entries, exits, and foot traffic."),
    ("Occupancy", "Track how many people are within a space."),
    ("Vehicle search", "Find vehicles across recorded footage."),
]


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@app.get("/analytics", response_class=HTMLResponse)
def analytics() -> str:
    cards = "".join(
        f'<article class="feature-card"><div class="feature-icon">✦</div><h2>{escape(name)}</h2><p>{escape(description)}</p><a class="download" href="/analytics/{slugify(name)}">Configure module</a></article>'
        for name, description in ANALYTICS_FEATURES
    )
    content = f"""<header class="topbar"><div><p class="eyebrow">Intelligent video</p><h1>AI analytics</h1></div></header><div class="section-head"><p>Analytics modules are staged for future camera processing.</p></div><section class="feature-grid">{cards}</section>"""
    return page_shell("AI analytics", "analytics", content)


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
    rows = []
    for event in events:
        rows.append(f'<tr class="search-event" data-id="{event.get("id")}" data-type="{event.get("event_type")}" data-camera="{event.get("camera")}" data-site="{event.get("site","home")}" data-color="{event.get("vehicle_color") or ""}" data-plate="{event.get("plate_number") or ""}" data-date="{event.get("timestamp","")[:10]}"><td>{escape((event.get("timestamp") or "").replace("T"," ")[:19])}</td><td>{escape(event.get("event_type","").replace("_"," ").title())}</td><td>Camera {event.get("camera")}</td><td>{escape(event.get("plate_number") or event.get("vehicle_color") or "—")}</td><td>{float(event.get("confidence",0))*100:.0f}%</td><td><button class="download review-action" data-action="acknowledged">Acknowledge</button> · <button class="download review-action" data-action="bookmarked">Bookmark</button> · <button class="download review-action" data-action="tags">Tag</button> · <button class="download review-action" data-action="notes">Notes</button> · <a class="download" href="{event.get("linked_recording") or '#'}">Download clip</a> · <button class="download" onclick="comingSoon(\'Share clip\')">Share</button> · <button class="download review-action" data-action="false_positive">False positive</button></td></tr>')
    camera_options = "".join(f'<option value="{n}">Camera {n}</option>' for n in range(1,CAMERA_COUNT+1))
    content = f"""<header class="topbar"><div><p class="eyebrow">Experimental discovery</p><h1>Smart search</h1></div></header><div class="mock-banner">Mock detection data is shown. Structured filters and review actions are functional.</div><section class="panel"><label>Natural-language search (experimental)<div class="portal-search-row"><input class="portal-search" id="natural-search" placeholder="Example: silver vehicles on camera 2"><button class="action-button" id="translate-search">Translate</button></div></label><div class="health-detail" id="translated-filters"></div><div class="library-toolbar" style="margin-top:18px"><select class="date-filter" id="search-type"><option value="">All event types</option><option value="person">Person</option><option value="vehicle">Vehicle</option><option value="plate">Plate</option><option value="line_crossing">Line crossing</option><option value="intrusion">Intrusion</option></select><select class="date-filter" id="search-camera"><option value="">All cameras</option>{camera_options}</select><input class="date-filter" id="search-color" placeholder="Color"><input class="date-filter" id="search-plate" placeholder="Plate"><input class="date-filter" id="search-date" type="date"></div><table class="data-table"><thead><tr><th>Time</th><th>Type</th><th>Camera</th><th>Detail</th><th>Confidence</th><th>Review tools</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>"""
    scripts = """<script>const searchRows=[...document.querySelectorAll('.search-event')],type=document.getElementById('search-type'),camera=document.getElementById('search-camera'),color=document.getElementById('search-color'),plate=document.getElementById('search-plate'),date=document.getElementById('search-date');function filterEvents(){searchRows.forEach(row=>row.hidden=(type.value&&row.dataset.type!==type.value)||(camera.value&&row.dataset.camera!==camera.value)||(color.value&&!row.dataset.color.toLowerCase().includes(color.value.toLowerCase()))||(plate.value&&!row.dataset.plate.toLowerCase().includes(plate.value.toLowerCase()))||(date.value&&row.dataset.date!==date.value))}[type,camera,color,plate,date].forEach(input=>input.addEventListener('input',filterEvents));document.getElementById('translate-search').addEventListener('click',async()=>{const response=await fetch('/api/analytics/natural-search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:document.getElementById('natural-search').value})}),result=await response.json(),filters=result.filters;document.getElementById('translated-filters').textContent='Experimental filters: '+JSON.stringify(filters);if(filters.event_type)type.value=filters.event_type;if(filters.camera)camera.value=filters.camera;if(filters.color)color.value=filters.color;if(filters.plate)plate.value=filters.plate;filterEvents()});document.querySelectorAll('.review-action').forEach(button=>button.addEventListener('click',async()=>{const row=button.closest('tr'),action=button.dataset.action,payload={event_id:row.dataset.id,acknowledged:false,bookmarked:false,false_positive:false,tags:[],notes:''};if(action==='tags')payload.tags=[prompt('Add tag')||''].filter(Boolean);else if(action==='notes')payload.notes=prompt('Add review notes')||'';else payload[action]=true;const response=await fetch(`/api/analytics/events/${row.dataset.id}/review`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=await response.json();showToast(result.message)}));</script>"""
    return page_shell("Smart search", "analytics", content, scripts)


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


@app.get("/audit-logs", response_class=HTMLResponse)
def audit_logs() -> str:
    rows = "".join(f'<tr><td>{datetime.now().strftime("%I:%M %p")}</td><td>Local user</td><td>Web browser</td><td>Camera {n} health checked</td></tr>' for n in range(1, CAMERA_COUNT + 1))
    content = f"""<header class="topbar"><div><p class="eyebrow">Accountability</p><h1>Audit logs</h1></div><div><button class="filter active">Last 30 days</button> <button class="action-button">Today</button></div></header><section class="panel"><div class="panel-head"><h2>Recent system actions</h2><span class="health-detail">Local VMS</span></div><table class="data-table"><thead><tr><th>Action time</th><th>User</th><th>Device</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></section>"""
    return page_shell("Audit logs", "audit", content)


@app.get("/sites", response_class=HTMLResponse)
def sites() -> str:
    content = f"""<header class="topbar"><div><p class="eyebrow">Locations</p><h1>Sites</h1></div></header><section class="feature-grid"><article class="feature-card"><div class="feature-icon">⌂</div><h2>Home</h2><p>{CAMERA_COUNT} configured cameras · Local VMS</p><a class="download" href="/">Open cameras</a></article><article class="feature-card"><div class="feature-icon">＋</div><h2>Add another site</h2><p>Multi-site management is prepared for a future update.</p><span class="coming">Coming soon</span></article></section>"""
    return page_shell("Sites", "sites", content)


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
def settings() -> str:
    links = "".join(
        f'<a class="setting-link" href="/settings/{slugify(name)}"><div><strong>{escape(name)}</strong><div class="health-detail">{escape(description)}</div></div><span>›</span></a>'
        for name, description in SETTINGS_CATEGORIES
    )
    content = f"""<header class="topbar"><div><p class="eyebrow">Configuration</p><h1>Settings</h1></div></header><section class="settings-list">{links}</section>"""
    return page_shell("Settings", "settings", content)


@app.get("/settings/{settings_slug}", response_class=HTMLResponse)
def settings_detail(settings_slug: str) -> str:
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


@app.get("/help", response_class=HTMLResponse)
def help_page() -> str:
    content = """<header class="topbar"><div><p class="eyebrow">Support</p><h1>Help</h1></div></header><section class="feature-grid"><article class="feature-card"><div class="feature-icon">?</div><h2>Getting started</h2><p>Use Live view to monitor cameras and Playback to review completed five-minute recordings.</p></article><article class="feature-card"><div class="feature-icon">⌁</div><h2>Remote access</h2><p>Use the same page through your private Tailscale address while the home computer and VMS are running.</p></article><article class="feature-card"><div class="feature-icon">!</div><h2>Camera offline</h2><p>The interface remains available when cameras are unreachable and reconnects after the VMS restarts.</p></article></section>"""
    return page_shell("Help", "help", content)


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
    sections = []
    total_clips = 0
    for camera_number in range(1, CAMERA_COUNT + 1):
        camera_folder = RECORDINGS_FOLDER / f"camera{camera_number}"
        clips = sorted(
            camera_folder.glob("*.mkv"),
            key=lambda clip: clip.stat().st_mtime,
            reverse=True,
        )
        total_clips += len(clips)
        items = []
        for clip in clips:
            safe_name = escape(clip.name, quote=True)
            url = f"/recordings/camera{camera_number}/{quote(clip.name)}"
            label, metadata = recording_details(clip, camera_number)
            clip_date = datetime.fromtimestamp(clip.stat().st_mtime).strftime("%Y-%m-%d")
            items.append(f"""<article class="clip" data-recorded="{clip_date}"><video controls preload="metadata"><source src="{url}"></video><div class="clip-body"><div class="clip-time">{escape(label)}</div><div class="clip-meta">{escape(metadata)}</div><a class="download" href="{url}" download="{safe_name}">Download recording</a></div></article>""")
        body = "".join(items) or "<div class=\"empty\">No completed recordings yet.</div>"
        sections.append(f"""<section class="camera-section" data-camera="{camera_number}"><div class="section-head"><h2>Camera {camera_number}</h2><p>{len(clips)} recording{'s' if len(clips) != 1 else ''}</p></div><div class="recording-grid">{body}</div></section>""")
    filters = "".join(f'<button class="filter" data-filter="{n}">Camera {n}</button>' for n in range(1, CAMERA_COUNT + 1))
    camera_options = "".join(f'<option value="{n}">Camera {n}</option>' for n in range(1, CAMERA_COUNT + 1))
    manual_clips = []
    for clip in sorted(CLIPS_FOLDER.glob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True):
        clip_url = f"/recordings/clips/{quote(clip.name)}"
        manual_clips.append(f'<article class="clip"><video controls preload="metadata"><source src="{clip_url}" type="video/mp4"></video><div class="clip-body"><div class="clip-time">{escape(clip.stem.replace("_", " "))}</div><div class="clip-meta">Manual clip · {clip.stat().st_size / (1024 * 1024):.1f} MB</div><a class="download" href="{clip_url}" download>Download clip</a></div></article>')
    manual_clip_section = "".join(manual_clips) or '<div class="empty" id="no-manual-clips">No manual clips created yet.</div>'
    playback_events = sorted(load_motion_events(), key=lambda event: event.get("start_time") or event.get("timestamp", ""), reverse=True)[:12]
    motion_cards = []
    timeline_markers = []
    for event in playback_events:
        event_time = event.get("start_time") or event.get("timestamp", "")
        thumbnail = f'<img src="{event["thumbnail"]}" alt="Motion thumbnail" style="width:100%;aspect-ratio:16/9;object-fit:cover">' if event.get("thumbnail") else '<div class="empty" style="aspect-ratio:16/9;display:grid;place-items:center">Motion</div>'
        recording_link = event.get("linked_recording") or "#create-clip"
        event_type = event.get("event_type", "motion")
        motion_cards.append(f'<article class="clip" data-event-type="{escape(event_type)}">{thumbnail}<div class="clip-body"><div class="clip-time">Camera {event.get("camera", "—")} {escape(event_type)}</div><div class="clip-meta">{escape(event_time.replace("T", " ")[:19])} · {float(event.get("confidence", event.get("score", 0))):.1f}% confidence</div><a class="download" href="{recording_link}">Jump to recording</a></div></article>')
        marker_position = (len(timeline_markers) * 83 + 7) % 96
        timeline_markers.append(f'<a href="{recording_link}" title="Camera {event.get("camera", "")} {escape(event_type)}" data-event-type="{escape(event_type)}" style="position:absolute;left:{marker_position}%;top:8px;width:10px;height:38px;border-radius:5px;background:var(--brand-action)"></a>')
    motion_section = "".join(motion_cards) or '<div class="empty">No motion events recorded yet.</div>'
    event_filters = "".join(f'<button class="filter event-filter" data-event-filter="{event_type}">{label}</button>' for event_type, label in [("motion", "Motion"), ("person", "Person"), ("vehicle", "Vehicle"), ("line_crossing", "Line crossing"), ("intrusion", "Intrusion"), ("bookmark", "Manual bookmarks")])
    timeline = f'<section class="timeline-shell"><div class="panel-head"><h2>Event timeline</h2><div class="library-toolbar"><button class="filter event-filter active" data-event-filter="all">All events</button>{event_filters}</div></div><div id="event-timeline" style="position:relative;height:54px;border-top:2px solid #b5bec7;background:repeating-linear-gradient(90deg,transparent 0 24px,rgba(255,255,255,.25) 25px 26px)">{"".join(timeline_markers)}</div></section>'
    content = f"""<header class="topbar"><div><p class="eyebrow">Recording library</p><h1>Playback</h1></div></header><section class="summary" aria-label="Recording summary"><div class="stat"><span class="stat-label">Saved recordings</span><span class="stat-value">{total_clips}</span></div><div class="stat"><span class="stat-label">Manual clips</span><span class="stat-value">{len(manual_clips)}</span></div><div class="stat"><span class="stat-label">Motion events</span><span class="stat-value">{len(playback_events)}</span></div></section><section class="panel" id="create-clip"><div class="panel-head"><div><h2>Create manual clip</h2><div class="health-detail">Choose a camera and a time range covered by completed recordings.</div></div></div><form id="clip-form" class="clip-form"><label>Camera<select id="clip-camera" required>{camera_options}</select></label><label>Start time<input id="clip-start" type="datetime-local" required></label><label>End time<input id="clip-end" type="datetime-local" required></label><button class="action-button" type="submit">Create clip</button></form><div id="clip-job" class="clip-job" hidden><div class="health-detail" id="clip-message">Preparing…</div><div class="storage-bar"><span id="clip-progress" style="width:0%"></span></div></div></section><section class="camera-section"><div class="section-head"><h2>Motion events</h2><a class="download" href="/events">View all events</a></div><div class="recording-grid">{motion_section}</div></section><section class="camera-section"><div class="section-head"><h2>Manual clips</h2><a class="download" href="/media">Open media library</a></div><div class="recording-grid" id="manual-clips">{manual_clip_section}</div></section><div class="library-toolbar" aria-label="Filter recordings"><button class="filter active" data-filter="all">All cameras</button>{filters}<label class="sr-only" for="recording-date">Recording date</label><input class="date-filter" id="recording-date" type="date" title="Filter by date"><button class="filter" id="clear-date" type="button">Clear date</button></div><div id="recording-sections">{''.join(sections)}</div>"""
    scripts = """<script>let cameraFilter='all';const dateInput=document.getElementById('recording-date');function applyFilters(){document.querySelectorAll('#recording-sections .camera-section').forEach(section=>{const cameraMatch=cameraFilter==='all'||section.dataset.camera===cameraFilter;let visibleClips=0;section.querySelectorAll('.clip').forEach(clip=>{const dateMatch=!dateInput.value||clip.dataset.recorded===dateInput.value;clip.hidden=!dateMatch;if(dateMatch)visibleClips++});section.hidden=!cameraMatch||visibleClips===0})}document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('[data-filter]').forEach(item=>item.classList.remove('active'));button.classList.add('active');cameraFilter=button.dataset.filter;applyFilters()}));dateInput.addEventListener('change',applyFilters);document.getElementById('clear-date').addEventListener('click',()=>{dateInput.value='';applyFilters()});const clipForm=document.getElementById('clip-form'),jobBox=document.getElementById('clip-job'),jobMessage=document.getElementById('clip-message'),jobProgress=document.getElementById('clip-progress');async function pollClip(id){const response=await fetch(`/api/clips/${id}`,{cache:'no-store'}),job=await response.json();jobMessage.textContent=job.message;jobProgress.style.width=`${job.progress||0}%`;if(job.status==='complete'){showToast('Clip created successfully.');setTimeout(()=>location.reload(),900);return}if(job.status==='error'){showToast(job.message);return}setTimeout(()=>pollClip(id),1000)}clipForm.addEventListener('submit',async event=>{event.preventDefault();jobBox.hidden=false;jobMessage.textContent='Submitting clip request…';jobProgress.style.width='2%';const payload={camera:Number(document.getElementById('clip-camera').value),start_time:document.getElementById('clip-start').value,end_time:document.getElementById('clip-end').value};try{const response=await fetch('/api/clips',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),job=await response.json();if(job.status==='error'){jobMessage.textContent=job.message;showToast(job.message);return}pollClip(job.id)}catch(error){jobMessage.textContent='Could not create clip: '+error.message;showToast(jobMessage.textContent)}});</script>"""
    content = content.replace('</section><section class="camera-section">', f'</section>{timeline}<section class="camera-section">', 1)
    scripts = scripts.replace('</script>', "document.querySelectorAll('.event-filter').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('.event-filter').forEach(item=>item.classList.remove('active'));button.classList.add('active');const type=button.dataset.eventFilter;document.querySelectorAll('[data-event-type]').forEach(item=>item.hidden=type!=='all'&&item.dataset.eventType!==type)}));</script>")
    return page_shell("Playback", "playback", content, scripts)
