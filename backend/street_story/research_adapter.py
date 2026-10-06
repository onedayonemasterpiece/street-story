"""Product bindings for existing DevCoveer OpenCode with shared admission.

Logical attempts persist in the product DB. No coding-agent task endpoint,
credential hopping or new POI/job system is involved.
"""
from __future__ import annotations
import hashlib
import json
import logging
import math
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from .opencode_research import OpenCodeResearch, ResearchUnavailable
from .errors import PermanentProviderError, RetryableProviderError, research_retry_at
from .service import ConflictError, canonical
from .config import reveal

LOG = logging.getLogger(__name__)


def _failure_code(exc):
    value = getattr(exc, 'code', None) or str(exc)
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value) else type(exc).__name__


def _closed_malformed_visual(receipt):
    """All sends produced complete responses; malformed content is not timeout."""
    attempts = receipt.get('model_attempts') if isinstance(receipt, dict) else None
    if not isinstance(attempts, list) or not attempts:
        return False
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get('category') != 'malformed_response':
            return False
        request_id, usage = attempt.get('provider_request_id'), attempt.get('usage')
        if not isinstance(request_id, str) or not request_id.strip() or not isinstance(usage, dict):
            return False
        total = usage.get('total_tokens')
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            return False
    return True


FACT_PAGE_SCHEMA = {'type':'object','properties':{
    'research_sufficient':{'type':'boolean'},
    'next_research_query':{'type':'string','maxLength':500},
    'next_research_goal':{'type':'string','maxLength':1000},
    'source_matches_poi':{'type':'boolean'},'source_content_valid':{'type':'boolean'},
    'continuation_needed':{'type':'boolean'},'facts':{'type':'array','maxItems':32,'items':{
        'type':'object','properties':{
            'text':{'type':'string','minLength':1,'maxLength':1200},'claim_key':{'type':'string'},
            'existing_fact_id':{'type':'string'},'confidence':{'type':'number','minimum':0,'maximum':1},
            'source_refs':{'type':'array','items':{'type':'string'}},
            'passage_ids':{'type':'array','minItems':1,'items':{'type':'integer'}},
            'verdict':{'enum':['supported','insufficient','contradicted','possible_conflict']},
            'atomic':{'type':'boolean'},'support_complete':{'type':'boolean'},'qualifiers_preserved':{'type':'boolean'},
            'review_reason':{'type':'string','minLength':1,'maxLength':500}},
        'required':['text','claim_key','existing_fact_id','confidence','passage_ids','verdict','atomic','support_complete','qualifiers_preserved','review_reason']}}},
    'required':['facts','source_matches_poi','source_content_valid','continuation_needed']}


def fact_page_capsule(page, context):
    """One semantic index, with intact claims and authorized frozen paging.

    Runtime identity receipts and repeated evidence DTOs stay in the ledger.
    Their absence from a prompt does not truncate claims or their qualifiers.
    """
    public = {key: value for key, value in context.items()
              if key not in {'_known_fact_inventory', 'known_facts', 'prior_poi_facts'}}
    identity = public.get('confirmed_identity')
    if isinstance(identity, dict):
        public['confirmed_identity'] = {key: identity[key] for key in (
            'status', 'candidate_id', 'candidate_name', 'candidate_url', 'wikipedia_url', 'wikidata', 'osm_id')
            if key in identity}
    if isinstance(public.get('previously_processed_sources'), list):
        public['previously_processed_sources'] = [{key: source[key] for key in (
            'url', 'title', 'extraction_coverage') if key in source}
            for source in public['previously_processed_sources'] if isinstance(source, dict)]
    inventory = context.get('_known_fact_inventory', context.get('known_facts', []))
    if not isinstance(inventory, list):
        raise PermanentProviderError('research_fact_inventory_invalid')
    by_id = {}
    for fact in [*inventory, *(context.get('prior_poi_facts') or [])]:
        if not isinstance(fact, dict):
            continue
        item = {key: fact[key] for key in ('fact_id', 'text', 'claim_key', 'qualifiers', 'eligibility') if key in fact}
        by_id.setdefault(str(fact.get('fact_id') or canonical(item)), item)
    complete = list(by_id.values())
    first, size = [], 0
    for item in complete:
        item_size = len(canonical(item).encode('utf-8'))
        if size + item_size > 12000:
            break
        first.append(item)
        size += item_size
    public.update(known_inventory_complete=len(first) == len(complete),
                  known_inventory_total=len(complete), known_inventory_next_offset=len(first),
                  known_inventory_omitted_count=len(complete) - len(first))
    return {'context': public, '_known_fact_inventory': complete, 'known_fact_inventory': first,
            'sources': [{'source_version_id': page['source_version_id'], 'url': page['source_url'],
                         'title': page.get('source_title', ''), 'passages': [
                             {'passage_id': p['passage_id'], 'text': p['text']} for p in page['evidence_passages']]}]}


