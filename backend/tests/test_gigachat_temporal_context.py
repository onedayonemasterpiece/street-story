"""Exercise the actual native v3 tool and final-request temporal context."""
import copy
import json

import httpx
import pytest

from street_story.research_adapter import fact_page_capsule
from street_story.source_assessment import VALUE_CONTRACT
from test_gigachat_research import Admission, Provider, client


@pytest.mark.asyncio
async def test_actual_giga_v3_final_preserves_frozen_date_source_and_bad_known_id():
    source = '25.06.2025. Дом восстановили в конце 1950-х. Сейчас фасад ремонтируют.'
    known = {'fact_id': 'known_current_repair', 'text': 'Фасад ремонтируют.', 'eligibility': 'eligible'}
    page = {'source_version_id': 'srcv_frozen', 'source_url': 'https://example.org/article',
            'core_text': source, 'evidence_passages': [{'passage_id': 0, 'text': source}]}
    context = {'review_as_of_date_utc': '2026-10-13', 'known_facts': [known],
               'confirmed_identity': {'candidate_id': 'selected_house', 'candidate_name': 'Дом'}}
    capsule = fact_page_capsule(page, context)
    original = copy.deepcopy(capsule)
    provider, admission = Provider(), Admission()
    chats = []

    async def handle(request):
        if request.url.path == '/api/v2/oauth':
            return await provider(request)
        body = json.loads(request.content)
        chats.append(body)
        system = body['messages'][0]['content']
        supplied = json.loads(body['messages'][1]['content'])
        assert supplied['context']['review_as_of_date_utc'] == '2026-10-13'
        assert supplied['known_fact_inventory'] == [known]
        assert 'context.review_as_of_date_utc' in system
        # Owner decision 20.10.2026: the state's date stays in fact.text, the source's name never does.
        assert 'date in fact.text as the time of the state' in system and 'never names its source' in system
        assert 'Resolve relative dates' in system and 'source publication date' in system
        assert 'unchanged text and ID as nonpositive' in system
        assert 'new claim with existing_fact_id=""' in system
        assert 'including approximate dates' in system
        assert '25.06.2025' not in system and known['text'] not in system
        if len(chats) == 1:
            assert body['function_call'] == {'name': 'get_evidence'}
            message = {'role': 'assistant', 'content': '', 'function_call': {
                'name': 'get_evidence', 'arguments': {'source_version_id': 'srcv_frozen', 'passage_ids': [0]}}}
            finish = 'function_call'
        else:
            evidence = json.loads(body['messages'][-1]['content'])
            assert evidence['text'] == source
            message = {'role': 'assistant', 'content': json.dumps({'facts': [], 'continuation_needed': False,
                       'source_matches_poi': True, 'source_content_valid': True})}
            finish = 'stop'
        return httpx.Response(200, json={'model': 'GigaChat-2', 'choices': [
            {'message': message, 'finish_reason': finish}]})

    adapter = client(handle, admission)
    try:
        await adapter.research('Read the supplied source', capsule=capsule, max_tool_calls=1)
    finally:
        await adapter.aclose()
    assert capsule == original and capsule['semantic_contract'] == VALUE_CONTRACT
    schema = chats[-1]['response_format']['schema']
    assert chats[-1]['response_format']['strict'] is True
    assert 'own_evidence_values' in schema['properties']['facts']['items']['required']
    assert 'review_as_of_date_utc' not in schema['properties']
    assert len(chats) == 2 and len(admission.sends) == 2
