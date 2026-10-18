"""Literal owner-snapshot addresses reach planning and comparison, never identity."""

import copy
import json

import pytest

from street_story import owner_group_identity as owner
from street_story import live_visual_comparison as visual
from street_story.identity_discovery import _map_query_context, region_hint
from street_story.identity_model_context import physical_decision_context
from street_story.identity_source_selection import first_wave_catalog

def fixture(city="Калининград", other_city=None):
    def building(cid, x, address=None):
        url = "https://www.openstreetmap.org/way/" + cid.split(":")[-1]
        return {
            "candidate_id": cid,
            "name": "Здание " + cid,
            "name_provenance": "map.placeholder",
            "url": url,
            "map_coordinates": {"latitude": x, "longitude": 20},
            "map_object": {
                "provenance": "osm.tags",
                "source_url": url,
                "tags": {"building": "yes"},
                "building_entrances": {
                    "proof": "osm_closed_way_node_membership",
                    "source_url": url,
                    "candidate_ids": ["osm:node:22", "osm:node:24"] if cid == "osm:way:1" else [],
                },
            },
            "map_geometry": {
                "rings": [
                    {
                        "role": "outer",
                        "closed": True,
                        "points": [
                            {"lat": x, "lon": 20},
                            {"lat": x, "lon": 20.001},
                            {"lat": x + 0.001, "lon": 20.001},
                            {"lat": x + 0.001, "lon": 20},
                            {"lat": x, "lon": 20},
                        ],
                    }
                ]
            },
            "map_address": address or {},
            "search_anchor": {
                "city": other_city if cid == "osm:way:2" and other_city is not None else city,
                "source": "osm.observed_tags",
                "provenance": "osm.observed_tags",
            },
        }

    members = [
        building("osm:way:1", 54),
        building("osm:way:2", 54.002, {"street": "Другая улица", "house_number": "9", "provenance": "osm.tags"}),
    ]
    nodes = []
    for n, lon in [("22", 20), ("24", 20.001)]:
        url = "https://www.openstreetmap.org/node/" + n
        nodes.append(
            {
                "candidate_id": "osm:node:" + n,
                "name": "entrance",
                "url": url,
                "map_coordinates": {"latitude": 54, "longitude": lon},
                "map_object": {"tags": {"entrance": "staircase"}, "provenance": "osm.tags", "source_url": url},
                "map_address": {"street": "Житомирская улица", "house_number": n, "provenance": "osm.tags", "source_url": url},
                "search_anchor": {"city": city, "source": "osm.observed_tags", "provenance": "osm.observed_tags"},
            }
        )
    snapshot = {
        "snapshot_id": "map-1",
        "photo_sha256": "photo",
        "candidates": copy.deepcopy(members),
        "address_objects": nodes,
        "city_objects": copy.deepcopy(nodes),
        "city_reverse": {},
    }
    group = {
        "mode": "rough",
        "group_id": "group-1",
        "generation": 1,
        "photo_sha256": "photo",
        "snapshot_id": "map-1",
        "candidate_ids": [m["candidate_id"] for m in members],
        "candidates": members,
        "location_basis": "near_gps",
    }
    research = {
        "identity_generation": 1,
        "place_selection": group,
        "place_map_snapshots": [snapshot],
        "osm": {"reverse": {"address": {"city": "CAMERA_CITY"}}},
    }
    story = {"id": "story-1", "photo_sha256": "photo", "latitude": 1, "longitude": 2, "research_json": json.dumps(research)}
    return story, research


def assert_inputs(story, research, city):
    scoped = owner.group_input_context(story, research)
    members = research["place_selection"]["candidates"]
    context = _map_query_context(scoped, members)["observed_address_context"]
    addresses = context["building_address_memberships"]
    assert len(addresses) == 1
    assert addresses[0]["physical_candidate_id"] == "osm:way:1"
    assert [a["address"]["house_number"] for a in addresses[0]["address_entries"]] == ["22", "24"]
    assert all(a["address"]["city"] == city for a in addresses[0]["address_entries"])
    assert region_hint(scoped) == city
    wave = first_wave_catalog(scoped, members)
    assert wave["required_grounded_count"] == 2
    for n in ["22", "24"]:
        option = wave["options"][("address", "osm:node:" + n)]
        assert option["group_key"] == "osm:way:1"
        assert option["literal_query"] == city + " Житомирская улица " + n
    manifest = {"objects": {}, "anchor": {"latitude": 54, "longitude": 20}}
    physical = physical_decision_context(scoped, members, manifest)
    row = dict(zip(physical["columns"], next(r for r in physical["rows"] if r[1] == "osm:way:1")))
    assert [x[3] for x in row["literal_address_entries"]] == ["22", "24"]
    assert all(x[1] == city for x in row["literal_address_entries"])
    return scoped


