from __future__ import annotations

import base64
import copy
import io
import json
from contextlib import asynccontextmanager

import httpx
from PIL import Image
import pytest

from street_story.opencode_research import OpenCodeResearch, ResearchLimits, ResearchUnavailable, research_config, search_tool_sources


class Harness:
    def __init__(self):
        self.config = research_config('mimo-v2.6-flash-free')
        self.requests, self.checkpoints, self.admissions, self.sends, self.finalized = [], [], [], [], []
        self.message_id, self.parts = None, []
        self.result, self.tool_status = {'summary': 'Actual sources'}, 'completed'
        self.timeout = self.drop_image = self.prompt_timeout = False
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        payload = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, payload))
        path = request.url.path
        if path == '/config':
            return httpx.Response(200, json=self.config)
        if path == '/agent':
            agents = []
            for name, value in self.config['agent'].items():
                if value.get('disable'):
                    continue
                agents.append({'name': name, 'mode': 'primary', 'steps': value['steps'], 'options': {},
                               'permission': [{'permission': permission, 'pattern': '*', 'action': action}
                                              for permission, action in value['permission'].items()]})
            return httpx.Response(200, json=agents)
        if path == '/session' and request.method == 'POST':
            return httpx.Response(200, json={'id': 'sesBounded'})
        if path.endswith('/prompt_async'):
            self.message_id, self.parts = payload['messageID'], payload['parts']
            if self.prompt_timeout:
                raise httpx.ReadTimeout('lost response', request=request)
            return httpx.Response(204)
        if path.endswith('/abort'):
            return httpx.Response(200, json=True)
        if path.endswith('/message'):
            return httpx.Response(200, json=self.messages())
        raise AssertionError(path)

    def messages(self):
        if not self.message_id:
            return []
        user = {'info': {'role': 'user', 'id': self.message_id},
                'parts': [part for part in self.parts if not self.drop_image or part['type'] != 'file']}
        if self.timeout:
            return [user]
        tool = {'type': 'tool', 'tool': 'websearch', 'callID': 'call_actual',
                'state': {'status': self.tool_status, 'input': {'query': 'facade views'},
                          'output': 'Title: Useful gallery\nURL: https://example.org/gallery\nText: body\n',
                          'metadata': {'backend': 'exa'}, 'time': {'start': 1, 'end': 2}}}
        # Search tool present only for the prompt requesting search.
        search = any(part.get('type') == 'text' and part.get('text', '').startswith('Use websearch') for part in self.parts)
        assistant = {'info': {'role': 'assistant', 'id': 'msgAnswer', 'parentID': self.message_id,
                             'providerID': 'opencode', 'modelID': 'mimo-v2.6-flash-free', 'time': {'created': 1, 'completed': 2},
                             'tokens': {'input': 40, 'output': 20, 'reasoning': 0, 'cache': {'read': 10, 'write': 0}},
                             'cost': 0, 'structured': self.result},
                     'parts': ([tool] if search else []) + [{'type': 'text', 'text': 'https://invented.example/fake'}]}
        return [user, assistant]

    @asynccontextmanager
    async def admission(self, binding, workload):
        self.admissions.append((binding, workload))
        yield self

    async def before_send(self, metadata):
        self.sends.append(metadata)

    async def finalize(self, usage, status):
        self.finalized.append((usage, status))

    async def checkpoint(self, binding, receipt):
        self.checkpoints.append((copy.deepcopy(binding), copy.deepcopy(receipt)))

    def adapter(self, **kwargs):
        return OpenCodeResearch('http://127.0.0.1:4097', model_id='mimo-v2.6-flash-free',
                                client=self.client, admission=self.admission, checkpoint=self.checkpoint, **kwargs)


def sheet():
    output = io.BytesIO()
    Image.new('RGB', (24, 12), 'green').save(output, format='PNG')
    return output.getvalue()


@pytest.mark.asyncio
async def test_search_uses_actual_tool_urls_admission_and_durable_intent():
    h = Harness()
    result = await h.adapter().search_articles('Unknown photo facade', {'request_id': 'logical', 'generation': 0})
    assert [source['url'] for source in result['sources']] == ['https://example.org/gallery']
    assert result['sources'][0]['search_backend'] == 'exa'
    assert len(h.sends) == 1
    assert h.admissions[0][1]['max_steps'] == 3
    assert 'max_search_calls' not in h.admissions[0][1]
    assert any(receipt['phase'] == 'prompt_intent' for _binding, receipt in h.checkpoints)
    prompt = next(payload for _method, path, payload in h.requests if path.endswith('prompt_async'))
    assert 'format' not in prompt  # Installed1.18.31 native format fails persisted readback.
    assert 'Response JSON schema' in prompt['parts'][0]['text']
    assert 'tools' not in prompt
    session = next(payload for _method, path, payload in h.requests if path == '/session' and _method == 'POST')
    assert session['permission'] == [{'permission': '*', 'pattern': '*', 'action': 'deny'},
                                     {'permission': 'websearch', 'pattern': '*', 'action': 'allow'}]
    assert result['receipt']['assistants'][0]['tokens']['cache']['read'] == 10
    assert h.finalized[0][1] == 'completed'


