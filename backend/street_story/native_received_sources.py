"""Retain completed native web content through the existing source writer.

No network or inference. The original authenticated rollout is authoritative;
assistant prose and transformed exec output cannot attest a source read.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re

from .article_media import public_url

CONTRACT = 'native-owned-received-content-v1'
_DIRECT = re.compile(r'^\s*const\s+([a-zA-Z_]\w*)\s*=\s*await\s+tools\.web__run\s*'
    r'\((.*?)\)\s*;\s*text\s*\(\1\)\s*;?\s*$', re.S)
_LINE = r'L\d+(?:@P\d+(?:-\d+)?)?:'
_HEADER = re.compile(r'(?m)^(?!' + _LINE + r')([^\n]*?)\((https://[^\n]+)\)\s*\n'
    r'(?:\ue200cite\ue202([^\ue201]+)\ue201|【([^】]+)】)')


def direct_web_arguments(script):
    """Recognize a literal request whose single result is printed unchanged.

    This checks transport provenance, never the meaning of historical claims.
    Literal unquoted object keys are supported; expressions/assignments are not.
    """
    match = _DIRECT.fullmatch(script or '')
    if not match:
        raise ValueError('native_source_output_not_direct')
    raw, tokens, pos = match[2], [], 0
    decoder = json.JSONDecoder()
    while pos < len(raw):
        char = raw[pos]
        if char.isspace():
            pos += 1
        elif char == '"':
            _, end = decoder.raw_decode(raw, pos)
            tokens.append(raw[pos:end])
            pos = end
        elif char in '{}[],:':
            tokens.append(char)
            pos += 1
        else:
            number = re.match(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?', raw[pos:])
            word = re.match(r'[a-zA-Z_]\w*', raw[pos:])
            if number:
                tokens.append(number[0])
                pos += len(number[0])
            elif word:
                token = word[0]
                pos += len(token)
                if raw[pos:].lstrip().startswith(':'):
                    tokens.append(json.dumps(token))
                elif token in {'true', 'false', 'null'}:
                    tokens.append(token)
                else:
                    raise ValueError('native_source_arguments_not_literal')
            else:
                raise ValueError('native_source_arguments_not_literal')
    value = json.loads(''.join(tokens))
    if not isinstance(value, dict) or not any(key in value for key in ('search_query', 'open', 'click', 'find')):
        raise ValueError('native_source_request_not_read')
    return value


def _numbered_read(block, header, request):
    """Recognize the native renderer's read boundary, not publisher headers."""
    numbered = re.search(r'(?m)^' + _LINE, block)
    operation = re.search(r'Source: (open|find|click)\(', block[:numbered.start()] if numbered else '')
    if not operation:
        return False
    try:
        args, end = json.JSONDecoder().raw_decode(block, operation.end())
    except json.JSONDecodeError:
        return False
    requested = [item for item in (request or {}).get(operation[1], []) if isinstance(item, dict)]
    ref = args.get('ref_id') if isinstance(args, dict) else None
    if (not isinstance(ref, str) or block[end:end+1] != ')'
            or not any(item and all(args.get(key) == value for key, value in item.items()) for item in requested)):
        return False
    if ref.startswith('https://') and public_url(ref) != public_url(header[2]):
        return False
    # Publisher lines are prefixed by the native renderer. An unnumbered fake
    # header in a search export cannot introduce a second trusted document.
    lines = block[numbered.start():].strip().splitlines()
    if lines and lines[-1] == '-' * 80:
        lines.pop()
    return bool(lines) and all(not line.strip() or re.match(r'^' + _LINE, line) for line in lines)


