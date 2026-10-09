import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
import sys

import pytest

from street_story.poi_memory import _review_snapshot
from street_story.service import canonical
from test_visual_search_continuation import prepared


TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location('product_recovery_acceptance', TOOLS / 'product_recovery_acceptance.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def item():
    return {'message_id': 102, 'sha256': hashlib.sha256(b'original-source').hexdigest(),
        'mime': 'image/jpeg', 'min_useful_facts': 1, 'expected_physical_id': 'osm:way:building',
        'report_label': 'Hidden building expectation', 'address': 'Not a provider seed'}


def test_real_acceptance_storage_guard_rejects_unmanaged_external_paths():
    with pytest.raises(ValueError, match='under /home/dev/artifacts'):
        harness.managed(Path('/outside-artifact-root/source.jpg'))


def test_requested_canaries_keep_simple_then_complex_order_and_reject_duplicate_spend():
    items = [{'message_id': identifier} for identifier in (102, 104, 111, 122, 132)]
    assert [entry['message_id'] for entry in harness.selected_items(items, '104,102,111,122,132')] == [
        104, 102, 111, 122, 132]
    for requested in ('104,104', '104,999'):
        with pytest.raises(ValueError, match='distinct and present'):
            harness.selected_items(items, requested)


def test_manifest_digest_freezes_expectations_but_is_independent_of_json_format(tmp_path, monkeypatch):
    # Exercise parsing using pytest-owned fixtures on hosted CI; the real CLI
    # storage guard has its own negative control below and stays unchanged.
    monkeypatch.setattr(harness, 'managed', lambda path: path.resolve())
    source = tmp_path / 'source.jpg'
    source.write_bytes(b'original-source')
    entry = {**item(), 'path': 'source.jpg'}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'items': [entry]}))
    normalized, original_digest = harness.load_manifest(manifest)
    assert normalized[0]['path'] == str(source)
    manifest.write_text(json.dumps({'items': [entry]}, indent=4, sort_keys=True))
    assert harness.load_manifest(manifest)[1] == original_digest
    entry['min_useful_facts'] = 2
    manifest.write_text(json.dumps({'items': [entry]}))
    assert harness.load_manifest(manifest)[1] != original_digest
    manifest.write_text(json.dumps({'items': [entry, entry]}))
    with pytest.raises(ValueError, match='Duplicate'):
        harness.load_manifest(manifest)


def test_upload_preserves_source_sha_and_exif_without_hidden_expectations():
    class Service:
        def create_story(self, **kwargs):
            return kwargs
    result = harness.upload_story(Service(), item(), b'original-source', 'frozen-manifest')
    assert result['photo_sha256'] == hashlib.sha256(result['photo_bytes']).hexdigest()
    assert result['lat'] is None and result['lon'] is None
    assert set(result) == {'key', 'client_story_id', 'photo_sha256', 'photo_mime_type',
        'photo_bytes', 'voice_protocol', 'lat', 'lon'}
    assert 'Hidden' not in str(result) and 'provider seed' not in str(result)


def test_explicit_owner_approx_camera_is_input_but_truth_and_geometric_result_are_not():
    class Service:
        def create_story(self, **kwargs):
            return kwargs
    entry = {**item(), 'owner_approx_camera': {'latitude': 55.074106, 'longitude': 21.903648},
        'acceptance_mode': 'geometry_without_reference'}
    result = harness.upload_story(Service(), entry, b'original-source', 'frozen-manifest')
    assert (result['lat'], result['lon']) == (55.074106, 21.903648)
    assert result['location_provenance'] == {'kind': 'owner_approx_camera'}
    assert 'expected_physical_id' not in result and 'report_label' not in result
    assert 'acceptance_mode' not in result and 'Not a provider seed' not in str(result)


@pytest.mark.asyncio
async def test_directed_spatial_mode_blocks_reference_dispatch_only_for_selected_source():
    from street_story.errors import PermanentProviderError
    class Service:
        async def _identify_photo(self, story, *args, **kwargs):
            return 'ordinary-reference-path'
        async def _identify_photo_batch(self, story, *args, **kwargs):
            return 'ordinary-reference-batch'
    service = Service()
    restore = harness.block_directed_reference_inference(service, [{**item(),
        'acceptance_mode': 'geometry_without_reference'}])
    try:
        with pytest.raises(PermanentProviderError, match='external_reference_disabled'):
            await service._identify_photo({'photo_sha256': item()['sha256']}, '', [])
        assert await service._identify_photo({'photo_sha256': 'other'}, '', []) == 'ordinary-reference-path'
    finally:
        restore()
    assert await service._identify_photo({'photo_sha256': item()['sha256']}, '', []) == 'ordinary-reference-path'


