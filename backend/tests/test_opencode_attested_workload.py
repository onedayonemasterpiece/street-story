import pytest

from test_opencode_research import Harness


@pytest.mark.asyncio
@pytest.mark.parametrize('role,search_chars,steps', [('facts', 0, 2), ('vision', 0, 2), ('search', 16000, 3)])
async def test_reservation_counts_only_actually_permitted_search_context(role, search_chars, steps):
    h = Harness()
    try:
        prompt = 'Use websearch to find the supplied building.' if role == 'search' else 'Return one JSON object.'
        result = await h.adapter()._run(role, prompt, {'request_id': 'attested-'+role}, {'type': 'object'})
        workload = h.admissions[0][1]
        isolation = result['receipt']['isolation']
        sent = next(payload for method, path, payload in h.requests if path.endswith('prompt_async'))
        text = sent['parts'][0]['text']
        assert ('websearch' in isolation['allowed_tools']) == (role == 'search')
        assert workload['max_search_context_chars'] == search_chars
        assert workload['max_steps'] == steps
        assert workload['max_output_tokens'] == isolation['max_output_tokens']
        assert workload['estimated_tokens'] == ((len(text.encode('utf-8')) + 2)//3 + 10000
            + (search_chars + 2)//3 * steps + isolation['max_output_tokens'])
        assert len(h.sends) == 1 and h.finalized[0][1] == 'completed'
    finally:
        await h.client.aclose()
