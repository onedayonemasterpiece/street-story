"""Declared conflicts/claims and own quotes cannot contradict positive commit."""
import copy
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from street_story.fact_page_boundary import isolate_rows
from street_story.headless_facts import HeadlessFacts
from street_story.research_adapter import FACT_PAGE_SCHEMA
from street_story.source_assessment import CONTRACT, LEGACY_CONTRACT, public_fact, response_schema, source_first_input
from test_headless_fact_pool import fixture, partial, result

SCHEMA = response_schema(FACT_PAGE_SCHEMA, CONTRACT)
PASSAGES = {0: 'Дата постройки 1853 год. Башня была построена в 1859 году.',
            1: 'Диаметр 34 метра. Высота 12 метров.', 2: 'В 1944 году внутри были размещены склады.'}


def test_source_first_input_preserves_exact_evidence_and_index_and_legacy_order():
    capsule = {'context': {'known_inventory_complete': False, 'known_inventory_next_offset': 1},
        'known_fact_inventory': [{'fact_id': 'old', 'text': 'A prior assertion.', 'eligibility': 'eligible'}],
        'semantic_contract': CONTRACT,
        'sources': [{'source_version_id': 'own', 'passages': [{'passage_id': 0, 'text': PASSAGES[0]}]}]}
    original = copy.deepcopy(capsule)
    actual = source_first_input(capsule)
    assert json.loads(json.dumps(actual)) == original == capsule
    assert list(actual)[0] == 'sources'
    capsule['semantic_contract'] = LEGACY_CONTRACT
    assert source_first_input(capsule) is capsule and list(capsule)[0] == 'context'


@pytest.mark.asyncio
async def test_live_source_body_precedes_prior_claims_in_actual_operation_context(tmp_path):
    from test_autonomous_live_research import binding, client
    from test_mvp_research import service
    svc, _, story = service(tmp_path)
    payload = {'facts': [row()], 'source_matches_poi': True,
        'source_content_valid': True, 'continuation_needed': False}
    provider, calls = client(svc, 'valid', tool_args=payload)
    initialized = []
    factory = provider.host_factory
    def capture(*args, **kwargs):
        host = factory(*args, **kwargs)
        original_start = host.start
        async def start(**start_args):
            result = await original_start(**start_args)
            initialized.append(host.initialized)
            return result
        host.start = start
        return host
    provider.host_factory = capture
    capsule = {'context': {'coverage_goal': 'Read own facts.'},
        'known_fact_inventory': [{'fact_id': 'old', 'text': 'Prior compound claim.', 'eligibility': 'eligible'}],
        'semantic_contract': CONTRACT, 'jsonschema': SCHEMA,
        'sources': [{'source_version_id': 'own', 'passages': [{'passage_id': 2, 'text': PASSAGES[2]}]}]}
    original = copy.deepcopy(capsule)
    closed = await provider.extract_facts(capsule, {**binding(svc, story), 'semantic_contract': CONTRACT})
    prompt = initialized[0]['context']['frozen_research_operation']['prompt']
    serialized = prompt.split('Frozen source unit:\n', 1)[1]
    assert serialized.index('"sources"') < serialized.index('"known_fact_inventory"')
    assert json.loads(serialized) == {key: value for key, value in original.items() if key != 'jsonschema'}
    assert capsule == original and closed['result'] == payload
    assert [name for name, _ in calls] == ['start', 'input', 'stop']


def row(text='В 1944 году внутри были размещены склады.', pid=2, key='warehouse'):
    return {'text':text,'claim_key':key,'existing_fact_id':'','confidence':.95,
        'passage_ids':[pid], 'verdict':'supported','atomic':True,'support_complete':True,
        'qualifiers_preserved':True,'review_reason':'Own dated statement.', 'claims':[text],
        'own_evidence_values':[{'property':'use','value':'склады','passage_ids':[pid],
            'quotes':[PASSAGES[pid]]}], 'own_value_conflicts':[]}


def isolate(rows):
    return isolate_rows({'facts':rows, 'source_matches_poi':True,'source_content_valid':True,
        'continuation_needed':False},SCHEMA,passage_ids=set(PASSAGES),passages=PASSAGES)


