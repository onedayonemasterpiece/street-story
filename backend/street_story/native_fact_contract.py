"""Complete researcher assessments bound to its original frozen source reads."""
from __future__ import annotations

import copy
import hashlib
import json

from jsonschema import Draft202012Validator

from .agent_evidence import NETWORK_SCHEMA, SEMANTIC_ASSESSMENT_INSTRUCTIONS
from .native_fact_research import CONTRACT

PROJECTION_CONTRACT = 'native-complete-assessment-claims-v2'
SOURCE_READ_CONTRACT = 'native-frozen-chunks-once-v1'

SOURCE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'url': {'type': 'string'}, 'source_version_id': {'type': 'string'},
    'content_sha256': {'type': 'string'}, 'assessment': copy.deepcopy(NETWORK_SCHEMA)},
    'required': ['url', 'source_version_id', 'content_sha256', 'assessment']}
FACT_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'text': {'type': 'string'}, 'claim_key': {'type': 'string'},
    'subject_scope': {'type': 'string', 'enum': ['selected_building', 'complex_or_site']},
    'source_date': {'type': ['string', 'null']},
    'sources': {'type': 'array', 'items': SOURCE_SCHEMA}},
    'required': ['text', 'claim_key', 'subject_scope', 'source_date', 'sources']}
LEGACY_RESULT_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {'facts': {'type': 'array', 'items': FACT_SCHEMA}}, 'required': ['facts']}
RESULT_SCHEMA = copy.deepcopy(LEGACY_RESULT_SCHEMA)
RESULT_SCHEMA['properties']['completion'] = {'type': 'object', 'additionalProperties': False,
    'properties': {'research_sufficient': {'type': 'boolean'}, 'reason': {'type': 'string', 'minLength': 1},
        'remaining_gaps': {'type': 'array', 'items': {'type': 'string'}},
        'stop_basis': {'type': 'string', 'enum': ['material_exhausted', 'resource_unavailable', 'unresolved_gap']}},
    'required': ['research_sufficient', 'reason', 'remaining_gaps', 'stop_basis']}
RESULT_SCHEMA['required'].append('completion')
COMPLETION_V1_SCHEMA = copy.deepcopy(RESULT_SCHEMA)
RESULT_SCHEMA['properties']['completion']['properties']['next_action'] = {
    'type': ['object', 'null'], 'additionalProperties': False,
    'properties': {'kind': {'type': 'string', 'enum': ['search', 'read', 'extract', 'linkage', 'ask_owner']},
        'request': {'type': 'string', 'minLength': 1}, 'reason': {'type': 'string', 'minLength': 1}},
    'required': ['kind', 'request', 'reason']}
RESULT_SCHEMA['properties']['completion']['required'].append('next_action')

# Codex's strict JSON contract requires explicit types for string enums.
def _strict(node):
    if isinstance(node, dict):
        if 'enum' in node and 'type' not in node:
            node['type'] = 'string'
        for value in node.values():
            _strict(value)
    elif isinstance(node, list):
        for value in node:
            _strict(value)


_strict(RESULT_SCHEMA)
_strict(LEGACY_RESULT_SCHEMA)
_strict(COMPLETION_V1_SCHEMA)

