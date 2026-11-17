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

