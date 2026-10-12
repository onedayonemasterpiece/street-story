"""Offline request/proof boundary checks; no provider or external network calls."""

import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import types

import pytest
from jsonschema import Draft202012Validator
from street_story.identity_architectural_comparison import (
    architectural_comparison_scope,
    normalize_architectural_decision,
    retained_text_portion_role,
    prepare_architectural_comparison,
)
from street_story.identity_candidate_policy import physical_research_priority, promote_observed_candidates
from street_story.identity_proof import freeze_architectural_text_proof
from street_story.identity_source_selection import freeze_identity_response_context, resolve_identity_response_ids
from test_architectural_t_supply import _comparison_fixture, _issued_span_decision
from test_identity_spatial_features import building, story as spatial_story


def body(cid):
    return {"candidate_id": cid, "identity_eligible": True, "map_object": {"tags": {"building": "yes"}}}


def pair(a, b, rid):
    return {
        "id": rid,
        "kind": "observed_side_pair",
        "map_features": [
            {"candidate_id": a, "kind": "segment", "ring_index": 0, "segment_index": 0},
            {"candidate_id": b, "kind": "segment", "ring_index": 0, "segment_index": 0},
        ],
    }


def request(cid):
    return {
        "candidate_ids": [cid],
        "reason": "This exact mapped body is unexamined.",
        "next_question": "Acquire its own facade image, not the adjacent complex.",
        "next_step": "expand_reserve",
        "contradictions": [],
    }


def test_scope_preserves_unexamined_reserve_and_never_traverses_transitively():
    x = architectural_comparison_scope(
        ["a"], ["a", "b", "c"], {"a": ["b"], "b": ["a", "c"], "c": ["b"]}, [pair("a", "b", "r1"), pair("b", "c", "r2")]
    )
    assert x["comparison_candidate_ids"] == ["a", "b"]
    assert x["host_reserve_candidate_ids"] == ["c"]
    assert [r["id"] for r in x["observed_map_relations"]] == ["r1"]
    assert x["context_only_expansions"][0]["identity_established"] is False


def test_scope_is_permutation_invariant_without_topN():
    ids = [str(i) for i in range(100)]
    rels = [pair("a", i, i) for i in ids]
    x = architectural_comparison_scope(["a"], ["a", *ids, "reserve"], {"a": ids}, rels)
    y = architectural_comparison_scope(["a"], ["reserve", *reversed(ids), "a"], {"a": list(reversed(ids))}, list(reversed(rels)))
    assert x["comparison_candidate_ids"] == y["comparison_candidate_ids"]
    assert len(x["comparison_candidate_ids"]) == 101
    assert x["host_reserve_sha256"] == y["host_reserve_sha256"]


@pytest.mark.parametrize(
    "nominated,physical,pairs,relations",
    [(["missing"], ["a"], {}, []), (["a"], ["a"], {"a": ["missing"]}, []), (["a"], ["a", "b"], {"a": ["b"]}, [])],
)
def test_unobserved_or_unproven_scope_fails(nominated, physical, pairs, relations):
    with pytest.raises(ValueError):
        architectural_comparison_scope(nominated, physical, pairs, relations)


def test_actual_builder_omits_rows_and_enum_for_unissued_reserve():
    source, candidates, decision, receipt = _comparison_fixture()
    candidates.extend(body(f"osm:way:{n}") for n in range(100, 300))
    old = copy.deepcopy(receipt)
    result = prepare_architectural_comparison(source, candidates, receipt)
    packet = json.loads(result["prompt"].rsplit("\n", 1)[-1])
    assert packet["physical_reserve"]["count"] == 200
    assert "rows" not in packet["physical_reserve"]
    assert "osm:way:299" not in result["prompt"] + json.dumps(result["schema"])
    assert result["candidate_ids"] == [row["candidate_id"] for row in packet["physical_candidates"]]
    assert receipt == old


