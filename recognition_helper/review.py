"""Read-only review presentation and explicit, cycle-scoped reference proposals."""
import copy
import math
from pathlib import Path

from .decisions import compatible, norm, map_outputs
from .source_references import apply_reference, signature


def source_view(source, identity, minimum):
    b = source.get('backend_metadata', {})
    actual = b.get('duration_validation', {}).get('actual_seconds')
    if actual is None:
        actual = b.get('output_probe', {}).get('format', {}).get('duration')
    scanned = b.get('scan_duration')
    def duration(v):
        try:
            n = float(v)
            return n if math.isfinite(n) and n > 0 else None
        except (ValueError, TypeError):
            return None
    seconds = duration(actual)
    basis = 'actual extracted' if seconds is not None else 'scan estimate'
    if seconds is None:
        seconds = duration(scanned)
    if seconds is None:
        basis = 'unavailable'
    a = source.get('assignment') or {}
    tv = identity.get('media_type') == 'tv'
    role = ('Alternate version' if a.get('version') else 'Episode assignment (source-order assumption)' if a.get('kind') in ('tv','episode') and 'ascending' in a.get('attribution','') else 'Confirmed episode association' if a.get('kind') in ('tv','episode') else
            'Known extra' if a.get('kind') == 'extra' or source.get('is_extra') else
            'Episode candidate (duration only)' if tv and seconds is not None and seconds >= minimum else
            'Short clip' if tv and seconds is not None and seconds < minimum else 'Unassigned source')
    return {**copy.deepcopy(source), 'duration_seconds': seconds, 'duration_basis': basis,
            'display_role': role, 'short_clip': role == 'Short clip',
            'episode_candidate': role in ('Confirmed episode association','Episode assignment (source-order assumption)','Episode candidate (duration only)','Alternate version'),
            'basename': Path(source.get('path','')).name}


def suggestions(cycle, references):
    decision = cycle.get('decision') or {}
    identity = decision.get('identity', {})
    raw = [o.get('findings', {}) for o in cycle.get('locals', [])]
    observed_series = [o['series'] for o in raw if o.get('series')]
    if identity.get('series'):
        observed_series.append(identity['series'])
    visible = ' '.join(norm(o.get('visible_text','')) for o in raw)
    rejected = cycle.get('masterlist_review', {}).get('rejected', [])
    result = []
    for ref in references:
        facts = ref.get('facts', {})
        if facts.get('media_type') == 'tv':
            if not facts.get('series') or not observed_series or not any(compatible('series', s, facts.get('series')) for s in observed_series):
                continue
        elif not any(identity.get(k) and facts.get(k) and compatible(k,identity[k],facts[k]) for k in ('title','album','book')):
            continue
        evidence = []
        for field in ('series','season','disc','episodes','edition'):
            value = facts.get(field)
            if value is None:
                continue
            claims = [o[field] for o in raw if o.get(field) not in (None,'',[])]
            matches = [v for v in claims if compatible(field,v,value)]
            evidence.append({'field':field,'expected':value,'observed':claims,
                             'status':'exact' if matches and all(norm(v)==norm(value) for v in claims) else
                                      'compatible partial / disputed' if matches else 'contradiction' if claims else 'missing'})
        identifiers = ref.get('descriptive_disc', {}).get('printed_identifiers', [])
        exact_identifier = False
        for value in identifiers:
            matched = bool(norm(value) and norm(value) in visible)
            exact_identifier |= matched
            evidence.append({'field':'printed identifier','expected':value,'status':'exact' if matched else 'not observed'})
        result.append({**copy.deepcopy(ref),'evidence':evidence,'exact_identifier':exact_identifier,
                       'rejected':ref['id'] in rejected,
                       'confirmed':cycle.get('masterlist_review', {}).get('confirmed_id')==ref['id']})
    return sorted(result,key=lambda r:(not r['exact_identifier'],sum(e['status']=='contradiction' for e in r['evidence']),r['id']))


def confirmed_proposal(cycle, reference, minimum):
    """User confirms disc identity, not literal IDs or completion/release."""
    identity = copy.deepcopy(cycle.get('review_draft', {}).get('identity') or
                             (cycle.get('corrections') or [{}])[-1].get('identity') or
                             (cycle.get('decision') or {}).get('identity', {}))
    facts = reference.get('facts', {})
    allowed = ('media_type','series','season','disc','episodes','edition','title','album','book','artist','author','series_year')
    for field in allowed:
        if facts.get(field) not in (None,'',[]):
            identity[field] = copy.deepcopy(facts[field])
    if identity.get('media_type') == 'tv':
        identity.pop('title',None)  # title/range labels are not the established series
        for field in ('year','series_year','disc_total','disc_total_scope','part'):
            if field not in facts and not cycle.get('review_draft') and not cycle.get('corrections'):
                identity.pop(field,None)  # do not reuse disputed optional model guesses
        identity['episode_names'] = copy.deepcopy(reference.get('episode_names', {}))
        identity['edition_descriptors'] = copy.deepcopy(reference.get('edition_descriptors', []))
    sources = copy.deepcopy(cycle['sources'])
    for source in sources:
        if source.get('state') != 'released':
            for key in ('assignment','identified_assignment','is_extra','extra_identity'):
                source.pop(key,None)
    # Apply existing disc-scoped mapping validation, including full scan if retained.
    titles = next((s.get('backend_metadata',{}).get('video_selection',{}).get('titles') for s in sources
                   if s.get('backend_metadata',{}).get('video_selection',{}).get('titles')),None)
    full = [{'source_id':t['source_id'],'duration_seconds':t['duration_seconds']} for t in titles] if titles else None
    sources, reasons = apply_reference(sources,reference,cycle.get('backend_detail',{}).get('job',{}),full)
    eligible, excluded = [], []
    for source in sources:
        view = source_view(source,identity,minimum)
        if source.get('state') == 'released':
            continue
        if identity.get('media_type') != 'tv' or source.get('identified_assignment') or (view['duration_seconds'] is not None and view['duration_seconds'] >= minimum and not source.get('is_extra')):
            eligible.append(source)
        else:
            excluded.append(source)
    decision = {'identity':identity,'conflicts':{},'mapping_reasons':reasons}
    mapped = map_outputs(decision,eligible,cycle.get('locals',[]))
    assignments = {str(s['source_id']):s['assignment'] for s in mapped if s.get('proposed') and s.get('assignment')}
    warnings = list(dict.fromkeys(reasons+[s['reason'] for s in mapped if s.get('reason')]))
    for source in cycle['sources']:
        if source.get('state') != 'released' and source.get('manual_authorized') and source.get('assignment'):
            assignments[str(source['source_id'])] = copy.deepcopy(source['assignment'])
            warnings.append('Existing manual assignment retained for source ' + str(source['source_id']))
    return {'identity':identity,'assignments':assignments,'warnings':warnings,
            'decision':decision,
            'mapping_basis':'Verified applicable explicit source associations first; otherwise ascending eligible source IDs to ascending established episode numbers (assumption, not content verification)',
            'excluded_source_ids':[s['source_id'] for s in excluded]}
