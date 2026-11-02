"""Bounded, read-only full owner-map probe around the actual Murarium OSM way."""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from street_story.providers import OSMClient
from street_story.place_selection import _candidate, _geometry
from street_story.identity_map_context import expand_disjoint_building_components

async def main():
    lat, lon, radius = 54.95979, 20.48254, 350
    osm = OSMClient(None, "StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story)")
    async with httpx.AsyncClient(timeout=27) as client:
        raw=await osm._map_nearby(client,lat,lon,radius)
    buildings = []
    total = 0
    target = None
    for item in expand_disjoint_building_components(raw.get("elements") or []):
        tags=item.get("tags") or {}
        if tags.get("building") in {None, "", "no"} or item.get("identity_eligible") is False:
            continue
        c = _candidate(item)
        geom=_geometry(c)
        if not geom: continue
        total+=1
        cid=c["candidate_id"]
        if cid=="osm:way:133724496":
            target={"id":cid,"name":c["name"],"geometry":geom,"ordinal":total,"tags":tags}
        if len(buildings)<250: buildings.append(cid)
    print(json.dumps({"total":total,"retained":len(buildings),"target_seen":target is not None,
        "target_in_first_250": target is not None and target["id"] in buildings,
        "target_ordinal": target["ordinal"] if target else None,
        "target_polygon":target["geometry"]["coordinates"][0] if target else None,
        "incomplete_buildings":raw.get("incomplete_building_ids") or []},ensure_ascii=False))
if __name__=="__main__":
    asyncio.run(main())
