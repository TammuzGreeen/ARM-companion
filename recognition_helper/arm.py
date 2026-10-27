"""ARM Neu adapter. Extraction never depends on a recognition decision."""
from pathlib import Path, PurePosixPath

import httpx


class SelectionPending(ValueError):
    def __init__(self, report):
        super().__init__(report.get('reason', 'Source selection awaiting recognition'))
        self.report = report


class ARM:
    def __init__(self, settings, *, transport=None):
        self.s = settings
        headers = {'X-Api-Version': '2'}
        if settings.arm_token:
            headers['Authorization'] = 'Bearer ' + settings.arm_token
        self.client = httpx.Client(base_url=settings.arm_url.rstrip('/') + '/api/v1/',
                                   headers=headers, timeout=30, trust_env=False,
                                   follow_redirects=False, transport=transport)

    def call(self, method, path, body=None):
        response = self.client.request(method, path.lstrip('/'), json=body)
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict) and result.get('success') is False:
            raise ValueError('ARM rejected operation: ' + str(result.get('error', 'unknown error')))
        return result

    def inspect(self):
        result = {
            'version': self.call('GET', 'system/version'),
            'drives': self.call('GET', 'drives')['drives'],
            'config': self.call('GET', 'settings/config')['config'],
            **self.call('GET', 'system/ripping-enabled'),
        }
        try:
            result['overlay'] = self.call('GET', 'recognition-helper/capabilities')
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            result['overlay'] = {'enabled': False, 'reason': 'Backend overlay is not installed'}
        return result

    def jobs(self):
        page, jobs = 1, []
        while True:
            data = self.call('GET', f'jobs/paginated?page={page}&per_page=100')
            jobs.extend(data['jobs'])
            if page >= data['pages']:
                return jobs
            page += 1

    def detail(self, job_id):
        return self.call('GET', f'jobs/{int(job_id)}/detail')

    def metadata(self, job_id):
        """Backend editorial evidence, not independently extracted disc facts."""
        return self.call('GET', f'jobs/{int(job_id)}/metadata')

    def tray(self, method):
        """One request only. The engine persists intent; never retry an uncertain close."""
        if method not in ('close', 'eject'):
            raise ValueError('Tray operation must be explicit close or eject')
        drives = self.call('GET', 'drives')['drives']
        selected = [d for d in drives if d['mount'] == self.s.drive]
        if len(selected) != 1 or selected[0].get('job_id_current'):
            raise ValueError('Selected drive is missing, ambiguous, or busy')
        if method == 'close':
            if self.call('GET', 'system/ripping-enabled').get('ripping_enabled') is not False:
                self.call('POST', 'system/ripping-enabled', {'enabled': False})
                if self.call('GET', 'system/ripping-enabled').get('ripping_enabled') is not False:
                    raise ValueError('ARM did not retain scan pause before provisional naming')
            if not self.call('GET', 'recognition-helper/capabilities').get('enabled'):
                raise ValueError('Backend detention write hook is unavailable')
        # The installed close endpoint also starts a scan; do not send another scan.
        return self.call('POST', f"drives/{selected[0]['drive_id']}/eject", {'method': method})

    def _local(self, backend_path, *, failed_evidence=False):
        path = PurePosixPath(backend_path)
        root = PurePosixPath(self.s.arm_media)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('Unsafe ARM output path')
        relative = path.relative_to(root)
        local = Path(self.s.media) / str(relative)
        detention = Path(self.s.detention).resolve()
        if not local.resolve().is_relative_to(detention) or (not failed_evidence and not local.name.startswith('DETAINED_')):
            raise ValueError('ARM output is not a prefixed detention file')
        return str(local)

    def _sources(self, manifest, detail):
        sources = []
        for item in manifest['sources']:
            record = {**item, 'path': self._local(item['path'], failed_evidence=item.get('state') == 'failed')}
            if item.get('retained_files'):
                record['retained_files'] = [{**e, 'path': self._local(e['path'], failed_evidence=True)} for e in item['retained_files']]
            record['backend_metadata'] = {
                **item.get('backend_metadata', {}), 'backend_path': item['path'],
                'overlay_version': manifest['overlay_version'], 'runtime': manifest.get('runtime'),
                'cycle_id': manifest['cycle_id'], 'job_guid': manifest['job_guid'],
                'prescan': manifest.get('prescan', {'status': 'unavailable', 'limitation': 'Older overlay manifest'}),
                'audio_reconciliation': manifest.get('audio_reconciliation'),
                'job_identifiers': {k: detail['job'].get(k) for k in
                                    ('job_id', 'guid', 'crc_id', 'label', 'disctype', 'start_time', 'arm_version')},
            }
            sources.append(record)
        return sources

    def prepare_and_start(self, job_id, cycle_id, content_kind=None, recognition_finished=False, tv_context=None):
        detail = self.detail(job_id)
        if detail['job'].get('devpath') != self.s.drive:
            raise ValueError('Job is not from the selected physical drive')
        # This endpoint reserves outputs then signals manual_start. Same-cycle replay
        # returns the prior reservation, never resets files or creates another job.
        try:
            manifest = self.call('POST', f'jobs/{int(job_id)}/recognition-helper/start',
                                 {'cycle_id': cycle_id, 'content_kind': content_kind,
                                  'recognition_finished': recognition_finished, 'tv_context': tv_context})
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 409 and error.response.json().get('code') == 'selection_pending':
                raise SelectionPending(error.response.json()['selection']) from error
            raise
        return self._sources(manifest, detail)

    def queue_status(self):
        return self.call('GET', 'recognition-helper/queue')

    def deletion_check(self, job_id, guid, cycle_id):
        return self.call('POST', f'jobs/{int(job_id)}/recognition-helper/delete-check', {'job_guid':guid, 'cycle_id':cycle_id})

    def delete_finished(self, job_id, guid, cycle_id):
        return self.call('POST', f'jobs/{int(job_id)}/recognition-helper/delete', {'job_guid':guid, 'cycle_id':cycle_id})

    def selection_analysis(self, job_id):
        return self.call('GET', f'jobs/{int(job_id)}/recognition-helper/selection-analysis')

    def reconcile_drive(self, job_id, job_guid):
        return self.call('POST', 'recognition-helper/reconcile-drive', {'job_id': job_id, 'job_guid': job_guid})

    def recover(self, job_id, cycle_id):
        """Read-only reconciliation after all bounded start-response attempts failed."""
        detail = self.detail(job_id)
        manifest = self.call('GET', f'jobs/{int(job_id)}/recognition-helper')
        if manifest['cycle_id'] != cycle_id or detail['job'].get('devpath') != self.s.drive:
            raise ValueError('Recovered manifest belongs to another physical cycle/drive')
        return self._sources(manifest, detail)

    def completion(self, job_id, sources):
        detail = self.detail(job_id)
        manifest = self.call('GET', f'jobs/{int(job_id)}/recognition-helper')
        expected = {(int(s['source_id']), s.get('arm_track_id')) for s in sources}
        actual = {(int(s['source_id']), s.get('arm_track_id')) for s in manifest['sources']}
        if actual != expected:
            raise ValueError('Backend source association changed')
        for source in sources:
            old = source.get('backend_metadata', {})
            if old.get('job_guid') != manifest['job_guid'] or old.get('cycle_id') != manifest['cycle_id']:
                raise ValueError('Backend job/cycle association changed')
        status = detail['job']['status']
        # success briefly precedes the finally/ejection block. stop_time is the
        # backend's last committed finalization boundary, not track.ripped alone.
        done = status in ('success', 'fail') and bool(detail['job'].get('stop_time'))
        updated = self._sources(manifest, detail)
        for source in updated:
            if done:
                if source['state'] == 'complete' and source.get('finalized_at'):
                    path = Path(source['path'])
                    if not path.is_file() or path.stat().st_size != source.get('size_bytes'):
                        source.update(state='failed', error='Finalized output missing or size changed')
                else:
                    source.update(state='failed', error=source.get('error') or
                                  detail['job'].get('errors') or 'Source did not finalize')
            else:
                # No rename/release while any backend finalization is outstanding.
                source['extraction_state'] = source['state']
                if source['state'] == 'complete':
                    source['state'] = 'awaiting_finalization'
        return {'done': done, 'status': status, 'sources': updated, 'backend_metadata': detail}

    def close(self):
        self.client.close()
