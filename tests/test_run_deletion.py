import copy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import httpx
import pytest

from recognition_helper.config import Settings
from recognition_helper.engine import Engine
from recognition_helper.run_deletion import preview, execute


@pytest.fixture
def run(tmp_path):
    e=Engine(Settings(state=tmp_path/'state',media=tmp_path/'media',released=tmp_path/'released'))
    c=e.store.create(state='completed_detained',local_done=True)
    c.update(backend_done=True,job_id=42,job_guid='11111111-1111-4111-8111-111111111111',recognition_done=True)
    root=e.s.detention/f"job-42-{c['job_guid']}"
    root.mkdir(parents=True)
    (root/'.recognition-helper.json').write_text('{}')
    for n in range(2):
        p=root/f'source-{n:05d}'/f'DETAINED_source{n}.mkv';p.parent.mkdir();p.write_bytes(b'synthetic')
        c['sources'].append({'source_id':n,'path':str(p),'state':'complete','reason':'unidentified'})
    removed=[False]
    queue={'drives':[{'mount':e.s.drive,'state':'ready','job_id':None}],'optical_processes':[]}
    def check(*args):
        if removed[0]:
            raise httpx.HTTPStatusError('missing',request=httpx.Request('GET','http://synthetic/jobs/42'),response=httpx.Response(404))
        return {'root':str(Path(e.s.arm_media)/'detention'/root.name),'job_guid':c['job_guid'],'cycle_id':c['id'],'status':'success'}
    def delete(*args):
        removed[0]=True
        return {'arm':{'success':True},'queue':queue}
    e.arm=NS(deletion_check=Mock(side_effect=check),delete_finished=Mock(side_effect=delete),
             jobs=Mock(side_effect=lambda:[] if removed[0] else [{'job_id':42,'guid':c['job_guid']}]),queue_status=Mock(return_value=queue))
    yield e,c,root
    e.pool.shutdown(wait=True)


def perform(e,c):
    p=preview(e,c)
    assert p['safe'],p
    return execute(e,c,p['token'],c['job_id'])


@pytest.mark.parametrize('state',['completed_detained','completed_with_technical_failures','technical_failure_no_outputs'])
def test_completed_cleanup_exact_files_audit_ready(run,state):
    e,c,root=run;c['state']=state
    unrelated=e.s.detention/'unrelated.mkv';unrelated.write_bytes(b'keep')
    p=preview(e,c)
    assert p['file_count']==3 and p['total_size_bytes']==20
    assert root.exists() and not e.arm.delete_finished.called
    result=execute(e,c,p['token'],42)
    assert result['status']=='deleted' and result['deleted_size_bytes']==20
    assert not root.exists() and unrelated.read_bytes()==b'keep'
    assert result['arm_removed'] and result['queue_result']['drives'][0]['state']=='ready'
    assert c['state']=='deleted' and c['sources'][0]['state']=='complete'
    assert execute(e,c,p['token'],42)==result
    assert e.arm.delete_finished.call_count==1
    assert e.may_calibrate()==(True,None)


@pytest.mark.parametrize('active',['backend','source','local','worker','released'])
def test_active_or_released_runs_are_protected(run,active):
    e,c,root=run
    if active=='backend':c['backend_done']=False
    elif active=='source':c['sources'][0]['state']='writing'
    elif active=='local':c['local_done']=False
    elif active=='worker':e.releasing.add(c['id'])
    else:c['sources'][0]['state']='released'
    assert not preview(e,c)['safe'] and root.exists()
    e.arm.delete_finished.assert_not_called()


@pytest.mark.parametrize('mode',['missing','wrong_token','wrong_job','changed_file','new_file'])
def test_confirmation_and_stale_preview_rejected(run,mode):
    e,c,root=run;p=preview(e,c)
    token=p['token'];job=42
    if mode=='missing':token=''
    if mode=='wrong_token':token='other'
    if mode=='wrong_job':job=43
    if mode=='changed_file':Path(c['sources'][0]['path']).write_bytes(b'new bytes')
    if mode=='new_file':(root/'unexpected.mkv').write_bytes(b'new')
    with pytest.raises(ValueError):execute(e,c,token,job)
    assert root.exists() and not e.arm.delete_finished.called


@pytest.mark.parametrize('mode',['symlink','hardlink','shared','root_symlink'])
def test_shared_and_symlink_paths_protected(run,tmp_path,mode):
    e,c,root=run;p=Path(c['sources'][0]['path']);outside=tmp_path/'outside';outside.write_bytes(b'keep')
    if mode=='symlink':p.unlink();p.symlink_to(outside)
    if mode=='hardlink':outside.unlink();outside.hardlink_to(p)
    if mode=='shared':
        other=e.store.create(state='completed_detained');other['sources']=[copy.deepcopy(c['sources'][0])]
    if mode=='root_symlink':
        moved=root.with_name('moved');root.rename(moved);root.symlink_to(moved,target_is_directory=True)
    assert not preview(e,c)['safe']
    assert outside.exists() and not e.arm.delete_finished.called


