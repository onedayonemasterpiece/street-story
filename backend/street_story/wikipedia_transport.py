"""One endpoint Retry-After gate for Wikimedia metadata HTTP reads."""
import hashlib
import json
import math
from email.utils import parsedate_to_datetime

from .errors import RetryableProviderError


def _key(endpoint):
    # Keep the preexisting nearby gate key shared by direct-title reads.
    encoded = json.dumps(endpoint, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return 'wikipedia-nearby-cooldown-v1:' + hashlib.sha256(encoded.encode()).hexdigest()


def check_cooldown(store, endpoint, *, role='mapped'):
    cooldown = store.cache_get(_key(endpoint))
    if cooldown and cooldown.get('retry_at', 0) > store.now():
        raise RetryableProviderError(f'wikipedia_{role}_retry_after', retry_at=cooldown['retry_at'])


def check_response(store, endpoint, response, *, role='mapped'):
    if response.status_code == 429:
        value = response.headers.get('Retry-After', '')
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - store.now()
            except (ValueError, TypeError, OverflowError):
                delay = 60
        if not math.isfinite(delay):
            delay = 60
        delay = max(1, delay)
        retry_at = store.now() + delay
        store.cache_put(_key(endpoint), {'retry_at': retry_at}, delay)
        raise RetryableProviderError(f'wikipedia_{role}_http_429', retry_at=retry_at)
    response.raise_for_status()
