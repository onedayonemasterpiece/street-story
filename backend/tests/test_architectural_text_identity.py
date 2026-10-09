"""Actual frozen text pointers admit physical identity without external images."""
import copy
import hashlib
import json

import pytest

from street_story.headless_facts import acquired_subject_articles, reviewed_reference_articles
from street_story.identity_model_context import compact_physical_identity
from street_story.identity_proof import (accepted_identity, architectural_text_result_valid,
    freeze_architectural_text_proof, verified_physical_identity)
from test_geometry_subject_articles import geometry_identity

URL = 'https://archive.example/physical-building'
TEXT = 'The facade has a central bay with three vertical window axes and a semicircular cornice. The entrance is on the left.'


def with_received_physical_links(decision, contents):
    """Fixture model links the literal records in the actual issued T packet."""
    inventory = None
    for line in contents[-1].splitlines():
        try:
            packet = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(packet, dict):
            continue
        found = (packet if packet.get('contract') == 'llm-first-literal-evidence-v1' else
            packet.get('publisher_and_OSM_literal_records_NOT_prejoined') or
            (packet.get('acquired_architectural_text') or {}).get('publisher_and_OSM_literal_records_NOT_prejoined'))
        if found:
            inventory = found
    assert inventory, 'The actual issued model packet must include literal evidence.'
    answer = copy.deepcopy(decision)
    answer['physical_link_evidence'] = [{
        'article_id': binding['article_id'], 'candidate_id': binding['candidate_id'],
        'publisher_ref': next(ref for ref, row in inventory['publisher_refs'].items()
            if row['article_id'] == binding['article_id']),
        'osm_ref': next(ref for ref, row in inventory['osm_refs'].items()
            if row['candidate_id'] == binding['candidate_id']),
        'relationship': 'same_individual_physical_body',
        'subject_scope': 'specific_photographed_OSM_body',
        'architectural_scope_explanation': 'Fixture SOURCE and article describe this bay and return.',
        'postal_interpretation': 'Literal received records identify the fixture physical scope.'}
        for binding in answer['article_bindings']]
    return answer


def text_inputs(*, candidate_id='osm:way:7', photo='a'*64, generation=2, revision=0, url=URL):
    observed = geometry_identity(candidate_id=candidate_id, photo=photo, generation=generation, revision=revision)
    source = observed['geometry_proof']['source_map_receipt']
    story = {'photo_sha256': photo, '_identity_generation': generation,
        '_identity_research_control_revision': revision, '_identity_observed_candidates': observed['candidates']}
    body = TEXT.encode('cp1251')
    article = {'article_id': 'catalog:physical-building', 'url': url, 'title': 'Physical building',
        'source_sha256': hashlib.sha256(body).hexdigest(), 'text_sha256': hashlib.sha256(TEXT.encode()).hexdigest(),
        'raw_body_sha256_verified': True, 'input_kind': 'acquired_article_text', 'text': TEXT,
        'address': 'Observed literal street 7', 'scope': 'Main physical building'}
    receipt = {key: source[key] for key in ('source_photo_sha256', 'original_source_sha256', 'model_source_sha256')}
    receipt.update(source_image_input=True, source_preparation={'orientation_applied': True, 'width': 16, 'height': 16}, articles=[article])
    decision = {'decision': 'accepted_architectural_text', 'candidate_id': candidate_id,
        'scope': 'Main physical building; institution and adjoining wing are separate subjects.',
        'discriminating_combination': 'Bay shape, three window axes and cornice placement agree in SOURCE.',
        'article_bindings': [{'article_id': article['article_id'], 'candidate_id': candidate_id,
            'scope': 'Main physical building', 'binding_basis': 'Received address describes this physical candidate, not its neighbor.',
            'physical_binding_resolved': True}],
        'correspondences': [{'article_id': article['article_id'], 'source_quote': TEXT.split('. ')[0] + '.',
            'source_observation': 'The central bay has three window axes and a semicircular upper cornice.',
            'status': 'stable_match', 'feature_kind': 'composition',
            'reason': 'The relations among these stable elements agree.'}],
        'material_alternatives': [], 'material_alternatives_resolved': True,
        'unresolved_contradictions': [], 'limitations': ['Entrance is cropped; paint is mutable.']}
    return story, observed['candidates'], decision, receipt


def architectural_identity(**kwargs):
    story, candidates, decision, receipt = text_inputs(**kwargs)
    proof = freeze_architectural_text_proof(story, decision, receipt, candidates)
    assert proof is not None
    return {'status': 'match', 'proof_kind': 'architectural_text', 'visual_reference_verified': False,
        'candidate_id': decision['candidate_id'], 'candidate_name': 'Observed building',
        'photo_sha256': story['photo_sha256'], 'generation': story['_identity_generation'],
        'control_revision': story['_identity_research_control_revision'],
        'architectural_text_proof': proof, 'candidates': candidates, 'reference_evidence': []}


