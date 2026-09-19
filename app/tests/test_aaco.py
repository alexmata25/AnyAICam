from datetime import datetime
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
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
