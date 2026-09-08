"""Single-process, durable observed-health router. No secrets in persisted state/logs.

Provider failover is bounded and immediate. Waiting belongs to the durable job,
never to a provider hot loop. Quotas are project-based at Google; local accounting
is not an assertion of remaining provider quota or a distributed rate limiter.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from threading import RLock
from typing import Awaitable, Callable, TypeVar

import httpx
from pydantic import SecretStr, ValidationError

from .db import Store
from .errors import MalformedProviderResponse, PermanentProviderError, RetryableProviderError

logger = logging.getLogger('uvicorn.error.street_story.gemini')
OPERATIONS = ('transcription', 'grounded_research')
T = TypeVar('T')


@dataclass(frozen=True)
class GeminiPolicy:
    call_timeout: float = 20
    attempt_timeout: float = 60
    max_failover_keys: int = 32
    rate_cooldown: float = 60
    rate_cooldown_cap: float = 3600
    transient_cooldown: float = 5
    transcription_rpm: int = 0
    grounded_research_rpm: int = 0


@dataclass(frozen=True)
class Failure:
    category: str
    code: int | None = None
    retry_after: float | None = None
    permanent: bool = False
    disable_key: bool = False


class GeminiUnavailable(RetryableProviderError):
    def __init__(self, retry_at: float, reason: str = 'all_keys_unavailable'):
        # Closed-vocabulary reason only, never the provider exception string.
        super().__init__('gemini:' + reason, retry_at=retry_at)


def _retry_after(error: Exception, now: float) -> float | None:
    delays: list[float] = []
    response = getattr(error, 'response', None)
    headers = getattr(response, 'headers', {}) or {}
    raw = headers.get('Retry-After') or headers.get('retry-after')
    if raw is not None:
        try:
            delays.append(float(raw))
        except (ValueError, TypeError):
            try:
                delays.append(parsedate_to_datetime(str(raw)).timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass

    def visit(data, depth=0):
        if depth > 5:
            return
        if isinstance(data, list):
            for item in data[:16]:
                visit(item, depth+1)
        elif isinstance(data, dict):
            raw_delay = data.get('retryDelay', data.get('retry_delay'))
            if raw_delay is not None:
                try:
                    delay = (float(raw_delay.get('seconds', 0)) + float(raw_delay.get('nanos', 0))/1e9) if isinstance(raw_delay, dict) else float(str(raw_delay).removesuffix('s'))
                    delays.append(delay)
                except (ValueError, TypeError, OverflowError):
                    pass
            for key in ('error', 'details'):
                if key in data:
                    visit(data[key], depth+1)

    visit(getattr(error, 'details', None))
    valid = [min(d, 7*86400) for d in delays if math.isfinite(d) and d >= 0]
    return max(valid) if valid else None


def classify_error(error: Exception, *, now: float) -> Failure:
    code = getattr(error, 'code', None) or getattr(getattr(error, 'response', None), 'status_code', None)
    try:
        code = int(code) if code is not None else None
    except (ValueError, TypeError):
        code = None
    status = str(getattr(error, 'status', '') or '').upper()
    # Used for recognition only; never returned, persisted, or logged.
    message = str(error)[:4096].lower()
    retry = _retry_after(error, now)
    if code == 429 or status == 'RESOURCE_EXHAUSTED' or 'resource_exhausted' in message:
        return Failure('quota_exhausted' if 'quota' in message else 'rate_limited', 429, retry)
    if code in (401, 403) or 'api_key_invalid' in message or 'api key not valid' in message or status == 'UNAUTHENTICATED':
        return Failure('auth_invalid', code, disable_key=True)
    if code in (408, 504) or status == 'DEADLINE_EXCEEDED' or isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return Failure('timeout', code, retry)
    if code is not None and 500 <= code <= 599 or status in ('UNAVAILABLE', 'INTERNAL', 'ABORTED'):
        return Failure('provider_overload', code, retry)
    if isinstance(error, (httpx.TransportError, ConnectionError, OSError)):
        return Failure('network', code, retry)
    if isinstance(error, MalformedProviderResponse):
        return Failure('malformed_response', code, retry)
    if code == 404 or status == 'NOT_FOUND':
        return Failure('unsupported_model', code, permanent=True)
    if code is not None and 400 <= code <= 499 or status in ('INVALID_ARGUMENT', 'FAILED_PRECONDITION') or isinstance(error, (ValidationError, ValueError, TypeError)):
        return Failure('invalid_request', code, permanent=True)
    if isinstance(error, PermanentProviderError):
        return Failure('invalid_request', code, permanent=True)
    # Unknown SDK/provider exceptions are conservatively retryable, but bounded.
    return Failure('sdk_transient', code, retry)


class GeminiKeyPool:
    def __init__(self, store: Store, keys: tuple[SecretStr, ...], model: str, *, policy: GeminiPolicy | None = None, clock: Callable[[], float] = time.time):
        self.store, self.keys, self.model = store, tuple(dict.fromkeys(keys)), model
        self.policy, self.clock = policy or GeminiPolicy(), clock
        if len(self.keys) > 32:
            raise ValueError('Gemini supports at most 32 key slots')
        self.ids = tuple(hashlib.sha256(k.get_secret_value().encode()).hexdigest() for k in self.keys)
        self._in_flight: dict[str, int] = {key_id: 0 for key_id in self.ids}
        self._lock = RLock()
        with self.store.tx() as db:
            for key_id in self.ids:
                db.execute('INSERT OR IGNORE INTO gemini_credentials(key_id) VALUES(?)', (key_id,))
                for operation in OPERATIONS:
                    db.execute('INSERT OR IGNORE INTO gemini_key_health(key_id,model,operation) VALUES(?,?,?)', (key_id, model, operation))

    def _rows(self, db, operation):
        rows = db.execute('SELECT h.*,c.disabled,c.busy_until FROM gemini_key_health h JOIN gemini_credentials c USING(key_id) WHERE h.model=? AND h.operation=?', (self.model, operation))
        return [dict(row) for row in rows if row['key_id'] in self._in_flight]

    def _eligible_at(self, row, operation, now):
        limit = getattr(self.policy, operation+'_rpm')
        minute_end = row['minute_bucket']+60 if limit and row['minute_bucket'] == int(now//60)*60 and row['minute_used'] >= limit else 0
        return max(row['cooldown_until'], row['busy_until'], row['advisory_until'], minute_end)

    def reserve(self, operation: str, excluded: set[str], timeout: float):
        if operation not in OPERATIONS:
            raise ValueError('Unknown Gemini operation class')
        now = self.clock()
        with self._lock, self.store.tx() as db:
            rows = [r for r in self._rows(db, operation) if not r['disabled'] and r['key_id'] not in excluded and not self._in_flight[r['key_id']] and self._eligible_at(r, operation, now) <= now]
            if not rows:
                return None
            bucket = int(now//60)*60
            row = min(rows, key=lambda r: (r['consecutive_failures'], r['advisory_load'] if now-r['advisory_observed_at'] < 60 else 0, r['minute_used'] if r['minute_bucket'] == bucket else 0, r['last_selected'], self.ids.index(r['key_id'])))
            key_id = row['key_id']
            db.execute('UPDATE gemini_credentials SET busy_until=? WHERE key_id=?', (now+timeout+5, key_id))
            db.execute('UPDATE gemini_key_health SET minute_bucket=?,minute_used=?,last_selected=? WHERE key_id=? AND model=? AND operation=?',
                       (bucket, (row['minute_used'] if row['minute_bucket'] == bucket else 0)+1, now, key_id, self.model, operation))
            self._in_flight[key_id] += 1
            return key_id

    def finish(self, key_id: str, operation: str, failure: Failure | None):
        now = self.clock()
        with self._lock, self.store.tx() as db:
            row = dict(db.execute('SELECT * FROM gemini_key_health WHERE key_id=? AND model=? AND operation=?', (key_id, self.model, operation)).fetchone())
            if failure is None:
                db.execute("UPDATE gemini_key_health SET cooldown_until=0,consecutive_failures=0,last_success=?,quota_state='available',retry_after=NULL,last_failure=NULL WHERE key_id=? AND model=? AND operation=?", (now, key_id, self.model, operation))
            elif failure.disable_key:
                db.execute("UPDATE gemini_credentials SET disabled=1,disabled_reason='auth_invalid' WHERE key_id=?", (key_id,))
            elif not failure.permanent:
                count = row['consecutive_failures'] + 1
                rate = failure.code == 429
                delay = min(self.policy.rate_cooldown_cap if rate else 60, (self.policy.rate_cooldown if rate else self.policy.transient_cooldown)*2**min(count-1, 10))
                delay = max(1, delay, failure.retry_after or 0)
                metadata = json.dumps({'category': failure.category, 'code': failure.code}, separators=(',', ':'))
                db.execute('UPDATE gemini_key_health SET cooldown_until=?,consecutive_failures=?,quota_state=?,retry_after=?,last_failure=? WHERE key_id=? AND model=? AND operation=?',
                           (now+delay, count, failure.category, failure.retry_after, metadata, key_id, self.model, operation))
            db.execute('UPDATE gemini_credentials SET busy_until=0 WHERE key_id=?', (key_id,))
            self._in_flight[key_id] = max(0, self._in_flight[key_id]-1)

    def unavailable(self, operation: str) -> GeminiUnavailable:
        now = self.clock()
        with self._lock, self.store.connection() as db:
            rows = [r for r in self._rows(db, operation) if not r['disabled']]
            when = min((self._eligible_at(r, operation, now) for r in rows), default=now+300)
        reason = 'no_configured_keys' if not self.keys else 'all_keys_disabled' if not rows else 'all_keys_unavailable'
        self.event('all_keys_unavailable', operation, reason=reason)
        return GeminiUnavailable(max(now+1, when), reason)

    def snapshot(self, operation: str = 'grounded_research') -> dict[str, int]:
        now = self.clock()
        with self._lock, self.store.connection() as db:
            rows = self._rows(db, operation)
            return {'configured_keys': len(self.keys), 'healthy_keys': sum(not r['disabled'] and self._eligible_at(r, operation, now) <= now for r in rows),
                    'cooling_down_keys': sum(not r['disabled'] and max(r['cooldown_until'], r['advisory_until']) > now for r in rows),
                    'disabled_keys': sum(bool(r['disabled']) for r in rows), 'in_flight': sum(self._in_flight.values())}

    def apply_advisory(self, observations: dict[str, tuple[float, float]]):
        # Known model-level exhaustion affects both workloads, not a credential ban.
        with self._lock, self.store.tx() as db:
            for key_id, (until, utilization) in observations.items():
                if key_id in self.ids:
                    db.execute('UPDATE gemini_key_health SET advisory_until=?,advisory_load=?,advisory_observed_at=? WHERE key_id=? AND model=?',
                               (until, utilization, self.clock(), key_id, self.model))

    def event(self, event: str, operation: str, key_id: str | None = None, **fields):
        record = {'event': 'gemini.'+event, 'operation': operation, **fields}
        if key_id:
            record.update(key_id=key_id[:12], slot=self.ids.index(key_id)+1)
        logger.info('%s', json.dumps(record, sort_keys=True))


class GeminiExecutor:
    def __init__(self, pool: GeminiKeyPool):
        self.pool = pool

    async def execute(self, operation: str, call: Callable[[str, float], Awaitable[T]]) -> T:
        started = time.monotonic()
        attempted: set[str] = set()
        while len(attempted) < min(len(self.pool.keys), self.pool.policy.max_failover_keys):
            remaining = self.pool.policy.attempt_timeout - (time.monotonic()-started)
            if remaining <= 0:
                break
            timeout = min(remaining, self.pool.policy.call_timeout)
            key_id = self.pool.reserve(operation, attempted, timeout)
            if key_id is None:
                break
            attempted.add(key_id)
            self.pool.event('selected', operation, key_id, failover_count=len(attempted)-1)
            call_started = time.monotonic()
            try:
                async with asyncio.timeout(timeout):
                    result = await call(self.pool.keys[self.pool.ids.index(key_id)].get_secret_value(), timeout)
            except asyncio.CancelledError:
                self.pool.finish(key_id, operation, Failure('cancelled'))
                raise
            except GeminiUnavailable:
                self.pool.finish(key_id, operation, Failure('shared_control_unavailable'))
                raise
            except Exception as exc:
                failure = classify_error(exc, now=self.pool.clock())
                self.pool.finish(key_id, operation, failure)
                self.pool.event('failure', operation, key_id, category=failure.category, code=failure.code, retry_after=failure.retry_after,
                                cooldown=not (failure.permanent or failure.disable_key), disabled=failure.disable_key,
                                latency_ms=round((time.monotonic()-call_started)*1000), failover_count=len(attempted)-1)
                if failure.permanent:
                    raise PermanentProviderError('gemini:'+failure.category) from None
                continue
            self.pool.finish(key_id, operation, None)
            self.pool.event('success', operation, key_id, latency_ms=round((time.monotonic()-call_started)*1000), failover_count=len(attempted)-1)
            return result
        raise self.pool.unavailable(operation) from None
