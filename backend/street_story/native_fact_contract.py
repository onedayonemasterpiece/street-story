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
    'an unsupported clause or silently overriding conflict arbitration.')


def source_candidates(raw):
    """Project complete assessed claims, preserving the original container.

    NETWORK_SCHEMA already carries the exact claim the model assessed. A
    mistaken outer grouping must neither replace that claim with an unassessed
    summary nor discard independent complete assessments. Ordinary ledger
    admission still owns semantic reuse and source union.
    """
    # Exact write recovery still accepts closed historical facts-only packets.
    # NativeFactResearch enforces the frozen current schema for new operations.
    Draft202012Validator(RESULT_SCHEMA if 'completion' in raw else LEGACY_RESULT_SCHEMA).validate(raw)
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
    read = next((doc for call in receipt.get('native_tool_calls', {}).values()
        if call.get('phase') == 'completed' and (call.get('input') or {}).get('name') == 'webfetch'
        for doc in (call.get('result') or {}).get('documents', [])
        if doc.get('source_version_id') == version and doc.get('content_sha256') == basis['content_sha256']
        and basis['url'] in {doc.get('requested_url'), doc.get('final_url')}), None)
    # research_run_sources is a latest-per-URL pointer. The immutable chunk/run
    # relation retains earlier versions read in the same operation.
    frozen = db.execute('SELECT * FROM source_versions WHERE source_version_id=?', (version,)).fetchone()
    chunks = {row[0]: row[1] for row in db.execute('SELECT c.chunk_id,c.chunk_text FROM source_chunks c '
        'JOIN research_chunk_runs r ON r.chunk_id=c.chunk_id WHERE r.run_id=? AND c.source_version_id=?',
        (origin_run_id or run_id, version))}
    received = {chunk.get('chunk_id'): chunk.get('text', chunk.get('chunk_text'))
        for chunk in (read or {}).get('chunks', [])}
    required = set(row['chunk_ids']) | set(row['checks']['relation_basis']['chunk_ids'])
    compact = (read or {}).get('read_contract') == SOURCE_READ_CONTRACT
    if (not read or not frozen or frozen['access_scope'] != 'public' or not chunks
            or not required.issubset(chunks.keys() & received.keys())
            or any(received[key] != chunks[key] for key in required)
            or (compact and received != chunks)
            or (not compact and read.get('normalized_text') != frozen['normalized_text'])
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
