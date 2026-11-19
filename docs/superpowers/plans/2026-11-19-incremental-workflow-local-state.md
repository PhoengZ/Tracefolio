# Incremental Workflow and Local Private State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Reuse saved summaries, update only affected work, and migrate the complete private state into a Git-ignored directory in this checkout.

**Architecture:** Preserve existing immutable generations, snapshot inventories and evidence validation. Add compact read interfaces and explicit scheduling reasons, then introduce a narrowly authorized repository-local state location and a verified copy-before-switch migration. Coverage remains truthful and does not itself schedule analysis.

**Tech Stack:** Python 3.12+, Pydantic v2, argparse, pathlib, Git CLI, pytest, Ruff; existing dependencies only.

**Spec:** `docs/superpowers/specs/2026-11-19-incremental-workflow-local-state-design.md`

## Global constraints

- Python 3.12+; no new dependency, service, model integration or authentication route.
- Synthetic fixtures only in tests and CI. Never print real bindings, source roots, repository locators, packets, proposals or state contents in verification output.
- Preserve all existing immutable IDs, contribution/review fields, redaction rules, source scopes and external-registry validation.
- Keep persisted status enums and full portfolio schema compatible. No automatic ready promotion, backlog deletion or replacement of CURRENT/generation/evidence by hand.
- Private state is allowed only at `.tracefolio-local/state` under an explicitly bound Git top level, or at a previously permitted external location.
- Actual migration follows successful implementation verification; preserve the original state and do not push this repository.

## Branch prerequisite

- [x] At execution time inspect `git status --short`, `git branch --show-current`, and `git rev-parse HEAD`. Preserve unrelated changes, including any existing untracked directories.
- [x] Create or confirm a dedicated branch for this task before the first code/test edit. Record branch and starting commit in execution evidence. Planning-time observation: branch `main`, starting short commit `6f67dce`; recheck rather than assuming these remain current.

## Acceptance criteria

| ID | Required behavior | Task | Verification and expected result | Evidence |
|---|---|---|---|---|
| AC1 | Show saved summaries without collection or analysis | 1, 4 | Provider/packet/apply spies receive zero calls; state content and CURRENT remain identical | Verified; see implementation evidence report |
| AC2 | Unchanged snapshot reuses existing work | 2 | Same HEAD, and unrelated HEAD change with identical scoped blobs: no added pending evidence, no changed work, packet noop after prior apply | Verified; see implementation evidence report |
| AC3 | Scheduling uses explicit reasons, not partial/new IDs alone | 1, 2 | Rate-limited no-change source is blocked/reused; old backlog and revisited evidence are separately visible; changed work alone is selected | Verified; see implementation evidence report |
| AC4 | Brief output excludes evidence payloads and large provenance arrays | 1 | Fixture with many references returns bounded selected fields; full interface/export remains schema-valid and unchanged | Verified; see implementation evidence report |
| AC5 | Only the authorized ignored/untracked local state root is allowed | 3 | Accepted exact root; rejected wrong root, tracked files, missing ignore, symlink/junction escape, source overlap and non-export writes | Verified; see implementation evidence report |
| AC6 | Migration preserves complete state and incremental continuity | 5 | Byte manifests and semantic records match; IDs/cursors/CURRENT resolve; synthetic next collect is noop | Verified; see implementation evidence report |
| AC7 | Failed/concurrent migration never switches active state | 5 | Corrupt evidence, writer contention, occupied destination, copy failure and binding-write failure retain original binding/state | Verified; see implementation evidence report |
| AC8 | Workflow docs match the implemented interfaces | 4 | Reviewed command/intent table; no stale full-evidence or whole-map promise; manual trace for summary/update/blocker cases | Verified; see implementation evidence report |
| AC9 | Required checks and privacy review pass | 6 | Ruff, pytest, schema drift and diff checks pass; only synthetic fixtures enter tracked changes | Verified; see implementation evidence report |

## Review focus

