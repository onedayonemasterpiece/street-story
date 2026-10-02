from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from .config import Settings, reveal
from .db import Store
from .fact_conflicts import conflict_scan_items, normalize_model_conflict_records


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
        if url and title and len(self.results) < 8:
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
            }, "required": ["claim_key", "text", "confidence", "source_urls"]}},
        },
        "required": ["summary", "official_source_urls", "facts"],
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

    async def _public_web_search(self, query: str) -> GroundedResearch:
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
            # Keeping them out of facts prevents article titles/descriptions from
            # leaking into the editorial checklist when Google grounding is exhausted.
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
            summary_lines = [
                f"{index}. {item['title']}: {item['snippet']}"
                for index, item in enumerate(results[:6], start=1)
                if item["snippet"]
            ]
            return GroundedResearch(
                payload={
                    "summary": (
                        "Google Search grounding сейчас недоступен. Ниже поисковые сниппеты "
                        "из независимой веб-выдачи; используй их как источник для проверки, "
                        "а не как автоматически доказанные утверждения.\n"
                        + "\n".join(summary_lines)
                    )[:6000],
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
        prompt = (
            "Ты внутренний поисковый инструмент Street Story, а не собеседник. "
            "Используй Google Search grounding только для запроса пользователя. "
            "Сначала обязательно попробуй найти официальный источник объекта или организации, если он существует: "
            "сайт владельца, музея, учреждения, муниципалитета или оператора. Wikipedia, СМИ, агрегатор и "
            "туристический каталог официальным источником не являются. Верни реально найденные официальные URL "
            "в official_source_urls. Верни до 12 проверяемых ФАКТОВ, а не список источников. Каждый fact.text — "
            "один атомарный тезис до 160 знаков: дата, человек, архитектор, событие, функция, реконструкция, "
            "посещение или другой конкретный факт. Без вводных вроде «источник сообщает», без URL и без нескольких "
            "разных утверждений в одном пункте. Для каждого факта обязательно задай claim_key — короткую устойчивую "
            "семантическую идентичность смысла, не зависящую от перефразирования. Если новый найденный тезис семантически "
            "совпадает с known_facts, укажи его точный fact_id в existing_fact_id; иначе existing_fact_id оставь пустым. "
            "Не выдумывай existing_fact_id. Для новых тезисов используй устойчивый claim_key. Самые важные факты "
            "ставь первыми; сведения официального источника имеют приоритет. Если Current topic context содержит "
            "previously_considered_poi_facts, не повторяй их "
            "без явной просьбы пользователя повторить или перепроверить: ищи новую фактологию. Для каждого факта "
            "укажи только source_urls, которые реально видел в grounding. Не пиши публикацию и не предлагай редактуру.\n\n"
            "Search query: " + query[:1000] + "\n"
            "Current topic context: " + json.dumps(topic_context, ensure_ascii=False)[:12000]
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
                for fact in payload["facts"]:
                    if (
                        not isinstance(fact, dict)
                        or not isinstance(fact.get("claim_key"), str)
                        or (
                            fact.get("existing_fact_id") is not None
                            and not isinstance(fact.get("existing_fact_id"), str)
                        )
                        or not isinstance(fact.get("text"), str)
                        or not isinstance(fact.get("source_urls"), list)
                        or any(not isinstance(url, str) for url in fact["source_urls"])
                        or not math.isfinite(float(fact.get("confidence", 0)))
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
            seen = {source["url"].rstrip("/") for source in unique_sources}
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
                    "type": "official" if source["url"].rstrip("/") in official_set else source["type"],
                    **(
                        {"supports": supports_by_url.get(source["url"], [])[:4]}
                        if supports_by_url.get(source["url"])
                        else {}
                    ),
                }
                for source in unique_sources
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
        try:
            return await self._public_web_search(query)
        except RetryableProviderError:
            if retry_at:
                raise GeminiUnavailable(min(retry_at), "all_web_search_models_and_public_search_unavailable")
            raise


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
