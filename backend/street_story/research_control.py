"""Explicit durable research Stop/Resume, separate from microphone Stop."""
from __future__ import annotations

import json
import logging
from contextlib import nullcontext

from .service import ConflictError, canonical

PURPOSES = ('identity', 'facts')
FLAGS = {'identity': 'identity_research_cancelled', 'facts': 'fact_research_cancelled'}
KINDS = {'identity': {'identity', 'identity_visual'}, 'facts': {'research', 'refinement'}}
ACTIVE = {'ready', 'retry', 'running'}
LOG = logging.getLogger(__name__)


def research_stopped(research, purpose, *, photo_sha256, identity_generation):
    """A cancellation applies only to the photograph/generation it stopped."""
    record = (research.get('research_controls') or {}).get(purpose)
    if not isinstance(record, dict):
        return bool(research.get(FLAGS[purpose]))  # Existing legacy Stop records.
    return bool(record.get('stopped') and record.get('photo_sha256') == photo_sha256
                and record.get('identity_generation') == identity_generation)


def _purposes(purpose):
    if purpose == 'all':
        return PURPOSES
    if purpose not in PURPOSES:
        raise ValueError('purpose must be identity, facts, or all')
    return (purpose,)


def _scope(story, research, expected_photo_sha256, expected_identity_generation, expected_control_revision=None):
    generation = int(research.get('identity_generation') or 0)
    if ((expected_photo_sha256 is not None and expected_photo_sha256 != story['photo_sha256'])
            or (expected_identity_generation is not None and expected_identity_generation != generation)
            or (expected_control_revision is not None
                and expected_control_revision != int(research.get('research_control_revision') or 0))):
        raise ConflictError('research_control_stale', 'Фото или объект изменился. Обновите состояние истории.')
    return {'photo_sha256': story['photo_sha256'], 'identity_generation': generation}


def stop_research(service, story_id, *, purpose='all', expected_photo_sha256=None,
                  expected_identity_generation=None, expected_control_revision=None, _db=None):
    """Fence unfinished work and retain all finished facts, drafts and cursors.

    The worker must fence its own state writes by claimed attempts/state. Native
    inferences already sent are observed to completion, never resubmitted here.
    """
    purposes = _purposes(purpose)
    with service.store.tx() if _db is None else nullcontext(_db) as db:
        story = service._story_row(db, story_id)
        research = json.loads(story['research_json'] or '{}')
        scope = _scope(story, research, expected_photo_sha256, expected_identity_generation, expected_control_revision)
        controls = research.setdefault('research_controls', {})
        changed = []
        for item in purposes:
            if isinstance(controls.get(item), dict) and research_stopped(research, item, **scope):
                continue
            now = service.store.now()
            epoch = int(research.get('research_control_revision') or 0) + 1
            research['research_control_revision'] = epoch
            marker = f'research_control_stop:{item}:{epoch}'
            record = {**scope, 'stopped': True, 'revision': epoch, 'stopped_at': now,
                      'marker': marker, 'jobs': [], 'run_ids': []}
            for job in db.execute('SELECT * FROM jobs WHERE story_id=?', (story_id,)).fetchall():
                payload = json.loads(job['payload_json'] or '{}')
                if (job['kind'] not in KINDS[item] or job['state'] not in ACTIVE
                        or payload.get('identity_generation', scope['identity_generation']) != scope['identity_generation']
                        or payload.get('photo_sha256', scope['photo_sha256']) != scope['photo_sha256']):
                    continue
                record['jobs'].append({'id': job['id'], 'state': job['state'], 'last_error': job['last_error']})
                # Advancing a running attempt invalidates its heartbeat and
                # conditional completion even if Resume happens before it returns.
                db.execute("UPDATE jobs SET state='cancelled',attempts=attempts+?,lease_until=0,last_error=?,updated_at=? WHERE id=?",
                           (int(job['state'] == 'running'), marker, now, job['id']))
            if item == 'identity':
                visual = research.get('visual_search_operation')
                if isinstance(visual, dict):
                    visual.update(lease_owner=None, lease_until=0, control_revision=epoch)
            else:
                if 'pending_fact_request' in research:
                    record['pending_fact_request'] = research.pop('pending_fact_request')
                runs = db.execute("SELECT run_id FROM research_runs WHERE story_id=? AND identity_generation=? "
                                  "AND state NOT IN ('completed','cancelled','failed')",
                                  (story_id, scope['identity_generation'])).fetchall()
                for run in runs:
                    record['run_ids'].append(run['run_id'])
                    db.execute("UPDATE research_runs SET state='cancelled',status_detail=?,updated_at=? WHERE run_id=?",
                               (marker, now, run['run_id']))
                    db.execute('UPDATE research_chunk_runs SET lease_owner=NULL,lease_until=0,lease_fence=lease_fence+1 '
                               'WHERE run_id=?', (run['run_id'],))
            controls[item] = record
            research[FLAGS[item]] = True
            changed.append(item)
        if changed:
            db.execute('UPDATE stories SET research_json=?,revision=revision+1,updated_at=? WHERE id=?',
                       (canonical(research), service.store.now(), story_id))
            LOG.info('street_story_research_control action=stop story_id=%s purposes=%s generation=%s revision=%s',
                     story_id, ','.join(changed), scope['identity_generation'], research['research_control_revision'])
        return {'action': 'stop', 'purposes': list(purposes), 'changed': changed,
                **scope, 'research_control_revision': int(research.get('research_control_revision') or 0),
                'story': service._story_repr(db, service._story_row(db, story_id))}