def test_received_reserve_request_is_consumed_by_existing_candidate_promotion():
    source, candidates, decision, receipt = _comparison_fixture()
    candidates.append(body("osm:way:999"))
    result = prepare_architectural_comparison(source, candidates, receipt)
    decision = _issued_span_decision(decision, result, receipt)
    decision.update(decision="uncertain", candidate_id="", research_priority=request("osm:way:999"))
    answer = normalize_architectural_decision(decision, result["schema"], received_candidate_ids=result["received_candidate_ids"])
    priority = physical_research_priority(source, {"accepted_architectural_text": answer}, candidates, receipt)
    assert priority["active_candidate_ids"] == ["osm:way:999"]
    promoted = promote_observed_candidates(candidates[:2], candidates, priority["active_candidate_ids"])
    assert promoted[-1]["candidate_id"] == "osm:way:999"
    assert not priority["identity_established"]
    with pytest.raises(ValueError, match="unobserved"):
        normalize_architectural_decision(
            {**decision, "research_priority": request("osm:way:999999")},
            result["schema"],
            received_candidate_ids=result["received_candidate_ids"],
        )
    with pytest.raises(ValueError, match="cannot_accept"):
        normalize_architectural_decision(
            {**decision, "decision": "accepted_architectural_text"},
            result["schema"],
            received_candidate_ids=result["received_candidate_ids"],
        )


def test_frozen_pointer_context_survives_UNKNOWN_label_permutation():
    original = {"map_scene": {"objects": {"columns": ["label", "candidate_id"], "rows": [[7, "osm:way:1"], [8, "osm:way:2"]]}}}
    frozen = freeze_identity_response_context(original)
    original["map_scene"]["objects"]["rows"] = [[7, "osm:way:2"], [8, "osm:way:1"]]
    value, proof = resolve_identity_response_ids({"research_priority": request("@7")}, frozen)
    assert value["research_priority"]["candidate_ids"] == ["osm:way:1"]
    assert proof["resolutions"][0]["reference"] == "@7"


def test_more_than_eight_issued_alternatives_keep_one_proof_contract():
    source, candidates, decision, receipt = _comparison_fixture()
    main = candidates[0]["candidate_id"]
    # Eight nominated bodies, two additional exact side-pair counterparts.
    candidates.extend(body(f"osm:way:{n}") for n in range(100, 108))
    receipt["conditional_initial_decision"]["candidate_ids"] = [main, "osm:way:88", *[f"osm:way:{n}" for n in range(100, 106)]]
    source.update(spatial_story([building(7, 10, 20), building(106, 50, 20), building(107, 90, 20)]))
    receipt["manifest"] = {"relation_options": [pair(main, "osm:way:106", "R1"), pair(main, "osm:way:107", "R2")]}
    result = prepare_architectural_comparison(source, candidates, receipt)
    assert len(result["comparison_scope"]["comparison_candidate_ids"]) == 10
    receipt.update(
        physical_link_inventory=result["physical_link_inventory"],
        comparison_scope=result["comparison_scope"],
        required_map_counterpoints=result["required_map_counterpoints"],
    )
    decision["material_alternatives"] = [
        {"candidate_id": cid, "reason": "Fixture SOURCE visibly distinguishes its facade."}
        for cid in result["comparison_scope"]["comparison_candidate_ids"]
        if cid != main
    ]
    decision = _issued_span_decision(decision, result, receipt)
    assert len(decision["material_alternatives"]) == 9
    assert Draft202012Validator(result["schema"]).is_valid(decision)
    assert freeze_architectural_text_proof(source, decision, receipt, candidates) is not None
    receipt["comparison_scope"]["comparison_candidate_ids"].append("osm:way:unobserved")
    assert freeze_architectural_text_proof(source, decision, receipt, candidates) is None


def test_text_portion_upgrade_resumes_exact_original_role_without_new_send():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("create table research_provider_attempts(story_id text,role text,receipt_json text,created_at real)")
    source = {"id": "story-a", "photo_sha256": "a" * 64, "_identity_generation": 2, "_identity_research_control_revision": 3}
    receipt = {
        "original_source_sha256": "b" * 64,
        "articles": [{"article_id": "article-a", "source_sha256": "c" * 64, "text_sha256": "d" * 64, "text_cursor": 12000}],
    }
    saved = {
        "phase": "unknown",
        "binding": {"generation": 2, "control_revision": 3, "source_map_photo_sha256": "a" * 64},
        "frozen_source_map": {"host_contract": {"legacy": "exact-original"}, "host_context": {"source_text_receipt": receipt}},
    }
    role = "vision_native_text_portion:original-prompt-unit"
    db.execute("insert into research_provider_attempts values(?,?,?,?)", ("story-a", role, json.dumps(saved), 1))

    @contextmanager
    def connection():
        yield db

    service = types.SimpleNamespace(store=types.SimpleNamespace(connection=connection))
    assert retained_text_portion_role(service, source, copy.deepcopy(receipt)) == role
    changed = copy.deepcopy(receipt)
    changed["articles"][0]["text_cursor"] = 24000
    assert retained_text_portion_role(service, source, changed) is None
    assert retained_text_portion_role(service, {**source, "_identity_research_control_revision": 4}, receipt) is None
    db.close()