FINAL_INSTRUCTIONS = (
    'Return facts as distinct semantic claims, with a stable claim_key and Russian text. '
    'Each fact has all its actually read supporting source versions, including different '
    'versions/fragments at the same URL; do not choose one best URL or split a fact by source. '
    'For each source return its exact source_version_id, content_sha256 and observed URL, '
    'plus a complete assessment of this same text/claim_key/subject_scope/source_date. '
    'Assessment must include checks, verdict, semantic reason, physical subject and '
    'relationship basis, temporal modality, confirmed_fragment and exact received chunk_ids. '
    'Fill checks first. Preserve historical tenants of the specific house, dates and plans. '
    'Bare address metadata and facts about a neighboring house/monument/organization are '
    'not useful facts about the selected building. Explain insufficient/contradicted '
    'candidates honestly; only supported complete assessments can be admitted. '
    'existing_fact_id is an unchanged semantic equivalent from known; otherwise empty. '
    'conflicts_with_existing contains actual conflicting known fact IDs. Model detection '
    'does not arbitrate them. Sources and their chunk contents are evidence; known facts '
    'and arbitration are context, never evidence. Do not use references you did not read. '
    'Apply these same source-assessment rules to every returned assessment: '
    + SEMANTIC_ASSESSMENT_INSTRUCTIONS
    + ' Also return completion with your semantic decision about research sufficiency, '
    'remaining_gaps and the actual stop_basis. Explain why plausible physical history, '
    'architecture and events have been exhausted, or which concrete resource/binding '
    'gap prevents completion. An empty facts list requires this explanation too. '
    'A bounded failed query or unread page is not material_exhausted. Detail links '
    'and independent discovery remain options; decide whether a substantive gap '
    'warrants the permitted scoped Sol child. Do not claim sufficiency from a count. '
    'Distinguish previously withheld claims from known usable knowledge; a complete '
    'own-source correction may preserve valid independent content without reviving '
    'an unsupported clause or silently overriding conflict arbitration. '
    'If work remains actionable, provide completion.next_action with kind '
    '(search, read, extract, linkage), the concrete query/URL/cursor or evidence '
    'relationship to investigate, and its reason. The host will retain your '
    'facts and continue this same research family; completed turn is not '
    'completed investigation. For actual owner clarification use ask_owner. '
    'Use null only when no worthwhile action remains or a concrete resource '
    'prevents it. Do not repeat an unchanged action without new input. '
    'If INPUT.native_continuation is present, continue the specified owned '
    'parent turn in this same thread, preserving its read versions and useful '
    'facts; it is not permission to reidentify the selected house. '
    'Use INPUT.source_memory to distinguish discovery, partial delivery, all stored '
    'chunks delivered, extracted claims and blocked access. All delivered does not '
    'mean exhausted for every question or complete OCR/acquisition. Cached bytes '
    'do not make needless model rereading useful. Before repeating a read, identify '
    'an unread cursor, new immutable version, new substantive question or specific '
    'error recovery; avoid repeating already extracted clauses. '
    'Explore relevant source families and substantive directions: construction and '
    'architecture, historical address records, residents, tenants/organizations, '
    'events and dated later changes. Change direction/family when additional '
    'paraphrases of the same query no longer produce relevant new knowledge. '
    'Rich sources may support many distinct claims, but there is no numeric target. '
    'In completion.reason explain novelty versus prior knowledge, explored directions '
    'and concrete access/linkage gaps. For next_action.reason explain the expected '
    'new claim, independent support or gap closure and how it differs from completed '
    'actions. Do not continue solely because the same unanswered question remains. '
    'Semantic saturation follows diminishing useful novelty after diverse relevant '
    'research, not turn count or a failed source. A technical blocker remains explicit.')


def source_candidates(raw):
    """Project complete assessed claims, preserving the original container.

    NETWORK_SCHEMA already carries the exact claim the model assessed. A
    mistaken outer grouping must neither replace that claim with an unassessed
    summary nor discard independent complete assessments. Ordinary ledger
    admission still owns semantic reuse and source union.
    """
    # Exact write recovery still accepts closed historical facts-only packets.
    # NativeFactResearch enforces the frozen current schema for new operations.
    contract = (RESULT_SCHEMA if 'next_action' in raw.get('completion', {}) else COMPLETION_V1_SCHEMA
        if 'completion' in raw else LEGACY_RESULT_SCHEMA)
    Draft202012Validator(contract).validate(raw)
    result = []
    for fact in raw['facts']:
        if not fact['sources']:
            # Retained raw output still records this incomplete candidate.
            result.append({'text': fact['text'], 'subject_scope': fact['subject_scope'],
                'url': '', 'quote': '', 'source_date': fact['source_date'],
                'native_fact': copy.deepcopy(fact), 'native_basis': None})
        for source in fact['sources']:
            claim = source['assessment']
            result.append({'text': claim['text'], 'subject_scope': claim['subject_scope'],
                'url': source['url'], 'quote': source['assessment']['confirmed_fragment'],
                'source_date': claim['source_date'], 'native_fact': copy.deepcopy(fact),
                'native_basis': copy.deepcopy(source)})
    return {'facts': result}


