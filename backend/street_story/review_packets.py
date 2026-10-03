"""Durable mechanical review addressing; every semantic verdict is model supplied."""
import hashlib
import json
import uuid

from .research_budget import PAGE_UNITS, response_units
from .service import ConflictError, canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_review_packets(
 packet_ref TEXT PRIMARY KEY, story_id TEXT NOT NULL REFERENCES stories(id),
 run_id TEXT NOT NULL, binding TEXT NOT NULL, story_revision INTEGER NOT NULL,
 identity_generation INTEGER NOT NULL, payload_json TEXT NOT NULL,
 decisions_json TEXT NOT NULL DEFAULT '{}', result_json TEXT, request_json TEXT
);
"""


def binding(session):
    return hashlib.sha256(canonical(getattr(session, 'actor', None)).encode()).hexdigest()


def bundle(db, story_id):
    return {str(r['assertion_id']): str(r['revision_digest']) for r in db.execute(
        'SELECT a.assertion_id,a.revision_digest FROM fact_assertions a JOIN facts f '
        'ON f.story_id=a.story_id AND f.fact_id=a.assertion_id WHERE a.story_id=? '
        'AND f.evidence_supported=1 ORDER BY a.assertion_id', (story_id,))}


def load(adapter, session, db, ref):
    row = db.execute('SELECT * FROM live_review_packets WHERE packet_ref=? AND story_id=?',
                     (ref, session.resource_id)).fetchone()
    if row is None or row['binding'] != binding(session):
        raise ConflictError('live_review_packet_unknown', 'Packet belongs to another story or owner.')
    if row['result_json']:
        return row, json.loads(row['payload_json'])
    story, _ = adapter._research_run_guard(db, session, row['run_id'])
    research = json.loads(story['research_json'] or '{}')
    payload = json.loads(row['payload_json'])
    if int(story['revision']) != row['story_revision'] or int(research.get('identity_generation') or 0) != row['identity_generation'] or bundle(db, session.resource_id) != payload['bundle']:
        raise ConflictError('live_review_packet_stale', 'Revisions changed; request a new packet. No decision applied.')
    actual = {r['evidence_id']: dict(r) for r in db.execute(
        "SELECT e.evidence_id,e.span_sha256,o.assertion_id FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id WHERE o.story_id=? AND o.status='accepted'", (session.resource_id,))}
    for item in payload['items']:
        for ev in item['evidence']:
            if actual.get(ev['id']) != {'evidence_id': ev['id'], 'span_sha256': ev['sha'], 'assertion_id': item['id']}:
                raise ConflictError('live_review_packet_stale', 'Evidence changed; request a new packet.')
    return row, payload


def read(adapter, session, args):
    with adapter.service.store.tx() as db:
        ref = str(args.get('packet_ref') or '')
        if not ref:
            run_id = str(args.get('run_id') or '')
            story, run = adapter._research_run_guard(db, session, run_id)
            exact = bundle(db, session.resource_id)
            items = []
            for fact_id in exact:
                text = db.execute('SELECT text FROM facts WHERE story_id=? AND fact_id=?', (session.resource_id, fact_id)).fetchone()[0]
                evidence = [dict(r) for r in db.execute(
                    "SELECT e.evidence_id AS id,e.span_sha256 AS sha,e.span_text AS text,e.source_url AS url FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id WHERE o.story_id=? AND o.assertion_id=? AND o.status='accepted' ORDER BY e.evidence_id", (session.resource_id, fact_id))]
                if not evidence:
                    raise ConflictError('live_fact_review_evidence_required', 'Accepted evidence is required for every assertion.')
                items.append({'id': fact_id, 'text': text, 'evidence': evidence})
            reused = {}
            indexes = {item['id']: f for f, item in enumerate(items)}
            for prior in db.execute('SELECT payload_json,decisions_json FROM live_review_packets WHERE story_id=? AND binding=? AND identity_generation=? ORDER BY rowid', (session.resource_id, binding(session), int(run['identity_generation']))):
                old = json.loads(prior['payload_json'])
                for number, decision in json.loads(prior['decisions_json']).items():
                    old_item = old['items'][int(number)]
                    fact_id = old_item['id']
                    if fact_id not in indexes or old['bundle'].get(fact_id) != exact[fact_id] or decision['verdict'] == 'equivalent':
                        continue
                    f = indexes[fact_id]
                    current_evs = {ev['id']: (e, ev['sha']) for e, ev in enumerate(items[f]['evidence'])}
                    chosen = [old_item['evidence'][e] for e in decision['evidence']]
                    if all(ev['id'] in current_evs and current_evs[ev['id']][1] == ev['sha'] for ev in chosen):
                        reused[str(f)] = {**decision, 'fact': f, 'evidence': [current_evs[ev['id']][0] for ev in chosen]}
            ref = 'p' + uuid.uuid4().hex[:12]
            payload = {'bundle': exact, 'items': items}
            db.execute('INSERT INTO live_review_packets(packet_ref,story_id,run_id,binding,story_revision,identity_generation,payload_json,decisions_json) VALUES(?,?,?,?,?,?,?,?)',
                       (ref, session.resource_id, run_id, binding(session), int(story['revision']), int(run['identity_generation']), canonical(payload), canonical(reused)))
        row, payload = load(adapter, session, db, ref)
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
    page = {'packet_ref': ref, 'run_id': row['run_id'], 'items': [], 'total_facts': len(payload['items']),
            'next_cursor': None, 'has_more': False, 'next_tool': 'finalize_fact_review'}
    while cursor < len(slices):
        candidate = {**page, 'items': [*page['items'], slices[cursor]], 'next_cursor': cursor + 1, 'has_more': cursor + 1 < len(slices)}
        if response_units('get_review_packet', candidate) > PAGE_UNITS:
            if not page['items']:
                raise ConflictError('live_review_item_oversize', 'Assertion text exceeds a bounded review operation; all data preserved.')
            break
        page = candidate
        cursor += 1
    return page


def prepare(adapter, session, args):
    ref = str(args.get('packet_ref') or '')
    with adapter.service.store.tx() as db:
        row, payload = load(adapter, session, db, ref)
        if row['result_json']:
            if canonical(args) != row['request_json']:
                raise ConflictError('live_review_decision_replay_mismatch', 'Final packet decisions are immutable.')
            return json.loads(row['result_json']), None, None
        decisions = json.loads(row['decisions_json'])
        incoming = args.get('decisions')
        if not isinstance(incoming, list) or len(incoming) > 240:
            raise ConflictError('live_review_decisions_invalid', 'Return at most 240 explicit decisions per operation.')
        for decision in incoming:
            if not isinstance(decision, dict) or type(decision.get('fact')) is not int or not 0 <= decision['fact'] < len(payload['items']):
                raise ConflictError('live_review_decisions_invalid', 'Use local fact numbers from this packet.')
            verdict = decision.get('verdict')
            if verdict not in {'supported', 'not_supported', 'contradicted', 'role_mismatch', 'equivalent'}:
                raise ConflictError('live_review_decisions_invalid', 'Explicit semantic support verdict required.')
            if verdict == 'equivalent':
                other = decision.get('equivalent_to')
                if type(other) is not int or other == decision['fact'] or not 0 <= other < len(payload['items']):
                    raise ConflictError('live_review_decisions_invalid', 'Equivalence requires a distinct canonical fact number.')
            refs = decision.get('evidence')
            evs = payload['items'][decision['fact']]['evidence']
            if not isinstance(refs, list) or not refs or any(type(e) is not int or not 0 <= e < len(evs) for e in refs):
                raise ConflictError('live_fact_review_evidence_invalid', 'Use evidence numbers scoped to this assertion.')
            key = str(decision['fact'])
            if key in decisions and decisions[key] != decision:
                raise ConflictError('live_review_decision_replay_mismatch', 'A saved semantic decision cannot be overwritten.')
            decisions[key] = decision
        db.execute('UPDATE live_review_packets SET decisions_json=? WHERE packet_ref=?', (canonical(decisions), ref))
        if len(decisions) < len(payload['items']) or args.get('relations_complete') is not True:
            return {'packet_ref': ref, 'review_saved': True, 'complete': False, 'remaining_facts': len(payload['items']) - len(decisions), 'cross_packet_review_required': True}, None, None
        conflicts = []
        for relation in args.get('conflicts') or []:
            left, right = relation.get('left'), relation.get('right')
            if type(left) is not int or type(right) is not int or left == right or not 0 <= left < len(payload['items']) or not 0 <= right < len(payload['items']):
                raise ConflictError('live_fact_review_conflicts_invalid', 'Use distinct local fact numbers from this packet.')
            conflicts.append({**relation, 'left_fact_id': payload['items'][left]['id'], 'right_fact_id': payload['items'][right]['id']})
        reviewed, rejected = [], []
        for f, item in enumerate(payload['items']):
            d = decisions[str(f)]
            reviewed.append({'fact_id': item['id'], 'revision_digest': payload['bundle'][item['id']], 'supporting_evidence_ids': [item['evidence'][e]['id'] for e in d['evidence']]})
            if d['verdict'] != 'supported':
                rejected.append(item['id'])
        expanded = {**args, '_packet_request': args, 'run_id': row['run_id'], 'reviewed_assertions': reviewed, 'conflicts': conflicts}
        return None, expanded, rejected
