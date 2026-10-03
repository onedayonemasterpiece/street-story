import json

from street_story.db import Store
from street_story.fact_ledger import (
    backfill_legacy_fact_ledger,
    candidate_assertion_id,
    eligible_selected_fact_ids,
    persist_fact_candidates,
    refresh_review_status,
    selected_eligibility_issues,
    set_owner_selection,
)


def create_story(store: Store, story_id: str = "story_ledger001") -> str:
    now = store.now()
    with store.tx() as db:
        db.execute(
            "INSERT INTO stories("
            "id,client_story_id,photo_sha256,photo_mime_type,photo_path,voice_protocol,state,"
            "research_json,visual_context_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,'{}','{}',?,?)",
            (
                story_id,
                "client-" + story_id,
                "a" * 64,
                "image/jpeg",
                "/tmp/no-photo.jpg",
                "voice-chunks-v2",
                "identity_ready",
                now,
                now,
            ),
        )
    return story_id


def source(url: str, *supports: str) -> dict:
    return {
        "type": "web",
        "title": url,
        "url": url,
        "supports": [
            {"kind": "page_excerpt", "source_url": url, "text": text}
            for text in supports
        ],
    }


def fact(key: str, text: str, url: str, *supports: str, existing_fact_id: str | None = None) -> dict:
    value = {
        "claim_key": key,
        "text": text,
        "confidence": .9,
        "selected": True,
        "sources": [source(url, *supports)],
    }
    if existing_fact_id:
        value["existing_fact_id"] = existing_fact_id
    return value


def persist(store: Store, story_id: str, items: list[dict], run: str, batch: str) -> list[str]:
    with store.tx() as db:
        return persist_fact_candidates(
            db,
            story_id=story_id,
            poi_key="wiki:403645",
            facts=items,
            run_id=run,
            batch_id=batch,
            model_name="test-model",
            prompt_version="test-v1",
            now=store.now(),
        )


def test_pass_two_does_not_remove_pass_one_observation_or_projection(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    persist(
        store,
        story_id,
        [fact("architect", "Архитектором ворот был Фридрих Штюлер.", "https://a.example", "Архитектор — Штюлер.")],
        "run-1",
        "batch-1",
    )
    persist(
        store,
        story_id,
        [fact("restoration", "Реставрация завершилась в 2005 году.", "https://b.example", "Реставрация завершена в 2005 году.")],
        "run-1",
        "batch-2",
    )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM facts WHERE story_id=?", (story_id,)).fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM fact_observations WHERE story_id=?", (story_id,)).fetchone()[0] == 2


