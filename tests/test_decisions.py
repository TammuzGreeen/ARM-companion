import tempfile
import unittest
from pathlib import Path

from recognition_helper.decisions import compatible, filename, map_outputs, merge
from recognition_helper.masterlists import load_references
from recognition_helper.tray_monitor import TrayMonitor


def local(model='local1', **facts):
    return {'model': model, 'findings': facts}


def sources(count=4):
    return [{'source_id': n, 'path': f'/media/detention/DETAINED_job_source{n}.mkv', 'state': 'writing'} for n in range(count)]


class DecisionsTest(unittest.TestCase):
    def test_reversed_episode_list_maps_ascending_established_numbers(self):
        d=merge([local(media_type='tv',series='Voyager',season=2,episodes=[8,7,6,5])])
        result=map_outputs(d,list(reversed(sources())))
        self.assertEqual([(s['source_id'],s['assignment']['number']) for s in sorted(result,key=lambda s:s['source_id'])],[(0,5),(1,6),(2,7),(3,8)])

    def test_compatible_partial(self):
        d = merge([local(series='Star Trek', episodes=[1, 2, 3, 4]),
                   local('local2', media_type='tv', series='Star Trek: Voyager', season=2)])
        self.assertFalse(d['conflicts'])
        self.assertEqual(d['identity']['series'], 'Star Trek: Voyager')
        self.assertTrue(all(s['proposed'] for s in map_outputs(d, sources())))

    def test_different_series_conflict(self):
        d = merge([local(series='Star Trek: Voyager'), local('local2', series='Star Trek: Deep Space Nine')])
        self.assertIn('series', d['conflicts'])

    def test_short_specific_series_beats_longer_broad_franchise(self):
        d = merge([local(series='Voyager'), local('b', series='Star Trek')])
        self.assertEqual(d['identity']['series'], 'Voyager')

    def test_broad_franchise_is_insufficient_series_identity(self):
        d = merge([local(media_type='tv', series='Star Trek', season=2, episodes=[1, 2, 3, 4])])
        self.assertTrue(all(s['reason'] for s in map_outputs(d, sources())))

    def test_fallback_agrees_either_local(self):
        d = merge([local(season=1), local('local2', season=2)], local('external', season=2))
        self.assertFalse(d['conflicts'])
        self.assertEqual(d['identity']['season'], 2)
        self.assertIn('local2', d['attribution']['season'])

    def test_fallback_neither_detains(self):
        d = merge([local(season=1), local('local2', season=2)], local('external', season=3))
        self.assertIn('season', d['conflicts'])

    def test_reference_only_fallback_vote_cannot_overrule_both_locals(self):
        refs = [{'id': 'release', 'facts': {'series': 'Voyager', 'season': 2, 'episodes': [9]}}]
        d = merge([local(media_type='tv', series='Voyager', season=1, episodes=[9]),
                   local('b', series='Voyager', season=1, episodes=[9])], local('external', season=2), refs)
        self.assertIn('season', d['conflicts'])
        self.assertIsNone(map_outputs(d, sources(1))[0]['proposed'])

    def test_fallback_can_add_compatible_episode_coverage(self):
        d = merge([local(media_type='tv', series='Voyager', season=2, episodes=[5, 6]),
                   local('b', episodes=[5, 6])], local('external', episodes=[5, 6, 7, 8]))
        self.assertFalse(d['conflicts'])
        self.assertTrue(all(s['proposed'] for s in map_outputs(d, sources())))

    def test_fallback_resolved_field_not_reconflicted_by_reference(self):
        refs = [{'id': 'r', 'facts': {'series': 'Voyager', 'season': 1, 'episodes': [1, 2]}}]
        d = merge([local(series='Voyager', season=1, episodes=[1, 2]), local('b', season=2)],
                  local('external', season=2), refs)
        self.assertNotIn('season', d['conflicts'])
        self.assertEqual(d['identity']['season'], 2)

    def test_fallback_rejects_other_facts_from_incompatible_reference(self):
        refs = [{'id': 'r', 'facts': {'series': 'Voyager', 'season': 1, 'episodes': [1, 2], 'disc': 7}}]
        d = merge([local(series='Voyager', season=1, episodes=[1, 2]), local('b', season=2)],
                  local('external', season=2), refs)
        self.assertNotIn('disc', d['identity'])

    def test_rejected_reference_cannot_leave_unsupported_series_specificity(self):
        refs = [{'id': 'r', 'facts': {'series': 'Star Trek: Voyager', 'season': 1, 'episodes': [1]}}]
        locals_ = [local(media_type='tv', series='Star Trek', season=1, episodes=[1]),
                   local('b', series='Star Trek', season=2)]
        d = merge(locals_, local('external', season=2), refs)
        self.assertEqual(d['identity']['series'], 'Star Trek')
        self.assertIsNone(map_outputs(d, sources(1))[0]['proposed'])
        supported = merge(locals_, local('external', series='Star Trek: Voyager', season=2), refs)
        self.assertTrue(map_outputs(supported, sources(1))[0]['proposed'])

    def test_compatible_partial_reference_extends_episode_coverage(self):
        refs = [{'id': 'r', 'facts': {'series': 'Voyager', 'season': 1, 'episodes': [1, 2]}}]
        d = merge([local(media_type='tv', series='Voyager', season=1, episodes=[1])], references=refs)
        self.assertEqual(d['identity']['episodes'], [1, 2])
        self.assertTrue(all(s['proposed'] for s in map_outputs(d, sources(2))))

    def test_ambiguity_requires_fallback_and_can_be_resolved(self):
        observations = [local(media_type='movie', title='Dune', ambiguous=True)]
        self.assertTrue(map_outputs(merge(observations), sources(1))[0]['reason'])
        resolved = merge(observations, local('external', title='Dune', year=2021, ambiguous=False))
        self.assertIsNotNone(map_outputs(resolved, sources(1))[0]['proposed'])

    def test_contradictory_source_association_not_overridden(self):
        d = merge([local(media_type='tv', series='V', season=2, episodes=[1, 2])])
        original = sources(2)
        original[0]['identified_assignment'] = {'number': 8, 'evidence': 'disc metadata'}
        self.assertTrue(all(s['reason'] for s in map_outputs(d, original)))

    def test_fallback_supplies_missing(self):
        d = merge([local(media_type='tv', series='Voyager'), local('local2', series='Voyager')],
                  local('external', season=2, episodes=[1, 2, 3, 4]))
        self.assertTrue(all(s['proposed'] for s in map_outputs(d, sources())))

    def test_missing_season_insufficient(self):
        d = merge([local(media_type='tv', series='Voyager', episodes=[1, 2, 3, 4])])
        self.assertTrue(all(s['reason'] for s in map_outputs(d, sources())))

    def test_episode_count_mismatch_never_invents_extra(self):
        d = merge([local(media_type='tv', series='Voyager', season=2, episodes=[5, 6])])
        result = map_outputs(d, sources())
        self.assertTrue(all(not s['proposed'] for s in result))
        self.assertTrue(all('mismatch' in s['reason'] for s in result))

    def test_default_source_order_not_database_id(self):
        d = merge([local(media_type='tv', series='Voyager', season=2, episodes=[5, 6, 7, 8])])
        original = sources()
        for i, s in enumerate(original):
            s['arm_track_id'] = 100 - i
        result = map_outputs(d, list(reversed(original)))
        self.assertEqual([s['assignment']['number'] for s in result], [5, 6, 7, 8])
        self.assertIn('ascending', result[0]['assignment']['attribution'])

    def test_partial_masterlist_without_approval(self):
        refs = [{'id': 'partial', 'facts': {'series': 'Star Trek: Voyager', 'season': 2, 'episodes': [1, 2, 3, 4]}}]
        d = merge([local(series='Star Trek', episodes=[1, 2, 3, 4])], references=refs)
        self.assertEqual(d['identity']['season'], 2)
        self.assertEqual(d['attribution']['season'], ['masterlist:partial'])
        self.assertEqual(d['identity']['series'], 'Star Trek: Voyager')
        self.assertTrue(all(s['proposed'] for s in map_outputs(d, sources())))

    def test_alias_collision_preserves_ambiguity_regardless_of_reference_order(self):
        refs = [{'id': 'a', 'aliases': ['Shared Label'], 'facts': {'series': 'Series A', 'season': 1, 'episodes': [1]}},
                {'id': 'b', 'aliases': ['Shared Label'], 'facts': {'series': 'Series B', 'season': 1, 'episodes': [2]}}]
        for order in (refs, list(reversed(refs))):
            d = merge([local(media_type='tv', series='Shared Label', season=1)], references=order)
            self.assertTrue(d['alias_ambiguities'])
            self.assertNotIn('series', d['identity'])
            self.assertIsNone(map_outputs(d, sources(1))[0]['proposed'])

    def test_other_local_can_disambiguate_known_alias(self):
        refs = [{'id': 'a', 'aliases': ['Shared Label'], 'facts': {'series': 'Series A', 'season': 1, 'episodes': [1]}},
                {'id': 'b', 'aliases': ['Shared Label'], 'facts': {'series': 'Series B', 'season': 1, 'episodes': [2]}}]
        d = merge([local(media_type='tv', series='Shared Label', season=1), local('b', series='Series A')], references=refs)
        self.assertEqual(d['identity']['series'], 'Series A')
        self.assertTrue(map_outputs(d, sources(1))[0]['proposed'])

    def test_ambiguous_masterlists_not_arbitrary(self):
        refs = [{'id': str(n), 'facts': {'series': 'Voyager', 'season': n, 'episodes': [1, 2]}} for n in (1, 2)]
        d = merge([local(series='Voyager', episodes=[1, 2])], references=refs)
        self.assertNotIn('season', d['identity'])

    def test_more_optional_fields_do_not_make_an_incompatible_candidate_authoritative(self):
        refs = [{'id': 'a', 'facts': {'series': 'Voyager', 'season': 2, 'disc': 1, 'episodes': [1, 2]}},
                {'id': 'b', 'facts': {'series': 'Voyager', 'season': 2, 'episodes': [3, 4]}}]
        d = merge([local(series='Voyager', season=2, disc=1)], references=refs)
        self.assertNotIn('episodes', d['identity'])
        self.assertEqual(set(d['masterlist']['candidates']), {'a', 'b'})

    def test_episode_title_disambiguates_partial_reference(self):
        refs = [{'id': str(n), 'facts': {'series': 'Star Trek: Voyager', 'season': n, 'episodes': [1, 2, 3, 4]},
                 'episode_titles': ['The 37s'] if n == 2 else ['Other title']} for n in (1, 2)]
        d = merge([local(series='Star Trek', episodes=[1, 2, 3, 4], episode_titles=['The 37s'])], references=refs)
        self.assertEqual(d['identity']['season'], 2)

    def test_local_masterlist_contradiction(self):
        refs = [{'id': 'partial', 'facts': {'series': 'Voyager', 'season': 2, 'episodes': [1, 2]}}]
        d = merge([local(series='Voyager', season=2), local('local2', season=3)], references=refs)
        self.assertIn('season', d['conflicts'])

    def test_episodes_release_extra_detained(self):
        d = merge([local(media_type='tv', series='Voyager', season=2, episodes=[1, 2, 3, 4])])
        original = sources(5)
        original[-1]['is_extra'] = True  # established source metadata, never guessed by count
        result = map_outputs(d, original)
        self.assertTrue(all(s['proposed'] for s in result[:4]))
        self.assertIsNone(result[-1]['proposed'])

    def test_established_episode_proceeds_while_other_sources_have_mismatched_coverage(self):
        d = merge([local(media_type='tv', series='Voyager', season=2, episodes=[1, 2])])
        original = sources(3)
        original[0]['identified_assignment'] = {'number': 1, 'evidence': 'disc-specific metadata'}
        result = map_outputs(d, original)
        self.assertTrue(result[0]['proposed'])
        self.assertTrue(all(s['proposed'] is None and 'mismatch' in s['reason'] for s in result[1:]))
        self.assertFalse(any(s.get('is_extra') for s in result))

    def test_source_associated_extra_is_independent_of_season_conflict(self):
        observations = [local(media_type='tv', series='Voyager', season=1, episodes=[1], extras=['Interview']),
                        local('b', season=2, extras=['Interview'])]
        original = sources(2)
        original[1]['extra_identity'] = 'Interview'
        result = map_outputs(merge(observations), original, observations)
        self.assertIsNone(result[0]['proposed'])
        self.assertEqual(result[1]['proposed'], 'extras/Interview.mkv')

    def test_both_local_extra_with_source_association(self):
        observations = [local(media_type='tv', series='Voyager', season=2, episodes=[1], extras=['Interview']),
                        local('local2', extras=['Interview'])]
        d = merge(observations)
        original = sources(2)
        original[-1]['extra_identity'] = 'Interview'
        result = map_outputs(d, original, observations)
        self.assertEqual(result[-1]['proposed'], 'extras/Interview.mkv')

    def test_extra_identity_without_source_association_not_guessed(self):
        observations = [local(media_type='tv', series='V', season=1, episodes=[1], extras=['Interview']),
                        local('local2', extras=['Interview'])]
        result = map_outputs(merge(observations), sources(2), observations)
        self.assertTrue(all(s['proposed'] is None for s in result))

    def test_movie_year_optional(self):
        d = merge([local(media_type='movie', title='Arrival')])
        self.assertEqual(map_outputs(d, sources(1))[0]['proposed'], 'movies/Arrival/Arrival.mkv')

    def test_distinct_movie_titles_not_prefix_merged(self):
        self.assertFalse(compatible('title', 'It', 'It Follows'))

    def test_music_and_audiobook_names(self):
        self.assertEqual(str(filename({'media_type': 'music', 'artist': 'A', 'album': 'B'}, {'kind': 'music', 'number': 3}, '.flac')), 'music/A/B/03.flac')
        name = filename({'media_type': 'audiobook', 'author': 'A', 'book': 'B', 'disc': 2}, {'kind': 'audiobook', 'number': 1}, '.flac')
        self.assertEqual(str(name), 'audiobooks/B/Disc 2 - 01.flac')
        name = filename({'media_type': 'audiobook', 'author': 'A', 'book': 'B', 'disc': 2, 'part': '3'}, {'kind': 'audiobook', 'number': 1}, '.flac')
        self.assertEqual(str(name), 'audiobooks/B/Part 3 - Disc 2 - 01.flac')

    def test_versions_explicit_assignment(self):
        d = {'identity': {'media_type': 'tv', 'series': 'S', 'season': 1, 'episodes': [1]}, 'conflicts': {}}
        result = map_outputs(d, sources(2), assignments={'0': {'kind': 'tv', 'number': 1, 'version': 'original'}, '1': {'kind': 'tv', 'number': 1, 'version': 'remastered'}})
        self.assertNotEqual(result[0]['proposed'], result[1]['proposed'])

    def test_duplicate_version_names_detain_both_sources(self):
        d = {'identity': {'media_type': 'tv', 'series': 'S', 'season': 1, 'episodes': [1]}, 'conflicts': {}}
        assignments = {'0': {'kind': 'tv', 'number': 1, 'version': 'Extended'},
                       '1': {'kind': 'tv', 'number': 1, 'version': 'extended'}}
        result = map_outputs(d, sources(2), assignments=assignments)
        self.assertTrue(all(s['proposed'] is None and s['reason'] for s in result))

    def test_legacy_partial_approved_false_usable(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'ref.json').write_text('{"series":"V","season":2,"approved":false,"discs":[{"number":1,"episodes":[{"number":1}]}]}')
            refs = load_references(root)
            self.assertFalse(refs[0]['legacy_approved'])
            self.assertEqual(refs[0]['facts']['episodes'], [1])


class TrayTest(unittest.TestCase):
    def test_unknown_interval_preserves_transition(self):
        readings = iter([1, 1, 1, 3, 2, 2, 2])
        monitor = TrayMonitor('/dev/test', reader=lambda: next(readings))
        results = [monitor.poll(n + 1) for n in range(7)]
        self.assertTrue(results[2]['initial'])
        self.assertEqual(results[-1]['transition'], 'open')

    def test_no_disc_means_closed(self):
        monitor = TrayMonitor('/dev/test', reader=lambda: 1)
        for n in range(3):
            result = monitor.poll(n + 1)
        self.assertEqual(result['state'], 'closed')


if __name__ == '__main__':
    unittest.main()
