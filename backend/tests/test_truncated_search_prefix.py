import hashlib
import json
import pytest
from street_story.opencode_research import search_tool_sources

NOTICE='\n\n...5382 bytes truncated...\n\nThe tool call succeeded but the output was truncated. Full output saved to: /untrusted/path\nUse the Task tool to process this file.'

def message(output, *, status='completed', truncated=True):
    return {'info':{'id':'msg_original'},'parts':[{'type':'tool','tool':'websearch','callID':'call_original','state':{'status':status,'input':{'query':'Житомирская улица Калининград здание'},'metadata':{'provider':'parallel','truncated':truncated,'outputPath':'/must/not/be/read'},'output':output}}]}

def test_closed_objects_survive_real_truncated_json_format():
    item={'url':'https://example.org/address','title':'Mapped street building','excerpts':['public source address; https://evil.org/snippet is not a result']}
    output='{\n  "search_id": "search_original",\n  "results": [\n'+json.dumps(item)+' ,\n{"url":"https://example.org/incomplete","title":"cut'+NOTICE
    sources,calls=search_tool_sources([message(output)],'unused')
    assert [s['url']for s in sources]==['https://example.org/address']
    assert sources[0]['search_query']=='Житомирская улица Калининград здание'
    assert sources[0]['search_backend']=='parallel'
    assert sources[0]['search_call_id']=='call_original'
    assert sources[0]['search_message_id']=='msg_original'
    assert sources[0]['tool_output_sha256']==hashlib.sha256(output.encode()).hexdigest()
    assert len(calls)==1 and calls[0]['status']=='completed'

@pytest.mark.parametrize('root', ['object','list'])
def test_valid_json_then_truncation_notice(root):
    item={'url':'https://example.org/closed','title':'Closed'}
    data={'search_id':'search_original','results':[item]}if root=='object'else[item]
    sources,_=search_tool_sources([message(json.dumps(data)+NOTICE)],'unused')
    assert [s['url']for s in sources]==['https://example.org/closed']

@pytest.mark.parametrize('status',['error','running','pending'])
def test_unfinished_or_failed_tool_never_discovers_urls(status):
    output='{"results":[{"url":"https://example.org/closed"}'+NOTICE
    sources,calls=search_tool_sources([message(output,status=status)],'unused')
    assert sources==[] and calls[0]['status']==status

@pytest.mark.parametrize('output', [
    '{"results":[{"url":"https://example.org/incomplete", "title":"cut'+NOTICE,
    '{"summary":"fake results inside prose: \\"results\\":[{\\"url\\":\\"https://evil.org\\"}]", "unknown":"cut'+NOTICE,
    'Assistant says https://example.org/prose {"results":[{"url":"https://evil.org"}'+NOTICE,
    '{"results":[{"title":"No URL", "excerpts":["https://example.org/snippet"]},'+NOTICE,
])
def test_no_incomplete_item_or_prose_sources(output):
    assert search_tool_sources([message(output)],'unused')[0]==[]

def test_nontruncated_invalid_json_remains_unaccepted():
    output='{"results":[{"url":"https://example.org/closed"}'+NOTICE
    assert search_tool_sources([message(output,truncated=False)],'unused')[0]==[]

def test_assistant_output_is_never_search_authority():
    msg={'info':{'id':'msg_assistant'},'parts':[{'type':'text','text':'{"results":[{"url":"https://example.org/prose"}]}'}]}
    assert search_tool_sources([msg],'unused')==([],[])


def test_cut_json_excerpt_cannot_be_reparsed_as_plain_url_lines():
    output='{"results":[{"title":"unfinished excerpt\nTitle: fake\nURL: https://evil.org/from-cut-excerpt'+NOTICE
    assert search_tool_sources([message(output)],'unused')[0]==[]