def test_availability_transfer_is_scoped_negative_history_with_original_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'managed', lambda path: path.resolve())
    source = tmp_path/'prior'
    (source/'data').mkdir(parents=True)
    frozen = {'environment_sha256': {'provider': 'same'}, 'qualification_sha256': 'same',
              'public_settings': {'gemini_model': 'fixture', 'data_dir': 'new'}}
    prior = {**frozen, 'source_sha': 'a'*40,
             'public_settings': {'gemini_model': 'fixture', 'data_dir': 'old'}}
    (source/'run.json').write_text(json.dumps(prior))
    key_id = 'b'*64
    with sqlite3.connect(source/'data/street-story.sqlite3') as db:
        db.execute('CREATE TABLE gemini_key_health(key_id,model,operation,cooldown_until,'
                   'consecutive_failures,last_failure)')
        db.execute('INSERT INTO gemini_key_health VALUES(?,?,?,?,?,?)',
            (key_id, 'fixture', 'web_search', 10, 2,
             json.dumps({'category': 'quota_exhausted', 'code': 429, 'private': 'discard-this'})))
        db.execute('INSERT INTO gemini_key_health VALUES(?,?,?,?,?,?)',
            (key_id, 'fixture', 'grounded_research', 99, 3, None))
        db.execute('CREATE TABLE stories(private_original)')
        db.execute("INSERT INTO stories VALUES('never-transfer-object-material')")
    snapshots = harness.availability_history([source], frozen)
    assert len(snapshots[0]['rows']) == 1
    assert 'discard-this' not in json.dumps(snapshots)
    assert 'never-transfer-object-material' not in json.dumps(snapshots)
    svc, _, _, _ = prepared(tmp_path/'current')
    with svc.store.tx() as db:
        db.execute('INSERT INTO gemini_credentials(key_id,busy_until,disabled) VALUES(?,55,1)', (key_id,))
        for operation in ('web_search', 'grounded_research'):
            db.execute('INSERT INTO gemini_key_health(key_id,model,operation) VALUES(?,?,?)',
                       (key_id, 'fixture', operation))
    harness.apply_availability_history(svc.store, snapshots)
    with svc.store.connection() as db:
        rows = {row['operation']: dict(row) for row in db.execute('SELECT * FROM gemini_key_health')}
        credential = dict(db.execute('SELECT * FROM gemini_credentials').fetchone())
    assert rows['web_search']['consecutive_failures'] == 2
    assert rows['web_search']['quota_state'] == 'quota_exhausted'
    assert rows['web_search']['cooldown_until'] == 10 < svc.store.now()
    assert rows['grounded_research']['consecutive_failures'] == rows['grounded_research']['cooldown_until'] == 0
    assert credential['busy_until'] == 55 and credential['disabled'] == 1
    prior['environment_sha256'] = {'provider': 'different'}
    (source/'run.json').write_text(json.dumps(prior))
    with pytest.raises(ValueError, match='different configuration'):
        harness.availability_history([source], frozen)