@pytest.mark.asyncio
async def test_unstructured_search_summary_does_not_discard_actual_tool_sources():
    h = Harness()
    h.result = None
    result = await h.adapter().search_articles('Facade', {'request_id': 'logical'})
    assert result['sources'][0]['url'] == 'https://example.org/gallery'
    assert result['result'] == {'summary': ''}
    assert result['receipt']['summary_json_valid'] is False
    assert result['receipt']['phase'] == 'completed'


@pytest.mark.asyncio
async def test_vision_inline_actual_pixels_readback_and_no_search():
    h = Harness()
    h.result = {'status': 'mismatch'}
    result = await h.adapter().compare_image(sheet(), {'request_id': 'photo0', 'photo_sha256': 'owner'},
                                            {'type': 'object'}, 'SOURCE owner / REF park')
    image = next(part for part in h.parts if part['type'] == 'file')
    assert image['mime'] == 'image/png'
    assert base64.b64decode(image['url'].split(',', 1)[1]) == sheet()
    assert result['receipt']['image_attachment_readback_verified'] is True
    assert result['receipt']['image_usage'] == 'unknown'
    assert h.admissions[0][1]['image_bytes'] == len(sheet())
    assert h.admissions[0][1]['max_steps'] == 2
    assert not result['sources']


@pytest.mark.asyncio
async def test_missing_attachment_does_not_accept_textual_verdict():
    h = Harness()
    h.drop_image = True
    with pytest.raises(ResearchUnavailable, match='research_image_delivery_unverified'):
        await h.adapter().compare_image(sheet(), {'request_id': 'photo0'}, {'type': 'object'})


@pytest.mark.asyncio
async def test_fact_extraction_uses_supplied_capsule_schema_no_tool_or_new_fetch():
    h = Harness()
    h.result = {'facts': []}
    result = await h.adapter().extract_facts({'binding': {'request_id': 'facts'}, 'jsonschema': {'type': 'object'},
                                             'source_passages': [{'source_id': 's1', 'text': 'Construction was planned.'}]})
    assert result['result'] == {'facts': []}
    prompt = next(payload for _method, path, payload in h.requests if path.endswith('prompt_async'))
    assert prompt['agent'] == 'street-story-facts' and 'tools' not in prompt
    session = next(payload for _method, path, payload in h.requests if path == '/session' and _method == 'POST')
    assert session['permission'] == [{'permission': '*', 'pattern': '*', 'action': 'deny'}]
    assert 'Construction was planned.' in prompt['parts'][0]['text']


@pytest.mark.asyncio
@pytest.mark.parametrize('alteration,code', [({'permission': {'*': 'allow'}}, 'research_default_permissions_not_denied'),
                                            ({'mcp': {'foreign': {'enabled': True}}}, 'research_runtime_not_isolated'),
                                            ({'plugin': ['foreign']}, 'research_runtime_not_isolated'),
                                            ({'instructions': ['AGENTS.md']}, 'research_runtime_not_isolated'),
                                            ({'share': 'auto'}, 'research_runtime_not_isolated')])
async def test_coding_runtime_not_used_for_production_research(alteration, code):
    h = Harness()
    h.config.update(alteration)
    with pytest.raises(ResearchUnavailable, match=code):
        await h.adapter().search_articles('photo', {'request_id': 'r'})
    assert not h.sends and not any(method == 'POST' for method, _path, _payload in h.requests)


@pytest.mark.asyncio
async def test_permission_and_cost_bounds_are_not_only_prompt_instructions():
    h = Harness()
    h.config['agent']['street-story-search']['permission']['bash'] = 'allow'
    with pytest.raises(ResearchUnavailable, match='research_agent_extra_permissions'):
        await h.adapter().search_articles('photo', {'request_id': 'r'})
    h.config = research_config('mimo-v2.6-flash-free')
    h.config['provider']['opencode']['models']['mimo-v2.6-flash-free']['limit']['output'] = 32000
    with pytest.raises(ResearchUnavailable, match='research_model_output_not_bounded'):
        await h.adapter().search_articles('photo', {'request_id': 'r'})
    assert not h.sends


@pytest.mark.asyncio
async def test_missing_admission_or_durable_checkpoint_cannot_dispatch():
    h = Harness()
    adapter = h.adapter()
    adapter.admission = None
    with pytest.raises(ResearchUnavailable, match='research_admission_required'):
        await adapter.search_articles('photo', {'request_id': 'r'})
    adapter.admission, adapter.checkpoint = h.admission, None
    with pytest.raises(ResearchUnavailable, match='research_durable_checkpoint_required'):
        await adapter.search_articles('photo', {'request_id': 'r'})
    assert not h.requests


