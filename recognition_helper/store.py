"""Atomic per-cycle structured and human-readable accountability records."""
import copy
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
        for path in self.root.glob('*/record.json'):
            record = json.loads(path.read_text())
            self.cycles[record['id']] = record

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

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(sorted(self.cycles.values(), key=lambda c: c['created'], reverse=True))
