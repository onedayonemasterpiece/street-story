from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from street_story.config import Settings
from street_story.mvp import MvpProductStreetStoryService
from street_story.product import ProductStreetStoryService
from street_story.service import InvalidStateError, ProviderBundle
from street_story.providers import PermanentProviderError, RetryableProviderError
from test_product_checkpoint import ProductFakeVibePublish


_photo_stream = io.BytesIO()
Image.new('RGB', (40, 30), 'blue').save(_photo_stream, format='JPEG')
PHOTO = _photo_stream.getvalue()
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
PROCESSED = b"mvp-processed"
PROCESSED_SHA = hashlib.sha256(PROCESSED).hexdigest()
PROMPT_SHA = '02a91163ac8982aec18ab77f8eda4a952d36dafdef157f61653a6757a0efd7b2'


class NoopOSM:
    async def lookup(self, lat, lon):
        return {"reverse": {}, "nearby": []}


class NoopWikipedia:
    async def nearby(self, lat, lon):
        return []


class NoopGemini:
    pass


class RecoverableVisualVibePublish:
    def __init__(self):
        self.tune_calls = 0
        self.status_calls = 0

    async def bootstrap(self):
        return {"routing_revision": 1, "destinations": [], "capabilities": []}

    async def ingress_asset(self, data: bytes, mime_type: str, request_key: str):
        return {"asset_id": "source-asset", "source_sha256": hashlib.sha256(data).hexdigest()}

    async def visual(self, payload: dict, request_key: str):
        assert payload["command"]["kind"] == "tune"
        assert len(payload["command"]["brief"]) <= 5000
        assert payload["command"]["formats"] == ["post_4_5"]
        assert payload["command"]["copy"] == {}
        assert "exact portrait 4:5" in payload["command"]["brief"]
        self.tune_calls += 1
        return {"operation_id": "visual-op", "state": "accepted", "visual_job_id": "job-1"}

    async def status(self, operation_id: str):
        assert operation_id == "visual-op"
        self.status_calls += 1
        if self.status_calls == 1:
            return {"receipts": [{"operation_id": operation_id, "state": "accepted"}]}
        return {
            "receipts": [
                {
                    "operation_id": operation_id,
                    "state": "verified",
                    "visual_job_id": "job-1",
                    "selected_asset_ref": "processed-asset",
                    "selected_sha256": PROCESSED_SHA,
                    "candidates": [{"id": "candidate-1", "asset_ref": "processed-asset", "sha256": PROCESSED_SHA,
                                    "width": 1080, "height": 1350}],
                }
            ]
        }

    async def read_asset(self, asset_id: str):
        assert asset_id == "processed-asset"
        return PROCESSED, "image/png"


def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        device_token="device",
        gemini_api_key="gemini",
        gemini_model="gemini-3.1-flash-lite",
        vibepublish_base_url="https://vibepublish.test",
        vibepublish_bearer_token="vp-token",
        osm_user_agent="Street Story MVP tests",
        worker_poll_seconds=0.01,
    )


def service(tmp_path: Path, vp=None) -> MvpProductStreetStoryService:
    return MvpProductStreetStoryService(
        settings(tmp_path),
        ProviderBundle(NoopOSM(), NoopWikipedia(), NoopGemini(), vp or RecoverableVisualVibePublish()),
    )


def create_story(svc: MvpProductStreetStoryService):
    return svc.create_story(
        key="create-mvp",
        client_story_id="story-mvp",
        photo_sha256=PHOTO_SHA,
        photo_mime_type="image/jpeg",
        photo_bytes=PHOTO,
        voice_protocol="voice-chunks-v2",
        lat=54.7104,
        lon=20.4522,
    )


def test_owner_prompt_is_exact_and_expands_russian_notes(tmp_path):
    prompt = Path(__file__).resolve().parents[1] / "prompts" / "street-story-image-v2.txt"
    data = prompt.read_bytes()
    assert len(data) == 3369
    assert hashlib.sha256(data).hexdigest() == '92496e7fd70419af40312865f486907fecea9ab84fdb35edc0fbef427faec424'

    svc = service(tmp_path)
    brief = svc._visual_brief(
        {"place_name": "Калининград"},
        {
            "user_voice_intent": "Здесь чувствуется ритм старого города",
            "selected_facts": [{"fact_id": "f1", "text": "Проверенный факт", "sources": []}],
        },
    )
    assert "{{CITY_NOTE_THEMES}}" not in brief
    assert "Проверенный факт" in brief
    assert "all visible handwritten city notes and annotations" in brief
    assert "without adding text" not in brief
    assert "safe area about 8%" in brief
    assert "Nothing important may touch or cross an edge" in brief
    assert "exact portrait 4:5" in brief
    assert "no white bars, blank padding or borders" in brief
    assert "Extend peripheral background" not in brief
    assert "Never outpaint" in brief
    assert len(brief) <= 5000


