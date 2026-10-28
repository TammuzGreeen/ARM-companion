"""Atomic per-cycle structured and human-readable accountability records."""
import copy
import hashlib
import json
import os
import threading
import time
import uuid


class Store:
    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.cycles = {}
        self._saved_content = {}
        for path in self.root.glob('*/record.json'):
            record = json.loads(path.read_text())
            self.cycles[record['id']] = record
            self._saved_content[record['id']] = self._content_digest(record)

    def create(self, **fields):
        with self.lock:
            cycle = dict(id=uuid.uuid4().hex, created=time.time(), history=[], sources=[],
                         locals=[], recognition_done=False, backend_done=False,
                         manual_required=False, **fields)
            self.cycles[cycle['id']] = cycle
            self.save(cycle)
            return cycle

    def event(self, cycle, action, **fields):
        cycle['history'].append(dict(at=time.time(), action=action, **fields))
        self.save(cycle)

    def save(self, cycle):
        with self.lock:
            digest = self._content_digest(cycle)
            if self._saved_content.get(cycle['id']) == digest:
                return False
            cycle['updated'] = time.time()
            folder = self.root / cycle['id']
            folder.mkdir(exist_ok=True, mode=0o700)
            text = json.dumps(cycle, ensure_ascii=False, indent=2)
            temporary = folder / 'record.json.new'
            with temporary.open('w') as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, folder / 'record.json')
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            # Keep records small while remaining genuinely human-readable.
            lines = [f'# Disc cycle {cycle["id"]}', '',
                     f'State: {cycle.get("state", "new")}',
                     f'ARM job: {cycle.get("job_id", "not yet associated")}',
                     f'Recognition: {"complete" if cycle["recognition_done"] else "pending"}',
                     f'Extraction: {cycle.get("backend_status", "pending")}', '', '## Outputs']
            for source in cycle['sources']:
                lines += [f'- Source {source["source_id"]}: {source.get("state", "detained")} — '
                          f'{source.get("path", "not written")}',
                          f'  Reason/assignment: {source.get("reason") or source.get("assignment")}']
            lines += ['', '## Decision and attribution', '```json',
                      json.dumps(cycle.get('decision', {}), ensure_ascii=False, indent=2), '```',
                       '', '## History']
            selection = next((s.get('backend_metadata', {}).get('video_selection')
                              for s in cycle['sources'] if s.get('backend_metadata', {}).get('video_selection')), None)
            if selection:
                lines[-1:-1] = ['## Source selection',
                    'Effective settings: ' + json.dumps(selection.get('settings', {})),
                    'Selected source IDs: ' + str(selection.get('selected_ids', [])),
                    'Skipped source IDs: ' + str([t['source_id'] for t in selection.get('titles', []) if not t['selected']]), '']
            for event in cycle['history']:
                lines.append(f'- {event["at"]}: {event["action"]} ' +
                             json.dumps({k: v for k, v in event.items() if k not in ('at', 'action')}, ensure_ascii=False))
            lines += ['', 'Full findings, source metadata, timestamps and paths: record.json']
            (folder / 'record.md').write_text('\n'.join(lines))
            self._saved_content[cycle['id']] = digest
            return True

    @staticmethod
    def _content_digest(cycle):
        # Ignore only the persistence timestamp, never evidence or audit history.
        content = {k: v for k, v in cycle.items() if k != 'updated'}
        return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()).digest()

    def summaries(self):
        """Copy only card fields under the lock, not the historical evidence corpus."""
        fields = ('id', 'created', 'updated', 'state', 'job_id', 'backend_status',
                  'recognition_done', 'technical_error', 'fallback_unavailable', 'snapshots')
        source_fields = ('source_id', 'state', 'path', 'basename', 'arm_track_id',
                         'proposed', 'reason', 'release_error', 'error')
        with self.lock:
            rows = []
            for cycle in sorted(self.cycles.values(), key=lambda c: c['created'], reverse=True):
                row = {key: copy.deepcopy(cycle[key]) for key in fields if key in cycle}
                if cycle.get('deletion'):
                    row['deletion'] = {'status': cycle['deletion'].get('status')}
                row['sources'] = []
                for source in cycle['sources']:
                    item = {key: copy.deepcopy(source[key]) for key in source_fields if key in source}
                    metadata = source.get('backend_metadata', {})
                    item['backend_metadata'] = {
                        'scan_duration': metadata.get('scan_duration'),
                        'duration_validation': {'actual_seconds': metadata.get('duration_validation', {}).get('actual_seconds')},
                        'output_probe': {'format': {'duration': metadata.get('output_probe', {}).get('format', {}).get('duration')}}}
                    row['sources'].append(item)
                rows.append(row)
            return rows

    def detail(self, cycle_id):
        with self.lock:
            return copy.deepcopy(self.cycles[cycle_id])

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(sorted(self.cycles.values(), key=lambda c: c['created'], reverse=True))
