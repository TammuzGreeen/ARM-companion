import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from backend_overlay.apply import NATIVE_RANKER
from backend_overlay.video_selection import select, settings, SelectionPending

namespace = {}
exec(NATIVE_RANKER, namespace)
rank = namespace['rank_mainfeature_tracks']


def track(number, length, chapters=1, size=100, **kw):
    return NS(track_number=str(number), track_id=100+number, length=length,
              chapters=chapters, filesize=size, **kw)


def job(kind='movie'):
    return NS(video_type=kind, multi_title=False, config=NS(MINLENGTH=0, MAXLENGTH=99998))


@pytest.mark.parametrize('kind', ['movie', 'unknown'])
def test_retained_73_title_scan(kind):
    path=Path(__file__).parent/'fixtures/bluray-73-scan.json'
    if not path.exists():pytest.skip('Optional retained private scan is not distributed')
    fixture=json.loads(path.read_text())
    tracks=[track(r[0],r[2],r[3],r[4]) for r in fixture['rows']]
    for t,r in zip(tracks,fixture['rows']):
        t.track_id=r[1]
    chosen, report=select(job(kind), tracks, rank)
    assert len(report['titles']) == 73
    assert sum(t.length < 60 for t in tracks) == 61
    assert [t.track_number for t in chosen] == ['0']
    assert report['selected_ids'] == [0]
    assert report['titles'][0]['arm_track_id'] == 51
    assert report['titles'][0]['duration_seconds'] == 7342
    assert all(not t['selected'] for t in report['titles'] if t['duration_seconds']<600)
    assert all(t['reason'] for t in report['titles'])
    # Relabeling every ID proves the policy doesn't depend on this film/title 0.
    for t in tracks:
        t.track_number=str(int(t.track_number)+400)
    assert select(job(kind),tracks,rank)[1]['selected_ids']==[400]


def test_native_rank_is_not_longest_title():
    tracks=[track(5,3000,8),track(9,3200,7),track(1,2900,8)]
    assert [t.track_number for t in rank(tracks)]==['5','1','9']
    assert [t.track_number for t in rank([track(4,900,2,400),track(3,900,2,400),track(2,900,2,100)])]==['3','4','2']


def test_minimum_and_disabled_flags(monkeypatch):
    monkeypatch.setenv('MOVIE_MIN_LENGTH_SECONDS','1200')
    tracks=[track(0,1199,20),track(1,1500,5),track(2,5000,40,enabled=False)]
    assert select(job(),tracks,rank)[1]['selected_ids']==[1]


def test_ambiguous_unknown_waits_then_tv_uses_no_movie_minimum(monkeypatch):
    monkeypatch.setenv('MOVIE_MIN_LENGTH_SECONDS','9999')
    tracks=[track(0,1400),track(2,1405)]
    with pytest.raises(SelectionPending) as exc:
        select(job('unknown'),tracks,rank)
    assert len(exc.value.report['titles'])==2
    assert select(job('unknown'),tracks,rank,'tv',True)[1]['selected_ids']==[0,2]
    assert select(job('unknown'),tracks,rank,None,True)[0]==[]


def test_ambiguous_movie_does_not_choose_cut():
    tracks=[track(0,7000,10),track(1,7100,10)]
    assert select(job(),tracks,rank)[1]['status']=='ambiguous'


def test_explicit_all_titles_still_filters_and_is_not_unknown_default(monkeypatch):
    monkeypatch.setenv('MOVIE_EXTRACTION_MODE','all_titles')
    tracks=[track(0,1200),track(1,1100),track(2,90)]
    assert select(job(),tracks,rank)[1]['selected_ids']==[0,1]
    with pytest.raises(SelectionPending):
        select(job('unknown'),tracks,rank)


@pytest.mark.parametrize('key,value', [('MOVIE_EXTRACTION_MODE','bad'),('MOVIE_MIN_LENGTH_SECONDS','-1'),('MOVIE_EXTRAS_POLICY','all')])
def test_invalid_configuration(key,value,monkeypatch):
    monkeypatch.setenv(key,value)
    with pytest.raises(ValueError):
        settings()
