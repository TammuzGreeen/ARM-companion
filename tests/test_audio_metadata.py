"""Synthetic physical/lookup evidence; not a physical qualification."""
import copy

import pytest

from recognition_helper.audio_metadata import apply_audio_metadata, partial_title
from recognition_helper.decisions import merge, map_outputs


def audio_cycle(spoken=False):
    title = 'Winter Tales: An Audio Drama' if spoken else 'Northern Lights: Live in Oslo'
    toc = [{'number': n, 'duration_seconds': 90 + n * 31, 'offset_frames': 150 + n * 5000} for n in range(1, 6)]
    candidate = {'release_id': 'release-a', 'format': 'CD', 'disc_ids': ['actual-disc-id'],
                 'facts': {'album': title, 'artist': 'The Ensemble', 'year': '2006', 'disc': '1'},
                 'tracks': [dict(number=t['number'], duration_seconds=t['duration_seconds'] - .5,
                                 title=f'Title {t["number"]}') for t in toc]}
    evidence = {'kind': 'physical CD TOC via ARM libdiscid read', 'status': 'completed',
                'disc_id': 'actual-disc-id', 'job_id': 8, 'job_guid': 'guid-8', 'tracks': toc,
                'musicbrainz': {'lookup_disc_id': 'actual-disc-id', 'candidates': [candidate]}}
    locals_ = [{'model': 'a', 'findings': {'media_type': 'audiobook' if spoken else 'music',
                  'album': title.split(':')[0], 'book': title.split(':')[-1].strip(),
                  'author': 'Various' if spoken else None, 'tracks': [1], 'disc': 1}},
               {'model': 'b', 'findings': {'media_type': 'music', 'album': title.split(':')[-1].strip(),
                  'artist': 'Wrong visual guess', 'tracks': [1], 'disc': 1}}]
    sources = [{'source_id': t['number'], 'arm_track_id': t['number'] + 100, 'path': f'/detention/DETAINED_source{t["number"]}.flac',
                'state': 'writing', 'backend_metadata': {'source_number_kind': 'CD TOC track number',
                'physical_toc_track': t, 'prescan': evidence, 'cycle_id': 'cycle-a', 'job_guid': 'guid-8'}} for t in toc]
    return {'id': 'cycle-a', 'job_id': 8, 'locals': locals_, 'sources': sources,
            'backend_editorial_metadata': dict(candidate['facts'])}


def decide(cycle):
    decision = merge(cycle['locals'], cycle.get('fallback'))
    assert apply_audio_metadata(cycle, decision)
    return decision


def candidate(cycle):
    return cycle['sources'][0]['backend_metadata']['prescan']['musicbrainz']['candidates'][0]


def test_corroborated_field_override_physical_tracks_and_optional_titles():
    cycle = audio_cycle()
    originals = copy.deepcopy(cycle['locals'])
    decision = decide(cycle)
    assert decision['identity']['artist'] == 'The Ensemble'
    assert decision['identity']['album'] == 'Northern Lights: Live in Oslo'
    assert decision['identity']['tracks'] == [1, 2, 3, 4, 5]
    assert decision['identity']['track_titles']['5'] == 'Title 5'
    assert decision['conflicts'] == {}
    outputs = map_outputs(decision, list(reversed(cycle['sources'])), cycle['locals'])
    assert [o['assignment']['number'] for o in outputs] == [1, 2, 3, 4, 5]
    assert all(o['proposed'].endswith(f'{o["source_id"]:02d} - Title {o["source_id"]}.flac') for o in outputs)
    assert cycle['locals'] == originals
    report = decision['audio_metadata']
    assert report['visual_tracks'] == [[1], [1]]
    assert report['physical_track_count'] == report['backend_source_count'] == 5
    assert report['extracted_sources'] == 0


def test_missing_catalogue_fields_use_compatible_visual_findings():
    cycle = audio_cycle()
    candidate(cycle)['facts'].pop('artist')
    for observation in cycle['locals']:
        observation['findings']['artist'] = 'Printed Artist'
    decision = decide(cycle)
    assert decision['identity']['artist'] == 'Printed Artist'
    assert decision['attribution']['artist'] == ['a', 'b']


