"""Owner-selected physical hypotheses, using the existing identity/visual jobs."""
from __future__ import annotations

import hashlib
import json
import logging

from .camera_hints import read_camera_hints
from .identity_proof import architectural_text_result_valid, geometry_result_valid
from .research_control import research_stopped
from .service import canonical

LOG = logging.getLogger(__name__)


def selected_group(research, *, photo_sha256=None, generation=None):
    group = research.get('place_selection') or {}
    if (group.get('mode') != 'rough' or not group.get('candidate_ids')
            or photo_sha256 is not None and group.get('photo_sha256') != photo_sha256
            or generation is not None and group.get('generation') != generation):
        return None
    return group


def physical_members(research, candidates):
    group = selected_group(research)
    if not group:
        return candidates
    members = {c['candidate_id']: c for c in group['candidates']}
    # Enrichment can add reference fields; observed physical identity, geometry,
    # coordinates and provenance remain the owner's frozen snapshot.
    protected = {'map_geometry', 'map_coordinates', 'map_object', 'map_address', 'search_anchor',
        'physical_component', 'physical_components', 'parent_relation_context', 'osm_id',
        'boundary_distance_m', 'representative_distance_m', 'distance_provenance',
        'footprint_bearing_interval', 'camera_inside_footprint'}
    return [{**{k: v for k, v in c.items() if k not in protected},
             **json.loads(canonical(members[c['candidate_id']]))}
            for c in candidates if c.get('candidate_id') in members]


def reference_allowed(research, candidate):
    group = selected_group(research)
    if not group:
        return True
    # Web pictures are evidence leads, never additional physical hypotheses.
    return (candidate.get('candidate_id') in group['candidate_ids']
        or str(candidate.get('candidate_id') or '').startswith('web:')
        or (candidate.get('reference_reuse') or {}).get('subject_candidate_id') in group['candidate_ids'])


def project_group_resolution(research, identity, now):
    group = selected_group(research)
    if not group:
        return
    selected = identity.get('candidate_id')
    matched = identity.get('status') == 'match' and selected in group['candidate_ids']
    identity.update(selection_basis='automatic_within_owner_group' if matched else 'owner_rough_group',
                    owner_group_id=group['group_id'])
    if matched:
        member = next(c for c in group['candidates'] if c['candidate_id'] == selected)
        identity['search_anchor'] = member['search_anchor']
        research['place_selection'] = {**group, 'status': 'resolved', 'candidate_id': selected,
            'narrowed_candidate_id': selected, 'resolved_at': now, 'resolution_basis': identity.get('proof_kind')}
    else:
        research['place_selection'] = {**group, 'status': 'needs_review', 'candidate_id': None,
                                      'narrowed_candidate_id': None}


