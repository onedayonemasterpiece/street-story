from __future__ import annotations

import json
import math
from typing import Any

import httpx

from .mvp_acceptance import MvpAcceptanceStreetStoryService
from .providers import PermanentProviderError, RetryableProviderError
from .service import canonical


class MvpLocationStreetStoryService(MvpAcceptanceStreetStoryService):
    """Voice-declared place fallback when the source photo has no EXIF GPS.

    The fallback never reads or invents the phone's current location. It extracts an
    explicitly spoken place/address, resolves that text through Nominatim, records the
    provenance, and then reuses the normal OSM/Wikipedia -> visual identity pipeline.
    """

    async def _extract_place_query(self, transcript: str) -> str:
        custom = getattr(self.providers.gemini, "extract_place_query", None)
        if callable(custom):
            return str(await custom(transcript) or "").strip()[:300]

        gemini = self.providers.gemini
        if not hasattr(gemini, "_generate") or not hasattr(gemini, "executor"):
            return ""
        from google.genai import types

        schema = {
            "type": "object",
            "properties": {"place_query": {"type": "string"}},
            "required": ["place_query"],
        }
        prompt = (
            "Извлеки только явно названное пользователем место, объект или адрес из голосовых заметок Street Story. "
            "Не угадывай город и не используй текущее местоположение устройства. Если явного названия/адреса нет, "
            "верни пустую строку. Для геопоиска сохрани город, если он был произнесён.\n\n"
            + transcript[:5000]
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=schema,
        )

        async def call(api_key, timeout):
            response = await gemini._generate(api_key, timeout, [prompt], config)
            try:
                payload = json.loads(response.text or "{}")
                value = payload.get("place_query")
                if not isinstance(value, str):
                    raise ValueError
            except (ValueError, TypeError, json.JSONDecodeError):
                raise PermanentProviderError("Gemini returned malformed place-query JSON") from None
            return value.strip()[:300]

        return await gemini.executor.execute("grounded_research", call)

    async def _resolve_place_query(self, query: str) -> dict[str, Any] | None:
        custom = getattr(self.providers.osm, "geocode", None)
        if callable(custom):
            value = await custom(query)
            return value if isinstance(value, dict) else None

        user_agent = str(getattr(self.providers.osm, "user_agent", "StreetStory/0.1"))
        own = getattr(self.providers.osm, "http", None) is None
        client = getattr(self.providers.osm, "http", None) or httpx.AsyncClient(
            timeout=20,
            headers={"User-Agent": user_agent},
        )
        try:
            response = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "format": "jsonv2",
                    "q": query,
                    "limit": 5,
                    "addressdetails": 1,
                    "namedetails": 1,
                },
                headers={"User-Agent": user_agent},
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Nominatim search result is not a list")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    lat = float(row.get("lat"))
                    lon = float(row.get("lon"))
                except (TypeError, ValueError):
                    continue
                if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                return {
                    "lat": lat,
                    "lon": lon,
                    "display_name": str(row.get("display_name") or query)[:500],
                    "osm_type": str(row.get("osm_type") or ""),
                    "osm_id": row.get("osm_id"),
                }
            return None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError) as exc:
            raise RetryableProviderError(f"Owner place geocoding failed: {exc}") from exc
        finally:
            if own:
                await client.aclose()

    async def _run_research(self, job: dict[str, Any]) -> None:
        payload = json.loads(job["payload_json"] or "{}")
        session_ids = [str(value) for value in payload.get("voice_session_ids", [])]
        live_transcript = str(payload.get("live_transcript") or "").strip()
        provenance: dict[str, Any] | None = None
        with self.store.connection() as db:
            story = dict(self._story_row(db, job["story_id"]))

        if story["latitude"] is None or story["longitude"] is None:
            if live_transcript:
                transcript = live_transcript[:12000]
            else:
                transcripts = [await self._transcribe_session(session_id) for session_id in session_ids]
                transcript = "\n\n".join(value.strip() for value in transcripts if value.strip())
            query = self.store.checkpoint_get(job["id"], "owner_place_query")
            if query is None:
                query = await self._extract_place_query(transcript)
                self.store.checkpoint_put(job["id"], "owner_place_query", query)
            query = str(query or "").strip()
            if query:
                resolved = self.store.checkpoint_get(job["id"], "owner_place_geocode")
                if resolved is None:
                    resolved = await self._resolve_place_query(query)
                    self.store.checkpoint_put(job["id"], "owner_place_geocode", resolved)
                if isinstance(resolved, dict):
                    lat = float(resolved["lat"])
                    lon = float(resolved["lon"])
                    provenance = {
                        "kind": "owner_voice_place_query",
                        "query": query,
                        "resolved_lat": lat,
                        "resolved_lon": lon,
                        "display_name": str(resolved.get("display_name") or query),
                        "not_device_current_location": True,
                    }
                    with self.store.tx() as db:
                        row = self._story_row(db, job["story_id"])
                        research = json.loads(row["research_json"] or "{}")
                        research["location_provenance"] = provenance
                        db.execute(
                            "UPDATE stories SET latitude=?,longitude=?,research_json=?,updated_at=? WHERE id=?",
                            (lat, lon, canonical(research), self.store.now(), job["story_id"]),
                        )

        await super()._run_research(job)

        if provenance is not None:
            with self.store.tx() as db:
                row = self._story_row(db, job["story_id"])
                research = json.loads(row["research_json"] or "{}")
                research["location_provenance"] = provenance
                db.execute(
                    "UPDATE stories SET research_json=?,updated_at=? WHERE id=?",
                    (canonical(research), self.store.now(), job["story_id"]),
                )

    def _story_repr(self, db, row) -> dict[str, Any]:
        result = super()._story_repr(db, row)
        research = json.loads(row["research_json"] or "{}")
        provenance = research.get("location_provenance")
        if isinstance(provenance, dict):
            result["location_provenance"] = provenance
        return result
