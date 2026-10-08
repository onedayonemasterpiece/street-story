from __future__ import annotations

import importlib.util
import hashlib
import json
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


def test_publisher_maintenance_does_not_stop_generated_backend_unit(tmp_path, monkeypatch):
    from configparser import ConfigParser
    module = _load_installer()
    captured = []
    monkeypatch.setattr(module, 'DATA_ROOT', tmp_path / 'data')
    monkeypatch.setattr(module, 'UNIT_ROOT', tmp_path / 'units')
    monkeypatch.setattr(module, 'private_write', lambda path, text: captured.append(text))
    monkeypatch.setattr(module, 'run', lambda *args, **kwargs: '')
    module.install_service(tmp_path / 'release', tmp_path / 'venv')
    unit = ConfigParser(interpolation=None, strict=False)
    unit.read_string(captured[0])
    assert 'vibepublish.service' not in unit['Unit'].get('Requires', '').split()
    assert 'vibepublish.service' in unit['Unit']['Wants'].split()
    assert 'vibepublish.service' in unit['Unit']['After'].split()


@pytest.mark.parametrize('broken', [None, 'missing_proof', 'wrong_control', 'unverified_common_gate'])
def test_installer_preserves_proven_direct_vision_route_and_rejects_incomplete_metadata(tmp_path, monkeypatch, broken):
    module = _load_installer()
    proof = tmp_path / 'vision-control.json'
    proof.write_text('{"positive":"match","negative":"mismatch"}')
    digest = hashlib.sha256(proof.read_bytes()).hexdigest()
    model = {'model': 'gemini-3.5-flash-lite', 'transport': 'gemini_generate_content',
             'controls': {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True},
             'common_acceptance_verified': True, 'qualification_receipt': str(proof), 'qualification_sha256': digest}
    if broken == 'missing_proof':
        model['qualification_receipt'] = str(tmp_path / 'not-retained.json')
    elif broken == 'wrong_control':
        model['controls']['negative'] = 'match'
    elif broken == 'unverified_common_gate':
        model['common_acceptance_verified'] = False
    caches = {'native-vision-verification-v1': {'model': 'gpt-6-luna', 'transport': 'native_codex_app_server',
              'controls': {'positive': 'match', 'negative': 'mismatch', 'pixel_transport_verified': True},
              'common_acceptance_verified': True},
              'research-text-verification-v1': {'gigachat_model': 'GigaChat-2', 'semantic_contract_verified': True},
              'headless-vision-verification-v1': {'models': [model]}}
    qualification = tmp_path / 'qualification.json'
    qualification.write_text(json.dumps({'evidence': [{'path': str(proof), 'sha256': digest}], 'caches': caches}))
    qualification.chmod(0o600)
    source = tmp_path / 'release/source/backend/deploy'
    source.mkdir(parents=True)
    (source / 'research_guard.mjs').write_text('retained guard fixture')
    with sqlite3.connect(tmp_path / 'street-story.sqlite3') as db:
        db.execute('CREATE TABLE cache(key TEXT PRIMARY KEY,value_json TEXT,expires_at REAL,created_at REAL)')
    monkeypatch.setattr(module, 'RESEARCH_QUALIFICATION', qualification)
    monkeypatch.setattr(module, 'RESEARCH_DIRECTORY', tmp_path / 'opencode')
    monkeypatch.setattr(module, 'DATA_ROOT', tmp_path)
    calls = []
    def attest(argv, **kwargs):
        calls.append(argv)
        return '{}'
    monkeypatch.setattr(module, 'run', attest)
    if broken:
        with pytest.raises(module.DeployError, match='direct vision qualification incomplete'):
            module.install_research_runtime(tmp_path / 'release', tmp_path / 'existing-venv')
        assert not calls
    else:
        receipt = module.install_research_runtime(tmp_path / 'release', tmp_path / 'existing-venv')
        assert receipt['new_inference'] is False and len(calls) == 1
        with sqlite3.connect(tmp_path / 'street-story.sqlite3') as db:
            row = db.execute('SELECT value_json FROM cache WHERE key=?', ('headless-vision-verification-v1',)).fetchone()
        assert json.loads(row[0]) == caches['headless-vision-verification-v1']


