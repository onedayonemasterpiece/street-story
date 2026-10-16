"""Bounded map snapshots and explicit owner selection using existing POI/job fences."""
from __future__ import annotations

import json
import logging
import math
import asyncio
import re
import time
import uuid

import httpx

from .identity_map_context import expand_disjoint_building_components, geometry_camera_context, has_organization_tags, map_entry_context
from .service import ConflictError, canonical, digest
from .providers import RetryableProviderError

LOG = logging.getLogger(__name__)
MAP_LOG = logging.getLogger('uvicorn.error')
# These bound this read-only HTTP response, not the durable research lifecycle.
OWNER_MAP_REQUEST_SECONDS = 28.0
OWNER_MAP_ENRICHMENT_SECONDS = 8.0


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
    return None


def object_center(candidate):
    center = candidate.get('map_coordinates') or {}
    if center.get('latitude') is not None and center.get('longitude') is not None:
        return point(center['latitude'], center['longitude'])
    points = [p for r in (candidate.get('map_geometry') or {}).get('rings') or []
              if r.get('role') == 'outer' for p in r.get('points') or []]
    if points:
        return point(sum(p['lat'] for p in points)/len(points), sum(p['lon'] for p in points)/len(points))
    return None


def offset_m(a, b):
    lat1, lat2 = math.radians(a['latitude']), math.radians(b['latitude'])
    dlat, dlon = lat2-lat1, math.radians(b['longitude']-a['longitude'])
    value = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6_371_000 * 2 * math.asin(min(1, math.sqrt(max(0, value))))


def selection_location(row, members):
    camera = camera_point(row, {})
    centers = [center for c in members if (center := object_center(c))]
    if not camera or not centers:
        return {'gps_offset_m': None, 'location_basis': 'near_gps'}
    distance = min(offset_m(camera, center) for center in centers)
    return {'gps_offset_m': round(distance, 1),
            'location_basis': 'owner_relocated' if distance > 350 else 'near_gps'}


def snapshot_member_address(snapshot, chosen):
    """Only observed entrance nodes proved members of this selected closed way."""
    mapped = chosen.get('map_object') or {}
    membership = mapped.get('building_entrances') or {}
    cid = chosen.get('candidate_id') or ''
    url = chosen.get('url')
    rings = [r for r in (chosen.get('map_geometry') or {}).get('rings') or []
             if r.get('closed') and r.get('role') == 'outer']
    if (not cid.startswith('osm:way:') or not rings or mapped.get('provenance') != 'osm.tags'
            or (mapped.get('tags') or {}).get('building') in {None, '', 'no'}
            or membership.get('proof') != 'osm_closed_way_node_membership'
            or not url or membership.get('source_url') != url or mapped.get('source_url') != url):
        return
    entries = {c.get('candidate_id'): c for c in [*snapshot.get('address_objects', []),
        *snapshot.get('city_objects', []), *snapshot.get('candidates', [])]}
    linked = []
    for node_id in membership.get('candidate_ids') or []:
        node = entries.get(node_id) or {}
        node_map = node.get('map_object') or {}
        tags = node_map.get('tags') or {}
        address = node.get('map_address') or {}
        center = object_center(node)
        if (not str(node_id).startswith('osm:node:') or node_map.get('provenance') != 'osm.tags'
                or tags.get('entrance') in {None, '', 'no'} or any(tags.get(k) for k in ('amenity', 'shop', 'office'))
                or address.get('provenance') != 'osm.tags' or not address.get('street') or not address.get('house_number')
                or not node.get('url') or address.get('source_url') != node['url'] or node_map.get('source_url') != node['url']
                or not center or not any(abs(p['lat']-center['latitude']) < 1e-7
                    and abs(p['lon']-center['longitude']) < 1e-7 for r in rings for p in r.get('points') or [])):
            continue
        linked.append({'candidate_id': node_id, 'address': dict(address),
                       'coordinates': center, 'provenance': 'osm_closed_way_node_membership'})
    if not linked:
        return
    anchor = chosen['search_anchor']
    anchor['address_entries'] = linked
    anchor['address_membership_proof'] = dict(membership)
    anchor['address_provenance'] = 'osm_closed_way_node_membership'
    labels = list(dict.fromkeys(e['address']['street'] + ' ' + e['address']['house_number'] for e in linked))
    streets = list(dict.fromkeys(e['address']['street'] for e in linked))
    if len(streets) == 1 and not anchor.get('street'):
        anchor['street'] = streets[0]
    if len(streets) == 1 and anchor.get('street') == streets[0] and not anchor.get('house_number'):
        numbers = list(dict.fromkeys(e['address']['house_number'] for e in linked))
        anchor.update(house_numbers=numbers, house_number=', '.join(numbers))
    if not anchor.get('address'):
        anchor['address'] = ' / '.join(labels)
    if chosen.get('name_provenance') == 'map.placeholder':
        label = anchor['street'] + ' ' + anchor['house_number'] if len(streets) == 1 else anchor['address']
        chosen.update(map_label=chosen['name'], name=label,
                      name_provenance='osm_closed_way_node_membership')


def owner_context_missing(chosen):
    anchor = chosen.get('search_anchor') or {}
    return not (anchor.get('names') or anchor.get('address')
                or anchor.get('street') and anchor.get('house_number'))


_STREET_TYPE = {'ул': 'улица', 'пр': 'проспект', 'пр-т': 'проспект', 'просп': 'проспект', 'пер': 'переулок',
                'пл': 'площадь', 'б-р': 'бульвар', 'бул': 'бульвар', 'наб': 'набережная', 'ш': 'шоссе'}
_NUMBER = r'\d+[0-9a-zа-я/-]*'


