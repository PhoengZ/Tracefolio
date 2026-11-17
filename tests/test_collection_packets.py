import copy
import json

import pytest

from tracefolio.collection import collect_projects
from tracefolio.contracts.knowledge import Claim, ProjectRecord, Provenance, WorkRecord
from tracefolio.errors import TracefolioError
from tracefolio.investigation import create_packet, extend_packet
from tracefolio.private_config import load_config
from tracefolio.sources.base import ContextRequest
from tracefolio.state import StateStore


@pytest.fixture
def workspace(tmp_path, monkeypatch, route_payload):
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.py").write_text("checkpoint = 1\n")
    private = tmp_path / "private"
    private.mkdir()
    path = private / "routes.json"
    path.write_text(json.dumps(route_payload), encoding="utf-8")
    monkeypatch.setenv("TRACEFOLIO_DEMO_ROOT", str(source))
    monkeypatch.setenv("TRACEFOLIO_STATE_DIR", str(tmp_path / "state"))
    return load_config(path), source, path


def test_unchanged_collection_is_noop(workspace):
    cfg, _, _ = workspace
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    first = store.read_state("demo")
    collect_projects(cfg, ["demo"])
    after = store.read_state("demo")
    assert after.collection_revision == first.collection_revision
    assert after.pending_analysis == first.pending_analysis
    assert len(after.evidence_ids) == 1


def test_add_route_without_code_change_and_source_isolation(workspace, monkeypatch):
    cfg, _source, path = workspace
    collect_projects(cfg, ["demo"])
    payload = json.loads(path.read_text())
    other = copy.deepcopy(payload["projects"][0])
    other["project_id"] = "second"
    payload["projects"].append(other)
    path.write_text(json.dumps(payload))
    new = load_config(path)
    collect_projects(new, ["second"])
    store = StateStore.open(new)
    assert store.read_state("demo").evidence_ids != store.read_state("second").evidence_ids
    assert len(store.read_state("demo").evidence_ids) == 1


def test_changed_source_binding_requires_reconciliation(workspace, tmp_path, monkeypatch):
    cfg, _, path = workspace
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    before = store.read_state("demo")
    newroot = tmp_path / "another"
    newroot.mkdir()
    (newroot / "one.py").write_text("other = 2\n")
    monkeypatch.setenv("TRACEFOLIO_DEMO_ROOT", str(newroot))
    collect_projects(load_config(path), ["demo"])
    after = store.read_state("demo")
    assert after.sources["code"].fingerprint != before.sources["code"].fingerprint
    assert len(after.evidence_ids) == 2


def test_one_source_failure_keeps_other_projects(workspace):
    cfg, source, path = workspace
    payload = json.loads(path.read_text())
    other = copy.deepcopy(payload["projects"][0])
    other["project_id"] = "second"
    payload["projects"].append(other)
    path.write_text(json.dumps(payload))
    cfg = load_config(path)
    collect_projects(cfg, ["demo", "second"])
    (source / "one.py").write_text("other = 2\n")

    from tracefolio.sources.local_directory import LocalDirectoryProvider
    original = LocalDirectoryProvider()

    class Selective:
        def collect_source(self, req, previous):
            if req.project_id == "demo":
                raise TracefolioError("synthetic_source_failure", exit_code=3)
            return original.collect_source(req, previous)

    report = collect_projects(cfg, ["demo", "second"],
                              providers={"local_directory": Selective()})
    store = StateStore.open(cfg)
    assert store.read_state("demo").sources["code"].errors == ["synthetic_source_failure"]
    assert len(store.read_state("second").evidence_ids) == 2
    assert report.partial


def test_packet_budget_tracks_unanalyzed_backlog(workspace):
    cfg, source, _ = workspace
    (source / "one.py").write_text("x = " + repr("A" * 650))
    (source / "two.py").write_text("x = " + repr("B" * 650))
    cfg.registry.limits.max_packet_bytes = 2400
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    result = create_packet(store, "demo")
    assert result.status == "created"
    packet = store.read_packet(result.packet_id)
    assert len(packet.pending_evidence_ids) == 1
    assert len(store.read_state("demo").pending_analysis) == 2
    assert len(packet.model_dump_json().encode()) <= 2400


def test_shared_work_across_sources_has_one_identity(workspace):
    cfg, _, path = workspace
    payload = json.loads(path.read_text())
    source = payload["projects"][0]["sources"][0]
    source["work_units"] = [{"work_id": "shared", "context": "Investigate work.",
                             "include_paths": ["**/*.py"]}]
    other = copy.deepcopy(source)
    other["source_id"] = "second"
    payload["projects"][0]["sources"].append(other)
    path.write_text(json.dumps(payload))
    cfg = load_config(path)
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    assert packet.configured_work_ids == ["shared"]