def test_visual_brief_bounds_dynamic_context_and_keeps_current_edit(tmp_path):
    svc = service(tmp_path)
    brief = svc._visual_brief(
        {"place_name": "Калининград"},
        {
            "visual_instruction": "Сделай акцент на фасаде и вечернем свете. " * 30,
            "selected_facts": [
                {
                    "fact_id": f"f{index}",
                    "text": "Проверенный исторический факт с источником.",
                    "sources": [],
                }
                for index in range(6)
            ],
            "user_voice_intent": "Длинное авторское наблюдение о месте. " * 100,
        },
    )

    assert len(brief) <= svc.VIBEPUBLISH_BRIEF_LIMIT
    assert "Текущая визуальная правка автора:" in brief
    assert "Калининград" in brief
    assert "{{CITY_NOTE_THEMES}}" not in brief


def test_visual_brief_keeps_every_selected_fact_and_its_qualifiers(tmp_path):
    svc = service(tmp_path)
    facts = [f"Факт {index}: историческое здание." for index in range(6)]
    facts.append("Закхаймские ворота — " + "важный памятник " * 16
                 + "истории и культуры федерального значения.")
    brief = svc._visual_brief({}, {
        "selected_facts": [{"fact_id": str(i), "text": text} for i, text in enumerate(facts)],
        "publication_concept": "Редакционный контекст. " * 100,
    })
    assert all(fact in brief for fact in facts)
    assert "каждый выбранный факт" in brief
    assert "Every selected fact must remain legible" in brief
    assert len(brief) <= svc.VIBEPUBLISH_BRIEF_LIMIT


def test_visual_brief_rejects_overflow_instead_of_silently_losing_facts(tmp_path):
    from street_story.service import InvalidStateError

    svc = service(tmp_path)
    with pytest.raises(InvalidStateError) as failure:
        svc._visual_brief({}, {
            "selected_facts": [{"fact_id": "long", "text": "Факт с уточнением. " * 400}],
        })
    assert failure.value.code == "visual_fact_annotations_too_long"


def test_visual_request_freezes_content_snapshot(tmp_path):
    svc = service(tmp_path)
    story = create_story(svc)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review' WHERE id=?", (story["id"],))
    result = svc.mutate_visual(story["id"], "visual-key", {"selected_fact_ids": []})
    assert result["visual"]["prompt_version"] == "street-story-image-v3"
    assert result["visual"]["prompt_sha256"] == PROMPT_SHA
    assert len(result["visual"]["content_revision"]) == 64
    with svc.store.connection() as db:
        row = db.execute("SELECT visual_context_json FROM stories WHERE id=?", (story["id"],)).fetchone()
    context = json.loads(row["visual_context_json"])
    assert context["source_photo_sha256"] == PHOTO_SHA
    assert context["ordered_voice_ids"] == []
    assert context["brief"].startswith("Use the uploaded street photo as the main visual reference")
    assert "exact portrait 4:5" in context["brief"]


def test_product_visual_brief_requests_portrait_without_losing_fact_context():
    fact = "Дом восстановили в конце 1950-х годов."
    brief = ProductStreetStoryService._visual_brief(
        {"place_name": "Житомирская, 22–24"},
        {"selected_facts": [{"text": fact}]},
    )
    assert fact in brief
    assert "exact portrait 4:5" in brief
    assert "no white bars, blank padding or borders" in brief
    assert "Do not crop the subject" in brief
    assert "Never outpaint" in brief and "correct perspective" in brief


