"""Independent SOURCE+MAP failure recovery: one owned visual send, never REF."""
import base64
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from street_story import identity_discovery
from street_story.gemini import GeminiUnavailable
from street_story.providers import RetryableProviderError
from street_story.errors import PermanentProviderError
from street_story.visual_attachments import direct_source_map_parts
from test_geometry_identity_plan import geometry_decision, geometry_setup, payload


def image_part(label, raw=b'img', mime='image/png'):
    return {'label': label, 'mime_type': mime, 'data': base64.b64encode(raw).decode(),
            'sha256': hashlib.sha256(raw).hexdigest()}


def test_source_map_is_not_external_ref_and_requires_original_hashes():
    sample = {'kind': 'source_map', 'parts': [image_part('SOURCE', b'a'), image_part('MAP', b'b')]}
    parts = direct_source_map_parts(sample)
    assert [part['label'] for part in parts] == ['SOURCE', 'MAP']
    assert [part['bytes'] for part in parts] == [b'a', b'b']
    assert all(part['url'].startswith('data:') for part in parts)
    sample['parts'][1]['sha256'] = 'a' * 64
    with pytest.raises(PermanentProviderError):
        direct_source_map_parts(sample)
    sample['parts'][1] = image_part('REF 1', b'b')
    with pytest.raises(PermanentProviderError):
        direct_source_map_parts(sample)


class GoogleUnavailable:
    def __init__(self, calls, retry_at):
        self.calls, self.retry_at = calls, retry_at

    async def execute(self, role, call):
        self.calls.append('google_admission_not_sent')
        raise GeminiUnavailable(self.retry_at, 'fixture_shared_rpd')


@pytest.mark.asyncio
async def test_google_rpd_falls_back_to_existing_visual_model_without_ref_or_text(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    service.settings = replace(service.settings, gemini_web_search_model='text_model',
        gemini_web_search_tertiary_model='visual_model')
    calls = []
    google = GoogleUnavailable(calls, service.store.now() + 77_000)

    async def never_google_sdk(*args, **kwargs):
        pytest.fail('Gemini admission denied this request before SDK send')

    async def independent(snapshot, prompt, schema, source_mime, source, map_mime, map_data):
        calls.append('opencode_source_map')
        assert source_mime == 'image/jpeg' and source
        assert map_mime == 'image/png' and map_data[:4] == bytes((137, 80, 78, 71))
        assert 'physical' in prompt.lower()
        assert schema['type'] == 'object'
        return {'result': payload(geometry_decision()),
                'receipt': {'provider_id': 'opencode', 'model_id': 'mimo-v2.6-flash-free',
                            'assistant_message_id': 'verified-visual-receipt',
                            'image_attachment_readback_verified': True}}

    async def no_text(*args, **kwargs):
        pytest.fail('Confirmed geometry needs neither TEXT fallback nor REF')
    service.providers.gemini = SimpleNamespace(executor=google, _generate=never_google_sdk,
        web_search_routes=[('visual_model', object(), object(), google)], research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_source_map=independent,
        identity_source_map_pending=lambda snapshot: False, plan_identity_search=no_text)
    await identity_discovery.prepare_search_plan(service, story, '', active)
    assert calls == ['google_admission_not_sent', 'opencode_source_map']
    assert story['_identity_search_plan_route'] == 'opencode_source_map'
    assert story['_identity_search_plan_payload']['geometry_proof']['candidate_id'] == 'osm:way:2'
    assert story['_identity_geometry_result']['proof_kind'] == 'geometry'


@pytest.mark.asyncio
async def test_unknown_other_provider_never_falls_through_to_google_or_text(tmp_path):
    service, story, active = geometry_setup(tmp_path)
    calls, pending = [], {'value': False}
    google = GoogleUnavailable(calls, service.store.now()+77_000)
    service.settings = replace(service.settings, gemini_web_search_model='text_model',
        gemini_web_search_tertiary_model='visual_model')

    async def alternative(*args):
        calls.append('opencode_started')
        pending['value'] = True
        raise RetryableProviderError('provider_submit_outcome_unknown')

    async def never(*args, **kwargs):
        pytest.fail('Pending image request cannot be duplicated through another route')
    service.providers.gemini = SimpleNamespace(executor=google, _generate=never,
        web_search_routes=[('visual_model', object(), object(), google)], research_routes=[])
    service.providers.research = SimpleNamespace(plan_identity_source_map=alternative,
        identity_source_map_pending=lambda snapshot: pending['value'], plan_identity_search=never)
    with pytest.raises(RetryableProviderError, match='readback_required'):
        await identity_discovery.prepare_search_plan(service, story, '', active)
    assert calls == ['google_admission_not_sent', 'opencode_started']
    with pytest.raises(RetryableProviderError, match='readback_required'):
        await identity_discovery.prepare_search_plan(service, story, '', active)
    assert calls == ['google_admission_not_sent', 'opencode_started', 'opencode_started']
