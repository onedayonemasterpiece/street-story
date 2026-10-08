"""One bounded visual-query recovery when coordinate-only recall misses an object.

Vision proposes search terms, never proof. Only fetched Wikimedia records become
candidates, and the existing visual proof gate must still accept actual images.
"""
from __future__ import annotations
import asyncio
import hashlib
import html
import json
import math
import re
from urllib.parse import quote

import httpx

from .gemini import GeminiUnavailable
from .identity_candidate_policy import wikipedia_coordinate_context, wikipedia_identity_eligible
from .identity_telemetry import record_identity_event
from .identity_references import canonical_reference, original_reference
from .providers import WIKIPEDIA_USER_AGENT, PermanentProviderError, RetryableProviderError

WIKI = 'https://ru.wikipedia.org/w/api.php'
COMMONS = 'https://commons.wikimedia.org/w/api.php'
IMAGE_SUFFIX = re.compile(r'\.(?:jpe?g|png|webp)$', re.I)


def plain(value, limit=700):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', ' ', str(value or '')))).strip()[:limit]


def region_hint(story):
    address = ((story or {}).get('_identity_search_context') or {}).get('reverse_address') or {}
    return ', '.join(dict.fromkeys(plain(address[key], 180) for key in
        ('city', 'town', 'village', 'state', 'country') if address.get(key)))


def queries_from(payload):
    if not isinstance(payload, dict):
        return '', [], '', ''
    queries = payload.get('wikipedia_queries')
    if not isinstance(queries, list):
        return '', [], '', ''
    entity = plain(payload.get('entity_name', ''), 180)
    queries = list(dict.fromkeys(plain(q, 180) for q in queries if isinstance(q, str) and q.strip()))[:2]
    visual_query = plain(payload.get('visual_query', ''), 180)
    commons = payload.get('commons_query', '')
    return entity, queries, visual_query, plain(commons, 180) if isinstance(commons, str) else ''


def _map_query_context(story, candidates):
    from .identity_source_selection import observed_address_context
    context = dict(story.get('_identity_search_context') or {})
    # Addresses identify their mapped entry only. Present every supplied anchor,
    # including address nodes absent from the physical building shortlist.
    anchors = []
    research = json.loads(story.get('research_json') or '{}')
    observed = (story.get('_identity_observed_candidates') or
        (research.get('visual_identity') or {}).get('observed_candidates') or [])
    for item in [*(context.get('nearby') or []), *observed, *candidates]:
        if not item.get('map_address'):
            continue
        anchor = {key: item[key] for key in ('candidate_id', 'map_address', 'map_coordinates', 'distance_m') if key in item}
        if anchor not in anchors:
            anchors.append(anchor)
    context['nearby_address_hypotheses'] = anchors
    context['observed_address_context'] = observed_address_context(story, candidates)
    return context