def test_planner_and_reference_comparison_receive_literal_membership_city_without_narrowing():
    story, research = fixture()
    before = copy.deepcopy(research)
    scoped = assert_inputs(story, research, "Калининград")
    identity = {
        "candidates": research["place_selection"]["candidates"],
        "owner_selected_group": scoped["_owner_selected_group"],
        "_owner_group_address_context": scoped["_owner_group_address_context"],
    }
    reply = visual.LiveVisualComparisonMixin._visual_reply("comparison", [], identity, 0)
    assert reply["observed_address_context"]["building_address_memberships"][0]["physical_candidate_id"] == "osm:way:1"
    assert reply["owner_selected_group"]["candidate_ids"] == ["osm:way:1", "osm:way:2"]
    assert {r["candidate_id"] for r in reply["physical_candidates"]} == {"osm:way:1", "osm:way:2"}
    assert research == before
    assert all(n["identity_eligible"] is False for n in scoped["_identity_observed_candidates"][2:])


def test_relocated_sovetsk_group_never_uses_camera_locality_or_camera_geometry():
    story, research = fixture("Советск")
    research["place_selection"]["location_basis"] = "owner_relocated"
    story.update(_camera_hints={"direction_degrees": 123}, _camera_position_verified=True)
    scoped = assert_inputs(story, research, "Советск")
    assert scoped["latitude"] is None and scoped["longitude"] is None
    assert scoped["_camera_hints"] == {} and not scoped["_camera_position_verified"]
    assert "CAMERA_CITY" not in json.dumps(_map_query_context(scoped, research["place_selection"]["candidates"]))
    assert story["latitude"] == 1 and story["longitude"] == 2


@pytest.mark.parametrize("damage", ["node_outside", "node_occupant", "wrong_url", "wrong_membership", "open_contour", "snapshot_photo"])
def test_unproved_neighbor_addresses_do_not_join_group(damage):
    story, research = fixture()
    group = research["place_selection"]
    snapshot = research["place_map_snapshots"][0]
    if damage == "node_outside":
        for node in [*snapshot["address_objects"], *snapshot["city_objects"]]:
            node["map_coordinates"]["latitude"] = 55
    elif damage == "node_occupant":
        for node in [*snapshot["address_objects"], *snapshot["city_objects"]]:
            node["map_object"]["tags"]["shop"] = "yes"
    elif damage == "wrong_url":
        group["candidates"][0]["map_object"]["source_url"] = "https://wrong.example"
    elif damage == "wrong_membership":
        group["candidates"][0]["map_object"]["building_entrances"]["proof"] = "nearby"
    elif damage == "open_contour":
        group["candidates"][0]["map_geometry"]["rings"][0]["closed"] = False
    elif damage == "snapshot_photo":
        snapshot["photo_sha256"] = "other-photo"
    scoped = owner.group_input_context(story, research)
    assert scoped["_owner_group_address_context"]["building_address_memberships"] == []
    assert all(e["candidate_id"].startswith("osm:way:") for e in scoped["_identity_observed_candidates"])


def test_missing_and_mixed_localities_never_inherit_camera_city():
    for city, other in [("", None), ("Советск", "Калининград")]:
        story, research = fixture(city, other)
        scoped = owner.group_input_context(story, research)
        assert region_hint(scoped) == ""
        assert "CAMERA_CITY" not in json.dumps(_map_query_context(scoped, research["place_selection"]["candidates"]))
        if city:
            wave = first_wave_catalog(scoped, research["place_selection"]["candidates"])
            assert wave["options"][("address", "osm:node:22")]["literal_query"].startswith("Советск ")
            assert wave["options"][("address", "osm:way:2")]["literal_query"].startswith("Калининград ")


def test_stale_group_context_is_not_rebound_to_new_photo():
    story, research = fixture()
    story["photo_sha256"] = "new-photo"
    assert owner.group_input_context(story, research) is story
