import asyncio
import json
from types import SimpleNamespace

import pytest

from street_story import review_packets
from street_story.headless_fact_review import HeadlessFactReview
from street_story.headless_facts import HeadlessFacts
from street_story.service import ConflictError
from test_headless_fact_pool import fixture, result, RUN


@pytest.mark.parametrize('quote,expected', [
    ('Дата основания\\n1843', 'Дата основания\n1843'),
    ('Дата основания\\\\n1843', 'Дата основания\n1843'),
    ('Дата основания\\n1853', 'Дата основания\\n1853'),
    ('чужой факт\\n1843', 'чужой факт\\n1843'),
    ('Дата основания\\u000a1843', 'Дата основания\\u000a1843'),
    ('Дата основания\n1843', 'Дата основания\n1843'),
    ('\\n', '\\n'),
    ('\\\\n\\t\\r', '\\\\n\\t\\r'),
])
def test_quote_presentation_requires_exact_own_passage(quote, expected):
    assert review_packets.literal_basis_quote(quote, ['Дата основания\n1843']) == expected


@pytest.mark.asyncio
async def test_escaped_quote_preserves_raw_request_and_immutable_replay(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=1)
    session = SimpleNamespace(id='escaped-quote', resource_id=job['story_id'], actor=None,
                              closed=False, model='fixture', state={})
    packet = review_packets.read(harness.adapter, session, {'run_id': RUN, '_parallel_candidate_review': True})
    text = packet['items'][0]['text']
    with svc.store.tx() as db:
        row = db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=?',
                         (packet['packet_ref'],)).fetchone()
        payload = json.loads(row[0])
        payload['items'][0]['evidence'][0]['text'] = text + '\nOwn qualifier.'
        db.execute('UPDATE live_review_packets SET payload_json=? WHERE packet_ref=?',
                   (json.dumps(payload), packet['packet_ref']))
    quote = text + '\\nOwn qualifier.'
    args = {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': 0, 'evidence': [0],
        'verdict': 'supported', 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
        'claims': [text], 'basis_quotes': [quote], 'reason': 'Own literal passage.'}],
        'relations_complete': True, 'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}
    value = await harness.adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'escaped', 'args': args})
    assert value['eligible_count'] == 1
    with svc.store.connection() as db:
        row = db.execute('SELECT request_json,decisions_json FROM live_review_packets WHERE packet_ref=?',
                         (packet['packet_ref'],)).fetchone()
    assert json.loads(row['request_json'])['decisions'][0]['basis_quotes'] == [quote]
    assert json.loads(row['decisions_json'])['0']['basis_quotes'] == [text + '\nOwn qualifier.']
    replay = await harness.adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'replay', 'args': args})
    assert replay == value


@pytest.mark.parametrize('quote', [' ', '\n\t\r', '\u2003', '\\n', '\\\\n', '\\n\\t\\r'])
@pytest.mark.asyncio
async def test_whitespace_only_quote_never_establishes_own_support(tmp_path, quote):
    svc, job, harness = await candidates(tmp_path, count=1)
    session = SimpleNamespace(id='empty-quote', resource_id=job['story_id'], actor=None,
                              closed=False, model='fixture', state={})
    packet = review_packets.read(harness.adapter, session, {'run_id': RUN, '_parallel_candidate_review': True})
    args = {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': 0, 'evidence': [0],
        'verdict': 'supported', 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
        'claims': [packet['items'][0]['text']], 'basis_quotes': [quote], 'reason': 'Claimed literal support.'}],
        'relations_complete': True, 'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}
    with pytest.raises(ConflictError) as error:
        await harness.adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'empty', 'args': args})
    assert error.value.code == 'live_fact_review_evidence_invalid'
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 0
        row = db.execute('SELECT decisions_json,request_json FROM live_review_packets WHERE packet_ref=?',
                         (packet['packet_ref'],)).fetchone()
        assert json.loads(row['decisions_json']) == {} and row['request_json'] is None


@pytest.mark.parametrize('foreign_scope', ['unselected_own', 'sibling'])
@pytest.mark.asyncio
async def test_presentation_decoder_cannot_borrow_unselected_or_sibling_passage(tmp_path, foreign_scope):
    svc, job, harness = await candidates(tmp_path, count=2)
    session = SimpleNamespace(id='scoped-quote', resource_id=job['story_id'], actor=None,
                              closed=False, model='fixture', state={})
    packet = review_packets.read(harness.adapter, session, {'run_id': RUN, '_parallel_candidate_review': True})
    with svc.store.tx() as db:
        payload = json.loads(db.execute('SELECT payload_json FROM live_review_packets WHERE packet_ref=?',
                                       (packet['packet_ref'],)).fetchone()[0])
        foreign = 'Foreign date\n1853'
        if foreign_scope == 'unselected_own':
            payload['items'][0]['evidence'].append({**payload['items'][0]['evidence'][0], 'text': foreign})
        else:
            payload['items'][1]['evidence'][0]['text'] = foreign
        db.execute('UPDATE live_review_packets SET payload_json=? WHERE packet_ref=?',
                   (json.dumps(payload), packet['packet_ref']))
    args = {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': 0, 'evidence': [0],
        'verdict': 'supported', 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
        'claims': [packet['items'][0]['text']], 'basis_quotes': ['Foreign date\\\\n1853'], 'reason': 'Borrowed support.'}],
        'relations_complete': True, 'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}
    with pytest.raises(ConflictError) as error:
        await harness.adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'foreign', 'args': args})
    assert error.value.code == 'live_fact_review_evidence_invalid'
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 0


