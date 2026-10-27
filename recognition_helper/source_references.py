"""Optional disc-scoped source associations; identity snippets never need these."""
import copy


def signature(sources):
    return [{'source_id': int(s['source_id']),
             'duration_seconds': s.get('backend_metadata', {}).get('scan_duration')}
            for s in sorted(sources, key=lambda s: int(s['source_id']))]


def apply_reference(sources, reference, job, scan_signature=None):
    sources = copy.deepcopy(sources)
    observations = reference.get('source_observations', [])
    conventions = reference.get('collection_conventions', {})
    if not observations:
        if conventions.get('multiple_versions_per_episode'):
            return sources, ['Reference establishes episode versions, but actual source association is unresolved']
        return sources, []
    scope = reference.get('disc_observation', {})
    scopes = [signature(sources)]
    if scan_signature is not None:
        scopes.append(scan_signature)
    if not scope.get('label') or scope['label'] != job.get('label') or scope.get('source_signature') not in scopes:
        # Literal IDs from a descriptive/exported reference are not portable to a
        # different disc merely because series/season or title counts match.
        versions = any(o.get('assignment', {}).get('version') for o in observations if o.get('assignment'))
        return sources, ['Reference versions have no established association with these sources'] if versions else []
    by_id = {int(s['source_id']): s for s in sources}
    for observation in observations:
        number = int(observation['source_id'])
        assignment = observation.get('assignment')
        if number not in by_id or not assignment:
            continue
        source = by_id[number]
        if assignment.get('kind') == 'extra':
            # A reference can establish which source is an extra without naming it
            # from a single model. Automatic extra release still needs both locals.
            source['is_extra'] = True
            source['extra_identity'] = assignment.get('title')
        else:
            source['identified_assignment'] = dict(assignment, evidence='Exact disc label and source signature match: ' + reference['id'])
    return sources, []
