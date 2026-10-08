"""Offline reproduction of two bounded scene-plan diagnostics; no network/model calls."""
import json, math, hashlib, textwrap, gzip
from io import BytesIO
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Circle, Wedge
from osm_geometry_helper import parse_osm, closest_segment, bearing, circular_interval
ROOT=Path(__file__).resolve().parent
CASES={
106:dict(lat=54.7331882,lon=20.478384299722222,origin='original_EXIF',hfov=71.5915,
 ids={'A':150596899,'B':150596903,'C':66345237,'D':102519047,'E':150596900},
 names={'A':'88: основной передний объём','B':'86А / 86Б / 86: один контур','C':'84 / 82А / 82 / 80: возврат','D':'83: противоположный T-план','E':'90: продолжение линии A'},
 extent=(-88,72,-111,83),heading=55,fan_origin=(-2,0),fan_radius=70),
125:dict(lat=54.712702,lon=20.527291,origin='owner_approximate_location_NOT_EXIF',hfov=56.8117,
 ids={'A':152315116,'B':90894694,'C':90894945,'D':90894700,'E':90894651},
 names={'A':'53–57: один OSM footprint','B':'59 / 61 / 63 / 65 / 67: входы','C':'Ближайший отдельный прямоугольник','D':'37–43 / Гражданская: альтернатива','E':'71: следующий отдельный объём'},
 extent=(-133,112,-70,132),heading=298,fan_origin=(0,0),fan_radius=112)}
COL={'A':'#336eab','B':'#df923c','C':'#689c7b','D':'#9472a8','E':'#81919e'}
def unit(a,b):
 d=math.dist(a,b);return ((b[0]-a[0])/d,(b[1]-a[1])/d)
def dot(v,u):return sum(a*b for a,b in zip(v,u))
def sub(a,b):return(a[0]-b[0],a[1]-b[1])
def signed_line_distance(p,a,b):
 u=unit(a,b);return dot(sub(p,a),(-u[1],u[0]))
def line_distance(p,a,b):
 u=unit(a,b);return abs(dot(sub(p,a),(-u[1],u[0])))