async def candidates(tmp_path, count=6, source_texts=None):
    svc, job = fixture(tmp_path, count=count if source_texts is None else 0)
    if source_texts is not None:
        from street_story.research_runs import persist_source_version
        with svc.store.tx() as db:
            for i, text in enumerate(source_texts):
                url = f'https://archive.example/history-{i}'
                persist_source_version(db, run_id=RUN, requested_url=url, final_url=url, title=f'History {i}',
                    content_type='text/html', http_status=200, redirect_chain=[], normalized_text=text,
                    read_status='complete', now=svc.store.now())
    async def extract(page, story, context):
        return result(page)
    svc.providers.research = SimpleNamespace(client=None, extract_fact_page=extract)
    from street_story.errors import RetryableProviderError
    while True:
        try:
            await HeadlessFacts(svc).run(job, RUN, 'History', 'history')
            break
        except RetryableProviderError:
            pass
    return svc, job, HeadlessFacts(svc)


def controlled_review_route(client=None):
    client = client or SimpleNamespace(model_id='fixture', directory=None)
    return {'role': 'facts_fixture', 'provider_id': 'fixture', 'model_id': 'fixture',
            'endpoint': 'fixture:controlled-review', 'client': client, 'qualified': True, 'available': True}


def qualify_controlled_review(harness):
    route = controlled_review_route()
    harness.service.providers.research._fact_pool_routes = lambda: [route]
    entry = {**{key: route[key] for key in ('provider_id', 'model_id', 'endpoint')},
             'schema_verified': True, 'own_passages_verified': True, 'qualifier_negative_verified': True,
             'nearby_duplicate_verified': True, 'nearby_conflict_verified': True}
    harness.service.store.cache_put('fact-semantic-verification-v1', {'routes': [entry]}, ttl_seconds=3600)
    return route


class ControlledReview(HeadlessFactReview):
    MAX_PACKET_FACTS = 3  # Deliberately force siblings to exercise stale/unknown fences.
    active = 0
    peak = 0
    calls = 0
    mode = 'positive'

    def __init__(self, harness):
        super().__init__(harness)
        self.fixture_route = qualify_controlled_review(harness)

    async def _infer(self, packet, job, unit, saved, ordinal=0):
        if saved.get('phase') in {'unknown', 'started'}:
            return None
        type(self).calls += 1
        type(self).active += 1
        type(self).peak = max(type(self).peak, type(self).active)
        await asyncio.sleep(.02)
        type(self).active -= 1
        if type(self).mode == 'unknown':
            self._put(job, unit, {'phase': 'unknown', 'packet_ref': packet['packet_ref'], 'route': 'facts_review_fixture',
                                 'route_identity': self._route_identity(self.fixture_route)})
            return None
        decisions=[]
        for fact in sorted({r['fact'] for r in packet['items']}):
            rows=[r for r in packet['items'] if r['fact']==fact]
            item=rows[0]
            decisions.append({'fact':fact,'evidence':sorted({r['evidence'] for r in rows}),
                'verdict':'supported','atomic':True,'support_complete':True,'qualifiers_preserved':True,
                'claims':[item['text']],'basis_quotes':[item['text']], 'reason':'Controlled own exact passage.'})
        args={'packet_ref':packet['packet_ref'],'decisions':decisions,'relations_complete':True,
              'conflicts':[],'coverage_complete':False,'missing_aspects':[]}
        self._put(job, unit, {'phase':'result','args':args,
                             'route_identity': self._route_identity(self.fixture_route)})
        return args


@pytest.fixture(autouse=True)
def reset_host():
    ControlledReview.active=ControlledReview.peak=ControlledReview.calls=0
    ControlledReview.mode='positive'


@pytest.mark.asyncio
async def test_one_bounded_packet_reviews_twelve_candidates_without_stale_sibling_rework(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=12)
    class GroupedReview(ControlledReview):
        MAX_PACKET_FACTS = HeadlessFactReview.MAX_PACKET_FACTS
    engine = GroupedReview(harness)
    assert await engine.run(job, RUN, 0) == 1
    assert GroupedReview.calls == 1
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 12
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 12
        assert db.execute('SELECT SUM(owner_selected) FROM fact_assertions').fetchone()[0] == 0
    assert await engine.run(job, RUN, 0) == 0
    assert GroupedReview.calls == 1


@pytest.mark.asyncio
async def test_empty_optional_existing_relation_never_grants_support_or_accepts_foreign_id(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=1)
    session = SimpleNamespace(id='existing-relation', resource_id=job['story_id'], actor=None,
                              closed=False, model='fixture', state={})
    packet = review_packets.read(harness.adapter, session, {'run_id': RUN, '_parallel_candidate_review': True})
    item = packet['items'][0]
    decision = {'fact': 0, 'evidence': [0], 'verdict': 'supported', 'atomic': True,
                'support_complete': True, 'qualifiers_preserved': True, 'claims': [item['text']],
                'basis_quotes': [item['text']], 'reason': 'Own literal evidence.',
                'equivalent_to_existing': 'foreign-id'}
    args = {'packet_ref': packet['packet_ref'], 'decisions': [decision], 'relations_complete': True,
            'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}
    with pytest.raises(ConflictError, match='nearby_existing_claims'):
        await harness.adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'foreign', 'args': args})
    decision['equivalent_to_existing'] = ''
    decision['support_complete'] = False
    with pytest.raises(ConflictError):
        await harness.adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'unsupported', 'args': args})
    decision['support_complete'] = True
    result = await harness.adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'supported', 'args': args})
    assert result['eligible_count'] == 1
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_packet_capacity_reduces_whole_candidates_without_clipping_evidence(tmp_path):
    from street_story.headless_fact_review import VERIFIER_PROMPT
    from street_story.service import canonical
    svc, job, harness = await candidates(tmp_path, count=6)
    session = SimpleNamespace(id='estimate', resource_id=job['story_id'], model='fixture',
        actor=None, closed=False, state={})
    with svc.store.connection() as db:
        ids = review_packets.pending_candidates(db, job['story_id'], RUN)
    first = review_packets.read(harness.adapter, session, {'run_id': RUN, '_candidate_ids': ids})
    items = list(first['items'])
    while first.get('has_more'):
        first = review_packets.read(harness.adapter, session, first['next_args'])
        items.extend(first['items'])
    budget = len(VERIFIER_PROMPT + canonical({**first, 'items': items})) - 300
    calls = []
    class BoundedReview(ControlledReview):
        MAX_PACKET_FACTS = 12
        async def _infer(self, packet, *args, **kwargs):
            assert len(VERIFIER_PROMPT + canonical(packet)) <= budget
            for item in packet['items']:
                assert item['passage'] == item['text'] and item['passage_complete'] is True
            calls.append(packet['total_facts'])
            return await super()._infer(packet, *args, **kwargs)
    engine = BoundedReview(harness)
    engine._qualified_routes = lambda **_: [controlled_review_route(
        SimpleNamespace(directory=None, limits=SimpleNamespace(max_input_chars=budget)))]
    committed = await engine.run(job, RUN, 0)
    assert committed == len(calls) and committed > 1
    assert calls and max(calls) < 6


