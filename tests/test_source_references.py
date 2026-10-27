import unittest

from recognition_helper.decisions import map_outputs
from recognition_helper.source_references import apply_reference, signature


class SourceReferenceTest(unittest.TestCase):
    def sources(self):
        return [{'source_id': n, 'path': f'DETAINED_{n}.mkv',
                 'backend_metadata': {'scan_duration': 2600 + n}} for n in range(2)]

    def reference(self, sources):
        return {'id': 'release/disc1', 'facts': {'media_type': 'tv', 'series': 'A', 'season': 1, 'episodes': [1]},
                'disc_observation': {'label': 'DISC_ONE', 'source_signature': signature(sources)},
                'source_observations': [{'source_id': n, 'assignment': {'kind': 'tv', 'number': 1, 'version': v}}
                                        for n, v in enumerate(('original', 'remastered'))]}

    def test_exact_disc_scoped_version_association(self):
        sources = self.sources()
        ref = self.reference(sources)
        mapped_sources, reasons = apply_reference(sources, ref, {'label': 'DISC_ONE'})
        self.assertFalse(reasons)
        decision = {'identity': ref['facts'], 'conflicts': {}}
        result = map_outputs(decision, mapped_sources)
        self.assertTrue(all(s['proposed'] for s in result))
        self.assertIn('original', result[0]['proposed'])
        self.assertIn('remastered', result[1]['proposed'])

    def test_never_transplants_literal_ids_to_another_disc(self):
        sources = self.sources()
        mapped, reasons = apply_reference(sources, self.reference(sources), {'label': 'DISC_TWO'})
        self.assertTrue(reasons)
        self.assertTrue(all('identified_assignment' not in s for s in mapped))

    def test_different_source_structure_does_not_inherit_ids(self):
        sources = self.sources()
        ref = self.reference(sources)
        sources[1]['backend_metadata']['scan_duration'] = 3600
        _, reasons = apply_reference(sources, ref, {'label': 'DISC_ONE'})
        self.assertTrue(reasons)

    def test_partial_identity_reference_needs_no_inventory(self):
        sources = self.sources()
        mapped, reasons = apply_reference(sources, {'id': 'partial', 'facts': {'season': 2}}, {})
        self.assertFalse(reasons)
        self.assertEqual(mapped, sources)

    def test_collection_convention_is_not_literal_source_association(self):
        _, reasons = apply_reference(self.sources(), {'collection_conventions': {'multiple_versions_per_episode': True}}, {})
        self.assertTrue(reasons)

    def test_exported_single_movie_assignment_round_trips(self):
        sources = self.sources()[:1]
        ref = {'id': 'movie-disc', 'facts': {'media_type': 'movie', 'title': 'Arrival'},
               'disc_observation': {'label': 'MOVIE', 'source_signature': signature(sources)},
               'source_observations': [{'source_id': 0, 'assignment': {'kind': 'movie'}}]}
        scoped, reasons = apply_reference(sources, ref, {'label': 'MOVIE'})
        result = map_outputs({'identity': ref['facts'], 'conflicts': {}}, scoped)
        self.assertFalse(reasons)
        self.assertEqual(result[0]['proposed'], 'movies/Arrival/Arrival.mkv')
