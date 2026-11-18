# GitHub plugin bridge implementation plan

> Execute inline with test-driven development. The supplied implementation brief authorizes execution without another design or review menu.

**Goal:** Let an authorized external harness relay connected GitHub GET responses into the existing public-only collector without credentials or raw disk caches.

**Architecture:** An opt-in STDIO transport emits fixed-host requests and consumes bounded UTF-8 JSON-line replies. Existing GitHub provider logic remains responsible for repository visibility, pinned revisions, collection cursors, scope, attribution and pre-persistence redaction. Headerless full pages conservatively continue pagination.

**Tech stack:** Python 3.12+, standard library, existing pytest suite.

**Spec:** The user-authorized bridge implementation brief supplied in this session.

## Constraints and review focus

- No Python MCP SDK, direct credentials, raw response files, private/local fallback, or owner-identity changes.
- Reject unsafe URLs before emitting requests; never echo malformed replies or tool errors.
- Read binary stdin so UTF-8 replies work under legacy Windows text encodings.
- Bound wire and decoded data bytes, preserve retryable errors, and stop after private metadata.
- Retain existing ordinary CLI behavior unless `--github-plugin` is explicitly selected.

## Tasks

- [x] Write and observe failing tests for request/reply validation, Unicode, failures, pagination, visibility, source-byte budgets, CLI injection, and sanitized publication.
- [x] Add `sources/github_plugin_transport.py`; share fixed-host path validation with the anonymous transport and expose its response limit to the provider.
- [x] Inject the transport into `collect` and `context` via `--github-plugin`; preserve all normal provider checks.
- [x] Verify affected tests, full pytest, Ruff, schema drift and whitespace checks.
- [x] Document exact harness tool flow and transport status caveats; update existing workflow/security pointers without erasing local setup instructions.

Verification: new transport/CLI tests first failed against the missing feature; 44 affected tests then passed. Full suite: 145 passed. Ruff, schema drift and `git diff --check` passed. The author reviewed the diff; the parent harness owns live plugin verification and independent final review. No subagent, commit or push was used.

Review fix: safe URL-encoded reserved characters in refs/filenames were incorrectly rejected. Six regression cases failed before the fix; encoded data is now preserved while malformed escapes and decoding-layer traversal remain blocked. On the subsequently merged token-optimization tree, 72 affected tests and all 176 tests passed; Ruff, schema drift and whitespace checks passed. Incremental references, evidence aliases, wildcard accounting and the user's merged optimization were preserved.

No commit or push is part of this handoff. The parent agent performs the live plugin smoke check and reviews the final diff.
