import base64
import hashlib
import json

import pytest
import test_live_editor as fixtures
from street_story.research_budget import ResearchTerminated, ensure_budget

URL = 'https://example.org/building'


def setup_scene(tmp_path, stale=None, support='spatially_supported'):
    svc, adapter, session, _ = fixtures.make_service(tmp_path)
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        fence = {'photo_sha256': row['photo_sha256'], 'generation': 0, 'control_revision': 0}
        plan_fence = dict(fence)
        if stale:
            plan_fence[stale] = {'photo_sha256': 'old', 'generation': 99, 'control_revision': 99}[stale]
        research = {'identity_generation': 0, 'visual_identity': {**fence, 'status': 'uncertain',
            'candidates': [{'candidate_id': 'osm:way:77', 'name': 'Observed building'}]},
            'identity_article_discovery': {**fence, 'sources': [{'url': URL, 'title': 'Building history'}],
                'search_plan': {**plan_fence, 'payload': {'source_map_receipt': {
                    'source_photo_sha256': row['photo_sha256'], 'map_image_sha256': 'a'*64, 'joint_image_input': True},
                    'spatial_hypotheses': [{
                    'candidate_id': 'osm:way:77', 'support_status': support,
                    'basis': ['Low street wing agrees with SOURCE and map'],
                    'counterevidence': ['No independent facade photograph'], 'assumptions': ['GPS uncertainty'],
                    'next_action': 'Ask which street faces the entrance'}]}}}}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
    body = b'<article>Institution founded in 1586. This building was constructed in 1900.</article>'
    svc.store.cache_put('public-article-acquisition-v1:' + hashlib.sha256(URL.encode()).hexdigest(),
        {'body': base64.b64encode(body).decode(), 'sha256': hashlib.sha256(body).hexdigest(),
         'mime': 'text/html', 'final_url': URL}, 86400)
    return svc, adapter, session


def test_probable_spatial_sources_are_useful_but_not_canonical(tmp_path):
    svc, adapter, session = setup_scene(tmp_path)
    context = adapter._compact_context(adapter._topic_state(session.resource_id))
    conditional = context['identity_research_context']
    assert conditional['hypotheses'][0]['support_status'] == 'spatially_supported'
    source = conditional['article_sources'][0]
    assert source['text'] == 'Institution founded in 1586. This building was constructed in 1900.'
    assert source['canonical_eligible'] is False and source['source_identity_verified'] is False
    assert conditional['canonical_eligible'] is False
    assert context['visual_identity']['status'] == 'uncertain'
    assert context['facts'] == []
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM fact_observations').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_versions').fetchone()[0] == 0
    initialized = adapter.initialize(resource_id=session.resource_id, actor=None, model='gemini-3.8-live')
    assert initialized['context']['identity_research_context'] == conditional
    assert 'never visual MATCH' in initialized['configuration']['system_instruction']


@pytest.mark.parametrize('stale', ['photo_sha256', 'generation', 'control_revision'])
def test_stale_spatial_plan_cannot_project_conditional_claims(tmp_path, stale):
    _, adapter, session = setup_scene(tmp_path, stale=stale)
    assert adapter._topic_state(session.resource_id)['identity_research_context'] is None


def test_plausible_does_not_become_spatially_supported(tmp_path):
    _, adapter, session = setup_scene(tmp_path, support='plausible')
    assert adapter._topic_state(session.resource_id)['identity_research_context']['hypotheses'][0]['support_status'] == 'plausible'


