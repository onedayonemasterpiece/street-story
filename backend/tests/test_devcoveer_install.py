from __future__ import annotations

import importlib.util
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
