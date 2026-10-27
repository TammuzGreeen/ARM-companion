import json
import os
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from backend_overlay.watchdog import Watchdog


def guard(tmp_path, monkeypatch):
    monkeypatch.setattr('backend_overlay.watchdog.time.monotonic',lambda:100.)
    proc=NS(pid=987654, poll=Mock(return_value=None),wait=Mock(return_value=0))
    env={'RH_RUNTIME_PATH':str(tmp_path/'progress.json'),'RH_OUTPUT_DIRECTORY':str(tmp_path),'VIDEO_STALL_SECONDS':'60'}
    return Watchdog(proc,env)


def test_repeated_same_progress_is_not_progress(tmp_path, monkeypatch):
    g=guard(tmp_path,monkeypatch)
    g.observe('PRGV:1,2,100')
    g.observe('PRGC:0,0,"operation"')
    monkeypatch.setattr('backend_overlay.watchdog.time.monotonic',lambda:161.)
    g.observe('PRGV:1,2,100')
    g.observe('PRGC:0,0,"operation"')
    kill=Mock();monkeypatch.setattr(os,'killpg',kill)
    assert g.check()
    kill.assert_called_once_with(g.proc.pid,signal.SIGTERM)
    assert json.loads(g.path.read_text())['state']=='stalled'


def test_changed_progress_and_output_growth_protect_long_rip(tmp_path, monkeypatch):
    g=guard(tmp_path,monkeypatch)
    kill=Mock();monkeypatch.setattr(os,'killpg',kill)
    for tick in (150.,200.,250.,300.):
        monkeypatch.setattr('backend_overlay.watchdog.time.monotonic',lambda:tick)
        g.observe(f'PRGV:{tick},1000,1000')
        assert not g.check()
    monkeypatch.setattr('backend_overlay.watchdog.time.monotonic',lambda:359.)
    (tmp_path/'film.mkv').write_bytes(b'growing')
    assert not g.check()
    kill.assert_not_called()


def test_stall_escalation_is_bounded(tmp_path,monkeypatch):
    g=guard(tmp_path,monkeypatch)
    g.proc.wait.side_effect=[subprocess.TimeoutExpired('owned',15),0]
    kill=Mock();monkeypatch.setattr(os,'killpg',kill)
    g.terminate()
    assert [c.args for c in kill.call_args_list]==[(g.proc.pid,signal.SIGTERM),(g.proc.pid,signal.SIGKILL)]
    assert all(c.kwargs['timeout']==15 for c in g.proc.wait.call_args_list)


def test_finished_process_is_never_signalled(tmp_path,monkeypatch):
    g=guard(tmp_path,monkeypatch)
    g.proc.poll.return_value=0
    kill=Mock();monkeypatch.setattr(os,'killpg',kill)
    g.terminate()
    kill.assert_not_called()


def test_cancellation_reaps_real_owned_child(tmp_path):
    proc=subprocess.Popen(['sleep','60'],start_new_session=True)
    g=Watchdog(proc,{'RH_RUNTIME_PATH':str(tmp_path/'progress.json'),'RH_OUTPUT_DIRECTORY':str(tmp_path)})
    try:
        g.start()
        g.terminate()
        g.finish()
        assert proc.poll() is not None
        assert not g.thread.is_alive()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid,signal.SIGKILL)
            proc.wait(timeout=5)


def test_diagnostic_write_failure_does_not_disable_stall_recovery(tmp_path,monkeypatch):
    g=guard(tmp_path,monkeypatch)
    g.path=tmp_path/'missing'/'progress.json'
    monkeypatch.setattr('backend_overlay.watchdog.time.monotonic',lambda:161.)
    kill=Mock();monkeypatch.setattr(os,'killpg',kill)
    assert g.check()
    kill.assert_called_once()
