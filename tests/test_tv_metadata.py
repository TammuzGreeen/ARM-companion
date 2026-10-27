import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from backend_overlay.video_selection import select, SelectionPending
from recognition_helper.decisions import merge, map_outputs, filename
from recognition_helper.config import Settings
from recognition_helper.engine import Engine
from recognition_helper.source_references import apply_reference
from recognition_helper.masterlists import load_references
from tests.test_video_selection import rank, track, job


def observations():
    common={'media_type':'tv','series':'Northern Harbor','season':2,'disc':1,'episodes':[4,3]}
    return [{'model':'a','findings':{**common,'title':'Remastered Edition','disc_total':1}},
            {'model':'b','findings':{**common,'title':'Northern Harbor','disc_total':3}}]


def reference():
    return {'id':'harbor/disc1','facts':{'media_type':'tv','series':'Northern Harbor','season':2,
        'disc':1,'episodes':[3,4],'edition':'anniversary'},'edition_name':'Anniversary Collection, Remastered Edition',
        'episode_names':{'3':'Arrival / Rückkehr','4':'Departure'},'disc_totals':{'release':8}}


def sources():
    return [{'source_id':9,'path':'DETAINED_9.mkv','state':'complete'},
            {'source_id':2,'path':'DETAINED_2.mkv','state':'complete'}]


def test_series_and_edition_reconcile_with_applicable_optional_reference():
    obs=observations();raw=copy.deepcopy(obs)
    d=merge(obs,references=[reference()])
    assert not d['conflicts']
    assert d['identity']['series']=='Northern Harbor'
    assert 'title' not in d['identity']
    assert d['identity']['edition_descriptors']==['Remastered Edition']
    assert d['tv_metadata']['resolved_conflicts']['title']['original']
    assert d['tv_metadata']['original_conflicts']['disc_total']
    assert obs==raw
    result=map_outputs(d,sources())
    assert [(s['source_id'],s['assignment']['number']) for s in result]==[(2,3),(9,4)]
    assert result[0]['proposed']=='tv/Northern Harbor/Season 02/Northern Harbor - S02E03 - Arrival - Rückkehr.mkv'


def test_reference_is_optional_and_episode_titles_are_optional():
    obs=observations()
    for o in obs:o['findings'].pop('title')
    d=merge(obs)
    assert all(s['proposed'] for s in map_outputs(d,sources()))
    assert not d['masterlist'].get('facts')


@pytest.mark.parametrize('change',['other_series','other_disc','different_title'])
def test_genuine_identity_conflicts_still_detain(change):
    obs=observations();ref=reference()
    if change=='other_series':obs[1]['findings']['series']='Southern Harbor'
    elif change=='other_disc':obs[1]['findings']['disc']=2
    else:obs[0]['findings']['title']='A Different Programme'
    d=merge(obs,references=[ref])
    assert d['conflicts']
    assert not any(s['proposed'] for s in map_outputs(d,sources()))


def test_unmatched_edition_description_cannot_resolve_title():
    ref=reference();ref['facts']['disc']=2
    d=merge(observations(),references=[ref])
    assert 'title' in d['conflicts']


def test_current_disc_and_scoped_totals_remain_separate():
    obs=observations()
    obs[0]['findings'].update(disc_total=3,disc_total_scope='season')
    obs[1]['findings'].update(disc_total=8,disc_total_scope='release')
    d=merge(obs,references=[reference()])
    assert d['identity']['disc']==1
    assert d['identity']['disc_totals']=={'season':3,'release':8}
    assert 'disc_total' not in d['identity']
    assert not d['conflicts']


def test_unknown_or_disputed_totals_do_not_invent_release_counts():
    ref=reference();ref.pop('disc_totals')
    d=merge(observations(),references=[ref])
    assert 'disc_totals' not in d['identity'] and 'disc_total' not in d['identity']
    assert d['tv_metadata']['uncertainties']['disc_totals']
    assert not d['conflicts']
    # Collection text/partial reference count is not a structured scoped total.
    ref['edition_name']='Eight Disc Complete Collection, Remastered Edition'
    assert 'disc_totals' not in merge(observations(),references=[ref])['identity']


def test_series_premiere_year_is_distinct_from_generic_copyright_year():
    identity={'media_type':'tv','series':'Northern Harbor','season':1,'year':2020}
    assert str(filename(identity,{'number':1},'.mkv')).startswith('tv/Northern Harbor/')
    identity['series_year']=1994
    assert str(filename(identity,{'number':1},'.mkv')).startswith('tv/Northern Harbor (1994)/Season 01/')


def test_explicitly_inferred_premiere_year_is_not_used():
    obs=observations()
    for o in obs:o['findings'].update(series_year=1994,uncertainties=['premiere year inferred, not printed'])
    d=merge(obs,references=[reference()])
    assert 'series_year' not in d['identity']
    assert d['tv_metadata']['omitted_uncertain_series_years']
    assert all('(1994)' not in s['proposed'] for s in map_outputs(d,sources()))


