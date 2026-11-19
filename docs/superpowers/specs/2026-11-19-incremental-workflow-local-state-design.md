# Incremental summary workflow and repository-local private state

Status: proposed design for the requested implementation plan; implementation has not started.

## Intent and decisions

The user expects saved work summaries to be reused, snapshot/hash checks to prevent repeated analysis, and the complete private state to live inside this checkout under Git ignore. The user selected migration of the whole private state rather than a second summary-only copy.

Two storage approaches were considered: a local summary projection with external canonical state, or moving the existing state as one unit. The selected whole-state approach preserves generations, evidence references, cursors, pending analysis and proposal receipts together, with one active state root. External registries and source bindings remain external. The old state is retained as an inactive rollback copy.

The following workflow details are proposed defaults, not previously approved implementation details:

- A request to summarize/show existing work reads saved summaries. It does not collect sources, create packets or apply proposals.
- A request to update/refresh work performs bounded incremental collection for the requested scope, then analyzes only required work. An explicitly requested deliberate review may include prior evidence.
- Preserve the source's configured collection mode. Use snapshot for ongoing snapshot sources; a mode transition is explicit and must not be presented as a newly implemented feature.
- Keep existing `partial` values for truthful coverage and compatibility. Separate coverage from analysis scheduling; `partial` alone never selects a project for reanalysis.
- Retain the full JSON interface and add an explicit brief view for agent/user summaries. Do not print full provenance arrays or raw packet payloads for ordinary summaries.

## Existing behavior and constraints

- `SourceRoute.collection_mode` still defaults to `commits`. Both Git snapshot providers compare HEAD and scoped blob inventories; local directories compare normalized content hashes.
- `new_evidence` counts previously unseen evidence IDs, not pending work. Revisited evidence, source/route changes and previous backlog can require review without new IDs.
- `create_packet` returns `noop` only when pending analysis, invalidation and reconciliation are absent and a generation exists. Incremental packets reuse `prior_works`; their semantic map covers included evidence, not necessarily the entire project.
- `partial` can represent incomplete acquisition, unfinished analysis, or an invalidation overlay. Snapshot eliminates neither source failures nor the distinction between collected and analyzed evidence.
- `load_config_set` currently rejects a state root under the checkout, and `validate_export_path` rejects all checkout exports. Both need a narrow exception, not removal of the source-boundary rules.

## Required behavior

1. Ordinary summaries make zero provider calls and zero packet/proposal writes. Existing coverage gaps remain visible without triggering repair work.
2. A compact status command exposes scheduling reasons separately from coverage: pending evidence count, invalidated work IDs, reconciliation source IDs, missing configured work IDs and source limitations.
3. Updates reuse unchanged summaries. Relevant reasons, not a project-level `partial` flag or `new_evidence` alone, select work. An incomplete source with no analyzable change is reported as blocked with last-good data; it is not polled indefinitely. Unverified authorship, unreviewed machine output and unknown business impact alone do not require reanalysis.
4. Repeated packet reads are unnecessary: use one current packet until apply, stale context, or an explicit need for more scoped context. A persistent blocker ends the attempt. Prior unrelated backlog is reported, not silently included when the user selected particular work.
5. Private storage is allowed only at the fixed relative location `.tracefolio-local/state` under an explicitly bound Git checkout. The binding must point at the actual Git top level. The location must be ignored and untracked, with no symlink/junction escape. Existing external-state configurations remain valid.
6. Only `<active-state>/exports` remains an allowed export namespace. Local state is never a source, dependency artifact, package payload, fixture or CI upload. Real bindings and state values never enter tracked documentation.
7. Migration copies and validates state without recollection, reanalysis, ID changes or rewriting immutable records. It excludes runtime locks and temporary files. Bindings switch only after validation, inside the same writer barrier as the verified copy. Original durable artifacts remain intact; successful activation adds an owned retirement marker that blocks writes through stale old bindings. Failure restores the original active binding and removes only its own temporary markers.
8. Migration uses a state-wide writer barrier and existing project locks so a concurrent collect/apply cannot produce a mixed copy. Migration detects corruption, nonempty destinations, changed source manifests and permission failures before activation. Readers may continue to read last-good data.

## Non-goals

No removal/renaming of persisted status enums, replacement of the collector architecture, new model integration, new authentication route, public export, UI, automatic cleanup of old state, or reanalysis of the real portfolio. No numerical token/time-saving claims without measurement.

## Implementation boundaries

Reuse Pydantic, the existing CLI, atomic JSON writes, privacy policy, synthetic fixtures and project locks. Add small status and migration modules rather than restructuring collectors. Keep full portfolio schemas compatible; new CLI projections do not alter existing persisted artifacts. Planning creates documents only. Implementation and actual migration follow review of this plan.