async def suggest(service, story, transcript, candidates):
    from google.genai import types
    from .identity_source_selection import (regional_source_profile, model_identity_context,
        first_wave_catalog, first_wave_schema, render_first_wave)
    schema = {'type': 'object', 'properties': {
        'entity_name': {'type': 'string'},
        'wikipedia_queries': {'type': 'array', 'items': {'type': 'string'}},
        'visual_query': {'type': 'string'},
        'commons_query': {'type': 'string'},
        'article_queries': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['entity_name', 'wikipedia_queries', 'visual_query', 'commons_query', 'article_queries']}
    research = json.loads(story.get('research_json') or '{}')
    observed = (story.get('_identity_observed_candidates') or
        (research.get('visual_identity') or {}).get('observed_candidates') or [])
    observed_ids = [item['candidate_id'] for item in observed if item.get('candidate_id')
        and item.get('identity_eligible') is not False
        and not ((item.get('map_object') or {}).get('tags') or {}).get('entrance')]
    if observed_ids:
        schema['properties']['observed_candidate_ids'] = {'type': 'array', 'maxItems': 6,
            'items': {'type': 'string', 'enum': observed_ids}}
    import copy
    legacy_schema = copy.deepcopy(schema)
    if 'observed_candidate_ids' in legacy_schema['properties']:
        legacy_schema['properties']['observed_candidate_ids'] = {'type': 'array', 'maxItems': 6, 'items': {'type': 'string'}}
    first_wave = first_wave_catalog(story, candidates)
    schema['properties']['first_wave_hypotheses'] = first_wave_schema(first_wave)
    schema['required'].append('first_wave_hypotheses')
    source_bytes = service._source_photo_bytes(story['id'])
    from .reference_image_codec import normalize_reference
    source_mime, source_bytes = await asyncio.to_thread(normalize_reference, source_bytes)
    prompt = (
        'Определи, что следует искать для установления конкретного физического объекта на фото. '
        'Это только поисковые гипотезы, не доказательство. Не выбирай заведомо неподходящее '
        'ближайшее здание. Разрешено узнавать известные сооружения без GPS, но не выдумывать '
        'неизвестное название. Фото может показывать только часть объекта. '
        'entity_name — короткое наиболее вероятное название именно сооружения/объекта по-русски; '
        'название города, района или общий тип здания не подходит. '
        'Используй только адрес и географию, подтверждённые доступными данными; не выдумывай их. '
        'Пустой region_hint означает неизвестную географию: используй OCR, авторский контекст '
        'и визуальные гипотезы, не считай снимок автоматически калининградским. '
        'article_queries — готовый план буквальных интернет-запросов для статей с современными внешними фотографиями, достаточный для разных правдоподобных гипотез. '
        'Сначала используй короткий запрос по реальному адресу или названию и городу без лишних ограничений. '
        'Сохраняй полное наблюдавшееся имя населённого пункта, тип улицы, литеру и диапазон номера. '
        'observed_address_context содержит реальные адресные якоря и точные связи входов '
        'с наблюдаемыми контурами зданий. Не игнорируй номер дома из адресного входа только потому, '
        'что у самого контура building нет addr:housenumber: ищи по этому якорю, не присваивая его SOURCE. '
        'Если SOURCE показывает близкий дом и есть правдоподобные адресные якоря, первые два запроса '
        'должны проверять конкретные наблюдавшиеся адреса с городом, а не общую архитектуру улицы, '
        'района или список достопримечательностей. Для каждого выбора сопоставь SOURCE и геометрию; '
        'ближайший якорь не обязательно верный. Третья гипотеза может быть по видимым признакам. '
        'Связь нескольких адресных входов с одним контуром не означает разные здания и не позволяет '
        'сочинить общий номер/диапазон, отсутствующий в исходных данных. '
        'Город обязателен в каждом запросе, включая английский визуальный запрос, если город наблюдался. '
        'Не заменяй конкретный адрес запросом «старые дома», «архитектура» или «достопримечательности» '
        'без номера, когда доступен подходящий реальный адрес. '
        'Первая волна — 2–3 различные сильные гипотезы; всего не более восьми запросов в двух волнах. '
        'first_wave_hypotheses — обязательные структурированные выборы для первой волны. '
        'Выбери разные group_key из first_wave_subjects по SOURCE и геометрии: это реальные поисковые '
        'гипотезы, не привязка SOURCE. Требуемое число наблюдавшихся групп указано в required_grounded_count. '
        'kind=address или observed_named требует точный subject_id из first_wave_subjects; query для них '
        'оставь пустым: хост отправит буквальный реальный адрес или наблюдавшееся имя с населённым пунктом. '
        'Два входа одного точного контура — одна группа, как и имя этого здания плюс его адрес. '
        'mapped_occupant_context сохраняет буквальное имя арендатора как поисковую подсказку, '
        'но пустой group_key не считается отдельным зданием и не покрывает required_grounded_count. '
        'Не подменяй выборы общей архитектурой улицы и не повторяй одну физическую догадку разными словами. '
        'kind=unmapped_named или appearance допускает query по распознаваемому сооружению вне каталога '
        'либо видимым признакам, subject_id тогда пустой; это не увеличивает покрытие наблюдавшихся групп. '
        'При отсутствии GPS, адресов или названий продолжай такими гипотезами без выдуманной географии. '
        'При одной доступной группе нужна одна; неизвестная принадлежность входа остаётся гипотезой '
        'адресной записи, не придуманным зданием. Дай короткий reason каждому выбору. '
        'article_queries — только оставшиеся альтернативы после этих структурированных выборов. '
        'Вторую волну выполняй только для конкретного отсутствующего evidence после первой. '
        'regional_source_profile содержит предпочтения источников из наблюдавшейся географии, не ответы. '
        'observed_candidate_ids — до шести реальных физических кандидатов из observed_physical_candidates, '
        'которые полезно добавить к активным гипотезам; это не подтверждение identity. '
        'Обычный дом может иметь данные о строительстве, эксплуатации или ремонте без исторической статьи. '
        'Современный внешний вид — требование к REF, а не обязательные слова каждого запроса. '
        'Предусмотри в плане отдельный запрос по фасаду, внешнему виду или фото с улицы для правдоподобного адреса, если простой запрос может дать лишь адресные справочники. '
        'address_anchors — полный список переданных реальных соседних адресных якорей, '
        'а не подтверждённый адрес SOURCE. Рассмотри их вместе с самим фото. '
        'Если несколько адресов правдоподобны, предложи содержательно разные запросы по этим адресам '
        'или видимым признакам; сначала проверь разные правдоподобные адреса простыми запросами. '
        'Для близкого обычного дома включи разные реальные подходящие адресные якоря в начало плана; '
        'несколько описательных перефразировок одной улицы не дают покрытия других адресных гипотез. '
        'Не расходуй весь план на одну догадку и её повтор на другом сайте. '
        'SOURCE — современный снимок: для визуального сравнения ищи современные фотографии '
        'нынешнего здания, фасада и адреса. Историческое здание не означает историческую фотографию. '
        'Не направляй этот поиск в общие старые фотоархивы вместо современных видов. '
        'Исторические названия и архивные материалы полезны для фактов после определения объекта. '
        'Если на фото близкий дом, сначала используй ближайшие улицы/подтверждённые адреса '
        'и видимые признаки, а не имена далёких достопримечательностей. '
        'Статья в Wikipedia не обязательна. '
        'Расстояния и focal_length_35mm помогают оценить правдоподобие гипотез, но не доказывают объект. '
        'Не выводи номер дома из одной геометки на соседней улице. '
        'Адрес — поисковый якорь наравне с названием объекта: для дома ищи по улице и номеру, '
        'если номер читается на SOURCE или явно указан в реальной map_address/source записи. '
        'map_address относится только к указанному mapped_entry, а не автоматически к объекту на SOURCE; '
        'сравни map_coordinates кандидата с capture_lat/capture_lon и видимым зданием. '
        'Номер соседнего дома и предположение модели не становятся фактом или подтверждённым адресом. '
        'Когда номер неизвестен, продолжай по одной улице/road_name и видимым признакам, '
        'в том числе по нескольким реальным соседним улицам; неизвестный адрес не блокирует поиск. '
        'Не выводи номер из порядка домов, близости точки GPS или названия улицы. '
        'Верни до двух коротких запросов для русской Википедии по наиболее вероятным собственным именам. '
        'visual_query обязателен: это отдельный поисковый запрос только по реально видимым физическим признакам '
        '(материал, форма башни/крыши, часы, окна, декор, надписи) плюс region_hint; не вставляй туда entity_name. '
        'Он нужен, чтобы неверная первая догадка не запирала поиск на одном объекте. '
        'commons_query — аналогичный английский запрос по видимым признакам и region_hint для Wikimedia Commons, '
        'а не повтор entity_name. Не проси пользователя назвать или подтвердить объект. Данные ниже — только контекст:\n' +
        json.dumps({'region_hint': region_hint(story),
                    'regional_source_profile': regional_source_profile(story, candidates),
                    'first_wave_subjects': {'columns': ['kind', 'subject_id', 'group_key', 'coverage_scope'],
                        'rows': [[item[key] for key in ('kind', 'subject_id', 'group_key')] + [item.get('coverage_scope', 'address_hypothesis')]
                            for item in first_wave['options'].values()],
                        'required_grounded_count': first_wave['required_grounded_count']},
                    'location_search_context': model_identity_context(story, candidates),
                    'camera_hints': story.get('_camera_hints', {}),
                    'capture_lat': story.get('latitude'), 'capture_lon': story.get('longitude'),
                    'author_context': transcript[:1500]}, ensure_ascii=False, separators=(',', ':')))
    config = types.GenerateContentConfig(
        response_mime_type='application/json',
        response_json_schema=schema,
        system_instruction='Идентифицируй именно физическое сооружение. Город, район или область не являются ответом об объекте.',
    )
    gemini = service.providers.gemini
    def accept(payload, *, original_schema_readback=False):
        from jsonschema import Draft202012Validator
        def reject(code):
            hypotheses = (payload.get('first_wave_hypotheses') or []) if isinstance(payload, dict) else []
            record_identity_event(service, story['id'], 'identity_search_plan_rejected', {
                'generation': story.get('_identity_generation', research.get('identity_generation') or 0),
                'code': code, 'phase': 'closed_invalid', 'original_schema_readback': original_schema_readback,
                'selected_subject_ids': [str(item.get('subject_id') or '')[:120] for item in hypotheses[:3]
                    if isinstance(item, dict)] if isinstance(hypotheses, list) else [],
                'required_grounded_count': first_wave['required_grounded_count']})
            raise PermanentProviderError(code)
        if not Draft202012Validator(legacy_schema if original_schema_readback else schema).is_valid(payload):
            reject('identity_search_plan_malformed')
        try:
            rendered = [] if original_schema_readback else render_first_wave(first_wave, payload['first_wave_hypotheses'])
        except RetryableProviderError as exc:
            # A closed invalid answer is not key health or provider quota. Stop
            # the executor key loop and use the existing qualified fallback.
            reject(str(exc))
        queries = [item['query'] for item in rendered]
        result = queries_from(payload)
        if rendered and len(queries) < 3 and result[2]:
            queries.append(result[2])
        queries.extend(payload.get('article_queries') or [])
        story['_identity_article_queries'] = list(dict.fromkeys(plain(q, 240) for q in queries
            if isinstance(q, str) and q.strip()))[:8]
        if result[2] and result[2] not in story['_identity_article_queries']:
            story['_identity_article_queries'] = [*story['_identity_article_queries'][:7], result[2]]
        story['_identity_search_plan_payload'] = {**payload,
            'article_queries': story['_identity_article_queries'],
            **({'first_wave_hypotheses': rendered, 'first_wave_contract': 'grounded-subjects-v1'} if not original_schema_readback
                else {'original_schema_readback': True})}
        from .identity_candidate_policy import promote_observed_candidates
        candidates[:] = promote_observed_candidates(candidates, observed,
            [*(payload.get('observed_candidate_ids') or []),
             *(item['group_key'] for item in rendered if item['group_key'].startswith('osm:way:')
                or item['group_key'].startswith('osm:relation:'))])
        return result
    async def call(key, timeout, *, model=None, quota=None):
        response = await gemini._generate(key, timeout, [
            types.Part.from_bytes(data=source_bytes, mime_type=source_mime), prompt], config,
            operation='grounded_research', model=model, quota=quota)
        payload = json.loads(response.text or '{}')
        return accept(payload)
    async def fallback(cause):
        planner = getattr(getattr(service.providers, 'research', None), 'plan_identity_search', None)
        if not callable(planner):
            raise cause
        # The qualified text worker plans from observed anchors, OCR/previous
        # observations and author context. It must not pretend to see SOURCE.
        result = await planner(story, prompt + '\nSOURCE image is unavailable to this text fallback. '
            'Use only supplied observed anchors and context; unknown visual details stay unknown.', schema)
        story['_identity_search_plan_route'] = 'qualified_text_fallback'
        record_identity_event(service, story['id'], 'identity_search_plan_fallback',
            {'cause': getattr(cause, 'code', type(cause).__name__)})
        return accept(result.get('result') or {}, original_schema_readback=result.get('original_schema_readback') is True)
    researcher = getattr(service.providers, 'research', None)
    readback = getattr(researcher, 'has_identity_search_plan_readback', None)
    if callable(readback) and readback(story):
        # An original addressed operation precedes both fresh Google work and
        # planner admission. Its frozen response keeps its original contract.
        return await fallback(RetryableProviderError('identity_search_plan_original_readback'))
    if hasattr(service, 'settings'):
        from .research_budget import reserve_work
        from .service import digest
        reserve_work(service, story['id'], 'planner_calls',
            [digest([story['photo_sha256'], prompt, schema])])
    routes = getattr(gemini, 'research_routes', None)
    if not hasattr(gemini, '_generate') or not hasattr(gemini, 'executor'):
        return await fallback(RetryableProviderError('identity_google_planner_unavailable'))
    if not routes:
        try:
            return await gemini.executor.execute('grounded_research', call)
        except (GeminiUnavailable, PermanentProviderError, RetryableProviderError) as exc:
            return await fallback(exc)
    retry_at = []
    for model, _pool, quota, executor in routes:
        async def routed_call(key, timeout, *, _model=model, _quota=quota):
            return await call(key, timeout, model=_model, quota=_quota)
        try:
            return await executor.execute('grounded_research', routed_call)
        except GeminiUnavailable as exc:
            if exc.retry_at is not None:
                retry_at.append(exc.retry_at)
            continue
        except PermanentProviderError as exc:
            if str(exc) == 'gemini:unsupported_model':
                continue
            return await fallback(exc)
        except RetryableProviderError as exc:
            return await fallback(exc)
    return await fallback(GeminiUnavailable(min(retry_at) if retry_at else None,
        'all_identity_discovery_models_unavailable'))


async def api(service, client, endpoint, params):
    params = {'action': 'query', 'format': 'json', 'formatversion': 2, **params}
    key = 'identity-discovery-v1:' + hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()
    cached = service.store.cache_get(key)
    if cached is not None:
        return cached
    response = await client.get(endpoint, params=params)
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError('identity_discovery_response_size')
    payload = response.json()
    if payload.get('error'):
        raise ValueError('identity_discovery_api_error')
    pages = payload.get('query', {}).get('pages', [])
    service.store.cache_put(key, pages, 86400 if pages else 300)
    return pages


def file_title(value):
    return re.sub(r'^(?:File|Файл|Image|Изображение):', 'File:', str(value), flags=re.I)


def image_urls(page):
    values = [(page.get('thumbnail') or {}).get('source'), (page.get('original') or {}).get('source')]
    for info in page.get('imageinfo', []):
        values.extend([info.get('thumburl'), info.get('url')])
    return list(dict.fromkeys(url for value in values if (url := canonical_reference(str(value or '')))))


def _tokens(value):
    return {token for token in re.findall(r'[a-zа-яё0-9]{4,}', plain(value, 500).casefold())}


def _category_names(page):
    return [re.sub(r'^Category:', '', str(item.get('title') or ''), flags=re.I)
            for item in page.get('categories', []) if isinstance(item, dict)]


def _generic_category(category):
    lowered = category.casefold()
    return ('russian heritage id' in lowered or any(fragment in lowered for fragment in (
        'cultural heritage monuments in russia', 'cc-by', 'uploaded via',
        'self-published', 'photographs by', 'files with', 'pages with',
        'wikimedia', 'coordinates', 'taken with', ' in kaliningrad oblast',
    )))


def _entity_keys(page, hint):
    keys = []
    hint_tokens = _tokens(hint)
    for category in _category_names(page):
        lowered = category.casefold()
        heritage = re.search(r'russian heritage id\s+(\d+)', lowered)
        if heritage:
            keys.append('heritage:' + heritage.group(1))
            continue
        if _generic_category(category):
            continue
        category_tokens = _tokens(category)
        if hint_tokens and category_tokens & hint_tokens:
            keys.append('category:' + ' '.join(sorted(category_tokens)))
    return list(dict.fromkeys(keys))[:8]


def _specific_aliases(page, hint):
    hint_tokens = _tokens(hint)
    scored = []
    for category in _category_names(page):
        if _generic_category(category):
            continue
        tokens = _tokens(category)
        overlap = len(tokens & hint_tokens)
        if overlap:
            scored.append((overlap, len(tokens), category))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[2] for item in scored[:4]]


