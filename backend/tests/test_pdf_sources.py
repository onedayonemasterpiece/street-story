import hashlib
from io import BytesIO

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from street_story.pdf_sources import extract_pdf, persist_pdf
from street_story.research_runs import persist_source_version
from test_headless_facts import fixture, URL


def document(count=70, *, blank=False, author='First'):
    writer = PdfWriter()
    for number in range(1, count + 1):
        page = writer.add_blank_page(width=600, height=800)
        if blank:
            continue
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf 30 750 Td (Historical own document page {number}. '
                        f'Independent text for the retained public document.) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.add_metadata({'/Author': author})
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.asyncio
async def test_reader_retains_pdf_bytes_hash_all_pages_and_last_page_without_refetch(tmp_path):
    service, _, _, reader, _ = await fixture(tmp_path)
    original = document()
    requests = []
    async def fetch(request):
        requests.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'application/pdf'}, content=original)
    await reader.search_http.aclose()
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(fetch))
    try:
        docs = await reader._fetch_page_documents([URL], {'research_run_id': 'headless-run'})
        assert URL in docs
        result = docs[URL]
        assert result['read_status'] == 'complete'
        assert result['pdf']['page_count'] == 70
        assert 'document page 70.' in result['normalized_text']
        assert len(result['pdf']['pages']) == 70
        assert result['pdf']['raw_sha256'] == hashlib.sha256(original).hexdigest()
        with service.store.connection() as db:
            raw = db.execute('SELECT raw_bytes FROM source_pdf_documents WHERE source_version_id=?',
                (result['source_version_id'],)).fetchone()[0]
            assert raw == original
            assert db.execute('SELECT COUNT(*) FROM source_pdf_pages WHERE source_version_id=?',
                (result['source_version_id'],)).fetchone()[0] == 70
        reopened = await reader._fetch_page_documents([URL], {'research_run_id': 'headless-run'})
        assert reopened[URL]['source_version_id'] == result['source_version_id']
        assert reopened[URL]['pdf'] == result['pdf']
        assert len(requests) == 1
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_scanned_pdf_is_retained_as_requires_ocr_not_empty_history(tmp_path):
    service, _, _, reader, _ = await fixture(tmp_path)
    raw = document(3, blank=True)
    await reader.search_http.aclose()
    reader.search_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, headers={'content-type': 'application/pdf'}, content=raw)))
    try:
        result = (await reader._fetch_page_documents([URL], {'research_run_id': 'headless-run'}))[URL]
        assert result['read_status'] == 'requires_ocr'
        assert result['pdf']['ocr_available'] is False
        assert not result['pdf']['text_extraction_complete']
        assert [p['status'] for p in result['pdf']['pages']] == ['requires_ocr'] * 3
        with service.store.connection() as db:
            assert db.execute('SELECT raw_bytes FROM source_pdf_documents WHERE source_version_id=?',
                (result['source_version_id'],)).fetchone()[0] == raw
    finally:
        await reader.search_http.aclose()


@pytest.mark.asyncio
async def test_equal_pdf_text_at_same_url_keeps_distinct_original_byte_versions(tmp_path):
    service, _, _, reader, _ = await fixture(tmp_path)
    versions = []
    try:
        with service.store.tx() as db:
            for author in ['First', 'Second']:
                raw = document(2, author=author)
                pdf = extract_pdf(raw)
                result = persist_source_version(db, run_id='headless-run', requested_url=URL, final_url=URL,
                    title='Archive', content_type='application/pdf', http_status=200, redirect_chain=[],
                    normalized_text=pdf['normalized_text'], read_status='complete', access_scope='public',
                    source_bytes_sha256=pdf['raw_sha256'], now=service.store.now())
                persist_pdf(db, result['source_version_id'], raw, pdf)
                versions.append(result)
        assert versions[0]['content_sha256'] == versions[1]['content_sha256']
        assert versions[0]['source_version_id'] != versions[1]['source_version_id']
        with service.store.connection() as db:
            assert db.execute('SELECT COUNT(DISTINCT raw_sha256) FROM source_pdf_documents').fetchone()[0] == 2
    finally:
        await reader.search_http.aclose()


def test_one_pdf_page_error_preserves_later_page_and_error_index(monkeypatch):
    class Page:
        def __init__(self, value):
            self.value = value
        def extract_text(self):
            if self.value is None:
                raise ValueError('Page could not be decoded')
            return self.value
    class Reader:
        is_encrypted = False
        def __init__(self, *args, **kwargs):
            self.pages = [Page('First page'), Page(None), Page('Last historical page')]
    monkeypatch.setattr('pypdf.PdfReader', Reader)
    result = extract_pdf(b'%PDF-1.7\ncontrolled page error')
    assert result['read_status'] == 'partial_page_errors'
    assert result['page_index_complete'] and not result['text_extraction_complete']
    assert result['pages'][1]['error_code'] == 'ValueError'
    assert 'Last historical page' in result['normalized_text']


def test_pdf_mime_does_not_accept_non_pdf_signature():
    with pytest.raises(ValueError, match='pdf_signature_invalid'):
        extract_pdf(b'<html>not a PDF</html>')
