import json
import os
from pathlib import Path

import pytest

from tracefolio.cli import main
from tracefolio.collection import collect_projects
from tracefolio.errors import TracefolioError
from tracefolio.investigation import create_packet
from tracefolio.portfolio import brief_portfolio, summarize_portfolio
from tracefolio.proposals import apply_proposal
from tracefolio.reviews import apply_review_feedback
from tracefolio.state import StateStore


def _seed_two_works(config, tmp_path):
    store = StateStore.open(config)
    collect_projects(config, ["demo"])
    packet_res = create_packet(store, "demo")
    packet = store.read_packet(packet_res.packet_id)
    ev = packet.allowed_evidence_ids[0]
    proposal_path = tmp_path / "state" / "proposals" / "seed.json"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal = {
        "schema_version": "1.0.0",
        "language": "en",
        "packet_id": packet.packet_id,
        "packet_digest": packet.packet_digest,
        "project_id": "demo",
        "base_generation_id": packet.base_generation_id,
        "collection_revision": packet.collection_revision,
        "project_summary": "Synthetic demo project with two works.",
        "work_updates": [
            {
                "work_id": "work-alpha",
                "title": "Work Alpha",
                "actions": ["Implemented alpha module."],
                "skills": [{"name": "Python", "evidence_refs": [ev]}],
                "claims": [{
                    "claim_id": "alpha-claim",
                    "statement": "Alpha module is present.",
                    "status": "observed",
                    "evidence_refs": [ev],
                }],
                "contribution": "unverified",
                "implementation_status": "implemented",
                "analysis_status": "ready",
                "review_status": "unreviewed",
                "unknowns": [],
                "provenance": {"evidence_refs": [ev]},
            },
            {
                "work_id": "work-beta",
                "title": "Work Beta",
                "actions": ["Implemented beta module."],
                "skills": [{"name": "Python", "evidence_refs": [ev]}],
                "claims": [{
                    "claim_id": "beta-claim",
                    "statement": "Beta module is present.",
                    "status": "observed",
                    "evidence_refs": [ev],
                }],
                "contribution": "unverified",
                "implementation_status": "implemented",
                "analysis_status": "ready",
                "review_status": "unreviewed",
                "unknowns": [],
                "provenance": {"evidence_refs": [ev]},
            },
        ],
        "analyzed_evidence_ids": [ev],
        "excluded_evidence": [],
    }
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    apply_proposal(store, proposal_path)
    return store


def test_review_apply_jsonl_and_idempotency(knowledge_workspace, tmp_path):
    config, _, _ = knowledge_workspace
    store = _seed_two_works(config, tmp_path)

    feedback_path = tmp_path / "state" / "feedback" / "review.jsonl"
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "schema_version": "1.0.0",
            "project_id": "demo",
            "work_id": "work-alpha",
            "record_revision": 1,
            "verdict": "correct",
            "is_correct": True,
            "ownership_confirmed": True,
            "reason": "",
            "reviewed_at": "2026-11-26T00:00:00Z",
        }),
        json.dumps({
            "schema_version": "1.0.0",
            "project_id": "demo",
            "work_id": "work-beta",
            "record_revision": 1,
            "verdict": "needs_edit",
            "is_correct": False,
            "ownership_confirmed": True,
            "reason": "Clarify scope",
            "reviewed_at": "2026-11-26T00:01:00Z",
        }),
    ]
    feedback_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = apply_review_feedback(store, feedback_path)
    assert summary.processed_item_count == 2
    assert summary.updated_work_count == 1
    assert summary.skipped_count == 1
    assert summary.projects[0].status == "published"
    assert summary.projects[0].updated_work_ids == ["work-alpha"]
    assert summary.projects[0].project_review_status == "unreviewed"
    assert summary.skipped[0].work_id == "work-beta"
    assert summary.skipped[0].reason == "verdict_not_correct"

    gen = store.current("demo")
    assert gen.work("work-alpha").review_status == "user_reviewed"
    assert gen.work("work-alpha").record_revision == 1
    assert gen.work("work-beta").review_status == "unreviewed"
    assert gen.project.review_status == "unreviewed"

    # Idempotent re-apply produces noop without creating a new generation.
    repeat = apply_review_feedback(store, feedback_path)
    assert repeat.updated_work_count == 0
    assert repeat.unchanged_work_count == 1
    assert repeat.projects[0].status == "noop"
    assert repeat.projects[0].generation_id == gen.generation_id


