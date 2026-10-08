import hashlib
import io
import json

from PIL import Image

import pytest

from street_story.headless_facts import acquired_subject_articles, reviewed_reference_articles

URL = 'https://ru.wikipedia.org/wiki/Documented_building'


def geometry_identity(*, candidate_id='osm:way:7', photo='a'*64, generation=2, revision=0, observed_buildings=1):
    # Freeze actual observed vector + rendered map receipt; no REF or inference.
    from street_story.identity_map_context import map_entry_context
    from street_story.identity_proof import freeze_geometry_proof
    from street_story.identity_scene import render_scene
    from test_geometry_identity_plan import geometry_decision
    from test_identity_scene import building
    raw = building(int(candidate_id.rsplit(':', 1)[1]), 20)
    candidate = map_entry_context(raw)
    pool = [raw, *[building(index, index*15) for index in range(1000, 1000+observed_buildings-1)]]
    story = {'photo_sha256': photo, 'latitude': 54.7, 'longitude': 20.5,
        '_identity_generation': generation, '_identity_research_control_revision': revision,
        '_identity_map_snapshot': {'observed_pool': pool}, '_identity_observed_candidates': [candidate]}
    scene = render_scene(story, [candidate])
    decision = geometry_decision()
    decision.update(candidate_id=candidate_id, rejected_alternatives=[])
    table = scene['manifest']['objects']
    decision['candidate_label'] = next(dict(zip(table['columns'], row))['label'] for row in table['rows']
        if dict(zip(table['columns'], row))['candidate_id'] == candidate_id)
    decision['decisive_relations'][0]['map_features'] = [{'candidate_id': candidate_id, 'kind': 'contour'}]
    source = io.BytesIO()
    Image.new('RGB', (16, 16), 'gray').save(source, format='PNG')
    source_digest = hashlib.sha256(source.getvalue()).hexdigest()
    receipt = {'joint_image_input': True, 'source_photo_sha256': photo,
        'original_source_sha256': source_digest, 'model_source_sha256': source_digest,
        'map_image_sha256': scene['manifest']['image_sha256'], 'manifest': scene['manifest']}
    proof = freeze_geometry_proof(story, decision, receipt, [candidate])
    assert proof is not None
    return {'status': 'match', 'proof_kind': 'geometry', 'visual_reference_verified': False,
        'candidate_id': candidate_id, 'candidate_name': 'Observed building',
        'photo_sha256': photo, 'generation': generation, 'control_revision': revision,
        'geometry_proof': proof, 'candidates': [candidate], 'reference_evidence': []}


def subject_fixture():
    identity = geometry_identity()
    research = {'identity_generation': 2, 'wikipedia': [{'pageid': 77, 'title': 'Documented building', 'url': URL,
        'mapped_wikipedia_sources': [{'candidate_id': 'osm:way:7', 'source_url': 'https://www.openstreetmap.org/way/7'}]}]}
    return identity, research


def test_geometry_linked_article_is_lead_without_any_reviewed_reference():
    identity, research = subject_fixture()
    assert reviewed_reference_articles(identity) == {}
    lead = acquired_subject_articles(identity, research)[URL]
    assert lead['subject_candidate_ids'] == ['osm:way:7']
    assert lead['acquisition_kind'] == 'accepted_identity_subject_lead'
    assert lead['visual_reference_verified'] is False


def test_selected_nearby_wiki_article_or_neighbor_is_not_bound_to_main_subject():
    identity, research = subject_fixture()
    research['wikipedia'][0]['mapped_wikipedia_sources'][0]['candidate_id'] = 'osm:way:neighbor'
    research['identity_article_discovery'] = {'search_plan': {'payload': {'selected_wikipedia_page_ids': ['77']}}}
    assert acquired_subject_articles(identity, research) == {}


@pytest.mark.parametrize('change', [{'status': 'uncertain'}, {'generation': 1}, {'photo_sha256': ''}])
def test_unaccepted_or_stale_identity_does_not_open_subject_leads(change):
    identity, research = subject_fixture()
    identity.update(change)
    assert acquired_subject_articles(identity, research) == {}


@pytest.mark.parametrize('stale', [None, 'photo', 'generation', 'control'])
def test_saved_explicit_subject_sources_keep_original_fences(stale):
    identity, research = subject_fixture()
    research['wikipedia'] = []
    fence = {'photo_sha256': identity['photo_sha256'], 'generation': 2, 'control_revision': 0}
    plan = dict(fence)
    if stale == 'photo':
        plan['photo_sha256'] = 'old'
    elif stale == 'generation':
        plan['generation'] = 1
    elif stale == 'control':
        plan['control_revision'] = 3
    research['identity_article_discovery'] = {**fence, 'search_plan': plan,
        'sources': [{'url': URL, 'title': 'Historical lead', 'physical_subject_candidate_ids': ['osm:way:7']},
                    {'url': 'https://example.org/neighbor', 'physical_subject_candidate_ids': ['osm:way:neighbor']}]}
    found = acquired_subject_articles(identity, research)
    assert list(found) == ([URL] if stale is None else [])
    # Lead selection never fabricates canonical claims or REF receipts.
    assert json.loads(json.dumps(identity))['reference_evidence'] == []


@pytest.mark.parametrize('change', [{'photo_sha256': 'b'*64}, {'generation': 3}, {'control_revision': 1}])
def test_current_geometry_scope_fence_cannot_authorize_old_subject(change):
    from street_story.identity_proof import accepted_identity
    identity, research = subject_fixture()
    current = {'photo_sha256': identity['photo_sha256'], 'generation': 2, 'control_revision': 0}
    current.update(change)
    assert not accepted_identity(identity, **current)
    assert identity['visual_reference_verified'] is False
