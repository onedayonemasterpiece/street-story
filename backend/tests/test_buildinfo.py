from __future__ import annotations

from street_story.buildinfo import checkout_source_sha


def test_checkout_source_sha_reads_loose_ref(tmp_path):
    sha = "a" * 40
    git = tmp_path / ".git"
    (git / "refs/heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs/heads/main").write_text(sha + "\n", encoding="utf-8")
    assert checkout_source_sha(tmp_path) == sha


def test_checkout_source_sha_reads_packed_ref(tmp_path):
    sha = "b" * 40
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/work\n", encoding="utf-8")
    (git / "packed-refs").write_text(f"# pack-refs\n{sha} refs/heads/work\n", encoding="utf-8")
    assert checkout_source_sha(tmp_path) == sha


def test_checkout_source_sha_prefers_valid_env_override(tmp_path, monkeypatch):
    sha = "c" * 40
    monkeypatch.setenv("STREET_STORY_DEPLOY_SHA", sha.upper())
    assert checkout_source_sha(tmp_path) == sha
