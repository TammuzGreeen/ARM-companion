"""Selection policy around ARM Neu's shared native title ranker; no extraction."""
import os
import re
import unicodedata
import math
import hashlib
import json


def analyze_titles(tracks, scan=None):
    """Technical extraction evidence, not editorial identity or episode numbering."""
    scan = scan or {}
    rows = []
    for t in tracks:
        attrs = scan.get('sources', {}).get(str(int(t.track_number)), {}).get('title_attributes', {})
        field = lambda n: attrs.get(str(n), {}).get('value')
        raw = field(26)
        segments = [int(v) for v in raw.split(',')] if raw and re.fullmatch(r'\d+(,\d+)*', raw) else []
        seconds = float(t.length or 0)
        streams = scan.get('sources', {}).get(str(int(t.track_number)), {}).get('streams',{})
        rows.append({'source_id': int(t.track_number), 'duration_seconds': seconds if math.isfinite(seconds) else 0,
                     'chapters': getattr(t, 'chapters', None), 'playlist': field(16), 'segments': segments,
                     'stream_signature':hashlib.sha256(json.dumps(streams,sort_keys=True).encode()).hexdigest() if streams else None})
    duplicates, unique = {}, []
    for row in sorted(rows, key=lambda r:r['source_id']):
        peer = next((r for r in unique if row['segments'] and row['segments'] == r['segments']
                     and abs(row['duration_seconds']-r['duration_seconds']) <= 2
                     and ((row['stream_signature'] and row['stream_signature']==r['stream_signature']) or
                          (not row['stream_signature'] and not r['stream_signature'] and row['playlist'] and row['playlist']==r['playlist']))), None)
        if peer:
            duplicates[str(row['source_id'])] = peer['source_id']
        else:
            unique.append(row)
    # 4+ similarly sized 40–75 minute titles is a strong signal, not a universal
    # TV rule. Also support >=4 short-form episodes and >=3 segment-independent ones.
    candidates = [r for r in unique if 900 <= r['duration_seconds'] <= 4500]
    clusters = []
    for seed in candidates:
        group = [r for r in candidates if seed['duration_seconds'] <= r['duration_seconds'] <= seed['duration_seconds']*1.2]
        if len(group) >= 3:
            clusters.append(group)
    cluster = max(clusters, key=lambda g:(len(g),sum(r['duration_seconds'] for r in g)), default=[])
    independent = bool(cluster) and all(r['segments'] for r in cluster) and all(
        not set(a['segments']) & set(b['segments']) for i,a in enumerate(cluster) for b in cluster[i+1:])
    overlap = any(set(a['segments']) & set(b['segments']) for i,a in enumerate(cluster)
                  for b in cluster[i+1:] if a['segments'] and b['segments'])
    strong = bool(cluster) and not overlap and (independent or len(cluster) >= 4)
    combined = []
    for row in unique:
        if row in cluster or len(cluster) < 3:
            continue
        total = sum(r['duration_seconds'] for r in cluster)
        if abs(row['duration_seconds']-total) <= max(5,total*.02):
            union = set().union(*(set(r['segments']) for r in cluster))
            exact = independent and bool(row['segments']) and set(row['segments']) == union
            chapters = all(r['chapters'] for r in cluster) and row['chapters'] == sum(r['chapters'] for r in cluster)
            combined.append({'source_id':row['source_id'], 'status':'established segment union' if exact else 'possible Play All',
                             'exclude':bool(exact or (strong and chapters)),
                             'evidence':['duration approximates episode sum'] + (['disjoint episode segment union'] if exact else []) +
                                        (['chapter count equals episode sum'] if chapters else [])})
    unexplained = [r['source_id'] for r in unique if cluster and r not in cluster and r['duration_seconds']>max(x['duration_seconds'] for x in cluster)*1.5
                   and r['source_id'] not in {c['source_id'] for c in combined}]
    return {'episode_cluster_ids':[r['source_id'] for r in cluster], 'strong_tv_structure':strong,
            'unexplained_long_sources':unexplained,
            'independent_segments':independent, 'overlapping_episode_segments':overlap,
            'duplicate_sources':duplicates, 'combined_candidates':combined, 'titles':rows,
            'basis':'Technical hypothesis only: 3+ independent or 4+ similarly sized 15–75 minute titles; 4+ over 40 minutes is a strong signal. Duration alone does not prove identity.'}


class SelectionPending(ValueError):
    def __init__(self, report):
        super().__init__(report['reason'])
        self.report = report


