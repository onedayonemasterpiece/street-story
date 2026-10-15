"""Durable mechanical review addressing; every semantic verdict is model supplied."""
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from .research_budget import PAGE_UNITS, response_units
from .service import ConflictError, canonical

POLICY_VERSION = 'own-evidence-repair-v6'
LOG = logging.getLogger(__name__)


def literal_basis_quote(quote, passages):
    """Resolve presentation escapes only when the unchanged own passage proves them."""
    if not isinstance(quote, str):
        return quote
    def contained(value):
        normalized = ' '.join(value.split())
        return bool(normalized) and any(normalized in ' '.join(text.split()) for text in passages)
    if contained(quote):
        return quote
    # Nested JSON setup context can elicit a once- or twice-escaped whitespace
    # character. Never decode words, dates, Unicode, or another fact's evidence.
    decoded = re.sub(r'(?<!\\)\\{1,2}([nrt])',
                     lambda match: {'n': '\n', 'r': '\r', 't': '\t'}[match[1]], quote)
    return decoded if decoded != quote and contained(decoded) else quote

ATOMIC_PROPOSITION_CHECKS = (
    'Atomic means one independently selectable proposition, not one sentence. '
    'Enumerate what the reader could independently include or omit BEFORE choosing a verdict; '
    'copying an entire compound sentence as one claims entry is not decomposition. '
    'A construction date, a building height, a tenant and a facade feature are separate '
    'propositions even when a source puts them in one sentence. Distinct tenants, facade '
    'features and successive events also remain separately selectable. '
    'Diameter, height, floor count and wall thickness are also independently selectable '
    'properties of the same building: a size sentence containing two dimensions is not '
    'one atomic proposition merely because both measurements are accurate. An event date, '
    'approximation, affected part or essential condition qualifies that SAME proposition '
    'and must stay with it; do not split a qualifier into an unsupported standalone fact. '
    'A known subject name identifies the subject, but additional descriptive assertions '
    'must not be hidden in that name.'
)

ATOMIC_CLAIM_CHECKS = (ATOMIC_PROPOSITION_CHECKS + ' '
    'For a compound ORIGINAL candidate enumerate all '
    'its propositions, set atomic=false and use repair_needed before accepting any '
    'narrowed replacement. The model decides this from meaning, never punctuation.'
)

SUFFICIENCY_BASIS_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'candidate_indices': {'type': 'array', 'maxItems': 32, 'uniqueItems': True,
                          'items': {'type': 'integer', 'minimum': 0, 'maximum': 31}},
    'known_fact_ids': {'type': 'array', 'maxItems': 32, 'uniqueItems': True,
                       'items': {'type': 'string', 'minLength': 1}}},
    'required': ['candidate_indices', 'known_fact_ids']}

SUFFICIENCY_CHECKS = (
    'Decide research_sufficient for coverage_goal from meaning, not a fact count. '
    'When true, supply research_sufficient_basis: candidate_indices are the exact zero-based '
    'positions in your returned facts array and known_fact_ids are exact supplied eligible '
    'host IDs. Choose the compact substantive basis that satisfies the goal when each '
    'new claim passes your own assessment in this pass; enrichment is not part of that basis. '
    'One accepted claim cannot stand in for the other claims you relied on. '
    'Location metadata alone is not sufficient historical or architectural material. '
    'Never use an unreviewed known claim or invent an ID. When false, use empty basis arrays.'
)

EXTRACTION_CHECKS = (ATOMIC_CLAIM_CHECKS + ' ' + SUFFICIENCY_CHECKS + ' '
    'Write each fact.text in Russian for publication, preserving supported scope and qualifiers. '
    'For existing_fact_id copy only an exact host fact_id from the supplied known inventory; '
    'otherwise use the empty string. Never invent IDs or use ordinal placeholders. '
    'Choose independently selectable claims relevant to coverage_goal; location metadata alone '
    'does not supply missing substantive history. '
    'Before saving, enumerate independently selectable assertions from the source '
    '(each depicted person, role or event separately). Form each candidate only after '
    'checking all its dates, numbers, parts, stages and qualifiers against its own '
    'chosen passages. Include the actual antecedent of a date and the actual outcome '
    'of a request in those passages; neither may be inferred from another candidate. '
    'Preserve modality and time direction exactly: planned, expected, estimated or '
    'future values must not be rewritten as completed, paid or actual outcomes. '
    'A dated article describes its own time: mutable states (registration, ownership, '
    'condition or use) need an explicit as-of date unless current evidence verifies them. '
    'An undated source saying currently or these days does not establish a current as-of date. '
    'Retrieval time is not publication or event time. Preserve unresolved temporal ambiguity '
    'and conflicting source accounts rather than inventing a date or selecting one silently. '
    'Keep the physical building, institution, individual part and larger complex distinct; '
    'an institution\'s founding date is not automatically the building\'s construction date. '
    'Resolve the explicitly named subject in each source sentence against confirmed_identity. '
    'A page about the confirmed building may describe another building; never transfer that '
    'other subject\'s architects, dates or roles to the confirmed subject. Name the actual '
    'supported subject explicitly when a generic word such as building would be ambiguous. '
    'Do not merge a news event such as work starting with an adjacent planned budget '
    'into one fact merely because both appear in the same source paragraph. '
    'Preserve uncertainty and subset versus whole. If the source context is incomplete, '
    'continue reading or leave that claim unresolved; do not save a confident guess '
    'for a later correction. Save the supported atomic assertions directly.'
)