def _deployment_fixture(root: Path, sha: str, repository: str = "onedayonemasterpiece/street-story") -> Path:
    release = root / sha
    (release / "source/backend/street_story").mkdir(parents=True)
    (release / "source/backend/street_story/keep.py").write_text("historical source")
    (release / "venv/bin").mkdir(parents=True)
    (release / "venv/bin/python").write_text("rebuildable dependency fixture")
    (release / ".street-story-release.json").write_text(json.dumps({
        "repository": repository, "release_sha": sha,
    }))
    return release


def test_environment_retention_preserves_rollback_processes_and_all_source(monkeypatch, tmp_path):
    module = _load_installer()
    releases = [_deployment_fixture(tmp_path, character * 40) for character in "abcd"]
    monkeypatch.setattr(module, "RELEASES_ROOT", tmp_path)
    monkeypatch.setattr(module, "release_process_references", lambda: {"c" * 40})

    receipt = module.prune_release_environments("a" * 40, "b" * 40)

    assert receipt["removed"] == ["d" * 40]
    assert all((release / "venv").exists() for release in releases[:3])
    assert not (releases[3] / "venv").exists()
    assert all((release / "source/backend/street_story/keep.py").read_text() == "historical source"
               for release in releases)
    assert all((release / ".street-story-release.json").exists() for release in releases)


def test_environment_retention_never_follows_symlinks_or_unverified_manifests(monkeypatch, tmp_path):
    module = _load_installer()
    root = tmp_path / "releases"
    root.mkdir()
    foreign = _deployment_fixture(root, "b" * 40, "someone/another-project")
    unverified = _deployment_fixture(root, "c" * 40)
    (unverified / ".street-story-release.json").write_text("invalid")
    outside = _deployment_fixture(tmp_path, "d" * 40)
    (root / ("d" * 40)).symlink_to(outside, target_is_directory=True)
    linked_env = _deployment_fixture(root, "e" * 40)
    module.shutil.rmtree(linked_env / "venv")
    (linked_env / "venv").symlink_to(outside / "venv", target_is_directory=True)
    monkeypatch.setattr(module, "RELEASES_ROOT", root)
    monkeypatch.setattr(module, "release_process_references", set)

    assert module.prune_release_environments("a" * 40, None)["removed"] == []
    assert all((release / "venv/bin/python").exists() for release in [foreign, unverified, outside, linked_env])


def test_environment_retention_keeps_newly_started_worker(monkeypatch, tmp_path):
    module = _load_installer()
    release = _deployment_fixture(tmp_path, "b" * 40)
    monkeypatch.setattr(module, "RELEASES_ROOT", tmp_path)
    snapshots = iter([set(), {"b" * 40}])
    monkeypatch.setattr(module, "release_process_references", lambda: next(snapshots))

    assert module.prune_release_environments("a" * 40, None)["removed"] == []
    assert (release / "venv/bin/python").exists()


def test_environment_retention_skips_when_process_verification_fails(monkeypatch, tmp_path):
    module = _load_installer()
    release = _deployment_fixture(tmp_path, "b" * 40)
    monkeypatch.setattr(module, "RELEASES_ROOT", tmp_path)

    def unavailable():
        raise module.DeployError("process references unavailable")

    monkeypatch.setattr(module, "release_process_references", unavailable)
    receipt = module.prune_release_environments("a" * 40, None)
    assert receipt["status"] == "skipped"
    assert (release / "venv/bin/python").exists()


