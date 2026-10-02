from datetime import datetime
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import pytest

from aaco import AacoCommand,Clarification,DeterministicLanguageAdapter,execute

class Vms:
 def authorized_camera(self,i,c): return c=='camera-4' and i['customer_id']=='a'
 def live_view(self,i,c): return ('live',c)
 def playback(self,i,c,s,e): return ('playback',c,s,e)
 def search_events(self,i,**kw): return kw
 def previous_event(self,i,c,b): return ('previous-event',c,b)
 def camera_status(self,i): return ['camera-4']
 def unlock_door(self,i,d):
  if d=='camera-name:ambiguous door': return Clarification('More than one authorized door matches that name.')
  if d=='camera-name:front door' and i['customer_id']=='a': return ('door-unlock',d)
  raise PermissionError('Door is unavailable.')

def test_families_and_ambiguity_fail_closed():
 p=DeterministicLanguageAdapter(); now=datetime(2026,9,15,12)
 assert p.parse('Show Camera 4',now=now).operation=='live_view'
 assert p.parse('Show Camera 4 yesterday at 3:30 PM',now=now).operation=='playback'
 assert p.parse('Show Camera 2 from 3:15 yesterday',now=now).operation=='playback'
 assert p.parse('Show the front entrance',now=now).camera_id=='camera-name:front entrance'
 assert p.parse('Show person events from the last 2 hours',now=now).operation=='event_search'
 assert p.parse('Which cameras are offline?',now=now).operation=='camera_status'
 assert isinstance(p.parse('Show the camera',now=now),Clarification)
 assert isinstance(p.parse('Return to live',now=now),Clarification)
 assert isinstance(p.parse('Show previous event',now=now),Clarification)
 assert isinstance(p.parse('Delete everything',now=now),Clarification)
 open_cmd=p.parse('Open Front Door',now=now)
 assert open_cmd.operation=='unlock_door' and open_cmd.camera_id=='camera-name:front door'
 unlock_cmd=p.parse('Unlock Front Door',now=now)
 assert unlock_cmd.operation=='unlock_door' and unlock_cmd.camera_id=='camera-name:front door'
 assert isinstance(p.parse('Open the door',now=now),Clarification)
 assert isinstance(p.parse('Unlock doors',now=now),Clarification)

def test_event_search_recognizes_the_full_investigate_category_taxonomy_not_just_person_vehicle():
 # 2026-09-19: widened alongside main.py's _aaco_event_category() so
 # AACO can find the same event categories the Investigate page's own
 # filter chips already offer -- motion/lpr/people_counting/intrusion,
 # not just the original person/vehicle/car set.
 p=DeterministicLanguageAdapter(); now=datetime(2026,9,15,12)
 assert p.parse('Show motion events from the last 2 hours',now=now).event_type=='motion'
 assert p.parse('Show lpr events from the last 2 hours',now=now).event_type=='lpr'
 assert p.parse('Show plate events from the last 2 hours',now=now).event_type=='lpr'
 assert p.parse('Show license plate events from the last 2 hours',now=now).event_type=='lpr'
 assert p.parse('Show people counting events from the last 2 hours',now=now).event_type=='people_counting'
 assert p.parse('Show intrusion events from the last 2 hours',now=now).event_type=='intrusion'
 assert p.parse('Show vehicle events from the last 2 hours',now=now).event_type=='vehicle'
 # "car" is intentionally left unnormalized here -- the VMS boundary
 # (main.py's _aaco_event_category()) does that, matching how "car"
 # already worked for this operation before this change.
 assert p.parse('Show car events from the last 2 hours',now=now).event_type=='car'

