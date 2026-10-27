"""Pure recognition merging, partial-reference enrichment and output naming."""
import re
import unicodedata
import json
import copy
from pathlib import Path

ALIASES = json.loads(Path(__file__).with_name('identity_aliases.json').read_text())


IDENTITY_FIELDS = ('media_type', 'series', 'season', 'episodes', 'title', 'year',
                   'artist', 'album', 'author', 'book', 'disc', 'part', 'tracks',
                   'episode_titles', 'extras', 'ambiguous', 'disc_total', 'narrator', 'performer',
                   'disc_total_scope', 'series_year', 'edition', 'edition_descriptors')


def norm(value):
    text = unicodedata.normalize('NFKD', str(value)).casefold()
    return re.sub(r'[^\w]+', ' ', text).strip()


def compatible(field, left, right):
    if left is None or right is None or left == [] or right == []:
        return True
    if isinstance(left, list) or isinstance(right, list):
        if field in ('episodes', 'tracks') and isinstance(left, list) and isinstance(right, list):
            return set(left).issubset(right) or set(right).issubset(left)
        return left == right
    a, b = norm(left), norm(right)
    if field == 'series':
        a, b = ALIASES['aliases'].get(a, a), ALIASES['aliases'].get(b, b)
        return a == b or b in ALIASES['broad_identities'].get(a, []) or a in ALIASES['broad_identities'].get(b, [])
    return a == b


def most_specific(field, values):
    if field in ('episodes', 'tracks'):
        return max(values, key=len)
    if field == 'series':
        def specificity(value):
            normalized = norm(value)
            canonical = ALIASES['aliases'].get(normalized, normalized)
            return (canonical not in ALIASES['broad_identities'], len(canonical), len(str(value)))
        return max(values, key=specificity)
    return max(values, key=lambda value: len(str(value)))