class FrameBoundary(ProductFakeVibePublish):
    """Controlled receipt metadata, without an image provider or generated PNG."""
    def __init__(self, dimensions=None, *, verified=False, selected_asset='processed-asset'):
        super().__init__()
        self.dimensions = {'width': 1080, 'height': 1350} if dimensions is None else dimensions
        self.verified = verified
        self.selected_asset = selected_asset
        self.tune_calls = self.select_calls = 0
        self.on_status = None

    async def visual(self, payload, request_key):
        if payload['command']['kind'] == 'tune':
            self.tune_calls += 1
            return {'operation_id': 'frame-op', 'state': 'accepted', 'visual_job_id': 'frame-job'}
        assert payload['command']['kind'] == 'select'
        self.select_calls += 1
        self.verified = True
        return await self.status('frame-op')

    async def status(self, operation_id):
        if operation_id != 'frame-op':
            return await super().status(operation_id)
        if self.on_status:
            callback, self.on_status = self.on_status, None
            callback()
        candidate = {'id': 'candidate-1', 'asset_ref': 'processed-asset', 'sha256': PROCESSED_SHA,
                     'selection_token': 'private-token', **self.dimensions}
        receipt = {'operation_id': 'frame-op', 'state': 'verified' if self.verified else 'needs_selection',
                   'visual_job_id': 'frame-job', 'visual_revision': 1, 'candidates': [candidate]}
        if self.verified:
            receipt.update(selected_asset_ref=self.selected_asset, selected_sha256=PROCESSED_SHA)
        return receipt


def frame_job(svc):
    sid = create_story(svc)['id']
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review' WHERE id=?", (sid,))
    svc.mutate_visual(sid, 'frame-intent', {'selected_fact_ids': []})
    with svc.store.connection() as db:
        return sid, dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='visual'", (sid,)).fetchone())


def visual_context(svc, sid):
    with svc.store.connection() as db:
        return json.loads(svc._story_row(db, sid)['visual_context_json'])


@pytest.mark.asyncio
async def test_source_frame_request_preserves_layout_and_uses_existing_visual_worker_without_tune(tmp_path):
    class SourceFrameVibe(FrameBoundary):
        def __init__(self):
            super().__init__()
            self.frame_commands = []

        async def visual(self, payload, request_key):
            if payload['command']['kind'] == 'frame_source':
                self.frame_commands.append(payload['command'])
                return {'operation_id': 'frame-op', 'state': 'accepted', 'visual_job_id': 'frame-job'}
            return await super().visual(payload, request_key)

    vp = SourceFrameVibe()
    svc = service(tmp_path, vp)
    sid = create_story(svc)['id']
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review' WHERE id=?", (sid,))
    body = {'selected_fact_ids': [], 'preserve_source_frame': True,
        'visual_copy': {'title': 'Исходное фото'},
        'visual_layout': {'text_box': [.05, .02, .9, .14], 'title_size': 42, 'body_size': 28}}
    from street_story.photo_format import set_photo_format
    set_photo_format(svc, sid, 'choose-crop', {'mode': 'crop_4_5', 'expected_photo_sha256': PHOTO_SHA,
        'expected_identity_generation': svc.story(sid)['identity_generation']})
    svc.mutate_visual(sid, 'source-frame', body)
    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='visual'", (sid,)).fetchone())
    context = visual_context(svc, sid)
    assert context['source_photo_sha256'] == PHOTO_SHA
    assert context['source_frame']['copy'] == body['visual_copy']
    assert context['source_frame']['layout'] == body['visual_layout']
    await svc._run_visual(job)
    assert vp.tune_calls == 0 and vp.select_calls == 1 and len(vp.frame_commands) == 1
    assert vp.frame_commands[0] == {'kind': 'frame_source',
        'source': {'source': {'kind': 'asset', 'id': 'asset_source_1'}},
        'copy': body['visual_copy'], 'layout': body['visual_layout'], 'format': 'post_4_5'}
    assert visual_context(svc, sid)['source_frame'] == context['source_frame']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [None, 'running', 'unknown', 'other_candidate', 'changed_hash', 'stale_photo', 'unrejected'])