def settings():
    mode = os.getenv('MOVIE_EXTRACTION_MODE', 'main_feature')
    minimum = int(os.getenv('MOVIE_MIN_LENGTH_SECONDS', '600'))
    extras = os.getenv('MOVIE_EXTRAS_POLICY', 'identified_only')
    tv_minimum = int(os.getenv('TV_MIN_LENGTH_SECONDS', '900'))
    if mode not in ('main_feature', 'all_titles') or minimum < 0 or tv_minimum < 0 or extras not in ('identified_only', 'none'):
        raise ValueError('Invalid movie extraction settings')
    return {'mode': mode, 'minimum_seconds': minimum, 'extras_policy': extras,
            'tv_minimum_seconds': tv_minimum}


def verified_tv_associations(job, tracks, context):
    """Literal source IDs require exact current full-scan scope, never a series name."""
    if not context:
        return {}
    reference = context.get('reference', {})
    scope = reference.get('disc_observation', {})
    signature = [{'source_id': int(t.track_number), 'duration_seconds': t.length}
                 for t in sorted(tracks, key=lambda t: int(t.track_number))]
    if not scope.get('label') or scope['label'] != getattr(job, 'label', None) or scope.get('source_signature') != signature:
        return {}
    episodes = context.get('episodes', [])
    reference_episodes = reference.get('facts', {}).get('episodes', [])
    ids = {int(t.track_number) for t in tracks}
    result = {}
    for row in reference.get('source_observations', []):
        assignment = row.get('assignment') or {}
        if assignment.get('kind') not in ('tv', 'episode', 'extra'):
            continue
        number, source = assignment.get('number'), row.get('source_id')
        if not isinstance(source, int) or isinstance(source, bool) or source not in ids or str(source) in result:
            raise ValueError('TV reference contradicts established episode coverage/current source IDs')
        if assignment.get('kind') == 'extra':
            norm = lambda v: re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKD', str(v)).casefold()).strip()
            title = assignment.get('title')
            observations = context.get('extra_observations', [])
            if not isinstance(title, str) or not title.strip() or len(observations) != 2 or not all(
                    any(norm(title) == norm(v) for v in values) for values in observations):
                continue  # no automatic extraction of unidentified extras
        elif not isinstance(number, int) or isinstance(number, bool) or number not in episodes or number not in reference_episodes:
            raise ValueError('TV reference contradicts established episode coverage/current source IDs')
        result[str(source)] = {**assignment, 'kind': 'extra' if assignment.get('kind') == 'extra' else 'tv',
            'evidence': 'Exact current disc label and full scan signature: ' + str(reference.get('id', 'reference'))}
    for assignment in result.values():
        if assignment['kind'] == 'extra':
            continue
        peers = [a for a in result.values() if a.get('number') == assignment['number'] and a['kind'] == 'tv']
        if len(peers) > 1 and (any(not a.get('version') for a in peers) or
                              len({a['version'] for a in peers}) != len(peers)):
            raise ValueError('TV reference has duplicate episode identities without distinct versions')
    return result


