from street_story.db import Store
from street_story.poi_memory import (
    backfill_poi_assertion_review_state,
    persist_research_memory,
    poi_key,
    prior_facts,
    processed_sources,
    sync_poi_review_from_story,
)
import json


class FakeDB:
    def execute(self, _query, args):
        if (
            "FROM poi_aliases" in _query
            or "FROM poi_research_facts" in _query
            or "FROM poi_research_assertions" in _query
        ):
            return []
        assert args[0] == "current"
        assert args[1] == "wiki:1"
        return [
            {
                "fact_id": "architect", "text": "Архитектор — автор проекта.",
                "confidence": .9, "evidence_supported": 1, "selected": 0,
                "sources_json": '[{"type":"official","url":"https://official.example/history"}]',
            },
            {
                "fact_id": "built", "text": "Построен в начале XX века.",
                "confidence": .8, "evidence_supported": 1, "selected": 1,
                "sources_json": '[{"type":"web","url":"https://example.com/history"}]',
            },
            {
                "fact_id": "built", "text": "Дубликат.",
                "confidence": .8, "evidence_supported": 1, "selected": 1,
                "sources_json": "[]",
            },
        ]


def test_poi_key_uses_stable_candidate_id():
    assert poi_key({"candidate_id": "wiki:1", "candidate_name": "Объект"}) == "wiki:1"
    assert poi_key({"candidate_name": "Объект"}) is None


def test_prior_facts_are_unique():
    facts = prior_facts(FakeDB(), {"candidate_id": "wiki:1"}, "current")
    assert [item["fact_id"] for item in facts] == ["architect", "built"]
    assert facts[0]["sources"][0]["type"] == "official"


def _bind_memory_aliases(db):
    db.execute("INSERT INTO pois(id,status,canonical_name,created_at,updated_at) VALUES('gate','verified','Gate',1,1)")
    for key in ('wiki:memory', 'osm:way:memory'):
        db.execute("INSERT INTO poi_aliases(poi_id,namespace,value,normalized_value,created_at) VALUES('gate','street_story_candidate',?,?,1)", (key, key))


def _memory_story(db, story_id, key, updated_at):
    db.execute(
        "INSERT INTO stories(id,client_story_id,photo_sha256,photo_mime_type,photo_path,voice_protocol,state,research_json,created_at,updated_at) VALUES(?,?,?,'image/jpeg','fixture','voice-chunks-v2','identity_ready',?,1,?)",
        (story_id, story_id, 'a' * 64, json.dumps({'visual_identity': {'candidate_id': key, 'status': 'match'}}), updated_at),
    )


def _memory_assertion(db, key, fact_id, status='eligible', eligibility='eligible', reviewed_at=10, updated_at=10):
    db.execute(
        "INSERT INTO poi_research_assertions(poi_key,assertion_id,semantic_key,text,confidence,sources_json,created_at,updated_at,review_status,eligibility,reviewed_at) VALUES(?,?,?,'Факт.',.9,'[{\"url\":\"https://source.example/fact\"}]',1,?,?,?,?)",
        (key, fact_id, fact_id, updated_at, status, eligibility, reviewed_at),
    )


def _memory_review_fact(db, story_id, fact_id):
    db.execute("INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES(?,?,'Факт.',.9,1,0,'[{\"url\":\"https://source.example/fact\"}]')", (story_id, fact_id))


def test_alias_review_blocks_older_story_resurrection_and_preserves_newer_decisions(tmp_path):
    store = Store(tmp_path / 'memory.sqlite3')
    with store.tx() as db:
        _bind_memory_aliases(db)
        _memory_story(db, 'accepted-old', 'wiki:memory', 1)
        _memory_story(db, 'review-new', 'osm:way:memory', 30)
        _memory_assertion(db, 'wiki:memory', 'blocked')
        _memory_assertion(db, 'osm:way:memory', 'blocked', updated_at=40)
        _memory_review_fact(db, 'review-new', 'blocked')
        db.execute("INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES('accepted-old','blocked','Факт.',.9,1,0,'[]')")
        db.execute("INSERT INTO fact_assertions(story_id,assertion_id,semantic_key,display_text,owner_selected,review_status,eligibility,created_at,updated_at) VALUES('review-new','blocked','blocked','Факт.',0,'disputed','withheld',1,30)")
        assert sync_poi_review_from_story(db, 'review-new', 30) == 2
        assert prior_facts(db, {'candidate_id': 'wiki:memory'}, 'next') == []
        db.execute("UPDATE fact_assertions SET review_status='eligible',eligibility='eligible',updated_at=20 WHERE story_id='review-new'")
        assert sync_poi_review_from_story(db, 'review-new', 40) == 0
        assert prior_facts(db, {'candidate_id': 'osm:way:memory'}, 'next') == []