def test_title_pairs_keep_episode_numbers_when_partial_reference_omits_names(tmp_path):
    (tmp_path/'partial.json').write_text(json.dumps({'series':'A Show','season':1,'discs':[
        {'number':1,'episodes':[{'number':1},{'number':2,'title':'Second'}]}]}))
    ref=load_references(tmp_path)[0]
    assert ref['episode_names']=={'2':'Second'}


def test_episode_count_mismatch_is_not_forced_to_fit():
    d=merge(observations(),references=[reference()])
    assert not any(s['proposed'] for s in map_outputs(d,sources()+[{'source_id':20,'path':'DETAINED_20.mkv'}]))


def test_explicit_scoped_mapping_precedes_default_order():
    src=sources();ref=reference()
    ref.update(disc_observation={'label':'EXACT','source_signature':[
        {'source_id':2,'duration_seconds':None},{'source_id':9,'duration_seconds':None}]},
        source_observations=[{'source_id':2,'assignment':{'kind':'tv','number':4}},
                             {'source_id':9,'assignment':{'kind':'tv','number':3}}])
    scoped,reasons=apply_reference(src,ref,{'label':'EXACT'})
    assert not reasons
    mapped=map_outputs(merge(observations(),references=[ref]),scoped)
    assert [(s['source_id'],s['assignment']['number']) for s in mapped]==[(2,4),(9,3)]
    # Existing selected-source exports remain usable after full scans are retained.
    scoped,reasons=apply_reference(src,ref,{'label':'EXACT'},scan_signature=[
        {'source_id':2,'duration_seconds':None},{'source_id':7,'duration_seconds':10},
        {'source_id':9,'duration_seconds':None}])
    assert scoped[0]['identified_assignment']['number']==3 and not reasons


def test_tv_threshold_defaults_to_900_and_is_inclusive(monkeypatch):
    monkeypatch.delenv('TV_MIN_LENGTH_SECONDS',raising=False)
    tracks=[track(9,899),track(2,900),track(7,901),track(0,0)]
    chosen,report=select(job('tv'),tracks,rank,recognition_finished=True)
    assert sorted(int(t.track_number) for t in chosen)==[2,7]
    assert len(report['titles'])==4 and report['settings']['tv_minimum_seconds']==900
    assert not report['titles'][0]['selected']


def test_tv_threshold_configurable_and_does_not_change_movie_minimum(monkeypatch):
    monkeypatch.setenv('TV_MIN_LENGTH_SECONDS','1800')
    assert select(job('tv'),[track(0,1799),track(1,1800)],rank,recognition_finished=True)[1]['selected_ids']==[1]
    assert select(job('movie'),[track(0,1500)],rank)[1]['selected_ids']==[0]
    with pytest.raises(SelectionPending):select(job('tv'),[track(0,None)],rank)


@pytest.mark.parametrize('value',['-1','bad'])
def test_invalid_tv_config_rejected_in_helper_and_backend(value,monkeypatch):
    monkeypatch.setenv('TV_MIN_LENGTH_SECONDS',value)
    with pytest.raises(ValueError):Settings()
    with pytest.raises(ValueError):select(job('tv'),[track(0,2000)],rank)


def tv_context(tracks):
    return {'episodes':[3,4],'reference':{'id':'shorts','facts':{'episodes':[3,4]},
        'disc_observation':{'label':'EXACT','source_signature':[{'source_id':int(t.track_number),'duration_seconds':t.length} for t in tracks]},
        'source_observations':[{'source_id':0,'assignment':{'kind':'tv','number':4}},
                               {'source_id':2,'assignment':{'kind':'tv','number':3}}]}}


def test_verified_legitimate_short_episode_exception_and_full_scan_scope():
    tracks=[track(0,400),track(2,450)];j=job('tv');j.label='EXACT';ctx=tv_context(tracks)
    _,report=select(j,tracks,rank,'tv',True,ctx)
    assert report['selected_ids']==[0,2]
    assert report['verified_associations']['0']['number']==4
    j.label='OTHER'
    assert select(j,tracks,rank,'tv',True,ctx)[1]['selected_ids']==[]
    j.label='EXACT';tracks[0].length=401
    assert select(j,tracks,rank,'tv',True,ctx)[1]['selected_ids']==[]


def test_short_exception_never_accepts_an_arbitrary_episode_or_extra():
    tracks=[track(0,400),track(2,450)];j=job('tv');j.label='EXACT';ctx=tv_context(tracks)
    ctx['reference']['source_observations'][0]['assignment']['number']=99
    with pytest.raises(ValueError):select(j,tracks,rank,'tv',True,ctx)
    for row in ctx['reference']['source_observations']:row['assignment']={'kind':'extra','title':'Trailer'}
    assert select(j,tracks,rank,'tv',True,ctx)[0]==[]


