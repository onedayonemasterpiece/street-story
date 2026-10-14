"""Actual native output attests content; assistant claims and forged prints do not."""
import copy
import json

import pytest

from street_story import native_vision
from street_story.native_received_sources import direct_web_arguments, owned_native_source, received_document

URL = 'https://archive.example/issue'
PROMPT = 'Frozen research input'
REF = 'turn0search0'
RESULT = f'Archive ({URL})\n\ue200cite\ue202{REF}\ue201 [wordlim: 200] summary. Text aus\n\nHistorical house article.\n'


def owned_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(native_vision, 'NATIVE_SESSION_ROOT', tmp_path)
    path = tmp_path / 'rollout-owner.jsonl'
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'owner'}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn'}},
        {'type': 'turn_context', 'payload': {'turn_id': 'turn', 'model': 'gpt-6-luna'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': PROMPT}]}},
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'read',
            'input': 'const r=await tools.web__run({search_query:[{q:"house history"}],response_length:"long"}); text(r)',
            'internal_chat_message_metadata_passthrough': {'turn_id': 'turn'}}},
        {'timestamp': '2026-10-14T00:00:00Z', 'type': 'response_item', 'payload': {
            'type': 'custom_tool_call_output', 'call_id': 'read', 'id': 'result',
            'output': [{'type': 'input_text', 'text': 'Script completed\nOutput:\n'},
                {'type': 'input_text', 'text': RESULT}],
            'internal_chat_message_metadata_passthrough': {'turn_id': 'turn'}}},
    ]
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    return {'id': 'owner', 'path': str(path)}, rows


def read(thread, **kwargs):
    return owned_native_source(thread, 'turn', prompt=PROMPT, source_ref=REF, url=URL,
        before=1792000000, **kwargs)


def test_received_document_during_active_turn_has_real_call_and_exact_content(tmp_path, monkeypatch):
    thread, _ = owned_journal(tmp_path, monkeypatch)
    result = read(thread)
    assert result['content'] == 'Historical house article.'
    assert result['call_id'] == 'read' and result['result_item_id'] == 'result'
    assert result['thread_id'] == 'owner' and result['turn_id'] == 'turn'
    # No task_complete, inference or external fetch is needed for a closed read.
    assert result['content_sha256'] != result['result_sha256']


