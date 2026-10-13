"""Independent fact agents inside the existing durable research job."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from .errors import RetryableProviderError
from .fact_agent_providers import AgentRoute, run_tool_agent
from .research_runs import set_run_state

LOG = logging.getLogger(__name__)
QUALIFICATION_KEY = 'research-agent-verification-v1'


def configured_routes(adapter, *, include_unqualified=False):
    settings = adapter.service.settings
    config = list(settings.fact_agent_routes)
    gemini = adapter.service.providers.gemini
    if not config:
        config = [{'provider_id': 'google', 'model_id': model} for model, *_ in getattr(gemini, 'research_routes', ())]
        if adapter.giga is not None:
            config.append({'provider_id': 'gigachat', 'model_id': adapter.giga.binding['model']})
    proofs = (adapter.service.store.cache_get(QUALIFICATION_KEY) or {}).get('routes', [])
    routes = []
    for item in config:
        if item.get('enabled') is False:
            continue
        provider, model = item['provider_id'], item['model_id']
        if provider == 'google' and settings.gemini_keys:
            if not any(route[0] == model for route in gemini.research_routes):
                # Same quota SDK, keys and shared authority as ordinary Gemini research.
                from .gemini import GeminiExecutor, GeminiKeyPool
                from .quota import SharedQuotaGate
                pool = GeminiKeyPool(gemini.store, settings.gemini_keys, model, policy=gemini.pool.policy)
                quota = SharedQuotaGate(settings, pool)
                gemini.research_routes.append((model, pool, quota, GeminiExecutor(pool)))
            route = AgentRoute(provider, model, 'https://generativelanguage.googleapis.com', gemini)
        elif provider == 'gigachat' and adapter.giga is not None:
            route = AgentRoute(provider, model, 'https://api.giga.chat/v1/chat/completions', adapter.giga)
        elif provider == 'opencode' and item.get('directory'):
            from .shared_devcoveer_research import SharedDevCoveerResearch
            from .opencode_research import ResearchLimits
            clients = adapter.__dict__.setdefault('_fact_agent_clients', {})
            key = (model, item['directory'])
            if key not in clients:
                clients[key] = SharedDevCoveerResearch(item['directory'], model_id=model,
                    provider_id='opencode', limits=ResearchLimits(timeout_seconds=720, max_steps=4,
                        max_output_tokens=4096), agent_research=True,
                    admission=adapter.client.admission if adapter.client else adapter.agent_admission,
                    checkpoint=adapter.checkpoint, scope_guard=adapter.transport_scope_guard)
            client = clients[key]
            route = AgentRoute(provider, model, client.endpoint, client, client.directory)
        else:
            continue
        if item.get('endpoint') and item['endpoint'].rstrip('/') != route.endpoint.rstrip('/'):
            continue
        qualified = any(isinstance(proof, dict) and proof.get('route_id') == route.route_id
            and proof.get('profile_sha256') == route.profile_fingerprint
            and proof.get('contract') == QUALIFICATION_KEY
            and proof.get('tool_access_verified') is True and proof.get('tool_safety_verified') is True
            and proof.get('qualification_receipts') for proof in proofs)
        if include_unqualified or qualified:
            if route.route_id not in {old.route_id for old in routes}:
                routes.append(route)
    return routes


def route_plan(routes):
    """First two independent services, then at most two unused route fallbacks."""
    primary, tail, seen = [], [], set()
    for route in routes:
        if route.provider_id not in seen and len(primary) < 2:
            primary.append(route)
            seen.add(route.provider_id)
        else:
            tail.append(route)
    return primary + tail[:max(0, 4-len(primary))] if len(primary) == 2 else []


def agent_context(identity, goal, run_id, now):
    # Explicit allowlist prevents prior facts, URLs and camera context entering discovery.
    selected = {key: identity[key] for key in ('candidate_id', 'name', 'names', 'address',
        'city', 'search_anchor', 'owner_context', 'building_polygon') if key in identity}
    return {'selected_object': selected, 'editorial_goal': goal, 'run_id': run_id,
        'review_as_of_date_utc': datetime.fromtimestamp(now, timezone.utc).date().isoformat()}


async def run_fact_agents(adapter, job, run_id, goal):
    from .headless_facts import HeadlessFacts
    headless = HeadlessFacts(adapter.service)
    first = headless._snapshot(job, run_id)
    if first is None:
        return None
    story, research, _run = first
    epoch = story['_fact_research_control_revision']
    stage = 'fact_agent_plan:' + run_id
    saved = adapter.service.store.checkpoint_get(job['id'], stage)
    available = configured_routes(adapter)
    routes_by_id = {route.route_id: route for route in available}
    if saved is None:
        plan = route_plan(available)
        if not plan:
            _finish(adapter, job, run_id, headless, epoch, 'research_routes_unavailable', pending=True)
            return {'outcome': 'resource_blocked', 'reason': 'research_routes_unavailable'}
        saved = {'pipeline': QUALIFICATION_KEY, 'route_ids': [r.route_id for r in plan],
            'context': agent_context(research['visual_identity'], goal, run_id, adapter.service.store.now()),
            'routes': {}}
        adapter.service.store.checkpoint_put(job['id'], stage, saved)
    context = saved['context']

    def guard():
        if headless._snapshot(job, run_id, epoch) is None:
            from .service import ConflictError
            raise ConflictError('agent_research_stale', 'Owner selection or Stop changed.')

    async def one(route_id):
        guard()
        state = saved['routes'].get(route_id, {})
        route = routes_by_id.get(route_id)
        if state.get('phase') in {'completed', 'failed'} or (state.get('phase') == 'unknown'
                and (route is None or route.provider_id != 'opencode')):
            # No blind retry of a failed/unknown synchronous operation.
            return state
        if route is None:
            state = {'phase': 'failed', 'reason': 'research_agent_route_unavailable'}
        else:
            role = 'facts_agent_' + route.route_id
            unit = {'run_id': run_id, 'context': context, 'route_id': route.route_id}
            digest = hashlib.sha256(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            owned = {**story, '_fact_pool_unit_id': 'agent_'+digest[:24], '_fact_pool_input_sha256': digest}
            try:
                async def invoke(binding):
                    return await run_tool_agent(adapter, route, owned, context, binding=binding, guard=guard)
                result = await adapter.run(owned, role, unit, invoke, client=route)
                receipt = result['receipt']
                if receipt.get('phase') != 'completed':
                    raise RetryableProviderError('research_agent_original_'+str(receipt.get('phase', 'unknown')))
                agent = {'route_id': route.route_id, 'provider_id': route.provider_id,
                    'model_id': route.model_id, 'attempt_id': receipt['binding']['attempt_id'],
                    'elapsed_seconds': receipt.get('elapsed_seconds'), 'prompt_sha256': receipt.get('prompt_sha256')}
                outcomes = []
                for candidate in result['result']['facts']:
                    guard()
                    outcomes.append(await adapter.service.confirm_agent_fact(job, run_id, candidate, agent))
                fact_ids = {outcome['fact_id'] for outcome in outcomes if outcome.get('fact_id')}
                with adapter.service.store.connection() as db:
                    eligible_ids = {row[0] for row in db.execute(
                        "SELECT assertion_id FROM fact_assertions WHERE story_id=? AND eligibility='eligible'",
                        (job['story_id'],))} & fact_ids
                state = {'phase': 'completed', 'attempt_id': agent['attempt_id'], 'outcomes': outcomes,
                    'candidate_count': len(outcomes),
                    'eligible_fact_ids': sorted(eligible_ids), 'supported_count': len(eligible_ids)}
            except RetryableProviderError as exc:
                # Existing attempt journal determines unknown versus closed failure.
                logical = hashlib.sha256(json.dumps([story['id'], story['photo_sha256'],
                    story['_identity_generation'], role, unit], ensure_ascii=False,
                    separators=(',', ':'), sort_keys=True).encode()).hexdigest()
                with adapter.service.store.connection() as db:
                    row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE logical_id=? '
                        'ORDER BY created_at DESC,rowid DESC LIMIT 1', (logical,)).fetchone()
                receipt = json.loads(row[0]) if row else {}
                state = {'phase': 'unknown' if receipt.get('phase') not in {'failed', 'completed', 'created'} else 'failed',
                    'reason': str(exc), 'attempt_id': (receipt.get('binding') or {}).get('attempt_id')}
            saved['routes'][route_id] = state
            adapter.service.store.checkpoint_put(job['id'], stage, saved)
            LOG.info('street_story_fact_agent run_id=%s route_id=%s phase=%s candidates=%s',
                run_id, route_id, state['phase'], state.get('candidate_count', 0))
        return state

    ids = saved['route_ids']
    tasks = {asyncio.create_task(one(route_id)) for route_id in ids[:2]}
    unused, outcomes = iter(ids[2:]), []
    try:
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = task.result()
                outcomes.append(result)
                if len({fact_id for row in outcomes for fact_id in row.get('eligible_fact_ids', [])}) < 3:
                    route_id = next(unused, None)
                    if route_id is not None:
                        tasks.add(asyncio.create_task(one(route_id)))
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    pending = any(result.get('phase') == 'unknown' or any(outcome.get('status') == 'unverified'
        for outcome in result.get('outcomes', [])) for result in outcomes)
    failed = any(result.get('phase') == 'failed' for result in outcomes)
    outcome = _finish(adapter, job, run_id, headless, epoch,
        'research_agent_original_pending' if pending else 'research_agent_route_failed' if failed else 'research_agent_completed',
        pending=pending or failed)
    if pending:
        with adapter.service.store.connection() as db:
            addressed = db.execute("SELECT 1 FROM research_provider_attempts WHERE story_id=? "
                "AND role LIKE 'facts_agent_%' AND json_extract(receipt_json,'$.binding.job_id')=? "
                "AND json_extract(receipt_json,'$.phase') NOT IN ('created','completed','failed','aborted') "
                "AND json_extract(receipt_json,'$.session_id') IS NOT NULL "
                "AND json_extract(receipt_json,'$.message_id') IS NOT NULL LIMIT 1", (story['id'], job['id'])).fetchone()
        if addressed:
            raise RetryableProviderError('research_agent_original_pending', retry_at=adapter.service.store.now()+30)
    return outcome


def _finish(adapter, job, run_id, headless, epoch, reason, *, pending):
    from .service import canonical
    with adapter.service.store.tx() as db:
        snapshot = headless._snapshot(job, run_id, epoch)
        if snapshot is None:
            return None
        story, research, _ = snapshot
        eligible = db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=? AND eligibility='eligible'",
            (job['story_id'],)).fetchone()[0]
        outcome = 'useful_partial' if eligible and pending else 'useful_complete' if eligible else 'resource_blocked' if pending else 'no_supported_facts'
        value = {'outcome': outcome, 'reason': reason, 'eligible_count': eligible,
            'coverage_complete': not pending, 'pipeline': QUALIFICATION_KEY,
            'photo_sha256': story['photo_sha256'], 'identity_generation': story['_identity_generation'],
            'purpose': 'facts', 'run_id': run_id}
        research['research_outcome'] = value
        unavailable = reason == 'research_routes_unavailable'
        set_run_state(db, run_id, 'failed' if unavailable else 'partial' if pending else 'completed', detail=reason,
            now=adapter.service.store.now(), completed=not pending or unavailable)
        db.execute('UPDATE stories SET research_json=?,state=CASE WHEN draft_text IS NULL THEN ? ELSE state END,updated_at=? WHERE id=?',
            (canonical(research), 'facts_ready', adapter.service.store.now(), story['id']))
    adapter.service.store.checkpoint_put(job['id'], 'headless_fact_outcome:'+run_id, value)
    return value
