"""Per-camera video processing profile from MEASURED stream geometry
(2026-10-11, video-pipeline).

cameras.resolution is a sales label ('2mp' by default, hard-coded by
partner onboarding, never written by provisioning) and nothing in the video
pipeline may trust it. The real main/sub stream width, height, codec and
fps are measured on the edge (camera_video_probe.py: the ONVIF media
profiles already discovered by camera_capabilities.py, then ffprobe on the
stream itself) and stored in the camera's capability record under each
stream's "measured" key. This module turns that record into one profile
that every encoder/reader asks instead of guessing:

  clip      event clip re-encode: fit inside a box (1920x1080 for a normal
            landscape camera, swapped for a corridor/portrait camera,
            1440x1440 for fisheye/square, 2560x1080 for panoramic),
            never upscaled; H.264 yuv420p; bitrate cap from output pixels
  live      the live HLS libx264 encode: the same box and a bitrate cap
            no higher than ANYAICAM_LIVE_MAX_BITRATE_KBPS
  analytics the stream the AI reader decodes (select_streams(record,
            "analytics"): the smallest stream at least 640 px wide, else
            the main stream) and the frame size it is scaled to (fits
            1280x720, never upscaled)
  thumbnail the largest width an AI/analytics thumbnail is saved at
            (ANYAICAM_THUMBNAIL_MAX_WIDTH, 1280)

A camera that was never measured, or whose probe failed, gets SAFE
defaults: the scale filters below are written as ffmpeg expressions that
only ever DOWNscale (min(iw, box)) and keep the source aspect ratio, so an
unknown or changed resolution can never distort or upscale a picture --
the measured values only choose the box (orientation, fisheye, panoramic)
and the bitrate caps.

Pure functions; no I/O. The resolution class is reporting only ("2mp",
"4mp", "5mp", "8mp", "other", "unknown"); nothing below branches on it."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

SCHEMA = 1


def _int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)) or default))
    except ValueError:
        return default


# Output boxes: (long side, short side).
CLIP_BOX = (_int_env("ANYAICAM_CLIP_MAX_LONG_SIDE", 1920, 320), _int_env("ANYAICAM_CLIP_MAX_SHORT_SIDE", 1080, 180))
LIVE_BOX = (_int_env("ANYAICAM_LIVE_MAX_LONG_SIDE", 1920, 320), _int_env("ANYAICAM_LIVE_MAX_SHORT_SIDE", 1080, 180))
SQUARE_BOX_SIDE = _int_env("ANYAICAM_SQUARE_MAX_SIDE", 1440, 320)
PANORAMIC_LONG_SIDE = _int_env("ANYAICAM_PANORAMIC_MAX_LONG_SIDE", 2560, 320)
ANALYTICS_BOX = (_int_env("ANYAICAM_ANALYTICS_MAX_LONG_SIDE", 1280, 320), _int_env("ANYAICAM_ANALYTICS_MAX_SHORT_SIDE", 720, 180))
# 0 = save AI thumbnails at the analytics frame's own size (no downscale).
THUMBNAIL_MAX_WIDTH = _int_env("ANYAICAM_THUMBNAIL_MAX_WIDTH", 1280, 0)
if 0 < THUMBNAIL_MAX_WIDTH < 160:
    THUMBNAIL_MAX_WIDTH = 160
CLIP_MIN_KBPS, CLIP_MAX_KBPS = 800, 8000
# ~2 kbit per 1000 output pixels: 1920x1080 -> 4147 kbps, 1280x720 -> 1843.
KBPS_PER_KILOPIXEL = 2.0


def _positive(value) -> Optional[int]:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if 16 <= number <= 16384 else None


def resolution_class(width, height) -> str:
    """Reporting label from measured pixels (never from the sales label).
    2mp <= 2.6 MP (1080p, 1280x960 ...); 4mp <= 4.5 (2560x1440, 2688x1520);
    5mp <= 6.5 (2592x1944, 3072x1728); 8mp <= 9.5 (3840x2160); bigger
    (4000x3000 12 MP, 4K fisheye ...) is "other"."""
    w, h = _positive(width), _positive(height)
    if not w or not h:
        return "unknown"
    megapixels = w * h / 1_000_000.0
    if megapixels <= 2.6:
        return "2mp"
    if megapixels <= 4.5:
        return "4mp"
    if megapixels <= 6.5:
        return "5mp"
    if megapixels <= 9.5:
        return "8mp"
    return "other"


def shape(width, height) -> str:
    """"landscape" (16:9, 4:3, 5:4 ...), "portrait" (corridor mode),
    "square" (fisheye, 1:1 within 12 %), "panoramic" (2.2:1 or wider) or
    "unknown"."""
    w, h = _positive(width), _positive(height)
    if not w or not h:
        return "unknown"
    ratio = w / float(h)
    if abs(ratio - 1.0) <= 0.12:
        return "square"
    if ratio >= 2.2:
        return "panoramic"
    if ratio < 1.0:
        return "portrait"
    return "landscape"


def box_for(stream_shape: str, base: tuple[int, int]) -> tuple[int, int]:
    """(max width, max height) for a source of this shape."""
    long_side, short_side = base
    if stream_shape == "square":
        side = max(short_side, min(long_side, SQUARE_BOX_SIDE))
        return side, side
    if stream_shape == "panoramic":
        return max(long_side, PANORAMIC_LONG_SIDE), short_side
    if stream_shape == "portrait":
        return short_side, long_side
    return long_side, short_side


def _even(value: float) -> int:
    return max(2, int(round(value)) // 2 * 2)


def fit(width, height, box: tuple[int, int]) -> Optional[tuple[int, int]]:
    """Size the scale filter will produce for a known source: aspect kept,
    never upscaled, even dimensions. None for an unknown source."""
    w, h = _positive(width), _positive(height)
    if not w or not h:
        return None
    factor = min(1.0, box[0] / float(w), box[1] / float(h))
    if factor >= 1.0:
        return w // 2 * 2 or w, h // 2 * 2 or h
    return _even(w * factor), _even(h * factor)


def scale_filter(box: tuple[int, int]) -> str:
    """Downscale-only, aspect-preserving scale into box; a source already
    inside it keeps its size. Safe for an unknown or changed resolution."""
    return (f"scale='min({box[0]},iw)':'min({box[1]},ih)'"
            ":force_original_aspect_ratio=decrease:force_divisible_by=2")


def bitrate_kbps(size: Optional[tuple[int, int]], box: tuple[int, int]) -> int:
    width, height = size or box
    return int(min(CLIP_MAX_KBPS, max(CLIP_MIN_KBPS, round(width * height / 1000.0 * KBPS_PER_KILOPIXEL))))


def _fps_value(value) -> Optional[float]:
    try:
        fps = round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None
    return fps if fps and 0 < fps <= 240 else None


def stream_geometry(stream: Optional[dict]) -> dict:
    """{width, height, codec, fps, source, measured_by} for one stream.

    source says where the numbers come from -- never mixed:
      "measured"    ffprobe measured the stream (stream["measured"]);
      "configured"  only the camera's ONVIF encoder configuration is known
                    (what the camera says it is set to, NOT a measurement);
      None          nothing is known.
    Anything that shows or reports the geometry must say "configured (not
    yet measured)" for "configured" (Codex review, 2026-10-11)."""
    empty = {"width": None, "height": None, "codec": None, "fps": None, "source": None, "measured_by": None}
    if not isinstance(stream, dict):
        return empty
    measured = stream.get("measured") if isinstance(stream.get("measured"), dict) else {}
    width, height = _positive(measured.get("width")), _positive(measured.get("height"))
    if width and height:
        codec = measured.get("codec")
        return {"width": width, "height": height, "codec": str(codec).upper()[:16] if codec else None,
                "fps": _fps_value(measured.get("fps")), "source": "measured", "measured_by": measured.get("by") or "ffprobe"}
    width, height = _positive(stream.get("width")), _positive(stream.get("height"))
    if width and height:
        codec = stream.get("codec")
        return {"width": width, "height": height, "codec": str(codec).upper()[:16] if codec else None,
                "fps": _fps_value(stream.get("fps")), "source": "configured", "measured_by": None}
    return empty


