"""Render cached OSM research geometry with matplotlib; no network calls.

Example:
  python research/plot_geometry.py research/osm/photo-104.geometry.json \
      --target relation/3665416 --radius 160
"""
import argparse
import json
import math
from pathlib import Path
import textwrap

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.path import Path as MplPath
from matplotlib.patches import Circle, PathPatch
from matplotlib.lines import Line2D
try:
    from .geometry_research import closest_segment,inside,bearing,segments
except ImportError:
    from geometry_research import closest_segment,inside,bearing,segments


def signed_area(ring):
    return sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(ring,ring[1:]))/2


def polygon_path(polygons):
    """Orient exterior/hole rings oppositely for matplotlib's nonzero fill."""
    vertices=[];codes=[]
    for polygon in polygons:
        for hole,ring in enumerate([polygon['outer'],*polygon['holes']]):
            coords=list(ring)
            if (signed_area(coords)>0) == bool(hole):
                coords.reverse()
            vertices.extend(coords)
            codes.extend([MplPath.MOVETO]+[MplPath.LINETO]*(len(coords)-2)+[MplPath.CLOSEPOLY])
    return MplPath(vertices,codes)


def label_address(tags):
    street=tags.get('addr:street','')
    number=tags.get('addr:housenumber','')
    return f'{street}, {number}'.strip(', ')