def test_projection_refresh_uses_model_admission_time_and_reuse_cannot_resurrect_withheld_claim(tmp_path):
    store = Store(tmp_path / 'memory.sqlite3')
    with store.tx() as db:
        _bind_memory_aliases(db)
        _memory_story(db, 'model-review', 'osm:way:memory', 1)
        _memory_assertion(db, 'osm:way:memory', 'claim')
        _memory_review_fact(db, 'model-review', 'claim')
        db.execute("INSERT INTO fact_assertions(story_id,assertion_id,display_text,revision_digest,review_status,eligibility,created_at,updated_at) VALUES('model-review','claim','Факт.','reviewed-revision','eligible','eligible',1,100)")
        db.execute("INSERT INTO fact_conflict_scans(story_id,detector,status,pair_count,detected_count,coverage_complete,revision_bundle_json,conflict_ids_json,missing_aspects_json,created_at) VALUES('model-review','mira_live_review','ok',0,0,1,?,'[]','[]',20)",
                   (json.dumps({'claim': 'reviewed-revision'}),))
        assert sync_poi_review_from_story(db, 'model-review', 100) == 1
        assert db.execute('SELECT reviewed_at FROM poi_research_assertions').fetchone()[0] == 20
        db.execute("UPDATE poi_research_assertions SET eligibility='withheld',review_status='disputed',reviewed_at=30")
        db.execute("UPDATE fact_conflict_scans SET detector='poi_memory_reuse'")
        db.execute("UPDATE fact_assertions SET updated_at=200")
        assert sync_poi_review_from_story(db, 'model-review', 200) == 0
        assert db.execute('SELECT eligibility FROM poi_research_assertions').fetchone()[0] == 'withheld'


def test_backfill_reviews_exact_alias_family_and_reader_honors_latest_review(tmp_path):
    store = Store(tmp_path / 'memory.sqlite3')
    with store.tx() as db:
        _bind_memory_aliases(db)
        _memory_story(db, 'review-new', 'osm:way:memory', 30)
        _memory_assertion(db, 'wiki:memory', 'blocked')
        _memory_review_fact(db, 'review-new', 'blocked')
        db.execute("INSERT INTO fact_assertions(story_id,assertion_id,semantic_key,display_text,owner_selected,review_status,eligibility,created_at,updated_at) VALUES('review-new','blocked','blocked','Факт.',0,'disputed','withheld',1,30)")
        assert backfill_poi_assertion_review_state(db, 30) == 1
        # A later acquisition under another alias cannot undo a reviewed dispute.
        _memory_assertion(db, 'osm:way:memory', 'blocked', updated_at=50)
        assert prior_facts(db, {'candidate_id': 'wiki:memory'}, 'next') == []


def test_prior_story_duplicates_do_not_exhaust_unique_fact_limit(tmp_path):
    store = Store(tmp_path / 'memory.sqlite3')
    with store.tx() as db:
        for i, fact_id in enumerate(('older-unique', 'repeated', 'repeated', 'repeated')):
            _memory_story(db, f'story-{i}', 'wiki:legacy', i + 1)
            db.execute("INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES(?,?,?,.9,1,0,'[]')", (f'story-{i}', fact_id, fact_id))
        facts = prior_facts(db, {'candidate_id': 'wiki:legacy'}, 'next', limit=2)
        assert [item['fact_id'] for item in facts] == ['repeated', 'older-unique']