def test_unlock_door_bypasses_the_generic_camera_gate_and_can_return_a_clarification():
 v=Vms()
 assert execute(AacoCommand('unlock_door',camera_id='camera-name:front door'),identity={'customer_id':'a'},vms=v)==('door-unlock','camera-name:front door')
 try: execute(AacoCommand('unlock_door',camera_id='camera-name:front door'),identity={'customer_id':'b'},vms=v); assert False
 except PermissionError: pass
 result=execute(AacoCommand('unlock_door',camera_id='camera-name:ambiguous door'),identity={'customer_id':'a'},vms=v)
 assert isinstance(result,Clarification)
 try: execute(AacoCommand('unlock_door',camera_id=None),identity={'customer_id':'a'},vms=v); assert False
 except ValueError: pass

def test_tenant_boundary_and_context_navigation():
 v=Vms(); p=DeterministicLanguageAdapter(); now=datetime(2026,9,15,12)
 assert execute(AacoCommand('live_view','camera-4'),identity={'customer_id':'a'},vms=v)==('live','camera-4')
 try: execute(AacoCommand('live_view','camera-4'),identity={'customer_id':'b'},vms=v); assert False
 except PermissionError: pass
 c=p.parse('Go back 20 minutes',now=now,context={'camera_id':'camera-4','playback_at':now})
 assert execute(c,identity={'customer_id':'a'},vms=v)[0]=='playback'
 c=p.parse('Return to live',now=now,context={'camera_id':'camera-4'})
 assert execute(c,identity={'customer_id':'a'},vms=v)[0]=='live'
 c=p.parse('Show previous event',now=now,context={'camera_id':'camera-4','event_at':now})
 assert execute(c,identity={'customer_id':'a'},vms=v)[0]=='previous-event'


# ---------------------------------------------------------------------------
# 2026-09-23: naturalness broadening. A customer should be able to phrase a
# request for any of these already-supported actions however they naturally
# would -- with polite wrapping, a leading "AACO,", or a synonym verb -- and
# land on the exact same AacoCommand the grammar's own canonical phrasing
# produces. No new action/Operation is introduced anywhere below; every group
# resolves to one of the seven Operation values that already existed.
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 15, 12)


@pytest.mark.parametrize("text", [
    "Which cameras are offline?",
    "which cameras are offline",
    "what cameras are down",
    "any cameras not working?",
    "show me the camera status",
    "Can you tell me which cameras are down?",
    "AACO, what's the camera status?",
    "Could you please tell me which cameras are unavailable?",
])
def test_many_phrasings_of_camera_status_resolve_to_the_same_action(text):
    assert DeterministicLanguageAdapter().parse(text, now=NOW) == AacoCommand("camera_status")


@pytest.mark.parametrize("text", [
    "Show Camera 1",
    "show camera 1",
    "Can you please show me camera 1?",
    "AACO, pull up camera 1",
    "I want to see camera 1",
    "Could you bring up camera 1?",
    "Please display camera 1",
    "I'd like to see camera 1",
])
def test_many_phrasings_of_live_view_by_number_resolve_to_the_same_action(text):
    assert DeterministicLanguageAdapter().parse(text, now=NOW) == AacoCommand("live_view", camera_id="camera-1")


@pytest.mark.parametrize("text", [
    "Show the front entrance",
    "show front entrance",
    "Let me see the front entrance",
    "Can you display the front entrance?",
    "I'd like to see the front entrance",
    "AACO, could you please pull up the front entrance?",
])
def test_many_phrasings_of_live_view_by_name_resolve_to_the_same_action(text):
    assert DeterministicLanguageAdapter().parse(text, now=NOW) == AacoCommand("live_view", camera_id="camera-name:front entrance")


@pytest.mark.parametrize("text", [
    "Return to live",
    "return live",
    "go live",
    "go back to live",
    "switch to live",
    "AACO, go back to live view",
    "Can you return to live?",
])
def test_many_phrasings_of_return_to_live_resolve_to_the_same_action(text):
    context = {"camera_id": "camera-4"}
    assert DeterministicLanguageAdapter().parse(text, now=NOW, context=context) == AacoCommand("live_view", camera_id="camera-4")