def analyze(n,cfg):
 raw=ROOT/f'photo-{n}.osm'
 raw_bytes=raw.read_bytes() if raw.exists() else gzip.decompress(Path(str(raw)+'.gz').read_bytes())
 d=parse_osm(BytesIO(raw_bytes),cfg['lat'],cfg['lon'])
 idx={c['osm_id']:c for c in d['buildings']};v={k:idx[i] for k,i in cfg['ids'].items()}
 ring={k:c['polygons_xy'][0]['outer'] for k,c in v.items()}
 if n==106:
  ar=ring['A'];br=ring['B'];cr=ring['C'];dr=ring['D']
  p,q=ar[3],ar[2];back=br[6]
  measurements={'A_street_facade_length_m':math.dist(ar[3],ar[0]),'A_return_depth_m':math.dist(p,q),
   'B_setback_from_A_front_line_at_86B_m':line_distance(back,ar[3],ar[0]),
   'A_B_small_mapping_gap_m':math.dist(ar[2],br[4]),
   'courtyard_front_opening_A_to_C_m':math.dist(ar[3],cr[2]),
   'D_east_facing_short_facade_m':math.dist(dr[7],dr[0]),
   'D_right_return_depth_m':math.dist(dr[7],dr[6]),
   'D_long_south_facade_m':math.dist(dr[0],dr[1]),
   'D_south_front_min_camera_shift_south_m':abs(dr[0][1]-(dr[1][1]-dr[0][1])/(dr[1][0]-dr[0][0])*dr[0][0]),
   'D_south_front_min_camera_shift_anydirection_m':line_distance((0,0),dr[0],dr[1]),
   'source_complete_main_storeys_observed':3,'source_attic_observed':True,
   'A_osm_levels':v['A']['tags'].get('building:levels'),'D_osm_levels':v['D']['tags'].get('building:levels')}
  anchors={'A_to_B':[ar[0],ar[3],br[5]],'D_to_D_return':[dr[0],dr[7],dr[5]]}
  arrow=(p,q);caption='Поворот вглубь ≈12.5 м'
 else:
  ar=ring['A'];br=ring['B'];er=ring['E'];dr=ring['D']
  p,q=ar[8],ar[7]
  measurements={'A_street_frontage_m':math.dist(ar[10],ar[8]),
   'A_to_B_return_segment_m':math.dist(p,q),'B_setback_normal_to_A_frontage_m':line_distance(q,ar[10],ar[8]),
   'A_B_shared_coordinate':q,'A_B_shared_node_present':q in br,'A_B_shared_node_ids':[1055390458],
   'right_E_advance_vs_B_frontage_m':-signed_line_distance(er[1],br[9],br[7]),'A_advance_vs_left_D_frontage_m':-signed_line_distance(ar[10],dr[0],dr[-2]),
   'B_frontage_axis_deg_mod180':bearing(sub(br[8],br[9]))%180,
   'A_frontage_axis_deg_mod180':bearing(sub(ar[8],ar[10]))%180,
   'C_rectangle_edge_lengths_m':[math.dist(a,b) for a,b in zip(ring['C'],ring['C'][1:])],
   'arch_or_building_passage_mapped':False,'observed_arch_in_source':True,
   'historical_single_building_proved_by_osm':False,
   'identity_scope':'One mapped physical footprint with address range53–57; OSM alone does not establish historical unity or the exact numbered entrance of the pictured arch.'}
  anchors={'A_to_B':[ar[10],ar[8],br[9]],'B_to_E':[br[9],br[7],er[1]]}
  arrow=(p,q);caption='Правый объём глубже ≈9.7 м'
 features=[]
 for letter,x in v.items():
  g=next(g for g in d['building_groups'] if g['group_key']==x['group_key'])
  features.append({'label':letter,'osm_ref':f"way/{x['osm_id']}",'tags':x['tags'],
   'linked_addresses':x.get('linked_addresses',[]),'boundary_distance_m':x['boundary_distance_m'],
   'bbox_center_distance_m':x['bbox_center_distance_m'],'boundary_rank':g['rank_group_boundary'],
   'centroid_bearing_deg':x['centroid_bearing_deg'],'geometry_complete':True})
 scenarios=[]
 for name,points in anchors.items():
  for camera in [(0,0),(-2,0),(-10,0),(10,0),(0,-10),(0,10),(-30,0),(30,0),(0,-30),(0,30)]:
   interval=circular_interval([sub(p,camera) for p in points])
   scenarios.append({'hypothesis':name,'camera_offset_xy_m':camera,'anchor_span_deg':interval['width_deg'],
     'fits_approx_hfov':interval['width_deg']<=cfg['hfov'],
     'scope':'Only selected ground-plan anchor angular span; not whole-photo visibility or calibrated identity likelihood.'})
 result={'photo':n,'camera':{'latitude':cfg['lat'],'longitude':cfg['lon'],'source':cfg['origin'],'heading_measured':False,'accuracy_measured':False},
  'approx_hfov_deg':cfg['hfov'],'raw_sha256':hashlib.sha256(raw_bytes).hexdigest(),
  'candidate_pool_complete_groups':len(d['building_groups']),'unresolved_buildings':len(d['unresolved_buildings']),
  'measurements':measurements,'selected_candidates':features,'bounded_anchor_scenarios':scenarios,
  'computed_uniqueness':False,'whole_candidate_pool_pose_search_done':False,
  'no_external_reference_requirement':'An adequately discriminative photo+map match may close identity without fetching an exterior photo. This diagnostic does not claim an exhaustive uniqueness proof.',
  'decision':{'status':'accepted_photo_plus_map' if n==106 else 'accepted_geometry_with_owner_location','accepted_physical_ref':f"way/{cfg['ids']['A']}",'external_photo_required':False,'scope':'106 uses source3complete storeys and mapped3vs2storey alternative; pureplan retainsD.125 accepts address-range footprint53–57, not exact arch address57.','residual_uncertainty':'No exhaustive continuous camera/heading search; missing or wrong map details can reopen an alternative.'},
  'map_sector':{'role':'illustrative candidate-consistent direction, not EXIF heading','origin_offset_xy_m':cfg['fan_origin'],'heading_deg':cfg['heading'],'hfov_deg':cfg['hfov']}}
 for k,value in measurements.items():
  if isinstance(value,float):measurements[k]=round(value,3)
 (ROOT/f'photo-{n}.proof.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 draw(n,cfg,d,v,arrow,caption)
 return result

def draw(n,cfg,d,v,arrow,caption):
 plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10})
 fig=plt.figure(figsize=(15.5,10.5),facecolor='white')
 ax=fig.add_axes([.065,.17,.59,.70]);notes=fig.add_axes([.72,.18,.26,.69]);notes.axis('off')
 x0,x1,y0,y1=cfg['extent'];ax.set(xlim=(x0,x1),ylim=(y0,y1),aspect='equal')
 ids=set(cfg['ids'].values())
 for b in d['buildings']:
  if b.get('parent_ref'):continue
  for p in b['polygons_xy']:
   ax.add_patch(Polygon(p['outer'],facecolor='#e8ecef',edgecolor='#b4bec5',lw=.6,zorder=1))
 for r in d['roads']:
  for segment in r.get('geometry_segments_xy',[r['xy']]):
   if len(segment)<2:continue
   xs,ys=zip(*segment);major=r['tags'].get('highway') in ('residential','secondary','tertiary')
   ax.plot(xs,ys,color='#c7cdd2',lw=3 if major else 1,zorder=2)
 for letter,b in v.items():
  for p in b['polygons_xy']:ax.add_patch(Polygon(p['outer'],facecolor=COL[letter],edgecolor='#425668',alpha=.5,lw=1.3,zorder=3))
  cx,cy=b['centroid_xy']
  if x0<cx<x1 and y0<cy<y1:ax.text(cx,cy,letter,ha='center',va='center',weight='bold',fontsize=14,
    bbox=dict(boxstyle='circle,pad=.22',facecolor='white',edgecolor=COL[letter],lw=1.5),zorder=10)
 for rad in (10,30):ax.add_patch(Circle((0,0),rad,fill=False,edgecolor='#738fa4',ls='--',lw=.8,zorder=4))
 h=cfg['heading'];f=cfg['hfov'];origin=cfg['fan_origin'];rad=cfg['fan_radius']
 ax.add_patch(Wedge(origin,rad,90-h-f/2,90-h+f/2,color='#5b9dc1',alpha=.11,zorder=4))
 for b in (h-f/2,h+f/2):
  q=(origin[0]+rad*math.sin(math.radians(b)),origin[1]+rad*math.cos(math.radians(b)))
  ax.plot([origin[0],q[0]],[origin[1],q[1]],ls=':',lw=1.2,color='#3e7999',zorder=5)
 ax.scatter([0],[0],marker='*',s=170,c='#182e42',edgecolor='white',zorder=12)
 ax.annotate('Камера EXIF' if n==106 else 'Точка владельца\n(не EXIF)',(0,0),xytext=((-65,-38) if n==106 else (25,-42)),textcoords='offset points',
  fontsize=9,color='#182e42',arrowprops={'arrowstyle':'-','color':'#182e42'},zorder=12,
  bbox={'facecolor':'white','edgecolor':'none','alpha':.9,'pad':2})
 p,q=arrow;ax.annotate('',q,p,arrowprops={'arrowstyle':'<->','color':'#a12927','lw':2},zorder=11)
 xy=((p[0]+q[0])/2,(p[1]+q[1])/2)
 textloc=(48,58) if n==106 else(-30,-37)
 ax.annotate(caption,xy,xytext=textloc,fontsize=9,ha='center',color='#9a2525',
   arrowprops={'arrowstyle':'-','color':'#9a2525'},bbox={'facecolor':'white','edgecolor':'#dca8a8','boxstyle':'round,pad=.4'},zorder=13)
 ax.grid(alpha=.2);ax.set_xlabel('Восток от точки камеры, м');ax.set_ylabel('Север от точки камеры, м')
 ax.annotate('N',xy=(.06,.94),xytext=(.06,.83),xycoords='axes fraction',ha='center',weight='bold',fontsize=13,
  arrowprops={'arrowstyle':'-|>','lw':1.6,'color':'#243c51'})
 sx=x0+12;sy=y0+10;ax.plot([sx,sx+20],[sy,sy],lw=3,color='#243c51');ax.text(sx+10,sy+4,'20 м',ha='center',fontsize=9)
 notes.text(0,1,'Проверяемая конфигурация',weight='bold',fontsize=13,va='top',color='#17374e')
 yy=.91
 for letter in cfg['ids']:
  text='\n'.join(textwrap.wrap(f"{letter} · {cfg['names'][letter]}",width=28))
  notes.text(0,yy,text,fontsize=10.5,weight='bold',color=COL[letter],va='top')
  lines=text.count('\n')+1;yy-=lines*.037
  notes.text(0,yy,f"way/{cfg['ids'][letter]} · {v[letter]['boundary_distance_m']:.1f} м",fontsize=9,color='#596874',va='top');yy-=.072
 note=('A → поворот на 12.5 м → B.\nB объединяет три адреса входов.\nD — реальная альтернатива плана;\nу него 2 этажа в OSM против 3\nполных этажей на фото.' if n==106 else 'A → отступ 9.7 м → B.\nA и B имеют общую вершину.\nАрка видна на фото, но\nне закодирована проходом OSM.\nScope A: 53–57, не только 57.')
 note='\n'.join('\n'.join(textwrap.wrap(line,width=34)) for line in note.split('\n'))
 notes.text(0,yy-.005,note,fontsize=9.5,color='#273b49',va='top',linespacing=1.6)
 fig.text(.065,.955,f'Фото {n} · Фасад, отступ и пространственные альтернативы',fontsize=17,weight='bold',color='#17374e')
 fig.text(.065,.915,f"{cfg['lat']:.7f}, {cfg['lon']:.7f} · направление съёмки неизвестно",fontsize=10,color='#657684')
 fig.text(.065,.085,'Сектор — пример направления для видимой части фасада; не измеренный компас. Круги 10/30 м — сценарии, не точность GPS.',fontsize=9,color='#657684')
 fig.text(.065,.058,'Цвета A–E обозначают роли при сравнении, не автоматическую вероятность. © OpenStreetMap contributors · ODbL1.0',fontsize=9,color='#657684')
 if n==106:fig.text(.065,.031,'Начало сектора смещено на 2 м к западу: один допустимый пример при неизвестной ошибке EXIF.',fontsize=9,color='#657684')
 fig.canvas.draw();renderer=fig.canvas.get_renderer();fw,fh=fig.canvas.get_width_height()
 overflow=[]
 for artist in [*fig.texts,*notes.texts]:
  box=artist.get_window_extent(renderer)
  if box.x0<0 or box.y0<0 or box.x1>fw or box.y1>fh:overflow.append(artist.get_text())
 (ROOT/f'map-{n}.layout.json').write_text(json.dumps({'figure_text_overflow':overflow,'checked_figure_and_sidebar_texts':len(fig.texts)+len(notes.texts)},ensure_ascii=False,indent=2))
 fig.savefig(ROOT/f'map-{n}.png',dpi=160);plt.close(fig)

if __name__=='__main__':
 result=[analyze(n,cfg) for n,cfg in CASES.items()]
 (ROOT/'metrics.json').write_text(json.dumps({'cases':result,'resource_use':{'new_osm_http_requests':1,'reused_osm_snapshots':1,'external_photo_searches':0,'product_model_calls':0}},ensure_ascii=False,indent=2))
 print(json.dumps([{'photo':r['photo'],'decision':r['decision'],'measurements':r['measurements']} for r in result],ensure_ascii=False))
