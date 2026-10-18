"""PDF decoding and page provenance in the existing immutable source reader."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json


def extract_pdf(raw: bytes) -> dict:
    from pypdf import PdfReader
    if not raw.startswith(b'%PDF-'):
        raise ValueError('pdf_signature_invalid')
    try:
        reader = PdfReader(BytesIO(raw), strict=True)
    except Exception as exc:
        raise ValueError('pdf_structure_invalid') from exc
    if reader.is_encrypted and not reader.decrypt(''):
        raise ValueError('pdf_encrypted_requires_password')
    raw_sha = hashlib.sha256(raw).hexdigest()
    pages, parts, offset = [], [], 0
    for number, page in enumerate(reader.pages, 1):
        error = None
        try:
            text = page.extract_text() or ''
            status = 'extracted' if text.strip() else 'requires_ocr'
        except Exception as exc:
            text, status, error = '', 'page_error', type(exc).__name__
        prefix = ('\n\n' if parts else '') + f'[PDF page {number}]\n'
        start = offset + len(prefix)
        parts.append(prefix + text)
        offset = start + len(text)
        pages.append({'page_number': number,
            'page_id': 'pdfp_' + hashlib.sha256(f'{raw_sha}:{number}'.encode()).hexdigest()[:24],
            'text': text, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'status': status, 'error_code': error, 'start': start, 'end': offset})
    if not pages:
        raise ValueError('pdf_no_pages')
    failures = any(p['status'] == 'page_error' for p in pages)
    ocr = any(p['status'] == 'requires_ocr' for p in pages)
    return {'raw_sha256': raw_sha, 'raw_bytes': len(raw), 'page_count': len(pages), 'pages': pages,
        'normalized_text': ''.join(parts),
        'read_status': 'partial_page_errors' if failures else 'requires_ocr' if ocr else 'complete',
        'page_index_complete': True, 'text_extraction_complete': not (failures or ocr),
        'ocr_available': False, 'ocr_status': 'requires_ocr' if ocr else 'not_needed'}


def persist_pdf(db, version_id: str, raw: bytes, document: dict):
    """Retain original bytes and every page, including local errors, durably."""
    db.execute('CREATE TABLE IF NOT EXISTS source_pdf_documents ('
        'source_version_id TEXT PRIMARY KEY,raw_sha256 TEXT NOT NULL,raw_bytes BLOB NOT NULL,'
        'page_count INTEGER NOT NULL,metadata_json TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS source_pdf_pages ('
        'source_version_id TEXT NOT NULL,page_number INTEGER NOT NULL,page_id TEXT NOT NULL,'
        'text TEXT NOT NULL,text_sha256 TEXT NOT NULL,status TEXT NOT NULL,error_code TEXT,'
        'start INTEGER NOT NULL,end INTEGER NOT NULL,PRIMARY KEY(source_version_id,page_number))')
    metadata = {k: v for k, v in document.items() if k not in {'pages', 'normalized_text'}}
    db.execute('INSERT OR IGNORE INTO source_pdf_documents VALUES(?,?,?,?,?)',
        (version_id, document['raw_sha256'], raw, document['page_count'], json.dumps(metadata)))
    for page in document['pages']:
        db.execute('INSERT OR IGNORE INTO source_pdf_pages VALUES(?,?,?,?,?,?,?,?,?)',
            (version_id, *[page[k] for k in ('page_number', 'page_id', 'text', 'text_sha256',
                                          'status', 'error_code', 'start', 'end')]))


def pdf_metadata(db, version_id: str) -> dict:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='source_pdf_documents'").fetchone():
        return {}
    row = db.execute('SELECT metadata_json FROM source_pdf_documents WHERE source_version_id=?', (version_id,)).fetchone()
    if not row:
        return {}
    value = json.loads(row[0])
    value['pages'] = [dict(p) for p in db.execute('SELECT page_number,page_id,text_sha256,status,error_code,start,end '
        'FROM source_pdf_pages WHERE source_version_id=? ORDER BY page_number', (version_id,))]
    return {'pdf': value}


def public_pdf_metadata(document: dict) -> dict:
    return {**{k: v for k, v in document.items() if k not in {'normalized_text', 'pages'}},
        'pages': [{k: v for k, v in page.items() if k != 'text'} for page in document['pages']]}
