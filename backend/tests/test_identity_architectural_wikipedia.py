"""Wikipedia fallback supplies genuine architectural text, never an identity."""
import hashlib
import json

import httpx
import pytest

from street_story.identity_architectural_wikipedia import (ArchitecturalWikipediaReader,
    wikipedia_title_choice_schema,prepare_wikipedia_title_queries,
    validate_model_wikipedia_title_queries)


class Cache:
    def __init__(self,tmp_path):
        self.path=tmp_path/'cache'
        self.values={}
    def now(self):
        return 1000
    def cache_get(self,key):
        return self.values.get(key)
    def cache_put(self,key,value,ttl):
        assert ttl > 0
        self.values[key]=value


async def resolver(host):
    assert host in {'ru.wikipedia.org','de.wikipedia.org'}
    return '93.184.216.34'


def actual_article(pageid=80717,title='Башня на набережной',body=None):
    if body is None:
        body=('Здание имеет ступенчатый фронтон и три различающихся '
              'оконных оси, центральный эркер и арочный портал.')
    return json.dumps({'batchcomplete':True,'query':{'pages':[{
        'pageid':pageid,'ns':0,'title':title,'extract':body}]}},
        ensure_ascii=False).encode()


@pytest.mark.asyncio
async def test_actual_original_title_wikipedia_body_sha_and_cache(tmp_path):
    requests=[]
    raw=actual_article()
    def handler(request):
        requests.append(request)
        assert request.headers['Host']=='ru.wikipedia.org'
        assert request.extensions['sni_hostname']=='ru.wikipedia.org'
        assert request.url.path=='/w/api.php'
        assert request.url.params['titles']=='Башня на набережной'
        assert request.url.params['prop']=='extracts'
        assert request.url.params['explaintext']=='1'
        return httpx.Response(200,content=raw,headers={'content-type':'application/json'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        reader=ArchitecturalWikipediaReader(Cache(tmp_path),http,resolver=resolver)
        first=await reader.article_by_observed_title(' Башня на набережной ')
        second=await reader.article_by_observed_title('Башня на набережной')
    assert len(requests)==1
    assert first['status']=='completed' and second['cache_hit'] is True
    assert first['article_id']=='wiki:80717'
    assert first['url'].endswith('/wiki/%D0%91%D0%B0%D1%88%D0%BD%D1%8F_%D0%BD%D0%B0_%D0%BD%D0%B0%D0%B1%D0%B5%D1%80%D0%B5%D0%B6%D0%BD%D0%BE%D0%B9')
    assert first['source_sha256']==hashlib.sha256(raw).hexdigest()
    assert first['text_sha256']==hashlib.sha256(first['text'].encode()).hexdigest()
    assert first['raw_body_sha256_verified'] is True
    assert first['input_kind']=='acquired_article_text'
    assert first['physical_identity_inferred'] is False
    assert first['address']==''


@pytest.mark.asyncio
async def test_unfound_wikipedia_title_is_not_invented_article(tmp_path):
    raw=json.dumps({'query':{'pages':[{
        'ns':0,'title':'Ненайденный объект','missing':True}]}}).encode()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _:httpx.Response(200,content=raw,headers={
            'content-type':'application/json'}))) as http:
        result=await ArchitecturalWikipediaReader(
            Cache(tmp_path),http,resolver=resolver).article_by_observed_title('Ненайденный объект')
    assert result['status']=='completed_empty'
    assert result['article_id'] is None
    assert 'identity' not in result


@pytest.mark.asyncio
async def test_wikipedia_source_title_does_not_trigger_repeated_search_variants(tmp_path):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,content=actual_article(),headers={
            'content-type':'application/json'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        reader=ArchitecturalWikipediaReader(Cache(tmp_path),http,resolver=resolver)
        for language,title in [('zz','Архитектурное описание'),
                ('ru',''),('ru',' \u0002 '),('ru','A'*181)]:
            with pytest.raises(ValueError):
                await reader.article_by_observed_title(title,language=language)
        good=await reader.article_by_observed_title('Altbau Denkmal',language='de')
    assert good['status']=='completed'
    assert len(calls)==1
    assert calls[0].headers['Host']=='de.wikipedia.org'


@pytest.mark.asyncio
async def test_wikipedia_redirect_outside_owned_domain_not_trusted(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _:httpx.Response(302,headers={'location':'https://example.org/redirect'}))) as http:
        result=await ArchitecturalWikipediaReader(
            Cache(tmp_path),http,resolver=resolver).article_by_observed_title('Башня')
    assert result['status']=='parse_failed'
    assert result.get('raw_body_sha256_verified') is not True


@pytest.mark.asyncio
async def test_wikipedia_article_conveys_current_and_historic_facade_without_host_choosing(tmp_path):
    body=('Здание имеет асимметричный объём и две оконные оси. '
        'Справа под фронтоном сохранилась скульптурная голова льва. '
        'Башня была утрачена после войны.')
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _:httpx.Response(200,content=actual_article(body=body),
            headers={'content-type':'application/json'}))) as http:
        result=await ArchitecturalWikipediaReader(
            Cache(tmp_path),http,resolver=resolver).article_by_observed_title('Башня на набережной')
    assert result['text']==body
    assert result['scope'].startswith('Encyclopedia article')
    assert result['physical_identity_inferred'] is False
    assert 'candidate_id' not in result