async def test_new_source_frame_requires_this_exact_closed_rejected_candidate(tmp_path, mutation):
    from street_story.service import ConflictError
    vp = FrameBoundary({'width': 1024, 'height': 1350})
    svc = service(tmp_path, vp)
    sid, job = frame_job(svc)
    await svc._run_visual(job)
    original = visual_context(svc, sid)
    status = await vp.status('frame-op')
    status['operation_complete'] = True
    if mutation in {'running', 'unknown'}:
        status['state'] = 'running' if mutation == 'running' else 'outcome_unknown'
        status['operation_complete'] = False
    elif mutation == 'other_candidate':
        status['candidates'][0]['id'] = 'different'
    elif mutation == 'changed_hash':
        status['candidates'][0]['sha256'] = 'f'*64
    elif mutation in {'stale_photo', 'unrejected'}:
        with svc.store.tx() as db:
            changed = json.loads(svc._story_row(db, sid)['visual_context_json'])
            if mutation == 'stale_photo':
                changed['output_frame_evidence']['source_photo_sha256'] = 'other-photo'
            else:
                changed['output_frame_evidence']['state'] = 'verified'
            db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (json.dumps(changed), sid))
    async def closed_status(operation):
        assert operation == 'frame-op'
        return status
    vp.status = closed_status
    body = {'selected_fact_ids': [], 'preserve_source_frame': True, 'visual_copy': {'title': 'Исходное фото'},
        'visual_layout': {'text_box': [.05, .02, .9, .14]}}
    if mutation:
        with pytest.raises(ConflictError):
            await svc.request_visual(sid, 'distinct-source-composition', body)
        assert visual_context(svc, sid)['content_revision'] == original['content_revision']
    else:
        await svc.request_visual(sid, 'distinct-source-composition', body)
        next_context = visual_context(svc, sid)
        assert next_context['source_frame']['copy'] == body['visual_copy']
        assert next_context['attempt_history'][-1]['output_frame_evidence'] == original['output_frame_evidence']
    assert vp.tune_calls == 1 and vp.select_calls == 0 and not vp.publish_effects


@pytest.mark.asyncio
@pytest.mark.parametrize('dimensions,reason', [
    ({'width': 1080, 'height': 810}, 'visual_format_mismatch'),
    ({'width': 1080}, 'visual_format_dimensions_missing'),
    ({'width': True, 'height': 1350}, 'visual_format_dimensions_missing'),
    ({'width': 0, 'height': 1350}, 'visual_format_dimensions_missing'),
])
async def test_new_frame_mismatch_retains_candidate_without_select_or_generation_retry(tmp_path, dimensions, reason):
    vp = FrameBoundary(dimensions)
    svc = service(tmp_path, vp)
    sid, job = frame_job(svc)
    await svc._run_visual(job)
    context = visual_context(svc, sid)
    proof = context['output_frame_evidence']
    assert proof['state'] == 'rejected' and proof['reason'] == reason
    assert proof['asset_ref'] == 'processed-asset' and proof['sha256'] == PROCESSED_SHA
    assert proof['content_revision'] == context['content_revision']
    assert 'private-token' not in json.dumps(proof)
    assert svc.story(sid)['state'] == 'needs_review'
    assert svc.story(sid)['error']['code'] == reason
    with pytest.raises(InvalidStateError, match='verified visual'):
        svc.mutate_publish(sid, 'blocked-post', {'destinations': ['love-kld-main'], 'delay_minutes': 60})
    await svc._run_visual(job)  # Same persisted operation can only be observed.
    assert vp.tune_calls == 1 and vp.select_calls == 0 and not vp.publish_effects


@pytest.mark.asyncio
@pytest.mark.parametrize('verified', [False, True])
async def test_new_frame_passes_selection_or_verified_recovery_and_publishes_bound_asset(tmp_path, verified):
    vp = FrameBoundary(verified=verified)
    svc = service(tmp_path, vp)
    sid, job = frame_job(svc)
    await svc._run_visual(job)
    assert svc.story(sid)['state'] == 'ready_to_publish'
    assert vp.tune_calls == 1 and vp.select_calls == int(not verified)
    svc.mutate_publish(sid, 'valid-post', {'destinations': ['love-kld-main'], 'delay_minutes': 60})
    with svc.store.connection() as db:
        publish_job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='publish'", (sid,)).fetchone())
    await svc._run_publish(publish_job)
    assert len(vp.publish_effects) == 1