def test_processed_sources_merges_alias_evidence_before_unique_url_limit(tmp_path):
    store = Store(tmp_path / 'memory.sqlite3')
    with store.tx() as db:
        _bind_memory_aliases(db)
        for key, url, timestamp, text in (
            ('wiki:memory', 'https://source.example/a', 20, 'старый отрывок'),
            ('osm:way:memory', 'https://source.example/a', 30, 'новый отрывок'),
            ('wiki:memory', 'https://source.example/b', 10, 'другой источник'),
        ):
            db.execute("INSERT INTO poi_research_sources(poi_key,url,title,last_query,supports_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,1,?)", (key, url, text, text, json.dumps([{'kind': 'verified_page_span', 'text': text, 'source_url': url}]), timestamp))
        sources = processed_sources(db, {'candidate_id': 'wiki:memory'}, limit=2)
        assert [source['url'] for source in sources] == ['https://source.example/a', 'https://source.example/b']
        assert sources[0]['last_query'] == 'новый отрывок'
        assert {support['text'] for support in sources[0]['supports']} == {'старый отрывок', 'новый отрывок'}


def test_review_projection_rejects_changed_literal_and_evidence_snapshot(tmp_path):
    store = Store(tmp_path / 'memory.sqlite3')
    with store.tx() as db:
        _bind_memory_aliases(db)
        _memory_story(db, 'review-new', 'osm:way:memory', 30)
        _memory_assertion(db, 'wiki:memory', 'claim')
        _memory_review_fact(db, 'review-new', 'claim')
        db.execute("INSERT INTO fact_assertions(story_id,assertion_id,semantic_key,display_text,owner_selected,review_status,eligibility,created_at,updated_at) VALUES('review-new','claim','claim','Другой факт.',0,'eligible','eligible',1,30)")
        assert sync_poi_review_from_story(db, 'review-new', 30) == 0
        assert backfill_poi_assertion_review_state(db, 30) == 1
        assert db.execute("SELECT eligibility FROM poi_research_assertions").fetchone()[0] == 'unreviewed'
        db.execute("UPDATE fact_assertions SET display_text='Факт.' WHERE story_id='review-new'")
        db.execute("UPDATE facts SET sources_json='[{\"url\":\"https://different.example/fact\"}]' WHERE story_id='review-new'")
        assert sync_poi_review_from_story(db, 'review-new', 30) == 0
        db.execute("UPDATE facts SET sources_json='[{\"url\":\"https://source.example/fact\"}]' WHERE story_id='review-new'")
        assert sync_poi_review_from_story(db, 'review-new', 30) == 1


