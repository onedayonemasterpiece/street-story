from dataclasses import replace

import pytest

from street_story.service import ConflictError
from street_story.publication_slots import DEFAULT_DESTINATIONS
from test_mvp_research import NoopVP, service


TEST_ALIAS = "street_story_e2e_20260928_tg"


class TestDestinationVP(NoopVP):
    __test__ = False

    async def bootstrap(self):
        payload = await super().bootstrap()
        payload["destinations"].append({"alias": TEST_ALIAS, "kind": "destination", "label": "Owner test group", "provider": "telegram"})
        payload["capabilities"].append({"destination": TEST_ALIAS, "operation": "publish", "surface": "post", "provider": "telegram", "status": "supported"})
        return payload


class BothVP(TestDestinationVP):
    __test__ = False

    async def bootstrap(self):
        payload = await super().bootstrap()
        payload["destinations"].append({"alias": "lovekenig_tg", "kind": "destination", "label": "lovekenig Telegram", "provider": "telegram"})
        payload["capabilities"].append({"destination": "lovekenig_tg", "operation": "publish", "surface": "post", "provider": "telegram", "status": "supported"})
        for alias, provider in zip(DEFAULT_DESTINATIONS, ("telegram", "vk")):
            payload["destinations"].append({"alias": alias, "kind": "destination",
                                            "label": "История за поворотом", "provider": provider})
            payload["capabilities"].append({"destination": alias, "operation": "publish",
                                            "surface": "post", "provider": provider, "status": "supported"})
        payload["destinations"].append({"alias": "street_story_e2e_other_tg", "kind": "destination", "label": "Other", "provider": "telegram"})
        payload["capabilities"].append({"destination": "street_story_e2e_other_tg", "operation": "publish", "surface": "post", "provider": "telegram", "status": "supported"})
        return payload


@pytest.mark.asyncio
async def test_the_owner_sees_default_real_channels_and_explicit_test_chat(tmp_path):
    # Owner decision 31.10.2026: default real channels, test chat only by name.
    svc, _, _ = service(tmp_path)
    svc.settings = replace(svc.settings, publication_test_alias=TEST_ALIAS)
    svc.providers.vibepublish = BothVP()
    result = await svc.capabilities()
    assert [(d["alias"], d["label"], d["selected"]) for d in result["destinations"]] == [
        (TEST_ALIAS, "Тестовый чат", False), ("tg-safe", "Полюбить Калининград", False),
        ("lovekenig_tg", "Полюбить Калининград", False),
        (DEFAULT_DESTINATIONS[0], "История за поворотом", True),
        (DEFAULT_DESTINATIONS[1], "История за поворотом", True)]


@pytest.mark.asyncio
async def test_without_the_test_chat_nothing_is_preselected_for_production(tmp_path):
    svc, _, _ = service(tmp_path)
    svc.settings = replace(svc.settings, publication_test_alias=TEST_ALIAS)
    destinations = (await svc.capabilities())["destinations"]
    assert destinations and not any(d["selected"] for d in destinations)


@pytest.mark.parametrize(("destinations", "code"), [([], "publish_destination_required"),
                                                    ([TEST_ALIAS, "lovekenig_tg"], "test_and_production_mixed"),
                                                    (["street_story_e2e_other_tg"], "test_destination_only")])
def test_a_publication_goes_either_to_the_test_chat_or_to_real_channels(tmp_path, destinations, code):
    svc, _, _ = service(tmp_path)
    svc.settings = replace(svc.settings, publication_test_alias=TEST_ALIAS)
    with pytest.raises(ConflictError) as error:
        svc.mutate_publish("not-created", "scope-guard", {"destinations": destinations})
    assert error.value.code == code
    with svc.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM publish_intents").fetchone()[0] == 0


def test_test_scope_configuration_cannot_select_a_production_alias(tmp_path):
    svc, _, _ = service(tmp_path)
    with pytest.raises(ValueError, match="explicit Street Story E2E alias"):
        replace(svc.settings, publication_test_alias="lovekenig_tg")