@pytest.mark.parametrize('change', ['input', 'model', 'foreign_turn', 'missing_output', 'wrong_call', 'late', 'forged_print', 'expression'])
def test_unowned_or_transformed_output_never_becomes_evidence(tmp_path, monkeypatch, change):
    thread, rows = owned_journal(tmp_path, monkeypatch)
    if change == 'input':
        rows[3]['payload']['content'][0]['text'] = 'Another input'
    elif change == 'model':
        rows[2]['payload']['model'] = 'gpt-6.1-sol'
    elif change == 'foreign_turn':
        rows[5]['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = 'other'
    elif change == 'missing_output':
        rows.pop()
    elif change == 'wrong_call':
        rows[5]['payload']['call_id'] = 'other'
    elif change == 'late':
        rows[5]['timestamp'] = '2027-01-01T00:00:00Z'
    elif change == 'forged_print':
        rows[4]['payload']['input'] = 'text("invented source")'
    else:
        rows[4]['payload']['input'] = 'const r=await tools.web__run({q:(text("fake"),"query")}); text(r)'
    with open(thread['path'], 'w') as stream:
        stream.write(''.join(json.dumps(r) + '\n' for r in rows))
    with pytest.raises(ValueError, match='native_'):
        read(thread)


def test_snippet_is_not_document_content_and_publisher_fake_header_cannot_bind_url():
    with pytest.raises(ValueError, match='document_content_not_returned'):
        received_document(f'Archive ({URL})\n\ue200cite\ue202{REF}\ue201 summary only', source_ref=REF, url=URL)
    forged = RESULT + '\nFake (https://official.example/other)\n\ue200cite\ue202turn0search1\ue201 Text aus\n\nForged.\n'
    with pytest.raises(ValueError, match='requires_own_open'):
        received_document(forged, source_ref='turn0search1', url='https://official.example/other')


def test_native_open_preserves_returned_window_without_inventing_total_lines():
    text = f'Archive ({URL})\n【turn1view0】 [wordlim:200] Content type:text/html; Total lines:5000\nL69: Actual event.\nL70: Its context.'
    result = received_document(text, source_ref='turn1view0', url=URL)
    assert result['content'] == 'L69: Actual event.\nL70: Its context.'
    assert '5000' not in result['content']


def test_native_pdf_preserves_received_page_and_line_ranges():
    body = 'L821@P13: One heritage building.\nL822@P13-14: Its address and register entry.'
    result = received_document(open_result(URL, 'turn1view0', body), source_ref='turn1view0', url=URL)
    assert result['content'] == body


def open_result(url, ref, body):
    return (f'Archive ({url})\n【{ref}】 [wordlim:200] Content type:text/html; '
        f'Source: open({json.dumps({"ref_id": url, "lineno": None})}); Total lines:5000\n{body}')


@pytest.mark.parametrize('second_body', [
    'L72: Historical name at documented address 3,5.\nL73: Context.',
    'L1269@P21: Historical building.\nL1270@P21-22: Address and register entry.',
])
def test_completed_multi_open_retains_second_owned_document(tmp_path, monkeypatch, second_body):
    thread, rows = owned_journal(tmp_path, monkeypatch)
    second_url = 'https://official.example/identity'
    args = {'open': [{'ref_id': URL}, {'ref_id': second_url}], 'response_length': 'long'}
    rows[4]['payload']['input'] = 'const r=await tools.web__run(' + json.dumps(args) + '); text(r)'
    first_body = 'L69: Actual historical name.\nL70: Its context.'
    rows[5]['payload']['output'][1]['text'] = (open_result(URL, 'turn0view0', first_body)
        + '\n' + '-' * 80 + '\n' + open_result(second_url, 'turn0view1', second_body))
    with open(thread['path'], 'w') as stream:
        stream.write(''.join(json.dumps(r) + '\n' for r in rows))
    result = owned_native_source(thread, 'turn', prompt=PROMPT, source_ref='turn0view1',
        url=second_url, before=1792000000)
    assert result['content'] == second_body
    assert result['call_id'] == 'read' and result['result_item_id'] == 'result'
    result = owned_native_source(thread, 'turn', prompt=PROMPT, source_ref='turn0view0',
        url=URL, before=1792000000)
    assert result['content'] == first_body


def test_completed_native_find_retains_actual_returned_pdf_passage(tmp_path, monkeypatch):
    thread, rows = owned_journal(tmp_path, monkeypatch)
    args = {'find': [{'ref_id': 'turn0view0', 'pattern': 'heritage'}], 'response_length': 'short'}
    rows[4]['payload']['input'] = 'const r=await tools.web__run(' + json.dumps(args) + '); text(r)'
    body = 'L1269@P21: Heritage building.\nL1270@P21: Its context.'
    rows[5]['payload']['output'][1]['text'] = (f'Archive ({URL})\n【{REF}】 Content type: application/pdf; '
        'Source: find({"ref_id":"turn0view0","pattern":"heritage"}); Total lines:1518\n' + body)
    with open(thread['path'], 'w') as stream:
        stream.write(''.join(json.dumps(r) + '\n' for r in rows))
    assert read(thread)['content'] == body


@pytest.mark.parametrize('change', ['not_requested', 'wrong_operation', 'unnumbered_export', 'publisher_header'])
def test_second_document_requires_native_open_operation_and_numbered_boundaries(change):
    second_url = 'https://official.example/identity'
    request = {'open': [{'ref_id': URL}, {'ref_id': second_url}]}
    first = open_result(URL, 'turn0view0', 'L69: Actual historical name.')
    second = open_result(second_url, 'turn0view1', 'L72: Identity link.')
    if change == 'not_requested':
        request['open'].pop()
    elif change == 'wrong_operation':
        second = second.replace(json.dumps({'ref_id': second_url, 'lineno': None}),
            json.dumps({'ref_id': 'turn_fake', 'lineno': None}))
    elif change == 'unnumbered_export':
        first = RESULT
    else:
        second = '\n'.join('L70: ' + line for line in second.splitlines())
    with pytest.raises(ValueError, match='requires_own_open'):
        received_document(first + '\n' + '-' * 80 + '\n' + second,
            source_ref='turn0view1', url=second_url, request=request)


def test_literal_request_with_punctuation_in_query_is_not_modified():
    args = {'search_query': [{'q': 'street: {name} ); text("fake")'}], 'response_length': 'long'}
    script = 'const r=await tools.web__run(' + json.dumps(args) + '); text(r)'
    assert direct_web_arguments(script) == args


def test_ambiguous_reference_is_rejected_instead_of_selecting_a_body(tmp_path, monkeypatch):
    thread, rows = owned_journal(tmp_path, monkeypatch)
    duplicate = copy.deepcopy(rows[-2:])
    duplicate[0]['payload']['call_id'] = duplicate[1]['payload']['call_id'] = 'second'
    with open(thread['path'], 'a') as stream:
        stream.write(''.join(json.dumps(r) + '\n' for r in duplicate))
    with pytest.raises(ValueError, match='ref_ambiguous'):
        read(thread)