def render(data,output,target=None,radius=180,title=None):
    index={f"{c['osm_type']}/{c['osm_id']}":c for c in data['buildings']}
    groups=data.get('building_groups',[])
    if target and '/' not in target:
        matches=[key for key in index if key.rsplit('/',1)[-1]==target]
        if len(matches)!=1:raise ValueError(f'Ambiguous or missing target: {target}')
        target=matches[0]
    if target and target not in index:
        raise ValueError(f'Target has no complete geometry in this snapshot: {target}')
    if target:target=index[target].get('group_key',target)
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':9,'axes.unicode_minus':False})
    fig=plt.figure(figsize=(12,9.6),facecolor='white')
    ax=fig.add_axes([0.065,0.16,0.68,0.72],facecolor='#fafaf7')
    sidebar=fig.add_axes([0.785,0.155,0.2,0.73]);sidebar.axis('off')
    ax.set_xlim(-radius,radius);ax.set_ylim(-radius,radius);ax.set_aspect('equal')
    ax.set_xlabel('Восток от камеры, м',labelpad=8,color='#4b5563')
    ax.set_ylabel('Север от камеры, м',labelpad=8,color='#4b5563')
    ax.tick_params(colors='#6b7280',labelsize=8)
    for spine in ax.spines.values():spine.set_color('#d1d5db')
    ax.grid(color='#e6e8e8',linewidth=0.55,zorder=0)

    # Parts remain in JSON. Drawing one outline per group keeps the map legible.
    for group in groups:
        key=group['representative_ref'];candidate=index[key]
        selected=key==target
        ax.add_patch(PathPatch(polygon_path(candidate['polygons_xy']),
            facecolor='#f2b55d' if selected else '#c8d4de',
            edgecolor='#9f5411' if selected else '#72869a',
            linewidth=1.8 if selected else 0.7,zorder=3 if selected else 1))
    drawn_context=False
    if target:
        area_index={f"{a['osm_type']}/{a['osm_id']}":a for a in data.get('context_areas',[])}
        for association in index[target].get('context_associations',[]):
            area=area_index.get(f"{association['osm_type']}/{association['osm_id']}")
            if area and area.get('geometry_complete'):
                ax.add_patch(PathPatch(polygon_path(area['polygons_xy']),fill=False,
                    edgecolor='#6c956b',linewidth=1.5,linestyle=(0,(5,3)),zorder=3))
                drawn_context=True

    road_labels={}
    for road in data['roads']:
        tags=road['tags'];kind=tags.get('highway','')
        width=2.5 if kind in ('primary','secondary','tertiary','residential','unclassified') else 1.2
        for coords in road.get('geometry_segments_xy',[road['xy']]):
            if len(coords)<2:continue
            xs,ys=zip(*coords)
            ax.plot(xs,ys,color='white',linewidth=width+2,zorder=2)
            ax.plot(xs,ys,color='#aeb7be',linewidth=width,zorder=2)
            name=tags.get('name')
            if name:
                visible=[(a,b) for a,b in zip(coords,coords[1:])
                         if all(abs(v)<radius*.93 for v in ((a[0]+b[0])/2,(a[1]+b[1])/2))]
                if visible:
                    a,b=max(visible,key=lambda pair:math.dist(*pair));length=math.dist(a,b)
                    if length>road_labels.get(name,(0,None,None))[0]:road_labels[name]=(length,a,b)
    for name,(length,a,b) in road_labels.items():
        if length<25:continue
        angle=math.degrees(math.atan2(b[1]-a[1],b[0]-a[0]))
        if angle>90:angle-=180
        if angle<-90:angle+=180
        ax.text((a[0]+b[0])/2,(a[1]+b[1])/2,name,fontsize=7.5,color='#6b7280',
                ha='center',va='center',rotation=angle,rotation_mode='anchor',zorder=4,
                bbox={'facecolor':'white','edgecolor':'none','pad':1,'alpha':0.7},clip_on=True)

    for uncertainty,color in ((60,'#6486bb'),(30,'#4672a8'),(10,'#245a92')):
        ax.add_patch(Circle((0,0),uncertainty,facecolor='#70a3da' if uncertainty==60 else 'none',
                    edgecolor=color,alpha=.12 if uncertainty==60 else .65,linewidth=1.1,
                    linestyle=(0,(4,3)),zorder=5))
    ax.scatter([0],[0],s=190,marker='*',c='#172f4a',edgecolors='white',linewidths=1,zorder=10)
    ax.annotate('Камера',(0,0),xytext=(8,-14),textcoords='offset points',
                color='#172f4a',weight='bold',fontsize=9,zorder=11)
    ax.annotate('N',xy=(.06,.955),xytext=(.06,.885),xycoords='axes fraction',
                textcoords='axes fraction',ha='center',va='center',weight='bold',fontsize=12,
                arrowprops={'arrowstyle':'-|>','color':'#334155','lw':1.6},zorder=12)
    scale=50 if radius<200 else 100
    sx=-radius*.88;sy=-radius*.9
    ax.plot([sx,sx+scale],[sy,sy],color='#334155',lw=2,zorder=12)
    ax.plot([sx,sx],[sy-2,sy+2],color='#334155',lw=1.3,zorder=12)
    ax.plot([sx+scale,sx+scale],[sy-2,sy+2],color='#334155',lw=1.3,zorder=12)
    ax.text(sx+scale/2,sy+5,f'{scale} м',ha='center',color='#334155',fontsize=8,zorder=12)

    shortlist=[g for g in groups if g['boundary_distance_m']<radius][:8]
    if target and all(g['group_key']!=target for g in shortlist):
        shortlist.append(next(g for g in groups if g['group_key']==target))
    marked={g['group_key'] for g in shortlist}
    label_items=[]
    for group in groups:
        key=group['group_key'];candidate=index[key]
        x,y=candidate['centroid_xy']
        if abs(x)>radius*.95 or abs(y)>radius*.95:continue
        house=candidate['tags'].get('addr:housenumber')
        label=(f"[{group['rank_group_boundary']}]"+(f' {house}' if house else '')) if key in marked else house
        if label:label_items.append((key==target,key in marked,label,x,y))
    # Draw important labels first; avoid overlaps without shifting geometry.
    fig.canvas.draw();renderer=fig.canvas.get_renderer();label_boxes=[]
    for selected,important,label,x,y in sorted(label_items,reverse=True):
        for dx,dy in ((0,0),(0,9),(0,-9),(12,0),(-12,0),(14,10),(-14,-10)):
            artist=ax.annotate(label,(x,y),xytext=(dx,dy),textcoords='offset points',ha='center',va='center',
                fontsize=9 if important else 7.5,weight='bold' if important else 'normal',
                color='#7b3e09' if selected else '#263f57',zorder=9,clip_on=True,
                bbox={'facecolor':'#fff8eb' if selected else 'white','edgecolor':'none','pad':1.3,'alpha':.92})
            box=artist.get_window_extent(renderer).expanded(1.05,1.15)
            if all(not box.overlaps(other) for other in label_boxes):
                label_boxes.append(box);break
            artist.remove()

    # Independent address nodes remain visible when the outline has no address.
    existing={c['tags'].get('addr:housenumber') for c in data['buildings']
              if abs(c['centroid_xy'][0])<radius and abs(c['centroid_xy'][1])<radius}
    for node in data['tagged_nodes']:
        number=node['tags'].get('addr:housenumber');x,y=node['xy']
        if not number or number in existing or max(abs(x),abs(y))>radius*.94:continue
        ax.scatter([x],[y],s=8,c='#826aa5',marker='s',zorder=7)
        artist=ax.annotate(number,(x,y),xytext=(3,4),textcoords='offset points',fontsize=7,
                           color='#69518d',clip_on=True,zorder=8)
        box=artist.get_window_extent(renderer).expanded(1.1,1.1)
        if any(box.overlaps(other) for other in label_boxes):artist.remove()
        else:label_boxes.append(box)

    sidebar.text(0,1,'Кандидаты на карте',fontsize=11,weight='bold',color='#172f4a',va='top')
    sidebar.text(0,.955,'Порядок по расстоянию до контура.\nОн не доказывает совпадение с фото.',
                 fontsize=8,color='#667085',va='top',linespacing=1.4)
    wrapped=[]
    for group in shortlist:
        name=group['tags'].get('name') or label_address(group['tags']) or group['group_key']
        full=f"[{group['rank_group_boundary']}] {name}"
        lines=textwrap.wrap(full,width=27)
        if len(lines)>3:lines=lines[:3];lines[-1]=lines[-1].rstrip(' .,;')+'…'
        wrapped.append(lines)
    unit=min(.022,.83/sum(len(lines)+2.55 for lines in wrapped)) if wrapped else .022
    name_size=min(8.7,max(7.1,unit*fig.get_figheight()*.73*72*.88))
    y=.89
    for group,lines in zip(shortlist,wrapped):
        tags=group['tags'];selected=group['group_key']==target
        text='\n'.join(lines)
        sidebar.text(0,y,text,fontsize=name_size,
                     color='#9f5411' if selected else '#20374d',weight='bold',va='top',linespacing=1.25)
        y-=unit*(len(lines)+.35)
        sidebar.text(0,y,f"Контур {group['boundary_distance_m']:.1f} м · bbox {group['bbox_center_distance_m']:.1f} м",
                     fontsize=min(7.7,name_size),color='#596777',va='top')
        y-=unit
        sidebar.text(0,y,group['group_key']+(f" · частей: {group['part_count']}" if group['part_count'] else ''),
                     fontsize=min(7.2,name_size),color='#83909c',va='top')
        y-=unit*1.2
    camera=data['camera']
    fig.text(.065,.955,title or 'Геометрия окружения фотографии',fontsize=17,weight='bold',color='#172f4a')
    fig.text(.065,.919,f"GPS камеры: {camera['lat']:.7f}, {camera['lon']:.7f}  ·  локальная метрическая проекция",
             fontsize=9,color='#667085')
    fig.legend(handles=[Line2D([0],[0],marker='s',color='none',markerfacecolor='#c8d4de',markeredgecolor='#72869a',label='Контуры зданий'),
                        Line2D([0],[0],marker='s',color='none',markerfacecolor='#f2b55d',markeredgecolor='#9f5411',label='Проверяемый объект'),
                        Line2D([0],[0],color='#4672a8',linestyle='--',label='Смещение GPS: 10 / 30 / 60 м')],
               loc='lower left',bbox_to_anchor=(.06,.074),frameon=False,ncol=3,fontsize=8.5)
    unresolved=len(data.get('unresolved_buildings',[]))
    fig.text(.065,.058,'Круги — сценарии чувствительности, не измеренная точность GPS. Направление съёмки неизвестно.',
             fontsize=8,color='#667085')
    fig.text(.065,.035,f"Неполных геометрий зданий в снимке OSM: {unresolved}.  © OpenStreetMap contributors · ODbL 1.0",
             fontsize=8,color='#667085')
    if drawn_context:
        fig.text(.79,.115,'Зелёный пунктир: территория учреждения.\nЗдание и территория не объединены.',
                 fontsize=7.5,color='#557f55',linespacing=1.4)
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(output,dpi=160,facecolor='white')
    plt.close(fig)
    return {'output':str(output),'target':target,'radius_m':radius,'label_count':len(label_boxes)}


