"""Managed pre-extraction filtering, without a drive or real extraction."""
from pathlib import Path
from types import SimpleNamespace as NS

from tests.test_backend import fake_backend, helper


def test_tv_prepare_and_extract_only_filtered_sources(fake_backend, monkeypatch):
    monkeypatch.setenv('TV_MIN_LENGTH_SECONDS','900')
    job=fake_backend;job.video_type='unknown'
    job.tracks=[NS(track_number=str(n),track_id=700+n,length=1500 if n in (0,4,8) else 12,
        chapters=5,filesize=5000,filename='scan.mkv',source='MakeMKV',ripped=False,status=None,error=None)
        for n in range(10)]
    plan=helper.prepare_and_start(job,'tv-ordinary','tv',True)
    assert [s['source_id'] for s in plan['sources']]==[0,4,8]
    assert len(plan['video_selection']['titles'])==10
    assert all(t.process is False for t in job.tracks if int(t.track_number) not in (0,4,8))
    calls=[]
    def home(directory,filename):
        p=directory/'.makemkv-home';p.mkdir();return p
    def run(args,types,env=None):
        source=int(args[-2]);calls.append(source)
        (Path(args[-1])/f'DETAINED_job7_source{source}.mkv').write_bytes(b'software-only')
        return iter(())
    def probe(path,source,**kw):source.update(path=str(path),state='complete',finalized_at=1,size_bytes=path.stat().st_size)
    monkeypatch.setattr(helper,'settings_home',home);monkeypatch.setattr(helper,'probe',probe)
    helper.rip_video(job,run,0,'/unused')
    assert calls==[0,4,8]


def test_verified_short_episode_assignment_is_reserved_and_retained(fake_backend,monkeypatch):
    monkeypatch.setenv('TV_MIN_LENGTH_SECONDS','900')
    j=fake_backend;j.label='SYNTHETIC_SHORTS'
    ctx={'episodes':[5,6],'reference':{'id':'short-season','facts':{'episodes':[5,6]},
        'disc_observation':{'label':j.label,'source_signature':[{'source_id':int(t.track_number),'duration_seconds':t.length} for t in j.tracks]},
        'source_observations':[{'source_id':0,'assignment':{'kind':'tv','number':6}},
                               {'source_id':2,'assignment':{'kind':'tv','number':5}}]}}
    plan=helper.prepare_and_start(j,'short-tv','tv',True,ctx)
    assert [s['source_id'] for s in plan['sources']]==[0,2]
    assert [s['identified_assignment']['number'] for s in plan['sources']]==[6,5]
    assert all('full scan signature' in s['identified_assignment']['evidence'] for s in plan['sources'])


def test_mixed_tv_episodes_do_not_start_before_short_exception_can_be_considered(fake_backend,monkeypatch):
    import pytest
    monkeypatch.setenv('TV_MIN_LENGTH_SECONDS','900')
    j=fake_backend;j.label='MIXED_TV';j.tracks[0].length=1800;j.tracks[1].length=450
    with pytest.raises(helper.SelectionPending):helper.prepare_and_start(j,'mixed-cycle')
    assert not j.manual_start and not helper.managed(j)
    ctx={'episodes':[7,8],'reference':{'id':'mixed','facts':{'episodes':[7,8]},
        'disc_observation':{'label':j.label,'source_signature':[{'source_id':int(t.track_number),'duration_seconds':t.length} for t in j.tracks]},
        'source_observations':[{'source_id':0,'assignment':{'kind':'tv','number':7}},
                               {'source_id':2,'assignment':{'kind':'tv','number':8}}]}}
    plan=helper.prepare_and_start(j,'mixed-cycle','tv',True,ctx)
    assert j.manual_start and [s['source_id'] for s in plan['sources']]==[0,2]


def test_identified_short_extra_is_not_reserved_as_an_episode(fake_backend,monkeypatch):
    monkeypatch.setenv('TV_MIN_LENGTH_SECONDS','900')
    j=fake_backend;j.label='VERIFIED_EXTRA';j.tracks[0].length=1800
    ctx={'episodes':[1],'extra_observations':[['Behind the Scenes'],['Behind the Scenes']],
        'reference':{'id':'extra-disc','facts':{'episodes':[1]},
            'disc_observation':{'label':j.label,'source_signature':[{'source_id':int(t.track_number),'duration_seconds':t.length} for t in j.tracks]},
            'source_observations':[{'source_id':0,'assignment':{'kind':'tv','number':1}},
                                   {'source_id':2,'assignment':{'kind':'extra','title':'Behind the Scenes'}}]}}
    plan=helper.prepare_and_start(j,'extra-cycle','tv',True,ctx)
    extra=plan['sources'][1]
    assert extra['source_id']==2 and extra['is_extra'] and extra['extra_identity']=='Behind the Scenes'
    assert 'identified_assignment' not in extra