def merge(locals_, fallback=None, references=()):
    """Missing fields are not votes against an observation; retain all attribution."""
    merged, attribution, conflicts = {}, {}, {}
    observations = [dict(o.get('findings', {})) for o in locals_]
    omitted_series_years = []
    for index, observation in enumerate(observations):
        if observation.get('media_type') == 'tv' and observation.get('series_year') is not None and any(
                'year' in str(u).lower() and any(w in str(u).lower() for w in ('infer', 'unknown', 'not printed', 'unverified'))
                for u in observation.get('uncertainties', [])):
            omitted_series_years.append({'model': locals_[index].get('model'), 'value': observation.pop('series_year')})
    # Alias relationships are reference data, not release-specific code branches.
    aliases = {}
    for reference in references:
        series = reference.get('facts', {}).get('series')
        if series:
            for alias in reference.get('aliases', []):
                aliases.setdefault(norm(alias), {})[norm(series)] = series
    alias_ambiguities = []
    for observation in observations:
        options = aliases.get(norm(observation.get('series', '')), {})
        if len(options) == 1:
            observation['series'] = next(iter(options.values()))
        elif options:
            supported = {norm(other.get('findings', {}).get('series', '')) for other in locals_} & set(options)
            if len(supported) == 1:
                observation['series'] = options[next(iter(supported))]
            else:
                alias_ambiguities.append({'observed': observation.pop('series'), 'candidates': sorted(options.values())})
    for field in IDENTITY_FIELDS:
        values = [(o.get('model', f'local-{i+1}'), observations[i][field])
                  for i, o in enumerate(locals_)
                  if observations[i].get(field) not in (None, '', [])]
        if not values:
            continue
        if field in ('episode_titles', 'extras'):
            merged[field] = list(dict.fromkeys(item for _, value in values for item in value))
            attribution[field] = [name for name, _ in values]
            continue
        if any(not compatible(field, a[1], b[1]) for a in values for b in values):
            conflicts[field] = values
        else:
            merged[field] = most_specific(field, [v for _, v in values])
            attribution[field] = [name for name, _ in values]
    local_identity = copy.deepcopy(merged)
    local_attribution = copy.deepcopy(attribution)
    applied, reference_conflicts = enrich(merged, references, observations)
    for field, values in reference_conflicts.items():
        conflicts.setdefault(field, []).extend(values)
    for field, value in applied.get('facts', {}).items():
        if field not in merged:
            merged[field] = value
            attribution[field] = ['masterlist:' + applied['id']]
        elif (field in ('series', 'episodes', 'tracks') and compatible(field, merged[field], value)
              and most_specific(field, [merged[field], value]) != merged[field]):
            merged[field] = value
            attribution.setdefault(field, []).append('masterlist:' + applied['id'])
    resolved_fields = set()
    if fallback is not None:
        finding = dict(fallback.get('findings', {}))
        if (finding.get('media_type', merged.get('media_type')) == 'tv' and finding.get('series_year') is not None and any(
                'year' in str(u).lower() and any(w in str(u).lower() for w in ('infer', 'unknown', 'not printed', 'unverified'))
                for u in finding.get('uncertainties', []))):
            omitted_series_years.append({'model': fallback.get('model'), 'value': finding.pop('series_year')})
        for field in IDENTITY_FIELDS:
            value = finding.get(field)
            if value in (None, '', []):
                continue
            if field == 'ambiguous':
                merged[field] = value
                attribution[field] = [fallback.get('model', 'fallback')]
                conflicts.pop(field, None)
                continue
            if field in conflicts:
                local_support = [(locals_[i].get('model', f'local-{i+1}'), o[field])
                                 for i, o in enumerate(observations)
                                 if o.get(field) not in (None, '', []) and compatible(field, value, o[field])]
                if local_support:
                    merged[field] = value
                    attribution[field] = [fallback.get('model', 'fallback')] + [name for name, _ in local_support]
                    del conflicts[field]
                    resolved_fields.add(field)
            elif field not in merged or compatible(field, merged[field], value):
                merged[field] = most_specific(field, [merged.get(field, value), value])
                attribution.setdefault(field, []).append(fallback.get('model', 'fallback'))
            else:
                conflicts[field] = [('combined', merged[field]), ('fallback', value)]
        # Fallback may make a previously ambiguous partial reference applicable.
        if not conflicts:
            remaining_references = [ref for ref in references if all(
                field not in ref.get('facts', {}) or compatible(field, merged[field], ref['facts'][field])
                for field in resolved_fields)]
            if applied.get('facts') and not any(ref.get('id') == applied['id'] for ref in remaining_references):
                reference_id = 'masterlist:' + applied['id']
                for field in list(merged):
                    if reference_id in attribution.get(field, []):
                        if field in local_identity:
                            merged[field] = copy.deepcopy(local_identity[field])
                            attribution[field] = list(local_attribution.get(field, []))
                        else:
                            merged.pop(field)
                            attribution.pop(field, None)
                        supplied = finding.get(field)
                        if supplied not in (None, '', []) and compatible(field, merged.get(field), supplied):
                            merged[field] = most_specific(field, [merged.get(field, supplied), supplied])
                            attribution.setdefault(field, []).append(fallback.get('model', 'fallback'))
                applied = {'rejected_match': applied['id'], 'reason': 'Fallback resolved identity against this reference'}
            extra, extra_conflicts = enrich(merged, remaining_references, observations + [finding])
            conflicts.update({k: v for k, v in extra_conflicts.items() if k not in resolved_fields})
            if extra.get('facts'):
                applied = extra
                for field, value in extra['facts'].items():
                    if field not in merged:
                        merged[field] = value
                        attribution[field] = ['masterlist:' + extra['id']]
                    elif (field in ('series', 'episodes', 'tracks') and compatible(field, merged[field], value)
                          and most_specific(field, [merged[field], value]) != merged[field]):
                        merged[field] = value
                        attribution.setdefault(field, []).append('masterlist:' + extra['id'])
    if not merged.get('media_type') and 'media_type' not in conflicts:
        kinds = [kind for kind, fields in (('tv', ('series',)), ('movie', ('title',)),
                 ('music', ('artist', 'album')), ('audiobook', ('author', 'book')))
                 if all(merged.get(field) not in (None, '', []) for field in fields)]
        if len(kinds) == 1:
            merged['media_type'] = kinds[0]
            attribution['media_type'] = ['category derived from compatible naming fields']
    decision = {'identity': merged, 'attribution': attribution, 'conflicts': conflicts,
                'masterlist': applied, 'alias_ambiguities': alias_ambiguities}
    from .tv_metadata import reconcile_tv_metadata
    reconcile_tv_metadata(decision, list(locals_) + ([fallback] if fallback else []))
    if omitted_series_years and 'tv_metadata' in decision:
        decision['tv_metadata']['omitted_uncertain_series_years'] = omitted_series_years
    return decision


