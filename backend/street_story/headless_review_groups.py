"""Group addressed dependencies; only model replies declare semantic relations."""
import json

from . import review_packets
from .service import canonical


def groups(db, story_id, run_id, pending, blocked):
    current = review_packets.bundle(db, story_id)
    links = [dict(row) for row in db.execute('SELECT l.* FROM live_pending_review_links l '
        'WHERE l.story_id=? AND l.run_id=? AND NOT EXISTS(SELECT 1 FROM live_pending_review_resolutions r '
        'WHERE r.story_id=l.story_id AND r.run_id=l.run_id AND r.left_id=l.left_id '
        'AND r.right_id=l.right_id AND r.packet_ref=l.packet_ref) ORDER BY l.rowid', (story_id, run_id))
        if current.get(row['left_id']) == row['left_revision']
        and current.get(row['right_id']) == row['right_revision']]
    ids = list(pending)
    while True:
        added = [fid for row in links if row['left_id'] in ids or row['right_id'] in ids
                 for fid in (row['left_id'], row['right_id']) if fid not in ids]
        if not added:
            break
        ids.extend(dict.fromkeys(added))
    parent = {fid: fid for fid in ids}

    def root(fid):
        while parent[fid] != fid:
            fid = parent[fid]
        return fid

    def join(first, second):
        if first in parent and second in parent:
            parent[root(second)] = root(first)

    seen = {}
    for fid in ids:
        keys = [('source', row[0]) for row in db.execute('SELECT e.source_version_id FROM fact_evidence_spans e '
            'JOIN fact_observations o ON o.observation_id=e.observation_id WHERE o.story_id=? '
            "AND o.assertion_id=? AND o.status='accepted'", (story_id, fid)) if row[0]]
        keys.extend(('model_claim_key', row[0]) for row in db.execute('SELECT model_claim_key FROM fact_observations '
            "WHERE story_id=? AND assertion_id=? AND status='accepted'", (story_id, fid)) if row[0])
        for key in keys:
            if key in seen:
                join(seen[key], fid)
            seen[key] = fid
    for row in links:
        join(row['left_id'], row['right_id'])
    components = {}
    for fid in ids:
        components.setdefault(root(fid), []).append(fid)
    return [group for group in components.values() if not set(group).intersection(blocked)]


def context(db, story_id, run_id, candidates):
    current = review_packets.bundle(db, story_id)
    return [{'fact_id': fid, 'revision_digest': current[fid],
             'text': db.execute('SELECT text FROM facts WHERE story_id=? AND fact_id=?', (story_id, fid)).fetchone()[0],
             'authority': 'relation_context_only'}
            for group in groups(db, story_id, run_id, review_packets.pending_candidates(db, story_id, run_id), set())
            for fid in group if fid not in candidates and fid in current]


def unit_recipe(db, story, run_id, candidates, contract):
    return [contract, run_id, candidates, review_packets.candidate_review_fence(db, story),
            review_packets.canonical_review_fence(db, story, candidates, include_projection=True),
            context(db, story['id'], run_id, candidates), review_packets.REVIEW_SLICE_CONTRACT]


def record_links(engine, job, run_id, packet, args):
    peers = {row['fact_id']: row for row in packet.get('pending_relation_context', [])}
    if not peers:
        return
    with engine.service.store.tx() as db:
        original = json.loads(db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=?',
            (packet['packet_ref'],)).fetchone()[0])
        current = review_packets.bundle(db, job['story_id'])
        for decision in args.get('decisions', []):
            if not isinstance(decision, dict) or type(decision.get('fact')) is not int:
                continue
            number = decision['fact']
            related = decision.get('related_pending_ids', [])
            if not 0 <= number < len(original['items']) or not isinstance(related, list) or not 1 <= len(related) <= 12:
                continue
            if any(not isinstance(fid, str) or fid not in peers for fid in related):
                continue
            own = original['items'][number]['id']
            if current.get(own) != original['bundle'].get(own):
                continue
            for fid in dict.fromkeys(related):
                if current.get(fid) != peers[fid]['revision_digest']:
                    continue
                db.execute('INSERT OR IGNORE INTO live_pending_review_links VALUES(?,?,?,?,?,?,?,?)',
                    (job['story_id'], run_id, own, fid, current[own], current[fid], packet['packet_ref'], canonical(decision)))


def close_links(engine, job, run_id, packet):
    with engine.service.store.tx() as db:
        row = db.execute('SELECT payload_json,decisions_json,result_json FROM live_review_packets WHERE packet_ref=?',
            (packet['packet_ref'],)).fetchone()
        if not row['result_json']:
            return
        original, decisions = json.loads(row['payload_json']), json.loads(row['decisions_json'])
        reviewed = {original['items'][int(number)]['id'] for number in decisions}
        for link in db.execute('SELECT * FROM live_pending_review_links WHERE story_id=? AND run_id=?',
                               (job['story_id'], run_id)).fetchall():
            if {link['left_id'], link['right_id']} <= reviewed:
                db.execute('INSERT OR IGNORE INTO live_pending_review_resolutions VALUES(?,?,?,?,?,?)',
                    (job['story_id'], run_id, link['left_id'], link['right_id'], link['packet_ref'], packet['packet_ref']))