@pytest.mark.asyncio
async def test_cyrillic_packets_split_before_actual_live_send_and_finish_remaining_candidates(tmp_path):
    from contextvars import ContextVar
    from street_story.headless_fact_review import VERIFIER_PROMPT
    from street_story.live_research import LiveSemanticClient, RESULT_TOOL
    from street_story.research_adapter import ProductResearchAdapter
    from street_story.service import canonical

    texts = [f'Здание {i} сохранило ' + 'кирпичную облицовку фасада с узорчатыми деталями, ' * 8
             + 'согласно описанию 2005 года.' for i in range(12)]
    svc, job, harness = await candidates(tmp_path, source_texts=texts)
    provider = object.__new__(ProductResearchAdapter)
    provider.service, provider.client, provider.giga = svc, None, None
    provider._active_binding = ContextVar('bounded-live-review-test', default=None)
    starts, sends = [], []

    class Host:
        def __init__(self, _service, _settings, **kwargs):
            self.adapter = kwargs['operation_adapter_factory']()
            self.guard = kwargs['before_operation_send']
            self.session = SimpleNamespace(id=f'bounded-live-{len(starts)}')
        async def start(self, **kwargs):
            initialized = self.adapter.initialize(**kwargs)
            prompt = initialized['context']['frozen_research_operation']['prompt']
            assert len(prompt.encode('utf-8')) <= 24000
            self.packet = json.loads(prompt.split('Frozen packet: ', 1)[1])
            for item in self.packet['items']:
                assert item['passage'] == item['text'] and item['passage'] in texts
                assert item['passage_complete'] is True
            starts.append(prompt)
            self.adapter.on_event(self.session, {'type': 'ready'})
            return {'session_id': self.session.id}
        async def input(self, **kwargs):
            self.guard()
            sends.append(self.packet['total_facts'])
            self.adapter.on_event(self.session, {'type': 'input_timing', 'text_sent_at': 1})
            args = {'packet_ref': self.packet['packet_ref'], 'decisions': [
                {'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported',
                 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
                 'claims': [item['text']], 'basis_quotes': [item['quote_ref']], 'reason': 'Own unchanged passage.'}
                for item in self.packet['items']], 'relations_complete': True, 'conflicts': [],
                'coverage_complete': False, 'missing_aspects': []}
            await self.adapter.execute_tool(self.session, {'name': RESULT_TOOL, 'id': 'bounded', 'args': args})
        async def stop_all(self):
            pass

    provider.live_facts = LiveSemanticClient(provider, host_factory=Host)
    svc.providers.research = provider
    svc.store.cache_put('fact-semantic-verification-v1', {'routes': [{
        'provider_id': provider.live_facts.provider_id, 'model_id': provider.live_facts.model_id,
        'endpoint': provider.live_facts.endpoint, 'schema_verified': True, 'own_passages_verified': True,
        'qualifier_negative_verified': True, 'nearby_duplicate_verified': True,
        'nearby_conflict_verified': True}]}, ttl_seconds=3600)
    engine = HeadlessFactReview(harness)
    session = SimpleNamespace(id='estimate-bytes', resource_id=job['story_id'], actor=None,
                              closed=False, model='fixture', state={})
    with svc.store.connection() as db:
        ids = review_packets.pending_candidates(db, job['story_id'], RUN)
    full, _, _ = engine._prepare_packet(job, RUN, session, ids)
    prompt = VERIFIER_PROMPT + canonical(full)
    assert len(prompt) < 24000 < len(prompt.encode('utf-8'))
    assert await engine._run_one(job, RUN, 0) == 1
    with svc.store.connection() as db:
        assert 0 < len(review_packets.pending_candidates(db, job['story_id'], RUN)) < len(texts)
    assert await engine.run(job, RUN, 0) > 0
    assert len(sends) == len(starts) > 1 and sum(sends) == len(texts)
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == len(texts)
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == len(texts)
        receipts = [json.loads(row[0]) for row in db.execute('SELECT receipt_json FROM research_provider_attempts')]
    assert len(receipts) == len(sends)
    assert all(receipt['phase'] == 'completed' and receipt['text_sends'] == 1 for receipt in receipts)


def test_route_capacity_measures_serialized_prompt_with_escaping_and_transport_units():
    from street_story.headless_fact_review import VERIFIER_PROMPT
    from street_story.service import canonical
    live = {'role': 'facts_live', 'client': SimpleNamespace()}
    native = {'role': 'facts_native', 'client': SimpleNamespace(limits=SimpleNamespace(max_input_chars=24000))}
    prompt = VERIFIER_PROMPT + canonical({'passage': 'Ж' * 11000})
    assert len(prompt) < 24000 < len(prompt.encode('utf-8'))
    assert not HeadlessFactReview._route_accepts_prompt(live, prompt)
    assert HeadlessFactReview._route_accepts_prompt(native, prompt)
    assert not HeadlessFactReview._packet_fits([live, native], prompt)
    assert HeadlessFactReview._packet_fits([live, native], prompt, single=True)
    raw = '\\' * 11000
    escaped = VERIFIER_PROMPT + canonical({'passage': raw})
    assert len(raw) < 24000 < len(escaped)
    assert not HeadlessFactReview._route_accepts_prompt(native, escaped)


