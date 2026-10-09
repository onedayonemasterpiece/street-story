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


@pytest.mark.asyncio
async def test_article_preserves_literal_modern_address_from_real_metadata_dom(tmp_path):
    # The actual publisher article has this table *outside* justified history.
    # It is essential for distinguishing compound/single and neighbor addresses.
    markup = html('<table><tr><td width=55 style="font-size:8pt;">Адрес:</td>'
        '<td style="font-size:8pt;">Калининградская область, г. Калининград, '
        'ул. Житомирская, 22, 24</td></tr></table>'
        '<table><tr><td style="text-align:justify">Центральный эркер имеет '
        'несколько ярусов и полукруглое завершение.</td></tr></table>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: response(markup))) as http:
        page = await Prussia39Adapter(Cache(tmp_path / 'cache'),http,resolver=resolver).article(51)
    assert page['status'] == 'completed'
    assert page['address_text'] == 'Калининградская область, г. Калининград, ул. Житомирская, 22, 24'
    assert page['address_provenance'] == 'publisher_article_metadata_table'
    assert page['raw_body_sha256_verified'] is True
    assert 'Житомирская' not in page['text']


@pytest.mark.asyncio
async def test_publisher_address_21_is_not_silently_inferred_as_22(tmp_path):
    markup = html('<table><tr><td>Адрес:</td><td>Советск, ул. Капитана Гастелло, 21</td></tr>'
                  '</table><td style="text-align:justify">Дом жилой с эркером.</td>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: response(markup))) as http:
        page = await Prussia39Adapter(Cache(tmp_path / 'cache'),http,resolver=resolver).article(52)
    assert page['address_text'].endswith('Гастелло, 21')
    assert page['address_provenance'] == 'publisher_article_metadata_table'


@pytest.mark.asyncio
async def test_no_gps_title_search_preserves_real_publisher_cards_and_modern_addresses(tmp_path):
    body = html('<title>Поиск по сайту Prussia39.ru</title>'
        '<table><tr><td style="margin:3px;text-align:justify;">'
        '<b>Казарменный корпус с порталами</b><br/>'
        '<p style="margin:0;font-size:8pt;">Область, г. Город, ул. Тестовая, 18</p>'
        'Арочные порталы и верхние стрельчатые окна.'
        '<a href="../sight/index.php?sid=91">Подробнее о достопримечательности</a>'
        '</td><td><a href="../sight/index.php?sid=91"><img src="/thumb.jpg"/></a>'
        '</td></tr></table>')
    seen = []
    def transport(req):
        seen.append(req)
        return response(body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        adapter = Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver)
        result = await adapter.title_search('  казармы  ')
        again = await adapter.title_search('казармы')
    assert len(seen) == 1 and again['cache_hit'] is True
    assert result['status'] == 'completed'
    assert result['inventory_scope'] == 'observed_publisher_title_search_page'
    assert result['results'] == [{
        'article_id':'prussia39:sid:91','sid':91,
        'canonical_url':'https://www.prussia39.ru/sight/index.php?sid=91',
        'title':'Казарменный корпус с порталами',
        'address_text':'Область, г. Город, ул. Тестовая, 18',
        'annotation':'Арочные порталы и верхние стрельчатые окна.'}]
    from urllib.parse import parse_qs
    assert parse_qs(seen[0].url.query.decode(),encoding='cp1251') == {
        'text':['казармы'], 'search_obj':['2']}


@pytest.mark.asyncio
async def test_no_gps_title_search_not_an_identity_and_empty_not_absent(tmp_path):
    empty=html('<h1>Поиск по сайту Prussia39.ru</h1><p>Нет найденных карточек.</p>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: response(empty))) as http:
        adapter=Prussia39Adapter(Cache(tmp_path / 'cache'),http,resolver=resolver)
        results=await adapter.title_search('неизвестное строение')
    assert results['status']=='completed_empty'
    assert results['inventory_complete'] is True
    assert results['results']==[]
    assert results['inventory_scope']=='observed_publisher_title_search_page'


@pytest.mark.asyncio
async def test_source_derived_title_search_reads_one_observed_continuation_only(tmp_path):
    first_page = html(
        '<h1>Поиск по сайту</h1>'
        '<table><tr><td style="text-align:justify"><b>Дом с эркером</b>'
        '<p style="font-size:8pt;">Город, ул. Нейтральная, 8</p>'
        '<a href="/sight/index.php?sid=61">Подробнее о достопримечательности</a>'
        '</td></tr></table>'
        '<a href="/search.php?text=%E2%E8%EB%EB%E0&amp;search_obj=2&amp;p=2">2</a>')
    next_page = html(
        '<h1>Поиск по сайту</h1>'
        '<table><tr><td style="text-align:justify"><b>Дом с фронтоном</b>'
        '<p style="font-size:8pt;">Город, ул. Другая, 6</p>'
        '<a href="/sight/index.php?sid=62">Подробнее о достопримечательности</a>'
        '</td></tr></table>')
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return response(next_page if 'p=2' in str(request.url) else first_page)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver)
        first = await adapter.title_search('вилла')
        assert first['inventory_complete'] is False
        assert len(first['results']) == 1
        assert len(first['pagination_urls']) == 1
        injected = first['pagination_urls'][0].replace('p=2', 'p=42')
        rejected = await adapter.title_search_page(injected, previous_receipt=first)
        assert rejected['status'] == 'not_sent'
        hijack = first['pagination_urls'][0].replace('%E2%E8%EB%EB%E0', '%E4%EE%EC')
        previous = dict(first, pagination_urls=[hijack])
        rejected = await adapter.title_search_page(hijack, previous_receipt=previous)
        assert rejected['status'] == 'not_sent'
        page = await adapter.title_search_page(first['pagination_urls'][0], previous_receipt=first)
        again = await adapter.title_search_page(first['pagination_urls'][0], previous_receipt=first)
    assert len(calls) == 2
    assert page['status'] == 'completed' and again['cache_hit'] is True
    assert page['results'][0]['article_id'] == 'prussia39:sid:62'
    assert page['results'][0]['title'] == 'Дом с фронтоном'
    assert page['results'][0]['address_text'] == 'Город, ул. Другая, 6'
    assert page['inventory_complete'] is True


@pytest.mark.asyncio
async def test_untrusted_title_page_cannot_leave_publisher_or_change_method(tmp_path):
    observed = 'https://www.prussia39.ru/search.php?text=%E2%E8%EB%EB%E0&search_obj=2&p=2'
    prior = {'operation':'title','original_query':{'text':'вилла'},'pagination_urls':[observed]}
    requested = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: requested.append(r))) as http:
        adapter = Prussia39Adapter(Cache(tmp_path / 'cache'), http, resolver=resolver)
        for altered in (
                observed.replace('www.prussia39.ru','example.com'),
                observed.replace('https:', 'http:'),
                observed.replace('search_obj=2','search_obj=3'),
                observed.replace('p=2','p=2000')):
            bad = await adapter.title_search_page(altered,
                previous_receipt={**prior,'pagination_urls':[altered]})
            assert bad['status'] == 'not_sent'
    assert not requested