def _address_key(text, city):
    """Street words with their type, plus the whole house number (3/5к2 is not 3)."""
    text = str(text or '').casefold().replace('ё', 'е')
    numbers = re.findall(_NUMBER, text)
    words = [_STREET_TYPE.get(word, word) for word in re.findall(r'[a-zа-я][a-zа-я-]*', re.sub(_NUMBER, ' ', text))]
    street = frozenset(word for word in [*words, *numbers[:-1]] if word not in city and word not in {'дом', 'д', 'город', 'г'})
    return street, numbers[-1] if numbers else ''


def search_terms(chosen):
    """Literal search keys of the selected house: each address as (street words, number) and each name."""
    anchor = chosen.get('search_anchor') or {}
    city = set(re.findall(r'[a-zа-я][a-zа-я-]*', str(anchor.get('city') or '').casefold().replace('ё', 'е')))
    addresses = [anchor.get('address'), ' '.join(str(anchor.get(k) or '') for k in ('street', 'house_number'))]
    names = [value for value in anchor.get('names') or [] if str(value or '').strip()]
    keys = {('address', _address_key(value, city)) for value in [*addresses, chosen.get('name'), *names]
            if str(value or '').strip()}
    keys |= {('name', ' '.join(re.findall(r'[0-9a-zа-я]+', str(value).casefold().replace('ё', 'е')))) for value in names}
    return {key for key in keys if key[0] == 'name' or any(key[1])}


_MAP_ADDRESS_KEYS = ('street', 'house_number', 'house_numbers', 'address',
                     'address_entries', 'address_membership_proof', 'address_provenance')
_ADDRESS_DERIVED_NAMES = {'map.placeholder', 'osm.tags.address', 'osm_closed_way_node_membership'}


def apply_owner_address(chosen, address, provenance):
    """The author's address becomes the only search address of the selected house.

    A map address tag can be stale or name another street. Its values move out
    of the search anchor into replaced_map_address; the ID, observed tags,
    map_address and geometry stay.
    """
    anchor = dict(chosen.get('search_anchor') or {})
    replaced = {key: anchor.pop(key) for key in _MAP_ADDRESS_KEYS if key in anchor}
    anchor.update(address=address, address_provenance=provenance)
    chosen = {**chosen, 'search_anchor': anchor}
    if replaced and replaced.get('address') != address:
        chosen['replaced_map_address'] = {**replaced, 'replaced_by': provenance}
    if chosen.get('name_provenance') in _ADDRESS_DERIVED_NAMES:
        chosen.setdefault('map_label', chosen.get('name'))
        chosen.update(name=address, name_provenance=provenance)
    return chosen


def _outline_gap_m(left, right):
    """Shortest gap between two sets of polylines, each a list of (lat, lon), in metres."""
    if not any(left) or not any(right):
        return None
    scale = 111_320 * math.cos(math.radians(next(line for line in left if line)[0][0]))
    def xy(lines):
        return [[(lon * scale, lat * 111_320) for lat, lon in line] for line in lines]
    def gap(point_lines, segment_lines):
        best = math.inf
        for (ax, ay), (bx, by) in ((a, b) for line in segment_lines for a, b in zip(line, line[1:])):
            dx, dy = bx - ax, by - ay
            for px, py in (p for line in point_lines for p in line):
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ((dx * dx + dy * dy) or 1)))
                best = min(best, math.hypot(px - ax - t * dx, py - ay - t * dy))
        return best
    a, b = xy(left), xy(right)
    value = min(gap(a, b), gap(b, a))
    return None if value == math.inf else value


def _rings(candidate):
    return [[(p['lat'], p['lon']) for p in r.get('points') or []]
            for r in (candidate.get('map_geometry') or {}).get('rings') or []]


def _sampled(lines, limit=64):
    # Distances are compared with tens-of-metres thresholds; a very detailed
    # outline does not need every vertex (and must not block a request).
    count = sum(len(line) for line in lines)
    step = max(1, math.ceil(count / limit))
    return [line[::step] + ([line[-1]] if step > 1 and (len(line) - 1) % step else []) for line in lines if line]


def _box(lines, margin_m=0.0):
    points = [p for line in lines for p in line]
    if not points:
        return None
    lat0 = points[0][0]
    dlat, dlon = margin_m / 111_320, margin_m / (111_320 * max(0.1, math.cos(math.radians(lat0))))
    return (min(p[0] for p in points) - dlat, min(p[1] for p in points) - dlon,
            max(p[0] for p in points) + dlat, max(p[1] for p in points) + dlon)


def _overlaps(a, b):
    return bool(a and b) and a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _near_runs(line, box):
    # Keep only consecutive segments whose own box meets the search box.
    runs, run = [], []
    for a, b in zip(line, line[1:]):
        if _overlaps(box, _box([[a, b]])):
            run = run or [a]
            run.append(b)
        elif run:
            runs.append(run)
            run = []
    return runs + ([run] if run else [])


def _label(address):
    return ' '.join(str(address.get(k) or '').strip() for k in ('street', 'house_number')).strip()