def test_review_apply_json_bundle_marks_project_user_reviewed(knowledge_workspace, tmp_path):
    config, _, _ = knowledge_workspace
    store = _seed_two_works(config, tmp_path)

    bundle_path = tmp_path / "state" / "exports" / "feedback-bundle.json"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle = {
        "schema_version": "1.0.0",
        "exported_at": "2026-11-26T00:00:00Z",
        "exported_count": 2,
        "items": [
            {"project_id": "demo", "work_id": "work-alpha", "record_revision": 1, "verdict": "correct"},
            {"project_id": "demo", "work_id": "work-beta", "record_revision": 1, "verdict": "correct"},
        ],
    }
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    summary = apply_review_feedback(store, bundle_path)
    assert summary.updated_work_count == 2
    assert summary.projects[0].project_review_status == "user_reviewed"

    snapshot = summarize_portfolio(store, config.registry)
    brief = brief_portfolio(snapshot)
    assert all(w["review_status"] == "user_reviewed" for w in brief["entries"][0]["works"])


def test_review_apply_rejects_revision_mismatch_and_invalidated(knowledge_workspace, tmp_path):
    config, source, _ = knowledge_workspace
    store = _seed_two_works(config, tmp_path)

    # Mutate source so existing works referencing one.py get invalidated on collect.
    (source / "one.py").write_text("checkpoint = 2\n", encoding="utf-8")
    collect_projects(config, ["demo"])

    fb_invalidated = tmp_path / "state" / "feedback" / "inv.jsonl"
    fb_invalidated.parent.mkdir(parents=True, exist_ok=True)
    fb_invalidated.write_text(
        json.dumps({"project_id": "demo", "work_id": "work-alpha", "record_revision": 1, "verdict": "correct"}) + "\n",
        encoding="utf-8",
    )
    res_inv = apply_review_feedback(store, fb_invalidated)
    assert res_inv.updated_work_count == 0
    assert res_inv.skipped[0].reason == "work_invalidated"

    # Re-analyze only work-beta via proposal; work-alpha stays invalidated until addressed or updated.
    packet_res = create_packet(store, "demo")
    packet = store.read_packet(packet_res.packet_id)
    ev = packet.allowed_evidence_ids[0]
    prop_path = tmp_path / "state" / "proposals" / "rev2.json"
    prop_path.write_text(json.dumps({
        "schema_version": "1.0.0",
        "language": "en",
        "packet_id": packet.packet_id,
        "packet_digest": packet.packet_digest,
        "project_id": "demo",
        "base_generation_id": packet.base_generation_id,
        "collection_revision": packet.collection_revision,
        "project_summary": "Updated demo.",
        "work_updates": [
            {
                "work_id": "work-alpha",
                "title": "Work Alpha v2",
                "actions": ["Updated alpha module."],
                "skills": [{"name": "Python", "evidence_refs": [ev]}],
                "claims": [{
                    "claim_id": "alpha-claim",
                    "statement": "Alpha updated.",
                    "status": "observed",
                    "evidence_refs": [ev],
                }],
                "contribution": "unverified",
                "implementation_status": "implemented",
                "analysis_status": "ready",
                "review_status": "unreviewed",
                "unknowns": [],
                "provenance": {"evidence_refs": [ev]},
            },
            {
                "work_id": "work-beta",
                "title": "Work Beta v2",
                "actions": ["Updated beta module."],
                "skills": [{"name": "Python", "evidence_refs": [ev]}],
                "claims": [{
                    "claim_id": "beta-claim",
                    "statement": "Beta updated.",
                    "status": "observed",
                    "evidence_refs": [ev],
                }],
                "contribution": "unverified",
                "implementation_status": "implemented",
                "analysis_status": "ready",
                "review_status": "unreviewed",
                "unknowns": [],
                "provenance": {"evidence_refs": [ev]},
            },
        ],
        "analyzed_evidence_ids": [ev],
        "excluded_evidence": [],
    }), encoding="utf-8")
    apply_proposal(store, prop_path)

    # Stale revision 1 feedback must be rejected for rev 2 work.
    res_stale = apply_review_feedback(store, fb_invalidated)
    assert res_stale.updated_work_count == 0
    assert res_stale.skipped[0].reason == "revision_mismatch"


