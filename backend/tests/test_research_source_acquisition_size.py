"""Archive text has its own request budget; media and SSRF fences remain."""
import httpx
import pytest

from street_story.article_media import MAX_PAGE_BYTES, cached_public_page
from street_story.providers import MAX_RESEARCH_SOURCE_BYTES
from test_headless_facts import CLAIM, URL, fixture


@pytest.mark.asyncio
async def test_large_archive_uses_one_acquisition_and_preserves_media_budget(tmp_path):
    service, _, _, reader, _ = await fixture(tmp_path)
    await reader.search_http.aclose()
    body = b'<!--' + b'x' * MAX_PAGE_BYTES + b'--><main><p>' + (CLAIM * 3).encode() + b'</p></main>'
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/html'}, content=body)
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await reader._fetch_page_documents([URL], {'research_run_id': 'headless-run'})
        doc = result[URL]
        assert doc['read_status'] == 'complete'  # No body was truncated at 768 KiB.
        assert doc['normalized_text'] == CLAIM * 3
        # A larger research acquisition cannot silently enlarge a media read.
        with pytest.raises(ValueError, match='article_media_size'):
            await cached_public_page(service.store, reader.search_http, URL)
        same = await cached_public_page(service.store, reader.search_http, URL, maximum=MAX_RESEARCH_SOURCE_BYTES)
        assert same[2] == body and len(requests) == 1
        with service.store.connection() as db:
            saved = db.execute('SELECT normalized_text FROM source_versions WHERE source_version_id=?',
                (doc['source_version_id'],)).fetchone()
        assert saved[0] == CLAIM * 3
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_archive_above_request_budget_records_specific_failure_without_source(tmp_path):
    service, _, _, reader, _ = await fixture(tmp_path)
    await reader.search_http.aclose()
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, headers={'content-type': 'application/pdf',
            'content-length': str(MAX_RESEARCH_SOURCE_BYTES + 1)}, content=b'%PDF-')))
    try:
        assert await reader._fetch_page_documents([URL], {'research_run_id': 'headless-run'}) == {}
        with service.store.connection() as db:
            attempt = db.execute('SELECT status,error_code,source_version_id FROM research_run_sources '
                'WHERE run_id=? AND url=?', ('headless-run', URL)).fetchone()
        assert tuple(attempt) == ('failed', 'article_media_size', None)
    finally:
        await reader.search_http.aclose()