@pytest.mark.parametrize('short,full,expected', [
    ('Northern Lights', 'Northern Lights: Live in Oslo', True),
    ('Live in Oslo', 'Northern Lights: Live in Oslo', True),
    ('Other Recording', 'Northern Lights: Live in Oslo', False),
    ('Age', 'Ice Age', False), ('Lights Northern', 'Northern Lights', False)])
def test_audio_partial_title_compatibility(short, full, expected):
    assert partial_title(short, full) is expected


def test_contradictory_visual_title_is_audited_not_called_compatible():
    cycle = audio_cycle()
    for o in cycle['locals']:
        o['findings']['album'] = 'Unrelated Recording'
    decision = decide(cycle)
    assert decision['audio_metadata']['fields']['album']['visual_relation'] == 'contradictory_visual_guess'
    assert decision['identity']['album'] != 'Unrelated Recording'


def test_audio_drama_unknown_author_does_not_block_or_become_performer():
    cycle = audio_cycle(spoken=True)
    decision = decide(cycle)
    assert decision['identity']['media_type'] == 'audiobook'
    assert decision['identity']['book'] == 'Winter Tales: An Audio Drama'
    assert decision['identity']['artist'] == 'The Ensemble'
    assert 'author' not in decision['identity']
    assert all(o['proposed'] for o in map_outputs(decision, cycle['sources']))
    assert decision['audio_metadata']['author_status'] == 'unknown_optional'


def test_explicit_author_can_be_used_but_artist_cannot_supply_it():
    cycle = audio_cycle(spoken=True)
    candidate(cycle)['facts']['author'] = 'Explicit Writer'
    decision = decide(cycle)
    assert decision['identity']['author'] == 'Explicit Writer'
    assert all(o['proposed'] for o in map_outputs(decision, cycle['sources']))


def test_optional_track_titles_not_required():
    cycle = audio_cycle()
    for t in candidate(cycle)['tracks']:
        t.pop('title')
    decision = decide(cycle)
    assert 'track_titles' not in decision['identity']
    assert all(o['proposed'] for o in map_outputs(decision, cycle['sources']))


@pytest.mark.parametrize('defect', ['durations', 'disc_id', 'missing_toc', 'wrong_job', 'wrong_cycle', 'count_only', 'offsets'])
def test_unreliable_or_physically_contradictory_metadata_cannot_override(defect):
    cycle = audio_cycle()
    c = candidate(cycle)
    evidence = cycle['sources'][0]['backend_metadata']['prescan']
    if defect == 'durations':
        c['tracks'][2]['duration_seconds'] += 100
    elif defect == 'disc_id':
        c['disc_ids'] = ['different-disc']
    elif defect == 'missing_toc':
        evidence['tracks'] = []
    elif defect == 'wrong_job':
        evidence['job_id'] = 7
    elif defect == 'wrong_cycle':
        cycle['id'] = 'next-disc'
    elif defect == 'offsets':
        evidence['musicbrainz']['lookup_offsets'] = [150, 1, 2, 3, 4]
    else:
        for t in c['tracks']:
            t['duration_seconds'] = None
    decision = decide(cycle)
    assert decision['identity']['artist'] == 'Wrong visual guess'
    assert decision['audio_metadata']['corroboration'] == 'reliability_not_established'
    assert 'track_titles' not in decision['identity']


def test_missing_required_catalogue_identity_not_bootstrapped_by_count_only():
    cycle = audio_cycle()
    candidate(cycle)['disc_ids'] = []
    for o in cycle['locals']:
        o['findings'].pop('artist', None)
    assert 'artist' not in decide(cycle)['identity']


