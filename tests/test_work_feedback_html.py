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
def test_feedback_core_js_parsing_verdicts_and_roundtrip():
    html = HTML_PATH.read_text(encoding="utf-8")
    core_js = _extract_core_script(html)
    jsonl_text = EXAMPLE_JSONL_PATH.read_text(encoding="utf-8")
    single_work_text = EXAMPLE_WORK_PATH.read_text(encoding="utf-8")

    driver = r"""
const assert = require("assert");
const Core = globalThis.TracefolioFeedbackCore;
const payload = JSON.parse(process.argv[1]);

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
"""
    script = core_js + "\n" + driver
    result = subprocess.run(
        ["node", "-e", script, json.dumps({
            "jsonlText": jsonl_text,
            "singleWorkText": single_work_text,
        })],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"Node test failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
