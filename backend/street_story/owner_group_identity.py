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


def group_input_context(story, research):
    """Observed address/locality inputs beside a frozen owner group; never identity."""
    from .place_selection import snapshot_member_address, snapshot_city, object_center
    from .identity_source_selection import observed_address_context

    group = selected_group(research, photo_sha256=story['photo_sha256'],
                           generation=int(research.get('identity_generation') or 0))
    if not group:
        return story
    snapshot = next((s for s in research.get('place_map_snapshots') or []
        if s.get('snapshot_id') == group.get('snapshot_id')
        and s.get('photo_sha256') == story['photo_sha256']), {})
    members = json.loads(canonical(group['candidates']))
    original = {c['candidate_id']: c for c in [*snapshot.get('address_objects', []),
        *snapshot.get('city_objects', []), *snapshot.get('candidates', [])]}
    nodes, cities = {}, []

    def literal_city(entry):
        anchor = entry.get('search_anchor') or {}
        # This DTO retains addr:city separately from map_object's type tags.
        return (anchor.get('city') if anchor.get('source') == 'osm.observed_tags'
            and anchor.get('city_provenance') in {None, 'osm.selected.addr_city'} else None)

    for member in members:
        cid = member['candidate_id']
        captured = original.get(cid) or {}
        own_city = literal_city(captured)
        if own_city and member.get('map_address'):
            member['map_address'] = {**member['map_address'], 'city': own_city}
        # Revalidate linkage against original nodes and the frozen group contour.
        checked = {**member, 'search_anchor': {'city': own_city} if own_city else {}}
        snapshot_member_address(snapshot, checked)
        for address in checked['search_anchor'].get('address_entries') or []:
            node = json.loads(canonical(original[address['candidate_id']]))
            node_city = literal_city(node)
            if node_city:
                node['map_address'] = {**node['map_address'], 'city': node_city}
            node.update(identity_eligible=False, identity_role='owner_group_address_context')
            nodes[node['candidate_id']] = node
        snapshot_city(snapshot, checked)
        anchor = checked['search_anchor']
        if not anchor.get('city'):
            receipt = (snapshot.get('city_reverse') or {}).get(cid) or {}
            center = object_center(member)
            if (receipt.get('status') == 'completed' and receipt.get('center') == center
                    and receipt.get('provenance') == 'osm.selected_center_reverse'):
                anchor.update(city=receipt.get('city'), city_provenance=receipt['provenance'])
        cities.append({'candidate_id': cid, 'city': anchor.get('city') or '',
            'provenance': anchor.get('city_provenance'),
            'source_candidate_id': anchor.get('city_source_candidate_id')})
    shared = {c['city'] for c in cities if c['city']}
    city = next(iter(shared)) if len(shared) == 1 and all(c['city'] for c in cities) else ''
    observed = [*members, *nodes.values()]
    # The existing address builder restores city from same-ID raw OSM input.
    # Keep only the literal captured city tag; no neighbor/derived city becomes
    # a physical object's own attribute.
    city_tags = []
    for entry in observed:
        captured = original.get(entry['candidate_id']) or {}
        own_city = literal_city(captured)
        if own_city:
            _, kind, identifier = entry['candidate_id'].split(':')
            city_tags.append({'type': kind, 'id': int(identifier), 'tags': {'addr:city': own_city}})
    group_context = {'group_id': group['group_id'], 'candidate_ids': group['candidate_ids'],
                     'owner_context': group.get('owner_context') or {}, 'locality_context': cities}
    # A prior camera reverse/OSM projection cannot widen or locate this group.
    scoped_research = {k: v for k, v in research.items() if k != 'osm'}
    result = {**story, 'research_json': canonical(scoped_research),
        '_identity_observed_candidates': observed,
        '_identity_map_snapshot': {'owner_group_snapshot_id': group.get('snapshot_id'),
            'observed_pool': city_tags},
        '_owner_selected_group': group_context,
        '_identity_search_context': {'nearby': observed, 'reverse_address': {'city': city} if city else {},
            'reverse_address_provenance': 'owner_selected_snapshot_locality',
            'owner_selected_group': group_context}}
    if group.get('location_basis') == 'owner_relocated':
        result.update(latitude=None, longitude=None, _camera_hints={}, _camera_position_verified=False)
    result['_owner_group_address_context'] = observed_address_context(result, members)
    LOG.info('street_story_identity stage=owner_group_context story_id=%s generation=%s '
        'group_id=%s members=%s address_nodes=%s known_localities=%s shared_locality=%s',
        story['id'], group['generation'], group['group_id'], len(members), len(nodes),
        sum(bool(c['city']) for c in cities), bool(city))
    return result


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
    relocated = group.get('location_basis') == 'owner_relocated'
    camera = {} if relocated else story
    from .photo_metadata import inspect_gps
    metadata = inspect_gps(source)
    binding = research.get('photo_camera_hints') or {}
    recovered_hints = (binding.get('photo_sha256') == story['photo_sha256']
        and binding.get('source') == 'selected_original_exif'
        and (research.get('location_provenance') or {}).get('kind') == 'selected_original_exif')
    position_verified = bool(not relocated and camera.get('latitude') is not None and camera.get('longitude') is not None
        and (research.get('location_provenance') or {}).get('kind') != 'owner_approx_camera'
        and (recovered_hints or metadata['status'] == 'gps_present'
            and abs(float(camera['latitude']) - metadata['latitude']) <= .00001
            and abs(float(camera['longitude']) - metadata['longitude']) <= .00001))
    hints = {} if relocated else binding['metadata'] if recovered_hints else read_camera_hints(source)
    story = {**story, '_identity_generation': generation, '_identity_research_control_revision': control,
        '_research_job_id': job_id, '_research_job_attempt': job_attempt,
        '_identity_original_source_sha256': hashlib.sha256(source).hexdigest(),
        '_camera_hints': hints, '_camera_position_verified': position_verified,
        '_identity_observed_candidates': members, '_identity_shortlist': members,
        '_identity_owner_hint': research.get('identity_owner_hint') or {},
        '_owner_selected_group': {'group_id': group['group_id'], 'candidate_ids': group['candidate_ids'],
                                  'owner_context': group.get('owner_context') or {}},
        'latitude': camera.get('latitude'), 'longitude': camera.get('longitude')}
    story = group_input_context(story, research)
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