def snapshot_surroundings(snapshot, chosen):
    """Map observations beside the selected contour, for discovering its other addresses.

    The owner chooses a house by place, not by address, and a map address tag
    can be stale or name a street away from the building. Named streets within
    35 m, the distance to the tagged street and neighbouring mapped addresses are
    search hints for the building's other addresses. Map proximity does not prove
    which street the facade faces: never facts, never its address, never a new identity.
    """
    outline = _sampled(_rings(chosen))  # bounded work inside the selection transaction
    if not outline or not outline[0]:
        return
    street_box, neighbour_box = _box(outline, 150), _box(outline, 60)
    streets = {}
    for feature in (snapshot.get('streets') or {}).get('features') or []:
        name = str((feature.get('properties') or {}).get('name') or '').strip()
        lines = [[(lat, lon) for lon, lat in line] for line in (feature.get('geometry') or {}).get('coordinates') or []]
        near = [run for line in lines for run in _near_runs(line, street_box)] if name else []
        gap = _outline_gap_m(outline, _sampled(near, 100)) if near else None
        if gap is not None and gap < streets.get(name, math.inf):
            streets[name] = gap
    own = chosen.get('map_address') or {}
    own_label = _label(own).casefold()
    neighbours = {}
    centre = object_center(chosen)
    for other in [*(snapshot.get('candidates') or []), *(snapshot.get('address_objects') or [])]:
        label = _label(other.get('map_address') or {})
        other_centre = object_center(other)
        if (not label or other.get('candidate_id') == chosen.get('candidate_id') or label.casefold() == own_label
                or not centre or not other_centre or offset_m(centre, other_centre) > 150):
            continue
        spot = (other_centre['latitude'], other_centre['longitude'])
        shape = _rings(other) or [[spot, spot]]
        if not _overlaps(neighbour_box, _box(shape)):
            continue
        gap = _outline_gap_m(outline, _sampled(shape, 100))
        if gap is not None and gap <= 60 and gap < neighbours.get(label, (math.inf,))[0]:
            neighbours[label] = (gap, other.get('candidate_id'))
    nearby = [{'name': name, 'distance_m': round(gap)} for name, gap in sorted(streets.items(), key=lambda s: s[1]) if gap <= 35][:3]
    surroundings = {'provenance': 'osm.snapshot_geometry', 'scope': 'search_discovery_only',
        'meaning': 'map proximity to the contour; not proof of the facade street or address',
        'snapshot_id': snapshot.get('snapshot_id'), 'streets_within_35m': nearby,
        'neighbour_addresses': [{'address': label, 'distance_m': round(gap), 'candidate_id': cid}
                                for label, (gap, cid) in sorted(neighbours.items(), key=lambda n: n[1][0])[:6]]}
    tagged = str(own.get('street') or '').strip()
    if tagged:
        distance = streets.get(tagged)
        surroundings['tagged_street_distance_m'] = None if distance is None else round(distance)
        surroundings['tagged_street_within_35m'] = any(s['name'] == tagged for s in nearby)
    if not nearby and not surroundings['neighbour_addresses']:
        return
    chosen['search_anchor'] = {**chosen['search_anchor'], 'map_surroundings': surroundings}


def _spelling_distance(left, right):
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def _neighbourhood_city_spelling(snapshot, chosen, city):
    """A selected addr:city one or two letters off an overwhelming local majority is a typo."""
    counts = {}
    for item in snapshot.get('city_objects') or []:
        value = (item.get('search_anchor') or {}).get('city')
        if value and item.get('candidate_id') != chosen.get('candidate_id'):
            counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    majority, votes = max(counts.items(), key=lambda item: item[1])
    if (majority != city and votes >= 3 and votes >= 3 * counts.get(city, 0)
            and 0 < _spelling_distance(city.casefold(), majority.casefold()) <= 2):
        return majority
    return None


def _majority_city_spelling(snapshot, chosen):
    # Keep the observed tag; the reader-facing city uses the local spelling,
    # whether the tag came from the selected object or a neighbour.
    anchor = chosen['search_anchor']
    corrected = _neighbourhood_city_spelling(snapshot, chosen, anchor['city'])
    if corrected:
        anchor.update(city=corrected, city_provenance='osm.snapshot_majority_spelling',
                      **({'city_selected_tag': anchor['city']} if anchor['city_provenance'] == 'osm.selected.addr_city'
                         else {'city_neighbour_tag': anchor['city']}))


def snapshot_city(snapshot, chosen):
    anchor = chosen.setdefault('search_anchor', {})
    if anchor.get('city'):
        anchor.setdefault('city_provenance', 'osm.selected.addr_city')
        if anchor['city_provenance'] == 'osm.selected.addr_city':
            _majority_city_spelling(snapshot, chosen)
        return True
    center = object_center(chosen)
    neighbors = [(offset_m(center, p) if center and (p := object_center(c)) else 0, c)
        for c in snapshot.get('city_objects') or snapshot.get('candidates') or []
        if (c.get('search_anchor') or {}).get('city') and (c.get('search_anchor') or {}).get('city_provenance')
            not in {'osm.snapshot_neighbor.addr_city', 'osm.selected_center_reverse'}]
    if neighbors:
        source = min(neighbors, key=lambda item: item[0])[1]
        anchor.update(city=source['search_anchor']['city'], city_provenance='osm.snapshot_neighbor.addr_city',
                      city_source_candidate_id=source['candidate_id'])
        _majority_city_spelling(snapshot, chosen)
        return True
    return False


async def reverse_selected_city(service, center):
    osm = service.providers.osm
    receipt = {'status': 'unavailable', 'center': center}
    own = getattr(osm, 'http', None) is None
    client = getattr(osm, 'http', None) or httpx.AsyncClient(timeout=15)
    try:
        response = await client.get(osm.reverse_url, params={'format': 'jsonv2',
            'lat': center['latitude'], 'lon': center['longitude'], 'zoom': 18, 'addressdetails': 1},
            headers={'User-Agent': getattr(osm, 'user_agent', service.settings.osm_user_agent)})
        response.raise_for_status()
        address = response.json().get('address') or {}
        city = next((str(address[k])[:180] for k in ('city', 'town', 'village', 'municipality') if address.get(k)), '')
        receipt.update(status='completed', city=city, provenance='osm.selected_center_reverse')
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        receipt['reason'] = type(exc).__name__
    finally:
        if own:
            await client.aclose()
    return receipt


