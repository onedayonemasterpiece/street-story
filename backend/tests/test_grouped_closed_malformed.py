import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from street_story.errors import PermanentProviderError, RetryableProviderError
from street_story.gemini import GeminiUnavailable
from street_story.research_adapter import ProductResearchAdapter
from test_research_control import fixture


def response_receipt():
    return {'model_attempts': [{'category': 'malformed_response',
        'provider_request_id': 'actual-response-id', 'usage': {'total_tokens': 8559}}]}


def adapter_fixture(tmp_path, receipt):
    service, sid, photo = fixture(tmp_path)
    adapter = ProductResearchAdapter.__new__(ProductResearchAdapter)
    adapter.service, adapter.native_vision, adapter.client = service, None, None
    sends = []
    async def compare(snapshot, story, schema, context):
        sends.append('group' if story.get('_visual_image_parts') else 'pair')
        if not story.get('_visual_image_parts'):
            return {'result': {'status': 'uncertain'}, 'receipt': {'provider': 'google'}}
        error = GeminiUnavailable(1200, 'all_keys_unavailable')
        error.receipt = receipt
        raise error
    adapter.primary_vision = SimpleNamespace(available=True, compare_visual=compare)
    story = {'id': sid, 'photo_sha256': photo,
        '_visual_image_parts': [{'label': 'SOURCE', 'data': 'source'}, {'label': 'REF 1', 'data': 'ref1'}, {'label': 'REF 2', 'data': 'ref2'}],
        '_visual_reference_mapping': [{'reference_id': 'r1'}, {'reference_id': 'r2'}]}
    context = json.dumps({'references': story['_visual_reference_mapping']})
    return adapter, service, story, context, sends


@pytest.mark.asyncio
async def test_closed_malformed_group_permits_different_pair_without_repeating_group(tmp_path):
    adapter, service, story, context, sends = adapter_fixture(tmp_path, response_receipt())
    with pytest.raises(PermanentProviderError, match='group_pair_required'):
        await adapter.visual_verdict(b'snapshot', story, {}, context)
    with service.store.connection() as db:
        before = [dict(row) for row in db.execute('SELECT * FROM research_provider_attempts WHERE story_id=?', (story['id'],))]
    assert len(before) == 1
    receipt = json.loads(before[0]['receipt_json'])
    assert receipt['phase'] == 'failed'
    assert receipt['provider_send_state'] == 'response_closed'
    assert receipt['retry_safe'] is True and receipt['fallback_mode'] == 'pair'
    assert receipt['observation'] == response_receipt()
    # The identical closed group stays fenced, including after a new adapter call.
    with pytest.raises(PermanentProviderError, match='group_pair_required'):
        await adapter.visual_verdict(b'snapshot', story, {}, context)
    pair = {key: value for key, value in story.items() if not key.startswith('_visual_')}
    result = await adapter.visual_verdict(b'different-pair-snapshot', pair, {}, context)
    assert result['result']['status'] == 'uncertain'
    assert sends == ['group', 'pair']
    with service.store.connection() as db:
        after = [dict(row) for row in db.execute('SELECT * FROM research_provider_attempts WHERE story_id=?', (story['id'],))]
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['timeout', 'missing_id', 'unknown_usage', 'mixed_attempts'])
async def test_unconfirmed_group_outcome_remains_unknown_and_never_repeats(tmp_path, fault):
    receipt = deepcopy(response_receipt())
    item = receipt['model_attempts'][0]
    if fault == 'timeout':
        item['category'] = 'timeout'
    elif fault == 'missing_id':
        item.pop('provider_request_id')
    elif fault == 'unknown_usage':
        item['usage']['total_tokens'] = 'unknown'
    else:
        receipt['model_attempts'].append({'category': 'timeout'})
    adapter, service, story, context, sends = adapter_fixture(tmp_path, receipt)
    for _ in range(2):
        with pytest.raises(RetryableProviderError, match='group_outcome_unknown'):
            await adapter.visual_verdict(b'snapshot', story, {}, context)
    assert sends == ['group']
    with service.store.connection() as db:
        rows = list(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE story_id=?', (story['id'],)))
    assert len(rows) == 1
    saved = json.loads(rows[0]['receipt_json'])
    assert saved['phase'] == 'unknown' and saved['retry_safe'] is False
    assert saved['provider_send_state'] == 'possibly_sent'
