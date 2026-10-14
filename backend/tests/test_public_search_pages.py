import html
from urllib.parse import parse_qs

import httpx
import pytest

from street_story.errors import RetryableProviderError
from test_headless_facts import fixture


def results(start, count, query, *, action='/html/'):
    links = ''.join(f'<a class="result__a" href="https://example.org/source-{i}">Source {i}</a>'
        f'<a class="result__snippet">Discovery snippet {i}</a>' for i in range(start, start+count))
    return links + (f'<form method="POST" action="{action}"><input type="hidden" name="q" '
        f'value="{html.escape(query)}"><input type="hidden" name="s" value="30">'
        '<input type="submit" name="next" value="Next"></form>' if start == 0 else '')


@pytest.mark.asyncio
async def test_public_search_uses_returned_next_form_without_12_result_cap(tmp_path):
    _, _, _, reader, _ = await fixture(tmp_path)
    query = 'Selected building historical sources'
    requests = []
    def fetch(request):
        requests.append(request)
        if request.method == 'POST':
            assert parse_qs(request.content.decode()) == {'q':[query],'s':['30'],'next':['Next']}
            return httpx.Response(200, text=results(30, 18, query))
        return httpx.Response(200, text=results(0, 30, query))
    await reader.search_http.aclose()
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(fetch))
    try:
        first = await reader._public_web_search(query)
        assert len(first.grounding_sources) == 30
        assert first.payload['has_more']
        next_page = await reader._public_web_search(query, cursor=first.payload['next_cursor'])
        assert len(next_page.grounding_sources) == 18 and not next_page.payload['has_more']
        assert [request.method for request in requests] == ['GET','POST']
        with pytest.raises(RetryableProviderError, match='cursor_invalid'):
            await reader._public_web_search('Another POI', cursor=first.payload['next_cursor'])
        assert len(requests) == 2
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_public_search_never_posts_to_offsite_next_form(tmp_path):
    _, _, _, reader, _ = await fixture(tmp_path)
    await reader.search_http.aclose()
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, text=results(0, 20, 'History', action='https://foreign.example/submit'))))
    try:
        response = await reader._public_web_search('History')
        assert len(response.grounding_sources) == 20
        assert not response.payload['has_more'] and response.payload['next_cursor'] is None
        assert response.payload['pagination_status'] == 'no_supported_next_form'
    finally:
        await reader.search_http.aclose()
