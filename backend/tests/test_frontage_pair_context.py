"""Real OSM paired-body review is conditional, never physical identification."""
from street_story.identity_frontage_context import adjacent_model_nomination_pair
from test_original_osm_geometry_regression import story_for


def test_real_106_adjacent_alternative_from_model_pool_is_exposed_not_promoted():
    story=story_for(106)
    # Input pair is from a hypothetical visual model. This is a unit test,
    # NOT evidence the actual model selected the correct photographed house.
    subject='osm:way:150596903'
    options=['osm:way:150596899','osm:way:102519047']
    result=adjacent_model_nomination_pair(story,[],subject,options)
    assert result is not None
    assert result['physical_subject_candidate_id']==subject
    assert result['adjacent_alternative_candidate_id']=='osm:way:150596899'
    assert result['observed_osm_boundary_gap_m']<1.
    assert result['physical_passage_verified'] is False
    assert result['authorizes_identity'] is False


def test_missing_or_far_model_alternatives_keep_scope_unresolved():
    story=story_for(106)
    assert adjacent_model_nomination_pair(story,[],'osm:way:150596903',[]) is None
    assert adjacent_model_nomination_pair(story,[],'osm:way:100659323',
       ['osm:way:150596899']) is None
    assert adjacent_model_nomination_pair(story,[],'osm:way:missing',
       ['osm:way:150596899']) is None