def test_same_semantic_key_different_values_are_distinct_assertions(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    ids = persist(
        store,
        story_id,
        [
            fact("construction-year", "Строительство началось в 1843 году.", "https://a.example", "Начало строительства — 1843 год."),
            fact("construction-year", "Строительство завершилось в 1850 году.", "https://b.example", "Строительство завершено в 1850 году."),
        ],
        "run-1",
        "batch-1",
    )
    assert len(set(ids)) == 2
    assert candidate_assertion_id("construction-year", "Строительство началось в 1843 году.") != candidate_assertion_id(
        "construction-year", "Строительство завершилось в 1850 году."
    )
    with store.connection() as db:
        rows = list(db.execute(
            "SELECT assertion_id,semantic_key,display_text FROM fact_assertions WHERE story_id=? ORDER BY assertion_id",
            (story_id,),
        ))
        assert len(rows) == 2
        assert {row["semantic_key"] for row in rows} == {"construction-year"}


def test_same_url_multiple_passages_are_preserved(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    first_id = persist(
        store,
        story_id,
        [fact("facade-figures", "На фасаде находятся три фигуры.", "https://same.example/page", "На фасаде — три фигуры.")],
        "run-1",
        "batch-1",
    )[0]
    persist(
        store,
        story_id,
        [
            fact(
                "facade-figures",
                "На фасаде находятся три фигуры.",
                "https://same.example/page",
                "Слева направо названы Отакар II, Фридрих I и Альбрехт I.",
                existing_fact_id=first_id,
            )
        ],
        "run-1",
        "batch-2",
    )
    with store.connection() as db:
        row = db.execute(
            "SELECT sources_json FROM facts WHERE story_id=? AND fact_id=?",
            (story_id, first_id),
        ).fetchone()
        sources = json.loads(row["sources_json"])
        assert len(sources) == 1
        assert {support["text"] for support in sources[0]["supports"]} == {
            "На фасаде — три фигуры.",
            "Слева направо названы Отакар II, Фридрих I и Альбрехт I.",
        }
        assert db.execute(
            "SELECT COUNT(*) FROM fact_evidence_spans e JOIN fact_observations o ON o.observation_id=e.observation_id "
            "WHERE o.story_id=? AND o.assertion_id=?",
            (story_id, first_id),
        ).fetchone()[0] == 2


def test_more_than_eighty_existing_facts_survive_next_batch(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    initial = [
        fact(
            f"claim-{index}",
            f"Проверяемый факт номер {index}.",
            f"https://source{index}.example/page",
            f"Evidence for fact {index}.",
        )
        for index in range(85)
    ]
    persist(store, story_id, initial, "run-1", "batch-1")
    persist(
        store,
        story_id,
        [fact("claim-new", "Новый факт после большого inventory.", "https://new.example/page", "New evidence.")],
        "run-2",
        "batch-1",
    )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM facts WHERE story_id=?", (story_id,)).fetchone()[0] == 86
        assert db.execute("SELECT COUNT(*) FROM fact_assertions WHERE story_id=?", (story_id,)).fetchone()[0] == 86


def test_legacy_fact_backfill_is_unreviewed_and_preserves_owner_selection(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    with store.tx() as db:
        db.execute(
            "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                story_id,
                "legacy-1",
                "Старый факт.",
                .8,
                1,
                1,
                json.dumps([source("https://legacy.example", "Legacy evidence.")], ensure_ascii=False),
            ),
        )
        assert backfill_legacy_fact_ledger(db, store.now()) == 1
    with store.connection() as db:
        row = db.execute(
            "SELECT owner_selected,review_status,eligibility FROM fact_assertions "
            "WHERE story_id=? AND assertion_id='legacy-1'",
            (story_id,),
        ).fetchone()
        assert dict(row) == {
            "owner_selected": 1,
            "review_status": "unreviewed",
            "eligibility": "unreviewed",
        }


def test_selected_fact_is_fail_closed_until_successful_review_scan(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    fact_id = persist(
        store,
        story_id,
        [fact("architect", "Архитектором был Штюлер.", "https://a.example", "Архитектор — Штюлер.")],
        "run-1",
        "batch-1",
    )[0]

    with store.tx() as db:
        set_owner_selection(db, story_id, [fact_id], store.now())
        assert selected_eligibility_issues(db, story_id)[0]["reason"] == "fact_not_eligible"
        db.execute(
            "INSERT INTO fact_conflict_scans(story_id,poi_key,detector,status,pair_count,detected_count,coverage_complete,error_type,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (story_id, "wiki:403645", "test", "detector_unavailable", 0, 0, 0, "quota", store.now()),
        )
        refresh_review_status(db, story_id, store.now())
        assert selected_eligibility_issues(db, story_id)

        db.execute(
            "INSERT INTO fact_conflict_scans(story_id,poi_key,detector,status,pair_count,detected_count,coverage_complete,error_type,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (story_id, "wiki:403645", "test", "no_candidates", 0, 0, 1, None, store.now() + 1),
        )
        refresh_review_status(db, story_id, store.now() + 1)
        assert selected_eligibility_issues(db, story_id) == []
        assert eligible_selected_fact_ids(db, story_id) == [fact_id]


def test_new_evidence_reopens_previous_arbitration(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    left, right = persist(
        store,
        story_id,
        [
            fact("date", "Событие датируется 1843 годом.", "https://a.example", "1843."),
            fact("date", "Событие датируется 1845 годом.", "https://b.example", "1845."),
        ],
        "run-1",
        "batch-1",
    )
    now = store.now()
    with store.tx() as db:
        db.execute(
            """
            INSERT INTO fact_conflicts(
              story_id,conflict_id,poi_key,left_fact_id,right_fact_id,left_text,right_text,
              relation,detector_confidence,suggested_resolution,suggested_fact_id,
              detector_rationale,final_resolution,final_fact_id,arbitration_reason,
              arbitration_confidence,arbitrated_by,evidence_json,times_seen,first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
            """,
            (
                story_id,
                "conflict-test",
                "wiki:403645",
                left,
                right,
                "Событие датируется 1843 годом.",
                "Событие датируется 1845 годом.",
                "contradiction",
                .9,
                "prefer_left",
                left,
                "Different dates.",
                "prefer_left",
                left,
                "Source A preferred.",
                .8,
                "mira",
                "{}",
                now,
                now,
            ),
        )

    persist(
        store,
        story_id,
        [
            fact(
                "date",
                "Событие датируется 1843 годом.",
                "https://c.example",
                "Новый независимый фрагмент про 1843 год.",
                existing_fact_id=left,
            )
        ],
        "run-2",
        "batch-1",
    )
    with store.connection() as db:
        row = db.execute(
            "SELECT final_resolution,final_fact_id,arbitration_reason,arbitrated_by "
            "FROM fact_conflicts WHERE story_id=? AND conflict_id='conflict-test'",
            (story_id,),
        ).fetchone()
        assert dict(row) == {
            "final_resolution": None,
            "final_fact_id": None,
            "arbitration_reason": None,
            "arbitrated_by": None,
        }


def test_successful_bounded_review_never_marks_over_eighty_inventory_fully_eligible(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    initial = [
        fact(
            f"claim-{index}",
            f"Проверяемый факт номер {index}.",
            f"https://review{index}.example/page",
            f"Evidence {index}.",
        )
        for index in range(81)
    ]
    ids = persist(store, story_id, initial, "run-review", "batch-1")
    with store.tx() as db:
        set_owner_selection(db, story_id, [ids[-1]], store.now())
        db.execute(
            "INSERT INTO fact_conflict_scans(story_id,poi_key,detector,status,pair_count,detected_count,coverage_complete,error_type,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (story_id, "wiki:403645", "test", "ok", 3160, 0, 0, None, store.now()),
        )
        refresh_review_status(db, story_id, store.now())
        issue = selected_eligibility_issues(db, story_id)
        assert issue and issue[0]["fact_id"] == ids[-1]
        assert issue[0]["eligibility"] == "unreviewed"


def test_complete_review_can_mark_over_eighty_inventory_eligible(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    initial = [
        fact(
            f"claim-complete-{index}",
            f"Проверяемый полный факт номер {index}.",
            f"https://complete{index}.example/page",
            f"Evidence complete {index}.",
        )
        for index in range(81)
    ]
    ids = persist(store, story_id, initial, "run-complete-review", "batch-1")
    with store.tx() as db:
        set_owner_selection(db, story_id, [ids[-1]], store.now())
        db.execute(
            "INSERT INTO fact_conflict_scans("
            "story_id,poi_key,detector,status,pair_count,detected_count,"
            "coverage_complete,error_type,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                story_id,
                "wiki:403645",
                "test",
                "ok",
                3240,
                0,
                1,
                None,
                store.now(),
            ),
        )
        refresh_review_status(db, story_id, store.now())
        assert selected_eligibility_issues(db, story_id) == []
        assert eligible_selected_fact_ids(db, story_id) == [ids[-1]]
