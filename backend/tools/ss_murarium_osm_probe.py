"""Read-only, bounded public OSM map evidence for the Murarium selection regression.

Run manually with the existing backend venv; never reads app databases or credentials.
"""
from __future__ import annotations

import asyncio
import json
import math

import httpx

CENTER_LAT, CENTER_LON = 54.95980, 20.48248
BBOX = "20.48200,54.95950,20.48292,54.96006"


async def main() -> None:
    async with httpx.AsyncClient(timeout=18, headers={
        "User-Agent": "StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story)"
    }) as client:
        response = await client.get("https://api.openstreetmap.org/api/0.6/map.json",
                                    params={"bbox": BBOX})
    print("osm_api_status", response.status_code)
    response.raise_for_status()
    elements = response.json()["elements"]
    nodes = {int(e["id"]): e for e in elements if e.get("type") == "node" and "lat" in e}
    rows = []
    for e in elements:
        if e.get("type") not in {"way", "relation"}:
            continue
        tags = e.get("tags") or {}
        refs = e.get("nodes") or []
        points = [(nodes[n]["lat"], nodes[n]["lon"]) for n in refs if n in nodes]
        if len(points) < 4 or len(points) != len(refs):
            continue
        center = (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
        meters = math.hypot((center[0]-CENTER_LAT)*111_320, (center[1]-CENTER_LON)*111_320*.574)
        if meters > 85:
            continue
        selected_tags = {k: str(v)[:100] for k, v in tags.items() if k in {
            "name", "name:ru", "building", "building:part", "man_made", "tourism", "historic",
            "addr:housenumber", "addr:street", "wikidata", "heritage", "description",
        }}
        rows.append(dict(id=f"osm:{e['type']}:{e['id']}", meters=round(meters,1),
                         vertices=len(points), closed=bool(refs[0] == refs[-1]),
                         bounds=[round(min(p[0] for p in points),7), round(min(p[1] for p in points),7),
                                 round(max(p[0] for p in points),7), round(max(p[1] for p in points),7)],
                         tags=selected_tags, geometry=points if e.get('id') in {133724496,382545007} else None))
    print(json.dumps(sorted(rows,key=lambda x:x["meters"])[:45],ensure_ascii=False,indent=2))


if __name__ == "__main__":
    asyncio.run(main())
