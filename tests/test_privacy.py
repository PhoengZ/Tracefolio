import copy
import json
import os

import pytest

from tracefolio.errors import TracefolioError
from tracefolio.privacy import (
    PrivacyPolicy,
    path_allowed,
    resolve_allowed_path,
    sanitize_text,
    secure_private_root,
)
from tracefolio.private_config import load_config, load_config_set, selected_config_paths


def configured(tmp_path, monkeypatch, payload):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    config = tmp_path / "private" / "routes.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("TRACEFOLIO_DEMO_ROOT", str(source))
    monkeypatch.setenv("TRACEFOLIO_STATE_DIR", str(tmp_path / "state"))
    return config, source


def test_missing_binding_is_safe(tmp_path, monkeypatch, route_payload):
    config, _ = configured(tmp_path, monkeypatch, route_payload)
    monkeypatch.delenv("TRACEFOLIO_DEMO_ROOT")
    with pytest.raises(TracefolioError) as error:
        load_config(config)
    assert error.value.code == "missing_binding"
    assert str(tmp_path) not in str(error.value)


def test_private_roots_cannot_be_inside_sources(tmp_path, monkeypatch, route_payload):
    config, source = configured(tmp_path, monkeypatch, route_payload)
    monkeypatch.setenv("TRACEFOLIO_STATE_DIR", str(source / "state"))
    with pytest.raises(TracefolioError, match="unsafe_root"):
        load_config(config)


def test_home_root_rejected(tmp_path, monkeypatch, route_payload):
    from pathlib import Path
    config, _ = configured(tmp_path, monkeypatch, route_payload)
    monkeypatch.setenv("TRACEFOLIO_DEMO_ROOT", str(Path.home()))
    with pytest.raises(TracefolioError, match="unsafe_root"):
        load_config(config)


def test_disabled_project_does_not_resolve_missing_binding_or_read_source(
        tmp_path, monkeypatch, route_payload):
    route_payload["projects"][0]["enabled"] = False
    config, _ = configured(tmp_path, monkeypatch, route_payload)
    monkeypatch.delenv("TRACEFOLIO_DEMO_ROOT")
    assert load_config(config).locators == {}


def test_registry_composition_and_conflicts(tmp_path, monkeypatch, route_payload):
    config, _ = configured(tmp_path, monkeypatch, route_payload)
    second = copy.deepcopy(route_payload)
    second["projects"][0]["project_id"] = "second"
    other = config.parent / "other.json"
    other.write_text(json.dumps(second), encoding="utf-8")
    assert len(load_config_set([config, other]).registry.projects) == 2
    other.write_text(json.dumps(route_payload), encoding="utf-8")
    with pytest.raises(TracefolioError, match="duplicate_project"):
        load_config_set([config, other])
    second["limits"] = {"max_commits_per_run": 10}
    other.write_text(json.dumps(second), encoding="utf-8")
    with pytest.raises(TracefolioError, match="settings_conflict"):
        load_config_set([config, other])


def test_ambiguous_selectors_rejected(monkeypatch):
    monkeypatch.setenv("TRACEFOLIO_CONFIG", "one")
    monkeypatch.setenv("TRACEFOLIO_CONFIG_FILES", '["two"]')
    with pytest.raises(TracefolioError, match="config_selection"):
        selected_config_paths()


@pytest.mark.parametrize("surface", ["message", "+ diff", "- diff", "context", "path", "exception"])
def test_redaction_covers_all_input_surfaces(surface):
    secret = "ghp_" + "A" * 36
    values = (r"C:\private\project", "example-org/private-repo",
              "https://private.invalid/api", "owner@example.invalid")
    text = surface + " checkpoint " + secret + " " + " ".join(values)
    clean = sanitize_text(text, PrivacyPolicy(values))
    assert "checkpoint" in clean.text
    for value in values + (secret,):
        assert value not in clean.text
    assert clean.redactions


def test_absolute_source_urls_and_paths_are_redacted():
    clean = sanitize_text("https://example.invalid/a /home/alice/private/file C:\\work\\file",
                          PrivacyPolicy())
    assert "example.invalid" not in clean.text
    assert "alice" not in clean.text
    assert "C:\\work" not in clean.text


def test_traversal_and_denied_files(tmp_path):
    for bad in ("../outside", "/absolute", r"C:\outside", ".env", "config/brain.env",
                ".git/config", "credentials.json"):
        with pytest.raises(TracefolioError):
            resolve_allowed_path(tmp_path, bad)
    assert path_allowed("one.py", ["**/*.py"], [])
    assert not path_allowed("node_modules/foo.py", ["**/*.py"], [])


def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import subprocess
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                capture_output=True, check=False)
        assert result.returncode == 0
    with pytest.raises(TracefolioError, match="path_escape"):
        resolve_allowed_path(root, "link/file.py")


def test_private_root_permissions(tmp_path):
    root = tmp_path / "private-state"
    secure_private_root(root)
    assert root.is_dir()
    if os.name != "nt":
        assert root.stat().st_mode & 0o077 == 0


def test_private_permissions_can_be_reapplied_with_children(tmp_path):
    root = tmp_path / "private-reused"
    secure_private_root(root)
    child = root / "record.json"
    child.write_text("{}", encoding="utf-8")
    secure_private_root(root)
    assert child.read_text(encoding="utf-8") == "{}"

