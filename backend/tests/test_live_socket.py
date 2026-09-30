from __future__ import annotations

import base64
import json
import struct

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from live_interaction import LiveSocketSessionHost

from street_story.app import create_app
from street_story.config import reveal
from street_story.live import StreetStoryLiveAdapter
from test_live_editor import make_service


class Provider:
    def __init__(self):
        self.inputs = []

    async def run(self, *, load_key, reader, on_event):
        assert load_key() == 'fixture-key-not-a-provider'
        self.inputs.append(json.loads(await reader.readline()))
        on_event({'type': 'ready', 'model': 'gemini-3.8-live'})
        while raw := await reader.readline():
            value = json.loads(raw)
            self.inputs.append(value)
            if value['type'] == 'stop':
                return
            if value['type'] == 'text':
                on_event({'type': 'output_transcript', 'text': 'fixture reply'})
                on_event({'type': 'audio', 'data': base64.b64encode(b'\x01\x00\x02\x00').decode(), 'mime_type': 'audio/pcm;rate=24000'})
                on_event({'type': 'turn_complete'})
            if value['type'] == 'activity_end':
                on_event({'type': 'input_timing', 'activity_end_sent_at': 1})


@pytest.fixture
def client(tmp_path, monkeypatch):
    svc, _, session, _ = make_service(tmp_path)
    provider = Provider()
    host = LiveSocketSessionHost(
        adapter_factory=lambda **hooks: StreetStoryLiveAdapter(svc, hooks['emit']),
        key_resolver=lambda *_: 'fixture-key-not-a-provider', provider_run=provider.run,
        ready_timeout_ms=500,
    )
    monkeypatch.setattr('street_story.app.create_live_host', lambda *_: host)
    app = create_app(settings=svc.settings, service=svc)
    with TestClient(app) as test_client:
        test_client.headers['Authorization'] = 'Bearer ' + reveal(svc.settings.device_token)
        yield test_client, session.resource_id, provider, host


def start(client):
    c, story_id, _, _ = client
    response = c.post(f'/v1/stories/{story_id}/live-sessions', json={'transport': 'wss', 'attempt_id': 'attempt_contract'})
    assert response.status_code == 200, response.text
    return response.json()


def socket(c, value, origin='http://testserver', path=None):
    return c.websocket_connect(path or value['socket_url'], subprotocols=['wl-live-v1', 'wl-ticket.' + value['socket_ticket']], headers={'Origin': origin})


def hello(ws, value):
    ws.send_json({'type': 'hello', 'protocol': 'wl-live-v1', 'attempt_id': value['attempt_id'], 'connection_generation': 1, 'cursor': 0})
    assert ws.receive_json()['type'] == 'hello_ack'


def test_wss_push_binary_pcm_and_original_auth_boundary(client):
    c, story, provider, host = client
    denied = c.post(f'/v1/stories/{story}/live-sessions', json={}, headers={'Authorization': ''})
    assert denied.status_code == 401
    value = start(client)
    assert value['transport_protocol'] == 'wl-live-v1'
    assert '?' not in value['socket_url']
    with socket(c, value) as ws:
        hello(ws, value)
        ws.send_json({'type': 'input', 'message': {'activity_start': True}})
        ws.send_bytes(struct.pack('!III', 0x574c4131, 1, 12) + b'\x01\x00\x02\x00')
        ack = None
        for _ in range(12):
            message = ws.receive_json()
            if message['type'] == 'audio_ack':
                ack = message
                break
        assert ack and ack['seq'] == 1
        ws.send_json({'type': 'input', 'message': {'activity_end': True}})
        ws.send_json({'type': 'input', 'message': {'text': 'reply'}})
        binary = None
        transcript = False
        for _ in range(15):
            frame = ws.receive()
            if frame.get('bytes'):
                binary = frame['bytes']
                break
            event = json.loads(frame['text']).get('event', {})
            transcript |= event.get('type') == 'output_transcript'
        assert transcript and binary is not None
        assert struct.unpack('!III', binary[:12])[::2] == (0x574c4f31, 24000)
        assert binary[12:] == b'\x01\x00\x02\x00'
        rejected = c.post(f"/v1/stories/{story}/live-sessions/{value['session_id']}/input", json={'text': 'never fallback'})
        assert rejected.status_code == 409
        assert rejected.json()['error']['code'] == 'live_transport_mismatch'
        ws.send_json({'type': 'stop'})
        for _ in range(20):
            if ws.receive()['type'] == 'websocket.close':
                break
        else:
            pytest.fail('WSS Stop did not close the socket')
    c.post(f"/v1/stories/{story}/live-sessions/{value['session_id']}/stop")
    assert host.size() == 0
    assert [v['type'] for v in provider.inputs][:5] == ['start', 'activity_start', 'audio', 'activity_end', 'text']


def test_origin_query_and_resource_rejected_before_ticket_is_used(client):
    c, story, _, _ = client
    value = start(client)
    for origin, path in [('https://evil.invalid', value['socket_url']), ('http://testserver', value['socket_url'] + '?ticket=forbidden'),
                         ('http://testserver', value['socket_url'].replace(story, 'story_other'))]:
        with pytest.raises(WebSocketDisconnect):
            with socket(c, value, origin=origin, path=path):
                pass
    with socket(c, value) as ws:
        hello(ws, value)
        ws.send_json({'type': 'stop'})


def test_bootstrap_and_ticket_renewal_are_bounded_and_authenticated(client):
    c, story, _, _ = client
    base = f'/v1/stories/{story}/live-sessions'
    assert c.post(base, content='x' * 4097).status_code == 413
    assert c.post(base, json={'model': 'not-server-selected'}).status_code == 400
    value = start(client)
    url = f"{base}/{value['session_id']}/socket-ticket"
    assert c.post(url, headers={'Authorization': ''}).status_code == 401
    renewed = c.post(url).json()
    assert renewed['socket_ticket'] != value['socket_ticket']
    with pytest.raises(WebSocketDisconnect):
        with socket(c, value):
            pass
    with socket(c, {**value, **renewed}) as ws:
        hello(ws, value)
        ws.send_json({'type': 'stop'})
