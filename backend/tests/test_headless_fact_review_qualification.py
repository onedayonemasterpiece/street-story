"""Reviewer proof is independent of extraction transport and route preference."""
from types import SimpleNamespace

import pytest

from street_story.headless_fact_review import HeadlessFactReview
from street_story import headless_review_quotes, review_packets
from street_story.live import FUNCTIONS
from street_story.research_adapter import ProductResearchAdapter
from street_story.opencode_research import ResearchUnavailable
from test_headless_fact_review_parallel import candidates
from test_headless_fact_pool import RUN
from test_live_editor import make_service


FLAGS = ('schema_verified', 'own_passages_verified', 'qualifier_negative_verified',
         'nearby_duplicate_verified', 'nearby_conflict_verified')
EXTRACTION_FLAGS = ('semantic_contract_verified', 'source_subject_negative_verified',
                    'planned_modality_verified', 'known_claim_reuse_verified')


def route(role='facts_live'):
    live = role == 'facts_live'
    client = SimpleNamespace(provider_id='google-live' if live else 'opencode',
        model_id='gemini-3.8-live' if live else 'mimo-v2.6-flash-free',
        endpoint='live-interaction:street-story' if live else 'http://127.0.0.1:4097',
        directory=None if live else '/existing/research')
    return {'role': role, 'provider_id': client.provider_id, 'model_id': client.model_id,
            'endpoint': client.endpoint, 'client': client, 'qualified': True, 'available': True}


def proof(route):
    return {**{key: route[key] for key in ('provider_id', 'model_id', 'endpoint')},
            'directory': route['client'].directory, **dict.fromkeys(FLAGS, True)}


def engine(routes, entries):
    provider = SimpleNamespace(_fact_pool_routes=lambda: routes)
    store = SimpleNamespace(cache_get=lambda key: {'routes': entries}
                            if key == 'fact-semantic-verification-v1' else None)
    return HeadlessFactReview(SimpleNamespace(service=SimpleNamespace(store=store,
                                                    providers=SimpleNamespace(research=provider))))


@pytest.mark.parametrize('role', ['facts_live', 'facts'])
def test_extraction_qualification_alone_never_admits_semantic_reviewer(role):
    selected = route(role)
    assert engine([selected], [])._qualified_routes() == []
    assert engine([selected], [])._qualified_routes(available=False) == []


@pytest.mark.parametrize('flag', FLAGS)
@pytest.mark.parametrize('value', [False, None, 1])
def test_live_review_requires_every_explicit_semantic_control(flag, value):
    selected = route()
    entry = proof(selected)
    entry[flag] = value
    assert engine([selected], [entry])._qualified_routes() == []


@pytest.mark.parametrize('key,value', [('provider_id', 'another-provider'), ('model_id', 'another-model'),
                                     ('endpoint', 'another-endpoint'), ('directory', '/other/profile')])
def test_review_proof_cannot_transfer_to_another_route(key, value):
    selected = route()
    entry = proof(selected)
    entry[key] = value
    assert engine([selected], [entry])._qualified_routes() == []


def test_explicit_matching_live_semantic_proof_allows_review_and_retains_health_wait():
    selected = route()
    entry = proof(selected)
    reviewer = engine([selected], [entry])
    assert reviewer._qualified_routes() == [selected]
    selected['available'] = False
    assert reviewer._qualified_routes() == []
    assert reviewer._qualified_routes(available=False) == [selected]


def test_matching_existing_text_proof_remains_available_without_live_proof():
    live, text = route(), route('facts')
    assert engine([live, text], [proof(text)])._qualified_routes() == [text]


def test_live_first_extraction_and_qualified_text_review_use_same_existing_pool(tmp_path):
    service, _, _, _ = make_service(tmp_path)
    provider = object.__new__(ProductResearchAdapter)
    provider.service = service
    provider.live_facts = route()['client']
    provider.client = route('facts')['client']
    extra = SimpleNamespace(**{**vars(provider.client), 'model_id': 'nemotron-3-ultra-free'})
    provider._fact_extractor_clients = {extra.model_id: extra}
    provider.giga = None
    text_clients = [provider.client, extra]
    service.store.cache_put('research-text-verification-v1', {'extractors': [
        {**vars(client), **dict.fromkeys(EXTRACTION_FLAGS, True)} for client in text_clients]}, ttl_seconds=3600)
    service.store.cache_put('fact-semantic-verification-v1', {'routes': [
        {**vars(client), **dict.fromkeys(FLAGS, True)} for client in text_clients]}, ttl_seconds=3600)
    service.providers.research = provider

    pool = [item for item in provider._fact_pool_routes() if item['qualified']]
    assert pool[0]['role'] == 'facts_live'
    assert provider.order_fact_routes(pool, review=False)[0]['role'] == 'facts_live'
    reviewers = HeadlessFactReview(SimpleNamespace(service=service))._qualified_routes()
    assert [item['model_id'] for item in reviewers] == ['mimo-v2.6-flash-free', 'nemotron-3-ultra-free']


