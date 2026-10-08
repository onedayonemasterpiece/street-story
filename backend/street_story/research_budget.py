"""Durable wall-clock envelope for one photo research wave, not a quota ledger."""
from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from contextvars import ContextVar

LOG = logging.getLogger(__name__)
RESEARCH_KINDS = {'identity', 'identity_visual', 'research', 'refinement'}
RESEARCH_SEND_GUARD = ContextVar('street_story_research_send_guard', default=None)


def guard_research_send():
    """Inherited worker scope fences requests after asynchronous admission."""
    guard = RESEARCH_SEND_GUARD.get()
    if guard is not None:
        guard()


class ResearchTerminated(Exception):
    def __init__(self, outcome='deadline_exceeded', reason='research_deadline_exceeded'):
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason


def _envelope(service, row, research, *, start=None):
    started = float(row['created_at'] if start is None else start)
    return {'policy_version': 'photo-research-budget-v1',
            'photo_sha256': row['photo_sha256'],
            'identity_generation': int(research.get('identity_generation') or 0),
            'started_at': started,
            'identity_deadline_at': started + service.settings.identity_timeout_seconds,
            'deadline_at': started + service.settings.research_timeout_seconds,
            'last_progress_at': started}


def ensure_budget(service, story_id, *, db=None, explicit=False):
    with service.store.tx() if db is None else nullcontext(db) as conn:
        row = service._story_row(conn, story_id)
        research = json.loads(row['research_json'] or '{}')
        budget = research.get('research_budget') or {}
        scope_changed = bool(budget and (budget.get('photo_sha256') != row['photo_sha256']
            or budget.get('identity_generation') != int(research.get('identity_generation') or 0)))
        if explicit or not budget or scope_changed:
            # Legacy waves inherit upload time. Only a new owner scope or an
            # explicit owner retry authorizes another envelope.
            budget = _envelope(service, row, research,
                start=service.store.now() if explicit or scope_changed else None)
            research['research_budget'] = budget
            research.pop('automatic_research_outcome', None)
            conn.execute('UPDATE stories SET research_json=? WHERE id=?',
                         (json.dumps(research, ensure_ascii=False), story_id))
        return budget


def remaining_seconds(service, story_id, purpose='facts', *, db=None):
    budget = ensure_budget(service, story_id, db=db)
    key = 'identity_deadline_at' if purpose == 'identity' else 'deadline_at'
    return max(0.0, float(budget[key]) - service.store.now())


def require_remaining(service, story_id, purpose='facts'):
    remaining = remaining_seconds(service, story_id, purpose)
    with service.store.connection() as db:
        research = json.loads(service._story_row(db, story_id)['research_json'] or '{}')
    finished = research.get('automatic_research_outcome')
    if finished:
        raise ResearchTerminated(finished['outcome'], 'research_attempt_already_finished')
    if remaining <= 0:
        raise ResearchTerminated(reason='identity_deadline_exceeded' if purpose == 'identity'
                                 else 'research_deadline_exceeded')
    return remaining


def bounded_timeout(service, story_id, seconds, purpose='facts'):
    return min(float(seconds), require_remaining(service, story_id, purpose))


