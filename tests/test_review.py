import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from recognition_helper.config import Settings
from recognition_helper.engine import Engine
from recognition_helper.review import source_view, suggestions
from recognition_helper.store import Store


def populate(e):
    c=e.store.create(state='completed_detained',local_done=True)
    c.update(job_id=99,job_guid='synthetic-guid',backend_done=True,recognition_done=True,
             backend_status='success',backend_detail={'job':{'label':'CEDAR_D3'}},
             decision={'identity':{'media_type':'tv','series':'Cedar Valley','season':1,'episodes':[9,10,11,12]},
                       'conflicts':{'disc':[['a',1],['b',2]]}},
             locals=[{'model':m,'findings':{'media_type':'tv','series':'Cedar Valley','season':1,
                      'episodes':[9,10,11,12],'disc':disc,'visible_text':'CEDAR L3 Director Cut'}} for m,disc in [('a',1),('b',2)]])
    for n in range(11):
        p=e.s.detention/f'DETAINED_source{n}.mkv';p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'closed synthetic output')
        c['sources'].append({'source_id':n,'arm_track_id':700+n,'path':str(p),'state':'complete',
            'backend_metadata':{'scan_duration':3400 if n in (0,3,4,6) else 10,
                                'duration_validation':{'actual_seconds':3414.295 if n in (0,3,4,6) else 4.671}}})
    e.references=[{'id':'cedar/s1d3','facts':{'media_type':'tv','series':'Cedar Valley','season':1,'disc':3,
                   'episodes':[9,10,11,12],'edition':'collection'},'episode_names':{'9':'Arrival','10':'Wunder','11':'Rückkehr','12':'Departure'},
                   'edition_name':'Collection Director Cut','descriptive_disc':{'printed_identifiers':['CEDAR L3']},
                   'provenance':['Synthetic example only']},
                  {'id':'cedar/s1d1','facts':{'media_type':'tv','series':'Cedar Valley','season':1,'disc':1,'episodes':[1,2,3,4]}},
                  {'id':'unrelated','facts':{'media_type':'tv','series':'Unrelated Show','season':1,'episodes':[9,10,11,12]}}]
    e.store.save(c)
    return c


@pytest.fixture
def review_engine(tmp_path):
    e=Engine(Settings(state=tmp_path/'state',media=tmp_path/'media',released=tmp_path/'released'))
    c=populate(e)
    yield e,c
    e.pool.shutdown(wait=True)


def test_duration_actual_precedes_estimate_and_missing_is_not_zero():
    s={'source_id':0,'path':'DETAINED.mkv','backend_metadata':{'scan_duration':60,'duration_validation':{'actual_seconds':91.25}}}
    assert source_view(s,{'media_type':'tv'},900)['duration_seconds']==91.25
    assert source_view(s,{'media_type':'tv'},900)['duration_basis']=='actual extracted'
    s['backend_metadata'].pop('duration_validation')
    assert source_view(s,{'media_type':'tv'},900)['duration_basis']=='scan estimate'
    s['backend_metadata']['scan_duration']=None
    assert source_view(s,{},900)['duration_seconds'] is None


def test_suggestions_relevant_evidence_and_stable_candidate_order(review_engine):
    e,c=review_engine;r=e.review(c['id'])
    assert [s['source_id'] for s in r['sources'][:4]]==[0,3,4,6]
    assert sum(s['short_clip'] for s in r['sources'])==7
    assert r['candidates'][0]['id']=='cedar/s1d3'
    assert r['candidates'][0]['exact_identifier']
    assert 'unrelated' not in [r['id'] for r in r['candidates']]


def test_confirmation_persists_proposals_without_publication_or_inference(review_engine):
    e,c=review_engine;raw=copy.deepcopy(c['locals']);old=copy.deepcopy(c['decision']);e.pool.submit=Mock()
    result=e.review_masterlist(c['id'],'cedar/s1d3','confirm')
    assert result['identity']['disc']==3
    assert list(result['draft']['assignments'])==['0','3','4','6']
    assert [a['number'] for a in result['draft']['assignments'].values()]==[9,10,11,12]
    assert result['preview'][0]['proposed'].endswith('S01E09 - Arrival.mkv')
    assert c['locals']==raw and c['decision']==old and all(s['state']=='complete' for s in c['sources'])
    e.pool.submit.assert_not_called()
    reloaded=Store(e.store.root).cycles[c['id']]
    assert reloaded['review_draft']['identity']['disc']==3
    assert reloaded['masterlist_review']['confirmed_id']=='cedar/s1d3'


def test_rejection_is_cycle_scoped_and_can_be_reconsidered(review_engine):
    e,c=review_engine;e.review_masterlist(c['id'],'cedar/s1d3','reject')
    assert e.review(c['id'])['candidates'][0]['rejected']
    assert Store(e.store.root).cycles[c['id']]['masterlist_review']['rejected']==['cedar/s1d3']
    other=copy.deepcopy(c);other.pop('masterlist_review');assert not suggestions(other,e.references)[0]['rejected']
    e.review_masterlist(c['id'],'cedar/s1d3','reconsider')
    assert not e.review(c['id'])['candidates'][0]['rejected']


def test_user_accepts_edited_proposal_partial_release_and_audit(review_engine):
    e,c=review_engine;e.review_masterlist(c['id'],'cedar/s1d3','confirm')
    payload=copy.deepcopy(c['review_draft']);payload['assignments']['0']['number']=10
    payload['assignments']['3']['number']=9
    result=e.correct(c['id'],payload)
    assert result['state']=='partially_released'
    assert sum(s['state']=='released' for s in c['sources'])==4
    assert c['sources'][0]['path'].endswith('S01E10 - Wunder.mkv')
    assert c['sources'][1]['state']=='complete'
    assert c['corrections'][-1]['reviewed_proposal']['assignments']['0']['number']==9
    assert c['corrections'][-1]['assignments']['0']['number']==10


def test_collision_and_unfinished_preview_are_honest(review_engine):
    e,c=review_engine;result=e.review_masterlist(c['id'],'cedar/s1d3','confirm')
    target=Path(result['preview'][0]['destination']);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'previous output')
    c['sources'][3]['state']='writing'
    preview=e.preview_correction(c['id'],c['review_draft'])
    assert preview[0]['collision'] and not preview[0]['publication_ready']
    assert not preview[3].get('publication_ready',False)
    e.correct(c['id'],c['review_draft'])
    assert target.read_bytes()==b'previous output'
    assert c['sources'][3]['state']=='writing'


def test_mismatch_does_not_force_sources_into_positions(review_engine):
    e,c=review_engine;e.references[0]['facts']['episodes']=[9,10,11]
    r=e.review_masterlist(c['id'],'cedar/s1d3','confirm')
    assert r['draft']['warnings'] and not r['draft']['assignments']


def test_masterlist_names_cannot_traverse_release_root(review_engine):
    e,c=review_engine;e.references[0]['facts']['series']='../../Cedar <script>'
    e.references[0]['episode_names']['9']='../../unsafe'
    r=e.review_masterlist(c['id'],'cedar/s1d3','confirm')
    for s in r['preview']:
        if s.get('destination'):assert Path(s['destination']).resolve().is_relative_to(e.s.released.resolve())
