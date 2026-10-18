"""Host-side final-identity eligibility; search context may be broader than a physical object."""
from __future__ import annotations

import math
import re

_LOCALITY = re.compile(
    r"[—–-]\s*(?:город|пос[её]лок(?:\s+городского\s+типа)?|село|деревня|"
    r"муниципальное\s+образование|городской\s+округ|муниципальный\s+округ|"
    r"район|область|край|административно[- ]территориальная\s+единица)\b",
    re.IGNORECASE,
)


def wikipedia_identity_eligible(title: str, extract: str) -> bool:
    """False for locality/container articles that can supply context but not the object name."""
    text = re.sub(r"\s+", " ", str(extract or ""))[:1400]
    if _LOCALITY.search(text):
        return False
    title_text = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    if re.search(r"\b(?:район|область|городской округ|муниципальный округ)\b", title_text):
        return False
    return True


def osm_identity_eligible(tags: dict) -> bool:
    """Settlement labels are context; explicit physical-object tags remain usable."""
    if str(tags.get('place') or '').casefold() not in {'city', 'town', 'village'}:
        return True
    return any(str(tags.get(key) or '').casefold() not in {'', 'no'}
               for key in ('building', 'historic', 'man_made'))


def candidate_identity_eligible(candidate: dict) -> bool:
    return (candidate.get("identity_eligible") is not False
        and candidate.get('identity_role') != 'multi_component_building_context')


def promote_observed_candidates(active: list[dict], observed: list[dict], candidate_ids: list[str]) -> list[dict]:
    """Promote exact host-observed map IDs without creating an entity or proof."""
    by_id = {item.get('candidate_id'): item for item in observed if isinstance(item, dict)}
    output = list(active)
    present = {item.get('candidate_id') for item in active}
    for cid in candidate_ids:
        if not isinstance(cid, str):
            continue
        item = by_id.get(cid)
        if (not cid.startswith('osm:') or cid in present
                or not item or not candidate_identity_eligible(item)):
            continue
        output.append({**item, 'shortlist_bucket': 'observed_promotion'})
        present.add(cid)
    return output


def research_priority_schema(candidate_ids):
    """Optional research guidance in the existing image decision, never proof."""
    cid = {'type': 'string', 'enum': list(dict.fromkeys(candidate_ids))}
    text = {'type': 'string', 'minLength': 1, 'maxLength': 600}
    return {'type': 'object', 'properties': {
        'candidate_ids': {'type': 'array', 'maxItems': len(candidate_ids), 'uniqueItems': True, 'items': cid},
        'reason': text,
        'next_question': {'type': 'string', 'maxLength': 600},
        'next_step': {'type': 'string', 'enum': ['text', 'existing_images', 'targeted_search', 'expand_reserve']},
        'contradictions': {'type': 'array', 'maxItems': len(candidate_ids), 'items': {
            'type': 'object', 'properties': {'candidate_id': cid, 'reason': text,
                'conditions': text, 'scope': {'type': 'string', 'enum': ['physical_body', 'article', 'facade']},
                'source_url': {'type': 'string', 'maxLength': 1000}},
            'required': ['candidate_id', 'reason', 'conditions', 'scope', 'source_url'],
            'additionalProperties': False}}},
        'required': ['candidate_ids', 'reason', 'next_question', 'next_step', 'contradictions'],
        'additionalProperties': False}


