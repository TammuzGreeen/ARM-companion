from types import SimpleNamespace as NS
import pytest
from backend_overlay.video_selection import select, analyze_titles, SelectionPending


def tracks(lengths, ids=None):
    return [NS(track_number=str(n),track_id=100+n,length=s,chapters=5,filesize=s*100,enabled=True,process=True)
            for n,s in zip(ids or range(len(lengths)),lengths)]


def rank(rows):
    return sorted(rows,key=lambda t:(-t.chapters,-t.length,-t.filesize,int(t.track_number)))


def job(kind='unknown'):
    return NS(video_type=kind,multi_title=False,config=NS(MINLENGTH=0,MAXLENGTH=99998))


def scan(mapping):
    return {'sources':{str(n):{'streams':{'0':{'1':{'code':6201,'value':'Video'}}},'title_attributes':{'16':{'value':f'{n:05d}.mpls'},
                          '26':{'value':','.join(map(str,segments))}}} for n,segments in mapping.items()}}


@pytest.mark.parametrize('seconds,count',[(3300,4),(1500,6),(2700,4)])
def test_clear_unknown_tv_starts_before_vision(seconds,count):
    ts=tracks([seconds+i*10 for i in range(count)]+[4,10])
    selected,report=select(job(),ts,rank)
    assert [int(t.track_number) for t in selected]==list(range(count))
    assert report['selection_category']=='tv' and report['technical_analysis']['strong_tv_structure']


def test_synthetic_retained_job18_shape_segment_union_not_longest():
    # Synthetic identifiers/metadata, preserving the regression's technical shape.
    ids=[2,5,9,12,15,18,30]
    lengths=[3300,3416,3234,3235,3240,3240,19666]
    ts=tracks(lengths,ids);ts[-1].chapters=30
    evidence=scan({n:[i] for i,n in enumerate(ids[:-1])}|{30:list(range(6))})
    chosen,report=select(job(),ts,rank,scan=evidence)
    assert [int(t.track_number) for t in chosen]==ids[:-1]
    assert report['technical_analysis']['combined_candidates'][0]['status']=='established segment union'
    assert 30 not in report['selected_ids']
    assert len(report['titles'])==7


def test_duration_only_playall_without_chapter_evidence_waits():
    ts=tracks([3300]*4+[13200])
    with pytest.raises(SelectionPending) as exc:select(job(),ts,rank)
    assert exc.value.report['technical_analysis']['combined_candidates'][0]['status']=='possible Play All'
    assert exc.value.report['selected_ids']==[]


def test_duration_and_chapter_sum_corroborates_playall():
    ts=tracks([3300]*4+[13200]);ts[-1].chapters=20
    chosen,report=select(job(),ts,rank)
    assert len(chosen)==4 and report['selection_category']=='tv'


def test_three_segment_independent_episodes_start():
    ts=tracks([2700,2710,2720])
    chosen,_=select(job(),ts,rank,scan=scan({0:[1],1:[2],2:[3]}))
    assert len(chosen)==3


def test_overlapping_episode_playlists_wait():
    ts=tracks([2700,2710,2720,2730])
    with pytest.raises(SelectionPending):select(job(),ts,rank,scan=scan({0:[1,2],1:[2,3],2:[4],3:[5]}))


def test_exact_duplicate_movie_playlist_not_another_cut():
    ts=tracks([7200,7200,8])
    chosen,report=select(job(),ts,rank,scan=scan({0:[1,2,3],1:[1,2,3],2:[4]}))
    assert [t.track_number for t in chosen]==['0']
    assert report['technical_analysis']['duplicate_sources']=={'1':0}


@pytest.mark.parametrize('kind',['unknown','movie'])
def test_multiple_movie_cuts_are_not_tv_or_arbitrarily_main(kind):
    ts=tracks([7200,7300,7400])
    if kind=='unknown':
        with pytest.raises(SelectionPending):select(job(kind),ts,rank)
    else:
        selected,report=select(job(kind),ts,rank,recognition_finished=True)
        assert not selected and report['status']=='ambiguous'


@pytest.mark.parametrize('kind',['unknown','movie'])
def test_clear_movie_uses_native_main_feature(kind):
    ts=tracks([7200,800,20]);ts[0].chapters=20
    selected,report=select(job(kind),ts,rank)
    assert [t.track_number for t in selected]==['0']
    assert report['settings']['minimum_seconds']==600


def test_feature_length_documentary_is_not_automatically_episode():
    ts=tracks([7200,6600,4])
    with pytest.raises(SelectionPending):select(job(),ts,rank)


def test_late_category_does_not_change_existing_reservation():
    # Same-cycle reservation immutability is tested by overlay prepare tests;
    # analysis itself does not mutate tracks or perform extraction.
    ts=tracks([3300]*4)
    original=[vars(t).copy() for t in ts]
    assert analyze_titles(ts)['strong_tv_structure']
    assert [vars(t) for t in ts]==original


def test_mixed_short_programme_waits_for_scoped_evidence():
    with pytest.raises(SelectionPending):select(job(),tracks([3300]*4+[450]),rank)


def test_known_tv_noncluster_episode_is_not_dropped():
    chosen,_=select(job('series'),tracks([3300]*4+[1800]),rank,recognition_finished=True)
    assert len(chosen)==5


def test_same_playlist_conflicting_streams_are_not_deduplicated():
    ts=tracks([7200,7200])
    evidence=scan({0:[1,2],1:[1,2]})
    evidence['sources']['1']['title_attributes']['16']['value']=evidence['sources']['0']['title_attributes']['16']['value']
    evidence['sources']['1']['streams']['1']={'1':{'code':6203,'value':'Subtitle'}}
    report=analyze_titles(ts,evidence)
    assert not report['duplicate_sources']
    with pytest.raises(SelectionPending):select(job(),ts,rank,scan=evidence)


def test_unknown_duration_waits_then_scoped_complete_plan_includes_it():
    ts=tracks([3300]*4+[1]);ts[-1].length=None
    j=job();j.label='SYNTHETIC'
    with pytest.raises(SelectionPending):select(j,ts,rank)
    ref={'id':'synthetic-scope','facts':{'episodes':[1,2,3,4,5]},
         'disc_observation':{'label':j.label,'source_signature':[{'source_id':i,'duration_seconds':t.length} for i,t in enumerate(ts)]},
         'source_observations':[{'source_id':i,'assignment':{'kind':'tv','number':i+1}} for i in range(5)]}
    chosen,_=select(j,ts,rank,content_kind='tv',tv_context={'reference':ref,'episodes':[1,2,3,4,5]})
    assert len(chosen)==5
