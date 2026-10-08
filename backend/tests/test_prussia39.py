"""Offline publisher contracts; fixture names are unrelated to acceptance cases."""
import asyncio
import hashlib
import json

import httpx
import pytest

from street_story.prussia39 import Prussia39Adapter


class Cache:
    def __init__(self, path):
        self.path, self.values, self.clock = path, {}, 1000

    def now(self):
        return self.clock

    def cache_get(self, key):
        saved = self.values.get(key)
        return saved[0] if saved and saved[1] > self.clock else None

    def cache_put(self, key, value, ttl):
        self.values[key] = (value, self.clock + ttl)


async def resolver(host):
    assert host == 'www.prussia39.ru'
    return '93.184.216.34'


def html(body):
    return ('<html><head><meta charset="windows-1251"><title>История дома</title></head>'
            '<body>' + body + '</body></html>').encode('cp1251')


def response(body, status=200, headers=None):
    return httpx.Response(status, content=body, headers={'content-type': 'text/html; charset=windows-1251', **(headers or {})})


def results(total=2, pagination=False):
    rows = ''.join(f'<tr><td><a href="index.php?sid={n}">Дом {n}</a></td>'
                   f'<td>Адрес: ул. Тестовая, {n}а</td></tr>' for n in range(1, 3)) if total else ''
    page = '<a href="database.php?text_adr=%D3%EB%E8%F6%E0&amp;p=2">2</a>' if pagination else ''
    return html(f'<div><b>Результаты поиска</b><p>всего совпадений: {total}</p><table>{rows}</table>{page}</div>')


@pytest.mark.asyncio
async def test_full_form_cp1251_literals_no_images_and_truthful_cards(tmp_path):
    calls = []
    async def handler(request):
        calls.append(request)
        return response(results())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver)
        value = await adapter.address_search('Город', '  ул. Тестовая, 1а–3  ')
        again = await adapter.address_search('Город', 'ул. Тестовая, 1а-3')
    assert value['status'] == 'completed' and value['inventory_complete']
    assert value['original_query']['address'] == '  ул. Тестовая, 1а–3  '
    assert [c['address_text'] for c in value['results']] == ['ул. Тестовая, 1а', 'ул. Тестовая, 2а']
    assert value['results'][0]['canonical_url'] == 'https://www.prussia39.ru/sight/index.php?sid=1'
    assert len(calls) == 1 and again['cache_hit']
    from urllib.parse import parse_qs
    query = parse_qs(calls[0].url.query.decode(), encoding='cp1251')
    assert query == {'text_n': ['Название достопримечательности'], 'text_np': ['Город'],
                     'text_adr': ['Тестовая, 1а-3'], 'text_link': ['Официальный сайт'],
                     'text_cph': ['Код'], 'text_nph': ['Номер телефона'],
                     'w_id_wout': ['0'], 'w_id_photo': ['0'], 'w_st_okn': ['0']}
    assert calls[0].headers['Host'] == 'www.prussia39.ru'
    assert calls[0].headers['User-Agent'] == 'StreetStoryResearch/1.0'
    assert calls[0].extensions['sni_hostname'] == 'www.prussia39.ru'


@pytest.mark.asyncio
@pytest.mark.parametrize('body,status,error', [
    (results(0), 'completed_empty', None),
    (b'', 'transport_failed', 'empty_body'),
    (html('<form>Адрес</form>'), 'parse_failed', 'address_results_section_missing'),
    (html('<div>Результаты поиска</div>'), 'parse_failed', 'address_total_missing'),
    (html('<div>Результаты поиска всего совпадений: 1</div>'), 'parse_failed', 'address_result_count_mismatch'),
])
async def test_empty_parse_and_transport_are_distinct(tmp_path, body, status, error):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(body))) as http:
        value = await Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver).address_search('Город', 'Улица')
    assert value['status'] == status and value.get('error_code') == error
    if body:
        assert value['source_sha256'] == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_partial_inventory_requires_explicit_observed_pagination(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return response(results(35, pagination=True))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver)
        first = await adapter.address_search('Город', 'Улица')
        assert first['total_count'] == 35 and not first['inventory_complete']
        assert len(first['results']) == 2 and len(calls) == 1
        denied = await adapter.search_page('https://www.prussia39.ru/sight/database.php?text_adr=x&p=8', previous_receipt=first)
        assert denied['status'] == 'not_sent' and len(calls) == 1
        next_page = await adapter.search_page(first['pagination_urls'][0], previous_receipt=first)
    assert next_page['status'] == 'completed' and len(calls) == 2


@pytest.mark.asyncio
async def test_qualified_publisher_card_dom_preserves_literal_complex_address(tmp_path):
    body = html('<td><p>Результаты поиска (всего совпадений: 1)</p><table><tr>'
                '<td style="margin:3px;text-align:justify;"><b>Дом с башней</b>'
                '<p style="font-size:8pt;">Область, г. Город, ул. Тестовая, 11, 11А, 13-15</p>'
                'Два корпуса.<a href="../sight/index.php?sid=31">Подробнее о достопримечательности</a></td>'
                '<td><a href="../sight/index.php?sid=31"><img src="/thumb.jpg"></a></td></tr></table></td>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(body))) as http:
        value = await Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver).address_search('Город', 'Тестовая')
    assert value['status'] == 'completed' and len(value['results']) == 1
    assert value['results'][0]['title'] == 'Дом с башней'
    assert value['results'][0]['address_text'] == 'Область, г. Город, ул. Тестовая, 11, 11А, 13-15'


