# Tracefolio

Turn configured code history and work notes into English, evidence-linked project/work JSON. Tracefolio collects commit messages **and changes**, prepares bounded investigation packets, validates proposals from your existing LLM harness, and aggregates the resulting knowledge.

Phase 1 is a work-knowledge foundation. It does not generate resumes, rank jobs, schedule runs, connect email, or send messages. Its semantic layer defines projects, work identities, source scope, evidence, and claim meaning; it is not a SQL metrics layer or an embedding database.

## Install

Requires Python 3.12+ and Git. In a virtual environment:

```text
python -m pip install -e ".[dev]"
tracefolio --help
```

Copy [the synthetic registry](examples/routes.example.json) to a private directory **outside the checkout and source folders**. Edit the copy. Route files contain environment-variable names, never literal paths or repository URLs. Bind those variables privately.

PowerShell example (replace every placeholder):

```powershell
$env:TRACEFOLIO_CONFIG = '<absolute-private-registry-file>'
$env:TRACEFOLIO_STATE_DIR = '<absolute-private-state-directory>'
$env:TRACEFOLIO_PIPELINE_ROOT = '<absolute-source-git-directory>'
$env:TRACEFOLIO_ANALYTICS_ROOT = '<absolute-notes-directory>'
$env:TRACEFOLIO_SERVICE_REPOSITORY = '<owner>/<public-repository>'
$env:TRACEFOLIO_AUTHOR_EMAILS = '[]'
$env:TRACEFOLIO_REDACT_VALUES = '[]'
tracefolio config validate
```

For POSIX shells use `export NAME='value'`. Optional identity/redaction bindings are JSON arrays of strings. Without verified author identities, contribution stays `unverified`. Add private endpoint strings to the redaction array. Do not paste secrets into prompts or commit them.

## Public and local registries

Use `github_public` with `repository_env` for confirmed public repositories; use `local_git` or `local_directory` with `root_env` for private/local work. Each project belongs to one registry. A project can contain several sources.

Select exactly one of:

- `TRACEFOLIO_CONFIG`: one absolute private registry filename.
- `TRACEFOLIO_CONFIG_FILES`: a JSON array of absolute private registry filenames.

For multiple registries, unset `TRACEFOLIO_CONFIG` first. Shared owner/privacy/budget settings must match; duplicate projects are rejected. Selecting only the public registry needs no local bindings. Disabled projects do not resolve bindings or read sources. Public GitHub uses anonymous HTTPS and never falls back to authentication or a local clone.

## Use

```text
tracefolio collect --all
tracefolio packet create --project <project-id>
tracefolio apply --proposal <absolute-private-proposal-file>
tracefolio summarize --all --format json
```

Collection alone does **not** create a semantic summary. An existing agent/harness reads the packet and writes the proposal. See [the exact harness workflow](docs/harness-workflow.md), including continuation, context reads, and proposal rules. Output JSON is English; source evidence retains its original language. Models can still misunderstand code: inspect evidence and review narratives before reusing them.

To add a project, add a route and its private environment binding, validate, then collect and investigate it. No application-code edit is needed for an existing source kind. Use stable `project_id`/`work_id` values; changing an ID creates a different identity. Work-unit descriptions guide investigation, not assertions of accomplishment.

Acceptance prompt for the agent working in this checkout:

> Summarize all configured projects and work items. Update changed sources first. Return English portfolio JSON with evidence references, unknowns, coverage, and review status. Do not create a resume or send any messages.

`summarize` reads existing knowledge; the agent follows the full workflow to update it first. Every configured project appears, including disabled, pending, partial, and failed projects. Exit codes: `0` success/ready, `2` invalid or unsafe input, `3` incomplete/retryable, `4` operational failure. JSON stdout and safe JSON diagnostics on stderr are separate. Export with `--out <absolute-path-inside-private-state>`; no public export exists.

## Verification and limits

```text
python -m ruff check .
python -m pytest -q
python -m tracefolio.schema_export --check
```

Tests use synthetic local repositories and a fake GitHub transport. Windows and Linux CI are configured; consult actual CI results before claiming a platform passed. Anonymous GitHub limits, missing patches, large files, rewritten history, and bounded packets can leave coverage partial. Notebooks contribute code/Markdown cells only, without execution or saved outputs. No measured impact is inferred from a successful training script.

Read [security boundaries](docs/security.md), [schemas and state](docs/schema.md), and [manual acceptance](docs/manual-acceptance.md). Private source content may still be confidential after heuristic redaction. Your chosen external harness may transmit packets to its model provider.