def reserve_work(service, story_id, kind, unit_ids, *, purpose='identity'):
    """Bound distinct evidence work, preserving already-addressed units.

    This is a wave envelope; provider admission and receipts still own actual
    sends/accounting. Reusing one exact reference on its original operation
    does not consume another slot.
    """
    limit = {'exact_pairs': service.settings.identity_max_exact_pairs,
             'query_hypotheses': service.settings.identity_max_query_hypotheses,
             'pages': service.settings.identity_max_pages,
             'planner_calls': service.settings.identity_max_planner_calls}.get(kind)
    if limit is None:
        raise ValueError('research_work_kind_invalid')
    with service.store.tx() as db:
        budget = ensure_budget(service, story_id, db=db)
        row = service._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
        finished = research.get('automatic_research_outcome')
        if finished:
            raise ResearchTerminated(finished['outcome'], 'research_attempt_already_finished')
        deadline = budget['identity_deadline_at' if purpose == 'identity' else 'deadline_at']
        if deadline <= service.store.now():
            raise ResearchTerminated()
        work = budget.setdefault('work_units', {})
        previous = list(work.get(kind) or [])
        combined = list(dict.fromkeys([*previous, *unit_ids]))
        if len(combined) > limit:
            scope = {'exact_pairs': 'exact_pair', 'query_hypotheses': 'query_hypothesis',
                     'pages': 'page', 'planner_calls': 'planner_call'}[kind]
            raise ResearchTerminated('search_exhausted', 'identity_' + scope + '_envelope_exhausted')
        if combined != previous:
            work[kind] = combined
            row = service._story_row(db, story_id)
            research = json.loads(row['research_json'] or '{}')
            research['research_budget'] = budget
            db.execute('UPDATE stories SET research_json=? WHERE id=?',
                       (json.dumps(research, ensure_ascii=False), story_id))
        return combined


def note_evidence(service, db, story_id, evidence_id, *, generation=None):
    """Record novel evidence, never provider polling or admission heartbeat."""
    row = service._story_row(db, story_id)
    research = json.loads(row['research_json'] or '{}')
    budget = research.get('research_budget')
    if not budget or research.get('automatic_research_outcome'):
        return
    if generation is not None and generation != int(research.get('identity_generation') or 0):
        return
    previous = budget.setdefault('evidence_units', [])
    if evidence_id in previous:
        return
    budget['evidence_units'] = [*previous, evidence_id][-64:]
    budget['last_progress_at'] = service.store.now()
    db.execute('UPDATE stories SET research_json=? WHERE id=?',
               (json.dumps(research, ensure_ascii=False), story_id))


def finish_attempt(service, db, story_id, *, outcome, reason, purpose='facts', job=None,
                   coverage_complete=False):
    """End product waiting without destroying addressed sends or evidence."""
    row = service._story_row(db, story_id)
    research = json.loads(row['research_json'] or '{}')
    if job is not None:
        payload = json.loads(job['payload_json'] or '{}')
        if (payload.get('photo_sha256', row['photo_sha256']) != row['photo_sha256']
                or payload.get('identity_generation', research.get('identity_generation', 0))
                != research.get('identity_generation', 0)
                or not db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND attempts=?",
                                  (job['id'], job['attempts'])).fetchone()):
            return False
    eligible = db.execute("SELECT count(*) FROM fact_assertions WHERE story_id=? AND eligibility='eligible'",
                          (story_id,)).fetchone()[0]
    matched = (research.get('visual_identity') or {}).get('status') in {'match', 'owner_confirmed'}
    if matched and eligible:
        outcome = 'useful_complete' if coverage_complete else 'useful_partial'
    result = {
        'outcome': outcome, 'reason': reason, 'finished_at': service.store.now(),
        'coverage_complete': bool(coverage_complete), 'eligible_count': eligible,
        'purpose': purpose, 'identity_generation': int(research.get('identity_generation') or 0),
        'photo_sha256': row['photo_sha256']}
    budget = research.setdefault('research_budget', _envelope(service, row, research))
    # A slow discovery sibling may reach its identity deadline after another
    # worker has proved the object. Its closure cannot end the fact wave.
    identity_already_proved = purpose == 'identity' and matched
    research['identity_attempt_outcome' if identity_already_proved else 'automatic_research_outcome'] = result
    if not identity_already_proved:
        progress = dict(research.get('identity_progress') or {})
        progress.update(finished=True, updated_at=service.store.now(),
            generation=int(research.get('identity_generation') or 0),
            elapsed_ms=max(0, round((service.store.now() - budget['started_at']) * 1000)))
        steps = [dict(step, status='warning') if step.get('status') == 'working' else dict(step)
                 for step in progress.get('steps') or []]
        message = ('Найдены подтверждённые факты · исследование завершено' if matched and eligible else
                   'Объект определён · подтверждённых фактов пока нет' if matched else
                   'Поиск завершён · источники временно недоступны' if outcome == 'resource_blocked' else
                   'Поиск завершён · доказательств недостаточно')
        steps = [step for step in steps if step.get('key') != 'research_result']
        progress['steps'] = [*steps, {'key': 'research_result', 'label': message,
                                    'status': 'done' if matched and eligible else 'warning'}][-9:]
        research['identity_progress'] = progress
    kinds = ('identity', 'identity_visual') if purpose == 'identity' else tuple(RESEARCH_KINDS)
    db.execute("UPDATE jobs SET state='done',lease_until=0,last_error=?,updated_at=? "
               "WHERE story_id=? AND kind IN (" + ','.join('?' for _ in kinds) + ") "
               "AND state IN ('ready','retry','running')", (reason, service.store.now(), story_id, *kinds))
    # Frozen provider receipts and reservations intentionally remain untouched.
    if not identity_already_proved:
        db.execute("UPDATE research_runs SET state='completed',status_detail=?,updated_at=? "
                   "WHERE story_id=? AND identity_generation=? AND state NOT IN ('completed','cancelled')",
                   (outcome, service.store.now(), story_id, int(research.get('identity_generation') or 0)))
    state = row['state']
    if not identity_already_proved and state in {'photo_ready', 'identifying', 'needs_review', 'identity_ready', 'researching'}:
        state = 'facts_ready' if matched and eligible else 'identity_ready' if matched else 'needs_review'
    db.execute('UPDATE stories SET research_json=?,state=?,error_code=?,error_message=?,revision=revision+1,updated_at=? WHERE id=?',
        (json.dumps(research, ensure_ascii=False), state, None if matched else 'visual_identity_uncertain',
         None if matched else 'Не удалось надёжно определить объект за время исследования.',
         service.store.now(), story_id))
    LOG.info('street_story_research_terminal story_id=%s outcome=%s reason=%s eligible=%s coverage_complete=%s',
             story_id, outcome, reason, eligible, coverage_complete)
    return True


