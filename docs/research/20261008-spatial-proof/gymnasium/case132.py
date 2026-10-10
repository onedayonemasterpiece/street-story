"""Reproduce bounded, offline street-termination diagnostics for SOURCE 132.

Input is a complete cached OSM API map, not a prepared identity answer.
The SOURCE-to-street interpretation is a human observation; this script measures
its consequences and does not pretend to run a general visual recognizer.
"""
import gzip
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import time

HERE = Path(__file__).resolve().parent
HELPERS = [HERE.parents[1] / 'research/geometry_research.py',
           HERE.parents[1] / '20261008-gps-osm/research/geometry_research.py']
helper = next((p for p in HELPERS if p.exists()), None)
if helper is None:
    raise FileNotFoundError('Keep this package alongside 20261008-gps-osm')
spec = importlib.util.spec_from_file_location('geometry_research', helper)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def cross(a, b):
    return a[0]*b[1]-a[1]*b[0]


def ray_hits(origin, direction, buildings):
    hits = []
    for c in buildings:
        nearest = None
        for poly in c['polygons_xy']:
            for ring in [poly['outer'], *poly['holes']]:
                for a, b in zip(ring, ring[1:]):
                    edge = (b[0]-a[0], b[1]-a[1])
                    delta = (a[0]-origin[0], a[1]-origin[1])
                    den = cross(direction, edge)
                    if abs(den) < 1e-9:
                        continue
                    t, u = cross(delta, edge)/den, cross(delta, direction)/den
                    if t > 1e-6 and -1e-9 <= u <= 1+1e-9 and (nearest is None or t < nearest):
                        nearest = t
        if nearest is not None:
            hits.append({'ref': f"{c['osm_type']}/{c['osm_id']}",
                         'distance_m': round(nearest, 3),
                         'point_xy': [origin[i]+nearest*direction[i] for i in range(2)]})
    return sorted(hits, key=lambda h: h['distance_m'])


