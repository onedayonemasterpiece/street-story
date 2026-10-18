"""Selected groups constrain automatic proof, queue recovery and target changes."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from street_story.identity_proof import accepted_identity
from street_story.place_selection import confirm_place
from street_story.research_control import stop_research
from street_story.service import ConflictError
from test_owner_map_selection import command, create, make_service, map_for


def group_command(story, snapshot, key='group'):
    body = command(story, snapshot, '', key)
    body.pop('candidate_id')
    return {**body, 'mode': 'rough', 'candidate_ids': ['osm:way:1', 'osm:way:2']}


async def group_fixture(tmp_path):
    service, gemini = make_service(tmp_path)
    story = create(service)
    snapshot = await map_for(service, story)
    body = group_command(story, snapshot)
    return service, gemini, confirm_place(service, story['id'], 'group', body), body


def proof(cid):
    return {'status': 'match', 'candidate_id': cid, 'confidence': .98,
        'observations': ['The same distinctive arches and facade proportions are visible.'],
        'alternative_candidate_ids': [], '_references_sent': [cid]}


@pytest.mark.asyncio
async def test_rough_is_persisted_group_not_accepted_identity_or_poi_and_enqueues_once(tmp_path):
    service, gemini, story, body = await group_fixture(tmp_path)
    selected = story['place_selection']
    assert selected['mode'] == 'rough' and selected['status'] == 'identifying'
    assert selected['candidate_id'] is None and selected['narrowed_candidate_id'] is None
    assert len(selected['features']['features']) == 2
    assert all(c['map_geometry']['rings'][0]['closed'] for c in selected['candidates'])
    assert not accepted_identity(story['visual_identity'], story['photo_sha256'], 1)
    assert service.story(story['id'])['place_selection'] == selected
    assert confirm_place(service, story['id'], 'group', body)['revision'] == story['revision']
    with service.store.connection() as db:
        jobs = list(db.execute('SELECT kind FROM jobs'))
        assert [j['kind'] for j in jobs] == ['identity']
        assert db.execute('SELECT count(*) FROM pois').fetchone()[0] == 0
    assert not gemini.identity_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [[], ['osm:way:1'], ['osm:way:1', 'osm:way:1'], ['osm:way:1', 'osm:way:999']])
async def test_group_rejects_invalid_or_unshown_members_atomically(tmp_path, ids):
    service, _ = make_service(tmp_path)
    story = create(service)
    snapshot = await map_for(service, story)
    body = {**group_command(story, snapshot), 'candidate_ids': ids}
    with pytest.raises(ConflictError):
        confirm_place(service, story['id'], 'group', body)
    assert service.story(story['id'])['identity_generation'] == 0
    with service.store.connection() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_exact_multiselect_does_not_manufacture_an_owner_identity(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service)
    snapshot = await map_for(service, story)
    with pytest.raises(ConflictError, match='один дом'):
        confirm_place(service, story['id'], 'group', {**group_command(story, snapshot), 'mode': 'exact'})


@pytest.mark.asyncio
@pytest.mark.parametrize('cid', ['osm:way:2', 'osm:way:999'])
async def test_automatic_narrowing_accepts_only_proved_member_and_keeps_group(tmp_path, monkeypatch, cid):
    service, _, story, _ = await group_fixture(tmp_path)
    calls = []
    async def identify(current, transcript, candidates):
        calls.append([c['candidate_id'] for c in candidates])
        assert current['_owner_selected_group']['candidate_ids'] == ['osm:way:1', 'osm:way:2']
        return proof(cid)
    monkeypatch.setattr(service, '_identify_photo', identify)
    async def forbidden(*args, **kwargs):
        pytest.fail('Group narrowing must not run a nearby/outside lookup')
    monkeypatch.setattr(service.providers.wikipedia, 'nearby', forbidden)
    with service.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',attempts=1 WHERE kind='identity'")
        job = dict(db.execute("SELECT * FROM jobs WHERE kind='identity'").fetchone())
    await service._run_identity(job)
    result = service.story(story['id'])
    assert calls == [['osm:way:1', 'osm:way:2']]
    assert result['place_selection']['candidate_ids'] == story['place_selection']['candidate_ids']
    assert result['place_selection']['features'] == story['place_selection']['features']
    if cid == 'osm:way:2':
        assert result['visual_identity']['status'] == 'match'
        assert result['visual_identity']['selection_basis'] == 'automatic_within_owner_group'
        assert result['place_selection']['status'] == 'resolved'
        assert result['place_selection']['narrowed_candidate_id'] == cid
        assert accepted_identity(result['visual_identity'], result['photo_sha256'], 1)
        service.providers.research = SimpleNamespace(facts_available=True)
        service._schedule_confirmed_facts()
        service._schedule_confirmed_facts()
        with service.store.connection() as db:
            assert db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0] == 1
    else:
        assert result['visual_identity']['candidate_id'] is None
        assert result['place_selection']['status'] == 'needs_review'
        assert result['identity_clarification']['reason'] == 'selected_group_ambiguous'
        with service.store.connection() as db:
            assert db.execute('SELECT count(*) FROM pois').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_deferred_group_uses_existing_visual_queue_without_widening_or_repeat(tmp_path):
    service, _, story, _ = await group_fixture(tmp_path)
    service.providers.research = SimpleNamespace(vision_available=True)
    with service.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',attempts=1 WHERE kind='identity'")
        job = dict(db.execute("SELECT * FROM jobs WHERE kind='identity'").fetchone())
    await service._run_identity(job)
    result = service.story(story['id'])
    assert result['place_selection']['status'] == 'identifying'
    assert result['visual_identity']['status'] == 'uncertain'
    assert {c['candidate_id'] for c in result['visual_identity']['candidates']} == {'osm:way:1', 'osm:way:2'}
    await service._run_identity(job)
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='identity_visual'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='research'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_resolved_group_research_freezes_proved_member_without_lookup_or_reidentify(tmp_path, monkeypatch):
    from street_story.headless_facts import HeadlessFacts
    service, _, story, _ = await group_fixture(tmp_path)
    async def identify(*args):
        return proof('osm:way:2')
    monkeypatch.setattr(service, '_identify_photo', identify)
    with service.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',attempts=1 WHERE kind='identity'")
        identity_job = dict(db.execute("SELECT * FROM jobs WHERE kind='identity'").fetchone())
    await service._run_identity(identity_job)
    async def forbidden(*args, **kwargs):
        pytest.fail('A resolved group physical POI must never be rediscovered or replaced')
    service.providers.research = SimpleNamespace(facts_available=True, extract_fact_page=forbidden)
    monkeypatch.setattr(service, '_identify_photo', forbidden)
    monkeypatch.setattr(service.providers.wikipedia, 'nearby', forbidden)
    service.providers.osm.lookup = forbidden
    calls = []
    async def facts(harness, job, run_id, goal, scope):
        snapshot = harness._snapshot(job, run_id)
        assert snapshot[0]['_identity_generation'] == 1
        assert snapshot[1]['visual_identity']['status'] == 'match'
        assert snapshot[1]['visual_identity']['candidate_id'] == 'osm:way:2'
        assert snapshot[2]['poi_key'] == 'osm:way:2'
        calls.append(run_id)
    monkeypatch.setattr(HeadlessFacts, 'run', facts)
    service._schedule_confirmed_facts()
    with service.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',attempts=1 WHERE kind='research'")
        job = dict(db.execute("SELECT * FROM jobs WHERE kind='research'").fetchone())
    await service._run_research(job)
    assert len(calls) == 1
    assert service.story(story['id'])['place_selection']['narrowed_candidate_id'] == 'osm:way:2'


@pytest.mark.asyncio
async def test_clarification_retains_group_constraint_in_new_generation(tmp_path, monkeypatch):
    service, _, story, _ = await group_fixture(tmp_path)
    async def unknown(*args):
        return {'status': 'uncertain', 'candidate_id': '', 'observations': ['Ambiguous group.']}
    monkeypatch.setattr(service, '_identify_photo', unknown)
    await service.resolve_identity(story['id'])
    answer = service.accept_identity_hint(story['id'], 'group-hint', 'The building has two arches.')
    assert answer['identity_generation'] == 2
    assert answer['place_selection']['generation'] == 2
    assert answer['place_selection']['candidate_ids'] == ['osm:way:1', 'osm:way:2']
    calls = []
    async def observe(current, transcript, candidates):
        calls.append((transcript, [c['candidate_id'] for c in candidates]))
        return proof('osm:way:999')
    monkeypatch.setattr(service, '_identify_photo', observe)
    with service.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',attempts=1 WHERE kind='identity' AND json_extract(payload_json,'$.identity_generation')=2")
        job = dict(db.execute("SELECT * FROM jobs WHERE kind='identity' AND json_extract(payload_json,'$.identity_generation')=2").fetchone())
    await service._run_identity(job)
    assert calls == [('The building has two arches.', ['osm:way:1', 'osm:way:2'])]
    assert service.story(story['id'])['visual_identity']['candidate_id'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('next_mode', ['exact', 'rough'])
async def test_stop_group_a_then_b_fences_late_narrowing(tmp_path, monkeypatch, next_mode):
    service, _, story, body = await group_fixture(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    async def identify(*args):
        started.set()
        await release.wait()
        return proof('osm:way:1')
    monkeypatch.setattr(service, '_identify_photo', identify)
    with service.store.tx() as db:
        db.execute("UPDATE jobs SET state='running',attempts=1 WHERE kind='identity'")
        job = dict(db.execute("SELECT * FROM jobs WHERE kind='identity'").fetchone())
    pending = asyncio.create_task(service._run_identity(job))
    await started.wait()
    stopped = stop_research(service, story['id'], purpose='identity')['story']
    snapshot = await map_for(service, stopped)
    b_body = command(stopped, snapshot, 'osm:way:2', 'b') if next_mode == 'exact' else group_command(stopped, snapshot, 'b')
    b = confirm_place(service, story['id'], 'b', b_body)
    release.set()
    await pending
    assert service.story(story['id'])['visual_identity']['candidate_id'] == ('osm:way:2' if next_mode == 'exact' else None)
    assert service.story(story['id'])['place_selection'] == b['place_selection']
    assert service.story(story['id'])['identity_generation'] == b['identity_generation'] == 2
    assert not b['research_controls']['facts']['stopped']
    assert confirm_place(service, story['id'], 'group', body)['identity_generation'] == 2
    with service.store.connection() as db:
        assert db.execute("SELECT state FROM jobs WHERE id=?", (job['id'],)).fetchone()[0] == 'cancelled'
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='research' AND state='ready'").fetchone()[0] == (1 if next_mode == 'exact' else 0)
        assert db.execute("SELECT count(*) FROM jobs WHERE kind='identity' AND state='ready'").fetchone()[0] == (1 if next_mode == 'rough' else 0)


@pytest.mark.asyncio
async def test_enrichment_cannot_replace_selected_physical_snapshot(tmp_path):
    from street_story.owner_group_identity import physical_members
    service, _, story, _ = await group_fixture(tmp_path)
    _, research = service._identity_snapshot(story['id'])
    original = story['place_selection']['candidates'][0]
    forged = {**original, 'map_geometry': {'rings': []},
        'map_coordinates': {'latitude': 0, 'longitude': 0},
        'map_object': {'provenance': 'untrusted'}, 'search_anchor': {'city': 'Outside'},
        'name': 'Outside', 'reference_image_urls': ['https://example.com/member.jpg']}
    enriched = physical_members(research, [forged])[0]
    for key in ('map_geometry', 'map_coordinates', 'map_object', 'search_anchor', 'name'):
        assert enriched.get(key) == original.get(key)
    assert enriched['reference_image_urls'] == forged['reference_image_urls']
    enriched['map_geometry']['rings'].clear()
    assert research['place_selection']['candidates'][0]['map_geometry'] == original['map_geometry']


@pytest.mark.asyncio
async def test_actual_visual_queue_filters_outside_references_and_resolves_member_after_reopen(tmp_path):
    from test_visual_search_continuation import prepared
    from visual_queue_fixture import reference_receipt
    service, adapter, initial, session = prepared(tmp_path)
    initial = service.story(initial['id'])
    snapshot = await map_for(service, initial)
    story = confirm_place(service, initial['id'], 'group', group_command(initial, snapshot))
    members = story['place_selection']['candidates']
    member = {**members[1], 'reference_image_urls': [
        'https://example.com/member-first.jpg', 'https://example.com/member-second.jpg']}
    outside = {'candidate_id': 'wiki:999', 'name': 'Outside', 'url': 'https://example.com/outside',
        'reference_image_urls': ['https://example.com/outside.jpg']}
    with service.store.tx() as db:
        research = json.loads(service._story_row(db, story['id'])['research_json'])
        research['visual_identity']['candidates'] = [members[0], member, outside]
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    loaded = []
    async def images(candidates, limit, *, story_id, evidence):
        candidate = candidates[0]
        loaded.append(candidate['candidate_id'])
        evidence.append(reference_receipt(candidate))
        return [(candidate['candidate_id'], 'image/jpeg', candidate['reference_image_urls'][0])]
    service._candidate_reference_images = images
    first_session = session()
    first = await adapter._compare_place_images(first_session, {}, page_budget=0, search_budget=0)
    assert first['owner_selected_group']['candidate_ids'] == ['osm:way:1', 'osm:way:2']
    adapter._record_place_comparison(first_session, 'negative', {
        'comparison_id': first['comparison_id'], 'status': 'mismatch', 'candidate_id': '',
        'confidence': 1, 'observations': ['Different facade'], 'alternative_candidate_ids': []})
    reopened = session()
    second = await adapter._compare_place_images(reopened, {}, page_budget=0, search_budget=0)
    assert second['comparison_id'] != first['comparison_id']
    result = adapter._record_place_comparison(reopened, 'positive', {
        **proof('osm:way:2'), 'comparison_id': second['comparison_id']})
    assert result['matched']
    saved = service.story(story['id'])
    assert saved['place_selection']['status'] == 'resolved'
    assert saved['place_selection']['narrowed_candidate_id'] == 'osm:way:2'
    assert saved['place_selection']['features'] == story['place_selection']['features']
    assert saved['visual_identity']['status'] == 'match'
    assert saved['visual_identity']['selection_basis'] == 'automatic_within_owner_group'
    assert accepted_identity(saved['visual_identity'], saved['photo_sha256'], 1)
    assert loaded == ['osm:way:2', 'osm:way:2']


@pytest.mark.asyncio
async def test_original_outside_comparison_receipt_cannot_rebind_group(tmp_path):
    from test_visual_search_continuation import prepared
    from visual_queue_fixture import reference_receipt
    service, adapter, initial, session = prepared(tmp_path)
    initial = service.story(initial['id'])
    snapshot = await map_for(service, initial)
    story = confirm_place(service, initial['id'], 'group', group_command(initial, snapshot))
    member = {**story['place_selection']['candidates'][1],
        'reference_image_urls': ['https://example.com/member.jpg']}
    outside = {'candidate_id': 'wiki:999', 'name': 'Outside', 'url': 'https://example.com/outside',
        'reference_image_urls': ['https://example.com/outside.jpg']}
    with service.store.tx() as db:
        research = json.loads(service._story_row(db, story['id'])['research_json'])
        research['visual_identity']['candidates'] = [member, outside]
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (json.dumps(research), story['id']))
    async def images(candidates, limit, *, story_id, evidence):
        evidence.append(reference_receipt(candidates[0]))
        return [(candidates[0]['candidate_id'], 'image/jpeg', candidates[0]['reference_image_urls'][0])]
    service._candidate_reference_images = images
    current = session()
    reply = await adapter._compare_place_images(current, {}, page_budget=0, search_budget=0)
    # Recovery must retain an already-addressed receipt even if its original
    # subject lies outside the group; the final canonical admission must reject it.
    pending = current.state['visual_comparison']['pending']
    outside['reference_id'] = pending['candidates'][0]['reference_id']
    pending['candidates'] = [outside]
    pending['evidence'] = [{**pending['evidence'][0], 'candidate_id': outside['candidate_id']}]
    result = adapter._record_place_comparison(current, 'outside-verdict', {
        **proof(outside['candidate_id']), 'comparison_id': reply['comparison_id']})
    assert not result['matched']
    saved = service.story(story['id'])
    assert saved['visual_identity']['candidate_id'] is None
    assert saved['place_selection']['narrowed_candidate_id'] is None
    verdict = current.state['visual_comparison']['verdict_history'][-1]
    assert verdict['model_status'] == 'match' and verdict['status'] == 'uncertain'
    assert verdict['uncertain_reason'] == 'outside_owner_selected_group'
    with service.store.connection() as db:
        assert db.execute('SELECT count(*) FROM pois').fetchone()[0] == 0
