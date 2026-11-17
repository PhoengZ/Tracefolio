# Tracefolio Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. This artifact authorizes planning only; the owner will choose whether/where to implement.

**Goal:** Build a secure, configurable work-knowledge CLI that lets an existing LLM harness produce English, evidence-linked project/work JSON from local folders, local Git, and public GitHub.

**Architecture:** Source adapters normalize bounded evidence into private state. A harness reads sanitized investigation packets and submits typed proposals; deterministic validation publishes immutable per-project/work generations. A portfolio command aggregates every configured project with explicit coverage and gaps.

**Tech Stack:** Proposed Python 3.12+, Git CLI, Pydantic v2; standard-library argparse, urllib, hashlib, pathlib, and JSON; pytest, jsonschema, and Ruff for development. Resolve/test compatible versions during implementation. No LLM SDK, database server, browser automation, email client, or service framework.

**Spec:** ../specs/2026-11-17-tracefolio-design.md (authoritative proposed design).

## Global Constraints

- Python 3.12+; Windows and Linux are target platforms.
- All artifacts are UTF-8 JSON, English keys, snake_case fields, explicit schema_version, RFC 3339 UTC timestamps ending in Z, and stable logical IDs.
- Pydantic models are the single contract source; committed generated JSON Schemas use Draft 2020-12.
- No real personal endpoints, absolute source directories, credentials, or private work committed to application code.
- Private runtime state outside the source repository, selected by an environment variable.
- Adding another route with an existing kind requires no source-code edits.
- Select one or multiple explicit private registry files; confirmed public repositories use github_public as primary, with no automatic private-GitHub authentication or local-clone fallback.
- No LLM SDK or direct model API in Phase 1.
- No email connections, drafts, email sending, test emails, job applications, or any message delivery.
- No resume/PDF generation, public export, job search, scheduling, cloud bucket, or Notion integration in Phase 1.
- Machine-generated claims remain unreviewed; validation does not establish truth or ownership.
- Proposed commands below DO NOT exist yet. Implementation evidence is PENDING.

## Review Focus

Each condition below is explicitly tested in the owning task:
1. Partial packets must not replace unseen work or confuse collected evidence with analyzed evidence (T5/T6).
2. A credential in a removed diff line, commit message, README, file path, or exception must not leak into a packet, output, or diagnostics (T2/T8).
3. A rewritten branch, API pagination limit, or 429 must not advance a completed checkpoint or erase a good generation (T3/T4/T5).
4. A route outside Git or to somebody else's public repository must not imply self-authored professional experience (T3/T4/T6).
5. A stale proposal, interrupted publication, lock conflict, or junction/symlink escape must preserve the last valid state (T2/T5/T6).

## Repository status and implementation prerequisite

The owner has now designated and authorized initialization of a local product checkout and its origin remote. It contains copied planning documents and synthetic examples only, on an unborn main branch. No product implementation, commit, or push has occurred. Source configuration is a private proposal awaiting explicit owner confirmation; after that confirmation the owner authorizes execution of this plan. The actual user-specific directory and remote are intentionally omitted from these public planning documents.

- [ ] At execution start, confirm the owner's chosen repository and read applicable AGENTS.md files.
- [ ] Inspect git status --short, git branch --show-current, and git rev-parse --verify HEAD. Record actual branch/base commit below; if there is no initial commit, record that state explicitly.
- [ ] Confirm/create a dedicated feat/semantic-work-layer branch from the confirmed base before any product code OR test edit. Reuse an existing branch only if it belongs to this same task. Do not implement on a shared/default branch without explicit owner override.
- [ ] Follow the installed worktree/isolation workflow separately if needed. Preserve existing user changes; never stash/reset/delete/commit unrelated work just to satisfy the branch requirement.
- [ ] Ensure these planning documents travel into the chosen checkout without adding private routes.

| Execution evidence | Status |
|---|---|
| Target repository | Initialized locally per owner instruction; no product implementation yet |
| Task branch | PENDING execution |
| Starting commit / empty-repo state | PENDING execution |
| Python/Git versions | PENDING execution |
| Harness selected by owner | PENDING manual acceptance |

## Intended file map

All paths below are NEW proposed files unless the eventual repository already provides an equivalent; inspect and reuse before creating duplicates.