- `new_evidence=0` can coexist with actionable old/revisited evidence: prove this separately from an unchanged analyzed project (Task 2).
- Missing/stale summaries and an unavailable source must remain visible without a loop or fabricated readiness (Tasks 1, 4).
- CLI mode overrides must not silently discard the existing snapshot baseline or masquerade as product changes (Task 2).
- Git ignore alone does not protect already tracked files, junction targets or build artifacts (Task 3).
- A concurrent writer or interrupted binding switch must not activate a partial migration (Task 5).

## Task 1: Compact saved summaries and scheduling status

**Files:** modify `src/tracefolio/cli.py`, `src/tracefolio/portfolio.py`; create `src/tracefolio/status.py`, `tests/test_status.py`; extend `tests/test_portfolio_cli.py`.

**Interfaces:**
- Add `status --all` / repeated `--project <id>` with JSON output. It consumes configured routes and active ledger/generation metadata; it never calls providers or creates packets.
- `inspect_project(store, route) -> dict` returns `project_id`, `generation_id`, `coverage_state`, `pending_evidence_count`, `invalidated_work_ids`, `reconciliation_source_ids`, `missing_work_ids`, `limitations`, `analysis_action` and `reasons`. `analysis_action` is `reuse`, `analyze`, `blocked`, `missing` or `disabled`.
- Determine action in this order: disabled -> `disabled`; unreadable state -> `blocked/state_integrity`; route fingerprint mismatch -> `blocked/collection_required`; pending evidence -> `analyze/pending_evidence`; invalidation/reconciliation with complete affected sources -> `analyze/review_required`; the same with incomplete sources -> `blocked/source_incomplete`; no generation -> `missing/no_generation`; missing configured works -> `missing/configured_work_missing`; partial work or unresolved claims without any backlog -> `blocked/explicit_review_required`; otherwise -> `reuse`. Missing/blocked records retain last-good summaries when available. Unverified contribution, unreviewed status and unknown impact alone do not block reuse. Report all applicable reasons even when the first determines action.
- Add `summarize --view brief|full`; preserve `full` as the compatibility default. `brief` includes project/work IDs, titles, project summary, work problem/actions, implementation/contribution/review statuses, unknowns, coverage counts and safe error/reason codes. Omit evidence payloads, evidence/provenance arrays, source revisions and local paths. `--out` still exports the complete existing portfolio artifact.
- Exit codes retain existing semantics: status returns 0 for reuse/analyze/disabled, 3 for blocked/missing, 2 for invalid input and 4 for operational failure. Brief summarize follows existing portfolio readiness exit codes; exit 3 does not imply permission to repair.

- [x] Write failing tests for ready, partial-with-zero-backlog, old backlog, missing generation/work, disabled, corrupt state and unknown project selection. Assert reason/action fields and isolation of failures.
- [x] Test summary/status with provider, packet and apply functions made to raise if called. Compare persisted file digests before/after; disregard only existing permission enforcement metadata, not content changes.
- [x] Add a large-reference synthetic portfolio test: brief output contains none of `payload`, `evidence_ids`, `evidence_refs`, `packet_ids`, `collection_ids`; its size does not grow with reference count. Full export still validates against `portfolio.schema.json`.
- [x] Run `./.venv/Scripts/python.exe -m pytest tests/test_status.py tests/test_portfolio_cli.py -q`; confirm new assertions fail before implementation.
- [x] Implement the interfaces using existing store/portfolio helpers; do not change persisted contracts merely to support a CLI projection.
- [x] Rerun the tests; expected all pass. Review the diff and commit only task files.

## Task 2: Explicit incremental collection outcomes

**Files:** modify `src/tracefolio/collection.py`, `src/tracefolio/investigation.py`; extend `tests/test_collection_packets.py`, `tests/test_proposals.py`, `tests/test_local_sources.py`, `tests/test_github_source.py`. Modify collectors only if a regression proves a defect.

**Interfaces:** Extend each collection report entry additively with `pending_before`, `pending_after`, `revisited_evidence`, `invalidated_work_ids`, `reconciliation_source_ids` and safe `limitations`. Preserve `new_evidence`, `status` and existing callers. Report new/revisited counts separately; do not reset ledger backlog.