@pytest.mark.asyncio
async def test_fresh_oversized_live_packet_uses_fitting_qualified_text_route_without_live_attempt(tmp_path):
    from street_story.headless_fact_review import VERIFIER_PROMPT
    from street_story.service import canonical
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = HeadlessFactReview(harness)
    session = SimpleNamespace(id='whole-unit', resource_id=job['story_id'], actor=None,
                              closed=False, model='fixture', state={})
    packet = review_packets.read(harness.adapter, session, {'run_id': RUN, '_parallel_candidate_review': True})
    # An indivisible frozen unit can contain a large conflict ledger. Its own
    # assertion and passage remain intact; an unavailable route cannot clip it.
    packet['nearby_existing_claims'] = [{'fact_id': f'existing-{i}', 'text': 'Ж' * 450} for i in range(23)]
    prompt = VERIFIER_PROMPT + canonical(packet)
    assert len(prompt) < 24000 < len(prompt.encode('utf-8'))
    calls = []
    class Client:
        directory = '/existing/qualified'
        limits = SimpleNamespace(max_input_chars=24000)
        def __init__(self, model_id):
            self.model_id = model_id
        async def _run(self, role, supplied, binding, schema):
            assert self.model_id == 'qualified-text'
            assert supplied == prompt and len(supplied) <= self.limits.max_input_chars
            calls.append(self.model_id)
            return {'result': {'packet_ref': packet['packet_ref'], 'decisions': [],
                'relations_complete': True, 'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}}
    routes = [{'role': role, 'qualified': True, 'available': True, 'endpoint': f'fixture:{model}',
               'model_id': model, 'provider_id': 'fixture', 'client': Client(model)}
              for role, model in [('facts_live', 'live'), ('facts_native', 'qualified-text')]]
    engine._qualified_routes = lambda **_: routes
    async def run(story, role, unit, operation, *, client):
        return await operation({'attempt_id': 'fresh-whole-unit'})
    svc.providers.research = SimpleNamespace(run=run)
    args = await engine._infer(packet, job, 'fresh-whole-unit', {})
    assert args['packet_ref'] == packet['packet_ref'] and calls == ['qualified-text']
    saved = svc.store.checkpoint_get(job['id'], 'headless_fact_review:fresh-whole-unit')
    assert saved['phase'] == 'result' and saved['model_id'] == 'qualified-text'


@pytest.mark.asyncio
async def test_packet_sizing_uses_available_routes_before_cooling_live_route(tmp_path):
    texts = [f'Здание {i} сохранило ' + 'кирпичную облицовку фасада с узорчатыми деталями, ' * 8
             + 'согласно описанию 2005 года.' for i in range(12)]
    svc, job, harness = await candidates(tmp_path, source_texts=texts)
    calls = []
    class AvailableReview(ControlledReview):
        MAX_PACKET_FACTS = 12
        async def _infer(self, packet, *args, **kwargs):
            calls.append(packet['total_facts'])
            return await super()._infer(packet, *args, **kwargs)
    engine = AvailableReview(harness)
    routes = [{**controlled_review_route(), 'role': 'facts_live', 'available': False},
              {**controlled_review_route(SimpleNamespace(directory=None, limits=SimpleNamespace(max_input_chars=24000))),
               'role': 'facts_native'}]
    engine._qualified_routes = lambda available=True: [r for r in routes if not available or r['available']]
    assert await engine.run(job, RUN, 0) == 1
    assert calls == [12]


@pytest.mark.asyncio
async def test_ready_review_packets_continue_same_job_after_progress_without_claiming_complete(tmp_path, monkeypatch):
    from street_story.errors import RetryableProviderError
    svc, job, harness = await candidates(tmp_path, count=3)
    routes = [controlled_review_route(SimpleNamespace(directory=None, limits=SimpleNamespace(max_input_chars=24000)))]
    monkeypatch.setattr(HeadlessFactReview, '_qualified_routes', lambda self, **kwargs: routes)
    class SinglePacketReview(ControlledReview):
        MAX_PACKET_FACTS = 1
    engine = SinglePacketReview(harness)
    async def review_one(job, run_id, control_revision):
        return await engine._run_one(job, run_id, control_revision)
    harness._review_candidates = review_one
    for eligible in (1, 2):
        now = svc.store.now()
        with pytest.raises(RetryableProviderError) as error:
            await harness.run(job, RUN, 'History', 'history')
        assert str(error.value) == 'research_fact_review_partial'
        assert now + 1 <= error.value.retry_at <= svc.store.now() + 1
        with svc.store.connection() as db:
            run = db.execute('SELECT state,completed_at FROM research_runs WHERE run_id=?', (RUN,)).fetchone()
            assert run['state'] == 'partial' and run['completed_at'] is None
            assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == eligible
    outcome = await harness.run(job, RUN, 'History', 'history')
    assert outcome['coverage_complete'] is True and outcome['eligible_count'] == 3
    assert SinglePacketReview.calls == 3


@pytest.mark.parametrize('blocked', ['no_progress', 'unknown', 'cooldown', 'exhausted', 'route_unavailable'])
@pytest.mark.asyncio
async def test_review_progress_never_shortens_unknown_or_cooldown_wait(tmp_path, monkeypatch, blocked):
    svc, job, harness = await candidates(tmp_path, count=1)
    routes = [] if blocked == 'route_unavailable' else [{'available': True, 'client': SimpleNamespace()}]
    monkeypatch.setattr(HeadlessFactReview, '_qualified_routes', lambda self, **kwargs: routes)
    if blocked in {'unknown', 'exhausted'}:
        session = SimpleNamespace(id='unknown-wait', resource_id=job['story_id'], actor=None,
                                  closed=False, model='fixture', state={})
        with svc.store.connection() as db:
            ids = review_packets.pending_candidates(db, job['story_id'], RUN)
        packet, unit, _ = HeadlessFactReview(harness)._prepare_packet(job, RUN, session, ids)
        svc.store.checkpoint_put(job['id'], 'headless_fact_review:' + unit,
                                 {'phase': blocked, 'packet_ref': packet['packet_ref']})
    elif blocked == 'cooldown':
        svc.store.checkpoint_put(job['id'], 'headless_fact_review:blocked',
                                 {'phase': 'closed_error', 'retry_at': svc.store.now() + 300})
    now = svc.store.now()
    monkeypatch.setattr(svc.store, 'now', lambda: now)
    assert harness._review_retry_at(job, RUN, 0 if blocked == 'no_progress' else 1) == now + 60


@pytest.mark.parametrize('blocked', ['unknown', 'cooldown'])
@pytest.mark.asyncio
async def test_review_progress_schedules_ready_assertions_outside_exact_waiting_bundle(tmp_path, monkeypatch, blocked):
    svc, job, harness = await candidates(tmp_path, count=2)
    routes = [{'available': True, 'client': SimpleNamespace()}]
    monkeypatch.setattr(HeadlessFactReview, '_qualified_routes', lambda self, **kwargs: routes)
    session = SimpleNamespace(id='scoped-wait', resource_id=job['story_id'], actor=None,
                              closed=False, model='fixture', state={})
    with svc.store.connection() as db:
        ids = review_packets.pending_candidates(db, job['story_id'], RUN)
    packet = review_packets.read(harness.adapter, session,
        {'run_id': RUN, '_parallel_candidate_review': True, '_candidate_ids': ids[:1]})
    now = svc.store.now()
    monkeypatch.setattr(svc.store, 'now', lambda: now)
    saved = {'phase': 'unknown' if blocked == 'unknown' else 'closed_error', 'packet_ref': packet['packet_ref']}
    if blocked == 'cooldown':
        saved['retry_at'] = now + 300
    svc.store.checkpoint_put(job['id'], 'headless_fact_review:scoped-wait', saved)
    assert harness._review_retry_at(job, RUN, 1) == now + 1
    with svc.store.tx() as db:
        # Once the independent ready assertion is gone, only the waiting scope
        # remains and its original wait must be preserved.
        db.execute("UPDATE fact_assertions SET eligibility='eligible' WHERE story_id=? AND assertion_id=?",
                   (job['story_id'], ids[1]))
    assert harness._review_retry_at(job, RUN, 1) == now + 60


@pytest.mark.asyncio
async def test_independent_packets_stay_pending_but_new_eligible_claim_stales_sibling(tmp_path):
    svc, job, harness=await candidates(tmp_path)
    session=SimpleNamespace(id='review',resource_id=job['story_id'],model='gemini-3.8-live',actor=None,closed=False,state={})
    with svc.store.connection() as db:
        ids=review_packets.pending_candidates(db,job['story_id'],RUN)
    packets=[review_packets.read(harness.adapter,session,{'run_id':RUN,'_candidate_ids':ids[i:i+3],
        '_parallel_candidate_review':True}) for i in (0,3)]
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM live_review_attempts WHERE state='pending'").fetchone()[0]==2
        for packet in packets:
            review_packets.load(harness.adapter,session,db,packet['packet_ref'])
    # Unrelated background candidate revision does not spoil own frozen review.
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET revision=revision+1 WHERE id=?',(job['story_id'],))
    with svc.store.connection() as db:
        review_packets.load(harness.adapter,session,db,packets[0]['packet_ref'])
    with svc.store.tx() as db:
        db.execute("UPDATE fact_assertions SET eligibility='eligible' WHERE story_id=? AND assertion_id=?",(job['story_id'],ids[0]))
    with svc.store.connection() as db:
        with pytest.raises(ConflictError,match='Revisions changed'):
            review_packets.load(harness.adapter,session,db,packets[1]['packet_ref'])


@pytest.mark.asyncio
async def test_two_reviews_prepare_serially_with_current_ledger_and_preserve_owner(tmp_path):
    svc,job,harness=await candidates(tmp_path)
    sid=job['story_id']
    with svc.store.tx() as db:
        row=svc._story_row(db,sid)
        research=json.loads(row['research_json'])
        research['publication_concept']='Owner concept'
        db.execute('UPDATE stories SET draft_text=?,research_json=? WHERE id=?',('Owner draft',json.dumps(research),sid))
    engine=ControlledReview(harness)
    assert await engine.run(job,RUN,0)==2
    assert ControlledReview.peak==1 and ControlledReview.calls==2
    with svc.store.connection() as db:
        eligible=db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0]
        assert eligible==6
        assert db.execute('SELECT SUM(owner_selected) FROM fact_assertions').fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0]==6
        assert db.execute("SELECT COUNT(*) FROM fact_conflict_scans WHERE detector='backend_semantic_review'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM fact_conflict_scans WHERE detector='mira_live_review'").fetchone()[0] == 0
        assert svc._story_row(db,sid)['draft_text']=='Owner draft'
    # Both packets committed on their first paid review, with no stale sibling.
    assert await engine.run(job,RUN,0)==0
    assert ControlledReview.calls==2
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0]==6
        assert json.loads(svc._story_row(db,sid)['research_json'])['publication_concept']=='Owner concept'


