"""The house's 2GIS card pages (organizations, visitor reviews) as owned research sources."""
from street_story import map_pages
from test_map_links import tower


def test_page_urls_name_the_tab_and_reviews_are_recognised():
    urls = map_pages.page_urls(tower())
    assert set(urls) == {'organizations', 'reviews'}
    assert urls['reviews'].startswith('https://2gis.ru/geo/') and urls['reviews'].endswith('#reviews')
    assert map_pages.from_visitor_reviews(urls['reviews'])
    assert not map_pages.from_visitor_reviews(urls['organizations'])
    assert not map_pages.from_visitor_reviews('https://example.org/history#reviews-of-a-book')
    assert map_pages.page_urls({}) == {}


def test_card_tab_follows_the_resolved_firm_or_building_card():
    assert (map_pages._tab_url('https://2gis.ru/kaliningrad/firm/5630027815197073/20.49%2C54.72?m=1', 'reviews')
            == 'https://2gis.ru/kaliningrad/firm/5630027815197073/tab/reviews')
    assert (map_pages._tab_url('https://2gis.ru/kaliningrad/geo/5630135189374621/20.48%2C54.72', 'inside')
            == 'https://2gis.ru/kaliningrad/geo/5630135189374621/tab/inside')
    assert map_pages._tab_url('https://2gis.ru/kaliningrad?m=20.4%2C54.7%2F18', 'reviews') is None


def test_reviews_source_says_it_is_people_opinion():
    assert 'мнения и впечатления людей' in map_pages.HEADERS['reviews']
    assert 'не проверенная история' in map_pages.HEADERS['reviews']


def test_a_hung_read_is_cut_by_the_hard_limit(monkeypatch):
    import asyncio
    import pytest

    async def hang(url, budget):
        await asyncio.sleep(3600)
    monkeypatch.setattr(map_pages, '_capture', hang)
    with pytest.raises(ValueError, match='map_page_timeout'):
        asyncio.run(map_pages.capture('https://2gis.ru/geo/1#reviews', budget_seconds=-119.9))