@pytest.mark.parametrize('defect', ['multiple_claims','declared_conflict','foreign_value_passage','foreign_quote','missing_enumeration'])
def test_bad_semantic_declaration_is_local_and_does_not_promote_dependent_row(defect):
    bad=row('Дата постройки 1853 год.',0,'date')
    if defect=='multiple_claims':
        bad['claims']=['Дата постройки 1853 год.','Название дано в честь человека.']
    elif defect=='declared_conflict':
        bad['own_value_conflicts']=['Инфобокс 1853 / проза 1859: стадия не объяснена.']
    elif defect=='foreign_value_passage':
        bad['own_evidence_values'][0]['passage_ids']=[1]
    elif defect=='foreign_quote':
        bad['own_evidence_values'][0]['quotes']=[PASSAGES[1]]
    else:
        bad.pop('own_evidence_values')
    dependent={**row('Другой текст.',0,'date')}
    original=copy.deepcopy([bad,dependent,row()])
    isolated=isolate(original)
    assert isolated['row_isolation']['rejected_indices']==[0]
    assert isolated['row_isolation']['dependent_indices']==[1]
    assert isolated['row_isolation']['accepted_indices']==[2]
    assert isolated['result']['facts']==[original[2]]
    assert isolated['raw_result']['facts']==original


def test_explicit_unresolved_conflict_stays_nonpositive_and_own_values_remain_intact():
    candidate=row('Источник даёт даты постройки 1853 и 1859 без объяснения.',0)
    candidate.update(verdict='possible_conflict',own_value_conflicts=['1853 / 1859'])
    candidate['own_evidence_values']=[
        {'property':'construction','value':str(year),'passage_ids':[0],'quotes':[quote]}
        for year,quote in [(1853,'Дата постройки 1853 год.'),(1859,'Башня была построена в 1859 году.')]]
    assert isolate([candidate])['result']['facts']==[candidate]
    assert public_fact(candidate)['verdict']=='possible_conflict'
    assert 'claims' not in public_fact(candidate)


def test_old_closed_reading_schema_is_preserved_without_new_fields():
    candidate=public_fact(row())
    legacy=response_schema(FACT_PAGE_SCHEMA,LEGACY_CONTRACT)
    assert legacy is FACT_PAGE_SCHEMA
    assert Draft202012Validator(legacy['properties']['facts']['items']).is_valid(candidate)
    assert not Draft202012Validator(SCHEMA['properties']['facts']['items']).is_valid(candidate)


@pytest.mark.asyncio
async def test_v3_onepass_commits_own_claim_without_second_review_and_reopens(tmp_path):
    svc,job=fixture(tmp_path,count=1)
    calls=[]
    async def extract(page,story,context):
        calls.append(page['_unit_id'])
        response=result(page)
        fact=response['result']['facts'][0]
        own=page['evidence_passages'][0]
        fact.update(claims=[fact['text']],own_value_conflicts=[],own_evidence_values=[{
            'property':'use','value':'exhibit','passage_ids':[own['passage_id']],'quotes':[own['text']]}])
        response['receipt']['binding']={'semantic_contract':CONTRACT}
        return response
    svc.providers.research=SimpleNamespace(client=SimpleNamespace(model_id='controlled-model'),extract_fact_page=extract)
    await partial(HeadlessFacts(svc),job)
    reopened=type(svc)(svc.settings,providers=svc.providers)
    facts=reopened.story(job['story_id'])['facts']
    assert len(calls)==1 and len(facts)==1 and facts[0]['eligibility']=='eligible' and not facts[0]['selected']
    with reopened.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM live_review_packets').fetchone()[0]==0
        raw=json.loads(db.execute("SELECT value_json FROM research_checkpoints WHERE stage LIKE 'headless_fact_result:%'").fetchone()[0])
        assert raw['extracted']['result']['facts'][0]['own_evidence_values'][0]['quotes']


@pytest.mark.asyncio
async def test_shared_live_v3_keeps_one_send_and_original_bad_semantic_row(tmp_path):
    from test_autonomous_live_research import binding, client
    from test_mvp_research import service
    svc,_,story=service(tmp_path)
    bad=row('Дата постройки 1853 год.',0,'date')
    bad['own_value_conflicts']=['1853 / 1859: собственный источник не объясняет стадии.']
    raw={'facts':[bad,row()], 'source_matches_poi':True,'source_content_valid':True,'continuation_needed':False}
    provider,calls=client(svc,'valid',tool_args=raw)
    closed=await provider._run('facts','Frozen exact own source',
        {**binding(svc,story),'semantic_contract':CONTRACT},SCHEMA)
    assert closed['result']['facts']==[raw['facts'][1]]
    assert closed['receipt']['raw_result']==raw and closed['receipt']['text_sends']==1
    assert closed['receipt']['row_isolation']['rejected_indices']==[0]
    assert [name for name,_ in calls]==['start','input','stop']
