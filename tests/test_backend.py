"""Pure adapter/overlay tests. No Docker, drive, camera, or real extraction."""
import importlib.util
from dataclasses import make_dataclass
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import httpx
import pytest

from recognition_helper.arm import ARM

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


helper = module('overlay_helper_test', 'backend_overlay/recognition_helper.py')
patcher = module('overlay_apply_test', 'backend_overlay/apply.py')


@pytest.fixture
def fake_backend(tmp_path, monkeypatch):
    # Legacy tiny-title adapter fixtures test lifecycle, not default TV policy.
    monkeypatch.setenv('TV_MIN_LENGTH_SECONDS', '0')
    monkeypatch.setenv('RECOGNITION_HELPER_ENABLED', '1')
    monkeypatch.setenv('RECOGNITION_HELPER_DETENTION_ROOT', str(tmp_path / 'detention'))
    session = NS(commit=lambda: None)
    package = ModuleType('arm')
    package.__path__ = []
    database = ModuleType('arm.database')
    database.db = NS(session=session)
    config_package = ModuleType('arm.config')
    config_package.__path__ = []
    config = ModuleType('arm.config.config')
    config.arm_config = {'SKIP_TRANSCODE': True}
    ripper = ModuleType('arm.ripper')
    ripper.__path__ = []
    native = ModuleType('arm.ripper.makemkv')
    exec(patcher.NATIVE_RANKER, native.__dict__)
    for name, mod in [('arm', package), ('arm.database', database),
                       ('arm.config', config_package), ('arm.config.config', config),
                       ('arm.ripper', ripper), ('arm.ripper.makemkv', native)]:
        monkeypatch.setitem(sys.modules, name, mod)
    tracks = [NS(track_number=str(n), track_id=n + 100, filename=f'old_t{n:02d}.mkv',
              length=42, chapters=1, filesize=10, source='MakeMKV', ripped=False, status=None, error=None)
              for n in (0, 2)]
    return NS(job_id=7, guid='e91d0c8a-9ae8-420a-81d7-e89bb1865be9',
              status='manual_paused', manual_start=False, manual_pause=False,
              disctype='dvd', video_type='series', multi_title=False,
              config=NS(SKIP_TRANSCODE=True, RIPMETHOD='mkv', MINLENGTH=0, MAXLENGTH=99998),
              tracks=tracks, makemkv_source='dev:/dev/fake', devpath='/dev/fake')


def test_prepare_idempotent_no_recognition_or_approval(fake_backend):
    job = fake_backend
    plan = helper.prepare_and_start(job, 'cycle-1')
    assert job.manual_start is True
    assert [s['source_id'] for s in plan['sources']] == [0, 2]
    for source in plan['sources']:
        assert Path(source['path']).name.startswith(f'DETAINED_job7_source{source["source_id"]}')
        assert Path(source['path']).parent.name == f'source-{source["source_id"]:05d}'
    sentinel = Path(plan['sources'][0]['path'])
    sentinel.write_bytes(b'writing')
    assert helper.prepare_and_start(job, 'cycle-1')['sources'] == plan['sources']
    assert sentinel.read_bytes() == b'writing'
    with pytest.raises(ValueError, match='another physical cycle'):
        helper.prepare_and_start(job, 'cycle-2')


def test_orphan_collision_is_preserved(fake_backend):
    helper.root().mkdir()
    helper.job_dir(fake_backend).mkdir()
    sentinel = helper.job_dir(fake_backend) / 'old.mkv'
    sentinel.write_bytes(b'never overwrite')
    with pytest.raises(FileExistsError):
        helper.prepare_and_start(fake_backend, 'cycle-1')
    assert sentinel.read_bytes() == b'never overwrite'


def test_managed_movie_reserves_only_native_selected_track(fake_backend):
    job=fake_backend
    job.video_type='unknown'
    job.tracks[0].length=7200
    job.tracks[0].chapters=14
    job.tracks[1].length=10
    plan=helper.prepare_and_start(job,'movie-cycle')
    assert [s['source_id'] for s in plan['sources']]==[0]
    assert plan['video_selection']['selected_ids']==[0]
    assert len(plan['video_selection']['titles'])==2
    assert job.tracks[1].process is False
    assert not hasattr(job.tracks[1],'enabled')  # never forcibly enable every row
    assert plan['sources'][0]['identified_assignment']['kind']=='movie'


