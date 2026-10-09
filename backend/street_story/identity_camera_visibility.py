"""Nominal camera exterior-halfplane evidence from original OSM contour vertices.

Even two genuinely consecutive OSM sides may represent a REAR corner, not
the corner visible in SOURCE. This is a necessary 2D wall visibility check,
never an inferred yaw, actual pixel match, 3D obstruction or proof of identity.
Unknown EXIF GPS precision and owner-approximate camera location remain
explicit limitations. No SOURCE or OSM candidate is selected here.
"""
from __future__ import annotations
import math


def _ring_halfplanes(geometry, origin, ring_index):
    from .identity_spatial_features import _point, _local
    rings=(geometry.get('rings') or [])
    if (type(ring_index) is not int or not 0<=ring_index<len(rings)):
        return None
    ring=rings[ring_index]
    if ring.get('role') not in {'outer',None} or ring.get('closed') is not True:
        return None
    pts=[_point(v) for v in (ring.get('points') or [])]
    if len(pts)<4 or any(p is None for p in pts) or pts[0]!=pts[-1]:
        return None
    local=[_local(p,origin) for p in pts]
    signed_area=sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(local,local[1:]))/2
    if not math.isfinite(signed_area) or abs(signed_area)<.01:
        return None
    output=[]
    for a,b in zip(local,local[1:]):
        length=math.dist(a,b)
        if not math.isfinite(length) or length<.01:
            output.append(None)
            continue
        # The local origin (0,0) is the camera coordinate, not model yaw.
        cross=(b[0]-a[0])*(-a[1])-(b[1]-a[1])*(-a[0])
        signed_left=cross/length
        outward=signed_left if signed_area<0 else -signed_left
        output.append(round(outward,2))
    return output


def nominal_exterior_sides(story, manifest, geometry, *, epsilon_m=2.0):
    """Return original OSM side indexes according to the nominal camera point.

    The 2m epsilon is a numerical guard, NOT estimated EXIF GPS accuracy.
    Inward result is a conditional contradiction, never authorization or
    exclusion of a physical body. Outer polygon halfplane is necessary
    rather than sufficient for true photographic visibility.
    """
    from .identity_spatial_features import _point
    basis=(manifest.get('camera') or {}).get('position_status')
    result={'position_basis':basis,'epsilon_m_not_measured_accuracy':epsilon_m,
        'outward_segments':[],'inward_segments':[],'near_boundary_segments':[],
        'explanation':'Original OSM outer-side camera halfplanes; neither camera yaw '
            'nor visible PHOTO wall is determined, GPS accuracy unknown.'}
    if basis not in {'original_exif','owner_approximate'}:
        return result
    origin=_point(manifest.get('anchor') or {}) or _point(story)
    if origin is None:
        return result
    for ri,_ring in enumerate((geometry or {}).get('rings') or []):
        distances=_ring_halfplanes(geometry,origin,ri)
        if distances is None:
            continue
        for si,dist in enumerate(distances):
            if dist is None:
                continue
            ref=[ri,si,dist]
            if dist>epsilon_m:
                result['outward_segments'].append(ref)
            elif dist< -epsilon_m:
                result['inward_segments'].append(ref)
            else:
                result['near_boundary_segments'].append(ref)
    return result


def observed_corner_halfplane(story, manifest, candidate_id, ring_index,
                              first_index, second_index, candidates=()):
    """Check one model-nominated joined OSM corner, NEVER replace it.

    Status corner_not_nominally_visible means at least one wall's front
    exterior halfplane faces away from the given camera anchor. Treat this
    as an uncertainty/contradiction, not a fabricated alternative corner.
    """
    from .identity_scene import scene_entries
    if (not isinstance(candidate_id,str) or not candidate_id.startswith('osm:')
            or any(type(i) is not int or i<0 for i in (
                ring_index,first_index,second_index))):
        return {'status':'invalid_original_wall_reference'}
    entry=next((e for e in scene_entries(story,list(candidates))
        if e.get('candidate_id')==candidate_id),None)
    if entry is None:
        return {'status':'candidate_unobserved'}
    geometry=entry.get('map_geometry') or {}
    result=nominal_exterior_sides(story,manifest,geometry)
    basis=result['position_basis']
    if basis not in {'original_exif','owner_approximate'}:
        return {'status':'camera_position_unavailable',
            'camera_position_basis':basis,'authorizes_identity':False}
    rings=geometry.get('rings') or []
    if ring_index>=len(rings) or first_index==second_index:
        return {'status':'invalid_original_wall_reference'}
    ring=rings[ring_index]
    last=len(ring.get('points') or [])-2
    direct=abs(first_index-second_index)==1
    wrapped=ring.get('closed') is True and {first_index,second_index}=={0,last}
    if not (direct or wrapped):
        return {'status':'not_actual_ring_neighbours',
            'camera_position_basis':basis,'authorizes_identity':False}
    distances={(ri,si):d for ri,si,d in [
        *result['outward_segments'],*result['inward_segments'],
        *result['near_boundary_segments']]}
    values=[distances.get((ring_index,si)) for si in (first_index,second_index)]
    if any(v is None for v in values):
        return {'status':'incomplete_original_outer_contour',
            'camera_position_basis':basis,'authorizes_identity':False}
    eps=result['epsilon_m_not_measured_accuracy']
    if any(v < -eps for v in values):
        status='corner_not_nominally_visible'
    elif any(abs(v)<=eps for v in values):
        status='nominal_camera_near_wall_plane_uncertain'
    else:
        status='both_walls_nominally_exterior'
    return {'status':status,'candidate_id':candidate_id,
        'observed_ring_index':ring_index,
        'observed_side_indices':[first_index,second_index],
        'signed_exterior_camera_distances_m':values,
        'camera_position_basis':basis,
        'epsilon_m_not_measured_gps_accuracy':eps,
        'authorizes_identity':False,
        'visibility_not_exhaustively_proven':True}