@pytest.mark.parametrize("text", [
    "Show previous event",
    "previous event",
    "go to the previous event",
    "show me the last event",
    "AACO, show the previous event",
    "Could you show the last event?",
])
def test_many_phrasings_of_previous_event_resolve_to_the_same_action(text):
    context = {"camera_id": "camera-4", "event_at": NOW}
    assert DeterministicLanguageAdapter().parse(text, now=NOW, context=context) == AacoCommand("event_navigation", camera_id="camera-4", end=NOW)


@pytest.mark.parametrize("text", [
    "Show person events from the last 2 hours",
    "any person events in the last 2 hours",
    "were there any person events in the past 2 hours",
    "person events last 2 hours",
    "display person events from the past 2 hours",
])
def test_many_phrasings_of_event_search_resolve_to_the_same_action(text):
    from datetime import timedelta
    result = DeterministicLanguageAdapter().parse(text, now=NOW)
    assert result.operation == "event_search"
    assert result.event_type == "person"
    assert result.start == NOW - timedelta(hours=2)
    assert result.end == NOW


@pytest.mark.parametrize("text", [
    "Go back 20 minutes",
    "rewind 20 minutes",
    "back up 20 minutes",
    "Can you go back 20 minutes?",
    "Please rewind 20 minutes",
])
def test_many_phrasings_of_playback_navigation_resolve_to_the_same_action(text):
    context = {"camera_id": "camera-4", "playback_at": NOW}
    result = DeterministicLanguageAdapter().parse(text, now=NOW, context=context)
    assert result.operation == "playback_navigation"
    assert result.offset_minutes == 20
    assert result.camera_id == "camera-4"


@pytest.mark.parametrize("text", [
    "Unlock Front Door",
    "Open Front Door",
    "unlock front door",
    "Can you please unlock the front door?",
    "AACO, open the front door",
    "Could you unlock the front door?",
])
def test_many_phrasings_of_unlock_door_resolve_to_the_same_action(text):
    assert DeterministicLanguageAdapter().parse(text, now=NOW) == AacoCommand("unlock_door", camera_id="camera-name:front door")


@pytest.mark.parametrize("text", ["let me in", "can I come in", "Open the door", "unlock doors"])
def test_ambiguous_or_unnamed_door_requests_always_ask_for_clarification_never_unlock(text):
    """The one safety-critical property naturalness broadening must never
    weaken: however a customer phrases an unlock request, if no specific,
    authorized door name was given, AACO must ask which door -- never
    guess, never unlock anything by default. This directly covers the
    "let me in" phrasing broadening added alongside the door-unlock verb
    synonyms, proving it lands on the exact same safe Clarification path
    as the grammar's own pre-existing "open the door"/"unlock doors"
    bare-name handling, not a new, less-safe path."""
    result = DeterministicLanguageAdapter().parse(text, now=NOW)
    assert isinstance(result, Clarification)


def test_leading_aaco_address_and_polite_wrapping_are_stripped_consistently():
    """One assertion covering several axes of the same normalization at
    once: an address ("AACO,"), polite wrapping ("could you please"),
    and a trailing question mark all disappear without affecting the
    underlying command -- proving _normalize() is doing real, compounding
    work, not just handling one axis at a time."""
    plain = DeterministicLanguageAdapter().parse("Show Camera 4", now=NOW)
    wrapped = DeterministicLanguageAdapter().parse("AACO, could you please show me camera 4?", now=NOW)
    assert plain == wrapped == AacoCommand("live_view", camera_id="camera-4")


def test_naturalness_broadening_never_changes_unrelated_or_unsafe_commands():
    """Guards against the broadening being too aggressive: gibberish and
    destructive-sounding commands outside AACO's action set must still
    fall through to the exact same honest Clarification as before, not
    get accidentally captured by a broadened pattern."""
    p = DeterministicLanguageAdapter()
    assert isinstance(p.parse("Delete everything", now=NOW), Clarification)
    assert isinstance(p.parse("Completely unparseable gibberish", now=NOW), Clarification)
    assert isinstance(p.parse("Show the camera", now=NOW), Clarification)