@pytest.mark.asyncio
async def test_city_reply_uses_plaintext_and_keeps_original_wave(tmp_path):
    svc, adapter, session, _ = fixtures.make_service(tmp_path)
    original = ensure_budget(svc, session.resource_id)
    original['work_units'] = {'planner_calls': ['existing-plan']}
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        research = json.loads(row['research_json'] or '{}')
        research.update(research_budget=original, automatic_research_outcome={'outcome': 'clarification_required'})
        db.execute('UPDATE stories SET latitude=NULL,longitude=NULL,research_json=? WHERE id=?',
            (json.dumps(research), session.resource_id))
    assert adapter._topic_state(session.resource_id)['identity_research_context']['clarification']['question']
    seen = []
    async def geocode(text):
        seen.append(('geocode', text))
        return {'lat': 55, 'lon': 21}
    async def resolve(story_id, transcript, *, owner_hint):
        seen.append(('resolve', owner_hint))
        return svc.story(story_id)
    svc._resolve_place_query = geocode
    svc.resolve_identity = resolve
    adapter.input(session, {'text': 'Это в Советске'})
    await adapter._resolve_place_state(session, 'Это в Советске')
    after = ensure_budget(svc, session.resource_id)
    assert after['identity_generation'] == 1
    assert {k: v for k, v in after.items() if k != 'identity_generation'} == {k: v for k, v in original.items() if k != 'identity_generation'}
    assert seen == [('geocode', 'Это в Советске'), ('resolve', 'Это в Советске')]
    with svc.store.connection() as db:
        research = json.loads(svc._story_row(db, session.resource_id)['research_json'])
        assert 'automatic_research_outcome' not in research
        assert research['identity_owner_hint']['independently_verified'] is False
        assert db.execute("SELECT text FROM live_messages WHERE role='user'").fetchone()[0] == 'Это в Советске'


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['search_exhausted', 'deadline_exceeded', 'resource_blocked'])
async def test_city_hint_does_not_clear_other_terminal_outcomes(tmp_path, outcome):
    svc, adapter, session, _ = fixtures.make_service(tmp_path)
    ensure_budget(svc, session.resource_id)
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        research = json.loads(row['research_json'])
        research['automatic_research_outcome'] = {'outcome': outcome}
        db.execute('UPDATE stories SET latitude=NULL,longitude=NULL,research_json=? WHERE id=?',
            (json.dumps(research), session.resource_id))
    async def forbidden(*args):
        pytest.fail('No geocode after terminal result')
    svc._resolve_place_query = forbidden
    with pytest.raises(ResearchTerminated):
        await adapter._resolve_place_state(session, 'Owner city')


def test_nearest_distance_without_model_assessment_is_not_probable_identity(tmp_path):
    svc, adapter, session = setup_scene(tmp_path)
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, session.resource_id)['research_json'])
        research['identity_article_discovery']['search_plan']['payload'].pop('spatial_hypotheses')
        research['visual_identity']['candidates'][0]['boundary_distance_m'] = 0.1
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
    assert adapter._topic_state(session.resource_id)['identity_research_context'] is None


def test_corrupt_acquired_page_is_not_reportable_conditional_evidence(tmp_path):
    svc, adapter, session = setup_scene(tmp_path)
    key = 'public-article-acquisition-v1:' + hashlib.sha256(URL.encode()).hexdigest()
    saved = svc.store.cache_get(key)
    saved['sha256'] = 'incorrect'
    svc.store.cache_put(key, saved, 86400)
    assert adapter._topic_state(session.resource_id)['identity_research_context']['article_sources'] == []


@pytest.mark.asyncio
async def test_expired_clarification_cannot_renew_deadline_or_clear_terminal(tmp_path):
    svc, adapter, session, _ = fixtures.make_service(tmp_path)
    budget = ensure_budget(svc, session.resource_id)
    budget['identity_deadline_at'] = svc.store.now() - 1
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        research = json.loads(row['research_json'])
        research.update(research_budget=budget, automatic_research_outcome={'outcome': 'clarification_required'})
        db.execute('UPDATE stories SET latitude=NULL,longitude=NULL,research_json=? WHERE id=?',
            (json.dumps(research), session.resource_id))
    with pytest.raises(ResearchTerminated):
        await adapter._resolve_place_state(session, 'Owner city')
    with svc.store.connection() as db:
        research = json.loads(svc._story_row(db, session.resource_id)['research_json'])
        assert research['research_budget'] == budget
        assert research['automatic_research_outcome']['outcome'] == 'clarification_required'


