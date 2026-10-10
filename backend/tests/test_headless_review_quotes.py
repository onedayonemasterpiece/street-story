from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from street_story import headless_review_quotes, review_packets
from street_story.headless_fact_review import HeadlessFactReview
from street_story.live import FUNCTIONS
from street_story.service import ConflictError
from test_headless_fact_review_parallel import candidates, qualify_controlled_review, RUN


def packet_fixture():
    return headless_review_quotes.with_quote_catalog({'packet_ref': 'p_frozen', 'items': [
        {'fact': 0, 'evidence': 0, 'offset': 0, 'passage': 'Башня была построена в 1859 году.'},
        {'fact': 0, 'evidence': 1, 'offset': 0, 'passage': 'Дата постройки 1853 год'},
        {'fact': 1, 'evidence': 0, 'offset': 0, 'passage': 'Названа в честь генерала.'}]})


@pytest.mark.parametrize('mutation', ['foreign_fact', 'unselected_evidence', 'unknown', 'other_packet', 'changed_passage', 'changed_catalog'])
def test_quote_labels_cannot_cross_or_change_frozen_scope(mutation):
    packet = packet_fixture()
    label = packet['items'][0]['quote_ref']
    if mutation == 'foreign_fact':
        label = packet['items'][2]['quote_ref']
    elif mutation == 'unselected_evidence':
        label = packet['items'][1]['quote_ref']
    elif mutation == 'unknown':
        label += 'altered'
    elif mutation == 'other_packet':
        label = label.replace('p_frozen', 'p_foreign')
    elif mutation == 'changed_passage':
        packet['items'][0]['passage'] = 'Башня была построена в 1853 году.'
    else:
        packet['quote_catalog'][label]['fact'] = 1
    with pytest.raises(ConflictError) as error:
        headless_review_quotes.resolve_quotes(packet, {'packet_ref': packet['packet_ref'], 'decisions': [
            {'fact': 0, 'evidence': [0], 'basis_quotes': [label]}]})
    assert error.value.code == 'live_fact_review_evidence_invalid'


def test_original_answer_and_legacy_literals_remain_unchanged():
    packet = packet_fixture()
    args = {'packet_ref': packet['packet_ref'], 'decisions': [{'fact': 0, 'evidence': [0],
                          'basis_quotes': [packet['items'][0]['quote_ref'], 'unchanged literal']}]}
    original = deepcopy(args)
    resolved = headless_review_quotes.resolve_quotes(packet, args)
    assert args == original
    assert resolved['decisions'][0]['basis_quotes'] == ['Башня была построена в 1859 году.', 'unchanged literal']
    legacy = {'packet_ref': 'p_original', 'items': []}
    assert headless_review_quotes.resolve_quotes(legacy, args) is args


def test_quote_catalog_cannot_resolve_another_result_packet():
    packet = packet_fixture()
    with pytest.raises(ConflictError):
        headless_review_quotes.resolve_quotes(packet, {'packet_ref': 'p_foreign', 'decisions': [
            {'fact': 0, 'evidence': [0], 'basis_quotes': [packet['items'][0]['quote_ref']]}]})


def test_grouped_model_view_retains_all_competing_own_slices_once_per_assertion():
    packet = packet_fixture()
    for item in packet['items']:
        item['text'] = 'Дата постройки 1859 год.' if item['fact'] == 0 else 'Названа в честь генерала.'
    original = deepcopy(packet)
    view = headless_review_quotes.model_packet(packet)
    assert packet == original and len(view['facts']) == 2
    own = view['facts'][0]
    assert own['fact'] == 0 and len(own['evidence']) == 2
    assert [slice['passage'] for evidence in own['evidence'] for slice in evidence['slices']] == [
        'Башня была построена в 1859 году.', 'Дата постройки 1853 год']
    assert {s['quote_ref'] for fact in view['facts'] for e in fact['evidence'] for s in e['slices']} == set(packet['quote_catalog'])


def test_fresh_private_value_assessment_is_required_and_cannot_support_declared_conflict():
    from jsonschema import Draft202012Validator
    packet = packet_fixture()
    packet['verifier_presentation'] = 'one_assertion_all_own_slices_v1'
    schema = headless_review_quotes.response_schema(packet, public_schema())
    args = answer(packet)
    for decision in args['decisions']:
        decision.pop('own_evidence_values', None)
        decision.pop('own_value_conflicts', None)
    assert not Draft202012Validator(schema).is_valid(args)
    for decision in args['decisions']:
        decision.update(own_evidence_values=[], own_value_conflicts=[])
    assert Draft202012Validator(schema).is_valid(args)
    args['decisions'][0]['fact'] = 2
    assert not Draft202012Validator(schema).is_valid(args)
    args['decisions'][0]['fact'] = 0
    args['decisions'][0]['own_value_conflicts'] = ['The same construction property has two own-source dates.']
    with pytest.raises(ConflictError, match='unresolved own-value conflicts'):
        headless_review_quotes.public_result(packet, args)
    args['decisions'][0]['verdict'] = 'repair_needed'
    public = headless_review_quotes.public_result(packet, args)
    assert 'own_evidence_values' not in public['decisions'][0]
    assert args['decisions'][0]['own_value_conflicts']  # Preserve original private result.