def test_managed_unknown_tv_wait_does_not_mutate_selection(fake_backend):
    job=fake_backend
    job.video_type='unknown'
    for row in job.tracks:
        row.length=2600
    with pytest.raises(helper.SelectionPending):
        helper.prepare_and_start(job,'tv-cycle')
    assert job.manual_start is False
    assert not helper.managed(job)
    plan=helper.prepare_and_start(job,'tv-cycle','tv',True)
    assert [s['source_id'] for s in plan['sources']]==[0,2]


def test_failed_probe_retains_expected_and_actual_duration(tmp_path,monkeypatch):
    path=tmp_path/'partial.mkv'
    path.write_bytes(b'partial')
    source={'backend_metadata':{'scan_duration':10}}
    data={'format':{'duration':'0.125'},'streams':[{'codec_type':'video','codec_name':'h264'}]}
    monkeypatch.setattr(helper.subprocess,'run',lambda *a,**k:NS(returncode=0,stdout=json.dumps(data)))
    with pytest.raises(ValueError):
        helper.probe(path,source)
    assert source['backend_metadata']['duration_validation']=={
        'expected_seconds':10.,'actual_seconds':.125,'tolerance_seconds':5,'passed':False}


def test_reconcile_never_cleans_active_or_wrong_guid(fake_backend,monkeypatch):
    models=ModuleType('arm.models')
    job=fake_backend
    job.status='success';job.stop_time='now';job.pid=999999999
    models.Job=NS(query=NS(get=lambda n:job))
    services=ModuleType('arm.services.drives')
    cleaned=[]
    services.job_cleanup=cleaned.append
    monkeypatch.setitem(sys.modules,'arm.models',models)
    monkeypatch.setitem(sys.modules,'arm.services.drives',services)
    monkeypatch.setattr(helper,'optical_processes',lambda:[{'pid':123}])
    assert not helper.reconcile_drive(7,str(job.guid))['success']
    monkeypatch.setattr(helper,'optical_processes',lambda:[])
    assert not helper.reconcile_drive(7,'another-guid')['success']
    job.pid=__import__('os').getpid()
    assert helper.reconcile_drive(7,str(job.guid))['deferred']
    assert cleaned==[]
    job.pid=999999999
    assert helper.reconcile_drive(7,str(job.guid))['success']
    assert cleaned==['7']


def test_video_writes_prefixed_and_preserves_source_identity(fake_backend, monkeypatch):
    job = fake_backend
    helper.prepare_and_start(job, 'cycle-1')
    calls = []

    def fake_home(directory, filename):
        prefs = directory / '.makemkv-home'
        prefs.mkdir()
        (prefs / 'template').write_text(filename)
        return prefs

    def fake_run(args, types, env=None):
        number = int(args[-2])
        calls.append(number)
        assert '--minlength=0' in args
        filename = (Path(env['HOME']) / 'template').read_text()
        assert filename == f'DETAINED_job7_source{number}'
        (Path(args[-1]) / (filename + '.mkv')).write_bytes(b'closed-media')
        yield NS()

    def fake_probe(path, source, **kwargs):
        source.update(path=str(path), state='complete', finalized_at=1,
                      size_bytes=path.stat().st_size)

    monkeypatch.setattr(helper, 'settings_home', fake_home)
    monkeypatch.setattr(helper, 'probe', fake_probe)
    helper.rip_video(job, fake_run, 0, '/unused-progress')
    assert calls == [0, 2]  # source ID gap retained, not renumbered or copied from another disc
    helper.finalize_video(job)
    plan = helper.load(job)
    assert all(s['state'] == 'complete' for s in plan['sources'])
    assert all(Path(s['path']).is_file() for s in plan['sources'])
    assert not list(helper.job_dir(job).rglob('.makemkv-home'))


def test_video_failure_does_not_erase_successful_other_source(fake_backend, monkeypatch):
    job = fake_backend
    helper.prepare_and_start(job, 'cycle-1')

    def fake_home(directory, filename):
        home = directory / '.makemkv-home'
        home.mkdir()
        return home

    def fake_run(args, types, env=None):
        number = int(args[-2])
        path = Path(args[-1]) / f'DETAINED_job7_source{number}.mkv'
        path.write_bytes(b'partial' if number == 0 else b'complete')
        if number == 0:
            raise RuntimeError('extraction failed')
        return iter(())

    def fake_probe(path, source, **kwargs):
        source.update(path=str(path), state='complete', finalized_at=1, size_bytes=8)

    monkeypatch.setattr(helper, 'settings_home', fake_home)
    monkeypatch.setattr(helper, 'probe', fake_probe)
    with pytest.raises(ValueError, match='Some sources failed'):
        helper.rip_video(job, fake_run, 0, '/unused-progress')
    plan = helper.load(job)
    assert [s['state'] for s in plan['sources']] == ['failed', 'complete']
    assert Path(plan['sources'][0]['path']).read_bytes() == b'partial'