def test_new_memory_evidence_invalidates_review_without_losing_observations(tmp_path):
    store = Store(tmp_path / 'memory.sqlite3')
    identity = {'candidate_id': 'wiki:memory'}
    source = {'url': 'https://source.example/fact', 'supports': [{'text': 'Факт.', 'kind': 'verified_page_span'}]}
    fact = {'fact_id': 'claim', 'text': 'Факт.', 'confidence': .9, 'sources': [source]}
    with store.tx() as db:
        _bind_memory_aliases(db)
        persist_research_memory(db, identity, [fact], [source], 'initial', 10)
        _memory_story(db, 'old-review', 'wiki:memory', 20)
        db.execute("INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES('old-review','claim','Факт.',.9,1,0,?)", (json.dumps([source]),))
        db.execute("INSERT INTO fact_assertions(story_id,assertion_id,semantic_key,display_text,owner_selected,review_status,eligibility,created_at,updated_at) VALUES('old-review','claim','claim','Факт.',0,'eligible','eligible',1,20)")
        db.execute("UPDATE poi_research_assertions SET review_status='eligible',eligibility='eligible',reviewed_at=20,review_story_id='old-review'")
        # Replaying the identical snapshot preserves its decision.
        persist_research_memory(db, identity, [fact], [source], 'replay', 30)
        assert db.execute("SELECT eligibility FROM poi_research_assertions").fetchone()[0] == 'eligible'
        enriched = {'url': source['url'], 'supports': [{'text': 'Новый отрывок.', 'kind': 'verified_page_span'}]}
        persist_research_memory(db, {'candidate_id': 'osm:way:memory'}, [{**fact, 'sources': [enriched]}], [enriched], 'enrichment', 40)
        assert {row[0] for row in db.execute("SELECT eligibility FROM poi_research_assertions")} == {'unreviewed'}
        assert db.execute("SELECT COUNT(*) FROM poi_research_observations").fetchone()[0] == 3
        # A legacy review of the older alias cannot requalify an enriched
        # canonical assertion's current evidence revision on the next backfill.
        assert backfill_poi_assertion_review_state(db, 50) == 0
        assert {row[0] for row in db.execute("SELECT eligibility FROM poi_research_assertions")} == {'unreviewed'}
        _memory_story(db, 'fresh-review', 'osm:way:memory', 55)
        db.execute("INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) VALUES('fresh-review','claim','Факт.',.9,1,0,?)", (json.dumps([enriched]),))
        db.execute("INSERT INTO fact_assertions(story_id,assertion_id,semantic_key,display_text,owner_selected,review_status,eligibility,created_at,updated_at) VALUES('fresh-review','claim','claim','Факт.',0,'eligible','eligible',1,55)")
        assert sync_poi_review_from_story(db, 'fresh-review', 55) == 1
        persist_research_memory(db, {'candidate_id': 'osm:way:memory'}, [{**fact, 'sources': [enriched]}], [enriched], 'same-latest-replay', 60)
        current = db.execute("SELECT eligibility,review_story_id FROM poi_research_assertions WHERE poi_key='osm:way:memory'").fetchone()
        assert tuple(current) == ('eligible', 'fresh-review')
        assert db.execute("SELECT COUNT(*) FROM poi_research_observations").fetchone()[0] == 4


def test_poi_research_memory_is_independent_of_story_rows(tmp_path):
    store = Store(tmp_path / "street-story.sqlite3")
    identity = {"candidate_id": "wiki:403645", "candidate_name": "Королевские ворота"}
    source = {
        "type": "web_search",
        "title": "Museum history",
        "url": "https://museum.example/royal-gate",
        "supports": [{"kind": "search_snippet", "source_url": "https://museum.example/royal-gate", "text": "Три скульптуры находятся на фасаде."}],
    }
    fact = {
        "fact_id": "claim-sculptures",
        "claim_key": "royal-gate-facade-sculptures",
        "text": "На фасаде находятся три исторические скульптуры.",
        "confidence": .95,
        "evidence_supported": True,
        "selected": True,
        "sources": [source],
    }
    with store.tx() as db:
        persist_research_memory(db, identity, [fact], [source], "скульптуры Королевских ворот", store.now())
    with store.connection() as db:
        facts = prior_facts(db, identity, "story-that-does-not-exist")
        sources = processed_sources(db, identity)
    assert facts[0]["fact_id"] == "claim-sculptures"
    assert facts[0]["claim_key"] == "royal-gate-facade-sculptures"
    assert facts[0]["origin"] == "poi_research"
    assert facts[0]["selected"] is False
    assert sources[0]["url"] == "https://museum.example/royal-gate"
    assert sources[0]["last_query"] == "скульптуры Королевских ворот"


