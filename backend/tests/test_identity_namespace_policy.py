"""Article namespaces never manufacture received physical map primitives."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from street_story import identity_discovery
from street_story.identity_proof import freeze_geometry_proof
from street_story.identity_source_selection import (
    compact_planner_packet, expand_planner_packet, geometry_decision_schema,
    resolve_identity_response_ids,
)
from test_geometry_identity_plan import Executor, geometry_setup, geometry_decision, payload


def test_wikipedia_number_with_invented_osm_prefix_is_not_an_exact_transport_join():
    scene = {'objects': {'columns': ['label', 'candidate_id'], 'rows': [[1, 'osm:way:22']]}}
    packet = compact_planner_packet({'map_scene': scene,
        'wikipedia_metadata': {'columns': ['page_id', 'mapped_osm_ids'], 'rows': [['17', []]]}})
    decision = geometry_decision()
    decision.pop('spatial_correspondence')  # This assertion exercises the legacy namespace contract.
    decision['candidate_id'] = 'osm:way:22'
    decision['candidate_label'] = 1
    decision['decisive_relations'][0]['map_features'] = [{'candidate_id': 'osm:way:22', 'kind': 'contour'}]
    decision['rejected_alternatives'] = [{'candidate_id': 'osm:relation:17',
        'reason': 'Model incorrectly assigned an OSM namespace to an article-only number.'}]
    normalized, receipt = resolve_identity_response_ids(payload(decision), packet)
    assert receipt is None and normalized == payload(decision)
    assert Draft202012Validator(geometry_decision_schema([])).is_valid(decision)
    assert identity_discovery._geometry_binding_issues(decision, scene) == {
        'unreceived_alternative_ids': ['osm:relation:17']}
    assert 'exact @N/$N references are also allowed in identifier fields' in packet['join_policy']


@pytest.mark.asyncio
async def test_real_joint_transport_receives_namespace_scope_cue_without_extra_geometry_judge(tmp_path):
    service, story, candidates = geometry_setup(tmp_path)
    calls = []
    async def generate(key, timeout, contents, config, **kwargs):
        calls.append(contents)
        instruction = config.system_instruction
        assert 'Never prepend an OSM prefix to an article page number' in instruction
        assert '[] supplies no such association' in instruction
        assert 'copy a received exact ID or use the string @N' in instruction
        assert 'first_wave subject_id, spatial_hypotheses, regional selections/lookups' in instruction
        assert 'rejected_alternatives may be []' in instruction
        assert 'return uncertain if it leaves identity unresolved' in instruction
        return SimpleNamespace(text=json.dumps(payload(geometry_decision())))
    async def forbidden(*args, **kwargs):
        pytest.fail('An existing valid geometry decision needs no further semantic planner')
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=generate)
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden)
    await identity_discovery.prepare_search_plan(service, story, '', candidates)
    assert len(calls) == 1
    assert story['_identity_geometry_result']['geometry_proof']['validated'] is True


def test_saved_4cdd_actual_foreign_namespace_stays_rejected_by_unchanged_host_proof():
    retained = os.environ.get('STREET_STORY_SAVED_NAMESPACE_CASE')
    if not retained:
        pytest.skip('Offline actual-case replay explicitly supplies its retained database')
    database = Path(retained)
    with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        story = dict(db.execute('SELECT * FROM stories').fetchone())
    research = json.loads(story['research_json'])
    saved = research['identity_joint_initial']['closed_plan']
    raw = json.loads(saved['raw_json'])
    assert raw == saved['payload']
    assert hashlib.sha256(saved['raw_json'].encode()).hexdigest() == research['identity_joint_initial']['response_sha256']
    packet = json.loads(saved['prompt'].split('Данные ниже — только контекст:\n', 1)[1])
    normalized, receipt = resolve_identity_response_ids(raw, packet)
    assert receipt is None and normalized == raw
    Draft202012Validator(saved['schema']).validate(normalized)
    plain = expand_planner_packet(packet)
    decision = raw['accepted_geometry']
    issues = identity_discovery._geometry_binding_issues(decision, plain['map_scene'])
    assert list(issues) == ['unreceived_alternative_ids']
    missing = issues['unreceived_alternative_ids']
    wiki = plain['wikipedia_metadata']
    pages = [dict(zip(wiki['columns'], row)) for row in wiki['rows']]
    assert all(any(cid.split(':')[-1] == page['page_id'] and page['mapped_osm_ids'] == []
        for page in pages) for cid in missing)
    identity = research['visual_identity']
    story['_identity_observed_candidates'] = identity['observed_candidates']
    story['_identity_map_snapshot'] = research['osm']
    story['_camera_hints'] = plain['camera_hints']
    original = database.parent / 'stories' / story['id'] / 'source.original'
    story['_identity_original_source_sha256'] = hashlib.sha256(original.read_bytes()).hexdigest()
    source_map_receipt = research['identity_article_discovery']['search_plan']['payload']['source_map_receipt']
    assert freeze_geometry_proof(story, decision, source_map_receipt, identity['candidates']) is None
