import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import errno
import os
import shutil

from recognition_helper.config import Settings
from recognition_helper.engine import Engine, preserved_move, already_associated


class EngineTest(unittest.TestCase):
    def test_deleted_arm_numeric_id_can_be_reused_only_with_distinct_guid(self):
        cycles=[{'job_id':8,'backend_detail':{'job':{'guid':'old-guid'}}}]
        self.assertFalse(already_associated({'job_id':8,'guid':'new-guid'},cycles))
        self.assertTrue(already_associated({'job_id':8,'guid':'old-guid'},cycles))
        self.assertTrue(already_associated({'job_id':8},cycles))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = Engine(Settings(state=self.root / 'state', media=self.root / 'media', released=self.root / 'released'))
        self.cycle = self.engine.store.create(state='test')
        self.path = self.root / 'media' / 'detention' / 'DETAINED_job_source0.mkv'
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'not a physical rip; software test')
        self.cycle['sources'] = [{'source_id': 0, 'path': str(self.path), 'state': 'writing'}]

    def tearDown(self):
        self.engine.pool.shutdown(wait=True)
        self.temp.cleanup()

    def locals(self):
        self.cycle['locals'] = [{'model': 'a', 'findings': {'media_type': 'movie', 'title': 'Arrival'}},
                                {'model': 'b', 'findings': {'media_type': 'movie', 'title': 'Arrival'}}]
        self.cycle['local_done'] = True

    def test_recognition_first_keeps_prefix_until_complete(self):
        self.locals()
        self.engine.decide(self.cycle)
        self.engine.release(self.cycle)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.cycle['sources'][0]['state'], 'writing')
        self.cycle['sources'][0]['state'] = 'complete'
        self.cycle['backend_done'] = True
        self.engine.release(self.cycle)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.cycle['sources'][0]['state'], 'released')

    def test_queue_reconciliation_is_persistently_bounded(self):
        self.engine.arm=Mock()
        self.engine.arm.queue_status.return_value={'drives':[{'state':'cleanup_pending','mount':'/dev/sr0','job_id':7,'job_guid':'old-guid'}]}
        self.engine.arm.reconcile_drive.return_value={'success':False,'reason':'Original job PID still exists'}
        for _ in range(6):
            self.engine.refresh_queue()
        self.assertEqual(self.engine.arm.reconcile_drive.call_count,3)
        self.assertTrue((self.engine.store.root/'drive-recovery.json').exists())
        self.assertFalse(self.engine.may_calibrate()[0])

    def test_uncertain_movie_year_does_not_enter_filename(self):
        self.locals()
        for observation in self.cycle['locals']:
            observation['findings'].update(year=2016,uncertainties=['year inferred, not printed'])
        self.engine.decide(self.cycle)
        self.assertNotIn('year',self.cycle['decision']['identity'])
        self.assertNotIn('2016',self.cycle['sources'][0]['proposed'])

    def test_transient_shutdown_does_not_exhaust_drive_recovery(self):
        self.engine.arm=Mock()
        self.engine.arm.queue_status.return_value={'drives':[{'state':'cleanup_pending','mount':'/dev/sr0','job_id':7,'job_guid':'old-guid'}]}
        self.engine.arm.reconcile_drive.side_effect=[{'success':False,'deferred':True}]*4+[{'success':True}]
        with patch('recognition_helper.engine.time.time') as clock:
            for tick in range(5):
                clock.return_value=1000+tick*31
                self.engine.refresh_queue()
        self.assertEqual(self.engine.arm.reconcile_drive.call_count,5)
        self.assertEqual(self.engine.queue['drives'][0]['recovery'],{'success':True})

    def test_pending_selection_can_resolve_conflicting_categories_with_existing_fallback(self):
        self.cycle.update(sources=[],selection_waiting=True,local_done=True)
        self.cycle['locals']=[{'model':'a','findings':{'media_type':'tv','series':'Voyager'}},
                              {'model':'b','findings':{'media_type':'movie','title':'Arrival'}}]
        self.engine.decide(self.cycle)
        self.assertTrue(self.cycle['fallback_attempted'])
        self.assertIn('media_type',self.cycle['decision']['conflicts'])
        self.assertFalse(self.cycle['recognition_done'])
        self.cycle['fallback']={'model':'external','findings':{'media_type':'tv','series':'Voyager'}}
        self.engine.decide(self.cycle)
        self.assertNotIn('media_type',self.cycle['decision']['conflicts'])
        self.assertEqual(self.cycle['decision']['identity']['media_type'],'tv')

    def test_locals_finished_before_first_prepare_still_enters_pending_decision(self):
        from recognition_helper.arm import SelectionPending
        self.cycle.update(sources=[],local_done=True,close_intent=True,job_id=7,job_guid='guid')
        self.cycle['locals']=[{'model':'a','findings':{'media_type':'tv','series':'Voyager'}},
                              {'model':'b','findings':{'media_type':'movie','title':'Arrival'}}]
        self.engine.arm=Mock()
        self.engine.arm.queue_status.return_value={'drives':[{'state':'ready'}]}
        self.engine.arm.detail.return_value={'job':{'status':'manual_paused','guid':'guid'}}
        self.engine.arm.prepare_and_start.side_effect=[SelectionPending({'reason':'ambiguous'}),[]]
        self.engine.stop=Mock()
        self.engine.stop.is_set.side_effect=[False,False,True]
        self.engine.run()
        calls=self.engine.arm.prepare_and_start.call_args_list
        self.assertEqual(len(calls),2)
        self.assertFalse(calls[0].kwargs['recognition_finished'])
        self.assertTrue(calls[1].kwargs['recognition_finished'])
        self.assertTrue(self.cycle['fallback_attempted'])
        self.assertIsNone(calls[1].kwargs['content_kind'])

    def test_backend_first_recognition_later(self):
        self.cycle['sources'][0]['state'] = 'complete'
        self.engine.release(self.cycle)
        self.assertTrue(self.path.exists())
        self.locals()
        self.engine.decide(self.cycle)
        self.engine.release(self.cycle)
        self.assertEqual(self.cycle['sources'][0]['state'], 'released')

    def test_failed_extraction_never_released(self):
        self.locals()
        self.cycle['sources'][0]['state'] = 'failed'
        self.engine.decide(self.cycle)
        self.engine.release(self.cycle)
        self.assertTrue(self.path.exists())

    def test_manual_preview_and_correction_does_not_rename_writing_file(self):
        self.cycle.update(manual_required=True, recognition_done=True)
        payload = {'identity': {'media_type': 'movie', 'title': 'Correct Name'}, 'assignments': {'0': {'kind': 'movie'}}}
        result = self.engine.preview_correction(self.cycle['id'], payload)
        self.assertIn('Correct Name', result[0]['proposed'])
        self.engine.correct(self.cycle['id'], payload)
        self.assertTrue(self.path.exists())
        self.cycle['sources'][0]['state'] = 'complete'
        self.engine.release(self.cycle)
        self.assertEqual(self.cycle['sources'][0]['state'], 'released')
        self.assertEqual(len(self.cycle['corrections']), 1)

    def test_correction_owns_mapping_late_locals_preserved(self):
        payload = {'identity': {'media_type': 'movie', 'title': 'User Name'}, 'assignments': {'0': {'kind': 'movie'}}}
        self.engine.correct(self.cycle['id'], payload)
        self.locals()
        self.engine.decide(self.cycle)
        self.assertIn('User Name', self.cycle['sources'][0]['proposed'])
        self.assertEqual(self.cycle['locals'][0]['findings']['title'], 'Arrival')
        self.assertIn('previous_sources', self.cycle['corrections'][0])

    def test_collision_preserves_both_files(self):
        destination = self.root / 'released' / 'exists.mkv'
        destination.parent.mkdir()
        destination.write_bytes(b'previous user file')
        with self.assertRaises(ValueError):
            preserved_move(self.path, destination)
        self.assertTrue(self.path.exists())
        self.assertEqual(destination.read_bytes(), b'previous user file')

    def test_cross_volume_publication_keeps_copy_prefixed_until_complete(self):
        destination = self.root / 'released' / 'final.mkv'
        real_link, real_copy = os.link, shutil.copyfileobj
        calls = 0

        def link(source, target):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(errno.EXDEV, 'different filesystem')
            return real_link(source, target)

        def copy(original, output):
            self.assertFalse(destination.exists())
            staged = list((destination.parent / '.detention').iterdir())
            self.assertEqual(len(staged), 1)
            self.assertTrue(staged[0].name.startswith('DETAINED_'))
            return real_copy(original, output)

        with patch('recognition_helper.engine.os.link', side_effect=link), patch('recognition_helper.engine.shutil.copyfileobj', side_effect=copy):
            preserved_move(self.path, destination)
        self.assertTrue(destination.exists())
        self.assertFalse(self.path.exists())
        self.assertEqual(list((destination.parent / '.detention').iterdir()), [])

    def test_released_parent_symlink_cannot_escape_output_root(self):
        self.locals()
        self.cycle['sources'][0]['state'] = 'complete'
        self.engine.decide(self.cycle)
        self.engine.s.released.mkdir()
        outside = self.root / 'elsewhere'
        outside.mkdir()
        (self.engine.s.released / 'movies').symlink_to(outside, target_is_directory=True)
        self.engine.release(self.cycle)
        self.assertTrue(self.path.exists())
        self.assertIn('escapes', self.cycle['sources'][0]['release_error'])

    def test_technical_release_error_is_retried_without_new_identification(self):
        self.locals()
        self.cycle['sources'][0]['state'] = 'complete'
        self.cycle['backend_done'] = True
        self.engine.decide(self.cycle)
        with patch('recognition_helper.engine.preserved_move', side_effect=PermissionError('temporary permission error')):
            self.engine.release(self.cycle)
        source = self.cycle['sources'][0]
        self.assertIsNone(source['reason'])
        self.assertIn('permission', source['release_error'])
        self.engine.release(self.cycle)
        self.assertEqual(source['state'], 'released')
        self.assertNotIn('release_error', source)
        self.assertEqual(self.cycle['state'], 'released')

    def test_resolved_legacy_error_is_preserved_but_not_displayed_as_failure(self):
        self.locals()
        self.cycle['sources'][0]['state'] = 'complete'
        self.cycle['backend_done'] = True
        self.engine.decide(self.cycle)
        self.engine.release(self.cycle)
        source = self.cycle['sources'][0]
        source['reason'] = '[Errno 13] Permission denied: released directory'
        self.engine.release(self.cycle)
        self.assertIsNone(source['reason'])
        self.assertEqual(len(source['resolved_release_errors']), 1)

    def test_postrip_open_suppressed_then_manual_open_allowed(self):
        self.cycle['job_id'] = 1
        self.engine.arm = Mock()
        self.engine.arm.detail.return_value = {'job': {'status': 'success', 'ejected': True}}
        self.assertFalse(self.engine.may_calibrate()[0])
        self.assertTrue(self.engine.may_calibrate()[0])

    def test_old_unmatched_cycle_cannot_claim_next_disc(self):
        self.cycle['close_acknowledged'] = 1
        self.engine.arm = Mock()
        self.engine.arm.jobs.return_value = []
        next_id = self.engine.opened()
        self.assertTrue(self.cycle['superseded'])
        self.assertNotEqual(next_id, self.cycle['id'])

    def test_late_response_original_cycle(self):
        replacement = self.engine.store.create(state='new-disc')
        (self.engine.store.root / self.cycle['id'] / 'model-input-0.jpg').write_bytes(b'software-only mock model input')
        self.engine.vision.observe = Mock(return_value={'model': 'x', 'findings': {}, 'latency_seconds': 0})
        self.engine.recognize(self.cycle['id'])
        self.assertEqual(len(self.cycle['locals']), 2)
        self.assertEqual(replacement['locals'], [])

    def test_stale_capture_cannot_close_replacement_disc(self):
        replacement = self.engine.store.create(state='new-disc')
        self.engine.camera.cycle = replacement['id']
        self.engine.camera.status['tray'] = 'open'
        self.engine.arm = Mock()
        self.engine.close_and_launch(self.cycle['id'])
        self.engine.arm.tray.assert_not_called()
        self.assertEqual(self.cycle['state'], 'capture_invalidated')

    def configure_single_backend_tick(self, job_status='manual_paused', stop_time=None):
        self.engine.arm = Mock()
        self.engine.arm.jobs.return_value = [{'job_id': 2, 'devpath': '/dev/sr0'}]
        self.engine.arm.detail.return_value = {'job': {'job_id': 2, 'status': job_status, 'stop_time': stop_time}}
        self.engine.arm.prepare_and_start.return_value = [{'source_id': 0, 'path': str(self.path), 'state': 'writing'}]
        self.engine.arm.metadata.return_value = {}
        self.engine.arm.completion.return_value = {'done': False, 'status': job_status, 'sources': []}
        self.engine.stop = Mock()
        self.engine.stop.is_set.side_effect = [False, True]
        self.engine.pool.submit = Mock()
        self.cycle.update(baseline_job_ids=[], sources=[], close_intent=100, capture_complete=True)
        self.engine.camera.status['tray'] = 'closed'
        self.engine.camera.cycle = None

    def test_lost_close_response_reconciles_physical_close_without_duplicate_command(self):
        self.configure_single_backend_tick()
        self.cycle.update(state='close_error', technical_error='HTTP response lost')
        self.engine.run()
        self.engine.arm.tray.assert_not_called()
        self.assertIn('closed_at', self.cycle)
        self.assertEqual(self.cycle['job_id'], 2)
        self.engine.arm.prepare_and_start.assert_called_once_with(2, self.cycle['id'])
        self.engine.pool.submit.assert_called_once_with(self.engine.recognize, self.cycle['id'])

    def test_exhausted_start_response_retries_recover_existing_manifest_read_only(self):
        self.configure_single_backend_tick(job_status='video_ripping')
        self.cycle.update(prepare_attempts=3, prepare_intent=110, closed_at=120)
        self.engine.arm.recover.return_value = [{'source_id': 0, 'path': str(self.path), 'state': 'writing'}]
        self.engine.run()
        self.engine.arm.prepare_and_start.assert_not_called()
        self.engine.arm.recover.assert_called_once_with(2, self.cycle['id'])
        self.assertTrue(self.cycle['sources'])

    def test_backend_failure_before_sources_does_not_stay_in_progress_forever(self):
        self.configure_single_backend_tick(job_status='fail', stop_time='2026-10-24T10:00:00')
        self.engine.run()
        self.assertTrue(self.cycle['backend_done'])
        self.assertEqual(self.cycle['state'], 'technical_failure_no_outputs')

    def test_restart_does_not_repeat_an_uncertain_paid_fallback(self):
        self.cycle.update(fallback_pending=True, fallback_attempted=True, fallback_budget={'reserved_usd': .2})
        self.engine.vision.observe = Mock()
        self.engine.recover_workers()
        self.assertFalse(self.cycle['fallback_pending'])
        self.assertEqual(self.cycle['fallback_cost_usd'], .2)
        self.assertIn('unknown', self.cycle['fallback']['error'])
        self.engine.vision.observe.assert_not_called()

    def test_restart_records_interrupted_local_and_only_runs_the_unstarted_model(self):
        first, second = self.engine.s.models[:2]
        self.cycle.update(closed_at=100, local_model_ids=[first, second],
                          local_requests=[{'model': first, 'started': 100, 'status': 'running', 'attempt': 1}])
        self.engine.pool.submit = Mock()
        self.engine.recover_workers()
        self.assertEqual(self.cycle['locals'][0]['model'], first)
        self.assertIn('interrupted', self.cycle['locals'][0]['error'])
        (self.engine.store.root / self.cycle['id'] / 'model-input-0.jpg').write_bytes(b'software-only mock model input')
        self.engine.vision.observe = Mock(return_value={'model': second, 'findings': {}, 'latency_seconds': 0})
        self.engine.recognize(self.cycle['id'])
        self.assertEqual(self.engine.vision.observe.call_count, 1)
        self.assertEqual(self.engine.vision.observe.call_args.args[0], second)
        self.assertTrue(self.cycle['local_done'])

    def test_image_loss_does_not_send_a_text_only_guessing_request(self):
        self.engine.vision.observe = Mock()
        self.engine.recognize(self.cycle['id'])
        self.engine.vision.observe.assert_not_called()
        self.assertTrue(self.cycle['local_done'])
        self.assertTrue(all(o.get('error') for o in self.cycle['locals']))

    def test_editorial_backend_metadata_fills_compatible_gaps_with_attribution(self):
        self.locals()
        self.cycle['backend_editorial_metadata'] = {'title': 'Arrival', 'video_type': 'movie', 'year': 2016}
        self.engine.decide(self.cycle)
        self.assertEqual(self.cycle['decision']['identity']['year'], 2016)
        self.assertIn('editorial', self.cycle['decision']['attribution']['year'][0])

    def test_fallback_with_local_support_is_not_reconflicted_by_backend_catalogue(self):
        self.locals()
        self.cycle['locals'][0]['findings']['year'] = 2016
        self.cycle['fallback'] = {'model': 'external', 'findings': {'year': 2016}}
        self.cycle['backend_editorial_metadata'] = {'year': 2017}
        self.engine.decide(self.cycle)
        self.assertNotIn('year', self.cycle['decision']['conflicts'])
        self.assertEqual(self.cycle['decision']['rejected_backend_facts']['year'], 2017)

    def test_extra_only_manual_correction_needs_no_new_episode_inventory(self):
        self.cycle.update(manual_required=True, recognition_done=True)
        result = self.engine.preview_correction(self.cycle['id'], {'identity': {'media_type': 'tv'}, 'assignments': {'0': {'kind': 'extra', 'title': 'Interview'}}})
        self.assertEqual(result[0]['proposed'], 'extras/Interview.mkv')

    def audio(self, spoken=False):
        from tests.test_audio_metadata import audio_cycle
        fixture = audio_cycle(spoken)
        fixture['sources'][0]['backend_metadata']['prescan']['job_guid'] = 'guid-8'
        for source in fixture['sources']:
            source['backend_metadata']['cycle_id'] = self.cycle['id']
            path = self.path.parent / f'DETAINED_audio{source["source_id"]}.flac'
            from tests.audio_fixture import make_flac
            make_flac(path)
            source['path'] = str(path)
        self.cycle.update({k: v for k, v in fixture.items() if k != 'id'})
        self.cycle['local_done'] = True

    def test_audio_physical_mapping_no_fallback_for_inferior_visual_fields(self):
        self.audio()
        self.engine.decide(self.cycle)
        self.assertFalse(self.cycle['manual_required'])
        self.assertNotIn('fallback_attempted', self.cycle)
        self.engine.release(self.cycle)
        self.assertTrue(all(s['state'] == 'writing' for s in self.cycle['sources']))
        for source in self.cycle['sources']:
            source['state'] = 'complete'
        self.cycle['backend_done'] = True
        self.engine.release(self.cycle)
        self.assertTrue(all(s['state'] == 'released' for s in self.cycle['sources']))

    def test_spoken_work_extracts_first_then_releases_without_author(self):
        self.audio(spoken=True)
        self.cycle['backend_done'] = True
        for source in self.cycle['sources']:
            source['state'] = 'complete'
        self.engine.release(self.cycle)  # backend completion does not supply identity
        self.assertTrue(all(Path(s['path']).exists() for s in self.cycle['sources']))
        self.engine.decide(self.cycle)
        self.engine.release(self.cycle)
        self.assertTrue(all(s['state'] == 'released' for s in self.cycle['sources']))
        self.assertNotIn('fallback_attempted', self.cycle)
        self.assertNotIn('author', self.cycle['decision']['identity'])
        self.assertTrue(all(s['tagging']['audio_frames_unchanged'] for s in self.cycle['sources']))
        self.assertEqual(len(self.cycle['locals']), 2)

    def test_tagging_failure_preserves_completed_detention_and_retries(self):
        self.audio(spoken=True)
        self.engine.decide(self.cycle)
        for source in self.cycle['sources']:
            source['state'] = 'complete'
        self.cycle['backend_done'] = True
        originals = {s['path']: Path(s['path']).read_bytes() for s in self.cycle['sources']}
        with patch('recognition_helper.audio_tags.tag_completed_flac', side_effect=ValueError('tag failure')):
            self.engine.release(self.cycle)
        self.assertTrue(all(s['state'] == 'complete' and 'tag failure' in s['release_error'] for s in self.cycle['sources']))
        self.assertTrue(all(Path(p).read_bytes() == data for p,data in originals.items()))
        self.engine.release(self.cycle)
        self.assertTrue(all(s['state'] == 'released' for s in self.cycle['sources']))

    def test_unknown_optionals_no_musicbrainz_does_not_request_fallback(self):
        self.audio(spoken=True)
        self.cycle['sources'][0]['backend_metadata']['prescan'].pop('musicbrainz')
        self.cycle['backend_editorial_metadata'] = {}
        self.cycle['locals'] = [{'model':name,'findings':{'media_type':'audiobook','book':'Collected Public Talks'}} for name in ('a','b')]
        self.engine.decide(self.cycle)
        self.assertFalse(self.cycle['manual_required'])
        self.assertNotIn('fallback_attempted', self.cycle)
        self.assertTrue(all(s['proposed'] for s in self.cycle['sources']))


if __name__ == '__main__':
    unittest.main()