SAVED = Path("/home/dev/artifacts/street-story/20261011T082451Z-product-recovery-finish-20261011/final-8a4d3a9/data/street-story.sqlite3")
CASES = [
    (104, "story_3e628061fdb659793481a2f2", 102, 5),
    (108, "story_2d889a6a5e942f37fc8da9ba", 106, 6),
    (130, "story_d2fb872039db83de19a7f863", 204, 6),
]


@pytest.mark.skipif(not SAVED.is_file(), reason="Saved real corpus is unavailable in this environment")
@pytest.mark.parametrize("photo,sid,before_count,after_count", CASES)
def test_saved_real_inputs_before_after_without_inference(photo, sid, before_count, after_count):
    db = sqlite3.connect("file:" + str(SAVED) + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    source = dict(db.execute("select * from stories where id=?", (sid,)).fetchone())
    db.close()
    r = json.loads(source["research_json"])
    v = r["visual_identity"]
    source.update(_identity_observed_candidates=v["observed_candidates"], _identity_map_snapshot=r["osm"])
    candidates = v["candidates"]
    receipt = r["identity_article_discovery"]["search_plan"]["payload"]["source_text_receipt"]
    frozen = json.loads(r["identity_joint_followup"]["prepared_request"]["prompt"].rsplit("\n", 1)[-1])
    base = subprocess.run(
        ["git", "show", "b46559aab260cb20dff6062837dc3c5eac938f43:backend/street_story/identity_architectural_comparison.py"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    before_module = types.ModuleType("street_story._g_handoff_b465")
    before_module.__package__ = "street_story"
    exec(compile(base, "b46559a:identity_architectural_comparison.py", "exec"), before_module.__dict__)
    before = before_module.prepare_architectural_comparison(source, candidates, receipt, complete_source=True)
    after = prepare_architectural_comparison(source, candidates, receipt, complete_source=True)
    old = json.loads(before["prompt"].rsplit("\n", 1)[-1])
    new = json.loads(after["prompt"].rsplit("\n", 1)[-1])
    prior_ids = [c["candidate_id"] for c in old["physical_candidates"]]
    before_ids = prior_ids + [row[0] for row in old["physical_reserve"]["rows"]]
    frozen_ids = [c["candidate_id"] for c in frozen["physical_candidates"]] + [row[0] for row in frozen["physical_reserve"]["rows"]]
    after_ids = [c["candidate_id"] for c in new["physical_candidates"]]
    assert set(before_ids) == set(frozen_ids)
    assert len(before_ids) == before_count and len(after_ids) == after_count
    assert set(after_ids) | set(after["comparison_scope"]["host_reserve_candidate_ids"]) == set(before_ids)
    assert not set(after_ids) & set(after["comparison_scope"]["host_reserve_candidate_ids"])
    assert set(after["candidate_ids"]) == set(prior_ids)
    if photo == 130:
        assert "osm:way:193106188" in after_ids
        assert "osm:way:193106188" not in after["candidate_ids"]
        e = next(x for x in after["comparison_scope"]["context_only_expansions"] if x["candidate_id"] == "osm:way:193106188")
        assert e["relation_ids"] == ["R1", "R2"]
    if photo == 108:
        assert "osm:relation:1383564" in after["candidate_ids"]
    schema = json.dumps(after["schema"])
    for cid in after["comparison_scope"]["host_reserve_candidate_ids"]:
        assert cid not in schema
    output = {
        "photo": photo,
        "runtime_sha": "8a4d3a9",
        "candidate_base_sha": "b46559a",
        "kind": "saved_input_reconstruction_no_inference",
        "before_count": len(before_ids),
        "after_count": len(after_ids),
        "positive_ids": after["candidate_ids"],
        "comparison_ids": after_ids,
        "expansion_reasons": after["comparison_scope"]["context_only_expansions"],
        "host_reserve_count": new["physical_reserve"]["count"],
        "before_prompt_bytes": before["utf8_bytes"],
        "after_prompt_bytes": after["utf8_bytes"],
        "before_schema_bytes": len(json.dumps(before["schema"]).encode()),
        "after_schema_bytes": len(schema.encode()),
        "before_packet_sha256": hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest(),
        "after_packet_sha256": hashlib.sha256(json.dumps(new, sort_keys=True).encode()).hexdigest(),
    }
    print("G_HANDOFF_SNAPSHOT " + json.dumps(output, ensure_ascii=False, sort_keys=True))