def expire_queued(service, db):
    now = service.store.now()
    rows = list(db.execute("SELECT DISTINCT s.* FROM stories s JOIN jobs j ON j.story_id=s.id "
        "WHERE j.kind IN ('identity','identity_visual','research','refinement') "
        "AND j.state IN ('ready','retry')"))
    expired = 0
    for row in rows:
        budget = ensure_budget(service, row['id'], db=db)
        research = json.loads(service._story_row(db, row['id'])['research_json'] or '{}')
        matched = (research.get('visual_identity') or {}).get('status') in {'match', 'owner_confirmed'}
        purpose = 'facts' if matched else 'identity'
        deadline = budget['deadline_at'] if matched else budget['identity_deadline_at']
        if now >= deadline:
            # A live owner is stopped by its bounded worker; do not fence a
            # concurrent provider observer simply because a sibling is queued.
            if db.execute("SELECT 1 FROM jobs WHERE story_id=? AND state='running' AND lease_until>?",
                          (row['id'], now)).fetchone():
                continue
            expired += int(finish_attempt(service, db, row['id'], outcome='deadline_exceeded',
                reason='research_deadline_exceeded' if matched else 'identity_deadline_exceeded', purpose=purpose))
    return expired


# Existing bounded Live page contract remains authoritative.
PAGE_UNITS = 5500


def response_units(name, result, call_id="x" * 160):
    envelope = {"toolResponse": {"functionResponses": [
        {"id": call_id, "name": name, "response": {"result": result}},
    ]}}
    # Equivalent to ai-resource-control 0.1.11's text/JSON estimator. Do not
    # import or copy its private implementation into this public consumer.
    return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())


def bounded_inventory(read, name, args, items_key):
    limit = min(int(args.get("limit") or 30), 50)
    while limit > 0:
        result = read({**args, "limit": limit})
        if response_units(name, result) <= PAGE_UNITS:
            return result
        limit -= 1
    from .service import ConflictError
    raise ConflictError("live_review_item_oversize", "One item exceeds the page ceiling. Use the bounded review packet passages; data is preserved.")