def physical_research_priority(story, payload, observed, receipt):
    """Project a closed SOURCE decision onto reversible research state.

    Unexamined bodies and scoped material contradictions remain recoverable.
    Legacy nominations are exploratory priorities, not a new shortlist result.
    Neither cardinality nor a model priority can authorize identity or POI facts.
    """
    import hashlib
    import json
    from jsonschema import Draft202012Validator
    from .identity_architectural_context import _physical_subject
    catalog = {c['candidate_id']: c for c in observed if isinstance(c, dict)
        and isinstance(c.get('candidate_id'), str) and _physical_subject(c)
        and candidate_identity_eligible(c)}
    if (not catalog or not story.get('photo_sha256') or not isinstance(payload, dict) or not isinstance(receipt, dict)
            or not (receipt.get('joint_image_input') is True or receipt.get('source_image_input') is True)
            or receipt.get('source_photo_sha256') != story.get('photo_sha256')):
        return None
    decision = payload.get('accepted_architectural_text') or {}
    guidance = decision.get('research_priority') or payload.get('research_priority')
    explicit = guidance is not None
    if explicit:
        if not Draft202012Validator(research_priority_schema(list(catalog))).is_valid(guidance):
            return None
        active = guidance['candidate_ids']
        contradictions = guidance['contradictions']
        # An exploratory subject may itself be the model's unresolved question.
        # Keep both its requested investigation and scoped contrary evidence;
        # this projection never accepts identity or clears contradictions.
        source_urls = {a.get('url') for a in receipt.get('articles') or []}
        if any(c['scope'] != 'physical_body' and c['source_url'] not in source_urls for c in contradictions):
            return None
    else:
        geometry = payload.get('accepted_geometry') or (payload.get('rejected_geometry') or {}).get('decision') or {}
        action = geometry.get('next_action') or {}
        declared = [*(payload.get('observed_candidate_ids') or []),
            *(c.get('candidate_id') for c in payload.get('spatial_hypotheses') or []
                if c.get('support_status') in {'plausible', 'spatially_supported'}),
            *(action.get('target_candidate_ids') or []), geometry.get('candidate_id'), decision.get('candidate_id'),
            *(c.get('group_key') or c.get('subject_id') for c in payload.get('first_wave_hypotheses') or [])]
        active = list(dict.fromkeys(cid for cid in declared if cid in catalog))
        contradictions = []  # Legacy rejected alternatives are not global exclusions.
        guidance = {'reason': action.get('reason') or decision.get('discriminating_combination') or
            'Existing closed model nominations; physical identity remains unconfirmed.',
            'next_question': '; '.join(decision.get('limitations') or [])[:600],
            'next_step': 'existing_images' if decision else 'text'}
    captured = json.loads(story.get('research_json') or '{}')
    return {'photo_sha256': story['photo_sha256'],
        'generation': int(story.get('_identity_generation', captured.get('identity_generation') or 0)),
        'control_revision': int(story.get('_identity_research_control_revision',
            ((captured.get('research_controls') or {}).get('identity') or {}).get('revision') or 0)),
        'map_sha256': receipt.get('map_image_sha256'),
        'original_source_sha256': receipt.get('original_source_sha256'),
        'model_response_sha256': hashlib.sha256(json.dumps(payload, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'active_candidate_ids': active,
        'reserve_candidate_ids': [cid for cid in catalog if cid not in active],
        'contradictions': contradictions, 'reason': guidance['reason'],
        'next_question': guidance['next_question'], 'next_step': guidance['next_step'],
        'explicit_model_selection': explicit, 'identity_established': False}


def order_research_candidates(candidates, priority):
    """Stable priority only: retain every reserve candidate and material."""
    active = set((priority or {}).get('active_candidate_ids') or [])
    def rank(item):
        links = {item.get('candidate_id'), item.get('physical_subject_candidate_id'),
            *(item.get('lookup_candidate_ids') or [])}
        links.update(link.get('candidate_id') for link in item.get('mapped_wikipedia_sources') or [])
        return 0 if links.intersection(active) else 1
    return sorted(candidates, key=rank)


def wikipedia_coordinate_context(page: dict, story: dict | None, radius_m: float | None) -> dict:
    """Authoritative coordinates constrain this local search, never prove a match.

    Missing capture/coordinates preserve eligibility. This footprint is a query
    domain, not a universal camera sightline or a lens-derived distance claim.
    """
    def point(lat, lon):
        if isinstance(lat, bool) or isinstance(lon, bool):
            return None
        try:
            lat, lon = float(lat), float(lon)
            if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
        except (TypeError, ValueError, OverflowError):
            pass
        return None
    coordinates = page.get('coordinates') or []
    if not isinstance(coordinates, list):
        return {}
    # Ask GeoData for primary coordinates; ignore off-Earth and malformed data.
    coordinate = next((item for item in coordinates if isinstance(item, dict)
        and item.get('primary') is True and item.get('globe', 'earth') == 'earth'
        and point(item.get('lat'), item.get('lon')) is not None), None)
    if coordinate is None:
        return {}
    target = point(coordinate.get('lat'), coordinate.get('lon'))
    result = {'lat': target[0], 'lon': target[1], 'coordinate_provenance': 'wikipedia.coordinates.primary'}
    capture = point((story or {}).get('latitude'), (story or {}).get('longitude'))
    if capture is None:
        return result
    phi1, phi2 = math.radians(capture[0]), math.radians(target[0])
    a = math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(target[1] - capture[1]) / 2) ** 2
    distance = 2 * 6_371_000 * math.asin(math.sqrt(min(1.0, max(0.0, a))))
    result.update(distance_m=round(distance, 1), distance_provenance='capture_to_wikipedia_primary_coordinate')
    try:
        radius = float(radius_m)
    except (TypeError, ValueError, OverflowError):
        return result
    if not math.isfinite(radius) or radius <= 0:
        return result
    result['local_search_radius_m'] = radius
    if distance > radius:
        result.update(identity_eligible=False, identity_ineligible_reason='outside_local_search_footprint')
    return result
