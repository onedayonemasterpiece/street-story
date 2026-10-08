"""Allowlisted structured map anchors; no address or physical identity inference."""
from __future__ import annotations

import math


def map_entry_context(item: dict, *, source_url: str | None = None, coordinate_provenance: str | None = None) -> dict:
    tags = item.get('tags') if isinstance(item.get('tags'), dict) else {}
    kind = item.get('osm_type') or item.get('type')
    object_id = item.get('osm_id') or item.get('id')
    osm_object = kind in {'node', 'way', 'relation'} and bool(object_id)
    source_url = source_url or (f'https://www.openstreetmap.org/{kind}/{object_id}' if osm_object else None)
    result = {}
    if osm_object:
        result['candidate_id'] = f'osm:{kind}:{object_id}'
    mapped_tags = {key: str(tags[key])[:180] for key in (
        'name', 'building', 'building:part', 'entrance', 'amenity', 'shop', 'tourism',
        'historic', 'highway', 'man_made', 'landuse', 'leisure', 'place') if tags.get(key)}
    mapped_kind = {key: str(item[key])[:100] for key in ('category', 'class', 'addresstype') if item.get(key)}
    if item.get('osm_type') and item.get('type'):
        mapped_kind['type'] = str(item['type'])[:100]
    if mapped_tags or mapped_kind:
        result['map_object'] = {**mapped_kind, 'tags': mapped_tags,
            'provenance': 'osm.tags' if mapped_tags else 'nominatim.reverse',
            'source_url': source_url, 'scope': 'mapped_entry_only'}
        entrances = item.get('building_entrance_node_ids')
        if kind == 'way' and mapped_tags.get('building') not in {None, '', 'no'} and isinstance(entrances, list):
            result['map_object']['building_entrances'] = {
                'proof': 'osm_closed_way_node_membership', 'source_url': source_url,
                'candidate_ids': [f'osm:node:{value}' for value in entrances
                                  if isinstance(value, int) and not isinstance(value, bool) and value > 0]}
    address = {key: str(tags[field]).strip()[:180] for key, field in (
        ('street', 'addr:street'), ('house_number', 'addr:housenumber')) if tags.get(field)}
    origin = 'osm.tags'
    if not address and isinstance(item.get('address'), dict):
        address = {key: str(item['address'][field]).strip()[:180] for key, field in (
            ('street', 'road'), ('house_number', 'house_number')) if item['address'].get(field)}
        origin = 'nominatim.reverse.address'
    if address:
        result['map_address'] = {**address, 'provenance': origin,
                                 'source_url': source_url, 'scope': 'mapped_entry_only'}
    if tags.get('highway') and tags.get('name'):
        result['road_name'] = str(tags['name']).strip()[:180]
    center = item.get('center') if isinstance(item.get('center'), dict) else {}
    raw_lat, raw_lon = item.get('lat', center.get('lat')), item.get('lon', center.get('lon'))
    if isinstance(raw_lat, bool) or isinstance(raw_lon, bool):
        return result
    try:
        lat, lon = float(raw_lat), float(raw_lon)
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            result['map_coordinates'] = {'latitude': lat, 'longitude': lon,
                'provenance': coordinate_provenance or ('osm.center' if center else 'osm.position'), 'source_url': source_url}
    except (TypeError, ValueError, OverflowError):
        pass
    return result
