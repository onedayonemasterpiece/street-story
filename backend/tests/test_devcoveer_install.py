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


def test_tracked_status_ignores_untracked_files(monkeypatch) -> None:
    module = _load_installer()
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        del kwargs
        seen.append(argv)
        return ""

    monkeypatch.setattr(module, "run", fake_run)

    assert module._tracked_status() == ""
    assert seen == [[
        "git",
        "-C",
        str(module.REPO_ROOT),
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
    ]]


def test_exact_source_still_rejects_tracked_changes(monkeypatch) -> None:
    module = _load_installer()
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        del kwargs
        calls.append(argv)
        if "status" in argv:
            return " M backend/street_story/app.py\n"
        pytest.fail(f"unexpected command after tracked dirty gate: {argv}")

    monkeypatch.setattr(module, "run", fake_run)

    with pytest.raises(module.DeployError, match="not clean"):
        module.exact_source("a" * 40)

    assert len(calls) == 1
    assert "--untracked-files=no" in calls[0]


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
        raise module.VibeHttpError(401, "/v1/bootstrap", "unauthorized")

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
            module.VibeHttpError(403, "/v1/bootstrap", "invalid_host")
        ),
    )
    monkeypatch.setattr(
        module,
        "_create_vibe_principal",
        lambda sha: pytest.fail(f"unexpected principal rotation for {sha}"),
    )

    with pytest.raises(module.VibeHttpError, match="invalid_host"):
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


def test_provider_env_rejects_unverified_generic_limiter_credential(monkeypatch, tmp_path) -> None:
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
    monkeypatch.setattr(module, "verify_limiter_credential", lambda *_args: False)

    with pytest.raises(module.DeployError, match="verified canonical Google AI limiter"):
        module.configure_provider_env()


def test_provider_env_promotes_only_verified_server_service_key(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    captured: dict[str, str] = {}
    attempts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module,
        "parse_dotenv",
        lambda _path: {
            "GOOGLE_API_KEY": "fixture-key",
            "SUPABASE_URL": "https://wrong-project.example",
            "PERSONALIZATION_SUPABASE_SECRET_KEY": "canonical-service-key",
        },
    )
    monkeypatch.setattr(module, "PROVIDERS_ENV", tmp_path / "providers.env")
    monkeypatch.setattr(
        module,
        "verify_limiter_credential",
        lambda url, key: attempts.append((url, key)) is None and key == "canonical-service-key",
    )
    monkeypatch.setattr(
        module,
        "private_write",
        lambda path, content: captured.update(path=str(path), content=content),
    )

    module.configure_provider_env()

    assert attempts == [(module.CANONICAL_GOOGLE_AI_LIMITER_URL, "canonical-service-key")]
    content = captured["content"]
    assert f"GOOGLE_AI_LIMITER_SUPABASE_URL={module.CANONICAL_GOOGLE_AI_LIMITER_URL}" in content
    assert "GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY=canonical-service-key" in content
    assert "wrong-project.example" not in content


def test_provider_env_writes_shared_live_contract(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        module,
        "parse_dotenv",
        lambda _path: {
            "GOOGLE_API_KEY": "fixture-key-one",
            "GOOGLE_API_KEY2": "fixture-key-two",
            "GOOGLE_AI_LIMITER_SUPABASE_URL": module.CANONICAL_GOOGLE_AI_LIMITER_URL,
            "GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY": "limiter-key",
            "AI_RESOURCE_LEDGER_ID": "ledger-fixture",
        },
    )
    monkeypatch.setattr(module, "verify_limiter_credential", lambda *_args: True)
    monkeypatch.setattr(module, "PROVIDERS_ENV", tmp_path / "providers.env")
    monkeypatch.setattr(
        module,
        "private_write",
        lambda path, content: captured.update(path=str(path), content=content),
    )

    module.configure_provider_env()

    content = captured["content"]
    assert (
        f"GOOGLE_AI_LIMITER_SUPABASE_URL={module.CANONICAL_GOOGLE_AI_LIMITER_URL}"
        in content
    )
    assert "GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY=limiter-key" in content
    assert f"AI_RESOURCE_CONTROL_URL={module.CANONICAL_GOOGLE_AI_LIMITER_URL}" in content
    assert "AI_RESOURCE_CONTROL_SERVICE_KEY=limiter-key" in content
    assert "AI_RESOURCE_KEY_ENVS=GOOGLE_API_KEY,GOOGLE_API_KEY2" in content
    assert "AI_RESOURCE_LEDGER_ID=ledger-fixture" in content
    assert f"GEMINI_QUOTA_SUPABASE_URL={module.CANONICAL_GOOGLE_AI_LIMITER_URL}" in content
    assert not any(line.startswith("SUPABASE_URL=") for line in content.splitlines())