@pytest.mark.asyncio
async def test_mediawiki_search_offers_actual_alternative_titles_without_suffix_rules(tmp_path):
    import jsonschema
    requests=[]
    search_raw=json.dumps({'query':{'search':[{
        'pageid':411,'title':'Архитектурная вилла','snippet':'some matching words'},
        {'pageid':412,'title':'Архитектурная вилла (другой район)',
         'snippet':'another possible building'}]}},ensure_ascii=False).encode()
    title_raw=actual_article(pageid=411,title='Архитектурная вилла',
        body='Две оконные оси под фронтоном, вокруг них общий каменный наличник.')
    def handler(request):
        requests.append(request)
        assert request.headers['Host']=='ru.wikipedia.org'
        if request.url.params.get('list')=='search':
            assert request.url.params['srsearch']=='Архитектурная вилла (историческая) — город'
            return httpx.Response(200,content=search_raw,
                headers={'content-type':'application/json'})
        assert request.url.params['titles']=='Архитектурная вилла'
        return httpx.Response(200,content=title_raw,
            headers={'content-type':'application/json'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        reader=ArchitecturalWikipediaReader(Cache(tmp_path),http,resolver=resolver)
        candidates=await reader.search_observed_title(
            'Архитектурная вилла (историческая) — город')
        schema=wikipedia_title_choice_schema(candidates)
        assert jsonschema.Draft202012Validator(schema).is_valid({
            'pageid':411,'title_fit':'same_subject','reason':'SOURCE and article facade match'})
        assert jsonschema.Draft202012Validator(schema).is_valid({
            'pageid':None,'title_fit':'ambiguous','reason':'Several facades possible'})
        assert not jsonschema.Draft202012Validator(schema).is_valid({
            'pageid':999,'title_fit':'same_subject','reason':'Invented ID'})
        with pytest.raises(ValueError,match='not_received'):
            await reader.article_by_model_selected_pageid(candidates,999)
        import copy
        forged=copy.deepcopy(candidates)
        forged['results'][0]['title']='Invented title for unrelated subject'
        with pytest.raises(ValueError,match='not_received'):
            await reader.article_by_model_selected_pageid(forged,411)
        changed=copy.deepcopy(candidates)
        changed['raw_source_sha256']='0'*64
        with pytest.raises(ValueError,match='sha_mismatch'):
            await reader.article_by_model_selected_pageid(changed,411)
        selected=await reader.article_by_model_selected_pageid(candidates,411)
        again=await reader.search_observed_title('Архитектурная вилла (историческая) — город')
    assert candidates['status']=='completed'
    assert candidates['identity_inferred'] is False
    assert len(candidates['results'])==2
    assert [x['pageid'] for x in candidates['results']]==[411,412]
    assert selected['article_id']=='wiki:411'
    assert selected['raw_body_sha256_verified'] is True
    assert 'Две оконные оси' in selected['text']
    assert again['cache_hit'] is True
    assert len(requests)==2  # one search + one selected full article, no variant loops



def test_wikipedia_model_query_planner_uses_acquired_title_verbatim_not_suffix_heuristic():
    article={'article_id':'prussia39:sid:77','input_kind':'acquired_article_text',
        'raw_body_sha256_verified':True,
        'title':'Новый корпус городской виллы (старый) — город Тестовый',
        'text':'Асимметричный фронтон над двумя оконными осями и каменный карниз.',
        'source_sha256':'a'*64}
    article['text_sha256']=hashlib.sha256(article['text'].encode()).hexdigest()
    plan=prepare_wikipedia_title_queries(article)
    assert article['title'] in plan['prompt']
    assert article['text'] in plan['prompt']
    assert plan['input_contract']=='wiki-T-title-search-llm-v1'
    assert plan['schema']['properties']['queries']['maxItems']==2
    actual={'queries':['Новый корпус городской виллы','Городская вилла'],
        'query_reason':'Two possible publisher-title interpretations, each only a search lead.'}
    assert validate_model_wikipedia_title_queries(plan,actual)==actual['queries']
    import copy
    bad=copy.deepcopy(actual)
    bad['queries']=['А','Б','В']
    with pytest.raises(ValueError,match='invalid_model_wikipedia_title_queries'):
        validate_model_wikipedia_title_queries(plan,bad)
    bad['queries']=['Одинаковое','Одинаковое']
    with pytest.raises(ValueError,match='invalid_model_wikipedia_title_queries'):
        validate_model_wikipedia_title_queries(plan,bad)
    mutated=copy.deepcopy(article)
    mutated['text']+='Invented after the publisher fetch.'
    with pytest.raises(ValueError,match='verified_publisher_article'):
        prepare_wikipedia_title_queries(mutated)
