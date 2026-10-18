import asyncio
import copy

import pytest

from street_story.errors import RetryableProviderError
from street_story.native_quota import NativeQuotaPermission


class Store:
    def __init__(self):
        self.time, self.cache = 1000, {}
    def now(self):
        return self.time
    def cache_get(self, key):
        return self.cache.get(key)
    def cache_put(self, key, value, ttl):
        self.cache[key] = value


class Client:
    def __init__(self, used=50):
        self.calls = 0
        self.reply = {'accountId': 'test-account', 'ordinaryUsageAllowed': True,
                      'rateLimits': {'limitId': 'codex', 'spendControlReached': False,
                                     'primary': {'usedPercent': used, 'resetsAt': 9000}, 'secondary': None}}
    async def request(self, method, params):
        assert method == 'account/rateLimits/read' and params == {}
        self.calls += 1
        await asyncio.sleep(0)
        return copy.deepcopy(self.reply)


@pytest.mark.asyncio
async def test_each_native_send_rechecks_quota_even_before_snapshot_expiry():
    store, client = Store(), Client()
    permission = NativeQuotaPermission(store, binding_stamp=lambda: 'binding')
    first = await permission.ensure(client)
    store.time += 899
    second = await permission.ensure(client)
    assert second['issued_at'] == store.time
    assert client.calls == 2 and first['expires_at'] == 1900
    store.time += 1
    renewed = await permission.ensure(client)
    assert client.calls == 3 and renewed['expires_at'] == 2800
    assert 'test-account' not in str(store.cache)


@pytest.mark.asyncio
async def test_concurrent_calls_each_check_shared_account_and_credential_change_refreshes():
    store, client, stamp = Store(), Client(), ['first']
    permission = NativeQuotaPermission(store, binding_stamp=lambda: stamp[0])
    await asyncio.gather(permission.ensure(client), permission.ensure(client))
    assert client.calls == 2
    stamp[0] = 'second'
    client.reply['accountId'] = 'other-account'
    second = await permission.ensure(client)
    assert client.calls == 3 and second['binding_stamp'] == 'second'


@pytest.mark.asyncio
async def test_provider_quota_error_invalidates_permission_before_expiry():
    store, client = Store(), Client()
    permission = NativeQuotaPermission(store, binding_stamp=lambda: 'binding')
    grant = await permission.ensure(client)
    permission.invalidate(grant, 'provider_quota')
    client.reply['rateLimits']['primary']['usedPercent'] = 100
    with pytest.raises(RetryableProviderError, match='below_reserve') as exc:
        await permission.ensure(client)
    assert client.calls == 2 and exc.value.retry_at == 9000


@pytest.mark.parametrize('used', [True, -1, 101, float('nan'), float('inf'), '51', None])
@pytest.mark.asyncio
async def test_unknown_invalid_quota_does_not_issue_permission(used):
    permission = NativeQuotaPermission(Store(), binding_stamp=lambda: 'binding')
    with pytest.raises(RetryableProviderError, match='unknown'):
        await permission.ensure(Client(used))


@pytest.mark.asyncio
@pytest.mark.parametrize('used', [98, 99, 99.9, 100])
async def test_owner_three_percent_reserve_blocks_new_native_work(used):
    permission = NativeQuotaPermission(Store(), binding_stamp=lambda: 'binding')
    with pytest.raises(RetryableProviderError, match='below_reserve') as exc:
        await permission.ensure(Client(used))
    assert exc.value.retry_at == 9000


@pytest.mark.asyncio
async def test_exact_threshold_and_all_native_windows_are_checked():
    permission = NativeQuotaPermission(Store(), binding_stamp=lambda: 'binding')
    assert (await permission.ensure(Client(97)))['remaining_percent'] == 3
    permission = NativeQuotaPermission(Store(), binding_stamp=lambda: 'other')
    client = Client(20)
    client.reply['rateLimits']['secondary'] = {'usedPercent': 100, 'resetsAt': 3000}
    with pytest.raises(RetryableProviderError, match='below_reserve'):
        await permission.ensure(client)


@pytest.mark.asyncio
async def test_cached_grant_below_restored_owner_reserve_is_rechecked():
    store, client = Store(), Client()
    permission = NativeQuotaPermission(store, binding_stamp=lambda: 'binding')
    await permission.ensure(client)
    for grant in store.cache.values():
        grant['remaining_percent'] = 1
    client.reply['rateLimits']['primary']['usedPercent'] = 99
    with pytest.raises(RetryableProviderError, match='below_reserve'):
        await permission.ensure(client)
    assert client.calls == 2