def main_stream(record: Optional[dict]) -> Optional[dict]:
    streams = [s for s in (record or {}).get("streams") or [] if isinstance(s, dict)]
    mains = [s for s in streams if s.get("role") == "main"]
    return (mains or streams or [None])[0]


# Analytics stream policy (owner decision 2026-10-11): the smallest stream at
# least ANALYTICS_MIN_WIDTH (640) wide; else, ONLY when
# ANYAICAM_AI_ALLOW_480_SUBSTREAM is on, the best (widest) stream at least
# ANALYTICS_FALLBACK_MIN_WIDTH (480) wide; else the main stream (decoded and
# scaled to the analytics box). The default follows the accuracy validation
# in docs/video-pipeline-20261011.md section 4.6.
ANALYTICS_MIN_WIDTH = 640
ANALYTICS_FALLBACK_MIN_WIDTH = 480
ALLOW_480_SUBSTREAM = os.environ.get("ANYAICAM_AI_ALLOW_480_SUBSTREAM", "false").strip().lower() == "true"

ANALYTICS_CHOICES = ("substream_640", "substream_480_fallback", "main_480_fallback_disabled", "main_no_usable_substream")


def analytics_choice(record: Optional[dict], allow_480: Optional[bool] = None) -> tuple:
    """(stream, reason): which stream AI frames come from and why. A stream
    marked verified=False is skipped."""
    allow_480 = ALLOW_480_SUBSTREAM if allow_480 is None else allow_480
    streams = [s for s in (record or {}).get("streams") or [] if isinstance(s, dict) and s.get("uri")]
    main = main_stream(record)
    subs = [s for s in streams if s is not main and s.get("role") != "main" and s.get("verified") is not False]

    def width(stream):
        return stream_geometry(stream)["width"] or 0

    preferred = sorted((s for s in subs if width(s) >= ANALYTICS_MIN_WIDTH), key=width)
    if preferred:
        return preferred[0], "substream_640"
    fallback = sorted((s for s in subs if ANALYTICS_FALLBACK_MIN_WIDTH <= width(s) < ANALYTICS_MIN_WIDTH),
                      key=width, reverse=True)
    if fallback:
        return (fallback[0], "substream_480_fallback") if allow_480 else (main, "main_480_fallback_disabled")
    return main, "main_no_usable_substream"