@pytest.mark.asyncio
async def test_unknown_review_fences_conflicting_ledger_without_new_sibling_send(tmp_path):
    svc,job,harness=await candidates(tmp_path)
    ControlledReview.mode='unknown'
    engine=ControlledReview(harness)
    assert await engine.run(job,RUN,0)==0
    assert ControlledReview.calls==1
    ControlledReview.mode='positive'
    assert await engine.run(job,RUN,0)==0
    assert ControlledReview.calls==1


@pytest.mark.asyncio
async def test_completed_original_review_is_recovered_after_checkpoint_interruption_without_new_model(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=3)
    ControlledReview.mode = 'unknown'
    assert await ControlledReview(harness).run(job, RUN, 0) == 0
    with svc.store.connection() as db:
        row = db.execute("SELECT stage,value_json FROM research_checkpoints WHERE job_id=? AND stage LIKE 'headless_fact_review:%'", (job['id'],)).fetchone()
    unit, saved = row['stage'].split(':', 1)[1], json.loads(row['value_json'])
    session = SimpleNamespace(id='headless-review:' + job['id'], resource_id=job['story_id'], actor=None,
                              closed=False, model='fixture', state={})
    packet = review_packets.read(harness.adapter, session, {'packet_ref': saved['packet_ref']})
    decisions = [{'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported',
        'atomic': True, 'support_complete': True, 'qualifiers_preserved': True, 'claims': [item['text']],
        'basis_quotes': [item['text']], 'reason': 'Original own literal evidence.'} for item in packet['items']]
    args = {'packet_ref': packet['packet_ref'], 'decisions': decisions, 'relations_complete': True,
            'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}
    with svc.store.tx() as db:
        receipt = {'binding': {'fact_unit_id': unit}, 'phase': 'completed', 'model_id': 'original-fixture', 'result': args}
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('original-completed', 'original-logical', job['story_id'], saved['route'], json.dumps(receipt), svc.store.now(), svc.store.now()))
    # The original controlled route retains semantic qualification; recovery
    # commits its exact closed response without another inference.
    assert await HeadlessFactReview(harness).run(job, RUN, 0) == 1
    assert ControlledReview.calls == 1
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['addressed', 'saved_contract', 'missing_message', 'changed_route'])
async def test_restart_observes_exact_addressed_review_without_replacing_its_packet(tmp_path, monkeypatch, case):
    svc, job, harness = await candidates(tmp_path, count=3)
    ControlledReview.mode = 'unknown'
    await ControlledReview(harness).run(job, RUN, 0)
    with svc.store.connection() as db:
        row = db.execute("SELECT stage,value_json FROM research_checkpoints WHERE job_id=? AND stage LIKE 'headless_fact_review:%'", (job['id'],)).fetchone()
    unit, saved = row['stage'].split(':', 1)[1], json.loads(row['value_json'])
    session = SimpleNamespace(id='headless-review:' + job['id'], resource_id=job['story_id'], actor=None,
                              closed=False, model='fixture', state={})
    packet = review_packets.read(harness.adapter, session, {'packet_ref': saved['packet_ref']})
    args = {'packet_ref': packet['packet_ref'], 'decisions': [
        {'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported', 'atomic': True,
         'support_complete': True, 'qualifiers_preserved': True, 'claims': [item['text']],
         'basis_quotes': [item['text']], 'reason': 'Own original passage.'} for item in packet['items']],
        'relations_complete': True, 'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}
    calls = []
    from street_story.headless_fact_review import LEGACY_VERIFIER_PROMPT, VERIFIER_PROMPT, VERIFIER_CONTRACT_ID
    original_prompt = VERIFIER_PROMPT if case == 'saved_contract' else LEGACY_VERIFIER_PROMPT
    async def closed_readback(role, prompt, binding, schema):
        assert role == 'facts' and binding['message_id'] == 'msg_original'
        assert prompt == original_prompt + json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        assert json.loads(prompt.split('Frozen packet: ')[1]) == packet
        calls.append('original_readback')
        return {'result': args}
    client = SimpleNamespace(model_id='fixture', directory='/fixture', _run=closed_readback)
    route = {'provider_id': 'fixture', 'model_id': 'fixture', 'endpoint': 'existing', 'client': client}
    engine = HeadlessFactReview(harness)
    role = 'facts_review_fixture'
    original = {**saved, 'frozen_packet': packet, 'route_identity': engine._route_identity(route)}
    if case == 'saved_contract':
        original.update(verifier_prompt=original_prompt, verifier_contract_id=VERIFIER_CONTRACT_ID)
        from street_story import headless_fact_review
        monkeypatch.setattr(headless_fact_review, 'VERIFIER_PROMPT', 'Changed instructions must not reach original request. ')
        monkeypatch.setattr(headless_fact_review, 'VERIFIER_CONTRACT_ID', 'changed-later-policy')
    engine._put(job, unit, original)
    receipt = {'binding': {'fact_unit_id': unit}, 'phase': 'submitted', 'session_id': 'ses_original', 'message_id': 'msg_original'}
    if case == 'missing_message':
        receipt.pop('message_id')
    if case == 'changed_route':
        route['endpoint'] = 'replacement'
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('original-submitted', 'original-logical', job['story_id'], role, json.dumps(receipt), svc.store.now(), svc.store.now()))
    async def run(story, received_role, received_unit, invoke, *, client):
        assert received_role == role and received_unit == unit
        return await invoke({'phase': 'submitted', 'session_id': 'ses_original', 'message_id': 'msg_original'})
    svc.providers.research = SimpleNamespace(run=run)
    monkeypatch.setattr(engine, '_qualified_routes', lambda available=True: [route])
    committed = await engine.run(job, RUN, 0)
    if case in {'missing_message', 'changed_route'}:
        assert committed == 0 and calls == []
        return
    assert committed == 1
    assert calls == ['original_readback']
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_closed_client_pending_backend_review_retries_and_resumes_without_reextracting(tmp_path, monkeypatch):
    from street_story.errors import RetryableProviderError
    svc, job = fixture(tmp_path, count=1)
    calls = []
    async def extract(page, story, context):
        calls.append(page['chunk_id'])
        return result(page)
    svc.providers.research = SimpleNamespace(client=None, extract_fact_page=extract)
    monkeypatch.setattr(HeadlessFactReview, '_qualified_routes', lambda self, available=True: [controlled_review_route()])
    first = HeadlessFacts(svc)
    async def temporarily_unavailable(*args):
        return 0
    monkeypatch.setattr(first, '_review_candidates', temporarily_unavailable)
    with pytest.raises(RetryableProviderError, match='research_fact_review_partial'):
        await first.run(job, RUN, 'History', 'history')
    # A newly constructed executor has no client session or in-memory cursor.
    resumed = HeadlessFacts(svc)
    async def review(job, run_id, control_revision):
        return await ControlledReview(resumed).run(job, run_id, control_revision)
    monkeypatch.setattr(resumed, '_review_candidates', review)
    await resumed.run(job, RUN, 'History', 'history')
    assert len(calls) == 1
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM fact_conflict_scans WHERE detector='backend_semantic_review'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_original_readback_serializes_conflicting_candidate_progress(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=6)
    ControlledReview.mode = 'unknown'
    await ControlledReview(harness).run(job, RUN, 0)
    with svc.store.connection() as db:
        saved_units = list(db.execute("SELECT stage,value_json FROM research_checkpoints WHERE job_id=? AND stage LIKE 'headless_fact_review:%' ORDER BY stage", (job['id'],)))
    unit, saved = saved_units[0]['stage'].split(':', 1)[1], json.loads(saved_units[0]['value_json'])
    session = SimpleNamespace(id='headless-review:' + job['id'], resource_id=job['story_id'], model='fixture',
                              actor=None, closed=False, state={})
    packet = review_packets.read(harness.adapter, session, {'packet_ref': saved['packet_ref']})
    waiting, release = asyncio.Event(), asyncio.Event()
    class RollingReview(ControlledReview):
        async def _infer(self, packet, job, unit, saved, ordinal=0):
            if saved.get('phase') == 'observe_original':
                waiting.set()
                await release.wait()
                saved = {}
            return await super()._infer(packet, job, unit, saved, ordinal)
    engine = RollingReview(harness)
    engine._put(job, unit, {**saved, 'frozen_packet': packet, 'route_identity': {'fixture': True}})
    assert len(saved_units) == 1
    ControlledReview.mode = 'positive'
    task = asyncio.create_task(engine.run(job, RUN, 0))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        await asyncio.sleep(.05)
        with svc.store.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0]
        assert count == 0 and not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_owner_selection_change_stales_private_background_packet(tmp_path):
    svc,job,harness=await candidates(tmp_path)
    session=SimpleNamespace(id='review',resource_id=job['story_id'],actor=None,closed=False,state={})
    packet=review_packets.read(harness.adapter,session,{'run_id':RUN,'_parallel_candidate_review':True})
    with svc.store.tx() as db:
        db.execute('UPDATE fact_assertions SET owner_selected=1 WHERE story_id=?',(job['story_id'],))
    with svc.store.connection() as db:
        with pytest.raises(ConflictError):
            review_packets.load(harness.adapter,session,db,packet['packet_ref'])