@pytest.mark.asyncio
async def test_conditional_source_does_not_authorize_canonical_research(tmp_path):
    from street_story.service import InvalidStateError
    svc, adapter, session = setup_scene(tmp_path)
    assert adapter._topic_state(session.resource_id)['identity_research_context']['article_sources']
    with pytest.raises(InvalidStateError) as rejected:
        await adapter._search_web(session, 'no-canonical-send', {'query': 'Building history'})
    assert rejected.value.code == 'identity_required'
    assert svc.providers.gemini.searches == []


@pytest.mark.parametrize('mime', ['text/plain', 'text/html'])
def test_source_scope_and_literal_text_remain_separate_from_photo_identity(tmp_path, mime):
    svc, adapter, session = setup_scene(tmp_path)
    raw = b'Archive proposes reconstruction in 2030, not completed work.'
    body = raw if mime == 'text/plain' else b'<article>' + raw + b'</article>'
    svc.store.cache_put('public-article-acquisition-v1:' + hashlib.sha256(URL.encode()).hexdigest(),
        {'body': base64.b64encode(body).decode(), 'sha256': hashlib.sha256(body).hexdigest(),
         'mime': mime, 'final_url': URL}, 86400)
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, session.resource_id)['research_json'])
        research['identity_article_discovery']['sources'][0].update(candidate_id='web:article',
            physical_subject_candidate_ids=['osm:way:other'])
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
    context = adapter._topic_state(session.resource_id)['identity_research_context']
    source = context['article_sources'][0]
    assert source['text'] == raw.decode()
    assert source['candidate_id'] == 'web:article'
    assert source['physical_subject_candidate_ids'] == ['osm:way:other']
    assert source['source_identity_verified'] is False
    assert context['hypotheses'][0]['candidate_id'] == 'osm:way:77'


def test_current_model_question_is_projected_without_replacing_owner_context(tmp_path):
    svc, adapter, session, _ = fixtures.make_service(tmp_path)
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        research = json.loads(row['research_json'] or '{}')
        research.update(transcript='На фасаде написано Гимназия', identity_clarification={
            'photo_sha256': row['photo_sha256'], 'generation': 0, 'control_revision': 0,
            'reason': 'geographic_context_missing', 'question': 'В каком городе эта гимназия?'})
        db.execute('UPDATE stories SET latitude=NULL,longitude=NULL,research_json=? WHERE id=?',
            (json.dumps(research), session.resource_id))
    context = adapter._topic_state(session.resource_id)['identity_research_context']
    assert context['clarification']['question'] == 'В каком городе эта гимназия?'
    assert context['owner_context']['text'] == 'На фасаде написано Гимназия'


@pytest.mark.parametrize('old_status', ['uncertain', 'match'])
@pytest.mark.parametrize('missing_location', [False, True])
def test_current_plan_cannot_borrow_old_visual_generation_candidates(tmp_path, old_status, missing_location):
    svc, adapter, session = setup_scene(tmp_path)
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        research = json.loads(row['research_json'])
        research['visual_identity'].update(generation=99, status=old_status)
        research['identity_clarification'] = {'photo_sha256': row['photo_sha256'],
            'generation': 0, 'control_revision': 0, 'reason': 'geographic_context_missing',
            'question': 'Какой город указан в вашем комментарии?'}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
        if missing_location:
            db.execute('UPDATE stories SET latitude=NULL,longitude=NULL WHERE id=?', (session.resource_id,))
    context = adapter._topic_state(session.resource_id)['identity_research_context']
    if missing_location:
        assert context['hypotheses'] == [] and context['article_sources'] == []
        assert context['clarification']['question'] == 'Какой город указан в вашем комментарии?'
    else:
        assert context is None


@pytest.mark.parametrize('answered', [False, True])
def test_new_current_model_question_is_not_hidden_by_retained_owner_hint(tmp_path, answered):
    svc, adapter, session, _ = fixtures.make_service(tmp_path)
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        research = json.loads(row['research_json'] or '{}')
        research.update(identity_generation=1,
            identity_owner_hint={'text': 'Город уже назван', 'independently_verified': False},
            identity_clarification={'photo_sha256': row['photo_sha256'], 'generation': 1,
                'control_revision': 0, 'reason': 'geographic_context_missing',
                'question': 'На какой улице находится этот дом?', 'answered': answered})
        db.execute('UPDATE stories SET latitude=NULL,longitude=NULL,research_json=? WHERE id=?',
            (json.dumps(research), session.resource_id))
    context = adapter._topic_state(session.resource_id)['identity_research_context']
    if answered:
        assert context['clarification'] is None
    else:
        assert context['clarification']['question'] == 'На какой улице находится этот дом?'


