"""T architecture-selection regression; positive proof from more than two real receipts."""
import copy
import hashlib

import pytest

from street_story.identity_architectural_pool import (
    _excerpt, prepare_architectural_pool, close_architectural_pool_response)
from test_architectural_text_identity import text_inputs


def _article(aid, text):
    return {'article_id':aid,
        'url':'https://archive.example/physical-building',
        'title':'Publisher architecture', 'scope':'Observed physical building',
        'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'raw_body_sha256_verified':True,'input_kind':'acquired_article_text',
        'text':text}


def _three_documents():
    story,candidates,decision,receipt=text_inputs()
    first=receipt['articles'][0]
    second=_article('catalog:neighbor-one',
        'The building is historically important but the article describes no visible facade.')
    third=_article('catalog:neighbor-two',
        'An alternative building is on a different historic street and has an unrelated roof.')
    receipt['articles']=[second,third,first]
    return story,candidates,decision,receipt


def _closed_answer(decision,ids):
    return {**copy.deepcopy(decision), 'article_comparisons':[{
        'article_id':cid,
        'visual_fit':'distinctive_match' if cid==decision['article_bindings'][0]['article_id'] else 'generic_only',
        'architectural_difference':'Observed bay and cornice combination differs from neighboring buildings.',
        'visible_SOURCE_specifics':'The SOURCE has an individual central bay and aligned axes.'}
        for cid in ids]}


def test_architecture_excerpt_preserves_verbatim_actual_passages_and_later_renovation():
    introductory='Дом имеет историческую ценность. '*85
    material='Фасад: три оси окон и прямоугольный эркер на верхних этажах. '
    mutable='При реставрации 2010 года часть декора восстановлена. '
    body=introductory+'\n'+material*17+'\n'+mutable*12
    assert len(body)>3800
    segments=_excerpt(body,max_chars=2100)
    assert sum(len(x['text']) for x in segments)<=2100
    assert all(body[x['start']:x['end']]==x['text'] for x in segments)
    joined='\n'.join(x['text'] for x in segments)
    assert 'фасад' in joined.casefold()
    assert 'реставрации' in joined.casefold()
    assert len(joined)<len(body)
    assert segments[0]['start']==0


def test_three_actual_articles_reach_one_contrastive_model_call_and_correct_third_can_win():
    story,candidates,decision,receipt=_three_documents()
    packet=prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    assert packet['article_ids']==[
        'catalog:neighbor-one','catalog:neighbor-two','catalog:physical-building']
    assert packet['input_contract']=='source-multiple-architecture-pool-v1'
    assert packet['text_utf8_bytes']<16000
    assert 'source_quote' in str(packet['schema'])
    assert packet['schema']['properties']['article_comparisons']['minItems']==3
    output=_closed_answer(decision,packet['article_ids'])
    final=close_architectural_pool_response(story,candidates,packet,output,
        source_text_receipt=receipt)
    assert final['accepted'] is True
    proof=final['proof']
    assert proof['candidate_id']=='osm:way:7'
    assert [article['article_id'] for article in proof['source_text_receipt']['articles']] == [
        'catalog:physical-building']
    assert proof['source_text_receipt']['articles'][0]['raw_body_sha256_verified'] is True
    assert final['evidence_model_response_sha256']


def test_incomplete_contrast_and_wrong_article_quote_cannot_yield_identity():
    story,candidates,decision,receipt=_three_documents()
    packet=prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    good=_closed_answer(decision,packet['article_ids'])
    partial=copy.deepcopy(good)
    partial['article_comparisons'].pop()
    with pytest.raises(ValueError,match='model_architectural_pool_response_malformed'):
        close_architectural_pool_response(story,candidates,packet,partial,source_text_receipt=receipt)
    duplicate=copy.deepcopy(good)
    duplicate['article_comparisons'][0]['article_id']=duplicate['article_comparisons'][1]['article_id']
    with pytest.raises(ValueError,match='unassessed_real_publisher_article'):
        close_architectural_pool_response(story,candidates,packet,duplicate,source_text_receipt=receipt)
    missing_support=copy.deepcopy(good)
    missing_support['article_comparisons'][-1]['visual_fit']='generic_only'
    result=close_architectural_pool_response(story,candidates,packet,missing_support,
        source_text_receipt=receipt)
    assert result['accepted'] is False
    assert result['reason']=='positive_binding_not_supported_by_model_contrast'
    invented=copy.deepcopy(good)
    invented['correspondences'][0]['source_quote']='Never observed invented quote'
    assert close_architectural_pool_response(story,candidates,packet,invented,
        source_text_receipt=receipt)['accepted'] is False


def test_verified_architecture_pool_never_silently_drops_excess_articles():
    story,candidates,decision,receipt=_three_documents()
    receipt['articles']=[_article(f'catalog:{i}',f'Building {i} has a visible portal.')
        for i in range(9)]
    with pytest.raises(ValueError,match='bounded_original_SOURCE'):
        prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    receipt['articles'][:]=receipt['articles'][:8]
    packet=prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    assert len(packet['article_ids'])==8
    assert len(packet['checked_articles'])==8
    assert packet['max_article_excerpts']==8


def test_bad_article_hash_or_unobserved_physical_id_stops_before_any_model_call():
    story,candidates,decision,receipt=_three_documents()
    receipt['articles'][0]['text_sha256']='0'*64
    with pytest.raises(ValueError,match='unverified_article'):
        prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:7'])
    receipt['articles'][0]['text_sha256']=hashlib.sha256(receipt['articles'][0]['text'].encode()).hexdigest()
    with pytest.raises(ValueError,match='unobserved_physical_candidate'):
        prepare_architectural_pool(story,candidates,receipt,candidate_ids=['osm:way:999'])