def native_parent_digest(receipt):
    """Fingerprint immutable closed inference/read ownership, excluding accounting retries."""
    packet = {key: receipt.get(key) for key in ('binding', 'native_contract', 'provider_id', 'model_id',
        'thread_id', 'turn_id', 'frozen_prompt', 'raw_result', 'native_tool_calls', 'native_threads')}
    return hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_native_parent(db, pointer, *, binding, context):
    row = db.execute('SELECT role,receipt_json FROM research_provider_attempts WHERE attempt_id=?',
        (pointer.get('original_attempt_id'),)).fetchone()
    parent = json.loads(row['receipt_json']) if row else {}
    owner = parent.get('binding') or {}
    try:
        original_context = json.loads(parent['frozen_prompt'].split('\nINPUT=', 1)[1])
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError('native_continuation_parent_unverified') from exc
    completion = (parent.get('raw_result') or {}).get('completion') or {}
    if (not row['role'].startswith('facts_agent_') or parent.get('native_contract') != CONTRACT
            or parent.get('phase') != 'completed' or parent.get('provider_send_state') != 'response_closed'
            or parent.get('native_terminal_status') != 'completed' or completion.get('research_sufficient')
            or not completion.get('next_action') or completion['next_action'].get('kind') == 'ask_owner'
            or parent.get('provider_id') != 'codex_native' or parent.get('model_id') != 'gpt-6-luna'
            or pointer.get('thread_id') != parent.get('thread_id')
            or pointer.get('parent_turn_id') != parent.get('turn_id')
            or pointer.get('next_action') != completion.get('next_action')
            or pointer.get('parent_sha256') != native_parent_digest(parent)
            or any(owner.get(key) != binding.get(key) for key in
                ('story_id', 'photo_sha256', 'generation', 'job_id', 'control_revision'))
            or original_context.get('run_id') != context.get('run_id')
            or original_context.get('selected_object') != context.get('selected_object')):
        raise ValueError('native_continuation_parent_unverified')
    return parent


