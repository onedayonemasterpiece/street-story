"""Small bidirectional road-axis evidence from the ACTUAL received OSM map.

Deterministically select only a few road AXES near the supplied camera point,
never buildings. For both orientations show observed 2D building-ray hits so
the image-using LLM may decide the viewing direction from SOURCE, not an
invented GPS compass or a hard nearest-building rule.
"""
from __future__ import annotations

import math


def observed_bidirectional_road_axes(story, candidates, manifest, *, max_roads=3,
                                     max_hits=3):
    from .identity_scene import scene_entries
    from .identity_map_context import osm_geometry_context
    from .identity_spatial_features import _distance, _local, _point, _bearing, measure_spatial_relations

    camera = manifest.get('camera') or {}
    basis = camera.get('position_status')
    policy = ('Nearest observed OSM ROAD AXES to the supplied camera position; both '
        'headings are conditional hypotheses, not measured yaw. Ray intersections '
        'are 2D plan-only across the received map, not proof of camera view, '
        'physical street connectivity, 3D visibility or absence of other buildings. '
        'A farther target remains eligible even if absent from the first hits. '
        'This never identifies/ranks/filters physical building candidates.')
    empty = {'camera_basis':basis, 'columns':['road_candidate_id','road_name','line_index',
        'segment_index','camera_axis_distance_m','axis_length_m','directions','highway_type'],
        'direction_columns':['heading_deg','first_hit_physical_bodies'],
        'hit_columns':['label','candidate_id','plan_ray_distance_m'],
        'rows':[], 'policy':policy, 'observed_road_count':0, 'omitted_road_count':0}
    if basis not in {'original_exif','owner_approximate'}:
        return empty
    origin = _point(camera)
    if origin is None:
        return empty
    received = scene_entries(story,candidates)
    label_columns=(manifest.get('objects') or {}).get('columns') or []
    labels={dict(zip(label_columns,row)).get('candidate_id'):
        dict(zip(label_columns,row)).get('label') for row in
        (manifest.get('objects') or {}).get('rows') or []}
    observed_roads=[]
    for entry in received:
        cid=entry.get('candidate_id')
        tags={**(entry.get('tags') or {}),**(entry.get('map_object') or {}).get('tags',{})}
        if (not isinstance(cid,str) or not cid.startswith('osm:way:')
                or not tags.get('highway')):
            continue
        geometry=entry.get('map_geometry') or osm_geometry_context(entry)
        nearest=None
        for line_index,line in enumerate(geometry.get('lines') or []):
            for segment_index,(first,second) in enumerate(zip(line,line[1:])):
                a,b = _point(first),_point(second)
                if a is None or b is None:
                    continue
                start,end=_local(a,origin),_local(b,origin)
                length=math.dist(start,end)
                if not 0.25 <= length <= 1000:
                    continue
                distance=_distance((0.,0.),start,end)
                bearing=_bearing((end[0]-start[0],end[1]-start[1]))
                record=(distance,cid,line_index,segment_index,length,bearing,tags.get('name'),tags.get('highway'))
                if nearest is None or record[:4]<nearest[:4]:
                    nearest=record
        if nearest is not None:
            observed_roads.append(nearest)
    observed_roads.sort(key=lambda r:r[:4])
    road_count=len(observed_roads)
    chosen=observed_roads[:max_roads]
    # Sidewalk/service paths near the camera may obscure the existence of the
    # actual named approach street. Include one nearby named OSM road when all
    # closest cues are unnamed; this changes road-axis presentation only and
    # cannot nominate/limit any building in the full original map.
    if chosen and not any(row[6] for row in chosen):
        named=next((row for row in observed_roads
            if row[6] and row[0]<=100),None)
        if named is not None and named not in chosen:
            chosen=sorted([*chosen[:max_roads-1],named],key=lambda row:row[:4])
    rows=[]
    for distance,cid,li,si,length,angle,name,highway in chosen:
        axis={'candidate_id':cid,'kind':'road_axis','line_index':li,'segment_index':si}
        directions=[]
        for heading in (angle,(angle+180.)%360):
            measured=measure_spatial_relations(story,candidates,[cid],
                pose={'east_m':0.,'north_m':0.,'heading_degrees':heading},
                front_segments=[],street_axis=axis)
            hits=(measured or {}).get('street_axis_ray_intersections') or []
            directions.append([round(heading,1),[
                [labels.get(hit['candidate_id']),hit['candidate_id'],hit['ray_distance_m']]
                for hit in hits[:max_hits]]])
        rows.append([cid,str(name or '')[:80],li,si,round(distance,2),
                     round(length,2),directions,str(highway or '')[:40]])
    return {**empty,'rows':rows,'observed_road_count':road_count,
        'omitted_road_count':max(0,road_count-len(rows))}
