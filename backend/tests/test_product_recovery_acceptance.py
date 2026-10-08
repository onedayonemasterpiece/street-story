import hashlib
import importlib.util
import json
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


def test_manifest_digest_freezes_expectations_but_is_independent_of_json_format(tmp_path):
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


def test_between_identity_and_facts_is_not_terminal(tmp_path):
    svc, now, case = readback_fixture(tmp_path)
    now[0] = 210
    with svc.store.tx() as db:
        db.execute("DELETE FROM jobs WHERE id='facts'")
    assert harness.read_case(svc, case, item())['terminal'] is False


def test_resource_block_and_missing_hidden_expected_id_are_distinct_results():
    assert harness.acceptance_status({'terminal': True,
        'product_outcome': {'outcome': 'resource_blocked'}, 'gates': {'identity': False}}) == 'BLOCKED'
    assert harness.acceptance_status({'terminal': True,
        'gates': {'identity': True, 'correct_physical_object': None}}) == 'REVIEW_REQUIRED'
