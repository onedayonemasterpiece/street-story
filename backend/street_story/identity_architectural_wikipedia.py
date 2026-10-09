"""On-demand Wikipedia architectural text when a first publisher body lacks
enough individual SOURCE-visible features.

A generic T source supplier, not a building recognizer: accepts only the
literal title of a genuinely acquired publisher article (or an already
observed model-nominated title). Reads the resolved public encyclopedia page,
stores exact raw bytes and provenance, and returns an *unaccepted* article.

The caller must still give SOURCE pixels, observed OSM candidates, all
material alternatives and the acquired Wikipedia body to its SOURCE+TEXT LLM.
Neither Wikipedia title nor address is automatically treated as identity.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import unicodedata
from urllib.parse import urlencode, urlsplit, quote

import httpx

from .article_media import fetch_public, resolve_public

_ALLOWED = {'ru', 'de', 'en', 'pl'}
_MAX_RAW_BYTES = 2_000_000
_MAX_MODEL_CHARS = 12_000
_TTL_SECONDS = 7 * 86400


def _query(title, language):
    if not isinstance(language,str) or language not in _ALLOWED:
        raise ValueError('unsupported_wikipedia_language')
    if not isinstance(title,str):
        raise ValueError('missing_observed_publisher_title')
    title=unicodedata.normalize('NFC',title.strip())
    if (not title or len(title)>180 or any(ord(c)<32 for c in title)):
        raise ValueError('invalid_observed_publisher_title')
    # This is an exact title lookup (NOT model source ranking, title fuzzy
    # similarity, a hardcoded correct name, or a search over all Wikipedia).
    domain=f'{language}.wikipedia.org'
    url=f'https://{domain}/w/api.php?'+urlencode({
        'action':'query','format':'json','formatversion':'2',
        'prop':'extracts','explaintext':'1','redirects':'1','titles':title})
    return title,domain,url



def wikipedia_title_choice_schema(received_search):
    """The LLM chooses only a publisher-returned MediaWiki page ID or null."""
    if not isinstance(received_search,dict) or received_search.get('status') not in (
            'completed','completed_empty'):
        raise ValueError('no_closed_wikipedia_title_search')
    ids=[item['pageid'] for item in received_search.get('results') or []
        if isinstance(item,dict) and type(item.get('pageid')) is int]
    return {'type':'object','properties':{
        'pageid':{'anyOf':[{'type':'integer','enum':ids},{'type':'null'}]},
        'reason':{'type':'string','maxLength':400},
        'title_fit':{'type':'string','enum':['same_subject','ambiguous','none']}},
        'required':['pageid','reason','title_fit'],'additionalProperties':False}


def _decode_article(raw, *, original_title, requested_url, final_url, domain):
    if urlsplit(final_url).hostname!=domain:
        raise ValueError('wikipedia_unexpected_final_domain')
    if len(raw)>_MAX_RAW_BYTES:
        raise ValueError('wikipedia_raw_body_size')
    value=json.loads(raw)
    if not isinstance(value,dict) or not isinstance(value.get('query'),dict):
        raise ValueError('wikipedia_query_payload_missing')
    pages=value['query'].get('pages')
    if not isinstance(pages,list) or len(pages)!=1:
        raise ValueError('wikipedia_query_unexpected_page_count')
    page=pages[0]
    if not isinstance(page,dict) or page.get('missing') is True or page.get('invalid') is True:
        return {'status':'completed_empty', 'article_id':None,
            'retrieval_reason':'wikipedia_exact_title_not_found'}
    pageid=page.get('pageid')
    title=page.get('title')
    body=page.get('extract')
    if (type(pageid) is not int or pageid<=0 or not isinstance(title,str)
            or not title.strip() or not isinstance(body,str)):
        raise ValueError('wikipedia_article_fields_invalid')
    if not body.strip():
        return {'status':'completed_empty','article_id':f'wiki:{pageid}',
            'retrieval_reason':'wikipedia_article_without_extract'}
    text=body[:_MAX_MODEL_CHARS]
    canonical=f'https://{domain}/wiki/'+quote(title.replace(' ','_'),safe='')
    return {'status':'completed',
        'article_id':f'wiki:{pageid}',
        'url':canonical,
        'canonical_url':canonical,
        'title':title,
        'lookup_title':original_title,
        'lookup_url':requested_url,
        'publisher_domain':domain,
        'source_sha256':hashlib.sha256(raw).hexdigest(),
        'raw_body_sha256_verified':True,
        'text':text,
        'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'input_kind':'acquired_article_text',
        'address':'',
        'address_provenance':'not_offered_by_wikipedia_exact_title_reader',
        'scope':'Encyclopedia article content; SOURCE/physical building link not inferred',
        'physical_identity_inferred':False,
        'body_truncated':len(body)>len(text)}


class ArchitecturalWikipediaReader:
    def __init__(self, store, http: httpx.AsyncClient, *, resolver=resolve_public):
        self.store=store
        self.http=http
        self.resolver=resolver

    async def article_by_observed_title(self, title, *, language='ru'):
        """One bounded HTTP GET, or SHA-verified cache hit, no model calls."""
        original,domain,url=_query(title,language)
        digest=hashlib.sha256(url.encode()).hexdigest()
        cache_key='wikipedia-architectural-exact-title-v1:'+digest
        start=time.monotonic()
        receipt={'status':'not_sent','url':url,'requested_title':original,
            'language':language,'query_sha256':digest,
            'cache_hit':False,'publisher_text_only_not_physical_identity':True}
        try:
            cached=self.store.cache_get(cache_key)
            raw=None
            final=url
            if isinstance(cached,dict):
                value=cached.get('body')
                if isinstance(value,str):
                    try:
                        raw=base64.b64decode(value,validate=True)
                    except (ValueError,TypeError):
                        raw=None
                if (raw is not None and cached.get('sha256')==hashlib.sha256(raw).hexdigest()
                        and cached.get('final_url')==url):
                    receipt['cache_hit']=True
                else:
                    raw=None
            if raw is None:
                receipt['status']='dispatch_intent'
                async def same_publisher_resolver(host):
                    # Reject external redirects before DNS/HTTP, not merely
                    # after fetching content from an unrelated publisher.
                    if host != domain:
                        raise ValueError('wikipedia_cross_publisher_redirect')
                    return await self.resolver(host)
                final,mime,raw=await fetch_public(
                    self.http,url,_MAX_RAW_BYTES,resolver=same_publisher_resolver)
                if mime not in {'application/json','text/json'}:
                    raise ValueError('wikipedia_unexpected_mime')
                if urlsplit(final).hostname!=domain:
                    raise ValueError('wikipedia_cross_publisher_redirect')
                if not raw:
                    raise ValueError('wikipedia_response_empty')
                self.store.cache_put(cache_key,{
                    'body':base64.b64encode(raw).decode(),
                    'sha256':hashlib.sha256(raw).hexdigest(),
                    'final_url':final,
                    'fetched_at':self.store.now()},_TTL_SECONDS)
            receipt.update(_decode_article(raw,original_title=original,
                requested_url=url,final_url=final,domain=domain))
            receipt.update(response_bytes=len(raw),
                raw_source_sha256=hashlib.sha256(raw).hexdigest())
        except httpx.HTTPStatusError as exc:
            receipt.update(status='transport_failed',
                http_status=exc.response.status_code,
                error_code=f'http_{exc.response.status_code}')
        except (httpx.RequestError,TimeoutError) as exc:
            receipt.update(status='transport_failed',error_code=type(exc).__name__)
        except (ValueError,TypeError,KeyError,UnicodeError,json.JSONDecodeError) as exc:
            receipt.update(status='parse_failed',error_code=str(exc)[:160])
        finally:
            receipt['elapsed_seconds']=round(time.monotonic()-start,3)
        return receipt


    async def search_observed_title(self, title, *, language='ru'):
        """Return publisher search candidates, not a guessed first article."""
        original,domain,_=_query(title,language)
        url=f'https://{domain}/w/api.php?'+urlencode({
            'action':'query','format':'json','formatversion':'2',
            'list':'search','srsearch':original,'srnamespace':'0',
            'srlimit':'6'})
        digest=hashlib.sha256(url.encode()).hexdigest()
        key='wikipedia-architectural-title-candidates-v1:'+digest
        start=time.monotonic()
        result={'status':'not_sent','requested_title':original,
            'language':language,'requested_url':url,'query_sha256':digest,
            'cache_hit':False,'results':[],'identity_inferred':False}
        try:
            cached=self.store.cache_get(key)
            raw=None
            if isinstance(cached,dict) and isinstance(cached.get('body'),str):
                try:
                    raw=base64.b64decode(cached['body'],validate=True)
                except (ValueError,TypeError):
                    raw=None
                if (raw is not None and cached.get('sha256')==hashlib.sha256(raw).hexdigest()
                        and cached.get('final_url')==url):
                    result['cache_hit']=True
                else:
                    raw=None
            if raw is None:
                async def same_publisher_resolver(host):
                    if host!=domain:
                        raise ValueError('wikipedia_cross_publisher_redirect')
                    return await self.resolver(host)
                result['status']='dispatch_intent'
                final,mime,raw=await fetch_public(
                    self.http,url,_MAX_RAW_BYTES,resolver=same_publisher_resolver)
                if mime not in {'application/json','text/json'} or urlsplit(final).hostname!=domain:
                    raise ValueError('wikipedia_search_origin_or_mime_mismatch')
                self.store.cache_put(key,{'body':base64.b64encode(raw).decode(),
                    'sha256':hashlib.sha256(raw).hexdigest(),'final_url':final,
                    'fetched_at':self.store.now()},_TTL_SECONDS)
            payload=json.loads(raw)
            entries=(payload.get('query') or {}).get('search') if isinstance(payload,dict) else None
            if not isinstance(entries,list):
                raise ValueError('wikipedia_search_rows_missing')
            cards=[]
            for item in entries[:6]:
                if not isinstance(item,dict):
                    continue
                pid=item.get('pageid')
                page_title=item.get('title')
                if (type(pid) is int and pid>0 and isinstance(page_title,str)
                        and page_title.strip() and pid not in [x['pageid'] for x in cards]):
                    cards.append({'pageid':pid,'title':page_title,
                        'canonical_url':f'https://{domain}/wiki/'+quote(
                            page_title.replace(' ','_'),safe='')})
            result.update(status='completed' if cards else 'completed_empty',
                results=cards,raw_source_sha256=hashlib.sha256(raw).hexdigest(),
                response_bytes=len(raw))
        except httpx.HTTPStatusError as exc:
            result.update(status='transport_failed',
                error_code=f'http_{exc.response.status_code}')
        except (httpx.RequestError,TimeoutError) as exc:
            result.update(status='transport_failed',error_code=type(exc).__name__)
        except (ValueError,TypeError,KeyError,UnicodeError,json.JSONDecodeError) as exc:
            result.update(status='parse_failed',error_code=str(exc)[:160])
        finally:
            result['elapsed_seconds']=round(time.monotonic()-start,3)
        return result

    async def article_by_model_selected_pageid(self, received_search, selected_pageid):
        """Read only a page from the real frozen title-candidate search list."""
        if (not isinstance(received_search,dict) or
                received_search.get('status')!='completed'
                or type(selected_pageid) is not int):
            raise ValueError('model_selected_wikipedia_page_not_received')
        cards=[row for row in received_search.get('results') or []
            if isinstance(row,dict) and row.get('pageid')==selected_pageid]
        if len(cards)!=1:
            raise ValueError('model_selected_wikipedia_page_not_received')
        return await self.article_by_observed_title(
            cards[0]['title'],language=received_search['language'])
