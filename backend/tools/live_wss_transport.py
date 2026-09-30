"""WSS adapter for the existing product acceptance harness, not a new harness.

Domain preparation/assertions stay in devcoveer_live_product_smoke. Only Live
input/events use WSS. Ordinary product readback remains HTTPS. No direct model
API, credential selection or alternate product engine is introduced.
"""
from __future__ import annotations

import json
import struct
import threading
import time
from collections import deque
from urllib.parse import urljoin, urlsplit

import httpx
from websockets.sync.client import connect


class WssCanaryError(RuntimeError):
    pass


class WssCanaryClient:
    def __init__(self, **kwargs):
        self.http = httpx.Client(**kwargs)
        self.base_url = str(self.http.base_url).rstrip('/')
        self.ws = None
        self.session_id = ''
        self.live_root = ''
        self.events_buffer = deque(maxlen=1024)
        self.lock = threading.Lock()
        self.stop_reader = threading.Event()
        self.reader = None
        self.failure = None
        self.closed = False
        self.last_seq = 0
        self.metrics = {'transport': 'wss', 'hello_ack': False, 'http_audio_requests': 0,
                        'http_event_polls': 0, 'output_pcm_bytes': 0, 'pushed_events': 0}

    def __enter__(self):
        self.http.__enter__()
        return self

    def __exit__(self, *args):
        self._close_socket()
        return self.http.__exit__(*args)

    def _close_socket(self):
        self.stop_reader.set()
        if self.ws is not None:
            self.ws.close()
        if self.reader is not None and self.reader is not threading.current_thread():
            self.reader.join(timeout=2)
        self.ws = None

    def _start_socket(self, started):
        if started.get('transport_protocol') != 'wl-live-v1':
            raise WssCanaryError('wss_required')
        target = urlsplit(urljoin(self.base_url + '/', started['socket_url']))
        base = urlsplit(self.base_url)
        if target.scheme != 'https' or target.netloc != base.netloc or target.query or target.fragment or target.username:
            raise WssCanaryError('wss_origin_mismatch')
        self.session_id = started['session_id']
        self.live_root = target.path.removesuffix('/socket')
        self.ws = connect('wss://' + target.netloc + target.path,
                          origin='https://' + base.netloc,
                          subprotocols=['wl-live-v1', 'wl-ticket.' + started['socket_ticket']],
                          open_timeout=10, close_timeout=2, max_size=1_000_000, max_queue=32)
        self.ws.send(json.dumps({'type': 'hello', 'protocol': 'wl-live-v1',
                                 'attempt_id': started['attempt_id'], 'cursor': 0, 'connection_generation': 1}))
        hello = json.loads(self.ws.recv(timeout=3))
        if hello.get('type') != 'hello_ack' or hello.get('protocol') != 'wl-live-v1' or hello.get('connection_generation') != 1:
            raise WssCanaryError('wss_hello_invalid')
        self.metrics['hello_ack'] = True
        self.reader = threading.Thread(target=self._read, name='wss-canary-receiver', daemon=True)
        self.reader.start()

    def _read(self):
        next_ping = time.monotonic() + 10
        try:
            while not self.stop_reader.is_set():
                if time.monotonic() >= next_ping:
                    self.ws.send('{"type":"ping"}')
                    next_ping = time.monotonic() + 10
                try:
                    frame = self.ws.recv(timeout=0.25)
                except TimeoutError:
                    continue
                if isinstance(frame, bytes):
                    if len(frame) < 14 or len(frame) % 2:
                        raise WssCanaryError('wss_output_frame')
                    magic, seq, rate = struct.unpack('!III', frame[:12])
                    if magic != 0x574c4f31 or not 8000 <= rate <= 96000:
                        raise WssCanaryError('wss_output_frame')
                    with self.lock:
                        self.metrics['output_pcm_bytes'] += len(frame) - 12
                    event = {'type': 'audio', 'seq': seq, 'mime_type': f'audio/pcm;rate={rate}'}
                else:
                    message = json.loads(frame)
                    if message.get('type') != 'event':
                        continue
                    event = message['event']
                with self.lock:
                    seq = int(event.get('seq') or 0)
                    if seq <= self.last_seq:
                        continue
                    self.last_seq = seq
                    self.events_buffer.append(event)
                    self.metrics['pushed_events'] += 1
                    if event.get('type') == 'closed':
                        self.closed = True
        except Exception as exc:
            if not self.stop_reader.is_set():
                self.failure = str(exc) if isinstance(exc, WssCanaryError) else 'wss_receive_failed'

    def post(self, path, **kwargs):
        if self.live_root and path == self.live_root + '/input':
            if self.failure:
                raise WssCanaryError(self.failure)
            body = kwargs.get('json')
            if not isinstance(body, dict) or 'audio_base64' in body:
                raise WssCanaryError('use_native_acceptance_for_audio_input')
            self.ws.send(json.dumps({'type': 'input', 'message': body}, ensure_ascii=False))
            return httpx.Response(200, request=httpx.Request('POST', self.base_url + path),
                                  json={'ok': True, 'session_id': self.session_id})
        response = self.http.post(path, **kwargs)
        if path.endswith('/live-sessions') and response.is_success:
            started = response.json()
            try:
                self._start_socket(started)
            except Exception as exc:
                self._close_socket()
                self.http.post(path + '/' + started['session_id'] + '/stop')
                code = str(exc) if isinstance(exc, WssCanaryError) else 'wss_start_' + type(exc).__name__
                raise WssCanaryError(code) from None
        if self.live_root and path == self.live_root + '/stop':
            self._close_socket()
        return response

    def get(self, path, **kwargs):
        if self.live_root and path == self.live_root + '/events':
            if self.failure and not self.closed:
                raise WssCanaryError(self.failure)
            after = int((kwargs.get('params') or {}).get('after', 0))
            with self.lock:
                events = list(self.events_buffer)
                page = [event for event in events if event['seq'] > after][:64]
                cursor = page[-1]['seq'] if page else after
                payload = {'session_id': self.session_id, 'events': page, 'cursor': cursor,
                           'has_more': any(event['seq'] > cursor for event in events),
                           'gap': bool(events and after < events[0]['seq'] - 1), 'closed': self.closed}
            return httpx.Response(200, request=httpx.Request('GET', self.base_url + path), json=payload)
        return self.http.get(path, **kwargs)