def test_ambiguous_releases_accept_only_shared_fields_not_arbitrary_album():
    cycle = audio_cycle()
    other = copy.deepcopy(candidate(cycle))
    other['release_id'] = 'release-b'
    other['facts']['album'] = 'Another Concert'
    cycle['sources'][0]['backend_metadata']['prescan']['musicbrainz']['candidates'].append(other)
    decision = decide(cycle)
    assert decision['identity']['artist'] == 'The Ensemble'
    assert 'album' not in decision['identity']
    assert 'album' in decision['conflicts']
    assert not any(o['proposed'] for o in map_outputs(decision, cycle['sources']))


def test_incomplete_candidate_cannot_silently_break_release_tie():
    cycle = audio_cycle()
    other = copy.deepcopy(candidate(cycle))
    other['tracks'][0]['duration_seconds'] = None
    other['facts']['artist'] = 'Other Performer'
    cycle['sources'][0]['backend_metadata']['prescan']['musicbrainz']['candidates'].append(other)
    decision = decide(cycle)
    assert 'audio_release' in decision['conflicts']
    assert decision['identity']['artist'] == 'Wrong visual guess'


@pytest.mark.parametrize('kind', ['tv', 'movie'])
def test_audio_exception_does_not_change_tv_or_movie(kind):
    cycle = audio_cycle()
    for o in cycle['locals']:
        o['findings']['media_type'] = kind
    decision = merge(cycle['locals'])
    original = copy.deepcopy(decision)
    assert not apply_audio_metadata(cycle, decision)
    assert decision == original


def test_technical_music_does_not_resolve_spoken_category_conflict_without_cue():
    cycle = audio_cycle()
    cycle['locals'][0]['findings']['media_type'] = 'audiobook'
    decision = decide(cycle)
    assert 'media_type' in decision['conflicts']


def test_existing_fallback_local_agreement_resolves_non_catalogue_field():
    cycle = audio_cycle(spoken=True)
    cycle['locals'][0]['findings']['author'] = 'Writer A'
    cycle['locals'][1]['findings']['author'] = 'Writer B'
    cycle['fallback'] = {'model': 'fallback', 'findings': {'author': 'Writer B'}}
    decision = decide(cycle)
    assert decision['identity']['author'] == 'Writer B'
    assert 'author' not in decision['conflicts']


def test_fallback_with_local_support_rejects_uncorroborated_editorial_guess():
    cycle = audio_cycle()
    for t in candidate(cycle)['tracks']:
        t['duration_seconds'] = None
    for o in cycle['locals']:
        o['findings']['artist'] = 'Printed Artist'
    cycle['fallback'] = {'model': 'fallback', 'findings': {'artist': 'Printed Artist'}}
    decision = decide(cycle)
    assert decision['identity']['artist'] == 'Printed Artist'
    assert 'artist' not in decision['conflicts']
    assert decision['audio_metadata']['rejected_backend_facts']['artist'] == 'The Ensemble'


def test_previous_ice_age_observations_with_two_disc_id_release_matches():
    # Public facts from retained job 6 and the fresh read-only MusicBrainz check.
    # Same physical disc matches two barcodes; do not claim a unique edition.
    cycle = audio_cycle(spoken=True)
    toc = cycle['sources'][0]['backend_metadata']['prescan']['tracks']
    durations = [707, 1151, 916, 665, 175]
    offsets = [150, 53149, 139439, 208156, 258053]
    for t, seconds, offset in zip(toc, durations, offsets):
        t.update(duration_seconds=seconds, offset_frames=offset)
    first = candidate(cycle)
    first['facts'].update(album='Ice Age: Das Original-Hörspiel zum Kinofilm', artist='Hans‐Georg Panczak')
    for t, seconds, title in zip(first['tracks'], durations, ['01-Ice Age', '02-Ice Age', '03-Ice Age', '04-Ice Age', 'Eiszeit']):
        t.update(duration_seconds=seconds - .4, title=title)
    other = copy.deepcopy(first)
    other['release_id'] = 'another-barcode'
    cycle['sources'][0]['backend_metadata']['prescan']['musicbrainz']['candidates'].append(other)
    cycle['locals'][0]['findings'].update(title='Ice Age', book='Das Original-Hörspiel zum Kinofilm', album='Ice Age', author='Various')
    cycle['locals'][1]['findings'].update(artist='Ice Age', album='Das Original-Hörspiel zum Kinofilm')
    decision = decide(cycle)
    assert decision['identity']['media_type'] == 'audiobook'
    assert decision['identity']['album'] == decision['identity']['book'] == first['facts']['album']
    assert decision['identity']['tracks'] == [1, 2, 3, 4, 5]
    assert decision['identity']['track_titles']['5'] == 'Eiszeit'
    assert decision['conflicts'] == {}
    assert 'author' not in decision['identity']
    assert all(o['proposed'] for o in map_outputs(decision, cycle['sources']))