def test_same_semantic_key_different_values_survive_canonical_poi_memory(tmp_path):
    store = Store(tmp_path / "street-story.sqlite3")
    identity = {"candidate_id": "wiki:403645", "candidate_name": "Королевские ворота"}
    source_a = {
        "type": "web",
        "title": "A",
        "url": "https://a.example/gate",
        "supports": [{
            "kind": "verified_page_span",
            "source_url": "https://a.example/gate",
            "text": "Строительство началось в 1843 году.",
        }],
    }
    source_b = {
        "type": "web",
        "title": "B",
        "url": "https://b.example/gate",
        "supports": [{
            "kind": "verified_page_span",
            "source_url": "https://b.example/gate",
            "text": "Строительство завершилось в 1850 году.",
        }],
    }
    facts = [
        {
            "fact_id": "claim-start-1843",
            "claim_key": "construction-year",
            "text": "Строительство началось в 1843 году.",
            "confidence": .95,
            "evidence_supported": True,
            "sources": [source_a],
        },
        {
            "fact_id": "claim-finish-1850",
            "claim_key": "construction-year",
            "text": "Строительство завершилось в 1850 году.",
            "confidence": .96,
            "evidence_supported": True,
            "sources": [source_b],
        },
    ]
    with store.tx() as db:
        persist_research_memory(
            db,
            identity,
            facts,
            [source_a, source_b],
            "история строительства",
            store.now(),
            research_run_id="run-poi-two-values",
        )
    with store.connection() as db:
        rows = list(db.execute(
            "SELECT assertion_id,semantic_key,text FROM poi_research_assertions "
            "WHERE poi_key=? ORDER BY assertion_id",
            ("wiki:403645",),
        ))
        observations = list(db.execute(
            "SELECT assertion_id,research_run_id FROM poi_research_observations "
            "WHERE poi_key=? ORDER BY assertion_id",
            ("wiki:403645",),
        ))
        legacy = list(db.execute(
            "SELECT claim_key,fact_id,text FROM poi_research_facts WHERE poi_key=?",
            ("wiki:403645",),
        ))
        hydrated = prior_facts(db, identity, "new-story")

    assert len(rows) == 2
    assert {row["semantic_key"] for row in rows} == {"construction-year"}
    assert {row["assertion_id"] for row in rows} == {"claim-start-1843", "claim-finish-1850"}
    assert len(observations) == 2
    assert {row["research_run_id"] for row in observations} == {"run-poi-two-values"}
    # Legacy compatibility snapshot remains lossy by design but never overwrites.
    assert len(legacy) == 1
    assert len([item for item in hydrated if item.get("origin") == "poi_research"]) == 2


def test_poi_source_passages_merge_instead_of_replace(tmp_path):
    store = Store(tmp_path / "street-story.sqlite3")
    identity = {"candidate_id": "wiki:403645", "candidate_name": "Королевские ворота"}
    url = "https://same.example/gate"
    first = {
        "type": "web",
        "title": "Same",
        "url": url,
        "supports": [{
            "kind": "search_snippet",
            "source_url": url,
            "text": "На фасаде находятся три фигуры.",
        }],
    }
    second = {
        "type": "web",
        "title": "Same",
        "url": url,
        "supports": [{
            "kind": "verified_page_span",
            "source_url": url,
            "text": "Слева направо: Отакар II, Фридрих I и Альбрехт I.",
        }],
    }
    fact_id = "claim-three-figures"
    with store.tx() as db:
        persist_research_memory(
            db,
            identity,
            [{
                "fact_id": fact_id,
                "claim_key": "facade-figures",
                "text": "На фасаде находятся три фигуры.",
                "confidence": .8,
                "evidence_supported": True,
                "sources": [first],
            }],
            [first],
            "первый проход",
            store.now(),
            research_run_id="run-1",
        )
        persist_research_memory(
            db,
            identity,
            [{
                "fact_id": fact_id,
                "claim_key": "facade-figures",
                "text": "На фасаде находятся три фигуры.",
                "confidence": .9,
                "evidence_supported": True,
                "sources": [second],
            }],
            [second],
            "второй проход",
            store.now() + 1,
            research_run_id="run-2",
        )
    with store.connection() as db:
        remembered = processed_sources(db, identity)
        assertion = db.execute(
            "SELECT sources_json FROM poi_research_assertions "
            "WHERE poi_key=? AND assertion_id=?",
            ("wiki:403645", fact_id),
        ).fetchone()
    assert {
        item["text"] for item in remembered[0]["supports"]
    } == {
        "На фасаде находятся три фигуры.",
        "Слева направо: Отакар II, Фридрих I и Альбрехт I.",
    }
    assertion_sources = __import__("json").loads(assertion["sources_json"])
    assert len(assertion_sources[0]["supports"]) == 2


