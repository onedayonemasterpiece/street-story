from street_story.db import Store
from street_story.research_runs import (
    begin_research_run,
    manifest_complete,
    mark_chunk,
    persist_source_version,
    record_chunk_batch,
    plan_text_chunks,
    register_discovered_source,
    run_manifest,
    set_run_state,
)


def create_story(store: Store, story_id: str = "story_run001") -> str:
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


def test_chunk_manifest_covers_every_core_character_without_gap_or_overlap():
    text = (
        ("Вводный абзац. " * 150)
        + "\n\n"
        + ("Очень длинный абзац без удобной границы " * 500)
        + "\n\n"
        + ("Хвост статьи с обязательным фактом: Отакар II. " * 120)
    )
    chunks = plan_text_chunks(text, target_chars=2400, overlap_chars=180)
    assert len(chunks) > 6
    assert chunks[0]["core_start"] == 0
    assert chunks[-1]["core_end"] == len(text)
    for left, right in zip(chunks, chunks[1:]):
        assert left["core_end"] == right["core_start"]
        assert left["context_end"] >= left["core_end"]
        assert right["context_start"] <= right["core_start"]
    rebuilt = "".join(text[item["core_start"]:item["core_end"]] for item in chunks)
    assert rebuilt == text
    assert "Отакар II" in text[chunks[-1]["core_start"]:chunks[-1]["core_end"]]


def test_manifest_records_all_chunks_and_terminal_states(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    now = store.now()
    with store.tx() as db:
        run_id = begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:403645",
            goal="Проверить весь документ",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-manifest",
            now=now,
        )
        set_run_state(db, run_id, "fetching", now=now)
        doc = persist_source_version(
            db,
            run_id=run_id,
            requested_url="https://example.org/a",
            final_url="https://example.org/a",
            title="A",
            content_type="text/html",
            http_status=200,
            redirect_chain=[],
            normalized_text=("абзац " * 3000),
            read_status="complete",
            now=now,
            target_chars=2500,
            overlap_chars=200,
        )
        assert len(doc["chunks"]) > 6
        for item in doc["chunks"]:
            record_chunk_batch(db, run_id=run_id, chunk_id=item["chunk_id"], batch_index=0,
                               status="completed", raw_fact_count=0, accepted_fact_count=0,
                               continuation_needed=False, continuation_reason="", model_name="test-model",
                               prompt_version="chunk-v1", now=now, payload={"facts": [], "no_claims": True})
            mark_chunk(
                db,
                run_id=run_id,
                chunk_id=item["chunk_id"],
                status="no_claims",
                observation_count=0,
                model_name="test-model",
                prompt_version="chunk-v1",
                now=now,
            )
        set_run_state(db, run_id, "completed", now=now, completed=True)

    with store.connection() as db:
        manifest = run_manifest(db, "run-manifest")
    assert manifest["counts"]["chunks_planned"] == len(doc["chunks"])
    assert manifest["counts"]["chunks_completed"] == len(doc["chunks"])
    assert manifest["counts"]["sources_fetched"] == 1
    assert manifest_complete(manifest) is True


