from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx

from .config import Settings, reveal
from .db import Store
from .fact_conflicts import conflict_scan_items, normalize_model_conflict_records
from .research_runs import (
    chunk_checkpoint,
    mark_chunk,
    persist_source_version,
    record_chunk_batch,
    register_discovered_source,
)


from .errors import MalformedProviderResponse, PermanentProviderError, RetryableProviderError
from .gemini import GeminiExecutor, GeminiKeyPool, GeminiPolicy, GeminiUnavailable
WIKIPEDIA_USER_AGENT = "StreetStoryWikipediaBot/0.1 (https://github.com/onedayonemasterpiece/street-story; nearby research)"


def _stable_cache_key(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return prefix + ":" + hashlib.sha256(raw).hexdigest()


class OSMClient:
    def __init__(self, store: Store, user_agent: str, http: httpx.AsyncClient | None = None):
        self.store = store
        self.user_agent = user_agent
        self.http = http
        self.reverse_url = "https://nominatim.openstreetmap.org/reverse"
        self.overpass_url = "https://overpass-api.de/api/interpreter"

    async def lookup(self, lat: float, lon: float) -> dict[str, Any]:
        key = _stable_cache_key("osm-visible-nearby-v4", [round(lat, 6), round(lon, 6)])
        cached = self.store.cache_get(key)
        if cached is not None:
            return cached
        own = self.http is None
        client = self.http or httpx.AsyncClient(timeout=20, headers={"User-Agent": self.user_agent})
        try:
            reverse_response = await client.get(
                self.reverse_url,
                params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18, "addressdetails": 1, "namedetails": 1},
                headers={"User-Agent": self.user_agent},
            )
            reverse_response.raise_for_status()
            reverse = reverse_response.json()
            radius_m = 600
            close_radius_m = 160
            landmark_query = f"""[out:json][timeout:12];(
                nwr(around:{radius_m},{lat:.6f},{lon:.6f})[historic];
                nwr(around:{radius_m},{lat:.6f},{lon:.6f})[wikipedia];
                nwr(around:{radius_m},{lat:.6f},{lon:.6f})[heritage];
                nwr(around:{radius_m},{lat:.6f},{lon:.6f})[wikidata][name];
                nwr(around:{radius_m},{lat:.6f},{lon:.6f})[tourism~"^(attraction|museum|gallery|viewpoint|artwork)$"];
                nwr(around:{radius_m},{lat:.6f},{lon:.6f})[amenity~"^(place_of_worship|theatre|arts_centre|townhall|library)$"][name];
                nwr(around:{radius_m},{lat:.6f},{lon:.6f})[man_made~"^(tower|lighthouse|obelisk)$"][name];
                nwr(around:{radius_m},{lat:.6f},{lon:.6f})[barrier~"^(city_wall|gate)$"];
                nwr(around:{radius_m},{lat:.6f},{lon:.6f})[bridge][name];
                nwr(around:{radius_m},{lat:.6f},{lon:.6f})[leisure~"^(park|garden)$"][name];
            );out center tags 240;"""
            nearby_query = f"""[out:json][timeout:12];(
                nwr(around:{close_radius_m},{lat:.6f},{lon:.6f})[building];
                nwr(around:{close_radius_m},{lat:.6f},{lon:.6f})[name];
            );out center tags 180;"""

            landmark_response = await client.post(
                self.overpass_url,
                content=landmark_query.encode(),
                headers={"User-Agent": self.user_agent, "Content-Type": "text/plain; charset=utf-8"},
            )
            landmark_response.raise_for_status()
            nearby_response = await client.post(
                self.overpass_url,
                content=nearby_query.encode(),
                headers={"User-Agent": self.user_agent, "Content-Type": "text/plain; charset=utf-8"},
            )
            nearby_response.raise_for_status()

            def normalized(raw: Any, bucket: str) -> dict[str, Any] | None:
                if not isinstance(raw, dict):
                    return None
                tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
                if str(raw.get("type") or "") == "relation" and (
                    tags.get("route")
                    or tags.get("boundary")
                    or str(tags.get("type") or "") in {"route", "boundary", "network"}
                ):
                    return None
                center = raw.get("center") if isinstance(raw.get("center"), dict) else {}
                try:
                    item_lat = float(raw.get("lat", center.get("lat")))
                    item_lon = float(raw.get("lon", center.get("lon")))
                    phi1, phi2 = math.radians(lat), math.radians(item_lat)
                    dphi = math.radians(item_lat - lat)
                    dlambda = math.radians(item_lon - lon)
                    a = (
                        math.sin(dphi / 2) ** 2
                        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
                    )
                    distance_m = 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))
                except (TypeError, ValueError):
                    distance_m = float(radius_m + 1)

                historic = str(tags.get("historic") or "")
                tourism = str(tags.get("tourism") or "")
                amenity = str(tags.get("amenity") or "")
                man_made = str(tags.get("man_made") or "")
                barrier = str(tags.get("barrier") or "")
                if (
                    historic
                    or tags.get("wikipedia")
                    or tags.get("heritage")
                    or barrier in {"city_wall", "gate"}
                    or man_made in {"tower", "lighthouse", "obelisk"}
                ):
                    salience_rank = 0
                elif tourism in {"attraction", "museum", "gallery", "viewpoint"} or amenity in {
                    "place_of_worship", "theatre", "arts_centre", "townhall", "library"
                }:
                    salience_rank = 1
                elif tourism == "artwork" or (tags.get("building") and tags.get("name")) or (
                    tags.get("wikidata") and tags.get("name")
                ):
                    salience_rank = 2
                else:
                    salience_rank = 3
                return {
                    **raw,
                    "distance_m": round(distance_m, 1),
                    "selection_bucket": bucket,
                    "salience_rank": salience_rank,
                }

            landmarks = [
                item for item in (
                    normalized(raw, "landmark")
                    for raw in landmark_response.json().get("elements", [])[:240]
                )
                if item is not None and float(item.get("distance_m", radius_m + 1)) <= radius_m
            ]
            nearby = [
                item for item in (
                    normalized(raw, "nearby")
                    for raw in nearby_response.json().get("elements", [])[:180]
                )
                if item is not None and float(item.get("distance_m", close_radius_m + 1)) <= close_radius_m
            ]

            def item_key(item: dict[str, Any]) -> tuple[str, str]:
                return (str(item.get("type") or item.get("osm_type") or ""), str(item.get("id") or item.get("osm_id") or ""))

            selected: list[dict[str, Any]] = []
            seen_ids: set[tuple[str, str]] = set()

            def add(items: list[dict[str, Any]], limit: int) -> None:
                added = 0
                for item in items:
                    key = item_key(item)
                    if not all(key) or key in seen_ids:
                        continue
                    seen_ids.add(key)
                    selected.append(item)
                    added += 1
                    if added >= limit:
                        return

            # Preserve every distance zone independently. Stress tests in dense
            # Kaliningrad blocks show relevant landmarks can rank hundreds of
            # positions below generic POIs by pure distance.
            for low, high in ((0.0, 200.0), (200.0, 400.0), (400.0, 600.1)):
                band = [
                    item for item in landmarks
                    if low <= float(item.get("distance_m", radius_m + 1)) < high
                ]
                band.sort(key=lambda item: (int(item.get("salience_rank", 3)), float(item.get("distance_m", radius_m + 1))))
                add(band, 16)

            nearby.sort(key=lambda item: float(item.get("distance_m", close_radius_m + 1)))
            add(nearby, 20)

            # Reverse geocoding returns an object's representative position,
            # not necessarily the camera location and never a confirmed identity.
            try:
                rlat, rlon = float(reverse["lat"]), float(reverse["lon"])
                if not math.isfinite(rlat) or not math.isfinite(rlon):
                    raise ValueError
                a = math.sin(math.radians(rlat - lat) / 2) ** 2 + math.cos(math.radians(lat)) * math.cos(math.radians(rlat)) * math.sin(math.radians(rlon - lon) / 2) ** 2
                reverse_distance = round(6_371_000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, a)))), 1)
            except (KeyError, TypeError, ValueError):
                reverse_distance = None
            result = {
                "reverse": {**reverse, "distance_m": reverse_distance, "selection_bucket": "reverse", "salience_rank": -1},
                "nearby": selected[:68],
                "radius_m": radius_m,
                "close_radius_m": close_radius_m,
                "candidate_pool_counts": {
                    "landmark": len(landmarks),
                    "nearby": len(nearby),
                },
            }
            self.store.cache_put(key, result, 7 * 24 * 3600)
            return result
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError) as exc:
            raise RetryableProviderError(f"OSM lookup failed: {exc}") from exc
        finally:
            if own:
                await client.aclose()

    @staticmethod
    def sources(data: dict[str, Any]) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        for item in [data.get("reverse", {}), *data.get("nearby", [])]:
            osm_type = item.get("osm_type") or item.get("type")
            osm_id = item.get("osm_id") or item.get("id")
            if osm_type in {"node", "way", "relation"} and osm_id is not None:
                tags = item.get("tags") or {}
                title = tags.get("name") or item.get("display_name") or f"OpenStreetMap {osm_type} {osm_id}"
                output.append({"type": "osm", "title": str(title), "url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}"})
        unique = {s["url"]: s for s in output}
        return list(unique.values())


class WikipediaClient:
    def __init__(self, store: Store, http: httpx.AsyncClient | None = None):
        self.store = store
        self.http = http
        self.endpoint = "https://ru.wikipedia.org/w/api.php"

    async def nearby(self, lat: float, lon: float) -> list[dict[str, Any]]:
        key = _stable_cache_key("wikipedia-pageimages-position-v4", [round(lat, 6), round(lon, 6)])
        cached = self.store.cache_get(key)
        if cached is not None:
            return cached
        own = self.http is None
        client = self.http or httpx.AsyncClient(timeout=20, headers={"User-Agent": WIKIPEDIA_USER_AGENT})
        try:
            geo = await client.get(self.endpoint, params={
                "action": "query", "list": "geosearch", "gscoord": f"{lat}|{lon}", "gsradius": 750,
                "gslimit": 20, "format": "json", "formatversion": 2,
            }, headers={"User-Agent": WIKIPEDIA_USER_AGENT})
            geo.raise_for_status()
            hits = geo.json().get("query", {}).get("geosearch", [])[:20]
            hit_by_page = {
                str(hit.get("pageid")): hit
                for hit in hits
                if isinstance(hit, dict) and hit.get("pageid") is not None
            }
            if not hits:
                self.store.cache_put(key, [], 24 * 3600)
                return []
            ids = "|".join(str(hit["pageid"]) for hit in hits)
            extracts = await client.get(self.endpoint, params={
                "action": "query", "pageids": ids, "prop": "extracts|info|pageimages", "exintro": 1,
                "explaintext": 1, "inprop": "url", "piprop": "original|thumbnail", "pithumbsize": 1200,
                "format": "json", "formatversion": 2,
            }, headers={"User-Agent": WIKIPEDIA_USER_AGENT})
            extracts.raise_for_status()
            pages = extracts.json().get("query", {}).get("pages", [])
            result = [{
                "pageid": page.get("pageid"), "title": page.get("title", ""),
                "extract": page.get("extract", "")[:6000],
                "url": page.get("fullurl") or f"https://ru.wikipedia.org/wiki/{quote(page.get('title', '').replace(' ', '_'))}",
                "image_url": (page.get("original") or {}).get("source"),
                "thumbnail_url": (page.get("thumbnail") or {}).get("source"),
                "lat": hit_by_page.get(str(page.get("pageid")), {}).get("lat"),
                "lon": hit_by_page.get(str(page.get("pageid")), {}).get("lon"),
                "distance_m": (
                    float(hit_by_page.get(str(page.get("pageid")), {}).get("dist"))
                    if hit_by_page.get(str(page.get("pageid")), {}).get("dist") is not None
                    else None
                ),
            } for page in pages if page.get("title")]
            self.store.cache_put(key, result, 7 * 24 * 3600)
            return result
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError) as exc:
            raise RetryableProviderError(f"Wikipedia lookup failed: {exc}") from exc
        finally:
            if own:
                await client.aclose()


@dataclass
class GroundedResearch:
    payload: dict[str, Any]
    grounding_sources: list[dict[str, str]]


