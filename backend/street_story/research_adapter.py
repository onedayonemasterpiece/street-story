"""Product bindings for existing DevCoveer OpenCode with shared admission.

Logical attempts persist in the product DB. No coding-agent task endpoint,
credential hopping or new POI/job system is involved.
"""
from __future__ import annotations
import hashlib
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from .opencode_research import OpenCodeResearch, ResearchUnavailable
from .errors import RetryableProviderError
from .service import ConflictError, canonical
from .config import reveal

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
            if (binding.get('photo_sha256') != story['photo_sha256'] or binding.get('generation') != generation
                    or research_stopped(research, purpose, photo_sha256=story['photo_sha256'], identity_generation=generation)
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
        logical = hashlib.sha256(canonical([story['id'], story['photo_sha256'],
            story.get('_identity_generation', 0), role, unit]).encode()).hexdigest()
        with self.service.store.tx() as db:
            rows = list(db.execute('SELECT * FROM research_provider_attempts WHERE logical_id=? ORDER BY created_at DESC', (logical,)))
            old = json.loads(rows[0]['receipt_json']) if rows else {}
            if rows and old.get('phase') == 'completed':
                return None, old
            resumed = {**(old.get('binding') or {}), **{k: old[k] for k in
                ('session_id', 'message_id', 'thread_id', 'turn_id', 'profile_verified', 'phase') if k in old}}
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
                'photo_sha256': story['photo_sha256'], 'generation': story.get('_identity_generation', 0),
                'attempt_created_at': self.service.store.now(),
                'purpose': 'facts' if role.startswith('facts') or '_fact_research_control_revision' in story else 'identity',
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
        for health_key in (route_key, quota_key):
            health = self.service.store.cache_get(health_key) or {}
            if health.get('retry_at', 0) > self.service.store.now():
                raise RetryableProviderError(health.get('category','research_route_waiting'), retry_at=health['retry_at'])
        binding, saved = self.attempt(story, role, unit)
        if saved:
            return {'result': saved.get('result'), 'sources': saved.get('sources', []), 'receipt': saved}
        try:
            return await invoke(binding)
        except ResearchUnavailable as exc:
            status = exc.receipt.get('provider_status')
            category = ('research_provider_credential_or_eligibility' if status in {401,403} else
                        'research_provider_quota' if status == 429 else exc.code)
            delay = 3600 if status in {401,403} else 30
            try:
                delay = max(delay,float(exc.receipt.get('provider_retry_after')))
            except (TypeError,ValueError):
                pass
            retry_at = self.service.store.now()+delay
            if status:
                self.service.store.cache_put(quota_key if status == 429 else route_key,
                    {'category':category,'status':status,'retry_at':retry_at},delay)
            raise RetryableProviderError(category, retry_at=retry_at) from exc
        except Exception as exc:
            if getattr(exc, 'resource_failure', False):
                raise RetryableProviderError(getattr(exc, 'code', 'research_admission_unavailable'),
                    retry_at=self.service.store.now()+max(3, getattr(exc, 'retry_after_ms', 30000)/1000)) from exc
            raise

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
            found = await direct(query)
            return {'sources':found.grounding_sources,'receipt':{
                'provider':'gemini_google_search','backend':'google_search','independent_failure':str(exc)}}

    @property
    def facts_available(self):
        proof = self.service.store.cache_get('research-text-verification-v1') or {}
        return self.giga is not None and proof.get('gigachat_model')=='GigaChat-2' and proof.get('semantic_contract_verified') is True

    async def extract_fact_page(self, page, story, context):
        from jsonschema import Draft202012Validator
        from .errors import MalformedProviderResponse
        capsule = {'context':{key:value for key,value in context.items() if key != '_known_fact_inventory'},
            '_known_fact_inventory':context.get('_known_fact_inventory',context.get('known_facts',[])),
            'sources':[{'source_version_id':page['source_version_id'],
            'url':page['source_url'],'title':page.get('source_title',''),
            'passages':[{'passage_id':p['passage_id'],'text':p['text']} for p in page['evidence_passages']]}],
            'known_fact_inventory':context.get('known_facts',[])}
        capsule['context']['known_inventory_next_offset'] = len(capsule['known_fact_inventory'])
        if self.giga is None:
            return await self.run(story,'facts',page['_unit_id'],lambda binding:
                self.client.extract_facts({**capsule,'jsonschema':FACT_PAGE_SCHEMA},binding))
        binding,saved = self.attempt(story,'facts_gigachat',page['_unit_id'])
        if saved:
            return {'result':saved['result'],'receipt':saved}
        if binding.get('phase') not in {None,'created'}:
            raise RetryableProviderError('gigachat_attempt_outcome_unknown',retry_at=self.service.store.now()+300)
        receipt={'binding':binding,'phase':'submitted','model_id':'GigaChat-2','provider_id':'gigachat'}
        await self.checkpoint(binding,receipt)
        try:
            token = self._active_binding.set(binding)
            try:
                result=await self.giga.research(context.get('coverage_goal',''),capsule=capsule,max_tool_calls=2)
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
            receipt['phase']='completed'
            await self.checkpoint(binding,receipt)
            return {'result':payload,'receipt':receipt}
        except Exception as exc:
            known_closed = isinstance(exc,MalformedProviderResponse) or getattr(exc,'status',None) is not None or getattr(exc,'resource_failure',False)
            receipt.update(phase='failed' if known_closed else 'unknown',error_type=type(exc).__name__)
            await self.checkpoint(binding,receipt)
            if not known_closed or getattr(exc,'resource_failure',False):
                raise RetryableProviderError('gigachat_research_waiting',retry_at=self.service.store.now()+300) from exc
            raise

    async def compare_image(self, sheet, story, schema, context):
        context = semantic_visual_context(context)
        unit = hashlib.sha256(sheet).hexdigest() + hashlib.sha256((context + canonical(schema)).encode()).hexdigest()
        return await self.run(story, 'vision', unit,
            lambda binding: self.client.compare_image(sheet, binding, schema, context))

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

    async def visual_verdict(self, snapshot, story, schema, context):
        import io
        from PIL import Image
        if not self.vision_available:
            raise RetryableProviderError('research_vision_unverified', retry_at=self.service.store.now()+300)
        from .gemini import GeminiUnavailable
        failures = []
        if self.primary_vision.available:
            try:
                return await self.primary_vision.compare_visual(snapshot, story, schema, context)
            except GeminiUnavailable as exc:
                failures.append({'route': 'google', 'category': str(exc), 'retry_at': exc.retry_at})
        if self.native_vision is not None and self.native_vision.available:
            context = semantic_visual_context(context)
            unit = hashlib.sha256(snapshot).hexdigest() + hashlib.sha256((context + canonical(schema)).encode()).hexdigest()
            binding, saved = self.attempt(story, 'vision_native', unit)
            if saved:
                return {'result': saved['result'], 'receipt': saved}
            result = await self.native_vision.compare_visual(snapshot, story, schema, context, binding)
            result['receipt']['availability_failures'] = failures
            return result
        if not self.opencode_vision_available:
            due = [f['retry_at'] for f in failures if f.get('retry_at')]
            raise RetryableProviderError('research_vision_waiting', retry_at=min(due) if due else self.service.store.now()+300)
        # The existing SOURCE/REF sheet is JPEG; the research attachment uses PNG.
        with Image.open(io.BytesIO(snapshot)) as image:
            output = io.BytesIO()
            image.save(output, format='PNG', optimize=True)
        return await self.compare_image(output.getvalue(), story, schema, context)


def semantic_visual_context(context):
    """Queue lease IDs do not change an already observed pair of pixels.

    Photo/generation, catalog, evidence and schema still bind the attempt. The
    current queue ID is applied only when recording through the common gate.
    """
    supplied = json.loads(context)
    supplied.pop('comparison_id', None)
    supplied.pop('remaining_illustrations', None)
    return canonical(supplied)
