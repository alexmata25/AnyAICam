import unittest

from anyaicam_agent.camera_binding import MAX_CAMERA_NUMBER
from anyaicam_agent.commands import _validate_relay_camera_number


class CameraNumberCapacityTests(unittest.TestCase):
    def test_agent_and_relay_commands_share_the_supported_limit(self):
        self.assertEqual(MAX_CAMERA_NUMBER, 10_000)
        self.assertEqual(_validate_relay_camera_number(10_000), 10_000)
        self.assertIsNone(_validate_relay_camera_number(10_001))
        self.assertIsNone(_validate_relay_camera_number(True))


if __name__ == "__main__":
    unittest.main()
