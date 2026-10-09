from copy import deepcopy
from types import SimpleNamespace

import pytest

from street_story import headless_review_quotes, review_packets
from street_story.headless_fact_review import HeadlessFactReview
from street_story.service import ConflictError
from test_headless_fact_review_parallel import candidates, RUN


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


@pytest.mark.asyncio
async def test_label_review_commits_own_literals_without_rewriting_closed_answer(tmp_path):
    svc, job, harness = await candidates(tmp_path, count=2)
    answers = []
    class LabelReview(HeadlessFactReview):
        async def _infer(self, packet, job, unit, saved, ordinal=0):
            args = {'packet_ref': packet['packet_ref'], 'decisions': [
                {'fact': item['fact'], 'evidence': [item['evidence']], 'verdict': 'supported',
                 'atomic': True, 'support_complete': True, 'qualifiers_preserved': True,
                 'claims': [item['text']], 'basis_quotes': [item['quote_ref']]}
                for item in packet['items']], 'relations_complete': True, 'conflicts': [],
                'coverage_complete': False, 'missing_aspects': []}
            answers.append(deepcopy(args))
            self._put(job, unit, {'phase': 'result', 'args': args, 'frozen_packet': packet})
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
