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


@pytest.mark.parametrize('valid', [True, False])
def test_current_semantic_wiki_binding_can_supply_unmapped_article_lead(valid):
    identity, research = subject_fixture()
    research['wikipedia'][0]['mapped_wikipedia_sources'] = []
    fence = {'photo_sha256': identity['photo_sha256'], 'generation': 2, 'control_revision': 0}
    research['identity_article_discovery'] = {**fence, 'search_plan': {**fence, 'payload': {
        'subject_article_bindings': [{'article_id': 'wiki:77', 'candidate_id': 'osm:way:7',
            'scope': 'Main physical building', 'binding_basis': 'Actual article describes exact observed building.',
            'physical_binding_resolved': valid}]}}}
    assert list(acquired_subject_articles(identity, research)) == ([URL] if valid else [])
    assert reviewed_reference_articles(identity) == {}


REGIONAL_URL = 'https://www.prussia39.ru/sight/index.php?sid=7'


def regional_subject_fixture(*, acquired=False):
    identity, research = subject_fixture()
    research['wikipedia'] = []
    fence = {'photo_sha256': identity['photo_sha256'], 'generation': 2, 'control_revision': 0}
    selection = {'article_id': 'prussia39:sid:7', 'candidate_id': identity['candidate_id'],
        'scope': 'The observed physical building, not its current tenant',
        'binding_basis': 'The received publisher card describes the selected footprint.',
        'physical_binding_resolved': True}
    payload = {'regional_article_selections': [selection], 'regional_catalogue': {
        'scope': dict(fence), 'results': [{'article_id': selection['article_id'],
            'canonical_url': REGIONAL_URL, 'title': 'Actual publisher card'}]}}
    if acquired:
        text = 'Описание архитектуры здания из действительно полученной статьи.'
        payload['source_text_receipt'] = {'source_photo_sha256': identity['photo_sha256'],
            'source_image_input': False, 'provider_send_state': 'not_sent', 'articles': [{
                'article_id': selection['article_id'], 'url': REGIONAL_URL, 'title': 'Actual acquired article',
                'input_kind': 'acquired_article_text', 'raw_body_sha256_verified': True,
                'source_sha256': hashlib.sha256(b'cached publisher bytes').hexdigest(),
                'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text,
                'scope': selection['scope'], 'binding_basis': selection['binding_basis'],
                'lookup_candidate_ids': [identity['candidate_id']]}]}
        # Text receipts remain useful without a text identity proof or an
        # initial catalogue nomination (e.g. a closed narrow lookup acquired it).
        payload.pop('regional_article_selections')
        payload.pop('regional_catalogue')
    research['identity_article_discovery'] = {**fence, 'sources': [],
        'search_plan': {**fence, 'payload': payload}}
    return identity, research, payload


@pytest.mark.parametrize('acquired', [False, True])
def test_geometry_keeps_bound_received_regional_page_for_normal_fact_reader(acquired):
    identity, research, payload = regional_subject_fixture(acquired=acquired)
    before = json.dumps(identity, sort_keys=True)
    lead = acquired_subject_articles(identity, research)[REGIONAL_URL]
    assert lead['subject_candidate_ids'] == [identity['candidate_id']]
    assert lead['article_id'] == 'prussia39:sid:7'
    assert lead['physical_scope'] == 'The observed physical building, not its current tenant'
    assert lead['binding_basis']
    assert lead['visual_reference_verified'] is False
    assert 'text' not in lead
    assert lead['title'] == ('Actual acquired article' if acquired else 'Actual publisher card')
    if acquired:
        article = payload['source_text_receipt']['articles'][0]
        assert lead['source_sha256'] == article['source_sha256']
        assert lead['text_sha256'] == article['text_sha256']
    assert json.dumps(identity, sort_keys=True) == before
    assert reviewed_reference_articles(identity) == {}


def test_mixed_received_regional_selections_keep_only_accepted_subject_card():
    identity, research, payload = regional_subject_fixture()
    neighbor_url = 'https://www.prussia39.ru/sight/index.php?sid=8'
    payload['regional_catalogue']['results'].append({'article_id': 'prussia39:sid:8',
        'canonical_url': neighbor_url, 'title': 'Actual neighboring building card'})
    payload['regional_article_selections'].append({**payload['regional_article_selections'][0],
        'article_id': 'prussia39:sid:8', 'candidate_id': 'osm:way:8',
        'scope': 'The neighboring physical building'})
    found = acquired_subject_articles(identity, research)
    assert list(found) == [REGIONAL_URL]
    assert found[REGIONAL_URL]['subject_candidate_ids'] == [identity['candidate_id']]
    assert neighbor_url not in found
    assert reviewed_reference_articles(identity) == {}
    # Independent validation must preserve the actual host limit too.
    payload['regional_article_selections'].append(dict(payload['regional_article_selections'][0]))
    assert acquired_subject_articles(identity, research) == {}


@pytest.mark.parametrize('change', ['unreceived', 'unresolved', 'scope', 'basis', 'neighbor',
                                  'catalogue_photo', 'catalogue_generation', 'catalogue_control'])
def test_regional_selection_cannot_bind_unreceived_unresolved_or_stale_card(change):
    identity, research, payload = regional_subject_fixture()
    selection = payload['regional_article_selections'][0]
    if change == 'unreceived':
        selection['article_id'] = 'prussia39:sid:99'
    elif change == 'unresolved':
        selection['physical_binding_resolved'] = False
    elif change == 'scope':
        selection['scope'] = ' '
    elif change == 'basis':
        selection['binding_basis'] = ''
    elif change == 'neighbor':
        selection['candidate_id'] = 'osm:way:8'
    else:
        key = change.removeprefix('catalogue_')
        payload['regional_catalogue']['scope'][{'photo': 'photo_sha256', 'control': 'control_revision'}.get(key, key)] = 'old'
    assert acquired_subject_articles(identity, research) == {}


@pytest.mark.parametrize('change', ['receipt_photo', 'body_unverified', 'body_hash', 'text_hash',
                                  'scope', 'basis', 'neighbor', 'plan_generation', 'plan_control'])
def test_geometry_cannot_reuse_unbound_unverified_or_stale_acquired_text(change):
    identity, research, payload = regional_subject_fixture(acquired=True)
    receipt = payload['source_text_receipt']
    article = receipt['articles'][0]
    if change == 'receipt_photo':
        receipt['source_photo_sha256'] = 'old'
    elif change == 'body_unverified':
        article['raw_body_sha256_verified'] = False
    elif change == 'body_hash':
        article['source_sha256'] = 'not-a-byte-hash'
    elif change == 'text_hash':
        article['text'] += ' Changed since acquisition.'
    elif change == 'scope':
        article['scope'] = ''
    elif change == 'basis':
        article['binding_basis'] = ' '
    elif change == 'neighbor':
        article['lookup_candidate_ids'] = ['osm:way:8']
    else:
        key = 'generation' if change == 'plan_generation' else 'control_revision'
        research['identity_article_discovery']['search_plan'][key] = -1
    assert acquired_subject_articles(identity, research) == {}
