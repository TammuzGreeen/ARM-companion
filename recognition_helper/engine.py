import copy
import json
import os
import shutil
import threading
import time
import tempfile
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2

from . import __version__
from .arm import ARM, SelectionPending
from .camera import Camera
from .decisions import map_outputs, merge, compatible, most_specific
from .masterlists import load_references
from .store import Store
from .vision import Vision, encode_images
from .budget import committed, usage_charge
from .source_references import apply_reference, signature


def already_associated(job, cycles):
    """SQLite may reuse a deleted numeric ID; a different GUID is a new job."""
    for cycle in cycles:
        if cycle.get('job_id') != job['job_id']:
            continue
        old_guid = cycle.get('job_guid') or cycle.get('backend_detail', {}).get('job', {}).get('guid')
        if not old_guid or not job.get('guid') or old_guid == job['guid']:
            return True
    return False


def preserved_move(source, destination):
    """Never replace an existing destination; prefer same-filesystem hardlink/unlink."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except FileExistsError:
        raise ValueError('Destination already exists; original detained file preserved') from None
    except OSError as error:
        import errno
        if error.errno != errno.EXDEV:
            raise
        # Cross-volume delivery must not expose an unprefixed, partial final file.
        staging = destination.parent / '.detention'
        staging.mkdir(exist_ok=True)
        if not staging.resolve().is_relative_to(destination.parent.resolve()):
            raise ValueError('Detention staging directory escapes the released destination')
        fd, name = tempfile.mkstemp(prefix='DETAINED_RELEASE_', suffix=destination.suffix, dir=staging)
        temporary = Path(name)
        with os.fdopen(fd, 'wb') as output, source.open('rb') as original:
            shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, source.stat().st_mode & 0o666)
        try:
            os.link(temporary, destination)
        finally:
            # Only the new redundant staging copy is removed; original remains
            # intact if publication fails or collides.
            temporary.unlink()
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    source.unlink()


class Engine:
    def __init__(self, settings):
        self.s = settings
        self.store = Store(settings.state / 'cycles')
        self.arm = ARM(settings)
        self.vision = Vision(settings)
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix='cycle-worker')
        self.stop = threading.Event()
        self.lock = self.store.lock
        self.camera = Camera(settings, self.opened, self.captured, self.may_calibrate)
        self.backend = {}
        self.error = None
        self.queue = {'state': 'unknown', 'reason': 'Backend queue not yet checked'}
        self.references = load_references(settings.state / 'masterlists')
        self.releasing = set()
        self.recognizing = set()
        from .transfers import TransferWorker
        self.transfers = TransferWorker(settings.transfer, self.transfer_records)

    def transfer_records(self):
        # The release lock is held only to copy small immutable record fields.
        # File IO/hashing never runs under the recognition/publication lock.
        with self.lock:
            return [dict(cycle_id=c['id'], job_guid=c.get('job_guid'), source_id=s['source_id'],
                         state=s['state'], path=s['path'], size_bytes=s.get('size_bytes'), released_at=s.get('released_at'))
                    for c in self.store.cycles.values() for s in c['sources']
                    if s.get('state') == 'released' and s.get('path')]

    def start(self):
        self.backend = self.arm.inspect()
        self.vision.check_access()
        self.recover_workers()
        self.camera.start()
        self.thread = threading.Thread(target=self.run, daemon=True, name='backend-monitor')
        self.thread.start()
        self.transfers.start()

    def close(self):
        self.transfers.close()
        self.stop.set()
        self.camera.close()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def recover_workers(self):
        """An uncertain prior model request is evidence loss, not permission to repeat it."""
        with self.lock:
            for cycle in self.store.cycles.values():
                if cycle.get('fallback_pending'):
                    cycle['fallback'] = {'model': self.s.external_model, 'provider': 'openai', 'findings': {},
                                         'error': 'Service restarted during fallback; response and billing outcome unknown',
                                         'latency_seconds': None}
                    cycle['fallback_cost_usd'] = usage_charge(None, cycle['fallback_budget'])
                    cycle['fallback_pending'] = False
                    cycle['fallback_attempted'] = True
                    self.store.event(cycle, 'interrupted fallback recorded without another paid request')
                if not cycle.get('closed_at') or cycle.get('local_done'):
                    continue
                models = cycle.setdefault('local_model_ids', list(self.s.models[:2]))
                tracked = 'local_requests' in cycle
                requests = cycle.setdefault('local_requests', [])
                existing = {result['model'] for result in cycle['locals']}
                if not tracked:
                    for model in models:
                        if model not in existing:
                            cycle['locals'].append({'model': model, 'findings': {},
                                'error': 'No recoverable request history after restart; this model was not re-requested',
                                'latency_seconds': None})
                            existing.add(model)
                for request in requests:
                    if request['status'] == 'running':
                        request['status'] = 'interrupted'
                        if request['model'] not in existing:
                            cycle['locals'].append({'model': request['model'], 'provider': 'ollama', 'findings': {},
                                                    'error': 'Local request interrupted by service restart; response unavailable',
                                                    'started': request['started'], 'latency_seconds': None})
                            existing.add(request['model'])
                if cycle.get('recognition_done'):
                    self.store.save(cycle)
                elif all(model in existing for model in models):
                    cycle['local_done'] = True
                    self.store.save(cycle)
                else:
                    self.pool.submit(self.recognize, cycle['id'])

    def may_calibrate(self):
        with self.lock:
            if any(d.get('state') != 'ready' for d in self.queue.get('drives', [])):
                return False, 'Drive busy or cleanup pending; not ready for a new physical cycle.'
            for cycle in self.store.snapshot():
                if not cycle.get('job_id') or cycle.get('ejection_observed'):
                    continue
                detail = self.arm.detail(cycle['job_id'])
                job = detail['job']
                if job.get('ejected') or (cycle.get('rip_started_at') and job.get('status') in ('success', 'fail', 'failed')):
                    cycle = self.store.cycles[cycle['id']]
                    cycle['ejection_observed'] = True
                    self.store.event(cycle, 'post-rip opening observed; not an insertion cycle')
                    return False, 'Completed disc ejected. Remove it, manually close, then reopen empty for the next cycle.'
                if job.get('status') not in ('success', 'fail', 'failed', 'cancelled'):
                    return False, 'ARM still owns this disc job; opening is not treated as an empty insertion cycle.'
            return True, None

    def opened(self):
        with self.lock:
            jobs = self.arm.jobs()
            for older in self.store.cycles.values():
                if not older.get('job_id') and not older.get('superseded'):
                    older['superseded'] = True
                    older['state'] = 'superseded_without_arm_job'
                    self.store.event(older, 'unmatched earlier cycle superseded by a fresh physical opening')
            cycle = self.store.create(state='calibrating', helper_version=__version__,
                                      baseline_job_ids=[j['job_id'] for j in jobs],
                                      drive=self.s.drive, backend_version=self.backend.get('version', {}),
                                      local_model_ids=list(self.s.models[:2]), local_requests=[])
            self.store.event(cycle, 'manual empty opening observed')
            return cycle['id']

    def captured(self, cycle_id, samples):
        with self.lock:
            cycle = self.store.cycles[cycle_id]
            if cycle.get('capture_complete'):
                return
            folder = self.store.root / cycle_id
            paths, crops = [], []
            for index, (original, crop) in enumerate(samples):
                for name, image in ((f'original-{index}.jpg', original), (f'capture-{index}.jpg', crop)):
                    path = folder / name
                    if not cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 94]):
                        raise ValueError('Could not retain snapshot; tray remains open')
                    with path.open('rb') as handle:
                        os.fsync(handle.fileno())
                    paths.append(name)
                crops.append(crop)
            # Retain the exact resized inputs once; do not save duplicate request payloads.
            encode_images(crops, folder)
            cycle.update(capture_complete=True, snapshots=paths, captured_at=time.time(), state='captured')
            self.store.event(cycle, 'usable snapshots retained before automatic close')
            self.pool.submit(self.close_and_launch, cycle_id)

    def close_and_launch(self, cycle_id):
        with self.lock:
            cycle = self.store.cycles[cycle_id]
            if cycle.get('close_intent') or cycle.get('superseded'):
                return
            if self.camera.cycle != cycle_id or self.camera.status['tray'] != 'open':
                cycle.update(state='capture_invalidated', technical_error='Physical tray cycle ended before the close command; snapshots retained')
                self.store.event(cycle, 'stale close work rejected without a drive operation')
                return
            cycle['close_intent'] = time.time()
            self.store.event(cycle, 'ARM tray close intent')
        try:
            self.arm.tray('close')
            with self.lock:
                cycle['close_acknowledged'] = time.time()
                if not cycle.get('closed_at'):
                    cycle['state'] = 'closing'
                self.store.save(cycle)
            deadline = time.monotonic() + 30
            while self.camera.status['tray'] != 'closed':
                if time.monotonic() > deadline:
                    raise ValueError('ARM acknowledged close, but physical closed state was not observed within 30 seconds')
                if self.stop.wait(.2):
                    return
            self.observed_closed(cycle)
        except Exception as error:
            with self.lock:
                cycle['technical_error'] = str(error)
                cycle['state'] = 'close_error'
                self.store.event(cycle, 'automatic close failed or outcome uncertain; not repeated')

    def observed_closed(self, cycle):
        with self.lock:
            if cycle.get('closed_at') or cycle.get('superseded'):
                return
            cycle.update(closed_at=time.time(), state='recognizing_and_ripping')
            self.store.event(cycle, 'physical closure observed; parallel recognition and backend workflow launched',
                             close_acknowledged=bool(cycle.get('close_acknowledged')),
                             previous_error=cycle.pop('technical_error', None))
            self.pool.submit(self.recognize, cycle['id'])

    def recognize(self, cycle_id):
        with self.lock:
            if cycle_id in self.recognizing:
                return
            self.recognizing.add(cycle_id)
            cycle = self.store.cycles[cycle_id]
            models = cycle.setdefault('local_model_ids', list(self.s.models[:2]))
            requests = cycle.setdefault('local_requests', [])
        import base64
        try:
            folder = self.store.root / cycle_id
            images = [base64.b64encode(path.read_bytes()).decode('ascii') for path in sorted(folder.glob('model-input-*.jpg'))]
            if not 1 <= len(images) <= 2:
                raise ValueError('Retained model inputs unavailable; no image-free identification request is allowed')
            # Ollama may serialize on its available memory; ripping never waits for it.
            for model in models:
                with self.lock:
                    if any(result['model'] == model for result in cycle['locals']):
                        continue
                    request = {'model': model, 'started': time.time(), 'status': 'running', 'attempt': 1}
                    requests.append(request)
                    self.store.event(cycle, 'local observation request started', model=model)
                record = self.vision.observe(model, images)
                with self.lock:
                    request['status'] = 'failed' if record.get('error') else 'finished'
                    cycle['locals'].append(record)
                    self.store.event(cycle, 'local observation finished', model=model, latency=record['latency_seconds'])
        except Exception as error:
            with self.lock:
                for model in models:
                    if not any(result['model'] == model for result in cycle['locals']):
                        cycle['locals'].append({'model': model, 'findings': {}, 'error': type(error).__name__ + ': observation unavailable', 'latency_seconds': None})
                self.store.event(cycle, 'recognition failed; extraction remains independent')
        finally:
            with self.lock:
                cycle['local_done'] = True
                self.recognizing.discard(cycle_id)
                self.store.save(cycle)

    def decide(self, cycle):
        references = [r for r in self.references if r['id'] not in cycle.get('masterlist_review',{}).get('rejected',[])]
        if cycle.get('local_done') and not cycle.get('sources') and cycle['backend_done']:
            cycle['decision'] = merge(cycle['locals'], cycle.get('fallback'), references)
            cycle['recognition_done'] = True
            self.store.save(cycle)
            return
        if not cycle.get('local_done') or (not cycle.get('sources') and not cycle.get('selection_waiting')) or cycle.get('recognition_done') or cycle.get('fallback_pending'):
            return
        observations = copy.deepcopy(cycle['locals'])
        omitted_years = []
        for observation in observations:
            f = observation.get('findings', {})
            if f.get('media_type') == 'movie' and any('year' in str(u).lower() and
                    any(word in str(u).lower() for word in ('infer', 'not printed', 'unknown'))
                    for u in f.get('uncertainties', [])):
                if f.get('year') is not None:
                    omitted_years.append({'model': observation.get('model'), 'year': f.pop('year')})
        decision = merge(observations, cycle.get('fallback'), references)
        if omitted_years:
            decision['omitted_uncertain_movie_years'] = omitted_years
        from .audio_metadata import apply_audio_metadata
        audio_precedence = apply_audio_metadata(cycle, decision)
        if not audio_precedence:
            self.enrich_backend_metadata(cycle, decision)
        identity = decision['identity']
        audio_sources = bool(cycle['sources']) and all(s.get('backend_metadata', {}).get('source_number_kind', '').startswith(('CD TOC', 'ARM audio')) for s in cycle['sources'])
        if not audio_precedence and identity.get('media_type') in ('music', 'audiobook') and audio_sources:
            if not identity.get('tracks'):
                identity['tracks'] = sorted(int(s['source_id']) for s in cycle['sources'])
                physical = all(s.get('backend_metadata', {}).get('physical_toc_track') for s in cycle['sources'])
                decision['attribution']['tracks'] = ['backend physical CD TOC' if physical else
                    'backend enumerated audio source numbers; no independent physical TOC retained']
            job = cycle.get('backend_detail', {}).get('job', {})
            for field in ('artist', 'album', 'disc_number'):
                target = 'disc' if field == 'disc_number' else field
                if not identity.get(target) and job.get(field) not in (None, ''):
                    identity[target] = job[field]
                    decision['attribution'][target] = ['backend disc metadata (not model inference)']
        scan_signature = None
        if identity.get('media_type') == 'tv' and cycle['sources']:
            titles = cycle['sources'][0].get('backend_metadata', {}).get('video_selection', {}).get('titles', [])
            if titles:
                scan_signature = [{'source_id': t['source_id'], 'duration_seconds': t['duration_seconds']}
                                  for t in sorted(titles, key=lambda t: int(t['source_id']))]
        referenced_sources, mapping_reasons = apply_reference(cycle['sources'], decision.get('masterlist', {}),
                                                            cycle.get('backend_detail', {}).get('job', {}), scan_signature)
        if mapping_reasons:
            decision['mapping_reasons'] = mapping_reasons
        mapped = map_outputs(decision, referenced_sources, cycle['locals'])
        reasons = [s['reason'] for s in mapped if s['reason']]
        if cycle.get('selection_waiting') and not cycle['sources'] and (
                identity.get('media_type') not in ('tv', 'movie') or 'media_type' in decision['conflicts']):
            reasons.append('Source selection needs an established TV/movie category; technical candidates are ambiguous')
        cycle['decision'] = decision
        if reasons and not cycle.get('fallback_attempted'):
            cycle['fallback_attempted'] = True
            cycle['fallback_reasons'] = list(dict.fromkeys(reasons))
            used = sum(bool(c.get('fallback_reserved')) for c in self.store.cycles.values())
            key_present = bool(self.s.external_key or (self.s.key_file and Path(self.s.key_file).is_file()))
            cycle['fallback_context'] = {
                'locals': [{'model': o['model'], 'findings': o.get('findings', {})} for o in cycle['locals']],
                'identity': decision['identity'], 'conflicts': decision['conflicts'],
                'masterlist': decision['masterlist'], 'source_count': len(cycle['sources']),
                'sources': [{'source_id': s['source_id'],
                             'duration_seconds': s.get('backend_metadata', {}).get('scan_duration'),
                             'number_kind': s.get('backend_metadata', {}).get('source_number_kind')}
                            for s in cycle['sources'][:100]],
                'additional_source_descriptor_count': max(0, len(cycle['sources']) - 100),
                'audio_metadata': decision.get('audio_metadata'),
            }
            budget = None
            if key_present:
                try:
                    budget = self.vision.quote(cycle['fallback_context'])
                except ValueError as error:
                    cycle['fallback_unavailable'] = str(error)
            if (key_present and budget and self.s.spending_limit > 0 and used < self.s.fallback_limit
                    and committed(self.store.cycles.values()) + budget['reserved_usd'] <= self.s.spending_limit):
                cycle.update(fallback_pending=True, fallback_reserved=True)
                cycle['fallback_budget'] = budget
                self.store.event(cycle, 'external fallback reserved', reasons=cycle['fallback_reasons'])
                self.pool.submit(self.fallback, cycle['id'])
                return
            cycle.setdefault('fallback_unavailable', 'Runtime key/access or configured fallback spending/call budget is missing/exhausted')
        if cycle.get('fallback_pending'):
            return
        if cycle.get('selection_waiting') and not cycle['sources']:
            self.store.save(cycle)
            return  # selection may now resume; final source decision follows reservation
        cycle.update(recognition_done=True, decision=decision, sources=mapped)
        if reasons:
            cycle['manual_required'] = True
            cycle['state'] = 'identification_detained'
        else:
            cycle['state'] = 'identified_awaiting_completion'
        self.store.event(cycle, 'recognition decision complete', manual_required=cycle['manual_required'])

    def enrich_backend_metadata(self, cycle, decision):
        """Editorial identity is useful evidence, never mislabeled as physical extraction."""
        metadata = cycle.get('backend_editorial_metadata', {})
        identity = decision['identity']
        backend_kind = {'series': 'tv', 'movie': 'movie'}.get(metadata.get('video_type'))
        kind = backend_kind or identity.get('media_type')
        facts = {k: metadata[k] for k in ('year', 'season', 'artist', 'album') if metadata.get(k) not in (None, '')}
        if kind:
            facts['media_type'] = kind
        if metadata.get('title') and kind in ('tv', 'movie'):
            facts['series' if kind == 'tv' else 'title'] = metadata['title']
        for field, value in facts.items():
            source = 'backend editorial metadata (may be looked up/edited; not printed-disc extraction)'
            if field not in identity:
                identity[field] = value
                decision['attribution'][field] = [source]
            elif not compatible(field, identity[field], value):
                fallback_value = cycle.get('fallback', {}).get('findings', {}).get(field)
                if (fallback_value not in (None, '', []) and compatible(field, fallback_value, identity[field])
                        and any(o.get('findings', {}).get(field) not in (None, '', [])
                                and compatible(field, fallback_value, o['findings'][field]) for o in cycle['locals'])):
                    decision.setdefault('rejected_backend_facts', {})[field] = value
                    continue
                # Preserve uncertainty instead of silently letting a catalogue
                # lookup replace what the camera models actually observed.
                decision['conflicts'].setdefault(field, [('combined observation', identity[field])]).append((source, value))
            elif field == 'series':
                refined = most_specific(field, [identity[field], value])
                if refined != identity[field]:
                    identity[field] = refined
                    decision['attribution'].setdefault(field, []).append(source)

    def fallback(self, cycle_id):
        import base64
        with self.lock:
            cycle = self.store.cycles[cycle_id]
            context = cycle['fallback_context']
        try:
            images = [base64.b64encode(path.read_bytes()).decode('ascii') for path in sorted((self.store.root / cycle_id).glob('model-input-*.jpg'))]
            if not 1 <= len(images) <= 2:
                raise ValueError('Retained fallback images unavailable')
            result = self.vision.observe(self.s.external_model, images, external=True, context=context)
        except Exception as error:
            result = {'model': self.s.external_model, 'provider': 'openai', 'findings': {},
                      'error': type(error).__name__ + ': fallback response unavailable', 'latency_seconds': None}
        with self.lock:
            cycle['fallback'] = result
            cycle['fallback_cost_usd'] = usage_charge(result.get('usage'), cycle['fallback_budget'])
            cycle['fallback_pending'] = False
            self.store.event(cycle, 'external fallback finished', latency=result['latency_seconds'])

    def preview_correction(self, cycle_id, payload):
        with self.lock:
            cycle = self.store.cycles[cycle_id]
            decision = {'identity': payload['identity'], 'conflicts': {}, 'attribution': {'source': 'manual correction'}}
            assignments = payload['assignments']
            known = {str(s['source_id']) for s in cycle['sources']}
            if set(assignments) - known:
                raise ValueError('Correction contains unknown source IDs')
            if len([a for a in assignments.values() if a]) != len(assignments):
                raise ValueError('Each supplied source needs an explicit assignment')
            mapped = map_outputs(decision, cycle['sources'], assignments=assignments)
            proposed = [s['proposed'] for s in mapped if s['proposed']]
            if len(set(proposed)) != len(proposed):
                raise ValueError('Proposed names collide; specify versions or distinct assignments')
            for source in mapped:
                if source.get('proposed'):
                    destination = self.s.released / source['proposed']
                    if not destination.resolve().is_relative_to(self.s.released.resolve()):
                        raise ValueError('Proposed destination escapes released root')
                    source['destination'] = str(destination)
                    source['collision'] = (destination.exists() or destination.is_symlink()) and source.get('state') != 'released'
                    source['publication_ready'] = source.get('state') == 'complete' and not source['collision']
            return mapped

    def review(self, cycle_id):
        from .review import suggestions, source_view
        with self.lock:
            cycle = self.store.cycles[cycle_id]
            draft = cycle.get('review_draft') or {}
            identity = draft.get('identity') or (cycle.get('corrections') or [{}])[-1].get('identity') or (cycle.get('decision') or {}).get('identity',{})
            sources = [source_view(dict(s,assignment=draft['assignments'].get(str(s['source_id'])))
                                   if 'assignments' in draft and s.get('state')!='released' else s,
                                   identity,self.s.tv_min_length_seconds) for s in cycle['sources']]
            sources.sort(key=lambda s:(not s['episode_candidate'],s['short_clip'],int(s['source_id'])))
            return {'cycle_id':cycle_id,'job_guid':cycle.get('job_guid'),'identity':copy.deepcopy(identity),
                    'sources':sources,'candidates':suggestions(cycle,self.references),
                    'masterlist_review':copy.deepcopy(cycle.get('masterlist_review',{})),
                    'draft':copy.deepcopy(draft),'released_root':str(self.s.released),
                    'tv_min_length_seconds':self.s.tv_min_length_seconds}

    def review_masterlist(self, cycle_id, reference_id, action):
        from .review import confirmed_proposal
        with self.lock:
            cycle = self.store.cycles[cycle_id]
            reference = next((r for r in self.references if r['id']==reference_id),None)
            if reference is None:
                raise ValueError('Unknown masterlist identifier')
            if action not in ('reject','confirm','reconsider'):
                raise ValueError('Unknown masterlist review action')
            if action == 'confirm' and all(s.get('state')=='released' for s in cycle['sources']):
                raise ValueError('No detained sources remain')
            state = cycle.setdefault('masterlist_review',{'rejected':[],'history':[]})
            state['history'].append({'at':time.time(),'reference_id':reference_id,'action':action,
                                     'job_guid':cycle.get('job_guid')})
            if action == 'reject':
                if reference_id not in state['rejected']:
                    state['rejected'].append(reference_id)
                if state.get('confirmed_id') == reference_id:
                    state.pop('confirmed_id',None)
                    cycle.pop('review_draft',None)
            elif action == 'confirm':
                state['rejected'] = [r for r in state['rejected'] if r!=reference_id]
                state['confirmed_id'] = reference_id
                proposal = confirmed_proposal(cycle,reference,self.s.tv_min_length_seconds)
                proposal['reference_id'] = reference_id
                proposal['reference_snapshot'] = copy.deepcopy(reference)
                proposal['original_decision'] = copy.deepcopy(cycle.get('decision'))
                cycle['review_draft'] = proposal
            elif action == 'reconsider':
                state['rejected'] = [r for r in state['rejected'] if r!=reference_id]
            else:
                raise ValueError('Unknown masterlist review action')
            self.store.event(cycle,'manual masterlist review; no publication',reference_id=reference_id,action_taken=action)
            result = self.review(cycle_id)
            if cycle.get('review_draft'):
                result['preview'] = self.preview_correction(cycle_id,cycle['review_draft'])
            return result

    def correct(self, cycle_id, payload):
        with self.lock:
            cycle = self.store.cycles[cycle_id]
            if not any(source.get('state') != 'released' for source in cycle['sources']):
                raise ValueError('No detained outputs remain in this cycle; released files are unchanged')
            if any(s.get('state') == 'released' for s in cycle['sources']):
                # Corrections act only on detained sources; released files are immutable here.
                released = {str(s['source_id']): s for s in cycle['sources'] if s.get('state') == 'released'}
            else:
                released = {}
            mapped = self.preview_correction(cycle_id, payload)
            previous_sources = [{k: copy.deepcopy(s.get(k)) for k in
                                 ('source_id', 'arm_track_id', 'path', 'state', 'assignment', 'proposed', 'reason')}
                                for s in cycle['sources']]
            cycle.setdefault('corrections', []).append(dict(at=time.time(), identity=payload['identity'], assignments=payload['assignments'],
                                                         masterlist_confirmation=copy.deepcopy(cycle.get('masterlist_review')),
                                                         reviewed_proposal=copy.deepcopy(cycle.get('review_draft')),
                                                         previous_sources=previous_sources, previous_decision=copy.deepcopy(cycle.get('decision'))))
            cycle['sources'] = [released.get(str(s['source_id']), dict(s, manual_authorized=bool(s.get('proposed')))) for s in mapped]
            cycle['manual_required'] = True  # sticky: unresolved sources can never be silently re-guessed
            cycle['recognition_done'] = True
            cycle['state'] = 'correction_accepted_awaiting_completion' if not cycle['backend_done'] else 'reviewing_completed_outputs'
            self.store.event(cycle, 'user submitted correction and requested release', correction_index=len(cycle['corrections']) - 1,
                             identity=payload['identity'], assignments=payload['assignments'])
            self.release(cycle)
            return copy.deepcopy(cycle)

    def release(self, cycle):
        for source in cycle['sources']:
            if source.get('state') == 'released':
                # v0.1 left a resolved filesystem error in the display reason.
                # Preserve that history, but do not label a verified released file
                # as unidentified or failed forever.
                notice = source.get('release_error') or source.get('reason')
                if notice and Path(source['path']).is_file():
                    source.setdefault('resolved_release_errors', []).append(notice)
                    source['reason'] = None
                    source.pop('release_error', None)
                    self.store.event(cycle, 'prior release error resolved; released file verified',
                                     source_id=source['source_id'], previous_error=notice)
                continue
            if (source.get('release_intent') and source.get('reason') and
                    source['reason'].startswith(('[Errno ', 'Destination already exists'))):
                source['release_error'] = source['reason']
                source['reason'] = None
            if source.get('state') != 'complete' or not source.get('proposed'):
                continue
            # Automatic passes can release good sources even while another is detained.
            if source.get('reason') and not source.get('manual_authorized'):
                continue
            path = Path(source['path'])
            relative = Path(source['proposed'])
            if relative.is_absolute() or '..' in relative.parts:
                source['reason'] = 'Unsafe output name rejected'
                continue
            destination = self.s.released / relative
            if not destination.resolve().is_relative_to(self.s.released.resolve()):
                source['release_error'] = 'Released destination escapes configured root through a symlink'
                continue
            if (not path.is_file() or path.is_symlink() or not path.name.startswith('DETAINED_')
                    or not path.resolve().is_relative_to(self.s.detention.resolve())):
                source['release_error'] = 'Finalized detained output not found; original files preserved'
                continue
            try:
                identity = (cycle['corrections'][-1]['identity'] if cycle.get('corrections')
                            else cycle.get('decision', {}).get('identity', {}))
                if (path.suffix.lower() == '.flac' and identity.get('media_type') in ('music', 'audiobook')
                        and source.get('assignment', {}).get('kind') in ('music', 'audiobook', 'track')):
                    from .audio_tags import tag_completed_flac
                    source.setdefault('extraction_size_bytes', path.stat().st_size)
                    excluded = (() if cycle.get('corrections') else
                                cycle.get('decision', {}).get('audio_metadata', {}).get('optional_disagreements', {}))
                    source['tagging'] = tag_completed_flac(path, identity, source['assignment'], excluded)
                    source['size_bytes'] = source['tagging']['size_bytes']
                source['release_intent'] = {'from': str(path), 'to': str(destination), 'at': time.time()}
                self.store.save(cycle)
                preserved_move(path, destination)
                if source.get('release_error'):
                    source.setdefault('resolved_release_errors', []).append(source.pop('release_error'))
                source.update(state='released', reason=None, original_detained_path=str(path), path=str(destination), released_at=time.time())
                self.store.event(cycle, 'output released', source_id=source['source_id'], path=str(destination))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                source['release_error'] = str(error)
        if cycle['backend_done']:
            states = [source.get('state') for source in cycle['sources']]
            if states and all(state == 'released' for state in states):
                cycle['state'] = 'released'
            elif 'failed' in states:
                cycle['state'] = 'completed_with_technical_failures'
            elif 'released' in states:
                cycle['state'] = 'partially_released'
            else:
                cycle['state'] = 'completed_detained'
        self.store.save(cycle)

    def refresh_queue(self):
        queue = self.arm.queue_status()
        if not isinstance(queue, dict):
            return
        self.queue = queue
        path = self.store.root / 'drive-recovery.json'
        attempts = json.loads(path.read_text()) if path.exists() else {}
        for drive in queue.get('drives', []):
            if drive.get('state') != 'cleanup_pending':
                continue
            key = str(drive.get('job_guid')) + ':' + str(drive.get('mount'))
            entry = attempts.setdefault(key, {'attempts': 0})
            if time.time() < entry.get('retry_after', 0):
                drive['recovery'] = entry.get('result')
                continue
            if entry['attempts'] >= 3:
                drive['recovery'] = 'Automatic reconciliation exhausted; inspect backend ownership'
                continue
            entry.update(attempts=entry['attempts'] + 1, at=time.time())
            temp = path.with_suffix('.new')
            temp.write_text(json.dumps(attempts))
            temp.replace(path)  # persist intent before the supported cleanup operation
            entry['result'] = self.arm.reconcile_drive(drive['job_id'], drive['job_guid'])
            if entry['result'].get('deferred'):
                entry['attempts'] -= 1  # observing shutdown is not a failed cleanup operation
                entry['retry_after'] = time.time() + 30
            temp.write_text(json.dumps(attempts))
            temp.replace(path)
            drive['recovery'] = entry['result']

    def run(self):
        while not self.stop.is_set():
            try:
                self.refresh_queue()
                jobs = self.arm.jobs()
                with self.lock:
                    for cycle in self.store.cycles.values():
                        if cycle.get('superseded'):
                            continue
                        if not cycle.get('close_intent') and not cycle.get('close_acknowledged'):
                            continue
                        if cycle['backend_done']:
                            self.decide(cycle)
                            if cycle['recognition_done'] and any(s.get('state') != 'released' for s in cycle['sources']):
                                self.release(cycle)
                            continue
                        if (not cycle.get('closed_at') and self.camera.status['tray'] == 'closed'
                                and self.camera.cycle in (None, cycle['id'])):
                            self.observed_closed(cycle)
                        if not cycle.get('job_id'):
                            new = [j for j in jobs if j['job_id'] not in cycle['baseline_job_ids']
                                   and not already_associated(j, self.store.cycles.values()) and j.get('devpath') == self.s.drive]
                            if len(new) == 1:
                                cycle['job_id'] = new[0]['job_id']
                                cycle['job_guid'] = new[0].get('guid')
                                self.store.event(cycle, 'ARM job associated', job_id=cycle['job_id'])
                            elif len(new) > 1:
                                cycle['technical_error'] = 'Multiple unassociated jobs; refusing arbitrary disc association'
                        if cycle.get('job_id'):
                            detail = self.arm.detail(cycle['job_id'])
                            if cycle.get('job_guid') and detail['job'].get('guid') != cycle['job_guid']:
                                raise ValueError('ARM numeric job ID was reused; active cycle GUID no longer matches')
                            cycle['backend_status'] = detail['job'].get('status')
                            cycle['backend_detail'] = detail
                            if not cycle.get('sources') and (cycle['backend_status'] == 'manual_paused' or cycle.get('prepare_intent')) and not detail['job'].get('stop_time'):
                                if cycle.get('selection_waiting') and not cycle.get('local_done'):
                                    continue
                                if cycle.get('selection_waiting'):
                                    self.decide(cycle)
                                    if cycle.get('fallback_pending'):
                                        continue
                                attempts = cycle.get('prepare_attempts', 0)
                                if attempts < 3:
                                    cycle['prepare_intent'] = cycle.get('prepare_intent', time.time())
                                    cycle['prepare_attempts'] = attempts + 1
                                    self.store.event(cycle, 'idempotent same-cycle detention/start reservation attempt', attempt=attempts + 1)
                                    try:
                                        if cycle.get('local_done'):
                                            recognition = cycle.get('decision') or merge(cycle['locals'], cycle.get('fallback'), self.references)
                                            category = recognition['identity'].get('media_type') if 'media_type' not in recognition['conflicts'] else None
                                            cycle['sources'] = self.arm.prepare_and_start(cycle['job_id'], cycle['id'],
                                                content_kind=category, recognition_finished=bool(cycle.get('selection_waiting')),
                                                tv_context={'reference': recognition.get('masterlist', {}),
                                                            'episodes': recognition['identity'].get('episodes', []),
                                                            'extra_observations': [o.get('findings', {}).get('extras', []) for o in cycle['locals']]} if category == 'tv' else None)
                                        else:
                                            cycle['sources'] = self.arm.prepare_and_start(cycle['job_id'], cycle['id'])
                                    except SelectionPending as pending:
                                        cycle.update(selection_waiting=True, state='selection_waiting_for_recognition',
                                                     backend_selection=pending.report, prepare_attempts=attempts)
                                        self.store.save(cycle)
                                        continue
                                    cycle.pop('selection_waiting', None)
                                    cycle['rip_started_at'] = cycle.get('rip_started_at', time.time())
                                    self.store.event(cycle, 'ARM extraction associated independently of recognition')
                                else:
                                    cycle['sources'] = self.arm.recover(cycle['job_id'], cycle['id'])
                                    cycle['rip_started_at'] = cycle.get('rip_started_at', cycle['prepare_intent'])
                                    self.store.event(cycle, 'existing extraction recovered read-only after uncertain start responses')
                            if cycle.get('sources') and not cycle.get('metadata_attempted'):
                                cycle['metadata_attempted'] = time.time()
                                try:
                                    cycle['backend_editorial_metadata'] = self.arm.metadata(cycle['job_id'])
                                except Exception as error:
                                    cycle['metadata_error'] = type(error).__name__ + ': editorial metadata unavailable; extraction continues'
                                self.store.save(cycle)
                            if (not cycle.get('sources') and cycle['backend_status'] in ('fail', 'failed', 'cancelled')
                                    and detail['job'].get('stop_time')):
                                cycle.update(backend_done=True, state='technical_failure_no_outputs',
                                             technical_error=detail['job'].get('errors') or 'Backend failed before any extraction outputs were reserved')
                                self.store.event(cycle, 'backend failed before extraction; retained camera/model evidence')
                            if cycle.get('sources') and not cycle['backend_done']:
                                completion = self.arm.completion(cycle['job_id'], cycle['sources'])
                                previous = {str(s['source_id']): s for s in cycle['sources']}
                                finalized = [{**previous[str(s['source_id'])], **s} for s in completion['sources']]
                                if finalized:
                                    cycle['sources'] = finalized
                                if completion['done']:
                                    cycle.update(backend_done=True, backend_status=completion['status'], sources=finalized)
                                    self.store.event(cycle, 'backend finalized', status=completion['status'])
                            self.decide(cycle)
                            if cycle['backend_done'] and cycle['recognition_done']:
                                self.release(cycle)
                            self.store.save(cycle)
                self.error = None
            except Exception as error:
                self.error = f'{type(error).__name__}: {error}'
            self.stop.wait(2)

    def export(self, cycle_id):
        cycle = self.store.cycles[cycle_id]
        identity = cycle.get('decision', {}).get('identity', {})
        if cycle.get('corrections'):
            identity = cycle['corrections'][-1]['identity']
        # Explicit allowlist, not a generic dump with private identifiers/paths.
        allowed = ('media_type', 'series', 'season', 'episodes', 'title', 'year', 'artist', 'album', 'author', 'book', 'disc', 'part', 'tracks')
        facts = {k: identity[k] for k in allowed if identity.get(k) not in (None, '', [])}
        job = cycle.get('backend_detail', {}).get('job', {})
        attribution = ({k: ['user-submitted correction'] for k in facts} if cycle.get('corrections')
                       else cycle.get('decision', {}).get('attribution', {}))
        versions = any(s.get('assignment', {}).get('version') for s in cycle['sources'] if s.get('assignment'))
        public_signature = {'facts': facts, 'label': job.get('label'), 'sources': signature(cycle['sources'])}
        reference_id = 'partial-' + hashlib.sha256(json.dumps(public_signature, sort_keys=True).encode()).hexdigest()[:12]
        return {'schema_version': 3, 'id': reference_id, 'facts': facts,
                'provenance': ['Exported explicitly from recognition-helper; verify applicability to your release'],
                'fact_attribution': attribution,
                'disc_observation': {'label': job.get('label'), 'disctype': job.get('disctype'),
                                     'source_signature': signature(cycle['sources'])},
                'source_observations': [{'source_id': s['source_id'], 'assignment': s.get('assignment'),
                                         'source_id_scope': 'this observed disc only; not reusable IDs for another disc'}
                                        for s in cycle['sources']],
                'collection_conventions': {'multiple_versions_per_episode': versions}, 'automatic_publication': False}
