"""Account link requests on Windows (2026-10-08).

The Windows counterpart of link_server.py (the Linux appliance's loopback
"AnyAiCam Setup" page), WITHOUT a local web page or any listening socket:
on a Windows PC several people may be signed in, and a loopback page would
let any of them link the PC to their own account. Here only an
administrator of the PC can ask for a link:

  <state_dir>\\link-requests\\   (C:\\ProgramData\\AnyAiCam\\agent\\link-requests)
      inherits the data folder's SYSTEM/Administrators-only permissions, so
      a standard user can neither create a request nor read an answer.
  <nonce>.request    written by the setup or by open-claim-link.ps1, both
                     running elevated; nonce = 32 lowercase hex characters.
                     Its content is ignored.
  <nonce>.response   written by this agent (LocalSystem), atomically:
                     {"status": "ok", "link": "<portal>/claim#code=XXXXXXXX&link=windows"}
                     {"status": "linked"}       this PC already is
                     {"status": "unavailable"}  the cloud cannot be reached
                                                or no claim can be opened now
                     The requester reads and deletes it; one left over is
                     deleted after RESPONSE_MAX_AGE seconds.

The link carries a fresh claim code (HeadlessClaim.link_code() in on-demand
mode: no label, no standing claim) only in its URL fragment; each request
replaces the previous code, and the code dies with the 15-minute claim. The
code is never logged.
"""
from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Callable

from .headless_claim import CLAIM_CODE_PATTERN, portal_origin

FOLDER_NAME = 'link-requests'
NONCE_PATTERN = re.compile(r'^[0-9a-f]{32}$')
POLL_SECONDS = 1.0
REQUEST_MAX_AGE = 120     # an older request is dropped unanswered
# Right after the service starts (the setup asks at once) the claim may not
# exist yet: a request waits this long for it before the answer is
# "unavailable".
STARTUP_GRACE = 20
RESPONSE_MAX_AGE = 120    # an unread answer (it holds a live code) is deleted
TEMPORARY_MAX_AGE = 60
REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


def request_folder(state_dir) -> Path:
    return Path(state_dir) / FOLDER_NAME


def _is_plain_file(entry: os.DirEntry) -> bool:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & REPARSE_POINT)


def _remove(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class LinkRequests:
    """Answers link requests from a background thread for the agent's
    lifetime. headless() returns the agent's HeadlessClaim (or None while
    there is none); linked() is True once the agent holds its credential."""

    def __init__(self, config, log, headless: Callable[[], object], linked: Callable[[], bool],
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.log = log
        self.headless = headless
        self.linked = linked
        self.clock = clock
        self.folder = request_folder(config.state_dir)
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> 'LinkRequests':
        self.folder.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name='anyaicam-link-requests', daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as error:  # never let this break the agent
                self.log.warning('Account link requests could not be read (%s).', type(error).__name__)
            self._stop.wait(POLL_SECONDS)

    def poll_once(self) -> int:
        """Answers every pending request once; returns how many."""
        answered = 0
        now = self.clock()
        if not self.folder.is_dir():
            return 0
        with os.scandir(self.folder) as entries:
            for entry in list(entries):
                name = entry.name
                age = now - self._mtime(entry)
                if name.endswith('.request'):
                    nonce = name[:-len('.request')]
                    if NONCE_PATTERN.match(nonce) and _is_plain_file(entry) and age <= REQUEST_MAX_AGE:
                        body = self.answer_for(age)
                        if body is None:
                            continue  # the claim is not ready yet; ask again next poll
                        self._write(nonce, body)
                        answered += 1
                    _remove(entry.path)
                elif name.endswith('.response'):
                    if age > RESPONSE_MAX_AGE or not _is_plain_file(entry):
                        _remove(entry.path)
                elif name.endswith('.tmp') and age > TEMPORARY_MAX_AGE:
                    _remove(entry.path)
        return answered

    @staticmethod
    def _mtime(entry: os.DirEntry) -> float:
        try:
            return entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            return 0.0

    def answer_for(self, age: float = STARTUP_GRACE) -> dict | None:
        """The answer to a request, or None to wait (the service has just
        started and its claim does not exist yet)."""
        if self.linked():
            return {'status': 'linked'}
        headless = self.headless()
        if headless is None and age < STARTUP_GRACE:
            return None
        code = headless.link_code() if headless is not None else None
        origin = portal_origin(self.config.portal_url)
        if not code or not CLAIM_CODE_PATTERN.match(str(code)) or not origin.startswith('https://'):
            self.log.info('An administrator asked to link this PC; no link could be prepared now.')
            return {'status': 'unavailable'}
        self.log.info('An administrator asked to link this PC; link prepared.')
        return {'status': 'ok', 'link': f'{origin}/claim#code={code}&link=windows'}

    def _write(self, nonce: str, answer: dict) -> None:
        body = json.dumps(answer)
        temporary = self.folder / f'{nonce}.tmp'
        target = self.folder / f'{nonce}.response'
        with open(temporary, 'w', encoding='utf-8') as handle:
            handle.write(body)
        os.replace(temporary, target)
