"""Bounded snapshot context and model-owned candidate repair; no semantic parsing."""
import hashlib
import logging
import math
import time

from . import review_packets
from .fact_ledger import persist_fact_candidates, _invalidate_story_outputs_for_fact_revision
from .research_budget import PAGE_UNITS, response_units
from .service import ConflictError, canonical

logger = logging.getLogger(__name__)


def _item(payload, number):
    if type(number) is not int or not 0 <= number < len(payload['items']):
        raise ConflictError('live_review_decisions_invalid', 'Use a fact number from this packet.')
    return payload['items'][number]


def context(adapter, session, args):
    with adapter.service.store.tx() as db:
        row, payload = review_packets.load(adapter, session, db, str(args.get('packet_ref') or ''))
        if row['result_json']:
            raise ConflictError('live_review_packet_stale', 'Start a new review attempt before reading repair context.')
        item = _item(payload, args.get('fact'))
        number = args.get('evidence', 0)
        if type(number) is not int or not 0 <= number < len(item['evidence']):
            raise ConflictError('live_fact_review_evidence_invalid', 'Evidence number must belong to this fact.')
        evidence = item['evidence'][number]
        source = db.execute('SELECT * FROM source_versions WHERE source_version_id=?', (evidence['source_version_id'],)).fetchone()
        if source is None or not source['normalized_text']:
            return {'context_available': False, 'reason': 'No retained full snapshot for this evidence; do not infer missing context.'}
        passages = adapter._core_passages(evidence['source_version_id'], source['normalized_text'])
        anchor = next((i for i, p in enumerate(passages) if p['core_offset'] <= (evidence['span_start'] or 0) < p['core_offset'] + len(p['text'])), 0)
        base = args.get('document_cursor', max(0, anchor - 2))
        cursor = args.get('cursor', 0)
        if type(base) is not int or type(cursor) is not int or base < 0 or cursor < 0 or base >= len(passages):
            raise ConflictError('live_research_cursor_invalid', 'Use the returned document_cursor/cursor.')
        end = min(len(passages), base + 5)
        page = {'packet_ref': row['packet_ref'], 'fact': args['fact'], 'context_available': True,
                'source_version_id': evidence['source_version_id'], 'document_cursor': base,
                'passages': [], 'has_more': False, 'next_args': None}
        for n in range(base + cursor, end):
            p = passages[n]
            start, text = p['core_offset'], p['text']
            sha = hashlib.sha256(text.encode()).hexdigest()
            ref = 'c' + hashlib.sha256(f"{row['packet_ref']}:{evidence['source_version_id']}:{start}:{sha}".encode()).hexdigest()[:16]
            entry = {'context_ref': ref, 'document_passage': n, 'text': text}
            candidate = {**page, 'passages': [*page['passages'], entry], 'has_more': n + 1 < end,
                         'next_args': {**args, 'document_cursor': base, 'cursor': n + 1 - base} if n + 1 < end else None}
            if response_units('get_review_context', candidate) > PAGE_UNITS:
                break
            count = db.execute('SELECT COUNT(*) FROM live_review_contexts WHERE packet_ref=?', (row['packet_ref'],)).fetchone()[0]
            if count >= 128 and db.execute('SELECT 1 FROM live_review_contexts WHERE context_ref=?', (ref,)).fetchone() is None:
                raise ConflictError('live_review_context_limit', 'Context-read limit reached; keep supported facts and withhold unresolved candidates.')
            db.execute('INSERT OR IGNORE INTO live_review_contexts VALUES(?,?,?,?,?,?)', (ref, row['packet_ref'], evidence['source_version_id'], start, start + len(text), sha))
            page = candidate
        return page