REVIEW_CHECKS = (
    'These are unverified candidates, not established facts. First enumerate independent claims '
    '(each person/role/event separately), then compare EVERY date, number, part, stage and qualifier '
    'to ONLY this candidate\'s attached evidence. Missing date antecedent or outcome: read '
    'get_review_context and attach it using repair_research_fact. Probable subset is not certain whole. '
    'Multiple independent claims: repair/split before supported. Literal basis_quotes cannot borrow '
    'another fact\'s spans. supported requires one claim and complete own support. '
    'A mutable state reported by an old article is not a current fact: repair it to '
    'retain the source date, or attach evidence verifying its present status. '
    'An undated currently or these days statement does not establish present status; '
    'the review or retrieval date is not the source\'s publication or event date. '
    'Preserve temporal ambiguity and source-specific conflicting accounts. Check the exact '
    'subject: building versus institution, individual part versus larger complex. Do not '
    'transfer an institution\'s founding date to the physical building. '
    'Keep correct affirmative candidates; missing context is insufficient, not historically false.'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_review_packets(
 packet_ref TEXT PRIMARY KEY, story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
 run_id TEXT NOT NULL, binding TEXT NOT NULL, story_revision INTEGER NOT NULL,
 identity_generation INTEGER NOT NULL, payload_json TEXT NOT NULL,
 decisions_json TEXT NOT NULL DEFAULT '{}', result_json TEXT, request_json TEXT
);
CREATE TABLE IF NOT EXISTS live_review_attempts(
 packet_ref TEXT PRIMARY KEY REFERENCES live_review_packets(packet_ref) ON DELETE CASCADE,
 policy_version TEXT NOT NULL, supersedes_ref TEXT,
 affected_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS live_review_row_rejections(
 packet_ref TEXT NOT NULL REFERENCES live_review_packets(packet_ref) ON DELETE CASCADE,
 request_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(packet_ref,request_sha256)
);
CREATE TABLE IF NOT EXISTS live_review_contexts(
 context_ref TEXT PRIMARY KEY, packet_ref TEXT NOT NULL REFERENCES live_review_packets(packet_ref) ON DELETE CASCADE,
 source_version_id TEXT NOT NULL, span_start INTEGER NOT NULL, span_end INTEGER NOT NULL,
 span_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_fact_repairs(
 story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE, parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
 command_id TEXT NOT NULL, packet_ref TEXT NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(story_id,parent_id,child_id,command_id)
);
CREATE TABLE IF NOT EXISTS live_review_assessments(
 packet_ref TEXT NOT NULL REFERENCES live_review_packets(packet_ref) ON DELETE CASCADE,
 cursor INTEGER NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(packet_ref,cursor)
);
CREATE TABLE IF NOT EXISTS live_pending_review_links(
 story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE, run_id TEXT NOT NULL,
 left_id TEXT NOT NULL, right_id TEXT NOT NULL, left_revision TEXT NOT NULL, right_revision TEXT NOT NULL,
 packet_ref TEXT NOT NULL, decision_json TEXT NOT NULL,
 PRIMARY KEY(story_id,run_id,left_id,right_id,packet_ref)
);
CREATE TABLE IF NOT EXISTS live_pending_review_resolutions(
 story_id TEXT NOT NULL REFERENCES stories(id) ON DELETE CASCADE, run_id TEXT NOT NULL,
 left_id TEXT NOT NULL, right_id TEXT NOT NULL, packet_ref TEXT NOT NULL, resolved_by TEXT NOT NULL,
 PRIMARY KEY(story_id,run_id,left_id,right_id,packet_ref)
);
"""


def binding(session):
    return hashlib.sha256(canonical(getattr(session, 'actor', None)).encode()).hexdigest()


def bundle(db, story_id):
    return {str(r['assertion_id']): str(r['revision_digest']) for r in db.execute(
        'SELECT a.assertion_id,COALESCE(a.accepted_revision_digest,a.revision_digest) AS revision_digest FROM fact_assertions a JOIN facts f '
        'ON f.story_id=a.story_id AND f.fact_id=a.assertion_id WHERE a.story_id=? '
        'AND f.evidence_supported=1 AND NOT EXISTS(SELECT 1 FROM live_fact_repairs r '
        'WHERE r.story_id=a.story_id AND r.parent_id=a.assertion_id AND r.child_id<>r.parent_id) '
        'ORDER BY a.assertion_id', (story_id,))}


def pending_candidates(db, story_id, run_id):
    ids = []
    for row in db.execute('SELECT payload_json FROM research_chunk_batches WHERE run_id=? ORDER BY created_at,batch_index', (run_id,)):
        saved = json.loads(row['payload_json'] or '{}')
        if saved.get('extractor_candidates'):
            ids.extend(str(f['fact_id']) for f in saved.get('facts', []) if f.get('fact_id'))
    ids.extend(row[0] for row in db.execute('SELECT r.child_id FROM live_fact_repairs r '
        'JOIN live_review_packets p ON p.packet_ref=r.packet_ref WHERE r.story_id=? AND p.run_id=? '
        'ORDER BY r.rowid', (story_id, run_id)))
    pending = [fid for fid in dict.fromkeys(ids) if db.execute(
        "SELECT 1 FROM fact_assertions WHERE story_id=? AND assertion_id=? AND eligibility='unreviewed'",
        (story_id, fid)).fetchone()]
    current = bundle(db, story_id)
    for row in db.execute('SELECT l.* FROM live_pending_review_links l WHERE l.story_id=? AND l.run_id=? '
        'AND NOT EXISTS(SELECT 1 FROM live_pending_review_resolutions r WHERE r.story_id=l.story_id '
        'AND r.run_id=l.run_id AND r.left_id=l.left_id AND r.right_id=l.right_id AND r.packet_ref=l.packet_ref)',
        (story_id, run_id)):
        if current.get(row['left_id']) == row['left_revision'] and current.get(row['right_id']) == row['right_revision']:
            pending.extend([row['left_id'], row['right_id']])
    return list(dict.fromkeys(pending))


def candidate_review_fence(db, story):
    """Owner inputs, distinct from unrelated background candidate revisions."""
    research = json.loads(story['research_json'] or '{}')
    return {'photo': story['photo_sha256'], 'identity_generation': research.get('identity_generation'),
            'controls': research.get('research_controls'), 'input_revision': research.get('input_revision'),
            'transcript': research.get('transcript'),
            'latitude': story['latitude'], 'longitude': story['longitude'],
            'voices': [list(r) for r in db.execute('SELECT session_id,recording_finished,transcript '
                'FROM voice_sessions WHERE story_id=? ORDER BY session_id', (story['id'],))],
            'subject': (research.get('visual_identity') or {}).get('candidate_id')}


def eligible_bundle(db, story_id):
    return {r[0]: r[1] for r in db.execute('SELECT assertion_id,COALESCE(accepted_revision_digest,revision_digest) AS revision_digest FROM fact_assertions '
        "WHERE story_id=? AND eligibility='eligible' ORDER BY assertion_id", (story_id,))}


def canonical_review_fence(db, story, assertion_ids, *, include_projection=False):
    """Versions of the shared claims actually used by this packet."""
    from .poi_memory import memory_keys
    research = json.loads(story['research_json'] or '{}')
    keys = memory_keys(db, research.get('visual_identity') or {})
    versions = {}
    if include_projection:
        for assertion_id in sorted(set(assertion_ids)):
            row = db.execute('SELECT eligibility,review_status,COALESCE(accepted_revision_digest,revision_digest) AS revision_digest FROM fact_assertions '
                'WHERE story_id=? AND assertion_id=?', (story['id'], assertion_id)).fetchone()
            versions[canonical(['story_projection', assertion_id])] = hashlib.sha256(canonical(dict(row)).encode()).hexdigest() if row else None
    for key in keys:
        for assertion_id in sorted(set(assertion_ids)):
            row = db.execute('SELECT text,sources_json,eligibility,review_status,review_story_id,reviewed_at,review_proof_json '
                'FROM poi_research_assertions WHERE poi_key=? AND assertion_id=?', (key, assertion_id)).fetchone()
            record = dict(row) if row else None
            if record:
                from .poi_memory import _canonical_admission_valid
                proof = json.loads(record.pop('review_proof_json') or '{}')
                if _canonical_admission_valid(db, proof, record['text'], key):
                    record.pop('sources_json')
                    record['accepted_basis'] = proof
            versions[canonical([key, assertion_id])] = hashlib.sha256(canonical(record).encode()).hexdigest() if record else None
    return versions


def load(adapter, session, db, ref):
    row = db.execute('SELECT * FROM live_review_packets WHERE packet_ref=? AND story_id=?',
                     (ref, session.resource_id)).fetchone()
    if row is None or row['binding'] != binding(session):
        raise ConflictError('live_review_packet_unknown', 'Unknown or foreign packet. Call get_review_packet with the SAME run_id ONLY; OMIT packet_ref to get a new packet. Never use a batch_id as packet_ref.')
    if row['result_json']:
        return row, json.loads(row['payload_json'])
    attempt = db.execute('SELECT state,policy_version FROM live_review_attempts WHERE packet_ref=?', (ref,)).fetchone()
    if attempt is None or attempt['state'] == 'superseded' or attempt['policy_version'] != POLICY_VERSION:
        raise ConflictError('live_review_packet_stale', 'This attempt was superseded; use a fresh packet.')
    story, _ = adapter._research_run_guard(db, session, row['run_id'])
    research = json.loads(story['research_json'] or '{}')
    payload = json.loads(row['payload_json'])
    current = bundle(db, session.resource_id)
    if payload.get('candidate_scope') is not None:
        current = {fid: current[fid] for fid in payload['candidate_scope'] if fid in current}
        for claim in payload.get('nearby_existing_claims', []):
            existing = db.execute("SELECT COALESCE(accepted_revision_digest,revision_digest) AS revision_digest,eligibility FROM fact_assertions WHERE story_id=? AND assertion_id=?", (session.resource_id, claim['fact_id'])).fetchone()
            if not existing or existing['eligibility'] != 'eligible' or existing['revision_digest'] != claim['revision_digest']:
                raise ConflictError('live_review_packet_stale', 'Nearby existing claim changed; request a fresh packet.')
    parallel = payload.get('parallel_candidate_review') is True
    revision_stale = int(story['revision']) != row['story_revision']
    if parallel:
        revision_stale = candidate_review_fence(db, story) != payload['owner_fence']
        if 'canonical_dependencies' in payload:
            dependencies = [*payload['candidate_scope'],
                            *[claim['fact_id'] for claim in payload.get('nearby_existing_claims', [])]]
            revision_stale |= canonical_review_fence(db, story, dependencies,
                include_projection=payload.get('canonical_projection_dependencies') is True) != payload['canonical_dependencies']
        else:
            # Pending packets created before scoped shared dependencies retain
            # their original stricter contract. Closed receipts above are immutable.
            revision_stale |= eligible_bundle(db, session.resource_id) != payload['eligible_bundle']
    if revision_stale or int(research.get('identity_generation') or 0) != row['identity_generation'] or current != payload['bundle']:
        raise ConflictError('live_review_packet_stale', 'Revisions changed; request a new packet. No decision applied.')
    actual = {r['evidence_id']: dict(r) for r in db.execute(
        "SELECT e.evidence_id,e.span_sha256,o.assertion_id FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id WHERE o.story_id=? AND o.status='accepted'", (session.resource_id,))}
    for item in payload['items']:
        for ev in item['evidence']:
            if actual.get(ev['id']) != {'evidence_id': ev['id'], 'span_sha256': ev['sha'], 'assertion_id': item['id']}:
                raise ConflictError('live_review_packet_stale', 'Evidence changed; request a new packet.')
    return row, payload


REVIEW_SLICE_CONTRACT = 'byte-bounded-own-passages-v2'


def bounded_review_slices(page, slices, *, quote_refs=False):
    """Fit each own passage beside the complete claim and selected identity.

    Split text at character boundaries, preserving every offset and final-tail
    marker. The frozen packet and existing public response limit stay intact.
    """
    result = []

    def append(item):
        if quote_refs:
            from .headless_review_quotes import with_quote_ref
            item = with_quote_ref(page, item)
        cursor = len(result) + 1
        probe = {**page, 'items': [item], 'next_cursor': cursor, 'has_more': True,
            'next_tool': 'get_review_packet',
            'next_args': {'packet_ref': page['packet_ref'], 'cursor': cursor}}
        if 'nearby_existing_claims' in probe:
            probe['nearby_existing_claims'] = []  # Existing pagination trims context separately.
        if response_units('get_review_packet', probe) <= PAGE_UNITS:
            result.append(item)
            return
        passage = item['passage']
        if len(passage) <= 1:
            raise ConflictError('live_review_item_oversize',
                'Assertion text exceeds a bounded review operation; all data preserved.')
        midpoint = len(passage) // 2
        append({**item, 'passage': passage[:midpoint], 'passage_complete': False})
        append({**item, 'passage': passage[midpoint:], 'offset': item['offset'] + midpoint})

    for item in slices:
        append(item)
    return result


def topic_run_binding(context):
    """Remember only the exact run and owner fences exposed by read_topic."""
    run = context.get('research_run') or {}
    if (not run.get('run_id') or not context.get('photo_sha256')
            or context.get('identity_generation') is None):
        return None
    controls = context.get('research_controls') or {}
    return {'story_id': context.get('story_id'), 'run_id': run['run_id'],
            'photo_sha256': context.get('photo_sha256'),
            'identity_generation': context.get('identity_generation'),
            'run_generation': run.get('identity_generation'),
            'subject': (context.get('visual_identity') or {}).get('candidate_id'),
            'facts_revision': int((controls.get('facts') or {}).get('revision') or 0),
            'identity_revision': int((controls.get('identity') or {}).get('revision') or 0)}


def bound_topic_run(adapter, session, db):
    bound = session.state.get('review_topic_binding')
    if not bound:
        raise ConflictError('live_review_run_required',
                            'Call read_topic in this review session before omitting run_id.')
    story, research = adapter._fact_research_control_guard(db, session)
    current_run = db.execute('SELECT run_id,identity_generation FROM research_runs WHERE story_id=? '
                             'ORDER BY created_at DESC LIMIT 1', (session.resource_id,)).fetchone()
    current = topic_run_binding({'story_id': story['id'], 'photo_sha256': story['photo_sha256'],
        'identity_generation': int(research.get('identity_generation') or 0),
        'visual_identity': research.get('visual_identity'), 'research_controls': research.get('research_controls'),
        'research_run': dict(current_run) if current_run else None})
    scope = session.state.get('research_control_scope') or {}
    if (bound != current or bound['identity_generation'] != bound['run_generation']
            or scope.get('expected_photo_sha256', bound['photo_sha256']) != bound['photo_sha256']
            or scope.get('expected_identity_generation', bound['identity_generation']) != bound['identity_generation']):
        raise ConflictError('live_review_run_stale',
                            'The run or owner scope shown by read_topic changed. Read the topic again; do not guess a run.')
    LOG.info('street_story_review_topic_run_bound story_id=%s session_id=%s run_id=%s generation=%s',
             session.resource_id, getattr(session, 'id', ''), bound['run_id'], bound['identity_generation'])
    return bound['run_id']


def read(adapter, session, args):
    with adapter.service.store.tx() as db:
        ref = str(args.get('packet_ref') or '')
        if not ref:
            run_id = str(args.get('run_id') or '')
            supersedes = str(args.get('supersedes_packet_ref') or '')
            if 'run_id' not in args and 'packet_ref' not in args and not supersedes:
                # Reference convenience only: no new run, semantic decision or
                # fallback from an explicit bad reference. Resolve in this tx,
                # before any packet/reopen writes; all existing guards follow.
                run_id = bound_topic_run(adapter, session, db)
            affected = set()
            if supersedes:
                prior, old = load(adapter, session, db, supersedes)
                if prior['run_id'] != run_id:
                    raise ConflictError('live_review_packet_unknown', 'Superseded packet must belong to the same run.')
                story = adapter.service._story_row(db, session.resource_id)
                research = json.loads(story['research_json'] or '{}')
                if int(research.get('identity_generation') or 0) != prior['identity_generation']:
                    raise ConflictError('live_review_packet_stale', 'Object identity changed.')
                numbers = args.get('recheck_facts', list(range(len(old['items']))))
                if not isinstance(numbers, list) or not numbers or any(type(n) is not int or not 0 <= n < len(old['items']) for n in numbers):
                    raise ConflictError('live_review_decisions_invalid', 'Use fact numbers of the superseded packet.')
                affected = {old['items'][n]['id'] for n in numbers}
                db.execute("UPDATE research_runs SET state='verifying',completed_at=NULL WHERE run_id=? AND story_id=? AND state='completed'", (run_id, session.resource_id))
            requested_scope = args.get('fact_ids')
            if requested_scope is not None:
                scoped_story, scoped_research = adapter._fact_research_control_guard(db, session)
                db.execute("UPDATE research_runs SET state='verifying',completed_at=NULL WHERE run_id=? "
                           "AND story_id=? AND identity_generation=? AND state='completed'",
                           (run_id, session.resource_id, int(scoped_research.get('identity_generation') or 0)))
            story, run = adapter._research_run_guard(db, session, run_id)
            if requested_scope is not None:
                if (not isinstance(requested_scope, list) or not 1 <= len(requested_scope) <= 12
                        or any(not isinstance(fid, str) for fid in requested_scope)
                        or len(set(requested_scope)) != len(requested_scope)):
                    raise ConflictError('live_review_decisions_invalid', 'Supply 1–12 exact fact_ids from this story; prefer a small independent review.')
                available = bundle(db, session.resource_id)
                if any(fid not in available or db.execute('SELECT review_status FROM fact_assertions '
                        'WHERE story_id=? AND assertion_id=?', (session.resource_id, fid)).fetchone()[0] == 'quarantined'
                       for fid in requested_scope):
                    raise ConflictError('live_review_decisions_invalid', 'Review only current own assertions with attached evidence; quarantine requires arbitration.')
                # An explicit reconsideration must produce new semantic evidence,
                # rather than reuse the old decision the owner is questioning.
                affected.update(requested_scope)
            candidate_ids, existing_hints = [], []
            for batch in db.execute('SELECT payload_json FROM research_chunk_batches WHERE run_id=? ORDER BY created_at,batch_index', (run_id,)):
                saved = json.loads(batch['payload_json'] or '{}')
                if saved.get('extractor_candidates'):
                    existing_hints.extend(str(fid) for fid in saved.get('extractor_existing_fact_ids', []) if fid)
                    candidate_ids.extend(str(f['fact_id']) for f in saved.get('facts', []) if f.get('fact_id'))
            if candidate_ids or args.get('_candidate_ids') is not None:
                candidate_ids.extend(row[0] for row in db.execute('SELECT r.child_id FROM live_fact_repairs r '
                    'JOIN live_review_packets p ON p.packet_ref=r.packet_ref WHERE r.story_id=? AND p.run_id=? '
                    'ORDER BY r.rowid', (session.resource_id, run_id)))
            candidate_mode = requested_scope is not None or bool(candidate_ids) and not supersedes
            if candidate_mode:
                if requested_scope is not None:
                    candidate_ids = requested_scope
                else:
                    requested_candidates = args.get('_candidate_ids')
                    if requested_candidates is not None and (not isinstance(requested_candidates, list)
                            or not 1 <= len(requested_candidates) <= 12
                            or any(not isinstance(fid, str) for fid in requested_candidates)
                            or len(set(requested_candidates)) != len(requested_candidates)):
                        raise ConflictError('live_review_decisions_invalid', 'Supply 1–12 distinct candidate IDs.')
                    candidate_ids = [fid for fid in dict.fromkeys(candidate_ids)
                                     if (requested_candidates is None or fid in requested_candidates) and db.execute(
                        "SELECT 1 FROM fact_assertions WHERE story_id=? AND assertion_id=? AND eligibility='unreviewed'",
                        (session.resource_id, fid)).fetchone()][:12 if requested_candidates is not None else 3]
                if not candidate_ids:
                    return {'run_id': run_id, 'review_available': False, 'pending_candidates': 0,
                            'instruction': 'All saved extractor candidates already have semantic decisions. Existing publication choices remain unchanged.'}
            pending = db.execute("SELECT COUNT(*) FROM research_chunk_runs WHERE run_id=? AND status NOT IN ('extracted','no_claims')", (run_id,)).fetchone()[0]
            unfetched = db.execute("SELECT COUNT(*) FROM research_run_sources WHERE run_id=? AND source_version_id IS NULL", (run_id,)).fetchone()[0]
            if (pending or unfetched) and not candidate_mode and args.get('allow_partial_review') is not True:
                for chunk_id, receipt in session.state.get('research_chunk_receipts', {}).items():
                    active = db.execute("SELECT r.status,c.core_start,c.core_end,v.normalized_text FROM research_chunk_runs r JOIN source_chunks c ON c.chunk_id=r.chunk_id JOIN source_versions v ON v.source_version_id=c.source_version_id WHERE r.run_id=? AND r.chunk_id=?", (run_id, chunk_id)).fetchone()
                    if active and active['status'] not in {'extracted', 'no_claims'}:
                        core = active['normalized_text'][active['core_start']:active['core_end']]
                        if len(session.state.get('research_passages_seen', {}).get(chunk_id, set())) == len(adapter._core_passages(chunk_id, core)):
                            return {'run_id': run_id, 'review_available': False, 'pending_chunks': pending,
                                    'next_tool': 'save_research_facts', 'next_args': receipt,
                                    'instruction': 'This core is read but not checkpointed. Save its findings or explicit facts=[] using next_args before review; do not reread it.'}
                return {'run_id': run_id, 'review_available': False, 'pending_chunks': pending,
                        'next_tool': 'get_research_chunk', 'next_args': {'run_id': run_id},
                        'instruction': 'Finish remaining source cores before full review. Call get_research_chunk with run_id ONLY: omit prior chunk_id and passage_cursor so the server selects the next unfinished core.'}
            exact = bundle(db, session.resource_id)
            nearby = []
            if candidate_mode:
                exact = {fid: exact[fid] for fid in candidate_ids if fid in exact}
                hints = list(dict.fromkeys(existing_hints))
                hint_slots = ','.join('?' for _ in hints) or "''"
                scope_slots = ','.join('?' for _ in candidate_ids)
                nearby = [dict(row) for row in db.execute(
                    "SELECT f.fact_id,f.text,COALESCE(a.accepted_revision_digest,a.revision_digest) AS revision_digest FROM facts f JOIN fact_assertions a "
                    "ON a.story_id=f.story_id AND a.assertion_id=f.fact_id "
                    "WHERE f.story_id=? AND f.evidence_supported=1 AND a.eligibility='eligible' "
                    f"AND a.assertion_id NOT IN ({scope_slots}) "
                    f"ORDER BY CASE WHEN f.fact_id IN ({hint_slots}) THEN 0 ELSE 1 END, "
                    "a.owner_selected DESC,f.rowid DESC", (session.resource_id, *candidate_ids, *hints))]
            items = []
            for fact_id in exact:
                text = db.execute('SELECT text FROM facts WHERE story_id=? AND fact_id=?', (session.resource_id, fact_id)).fetchone()[0]
                evidence = [dict(r) for r in db.execute(
                    "SELECT e.evidence_id AS id,e.span_sha256 AS sha,e.span_text AS text,e.source_url AS url,e.source_version_id,e.span_start,e.span_end,e.chunk_id FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id WHERE o.story_id=? AND o.assertion_id=? AND o.status='accepted' ORDER BY e.evidence_id", (session.resource_id, fact_id))]
                # Repeated observations of one literal span are not independent sources.
                # Keep all ledger rows; expose one stable representative per exact span.
                spans = {}
                for ev in evidence:
                    key = (ev['source_version_id'], ev['url'], ev['span_start'], ev['span_end'], ev['sha'])
                    spans.setdefault(key, ev)
                evidence = list(spans.values())
                if not evidence:
                    raise ConflictError('live_fact_review_evidence_required', 'Accepted evidence is required for every assertion.')
                items.append({'id': fact_id, 'text': text, 'evidence': evidence})
            reused = {}
            indexes = {item['id']: f for f, item in enumerate(items)}
            for prior in db.execute('SELECT p.payload_json,p.decisions_json FROM live_review_packets p JOIN live_review_attempts a ON a.packet_ref=p.packet_ref WHERE p.story_id=? AND p.binding=? AND p.identity_generation=? AND p.result_json IS NOT NULL AND a.state=? AND a.policy_version=? ORDER BY p.rowid', (session.resource_id, binding(session), int(run['identity_generation']), 'finished', POLICY_VERSION)):
                old = json.loads(prior['payload_json'])
                if old['bundle'] != exact:
                    continue  # New evidence/contradictions require new semantic decisions.
                for number, decision in json.loads(prior['decisions_json']).items():
                    old_item = old['items'][int(number)]
                    fact_id = old_item['id']
                    if fact_id in affected or fact_id not in indexes or old['bundle'].get(fact_id) != exact[fact_id]:
                        continue
                    f = indexes[fact_id]
                    current_evs = {ev['id']: (e, ev['sha']) for e, ev in enumerate(items[f]['evidence'])}
                    chosen = [old_item['evidence'][e] for e in decision['evidence']]
                    if all(ev['id'] in current_evs and current_evs[ev['id']][1] == ev['sha'] for ev in chosen):
                        cached = {**decision, 'fact': f, 'evidence': [current_evs[ev['id']][0] for ev in chosen]}
                        if decision.get('equivalent_to') is not None:
                            canonical_id = old['items'][decision['equivalent_to']]['id']
                            if canonical_id in indexes and old['bundle'].get(canonical_id) == exact[canonical_id]:
                                cached['equivalent_to'] = indexes[canonical_id]
                            else:
                                cached.pop('equivalent_to', None)
                        reused[str(f)] = cached
            ref = 'p' + uuid.uuid4().hex[:12]
            scope = sorted(affected & set(exact)) if supersedes else sorted(set(exact) - {items[int(n)]['id'] for n in reused})
            from .identity_model_context import fact_review_subject
            research = json.loads(story['research_json'] or '{}')
            payload = {'bundle': exact, 'items': items,
                       'confirmed_identity': fact_review_subject(research.get('visual_identity') or {},
                           photo_sha256=story['photo_sha256'], generation=int(run['identity_generation'] or 0)),
                       'review_as_of_date_utc': datetime.fromtimestamp(adapter.service.store.now(), timezone.utc).date().isoformat()}
            if candidate_mode:
                payload.update(candidate_scope=list(exact), nearby_existing_claims=nearby)
                if requested_scope is not None:
                    payload['requested_fact_scope'] = True
                if args.get('_parallel_candidate_review') is True:
                    payload.update(parallel_candidate_review=True, owner_fence=candidate_review_fence(db, story),
                                   canonical_dependencies=canonical_review_fence(db, story,
                                       [*exact, *[claim['fact_id'] for claim in nearby]], include_projection=True),
                                   canonical_projection_dependencies=True)
            for pending_packet in db.execute("SELECT p.packet_ref,p.payload_json FROM live_review_packets p "
                    "JOIN live_review_attempts a ON a.packet_ref=p.packet_ref WHERE p.story_id=? "
                    "AND p.binding=? AND a.state='pending'", (session.resource_id, binding(session))):
                previous = json.loads(pending_packet['payload_json'])
                independent = (candidate_mode and previous.get('candidate_scope') is not None
                               and not set(exact).intersection(previous['candidate_scope']))
                if not independent:
                    db.execute("UPDATE live_review_attempts SET state='superseded' WHERE packet_ref=?",
                               (pending_packet['packet_ref'],))
            db.execute('INSERT INTO live_review_packets(packet_ref,story_id,run_id,binding,story_revision,identity_generation,payload_json,decisions_json) VALUES(?,?,?,?,?,?,?,?)',
                       (ref, session.resource_id, run_id, binding(session), int(story['revision']), int(run['identity_generation']), canonical(payload), canonical(reused)))
            db.execute('INSERT INTO live_review_attempts(packet_ref,policy_version,supersedes_ref,affected_json) VALUES(?,?,?,?)', (ref, POLICY_VERSION, supersedes or None, canonical(scope)))
            if supersedes:
                db.execute("UPDATE live_review_attempts SET state='superseded' WHERE packet_ref=? AND state='pending'", (supersedes,))
        row, payload = load(adapter, session, db, ref)
        attempt = db.execute('SELECT supersedes_ref FROM live_review_attempts WHERE packet_ref=?', (ref,)).fetchone()
        superseding = bool(attempt and attempt[0]) or payload.get('requested_fact_scope') is True
    cursor = max(0, int(args.get('cursor') or 0))
    # Each domain page contains one fact and one literal evidence slice. Every
    # slice is addressed; a long passage is never silently clipped.
    slices = []
    for f, item in enumerate(payload['items']):
        for e, ev in enumerate(item['evidence']):
            for start in range(0, len(ev['text']), 900):
                slices.append({'fact': f, 'text': item['text'], 'evidence': e,
                               'passage': ev['text'][start:start + 900], 'offset': start,
                               'passage_complete': start + 900 >= len(ev['text']), 'source_url': ev['url'],
                               'saved_verdict': json.loads(row['decisions_json']).get(str(f), {}).get('verdict')})
    page = {'packet_ref': ref, 'run_id': row['run_id'], 'policy_version': POLICY_VERSION, 'review_as_of_date_utc': payload.get('review_as_of_date_utc'), 'review_checks': REVIEW_CHECKS, 'items': [], 'total_facts': len(payload['items']),
            'next_cursor': None, 'has_more': False, 'next_tool': 'finalize_fact_review'}
    if 'confirmed_identity' in payload:
        page['confirmed_identity'] = payload['confirmed_identity']
    if payload.get('candidate_scope') is not None:
        page['nearby_existing_claims'] = payload.get('nearby_existing_claims', [])
        page['review_checks'] += (' Compare against nearby_existing_claims without changing them: '
            'equivalent_to_existing marks a supported duplicate; conflicts_with_existing withholds '
            'the new candidate pending later arbitration. Require explicit atomic, support_complete '
            'and qualifiers_preserved checks plus own literal basis_quotes. This is partial candidate '
            'review; coverage_complete=false is sufficient. Never reread large documents for this operation.')
    slices = bounded_review_slices(page, slices, quote_refs=args.get('_live_quote_refs') is True)
    if page.get('nearby_existing_claims') and cursor < len(slices):
        while page['nearby_existing_claims'] and response_units('get_review_packet', {**page, 'items': [slices[cursor]]}) > PAGE_UNITS - 100:
            page['nearby_existing_claims'] = page['nearby_existing_claims'][:-1]
    while cursor < len(slices):
        candidate = {**page, 'items': [*page['items'], slices[cursor]], 'next_cursor': cursor + 1, 'has_more': cursor + 1 < len(slices)}
        if candidate['has_more']:
            candidate.update({'next_tool': 'get_review_packet', 'next_args': {'packet_ref': ref, 'cursor': cursor + 1}})
        else:
            candidate.pop('next_args', None)
            candidate['next_tool'] = 'finalize_fact_review'
            if superseding and hasattr(adapter.service.providers.gemini, 'assess_fact_candidates'):
                candidate.update(next_tool='assess_review_packet', next_args={'packet_ref': ref})
        if response_units('get_review_packet', candidate) > PAGE_UNITS:
            if not page['items']:
                raise ConflictError('live_review_item_oversize', 'Assertion text exceeds a bounded review operation; all data preserved.')
            break
        page = candidate
        cursor += 1
    return page


def fact_cursor(payload, number):
    return sum((len(ev['text']) + 899) // 900
               for item in payload['items'][:number] for ev in item['evidence'])




def validate_decision(decision, payload, ref):
    if not isinstance(decision, dict) or type(decision.get('fact')) is not int or not 0 <= decision['fact'] < len(payload['items']):
        raise ConflictError('live_review_decisions_invalid', 'Use local fact numbers from this packet.')
    if decision.get('_row_error'):
        raise ConflictError('live_fact_review_evidence_invalid',
            'The original own-quote/check row failed validation. Read get_review_packet with packet_ref=' + ref
            + '; use only its received quote_ref for this fact and selected evidence.')
    verdict = decision.get('verdict')
    if verdict not in {'supported', 'not_supported', 'contradicted', 'role_mismatch', 'insufficient', 'repair_needed'}:
        raise ConflictError('live_review_decisions_invalid', 'Return supported/not_supported/contradicted/role_mismatch for EVERY fact, including the canonical one. Equivalence alone is not support; use optional equivalent_to only for duplicates.')
    if decision.get('equivalent_to') is not None:
        other = decision['equivalent_to']
        if type(other) is not int or not 0 <= other < len(payload['items']):
            raise ConflictError('live_review_decisions_invalid', 'equivalent_to must be a canonical fact number from this packet.')
    if payload.get('candidate_scope') is not None:
        # Optional relation fields may use an empty string to mean no
        # existing claim. This carries no relation or support verdict;
        # every nonempty ID still must resolve in this frozen packet.
        if decision.get('equivalent_to_existing') == '':
            decision = dict(decision)
            decision.pop('equivalent_to_existing')
        nearby_ids = {claim['fact_id'] for claim in payload.get('nearby_existing_claims', [])}
        duplicate = decision.get('equivalent_to_existing')
        conflicts_existing = decision.get('conflicts_with_existing', [])
        if (duplicate is not None and duplicate not in nearby_ids) or not isinstance(conflicts_existing, list) or any(fid not in nearby_ids for fid in conflicts_existing):
            raise ConflictError('live_review_decisions_invalid', 'Use only nearby_existing_claims IDs for candidate relations.')
        if verdict == 'supported' and (any(decision.get(flag) is not True for flag in ('atomic', 'support_complete', 'qualifiers_preserved')) or not decision.get('claims') or not decision.get('basis_quotes')):
            raise ConflictError('live_review_decisions_invalid', 'Positive candidate review requires explicit checks, one claim and own basis quotes.')
    refs = decision.get('evidence')
    evs = payload['items'][decision['fact']]['evidence']
    if not isinstance(refs, list) or (verdict == 'supported' and not refs) or any(type(e) is not int or not 0 <= e < len(evs) for e in refs):
        raise ConflictError('live_fact_review_evidence_invalid', 'Use evidence numbers scoped to this assertion.')
    decision = dict(decision)
    claims = decision.get('claims')
    if claims is not None:
        if not isinstance(claims, list) or not 1 <= len(claims) <= 8 or any(not isinstance(c, str) or not 1 <= len(c) <= 500 for c in claims):
            raise ConflictError('live_review_decisions_invalid',
                f"Fact {decision['fact']}: claims must be a nonempty list of candidate propositions for EVERY verdict, including insufficient. It does not assert they are supported.")
        if verdict == 'supported' and len(claims) != 1:
            raise ConflictError('live_review_decisions_invalid', 'Your decomposition has multiple claims. Split the candidate before positive review.')
    quotes = decision.get('basis_quotes')
    if quotes is not None:
        if isinstance(quotes, list) and isinstance(refs, list):
            own_passages = [evs[e]['text'] for e in refs]
            normalized_quotes = [literal_basis_quote(q, own_passages) for q in quotes]
            if normalized_quotes != quotes:
                LOG.info('street_story_review_quote_presentation_normalized packet_ref=%s fact=%s',
                         ref, decision['fact'])
                quotes = normalized_quotes
                decision['basis_quotes'] = quotes
        # Presentation whitespace may vary; original snapshots/spans remain literal.
        if not isinstance(quotes, list) or len(quotes) > 8 or any(not isinstance(q, str) or not 1 <= len(q) <= 900 or not q.strip() or not any(' '.join(q.split()) in ' '.join(evs[e]['text'].split()) for e in refs) for q in quotes):
            next_args = {'packet_ref': ref, 'cursor': fact_cursor(payload, decision['fact'])}
            raise ConflictError('live_fact_review_evidence_invalid',
                'next_tool=get_review_packet; next_args=' + canonical(next_args) +
                f". Fact {decision['fact']}: reread own evidence and choose its received quote_ref or copy a short literal quote. "
                'Do not repeat finalize with the invalid quote or borrow another fact.')
        if verdict == 'supported' and not quotes:
            raise ConflictError('live_fact_review_evidence_invalid', 'Positive review needs a literal own-evidence basis.')
    if 'reason' in decision and (not isinstance(decision['reason'], str) or len(decision['reason']) > 500):
        raise ConflictError('live_review_decisions_invalid', 'Use a brief evidence-grounded reason.')
    if verdict == 'supported' and any(decision.get(k) is False for k in ('atomic', 'support_complete', 'qualifiers_preserved')):
        raise ConflictError('live_review_decisions_invalid', 'supported conflicts with your explicit semantic checks. Repair the candidate or withhold it.')
    if decision.get('equivalent_to') in (None, decision['fact']):
        decision.pop('equivalent_to', None)  # Absence/self-addressing adds no relation or support.
    return decision


def prepare(adapter, session, args):
    ref = str(args.get('packet_ref') or '')
    with adapter.service.store.tx() as db:
        row, payload = load(adapter, session, db, ref)
        if row['result_json']:
            if canonical(args) != row['request_json']:
                raise ConflictError('live_review_decision_replay_mismatch', 'Final packet decisions are immutable.')
            return json.loads(row['result_json']), None, None
        decisions = json.loads(row['decisions_json'])
        from .headless_review_quotes import resolve_live_rows
        incoming = resolve_live_rows(session, args)
        if not isinstance(incoming, list) or len(incoming) > 240:
            raise ConflictError('live_review_decisions_invalid', 'Return at most 240 explicit decisions per operation.')
        from .fact_page_boundary import partition_rows
        relations = args.get('conflicts') or []
        if not isinstance(relations, list) or any(not isinstance(r, dict) or
                type(r.get('left')) is not int or type(r.get('right')) is not int or
                r['left'] == r['right'] or not 0 <= r['left'] < len(payload['items']) or
                not 0 <= r['right'] < len(payload['items']) for r in relations):
            raise ConflictError('live_fact_review_conflicts_invalid', 'Use distinct own packet fact numbers.')
        def dependencies(decision):
            if not isinstance(decision, dict):
                return set()
            links = set()
            for field in ('fact', 'equivalent_to'):
                value = decision.get(field)
                if value is not None:
                    if type(value) is not int:
                        return None
                    links.add(('fact', value))
            for index, relation in enumerate(relations):
                if decision.get('fact') in (relation['left'], relation['right']):
                    links.add(('conflict', index))
            existing = decision.get('conflicts_with_existing', [])
            if not isinstance(existing, list) or any(not isinstance(v, str) for v in existing):
                return None
            links.update(('existing', v) for v in existing)
            duplicate = decision.get('equivalent_to_existing')
            if duplicate:
                if not isinstance(duplicate, str):
                    return None
                links.add(('existing', duplicate))
            return links
        accepted, isolation = partition_rows(incoming,
            lambda decision: validate_decision(decision, payload, ref), dependencies)
        for decision in accepted:
            key = str(decision['fact'])
            if key in decisions and decisions[key] != decision:
                raise ConflictError('live_review_decision_replay_mismatch',
                    'A saved semantic decision cannot be overwritten. Read get_review_packet with this packet_ref; '
                    'skip saved_verdict items. If all are decided, finalize with decisions=[] after checking '
                    'relations_complete. To change meaning, repair or start a superseding packet.')
            decisions[key] = decision
        db.execute('UPDATE live_review_packets SET decisions_json=? WHERE packet_ref=?', (canonical(decisions), ref))
        isolated = bool(isolation['rejected_indices'])
        if isolated:
            db.execute('INSERT OR IGNORE INTO live_review_row_rejections VALUES(?,?,?)',
                (ref, hashlib.sha256(canonical(args).encode()).hexdigest(),
                 canonical({'raw_request': args, 'row_isolation': isolation})))
        selected = sorted({decision['fact'] for decision in accepted}) if isolated else list(range(len(payload['items'])))
        if isolated:
            # A declared edge remains binding even when its other decision was
            # omitted. Neither that edge nor a missing canonical is discarded.
            while True:
                blocked = {f for f in selected if decisions[str(f)].get('equivalent_to', f) not in selected}
                blocked.update(f for relation in relations for f in selected
                    if f in (relation['left'], relation['right']) and
                    not {relation['left'], relation['right']}.issubset(selected))
                if not blocked:
                    break
                selected = [f for f in selected if f not in blocked]
        if not selected or (not isolated and len(decisions) < len(payload['items'])) or args.get('relations_complete') is not True:
            remaining = [n for n in range(len(payload['items'])) if str(n) not in decisions]
            staged = {'packet_ref': ref, 'review_saved': True, 'complete': False,
                      'remaining_facts': len(remaining), 'cross_packet_review_required': True}
            if remaining:
                staged.update(next_tool='get_review_packet', next_args={
                    'packet_ref': ref, 'cursor': fact_cursor(payload, remaining[0])})
            else:
                staged.update(next_tool='finalize_fact_review', next_args={'packet_ref': ref, 'decisions': []},
                    instruction='All decisions are saved; review remains incomplete until you assess relations. '
                    'Keep saved decisions unchanged. After checking relations, finalize with decisions=[], '
                    'relations_complete=true and your conflicts/coverage assessment.')
            return staged, None, None
        conflicts = []
        for relation in args.get('conflicts') or []:
            left, right = relation.get('left'), relation.get('right')
            if type(left) is not int or type(right) is not int or left == right or not 0 <= left < len(payload['items']) or not 0 <= right < len(payload['items']):
                raise ConflictError('live_fact_review_conflicts_invalid', 'Use distinct local fact numbers from this packet.')
            conflicts.append({**relation, 'left_fact_id': payload['items'][left]['id'], 'right_fact_id': payload['items'][right]['id']})
        reviewed, rejected = [], []
        for f in selected:
            item = payload['items'][f]
            d = decisions[str(f)]
            reviewed.append({'fact_id': item['id'], 'revision_digest': payload['bundle'][item['id']], 'supporting_evidence_ids': [item['evidence'][e]['id'] for e in d['evidence']]})
            canonical_fact = d.get('equivalent_to', f)
            canonical_decision = decisions[str(canonical_fact)]
            if canonical_fact != f and (canonical_decision['verdict'] != 'supported' or canonical_decision.get('equivalent_to', canonical_fact) != canonical_fact):
                raise ConflictError('live_review_canonical_invalid', 'Choose one explicitly supported canonical fact per equivalence group; do not form chains or cycles.')
            if d['verdict'] != 'supported' or canonical_fact != f or d.get('equivalent_to_existing') or d.get('conflicts_with_existing'):
                rejected.append(item['id'])
        expanded = {**args, '_packet_request': args, 'run_id': row['run_id'], 'reviewed_assertions': reviewed, 'conflicts': conflicts}
        if isolated:
            expanded['_candidate_scope'] = [payload['items'][f]['id'] for f in selected]
            expanded['_row_isolation'] = isolation
            expanded['conflicts'] = [relation for relation in conflicts
                if relation['left'] in selected and relation['right'] in selected]
        else:
            expanded['_candidate_scope'] = payload.get('candidate_scope')
        if payload.get('candidate_scope') is not None or isolated:
            expanded['coverage_complete'] = False
        return None, expanded, rejected
