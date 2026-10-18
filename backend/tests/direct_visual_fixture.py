"""Provider test attachments; bytes are opaque, never rendered or compared."""
import base64
from copy import deepcopy
import json


def visual_args(snapshot, story, schema, context):
    supplied = json.loads(context) if isinstance(context, str) else deepcopy(context)
    if story.get('_visual_image_parts') is not None:
        return None, story, schema, context
    refs = supplied.get('references') or []
    for index, ref in enumerate(refs, 1):
        ref.setdefault('label', f'REF {index}')
        ref.setdefault('reference_id', f'reference-{index}')
    raw = snapshot if isinstance(snapshot, bytes) and snapshot else b'opaque-source-fixture'
    parts = [{'label': 'SOURCE', 'mime_type': 'image/jpeg', 'data': base64.b64encode(raw).decode()}]
    parts += [{'label': ref['label'], 'url': ref.get('source_url') or f'https://example.org/reference-{i}.jpg'}
              for i, ref in enumerate(refs, 1)]
    story = {**story, '_visual_image_parts': parts, '_visual_reference_mapping': refs}
    return None, story, schema, json.dumps(supplied) if isinstance(context, str) else supplied


def opencode_args(snapshot, binding, schema, context=None):
    if not isinstance(context, dict) and not (isinstance(context, str) and context.startswith('{')):
        context = {'comparison_id': 'opencode-fixture', 'references': [{'candidate_id': 'wiki:1'}]}
    _, story, _, context = visual_args(snapshot, {}, schema, context)
    story['_visual_image_parts'][0]['mime_type'] = 'image/png'
    return story['_visual_image_parts'], binding, schema, context
