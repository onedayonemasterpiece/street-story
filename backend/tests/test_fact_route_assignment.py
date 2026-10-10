"""Scheduling uses real completion/timeout receipts, never new quota probes."""
import json

import pytest

from test_verified_fact_pool import setup


def record(adapter, story, client, phase, seconds, *, role=None, directory=None, age=0):
    now = adapter.service.store.now() - age
    receipt = {'provider_id': client.provider_id, 'model_id': client.model_id,
               'phase': phase, 'elapsed_ms': seconds * 1000}
    if phase == 'aborted':
        receipt.update(abort_acknowledged=True, error_code='research_worker_timeout')
    if directory:
        receipt['isolation'] = {'directory': directory}
    with adapter.service.store.tx() as db:
        count = db.execute('SELECT COUNT(*) FROM research_provider_attempts').fetchone()[0]
        db.execute('INSERT INTO research_provider_attempts VALUES(?,?,?,?,?,?,?)',
                   (f'route-{count}', f'logical-{count}', story['id'], role or
                    ('facts' if client is adapter.client else 'facts_opencode_nemotron'),
                    json.dumps(receipt), now, now))


def routes(adapter):
    return [r for r in adapter._fact_pool_routes() if r.get('endpoint')]


def test_timeouts_do_not_win_over_useful_completions(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    record(adapter, story, adapter.client, 'aborted', 120)
    record(adapter, story, extra, 'completed', 35)
    assert adapter.order_fact_routes(routes(adapter), 0)[0]['client'] is extra


def test_acknowledged_timeout_without_stopwatch_uses_boundary_timestamps(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    record(adapter, story, adapter.client, 'aborted', 120)
    with adapter.service.store.tx() as db:
        db.execute("UPDATE research_provider_attempts SET receipt_json=json_remove(receipt_json,'$.elapsed_ms'),"
                   'created_at=updated_at-120')
    record(adapter, story, extra, 'completed', 35)
    assert adapter.order_fact_routes(routes(adapter), 0)[0]['client'] is extra


def test_outstanding_work_releases_next_unit_to_other_route(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    record(adapter, story, adapter.client, 'completed', 10)
    record(adapter, story, extra, 'completed', 20)
    for _ in range(2):
        record(adapter, story, adapter.client, 'submitted', 0)
    assert adapter.order_fact_routes(routes(adapter), 0)[0]['client'] is extra


def test_not_sent_and_other_configuration_are_not_latency_measurements(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    record(adapter, story, adapter.client, 'failed', 500)
    record(adapter, story, extra, 'completed', 1, directory='/another/product')
    record(adapter, story, extra, 'completed', 1, age=1801)
    assert adapter.order_fact_routes(routes(adapter), 0)[0]['client'] is adapter.client
    assert adapter.order_fact_routes(routes(adapter), 1)[0]['client'] is extra


def test_review_has_independent_measured_history(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    record(adapter, story, adapter.client, 'completed', 2)
    record(adapter, story, extra, 'completed', 90)
    record(adapter, story, adapter.client, 'aborted', 120, role='facts_review_' + adapter.client.model_id)
    record(adapter, story, extra, 'completed', 15, role='facts_review_' + extra.model_id)
    assert adapter.order_fact_routes(routes(adapter), 0)[0]['client'] is adapter.client
    assert adapter.order_fact_routes(routes(adapter), 0, review=True)[0]['client'] is extra


def timed_routes(adapter, extra):
    pool = routes(adapter)
    for route, elapsed in zip(pool, (91741, 34284)):
        assert route['client'] in (adapter.client, extra)
        route['review_latency_hint'] = {'operation': 'semantic_fact_review', 'phase': 'completed',
            'elapsed_ms': elapsed, 'source_sha256': 'a' * 64,
            **{k: route[k] for k in ('provider_id', 'model_id', 'endpoint')},
            'directory': route['client'].directory}
    return pool


def test_cold_review_uses_measured_qualification_not_extraction_or_model_priority(tmp_path):
    adapter, extra, _story, _ = setup(tmp_path)
    pool = timed_routes(adapter, extra)
    assert adapter.order_fact_routes(pool, review=True)[0]['client'] is extra
    assert adapter.order_fact_routes(pool, review=False)[0]['client'] is adapter.client
    # The scalar controls ordering, not a preference for a particular model.
    pool[0]['review_latency_hint']['elapsed_ms'] = 12000
    assert adapter.order_fact_routes(pool, review=True)[0]['client'] is adapter.client


def test_actual_review_receipts_override_both_cold_timings(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    pool = timed_routes(adapter, extra)
    record(adapter, story, adapter.client, 'completed', 5, role='facts_review_' + adapter.client.model_id)
    record(adapter, story, extra, 'completed', 60, role='facts_review_' + extra.model_id)
    assert adapter.order_fact_routes(pool, review=True)[0]['client'] is adapter.client


def test_acknowledged_timeout_overrides_an_optimistic_cold_hint(tmp_path):
    adapter, extra, story, _ = setup(tmp_path)
    pool = timed_routes(adapter, extra)
    pool[0]['review_latency_hint']['elapsed_ms'] = 1000
    record(adapter, story, adapter.client, 'aborted', 120, role='facts_review_' + adapter.client.model_id)
    assert adapter.order_fact_routes(pool, review=True)[0]['client'] is extra


@pytest.mark.parametrize('field,value', [('provider_id', 'other'), ('model_id', 'other'),
    ('endpoint', 'http://other'), ('directory', '/other'), ('operation', 'extract_facts'),
    ('phase', 'submitted'), ('elapsed_ms', True), ('elapsed_ms', 0), ('elapsed_ms', float('nan'))])
def test_foreign_unclosed_or_invalid_cold_hint_preserves_unmeasured_rotation(tmp_path, field, value):
    adapter, extra, _story, _ = setup(tmp_path)
    pool = timed_routes(adapter, extra)
    pool[0].pop('review_latency_hint')
    pool[1]['review_latency_hint'][field] = value
    assert adapter.order_fact_routes(pool, review=True)[0]['client'] is adapter.client