class ProductResearchAdapter:
    def __init__(self, service, *, admission=None, client=None):
        self.service = service
        self._active_binding = ContextVar('street_story_research_binding', default=None)
        self.control = None
        if admission is None:
            from ai_resource_control.client import Config, Control
            from ai_resource_control.workload import WorkloadAdmission
            self.control = Control(Config.from_env('street-story'))
            admission = WorkloadAdmission(self.control, 'opencode:street-story-research')
        self.client = None
        if service.settings.research_endpoint:
            if client is not None:
                self.client = OpenCodeResearch(service.settings.research_endpoint,
                    model_id=service.settings.research_model, admission=self.fenced_admission(admission),
                    checkpoint=self.checkpoint, client=client)
            elif service.settings.research_directory:
                from .shared_devcoveer_research import SharedDevCoveerResearch
                self.client = SharedDevCoveerResearch(str(service.settings.research_directory),
                    model_id=service.settings.research_model, admission=self.fenced_admission(admission), checkpoint=self.checkpoint)
        from .headless_vision import HeadlessVisionProvider
        self.primary_vision = HeadlessVisionProvider(service)
        self.native_vision = None
        if service.settings.native_vision_reserve:
            from ai_resource_control.workload import WorkloadAdmission
            from .native_vision import NativeVisionProvider, ACCOUNT_SCOPE
            self.native_vision = NativeVisionProvider(service, admission=self.fenced_admission(WorkloadAdmission(self.control, ACCOUNT_SCOPE)),
                checkpoint=self.checkpoint)
        self.giga = None
        if reveal(service.settings.research_gigachat_key):
            from ai_resource_control.workload import WorkloadAdmission
            from .gigachat_research import GigaChatResearchClient, ACCOUNT_SCOPE
            self.giga = GigaChatResearchClient(reveal(service.settings.research_gigachat_key).removeprefix('Basic '),
                admission=self.fenced_admission(WorkloadAdmission(self.control,ACCOUNT_SCOPE)), credential_ref='STREET_STORY_GIGACHAT_KEY',
                ca_bundle_file=service.settings.research_gigachat_ca, scope=service.settings.research_gigachat_scope)

    async def checkpoint(self, binding, receipt):
        with self.service.store.tx() as db:
            db.execute('UPDATE research_provider_attempts SET receipt_json=?,updated_at=? WHERE attempt_id=?',
                (canonical(receipt), self.service.store.now(), binding['attempt_id']))

    def guard_binding(self, binding):
        if not binding or not binding.get('story_id'):
            return
        from .research_control import research_stopped
        with self.service.store.connection() as db:
            story = self.service._story_row(db, binding['story_id'])
            research = json.loads(story['research_json'] or '{}')
            generation = int(research.get('identity_generation') or 0)
            purpose = binding.get('purpose', 'identity')
            control = (research.get('research_controls') or {}).get(purpose) or {}
            visual = binding.get('visual_scope') is True
            stopped = ((bool(control.get('stopped') and control.get('identity_generation') == generation)
                        if control else bool(research.get('identity_research_cancelled')))
                       if visual else research_stopped(research, purpose,
                            photo_sha256=story['photo_sha256'], identity_generation=generation))
            if ((not visual and binding.get('photo_sha256') != story['photo_sha256']) or binding.get('generation') != generation
                    or stopped
                    or binding.get('control_revision', 0) != int(control.get('revision') or 0)):
                raise ConflictError('research_scope_superseded', 'Исследование остановлено или относится к предыдущему объекту.')
            if binding.get('job_id') and not db.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND attempts=?",
                                                       (binding['job_id'], binding.get('job_attempt'))).fetchone():
                raise ConflictError('research_worker_superseded', 'Этот запуск исследования больше не владеет задачей.')

    def fenced_admission(self, admission):
        adapter = self

        @asynccontextmanager
        async def admitted(binding, workload):
            owned = binding if binding.get('story_id') else adapter._active_binding.get()
            async with admission(binding, workload) as lease:
                class FencedLease:
                    async def before_send(self, metadata):
                        adapter.guard_binding(owned)
                        return await lease.before_send(metadata)
                    async def finalize(self, metadata, state):
                        return await lease.finalize(metadata, state)
                yield FencedLease()
        return admitted

    async def close(self):
        if self.native_vision is not None:
            await self.native_vision.close()

    def attempt(self, story, role, unit):
        visual = role.startswith('vision')
        identity = [story['id'], story.get('_identity_generation', 0), role, unit] if visual else [
            story['id'], story['photo_sha256'], story.get('_identity_generation', 0), role, unit]
        logical = hashlib.sha256(canonical(identity).encode()).hexdigest()
        with self.service.store.tx() as db:
            rows = list(db.execute('SELECT * FROM research_provider_attempts WHERE logical_id=? ORDER BY created_at DESC', (logical,)))
            old = json.loads(rows[0]['receipt_json']) if rows else {}
            if (role in {'vision_google_group', 'vision_google_pair'} and old.get('phase') == 'failed'
                    and old.get('fallback_mode') in {'pair', 'native'} and old.get('retry_safe') is True
                    and (role == 'vision_google_group' or any(
                        item.get('category') == 'malformed_response'
                        for item in (old.get('observation') or {}).get('model_attempts', [])))):
                # This exact group has a closed result/known local refusal. A
                # smaller pair is a different unit; never repeat this group.
                if role == 'vision_google_pair':
                    from .gemini import GeminiUnavailable
                    raise GeminiUnavailable(self.service.store.now()+300, 'visual_pair_known_failure')
                raise PermanentProviderError('research_visual_group_pair_required')
            if rows and old.get('phase') == 'completed':
                return None, old
            resumed = {**(old.get('binding') or {}), **{k: old[k] for k in
                ('session_id', 'message_id', 'thread_id', 'turn_id', 'profile_verified', 'phase', 'quota_permission') if k in old}}
            if rows and old.get('phase') == 'created':
                resumed.update(control_revision=story.get('_fact_research_control_revision', story.get('_identity_research_control_revision', 0)),
                               job_id=story.get('_research_job_id'), job_attempt=story.get('_research_job_attempt'))
                return resumed, None
            # A timeout alone does not authorize another model request. Require
            # acknowledged abort; unaddressable creation/submit stays waiting.
            if rows and old.get('phase') not in {'created', 'aborted', 'failed'}:
                return resumed, None
            if rows and old.get('phase') == 'aborted' and not old.get('abort_acknowledged'):
                raise RetryableProviderError('research_attempt_unknown', retry_at=self.service.store.now()+60)
            attempt = 'rattempt_' + hashlib.sha256(f'{logical}:{len(rows)}'.encode()).hexdigest()[:24]
            binding = {'attempt_id': attempt, 'story_id': story['id'], 'request_id': logical,
                **({'visual_scope': True} if visual else {'photo_sha256': story['photo_sha256']}),
                'generation': story.get('_identity_generation', 0),
                'attempt_created_at': self.service.store.now(),
                'purpose': 'identity' if visual else ('facts' if role.startswith('facts') or '_fact_research_control_revision' in story else 'identity'),
                'control_revision': story.get('_fact_research_control_revision', story.get('_identity_research_control_revision', 0)),
                'job_id': story.get('_research_job_id'), 'job_attempt': story.get('_research_job_attempt')}
            now = self.service.store.now()
            db.execute('INSERT OR IGNORE INTO research_provider_attempts(attempt_id,logical_id,story_id,role,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                (attempt, logical, story['id'], role, canonical({'binding': binding, 'phase': 'created'}), now, now))
            return binding, None

    async def run(self, story, role, unit, invoke):
        if self.client is None:
            raise RetryableProviderError('research_opencode_unconfigured',retry_at=self.service.store.now()+300)
        route_key = 'research-route-health:' + hashlib.sha256(canonical([
            self.client.endpoint, self.client.model_id, getattr(self.client, 'directory', None),
            getattr(self.client, 'profile_fingerprint', None)]).encode()).hexdigest()
        quota_key = 'research-quota-health:' + self.client.provider_id + ':' + self.client.model_id
        binding, saved = self.attempt(story, role, unit)
        if saved:
            return {'result': saved.get('result'), 'sources': saved.get('sources', []), 'receipt': saved}
        readback = (binding.get('session_id') and binding.get('message_id')
                    and binding.get('phase') in {'prompt_intent', 'submitted', 'abort_intent', 'aborted', 'abort_outcome_unknown'})
        # Existing receipt metadata is the durable wait fence. An explicit
        # Resume changes the owner epoch and permits one new admission probe;
        # it never authorizes a model send without the shared resource grant.
        wait_scope = hashlib.sha256(canonical([route_key, story['id'], None if role.startswith('vision') else story['photo_sha256'],
            story.get('_identity_generation', 0), binding.get('control_revision', 0)]).encode()).hexdigest()
        with self.service.store.connection() as db:
            row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                             (binding['attempt_id'],)).fetchone()
        prior_failure = (json.loads(row['receipt_json'] or '{}').get('route_failure') or {}) if row else {}
        if (not readback and prior_failure.get('requires_binding_change')
                and prior_failure.get('wait_scope') == wait_scope):
            raise RetryableProviderError(prior_failure['code'], retry_at=research_retry_at(
                prior_failure['code'], self.service.store.now(), self.service.store.now()+3600))
        for health_key in (() if readback else (route_key, quota_key)):
            health = self.service.store.cache_get(health_key) or {}
            resumed_probe = (health.get('category') == 'RESOURCE_DAILY_BUDGET'
                and prior_failure.get('wait_scope') != wait_scope
                and (int(binding.get('control_revision', 0)) > 0
                     or prior_failure.get('code') == 'RESOURCE_DAILY_BUDGET' and prior_failure.get('wait_scope')))
            if health.get('retry_at', 0) > self.service.store.now() and not resumed_probe:
                raise RetryableProviderError(health.get('category','research_route_waiting'), retry_at=health['retry_at'])
        try:
            return await invoke(binding)
        except ResearchUnavailable as exc:
            status = exc.receipt.get('provider_status')
            category = ('research_provider_credential_or_eligibility' if status in {401,403} else
                        'research_provider_quota' if status == 429 else exc.code)
            # Credential/eligibility rejection keeps the existing 1h cooldown;
            # non-status unavailable routes use 5m, with longer provider hints.
            delay = 3600 if status in {401,403} else 60 if status == 429 else 300
            try:
                delay = max(delay,float(exc.receipt.get('provider_retry_after')))
            except (TypeError,ValueError):
                pass
            retry_at = research_retry_at(category, self.service.store.now(), self.service.store.now()+delay)
            binding_changed = category.endswith('binding_changed')
            self._record_route_failure(binding, role, category, retry_at,
                wait_scope=wait_scope, requires_binding_change=binding_changed)
            route_unavailable = status in {401, 403, 429} or isinstance(status, int) and status >= 500 or category.lower().endswith('_unavailable')
            if not binding_changed and route_unavailable:
                self.service.store.cache_put(quota_key if status == 429 else route_key,
                    {'category':category,'status':status,'retry_at':retry_at},
                    math.ceil(retry_at-self.service.store.now()))
            raise RetryableProviderError(category, retry_at=retry_at) from exc
        except Exception as exc:
            if getattr(exc, 'resource_failure', False):
                code = _failure_code(exc)
                retry_at = research_retry_at(code, self.service.store.now(),
                    self.service.store.now()+max(3, getattr(exc, 'retry_after_ms', 30000)/1000))
                self._record_route_failure(binding, role, code, retry_at, wait_scope=wait_scope,
                    requires_binding_change=code.lower().endswith('binding_changed'))
                # Per-binding/minute-capacity refusals may leave another smaller
                # workload healthy. Cache only shared daily/configuration waits.
                health_key = quota_key if code == 'RESOURCE_DAILY_BUDGET' else route_key if code in {
                    'RESOURCE_CONTROL_UNAVAILABLE', 'RESOURCE_POLICY_UNAVAILABLE', 'RESOURCE_UNAVAILABLE'} else None
                if health_key:
                    self.service.store.cache_put(health_key, {'category':code,'retry_at':retry_at},
                        math.ceil(retry_at-self.service.store.now()))
                raise RetryableProviderError(code, retry_at=retry_at) from exc
            raise

    def _record_route_failure(self, binding, role, code, retry_at, *, wait_scope=None, requires_binding_change=False):
        # Preserve the dispatch/recovery phase. An admission failure does not
        # prove that an older submitted request was never sent.
        with self.service.store.tx() as db:
            row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                             (binding['attempt_id'],)).fetchone()
            if row:
                receipt = json.loads(row['receipt_json'] or '{}')
                receipt['route_failure'] = {'code': code, 'retry_at': retry_at,
                                            'observed_at': self.service.store.now(),
                                            'wait_scope': wait_scope, 'requires_binding_change': requires_binding_change}
                db.execute('UPDATE research_provider_attempts SET receipt_json=?,updated_at=? WHERE attempt_id=?',
                           (canonical(receipt), self.service.store.now(), binding['attempt_id']))
        LOG.warning('street_story_research_route story_id=%s role=%s attempt_id=%s code=%s retry_at=%s',
                    binding.get('story_id'), role, binding['attempt_id'], code, retry_at)

    async def search_articles(self, query, story):
        unit = canonical([query,story.get('_research_run_id')])
        history = self.search_history(story)
        capsule = canonical({'query': query, 'purpose': 'facts' if '_fact_research_control_revision' in story else 'identity',
                             'research_history': history,
                             'visual_evidence_context': story.get('_identity_query_context', {})})
        return await self.run(story, 'search', unit, lambda binding: self.client.search_articles(capsule, binding))

    def search_history(self, story):
        """Discovery/acquisition is distinct from extraction for a given scope."""
        found, queries, completed, unfinished = {}, [], [], []
        with self.service.store.connection() as db:
            row = self.service._story_row(db, story['id'])
            research = json.loads(row['research_json'] or '{}')
            for attempt in db.execute("SELECT receipt_json FROM research_provider_attempts WHERE story_id=? AND role='search' ORDER BY updated_at", (story['id'],)):
                receipt = json.loads(attempt['receipt_json'] or '{}')
                for source in receipt.get('sources') or []:
                    if isinstance(source, dict) and source.get('url'):
                        found[source['url']] = {'url': source['url'], 'title': source.get('title', '')}
                queries.extend(call['query'] for call in receipt.get('search_calls') or [] if call.get('query'))
            for source in research.get('grounding_sources') or []:
                if isinstance(source, dict) and source.get('url'):
                    found[source['url']] = {'url': source['url'], 'title': source.get('title', '')}
            if story.get('_research_run_id'):
                for source in db.execute('SELECT url,title,status FROM research_run_sources WHERE run_id=?', (story['_research_run_id'],)):
                    found[source['url']] = {'url': source['url'], 'title': source['title']}
                    if source['status'] not in {'completed', 'failed'}:
                        unfinished.append(source['url'])
                run = db.execute('SELECT extraction_scope FROM research_runs WHERE run_id=?', (story['_research_run_id'],)).fetchone()
                if run:
                    from .poi_memory import processed_sources
                    for source in processed_sources(db, research.get('visual_identity') or {}):
                        scopes = source.get('extraction_coverage') or []
                        if any(covered.get('scope') == run['extraction_scope'] and covered.get('completed') is True
                               for covered in scopes if isinstance(covered, dict)):
                            completed.append(source['url'])
        history = {'found_sources': list(found.values()), 'completed_for_scope': completed,
                   'unfinished_sources': unfinished, 'prior_search_queries': list(dict.fromkeys(queries))}
        # Model context is a window over durable history. The omitted inventory
        # remains stored and host deduplication never treats it as forgotten.
        omitted = 0
        while len(canonical(history)) > 12000:
            largest = max(history, key=lambda key: len(canonical(history[key])))
            if not isinstance(history[largest], list) or not history[largest]:
                break
            history[largest].pop(0)
            omitted += 1
        history['omitted_history_items'] = omitted
        return history

    async def search_fact_articles(self, query, story):
        try:
            return await self.search_articles(query,story)
        except RetryableProviderError as exc:
            direct = getattr(self.service.providers.gemini,'discover_article_urls',None)
            if not callable(direct):
                raise
            try:
                found = await direct(query)
            except RetryableProviderError as fallback:
                codes = {'opencode': _failure_code(exc), 'google': _failure_code(fallback)}
                retry = [error.retry_at for error in (exc, fallback) if error.retry_at is not None]
                LOG.warning('street_story_fact_search_waiting story_id=%s routes=%s',
                            story['id'], canonical(codes))
                failure = RetryableProviderError('all_fact_search_routes_unavailable:' + ':'.join(codes.values()),
                    retry_at=min(retry) if retry else self.service.store.now()+30)
                failure.route_failures = codes
                raise failure from fallback
            return {'sources':found.grounding_sources,'receipt':{
                'provider':'gemini_google_search','backend':'google_search','independent_failure':_failure_code(exc)}}

    @property
    def facts_available(self):
        proof = self.service.store.cache_get('research-text-verification-v1') or {}
        return (self.giga is not None and proof.get('gigachat_model')=='GigaChat-2'
                and proof.get('semantic_contract_verified') is True) or self.opencode_facts_available

    @property
    def opencode_facts_available(self):
        proof = self.service.store.cache_get('research-text-verification-v1') or {}
        client = getattr(self, 'client', None)
        return (client is not None and proof.get('model_id') == client.model_id
                and proof.get('endpoint') == client.endpoint and proof.get('semantic_contract_verified') is True)

    async def _extract_opencode_page(self, capsule, page, story):
        if not self.opencode_facts_available:
            raise RetryableProviderError('research_text_fallback_unverified', retry_at=self.service.store.now()+300)
        public = {key: value for key, value in capsule.items() if key != '_known_fact_inventory'}
        # OpenCode's qualified extraction profile has no private inventory
        # paging tool: supply the complete compact index under admission.
        public['known_fact_inventory'] = capsule['_known_fact_inventory']
        public['context'] = {**public['context'], 'known_inventory_complete': True,
                             'known_inventory_omitted_count': 0}
        return await self.run(story, 'facts', page['_unit_id'], lambda binding:
                              self.client.extract_facts({**public, 'jsonschema': FACT_PAGE_SCHEMA}, binding))

    async def extract_fact_page(self, page, story, context):
        from jsonschema import Draft202012Validator
        from .errors import MalformedProviderResponse
        capsule = fact_page_capsule(page, context)
        if self.giga is None:
            return await self._extract_opencode_page(capsule, page, story)
        input_sha256=hashlib.sha256(canonical({
            'query':context.get('coverage_goal',''),'capsule':capsule,'max_tool_calls':2}).encode()).hexdigest()
        logical=hashlib.sha256(canonical([story['id'],story['photo_sha256'],
            story.get('_identity_generation',0),'facts_gigachat',page['_unit_id']]).encode()).hexdigest()
        with self.service.store.connection() as db:
            prior=[json.loads(row['receipt_json']) for row in db.execute(
                'SELECT receipt_json FROM research_provider_attempts WHERE logical_id=? ORDER BY created_at DESC,rowid DESC',
                (logical,))]
        # An unknown current send keeps its existing wait fence. A closed
        # malformed response does not authorize paying for an unchanged unit.
        unknown=bool(prior and (prior[0].get('phase') not in {'created','aborted','failed','completed'}
            or (prior[0].get('phase')=='aborted' and not prior[0].get('abort_acknowledged'))))
        repeated_closed=not unknown and not (prior and prior[0].get('phase')=='completed') and any(
            old.get('phase')=='failed' and old.get('provider_send_state')=='response_closed'
            and old.get('error_type')=='MalformedProviderResponse'
            and old.get('input_sha256')==input_sha256 for old in prior)
        if repeated_closed:
            LOG.warning('street_story_fact_closed_unit_reused story_id=%s logical_id=%s input_sha256=%s fallback=%s',
                        story['id'],logical,input_sha256,self.opencode_facts_available)
            if self.opencode_facts_available:
                return await self._extract_opencode_page(capsule,page,story)
            raise PermanentProviderError('gigachat:closed_semantic_unit_requires_live')
        binding,saved = self.attempt(story,'facts_gigachat',page['_unit_id'])
        if saved:
            return {'result':saved['result'],'receipt':saved}
        if binding.get('phase') not in {None,'created'}:
            raise RetryableProviderError('gigachat_attempt_outcome_unknown',retry_at=self.service.store.now()+300)
        receipt={'binding':binding,'phase':'created','model_id':'GigaChat-2','provider_id':'gigachat',
                 'provider_send_state':'not_sent'}
        async def before_inference(metadata):
            self.guard_binding(binding)
            receipt.update(phase='submitted', provider_send_state='possibly_sent', retry_safe=False)
            receipt.setdefault('inference_sends', []).append({key:metadata[key] for key in
                ('attempt_id','operation','purpose','estimated_input_tokens','output_allowance','images','request_body_sha256')
                if key in metadata})
            await self.checkpoint(binding,receipt)
        receipt['capsule_component_bytes'] = {key: len(canonical(value).encode('utf-8'))
            for key, value in capsule.items() if key != '_known_fact_inventory'}
        try:
            receipt['input_sha256']=input_sha256
            await self.checkpoint(binding,receipt)
            token = self._active_binding.set(binding)
            try:
                result=await self.giga.research(context.get('coverage_goal',''),capsule=capsule,
                                              max_tool_calls=2,before_inference=before_inference)
            finally:
                self._active_binding.reset(token)
            payload=dict(result['payload'])
            facts=[]
            for fact in payload['facts']:
                facts.append({**{k:v for k,v in fact.items() if k not in {'source_version_id','evidence_quotes','selected'}},
                    'source_refs':[page['source_ref']],
                    'verdict':{'unsupported':'contradicted','uncertain':'insufficient'}.get(fact['verdict'],fact['verdict'])})
            payload['facts']=facts
            receipt.update(receipts=result['receipts'],result=payload,actual_model=result.get('actual_model'),cost='unknown')
            if not Draft202012Validator(FACT_PAGE_SCHEMA).is_valid(payload):
                raise MalformedProviderResponse('gigachat:fact_page_schema_invalid')
            receipt.update(phase='completed',provider_send_state='response_closed')
            await self.checkpoint(binding,receipt)
            return {'result':payload,'receipt':receipt}
        except Exception as exc:
            not_sent = receipt['phase']=='created'
            known_closed = not_sent or isinstance(exc,MalformedProviderResponse) or getattr(exc,'status',None) is not None or getattr(exc,'resource_failure',False)
            receipt.update(phase='failed' if known_closed else 'unknown',error_type=type(exc).__name__,
                           retry_safe=known_closed,provider_send_state='not_sent' if not_sent else
                           'response_closed' if known_closed else 'possibly_sent')
            if isinstance(exc,ValueError):
                safe_codes={'gigachat:bounded_request_required','gigachat:inventory_invalid','gigachat:capsule_too_large',
                            'gigachat:frozen_sources_required','gigachat:frozen_passages_invalid'}
                receipt['error_code']=str(exc) if not_sent and str(exc) in safe_codes else 'gigachat:local_validation_failed'
            else:
                receipt['error_code']=_failure_code(exc)
            await self.checkpoint(binding,receipt)
            LOG.warning('street_story_fact_provider_failure story_id=%s attempt_id=%s provider=gigachat phase=%s not_sent=%s code=%s error_type=%s',
                        story['id'],binding['attempt_id'],receipt['phase'],not_sent,receipt['error_code'],receipt['error_type'])
            if not_sent and isinstance(exc, ValueError):
                raise PermanentProviderError(receipt['error_code']) from exc
            if known_closed and self.opencode_facts_available:
                return await self._extract_opencode_page(capsule, page, story)
            if not_sent or not known_closed or getattr(exc,'resource_failure',False):
                raise RetryableProviderError('gigachat_research_waiting',retry_at=self.service.store.now()+300) from exc
            raise

    async def compare_image(self, snapshot, story, schema, context):
        from .visual_attachments import direct_visual_parts, visual_operation_unit
        supplied = json.loads(context) if isinstance(context, str) else context
        direct_visual_parts(story, supplied)
        unit = canonical(visual_operation_unit(story, supplied))
        return await self.run(story, 'vision', unit,
            lambda binding: self.client.compare_image(story['_visual_image_parts'], binding, schema, context))

    @property
    def opencode_vision_available(self):
        receipt = self.service.store.cache_get('research-vision-verification-v1') or {}
        return (self.client is not None and receipt.get('model_id') == self.client.model_id
            and receipt.get('endpoint') == self.client.endpoint and receipt.get('positive') == 'match'
            and receipt.get('negative') == 'mismatch' and receipt.get('pixel_transport_verified') is True)

    @property
    def vision_available(self):
        return (self.primary_vision.available or self.opencode_vision_available
                or self.native_vision is not None and self.native_vision.available)

    @property
    def vision_model(self):
        routes = self.primary_vision._verified_routes()
        if routes:
            return routes[0][0]
        if self.native_vision is not None and self.native_vision.available:
            return 'gpt-6-luna'
        return self.client.model_id if self.client else 'unavailable'

    def _guard_legacy_visual_unknown(self, story):
        # Migrating away from image hashes must not orphan a previously sent
        # operation. Old unresolved units remain fenced until native readback
        # or an authoritative terminal observation closes them.
        with self.service.store.connection() as db:
            rows = db.execute("SELECT receipt_json FROM research_provider_attempts WHERE story_id=? AND role LIKE 'vision%'",
                              (story['id'],)).fetchall()
        for row in rows:
            receipt = json.loads(row['receipt_json'] or '{}')
            binding = receipt.get('binding') or {}
            if (binding.get('visual_scope') is not True
                    and binding.get('generation', 0) == story.get('_identity_generation', 0)
                    and receipt.get('phase') in {'prompt_intent', 'submitted', 'unknown',
                                                'thread_create_intent', 'session_create_intent',
                                                'abort_intent', 'abort_outcome_unknown'}):
                raise RetryableProviderError('research_visual_legacy_outcome_unknown',
                                             retry_at=self.service.store.now()+300)

    def _native_visual_readback_binding(self, story, unit):
        logical = hashlib.sha256(canonical([story['id'], story.get('_identity_generation', 0),
                                            'vision_native', unit]).encode()).hexdigest()
        with self.service.store.connection() as db:
            row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE logical_id=? ORDER BY created_at DESC LIMIT 1',
                             (logical,)).fetchone()
        receipt = json.loads(row['receipt_json'] or '{}') if row else {}
        if receipt.get('phase') not in {'prompt_intent', 'submitted', 'unknown', 'thread_create_intent'}:
            return None
        return {**(receipt.get('binding') or {}), **{key: receipt[key] for key in
                ('thread_id', 'turn_id', 'profile_verified', 'phase', 'quota_permission') if key in receipt}}

    async def visual_verdict(self, snapshot, story, schema, context):
        from .gemini import GeminiUnavailable
        from .visual_attachments import direct_visual_parts, visual_operation_unit
        supplied = json.loads(context) if isinstance(context, str) else context
        parts = direct_visual_parts(story, supplied)
        self._guard_legacy_visual_unknown(story)
        unit = canonical(visual_operation_unit(story, supplied))
        pending_native = self._native_visual_readback_binding(story, unit)
        if pending_native:
            # The original send remains bound to its saved thread/turn/quota.
            # A retry worker has a new job lease and may only read that turn.
            self.guard_binding({**pending_native,
                'job_id': story.get('_research_job_id'),
                'job_attempt': story.get('_research_job_attempt')})
            if self.native_vision is None:
                raise RetryableProviderError('native_turn_outcome_unknown', retry_at=self.service.store.now()+300)
            return await self.native_vision.compare_visual(None, story, schema, context, pending_native)
        grouped = len(parts) > 2
        failures = []
        # Always inspect the durable Google operation before availability-based
        # routing: an unknown send still blocks when the route goes unavailable.
        try:
            return await self._google_visual_verdict(None, story, schema, context, grouped=grouped)
        except GeminiUnavailable as exc:
            failures.append({'route': 'google', 'category': str(exc), 'retry_at': exc.retry_at})
        if self.native_vision is not None and self.native_vision.available:
            unit = canonical(visual_operation_unit(story, supplied))
            binding, saved = self.attempt(story, 'vision_native', unit)
            if saved:
                return {'result': saved['result'], 'receipt': saved}
            # Native reconciles its own submitted operation. A timeout never
            # authorizes another provider send.
            try:
                result = await self.native_vision.compare_visual(None, story, schema, context, binding)
                result['receipt']['availability_failures'] = failures
                return result
            except RetryableProviderError as exc:
                with self.service.store.connection() as db:
                    row = db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                                     (binding['attempt_id'],)).fetchone()
                receipt = json.loads(row['receipt_json'] or '{}') if row else {}
                if receipt.get('phase') not in {'created', 'thread_created', 'failed', 'response_completed'}:
                    raise  # Unknown native send never permits another provider.
                failures.append({'route': 'native', 'category': _failure_code(exc), 'retry_at': exc.retry_at})
        if not self.opencode_vision_available:
            due = [f['retry_at'] for f in failures if f.get('retry_at')]
            raise RetryableProviderError('research_vision_waiting', retry_at=min(due) if due else self.service.store.now()+300)
        return await self.compare_image(None, story, schema, context)

    async def _grouped_visual_verdict(self, snapshot, story, schema, context):
        """An unknown grouped send never authorizes a new pair of model calls."""
        return await self._google_visual_verdict(snapshot, story, schema, context, grouped=True)

    async def _google_visual_verdict(self, snapshot, story, schema, context, *, grouped):
        from .gemini import GeminiUnavailable
        role = 'vision_google_group' if grouped else 'vision_google_pair'
        label = 'group' if grouped else 'pair'
        fallback_mode = 'pair' if grouped else 'native'
        from .visual_attachments import visual_operation_unit
        supplied = json.loads(context) if isinstance(context, str) else context
        unit = canonical(visual_operation_unit(story, supplied))
        scope = {'story_id': story['id'], 'visual_scope': True,
                 'generation': story.get('_identity_generation', 0), 'purpose': 'identity',
                 'control_revision': story.get('_identity_research_control_revision', 0),
                 'job_id': story.get('_research_job_id'), 'job_attempt': story.get('_research_job_attempt')}
        self.guard_binding(scope)
        binding, saved = self.attempt(story, role, unit)
        if saved:
            return {'result': saved['result'], 'receipt': saved}
        if binding.get('phase', 'created') != 'created':
            raise RetryableProviderError('research_visual_'+label+'_outcome_unknown',
                                         retry_at=self.service.store.now()+300)
        self.guard_binding(binding)
        if not self.primary_vision.available:
            await self.checkpoint(binding, {'binding': dict(binding), 'phase': 'failed',
                'provider_send_state': 'not_sent', 'retry_safe': True, 'fallback_mode': fallback_mode,
                'error_code': 'google_visual_route_unavailable'})
            if grouped:
                raise PermanentProviderError('research_visual_group_pair_required')
            raise GeminiUnavailable(self.service.store.now()+300, 'visual_pair_known_failure')
        intent = {'binding': dict(binding), 'phase': 'submitted', 'provider': 'google',
                  'transport': 'gemini_generate_content', 'workload': 'identity_comparison',
                  'reference_mapping': story.get('_visual_reference_mapping'),
                  'provider_send_state': 'possibly_sent'}
        await self.checkpoint(binding, intent)
        try:
            response = await self.primary_vision.compare_visual(snapshot, story, schema, context)
        except BaseException as exc:
            prior = getattr(exc, 'receipt', None) or {}
            code = str(exc) if isinstance(exc, PermanentProviderError) else None
            known_local = code in {
                'headless_vision:invalid_image_attachment', 'headless_vision:invalid_comparison_context',
                'visual_direct_attachments_invalid', 'research_visual_group_pair_required'}
            attempts = prior.get('model_attempts')
            known_unsent = known_local or (isinstance(exc, GeminiUnavailable) and not attempts) or (
                isinstance(attempts, list) and bool(attempts)
                and all(isinstance(item, dict) and item.get('provider_send_state') == 'not_sent' for item in attempts))
            known_closed = _closed_malformed_visual(prior) or (
                isinstance(attempts, list) and bool(attempts)
                and all(isinstance(item, dict) and item.get('provider_send_state') in
                        {'not_sent', 'response_closed'} for item in attempts)
                and any(item.get('provider_send_state') == 'response_closed' for item in attempts))
            retry_safe = known_unsent or known_closed
            failed = {**intent, 'phase': 'failed' if retry_safe else 'unknown',
                      'provider_send_state': 'not_sent' if known_unsent else
                                             'response_closed' if known_closed else 'possibly_sent',
                      'retry_safe': retry_safe, 'fallback_mode': fallback_mode if retry_safe else None,
                      'error_type': type(exc).__name__,
                      'observation': prior}
            await self.checkpoint(binding, failed)
            LOG.warning('street_story_visual_google story_id=%s attempt_id=%s role=%s phase=%s provider_send_state=%s',
                        story['id'], binding['attempt_id'], role, failed['phase'], failed['provider_send_state'])
            if not isinstance(exc, Exception):
                raise
            if known_local:
                raise
            if retry_safe:
                if grouped:
                    raise PermanentProviderError('research_visual_group_pair_required') from None
                raise GeminiUnavailable(getattr(exc, 'retry_at', None) or self.service.store.now()+300,
                                        'visual_pair_known_failure') from None
            raise RetryableProviderError('research_visual_'+label+'_outcome_unknown',
                retry_at=getattr(exc, 'retry_at', None) or self.service.store.now()+300) from None
        receipt = {**response['receipt'], 'binding': dict(binding), 'phase': 'completed',
                   'provider_send_state': 'response_closed', 'result': response['result']}
        await self.checkpoint(binding, receipt)
        return {'result': response['result'], 'receipt': receipt}



def semantic_visual_context(context):
    """Queue lease IDs do not change an already observed pair of pixels.

    Photo/generation, catalog, evidence and schema still bind the attempt. The
    current queue ID is applied only when recording through the common gate.
    """
    supplied = json.loads(context)
    supplied.pop('comparison_id', None)
    supplied.pop('remaining_illustrations', None)
    return canonical(supplied)
