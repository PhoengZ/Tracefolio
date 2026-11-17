import copy
import json

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from tracefolio.collection import collect_projects
from tracefolio.errors import TracefolioError
from tracefolio.investigation import create_packet
from tracefolio.proposals import apply_proposal
from tracefolio.schema_export import schemas
from tracefolio.state import StateStore


def proposal_for(packet, works=None):
    ev = packet.evidence[0].evidence_id
    if works is None:
        works = [{
            "work_id": "work-a", "title": "Checkpoint handling",
            "actions": ["Implemented checkpoint handling."],
            "claims": [{"claim_id": "checkpoint", "statement": "Adds checkpoint handling.",
                        "status": "observed", "evidence_refs": [ev]}],
            "implementation_status": "implemented", "analysis_status": "ready",
            "provenance": {"evidence_refs": [ev], "source_ids": ["code"]},
        }]
    return {
        "packet_id": packet.packet_id, "packet_digest": packet.packet_digest,
        "project_id": packet.project_id, "base_generation_id": packet.base_generation_id,
        "collection_revision": packet.collection_revision,
        "project_summary": "A synthetic checkpoint implementation.",
        "work_updates": works, "analyzed_evidence_ids": packet.allowed_evidence_ids,
    }


@pytest.fixture
def prepared(knowledge_workspace):
    cfg, source, config_path = knowledge_workspace
    collect_projects(cfg, ["demo"])
    store = StateStore.open(cfg)
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    path = config_path.parent / "proposal.json"
    data = proposal_for(packet)
    return store, source, path, data


