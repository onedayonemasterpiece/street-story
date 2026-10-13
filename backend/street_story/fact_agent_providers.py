"""Tool loops over existing provider SDKs, quota gates and account admission.

Only public search and bounded public page reading are exposed. Unknown sends
are durable operations, never permission to start the same route again.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from .opencode_research import ResearchUnavailable

CANDIDATE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'text': {'type': 'string', 'minLength': 1, 'maxLength': 1200},
    'subject_scope': {'enum': ['selected_building', 'complex_or_site']},
    'url': {'type': 'string', 'minLength': 1, 'maxLength': 2048},
    'quote': {'type': 'string', 'minLength': 1, 'maxLength': 500},
    'source_date': {'type': ['string', 'null']}},
    'required': ['text', 'subject_scope', 'url', 'quote', 'source_date']}
RESULT_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'facts': {'type': 'array', 'maxItems': 24, 'items': CANDIDATE_SCHEMA}}, 'required': ['facts']}
TOOLS = [{'name': 'websearch', 'description': 'Discover public article URLs; snippets are discovery only.',
          'parameters': {'type': 'object', 'properties': {'query': {'type': 'string', 'maxLength': 500}},
                         'required': ['query'], 'additionalProperties': False}},
         {'name': 'webfetch', 'description': 'Read a public article. Read it before citing its URL.',
          'parameters': {'type': 'object', 'properties': {'url': {'type': 'string', 'maxLength': 2048}},
                         'required': ['url'], 'additionalProperties': False}}]
def wire_tools():
    """SDK Schema supports a narrower subset; host validation stays strict."""
    return [{**item, 'parameters': {'type': 'object',
        'properties': {key: {'type': value['type']} for key, value in item['parameters']['properties'].items()},
        'required': item['parameters']['required']}} for item in TOOLS]


INSTRUCTIONS = ('Research the explicitly selected physical building. Independently search and read public articles. '
    'Only websearch and webfetch tools exist. Do not reidentify the owner selection. '
    'Find useful publication facts, including history, architecture, events and documented site context. '
    'Preserve dates, attribution, uncertainty and planned versus completed events. '
    'Dated mutable states describe their source time, never an unsupported current state. '
    'Return JSON {facts:[{text,subject_scope,url,quote,source_date}]}; text must be Russian. '
    'subject_scope is selected_building or complex_or_site; explicitly label site context. '
    'quote contains at most 25 words. source_date is the documented source date or null. '
    'Do not invent facts or reuse outside POI memory. A backend model independently checks each candidate.')


def validate_result(raw):
    if not Draft202012Validator(RESULT_SCHEMA).is_valid(raw):
        raise ValueError('agent_result_malformed')
    # Format and publication quote bounds only; source support is model assessed.
    if any(len(item['quote'].split()) > 25 for item in raw['facts']):
        raise ValueError('agent_quote_too_long')
    return raw


@dataclass
class AgentRoute:
    provider_id: str
    model_id: str
    endpoint: str
    client: object
    directory: str | None = None

    @property
    def profile_fingerprint(self):
        value = getattr(self.client, 'profile_fingerprint', None)
        return value or hashlib.sha256(json.dumps({'instructions': INSTRUCTIONS, 'tools': TOOLS,
            'schema': RESULT_SCHEMA, 'wire_tools': wire_tools(),
            'transport_contract': 'agent-tools-per-turn-admission-v2'}, sort_keys=True).encode()).hexdigest()

    @property
    def route_id(self):
        return hashlib.sha256(json.dumps([self.provider_id, self.model_id, self.endpoint,
            self.directory], sort_keys=True).encode()).hexdigest()[:24]


async def run_tool_agent(adapter, route, story, context, *, binding, guard):
    """A bounded agent task; no new scheduler, credential hopping or tool runtime."""
    if route.provider_id != 'opencode' and binding.get('phase') not in {None, 'created'}:
        raise ResearchUnavailable('research_agent_original_unaddressable', {'binding': binding,
            'phase': binding.get('phase'), 'provider_send_state': 'possibly_sent', 'retry_safe': False})
    prompt = INSTRUCTIONS + '\nINPUT=' + json.dumps(context, ensure_ascii=False)
    if route.provider_id == 'opencode':
        native = await route.client._run('agent_research', prompt, binding, RESULT_SCHEMA)
        try:
            raw = validate_result(native['result'])
        except (ValueError, TypeError) as exc:
            receipt = {**native['receipt'], 'phase': 'failed', 'error_code': 'agent_result_malformed'}
            adapter._checkpoint_receipt(binding, receipt)
            raise ResearchUnavailable('agent_result_malformed', receipt) from exc
        receipt = {**native['receipt'], 'prompt_sha256': hashlib.sha256(
            str(native['receipt'].get('frozen_prompt') or prompt).encode()).hexdigest()}
        return {**native, 'result': raw, 'receipt': receipt}
    receipt = {'binding': binding, 'phase': 'created', 'provider_id': route.provider_id,
        'model_id': route.model_id, 'frozen_prompt': prompt, 'frozen_schema': RESULT_SCHEMA,
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'provider_send_state': 'not_sent', 'sources': [], 'model_attempts': []}
    adapter._checkpoint_receipt(binding, receipt)
    sent = False
    fetched = set()
    started = time.monotonic()

    def checkpoint_send(metadata=None):
        nonlocal sent
        guard()
        sent = True
        receipt.update(phase='submitted', provider_send_state='possibly_sent')
        receipt['last_request_sent'] = True
        if metadata:
            receipt['last_send'] = metadata
        adapter._checkpoint_receipt(binding, receipt)

    async def tool(name, args):
        guard()
        declaration = next((item for item in TOOLS if item['name'] == name), None)
        if not declaration or not Draft202012Validator(declaration['parameters']).is_valid(args):
            raise ValueError('agent_tool_outside_scope')
        client = adapter.service.providers.gemini
        if name == 'websearch':
            result = await client._public_web_search(args['query'])
            value = {'sources': result.grounding_sources}
        else:
            docs = await client._fetch_page_documents([args['url']], {'research_run_id': context['run_id']})
            docs = list(docs.values())
            value = {'documents': [{key: doc.get(key) for key in ('requested_url', 'final_url', 'title',
                'source_version_id', 'content_sha256', 'read_status', 'normalized_text')} for doc in docs]}
            for doc in docs:
                fetched.update([doc.get('requested_url'), doc.get('final_url')])
                receipt['sources'].append({key: doc.get(key) for key in ('requested_url', 'final_url',
                    'source_version_id', 'content_sha256', 'read_status')})
        guard()
        receipt.setdefault('tool_receipts', []).append({'name': name, 'arguments': args,
            'completed_at': adapter.service.store.now(), 'result_sha256': hashlib.sha256(
                json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()})
        adapter._checkpoint_receipt(binding, receipt)
        return value

    def final(text):
        raw = validate_result(json.loads(text))
        if any(item['url'] not in fetched for item in raw['facts']):
            raise ValueError('agent_source_not_read')
        guard()
        receipt.update(phase='completed', provider_send_state='response_closed', result=raw,
            elapsed_seconds=time.monotonic()-started)
        adapter._checkpoint_receipt(binding, receipt)
        return {'result': raw, 'receipt': receipt, 'sources': receipt['sources']}

    try:
        async with asyncio.timeout(720):
            if route.provider_id == 'google':
                from google.genai import types
                client = route.client
                model, _pool, quota, executor = next(item for item in client.research_routes if item[0] == route.model_id)
                contents = [types.Content(role='user', parts=[types.Part(text=prompt)])]
                config = types.GenerateContentConfig(tools=[types.Tool(function_declarations=[
                    types.FunctionDeclaration(**item) for item in wire_tools()])], max_output_tokens=4096)
                for _ in range(24):
                    guard()
                    receipt['last_request_sent'] = False
                    async def call(key, timeout):
                        nonlocal sent
                        try:
                            return await client._generate(key, timeout, contents, config,
                                operation='grounded_research', model=model, quota=quota,
                                before_provider_send=checkpoint_send)
                        except Exception as exc:
                            from .gemini import classify_error
                            failure = classify_error(exc, now=adapter.service.store.now())
                            receipt['sdk_error'] = {'type': type(exc).__name__, 'code': failure.code,
                                'message': adapter.service.settings.redact(str(exc))}
                            adapter._checkpoint_receipt(binding, receipt)
                            if failure.code or getattr(exc, 'provider_send_state', None) == 'not_sent':
                                sent = False
                                receipt['provider_status'] = failure.code
                            raise
                    response = await executor.execute('grounded_research', call,
                        can_failover=lambda: not receipt['last_request_sent'])
                    sent = False
                    receipt['provider_send_state'] = 'response_closed'
                    usage = getattr(response, 'usage_metadata', None)
                    receipt['model_attempts'].append({'provider_request_id': getattr(response, 'response_id', None),
                        'usage': usage.model_dump(mode='json') if usage else None})
                    parts = response.candidates[0].content.parts
                    calls = [part.function_call for part in parts if part.function_call]
                    if not calls:
                        return final(response.text or '')
                    contents.append(response.candidates[0].content)
                    responses = []
                    for invocation in calls:
                        value = await tool(invocation.name, dict(invocation.args or {}))
                        responses.append(types.Part(function_response=types.FunctionResponse(
                            name=invocation.name, response=value)))
                    contents.append(types.Content(role='user', parts=responses))
            elif route.provider_id == 'gigachat':
                client = route.client
                messages = [{'role': 'system', 'content': INSTRUCTIONS},
                    {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]
                from ai_resource_control.client import estimate_input_tokens
                for turn in range(24):
                    guard()
                    body = {'model': route.model_id, 'messages': messages, 'stream': False,
                        'max_tokens': 4096, 'functions': wire_tools(),
                        'function_call': 'none' if turn == 23 else 'auto'}
                    child, saved = adapter.attempt(story, 'facts_agent_turn_'+route.route_id,
                        {'parent_attempt_id': binding['attempt_id'], 'turn': turn})
                    if saved:
                        raise ResearchUnavailable('research_agent_turn_already_closed', saved)
                    if child.get('phase') not in {None, 'created'}:
                        raise ResearchUnavailable('research_agent_turn_unknown', {'binding': child,
                            'phase': child.get('phase'), 'retry_safe': False})
                    child_receipt = {'binding': child, 'phase': 'created', 'provider_id': route.provider_id,
                        'model_id': route.model_id, 'parent_attempt_id': binding['attempt_id'], 'turn': turn,
                        'request_body_sha256': hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                        'provider_send_state': 'not_sent'}
                    adapter._checkpoint_receipt(child, child_receipt)
                    workload = {'purpose': 'fact_research', 'modalities': ['text'], 'images': 0,
                        'estimated_input_tokens': estimate_input_tokens(body), 'output_allowance': 4096,
                        'max_provider_sends': 2}
                    turn_receipts = []
                    async with client.admission({**client.binding, **child}, workload) as lease:
                        try:
                            async def before(metadata):
                                checkpoint_send(metadata)
                                child_receipt.update(phase='submitted', provider_send_state='possibly_sent', last_send=metadata)
                                adapter._checkpoint_receipt(child, child_receipt)
                            message, _reason = await client._completion(lease, body, turn_receipts,
                                'fact_research', 4096, before)
                            sent = False
                            child_receipt.update(phase='completed', provider_send_state='response_closed',
                                result={'message': message}, model_attempts=turn_receipts)
                            adapter._checkpoint_receipt(child, child_receipt)
                        except BaseException:
                            closed = bool(turn_receipts) and turn_receipts[-1].get('http_status') is not None
                            child_receipt.update(phase='failed' if closed or not sent else 'unknown',
                                provider_send_state='response_closed' if closed else 'possibly_sent' if sent else 'not_sent',
                                model_attempts=turn_receipts, retry_safe=False)
                            adapter._checkpoint_receipt(child, child_receipt)
                            raise
                        finally:
                            receipt['model_attempts'].extend(turn_receipts)
                            receipt.setdefault('turn_attempt_ids', []).append(child['attempt_id'])
                            usage_known = bool(turn_receipts) and all(type((row.get('usage') or {}).get('prompt_tokens')) is int
                                and type((row.get('usage') or {}).get('completion_tokens')) is int for row in turn_receipts)
                            await lease.finalize({'provider': 'gigachat', 'model': route.model_id,
                                'input_tokens': sum((row.get('usage') or {}).get('prompt_tokens', 0) for row in turn_receipts) if usage_known else None,
                                'output_tokens': sum((row.get('usage') or {}).get('completion_tokens', 0) for row in turn_receipts) if usage_known else None,
                                'usage_known': usage_known, 'cost': 'unknown', 'receipts': turn_receipts},
                                'completed' if turn_receipts and all(row.get('status') in {'succeeded', 'provider_error', 'credential_expired'}
                                    for row in turn_receipts) else 'unknown')
                    receipt['provider_send_state'] = 'response_closed'
                    invocation = message.get('function_call')
                    if not invocation:
                        return final(message.get('content') or '')
                    args = invocation.get('arguments')
                    args = json.loads(args) if isinstance(args, str) else args
                    value = await tool(invocation.get('name'), args)
                    messages.extend([message, {'role': 'function', 'name': invocation['name'],
                        'content': json.dumps(value, ensure_ascii=False)}])
            else:
                raise ValueError('agent_provider_unavailable')
            raise ValueError('agent_tool_rounds_exhausted')
    except BaseException as exc:
        if getattr(exc, 'provider_send_state', None) in {'not_sent', 'response_closed'}:
            sent = False
        if route.provider_id == 'gigachat' and receipt['model_attempts'] and receipt['model_attempts'][-1].get('http_status') is not None:
            sent = False
        receipt.update(phase='unknown' if sent else 'failed',
            provider_send_state='possibly_sent' if sent else 'response_closed' if receipt.get('last_request_sent') or receipt['model_attempts'] else 'not_sent',
            retry_safe=False, error_code=getattr(exc, 'code', None) or (str(exc)[:200] if isinstance(exc, ValueError) else type(exc).__name__),
            elapsed_seconds=time.monotonic()-started)
        adapter._checkpoint_receipt(binding, receipt)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ResearchUnavailable(receipt['error_code'], receipt) from exc
