import hashlib
import json
from types import SimpleNamespace

import pytest

from street_story.errors import MalformedProviderResponse, PermanentProviderError
from street_story.gemini import GeminiUnavailable
from street_story.headless_identity import VERDICT_SCHEMA
from street_story.headless_vision import HeadlessVisionProvider
from test_reference_image_codec import jpeg


class Executor:
    def __init__(self, failure=None):
        self.failure = failure
        self.operations = []

    async def execute(self, operation, call):
        self.operations.append(operation)
        if self.failure:
            raise self.failure
        return await call('unit-test-key', 20)


def setup(status='match', usage=None):
    first, second = Executor(), Executor()
    calls = []
    verdict = {'status': status, 'candidate_id': 'web:gallery', 'reference_subject_candidate_id': 'wiki:1',
               'reference_subject_observations': ['Same arches as physical candidate.'],
               'confidence': .97, 'observations': ['Distinctive three windows and rear arch.'],
               'alternative_candidate_ids': [], 'shared_distinctive_geometry': True,
               'observable_correspondences': [{'source_detail': 'Three windows and rear arch',
                                              'reference_detail': 'Same three windows and rear arch'}]}

    async def generate(key, timeout, contents, config, **kwargs):
        assert key == 'unit-test-key' and timeout == 20
        calls.append({'contents': contents, 'config': config, **kwargs})
        return SimpleNamespace(text=json.dumps(verdict), usage_metadata=usage, response_id='provider-test-response')

    client = SimpleNamespace(research_routes=[('gemini-primary', None, 'quota-primary', first),
                                             ('gemini-fallback', None, 'quota-fallback', second)], _generate=generate)
    verification = {'models': [{'model': model, 'transport': 'gemini_generate_content',
                               'controls': {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}}
                              for model in ('gemini-primary', 'gemini-fallback')]}
    service = SimpleNamespace(providers=SimpleNamespace(gemini=client),
                              store=SimpleNamespace(now=lambda: 1000, cache_get=lambda _key: verification))
    context = {'comparison_id': 'comparison-1', 'references': [{'label': 'REF 1', 'candidate_id': 'web:gallery'}],
               'physical_candidates': [{'candidate_id': 'wiki:1', 'name': 'Physical gate',
                                        'alias_candidate_ids': ['osm:way:1']},
                                       {'candidate_id': 'wiki:2', 'name': 'Different gate'}],
               'reference_evidence': [{'candidate_id': 'web:gallery', 'model_image_sha256': 'a' * 64}]}
    return HeadlessVisionProvider(service), verdict, context, calls, first, second


def test_qualified_web_route_is_available_without_duplicate_executor():
    provider, _verdict, _context, _calls, _first, _second = setup()
    routes = provider.client.research_routes
    provider.client.web_search_routes = routes[:]
    provider.client.research_routes = routes[:1]
    assert provider._verified_routes() == routes


@pytest.mark.asyncio
async def test_native_image_transport_schema_admission_full_shortlist_and_receipt():
    usage = SimpleNamespace(prompt_token_count=800, candidates_token_count=140,
                            total_token_count=940, cached_content_token_count=0,
                            prompt_tokens_details=[SimpleNamespace(modality='IMAGE', token_count=258)])
    provider, _verdict, context, calls, first, second = setup(usage=usage)
    snapshot = jpeg((1280, 640))
    result = await provider.compare_visual(snapshot, {'id': 'story', 'photo_sha256': 'b' * 64,
                                                     '_identity_generation': 4}, VERDICT_SCHEMA, json.dumps(context))
    assert first.operations == ['grounded_research'] and not second.operations
    assert calls[0]['contents'][0].inline_data.data == snapshot
    assert calls[0]['contents'][0].inline_data.mime_type == 'image/jpeg'
    prompt = calls[0]['contents'][1]
    assert 'wiki:2' in prompt and 'osm:way:1' in prompt and 'reference_subject_observations' in prompt
    native_schema = calls[0]['config'].response_json_schema
    assert native_schema['properties']['candidate_id']['enum'] == ['', 'web:gallery']
    assert native_schema['properties']['reference_subject_candidate_id']['enum'] == ['', 'wiki:1', 'wiki:2']
    assert 'enum' not in VERDICT_SCHEMA['properties']['candidate_id']
    assert calls[0]['config'].response_mime_type == 'application/json'
    assert calls[0]['model'] == 'gemini-primary' and calls[0]['quota'] == 'quota-primary'
    receipt = result['receipt']
    assert receipt['model_image_sha256'] == hashlib.sha256(snapshot).hexdigest()
    assert receipt['model_image_bytes'] == len(snapshot)
    assert receipt['reference_evidence'] == context['reference_evidence']
    assert receipt['generation'] == 4 and receipt['provider_request_id'] == 'provider-test-response'
    assert receipt['usage'] == {'input_tokens': 800, 'output_tokens': 140, 'total_tokens': 940,
                                'cached_tokens': 0, 'image_tokens': 258, 'cost': 'unknown'}
    assert len(receipt['model_attempts']) == 1
    assert receipt['model_attempts'][0]['usage'] == receipt['usage']
    assert receipt['model_attempts'][0]['category'] == 'success'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['mismatch', 'uncertain'])
