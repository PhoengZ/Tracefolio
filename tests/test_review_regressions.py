import copy
import json
import os
import subprocess
import sys

import pytest
from test_github_source import OLDER, SHA, FakeGitHub, req
from test_local_sources import commit, git, request
from test_proposals import proposal_for, write_proposal

from tracefolio.collection import collect_projects
from tracefolio.errors import TracefolioError
from tracefolio.investigation import create_packet, extend_packet
from tracefolio.portfolio import export_portfolio, summarize_portfolio
from tracefolio.proposals import apply_proposal
from tracefolio.sources.base import ContextRequest
from tracefolio.sources.github_public import GitHubPublicProvider
from tracefolio.sources.github_transport import ApiResponse
from tracefolio.sources.local_directory import LocalDirectoryProvider
from tracefolio.sources.local_git import LocalGitProvider
from tracefolio.state import StateStore


def init_git(root):
    for args in (("init", "-q"), ("config", "user.name", "Synthetic Author"),
                 ("config", "user.email", "author@example.invalid")):
        git(root, *args)


def test_git_binding_cannot_escape_via_enclosing_repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    init_git(root)
    allowed = root / "allowed"
    allowed.mkdir()
    (allowed / "own.py").write_text("own = 1")
    commit(root, "outside = 1\n", filename="neighbor.py")
    git(root, "add", ".")
    git(root, "commit", "-qm", "own source")
    provider = LocalGitProvider()
    with pytest.raises(TracefolioError, match="git_root_required"):
        provider.collect_source(request(allowed), None)
    with pytest.raises(TracefolioError, match="git_root_required"):
        provider.read_context(ContextRequest(request(allowed), git(root, "rev-parse", "HEAD"),
                                              "neighbor.py", "packet-test"))


def test_backward_reset_invalidates_ready_work_and_surfaces_reconciliation(knowledge_workspace):
    cfg, source, path = knowledge_workspace
    init_git(source)
    first = commit(source, "checkpoint = 1\n")
    second = commit(source, "checkpoint = 2\n")
    cfg.registry.projects[0].sources[0].kind = "local_git"
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    data = proposal_for(packet)
    reference = next(e.evidence_id for e in packet.evidence if e.revision == second)
    data["work_updates"][0]["claims"][0]["evidence_refs"] = [reference]
    data["work_updates"][0]["provenance"]["evidence_refs"] = [reference]
    proposal = path.parent / "proposal.json"
    write_proposal(proposal, data)
    apply_proposal(store, proposal)
    git(source, "reset", "--hard", first)
    collect_projects(cfg, ["demo"])
    collect_projects(cfg, ["demo"])
    entry = summarize_portfolio(store, cfg.registry).entries[0]
    assert entry.status == "partial"
    assert entry.works[0].claims[0].status == "needs_review"
    assert any("history_reconciliation_required" in s.limitations for s in entry.coverage.sources)
    fresh = store.read_packet(create_packet(store, "demo").packet_id)
    assert fresh.prior_works and fresh.prior_works[0].analysis_status == "partial"
    context = extend_packet(store, ContextRequest(store.source_request("demo", "code"),
                                                  first, "main.py", fresh.packet_id))
    fresh = store.read_packet(context.packet_id)
    current_ref = next(e.evidence_id for e in fresh.evidence if e.origin == "context")
    data = proposal_for(fresh)
    data["work_updates"][0]["claims"][0].update(
        statement="Current source initializes the checkpoint to one.", evidence_refs=[current_ref])
    data["work_updates"][0]["provenance"]["evidence_refs"] = [current_ref]
    write_proposal(proposal, data)
    apply_proposal(store, proposal)
    assert summarize_portfolio(store, cfg.registry).entries[0].status == "ready"


def test_snapshot_verification_resumes_under_byte_budget(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("x" * 20)
    provider = LocalDirectoryProvider()
    source = request(root, kind="local_directory", max_total_source_bytes=25)
    cursor = None
    for _ in range(9):
        result = provider.collect_source(source, cursor)
        cursor = result.cursor
        if result.coverage.state == "complete":
            break
    assert result.coverage.state == "complete" and cursor.head
    assert len(cursor.inventory) == 3


def test_github_finishes_pinned_range_before_following_new_head():
    class MovingGitHub(FakeGitHub):
        def __init__(self):
            super().__init__()
            self.current = SHA
            self.history_pages = [[{"sha": SHA}], [{"sha": OLDER}]]

        def get_json(self, path, params=None):
            if path == "/repos/example/demo/commits/main":
                return ApiResponse({"sha": self.current})
            if "/compare/" in path:
                return ApiResponse({"status": "ahead", "commits": [{"sha": self.current}]})
            if path.endswith("/commits/" + "c" * 40):
                data = copy.deepcopy(super().get_json("/repos/example/demo/commits/" + SHA, params).data)
                data["sha"] = self.current
                return ApiResponse(data)
            return super().get_json(path, params)

    api = MovingGitHub()
    provider = GitHubPublicProvider(api)
    first = provider.collect_source(req(), None)
    api.current = "c" * 40
    second = provider.collect_source(req(), first.cursor)
    assert second.coverage.state == "partial" and second.cursor.head == SHA
    assert second.cursor.target is None
    third = provider.collect_source(req(), second.cursor)
    assert third.coverage.state == "complete" and third.cursor.head == api.current


@pytest.mark.parametrize("target", ["projects/demo/CURRENT", "policy.json", "evidence/fake.json"])
def test_exports_cannot_overwrite_internal_state(knowledge_workspace, target):
    cfg, _, _ = knowledge_workspace
    store = StateStore.open(cfg)
    path = cfg.state_root / target
    store._atomic_json(path, {"preserve": True})
    before = path.read_bytes()
    with pytest.raises(TracefolioError, match="unsafe_export"):
        export_portfolio(store, summarize_portfolio(store, cfg.registry), path)
    assert path.read_bytes() == before


def test_cli_json_is_utf8_even_with_legacy_stdio_encoding():
    program = 'from tracefolio.cli import emit; emit({"label": "Caf\\u00e9 \\u0e01"})'
    result = subprocess.run([sys.executable, "-c", program], capture_output=True,
                            env=dict(os.environ, PYTHONIOENCODING="cp874"), check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout.decode("utf-8"))["label"] == "Caf\u00e9 \u0e01"


@pytest.mark.parametrize("private_path", ["/srv/internal/private.py", "/data/private.py",
                                          r"\\server\share\private.py"])
def test_generic_absolute_paths_redacted_from_packets_and_rejected_in_proposals(
        knowledge_workspace, private_path):
    cfg, source, path = knowledge_workspace
    (source / "one.py").write_text("# checkpoint " + private_path)
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    assert private_path not in packet.model_dump_json()
    assert private_path not in store.read_evidence(packet.evidence[0].evidence_id).payload
    data = proposal_for(packet)
    data["project_summary"] = "See " + private_path
    proposal = path.parent / "proposal.json"
    write_proposal(proposal, data)
    with pytest.raises(TracefolioError, match="unsafe_proposal_content"):
        apply_proposal(store, proposal)