- pyproject.toml; .gitignore; README.md; AGENTS.md; .github/workflows/ci.yml.
- src/tracefolio/__init__.py; cli.py; errors.py.
- src/tracefolio/contracts/routes.py (config and budgets), evidence.py (collection/context), knowledge.py (project/work/claims), packets.py (packet/proposal), portfolio.py (aggregate).
- src/tracefolio/schema_export.py; schemas/{routes,evidence,collection,packet,proposal,project,work,portfolio}.schema.json.
- src/tracefolio/privacy.py (containment/redaction), private_config.py (env binding), state.py (atomic generations/locks).
- src/tracefolio/sources/base.py (internal requests/protocol), local_git.py, local_directory.py, github_public.py, github_transport.py.
- src/tracefolio/sources/notebooks.py (shared data-only cell-source normalization and safe notebook diffs).
- src/tracefolio/collection.py (cursor/evidence pipeline), investigation.py (bounded packets/context), proposals.py (validation/upsert/ledger), portfolio.py (aggregate).
- docs/harness-workflow.md; docs/security.md; docs/schema.md; docs/manual-acceptance.md.
- examples/routes.example.json; examples/project.example.json; examples/work.example.json (synthetic).
- tests/conftest.py; test_contracts.py; test_privacy.py; test_local_sources.py; test_github_source.py; test_collection_packets.py; test_proposals.py; test_portfolio_cli.py; test_privacy_regression.py.
- tests/fixtures/github/*.json (synthetic HTTP responses), tests/fixtures/proposals/*.json (synthetic analysis outputs). Runtime temp repositories/state must live in pytest temporary directories, not the checked-out source tree.

## Shared interfaces and identities

The exact model fields/enums are pinned by spec sections 5-9; no downstream task invents parallel dictionaries.
- routes.py: RouteRegistry, ProjectRoute, SourceRoute (discriminated by kind), Limits.
- evidence.py: EvidenceRecord, SourceCursor, CollectionResult, CollectionManifest, Coverage, ContextResult.
- knowledge.py: Claim, Skill, ProjectRecord, WorkRecord.
- packets.py: InvestigationPacket, AnalysisProposal.
- portfolio.py: PortfolioSnapshot, PortfolioEntry.
- errors.py: TracefolioError(code: str, safe_message: str); subclasses distinguish input/security (exit 2), partial (3), and fatal operational failure (4). Raw exception/config text is never safe_message.
- base.py: SourceRequest and ContextRequest are internal resolved-value objects, never artifact serializers. Define SourceProvider.collect_source(request: SourceRequest, previous: SourceCursor | None) -> CollectionResult and read_context(request: ContextRequest) -> ContextResult.
- state.py: StateStore.open(config: ResolvedConfig) -> StateStore; current(project_id: str) -> Generation | None; publish(project_id: str, expected_base: str | None, project: ProjectRecord, works: list[WorkRecord], ledger: AnalysisLedger) -> PublicationResult.
- private_config.py: load_config(config_path: Path) -> ResolvedConfig (registry, validated bindings, private state root, privacy policy). ResolvedConfig never supports artifact/model dumping.
- private_config.py: load_config_set(config_paths: list[Path]) -> ResolvedConfig composes disjoint project registries; load_config is its single-file wrapper. cli.main resolves exactly one of TRACEFOLIO_CONFIG or TRACEFOLIO_CONFIG_FILES (a JSON array of paths). Reject duplicate project/source identities, different shared settings, missing files, or ambiguous selection before source IO.
- generation IDs are opaque generated IDs; artifacts carry a monotonic per-project record_revision. Evidence IDs follow the spec's canonical SHA-256 rule. Work identity is (project_id, work_id).

Definitions such as Generation, AnalysisLedger, PublicationResult, CollectionReport, and PacketResult are internal dataclasses in the owning module, with safe serialize methods only where the CLI requires them. New public artifacts require schema updates; internal structs do not.

## Task 1: Establish portable contracts and safe configuration shape

**Files:** Create pyproject.toml, package init/cli/errors, contracts/*, schema_export.py, schemas/*, .gitignore, tests/test_contracts.py, and initial tests/conftest.py.
**Consumes:** Spec sections 5, 7, 11.
**Produces:** All shared public models; offline schema registry; initial CLI entry point tracefolio.

- [ ] Create the minimal package/test configuration after the branch prerequisite. Runtime dependency is Pydantic >=2,<3; development dependencies are pytest, jsonschema, and Ruff. Set Python >=3.12 and register tracefolio = tracefolio.cli:main. Do not add provider credentials.
- [ ] Write failing contract tests: test_routes_reject_literal_locator_fields, test_duplicate_ids_rejected, test_unknown_fields_rejected, test_all_artifacts_have_version, test_utc_timestamps_and_enums, test_project_and_work_validate_in_both_engines. Assert a route with root, url, or token fails; valid synthetic records pass Pydantic and Draft202012Validator with FormatChecker; inconsistent IDs/unsupported versions fail.
- [ ] Install proposed test tooling with python -m pip install -e ".[dev]" and run python -m pytest tests/test_contracts.py -q. Expect failure for the unimplemented contracts, not a passing placeholder.
- [ ] Implement the public models and generate schemas offline. Forbid extra fields, use explicit optional/null semantics, validate cross-record identifiers, and keep root/repository values out of schema examples.
- [ ] Define schema_export.main(argv: list[str] | None = None) -> int with --check for committed schema drift. Validation uses the locally registered schemas only; untrusted $schema/$ref values must not trigger network access.
- [ ] Rerun the contract tests; expect PASS. Run python -m tracefolio.schema_export --check; expect exit 0/no drift.
- [ ] Commit task-owned public files only: feat: define versioned work knowledge contracts.

Representative contract assertion:
~~~python
def test_routes_reject_literal_locator_fields():
    payload = minimal_route()
    payload["projects"][0]["sources"][0]["root"] = "/synthetic/private/root"
    with pytest.raises(ValidationError):
        RouteRegistry.model_validate(payload)
~~~

## Task 2: Enforce private bindings, containment, and redaction

**Files:** Create private_config.py, privacy.py, tests/test_privacy.py; expand conftest.py.
**Consumes:** RouteRegistry/Limits and TracefolioError.
**Produces:** load_config; PrivacyPolicy; resolve_allowed_path(root: Path, relative_path: str) -> Path; sanitize_text(text: str, policy: PrivacyPolicy) -> SanitizedText; secure_private_root(path: Path) -> None.

- [ ] Add failing tests for missing TRACEFOLIO_* bindings; source/config/state/output root nesting; drive/home roots; .. and symlink/junction escapes; unsafe IDs; unreadable paths; and unknown source kinds. Use synthetic temporary directories.
- [ ] Add test_disabled_project_does_not_resolve_missing_binding_or_read_source; assert its structural IDs are still validated and summary status remains disabled. Include absolute source URLs in the redaction regression cases.
- [ ] Add registry-composition tests for public-only selection, local-only selection, both registries, both selector variables being set, duplicate project IDs, different global limits, and an unavailable registry. Assert no implicit override, no source IO on invalid sets, and no leak of local roots into public-only summaries. Automatically redact all selected registry paths.
- [ ] Add test_redaction_covers_all_input_surfaces parameterized over messages, added/removed diff lines, context, paths, and exceptions. Include a fake credential, bound source path, bound repository locator, configured private endpoint, owner email, and an unrelated benign identifier. Assert sensitive strings are absent and the benign identifier survives.
- [ ] Run python -m pytest tests/test_privacy.py -q; expect FAIL before implementation.
- [ ] Implement environment resolution without dumping process environment. Parse owner emails and additional redaction strings from JSON-array environment bindings; automatically register all resolved locator values. Reject additional redaction values shorter than four characters.
- [ ] Implement resolved-path checks and mandatory deny rules before IO. Check file identity/containment at open time as far as supported; do not promise race-proof protection against a malicious process modifying the same filesystem. Enforce/document current-user private-state ACL/mode on Windows/Linux.
- [ ] Redact in memory before ANY evidence persistence; make exceptions safe by construction. Document heuristic limits and that an external harness may use a cloud model.
- [ ] Rerun tests; expect PASS. Windows junction assertions run on Windows; POSIX symlink/mode assertions run on Linux. If required ACL setup is unavailable, return a controlled failure rather than silently loosening permissions.
- [ ] Commit: feat: enforce private configuration and evidence boundaries.

Representative privacy assertion:
~~~python
clean = sanitize_text("- old token: ghp_" + "A" * 36, policy)
assert "ghp_" + "A" * 36 not in clean.text
assert clean.redactions
~~~

## Task 3: Collect local Git changes and non-Git snapshots

**Files:** Create sources/base.py, sources/local_git.py, sources/local_directory.py, sources/notebooks.py, tests/test_local_sources.py.
**Consumes:** SourceRoute, SourceRequest/ContextRequest, privacy helpers.
**Produces:** LocalGitProvider and LocalDirectoryProvider implementing SourceProvider, with normalized candidate evidence and safe coverage/cursors.

- [ ] Create a synthetic temp Git history: vague-message feature, later fix, another author, merge, revert, deleted/renamed file, binary file, and a deliberately changed working-tree file. Initialize Git identity/config only in the temporary repo.
- [ ] Write failing tests: test_message_and_actual_diff_are_collected; test_uncommitted_edits_not_in_git_evidence; test_authorship_is_not_inferred; test_merge_resolution_delta_is_retained_as_context; test_revert_retained; test_context_uses_pinned_revision; test_rewritten_head_requires_reconciliation. Assert merge context retains a conflict-resolution change and does not imply a new self-authored accomplishment.
- [ ] Add snapshot tests for new/changed/deleted text files, non-Git attribution unverified, mandatory exclusions, escaped links, file/byte budgets, and scan-capacity continuation. Assert oversized/unavailable content creates partial coverage, not fabricated text.
- [ ] Add notebook tests: test_notebook_sources_exclude_outputs_metadata_and_attachments, test_notebook_diff_uses_parent_and_commit_cell_sources, test_notebook_changes_never_execute, and test_malformed_or_oversized_notebook_is_partial. Seed fake secrets in output/metadata and assert they never enter packets; preserve changed code/Markdown plus revision-local cell provenance. Define normalize_notebook(raw_json: str) -> NotebookSource in sources/notebooks.py; NotebookSource contains normalized source text and cell provenance. Enforce max_notebook_bytes=8388608 on raw notebook reads before parsing, max_text_file_bytes=131072 on normalized text, and the total raw-byte source budget. Test that a 1 MiB output-heavy notebook with small source cells succeeds while one above the raw limit is rejected as partial; use this normalizer across all providers.
- [ ] Run python -m pytest tests/test_local_sources.py -q; expect FAIL.
- [ ] Implement read-only Git commands with shell=False, no pager/external diff/textconv/signature execution, and fsmonitor disabled; never change repository config, working tree, index, hooks, or refs. Pin revisions before reads and preserve complete commit messages plus available diffs.
- [ ] Implement local text snapshots with a stable relative-path ordering and content hashes. Honor explicit include/exclude scopes and record deletions. A cursor preserves pinned scan membership and an observation interval; files changed mid-scan are detected, keep coverage partial, and are queued for a subsequent scan. Do not claim an atomic filesystem snapshot. Compute identity attribution before redacting raw author identities.
- [ ] Route .ipynb reads/diffs through the shared source-cell normalizer before redaction/persistence, never through raw notebook output patches. Record normalization version, cell index, source digest, and pinned revision without executing notebook code.
- [ ] Rerun tests; expect PASS. Assert source working tree/index/refs are identical before and after collection.
- [ ] Commit: feat: collect local git evidence and directory snapshots.

Representative evidence assertion:
~~~python
assert any("checkpoint" in item.payload for item in result.evidence)
assert result.evidence[0].revision == pinned_sha
assert result.coverage.state in {"complete", "partial"}
~~~

## Task 4: Collect public GitHub evidence through a constrained transport

**Files:** Create sources/github_public.py, sources/github_transport.py, tests/test_github_source.py, tests/fixtures/github/*.
**Consumes:** SourceProvider contracts, privacy policy, SourceCursor.
**Produces:** GitHubPublicProvider; GitHubTransport.get_json(path: str, params: dict[str, str]) -> ApiResponse; injectable fake transport.

- [ ] Write failing tests for public visibility, owner/repo syntax, default-branch SHA pinning, history/detail pagination, per-commit diff extraction, shared evidence normalization, and unknown-author attribution.
- [ ] Add fixtures for 404/private visibility, 429 with retry-after, 5xx, timeout, binary/missing patch, provider file cap, moving default branch, and base/head divergence. Assert safe errors, persisted continuation, partial coverage, no advancing completed checkpoint, and no last-good-state loss.
- [ ] Add test_transport_rejects_external_redirect_and_credentials. Assert no token/header/credential helper is loaded, and all requests remain within fixed GitHub HTTPS provider hosts.
- [ ] Add test_public_repository_becoming_private_does_not_fallback_to_auth_or_local. Recheck visibility on each collection; preserve prior records and report a source error without touching an unselected local source. Public-repository metadata discovery prepares review candidates only and must not automatically enroll every visible repository.
- [ ] Run python -m pytest tests/test_github_source.py -q; expect FAIL. Automated tests use fake responses and make no live network requests.
- [ ] Implement anonymous REST collection; pin target SHA, paginate commit sets and each commit's files, then request only bounded allowed text context at a pinned SHA. Never infer completeness from a capped aggregate file list.
- [ ] Reuse notebook normalization from T3 for bounded pinned parent/current notebook contents; if the required contents cannot be acquired within budget, report partial coverage rather than retaining the raw notebook patch.
- [ ] Treat unavailable or oversized patches as evidence gaps. Resume rate limits conservatively; no unbounded retries or sleeps. Keep endpoint construction internal; source coordinates exist only in private bindings.
- [ ] Rerun tests; expect PASS. Real public-repository use is tested later by the owner against a route they select.
- [ ] Commit: feat: add public github evidence provider.

## Task 5: Coordinate incremental collection, packets, and durable private state

**Files:** Create state.py, collection.py, investigation.py, tests/test_collection_packets.py; wire collect, packet create, and context CLI commands.
**Consumes:** Providers and all collection/packet contracts.
**Produces:** collect_projects(config: ResolvedConfig, project_ids: list[str]) -> CollectionReport; create_packet(store: StateStore, project_id: str) -> PacketResult; extend_packet(store: StateStore, request: ContextRequest) -> PacketResult; locking/atomic generation primitives; separate traversal and analysis ledgers.

- [ ] Write failing tests: test_add_route_without_code_change; test_project_source_and_work_isolation; test_shared_work_across_sources_has_one_identity; test_changed_source_binding_requires_reconciliation; test_resume_after_commit_budget; test_unchanged_collection_is_noop; test_no_head_advance_on_incomplete_range; test_one_source_failure_keeps_other_projects.
- [ ] Add test_packet_budget_tracks_unanalyzed_backlog, test_required_prior_work_context_cannot_be_silently_dropped, test_context_request_is_bound_to_source_revision_and_scope, and test_invalidation_overlay_marks_claim_needs_review_without_rewriting_history. Assert every declared packet byte budget includes its envelope/context and that a blocked work returns partial/blocked_budget.
- [ ] Add state tests for exclusive lock conflict, interrupted staging, and atomic CURRENT replacement. Assert previous generation remains readable if any file/validation/publication step fails.
- [ ] Run python -m pytest tests/test_collection_packets.py -q; expect FAIL.
- [ ] Implement content-addressed sanitized evidence, pinned collection manifests/cursors, pending-analysis IDs, and route/source/policy fingerprints. Persist raw private bindings only in config, never collection artifacts. Distinguish collected, unavailable, excluded, and analyzed evidence. Changed source identity/scope forces reconciliation; a stricter redaction policy requires rebuilding into a new private state root, with no claim that old retained artifacts were retroactively sanitized.
- [ ] Build deterministic packets by pending evidence/work context; pack complete existing-work context or stop with blocked_budget. Additional context creates a new packet digest and invalidates proposals for the old digest. Bind every packet to collection_revision; new evidence or changed route scopes invalidate outstanding project packets before they can clear newer needs_review flags.
- [ ] Implement StateStore with same-filesystem staging, current-user permissions, per-project locks, and atomic pointer replacement. Document operator-only stale-lock recovery. Collection progress cannot masquerade as semantic publication.
- [ ] Rerun tests; expect PASS. Assert source errors never mutate another project's generation.
- [ ] Commit: feat: add resumable evidence collection and investigation packets.

## Task 6: Validate analysis proposals and publish stable project/work JSON

**Files:** Create proposals.py, tests/test_proposals.py, tests/fixtures/proposals/*; wire apply.
**Consumes:** InvestigationPacket, AnalysisProposal, StateStore and public record models.
**Produces:** apply_proposal(store: StateStore, proposal_path: Path) -> PublicationResult; semantic reference validator; immutable project.json and works/<work_id>.json publication.

- [ ] Write failing tests: test_foreign_or_missing_evidence_rejected; test_packet_and_base_generation_conflicts; test_new_collection_invalidates_old_packet_without_base_generation_change; test_metric_requires_measurement_evidence; test_deployed_requires_deployment_evidence; test_machine_cannot_mark_user_reviewed; test_unknown_owner_stays_unverified.
- [ ] Add test_partial_proposal_upserts_without_dropping_unseen_works and test_every_packet_evidence_id_accounted_for_once. Assert omitted work B is byte-equivalent when only work A updates, unless B was independently invalidated by collection.
- [ ] Add test_failed_apply_leaves_analysis_backlog_unchanged, test_reapply_is_noop, test_work_id_stable_across_new_commits, and test_changed_work_does_not_inherit_review_approval. Reject incomplete existing-work context and deletion/retirement fields.
- [ ] Run python -m pytest tests/test_proposals.py -q; expect FAIL.
- [ ] Implement offline schema validation, project/source/work/evidence integrity, proposal digest/base-generation checks, complete per-work semantic replacement within a project-level upsert, and generated timestamps/revisions. Validate claim categories and explicitly represent unknowns.
- [ ] Account for packet evidence exactly once: analyzed or an allowed exclusion reason. Remove only accepted/accounted IDs from the analysis backlog in the same project publication transaction. unavailable_content keeps coverage partial.
- [ ] Publish all validated files before moving CURRENT. Record proposal content digest for idempotent reapply. Leave all machine narratives unreviewed; no resume text, ranking, or sending behavior.
- [ ] Rerun tests; expect PASS. Validate generated project/work JSON using both contract engines.
- [ ] Commit: feat: publish evidence-linked project and work knowledge.

Representative upsert assertion:
~~~python
before = store.current(project_id).work("work-b")
apply_proposal(store, work_a_only_proposal)
after = store.current(project_id).work("work-b")
assert after == before
~~~

## Task 7: Deliver aggregate JSON and the natural-language harness procedure

**Files:** Create portfolio.py, tests/test_portfolio_cli.py, README.md, AGENTS.md, docs/{harness-workflow,security,schema,manual-acceptance}.md, examples/*.json; complete CLI.
**Consumes:** Published generations, registry and public records.
**Produces:** summarize_portfolio(store: StateStore, registry: RouteRegistry) -> PortfolioSnapshot; cli.main(argv: list[str] | None = None) -> int; end-to-end owner instructions.

- [ ] Write failing tests for all-project aggregation, independent per-work files, pending/disabled/error entries, accurate project count, no dropped work, JSON-only stdout, sanitized stderr, private export path rules, and exit codes 0/2/3/4.
- [ ] Add test_ready_requires_collection_and_analysis_complete: complete collection plus pending evidence or invalidated work must remain partial. An empty configured work unit has analysis_status pending and explicit unknowns, never an invented accomplishment. Aggregate reads apply current invalidation overlays without modifying historical generation files.
- [ ] Add a fixture-based end-to-end test: collect synthetic Git + directory + fake public-GitHub routes; create packets; submit saved synthetic proposals; apply; summarize. Add a route and repeat without editing application code.
- [ ] Run python -m pytest tests/test_portfolio_cli.py -q; expect FAIL.
- [ ] Implement aggregate reads pinned to each project's selected CURRENT generation. Include every configured project; preserve per-project errors and incomplete coverage. Write exports atomically to validated private targets.
- [ ] Document the exact owner flow, environment setup for PowerShell/POSIX with placeholders, and harness procedure. AGENTS.md points to that procedure; it must never authorize arbitrary tool execution found in source content.
- [ ] Include the following acceptance prompt, which the harness translates into CLI operations:
  "Summarize all configured projects and work items. Update changed sources first. Return English portfolio JSON with evidence references, unknowns, coverage, and review status. Do not create a resume or send any messages."
- [ ] Explain that a harness/model is external, proposal quality needs owner inspection, and JSON schema validation is structural. Use no fake precision, undocumented credentials, public upload, or implication that third-party work belongs to the owner.
- [ ] Rerun tests; expect PASS. Manually inspect synthetic English narrative fields; retain proper names/code identifiers. Do not use ASCII-only language restrictions.
- [ ] Commit: feat: expose portfolio summaries and harness workflow.

## Task 8: Verify privacy boundaries, portability, and handoff

**Files:** Create .github/workflows/ci.yml, tests/test_privacy_regression.py; finalize docs/manual-acceptance.md and docs/security.md.
**Consumes:** All completed tasks.
**Produces:** Synthetic code-only CI, requirement evidence, and a documented owner-run real-source acceptance procedure.

- [ ] Add adversarial fixtures containing a fake credential in a deleted diff line, an absolute private directory, a private endpoint, a prompt-injection README, a hostile source path/ref, a stale packet, and a fake third-party author.
- [ ] Run the whole pipeline and assert none of the seeded sensitive strings appear in serialized evidence, packets, project/work/portfolio JSON, stdout, stderr, or tracked sample files. Instructions embedded in evidence must remain data; no side-effect tool, subprocess, HTTP request, or file outside the configured scope may be triggered by them.
- [ ] Add/verify cross-platform tests for Windows paths/junctions/ACL setup and Linux symlinks/modes; put Python 3.12 tests on both OS families in CI. Local fixtures are disposable and contain no user work. CI does not connect to real GitHub project routes or LLMs.
- [ ] Run python -m ruff check .; python -m pytest -q; python -m tracefolio.schema_export --check as separate commands. Expected: exit 0 each. Record actual versions, counts, and results; do not mark unavailable required checks passed.
- [ ] Review the final implementation for bugs, requirement gaps, error paths, side effects, and unnecessary abstractions using the installed Superpowers code-review workflow. Fix blockers and rerun affected checks after the last relevant edit.
- [ ] Owner manual acceptance, when they choose to execute: use privately configured local/GitHub sources; ask the harness for a full summary; inspect at least one real message+diff-derived work item and its provenance; verify unknown impact/ownership is not invented. Add a route, add a commit, and rerun unchanged data. Record results without checking private outputs into Git.
- [ ] Do not connect Gmail, create drafts, send test mail, generate resumes/PDFs, publish outputs, or apply for jobs. Stop at the semantic-layer JSON deliverable.
- [ ] Commit task-owned verification/docs: test: verify privacy and cross-platform knowledge workflow.

## Acceptance matrix

All methods below are PLANNED, not evidence that software exists or passes.

| AC ID | Required behavior | Owning task | Verification method | Expected result | Evidence/status |
|---|---|---|---|---|---|
| AC-001 | Add supported routes without changing code | T1,T5,T7 | New config-only project in end-to-end fixture | New independent aggregate/project/work output | PENDING |
| AC-002 | Read commit messages AND actual code changes | T3,T4 | Vague-message fixtures with distinctive diff | Both message and changed behavior present in evidence | PENDING |
| AC-003 | Support local Git and non-Git text directories | T3 | Synthetic Git and snapshot lifecycle tests | Correct evidence origin, deletion tracking, safe attribution | PENDING |
| AC-004 | Support public GitHub without a token | T4 | Paginated fake transport; later owner-selected real route | Public-only read access, pinned revisions, visible gaps | PENDING |
| AC-005 | Produce separate stable project/work JSON | T1,T6 | Inspect generation files and validate with two engines | Referenced work files exist; IDs persist | PENDING |
| AC-006 | Produce English portable schema-versioned JSON | T1,T7 | Schema/format tests and narrative inspection | Draft 2020-12 compatibility, English narratives, UTC dates | PENDING |
| AC-007 | Keep locators/secrets out of code and runtime artifacts | T2,T8 | Seeded privacy regression across every input/output surface | Seeded values absent; logical refs/redaction markers remain | PENDING |
| AC-008 | Keep private config/state/export outside repositories | T2,T7 | Resolved-path, nesting, ACL and export tests | Unsafe roots/escapes rejected before IO | PENDING |
| AC-009 | Incremental update, safe resume, unchanged-run idempotence | T3,T4,T5,T6 | Budget, failure, divergence and repeated-proposal cases | No lost ranges/duplicates; correct partial/no-op states | PENDING |
| AC-010 | Trace claims to evidence without inventing impact/ownership | T6 | Integrity, metric/deployment and attribution tests; manual sample | Unsupported claims rejected or marked unknown/unreviewed | PENDING |
| AC-011 | Partial packets retain unseen work and analysis backlog | T5,T6 | Upsert + exact evidence-accounting tests | No dropped work; ready only after complete collection+analysis | PENDING |
| AC-012 | Summarize every configured project, including gaps | T7 | All-project CLI/exit-code and manual harness flow | Valid JSON, accurate count/status, no hidden failures | PENDING |
| AC-013 | Resist stale writes/crashes/concurrent writers | T5,T6 | Fault-injected publication and lock conflict | Last valid generation preserved | PENDING |
| AC-014 | Untrusted source content cannot expand acquisition scope | T2,T4,T5,T8 | Path/ref/redirect/injection cases | No unauthorized source/network/execution side effects | PENDING |
| AC-015 | No email, sending, Resume/PDF, or application side effects | T7,T8 | Scope/code/dependency review and side-effect spies | Only private evidence/JSON outputs and allowed source reads | PENDING |
| AC-016 | Required verification and owner handoff are recorded | T8 | CI + final review + manual procedure | Actual results and remaining limits reported honestly | PENDING |
| AC-017 | Notebook evidence contains source cells only | T3,T4,T8 | Parent/current cell-source and sensitive-output fixtures | No outputs, metadata, attachments, execution, or raw output patches; provenance retained | PENDING |
| AC-018 | Public-first and private-local registries compose safely | T2,T4,T7 | Single/multiple registry selection, conflict and visibility-change tests | Public-only runs work without local bindings; private sources remain local; no implicit fallback or duplication | PENDING |

## Commands and execution evidence

Discovered now: initialized product checkout with planning files only. Source discovery inspected directory structure, selected README summaries, package dependency names, and selected function names to prepare a private source proposal. No credentials, environment-file contents, product package/tests, or live services were used. Actual source configuration remains unapproved and inactive.
Proposed after implementation:
- python -m pip install -e ".[dev]" -> install proposed package and tools.
- python -m ruff check . -> exit 0.
- python -m pytest -q -> all relevant synthetic tests pass.
- python -m tracefolio.schema_export --check -> exit 0, no schema drift.
- tracefolio config validate -> exit 0 for privately configured valid routes.
- tracefolio collect --all -> exit 0 when complete, 3 while partial; no lost cursor.
- tracefolio summarize --all --format json -> schema-valid aggregate; exit 0 only when enabled projects are ready.

No future implementation checks have been run during planning.

## Plan review record

Initial self-review: checked requirements against spec, interface consistency, no-email boundary, partial-update semantics, redaction bindings, and branch prerequisite.
Builder review of the design found two material ambiguities: partial work upserts/analysis coverage, and the configuration source for extra private redaction values. Both were resolved in spec sections 5 and 9 and tasks T2/T5/T6.
Quality-gate review: PLAN READY for the proposed scope. Assessed all seven gates: scope/coverage, observable acceptance, executable work, failure cases, simplicity, completion evidence, and implementation-branch prerequisite. Commands and new files are explicitly marked proposed; all product evidence remains PENDING. No unresolved product decisions block this plan. The working name, actual target repository, private source bindings, and harness are supplied/confirmed by the owner at execution/use time.
Review repairs: clarified partial-work upsert versus replacement, exact analysis-backlog accounting, private redaction bindings, merge-context retention, collection-revision stale protection, immutable invalidation overlays, work readiness, and source-identity reconciliation.
Source-discovery amendment: add shared notebook source-cell normalization, disabled-project lazy binding resolution, and default source-URL redaction. These are documented requirements pending owner confirmation of the private source proposal; no corresponding code has been implemented.
Owner-directed revision: remove the unowned deck-ingestion source from private setup proposals; propose the reviewed model-training project; split public-GitHub and private-local registries with explicit composition and no fallback. Add an 8 MiB raw notebook budget so real source-only ML investigations fit while outputs remain excluded. Source approval remains pending; no product code or external delivery is authorized yet.
Implementation evidence: PENDING throughout.