def run():
    started = time.monotonic()
    raw = HERE / 'photo-132.osm'
    if raw.exists():
        d = g.parse_osm(raw, 55.079386, 21.889776)
    else:
        with tempfile.TemporaryDirectory() as td:
            xml = Path(td)/'map.osm'
            xml.write_bytes(gzip.decompress((HERE/'photo-132.osm.gz').read_bytes()))
            d = g.parse_osm(xml, 55.079386, 21.889776)
    target = next(c for c in d['buildings'] if c['osm_id'] == 192217077)
    approach = next(r for r in d['roads'] if r['osm_id'] == 67826885)
    junction, upstream = approach['xy'][:2]
    length = math.dist(junction, upstream)
    direction = [(junction[i]-upstream[i])/length for i in range(2)]
    normal = [-direction[1], direction[0]]
    cam_road_distance, cam_on_axis = g.closest_segment((0, 0), junction, upstream)
    forward = ray_hits(cam_on_axis, direction, d['buildings'])
    reverse = ray_hits(cam_on_axis, [-v for v in direction], d['buildings'])
    facade = next(e for e in target['front_facing_edges_2d'] if e['segment'] == 2)
    point = facade['midpoint_xy']
    street_candidates = []
    for road in d['roads']:
        if road['tags'].get('highway') not in ('residential','unclassified','tertiary','secondary','primary','service'):
            continue
        distances = [g.closest_segment((0,0),a,b)[0] for a,b in zip(road['xy'],road['xy'][1:])]
        if distances:
            street_candidates.append({'ref': f"way/{road['osm_id']}", 'name': road['tags'].get('name'),
                                      'distance_m': round(min(distances),3)})
    street_candidates.sort(key=lambda r:r['distance_m'])
    blockers = [f"{c['osm_type']}/{c['osm_id']}" for c in d['buildings'] if c is not target
                and g.ray_crosses_filled_area((0,0), point, c['polygons_xy'])]
    # Two explicit, reproducible stress scenarios, not a GPS accuracy estimate.
    # A 2m grid in discs around the owner hint, restricted to a 10m corridor
    # of the photo-compatible approach and to points outside building interiors.
    scenarios = []
    for radius in (10,30):
        count=0; hit_counts={}; heading=[]
        for x in range(-radius,radius+1,2):
            for y in range(-radius,radius+1,2):
                p=(x,y)
                if math.hypot(x,y)>radius or g.closest_segment(p,junction,upstream)[0]>10:
                    continue
                if any(g.in_polygons(p,c['polygons_xy']) for c in d['buildings']):
                    continue
                hits=ray_hits(p,direction,d['buildings'])
                ref=hits[0]['ref'] if hits else 'no_hit_in_snapshot'
                hit_counts[ref]=hit_counts.get(ref,0)+1
                count+=1
                heading.append(g.bearing((point[0]-x,point[1]-y)))
        scenarios.append({'position_disc_radius_m':radius,'grid_step_m':2,'street_corridor_halfwidth_m':10,
                          'positions':count,'first_footprint_parallel_to_approach':hit_counts,
                          'heading_to_central_projection_range_deg':[round(min(heading),2),round(max(heading),2)],
                          'meaning':'conditional stress scenario; not a measured GPS error or probability'})
    west = next(c for c in d['buildings'] if c['osm_id'] == 192220354)
    metrics = {
        'photo':132,'camera':{'lat':55.079386,'lon':21.889776,'source':'owner_approximate_hint','exif_gps':False},
        'identity':{'osm_ref':'way/192217077','address_from_osm':'Советск, Школьная улица, 13','name_from_osm':'Гимназия № 1'},
        'source_observation':'view along an approach street across a T junction to a broad frontal facade with central projection',
        'map_condition':'camera lies near the last Smolenskaya segment; forward is toward its T junction with Shkolnaya',
        'counts':d['data_quality'],
        'camera_distance_to_approach_axis_m':round(cam_road_distance,3),
        'camera_distance_to_junction_m':round(math.hypot(*junction),3),
        'street_axis_bearing_toward_junction_deg':round(g.bearing(direction),3),
        'nearest_streets':street_candidates[:7],
        'target_boundary_distance_m':target['boundary_distance_m'],
        'target_boundary_rank':target['rank_boundary'],'target_bbox_center_rank':target['rank_bbox_center'],
        'target_plan_angular_width_deg':target['bearing_interval']['width_deg'],
        'opposite_direction_alternative':{'ref':'way/192220354','address_from_osm':'Интернациональная улица, 1',
             'boundary_distance_m':west['boundary_distance_m'],
             'plan_angular_width_deg':west['bearing_interval']['width_deg'],
             'comparison':'about 200m away and only 9.23 degrees wide, unlike the broad near facade in SOURCE; depends on ordinary uncropped framing'},
        'central_projection_width_m':facade['length_m'],
        'central_projection_center_distance_m':round(math.hypot(*point),3),
        'central_projection_center_bearing_deg':round(g.bearing(point),3),
        'facade_axis_deg_mod180':facade['axis_deg_mod180'],
        'forward_axis_first_hits':forward[:4],
        'reverse_axis_first_hits':reverse[:4],
        'central_projection_ground_plan_blockers':blockers,
        'conditional_position_scenarios':scenarios,
        'limits':['zero unresolved OSM rings is not proof of complete real-world mapping',
                  'street interpretation comes from SOURCE; exact compass yaw is not in EXIF',
                  '2D first intersection is a plan relation, not a height-aware visibility proof',
                  'grid is conditional and finite; it does not exhaust every possible pose'],
        'geometry_conclusion':'accepted physical identity from SOURCE plus street termination geometry; external reference photo is optional',
    }
    (HERE/'metrics-132.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n')
    render(d,target,approach,cam_on_axis,forward[0],metrics)
    print(json.dumps({'case':132,'offline_analysis_and_render_seconds':round(time.monotonic()-started,3),
                      'scenarios':scenarios,'output':'map-132.png'},ensure_ascii=False))


