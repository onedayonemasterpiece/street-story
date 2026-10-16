"""Complete researcher assessments bound to its original frozen source reads."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from jsonschema import Draft202012Validator

from .agent_evidence import NETWORK_SCHEMA, SEMANTIC_ASSESSMENT_INSTRUCTIONS
from .native_fact_research import CONTRACT
from .review_packets import ATOMIC_PROPOSITION_CHECKS

PROJECTION_CONTRACT = 'native-complete-assessment-claims-v2'
SOURCE_READ_CONTRACT = 'native-frozen-chunks-once-v1'


class NativeFactSourceReferenceInvalid(ValueError):
    """A rejected reference with feedback from this already verified own read."""

    def __init__(self, assessment, *, version, content_sha256, received):
        super().__init__('native_fact_source_not_owned_read')
        invalid = []
        for path, values in ((['assessment', 'chunk_ids'], assessment['chunk_ids']),
                (['assessment', 'checks', 'relation_basis', 'chunk_ids'],
                    assessment['checks']['relation_basis']['chunk_ids'])):
            for index, value in enumerate(values):
                if value not in received:
                    # Public diagnostic identifiers only; arbitrary supplied
                    # strings may contain private payloads and are not echoed.
                    invalid.append({'path': [*path, index],
                        **({'reference': value} if re.fullmatch(r'chunk_[0-9a-f]{1,64}', value)
                            else {'reference_redacted': True})})
        keys = sorted(received)
        self.reference_diagnostics = {
            'contract': 'native-owned-chunk-reference-feedback-v1',
            'source_version_id': version, 'content_sha256': content_sha256,
            'invalid_references': invalid[:16], 'invalid_references_truncated': len(invalid) > 16,
            'received_chunk_ids': keys[:16],
            'received_chunk_ids_truncated': len(keys) > 16,
            'meaning': 'Reference correction only, from this source version and already received chunks. '
                'No fact accepted or semantic verdict supplied.'}

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
    + ATOMIC_PROPOSITION_CHECKS + ' '
    'Check temporal support for each proposition and list item separately. '
    'A date or period attached to one occupant/event does not date the others. '
    'An undated list of former uses does not establish a shared historical period. '
    'Read the available chronology or preserve the undated scope rather than '
    'extending a qualifier across independently selectable claims. '
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
    'INPUT.related_map_observations keeps organizations separate from building identity, '
    'with their own OSM IDs, links, original tags and spatial/address relationship. '
    'A related organization is positive evidence and a useful research lead, not bare '
    'address metadata. Assess present use, lifecycle tags, observation/check/edit times '
    'and contradictions proportionately; no universal second independent source is required. '
    'An edit/check date is not an opening date; an old opening alone does not prove current operation. '
    'Read a supplied retained_map_sources URL through webfetch for its already acquired '
    'immutable chunks, without another HTTP download, before saving an own map assessment. '
    'Follow useful explicit website/contact:website and Wikipedia/Wikidata links with '
    'the existing readers/resolvers. Do not assume a missing link or treat '
    'not_recorded_in_selected_snapshot as absence in OSM. A nearby unrelated organization '
    'does not become this house; spatial containment is not organizational ownership '
    'or guaranteed availability to visitors. '
    'existing_fact_id is an unchanged semantic equivalent from known; otherwise empty. '
    'conflicts_with_existing contains actual conflicting known fact IDs. Model detection '
    'does not arbitrate them. Sources and their chunk contents are evidence; known facts '
    'and arbitration are context, never evidence. Do not use references you did not read. '
    'KNOWN summaries or remembered identity links alone guide discovery but cannot '
    'establish a source\'s physical-subject relationship. Use an actually received '
    'identity passage with its owned read receipt before assessing a historical-name '
    'claim as supported, and identify that read source in the reason. Reuse a supplied '
    'verified frozen identity read with its receipt and scope when available; otherwise '
    'read the identity source with your native reader and retain its actual result. '
    'If an available read can close this gap, '
    'perform it before ending rather than returning conditionally supported claims. '
    'Without that evidence mark object_relation unestablished and verdict '
    'insufficient, never selected_building conditional on a known identity. '
    'A historical source need not repeat the modern address. When a separately read '
    'identity source links its historical name to the selected building or documented '
    'site, explain that chain and both sources in the assessment reason. Each '
    'relation_basis fragment and chunk_ids still belong to the source being assessed: '
    'use its actual naming passage, never copy a modern address or another source\'s '
    'chunks into it. A name alone without an established contextual link is insufficient. '
    'Assess the actual physical subject from the read sources. Several address '
    'numbers or entrances may identify the same house; the numbers alone do not '
    'establish a complex, separate bodies or uncertainty. Ordinary history of '
    'the same documented house remains selected_building without artificial '
    'site caveats. Use documented_site only when the actual evidence describes '
    'a larger site or a genuinely unresolved physical relationship. A different '
    'building cannot supply individual properties of the selected house. '
    'Before delivery check that subject_scope and checks.object_relation agree: '
    'documented_site requires complex_or_site. Never select selected_building '
    'and try to weaken it with a site-only caveat in the text or reason. '
    'For a cross-source historical-name link, identify the actually retained '
    'identity source version, received chunk and linking passage in reason. '
    'A failed retain is not a completed attachment; use an already supplied '
    'verified identity receipt or resolve the attachment before a supported save. '
    'Keep text to the assertions supported by its attached source assessments. '
    'Identity context belongs in reason: do not add a second page\'s address '
    'sentence to a newspaper-only claim, or mark the identity page supported '
    'for event details it does not report. Preserve the broader subject when '
    'a report describes an investigation involving several targets. '
    'Metadata-only identity evidence is not a substantive history fact and must '
    'not inflate the useful fact count. '
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
    'Perform available worthwhile actions yourself rather than returning a '
    'query/URL/cursor for the host to execute in a new turn. Finish only on '
    'semantic sufficiency, a concrete resource blocker or an owner decision. '
    'completion.next_action is normally null; use ask_owner for actual owner '
    'clarification, or record a genuinely blocked action and its reason for '
    'rare explicit recovery. Do not repeat an unchanged action without new input. '
    'When save_facts is among the frozen tools, deliver complete assessments through '
    'that tool as soon as useful knowledge is ready, then perform your next '
    'worthwhile search/read/extraction inside this same turn. Do not close a turn '
    'just to request a host continuation. After saving, retain all sources and '
    'durable fact IDs; final facts need not repeat already saved equivalents. '
    'Before completion, check the selected-subject passages already received for '
    'useful unsaved claims. When a native read supplied useful new material, retain '
    'its actual ref and deliver its assessments here; mentioning that unsaved '
    'material only in completion.reason is not delivery. If attachment or saving '
    'cannot finish, report the concrete returned blocker and preserve the exact '
    'read/ref for recovery. Do not repeat saved equivalents or turn unrelated '
    'clauses and bare metadata into additional facts. '
    'Return next_action only for an actual blocker or owner decision after your '
    'autonomous exploration, never as a substitute for an available next step. '
    'If you propose a concrete worthwhile query or read and its native tool is '
    'available now, execute and assess it before completion. An unresolved '
    'question alone is not an out-of-turn blocker. If no distinct worthwhile '
    'step remains, explain why further research is unlikely to add evidence; '
    'you may finish with unresolved gaps without inventing a host search task. '
    'If INPUT.native_continuation is present, continue the specified owned '
    'parent turn in this same thread, preserving its read versions and useful '
    'facts; it is not permission to reidentify the selected house. '
    'If it carries unresolved_gaps, your previous turn ended with those gaps while '
    'resources remained and without a next step of its own: pursue them now through '
    'distinct new directions (another source family, historical street names and '
    'numbers, material you found but did not read, scoped Sol help) and save supported '
    'knowledge, or explain concretely for each gap why further research cannot close it. '
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


def delivered_candidates(receipt):
    """Current-turn save packets retain their exact call proof at final admission."""
    final = source_candidates(receipt['raw_result'])
    delivered = []
    for call_id, call in receipt.get('native_tool_calls', {}).items():
        owner = call.get('input') or {}
        if (call.get('phase') != 'completed' or owner.get('name') != 'save_facts'
                or owner.get('thread_id') != receipt.get('thread_id')
                or owner.get('turn_id') != receipt.get('turn_id')):
            continue
        for candidate in source_candidates(owner['arguments'])['facts']:
            # Exact copies may be omitted from final prose; this is mechanical
            # replay deduplication, never semantic equivalence inferred by code.
            final['facts'] = [x for x in final['facts'] if x != candidate]
            candidate['native_checkpoint_call_id'] = call_id
            delivered.append(candidate)
    return {'facts': [*delivered, *final['facts']]}


def unresolved_gaps(completion):
    """The model's own remaining gaps after an unresolved_gap stop, as sent back to it."""
    if (completion or {}).get('research_sufficient') or (completion or {}).get('stop_basis') != 'unresolved_gap':
        return []
    return [gap.strip()[:500] for gap in (completion.get('remaining_gaps') or [])[:8]
            if isinstance(gap, str) and gap.strip()]


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
    # The model's own reported gaps continue its turn when it named no next step.
    gap_continuation = (not completion.get('next_action') and pointer.get('unresolved_gaps')
        and completion.get('stop_basis') == 'unresolved_gap'
        and pointer['unresolved_gaps'] == unresolved_gaps(completion))
    if (not row['role'].startswith('facts_agent_') or parent.get('native_contract') != CONTRACT
            or parent.get('phase') != 'completed' or parent.get('provider_send_state') != 'response_closed'
            or parent.get('native_terminal_status') != 'completed' or completion.get('research_sufficient')
            or not (completion.get('next_action') or gap_continuation)
            or (completion.get('next_action') or {}).get('kind') == 'ask_owner'
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
            or receipt.get('provider_id') != 'codex_native' or receipt.get('model_id') != 'gpt-6-luna'
            or agent.get('provider_id') != 'codex_native' or agent.get('model_id') != 'gpt-6-luna'
            or any(binding.get(key) != value for key, value in {
                'story_id': story['id'], 'photo_sha256': story['photo_sha256'],
                'generation': story['_identity_generation'], 'control_revision': story['_fact_research_control_revision'],
                **({} if origin_run_id else {'job_id': story['_research_job_id']})}.items())
            or basis not in fact.get('sources', [])):
        raise ValueError('native_fact_operation_unverified')
    parent = receipt.get('native_threads', {}).get(receipt.get('thread_id')) or {}
    if parent.get('model_id') != 'gpt-6-luna' or parent.get('turn_id') != receipt.get('turn_id'):
        raise ValueError('native_fact_executed_model_unverified')
    checkpoint_call_id = candidate.get('native_checkpoint_call_id')
    checkpoint_at = None
    if checkpoint_call_id is not None:
        # The complete assessment was delivered by a verified root tool call;
        # the enclosing inference may still be running or have an unknown tail.
        # Its original final-result proof branch remains unchanged below.
        call = receipt.get('native_tool_calls', {}).get(checkpoint_call_id) or {}
        delivered = call.get('input') or {}
        packet = delivered.get('arguments') or {}
        contexts = delivered.get('execution_contexts') or []
        checkpoint_at = call.get('submitted_at')
        declaration = next((tool for tool in receipt.get('provided_tools', []) if tool.get('name') == 'save_facts'), None)
        try:
            frozen_context = json.loads(receipt['frozen_prompt'].split('\nINPUT=', 1)[1])
        except (KeyError, IndexError, ValueError, TypeError):
            raise ValueError('native_fact_checkpoint_input_unverified') from None
        if (not declaration or declaration.get('parameters') != LEGACY_RESULT_SCHEMA
                or type(checkpoint_at) not in (int, float)
                or call.get('phase') not in {'submitted', 'completed'} or delivered.get('name') != 'save_facts'
                or delivered.get('thread_id') != receipt.get('thread_id')
                or delivered.get('turn_id') != receipt.get('turn_id')
                or frozen_context.get('run_id') != run_id
                or not Draft202012Validator(LEGACY_RESULT_SCHEMA).is_valid(packet)
                or fact not in packet['facts'] or not contexts
                or any(x.get('model') != 'gpt-6-luna' or x.get('turn_id') != receipt.get('turn_id') for x in contexts)):
            raise ValueError('native_fact_checkpoint_operation_unverified')
    elif (receipt.get('phase') != 'completed' or receipt.get('provider_send_state') != 'response_closed'
            or receipt.get('native_terminal_status') != 'completed'
            or fact not in (receipt.get('raw_result') or {}).get('facts', [])):
        raise ValueError('native_fact_operation_unverified')
    elif not parent.get('execution_contexts'):
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
        if call.get('phase') == 'completed' and (call.get('input') or {}).get('name') in {'webfetch', 'retain_native_source'}
        and (checkpoint_at is None or type(call.get('completed_at')) in (int, float)
            and call['completed_at'] <= checkpoint_at)
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
            or any(received[key] != chunks[key] for key in required & chunks.keys() & received.keys())
            or (compact and received != chunks)
            or (paged and any(chunks.get(key) != text for key, text in received.items()))
            or (not compact and not paged and read.get('normalized_text') != frozen['normalized_text'])
            or frozen['content_sha256'] != basis['content_sha256']
            or hashlib.sha256(frozen['normalized_text'].encode()).hexdigest() != basis['content_sha256']):
        raise ValueError('native_fact_source_not_owned_read')
    if not required.issubset(chunks.keys() & received.keys()):
        # Ownership and source integrity above must pass before exposing even
        # public identifiers. Never repair the packet or suggest another source.
        raise NativeFactSourceReferenceInvalid(row, version=version,
            content_sha256=basis['content_sha256'],
            received={key for key, text in received.items() if chunks.get(key) == text})
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
    if checkpoint_call_id is not None:
        assessment['original_native_checkpoint_call_id'] = checkpoint_call_id
        assessment['assessment_delivery'] = {'contract': 'native-root-tool-assessment-delivery-v1',
            'state': 'received', 'call_id': checkpoint_call_id, 'thread_id': receipt['thread_id'],
            'turn_id': receipt['turn_id'], 'received_at': checkpoint_at,
            'arguments_sha256': hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
    return {**dict(frozen), 'title': read.get('title') or basis['url']}, {
        'result': copy.deepcopy(row), 'receipt': assessment}