@pytest.mark.asyncio
async def test_good_fact_enters_poi_while_slow_extraction_siblings_still_run(tmp_path, monkeypatch):
    svc,job=fixture(tmp_path,count=3)
    release=asyncio.Event()
    active=asyncio.Event()
    async def extract(page,story,context):
        if page['_extractor_ordinal']:
            active.set()
            await release.wait()
        return result(page)
    svc.providers.research=SimpleNamespace(client=None,extract_fact_page=extract)
    harness=HeadlessFacts(svc)
    async def review(job,run_id,control_revision):
        return await ControlledReview(harness).run(job,run_id,control_revision)
    monkeypatch.setattr(harness,'_review_candidates',review)
    task=asyncio.create_task(harness.run(job,RUN,'History','history'))
    try:
        await asyncio.wait_for(active.wait(),1)
        for _ in range(30):
            with svc.store.connection() as db:
                eligible=db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0]
            if eligible:
                break
            await asyncio.sleep(.05)
        assert eligible==1 and not task.done()
        release.set()
        await asyncio.wait_for(task,3)
        with svc.store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0]==3
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.asyncio
async def test_repeated_local_capacity_polling_backs_off_without_model_send_or_owner_loss(tmp_path, monkeypatch):
    from street_story.errors import RetryableProviderError
    svc,job=fixture(tmp_path,count=3)
    current=svc.store.now()
    monkeypatch.setattr(svc.store,'now',lambda: current)
    calls=[]
    async def no_capacity(page,story,context):
        calls.append(page['_unit_id'])
        error=RetryableProviderError('research_fact_pool_waiting',retry_at=current+3)
        error.route_failures=['RESOURCE_NO_CAPACITY','RESOURCE_NO_CAPACITY']
        raise error
    svc.providers.research=SimpleNamespace(client=None,extract_fact_page=no_capacity)
    harness=HeadlessFacts(svc)
    monkeypatch.setattr(harness,'_boundary_closed',lambda *args:True)
    for expected in (5,10,20,40,60,60):
        with pytest.raises(RetryableProviderError) as failure:
            await harness.run(job,RUN,'History','history')
        assert failure.value.retry_at >= current+expected
        current=failure.value.retry_at+1
    assert len(calls)==18
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0]==0
        assert svc._story_row(db,job['story_id'])['state']!='needs_review'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['malformed','unknown','aborted_unknown','exhausted'])
