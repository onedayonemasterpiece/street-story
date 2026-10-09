"""One offline matrix over existing real adapters/workers and their fences.

Reuse the focused scenarios rather than another fake reliability manager.
Each driver asserts durable progress, original-operation preservation and the
relevant negative scope. No scenario uses production credentials or inference.
"""
from importlib import import_module

import pytest


SCENARIOS = [
    ('reverse_503_overpass_healthy', 'test_osm_visibility_candidates',
     'test_reverse_failure_does_not_block_objects_and_recovers_without_sticky_cache', {'failure': '503'}),
    ('reference_http_text_healthy', 'test_research_adapter_fence',
     'test_reference_download_failure_does_not_block_independent_fact_model', {}),
    ('gallery_other_article_ready', 'test_independent_article_priority',
     'test_independent_news_materializes_before_remaining_wiki_gallery_but_ready_initial_ref_is_immediate', {'units': 2}),
    ('google_unknown_independent_routes_restart', 'test_identity_initial_fence',
     'test_unknown_routes_do_not_block_independent_text_or_replay_after_restart', {}),
    ('text_unknown_independent_ref_plan_restart', 'test_closed_initial_plan_reuse',
     'test_unknown_followup_preserves_independent_initial_ref_plan_across_restart', {}),
    ('review_pending_selection_draft_restart', 'test_headless_fact_review_parallel',
     'test_original_readback_allows_selection_draft_and_restart_before_last_review', {}),
    ('saved_review_original_readback', 'test_headless_fact_review_parallel',
     'test_restart_observes_exact_addressed_review_without_replacing_its_packet', {'case': 'saved_contract'}),
    ('shared_quota_blocks_other_units', 'test_research_wait_backoff',
     'test_provider429_wait_still_blocks_new_independent_units', {}),
    ('stop_resume_blocks_old_worker', 'test_research_adapter_fence',
     'test_admission_wait_cannot_send_old_worker_after_stop_resume', {}),
    ('changed_source_blocks_readback_commit', 'test_research_adapter_fence',
     'test_spatial_native_reads_original_turn_without_fresh_availability_and_fences_source_scope', {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario,module,name,arguments', SCENARIOS, ids=[row[0] for row in SCENARIOS])
async def test_existing_product_failure_scopes(tmp_path, monkeypatch, scenario, module, name, arguments):
    import inspect
    driver = getattr(import_module(module), name)
    parameters = inspect.signature(driver).parameters
    supplied = {'tmp_path': tmp_path, **arguments}
    if 'monkeypatch' in parameters:
        supplied['monkeypatch'] = monkeypatch
    await driver(**supplied)