@pytest.mark.parametrize('foreign', [False, True])
def test_review_goal_basis_resolves_only_received_candidate_and_eligible_ids(foreign):
    packet = packet_fixture()
    packet['eligible_facts'] = [{'fact_id': 'known_own', 'text': 'Own eligible proposition.'}]
    items = [{'id': 'new_own', 'text': 'New proposition.'}]
    args = {'research_sufficient': True, 'research_sufficient_basis': {
        'candidate_indices': [0], 'known_fact_ids': ['foreign' if foreign else 'known_own']}}
    assert headless_review_quotes.sufficient_basis(packet, args, items) == (
        [] if foreign else [('new_own', 'New proposition.'), ('known_own', 'Own eligible proposition.')])


def public_schema():
    return next(tool['parameters'] for tool in FUNCTIONS if tool['name'] == 'finalize_fact_review')


def answer(packet):
    return {'packet_ref': packet['packet_ref'], 'decisions': [
        {'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported',
         **({'own_evidence_values': [], 'own_value_conflicts': []}
            if packet.get('verifier_presentation') else {}),
         'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
         'claims': ['One independently selectable claim.'], 'basis_quotes': [item['quote_ref']],
         'reason': 'Own literal selected evidence.'} for item in packet['items']],
        'relations_complete': True, 'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}


def test_private_equivalence_schema_matches_host_optional_pointer_scope():
    from jsonschema import Draft202012Validator
    packet = packet_fixture()
    original = deepcopy(public_schema())
    schema = headless_review_quotes.response_schema(packet, original)
    args = answer(packet)
    validate = Draft202012Validator(schema).is_valid
    assert validate(args)
    args['decisions'][0]['equivalent_to'] = None
    assert validate(args)
    args['decisions'][0]['equivalent_to'] = 1
    assert validate(args)
    for invalid in (-1, 2, '1', True):
        args['decisions'][0]['equivalent_to'] = invalid
        assert not validate(args)
    assert original == public_schema()


def test_private_schema_rejects_candidate_prose_without_changing_public_contract():
    from jsonschema import Draft202012Validator
    packet = packet_fixture()
    original = deepcopy(public_schema())
    private = headless_review_quotes.response_schema(packet, public_schema())
    args = answer(packet)
    assert Draft202012Validator(private).is_valid(args)
    args['decisions'][0]['basis_quotes'] = ['Башня была построена в 1859 году.']
    assert Draft202012Validator(public_schema()).is_valid(args)
    errors = list(Draft202012Validator(private).iter_errors(args))
    assert len(errors) == 1 and errors[0].validator == 'enum'
    assert list(errors[0].path) == ['decisions', 0, 'basis_quotes', 0]
    assert public_schema() == original
    assert headless_review_quotes.response_schema({'packet_ref': 'legacy'}, public_schema()) == original


@pytest.mark.asyncio
async def test_private_original_readback_and_recovery_use_exact_saved_schema(tmp_path, monkeypatch):
    svc, job, harness = await candidates(tmp_path, count=1)
    engine = HeadlessFactReview(harness)
    session = SimpleNamespace(id='headless-review:' + job['id'], resource_id=job['story_id'],
                              actor=None, closed=False, state={})
    with svc.store.connection() as db:
        ids = review_packets.pending_candidates(db, job['story_id'], RUN)
    packet, unit, _ = engine._prepare_packet(job, RUN, session, ids)
    schema = headless_review_quotes.response_schema(packet, public_schema())
    args = answer(packet)
    route = {'provider_id': 'fixture', 'model_id': 'fixture', 'endpoint': 'original',
             'qualified': True, 'available': True}
    calls = []
    async def readback(role, prompt, binding, received_schema):
        assert binding == {'session_id': 'ses_original', 'message_id': 'msg_original'}
        assert received_schema == schema
        assert prompt.startswith('Original frozen prompt. ')
        calls.append('original-addressed-read')
        return {'result': args}
    route['client'] = SimpleNamespace(model_id='fixture', directory='/fixture', _run=readback)
    saved = {'phase': 'unknown', 'packet_ref': packet['packet_ref'], 'frozen_packet': packet,
             'route': 'facts_review_fixture', 'route_identity': engine._route_identity(route),
             'verifier_schema': schema, 'verifier_prompt': 'Original frozen prompt. ',
             'verifier_contract_id': 'original-private-contract'}
    engine._put(job, unit, saved)
    receipt = {'phase': 'submitted', 'binding': {'fact_unit_id': unit},
               'session_id': 'ses_original', 'message_id': 'msg_original'}
    with svc.store.tx() as db:
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
            ('original-submitted', 'original-logical', job['story_id'], 'facts_review_fixture',
             json.dumps(receipt), svc.store.now(), svc.store.now()))
    async def run(story, role, received_unit, invoke, *, client):
        assert received_unit == unit
        return await invoke({'session_id': 'ses_original', 'message_id': 'msg_original'})
    svc.providers.research = SimpleNamespace(run=run)
    monkeypatch.setattr(engine, '_qualified_routes', lambda available=True: [route])
    monkeypatch.setattr(headless_review_quotes, 'response_schema',
                        lambda *a: pytest.fail('Original schema must not be regenerated.'))
    assert await engine._infer(packet, job, unit, {**saved, 'phase': 'observe_original'}) == args
    assert calls == ['original-addressed-read']
    result = svc.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit)
    assert result['verifier_schema'] == schema and result['args'] == args

    engine._put(job, unit, saved)
    paraphrase = deepcopy(args)
    paraphrase['decisions'][0]['basis_quotes'] = ['Candidate prose is not a quote label.']
    receipt.update(phase='completed', result=paraphrase)
    with svc.store.tx() as db:
        db.execute('UPDATE research_provider_attempts SET receipt_json=?', (json.dumps(receipt),))
    engine._recover_closed_reviews(job)
    assert svc.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit)['phase'] == 'unknown'
    receipt['result'] = args
    with svc.store.tx() as db:
        db.execute('UPDATE research_provider_attempts SET receipt_json=?', (json.dumps(receipt),))
    engine._recover_closed_reviews(job)
    recovered = svc.store.checkpoint_get(job['id'], 'headless_fact_review:' + unit)
    assert recovered['phase'] == 'result' and recovered['verifier_schema'] == schema
    assert calls == ['original-addressed-read']