@pytest.mark.asyncio
async def test_verified_recovery_rejects_dimensions_from_a_different_selected_asset(tmp_path):
    vp = FrameBoundary(verified=True, selected_asset='another-asset')
    svc = service(tmp_path, vp)
    sid, job = frame_job(svc)
    await svc._run_visual(job)
    assert svc.story(sid)['state'] == 'needs_review'
    assert visual_context(svc, sid)['output_frame_evidence']['reason'] == 'visual_format_asset_mismatch'
    assert vp.select_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['new_object', 'cancelled_job'])
async def test_stale_frame_cannot_select_or_promote_after_object_change_or_job_stop(tmp_path, change):
    vp = FrameBoundary()
    svc = service(tmp_path, vp)
    sid, job = frame_job(svc)
    def interrupt():
        with svc.store.tx() as db:
            if change == 'new_object':
                db.execute("UPDATE stories SET visual_context_json='{}',state='place_selected' WHERE id=?", (sid,))
            else:
                db.execute("UPDATE jobs SET state='cancelled',attempts=attempts+1 WHERE id=?", (job['id'],))
    vp.on_status = interrupt
    await svc._run_visual(job)
    assert 'output_frame_evidence' not in visual_context(svc, sid)
    assert svc.story(sid)['state'] != 'ready_to_publish'
    assert vp.select_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('attempts', [1, 2])
@pytest.mark.parametrize('change', ['missing_proof', 'asset_sha', 'source_photo', 'fact_revision', 'new_object'])
async def test_marked_pending_publication_requires_current_bound_frame_before_post(tmp_path, change, attempts):
    vp = FrameBoundary()
    svc = service(tmp_path, vp)
    sid, job = frame_job(svc)
    await svc._run_visual(job)
    svc.mutate_publish(sid, 'frozen-post', {'destinations': ['love-kld-main'], 'delay_minutes': 60})
    with svc.store.tx() as db:
        context = visual_context(svc, sid)
        if change == 'missing_proof':
            context.pop('output_frame_evidence')
        elif change == 'asset_sha':
            context['selected_sha256'] = '0' * 64
        elif change == 'source_photo':
            db.execute("UPDATE stories SET photo_sha256=? WHERE id=?", ('1' * 64, sid))
        elif change == 'fact_revision':
            context['fact_revision_bundle'] = {'changed-binding': '2' * 64}
        elif change == 'new_object':
            context = {}  # Pending intent retains its new-contract marker.
        db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (json.dumps(context), sid))
        publish_job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='publish'", (sid,)).fetchone())
        publish_job['attempts'] = attempts
    with pytest.raises(PermanentProviderError, match='visual_format_unverified'):
        await svc._run_publish(publish_job)
    assert not vp.publish_effects


@pytest.mark.asyncio
async def test_marked_existing_publication_remains_readable_after_frame_context_changes(tmp_path):
    vp = FrameBoundary()
    svc = service(tmp_path, vp)
    sid, job = frame_job(svc)
    await svc._run_visual(job)
    svc.mutate_publish(sid, 'existing-post', {'destinations': ['love-kld-main'], 'delay_minutes': 60})
    with svc.store.connection() as db:
        publish_job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='publish'", (sid,)).fetchone())
    await svc._run_publish(publish_job)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET visual_context_json='{}' WHERE id=?", (sid,))
    async def no_post(*args):
        pytest.fail('accepted original publication must only be observed')
    vp.publish = no_post
    await svc._run_publish(publish_job)
    assert len(vp.publish_effects) == 1


@pytest.mark.asyncio
async def test_format_change_during_publication_bootstrap_blocks_final_post(tmp_path):
    vp = FrameBoundary()
    svc = service(tmp_path, vp)
    sid, job = frame_job(svc)
    await svc._run_visual(job)
    svc.mutate_publish(sid, 'racing-post', {'destinations': ['love-kld-main'], 'delay_minutes': 60})
    original_bootstrap = vp.bootstrap
    async def invalidate():
        result = await original_bootstrap()
        context = visual_context(svc, sid)
        context.pop('output_frame_evidence')
        with svc.store.tx() as db:
            db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (json.dumps(context), sid))
        return result
    vp.bootstrap = invalidate
    with svc.store.connection() as db:
        publish_job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='publish'", (sid,)).fetchone())
    with pytest.raises(PermanentProviderError, match='visual_format_unverified'):
        await svc._run_publish(publish_job)
    assert not vp.publish_effects


