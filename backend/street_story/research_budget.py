"""Durable SOURCE scope and work journal, independent of provider quota/timeouts."""
from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from contextvars import ContextVar

from .errors import RetryableProviderError

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


class ResearchWorkExhausted(RetryableProviderError):
    """A category cannot start more work; independent work remains valid."""
    def __init__(self, kind, reason):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.not_sent = True


def reserve_available_work(service, story_id, kind, unit_ids, *, purpose='identity'):
    """Journal each distinct admitted unit; cached reads remain reusable."""
    admitted = []
    for unit in dict.fromkeys(unit_ids):
        if kind == 'pages':
            from .prussia39 import cached_get_available
            if cached_get_available(service.store, unit):
                admitted.append(unit)
                continue
        try:
            reserve_work(service, story_id, kind, [unit], purpose=purpose)
            admitted.append(unit)
        except ResearchWorkExhausted:
            continue
    return admitted


def _envelope(service, row, research, *, start=None):
    started = float(row['created_at'] if start is None else start)
    return {'policy_version': 'photo-research-continuation-v2',
            'photo_sha256': row['photo_sha256'],
            'identity_generation': int(research.get('identity_generation') or 0),
            'started_at': started,
            'identity_deadline_at': None,
            'deadline_at': None,
            'last_progress_at': started}


def archive_attempt(research, now):
    """Keep prior scope, clocks and outcomes before an authorized new wave."""
    record = {key: research.get(key) for key in (
        'research_budget', 'automatic_research_outcome', 'identity_attempt_outcome') if research.get(key)}
    if not record:
        return
    history = research.setdefault('research_attempt_history', [])
    if history and {key: value for key, value in history[-1].items() if key != 'archived_at'} == record:
        return
    history.append(json.loads(json.dumps({**record, 'archived_at': now})))


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
            archive_attempt(research, service.store.now())
            budget = _envelope(service, row, research,
                start=service.store.now() if explicit or scope_changed else None)
            research['research_budget'] = budget
            research.pop('automatic_research_outcome', None)
            conn.execute('UPDATE stories SET research_json=? WHERE id=?',
                         (json.dumps(research, ensure_ascii=False), story_id))
        elif not research.get('automatic_research_outcome') and budget.get('policy_version') != 'photo-research-continuation-v2':
            # Migrate only an active wave. Historical terminal outcomes and
            # cancelled jobs stay closed; original provider bindings are never
            # renewed or resent by changing this scheduler policy.
            budget = {**budget, 'policy_version': 'photo-research-continuation-v2',
                'previous_deadlines': {key: budget.get(key) for key in ('identity_deadline_at', 'deadline_at')},
                'identity_deadline_at': None, 'deadline_at': None}
            research['research_budget'] = budget
            conn.execute('UPDATE stories SET research_json=? WHERE id=?',
                         (json.dumps(research, ensure_ascii=False), story_id))
            LOG.info('street_story_research_continuation_enabled story_id=%s generation=%s',
                     story_id, budget['identity_generation'])
        return budget


def remaining_seconds(service, story_id, purpose='facts', *, db=None):
    ensure_budget(service, story_id, db=db)
    # Compatibility for callers bounding ONE request by min(request_timeout,
    # remaining). The background task itself has no elapsed-time deadline.
    return float('inf')


def require_remaining(service, story_id, purpose='facts'):
    remaining = remaining_seconds(service, story_id, purpose)
    with service.store.connection() as db:
        research = json.loads(service._story_row(db, story_id)['research_json'] or '{}')
    finished = research.get('automatic_research_outcome')
    if finished:
        raise ResearchTerminated(finished['outcome'], 'research_attempt_already_finished')
    return remaining


def bounded_timeout(service, story_id, seconds, purpose='facts'):
    return min(float(seconds), require_remaining(service, story_id, purpose))


def reserve_work(service, story_id, kind, unit_ids, *, purpose='identity'):
    """Journal distinct evidence work, preserving already-addressed units.

    This is a work journal; provider admission and receipts still own actual
    sends/accounting. Reusing one exact reference on its original operation
    does not create another unit.
    """
    if kind not in {'exact_pairs', 'query_hypotheses', 'pages', 'planner_calls'}:
        raise ValueError('research_work_kind_invalid')
    with service.store.tx() as db:
        budget = ensure_budget(service, story_id, db=db)
        row = service._story_row(db, story_id)
        research = json.loads(row['research_json'] or '{}')
        finished = research.get('automatic_research_outcome')
        if finished:
            raise ResearchTerminated(finished['outcome'], 'research_attempt_already_finished')
        work = budget.setdefault('work_units', {})
        previous = list(work.get(kind) or [])
        combined = list(dict.fromkeys([*previous, *unit_ids]))
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


def finish_requested_clarification(service, db, story_id, *, job=None):
    """A current model question ends waiting while retaining the original clock."""
    row = service._story_row(db, story_id)
    research = json.loads(row['research_json'] or '{}')
    question = research.get('identity_clarification') or {}
    revision = int(((research.get('research_controls') or {}).get('identity') or {}).get('revision') or 0)
    if (research.get('automatic_research_outcome') or question.get('answered')
            or question.get('background_continuation_pending') is True
            or (research.get('visual_identity') or {}).get('status') in {'match', 'owner_confirmed'}
            or question.get('photo_sha256') != row['photo_sha256']
            or question.get('generation') != int(research.get('identity_generation') or 0)
            or question.get('control_revision') != revision
            or not isinstance(question.get('question'), str) or not question['question'].strip()):
        return False
    return finish_attempt(service, db, story_id, outcome='clarification_required',
        reason='model_requested_geographic_clarification', purpose='identity', job=job)


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
    # A slow discovery sibling may close its own search after another
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
                   str((research.get('identity_clarification') or {}).get('question') or '')[:500]
                       if outcome == 'clarification_required' else
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
        (json.dumps(research, ensure_ascii=False), state, None if matched else
            'identity_clarification_required' if outcome == 'clarification_required' else 'visual_identity_uncertain',
         None if matched else str((research.get('identity_clarification') or {}).get('question') or '')[:500]
            if outcome == 'clarification_required' else 'Не удалось надёжно определить объект по доступным подтверждениям.',
         service.store.now(), story_id))
    LOG.info('street_story_research_terminal story_id=%s outcome=%s reason=%s eligible=%s coverage_complete=%s',
             story_id, outcome, reason, eligible, coverage_complete)
    return True


def maintain_queued_budgets(service, db):
    """Migrate only pending active budgets, outside worker claim polling."""
    rows = list(db.execute("SELECT DISTINCT s.id FROM stories s JOIN jobs j ON j.story_id=s.id "
        "WHERE j.kind IN ('identity','identity_visual','research','refinement') "
        "AND j.state IN ('ready','retry') "
        "AND json_extract(s.research_json,'$.automatic_research_outcome') IS NULL "
        "AND (json_extract(s.research_json,'$.research_budget.policy_version') IS NOT 'photo-research-continuation-v2' "
        "OR json_extract(s.research_json,'$.research_budget.photo_sha256') IS NOT s.photo_sha256 "
        "OR json_extract(s.research_json,'$.research_budget.identity_generation') IS NOT "
        "coalesce(json_extract(s.research_json,'$.identity_generation'),0))"))
    for row in rows:
        ensure_budget(service, row['id'], db=db)
    if rows:
        LOG.info('street_story_research_budget_maintenance changed_stories=%s', len(rows))
    return len(rows)


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
