"""Measured original OSM shape supplier, selectively integrated from G PR250."""
import json
import math
from pathlib import Path

import pytest

from street_story.identity_shape_context import SHAPE_COLUMNS, observed_plan_shape

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/g_original_111_morphology.json').read_text())
OBJECTS = FIXTURE['received_objects']

def measured(item):
    return dict(zip(SHAPE_COLUMNS,observed_plan_shape(item)))


def test_real_111_shape_exposes_size_and_complexity_without_imaginary_height():
    way=measured(next(x for x in OBJECTS if x['type']=='way'))
    relation=measured(next(x for x in OBJECTS if x['type']=='relation'))
    assert way['status']=='observed_closed_outer'
    assert way['long_axis_m']==pytest.approx(47.79,abs=.15)
    assert way['short_axis_m']==pytest.approx(28.60,abs=.15)
    assert way['footprint_area_m2']==pytest.approx(963.6,abs=3)
    assert way['footprint_elongation']==pytest.approx(1.67,abs=.03)
    assert way['explicit_height_m'] is None
    assert way['observed_building_levels'] is None
    assert relation['status']=='observed_closed_outer'
    assert relation['long_axis_m']==pytest.approx(112.06,abs=.20)
    assert relation['short_axis_m']==pytest.approx(100.87,abs=.20)
    assert relation['footprint_area_m2']==pytest.approx(5821.3,abs=5)
    assert relation['footprint_area_m2']/way['footprint_area_m2']>6
    assert relation['explicit_height_m']==pytest.approx(49.64,abs=.01)
    assert relation['observed_building_levels']==12.
    assert relation['height_over_long_axis']==pytest.approx(.44,abs=.01)
    assert relation['concave_corner_count']>way['concave_corner_count']
    # Tall source silhouette must not fabricate a numeric height for the WAY.
    assert way['height_over_long_axis'] is None


@pytest.mark.parametrize('tags,expected',[
    ({'height':'49.64','building:levels':'12'},(49.64,12.)),
    ({'height':'50 m','building:levels':'7.5'},(50.,7.5)),
    ({'height':'160ft','building:levels':'many'},(None,None)),
    ({'height':'unknown','building:levels':'4;5'},(None,None)),
    ({'height':'-1','building:levels':'0'},(None,None)),
])
def test_height_and_floor_tags_are_not_fabricated(tags,expected):
    base=next(x for x in OBJECTS if x['type']=='way')
    val=measured({**base,'tags':tags})
    assert (val['explicit_height_m'],val['observed_building_levels'])==expected


def test_unclosed_and_disjoint_multipolygon_never_invents_one_large_building():
    base=next(x for x in OBJECTS if x['type']=='way')
    without_closure={**base,'geometry':base['geometry'][:-1]}
    assert measured(without_closure)['status']=='incomplete_or_excessive_outer_contour'
    both={**base,'members':[{'type':'way','ref':1,'role':'outer',
        'geometry':base['geometry']}]}
    assert measured(both)['status']=='multipart_or_missing_outer_contour'


@pytest.mark.parametrize('degrees',[0,31,77,123])
def test_oriented_rectangle_ratio_is_nearly_rotation_invariant(degrees):
    angle=math.radians(degrees)
    c,s=math.cos(angle),math.sin(angle)
    corner=[(-25,-5),(25,-5),(25,5),(-25,5),(-25,-5)]
    lat0,lon0=54.7,20.5
    pts=[{'lat':lat0+(x*s+y*c)/111195,
        'lon':lon0+(x*c-y*s)/(111195*math.cos(math.radians(lat0)))}
        for x,y in corner]
    val=measured({'type':'way','id':123,'tags':{'building':'yes'},
        'geometry':pts})
    assert val['status']=='observed_closed_outer'
    assert val['long_axis_m']==pytest.approx(50,abs=.5)
    assert val['short_axis_m']==pytest.approx(10,abs=.5)
    assert val['footprint_elongation']==pytest.approx(5,abs=.18)

