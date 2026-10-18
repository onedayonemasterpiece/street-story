"""Article metadata follows direct REF addresses, independently of image bytes."""
from types import SimpleNamespace
import httpx
import pytest
from street_story.identity_lifecycle import visual_match
from street_story.identity_references import reference_images, thumbnail_reference
from street_story.identity_subject_binding import bind_reference_subject

ARTICLE='https://de.wikipedia.org/wiki/Example_gate'
ORIGINAL='https://upload.wikimedia.org/wikipedia/commons/e/e0/Example_gate.jpg'
THUMBNAIL=thumbnail_reference(ORIGINAL)
PHYSICAL={'candidate_id':'osm:way:123','name':'Physical gate'}

def candidate(article=ARTICLE,**descriptor):
    return {'candidate_id':'web:illustrated-article','reference_id':'ref-article','url':article,
        'discovery':'wikipedia_article_media','reference_image_urls':[ORIGINAL],
        'article_media':[{'article_url':article,'image_url':ORIGINAL,'kind':'img',
                         'figcaption':'A historical view',**descriptor}]}

def completed_verdict(**values):
    return {'status':'match','candidate_id':'web:illustrated-article','confidence':.91,
        'reference_subject_candidate_id':PHYSICAL['candidate_id'],'observations':['Same arch and distinctive brick openings.'],
        'alternative_candidate_ids':[],'_references_sent':['web:illustrated-article'],
        '_reference_ids_sent':['ref-article'],**values}

@pytest.mark.asyncio
async def test_direct_wikipedia_reference_preserves_article_metadata_and_subject_gate():
    selected=candidate(candidate_id='spoofed',source_url='https://evil.invalid/image',model_image_sha256='obsolete')
    evidence=[]
    images=await reference_images(SimpleNamespace(),[selected],evidence=evidence)
    assert images==[('web:illustrated-article','image/jpeg',ORIGINAL)]
    receipt=evidence[0]
    assert receipt['candidate_id']==selected['candidate_id'] and receipt['source_url']==ORIGINAL
    assert receipt['article_url']==ARTICLE and receipt['figcaption']=='A historical view'
    assert not any('sha' in key for key in receipt)
    bound=bind_reference_subject(completed_verdict(),[selected],[PHYSICAL],evidence)
    assert bound['status']=='bound' and visual_match(bound['result'],[selected],[PHYSICAL])
    competitor={'candidate_id':'osm:way:456','name':'Other gate'}
    conflicting=bind_reference_subject(completed_verdict(alternative_candidate_ids=[competitor['candidate_id']]),
        [selected],[PHYSICAL,competitor],evidence)
    assert not visual_match(conflicting['result'],[selected],[PHYSICAL,competitor])

@pytest.mark.asyncio
async def test_same_public_image_keeps_each_current_article_context_without_image_cache():
    service=SimpleNamespace()
    for article in [ARTICLE,'https://de.wikipedia.org/wiki/Another_article']:
        evidence=[]
        selected=candidate(article)
        assert (await reference_images(service,[selected],evidence=evidence))[0][2]==ORIGINAL
        assert evidence[0]['article_url']==article and evidence[0]['source_url']==ORIGINAL
        assert bind_reference_subject(completed_verdict(),[selected],[PHYSICAL],evidence)['status']=='bound'
    assert not hasattr(service,'_identity_reference_cache')

@pytest.mark.asyncio
@pytest.mark.parametrize('alteration',[{'article_url':'https://de.wikipedia.org/wiki/Other'},
    {'image_url':'https://upload.wikimedia.org/other.jpg'}])
async def test_unmatched_extraction_cannot_fabricate_article_binding(alteration):
    evidence=[]
    selected=candidate(**alteration)
    assert await reference_images(SimpleNamespace(),[selected],evidence=evidence)
    assert 'article_url' not in evidence[0] and evidence[0]['source_url']==ORIGINAL
    assert bind_reference_subject(completed_verdict(),[selected],[PHYSICAL],evidence)['reason']=='reference_provenance_missing'

@pytest.mark.asyncio
@pytest.mark.parametrize('status',[403,404,302])
async def test_selection_does_not_fetch_thumbnail_original_or_redirect(status):
    requests=[]
    async def forbidden(request):
        requests.append(str(request.url))
        return httpx.Response(status,headers={'location':THUMBNAIL})
    evidence=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        await reference_images(SimpleNamespace(),[candidate()],http=http,evidence=evidence)
    assert not requests and evidence[0]['source_url']==ORIGINAL
    assert evidence[0]['delivery']=='direct_public_url'
    assert 'resolved_image_url' not in evidence[0] and 'requested_image_url' not in evidence[0]
