"""Independent fact agents inside the existing durable research job."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from random import SystemRandom

from .errors import RetryableProviderError
from .fact_agent_providers import AgentRoute, run_tool_agent
from .research_runs import set_run_state

LOG = logging.getLogger(__name__)
QUALIFICATION_KEY = 'research-agent-verification-v1'


def retained_plan_scope_matches(db, job, plan, *, photo_sha256, generation, control_revision):
    """The saved original inputs and retained bindings belong to this owner epoch."""
    expected = {'story_id': job['story_id'], 'job_id': job['id'], 'photo_sha256': photo_sha256,
        'generation': generation, 'control_revision': control_revision}
    found = False
    for route_id, state in plan.get('routes', {}).items():
        retained = state.get('retained') or {}
        if not retained.get('attempt_id'):
            continue
        found = True
        unit = {'run_id': plan.get('context', {}).get('run_id'), 'context': plan.get('context'), 'route_id': route_id}
        digest = hashlib.sha256(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        for attempt_id, binding_expected in ((state.get('attempt_id'),
                {**expected, 'fact_input_sha256': digest, 'fact_unit_id': 'agent_'+digest[:24]}),
                (retained['attempt_id'], expected)):
            row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if not row or any((json.loads(row[0]).get('binding') or {}).get(key) != value
                    for key, value in binding_expected.items()):
                return False
    return found


def retained_retry_waits(db, job, plan, *, photo_sha256, generation, control_revision):
    """Read authoritative unsent retained waits without authorizing any send."""
    from .research_adapter import retained_not_sent_retry_at
    waits = []
    for original_route_id, state in plan.get('routes', {}).items():
        retained = state.get('retained') or {}
        row = db.execute('SELECT role,receipt_json FROM research_provider_attempts WHERE attempt_id=?',
            (retained.get('attempt_id'),)).fetchone()
        if not row or not row['role'].startswith('facts_agent_retained_'):
            continue
        receipt = json.loads(row['receipt_json'])
        binding = receipt.get('binding') or {}
        original_row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
            (state.get('attempt_id'),)).fetchone()
        original = json.loads(original_row[0]) if original_row else {}
        original_binding = original.get('binding') or {}
        original_unit = {'run_id': plan.get('context', {}).get('run_id'),
            'context': plan.get('context'), 'route_id': original_route_id}
        original_digest = hashlib.sha256(json.dumps(original_unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if (any(binding.get(key) != value for key, value in {
                'story_id': job['story_id'], 'job_id': job['id'], 'photo_sha256': photo_sha256,
                'generation': generation, 'control_revision': control_revision}.items())
                or any(original_binding.get(key) != value for key, value in {
                    'story_id': job['story_id'], 'job_id': job['id'], 'photo_sha256': photo_sha256,
                    'generation': generation, 'control_revision': control_revision,
                    'fact_input_sha256': original_digest, 'fact_unit_id': 'agent_'+original_digest[:24]}.items())
                or receipt.get('source_cohort_sha256') != retained.get('source_cohort_sha256')
                or receipt.get('original_attempt_id') != state.get('attempt_id')):
            continue
        deadline = retained_not_sent_retry_at(db, receipt)
        if deadline is not None:
            waits.append({'attempt_id': binding['attempt_id'], 'retry_at': deadline,
                'code': receipt['route_failure']['code'], 'send_state': 'not_sent'})
    return waits


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
        elif provider == 'codex_native' and model == 'gpt-6-luna':
            from ai_resource_control.workload import WorkloadAdmission
            from .native_fact_research import NativeFactResearch
            from .native_vision import ACCOUNT_SCOPE
            client = getattr(adapter, 'native_facts', None)
            if client is None:
                admission = adapter.fenced_admission(WorkloadAdmission(adapter.control, ACCOUNT_SCOPE)) if adapter.control else adapter.agent_admission
                adapter.native_facts = client = NativeFactResearch(adapter.service,
                    admission=admission, checkpoint=adapter.checkpoint)
            route = AgentRoute(provider, model, client.endpoint, client)
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
    """Bound concurrent primaries, retaining every qualified fallback route."""
    routes = list(routes)
    native = [route for route in routes if route.provider_id == 'codex_native']
    if native:
        # Luna owns the decision and native child call; no host count heuristic
        # starts Sol or a second simultaneous research operation.
        return native[:1] + [route for route in routes if route.provider_id != 'codex_native']
    SystemRandom().shuffle(routes)
    primary, tail, seen = [], [], set()
    for route in routes:
        if route.provider_id not in seen and len(primary) < 2:
            primary.append(route)
            seen.add(route.provider_id)
        else:
            tail.append(route)
    return primary + tail


def _coverage_gaps(state):
    """Continuation signals from receipts/admission, never a fact-count target."""
    retained = state.get('retained') or {}
    if retained.get('phase') in {'completed', 'failed', 'unknown'}:
        return _coverage_gaps(retained)
    gaps = []
    if state.get('phase') in {'failed', 'unknown'}:
        gaps.append('original_' + state['phase'])
    completion = state.get('native_completion')
    if isinstance(completion, dict) and (not completion.get('research_sufficient')
            or completion.get('remaining_gaps')):
        gaps.append('model_reported_research_gap')
    native_decision = state.get('provider_id') == 'codex_native' and isinstance(completion, dict)
    if (not native_decision and state.get('phase') == 'completed'
            and not state.get('candidate_count', len(state.get('outcomes', [])))):
        gaps.append('no_source_backed_candidates')
        if state.get('source_read_failures'):
            gaps.append('source_read_unavailable')
    if state.get('rejected_candidate_count'):
        gaps.append('candidate_format_rejections')
    if not native_decision and state.get('unproposed_source_version_ids'):
        gaps.append('read_sources_without_candidates')
    if any(row.get('status') not in {'supported', 'duplicate'} for row in state.get('outcomes', [])):
        gaps.append('candidate_admission_unresolved')
    return gaps


def _source_coverage(receipt):
    """Identify completed own reads not represented by a candidate URL."""
    from .fact_agent_providers import _read_url_key
    proposed = {_read_url_key(row['url']) for row in (receipt.get('result') or {}).get('facts', [])
        if isinstance(row.get('url'), str)}
    proposed_versions = {(row.get('native_basis') or {}).get('source_version_id')
        for row in (receipt.get('result') or {}).get('facts', [])}
    unproposed = set()
    for source in receipt.get('sources', []):
        if not source.get('source_version_id') or not source.get('content_sha256'):
            continue
        urls = {_read_url_key(source[key]) for key in ('requested_url', 'final_url')
            if isinstance(source.get(key), str)}
        if (receipt.get('provider_id') == 'codex_native' and source['source_version_id'] not in proposed_versions
                or receipt.get('provider_id') != 'codex_native' and not (urls - {None}) & proposed):
            unproposed.add(source['source_version_id'])
    return {**({'native_completion': (receipt.get('raw_result') or {}).get('completion')}
            if receipt.get('provider_id') == 'codex_native' else {}),
        'rejected_candidate_count': len(receipt.get('rejected_candidates') or []),
        'unproposed_source_version_ids': sorted(unproposed),
        'source_read_failures': [attempt for call in receipt.get('native_tool_calls', {}).values()
            if call.get('phase') == 'completed' and (call.get('input') or {}).get('name') == 'webfetch'
            for attempt in (call.get('result') or {}).get('read_attempts', [])
            if attempt.get('status') in {'failed', 'rejected'}],
        'original_send_unresolved': receipt.get('phase') == 'unknown'
            or receipt.get('provider_send_state') == 'possibly_sent'
            or receipt.get('error_code') == 'CancelledError' and receipt.get('last_request_sent') is True}


def _healthy_routes(adapter, routes):
    from .research_adapter import LOCAL_IMAGE_FAILURES
    from .service import canonical
    store = adapter.service.store
    healthy = []
    for route in routes:
        route_key = 'research-route-health:' + hashlib.sha256(canonical([
            route.endpoint, route.model_id, route.directory, route.profile_fingerprint]).encode()).hexdigest()
        quota_key = 'research-quota-health:' + route.provider_id + ':' + route.model_id
        blocked = False
        for key in (route_key, quota_key):
            health = store.cache_get(key) or {}
            if (key == quota_key and health.get('category') == 'RESOURCE_DAILY_BUDGET'
                    or health.get('category') in LOCAL_IMAGE_FAILURES and health.get('status') is None):
                continue
            if health.get('retry_at', 0) > store.now():
                blocked = True
        if not blocked:
            healthy.append(route)
    return healthy


def _persist_plan(adapter, job, run_id, headless, epoch, *, initial=None, route_id=None, state=None, recovery=False,
                  continuation=False):
    """Freeze discovery inputs once; merge only route progress under owner fences."""
    from .service import ConflictError, canonical
    store = adapter.service.store
    stage = 'fact_agent_plan:' + run_id
    with store.tx() as db:
        if headless._snapshot(job, run_id, epoch) is None:
            raise ConflictError('agent_research_stale', 'Owner selection or Stop changed.')
        if initial is not None:
            db.execute('INSERT OR IGNORE INTO research_checkpoints(job_id,stage,value_json,created_at) VALUES(?,?,?,?)',
                (job['id'], stage, canonical(initial), store.now()))
        row = db.execute('SELECT value_json FROM research_checkpoints WHERE job_id=? AND stage=?',
            (job['id'], stage)).fetchone()
        if row is None:
            raise ConflictError('agent_research_plan_missing', 'The original agent plan is unavailable.')
        saved = json.loads(row[0])
        if route_id is not None:
            if route_id not in saved['route_ids']:
                raise ConflictError('agent_research_plan_changed', 'The route is outside the original agent plan.')
            routes = saved.setdefault('routes', {})
            old = routes.get(route_id, {})
            if continuation and (old.get('phase') != 'completed' or old.get('provider_id') != 'codex_native'
                    or state.get('phase') != 'created' or state.get('parent_attempt_id') != old.get('attempt_id')
                    or not (old.get('native_completion') or {}).get('next_action')):
                raise ConflictError('agent_research_continuation_changed', 'Closed owned model action required.')
            if (old.get('phase') not in {'completed', 'failed'} or recovery
                    and state.get('phase') in {old.get('phase'), 'completed'} and state.get('attempt_id')
                    and state['attempt_id'] == old.get('attempt_id') or continuation):
                routes[route_id] = state
                db.execute('UPDATE research_checkpoints SET value_json=? WHERE job_id=? AND stage=?',
                    (canonical(saved), job['id'], stage))
        return saved


def _recover_route_state(adapter, job, run_id, story, context, route_id):
    """A missing plan slot does not erase an already journalled original send."""
    from .service import ConflictError, canonical
    unit = {'run_id': run_id, 'context': context, 'route_id': route_id}
    role = 'facts_agent_' + route_id
    logical = hashlib.sha256(canonical([story['id'], story['photo_sha256'],
        story['_identity_generation'], role, unit]).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with adapter.service.store.connection() as db:
        row = db.execute('SELECT attempt_id,receipt_json FROM research_provider_attempts '
            'WHERE logical_id=? AND story_id=? AND role=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
            (logical, story['id'], role)).fetchone()
    if row is None:
        return None
    receipt = json.loads(row['receipt_json'])
    binding = receipt.get('binding') or {}
    expected = {'attempt_id': row['attempt_id'], 'story_id': story['id'], 'photo_sha256': story['photo_sha256'],
        'generation': story['_identity_generation'], 'job_id': job['id'],
        'control_revision': story['_fact_research_control_revision'],
        'fact_unit_id': 'agent_' + digest[:24], 'fact_input_sha256': digest}
    if any(binding.get(key) != value for key, value in expected.items()):
        raise ConflictError('agent_research_original_binding_invalid', 'The original agent binding is inconsistent.')
    if (receipt.get('phase') == 'created' and receipt.get('provider_send_state') in {None, 'not_sent'}
            and receipt.get('last_request_sent') is not True):
        return None  # Existing not-sent binding still uses ordinary admission.
    phase = receipt.get('phase')
    candidates = (receipt.get('result') or {}).get('facts') or []
    return {'phase': 'completed' if phase == 'completed' else 'failed' if phase in {'failed', 'aborted'} else 'unknown',
        'original_phase': phase, 'reason': receipt.get('error_code', 'original_agent_state_recovered'),
        'attempt_id': row['attempt_id'], 'provider_id': receipt.get('provider_id'),
        'candidate_count': len(candidates), **_source_coverage(receipt),
        'outcomes': [{'status': 'unverified', 'reason': 'original_confirmation_unavailable'} for _ in candidates]}


async def _recover_confirmations(adapter, job, run_id, story, context, route_id, state, guard):
    """Re-admit original candidates from a closed agent, without invoking its SDK."""
    from .service import canonical
    attempt_id = state.get('attempt_id')
    if not attempt_id:
        return None
    unit = {'run_id': run_id, 'context': context, 'route_id': route_id}
    logical = hashlib.sha256(canonical([story['id'], story['photo_sha256'],
        story['_identity_generation'], 'facts_agent_' + route_id, unit]).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    store = adapter.service.store
    with store.connection() as db:
        row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=? '
            'AND logical_id=? AND story_id=? AND role=?',
            (attempt_id, logical, story['id'], 'facts_agent_' + route_id)).fetchone()
    receipt = json.loads(row[0]) if row else {}
    binding = receipt.get('binding') or {}
    expected = {'attempt_id': attempt_id, 'story_id': story['id'], 'photo_sha256': story['photo_sha256'],
        'generation': story['_identity_generation'], 'job_id': job['id'],
        'control_revision': story['_fact_research_control_revision'],
        'fact_unit_id': 'agent_' + digest[:24], 'fact_input_sha256': digest}
    candidates = (receipt.get('result') or {}).get('facts')
    if any(binding.get(key) != value for key, value in expected.items()):
        return None
    if receipt.get('phase') in {'failed', 'created', 'aborted'}:
        recovered = {**state, **_source_coverage(receipt)}
        return recovered if recovered != state else None
    if receipt.get('phase') != 'completed' or not isinstance(candidates, list):
        return None
    if state.get('phase') == 'completed' and not any(
            row.get('status') == 'unverified' for row in state.get('outcomes', [])):
        recovered = {**state, **_source_coverage(receipt)}
        return recovered if recovered != state else None
    outcomes = []
    for index, candidate in enumerate(candidates):
        guard()
        old = (state.get('outcomes') or [])[index:index+1]
        if old and old[0].get('status') != 'unverified':
            outcomes.append(old[0])
            continue
        confirmation = hashlib.sha256(canonical({'run': run_id,
            'candidate': candidate, 'agent': route_id}).encode()).hexdigest()[:24]
        saved = store.checkpoint_get(job['id'], 'agent_confirmation:' + confirmation) or {}
        outcome = saved.get('outcome') or {}
        agent = saved.get('agent') or outcome.get('agent') or (old[0].get('agent') if old else {}) or {}
        if (not outcome and saved.get('candidate') != candidate or agent.get('route_id') != route_id
                or agent.get('attempt_id') != attempt_id):
            outcomes.append(old[0] if old else {'status': 'unverified', 'reason': 'original_confirmation_unavailable'})
            continue
        # Saved UNKNOWN returns pending; a CLOSED assessment reuses retained
        # chunks. A missing stage must never cause a fresh read or judge send.
        outcomes.append(await adapter.service.confirm_agent_fact(job, run_id, candidate, agent))
    guard()
    fact_ids = {outcome['fact_id'] for outcome in outcomes if outcome.get('fact_id')}
    with store.connection() as db:
        eligible = {row[0] for row in db.execute(
            "SELECT assertion_id FROM fact_assertions WHERE story_id=? AND eligibility='eligible'",
            (story['id'],))} & fact_ids
    return {**state, 'phase': 'completed', 'attempt_id': attempt_id, 'outcomes': outcomes, **_source_coverage(receipt),
        'candidate_count': len(candidates), 'eligible_fact_ids': sorted(eligible), 'supported_count': len(eligible)}


def agent_context(identity, goal, run_id, now):
    # Explicit allowlist prevents prior facts, URLs and camera context entering discovery.
    selected = {key: identity[key] for key in ('candidate_id', 'name', 'names', 'address',
        'city', 'search_anchor', 'owner_context', 'building_polygon') if key in identity}
    task_goal = (goal + '\nExtract distinct useful knowledge supported by the available sources. '
        'Use already read pages first; investigate remaining historical, architectural, people, '
        'organization and event gaps with another concise query or article when resources permit. '
        'Vary street-type suffixes and quoted versus unquoted address queries when needed. '
        'Read the full page before citing it. Never invent facts to meet a count.')
    return {'selected_object': selected, 'editorial_goal': task_goal, 'run_id': run_id,
        'review_as_of_date_utc': datetime.fromtimestamp(now, timezone.utc).date().isoformat()}


def _context_source_refs(sources):
    """Keep exact provenance; full bodies remain in the owned reader/ledger."""
    return [{**{key: value for key, value in source.items()
                if key not in ('confirmed_fragment', 'supports')},
        'supports': [{key: value for key, value in support.items() if key != 'text'}
            for support in source.get('supports') or []]} for source in sources]


def _distinct_discovery_context(items):
    """Losslessly union repeated discovery observations, never rank or truncate."""
    from .service import canonical
    distinct = {}
    for item in items:
        fields = {key: value for key, value in item.items() if key != 'original_attempt_id'}
        key = canonical(fields)
        if key not in distinct:
            distinct[key] = dict(item)
        else:
            saved = distinct[key]
            attempts = saved.setdefault('original_attempt_ids', [saved['original_attempt_id']])
            if item['original_attempt_id'] not in attempts:
                attempts.append(item['original_attempt_id'])
    return list(distinct.values())


def native_agent_context(service, story, identity, goal, run_id):
    """Owned accumulated knowledge and the existing complete conflict journal."""
    from .conflict_journal import conflict_journal
    from .agent_evidence import selected_object_context
    context = agent_context(identity, goal, run_id, service.store.now())
    context['selected_object'].update(selected_object_context(identity))
    with service.store.connection() as db:
        context['known'] = [{**dict(row), 'sources': json.loads(row['sources_json'] or '[]')}
            for row in db.execute('SELECT a.assertion_id AS fact_id,a.display_text AS text,'
                'a.semantic_key AS claim_key,a.eligibility,a.review_status,a.eligibility_reason,'
                'a.revision_digest,f.sources_json '
                'FROM fact_assertions a LEFT JOIN facts f ON f.story_id=a.story_id AND f.fact_id=a.assertion_id '
                'WHERE a.story_id=? ORDER BY a.assertion_id', (story['id'],))]
        for item in context['known']:
            item.pop('sources_json', None)
            # Known claims are context, never a substitute for an own read.
            # Keep every source/version/span reference without resending whole
            # article chunks once per accepted claim on every continuation.
            item['sources'] = _context_source_refs(item['sources'])
        reviews = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_review_packets'").fetchone():
            current = {item['fact_id']: item['revision_digest'] for item in context['known']}
            for record in db.execute('SELECT p.packet_ref,p.payload_json,p.decisions_json '
                    'FROM live_review_packets p JOIN live_review_attempts a ON a.packet_ref=p.packet_ref '
                    'WHERE p.story_id=? AND p.identity_generation=? AND a.state=? ORDER BY p.rowid',
                    (story['id'], story['_identity_generation'], 'finished')):
                packet = json.loads(record['payload_json'])
                if (packet.get('owner_fence') or {}).get('photo') != story['photo_sha256']:
                    continue
                for decision in json.loads(record['decisions_json']).values():
                    index = decision.get('fact')
                    if type(index) is not int or not 0 <= index < len(packet.get('items', [])):
                        continue
                    item = packet['items'][index]
                    fact_id = item['id']
                    revision = (packet.get('bundle') or {}).get(fact_id)
                    reviews.append({'packet_ref': record['packet_ref'], 'fact_id': fact_id,
                        'revision_digest': revision, 'current_revision': bool(revision and current.get(fact_id) == revision),
                        'verdict': decision.get('verdict'), 'reason': decision.get('reason'),
                        'source_version_ids': sorted({e['source_version_id'] for e in item.get('evidence', [])
                            if e.get('source_version_id')}), 'kind': 'own_review_history_context_not_evidence'})
        context['source_review_history'] = reviews
        prior, leads, queries, receipts = [], [], [], []
        for record in db.execute("SELECT receipt_json FROM research_provider_attempts WHERE story_id=? "
                "AND role LIKE 'facts_agent_%' ORDER BY created_at", (story['id'],)):
            receipt = json.loads(record[0])
            binding = receipt.get('binding') or {}
            if (binding.get('photo_sha256') != story['photo_sha256']
                    or binding.get('generation') != story['_identity_generation']
                    or receipt.get('provider_id') != 'codex_native'):
                continue
            receipts.append(receipt)
            for call in receipt.get('native_tool_calls', {}).values():
                if call.get('phase') != 'completed':
                    continue
                value = call.get('result') or {}
                if (call.get('input') or {}).get('name') == 'websearch':
                    queries.append({'query': (call['input'].get('arguments') or {}).get('query'),
                        'cursor': (call['input'].get('arguments') or {}).get('cursor'),
                        'original_attempt_id': binding.get('attempt_id')})
                    for source in value.get('sources', []):
                        leads.append({'title': source.get('title'), 'url': source.get('url'),
                            'original_attempt_id': binding.get('attempt_id'), 'kind': 'unverified_search_lead'})
            for discovery in receipt.get('native_discovery') or []:
                action = discovery.get('action') or {}
                if action.get('type') == 'search':
                    for query in action.get('queries') or ([action['query']] if action.get('query') else []):
                        queries.append({'query': query, 'cursor': None,
                            'original_attempt_id': binding.get('attempt_id'), 'discovery_provider': 'native_hosted'})
                for source in discovery.get('results') or []:
                    if isinstance(source.get('url'), str):
                        leads.append({'title': source.get('title'), 'url': source['url'],
                            'original_attempt_id': binding.get('attempt_id'), 'kind': 'unverified_search_lead',
                            'discovery_provider': 'native_hosted'})
            prior.append(binding.get('attempt_id'))
        context['prior_search_leads'] = _distinct_discovery_context(leads)
        context['prior_search_queries'] = _distinct_discovery_context(queries)
        context['prior_read_outcomes'] = [dict(row) for row in db.execute(
            'SELECT s.url,s.status,s.error_code,s.source_version_id,r.run_id FROM research_run_sources s '
            'JOIN research_runs r ON r.run_id=s.run_id WHERE r.story_id=? AND r.identity_generation=? '
            "ORDER BY s.updated_at", (story['id'], story['_identity_generation']))]
        context['prior_native_attempt_ids'] = prior
        from .native_source_memory import source_memory
        context['source_memory'] = source_memory(db, receipts, context['known'])
    conflicts, cursor = [], None
    while True:
        page = conflict_journal(service, story['id'], cursor)
        for item in page['items']:
            # A conflict must keep both claims and every decision/revision,
            # but does not need to repeat each entire article yet again.
            for side in ('left', 'right'):
                item[side]['sources'] = _context_source_refs(item[side]['sources'])
            conflicts.append(item)
        if not page['has_more']:
            break
        cursor = page['next_cursor']
    context['conflict_journal'] = conflicts
    return context


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
        plan = route_plan(_healthy_routes(adapter, available))
        if not plan:
            _finish(adapter, job, run_id, headless, epoch, 'research_routes_unavailable', pending=True)
            return {'outcome': 'resource_blocked', 'reason': 'research_routes_unavailable'}
        context = agent_context(research['visual_identity'], goal, run_id, adapter.service.store.now())
        if any(route.provider_id == 'codex_native' for route in plan):
            context = native_agent_context(adapter.service, story, research['visual_identity'], goal, run_id)
        saved = {'pipeline': QUALIFICATION_KEY, 'route_ids': [r.route_id for r in plan],
            'primary_count': (1 if plan[0].provider_id == 'codex_native'
                else min(2, len({r.provider_id for r in plan}))),
            'provider_availability': 'independent' if len({r.provider_id for r in plan}) > 1 else 'reduced',
            'context': context,
            'routes': {}}
        saved = _persist_plan(adapter, job, run_id, headless, epoch, initial=saved)
    context = saved['context']

    def guard():
        if headless._snapshot(job, run_id, epoch) is None:
            from .service import ConflictError
            raise ConflictError('agent_research_stale', 'Owner selection or Stop changed.')

    async def admit(receipt, agent):
        outcomes = []
        for candidate in receipt['result']['facts']:
            guard()
            outcomes.append(await adapter.service.confirm_agent_fact(job, run_id, candidate, agent))
        fact_ids = {row['fact_id'] for row in outcomes if row.get('fact_id')}
        with adapter.service.store.connection() as db:
            eligible = {row[0] for row in db.execute(
                "SELECT assertion_id FROM fact_assertions WHERE story_id=? AND eligibility='eligible'",
                (job['story_id'],))} & fact_ids
        return {'phase': 'completed', 'attempt_id': agent['attempt_id'], 'outcomes': outcomes,
            **_source_coverage(receipt), 'candidate_count': len(outcomes),
            'eligible_fact_ids': sorted(eligible), 'supported_count': len(eligible)}

    async def retained(route_id, state):
        """Extract the same owned cohort once before asking another discovery route."""
        if state.get('provider_id') == 'codex_native':
            # Native continuation/readback belongs to the original Luna family.
            # A host coverage heuristic must not launch another model over it.
            return state
        from .fact_agent_providers import retained_source_cohort, run_retained_source_agent
        continuation = state.get('retained') or {}
        with adapter.service.store.connection() as db:
            waits = retained_retry_waits(db, job, {'context': context, 'routes': {route_id: state}},
                photo_sha256=story['photo_sha256'], generation=story['_identity_generation'], control_revision=epoch)
            exhausted = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                (continuation.get('attempt_id'),)).fetchone()
            if exhausted:
                from .research_adapter import MAX_RETAINED_ADMISSION_ATTEMPTS, retained_admission_count, retained_not_sent_retry_at
                exhausted = json.loads(exhausted[0])
                if (retained_not_sent_retry_at(db, exhausted, enforce_limit=False) is not None
                        and retained_admission_count(db, exhausted) >= MAX_RETAINED_ADMISSION_ATTEMPTS):
                    state = {**state, 'retained': {**continuation, 'phase': 'failed',
                        'reason': 'research_agent_retained_admission_exhausted',
                        'admission_attempts': MAX_RETAINED_ADMISSION_ATTEMPTS}}
                    persisted = _persist_plan(adapter, job, run_id, headless, epoch,
                        route_id=route_id, state=state, recovery=True)
                    saved['routes'][route_id] = persisted['routes'][route_id]
                    return saved['routes'][route_id]
        unsent = next(iter(waits), None)
        if unsent and unsent['retry_at'] > adapter.service.store.now():
            return state
        if (continuation.get('phase') == 'failed' and not unsent or continuation.get('phase') == 'unknown'
                or continuation.get('phase') == 'completed'
                and not any(row.get('status') == 'unverified' for row in continuation.get('outcomes', []))):
            return state
        if (state.get('phase') not in {'failed', 'unknown'}
                and not state.get('unproposed_source_version_ids') and not state.get('rejected_candidate_count')):
            return state
        with adapter.service.store.connection() as db:
            row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                (state.get('attempt_id'),)).fetchone()
        original = json.loads(row[0]) if row else {}
        try:
            cohort = retained_source_cohort(adapter, original, story=story, context=context, guard=guard)
        except ValueError as exc:
            state = {**state, 'retained': {'phase': 'failed', 'reason': str(exc)}}
            persisted = _persist_plan(adapter, job, run_id, headless, epoch,
                route_id=route_id, state=state, recovery=True)
            saved['routes'][route_id] = persisted['routes'][route_id]
            return saved['routes'][route_id]
        if cohort is None:
            return state
        if continuation.get('extraction_route_id'):
            route = routes_by_id.get(continuation['extraction_route_id'])
        else:
            original_provider = state.get('provider_id') or original.get('provider_id') or (routes_by_id[route_id].provider_id
                if route_id in routes_by_id else (original.get('binding') or {}).get('provider_id'))
            unresolved = state.get('phase') == 'unknown' or state.get('original_send_unresolved')
            choices = [route for route in available if route.provider_id != 'opencode'
                and (not unresolved or original_provider and route.provider_id != original_provider)]
            choices.sort(key=lambda route: (state.get('phase') == 'failed' and route.provider_id == original_provider,
                route.route_id != route_id))
            route = next(iter(_healthy_routes(adapter, choices)), None)
        if route is None:
            return state
        state = {**state, 'retained': {**continuation, 'phase': continuation.get('phase', 'created'),
            'extraction_route_id': route.route_id, 'source_cohort_sha256': cohort['source_cohort_sha256']}}
        persisted = _persist_plan(adapter, job, run_id, headless, epoch,
            route_id=route_id, state=state, recovery=True)
        state = saved['routes'][route_id] = persisted['routes'][route_id]
        unit = {'run_id': run_id, 'context': context, 'route_id': route.route_id, 'original_route_id': route_id,
            'original_attempt_id': cohort['original_attempt_id'],
            'source_cohort_sha256': cohort['source_cohort_sha256']}
        digest = hashlib.sha256(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        owned = {**story, '_fact_pool_unit_id': 'agent_retained_' + digest[:24], '_fact_pool_input_sha256': digest}
        role = 'facts_agent_retained_' + route.route_id
        try:
            async def invoke(binding):
                return await run_retained_source_agent(adapter, route, owned, context, cohort,
                    original_receipt=original, binding=binding, guard=guard)
            result = await adapter.run(owned, role, unit, invoke, client=route)
            receipt = result['receipt']
            if receipt.get('phase') != 'completed':
                raise RetryableProviderError('research_agent_retained_' + str(receipt.get('phase', 'unknown')))
            agent = {'route_id': route.route_id, 'provider_id': route.provider_id, 'model_id': route.model_id,
                'attempt_id': receipt['binding']['attempt_id'], 'prompt_sha256': receipt.get('prompt_sha256'),
                'original_attempt_id': cohort['original_attempt_id'],
                'retained_source_cohort_sha256': cohort['source_cohort_sha256']}
            continuation = await admit(receipt, agent)
        except RetryableProviderError as exc:
            logical = hashlib.sha256(json.dumps([story['id'], story['photo_sha256'],
                story['_identity_generation'], role, unit], ensure_ascii=False,
                separators=(',', ':'), sort_keys=True).encode()).hexdigest()
            with adapter.service.store.connection() as db:
                row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE logical_id=? '
                    'ORDER BY created_at DESC,rowid DESC LIMIT 1', (logical,)).fetchone()
            receipt = json.loads(row[0]) if row else {}
            continuation = {'phase': 'failed' if receipt.get('phase') in {'created', 'failed', 'completed'} else 'unknown',
                'reason': str(exc), 'attempt_id': (receipt.get('binding') or {}).get('attempt_id'),
                **_source_coverage(receipt)}
            with adapter.service.store.connection() as db:
                from .research_adapter import MAX_RETAINED_ADMISSION_ATTEMPTS, retained_admission_count, retained_not_sent_retry_at
                retry_at = retained_not_sent_retry_at(db, receipt)
                if (retained_not_sent_retry_at(db, receipt, enforce_limit=False) is not None
                        and retained_admission_count(db, receipt) >= MAX_RETAINED_ADMISSION_ATTEMPTS):
                    continuation.update(reason='research_agent_retained_admission_exhausted',
                        admission_attempts=MAX_RETAINED_ADMISSION_ATTEMPTS)
            if retry_at is not None:
                continuation.update(retry_at=retry_at, code=receipt['route_failure']['code'], send_state='not_sent')
        state = {**state, 'retained': {**continuation, 'extraction_route_id': route.route_id,
            'source_cohort_sha256': cohort['source_cohort_sha256']}}
        persisted = _persist_plan(adapter, job, run_id, headless, epoch,
            route_id=route_id, state=state, recovery=True)
        saved['routes'][route_id] = persisted['routes'][route_id]
        return saved['routes'][route_id]

    async def one_step(route_id):
        guard()
        state = saved['routes'].get(route_id, {})
        context = state.get('operation_context') or saved['context']
        lineage = {key: state[key] for key in ('operation_context', 'history', 'continuation_keys') if key in state}
        route = routes_by_id.get(route_id)
        if not state:
            recovered = _recover_route_state(adapter, job, run_id, story, context, route_id)
            if recovered is not None:
                if not recovered.get('provider_id') and route:
                    recovered['provider_id'] = route.provider_id
                persisted = _persist_plan(adapter, job, run_id, headless, epoch, route_id=route_id, state=recovered)
                state = saved['routes'][route_id] = persisted['routes'][route_id]
                LOG.info('street_story_fact_agent_state_recovered story_id=%s run_id=%s route_id=%s '
                    'attempt_id=%s original_phase=%s unresolved=%s', story['id'], run_id, route_id,
                    state['attempt_id'], state.get('original_phase'), state.get('original_send_unresolved'))
        if state.get('phase') in {'completed', 'failed'}:
            recovered = await _recover_confirmations(adapter, job, run_id, story, context, route_id, state, guard)
            if recovered is not None:
                persisted = _persist_plan(adapter, job, run_id, headless, epoch,
                    route_id=route_id, state=recovered, recovery=True)
                state = saved['routes'][route_id] = persisted['routes'][route_id]
                LOG.info('street_story_fact_agent_confirmation_recovered story_id=%s run_id=%s route_id=%s '
                    'attempt_id=%s supported=%s pending=%s', story['id'], run_id, route_id, state['attempt_id'],
                    state.get('supported_count', 0),
                    sum(row.get('status') == 'unverified' for row in state.get('outcomes', [])))
            return await retained(route_id, state)
        if state.get('phase') == 'unknown' and (route is None or route.provider_id not in {'opencode', 'codex_native'}):
            # No blind retry of a failed/unknown synchronous operation.
            return await retained(route_id, state)
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
                state = await admit(receipt, agent)
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
                    'reason': str(exc), 'attempt_id': (receipt.get('binding') or {}).get('attempt_id'),
                    **_source_coverage(receipt)}
        state = {**state, **lineage, **({'provider_id': route.provider_id} if route else {})}
        persisted = _persist_plan(adapter, job, run_id, headless, epoch, route_id=route_id, state=state)
        state = saved['routes'][route_id] = persisted['routes'][route_id]
        LOG.info('street_story_fact_agent run_id=%s route_id=%s phase=%s candidates=%s',
            run_id, route_id, state['phase'], state.get('candidate_count', 0))
        return await retained(route_id, state)

    async def one(route_id):
        # Each closed packet is admitted before the model's next action. New
        # turns stay in its existing thread and this same durable job/run.
        # Neither a fact count nor a continuation count decides saturation.
        while True:
            result = await one_step(route_id)
            completion = result.get('native_completion') or {}
            action = completion.get('next_action')
            unresolved = [row for row in result.get('outcomes', []) if row.get('status') == 'unverified']
            # A closed native packet with a rejected own-source reference has
            # no outstanding judge send. Return the actual mechanical rejection
            # to the model; only its next guarded assessment can repair it.
            closed_reference_errors = bool(unresolved) and all(
                row.get('reason') == 'native_fact_source_not_owned_read' and not row.get('receipt')
                and not row.get('confirmation') for row in unresolved)
            if (result.get('provider_id') != 'codex_native' or result.get('phase') != 'completed'
                    or completion.get('research_sufficient') or not action or action.get('kind') == 'ask_owner'
                    or unresolved and not closed_reference_errors):
                return result
            guard()
            with adapter.service.store.connection() as db:
                row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                    (result.get('attempt_id'),)).fetchone()
                parent = json.loads(row[0]) if row else {}
                # Stable newly observed inputs; repeating an unchanged action
                # is an explicit blocked loop, never "material exhausted".
                knowledge = [dict(row) for row in db.execute('SELECT assertion_id,revision_digest FROM fact_assertions '
                    'WHERE story_id=? ORDER BY assertion_id', (story['id'],))]
            if (parent.get('phase') != 'completed' or parent.get('provider_send_state') != 'response_closed'
                    or parent.get('native_terminal_status') != 'completed'):
                return result
            from .native_fact_contract import native_parent_digest, load_native_parent
            reads = sorted({(doc.get('source_version_id'), doc.get('content_sha256'), chunk.get('chunk_id'))
                for call in parent.get('native_tool_calls', {}).values() if call.get('phase') == 'completed'
                for doc in (call.get('result') or {}).get('documents', []) for chunk in doc.get('chunks', [])})
            failures = sorted({(attempt.get('requested_url'), attempt.get('status'), attempt.get('error_code'))
                for call in parent.get('native_tool_calls', {}).values() if call.get('phase') == 'completed'
                for attempt in (call.get('result') or {}).get('read_attempts', [])})
            progress_key = hashlib.sha256(json.dumps([action, knowledge, reads, failures],
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            keys = result.get('continuation_keys') or []
            if progress_key in keys:
                state = {**result, 'continuation_blocker': 'same_action_without_new_input'}
                _persist_plan(adapter, job, run_id, headless, epoch, route_id=route_id, state=state, recovery=True)
                LOG.warning('street_story_native_continuation run_id=%s attempt_id=%s status=blocked reason=same_action_without_new_input',
                    run_id, result['attempt_id'])
                return state
            pointer = {'original_attempt_id': result['attempt_id'], 'thread_id': parent['thread_id'],
                'parent_turn_id': parent['turn_id'], 'next_action': action, 'parent_sha256': native_parent_digest(parent)}
            next_context = native_agent_context(adapter.service, story, research['visual_identity'], goal, run_id)
            next_context['native_continuation'] = pointer
            if closed_reference_errors:
                next_context['native_continuation_diagnostics'] = {
                    'original_attempt_id': result['attempt_id'],
                    'kind': 'closed_local_source_reference_rejection_not_provider_unknown',
                    'outcomes': unresolved,
                    'meaning': 'Rejected references were not admitted. Correct them only from actual own frozen reads; '
                        'the host has not repaired evidence or accepted the unsupported claim.'}
            with adapter.service.store.connection() as db:
                load_native_parent(db, pointer, binding=parent['binding'], context=next_context)
            state = {'phase': 'created', 'provider_id': 'codex_native', 'parent_attempt_id': result['attempt_id'],
                'operation_context': next_context, 'continuation_keys': [*keys, progress_key],
                'history': [*(result.get('history') or []), {key: value for key, value in result.items()
                    if key not in {'history', 'continuation_keys'}}]}
            persisted = _persist_plan(adapter, job, run_id, headless, epoch, route_id=route_id,
                state=state, continuation=True)
            saved['routes'][route_id] = persisted['routes'][route_id]
            LOG.info('street_story_native_continuation run_id=%s parent_attempt_id=%s thread_id=%s action=%s status=ready',
                run_id, result['attempt_id'], parent['thread_id'], action['kind'])

    ids = saved['route_ids']
    primary_count = saved.get('primary_count', min(2, len(ids)))
    tasks = {asyncio.create_task(one(route_id)) for route_id in ids[:primary_count]}
    unused, outcomes = iter(ids[primary_count:]), []
    try:
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = task.result()
                outcomes.append(result)
                if _coverage_gaps(result):
                    # Native work retains its original Luna/Sol family. The
                    # existing non-native read-only pool may still do bounded
                    # independent work while an exact original is observed;
                    # unknown_providers below excludes that original resource.
                    if (result.get('provider_id') == 'codex_native'
                            and (result.get('phase') == 'unknown' or result.get('original_send_unresolved')
                                 or (result.get('retained') or {}).get('phase') == 'unknown')):
                        continue
                    if (result.get('provider_id') == 'codex_native'
                            and result.get('phase') == 'completed'
                            and (result.get('native_completion') or {}).get('stop_basis') != 'resource_unavailable'):
                        continue  # The owned model's next action governs its continuation.
                    unknown_providers = {part.get('provider_id') or (routes_by_id[part_id].provider_id
                        if part_id in routes_by_id else None)
                        for original_id, original in saved['routes'].items()
                        for part_id, part in ((original_id, original),
                            ((original.get('retained') or {}).get('extraction_route_id'), original.get('retained') or {}))
                        if part.get('phase') == 'unknown' or part.get('original_send_unresolved')}
                    for route_id in unused:
                        route = routes_by_id.get(route_id)
                        if (route is not None and route.provider_id not in unknown_providers
                                and _healthy_routes(adapter, [route])):
                            tasks.add(asyncio.create_task(one(route_id)))
                            break
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    assessments = [part for result in outcomes for part in (result, result.get('retained') or {})]
    pending = any(result.get('phase') == 'unknown' or result.get('original_send_unresolved')
        or any(outcome.get('status') == 'unverified'
        for outcome in result.get('outcomes', [])) for result in assessments)
    failed = any(result.get('phase') == 'failed' for result in outcomes)
    gaps = sorted({gap for result in outcomes for gap in _coverage_gaps(result)})
    exhausted = any((row.get('retained') or {}).get('reason') == 'research_agent_retained_admission_exhausted'
        for row in outcomes)
    with adapter.service.store.connection() as db:
        waits = retained_retry_waits(db, job, saved, photo_sha256=story['photo_sha256'],
            generation=story['_identity_generation'], control_revision=epoch)
    future_waits = [row for row in waits if row['retry_at'] > adapter.service.store.now()]
    outcome = _finish(adapter, job, run_id, headless, epoch,
        'research_agent_retained_admission_exhausted' if exhausted else
        'research_agent_retained_waiting' if future_waits else
        'research_agent_original_pending' if pending else 'research_agent_route_failed' if failed
            else 'research_agent_content_gaps' if gaps else 'research_agent_completed',
        pending=pending or failed or bool(gaps), content_gaps=gaps,
        provider_availability=saved.get('provider_availability'), retained_waits=waits)
    if future_waits:
        # The existing worker keeps the same job/run at the last outstanding
        # refusal deadline; provider admission remains mandatory after waking.
        raise RetryableProviderError('research_agent_retained_waiting',
            retry_at=max(adapter.service.store.now()+1, *(row['retry_at'] for row in waits)))
    if pending:
        with adapter.service.store.connection() as db:
            addressed = db.execute("SELECT 1 FROM research_provider_attempts WHERE story_id=? "
                "AND role LIKE 'facts_agent_%' AND json_extract(receipt_json,'$.binding.job_id')=? "
                "AND json_extract(receipt_json,'$.phase') NOT IN ('created','completed','failed','aborted') "
                "AND ((json_extract(receipt_json,'$.session_id') IS NOT NULL "
                "AND json_extract(receipt_json,'$.message_id') IS NOT NULL) OR "
                "(json_extract(receipt_json,'$.thread_id') IS NOT NULL "
                "AND json_extract(receipt_json,'$.turn_id') IS NOT NULL)) LIMIT 1", (story['id'], job['id'])).fetchone()
        if addressed:
            raise RetryableProviderError('research_agent_original_pending', retry_at=adapter.service.store.now()+30)
    return outcome


def _finish(adapter, job, run_id, headless, epoch, reason, *, pending, content_gaps=None,
            provider_availability=None, retained_waits=None):
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
        if content_gaps:
            value['content_gaps'] = content_gaps
        if provider_availability:
            value['provider_availability'] = provider_availability
        if retained_waits:
            value.update(retained_waits=retained_waits, retry_at=max(row['retry_at'] for row in retained_waits))
        research['research_outcome'] = value
        unavailable = reason == 'research_routes_unavailable'
        set_run_state(db, run_id, 'failed' if unavailable else 'partial' if pending else 'completed', detail=reason,
            now=adapter.service.store.now(), completed=not pending or unavailable)
        db.execute('UPDATE stories SET research_json=?,state=CASE WHEN draft_text IS NULL THEN ? ELSE state END,updated_at=? WHERE id=?',
            (canonical(research), 'facts_ready', adapter.service.store.now(), story['id']))
    adapter.service.store.checkpoint_put(job['id'], 'headless_fact_outcome:'+run_id, value)
    return value