`create_packet(store, project_id, *, work_id=None)` retains its signature. When `work_id` is supplied, filter pending candidates to paths associated with that work's prior references or configured work routes before building the packet; do not merely add its prior context to a project-wide packet. Shared evidence may be included, but unrelated pending evidence/invalidated work remains untouched. When no matching candidates remain, preserve deliberate prior-evidence review for that existing work. Unknown work IDs remain errors. Project-level collection still observes the unchanged configured scope; work selection restricts analysis rather than silently rewriting acquisition scopes.

- [x] Write end-to-end regression fixtures that collect, apply a real synthetic proposal, then collect again. Same HEAD gives zero new/revisited evidence, empty backlog and `create_packet(...).status == 'noop'`.
- [x] Change only an out-of-scope file at a new Git HEAD: scoped blob inventory and work record revisions remain unchanged; no work is queued. Change one in-scope file: only its associated work is invalidated; unrelated summaries/IDs remain identical.
- [x] Cover old backlog with zero new IDs, A-to-B-to-A revisited directory evidence, incomplete collection without analyzable change, and deliberate mode transition. Demonstrate why neither `partial` nor `new_evidence` alone is a selection rule.
- [x] Add two-work backlog fixtures, shared-path evidence and unrouted new evidence. A packet for one existing work includes only its associated candidates/prior context; applying it preserves the other work's record, invalidation and pending IDs. A target with no matching changes can be deliberately reviewed without consuming unrelated backlog.
- [x] Run `./.venv/Scripts/python.exe -m pytest tests/test_collection_packets.py tests/test_proposals.py tests/test_local_sources.py tests/test_github_source.py -q`; confirm the new reporting and work-selection assertions fail before implementation.
- [x] Add report accounting around existing cursor/hash logic. Preserve source-configured mode unless the caller explicitly passes an override. If a mode-transition test exposes a baseline defect, fix only that proven transition and retain the regression.
- [x] Rerun the same command; all pass. Commit the bounded change.

## Task 3: Authorize one repository-local private state root

**Files:** modify `.gitignore`, `src/tracefolio/private_config.py`; extend `tests/test_privacy.py`, `tests/test_privacy_regression.py`, `tests/test_portfolio_cli.py`. Keep `src/tracefolio/privacy.py` exclusions unless tests expose a missing boundary.

**Interfaces:** Add optional private environment binding `TRACEFOLIO_LOCAL_STATE_CHECKOUT` (absolute Git top level). Keep `TRACEFOLIO_STATE_DIR` as the actual absolute state path. Add `validate_state_location(state: Path, checkout: Path | None) -> None` and reuse it in config/export checks. The module's install location must not be mistaken for a user-selected checkout.

- [x] Add `/.tracefolio-local/` to `.gitignore` before any private data is copied.
- [x] Write failing synthetic-Git tests accepting exactly `<bound-checkout>/.tracefolio-local/state`; accept existing safe external state with no new binding.
- [x] Reject arbitrary checkout subdirectories, a mismatched Git top level, a missing ignore rule, any cached/tracked private file, symlink/junction escape, and overlap with configured sources/registries. Check both ignore status (`git check-ignore --no-index`) and tracked status (`git ls-files`); use argument arrays, not interpolated shell commands.
- [x] Add export regression tests: local `<state>/exports/...` succeeds; checkout files, evidence/generations namespaces and traversal escapes remain rejected. A private-root marker fixture never passes `mandatory_allowed` or appears in built package file listings.
- [x] Run `./.venv/Scripts/python.exe -m pytest tests/test_privacy.py tests/test_privacy_regression.py tests/test_portfolio_cli.py -q`; confirm new exception tests fail before implementation.
- [x] Implement only the narrow local-state exception. Retain owner-only permissions, public output redaction, external registry and source restrictions. Do not print binding values in diagnostics.
- [x] Rerun tests; verify Git ignores synthetic/private markers and does not track them. Commit code, tests and ignore rule only.

