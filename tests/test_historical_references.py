import csv
import hashlib
import io
import json
import pytest
from pathlib import Path

from recognition_helper.decisions import merge, map_outputs
from recognition_helper.masterlists import load_references
from recognition_helper.source_references import apply_reference

ROOT=Path(__file__).parent/'fixtures/historical'
pytestmark=pytest.mark.skipif(not ROOT.exists(), reason='Optional private historical evidence is not distributed')
REVISION='d3459f8ad562e08a10535322efbcb000384e5316'


def test_actual_tng_reference_enriches_compatible_disc():
    refs=load_references(ROOT)
    findings={'media_type':'tv','series':'Star Trek: The Next Generation','season':2,
              'disc':1,'edition':'German Paramount DVD season parts'}
    decision=merge([{'model':'fixture-vision','findings':findings}],references=refs)
    assert decision['identity']['episodes']==[1,2,3,4]
    assert not decision['conflicts']
    outputs=map_outputs(decision,[{'source_id':n,'path':f'DETAINED_{n}.mkv'} for n in range(4)])
    assert all(s['proposed'] for s in outputs)
    assert [s['assignment']['number'] for s in outputs]==[1,2,3,4]


def test_actual_tv_references_do_not_apply_to_movie():
    refs=load_references(ROOT)
    d=merge([{'model':'fixture','findings':{'media_type':'movie','title':'Star Trek Beyond'}}],references=refs)
    assert d['identity']=={'media_type':'movie','title':'Star Trek Beyond'}


def test_tng_wrong_edition_and_missing_disc_not_arbitrarily_selected():
    refs=load_references(ROOT)
    for facts in ({'edition':'unrelated Blu-ray'},{}):
        d=merge([{'model':'fixture','findings':{'media_type':'tv','series':'Star Trek: The Next Generation','season':2,**facts}}],references=refs)
        assert 'episodes' not in d['identity']


def test_actual_partial_tos_preserves_version_convention_without_ids(tmp_path):
    original=ROOT/'arm-season-queue/examples/masterlist-drafts/tos/arm_vision_masterlist_v2.json'
    data=json.loads(original.read_text())
    data['historical_source_pairs']=list(csv.DictReader(io.StringIO((original.parent/'source_pairs.csv').read_text())))
    data['reference_origin']={'revision':REVISION,'path':str(original.relative_to(ROOT)),
                              'sha256':hashlib.sha256(original.read_bytes()).hexdigest()}
    (tmp_path/'runtime.json').write_text(json.dumps(data))
    refs=load_references(tmp_path)
    assert len(refs)==1
    ref=refs[0]
    assert ref['legacy_approved'] is False
    assert ref['collection_conventions']['multiple_versions_per_episode']
    assert {p['version'] for p in ref['historical_source_pairs']}=={'Original','Remastered'}
    assert ref['reference_origin']['revision']==REVISION
    assert not ref.get('source_observations')
    sources=[{'source_id':0,'path':'DETAINED.mkv','backend_metadata':{'scan_duration':3000}}]
    scoped,reasons=apply_reference(sources,ref,{'label':'other pressing'})
    assert 'identified_assignment' not in scoped[0]
    assert reasons  # version convention is not a portable ID mapping


def test_original_tng_fixture_checksum():
    source=ROOT/'arm-season-queue/examples/masterlists/verified-metadata/tng/tng_season_02.json'
    assert hashlib.sha256(source.read_bytes()).hexdigest()=='f5efaed5f4714cca9043eaa169a826c89f858cc340c6149561a94211cf7820be'
