from types import SimpleNamespace as NS
from pathlib import Path
import pytest
from backend_overlay import recognition_helper as helper


@pytest.mark.parametrize('condition',['active','guid','pid','optical','other_drive','cycle'])
def test_backend_finalized_guid_process_guards(monkeypatch,condition):
    job=NS(job_id=1,guid='11111111-1111-4111-8111-111111111111',status='success',stop_time='done',pid=None)
    monkeypatch.setattr(helper,'optical_processes',lambda:[])
    monkeypatch.setattr(helper,'queue_state',lambda:{'drives':[{'job_id':None}]})
    monkeypatch.setattr(helper,'managed',lambda j:True)
    monkeypatch.setattr(helper,'load',lambda j:{'cycle_id':'cycle'})
    guid=str(job.guid)
    if condition=='active':job.status='video_ripping'
    if condition=='guid':guid='other'
    if condition=='pid':job.pid=1
    if condition=='optical':monkeypatch.setattr(helper,'optical_processes',lambda:[{'pid':12}])
    if condition=='other_drive':monkeypatch.setattr(helper,'queue_state',lambda:{'drives':[{'job_id':2}]})
    if condition=='cycle':monkeypatch.setattr(helper,'load',lambda j:{'cycle_id':'different'})
    with pytest.raises(ValueError):helper.deletion_check(job,guid,'cycle')
