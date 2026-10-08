"""Reproduce bounded scene diagnostics from retained OSM; no model calls.
Run: python reproduce.py. Optional --fetch downloads missing snapshots only.
Camera offsets are sensitivity scenarios, not estimated EXIF or GPS accuracy.
"""
from pathlib import Path
import argparse, gzip, hashlib, io, json, math, urllib.request
from PIL import Image
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle
import geometry_core as g

ROOT=Path(__file__).resolve().parent
CASES={126:dict(lat=55.077788,lon=21.888265,ids=[192219355,192219449,192219447],pose=(-8,0),front=1,side=2,limits=(-40,85,-25,100),labels=['A: северный сосед: OSM 5 / скриншот 7','B: главный контур, OSM 9-го Января, 5','C: комендатура, Искры, 7']),130:dict(lat=55.074106,lon=21.903648,ids=[193106197,193106188,193106171],pose=(0,-10),front=0,side=3,limits=(-55,105,-85,45),labels=['A: Гастелло, 24 — СЛЕВА в кадре','B: Гастелло, 22 — главный дом','C: Гастелло, 22А — в глубине СПРАВА'])}
COL=['#40926a','#db792b','#487cbb']
def minus(a,b):return(a[0]-b[0],a[1]-b[1])
def edge_info(c,idx,p):
 r=c['geometry_xy'];a,b=r[idx:idx+2];area=sum(x[0]*y[1]-y[0]*x[1] for x,y in zip(r,r[1:]));sgn=1 if area>0 else -1;L=math.dist(a,b);normal=(sgn*(b[1]-a[1])/L,-sgn*(b[0]-a[0])/L);m=((a[0]+b[0])/2,(a[1]+b[1])/2);v=minus(p,m)
 return dict(segment=idx,length_m=round(L,3),outward_bearing_deg=round(g.bearing(normal),3),signed_camera_distance_to_facade_plane_m=round(sum(x*y for x,y in zip(v,normal)),3),angle_deg=round(g.circular_interval([minus(a,p),minus(b,p)])['width_deg'],3),a=a,b=b)
def distance_pair(a,b):
 best=(float('inf'),None,None)
 for p in a['geometry_xy'][:-1]:
  for q,r in zip(b['geometry_xy'],b['geometry_xy'][1:]):
   d,z=g.closest_segment(p,q,r)
   if d<best[0]:best=(d,p,z)
 for p in b['geometry_xy'][:-1]:
  for q,r in zip(a['geometry_xy'],a['geometry_xy'][1:]):
   d,z=g.closest_segment(p,q,r)
   if d<best[0]:best=(d,z,p)
 return {'gap_m':round(best[0],3),'anchor_a':best[1],'anchor_b':best[2]}
def at_pose(d,cs,p,front,side):
 main=cs[1];angles=[g.bearing(minus(c['centroid_xy'],p)) for c in cs];yaw=angles[1];relative=[(a-yaw+180)%360-180 for a in angles];order=['ABC'[i] for i in sorted(range(3),key=lambda j:relative[j])]
 return {'offset_east_north_m':p,'camera_inside_footprints':[f"{c['osm_type']}/{c['osm_id']}" for c in d['buildings'] if g.in_polygons(p,c['polygons_xy'])],'centroid_bearings_ABC':angles,'yaw_to_B_centroid_deg':yaw,'left_to_right_centroid_order':order,'front':edge_info(main,front,p),'side':edge_info(main,side,p),'note':'Rays to centroids are positional diagnostics, not facade feature correspondences; 2D tests do not establish roof visibility.'}