def test_old_429_history_uses_original_qualification_while_derived_caches_are_frozen(tmp_path, monkeypatch):
    from test_fact_semantic_qualification import installer, persist_timed_proof, timed_proof
    module = installer()
    caches, evidence, text, route, path, report = timed_proof(tmp_path, module)
    persist_timed_proof(evidence, route, path, report)
    qualification = {'caches': {**caches, 'research-text-verification-v1': text}, 'evidence': evidence}
    original = json.loads(json.dumps(qualification))
    raw_digest = harness.digest(qualification)
    runtime = harness.runtime_qualification_caches(qualification, module)
    assert qualification == original
    assert harness.digest(qualification) == raw_digest
    assert harness.digest(runtime) != harness.digest(qualification['caches'])
    runtime_hint = runtime['fact-semantic-verification-v1']['routes'][0]['qualification_review_timing']
    assert runtime_hint['elapsed_ms'] == 91741
    frozen = {'qualification_sha256': raw_digest,
        'runtime_qualification_caches_sha256': harness.digest(runtime),
        'environment_sha256': {'provider': 'same-credential-config'},
        'public_settings': {'gemini_model': 'same-model'}}
    prior = {k: v for k, v in frozen.items() if k != 'runtime_qualification_caches_sha256'}
    prior['source_sha'] = 'a' * 40  # Older source has no derived-runtime field.
    monkeypatch.setattr(harness, 'managed', lambda path: path.resolve())
    source = tmp_path / 'old-actual-quota-boundary'
    (source / 'data').mkdir(parents=True)
    (source / 'run.json').write_text(json.dumps(prior))
    with sqlite3.connect(source / 'data/street-story.sqlite3') as db:
        db.execute('CREATE TABLE gemini_key_health(key_id,model,operation,cooldown_until,consecutive_failures,last_failure)')
        db.execute('INSERT INTO gemini_key_health VALUES(?,?,?,?,?,?)',
            ('b' * 64, 'same-model', 'web_search', 2000, 2,
             json.dumps({'category': 'rate_limited', 'code': 429})))
    snapshots = harness.availability_history([source], frozen)
    assert snapshots[0]['rows'][0]['cooldown_until'] == 2000
    assert snapshots[0]['rows'][0]['operation'] == 'web_search'
    assert json.loads(snapshots[0]['rows'][0]['last_failure']) == {'category': 'rate_limited', 'code': 429}
    runtime_hint['elapsed_ms'] += 1
    assert harness.digest(runtime) != frozen['runtime_qualification_caches_sha256']
    assert harness.digest(qualification) == raw_digest
    for changed in ({'qualification_sha256': 'other-proof'},
                    {'environment_sha256': {'provider': 'other-credentials'}},
                    {'public_settings': {'gemini_model': 'other-model'}}):
        with pytest.raises(ValueError, match='different'):
            harness.availability_history([source], {**frozen, **changed})


def attempt(identifier, receipt):
    return {'attempt_id': identifier, 'logical_id': identifier, 'role': 'search',
        'receipt_json': json.dumps(receipt), 'created_at': 1, 'updated_at': 2}


def test_receipts_count_original_sends_instead_of_attempts_or_readbacks():
    rows = [attempt('first', {'phase': 'response_completed', 'assistants': [{'message_id': 'original-assistant'}]}),
        attempt('same-readback', {'phase': 'response_completed', 'assistants': [{'message_id': 'original-assistant'}]}),
        attempt('never-dispatched', {'phase': 'failed', 'provider_send_state': 'not_sent'}),
        attempt('unknown', {'phase': 'unknown', 'provider_send_state': 'possibly_sent'}),
        attempt('google', {'phase': 'completed', 'model_attempts': [{'provider_send_state': 'response_closed'},
            {'provider_send_state': 'not_sent'}]})]
    result = harness.summarize_receipts(rows)
    assert result['attempts'] == 5 and result['actual_sends'] == 2
    assert result['unknown'] == 1 and result['not_sent'] == 1
    assert result['actual_sends_is_lower_bound'] is True
    assert result['money'].startswith('unknown')


@pytest.mark.asyncio
async def test_sdk_journal_counts_unreceipted_calls_and_preserves_unknown_usage(tmp_path):
    from types import SimpleNamespace
    class Client:
        settings = SimpleNamespace(gemini_model='configured-model')
        async def _provider_request(self, key, timeout, contents, config=None, *, model=None):
            if contents == ['fail']:
                raise TimeoutError()
            return SimpleNamespace(response_id='response-id', usage_metadata=SimpleNamespace(
                prompt_token_count=42, candidates_token_count=7, total_token_count=49))
    original = Client._provider_request
    journal = tmp_path/'sdk-calls.jsonl'
    restore = harness.instrument_google_sdk(Client, journal)
    try:
        client = Client()
        await client._provider_request('secret-never-recorded', 10, ['private-never-recorded'])
        with pytest.raises(TimeoutError):
            await client._provider_request('secret-never-recorded', 10, ['fail'])
        measured = harness.summarize_sdk_journal(journal)
        assert measured['google_sdk_invocations'] == 2
        assert measured['response_closed'] == measured['outcome_unavailable'] == 1
        assert measured['details'][0]['usage']['total_tokens'] == 49
        assert measured['details'][0]['key_fingerprint'] == hashlib.sha256(
            b'secret-never-recorded').hexdigest()
        assert measured['details'][1]['usage']['total_tokens'] == 'unknown'
        assert measured['money'].startswith('unknown')
        assert 'secret-never-recorded' not in journal.read_text()
        assert 'private-never-recorded' not in journal.read_text()
        assert 'no inferred per-story' in measured['scope']
    finally:
        restore()
    assert Client._provider_request is original