async def original_fixture(tmp_path):
    import json
    svc, job, harness = await candidates(tmp_path, count=1)
    reviewer = HeadlessFactReview(harness)
    session = SimpleNamespace(id='headless-review:' + job['id'], resource_id=job['story_id'],
                              actor=None, closed=False, state={})
    with svc.store.connection() as db:
        ids = review_packets.pending_candidates(db, job['story_id'], RUN)
    packet, unit, _ = reviewer._prepare_packet(job, RUN, session, ids)
    schema = headless_review_quotes.response_schema(packet,
        next(tool['parameters'] for tool in FUNCTIONS if tool['name'] == 'finalize_fact_review'))
    live = route()
    args = {'packet_ref': packet['packet_ref'], 'decisions': [
        {'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported',
         'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
         'claims': [item['text']], 'basis_quotes': [item['quote_ref']], 'reason': 'Own literal evidence.'}
        for item in packet['items']], 'relations_complete': True, 'conflicts': [],
        'coverage_complete': False, 'missing_aspects': []}
    saved = {'phase': 'unknown', 'packet_ref': packet['packet_ref'], 'frozen_packet': packet,
        'route': 'facts_review_' + live['model_id'], 'route_identity': reviewer._route_identity(live),
        'verifier_prompt': 'Exact original prompt. Frozen packet: ', 'verifier_schema': schema,
        'verifier_contract_id': 'historical-frozen-contract'}
    reviewer._put(job, unit, saved)
    receipt = {'phase': 'submitted', 'binding': {'fact_unit_id': unit},
               'session_id': 'ses_original', 'message_id': 'msg_original'}
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('original-read', 'original-logical', job['story_id'], saved['route'],
             json.dumps(receipt), svc.store.now(), svc.store.now()))
    return svc, job, harness, reviewer, packet, unit, saved, live, receipt, args


@pytest.mark.parametrize('displaced', [None, 'endpoint', 'model_id', 'directory'])
@pytest.mark.asyncio
async def test_unqualified_unknown_observes_only_exact_original_and_never_replaces(tmp_path, displaced):
    import json
    from street_story.service import canonical
    svc, job, harness, reviewer, packet, unit, saved, live, _, _ = await original_fixture(tmp_path)
    original_reads, fresh_sends = [], []
    async def observe(role, prompt, binding, schema):
        assert role == 'facts' and prompt == saved['verifier_prompt'] + canonical(packet)
        assert schema == saved['verifier_schema']
        assert binding == {'phase': 'submitted', 'session_id': 'ses_original', 'message_id': 'msg_original'}
        original_reads.append(binding)
        raise ResearchUnavailable('original_outcome_unknown')
    live['client']._run = observe
    text = route('facts')
    async def fresh(*args):
        fresh_sends.append(args)
        pytest.fail('An original UNKNOWN must not authorize a replacement.')
    text['client']._run = fresh
    extraction = svc.providers.research.extract_fact_page
    async def run(story, role, received_unit, invoke, *, client):
        assert received_unit == unit and role == saved['route'] and client is live['client']
        return await invoke({'phase': 'submitted', 'session_id': 'ses_original', 'message_id': 'msg_original'})
    svc.providers.research = SimpleNamespace(_fact_pool_routes=lambda: [live, text], run=run,
                                            extract_fact_page=extraction)
    svc.store.cache_put('fact-semantic-verification-v1', {'routes': [proof(text)]}, ttl_seconds=3600)
    if displaced == 'directory':
        live['client'].directory = '/changed/profile'
    elif displaced:
        live[displaced] = 'changed-original-route'

    assert await reviewer.run(job, RUN, 0) == 0
    assert await reviewer.run(job, RUN, 0) == 0
    assert fresh_sends == []
    assert len(original_reads) == (2 if displaced is None else 0)
    remaining = svc.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit)
    assert remaining['phase'] == 'unknown'
    for key in ('frozen_packet', 'route_identity', 'verifier_prompt', 'verifier_contract_id', 'verifier_schema'):
        assert remaining[key] == saved[key]
    with svc.store.connection() as db:
        raw = json.loads(db.execute('SELECT receipt_json FROM research_provider_attempts WHERE attempt_id=?',
                                    ('original-read',)).fetchone()[0])
        assert raw['session_id'] == 'ses_original' and raw['message_id'] == 'msg_original'
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 0


