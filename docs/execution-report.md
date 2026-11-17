# Phase 1 execution report

The owner approved implementation after reviewing private routes and removing six additional GitHub candidates. The unowned deck-ingestion source is excluded. Private setup selects public-GitHub and local-private registries independently or together; it currently contains 15 configured projects, 11 enabled projects, and 19 enabled sources. No personal bindings or real work artifacts are tracked here.

Implementation is on `feat/semantic-work-layer`, from planning baseline `a385d4b`. The designated checkout and configured origin are preserved. No push, email integration, message delivery, resume/PDF generation, or job application has occurred.

## Verification

Local environment: Windows, Python 3.12.3, Git 2.49.0.windows.1; Pydantic 2.13.3, pytest 8.3.4, jsonschema 4.23.0, Ruff 0.17.5. Tests use disposable synthetic repositories and fake GitHub responses.

| Check | Observed result |
|---|---|
| Typed contracts and offline schemas | Pydantic and Draft 2020-12 examples validated |
| Privacy/configuration | Binding separation, safe registry composition, path containment, Windows ACL reapplication, seeded redaction verified |
| Local source collection | Commit messages and changes, snapshots/deletions, continuation, merge/revert/rewritten-history cases, notebook source-only parsing verified |
| Public GitHub adapter | Anonymous constrained transport, visibility, pinning, pagination, missing patches, rate-limit continuation verified with fakes |
| Collection/packets | Pending-analysis ledger, bounded packets, scoped context, invalidation, fault/lock behavior verified |
| Proposal publication | 21 tests passed; exact evidence accounting, unsupported claims, stale writes, work retention, atomic pointer safety and reapplication verified |
| Portfolio/CLI | 8 tests passed; every configured project, current overlays, private exports, all exit classes, three-source-kind pipeline and config-only addition verified |
| Adversarial pipeline | 4 tests passed; deleted-line credentials, private paths/endpoints, generated unsafe content, source injection as data, third-party attribution, disappearing snapshot files verified |
| Full Windows suite | 103 passed in 77.11 seconds |
| Ruff | Passed |
| Schema drift | Passed |
| Real private registry validation | Passed: 15 configured, 11 enabled |
| Linux | CI defined; not executed locally |
| Fresh whole-branch review | Pending final review |

Automated tests cannot establish truth, impact, ownership, or exhaustive secret removal. English narrative quality is inspected by the harness/owner, not guaranteed by schema validation. All machine output remains unreviewed. Real-source full-portfolio/LLM acceptance remains owner-run using [manual-acceptance.md](manual-acceptance.md); no claim is made that every configured project already has populated semantic records.

## Implementation rulings

1. Reused the owner's fresh designated checkout on a dedicated feature branch instead of creating another worktree. This preserves the requested directory; the cost if wrong is less filesystem isolation while main remains at the planning baseline.
2. Implemented inline with one fresh whole-branch review. Closely coupled interfaces do not require per-task agents; the cost if wrong is finding defects later than per-task review.
3. Anchored private-route ignore rules at the repository root so `schemas/routes.schema.json` stays tracked. The cost if wrong is that additional private filenames placed elsewhere in the checkout would need explicit exclusion; private files belong outside the checkout regardless.
4. CURRENT selects an immutable control revision containing the ledger and generation reference. This makes backlog consumption and semantic publication atomic together; the cost is extra small immutable metadata revisions.
5. Added optional `packet create --work` to include full prior context for a deliberate existing-work update. Oversized context blocks instead of truncating; the cost is that a large work item can require an increased packet budget.
6. Restricted private exports to the secured state root instead of altering arbitrary folders' permissions. Proposal inputs can remain outside the source/checkout; the cost is that another export destination needs a deliberate owner copy.

## Deferred minor findings

Pending final review.