async def test_semantic_text_pool_closed_fallback_and_unknown_fence_are_distinct(tmp_path,mode):
    from contextvars import ContextVar
    from street_story.research_adapter import ProductResearchAdapter
    from street_story.opencode_research import ResearchUnavailable
    svc,job,harness=await candidates(tmp_path,count=3)
    provider=object.__new__(ProductResearchAdapter)
    provider.service=svc
    provider._active_binding=ContextVar('semantic-test',default=None)
    provider.giga=None
    calls=[]
    class Client:
        endpoint='http://127.0.0.1:4097'
        provider_id='opencode'
        directory='/existing/research'
        def __init__(self,model_id):
            self.model_id=model_id
        async def _run(self,role,prompt,binding,schema):
            calls.append(self.model_id)
            packet=json.loads(prompt.split('Frozen packet: ',1)[1])
            if self.model_id=='mimo-v2.6-flash-free' and mode in {'unknown','aborted_unknown'}:
                receipt={'binding':binding,'phase':'aborted' if mode=='aborted_unknown' else 'submitted','session_id':'ses_existing',
                         'message_id':'msg_existing','model_id':self.model_id}
                await provider.checkpoint(binding,receipt)
                raise ResearchUnavailable('research_attempt_unknown',receipt)
            if self.model_id=='mimo-v2.6-flash-free' or mode == 'exhausted':
                args={'decisions':'broken'}
            else:
                args={'packet_ref':packet['packet_ref'],'decisions':[],
                    'relations_complete':True,'conflicts':[],'coverage_complete':False,'missing_aspects':[]}
            receipt={'binding':binding,'phase':'completed','result':args,'model_id':self.model_id}
            await provider.checkpoint(binding,receipt)
            return {'result':args,'receipt':receipt}
    provider.client=Client('mimo-v2.6-flash-free')
    extra=Client('nemotron-3-ultra-free')
    provider._fact_extractor_clients={extra.model_id:extra}
    fields={'semantic_contract_verified':True,'source_subject_negative_verified':True,
            'planned_modality_verified':True,'known_claim_reuse_verified':True,
            'schema_verified':True,'own_passages_verified':True,'qualifier_negative_verified':True,
            'nearby_duplicate_verified':True,'nearby_conflict_verified':True}
    entries=[{'provider_id':'opencode','model_id':client.model_id,'endpoint':client.endpoint,**fields}
             for client in (provider.client,extra)]
    svc.store.cache_put('research-text-verification-v1',{'extractors':entries},ttl_seconds=3600)
    svc.store.cache_put('fact-semantic-verification-v1',{'routes':entries},ttl_seconds=3600)
    svc.providers.research=provider
    session=SimpleNamespace(id='review',resource_id=job['story_id'],actor=None,closed=False,state={})
    packet=review_packets.read(harness.adapter,session,{'run_id':RUN,'_parallel_candidate_review':True})
    engine=HeadlessFactReview(harness)
    response=await engine._infer(packet,job,'semantic-controlled-unit',{})
    assert calls==['mimo-v2.6-flash-free']+([] if mode in {'unknown','aborted_unknown'} else ['nemotron-3-ultra-free'])
    assert (response is None)==(mode in {'unknown','aborted_unknown','exhausted'})
    if mode == 'exhausted':
        saved = svc.store.checkpoint_get(job['id'], 'headless_fact_review:semantic-controlled-unit')
        assert saved['phase'] == 'exhausted'
        assert await engine._infer(packet, job, 'semantic-controlled-unit', saved) is None
        assert len(calls) == 2
    if mode in {'unknown','aborted_unknown'}:
        saved=svc.store.checkpoint_get(job['id'],'headless_fact_review:semantic-controlled-unit')
        assert await engine._infer(packet,job,'semantic-controlled-unit',saved) is None
        assert len(calls)==1


