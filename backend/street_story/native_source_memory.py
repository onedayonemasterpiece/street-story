"""Read-only memory of actual model deliveries; inherited calls count once."""
from __future__ import annotations

CONTRACT = 'native-source-memory-v2-retained-reads'


def source_memory(db, receipts, known):
    from .native_source_reads import _cursor_checksum
    calls, versions, blocked = {}, {}, {}
    for receipt in receipts:
        for call_id, call in receipt.get('native_tool_calls', {}).items():
            if (call.get('phase') == 'completed'
                    and (call.get('input') or {}).get('name') in {'webfetch', 'retain_native_source'}):
                calls[((call.get('input') or {}).get('thread_id'), call_id)] = call
    for call in calls.values():
        args = (call.get('input') or {}).get('arguments') or {}
        result = call.get('result') or {}
        for failed in result.get('read_attempts') or []:
            if failed.get('error_code'):
                key = (failed.get('requested_url'), failed.get('error_code'))
                item = blocked.setdefault(key, {'url': key[0], 'error_code': key[1], 'model_read_requests': 0})
                item['model_read_requests'] += 1
        for document in result.get('documents') or []:
            version = document.get('source_version_id')
            if not version:
                continue
            item = versions.setdefault(version, {key: document.get(key) for key in
                ('source_version_id', 'content_sha256', 'requested_url', 'final_url', 'title', 'read_status')})
            item.setdefault('delivered_chunk_ids', set()).update(c['chunk_id'] for c in document.get('chunks') or [])
            item['model_read_requests'] = item.get('model_read_requests', 0) + 1
            if (call.get('input') or {}).get('name') == 'retain_native_source':
                item['native_retention_requests'] = item.get('native_retention_requests', 0) + 1
                item['native_read_scope'] = 'actual_received_excerpt_not_full_publication'
            field = 'cursor_requests' if args.get('cursor') else 'initial_read_requests'
            item[field] = item.get(field, 0) + 1
    for version, item in versions.items():
        chunks = list(db.execute('SELECT chunk_id,ordinal FROM source_chunks WHERE source_version_id=? ORDER BY ordinal', (version,)))
        received = item.pop('delivered_chunk_ids')
        missing = [row for row in chunks if row['chunk_id'] not in received]
        item.update(delivered_chunks=len(received), total_chunks=len(chunks),
            delivery_state='partial' if missing else 'all_stored_chunks_delivered',
            acquisition_complete=item['read_status'] == 'complete',
            extracted_claim_ids=sorted({fact['fact_id'] for fact in known for source in fact.get('sources') or []
                if source.get('source_version_id') == version or any(
                    span.get('source_version_id') == version for span in source.get('supports') or [])}),
            question_exhausted='not_recorded', network_refetches='not_recorded', cache_hits='not_recorded')
        item['next_unread_cursor'] = (f'sourcepage:{version}:{missing[0]["ordinal"]}:0:'
            + _cursor_checksum(item['requested_url'], version, item['content_sha256'], missing[0]['ordinal'], 0)) if missing else None
    return {'contract': CONTRACT, 'sources': list(versions.values()),
        'blocked': list(blocked.values()),
        'meaning': 'Actual model deliveries, not semantic exhaustion or proof. Inherited calls counted once. '
            'Native retention records an already received excerpt; chunk delivery describes adapter '
            'text/ID deliveries, not absence of earlier native reading. Acquired complete and all '
            'stored chunks delivered differ from OCR/research completeness. '
            'Unknown cache/network counts are explicit; a model reread still costs inference.'}