async def prepare_place_context(service, story_id, body):
    """One selected-object reverse read; it cannot change the owner identity."""
    with service.store.connection() as db:
        row = service._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
    generation = int(research.get('identity_generation') or 0)
    if (body.get('expected_photo_sha256') != row['photo_sha256']
            or body.get('expected_identity_generation', body.get('expected_generation')) != generation):
        return  # The normal command fence reports stale requests, including replay.
    fallback = body.get('fallback')
    if isinstance(fallback, dict):
        center = point(fallback.get('latitude'), fallback.get('longitude'))
        cache_key = 'owner-selected-point-city:' + digest(center)
        if service.store.cache_get(cache_key) is None:
            service.store.cache_put(cache_key, await reverse_selected_city(service, center), 3600)
        return
    snapshot = next((s for s in research.get('place_map_snapshots') or []
        if s['snapshot_id'] == body.get('snapshot_id') and s['identity_generation'] == generation), None)
    if not snapshot:
        return
    ids = body.get('candidate_ids') if body.get('mode') == 'rough' else [body.get('candidate_id') or body.get('poi_id')]
    if not isinstance(ids, list):
        return
    targets = [c for c in snapshot['candidates'] if c['candidate_id'] in ids]
    for chosen in targets:
        snapshot_member_address(snapshot, chosen)
        if snapshot_city(snapshot, chosen):
            continue
        center = object_center(chosen)
        cached = snapshot.setdefault('city_reverse', {}).get(chosen['candidate_id'])
        if cached is None and center:
            cached = await reverse_selected_city(service, center)
            snapshot['city_reverse'][chosen['candidate_id']] = cached
            LOG.info('street_story_map stage=selected_city story_id=%s generation=%s candidate_id=%s status=%s',
                     story_id, generation, chosen['candidate_id'], cached['status'])
        if cached and cached.get('city'):
            chosen['search_anchor'].update(city=cached['city'], city_provenance=cached['provenance'])
    with service.store.tx() as db:
        current = service._story_row(db, story_id)
        latest = json.loads(current['research_json'] or '{}')
        if current['photo_sha256'] != row['photo_sha256'] or int(latest.get('identity_generation') or 0) != generation:
            return
        for saved in latest.get('place_map_snapshots') or []:
            if saved['snapshot_id'] == snapshot['snapshot_id']:
                saved.update(candidates=snapshot['candidates'], city_reverse=snapshot.get('city_reverse') or {})
                for feature in saved['buildings']['features']:
                    selected = next((c for c in targets if c['candidate_id'] == feature['id']), None)
                    if selected:
                        feature['properties']['search_anchor'] = selected['search_anchor']
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(latest), story_id))


def place_action(service, story_id, key, body, *, unknown=False):
    action = 'place_unknown' if unknown else 'place_navigation'
    with service.store.tx() as db:
        row = service._story_row(db, story_id)
        if service._idem(db, key, action, digest({'story_id': story_id, **body}), 'story', story_id):
            return service._story_repr(db, row)
        research = json.loads(row['research_json'] or '{}')
        if (body.get('expected_photo_sha256') != row['photo_sha256']
                or body.get('expected_identity_generation') != int(research.get('identity_generation') or 0)
                or 'expected_candidate_id' not in body
                or body['expected_candidate_id'] != (research.get('visual_identity') or {}).get('candidate_id')):
            raise ConflictError('place_selection_stale', 'Фото или объект изменился. Обновите историю.')
        if row['state'] in {'scheduling', 'scheduled', 'published'}:
            raise ConflictError('place_publication_active', 'Публикация уже подготовлена.')
        if unknown:
            research, _, _ = detach_place_material(service, db, row, research)
            research['place_selection'] = {'mode': 'unknown', 'status': 'place_unknown',
                'photo_sha256': row['photo_sha256'], 'generation': research['identity_generation']}
            db.execute("UPDATE stories SET state='place_unknown',place_name=NULL,draft_text=NULL,summary=NULL,"
                "processed_image_path=NULL,processed_image_url=NULL,vibepublish_asset_ref=NULL,visual_context_json='{}' WHERE id=?", (story_id,))
        research['place_navigation_active'] = True
        db.execute('UPDATE stories SET research_json=?,revision=revision+1,updated_at=? WHERE id=?',
                   (canonical(research), service.store.now(), story_id))
        return service._story_repr(db, service._story_row(db, story_id))


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
    candidate.update(name=name, name_provenance='osm.tags.name' if tags.get('name') else (
        'osm.tags.address' if address.get('street') or address.get('house_number') else 'map.placeholder'), url=(candidate.get('map_object') or {}).get('source_url'),
                     type='building', osm_id=candidate.get('candidate_id'))
    candidate['search_anchor'] = {'city': str(tags.get('addr:city') or '')[:180],
        'street': address.get('street', ''), 'house_number': address.get('house_number', ''),
        'names': [str(tags[k])[:180] for k in ('name', 'old_name', 'alt_name') if tags.get(k)],
        'source': 'osm.observed_tags', 'provenance': 'osm.observed_tags', 'candidate_id': candidate.get('candidate_id'),
        'coordinates': candidate.get('map_coordinates') or {}}
    candidate['osm_observation'] = {key: item[key] for key in (
        'type', 'id', 'tags', 'lat', 'lon', 'center', 'geometry', 'members',
        'version', 'timestamp', 'changeset') if key in item}
    if 'map_coordinates' not in candidate and (center := object_center(candidate)):
        candidate['map_coordinates'] = {**center, 'provenance': 'osm.geometry_center', 'source_url': candidate.get('url')}
        candidate['search_anchor']['coordinates'] = candidate['map_coordinates']
    return candidate