@pytest.mark.parametrize('pointer', ['absent', 'null', 'self'])
@pytest.mark.asyncio
async def test_label_review_commits_own_literals_without_rewriting_closed_answer(tmp_path, pointer):
    svc, job, harness = await candidates(tmp_path, count=2)
    route = qualify_controlled_review(harness)
    answers = []
    class LabelReview(HeadlessFactReview):
        async def _infer(self, packet, job, unit, saved, ordinal=0):
            args = {'packet_ref': packet['packet_ref'], 'decisions': [
                {'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported',
                 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
                 'claims': [item['text']], 'basis_quotes': [item['quote_ref']]}
                for item in packet['items']], 'relations_complete': True, 'conflicts': [],
                'coverage_complete': False, 'missing_aspects': []}
            if pointer != 'absent':
                for decision in args['decisions']:
                    decision['equivalent_to'] = None if pointer == 'null' else decision['fact']
            answers.append(deepcopy(args))
            self._put(job, unit, {'phase': 'result', 'args': args, 'frozen_packet': packet,
                                 'route_identity': self._route_identity(route)})
            return args
    assert await LabelReview(harness).run(job, RUN, 0) == 1
    assert all(d['basis_quotes'][0].startswith(headless_review_quotes.PREFIX) for d in answers[0]['decisions'])
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 2
        assert db.execute('SELECT SUM(owner_selected) FROM fact_assertions').fetchone()[0] == 0
        row = db.execute('SELECT decisions_json FROM live_review_packets WHERE result_json IS NOT NULL').fetchone()
        assert '@own_quote:' not in row[0]


@pytest.mark.parametrize('invalid', ['paraphrase', 'multiple_claims', 'failed_qualifiers'])
@pytest.mark.asyncio
async def test_valid_labels_do_not_bypass_actual_literal_or_semantic_guards(tmp_path, invalid):
    svc, job, harness = await candidates(tmp_path, count=1)
    session = SimpleNamespace(id='literal-guard', resource_id=job['story_id'], actor=None,
                              closed=False, model='fixture', state={})
    packet = headless_review_quotes.with_quote_catalog(review_packets.read(harness.adapter, session, {'run_id': RUN}))
    item = packet['items'][0]
    decision = {'fact': 0, 'evidence': [0], 'verdict': 'supported', 'atomic': True,
                'support_complete': True, 'qualifiers_preserved': True,
                'claims': [item['text']], 'basis_quotes': [item['quote_ref']]}
    if invalid == 'paraphrase':
        decision['basis_quotes'] = ['Invented source paraphrase.']
    elif invalid == 'multiple_claims':
        decision['claims'].append('Another independently selectable role.')
    else:
        decision['qualifiers_preserved'] = False
    args = {'packet_ref': packet['packet_ref'], 'decisions': [decision], 'relations_complete': True,
            'conflicts': [], 'coverage_complete': False, 'missing_aspects': []}
    with pytest.raises(ConflictError):
        await harness.adapter.execute_tool(session, {'name': 'finalize_fact_review', 'id': 'guard',
            'args': headless_review_quotes.resolve_quotes(packet, args)})
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE eligibility='eligible'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM poi_research_assertions WHERE eligibility='eligible'").fetchone()[0] == 0
