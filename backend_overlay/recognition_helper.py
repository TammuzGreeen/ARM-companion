"""Small, opt-in ARM Neu extraction hook; no recognition or release policy here."""
import dataclasses
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import subprocess
import tempfile
import time
from uuid import UUID

try:
    from arm.ripper.recognition_helper_selection import select as select_video, SelectionPending, settings as movie_settings
except ImportError:  # software tests outside installed ARM
    from backend_overlay.video_selection import select as select_video, SelectionPending, settings as movie_settings

VERSION = 'recognition-helper-detention-v7'


def evidence_path(job):
    # Separate from the exclusive output reservation, which happens AFTER scan.
    return root() / '.prescan' / f'job-{int(job.job_id)}-{UUID(str(job.guid))}.json'


def write_evidence(job, value):
    directory = evidence_path(job).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o2770)
    fd, name = tempfile.mkstemp(prefix='.scan-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o660)
            json.dump(dict(value, job_id=job.job_id, job_guid=str(job.guid)), output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, evidence_path(job))
        sync_directory(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_evidence(job):
    try:
        value = json.loads(evidence_path(job).read_text())
    except FileNotFoundError:
        return {'status': 'unavailable', 'limitation': 'No retained scan evidence (older overlay or scan not captured)'}
    if value.get('job_id') != job.job_id or value.get('job_guid') != str(job.guid):
        raise ValueError('Prescan evidence job identity mismatch')
    return value


def capture_prescan(job, messages):
    """Tee the EXISTING scan iterator; never run MakeMKV or query the drive."""
    if not enabled():
        yield from messages
        return
    value = {'kind': 'MakeMKV robot prescan', 'started_at': time.time(),
             'status': 'interrupted', 'disc_attributes': {}, 'sources': {},
             'truncated': False, 'inventory_scope': 'observed MakeMKV titles/streams, not disc filesystem'}
    count = 0
    try:
        for message in messages:
            kind = type(message).__name__
            if kind in ('CInfo', 'TInfo', 'SInfo'):
                # Compact equivalent of CINFO/TINFO/SINFO: keep numeric attribute
                # IDs, codes and values, not repeated raw robot-line scaffolding.
                if count < 50000:
                    attrs = value['disc_attributes']
                    if kind != 'CInfo':
                        source = value['sources'].setdefault(str(message.tid),
                                                            {'title_attributes': {}, 'streams': {}})
                        attrs = source['title_attributes']
                        if kind == 'SInfo':
                            attrs = source['streams'].setdefault(str(message.sid), {})
                    text = str(message.value)
                    attrs[str(message.id)] = {'code': int(message.code), 'value': text[:4096]}
                    value['truncated'] |= len(text) > 4096
                    count += 1
                else:
                    value['truncated'] = True
            elif kind == 'Titles':
                value['reported_title_count'] = int(message.count)
            yield message
        value['status'] = 'completed'
    finally:
        value['finished_at'] = time.time()
        write_evidence(job, value)


def capture_cd_toc(job, toc):
    """Retain the libdiscid object ARM already read, before MB replaces rows."""
    if not enabled() or getattr(job, 'job_id', None) is None:
        return
    tracks = [{'number': int(t.number), 'duration_seconds': float(t.seconds),
               'offset_frames': int(t.offset)} for t in toc.tracks]
    write_evidence(job, {'kind': 'physical CD TOC via ARM libdiscid read',
                        'status': 'completed', 'captured_at': time.time(),
                        'disc_id': str(toc.id), 'tracks': tracks,
                         'inventory_scope': 'libdiscid audio TOC tracks; not data files or hidden pregap audio'})


def capture_musicbrainz(job, response):
    """Retain ALL lookup candidates before ARM chooses its first CD release.

    This tees the existing lookup, never performs another network/drive request.
    Artist credits are performers, not authors. Stubs are not release evidence.
    """
    if not enabled():
        return
    evidence = read_evidence(job)
    disc = response.get('disc', {}) if isinstance(response, dict) else {}
    candidates = []
    for release in disc.get('release-list', []):
        credits = release.get('artist-credit', [])
        artist = ''.join(c if isinstance(c, str) else
                         c.get('name', c.get('artist', {}).get('name', '')) + c.get('joinphrase', '')
                         for c in credits)
        for medium in release.get('medium-list', []):
            tracks = []
            for track in medium.get('track-list', []):
                recording = track.get('recording', {})
                length = track.get('length', recording.get('length'))
                try:
                    seconds = float(length) / 1000 if length is not None else None
                except (ValueError, TypeError):
                    seconds = None
                tracks.append({'number': track.get('position', track.get('number')),
                               # Match ARM's recording-title convention, retaining
                               # release-specific labels separately for auditing.
                               'title': recording.get('title', track.get('title')),
                               'release_track_title': track.get('title'),
                               'recording_id': recording.get('id'),
                               'duration_seconds': seconds})
            candidates.append({'release_id': release.get('id'), 'format': medium.get('format'),
                               'disc_ids': [d.get('id') for d in medium.get('disc-list', [])],
                               'medium_discs': [{k: d.get(k) for k in ('id', 'offset-list', 'sectors')}
                                                for d in medium.get('disc-list', [])],
                               'facts': {'album': release.get('title'), 'artist': artist or None,
                                         'year': str(release.get('date', ''))[:4] or None,
                                         'disc': medium.get('position'),
                                         'disc_total': len(release.get('medium-list', [])) or None},
                               'tracks': tracks})
    evidence['musicbrainz'] = {'lookup_disc_id': disc.get('id'),
                              'lookup_offsets': disc.get('offset-list'),
                              'status': 'release_candidates' if candidates else 'unavailable_or_stub',
                              'candidates': candidates,
                              'provenance': 'existing ARM MusicBrainz disc-ID response, before first-release selection'}
    write_evidence(job, evidence)


def stream_descriptors(scan_source):
    result = []
    for sid, attrs in sorted(scan_source.get('streams', {}).items(), key=lambda item: int(item[0])):
        def field(number):
            return attrs.get(str(number), {}).get('value')
        # MakeMKV robot type codes, independent of translated display strings.
        kind = {6201: 'video', 6202: 'audio', 6203: 'subtitle'}.get(attrs.get('1', {}).get('code'))
        result.append({'stream_id': int(sid), 'kind': kind, 'language': field(3),
                       'codec': field(5) or field(6), 'channels': field(14),
                       'attributes': attrs})
    return result


def compare_streams(source, output):
    """Compare observed counts; missing tags/unknown descriptors are not loss."""
    metadata = source['backend_metadata']
    observed = metadata.get('source_streams', [])
    scan = metadata.get('source_scan', {})
    actual = [{'index': s.get('index'), 'kind': s.get('codec_type'),
               'codec': s.get('codec_name'), 'channels': s.get('channels'),
               'language': s.get('tags', {}).get('language')}
              for s in output.get('streams', []) if s.get('codec_type') in ('audio', 'subtitle')]
    missing, uncertainty, counts, disagreements = [], [], {}, []
    if scan.get('status') != 'completed' or scan.get('truncated') or not observed:
        uncertainty.append('Source stream enumeration absent, interrupted, truncated or empty')
    if any(s.get('kind') is None for s in observed):
        uncertainty.append('Some observed source streams have unknown type codes')
    for kind in ('audio', 'subtitle'):
        before = sum(s.get('kind') == kind for s in observed)
        after = sum(s['kind'] == kind for s in actual)
        counts[kind] = {'observed_source': before, 'final_output': after}
        if before > after:
            missing.append({'kind': kind, 'minimum_missing_count': before - after,
                            'evidence': 'fewer output streams than individually observed source stream IDs'})
        # Compare only recognized codec IDs. Unknown names and absent language
        # tags are uncertainty, never invented losses or language-selection rules.
        codecs = {'A_AC3': 'ac3', 'A_EAC3': 'eac3', 'A_DTS': 'dts',
                  'A_TRUEHD': 'truehd', 'A_AAC': 'aac', 'A_FLAC': 'flac',
                  'S_HDMV/PGS': 'hdmv_pgs_subtitle',
                  'S_VOBSUB': 'dvd_subtitle', 'S_TEXT/UTF8': 'subrip'}
        expected = [codecs.get(s.get('codec')) for s in observed if s.get('kind') == kind]
        delivered = [s.get('codec') for s in actual if s['kind'] == kind]
        for codec in sorted({c for c in expected if c}):
            if expected.count(codec) > delivered.count(codec):
                disagreements.append({'kind': kind, 'field': 'codec', 'value': codec,
                                      'observed_source': expected.count(codec),
                                      'final_output': delivered.count(codec)})
        if any(c is None for c in expected):
            uncertainty.append(f'Some {kind} source codec IDs are unavailable/unrecognized')
        aliases = {'en': 'eng', 'de': 'deu', 'ger': 'deu', 'fr': 'fra', 'fre': 'fra',
                   'es': 'spa', 'it': 'ita', 'ja': 'jpn', 'zh': 'zho', 'chi': 'zho',
                   'nl': 'nld', 'dut': 'nld', 'alb': 'sqi', 'arm': 'hye', 'baq': 'eus',
                   'bur': 'mya', 'cze': 'ces', 'geo': 'kat', 'gre': 'ell', 'ice': 'isl',
                   'mac': 'mkd', 'mao': 'mri', 'may': 'msa', 'per': 'fas', 'rum': 'ron',
                   'slo': 'slk', 'tib': 'bod', 'wel': 'cym'}
        def language(raw):
            text = str(raw or '').lower().strip()
            return None if text in ('', 'und') else aliases.get(text, text)
        expected_languages = [language(s.get('language')) for s in observed if s.get('kind') == kind]
        delivered_languages = [language(s.get('language')) for s in actual if s['kind'] == kind]
        if None in expected_languages:
            uncertainty.append(f'Some {kind} source language tags are missing/undetermined')
        if None not in delivered_languages:
            for lang in sorted({v for v in expected_languages if v}):
                if expected_languages.count(lang) > delivered_languages.count(lang):
                    disagreements.append({'kind': kind, 'field': 'language', 'value': lang,
                                          'observed_source': expected_languages.count(lang),
                                          'final_output': delivered_languages.count(lang)})
    if any(not s.get('language') or s.get('language') == 'und' for s in actual):
        uncertainty.append('Output language tags missing/undetermined; not evidence of a missing stream')
    uncertainty.append('Counts do not prove descriptor/content equivalence or complete disc inventory; '
                       'consult retained attributes and extraction skip messages (including identical/empty streams)')
    return {'status': 'observed_shortfall' if missing else ('no_observed_shortfall' if observed else 'unknown'),
            'counts': counts, 'demonstrated_missing_streams': missing,
            'descriptor_disagreements': disagreements,
            'uncertainty': uncertainty, 'output_descriptors': actual,
            'method': 'observed audio/subtitle source-ID counts versus finalized ffprobe; no language filtering'}


def reconcile_cd_tracks(job, tracks, evidence):
    """Use actual TOC when retained; reuse ARM's track writer for missing rows."""
    toc = evidence.get('tracks') if evidence.get('kind') == 'physical CD TOC via ARM libdiscid read' else None
    if not toc:
        return tracks, {'status': 'unavailable', 'limitation': 'No retained physical TOC; ARM rows may be MusicBrainz-derived'}
    physical = {int(t['number']): t for t in toc}
    rows = {int(t.track_number): t for t in tracks}
    missing = sorted(physical.keys() - rows.keys())
    excluded = [{'arm_track_id': t.track_id, 'number': int(t.track_number), 'source': t.source}
                for t in tracks if int(t.track_number) not in physical]
    if missing:
        from arm.ripper import utils
        for number in missing:
            utils.put_track(job, number, int(physical[number]['duration_seconds']), 'n/a', 0.1,
                            False, 'TOC', f'Track {number}.flac', title=f'Track {number}')
        rows = {int(t.track_number): t for t in job.tracks}
    return [rows[n] for n in sorted(physical)], {
        'status': 'reconciled', 'physical_tracks': toc, 'added_physical_numbers': missing,
        'excluded_nonphysical_rows': excluded,
        'duration_disagreements': [{'number': n, 'arm_row_seconds': rows[n].length,
                                    'physical_seconds': physical[n]['duration_seconds']}
                                   for n in sorted(physical) if rows[n].length is not None and
                                   abs(float(rows[n].length) - physical[n]['duration_seconds']) > 2]}


def enabled():
    return os.getenv('RECOGNITION_HELPER_ENABLED', '') == '1'


def root():
    return Path(os.getenv('RECOGNITION_HELPER_DETENTION_ROOT', '/home/arm/media/detention'))


def job_dir(job):
    return root() / f'job-{int(job.job_id)}-{UUID(str(job.guid))}'


def manifest_path(job):
    return job_dir(job) / '.recognition-helper.json'


def managed(job):
    return enabled() and manifest_path(job).is_file()


def load(job):
    value = json.loads(manifest_path(job).read_text())
    if value['job_guid'] != str(job.guid) or value['job_id'] != job.job_id:
        raise ValueError('Detention manifest job identity mismatch')
    runtime = job_dir(job) / '.extraction-progress.json'
    if runtime.is_file():
        value['runtime'] = json.loads(runtime.read_text())
    return value


def save(job, value):
    directory = job_dir(job)
    fd, name = tempfile.mkstemp(prefix='.manifest-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o660)
            json.dump(value, output, ensure_ascii=False, default=str)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, manifest_path(job))
        sync_directory(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def capabilities():
    return {'overlay_version': VERSION, 'enabled': enabled(), 'detention_root': str(root()),
            'video': 'native ARM ranked/filtered selected titles; +sel:all streams; no transcode',
            'movie_selection': movie_settings(),
            'tv_selection': {'minimum_seconds': movie_settings()['tv_minimum_seconds'],
                             'short_episode_exceptions': 'exact disc label and full scan signature with established episodes'},
            'audio': 'per-source abcde/cdparanoia to FLAC; no normalization',
            'finalization': 'manifest finalized_at plus terminal ARM job stop_time',
            'evidence_hooks': {'video_prescan': 'existing CINFO/TINFO/SINFO iterator retained',
                               'cd_toc': 'existing libdiscid read retained and reconciled',
                               'streams': 'observed source/output count comparison with explicit uncertainty',
                               'metadata': 'GET /api/v1/jobs/{job_id}/metadata'}}


def prepare_and_start(job, cycle_id, content_kind=None, recognition_finished=False, tv_context=None):
    from arm.database import db
    import arm.config.config as cfg
    if not enabled():
        raise ValueError('Recognition-helper overlay is disabled')
    if not isinstance(cycle_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', cycle_id):
        raise ValueError('Invalid cycle ID')
    root().mkdir(parents=True, exist_ok=True, mode=0o2770)
    lockpath = root() / f'.job-{job.job_id}-{UUID(str(job.guid))}.lock'
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if manifest_path(job).exists():
            plan = load(job)
            if plan['cycle_id'] != cycle_id:
                raise ValueError('Job already belongs to another physical cycle')
            # Recovery after a lost HTTP response or a process interruption:
            # signal only the same still-waiting backend thread, never reset outputs.
            if job.status == 'manual_paused' and not job.manual_start:
                job.manual_pause = False
                job.manual_start = True
                db.session.commit()
            return plan
        if job.status != 'manual_paused' or job.manual_start:
            raise ValueError('A fresh prescanned waiting job is required')
        if job.disctype not in ('dvd', 'bluray', 'bluray4k', 'music'):
            raise ValueError('This extraction hook supports video discs and audio CDs only')
        skip = getattr(job.config, 'SKIP_TRANSCODE', None)
        if skip is None:
            skip = cfg.arm_config.get('SKIP_TRANSCODE', False)
        if str(skip).lower() not in ('true', '1'):
            raise ValueError('SKIP_TRANSCODE must be enabled')
        if job.disctype != 'music' and job.config.RIPMETHOD != 'mkv':
            raise ValueError('Video extraction requires RIPMETHOD=mkv')
        tracks = sorted(list(job.tracks), key=lambda t: int(t.track_number))
        if len({int(t.track_number) for t in tracks}) != len(tracks):
            raise ValueError('Scan has duplicate source numbers; refusing arbitrary ARM row association')
        evidence = read_evidence(job)
        audio_reconciliation = None
        if job.disctype == 'music':
            tracks, audio_reconciliation = reconcile_cd_tracks(job, tracks, evidence)
        video_selection = None
        if job.disctype != 'music':
            from arm.ripper.makemkv import rank_mainfeature_tracks
            if int(os.getenv('VIDEO_STALL_SECONDS', '1800')) < 60:
                raise ValueError('VIDEO_STALL_SECONDS must be at least 60')
            try:
                tracks, video_selection = select_video(job, tracks, rank_mainfeature_tracks,
                                                        content_kind, recognition_finished, tv_context, evidence)
            except SelectionPending as exc:
                evidence['video_selection'] = exc.report
                write_evidence(job, evidence)
                raise
            selected_ids = {int(t.track_number) for t in tracks}
            for row in job.tracks:
                if int(row.track_number) not in selected_ids:
                    row.process = False
                    # Native SkipReason enum, not an invented job status.
                    row.skip_reason = 'user_disabled'
        ids = [int(t.track_number) for t in tracks]
        if (not ids and video_selection is None) or len(set(ids)) != len(ids) or (ids and min(ids) < (1 if job.disctype == 'music' else 0)):
            raise ValueError('Scan has no stable, unique source numbers')
        # mkdir, not exist_ok: preserve any orphaned reservation or existing media.
        job_dir(job).mkdir(mode=0o2770)
        job_dir(job).chmod(0o2770)
        sources = []
        extension = '.flac' if job.disctype == 'music' else '.mkv'
        for track in tracks:
            number = int(track.track_number)
            scanned = evidence.get('sources', {}).get(str(number), {})
            toc_track = next((t for t in evidence.get('tracks', []) if t['number'] == number), None)
            directory = job_dir(job) / f'source-{number:05d}'
            directory.mkdir(mode=0o2770)
            directory.chmod(0o2770)
            path = directory / f'DETAINED_job{job.job_id}_source{number}{extension}'
            sources.append({'source_id': number, 'arm_track_id': track.track_id,
                            'path': str(path), 'expected_path': str(path), 'state': 'writing',
                            'backend_metadata': {'source_number_kind':
                             ('CD TOC track number' if toc_track or track.source == 'TOC' else
                              'ARM audio track number (physical TOC not retained)') if
                             job.disctype == 'music' else 'MakeMKV title ID (not DVD title number)',
                             'scan_source': getattr(track, 'source', None),
                              'scan_filename': track.filename,
                              'scan_title': getattr(track, 'title', None),
                             'arm_row_duration': track.length,
                             'scan_duration': toc_track['duration_seconds'] if toc_track else track.length,
                             'physical_toc_track': toc_track,
                             'source_scan': {'status': evidence.get('status'),
                                             'truncated': evidence.get('truncated', False),
                                             'provenance': evidence.get('kind', evidence.get('limitation'))},
                             'title_attributes': scanned.get('title_attributes', {}),
                              'source_streams': stream_descriptors(scanned)}})
            if video_selection is not None:
                track.process = True  # only selected, previously eligible rows
                track.skip_reason = None
                sources[-1]['backend_metadata']['video_selection'] = video_selection
                association = video_selection.get('verified_associations', {}).get(str(number))
                if association:
                    if association['kind'] == 'extra':
                        sources[-1].update(is_extra=True, extra_identity=association['title'])
                    else:
                        sources[-1]['identified_assignment'] = association
                if video_selection['selection_category'] != 'tv' and len(tracks) == 1:
                    sources[-1]['identified_assignment'] = {'kind': 'movie',
                        'evidence': video_selection['reason']}
            if job.disctype == 'music':
                track.enabled = True
                track.process = True
                track.skip_reason = None
        plan = {'overlay_version': VERSION, 'job_id': job.job_id, 'job_guid': str(job.guid),
                'cycle_id': cycle_id, 'disctype': job.disctype,
                'created_at': time.time(), 'sources': sources,
                'enumeration': 'ARM prescan tracks; not a complete disc filesystem inventory'}
        plan['prescan'] = {k: v for k, v in evidence.items() if k != 'sources'}
        if video_selection is not None:
            plan['video_selection'] = video_selection
        if audio_reconciliation is not None:
            plan['audio_reconciliation'] = audio_reconciliation
        save(job, plan)
        job.manual_pause = False
        job.manual_start = True
        db.session.commit()
        return plan
    finally:
        os.close(fd)


def probe(path, source, *, audio=False):
    """Inspect the closed actual file, without claiming a complete source inventory."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError('Extractor produced no regular nonempty output')
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                             '-of', 'json', str(path)], capture_output=True, timeout=60, check=True)
    data = json.loads(result.stdout)
    source['backend_metadata']['output_probe'] = data
    streams = data.get('streams', [])
    if not any(s.get('codec_type') == ('audio' if audio else 'video') for s in streams):
        raise ValueError('Final file has no required media stream')
    duration = float(data.get('format', {}).get('duration') or 0)
    expected = float(source['backend_metadata'].get('scan_duration') or 0)
    source['backend_metadata']['duration_validation'] = {
        'expected_seconds': expected, 'actual_seconds': duration,
        'tolerance_seconds': max(5, expected * .03), 'passed': duration > 0 and
        (not expected or abs(duration - expected) <= max(5, expected * .03))}
    if duration <= 0 or (expected and abs(duration - expected) > max(5, expected * 0.03)):
        raise ValueError('Final duration disagrees with scanned source')
    if audio and any(s.get('codec_type') == 'audio' and s.get('codec_name') != 'flac' for s in streams):
        raise ValueError('Audio output is not lossless FLAC')
    with path.open('rb') as stream:
        os.fsync(stream.fileno())
    sync_directory(path.parent)
    source.update(path=str(path), size_bytes=path.stat().st_size, state='complete',
                  finalized_at=time.time())
    source['backend_metadata']['output_probe'] = data
    if not audio:
        source['backend_metadata']['stream_comparison'] = compare_streams(source, data)


def source_result(job, plan, source, track, error=None):
    from arm.database import db
    if error:
        source.update(state='failed', error=str(error)[:1000], finalized_at=None)
        directory = Path(source['expected_path']).parent
        retained = []
        for path in sorted(directory.rglob('*')):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in ('.mkv', '.flac', '.wav', '.mka'):
                retained.append({'path': str(path), 'size_bytes': path.stat().st_size,
                                 'detention_prefix': path.name.startswith('DETAINED_')})
                if len(retained) >= 32:
                    break
        source['retained_files'] = retained
        if retained:
            source['path'] = retained[0]['path']
        source['path_exists'] = Path(source['path']).is_file()
        source['detention_prefix_violation'] = any(not item['detention_prefix'] for item in retained)
    track.ripped = source['state'] == 'complete'
    track.status = 'success' if track.ripped else 'failed'
    track.error = None if track.ripped else source.get('error')
    if track.ripped:
        track.filename = Path(source['path']).name
    save(job, plan)
    db.session.commit()


def settings_home(directory, filename):
    """Private per-subprocess preferences; never modify shared MakeMKV settings."""
    home = directory / '.makemkv-home'
    prefs = home / '.MakeMKV'
    prefs.mkdir(parents=True, mode=0o700)
    original = Path('/home/arm/.MakeMKV/settings.conf')
    content = original.read_text() if original.exists() else ''
    content = re.sub(r'^\s*(app_DefaultOutputFileName|app_DefaultSelectionString)\s*=.*$',
                     '', content, flags=re.MULTILINE)
    content += (f'\napp_DefaultOutputFileName = "{filename}"\n'
                'app_DefaultSelectionString = "+sel:all"\n')
    target = prefs / 'settings.conf'
    target.write_text(content)
    target.chmod(0o600)
    # Runtime registration settings are private, never returned in metadata.
    return home


def rip_video(job, run, output_type, progress_path):
    from arm.database import db
    plan = load(job)
    tracks = {t.track_id: t for t in job.tracks}
    job.raw_path = str(job_dir(job))
    job.path = str(job_dir(job))
    db.session.commit()
    if not plan['sources']:
        plan['extraction_finished_at'] = time.time()
        save(job, plan)
        raise ValueError('No video title selected: ' + plan.get('video_selection', {}).get('reason', 'selection unresolved'))
    stop_reason = None
    for source in plan['sources']:
        track = tracks[source['arm_track_id']]
        if stop_reason:
            source_result(job, plan, source, track, stop_reason + '; not attempted')
            continue
        directory = Path(source['expected_path']).parent
        messages = []
        source['backend_metadata']['extraction_messages'] = messages
        try:
            if any(directory.iterdir()):
                raise ValueError('Source directory is not empty; previous files preserved')
            home = settings_home(directory, Path(source['expected_path']).stem)
            env = {**os.environ, 'HOME': str(home),
                   'RH_RUNTIME_PATH': str(job_dir(job) / '.extraction-progress.json'),
                   'RH_OUTPUT_DIRECTORY': str(directory)}
            # Fixed explicit arguments: no language filtering, main-feature selection,
            # transcoding, inferred episodes, or untrusted per-job command strings.
            args = ['mkv', '--minlength=0', f'--progress={progress_path}',
                    job.makemkv_source, str(source['source_id']), str(directory)]
            for message in run(args, output_type, env=env):
                if hasattr(message, 'tid') and int(message.tid) != source['source_id']:
                    continue
                if dataclasses.is_dataclass(message):
                    messages.append({'kind': type(message).__name__, **dataclasses.asdict(message)})
                else:
                    messages.append({'message': str(message)})
            source['backend_metadata']['extraction_messages'] = messages
            if any('tid' not in m and int(m.get('code', 0)) in (5003, 5010, 5021, 5055)
                   for m in messages):
                raise ValueError('MakeMKV reported a failed title or unavailable extraction')
            outputs = list(directory.glob('*.mkv'))
            if len(outputs) != 1 or not outputs[0].name.startswith(Path(source['expected_path']).stem):
                raise ValueError('MakeMKV did not honor the detained filename template')
            probe(outputs[0], source)
            source_result(job, plan, source, track)
        except Exception as exc:
            source_result(job, plan, source, track, exc)
            runtime = job_dir(job) / '.extraction-progress.json'
            if any(int(m.get('code', 0)) == 5010 for m in messages):
                stop_reason = 'Disc unavailable; extraction queue stopped'
            elif runtime.is_file() and json.loads(runtime.read_text()).get('state') == 'stalled':
                stop_reason = 'No-progress watchdog stopped extraction'
        finally:
            # Preferences may contain the registration key; never retain as evidence.
            shutil.rmtree(directory / '.makemkv-home', ignore_errors=True)
    plan['extraction_finished_at'] = time.time()
    save(job, plan)
    if any(s['state'] != 'complete' for s in plan['sources']):
        raise ValueError('Some sources failed; successful and partial files remain in detention')
    return str(job_dir(job))


def optical_processes():
    """Conservative single-drive deployment inventory; never kill by a saved PID."""
    found = []
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = path.read_bytes().split(b'\0')
            names = [Path(a.decode(errors='replace')).name for a in args[:3] if a]
            if any(n in ('makemkvcon', 'cdparanoia', 'abcde', 'recognition-helper-abcde') for n in names):
                found.append({'pid': int(path.parent.name), 'process': names[0]})
        except (OSError, ValueError):
            continue
    return found


def queue_state():
    from arm.models import SystemDrives, Job
    processes = optical_processes()
    drives = []
    for drive in SystemDrives.query.all():
        job = Job.query.get(drive.job_id_current) if drive.job_id_current else None
        terminal = job and job.status in ('success', 'fail') and bool(job.stop_time)
        state = 'busy' if processes or job and not terminal else 'cleanup_pending' if drive.job_id_current else 'ready'
        runtime = load(job).get('runtime', {}) if job and managed(job) else {}
        if processes and runtime.get('state', '').startswith('stalled'):
            state = 'stalled'
        elif processes and runtime.get('state') == 'progressing':
            state = 'progressing'
        drives.append({'mount': drive.mount, 'state': state, 'job_id': drive.job_id_current,
                       'job_guid': str(job.guid) if job else None, 'job_status': job.status if job else None,
                       'runtime': runtime})
    return {'drives': drives, 'optical_processes': processes}


def reconcile_drive(job_id, job_guid):
    from arm.models import Job
    from arm.services.drives import job_cleanup
    job = Job.query.get(int(job_id))
    if (not job or str(job.guid) != job_guid or job.status not in ('success', 'fail')
            or not job.stop_time or optical_processes()):
        return {'success': False, 'reason': 'Job not finalized or optical process still active; ownership unchanged'}
    # The original ripper process must also be gone, even if MakeMKV has exited.
    pid = getattr(job, 'pid', None)
    if pid and Path(f'/proc/{int(pid)}').exists():
        return {'success': False, 'deferred': True, 'reason': 'Original job PID still exists; ownership unchanged'}
    job_cleanup(str(job.job_id))
    return {'success': True, 'job_id': job.job_id, 'job_guid': str(job.guid)}


def deletion_check(job, guid, cycle_id):
    if str(job.guid) != guid or job.status not in ('success', 'fail') or not job.stop_time:
        raise ValueError('Job GUID changed or job is not finalized')
    if optical_processes() or (getattr(job, 'pid', None) and Path(f'/proc/{int(job.pid)}').exists()):
        raise ValueError('Optical/ripper process still exists; deletion unavailable')
    if managed(job) and load(job)['cycle_id'] != cycle_id:
        raise ValueError('Job manifest belongs to another cycle')
    queue = queue_state()
    if any(d['job_id'] not in (None, job.job_id) for d in queue['drives']):
        raise ValueError('Another job owns a drive; deletion deferred')
    return {'job_id':job.job_id, 'job_guid':guid, 'cycle_id':cycle_id,
            'status':job.status, 'root':str(job_dir(job)), 'queue':queue,
            'arm_operation':'installed svc_jobs.delete_job: database rows and matching drive references only; no media deletion'}


def delete_finished(job, guid, cycle_id):
    # Same lock used by the start reservation. Never terminate or reclassify a job.
    from arm.services import jobs as svc_jobs
    lockpath = root() / f'.job-{job.job_id}-{UUID(str(job.guid))}.lock'
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deletion_check(job, guid, cycle_id)
        result = svc_jobs.delete_job(str(job.job_id), 'delete')
        if not result.get('success'):
            raise ValueError('Installed ARM delete operation failed')
        return {'arm':result, 'queue':queue_state()}
    finally:
        os.close(fd)


def finalize_video(job):
    from arm.database import db
    plan = load(job)
    if not plan.get('extraction_finished_at') or any(s['state'] != 'complete' for s in plan['sources']):
        raise ValueError('Detained extraction has not finalized')
    # The extraction files are already in their final detention location. ARM must
    # not move/rename them into its ordinary completed tree or overwrite anything.
    job.path = str(job_dir(job))
    db.session.commit()


def audio_config(directory):
    # Read+encode only: no CDDB query, tag rewriting, gain normalization, playlist,
    # metadata-dependent filenames, cleanup of old rips, or extra media copies.
    work = directory / 'work'
    work.mkdir(mode=0o2770)
    work.chmod(0o2770)
    config = directory / '.abcde.conf'
    config.write_text("\n".join([
        "ACTIONS=read,encode", "OUTPUTTYPE=flac", "FLACENCODERSYNTAX=flac",
        "FLACOPTS='-8 --verify'", "CDROMREADERSYNTAX=cdparanoia",
        "CDPARANOIA=cdparanoia", "CDPARANOIAOPTS='--never-skip'", "INTERACTIVE=n",
        "PADTRACKS=y", "MAXPROCS=1", "USEPIPES=n", "KEEPWAVS=y",
        f"OUTPUTDIR={shlex.quote(str(work))}", f"WAVOUTPUTDIR={shlex.quote(str(work))}", "",
    ]))
    return config, work


def rip_audio(job, logfile):
    from arm.database import db
    from arm.ripper import utils
    plan = load(job)
    tracks = {t.track_id: t for t in job.tracks}
    job.status = 'audio_ripping'
    job.path = str(job_dir(job))
    db.session.commit()
    for source in plan['sources']:
        track = tracks[source['arm_track_id']]
        directory = Path(source['expected_path']).parent
        try:
            if any(directory.iterdir()):
                raise ValueError('Source directory is not empty; previous files preserved')
            config, work = audio_config(directory)
            prefix = Path(source['expected_path']).stem + '_'
            proc = subprocess.Popen(['/opt/arm/recognition-helper-abcde', '-c', str(config),
                                     '-d', str(job.devpath), str(source['source_id'])],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors='replace',
                                    start_new_session=True,
                                    env={**os.environ, 'RH_PREFIX': prefix})
            try:
                errors = utils._stream_abcde_output(proc, job)
                proc.wait(timeout=30)
            finally:
                # The stock stall watchdog kills abcde itself, not necessarily its
                # cdparanoia/encoder children. They must not survive into a new source.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if proc.poll() is None:
                    proc.wait()
                proc.stdout.close()
            if proc.returncode or errors:
                raise ValueError('abcde failed: ' + '; '.join(errors or [str(proc.returncode)])[:800])
            outputs = list(work.rglob(prefix + '*.flac'))
            if len(outputs) != 1:
                raise ValueError('abcde did not produce exactly one prefixed FLAC for this source')
            probe(outputs[0], source, audio=True)
            subprocess.run(['flac', '--test', '--silent', str(outputs[0])],
                           timeout=120, check=True, capture_output=True)
            destination = Path(source['expected_path'])
            # Atomic no-clobber publication of a CLOSED file on the same filesystem.
            # link() fails on collision; neither old nor new content is overwritten.
            os.link(outputs[0], destination, follow_symlinks=False)
            sync_directory(destination.parent)
            outputs[0].unlink()
            source['path'] = str(destination)
            # Successful lossless encoding permits removing its redundant WAV scratch,
            # never another source or any incomplete/failed extraction.
            for wav in work.rglob(prefix + '*.wav'):
                wav.unlink()
            source_result(job, plan, source, track)
        except Exception as exc:
            source_result(job, plan, source, track, exc)
    plan['extraction_finished_at'] = time.time()
    save(job, plan)
    okay = all(s['state'] == 'complete' for s in plan['sources'])
    # ARM main.py owns SUCCESS and its finally/eject/stop_time boundary after
    # this function returns True. JobState.IDLE is actually "ready", not "idle";
    # neither ready nor helper identification state belongs in this transition.
    if not okay:
        job.status = 'fail'
        job.errors = 'Some CD sources failed; partial and successful outputs preserved in detention'
    db.session.commit()
    return okay
