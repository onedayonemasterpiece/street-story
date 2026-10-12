"""Bounded map snapshots and explicit owner selection using existing POI/job fences."""
from __future__ import annotations

import json
import logging
import math
import uuid

import httpx

from .identity_map_context import expand_disjoint_building_components, map_entry_context
from .service import ConflictError, canonical, digest
from .providers import RetryableProviderError

LOG = logging.getLogger(__name__)


def point(latitude, longitude):
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in (latitude, longitude)) or not -85 <= latitude <= 85 or not -180 <= longitude <= 180):
        raise ConflictError('place_coordinates_invalid', 'Укажите допустимую точку на карте.')
    return {'latitude': float(latitude), 'longitude': float(longitude)}


def camera_point(row, research):
    if row['latitude'] is not None and row['longitude'] is not None:
        provenance = research.get('location_provenance') or {}
        return {'latitude': row['latitude'], 'longitude': row['longitude'],
                'origin': 'manual' if provenance.get('kind') == 'owner_approx_camera' else 'exif'}
    return research.get('manual_camera_point')


def _geometry(candidate):
    rings = (candidate.get('map_geometry') or {}).get('rings') or []
    outer, inner = [], []
    for ring in rings:
        if not ring.get('closed') or len(ring.get('points') or []) < 4:
            continue
        pts = [[p['lon'], p['lat']] for p in ring['points']]
        if len(pts) > 1500:
            return None  # Never replace an observed contour with a simplified invented shape.
        (inner if ring['role'] == 'inner' else outer).append(pts)
    if len(outer) == 1:
        return {'type': 'Polygon', 'coordinates': [outer[0], *inner]}
    # Disjoint multipolygons are context; independently observed member ways
    # are selectable through expand_disjoint_building_components.
    return None


def _candidate(item):
    candidate = map_entry_context(item)
    tags = item.get('tags') or {}
    address = candidate.get('map_address') or {}
    name = str(tags.get('name') or ' '.join(str(address.get(k) or '') for k in ('street', 'house_number')).strip()
               or 'Здание ' + str(candidate.get('candidate_id') or ''))[:300]
    candidate.update(name=name, url=(candidate.get('map_object') or {}).get('source_url'),
                     type='building', osm_id=candidate.get('candidate_id'))
    candidate['search_anchor'] = {'city': str(tags.get('addr:city') or '')[:180],
        'street': address.get('street', ''), 'house_number': address.get('house_number', ''),
        'names': [str(tags[k])[:180] for k in ('name', 'old_name', 'alt_name') if tags.get(k)],
        'source': 'osm.observed_tags', 'provenance': 'osm.observed_tags', 'candidate_id': candidate.get('candidate_id'),
        'coordinates': candidate.get('map_coordinates') or {}}
    return candidate


async def _owner_map(osm, client, latitude, longitude, radius):
    """Reuse adapter routes, fetching complete geometry for map API omissions."""
    dlat = math.degrees(radius / 6_371_000) * 1.001
    dlon = dlat / max(.000001, abs(math.cos(math.radians(latitude))))
    bbox = ','.join(f'{v:.7f}' for v in (latitude-dlat, longitude-dlon, latitude+dlat, longitude+dlon))
    try:
        raw = await osm._map_nearby(client, latitude, longitude, radius)
    except RetryableProviderError:
        query = f'[out:json][timeout:20];(way["building"]({bbox});relation["building"]({bbox});way["highway"]({bbox}););out geom center;'
        raw, _ = await osm._overpass_query(client, query, 'owner_map', None, set())
        raw['map_source'] = 'overpass.bounded_geometry'
        return raw
    missing = raw.get('incomplete_building_ids') or []
    raw['map_source'] = 'osm.map_api'
    if missing and callable(getattr(osm, '_overpass_query', None)):
        parts = []
        for cid in missing[:16]:
            _, kind, identifier = cid.split(':')
            if kind in {'way', 'relation'} and identifier.isdigit():
                parts.append(f'{kind}({identifier});')
        if parts:
            try:
                extra, _ = await osm._overpass_query(client, '[out:json][timeout:20];(' + ''.join(parts) + ');out geom center;',
                                                    'owner_map_incomplete', None, set())
                original = {(x['type'], x['id']): x for x in raw['elements']}
                recovered = set()
                for item in extra['elements']:
                    candidate = _candidate(item)
                    if _geometry(candidate):
                        original[(item['type'], item['id'])] = item
                        recovered.add(candidate.get('candidate_id'))
                raw['elements'] = list(original.values())
                missing = [cid for cid in missing if cid not in recovered]
            except RetryableProviderError:
                pass  # The remaining complete ways remain available to select.
    raw['incomplete_building_ids'] = missing
    return raw