def resume_research(service, story_id, *, purpose='all', expected_photo_sha256=None,
                    expected_identity_generation=None, expected_control_revision=None, _db=None):
    """Explicitly wake only this Stop's existing jobs and frozen checkpoints."""
    purposes = _purposes(purpose)
    with service.store.tx() if _db is None else nullcontext(_db) as db:
        story = service._story_row(db, story_id)
        research = json.loads(story['research_json'] or '{}')
        scope = _scope(story, research, expected_photo_sha256, expected_identity_generation, expected_control_revision)
        controls = research.get('research_controls') or {}
        changed, resumed_jobs = [], []
        epoch = int(research.get('research_control_revision') or 0) + 1
        for item in purposes:
            record = controls.get(item)
            if not isinstance(record, dict) or not record.get('stopped'):
                continue
            if any(record.get(key) != value for key, value in scope.items()):
                raise ConflictError('research_control_stale', 'Остановленное исследование относится к другому фото или объекту.')
            now = service.store.now()
            marker = record['marker']
            for job in record.get('jobs') or []:
                update = db.execute("UPDATE jobs SET state='retry',available_at=?,lease_until=0,last_error=?,updated_at=? "
                                    "WHERE id=? AND story_id=? AND state='cancelled' AND last_error=?",
                                    (now, job.get('last_error'), now, job['id'], story_id, marker))
                if update.rowcount:
                    resumed_jobs.append(job['id'])
            for run_id in record.get('run_ids') or []:
                db.execute("UPDATE research_runs SET state='partial',status_detail='explicit_resume',updated_at=? "
                           "WHERE run_id=? AND story_id=? AND state='cancelled' AND status_detail=?",
                           (now, run_id, story_id, marker))
            if item == 'facts' and record.get('pending_fact_request'):
                # No implicit recovery path sees this request until Resume.
                if 'pending_fact_request' in research:
                    raise ConflictError('research_control_pending_changed', 'Появился другой запрос исследования; обновите состояние.')
                research['pending_fact_request'] = record.pop('pending_fact_request')
            record.update(stopped=False, resumed_at=now, revision=epoch)
            if item == 'identity':
                visual = research.get('visual_search_operation') or {}
                if (visual.get('photo_sha256') == scope['photo_sha256']
                        and visual.get('generation') == scope['identity_generation']):
                    visual.update(control_revision=epoch, lease_owner=None, lease_until=0)
            research[FLAGS[item]] = False
            changed.append(item)
        if changed:
            research['research_control_revision'] = epoch
            db.execute('UPDATE stories SET research_json=?,revision=revision+1,updated_at=? WHERE id=?',
                       (canonical(research), service.store.now(), story_id))
            scheduler = getattr(service, '_resume_joined_fact_request', None)
            if 'facts' in changed and callable(scheduler):
                scheduler(db, story_id)
            LOG.info('street_story_research_control action=resume story_id=%s purposes=%s generation=%s revision=%s jobs=%s',
                     story_id, ','.join(changed), scope['identity_generation'], research['research_control_revision'], len(resumed_jobs))
        return {'action': 'resume', 'purposes': list(purposes), 'changed': changed,
                'resumed_job_ids': resumed_jobs, **scope,
                'research_control_revision': int(research.get('research_control_revision') or 0),
                'story': service._story_repr(db, service._story_row(db, story_id))}
