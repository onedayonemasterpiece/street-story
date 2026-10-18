import pytest

from street_story.identity_visual import identify_nearest
from test_identity_lifecycle import make_service, create
from test_identity_recovery_policy import candidate, match


@pytest.mark.asyncio
async def test_near_candidate_gets_its_reference_before_distant_search(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service)
    calls = []
    async def compare(_story, _text, batch, reference_limit):
        calls.append(([x['candidate_id'] for x in batch], reference_limit))
        if len(calls) == 1:
            return {**match('c3'), '_references_sent': ['c1', 'c2']}
        assert batch[0]['candidate_id'] == 'c3'
        return match('c3')
    service._identify_photo_batch = compare
    result = await identify_nearest(service, {'id': story['id']}, '', [candidate(i) for i in range(1, 17)])
    assert result['candidate_id'] == 'c3' and result['status'] == 'match'
    assert calls == [(['c1', 'c2', 'c3', 'c4'], 2), (['c3'], 1)]


@pytest.mark.asyncio
async def test_failed_targeted_comparison_cannot_claim_early_success(tmp_path):
    service, _ = make_service(tmp_path)
    story = create(service)
    calls = []
    async def compare(_story, _text, batch, reference_limit):
        calls.append(batch)
        if len(calls) == 1:
            return {**match('c3'), '_references_sent': ['c1', 'c2']}
        return {'status': 'mismatch', 'candidate_id': 'c3', 'confidence': .99,
                'observations': ['Reference does not match'], '_references_sent': ['c3']}
    service._identify_photo_batch = compare
    result = await identify_nearest(service, {'id': story['id']}, '', [candidate(i) for i in range(1, 5)])
    assert result['status'] != 'match'
    assert len(calls) == 2
