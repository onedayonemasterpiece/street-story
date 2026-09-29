from datetime import datetime, timedelta, timezone

import pytest

from street_story.service import ConflictError, InvalidStateError
from test_backend import create, seed_ready_visual, service


def test_publish_rejects_native_schedule_with_insufficient_lead(tmp_path):
    svc, _, _ = service(tmp_path)
    story = create(svc)
    seed_ready_visual(svc, story["id"])
    too_soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    with pytest.raises(ConflictError) as exc:
        svc.mutate_publish(
            story["id"],
            "publish-too-soon",
            {"destinations": ["polyubit-kaliningrad-telegram"], "scheduled_for": too_soon},
        )
    assert exc.value.code == "publish_time_too_soon"
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM publish_intents").fetchone()[0] == 0


def test_definite_pre_dispatch_422_can_be_reconfirmed_with_new_intent(tmp_path):
    svc, _, _ = service(tmp_path)
    story = create(svc)
    seed_ready_visual(svc, story["id"])
    svc.mutate_publish(
        story["id"],
        "publish-first",
        {"destinations": ["polyubit-kaliningrad-telegram"], "delay_minutes": 5},
    )
    with svc.store.tx() as db:
        intent = db.execute(
            "SELECT id FROM publish_intents WHERE story_id=? ORDER BY created_at DESC LIMIT 1",
            (story["id"],),
        ).fetchone()
        db.execute(
            "UPDATE jobs SET state='failed',last_error='VibePublish request failed: HTTP 422' "
            "WHERE story_id=? AND kind='publish'",
            (story["id"],),
        )
        db.execute(
            "UPDATE stories SET state='needs_review',error_code='provider_permanent_error',"
            "error_message='review' WHERE id=?",
            (story["id"],),
        )
        assert intent is not None

    retried = svc.mutate_publish(
        story["id"],
        "publish-second",
        {"destinations": ["polyubit-kaliningrad-telegram"], "delay_minutes": 5},
    )
    assert retried["state"] == "scheduling"
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM publish_intents WHERE story_id=?", (story["id"],)).fetchone()[0] == 2


def test_reconfirm_is_forbidden_when_provider_operation_identity_exists(tmp_path):
    svc, _, _ = service(tmp_path)
    story = create(svc)
    seed_ready_visual(svc, story["id"])
    svc.mutate_publish(
        story["id"],
        "publish-first",
        {"destinations": ["polyubit-kaliningrad-telegram"], "delay_minutes": 5},
    )
    with svc.store.tx() as db:
        db.execute(
            "UPDATE jobs SET state='failed',last_error='VibePublish request failed: HTTP 422' "
            "WHERE story_id=? AND kind='publish'",
            (story["id"],),
        )
        db.execute(
            "UPDATE publish_intents SET vibepublish_operation_id='op_uncertain' WHERE story_id=?",
            (story["id"],),
        )
        db.execute(
            "UPDATE stories SET state='needs_review',error_code='provider_permanent_error',"
            "error_message='review' WHERE id=?",
            (story["id"],),
        )

    with pytest.raises(InvalidStateError):
        svc.mutate_publish(
            story["id"],
            "publish-second",
            {"destinations": ["polyubit-kaliningrad-telegram"], "delay_minutes": 5},
        )
