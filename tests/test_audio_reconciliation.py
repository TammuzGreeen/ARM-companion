"""Final work identity versus historical observations, independent of drive access."""
import copy
import json
from pathlib import Path

import pytest

from recognition_helper.decisions import map_outputs
from tests.test_audio_metadata import audio_cycle, candidate, decide


def work_cycle(title='The Winter Stories', names=('Ada Example', 'Ada Example'), music=False):
    c=audio_cycle()
    kind='music' if music else 'audiobook'
    c['locals']=[{'model':str(i),'findings':{'media_type':kind,'title':v,
                    'author':names[i] if not music else None,'artist':names[i]}}
                 for i,v in enumerate((f'The {names[0]} Winter Stories',title))]
    candidate(c)['facts'].update(album=title,artist=names[0],disc_total=3)
    return c


def add_edition(c,title):
    other=copy.deepcopy(candidate(c));other['release_id']='other-edition';other['facts']['album']=title
    c['sources'][0]['backend_metadata']['prescan']['musicbrainz']['candidates'].append(other)
    return other


@pytest.mark.parametrize('music',[False,True])
def test_person_decorated_work_title_resolves_without_changing_raw_findings(music):
    c=work_cycle(music=music);raw=copy.deepcopy(c['locals'])
    d=decide(c)
    assert not d['conflicts']
    assert d['identity']['title']=='The Winter Stories'
    assert all(s['proposed'] for s in map_outputs(d,c['sources']))
    assert c['locals']==raw
    assert d['audio_metadata']['resolved_conflicts']['title']['original']


def test_single_corroborated_full_title_can_resolve_shortened_visual_titles():
    c=work_cycle();candidate(c)['facts']['album']='The Winter Stories: Complete Readings'
    d=decide(c)
    assert d['identity']['book']==d['identity']['title']=='The Winter Stories: Complete Readings'
    assert not d['conflicts']


def test_shared_work_omits_uncertain_edition_subtitle():
    c=work_cycle();add_edition(c,'The Winter Stories: Complete Readings')
    d=decide(c)
    assert d['identity']['album']==d['identity']['book']==d['identity']['title']=='The Winter Stories'
    assert d['audio_metadata']['edition_uncertainties']
    assert d['audio_metadata']['work_identity']['release_id_selected'] is None
    assert all(s['proposed'] for s in map_outputs(d,c['sources']))


def test_two_different_explicit_subtitles_keep_common_work_only():
    c=work_cycle();candidate(c)['facts']['album']='The Winter Stories: Collector Edition'
    add_edition(c,'The Winter Stories: Anniversary Edition')
    d=decide(c)
    assert d['identity']['book']=='The Winter Stories'
    assert not d['conflicts']


def test_real_different_work_title_is_not_erased():
    c=work_cycle();c['locals'][0]['findings']['title']='The Summer Adventures'
    d=decide(c)
    assert 'title' in d['conflicts']
    assert not any(s['proposed'] for s in map_outputs(d,c['sources']))


@pytest.mark.parametrize('visual',['The Winter Stories: Volume One','The Winter Stories: Summer Adventures'])
def test_material_visual_suffix_is_not_discarded(visual):
    c=work_cycle();c['locals'][0]['findings']['title']=visual
    assert 'title' in decide(c)['conflicts']


def test_shared_generic_prefix_does_not_hide_different_volumes():
    c=work_cycle();candidate(c)['facts']['album']='The Winter Stories Volume One'
    add_edition(c,'The Winter Stories Volume Two')
    assert 'album' in decide(c)['conflicts']


@pytest.mark.parametrize('first,second',[
    ('The Winter Stories: Volume One','The Winter Stories: Volume Two'),
    ('The Winter Stories: Part One','The Winter Stories: Part Two'),
    ('The Winter Stories: Summer Adventures','The Winter Stories: Mountain Tales'),
    ('The Winter Stories','The Winter Stories: Volume One'),
    ('The Winter Stories: Deluxe Edition Part One','The Winter Stories: Deluxe Edition Part Two')])
def test_colon_does_not_turn_material_work_difference_into_optional_edition(first,second):
    c=work_cycle();candidate(c)['facts']['album']=first
    add_edition(c,second)
    d=decide(c)
    assert 'album' in d['conflicts']
    assert not any(s['proposed'] for s in map_outputs(d,c['sources']))


