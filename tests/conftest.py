import json
from pathlib import Path

import pytest


@pytest.fixture
def route_payload():
    return {
        "schema_version": "1.0.0",
        "state_dir_env": "TRACEFOLIO_STATE_DIR",
        "projects": [{
            "project_id": "demo",
            "title": "Demo",
            "context": "Investigate a synthetic project.",
            "enabled": True,
            "sources": [{
                "source_id": "code",
                "kind": "local_directory",
                "root_env": "TRACEFOLIO_DEMO_ROOT",
                "include_paths": ["**/*.py", "README.md"],
            }],
        }],
    }


@pytest.fixture
def example_records():
    root = Path(__file__).resolve().parents[1] / "examples"
    return tuple(json.loads((root / name).read_text(encoding="utf-8")) for name in
                 ("project.example.json", "work.example.json"))


@pytest.fixture
def knowledge_workspace(tmp_path, monkeypatch, route_payload):
    from tracefolio.private_config import load_config
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.py").write_text("checkpoint = 1\n", encoding="utf-8")
    private = tmp_path / "private"
    private.mkdir()
    path = private / "routes.json"
    path.write_text(json.dumps(route_payload), encoding="utf-8")
    monkeypatch.setenv("TRACEFOLIO_DEMO_ROOT", str(source))
    monkeypatch.setenv("TRACEFOLIO_STATE_DIR", str(tmp_path / "state"))
    return load_config(path), source, path