@pytest.mark.asyncio
async def test_coordinate_post_json_arrays_canonical_dedup_and_shared_cache(tmp_path):
    calls = []
    label = '<a href="/sight/index.php?sid=0007">Комплекс</a>'
    body = html('<script>var name_fgr=' + json.dumps([label, label]) + ';var coords=[[54.7,20.5],[54.7,20.5]];</script>')
    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0)
        return response(body)
    cache = Cache(tmp_path / 'cache')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        values = await asyncio.gather(*(Prussia39Adapter(cache, http, resolver=resolver).coordinate_search(54.7, 20.5) for _ in range(2)))
    assert len(calls) == 1 and calls[0].method == 'POST'
    assert calls[0].content == b'new_coords=54.7%2C20.5'
    assert values[0]['status'] == 'completed' and len(values[0]['results']) == 1
    assert values[0]['window_bounds'] is None
    assert values[0]['results'][0]['coordinates']['provenance'] == 'publisher_coordinate_array'
    assert values[1]['cache_hit']


@pytest.mark.asyncio
@pytest.mark.parametrize('script,status', [
    ('var name_fgr=[];var coords=[];', 'completed_empty'),
    ('var name_fgr=[];', 'parse_failed'),
    ('var name_fgr=["name"];var coords=[];', 'parse_failed'),
    ('var name_fgr=["name"];var coords=[[54,20]];', 'parse_failed'),
    ('var name_fgr=[];var coords=evil();', 'parse_failed'),
])
async def test_coordinate_empty_must_have_both_valid_arrays(tmp_path, script, status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(html('<script>' + script + '</script>')))) as http:
        value = await Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver).coordinate_search(54, 20)
    assert value['status'] == status


@pytest.mark.asyncio
async def test_article_canonical_reuse_exact_hashes_and_no_gallery_reads(tmp_path):
    body = html('<table><tr><td style="text-align: justify;">Дом построен из кирпича.<br>Фасад имеет три арки.'
                '<img src="/photo.jpg"><script>junk()</script>Интерактивный путеводитель скрыт.</td></tr>'
                '<tr><td style="text-align:justify">Все фотографии<br>Подпись</td></tr></table>')
    calls = []
    def handler(request):
        calls.append(request)
        return response(body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver)
        value = await adapter.article('http://prussia39.ru/sight/index.php?sid=0007&utm=x')
        again = await adapter.article(7)
    assert value['status'] == 'completed' and len(calls) == 1 and again['cache_hit']
    assert value['article_id'] == 'prussia39:sid:7'
    assert value['text'] == 'Дом построен из кирпича.\nФасад имеет три арки.'
    assert value['source_sha256'] == hashlib.sha256(body).hexdigest()
    assert value['text_sha256'] == hashlib.sha256(value['text'].encode()).hexdigest()
    assert value['input_kind'] == 'acquired_article_text' and value['raw_body_sha256_verified']
    assert value['coordinates'] is None and value['address_text'] == ''


@pytest.mark.asyncio
async def test_changed_article_layout_fails_instead_of_reading_navigation(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(html('<nav>Старая навигация</nav>')))) as http:
        value = await Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver).article(7)
    assert value['status'] == 'parse_failed' and 'text_sha256' not in value


@pytest.mark.asyncio
async def test_article_redirect_cannot_relabel_another_sid_as_requested_subject(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        if b'sid=7' in request.url.query:
            return httpx.Response(302, headers={'location': 'https://www.prussia39.ru/sight/index.php?sid=8'})
        return response(html('<td style="text-align:justify">Другой объект.</td>'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        value = await Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver).article(7)
    assert value['status'] == 'transport_failed' and len(calls) == 2
    assert value['error_code'] == 'publisher_article_redirect_mismatch'
    assert 'raw_body_sha256_verified' not in value


@pytest.mark.asyncio
async def test_429_cooldown_shared_across_routes_and_no_automatic_retry(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return response(b'', 429, {'Retry-After': '90'})
    cache = Cache(tmp_path / 'cache')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = Prussia39Adapter(cache, http, resolver=resolver)
        first = await adapter.address_search('Город', 'Улица')
        second = await adapter.coordinate_search(54, 20)
        third = await adapter.article(7)
    assert first['status'] == 'transport_failed' and first['retry_at'] == 1090
    assert second['status'] == third['status'] == 'not_sent' and len(calls) == 1


@pytest.mark.asyncio
async def test_private_dns_and_unobserved_external_urls_never_dispatch(tmp_path):
    calls = []
    async def private(_):
        return '127.0.0.1'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r))) as http:
        adapter = Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=private)
        values = [await adapter.coordinate_search(54, 20), await adapter.article('https://private.example/sight/index.php?sid=7'),
                  await adapter.coordinate_search(float('nan'), 20)]
    assert not calls and all(v['status'] != 'completed' for v in values)
