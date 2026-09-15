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
 def camera_status(self,i): return ['camera-4']

def test_families_and_ambiguity_fail_closed():
 p=DeterministicLanguageAdapter(); now=datetime(2026,9,15,12)
 assert p.parse('Show Camera 4',now=now).operation=='live_view'
 assert p.parse('Show Camera 4 yesterday at 3:30 PM',now=now).operation=='playback'
 assert p.parse('Show person events from the last 2 hours',now=now).operation=='event_search'
 assert p.parse('Which cameras are offline?',now=now).operation=='camera_status'
 assert isinstance(p.parse('Show the camera',now=now),Clarification)
 assert isinstance(p.parse('Delete everything',now=now),Clarification)

def test_tenant_boundary_and_context_navigation():
 v=Vms(); p=DeterministicLanguageAdapter(); now=datetime(2026,9,15,12)
 assert execute(AacoCommand('live_view','camera-4'),identity={'customer_id':'a'},vms=v)==('live','camera-4')
 try: execute(AacoCommand('live_view','camera-4'),identity={'customer_id':'b'},vms=v); assert False
 except PermissionError: pass
 c=p.parse('Go back 20 minutes',now=now,context={'camera_id':'camera-4','playback_at':now})
 assert execute(c,identity={'customer_id':'a'},vms=v)[0]=='playback'
