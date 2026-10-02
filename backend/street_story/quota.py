"""Mandatory DevCoveer reserve -> mark_sent -> finalize gate; never fail open.

SQLite holds only an accounting recovery journal. Shared google_ai_* RPCs are
quota authority. No provider request is permitted by local accounting alone.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
import uuid

import httpx

from .config import reveal
from .gemini import GeminiUnavailable


class SharedQuotaDenied(Exception):
    code = 429

    def __init__(self, delay):
        super().__init__('shared_quota_denied')
        self.details = {'retryDelay': str(delay)+'s'}


class SharedQuotaGate:
    def __init__(self, settings, pool, *, http=None):
        self.settings, self.pool, self.store, self.http = settings, pool, pool.store, http
        self.url = (settings.gemini_quota_supabase_url or '').rstrip('/')
        self.token = settings.gemini_quota_supabase_key
        # Resolve registered names by secret equality, also for legacy single-key
        # and JSON-list configuration. Never send key values to the controller.
        self.env_keys = {name: hashlib.sha256(value.strip().encode()).hexdigest()
                         for name, value in os.environ.items()
                         if (name in settings.gemini_key_refs or name.startswith(('GOOGLE_API_KEY', 'GEMINI_API_KEY'))) and value.strip()
                         and name not in ('GEMINI_API_KEYS', 'GEMINI_API_KEY_REFS')}
        self.registry = {}
        self.refresh_at = 0
        self.lock = asyncio.Lock()
        self.active = set()

    def unavailable(self):
        self.pool.event('shared_control_unavailable', 'shared_model')
        return GeminiUnavailable(self.pool.clock()+30, 'shared_control_unavailable')

    async def request(self, method, path, **kwargs):
        if not self.url.startswith('https://') or not reveal(self.token):
            raise self.unavailable()
        own = self.http is None
        client = self.http or httpx.AsyncClient(timeout=3, follow_redirects=False)
        try:
            async with asyncio.timeout(4):
                response = await client.request(method, self.url+'/rest/v1/'+path,
                    headers={'apikey':reveal(self.token), 'Authorization':'Bearer '+reveal(self.token)}, **kwargs)
                response.raise_for_status()
                return response.json() if response.content else None
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise self.unavailable() from None
        finally:
            if own:
                await client.aclose()

    async def rpc(self, name, payload):
        return await self.request('POST', 'rpc/google_ai_'+name, json=payload)

    async def registered_id(self, key):
        now = self.pool.clock()
        if now >= self.refresh_at:
            rows = await self.request('GET', 'google_ai_api_keys', params={'select':'id,env_var_name,is_active','limit':'1000'})
            if not isinstance(rows, list) or len(rows) >= 1000:
                raise self.unavailable()
            registry = {}
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('is_active'), bool):
                    raise self.unavailable()
                if row['is_active'] and row.get('env_var_name') in self.env_keys:
                    try:
                        identifier = str(uuid.UUID(row['id']))
                    except (ValueError, TypeError, KeyError):
                        raise self.unavailable() from None
                    registry[self.env_keys[row['env_var_name']]] = identifier
            self.registry, self.refresh_at = registry, now+30
        identifier = self.registry.get(hashlib.sha256(key.encode()).hexdigest())
        if identifier is None:
            raise SharedQuotaDenied(300)
        return identifier

    @staticmethod
    def final_payload(uid, response=None, category='interrupted', duration=0):
        usage = getattr(response, 'usage_metadata', None)
        def token(name):
            value = getattr(usage, name, None)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        return dict(p_request_uid=uid, p_attempt_no=1,
                    p_usage_input_tokens=token('prompt_token_count'), p_usage_output_tokens=token('candidates_token_count'),
                    p_usage_total_tokens=token('total_token_count'), p_duration_ms=max(0, int(duration)),
                    p_provider_status='success' if response is not None else category,
                    p_error_type=None if response is not None else category,
                    p_error_code=None, p_error_message=None)

    def queue_finalize(self, uid, payload):
        with self.store.tx() as db:
            db.execute("UPDATE gemini_quota_journal SET state='finalize',finalize_json=? WHERE request_uid=?",
                       (json.dumps(payload), uid))

    async def receipt(self, uid):
        rows = await self.request('GET', 'google_ai_requests', params={
            'select':'request_uid,sent_at,finalized_at','request_uid':'eq.'+uid,'limit':'2'})
        if not isinstance(rows, list) or len(rows)>1:
            raise self.unavailable()
        if rows and (not isinstance(rows[0], dict) or rows[0].get('request_uid') != uid
                     or not {'sent_at','finalized_at'} <= rows[0].keys()):
            raise self.unavailable()
        return rows[0] if rows else None

    async def finalize(self, uid, payload):
        await self.rpc('finalize', payload)
        receipt = await self.receipt(uid)
        if receipt is None or not receipt['finalized_at']:
            raise self.unavailable()
        with self.store.tx() as db:
            db.execute('DELETE FROM gemini_quota_journal WHERE request_uid=?', (uid,))

    async def recover(self):
        # One process owns a DATA_DIR; separate resident services have separate DBs.
        async with self.lock:
            with self.store.connection() as db:
                rows = list(db.execute('SELECT * FROM gemini_quota_journal ORDER BY created_at LIMIT 64'))
            for row in rows:
                uid = row['request_uid']
                if row['state'] == 'finalize':
                    await self.finalize(uid, json.loads(row['finalize_json']))
                elif row['deadline'] <= self.pool.clock():
                    # Unknown reserve/sent outcome: read back, NEVER reserve again.
                    receipt = await self.receipt(uid)
                    if receipt is None or receipt['finalized_at']:
                        with self.store.tx() as db:
                            db.execute('DELETE FROM gemini_quota_journal WHERE request_uid=?', (uid,))
                    else:
                        payload = self.final_payload(uid)
                        self.queue_finalize(uid, payload)
                        await self.finalize(uid, payload)
                elif row['state'] == 'uncertain' or uid not in self.active:
                    raise self.unavailable()

    async def run(self, key, timeout, reserved_tpm, call):
        await self.recover()
        identifier = await self.registered_id(key)
        uid = str(uuid.uuid4())
        now = self.pool.clock()
        # Intent exists before remote mutation. Uncertain response cannot trigger
        # another reservation until reconciled. RPM/RPD remain charged on failure.
        with self.store.tx() as db:
            db.execute('INSERT INTO gemini_quota_journal VALUES(?,?,?,?,?)',
                       (uid,'active',now+timeout+60,None,now))
        self.active.add(uid)
        try:
            result = await self.rpc('reserve', dict(p_request_uid=uid,p_attempt_no=1,p_consumer='street-story',
                p_account_name='street-story',p_model=self.pool.model,p_reserved_tpm=reserved_tpm,p_candidate_key_ids=[identifier]))
            if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
                raise self.unavailable()
            if not result['ok']:
                with self.store.tx() as db:
                    db.execute('DELETE FROM gemini_quota_journal WHERE request_uid=?', (uid,))
                reason = result.get('blocked_reason')
                delay = result.get('retry_after_ms')
                if reason not in ('rpm','tpm','rpd','no_keys','model_not_found'):
                    raise self.unavailable()
                delay = delay/1000 if isinstance(delay,(int,float)) and math.isfinite(delay) and delay>=0 else 60
                if reason == 'rpd':
                    delay = max(delay,(int(now//86400)+1)*86400-now)
                delay = max(1,min(delay,86400))
                self.pool.apply_advisory({hashlib.sha256(key.encode()).hexdigest(): (now+delay, 1.0)})
                self.pool.event('shared_quota_denied', 'shared_model', reason=reason, retry_after=delay)
                raise SharedQuotaDenied(delay)
            if result.get('api_key_id') != identifier:
                raise self.unavailable()
            await self.rpc('mark_sent', dict(p_request_uid=uid,p_attempt_no=1))
            receipt = await self.receipt(uid)
            if not receipt or not receipt['sent_at'] or receipt['finalized_at']:
                raise self.unavailable()
        except BaseException:
            self.active.discard(uid)
            with self.store.tx() as db:
                db.execute("UPDATE gemini_quota_journal SET state='uncertain' WHERE request_uid=?", (uid,))
            raise
        started = time.monotonic()
        response = None
        category = 'cancelled'
        try:
            response = await call()
            return response
        except Exception as exc:
            from .gemini import classify_error
            category = classify_error(exc, now=self.pool.clock()).category
            raise
        finally:
            # Commit outbox BEFORE networking, including cancellation. Missing
            # usage stays NULL so controller conservatively keeps reserved TPM.
            payload = self.final_payload(uid,response,category,(time.monotonic()-started)*1000)
            self.queue_finalize(uid,payload)
            self.active.discard(uid)
            # No network await after successful response: timeout/cancellation
            # must not discard paid work. Worker/next call drains durable outbox.
