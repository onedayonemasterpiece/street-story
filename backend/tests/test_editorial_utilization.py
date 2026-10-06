import json

import pytest

from street_story.fact_ledger import backfill_legacy_fact_ledger, set_owner_selection
from street_story.service import InvalidStateError
from tools.editorial_utilization_acceptance import assess, frozen_corpus, seed_story
from test_live_editor import make_service, mark_identity_ready


def rich_service(tmp_path):
    service, adapter, session, events = make_service(tmp_path)
    mark_identity_ready(service, session.resource_id)
    with service.store.tx() as db:
        for index in range(9):
            db.execute('INSERT INTO facts VALUES(?,?,?,?,?,?,?)',
                (session.resource_id, f'f{index}', f'Claim {index}', .95, 1, int(index < 4),
                 json.dumps([{'url': 'https://example.org/source', 'supports': [{'kind': 'page_excerpt', 'text': f'Claim {index}'}]}])))
        backfill_legacy_fact_ledger(db, service.store.now())
        # This fixture starts after semantic acceptance; the tests concern
        # editorial authorization, not candidate promotion.
        db.execute("UPDATE fact_assertions SET review_status='eligible',eligibility='eligible' WHERE story_id=?", (session.resource_id,))
        db.execute("UPDATE stories SET draft_text='Saved draft' WHERE id=?", (session.resource_id,))
    return service, adapter, session, events


@pytest.mark.asyncio
async def test_visual_rejects_eligible_unselected_fact_before_dispatch(tmp_path):
    service, adapter, session, _events = rich_service(tmp_path)
    called = []

    async def forbidden(*args):
        called.append(args)
        raise AssertionError('must not dispatch')

    service.request_visual = forbidden
    with pytest.raises(InvalidStateError) as error:
        await adapter.execute_tool(session, {'id': 'unselected', 'name': 'generate_visual',
            'args': {'fact_ids': ['f0', 'f8']}})
    assert error.value.code == 'visual_fact_not_selected'
    assert not called
    with service.store.connection() as db:
        assert not db.execute("SELECT 1 FROM live_commands WHERE command_id='unselected'").fetchone()


@pytest.mark.asyncio
async def test_visual_can_freeze_subset_without_changing_rich_owner_selection_or_draft(tmp_path):
    service, adapter, session, _events = rich_service(tmp_path)
    await adapter.execute_tool(session, {'id': 'subset', 'name': 'generate_visual', 'args': {'fact_ids': ['f1', 'f3']}})
    story = service.story(session.resource_id)
    assert [f['fact_id'] for f in story['facts'] if f['selected']] == ['f0', 'f1', 'f2', 'f3']
    assert story['draft_text'] == 'Saved draft'
    with service.store.connection() as db:
        visual = json.loads(service._story_row(db, session.resource_id)['visual_context_json'])
        assert [f['fact_id'] for f in visual['selected_facts']] == ['f1', 'f3']


@pytest.mark.asyncio
async def test_observe_subset_uses_frozen_ids_and_blocks_removed_selection(tmp_path):
    service, adapter, session, _events = rich_service(tmp_path)
    with service.store.tx() as db:
        db.execute('UPDATE stories SET visual_context_json=? WHERE id=?',
            (json.dumps({'selected_facts': [{'fact_id': 'f1'}, {'fact_id': 'f3'}]}), session.resource_id))
    observed = []

    async def observe(story_id, key, body):
        observed.append(body)
        return service.story(story_id)

    service.request_visual = observe
    await adapter.execute_tool(session, {'id': 'observe-subset', 'name': 'generate_visual',
        'args': {'observe_existing_visual': True}})
    assert observed[0]['selected_fact_ids'] == ['f1', 'f3']
    with service.store.tx() as db:
        set_owner_selection(db, session.resource_id, ['f0', 'f1'], service.store.now())
    with pytest.raises(InvalidStateError) as error:
        await adapter.execute_tool(session, {'id': 'observe-removed', 'name': 'generate_visual',
            'args': {'observe_existing_visual': True}})
    assert error.value.code == 'visual_fact_not_selected'
    assert len(observed) == 1


def test_existing_poi_story_history_is_compact_context_not_a_selection(tmp_path):
    service, adapter, session, _events = rich_service(tmp_path)
    with service.store.tx() as db:
        row = service._story_row(db, session.resource_id)
        research = json.loads(row['research_json'])
        research['publication_concept'] = 'Previously selected publication angle'
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
    corpus = frozen_corpus(service.store.path, session.resource_id)
    next_story = seed_story(service, corpus)
    context = adapter.initialize(resource_id=next_story, actor=None, model='controlled')['context']
    previous = context['previous_editorial_context']
    assert previous[0]['story_id'] == session.resource_id
    assert previous[0]['concept'] == research['publication_concept']
    assert set(previous[0]['selected_fact_ids']) == {'f0', 'f1', 'f2', 'f3'}
    assert not context['selected_fact_ids']
    assert context['publication_concept'] is None
    assert len(corpus['facts']) == 9
    assert set(corpus) == {'source_story_id', 'identity', 'place_name', 'prior_concept',
        'facts', 'fact_assertions', 'fact_observations', 'fact_evidence_spans'}


def test_counts_and_different_ids_cannot_grant_semantic_acceptance():
    scenario = {'selected_fact_ids': ['a', 'b', 'c'], 'draft': 'Unsupported claim', 'concept': 'New angle'}
    receipt = {'mechanical_pass': True, 'scenarios': [scenario, scenario]}
    review = {**scenario, 'source_backed_diverse_selection': True,
        'draft_uses_only_selected_facts': False, 'reason': 'Draft adds an unsupported claim.'}
    assert assess(receipt, {'scenarios': [review, review]})['status'] == 'FAIL_SEMANTIC'
    review['draft_uses_only_selected_facts'] = True
    assert assess(receipt, {'scenarios': [review, review]})['status'] == 'PASS'
    review['draft'] = 'Changed after review'
    assert assess(receipt, {'scenarios': [review, review]})['status'] == 'FAIL_SEMANTIC'
