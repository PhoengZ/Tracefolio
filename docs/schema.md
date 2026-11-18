# Contracts and storage

Pydantic models in `src/tracefolio/contracts` are the contract source. `python -m tracefolio.schema_export` regenerates the eight offline Draft 2020-12 schemas; `--check` detects drift. Runtime validators enforce additional binding, identity, reference, readiness, and privacy rules beyond JSON Schema. All public artifacts use schema version `1.0.0`, English keys, `language: en`, snake_case, and UTC timestamps ending in `Z`. Narratives are English by harness instruction; Unicode names and source code remain valid.

Logical project/work/source IDs are lowercase kebab-case. Evidence is content addressed after sanitization and normalization. A work's identity is `(project_id, work_id)`; record revisions increment on semantic replacement. Source changes add evidence and invalidate affected works, without rewriting historical generation files. History rewrites persist `history_reconciliation_required` coverage until affected work and evidence have been analyzed again, even when replayed evidence already exists. Repeated identical proposals are idempotent. A packet digest and collection/base-generation revision bind proposals to a precise context.

Private state layout:

```text
policy.json
evidence/<evidence-id>.json
collections/<collection-id>.json
packets/<packet-id>/packet.json
projects/<project-id>/CURRENT
projects/<project-id>/revisions/<revision-id>/state.json
projects/<project-id>/generations/<generation-id>/project.json
projects/<project-id>/generations/<generation-id>/works/<work-id>.json
locks/<project-id>/
proposals/                         # harness writes here
exports/portfolio.json             # optional private aggregate
```

CURRENT contains a control-revision ID. Read its ledger once, then select its generation. The ledger holds collection cursors, pending analysis, invalidated works, exclusions, and proposal receipts. Generation files are immutable; summary overlays reflect newer gaps/review needs. Do not select a generation by directory order or newest timestamp.

Evidence records carry revision/path, attribution, completeness, sanitization markers, payload, and optional normalized notebook cell provenance. Stored evidence is already sanitized; diffs can include deletions and revert/merge context, which the harness must interpret rather than count as achievements.

Packets include project context, scoped work routes, a bounded evidence subset, prior work context, known work identities, allowed IDs, pending IDs, and coverage. Full existing-work evidence is required for replacement; oversized context blocks rather than silently truncating. Collection and analysis coverage are distinct. `unavailable_content` exclusions keep coverage partial; irrelevant/duplicate exclusions are explicit accounting decisions.

Portfolio entries include every selected configured project with status `ready`, `partial`, `pending`, `error`, or `disabled`. Last-good data is retained with current coverage and invalidation. Configured but unanalyzed work units appear as pending placeholders with unknowns and no claims. Ready requires a semantic generation, complete collection/analysis, represented configured works, no unresolved claims, and no current source/route errors. `unreviewed` still means an owner has not verified the machine narrative; ready is an analysis-completeness label.
