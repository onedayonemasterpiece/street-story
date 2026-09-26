from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_installer():
    path = Path(__file__).resolve().parents[1] / "deploy" / "devcoveer_install.py"
    spec = importlib.util.spec_from_file_location("street_story_devcoveer_install", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python_312_runtime_prefers_healthy_bridge(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    bridge = tmp_path / "bridge-python"
    system = tmp_path / "system-python"
    bridge.write_text("")
    system.write_text("")
    monkeypatch.setattr(module, "BRIDGE_PYTHON", bridge)
    monkeypatch.setattr(module.shutil, "which", lambda name: str(system) if name == "python3.12" else None)

    seen: list[str] = []

    def fake_run(argv, **kwargs):
        del kwargs
        seen.append(argv[0])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module._python_312_runtime() == str(bridge)
    assert seen == [str(bridge)]


def test_python_312_runtime_falls_back_only_when_bridge_is_unhealthy(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    bridge = tmp_path / "bridge-python"
    system = tmp_path / "system-python"
    bridge.write_text("")
    system.write_text("")
    monkeypatch.setattr(module, "BRIDGE_PYTHON", bridge)
    monkeypatch.setattr(module.shutil, "which", lambda name: str(system) if name == "python3.12" else None)

    def fake_run(argv, **kwargs):
        del kwargs
        return SimpleNamespace(returncode=1 if argv[0] == str(bridge) else 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module._python_312_runtime() == str(system)


def test_python_312_runtime_fails_closed_without_healthy_runtime(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    bridge = tmp_path / "bridge-python"
    system = tmp_path / "system-python"
    bridge.write_text("")
    system.write_text("")
    monkeypatch.setattr(module, "BRIDGE_PYTHON", bridge)
    monkeypatch.setattr(module.shutil, "which", lambda name: str(system) if name == "python3.12" else None)
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kwargs: SimpleNamespace(returncode=1))

    with pytest.raises(module.DeployError, match="healthy Python 3.12"):
        module._python_312_runtime()


def _configure_vibe_state(module, monkeypatch, tmp_path) -> None:
    for name in ("VIBE_PY", "VIBE_DB", "VIBE_OWNER_TOKEN_FILE"):
        path = tmp_path / name.lower()
        path.write_text("placeholder")
        path.chmod(0o600)
        monkeypatch.setattr(module, name, path)
    token_file = tmp_path / "vibe-token"
    principal_file = tmp_path / "vibe-principal"
    token_file.write_text("t" * 40)
    principal_file.write_text("street-story-runtime-old")
    token_file.chmod(0o600)
    principal_file.chmod(0o600)
    monkeypatch.setattr(module, "VIBE_TOKEN_FILE", token_file)
    monkeypatch.setattr(module, "VIBE_PRINCIPAL_FILE", principal_file)
    monkeypatch.setattr(module, "principal_binding_exists", lambda principal: True)


def test_stale_vibe_token_is_replaced_only_after_auth_failure(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    _configure_vibe_state(module, monkeypatch, tmp_path)
    created: list[str] = []

    def denied(*args, **kwargs):
        del args, kwargs
        raise module.DeployError("VibePublish HTTP 403 for /v1/bootstrap")

    def create(sha: str):
        created.append(sha)
        return "street-story-runtime-new", "n" * 40

    monkeypatch.setattr(module, "vibe_request", denied)
    monkeypatch.setattr(module, "_create_vibe_principal", create)

    principal, token = module.ensure_vibe_principal("a" * 40)

    assert principal == "street-story-runtime-new"
    assert token == "n" * 40
    assert created == ["a" * 40]


def test_non_auth_vibe_failure_does_not_rotate_principal(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    _configure_vibe_state(module, monkeypatch, tmp_path)
    monkeypatch.setattr(
        module,
        "vibe_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            module.DeployError("VibePublish HTTP 500 for /v1/bootstrap")
        ),
    )
    monkeypatch.setattr(
        module,
        "_create_vibe_principal",
        lambda sha: pytest.fail(f"unexpected principal rotation for {sha}"),
    )

    with pytest.raises(module.DeployError, match="HTTP 500"):
        module.ensure_vibe_principal("b" * 40)


def test_incomplete_vibe_state_fails_closed(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    for name in ("VIBE_PY", "VIBE_DB", "VIBE_OWNER_TOKEN_FILE"):
        path = tmp_path / name.lower()
        path.write_text("placeholder")
        path.chmod(0o600)
        monkeypatch.setattr(module, name, path)
    token_file = tmp_path / "vibe-token"
    token_file.write_text("t" * 40)
    token_file.chmod(0o600)
    monkeypatch.setattr(module, "VIBE_TOKEN_FILE", token_file)
    monkeypatch.setattr(module, "VIBE_PRINCIPAL_FILE", tmp_path / "missing-principal")

    with pytest.raises(module.DeployError, match="incomplete"):
        module.ensure_vibe_principal("c" * 40)


def _owner_binding_db(path: Path, *, distinct_targets: int) -> None:
    db = sqlite3.connect(path)
    try:
        db.executescript(
            """
            CREATE TABLE principals(id TEXT PRIMARY KEY, tenant_id TEXT, owner INTEGER, active INTEGER);
            CREATE TABLE connections(id TEXT PRIMARY KEY, tenant_id TEXT, provider TEXT, active INTEGER);
            CREATE TABLE destinations(id TEXT PRIMARY KEY, connection_id TEXT, native_id TEXT, label TEXT);
            CREATE TABLE bindings(id TEXT PRIMARY KEY, principal_id TEXT, alias TEXT, destination_id TEXT, active INTEGER);
            INSERT INTO principals VALUES('owner-a','tenant',1,1);
            INSERT INTO principals VALUES('owner-b','tenant',1,1);
            INSERT INTO connections VALUES('conn-a','tenant','telegram',1);
            INSERT INTO destinations VALUES('dest-a','conn-a','native-a','Telegram');
            INSERT INTO bindings VALUES('bind-a','owner-a','lovekenig_tg','dest-a',1);
            INSERT INTO bindings VALUES('bind-b','owner-b','lovekenig_tg','dest-a',1);
            """
        )
        if distinct_targets > 1:
            db.executescript(
                """
                INSERT INTO connections VALUES('conn-b','tenant','telegram',1);
                INSERT INTO destinations VALUES('dest-b','conn-b','native-b','Other Telegram');
                INSERT INTO bindings VALUES('bind-c','owner-b','lovekenig_tg','dest-b',1);
                """
            )
        db.commit()
    finally:
        db.close()


def test_owner_binding_deduplicates_same_target(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    db_path = tmp_path / "vibe.sqlite3"
    _owner_binding_db(db_path, distinct_targets=1)
    monkeypatch.setattr(module, "VIBE_DB", db_path)
    monkeypatch.setattr(module, "VIBE_ALIAS", "lovekenig_tg")

    assert module.owner_binding() == ("tenant", "conn-a", "native-a", "Telegram")


def test_owner_binding_still_fails_closed_for_distinct_targets(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    db_path = tmp_path / "vibe.sqlite3"
    _owner_binding_db(db_path, distinct_targets=2)
    monkeypatch.setattr(module, "VIBE_DB", db_path)
    monkeypatch.setattr(module, "VIBE_ALIAS", "lovekenig_tg")

    with pytest.raises(module.DeployError, match="not uniquely available"):
        module.owner_binding()


def test_provider_env_requires_dedicated_limiter_aliases(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    monkeypatch.setattr(
        module,
        "parse_dotenv",
        lambda _path: {
            "GOOGLE_API_KEY": "fixture-key",
            "SUPABASE_URL": "https://product.example",
            "SUPABASE_KEY": "product-key",
        },
    )
    monkeypatch.setattr(module, "PROVIDERS_ENV", tmp_path / "providers.env")

    with pytest.raises(module.DeployError, match="dedicated GOOGLE_AI_LIMITER"):
        module.configure_provider_env()


def test_provider_env_writes_shared_live_contract(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        module,
        "parse_dotenv",
        lambda _path: {
            "GOOGLE_API_KEY": "fixture-key-one",
            "GOOGLE_API_KEY2": "fixture-key-two",
            "GOOGLE_AI_LIMITER_SUPABASE_URL": "https://limiter.example/",
            "GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY": "limiter-key",
            "AI_RESOURCE_LEDGER_ID": "ledger-fixture",
        },
    )
    monkeypatch.setattr(module, "PROVIDERS_ENV", tmp_path / "providers.env")
    monkeypatch.setattr(
        module,
        "private_write",
        lambda path, content: captured.update(path=str(path), content=content),
    )

    module.configure_provider_env()

    content = captured["content"]
    assert "GOOGLE_AI_LIMITER_SUPABASE_URL=https://limiter.example" in content
    assert "GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY=limiter-key" in content
    assert "AI_RESOURCE_KEY_ENVS=GOOGLE_API_KEY,GOOGLE_API_KEY2" in content
    assert "AI_RESOURCE_LEDGER_ID=ledger-fixture" in content
    assert "GEMINI_QUOTA_SUPABASE_URL=https://limiter.example" in content
    assert not any(line.startswith("SUPABASE_URL=") for line in content.splitlines())


def test_private_resource_release_is_pinned() -> None:
    module = _load_installer()
    assert module.AI_RESOURCE_CONTROL_VERSION == "0.1.2"
    assert module.AI_RESOURCE_CONTROL_REPO.name == "ai-resource-control"


def test_vibe_request_uses_exact_public_host(monkeypatch) -> None:
    module = _load_installer()
    seen: dict[str, str] = {}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b'{}'

    def urlopen(request, timeout):
        del timeout
        seen["host"] = request.get_header("Host")
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module.json, "load", lambda response: {})

    assert module.vibe_request("t" * 40, "GET", "/v1/bootstrap") == {}
    assert seen["host"] == "mcp-vibepublish.kenigevents.ru"