def _name_score(candidate, entity_name):
    target = _tokens(entity_name)
    text = _tokens(str(candidate.get('name') or '') + ' ' + str(candidate.get('extract') or ''))
    overlap = len(target & text)
    rank = {'wikipedia_text_search': 5, 'commons_category': 4, 'commons_text_search': 2}.get(
        candidate.get('discovery'), 1)
    exact = 20 if entity_name and plain(candidate.get('name'), 180).casefold() == entity_name.casefold() else 0
    return exact + overlap * 10 + rank


def _alias_phrase(value):
    value = re.sub(r'\([^)]*\)', ' ', plain(value, 220))
    return re.sub(r'[^0-9A-Za-zА-Яа-яЁё]+', ' ', value.casefold()).strip()


def _alias_stems(value):
    words = re.findall(r'[0-9A-Za-zА-Яа-яЁё]{4,}', _alias_phrase(value))
    return {word if not re.fullmatch(r'[А-Яа-яЁё]+', word) else word[:max(4, len(word) - 2)]
            for word in words}


def _explicit_page_alias(left, right):
    entity = str(left.get('wikidata') or '')
    if not re.fullmatch(r'Q[1-9]\d*', entity) or entity != right.get('wikidata'):
        return False  # Mere mentions/current-use prose are retrieval hints, not entity links.
    left_name = _alias_phrase(left.get('name'))
    right_name = _alias_phrase(right.get('name'))
    left_extract = _alias_phrase(left.get('extract'))
    right_extract = _alias_phrase(right.get('extract'))
    left_stems = _alias_stems(left.get('name'))
    right_stems = _alias_stems(right.get('name'))
    left_extract_stems = _alias_stems(left.get('extract'))
    right_extract_stems = _alias_stems(right.get('extract'))
    return (
        (len(left_name) >= 10 and left_name in right_extract)
        or (len(right_name) >= 10 and right_name in left_extract)
        or (len(left_stems) >= 2 and left_stems.issubset(right_extract_stems))
        or (len(right_stems) >= 2 and right_stems.issubset(left_extract_stems))
    )


def merge_candidates(candidates, entity_name):
    if len(candidates) < 2:
        return candidates
    parent = list(range(len(candidates)))
    # Shared illustration/category membership is weaker than two conflicting
    # explicit entities. Track whole components so an unlabelled Commons file
    # cannot bridge them transitively.
    entity_ids = [{str(item['wikidata'])} if re.fullmatch(r'Q[1-9]\d*', str(item.get('wikidata') or ''))
                  else set() for item in candidates]
    # Keep distinct Wikipedia subjects separate even through an unlabelled
    # Commons bridge. Shared pixels/categories do not establish subject identity.
    page_ids = [{str(item['candidate_id'])} if item.get('discovery') == 'wikipedia_text_search'
                else set() for item in candidates]
    strong_keys = [{str(key) for key in item.get('entity_keys') or []
                    if str(key).startswith('heritage:')} for item in candidates]
    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            combined = entity_ids[left] | entity_ids[right]
            if len(combined) > 1:
                return
            combined_pages = page_ids[left] | page_ids[right]
            if (len(combined_pages) > 1
                    and not (entity_ids[left] & entity_ids[right] or strong_keys[left] & strong_keys[right])):
                return
            parent[right] = left
            entity_ids[left] = combined
            page_ids[left] = combined_pages
            strong_keys[left] |= strong_keys[right]
    refs = [{root for url in item.get('reference_image_urls', [])
             if (root := original_reference(str(url)))} for item in candidates]
    keys = [set(item.get('entity_keys') or []) for item in candidates]
    source_urls = [set(str(url) for url in item.get('source_urls', []) if url) for item in candidates]
    urls = [str(item.get('url') or '') for item in candidates]
    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            source_membership = (
                (urls[left] and urls[left] in source_urls[right])
                or (urls[right] and urls[right] in source_urls[left])
            )
            explicit_alias = (
                candidates[left].get('discovery') == 'wikipedia_text_search'
                and candidates[right].get('discovery') == 'wikipedia_text_search'
                and _explicit_page_alias(candidates[left], candidates[right])
            )
            if (entity_ids[find(left)] & entity_ids[find(right)] or refs[left] & refs[right]
                    or keys[left] & keys[right] or source_membership or explicit_alias):
                union(left, right)
    grouped = {}
    for index, item in enumerate(candidates):
        grouped.setdefault(find(index), []).append(item)
    merged = []
    for members in grouped.values():
        if len(members) == 1:
            merged.append(members[0])
            continue
        best = max(members, key=lambda item: _name_score(item, entity_name))
        references = list(dict.fromkeys(
            url for item in members for url in item.get('reference_image_urls', [])))[:6]
        sources = list(dict.fromkeys(
            str(item.get('url') or '') for item in members if item.get('url')))[:6]
        aliases = list(dict.fromkeys(
            str(item.get('name') or '') for item in members if item.get('name')))[:8]
        merged_name = best.get('name')
        explicit_entities = {item['wikidata'] for item in members if item.get('wikidata')}
        merged.append({
            **best, 'name': merged_name, 'reference_image_urls': references,
            # A Commons image/category must not erase a page's host exclusion
            # when it becomes the cluster representative.
            **({'identity_eligible': False,
                'identity_ineligible_reason': next((item.get('identity_ineligible_reason')
                    for item in members if item.get('identity_ineligible_reason')), 'context_subject')}
                if any(item.get('identity_eligible') is False for item in members) else {}),
            **({'wikidata': next(iter(explicit_entities))} if len(explicit_entities) == 1 else {}),
            'source_urls': sources, 'entity_aliases': aliases,
            'alias_candidate_ids': [item['candidate_id'] for item in members
                                    if item['candidate_id'] != best['candidate_id']],
            'multi_view': len({original_reference(url) for url in references
                               if original_reference(url)}) > 1,
            'discovery': 'wikimedia_entity_cluster',
        })
    return merged