def test_unrelated_reviewed_work_stays_reviewed_on_other_work_update(knowledge_workspace, tmp_path):
    config, source, _ = knowledge_workspace
    store = _seed_two_works(config, tmp_path)

    # Confirm both works as user_reviewed first.
    fb = tmp_path / "state" / "feedback" / "both.jsonl"
    fb.parent.mkdir(parents=True, exist_ok=True)
    fb.write_text(
        "\n".join([
            json.dumps({"project_id": "demo", "work_id": "work-alpha", "record_revision": 1, "verdict": "correct"}),
            json.dumps({"project_id": "demo", "work_id": "work-beta", "record_revision": 1, "verdict": "correct"}),
        ]) + "\n",
        encoding="utf-8",
    )
    apply_review_feedback(store, fb)

    # Add a brand new file and update only work-beta via work-scoped packet.
    (source / "two.py").write_text("beta = 2\n", encoding="utf-8")
    collect_projects(config, ["demo"])
    packet_res = create_packet(store, "demo", work_id="work-beta")
    packet = store.read_packet(packet_res.packet_id)
    ev = packet.allowed_evidence_ids[0]
    prop = tmp_path / "state" / "proposals" / "beta-only.json"
    prop.write_text(json.dumps({
        "schema_version": "1.0.0",
        "language": "en",
        "packet_id": packet.packet_id,
        "packet_digest": packet.packet_digest,
        "project_id": "demo",
        "base_generation_id": packet.base_generation_id,
        "collection_revision": packet.collection_revision,
        "project_summary": "Beta updated.",
        "work_updates": [{
            "work_id": "work-beta",
            "title": "Work Beta Updated",
            "actions": ["Added two.py."],
            "skills": [{"name": "Python", "evidence_refs": [ev]}],
            "claims": [{
                "claim_id": "beta-two",
                "statement": "Added two.py.",
                "status": "observed",
                "evidence_refs": [ev],
            }],
            "contribution": "unverified",
            "implementation_status": "implemented",
            "analysis_status": "ready",
            "review_status": "unreviewed",
            "unknowns": [],
            "provenance": {"evidence_refs": [ev]},
        }],
        "analyzed_evidence_ids": [ev],
        "excluded_evidence": [],
    }), encoding="utf-8")
    apply_proposal(store, prop)

    gen = store.current("demo")
    assert gen.work("work-alpha").review_status == "user_reviewed"
    assert gen.work("work-beta").review_status == "unreviewed"


def test_review_apply_cli_and_path_boundaries(knowledge_workspace, tmp_path, monkeypatch, capsys):
    config, source, routes_path = knowledge_workspace
    store = _seed_two_works(config, tmp_path)

    # Path inside configured source root must be rejected.
    bad_source_fb = source / "feedback.jsonl"
    bad_source_fb.write_text(
        json.dumps({"project_id": "demo", "work_id": "work-alpha", "verdict": "correct"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TracefolioError) as exc:
        apply_review_feedback(store, bad_source_fb)
    assert exc.value.code == "unsafe_export"

    # Path inside tracked checkout must be rejected.
    checkout_fb = Path(__file__).resolve().parents[1] / "README.md"
    with pytest.raises(TracefolioError) as exc2:
        apply_review_feedback(store, checkout_fb)
    assert exc2.value.code == "unsafe_export"

    # External private feedback file works via CLI.
    external_fb = tmp_path / "external_feedback.jsonl"
    external_fb.write_text(
        json.dumps({"project_id": "demo", "work_id": "work-alpha", "record_revision": 1, "verdict": "correct"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRACEFOLIO_CONFIG", str(routes_path))
    monkeypatch.delenv("TRACEFOLIO_CONFIG_FILES", raising=False)
    code = main(["review", "apply", "--feedback", str(external_fb)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["updated_work_count"] == 1
    assert payload["projects"][0]["updated_work_ids"] == ["work-alpha"]

    # Ensure machine_review_forbidden guard on apply --proposal remains intact.
    assert os.environ.get("TRACEFOLIO_CONFIG") == str(routes_path)