def readback_fixture(tmp_path):
    svc, _adapter, story, _sessions = prepared(tmp_path)
    now = [100.0]
    svc.store.now = lambda: now[0]
    identity = {'status': 'match', 'candidate_id': 'osm:way:building',
        'visual_reference_verified': True, 'resolved_at': 200.0}
    sources = canonical([{'url': 'https://official.example/history'}])
    text = 'A model reviewed building history claim.'
    proof = {'detector': 'backend_semantic_review', 'scan_id': 1, 'source_story_id': story['id'],
        'snapshot': list(_review_snapshot(text, sources))}
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET created_at=100,state=\'identity_ready\',research_json=? WHERE id=?',
            (canonical({'visual_identity': identity}), story['id']))
        db.execute('INSERT INTO facts(story_id,fact_id,text,confidence,evidence_supported,selected,sources_json) '
            'VALUES(?,?,?,.9,1,0,?)', (story['id'], 'claim', text, sources))
        db.execute('INSERT INTO fact_assertions(story_id,assertion_id,display_text,revision_digest,'
            'review_status,eligibility,created_at,updated_at) VALUES(?,?,?,? ,\'eligible\',\'eligible\',100,390)',
            (story['id'], 'claim', text, 'reviewed-revision'))
        db.execute('INSERT INTO poi_research_assertions(poi_key,assertion_id,semantic_key,text,confidence,'
            'sources_json,created_at,updated_at,review_status,eligibility,review_proof_json) '
            'VALUES(?,?,?, ?, .9,?,100,390,\'eligible\',\'eligible\',?)',
            ('osm:way:building', 'claim', 'claim', text, sources, canonical(proof)))
        db.execute('INSERT INTO jobs(id,story_id,kind,semantic_key,payload_json,state,attempts,available_at,'
            'created_at,updated_at) VALUES(?,?,\'identity_visual\',?,\'{}\',\'done\',1,100,100,200)',
            ('identity', story['id'], 'identity-key'))
        db.execute('INSERT INTO jobs(id,story_id,kind,semantic_key,payload_json,state,attempts,available_at,'
            'created_at,updated_at) VALUES(?,?,\'research\',?,\'{}\',\'running\',1,200,200,390)',
            ('facts', story['id'], 'facts-key'))
    case = {**harness.blank_result(item()), 'story_id': story['id']}
    return svc, now, case


def test_first_fact_does_not_end_wait_and_caps_use_original_upload(tmp_path):
    svc, now, case = readback_fixture(tmp_path)
    now[0] = 390
    result = harness.read_case(svc, case, item())
    assert result['eligible_proved_count'] == 1
    assert result['first_eligible_elapsed_s'] == 290
    assert result['identity_elapsed_s'] == 100
    assert not result['terminal'] and result['status'] == 'RUNNING'
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, case['story_id'])['research_json'])
        research['automatic_research_outcome'] = {'outcome': 'useful_partial', 'finished_at': 581}
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), case['story_id']))
        db.execute('UPDATE jobs SET state=\'done\' WHERE id=\'facts\'')
    now[0] = 581
    result = harness.read_case(svc, case, item())
    assert result['terminal'] and result['total_elapsed_s'] == 481
    assert result['gates']['terminal_in_480s'] is False and result['status'] == 'FAIL'
    assert result['first_eligible_elapsed_s'] == 290


def test_wrong_snapshot_is_not_canonical_review_proof(tmp_path):
    svc, now, case = readback_fixture(tmp_path)
    now[0] = 390
    with svc.store.tx() as db:
        db.execute("UPDATE poi_research_assertions SET review_proof_json=?", (canonical({
            'detector': 'backend_semantic_review', 'scan_id': 1, 'source_story_id': case['story_id'],
            'snapshot': ['Changed claim', 'different evidence']}),))
    result = harness.read_case(svc, case, item())
    assert result['eligible_count'] == 1 and result['eligible_proved_count'] == 0
    assert result['gates']['canonical_poi_readback'] is False


def test_report_only_equivalent_identifiers_do_not_accept_neighbor_or_complex(tmp_path):
    svc, now, case = readback_fixture(tmp_path)
    now[0] = 390
    entry = {**item(), 'expected_physical_id': 'wiki:exact-building',
        'expected_physical_ids': ['wiki:exact-building', 'osm:way:building']}
    assert harness.read_case(svc, case, entry)['gates']['correct_physical_object'] is True
    entry['expected_physical_ids'] = ['wiki:neighbor', 'osm:relation:multi-building-complex']
    assert harness.read_case(svc, case, entry)['gates']['correct_physical_object'] is False
    assert 'expected_physical_ids' not in harness.upload_story(
        type('Service', (), {'create_story': lambda self, **kwargs: kwargs})(),
        entry, b'original-source', 'frozen-manifest')


