"""TV semantic reconciliation; raw observations never become mutable decisions."""
import copy


def reconcile_tv_metadata(decision, observations):
    from .decisions import compatible, norm
    identity, conflicts = decision['identity'], decision['conflicts']
    if identity.get('media_type') != 'tv' or 'media_type' in conflicts:
        return
    report = decision['tv_metadata'] = {'original_conflicts': copy.deepcopy(conflicts),
                                      'resolved_conflicts': {}, 'uncertainties': {}}
    reference = decision.get('masterlist', {})
    facts = reference.get('facts', {})
    applicable = bool(reference.get('match_basis') and identity.get('series') and
        not any(k in conflicts for k in ('series', 'season', 'disc', 'episodes', 'edition')) and all(compatible(k, identity[k], v)
        for k, v in facts.items() if k in identity))
    descriptors = list(reference.get('edition_descriptors', [])) if applicable else []
    edition_name = reference.get('edition_name', '') if applicable else ''

    def edition_descriptor(value):
        tokens = norm(value).split()
        explicit = any(norm(value) == norm(d) for d in descriptors)
        printed = norm(edition_name).split()
        return explicit or (len(tokens) >= 2 and any(
            printed[i:i + len(tokens)] == tokens for i in range(len(printed) - len(tokens) + 1)))

    title_claims = conflicts.get('title', [])
    if not title_claims and identity.get('title'):
        title_claims = [('observation', identity['title'])]
    if title_claims and identity.get('series') and 'series' not in conflicts:
        labels = [v for _, v in title_claims if edition_descriptor(v)]
        if all(compatible('series', identity['series'], v) or edition_descriptor(v) for _, v in title_claims):
            if 'title' in conflicts:
                report['resolved_conflicts']['title'] = {
                    'original': conflicts.pop('title'),
                    'basis': 'Established series identity; matched reference identifies edition labels separately'}
            identity.pop('title', None)
            decision['attribution'].pop('title', None)
            if labels:
                identity['edition_descriptors'] = list(dict.fromkeys(identity.get('edition_descriptors', []) + labels))
                decision['attribution']['edition_descriptors'] = ['compatible masterlist:' + reference['id']]
    # Disc totals are claims with scopes, not a count of partial reference rows.
    claims = [{'model': o.get('model'), 'value': o.get('findings', {}).get('disc_total'),
               'scope': o.get('findings', {}).get('disc_total_scope') or 'unknown'}
              for o in observations if o.get('findings', {}).get('disc_total') is not None]
    if applicable:
        for scope, value in reference.get('disc_totals', {}).items():
            claims.append({'model': 'masterlist:' + reference['id'], 'scope': scope, 'value': value})
    if claims or 'disc_total' in conflicts or 'disc_total_scope' in conflicts:
        report['disc_total_claims'] = copy.deepcopy(claims)
        totals = {}
        for scope in ('season', 'release'):
            values = [c['value'] for c in claims if c['scope'] == scope]
            if values and len(set(values)) == 1 and isinstance(values[0], int) and values[0] > 0:
                totals[scope] = values[0]
        identity.pop('disc_total', None)
        identity.pop('disc_total_scope', None)
        decision['attribution'].pop('disc_total', None)
        decision['attribution'].pop('disc_total_scope', None)
        if totals:
            identity['disc_totals'] = totals
        for field in ('disc_total', 'disc_total_scope'):
            if field in conflicts:
                report['resolved_conflicts'][field] = {
                    'original': conflicts.pop(field), 'basis': 'Optional scoped count uncertainty; not current disc or episode mapping'}
        if any(c['scope'] not in totals or c['value'] != totals.get(c['scope']) for c in claims):
            report['uncertainties']['disc_totals'] = copy.deepcopy(claims)
    if applicable and 'episodes' not in conflicts:
        names = reference.get('episode_names', {})
        episode_names = {str(n): names[str(n)] for n in identity.get('episodes', [])
                         if str(n) in names and isinstance(names[str(n)], str)}
        if episode_names:
            identity['episode_names'] = episode_names
            decision['attribution']['episode_names'] = ['masterlist:' + reference['id']]
