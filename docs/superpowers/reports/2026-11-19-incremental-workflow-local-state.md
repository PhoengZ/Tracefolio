# Incremental workflow implementation evidence

Plan: `docs/superpowers/plans/2026-11-19-incremental-workflow-local-state.md`

Implementation branch: `feat/incremental-local-state`, based on `6f67dce`.
The existing untracked `.gemini/` directory was left untouched. No push or merge is part of this task.

## Verification

- Baseline before changes: 179 tests passed.
- Final full suite after production changes: 228 tests passed.
- Additional separate-process recovery-owner serialization regression: passed.
- Ruff, exported-schema drift and `git diff --check`: passed.
- Private-state preflight and migration dry-run: verified; no source collection was required.
- Actual migration: 7,349 durable files copied and verified; digest match confirmed and active bindings validated. The original state remains an inactive backup. The separate authorized portfolio refresh completed with no pending analysis. All effective source cursors use snapshot mode, active local state is ignored and untracked, and machine review statuses remain unreviewed. The detailed comparison report stays private.

## Review corrections

The independent branch review was followed by one consolidated fix pass. Regression tests exercised the original failures before corrections:

- Hard process termination, owned project-lock recovery, journal/barrier ordering and recovery-owner serialization.
- Rejection of rollback against an already completed migration; destination verification and write exclusion during interrupted recovery.
- Private, ignored binding temporaries and actual-directory ignore checks.
- Preservation of durable export/proposal names and explicit unreadable-tree failures.
- Privacy-policy and provenance packet/collection validation.
- Writer barriers for exports and rejection of stale retired-root writers.
- Proposal reads in the authorized local private namespace.
- Persistence of effective snapshot mode and per-work review evidence for shared files.
- Read-only status/summary on an uninitialized root, while an explicit export initializes private storage.

The virgin-read issue was regraded from Minor to Important because it violated the read-only acceptance criterion, and fixed. No reviewed findings were deferred.

## Rulings made during execution

1. Use the dedicated branch in the named checkout rather than creating another worktree: this checkout is the selected state destination. Cost: less filesystem isolation; scoped staging preserved unrelated files.
2. Use a PowerShell progress ledger rather than the skill's POSIX helper scripts. Cost: manual bookkeeping; task results and commits were recorded explicitly.
3. Store effective mode and shared-work review obligations in private cursor/ledger fields. Exported artifact schemas remain unchanged. Cost: old cursors require conservative legacy mode inference; continuation and shared-file regressions cover it.
4. Verify historical packets against their original serialized digest when only the known defaulted map/alias fields are missing. Preserve bytes and IDs; do not weaken current proposal validation. Cost: compatibility must remain narrow; legacy acceptance and tamper rejection are tested.
5. Retain the implementation branch without merging or pushing: the approved plan requests a dedicated branch and the user intends to verify the resulting work. Cost: integration remains a separate user decision.

## Operational scope

The user's later confirmation explicitly authorizes a full configured-portfolio refresh after implementation and migration. That operational step is separate from the synthetic development tests. Real state, summaries, evidence, migration journals and comparison reports remain Git ignored; no real source content or private bindings belong in this report.

## Acceptance evidence

AC1/AC4: status/brief/virgin-read/export regressions. AC2/AC3: unchanged acquisition, scoped/shared-work and persisted-mode regressions. AC5: local-state Git/ignore/link/export tests plus actual ignored/untracked checks; package configuration remains restricted to src. AC6/AC7: manifest equality, provenance/policy/legacy validation and process-interruption/rollback/serialization tests plus actual verified migration. AC8: documented command/intent traces and CLI help checked. AC9: complete verification commands passed. No implementation acceptance criterion remains pending; source semantic verification by the user remains separate.