def repair(adapter, session, command_id, args):
    ref = str(args.get('packet_ref') or '')
    stable_id = 'repair_' + hashlib.sha256(canonical(args).encode()).hexdigest()[:24]
    with adapter.service.store.tx() as db:
        # Authenticate before immutable receipt replay, including after revisions changed.
        packet = db.execute('SELECT binding FROM live_review_packets WHERE packet_ref=? AND story_id=?', (ref, session.resource_id)).fetchone()
        if packet is None or packet['binding'] != review_packets.binding(session):
            raise ConflictError('live_review_packet_unknown', 'Unknown or foreign repair packet.')
        prior = adapter._command_replay(session.resource_id, stable_id, 'repair_research_fact', args)
        if prior is not None:
            return prior
        row, payload = review_packets.load(adapter, session, db, ref)
        if row['result_json']:
            raise ConflictError('live_review_packet_stale', 'Begin a new attempt before repairing a completed review.')
        changes = args.get('repairs')
        if changes is None:
            result = _apply_repair(adapter, session, db, row, payload, stable_id, args)
        else:
            if not isinstance(changes, list) or not 1 <= len(changes) <= 12 or any(not isinstance(c, dict) for c in changes):
                raise ConflictError('live_research_fact_invalid', 'Supply 1–12 scoped candidate repairs in one atomic batch.')
            numbers = [c.get('fact') for c in changes]
            if any(type(n) is not int for n in numbers) or len(set(numbers)) != len(numbers):
                raise ConflictError('live_research_fact_invalid', 'Each parent occurs once per repair batch.')
            results = [_apply_repair(adapter, session, db, row, payload, stable_id + '_' + str(n), change)
                       for n, change in enumerate(changes)]
            result = {'repairs': results, 'review_required': True, 'next_tool': 'get_review_packet',
                      'next_args': {'run_id': row['run_id']}}
            if payload.get('requested_fact_scope'):
                result['next_args']['fact_ids'] = list(dict.fromkeys(
                    fid for repaired in results for fid in repaired['fact_ids']))[:12]
        adapter._store_command(db, session.resource_id, stable_id, 'repair_research_fact', args, result)
        return result