## Task 4: Make the agent workflow follow user intent

**Files:** modify `AGENTS.md`, `README.md`, `docs/harness-workflow.md`, `docs/schema.md`, `docs/security.md`, `docs/manual-acceptance.md`; review `docs/github-plugin-workflow.md` for matching handoff language.

- [x] Add an intent table: “summarize/show/again” reads `summarize --view brief`; “update/refresh/latest” reads status, collects requested scope using the configured mode, then analyzes required work; “review this work” explicitly permits its prior context. User-specified scope always wins.
- [x] Explain the independent meanings of acquisition coverage, analysis backlog and review status. Keep `partial` as truthful metadata; neither it nor exit code 3 launches repair automatically.
- [x] For updates, consume Task 2 reports and Task 1 status. Preserve unrelated prior backlog when scope is narrower. Read one packet per current context and retain its parsed content; do not re-create/re-read it without apply, staleness or necessary scoped context.
- [x] State that `semantic_map` describes included packet evidence; only a complete initial snapshot packet may provide a complete scoped HEAD map. Replace the stale requirement to reread all existing evidence with prior semantic content plus validated references.
- [x] Specify persistent-blocker termination and last-good summaries. No provider/auth/local-clone fallback or implicit scope expansion. Document source mode continuity and that snapshot does not eliminate failure coverage.
- [x] Document the opt-in ignored local state location and revised export boundary using placeholders only. Add a manual acceptance trace for ordinary summary, unchanged update, one-work update, rate limit, old backlog and missing generation.
- [x] Check documented command spellings against CLI `--help`; review each trace against tests from Tasks 1-3. Commit documentation only after it matches those interfaces.

## Task 5: Verified migration with rollback

**Files:** create `src/tracefolio/migration.py`, `tests/test_state_migration.py`; modify `src/tracefolio/cli.py`, `src/tracefolio/state.py`, `docs/harness-workflow.md`. Modify `load-config.ps1` only if the loader needs support for the new binding; never replace its validation with a hard-coded path.

**Interfaces:** Add `state migrate --to-checkout <absolute-checkout> --bindings <ignored-local-bindings-file> [--dry-run]`. Source is the currently configured state; destination is the fixed Task 3 location. `migrate_state(config, checkout: Path, bindings_path: Path, *, dry_run: bool = False) -> dict` performs verified copy and binding activation under one writer barrier and returns only status/counts/digest-match flags. Accept only the checkout's ignored/untracked `bindings.local.json`; preserve all other binding entries and update the registry's actual `state_dir_env` key plus `TRACEFOLIO_LOCAL_STATE_CHECKOUT`. Reject binding/source-selection mismatches. An already active verified destination can return `noop`; other occupied destinations are rejected unless an owned manifest proves an identical inactive copy from an interrupted attempt.

`activate_local_bindings(bindings_path: Path, config, checkout: Path, destination: Path) -> None` is an internal helper called only while migration owns the barrier and project locks. It keeps original binding bytes for atomic rollback and validates the resulting selection in an isolated environment so the caller's old process environment does not falsely validate activation.