async def test_valid_semantic_negative_never_shops_other_model(status):
    provider, _verdict, context, calls, _first, second = setup(status=status)
    response = await provider.compare_visual(jpeg(), {}, VERDICT_SCHEMA, context)
    assert response['result']['status'] == status
    assert len(calls) == 1 and not second.operations
    assert response['receipt']['usage']['input_tokens'] == 'unknown'
    assert response['receipt']['usage']['cached_tokens'] == 'unknown'


@pytest.mark.asyncio
async def test_only_availability_failure_uses_next_configured_admitted_route():
    provider, _verdict, context, calls, first, second = setup()
    first.failure = GeminiUnavailable(1200, 'all_keys_unavailable')
    response = await provider.compare_visual(jpeg(), {}, VERDICT_SCHEMA, context)
    assert first.operations == second.operations == ['grounded_research']
    assert len(calls) == 1 and calls[0]['model'] == 'gemini-fallback'
    assert calls[0]['quota'] == 'quota-fallback'
    assert response['receipt']['availability_failures'][0]['retry_at'] == 1200


@pytest.mark.asyncio
async def test_all_routes_blocked_keep_retry_and_failure_receipt():
    provider, _verdict, context, calls, first, second = setup()
    first.failure = GeminiUnavailable(1300, 'all_keys_unavailable')
    second.failure = GeminiUnavailable(1200, 'shared_control_unavailable')
    with pytest.raises(GeminiUnavailable) as failure:
        await provider.compare_visual(jpeg(), {}, VERDICT_SCHEMA, context)
    assert failure.value.retry_at == 1200 and not calls
    assert failure.value.receipt['status'] == 'waiting'
    assert len(failure.value.receipt['availability_failures']) == 2


@pytest.mark.asyncio
async def test_native_schema_rejects_fake_observations_without_acceptance():
    provider, verdict, context, _calls, _first, second = setup()
    verdict['observations'] = 'A detached text description is not visual proof'
    with pytest.raises(MalformedProviderResponse):
        await provider.compare_visual(jpeg(), {}, VERDICT_SCHEMA, context)
    assert not second.operations


@pytest.mark.asyncio
@pytest.mark.parametrize('attachment', [b'', b'not-image', 'image-url-instead-of-bytes', b'x' * (480 * 1024 + 1)])
async def test_missing_or_invalid_pixels_never_reach_provider(attachment):
    provider, _verdict, context, calls, first, second = setup()
    with pytest.raises(PermanentProviderError):
        await provider.compare_visual(attachment, {}, VERDICT_SCHEMA, context)
    assert not calls and not first.operations and not second.operations


@pytest.mark.asyncio
async def test_missing_reference_context_never_reaches_provider():
    provider, _verdict, _context, calls, first, _second = setup()
    with pytest.raises(PermanentProviderError):
        await provider.compare_visual(jpeg(), {}, VERDICT_SCHEMA, {'references': []})
    assert not calls and not first.operations


@pytest.mark.asyncio
@pytest.mark.parametrize('verification', [None, {}, {'models': [{'model': 'gemini-primary',
    'transport': 'gemini_generate_content', 'controls': {'positive': 'match', 'negative': 'match', 'pixel_transport_verified': True}}]},
    {'models': [{'model': 'gemini-primary', 'transport': 'text_only',
                 'controls': {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True}}]},
    {'models': [{'model': 'gemini-primary', 'transport': 'gemini_generate_content',
                 'controls': {'positive': 'match', 'negative': 'mismatch'}}]}])
async def test_unverified_or_failed_negative_control_is_never_admitted(verification):
    provider, _verdict, context, calls, first, second = setup()
    provider.service.store.cache_get = lambda key: verification
    assert not provider.available
    with pytest.raises(GeminiUnavailable) as failure:
        await provider.compare_visual(jpeg(), {}, VERDICT_SCHEMA, context)
    assert failure.value.receipt['category'] == 'no_verified_route'
    assert not calls and not first.operations and not second.operations


@pytest.mark.asyncio
async def test_internal_probe_can_measure_unverified_route_without_marking_verified():
    provider, _verdict, context, calls, _first, _second = setup()
    provider.service.store.cache_get = lambda key: None
    response = await provider.compare_visual(jpeg(), {}, VERDICT_SCHEMA, context, verification_probe=True)
    assert response['receipt']['verification_probe']
    assert calls and not provider.available


@pytest.mark.asyncio
async def test_physical_hypothesis_cannot_be_returned_as_unsent_reference_id():
    provider, verdict, context, _calls, _first, _second = setup()
    verdict['candidate_id'] = 'wiki:1'
    with pytest.raises(MalformedProviderResponse):
        await provider.compare_visual(jpeg(), {}, VERDICT_SCHEMA, context)


@pytest.mark.asyncio
@pytest.mark.parametrize('values', [{'confidence': float('nan')}, {'reference_subject_observations': [' ']}])
async def test_nonfinite_confidence_or_blank_subject_resolution_is_not_valid(values):
    provider, verdict, context, _calls, _first, _second = setup()
    verdict.update(values)
    with pytest.raises(MalformedProviderResponse):
        await provider.compare_visual(jpeg(), {}, VERDICT_SCHEMA, context)