def analytics_stream(record: Optional[dict], allow_480: Optional[bool] = None) -> Optional[dict]:
    return analytics_choice(record, allow_480)[0]


@dataclass
class Profile:
    measured: bool          # True only when ffprobe MEASURED the main stream
    source: dict
    resolution_class: str
    shape: str
    clip_box: tuple[int, int]
    clip_size: Optional[tuple[int, int]]
    clip_max_kbps: int
    live_box: tuple[int, int]
    live_size: Optional[tuple[int, int]]
    live_max_kbps: int
    analytics_uri: Optional[str] = None      # credential-free
    analytics_is_main: bool = True
    analytics_source: dict = field(default_factory=dict)
    analytics_box: tuple[int, int] = ANALYTICS_BOX
    analytics_size: Optional[tuple[int, int]] = None
    thumbnail_max_width: int = THUMBNAIL_MAX_WIDTH
    geometry_source: str = "unknown"       # "measured" | "configured" | "unknown"
    analytics_reason: str = "main_no_usable_substream"

    def clip_filter(self) -> str:
        return scale_filter(self.clip_box) + ",format=yuv420p"

    def live_filter(self) -> str:
        return scale_filter(self.live_box) + ",format=yuv420p"

    def summary(self) -> dict:
        """Credential-free, JSON-safe (reported to the cloud)."""
        return {
            "schema": SCHEMA,
            "measured": self.measured,
            "geometry_source": self.geometry_source,
            "analytics_choice": self.analytics_reason,
            "resolution_class": self.resolution_class,
            "shape": self.shape,
            "main": {k: self.source.get(k) for k in ("width", "height", "codec", "fps", "source", "measured_by")},
            "analytics": None if self.analytics_is_main else {
                k: self.analytics_source.get(k) for k in ("width", "height", "codec", "fps", "source", "measured_by")},
            "clip": list(self.clip_size) if self.clip_size else None,
            "live": list(self.live_size) if self.live_size else None,
            "analytics_frame": list(self.analytics_size) if self.analytics_size else None,
            "thumbnail_max_width": self.thumbnail_max_width,
        }