@pytest.mark.asyncio
async def test_timeout_aborts_and_preserves_correlation():
    h = Harness()
    h.timeout = True
    with pytest.raises(ResearchUnavailable, match='research_timeout') as failure:
        await h.adapter(limits=ResearchLimits(timeout_seconds=.005, poll_seconds=.001)).search_articles('photo', {'request_id': 'r'})
    assert failure.value.receipt['session_id'] == 'sesBounded'
    assert failure.value.receipt['abort_acknowledged'] is True
    assert h.checkpoints[-1][0]['request_id'] == 'r'
    assert sum(path.endswith('/abort') for _method, path, _payload in h.requests) == 1


@pytest.mark.asyncio
async def test_resume_reads_existing_attempt_without_model_resubmission():
    h = Harness()
    adapter = h.adapter()
    result = await adapter.search_articles('photo', {'request_id': 'r'})
    receipt = result['receipt']
    resumed = await adapter.search_articles('photo', {'request_id': 'r', 'session_id': receipt['session_id'],
                                                     'message_id': receipt['message_id'], 'phase': 'submitted'})
    assert resumed['sources'] == result['sources']
    assert len(h.sends) == 1
    assert sum(path.endswith('prompt_async') for _method, path, _payload in h.requests) == 1


@pytest.mark.asyncio
async def test_unknown_prompt_outcome_without_message_does_not_resubmit():
    h = Harness()
    with pytest.raises(ResearchUnavailable, match='research_submit_outcome_unknown'):
        await h.adapter().search_articles('photo', {'request_id': 'r', 'session_id': 'sesExisting', 'phase': 'prompt_intent'})
    assert not h.sends and not any(path.endswith('prompt_async') for _method, path, _payload in h.requests)


@pytest.mark.asyncio
async def test_lost_prompt_response_is_aborted_reconciled_never_blindly_retried():
    h = Harness()
    h.prompt_timeout = True
    with pytest.raises(ResearchUnavailable, match='research_transport_unavailable') as failure:
        await h.adapter().search_articles('photo', {'request_id': 'r'})
    assert failure.value.receipt['abort_acknowledged'] is True
    h.prompt_timeout = False
    receipt = failure.value.receipt
    result = await h.adapter().search_articles('photo', {'request_id': 'r', 'session_id': receipt['session_id'],
                                                       'message_id': receipt['message_id'], 'phase': receipt['phase']})
    assert result['receipt']['phase'] == 'completed' and len(h.sends) == 1


@pytest.mark.asyncio
async def test_search_backend_error_is_not_successful_empty_result():
    h = Harness()
    h.tool_status = 'error'
    with pytest.raises(ResearchUnavailable, match='research_search_backend_failed'):
        await h.adapter().search_articles('photo', {'request_id': 'r'})


@pytest.mark.parametrize('endpoint', ['https://127.0.0.1:4097', 'http://remote.example', 'http://user:secret@127.0.0.1',
                                      'http://127.0.0.1/coding', 'http://localhost:4097', 'http://127.0.0.1:4098'])
def test_endpoint_is_loopback_without_credentials_or_coding_path(endpoint):
    with pytest.raises(ValueError):
        OpenCodeResearch(endpoint, model_id='m')


def test_sources_are_deduplicated_only_from_tool_receipts_backend_unknown_not_guessed():
    message = {'info': {'id': 'msgActual'}, 'parts': [
        {'type': 'text', 'text': 'URL: https://fabricated.example'},
        {'type': 'tool', 'tool': 'websearch', 'callID': 'c', 'state': {'status': 'completed', 'metadata': {},
         'output': json.dumps({'results': [{'url': 'https://article.example', 'title': 'A'},
                                            {'url': 'http://unsafe.example'}, {'url': 'https://article.example'}]})}}]}
    sources, calls = search_tool_sources([message], 'q')
    assert len(sources) == 1 and sources[0]['search_backend'] == 'unknown'
    assert calls[0]['output_sha256']


@pytest.mark.asyncio
async def test_platform_transport_reuses_existing_service_without_http_client():
    class PlatformBackend:
        def __init__(self):
            self.calls = []
        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {"id": "sesExisting"}
    adapter = OpenCodeResearch("http://127.0.0.1:4097", model_id="m")
    backend = PlatformBackend()
    adapter.shared_backend = backend
    assert await adapter._request(None, "GET", "/session/sesExisting/message", params={"limit": 100}) == {"id": "sesExisting"}
    assert backend.calls == [("GET", "/session/sesExisting/message?limit=100", {"payload": None})]