@pytest.mark.asyncio
async def test_legacy_unknown_operation_is_observed_without_retrofitting_or_generation(tmp_path):
    vp = FrameBoundary()
    svc = service(tmp_path, vp)
    sid = create_story(svc)['id']
    before = previous_visual(svc, sid)
    before.update(source_asset_ref='legacy-source',
                  visual_command={'command': {'kind': 'tune', 'brief': 'Legacy frozen artwork'}})
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (json.dumps(before), sid))
    async def unknown(operation):
        assert operation == before['operation_id']
        return {'operation_id': operation, 'state': 'outcome_unknown', 'retry_safe': False}
    vp.status = unknown
    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='visual'", (sid,)).fetchone())
    await svc._run_visual(job)
    assert visual_context(svc, sid) == before
    assert 'output_frame_contract' not in visual_context(svc, sid)
    assert vp.tune_calls == vp.select_calls == 0


@pytest.mark.asyncio
async def test_missing_frame_proof_rolls_back_new_publication_intent_atomically(tmp_path):
    vp = FrameBoundary()
    svc = service(tmp_path, vp)
    sid, job = frame_job(svc)
    await svc._run_visual(job)
    context = visual_context(svc, sid)
    context.pop('output_frame_evidence')
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (json.dumps(context), sid))
    with pytest.raises(InvalidStateError) as failure:
        svc.mutate_publish(sid, 'unverified-post', {'destinations': ['love-kld-main'], 'delay_minutes': 60})
    assert failure.value.code == 'visual_format_unverified'
    with svc.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM publish_intents').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE kind='publish'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM idempotency WHERE key='unverified-post'").fetchone()[0] == 0
    assert not vp.publish_effects


@pytest.mark.asyncio
async def test_marked_uncertain_dispatch_keeps_original_payload_and_same_key_recovery(tmp_path):
    vp = FrameBoundary()
    svc = service(tmp_path, vp)
    sid, job = frame_job(svc)
    await svc._run_visual(job)
    svc.mutate_publish(sid, 'uncertain-post', {'destinations': ['love-kld-main'], 'delay_minutes': 60})
    with svc.store.connection() as db:
        publish_job = dict(db.execute("SELECT * FROM jobs WHERE story_id=? AND kind='publish'", (sid,)).fetchone())
    original_publish, calls = vp.publish, []
    async def lose(payload, key):
        calls.append((json.dumps(payload, sort_keys=True), key))
        await original_publish(payload, key)
        raise RetryableProviderError('lost response')
    vp.publish = lose
    with pytest.raises(RetryableProviderError, match='lost response'):
        await svc._run_publish(publish_job)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET visual_context_json='{}' WHERE id=?", (sid,))
    async def recover(payload, key):
        calls.append((json.dumps(payload, sort_keys=True), key))
        return await original_publish(payload, key)
    vp.publish = recover
    await svc._run_publish(publish_job)
    assert calls[0] == calls[1]  # Existing idempotent dispatch, not a new intent.
    assert len(vp.publish_effects) == 1


@pytest.mark.asyncio
async def test_visual_operation_is_persisted_and_reconciled_without_second_paid_submit(tmp_path):
    vp = RecoverableVisualVibePublish()
    svc = service(tmp_path, vp)
    story = create_story(svc)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review' WHERE id=?", (story["id"],))
    svc.mutate_visual(story["id"], "visual-reconcile", {"selected_fact_ids": []})

    assert await svc.run_once() is True
    after_first = svc.story(story["id"])
    assert after_first["visual"]["operation_id"] == "visual-op"
    assert vp.tune_calls == 1

    with svc.store.tx() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE kind='visual'")
    assert await svc.run_once() is True
    ready = svc.story(story["id"])
    assert ready["state"] == "ready_to_publish"
    assert ready["visual"]["selected_asset_ref"] == "processed-asset"
    assert ready["visual"]["selected_sha256"] == PROCESSED_SHA
    assert vp.tune_calls == 1
    assert (await svc.asset(story["id"]))[0] == PROCESSED


