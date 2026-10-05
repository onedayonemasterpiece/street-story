from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from street_story.config import Settings
from street_story.mvp import MvpProductStreetStoryService
from street_story.service import ProviderBundle


PHOTO = b"mvp-photo"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
PROCESSED = b"mvp-processed"
PROCESSED_SHA = hashlib.sha256(PROCESSED).hexdigest()
PROMPT_SHA = "92496e7fd70419af40312865f486907fecea9ab84fdb35edc0fbef427faec424"


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
    assert hashlib.sha256(data).hexdigest() == PROMPT_SHA

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
    assert "Nothing important may touch, cross or be clipped" in brief
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
                    "text": "Проверенный исторический факт с источником. " * 20,
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


def test_visual_request_freezes_content_snapshot(tmp_path):
    svc = service(tmp_path)
    story = create_story(svc)
    with svc.store.tx() as db:
        db.execute("UPDATE stories SET state='review' WHERE id=?", (story["id"],))
    result = svc.mutate_visual(story["id"], "visual-key", {"selected_fact_ids": []})
    assert result["visual"]["prompt_version"] == "street-story-image-v2"
    assert result["visual"]["prompt_sha256"] == PROMPT_SHA
    assert len(result["visual"]["content_revision"]) == 64
    with svc.store.connection() as db:
        row = db.execute("SELECT visual_context_json FROM stories WHERE id=?", (story["id"],)).fetchone()
    context = json.loads(row["visual_context_json"])
    assert context["source_photo_sha256"] == PHOTO_SHA
    assert context["ordered_voice_ids"] == []
    assert context["brief"].startswith("Use the uploaded street photo as the main visual reference")


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
    assert svc.asset(story["id"])[0] == PROCESSED


def previous_visual(svc, story_id):
    svc.mutate_visual(story_id, 'original-visual', {'selected_fact_ids': []})
    with svc.store.tx() as db:
        row = db.execute('SELECT visual_context_json FROM stories WHERE id=?', (story_id,)).fetchone()
        context = json.loads(row[0])
        context.update(operation_id='visual-op', visual_job_id='original-job', visual_request_key='original-key')
        db.execute("UPDATE stories SET visual_context_json=?,state='needs_review',draft_text='Original draft' WHERE id=?",
                   (json.dumps(context), story_id))
        db.execute("UPDATE jobs SET state='done' WHERE story_id=?", (story_id,))
    return context


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
