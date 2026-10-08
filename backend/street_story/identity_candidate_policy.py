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
    return candidate.get("identity_eligible") is not False


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