def build(record: Optional[dict], live_max_kbps: int = 0) -> Profile:
    """The profile for a capability record (None/garbage -> safe defaults)."""
    main = main_stream(record) if isinstance(record, dict) else None
    source = stream_geometry(main)
    width, height = source["width"], source["height"]
    stream_shape = shape(width, height)
    measured = source["source"] == "measured"
    clip_box = box_for(stream_shape, CLIP_BOX)
    live_box = box_for(stream_shape, LIVE_BOX)
    clip_size = fit(width, height, clip_box)
    live_size = fit(width, height, live_box)
    live_kbps = bitrate_kbps(live_size, live_box)
    if live_max_kbps and live_max_kbps > 0:
        live_kbps = min(live_kbps, int(live_max_kbps))
    analytics, analytics_reason = analytics_choice(record) if isinstance(record, dict) else (None, "main_no_usable_substream")
    analytics_geometry = stream_geometry(analytics)
    analytics_is_main = analytics is None or analytics is main
    analytics_box = box_for(shape(analytics_geometry["width"], analytics_geometry["height"]), ANALYTICS_BOX)
    return Profile(
        measured=measured,
        source=source,
        resolution_class=resolution_class(width, height),
        shape=stream_shape,
        clip_box=clip_box,
        clip_size=clip_size,
        clip_max_kbps=bitrate_kbps(clip_size, clip_box),
        live_box=live_box,
        live_size=live_size,
        live_max_kbps=live_kbps,
        analytics_uri=(analytics or {}).get("uri"),
        analytics_is_main=analytics_is_main,
        analytics_source=analytics_geometry,
        analytics_box=analytics_box,
        analytics_size=fit(analytics_geometry["width"], analytics_geometry["height"], analytics_box),
        geometry_source=source["source"] or "unknown",
        analytics_reason=analytics_reason,
    )


def thumbnail_scale(frame_width: int, max_width: int = THUMBNAIL_MAX_WIDTH) -> float:
    """Factor (<= 1) to apply to a frame before saving it as a thumbnail."""
    try:
        width = int(frame_width)
    except (TypeError, ValueError):
        return 1.0
    if width <= 0 or max_width <= 0 or width <= max_width:
        return 1.0
    return max_width / float(width)


# ------------------------------------------------------------ cloud storage

MEASURED_COLUMNS = (
    "measured_width", "measured_height", "measured_codec", "measured_fps",
    "measured_sub_width", "measured_sub_height", "measured_sub_codec",
    "measured_resolution_class", "measured_shape", "measured_source", "measured_analytics_choice",
)


def _report_stream(value) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    width, height = _positive(value.get("width")), _positive(value.get("height"))
    if not width or not height:
        return None
    codec = value.get("codec")
    codec = str(codec).upper()[:16] if isinstance(codec, str) and codec.replace("_", "").isalnum() else None
    try:
        fps = round(float(value.get("fps")), 2) if value.get("fps") is not None else None
    except (TypeError, ValueError):
        fps = None
    return {"width": width, "height": height, "codec": codec, "fps": fps if fps and 0 < fps <= 240 else None}


def cloud_measured_columns(report) -> Optional[dict]:
    """cameras.measured_* values for an appliance's "video" report, or None
    (no report, a failed probe, or anything malformed). The class and shape
    are recomputed here from the pixels -- never taken from the report, and
    never from the sales label. measured_source is "measured" (status "ok":
    ffprobe measured it) or "configured" (status "configured": only the
    camera's ONVIF configuration is known); the route never lets a
    configured report replace a measured one."""
    if not isinstance(report, dict) or report.get("status") not in ("ok", "configured"):
        return None
    main = _report_stream(report.get("main"))
    if not main:
        return None
    sub = _report_stream(report.get("sub")) or {}
    return {
        "measured_width": main["width"], "measured_height": main["height"],
        "measured_codec": main["codec"], "measured_fps": main["fps"],
        "measured_sub_width": sub.get("width"), "measured_sub_height": sub.get("height"),
        "measured_sub_codec": sub.get("codec"),
        "measured_resolution_class": resolution_class(main["width"], main["height"]),
        "measured_shape": shape(main["width"], main["height"]),
        "measured_source": "measured" if report.get("status") == "ok" else "configured",
        "measured_analytics_choice": report.get("analytics_choice") if report.get("analytics_choice") in ANALYTICS_CHOICES else None,
    }


