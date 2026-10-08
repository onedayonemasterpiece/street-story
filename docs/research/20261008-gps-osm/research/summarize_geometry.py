"""Offline audit tables over cached originals/OSM; no API or model calls."""
import csv
import json
import math
from pathlib import Path

from geometry_research import closest_segment,inside,in_polygons,bearing

ROOT=Path(__file__).resolve().parent.parent
TARGETS={104:'relation/3665416',105:'way/105154961',106:'way/150596899',
         107:'way/135507463',108:'relation/1383564',109:'relation/17986347',
         110:'way/1085593396',111:'way/95290265',112:'way/91256978'}


def boundary_distance(point,polygons):
    return min(closest_segment(point,a,b)[0] for p in polygons
               for ring in [p['outer'],*p['holes']] for a,b in zip(ring,ring[1:]))


def outer_components(candidate):
    provenance=[p for p in candidate['ring_provenance'] if p['role']=='outer']
    result=[]
    for i,p in enumerate(candidate['polygons_xy']):
        edges=[(closest_segment((0,0),a,b),j) for j,(a,b) in enumerate(zip(p['outer'],p['outer'][1:]))]
        (distance,nearest),edge=min(edges,key=lambda e:e[0][0])
        contained=inside((0,0),p['outer']) and not any(inside((0,0),h) for h in p['holes'])
        result.append({'component':i,'outer_member_way_ids':provenance[i]['member_way_ids'],
                       'boundary_distance_m':round(distance,2),
                       'polygon_distance_m':0 if contained else round(distance,2),
                       'camera_inside':contained,'nearest_edge':edge,
                       'nearest_boundary_bearing_deg':round(bearing(nearest),2)})
    return result


def sensitivity(data,target):
    index={f"{c['osm_type']}/{c['osm_id']}":c for c in data['buildings']}
    groups=data['building_groups'];candidate=index[target]
    # 49 fixed points on a 9x9 Cartesian grid clipped to the disk. They are
    # scenarios, not random draws or a probability distribution of GNSS error.
    lattice=[(i,j) for i in range(-4,5) for j in range(-4,5) if i*i+j*j<=16]
    assert len(lattice)==49
    result=[]
    for radius in (10,30,60):
        samples=[]
        for i,j in lattice:
            point=(i*radius/4,j*radius/4)
            distances={g['group_key']:boundary_distance(point,index[g['representative_ref']]['polygons_xy']) for g in groups}
            bbox={g['group_key']:math.dist(point,index[g['representative_ref']]['bbox_center_xy']) for g in groups}
            order_boundary=sorted(distances,key=lambda ref:(distances[ref],ref))
            order_bbox=sorted(bbox,key=lambda ref:(bbox[ref],ref))
            samples.append({'offset_xy_m':point,'rank_boundary':order_boundary.index(target)+1,
                            'rank_bbox_center':order_bbox.index(target)+1,
                            'boundary_distance_m':round(distances[target],2),
                            'bbox_center_distance_m':round(bbox[target],2),
                            'camera_inside_target':in_polygons(point,candidate['polygons_xy'])})
        row={'scenario_radius_m':radius,'positions':49,
             'boundary_rank_best':min(s['rank_boundary'] for s in samples),
             'boundary_rank_worst':max(s['rank_boundary'] for s in samples),
             'bbox_rank_best':min(s['rank_bbox_center'] for s in samples),
             'bbox_rank_worst':max(s['rank_bbox_center'] for s in samples),
             'boundary_distance_min_m':min(s['boundary_distance_m'] for s in samples),
             'boundary_distance_max_m':max(s['boundary_distance_m'] for s in samples),
             'camera_inside_target_positions':sum(s['camera_inside_target'] for s in samples),
             'boundary_top5_excluded_positions':sum(s['rank_boundary']>5 for s in samples),
             'boundary_top8_excluded_positions':sum(s['rank_boundary']>8 for s in samples),
             'bbox_top8_excluded_positions':sum(s['rank_bbox_center']>8 for s in samples),
             'boundary_100m_cutoff_excluded_positions':sum(s['boundary_distance_m']>100 for s in samples),
             'bbox_100m_cutoff_excluded_positions':sum(s['bbox_center_distance_m']>100 for s in samples),
             'samples':samples}
        result.append(row)
    return result


def write_csv(path,rows):
    fields=list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)


