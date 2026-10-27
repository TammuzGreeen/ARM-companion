"""Build-time, checksum-locked patch of the inspected installed ARM Neu sources."""
import hashlib
from pathlib import Path
import shutil
import sys

BASE_HASHES = {
    'arm/ripper/makemkv.py': '3e03e294661350e411233a46fac2208ce6085e73022e9ef8a4a83fe55b2276c0',
    'arm/ripper/naming.py': '1e0d9c1988e66145964c24658e06b01b22e56c7f0c61870d23375ebb5e105b07',
    'arm/ripper/utils.py': '3d34aa510477046b78f2d7a04e91d8fb2eb2c2f0162f03e148c5e6690da6c7aa',
    'arm/api/v1/jobs.py': '4c2722f8bfe8457c61b5eb78b064bff6e9f56871c2dd0331f4d667f7faad1e5e',
    'arm/ripper/arm_ripper.py': '31cbd2513d5f4a50f22e7d8e2e44914cda6c11037dedefda8f6f9904a9eeb7d3',
    'arm/ripper/music_brainz.py': '117cb253e0e280249794b2dd9bd1012abc96deaac46075174cadbc4686629e7e',
}
ABCDE_HASH = '26bfe6e223cfdd2f7d6648b0245e90a14961d28f64380fa4fb2a5847db38a12b'

ROUTES = '''

# recognition-helper: opt-in, independent extraction and actual-output manifest.
@router.get('/recognition-helper/capabilities')
def recognition_helper_capabilities():
    from arm.ripper import recognition_helper as helper
    return helper.capabilities()


@router.post('/jobs/{job_id}/recognition-helper/start')
def recognition_helper_start(job_id: int, body: dict):
    from arm.ripper import recognition_helper as helper
    job = Job.query.get(job_id)
    if not job:
        return JSONResponse({'success': False, 'error': 'Job not found'}, status_code=404)
    try:
        return helper.prepare_and_start(job, body.get('cycle_id'), body.get('content_kind'), body.get('recognition_finished', False), body.get('tv_context'))
    except helper.SelectionPending as exc:
        return JSONResponse({'success': False, 'code': 'selection_pending', 'selection': exc.report}, status_code=409)
    except (ValueError, FileExistsError) as exc:
        return JSONResponse({'success': False, 'error': str(exc)}, status_code=409)


@router.get('/jobs/{job_id}/recognition-helper')
def recognition_helper_outputs(job_id: int):
    from arm.ripper import recognition_helper as helper
    job = Job.query.get(job_id)
    if not job or not helper.managed(job):
        return JSONResponse({'success': False, 'error': 'No managed extraction'}, status_code=404)
    return helper.load(job)


@router.get('/recognition-helper/queue')
def recognition_helper_queue():
    from arm.ripper import recognition_helper as helper
    return helper.queue_state()


@router.post('/recognition-helper/reconcile-drive')
def recognition_helper_reconcile(body: dict):
    from arm.ripper import recognition_helper as helper
    return helper.reconcile_drive(body.get('job_id'), body.get('job_guid'))
'''

