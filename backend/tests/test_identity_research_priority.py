"""Research narrowing keeps reserve, provenance and independent HTTP progress."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from street_story.identity_candidate_policy import (
    physical_research_priority, order_research_candidates,
)
from street_story import identity_architectural_context as context


def inputs():
    bodies = [{'candidate_id': f'osm:way:{i}', 'map_object': {'tags': {'building': 'yes'}}}
        for i in (2, 3, 4)]
    story = {'id': 's', 'photo_sha256': 'a' * 64, '_identity_generation': 0}
    receipt = {'source_photo_sha256': 'a' * 64, 'joint_image_input': True,
        'original_source_sha256': 'a' * 64, 'map_image_sha256': 'b' * 64}
    return story, bodies, receipt


def guidance(ids):
    return {'candidate_ids': ids, 'reason': 'Two bodies fit the cropped bay and gable.',
        'next_question': 'Need a view of the portal to distinguish the bodies.',
        'next_step': 'existing_images', 'contradictions': []}


def test_reduction_retains_unexamined_reserve_and_never_accepts_singleton():
    story, bodies, receipt = inputs()
    selected = guidance(['osm:way:3'])
    state = physical_research_priority(story, {'research_priority': selected}, bodies, receipt)
    assert state['active_candidate_ids'] == ['osm:way:3']
    assert state['reserve_candidate_ids'] == ['osm:way:2', 'osm:way:4']
    assert not state['identity_established']
    assert state['map_sha256'] == receipt['map_image_sha256']
    assert [c['candidate_id'] for c in order_research_candidates(bodies, state)] == [
        'osm:way:3', 'osm:way:2', 'osm:way:4']
    assert state['next_question'] == selected['next_question']


def test_t_can_revise_g_or_restore_reserve_in_same_model_answer():
    story, bodies, receipt = inputs()
    payload = {'research_priority': guidance(['osm:way:2', 'osm:way:3']),
        'accepted_architectural_text': {'decision': 'uncertain', 'research_priority': guidance(['osm:way:4'])}}
    state = physical_research_priority(story, payload, bodies, receipt)
    assert state['active_candidate_ids'] == ['osm:way:4']
    payload['accepted_architectural_text']['research_priority'].update(candidate_ids=[], next_step='expand_reserve')
    restored = physical_research_priority(story, payload, bodies, receipt)
    assert restored['reserve_candidate_ids'] == [b['candidate_id'] for b in bodies]


def test_legacy_nominations_are_exploratory_not_new_model_reduction():
    story, bodies, receipt = inputs()
    payload = {'observed_candidate_ids': ['osm:way:3'], 'spatial_hypotheses': [
        {'candidate_id': 'osm:way:2', 'support_status': 'plausible'},
        {'candidate_id': 'osm:way:4', 'support_status': 'contradicted'}]}
    state = physical_research_priority(story, payload, bodies, receipt)
    assert state['active_candidate_ids'] == ['osm:way:3', 'osm:way:2']
    assert not state['explicit_model_selection'] and state['contradictions'] == []
    assert state['reserve_candidate_ids'] == ['osm:way:4']


@pytest.mark.parametrize('defect', ['foreign_id', 'changed_photo', 'no_pixels', 'unreceived_article'])
def test_priority_cannot_rebind_source_or_invent_physical_or_material_ids(defect):
    story, bodies, receipt = inputs()
    selected = guidance(['osm:way:2'])
    if defect == 'foreign_id':
        selected['candidate_ids'] = ['osm:way:99']
    elif defect == 'changed_photo':
        receipt['source_photo_sha256'] = 'c' * 64
    elif defect == 'no_pixels':
        receipt['joint_image_input'] = False
    else:
        selected['contradictions'] = [{'candidate_id': 'osm:way:3', 'scope': 'article',
            'source_url': 'https://unreceived.example/article', 'reason': 'Different wing.', 'conditions': 'This article only.'}]
    assert physical_research_priority(story, {'research_priority': selected}, bodies, receipt) is None


def test_failed_facade_material_does_not_reject_body_or_other_wing():
    story, bodies, receipt = inputs()
    receipt['articles'] = [{'url': 'https://example.org/wing'}]
    selected = guidance(['osm:way:2', 'osm:way:3'])
    selected['contradictions'] = [{'candidate_id': 'osm:way:2', 'scope': 'facade',
        'source_url': 'https://example.org/wing', 'reason': 'Portal belongs to another wing.',
        'conditions': 'Only this article view of the western facade.'}]
    saved = copy.deepcopy(bodies)
    state = physical_research_priority(story, {'research_priority': selected}, bodies, receipt)
    assert state['active_candidate_ids'] == selected['candidate_ids'] and bodies == saved
    assert len(order_research_candidates(bodies, state)) == len(bodies)


def test_shared_article_keeps_both_physical_lookups_and_changed_source_versions():
    article = {'article_id': 'publisher:1', 'source_sha256': 'a' * 64,
        'text_sha256': 'b' * 64, 'lookup_candidate_ids': ['osm:way:2']}
    merged = context.merge_acquired_text_versions([article,
        {**article, 'lookup_candidate_ids': ['osm:way:3']},
        {**article, 'source_sha256': 'c' * 64, 'text_sha256': 'd' * 64}])
    assert len(merged) == 2
    assert merged[0]['lookup_candidate_ids'] == ['osm:way:2', 'osm:way:3']
    assert merged[1]['source_sha256'] == 'c' * 64
    assert article['lookup_candidate_ids'] == ['osm:way:2']


@pytest.mark.asyncio
async def test_first_text_proceeds_while_other_dispatched_reader_finishes(monkeypatch):
    story, bodies, _ = inputs()
    both_started = asyncio.Event()
    release_slow = asyncio.Event()
    started, retained, observers = [], [], []

    async def acquire(service, story, candidates, request):
        cid = request['candidate_ids'][0]
        started.append(cid)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        if cid == 'osm:way:3':
            await release_slow.wait()
        return [{'article_id': cid, 'url': 'https://example.org/' + cid,
            'lookup_candidate_ids': [cid]}], {'status': 'completed'}

    def retain(service, story, sources, **kwargs):
        retained.extend(kwargs['acquired_text_articles'])

    monkeypatch.setattr(context, 'acquire_regional_text', acquire)
    from street_story import identity_discovery
    monkeypatch.setattr(identity_discovery, '_retain_article_discovery', retain)
    service = SimpleNamespace(providers=SimpleNamespace(research=SimpleNamespace(
        retain_search_observer=observers.append)))
    texts, lookup = await asyncio.wait_for(context.acquire_active_regional_text(
        service, story, bodies, ['osm:way:2', 'osm:way:3', 'osm:way:4']), 2)
    assert texts[0]['article_id'] == 'osm:way:2'
    assert lookup['pending_reader_count'] == 1 and len(observers) == 1
    assert started == ['osm:way:2', 'osm:way:3']  # Reserve HTTP was never sent.
    release_slow.set()
    await observers[0]
    assert {a['article_id'] for a in retained} == {'osm:way:2', 'osm:way:3'}


@pytest.mark.asyncio
async def test_uncertain_g_group_reaches_same_t_call_and_keeps_t_reduction_without_identity(tmp_path, monkeypatch):
    import json
    from street_story import identity_discovery
    from test_geometry_identity_plan import geometry_setup, geometry_decision, payload, Executor
    from test_architectural_text_identity import text_inputs
    service, story, active = geometry_setup(tmp_path)
    g = geometry_decision()
    g.update(decision='uncertain', next_action={'kind': 'address_text',
        'reason': 'Two physical facades need architectural comparison.',
        'target_candidate_ids': ['osm:way:2', 'osm:way:3']})
    initial = {**payload(g), 'research_priority': guidance(['osm:way:2', 'osm:way:3'])}
    _s, _c, answer, receipt = text_inputs(candidate_id='osm:way:2')
    answer.update(decision='uncertain', physical_link_evidence=[],
        research_priority=guidance(['osm:way:3']))
    calls, reads = [], []

    async def acquire(svc, snapshot, candidates, request):
        cid = request['candidate_ids'][0]
        reads.append(cid)
        return (receipt['articles'] if cid == 'osm:way:2' else []), {'status': 'completed'}

    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        if len(calls) == 1:
            return SimpleNamespace(text=json.dumps(initial))
        assert len(calls) == 2
        assert set(reads) == {'osm:way:2', 'osm:way:3'}
        assert receipt['articles'][0]['text'] in contents[-1]
        return SimpleNamespace(text=json.dumps(answer))

    monkeypatch.setattr(context, 'acquire_regional_text', acquire)
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = None
    history, _ = await identity_discovery.prepare_search_plan(service, story, '', active)
    priority = history['search_plan']['payload']['physical_research_priority']
    assert priority['active_candidate_ids'] == ['osm:way:3']
    assert 'osm:way:2' in priority['reserve_candidate_ids']
    assert len(calls) == 2 and not story.get('_identity_geometry_result')
    assert history['acquired_text_articles'][0]['text'] == receipt['articles'][0]['text']
