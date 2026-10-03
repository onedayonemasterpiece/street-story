from __future__ import annotations

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
    mark_chunk,
    persist_source_version,
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
        },
        "required": ["facts", "needs_context", "context_reason"],
    }

    COVERAGE_REVIEW_SCHEMA = {
        "type": "object",
        "properties": {
            "coverage_satisfied": {"type": "boolean"},
            "summary": {"type": "string"},
            "missing_aspects": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["coverage_satisfied", "summary", "missing_aspects"],
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
        ) -> str:
            return (
                "Ты внутренний LLM-экстрактор Street Story. Передан один chunk документа. "
                "Извлекай атомарные проверяемые facts ТОЛЬКО когда утверждение поддерживается текстом секции [core]. "
                "[context_before] и [context_after] разрешено использовать только для разрешения ссылок, имён и границ; "
                "не создавай факт, если его содержательная опора находится только в context. "
                "Не используй знания вне chunk. Не превращай отсутствие ответа в факт. "
                "Если core оборван так, что смысл нельзя надёжно определить даже с context, needs_context=true. "
                "Если core понятен, needs_context=false, даже когда в нём нет релевантных facts. "
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
                + "Chunk text:\n" + str(chunk.get("text") or "")
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
                            if run_id:
                                with self.store.tx() as db:
                                    mark_chunk(
                                        db,
                                        run_id=run_id,
                                        chunk_id=chunk["chunk_id"],
                                        status="extracting",
                                        observation_count=0,
                                        model_name=str(model or ""),
                                        prompt_version="page-chunk-extraction-v1",
                                        now=self.store.now(),
                                    )
                            try:
                                response = await self._generate(
                                    key,
                                    timeout,
                                    [
                                        build_chunk_prompt(
                                            requested_url,
                                            document["source_version_id"],
                                            chunk,
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
                                ):
                                    raise ValueError("malformed_chunk_extraction")
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
                                chunk_facts, chunk_official, chunk_audit = normalize_payload(
                                    {
                                        **chunk_payload,
                                        "official_source_urls": [],
                                    },
                                    evidence_chunks=evidence_chunks,
                                )
                                for fact in chunk_facts:
                                    refs: list[str] = []
                                    for span in fact.get("evidence_spans") or []:
                                        chunk_id = str(span.get("chunk_id") or "")
                                        if chunk_id and chunk_id not in refs:
                                            refs.append(chunk_id)
                                        support = {
                                            "kind": "verified_page_span",
                                            "source_url": span["source_url"],
                                            "source_version_id": span["source_version_id"],
                                            "evidence_ref": chunk_id,
                                            "chunk_id": chunk_id,
                                            "text": span["quote"],
                                            "span_start": span["span_start"],
                                            "span_end": span["span_end"],
                                        }
                                        if not any(
                                            isinstance(item, dict)
                                            and str(item.get("chunk_id") or "") == chunk_id
                                            and str(item.get("text") or "") == span["quote"]
                                            for item in supports
                                        ):
                                            supports.append(support)
                                    fact["evidence_refs"] = refs
                                page_facts.extend(chunk_facts)
                                for url in chunk_official:
                                    if url not in page_official:
                                        page_official.append(url)
                                for field in (
                                    "raw_fact_count",
                                    "accepted_fact_count",
                                    "claim_key_fallback_count",
                                    "confidence_defaulted_count",
                                ):
                                    aggregate_audit[field] += int(chunk_audit.get(field) or 0)
                                for reason, count in (chunk_audit.get("rejected") or {}).items():
                                    aggregate_audit["rejected"][reason] = (
                                        int(aggregate_audit["rejected"].get(reason) or 0)
                                        + int(count or 0)
                                    )
                                if chunk_payload["needs_context"]:
                                    status = "needs_context"
                                    page_chunk_needs_context += 1
                                elif chunk_facts:
                                    status = "extracted"
                                else:
                                    status = "no_claims"
                                if run_id:
                                    with self.store.tx() as db:
                                        mark_chunk(
                                            db,
                                            run_id=run_id,
                                            chunk_id=chunk["chunk_id"],
                                            status=status,
                                            observation_count=len(chunk_facts),
                                            model_name=str(model or ""),
                                            prompt_version="page-chunk-extraction-v1",
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
                                            observation_count=0,
                                            model_name=str(model or ""),
                                            prompt_version="page-chunk-extraction-v1",
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

                    coverage_config_final = types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_json_schema=self.COVERAGE_REVIEW_SCHEMA,
                    )
                    coverage_prompt = (
                        "Ты проверяешь полноту уже извлечённых facts относительно coverage goal. "
                        "Не добавляй новых фактов и не используй внешние знания. "
                        "coverage_satisfied=true только если facts прямо отвечают на цель. "
                        "missing_aspects перечисляет конкретные пробелы.\n\n"
                        + "Coverage goal: " + coverage_goal
                        + "\nFacts: "
                        + json.dumps(
                            [
                                {
                                    "claim_key": fact.get("claim_key"),
                                    "text": fact.get("text"),
                                    "source_urls": fact.get("source_urls"),
                                }
                                for fact in normalized_facts
                            ],
                            ensure_ascii=False,
                        )
                    )
                    try:
                        coverage_response = await self._generate(
                            key,
                            timeout,
                            [coverage_prompt],
                            coverage_config_final,
                            operation="grounded_research",
                            model=model,
                            quota=quota,
                        )
                        coverage_payload = json.loads(coverage_response.text or "{}")
                        if not isinstance(coverage_payload, dict):
                            raise ValueError("coverage_payload_shape")
                        payload = {
                            **payload,
                            "coverage_satisfied": bool(coverage_payload.get("coverage_satisfied")),
                            "summary": str(coverage_payload.get("summary") or payload.get("summary") or "")[:1200],
                            "missing_aspects": [
                                str(value)[:300]
                                for value in (coverage_payload.get("missing_aspects") or [])[:20]
                                if str(value).strip()
                            ],
                        }
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
                            "missing_aspects": ["coverage_review_unavailable"],
                        }

            return GroundedResearch(
                payload={
                    "summary": str(payload.get("summary") or "")[:1200],
                    "official_source_urls": official_urls,
                    "facts": normalized_facts,
                    "search_provider": str(discovery.payload.get("search_provider") or "duckduckgo_html_fallback"),
                    "semantic_completion": semantic_completion,
                    "coverage_satisfied": bool(payload.get("coverage_satisfied")),
                    "missing_aspects": [
                        str(value)[:300]
                        for value in (payload.get("missing_aspects") or [])[:20]
                        if str(value).strip()
                    ],
                    "page_chunk_count": page_chunk_count,
                    "page_chunk_failures": page_chunk_failures,
                    "page_chunk_needs_context": page_chunk_needs_context,
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

    async def detect_fact_conflicts(
        self,
        items: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Let the model select and classify actual conflicts across the bounded fact set."""
        model_items = conflict_scan_items(items)
        if len(model_items) < 2:
            return []
        from google.genai import types

        prompt = (
            "Ты внутренний арбитр фактов Street Story. Перед тобой ограниченный набор уже извлечённых "
            "моделью утверждений об одном POI. Самостоятельно найди только те пары, между которыми есть "
            "смысловое противоречие или важное расхождение; сервер НЕ отбирал пары по словам, датам или типам. "
            "Ссылайся только на существующие fact_id из входа через left_fact_id/right_fact_id. "
            "relation: contradiction — одновременно истинными в одном смысле быть не могут; "
            "scope_difference — различаются объект/период/область; temporal_sequence — разные этапы времени; "
            "source_disagreement — источники расходятся и нужна дополнительная проверка; uncertain — данных мало. "
            "Не возвращай эквивалентные или просто разные совместимые факты. suggested_resolution: prefer_left, "
            "prefer_right, both_valid или unresolved. Количество сайтов НЕ является голосованием за истину. "
            "Учитывай происхождение, период, первичность, supports и Regional Knowledge evidence. "
            "Если доказательств недостаточно — unresolved. Не выдумывай факты, ссылки или идентификаторы.\n\n"
            "Context: " + json.dumps(context or {}, ensure_ascii=False)[:4000] + "\n"
            "Facts: " + json.dumps(model_items, ensure_ascii=False)[:48000]
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
            return normalize_model_conflict_records(model_items, payload)

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
                    "facts": facts[:20],
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
            try:
                cached_result = await self._semantic_complete_discovery(
                    query,
                    topic_context,
                    cached_discovery,
                )
                if (
                    cached_result.payload.get("coverage_satisfied") is True
                    and bool(cached_result.payload.get("facts"))
                ):
                    cached_result.payload["cache_only"] = True
                    return cached_result
            except (GeminiUnavailable, PermanentProviderError, MalformedProviderResponse):
                # Cache evaluation must never make web discovery less available.
                pass

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
            "ставь первыми; сведения официального источника имеют приоритет. Если Current topic context содержит "
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
            response_json_schema=self.WEB_SEARCH_SCHEMA,
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

            extraction_audit["accepted_fact_count"] = len(normalized_facts)
            extraction_audit["evidence_binding_status"] = binding_status
            if len(raw_facts) > 32:
                extraction_audit["rejected"]["over_batch_limit"] = len(raw_facts) - 32
            payload["facts"] = normalized_facts
            payload["extraction_audit"] = extraction_audit

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
        processed_urls = {
            str(item.get("url") or "").rstrip("/")
            for item in (topic_context.get("previously_processed_sources") or [])
            if isinstance(item, dict) and str(item.get("url") or "").startswith("https://")
        }
        try:
            discovery = await self._public_web_search(query, excluded_urls=processed_urls)
        except RetryableProviderError:
            if retry_at:
                raise GeminiUnavailable(min(retry_at), "all_web_search_models_and_public_search_unavailable")
            raise
        discovery = GroundedResearch(
            payload={**discovery.payload, "cached_source_count": len(cached_sources)},
            grounding_sources=self._merge_evidence_sources(
                discovery.grounding_sources,
                cached_sources,
            ),
        )
        try:
            return await self._semantic_complete_discovery(query, topic_context, discovery)
        except (GeminiUnavailable, PermanentProviderError, MalformedProviderResponse):
            # Fail open to the old Live-owned semantic fallback. The evidence is
            # still exact and durable; no deterministic extractor is introduced.
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