def previous_visual(svc, story_id):
    svc.mutate_visual(story_id, 'original-visual', {'selected_fact_ids': []})
    with svc.store.tx() as db:
        row = db.execute('SELECT visual_context_json FROM stories WHERE id=?', (story_id,)).fetchone()
        context = json.loads(row[0])
        # This fixture represents an operation frozen before the frame contract.
        context.pop('output_frame_contract', None)
        context.pop('identity_generation', None)
        context.update(operation_id='visual-op', visual_job_id='original-job', visual_request_key='original-key')
        db.execute("UPDATE stories SET visual_context_json=?,state='needs_review',draft_text='Original draft' WHERE id=?",
                   (json.dumps(context), story_id))
        db.execute("UPDATE jobs SET state='done' WHERE story_id=?", (story_id,))
    return context


def verified_visual(svc, story_id):
    before = previous_visual(svc, story_id)
    before.update(candidate_id='original-candidate', selected_sha256=PROCESSED_SHA,
                  selected_asset_ref='original-asset', source_asset_ref='source-asset')
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET visual_context_json=?,state='ready_to_publish' WHERE id=?",
                   (json.dumps(before), story_id))
    return before


@pytest.mark.asyncio
async def test_recompose_uses_verified_saved_art_without_new_generation_or_ingress(tmp_path):
    vp = RecoverableVisualVibePublish()
    svc = service(tmp_path, vp)
    sid = create_story(svc)['id']
    before = verified_visual(svc, sid)
    calls = []

    async def status(operation):
        return {'receipts': [{'operation_id': operation, 'state': 'verified',
            'visual_job_id': 'original-job' if operation == 'visual-op' else 'recomposed-job',
            'visual_revision': 4, 'selected_sha256': PROCESSED_SHA, 'selected_asset_ref': 'original-asset' if operation == 'visual-op' else 'processed-asset'}]}

    async def visual(command, key):
        calls.append(command)
        assert command['command'] == {'kind': 'recompose', 'job_id': 'original-job',
            'candidate_id': 'original-candidate', 'expected_visual_revision': 4,
            'expected_sha256': PROCESSED_SHA, 'format': 'post_4_5'}
        return {'operation_id': 'recomposed-op', 'visual_job_id': 'recomposed-job', 'state': 'verified'}

    async def no_ingress(*args):
        pytest.fail('verified art must not ingress another source image')

    vp.status, vp.visual, vp.ingress_asset = status, visual, no_ingress
    await svc.request_visual(sid, 'no-crop-recompose', {'selected_fact_ids': [], 'reuse_generated_art': True})
    assert await svc.run_once()
    result = svc.story(sid)
    assert result['state'] == 'ready_to_publish' and result['draft_text'] == 'Original draft'
    assert result['visual']['operation_id'] == 'recomposed-op'
    assert result['visual']['content_revision'] != before['content_revision']
    assert vp.tune_calls == 0 and len(calls) == 1
    assert svc._merge_visual_context(sid, before['content_revision'], {'operation_id': 'late-old'}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['photo', 'facts', 'concept', 'instruction', 'sha', 'job', 'revision', 'state'])
async def test_recompose_rejects_changed_content_or_unverified_art_without_context_reset(tmp_path, changed):
    from street_story.service import ConflictError
    vp = RecoverableVisualVibePublish()
    svc = service(tmp_path, vp)
    sid = create_story(svc)['id']
    before = verified_visual(svc, sid)
    fields = {'photo': 'source_photo_sha256', 'facts': 'fact_revision_bundle', 'concept': 'publication_concept'}
    if changed in fields:
        before[fields[changed]] = 'changed'
        with svc.store.tx() as db:
            db.execute('UPDATE stories SET visual_context_json=? WHERE id=?', (json.dumps(before), sid))
    receipt = {'operation_id': 'visual-op', 'state': 'verified', 'visual_job_id': 'original-job',
               'visual_revision': 4, 'selected_sha256': PROCESSED_SHA}
    if changed == 'sha':
        receipt['selected_sha256'] = 'a' * 64
    if changed == 'job':
        receipt['visual_job_id'] = 'another-job'
    if changed == 'revision':
        receipt.pop('visual_revision')
    if changed == 'state':
        receipt.update(state='failed', retry_safe=True)

    async def status(operation):
        return {'receipts': [receipt]}

    vp.status = status
    body = {'selected_fact_ids': [], 'reuse_generated_art': True}
    if changed == 'instruction':
        body['visual_instruction'] = 'Change the generated art'
    with pytest.raises(ConflictError, match='same verified art'):
        await svc.request_visual(sid, 'unsafe-recompose', body)
    with svc.store.connection() as db:
        after = json.loads(db.execute('SELECT visual_context_json FROM stories WHERE id=?', (sid,)).fetchone()[0])
        assert not db.execute("SELECT 1 FROM jobs WHERE semantic_key='visual:unsafe-recompose'").fetchone()
    assert before == after and vp.tune_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('state,retry_safe', [('outcome_unknown', False), ('running', False), ('failed', False)])