def enrich(identity, references, observations):
    candidates = []
    for ref in references:
        facts = ref.get('facts', {})
        anchors = sum(1 for k in ('series', 'season', 'disc', 'episodes', 'title', 'album', 'book')
                      if identity.get(k) not in (None, '', []) and facts.get(k) not in (None, '', [])
                      and compatible(k, identity[k], facts[k]))
        title_anchor = bool(set(map(norm, identity.get('episode_titles', []))) &
                            set(map(norm, ref.get('episode_titles', []))))
        if not anchors and not title_anchor:
            continue
        # A broad franchise alone must not choose a specific season/disc.
        distinguishing = any(identity.get(k) not in (None, '', []) and facts.get(k) not in (None, '', [])
                             for k in ('season', 'disc', 'episodes', 'title', 'album', 'book'))
        if not distinguishing and not title_anchor:
            continue
        mismatches = {k for k, v in identity.items() if k in facts and not compatible(k, v, facts[k])}
        # Preserve a reference supported by one local even when the other contradicts it.
        supported = any(sum(bool(o.get(k) not in (None, '', []) and facts.get(k) not in (None, '', [])
                                 and compatible(k, o[k], facts[k]))
                            for k in ('series', 'season', 'disc', 'episodes', 'title', 'album', 'book')) >= 2
                        for o in observations)
        if mismatches and not supported:
            continue
        candidates.append((ref, mismatches, title_anchor))
    # A known matching disc/edition must not compete with positively contradicted
    # discs from the same season. Keep contradiction handling when none match.
    compatible_candidates = [entry for entry in candidates if not entry[1]]
    if compatible_candidates:
        candidates = compatible_candidates
    # Missing disc/season fields are not counter-votes. Never select a reference
    # merely because it happens to contain more of those optional fields.
    # A positively matching observed episode title is a distinct corroborating
    # release clue (the specification's partial-reference example), not a score.
    title_matches = [entry for entry in candidates if entry[2]]
    if title_matches:
        candidates = title_matches
    if len(candidates) != 1:
        return {'candidates': [r['id'] for r, _, _ in candidates]}, {}
    ref, mismatches, title_match = candidates[0]
    conflicts = {k: [('masterlist:' + ref['id'], ref['facts'][k]),
                     *[('local', o[k]) for o in observations if o.get(k) not in (None, '', [])]]
                 for k in mismatches}
    return dict(ref, match_basis='observed episode title' if title_match else 'unique compatible observations'), conflicts


def safe(value):
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '-', str(value)).strip(' .')[:160]
    if not value:
        raise ValueError('A naming field is empty')
    return value