def write_proposal(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_valid_proposal_publishes_separate_project_work_json(prepared):
    store, _, path, data = prepared
    write_proposal(path, data)
    result = apply_proposal(store, path)
    current = store.current("demo")
    assert result.status == "published" and current.project.work_ids == ["work-a"]
    assert current.works[0].review_status == "unreviewed"
    for name, record in (("project", current.project), ("work", current.works[0])):
        Draft202012Validator(schemas()[name], format_checker=FormatChecker()).validate(
            record.model_dump(mode="json"))
    assert store.read_state("demo").pending_analysis == []


def test_foreign_or_missing_evidence_rejected_and_backlog_unchanged(prepared):
    store, _, path, data = prepared
    before = store.read_state("demo")
    data["work_updates"][0]["claims"][0]["evidence_refs"] = ["ev-" + "f" * 64]
    write_proposal(path, data)
    with pytest.raises(TracefolioError):
        apply_proposal(store, path)
    assert store.current("demo") is None
    assert store.read_state("demo") == before


@pytest.mark.parametrize("change", ["packet", "base"])
def test_packet_and_base_generation_conflicts(prepared, change):
    store, _, path, data = prepared
    data["packet_digest" if change == "packet" else "base_generation_id"] = "wrong"
    write_proposal(path, data)
    with pytest.raises(TracefolioError):
        apply_proposal(store, path)
    assert store.current("demo") is None


def test_new_collection_invalidates_old_packet_without_base_generation_change(prepared):
    store, source, path, data = prepared
    (source / "one.py").write_text("checkpoint = 2")
    collect_projects(store.config, ["demo"])
    write_proposal(path, data)
    with pytest.raises(TracefolioError, match="stale"):
        apply_proposal(store, path)
    assert store.current("demo") is None


@pytest.mark.parametrize("kind", ["metric", "deployment", "review", "ownership"])
def test_claim_authority_cannot_be_invented(prepared, kind):
    store, _, path, data = prepared
    work = data["work_updates"][0]
    if kind == "metric":
        work["claims"][0]["metric"] = {
            "value": "80", "unit": "percent", "scope": "runtime reduction",
            "evidence_refs": data["analyzed_evidence_ids"],
        }
    elif kind == "deployment":
        work["implementation_status"] = "deployed"
    elif kind == "review":
        work["review_status"] = "user_reviewed"
    else:
        work["contribution"] = "self_authored"
    write_proposal(path, data)
    with pytest.raises(TracefolioError):
        apply_proposal(store, path)


@pytest.mark.parametrize("accounting", ["missing", "duplicate", "foreign"])
def test_every_packet_evidence_id_accounted_for_once(prepared, accounting):
    store, _, path, data = prepared
    if accounting == "missing":
        data["analyzed_evidence_ids"] = []
    elif accounting == "duplicate":
        data["analyzed_evidence_ids"] *= 2
    else:
        data["analyzed_evidence_ids"].append("ev-" + "f" * 64)
    write_proposal(path, data)
    with pytest.raises(TracefolioError):
        apply_proposal(store, path)
    assert store.read_state("demo").pending_analysis


def test_reapply_is_noop(prepared):
    store, _, path, data = prepared
    write_proposal(path, data)
    first = apply_proposal(store, path)
    before = store.current("demo")
    second = apply_proposal(store, path)
    after = store.current("demo")
    assert second.status == "noop" and second.generation_id == first.generation_id
    assert after.project == before.project and after.works == before.works


def test_partial_proposal_upserts_without_dropping_unseen_works(prepared):
    store, source, path, data = prepared
    second = copy.deepcopy(data["work_updates"][0])
    second["work_id"] = "work-b"
    data["work_updates"].append(second)
    write_proposal(path, data)
    apply_proposal(store, path)
    before = store.current("demo").work("work-b")
    (source / "one.py").write_text("checkpoint = 2")
    collect_projects(store.config, ["demo"])
    packet = store.read_packet(create_packet(store, "demo").packet_id)
    update = proposal_for(packet)
    write_proposal(path, update)
    apply_proposal(store, path)
    assert store.current("demo").work("work-b") == before
    assert store.current("demo").work("work-a").record_revision == 2
    assert store.current("demo").project.work_ids == ["work-a", "work-b"]


def test_ready_work_without_evidence_rejected(prepared):
    store, _, path, data = prepared
    data["work_updates"] = [{"work_id": "fabricated", "title": "No evidence",
                             "analysis_status": "ready"}]
    write_proposal(path, data)
    with pytest.raises(TracefolioError):
        apply_proposal(store, path)


def test_private_values_in_model_output_are_rejected(prepared):
    store, _, path, data = prepared
    data["project_summary"] = "See https://private.invalid/api"
    write_proposal(path, data)
    with pytest.raises(TracefolioError, match="unsafe_proposal"):
        apply_proposal(store, path)


def test_deleted_or_retired_work_fields_rejected(prepared):
    store, _, path, data = prepared
    data["retire_work_ids"] = ["work-a"]
    write_proposal(path, data)
    with pytest.raises(TracefolioError):
        apply_proposal(store, path)


def test_ready_work_cannot_contain_needs_review_claim(prepared):
    store, _, path, data = prepared
    data["work_updates"][0]["claims"][0]["status"] = "needs_review"
    write_proposal(path, data)
    with pytest.raises(TracefolioError):
        apply_proposal(store, path)


def test_claim_cannot_use_evidence_it_excludes(prepared):
    store, _, path, data = prepared
    data["excluded_evidence"] = [{"evidence_id": data["analyzed_evidence_ids"][0],
                                 "reason": "unavailable_content"}]
    data["analyzed_evidence_ids"] = []
    write_proposal(path, data)
    with pytest.raises(TracefolioError):
        apply_proposal(store, path)


@pytest.mark.parametrize("fault", ["project_identity", "missing_work"])
def test_publication_rejects_inconsistent_project_work_graph(prepared, fault):
    from tracefolio.contracts.knowledge import ProjectRecord
    store, _, _, _ = prepared
    ledger = store.read_state("demo")
    project = ProjectRecord(project_id="other" if fault == "project_identity" else "demo",
                            record_revision=1, title="Wrong graph", summary="Wrong graph",
                            work_ids=["absent"] if fault == "missing_work" else [],
                            coverage=ledger.coverage())
    with pytest.raises(TracefolioError, match="publication_integrity"):
        store.publish("demo", None, project, [], ledger)
    assert store.current("demo") is None


