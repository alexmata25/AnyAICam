import tempfile
import unittest
from pathlib import Path

from appliance_protocol import OfflineQueue, RateLimiter, health_state, sanitize_appliance_payload, validate_request_time


class ApplianceProtocolTests(unittest.TestCase):
    def test_timestamp_window_rejects_replay_age(self):
        self.assertTrue(validate_request_time(1000,now=1299))
        self.assertFalse(validate_request_time(1000,now=1301))

    def test_rate_limiter(self):
        limiter=RateLimiter(limit=2,window_seconds=60)
        self.assertTrue(limiter.allow('a',now=1)); self.assertTrue(limiter.allow('a',now=2)); self.assertFalse(limiter.allow('a',now=3)); self.assertTrue(limiter.allow('a',now=70))

    def test_camera_credentials_are_removed(self):
        safe=sanitize_appliance_payload({'camera':{'name':'Front','username':'admin','password':'secret','rtsp_url':'rtsp://secret','online':True}})
        self.assertEqual(safe,{'camera':{'name':'Front','online':True}})

    def test_health_warnings(self):
        state,warnings=health_state({'cpu':95,'disk_capacity':100,'disk_used':92,'cameras':[{'online':False,'recording':False}]})
        self.assertEqual(state,'degraded'); self.assertEqual(warnings,['camera_offline','high_cpu','low_disk'])

    def test_offline_queue_is_idempotent_and_backs_off(self):
        with tempfile.TemporaryDirectory() as folder:
            queue=OfflineQueue(Path(folder)/'queue.db')
            self.assertTrue(queue.enqueue('event-1','event',{'id':'event-1','password':'remove-me'})); self.assertFalse(queue.enqueue('event-1','event',{'id':'event-1'}))
            ready=queue.ready(now=10**12); self.assertEqual(len(ready),1); self.assertNotIn('remove-me',ready[0]['payload_json'])
            delay=queue.failed('event-1',now=100); self.assertEqual(delay,2); self.assertEqual(queue.ready(now=101),[]); self.assertEqual(len(queue.ready(now=102)),1)
            queue.succeeded('event-1'); self.assertEqual(queue.ready(now=10**12),[])


if __name__=='__main__': unittest.main()
