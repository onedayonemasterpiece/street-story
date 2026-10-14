import base64
import json

import pytest

from street_story.native_source_reads import source_page
from street_story.pdf_sources import extract_pdf, persist_pdf
from street_story.research_runs import persist_source_version
from test_headless_facts import URL, fixture
from test_pdf_sources import document


@pytest.mark.asyncio
async def test_pdf_delivery_preserves_every_chunk_and_page_after_url_version_changes(tmp_path):
    service, _, _, reader, _ = await fixture(tmp_path)
    raw = document(150)
    pdf = extract_pdf(raw)
    try:
        with service.store.tx() as db:
            old = persist_source_version(db, run_id='headless-run', requested_url=URL, final_url=URL,
                title='Own archive', content_type='application/pdf', http_status=200, redirect_chain=[],
                normalized_text=pdf['normalized_text'], read_status=pdf['read_status'], access_scope='public',
                source_bytes_sha256=pdf['raw_sha256'], now=service.store.now())
            persist_pdf(db, old['source_version_id'], raw, pdf)
            first = source_page(db, run_id='headless-run', url=URL, version=old['source_version_id'], byte_budget=8000)
            assert first['has_more'] and not first['delivery_complete']
            persist_source_version(db, run_id='headless-run', requested_url=URL, final_url=URL,
                title='New bytes at same URL', content_type='text/html', http_status=200, redirect_chain=[],
                normalized_text='A later source revision', read_status='complete', access_scope='public', now=service.store.now())
            packets = [first]
            while packets[-1]['has_more']:
                packets.append(source_page(db, run_id='headless-run', url=URL,
                    cursor=packets[-1]['next_cursor'], byte_budget=8000))
            chunks = [chunk for packet in packets for chunk in packet['chunks']]
            expected = [dict(row) for row in db.execute('SELECT chunk_id,chunk_text AS text FROM source_chunks '
                'WHERE source_version_id=? ORDER BY ordinal', (old['source_version_id'],))]
            assert chunks == expected
            assert 'document page 150.' in chunks[-1]['text']
            assert [page['page_number'] for packet in packets for page in packet['pdf']['pages']] == list(range(1,151))
            assert {p['source_version_id'] for p in packets} == {old['source_version_id']}
            assert packets[-1]['delivery_complete']
            assert source_page(db, run_id='headless-run', url=URL, cursor=first['next_cursor'], byte_budget=8000) == packets[1]
            with pytest.raises(ValueError, match='not_owned'):
                source_page(db, run_id='foreign-run', url=URL, cursor=first['next_cursor'])
            with pytest.raises(ValueError, match='cursor_invalid'):
                source_page(db, run_id='headless-run', url='https://example.org/another', cursor=first['next_cursor'])
            changed = json.loads(base64.urlsafe_b64decode(first['next_cursor']))
            changed['sha256'] = '0' * 64
            bad = base64.urlsafe_b64encode(json.dumps(changed).encode()).decode()
            with pytest.raises(ValueError, match='cursor_invalid'):
                source_page(db, run_id='headless-run', url=URL, cursor=bad)
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_requires_ocr_stays_incomplete_after_last_delivery(tmp_path):
    service, _, _, reader, _ = await fixture(tmp_path)
    raw = document(90, blank=True)
    pdf = extract_pdf(raw)
    try:
        with service.store.tx() as db:
            frozen = persist_source_version(db, run_id='headless-run', requested_url=URL, final_url=URL,
                title='Scanned archive', content_type='application/pdf', http_status=200, redirect_chain=[],
                normalized_text=pdf['normalized_text'], read_status='requires_ocr', access_scope='public', now=service.store.now())
            persist_pdf(db, frozen['source_version_id'], raw, pdf)
            page = source_page(db, run_id='headless-run', url=URL, version=frozen['source_version_id'])
            pages = list(page['pdf']['pages'])
            while page['has_more']:
                page = source_page(db, run_id='headless-run', url=URL, cursor=page['next_cursor'])
                pages.extend(page['pdf']['pages'])
            assert page['delivery_complete'] and page['read_status'] == 'requires_ocr'
            assert not page['pdf']['text_extraction_complete'] and not page['pdf']['ocr_available']
            assert len(pages) == 90 and all(p['status'] == 'requires_ocr' for p in pages)
    finally:
        await reader.search_http.aclose()