class _DuckDuckGoResultParser(HTMLParser):
    """Bounded parser for DuckDuckGo's simple HTML result surface."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.capture: str | None = None
        self.buffer: list[str] = []

    @staticmethod
    def _target_url(raw: str) -> str | None:
        href = str(raw or "").strip()
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            target = parse_qs(parsed.query).get("uddg", [None])[0]
            href = str(target or "").strip()
            parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return href

    def _flush(self) -> None:
        if not self.current:
            return
        url = self._target_url(self.current.get("href", ""))
        title = " ".join(self.current.get("title", "").split())
        snippet = " ".join(self.current.get("snippet", "").split())
        if url and title and len(self.results) < 12:
            self.results.append({"url": url, "title": title[:300], "snippet": snippet[:700]})
        self.current = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        classes = set(str(values.get("class") or "").split())
        if "result__a" in classes:
            self._flush()
            self.current = {"href": str(values.get("href") or ""), "title": "", "snippet": ""}
            self.capture = "title"
            self.buffer = []
        elif "result__snippet" in classes and self.current is not None:
            self.capture = "snippet"
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.capture is not None:
            self.buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self.capture is None or self.current is None:
            return
        self.current[self.capture] = " ".join("".join(self.buffer).split())
        finished = self.capture == "snippet"
        self.capture = None
        self.buffer = []
        if finished:
            self._flush()

    def finish(self) -> list[dict[str, str]]:
        self._flush()
        return list(self.results)


class _ReadablePageParser(HTMLParser):
    """Extract bounded human-readable page text without script/style payloads."""

    BLOCK_TAGS = frozenset({
        "p", "div", "article", "main", "section", "li", "br",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "caption", "tr", "th", "td", "figcaption", "dt", "dd",
    })
    SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "canvas", "template"})

    def __init__(self, limit: int = 12_000):
        super().__init__(convert_charrefs=True)
        self.limit = max(1_000, int(limit))
        self.parts: list[str] = []
        self.size = 0
        self.skip_depth = 0
        self.truncated = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if not self.skip_depth and tag in self.BLOCK_TAGS and self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if not self.skip_depth and tag in self.BLOCK_TAGS and self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = " ".join(str(data or "").split())
        if not text:
            return
        if self.size >= self.limit:
            self.truncated = True
            return
        remaining = self.limit - self.size
        if len(text) > remaining:
            text = text[:remaining]
            self.truncated = True
        self.parts.append(text)
        self.parts.append(" ")
        self.size += len(text) + 1

    def finish(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()[: self.limit]


class GeminiClient:
    # Cache evaluation is an optional latency optimization. It must never spend
    # the full multi-key research retry budget before ordinary discovery.
    SEMANTIC_CACHE_PREFLIGHT_SECONDS = 8.0
    # Public discovery still gets one bounded semantic-completion opportunity;
    # after that the already-running Live model owns extraction via
    # save_research_facts.
    SEMANTIC_DISCOVERY_COMPLETION_SECONDS = 12.0
    # Native Google Search grounding is useful but must not serialize the full
    # per-key timeout budget inside a Live tool call. After this total phase
    # deadline, fall back to public discovery instead of keeping the UI stuck.
    NATIVE_WEB_SEARCH_PHASE_SECONDS = 10.0

    FACT_IDENTITY_RECONCILIATION_SCHEMA = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "incoming_index": {"type": "integer"},
                        "equivalent": {"type": "boolean"},
                        "existing_fact_id": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "incoming_index", "equivalent", "existing_fact_id", "rationale",
                    ],
                },
            },
        },
        "required": ["matches"],
    }

    FACT_CONFLICT_SCHEMA = {
        "type": "object",
        "properties": {
            "conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "left_fact_id": {"type": "string"},
                        "right_fact_id": {"type": "string"},
                        "relation": {"type": "string"},
                        "suggested_resolution": {"type": "string"},
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "left_fact_id", "right_fact_id", "relation",
                        "suggested_resolution", "confidence", "rationale",
                    ],
                },
            },
        },
        "required": ["conflicts"],
    }

    WEB_SEARCH_SCHEMA = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "official_source_urls": {"type": "array", "items": {"type": "string"}},
            "facts": {"type": "array", "items": {"type": "object", "properties": {
                "claim_key": {"type": "string"},
                "existing_fact_id": {"type": "string"},
                "text": {"type": "string"},
                "confidence": {"type": "number"},
                "source_urls": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "evidence_spans": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_url": {"type": "string"},
                            "chunk_id": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                        "required": ["source_url", "chunk_id", "quote"],
                    },
                },
            }, "required": ["claim_key", "text", "confidence", "source_urls"]}},
        },
        "required": ["summary", "official_source_urls", "facts"],
    }

    COVERAGE_ITEM_SCHEMA = {
        "type": "object",
        "properties": {
            "requirement": {"type": "string"},
            "satisfied": {"type": "boolean"},
            "fact_indices": {"type": "array", "items": {"type": "integer"}},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
        },
        "required": [
            "requirement", "satisfied", "fact_indices", "evidence_refs", "rationale",
        ],
    }

    NATIVE_WEB_SEARCH_SCHEMA = {
        "type": "object",
        "properties": {
            **WEB_SEARCH_SCHEMA["properties"],
            "coverage_satisfied": {"type": "boolean"},
            "missing_aspects": {"type": "array", "items": {"type": "string"}},
            "coverage_items": {
                "type": "array",
                "items": COVERAGE_ITEM_SCHEMA,
            },
            "continuation_needed": {"type": "boolean"},
            "continuation_reason": {"type": "string"},
        },
        "required": [
            "summary", "official_source_urls", "facts",
            "coverage_satisfied", "missing_aspects",
            "continuation_needed", "continuation_reason",
        ],
    }

    NATIVE_CONTINUATION_SCHEMA = {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    **WEB_SEARCH_SCHEMA["properties"]["facts"]["items"],
                    "required": [
                        "claim_key", "text", "confidence", "source_urls", "evidence_refs",
                    ],
                },
            },
            "continuation_needed": {"type": "boolean"},
            "continuation_reason": {"type": "string"},
        },
        "required": ["facts", "continuation_needed", "continuation_reason"],
    }

    CHUNK_EXTRACTION_SCHEMA = {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    **WEB_SEARCH_SCHEMA["properties"]["facts"]["items"],
                    "required": [
                        "claim_key", "text", "confidence", "source_urls", "evidence_spans",
                    ],
                },
            },
            "needs_context": {"type": "boolean"},
            "context_reason": {"type": "string"},
            "continuation_needed": {"type": "boolean"},
            "continuation_reason": {"type": "string"},
        },
        "required": [
            "facts", "needs_context", "context_reason",
            "continuation_needed", "continuation_reason",
        ],
    }

    COVERAGE_REVIEW_SCHEMA = {
        "type": "object",
        "properties": {
            "coverage_satisfied": {"type": "boolean"},
            "summary": {"type": "string"},
            "missing_aspects": {"type": "array", "items": {"type": "string"}},
            "coverage_items": {
                "type": "array",
                "items": COVERAGE_ITEM_SCHEMA,
            },
            "read_source_urls": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "coverage_satisfied", "summary", "missing_aspects",
            "coverage_items", "read_source_urls",
        ],
    }

    DISCOVERY_COVERAGE_SCHEMA = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "official_source_urls": {"type": "array", "items": {"type": "string"}},
            "facts": {
                "type": "array",
                "items": {
                    **WEB_SEARCH_SCHEMA["properties"]["facts"]["items"],
                    "required": [
                        "claim_key", "text", "confidence", "source_urls", "evidence_refs",
                    ],
                },
            },
            "coverage_satisfied": {"type": "boolean"},
            "missing_aspects": {"type": "array", "items": {"type": "string"}},
            "coverage_items": {
                "type": "array",
                "items": COVERAGE_ITEM_SCHEMA,
            },
            "read_source_urls": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "summary", "official_source_urls", "facts",
            "coverage_satisfied", "read_source_urls",
        ],
    }

    EVIDENCE_BINDING_SCHEMA = {
        "type": "object",
        "properties": {
            "bindings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fact_index": {"type": "integer"},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["fact_index", "evidence_refs"],
                },
            },
        },
        "required": ["bindings"],
    }

    COMPOSE_SCHEMA = {
        "type": "object",
        "properties": {
            "concept": {"type": "string"},
            "draft_text": {"type": "string"},
        },
        "required": ["concept", "draft_text"],
    }

    FACT_SCHEMA = {
        "type": "object",
        "properties": {
            "place_name": {"type": "string"},
            "summary": {"type": "string"},
            "draft_text": {"type": "string"},
            "facts": {"type": "array", "items": {"type": "object", "properties": {
                "text": {"type": "string"}, "confidence": {"type": "number"},
                "source_urls": {"type": "array", "items": {"type": "string"}},
            }, "required": ["text", "confidence", "source_urls"]}},
        },
        "required": ["place_name", "summary", "draft_text", "facts"],
    }

    def __init__(self, settings: Settings, store: Store | None = None):
        self.settings = settings
        self.search_http: httpx.AsyncClient | None = None
        shared_store = store or Store(settings.data_dir / "street-story.sqlite3")
        self.store = shared_store
        policy = GeminiPolicy(
            call_timeout=settings.gemini_call_timeout_seconds,
            attempt_timeout=settings.gemini_attempt_timeout_seconds,
            transcription_rpm=settings.gemini_transcription_rpm,
            grounded_research_rpm=settings.gemini_grounded_research_rpm,
            web_search_rpm=settings.gemini_grounded_research_rpm,
        )
        from .quota import SharedQuotaGate
        transcription_models = tuple(dict.fromkeys((
            settings.gemini_transcription_model,
            settings.gemini_transcription_fallback_model,
        )))
        self.transcription_routes = []
        for model in transcription_models:
            model_pool = GeminiKeyPool(shared_store, settings.gemini_keys, model, policy=policy)
            model_quota = SharedQuotaGate(settings, model_pool)
            self.transcription_routes.append((model, model_pool, model_quota, GeminiExecutor(model_pool)))
        self.transcription_pool = self.transcription_routes[0][1]
        self.transcription_quota = self.transcription_routes[0][2]
        self.transcription_executor = self.transcription_routes[0][3]
        research_models = tuple(dict.fromkeys((
            settings.gemini_model,
            settings.gemini_fallback_model,
        )))
        self.research_routes = []
        for model in research_models:
            model_pool = GeminiKeyPool(shared_store, settings.gemini_keys, model, policy=policy)
            model_quota = SharedQuotaGate(settings, model_pool)
            self.research_routes.append((model, model_pool, model_quota, GeminiExecutor(model_pool)))
        web_search_models = tuple(dict.fromkeys((
            settings.gemini_web_search_model,
            settings.gemini_web_search_tertiary_model,
        )))
        self.web_search_routes = []
        for model in web_search_models:
            model_pool = GeminiKeyPool(shared_store, settings.gemini_keys, model, policy=policy)
            model_quota = SharedQuotaGate(settings, model_pool)
            self.web_search_routes.append((model, model_pool, model_quota, GeminiExecutor(model_pool)))
        self.pool = self.research_routes[0][1]
        self.quota = self.research_routes[0][2]
        self.executor = self.research_routes[0][3]

    async def _generate(
        self,
        key: str,
        timeout: float,
        contents,
        config=None,
        *,
        operation: str = "grounded_research",
        model: str | None = None,
        quota=None,
    ):
        from google.genai import types
        config = config or types.GenerateContentConfig()
        config.max_output_tokens = 8192
        # Same reservation contract as the existing GoogleAI gateway: estimated
        # input + bounded output + safety margin, reconciled with actual usage.
        # This is not a provider token-count/remaining-quota guarantee.
        size = 1000 + 8192
        for part in contents:
            if isinstance(part, str):
                size += len(part.encode('utf-8'))
            else:
                inline = getattr(part, 'inline_data', None)
                data = getattr(inline, 'data', b'') or b''
                mime = getattr(inline, 'mime_type', '') or ''
                size += 8192 if mime.startswith('image/') else max(8192, len(data)//4)
        if operation == "transcription":
            quota = quota or self.transcription_quota
            model = model or self.settings.gemini_transcription_model
        else:
            quota = quota or self.quota
            model = model or self.settings.gemini_model
        return await quota.run(
            key,
            timeout,
            size,
            lambda: self._provider_request(key, timeout, contents, config, model=model),
        )

    async def _provider_request(self, key: str, timeout: float, contents, config=None, *, model: str | None = None):
        # Async transport is cancellable: no orphan to_thread SDK calls after failover.
        # Disable the SDK's hidden same-key retries; the pool owns this budget.
        from google import genai
        from google.genai import types
        options = types.HttpOptions(timeout=max(1, int(timeout * 1000)), retry_options=types.HttpRetryOptions(attempts=1))
        with genai.Client(api_key=key, http_options=options) as root:
            async with root.aio as client:
                return await client.models.generate_content(model=model or self.settings.gemini_model, contents=contents, config=config)

    async def transcribe(self, path: Path, mime_type: str) -> str:
        from google.genai import types
        data = path.read_bytes()
        prompt = (
            "Точно транскрибируй русскую голосовую заметку Street Story. Не выдумывай факты, "
            "не резюмируй, сохрани смысл, имена собственные и вопросы пользователя. Верни только транскрипт."
        )

        contents = [types.Part.from_bytes(data=data, mime_type=mime_type), prompt]
        retry_at: list[float] = []
        for model, _pool, quota, executor in self.transcription_routes:
            async def call(key, timeout, *, _model=model, _quota=quota):
                response = await self._generate(
                    key,
                    timeout,
                    contents,
                    operation="transcription",
                    model=_model,
                    quota=_quota,
                )
                text = response.text
                if text is not None and not isinstance(text, str):
                    raise MalformedProviderResponse("gemini:malformed_transcription")
                if text is None:
                    raise MalformedProviderResponse("gemini:missing_transcription")
                return text.strip()

            try:
                return await executor.execute("transcription", call)
            except GeminiUnavailable as exc:
                if exc.retry_at is not None:
                    retry_at.append(exc.retry_at)
                continue
            except PermanentProviderError as exc:
                if str(exc) == "gemini:unsupported_model":
                    continue
                raise
        if retry_at:
            raise GeminiUnavailable(min(retry_at), "all_transcription_models_unavailable")
        raise PermanentProviderError("gemini:unsupported_model")

    @staticmethod
    def _cached_evidence_sources(topic_context: dict[str, Any], limit: int = 12) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for item in topic_context.get("previously_processed_sources") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").rstrip("/")
            if not url.startswith("https://"):
                continue
            supports = []
            for support in item.get("supports") or []:
                if not isinstance(support, dict):
                    continue
                text = str(support.get("text") or "").strip()
                source_url = str(support.get("source_url") or url).rstrip("/")
                if not text or source_url != url:
                    continue
                supports.append({
                    "kind": str(support.get("kind") or "cached_evidence")[:80],
                    "source_url": url,
                    "text": text[:9000] if support.get("kind") == "page_excerpt" else text[:600],
                })
                if len(supports) >= 5:
                    break
            if not supports:
                continue
            sources.append({
                "type": str(item.get("type") or "poi_memory"),
                "title": str(item.get("title") or url)[:300],
                "url": url,
                "supports": supports,
                "cached": True,
            })
            if len(sources) >= max(1, min(int(limit), 24)):
                break
        return sources

    @staticmethod
    def _support_evidence_ref(source_url: str, support: dict[str, Any]) -> str:
        existing = str(support.get("evidence_ref") or "").strip()
        if existing:
            return existing
        payload = "\n".join([
            str(source_url or "").rstrip("/"),
            str(support.get("kind") or "support"),
            str(support.get("source_version_id") or ""),
            str(support.get("chunk_id") or ""),
            str(support.get("text") or "").strip(),
        ])
        return "evref_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def _with_support_evidence_refs(cls, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for raw in sources:
            if not isinstance(raw, dict):
                continue
            source = dict(raw)
            url = str(source.get("url") or "").rstrip("/")
            supports: list[dict[str, Any]] = []
            for raw_support in source.get("supports") or []:
                if not isinstance(raw_support, dict):
                    continue
                text = str(raw_support.get("text") or "").strip()
                if not text:
                    continue
                support = dict(raw_support)
                support["source_url"] = str(support.get("source_url") or url).rstrip("/")
                support["text"] = text
                support["evidence_ref"] = cls._support_evidence_ref(url, support)
                supports.append(support)
            if supports:
                source["supports"] = supports
            else:
                source.pop("supports", None)
            result.append(source)
        return result

    async def _bind_facts_to_evidence(
        self,
        key: str,
        timeout: float,
        candidate_facts: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        *,
        model: str | None,
        quota: str | None,
    ) -> dict[int, list[str]]:
        from google.genai import types

        evidence: list[dict[str, str]] = []
        valid_refs: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").rstrip("/")
            for support in source.get("supports") or []:
                if not isinstance(support, dict):
                    continue
                evidence_ref = str(support.get("evidence_ref") or "").strip()
                text = str(support.get("text") or "").strip()
                if not evidence_ref or not text or evidence_ref in valid_refs:
                    continue
                valid_refs.add(evidence_ref)
                evidence.append({
                    "evidence_ref": evidence_ref,
                    "source_url": url,
                    "kind": str(support.get("kind") or "support")[:80],
                    "text": text[:1200],
                })
        if not candidate_facts or not evidence:
            return {}

        prompt = (
            "Ты внутренний LLM-арбитр evidence Street Story. Для каждого candidate fact выбери только те "
            "evidence_ref, passage которых прямо поддерживает именно этот тезис. URL, домен, количество источников "
            "или общая тематическая близость сами по себе недостаточны. Не используй внешние знания. "
            "Если ни один passage не подтверждает fact, верни для него пустой evidence_refs. "
            "Один passage можно связать с несколькими атомарными facts, если он действительно подтверждает каждый. "
            "Верни binding для каждого fact_index.\n\nCandidate facts: "
            + json.dumps(
                [
                    {
                        "fact_index": index,
                        "text": str(fact.get("text") or ""),
                        "claim_key": str(fact.get("claim_key") or ""),
                        "source_urls_hint": list(fact.get("source_urls") or []),
                    }
                    for index, fact in enumerate(candidate_facts)
                ],
                ensure_ascii=False,
            )
            + "\nEvidence passages: "
            + json.dumps(evidence, ensure_ascii=False)
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=self.EVIDENCE_BINDING_SCHEMA,
        )
        response = await self._generate(
            key,
            timeout,
            [prompt],
            config,
            operation="grounded_research",
            model=model,
            quota=quota,
        )
        try:
            payload = json.loads(response.text or "{}")
            bindings = payload.get("bindings")
            if not isinstance(bindings, list):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            raise MalformedProviderResponse("gemini:malformed_evidence_binding") from None

        result: dict[int, list[str]] = {}
        for item in bindings:
            if not isinstance(item, dict):
                continue
            try:
                fact_index = int(item.get("fact_index"))
            except (TypeError, ValueError):
                continue
            if not (0 <= fact_index < len(candidate_facts)):
                continue
            refs: list[str] = []
            for raw_ref in item.get("evidence_refs") or []:
                evidence_ref = str(raw_ref or "").strip()
                if evidence_ref in valid_refs and evidence_ref not in refs:
                    refs.append(evidence_ref)
            result[fact_index] = refs
        return result

    @staticmethod
    def _merge_evidence_sources(
        primary: list[dict[str, Any]],
        cached: list[dict[str, Any]],
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for source in [*primary, *cached]:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").rstrip("/")
            if not url.startswith("https://"):
                continue
            current = merged.get(url)
            if current is None:
                current = {**source, "url": url}
                if not current.get("supports"):
                    current.pop("supports", None)
                merged[url] = current
                order.append(url)
            elif source.get("title") and not current.get("title"):
                current["title"] = source.get("title")
            if source.get("cached"):
                current["cached"] = True
            support_map = {
                (
                    str(item.get("kind") or ""),
                    str(item.get("source_url") or "").rstrip("/"),
                    str(item.get("text") or ""),
                ): item
                for item in current.get("supports") or []
                if isinstance(item, dict)
            }
            for support in source.get("supports") or []:
                if not isinstance(support, dict):
                    continue
                key = (
                    str(support.get("kind") or ""),
                    str(support.get("source_url") or "").rstrip("/"),
                    str(support.get("text") or ""),
                )
                if key[2]:
                    support_map[key] = support
            if support_map:
                current["supports"] = list(support_map.values())
            else:
                current.pop("supports", None)
        values = [merged[url] for url in order]
        values = GeminiClient._with_support_evidence_refs(values)
        if limit is None:
            return values
        return values[: max(1, int(limit))]

    async def _public_web_search(
        self,
        query: str,
        excluded_urls: set[str] | None = None,
    ) -> GroundedResearch:
        """Emergency independent web discovery after Google-grounding exhaustion.

        The result snippets are discovery evidence, not model-generated facts. They
        are deliberately low-confidence and keep their original source URLs.
        """
        own = self.search_http is None
        client = self.search_http or httpx.AsyncClient(
            timeout=8,
            follow_redirects=False,
            headers={"User-Agent": "StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story)"},
        )
        try:
            response = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query[:1000]},
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            response.raise_for_status()
            text = response.text
            if len(text.encode("utf-8")) > 1_500_000:
                raise RetryableProviderError("public_web_search_response_too_large")
            parser = _DuckDuckGoResultParser()
            parser.feed(text)
            results = parser.finish()
            if not results:
                raise RetryableProviderError("public_web_search_empty")

            # Public snippets are discovery material, not verified facts.
            # Keep only HTTPS evidence because downstream fact persistence deliberately
            # rejects non-HTTPS source references. Never show Mira evidence she cannot
            # subsequently bind to a durable fact.
            results = [
                item
                for item in results
                if str(item.get("url") or "").startswith("https://")
            ]
            if not results:
                raise RetryableProviderError("public_web_search_no_https_results")

            # Previously processed URLs are intentionally not filtered here:
            # a new coverage goal may require a different fact or page section.
            # Cached supports are supplied separately to the semantic model.
            _processed = {
                str(url).rstrip("/")
                for url in (excluded_urls or set())
                if str(url).startswith("https://")
            }

            facts: list[dict[str, Any]] = []
            sources = [
                {
                    "type": "web_search",
                    "title": item["title"],
                    "url": item["url"],
                    "supports": (
                        [
                            {
                                "kind": "search_snippet",
                                "source_url": item["url"],
                                "text": item["snippet"][:600],
                            }
                        ]
                        if item["snippet"]
                        else []
                    ),
                }
                for item in results
            ]
            return GroundedResearch(
                payload={
                    "summary": (
                        f"Google Search grounding сейчас недоступен. Получено {len(sources)} "
                        "discovery-источников; evidence находится в sources[].supports и не является "
                        "автоматически доказанными фактами."
                    ),
                    "official_source_urls": [],
                    "facts": facts,
                    "search_provider": "duckduckgo_html_fallback",
                },
                grounding_sources=sources,
            )
        except RetryableProviderError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError) as exc:
            raise RetryableProviderError(f"public_web_search_unavailable:{type(exc).__name__}") from exc
        finally:
            if own:
                await client.aclose()

    @staticmethod
    def _safe_page_url(url: str) -> bool:
        try:
            parsed = urlparse(str(url or ""))
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                return False
            host = parsed.hostname.strip(".").casefold()
            if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
                return False
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                return True
            return bool(address.is_global)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _chunk_page_text(text: str, target: int = 2200, overlap: int = 180) -> list[str]:
        from .research_runs import plan_text_chunks
        return [
            str(item["text"])
            for item in plan_text_chunks(
                str(text or ""),
                target_chars=target,
                overlap_chars=overlap,
            )
        ]

    async def _fetch_page_documents(
        self,
        urls: list[str],
        topic_context: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        selected: list[str] = []
        for raw in urls:
            url = str(raw or "").rstrip("/")
            if url in selected or not self._safe_page_url(url):
                continue
            selected.append(url)
            if len(selected) >= 4:
                break
        if not selected:
            return {}

        run_id = str(topic_context.get("research_run_id") or "").strip()
        source_titles = {
            str(item.get("url") or "").rstrip("/"): str(item.get("title") or "")
            for item in (topic_context.get("research_sources") or [])
            if isinstance(item, dict)
        }
        own = self.search_http is None
        client = self.search_http or httpx.AsyncClient(
            timeout=8,
            follow_redirects=False,
            headers={"User-Agent": "StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story)"},
        )
        documents: dict[str, dict[str, Any]] = {}
        try:
            for requested_url in selected:
                if run_id:
                    from .research_runs import saved_run_document
                    with self.store.connection() as db:
                        saved = saved_run_document(db, run_id, requested_url)
                    if saved is not None:
                        documents[requested_url] = saved
                        continue
                now = self.store.now()
                if run_id:
                    with self.store.tx() as db:
                        register_discovered_source(
                            db,
                            run_id=run_id,
                            url=requested_url,
                            title=source_titles.get(requested_url) or requested_url,
                            status="fetching",
                            now=now,
                        )
                current_url = requested_url
                redirect_chain: list[str] = []
                response = None
                error_code = None
                try:
                    for _ in range(4):
                        response = await client.get(
                            current_url,
                            headers={"Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"},
                        )
                        if 300 <= response.status_code < 400:
                            location = str(response.headers.get("location") or "").strip()
                            if not location:
                                error_code = "redirect_missing_location"
                                response = None
                                break
                            next_url = urljoin(current_url, location).rstrip("/")
                            if not self._safe_page_url(next_url):
                                error_code = "redirect_target_rejected"
                                response = None
                                break
                            redirect_chain.append(next_url)
                            current_url = next_url
                            continue
                        break
                    else:
                        error_code = "redirect_limit_exceeded"
                        response = None

                    if response is None:
                        raise ValueError(error_code or "page_fetch_failed")
                    if response.status_code != 200:
                        error_code = f"http_{response.status_code}"
                        raise ValueError(error_code)
                    content_type = str(response.headers.get("content-type") or "").casefold()
                    if content_type and not any(
                        kind in content_type
                        for kind in ("text/html", "application/xhtml+xml", "text/plain")
                    ):
                        error_code = "unsupported_content_type"
                        raise ValueError(error_code)

                    raw_bytes = response.content
                    if not raw_bytes:
                        error_code = "empty_body"
                        raise ValueError(error_code)
                    body_limited = len(raw_bytes) > 768_000

                    parser = _ReadablePageParser(limit=120_000)
                    parser.feed(response.text)
                    normalized_text = parser.finish()
                    if len(normalized_text) < 80:
                        error_code = "page_text_too_short"
                        raise ValueError(error_code)

                    read_status = (
                        "partial_body_limit"
                        if body_limited
                        else "partial_text_limit"
                        if parser.truncated
                        else "complete"
                    )
                    if run_id:
                        with self.store.tx() as db:
                            persisted = persist_source_version(
                                db,
                                run_id=run_id,
                                requested_url=requested_url,
                                final_url=current_url,
                                title=source_titles.get(requested_url) or source_titles.get(current_url) or current_url,
                                content_type=content_type,
                                http_status=response.status_code,
                                redirect_chain=redirect_chain,
                                normalized_text=normalized_text,
                                read_status=read_status,
                                now=self.store.now(),
                            )
                    else:
                        content_sha = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
                        source_version_id = "srcv_" + hashlib.sha256(
                            f"{current_url}\n{content_sha}\n{content_type}".encode("utf-8")
                        ).hexdigest()[:24]
                        from .research_runs import plan_text_chunks
                        chunks = plan_text_chunks(normalized_text)
                        persisted = {
                            "document_id": "doc_" + hashlib.sha256(current_url.encode("utf-8")).hexdigest()[:24],
                            "source_version_id": source_version_id,
                            "content_sha256": content_sha,
                            "read_status": read_status,
                            "char_count": len(normalized_text),
                            "chunks": [
                                {
                                    **item,
                                    "chunk_id": "chunk_" + hashlib.sha256(
                                        f"{source_version_id}:{item['core_start']}:{item['core_end']}".encode("utf-8")
                                    ).hexdigest()[:24],
                                }
                                for item in chunks
                            ],
                        }

                    documents[requested_url] = {
                        **persisted,
                        "requested_url": requested_url,
                        "final_url": current_url,
                        "title": source_titles.get(requested_url) or source_titles.get(current_url) or current_url,
                        "content_type": content_type,
                        "normalized_text": normalized_text,
                        "redirect_chain": redirect_chain,
                    }
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError, ValueError, UnicodeError) as exc:
                    error_code = error_code or type(exc).__name__
                    if run_id:
                        with self.store.tx() as db:
                            register_discovered_source(
                                db,
                                run_id=run_id,
                                url=requested_url,
                                title=source_titles.get(requested_url) or requested_url,
                                status="failed",
                                error_code=error_code,
                                now=self.store.now(),
                            )
                    continue
        finally:
            if own:
                await client.aclose()
        return documents

    @staticmethod
    def _validate_coverage_contract(
        payload: dict[str, Any],
        facts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        raw_items = payload.get("coverage_items")
        items: list[dict[str, Any]] = []
        missing: list[str] = []

        if isinstance(raw_items, list):
            for raw in raw_items[:40]:
                if not isinstance(raw, dict):
                    continue
                requirement = " ".join(str(raw.get("requirement") or "").split()).strip()[:500]
                if not requirement:
                    continue

                raw_indices = raw.get("fact_indices") if isinstance(raw.get("fact_indices"), list) else []
                indices: list[int] = []
                invalid_index = False
                for value in raw_indices:
                    if not isinstance(value, int) or isinstance(value, bool):
                        invalid_index = True
                        continue
                    if value < 0 or value >= len(facts):
                        invalid_index = True
                        continue
                    if value not in indices:
                        indices.append(value)

                raw_refs = raw.get("evidence_refs") if isinstance(raw.get("evidence_refs"), list) else []
                refs = list(dict.fromkeys(
                    str(value).strip()
                    for value in raw_refs
                    if str(value).strip()
                ))[:40]

                referenced = [facts[index] for index in indices]
                proof_ok = bool(referenced) and not invalid_index
                allowed_refs: set[str] = set()
                for fact in referenced:
                    fact_refs = {
                        str(value).strip()
                        for value in (fact.get("evidence_refs") or [])
                        if str(value).strip()
                    }
                    spans = [
                        span
                        for span in (fact.get("evidence_spans") or [])
                        if isinstance(span, dict)
                        and str(span.get("quote") or span.get("text") or "").strip()
                    ]
                    allowed_refs.update(fact_refs)
                    if fact_refs:
                        if not any(ref in fact_refs for ref in refs):
                            proof_ok = False
                    elif not spans:
                        proof_ok = False

                if any(ref not in allowed_refs for ref in refs):
                    proof_ok = False

                model_satisfied = raw.get("satisfied") is True
                satisfied = bool(model_satisfied and proof_ok)
                item = {
                    "requirement": requirement,
                    "satisfied": satisfied,
                    "fact_indices": indices,
                    "evidence_refs": [ref for ref in refs if ref in allowed_refs],
                    "rationale": str(raw.get("rationale") or "")[:800],
                }
                items.append(item)
                if not satisfied and requirement not in missing:
                    missing.append(requirement)

        for value in payload.get("missing_aspects") or []:
            aspect = " ".join(str(value or "").split()).strip()[:500]
            if aspect and aspect not in missing:
                missing.append(aspect)

        coverage_satisfied = bool(items) and all(item["satisfied"] for item in items)
        return {
            "coverage_satisfied": coverage_satisfied,
            "coverage_items": items,
            "missing_aspects": missing,
            "summary": str(payload.get("summary") or "")[:1200],
        }

    async def _review_coverage_contract(
        self,
        api_key: str,
        timeout: float,
        *,
        coverage_goal: str,
        facts: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        model: str | None,
        quota,
        allow_page_reads: bool,
    ) -> dict[str, Any]:
        from google.genai import types

        review_facts = []
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                continue
            review_facts.append({
                "fact_index": index,
                "claim_key": str(fact.get("claim_key") or "")[:300],
                "text": str(fact.get("text") or "")[:1200],
                "source_urls": [
                    str(url)
                    for url in (fact.get("source_urls") or [])
                    if str(url).startswith("https://")
                ][:12],
                "evidence_refs": [
                    str(ref)
                    for ref in (fact.get("evidence_refs") or [])
                    if str(ref).strip()
                ][:40],
                "evidence_spans": [
                    {
                        "source_url": str(span.get("source_url") or ""),
                        "chunk_id": str(span.get("chunk_id") or ""),
                        "quote": str(span.get("quote") or span.get("text") or "")[:1200],
                    }
                    for span in (fact.get("evidence_spans") or [])
                    if isinstance(span, dict)
                    and str(span.get("quote") or span.get("text") or "").strip()
                ][:12],
            })

        evidence = []
        allowed_urls: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").rstrip("/")
            if not url.startswith("https://"):
                continue
            allowed_urls.add(url)
            passages = [
                {
                    "evidence_ref": str(support.get("evidence_ref") or ""),
                    "text": str(support.get("text") or "")[:1200],
                }
                for support in (source.get("supports") or [])
                if isinstance(support, dict)
                and str(support.get("text") or "").strip()
            ]
            evidence.append({
                "source_url": url,
                "title": str(source.get("title") or url)[:240],
                "passages": passages[:20],
            })

        prompt = (
            "Ты проверяешь полноту уже извлечённых facts относительно coverage goal. "
            "Ты независимый LLM-reviewer Street Story. "
            "Не извлекай новые факты и не используй внешние знания. Работай только с Coverage goal, "
            "уже evidence-backed Facts и Evidence. Сначала декомпозируй Coverage goal на минимальные "
            "независимо проверяемые requirements. Если цель явно просит несколько позиций, ролей, людей, "
            "подписей, дат или иных отдельных элементов, создай отдельный coverage_item для каждого явно "
            "запрошенного элемента; не склеивай их в один пункт. Например, неупорядоченный список людей "
            "НЕ доказывает, кто расположен слева, в центре или справа. "
            "Для каждого requirement укажи fact_indices, которые прямо отвечают именно на него. "
            "Если fact опирается на evidence_refs, перечисли соответствующие refs; URL сам по себе не доказательство. "
            "Fact с exact evidence_spans может подтверждать requirement через fact_index без evidence_ref. "
            "satisfied=true только если указанные facts прямо отвечают requirement и их evidence поддерживает ответ. "
            "Если хотя бы один requirement не закрыт, coverage_satisfied=false. "
            + (
                "Для незакрытых requirements выбери максимум 2 read_source_urls только из Evidence — страницы, "
                "которые разумнее всего дочитать для закрытия пробела. "
                if allow_page_reads
                else
                "read_source_urls оставь пустым: этот review не запускает дополнительное чтение страниц. "
            )
            + "\n\nCoverage goal: " + str(coverage_goal or "")[:1600]
            + "\nFacts: " + json.dumps(review_facts, ensure_ascii=False)
            + "\nEvidence: " + json.dumps(evidence, ensure_ascii=False)
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=self.COVERAGE_REVIEW_SCHEMA,
        )
        response = await self._generate(
            api_key,
            timeout,
            [prompt],
            config,
            operation="grounded_research",
            model=model,
            quota=quota,
        )
        try:
            raw = json.loads(response.text or "{}")
            if not isinstance(raw, dict):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            raise MalformedProviderResponse("gemini:malformed_coverage_review") from None

        validated = self._validate_coverage_contract(raw, facts)
        read_urls: list[str] = []
        if allow_page_reads and not validated["coverage_satisfied"]:
            for value in raw.get("read_source_urls") or []:
                url = str(value or "").rstrip("/")
                if url in allowed_urls and url not in read_urls:
                    read_urls.append(url)
                if len(read_urls) >= 2:
                    break
        validated["read_source_urls"] = read_urls
        return validated

    async def _semantic_complete_discovery(
        self,
        query: str,
        topic_context: dict[str, Any],
        discovery: GroundedResearch,
    ) -> GroundedResearch:
        """Use a configured model to turn search evidence into durable fact semantics.

        The model owns semantic extraction and decides whether snippets answer the
        actual query. If not, it may select at most two already-discovered HTTPS
        sources for bounded page reading. Code only validates URLs, fetches bounded
        text, and re-runs the same model over that evidence.
        """
        from google.genai import types

        valid_sources = [
            source
            for source in discovery.grounding_sources
            if isinstance(source, dict)
            and str(source.get("url") or "").startswith("https://")
        ]
        referenced_sources = self._with_support_evidence_refs(valid_sources)
        active_sources = referenced_sources[:24]
        deferred_sources = referenced_sources[24:]

        allowed_urls: dict[str, str] = {}
        source_by_url: dict[str, dict[str, Any]] = {}
        for source in active_sources:
            url = str(source.get("url") or "").strip()
            canonical_url = url.rstrip("/")
            allowed_urls[canonical_url] = url
            source_by_url[canonical_url] = dict(source)

        if not allowed_urls:
            return discovery

        run_id = str(topic_context.get("research_run_id") or "").strip()
        if run_id:
            with self.store.tx() as db:
                for source in active_sources:
                    url = str(source.get("url") or "").rstrip("/")
                    if db.execute(
                        "SELECT 1 FROM research_run_sources WHERE run_id=? AND url=? "
                        "AND source_version_id IS NOT NULL", (run_id, url),
                    ).fetchone():
                        continue
                    register_discovered_source(
                        db,
                        run_id=run_id,
                        url=url,
                        title=str(source.get("title") or url),
                        status="snippet_only",
                        now=self.store.now(),
                    )
                for source in deferred_sources:
                    url = str(source.get("url") or "").rstrip("/")
                    register_discovered_source(
                        db,
                        run_id=run_id,
                        url=url,
                        title=str(source.get("title") or url),
                        status="deferred",
                        error_code="source_batch_limit",
                        now=self.store.now(),
                    )

        known_facts = []
        for item in topic_context.get("known_facts") or []:
            if not isinstance(item, dict):
                continue
            fact_id = str(item.get("fact_id") or "").strip()
            fact_text = str(item.get("text") or "").strip()
            if fact_id and fact_text:
                known_facts.append({"fact_id": fact_id, "text": fact_text[:360]})

        prior_poi = []
        for item in topic_context.get("previously_considered_poi_facts") or []:
            if not isinstance(item, dict):
                continue
            fact_id = str(item.get("fact_id") or item.get("claim_id") or "").strip()
            fact_text = str(item.get("text") or item.get("claim_text") or "").strip()
            if fact_text:
                prior_poi.append({"fact_id": fact_id or None, "text": fact_text[:360]})

        processed_sources = [
            str(item.get("url") or "").rstrip("/")
            for item in (topic_context.get("previously_processed_sources") or [])[:80]
            if isinstance(item, dict) and str(item.get("url") or "").startswith("https://")
        ]
        support_by_ref: dict[str, dict[str, Any]] = {}
        for canonical_url, source in source_by_url.items():
            exact_url = allowed_urls[canonical_url]
            for support in source.get("supports") or []:
                if not isinstance(support, dict):
                    continue
                evidence_ref = str(support.get("evidence_ref") or "").strip()
                support_text = str(support.get("text") or "").strip()
                if evidence_ref and support_text:
                    support_by_ref[evidence_ref] = {
                        "source_url": exact_url,
                        "text": support_text,
                        "support": support,
                    }

        def evidence_rows(with_pages: dict[str, str] | None = None) -> list[dict[str, Any]]:
            pages = with_pages or {}
            rows: list[dict[str, Any]] = []
            for canonical_url, exact_url in allowed_urls.items():
                source = source_by_url[canonical_url]
                snippets: list[dict[str, str]] = []
                seen_refs: set[str] = set()
                for support in source.get("supports") or []:
                    if not isinstance(support, dict):
                        continue
                    support_text = str(support.get("text") or "").strip()
                    evidence_ref = str(support.get("evidence_ref") or "").strip()
                    if support_text and evidence_ref and evidence_ref not in seen_refs:
                        snippets.append({
                            "evidence_ref": evidence_ref,
                            "text": support_text[:600],
                        })
                        seen_refs.add(evidence_ref)
                row = {
                    "source_url": exact_url,
                    "title": str(source.get("title") or exact_url)[:220],
                    "snippets": snippets,
                }
                if canonical_url in pages:
                    row["page_excerpt"] = pages[canonical_url][:9_000]
                rows.append(row)
            return rows

        def normalize_payload(
            payload: dict[str, Any],
            *,
            evidence_chunks: dict[str, dict[str, Any]] | None = None,
        ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
            raw_facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
            audit = {
                "raw_fact_count": len(raw_facts),
                "accepted_fact_count": 0,
                "claim_key_fallback_count": 0,
                "confidence_defaulted_count": 0,
                "rejected": {},
            }

            def reject(reason: str) -> None:
                rejected = audit["rejected"]
                rejected[reason] = int(rejected.get(reason, 0)) + 1

            normalized_facts: list[dict[str, Any]] = []
            for item in raw_facts:
                if not isinstance(item, dict):
                    reject("invalid_shape")
                    continue
                fact_text = " ".join(str(item.get("text") or "").split()).strip()
                if not fact_text:
                    reject("empty_text")
                    continue
                if len(fact_text) > 1200:
                    reject("text_over_safety_bound")
                    continue

                claim_key = " ".join(str(item.get("claim_key") or "").split()).strip().casefold()
                if not claim_key or len(claim_key) > 300:
                    claim_key = "exact-text:" + hashlib.sha256(fact_text.casefold().encode("utf-8")).hexdigest()[:24]
                    audit["claim_key_fallback_count"] += 1

                try:
                    confidence = float(item.get("confidence", 0.0))
                    if not math.isfinite(confidence):
                        raise ValueError
                except (TypeError, ValueError):
                    confidence = 0.0
                    audit["confidence_defaulted_count"] += 1

                source_urls: list[str] = []
                evidence_refs: list[str] = []
                if evidence_chunks is None:
                    for raw_ref in item.get("evidence_refs") or []:
                        evidence_ref = str(raw_ref or "").strip()
                        bound = support_by_ref.get(evidence_ref)
                        if bound is None or evidence_ref in evidence_refs:
                            continue
                        evidence_refs.append(evidence_ref)
                        exact_url = str(bound["source_url"])
                        if exact_url not in source_urls:
                            source_urls.append(exact_url)
                    if not evidence_refs:
                        reject("no_bound_evidence")
                        continue
                else:
                    for raw_url in item.get("source_urls") or []:
                        exact = allowed_urls.get(str(raw_url or "").rstrip("/"))
                        if exact and exact not in source_urls:
                            source_urls.append(exact)
                    if not source_urls:
                        reject("no_grounded_source")
                        continue

                evidence_spans: list[dict[str, Any]] = []
                if evidence_chunks is not None:
                    for raw_span in item.get("evidence_spans") or []:
                        if not isinstance(raw_span, dict):
                            continue
                        chunk_id = str(raw_span.get("chunk_id") or "").strip()
                        quote = str(raw_span.get("quote") or "").strip()
                        raw_source_url = str(raw_span.get("source_url") or "").rstrip("/")
                        chunk = evidence_chunks.get(chunk_id)
                        if chunk is None or not quote or len(quote) > 1600:
                            continue
                        exact_url = allowed_urls.get(raw_source_url)
                        if exact_url is None or exact_url.rstrip("/") != str(chunk.get("source_url") or "").rstrip("/"):
                            continue
                        core_text = str(chunk.get("core_text") or "")
                        relative_start = core_text.find(quote)
                        if relative_start < 0:
                            continue
                        base_offset = int(chunk.get("core_start") or 0)
                        evidence_spans.append({
                            "source_url": exact_url,
                            "source_version_id": str(chunk.get("source_version_id") or ""),
                            "chunk_id": chunk_id,
                            "quote": quote,
                            "span_start": base_offset + relative_start,
                            "span_end": base_offset + relative_start + len(quote),
                        })
                    if not evidence_spans:
                        reject("no_verified_span")
                        continue

                fact = {
                    "claim_key": claim_key,
                    "text": fact_text,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "source_urls": source_urls[:12],
                }
                if evidence_refs:
                    fact["evidence_refs"] = evidence_refs
                if evidence_spans:
                    fact["evidence_spans"] = evidence_spans
                existing_fact_id = str(item.get("existing_fact_id") or "").strip()
                if existing_fact_id:
                    fact["existing_fact_id"] = existing_fact_id[:200]
                normalized_facts.append(fact)

            audit["accepted_fact_count"] = len(normalized_facts)
            official_urls: list[str] = []
            for raw_url in payload.get("official_source_urls") or []:
                exact = allowed_urls.get(str(raw_url or "").rstrip("/"))
                if exact and exact not in official_urls:
                    official_urls.append(exact)
            return normalized_facts, official_urls[:12], audit

        coverage_goal = str(topic_context.get("coverage_goal") or query).strip()[:1600]

        def build_prompt(evidence: list[dict[str, Any]], *, page_pass: bool) -> str:
            coverage_rule = (
                "Это второй проход: page_excerpt — bounded текст выбранных страниц. Извлеки ответ на исходный query "
                "только из evidence; не проси читать страницы повторно. "
                if page_pass else
                "Отдельно оцени, отвечает ли snippets прямо на главный смысл query. Если запрос о конкретных фигурах, "
                "именах, надписях, деталях или авторах, простого упоминания объекта недостаточно: coverage_satisfied=true "
                "только когда evidence содержит конкретный ответ. Если ответа не хватает, coverage_satisfied=false и "
                "выбери в read_source_urls максимум 2 URL из evidence, страницы которых вероятнее всего закроют пробел. "
            )
            return (
                "Ты внутренний LLM-экстрактор фактов Street Story. Search discovery и semantic extraction разделены. "
                "Не используй знания вне переданного evidence. " + coverage_rule +
                "Извлеки до 32 содержательных атомарных проверяемых фактов. Один fact.text = один тезис. "
                "Если один абзац содержит несколько независимо проверяемых утверждений, разнеси их на отдельные facts; "
                "не склеивай перечень людей/дат/ролей в один факт, когда каждый элемент имеет самостоятельный смысл. "
                "Фраза о том, что источник не содержит нужной информации, НЕ является фактом об объекте: в таком случае "
                "не создавай meta-факт, а оставь facts пустым или извлеки только реально поддержанные сведения. "
                "Предварительно разложи coverage_goal на минимальные независимо проверяемые coverage_items; "
                "если явно запрошено несколько позиций/ролей/элементов, не объединяй их в один coverage_item. "
                "Для каждого факта дай устойчивый claim_key. Если тезис семантически совпадает с known_facts, обязательно "
                "верни exact fact_id в existing_fact_id. В snippet-pass для КАЖДОГО факта обязательно перечисли в "
                "evidence_refs только те opaque evidence_ref из snippets, которые прямо поддерживают именно этот тезис; "
                "source_urls должны соответствовать выбранным evidence_refs. URL без конкретного evidence_ref доказательством "
                "не является. Нельзя цитировать URL/ref вне evidence. Не сочиняй публикацию. previously_processed_sources "
                "— уже обработанные URL этого POI: они даны как coverage context, не как автоматическое доказательство.\n\n"
                + "Coverage goal: "
                + coverage_goal
                + "\nRetrieval query: "
                + query[:1000]
                + "\nEvidence (authoritative for extraction): "
                + json.dumps(evidence, ensure_ascii=False)
                + "\nCompact prior context (for dedup only, never evidence): "
                + json.dumps(
                    {
                        "place_name": str(topic_context.get("place_name") or "")[:300],
                        "known_facts": known_facts[:40],
                        "previously_considered_poi_facts": prior_poi[:20],
                        "previously_processed_sources": processed_sources[:40],
                    },
                    ensure_ascii=False,
                )
            )

        coverage_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=self.DISCOVERY_COVERAGE_SCHEMA,
        )
        chunk_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=self.CHUNK_EXTRACTION_SCHEMA,
        )

        def build_chunk_prompt(
            source_url: str,
            source_version_id: str,
            chunk: dict[str, Any],
            *,
            continuation_index: int = 0,
            already_returned: list[dict[str, str]] | None = None,
        ) -> str:
            prior = already_returned or []
            return (
                "Ты внутренний LLM-экстрактор Street Story. Передан один chunk документа. "
                "Извлекай атомарные проверяемые facts ТОЛЬКО когда утверждение поддерживается текстом секции [core]. "
                "[context_before] и [context_after] разрешено использовать только для разрешения ссылок, имён и границ; "
                "не создавай факт, если его содержательная опора находится только в context. "
                "Не используй знания вне chunk. Не превращай отсутствие ответа в факт. "
                "Если core оборван так, что смысл нельзя надёжно определить даже с context, needs_context=true. "
                "Если core понятен, needs_context=false, даже когда в нём нет релевантных facts. "
                "За один ответ верни не более 32 НОВЫХ атомарных facts. Если после этого в том же core остаются "
                "ещё содержательные не возвращённые facts, continuation_needed=true и кратко объясни это в "
                "continuation_reason. continuation_needed=false ставь только когда текущий core исчерпан. "
                "Если needs_context=true, continuation_needed=false: это отдельное terminal-состояние. "
                "На continuation-проходе не повторяй тезисы из Already returned facts. "
                "Для каждого факта source_urls должен содержать только переданный source_url. "
                "Для каждого fact обязательно верни evidence_spans с exact chunk_id и короткой дословной quote, "
                "скопированной из chunk. Не пересказывай quote и не ссылайся на другой chunk. "
                "Если факт семантически совпадает с known_facts, верни exact existing_fact_id; иначе оставь его пустым.\n\n"
                + "Coverage goal: " + coverage_goal + "\n"
                + "Retrieval query: " + query[:1000] + "\n"
                + "Source URL: " + source_url + "\n"
                + "Source version: " + source_version_id + "\n"
                + "Chunk id: " + str(chunk.get("chunk_id") or "") + "\n"
                + "Chunk ordinal: " + str(chunk.get("ordinal")) + "\n"
                + "Continuation batch index: " + str(max(0, int(continuation_index))) + "\n"
                + "Already returned facts from this exact chunk: "
                + json.dumps(prior, ensure_ascii=False)
                + "\nChunk text:\n" + str(chunk.get("text") or "")
                + "\nKnown facts (dedup references only): "
                + json.dumps(known_facts[:40], ensure_ascii=False)
            )

        async def call(key, timeout, *, model=None, quota=None):
            first = await self._generate(
                key,
                timeout,
                [build_prompt(evidence_rows(), page_pass=False)],
                coverage_config,
                operation="grounded_research",
                model=model,
                quota=quota,
            )
            try:
                payload = json.loads(first.text or "{}")
                if not isinstance(payload, dict):
                    raise ValueError
            except (ValueError, TypeError, json.JSONDecodeError):
                raise MalformedProviderResponse("gemini:malformed_discovery_facts") from None

            normalized_facts, official_urls, extraction_audit = normalize_payload(payload)
            preliminary_read_urls = [
                str(value or "").rstrip("/")
                for value in (payload.get("read_source_urls") or [])
                if str(value or "").rstrip("/") in allowed_urls
            ][:2]
            try:
                coverage_review = await self._review_coverage_contract(
                    key,
                    timeout,
                    coverage_goal=coverage_goal,
                    facts=normalized_facts,
                    sources=referenced_sources,
                    model=model,
                    quota=quota,
                    allow_page_reads=True,
                )
                if (
                    not coverage_review.get("coverage_satisfied")
                    and not coverage_review.get("read_source_urls")
                    and preliminary_read_urls
                ):
                    coverage_review["read_source_urls"] = preliminary_read_urls
                payload = {**payload, **coverage_review}
            except (
                GeminiUnavailable,
                PermanentProviderError,
                MalformedProviderResponse,
                ValueError,
                TypeError,
                json.JSONDecodeError,
            ):
                payload = {
                    **payload,
                    "coverage_satisfied": False,
                    "coverage_items": [],
                    "missing_aspects": ["coverage_review_unavailable"],
                    "read_source_urls": [],
                }

            selected_urls: list[str] = []
            if payload.get("coverage_satisfied") is False:
                for raw_url in payload.get("read_source_urls") or []:
                    canonical = str(raw_url or "").rstrip("/")
                    exact = allowed_urls.get(canonical)
                    if exact and exact not in selected_urls:
                        selected_urls.append(exact)
                    if len(selected_urls) >= 2:
                        break

            sources_for_result = [dict(source) for source in referenced_sources]
            semantic_completion = "gemini_research"
            page_chunk_failures = 0
            page_chunk_needs_context = 0
            page_chunk_deferred = 0
            page_continuation_batches = 0
            page_chunk_count = 0
            page_fact_count = 0
            if selected_urls:
                documents = await self._fetch_page_documents(
                    selected_urls,
                    {
                        **topic_context,
                        "research_sources": discovery.grounding_sources,
                    },
                )
                if documents:
                    source_result_by_url = {
                        str(source.get("url") or "").rstrip("/"): source
                        for source in sources_for_result
                        if isinstance(source, dict)
                    }
                    page_facts: list[dict[str, Any]] = []
                    page_official: list[str] = []
                    aggregate_audit = {
                        "raw_fact_count": int(extraction_audit.get("raw_fact_count") or 0),
                        "accepted_fact_count": int(extraction_audit.get("accepted_fact_count") or 0),
                        "claim_key_fallback_count": int(extraction_audit.get("claim_key_fallback_count") or 0),
                        "confidence_defaulted_count": int(extraction_audit.get("confidence_defaulted_count") or 0),
                        "rejected": dict(extraction_audit.get("rejected") or {}),
                    }
                    run_id = str(topic_context.get("research_run_id") or "").strip()
                    for requested_url, document in documents.items():
                        source = source_result_by_url.get(requested_url.rstrip("/"))
                        if source is None:
                            source = {
                                "type": "web",
                                "title": str(document.get("title") or requested_url),
                                "url": requested_url,
                            }
                            sources_for_result.append(source)
                            source_result_by_url[requested_url.rstrip("/")] = source
                        source["source_version_id"] = document["source_version_id"]
                        source["read_status"] = document["read_status"]
                        source["final_url"] = document["final_url"]
                        supports = [
                            item for item in (source.get("supports") or [])
                            if isinstance(item, dict)
                        ]
                        for chunk in document.get("chunks") or []:
                            page_chunk_count += 1
                            checkpoint = {
                                "status": "planned",
                                "terminal": False,
                                "facts": [],
                                "official_source_urls": [],
                                "next_batch_index": 0,
                                "continuation_batches": 0,
                                "payload_missing": False,
                                "raw_fact_count": 0,
                                "accepted_fact_count": 0,
                                "resumable_deferred_batches": [],
                            }
                            if run_id:
                                with self.store.connection() as db:
                                    checkpoint = chunk_checkpoint(
                                        db,
                                        run_id,
                                        chunk["chunk_id"],
                                    )
                                resumable_deferred = list(
                                    checkpoint.get("resumable_deferred_batches") or []
                                )
                                if resumable_deferred:
                                    placeholders = ",".join("?" for _ in resumable_deferred)
                                    with self.store.tx() as db:
                                        db.execute(
                                            f"UPDATE research_chunk_batches SET "
                                            f"status='continuation',error_code=NULL "
                                            f"WHERE run_id=? AND chunk_id=? "
                                            f"AND batch_index IN ({placeholders})",
                                            (
                                                run_id,
                                                chunk["chunk_id"],
                                                *resumable_deferred,
                                            ),
                                        )
                                if checkpoint.get("terminal"):
                                    restored_facts = [
                                        dict(item)
                                        for item in (checkpoint.get("facts") or [])
                                        if isinstance(item, dict)
                                    ]
                                    page_facts.extend(restored_facts)
                                    page_continuation_batches += int(
                                        checkpoint.get("continuation_batches") or 0
                                    )
                                    aggregate_audit["raw_fact_count"] += int(
                                        checkpoint.get("raw_fact_count") or 0
                                    )
                                    aggregate_audit["accepted_fact_count"] += len(
                                        restored_facts
                                    )
                                    for url in checkpoint.get("official_source_urls") or []:
                                        if url not in page_official:
                                            page_official.append(url)
                                    for restored in restored_facts:
                                        for span in restored.get("evidence_spans") or []:
                                            if not isinstance(span, dict):
                                                continue
                                            span_chunk_id = str(span.get("chunk_id") or "")
                                            quote = str(span.get("quote") or "")
                                            if not span_chunk_id or not quote:
                                                continue
                                            support = {
                                                "kind": "verified_page_span",
                                                "source_url": str(span.get("source_url") or requested_url),
                                                "source_version_id": str(
                                                    span.get("source_version_id")
                                                    or document["source_version_id"]
                                                ),
                                                "evidence_ref": span_chunk_id,
                                                "chunk_id": span_chunk_id,
                                                "text": quote,
                                                "span_start": span.get("span_start"),
                                                "span_end": span.get("span_end"),
                                            }
                                            if not any(
                                                isinstance(item, dict)
                                                and str(item.get("chunk_id") or "") == span_chunk_id
                                                and str(item.get("text") or "") == quote
                                                for item in supports
                                            ):
                                                supports.append(support)
                                    continue
                                with self.store.tx() as db:
                                    mark_chunk(
                                        db,
                                        run_id=run_id,
                                        chunk_id=chunk["chunk_id"],
                                        status="extracting",
                                        observation_count=len(checkpoint.get("facts") or []),
                                        model_name=str(model or ""),
                                        prompt_version="page-chunk-extraction-v2",
                                        now=self.store.now(),
                                    )
                            try:
                                normalized_text = str(document.get("normalized_text") or "")
                                core_start = int(chunk["core_start"])
                                core_end = int(chunk["core_end"])
                                evidence_chunks = {
                                    str(chunk["chunk_id"]): {
                                        "source_url": requested_url,
                                        "source_version_id": document["source_version_id"],
                                        "core_start": core_start,
                                        "core_text": normalized_text[core_start:core_end],
                                    }
                                }

                                chunk_facts_total: list[dict[str, Any]] = [
                                    dict(item)
                                    for item in (checkpoint.get("facts") or [])
                                    if isinstance(item, dict)
                                ]
                                seen_exact_outputs: set[str] = set()
                                already_returned: list[dict[str, str]] = []
                                for restored in chunk_facts_total:
                                    exact_payload = {
                                        "claim_key": str(restored.get("claim_key") or ""),
                                        "text": str(restored.get("text") or ""),
                                        "evidence_spans": restored.get("evidence_spans") or [],
                                    }
                                    seen_exact_outputs.add(
                                        hashlib.sha256(
                                            json.dumps(
                                                exact_payload,
                                                ensure_ascii=False,
                                                sort_keys=True,
                                                separators=(",", ":"),
                                            ).encode("utf-8")
                                        ).hexdigest()
                                    )
                                    already_returned.append({
                                        "claim_key": str(restored.get("claim_key") or "")[:300],
                                        "text": str(restored.get("text") or "")[:1200],
                                    })
                                    for span in restored.get("evidence_spans") or []:
                                        if not isinstance(span, dict):
                                            continue
                                        span_chunk_id = str(span.get("chunk_id") or "")
                                        quote = str(span.get("quote") or "")
                                        if not span_chunk_id or not quote:
                                            continue
                                        support = {
                                            "kind": "verified_page_span",
                                            "source_url": str(span.get("source_url") or requested_url),
                                            "source_version_id": str(
                                                span.get("source_version_id")
                                                or document["source_version_id"]
                                            ),
                                            "evidence_ref": span_chunk_id,
                                            "chunk_id": span_chunk_id,
                                            "text": quote,
                                            "span_start": span.get("span_start"),
                                            "span_end": span.get("span_end"),
                                        }
                                        if not any(
                                            isinstance(item, dict)
                                            and str(item.get("chunk_id") or "") == span_chunk_id
                                            and str(item.get("text") or "") == quote
                                            for item in supports
                                        ):
                                            supports.append(support)
                                for url in checkpoint.get("official_source_urls") or []:
                                    if url not in page_official:
                                        page_official.append(url)
                                aggregate_audit["raw_fact_count"] += int(
                                    checkpoint.get("raw_fact_count") or 0
                                )
                                aggregate_audit["accepted_fact_count"] += len(
                                    chunk_facts_total
                                )
                                page_continuation_batches += int(
                                    checkpoint.get("continuation_batches") or 0
                                )
                                terminal_status: str | None = None
                                terminal_error: str | None = None
                                continuation_budget = 6
                                batch_start = int(
                                    checkpoint.get("next_batch_index") or 0
                                )
                                batch_stop = batch_start + continuation_budget

                                for batch_index in range(batch_start, batch_stop):
                                    try:
                                        response = await self._generate(
                                            key,
                                            timeout,
                                            [
                                                build_chunk_prompt(
                                                    requested_url,
                                                    document["source_version_id"],
                                                    chunk,
                                                    continuation_index=batch_index,
                                                    already_returned=already_returned,
                                                )
                                            ],
                                            chunk_config,
                                            operation="grounded_research",
                                            model=model,
                                            quota=quota,
                                        )
                                        chunk_payload = json.loads(response.text or "{}")
                                        if (
                                            not isinstance(chunk_payload, dict)
                                            or not isinstance(chunk_payload.get("facts"), list)
                                            or not isinstance(chunk_payload.get("needs_context"), bool)
                                            or not isinstance(chunk_payload.get("context_reason"), str)
                                            or not isinstance(chunk_payload.get("continuation_needed"), bool)
                                            or not isinstance(chunk_payload.get("continuation_reason"), str)
                                        ):
                                            raise ValueError("malformed_chunk_extraction")
                                        if (
                                            chunk_payload["needs_context"]
                                            and chunk_payload["continuation_needed"]
                                        ):
                                            raise ValueError("chunk_context_continuation_conflict")

                                        batch_facts, chunk_official, chunk_audit = normalize_payload(
                                            {
                                                **chunk_payload,
                                                "official_source_urls": [],
                                            },
                                            evidence_chunks=evidence_chunks,
                                        )
                                    except (
                                        GeminiUnavailable,
                                        PermanentProviderError,
                                        MalformedProviderResponse,
                                        ValueError,
                                        TypeError,
                                        json.JSONDecodeError,
                                    ) as batch_exc:
                                        page_chunk_failures += 1
                                        terminal_status = "failed"
                                        terminal_error = type(batch_exc).__name__
                                        if run_id:
                                            with self.store.tx() as db:
                                                record_chunk_batch(
                                                    db,
                                                    run_id=run_id,
                                                    chunk_id=chunk["chunk_id"],
                                                    batch_index=batch_index,
                                                    status="failed",
                                                    raw_fact_count=0,
                                                    accepted_fact_count=0,
                                                    continuation_needed=False,
                                                    continuation_reason="",
                                                    model_name=str(model or ""),
                                                    prompt_version="page-chunk-extraction-v2",
                                                    error_code=terminal_error,
                                                    now=self.store.now(),
                                                )
                                        break

                                    new_batch_facts: list[dict[str, Any]] = []
                                    duplicate_count = 0
                                    for fact in batch_facts:
                                        exact_payload = {
                                            "claim_key": str(fact.get("claim_key") or ""),
                                            "text": str(fact.get("text") or ""),
                                            "evidence_spans": fact.get("evidence_spans") or [],
                                        }
                                        exact_fingerprint = hashlib.sha256(
                                            json.dumps(
                                                exact_payload,
                                                ensure_ascii=False,
                                                sort_keys=True,
                                                separators=(",", ":"),
                                            ).encode("utf-8")
                                        ).hexdigest()
                                        if exact_fingerprint in seen_exact_outputs:
                                            duplicate_count += 1
                                            continue
                                        seen_exact_outputs.add(exact_fingerprint)

                                        refs: list[str] = []
                                        for span in fact.get("evidence_spans") or []:
                                            span_chunk_id = str(span.get("chunk_id") or "")
                                            if span_chunk_id and span_chunk_id not in refs:
                                                refs.append(span_chunk_id)
                                            support = {
                                                "kind": "verified_page_span",
                                                "source_url": span["source_url"],
                                                "source_version_id": span["source_version_id"],
                                                "evidence_ref": span_chunk_id,
                                                "chunk_id": span_chunk_id,
                                                "text": span["quote"],
                                                "span_start": span["span_start"],
                                                "span_end": span["span_end"],
                                            }
                                            if not any(
                                                isinstance(item, dict)
                                                and str(item.get("chunk_id") or "") == span_chunk_id
                                                and str(item.get("text") or "") == span["quote"]
                                                for item in supports
                                            ):
                                                supports.append(support)
                                        fact["evidence_refs"] = refs
                                        new_batch_facts.append(fact)
                                        already_returned.append({
                                            "claim_key": str(fact.get("claim_key") or "")[:300],
                                            "text": str(fact.get("text") or "")[:1200],
                                        })

                                    chunk_facts_total.extend(new_batch_facts)
                                    for url in chunk_official:
                                        if url not in page_official:
                                            page_official.append(url)

                                    for field in (
                                        "raw_fact_count",
                                        "claim_key_fallback_count",
                                        "confidence_defaulted_count",
                                    ):
                                        aggregate_audit[field] += int(chunk_audit.get(field) or 0)
                                    aggregate_audit["accepted_fact_count"] += len(new_batch_facts)
                                    for reason, count in (chunk_audit.get("rejected") or {}).items():
                                        aggregate_audit["rejected"][reason] = (
                                            int(aggregate_audit["rejected"].get(reason) or 0)
                                            + int(count or 0)
                                        )
                                    if duplicate_count:
                                        aggregate_audit["rejected"]["continuation_exact_duplicate"] = (
                                            int(
                                                aggregate_audit["rejected"].get(
                                                    "continuation_exact_duplicate"
                                                )
                                                or 0
                                            )
                                            + duplicate_count
                                        )

                                    continuation_needed = bool(
                                        chunk_payload["continuation_needed"]
                                    )
                                    continuation_reason = str(
                                        chunk_payload.get("continuation_reason") or ""
                                    ).strip()

                                    batch_status = "completed"
                                    batch_error = None
                                    if chunk_payload["needs_context"]:
                                        terminal_status = "needs_context"
                                        page_chunk_needs_context += 1
                                    elif continuation_needed:
                                        if not new_batch_facts:
                                            batch_status = "deferred"
                                            batch_error = "continuation_stalled"
                                            terminal_status = "deferred"
                                            terminal_error = batch_error
                                            page_chunk_deferred += 1
                                        elif batch_index + 1 >= batch_stop:
                                            batch_status = "deferred"
                                            batch_error = "continuation_limit"
                                            terminal_status = "deferred"
                                            terminal_error = batch_error
                                            page_chunk_deferred += 1
                                        else:
                                            batch_status = "continuation"
                                            page_continuation_batches += 1
                                    else:
                                        terminal_status = (
                                            "extracted"
                                            if chunk_facts_total
                                            else "no_claims"
                                        )

                                    if run_id:
                                        with self.store.tx() as db:
                                            record_chunk_batch(
                                                db,
                                                run_id=run_id,
                                                chunk_id=chunk["chunk_id"],
                                                batch_index=batch_index,
                                                status=batch_status,
                                                raw_fact_count=len(
                                                    chunk_payload.get("facts") or []
                                                ),
                                                accepted_fact_count=len(new_batch_facts),
                                                continuation_needed=continuation_needed,
                                                continuation_reason=continuation_reason,
                                                model_name=str(model or ""),
                                                prompt_version="page-chunk-extraction-v2",
                                                error_code=batch_error,
                                                payload={
                                                    "facts": new_batch_facts,
                                                    "official_source_urls": chunk_official,
                                                    "no_claims": bool(
                                                        terminal_status == "no_claims"
                                                    ),
                                                },
                                                now=self.store.now(),
                                            )

                                    if terminal_status is not None:
                                        break

                                if terminal_status is None:
                                    terminal_status = "deferred"
                                    terminal_error = "continuation_unresolved"
                                    page_chunk_deferred += 1

                                page_facts.extend(chunk_facts_total)
                                if run_id:
                                    with self.store.tx() as db:
                                        mark_chunk(
                                            db,
                                            run_id=run_id,
                                            chunk_id=chunk["chunk_id"],
                                            status=terminal_status,
                                            observation_count=len(chunk_facts_total),
                                            model_name=str(model or ""),
                                            prompt_version="page-chunk-extraction-v2",
                                            error_code=terminal_error,
                                            now=self.store.now(),
                                        )
                            except (
                                GeminiUnavailable,
                                PermanentProviderError,
                                MalformedProviderResponse,
                                ValueError,
                                TypeError,
                                json.JSONDecodeError,
                            ) as exc:
                                page_chunk_failures += 1
                                if run_id:
                                    with self.store.tx() as db:
                                        mark_chunk(
                                            db,
                                            run_id=run_id,
                                            chunk_id=chunk["chunk_id"],
                                            status="failed",
                                            observation_count=len(
                                                checkpoint.get("facts") or []
                                            ),
                                            model_name=str(model or ""),
                                            prompt_version="page-chunk-extraction-v2",
                                            error_code=type(exc).__name__,
                                            now=self.store.now(),
                                        )
                                continue
                        source["supports"] = supports

                    if page_facts:
                        normalized_facts = [*normalized_facts, *page_facts]
                        page_fact_count = len(page_facts)
                    for url in page_official:
                        if url not in official_urls:
                            official_urls.append(url)
                    extraction_audit = aggregate_audit
                    semantic_completion = "gemini_research_page_chunks"

                    try:
                        coverage_review = await self._review_coverage_contract(
                            key,
                            timeout,
                            coverage_goal=coverage_goal,
                            facts=normalized_facts,
                            sources=sources_for_result,
                            model=model,
                            quota=quota,
                            allow_page_reads=False,
                        )
                        payload = {**payload, **coverage_review}
                    except (
                        GeminiUnavailable,
                        PermanentProviderError,
                        MalformedProviderResponse,
                        ValueError,
                        TypeError,
                        json.JSONDecodeError,
                    ):
                        payload = {
                            **payload,
                            "coverage_satisfied": False,
                            "coverage_items": [],
                            "missing_aspects": ["coverage_review_unavailable"],
                            "read_source_urls": [],
                        }

            return GroundedResearch(
                payload={
                    "summary": str(payload.get("summary") or "")[:1200],
                    "official_source_urls": official_urls,
                    "facts": normalized_facts,
                    "search_provider": str(discovery.payload.get("search_provider") or "duckduckgo_html_fallback"),
                    "semantic_completion": semantic_completion,
                    "coverage_satisfied": bool(payload.get("coverage_satisfied")),
                    "coverage_items": [
                        item
                        for item in (payload.get("coverage_items") or [])[:40]
                        if isinstance(item, dict)
                    ],
                    "missing_aspects": [
                        str(value)[:300]
                        for value in (payload.get("missing_aspects") or [])[:20]
                        if str(value).strip()
                    ],
                    "page_chunk_count": page_chunk_count,
                    "page_chunk_failures": page_chunk_failures,
                    "page_chunk_needs_context": page_chunk_needs_context,
                    "page_chunk_deferred": page_chunk_deferred,
                    "page_continuation_batches": page_continuation_batches,
                    "page_fact_count": page_fact_count,
                    "extraction_audit": extraction_audit,
                },
                grounding_sources=sources_for_result,
            )

        retry_at: list[float] = []
        for model, _pool, quota, executor in self.research_routes:
            async def routed_call(key, timeout, *, _model=model, _quota=quota):
                return await call(key, timeout, model=_model, quota=_quota)
            try:
                return await executor.execute("grounded_research", routed_call)
            except GeminiUnavailable as exc:
                if exc.retry_at is not None:
                    retry_at.append(exc.retry_at)
                continue
            except PermanentProviderError as exc:
                if str(exc) == "gemini:unsupported_model":
                    continue
                raise
        if retry_at:
            raise GeminiUnavailable(min(retry_at), "all_discovery_fact_models_unavailable")
        raise PermanentProviderError("gemini:unsupported_model")

    async def reconcile_fact_identities(
        self,
        incoming_facts: list[dict[str, Any]],
        existing_facts: list[dict[str, Any]],
        *,
        page_size: int = 40,
    ) -> dict[str, Any]:
        """Semantically map incoming claims onto the complete durable inventory.

        Code only pages and validates IDs. The configured model decides whether
        two statements express the same factual proposition. Different values,
        dates, people, positions, scopes or temporal stages must stay distinct.
        """
        from google.genai import types

        incoming: list[dict[str, Any]] = []
        for index, item in enumerate(incoming_facts):
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split()).strip()
            if not text or len(text) > 1200:
                continue
            incoming.append({
                "incoming_index": index,
                "claim_key": str(item.get("claim_key") or "")[:300],
                "text": text,
                "existing_fact_id": str(item.get("existing_fact_id") or "")[:200],
            })

        existing: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for item in existing_facts:
            if not isinstance(item, dict):
                continue
            fact_id = str(item.get("fact_id") or "").strip()
            text = " ".join(str(item.get("text") or "").split()).strip()
            if not fact_id or fact_id in seen_ids or not text:
                continue
            seen_ids.add(fact_id)
            existing.append({
                "fact_id": fact_id,
                "claim_key": str(item.get("claim_key") or "")[:300],
                "text": text[:1200],
            })

        explicit = {
            int(item["incoming_index"]): str(item["existing_fact_id"])
            for item in incoming
            if str(item.get("existing_fact_id") or "") in seen_ids
        }
        unresolved = {
            int(item["incoming_index"])
            for item in incoming
            if int(item["incoming_index"]) not in explicit
        }
        matches = dict(explicit)
        decisions: list[dict[str, Any]] = [
            {
                "incoming_index": index,
                "relation": "equivalent",
                "existing_fact_id": fact_id,
                "rationale": "Upstream extraction explicitly referenced this durable fact ID.",
                "model_name": "upstream_existing_fact_id",
                "prompt_version": "fact-identity-reconciliation-v1",
            }
            for index, fact_id in sorted(explicit.items())
        ]
        if not unresolved or not existing:
            return {
                "matches": matches,
                "decisions": decisions,
                "pages_reviewed": 0,
                "existing_fact_count": len(existing),
                "incoming_fact_count": len(incoming),
                "complete": True,
                "unmatched_count": len(unresolved),
            }

        size = max(10, min(int(page_size), 50))
        pages_reviewed = 0
        for start in range(0, len(existing), size):
            if not unresolved:
                break
            page = existing[start:start + size]
            page_ids = {item["fact_id"] for item in page}
            current_incoming = [
                item
                for item in incoming
                if int(item["incoming_index"]) in unresolved
            ]
            prompt = (
                "Ты внутренний LLM-reconciler фактов Street Story. Сопоставь новые утверждения только с "
                "фактами на ТЕКУЩЕЙ странице durable inventory. equivalent=true только если это один и тот же "
                "проверяемый фактический тезис, допускающий обычное перефразирование. Общий объект/тема недостаточны. "
                "Разные даты, числа, люди, позиции слева/справа, этапы времени, область или значение — НЕ эквивалентны. "
                "Не решай, какой факт истиннее, и не объединяй противоречия: это отдельный arbitration layer. "
                "Для каждого incoming_index верни одну строку. При отсутствии эквивалента equivalent=false и "
                "existing_fact_id=''. При equivalent=true existing_fact_id обязан быть exact ID из этой страницы.\n\n"
                "Incoming: " + json.dumps(current_incoming, ensure_ascii=False)
                + "\nExisting page: " + json.dumps(page, ensure_ascii=False)
            )
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=self.FACT_IDENTITY_RECONCILIATION_SCHEMA,
            )

            async def call(key, timeout, *, model=None, quota=None):
                response = await self._generate(
                    key,
                    timeout,
                    [prompt],
                    config,
                    operation="grounded_research",
                    model=model,
                    quota=quota,
                )
                try:
                    payload = json.loads(response.text or "{}")
                    rows = payload.get("matches")
                    if not isinstance(rows, list):
                        raise ValueError
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise MalformedProviderResponse("gemini:malformed_fact_reconciliation") from None
                return rows

            rows = None
            used_model = ""
            retry_at: list[float] = []
            for model, _pool, quota, executor in self.research_routes:
                async def routed_call(key, timeout, *, _model=model, _quota=quota):
                    return await call(key, timeout, model=_model, quota=_quota)
                try:
                    rows = await executor.execute("grounded_research", routed_call)
                    used_model = str(model or "")
                    break
                except GeminiUnavailable as exc:
                    if exc.retry_at is not None:
                        retry_at.append(exc.retry_at)
                    continue
                except PermanentProviderError as exc:
                    if str(exc) == "gemini:unsupported_model":
                        continue
                    raise
            if rows is None:
                if retry_at:
                    raise GeminiUnavailable(min(retry_at), "all_fact_reconciliation_models_unavailable")
                raise PermanentProviderError("gemini:unsupported_model")

            pages_reviewed += 1
            for row in rows:
                if not isinstance(row, dict) or row.get("equivalent") is not True:
                    continue
                try:
                    incoming_index = int(row.get("incoming_index"))
                except (TypeError, ValueError):
                    continue
                existing_fact_id = str(row.get("existing_fact_id") or "").strip()
                if incoming_index not in unresolved or existing_fact_id not in page_ids:
                    continue
                matches[incoming_index] = existing_fact_id
                decisions.append({
                    "incoming_index": incoming_index,
                    "relation": "equivalent",
                    "existing_fact_id": existing_fact_id,
                    "rationale": str(row.get("rationale") or "")[:1000],
                    "model_name": used_model,
                    "prompt_version": "fact-identity-reconciliation-v1",
                })
                unresolved.discard(incoming_index)

        return {
            "matches": matches,
            "decisions": decisions,
            "pages_reviewed": pages_reviewed,
            "existing_fact_count": len(existing),
            "incoming_fact_count": len(incoming),
            "complete": True,
            "unmatched_count": len(unresolved),
        }


    async def assess_fact_candidates(self, items, context):
        """Independent bounded semantic advice using the configured research routes.

        This does not write eligibility or replace Live's final review. Code does
        not classify claims or supply expected answers.
        """
        from google.genai import types
        from .review_packets import REVIEW_CHECKS
        if not 1 <= len(items) <= 12:
            raise ValueError('bounded_semantic_review_batch')
        schema = {"type": "object", "properties": {"decisions": {"type": "array", "items": {
            "type": "object", "properties": {
                "fact": {"type": "integer"},
                "verdict": {"type": "string", "enum": ["supported", "insufficient", "repair_needed", "contradicted", "role_mismatch"]},
                "reason": {"type": "string"}, "needs_context": {"type": "boolean"},
                "propositions": {"type": "array", "items": {"type": "string"}},
                "replacement_texts": {"type": "array", "items": {"type": "string"}},
            }, "required": ["fact", "verdict", "reason", "needs_context", "propositions", "replacement_texts"]
        }}}, "required": ["decisions"]}
        prompt = ('You independently verify unverified Street Story candidates. ' + REVIEW_CHECKS
                  + ' Enumerate each independent person/role/event in propositions, do not simply repeat a compound sentence. '
                  + 'replacement_texts may propose narrower or split claims supported by OWN evidence, '
                  + 'never manufacture missing context. Correct affirmative candidates must stay supported. '
                  + 'Give a short checkable reason, not private reasoning. No prior verdicts are provided.\n'
                  + 'Confirmed POI: ' + json.dumps(context, ensure_ascii=False) + '\n'
                  + 'Candidates with their own attached evidence: ' + json.dumps(items, ensure_ascii=False))
        config = types.GenerateContentConfig(response_mime_type='application/json', response_json_schema=schema)
        retry_at = []
        for model, _pool, quota, executor in self.research_routes:
            async def call(key, timeout, *, _model=model, _quota=quota):
                response = await self._generate(key, timeout, [prompt], config, operation='grounded_research', model=_model, quota=_quota)
                try:
                    payload = json.loads(response.text or '{}')
                    decisions = payload['decisions']
                    numbers = [d['fact'] for d in decisions]
                    if len(numbers) != len(items) or set(numbers) != {item['fact'] for item in items}:
                        raise ValueError('incomplete_or_foreign_decisions')
                    if any(len(d['reason']) > 500 or not 1 <= len(d['propositions']) <= 8 or len(d['replacement_texts']) > 8 or any(len(t) > 500 for t in d['propositions'] + d['replacement_texts']) for d in decisions):
                        raise ValueError('oversize_semantic_advice')
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    raise MalformedProviderResponse('gemini:malformed_semantic_review') from None
                return {'model': _model, 'decisions': decisions}
            try:
                return await executor.execute('grounded_research', call)
            except GeminiUnavailable as exc:
                if exc.retry_at is not None:
                    retry_at.append(exc.retry_at)
            except PermanentProviderError as exc:
                if str(exc) != 'gemini:unsupported_model':
                    raise
        raise GeminiUnavailable(min(retry_at) if retry_at else None, 'semantic_review_helpers_unavailable')

    async def detect_fact_conflicts(
        self,
        items: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Exhaustively scan the durable fact inventory using bounded LLM batches.

        Code only partitions the inventory and validates returned IDs. The model
        decides whether any pair is actually contradictory or meaningfully
        divergent. Cross-block batches ensure facts beyond the first page are not
        semantically invisible.
        """
        model_items = conflict_scan_items(items, max_items=None)
        if len(model_items) < 2:
            return {
                "records": [],
                "coverage_complete": True,
                "batch_count": 0,
                "fact_count": len(model_items),
            }
        from google.genai import types

        block_size = 24
        blocks = [
            model_items[start:start + block_size]
            for start in range(0, len(model_items), block_size)
        ]
        accumulated: dict[str, dict[str, Any]] = {}
        batch_count = 0

        async def detect_batch(
            batch: list[dict[str, Any]],
            *,
            group_a_ids: set[str],
            group_b_ids: set[str] | None,
        ) -> list[dict[str, Any]]:
            cross_instruction = (
                "Это cross-block проход. Возвращай ТОЛЬКО пары, где один fact_id из Group A, "
                "а второй из Group B; пары внутри одной группы уже проверяются отдельно.\n"
                f"Group A IDs: {json.dumps(sorted(group_a_ids), ensure_ascii=False)}\n"
                f"Group B IDs: {json.dumps(sorted(group_b_ids or set()), ensure_ascii=False)}\n"
                if group_b_ids is not None
                else
                "Это within-block проход: проверь все пары внутри переданного блока.\n"
            )
            prompt = (
                "Ты внутренний арбитр фактов Street Story. Перед тобой bounded batch из полного durable inventory "
                "одного POI. Самостоятельно найди только пары со смысловым противоречием или важным расхождением; "
                "сервер НЕ отбирал пары по словам, датам или типам. "
                + cross_instruction
                + "Ссылайся только на существующие fact_id из входа через left_fact_id/right_fact_id. "
                "relation: contradiction — одновременно истинными в одном смысле быть не могут; "
                "scope_difference — различаются объект/период/область; temporal_sequence — разные этапы времени; "
                "source_disagreement — источники расходятся и нужна дополнительная проверка; uncertain — данных мало. "
                "Не возвращай эквивалентные или просто разные совместимые факты. suggested_resolution: prefer_left, "
                "prefer_right, both_valid или unresolved. Количество сайтов НЕ является голосованием за истину. "
                "Учитывай происхождение, период, первичность, supports и Regional Knowledge evidence. "
                "Если доказательств недостаточно — unresolved. Не выдумывай факты, ссылки или идентификаторы.\n\n"
                "Context: " + json.dumps(context or {}, ensure_ascii=False)[:4000] + "\n"
                "Facts: " + json.dumps(batch, ensure_ascii=False)
            )
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=GeminiClient.FACT_CONFLICT_SCHEMA,
            )

            async def call(key, timeout, *, model=None, quota=None):
                response = await self._generate(
                    key,
                    timeout,
                    [prompt],
                    config,
                    operation="grounded_research",
                    model=model,
                    quota=quota,
                )
                try:
                    payload = json.loads(response.text or "{}")
                    if not isinstance(payload, dict) or not isinstance(payload.get("conflicts"), list):
                        raise ValueError
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise MalformedProviderResponse("gemini:malformed_fact_conflicts") from None
                records = normalize_model_conflict_records(batch, payload)
                if group_b_ids is None:
                    return records
                filtered: list[dict[str, Any]] = []
                for record in records:
                    left = str(record.get("left_fact_id") or "")
                    right = str(record.get("right_fact_id") or "")
                    cross = (
                        (left in group_a_ids and right in group_b_ids)
                        or (right in group_a_ids and left in group_b_ids)
                    )
                    if cross:
                        filtered.append(record)
                return filtered

            retry_at: list[float] = []
            for model, _pool, quota, executor in self.research_routes:
                async def routed_call(key, timeout, *, _model=model, _quota=quota):
                    return await call(key, timeout, model=_model, quota=_quota)
                try:
                    return await executor.execute("grounded_research", routed_call)
                except GeminiUnavailable as exc:
                    if exc.retry_at is not None:
                        retry_at.append(exc.retry_at)
                    continue
                except PermanentProviderError as exc:
                    if str(exc) == "gemini:unsupported_model":
                        continue
                    raise
            if retry_at:
                raise GeminiUnavailable(min(retry_at), "all_fact_conflict_models_unavailable")
            raise PermanentProviderError("gemini:unsupported_model")

        for left_index, left_block in enumerate(blocks):
            left_ids = {str(item["fact_id"]) for item in left_block}
            records = await detect_batch(
                left_block,
                group_a_ids=left_ids,
                group_b_ids=None,
            )
            batch_count += 1
            for record in records:
                accumulated[str(record["conflict_id"])] = record

            for right_index in range(left_index + 1, len(blocks)):
                right_block = blocks[right_index]
                right_ids = {str(item["fact_id"]) for item in right_block}
                records = await detect_batch(
                    [*left_block, *right_block],
                    group_a_ids=left_ids,
                    group_b_ids=right_ids,
                )
                batch_count += 1
                for record in records:
                    accumulated[str(record["conflict_id"])] = record

        return {
            "records": list(accumulated.values()),
            "coverage_complete": True,
            "batch_count": batch_count,
            "fact_count": len(model_items),
        }


    async def compose_publication(
        self,
        *,
        place_name: str | None,
        concept: str,
        author_note: str,
        facts: list[dict[str, Any]],
    ) -> dict[str, str]:
        """Compose editorial copy from already validated evidence-backed facts."""
        if not facts and not author_note.strip():
            return {"concept": concept.strip(), "draft_text": ""}
        from google.genai import types

        prompt = (
            "Ты редактор Street Story. Сформируй концепцию и готовый текст публикации на русском языке. "
            "Если publication_concept уже задан автором, сохрани его смысл; иначе предложи ясный редакционный угол. "
            "Используй ТОЛЬКО evidence-backed facts из входа и субъективный author_note. Не добавляй новые исторические "
            "сведения, даты, имена или причинно-следственные связи. Текст должен читаться как публикация, а не как "
            "список тезисов: обычно 2–5 коротких связных абзацев, естественный заход, развитие и завершение. "
            "Не делай каждый факт отдельным абзацем автоматически. Без Markdown-заголовка и служебных комментариев. "
            "Уложись в 1000 символов, чтобы оставался запас под Telegram photo caption.\n\n"
            + json.dumps(
                {
                    "place_name": place_name,
                    "publication_concept": concept,
                    "author_note": author_note,
                    "facts": facts,
                },
                ensure_ascii=False,
            )
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=self.COMPOSE_SCHEMA,
        )

        async def call(key, timeout, *, model=None, quota=None):
            response = await self._generate(
                key,
                timeout,
                [prompt],
                config,
                operation="grounded_research",
                model=model,
                quota=quota,
            )
            try:
                payload = json.loads(response.text or "{}")
                result_concept = str(payload.get("concept") or "").strip()
                draft = str(payload.get("draft_text") or "").strip()
                if not draft or len(draft) > 1024 or len(result_concept) > 1200:
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                raise MalformedProviderResponse("gemini:malformed_publication_composition") from None
            return {"concept": result_concept, "draft_text": draft}

        retry_at: list[float] = []
        for model, _pool, quota, executor in self.research_routes:
            async def routed_call(key, timeout, *, _model=model, _quota=quota):
                return await call(key, timeout, model=_model, quota=_quota)
            try:
                return await executor.execute("grounded_research", routed_call)
            except GeminiUnavailable as exc:
                if exc.retry_at is not None:
                    retry_at.append(exc.retry_at)
                continue
            except PermanentProviderError as exc:
                if str(exc) == "gemini:unsupported_model":
                    continue
                raise
        if retry_at:
            raise GeminiUnavailable(min(retry_at), "all_publication_composition_models_unavailable")
        raise PermanentProviderError("gemini:unsupported_model")

    async def _semantic_complete_discovery_best_effort(
        self,
        query: str,
        topic_context: dict[str, Any],
        discovery: GroundedResearch,
        *,
        timeout_seconds: float,
    ) -> GroundedResearch | None:
        try:
            async with asyncio.timeout(max(0.1, float(timeout_seconds))):
                return await self._semantic_complete_discovery(
                    query,
                    topic_context,
                    discovery,
                )
        except (
            TimeoutError,
            GeminiUnavailable,
            PermanentProviderError,
            MalformedProviderResponse,
        ):
            return None

    async def search_web(
        self,
        query: str,
        topic_context: dict[str, Any],
    ) -> GroundedResearch:
        """One bounded Google Search grounding call used as a Gemini Live tool.

        This helper never owns the conversation and never drafts publication text.
        It returns evidence to the already-running Gemini 3.8 Live session.
        """
        from google.genai import types

        query = str(query or "").strip()
        if not query:
            raise ValueError("web search query is required")

        cached_sources = self._cached_evidence_sources(topic_context)
        if cached_sources:
            cached_discovery = GroundedResearch(
                payload={
                    "summary": "Previously persisted POI evidence.",
                    "official_source_urls": [],
                    "facts": [],
                    "search_provider": "poi_cache",
                    "cached_source_count": len(cached_sources),
                },
                grounding_sources=cached_sources,
            )
            cached_result = await self._semantic_complete_discovery_best_effort(
                query,
                topic_context,
                cached_discovery,
                timeout_seconds=self.SEMANTIC_CACHE_PREFLIGHT_SECONDS,
            )
            if (
                cached_result is not None
                and cached_result.payload.get("coverage_satisfied") is True
                and bool(cached_result.payload.get("facts"))
            ):
                cached_result.payload["cache_only"] = True
                return cached_result

        prompt = (
            "Ты внутренний поисковый инструмент Street Story, а не собеседник. "
            "Используй Google Search grounding только для запроса пользователя. "
            "Сначала обязательно попробуй найти официальный источник объекта или организации, если он существует: "
            "сайт владельца, музея, учреждения, муниципалитета или оператора. Wikipedia, СМИ, агрегатор и "
            "туристический каталог официальным источником не являются. Верни реально найденные официальные URL "
            "в official_source_urls. Верни до 32 проверяемых ФАКТОВ, а не список источников. Каждый fact.text — "
            "один атомарный тезис, обычно до 500 знаков: дата, человек, архитектор, событие, функция, реконструкция, "
            "посещение или другой конкретный факт. Без вводных вроде «источник сообщает», без URL и без нескольких "
            "разных утверждений в одном пункте. Если evidence содержит несколько независимо проверяемых людей, дат, "
            "ролей, подписей или деталей, разнеси их в отдельные facts вместо одного перечня. Для каждого факта задай "
            "claim_key — короткую устойчивую "
            "семантическую идентичность смысла, не зависящую от перефразирования. Если новый найденный тезис семантически "
            "совпадает с known_facts, укажи его точный fact_id в existing_fact_id; иначе existing_fact_id оставь пустым. "
            "Не выдумывай existing_fact_id. Для новых тезисов используй устойчивый claim_key. Самые важные факты "
            "ставь первыми; сведения официального источника имеют приоритет. За один ответ верни не более 32 facts; "
            "предварительно разложи coverage_goal на минимальные независимо проверяемые coverage_items; если явно запрошено "
            "несколько позиций, ролей, людей или иных отдельных элементов, каждый должен быть отдельным coverage_item. "
            "coverage_satisfied=true только если evidence действительно закрывает все coverage_items; иначе false и перечисли "
            "конкретные missing_aspects. Независимо от этого, "
            "если в текущем grounding evidence остаются дополнительные содержательные атомарные facts, обязательно поставь "
            "continuation_needed=true и кратко объясни continuation_reason. continuation_needed=false допустим только когда "
            "текущий grounding evidence исчерпан. Если Current topic context содержит "
            "previously_considered_poi_facts, не повторяй их без явной просьбы пользователя повторить или перепроверить. "
            "Cached POI evidence ниже уже было получено ранее и может поддерживать новый аспект без повторного открытия страницы. "
            "Если оно не закрывает coverage_goal, ищи новые evidence; старый URL не запрещён, если он снова релевантен новому пробелу. "
            "Фраза о том, что источник не содержит нужной информации, НЕ является фактом об объекте и не должна попадать в facts. "
            "Для каждого факта укажи только source_urls из текущего grounding или Cached POI evidence. "
            "Не пиши публикацию и не предлагай редактуру.\n\n"
            "Coverage goal: " + str(topic_context.get("coverage_goal") or query)[:1600] + "\n"
            "Search query: " + query[:1000] + "\n"
            "Cached POI evidence: " + json.dumps(self._cached_evidence_sources(topic_context), ensure_ascii=False) + "\n"
            "Compact topic context: " + json.dumps(
                {
                    "place_name": topic_context.get("place_name"),
                    "known_facts": list(topic_context.get("known_facts") or [])[:40],
                    "previously_considered_poi_facts": list(topic_context.get("previously_considered_poi_facts") or [])[:20],
                    "visual_identity": topic_context.get("visual_identity"),
                },
                ensure_ascii=False,
            )
        )
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            response_mime_type="application/json",
            response_json_schema=self.NATIVE_WEB_SEARCH_SCHEMA,
        )

        async def call(key, timeout, *, model=None, quota=None):
            response = await self._generate(
                key,
                timeout,
                [prompt],
                config,
                operation="web_search",
                model=model,
                quota=quota,
            )
            try:
                payload = json.loads(response.text or "{}")
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("summary"), str)
                    or not isinstance(payload.get("official_source_urls"), list)
                    or any(not isinstance(url, str) for url in payload["official_source_urls"])
                    or not isinstance(payload.get("facts"), list)
                    or not isinstance(payload.get("coverage_satisfied"), bool)
                    or not isinstance(payload.get("missing_aspects"), list)
                    or any(not isinstance(value, str) for value in payload["missing_aspects"])
                    or not isinstance(payload.get("continuation_needed"), bool)
                    or not isinstance(payload.get("continuation_reason"), str)
                ):
                    raise ValueError
            except (ValueError, TypeError):
                raise MalformedProviderResponse("gemini:malformed_web_search") from None

            sources: list[dict[str, Any]] = []
            supports_by_url: dict[str, list[dict[str, str]]] = {}
            for candidate in getattr(response, "candidates", []) or []:
                metadata = getattr(candidate, "grounding_metadata", None)
                local_sources: list[dict[str, Any]] = []
                for chunk in getattr(metadata, "grounding_chunks", []) or []:
                    web = getattr(chunk, "web", None)
                    uri = getattr(web, "uri", None)
                    source = {
                        "type": "web",
                        "title": str(getattr(web, "title", "") or uri or ""),
                        "url": uri if isinstance(uri, str) and uri.startswith("https://") else "",
                    }
                    local_sources.append(source)
                    if source["url"]:
                        sources.append(source)
                for support in getattr(metadata, "grounding_supports", []) or []:
                    segment = getattr(support, "segment", None)
                    text = str(getattr(segment, "text", "") or "").strip()
                    if not text:
                        continue
                    for index in getattr(support, "grounding_chunk_indices", []) or []:
                        if isinstance(index, int) and 0 <= index < len(local_sources):
                            url = str(local_sources[index].get("url") or "")
                            if url:
                                supports_by_url.setdefault(url, []).append({
                                    "kind": "google_grounding",
                                    "source_url": url,
                                    "text": text[:600],
                                })
            unique_sources = list({source["url"]: source for source in sources}.values())
            current_sources = [
                {
                    **source,
                    **(
                        {"supports": supports_by_url.get(source["url"], [])}
                        if supports_by_url.get(source["url"])
                        else {}
                    ),
                }
                for source in unique_sources
            ]
            available_sources = self._merge_evidence_sources(
                current_sources,
                self._cached_evidence_sources(topic_context),
            )
            run_id = str(topic_context.get("research_run_id") or "").strip()
            if run_id:
                with self.store.tx() as db:
                    for source in available_sources:
                        url = str(source.get("url") or "").rstrip("/")
                        if not url.startswith("https://"):
                            continue
                        register_discovered_source(
                            db,
                            run_id=run_id,
                            url=url,
                            title=str(source.get("title") or url),
                            status="snippet_only",
                            now=self.store.now(),
                        )
            seen = {source["url"].rstrip("/") for source in available_sources}
            raw_facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
            extraction_audit = {
                "raw_fact_count": len(raw_facts),
                "accepted_fact_count": 0,
                "claim_key_fallback_count": 0,
                "confidence_defaulted_count": 0,
                "rejected": {},
            }

            def reject(reason: str) -> None:
                rejected = extraction_audit["rejected"]
                rejected[reason] = int(rejected.get(reason, 0)) + 1

            candidate_facts: list[dict[str, Any]] = []
            for item in raw_facts:
                if not isinstance(item, dict):
                    reject("invalid_shape")
                    continue
                fact_text = " ".join(str(item.get("text") or "").split()).strip()
                if not fact_text:
                    reject("empty_text")
                    continue
                if len(fact_text) > 1200:
                    reject("text_over_safety_bound")
                    continue

                claim_key = " ".join(str(item.get("claim_key") or "").split()).strip().casefold()
                if not claim_key or len(claim_key) > 300:
                    claim_key = "exact-text:" + hashlib.sha256(fact_text.casefold().encode("utf-8")).hexdigest()[:24]
                    extraction_audit["claim_key_fallback_count"] += 1

                try:
                    confidence = float(item.get("confidence", 0.0))
                    if not math.isfinite(confidence):
                        raise ValueError
                except (TypeError, ValueError):
                    confidence = 0.0
                    extraction_audit["confidence_defaulted_count"] += 1

                source_hints: list[str] = []
                for raw_url in item.get("source_urls") or []:
                    normalized_url = str(raw_url or "").rstrip("/")
                    if normalized_url in seen and normalized_url not in source_hints:
                        source_hints.append(normalized_url)

                candidate = {
                    "claim_key": claim_key,
                    "text": fact_text,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "source_urls": source_hints,
                }
                existing_fact_id = str(item.get("existing_fact_id") or "").strip()
                if existing_fact_id:
                    candidate["existing_fact_id"] = existing_fact_id[:200]
                candidate_facts.append(candidate)

            support_by_ref: dict[str, dict[str, Any]] = {}
            for source in available_sources:
                if not isinstance(source, dict):
                    continue
                url = str(source.get("url") or "").rstrip("/")
                for support in source.get("supports") or []:
                    if not isinstance(support, dict):
                        continue
                    evidence_ref = str(support.get("evidence_ref") or "").strip()
                    if evidence_ref:
                        support_by_ref[evidence_ref] = {
                            "source_url": url,
                            "support": support,
                        }

            try:
                bindings = await self._bind_facts_to_evidence(
                    key,
                    timeout,
                    candidate_facts,
                    available_sources,
                    model=model,
                    quota=quota,
                )
                binding_status = "bound"
            except (GeminiUnavailable, PermanentProviderError, MalformedProviderResponse):
                bindings = {}
                binding_status = "unavailable"

            normalized_facts: list[dict[str, Any]] = []
            for index, candidate in enumerate(candidate_facts):
                refs = [
                    ref for ref in bindings.get(index, [])
                    if ref in support_by_ref
                ]
                if not refs:
                    reject("no_bound_evidence")
                    continue
                source_urls: list[str] = []
                for evidence_ref in refs:
                    url = str(support_by_ref[evidence_ref]["source_url"])
                    if url and url not in source_urls:
                        source_urls.append(url)
                fact = {
                    **candidate,
                    "source_urls": source_urls,
                    "evidence_refs": refs,
                }
                normalized_facts.append(fact)

            native_continuation_batches = 0
            extraction_complete = not bool(payload.get("continuation_needed"))
            continuation_reason = str(payload.get("continuation_reason") or "").strip()
            if payload.get("continuation_needed"):
                continuation_config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=self.NATIVE_CONTINUATION_SCHEMA,
                )
                evidence_payload = [
                    {
                        "source_url": str(source.get("url") or "").rstrip("/"),
                        "title": str(source.get("title") or "")[:240],
                        "passages": [
                            {
                                "evidence_ref": str(support.get("evidence_ref") or ""),
                                "text": str(support.get("text") or "")[:600],
                            }
                            for support in (source.get("supports") or [])
                            if isinstance(support, dict)
                            and str(support.get("evidence_ref") or "").strip()
                            and str(support.get("text") or "").strip()
                        ],
                    }
                    for source in available_sources
                    if isinstance(source, dict)
                ]
                seen_outputs = {
                    hashlib.sha256(
                        json.dumps(
                            {
                                "claim_key": str(fact.get("claim_key") or ""),
                                "text": str(fact.get("text") or ""),
                                "evidence_refs": sorted(str(ref) for ref in (fact.get("evidence_refs") or [])),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    for fact in normalized_facts
                }
                already_returned = [
                    {
                        "claim_key": str(fact.get("claim_key") or "")[:300],
                        "text": str(fact.get("text") or "")[:500],
                    }
                    for fact in normalized_facts
                ]

                max_native_continuation_batches = 6
                for batch_index in range(max_native_continuation_batches):
                    continuation_prompt = (
                        "Ты внутренний LLM-экстрактор Street Story. Google Search уже выполнен; нового поиска НЕ делай. "
                        "Ниже дан полный набор grounding passages текущего search-call с opaque evidence_ref. "
                        "Верни до 32 НОВЫХ атомарных проверяемых facts, которых ещё нет в Already returned facts. "
                        "Для каждого fact обязательно укажи source_urls и evidence_refs только из Evidence. "
                        "URL без конкретного evidence_ref не является доказательством. "
                        "Если после текущего ответа в Evidence остаются ещё содержательные не возвращённые facts, "
                        "continuation_needed=true; иначе false. Не превращай отсутствие ответа в факт. "
                        "Не делай semantic dedup с внешними знаниями; не используй знания вне Evidence.\n\n"
                        "Coverage goal: " + str(topic_context.get("coverage_goal") or query)[:1600] + "\n"
                        "Search query: " + query[:1000] + "\n"
                        "Continuation batch index: " + str(batch_index) + "\n"
                        "Already returned facts: " + json.dumps(already_returned, ensure_ascii=False) + "\n"
                        "Evidence: " + json.dumps(evidence_payload, ensure_ascii=False)
                    )
                    try:
                        continuation_response = await self._generate(
                            key,
                            timeout,
                            [continuation_prompt],
                            continuation_config,
                            operation="grounded_research",
                            model=model,
                            quota=quota,
                        )
                        continuation_payload = json.loads(continuation_response.text or "{}")
                        if (
                            not isinstance(continuation_payload, dict)
                            or not isinstance(continuation_payload.get("facts"), list)
                            or not isinstance(continuation_payload.get("continuation_needed"), bool)
                            or not isinstance(continuation_payload.get("continuation_reason"), str)
                        ):
                            raise ValueError("malformed_native_continuation")
                    except (
                        GeminiUnavailable,
                        PermanentProviderError,
                        MalformedProviderResponse,
                        ValueError,
                        TypeError,
                        json.JSONDecodeError,
                    ) as exc:
                        extraction_complete = False
                        continuation_reason = type(exc).__name__
                        extraction_audit["rejected"]["native_continuation_error"] = (
                            int(extraction_audit["rejected"].get("native_continuation_error") or 0) + 1
                        )
                        break

                    new_batch: list[dict[str, Any]] = []
                    for raw in continuation_payload.get("facts") or []:
                        if not isinstance(raw, dict):
                            reject("continuation_invalid_shape")
                            continue
                        fact_text = " ".join(str(raw.get("text") or "").split()).strip()
                        if not fact_text:
                            reject("continuation_empty_text")
                            continue
                        if len(fact_text) > 1200:
                            reject("continuation_text_over_safety_bound")
                            continue
                        claim_key = " ".join(str(raw.get("claim_key") or "").split()).strip().casefold()
                        if not claim_key or len(claim_key) > 300:
                            claim_key = (
                                "exact-text:"
                                + hashlib.sha256(fact_text.casefold().encode("utf-8")).hexdigest()[:24]
                            )
                            extraction_audit["claim_key_fallback_count"] += 1
                        try:
                            confidence = float(raw.get("confidence", 0.0))
                            if not math.isfinite(confidence):
                                raise ValueError
                        except (TypeError, ValueError):
                            confidence = 0.0
                            extraction_audit["confidence_defaulted_count"] += 1

                        refs: list[str] = []
                        source_urls: list[str] = []
                        for raw_ref in raw.get("evidence_refs") or []:
                            evidence_ref = str(raw_ref or "").strip()
                            bound = support_by_ref.get(evidence_ref)
                            if bound is None or evidence_ref in refs:
                                continue
                            refs.append(evidence_ref)
                            source_url = str(bound["source_url"])
                            if source_url and source_url not in source_urls:
                                source_urls.append(source_url)
                        if not refs:
                            reject("continuation_no_bound_evidence")
                            continue

                        fact = {
                            "claim_key": claim_key,
                            "text": fact_text,
                            "confidence": max(0.0, min(1.0, confidence)),
                            "source_urls": source_urls,
                            "evidence_refs": refs,
                        }
                        existing_fact_id = str(raw.get("existing_fact_id") or "").strip()
                        if existing_fact_id:
                            fact["existing_fact_id"] = existing_fact_id[:200]
                        fingerprint = hashlib.sha256(
                            json.dumps(
                                {
                                    "claim_key": claim_key,
                                    "text": fact_text,
                                    "evidence_refs": sorted(refs),
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                        if fingerprint in seen_outputs:
                            reject("continuation_exact_duplicate")
                            continue
                        seen_outputs.add(fingerprint)
                        new_batch.append(fact)
                        already_returned.append({
                            "claim_key": claim_key[:300],
                            "text": fact_text[:500],
                        })

                    normalized_facts.extend(new_batch)
                    native_continuation_batches += 1
                    continuation_needed = bool(continuation_payload.get("continuation_needed"))
                    continuation_reason = str(
                        continuation_payload.get("continuation_reason") or ""
                    ).strip()
                    if not continuation_needed:
                        extraction_complete = True
                        break
                    if not new_batch:
                        extraction_complete = False
                        continuation_reason = continuation_reason or "continuation_stalled"
                        extraction_audit["rejected"]["native_continuation_stalled"] = (
                            int(extraction_audit["rejected"].get("native_continuation_stalled") or 0) + 1
                        )
                        break
                    if batch_index + 1 >= max_native_continuation_batches:
                        extraction_complete = False
                        continuation_reason = continuation_reason or "continuation_limit"
                        extraction_audit["rejected"]["native_continuation_limit"] = (
                            int(extraction_audit["rejected"].get("native_continuation_limit") or 0) + 1
                        )

            extraction_audit["accepted_fact_count"] = len(normalized_facts)
            extraction_audit["evidence_binding_status"] = binding_status
            extraction_audit["native_continuation_batches"] = native_continuation_batches
            payload["facts"] = normalized_facts
            payload["extraction_audit"] = extraction_audit
            payload["extraction_complete"] = extraction_complete
            payload["continuation_reason"] = continuation_reason
            try:
                coverage_review = await self._review_coverage_contract(
                    key,
                    timeout,
                    coverage_goal=str(topic_context.get("coverage_goal") or query)[:1600],
                    facts=normalized_facts,
                    sources=available_sources,
                    model=model,
                    quota=quota,
                    allow_page_reads=False,
                )
                payload = {**payload, **coverage_review}
            except (
                GeminiUnavailable,
                PermanentProviderError,
                MalformedProviderResponse,
                ValueError,
                TypeError,
                json.JSONDecodeError,
            ):
                payload = {
                    **payload,
                    "coverage_satisfied": False,
                    "coverage_items": [],
                    "missing_aspects": ["coverage_review_unavailable"],
                }

            blocked_official_hosts = {
                "wikipedia.org", "wikimedia.org", "openstreetmap.org", "google.com",
            }
            official_urls: list[str] = []
            for raw_url in payload.get("official_source_urls", []):
                normalized = str(raw_url).rstrip("/")
                host = (urlparse(normalized).hostname or "").lower().removeprefix("www.")
                blocked = any(host == domain or host.endswith("." + domain) for domain in blocked_official_hosts)
                if normalized in seen and not blocked and normalized not in official_urls:
                    official_urls.append(normalized)
            payload["official_source_urls"] = official_urls
            official_set = set(official_urls)
            decorated = [
                {
                    **source,
                    "type": (
                        "official"
                        if source["url"].rstrip("/") in official_set
                        else str(source.get("type") or "web")
                    ),
                }
                for source in available_sources
            ]
            return GroundedResearch(payload=payload, grounding_sources=decorated)

        retry_at: list[float] = []
        native_search_status = "exhausted"

        async def native_search_phase() -> GroundedResearch | None:
            for model, _pool, quota, executor in self.web_search_routes:
                async def routed_call(key, timeout, *, _model=model, _quota=quota):
                    return await call(key, timeout, model=_model, quota=_quota)

                try:
                    return await executor.execute("web_search", routed_call)
                except GeminiUnavailable as exc:
                    if exc.retry_at is not None:
                        retry_at.append(exc.retry_at)
                    continue
                except PermanentProviderError as exc:
                    if str(exc) == "gemini:unsupported_model":
                        continue
                    raise
            return None

        try:
            native_result = await asyncio.wait_for(
                native_search_phase(),
                timeout=max(0.01, float(self.NATIVE_WEB_SEARCH_PHASE_SECONDS)),
            )
        except TimeoutError:
            native_search_status = "timeout"
            native_result = None
        if native_result is not None:
            return native_result

        processed_urls = {
            str(item.get("url") or "").rstrip("/")
            for item in (topic_context.get("previously_processed_sources") or [])
            if isinstance(item, dict) and str(item.get("url") or "").startswith("https://")
        }
        try:
            discovery = await self._public_web_search(query, excluded_urls=processed_urls)
        except RetryableProviderError as exc:
            if cached_sources:
                discovery = GroundedResearch(
                    payload={
                        "summary": "Public discovery is temporarily unavailable; using persisted POI evidence.",
                        "official_source_urls": [],
                        "facts": [],
                        "search_provider": "poi_cache_fallback",
                        "public_search_status": "unavailable",
                        "public_search_error": type(exc).__name__,
                    },
                    grounding_sources=cached_sources,
                )
            elif retry_at:
                raise GeminiUnavailable(
                    min(retry_at),
                    "all_web_search_models_and_public_search_unavailable",
                )
            else:
                raise
        discovery = GroundedResearch(
            payload={
                **discovery.payload,
                "cached_source_count": len(cached_sources),
                "native_search_status": native_search_status,
            },
            grounding_sources=self._merge_evidence_sources(
                discovery.grounding_sources,
                cached_sources,
            ),
        )
        completed = await self._semantic_complete_discovery_best_effort(
            query,
            topic_context,
            discovery,
            timeout_seconds=self.SEMANTIC_DISCOVERY_COMPLETION_SECONDS,
        )
        if completed is not None:
            return completed
        # Fail open to the Live-owned semantic fallback. The evidence remains
        # exact and durable; no deterministic extractor is introduced.
        discovery.payload.update({
            "semantic_completion": "",
            "semantic_status": "live_model_required",
            "coverage_satisfied": False,
            "missing_aspects": ["semantic_model_temporarily_unavailable"],
        })
        return discovery


    async def research(
        self,
        photo_path: Path,
        photo_mime: str,
        transcript: str,
        place_context: dict[str, Any],
        wikipedia: list[dict[str, Any]],
        previous_facts: list[dict[str, Any]],
    ) -> GroundedResearch:
        from google.genai import types
        context = {
            "user_voice_intent": transcript,
            "place_context": place_context,
            "wikipedia": wikipedia,
            "previous_facts": previous_facts,
        }
        prompt = (
            "Ты исследователь Street Story. Используй предоставленный контекст и Google Search grounding. "
            "Не считай собственный текст доказательством. Верни компактные факты для пользовательского чек-листа, "
            "к каждому факту перечисли source_urls, реально поддерживающие этот факт. Не включай URL, который не видел. "
            "draft_text должен быть короткой публикацией, а не research dump. Structured context:\n"
            + json.dumps(context, ensure_ascii=False)
        )
        data = photo_path.read_bytes()
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            response_mime_type="application/json",
            response_json_schema=self.FACT_SCHEMA,
        )

        async def call(key, timeout, *, model=None, quota=None):
            response = await self._generate(
                key,
                timeout,
                [types.Part.from_bytes(data=data, mime_type=photo_mime), prompt],
                config,
                operation="grounded_research",
                model=model,
                quota=quota,
            )
            try:
                payload = json.loads(response.text or "{}")
                if not isinstance(payload, dict) or any(not isinstance(payload.get(k), str) for k in ("place_name", "summary", "draft_text")) or not isinstance(payload.get("facts"), list):
                    raise ValueError
                for fact in payload['facts']:
                    if not isinstance(fact, dict) or not isinstance(fact.get('text'), str) or not isinstance(fact.get('source_urls'), list):
                        raise ValueError
                    if any(not isinstance(url, str) for url in fact['source_urls']) or not math.isfinite(float(fact.get('confidence', 0))):
                        raise ValueError
            except (ValueError, TypeError):
                raise MalformedProviderResponse("gemini:malformed_research") from None
            sources: list[dict[str, str]] = []
            for candidate in getattr(response, "candidates", []) or []:
                metadata = getattr(candidate, "grounding_metadata", None)
                for chunk in getattr(metadata, "grounding_chunks", []) or []:
                    web = getattr(chunk, "web", None)
                    uri = getattr(web, "uri", None)
                    if isinstance(uri, str) and uri.startswith("https://"):
                        sources.append({"type": "web", "title": str(getattr(web, "title", "") or uri), "url": uri})
            return GroundedResearch(payload=payload, grounding_sources=list({s["url"]: s for s in sources}.values()))

        retry_at: list[float] = []
        for model, _pool, quota, executor in self.research_routes:
            async def routed_call(key, timeout, *, _model=model, _quota=quota):
                return await call(key, timeout, model=_model, quota=_quota)

            try:
                return await executor.execute("grounded_research", routed_call)
            except GeminiUnavailable as exc:
                if exc.retry_at is not None:
                    retry_at.append(exc.retry_at)
                continue
            except PermanentProviderError as exc:
                if str(exc) == "gemini:unsupported_model":
                    continue
                raise
        if retry_at:
            raise GeminiUnavailable(min(retry_at), "all_research_models_unavailable")
        raise PermanentProviderError("gemini:unsupported_model")


class VibePublishClient:
    """HTTP-only VibePublish integration. Never touches VibePublish storage or provider credentials."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self.http = http

    def configured(self) -> bool:
        return bool(self.settings.vibepublish_base_url and self.settings.vibepublish_bearer_token)

    async def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None, key: str | None = None) -> dict[str, Any]:
        if not self.configured():
            raise PermanentProviderError("VibePublish runtime is not configured")
        own = self.http is None
        client = self.http or httpx.AsyncClient(timeout=20)
        headers = {"Authorization": f"Bearer {reveal(self.settings.vibepublish_bearer_token)}", "Accept": "application/json"}
        if self.settings.vibepublish_http_host:
            headers["Host"] = self.settings.vibepublish_http_host
        if key:
            headers["Idempotency-Key"] = key
        try:
            response = await client.request(method, self.settings.vibepublish_base_url + path, json=json_body, headers=headers)
            if response.status_code >= 500 or response.status_code in {408, 425, 429}:
                raise RetryableProviderError(f"VibePublish HTTP {response.status_code}")
            response.raise_for_status()
            return response.json()
        except RetryableProviderError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableProviderError(f"VibePublish response outcome unknown: {exc}") from exc
        except (httpx.HTTPStatusError, ValueError) as exc:
            raise PermanentProviderError(f"VibePublish request failed: {exc}") from exc
        finally:
            if own:
                await client.aclose()

    async def bootstrap(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/bootstrap")

    async def publish(self, payload: dict[str, Any], request_key: str) -> dict[str, Any]:
        return await self._request("POST", "/v1/publications", json_body=payload, key=request_key)

    async def status(self, operation_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/operations/{operation_id}")

    @staticmethod
    def source_image_ingress_supported() -> bool:
        # Fresh-read VibePublish PR #1 @ 87be8fcf (2026-09-08): no authenticated HTTP image ingress.
        return False


def infer_provider(alias: str, label: str) -> str | None:
    text = f" {alias.lower().replace('_', ' ').replace('-', ' ')} {label.lower()} "
    if any(token in text for token in (" telegram ", " телеграм ", " tg ")):
        return "telegram"
    if any(token in text for token in (" vk ", " вк ", " vkontakte ", " вконтакте ")):
        return "vk"
    if any(token in text for token in (" max ", " макс ")):
        return "max"
    return None


def project_destinations(bootstrap: dict[str, Any]) -> list[dict[str, Any]]:
    caps = {
        c.get("destination"): c
        for c in bootstrap.get("capabilities", [])
        if c.get("operation") == "publish" and c.get("surface") == "post"
    }
    projected: list[dict[str, Any]] = []
    for item in bootstrap.get("destinations", []):
        if item.get("kind") != "destination":
            continue
        alias = str(item.get("alias", ""))
        label = str(item.get("label", ""))
        cap = caps.get(alias)
        if not cap:
            continue
        provider = infer_provider(alias, label)
        if provider is None:
            continue
        status = str(cap.get("status", "unsupported"))
        normalized = f"{alias} {label}".lower()
        is_primary = "полюбить калининград" in normalized and provider in {"telegram", "vk"}
        projected.append({
            "alias": alias,
            "label": label,
            "provider": provider,
            "status": status,
            "selected": bool(is_primary and status in {"supported", "needs_review"}),
        })
    return projected