async def category_candidates(service, client, searches, excluded, entity_name, *, failures=None):
    category_pages = {}
    for query in list(dict.fromkeys(value for value in searches if value))[:4]:
        try:
            pages = await api(service, client, COMMONS, {
                'generator': 'search', 'gsrsearch': query, 'gsrnamespace': 14,
                'gsrlimit': 3, 'prop': 'categoryinfo'})
        except (httpx.HTTPError, ValueError) as exc:
            if failures is not None:
                failures.append(exc)
            continue
        for page in pages:
            title = str(page.get('title') or '')
            if title.startswith('Category:'):
                category_pages.setdefault(title, page)
    result = []
    for title in list(category_pages)[:5]:
        cid = 'commonscat:' + hashlib.sha256(title.encode()).hexdigest()[:16]
        if cid in excluded:
            continue
        try:
            files = await api(service, client, COMMONS, {
                'generator': 'categorymembers', 'gcmtitle': title, 'gcmtype': 'file',
                'gcmlimit': 6, 'prop': 'imageinfo|categories', 'cllimit': 30,
                'iiprop': 'url|extmetadata', 'iiurlwidth': 1280})
        except (httpx.HTTPError, ValueError) as exc:
            if failures is not None:
                failures.append(exc)
            continue
        refs = list(dict.fromkeys(url for page in files for url in image_urls(page)))[:6]
        if not refs:
            continue
        category_name = re.sub(r'^Category:', '', title)
        source_urls = [f"https://commons.wikimedia.org/wiki/{quote(title.replace(' ', '_'))}"]
        source_urls.extend(
            f"https://commons.wikimedia.org/wiki/{quote(str(page.get('title') or '').replace(' ', '_'))}"
            for page in files if page.get('title'))
        entity_keys = ['category:' + ' '.join(sorted(_tokens(category_name)))]
        entity_keys.extend(key for page in files
                           for key in _entity_keys(page, entity_name or ' '.join(searches)))
        result.append({
            'candidate_id': cid, 'name': category_name,
            'url': source_urls[0], 'source_urls': list(dict.fromkeys(source_urls))[:6],
            'extract': category_name, 'reference_image_urls': refs,
            'entity_keys': list(dict.fromkeys(entity_keys))[:8],
            'entity_aliases': [category_name],
            'multi_view': len({original_reference(url) for url in refs
                               if original_reference(url)}) > 1,
            'discovery': 'commons_category',
        })
    return result


async def retrieve(service, wiki_queries, commons_query, excluded, *, entity_name='', story=None):
    if story and hasattr(service, 'settings'):
        from .research_budget import reserve_work
        reserve_work(service, story['id'], 'query_hypotheses', list(dict.fromkeys(
            ' '.join(query.split()).casefold() for query in [*wiki_queries, commons_query, entity_name] if query)))
    failures = []
    context = (story or {}).get('_identity_search_context') or {}
    wikipedia = getattr(getattr(service, 'providers', None), 'wikipedia', None)
    radii = [context.get('radius_m'), getattr(wikipedia, 'search_radius_m', None)]
    finite_radii = []
    for value in radii:
        try:
            value = float(value)
            if math.isfinite(value) and value > 0:
                finite_radii.append(value)
        except (TypeError, ValueError, OverflowError):
            pass
    local_radius = max(finite_radii, default=None)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False,
            headers={'User-Agent': WIKIPEDIA_USER_AGENT}) as client:
        jobs = [api(service, client, WIKI, {
            'generator': 'search', 'gsrsearch': query, 'gsrlimit': 3, 'gsrnamespace': 0,
            'prop': 'extracts|info|pageimages|images|pageprops|coordinates',
            'coprimary': 'primary', 'colimit': 'max', 'exintro': 1, 'explaintext': 1,
            'exchars': 1200, 'inprop': 'url', 'piprop': 'name|original|thumbnail',
            'pithumbsize': 1280, 'imlimit': 10}) for query in wiki_queries]
        responses = await asyncio.gather(*jobs, return_exceptions=True)
        pages = {}
        for response in responses:
            if isinstance(response, Exception):
                failures.append(response)
            if isinstance(response, list):
                for page in sorted(response, key=lambda item: item.get('index', 100)):
                    if page.get('pageid') and not page.get('missing'):
                        pages.setdefault(page['pageid'], page)
        selected = list(pages.values())[:6]
        titles = list(dict.fromkeys(
            [file_title(page.get('pageimage', '')) for page in selected if page.get('pageimage')]
            + [file_title(image.get('title', '')) for page in selected
               for image in page.get('images', [])
               if IMAGE_SUFFIX.search(str(image.get('title', '')))]))[:12]
        image_pages = await api(service, client, COMMONS, {
            'titles': '|'.join(titles), 'prop': 'imageinfo|categories', 'cllimit': 30,
            'iiprop': 'url|extmetadata', 'iiurlwidth': 1280}) if titles else []
        by_title = {page.get('title'): page for page in image_pages if page.get('imageinfo')}
        candidates = await category_candidates(
            service, client, [commons_query, *wiki_queries, entity_name], excluded, entity_name, failures=failures)
        for page in selected:
            cid = f"wiki:{page['pageid']}"
            if cid in excluded:
                continue
            pageimage = by_title.get(file_title(page.get('pageimage')), {}) if page.get('pageimage') else {}
            references = image_urls(page) + image_urls(pageimage)
            related_pages = [pageimage] if pageimage else []
            for image in page.get('images', []):
                related = by_title.get(file_title(image.get('title')), {})
                if related:
                    related_pages.append(related)
                    references.extend(image_urls(related))
            references = list(dict.fromkeys(references))[:6]
            if references:
                hint = commons_query or ' '.join(wiki_queries) or entity_name
                aliases = list(dict.fromkeys(
                    alias for related in related_pages
                    for alias in _specific_aliases(related, hint)))[:6]
                entity_keys = list(dict.fromkeys(
                    key for related in related_pages
                    for key in _entity_keys(related, hint)))[:8]
                candidates.append({
                    'candidate_id': cid,
                    'name': aliases[0] if aliases else plain(page.get('title'), 180),
                    'url': f"https://ru.wikipedia.org/wiki/{quote(str(page.get('title', '')).replace(' ', '_'))}",
                    'extract': plain(page.get('extract')), 'reference_image_urls': references,
                    'identity_eligible': wikipedia_identity_eligible(
                        str(page.get('title') or ''), str(page.get('extract') or '')),
                    **wikipedia_coordinate_context(page, story, local_radius),
                    'entity_keys': entity_keys, 'entity_aliases': aliases,
                    'discovery': 'wikipedia_text_search'})
        if commons_query:
            try:
                commons_pages = await api(service, client, COMMONS, {
                    'generator': 'search', 'gsrsearch': commons_query, 'gsrnamespace': 6,
                    'gsrlimit': 5, 'prop': 'imageinfo|categories', 'cllimit': 30,
                    'iiprop': 'url|extmetadata', 'iiurlwidth': 1280})
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(exc)
                commons_pages = []
            for page in sorted(commons_pages, key=lambda item: item.get('index', 100))[:5]:
                cid = f"commons:{page.get('pageid')}"
                refs = image_urls(page)
                if not refs or cid in excluded:
                    continue
                info = (page.get('imageinfo') or [{}])[0]
                description = plain(
                    (info.get('extmetadata', {}).get('ImageDescription') or {}).get('value'))
                name = re.sub(r'^(?:File|Файл):', '', plain(page.get('title'), 180))
                candidates.append({
                    'candidate_id': cid, 'name': name,
                    'url': f"https://commons.wikimedia.org/wiki/{quote(str(page.get('title', '')).replace(' ', '_'))}",
                    'extract': description, 'reference_image_urls': refs,
                    'entity_keys': _entity_keys(page, commons_query),
                    'discovery': 'commons_text_search'})
        from .identity_entity_aliases import enrich_entity_links
        if not candidates and failures:
            raise RetryableProviderError('identity_discovery_sources_waiting') from failures[0]
        return merge_candidates(enrich_entity_links(candidates, {}, selected), entity_name)[:10]


