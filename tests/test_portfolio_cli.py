import copy
import json
import subprocess

import pytest
from test_github_source import FakeGitHub
from test_proposals import proposal_for, write_proposal

from tracefolio.cli import main
from tracefolio.collection import collect_projects
from tracefolio.contracts.portfolio import PortfolioSnapshot
from tracefolio.contracts.routes import ProjectRoute, SourceRoute, WorkUnit
from tracefolio.investigation import create_packet
from tracefolio.portfolio import export_portfolio, summarize_portfolio
from tracefolio.proposals import apply_proposal
from tracefolio.sources.github_public import GitHubPublicProvider
from tracefolio.state import StateStore


def publish_demo(cfg, private):
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    path = private / "proposal.json"
    write_proposal(path, proposal_for(packet))
    apply_proposal(store, path)
    return store


def test_all_projects_pending_disabled_errors_and_counts(knowledge_workspace):
    cfg, _, _ = knowledge_workspace
    pending = cfg.registry.projects[0]
    cfg.registry.projects.extend([
        pending.model_copy(update={"project_id": "disabled", "enabled": False}),
        pending.model_copy(update={"project_id": "broken"}),
    ])
    store = StateStore.open(cfg)
    store._atomic_json(store._path("projects", "broken", "CURRENT"), {"revision_id": "missing"})
    result = summarize_portfolio(store, cfg.registry)
    assert result.configured_project_count == len(result.entries) == 3
    assert [e.status for e in result.entries] == ["pending", "disabled", "error"]
    assert result.entries[-1].errors[0].code == "state_read_failed"
    PortfolioSnapshot.model_validate(result.model_dump(mode="json"))


def test_ready_requires_complete_collection_and_analysis(knowledge_workspace):
    cfg, source, path = knowledge_workspace
    store = publish_demo(cfg, path.parent)
    result = summarize_portfolio(store, cfg.registry)
    assert result.entries[0].status == "ready"
    historical = store.current("demo").works[0].model_dump_json()
    (source / "one.py").write_text("checkpoint = 2")
    collect_projects(cfg, ["demo"])
    entry = summarize_portfolio(store, cfg.registry).entries[0]
    assert entry.status == "partial" and entry.coverage.pending_evidence_count
    assert entry.works[0].claims[0].status == "needs_review"
    assert store.current("demo").works[0].model_dump_json() == historical


def test_configured_empty_work_is_visible_and_pending(knowledge_workspace):
    cfg, _, _ = knowledge_workspace
    cfg.registry.projects[0].sources[0].work_units = [WorkUnit(
        work_id="unread-work", context="Investigate retry behavior.", include_paths=["retry.py"])]
    store = StateStore.open(cfg)
    entry = summarize_portfolio(store, cfg.registry).entries[0]
    assert entry.status == "pending"
    assert entry.works[0].work_id == "unread-work"
    assert entry.works[0].analysis_status == "pending" and entry.works[0].unknowns
    assert not entry.works[0].claims and entry.works[0].contribution == "unverified"


def test_packet_contains_work_route_descriptions(knowledge_workspace):
    cfg, _, _ = knowledge_workspace
    cfg.registry.projects[0].sources[0].work_units = [WorkUnit(
        work_id="checkpoint", context="Investigate checkpoint reliability.", include_paths=["one.py"])]
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    assert packet.work_routes[0].context == "Investigate checkpoint reliability."
    assert packet.work_routes[0].source_id == "code"
    assert packet.work_routes[0].include_paths == ["one.py"]


def test_route_change_marks_last_good_generation_partial(knowledge_workspace):
    cfg, _, path = knowledge_workspace
    store = publish_demo(cfg, path.parent)
    cfg.registry.projects[0].context = "Investigate changed context."
    entry = summarize_portfolio(store, cfg.registry).entries[0]
    assert entry.status == "partial" and entry.project is not None
    assert "route_changed" in [e.code for e in entry.errors]


