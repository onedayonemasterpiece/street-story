"""Bounded, cached OSM geometry research. No model/provider calls.

Coordinates are projected to a local tangent plane in metres. This is sufficient
for the sub-kilometre research windows; these diagnostics do not assert identity.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.request
import xml.etree.ElementTree as ET

R = 6371008.8

def xy(lon, lat, lon0, lat0):
    return (R * math.radians(lon-lon0) * math.cos(math.radians(lat0)),
            R * math.radians(lat-lat0))

def bearing(p):
    return math.degrees(math.atan2(p[0], p[1])) % 360

def closest_segment(p, a, b):
    dx,dy=b[0]-a[0],b[1]-a[1]
    q=dx*dx+dy*dy
    t=max(0.,min(1.,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/q)) if q else 0.
    c=(a[0]+t*dx,a[1]+t*dy)
    return math.hypot(p[0]-c[0],p[1]-c[1]),c

def inside(p, ring):
    result=False
    for a,b in zip(ring,ring[1:]):
        if ((a[1]>p[1]) != (b[1]>p[1])) and p[0] < (b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0]:
            result=not result
    return result

def circular_interval(points):
    angles=sorted(bearing(p) for p in points if math.hypot(*p)>1e-8)
    if not angles:return None
    gaps=[((angles[(i+1)%len(angles)]-a)%360,i) for i,a in enumerate(angles)]
    gap,i=max(gaps)
    start=angles[(i+1)%len(angles)]
    width=360-gap if len(angles)>1 else 0
    return {"start_deg":round(start,2),"end_deg":round(angles[i],2),"width_deg":round(width,2),"wraps_north":start>angles[i]}

def centroid(ring):
    cross=[a[0]*b[1]-b[0]*a[1] for a,b in zip(ring,ring[1:])]
    area2=sum(cross)
    if abs(area2)<1e-9:
        return (sum(p[0] for p in ring)/len(ring),sum(p[1] for p in ring)/len(ring)),0
    return (sum((a[0]+b[0])*c for a,b,c in zip(ring,ring[1:],cross))/(3*area2),
            sum((a[1]+b[1])*c for a,b,c in zip(ring,ring[1:],cross))/(3*area2)),abs(area2)/2

def segments(ring):
    result=[]
    for i,(a,b) in enumerate(zip(ring,ring[1:])):
        length=math.dist(a,b)
        if length<1:continue
        d,c=closest_segment((0,0),a,b)
        result.append({"segment":i,"length_m":round(length,2),"distance_m":round(d,2),"nearest_bearing_deg":round(bearing(c),2),"axis_deg_mod180":round(bearing((b[0]-a[0],b[1]-a[1]))%180,2),"a":a,"b":b})
    return sorted(result,key=lambda x:x["distance_m"])

def on_ring(p, ring, tolerance=0.002):
    """Include shared OSM boundary nodes without inventing a spatial buffer."""
    return any(closest_segment(p, a, b)[0] <= tolerance
               for a, b in zip(ring, ring[1:]))

def covers_ring(p, ring):
    return inside(p, ring) or on_ring(p, ring)

def in_polygons(p, polygons):
    return any(inside(p, poly['outer']) and
               not any(inside(p, hole) for hole in poly['holes'])
               for poly in polygons)

def ray_crosses_filled_area(p,q,polygons):
    """Exact 2-D interval test along a finite line; it says nothing about height."""
    dx=q[0]-p[0];dy=q[1]-p[1];length2=dx*dx+dy*dy
    if length2<1e-12:return False
    events=[0.,1.]
    for poly in polygons:
        for ring in [poly['outer'],*poly['holes']]:
            for a,b in zip(ring,ring[1:]):
                ex=b[0]-a[0];ey=b[1]-a[1]
                ax=a[0]-p[0];ay=a[1]-p[1]
                den=dx*ey-dy*ex
                if abs(den)>1e-10:
                    t=(ax*ey-ay*ex)/den;u=(ax*dy-ay*dx)/den
                    if 0<t<1 and -1e-9<=u<=1+1e-9:events.append(t)
                elif abs(ax*dy-ay*dx)<1e-8:
                    for v in (a,b):
                        t=((v[0]-p[0])*dx+(v[1]-p[1])*dy)/length2
                        if 0<t<1:events.append(t)
    events=sorted(set(round(t,12) for t in events))
    for a,b in zip(events,events[1:]):
        if b-a<1e-10:continue
        t=(a+b)/2
        sample=(p[0]+t*dx,p[1]+t*dy)
        if in_polygons(sample,polygons) and not any(on_ring(sample,r) for poly in polygons for r in [poly['outer'],*poly['holes']]):
            return True
    return False

def facing_edges(polygons):
    result=[]
    for pi,poly in enumerate(polygons):
        for ri,ring in enumerate([poly['outer'],*poly['holes']]):
            area2=sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(ring,ring[1:]))
            outward=(1 if area2>0 else -1)*(1 if ri==0 else -1)
            for edge in segments(ring):
                a=edge['a'];b=edge['b'];length=math.dist(a,b)
                midpoint=((a[0]+b[0])/2,(a[1]+b[1])/2)
                normal=(outward*(b[1]-a[1])/length,-outward*(b[0]-a[0])/length)
                facing=-(normal[0]*midpoint[0]+normal[1]*midpoint[1])
                if facing<=0.002:continue
                result.append({**edge,'component':pi,'ring_role':'outer' if ri==0 else 'inner',
                    'ring_index':ri,'midpoint_xy':midpoint,
                    'midpoint_bearing_deg':round(bearing(midpoint),2),
                    'outward_normal_bearing_deg':round(bearing(normal),2),
                    'self_occluded_in_2d':ray_crosses_filled_area((0,0),midpoint,polygons)})
    return sorted(result,key=lambda e:e['distance_m'])

def stitch_rings(members, ways, xy_nodes, role):
    """Stitch member ways by node IDs, accepting either way direction.

    Missing data and non-unique endpoint topology stay explicit. In particular,
    an open chain is never closed with a made-up straight segment. Closed rings
    already represented by one way can share nodes with other closed rings.
    """
    errors=[]; warnings=[]; pieces={}; rings=[]; seen=set()
    for member in members:
        if member.get('role') != role:
            continue
        ref=int(member['ref'])
        if member.get('type') != 'way':
            errors.append({'kind':'unsupported_member_type','role':role,
                           'type':member.get('type'),'ref':ref})
            continue
        if ref in seen:
            warnings.append({'kind':'duplicate_member','role':role,'way_id':ref})
            continue
        seen.add(ref)
        if ref not in ways:
            errors.append({'kind':'missing_way','role':role,'way_id':ref})
            continue
        ids=ways[ref]['nodes']
        missing=sorted({i for i in ids if i not in xy_nodes})
        if missing:
            errors.append({'kind':'missing_nodes','role':role,'way_id':ref,'node_ids':missing})
            continue
        if len(ids)<2:
            errors.append({'kind':'empty_way','role':role,'way_id':ref})
            continue
        if ids[0]==ids[-1]:
            if len(ids)>=4 and len(set(ids[:-1]))==len(ids)-1:
                rings.append({'node_ids':ids,'member_way_ids':[ref]})
            else:
                errors.append({'kind':'degenerate_or_repeated_ring_nodes','role':role,'way_id':ref})
        else:
            pieces[ref]=ids

    # Every endpoint of an unambiguous cycle has exactly two incident pieces.
    # Degree 1 means an open chain; degree >2 needs topology disambiguation.
    endpoint_ways={}
    for ref, ids in pieces.items():
        for endpoint in (ids[0],ids[-1]):
            endpoint_ways.setdefault(endpoint,[]).append(ref)
    unused=set(pieces)
    while unused:
        seed=min(unused); component={seed}; pending=[seed]
        while pending:
            ref=pending.pop()
            for endpoint in (pieces[ref][0],pieces[ref][-1]):
                for neighbor in endpoint_ways[endpoint]:
                    if neighbor not in component:
                        component.add(neighbor);pending.append(neighbor)
        unused.difference_update(component)
        bad_endpoints={str(n):len(refs) for n,refs in endpoint_ways.items()
                       if any(r in component for r in refs) and len(refs)!=2}
        if bad_endpoints:
            errors.append({'kind':'open_or_ambiguous_endpoint_graph','role':role,
                           'member_way_ids':sorted(component),'endpoint_degrees':bad_endpoints})
            continue
        chain=list(pieces[seed]);used=[seed];remaining=component-{seed}
        while remaining:
            choices=[ref for ref in endpoint_ways[chain[-1]] if ref in remaining]
            if len(choices)!=1:
                break
            ref=choices[0];ids=pieces[ref]
            if ids[0]!=chain[-1]:ids=list(reversed(ids))
            chain.extend(ids[1:]);used.append(ref);remaining.remove(ref)
        if remaining or chain[0]!=chain[-1] or len(chain)<4 or len(set(chain[:-1]))!=len(chain)-1:
            errors.append({'kind':'unresolved_ring','role':role,'member_way_ids':sorted(component)})
        else:
            rings.append({'node_ids':chain,'member_way_ids':used})
    return rings,errors,warnings

def ring_samples(ring):
    """Vertices plus interior edge samples for conservative containment grouping."""
    points=list(ring[:-1])
    for a,b in zip(ring,ring[1:]):
        for t in (0.25,0.5,0.75):
            points.append((a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])))
    return points

def assemble_multipolygon(members, ways, xy_nodes, lonlat_nodes):
    errors=[];warnings=[];by_role={}
    for role in ('outer','inner'):
        rings,role_errors,role_warnings=stitch_rings(members,ways,xy_nodes,role)
        errors.extend(role_errors);warnings.extend(role_warnings)
        by_role[role]=rings
    for member in members:
        if member.get('role','') not in ('outer','inner'):
            errors.append({'kind':'unresolved_member_role','member':member})
    if not by_role['outer']:
        errors.append({'kind':'no_complete_outer_ring'})
    polygons=[];polygons_lonlat=[];ring_provenance=[]
    for ring in by_role['outer']:
        coords=[xy_nodes[i] for i in ring['node_ids']]
        polygons.append({'outer':coords,'holes':[]})
        polygons_lonlat.append({'outer':[lonlat_nodes[i] for i in ring['node_ids']],'holes':[]})
        ring_provenance.append({'role':'outer',**ring})
    for ring in by_role['inner']:
        coords=[xy_nodes[i] for i in ring['node_ids']]
        # A hole belongs to the smallest containing outer, independently of
        # member order or winding. Multiple disjoint outer components work too.
        owners=[i for i,p in enumerate(polygons)
                if all(covers_ring(q,p['outer']) for q in ring_samples(coords))]
        if not owners:
            errors.append({'kind':'orphan_inner_ring','member_way_ids':ring['member_way_ids']})
            continue
        owner=min(owners,key=lambda i:centroid(polygons[i]['outer'])[1])
        polygons[owner]['holes'].append(coords)
        polygons_lonlat[owner]['holes'].append([lonlat_nodes[i] for i in ring['node_ids']])
        ring_provenance.append({'role':'inner','outer_component':owner,**ring})
    return polygons,polygons_lonlat,ring_provenance,errors,warnings

def candidate_geometry(osm_type,osm_id,tags,polygons,polygons_lonlat,provenance):
    """Metrics over the filled multipolygon and every real boundary ring."""
    rings=[r for p in polygons for r in [p['outer'],*p['holes']]]
    weighted=[]
    for poly in polygons:
        c,area=centroid(poly['outer']);weighted.append((c,area))
        for hole in poly['holes']:
            c,area=centroid(hole);weighted.append((c,-area))
    area=sum(a for _,a in weighted)
    if area<=0:
        raise ValueError('non_positive_filled_area')
    c=(sum(p[0]*a for p,a in weighted)/area,
       sum(p[1]*a for p,a in weighted)/area)
    all_pts=[p for ring in rings for p in ring]
    edge=min((closest_segment((0,0),a,b) for ring in rings
              for a,b in zip(ring,ring[1:])),key=lambda x:x[0])
    bbox=((min(p[0] for p in all_pts)+max(p[0] for p in all_pts))/2,
          (min(p[1] for p in all_pts)+max(p[1] for p in all_pts))/2)
    contained=in_polygons((0,0),polygons)
    camera_on_boundary=edge[0]<=0.002
    edges=[]
    for pi,poly in enumerate(polygons):
        for ri,ring in enumerate([poly['outer'],*poly['holes']]):
            edges.extend({**s,'component':pi,'ring_role':'outer' if ri==0 else 'inner','ring_index':ri}
                         for s in segments(ring))
    primary=max(range(len(polygons)),key=lambda i:centroid(polygons[i]['outer'])[1])
    camera_in_envelope=any(inside((0,0),poly['outer']) for poly in polygons)
    # A courtyard camera can see the building around all 360 degrees. A vertex
    # min-arc is only an angular envelope diagnostic, never an occlusion model.
    interval=({'start_deg':0.,'end_deg':0.,'width_deg':360.,'wraps_north':True}
              if camera_in_envelope and not contained else circular_interval(all_pts))
    return {'osm_type':osm_type,'osm_id':osm_id,'tags':tags,
            'boundary_distance_m':round(edge[0],2),
            'polygon_distance_m':0 if contained or camera_on_boundary else round(edge[0],2),
            'centroid_distance_m':round(math.hypot(*c),2),
            'bbox_center_distance_m':round(math.hypot(*bbox),2),
            'camera_inside':contained,'camera_on_boundary':camera_on_boundary,
            'nearest_boundary_bearing_deg':round(bearing(edge[1]),2),
            'centroid_bearing_deg':round(bearing(c),2),'bbox_center_xy':bbox,'centroid_xy':c,
            'area_m2':round(area,2),'bearing_interval':None if contained else interval,
            'nearest_edges':sorted(edges,key=lambda s:s['distance_m'])[:8],
            'front_facing_edges_2d':facing_edges(polygons)[:16],
            'facade_diagnostic_limit':'Facing normals and self-occlusion in ground plan only; heights, terrain, other buildings and actual photo correspondence are not evaluated.',
            'polygons_xy':polygons,'polygons_lonlat':polygons_lonlat,
            'geometry_xy':polygons[primary]['outer'],
            'geometry_lonlat':polygons_lonlat[primary]['outer'],
            'legacy_geometry_scope':'largest_outer_only_use_polygons_for_full_geometry',
            'ring_provenance':provenance,'geometry_complete':True,
            'geometry_validation_scope':'node_completeness_closed_rings_and_hole_assignment'}

def group_buildings(candidates,relations):
    """Keep parts inspectable while giving each physical building one shortlist slot.

    Explicit OSM relation membership is preferred. The spatial fallback applies
    only to tagged building:part, uses complete outer envelopes, and records its
    provenance. Independent buildings in a courtyard remain independent.
    """
    ref=lambda c:f"{c['osm_type']}/{c['osm_id']}"
    index={ref(c):c for c in candidates}
    parent_of={};reasons={}
    for rel in relations:
        parent=f"relation/{rel['osm_id']}"
        if rel['tags'].get('type')=='multipolygon' and parent in index:
            for member in rel['members']:
                child=f"{member['type']}/{member['ref']}"
                if member.get('role')=='outer' and child in index:
                    parent_of[child]=parent;reasons[child]='multipolygon_outer_member'
        elif rel['tags'].get('type')=='building':
            outlines=[f"{m['type']}/{m['ref']}" for m in rel['members']
                      if m.get('role')=='outline' and f"{m['type']}/{m['ref']}" in index]
            if len(outlines)==1:
                for member in rel['members']:
                    child=f"{member['type']}/{member['ref']}"
                    if member.get('role')=='part' and child in index:
                        parent_of[child]=outlines[0];reasons[child]='building_relation_outline_part'
    parents=[c for c in candidates if c['tags'].get('building') not in (None,'no')
             and c['tags'].get('building:part') in (None,'no')]
    for part in candidates:
        child=ref(part)
        if part['tags'].get('building:part') in (None,'no') or child in parent_of:
            continue
        points=[q for poly in part['polygons_xy'] for q in ring_samples(poly['outer'])]
        possible=[parent for parent in parents if ref(parent)!=child and
                  all(any(covers_ring(q,p['outer']) for p in parent['polygons_xy'])
                      for q in points)]
        if possible:
            # Ignore duplicate outline ways already canonicalised to a relation.
            canonical=[p for p in possible if ref(p) not in parent_of] or possible
            canonical.sort(key=lambda p:p['area_m2'])
            if len(canonical)>1:
                part['possible_parent_refs']=[ref(p) for p in canonical]
                # Ambiguous overlapping buildings are not silently coalesced.
                reasons[child]='ambiguous_parent_outer_containment'
            else:
                parent_of[child]=ref(canonical[0]);reasons[child]='part_within_parent_outer_envelope'
    def canonical(key):
        seen=set()
        while key in parent_of and key not in seen:
            seen.add(key);key=parent_of[key]
        return key
    members={}
    for candidate in candidates:
        key=ref(candidate);parent=canonical(key)
        candidate['group_key']=parent
        candidate['grouping_reason']=reasons.get(key,'standalone_building')
        members.setdefault(parent,[]).append(key)
    groups=[]
    metrics=('osm_type','osm_id','tags','boundary_distance_m','polygon_distance_m',
             'centroid_distance_m','bbox_center_distance_m','camera_inside',
             'nearest_boundary_bearing_deg','centroid_xy','area_m2','geometry_complete')
    for key,children in members.items():
        representative=index[key]
        groups.append({**{k:representative[k] for k in metrics},'group_key':key,
                       'representative_ref':key,'member_refs':children,
                       'member_count':len(children),
                       'part_count':sum(index[c]['tags'].get('building:part') not in (None,'no')
                                        for c in children)})
        for field in ('linked_addresses','context_associations'):
            if representative.get(field):groups[-1][field]=representative[field]
    groups.sort(key=lambda c:c['boundary_distance_m'])
    for i,g in enumerate(groups):g['rank_group_boundary']=i+1
    for i,g in enumerate(sorted(groups,key=lambda c:c['bbox_center_distance_m'])):
        g['rank_group_bbox_center']=i+1
    return groups

def link_context(candidates,nodes,areas):
    """Keep addresses/amenity grounds as evidence, without merging identities."""
    addressed=[n for n in nodes if n['tags'].get('addr:housenumber')]
    for candidate in candidates:
        boundary_nodes={node_id for ring in candidate['ring_provenance'] for node_id in ring['node_ids']}
        candidate['linked_addresses']=[]
        for node in addressed:
            if node['osm_id'] in boundary_nodes:
                reason='boundary_node_member'
            elif in_polygons(node['xy'],candidate['polygons_xy']):
                reason='address_node_inside_footprint'
            else:continue
            candidate['linked_addresses'].append({'osm_type':'node','osm_id':node['osm_id'],
                'tags':node['tags'],'xy':node['xy'],'association':reason})
        candidate['context_associations']=[]
        samples=[q for poly in candidate['polygons_xy'] for q in ring_samples(poly['outer'])]
        for area in areas:
            if not area.get('geometry_complete'):continue
            def covered(q):
                return (in_polygons(q,area['polygons_xy']) or
                        any(on_ring(q,ring) for p in area['polygons_xy'] for ring in [p['outer'],*p['holes']]))
            if all(covered(q) for q in samples):
                candidate['context_associations'].append({
                    'osm_type':area['osm_type'],'osm_id':area['osm_id'],'tags':area['tags'],
                    'association':'footprint_within_amenity_area',
                    'identity_merged':False})

def parse_osm(path,lat,lon):
    root=ET.parse(path).getroot()
    nodes={int(n.attrib["id"]):n for n in root.findall("node")}
    lonlat_nodes={i:(float(n.attrib['lon']),float(n.attrib['lat'])) for i,n in nodes.items()}
    xy_nodes={i:xy(*coord,lon,lat) for i,coord in lonlat_nodes.items()}
    tags_of=lambda el:{t.attrib['k']:t.attrib['v'] for t in el.findall('tag')}
    ways={int(w.attrib['id']):{'nodes':[int(n.attrib['ref']) for n in w.findall('nd')],
                              'tags':tags_of(w)} for w in root.findall('way')}
    is_building=lambda tags:(tags.get('building') not in (None,'no') or
                             tags.get('building:part') not in (None,'no'))
    candidates=[];roads=[];pois=[];unresolved=[];relations=[];context_areas=[]
    is_context=lambda tags:tags.get('amenity') in ('school','college','university','hospital','arts_centre')
    for node_id,n in nodes.items():
        tags=tags_of(n)
        if tags:
            pois.append({'osm_type':'node','osm_id':node_id,'tags':tags,'xy':xy_nodes[node_id],
                         'distance_m':round(math.hypot(*xy_nodes[node_id]),2)})
    for way_id,w in ways.items():
        tags=w['tags'];ids=w['nodes']
        missing=sorted({i for i in ids if i not in xy_nodes})
        complete=not missing
        pts=[xy_nodes[i] for i in ids if i in xy_nodes]
        if 'highway' in tags:
            runs=[];run=[]
            for node_id in ids:
                if node_id in xy_nodes:run.append(xy_nodes[node_id])
                else:
                    if len(run)>1:runs.append(run)
                    run=[]
            if len(run)>1:runs.append(run)
            roads.append({'osm_id':way_id,'tags':tags,'xy':pts,'complete':complete,
                          'geometry_segments_xy':runs,'missing_node_ids':missing})
        if not is_building(tags):
            if is_context(tags) and complete and len(ids)>=4 and ids[0]==ids[-1]:
                context_areas.append({'osm_type':'way','osm_id':way_id,'tags':tags,
                    'polygons_xy':[{'outer':pts,'holes':[]}],
                    'polygons_lonlat':[{'outer':[lonlat_nodes[i] for i in ids],'holes':[]}],
                    'geometry_complete':True})
            continue
        errors=[]
        if missing:errors.append({'kind':'missing_nodes','node_ids':missing})
        if len(ids)<4 or ids[0]!=ids[-1]:errors.append({'kind':'not_a_closed_ring'})
        if ids and len(set(ids[:-1]))!=len(ids)-1:
            errors.append({'kind':'repeated_ring_nodes'})
        if errors:
            unresolved.append({'osm_type':'way','osm_id':way_id,'tags':tags,
                               'geometry_complete':False,'geometry_errors':errors})
            continue
        provenance=[{'role':'outer','node_ids':ids,'member_way_ids':[way_id]}]
        try:
            candidates.append(candidate_geometry('way',way_id,tags,
                [{'outer':pts,'holes':[]}],
                [{'outer':[lonlat_nodes[i] for i in ids],'holes':[]}],provenance))
        except ValueError as exc:
            unresolved.append({'osm_type':'way','osm_id':way_id,'tags':tags,
                               'geometry_complete':False,'geometry_errors':[{'kind':str(exc)}]})
    for rel in root.findall('relation'):
        rel_id=int(rel.attrib['id']);raw_tags=tags_of(rel);tags=dict(raw_tags)
        members=[dict(x.attrib) for x in rel.findall('member')]
        inherited=[]
        # Old-style multipolygons may carry their feature tags on an outer way.
        # Do not inherit building from unrelated landuse/natural relations.
        if tags.get('type')=='multipolygon' and not is_building(tags) and not any(
            k in tags for k in ('landuse','natural','amenity','leisure','historic','water','waterway','boundary')):
            outers=[(int(m['ref']),ways[int(m['ref'])]['tags']) for m in members
                    if m.get('type')=='way' and m.get('role')=='outer' and int(m['ref']) in ways
                    and is_building(ways[int(m['ref'])]['tags'])]
            if outers:
                # Keep only values common to every tagged outer; raw relation
                # tags always win. The source IDs remain visible in diagnostics.
                common={k:v for k,v in outers[0][1].items()
                        if all(t.get(k)==v for _,t in outers)}
                tags={**common,**tags};inherited=[i for i,_ in outers]
        if not (is_building(tags) or tags.get('type') in ('multipolygon','building')):
            continue
        info={'osm_id':rel_id,'tags':tags,'raw_tags':raw_tags,'members':members}
        if inherited:info['inherited_tag_outer_way_ids']=inherited
        if tags.get('type')=='building':
            info['geometry_status']='grouping_relation_no_own_footprint'
            relations.append(info);continue
        as_context=not is_building(tags) and is_context(tags)
        if not is_building(tags) and not as_context:
            info['geometry_status']='non_building_not_assembled'
            relations.append(info);continue
        polygons,polygons_ll,provenance,errors,warnings=assemble_multipolygon(
            members,ways,xy_nodes,lonlat_nodes)
        info.update({'geometry_complete':not errors,'geometry_errors':errors,
                     'geometry_warnings':warnings,'outer_ring_count':len(polygons),
                     'inner_ring_count':sum(len(p['holes']) for p in polygons),
                     'ring_provenance':provenance})
        if not errors:
            if as_context:
                context_areas.append({'osm_type':'relation','osm_id':rel_id,'tags':tags,
                    'polygons_xy':polygons,'polygons_lonlat':polygons_ll,'geometry_complete':True,
                    'ring_provenance':provenance})
            else:
              try:
                candidate=candidate_geometry('relation',rel_id,tags,polygons,polygons_ll,provenance)
                if inherited:candidate['inherited_tag_outer_way_ids']=inherited
                candidates.append(candidate)
              except ValueError as exc:
                info['geometry_complete']=False;errors.append({'kind':str(exc)})
        if errors:
            record={'osm_type':'relation','osm_id':rel_id,'tags':tags,
                               'geometry_complete':False,'geometry_errors':errors,
                               'partial_polygons_xy':polygons}
            if as_context:context_areas.append(record)
            else:unresolved.append(record)
        relations.append(info)
    candidates.sort(key=lambda c:c['boundary_distance_m'])
    for i,c in enumerate(candidates):c['rank_boundary']=i+1
    for i,c in enumerate(sorted(candidates,key=lambda c:c['bbox_center_distance_m'])):
        c['rank_bbox_center']=i+1
    link_context(candidates,pois,context_areas)
    groups=group_buildings(candidates,relations)
    return {'camera':{'lat':lat,'lon':lon},'buildings':candidates,'building_groups':groups,
            'roads':roads,'tagged_nodes':pois,'relations':relations,'unresolved_buildings':unresolved,
            'context_areas':context_areas,
            'data_quality':{'complete_building_geometries':len(candidates),
                            'complete_building_groups':len(groups),
                            'unresolved_building_geometries':len(unresolved),
                            'note':'Rankings cover complete downloaded geometries; missing relation members are not evidence that an object does not exist.'},
            'osm_copyright':'© OpenStreetMap contributors, ODbL 1.0',
            'geometry_method':'local_tangent_plane_R6371008.8_multipolygon_boundaries_holes_and_parent_part_groups'}

def fetch_osm(out,lat,lon,radius,local_only=False):
    dlat=math.degrees(radius/R);dlon=dlat/math.cos(math.radians(lat))
    bbox=f'{lon-dlon:.7f},{lat-dlat:.7f},{lon+dlon:.7f},{lat+dlat:.7f}'
    url='https://api.openstreetmap.org/api/0.6/map?bbox='+bbox
    p=out.with_suffix('.osm')
    reused=p.exists()
    if not p.exists():
        if local_only:raise FileNotFoundError(f'Local OSM snapshot missing: {p}')
        t=time.monotonic()
        req=urllib.request.Request(url,headers={'User-Agent':'StreetStorySpatialResearch/1.0 (bounded owner-requested case study)'})
        with urllib.request.urlopen(req,timeout=45) as response:
            body=response.read(12_000_000)
            if len(body)>=12_000_000:raise ValueError('bounded response size exceeded')
        ET.fromstring(body)
        p.write_bytes(body)
        out.with_suffix('.fetch.json').write_text(json.dumps({'url':url,'bbox':bbox,'radius_m':radius,'elapsed_s':time.monotonic()-t,'size_bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'fetched_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())},indent=2))
    manifest_path=out.with_suffix('.fetch.json')
    manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    data=parse_osm(p,lat,lon)
    data.update({'source_url':manifest.get('url',url),
                 'radius_m':manifest.get('radius_m',radius),
                 'raw_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
                 'raw_cache_reused':reused})
    out.with_suffix('.geometry.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    return data

def main():
    parser=argparse.ArgumentParser();parser.add_argument('id');parser.add_argument('lat',type=float);parser.add_argument('lon',type=float);parser.add_argument('--radius',type=float,default=240);parser.add_argument('--out',default='research/osm');parser.add_argument('--local-only',action='store_true')
    a=parser.parse_args();p=Path(a.out)/('photo-'+a.id);p.parent.mkdir(parents=True,exist_ok=True)
    data=fetch_osm(p,a.lat,a.lon,a.radius,a.local_only)
    print(json.dumps({'id':a.id,'buildings':len(data['buildings']),
        'building_groups':len(data['building_groups']),'roads':len(data['roads']),
        'unresolved_buildings':len(data['unresolved_buildings']),
        'top_groups':[{k:c[k] for k in ['osm_type','osm_id','tags','boundary_distance_m',
            'bbox_center_distance_m','camera_inside','nearest_boundary_bearing_deg',
            'part_count','rank_group_boundary','rank_group_bbox_center']}
            for c in data['building_groups'][:12]]},ensure_ascii=False))

if __name__=='__main__':main()