- [x] Write failing fixtures containing policy, evidence, collections, packets, proposals, generations, CURRENT/revisions, receipts, exclusions, backlog and cursors. Assert byte-for-byte preservation of durable files and successful store integrity reads after copying.
- [x] Enumerate the source state without following links; reject symlinks, junctions and other redirecting reparse entries anywhere in durable namespaces before copying or recursive permission operations. Add synthetic source-link and destination-link escape tests. Route migration dispatch before the CLI's usual `StateStore.open` so its recursive permission enforcement cannot touch linked outside content before this check.
- [x] Test dry-run (zero state/binding changes), interrupted copy, mismatched digest/corrupt reference, unsafe destination, occupied destination and a changed source manifest. Exclude `locks/`, a migration barrier and temporary files from durable copying.
- [x] Implement an exclusive state-wide migration writer barrier integrated with `StateStore.lock`: normal writers check it before and after acquiring a project lock, and refuse a retired root. Migration establishes the barrier, acquires all existing/configured project locks, and refuses contention without copying. Keep the barrier through source-manifest recheck, destination validation, retirement-marker creation and binding activation. Test writer-before-migration, migration-before-writer and a stale-old-config writer after activation using separate processes. Readers may read old generations. Release locks/barrier on failure; never delete another process's marker.
- [x] Run `./.venv/Scripts/python.exe -m pytest tests/test_state_migration.py -q`; confirm new behavior fails before implementation and passes after the minimal migration implementation.
- [x] Copy into a private staging sibling, verify full durable relative-path/SHA-256 manifest plus evidence/packet/generation/reference integrity, then atomically rename to the fixed destination. No recollection or reanalysis occurs.
- [x] Test activation with a synthetic ignored bindings file: retain exact original contents for rollback, update only the state and checkout bindings atomically, validate config and compare brief/full semantic summaries. Binding-write/validation failure restores original bytes and removes this migration's retirement marker. Successful activation adds only the owned retirement marker to the old root; all original durable files remain byte-identical. Keep an owned private recovery manifest with phase and original-binding backup so an interrupted process can be explicitly resumed or rolled back; never automatically steal a live barrier.
- [x] Add explicit `state migrate --to-checkout ... --bindings ... --recover resume|rollback` (mutually exclusive with `--dry-run`). Recovery accepts only its matching owned manifest and a verified stopped owner; it rechecks manifests before resume, or restores the saved binding and removes only owned retirement/barrier markers on rollback. Test process interruption before activation and immediately after the atomic binding switch. Keep unverified destination files inert for inspection rather than recursively deleting them.
- [x] Test a synthetic collection after activation: no source changes means no additional evidence/backlog and the same work IDs/generation. Commit migration tooling/tests; do not include real data or local bindings.

## Task 6: Final verification and operational migration

- [x] Review implementation against AC1-AC9 for bugs, scope expansion and unnecessary abstractions. Correct blocking findings and rerun affected tests after the last edit.
- [x] Run `./.venv/Scripts/python.exe -m ruff check .` (expected exit 0).
- [x] Run `./.venv/Scripts/python.exe -m pytest -q --tb=short` (expected all tests pass).
- [x] Run `./.venv/Scripts/python.exe -m tracefolio.schema_export --check` (expected exit 0; existing persisted schemas should not drift).
- [x] Run `git diff --check` (expected exit 0); inspect changed/tracked files for private material and confirm the branch evidence.
- [x] Only after implementation verification, load bindings in the same PowerShell invocation as CLI operations, run migration dry-run, then the verified migration/activation command. No external source collection is needed.
- [x] Record a private before/after manifest and summary comparison. The migration command switches ignored local bindings only after destination verification under its writer barrier. Reload bindings in a new invocation, validate configuration and read saved summaries. On activation failure restore original binding bytes and the original root's write availability. Keep the original external state as an inactive backup.
- [x] Confirm the actual local state and all descendants are ignored/untracked, and that config/status/summarize output contains no raw bindings or private paths. Report safe aggregate results only.
- [x] Update the acceptance matrix with actual evidence, unresolved findings and checks not run. Do not call future checks PASS merely because this plan specifies them. Do not delete the backup, push, or update the real portfolio as part of migration.

## Plan review

Scope maps to AC1-AC9 and Tasks 1-6. Existing hash/cursor/packet tests are reused; new checks address scheduling, compact output and storage migration. No source-code change, state migration or new portfolio analysis is performed by writing this plan. Implementation evidence remains PENDING until execution.

## Completion evidence

Implemented on `feat/incremental-local-state`; see `../reports/2026-11-19-incremental-workflow-local-state.md` for verification and execution rulings. Full suite: 228 passed; an additional OS-owner serialization regression also passed. Actual migration, ignored/untracked storage, snapshot continuation and authorized portfolio refresh completed. Private comparison data remains under the active state exports namespace. Human semantic review remains unreviewed.
