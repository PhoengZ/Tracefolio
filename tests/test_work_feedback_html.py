import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tracefolio.contracts.knowledge import WorkRecord

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "tools" / "work-feedback.html"
EXAMPLE_JSONL_PATH = ROOT / "examples" / "works.example.jsonl"
EXAMPLE_WORK_PATH = ROOT / "examples" / "work.example.json"


def _extract_core_script(html: str) -> str:
    match = re.search(
        r'<script id="tracefolio-feedback-core">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None, "Missing tracefolio-feedback-core script block"
    return match.group(1)


def test_work_feedback_html_is_offline_and_xss_safe():
    html = HTML_PATH.read_text(encoding="utf-8")

    # Offline / privacy boundary: no external URLs, network APIs, or remote assets.
    forbidden_network = [
        "http://",
        "https://",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "sendBeacon",
        "@import",
    ]
    for token in forbidden_network:
        assert token not in html, f"Forbidden network/external token found: {token}"

    assert not re.search(r"<script[^>]+src=", html, flags=re.IGNORECASE)
    assert not re.search(r"<link[^>]+href=", html, flags=re.IGNORECASE)

    # XSS safety: untrusted work content must never flow through raw HTML sinks.
    forbidden_sinks = [
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ]
    for sink in forbidden_sinks:
        assert sink not in html, f"Forbidden DOM/eval sink found: {sink}"


def test_synthetic_works_example_jsonl_validates_against_work_contract():
    lines = [
        line.strip()
        for line in EXAMPLE_JSONL_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 3
    records = [WorkRecord.model_validate(json.loads(line)) for line in lines]
    assert {(r.project_id, r.work_id) for r in records} == {
        ("demo-ingestion", "incremental-sync"),
        ("demo-ingestion", "schema-validation-gate"),
        ("demo-analytics", "cohort-query-notes"),
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_work_feedback_html_scripts_pass_js_syntax_check():
    html = HTML_PATH.read_text(encoding="utf-8")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.DOTALL)
    assert len(scripts) == 2
    for idx, script in enumerate(scripts, start=1):
        result = subprocess.run(
            ["node", "--check"],
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 0, f"Script block #{idx} syntax error:\n{result.stderr}"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_feedback_core_js_parsing_verdicts_and_roundtrip(tmp_path):
    html = HTML_PATH.read_text(encoding="utf-8")
    core_js = _extract_core_script(html)
    jsonl_text = EXAMPLE_JSONL_PATH.read_text(encoding="utf-8")
    single_work_text = EXAMPLE_WORK_PATH.read_text(encoding="utf-8")

    driver = r"""
const assert = require("assert");
const Core = globalThis.TracefolioFeedbackCore;
const payload = JSON.parse(process.argv[2]);

// 1. Parse JSONL
const jsonlRes = Core.parseWorksText(payload.jsonlText, "works.example.jsonl");
assert.strictEqual(jsonlRes.warnings.length, 0);
assert.strictEqual(jsonlRes.works.length, 3);

// 2. Parse single WorkRecord JSON
const singleRes = Core.parseWorksText(payload.singleWorkText, "work.example.json");
assert.strictEqual(singleRes.warnings.length, 0);
assert.strictEqual(singleRes.works.length, 1);
assert.strictEqual(singleRes.works[0].key, "demo-ingestion::incremental-sync");

// 3. Parse brief portfolio snapshot
const briefPortfolio = JSON.stringify({
  configured_project_count: 1,
  entries: [
    {
      project_id: "demo-brief",
      status: "ready",
      title: "Demo Brief Project",
      works: [
        {
          work_id: "brief-work-1",
          title: "Brief Work",
          problem: "Synthetic brief problem",
          actions: ["Added brief test action."],
          contribution: "self_authored",
          implementation_status: "implemented",
          analysis_status: "ready",
          review_status: "unreviewed",
          unknowns: []
        }
      ]
    }
  ]
});
const briefRes = Core.parseWorksText(briefPortfolio, "brief.json");
assert.strictEqual(briefRes.warnings.length, 0);
assert.strictEqual(briefRes.works.length, 1);
assert.strictEqual(briefRes.works[0].project_id, "demo-brief");
assert.strictEqual(briefRes.works[0].project_id_inferred, false);

// 4. Infer project_id from directory path when missing on WorkRecord
const inferredRes = Core.parseWorksText(
  JSON.stringify({ work_id: "path-item", title: "Path Item" }),
  "state/projects/demo-folder/generations/gen-1/works/path-item.json"
);
assert.strictEqual(inferredRes.works.length, 1);
assert.strictEqual(inferredRes.works[0].project_id, "demo-folder");
assert.strictEqual(inferredRes.works[0].project_id_inferred, true);

// 5. Resilient JSONL parsing with malformed line
const mixedJsonl = '{"project_id":"p1","work_id":"w1","title":"Valid"}\n{not-valid-json\n';
const mixedRes = Core.parseWorksText(mixedJsonl, "mixed.jsonl");
assert.strictEqual(mixedRes.works.length, 1);
assert.strictEqual(mixedRes.warnings.length, 1);
assert.ok(mixedRes.warnings[0].includes("mixed.jsonl:2"));

// 6. Merge works and apply feedback verdicts
const merged = Core.mergeWorks(jsonlRes.works, briefRes.works);
assert.strictEqual(merged.works.length, 4);
assert.strictEqual(merged.added, 1);

const fbMap = {};
fbMap["demo-ingestion::incremental-sync"] = Core.createFeedbackEntry(
  "correct",
  "Verified exact implementation",
  undefined,
  "2026-11-19T00:00:00Z"
);
fbMap["demo-ingestion::schema-validation-gate"] = Core.createFeedbackEntry(
  "not_my_work",
  "Handled by platform team",
  undefined,
  "2026-11-19T00:01:00Z"
);
fbMap["demo-analytics::cohort-query-notes"] = Core.createFeedbackEntry(
  "misunderstood",
  "Wrote SQL helper only, not report spec",
  true,
  "2026-11-19T00:02:00Z"
);

const stats = Core.computeStats(merged.works, fbMap);
assert.strictEqual(stats.total, 4);
assert.strictEqual(stats.reviewed, 3);
assert.strictEqual(stats.unreviewed, 1);
assert.strictEqual(stats.correct, 1);
assert.strictEqual(stats.not_my_work, 1);
assert.strictEqual(stats.misunderstood, 1);
assert.strictEqual(stats.incorrect, 2);

// 7. Export JSONL (reviewedOnly) and round-trip import
const exportedItems = Core.buildFeedbackExport(merged.works, fbMap, { reviewedOnly: true });
assert.strictEqual(exportedItems.length, 3);
const exportedJsonl = Core.serializeFeedbackJsonl(exportedItems);
const parsedFeedback = Core.parseFeedbackText(exportedJsonl);
assert.strictEqual(parsedFeedback.warnings.length, 0);
assert.strictEqual(parsedFeedback.records.length, 3);

const reapplied = Core.applyFeedbackRecords(merged.works, {}, parsedFeedback.records);
assert.strictEqual(reapplied.matched, 3);
assert.strictEqual(reapplied.unmatched, 0);
assert.strictEqual(reapplied.feedbackMap["demo-ingestion::incremental-sync"].is_correct, true);
assert.strictEqual(reapplied.feedbackMap["demo-ingestion::schema-validation-gate"].ownership_confirmed, false);
assert.strictEqual(reapplied.feedbackMap["demo-analytics::cohort-query-notes"].ownership_confirmed, true);
assert.strictEqual(
  reapplied.feedbackMap["demo-analytics::cohort-query-notes"].reason,
  "Wrote SQL helper only, not report spec"
);

// 8. Dropping feedback JSONL into work importer warns cleanly instead of adding bogus works
const accidentalRes = Core.parseWorksText(exportedJsonl, "feedback.jsonl");
assert.strictEqual(accidentalRes.works.length, 0);
assert.strictEqual(accidentalRes.warnings.length, 3);

// 9. Export JSON bundle
const bundle = JSON.parse(Core.serializeFeedbackBundle(merged.works, fbMap, { reviewedOnly: false }));
assert.strictEqual(bundle.exported_count, 4);
assert.strictEqual(bundle.items.length, 4);
assert.strictEqual(bundle.summary.reviewed, 3);

// 10. Authorship Audit overlay parsing, risk classification (FP/FN/low_risk), and auto-detect
const auditJson = JSON.stringify({
  schema_version: "1.0.0",
  language: "en",
  scope: "Two-layer authorship audit",
  identity: { github_authenticated: true, configured_email_count: 1, derived_email_count: 1, effective_email_count: 2 },
  configured_project_count: 3,
  projects: [
    {
      project_id: "demo-ingestion",
      status: "audited",
      assessment: "mixed_or_shared",
      sources: [{
        source_id: "backend",
        kind: "github_public",
        assessment: "mixed_or_shared",
        components: [
          { component: "sync", verdict: "self_only", self_commits: 5, other_or_unmatched_commits: 0, merge_context_commits: 0, self_touched_files_count: 3, total_head_files_count: 3 },
          { component: "gate", verdict: "other_only", self_commits: 0, other_or_unmatched_commits: 4, merge_context_commits: 0, self_touched_files_count: 0, total_head_files_count: 2 }
        ]
      }],
      works: [
        {
          work_id: "incremental-sync",
          title: "Incremental sync",
          assessment: "self_supported",
          referenced_commit_counts: { self: 5 },
          referenced_path_counts: { self: 3 },
          referenced_components: ["sync"],
          claims: [{ claim_id: "c1", support_verdict: "self_supported", commit_support_counts: { self: 5 }, path_support_counts: {} }]
        },
        {
          work_id: "schema-validation-gate",
          title: "Schema gate",
          assessment: "other_authored_only",
          referenced_commit_counts: { other_account: 4 },
          referenced_path_counts: { other_account: 2 },
          referenced_components: ["gate"],
          claims: []
        }
      ]
    },
    {
      project_id: "demo-analytics",
      status: "audited",
      assessment: "unverifiable_non_git",
      sources: [],
      works: [
        {
          work_id: "cohort-query-notes",
          title: "Cohort notes",
          assessment: "unverifiable_non_git",
          referenced_commit_counts: {},
          referenced_path_counts: { unverifiable_non_git: 2 },
          referenced_components: ["notes"],
          claims: []
        }
      ]
    },
    {
      project_id: "demo-brief",
      status: "audited",
      assessment: "mixed_or_shared",
      sources: [],
      works: [
        {
          work_id: "brief-work-1",
          title: "Brief Work",
          assessment: "shared_supported",
          referenced_commit_counts: { self: 2, other_account: 3 },
          referenced_path_counts: {},
          referenced_components: ["api"],
          claims: []
        }
      ]
    }
  ]
});

// Auto-detect when audit JSON is dropped into parseWorksText
const autoDetect = Core.parseWorksText(auditJson, "authorship-component-audit.json");
assert.strictEqual(autoDetect.works.length, 0);
assert.strictEqual(autoDetect.warnings.length, 0);
assert.ok(autoDetect.auditReport && Object.keys(autoDetect.auditReport.auditMap).length === 4);

const auditMap = autoDetect.auditReport.auditMap;
assert.strictEqual(Core.classifyWorkRisk(merged.works[0], auditMap).bucket, "low_risk");
assert.strictEqual(Core.classifyWorkRisk(merged.works[0], auditMap).is_risky, false);
assert.strictEqual(Core.classifyWorkRisk(merged.works[1], auditMap).bucket, "suspected_fp");
assert.strictEqual(Core.classifyWorkRisk(merged.works[1], auditMap).is_risky, true);
assert.strictEqual(Core.classifyWorkRisk(merged.works[2], auditMap).bucket, "suspected_fn");
assert.strictEqual(Core.classifyWorkRisk(merged.works[2], auditMap).is_risky, true);
assert.strictEqual(Core.classifyWorkRisk(merged.works[3], auditMap).bucket, "suspected_fp");

const riskStats = Core.computeRiskStats(merged.works, fbMap, auditMap);
assert.strictEqual(riskStats.has_audit, true);
assert.strictEqual(riskStats.low_risk, 1);
assert.strictEqual(riskStats.suspected_fp, 2);
assert.strictEqual(riskStats.suspected_fn, 1);
assert.strictEqual(riskStats.total_risky, 3);
assert.strictEqual(riskStats.risky_reviewed, 2);
assert.strictEqual(riskStats.risky_unreviewed, 1);

const auditExport = Core.buildFeedbackExport(merged.works, fbMap, { reviewedOnly: true, auditMap: auditMap });
assert.strictEqual(auditExport[0].risk_bucket, "low_risk");
assert.strictEqual(auditExport[1].risk_bucket, "suspected_fp");
assert.strictEqual(auditExport[1].audit_assessment, "other_authored_only");

// 11. minor_touch_only -> suspected_fp; high self_share local shared (>=65%, no other_account) -> low_risk
const extraAudit = {
  "p::minor": { assessment: "minor_touch_only", self_share: 0.08, referenced_commit_counts: { unmatched_local_author: 4 }, referenced_path_counts: { shared: 1, unmatched_local_author: 10 } },
  "p::dominant": { assessment: "shared_supported", self_share: 0.88, referenced_commit_counts: { self: 9 }, referenced_path_counts: { self: 12, shared: 2 } },
  "p::excluded_other": { assessment: "other_authored_only", self_share: 0.0, referenced_commit_counts: { other_account: 10 }, referenced_path_counts: { other_account: 20 } },
  "p::excluded_minor": { assessment: "minor_touch_only", self_share: 0.04, referenced_commit_counts: { unmatched_local_author: 4 }, referenced_path_counts: { shared: 1, unmatched_local_author: 18 } }
};
assert.strictEqual(Core.classifyWorkRisk({ key: "p::minor" }, extraAudit).bucket, "suspected_fp");
assert.strictEqual(Core.classifyWorkRisk({ key: "p::dominant" }, extraAudit).bucket, "low_risk");

// 12. Works already excluded by LLM Authorship Gate (0 claims, 0 actions, unverified/unknown/partial) -> low_risk
const excludedWorkOther = {
  key: "p::excluded_other",
  claims: [],
  actions: [],
  contribution: "unverified",
  implementation_status: "unknown",
  analysis_status: "partial"
};
const excludedWorkMinor = {
  key: "p::excluded_minor",
  claims: [],
  actions: [],
  contribution: "unverified",
  implementation_status: "unknown",
  analysis_status: "partial"
};
const resExOther = Core.classifyWorkRisk(excludedWorkOther, extraAudit);
assert.strictEqual(resExOther.bucket, "low_risk");
assert.strictEqual(resExOther.is_risky, false);
const resExMinor = Core.classifyWorkRisk(excludedWorkMinor, extraAudit);
assert.strictEqual(resExMinor.bucket, "low_risk");
assert.strictEqual(resExMinor.is_risky, false);
"""
    script = core_js + "\n" + driver
    script_path = tmp_path / "feedback_core_driver.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path), json.dumps({
            "jsonlText": jsonl_text,
            "singleWorkText": single_work_text,
        })],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"Node test failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