def snapshot_related_objects(snapshot, chosen):
    """Carry spatial/address observations separately; infer no business status."""
    related = []
    addresses = [chosen.get('map_address') or {},
        *((entry.get('address') or {}) for entry in
          (chosen.get('search_anchor') or {}).get('address_entries') or [])]
    for candidate in snapshot.get('organization_objects') or []:
        if candidate.get('candidate_id') == chosen.get('candidate_id'):
            continue
        center = object_center(candidate)
        spatial = geometry_camera_context(chosen.get('osm_observation') or {},
            center['latitude'], center['longitude']) if center else {}
        address = candidate.get('map_address') or {}
        same_address = bool(address.get('street') and address.get('house_number') and any(
            all(str(address.get(k) or '').casefold() == str(a.get(k) or '').casefold()
                for k in ('street', 'house_number')) for a in addresses))
        inside = spatial.get('camera_inside_footprint') is True
        if not inside and not same_address:
            continue
        related.append({**candidate, 'relationship': {
            'selected_candidate_id': chosen['candidate_id'], 'snapshot_id': snapshot['snapshot_id'],
            'basis': [*(['observed_point_inside_building_polygon'] if inside else []),
                      *(['matching_observed_address'] if same_address else [])],
            'point_kind': 'osm_node' if (candidate.get('osm_observation') or {}).get('type') == 'node'
                else 'representative_point',
            'boundary_distance_m': spatial.get('boundary_distance_m'),
            'meaning': 'Spatial/address evidence only; organization and building remain distinct. '
                'The model assesses present use, lifecycle tags and temporal uncertainty.'}})
    chosen['related_map_observations'] = related
    observation = snapshot.get('map_observation') or {}
    if observation.get('source_url') and observation.get('response_sha256') and chosen.get('osm_observation'):
        chosen['retained_map_source'] = {**observation,
            'snapshot_id': snapshot['snapshot_id'], 'selected_candidate_id': chosen['candidate_id'],
            'normalized_text': canonical({'map_response_observation': observation,
                'selected_building': chosen['osm_observation'],
                'related_objects': [c['osm_observation'] for c in related]}),
            'meaning': 'Projection of actually received map objects. Read through webfetch for '
                'owned immutable source/chunk refs before saving a semantic assessment.'}


async def _owner_map(osm, client, latitude, longitude, radius, *, on_primary=None):
    """Reuse adapter routes, fetching complete geometry for map API omissions."""
    dlat = math.degrees(radius / 6_371_000) * 1.001
    dlon = dlat / max(.000001, abs(math.cos(math.radians(latitude))))
    bbox = ','.join(f'{v:.7f}' for v in (latitude-dlat, longitude-dlon, latitude+dlat, longitude+dlon))
    try:
        raw = await osm._map_nearby(client, latitude, longitude, radius)
    except RetryableProviderError:
        query = (f'[out:json][timeout:20];(way["building"]({bbox});relation["building"]({bbox});'
            f'way["highway"]({bbox});nwr[~"^((disused|abandoned|closed|was):)?(amenity|shop|office|tourism|craft)$"~"."]({bbox}););out meta geom center;')
        raw, _ = await osm._overpass_query(client, query, 'owner_map', None, set())
        raw['map_source'] = 'overpass.bounded_geometry'
        return raw
    missing = raw.get('incomplete_building_ids') or []
    raw['map_source'] = 'osm.map_api'
    if on_primary is not None:
        on_primary(raw)
    if missing and callable(getattr(osm, '_overpass_query', None)):
        parts = []
        for cid in missing[:16]:
            _, kind, identifier = cid.split(':')
            if kind in {'way', 'relation'} and identifier.isdigit():
                parts.append(f'{kind}({identifier});')
        if parts:
            try:
                # Other incomplete objects must not withhold already complete
                # selectable buildings beyond the Android response deadline.
                async with asyncio.timeout(OWNER_MAP_ENRICHMENT_SECONDS):
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
            except (RetryableProviderError, TimeoutError) as exc:
                MAP_LOG.info('street_story_owner_map_enrichment outcome=partial reason=%s incomplete_count=%s',
                             type(exc).__name__, len(missing))
                pass  # The remaining complete ways remain available to select.
    raw['incomplete_building_ids'] = missing
    return raw


async def place_map(service, story_id, *, latitude=None, longitude=None, radius_m=350, zoom=None):
    """Join identical active reads; each completed snapshot remains in the existing store."""
    with service.store.connection() as db:
        row = service._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
        scope = (row['photo_sha256'], int(research.get('identity_generation') or 0))
    key = digest([story_id, scope, latitude, longitude, radius_m, zoom,
                  research.get('place_map_focus'), (research.get('place_map') or {}).get('center')])
    requests = service._owner_map_requests
    task = requests.get(key)
    if task is None:
        task = asyncio.create_task(_place_map(service, story_id, latitude=latitude, longitude=longitude,
                                             radius_m=radius_m, zoom=zoom))
        requests[key] = task

        def release(done):
            if requests.get(key) is done:
                requests.pop(key)
            if not done.cancelled():
                done.exception()  # Consume a closed read failure if all waiters disconnected.

        task.add_done_callback(release)
    else:
        MAP_LOG.info('street_story_owner_map_read story_id=%s request_key=%s outcome=joined', story_id, key)
    # A disappearing view cancels its waiter, never another client's same read.
    return await asyncio.shield(task)


