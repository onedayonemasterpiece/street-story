from __future__ import annotations
import json
import time
from types import SimpleNamespace
import httpx
import pytest

from street_story.live_author_intent import (
    begin_turn, has_place_consent, suspected_noise_transcript, suspected_noise_turn,
)
from street_story.identity_progress import advance
from street_story.identity_references import reference_images, canonical_reference
from street_story.service import ConflictError
from test_live_editor import make_service
from test_identity_recovery_policy import candidate, match
from street_story.identity_visual import identify_nearest


@pytest.mark.asyncio
@pytest.mark.parametrize('utterance', ['¿Qué tal?', '¿Qué?', 'Да', 'Это не Бранденбургские ворота', 'Это Бранденбургские ворота?'])
async def test_false_or_ambiguous_speech_never_confirms_model_candidate(tmp_path, utterance):
    svc, adapter, session, _ = make_service(tmp_path)
    await adapter.execute_tool(session, {'id':'resolve-1','name':'resolve_place','args':{}})
    adapter.on_event(session, {'type':'input_transcript','text':utterance})
    with pytest.raises(ConflictError) as error:
        await adapter.execute_tool(session, {'id':'bad-confirm','name':'confirm_place', 'args':{'candidate_id':'wiki:77','candidate_name':'Бранденбургские ворота'}})
    assert error.value.code == 'live_place_author_consent_required'
    assert svc.story(session.resource_id)['visual_identity']['status'] != 'owner_confirmed'
    with svc.store.connection() as db:
        assert not db.execute("SELECT 1 FROM live_commands WHERE command_id='bad-confirm'").fetchone()


@pytest.mark.asyncio
async def test_named_fresh_consent_persists_evidence_and_cannot_authorize_another_command(tmp_path):
    svc, adapter, session, _ = make_service(tmp_path)
    await adapter.execute_tool(session, {'id':'r','name':'resolve_place','args':{}})
    adapter.input(session, {'text':'Да, подтверждаю: это Бранденбургские ворота.'})
    args = {'candidate_id':'wiki:77'}
    await adapter.execute_tool(session, {'id':'c','name':'confirm_place','args':args})
    identity = svc.story(session.resource_id)['visual_identity']
    assert identity['approval_evidence']['kind'] == 'fresh_explicit_named_author_turn'
    assert identity['approval_evidence']['origin'] == 'text'
    # Same command is idempotent; a new command needs new owner evidence.
    await adapter.execute_tool(session, {'id':'c','name':'confirm_place','args':args})
    with pytest.raises(ConflictError):
        await adapter.execute_tool(session, {'id':'c2','name':'confirm_place','args':args})


def test_stale_user_turn_or_new_audio_turn_cannot_confirm():
    session = SimpleNamespace(state={})
    begin_turn(session, 'Да, это Королевские ворота', origin='text')
    assert has_place_consent(session, 'Королевские ворота')
    session.state['author_turn']['at'] = time.monotonic() - 31
    assert not has_place_consent(session, 'Королевские ворота')
    begin_turn(session)
    assert not has_place_consent(session, 'Королевские ворота')