def test_previous_release_comes_from_live_unit(monkeypatch, tmp_path):
    module = _load_installer()
    bus_env = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=test-bus"}
    monkeypatch.setattr(module, "systemd_env", lambda: bus_env)
    monkeypatch.setattr(module, "RELEASES_ROOT", tmp_path)
    def unit(*args, **kwargs):
        assert kwargs['env'] == bus_env
        return str(tmp_path / ("b" * 40) / "source")
    monkeypatch.setattr(module, "run", unit)
    assert module.deployed_release_sha() == "b" * 40
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: "/some/unrelated/source")
    assert module.deployed_release_sha() is None


def test_verified_release_pointer_replaces_old_link_and_preserves_targets(monkeypatch, tmp_path):
    module = _load_installer()
    root = tmp_path / 'releases'
    root.mkdir()
    previous = _deployment_fixture(root, 'a' * 40)
    current_release = _deployment_fixture(root, 'b' * 40)
    pointer = tmp_path / 'current'
    pointer.symlink_to(previous, target_is_directory=True)
    monkeypatch.setattr(module, 'RELEASES_ROOT', root)
    module.activate_release_pointer(current_release)
    assert pointer.resolve() == current_release
    assert previous.is_dir() and current_release.is_dir()
    assert not list(tmp_path.glob('.street-story-current-*'))


def test_cleanup_failure_is_reported_without_failing_verified_service(monkeypatch, tmp_path):
    module = _load_installer()
    release = _deployment_fixture(tmp_path, 'b' * 40)
    monkeypatch.setattr(module, 'RELEASES_ROOT', tmp_path)
    monkeypatch.setattr(module, 'release_process_references', set)

    def denied(path):
        raise PermissionError(13, 'not removable')

    monkeypatch.setattr(module.shutil, 'rmtree', denied)
    receipt = module.prune_release_environments('a' * 40, None)
    assert receipt['status'] == 'partial'
    assert receipt['failures'] == [{'release': 'b' * 40, 'errno': 13}]
    assert (release / 'venv/bin/python').exists()


@pytest.mark.parametrize(
    ("alias_kind", "status", "accepted"),
    [
        ("test", "supported", True),
        ("test", "needs_review", True),
        ("production", "supported", False),
        ("test", "blocked", False),
    ],
)
def test_deployed_capability_gate_requires_test_group(monkeypatch, alias_kind, status, accepted):
    module = _load_installer()
    sha = "a" * 40
    alias = module.TEST_VIBE_ALIAS if alias_kind == "test" else module.VIBE_ALIAS
    rows = [{"alias": alias, "provider": "telegram", "status": status}]

    def fake_http(url, **kwargs):
        return {"ok": True, "source_sha": sha} if url.endswith("healthz") else {"destinations": rows}

    monkeypatch.setattr(module, "http_json", fake_http)
    if accepted:
        _, capabilities = module.verify_runtime(sha, "test-token")
        assert capabilities["destinations"] == rows
    else:
        with pytest.raises(module.DeployError, match="unexpected Street Story destination"):
            module.verify_runtime(sha, "test-token")


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
    assert module.AI_RESOURCE_CONTROL_VERSION == "0.1.14"
    assert module.AI_RESOURCE_CONTROL_RELEASE_SHA == "a82a97147d697c3fbf0ba0748d6e49be196d0a7a"
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
        module.install_ai_resource_control(tmp_path / "target-python")

    peel = next(argv for argv in calls if "cat-file" in argv)
    assert peel[-1] == module.AI_RESOURCE_CONTROL_RELEASE_SHA + "^{commit}"


def test_private_resource_wheel_digest_rejects_corrupt_or_symlink_artifact(monkeypatch, tmp_path):
    import hashlib
    module = _load_installer()
    wheel = tmp_path / 'private.whl'
    wheel.write_bytes(b'exact private wheel')
    monkeypatch.setattr(module, 'AI_RESOURCE_CONTROL_WHEEL_SHA256', hashlib.sha256(wheel.read_bytes()).hexdigest())
    module.verify_private_resource_wheel(wheel)
    link = tmp_path / 'other.whl'
    link.symlink_to(wheel)
    with pytest.raises(module.DeployError, match='digest mismatch'):
        module.verify_private_resource_wheel(link)
    wheel.write_bytes(b'corrupt wheel')
    with pytest.raises(module.DeployError, match='digest mismatch'):
        module.verify_private_resource_wheel(wheel)


