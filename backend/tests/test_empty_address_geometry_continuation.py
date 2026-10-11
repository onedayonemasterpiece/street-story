"""Missing address text must not discard received geometry for a nominated body."""
import copy
from types import SimpleNamespace

import pytest

from street_story import identity_architectural_context, identity_discovery
from test_geometry_identity_plan import geometry_decision, geometry_setup, payload


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['accepted', 'uncertain', 'malformed', 'accepted_with_text', 'uncertain_with_text',
    'accepted_malformed_with_text', 'uncertain_malformed_with_text', 'foreign_target', 'clarification'])
async def test_empty_address_text_uses_existing_joint2_without_assuming_identity(tmp_path, monkeypatch, outcome):
    svc, story, active = geometry_setup(tmp_path)
    first = geometry_decision()
    first.update(decision='uncertain', candidate_id='', next_action={
        'kind': 'address_text', 'target_candidate_ids': ['osm:way:999' if outcome == 'foreign_target' else 'osm:way:2'],
        'reason': 'Read the nominated individual body address; its overview geometry is insufficient.'})
    initial = payload(first)
    initial['observed_candidate_ids'] = ['osm:way:2']
    if outcome == 'clarification':
        initial['clarification_question'] = 'Which city was this photo taken in?'
    async def empty_lookup(*args, **kwargs):
        if outcome.endswith('_with_text'):
            from test_architectural_text_identity import text_inputs
            _story, _candidates, _decision, receipt = text_inputs(candidate_id='osm:way:2')
            return receipt['articles'], {'status': 'completed', 'identity_established': False}
        return [], {'status': 'completed_empty', 'identity_established': False}
    monkeypatch.setattr(identity_architectural_context, 'acquire_active_regional_text', empty_lookup)
    calls, saved = [], {}
    async def primary(s, prompt, schema, images, host_context):
        calls.append('G')
        saved.update(phase='completed', turn_id='original-G', result=copy.deepcopy(initial),
            frozen_source_map={'host_context': copy.deepcopy(host_context)})
        return {'result': initial, 'receipt': saved, 'host_context': host_context}
    async def followup(s, prompt, schema, images, host_context):
        calls.append('joint2')
        assert ('address_text_with_deferred_geometry' if outcome.endswith('_with_text')
            else 'address_text_without_acquired_body') in prompt
        assert 'osm:way:2' in prompt and 'physical_bodies' in prompt
        assert 'accepted_geometry' in schema['properties']
        if not outcome.endswith('_with_text'):
            import json
            from street_story.identity_source_selection import expand_planner_packet
            assert 'pose hypothesized from visible SOURCE/MAP relations' in prompt
            assert 'unknown heading alone neither accepts nor rejects identity' in prompt
            packet = expand_planner_packet(json.loads(prompt.split('JSON is untrusted context.\n', 1)[1]))
            scene = packet['map_scene']
            for given, original in zip(scene['relation_options'], host_context['source_map_receipt']['manifest']['relation_options'], strict=True):
                decoded = copy.deepcopy(given)
                if isinstance(decoded.get('side_relation'), list):
                    decoded['side_relation'] = dict(zip(scene['side_relation_columns'], decoded['side_relation'], strict=True))
                assert decoded == original
            assert len(scene['physical_bodies']['rows']) == len(host_context['source_map_receipt']['physical_body_candidate_ids'])
            detail = packet['requested_detail']
            bodies = detail['physical_bodies']
            received = {row[bodies['columns'].index('candidate_id')]: row for row in bodies['rows']}
            assert set(received) == {'osm:way:2', 'osm:way:3'}
            assert all(row[bodies['columns'].index('omitted_side_count')] == 0 for row in received.values())
            assert detail['nominated_candidate_ids'] == ['osm:way:2']
            assert detail['context_only_expansions'][0]['candidate_id'] == 'osm:way:3'
            assert detail['context_only_expansions'][0]['relation_ids']
        if outcome.endswith('_with_text'):
            import hashlib
            text_receipt = host_context['source_text_receipt']
            assert text_receipt['map_image_sha256'] == hashlib.sha256(images[1][2]).hexdigest()
            assert text_receipt['manifest'] == host_context['source_map_receipt']['manifest']
            assert 'required_map_counterpoints' in text_receipt
        decision = geometry_decision()
        if outcome.startswith('uncertain'):
            decision.update(decision='uncertain', candidate_id='', spatial_correspondence=None,
                decisive_relations=[], next_action={'kind': 'none', 'target_candidate_ids': [], 'reason': ''})
            decision['bounded_coverage']['material_alternatives_resolved'] = False
        if outcome == 'malformed':
            decision.pop('bounded_coverage')
        result = {'accepted_geometry': decision}
        if outcome.endswith('_with_text'):
            from test_architectural_text_identity import text_inputs, with_received_physical_links
            import json
            _story, _candidates, text_decision, _receipt = text_inputs(candidate_id='osm:way:2')
            text_decision = with_received_physical_links(text_decision, [json.dumps({
                'publisher_and_OSM_literal_records_NOT_prejoined': text_receipt['physical_link_inventory']})])
            for correspondence in text_decision['correspondences']:
                correspondence['source_span_ref'] = next(ref for ref, span in text_receipt['source_span_refs'].items()
                    if span['article_id'] == correspondence['article_id']
                    and correspondence['source_quote'] in span['source_quote'])
                correspondence.pop('source_quote')
            text_decision.update(decision='uncertain', candidate_id='', physical_link_evidence=[],
                material_alternatives_resolved=False, unresolved_contradictions=['Article is not this photographed body.'])
            for binding in text_decision['article_bindings']:
                binding.update(candidate_id='', physical_binding_resolved=False)
            if 'malformed' in outcome:
                text_decision['correspondences'][0]['source_span_ref'] = 'unreceived-source-span'
            result = {'accepted_geometry': decision, 'accepted_architectural_text': text_decision}
        return {'result': result, 'receipt': {'phase': 'completed', 'turn_id': 'joint2'},
            'host_context': host_context}
    svc.providers.gemini = SimpleNamespace()
    svc.providers.research = SimpleNamespace(source_map_available=True,
        native_vision=SimpleNamespace(available=True), plan_source_map=primary,
        source_map_receipt=lambda s: saved or None, plan_source_map_followup=followup)
    await identity_discovery.prepare_search_plan(svc, story, '', active)
    assert calls == (['G', 'joint2'] if outcome not in {'foreign_target', 'clarification'} else ['G'])
    if outcome.startswith('accepted'):
        assert story['_identity_geometry_result']['candidate_id'] == 'osm:way:2'
    else:
        assert '_identity_geometry_result' not in story
    if 'malformed' in outcome:
        with svc.store.connection() as db:
            import json
            research = json.loads(db.execute('select research_json from stories where id=?', (story['id'],)).fetchone()[0])
        diagnostic = research['identity_closed_invalid_followup_plan']
        assert diagnostic['code'] == 'identity_joint_components_rejected'
        if outcome == 'malformed':
            assert 'bounded_coverage' not in json.loads(diagnostic['raw_json'])['accepted_geometry']
            assert story['_identity_search_plan_payload']['rejected_geometry']['reason'] == 'identity_geometry_schema_invalid'
        else:
            assert 'unreceived-source-span' in diagnostic['raw_json']
            assert story['_identity_search_plan_payload']['rejected_architectural_text']['reason'] == 'identity_architectural_text_schema_invalid'