def test_received_publisher_html_image_links_are_observed_only_and_domain_bound():
    from bs4 import BeautifulSoup
    from street_story.prussia39 import parse_article
    html_body=(
        '<html><head><title>Real source text</title></head><body>'
        '<td style="text-align:justify">Общий вид жилого дома.'
        '<a href="/photos/subject-1.jpg">Полная фотография</a>'
        '<img src="/photos/subject-2.png"/>'
        '<a href="https://example.org/other-building.jpg">Other</a>'
        '<img src="javascript:alert(1)"/>'
        '</td></body></html>')
    parsed=parse_article(BeautifulSoup(html_body,'html.parser'))
    assert parsed['source_image_links']==[
        'https://www.prussia39.ru/photos/subject-1.jpg',
        'https://www.prussia39.ru/photos/subject-2.png']
    assert parsed['text']
    assert all(u.startswith('https://www.prussia39.ru/') for u in parsed['source_image_links'])



@pytest.mark.asyncio
async def test_already_read_architectural_article_retains_real_image_urls_without_fetch(tmp_path):
    requests=[]
    source=html(
        '<table><tr><td style="text-align:justify">'
        'На фасаде сохранились три фигурных эркера и портал.'
        '<img src="images/facade.jpg"/>'
        '<a href="/sight/photos/portal.png">Портал</a>'
        '<a href="https://evil.invalid/exterior.jpg">Неизвестный внешний источник</a>'
        '<img src="//evil.invalid/tracker.png"/>'
        '</td></tr></table>')
    async def handler(request):
        requests.append(request)
        return response(source)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter=Prussia39Adapter(Cache(tmp_path/'cache'),http,resolver=resolver)
        url='https://www.prussia39.ru/sight/index.php?sid=9837'
        receipt=await adapter.article(url)
        again=await adapter.article(url)
    assert len(requests)==1
    assert receipt['status']=='completed'
    assert again['cache_hit'] is True
    assert receipt['raw_body_sha256_verified'] is True
    assert receipt['source_image_links']==[
        'https://www.prussia39.ru/sight/images/facade.jpg',
        'https://www.prussia39.ru/sight/photos/portal.png']
    assert 'эркера' in receipt['text']
    assert receipt['source_sha256']==hashlib.sha256(source).hexdigest()
    # The image URLs are *source leads*, never proof they depict SOURCE.
    assert 'visual_reference_verified' not in receipt



@pytest.mark.asyncio
async def test_actual_publisher_html_gallery_captions_are_source_links_not_reference_proof(tmp_path):
    page=html(
        '<td style="text-align:justify">Жилое здание с щипцом, двумя эркерами '
        'и арочным порталом, перестроенное после войны.</td>'
        '<a href="../photo/show_photos.php?phid=103">'
        '<img src="../phsight/observed_103_sm.jpg" '
        'alt="Фасад у дворового проезда, историческая фотография" '
        'title="Фасад дома у бывшего угла. Октябрь 2018"></a>'
        '<img src="../img/site_banner.png" alt="Сайт музея">'
        '<img src="https://example.org/untrusted.png" alt="untrusted">'
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _:response(page))) as http:
        parsed=await Prussia39Adapter(Cache(tmp_path/'cache'),http,
            resolver=resolver).article(
                'https://www.prussia39.ru/sight/index.php?sid=103')
    assert parsed['status']=='completed'
    refs=parsed['source_image_links']
    assert refs==[
        'https://www.prussia39.ru/phsight/observed_103_sm.jpg',
        'https://www.prussia39.ru/img/site_banner.png']
    gallery=parsed['source_image_records'][0]
    assert gallery['image_url']==refs[0]
    assert gallery['publisher_img_alt']=='Фасад у дворового проезда, историческая фотография'
    assert gallery['publisher_img_title']=='Фасад дома у бывшего угла. Октябрь 2018'
    assert gallery['linked_publisher_page_url']==(
        'https://www.prussia39.ru/photo/show_photos.php?phid=103')
    assert gallery['reference_identity_inferred'] is False
    assert gallery['visual_subject_confirmed'] is False
    assert gallery['raw_image_fetched'] is False
    assert parsed['source_image_records'][1]['linked_publisher_page_url'] is None
    assert all('example.org' not in u for u in refs)
    assert parsed['raw_body_sha256_verified'] is True
