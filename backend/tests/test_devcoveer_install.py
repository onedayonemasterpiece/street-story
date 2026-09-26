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