def test_different_source_identity_in_same_toc_remains_material():
    c=work_cycle();other=add_edition(c,'The Winter Stories: Alternate Cut')
    other['tracks'][1]['title']='A Different Chapter'
    d=decide(c)
    assert 'audio_mapping' in d['conflicts']
    assert not any(s['proposed'] for s in map_outputs(d,c['sources']))


def test_no_role_transplant_or_unrecognized_person_removal():
    c=work_cycle(names=('Unknown','Unknown'))
    c['locals'][0]['findings']['title']='The Ada Example Winter Stories'
    d=decide(c)
    assert 'title' in d['conflicts']
    assert not d['identity'].get('author')


def retained_job12_cycle():
    path=Path(__file__).parent/'fixtures/audio-job12.json'
    if not path.exists():
        import pytest
        pytest.skip('Optional retained private evidence is not distributed')
    f=json.loads(path.read_text())
    c=audio_cycle()
    c['locals']=f['locals']
    evidence=c['sources'][0]['backend_metadata']['prescan']
    evidence.update(tracks=f['toc'],disc_id=f['disc_id'])
    evidence['musicbrainz'].update(lookup_disc_id=f['disc_id'],candidates=f['candidates'])
    c['sources']=[{'source_id':t['number'],'arm_track_id':100+t['number'],
        'state':'complete','path':f'/detention/DETAINED_{t["number"]}.flac',
        'backend_metadata':{'source_number_kind':'CD TOC track number','physical_toc_track':t,
                           'prescan':evidence,'cycle_id':c['id'],'job_guid':'guid-8'}} for t in f['toc']]
    return c


def test_retained_job12_evidence_resolves_common_title_and_keeps_22_tracks():
    c=retained_job12_cycle();original=copy.deepcopy(c['locals']);d=decide(c)
    assert d['identity']['book']==d['identity']['album']==d['identity']['title']=='Die Känguru-Chroniken'
    assert d['identity']['author']=='Marc-Uwe Kling'
    assert d['identity']['disc_total']==4
    assert d['identity']['tracks']==list(range(1,23))
    assert len(d['identity']['track_titles'])==22
    assert not d['conflicts']
    assert c['locals']==original
    assert d['audio_metadata']['original_conflicts']['title']
    assert d['audio_metadata']['edition_uncertainties']
    assert all(s['proposed'] for s in map_outputs(d,list(reversed(c['sources']))))


def test_reconciled_work_auto_releases_and_tags_without_fallback(tmp_path):
    from unittest.mock import Mock
    from mutagen.flac import FLAC
    from recognition_helper.config import Settings
    from recognition_helper.engine import Engine
    from recognition_helper.audio_tags import audio_frames_hash
    from tests.audio_fixture import make_flac
    e=Engine(Settings(state=tmp_path/'state',media=tmp_path/'media',released=tmp_path/'released'))
    try:
        c=e.store.create(state='test')
        sample=work_cycle();sample['id']=c['id']
        for s in sample['sources']:
            b=s['backend_metadata'];b['cycle_id']=c['id']
            path=tmp_path/'media'/'detention'/f'DETAINED_{s["source_id"]}.flac'
            path.parent.mkdir(exist_ok=True,parents=True)
            make_flac(path);s.update(path=str(path),state='complete')
        c.update(sample,local_done=True,backend_done=True)
        raw=copy.deepcopy(c['locals'])
        before=[audio_frames_hash(s['path']) for s in c['sources']]
        e.pool.submit=Mock()
        e.decide(c)
        assert not c.get('fallback_attempted')
        e.pool.submit.assert_not_called()
        e.release(c)
        assert c['state']=='released'
        assert c['locals']==raw
        for number,(s,digest) in enumerate(zip(c['sources'],before),1):
            tags=FLAC(s['path'])
            assert tags['ALBUM']==['The Winter Stories']
            assert tags['AUTHOR']==['Ada Example']
            assert tags['TRACKNUMBER']==[str(number)]
            assert tags['TRACKTOTAL']==['5']
            assert tags['DISCTOTAL']==['3']
            assert tags['TITLE']==[f'Title {number}']
            assert audio_frames_hash(s['path'])==digest
    finally:
        e.pool.shutdown(wait=True)