async def place_map(service, story_id, *, latitude=None, longitude=None, radius_m=350, zoom=None):
    if zoom is not None and (not isinstance(zoom, (int, float)) or not math.isfinite(zoom) or not 1 <= zoom <= 20):
        raise ConflictError('place_zoom_invalid', 'Недопустимый масштаб карты.')
    if not isinstance(radius_m, (int, float)) or not math.isfinite(radius_m) or not 50 <= radius_m <= 750:
        raise ConflictError('place_map_bounds_invalid', 'Область карты должна быть от 50 до 750 метров.')
    with service.store.connection() as db:
        row = service._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
        camera = camera_point(row, research)
        scope = (row['photo_sha256'], int(research.get('identity_generation') or 0))
        saved_map = research.get('place_map') or {}
        center = point(latitude, longitude) if latitude is not None or longitude is not None else (saved_map.get('center') or camera)
        if not center:
            raise ConflictError('place_map_point_required', 'В фото нет GPS. Укажите точку на карте.')
        effective_zoom = zoom or saved_map.get('zoom') or 17
        cache_key = digest([scope, center['latitude'], center['longitude'], radius_m, effective_zoom])
        cached = next((s for s in reversed(research.get('place_map_snapshots') or [])
                       if s.get('cache_key') == cache_key), None)
        if cached:
            return {**{k: v for k, v in cached.items() if k != 'candidates'},
                    'revision': row['revision'], 'selected': research.get('place_selection'), 'camera': camera}
    async with httpx.AsyncClient(timeout=25, headers={'User-Agent': service.settings.osm_user_agent}) as client:
        raw = await _owner_map(service.providers.osm, client, center['latitude'], center['longitude'], radius_m)
    candidates, buildings, streets = [], [], []
    building_count = 0
    for item in expand_disjoint_building_components(raw.get('elements') or []):
        tags = item.get('tags') or {}
        candidate = _candidate(item)
        cid = candidate.get('candidate_id')
        if not cid:
            continue
        if tags.get('building') not in {None, '', 'no'} and item.get('identity_eligible') is not False:
            geometry = _geometry(candidate)
            if geometry:
                building_count += 1
            if geometry and len(buildings) < 250:
                candidates.append(candidate)
                buildings.append({'type': 'Feature', 'id': cid, 'geometry': geometry,
                    'properties': {'candidate_id': cid, 'poi_id': cid, 'name': candidate['name'],
                                   'label': candidate['name'], 'address': candidate.get('map_address') or {}}})
        elif tags.get('highway') and len(streets) < 150:
            lines = (candidate.get('map_geometry') or {}).get('lines') or []
            if lines and all(len(line) <= 1500 for line in lines):
                streets.append({'type': 'Feature', 'id': cid, 'geometry': {'type': 'MultiLineString',
                    'coordinates': [[[p['lon'], p['lat']] for p in line] for line in lines]},
                    'properties': {'name': str(tags.get('name') or '')[:180]}})
    snapshot = {'snapshot_id': 'map_' + uuid.uuid4().hex, 'photo_sha256': scope[0],
        'identity_generation': scope[1], 'center': {k: center[k] for k in ('latitude', 'longitude')},
        'camera': camera, 'radius_m': radius_m, 'zoom': effective_zoom, 'cache_key': cache_key,
        'partial': building_count > len(buildings) or bool(raw.get('incomplete_building_ids')),
        'incomplete_building_count': len(raw.get('incomplete_building_ids') or []),
        'map_source': raw.get('map_source'), 'available_building_count': building_count, 'buildings': {'type': 'FeatureCollection', 'features': buildings},
        'streets': {'type': 'FeatureCollection', 'features': streets}, 'attribution': '© OpenStreetMap contributors'}
    with service.store.tx() as db:
        current = service._story_row(db, story_id)
        research = json.loads(current['research_json'] or '{}')
        if (current['photo_sha256'], int(research.get('identity_generation') or 0)) != scope:
            raise ConflictError('place_map_stale', 'Объект изменился. Обновите карту.')
        snapshots = research.get('place_map_snapshots') or []
        research['place_map_snapshots'] = [*snapshots[-2:], {**snapshot, 'candidates': candidates}]
        research['place_map'] = snapshot
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story_id))
        snapshot = {**snapshot, 'revision': current['revision'], 'selected': research.get('place_selection')}
    LOG.info('street_story_map stage=snapshot story_id=%s generation=%s buildings=%s', story_id, scope[1], len(buildings))
    return snapshot