def main():
    manifest=json.loads((ROOT/'geo_originals/gps-manifest.json').read_text())
    matrix=[];detailed=[];sensitivity_rows=[]
    for item in manifest['items']:
        num=item['message_id'];data=json.loads((ROOT/f'research/osm/photo-{num}.geometry.json').read_text())
        width=item['width'];height=item['height']
        if item.get('orientation') in (5,6,7,8):width,height=height,width
        aspect=width/height;f35=item['exif'].get('FocalLengthIn35mmFilm')
        diagonal=math.hypot(36,24)
        width_eq=diagonal*aspect/math.sqrt(1+aspect*aspect)
        height_eq=diagonal/math.sqrt(1+aspect*aspect)
        hfov=math.degrees(2*math.atan(width_eq/(2*f35))) if f35 else None
        vfov=math.degrees(2*math.atan(height_eq/(2*f35))) if f35 else None
        row={'photo':num,'target':TARGETS.get(num),'identity_status':'parent_verified_target' if num in TARGETS else 'pending',
             'latitude':item['latitude'],'longitude':item['longitude'],
             'focal_length_mm':item['exif'].get('FocalLength'),'focal_35mm_mm':f35,
             'digital_zoom_tag':item['exif'].get('DigitalZoomRatio'),
             'approx_hfov_deg':round(hfov,2) if hfov else None,
             'approx_vfov_deg':round(vfov,2) if vfov else None,
             'gps_heading_available':item.get('gps_direction_available',False),
             'gps_error_available':item.get('gps_error_available',False),
             'complete_groups':len(data['building_groups']),'unresolved_buildings':len(data['unresolved_buildings'])}
        detail={'photo':num,'target':row['target'],'fov_method':'Approximate diagonal 35mm equivalence and EXIF Orientation; no second multiplication by DigitalZoomRatio. Unknown cropping/calibration keeps FOV advisory.'}
        if num in TARGETS:
            target=TARGETS[num];candidate=next(c for c in data['buildings'] if f"{c['osm_type']}/{c['osm_id']}"==target)
            group=next(g for g in data['building_groups'] if g['group_key']==candidate['group_key'])
            row.update({'rank_bbox_center':group['rank_group_bbox_center'],'rank_boundary':group['rank_group_boundary'],
                'bbox_center_distance_m':group['bbox_center_distance_m'],'whole_boundary_distance_m':group['boundary_distance_m'],
                'whole_polygon_distance_m':group['polygon_distance_m'],'camera_inside':candidate['camera_inside'],
                'nearest_boundary_bearing_deg':candidate['nearest_boundary_bearing_deg'],
                'centroid_bearing_deg':candidate['centroid_bearing_deg'],
                'grouped_parts':group['part_count'],'outer_components':len(candidate['polygons_xy'])})
            detail.update({'components':outer_components(candidate),'linked_addresses':candidate.get('linked_addresses',[]),
                'context_associations':candidate.get('context_associations',[]),
                'front_facing_edges_2d':candidate['front_facing_edges_2d'],
                'visible_facade_distance_m':None,
                'visible_facade_note':'Whole-footprint distances do not establish the distance to the photographed facade. Exact view/pose remains uncalibrated.'})
            if num==107:
                edge=next(e for e in candidate['front_facing_edges_2d'] if e['component']==0 and e['ring_role']=='outer' and e['segment']==0)
                theta=edge['midpoint_bearing_deg'];normal=edge['outward_normal_bearing_deg']
                from_facade=(theta+180)%360
                incidence=abs((from_facade-normal+180)%360-180)
                detail['northern_end_facade']={**edge,'distance_to_midpoint_m':round(math.hypot(*edge['midpoint_xy']),2),
                    'horizontal_incidence_deg':round(incidence,2),
                    'association':'North end beside confirmed boundary entrance 61; geometry agrees with parent-verified image match.'}
            if num==110:
                museum=next(c for c in data['buildings'] if c['osm_id']==1085593395 and c['osm_type']=='way')
                detail['historic_complex_context']={'source':'parent-established historic relationship; no automatic entity merge',
                    'member_refs':['way/1085593395','way/1085593396'],
                    'whole_complex_boundary_min_m':min(candidate['boundary_distance_m'],museum['boundary_distance_m']),
                    'pictured_arts_school_footprint_boundary_m':candidate['boundary_distance_m'],
                    'neighbor_museum_footprint_boundary_m':museum['boundary_distance_m']}
            if num==111:
                detail['visual_component_scope']={
                    'identity':'Dmitriya Donskogo 15, physical building independently reviewed against two downloaded exterior photographs.',
                    'matched_components':'White cylindrical upper storey, circular cornices, arched dormers, pink rounded body, balcony recess and adjacent glazing; second exterior photograph includes the address plate.',
                    'reference_limit':'The upper glazed lantern and spire are not visible in the exterior reference. Their complete shape is not independently matched.',
                    'view_geometry':'Original GPS lies southwest of the building. References show the side along the northern street. Camera heading and exact image pose are not measured.',
                    'mapped_facade_assignment':'The simplified OSM footprint has no separately mapped rounded tower. No particular footprint vertex or edge is established as the photographed tower centre.',
                    'foreground_candidate_ref':'way/132936482',
                    'foreground_limit':'Neighbour Don17/Greenwest is spatial context. Ground-plan ray overlap alone does not establish real3D visibility.'}
            detail['gps_sensitivity']=sensitivity(data,target)
            for scenario in detail['gps_sensitivity']:
                sensitivity_rows.append({'photo':num,'target':target,**{k:v for k,v in scenario.items() if k!='samples'}})
        matrix.append(row);detailed.append(detail)
    (ROOT/'research/target_geometry_summary.json').write_text(json.dumps({'matrix':matrix,'details':detailed,
        'distance_semantics':'Distances refer to original EXIF camera position and cached OSM ground-plan geometry, not calibrated physical range to a visible gable/turret.',
        'sensitivity_method':'49 deterministic quarter-radius Cartesian grid points within each 10/30/60 m disk; not probabilities; extrema apply only to sampled points and complete cached candidate groups.'},ensure_ascii=False,indent=2))
    write_csv(ROOT/'research/target_geometry_matrix.csv',matrix)
    write_csv(ROOT/'research/gps_sensitivity.csv',sensitivity_rows)
    print(json.dumps({'matrix':matrix,'gps_sensitivity':sensitivity_rows},ensure_ascii=False))


if __name__=='__main__':main()
