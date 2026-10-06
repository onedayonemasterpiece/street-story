"""Queue/control tests prepare Live parts in RAM; transport is tested separately."""
import base64

from live_interaction import with_live_tool_parts


def queued_reference_count(state):
    pending = state.get('pending_descriptor') or state.get('pending') or {}
    return len(state.get('queue') or []) + len(pending.get('candidates') or [])


def reference_receipt(candidate):
    url = candidate['reference_image_urls'][0]
    return {'candidate_id': candidate['candidate_id'], 'reference_id': candidate.get('reference_id'),
            'source_url': url, 'image_url': url, 'article_url': candidate.get('url'),
            'kind': 'article_img', 'article_title': candidate.get('name'), 'delivery': 'direct_public_url'}


def prepare_live_parts(adapter, reference_bytes):
    async def comparison_result(pending, *, direct_provider=False):
        if direct_provider:
            return dict(pending['reply'])
        assert len(pending['image_parts']) == 2
        parts = []
        for index, part in enumerate(pending['image_parts']):
            data = part.get('data') or base64.b64encode(reference_bytes).decode()
            parts.append({'inlineData': {'mimeType': part.get('mime_type', 'image/jpeg'),
                'displayName': 'SOURCE' if index == 0 else 'REF_1', 'data': data}})
        return with_live_tool_parts(pending['reply'], parts)
    adapter._comparison_result = comparison_result
