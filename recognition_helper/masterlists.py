"""Partial references; legacy approval flags are provenance, never execution gates."""
import json
from pathlib import Path
import yaml
import copy


def load_references(root):
    references = []
    for path in sorted(Path(root).rglob('*')):
        if path.suffix not in ('.json', '.yaml', '.yml'):
            continue
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            continue
        if 'facts' in data:
            references.append(data)
            continue
        # Compatibility with historical schema v1/v2 descriptive snippets.
        for disc in data.get('discs', []):
            facts = {k: data[k] for k in ('series', 'season', 'edition', 'series_year') if data.get(k) is not None}
            if facts.get('series'):
                facts['media_type'] = 'tv'
            if disc.get('number') is not None:
                facts['disc'] = disc['number']
            episodes = disc.get('episodes', [])
            numbers = [ep['number'] for ep in episodes if ep.get('number') is not None]
            if numbers:
                facts['episodes'] = numbers
            conventions = copy.deepcopy(data.get('collection_conventions', {}))
            if isinstance(conventions, list):
                conventions = {'historical_conventions': conventions,
                               'multiple_versions_per_episode': any(c.get('kind') == 'paired_perspectives' for c in conventions)}
            pairs = [p for p in data.get('historical_source_pairs', []) if str(p.get('disc')) == str(disc.get('number'))]
            if len({p.get('version') for p in pairs if p.get('version')}) > 1:
                conventions['multiple_versions_per_episode'] = True
            references.append({'id': str(data.get('id', path.stem)) + '/' + str(disc.get('id', disc.get('number', 'partial'))),
                               'facts': facts, 'episode_titles': [e['title'] for e in episodes if e.get('title')],
                               'episode_names': {str(e['number']): e['title'] for e in episodes
                                                 if e.get('number') is not None and e.get('title')},
                               'aliases': data.get('title_aliases', []),
                               'provenance': data.get('provenance', []),
                               'legacy_approved': data.get('approved'),
                                'legacy_unresolved': data.get('unresolved', []) + disc.get('unresolved', []),
                                'collection_conventions': conventions,
                                'historical_source_pairs': pairs,
                                'descriptive_disc': {k: copy.deepcopy(disc[k]) for k in
                                                    ('printed_identifiers', 'labels', 'mapping_rules', 'inventory') if k in disc},
                                 'edition_name': data.get('edition_name'), 'edition_tokens': data.get('edition_tokens', []),
                                 'edition_descriptors': copy.deepcopy(data.get('edition_descriptors', [])),
                                 'disc_totals': copy.deepcopy(data.get('disc_totals', {})),
                                'reference_origin': data.get('reference_origin'),
                                'source': path.name})
    # Re-imported identical historical facts do not become competing candidates.
    # Distinct edition/fact/version information is deliberately not collapsed.
    unique = {}
    for ref in references:
        key = json.dumps({k: ref.get(k) for k in ('id','facts','aliases','episode_titles',
                         'collection_conventions','source_observations','episode_names',
                         'edition_name','edition_descriptors','disc_totals')}, sort_keys=True, ensure_ascii=False)
        if key not in unique:
            unique[key] = ref
        else:
            existing = unique[key]
            existing.setdefault('additional_sources', []).append(ref.get('source', ref.get('reference_origin')))
            existing['provenance'] = list(dict.fromkeys(existing.get('provenance', []) + ref.get('provenance', [])))
            if ref.get('reference_origin'):
                existing.setdefault('reference_origins', []).append(ref['reference_origin'])
    return list(unique.values())
