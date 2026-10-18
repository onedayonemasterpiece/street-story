"""Actual one-pass prompt/date wiring; controlled transports do not prove model cure."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from street_story.errors import RetryableProviderError
from street_story.headless_facts import HeadlessFacts
from street_story.opencode_research import OpenCodeResearch, ResearchLimits, ResearchUnavailable
from street_story.research_adapter import FACT_PAGE_SCHEMA, fact_page_capsule
from street_story.source_assessment import VALUE_CONTRACT, response_schema
from test_headless_fact_pool import RUN, fixture, result
from test_opencode_research import Harness


def retained_source():
    return json.loads((Path(__file__).parent / 'fixtures' / 'temporal_source_102.json').read_text())


def source_capsule(review_date):
    retained = retained_source()
    text = retained['source_text']
    assert hashlib.sha256(text.encode()).hexdigest() == retained['provenance']['normalized_text_sha256']
    page = {'source_version_id': retained['provenance']['source_version_id'],
            'source_url': retained['source_url'], 'core_text': text,
            'evidence_passages': [{'passage_id': 0, 'text': text}]}
    context = {'review_as_of_date_utc': review_date,
               'confirmed_identity': {'status': 'owner_confirmed', 'selection_basis': 'owner_exact',
                   'candidate_id': 'osm:way:133035113', 'candidate_name': 'Житомирская, 22–24',
                   'generation': 1, 'owner_selected': True, 'physical_identity_accepted': True},
               'known_facts': retained['known_unqualified_mutable_claims'],
               'coverage_goal': 'History and architecture of the selected building'}
    original = copy.deepcopy(context)
    capsule = fact_page_capsule(page, context)
    assert context == original
    return {key: value for key, value in capsule.items() if key != '_known_fact_inventory'}


@pytest.mark.asyncio
async def test_actual_v3_reader_receives_retained_temporal_negatives_and_source_date():
    capsule = source_capsule('2026-10-13')
    wire = {}
    client = object.__new__(OpenCodeResearch)

    async def capture(role, prompt, binding, schema):
        wire.update(role=role, prompt=prompt, binding=binding, schema=schema)
        return wire

    client._run = capture
    schema = response_schema(FACT_PAGE_SCHEMA, VALUE_CONTRACT)
    await client.extract_facts(capsule, {'request_id': 'temporal-wire', 'semantic_contract': VALUE_CONTRACT}, schema)
    instruction, encoded = wire['prompt'].split('Capsule:\n', 1)
    content = json.loads(encoded)
    assert content['review_as_of_date_utc'] == content['context']['review_as_of_date_utc'] == '2026-10-13'
    assert content['sources'][0]['text'] == retained_source()['source_text']
    assert '25.06.2025' in content['sources'][0]['text']
    assert all(claim in content['known_fact_inventory'] for claim in retained_source()['known_unqualified_mutable_claims'])
    # Neither the regression's expected useful facts nor negative verdicts are
    # injected as editorial answers; the reader receives the original source.
    for retained in retained_source()['historical_claims']:
        assert retained['text'] not in instruction
    assert 'Дом восстановили в конце 1950-х.' in content['sources'][0]['text']
    assert 'Завершить ремонт планируют в сентябре этого года.' in content['sources'][0]['text']
    assert 'date explicitly in fact.text' in instruction
    assert 'Resolve relative dates' in instruction and 'source publication date' in instruction
    assert 'unchanged text and ID as nonpositive' in instruction
    assert 'Keep supported historical states as dated facts' in instruction
    assert 'including approximate dates' in instruction
    assert wire['schema'] == schema
    assert 'review_as_of_date_utc' not in schema['properties']
    assert wire['binding']['semantic_contract'] == VALUE_CONTRACT


@pytest.mark.asyncio
async def test_headless_review_date_uses_frozen_run_start_across_midnight(tmp_path, monkeypatch):
    service, job = fixture(tmp_path, count=1)
    run_start = datetime(2026, 10, 12, 23, 59, tzinfo=timezone.utc).timestamp()
    later = datetime(2026, 10, 13, 5, 0, tzinfo=timezone.utc).timestamp()
    with service.store.tx() as db:
        db.execute('UPDATE research_runs SET created_at=? WHERE run_id=?', (run_start, RUN))
    monkeypatch.setattr(service.store, 'now', lambda: later)
    contexts = []

    async def extract(page, story, context):
        contexts.append(context)
        return result(page)

    service.providers.research = SimpleNamespace(client=SimpleNamespace(model_id='controlled-model'),
                                                extract_fact_page=extract)
    try:
        try:
            await HeadlessFacts(service).run(job, RUN, 'History', 'history')
        except RetryableProviderError:
            pass  # Ordinary unreviewed candidates remain pending.
        assert contexts and all(c['review_as_of_date_utc'] == '2026-10-12' for c in contexts)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unknown_temporal_reader_keeps_exact_original_prompt_after_date_changes():
    harness = Harness()
    harness.timeout = True
    adapter = harness.adapter(limits=ResearchLimits(timeout_seconds=.01, poll_seconds=.001))
    binding = {'request_id': 'original-temporal-unit', 'semantic_contract': VALUE_CONTRACT}
    schema = response_schema(FACT_PAGE_SCHEMA, VALUE_CONTRACT)
    try:
        with pytest.raises(ResearchUnavailable) as pending:
            await adapter.extract_facts(source_capsule('2026-10-12'), binding, schema)
        original = pending.value.receipt
        harness.timeout = False
        harness.result = {'facts': [], 'source_matches_poi': True, 'source_content_valid': True,
                          'continuation_needed': False}
        resumed_binding = {**binding, **original['binding'], **{key: original[key] for key in
                           ('session_id', 'message_id', 'phase', 'frozen_prompt', 'frozen_schema')}}
        recovered = await adapter.extract_facts(source_capsule('2026-10-13'), resumed_binding, schema)
        assert recovered['receipt']['readback_only'] is True
        assert recovered['receipt']['frozen_prompt'] == original['frozen_prompt']
        assert recovered['receipt']['session_id'] == original['session_id']
        assert recovered['receipt']['message_id'] == original['message_id']
        assert len(harness.sends) == len(harness.admissions) == 1
    finally:
        await harness.client.aclose()