def _apply_repair(adapter, session, db, row, payload, stable_id, args):
    ref = row['packet_ref']
    item = _item(payload, args.get('fact'))
    parent = db.execute('SELECT * FROM fact_assertions WHERE story_id=? AND assertion_id=?', (session.resource_id, item['id'])).fetchone()
    if parent['review_status'] == 'quarantined':
        raise ConflictError('live_fact_quarantined', 'Repair cannot release a quarantined assertion.')
    count = db.execute('SELECT COUNT(DISTINCT command_id) FROM live_fact_repairs WHERE story_id=? AND parent_id=?', (session.resource_id, item['id'])).fetchone()[0]
    total = db.execute('SELECT COUNT(DISTINCT r.command_id) FROM live_fact_repairs r JOIN live_review_packets p ON p.packet_ref=r.packet_ref WHERE p.run_id=?', (row['run_id'],)).fetchone()[0]
    if count >= 2 or total >= 12:
        raise ConflictError('live_research_repair_limit', 'Bounded repair exhausted; keep useful findings, withhold unresolved ones and report the remainder.')
    children = args.get('facts')
    reason = args.get('reason')
    if not isinstance(children, list) or not 1 <= len(children) <= 8 or not isinstance(reason, str) or not 1 <= len(reason) <= 500:
        raise ConflictError('live_research_fact_invalid', 'Supply 1–8 model-owned atomic replacements and a brief reason.')
    prepared = []
    evidence_only = len(children) == 1 and isinstance(children[0], dict) and children[0].get('text') == item['text']
    seen_texts = set()
    for child in children:
        if not isinstance(child, dict) or not isinstance(child.get('text'), str) or not 1 <= len(child['text']) <= 500 or not isinstance(child.get('claim_key'), str) or not 1 <= len(child['claim_key']) <= 300:
            raise ConflictError('live_research_fact_invalid', 'Each replacement needs atomic text and a model claim_key.')
        if child['text'] in seen_texts:
            raise ConflictError('live_research_fact_invalid', 'Do not repeat the same replacement in one repair.')
        seen_texts.add(child['text'])
        confidence = child.get('confidence', .9)
        if type(confidence) not in (float, int) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ConflictError('live_research_fact_invalid', 'Confidence must be a finite number from 0 to 1.')
        refs = child.get('evidence', [])
        contexts = child.get('context_refs', [])
        if not isinstance(refs, list) or not isinstance(contexts, list) or not 1 <= len(refs) + len(contexts) <= 8:
            raise ConflictError('live_fact_review_evidence_invalid', 'Select exact own evidence/context references.')
        sources = []
        for number in refs:
            if type(number) is not int or not 0 <= number < len(item['evidence']):
                raise ConflictError('live_fact_review_evidence_invalid', 'Evidence must belong to the parent.')
            ev = item['evidence'][number]
            sources.append({'url': ev['url'], 'source_version_id': ev['source_version_id'], 'supports': [{'kind': 'verified_page_span', 'text': ev['text'], 'source_version_id': ev['source_version_id'], 'span_start': ev['span_start'], 'span_end': ev['span_end'], 'chunk_id': ev['chunk_id']}]})
        allowed_versions = {e['source_version_id'] for e in item['evidence']}
        for context_ref in contexts:
            ctx = db.execute('SELECT * FROM live_review_contexts WHERE context_ref=? AND packet_ref=?', (str(context_ref), ref)).fetchone()
            if ctx is None or ctx['source_version_id'] not in allowed_versions:
                raise ConflictError('live_fact_review_evidence_invalid', 'Context must belong to this packet and parent source version.')
            sv = db.execute('SELECT normalized_text,final_url FROM source_versions WHERE source_version_id=?', (ctx['source_version_id'],)).fetchone()
            text = sv['normalized_text'][ctx['span_start']:ctx['span_end']]
            if hashlib.sha256(text.encode()).hexdigest() != ctx['span_sha256']:
                raise ConflictError('live_review_packet_stale', 'Snapshot context changed.')
            sources.append({'url': sv['final_url'], 'source_version_id': ctx['source_version_id'], 'supports': [{'kind': 'verified_page_span', 'text': text, 'source_version_id': ctx['source_version_id'], 'span_start': ctx['span_start'], 'span_end': ctx['span_end']}]})
        prepared.append({**child, 'sources': sources, 'confidence': float(confidence),
                         'selected': bool(parent['owner_selected']) if evidence_only else False,
                         'existing_fact_id': item['id'] if evidence_only else '',
                         'claim_key': f"repair:{item['id']}:{hashlib.sha256(child['text'].encode()).hexdigest()[:12]}:{child['claim_key']}"})
    now = adapter.service.store.now()
    ids = persist_fact_candidates(db, story_id=session.resource_id, poi_key=None, facts=prepared, run_id=row['run_id'], batch_id=stable_id, model_name=str(session.model), prompt_version=review_packets.POLICY_VERSION, now=now)
    for child_id in ids:
        db.execute('INSERT INTO live_fact_repairs VALUES(?,?,?,?,?,?)', (session.resource_id, item['id'], child_id, stable_id, ref, reason))
    if not evidence_only:
        db.execute("UPDATE fact_assertions SET owner_selected=0,review_status='withheld',eligibility='withheld',updated_at=? WHERE story_id=? AND assertion_id=?", (now, session.resource_id, item['id']))
        db.execute('UPDATE facts SET selected=0 WHERE story_id=? AND fact_id=?', (session.resource_id, item['id']))
        from .poi_memory import sync_poi_review_from_story
        # Withhold the exact old shared variant immediately. A provider failure
        # during the new review must not leave the superseded meaning reusable.
        sync_poi_review_from_story(db, session.resource_id, now)
    _invalidate_story_outputs_for_fact_revision(db, session.resource_id, item['id'], now)
    db.execute('UPDATE stories SET revision=revision+1,updated_at=? WHERE id=?', (now, session.resource_id))
    db.execute("UPDATE live_review_attempts SET state='superseded' WHERE packet_ref=?", (ref,))
    result = {'parent_fact_id': item['id'], 'fact_ids': ids, 'selection_preserved': evidence_only,
              'review_required': True, 'next_tool': 'get_review_packet', 'next_args': {'run_id': row['run_id']}}
    if payload.get('requested_fact_scope'):
        result['next_args']['fact_ids'] = ids
    return result


