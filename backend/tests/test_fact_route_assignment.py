"""Scheduling uses real completion/timeout receipts, never new quota probes."""
import json

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