def test_partial_source_and_failed_chunk_never_report_complete(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    now = store.now()
    with store.tx() as db:
        run_id = begin_research_run(
            db,
            story_id=story_id,
            poi_key=None,
            goal="Long page",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-partial",
            now=now,
        )
        doc = persist_source_version(
            db,
            run_id=run_id,
            requested_url="https://example.org/partial",
            final_url="https://example.org/partial",
            title="Partial",
            content_type="text/html",
            http_status=200,
            redirect_chain=[],
            normalized_text="text " * 1200,
            read_status="partial_text_limit",
            now=now,
            target_chars=2000,
            overlap_chars=100,
        )
        mark_chunk(
            db,
            run_id=run_id,
            chunk_id=doc["chunks"][0]["chunk_id"],
            status="failed",
            observation_count=0,
            model_name="test-model",
            prompt_version="chunk-v1",
            error_code="model_timeout",
            now=now,
        )
        set_run_state(db, run_id, "partial", detail="incomplete", now=now, completed=True)

    with store.connection() as db:
        manifest = run_manifest(db, "run-partial")
    assert manifest["counts"]["sources_partial"] == 1
    assert manifest["counts"]["chunks_failed"] == 1
    assert manifest_complete(manifest) is False


def test_discovered_source_can_exist_without_being_fetched(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    now = store.now()
    with store.tx() as db:
        run_id = begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:403645",
            goal="Discovery",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-discovery",
            now=now,
        )
        register_discovered_source(
            db,
            run_id=run_id,
            url="https://example.org/snippet",
            title="Snippet",
            status="snippet_only",
            now=now,
        )
    with store.connection() as db:
        manifest = run_manifest(db, "run-discovery")
    assert manifest["sources"][0]["status"] == "snippet_only"
    assert manifest["counts"]["sources_fetched"] == 0
    assert manifest["counts"]["sources_snippet_only"] == 1
    # The source is explicitly snippet-only; this does not pretend the page was
    # fetched, but it need not block a run whose semantic goal is already met.
    assert manifest_complete(manifest) is True


def test_deferred_source_keeps_manifest_partial(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    now = store.now()
    with store.tx() as db:
        run_id = begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:403645",
            goal="bounded batch",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-deferred-source",
            now=now,
        )
        register_discovered_source(
            db,
            run_id=run_id,
            url="https://example.org/deferred",
            title="Deferred",
            status="deferred",
            error_code="source_batch_limit",
            now=now,
        )
    with store.connection() as db:
        manifest = run_manifest(db, "run-deferred-source")
    assert manifest["counts"]["sources_pending"] == 1
    assert manifest["sources"][0]["error_code"] == "source_batch_limit"
    assert manifest_complete(manifest) is False


def test_chunk_batch_manifest_tracks_continuation_and_deferred_terminal_state(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    story_id = create_story(store)
    now = store.now()
    with store.tx() as db:
        run_id = begin_research_run(
            db,
            story_id=story_id,
            poi_key="wiki:403645",
            goal="dense chunk",
            expected_story_revision=0,
            identity_generation=0,
            run_id="run-chunk-continuation",
            now=now,
        )
        doc = persist_source_version(
            db,
            run_id=run_id,
            requested_url="https://example.org/dense",
            final_url="https://example.org/dense",
            title="Dense",
            content_type="text/html",
            http_status=200,
            redirect_chain=[],
            normalized_text="Dense facts. " * 500,
            read_status="complete",
            now=now,
            target_chars=10000,
            overlap_chars=100,
        )
        chunk_id = doc["chunks"][0]["chunk_id"]
        record_chunk_batch(
            db,
            run_id=run_id,
            chunk_id=chunk_id,
            batch_index=0,
            status="continuation",
            raw_fact_count=32,
            accepted_fact_count=32,
            continuation_needed=True,
            continuation_reason="More atomic facts remain in the same core span.",
            model_name="test-model",
            prompt_version="chunk-cont-v1",
            now=now,
        )
        record_chunk_batch(
            db,
            run_id=run_id,
            chunk_id=chunk_id,
            batch_index=1,
            status="deferred",
            raw_fact_count=32,
            accepted_fact_count=30,
            continuation_needed=True,
            continuation_reason="Continuation budget exhausted.",
            error_code="continuation_limit",
            model_name="test-model",
            prompt_version="chunk-cont-v1",
            now=now,
        )
        mark_chunk(
            db,
            run_id=run_id,
            chunk_id=chunk_id,
            status="deferred",
            observation_count=62,
            model_name="test-model",
            prompt_version="chunk-cont-v1",
            error_code="continuation_limit",
            now=now,
        )

    with store.connection() as db:
        manifest = run_manifest(db, "run-chunk-continuation")
    assert manifest["counts"]["chunk_batches_total"] == 2
    assert manifest["counts"]["chunk_batches_continuation"] == 1
    assert manifest["counts"]["chunk_batches_deferred"] == 1
    assert manifest["chunk_batches"][0]["batch_index"] == 0
    assert manifest["chunk_batches"][1]["error_code"] == "continuation_limit"
    assert manifest_complete(manifest) is False


def test_chunk_renewal_preserves_live_fence_and_cannot_revive_expired_or_displaced_owner(tmp_path):
    from street_story.research_runs import acquire_chunk_lease, renew_chunk_lease
    store = Store(tmp_path / 'lease.sqlite3')
    story = create_story(store)
    with store.tx() as db:
        run = begin_research_run(db, story_id=story, poi_key='poi', goal='History',
            expected_story_revision=0, identity_generation=0, run_id='lease-run', now=100)
        doc = persist_source_version(db, run_id=run, requested_url='https://example.org/lease',
            final_url='https://example.org/lease', title='Lease article', content_type='text/html',
            http_status=200, redirect_chain=[], normalized_text='An intact frozen passage.', read_status='complete', now=100)
        chunk = doc['chunks'][0]['chunk_id']
        fence = acquire_chunk_lease(db, run_id=run, chunk_id=chunk, owner='alive', now=100, ttl=180)
        assert renew_chunk_lease(db, run_id=run, chunk_id=chunk, owner='alive', fence=fence, now=200)
        row = db.execute('SELECT lease_fence,lease_until FROM research_chunk_runs WHERE run_id=? AND chunk_id=?', (run, chunk)).fetchone()
        assert row['lease_fence'] == fence and row['lease_until'] == 380
        assert not renew_chunk_lease(db, run_id=run, chunk_id=chunk, owner='alive', fence=fence, now=381)
        replacement = acquire_chunk_lease(db, run_id=run, chunk_id=chunk, owner='replacement', now=381)
        assert replacement > fence
        assert not renew_chunk_lease(db, run_id=run, chunk_id=chunk, owner='alive', fence=fence, now=382)