@pytest.mark.parametrize('closed_path', ['readback', 'recovery', 'saved_result'])
@pytest.mark.asyncio
async def test_unqualified_closed_original_is_technical_only_and_finishes_without_support(tmp_path, closed_path):
    import json
    svc, job, harness, reviewer, packet, unit, saved, live, receipt, args = await original_fixture(tmp_path)
    reads = []
    async def original(role, prompt, binding, schema):
        assert schema == saved['verifier_schema']
        reads.append(binding)
        return {'result': args}
    live['client']._run = original
    extraction = svc.providers.research.extract_fact_page
    async def run(story, role, received_unit, invoke, *, client):
        assert received_unit == unit and role == saved['route']
        return await invoke({'phase': 'submitted', 'session_id': 'ses_original', 'message_id': 'msg_original'})
    svc.providers.research = SimpleNamespace(_fact_pool_routes=lambda: [live], run=run,
                                            extract_fact_page=extraction)
    if closed_path == 'readback':
        assert await reviewer._infer(packet, job, unit, {**saved, 'phase': 'observe_original'}) is None
    elif closed_path == 'recovery':
        receipt.update(phase='completed', result=args, model_id=live['model_id'])
        with svc.store.tx() as db:
            db.execute('UPDATE research_provider_attempts SET receipt_json=? WHERE attempt_id=?',
                       (json.dumps(receipt), 'original-read'))
        reviewer._recover_closed_reviews(job)
    else:
        reviewer._put(job, unit, {**saved, 'phase': 'result', 'args': args})
        assert await reviewer.run(job, RUN, 0) == 0
    closed = svc.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit)
    assert closed['phase'] == 'exhausted' and closed['technical_only'] is True
    assert closed['args'] == args
    for key in ('frozen_packet', 'route_identity', 'verifier_prompt', 'verifier_contract_id', 'verifier_schema'):
        assert closed[key] == saved[key]
    assert len(reads) == (1 if closed_path == 'readback' else 0)
    assert await reviewer.run(job, RUN, 0) == 0  # Closed exact unit cannot dispatch a replacement.
    assert len(reads) == (1 if closed_path == 'readback' else 0)
    outcome = await harness.run(job, RUN, 'History', 'history')
    assert outcome['outcome'] == 'no_supported_facts'
    assert outcome['coverage_complete'] is False and outcome['eligible_count'] == 0
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 0
        assert db.execute('SELECT SUM(owner_selected) FROM fact_assertions').fetchone()[0] == 0
        assert {row[0] for row in db.execute('SELECT eligibility FROM fact_assertions')} == {'unreviewed'}


@pytest.mark.asyncio
async def test_withdrawn_review_proof_before_commit_cannot_promote_valid_closed_answer(tmp_path):
    from test_headless_fact_review_parallel import qualify_controlled_review
    svc, job, harness = await candidates(tmp_path, count=1)
    controlled = qualify_controlled_review(harness)
    class WithdrawnReview(HeadlessFactReview):
        async def _infer(self, packet, job, unit, saved, ordinal=0):
            args = {'packet_ref': packet['packet_ref'], 'decisions': [
                {'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported',
                 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
                 'claims': [item['text']], 'basis_quotes': [item['quote_ref']]}
                for item in packet['items']], 'relations_complete': True, 'conflicts': [],
                'coverage_complete': False, 'missing_aspects': []}
            self._put(job, unit, {'phase': 'result', 'args': args, 'packet_ref': packet['packet_ref'],
                                 'route_identity': self._route_identity(controlled)})
            svc.store.cache_put('fact-semantic-verification-v1', {'routes': []}, ttl_seconds=3600)
            return args
    assert await WithdrawnReview(harness).run(job, RUN, 0) == 0
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 0
    assert not harness._unreviewed_actionable(job, RUN)
