"""Per-frame batch addressing never reviews unreturned or malformed frames."""
import pytest

from street_story.service import canonical
from visual_queue_fixture import reference_receipt
from test_visual_search_continuation import prepared


async def group(tmp_path):
    svc, adapter, story, sessions = prepared(tmp_path)
    candidates = [{'candidate_id': 'wiki:77', 'name': 'Gate', 'url': 'https://en.wikipedia.org/wiki/Gate',
                   'reference_image_urls': [f'https://upload.wikimedia.org/gate-{i}.jpg' for i in range(3)]}]
    with svc.store.tx() as db:
        db.execute('UPDATE stories SET research_json=? WHERE id=?',
                   (canonical({'visual_identity': {'status': 'uncertain', 'candidates': candidates}}), story['id']))
    async def images(batch, limit, *, story_id, evidence):
        candidate = batch[0]
        url = candidate['reference_image_urls'][0]
        evidence.append(reference_receipt(candidate))
        return [(candidate['candidate_id'],'image/jpeg',url)]
    svc._candidate_reference_images = images
    session = sessions()
    session.visual_reference_limit = 4
    reply = await adapter._compare_place_images(session, {})
    assert len(reply['references']) == 3
    assert len({r['reference_id'] for r in reply['references']}) == 3
    assert len(session.state['visual_comparison']['pending']['image_parts']) == 4
    return svc, adapter, story, session, reply


def verdict(reference, status='mismatch'):
    return {'reference_id':reference['reference_id'], 'candidate_id':reference['candidate_id'],
            'status':status, 'confidence':.98,'observations':['Distinctive architecture differs.'],
            'alternative_candidate_ids':[]}


@pytest.mark.asyncio
async def test_permuted_partial_result_reviews_only_exact_returned_frame(tmp_path):
    svc, adapter, story, session, reply = await group(tmp_path)
    result = adapter._record_place_comparison(session,'grouped-test', {
        'comparison_id':reply['comparison_id'], 'reference_verdicts':[verdict(reply['references'][2])]})
    assert result['unreviewed_reference_count'] == 2
    assert svc.story(story['id'])['identity_progress']['images_reviewed_count'] == 1
    state = session.state['visual_comparison']
    assert {c['reference_id'] for c in state['queue']} == {r['reference_id'] for r in reply['references'][:2]}
    assert state['verdict_history'][0]['references'][0]['image_urls'][0].endswith('gate-2.jpg')


@pytest.mark.asyncio
async def test_invalid_item_isolated_without_erasing_valid_result(tmp_path):
    svc, adapter, story, session, reply = await group(tmp_path)
    bad = {**verdict(reply['references'][0]), 'confidence':'not-a-number'}
    result = adapter._record_place_comparison(session,'grouped-invalid', {
        'comparison_id':reply['comparison_id'], 'reference_verdicts':[bad,verdict(reply['references'][1])]})
    assert not result['matched'] and result['unreviewed_reference_count'] == 2
    assert svc.story(story['id'])['identity_progress']['images_reviewed_count'] == 1


@pytest.mark.asyncio
async def test_duplicate_address_cannot_review_or_confirm_a_frame(tmp_path):
    svc, adapter, story, session, reply = await group(tmp_path)
    item = verdict(reply['references'][0])
    result = adapter._record_place_comparison(session,'grouped-duplicate', {
        'comparison_id':reply['comparison_id'], 'reference_verdicts':[item,item]})
    assert result['unreviewed_reference_count'] == 3
    assert svc.story(story['id'])['identity_progress'].get('images_reviewed_count',0) == 0


@pytest.mark.asyncio
async def test_uncertain_receipt_without_planning_extension_advances_exact_frame(tmp_path):
    svc, adapter, story, session, reply = await group(tmp_path)
    result = adapter._record_place_comparison(session, 'grouped-uncertain', {
        'comparison_id': reply['comparison_id'],
        'reference_verdicts': [verdict(reply['references'][1], 'uncertain')]})
    assert not result['matched'] and result['unreviewed_reference_count'] == 2
    assert svc.story(story['id'])['identity_progress']['images_reviewed_count'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('item_change', [
    {'status': 'match'},
    {'search_feedback': {'next_action': 'invented_action'}},
    {'source_subject_scope': 'invented_scope'},
])
async def test_planning_schema_relaxation_does_not_accept_match_or_invalid_feedback(tmp_path, item_change):
    svc, adapter, story, session, reply = await group(tmp_path)
    result = adapter._record_place_comparison(session, 'grouped-invalid-extension', {
        'comparison_id': reply['comparison_id'],
        'reference_verdicts': [{**verdict(reply['references'][0]), **item_change}]})
    assert not result['matched'] and result['unreviewed_reference_count'] == 3
    assert svc.story(story['id'])['identity_progress'].get('images_reviewed_count', 0) == 0