def select(job, tracks, ranker, content_kind=None, recognition_finished=False, tv_context=None, scan=None):
    cfg = settings()
    backend_kind = getattr(job, 'video_type', 'unknown')
    kind = {'series': 'tv'}.get(backend_kind, backend_kind)
    if kind not in ('movie', 'tv'):
        kind = content_kind if content_kind in ('movie', 'tv') else 'unknown'
    report = {'settings': cfg, 'backend_category': backend_kind, 'selection_category': kind,
              'ranker': 'ARM Neu chapters DESC, length DESC, filesize DESC, title ID ASC',
              'titles': [], 'selected_ids': [], 'status': 'selected'}
    enabled = [t for t in tracks if getattr(t, 'enabled', True) is not False
                and getattr(t, 'process', None) is not False]
    analysis = analyze_titles(enabled, scan)
    report['technical_analysis'] = analysis
    excluded_combined = {r['source_id'] for r in analysis['combined_candidates'] if r['exclude']}
    possible_combined = any(not r['exclude'] for r in analysis['combined_candidates'])
    if kind == 'unknown' and analysis['strong_tv_structure'] and not possible_combined and not analysis['unexplained_long_sources']:
        kind = 'tv'
        report['selection_category'] = 'tv'
        report['category_basis'] = 'provisional technical episode structure; recognition continues independently'
    enabled = [t for t in enabled if str(int(t.track_number)) not in analysis['duplicate_sources']
               and int(t.track_number) not in excluded_combined]
    pending = False
    if kind == 'tv':
        associations = verified_tv_associations(job, tracks, tv_context)
        # Only a corroborated combined relationship establishes a bounded episode
        # group. A duration cluster alone must not drop known-TV noncluster sources.
        eligible_ids = set(analysis['episode_cluster_ids']) if excluded_combined and analysis['strong_tv_structure'] else None
        selected = [t for t in enabled if str(int(t.track_number)) in associations or
                    (t.length is not None and max(cfg['tv_minimum_seconds'], int(job.config.MINLENGTH))
                     <= t.length <= int(job.config.MAXLENGTH) and (eligible_ids is None or int(t.track_number) in eligible_ids))]
        reason = 'ARM enabled flags and TV duration bounds with TV minimum; verified scoped episode/identified-extra exceptions only'
        report['verified_associations'] = associations
        excluded = [t for t in enabled if t not in selected]
        # A short title may be a legitimate episode. Do not freeze a partial
        # manifest before recognition can supply an exact scoped association.
        # A complete verified episode plan can proceed without waiting for vision.
        complete_plan = bool(associations and tv_context and
            {a['number'] for a in associations.values() if a['kind'] == 'tv'} == set(tv_context.get('episodes', [])))
        established = {c['source_id'] for c in analysis['combined_candidates'] if c['status']=='established segment union'}
        programme_segments = set().union(*(set(r['segments']) for r in analysis['titles'] if r['source_id'] in established))
        by_id = {r['source_id']:r for r in analysis['titles']}
        potential_short = any(str(int(t.track_number)) not in associations and
                              (t.length is None or (60 <= t.length < cfg['tv_minimum_seconds'] and
                               (not programme_segments or not by_id[int(t.track_number)]['segments'] or
                                set(by_id[int(t.track_number)]['segments']) & programme_segments))) for t in excluded)
        pending = not recognition_finished and (not selected or potential_short or bool(excluded) and not analysis['strong_tv_structure']) and not complete_plan
        if analysis['overlapping_episode_segments'] or possible_combined:
            selected = []
            pending = not recognition_finished
            reason = 'Overlapping/possible combined playlists require explicit source evidence; no arbitrary extraction'
            report['status'] = 'ambiguous'
        if pending:
            selected = []
            reason = 'TV selection includes potentially short programmes; awaiting recognition/reference before freezing episode reservation'
            report['status'] = 'ambiguous'
    else:
        eligible = [t for t in enabled if t.length is not None and
                    max(cfg['minimum_seconds'], int(job.config.MINLENGTH)) <= t.length <= int(job.config.MAXLENGTH)]
        ranked = ranker(eligible)
        # Unknown discs may proceed without vision only when technical evidence
        # establishes one dominant programme, not several episode/cut candidates.
        comparable = ranked and any(float(t.length) >= float(ranked[0].length) * .8 for t in ranked[1:])
        dominant = ranked and (len(ranked) == 1 or all(
            float(t.length) < float(ranked[0].length) * .5 and
            float(ranked[0].length) - float(t.length) >= 600 for t in ranked[1:]))
        if kind == 'unknown' and (not dominant or analysis['episode_cluster_ids'] or analysis['combined_candidates']
                                  or sum((t.length or 0) >= 1200 for t in enabled) >= 3):
            selected = []
            reason = 'Technical programme selection ambiguous; awaiting semantic category, not extracting all titles'
            report.update(status='ambiguous', reason=reason)
            pending = not recognition_finished
        elif getattr(job, 'multi_title', False) and cfg['mode'] == 'main_feature' or comparable and cfg['mode'] == 'main_feature':
            selected = []
            reason = 'Multiple comparable features/cuts remain; no arbitrary main-feature association'
            report['status'] = 'ambiguous'
        elif cfg['mode'] == 'all_titles' and kind == 'movie':
            selected = eligible
            reason = 'Explicit all_titles movie mode with configured duration bounds'
        else:
            selected = ranked[:1]
            reason = 'Native ARM rank after duration filtering; one dominant technical programme' if kind == 'unknown' else 'Native ARM main-feature rank after duration filtering'
    ids = {int(t.track_number) for t in selected}
    if not selected:
        report['status'] = 'ambiguous' if report['status'] == 'ambiguous' else 'no_eligible_titles'
    report.update(selected_ids=sorted(ids), reason=reason)
    for track in tracks:
        number = int(track.track_number)
        report['titles'].append({'source_id': number, 'arm_track_id': track.track_id,
            'duration_seconds': track.length, 'chapters': getattr(track, 'chapters', 0),
            'size_bytes': getattr(track, 'filesize', 0), 'selected': number in ids,
             'reason': reason if number in ids else ('combined Play All excluded by technical relationship' if number in excluded_combined else
                        'duplicate segment playlist' if str(number) in analysis['duplicate_sources'] else 'ARM disabled/filtered' if track not in enabled else
                       'unknown TV duration; not an ordinary episode candidate' if kind == 'tv' and track.length is None else
                       'below configured TV minimum' if kind == 'tv' and track.length < cfg['tv_minimum_seconds'] else
                       'below configured movie minimum' if kind != 'tv' and (track.length or 0) < cfg['minimum_seconds'] else
                       'not selected; original scan evidence retained')})
    if pending:
        raise SelectionPending(report)
    return selected, report