def minimum(identity):
    kind = identity.get('media_type')
    required = {'tv': ('series', 'season', 'episodes'), 'movie': ('title',),
                'music': ('artist', 'album', 'tracks'),
                 'audiobook': ('book', 'tracks')}.get(kind)
    if required is None:
        return ['Media category is not identified']
    reasons = ['Missing ' + k for k in required if identity.get(k) in (None, '', [])]
    if kind == 'tv' and norm(identity.get('series', '')) in ALIASES['broad_identities']:
        reasons.append('Series identity is only a broad franchise; actual series is not identified')
    if identity.get('ambiguous'):
        reasons.append('Identity remains ambiguous')
    return reasons


def audio_track_title(identity, number):
    """Remove only a redundant matching numeric prefix, never arbitrary digits."""
    title = identity.get('track_titles', {}).get(str(number))
    if not title:
        return None
    match = re.match(r'^\s*0*(\d+)\s*[-.:]\s*(.+)$', str(title))
    if match and int(match[1]) == int(number):
        return match[2].strip()
    return str(title).strip()


def filename(identity, assignment, suffix):
    kind = assignment.get('kind', identity.get('media_type'))
    version = ' - ' + safe(assignment['version']) if assignment.get('version') else ''
    if kind == 'extra':
        return Path('extras') / (safe(assignment['title']) + version + suffix)
    if kind in ('tv', 'episode'):
        series, season = safe(identity['series']), int(identity['season'])
        ep = int(assignment['number'])
        directory = series + (f' ({int(identity["series_year"])})' if identity.get('series_year') else '')
        episode_title = identity.get('episode_names', {}).get(str(ep))
        label = ' - ' + safe(episode_title) if episode_title else ''
        return Path('tv') / directory / f'Season {season:02d}' / f'{series} - S{season:02d}E{ep:02d}{label}{version}{suffix}'
    if kind == 'movie':
        title = safe(identity['title']) + (f" ({int(identity['year'])})" if identity.get('year') else '')
        part = f" - Part {int(assignment['number']):02d}" if assignment.get('number') is not None else ''
        return Path('movies') / title / (title + part + version + suffix)
    if kind in ('music', 'audiobook', 'track'):
        audio_book = identity.get('media_type') == 'audiobook'
        album = safe(re.sub(r'\s*:\s*', ' - ', str(identity['book']))) if audio_book else safe(identity['album'])
        track_title = audio_track_title(identity, assignment['number'])
        label = ' - ' + safe(track_title) if track_title else ''
        if audio_book:
            if identity.get('year') not in (None, ''):
                album += ' (' + safe(identity['year']) + ')'
            directory = Path('audiobooks') / album
            part = 'Part ' + safe(identity['part']) + ' - ' if identity.get('part') not in (None, '') else ''
            # Keep known disc ordering in the basename without mandatory nesting.
            disc = f'Disc {safe(identity["disc"])} - ' if identity.get('disc') is not None else ''
            return directory / f'{part}{disc}{int(assignment["number"]):02d}{label}{version}{suffix}'
        creator = safe(identity['artist'])
        parts = []
        if identity.get('part') not in (None, ''):
            parts.append('Part ' + safe(identity['part']))
        if identity.get('disc') is not None:
            parts.append('Disc ' + safe(identity['disc']))
        prefix = ' - '.join(parts) + ' - ' if parts else ''
        return Path('music') / creator / album / f'{prefix}{int(assignment["number"]):02d}{label}{version}{suffix}'
    raise ValueError('Unknown assignment kind')