@pytest.mark.asyncio
async def test_existing_live_tool_contract_reviews_only_with_matching_semantic_qualification(tmp_path, monkeypatch):
    from contextvars import ContextVar
    from street_story.research_adapter import ProductResearchAdapter
    svc, job, harness = await candidates(tmp_path, count=1)
    provider = object.__new__(ProductResearchAdapter)
    provider.service, provider.client, provider.giga = svc, None, None
    provider._active_binding = ContextVar('live-review-test', default=None)
    calls = []
    class Live:
        endpoint, provider_id, model_id = 'live-interaction:street-story', 'google-live', 'gemini-3.8-live'
        async def _run(self, role, prompt, binding, schema):
            calls.append(binding['attempt_id'])
            packet = json.loads(prompt.split('Frozen packet: ', 1)[1])
            args = {'packet_ref': packet['packet_ref'], 'decisions': [
                {'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported',
                 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
                 'claims': [item['text']], 'basis_quotes': [item['quote_ref']], 'reason': 'Own literal frozen passage.'}
                for item in packet['items']], 'relations_complete': True, 'conflicts': [],
                'coverage_complete': False, 'missing_aspects': []}
            receipt = {'binding': binding, 'phase': 'completed', 'model_id': self.model_id,
                       'provider_id': self.provider_id, 'result': args}
            await provider.checkpoint(binding, receipt)
            return {'result': args, 'receipt': receipt}
    provider.live_facts = Live()
    svc.providers.research = provider
    assert await HeadlessFactReview(harness).run(job, RUN, 0) == 0
    assert calls == []
    svc.store.cache_put('fact-semantic-verification-v1', {'routes': [{
        'provider_id': provider.live_facts.provider_id, 'model_id': provider.live_facts.model_id,
        'endpoint': provider.live_facts.endpoint, 'schema_verified': True, 'own_passages_verified': True,
        'qualifier_negative_verified': True, 'nearby_duplicate_verified': True,
        'nearby_conflict_verified': True}]}, ttl_seconds=3600)
    now = svc.store.now()
    monkeypatch.setattr(svc.store, 'now', lambda: now + 61)  # Preserve the original NOT SENT retry checkpoint.
    assert await HeadlessFactReview(harness).run(job, RUN, 0) == 1
    assert len(calls) == 1
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 1
        assert db.execute('SELECT SUM(owner_selected) FROM fact_assertions').fetchone()[0] == 0