@pytest.mark.parametrize('change', ['reached', 'expired', 'invalid_reset', 'no_account'])
@pytest.mark.asyncio
async def test_unusable_native_snapshot_cannot_issue_grant(change):
    store, client = Store(), Client()
    if change == 'reached':
        client.reply['rateLimits']['rateLimitReachedType'] = 'weekly'
    elif change == 'expired':
        client.reply['rateLimits']['primary']['resetsAt'] = store.now()
    elif change == 'invalid_reset':
        client.reply['rateLimits']['primary']['resetsAt'] = True
    else:
        client.reply['accountId'] = None
    permission = NativeQuotaPermission(store, binding_stamp=lambda: 'binding')
    with pytest.raises(RetryableProviderError, match='unknown'):
        await permission.ensure(client)
    assert not store.cache


@pytest.mark.asyncio
async def test_account_changed_while_quota_rpc_pending_does_not_grant():
    store, client, stamps = Store(), Client(), iter(['before', 'after'])
    permission = NativeQuotaPermission(store, binding_stamp=lambda: next(stamps))
    with pytest.raises(RetryableProviderError, match='binding_changed'):
        await permission.ensure(client)
    assert not store.cache


@pytest.mark.asyncio
async def test_luna_and_sol_check_one_shared_account_before_each_send():
    store, client = Store(), Client()
    permission = NativeQuotaPermission(store, binding_stamp=lambda: 'binding')
    luna = await permission.ensure(client)
    sol = await permission.ensure(client, model='gpt-6.1-sol')
    assert client.calls == 2 and len(store.cache) == 1
    assert luna['model'] == 'gpt-6-luna' and sol['model'] == 'gpt-6.1-sol'
    assert {k: v for k, v in luna.items() if k != 'model'} == {
        k: v for k, v in sol.items() if k != 'model'}
    assert next(iter(store.cache.values()))['model'] == 'gpt-6.1-sol'


@pytest.mark.asyncio
async def test_external_account_usage_below_reserve_blocks_next_turn_without_invalidation():
    store, client = Store(), Client()
    permission = NativeQuotaPermission(store, binding_stamp=lambda: 'same-account')
    healthy = await permission.ensure(client)
    assert healthy['remaining_percent'] == 50
    # Another product consumes the common account one second later; no product
    # invalidation callback has happened and the old snapshot has not expired.
    store.time += 1
    client.reply['rateLimits']['primary']['usedPercent'] = 99
    with pytest.raises(RetryableProviderError, match='below_reserve'):
        await permission.ensure(client)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_sol_quota_failure_blocks_luna_and_sol_on_the_same_binding():
    store, client = Store(), Client()
    permission = NativeQuotaPermission(store, binding_stamp=lambda: 'binding')
    grant = await permission.ensure(client, model='gpt-6.1-sol')
    permission.invalidate(grant, 'provider_quota')
    client.reply['rateLimits']['primary']['usedPercent'] = 100
    for model in ('gpt-6-luna', 'gpt-6.1-sol'):
        with pytest.raises(RetryableProviderError, match='below_reserve'):
            await permission.ensure(client, model=model)
    assert len(store.cache) == 1


@pytest.mark.asyncio
async def test_sol_grant_rechecks_a_changed_credential_and_all_quota_windows():
    store, client, binding = Store(), Client(), ['first']
    permission = NativeQuotaPermission(store, binding_stamp=lambda: binding[0])
    await permission.ensure(client, model='gpt-6.1-sol')
    binding[0] = 'second'
    client.reply['rateLimits']['secondary'] = {'usedPercent': 100, 'resetsAt': 3000}
    with pytest.raises(RetryableProviderError, match='below_reserve'):
        await permission.ensure(client, model='gpt-6-luna')
    assert client.calls == 2


@pytest.mark.asyncio
async def test_unknown_model_cannot_reuse_an_account_grant():
    store, client = Store(), Client()
    permission = NativeQuotaPermission(store, binding_stamp=lambda: 'binding')
    await permission.ensure(client)
    before = copy.deepcopy(store.cache)
    with pytest.raises(ValueError, match='model_not_permitted'):
        await permission.ensure(client, model='unapproved')
    assert client.calls == 1 and store.cache == before