def map_outputs(decision, sources, locals_=(), assignments=None):
    identity = decision['identity']
    reasons = minimum(identity) if assignments is None else []
    if assignments is None:
        reasons.extend(decision.get('mapping_reasons', []))
    if decision.get('conflicts'):
        reasons.append('Unresolved conflicting fields: ' + ', '.join(decision['conflicts']))
    ordered = sorted(sources, key=lambda s: int(s['source_id']))
    mapping = dict(assignments or {})
    source_reasons = {}
    established_ids = set()
    coverage_reason = None
    if assignments is None:
        for source in ordered:
            extra = source.get('extra_identity')
            if extra and len(locals_) == 2 and all(any(norm(extra) == norm(name) for name in o.get('findings', {}).get('extras', [])) for o in locals_):
                mapping[str(source['source_id'])] = {'kind': 'extra', 'title': extra, 'attribution': 'both locals + source metadata'}
    if assignments is None and not reasons:
        kind = identity['media_type']
        regular = [s for s in ordered if str(s['source_id']) not in mapping and not s.get('is_extra')]
        numbers = identity.get('episodes' if kind == 'tv' else 'tracks', [])
        if kind == 'tv':
            numbers = sorted(set(numbers))  # established range, never source-count-derived
        established = [s.get('identified_assignment') for s in regular]
        if any(established):
            for source, assignment in zip(regular, established):
                if assignment:
                    if (kind != 'movie' and assignment.get('number') not in numbers) or not assignment.get('evidence'):
                        source_reasons[str(source['source_id'])] = 'Source metadata contradicts recognized coverage or lacks association evidence'
                    else:
                        mapping[str(source['source_id'])] = dict(assignment, kind=kind,
                            attribution='explicit source metadata/reference association')
                        established_ids.add(str(source['source_id']))
            assigned_numbers = [a['number'] for a in established if a and 'number' in a] if kind != 'movie' else []
            # Duplicate episode numbers require explicit distinct version labels.
            if len(set(assigned_numbers)) != len(assigned_numbers):
                for source, assignment in zip(regular, established):
                    peers = [a for a in established if a and assignment and a.get('number') == assignment.get('number')]
                    if assignment and len(peers) > 1 and any(not a.get('version') for a in peers):
                        key = str(source['source_id'])
                        source_reasons[key] = 'Multiple outputs share an episode without explicit versions'
                        established_ids.discard(key)
            numbers = [n for n in numbers if n not in assigned_numbers]
            regular = [s for s in regular if str(s['source_id']) not in mapping and str(s['source_id']) not in source_reasons]
        if kind == 'movie':
            if not regular:
                pass  # every movie source already has an explicit association
            elif len(regular) == 1 and not any(a.get('kind') == 'movie' for a in mapping.values()):
                mapping[str(regular[0]['source_id'])] = {'kind': kind, 'attribution': 'single source'}
            else:
                coverage_reason = 'Movie source association is ambiguous'
        elif len(numbers) != len(regular):
            coverage_reason = f'Coverage/output mismatch: {len(numbers)} numbers for {len(regular)} sources'
        else:
            for source, number in zip(regular, numbers):
                mapping[str(source['source_id'])] = {'kind': kind, 'number': number,
                    'attribution': 'ascending backend source_id order default; no applicable explicit source association provided'}
    result = []
    for source in ordered:
        assignment = mapping.get(str(source['source_id']))
        key = str(source['source_id'])
        blocking = reasons + ([source_reasons[key]] if key in source_reasons else [])
        if coverage_reason and key not in established_ids:
            blocking = blocking + [coverage_reason]
        reason = '; '.join(blocking) if blocking else 'Source identity/association not established'
        proposed = None
        # A source-associated, independently recognized extra is not held hostage
        # to a mismatch affecting the regular episode outputs.
        extra_pass = assignment and assignment.get('kind') == 'extra' and assignments is None
        if assignment and (not blocking or extra_pass):
            try:
                proposed = str(filename(identity, assignment, Path(source.get('path', '.mkv')).suffix or '.mkv'))
                reason = None
            except (KeyError, ValueError, TypeError):
                reason = 'Assignment lacks required naming metadata'
        result.append(dict(source, assignment=assignment, proposed=proposed, reason=reason))
    names = {}
    for output in result:
        if output['proposed']:
            names.setdefault(norm(output['proposed']), []).append(output)
    for group in names.values():
        if len(group) > 1:
            for output in group:
                output.update(proposed=None, reason='Multiple sources propose the same final name; source/version association is unresolved')
    return result
