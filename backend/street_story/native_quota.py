"""Short-lived permission for the owner's native Codex reserve.

Provider quota is checked when issuing a grant, independently of per-attempt
resource admission. A grant never buys credits or redeems rate-limit resets.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
from pathlib import Path

from .errors import RetryableProviderError

GRANT_SECONDS = 15 * 60
MIN_REMAINING_PERCENT = 3


def credential_stamp(home: Path = Path('/home/dev/.codex')) -> str:
    """Fingerprint the selected credential binding without reading its secret."""
    resolved = home.resolve(strict=True)
    stat = (resolved / 'auth.json').stat()
    return hashlib.sha256(f'{resolved}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}'.encode()).hexdigest()


def quota_snapshot(reply, *, now=None):
    if not isinstance(reply, dict) or reply.get('ordinaryUsageAllowed') is False:
        return None
    limits = reply.get('rateLimits')
    scoped = (reply.get('rateLimitsByLimitId') or {}).get('codex')
    if scoped is not None:
        limits = scoped
    if (not isinstance(limits, dict) or limits.get('spendControlReached') is True
            or limits.get('rateLimitReachedType') is not None):
        return None
    if limits.get('limitId') not in (None, 'codex'):
        return None
    windows = []
    for name in ('primary', 'secondary', 'individualLimit'):
        window = limits.get(name)
        if window is None:
            continue
        if not isinstance(window, dict):
            return None
        used = window.get('usedPercent')
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
            return None
        reset = window.get('resetsAt')
        if reset is not None and (isinstance(reset, bool) or not isinstance(reset, (int, float))
                                  or not math.isfinite(reset) or (now is not None and reset <= now)):
            return None
        windows.append({'window': name, 'remaining_percent': 100 - used, 'resets_at': reset})
    account = reply.get('accountId')
    if not windows or not isinstance(account, str) or not account:
        return None
    return {'account_hash': hashlib.sha256(account.encode()).hexdigest(), 'windows': windows,
            'remaining_percent': min(window['remaining_percent'] for window in windows)}


class NativeQuotaPermission:
    def __init__(self, store, *, binding_stamp=credential_stamp):
        self.store, self.binding_stamp = store, binding_stamp
        self.lock = asyncio.Lock()

    def _key(self, stamp):
        return 'native-luna-quota-permission-v1:' + stamp

    async def ensure(self, client):
        async with self.lock:
            try:
                stamp = self.binding_stamp()
            except OSError:
                raise RetryableProviderError('native_quota_binding_unavailable', retry_at=self.store.now() + 60) from None
            key, now = self._key(stamp), self.store.now()
            grant = self.store.cache_get(key) or {}
            if (grant.get('binding_stamp') == stamp and grant.get('model') == 'gpt-6-luna'
                    and grant.get('issued_at', now + 1) <= now < grant.get('expires_at', 0)
                    and grant.get('remaining_percent', 0) >= MIN_REMAINING_PERCENT):
                return grant
            try:
                reply = await client.request('account/rateLimits/read', {})
            except Exception:
                raise RetryableProviderError('native_quota_unknown', retry_at=now + 60) from None
            # A credential replacement during the RPC cannot grant permission
            # to the newly selected account from the old account's answer.
            try:
                if self.binding_stamp() != stamp:
                    raise RetryableProviderError('native_quota_binding_changed', retry_at=now + 1)
            except OSError:
                raise RetryableProviderError('native_quota_binding_unavailable', retry_at=now + 60) from None
            snapshot = quota_snapshot(reply, now=now)
            if snapshot is None:
                raise RetryableProviderError('native_quota_unknown', retry_at=now + 60)
            if snapshot['remaining_percent'] < MIN_REMAINING_PERCENT:
                future = [w['resets_at'] for w in snapshot['windows']
                          if isinstance(w['resets_at'], (int, float)) and w['resets_at'] > now]
                raise RetryableProviderError('native_quota_below_reserve', retry_at=min(future) if future else now + GRANT_SECONDS)
            grant = {**snapshot, 'binding_stamp': stamp, 'model': 'gpt-6-luna',
                     'issued_at': now, 'expires_at': now + GRANT_SECONDS}
            self.store.cache_put(key, grant, GRANT_SECONDS)
            return grant

    def invalidate(self, grant, reason):
        stamp = grant.get('binding_stamp')
        if not stamp:
            try:
                stamp = self.binding_stamp()
            except OSError:
                return
        if stamp:
            self.store.cache_put(self._key(stamp), {'expires_at': 0, 'invalidated_reason': reason}, GRANT_SECONDS)