@pytest.mark.asyncio
async def test_city_reply_marks_only_existing_question_answered(tmp_path):
    svc, adapter, session, _ = fixtures.make_service(tmp_path)
    ensure_budget(svc, session.resource_id)
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        research = json.loads(row['research_json'])
        research.update(automatic_research_outcome={'outcome': 'clarification_required'},
            identity_clarification={'photo_sha256': row['photo_sha256'], 'generation': 0,
                'control_revision': 0, 'question': 'В каком городе этот дом?'})
        db.execute('UPDATE stories SET latitude=NULL,longitude=NULL,research_json=? WHERE id=?',
            (json.dumps(research), session.resource_id))
    async def geocode(text):
        return None
    async def resolve(story_id, transcript, *, owner_hint):
        return svc.story(story_id)
    svc._resolve_place_query, svc.resolve_identity = geocode, resolve
    adapter.input(session, {'text': 'Город из контекста'})
    await adapter._resolve_place_state(session, 'Город из контекста')
    with svc.store.connection() as db:
        research = json.loads(svc._story_row(db, session.resource_id)['research_json'])
        assert research['identity_clarification']['answered'] is True
    assert adapter._topic_state(session.resource_id)['identity_research_context']['clarification'] is None


@pytest.mark.parametrize('proof_change', ['text_fallback', 'wrong_photo', 'missing_map'])
def test_text_only_or_unbound_planner_cannot_claim_joint_visual_support(tmp_path, proof_change):
    svc, adapter, session = setup_scene(tmp_path)
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, session.resource_id)['research_json'])
        proof = research['identity_article_discovery']['search_plan']['payload']['source_map_receipt']
        if proof_change == 'text_fallback':
            proof['joint_image_input'] = False
        elif proof_change == 'wrong_photo':
            proof['source_photo_sha256'] = 'old-photo'
        else:
            proof.pop('map_image_sha256')
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), session.resource_id))
    context = adapter._topic_state(session.resource_id)['identity_research_context']
    assert context['joint_visual_input_verified'] is False
    assert context['hypotheses'][0]['joint_visual_input_verified'] is False
    # Preserve the actual semantic result without inventing a confidence downgrade.
    assert context['hypotheses'][0]['support_status'] == 'spatially_supported'
    assert 'text-only plan is a hypothesis' in context['instruction']


@pytest.mark.parametrize('stale', [None, 'generation', 'control_revision', 'photo_sha256'])
def test_live_geometry_acceptance_is_computed_before_compaction_with_current_fence(tmp_path, stale):
    from test_geometry_subject_articles import geometry_identity
    svc, adapter, session, _ = fixtures.make_service(tmp_path)
    with svc.store.tx() as db:
        row = svc._story_row(db, session.resource_id)
        identity = geometry_identity(photo=row['photo_sha256'], generation=0)
        research = {'visual_identity': identity, 'identity_generation': 0}
        if stale == 'generation':
            research['identity_generation'] = 1
        elif stale == 'control_revision':
            research['research_controls'] = {'identity': {'revision': 1}}
        elif stale == 'photo_sha256':
            db.execute('UPDATE stories SET photo_sha256=? WHERE id=?', ('b'*64, row['id']))
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), row['id']))
    context = adapter._compact_context(adapter._topic_state(session.resource_id))
    assert context['physical_identity_accepted'] is (stale is None)
    assert context['visual_identity']['visual_reference_verified'] is False
    assert 'geometry_proof' not in context['visual_identity']
    initialized = adapter.initialize(resource_id=session.resource_id, actor=None, model='gemini-3.8-live')
    assert initialized['configuration']['application_search_function'] == ('search_web' if stale is None else 'find_place_articles')
