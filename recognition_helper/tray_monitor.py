"""Passive Linux tray polling, adapted from ARM-ripper-vision (MIT).

An unknown sample is not an opening/closing; preserve last known physical state
so a not-ready interval does not hide an actual subsequent transition.
"""
import fcntl
import os
import time


class TrayMonitor:
    def __init__(self, device, reader=None):
        self.device, self.reader = device, reader
        self.fd = None
        self.state = 'unknown'
        self.last_known = None
        self.candidate = None
        self.count = 0
        self.last_poll = 0

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        result = {'state': self.state, 'transition': None}
        if now - self.last_poll < .5:
            return result
        self.last_poll = now
        try:
            if self.reader:
                raw = self.reader()
            else:
                if self.fd is None:
                    self.fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                raw = fcntl.ioctl(self.fd, 0x5326, 0xFFFF)
            candidate = 'open' if raw == 2 else 'closed' if raw in (1, 4) else 'unknown'
            result['raw_status'] = raw
        except OSError:
            candidate = 'unknown'
            result['error'] = 'Drive status unavailable'
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
        if candidate == 'unknown':
            self.state = 'unknown'
            self.count = 0
            return dict(result, state='unknown')
        self.count = self.count + 1 if self.candidate == candidate else 1
        self.candidate = candidate
        if self.count >= 3:
            if self.last_known is None:
                result['initial'] = True
            elif self.last_known != candidate:
                result['transition'] = candidate
            self.state = self.last_known = candidate
        return dict(result, state=self.state)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
