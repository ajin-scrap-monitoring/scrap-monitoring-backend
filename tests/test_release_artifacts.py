"""Release source identity, public-only assets and settings documentation contract."""

import hashlib
import json
import os
import re
import subprocess

import pytest

from scripts.release import ROOT, identity, prepare_assets
from src.app.core.config import Settings

REFERENCE = "ghcr.io/ajin-scrap-monitoring/scrap-monitoring-backend@sha256:" + "a" * 64


def run(root, *args):
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic test",
        "GIT_COMMITTER_NAME": "Synthetic test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    return subprocess.check_output(
        ["git", "-C", str(root), *args], env=environment, text=True
    ).strip()


@pytest.fixture
def release_repo(tmp_path):
    run(tmp_path, "init", "-b", "main")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
    run(tmp_path, "add", "pyproject.toml")
    run(tmp_path, "commit", "-m", "Synthetic baseline")
    run(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    run(tmp_path, "tag", "-a", "v0.1.0", "-m", "Synthetic release")
    return tmp_path


def test_release_tag_matches_checked_out_main_source(release_repo):
    assert identity(release_repo, "v0.1.0") == {
        "version": "0.1.0",
        "commit": run(release_repo, "rev-parse", "HEAD"),
    }


@pytest.mark.parametrize("tag", ["main", "v0.1", "v01.1.0", "v0.1.0-rc1", "v0.1.0\n"])
def test_invalid_release_name_is_rejected(release_repo, tag):
    with pytest.raises(ValueError, match="Release tag"):
        identity(release_repo, tag)


def test_release_project_version_must_match(release_repo):
    with pytest.raises(ValueError, match="project.version"):
        identity(release_repo, "v0.2.0")


def test_branch_only_release_is_rejected(release_repo):
    run(release_repo, "tag", "-d", "v0.1.0")
    (release_repo / "new.txt").write_text("Synthetic unpublished work")
    run(release_repo, "add", "new.txt")
    run(release_repo, "commit", "-m", "Synthetic branch change")
    run(release_repo, "tag", "v0.1.0")
    with pytest.raises(ValueError, match="origin/main"):
        identity(release_repo, "v0.1.0")


def test_different_checked_out_revision_is_rejected(release_repo):
    (release_repo / "new.txt").write_text("Synthetic newer checkout")
    run(release_repo, "add", "new.txt")
    run(release_repo, "commit", "-m", "Synthetic newer checkout")
    with pytest.raises(ValueError, match="Checked-out"):
        identity(release_repo, "v0.1.0")


def test_assets_keep_private_environment_out_and_pin_document_links(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
    (root / ".env.example").write_text("APP_ENV=development\n")
    (root / ".env").write_text("PRIVATE_MARKER=must-not-be-published\n")
    (root / "docs/CONFIGURATION.md").write_text("[Example](../.env.example)\n")
    (root / "docs/RELEASING.md").write_text("[Config](CONFIGURATION.md)\n")
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps({"packages": [{"name": "synthetic"}], "notices": [{"text": "MIT"}]})
    )
    monkeypatch.setenv("EDGE_API_KEY", "must-not-be-published")
    monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", "/private/must-not-be-published.json")
    destination = tmp_path / "assets"
    prepare_assets(root, destination, REFERENCE, audit)
    assert {p.name for p in destination.iterdir()} == {
        "env.example",
        "CONFIGURATION.md",
        "RELEASING.md",
        "settings.schema.json",
        "image-reference.txt",
        "runtime-dependencies.json",
        "SHA256SUMS",
    }
    assert not any(p.name.startswith(".") for p in destination.iterdir())
    assert all("must-not-be-published" not in p.read_text() for p in destination.iterdir())
    assert "/blob/v0.1.0/.env.example" in (destination / "CONFIGURATION.md").read_text()
    schema = json.loads((destination / "settings.schema.json").read_text())
    assert set(schema["properties"]) == set(Settings.model_fields)
    assert schema["properties"]["EDGE_V2_DEVICES"]["default"] == []
    for line in (destination / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((destination / name).read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="empty"):
        prepare_assets(root, destination, REFERENCE, audit)


@pytest.mark.parametrize("reference", ["ghcr.io/other/backend:latest", REFERENCE + "\n"])
def test_assets_require_fixed_repository_digest(tmp_path, reference):
    with pytest.raises(ValueError, match="sha256"):
        prepare_assets(tmp_path, tmp_path / "assets", reference, tmp_path / "not-used.json")


def test_every_app_setting_has_a_documented_contract():
    text = (ROOT / "docs/CONFIGURATION.md").read_text()
    rows = set(re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \|", text, re.MULTILINE))
    assert set(Settings.model_fields) <= rows
    assert "필수" in text and "코드 기본값" in text and "형식 예시" in text