async def test_repeat_visual_keeps_original_context_when_outcome_unsafe(tmp_path, state, retry_safe):
    from street_story.service import ConflictError
    vp = RecoverableVisualVibePublish()
    svc = service(tmp_path, vp)
    story = create_story(svc)
    before = previous_visual(svc, story['id'])
    async def status(operation):
        return {'receipts': [{'operation_id': operation, 'state': state, 'retry_safe': retry_safe}]}
    vp.status = status
    with pytest.raises(ConflictError, match='original generation'):
        await svc.request_visual(story['id'], 'new-attempt', {'selected_fact_ids': []})
    with svc.store.connection() as db:
        after = json.loads(db.execute('SELECT visual_context_json FROM stories WHERE id=?', (story['id'],)).fetchone()[0])
        assert not db.execute("SELECT 1 FROM idempotency WHERE key='new-attempt'").fetchone()
    assert after == before and vp.tune_calls == 0


@pytest.mark.asyncio
async def test_explicit_safe_retry_same_story_preserves_history_and_fences_previous_worker(tmp_path):
    vp = RecoverableVisualVibePublish()
    svc = service(tmp_path, vp)
    story = create_story(svc)
    before = previous_visual(svc, story['id'])
    async def status(operation):
        return {'receipts': [{'operation_id': operation, 'state': 'failed', 'retry_safe': True,
                              'generation_dispatch': 'not_sent', 'revision': 2}]}
    vp.status = status
    result = await svc.request_visual(story['id'], 'new-attempt', {'selected_fact_ids': []})
    with svc.store.connection() as db:
        row = db.execute('SELECT * FROM stories WHERE id=?', (story['id'],)).fetchone()
        after = json.loads(row['visual_context_json'])
    assert after['content_revision'] != before['content_revision']
    assert after['brief'] == before['brief'] and after['source_photo_sha256'] == PHOTO_SHA
    assert after['attempt_history'][0]['operation_id'] == 'visual-op'
    assert after['attempt_history'][0]['outcome']['generation_dispatch'] == 'not_sent'
    assert not after.get('operation_id') and row['draft_text'] == 'Original draft'
    assert svc._merge_visual_context(story['id'], before['content_revision'], {'operation_id': 'late-old'}) is None
    assert (await svc.request_visual(story['id'], 'new-attempt', {'selected_fact_ids': []}))['revision'] == result['revision']
    assert vp.tune_calls == 0
    calls = []
    async def visual(payload, key):
        calls.append(key)
        return {'operation_id': 'retry-op', 'state': 'accepted', 'visual_job_id': 'retry-job'}
    async def retry_status(operation):
        assert operation == 'retry-op'
        return {'receipts': [{'operation_id': operation, 'state': 'verified', 'visual_job_id': 'retry-job',
                              'selected_asset_ref': 'processed-asset', 'selected_sha256': PROCESSED_SHA}]}
    vp.visual = visual
    vp.status = retry_status
    assert await svc.run_once()
    assert len(calls) == 1 and calls[0] != before['visual_request_key']
    assert svc.story(story['id'])['visual']['operation_id'] == 'retry-op'


@pytest.mark.asyncio
async def test_repeat_visual_rejects_story_changed_during_authoritative_read(tmp_path):
    from street_story.service import ConflictError
    vp = RecoverableVisualVibePublish()
    svc = service(tmp_path, vp)
    story = create_story(svc)
    before = previous_visual(svc, story['id'])
    async def status(operation):
        with svc.store.tx() as db:
            db.execute('UPDATE stories SET revision=revision+1 WHERE id=?', (story['id'],))
        return {'operation_id': operation, 'state': 'verified'}
    vp.status = status
    with pytest.raises(ConflictError, match='Story changed'):
        await svc.request_visual(story['id'], 'new-attempt', {'selected_fact_ids': []})
    assert svc.story(story['id'])['visual']['content_revision'] == before['content_revision']
