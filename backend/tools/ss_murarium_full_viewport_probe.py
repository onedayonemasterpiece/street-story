"""Read-only whole viewport inventory using the same public OSM Map API as Street Story."""
import asyncio, json, math, httpx
async def main():
  lat,lon,radius=54.95979,20.48254,350
  dlat=math.degrees(radius/6371000)*1.001
  dlon=dlat/max(.000001,abs(math.cos(math.radians(lat))))
  bbox=",".join(f"{x:.7f}" for x in (lon-dlon,lat-dlat,lon+dlon,lat+dlat))
  async with httpx.AsyncClient(timeout=29,headers={"User-Agent":"StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story)"}) as client:
    r=await client.get("https://api.openstreetmap.org/api/0.6/map.json",params={"bbox":bbox})
  print("status",r.status_code,"size",len(r.content))
  r.raise_for_status()
  elems=r.json()["elements"]
  node_ids={e["id"] for e in elems if e["type"]=="node"}
  ways=[e for e in elems if e["type"]=="way" and (e.get("tags") or {}).get("building") not in {None,"","no"}]
  full=[e for e in ways if len(e.get("nodes") or [])>=4 and (e["nodes"][0]==e["nodes"][-1]) and all(x in node_ids for x in e["nodes"])]
  idseq=[f"osm:way:{e['id']}" for e in full]
  target="osm:way:133724496"
  print(json.dumps({"elements":len(elems),"buildings":len(ways),"complete_closed_buildings":len(full),"target_index":idseq.index(target)+1 if target in idseq else None,
   "target_within_250":target in idseq[:250],"bbox":bbox,"first_ids":idseq[:3],
   "target_full_geometry_nodes":len(next(e for e in full if e["id"]==133724496)["nodes"]) if target in idseq else 0},ensure_ascii=False))
if __name__=="__main__":asyncio.run(main())