def test_poi_review_sync_ignores_transient_unreviewed_and_propagates_withheld(tmp_path):
    store = Store(tmp_path / "street-story.sqlite3")
    identity = {"candidate_id": "wiki:review", "candidate_name": "Review Gate"}
    source = {
        "type": "web",
        "title": "Evidence",
        "url": "https://example.org/review",
        "supports": [{
            "kind": "verified_page_span",
            "source_url": "https://example.org/review",
            "text": "Verified review evidence.",
        }],
    }
    fact = {
        "fact_id": "claim-review",
        "claim_key": "review-key",
        "text": "Проверяемый факт.",
        "confidence": .9,
        "evidence_supported": True,
        "sources": [source],
    }
    now = store.now()
    story_id = "story_review_sync"
    with store.tx() as db:
        db.execute(
            "INSERT INTO stories("
            "id,client_story_id,photo_sha256,photo_mime_type,photo_path,voice_protocol,state,"
            "research_json,visual_context_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,? ,?,'{}',?,?)",
            (
                story_id,
                "client-review-sync",
                "a" * 64,
                "image/jpeg",
                "/tmp/no-photo.jpg",
                "voice-chunks-v2",
                "identity_ready",
                __import__("json").dumps({
                    "visual_identity": {
                        "status": "match",
                        "candidate_id": "wiki:review",
                        "candidate_name": "Review Gate",
                    }
                }),
                now,
                now,
            ),
        )
        persist_research_memory(
            db,
            identity,
            [fact],
            [source],
            "review test",
            now,
        )
        db.execute(
            "INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) "
            "VALUES(?,?,?,?,1,1,?)",
            (
                story_id,
                "claim-review",
                "Проверяемый факт.",
                .9,
                __import__("json").dumps([source], ensure_ascii=False),
            ),
        )
        db.execute(
            "INSERT INTO fact_assertions("
            "story_id,assertion_id,semantic_key,display_text,owner_selected,"
            "review_status,eligibility,revision_digest,created_at,updated_at"
            ") VALUES(?,?,?,?,1,'eligible','eligible','digest',?,?)",
            (
                story_id,
                "claim-review",
                "review-key",
                "Проверяемый факт.",
                now,
                now,
            ),
        )
        assert sync_poi_review_from_story(db, story_id, now) == 1

    with store.connection() as db:
        row = db.execute(
            "SELECT review_status,eligibility,review_story_id "
            "FROM poi_research_assertions WHERE poi_key='wiki:review' "
            "AND assertion_id='claim-review'"
        ).fetchone()
        assert dict(row) == {
            "review_status": "eligible",
            "eligibility": "eligible",
            "review_story_id": story_id,
        }

    # A later transient detector outage must not downgrade canonical POI memory.
    with store.tx() as db:
        db.execute(
            "UPDATE fact_assertions SET review_status='unreviewed',eligibility='unreviewed',updated_at=? "
            "WHERE story_id=? AND assertion_id='claim-review'",
            (now + 10, story_id),
        )
        assert sync_poi_review_from_story(db, story_id, now + 10) == 0

    with store.connection() as db:
        row = db.execute(
            "SELECT review_status,eligibility FROM poi_research_assertions "
            "WHERE poi_key='wiki:review' AND assertion_id='claim-review'"
        ).fetchone()
        assert dict(row) == {
            "review_status": "eligible",
            "eligibility": "eligible",
        }

    # An explicit reviewed dispute does propagate and prevents future hydration.
    with store.tx() as db:
        db.execute(
            "UPDATE fact_assertions SET review_status='disputed',eligibility='withheld',updated_at=? "
            "WHERE story_id=? AND assertion_id='claim-review'",
            (now + 20, story_id),
        )
        assert sync_poi_review_from_story(db, story_id, now + 20) == 1

    with store.connection() as db:
        row = db.execute(
            "SELECT review_status,eligibility FROM poi_research_assertions "
            "WHERE poi_key='wiki:review' AND assertion_id='claim-review'"
        ).fetchone()
        assert dict(row) == {
            "review_status": "disputed",
            "eligibility": "withheld",
        }
        reused = prior_facts(
            db,
            {"candidate_id": "wiki:review"},
            "different-story",
        )
        assert all(item["fact_id"] != "claim-review" for item in reused)
