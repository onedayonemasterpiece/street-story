"""One neutral OSM overview for the existing SOURCE planning operation.

The map draws observed vectors and measured geometry, never an inferred target,
camera heading, height, occlusion or physical identity. No network acquisition.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math

from PIL import Image, ImageDraw, ImageFont

from .identity_map_context import map_entry_context

POLICY = 'observed-osm-scene-v1'


def _position(item):
    point = item.get('map_coordinates') or item.get('center') or item
    try:
        lat = point.get('latitude', point.get('lat'))
        lon = point.get('longitude', point.get('lon'))
        if isinstance(lat, bool) or isinstance(lon, bool):
            return None
        lat, lon = float(lat), float(lon)
        return (lat, lon) if math.isfinite(lat + lon) and -90 <= lat <= 90 and -180 <= lon <= 180 else None
    except (TypeError, ValueError):
        return None


def scene_entries(story, candidates):
    research = json.loads(story.get('research_json') or '{}')
    osm = story.get('_identity_map_snapshot') or research.get('osm') or {}
    observed = story.get('_identity_observed_candidates') or (research.get('visual_identity') or {}).get('observed_candidates') or []
    nearby = (story.get('_identity_search_context') or {}).get('nearby') or []
    entries = {}
    for item in [*(osm.get('observed_pool') or osm.get('nearby') or []), *nearby, *observed, *candidates]:
        if not isinstance(item, dict):
            continue
        entry = {**item, **map_entry_context(item)} if not item.get('candidate_id') else dict(item)
        cid = str(entry.get('candidate_id') or '')
        if not cid.startswith(('osm:node:', 'osm:way:', 'osm:relation:')):
            continue
        # Raw OSM geometry/tags remain authoritative when the active catalog
        # carries a shorter alias record for the same exact observed object.
        previous = entries.get(cid) or {}
        entries[cid] = {**previous, **entry,
            **({'map_geometry': previous['map_geometry']} if previous.get('map_geometry') else {}),
            'map_object': {**(previous.get('map_object') or {}), **(entry.get('map_object') or {}),
                'tags': {**(previous.get('map_object') or {}).get('tags', {}),
                    **(entry.get('map_object') or {}).get('tags', {})}}}
    return [entries[key] for key in sorted(entries)]


def scene_camera_context(story):
    """Separate measured EXIF, explicit approximate camera and search anchors."""
    point = _position({'lat': story.get('latitude'), 'lon': story.get('longitude')})
    research = json.loads(story.get('research_json') or '{}')
    identity = research.get('visual_identity') or {}
    provenance = research.get('location_provenance') or story.get('_location_provenance') or {}
    verified = bool(point and (story.get('_camera_position_verified') is True or (
        story.get('photo_sha256') and identity.get('photo_sha256') == story.get('photo_sha256')
        and identity.get('generation') == int(story.get('_identity_generation', research.get('identity_generation') or 0))
        and identity.get('camera_position_verified') is True)))
    owner_approx = bool(point and provenance.get('kind') == 'owner_approx_camera')
    if provenance.get('kind') in {'owner_live_place_query', 'owner_approx_camera'}:
        verified = False
    status = 'original_exif' if verified else 'owner_approximate' if owner_approx else 'search_context'
    hints = story.get('_camera_hints') or identity.get('camera_hints') or {}
    accuracy = hints.get('horizontal_error_m') if verified else provenance.get('accuracy_m') if owner_approx else None
    if isinstance(accuracy, bool) or not isinstance(accuracy, (int, float)) or not math.isfinite(accuracy) or accuracy < 0:
        accuracy = None
    return {'latitude': point[0] if point and status != 'search_context' else None,
        'longitude': point[1] if point and status != 'search_context' else None,
        'position_verified': verified, 'position_status': status,
        'position_source': 'selected_original_exif' if verified else provenance.get('kind') or 'unverified_coordinates',
        'accuracy_m': accuracy, 'accuracy_source': 'original_exif' if verified and accuracy is not None
            else 'owner_reported' if owner_approx and accuracy is not None else 'unknown',
        'heading_status': (hints.get('direction_status') or 'missing') if verified else 'missing',
        **({'heading_degrees': hints['direction_degrees'], 'heading_reference': hints.get('direction_ref')}
            if verified and hints.get('direction_degrees') is not None else {})}


def render_scene(story, candidates):
    camera = _position({'lat': story.get('latitude'), 'lon': story.get('longitude')})
    entries = scene_entries(story, candidates)
    if not camera or not entries:
        return None
    research = json.loads(story.get('research_json') or '{}')
    provenance = research.get('location_provenance') or story.get('_location_provenance') or {}
    camera_context = scene_camera_context(story)
    verified = camera_context['position_verified']
    owner_approx = camera_context['position_status'] == 'owner_approximate'
    origin = 'camera_gps' if verified else 'owner_approx_camera_hint' if owner_approx else 'search_context_anchor'
    scale = 6_371_000 * math.pi / 180
    def local(point):
        return (((point[1] - camera[1] + 180) % 360 - 180) * scale * math.cos(math.radians(camera[0])),
                (point[0] - camera[0]) * scale)
    def observed_points(line):
        points = [_position(raw) if isinstance(raw, dict) else None for raw in line]
        return points if points and all(point is not None for point in points) else []
    objects, extent = [], [(0., 0.)]
    for number, entry in enumerate(entries, 1):
        geometry = entry.get('map_geometry') or {}
        lines = [[local(point) for point in observed_points(line)] for line in geometry.get('lines') or []]
        lines = [line for line in lines if len(line) >= 2]
        rings = [{**ring, 'points': [local(point) for point in observed_points(ring.get('points') or [])]}
                 for ring in geometry.get('rings') or []]
        points = [point for line in lines for point in line]
        position = _position(entry)
        anchor = local(position) if position else ((sum(x for x, _ in points)/len(points),
            sum(y for _, y in points)/len(points)) if points else None)
        tags = (entry.get('map_object') or {}).get('tags') or entry.get('tags') or {}
        footprint = tags.get('building') not in {None, '', 'no'} or bool(tags.get('building:part'))
        entry_anchor = tags.get('entrance') or (entry.get('map_address') or {}).get('house_number')
        if footprint or entry_anchor:
            extent.extend(points or ([anchor] if anchor else []))
        lengths = sorted((math.dist(a, b) for line in lines for a, b in zip(line, line[1:])), reverse=True)
        dimensions = [round(max(p[i] for p in points)-min(p[i] for p in points), 1) for i in (0, 1)] if points else None
        raw_height = {key: str(tags[key])[:60] if tags.get(key) is not None else None
                      for key in ('height', 'building:levels', 'roof:levels')}
        metadata = {'label': number, 'candidate_id': entry['candidate_id'],
            'name':str(tags.get('name') or '')[:100],
            'object_kind': {key: str(tags[key])[:60] for key in ('building', 'building:part', 'entrance', 'highway') if tags.get(key)},
            'address': [str((entry.get('map_address') or {}).get(key) or '')[:100] for key in ('street', 'house_number')],
            'height_levels': list(raw_height.values()), 'height_status': 'observed' if raw_height['height'] is not None else 'missing',
            'attribute_provenance': (entry.get('map_object') or {}).get('provenance')
                if (entry.get('map_object') or {}).get('provenance') not in {None, 'osm.tags'} else None,
            'boundary_distance_m': entry.get('boundary_distance_m'),
            'representative_distance_m': round(math.hypot(*anchor), 1) if anchor else None,
            'bearing_start_end_span_degrees': [round(value,1) if isinstance(value,(int,float)) else None
                for key in ('start_degrees', 'end_degrees', 'angular_span_degrees')
                for value in [entry.get('footprint_bearing_interval', {}).get(key)]],
            'extent_east_north_m': dimensions, 'longest_observed_segments_m': [round(value, 1) for value in lengths[:4]],
            'geometry_status': 'observed' if lines else 'point_only' if anchor else 'missing',
            'contour_roles': [ring.get('role') for ring in rings],
            'contours_complete': all(ring.get('closed') for ring in rings) if rings else None}
        component = entry.get('physical_component')
        if component:
            metadata['component_membership'] = {key: component[key] for key in
                ('parent_candidate_id', 'member_candidate_id', 'role', 'proof') if key in component}
        objects.append({'entry': entry, 'tags': tags, 'anchor': anchor, 'lines': lines, 'rings': rings, 'metadata': metadata})
    if len(extent) == 1:
        return None
    # Fit every observed contour/point. No nearest-N or distance/FOV cutoff.
    xmin, xmax = min(p[0] for p in extent), max(p[0] for p in extent)
    ymin, ymax = min(p[1] for p in extent), max(p[1] for p in extent)
    span = max(xmax-xmin, ymax-ymin, 80.) * 1.12
    cx, cy = (xmin+xmax)/2, (ymin+ymax)/2
    width, margin = 1280, 70
    pixels_per_m = (width-2*margin)/span
    def pixel(point):
        return (round(width/2 + (point[0]-cx)*pixels_per_m), round(width/2 - (point[1]-cy)*pixels_per_m))
    canvas = Image.new('RGB', (width, width+70), '#fafaf7')
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 17)
    except OSError:
        font = ImageFont.load_default()
    # All footprints have the same styling. Roads and entries use only literal
    # OSM object attributes, not model relevance or reference availability.
    for obj in objects:
        tags = obj['tags']
        building = tags.get('building') not in {None, '', 'no'} or bool(tags.get('building:part'))
        if building:
            for ring in sorted(obj['rings'], key=lambda ring: ring.get('role') == 'inner'):
                points = [pixel(point) for point in ring['points']]
                if ring.get('closed') and len(points) >= 4:
                    draw.polygon(points, fill='#fafaf7' if ring.get('role') == 'inner' else '#e3e3df')
        for line in obj['lines']:
            draw.line([pixel(point) for point in line], fill='#8f8f87' if tags.get('highway') else '#556777', width=3 if tags.get('highway') else 2)
        if obj['anchor'] and tags.get('entrance'):
            x, y = pixel(obj['anchor'])
            draw.ellipse((x-4,y-4,x+4,y+4), fill='#b17436')
    occupied, road_names = [], {}
    for obj in objects:
        name = obj['tags'].get('name') if obj['tags'].get('highway') else None
        if not name:
            continue
        for line in obj['lines']:
            for a,b in zip(line,line[1:]):
                p,q=pixel(a),pixel(b)
                if all(60 < v < width-60 for v in (*p,*q)):
                    length=math.dist(p,q)
                    if length>road_names.get(name,(0,None))[0]:
                        road_names[name]=(length,((p[0]+q[0])/2,(p[1]+q[1])/2))
    for name, (_length, anchor) in road_names.items():
        draw.text(anchor,str(name)[:45],font=font,fill='#595950')
    for obj in objects:
        if not obj['anchor']:
            continue
        x, y = pixel(obj['anchor'])
        obj['metadata']['label_visible'] = 10 <= x < width-10 and 10 <= y < width-10
        if obj['tags'].get('highway') and not obj['tags'].get('building'):
            obj['metadata']['label_visible'] = False
        if not obj['metadata']['label_visible']:
            continue  # Long road ways remain context; their remote midpoint need not expand the building overview.
        label = str(obj['metadata']['label'])
        box = draw.textbbox((0,0), label, font=font)
        w, h = box[2]+6, 23
        chosen = (x, y)
        for dx, dy in [(0,0), (18,0), (-18,0), (0,22), (0,-22), (35,22), (-35,-22), (48,-35), (-48,35)]:
            lx, ly = x+dx, y+dy
            bounds = (lx-w/2, ly-h/2, lx+w/2, ly+h/2)
            if not any(bounds[0]<b[2] and bounds[2]>b[0] and bounds[1]<b[3] and bounds[3]>b[1] for b in occupied):
                chosen = (lx, ly)
                break
        lx, ly = chosen
        bounds = (lx-w/2, ly-h/2, lx+w/2, ly+h/2)
        occupied.append(bounds)
        if chosen != (x,y):
            draw.line([(x,y), chosen], fill='#96968c')
        draw.rectangle(bounds, fill='white', outline='#9a9a94')
        draw.text((lx-w/2+3,ly-h/2-1), label, font=font, fill='#151515')
        obj['metadata']['label_pixel'] = [round(lx), round(ly)]
    camera_pixel = pixel((0., 0.))
    x, y = camera_pixel
    draw.ellipse((x-7,y-7,x+7,y+7), fill='#c24738', outline='white', width=2)
    if owner_approx and camera_context['accuracy_m'] is not None:
        radius = camera_context['accuracy_m'] * pixels_per_m
        draw.ellipse((x-radius,y-radius,x+radius,y+radius),outline='#b5a49b',width=2)
    anchor_label = 'CAMERA GPS' if verified else 'CAMERA HINT (OWNER APPROX)' if owner_approx else 'SEARCH CONTEXT'
    draw.text((x+10,y-24),anchor_label,font=font,fill='#a02c23')
    draw.line([(width-42,96),(width-42,43)],fill='#151515',width=3)
    draw.polygon([(width-42,35),(width-48,48),(width-36,48)],fill='#151515')
    draw.text((width-48,12),'N',font=font,fill='#151515')
    target_scale = span/6
    power = 10**math.floor(math.log10(target_scale))
    scale_m = max(value*power for value in (1,2,5) if value*power <= target_scale)
    scale_px = round(scale_m*pixels_per_m)
    draw.line([(45,width-32),(45+scale_px,width-32)],fill='#151515',width=3)
    draw.text((45,width-61),f'{scale_m:g} m',font=font,fill='#151515')
    draw.text((30,width+10),'Observed OSM vectors; labels map to exact IDs. No target highlighted. Missing heading/height remain unknown.',font=font,fill='#303030')
    columns = list(dict.fromkeys(key for obj in objects for key in obj['metadata']))
    manifest = {'policy': POLICY, 'camera': camera_context,
        'anchor':{'latitude':camera[0], 'longitude':camera[1], 'source':provenance.get('kind') or
            ('selected_original_exif' if verified else 'unverified_coordinates'), 'label':anchor_label},
        'north_up':True, 'scale_bar_m':scale_m, 'meters_per_pixel':round(1/pixels_per_m,3),
        'image_size':[width,width+70], 'camera_pixel':list(camera_pixel),
        'projection':'local tangent plane; approximate measured distances',
        'distance_origin':origin, 'default_attribute_provenance':'osm.tags',
        'height_fields':['height','building:levels','roof:levels'], 'address_fields':['street','house_number'],
        'view_extent_basis':'all observed building contours and address/entrance anchors; context roads/areas are clipped',
        'objects':{'columns':columns,
            'rows':[[obj['metadata'].get(key) for key in columns] for obj in objects]},
        'policy_instruction':'Every received exact OSM ID remains available. Labels are neutral, not relevance ranks. '
            'SOURCE may show a partial contour; unknown levels/heading and missing Wikipedia never exclude a candidate. '
            '2D overlap without known heights does not prove occlusion. Map GPS is not confirmed SOURCE identity. '
            'Owner approximate camera coordinates are nominal inputs, not EXIF or measured accuracy; '
            'state material pose assumptions. If camera.position_status is search_context, distances/bearings '
            'describe only the search anchor, never camera position, shooting distance, direction or FOV constraints.',
        'coverage':{**((story.get('_identity_map_snapshot') or research.get('osm') or {}).get('coverage') or {}),
            'received_object_count':len(objects),'completeness':'unknown',
            'view_extent_east_north_m':[round(cx-span/2,1),round(cy-span/2,1),round(cx+span/2,1),round(cy+span/2,1)],
            'policy':'Received local OSM coverage, not proof that no farther or unmapped alternatives exist.'},
        'feature_reference_policy':'Observed footprint/component references use candidate_id, ring_index '
            '(zero based in contour_roles). Segments additionally use segment_index; road axes use line_index. '
            'Entry references require an actual mapped entrance/address node; tag uses tag_key with an observed literal. '
            'References identify supplied OSM primitives, never invented arches, heights or physical passages.'}
    out = io.BytesIO()
    canvas.save(out,format='PNG',optimize=True)
    data = out.getvalue()
    manifest['image_sha256'] = hashlib.sha256(data).hexdigest()
    return {'mime_type':'image/png','bytes':data,'manifest':manifest}


async def planner_scene(service, story, candidates):
    """CPU-only joint map transport; failures never block ready sources."""
    try:
        return await asyncio.to_thread(render_scene, story, candidates)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        import logging
        logging.getLogger('uvicorn.error').info('street_story_identity_scene story_id=%s outcome=unavailable', story.get('id'))
        return None


def lean_scene_manifest(manifest):
    """All exact labels; measured bodies once, without null road geometry rows."""
    table = manifest.get('objects') or {}
    columns = table.get('columns') or []
    decoded = [dict(zip(columns,row)) for row in table.get('rows') or []]
    labels = ['label','candidate_id']
    geometry = ['label','address','height_levels','height_status','attribute_provenance',
        'boundary_distance_m','representative_distance_m','bearing_start_end_span_degrees',
        'extent_east_north_m','longest_observed_segments_m','geometry_status','contour_roles',
        'contours_complete','component_membership']
    bodies, anchors=[],[]
    def compact_missing(value):
        if isinstance(value,(list,dict)):
            values=list(value.values()) if isinstance(value,dict) else value
            if not values or all(v is None or v == '' for v in values):
                return None
        return value
    for row in decoded:
        kind=row.get('object_kind') or {}
        road_context=kind.get('highway') and not any(kind.get(k) for k in ('building','building:part','entrance'))
        if not road_context or any(value is not None for value in row.get('height_levels') or []):
            if row.get('geometry_status')!='observed' and not row.get('component_membership') and not any(
                    value is not None for value in row.get('height_levels') or []):
                anchors.append([row.get('label'), row.get('representative_distance_m'), compact_missing(row.get('address')),
                    row.get('geometry_status')])
            else:
                bodies.append([compact_missing(row.get(key)) for key in geometry])
    missing_columns=[key for index,key in enumerate(geometry) if all(row[index] is None for row in bodies)]
    indexes=[index for index,key in enumerate(geometry) if key not in missing_columns]
    geometry=[geometry[index] for index in indexes]
    bodies=[[row[index] for index in indexes] for row in bodies]
    return {**{key:manifest[key] for key in ('policy','camera','anchor','distance_origin','height_fields',
        'address_fields','default_attribute_provenance','north_up','scale_bar_m','meters_per_pixel',
        'image_size','projection','view_extent_basis','image_sha256','policy_instruction','feature_reference_policy','coverage') if key in manifest},
        'objects':{'columns':labels,'rows':[[row.get(key) for key in labels] for row in decoded]},
        'physical_geometry':{'columns':geometry,'rows':bodies},
        'point_geometry':{'columns':['label','representative_distance_m','address','geometry_status'],'rows':anchors},
        'missing_fields':missing_columns,
        'missing_policy':'Omitted fields and null arrays mean unavailable/unknown, never zero height or contradiction. '
            'Actual height/level and contour values are retained when supplied.',
        'geometry_join':'physical_geometry.label joins objects.label; all received exact IDs remain in objects. '
            'Road context is drawn in the overview; missing road height/body summaries are not invented.'}
