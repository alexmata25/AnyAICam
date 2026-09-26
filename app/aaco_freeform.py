"""Free-form natural-language input for AACO (2026-09-26).

AACO used to understand a request only when it matched one of the fixed
sentence shapes in aaco.DeterministicLanguageAdapter ("person events in the
last 2 hours", "show camera 4 yesterday at 3:15 PM", ...). Anything else was
either refused or -- worse -- swallowed by the grammar's catch-all
"show <anything>", which turned "show person events this morning" into a
live-view request for a camera called "person events this morning".

FreeFormIntentParser reads a sentence the way a person says it, typed or
spoken (the browser's speech recognition fills the same box and submits the
same request, so a voice transcript and typed text take exactly this path):
it finds the intent and fills the details from anywhere in the sentence --
which camera (matched against the names of the cameras this customer is
authorized for, supplied by the server), which kind of event, a time range
("this morning", "the last 20 minutes", "yesterday") or a point in time
("at 3:15 PM", "an hour ago"), or "the latest" -- and produces exactly one
of the existing aaco.AacoCommand operations. It never creates a new
operation and never emits a door unlock or talk command: those stay on the
exact grammar only, so a physical action can never come from a loose match.

When a detail the action needs is missing it asks one short question
("Which camera should I play back?"); when the sentence mentions two kinds
of event at once it asks which one; an ambiguous camera name is asked about
by the VMS boundary, which holds the real camera list. Everything still runs
through aaco.execute() and the boundary, so authorization is unchanged.

FreeFormAacoLanguageAdapter layers this on top of the existing adapters
without replacing them: the exact grammar (and the optional local model in
front of it) answers first, and every command it understands is returned
unchanged. The free-form reading is used only when the grammar has no
answer, or produced nothing better than its catch-all camera-name guess.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from aaco import (PREVIOUS_EVENT_NEEDS_CONTEXT, UNKNOWN_REQUEST_MESSAGE, AacoCommand, Clarification,
                  DeterministicLanguageAdapter)

HELP_MESSAGE = ("I didn't catch a camera request there. I can open live view, play back a camera at a time, "
                "find events such as people, vehicles or motion, show the latest event, and list offline cameras. "
                "For example: “Show the front door”, “Any people on the driveway today?” or "
                "“Play back the garage at 3:15 PM”.")
_LLM_FALLBACK_PREFIX = "I couldn't safely understand that request"

LATEST_LOOKBACK = timedelta(days=7)
DEFAULT_EVENT_WINDOW = timedelta(hours=24)
POINT_EVENT_WINDOW = timedelta(minutes=15)
PLAYBACK_LENGTH = timedelta(minutes=5)

# ------------------------------------------------------------ normalizing

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty five": 45, "forty-five": 45, "sixty": 60, "ninety": 90,
}
_PHRASES = (
    (r"\bhalf an hour\b|\bhalf hour\b", "30 minutes"),
    (r"\ba couple(?: of)? hours\b", "2 hours"),
    (r"\ba couple(?: of)? minutes\b", "2 minutes"),
    (r"\b(?:an|a) hour\b", "1 hour"),
    (r"\b(?:a|one) minute\b", "1 minute"),
    (r"\bnoon\b", "12:00 pm"),
    (r"\bmidnight\b", "12:00 am"),
    (r"\ba\.?m\.?(?=\s|$)", "am"),
    (r"\bp\.?m\.?(?=\s|$)", "pm"),
    (r"\b(?:o'clock|oclock)\b", ""),
)


def normalize(text: str) -> str:
    value = " ".join(str(text).lower().split())
    value = re.sub(r"^(?:ok(?:ay)? |hey |hi )?aaco[,:]?\s*", "", value)
    for pattern, replacement in _PHRASES:
        value = re.sub(pattern, replacement, value)
    value = re.sub(r"[^\w\s:'&-]", " ", value)  # keep 3:15, names with & ' -
    for word, number in sorted(_NUMBER_WORDS.items(), key=lambda item: -len(item[0])):
        value = re.sub(rf"\b{word}\b", str(number), value)
    value = re.sub(r"(\d)\s+(am|pm)\b", r"\1\2", value)  # "3 pm" -> "3pm"
    return " ".join(value.split())


def _has(value: str, pattern: str) -> bool:
    return re.search(pattern, value) is not None

# ------------------------------------------------------------ vocabulary

_PHYSICAL_ACTION = r"\b(?:unlock|lock|let me in|buzz|talk|speak|say something|talkdown|talk down)\b|\bopen (?:the |my )?(?:front |back |side |garage )?(?:door|gate)\b"
_EVENT_TYPES = (  # checked in order; a sentence naming two different kinds is asked about
    ("people_counting", r"\bpeople count(?:ing|s)?\b|\bhow many people\b|\bentries and exits\b"),
    ("lpr", r"\blicen[cs]e plates?\b|\bnumber plates?\b|\bplates?\b|\blpr\b"),
    ("intrusion", r"\bintrusions?\b|\bintruders?\b|\btrespass\w*"),
    ("person", r"\bpersons?\b|\bpeople\b|\bsome ?one\b|\bsome ?body\b|\bany ?one\b|\bany ?body\b|\bvisitors?\b|\bhumans?\b|\bpedestrians?\b|\bwho\b"),
    ("vehicle", r"\bvehicles?\b|\bcars?\b|\btrucks?\b|\bvans?\b|\bmotorcycles?\b|\bbikes?\b|\bbicycles?\b"),
    ("motion", r"\bmotion\b|\bmovement\b|\bmoving\b"),
)
_EVENT_GENERIC = r"\bevents?\b|\balerts?\b|\bdetections?\b|\bactivity\b|\bcame by\b|\bcome by\b|\bshowed up\b|\bdetected\b|\bnotifications?\b"
_RECENCY = r"\b(?:latest|newest|most recent)\b|\blast (?:event|alert|detection|one|thing|time|activity|visitor|person|car|vehicle|motion|notification)\b"
_PLAYBACK = r"\bplay ?back\b|\breplay\b|\brecordings?\b|\bfootage\b|\brewind\b|\bgo back to\b|\bplay\b|\brecorded\b"
_HAPPENED = r"\bwhat happened\b|\bwhat was going on\b|\bwhat went on\b"
_LIVE = r"\blive\b|\bright now\b|\bcurrently\b|\bat the moment\b|\bnow\b|\bgoing on\b|\bhappening\b"
_VIEW = r"\bshow\b|\bsee\b|\bwatch\b|\bview\b|\blook\b|\bdisplay\b|\bpull up\b|\bbring up\b|\bcheck\b|\bopen\b|\bgo to\b|\bswitch to\b"
_STATUS = r"\boffline\b|\bonline\b|\bdown\b|\bnot working\b|\bworking\b|\bdisconnected\b|\bconnected\b|\bstatus\b|\bbroken\b|\bup and running\b|\bhealthy\b"
_STATUS_SUBJECT = r"\bcameras?\b|\beverything\b|\ball\b|\banything\b|\bsystem\b"
_COMMAND_PHRASES = (r"\bplay ?back\b|\bgo(?:ing)? back(?: to)?\b|\bcome back\b|\bcame back\b|\bback up\b|\brewind\b|"
                    r"\blook(?:ing)? at\b|\bpull up\b|\bbring up\b|\bswitch to\b|\bgo to\b|\bcame by\b|\bcome by\b|"
                    r"\bshowed up\b|\bright now\b|\b(?:last|past|previous) \d+ \w+|\b\d+ \w+ ago\b|\bthis (?:morning|afternoon|evening|week)\b|"
                    r"\blast night\b|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b")
_STOP = {"the", "my", "our", "a", "an", "camera", "cameras", "cam", "view", "feed", "please", "of", "at", "on", "in",
         "by", "near", "from", "to", "for", "and", "me", "show", "see", "is", "are", "was", "were", "any", "there", "what",
         "who", "today", "yesterday", "live", "now", "right", "last", "latest", "recent", "most", "events", "event"}

# ------------------------------------------------------------ time


def _at(now: datetime, day: datetime, hour: int, minute: int) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _time_point(value: str, now: datetime) -> tuple[datetime | None, bool]:
    """(moment, invalid). A clock time or "N minutes/hours ago"."""
    ago = re.search(r"\b(\d{1,3}) (minute|min|hour|hr)s? ago\b", value)
    if ago:
        amount = int(ago.group(1))
        return now - (timedelta(hours=amount) if ago.group(2).startswith("h") else timedelta(minutes=amount)), False
    # Speech recognition often writes "nine fifteen a.m." -> "9 15am" / "9 15".
    value = re.sub(r"\b(\d{1,2}) ([0-5]\d)(?=(?:am|pm)\b| (?:am|pm|today|yesterday|this|on|in)\b|$)", r"\1:\2", value)
    clock = (re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", value)
             or re.search(r"\b(\d{1,2})(am|pm)\b", value)
             or re.search(r"\b(?:at|around|about|from|starting|since) (\d{1,2})\b(?!:)(?! (?:minute|min|hour|hr|day)s?\b)", value))
    if not clock:
        return None, False
    groups = clock.groups()
    hour = int(groups[0])
    minute = int(groups[1]) if len(groups) > 1 and groups[1] and groups[1].isdigit() else 0
    ampm = next((g for g in groups[1:] if g in ("am", "pm")), None)
    if minute > 59 or hour > 23 or (ampm and (hour == 0 or hour > 12)):
        return None, True
    if _has(value, r"\bthis afternoon\b|\bthis evening\b|\btonight\b|\blast night\b") and not ampm and hour < 12:
        ampm = "pm"
    elif _has(value, r"\bthis morning\b") and not ampm:
        ampm = "am"
    day = now - timedelta(days=1) if _has(value, r"\byesterday\b|\blast night\b") else now
    if ampm:
        hour = hour % 12 + (12 if ampm == "pm" else 0)
        moment = _at(now, day, hour, minute)
    elif hour > 12 or _has(value, r"\byesterday\b|\btoday\b"):
        moment = _at(now, day, hour, minute)
    else:  # "at 3" -- the most recent 3:00, morning or afternoon
        options = [_at(now, now, hour % 12 + extra, minute) for extra in (0, 12)]
        past = [option for option in options if option <= now]
        moment = max(past) if past else max(options) - timedelta(days=1)
    if moment > now and not _has(value, r"\btoday\b|\btomorrow\b"):
        moment -= timedelta(days=1)  # "at 11 PM" said at 9 AM means last night
    return moment, False


def _time_range(value: str, now: datetime) -> tuple[datetime, datetime] | None:
    last = re.search(r"\b(?:last|past|previous) (\d{1,4}) (minute|min|hour|hr|day)s?\b", value)
    if last:
        amount, unit = int(last.group(1)), last.group(2)
        span = timedelta(days=amount) if unit == "day" else timedelta(hours=amount) if unit.startswith("h") else timedelta(minutes=amount)
        return now - span, now
    if _has(value, r"\b(?:last|past) (?:hour|hr)\b"):
        return now - timedelta(hours=1), now
    if _has(value, r"\b(?:last|past) (?:day|24 hours)\b"):
        return now - timedelta(days=1), now
    if _has(value, r"\b(?:this|past|last) week\b"):
        return now - timedelta(days=7), now
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if _has(value, r"\blast night\b|\bovernight\b"):
        return midnight - timedelta(hours=6), min(now, midnight + timedelta(hours=6))
    if _has(value, r"\byesterday\b"):
        return midnight - timedelta(days=1), midnight
    for pattern, start_hour, end_hour in ((r"\bthis morning\b", 5, 12), (r"\bthis afternoon\b", 12, 17),
                                          (r"\bthis evening\b|\btonight\b", 17, 24)):
        if _has(value, pattern):
            start = midnight + timedelta(hours=start_hour)
            return start, min(now, midnight + timedelta(hours=end_hour))
    if _has(value, r"\btoday\b|\bso far\b"):
        return midnight, now
    return None

# ------------------------------------------------------------ camera


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9&'-]+", text)


def _camera(value: str, camera_names) -> str | None:
    """A camera token for aaco's grammar ("camera-<n>" or
    "camera-name:<phrase>") -- the boundary resolves and authorizes it, and
    asks when it matches more than one camera."""
    numbered = re.search(r"\b(?:camera|cam) (\d{1,4})\b", value)
    if numbered:
        return f"camera-{numbered.group(1)}"
    names = [" ".join(str(name).lower().split()) for name in camera_names or () if str(name).strip()]
    exact = [name for name in names if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", value)]
    if exact:
        return f"camera-name:{max(exact, key=len)}"
    # Words that belong to the command itself never count toward a camera
    # name: "play back 3:15" must not pick a camera called "Back Lot".
    loose = re.sub(_COMMAND_PHRASES, " ", value)
    text_words = [word for word in _words(loose) if word not in _STOP]
    if names:
        best, hits = 0, []
        for name in names:
            overlap = [word for word in text_words if word in set(_words(name)) - _STOP]
            if len(overlap) > best:
                best, hits = len(overlap), [(name, overlap)]
            elif overlap and len(overlap) == best:
                hits.append((name, overlap))
        if hits:
            if len(hits) == 1:
                return f"camera-name:{hits[0][0]}"
            return "camera-name:" + " ".join(dict.fromkeys(word for _, words in hits for word in words))
        return None
    # No camera list (e.g. a boundary without camera_names): the phrase after a place preposition.
    match = re.search(r"\b(?:at|on|from|by|near|outside|in front of|of|in) (?:the |my |our )?([a-z][a-z0-9 &'-]{1,60}?)"
                      r"(?= (?:camera|cam)\b| (?:at|since|from|around|today|yesterday|this|last|in the|during)\b|$)", value)
    if match:
        phrase = " ".join(word for word in _words(match.group(1)) if word not in _STOP)
        if phrase and not re.fullmatch(r"\d+|am|pm|\d+(?:am|pm)", phrase):
            return f"camera-name:{phrase}"
    return None


def _camera_label(token: str) -> str:
    if token.startswith("camera-name:"):
        return token.removeprefix("camera-name:").title()
    return "Camera " + token.removeprefix("camera-")

# ------------------------------------------------------------ interpreter


class FreeFormIntentParser:
    def parse(self, text: str, *, now: datetime, camera_names=()) -> AacoCommand | Clarification | None:
        value = normalize(text)
        if not value or _has(value, _PHYSICAL_ACTION):
            return None  # doors/talk: exact grammar only
        kinds = [kind for kind, pattern in _EVENT_TYPES if _has(value, pattern)]
        if "people_counting" in kinds and "person" in kinds:
            kinds.remove("person")  # "people counting" is one kind
        camera = _camera(value, camera_names)
        point, invalid_time = _time_point(value, now)
        span = _time_range(value, now)
        recency = _has(value, _RECENCY)
        eventish = bool(kinds) or _has(value, _EVENT_GENERIC) or recency
        playbackish = _has(value, _PLAYBACK)
        happened = _has(value, _HAPPENED)

        if _has(value, _STATUS) and _has(value, _STATUS_SUBJECT) and not eventish and not point:
            return AacoCommand("camera_status")
        if invalid_time:
            return Clarification("Use a valid time, for example 3:15 PM.")
        if len(set(kinds)) > 1:
            names = {"person": "people", "vehicle": "vehicles", "motion": "motion", "lpr": "license plates",
                     "people_counting": "people counting", "intrusion": "intrusions"}
            return Clarification(f"Which should I look for: {' or '.join(names[k] for k in kinds)}? Ask for one at a time.")

        # Playback: a moment in time on a camera ("the driveway at 3:15", "play back the porch an hour ago").
        if (playbackish and not recency and not kinds) or (point and (happened or not eventish)):
            if not camera:
                return Clarification("Which camera should I play back?")
            if not point:
                example = f"“{_camera_label(camera)} at 8:30 AM”"
                return Clarification(f"What time should playback start? For example, {example}.")
            return AacoCommand("playback", camera_id=camera, start=point, end=point + PLAYBACK_LENGTH)

        if eventish or happened:
            event_type = kinds[0] if kinds else None
            if recency and not span and not point:
                return AacoCommand("event_search", camera_id=camera, event_type=event_type,
                                   start=now - LATEST_LOOKBACK, end=now, limit=1)
            if point:
                start, end = point - POINT_EVENT_WINDOW, min(now, point + POINT_EVENT_WINDOW)
            elif span:
                start, end = span
            else:
                start, end = now - DEFAULT_EVENT_WINDOW, now
            return AacoCommand("event_search", camera_id=camera, event_type=event_type, start=start, end=end,
                               limit=1 if recency else None)

        if camera and (_has(value, _VIEW) or _has(value, _LIVE) or len(_words(value)) <= 4):
            return AacoCommand("live_view", camera_id=camera)
        if _has(value, _VIEW) and _has(value, r"\bcameras?\b|\blive\b|\bfeed\b"):
            return Clarification("Which camera would you like to see?")
        return None


class FreeFormAacoLanguageAdapter:
    """The adapter AACO uses: existing adapters first, free-form second."""

    def __init__(self, base=None, parser: FreeFormIntentParser | None = None):
        self._base = base or DeterministicLanguageAdapter()
        self._free = parser or FreeFormIntentParser()

    def parse(self, text: str, *, now: datetime, context: dict | None = None) -> AacoCommand | Clarification:
        context = dict(context or {})
        camera_names = context.pop("camera_names", ()) or ()
        base = self._base.parse(text, now=now, context=context)
        generic_guess = isinstance(base, AacoCommand) and base.operation == "live_view" and (base.camera_id or "").startswith("camera-name:")
        if isinstance(base, AacoCommand) and not generic_guess:
            return base  # an exact grammar (or validated model) command -- unchanged
        replaceable = isinstance(base, Clarification) and (
            base.message in (UNKNOWN_REQUEST_MESSAGE, PREVIOUS_EVENT_NEEDS_CONTEXT) or base.message.startswith(_LLM_FALLBACK_PREFIX))
        if isinstance(base, Clarification) and not replaceable:
            return base  # a specific question the grammar already asks (doors, playback time, ...)
        free = self._free.parse(text, now=now, camera_names=camera_names)
        if generic_guess:
            # The grammar only guessed "a camera called <everything after show>";
            # prefer a real reading (events, playback, status, a question), or
            # the same live view naming one of this customer's actual cameras.
            known = {"camera-name:" + " ".join(str(name).lower().split()) for name in camera_names}
            if free is None:
                return base
            if isinstance(free, AacoCommand) and free.operation == "live_view":
                # Only when that camera's full name is actually said -- a
                # partial-word match never overrides the grammar's reading.
                said = free.camera_id in known and re.search(
                    rf"(?<![\w]){re.escape(free.camera_id.removeprefix('camera-name:'))}(?![\w])", normalize(text))
                return free if said and base.camera_id not in known else base
            return free
        if base.message == PREVIOUS_EVENT_NEEDS_CONTEXT:
            # "previous event" still needs a selected event; only "the last/latest
            # event" becomes a search for the newest one.
            return free if isinstance(free, AacoCommand) and free.limit == 1 else base
        return free if free is not None else Clarification(HELP_MESSAGE)