def render_detail(data,output,target,title=None):
    """Per-component footprint plan. No visual facade assignment is asserted."""
    index={f"{c['osm_type']}/{c['osm_id']}":c for c in data['buildings']}
    candidate=index[target];polygons=candidate['polygons_xy']
    fig=plt.figure(figsize=(12,9),facecolor='white')
    ax=fig.add_axes([.065,.14,.67,.75],facecolor='#fafaf7')
    sidebar=fig.add_axes([.775,.16,.21,.7]);sidebar.axis('off')
    coords=[(0,0)]+[q for p in polygons for q in p['outer']]
    xmin=min(q[0] for q in coords)-20;xmax=max(q[0] for q in coords)+20
    ymin=min(q[1] for q in coords)-20;ymax=max(q[1] for q in coords)+20
    extent=max(xmax-xmin,ymax-ymin)
    cx=(xmin+xmax)/2;cy=(ymin+ymax)/2
    ax.set_xlim(cx-extent/2,cx+extent/2);ax.set_ylim(cy-extent/2,cy+extent/2);ax.set_aspect('equal')
    ax.grid(color='#e3e8e9',lw=.6)
    ax.set_xlabel('Восток от камеры, м');ax.set_ylabel('Север от камеры, м')
    for spine in ax.spines.values():spine.set_color('#c8d0d5')
    for other in data['buildings']:
        if other.get('group_key')==target:continue
        ax.add_patch(PathPatch(polygon_path(other['polygons_xy']),facecolor='#e8ecee',edgecolor='#c8d0d5',lw=.5,zorder=1))
    for road in data['roads']:
        for line in road.get('geometry_segments_xy',[road['xy']]):
            if len(line)>1:ax.plot(*zip(*line),color='#cbd2d8',lw=1,zorder=1)
    colors=('#efb653','#8ac1c9','#c1a2ca','#b3c68c')
    y=.96;nearest=None;component_metrics=[]
    outer_sources=[r for r in candidate['ring_provenance'] if r['role']=='outer']
    for pi,poly in enumerate(polygons):
        letter=chr(ord('A')+pi)
        ax.add_patch(PathPatch(polygon_path([poly]),facecolor=colors[pi%len(colors)],edgecolor='#4d555b',lw=1.2,zorder=3))
        ring=poly['outer'];edges=segments(ring)
        edge=min(((closest_segment((0,0),a,b),i) for i,(a,b) in enumerate(zip(ring,ring[1:]))),key=lambda item:item[0][0])
        d,q=edge[0];edge_index=edge[1]
        if nearest is None or d<nearest[0]:nearest=(d,q,pi,edge_index)
        for i,qv in enumerate(ring[:-1]):
            ax.scatter([qv[0]],[qv[1]],s=9,c='#233f5a',zorder=5)
            ax.annotate(str(i),qv,xytext=(2,3),textcoords='offset points',fontsize=6.5,color='#234b74',zorder=6)
        for e in edges:
            if e['length_m']<5:continue
            a,b=e['a'],e['b'];mid=((a[0]+b[0])/2,(a[1]+b[1])/2)
            ax.text(*mid,f"{letter}{e['segment']}",ha='center',va='center',fontsize=7,color='#713811',zorder=7,
                    bbox={'facecolor':'white','edgecolor':'none','pad':1,'alpha':.8})
        parent_way_ids=outer_sources[pi]['member_way_ids'] if pi<len(outer_sources) else []
        component_metrics.append({'component':pi,'label':letter,'outer_member_way_ids':parent_way_ids,
            'outer_boundary_distance_m':round(d,2),'nearest_outer_edge':edge_index,
            'camera_inside_outer':inside((0,0),ring),
            'edges':[{'edge':e['segment'],'length_m':e['length_m'],'distance_m':e['distance_m'],
                     'axis_deg_mod180':e['axis_deg_mod180'],'nearest_bearing_deg':e['nearest_bearing_deg'],
                     'a_xy':e['a'],'b_xy':e['b']} for e in sorted(edges,key=lambda e:e['segment'])]})
        sidebar.text(0,y,f'Компонент {letter}',fontsize=12,weight='bold',color='#20374d',va='top');y-=.04
        sidebar.text(0,y,'outer way: '+', '.join(map(str,parent_way_ids)),fontsize=8.5,color='#667085',va='top');y-=.038
        sidebar.text(0,y,f'До внешней границы: {d:.2f} м\nБлижайшее ребро: {letter}{edge_index}\nGPS внутри: {"да" if inside((0,0),ring) else "нет"}',
                     fontsize=9.2,color='#20374d',va='top',linespacing=1.5);y-=.15
    d,q,pi,edge_index=nearest
    ax.scatter([0],[0],marker='*',s=190,c='#172f4a',edgecolor='white',lw=1,zorder=10)
    ax.annotate('GPS камеры',(0,0),xytext=(-6,-16),textcoords='offset points',fontsize=9,
                ha='right',weight='bold',color='#172f4a',zorder=10)
    ax.plot([0,q[0]],[0,q[1]],color='#b3333c',lw=2,zorder=8)
    ax.scatter([q[0]],[q[1]],s=40,facecolor='white',edgecolor='#b3333c',lw=1.5,zorder=9)
    sidebar.text(0,y,'Минимум по всему объекту',fontsize=10,weight='bold',color='#a3313c',va='top');y-=.04
    sidebar.text(0,y,f'{d:.2f} м до ребра {chr(ord("A")+pi)}{edge_index}',fontsize=13,weight='bold',color='#a3313c',va='top');y-=.065
    sidebar.text(0,y,'Это не установленная дистанция\nдо изображённого фронтона.\nСвязь конкретного фасада с фото\nтребует визуального подтверждения.',fontsize=9,color='#667085',va='top',linespacing=1.5)
    if d<3:
        inset=ax.inset_axes([.045,.64,.28,.28],facecolor='white')
        inset.set_zorder(25)
        for poly in polygons:
            inset.add_patch(PathPatch(polygon_path([poly]),facecolor='#f0c789',edgecolor='#977146',lw=1))
        inset.set_xlim(-5,5);inset.set_ylim(-5,5);inset.set_aspect('equal')
        inset.scatter([0],[0],s=80,marker='*',c='#172f4a',zorder=5)
        inset.plot([0,q[0]],[0,q[1]],c='#b3333c',lw=2,zorder=4)
        inset.scatter([q[0]],[q[1]],s=25,c='#b3333c',zorder=5)
        inset.tick_params(labelsize=6);inset.set_title('Увеличение: GPS и граница',fontsize=7)
        inset.grid(alpha=.2)
    fig.text(.065,.945,title or 'Составные контуры и рёбра объекта',fontsize=17,weight='bold',color='#172f4a')
    fig.text(.065,.91,target+' · синие числа — вершины; A0 / B7 — индексы рёбер',fontsize=9.5,color='#667085')
    fig.text(.065,.067,'Дистанции рассчитаны от исходной EXIF-точки. Погрешность GPS и направление камеры неизвестны.',fontsize=8.7,color='#667085')
    fig.text(.065,.04,'Полные координаты вершин и связь с OSM node IDs сохранены в geometry.json. © OpenStreetMap contributors · ODbL 1.0',fontsize=8.2,color='#667085')
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True);fig.savefig(output,dpi=160);plt.close(fig)
    result={'output':str(output),'target':target,'components':component_metrics,
            'nearest_whole_object_boundary_m':round(d,2),'visible_facade_assignment':'not_established_by_geometry'}
    output.with_suffix('.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('geometry');parser.add_argument('--target')
    parser.add_argument('--radius',type=float,default=180)
    parser.add_argument('--out');parser.add_argument('--title')
    parser.add_argument('--detail',action='store_true')
    args=parser.parse_args();path=Path(args.geometry)
    output=args.out or path.with_name(path.name.replace('.geometry.json','.detail.png' if args.detail else '.map.png'))
    title=args.title or f"Фото {path.stem.split('.')[0].removeprefix('photo-')} · Геометрия окружения"
    if args.detail:
        if not args.target:parser.error('--detail requires --target type/id')
        result=render_detail(json.loads(path.read_text()),output,args.target,args.title or title)
        print(json.dumps({k:result[k] for k in ('output','target','nearest_whole_object_boundary_m','visible_facade_assignment')},ensure_ascii=False))
    else:
        print(json.dumps(render(json.loads(path.read_text()),output,args.target,args.radius,title),ensure_ascii=False))


if __name__=='__main__':main()
