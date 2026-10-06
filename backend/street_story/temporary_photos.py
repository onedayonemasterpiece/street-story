"""Bounded, process-local photo transport. No image files or content digests."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict


class TemporaryPhotos:
    def __init__(self, *, capacity_bytes=96 * 1024 * 1024, idle_seconds=1800, clock=time.monotonic):
        self.capacity_bytes = capacity_bytes
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._photos = OrderedDict()
        self._lock = threading.RLock()

    def _expire(self):
        now = self.clock()
        for key, (_, touched) in list(self._photos.items()):
            if now - touched >= self.idle_seconds:
                self._photos.pop(key, None)

    def put(self, story_id: str, data: bytes):
        if not data or len(data) > self.capacity_bytes:
            raise ValueError('photo exceeds temporary transport capacity')
        with self._lock:
            self._expire()
            self._photos.pop(story_id, None)
            while self._photos and sum(len(value[0]) for value in self._photos.values()) + len(data) > self.capacity_bytes:
                self._photos.popitem(last=False)
            self._photos[story_id] = (data, self.clock())

    def get(self, story_id: str) -> bytes | None:
        with self._lock:
            self._expire()
            item = self._photos.pop(story_id, None)
            if item is None:
                return None
            self._photos[story_id] = (item[0], self.clock())
            return item[0]

    def discard(self, story_id: str):
        with self._lock:
            self._photos.pop(story_id, None)