async def _place_map(service, story_id, *, latitude=None, longitude=None, radius_m=350, zoom=None):
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
        focus = research.get('place_map_focus') or {}
        center = point(latitude, longitude) if latitude is not None or longitude is not None else (
            point(focus['lat'], focus['lon']) if focus else saved_map.get('center') or camera)
        if not center:
            raise ConflictError('photo_gps_required', 'В фото нет координат GPS — выберите снимок с геолокацией.')
        effective_zoom = zoom or focus.get('zoom') or saved_map.get('zoom') or 17
        cache_key = digest(['related-map-observations-v1', scope, center['latitude'], center['longitude'], radius_m, effective_zoom])
        cached = next((s for s in reversed(research.get('place_map_snapshots') or [])
                       if s.get('cache_key') == cache_key), None)
        if cached:
            return {**{k: v for k, v in cached.items() if k != 'candidates'},
                    'revision': row['revision'], 'selected': research.get('place_selection'), 'camera': camera}
    started = time.monotonic()
    MAP_LOG.info('street_story_owner_map_read story_id=%s scope_key=%s outcome=started', story_id, cache_key)
    received_primary = None

    def preserve_primary(raw):
        nonlocal received_primary
        received_primary = raw

    try:
        async with asyncio.timeout(OWNER_MAP_REQUEST_SECONDS):
            async with httpx.AsyncClient(timeout=25, headers={'User-Agent': service.settings.osm_user_agent}) as client:
                raw = await _owner_map(service.providers.osm, client, center['latitude'], center['longitude'], radius_m,
                                       on_primary=preserve_primary)
    except TimeoutError as exc:
        # The request deadline may preempt optional enrichment before its own
        # timeout. Keep the actual primary response instead of losing contours
        # already received. Empty/incomplete geometry still cannot be selected.
        if received_primary and any(
            (item.get('tags') or {}).get('building') not in {None, '', 'no'}
            and item.get('identity_eligible') is not False and _geometry(_candidate(item))
            for item in expand_disjoint_building_components(received_primary.get('elements') or [])
        ):
            raw = received_primary
            MAP_LOG.info('street_story_owner_map_read story_id=%s scope_key=%s outcome=partial_geometry_deadline duration_ms=%s',
                         story_id, cache_key, round((time.monotonic() - started) * 1000))
        else:
            MAP_LOG.info('street_story_owner_map_read story_id=%s scope_key=%s outcome=unavailable duration_ms=%s',
                         story_id, cache_key, round((time.monotonic() - started) * 1000))
            raise RetryableProviderError('owner_map_response_deadline') from exc
    except Exception as exc:
        MAP_LOG.info('street_story_owner_map_read story_id=%s scope_key=%s outcome=failed error_type=%s duration_ms=%s',
                     story_id, cache_key, type(exc).__name__, round((time.monotonic() - started) * 1000))
        raise
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
                                   'label': candidate['name'], 'address': candidate.get('map_address') or {},
                                   'search_anchor': candidate['search_anchor'],
                                   'tags': {key: str(tags[key])[:180] for key in
                                            ('name', 'building', 'addr:city', 'addr:street', 'addr:housenumber')
                                            if tags.get(key)}}})
        elif tags.get('highway') and len(streets) < 150:
            lines = (candidate.get('map_geometry') or {}).get('lines') or []
            if lines and all(len(line) <= 1500 for line in lines):
                streets.append({'type': 'Feature', 'id': cid, 'geometry': {'type': 'MultiLineString',
                    'coordinates': [[[p['lon'], p['lat']] for p in line] for line in lines]},
                    'properties': {'name': str(tags.get('name') or '')[:180]}})
    city_objects = [_candidate(item) for item in raw.get('elements') or [] if (item.get('tags') or {}).get('addr:city')]
    address_objects = [_candidate(item) for item in raw.get('elements') or [] if (item.get('tags') or {}).get('entrance')
        and (item.get('tags') or {}).get('addr:street') and (item.get('tags') or {}).get('addr:housenumber')]
    organization_objects = [{**_candidate(item), 'type': 'mapped_organization'} for item in raw.get('elements') or []
        if has_organization_tags(item.get('tags') or {})]
    for candidate in candidates:
        snapshot_member_address({'address_objects': address_objects}, candidate)
        snapshot_city({'city_objects': city_objects}, candidate)
    by_id = {c['candidate_id']: c for c in candidates}
    for feature in buildings:
        c = by_id[feature['id']]
        feature['properties'].update(name=c['name'], label=c['name'])
    snapshot = {'snapshot_id': 'map_' + uuid.uuid4().hex, 'photo_sha256': scope[0],
        'identity_generation': scope[1], 'center': {k: center[k] for k in ('latitude', 'longitude')},
        'camera': camera, 'radius_m': radius_m, 'zoom': effective_zoom, 'cache_key': cache_key, 'city_objects': city_objects, 'address_objects': address_objects,
        'organization_objects': organization_objects, 'map_observation': raw.get('map_observation'),
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
    MAP_LOG.info('street_story_owner_map_read story_id=%s scope_key=%s outcome=ready duration_ms=%s buildings=%s incomplete=%s',
                 story_id, cache_key, round((time.monotonic() - started) * 1000), len(buildings), snapshot['incomplete_building_count'])
    return snapshot


def confirm_place(service, story_id, key, body):
    if body.get('mode') not in {'exact', 'rough'}:
        raise ConflictError('place_mode_unsupported', 'Выберите точный дом или приблизительную группу.')
    if body.get('mode') == 'exact' and body.get('candidate_ids'):
        raise ConflictError('place_exact_cardinality', 'В точном режиме подтвердите только один дом.')
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
        if body['mode'] == 'rough':
            return confirm_group(service, db, row, research, command, body)
        fallback = body.get('fallback')
        if fallback is not None:
            if not isinstance(fallback, dict) or body.get('candidate_id') or body.get('poi_id'):
                raise ConflictError('place_fallback_invalid', 'Подтвердите точку и адрес явно.')
            coordinates = point(fallback.get('latitude'), fallback.get('longitude'))
            address, name = (str(fallback.get(k) or '').strip()[:300] for k in ('address', 'name'))
            city_receipt = service.store.cache_get('owner-selected-point-city:' + digest(coordinates)) or {}
            city = city_receipt.get('city') or ''
            cid = 'owner:place:' + digest([coordinates])[:24]
            chosen = {'candidate_id': cid, 'name': name or address or 'Выбранная точка дома', 'type': 'owner_point_address',
                'map_coordinates': {**coordinates, 'provenance': 'owner.explicit_point'},
                'map_address': {'address': address, 'city': city, 'provenance': 'owner.explicit_address'},
                'search_anchor': {'city': city, 'address': address, 'names': [name] if name else [],
                    'coordinates': coordinates, 'city_provenance': city_receipt.get('provenance'),
                    'source': 'owner.explicit_point_address', 'provenance': 'owner.explicit_point_address', 'candidate_id': cid}}
        else:
            cid = body.get('candidate_id') or body.get('poi_id')
            snapshot = next((s for s in research.get('place_map_snapshots') or []
                             if s['snapshot_id'] == body.get('snapshot_id')), None)
            if not snapshot or snapshot['photo_sha256'] != row['photo_sha256'] or snapshot['identity_generation'] != generation:
                raise ConflictError('place_snapshot_stale', 'Обновите карту перед подтверждением.')
            chosen = next((c for c in snapshot['candidates'] if c['candidate_id'] == cid), None)
            if not chosen:
                raise ConflictError('place_candidate_unknown', 'Выберите один показанный контур здания.')
            snapshot_member_address(snapshot, chosen)
            snapshot_city(snapshot, chosen)
            snapshot_related_objects(snapshot, chosen)
            snapshot_surroundings(snapshot, chosen)
        owner_context = body.get('owner_context')
        if owner_context is not None:
            if not isinstance(owner_context, dict) or any(k not in {'city', 'address', 'name'}
                    or not isinstance(v, str) or len(v) > 300 for k, v in owner_context.items()):
                raise ConflictError('place_owner_context_invalid', 'Нужны короткие сведения автора о выбранном доме.')
            chosen = {**chosen, 'search_anchor': dict(chosen['search_anchor'])}
            owner_context = {k: v.strip() for k, v in owner_context.items() if v.strip()}
            if owner_context.get('address'):
                chosen = apply_owner_address(chosen, owner_context['address'], 'owner.explicit_selection_hint')
            anchor = chosen['search_anchor']
            if owner_context.get('name'):
                anchor['names'] = list(dict.fromkeys([*anchor.get('names', []), owner_context['name']]))
            observed_name = ((chosen.get('map_object') or {}).get('tags') or {}).get('name')
            owner_label = owner_context.get('name') or owner_context.get('address')
            if cid.startswith('osm:') and not observed_name and owner_label:
                # An author's literal label replaces only navigation context.
                # The selected ID, observed tags and geometry stay unchanged.
                chosen.setdefault('map_label', chosen['name'])
                chosen['name'] = owner_label
                chosen['name_provenance'] = 'owner.explicit_selection_hint'
            anchor['owner_context'] = owner_context
            anchor['owner_context_provenance'] = 'owner.explicit_selection_hint'
        return transition_owner_selection(service, db, row, research, chosen, command,
                                          body=body, snapshot=None if fallback else snapshot, fallback=bool(fallback))


def confirm_group(service, db, row, research, command, body):
    ids = body.get('candidate_ids')
    if (not isinstance(ids, list) or not 2 <= len(ids) <= 16
            or any(not isinstance(cid, str) for cid in ids) or len(set(ids)) != len(ids)
            or body.get('candidate_id') or body.get('poi_id') or body.get('fallback')):
        raise ConflictError('place_group_invalid', 'Выберите от 2 до 16 разных показанных зданий.')
    generation = int(research.get('identity_generation') or 0)
    snapshot = next((s for s in research.get('place_map_snapshots') or []
                     if s['snapshot_id'] == body.get('snapshot_id')), None)
    if not snapshot or snapshot['photo_sha256'] != row['photo_sha256'] or snapshot['identity_generation'] != generation:
        raise ConflictError('place_snapshot_stale', 'Обновите карту перед подтверждением группы.')
    catalog = {c['candidate_id']: c for c in snapshot['candidates']}
    if any(cid not in catalog for cid in ids):
        raise ConflictError('place_candidate_unknown', 'Группа должна состоять из показанных контуров зданий.')
    context = body.get('owner_context') or {}
    if (not isinstance(context, dict) or any(k not in {'city', 'address', 'name'}
            or not isinstance(v, str) or len(v) > 300 for k, v in context.items())):
        raise ConflictError('place_owner_context_invalid', 'Нужны короткие сведения автора о группе.')
    members = json.loads(canonical([catalog[cid] for cid in ids]))
    for member in members:
        snapshot_city(snapshot, member)
    location = selection_location(row, members)
    research, generation, now = detach_place_material(service, db, row, research)
    group = {'mode': 'rough', 'group_id': 'group_' + digest([row['id'], command, generation, ids])[:24],
        'status': 'identifying', 'candidate_id': None, 'narrowed_candidate_id': None,
        'candidate_ids': ids, 'candidates': members, 'snapshot_id': snapshot['snapshot_id'],
        'features': {'type': 'FeatureCollection', 'features': [feature for feature in snapshot['buildings']['features']
            if feature['id'] in ids]}, 'owner_context': context,
        'context_provenance': 'owner.explicit_group_context', 'basis': 'owner_rough_osm_group',
        'generation': generation, 'photo_sha256': row['photo_sha256'], 'command_id': command, 'confirmed_at': now, **location}
    research['place_selection'] = group
    research['visual_identity'] = {'status': 'uncertain', 'selection_basis': 'owner_rough_group',
        'candidate_id': None, 'candidate_name': None, 'candidates': members, 'observed_candidates': members,
        'owner_group_id': group['group_id'], 'photo_sha256': row['photo_sha256'], 'generation': generation}
    if body.get('owner_comment'):
        research['transcript'] = str(body['owner_comment'])[:5000]
    db.execute("UPDATE stories SET place_name=NULL,state='identifying',summary=NULL,draft_text=NULL,"
        "processed_image_path=NULL,processed_image_url=NULL,vibepublish_asset_ref=NULL,visual_context_json='{}',"
        "research_json=?,error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
        (canonical(research), now, row['id']))
    from .research_budget import ensure_budget
    ensure_budget(service, row['id'], db=db, explicit=True)
    jid = service._enqueue_job(db, row['id'], 'identity', 'owner-group:' + digest([row['id'], command, generation]),
        {'identity_generation': generation, 'photo_sha256': row['photo_sha256'], 'owner_group_id': group['group_id'],
         'queue_priority': 'interactive'})
    LOG.info('street_story_map stage=group_confirmed story_id=%s generation=%s group_id=%s members=%s job_id=%s',
             row['id'], generation, group['group_id'], len(ids), jid)
    if location['location_basis'] == 'owner_relocated':
        LOG.info('street_story_place_relocated story_id=%s generation=%s gps_offset_m=%s',
                 row['id'], generation, location['gps_offset_m'])
    return service._story_repr(db, service._story_row(db, row['id']))


def detach_place_material(service, db, row, research):
    """Detach the prior target through the same generation, jobs and leases."""
    story_id = row['id']
    generation = int(research.get('identity_generation') or 0)
    from .poi_memory import sync_poi_review_from_story
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
    keep = {'identity_mode', 'location_provenance', 'place_map_snapshots', 'place_map', 'place_map_focus',
            'owner_place_hint', 'owner_place_areas',
            'research_controls', 'research_control_revision', 'research_budget'}
    # External operation receipts remain addressable by their original generation.
    keep.update(k for k in research if 'receipt' in k or 'operation' in k)
    research = {k: v for k, v in research.items() if k in keep}
    generation += 1
    research['identity_generation'] = generation
    research['fact_research_cancelled'] = False
    research['identity_research_cancelled'] = False
    return research, generation, now


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
    from .poi_memory import ensure_poi_identity, hydrate_story_facts
    research, generation, now = detach_place_material(service, db, row, research)
    confirmed = {'status': 'owner_confirmed', 'selection_basis': selection_basis, 'owner_selected': True,
        'candidate_id': cid, 'candidate_name': chosen['name'], 'candidate_name_provenance': chosen.get('name_provenance'),
        'candidate_url': chosen.get('url'),
        'photo_sha256': row['photo_sha256'], 'generation': generation, 'confidence': None,
        'search_anchor': chosen['search_anchor'], 'candidates': [chosen],
        'approval_evidence': approval or {'kind': 'owner_map_ui', 'command_id': command, 'confirmed_at': now},
        'source_links': [chosen['url']] if chosen.get('url') else []}
    research['visual_identity'] = confirmed
    if owner_context_missing(chosen):
        research['identity_clarification'] = {'question': 'Как называется этот выбранный дом или какой у него адрес?',
            'reason': 'owner_selected_context_missing', 'photo_sha256': row['photo_sha256'],
            'generation': generation, 'scope': 'search_context_only'}
    if body.get('owner_comment'):
        research['transcript'] = str(body['owner_comment'])[:5000]
    research['place_selection'] = {'mode': 'exact', 'candidate_id': cid, 'poi_id': cid,
        'candidate': chosen, 'snapshot_id': body.get('snapshot_id'), 'generation': generation,
        'photo_sha256': row['photo_sha256'], 'basis': 'owner_named_voice' if selection_basis == 'owner_live' else ('owner_explicit_point_address' if fallback else 'owner_exact_osm_contour'),
        'command_id': command, 'confirmed_at': now, **selection_location(row, [chosen])}
    if research['place_selection']['location_basis'] == 'owner_relocated':
        LOG.info('street_story_place_relocated story_id=%s generation=%s gps_offset_m=%s',
                 story_id, generation, research['place_selection']['gps_offset_m'])
    db.execute("UPDATE stories SET place_name=?,state='researching',summary=NULL,draft_text=NULL,processed_image_path=NULL,"
               "processed_image_url=NULL,vibepublish_asset_ref=NULL,visual_context_json='{}',research_json=?,"
               "error_code=NULL,error_message=NULL,revision=revision+1,updated_at=? WHERE id=?",
               (chosen['name'], canonical(research), now, story_id))
    target = chosen.get('map_coordinates') or {}
    research['poi_id'] = ensure_poi_identity(db, confirmed, latitude=target.get('latitude'), longitude=target.get('longitude'), now=now)
    research['poi_reused_fact_count'] = hydrate_story_facts(db, confirmed, story_id)
    if owner_context_missing(chosen):
        research['owner_research_waiting_context'] = {'photo_sha256': row['photo_sha256'],
            'generation': generation, 'candidate_id': cid, 'command_id': command,
            'reason': 'owner_selected_context_missing'}
        db.execute("UPDATE stories SET state='needs_review',research_json=? WHERE id=?", (canonical(research), story_id))
        LOG.info('street_story_map stage=context_required story_id=%s generation=%s candidate_id=%s', story_id, generation, cid)
        return service._story_repr(db, service._story_row(db, story_id))
    job = enqueue_owner_research(service, db, row, research, command, cid)
    result = service._story_repr(db, service._story_row(db, story_id))
    LOG.info('street_story_map stage=confirmed story_id=%s generation=%s candidate_id=%s job_id=%s', story_id, generation, cid, job)
    return result


def enqueue_owner_research(service, db, row, research, command, cid):
    story_id, generation = row['id'], int(research.get('identity_generation') or 0)
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
    return job

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
