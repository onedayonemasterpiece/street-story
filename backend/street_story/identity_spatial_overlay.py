"""Annotate only already model-nominated real OSM physical bodies on detail MAP.

Original SOURCE pixels are NEVER modified. The full neutral OSM overview,
body labels and original map geometry remain unchanged. A new right-hand MAP
detail adds translucent colored plan outlines and large numeric received
labels so a visual LLM can distinguish main facade vs neighboring wings.

This is a deterministic presentation transform, not target selection,
geometric identity evidence or reference-image generation.
"""
from __future__ import annotations

import hashlib
import io
import math


def model_proposed_outline_overlay(scene, story, candidate_ids):
    from PIL import Image, ImageDraw, ImageFont
    from .identity_scene import scene_entries
    from .identity_spatial_features import _point, _local

    manifest=scene.get('manifest') or {}
    if not scene.get('bytes') or not isinstance(candidate_ids,(list,tuple)):
        return None
    views=[r for r in manifest.get('views') or [] if r.get('name')=='nominated_detail']
    if len(views)!=1 or not 1<=len(set(candidate_ids))<=3:
        return None
    view=views[0]
    panel=view.get('panel_pixels') or []
    extent=view.get('extent_east_north_m') or []
    if (len(panel)!=4 or len(extent)!=4 or
            not all(isinstance(n,(float,int)) and math.isfinite(n)
                for n in [*panel,*extent])):
        return None
    left,top,right,_bottom=panel
    minx,miny,maxx,maxy=extent
    pixels=right-left
    span_x=maxx-minx
    span_y=maxy-miny
    if pixels<300 or span_x<5 or span_y<5:
        return None
    origin=_point(story)
    if origin is None:
        return None
    received={e['candidate_id']:e for e in scene_entries(story,[])}
    cols=(manifest.get('objects') or {}).get('columns') or []
    label_by_id={r['candidate_id']:r['label'] for r in (
        dict(zip(cols,row)) for row in (manifest.get('objects') or {}).get('rows') or [])
        if type(r.get('label')) is int and isinstance(r.get('candidate_id'),str)}
    if any(cid not in received or cid not in label_by_id for cid in candidate_ids):
        return None
    # Source order comes from the model. Colors denote different choices,
    # not higher confidence or verified correctness.
    palette=[(37,119,217),(229,131,36),(125,92,178)]
    img=Image.open(io.BytesIO(scene['bytes'])).convert('RGB')
    opacity=Image.new('RGBA',img.size,(0,0,0,0))
    d=ImageDraw.Draw(opacity)
    font=ImageFont.load_default(size=30)
    bbox=[]
    for color,cid in zip(palette,candidate_ids):
        geom=received[cid].get('map_geometry') or {}
        geometry=[]
        for ring in geom.get('rings') or []:
            if ring.get('role') not in (None,'outer') or not ring.get('closed'):
                continue
            points=[_point(p) for p in ring.get('points') or []]
            if len(points)<4 or any(p is None for p in points):
                continue
            coords=[_local(p,origin) for p in points]
            pix=[(round(left+(x-minx)/span_x*pixels),
                  round(top+(maxy-y)/span_y*pixels)) for x,y in coords]
            geometry+=pix
            d.polygon(pix,fill=(*color,45))
            d.line(pix,fill=(*color,235),width=5,joint='curve')
        if not geometry:
            continue
        # Centroid of rendered polygon is a label-PLACEMENT aid only.
        # It is not evidence of an entrance, camera pose or photographed wing.
        x=sum(p[0] for p in geometry)/len(geometry)
        y=sum(p[1] for p in geometry)/len(geometry)
        if not (left+10<=x<=right-10 and top+10<=y<=top+pixels-10):
            continue
        label='@'+str(label_by_id[cid])
        box=d.textbbox((0,0),label,font=font)
        width,height=box[2]-box[0],box[3]-box[1]
        cx=int(min(max(x,left+8+width/2),right-8-width/2))
        cy=int(min(max(y,top+8+height/2),top+pixels-8-height/2))
        d.rounded_rectangle((cx-width/2-10,cy-height/2-5,
                             cx+width/2+10,cy+height/2+6),
                            radius=8,fill=(*color,240))
        d.text((cx-width/2,cy-height/2),label,font=font,fill=(255,255,255,255),
               stroke_width=1,stroke_fill=(10,22,40,255))
        bbox.append({'label':label_by_id[cid],'model_chosen':True,
                     'outline_vertices_rendered':len(geometry),
                     'detail_label_pixel':[cx,cy]})
    if not bbox:
        return None
    img=Image.alpha_composite(img.convert('RGBA'),opacity).convert('RGB')
    buffer=io.BytesIO()
    img.save(buffer,format='PNG',optimize=True)
    data=buffer.getvalue()
    newmanifest={**manifest,'image_sha256':hashlib.sha256(data).hexdigest(),
       'parent_unhighlighted_map_sha256':manifest['image_sha256'],
       'visual_presentation':'model_proposed_osm_contour_highlight_v1',
       'model_nominated_overlay_labels':bbox,
       'not_geo_or_identity_proof':True}
    return {'mime_type':'image/png','bytes':data,'manifest':newmanifest}