async def web_search_hints(service, visual_query, *, story=None):
    search = getattr(service.providers.gemini, 'search_web', None)
    if not visual_query or not callable(search):
        return []
    try:
        result = await asyncio.wait_for(search(
            f"{visual_query} {region_hint(story)}".strip(),
            {'purpose': 'identity_candidate_discovery', 'region': region_hint(story)},
        ), timeout=12)
    except Exception:
        return []
    sources = getattr(result, 'grounding_sources', None) or []
    return list(dict.fromkeys(
        title for source in sources[:8]
        if isinstance(source, dict)
        and (title := plain(source.get('title'), 180))
        and not title.startswith('http')
    ))[:3]


async def web_image_sources(service, entity_name, visual_query, *, story=None, first_ready=False):
    """Independent grounded search before confirmation, with saved provenance.

    Google and OpenCode retain independent availability. URL discovery never
    implies physical identity and never extracts publication facts.
    """
    from .errors import RetryableProviderError
    from .gemini import GeminiUnavailable
    query = story.get('_identity_search_query') if story else None
    if not query:
        query = (f'{entity_name} {region_hint(story)} современные фотографии фасада' if entity_name
                 else f'{visual_query} {region_hint(story)} фото').strip()
    routes, failures = [], []
    researcher = getattr(service.providers, 'research', None)
    ready_sources, ready = {}, asyncio.Event()
    def announce(sources):
        ready_sources.update({source['url']: source for source in sources})
        if ready_sources:
            ready.set()
    async def choose_observed(observed):
        selector = getattr(researcher, 'select_identity_sources', None)
        text_selector = getattr(service.providers.gemini, 'select_identity_sources', None)
        if not observed:
            return {'sources': [], 'source_selection': {'status': 'model_selected',
                'discovered_count': 0, 'selected_count': 0}}
        if (not callable(selector) and not callable(text_selector)) or story is None:
            raise RetryableProviderError('identity_source_selection_unavailable')
        try:
            if not callable(text_selector):
                raise RetryableProviderError('identity_text_selection_unavailable')
            selection_story = dict(story)
            photo_reader = getattr(service, '_source_photo_bytes', None)
            if callable(photo_reader):
                from .reference_image_codec import normalize_reference
                selection_story['_identity_selection_image'] = await asyncio.to_thread(
                    normalize_reference, photo_reader(story['id']))
            return await text_selector(query, observed, selection_story)
        except (GeminiUnavailable, RetryableProviderError, PermanentProviderError):
            if not callable(selector):
                raise
            return await selector(query, observed, story)

    async def progressive_search():
        observer = getattr(researcher, 'identity_search_observations', None)
        if not callable(observer):
            return await researcher.search_articles(query, story)
        task = asyncio.create_task(researcher.search_articles(query, story))
        seen = set()
        try:
            while not task.done():
                observed = observer(query, story)
                urls = {source['url'] for source in observed}
                if urls - seen:
                    seen.update(urls)
                    _retain_article_discovery(service, story, [], discovered_sources=observed)
                    try:
                        selected = await choose_observed(observed)
                        if selected['source_selection'].get('status') == 'model_selected':
                            _retain_article_discovery(service, story, selected['sources'],
                                discovered_sources=observed,
                                source_selections={'opencode_observed:' + query: {
                                    **selected['source_selection'], 'discovered_sources': observed}})
                            announce(selected['sources'])
                            record_identity_event(service, story['id'], 'identity_search_observations_ready', {
                                'provider': 'opencode', 'discovered_count': len(observed),
                                'source_count': len(selected['sources']), 'original_query_pending': not task.done()})
                    except (GeminiUnavailable, RetryableProviderError, PermanentProviderError):
                        # The addressed search continues. No raw sighting is
                        # promoted to a reader or visual proof after refusal.
                        pass
                await asyncio.wait({task}, timeout=1)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    if researcher is not None and story is not None:
        routes.append(('opencode', progressive_search))
    google = getattr(service.providers.gemini, 'discover_article_urls', None)
    if callable(google):
        routes.append(('google', lambda: asyncio.wait_for(
            google(query, purpose='identity'), timeout=45)))
    frozen_public = None
    if story:
        research = json.loads(story.get('research_json') or '{}')
        if hasattr(service.store, 'connection'):
            with service.store.connection() as db:
                research = json.loads(service._story_row(db, story['id'])['research_json'] or '{}')
        history = research.get('identity_article_discovery') or {}
        if (history.get('generation', 0) == int(story.get('_identity_generation', research.get('identity_generation') or 0))
                and history.get('photo_sha256') == story.get('photo_sha256')):
            previous = (history.get('source_selections') or {}).get('public_web:' + query) or {}
            if isinstance(previous.get('discovered_sources'), list):
                frozen_public = previous['discovered_sources']
    public_search = getattr(service.providers.gemini, '_public_web_search', None)
    if callable(public_search) or frozen_public is not None:
        # Existing URL/snippet discovery needs neither model quota nor another
        # framework. Acquired articles and vision still supply identity proof.
        async def public_inventory():
            if frozen_public is not None:
                return {'sources': frozen_public}
            return await asyncio.wait_for(public_search(query), timeout=15)
        routes.append(('public_web', public_inventory))
    assigned_route = (story or {}).get('_identity_search_route')
    if assigned_route:
        routes = [(provider, call) for provider, call in routes if provider == assigned_route]
    async def discover(provider, call):
        started = asyncio.get_running_loop().time()
        observed = []
        try:
            result = await call()
            sources = (result.get('sources') or []) if isinstance(result, dict) else (getattr(result, 'grounding_sources', None) or [])
            payload = result if isinstance(result, dict) else (getattr(result, 'payload', None) or {})
            receipt = payload.get('receipt') or {}
            observed = payload.get('discovered_sources', receipt.get('discovered_sources', sources))
            selection = payload.get('source_selection', receipt.get('source_selection'))
            if provider == 'public_web':
                # Retain raw sightings before the fenced semantic operation. Raw
                # inventory is separate from selected reader/vision sources.
                if story:
                    _retain_article_discovery(service, story, [], discovered_sources=observed,
                        source_selections={provider + ':' + query: {'status': 'selection_pending',
                            'discovered_sources': observed}})
                result = await choose_observed(observed)
                sources, selection = result['sources'], result['source_selection']
                selection = {**selection, 'discovered_sources': observed}
            if story:
                _retain_article_discovery(service, story, [], discovered_sources=observed,
                    source_selections={provider + ':' + query: selection or {'status': 'selection_unavailable'}})
            if not selection or selection.get('status') != 'model_selected':
                raise RetryableProviderError('identity_source_selection_unavailable')
        except Exception as exc:
            failures.append(exc)
            if story:
                if observed:
                    _retain_article_discovery(service, story, [], discovered_sources=observed,
                        source_selections={provider + ':' + query: {'status': 'selection_unavailable',
                            'code': getattr(exc, 'code', type(exc).__name__),
                            'discovered_count': len(observed), 'selected_count': 0,
                            'discovered_sources': observed}})
                record_identity_event(service, story['id'], 'identity_search_route_unavailable', {
                    'provider': provider, 'code': getattr(exc, 'code', type(exc).__name__),
                    'retry_at': getattr(exc, 'retry_at', None),
                    'elapsed_ms': round((asyncio.get_running_loop().time()-started)*1000)})
            return []
        if story:
            # Deliver each completed route to the existing visual worker while
            # slower searches finish. Never cancel an addressed OpenCode send.
            if sources:
                _retain_article_discovery(service, story, sources)
            record_identity_event(service, story['id'], 'identity_search_route_ready', {
                'provider': provider, 'source_count': len(sources),
                'elapsed_ms': round((asyncio.get_running_loop().time()-started)*1000)})
        announce(sources)
        return sources
    group = asyncio.gather(*(discover(provider, call) for provider, call in routes), return_exceptions=True)
    retain = getattr(researcher, 'retain_search_observer', None)
    if first_ready and callable(retain):
        ready_wait = asyncio.create_task(ready.wait())
        try:
            await asyncio.wait({group, ready_wait}, return_when=asyncio.FIRST_COMPLETED)
            if ready_sources and not group.done():
                retain(group)
                record_identity_event(service, story['id'], 'identity_search_first_sources_ready', {
                    'source_count': len(ready_sources), 'provider_requests_pending': True})
                return list(ready_sources.values())
        except BaseException:
            group.cancel()
            await asyncio.gather(group, return_exceptions=True)
            raise
        finally:
            ready_wait.cancel()
            await asyncio.gather(ready_wait, return_exceptions=True)
    results = await group
    sources = {}
    for result in results:
        if isinstance(result, BaseException):
            # A persistence/Stop fence is not a provider failure to bypass.
            raise result
        for source in result:
            if isinstance(source, dict) and source.get('url'):
                sources.setdefault(source['url'], source)
    if sources:
        return list(sources.values())
    if failures:
        if any(str(getattr(exc, 'code', str(exc))).startswith('identity_source_selection_') for exc in failures):
            raise RetryableProviderError('identity_source_selection_unavailable', retry_at=service.store.now()+30)
        retry = [getattr(exc, 'retry_at', None) or service.store.now() + 30 for exc in failures]
        raise GeminiUnavailable(min(retry) if retry else service.store.now() + 30, 'all_article_search_routes_unavailable')
    if not routes:
        raise RetryableProviderError('article_url_discovery_not_configured')
    return []


