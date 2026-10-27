"""Field-level audio precedence. Catalogue evidence never becomes physical fact."""
import copy
import re
import unicodedata

from .decisions import compatible, norm


MATERIAL_QUALIFIER = r'volume|vol|part|book|band|teil|folge|chapter|disc|cd|cut|fassung|episode'


def partial_title(left, right):
    """Contiguous meaningful words, not arbitrary substrings or bag-of-words."""
    a, b = norm(left).split(), norm(right).split()
    short, long = sorted((a, b), key=len)
    return bool(short) and (a == b or len(short) >= 2 and any(
        long[i:i + len(short)] == short for i in range(len(long) - len(short) + 1)))


def common_work_title(values):
    """Use an actually supplied common title, never invent a shared prefix."""
    if not values or any(not isinstance(v, str) or not v.strip() for v in values):
        return None
    shortest = min(values, key=lambda v: (len(norm(v).split()), len(v)))
    if all(norm(v) == norm(shortest) for v in values):
        return shortest
    # A colon is punctuation, not proof that the suffix is an edition label.
    # Restrict trimming to explicit general edition/recording qualifiers; work,
    # volume, cut and part distinctions remain material even alongside a label.
    suffixes = [v.split(':', 1)[1] for v in values if ':' in v]
    material = r'\b(' + MATERIAL_QUALIFIER + r')\b'
    edition = r'\b(edition|ausgabe|anniversary|collector|collectors|deluxe|remaster(?:ed)?|live|unabridged|uncut|ungek[uü]rzt|complete readings)\b'
    if not suffixes or any(re.search(material, s, re.IGNORECASE) or
                           not re.search(edition, s, re.IGNORECASE) for s in suffixes):
        return None
    # Only a suffix separated as a subtitle can vary between catalogue editions.
    # 'Volume One' versus 'Volume Two' must not collapse to 'Volume'.
    if all(norm(v) == norm(shortest) or
           (':' in v and norm(v.split(':', 1)[0]) == norm(shortest)) for v in values):
        return shortest
    bases = [v.split(':', 1)[0].strip() for v in values]
    if all(norm(v) == norm(bases[0]) for v in bases):
        return bases[0]
    return None


def reconcile_audio_work(cycle, decision, catalogue_titles):
    """Final effective work identity; raw observations and conflicts remain audit data."""
    identity, conflicts = decision['identity'], decision['conflicts']
    report = decision['audio_metadata']
    common = common_work_title(catalogue_titles)
    if not common:
        return
    roles = [identity[field] for field in ('author', 'artist', 'performer', 'narrator')
             if field not in conflicts and isinstance(identity.get(field), str)
             and norm(identity[field]) not in ('unknown', 'various', 'various artists', 'unbekannt')]

    def without_names(value):
        words = norm(value).split()
        for name in sorted(roles, key=len, reverse=True):
            tokens = norm(name).split()
            if not tokens:
                continue
            for start in range(len(words) - len(tokens), -1, -1):
                if words[start:start + len(tokens)] == tokens:
                    words[start:start + len(tokens)] = []
        return ' '.join(words)

    def agrees(value):
        raw = value
        value = without_names(value)
        markers = lambda text: re.findall(r'\b(' + MATERIAL_QUALIFIER + r')\s+(\w+)', text)
        for title in catalogue_titles:
            normalized = without_names(title)
            if value == normalized:
                return True
            if not value or markers(value) != markers(normalized):
                continue
            if ':' in raw and norm(raw.split(':', 1)[0]) == norm(common) and not common_work_title([raw, common]):
                continue
            if partial_title(value, normalized):
                return True
        return False

    # Music uses album; spoken work uses book. 'title' is an alias only when
    # its observed meaning has actually been checked against this same work.
    for field in ('title', 'book', 'album'):
        disagreement = conflicts.get(field)
        values = [v for _, v in disagreement] if disagreement else [identity.get(field)]
        values = [v for v in values if v not in (None, '')]
        if values and not all(agrees(v) for v in values):
            if field == 'title':
                conflicts.setdefault(field, [('effective visual title', identity.get(field)),
                                             ('corroborated work', common)])
            continue
        if field == 'title' and not values:
            continue
        if disagreement:
            report.setdefault('resolved_conflicts', {})[field] = {
                'original': copy.deepcopy(disagreement), 'selected': common,
                'basis': 'compatible work descriptions and physically corroborated catalogue identity'}
            conflicts.pop(field)
        identity[field] = common
        decision['attribution'][field] = ['common work identity across physically corroborated MusicBrainz candidates']
    report['work_identity'] = {'selected': common, 'candidate_titles': list(catalogue_titles),
                               'release_id_selected': None}
    if len({norm(v) for v in catalogue_titles}) > 1:
        report['edition_uncertainties'] = [{'field': 'album', 'candidates': list(catalogue_titles),
            'reason': 'Optional catalogue subtitle differs; only common supplied work title is effective'}]