def test_text_identity_has_actual_source_hash_and_exact_lead_without_fake_ref():
    identity = architectural_identity()
    identity = json.loads(json.dumps(identity))
    assert accepted_identity(identity) and verified_physical_identity(identity)
    assert reviewed_reference_articles(identity) == {}
    lead = acquired_subject_articles(identity, {'identity_generation': 2})[URL]
    assert lead['visual_reference_verified'] is False
    compact = compact_physical_identity(identity)
    assert compact['physical_identity_accepted'] is True
    assert compact['physical_scope'] == identity['architectural_text_proof']['decision']['scope']
    assert 'architectural_text_proof' not in compact
    assert 'Main physical building' in compact['physical_scope']


def test_joint_model_selected_span_freezes_same_literal_proof_without_rewriting_claims():
    from street_story.identity_architectural_pool import joint_source_spans
    from street_story.identity_proof import architectural_text_decision_schema
    from jsonschema import Draft202012Validator
    story, candidates, decision, receipt = text_inputs()
    passages, refs = joint_source_spans(receipt['articles'])
    assert passages[0]['all_passages_displayed']
    chosen = next(ref for ref, span in refs.items()
        if span['source_quote'] == decision['correspondences'][0]['source_quote'])
    expected = freeze_architectural_text_proof(story, decision, receipt, candidates)
    requested = copy.deepcopy(decision)
    relation = requested['correspondences'][0]
    relation.pop('source_quote')
    relation['source_span_ref'] = chosen
    schema = architectural_text_decision_schema([candidates[0]['candidate_id']],
        [receipt['articles'][0]['article_id']], structural=True, source_span_refs=refs)
    assert Draft202012Validator(schema).is_valid(requested)
    receipt['source_span_refs'] = refs
    proof = freeze_architectural_text_proof(story, requested, receipt, candidates)
    assert proof and proof['decision'] == expected['decision']
    assert requested['correspondences'][0]['source_span_ref'] == chosen
    assert 'source_quote' not in requested['correspondences'][0]


@pytest.mark.parametrize('change', ['unreceived_ref', 'wrong_article', 'changed_offset', 'changed_text', 'both'])
def test_joint_literal_span_cannot_repair_unreceived_or_changed_evidence(change):
    from street_story.identity_architectural_pool import joint_source_spans
    story, candidates, decision, receipt = text_inputs()
    _, refs = joint_source_spans(receipt['articles'])
    chosen = next(iter(refs))
    relation = decision['correspondences'][0]
    literal = relation.pop('source_quote')
    relation['source_span_ref'] = chosen
    receipt['source_span_refs'] = refs
    if change == 'unreceived_ref':
        relation['source_span_ref'] = 'invented'
    elif change == 'wrong_article':
        refs[chosen]['article_id'] = 'another-article'
    elif change == 'changed_offset':
        refs[chosen]['start'] += 1
    elif change == 'changed_text':
        refs[chosen]['source_quote'] += ' invented'
    else:
        relation['source_quote'] = literal
    assert freeze_architectural_text_proof(story, decision, receipt, candidates) is None


@pytest.mark.parametrize('change', ['text_hash', 'raw_unverified', 'snippet', 'no_source_image', 'invented_quote',
    'wrong_physical_binding', 'unresolved_scope', 'general_history', 'material_alternative', 'structural_contradiction'])
def test_missing_or_wrong_evidence_cannot_freeze_text_identity(change):
    story, candidates, decision, receipt = text_inputs()
    if change == 'text_hash':
        receipt['articles'][0]['text'] += ' Altered after observation.'
    elif change == 'raw_unverified':
        receipt['articles'][0]['raw_body_sha256_verified'] = False
    elif change == 'snippet':
        receipt['articles'][0]['input_kind'] = 'search_snippet'
    elif change == 'no_source_image':
        receipt['source_image_input'] = False
    elif change == 'invented_quote':
        decision['correspondences'][0]['source_quote'] = 'An invented arch not present in this article.'
    elif change == 'wrong_physical_binding':
        decision['article_bindings'][0]['candidate_id'] = ''
    elif change == 'unresolved_scope':
        decision['article_bindings'][0]['physical_binding_resolved'] = False
    elif change == 'general_history':
        # Controlled semantic model correctly leaves generic/institution-only prose uncertain.
        decision['decision'] = 'uncertain'
        decision['correspondences'] = []
    elif change == 'material_alternative':
        decision['material_alternatives_resolved'] = False
    else:
        decision['unresolved_contradictions'] = ['SOURCE has a different principal volume arrangement.']
    assert freeze_architectural_text_proof(story, decision, receipt, candidates) is None


