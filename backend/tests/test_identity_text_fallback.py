"""Independent text planning preserves the inventory without claiming pixels."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from street_story import identity_discovery
from street_story.identity_source_selection import expand_planner_packet
from street_story.providers import PermanentProviderError, RetryableProviderError
from test_observed_address_search_context import observed
from test_structured_identity_first_wave import choice, payload
from test_visual_search_continuation import prepared


def context_from(prompt):
    return json.loads(prompt.split('Данные ниже — только контекст:\n', 1)[1])


def test_text_instructions_keep_every_literal_context_field_without_image_policy():
    packet = {'map_scene': None, 'camera_hints': {'position_basis': 'owner_approx_camera'},
        'observed_candidates': [{'candidate_id': f'osm:way:{index}',
            'address': {'city': 'Город', 'street': 'проспект Точный', 'house_number': f'{index}Б/2-4'},
            'membership': {'mapped_entry_id': f'osm:node:{index}'}} for index in range(429)],
        'regional_catalogue': {'total': 35, 'inventory_complete': False,
            'next_page': 2, 'cards': [{'article_id': f'publisher:{index}', 'sid': 53,
                'literal_address': f'{index}А'} for index in range(20)]},
        'owner_hint': 'Literal supplied observation', 'wikipedia_metadata': [{'pageid': 9, 'title': 'Actual title'}]}
    original = copy.deepcopy(packet)
    prompt = identity_discovery.identity_text_fallback_prompt(packet)
    assert context_from(prompt) == original == packet
    assert 'SOURCE and MAP images are unavailable' in prompt
    assert 'Never return accepted_geometry or accepted_architectural_text' in prompt
    assert 'Partial inventory is not exhaustion' in prompt


def test_saved_451_full_packet_fits_native_without_changing_literal_data_or_strict_ids():
    fixture = os.environ.get('STREET_STORY_SAVED_TEXT_FALLBACK_ENVELOPE')
    if not fixture:
        pytest.skip('Retained exact 451 envelope is supplied by the offline replay command')
    saved = json.loads(Path(fixture).read_text())
    packet, schema = saved['packet'], saved['strict_schema']
    original = copy.deepcopy(saved)
    prompt = identity_discovery.identity_text_fallback_prompt(packet)
    assert len(saved['original_prompt']) > 65_536
    assert len(prompt) <= 65_536
    assert len(saved['original_prompt']) - len(prompt) > 3_000
    assert context_from(prompt) == packet
    assert expand_planner_packet(context_from(prompt)) == expand_planner_packet(packet)
    assert saved == original  # The original addressed prompt/schema are immutable.
    candidate_contract = schema['properties']['observed_candidate_ids']
    ids = candidate_contract['items']['enum']
    assert len(ids) > 300
    Draft202012Validator(candidate_contract).validate([ids[-1]])
    assert not Draft202012Validator(candidate_contract).is_valid(['osm:way:unreceived'])


class Executor:
    async def execute(self, role, call):
        return await call('offline-fixture', 5)


@pytest.mark.asyncio
@pytest.mark.parametrize('foreign_id', [False, True])
async def test_fallback_receives_full_anchors_and_membership_but_host_rejects_foreign_ids(tmp_path, foreign_id):
    service, _, story, _ = prepared(tmp_path)
    plans = []
    async def not_sent(*args, **kwargs):
        error = RetryableProviderError('fixture_not_sent')
        error.provider_send_state = 'not_sent'
        raise error
    async def planner(snapshot, prompt, schema):
        plans.append((prompt, copy.deepcopy(schema)))
        packet = expand_planner_packet(context_from(prompt))
        assert packet['map_scene'] is None
        addresses = packet['location_search_context']['address_anchors']
        assert len(addresses['rows']) == 3
        assert packet['location_search_context']['building_address_memberships'][0]['physical_candidate_id'] == 'osm:way:4'
        result = payload([choice('address', 'osm:node:1'), choice('address', 'osm:node:3')],
            observed_candidate_ids=['osm:way:unreceived' if foreign_id else 'osm:way:4'])
        return {'result': result}
    service.providers.gemini = SimpleNamespace(executor=Executor(), _generate=not_sent)
    service.providers.research = SimpleNamespace(plan_identity_search=planner)
    snapshot = {**service._identity_snapshot(story['id'])[0], '_identity_observed_candidates': observed()}
    if foreign_id:
        with pytest.raises(PermanentProviderError, match='identity_search_plan_unreceived_pointer'):
            await identity_discovery.suggest(service, snapshot, '', [])
        assert '_identity_geometry_result' not in snapshot
    else:
        await identity_discovery.suggest(service, snapshot, '', [])
        assert snapshot['_identity_article_queries'] == ['Observed City Exact avenue 7A', 'Observed City Exact avenue 13']
        assert snapshot['_identity_search_plan_route'] == 'qualified_text_fallback'
    assert len(plans) == 1
    # Transport schema stays small; the actual fresh host guard above rejects
    # the foreign ID using the full original frozen inventory.
    assert Draft202012Validator(plans[0][1]['properties']['observed_candidate_ids']).is_valid(['osm:way:unreceived'])