@pytest.mark.parametrize('kind,title,artist', [
    ('music', 'Silent River Sessions', 'River Quartet'),
    ('audiobook', 'Letters from Home', None),
    ('audiobook', 'Harbour Radio Play', 'Harbour Players'),
    ('audiobook', 'Public Lectures Volume Two', None)])
def test_generic_audio_without_musicbrainz_optional_metadata(kind, title, artist):
    cycle = audio_cycle()
    cycle['sources'][0]['backend_metadata']['prescan'].pop('musicbrainz')
    cycle['backend_editorial_metadata'] = {}
    facts = {'media_type': kind, 'album' if kind == 'music' else 'book': title}
    if artist:
        facts['artist'] = artist
    cycle['locals'] = [{'model': name, 'findings': dict(facts)} for name in ('a', 'b')]
    decision = decide(cycle)
    assert decision['identity']['tracks'] == [1,2,3,4,5]
    assert decision['conflicts'] == {}
    assert all(o['proposed'] for o in map_outputs(decision, cycle['sources']))
    assert 'author' not in decision['identity']


def test_generic_spoken_title_only_and_ambiguous_work_remain_distinct():
    cycle = audio_cycle()
    cycle['sources'][0]['backend_metadata']['prescan'].pop('musicbrainz')
    cycle['backend_editorial_metadata'] = {}
    cycle['locals'] = [{'model': name, 'findings': {'media_type': 'audiobook', 'title': 'Tales at Dusk'}} for name in ('a','b')]
    assert decide(cycle)['identity']['book'] == 'Tales at Dusk'
    cycle['locals'][0]['findings']['title'] = 'Unrelated Tales'
    decision = decide(cycle)
    assert all(not o['proposed'] for o in map_outputs(decision, cycle['sources']))


def test_optional_spoken_disputes_are_retained_but_not_release_blockers():
    cycle=audio_cycle(spoken=True)
    cycle['sources'][0]['backend_metadata']['prescan'].pop('musicbrainz')
    cycle['backend_editorial_metadata']={}
    cycle['locals']=[{'model':str(i),'findings':{'media_type':'audiobook','book':'Shared Work',
                      'author':f'Writer {i}','narrator':f'Reader {i}','year':2000+i}} for i in (1,2)]
    decision=decide(cycle)
    assert not decision['conflicts']
    assert set(decision['audio_metadata']['optional_disagreements'])=={'author','narrator','year'}
    assert 'author' not in decision['identity'] and 'year' not in decision['identity']
    assert all(o['proposed'] for o in map_outputs(decision,cycle['sources']))


def test_no_mb_compatible_partial_album_can_supply_spoken_work():
    cycle=audio_cycle()
    cycle['sources'][0]['backend_metadata']['prescan'].pop('musicbrainz')
    cycle['backend_editorial_metadata']={}
    cycle['locals']=[{'model':str(i),'findings':{'media_type':'audiobook','album':title}}
                     for i,title in enumerate(('Public Lectures','Public Lectures: Modern Science'))]
    decision=decide(cycle)
    assert decision['identity']['book']=='Public Lectures: Modern Science'
    assert all(o['proposed'] for o in map_outputs(decision,cycle['sources']))