def test_cli_json_output_exit_codes_and_private_export(knowledge_workspace, monkeypatch, capsys):
    cfg, _, path = knowledge_workspace
    monkeypatch.delenv("TRACEFOLIO_CONFIG_FILES", raising=False)
    monkeypatch.setenv("TRACEFOLIO_CONFIG", str(path))
    assert main(["summarize", "--all", "--format", "json"]) == 3
    assert json.loads(capsys.readouterr().out)["entries"][0]["status"] == "pending"
    publish_demo(cfg, path.parent)
    target = cfg.state_root / "exports" / "portfolio.json"
    assert main(["summarize", "--all", "--format", "json", "--out", str(target)]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == json.loads(target.read_text())
    assert main(["summarize", "--all", "--out", str(path.parent / "unsafe.json")]) == 2
    assert "unsafe_export" in capsys.readouterr().err
    assert main(["summarize", "--project", "missing"]) == 2
    assert "unknown_project" in capsys.readouterr().err
    monkeypatch.setattr(StateStore, "open", lambda config: (_ for _ in ()).throw(
        RuntimeError("PRIVATE_EXCEPTION")))
    assert main(["summarize", "--all"]) == 4
    assert "PRIVATE_EXCEPTION" not in capsys.readouterr().err


def test_export_rejects_source_and_escape_targets(knowledge_workspace, tmp_path):
    cfg, source, _ = knowledge_workspace
    store = StateStore.open(cfg)
    snapshot = summarize_portfolio(store, cfg.registry)
    from tracefolio.errors import TracefolioError
    for target in (source / "export.json", tmp_path / "wide.json", cfg.state_root / ".." / "escape.json"):
        with pytest.raises(TracefolioError, match="unsafe_export"):
            export_portfolio(store, snapshot, target)


def test_fixture_pipeline_all_source_kinds_and_config_only_addition(knowledge_workspace, tmp_path):
    cfg, source, path = knowledge_workspace
    git_root = tmp_path / "git-source"
    git_root.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "Synthetic Author"],
                 ["config", "user.email", "author@example.invalid"]):
        subprocess.run(["git", "-C", str(git_root), *args], check=True, capture_output=True)
    (git_root / "main.py").write_text("checkpoint = 1\n")
    for args in (["add", "."], ["commit", "-qm", "vague update"]):
        subprocess.run(["git", "-C", str(git_root), *args], check=True, capture_output=True)
    project = cfg.registry.projects[0]
    project.sources.extend([
        SourceRoute(source_id="history", kind="local_git", root_env="TRACEFOLIO_GIT_ROOT",
                    include_paths=["*.py"]),
        SourceRoute(source_id="remote", kind="github_public", repository_env="TRACEFOLIO_REPO",
                    include_paths=["*.py"]),
    ])
    cfg.locators[("demo", "history")] = str(git_root)
    cfg.locators[("demo", "remote")] = "example/demo"
    providers = {"github_public": GitHubPublicProvider(FakeGitHub())}
    assert not collect_projects(cfg, ["demo"], providers=providers).partial
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    assert {e.source_id for e in packet.evidence} == {"code", "history", "remote"}
    proposal = path.parent / "proposal.json"
    write_proposal(proposal, proposal_for(packet))
    apply_proposal(store, proposal)
    assert summarize_portfolio(store, cfg.registry).entries[0].status == "ready"
    added = copy.deepcopy(project.model_dump())
    added.update(project_id="another", sources=[project.sources[0].model_dump()])
    cfg.registry.projects.append(ProjectRoute.model_validate(added))
    cfg.locators[("another", "code")] = str(source)
    collect_projects(cfg, ["another"])
    packet = store.read_packet(create_packet(store, "another").packet_id)
    write_proposal(proposal, proposal_for(packet))
    apply_proposal(store, proposal)
    result = summarize_portfolio(store, cfg.registry)
    assert result.configured_project_count == 2
    assert all(entry.status == "ready" and len(entry.works) == 1 for entry in result.entries)