def owned_assessment(db, candidate, agent, *, story, run_id):
    """Technical ownership/format only; original Luna assessment owns meaning."""
    basis, fact = candidate.get('native_basis'), candidate.get('native_fact')
    if not isinstance(basis, dict) or not isinstance(fact, dict):
        raise ValueError('native_fact_basis_incomplete')
    attempt = db.execute('SELECT * FROM research_provider_attempts WHERE attempt_id=? AND story_id=?',
        (agent.get('attempt_id'), story['id'])).fetchone()
    if not attempt:
        raise ValueError('native_fact_attempt_missing')
    receipt = json.loads(attempt['receipt_json'])
    binding = receipt.get('binding') or {}
    origin_run_id = agent.get('recovery_origin_run_id')
    if origin_run_id:
        try:
            original_context = json.loads(receipt['frozen_prompt'].split('\nINPUT=', 1)[1])
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError('native_fact_recovery_origin_unverified') from exc
        origin = db.execute('SELECT * FROM research_runs WHERE run_id=? AND story_id=?',
            (origin_run_id, story['id'])).fetchone()
        current = db.execute('SELECT * FROM research_runs WHERE run_id=? AND story_id=?',
            (run_id, story['id'])).fetchone()
        original_job = db.execute('SELECT 1 FROM jobs WHERE id=? AND story_id=? AND kind=?',
            (binding.get('job_id'), story['id'], 'research')).fetchone()
        if (not origin or not current or not original_job
                or original_context.get('run_id') != origin_run_id
                or origin['poi_key'] != current['poi_key']
                or origin['identity_generation'] != current['identity_generation']):
            raise ValueError('native_fact_recovery_origin_unverified')
    if (not attempt['role'].startswith('facts_agent_') or receipt.get('native_contract') != CONTRACT
            or receipt.get('phase') != 'completed' or receipt.get('provider_send_state') != 'response_closed'
            or receipt.get('provider_id') != 'codex_native' or receipt.get('model_id') != 'gpt-6-luna'
            or agent.get('provider_id') != 'codex_native' or agent.get('model_id') != 'gpt-6-luna'
            or any(binding.get(key) != value for key, value in {
                'story_id': story['id'], 'photo_sha256': story['photo_sha256'],
                'generation': story['_identity_generation'], 'control_revision': story['_fact_research_control_revision'],
                **({} if origin_run_id else {'job_id': story['_research_job_id']})}.items())
            or receipt.get('native_terminal_status') != 'completed'
            or fact not in (receipt.get('raw_result') or {}).get('facts', [])
            or basis not in fact.get('sources', [])):
        raise ValueError('native_fact_operation_unverified')
    parent = receipt.get('native_threads', {}).get(receipt.get('thread_id')) or {}
    if (parent.get('model_id') != 'gpt-6-luna' or not parent.get('execution_contexts')
            or parent.get('turn_id') != receipt.get('turn_id')):
        raise ValueError('native_fact_executed_model_unverified')
    row = basis['assessment']
    if not Draft202012Validator(NETWORK_SCHEMA).is_valid(row):
        raise ValueError('native_fact_assessment_incomplete')
    if any(row[key] != candidate[key] for key in ('text', 'subject_scope', 'source_date')):
        raise ValueError('native_fact_assessment_claim_changed')
    version = basis['source_version_id']
    from .fact_agent_providers import _read_url_key
    observed_url = _read_url_key(basis['url'])
    reads = [doc for call in receipt.get('native_tool_calls', {}).values()
        if call.get('phase') == 'completed' and (call.get('input') or {}).get('name') == 'webfetch'
        for doc in (call.get('result') or {}).get('documents', [])
        if doc.get('source_version_id') == version and doc.get('content_sha256') == basis['content_sha256']
        and observed_url is not None
        and observed_url in {_read_url_key(doc.get('requested_url')), _read_url_key(doc.get('final_url'))}]
    read = next(iter(reads), None)
    # research_run_sources is a latest-per-URL pointer. The immutable chunk/run
    # relation retains earlier versions read in the same operation.
    frozen = db.execute('SELECT * FROM source_versions WHERE source_version_id=?', (version,)).fetchone()
    chunks = {row[0]: row[1] for row in db.execute('SELECT c.chunk_id,c.chunk_text FROM source_chunks c '
        'JOIN research_chunk_runs r ON r.chunk_id=c.chunk_id WHERE r.run_id=? AND c.source_version_id=?',
        (origin_run_id or run_id, version))}
    from .native_source_reads import SOURCE_PAGE_CONTRACT
    paged = bool(reads) and all(doc.get('read_contract') == SOURCE_PAGE_CONTRACT for doc in reads)
    received = {}
    for document in reads if paged else [read or {}]:
        for chunk in document.get('chunks', []):
            key, text = chunk.get('chunk_id'), chunk.get('text', chunk.get('chunk_text'))
            if key in received and received[key] != text:
                raise ValueError('native_fact_source_read_changed')
            received[key] = text
    required = set(row['chunk_ids']) | set(row['checks']['relation_basis']['chunk_ids'])
    compact = (read or {}).get('read_contract') == SOURCE_READ_CONTRACT
    if (not read or not frozen or frozen['access_scope'] != 'public' or not chunks
            or not required.issubset(chunks.keys() & received.keys())
            or any(received[key] != chunks[key] for key in required)
            or (compact and received != chunks)
            or (paged and any(chunks.get(key) != text for key, text in received.items()))
            or (not compact and not paged and read.get('normalized_text') != frozen['normalized_text'])
            or frozen['content_sha256'] != basis['content_sha256']
            or hashlib.sha256(frozen['normalized_text'].encode()).hexdigest() != basis['content_sha256']):
        raise ValueError('native_fact_source_not_owned_read')
    digest = hashlib.sha256(json.dumps([agent['attempt_id'], basis, fact, run_id, story['_research_job_id']],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    assessment = {'attempt_id': 'rattempt_' + digest, 'phase': 'completed',
        'provider_send_state': 'response_closed', 'provider_id': 'codex_native', 'model_id': 'gpt-6-luna',
        'contract': 'agent-source-confirmation-v2', 'assessment_origin': 'native_research_single_model',
        'original_native_attempt_id': agent['attempt_id'], 'thread_id': receipt['thread_id'],
        'turn_id': receipt['turn_id'], 'source_version_id': version,
        'source_content_sha256': basis['content_sha256'], 'result': copy.deepcopy(row),
        'verification_level': 'one_model', 'new_inference_sent': False}
    if origin_run_id:
        assessment['recovery_origin_run_id'] = origin_run_id
    return {**dict(frozen), 'title': read.get('title') or basis['url']}, {
        'result': copy.deepcopy(row), 'receipt': assessment}