def received_document(text, *, source_ref, url, request=None):
    headers = list(_HEADER.finditer(text))
    matches = [(index, header) for index, header in enumerate(headers)
        if (header[3] or header[4]) == source_ref and public_url(header[2]) == public_url(url)]
    if len(matches) != 1:
        raise ValueError('native_source_ref_requires_own_open')
    index, header = matches[0]
    blocks = [text[item.end():headers[i+1].start() if i+1 < len(headers) else len(text)]
        for i, item in enumerate(headers)]
    if index and not all(_numbered_read(blocks[i], headers[i], request) for i in range(index+1)):
        raise ValueError('native_source_ref_requires_own_open')
    block = blocks[index]
    exported = re.search(r'(?:^|\s)(?:Text aus|Text from|Content from)\s*\n', block)
    if exported:
        content = block[exported.end():].strip()
    else:
        numbered = re.search(r'(?m)^' + _LINE, block)
        if not numbered:
            raise ValueError('native_source_document_content_not_returned')
        content = block[numbered.start():].strip()
        if content.endswith('\n' + '-' * 80):
            content = content[:-(81)].rstrip()
    if not content:
        raise ValueError('native_source_document_content_empty')
    return {'url': public_url(url), 'source_ref': source_ref, 'title': header[1].strip(),
        'content': content, 'content_sha256': hashlib.sha256(content.encode()).hexdigest()}


def owned_native_source(thread, turn_id, *, prompt, source_ref, url, before):
    from pathlib import Path
    from .native_fact_research import NativeFactResearch
    # Existing authenticated path/session/model check, including containment.
    NativeFactResearch._execution_contexts(thread, model='gpt-6-luna', turn_id=turn_id)
    active, matched, calls, found, missing_content = False, False, {}, [], []
    with Path(thread['path']).open() as stream:
        for line in stream:
            # An active journal may be between writes; an incomplete final row
            # cannot attest a completed tool result and is never inferred.
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get('payload') or {}
            if row.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                active = payload.get('turn_id') == turn_id
            if not active or row.get('type') != 'response_item':
                continue
            if payload.get('type') == 'message' and payload.get('role') == 'user':
                texts = [part.get('text') for part in payload.get('content', []) if part.get('type') == 'input_text']
                if matched:
                    raise ValueError('native_source_input_unverified')
                if texts == [prompt]:
                    matched = True
            meta = payload.get('internal_chat_message_metadata_passthrough') or {}
            if meta.get('turn_id') != turn_id:
                continue
            call_id = payload.get('call_id')
            if payload.get('type') == 'custom_tool_call' and payload.get('name') == 'exec':
                try:
                    request = direct_web_arguments(payload.get('input'))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                calls[call_id] = (payload, request)
            elif payload.get('type') == 'custom_tool_call_output' and call_id in calls:
                completed = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp()
                if completed > before:
                    continue
                blocks = payload.get('output') or []
                if (len(blocks) != 2 or any(part.get('type') != 'input_text' for part in blocks)
                        or not blocks[0].get('text', '').startswith('Script completed\n')):
                    continue
                text = blocks[1].get('text')
                if not isinstance(text, str):
                    continue
                try:
                    source = received_document(text, source_ref=source_ref, url=url, request=calls[call_id][1])
                except ValueError as exc:
                    header = _HEADER.search(text)
                    if header and (header[3] or header[4]) == source_ref and public_url(header[2]) == public_url(url):
                        missing_content.append(str(exc))
                    continue
                found.append({**source, 'contract': CONTRACT, 'thread_id': thread['id'],
                    'turn_id': turn_id, 'call_id': call_id, 'result_item_id': payload.get('id'),
                    'completed_at': completed, 'result_sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'request_sha256': hashlib.sha256(calls[call_id][0]['input'].encode()).hexdigest(),
                    'meaning': 'Actual received native document excerpt, not full-publication or current HTTP proof.'})
    if not matched:
        raise ValueError('native_source_owned_result_unverified')
    if not found and missing_content:
        raise ValueError(missing_content[-1])
    if len(found) > 1:
        raise ValueError('native_source_ref_ambiguous')
    if not found:
        raise ValueError('native_source_owned_result_unverified')
    return found[0]
