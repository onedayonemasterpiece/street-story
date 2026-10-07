import pytest
from street_story.headless_identity import VERDICT_SCHEMA
from street_story.errors import RetryableProviderError
from test_native_vision import setup


@pytest.mark.asyncio
async def test_empty_failed_readback_then_original_completed_does_not_close_or_resend(tmp_path):
    provider,client,_,story,context,receipts,sends,finalized=setup(tmp_path)
    original=client.request
    reads=0
    async def request(method,params,timeout=30):
        nonlocal reads
        result=await original(method,params,timeout)
        if method=='thread/read':
            reads+=1
            if reads==1:
                result['thread']['turns'][0].update(status='failed',error=None)
        return result
    client.request=request
    client.cached_status=lambda tid:(None,{'id':'turn_'+tid,'status':'completed'})
    result=await provider.compare_visual(None,story,VERDICT_SCHEMA,context,{'attempt_id':'actual-readback-race'})
    assert result['receipt']['phase']=='completed' and result['result']['status']=='mismatch'
    assert reads==2 and len(sends)==1
    assert sum(method=='turn/start'for method,_ in client.calls)==1
    pending=[r for r in receipts if r.get('terminal_readback_pending')]
    assert pending[0]['phase']=='submitted' and pending[0]['terminal_readback_pending']['notification_status']=='completed'
    assert not any(r['phase']=='failed'for r in receipts)
    assert finalized[-1][1]=='completed'


@pytest.mark.asyncio
async def test_persistent_empty_failed_projection_keeps_original_unknown_no_refund(tmp_path):
    provider,client,_,story,context,receipts,sends,finalized=setup(tmp_path)
    client.turn_status='failed'
    provider.timeout=.025
    with pytest.raises(RetryableProviderError,match='outcome_unknown'):
        await provider.compare_visual(None,story,VERDICT_SCHEMA,context,{'attempt_id':'projection-unresolved'})
    assert receipts[-1]['phase']in{'submitted','unknown'}
    assert receipts[-1]['thread_id'] and receipts[-1]['turn_id']
    assert len(sends)==1 and sum(m=='turn/start'for m,_ in client.calls)==1
    assert finalized[-1][1]=='unknown'
    assert not any(r['phase']=='failed'for r in receipts)


@pytest.mark.asyncio
async def test_real_failed_with_explicit_error_still_closes_original(tmp_path):
    provider,client,_,story,context,receipts,sends,finalized=setup(tmp_path)
    original=client.request
    async def request(method,params,timeout=30):
        result=await original(method,params,timeout)
        if method=='thread/read':
            result['thread']['turns'][0].update(status='failed',error={'message':'Actual provider rejection','codexErrorInfo':'unauthorized'})
        return result
    client.request=request
    with pytest.raises(RetryableProviderError,match='native_turn_failed'):
        await provider.compare_visual(None,story,VERDICT_SCHEMA,context,{'attempt_id':'true-failure'})
    assert receipts[-1]['phase']=='failed' and receipts[-1]['turn_error']['code']=='unauthorized'
    assert len(sends)==1 and finalized[-1][1]=='completed'