def thumbnail_image(frame, max_width: int = None):
    """(image to draw on and save, factor applied to frame coordinates).
    Always a new, writable array; never upscaled."""
    max_width = THUMBNAIL_MAX_WIDTH if max_width is None else max_width
    factor = thumbnail_scale(frame.shape[1], max_width)
    if factor >= 1.0:
        return frame.copy(), 1.0
    import cv2
    size = (max(2, int(round(frame.shape[1] * factor))), max(2, int(round(frame.shape[0] * factor))))
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA), factor


def scale_box(x, y, w, h, factor: float) -> tuple[int, int, int, int]:
    if factor >= 1.0:
        return int(x), int(y), int(w), int(h)
    return int(round(x * factor)), int(round(y * factor)), max(1, int(round(w * factor))), max(1, int(round(h * factor)))


NOT_MEASURED_TEXT = "Not yet measured"
CONFIGURED_SUFFIX = " - configured (not yet measured)"
# The cameras columns every camera view selects to show the video line.
DISPLAY_COLUMNS = ("measured_width", "measured_height", "measured_codec", "measured_fps",
                   "measured_resolution_class", "measured_source", "measured_video_at")


def display_resolution(camera: dict) -> Optional[str]:
    """Text for a cameras row's video, e.g. "3840x2160 H265 20 fps (8MP)";
    with " - configured (not yet measured)" appended when only the camera's
    ONVIF configuration is known; None when nothing is known (callers then
    show NOT_MEASURED_TEXT -- never the plan's sales label as if it were the
    camera's real resolution)."""
    if not isinstance(camera, dict):
        return None
    width, height = _positive(camera.get("measured_width")), _positive(camera.get("measured_height"))
    if not width or not height:
        return None
    codec = camera.get("measured_codec")
    fps = _fps_value(camera.get("measured_fps"))
    klass = camera.get("measured_resolution_class") or resolution_class(width, height)
    label = {"2mp": "2MP", "4mp": "4MP", "5mp": "5MP", "8mp": "8MP"}.get(klass, f"{width * height / 1e6:.1f}MP")
    text = f"{width}x{height}{' ' + str(codec) if codec else ''}{f' {fps:g} fps' if fps else ''} ({label})"
    if camera.get("measured_source") == "configured":
        text += CONFIGURED_SUFFIX
    return text


def display_video(camera: dict) -> str:
    return display_resolution(camera) or NOT_MEASURED_TEXT


ANALYTICS_CHOICE_TEXT = {
    "substream_640": "substream (>= 640 px)",
    "substream_480_fallback": "substream 480-639 px (fallback)",
    "main_480_fallback_disabled": "main stream (480 px substream fallback disabled)",
    "main_no_usable_substream": "main stream (no substream >= 480 px)",
}


def admin_camera_video_html(cameras, escape) -> str:
    """Admin appliance card: one line per camera -- measured (or configured,
    labelled) resolution/codec/fps and which stream AI analyses."""
    lines = []
    for camera in cameras or []:
        if not isinstance(camera, dict) or camera.get("status") == "removed":
            continue
        choice = ANALYTICS_CHOICE_TEXT.get(camera.get("measured_analytics_choice"))
        lines.append(f'<p>{escape(str(camera.get("name") or "Camera"))} &middot; {escape(display_video(camera))}'
                     + (f' &middot; AI: {escape(choice)}' if choice else '') + '</p>')
    return (f'<details style="margin-top:10px"><summary>Camera video ({len(lines)})</summary>{"".join(lines)}</details>'
            if lines else '')


ADMIN_CAMERA_VIDEO_SQL = ("SELECT name,status,camera_number,measured_width,measured_height,measured_codec,measured_fps,"
                          "measured_resolution_class,measured_source,measured_analytics_choice FROM cameras "
                          "WHERE appliance_id=? ORDER BY camera_number,name")


def resolution_source(camera: dict) -> str:
    """"measured" | "configured" | "not_measured" for a cameras row."""
    if not display_resolution(camera):
        return "not_measured"
    return "configured" if camera.get("measured_source") == "configured" else "measured"