def test_audio_config_is_lossless_and_not_metadata_dependent(tmp_path):
    config, work = helper.audio_config(tmp_path)
    text = config.read_text()
    assert 'ACTIONS=read,encode' in text
    assert 'OUTPUTTYPE=flac' in text and '--verify' in text
    assert 'normalize' not in text and 'move' not in text
    assert str(work) in text


def test_audio_extracts_prefixed_lossless_sources_without_overwrite(fake_backend, monkeypatch):
    job = fake_backend
    job.disctype = 'music'
    for number, track in enumerate(job.tracks, 1):
        track.track_number = str(number)
    helper.prepare_and_start(job, 'cycle-audio')
    ripper = ModuleType('arm.ripper')
    ripper.__path__ = []
    utils = ModuleType('arm.ripper.utils')
    utils._stream_abcde_output = lambda proc, job: []
    monkeypatch.setitem(sys.modules, 'arm.ripper', ripper)
    monkeypatch.setitem(sys.modules, 'arm.ripper.utils', utils)
    invocations = []
    statuses = []

    def valid_commit():
        statuses.append(job.status)
        assert job.status in ('manual_paused', 'audio_ripping', 'success', 'fail', 'ready')

    monkeypatch.setattr(sys.modules['arm.database'].db.session, 'commit', valid_commit)

    def popen(args, **kwargs):
        number = int(args[-1])
        prefix = kwargs['env']['RH_PREFIX']
        assert prefix == f'DETAINED_job7_source{number}_'
        assert kwargs['start_new_session'] is True
        directory = Path(args[2]).parent
        work = directory / 'work' / 'abcde.discid'
        work.mkdir()
        (work / f'{prefix}track{number:02d}.wav').write_bytes(b'wave-scratch')
        (work / f'{prefix}track{number:02d}.flac').write_bytes(b'lossless')
        invocations.append(number)
        return NS(pid=999999, returncode=0, stdout=NS(close=lambda: None),
                  wait=lambda **kwargs: 0, poll=lambda: 0)

    def probe(path, source, **kwargs):
        assert kwargs['audio'] is True
        source.update(path=str(path), state='complete', finalized_at=1,
                      size_bytes=path.stat().st_size)

    monkeypatch.setattr(helper.subprocess, 'Popen', popen)
    monkeypatch.setattr(helper.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(helper.os, 'killpg', lambda *args: None)
    monkeypatch.setattr(helper, 'probe', probe)
    assert helper.rip_audio(job, 'unused-log') is True
    # Successful return is consumed by ARM main.py, which writes SUCCESS and
    # handles ejection/stop_time. No helper-side ready/idle transition is needed.
    assert job.status == 'audio_ripping'
    assert 'idle' not in statuses and 'ready' not in statuses
    assert invocations == [1, 2]
    plan = helper.load(job)
    assert all(Path(s['path']).read_bytes() == b'lossless' for s in plan['sources'])
    assert not list(helper.job_dir(job).rglob('*.wav'))
    # No retry may truncate or replace the outputs already reserved/written.
    assert helper.rip_audio(job, 'unused-log') is False
    assert job.status == 'fail'
    assert all(Path(s['expected_path']).read_bytes() == b'lossless' for s in plan['sources'])


def test_musicbrainz_hook_retains_all_candidates_before_arm_first_choice(fake_backend):
    toc = NS(id='actual-id', tracks=[NS(number=1, seconds=42, offset=150)])
    helper.capture_cd_toc(fake_backend, toc)
    releases = [{'id': rid, 'title': title, 'date': '2006-01-01',
                 'artist-credit': [{'artist': {'name': 'Performer'}}],
                 'medium-list': [{'format': 'CD', 'position': '1', 'disc-list': [{'id': 'actual-id'}],
                                  'track-list': [{'position': '1', 'recording': {'title': 'Track title', 'length': '42000'}}]}]}
                for rid, title in [('a', 'Release A'), ('b', 'Release B')]]
    helper.capture_musicbrainz(fake_backend, {'disc': {'id': 'actual-id', 'release-list': releases}})
    evidence = helper.read_evidence(fake_backend)
    assert evidence['disc_id'] == 'actual-id'
    assert len(evidence['musicbrainz']['candidates']) == 2
    for c in evidence['musicbrainz']['candidates']:
        assert c['tracks'][0]['duration_seconds'] == 42
        assert c['facts']['artist'] == 'Performer'
        assert 'author' not in c['facts']


def test_malformed_lookup_track_does_not_break_independent_extraction(fake_backend):
    helper.capture_cd_toc(fake_backend, NS(id='actual-id', tracks=[NS(number=1, seconds=42, offset=150)]))
    helper.capture_musicbrainz(fake_backend, {'disc': {'id': 'actual-id', 'release-list': [
        {'id': 'a', 'medium-list': [{'format': 'CD', 'disc-list': [{'id': 'actual-id'}],
                                   'track-list': [{'length': 'unknown', 'title': 'Visible title'}]}]}]}})
    track = helper.read_evidence(fake_backend)['musicbrainz']['candidates'][0]['tracks'][0]
    assert track['duration_seconds'] is None
    assert track['number'] is None  # never manufacture a catalogue track number


def test_make_mkv_private_settings_request_all_streams(tmp_path):
    home = helper.settings_home(tmp_path, 'DETAINED_job7_source2')
    content = (home / '.MakeMKV/settings.conf').read_text()
    assert 'app_DefaultOutputFileName = "DETAINED_job7_source2"' in content
    assert 'app_DefaultSelectionString = "+sel:all"' in content


def adapter_fixture(tmp_path, status='manual_paused', stop_time=None, ripping_enabled=False):
    settings = NS(arm_url='http://arm', arm_token='', drive='/dev/sr0',
                  arm_media='/home/arm/media', media=tmp_path,
                  detention=tmp_path / 'detention')
    path = '/home/arm/media/detention/job-7-guid/source-00002/DETAINED_job7_source2.mkv'
    item = {'source_id': 2, 'arm_track_id': 102, 'path': path,
            'state': 'complete', 'finalized_at': 1, 'size_bytes': 5}
    plan = {'job_guid': 'guid', 'cycle_id': 'cycle-1', 'overlay_version': helper.VERSION,
            'sources': [item]}
    detail = {'job': {'job_id': 7, 'devpath': '/dev/sr0', 'status': status,
                      'stop_time': stop_time}, 'tracks': []}
    requests = []
    ripping = {'enabled': ripping_enabled}

    def respond(request):
        requests.append(request)
        if request.url.path.endswith('/detail'):
            return httpx.Response(200, json=detail)
        if request.url.path.endswith('/drives'):
            return httpx.Response(200, json={'drives': [{'drive_id': 1, 'mount': '/dev/sr0',
                                                        'job_id_current': None}]})
        if request.url.path.endswith('/eject'):
            return httpx.Response(200, json={'success': True})
        if request.url.path.endswith('/system/ripping-enabled'):
            if request.method == 'POST':
                ripping['enabled'] = json.loads(request.content)['enabled']
            return httpx.Response(200, json={'ripping_enabled': ripping['enabled']})
        if request.url.path.endswith('/recognition-helper/capabilities'):
            return httpx.Response(200, json={'enabled': True})
        return httpx.Response(200, json=plan)

    return ARM(settings, transport=httpx.MockTransport(respond)), plan, detail, requests


def test_adapter_prepare_and_real_completion_boundary(tmp_path):
    adapter, plan, detail, requests = adapter_fixture(tmp_path)
    sources = adapter.prepare_and_start(7, 'cycle-1')
    path = Path(sources[0]['path'])
    path.parent.mkdir(parents=True)
    path.write_bytes(b'media')
    assert sources[0]['backend_metadata']['job_identifiers']['job_id'] == 7
    detail['job']['status'] = 'success'
    assert adapter.completion(7, sources)['done'] is False
    assert adapter.completion(7, sources)['sources'][0]['state'] == 'awaiting_finalization'
    detail['job']['stop_time'] = '2026-10-23T00:00:00'
    assert adapter.completion(7, sources)['sources'][0]['state'] == 'complete'
    path.write_bytes(b'changed size')
    assert adapter.completion(7, sources)['sources'][0]['state'] == 'failed'


def test_adapter_rejects_late_other_cycle(tmp_path):
    adapter, plan, detail, _ = adapter_fixture(tmp_path)
    sources = adapter.prepare_and_start(7, 'cycle-1')
    plan['cycle_id'] = 'cycle-2'
    with pytest.raises(ValueError, match='association changed'):
        adapter.completion(7, sources)


def test_adapter_tray_exactly_one_backend_command(tmp_path):
    adapter, _, _, requests = adapter_fixture(tmp_path)
    adapter.tray('close')
    posts = [r for r in requests if r.method == 'POST']
    assert len(posts) == 1
    assert json.loads(posts[0].content) == {'method': 'close'}
    assert not any(r.url.path.endswith('/scan') for r in requests)


def test_close_automatically_establishes_scan_pause_before_drive_command(tmp_path):
    adapter, _, _, requests = adapter_fixture(tmp_path, ripping_enabled=True)
    adapter.tray('close')
    posts = [r for r in requests if r.method == 'POST']
    assert [r.url.path.rsplit('/', 1)[-1] for r in posts] == ['ripping-enabled', 'eject']
    assert json.loads(posts[0].content) == {'enabled': False}


def test_adapter_path_escape_and_unprefixed_output(tmp_path):
    adapter, _, _, _ = adapter_fixture(tmp_path)
    for path in ['/etc/passwd', '/home/arm/media/detention/../released/DETAINED_x.mkv',
                 '/home/arm/media/detention/raw.mkv']:
        with pytest.raises(ValueError):
            adapter._local(path)


def test_overlay_rejects_unknown_base_without_modification(tmp_path):
    for relative in patcher.BASE_HASHES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('unknown-source')
    abcde = tmp_path / 'abcde'
    abcde.write_text('unknown')
    with pytest.raises(ValueError, match='Unsupported installed source'):
        patcher.apply(tmp_path, abcde)
    assert all((tmp_path / p).read_text() == 'unknown-source' for p in patcher.BASE_HASHES)


def scan_messages():
    cinfo = make_dataclass('CInfo', [('id', int), ('code', int), ('value', str)])
    tinfo = make_dataclass('TInfo', [('id', int), ('code', int), ('value', str), ('tid', int)])
    sinfo = make_dataclass('SInfo', [('id', int), ('code', int), ('value', str), ('tid', int), ('sid', int)])
    return [cinfo(2, 0, 'Disc label'), tinfo(9, 0, '0:00:42', 2),
            sinfo(1, 6202, 'Audio', 2, 1), sinfo(3, 0, 'eng', 2, 1),
            sinfo(5, 0, 'A_AC3', 2, 1), sinfo(14, 0, '6', 2, 1),
            sinfo(1, 6203, 'Subtitle', 2, 2), sinfo(3, 0, 'ger', 2, 2),
            sinfo(5, 0, 'S_HDMV/PGS', 2, 2)]


def test_existing_prescan_iterator_retained_and_source_ids_associated(fake_backend):
    seen = []
    messages = scan_messages()

    def existing_scan():
        for message in messages:
            seen.append(message)
            yield message

    assert list(helper.capture_prescan(fake_backend, existing_scan())) == messages
    assert seen == messages  # exactly the supplied iterator, no second scan
    evidence = helper.read_evidence(fake_backend)
    assert evidence['status'] == 'completed'
    assert evidence['disc_attributes']['2']['value'] == 'Disc label'
    plan = helper.prepare_and_start(fake_backend, 'cycle-1')
    assert plan['prescan']['kind'] == 'MakeMKV robot prescan'
    assert plan['sources'][0]['backend_metadata']['source_streams'] == []
    source = plan['sources'][1]
    assert source['source_id'] == 2 and source['arm_track_id'] == 102
    streams = source['backend_metadata']['source_streams']
    assert [(s['stream_id'], s['kind']) for s in streams] == [(1, 'audio'), (2, 'subtitle')]
    assert streams[0]['codec'] == 'A_AC3'
    assert streams[0]['channels'] == '6'
    assert source['backend_metadata']['title_attributes']['9']['value'] == '0:00:42'


def test_interrupted_prescan_is_retained_not_falsely_completed(fake_backend):
    def interrupted():
        yield from scan_messages()
        raise RuntimeError('scan interrupted')

    with pytest.raises(RuntimeError, match='interrupted'):
        list(helper.capture_prescan(fake_backend, interrupted()))
    evidence = helper.read_evidence(fake_backend)
    assert evidence['status'] == 'interrupted'
    assert '2' in evidence['sources']


def test_disabled_scan_hooks_pass_through_without_evidence(fake_backend, monkeypatch):
    monkeypatch.setenv('RECOGNITION_HELPER_ENABLED', '0')
    messages = scan_messages()
    assert list(helper.capture_prescan(fake_backend, iter(messages))) == messages
    helper.capture_cd_toc(fake_backend, None)
    assert not helper.root().exists()


def test_scan_evidence_identity_and_truncation(fake_backend):
    messages = scan_messages()
    messages[0].value = 'x' * 5000
    list(helper.capture_prescan(fake_backend, iter(messages)))
    evidence = helper.read_evidence(fake_backend)
    assert evidence['truncated'] is True
    assert len(evidence['disc_attributes']['2']['value']) == 4096
    evidence['job_guid'] = 'another job'
    helper.evidence_path(fake_backend).write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match='identity mismatch'):
        helper.read_evidence(fake_backend)


