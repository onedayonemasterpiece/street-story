from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .db import Store


class RetryableProviderError(RuntimeError):
    pass


class PermanentProviderError(RuntimeError):
    pass


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
        key = _stable_cache_key("osm", [round(lat, 6), round(lon, 6)])
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
            query = f"""[out:json][timeout:12];(nwr(around:250,{lat:.6f},{lon:.6f})[name];nwr(around:250,{lat:.6f},{lon:.6f})[historic];nwr(around:250,{lat:.6f},{lon:.6f})[tourism];);out center tags 20;"""
            nearby_response = await client.post(
                self.overpass_url,
                content=query.encode(),
                headers={"User-Agent": self.user_agent, "Content-Type": "text/plain; charset=utf-8"},
            )
            nearby_response.raise_for_status()
            nearby = nearby_response.json().get("elements", [])[:20]
            result = {"reverse": reverse, "nearby": nearby}
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
        key = _stable_cache_key("wikipedia", [round(lat, 6), round(lon, 6)])
        cached = self.store.cache_get(key)
        if cached is not None:
            return cached
        own = self.http is None
        client = self.http or httpx.AsyncClient(timeout=20, headers={"User-Agent": "StreetStory/0.1"})
        try:
            geo = await client.get(self.endpoint, params={
                "action": "query", "list": "geosearch", "gscoord": f"{lat}|{lon}", "gsradius": 750,
                "gslimit": 6, "format": "json", "formatversion": 2,
            })
            geo.raise_for_status()
            hits = geo.json().get("query", {}).get("geosearch", [])[:6]
            if not hits:
                self.store.cache_put(key, [], 24 * 3600)
                return []
            ids = "|".join(str(hit["pageid"]) for hit in hits)
            extracts = await client.get(self.endpoint, params={
                "action": "query", "pageids": ids, "prop": "extracts|info", "exintro": 1,
                "explaintext": 1, "inprop": "url", "format": "json", "formatversion": 2,
            })
            extracts.raise_for_status()
            pages = extracts.json().get("query", {}).get("pages", [])
            result = [{
                "pageid": page.get("pageid"), "title": page.get("title", ""),
                "extract": page.get("extract", "")[:6000],
                "url": page.get("fullurl") or f"https://ru.wikipedia.org/wiki/{quote(page.get('title', '').replace(' ', '_'))}",
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


class GeminiClient:
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

    def __init__(self, settings: Settings):
        self.settings = settings

    def _client_and_types(self):
        if not self.settings.gemini_api_key:
            raise PermanentProviderError("GEMINI_API_KEY is not configured")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise PermanentProviderError("google-genai is not installed") from exc
        return genai.Client(api_key=self.settings.gemini_api_key), types

    async def transcribe(self, path: Path, mime_type: str) -> str:
        client, types = self._client_and_types()
        data = path.read_bytes()
        prompt = (
            "Точно транскрибируй русскую голосовую заметку Street Story. Не выдумывай факты, "
            "не резюмируй, сохрани смысл, имена собственные и вопросы пользователя. Верни только транскрипт."
        )
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=self.settings.gemini_model,
                contents=[types.Part.from_bytes(data=data, mime_type=mime_type), prompt],
            )
            return (response.text or "").strip()
        except PermanentProviderError:
            raise
        except Exception as exc:
            raise RetryableProviderError(f"Gemini transcription failed: {exc}") from exc

    async def research(
        self,
        photo_path: Path,
        photo_mime: str,
        transcript: str,
        place_context: dict[str, Any],
        wikipedia: list[dict[str, Any]],
        previous_facts: list[dict[str, Any]],
    ) -> GroundedResearch:
        client, types = self._client_and_types()
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
        try:
            config = types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                response_mime_type="application/json",
                response_json_schema=self.FACT_SCHEMA,
            )
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=self.settings.gemini_model,
                contents=[types.Part.from_bytes(data=photo_path.read_bytes(), mime_type=photo_mime), prompt],
                config=config,
            )
            payload = json.loads(response.text or "{}")
            sources: list[dict[str, str]] = []
            for candidate in getattr(response, "candidates", []) or []:
                metadata = getattr(candidate, "grounding_metadata", None)
                for chunk in getattr(metadata, "grounding_chunks", []) or []:
                    web = getattr(chunk, "web", None)
                    uri = getattr(web, "uri", None)
                    if isinstance(uri, str) and uri.startswith("https://"):
                        sources.append({"type": "web", "title": str(getattr(web, "title", "") or uri), "url": uri})
            return GroundedResearch(payload=payload, grounding_sources=list({s["url"]: s for s in sources}.values()))
        except PermanentProviderError:
            raise
        except (ValueError, TypeError) as exc:
            raise RetryableProviderError(f"Gemini returned malformed research: {exc}") from exc
        except Exception as exc:
            raise RetryableProviderError(f"Gemini grounded research failed: {exc}") from exc


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
        headers = {"Authorization": f"Bearer {self.settings.vibepublish_bearer_token}", "Accept": "application/json"}
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
