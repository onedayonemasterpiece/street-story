"""Decode actual cached PR245 OSM XML without any web or truth input."""
import gzip, xml.etree.ElementTree as ET
from pathlib import Path


def pool_from_cached_xml(file):
    xml=gzip.decompress(Path(file).read_bytes())
    tree=ET.fromstring(xml)
    nodes={int(x.get('id')):(float(x.get('lat')), float(x.get('lon')),
        {t.get('k'):t.get('v') for t in x.findall('tag')}) for x in tree.findall('node')}
    ways={}
    output=[]
    for x in tree.findall('way'):
        id_=int(x.get('id'))
        refs=[int(n.get('ref')) for n in x.findall('nd')]
        pts=[nodes[key] for key in refs if key in nodes]
        tags={t.get('k'):t.get('v') for t in x.findall('tag')}
        way={'type':'way','id':id_,'tags':tags,
            'geometry':[{'lat':pt[0],'lon':pt[1]} for pt in pts],
            'building_entrance_node_ids':[key for key in refs if key in nodes
                and (nodes[key][2].get('entrance') or nodes[key][2].get('addr:housenumber'))]}
        if pts:way['center']={'lat':sum(pt[0] for pt in pts)/len(pts),
            'lon':sum(pt[1] for pt in pts)/len(pts)}
        ways[id_]=way
        output.append(way)
    for x in tree.findall('relation'):
        tags={t.get('k'):t.get('v') for t in x.findall('tag')}
        if tags.get('type')!='multipolygon' and not tags.get('building'):
            continue
        members=[]
        for m in x.findall('member'):
            if m.get('type')!='way':continue
            member=ways.get(int(m.get('ref')))
            if not member:continue
            members.append({'type':'way','ref':member['id'],'role':m.get('role') or '',
                'geometry':member.get('geometry') or []})
        output.append({'type':'relation','id':int(x.get('id')),
            'tags':tags,'members':members})
    for id_,(lat,lon,tags) in nodes.items():
        if tags:output.append({'type':'node','id':id_,
            'lat':lat,'lon':lon,'tags':tags})
    return output