def test_ensure_venv_uses_only_release_python_for_pip(monkeypatch, tmp_path) -> None:
    module = _load_installer()
    release = tmp_path / "release"
    requirements = release / "source/backend/requirements.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("fastapi>=0.115,<1\n")
    _dependency_inputs(release)
    monkeypatch.setattr(module, 'RELEASES_ROOT', release.parent)
    calls: list[list[str]] = []

    monkeypatch.setattr(module, "_python_312_runtime", lambda: "/fixture/python3.12")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1),
    )
    monkeypatch.setattr(module, "install_ai_resource_control", lambda target: calls.append(["ai", str(target)]))

    def fake_run(argv, **kwargs):
        del kwargs
        calls.append([str(value) for value in argv])
        if argv[:3] == ["/fixture/python3.12", "-m", "venv"]:
            target = Path(argv[-1]) / "bin/python"
            target.parent.mkdir(parents=True)
            target.write_text("")
        return ""

    monkeypatch.setattr(module, "run", fake_run)

    venv = module.ensure_venv(release)
    target = str(venv / "bin/python")
    assert [target, "-m", "ensurepip", "--upgrade"] in calls
    assert [
        target,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "-r",
        str(requirements),
    ] in calls
    assert ["ai", target] in calls
    assert all(str(module.VIBE_PY) not in call for call in calls)


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
    other_key, other_text = module.preview_window('b' * 40, now=3_600.0)
    assert other_key != first_key and other_text != first_text


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


def _dependency_inputs(release, version='0.3.11rc3'):
    source = release / 'source'
    (source / 'backend').mkdir(parents=True, exist_ok=True)
    (source / 'backend/requirements.txt').write_text('fastapi>=0.115,<1\n')
    (source / 'vendor').mkdir(exist_ok=True)
    archive = source / 'vendor/live.tar.gz'
    archive.write_bytes(b'immutable package fixture')
    (source / 'live-framework.lock.json').write_text(json.dumps({
        'archive': 'vendor/live.tar.gz', 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'python_version': version}))


def test_compatible_env_reuse_survives_third_release_and_prune(monkeypatch, tmp_path):
    module = _load_installer()
    a, b, c = [_deployment_fixture(tmp_path, char * 40) for char in 'abc']
    for release in (a, b, c):
        _dependency_inputs(release)
    for release in (b, c):
        module.shutil.rmtree(release / 'venv')
    monkeypatch.setattr(module, 'RELEASES_ROOT', tmp_path)
    monkeypatch.setattr(module, 'release_process_references', set)
    probes = []
    monkeypatch.setattr(module, 'attest_dependency_environment', lambda env, fp: probes.append(env))
    monkeypatch.setattr(module, '_python_312_runtime', lambda: pytest.fail('reuse must not create another env'))
    assert module.ensure_venv(b).resolve() == a / 'venv'
    assert module.ensure_venv(c).resolve() == a / 'venv'
    assert (b / 'venv').is_symlink() and (c / 'venv').is_symlink()
    receipt = module.prune_release_environments(c.name, b.name)
    assert a.name in receipt['protected'] and receipt['removed'] == []
    assert all((release / 'venv/bin/python').is_file() for release in (a, b, c))
    assert probes == [a / 'venv', a / 'venv']


