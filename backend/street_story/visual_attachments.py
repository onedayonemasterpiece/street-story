"""Direct visual transport: labels, RAM bytes and public URLs; no image processing."""
from __future__ import annotations

import base64
import binascii
from urllib.parse import urlsplit

from .errors import PermanentProviderError


def direct_visual_parts(story, supplied):
    parts = story.get('_visual_image_parts')
    mapping = story.get('_visual_reference_mapping')
    if (not isinstance(parts, list) or not 2 <= len(parts) <= 5
            or not isinstance(mapping, list) or len(mapping) != len(parts) - 1
            or mapping != supplied.get('references')):
        raise PermanentProviderError('visual_direct_attachments_invalid')
    output, seen = [], set()
    for index, part in enumerate(parts):
        label = 'SOURCE' if not index else f'REF {index}'
        if not isinstance(part, dict) or part.get('label') != label:
            raise PermanentProviderError('visual_direct_attachments_invalid')
        mime = part.get('mime_type', 'image/jpeg')
        if mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}:
            raise PermanentProviderError('visual_direct_attachments_invalid')
        data, url = part.get('data'), part.get('url')
        pixels = None
        if isinstance(data, str) and data and url is None:
            try:
                pixels = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                raise PermanentProviderError('visual_direct_attachments_invalid') from None
            if not pixels:
                raise PermanentProviderError('visual_direct_attachments_invalid')
            url = f'data:{mime};base64,{data}'
        elif isinstance(url, str) and data is None:
            parsed = urlsplit(url)
            if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
                raise PermanentProviderError('visual_direct_attachments_invalid')
        else:
            raise PermanentProviderError('visual_direct_attachments_invalid')
        if index:
            ref = mapping[index-1]
            if (not isinstance(ref, dict) or ref.get('label') != label
                    or not isinstance(ref.get('reference_id'), str) or not ref['reference_id']
                    or ref['reference_id'] in seen or not ref.get('candidate_id')):
                raise PermanentProviderError('visual_direct_attachments_invalid')
            seen.add(ref['reference_id'])
        prepared = {'label': label, 'mime_type': mime, 'url': url, 'bytes': pixels}
        if index and pixels is None:
            descriptors = [item for item in ref.get('context') or []
                           if item.get('detail_page_url') and item.get('alt')]
            if len(descriptors) == 1:
                prepared['descriptor'] = {**descriptors[0], 'image_url': url,
                    'article_url': ref.get('article_url')}
        output.append(prepared)
    return output


def visual_operation_unit(story, supplied):
    """Stable operation identity; image bytes never participate."""
    return [story['id'], story.get('_identity_generation', 0), supplied.get('comparison_id'),
            [item['reference_id'] for item in story['_visual_reference_mapping']]]


def visual_context_without_image_hashes(value):
    if isinstance(value, dict):
        return {key: visual_context_without_image_hashes(item) for key, item in value.items()
                if key not in {'photo_sha256', 'source_photo_sha256', 'model_image_sha256',
                               'image_sha256', 'model_image_hash', 'image_hash', 'selected_sha256'}}
    if isinstance(value, list):
        return [visual_context_without_image_hashes(item) for item in value]
    return value