@pytest.mark.parametrize('field,value', [('photo_sha256', 'b'*64), ('generation', 3), ('control_revision', 1)])
def test_text_receipt_cannot_be_replayed_in_another_owner_fence(field, value):
    story, candidates, _decision, _receipt = text_inputs()
    identity = architectural_identity()
    expected = {'photo_sha256': story['photo_sha256'], 'generation': 2, 'control_revision': 0}
    expected[field] = value
    assert not accepted_identity(identity, **expected)
    if field == 'photo_sha256':
        story[field] = value
    else:
        story['_identity_' + ('generation' if field == 'generation' else 'research_control_revision')] = value
    assert not architectural_text_result_valid(identity, candidates, story)


def test_pending_text_replay_survives_decorative_promotion_but_not_changed_physical_metadata():
    story, candidates, _decision, _receipt = text_inputs()
    identity = architectural_identity()
    decorated = copy.deepcopy(candidates)
    decorated[0].update(name='Publisher alias', entity_aliases=['Alternate title'], reference_image_urls=['https://example.org/not-sent.jpg'])
    story['_identity_observed_candidates'] = decorated
    assert architectural_text_result_valid(identity, decorated, story)
    decorated[0]['map_address'] = {'street': 'Another street', 'house_number': '6A'}
    assert not architectural_text_result_valid(identity, decorated, story)


def test_tampered_frozen_text_does_not_authorize_facts():
    identity = architectural_identity()
    identity['architectural_text_proof']['decision']['scope'] = 'Whole complex instead of exact building'
    assert not accepted_identity(identity) and acquired_subject_articles(identity, {'identity_generation': 2}) == {}
    assert json.dumps(identity['reference_evidence']) == '[]'


def test_geometry_proof_with_many_integer_map_labels_survives_durable_json_keys():
    identity = geometry_identity(observed_buildings=120)
    restored = json.loads(json.dumps(identity))
    assert accepted_identity(identity) and accepted_identity(restored)
    assert restored['geometry_proof']['proof_sha256'] == identity['geometry_proof']['proof_sha256']


@pytest.mark.asyncio
async def test_normal_identity_worker_commits_text_and_stops_before_reference_search(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from street_story import identity_discovery
    from test_identity_lifecycle import make_service, create
    from test_identity_scene import building
    service, gemini = make_service(tmp_path)
    story = create(service)
    async def lookup(*args):
        return {'observed_pool': [building(7, 20)], 'nearby': [building(7, 20)]}
    async def planner(service, snapshot, transcript, candidates):
        selected = next(item for item in snapshot['_identity_observed_candidates'] if item['candidate_id'] == 'osm:way:7')
        _fixture, _candidates, decision, receipt = text_inputs(photo=snapshot['photo_sha256'],
            generation=snapshot['_identity_generation'], revision=snapshot['_identity_research_control_revision'])
        actual_source = hashlib.sha256(service._source_photo_bytes(snapshot['id'])).hexdigest()
        receipt.update(original_source_sha256=actual_source, model_source_sha256=actual_source)
        proof = freeze_architectural_text_proof(snapshot, decision, receipt, [selected])
        assert proof is not None
        snapshot['_identity_geometry_result'] = {'status': 'match', 'proof_kind': 'architectural_text',
            'candidate_id': selected['candidate_id'], 'architectural_text_proof': proof,
            '_references_sent': [], 'visual_reference_verified': False, 'observations': ['Observed bay/window relationships agree.']}
        if selected['candidate_id'] not in {item['candidate_id'] for item in candidates}:
            candidates.append(selected)
    async def forbidden(*args, **kwargs):
        pytest.fail('Accepted text must stop before any REF or repeated identity inference')
    service.providers.osm.lookup = lookup
    service.providers.research = SimpleNamespace(plan_identity_search=forbidden, vision_available=False)
    service._identify_photo = forbidden
    monkeypatch.setattr(identity_discovery, 'prepare_search_plan', planner)
    monkeypatch.setattr(identity_discovery, 'recover', forbidden)
    service.ensure_identity(story['id'])
    assert await service.run_once()
    current = service.story(story['id'])
    assert current['state'] == 'identity_ready'
    identity = current['visual_identity']
    assert identity['proof_kind'] == 'architectural_text' and accepted_identity(identity)
    assert identity['candidate_id'] == 'osm:way:7' and identity['visual_reference_verified'] is False
    assert current['identity_progress']['physical_identity_verified'] is True
    assert current['identity_progress']['visual_comparison_verified'] is False
    assert gemini.identity_calls == [] and current['facts'] == []
    with service.store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM pois').fetchone()[0] == 1