def seed_generation(store, project_id="demo"):
    state = store.read_state(project_id)
    ev = state.evidence_ids[0]
    work = WorkRecord(
        project_id=project_id, work_id="work-a", record_revision=1, title="Checkpoint work",
        claims=[Claim(claim_id="checkpoint", statement="Adds a checkpoint.",
                      status="observed", evidence_refs=[ev])],
        provenance=Provenance(source_ids=["code"], evidence_refs=[ev]),
        analysis_status="ready",
    )
    project = ProjectRecord(project_id=project_id, record_revision=1, title="Demo",
                            summary="Checkpoint work.", source_ids=["code"],
                            work_ids=["work-a"], evidence_ids=[ev],
                            coverage=state.coverage())
    state.pending_analysis = []
    store.publish(project_id, None, project, [work], state)


def test_invalidation_overlay_does_not_rewrite_history(workspace):
    cfg, source, _ = workspace
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    seed_generation(store)
    old = store.current("demo")
    (source / "one.py").write_text("checkpoint = 2\n")
    collect_projects(cfg, ["demo"])
    result = create_packet(store, "demo")
    packet = store.read_packet(result.packet_id)
    assert packet.prior_works[0].claims[0].status == "needs_review"
    assert store.current("demo").generation_id == old.generation_id
    assert store.current("demo").works[0].claims[0].status == "observed"


def test_required_prior_work_context_cannot_be_silently_dropped(workspace):
    cfg, source, _ = workspace
    (source / "one.py").write_text("x = " + repr("A" * 3000))
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    seed_generation(store)
    (source / "one.py").write_text("x = 2")
    collect_projects(cfg, ["demo"])
    cfg.registry.limits.max_packet_bytes = 1500
    result = create_packet(store, "demo")
    assert result.status == "blocked_budget" and result.packet_id is None


def test_context_request_is_bound_to_revision_and_scope(workspace):
    cfg, _, _ = workspace
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    req = store.source_request("demo", "code")
    with pytest.raises(TracefolioError, match="context_revision"):
        extend_packet(store, ContextRequest(req, "unseen-revision", "one.py", packet.packet_id))
    with pytest.raises(TracefolioError, match="context_out_of_scope"):
        extend_packet(store, ContextRequest(req, packet.evidence[0].revision,
                                           ".env", packet.packet_id))


def test_lock_conflict_and_interrupted_state_pointer_preserve_previous(workspace, monkeypatch):
    cfg, _, _ = workspace
    store = StateStore.open(cfg)
    with store.lock("demo"), pytest.raises(TracefolioError, match="project_locked"), store.lock("demo"):
        pass
    state = store.read_state("demo")
    state.collection_revision = 1
    with store.lock("demo"):
        store.save_state("demo", state)
    original = store._atomic_json

    def fail_pointer(path, data):
        if path.name == "CURRENT":
            raise TracefolioError("synthetic_crash", exit_code=4)
        return original(path, data)

    monkeypatch.setattr(store, "_atomic_json", fail_pointer)
    state.collection_revision = 2
    with pytest.raises(TracefolioError, match="synthetic_crash"), store.lock("demo"):
        store.save_state("demo", state)
    assert store.read_state("demo").collection_revision == 1


def test_collect_and_packet_cli_emit_only_safe_json(workspace, monkeypatch, capsys):
    from tracefolio.cli import main
    _, _, config_path = workspace
    monkeypatch.delenv("TRACEFOLIO_CONFIG_FILES", raising=False)
    monkeypatch.setenv("TRACEFOLIO_CONFIG", str(config_path))
    assert main(["collect", "--all"]) == 0
    assert json.loads(capsys.readouterr().out)["projects"][0]["status"] == "complete"
    assert main(["packet", "create", "--project", "demo"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["packet_id"].startswith("packet-")
    assert str(config_path.parent.parent) not in output


def test_changed_explicit_redaction_policy_requires_new_state(workspace, monkeypatch):
    cfg, _, path = workspace
    StateStore.open(cfg)
    payload = json.loads(path.read_text())
    payload["privacy"] = {"redact_values_env": "TRACEFOLIO_EXTRA_REDACTIONS"}
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("TRACEFOLIO_EXTRA_REDACTIONS", '["https://private.invalid/"]')
    changed = load_config(path)
    with pytest.raises(TracefolioError, match="privacy_policy_changed"):
        StateStore.open(changed)


def test_revisited_snapshot_content_returns_to_analysis_backlog(workspace):
    cfg, source, _ = workspace
    store = StateStore.open(cfg)
    collect_projects(cfg, ["demo"])
    initial = store.read_state("demo")
    original_id = initial.evidence_ids[0]
    initial.pending_analysis = []
    with store.lock("demo"):
        store.save_state("demo", initial)
    (source / "one.py").write_text("checkpoint = 2\n")
    collect_projects(cfg, ["demo"])
    middle = store.read_state("demo")
    middle.pending_analysis = []
    with store.lock("demo"):
        store.save_state("demo", middle)
    (source / "one.py").write_text("checkpoint = 1\n")
    collect_projects(cfg, ["demo"])
    assert store.read_state("demo").pending_analysis == [original_id]