def confirm_place(service, story_id, key, body):
    if body.get('mode') != 'exact':
        raise ConflictError('place_mode_unsupported', 'Для этого пути выберите один конкретный дом.')
    command = body.get('command_id') or key
    if command != key:
        raise ConflictError('place_command_conflict', 'Идентификатор команды не совпадает.')
    with service.store.tx() as db:
        row = service._story_row(db, story_id)
        if service._idem(db, key, 'place_selection', digest({'story_id': story_id, **body}), 'story', story_id):
            return service._story_repr(db, row)  # Return current state: replay A never rolls B back.
        if row['state'] in {'scheduling', 'scheduled', 'published'}:
            raise ConflictError('place_publication_active', 'Создайте новую историю для другого объекта после публикации.')
        research = json.loads(row['research_json'] or '{}')
        generation = int(research.get('identity_generation') or 0)
        active = (research.get('visual_identity') or {}).get('candidate_id')
        expected_generation = body.get('expected_identity_generation', body.get('expected_generation'))
        if (body.get('expected_photo_sha256') != row['photo_sha256'] or expected_generation != generation
                or 'expected_candidate_id' not in body or body['expected_candidate_id'] != active):
            raise ConflictError('place_selection_stale', 'Фото или объект изменился. Обновите историю.')
        fallback = body.get('fallback')
        if fallback is not None:
            if not isinstance(fallback, dict) or body.get('candidate_id') or body.get('poi_id'):
                raise ConflictError('place_fallback_invalid', 'Подтвердите точку и адрес явно.')
            coordinates = point(fallback.get('latitude'), fallback.get('longitude'))
            address, name, city = (str(fallback.get(k) or '').strip()[:300] for k in ('address', 'name', 'city'))
            if not address and not name:
                raise ConflictError('place_fallback_address_required', 'Для точки без контура нужно название объекта или адрес дома.')
            cid = 'owner:place:' + digest([coordinates, (address or name).casefold(), city.casefold()])[:24]
            chosen = {'candidate_id': cid, 'name': name or address, 'type': 'owner_point_address',
                'map_coordinates': {**coordinates, 'provenance': 'owner.explicit_point'},
                'map_address': {'address': address, 'city': city, 'provenance': 'owner.explicit_address'},
                'search_anchor': {'city': city, 'address': address, 'names': [name] if name else [],
                    'coordinates': coordinates, 'source': 'owner.explicit_point_address', 'provenance': 'owner.explicit_point_address', 'candidate_id': cid}}
        else:
            cid = body.get('candidate_id') or body.get('poi_id')
            snapshot = next((s for s in research.get('place_map_snapshots') or []
                             if s['snapshot_id'] == body.get('snapshot_id')), None)
            if not snapshot or snapshot['photo_sha256'] != row['photo_sha256'] or snapshot['identity_generation'] != generation:
                raise ConflictError('place_snapshot_stale', 'Обновите карту перед подтверждением.')
            chosen = next((c for c in snapshot['candidates'] if c['candidate_id'] == cid), None)
            if not chosen:
                raise ConflictError('place_candidate_unknown', 'Выберите один показанный контур здания.')
        owner_context = body.get('owner_context')
        if owner_context is not None:
            if not isinstance(owner_context, dict) or any(k not in {'city', 'address', 'name'}
                    or not isinstance(v, str) or len(v) > 300 for k, v in owner_context.items()):
                raise ConflictError('place_owner_context_invalid', 'Нужны короткие сведения автора о выбранном доме.')
            chosen = {**chosen, 'search_anchor': dict(chosen['search_anchor'])}
            anchor = chosen['search_anchor']
            owner_context = {k: v.strip() for k, v in owner_context.items() if v.strip()}
            for field in ('city', 'address'):
                if not anchor.get(field) and owner_context.get(field):
                    anchor[field] = owner_context[field]
            if owner_context.get('name'):
                anchor['names'] = list(dict.fromkeys([*anchor.get('names', []), owner_context['name']]))
            anchor['owner_context'] = owner_context
            anchor['owner_context_provenance'] = 'owner.explicit_selection_hint'
        return transition_owner_selection(service, db, row, research, chosen, command,
                                          body=body, snapshot=None if fallback else snapshot, fallback=bool(fallback))