def _retain_article_discovery(service, story, sources, *, receipts=(), articles=(), planned_queries=(), query_results=None,
                              discovered_sources=(), source_selections=None, search_plan=None):
    """Keep every URL and fetched media outside the bounded identity catalog."""
    from .article_media import public_url
    from .research_control import research_stopped
    from .service import ConflictError, canonical
    captured = json.loads(story.get('research_json') or '{}')
    generation = int(story.get('_identity_generation', captured.get('identity_generation') or 0))
    def revision(research):
        control = (research.get('research_controls') or {}).get('identity') or {}
        return int(control.get('revision') or 0) if (control.get('photo_sha256') == story['photo_sha256']
            and control.get('identity_generation') == generation) else 0
    with service.store.tx() as db:
        row = service._story_row(db, story['id'])
        research = json.loads(row['research_json'] or '{}')
        if (row['photo_sha256'] != story['photo_sha256'] or int(research.get('identity_generation') or 0) != generation
                or revision(research) != revision(captured)
                or research_stopped(research, 'identity', photo_sha256=row['photo_sha256'], identity_generation=generation)):
            raise ConflictError('visual_comparison_changed', 'Фото или управление исследованием изменилось.')
        history = research.get('identity_article_discovery') or {}
        if history.get('generation') != generation or history.get('photo_sha256') != story['photo_sha256']:
            history = {'generation': generation, 'photo_sha256': story['photo_sha256'], 'queries': {}, 'sources': []}
        history['planned_queries'] = list(dict.fromkeys([
            *history.get('planned_queries', []),
            *(plain(query, 240) for query in planned_queries if isinstance(query, str) and query.strip())]))
        if search_plan is not None:
            history['search_plan'] = {**search_plan, 'photo_sha256': story['photo_sha256'],
                'generation': generation, 'control_revision': revision(research)}
        queries = history.setdefault('queries', {})
        for query, result in (query_results or {}).items():
            key = next((key for key in queries if ' '.join(key.split()).casefold() ==
                        ' '.join(query.split()).casefold()), query)
            previous = queries.get(key) or {}
            if result.get('status') == 'in_progress' and (
                    previous.get('status') in {'completed', 'in_progress', 'unknown', 'submitted'}
                    or previous.get('retry_at', 0) > service.store.now()):
                continue
            if (previous.get('status') == 'in_progress'
                    and previous.get('claim_id') != result.get('claim_id')):
                continue
            if previous.get('status') == 'completed' and result.get('status') != 'completed':
                continue
            queries[key] = result
        unique = {source['url']: source for source in history.get('sources', [])}
        previous_source_urls = set(unique)
        for source in sources:
            if isinstance(source, dict) and (url := public_url(str(source.get('url') or ''))):
                unique[url] = {**unique.get(url, {}), **source, 'url': url}
        history['sources'] = list(unique.values())
        discovered = {source['url']: source for source in history.get('discovered_sources', [])}
        for source in discovered_sources:
            if isinstance(source, dict) and (url := public_url(str(source.get('url') or ''))):
                discovered[url] = {**discovered.get(url, {}), **source, 'url': url}
        history['discovered_sources'] = list(discovered.values())
        history.setdefault('source_selections', {}).update(source_selections or {})
        pages = history.setdefault('pages', {})
        for receipt in receipts:
            url = receipt.get('url')
            if url not in unique or receipt.get('status') == 'deferred':
                continue
            page = pages.setdefault(url, {'source': dict(unique[url]), 'attempts': 0})
            page['status'] = receipt['status']
            page['attempts'] += 1
            page['source'].update({key: receipt[key] for key in ('gallery_cursor', 'gallery_slide_cursor', 'static_media_delivered') if key in receipt})
            if receipt.get('collection_boundary'):
                page['collection_boundary'] = receipt['collection_boundary']
                page['candidates'] = []
                page['detail_sources'] = receipt.get('detail_sources') or []
            media = [item for item in articles if item.get('discovery_provenance', {}).get('url') == url
                     or item.get('url') == receipt.get('final_url', url)]
            if media:
                page['candidates'] = media
        research['identity_article_discovery'] = history
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), story['id']))
        if set(unique) - previous_source_urls:
            # A reader can use a late source independently of the failed search
            # that scheduled this wait. Keep the job and all dispatch receipts.
            db.execute("UPDATE jobs SET available_at=MIN(available_at,?),updated_at=? "
                       "WHERE story_id=? AND kind='identity_visual' AND state IN ('ready','retry') "
                       "AND json_extract(payload_json,'$.identity_generation')=?",
                       (service.store.now(), service.store.now(), story['id'], generation))
    return history


def _claim_article_query(service, story, query):
    """Fence this exact query in the existing durable history before sending."""
    import uuid
    history = _retain_article_discovery(service, story, [])
    previous = next((result for key, result in history['queries'].items()
        if ' '.join(key.split()).casefold() == ' '.join(query.split()).casefold()), {})
    if (previous.get('status') in {'completed', 'in_progress', 'unknown', 'submitted'}
            or previous.get('retry_at', 0) > service.store.now()):
        return None, previous
    if hasattr(service, 'settings'):
        from .research_budget import reserve_work
        reserve_work(service, story['id'], 'query_hypotheses', [' '.join(query.split()).casefold()])
    token = uuid.uuid4().hex
    history = _retain_article_discovery(service, story, [], query_results={query: {
        'status': 'in_progress', 'sources': [], 'claim_id': token,
        'started_at': service.store.now()}})
    result = next((result for key, result in history['queries'].items()
                   if ' '.join(key.split()).casefold() == ' '.join(query.split()).casefold()), {})
    return (token if result.get('claim_id') == token else None), result


