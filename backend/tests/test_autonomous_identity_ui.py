from copy import deepcopy
import hashlib
from types import SimpleNamespace

import httpx
import pytest

from street_story.identity_progress import advance, current_projection
from street_story.identity_references import reference_images
from street_story.live import SYSTEM_INSTRUCTION
from test_reference_image_codec import jpeg


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
async def test_receipts_describe_actual_normalized_bytes_not_proposed_urls():
    fixture = jpeg()
    calls = []
    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=fixture)
    service = SimpleNamespace()
    candidates = [{'candidate_id': 'wiki:1', 'reference_image_urls': [
        'https://evil.invalid/not-authorized.jpg', 'https://upload.wikimedia.org/real.jpg']}]
    receipts = []
    cached_receipts = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        images = await reference_images(service, candidates, http=client, evidence=receipts)
        await reference_images(service, candidates, http=client, evidence=cached_receipts)
    assert len(calls) == len(images) == len(receipts) == 1
    assert receipts[0]['source_url'] == calls[0]
    assert receipts[0]['model_image_sha256'] == hashlib.sha256(images[0][2]).hexdigest()
    assert receipts[0]['model_image_bytes'] == len(images[0][2])
    assert not receipts[0]['cache_hit'] and cached_receipts[0]['cache_hit']