def transition_owner_selection(service, db, row, research, chosen, command, *, body=None,
                               snapshot=None, fallback=False, selection_basis='owner_exact', approval=None):
    """Shared atomic domain transition after UI or named Live consent checks."""
    body = body or {}
    story_id = row['id']
    generation = int(research.get('identity_generation') or 0)
    cid = chosen['candidate_id']
    if not chosen.get('search_anchor'):
        address = chosen.get('map_address') or {}
        chosen = {**chosen, 'search_anchor': {'city': address.get('city', ''),
            'street': address.get('street', ''), 'house_number': address.get('house_number', ''),
            'names': [chosen['name']], 'candidate_id': cid, 'coordinates': chosen.get('map_coordinates') or {},
            'provenance': 'owner.named_confirmation', 'source': 'owner.named_confirmation'}}
    from .poi_memory import ensure_poi_identity, hydrate_story_facts, sync_poi_review_from_story
    sync_poi_review_from_story(db, story_id, service.store.now())
    now = service.store.now()
    old_visual = json.loads(row['visual_context_json'] or '{}')
    if old_visual.get('operation_id'):
        visual_job = db.execute("SELECT id FROM jobs WHERE story_id=? AND kind='visual' ORDER BY created_at DESC LIMIT 1",
                                (story_id,)).fetchone()
        if visual_job:
            db.execute('INSERT OR IGNORE INTO research_checkpoints(job_id,stage,value_json,created_at) VALUES(?,?,?,?)',
                       (visual_job['id'], 'detached_target_visual', canonical(old_visual), now))
        else:
            research['detached_visual_receipt'] = {'identity_generation': generation, 'context': old_visual}
    # Existing running job ownership and run/chunk lease fences invalidate late A writes.
    db.execute("UPDATE jobs SET state='cancelled',attempts=attempts+CASE WHEN state='running' THEN 1 ELSE 0 END,"
               "lease_until=0,last_error='owner_place_changed',updated_at=? WHERE story_id=? "
               "AND kind IN ('identity','identity_visual','research','refinement','visual') AND state IN ('ready','retry','running')",
               (now, story_id))
    db.execute("UPDATE research_runs SET state='cancelled',status_detail='owner_place_changed',updated_at=? "
               "WHERE story_id=? AND state NOT IN ('completed','failed','cancelled')", (now, story_id))
    db.execute('UPDATE research_chunk_runs SET lease_owner=NULL,lease_until=0,lease_fence=lease_fence+1 '
               'WHERE run_id IN (SELECT run_id FROM research_runs WHERE story_id=? AND identity_generation=?)',
               (story_id, generation))
    # Observations, evidence and POI memory remain durable; only active projections/owner choices detach.
    db.execute('DELETE FROM facts WHERE story_id=?', (story_id,))
    db.execute('DELETE FROM fact_assertions WHERE story_id=?', (story_id,))
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_editor_state'").fetchone():
        db.execute('DELETE FROM live_editor_state WHERE story_id=?', (story_id,))
    keep = {'identity_mode', 'location_provenance', 'manual_camera_point', 'place_map_snapshots', 'place_map',
            'research_controls', 'research_control_revision', 'research_budget'}
    # External operation receipts remain addressable by their original generation.
    keep.update(k for k in research if 'receipt' in k or 'operation' in k)
    research = {k: v for k, v in research.items() if k in keep}
    generation += 1
    research['identity_generation'] = generation
    research['fact_research_cancelled'] = False
    research['identity_research_cancelled'] = False
    confirmed = {'status': 'owner_confirmed', 'selection_basis': selection_basis, 'owner_selected': True,
        'candidate_id': cid, 'candidate_name': chosen['name'], 'candidate_url': chosen.get('url'),
        'photo_sha256': row['photo_sha256'], 'generation': generation, 'confidence': None,
        'search_anchor': chosen['search_anchor'], 'candidates': [chosen],
        'approval_evidence': approval or {'kind': 'owner_map_ui', 'command_id': command, 'confirmed_at': now},
        'source_links': [chosen['url']] if chosen.get('url') else []}
    research['visual_identity'] = confirmed
    if body.get('owner_comment'):
        research['transcript'] = str(body['owner_comment'])[:5000]
    research['place_selection'] = {'mode': 'exact', 'candidate_id': cid, 'poi_id': cid,
        'candidate': chosen, 'snapshot_id': body.get('snapshot_id'), 'generation': generation,
        'photo_sha256': row['photo_sha256'], 'basis': 'owner_named_voice' if selection_basis == 'owner_live' else ('owner_explicit_point_address' if fallback else 'owner_exact_osm_contour'),
        'command_id': command, 'confirmed_at': now}
    if row['latitude'] is None:
        camera = body.get('camera') or (snapshot or {}).get('center') or chosen.get('map_coordinates')
        if camera:
            research['manual_camera_point'] = {**point(camera.get('latitude'), camera.get('longitude')), 'origin': 'manual'}
    db.execute("UPDATE stories SET place_name=?,state='researching',summary=NULL,draft_text=NULL,processed_image_path=NULL,"
               "processed_image_url=NULL,vibepublish_asset_ref=NULL,visual_context_json='{}',research_json=?,"
               "error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
               (chosen['name'], canonical(research), now, story_id))
    target = chosen.get('map_coordinates') or {}
    research['poi_id'] = ensure_poi_identity(db, confirmed, latitude=target.get('latitude'), longitude=target.get('longitude'), now=now)
    research['poi_reused_fact_count'] = hydrate_story_facts(db, confirmed, story_id)
    revision = 'owner-map-facts:' + digest([story_id, command, generation, cid])
    research['automatic_fact_request'] = {'input_revision': revision, 'photo_sha256': row['photo_sha256'], 'identity_generation': generation}
    db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story_id))
    from .research_budget import ensure_budget
    ensure_budget(service, story_id, db=db, explicit=True)
    job = service._enqueue_job(db, story_id, 'research', revision, {'input_revision': revision,
        'photo_sha256': row['photo_sha256'], 'identity_generation': generation, 'voice_session_ids': [],
        'mode': 'initial', 'confirmed_candidate_id': cid, 'owner_research_wave': True,
        'queue_priority': 'interactive', 'minimum_useful_facts': 3,
        'coverage_goal': 'Найди проверенные факты о доме, явно выбранном автором. Сохраняй ссылки и полезные результаты постепенно.',
        'extraction_scope': 'initial-confirmed-poi-v1'})
    result = service._story_repr(db, service._story_row(db, story_id))
    LOG.info('street_story_map stage=confirmed story_id=%s generation=%s candidate_id=%s job_id=%s', story_id, generation, cid, job)
    return result