def apply_audio_metadata(cycle, decision):
    sources = cycle.get('sources', [])
    observations = [o.get('findings', {}) for o in cycle.get('locals', [])]
    kinds = {o.get('media_type') for o in observations} - {None, 'unknown'}
    if not sources or kinds - {'music', 'audiobook'} or not all(
            s.get('backend_metadata', {}).get('source_number_kind', '').startswith(('CD TOC', 'ARM audio'))
            for s in sources):
        return False  # TV/movie precedence is deliberately untouched.
    identity, conflicts = decision['identity'], decision['conflicts']
    attribution = decision['attribution']
    report = decision['audio_metadata'] = {'fields': {}, 'candidates': [],
        'original_conflicts': copy.deepcopy(conflicts),
        'visual_tracks': [o.get('tracks', []) for o in observations],
        'extracted_sources': sum(s.get('state') in ('complete', 'released', 'awaiting_finalization') for s in sources),
        'backend_source_count': len(sources)}
    evidence = sources[0]['backend_metadata'].get('prescan', {})
    toc = evidence.get('tracks', [])
    physical = {int(t['number']): t for t in toc}
    ids = [int(s['source_id']) for s in sources]
    bound = evidence.get('status') == 'completed' and evidence.get('kind') == 'physical CD TOC via ARM libdiscid read'
    bound = bound and bool(physical) and len(physical) == len(toc) and len(set(ids)) == len(ids) and set(ids) == set(physical)
    bound = bound and all(s['backend_metadata'].get('prescan') == evidence and
                          s['backend_metadata'].get('physical_toc_track') == physical[int(s['source_id'])]
                          for s in sources)
    bound = bound and evidence.get('job_id') == cycle.get('job_id') and bool(evidence.get('job_guid')) and all(
        s['backend_metadata'].get('job_guid') == evidence['job_guid'] and
        s['backend_metadata'].get('cycle_id') == cycle.get('id') for s in sources)

    def select(field, value, basis):
        old = copy.deepcopy(identity.get(field))
        previous_conflict = conflicts.pop(field, None)
        identity[field] = copy.deepcopy(value)
        attribution[field] = [basis]
        report['fields'][field] = {'status': 'corroborated', 'basis': basis,
                                   'selected': copy.deepcopy(value), 'previous': old,
                                   'previous_conflict': previous_conflict}

    report['physical_track_count'] = len(physical) if bound else None
    if toc and not bound:
        decision.setdefault('mapping_reasons', []).append('Physical TOC/source association is not verified')
    if bound:
        select('tracks', sorted(physical), 'physical CD TOC and stable backend source association')
        for source in sources:
            source['identified_assignment'] = {'number': int(source['source_id']),
                'evidence': 'physical CD TOC number bound to backend source/ARM track ID'}
    mb = evidence.get('musicbrainz', {})
    plausible, verified = [], []
    for candidate in mb.get('candidates', []):
        reason = 'reliability_not_established'
        tracks = candidate.get('tracks', [])
        try:
            numbers = [int(t['number']) for t in tracks]
            rowmap = {int(t['number']): t for t in tracks}
            disc_match = bool(evidence.get('disc_id')) and evidence['disc_id'] in candidate.get('disc_ids', [])
            if bound and disc_match and candidate.get('format') == 'CD' and mb.get('lookup_disc_id') == evidence['disc_id']:
                offsets = [int(physical[n]['offset_frames']) for n in sorted(physical)]
                supplied_offsets = [d.get('offset-list') for d in candidate.get('medium_discs', [])
                                    if d.get('id') == evidence['disc_id'] and d.get('offset-list') is not None]
                if mb.get('lookup_offsets') is not None:
                    supplied_offsets.append(mb['lookup_offsets'])
                if any([int(o) for o in supplied] != offsets for supplied in supplied_offsets):
                    reason = 'conflicts_with_physical_evidence'
                elif len(set(numbers)) != len(numbers) or set(numbers) != set(physical):
                    reason = 'conflicts_with_physical_evidence'
                elif any(t.get('duration_seconds') is not None and abs(float(t['duration_seconds']) -
                         float(physical[n]['duration_seconds'])) > 2 for n, t in rowmap.items()):
                    reason = 'conflicts_with_physical_evidence'
                elif all(t.get('duration_seconds') is not None for t in tracks):
                    reason = 'corroborated'
            if disc_match and reason != 'conflicts_with_physical_evidence':
                plausible.append(candidate)
                if reason == 'corroborated':
                    verified.append(candidate)
        except (ValueError, TypeError, KeyError):
            reason = 'reliability_not_established'
            plausible.append(candidate)  # malformed candidates cannot silently break a tie
        report['candidates'].append({'release_id': candidate.get('release_id'), 'status': reason})
    # A matching count alone, a selected ARM CRC, or a stub never earns authority.
    authoritative = bool(verified) and len(verified) == len(plausible)
    report['corroboration'] = ('disc ID, medium track numbers and all durations within 2s'
                               if authoritative else 'reliability_not_established')
    report['musicbrainz_track_counts'] = [len(c.get('tracks', [])) for c in mb.get('candidates', [])]
    if authoritative:
        basis = 'MusicBrainz field consensus across all physically corroborated release/medium candidates'
        for field in ('album', 'artist', 'year', 'disc', 'disc_total', 'part', 'author', 'narrator', 'performer'):
            values = [c.get('facts', {}).get(field) for c in verified]
            if any(v in (None, '') for v in values):
                continue
            common = common_work_title(values) if field == 'album' else None
            if common or all(compatible(field, values[0], v) for v in values):
                previous = identity.get(field)
                select(field, common or values[0], basis)
                if field == 'album' and previous:
                    report['fields'][field]['visual_relation'] = ('compatible_partial' if partial_title(previous, values[0])
                                                                 else 'contradictory_visual_guess')
            else:
                identity.pop(field, None)
                conflicts[field] = [('MusicBrainz:' + str(c.get('release_id')), c['facts'][field]) for c in verified]
                report['fields'][field] = {'status': 'ambiguous_release_candidates'}
        titles = {}
        mapping_conflicts = []
        for number in sorted(physical):
            values = [next(t for t in c['tracks'] if int(t['number']) == number).get('title') for c in verified]
            if all(values) and all(norm(v) == norm(values[0]) for v in values):
                titles[str(number)] = values[0]
            elif len({norm(v) for v in values if v}) > 1:
                mapping_conflicts.append({'source_id': number, 'candidate_titles': values})
        if titles:
            select('track_titles', titles, basis)
        if mapping_conflicts:
            conflicts['audio_mapping'] = [('corroborated candidates disagree on source identity', mapping_conflicts)]
            report['material_mapping_conflicts'] = mapping_conflicts
    else:
        # Uncorroborated backend fields may fill compatible gaps, not defeat vision.
        # Do not use ARM's arbitrarily selected first candidate when multiple remain.
        metadata = cycle.get('backend_editorial_metadata', {}) if len(mb.get('candidates', [])) <= 1 else {}
        contradicted = any(c['status'] == 'conflicts_with_physical_evidence' for c in report['candidates'])
        for field in ('album', 'artist', 'year'):
            value = metadata.get(field)
            if value in (None, ''):
                continue
            status = 'conflicts_with_physical_evidence' if contradicted else 'reliability_not_established'
            if not contradicted and field not in conflicts and field in identity and compatible(field, identity[field], value):
                attribution.setdefault(field, ['backend editorial metadata; not independently verified'])
                status = 'compatible_not_independently_verified'
            elif not contradicted and (field in identity or field in conflicts):
                fallback_value = cycle.get('fallback', {}).get('findings', {}).get(field)
                local_support = fallback_value not in (None, '', []) and any(
                    o.get(field) not in (None, '', []) and compatible(field, fallback_value, o[field])
                    for o in observations)
                if local_support and field in identity and compatible(field, fallback_value, identity[field]):
                    report.setdefault('rejected_backend_facts', {})[field] = value
                    status = 'rejected_by_fallback_with_local_support'
                else:
                    conflicts.setdefault(field, [('visual', identity.get(field))]).append(('backend editorial', value))
            report['fields'][field] = {'status': status, 'backend_value': value}
        if len(plausible) > 1:
            conflicts['audio_release'] = [('MusicBrainz', c.get('release_id')) for c in plausible]
    # Technical ARM "music" means audio-CD extraction, not musical content.
    # Existing audiobook bucket includes audio dramas/spoken word; author is optional.
    text = ' '.join([str(identity.get('album', ''))] + [str(o.get('visible_text', '')) for o in observations])
    text_plain = ''.join(c for c in unicodedata.normalize('NFKD', text).casefold() if not unicodedata.combining(c))
    spoken = bool(re.search(r'\b(horspiel|hoerspiel|audio drama|audio play|audiobooks?)\b', text_plain))
    if spoken and kinds <= {'music', 'audiobook'}:
        select('media_type', 'audiobook', 'explicit spoken-word/audio-drama text mapped to existing audiobook category')
    if not identity.get('media_type') and 'media_type' not in conflicts and identity.get('book'):
        identity['media_type'] = 'audiobook'
        attribution['media_type'] = ['identified spoken-work field on audio sources']
    # Reconcile partial audio names before deriving the required spoken-work title.
    for field in ('book', 'album'):
        values = conflicts.get(field, [])
        if values and (field != 'album' or not authoritative) and all(partial_title(a[1], b[1]) for a in values for b in values):
            identity[field] = max((v for _, v in values), key=lambda v: len(str(v)))
            attribution[field] = [source for source, _ in values]
            report['fields'][field] = {'status': 'compatible_not_independently_verified',
                                      'previous_conflict': conflicts.pop(field)}
    if identity.get('media_type') == 'audiobook':
        author_values = conflicts.get('author', [])
        known = [(source, value) for source, value in author_values
                 if norm(value) not in ('various', 'various artists', 'unknown', 'unbekannt')]
        if author_values and len(known) < len(author_values):
            report['rejected_author_guesses'] = author_values
            if not known:
                conflicts.pop('author')
            elif all(compatible('author', known[0][1], value) for _, value in known):
                identity['author'] = known[0][1]
                attribution['author'] = [source for source, _ in known]
                conflicts.pop('author')
        if norm(identity.get('author', '')) in ('various', 'various artists', 'unknown', 'unbekannt'):
            report['rejected_author'] = identity.pop('author')
            attribution.pop('author', None)
        if identity.get('album') and authoritative and 'album' not in conflicts:
            select('book', identity['album'], 'corroborated release title used as spoken-work title; not proof of authorship')
        if not identity.get('book') and 'book' not in conflicts:
            for field in ('album', 'title'):
                if identity.get(field) and field not in conflicts:
                    identity['book'] = identity[field]
                    attribution['book'] = attribution.get(field, ['compatible identified spoken-work title'])
                    break
        report['author_status'] = 'identified' if identity.get('author') else 'unknown_optional'
        # Optional disputes do not make an otherwise identified spoken work
        # unsafe. Preserve the disagreement, but don't publish a chosen guess.
        for field in ('author', 'narrator', 'performer', 'artist', 'year', 'track_titles'):
            if field in conflicts:
                report.setdefault('optional_disagreements', {})[field] = conflicts.pop(field)
                identity.pop(field, None)
                attribution.pop(field, None)
        report['author_status'] = 'identified' if identity.get('author') else 'unknown_optional'
    if authoritative and 'album' not in conflicts:
        reconcile_audio_work(cycle, decision, [c.get('facts', {}).get('album') for c in verified])
    return True