def ranges(rows,key):return [round(min(r[key] for r in rows),2),round(max(r[key] for r in rows),2)] if rows else None
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--fetch',action='store_true');args=ap.parse_args()
 for mid,cfg in CASES.items():
  p=ROOT/f'photo-{mid}.osm';meta=json.loads((ROOT/f'photo-{mid}.fetch.json').read_text())
  if not p.exists() and p.with_suffix('.osm.gz').exists():p.write_bytes(gzip.decompress(p.with_suffix('.osm.gz').read_bytes()))
  if not p.exists() and args.fetch:
   with urllib.request.urlopen(meta['url'],timeout=25) as r:p.write_bytes(r.read())
  assert hashlib.sha256(p.read_bytes()).hexdigest()==meta['sha256']
  d=g.parse_osm(p,cfg['lat'],cfg['lon']);by={c['osm_id']:c for c in d['buildings']};cs=[by[x] for x in cfg['ids']];d['camera']['provenance']='owner approximate marker; NOT EXIF';(ROOT/f'photo-{mid}.geometry.json').write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
  primary=at_pose(d,cs,cfg['pose'],cfg['front'],cfg['side']);poses=[at_pose(d,cs,z,cfg['front'],cfg['side']) for z in [(0,0),cfg['pose'],(20,0),(40,-10),(-20,-10)]]
  sensitivity=[]
  for radius in [10,20,30]:
   valid=[];all_free=0
   for x in range(-radius,radius+1,2):
    for y in range(-radius,radius+1,2):
     if math.hypot(x,y)>radius:continue
     q=at_pose(d,cs,(x,y),cfg['front'],cfg['side'])
     if q['camera_inside_footprints']:continue
     all_free+=1
     if q['left_to_right_centroid_order']==list('ABC') and q['front']['signed_camera_distance_to_facade_plane_m']>0 and q['side']['signed_camera_distance_to_facade_plane_m']>0:valid.append({'x':x,'y':y,'yaw':q['yaw_to_B_centroid_deg']})
   sensitivity.append({'scenario_radius_m':radius,'grid_step_m':2,'not_probability':True,'outside_buildings_count':all_free,'ABC_order_and_both_facades_count':len(valid),'x_range_m':ranges(valid,'x'),'y_range_m':ranges(valid,'y'),'yaw_circular_interval':g.circular_interval([(math.sin(math.radians(v['yaw'])),math.cos(math.radians(v['yaw']))) for v in valid]) if valid else None})
  pairs={a+b:distance_pair(cs[i],cs[j]) for a,b,i,j in [('A','B',0,1),('B','C',1,2),('A','C',0,2)]}
  alternative=[]
  if mid==130:
   for wid,front,side,name in [(193106197,0,7,'main=24'),(193106162,1,0,'main=20A'),(193106188,0,3,'main=22')]:
    cc=by[wid];f=edge_info(cc,front,cfg['pose']);s=edge_info(cc,side,cfg['pose']);alternative.append({'hypothesis':name,'way_id':wid,'same_camera_scenario':cfg['pose'],'front':f,'west_side':s,'front_to_side_angular_ratio':round(f['angle_deg']/s['angle_deg'],3) if s['angle_deg'] else None})
  record={'photo':mid,'camera_provenance':'owner approximate location and screenshot; source EXIF has no GPS','owner_camera':{'lat':cfg['lat'],'lon':cfg['lon']},'osm':meta,'diagnostic_pose':primary,'scenario_poses':poses,'scenario_grid':sensitivity,'ABC':[{k:c[k] for k in ['osm_id','tags','centroid_xy','boundary_distance_m','centroid_bearing_deg','rank_boundary','geometry_complete']} for c in cs],'pair_gaps':pairs,'alternative_main_facades':alternative,'scope':'Local scene, physical footprints; addresses from OSM kept separate from owner screenshot. Not a global uniqueness proof.'}
  record['reviewed_identity']={'status':'accepted_local_geometry','target_way_id':cfg['ids'][1],'review_scope':'given owner-localized street scene and examined alternatives; not global uniqueness','neighbor_assignments':'strong spatial hypotheses; no independent facade references','address_conflict':mid==126}
  (ROOT/f'photo-{mid}.scene.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
  fig,(ax,txt)=plt.subplots(1,2,figsize=(15,10),gridspec_kw={'width_ratios':[2.2,1]});txt.axis('off')
  for road in d['roads']:
   for run in road.get('runs',[road['xy']]):
    if len(run)>1:ax.plot(*zip(*run),color='#bbc5ca',lw=2,zorder=0)
  for b in d['buildings']:
   for poly in b['polygons_xy']:
    ax.fill(*zip(*poly['outer']),color='#e3e4e6',ec='#a8adb4',lw=.8)
    for hole in poly['holes']:ax.fill(*zip(*hole),color='white')
   x,y=b['centroid_xy'];left,right,bottom,top=cfg['limits']
   if left<x<right and bottom<y<top and b['osm_id'] not in cfg['ids']:ax.text(x,y,b['tags'].get('addr:housenumber',''),fontsize=8,color='#555',ha='center')
  for i,b in enumerate(cs):
   ax.fill(*zip(*b['geometry_xy']),color=COL[i],alpha=.52,ec=COL[i],lw=2);x,y=b['centroid_xy'];ax.text(x,y,'ABC'[i],fontsize=17,ha='center',weight='bold')
   q=cfg['pose'];ax.annotate('',xy=(x,y),xytext=q,arrowprops={'arrowstyle':'->','color':COL[i],'lw':1.7});m=((x+q[0])/2,(y+q[1])/2);ax.text(*m,f"{'ABC'[i]} {primary['centroid_bearings_ABC'][i]:.1f}°",fontsize=8,color=COL[i],bbox={'fc':'white','alpha':.85,'ec':'none'})
  for k,color in [('front','#903000'),('side','#123c86')]:
   z=primary[k];ax.plot(*zip(z['a'],z['b']),color=color,lw=4);m=tuple((a+b)/2 for a,b in zip(z['a'],z['b']));ax.text(*m,f"{z['length_m']:.1f}m",fontsize=9,color=color,bbox={'fc':'white','alpha':.8,'ec':'none'})
  for pair in ['AB','BC']:
   z=pairs[pair]
   if z['gap_m']>0.1:ax.plot(*zip(z['anchor_a'],z['anchor_b']),color='#333',ls='--',lw=1);m=tuple((a+b)/2 for a,b in zip(z['anchor_a'],z['anchor_b']));ax.text(*m,f"{z['gap_m']:.1f}m",fontsize=8,bbox={'fc':'white','ec':'none','alpha':.8})
  for radius in [10,20]:ax.add_patch(Circle((0,0),radius,fill=False,color='#bd879f',ls=':',lw=1))
  ax.scatter(0,0,color='#ca2853',s=70,zorder=10);ax.annotate('C0: точка владельца\nприблизительно, НЕ EXIF',xy=(0,0),xytext=(-30,10),textcoords='offset points',fontsize=8,color='#9f1438')
  if cfg['pose']!=(0,0):ax.scatter(*cfg['pose'],marker='x',color='#762978',s=80,zorder=10);ax.annotate(f'C1: сценарий {cfg["pose"]} м\nне измеренная камера',xy=cfg['pose'],xytext=(12,-25),textcoords='offset points',fontsize=8,color='#762978')
  ax.scatter(40,-10,color='#777',marker='x',s=45);ax.text(42,-11,'C2: конкурирующая восточная поза',fontsize=7,color='#666')
  ax.set_xlim(cfg['limits'][:2]);ax.set_ylim(cfg['limits'][2:]);ax.set_aspect('equal');ax.grid(alpha=.18);ax.set_xlabel('Восток, метры от приблизительной точки владельца');ax.set_ylabel('Север, метры');ax.annotate('С',xy=(.96,.95),xytext=(.96,.87),xycoords='axes fraction',ha='center',arrowprops={'arrowstyle':'->','lw':2});ax.set_title(f'Кадр {mid}: физическая сцена; север сверху')
  lines=[f'КАДР {mid} — ГЕОМЕТРИЯ СЦЕНЫ','',*cfg['labels'],'',f"Азимут к центру B: {primary['yaw_to_B_centroid_deg']:.1f}°",'Порядок в кадре для C1: '+','.join(primary['left_to_right_centroid_order']),'',f"Фасад B: {primary['front']['length_m']:.2f} м; угол {primary['front']['angle_deg']:.1f}°",f"Бок B: {primary['side']['length_m']:.2f} м; угол {primary['side']['angle_deg']:.1f}°",*[f"Зазор {k}: {v['gap_m']:.2f} м" for k,v in pairs.items() if k!='AC'],'','Пунктир: сценарии 10/20 м,','не измеренная точность GPS.','Лучи к центрам ≠ точки на фасадах.','План OSM ≠ видимость по высотам.','',f"Полный пул: {len(d['building_groups'])} групп",'OSM © участники / ODbL 1.0','']
  if mid==126:lines+=['Конфликт номера северного соседа:','на скриншоте 7, в OSM 5.','Контур и перекрёсток согласуются.','Два соседних адресных контура','сохранены раздельно.']
  else:lines+=['C0 попадает в противоположный №21.','C1 — пример совместимой позы.','Слева в кадре здесь ВОСТОК карты.','№20А вне этого тесного кадра.','Проезд западнее B: фото и скриншот.','Сквозная проходимость не доказана.']
  txt.text(0,1,'\n'.join(lines),va='top',fontsize=10,linespacing=1.5);fig.tight_layout(rect=(0,0,1,.965));buf=io.BytesIO();fig.savefig(buf,format='png',dpi=140,bbox_inches='tight',pad_inches=.25);Image.open(io.BytesIO(buf.getvalue())).verify();tmp=ROOT/f'map-{mid}.pending.png';tmp.write_bytes(buf.getvalue());tmp.replace(ROOT/f'map-{mid}.png');plt.close(fig)
  print(json.dumps({'photo':mid,'primary_pose':primary,'pair_gaps':pairs,'alternatives':alternative,'sensitivity':sensitivity},ensure_ascii=False))
if __name__=='__main__':main()
