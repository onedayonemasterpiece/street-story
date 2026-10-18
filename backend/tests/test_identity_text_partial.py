"""An invalid closed pointer cannot erase independent text acquisition leads."""
import json
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.providers import PermanentProviderError, RetryableProviderError
from test_geometry_identity_plan import Executor, geometry_setup


@pytest.mark.asyncio
@pytest.mark.parametrize('useful_queries', [False, True])
@pytest.mark.parametrize('pointer', ['unknown', '$0'])
async def test_closed_text_unreceived_subject_is_rejected_without_losing_independent_queries(tmp_path, useful_queries, pointer):
    service, story, candidates = geometry_setup(tmp_path)
    calls = []
    query = 'Received City street archive building history'
    raw = {'entity_name': '', 'wikipedia_queries': [], 'visual_query': '', 'commons_query': '',
           'article_queries': [query] if useful_queries else [],
           'first_wave_hypotheses': [{'kind': 'address', 'subject_id': pointer,
                                     'query': '', 'reason': 'Unresolved physical pointer'}],
           'observed_candidate_ids': [pointer]}

    async def unavailable(*args, **kwargs):
        calls.append('image')
        error = RetryableProviderError('fixture_unavailable')
        error.receipt = {'provider_send_state': 'not_sent'}
        raise error

    async def closed_text(snapshot, prompt, role_schema):
        calls.append('text')
        return {'result': raw, 'original_schema': role_schema}

    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=unavailable)
    service.providers.research = SimpleNamespace(plan_identity_search=closed_text)
    if useful_queries:
        await identity_discovery.prepare_search_plan(service, story, '', candidates)
        assert story['_identity_article_queries'] == [query]
        safe = story['_identity_search_plan_payload']
        assert safe['first_wave_hypotheses'] == []
        assert safe['observed_candidate_ids'] == []
    else:
        with pytest.raises(PermanentProviderError):
            await identity_discovery.prepare_search_plan(service, story, '', candidates)
        assert not story.get('_identity_article_queries')
    assert '_identity_geometry_result' not in story
    assert calls == ['image', 'text']
    invalid = service._identity_snapshot(story['id'])[1]['identity_closed_invalid_plan']
    assert json.loads(invalid['raw_json']) == raw
    assert invalid['phase'] == 'closed_invalid'
