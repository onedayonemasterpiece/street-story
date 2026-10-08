"""Small model context for an accepted physical subject; never an auth proof."""
from __future__ import annotations

from .identity_proof import accepted_identity, physical_scope


def compact_physical_identity(identity, *, photo_sha256=None, generation=None, control_revision=None):
    if not isinstance(identity, dict):
        return {}
    # A prior compact DTO is presentation only. Actual authorization always
    # evaluates the full frozen identity before constructing this context.
    projected = 'physical_identity_accepted' in identity and 'geometry_proof' not in identity
    accepted = bool(identity.get('physical_identity_accepted')) if projected else accepted_identity(
        identity, photo_sha256=photo_sha256, generation=generation, control_revision=control_revision)
    result = {}
    for key in ('status', 'candidate_id', 'candidate_name', 'canonical_name', 'locality', 'country',
            'proof_kind', 'photo_sha256', 'wikipedia_url', 'wikidata', 'osm_id', 'candidate_url'):
        value = identity.get(key)
        limit = 2000 if key.endswith('_url') else 300
        if isinstance(value, str) and len(value) <= limit:
            result[key] = value
    for key in ('generation', 'control_revision', 'visual_reference_verified'):
        if key in identity:
            result[key] = identity[key]
    scope = identity.get('physical_scope') if projected else physical_scope(identity)
    if isinstance(scope, str) and scope:
        result['physical_scope'] = scope[:600]  # Preserve either proof contract's bounded literal physical scope.
    result['physical_identity_accepted'] = accepted
    result['observations'] = [str(value)[:300] for value in (identity.get('observations') or [])[:3]]
    if projected:
        aliases = identity.get('subject_alias_candidate_ids') or [identity.get('candidate_id')]
        omitted = int(identity.get('subject_aliases_omitted_count') or 0)
    else:
        from .identity_subject_binding import subject_aliases
        aliases = sorted(subject_aliases(identity.get('candidates') or []).get(
            identity.get('candidate_id'), {identity.get('candidate_id')}))
        omitted = 0
    valid = [value for value in aliases if isinstance(value, str) and value and len(value) <= 100]
    result['subject_alias_candidate_ids'] = valid[:32]
    result['subject_aliases_omitted_count'] = omitted + len(aliases) - len(result['subject_alias_candidate_ids'])
    return result