async def _resume_article_query(service, story, query, previous):
    """Observe the original search ledger; never reroute an unfinished query."""
    from .service import canonical, digest
    captured = json.loads(story.get('research_json') or '{}')
    generation = int(story.get('_identity_generation', captured.get('identity_generation') or 0))
    unit = canonical([query, story.get('_research_run_id')])
    logical = digest([story['id'], story['photo_sha256'], generation, 'search', unit])
    with service.store.connection() as db:
        row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE logical_id=? '
                         'ORDER BY created_at DESC,rowid DESC LIMIT 1', (logical,)).fetchone()
    receipt = json.loads(row['receipt_json']) if row else {}
    observed = {**previous, 'status': 'unknown', 'sources': [], 'code': 'research_article_query_dispatch_unknown'}
    if receipt.get('phase') == 'completed':
        observed.update(status='completed', sources=receipt.get('sources') or [], provider_receipt=receipt)
        observed.pop('code', None)
    elif (receipt.get('phase') == 'created'
          or receipt.get('session_id') and receipt.get('message_id')
          and receipt.get('phase') in {'prompt_intent', 'submitted', 'abort_intent', 'abort_outcome_unknown'}):
        researcher = getattr(service.providers, 'research', None)
        search = getattr(researcher, 'search_articles', None)
        if callable(search):
            try:
                result = await search(query, {**story, '_identity_generation': generation})
                observed.update(status='completed', sources=result.get('sources') or [],
                                provider_receipt=result.get('receipt'))
                observed.pop('code', None)
            except Exception as exc:
                observed['code'] = getattr(exc, 'code', type(exc).__name__)
    elif receipt.get('phase') == 'failed' and receipt.get('provider_send_state') == 'not_sent':
        observed.update(status='temporary_failure', code='research_article_query_not_sent',
                        retry_at=(receipt.get('route_failure') or {}).get('retry_at', service.store.now()+15))
    history = _retain_article_discovery(service, story, observed['sources'], query_results={query: observed})
    return next(result for key, result in history['queries'].items()
                if ' '.join(key.split()).casefold() == ' '.join(query.split()).casefold())


def next_visual_query(identity, seed, searches, planned_queries=()):
    """Explore existing physical hypotheses/views; never synthesize an identity.

    The seed stays fixed in the durable operation, preventing a suffix chain.
    Completed equivalent queries are skipped across reconnects. Each actual
    call still needs the existing search admission and visual acceptance gate.
    """
    from .identity_candidate_policy import candidate_identity_eligible
    def normalized(value):
        return ' '.join(str(value or '').split()).casefold()
    completed = {normalized(query) for query, result in searches.items() if result.get('status') in {'completed', 'in_progress', 'unknown', 'submitted'}}
    tried = {normalized(query) for query in searches}
    plan = list(dict.fromkeys(plain(query, 240) for query in planned_queries if query))
    untried = next((query for query in plan if normalized(query) not in tried), '')
    if untried:
        return untried
    bases = list(dict.fromkeys(plain(value, 120) for value in [seed, *(
        candidate.get('name') for candidate in identity.get('candidates', [])
        if not str(candidate.get('candidate_id') or '').startswith('web:')
        and candidate_identity_eligible(candidate))] if value))
    variants = [*plan, *bases, *(f'{base} другие ракурсы фасад вход' for base in bases),
                *(f'{base} вид сбоку сзади детали здания' for base in bases)]
    return next((query for query in variants if normalized(query) not in completed), '')


