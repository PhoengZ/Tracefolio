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



@pytest.fixture
def migration_setup(knowledge_workspace, tmp_path):
    from test_local_state import git
    from test_portfolio_cli import publish_demo

    cfg, source, config_path = knowledge_workspace
    store = publish_demo(cfg, config_path.parent)
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    git(checkout, 'init', '-q')
    checkout.joinpath('.gitignore').write_text('/.tracefolio-local/\n/bindings.local.json\n')
    bindings = checkout / 'bindings.local.json'
    bindings.write_text(json.dumps({'TRACEFOLIO_CONFIG': str(config_path),
                                   'TRACEFOLIO_STATE_DIR': str(cfg.state_root),
                                   'TRACEFOLIO_DEMO_ROOT': str(source)}))
    return cfg, store, checkout, bindings