def test_changed_lock_requires_new_env_without_mutating_shared_owner(monkeypatch, tmp_path):
    module = _load_installer()
    a, b = [_deployment_fixture(tmp_path, char * 40) for char in 'ab']
    _dependency_inputs(a)
    _dependency_inputs(b, version='0.3.12')
    module.shutil.rmtree(b / 'venv')
    monkeypatch.setattr(module, 'RELEASES_ROOT', tmp_path)
    monkeypatch.setattr(module, 'attest_dependency_environment', lambda *args: pytest.fail('mismatch is not reusable'))
    assert module.reuse_compatible_environment(b, module.dependency_fingerprint(b)) is None
    assert not (b / 'venv').exists()
    assert (a / 'venv/bin/python').read_text() == 'rebuildable dependency fixture'
    # A previously linked release must fail closed rather than pip-modifying the old env.
    (b / 'venv').symlink_to(a / 'venv', target_is_directory=True)
    with pytest.raises(module.DeployError, match='incompatible'):
        module.reuse_compatible_environment(b, module.dependency_fingerprint(b))


def test_installed_dependency_attestation_and_donor_manifest_required(monkeypatch, tmp_path):
    module = _load_installer()
    a, b = [_deployment_fixture(tmp_path, char * 40) for char in 'ab']
    for release in (a, b):
        _dependency_inputs(release)
    module.shutil.rmtree(b / 'venv')
    monkeypatch.setattr(module, 'RELEASES_ROOT', tmp_path)
    def invalid_env(*args):
        raise module.DeployError('installed archive provenance mismatch')
    monkeypatch.setattr(module, 'attest_dependency_environment', invalid_env)
    assert module.reuse_compatible_environment(b, module.dependency_fingerprint(b)) is None
    monkeypatch.setattr(module, 'attest_dependency_environment', lambda *args: pytest.fail('invalid manifest must not be probed'))
    (a / '.street-story-release.json').write_text('{}')
    assert module.reuse_compatible_environment(b, module.dependency_fingerprint(b)) is None


def test_prune_fences_newly_started_process_with_env_alias(monkeypatch, tmp_path):
    module = _load_installer()
    a, b = [_deployment_fixture(tmp_path, char * 40) for char in 'ab']
    module.shutil.rmtree(b / 'venv')
    (b / 'venv').symlink_to(a / 'venv', target_is_directory=True)
    monkeypatch.setattr(module, 'RELEASES_ROOT', tmp_path)
    snapshots = iter([set(), {b.name}])
    monkeypatch.setattr(module, 'release_process_references', lambda: next(snapshots))
    assert module.prune_release_environments('c' * 40, None)['removed'] == []
    assert (a / 'venv/bin/python').is_file()


def test_dependency_fingerprint_rejects_corrupted_vendor(tmp_path):
    module = _load_installer()
    _dependency_inputs(tmp_path)
    (tmp_path / 'source/vendor/live.tar.gz').write_bytes(b'changed archive')
    with pytest.raises(module.DeployError, match='digest mismatch'):
        module.dependency_fingerprint(tmp_path)


def test_ensure_changed_lock_creates_own_environment_and_leaves_donor(monkeypatch, tmp_path):
    module = _load_installer()
    a, b = [_deployment_fixture(tmp_path, char * 40) for char in 'ab']
    _dependency_inputs(a)
    _dependency_inputs(b, version='0.3.12')
    module.shutil.rmtree(b / 'venv')
    monkeypatch.setattr(module, 'RELEASES_ROOT', tmp_path)
    monkeypatch.setattr(module, '_python_312_runtime', lambda: '/fixture/python3.12')
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(module, 'install_ai_resource_control', lambda target: None)
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ['/fixture/python3.12', '-m', 'venv']:
            python = Path(argv[-1]) / 'bin/python'
            python.parent.mkdir(parents=True)
            python.write_text('new version environment')
        return ''
    monkeypatch.setattr(module, 'run', fake_run)
    assert module.ensure_venv(b) == b / 'venv'
    assert not (b / 'venv').is_symlink()
    assert (a / 'venv/bin/python').read_text() == 'rebuildable dependency fixture'
    assert any(argv[:3] == ['/fixture/python3.12', '-m', 'venv'] for argv in calls)
    assert all(str(a / 'venv') not in str(argv) for argv in calls)