def save_draft(service, story_id, key, body):
    """Persist the author's editor text with the same evidence revisions as Live."""
    from .fact_ledger import eligible_selected_fact_ids, fact_revision_bundle, selected_eligibility_issues
    text = body.get('text')
    if not isinstance(text, str) or not text.strip() or len(text) > 5000:
        raise ConflictError('draft_text_invalid', 'Нужен текст до 5000 символов.')
    with service.store.tx() as db:
        story = service._story_row(db, story_id)
        if service._idem(db, key, 'owner_draft', digest({'story_id': story_id, **body}), 'story', story_id):
            return service._story_repr(db, story)
        research = json.loads(story['research_json'] or '{}')
        if (body.get('expected_identity_generation') != int(research.get('identity_generation') or 0)
                or 'expected_revision' in body and body['expected_revision'] != story['revision']):
            raise ConflictError('draft_stale', 'Материал изменился. Обновите историю перед сохранением.')
        if story['state'] in {'scheduling', 'scheduled', 'published'}:
            raise ConflictError('draft_publication_active', 'Публикация уже подтверждена.')
        ids = eligible_selected_fact_ids(db, story_id)
        if not ids or selected_eligibility_issues(db, story_id):
            raise ConflictError('fact_review_required', 'Выберите проверенные факты перед сохранением черновика.')
        if sorted(body.get('selected_fact_ids') or []) != sorted(ids):
            raise ConflictError('draft_fact_selection_changed', 'Выбор фактов изменился.')
        editor_exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='live_editor_state'").fetchone()
        editor = db.execute('SELECT * FROM live_editor_state WHERE story_id=?', (story_id,)).fetchone() if editor_exists else None
        if editor:
            literals = json.loads(editor['literal_json'] or '[]')
            if any(item.get('text') and item['text'] not in text for item in literals):
                raise ConflictError('literal_span_protected', 'Сохраните дословные фрагменты из голосового редактора.')
            history = json.loads(editor['history_json'] or '[]')
            history.append({'text': story['draft_text'] or '', 'literal_spans': literals,
                            'from_text_revision': editor['text_revision'], 'summary': editor['last_change']})
            db.execute('UPDATE live_editor_state SET text_revision=text_revision+1,history_json=?,last_change=?,updated_at=? WHERE story_id=?',
                       (canonical(history[-20:]), 'Сохранён текст автора', service.store.now(), story_id))
        research.update(draft_needs_refresh=False, draft_stale_reason=None, draft_composed_by='owner_editor',
                        content_identity_changed=False, draft_fact_revisions=fact_revision_bundle(db, story_id, ids))
        db.execute('UPDATE stories SET draft_text=?,research_json=?,revision=revision+1,updated_at=? WHERE id=?',
                   (text.strip(), canonical(research), service.store.now(), story_id))
        return service._story_repr(db, service._story_row(db, story_id))
