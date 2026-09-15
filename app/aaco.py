"""AACO Phase 1: provider-independent, fail-closed VMS command boundary."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
import re

Operation = Literal['live_view','playback','event_search','camera_status','playback_navigation']

@dataclass(frozen=True)
class AacoCommand:
    operation: Operation
    camera_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    event_type: str | None = None
    offset_minutes: int | None = None

@dataclass(frozen=True)
class Clarification:
    message: str

class RecordingResolver(Protocol):
    """Future storage-neutral resolver. Phase 1 never creates media."""
    def find_existing(self, *, camera_id: str, start: datetime, end: datetime) -> object: ...

class VmsBoundary(Protocol):
    def authorized_camera(self, identity: dict, camera_id: str) -> dict | None: ...
    def live_view(self, identity: dict, camera_id: str) -> object: ...
    def playback(self, identity: dict, camera_id: str, start: datetime, end: datetime) -> object: ...
    def search_events(self, identity: dict, *, event_type: str | None, camera_id: str | None, start: datetime, end: datetime) -> object: ...
    def camera_status(self, identity: dict) -> object: ...

class DeterministicLanguageAdapter:
    """Safe Phase-1 fixture parser; future LLMs may only emit AacoCommand."""
    def parse(self, text: str, *, now: datetime, context: dict | None = None) -> AacoCommand | Clarification:
        value=' '.join(text.lower().split())
        if value in {'which cameras are offline?','which cameras are offline'}:
            return AacoCommand('camera_status')
        if value.startswith('go back '):
            m=re.fullmatch(r'go back (\d+) minutes?',value)
            if not m or not context or not context.get('camera_id') or not context.get('playback_at'):
                return Clarification('Select a camera and playback time before navigating.')
            at=context['playback_at']-timedelta(minutes=int(m.group(1)))
            return AacoCommand('playback_navigation',camera_id=context['camera_id'],start=at,end=at+timedelta(minutes=1),offset_minutes=int(m.group(1)))
        m=re.fullmatch(r'show (person|vehicle|car) events from the last (\d+) hours?',value)
        if m: return AacoCommand('event_search',event_type=m.group(1),start=now-timedelta(hours=int(m.group(2))),end=now)
        m=re.fullmatch(r'show camera (\d+)',value)
        if m: return AacoCommand('live_view',camera_id=f'camera-{m.group(1)}')
        m=re.fullmatch(r'show camera (\d+) yesterday at (\d{1,2}):(\d{2}) ?([ap]m)',value)
        if m:
            hour=int(m.group(2))%12+(12 if m.group(4)=='pm' else 0); start=(now-timedelta(days=1)).replace(hour=hour,minute=int(m.group(3)),second=0,microsecond=0)
            return AacoCommand('playback',camera_id=f'camera-{m.group(1)}',start=start,end=start+timedelta(minutes=5))
        return Clarification('I can show an authorized camera, playback, events, offline cameras, or navigate current playback.')

def execute(command: AacoCommand, *, identity: dict, vms: VmsBoundary) -> object:
    """Only named operations cross this boundary; no SQL, shell, or storage access."""
    if command.operation=='camera_status': return vms.camera_status(identity)
    if command.operation=='event_search': return vms.search_events(identity,event_type=command.event_type,camera_id=command.camera_id,start=command.start,end=command.end)
    if not command.camera_id or not vms.authorized_camera(identity,command.camera_id):
        raise PermissionError('Camera is unavailable.')
    if command.operation=='live_view': return vms.live_view(identity,command.camera_id)
    if command.operation in {'playback','playback_navigation'}:
        if not command.start or not command.end: raise ValueError('Playback range required.')
        return vms.playback(identity,command.camera_id,command.start,command.end)
    raise ValueError('Unsupported AACO command.')
