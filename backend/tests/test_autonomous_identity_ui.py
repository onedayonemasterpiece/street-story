from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from street_story.identity_progress import advance, current_projection
from street_story.identity_references import reference_images
from street_story.live import SYSTEM_INSTRUCTION


def test_old_durable_copy_is_refreshed_without_mutation_or_new_work():
    old = {'attempt': 1, 'elapsed_ms': 1234, 'finished': True, 'steps': [
        {'key': 'result', 'label': 'Найден вероятный вариант · подтвердите объект', 'status': 'warning'}]}
    before = deepcopy(old)
    new = current_projection(old)
    assert old == before
    assert new['elapsed_ms'] == 1234 and new['attempt'] == 1
    assert 'подтвердите' not in new['steps'][0]['label']


def test_chosen_reference_success_removes_obsolete_warning():
    state = advance({}, 'identity_reference_unavailable', {}, 1)
    state = advance(state, 'identity_finished', {
        'status': 'match', 'candidate_id': 'wiki:1', 'reference_verified': True}, 2)
    refs = next(s for s in state['steps'] if s['key'] == 'references')
    assert refs['status'] == 'done'
    assert 'проверен' in refs['label']


def test_uncertainty_is_not_an_order_to_name_the_object():
    for candidate in (None, 'wiki:1'):
        state = advance({}, 'identity_finished', {'status': 'uncertain', 'candidate_id': candidate}, 1)
        label = next(s['label'] for s in state['steps'] if s['key'] == 'result')
        assert 'подтвердите' not in label and 'уточните' not in label
    assert 'Не проси автора подтвердить объект' in SYSTEM_INSTRUCTION
    assert 'публикация всегда двухшаговая' in SYSTEM_INSTRUCTION


@pytest.mark.asyncio
async def test_receipts_describe_direct_public_addresses_without_fetch_or_verdict_cache():
    calls = []
    async def forbidden(request):
        calls.append(str(request.url))
        raise AssertionError('URL selection must not fetch images')
    candidates = [{'candidate_id':'wiki:1', 'reference_image_urls':[
        'https://127.0.0.1/private.jpg','https://upload.wikimedia.org/real.jpg']}]
    first, second = [], []
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        images = await reference_images(SimpleNamespace(), candidates,http=client,evidence=first)
        repeated = await reference_images(SimpleNamespace(), candidates,http=client,evidence=second)
    assert not calls and images == repeated == [('wiki:1','image/jpeg','https://upload.wikimedia.org/real.jpg')]
    assert first == second and first[0]['source_url'] == images[0][2]
    assert first[0]['delivery'] == 'direct_public_url'
    assert not any('sha' in key or 'cache' in key for key in first[0])