NATIVE_RANKER = '''

def rank_mainfeature_tracks(tracks):
    """Shared installed ARM Neu ordering, also used by managed selection."""
    return sorted(tracks, key=lambda t: (-(t.chapters or 0), -(t.length or 0),
                                        -(t.filesize or 0), int(t.track_number)))
'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Patch anchor does not occur exactly once: ' + old[:90])
    return text.replace(old, new, 1)


def apply(root, abcde):
    root, abcde = Path(root), Path(abcde)
    originals = {}
    # Check every base before changing any file. Unknown source is not silently patched.
    for relative, expected in BASE_HASHES.items():
        data = (root / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Unsupported installed source: ' + relative)
        originals[relative] = data.decode()
    if hashlib.sha256(abcde.read_bytes()).hexdigest() != ABCDE_HASH:
        raise ValueError('Unsupported abcde source')
    changes = dict(originals)
    name = 'arm/ripper/makemkv.py'
    changes[name] = replace_once(changes[name],
        '    for message in prescan_disc_info(job, timeout=timeout, cache_mb=cache_mb):',
        '    from arm.ripper import recognition_helper as helper\n'
        '    for message in helper.capture_prescan(job, prescan_disc_info(job, timeout=timeout, cache_mb=cache_mb)):')
    changes[name] = replace_once(changes[name], 'def run(options, select):',
                                'def run(options, select, env=None):')
    changes[name] = replace_once(changes[name],
        'proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, start_new_session=True)',
         'proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, start_new_session=True, env=env)')
    # Factor the installed rank (not a new longest-title algorithm) for reuse
    # by native and managed workflows.
    changes[name] = replace_once(changes[name],
        '            best = Track.query.filter_by(job_id=job.job_id).order_by(\n'
        '                Track.chapters.desc(),\n                Track.length.desc(),\n'
        '                Track.filesize.desc(),\n                Track.track_number.asc()\n            ).first()',
        '            ranked = rank_mainfeature_tracks(list(job.tracks))\n'
        '            best = ranked[0] if ranked else None')
    changes[name] += NATIVE_RANKER
    changes[name] = replace_once(changes[name], '    try:\n        logging.info(f"MakeMKV subprocess started: PID {proc.pid}")',
        '    guard = None\n    stdout_finished = False\n    _heartbeat_path = None\n'
        '    try:\n'
        '        from arm.ripper.recognition_helper_watchdog import Watchdog\n'
        '        guard = Watchdog(proc, env) if env and env.get("RH_RUNTIME_PATH") else None\n'
        '        if guard:\n            guard.start()\n'
        '        logging.info(f"MakeMKV subprocess started: PID {proc.pid}")')
    changes[name] = replace_once(changes[name], '        for line in proc.stdout:\n            line = line.rstrip(os.linesep)',
        '        for line in proc.stdout:\n            line = line.rstrip(os.linesep)\n            if guard:\n                guard.observe(line)')
    changes[name] = replace_once(changes[name], '        logging.info("MakeMKV stdout exhausted',
        '        stdout_finished = True\n        logging.info("MakeMKV stdout exhausted')
    changes[name] = replace_once(changes[name], '        proc.wait()\n        logging.info("proc.wait() returned, returncode=%s", proc.returncode)',
        '        if not stdout_finished and env and env.get("RH_RUNTIME_PATH"):\n'
        '            from arm.ripper.recognition_helper_watchdog import Watchdog\n'
        '            Watchdog.terminate(guard if guard else type("Owned", (), {"proc": proc})())\n'
        '        try:\n            proc.wait()\n        finally:\n            if guard:\n                guard.finish()\n'
        '        logging.info("proc.wait() returned, returncode=%s", proc.returncode)')
    changes[name] = replace_once(changes[name], '            os.remove(_heartbeat_path)',
        '            if _heartbeat_path:\n                os.remove(_heartbeat_path)')
    changes[name] = replace_once(changes[name], '    rawpath = setup_rawpath(job.build_raw_path())',
        '    from arm.ripper import recognition_helper as helper\n'
        '    if helper.managed(job):\n'
        '        return helper.rip_video(job, run, OutputType.MSG | OutputType.SINFO | OutputType.TINFO, progress_log(job))\n\n'
        '    rawpath = setup_rawpath(job.build_raw_path())')
    name = 'arm/ripper/naming.py'
    changes[name] = replace_once(changes[name], "    raw_path = getattr(job, 'raw_path', None)",
        '    from arm.ripper import recognition_helper as helper\n'
        '    if helper.managed(job):\n'
        '        return helper.finalize_video(job)\n\n'
        "    raw_path = getattr(job, 'raw_path', None)")
    name = 'arm/ripper/utils.py'
    changes[name] = replace_once(changes[name], '    abcfile = cfg.arm_config["ABCDE_CONFIG_FILE"]',
        '    from arm.ripper import recognition_helper as helper\n'
        '    if helper.managed(job):\n'
        '        return helper.rip_audio(job, logfile)\n\n'
        '    abcfile = cfg.arm_config["ABCDE_CONFIG_FILE"]')
    changes['arm/api/v1/jobs.py'] += ROUTES
    name = 'arm/ripper/music_brainz.py'
    changes[name] = replace_once(changes[name], '    return read(disc.devpath)',
        '    toc = read(disc.devpath)\n'
        '    from arm.ripper import recognition_helper as helper\n'
        '    helper.capture_cd_toc(disc, toc)\n'
         '    return toc')
    changes[name] = replace_once(changes[name],
        '    artist_title = check_musicbrainz_data(job, disc_info)',
        '    from arm.ripper import recognition_helper as helper\n'
        '    helper.capture_musicbrainz(job, disc_info)\n'
        '    artist_title = check_musicbrainz_data(job, disc_info)')
    name = 'arm/ripper/arm_ripper.py'
    changes[name] = replace_once(changes[name],
        '    # Compute final path for DB/webhook metadata\n'
        '    final_directory = job.build_final_path()\n\n'
        '    # Check folders for already ripped jobs -> creates folder (handles collisions)\n'
        '    final_directory = utils.check_for_dupe_folder(have_dupes, final_directory, job)\n\n'
        '    # Persist path to DB\n'
        "    utils.database_updater({'path': final_directory}, job)\n"
        '    # Save poster image from disc if enabled\n'
        '    utils.save_disc_poster(final_directory, job)',
        '    from arm.ripper import recognition_helper as helper\n'
        '    if helper.managed(job):\n'
        '        final_directory = str(helper.job_dir(job))\n'
        '    else:\n'
        '        final_directory = job.build_final_path()\n'
        '        final_directory = utils.check_for_dupe_folder(have_dupes, final_directory, job)\n'
        '        utils.save_disc_poster(final_directory, job)\n'
        "    utils.database_updater({'path': final_directory}, job)")
    for relative, text in changes.items():
        compile(text, relative, 'exec')
    script = abcde.read_text().replace('track$', '${RH_PREFIX}track$')
    if '${RH_PREFIX}track$' not in script:
        raise ValueError('abcde filename patch did not apply')
    for relative, text in changes.items():
        (root / relative).write_text(text)
    shutil.copyfile(Path(__file__).with_name('recognition_helper.py'),
                    root / 'arm/ripper/recognition_helper.py')
    for source, target in [('video_selection.py', 'recognition_helper_selection.py'),
                           ('watchdog.py', 'recognition_helper_watchdog.py')]:
        shutil.copyfile(Path(__file__).with_name(source), root / 'arm/ripper' / target)
    target = root / 'recognition-helper-abcde'
    target.write_text(script)
    target.chmod(0o755)


if __name__ == '__main__':
    apply(sys.argv[1] if len(sys.argv) > 1 else '/opt/arm',
          sys.argv[2] if len(sys.argv) > 2 else '/usr/bin/abcde')
