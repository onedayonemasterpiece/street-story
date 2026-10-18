from dataclasses import replace

import pytest

from street_story.service import ConflictError
from test_mvp_research import NoopVP, service


TEST_ALIAS = "street_story_e2e_20260928_tg"


class TestDestinationVP(NoopVP):
    __test__ = False

    async def bootstrap(self):
        payload = await super().bootstrap()
        payload["destinations"].append({"alias": TEST_ALIAS, "kind": "destination", "label": "Owner test group", "provider": "telegram"})
        payload["capabilities"].append({"destination": TEST_ALIAS, "operation": "publish", "surface": "post", "provider": "telegram", "status": "supported"})
        return payload


@pytest.mark.asyncio
async def test_owner_mode_exposes_only_the_actual_test_group(tmp_path):
    svc, _, _ = service(tmp_path)
    svc.settings = replace(svc.settings, publication_test_alias=TEST_ALIAS)
    svc.providers.vibepublish = TestDestinationVP()
    result = await svc.capabilities()
    assert [item["alias"] for item in result["destinations"]] == [TEST_ALIAS]
    assert result["destinations"][0]["selected"] is True


@pytest.mark.asyncio
async def test_missing_test_destination_does_not_fall_back_to_production(tmp_path):
    svc, _, _ = service(tmp_path)
    svc.settings = replace(svc.settings, publication_test_alias=TEST_ALIAS)
    assert (await svc.capabilities())["destinations"] == []


@pytest.mark.parametrize("destinations", [[], ["lovekenig_tg"], [TEST_ALIAS, "lovekenig_tg"], ["street_story_e2e_other_tg"]])
def test_owner_mode_rejects_other_destinations_before_creating_an_intent(tmp_path, destinations):
    svc, _, _ = service(tmp_path)
    svc.settings = replace(svc.settings, publication_test_alias=TEST_ALIAS)
    with pytest.raises(ConflictError, match="configured test group"):
        svc.mutate_publish("not-created", "scope-guard", {"destinations": destinations})
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM publish_intents").fetchone()[0] == 0


def test_test_scope_configuration_cannot_select_a_production_alias(tmp_path):
    svc, _, _ = service(tmp_path)
    with pytest.raises(ValueError, match="explicit Street Story E2E alias"):
        replace(svc.settings, publication_test_alias="lovekenig_tg")
