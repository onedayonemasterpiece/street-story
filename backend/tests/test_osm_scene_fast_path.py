import asyncio
import math

import httpx
import pytest

from street_story.db import Store
from street_story.providers import OSMClient


def primitives(oid, distance):
    scale=6_371_000*math.pi/180
    x=distance/(scale*math.cos(math.radians(55)))
    nodes=[{'type':'node','id':oid+i,'lat':55+y/scale,'lon':21+x+e/(scale*math.cos(math.radians(55)))}
        for i,(e,y) in enumerate(((0,0),(10,0),(10,12),(0,12)))]
    return [*nodes,{'type':'way','id':oid,'nodes':[oid,oid+1,oid+2,oid+3,oid],
        'tags':{'building':'yes','addr:housenumber':'17','wikipedia':'ru:Literal title'}}]


@pytest.mark.asyncio
async def test_full_map_is_not_held_by_reverse_or_new_landmark_request_and_reverse_is_drained(tmp_path):
    started=asyncio.Event()
    drained=asyncio.Event()
    calls=[]
    async def handle(request):
        calls.append(request)
        if request.url.path.endswith('/reverse'):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        if request.url.path.endswith('/map.json'):
            await started.wait()  # Proves map was started while reverse was still pending.
            return httpx.Response(200,json={'elements':primitives(100,190)})
        raise AssertionError('Ready local geometry must not issue a compulsory broader landmark request')
    store=Store(tmp_path/'db.sqlite3')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client=OSMClient(store,'offline fixture',http)
        result=await asyncio.wait_for(client.lookup(55,21),timeout=.75)
        assert drained.is_set()
        assert result['unavailable_buckets']==['reverse'] and result['partial']
        assert result['coverage']=={'source':'osm_api_map','local_patch_radius_m':320,
            'broader_landmarks_queried':False,'broader_landmark_radius_m':None,'completeness':'unknown'}
        assert [row['id'] for row in result['observed_pool']]==[100]
        assert result['observed_pool'][0]['tags']['wikipedia']=='ru:Literal title'
        assert result['observed_pool'][0]['boundary_distance_m']>180
        assert await client.lookup(55,21)==result  # Short reuse includes geometry despite reverse deferral.
        assert len(calls)==2
    assert not any(task.get_name()=='street-story-osm-reverse' and not task.done() for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_failed_map_preserves_nearby_and_broader_landmark_fallback(tmp_path):
    posts=[]
    async def handle(request):
        if request.url.path.endswith('/reverse'):
            return httpx.Response(200,json={'lat':'55','lon':'21','address':{'city':'Observed city'}})
        if request.url.path.endswith('/map.json'):
            return httpx.Response(503)
        if request.method=='POST':
            posts.append(request)
            oid,distance=(100,20) if len(posts)==1 else (200,350)
            rows=primitives(oid,distance)
            nodes={row['id']:row for row in rows if row['type']=='node'}
            way=rows[-1]
            return httpx.Response(200,json={'elements':[{**way,'geometry':[
                {'lat':nodes[n]['lat'],'lon':nodes[n]['lon']} for n in way['nodes']]}]})
        raise AssertionError(str(request.url))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result=await OSMClient(Store(tmp_path/'db.sqlite3'),'offline fixture',http).lookup(55,21)
    assert len(posts)==2
    assert {row['id'] for row in result['observed_pool']}=={100,200}
    assert result['reverse']['address']['city']=='Observed city'
    assert result['coverage']['source']=='overpass_fallback'
    assert result['coverage']['broader_landmarks_queried']
    assert result['coverage']['broader_landmark_radius_m']==600


@pytest.mark.asyncio
async def test_lookup_cancellation_drains_parallel_reverse_before_return(tmp_path):
    reverse_started=asyncio.Event()
    map_started=asyncio.Event()
    reverse_drained=asyncio.Event()
    async def handle(request):
        if request.url.path.endswith('/reverse'):
            reverse_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                reverse_drained.set()
        map_started.set()
        await asyncio.Event().wait()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        task=asyncio.create_task(OSMClient(Store(tmp_path/'db.sqlite3'),'fixture',http).lookup(55,21))
        await asyncio.wait_for(reverse_started.wait(),.5)
        await asyncio.wait_for(map_started.wait(),.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert reverse_drained.is_set()
