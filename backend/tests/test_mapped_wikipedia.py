from types import SimpleNamespace

import httpx
import pytest

from street_story.errors import RetryableProviderError
from street_story.mapped_wikipedia import linked_pages


def mapped(distance=20, title='ru:Named Gate'):
    return {'type': 'way', 'id': 7, 'distance_m': distance,
        'tags': {'historic': 'city_gate', 'wikipedia': title, 'wikidata': 'Q7'}}


@pytest.mark.asyncio
async def test_exact_mapped_title_single_api_no_geosearch_own_coordinates_and_cache():
    calls = []
    cache = {}
    async def handle(request):
        calls.append(request)
        assert request.url.params['titles'] == 'Named Gate'
        assert 'geosearch' not in str(request.url)
        assert request.url.params['prop'] == 'extracts|info|pageimages|pageprops|coordinates'
        assert request.headers['User-Agent']
        return httpx.Response(200, json={'query': {'pages': [{'pageid': 7, 'title': 'Named Gate',
            'extract': 'Facts', 'fullurl': 'https://ru.wikipedia.org/wiki/Named_Gate',
            'original': {'source': 'https://upload.wikimedia.org/gate.jpg'},
            'coordinates': [{'lat': 10, 'lon': 20, 'primary': True, 'globe': 'earth'}]}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = SimpleNamespace(http=http, endpoint='https://ru.wikipedia.org/w/api.php',
            store=SimpleNamespace(cache_get=cache.get, cache_put=lambda k,v,t:cache.update({k:v})))
        result = await linked_pages(client, {'nearby': [mapped()]})
        again = await linked_pages(client, {'nearby': [mapped(30)]})
    assert len(calls) == 1 and result[0]['pageid'] == 7
    assert result[0]['lat'] == 10 and result[0]['distance_m'] == 20
    assert again[0]['distance_m'] == 30
    assert result[0]['mapped_wikipedia_sources'][0]['candidate_id'] == 'osm:way:7'
    assert result[0]['discovery'] == 'osm_explicit_wikipedia_link'


@pytest.mark.asyncio
@pytest.mark.parametrize('item', [mapped(float('nan')), {'tags': {}},
    {**mapped(), 'tags': {'place': 'city', 'wikipedia': 'ru:City'}},
    {**mapped(), 'tags': {'highway': 'residential', 'wikipedia': 'ru:Street'}}])
async def test_absent_eligible_close_link_returns_none_without_network(item):
    assert await linked_pages(SimpleNamespace(), {'nearby': [item]}) is None


@pytest.mark.asyncio
async def test_explicit_missing_page_returns_empty_and_network_failure_is_not_fallback():
    cache = {}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,
            json={'query': {'pages': [{'title': 'Named Gate', 'missing': True}]}}))) as http:
        c = SimpleNamespace(http=http, endpoint='https://ru.wikipedia.org/w/api.php',
            store=SimpleNamespace(cache_get=cache.get,cache_put=lambda k,v,t:cache.update({k:v})))
        assert await linked_pages(c, {'nearby': [mapped()]}) == []
    cache.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(403))) as http:
        c.http = http
        with pytest.raises(RetryableProviderError, match='mapped_wikipedia_lookup_failed'):
            await linked_pages(c, {'nearby': [mapped()]})


@pytest.mark.asyncio
async def test_redirect_maps_back_to_actual_osm_link_without_borrowing_osm_coordinates():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,
            json={'query': {'redirects': [{'from':'Named Gate','to':'Redirected Gate'}],
                'pages': [{'pageid': 8,'title':'Redirected Gate','original':{'source':'http://unsafe.example/img'}}]}}))) as http:
        c=SimpleNamespace(http=http,endpoint='https://ru.wikipedia.org/w/api.php',
            store=SimpleNamespace(cache_get=lambda k:None,cache_put=lambda *args:None))
        item={**mapped(),'lat':54.7,'lon':20.5}
        result=await linked_pages(c,{'nearby':[item]})
    assert result[0]['pageid']==8 and result[0]['image_url'] is None
    assert 'lat' not in result[0] and 'lon' not in result[0]
    assert result[0]['mapped_wikipedia_sources'][0]['link']=='ru:Named Gate'