def test_between_identity_and_facts_is_not_terminal(tmp_path):
    svc, now, case = readback_fixture(tmp_path)
    now[0] = 210
    with svc.store.tx() as db:
        db.execute("DELETE FROM jobs WHERE id='facts'")
    assert harness.read_case(svc, case, item())['terminal'] is False


def test_operator_stop_after_identity_ends_harness_without_acceptance_or_deadline_rewrite(tmp_path):
    from street_story.research_control import stop_research
    svc, now, case = readback_fixture(tmp_path)
    now[0] = 390
    stop_research(svc, case['story_id'], purpose='facts')
    result = harness.read_case(svc, case, item())
    assert result['terminal'] and result['operator_stopped']
    assert result['total_elapsed_s'] == 290
    assert result['eligible_proved_count'] == 1
    assert result['status'] == 'OPERATOR_STOPPED'
    assert result['gates']['natural_product_terminal'] is False
    now[0] = 600
    harness.apply_hard_cap(svc, case['story_id'])
    assert harness.read_case(svc, case, item())['product_outcome'] is None
    assert harness.read_case(svc, case, item())['total_elapsed_s'] == 290


@pytest.mark.parametrize('field,value', [('photo_sha256', 'other-photo'), ('identity_generation', 77)])
def test_stale_operator_stop_does_not_terminate_current_harness(tmp_path, field, value):
    from street_story.research_control import stop_research
    svc, now, case = readback_fixture(tmp_path)
    now[0] = 390
    stop_research(svc, case['story_id'], purpose='facts')
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, case['story_id'])['research_json'])
        research['research_controls']['facts'][field] = value
        db.execute('UPDATE stories SET research_json=? WHERE id=?', (canonical(research), case['story_id']))
    result = harness.read_case(svc, case, item())
    assert not result['terminal'] and not result['operator_stopped']
    assert result['status'] == 'RUNNING'


def test_harness_forced_completion_cannot_pass_as_product_terminal(tmp_path):
    svc, now, case = readback_fixture(tmp_path)
    now[0] = 400
    with svc.store.tx() as db:
        research = json.loads(svc._story_row(db, case['story_id'])['research_json'])
        research['automatic_research_outcome'] = {'outcome': 'useful_partial',
            'finished_at': 400, 'reason': 'acceptance_upload_deadline_exceeded'}
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
            (canonical(research), case['story_id']))
        db.execute("UPDATE jobs SET state='done' WHERE id='facts'")
    result = harness.read_case(svc, case, item())
    assert result['terminal'] and result['total_elapsed_s'] == 300
    assert result['gates']['natural_product_terminal'] is False
    assert result['status'] == 'FAIL'


def test_resource_block_and_missing_hidden_expected_id_are_distinct_results():
    assert harness.acceptance_status({'terminal': True,
        'product_outcome': {'outcome': 'resource_blocked'}, 'gates': {'identity': False}}) == 'BLOCKED'
    assert harness.acceptance_status({'terminal': True,
        'gates': {'identity': True, 'correct_physical_object': None}}) == 'REVIEW_REQUIRED'


def test_question_and_conditional_source_discussion_are_not_automatic_pass():
    assert harness.acceptance_status({'terminal': True, 'gates': {'identity': False},
        'product_outcome': {'outcome': 'clarification_required'},
        'conditional_identity_context': {'clarification': {'question': 'Which city?'}}}) == 'CLARIFICATION_REQUIRED'
    assert harness.acceptance_status({'terminal': True, 'gates': {'identity': False},
        'conditional_identity_context': {'clarification': {'question': 'Which city?'}}}) == 'FAIL'
    assert harness.acceptance_status({'terminal': True, 'gates': {'identity': False},
        'conditional_identity_context': {'joint_visual_input_verified': True,
            'hypotheses': [{'support_status': 'spatially_supported'}],
            'article_sources': [{'canonical_eligible': False}]}}) == 'CONDITIONAL_CONTEXT_AVAILABLE'
    assert harness.acceptance_status({'terminal': True, 'gates': {'identity': False},
        'conditional_identity_context': {'joint_visual_input_verified': False,
            'hypotheses': [{'support_status': 'spatially_supported'}],
            'article_sources': [{'canonical_eligible': False}]}}) == 'FAIL'
