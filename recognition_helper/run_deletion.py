"""Explicit, preview-bound cleanup of one finalized managed run. Never automatic."""
import contextlib
import copy
import os
from pathlib import Path
import secrets
import stat
import time
import re
from uuid import UUID

import httpx

FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def proof(s):
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def local_path(engine, value):
    """Historical expected_path uses ARM's mount prefix; final path is helper-local."""
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts:
        raise ValueError('Unsafe recorded output path')
    arm_root = Path(engine.s.arm_media)
    return engine.s.media/p.relative_to(arm_root) if p.is_relative_to(arm_root) else p


@contextlib.contextmanager
def opened(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Unsafe managed path')
    fd = os.open('/', FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def inventory(root):
    files, directories = [], []
    if not root.exists() and not root.is_symlink():
        return {'root_inode':None, 'files':[], 'directories':[]}
    with opened(root) as fd:
        root_inode = proof(os.fstat(fd))[:2]
        def walk(parent, relative):
            for name in sorted(os.listdir(parent)):
                p = relative / name
                s = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISDIR(s.st_mode):
                    child = os.open(name, FLAGS, dir_fd=parent)
                    try:
                        if proof(os.fstat(child))[:2] != proof(s)[:2]:
                            raise ValueError('Directory changed while inspecting run')
                        directories.append({'relative':str(p), 'inode':proof(s)[:2]})
                        walk(child, p)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(s.st_mode) and s.st_nlink == 1:
                    files.append({'relative':str(p), 'path':str(root/p), 'size_bytes':s.st_size, 'proof':proof(s)})
                else:
                    raise ValueError('Symlink, shared hardlink or special file prevents safe run deletion: '+str(p))
                if len(files)+len(directories) > 10000:
                    raise ValueError('Run inventory exceeds safety limit')
        walk(fd, Path())
    return {'root_inode':root_inode, 'files':files, 'directories':directories}


def backend(engine, cycle):
    try:
        return engine.arm.deletion_check(cycle['job_id'], cycle['job_guid'], cycle['id'])
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 404 or not cycle.get('deletion', {}).get('arm_attempted'):
            raise
        if any(j['job_id']==cycle['job_id'] for j in engine.arm.jobs()):
            raise ValueError('Numeric ARM job ID exists again; do not delete it')
        return {'missing':True, 'job_id':cycle['job_id'], 'job_guid':cycle['job_guid'], 'cycle_id':cycle['id']}


def inspect(engine, cycle):
    if not cycle.get('backend_done') or cycle.get('fallback_pending') or not cycle.get('local_done'):
        raise ValueError('Run still has extraction/recognition work; deletion unavailable')
    if cycle['id'] in engine.releasing or cycle['id'] in engine.recognizing:
        raise ValueError('Run still owns a helper worker')
    if any(r.get('status')=='running' for r in cycle.get('local_requests', [])):
        raise ValueError('Recognition request is still active')
    if any(s.get('state')=='released' for s in cycle['sources']):
        raise ValueError('Run contains released media; ordinary Delete run is prohibited')
    if any(s.get('state') not in ('complete','failed') for s in cycle['sources']):
        raise ValueError('A source is writing, tagging or finalizing')
    guid = str(UUID(cycle['job_guid']))
    root = engine.s.detention / f"job-{int(cycle['job_id'])}-{guid}"
    check = backend(engine, cycle)
    if not check.get('missing'):
        expected = str(Path(engine.s.arm_media)/'detention'/root.name)
        if check.get('root') != expected or check.get('job_guid') != guid or check.get('cycle_id') != cycle['id']:
            raise ValueError('Backend cleanup identity/root mismatch')
    for source in cycle['sources']:
        values = [source.get('path'),source.get('expected_path')]
        values += [r.get('path') for r in source.get('retained_files',[]) if r.get('path')]
        for value in filter(None,values):
            p = local_path(engine,value)
            if not p.is_absolute() or '..' in p.parts or not p.is_relative_to(root):
                raise ValueError('Recorded output is outside the verified job root; manual inspection required')
    snapshot = inventory(root)
    allowed = {str(root/'.recognition-helper.json'),str(root/'.extraction-progress.json')}
    audio_parents = set()
    for source in cycle['sources']:
        allowed.update(str(local_path(engine,p)) for p in (source.get('path'),source.get('expected_path')) if p)
        allowed.update(str(local_path(engine,r['path'])) for r in source.get('retained_files',[]) if r.get('path'))
        expected = local_path(engine,source.get('expected_path') or source['path'])
        if expected.suffix.lower()=='.flac' and expected.parent.is_relative_to(root):
            audio_parents.add(expected.parent)
            allowed.add(str(expected.parent/'.abcde.conf'))
    for item in snapshot['files']:
        p = Path(item['path'])
        audio_state = any(p.parent.parent==parent/'work' and
                          re.fullmatch(r'abcde\.[0-9a-fA-F]+',p.parent.name) and
                          p.name in ('cddbdiscid','mbdiscid','status') for parent in audio_parents)
        if item['path'] not in allowed and not audio_state:
            raise ValueError('Unregistered file in run directory; manual inspection required: '+item['relative'])
    shared = set()
    shared_paths = set()
    for other in engine.store.cycles.values():
        if other['id'] == cycle['id']:
            continue
        for source in other['sources']:
            paths = [source.get(k) for k in ('path','expected_path','original_detained_path')]
            paths += [r.get('path') for r in source.get('retained_files', [])]
            for value in filter(None, paths):
                p = local_path(engine,value)
                shared_paths.add(str(p))
                try:
                    s = p.stat()
                    shared.add((s.st_dev,s.st_ino))
                except FileNotFoundError:
                    pass
    for item in snapshot['files']:
        if item['path'] in shared_paths or tuple(item['proof'][:2]) in shared:
            raise ValueError('A file is referenced by another run; deletion prohibited')
    return {'job_id':cycle['job_id'], 'cycle_id':cycle['id'], 'job_guid':guid,
            'identity':copy.deepcopy((cycle.get('decision') or {}).get('identity', {})),
            'status':cycle['state'], 'root':str(root), 'snapshot':snapshot,
            'files':snapshot['files'], 'file_count':len(snapshot['files']),
            'total_size_bytes':sum(f['size_bytes'] for f in snapshot['files']),
            'backend':check, 'safe':True,
            'operation':'Delete only inventoried managed-run files/empty directories, then installed ARM job cleanup. Released files and diagnostic cycle record remain.'}


def preview(engine, cycle):
    if cycle.get('deletion', {}).get('status')=='deleted':
        return {'safe':False, 'reason':'Run already deleted', 'audit':copy.deepcopy(cycle['deletion'])}
    try:
        result = inspect(engine, cycle)
    except (ValueError, OSError, httpx.HTTPError, KeyError) as error:
        return {'safe':False, 'reason':str(error), 'job_id':cycle.get('job_id'), 'cycle_id':cycle['id']}
    result['token'] = secrets.token_urlsafe(32)
    cycle['delete_preview'] = {'token':result['token'], 'at':time.time(), 'plan':copy.deepcopy(result)}
    engine.store.save(cycle)
    return result


def execute(engine, cycle, token, confirmed_job_id):
    if cycle.get('deletion', {}).get('status')=='deleted':
        return copy.deepcopy(cycle['deletion'])
    saved = cycle.get('delete_preview', {})
    if (not token or not secrets.compare_digest(token, saved.get('token','')) or
            time.time()-saved.get('at',0)>600 or confirmed_job_id != cycle.get('job_id')):
        raise ValueError('Fresh job-specific deletion preview and explicit confirmation required')
    current = inspect(engine, cycle)
    plan = saved['plan']
    if current['snapshot'] != plan['snapshot'] or current['job_guid'] != plan['job_guid']:
        raise ValueError('Files changed since confirmation preview; inspect a new preview')
    cycle.pop('delete_preview',None)  # No replay of a partially successful operation.
    audit = cycle.setdefault('deletion', {'deleted_files':[], 'deleted_size_bytes':0, 'attempts':[]})
    attempt = {'at':time.time(), 'errors':[]}
    audit['attempts'].append(attempt)
    audit.update(status='partial', job_id=cycle['job_id'], cycle_id=cycle['id'])
    engine.store.event(cycle, 'explicit run deletion started; no automatic retry')
    root = Path(plan['root'])
    try:
        def delete_file(item):
            with opened(root/Path(item['relative']).parent) as fd:
                s = os.stat(Path(item['relative']).name, dir_fd=fd, follow_symlinks=False)
                if proof(s) != item['proof'] or not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
                    raise ValueError('File changed during deletion: '+item['relative'])
                os.unlink(Path(item['relative']).name, dir_fd=fd)
                os.fsync(fd)
            audit['deleted_files'].append(item['path'])
            audit['deleted_size_bytes'] += item['size_bytes']
            engine.store.save(cycle)
        manifest = [f for f in plan['files'] if f['relative']=='.recognition-helper.json']
        for item in plan['files']:
            if item not in manifest:
                delete_file(item)
        # No recursive delete: remove only explicitly inventoried, now-empty directories.
        for item in sorted(plan['snapshot']['directories'],key=lambda d:len(Path(d['relative']).parts),reverse=True):
            p = root/item['relative']
            with opened(p.parent) as fd:
                if proof(os.stat(p.name, dir_fd=fd, follow_symlinks=False))[:2] != item['inode']:
                    raise ValueError('Directory changed during deletion')
                os.rmdir(p.name, dir_fd=fd)
        # Keep the manifest/root until guarded ARM deletion has checked cycle ownership.
        audit['arm_attempted'] = True
        engine.store.save(cycle)
        if not current['backend'].get('missing'):
            audit['arm_result'] = engine.arm.delete_finished(cycle['job_id'],cycle['job_guid'],cycle['id'])
        else:
            audit['arm_result'] = {'already_absent':True}
        if any(j['job_id']==cycle['job_id'] for j in engine.arm.jobs()):
            raise ValueError('ARM job still exists or numeric ID was reused')
        audit['arm_removed'] = True
        for item in manifest:
            delete_file(item)
        if root.exists():
            with opened(root.parent) as fd:
                if proof(os.stat(root.name, dir_fd=fd, follow_symlinks=False))[:2] != plan['snapshot']['root_inode']:
                    raise ValueError('Run root changed')
                os.rmdir(root.name, dir_fd=fd)
        engine.queue = engine.arm.queue_status()
        audit['queue_result'] = copy.deepcopy(engine.queue)
        ready = (not engine.queue.get('optical_processes') and any(d.get('mount')==engine.s.drive and
                 d.get('state')=='ready' and not d.get('job_id') for d in engine.queue.get('drives',[])))
        if not ready:
            raise ValueError('ARM job removed but drive/queue is not ready; inspect queue status')
        audit.update(status='deleted', deleted_at=time.time())
        cycle['state'] = 'deleted'
        cycle['ejection_observed'] = True
    except Exception as error:
        attempt['errors'].append(type(error).__name__+': '+str(error))
    engine.store.event(cycle, 'explicit run deletion result', result=audit['status'], errors=attempt['errors'])
    return copy.deepcopy(audit)
