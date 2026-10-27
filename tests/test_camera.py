import unittest
from unittest.mock import Mock

import numpy as np

from recognition_helper.camera import Camera
from recognition_helper.config import Settings


class CameraTest(unittest.TestCase):
    def test_capture_threshold_adapts_to_empty_scene_texture(self):
        camera = Camera(Settings(roi='0,0,1,1', sharpness=60), Mock(), Mock(), lambda: (True, None))
        camera.status['stage'] = 'ready'
        camera.background = np.zeros((100, 160), dtype=np.float32)
        camera.baseline_sharpness = 20
        frame = np.random.default_rng(4).integers(30, 180, (100, 160, 3), dtype=np.uint8)
        camera.frame(frame, 1)
        self.assertEqual(camera.status['capture_sharpness_required'], 10)

    def setUp(self):
        self.on_open = Mock(return_value='cycle1')
        self.on_capture = Mock()
        self.camera = Camera(Settings(roi='0,0,1,1', sharpness=1, settle=1), self.on_open, self.on_capture, lambda: (True, None))

    def test_open_calibrates_without_empty_confirmation(self):
        self.camera.transition({'state': 'open', 'transition': 'open'}, 10)
        rng = np.random.default_rng(1)
        empty = rng.integers(30, 180, (100, 160, 3), dtype=np.uint8)
        for n in range(14):
            self.camera.frame(empty, 12 + n * .2)
        self.assertEqual(self.camera.status['stage'], 'ready')
        self.assertEqual(self.on_open.call_count, 1)

    def test_motion_after_first_sample_does_not_stick_capture(self):
        rng = np.random.default_rng(2)
        empty = rng.integers(30, 90, (100, 160, 3), dtype=np.uint8)
        disc = rng.integers(120, 200, (100, 160, 3), dtype=np.uint8)
        self.camera.transition({'state': 'open', 'transition': 'open'}, 1)
        for n in range(14):
            self.camera.frame(empty, 3 + n * .2)
        for n in range(13):
            self.camera.frame(disc, 6 + n * .2)
        self.camera.frame(empty, 9)  # interrupted stable interval
        for n in range(18):
            self.camera.frame(disc, 10 + n * .2)
        self.assertEqual(self.on_capture.call_count, 1)
        self.assertEqual(len(self.on_capture.call_args.args[1]), 2)

    def test_postrip_open_does_not_calibrate(self):
        self.camera.may_calibrate = lambda: (False, 'Remove completed disc')
        self.camera.transition({'state': 'open', 'transition': 'open'}, 1)
        self.assertEqual(self.camera.status['stage'], 'completed_disc')
        self.assertFalse(self.on_open.called)

    def test_already_open_startup_is_not_a_fabricated_transition(self):
        self.camera.transition({'state': 'open', 'initial': True}, 1)
        self.assertFalse(self.on_open.called)

    def test_calibration_failure_reopening_retries(self):
        self.camera.status['stage'] = 'calibration_failed'
        self.camera.transition({'state': 'closed', 'transition': 'closed'}, 1)
        self.camera.transition({'state': 'open', 'transition': 'open'}, 12)
        self.assertEqual(self.camera.status['stage'], 'calibrating')
