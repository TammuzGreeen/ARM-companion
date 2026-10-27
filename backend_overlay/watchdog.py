"""Bounded no-progress recovery for the existing ARM MakeMKV subprocess only."""
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


class Watchdog:
    def __init__(self, proc, env):
        self.proc = proc
        self.path = Path(env['RH_RUNTIME_PATH'])
        self.output = Path(env['RH_OUTPUT_DIRECTORY'])
        self.limit = int(env.get('VIDEO_STALL_SECONDS', '1800'))
        if self.limit < 60:
            raise ValueError('VIDEO_STALL_SECONDS must be at least 60')
        self.stop = threading.Event()
        self.last = time.time()
        self.last_tick = time.monotonic()
        self.lock = threading.Lock()
        self.progress = {}
        self.size = 0
        self.stalled = False
        self.thread = threading.Thread(target=self.monitor, daemon=True)

    def write(self, state):
        with self.lock:
            data = {'pid': self.proc.pid, 'state': state, 'last_progress_at': self.last,
                'output_bytes': self.size, 'progress': dict(self.progress), 'stall_seconds': self.limit,
                'updated_at': time.time()}
        try:
            temp = self.path.with_suffix('.new')
            temp.write_text(json.dumps(data))
            temp.replace(self.path)
        except OSError:
            logging.exception('Cannot write MakeMKV watchdog evidence')

    def start(self):
        self.write('progressing')
        self.thread.start()

    def observe(self, line):
        kind = line.split(':', 1)[0]
        with self.lock:
            if kind in ('PRGV', 'PRGC', 'PRGT') and line != self.progress.get(kind):
                self.progress[kind] = line
                self.last, self.last_tick = time.time(), time.monotonic()

    def terminate(self):
        # Only the unreaped child created with start_new_session=True is owned.
        if self.proc.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                self.proc.wait(timeout=15)
                return
            except subprocess.TimeoutExpired:
                continue

    def check(self):
        size = 0
        for path in self.output.glob('*.mkv'):
            try:
                size += path.stat().st_size
            except FileNotFoundError:
                continue
        with self.lock:
            if size > self.size:
                self.size, self.last, self.last_tick = size, time.time(), time.monotonic()
            expired = time.monotonic() - self.last_tick > self.limit
        if expired:
            self.stalled = True
            self.write('stalled_terminating')
            self.terminate()
            self.write('stalled')
        else:
            self.write('progressing')
        return expired

    def monitor(self):
        while not self.stop.wait(5):
            if self.proc.poll() is not None:
                return
            if self.check():
                return

    def finish(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join()
        self.write('stalled' if self.stalled else 'process_finished')