@pytest.mark.asyncio
async def test_wikimedia_429_is_visible_and_does_not_repeat_for_targeted_or_other_images(tmp_path):
    service, _, session, _ = make_service(tmp_path)
    requests = []
    async def handler(request):
        requests.append(str(request.url))
        return httpx.Response(429, headers={'Retry-After':'90'})
    candidates = [{'candidate_id':'c1','reference_image_urls':['https://upload.wikimedia.org/one.jpg?utm_source=wiki']},
                  {'candidate_id':'c2','reference_image_urls':['https://upload.wikimedia.org/two.jpg']}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await reference_images(service, candidates, story_id=session.resource_id, http=client) == []
        assert await reference_images(service, candidates, story_id=session.resource_id, http=client) == []
    assert requests == ['https://upload.wikimedia.org/one.jpg']
    with service.store.connection() as db:
        payload = json.loads(db.execute("SELECT payload_json FROM live_diagnostics WHERE event_type='identity_reference_unavailable' ORDER BY id LIMIT 1").fetchone()[0])
    assert payload['http_status'] == 429 and payload['reason'] == 'rate_limited'


@pytest.mark.asyncio
async def test_failed_first_reference_tries_second_bounded_url_and_refuses_foreign_redirect(tmp_path):
    service, _, _, _ = make_service(tmp_path)
    from test_reference_image_codec import jpeg
    from street_story.reference_image_codec import MAX_DOWNLOAD_BYTES, normalize_reference
    fixture = jpeg()
    async def handler(request):
        if request.url.path == '/original.jpg':
            return httpx.Response(200, headers={'content-type':'image/jpeg', 'content-length':str(MAX_DOWNLOAD_BYTES + 1)})
        if request.url.path == '/redirect.jpg':
            return httpx.Response(302, headers={'Location':'https://another.invalid/image.jpg'})
        return httpx.Response(200, headers={'content-type':'image/jpeg'}, content=fixture)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        images = await reference_images(service, [{'candidate_id':'c','reference_image_urls':[
            'https://upload.wikimedia.org/original.jpg', 'https://upload.wikimedia.org/thumbnail.jpg']}], http=client)
        assert images == [('c', *normalize_reference(fixture))]
        assert await reference_images(service,[{'candidate_id':'r','reference_image_urls':['https://upload.wikimedia.org/redirect.jpg']}], http=client) == []
    assert canonical_reference('https://user:pass@upload.wikimedia.org/a.jpg') is None


@pytest.mark.asyncio
async def test_unavailable_reference_host_stops_redundant_far_comparisons_without_false_match(tmp_path):
    service, _, session, _ = make_service(tmp_path)
    calls = []
    async def compare(story, transcript, batch, reference_limit):
        calls.append([x['candidate_id'] for x in batch])
        return {**match('c1', refs=False), '_references_rate_limited': True}
    service._identify_photo_batch = compare
    result = await identify_nearest(service, {'id':session.resource_id}, '', [candidate(i) for i in range(1,17)])
    assert result['status'] == 'uncertain'
    assert len(calls) == 1


def test_progress_preserves_stages_and_total_latency_across_retries():
    p = advance({}, 'identity_requested', {'generation':0}, 10)
    p = advance(p, 'identity_started', {'generation':0}, 11)
    p = advance(p, 'identity_location', {'coordinates_usable': True}, 11)
    p = advance(p, 'identity_failed', {}, 25)
    p = advance(p, 'identity_started', {}, 27)
    p = advance(p, 'identity_osm', {'retained_count':57,'candidate_pool_counts':{'landmark':48,'nearby':50}}, 30)
    p = advance(p, 'identity_wikipedia', {'count':5}, 32)
    p = advance(p, 'identity_shortlist', {'candidate_count':16}, 32)
    p = advance(p, 'identity_batch_finished', {'batch_candidate_count':4}, 38)
    p = advance(p, 'identity_finished', {'status':'uncertain','reference_verified':False}, 39)
    assert p['elapsed_ms'] == 29000 and p['attempt'] == 2
    assert p['reviewed_count'] == 4 and p['map_count'] == 57 and p['wiki_count'] == 5
    assert p['steps'][-1]['status'] == 'warning'

def test_short_foreign_noise_shape_is_narrow_and_russian_preferred():
    assert suspected_noise_transcript("¿Qué tal?", audio_chunks=84)
    assert suspected_noise_transcript("¿Qué?", audio_chunks=20)
    assert not suspected_noise_transcript("Что?", audio_chunks=84)
    assert not suspected_noise_transcript(
        "Please switch language to English now", audio_chunks=84
    )
    assert not suspected_noise_transcript(
        "This is a deliberate coherent sentence in English", audio_chunks=84
    )
    assert not suspected_noise_transcript("¿Qué tal?", audio_chunks=84, origin="text")


@pytest.mark.asyncio
async def test_suspected_noise_turn_cannot_execute_product_mutation(tmp_path):
    svc, adapter, session, _ = make_service(tmp_path)
    await adapter.execute_tool(session, {"id": "r", "name": "resolve_place", "args": {}})
    adapter.input(session, {"activity_start": True})
    adapter.on_event(session, {"type": "input_timing", "audio_chunks": 84})
    adapter.on_event(session, {"type": "input_transcript", "text": "¿Qué tal?"})
    assert suspected_noise_turn(session)
    result = await adapter.execute_tool(
        session,
        {
            "id": "noise-confirm",
            "name": "confirm_place",
            "args": {
                "candidate_id": "wiki:77",
                "candidate_name": "Бранденбургские ворота",
            },
        },
    )
    assert result["ignored"] is True
    assert svc.story(session.resource_id)["visual_identity"]["status"] != "owner_confirmed"
    with svc.store.connection() as db:
        blocked = db.execute(
            "SELECT payload_json FROM live_diagnostics "
            "WHERE story_id=? AND event_type='suspected_noise_tool_blocked'",
            (session.resource_id,),
        ).fetchone()
    assert blocked is not None


@pytest.mark.asyncio
async def test_publication_concept_is_durable_and_separate_from_draft(tmp_path):
    svc, adapter, session, _ = make_service(tmp_path)
    before = svc.story(session.resource_id).get("draft_text")
    result = await adapter.execute_tool(
        session,
        {
            "id": "concept-1",
            "name": "set_concept",
            "args": {"concept": "Современная жизнь ворот и их роль в городе сегодня"},
        },
    )
    assert result["publication_concept"].startswith("Современная жизнь")
    story = svc.story(session.resource_id)
    assert story["publication_concept"] == result["publication_concept"]
    assert story.get("draft_text") == before


def test_story_projection_uses_latest_actual_publish_intent_destinations(tmp_path):
    svc, _, session, _ = make_service(tmp_path)
    now = svc.store.now()
    with svc.store.tx() as db:
        db.execute(
            "INSERT INTO publish_intents("
            "id,story_id,request_key,vibepublish_request_key,request_json,state,"
            "scheduled_for,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "pub-test",
                session.resource_id,
                "req-test",
                "vp-test",
                json.dumps({
                    "destinations": ["street_story_e2e_20260928_tg"],
                    "scheduled_for": "2026-10-02T12:00:00+02:00",
                }),
                "scheduled",
                "2026-10-02T12:00:00+02:00",
                now,
                now,
            ),
        )
    publication = svc.story(session.resource_id)["publication"]
    assert publication["state"] == "scheduled"
    assert publication["destinations"] == ["street_story_e2e_20260928_tg"]
