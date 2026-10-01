from street_story.mvp_research import MvpResearchStreetStoryService


def osm_item(index: int, distance: float, *, bucket="landmark", salience=2, name=None):
    return {
        "type": "way",
        "id": index,
        "distance_m": distance,
        "selection_bucket": bucket,
        "salience_rank": salience,
        "tags": {"name": name or f"Объект {index}", "building": "yes"},
    }


def test_dense_shortlist_keeps_independent_distance_bands_and_is_bounded():
    nearby = []
    # Dense noise close to the camera.
    for i in range(1, 101):
        nearby.append(osm_item(i, 20 + i, bucket="nearby", salience=3))

    # Strong landmarks in each visibility band, including a far gate that pure
    # nearest-N ranking would discard behind the 100 closer objects.
    nearby.extend([
        osm_item(201, 90, salience=0, name="Ближний памятник"),
        osm_item(202, 310, salience=0, name="Средние ворота"),
        osm_item(203, 520, salience=0, name="Дальние ворота"),
    ])
    osm = {
        "reverse": {
            "osm_type": "way",
            "osm_id": 999,
            "display_name": "Дом у точки съёмки",
            "distance_m": 0.0,
            "selection_bucket": "reverse",
            "salience_rank": -1,
        },
        "nearby": nearby,
    }
    wikipedia = [{
        "pageid": 77,
        "title": "Энциклопедический объект",
        "url": "https://ru.wikipedia.org/wiki/Test",
        "extract": "",
        "distance_m": 650.0,
        "thumbnail_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a/Test.jpg/640px-Test.jpg",
    }]

    shortlist = MvpResearchStreetStoryService._candidate_catalog(osm, wikipedia)

    assert len(shortlist) <= 16
    names = {item["name"] for item in shortlist}
    assert "Дом у точки съёмки" in names
    assert "Ближний памятник" in names
    assert "Средние ворота" in names
    assert "Дальние ворота" in names
    assert "Энциклопедический объект" in names
    assert {item["shortlist_bucket"] for item in shortlist} >= {
        "reverse", "nearby", "landmark_near", "landmark_mid", "landmark_far", "wikipedia"
    }


def test_route_and_administrative_relations_do_not_enter_shortlist():
    osm = {
        "reverse": {},
        "nearby": [
            {
                "type": "relation",
                "id": 1,
                "distance_m": 30,
                "selection_bucket": "landmark",
                "salience_rank": 0,
                "tags": {"name": "Трамвай № 3", "route": "tram", "wikidata": "Q1"},
            },
            {
                "type": "relation",
                "id": 2,
                "distance_m": 40,
                "selection_bucket": "landmark",
                "salience_rank": 0,
                "tags": {"name": "Центральный район", "boundary": "administrative", "wikidata": "Q2"},
            },
            osm_item(3, 100, salience=0, name="Исторические ворота"),
        ],
    }

    shortlist = MvpResearchStreetStoryService._candidate_catalog(osm, [])

    assert [item["name"] for item in shortlist] == ["Исторические ворота"]
