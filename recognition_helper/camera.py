"""Persistent passive monitor and per-opening automatic capture.

Camera/ROI/MJPEG/motion/sharpness techniques reused from ARM-ripper-vision
arm-season-queue/app/camera.py (MIT), with a new specification-driven cycle.
"""
import threading
import time

import cv2
import numpy as np

from .tray_monitor import TrayMonitor


class Camera:
    def __init__(self, settings, on_open, on_capture, may_calibrate):
        self.s = settings
        self.on_open, self.on_capture, self.may_calibrate = on_open, on_capture, may_calibrate
        self.monitor = TrayMonitor(settings.drive)
        self.stop = threading.Event()
        self.preview = None
        self.status = {'tray': 'unknown', 'stage': 'waiting', 'message': 'Waiting for a manual empty opening'}
        self.cap = None
        self.cycle = None
        self.background = None
        self.previous = None
        self.samples = []
        self.stable_count = 0
        self.opened_at = None
        self.baseline_sharpness = None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='passive-camera-monitor')
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(5)
        self.monitor.close()

    def transition(self, reading, now):
        self.status['tray'] = reading['state']
        if reading.get('transition') == 'closed':
            self.status.update(stage='closed', message='Tray closed; follow extraction and recognition in the disc cycle below')
            self.cycle = None
            self.background = None
            self.baseline_sharpness = None
            self.previous = None
        if reading.get('transition') == 'open':
            allowed, reason = self.may_calibrate()
            self.background = None
            self.previous = None
            self.stable_count = 0
            self.samples = []
            if not allowed:
                self.status.update(stage='completed_disc', message=reason)
                return
            self.cycle = self.on_open()
            self.opened_at = now
            self.status.update(stage='calibrating', cycle_id=self.cycle,
                               message='Settling camera and tray; leave tray empty until ready')
        if reading.get('initial') and reading['state'] == 'open':
            self.status.update(stage='waiting', message='Tray was already open at startup. Close, wait about 10 seconds, then open it empty.')

    def frame(self, frame, now):
        original = frame.copy()
        if self.s.rotation == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        x1, y1, x2, y2 = map(float, self.s.roi.split(','))
        height, width = frame.shape[:2]
        frame = frame[int(y1 * height):int(y2 * height), int(x1 * width):int(x2 * width)]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (160, 100)).astype(np.float32)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        stable = self.previous is not None and np.mean(np.abs(small - self.previous)) < 3
        usable = 5 < float(gray.mean()) < 250 and float(gray.std()) > 8 and np.mean(gray > 250) < .55
        self.status.update(connected=True, sharpness=round(sharpness, 1), resolution=f'{width}x{height}')
        preview = cv2.resize(frame, (960, max(1, round(960 * frame.shape[0] / frame.shape[1]))))
        self.preview = cv2.imencode('.jpg', preview, [cv2.IMWRITE_JPEG_QUALITY, 75])[1].tobytes()
        stage = self.status['stage']
        if stage == 'calibrating':
            if now - self.opened_at > 60:
                self.status.update(stage='calibration_failed', message='No stable usable empty view. Close tray, wait about 10 seconds, reopen empty.')
            elif now - self.opened_at >= self.s.settle:
                self.stable_count = self.stable_count + 1 if stable and usable and sharpness > 3 else 0
                if self.stable_count >= 10:
                    self.background = small.copy()
                    self.baseline_sharpness = sharpness
                    self.status.update(stage='ready', message='READY FOR DISC — place it in the open tray')
                    self.stable_count = 0
        elif stage in ('ready', 'capturing') and self.background is not None:
            changed = float(np.mean(np.abs(small - self.background) > 25))
            present = changed > .12
            # Whole-frame edge variance depends on label texture. A sparse white
            # CD must not fail the threshold calibrated for densely printed DVDs.
            required = max(3, min(self.s.sharpness, (self.baseline_sharpness or self.s.sharpness * 2) * .5))
            self.status['capture_sharpness_required'] = round(required, 1)
            quality = stable and usable and sharpness >= required
            if not (present and quality):
                self.samples = []
            self.status.update(change_fraction=round(changed, 3), object_present=present)
            self.stable_count = self.stable_count + 1 if present and quality else 0
            if present:
                self.status.update(stage='capturing', message='Disc/change detected; waiting for hands and image to settle')
            if not present:
                self.samples = []
                self.status.update(stage='ready', message='READY FOR DISC — place it in the open tray')
            if self.stable_count in (12, 16):
                self.samples.append((original.copy(), frame.copy()))
            if self.stable_count >= 16 and len(self.samples) == 2:
                self.status.update(stage='captured', message='Snapshots retained; closing tray and starting recognition/extraction in parallel')
                samples, self.samples = self.samples, []
                self.on_capture(self.cycle, samples)
        self.previous = small

    def run(self):
        while not self.stop.is_set():
            now = time.monotonic()
            try:
                reading = self.monitor.poll(now)
                self.transition(reading, now)
                if reading['state'] == 'open' and self.status['stage'] not in ('completed_disc', 'waiting', 'calibration_failed', 'capture_failed'):
                    if self.cap is None:
                        self.cap = cv2.VideoCapture(self.s.camera, cv2.CAP_V4L2)
                        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.s.width)
                        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.s.height)
                        self.cap.set(cv2.CAP_PROP_FPS, 15)
                    ok, frame = self.cap.read()
                    if not ok:
                        raise ValueError('Webcam unavailable; check device mapping and camera connection')
                    self.frame(frame, now)
                elif self.cap is not None:
                    self.cap.release()
                    self.cap = None
                    self.status['connected'] = False
            except Exception as error:
                self.status.update(connected=False, message=str(error))
                if self.status['stage'] == 'calibrating':
                    self.status.update(stage='calibration_failed', message='Camera calibration failed. Close tray, wait about 10 seconds, reopen empty.')
                elif self.status['stage'] == 'captured':
                    self.status.update(stage='capture_failed', message='Snapshot retention failed; tray left open. Fix storage/camera error, then close and reopen empty.')
                if self.cap is not None:
                    self.cap.release()
                    self.cap = None
            self.stop.wait(.15)
        if self.cap is not None:
            self.cap.release()