async def assess(adapter, session, args):
    """Scoped configured-model advice, cached only for this frozen attempt."""
    import json
    from .gemini import GeminiUnavailable
    from .errors import MalformedProviderResponse, PermanentProviderError
    ref = str(args.get('packet_ref') or '')
    cursor = args.get('cursor', 0)
    decision_cursor = args.get('decision_cursor', 0)
    if type(cursor) is not int or type(decision_cursor) is not int or cursor < 0 or cursor % 12 or decision_cursor < 0:
        raise ConflictError('live_research_cursor_invalid', 'Use the returned assessment cursor.')
    with adapter.service.store.tx() as db:
        row, payload = review_packets.load(adapter, session, db, ref)
        if row['result_json']:
            raise ConflictError('live_review_packet_stale', 'Assessment requires a pending review attempt.')
        cached = db.execute('SELECT payload_json FROM live_review_assessments WHERE packet_ref=? AND cursor=?', (ref, cursor)).fetchone()
        attempts = db.execute('SELECT COUNT(*) FROM live_review_assessments a JOIN live_review_packets p ON p.packet_ref=a.packet_ref WHERE p.run_id=?', (row['run_id'],)).fetchone()[0]
        if cursor >= len(payload['items']):
            raise ConflictError('live_research_cursor_invalid', 'Assessment cursor exceeds the packet.')
        items = [{'fact': n, 'text': item['text'], 'evidence': [{'evidence': e, 'text': ev['text'], 'source_url': ev['url']} for e, ev in enumerate(item['evidence'])]}
                 for n, item in enumerate(payload['items'][cursor:cursor + 12], start=cursor)]
    if cached:
        result = json.loads(cached[0])
    else:
        helper = getattr(adapter.service.providers.gemini, 'assess_fact_candidates', None)
        started = time.monotonic()
        logger.info('street_story_review_assessment event=started story=%s packet=%s cursor=%s items=%s policy=%s',
                    session.resource_id, ref, cursor, len(items), review_packets.POLICY_VERSION)
        try:
            if helper is None:
                raise GeminiUnavailable(None, 'semantic_review_helper_not_configured')
            if attempts >= 12:
                raise GeminiUnavailable(None, 'semantic_review_helper_attempt_limit')
            state = adapter._compact_context(adapter._topic_state(session.resource_id))
            result = {**await helper(items, {'identity': state['visual_identity'], 'location': state['poi_location'],
                                            'current_date_utc': payload.get('review_as_of_date_utc')}), 'helper_available': True}
        except GeminiUnavailable as exc:
            result = {'helper_available': False, 'reason': str(exc), 'decisions': []}
        except MalformedProviderResponse:
            result = {'helper_available': False, 'reason': 'semantic_review_contract_invalid', 'decisions': []}
        except PermanentProviderError:
            result = {'helper_available': False, 'reason': 'semantic_review_request_rejected', 'decisions': []}
        logger.info('street_story_review_assessment event=finished story=%s packet=%s cursor=%s available=%s model=%s reason=%s elapsed_ms=%s',
                    session.resource_id, ref, cursor, result['helper_available'], result.get('model'),
                    result.get('reason'), round((time.monotonic() - started) * 1000))
        with adapter.service.store.tx() as db:
            current, _ = review_packets.load(adapter, session, db, ref)
            if current['result_json']:
                raise ConflictError('live_review_packet_stale', 'Review completed during helper assessment.')
            db.execute('INSERT OR IGNORE INTO live_review_assessments VALUES(?,?,?)', (ref, cursor, canonical(result)))
    page = {'packet_ref': ref, 'helper_available': result['helper_available'], 'model': result.get('model'),
            'thinking_level': result.get('thinking_level'),
            'policy_version': review_packets.POLICY_VERSION, 'decisions': [], 'has_more': False,
            'instruction': 'Independent model advice, not a completed review. Read missing context, batch-repair candidates, then review fresh revisions. Do not copy suggested text without binding its own evidence.',
            'next_tool': 'finalize_fact_review'}
    decisions = result['decisions']
    for n in range(decision_cursor, len(decisions)):
        candidate = {**page, 'decisions': [*page['decisions'], decisions[n]]}
        if response_units('assess_review_packet', candidate) > PAGE_UNITS - 400:
            break
        page = candidate
    consumed = decision_cursor + len(page['decisions'])
    if decisions and not page['decisions']:
        raise ConflictError('live_review_advice_oversize', 'One helper assessment exceeds the bounded page. Keep the snapshot and perform addressed Live review; do not repeat this cursor.')
    if consumed < len(decisions):
        page.update(has_more=True, next_tool='assess_review_packet', next_args={'packet_ref': ref, 'cursor': cursor, 'decision_cursor': consumed})
    elif result['helper_available'] and cursor + len(items) < len(payload['items']):
        page.update(has_more=True, next_tool='assess_review_packet', next_args={'packet_ref': ref, 'cursor': cursor + len(items)})
    if not result['helper_available']:
        page.update(reason=result['reason'], instruction='Configured helper unavailable. Mira owns the same semantic checks in Live; unresolved candidates remain withheld. No helper verdict was recorded.')
    return page