def render(d,target,approach,axispoint,hit,m):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon,Circle
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10})
    fig,(ax,side)=plt.subplots(1,2,figsize=(13.6,8.4),gridspec_kw={'width_ratios':[1.55,1]})
    fig.subplots_adjust(left=.065,right=.98,top=.86,bottom=.12,wspace=.17)
    ax.set(xlim=(-90,150),ylim=(-100,110),aspect='equal',xlabel='Восток от примерной камеры, м',ylabel='Север от примерной камеры, м')
    ax.set_facecolor('#f8fafb');ax.grid(color='#e2e8ef',lw=.6,zorder=0)
    for c in d['buildings']:
        selected=c is target
        for p in c['polygons_xy']:
            ax.add_patch(Polygon(p['outer'],closed=True,facecolor='#ffce80' if selected else '#d9e1e8',
                                 edgecolor='#b06b11' if selected else '#9ba9b5',lw=1.8 if selected else .7,zorder=2))
        x,y=c['centroid_xy']
        if -85<x<145 and -95<y<105:
            label='A' if selected else c['tags'].get('addr:housenumber','')
            if label:ax.text(x,y,label,ha='center',va='center',fontsize=13 if selected else 8,
                             weight='bold' if selected else 'normal',color='#533e16' if selected else '#526471',zorder=5)
    for r in d['roads']:
        xs,ys=zip(*r['xy'])
        ax.plot(xs,ys,color='white',lw=5,zorder=3)
        ax.plot(xs,ys,color='#8798a6',lw=1.3,zorder=3)
    for text,pos,angle in [('Смоленская',(-65,-7),6),('Школьная',(45,75),-65),('Студенческая',(103,54),5),('Лизы Чайкиной',(113,-55),14)]:
        ax.text(*pos,text,rotation=angle,fontsize=8,color='#435769',zorder=8,bbox={'facecolor':'white','edgecolor':'none','pad':1})
    ax.add_patch(Circle((0,0),30,fill=False,edgecolor='#4b87b9',lw=1,ls='--',zorder=5))
    ax.scatter([0],[0],s=150,marker='*',color='#174a74',zorder=10)
    ax.annotate('Примерная камера',(0,0),xytext=(-78,-45),arrowprops={'arrowstyle':'-','color':'#174a74'},color='#174a74',fontsize=9,zorder=10)
    ax.annotate('Сценарий 30 м',(-24,-18),xytext=(-81,-67),arrowprops={'arrowstyle':'-','color':'#4b87b9'},color='#4b87b9',fontsize=8,zorder=10)
    junction=approach['xy'][0]
    ax.scatter([junction[0]],[junction[1]],s=45,color='#4c547d',zorder=9)
    ax.annotate('Т-узел',junction,xytext=(4,-35),arrowprops={'arrowstyle':'-','color':'#4c547d'},color='#4c547d',fontsize=9,zorder=9)
    q=hit['point_xy']
    ax.plot([axispoint[0],q[0]],[axispoint[1],q[1]],color='#d07314',lw=2.4,zorder=7)
    ax.scatter([q[0]],[q[1]],s=55,marker='o',color='#d07314',zorder=9)
    center=next(e['midpoint_xy'] for e in target['front_facing_edges_2d'] if e['segment']==2)
    ax.plot([0,center[0]],[0,center[1]],color='#2a729f',lw=1.5,ls='--',zorder=7)
    ax.annotate('N',xy=(.06,.95),xytext=(.06,.83),xycoords='axes fraction',ha='center',weight='bold',
                arrowprops={'arrowstyle':'-|>','color':'#24475d'},color='#24475d')
    side.axis('off')
    blocks=[('Что связывает кадр с планом',
             'Улица перед камерой заканчивается\nпоперечной улицей. Сразу за ней —\nширокий фасад с выступом по центру.'),
            ('A · Школьная, 13',
             'way/192217077 · Гимназия № 1\nПо расстоянию до контура: только № 10.\nДевять ближних домов не завершают\nэтот видимый уличный коридор.'),
            ('Измеримая связь',
             f"Камера → ось Смоленской: {m['camera_distance_to_approach_axis_m']:.1f} м\nДо следующей улицы: {m['nearest_streets'][1]['distance_m']:.1f} м\nПродолжение оси: {m['street_axis_bearing_toward_junction_deg']:.1f}°\nПервое пересечение контура — здание A.\nДо границы A: {m['target_boundary_distance_m']:.1f} м."),
            ('Достаточное основание',
             'SOURCE + положение камеры + Т-узел\nдают физическую идентификацию.\nВнешняя фотография может дополнить\nрезультат, но не обязана его открывать.'),
            ('Границы вывода',
             'Координаты даны владельцем примерно.\nПунктирный круг — сценарий смещения,\nне измеренная точность GPS.\nПроверка 2D не измеряет высоты.')]
    y=1
    for title,body in blocks:
        side.text(0,y,title,ha='left',va='top',fontsize=11.5,weight='bold',color='#203e55')
        side.text(0,y-.046,body,ha='left',va='top',fontsize=10,color='#455766',linespacing=1.43)
        y-=.21 if title!='Измеримая связь' else .24
    fig.suptitle('132 · Дом замыкает видимую улицу',x=.07,ha='left',y=.97,fontsize=20,weight='bold',color='#183a52')
    fig.text(.07,.91,'Диагностическая карта OSM: оранжевая линия — продолжение оси улицы, синяя — взгляд на центральный выступ.',fontsize=10,color='#536575')
    fig.text(.07,.035,'© OpenStreetMap contributors · ODbL 1.0 · Snapshot 08.10.2026 · Положение камеры: подсказка владельца\nПодсветка A предназначена для разбора результата; при слепой проверке подпись ответа и подсветку нужно убрать.',fontsize=8,color='#5f6d7a')
    fig.savefig(HERE/'map-132.png',dpi=160,facecolor='white',metadata={'Software':'Street Story bounded spatial research'})
    plt.close(fig)


if __name__=='__main__':
    run()