def test_explicit_short_extra_requires_full_scope_and_both_local_identifications():
    tracks=[track(0,1800),track(2,45)];j=job('tv');j.label='EXACT';ctx=tv_context(tracks)
    ctx['reference']['source_observations'][1]['assignment']={'kind':'extra','title':'Behind the Scenes'}
    ctx['extra_observations']=[['Behind the Scenes'],[]]
    assert select(j,tracks,rank,'tv',True,ctx)[1]['selected_ids']==[0]
    ctx['extra_observations'][1]=['Behind the Scenes']
    report=select(j,tracks,rank,'tv',True,ctx)[1]
    assert report['selected_ids']==[0,2] and report['verified_associations']['2']['kind']=='extra'
    j.label='ANOTHER_DISC'
    assert select(j,tracks,rank,'tv',True,ctx)[1]['selected_ids']==[0]


def test_verified_special_episode_zero_can_be_short():
    tracks=[track(0,400)];j=job('tv');j.label='EXACT'
    ctx={'episodes':[0],'reference':{'id':'special','facts':{'episodes':[0]},
        'disc_observation':{'label':'EXACT','source_signature':[{'source_id':0,'duration_seconds':400}]},
        'source_observations':[{'source_id':0,'assignment':{'kind':'tv','number':0}}]}}
    assert select(j,tracks,rank,'tv',True,ctx)[1]['selected_ids']==[0]


def fixture():
    path=Path(__file__).parent/'fixtures/borgia-job16.json'
    if not path.exists():pytest.skip('Optional retained private evidence is not distributed')
    return json.loads(path.read_text())


def test_retained_job16_generic_preextraction_selection_mapping_and_names():
    f=fixture();tracks=[track(t['source_id'],t['duration_seconds'],t['chapters'],t['size_bytes']) for t in f['titles']]
    technical,technical_report=select(job('unknown'),tracks,rank)
    assert technical_report['selection_category']=='tv'
    assert [int(t.track_number) for t in technical]==[0,3,4,6]
    d=merge(f['locals'],references=[f['reference']])
    selected,report=select(job('unknown'),tracks,rank,d['identity']['media_type'],True)
    assert report['selected_ids']==[0,3,4,6]
    assert len(report['titles'])==11
    assert sum(not t['selected'] and t['duration_seconds']<60 for t in report['titles'])==7
    mapped=map_outputs(d,[{'source_id':int(t.track_number),'path':'DETAINED.mkv'} for t in reversed(selected)])
    assert [(s['source_id'],s['assignment']['number']) for s in mapped]==[(0,1),(3,2),(4,3),(6,4)]
    assert all('ascending backend source_id' in s['assignment']['attribution'] for s in mapped)
    assert mapped[0]['proposed']=='tv/Borgia/Season 01/Borgia - S01E01 - 1492.mkv'
    assert mapped[-1]['proposed'].endswith('Borgia - S01E04 - Die Weisheit des heiligen Geistes.mkv')
    assert not d['conflicts'] and 'disc_total' not in d['identity']


def test_resolved_tv_fields_do_not_request_fallback_and_completed_episodes_release(tmp_path):
    e=Engine(Settings(state=tmp_path/'state',media=tmp_path/'media',released=tmp_path/'released'))
    try:
        c=e.store.create(state='test',local_done=True)
        c['locals']=observations();e.references=[reference()];e.pool.submit=Mock()
        for s in sources():
            p=tmp_path/'media/detention'/s['path'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'software completed-video fixture')
            c['sources'].append({**s,'path':str(p)})
        e.decide(c);e.pool.submit.assert_not_called();assert not c.get('fallback_attempted')
        # Simulate the adapter's awaiting-finalization boundary: not publishable yet.
        for s in c['sources']:s['state']='awaiting_finalization'
        e.release(c);assert all(Path(s['path']).name.startswith('DETAINED_') for s in c['sources'])
        for s in c['sources']:s['state']='complete'
        c['backend_done']=True;e.release(c)
        assert c['state']=='released' and all(Path(s['path']).is_file() for s in c['sources'])
    finally:e.pool.shutdown(wait=True)


def test_old_completed_detained_decision_is_not_retroactively_reconciled(tmp_path):
    e=Engine(Settings(state=tmp_path/'state',media=tmp_path/'media',released=tmp_path/'released'))
    try:
        c=e.store.create(state='completed_detained',local_done=True)
        c.update(recognition_done=True,backend_done=True,locals=observations(),sources=sources(),
                 decision={'identity':{'series':'Northern Harbor'},'conflicts':{'title':[['old','Remastered Edition']]}})
        old=copy.deepcopy(c['decision']);e.decide(c)
        assert c['decision']==old and not any(s.get('proposed') for s in c['sources'])
    finally:e.pool.shutdown(wait=True)
