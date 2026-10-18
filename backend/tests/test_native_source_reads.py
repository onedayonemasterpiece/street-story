import base64
import json

import pytest

from street_story.native_source_reads import source_page
from street_story.native_source_reads import native_chunk_navigation
from street_story.pdf_sources import extract_pdf, persist_pdf
from street_story.research_runs import persist_source_version
from test_headless_facts import URL, fixture
from test_pdf_sources import document


@pytest.mark.asyncio
async def test_native_navigation_locates_tail_without_issuing_text_or_filling_line_gaps(tmp_path):
    service, _, _, reader, _ = await fixture(tmp_path)
    # Cyrillic text recreates a native excerpt exceeding the byte page budget.
    text = '\n'.join(f'L{n}: ' + 'История укреплений. ' * 180 for n in [112, 113, 133, 175, 204, 208])
    try:
        with service.store.tx() as db:
            version = persist_source_version(db, run_id='headless-run', requested_url=URL, final_url=URL,
                title='Received native excerpt', content_type='text/plain', http_status=0, redirect_chain=[],
                normalized_text=text, read_status='native_received_partial', access_scope='public',
                now=service.store.now())
            first = source_page(db, run_id='headless-run', url=URL, version=version['source_version_id'])
            assert first['has_more']
            before = json.dumps(first, sort_keys=True)
            nav = native_chunk_navigation(db, run_id='headless-run', url=URL, version=version['source_version_id'])
            assert json.dumps(first, sort_keys=True) == before
            labels = {n for entry in nav['chunks'] for start, end in entry['native_line_ranges']
                for n in range(start, end+1)}
            assert labels == {112, 113, 133, 175, 204, 208}
            assert all('text' not in entry for entry in nav['chunks'])
            target = next(entry for entry in nav['chunks'] if any(start <= 208 <= end
                for start, end in entry['native_line_ranges']))
            assert target['chunk_id'] not in {chunk['chunk_id'] for chunk in first['chunks']}
            later = source_page(db, run_id='headless-run', url=URL, cursor=target['cursor'])
            assert later['source_version_id'] == version['source_version_id']
            assert any(chunk['chunk_id'] == target['chunk_id'] and 'L208:' in chunk['text']
                for chunk in later['chunks'])
            with pytest.raises(ValueError, match='not_owned'):
                native_chunk_navigation(db, run_id='foreign', url=URL, version=version['source_version_id'])
            assert first['read_status'] == 'native_received_partial'
    finally:
        await reader.search_http.aclose()


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
            assert len(first['next_cursor']) < 80
            # Previously issued long cursors stay readable with exact binding.
            changed = {'url': URL, 'version': old['source_version_id'],
                'sha256': old['content_sha256'], 'chunk': first['chunk_offset']+len(first['chunks']),
                'page': len(first['pdf']['pages'])}
            legacy = base64.urlsafe_b64encode(json.dumps(changed).encode()).decode()
            assert source_page(db, run_id='headless-run', url=URL, cursor=legacy, byte_budget=8000) == packets[1]
            token = first['next_cursor']
            mutated = token[:-1]+('0' if token[-1] != '0' else '1')
            with pytest.raises(ValueError, match='cursor_invalid'):
                source_page(db, run_id='headless-run', url=URL, cursor=mutated)
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