async def resolve_group(service, story, research, *, job_id, job_attempt, transcript=''):
    """Acquire/compare only the selected physical group; never search a new POI."""
    from .identity_lifecycle import visual_match, confidence
    from .identity_discovery import prepare_search_plan, recover

    group = selected_group(research, photo_sha256=story['photo_sha256'],
                           generation=int(research.get('identity_generation') or 0))
    generation = group['generation']
    control = int(((research.get('research_controls') or {}).get('identity') or {}).get('revision') or 0)
    members = [c for c in json.loads(canonical(group['candidates']))
               if c['candidate_id'] not in research.get('identity_rejected_ids', [])]
    source = service._source_photo_for_job(story['id'])
    camera = (research.get('manual_camera_point') or {}) if story.get('latitude') is None else story
    from .photo_metadata import inspect_gps
    metadata = inspect_gps(source)
    binding = research.get('photo_camera_hints') or {}
    recovered_hints = (binding.get('photo_sha256') == story['photo_sha256']
        and binding.get('source') == 'selected_original_exif'
        and (research.get('location_provenance') or {}).get('kind') == 'selected_original_exif')
    position_verified = bool(camera.get('latitude') is not None and camera.get('longitude') is not None
        and (research.get('location_provenance') or {}).get('kind') != 'owner_approx_camera'
        and (recovered_hints or metadata['status'] == 'gps_present'
            and abs(float(camera['latitude']) - metadata['latitude']) <= .00001
            and abs(float(camera['longitude']) - metadata['longitude']) <= .00001))
    hints = binding['metadata'] if recovered_hints else read_camera_hints(source)
    story = {**story, '_identity_generation': generation, '_identity_research_control_revision': control,
        '_research_job_id': job_id, '_research_job_attempt': job_attempt,
        '_identity_original_source_sha256': hashlib.sha256(source).hexdigest(),
        '_camera_hints': hints, '_camera_position_verified': position_verified,
        '_identity_observed_candidates': members, '_identity_shortlist': members,
        '_identity_owner_hint': research.get('identity_owner_hint') or {},
        '_owner_selected_group': {'group_id': group['group_id'], 'candidate_ids': group['candidate_ids'],
                                  'owner_context': group.get('owner_context') or {}},
        'latitude': camera.get('latitude'), 'longitude': camera.get('longitude')}
    story['_identity_search_context'] = {'nearby': members, 'owner_selected_group': story['_owner_selected_group']}
    candidates = list(members)
    raw = None
    provider = getattr(service.providers, 'research', None)
    gemini = service.providers.gemini
    can_plan = bool(members) and (callable(getattr(provider, 'plan_identity_search', None)) or (
        callable(getattr(gemini, '_generate', None)) and callable(getattr(getattr(gemini, 'executor', None), 'execute', None))))
    if can_plan:
        await prepare_search_plan(service, story, transcript, candidates)
        candidates = physical_members(research, candidates)
        raw = story.get('_identity_accepted_result') or story.get('_identity_geometry_result')
    if raw is None:
        raw = await service._identify_photo(story, transcript, candidates) if candidates else {
            'status': 'uncertain', 'candidate_id': '', 'observations': ['Выбранные кандидаты были отклонены.']}
    def proved(value):
        return value.get('candidate_id') in group['candidate_ids'] and (
            visual_match(value, candidates) or geometry_result_valid(value, candidates, story)
            or architectural_text_result_valid(value, candidates, story))
    media = []
    if not proved(raw) and can_plan:
        recovery = await recover(service, story, transcript, candidates, set())
        if recovery:
            recovered, discovered = recovery
            media = [c for c in discovered if str(c.get('candidate_id') or '').startswith('web:')]
            candidates = physical_members(research, [*candidates, *discovered])
            candidates = list({c['candidate_id']: c for c in [*members, *candidates]}.values())
            if proved(recovered):
                raw = recovered
    matched = proved(raw)
    selected = next((c for c in candidates if c['candidate_id'] == raw.get('candidate_id')), None) if matched else None
    reference = matched and visual_match(raw, candidates)
    geometry = matched and geometry_result_valid(raw, candidates, story)
    text = matched and architectural_text_result_valid(raw, candidates, story)
    deferred = bool(not matched and (raw.get('_comparison_deferred') or media))
    identity = {'status': 'match' if matched else 'uncertain',
        'candidate_id': selected['candidate_id'] if selected else None,
        'candidate_name': selected['name'] if selected else None, 'candidate_url': selected.get('url') if selected else None,
        'candidates': [*candidates, *[{**c, 'identity_eligible': False, 'identity_role': 'reference_only_owner_group'} for c in media]],
        'observed_candidates': members, 'photo_sha256': story['photo_sha256'], 'generation': generation,
        'confidence': confidence(raw), 'observations': raw.get('observations') or [],
        'visual_reference_verified': reference, 'identity_verified': matched, 'control_revision': control,
        'camera_hints': story['_camera_hints'], 'camera_position_verified': position_verified,
        'proof_kind': 'combined' if sum([reference, geometry, text]) > 1 else 'geometry' if geometry else
                      'architectural_text' if text else 'visual_reference' if reference else None,
        'reference_evidence': raw.get('_reference_evidence') or [],
        **({'geometry_proof': raw['geometry_proof']} if geometry else {}),
        **({'architectural_text_proof': raw['architectural_text_proof']} if text else {})}
    with service.store.tx() as db:
        current = service._story_row(db, story['id'])
        latest = json.loads(current['research_json'] or '{}')
        current_group = selected_group(latest, photo_sha256=current['photo_sha256'], generation=generation)
        if (not current_group or current_group['group_id'] != group['group_id']
                or research_stopped(latest, 'identity', photo_sha256=current['photo_sha256'], identity_generation=generation)
                or int(((latest.get('research_controls') or {}).get('identity') or {}).get('revision') or 0) != control
                or (latest.get('visual_identity') or {}).get('status') in {'match', 'owner_confirmed'}
                or job_id and not db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND attempts=?",
                                             (job_id, job_attempt)).fetchone()):
            return service._story_repr(db, current)
        project_group_resolution(latest, identity, service.store.now())
        if deferred:
            latest['place_selection']['status'] = 'identifying'
        latest.update(visual_identity=identity, identity_attempted_generation=generation)
        if not matched and not deferred:
            latest['identity_clarification'] = {'question': 'Какие видимые детали или адрес помогут различить выбранные здания?',
                'reason': 'selected_group_ambiguous', 'photo_sha256': current['photo_sha256'],
                'generation': generation, 'control_revision': control}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(latest), current['id']))
        if matched:
            from .poi_memory import ensure_poi_identity, hydrate_story_facts
            coordinates = selected.get('map_coordinates') or {}
            latest['poi_id'] = ensure_poi_identity(db, identity, latitude=coordinates.get('latitude'),
                longitude=coordinates.get('longitude'), now=service.store.now())
            latest['poi_reused_fact_count'] = hydrate_story_facts(db, identity, current['id'])
        db.execute('UPDATE stories SET state=?,place_name=?,research_json=?,error_code=?,error_message=?,revision=revision+1,updated_at=? WHERE id=?',
            ('identity_ready' if matched else 'identifying' if deferred else 'needs_review', identity['candidate_name'],
             canonical(latest), None if matched or deferred else 'selected_group_ambiguous',
             None if matched or deferred else 'Пока недостаточно данных для различения выбранных зданий.',
             service.store.now(), current['id']))
        result = service._story_repr(db, service._story_row(db, current['id']))
    if deferred:
        service._schedule_identity_visual()
    LOG.info('street_story_identity stage=owner_group story_id=%s generation=%s group_id=%s members=%s matched=%s deferred=%s',
             story['id'], generation, group['group_id'], len(members), matched, deferred)
    return result