def test_filesystem_partial_failure_explicit_retry(run,monkeypatch):
    from recognition_helper import run_deletion as d
    e,c,root=run
    original=d.os.unlink
    def fail(name,**kw):
        if 'source1' in str(name):raise PermissionError('synthetic permission denied')
        return original(name,**kw)
    with monkeypatch.context() as m:
        m.setattr(d.os,'unlink',fail)
        result=perform(e,c)
    assert result['status']=='partial' and result['attempts'][-1]['errors']
    assert (root/'.recognition-helper.json').exists() and not e.arm.delete_finished.called
    assert perform(e,c)['status']=='deleted'


def test_arm_failure_keeps_manifest_and_requires_new_confirmation(run):
    e,c,root=run
    original=e.arm.delete_finished.side_effect
    e.arm.delete_finished.side_effect=ValueError('synthetic backend refusal')
    result=perform(e,c)
    assert result['status']=='partial' and (root/'.recognition-helper.json').exists()
    with pytest.raises(ValueError):execute(e,c,'old',42)
    e.arm.delete_finished.side_effect=original
    assert perform(e,c)['status']=='deleted'


def test_uncertain_arm_response_reconciles_without_deleting_reused_id(run):
    e,c,root=run
    original=e.arm.delete_finished.side_effect
    def timeout(*args):
        original(*args)
        raise httpx.ReadTimeout('synthetic lost reply')
    e.arm.delete_finished.side_effect=timeout
    assert perform(e,c)['status']=='partial'
    assert perform(e,c)['status']=='deleted'
    assert e.arm.delete_finished.call_count==1


def test_queue_blocked_reports_partial_not_success(run):
    e,c,root=run
    e.arm.queue_status.return_value={'drives':[{'mount':e.s.drive,'state':'busy','job_id':99}],'optical_processes':[{'pid':123}]}
    result=perform(e,c)
    assert result['status']=='partial' and result['arm_removed']
    assert 'not ready' in result['attempts'][-1]['errors'][0]


def test_backend_root_or_guid_mismatch_prohibited(run):
    e,c,root=run
    e.arm.deletion_check.side_effect=None
    e.arm.deletion_check.return_value={'root':'/arbitrary/../outside','job_guid':'different','cycle_id':c['id']}
    assert not preview(e,c)['safe']


def test_deletion_api_authenticated_and_confirmation_required(run,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from recognition_helper import main
    e,c,root=run
    password=tmp_path/'test-password';password.write_text('synthetic-delete-only')
    monkeypatch.setattr(main,'settings',NS(user='operator',password_file=str(password)))
    monkeypatch.setattr(main,'engine',e)
    client=TestClient(main.app)
    url=f"/api/cycles/{c['id']}"
    try:
        assert client.post(url+'/deletion-preview').status_code==401
        assert client.post(url+'/delete-run',json={'token':'x','confirmed_job_id':42}).status_code==401
        p=client.post(url+'/deletion-preview',auth=('operator','synthetic-delete-only')).json()
        assert p['safe'] and root.exists()
        assert client.post(url+'/delete-run',auth=('operator','synthetic-delete-only'),json={'token':p['token'],'confirmed_job_id':43}).status_code==409
        result=client.post(url+'/delete-run',auth=('operator','synthetic-delete-only'),json={'token':p['token'],'confirmed_job_id':42})
        assert result.status_code==200 and result.json()['status']=='deleted'
    finally:client.close()


@pytest.mark.parametrize('failed',[False,True])
def test_audio_generated_metadata_and_registered_scratch_cleanup(run,failed):
    e,c,root=run
    source=c['sources'][0]
    old=Path(source['path']);audio=old.with_suffix('.flac');old.rename(audio)
    source.update(path=str(audio),expected_path=str(audio))
    (audio.parent/'.abcde.conf').write_text('synthetic config')
    work=audio.parent/'work'/'abcde.0123abcd';work.mkdir(parents=True)
    for name in ('cddbdiscid','mbdiscid','status'):(work/name).write_text('synthetic state')
    if failed:
        scratch=work/'DETAINED_track01.wav';scratch.write_bytes(b'partial waveform')
        source.update(state='failed',retained_files=[{'path':str(scratch)}])
    assert perform(e,c)['status']=='deleted' and not root.exists()