def test_private_resource_release_is_pinned() -> None:
    module = _load_installer()
    assert module.AI_RESOURCE_CONTROL_VERSION == "0.1.5"
    assert module.AI_RESOURCE_CONTROL_RELEASE_SHA == "f2ca21d2b239fde1c8055dc8ad776efdcc94449a"
    assert module.AI_RESOURCE_CONTROL_REPO.name == "ai-resource-control"


def test_private_resource_commit_peel_is_literal(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    repo = tmp_path / "ai-resource-control"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(module, "AI_RESOURCE_CONTROL_REPO", repo)
    calls: list[list[str]] = []

    class StopAfterPeel(RuntimeError):
        pass

    def fake_run(argv, **kwargs):
        del kwargs
        calls.append(argv)
        if "cat-file" in argv:
            raise StopAfterPeel
        return ""

    monkeypatch.setattr(module, "run", fake_run)

    with pytest.raises(StopAfterPeel):
        module.install_ai_resource_control(tmp_path / "target-python", "driver-python")

    peel = next(argv for argv in calls if "cat-file" in argv)
    assert peel[-1] == module.AI_RESOURCE_CONTROL_RELEASE_SHA + "^{commit}"


def test_live_resource_preflight_is_read_only_and_central(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    providers = tmp_path / "providers.env"
    providers.write_text(
        "\n".join(
            [
                "AI_RESOURCE_CONTROL_URL=https://limiter.example",
                "AI_RESOURCE_CONTROL_SERVICE_KEY=fixture-key",
                "AI_RESOURCE_KEY_ENVS=GOOGLE_API_KEY,GOOGLE_API_KEY2",
                "GOOGLE_API_KEY=fixture-one",
                "GOOGLE_API_KEY2=fixture-two",
                "AI_RESOURCE_LEDGER_ID=ledger-fixture",
                "",
            ]
        )
    )
    providers.chmod(0o600)
    monkeypatch.setattr(module, "PROVIDERS_ENV", providers)
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return (
            '{"contract":"ai_resource_leases_v1","ledger_id":"ledger-fixture",'
            '"candidate_count":6,"acquire":"server_registry_v2",'
            '"key_material":"supabase_vault_canonical_v1",'
            '"key_delivery":"lease_wrapped_aes256_etm_v1",'
            '"retention":"live_only_27h_lazy_compaction_v1",'
            '"local_provider_aliases":0}'
        )

    monkeypatch.setattr(module, "run", fake_run)

    result = module.verify_live_resource_control(tmp_path / "venv")

    assert result == {
        "contract": "ai_resource_leases_v1",
        "ledger_id": "ledger-fixture",
        "candidate_count": 6,
        "acquire": "server_registry_v2",
        "key_material": "supabase_vault_canonical_v1",
        "key_delivery": "lease_wrapped_aes256_etm_v1",
        "retention": "live_only_27h_lazy_compaction_v1",
        "local_provider_aliases": 0,
    }
    assert seen["argv"][:2] == [str(tmp_path / "venv/bin/python"), "-c"]
    assert "acquire(" not in seen["argv"][2]
    assert seen["env"]["AI_RESOURCE_CONTROL_URL"] == "https://limiter.example"
    assert seen["env"]["AI_RESOURCE_CONTROL_SERVICE_KEY"] == "fixture-key"
    assert seen["env"]["AI_RESOURCE_LEDGER_ID"] == "ledger-fixture"
    assert not any(name.startswith("GOOGLE_API_KEY") for name in seen["env"])


@pytest.mark.parametrize(
    "payload",
    [
        '{"contract":"wrong","ledger_id":"ledger-fixture","candidate_count":6,'
        '"acquire":"server_registry_v2","key_material":"supabase_vault_canonical_v1",'
        '"key_delivery":"lease_wrapped_aes256_etm_v1","retention":"live_only_27h_lazy_compaction_v1",'
        '"local_provider_aliases":0}',
        '{"contract":"ai_resource_leases_v1","ledger_id":"","candidate_count":6,'
        '"acquire":"server_registry_v2","key_material":"supabase_vault_canonical_v1",'
        '"key_delivery":"lease_wrapped_aes256_etm_v1","retention":"live_only_27h_lazy_compaction_v1",'
        '"local_provider_aliases":0}',
        '{"contract":"ai_resource_leases_v1","ledger_id":"ledger-fixture","candidate_count":0,'
        '"acquire":"server_registry_v2","key_material":"supabase_vault_canonical_v1",'
        '"key_delivery":"lease_wrapped_aes256_etm_v1","retention":"live_only_27h_lazy_compaction_v1",'
        '"local_provider_aliases":0}',
        '{"contract":"ai_resource_leases_v1","ledger_id":"ledger-fixture","candidate_count":6,'
        '"acquire":"legacy","key_material":"supabase_vault_canonical_v1",'
        '"key_delivery":"lease_wrapped_aes256_etm_v1","retention":"live_only_27h_lazy_compaction_v1",'
        '"local_provider_aliases":0}',
        '{"contract":"ai_resource_leases_v1","ledger_id":"ledger-fixture","candidate_count":6,'
        '"acquire":"server_registry_v2","key_material":"supabase_vault_canonical_v1",'
        '"key_delivery":"lease_wrapped_aes256_etm_v1","retention":"live_only_27h_lazy_compaction_v1",'
        '"local_provider_aliases":1}',
        '{"contract":"ai_resource_leases_v1","ledger_id":"other-ledger","candidate_count":6,'
        '"acquire":"server_registry_v2","key_material":"supabase_vault_canonical_v1",'
        '"key_delivery":"lease_wrapped_aes256_etm_v1","retention":"live_only_27h_lazy_compaction_v1",'
        '"local_provider_aliases":0}',
    ],
)
def test_live_resource_preflight_fails_closed(monkeypatch, tmp_path, payload) -> None:
    module = _load_installer()
    providers = tmp_path / "providers.env"
    providers.write_text(
        "\n".join(
            [
                "AI_RESOURCE_CONTROL_URL=https://limiter.example",
                "AI_RESOURCE_CONTROL_SERVICE_KEY=fixture-key",
                "AI_RESOURCE_LEDGER_ID=ledger-fixture",
                "",
            ]
        )
    )
    providers.chmod(0o600)
    monkeypatch.setattr(module, "PROVIDERS_ENV", providers)
    monkeypatch.setattr(module, "run", lambda argv, **kwargs: payload)

    with pytest.raises(module.DeployError, match="shared Live resource"):
        module.verify_live_resource_control(tmp_path / "venv")


def test_preview_window_is_idempotent_within_half_hour_and_refreshes_intent_after() -> None:
    module = _load_installer()
    sha = "a" * 40

    first_key, first_text = module.preview_window(sha, now=3_600.0)
    same_key, same_text = module.preview_window(sha, now=3_600.0 + 1_799)
    next_key, next_text = module.preview_window(sha, now=3_600.0 + 1_800)

    assert (first_key, first_text) == (same_key, same_text)
    assert next_key != first_key
    assert next_text != first_text
    assert first_key.startswith("street-story-deploy-preview-" + "a" * 20 + "-")
    assert len(first_key) <= 128
    assert "Preview only; do not dispatch." in first_text
    assert module.preview_request_key(sha, now=3_600.0) == first_key


def test_preview_preflight_accepts_claimed_dry_run_without_bootstrap_supported(monkeypatch) -> None:
    module = _load_installer()
    calls: list[tuple[str, str]] = []

    def request(token, method, path, **kwargs):
        del token, kwargs
        calls.append((method, path))
        if method == "POST" and path == "/v1/publications":
            return {"operation_id": "op_fixture"}
        if method == "GET" and path == "/v1/operations/op_fixture":
            return {
                "receipts": [
                    {
                        "operation_id": "op_fixture",
                        "operation_complete": True,
                        "state": "needs_approval",
                        "dry_run": True,
                        "worker_seen_at": "2026-09-27T10:12:21Z",
                        "deliveries": [
                            {
                                "destination": module.VIBE_ALIAS,
                                "provider": "telegram",
                                "state": "needs_approval",
                                "observed": "not_attempted",
                            }
                        ],
                    }
                ]
            }
        pytest.fail(f"unexpected Vibe request {method} {path}")

    monkeypatch.setattr(module, "vibe_request", request)

    assert module.preview_preflight("t" * 40, "a" * 40) == {
        "alias": module.VIBE_ALIAS,
        "status": "supported",
        "reason": "Preview completed with worker/target validation and no provider dispatch",
    }
    assert calls == [
        ("POST", "/v1/publications"),
        ("GET", "/v1/operations/op_fixture"),
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"dry_run": False}, "dry-run semantics"),
        ({"worker_seen_at": None}, "claimed by a worker"),
        ({"deliveries": []}, "resolve the Telegram target"),
        (
            {
                "deliveries": [
                    {
                        "destination": "wrong_alias",
                        "provider": "telegram",
                        "state": "needs_approval",
                        "observed": "not_attempted",
                    }
                ]
            },
            "resolve the Telegram target",
        ),
        (
            {
                "deliveries": [
                    {
                        "destination": "lovekenig_tg",
                        "provider": "telegram",
                        "state": "needs_approval",
                        "observed": "provider_scheduled",
                    }
                ]
            },
            "unexpectedly reached provider dispatch",
        ),
    ],
)
def test_preview_preflight_fails_closed_on_unproved_preview(monkeypatch, mutation, message) -> None:
    module = _load_installer()
    current = {
        "operation_id": "op_fixture",
        "operation_complete": True,
        "state": "needs_approval",
        "dry_run": True,
        "worker_seen_at": "2026-09-27T10:12:21Z",
        "deliveries": [
            {
                "destination": module.VIBE_ALIAS,
                "provider": "telegram",
                "state": "needs_approval",
                "observed": "not_attempted",
            }
        ],
    }
    current.update(mutation)

    monkeypatch.setattr(
        module,
        "vibe_request",
        lambda token, method, path, **kwargs: (
            {"operation_id": "op_fixture"}
            if method == "POST"
            else {"receipts": [current]}
        ),
    )

    with pytest.raises(module.DeployError, match=message):
        module.preview_preflight("t" * 40, "b" * 40)


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


def test_vibe_http_error_preserves_only_safe_machine_code(monkeypatch) -> None:
    module = _load_installer()
    import io
    payload = b'{"error":{"code":"invalid_host","message":"private details are ignored"}}'
    error = module.urllib.error.HTTPError(
        module.VIBE_BASE_URL + "/v1/bootstrap",
        403,
        "Forbidden",
        {},
        io.BytesIO(payload),
    )
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(error),
    )

    with pytest.raises(module.VibeHttpError) as caught:
        module.vibe_request("t" * 40, "GET", "/v1/bootstrap")

    assert caught.value.status == 403
    assert caught.value.code == "invalid_host"
    assert "private details" not in str(caught.value)
    assert module._vibe_auth_failed(caught.value) is False
