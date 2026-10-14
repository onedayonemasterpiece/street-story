"""Bounded delivery of an owned immutable read; cursors never fetch new bytes."""
from __future__ import annotations

import base64
import hashlib
import json

SOURCE_PAGE_CONTRACT = 'native-frozen-chunk-pages-v2'
CURSOR_CONTRACT = 'native-frozen-source-cursor-v2-short-reference'


def _cursor_checksum(url, version, content_sha256, chunk, page):
    return hashlib.sha256(json.dumps([url, version, content_sha256, chunk, page],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:16]


def source_page(db, *, run_id, url, version=None, cursor=None, byte_budget=24000):
    position = {'chunk': 0, 'page': 0}
    if cursor is not None:
        try:
            if cursor.startswith('sourcepage:'):
                _, version, chunk, page, checksum = cursor.split(':')
                position = {'url': url, 'version': version, 'chunk': int(chunk), 'page': int(page),
                    'checksum': checksum}
            else:
                # Already-issued long cursors keep their exact readback.
                position = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (position['url'] != url or not isinstance(position['version'], str)
                    or any(type(position[key]) is not int or position[key] < 0 for key in ('chunk', 'page'))):
                raise ValueError()
            version = position['version']
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError('source_read_cursor_invalid') from exc
    frozen = db.execute('SELECT v.*,d.title FROM source_versions v JOIN source_documents d '
        'ON d.document_id=v.document_id WHERE source_version_id=?', (version,)).fetchone()
    if not frozen or frozen['access_scope'] != 'public':
        raise ValueError('source_read_version_unavailable')
    if url.rstrip('/') not in {frozen['requested_url'].rstrip('/'), frozen['final_url'].rstrip('/')}:
        raise ValueError('source_read_cursor_invalid' if cursor is not None else 'source_read_version_unavailable')
    # The immutable version/run relation remains valid when the latest URL pointer changes.
    owned = db.execute('SELECT 1 FROM research_run_sources WHERE run_id=? AND source_version_id=? '
        'UNION SELECT 1 FROM research_chunk_runs r JOIN source_chunks c ON c.chunk_id=r.chunk_id '
        'WHERE r.run_id=? AND c.source_version_id=? LIMIT 1', (run_id, version, run_id, version)).fetchone()
    if not owned:
        raise ValueError('source_read_version_not_owned')
    if cursor is not None:
        valid = (position['checksum'] == _cursor_checksum(url, version, frozen['content_sha256'],
            position['chunk'], position['page'])) if 'checksum' in position else position.get('sha256') == frozen['content_sha256']
        if not valid:
            raise ValueError('source_read_cursor_invalid')
    total_chunks = db.execute('SELECT COUNT(*) FROM source_chunks WHERE source_version_id=?', (version,)).fetchone()[0]
    pdf = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_pdf_documents'").fetchone():
        pdf = db.execute('SELECT raw_sha256,page_count,metadata_json FROM source_pdf_documents '
            'WHERE source_version_id=?', (version,)).fetchone()
    total_pages = pdf['page_count'] if pdf else 0
    if position['chunk'] > total_chunks or position['page'] > total_pages:
        raise ValueError('source_read_cursor_invalid')
    chunks, size = [], 0
    for row in db.execute('SELECT chunk_id,ordinal,chunk_text FROM source_chunks '
            'WHERE source_version_id=? AND ordinal>=? ORDER BY ordinal', (version, position['chunk'])):
        item = {'chunk_id': row['chunk_id'], 'text': row['chunk_text']}
        cost = len(json.dumps(item, ensure_ascii=False).encode())
        if chunks and size + cost > byte_budget:
            break
        chunks.append(item)
        size += cost
    next_chunk = position['chunk'] + len(chunks)
    pages, page_size = [], 0
    if pdf:
        for row in db.execute('SELECT page_number,page_id,text_sha256,status,error_code,start,end '
                'FROM source_pdf_pages WHERE source_version_id=? AND page_number>? ORDER BY page_number',
                (version, position['page'])):
            item = dict(row)
            cost = len(json.dumps(item, ensure_ascii=False).encode())
            if pages and page_size + cost > 8000:
                break
            pages.append(item)
            page_size += cost
    next_page = position['page'] + len(pages)
    has_more = next_chunk < total_chunks or next_page < total_pages
    next_cursor = (f'sourcepage:{version}:{next_chunk}:{next_page}:'
        + _cursor_checksum(url, version, frozen['content_sha256'], next_chunk, next_page)) if has_more else None
    packet = {key: frozen[key] for key in ('requested_url', 'final_url', 'title', 'source_version_id',
        'content_sha256', 'read_status')}
    packet.update(read_contract=SOURCE_PAGE_CONTRACT, cursor_contract=CURSOR_CONTRACT, chunks=chunks, total_chunks=total_chunks,
        chunk_offset=position['chunk'], has_more=has_more, next_cursor=next_cursor,
        delivery_complete=not has_more,
        meaning='Only the delivered chunks were read. Finished delivery does not imply complete acquisition, OCR or research.')
    if pdf:
        metadata = json.loads(pdf['metadata_json'])
        packet['pdf'] = {key: metadata[key] for key in ('page_count', 'page_index_complete',
            'text_extraction_complete', 'ocr_available', 'read_status') if key in metadata}
        packet['pdf'].update(raw_sha256=pdf['raw_sha256'], pages=pages, page_offset=position['page'])
    return packet