def observed_source(fake_backend):
    list(helper.capture_prescan(fake_backend, iter(scan_messages())))
    return helper.prepare_and_start(fake_backend, 'cycle-1')['sources'][1]


def test_stream_comparison_no_false_loss_for_missing_tags(fake_backend):
    source = observed_source(fake_backend)
    result = helper.compare_streams(source, {'streams': [
        {'index': 1, 'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6},
        {'index': 2, 'codec_type': 'subtitle', 'codec_name': 'hdmv_pgs_subtitle'}]})
    assert result['status'] == 'no_observed_shortfall'
    assert result['demonstrated_missing_streams'] == []
    assert result['descriptor_disagreements'] == []
    assert any('tags missing' in u for u in result['uncertainty'])


def test_stream_comparison_shortfall_separate_from_uncertainty(fake_backend):
    source = observed_source(fake_backend)
    result = helper.compare_streams(source, {'streams': [
        {'codec_type': 'audio', 'codec_name': 'ac3', 'tags': {'language': 'eng'}}]})
    assert result['status'] == 'observed_shortfall'
    assert result['demonstrated_missing_streams'] == [
        {'kind': 'subtitle', 'minimum_missing_count': 1,
         'evidence': 'fewer output streams than individually observed source stream IDs'}]
    assert result['uncertainty']  # does not claim missing unique content/full inventory


def test_descriptor_disagreement_is_not_invented_missing_stream(fake_backend):
    source = observed_source(fake_backend)
    result = helper.compare_streams(source, {'streams': [
        {'codec_type': 'audio', 'codec_name': 'aac', 'tags': {'language': 'fra'}},
        {'codec_type': 'subtitle', 'codec_name': 'hdmv_pgs_subtitle', 'tags': {'language': 'deu'}}]})
    assert result['demonstrated_missing_streams'] == []
    assert {r['field'] for r in result['descriptor_disagreements']} == {'codec', 'language'}
    assert not any(r['kind'] == 'subtitle' for r in result['descriptor_disagreements'])  # ger=deu


def test_old_overlay_stream_inventory_explicitly_unknown():
    result = helper.compare_streams({'backend_metadata': {}}, {'streams': []})
    assert result['status'] == 'unknown'
    assert not result['demonstrated_missing_streams']
    assert any('absent' in u for u in result['uncertainty'])


def test_probe_retains_source_output_comparison(fake_backend, monkeypatch):
    source = observed_source(fake_backend)
    path = Path(source['path'])
    path.write_bytes(b'closed')
    output = {'streams': [{'codec_type': 'video'}, {'codec_type': 'audio', 'codec_name': 'ac3'}],
              'format': {'duration': '42'}}
    monkeypatch.setattr(helper.subprocess, 'run', lambda *a, **kw: NS(stdout=json.dumps(output)))
    helper.probe(path, source)
    assert source['state'] == 'complete'
    assert source['backend_metadata']['stream_comparison']['status'] == 'observed_shortfall'
    assert source['backend_metadata']['output_probe'] == output


def test_cd_mb_rows_are_not_misrepresented_as_toc_without_evidence(fake_backend):
    fake_backend.disctype = 'music'
    for number, track in enumerate(fake_backend.tracks, 1):
        track.track_number = str(number)
        track.source = 'MusicBrainz'
    plan = helper.prepare_and_start(fake_backend, 'cycle-audio')
    assert plan['audio_reconciliation']['status'] == 'unavailable'
    for source in plan['sources']:
        assert source['backend_metadata']['scan_source'] == 'MusicBrainz'
        assert source['backend_metadata']['source_number_kind'] != 'CD TOC track number'


def test_cd_toc_reconciles_mb_numbers_and_uses_physical_duration(fake_backend, monkeypatch):
    job = fake_backend
    job.disctype = 'music'
    for number, track in zip((1, 3), job.tracks):
        track.track_number = str(number)
        track.source = 'MusicBrainz'
    toc = NS(id='physical-disc-id', tracks=[NS(number=n, seconds=45, offset=150 + n * 3375) for n in (1, 2)])
    helper.capture_cd_toc(job, toc)
    utils = ModuleType('arm.ripper.utils')
    ripper = ModuleType('arm.ripper')
    ripper.__path__ = []

    def put_track(job, number, seconds, *args, **kwargs):
        job.tracks.append(NS(track_number=str(number), length=seconds, track_id=200 + number,
                             source='TOC', filename=f'Track {number}.flac'))

    utils.put_track = put_track
    monkeypatch.setitem(sys.modules, 'arm.ripper', ripper)
    monkeypatch.setitem(sys.modules, 'arm.ripper.utils', utils)
    plan = helper.prepare_and_start(job, 'cycle-audio')
    assert [s['source_id'] for s in plan['sources']] == [1, 2]
    assert plan['audio_reconciliation']['added_physical_numbers'] == [2]
    assert plan['audio_reconciliation']['excluded_nonphysical_rows'][0]['number'] == 3
    assert plan['audio_reconciliation']['duration_disagreements'][0]['number'] == 1
    assert plan['sources'][0]['backend_metadata']['scan_source'] == 'MusicBrainz'
    assert plan['sources'][0]['backend_metadata']['scan_duration'] == 45
    assert plan['sources'][0]['backend_metadata']['arm_row_duration'] == 42
    assert plan['sources'][0]['backend_metadata']['physical_toc_track']['number'] == 1
    assert plan['sources'][0]['backend_metadata']['source_number_kind'] == 'CD TOC track number'


def test_adapter_metadata_uses_existing_read_only_endpoint(tmp_path):
    adapter, _, _, requests = adapter_fixture(tmp_path)
    adapter.metadata(7)
    assert len(requests) == 1
    assert requests[0].method == 'GET'
    assert requests[0].url.path == '/api/v1/jobs/7/metadata'


def test_adapter_old_manifest_remains_compatible(tmp_path):
    adapter, plan, _, _ = adapter_fixture(tmp_path)
    sources = adapter.prepare_and_start(7, 'cycle-1')
    assert sources[0]['backend_metadata']['prescan']['status'] == 'unavailable'


def test_prescan_retry_replaces_previous_attempt_not_another_job(fake_backend):
    list(helper.capture_prescan(fake_backend, iter(scan_messages())))
    list(helper.capture_prescan(fake_backend, iter(())))
    assert helper.read_evidence(fake_backend)['sources'] == {}
    fake_backend.guid = 'bdbed6a1-5ecd-46bf-a164-b3fe9a61700d'
    assert helper.read_evidence(fake_backend)['status'] == 'unavailable'


def test_cd_duplicate_rows_not_silently_chosen(fake_backend):
    fake_backend.disctype = 'music'
    for track in fake_backend.tracks:
        track.track_number = '1'
        track.source = 'MusicBrainz'
    with pytest.raises(ValueError, match='duplicate source numbers'):
        helper.prepare_and_start(fake_backend, 'cycle-audio')


def test_partial_extraction_messages_survive_failure(fake_backend, monkeypatch):
    helper.prepare_and_start(fake_backend, 'cycle-1')
    msg = make_dataclass('MakeMKVMessage', [('code', int), ('message', str)])

    def run(*args, **kwargs):
        yield msg(5003, 'Failed to save source')
        raise RuntimeError('process failed')

    monkeypatch.setattr(helper, 'settings_home', lambda directory, name: directory)
    with pytest.raises(ValueError, match='Some sources failed'):
        helper.rip_video(fake_backend, run, 0, '/unused-progress')
    for source in helper.load(fake_backend)['sources']:
        assert source['backend_metadata']['extraction_messages'][0]['code'] == 5003
