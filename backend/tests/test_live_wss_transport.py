import importlib.util
import json
from pathlib import Path
import queue
import struct
import time

import httpx
import pytest


def module():
    path = Path(__file__).resolve().parents[1] / 'tools/live_wss_transport.py'
    spec = importlib.util.spec_from_file_location('live_wss_canary_transport', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Socket:
    def __init__(self):
        self.frames = queue.Queue()
        self.sent = []

    def send(self, value):
        message = json.loads(value)
        self.sent.append(message)
        if message['type'] == 'hello':
            self.frames.put(json.dumps({'type': 'hello_ack', 'protocol': 'wl-live-v1', 'connection_generation': 1}))
        if message['type'] == 'input':
            self.frames.put(json.dumps({'type': 'event', 'event': {'type': 'output_transcript', 'seq': 1, 'text': 'reply'}}))
            self.frames.put(struct.pack('!III', 0x574c4f31, 2, 24000) + b'\x01\x00\x02\x00')
            self.frames.put(json.dumps({'type': 'event', 'event': {'type': 'turn_complete', 'seq': 3}}))

    def recv(self, timeout):
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None

    def close(self):
        pass


def test_existing_harness_live_calls_use_socket_not_http(monkeypatch):
    mod = module()
    seen, sockets = [], []
    root = '/v1/stories/story_fixture/live-sessions'
    session_root = root + '/live_fixture'
    peer = Socket()

    def connect(url, **kwargs):
        assert url == 'wss://street.example' + session_root + '/socket'
        assert kwargs['origin'] == 'https://street.example'
        assert kwargs['subprotocols'] == ['wl-live-v1', 'wl-ticket.' + 'a' * 32]
        sockets.append(url)
        return peer

    def handler(request):
        seen.append((request.method, request.url.path))
        assert request.headers['Authorization'] == 'Bearer fixture-device'
        assert not request.url.path.endswith(('/input', '/events'))
        if request.url.path == root:
            return httpx.Response(200, json={'session_id': 'live_fixture', 'model': 'fixture',
                'transport_protocol': 'wl-live-v1', 'socket_ticket': 'a' * 32,
                'socket_url': session_root + '/socket', 'attempt_id': 'attempt_test'})
        return httpx.Response(200, json={'ok': True})

    monkeypatch.setattr(mod, 'connect', connect)
    with mod.WssCanaryClient(base_url='https://street.example', headers={'Authorization': 'Bearer fixture-device'},
                            transport=httpx.MockTransport(handler)) as client:
        client.post(root).raise_for_status()
        client.post(session_root + '/input', json={'text': 'test'}).raise_for_status()
        deadline = time.monotonic() + 1
        payload = {}
        while time.monotonic() < deadline:
            payload = client.get(session_root + '/events', params={'after': 0}).json()
            if payload['cursor'] == 3:
                break
            time.sleep(0.01)
        assert [event['type'] for event in payload['events']] == ['output_transcript', 'audio', 'turn_complete']
        assert client.metrics['output_pcm_bytes'] == 4
        assert client.metrics['hello_ack'] is True
        assert client.metrics['http_event_polls'] == 0
        assert client.metrics['http_audio_requests'] == 0
        client.post(session_root + '/stop').raise_for_status()
    assert len(sockets) == 1
    assert seen == [('POST', root), ('POST', session_root + '/stop')]


def test_socket_origin_mismatch_fails_before_ticket_disclosure(monkeypatch):
    mod = module()
    root = '/v1/stories/story_fixture/live-sessions'
    def handler(request):
        if request.url.path == root:
            return httpx.Response(200, json={'session_id': 'live_fixture', 'transport_protocol': 'wl-live-v1',
                'socket_ticket': 'a' * 32, 'socket_url': 'https://another.invalid/socket', 'attempt_id': 'attempt_test'})
        return httpx.Response(200, json={'ok': True})
    monkeypatch.setattr(mod, 'connect', lambda *_args, **_kwargs: pytest.fail('ticket must not reach a foreign origin'))
    with mod.WssCanaryClient(base_url='https://street.example', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(mod.WssCanaryError, match='wss_origin_mismatch'):
            client.post(root)