async def recover(service, story, transcript, candidates, excluded):
    providers = getattr(service, 'providers', None)
    gemini = getattr(providers, 'gemini', None)
    captured = json.loads(story.get('research_json') or '{}')
    history = captured.get('identity_article_discovery') or {}
    google_planner = (callable(getattr(gemini, '_generate', None))
        and callable(getattr(getattr(gemini, 'executor', None), 'execute', None)))
    independent_planner = callable(getattr(getattr(providers, 'research', None), 'plan_identity_search', None))
    if (not google_planner and not independent_planner and story.get('id')
            and callable(getattr(service, '_identity_snapshot', None))):
        # Callers may hold a snapshot from before the plan checkpoint. Durable
        # work takes precedence over that snapshot during a provider outage.
        _current, latest = service._identity_snapshot(story['id'])
        history = latest.get('identity_article_discovery') or history
    # Existing plans remain usable during planner outages. A transport fixture
    # with no planner and no durable plan has no discovery operation to start.
    if not (google_planner or independent_planner or history.get('search_plan') or history.get('planned_queries')):
        return None
    record_identity_event(service, story['id'], 'identity_discovery_started', {'candidate_count': len(candidates)})
    def already_proved():
        current, latest = service._identity_snapshot(story['id'])
        return (current['photo_sha256'] == story['photo_sha256']
            and int(latest.get('identity_generation') or 0) == int(story.get('_identity_generation') or 0)
            and (latest.get('visual_identity') or {}).get('status') in {'match', 'owner_confirmed'})
    def remaining():
        from .research_budget import require_remaining
        # Lightweight offline transport fixtures do not own durable settings.
        if hasattr(service, 'settings'):
            return require_remaining(service, story['id'], 'identity')
        return 240

    async def work():
        # Read persisted work before invoking any planner. A completed plan and
        # its closed/UNKNOWN query receipts survive provider outages and wakes.
        history = _retain_article_discovery(service, story, [])
        saved = history.get('search_plan') or {}
        captured = json.loads(story.get('research_json') or '{}')
        control = (captured.get('research_controls') or {}).get('identity') or {}
        revision = int(control.get('revision') or 0)
        valid_saved = (saved.get('photo_sha256') == story['photo_sha256']
            and saved.get('generation') == int(story.get('_identity_generation', captured.get('identity_generation') or 0))
            and saved.get('control_revision') == revision)
        # Legacy plans predate metadata; they remain reusable for the original
        # revision, preserving all submitted query identities across deployment.
        legacy_saved = bool(history.get('planned_queries')) and not saved and revision == 0
        if valid_saved or legacy_saved:
            payload = saved.get('payload') or {}
            entity_name, wiki_queries, visual_query, commons_query = queries_from(payload)
            story['_identity_article_queries'] = history.get('planned_queries') or []
            from .identity_candidate_policy import promote_observed_candidates
            observed = story.get('_identity_observed_candidates') or (
                captured.get('visual_identity') or {}).get('observed_candidates') or []
            candidates[:] = promote_observed_candidates(candidates, observed,
                payload.get('observed_candidate_ids') or [])
            record_identity_event(service, story['id'], 'identity_search_plan_reused',
                {'query_count': len(story['_identity_article_queries']), 'control_revision': revision})
        else:
            remaining()
            entity_name, wiki_queries, visual_query, commons_query = await suggest(
                service, story, transcript, candidates)
            payload = story.get('_identity_search_plan_payload') or {
                'entity_name': entity_name, 'wikipedia_queries': wiki_queries,
                'visual_query': visual_query, 'commons_query': commons_query,
                'article_queries': story.get('_identity_article_queries') or []}
            history = _retain_article_discovery(service, story, [],
                planned_queries=story.get('_identity_article_queries') or [],
                search_plan={'policy_version': ('bounded-search-plan-v3' if payload.get('first_wave_contract')
                    else 'bounded-search-plan-v2'), 'payload': payload,
                    'route': story.get('_identity_search_plan_route', 'google'),
                    'created_at': service.store.now()})
        if already_proved():
            return None
        from .article_media import article_candidates
        record_identity_event(service, story['id'], 'identity_web_media_started', {'generation': story.get('_identity_generation', 0)})
        history = _retain_article_discovery(service, story, [],
            planned_queries=story.get('_identity_article_queries') or [])
        sources, search_failures = [], []
        plan = history.get('planned_queries') or [
            (f'{entity_name} {region_hint(story)} современные фотографии фасада' if entity_name
             else f'{visual_query} {region_hint(story)} фото').strip()]

        async def ready_article_media(query_sources):
            current_history = _retain_article_discovery(service, story, query_sources)
            pages = current_history.get('pages') or {}
            cached, unread = [], []
            for source in current_history['sources']:
                page = pages.get(source['url']) or {}
                if page.get('status') in {'completed', 'partial'}:
                    cached.extend(page.get('candidates', []))
                if (page.get('status') not in {'completed', 'excluded'}
                        and page.get('retry_at', 0) <= service.store.now()):
                    unread.append({**source, **{key: value for key, value in (page.get('source') or {}).items()
                        if key in {'gallery_cursor', 'gallery_slide_cursor', 'static_media_delivered'}}})
            if cached:
                return cached
            from .live_visual_comparison import _article_acquisition_rank
            unread.sort(key=lambda source: (pages.get(source['url'], {}).get('attempts', 0),
                                            _article_acquisition_rank(source)))
            if not unread:
                return []
            receipts = []
            remaining()
            fetched = await article_candidates(service, story, unread, excluded,
                                               receipts=receipts, first_ready=True)
            _retain_article_discovery(service, story, query_sources, receipts=receipts, articles=fetched)
            return fetched

        async def search_query(query, route=None):
            nonlocal history
            if already_proved():
                return query, []
            previous = history.get('queries', {}).get(query) or {}
            query_sources = []
            if previous.get('status') == 'completed':
                query_sources = previous.get('sources') or []
            elif previous.get('retry_at', 0) <= service.store.now():
                if previous.get('status') not in {'in_progress', 'unknown', 'submitted'}:
                    remaining()
                claim_id, previous = _claim_article_query(service, story, query)
                if not claim_id:
                    if previous.get('status') in {'in_progress', 'unknown', 'submitted'}:
                        previous = await _resume_article_query(service, story, query, previous)
                    if previous.get('status') == 'completed':
                        query_sources = previous.get('sources') or []
                else:
                    remaining()
                    query_story = {**story, '_identity_search_query': query,
                        **({'_identity_search_route': route} if route else {})}
                    try:
                        query_sources = await web_image_sources(service, entity_name, visual_query, story=query_story, first_ready=True)
                        history = _retain_article_discovery(service, story, query_sources,
                            query_results={query: {'sources': query_sources, 'status': 'completed',
                                'search_unavailable': False, 'claim_id': claim_id}})
                    except (RetryableProviderError, GeminiUnavailable) as exc:
                        search_failures.append(exc)
                        history = _retain_article_discovery(service, story, [], query_results={query: {
                            'sources': [], 'status': 'temporary_failure', 'search_unavailable': True, 'claim_id': claim_id,
                            'retry_at': getattr(exc, 'retry_at', None) or service.store.now()+15}})
            return query, query_sources

        async def consume_query(query, query_sources):
            nonlocal sources
            sources = list({source['url']: source for source in [*sources, *query_sources]}.values())
            if query_sources:
                articles = await ready_article_media(query_sources)
                if already_proved():
                    return None
                if articles:
                    return {'status': 'uncertain', 'candidate_id': '', 'confidence': 0,
                        'observations': ['Найдены иллюстрации в статьях; продолжаю визуальное сравнение в Live.'],
                        '_article_media_pending': True, '_references_sent': []}, articles
                record_identity_event(service, story['id'], 'identity_query_without_reference', {
                    'query': query, 'source_count': len(query_sources), 'next_action': 'continue_saved_plan'})
            return None

        researcher = getattr(service.providers, 'research', None)
        retain = getattr(researcher, 'retain_search_observer', None)
        route_names = []
        if callable(getattr(researcher, 'search_articles', None)):
            route_names.append('opencode')
        if callable(getattr(gemini, 'discover_article_urls', None)):
            route_names.append('google')
        if callable(getattr(gemini, '_public_web_search', None)):
            route_names.append('public_web')
        # Different first-wave hypotheses receive independent route slots. This
        # avoids sending every query to the complete provider pool. Keep the
        # existing observer alive for addressed sibling receipts on early media.
        concurrent = callable(retain) and len(route_names) > 1
        wave = plan[:min(3, len(route_names))] if concurrent else []
        tasks = {asyncio.create_task(search_query(query, route_names[index]))
                 for index, query in enumerate(wave)}
        remaining_plan = iter(plan[len(wave):] if concurrent else plan)
        try:
            while tasks or remaining_plan is not None:
                if tasks:
                    done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    results = [task.result() for task in done]
                else:
                    query = next(remaining_plan, None)
                    if query is None:
                        remaining_plan = None
                        continue
                    # Later hypotheses are already model-planned. Advance only
                    # after earlier searches produced no usable reference.
                    results = [await search_query(query, route_names[0] if concurrent else None)]
                for query, query_sources in results:
                    result = await consume_query(query, query_sources)
                    if result is not None:
                        return result
        finally:
            if tasks:
                # No fresh work is spawned here; these are original dispatched
                # searches whose completion must remain observable.
                if callable(retain):
                    retain(asyncio.gather(*tasks, return_exceptions=True))
                else:
                    await asyncio.gather(*tasks, return_exceptions=True)

        if not sources and search_failures:
            raise search_failures[0]
        if already_proved():
            return None
        history = _retain_article_discovery(service, story, sources)
        pages = history.get('pages') or {}
        cached, unread = [], []
        for source in history['sources']:
            page = pages.get(source['url']) or {}
            if page.get('status') in {'completed', 'partial'}:
                cached.extend(page.get('candidates', []))
            if page.get('status') not in {'completed', 'excluded'}:
                unread.append({**source, **{key: value for key, value in (page.get('source') or {}).items()
                    if key in {'gallery_cursor', 'gallery_slide_cursor', 'static_media_delivered'}}})
        from .live_visual_comparison import _article_acquisition_rank
        unread.sort(key=lambda source: (pages.get(source['url'], {}).get('attempts', 0),
                                        _article_acquisition_rank(source)))
        if cached:
            return {'status': 'uncertain', 'candidate_id': '', 'confidence': 0,
                'observations': ['Сохранённые иллюстрации готовы для визуального сравнения.'],
                '_article_media_pending': True, '_references_sent': []}, cached
        receipts = []
        remaining()
        fetched = await article_candidates(service, story, unread, excluded, receipts=receipts, first_ready=True)
        _retain_article_discovery(service, story, sources, receipts=receipts, articles=fetched)
        articles = [*cached, *fetched]
        # Third-party illustrations belong to the current Live conversation.
        # Prepare the queue here; never start another provider conversation.
        if articles:
            return {'status': 'uncertain', 'candidate_id': '', 'confidence': 0,
                'observations': ['Найдены иллюстрации в статьях; продолжаю визуальное сравнение в Live.'],
                '_article_media_pending': True, '_references_sent': []}, articles
        if already_proved():
            return None
        web_hints = list(dict.fromkeys(plain(source.get('title'), 180) for source in sources))[:3]
        search_queries = list(dict.fromkeys([
            *wiki_queries,
            *([visual_query] if visual_query else []),
            *web_hints,
        ]))[:6]
        if web_hints:
            record_identity_event(service, story['id'], 'identity_web_search_hints', {
                'hint_count': len(web_hints)})
        discovered = await retrieve(
            service, search_queries, commons_query, excluded, entity_name=entity_name, story=story)
        record_identity_event(service, story['id'], 'identity_discovery_candidates', {
            'candidate_ids': [x['candidate_id'] for x in discovered],
            'query_count': len(search_queries) + bool(commons_query),
            'web_hint_count': len(web_hints),
            'entity_name_present': bool(entity_name)})
        if not discovered:
            return None
        # Six images maximum; source records, not hypotheses, define the candidates.
        result = await service._identify_photo_batch(story, transcript, discovered, reference_limit=6)
        return result, discovered
    try:
        return await asyncio.wait_for(work(), timeout=min(240, remaining()))
    except Exception as exc:
        from .research_budget import ResearchTerminated
        if isinstance(exc, ResearchTerminated):
            raise
        record_identity_event(service, story['id'], 'identity_discovery_unavailable', {'error_type': type(exc).__name__})
        if isinstance(exc, (RetryableProviderError, GeminiUnavailable, httpx.HTTPError, TimeoutError, ValueError)):
            raise RetryableProviderError('identity_discovery_waiting', retry_at=getattr(exc, 'retry_at', None)) from exc
        return None
