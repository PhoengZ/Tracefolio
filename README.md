# Tracefolio

Turn configured code history and work notes into English, evidence-linked project/work JSON. Tracefolio collects commit messages **and changes**, prepares bounded investigation packets, validates proposals from your existing LLM harness, and aggregates the resulting knowledge.

Phase 1 is a work-knowledge foundation. It does not generate resumes, rank jobs, schedule runs, connect email, or send messages. Its semantic layer defines projects, work identities, source scope, evidence, and claim meaning; it is not a SQL metrics layer or an embedding database.

## Install

Requires Python 3.12+ and Git. In a virtual environment:

```text
python -m pip install -e ".[dev]"
tracefolio --help
```

To also install the optional local RenderCV toolchain for explicit LLM-guided resume generation (see [the resume workflow](docs/resume-workflow.md)):

```text
python -m pip install -e ".[dev,resume]"
```

If this checkout has a privately prepared, Git-ignored `bindings.local.json`, load it before running the CLI in PowerShell:

```powershell
. ./load-config.ps1
./.venv/Scripts/python.exe -m tracefolio.cli config validate
```

The loader sets process-local environment bindings; it does not collect sources or run LLM analysis. Reload it in each new shell/tool process. Keep actual registries and state outside the checkout, as described below.

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

For multiple registries, unset `TRACEFOLIO_CONFIG` first. Shared owner/privacy/budget settings must match; duplicate projects are rejected. Selecting only the public registry needs no local bindings. Disabled projects do not resolve bindings or read sources. Public GitHub sources read directly through the host's authenticated `gh api` CLI while preserving public-only visibility checks (`private == false`), configured scope, and private redaction.

## Use

```text
tracefolio collect --all
tracefolio collect --all --mode snapshot
tracefolio packet create --project <project-id>
tracefolio apply --proposal <absolute-private-proposal-file>
tracefolio summarize --all --format json
```

Collection supports commit-history diffs (`--mode commits`, default) and whole-project HEAD snapshots (`--mode snapshot` or `"collection_mode": "snapshot"` in a route), and embeds a deterministic `semantic_map` of project files, symbols, and work mappings into each investigation packet. Collection alone does **not** create a semantic summary. An existing agent/harness reads the packet and writes the proposal. See [the exact harness workflow](docs/harness-workflow.md), including continuation, context reads, and proposal rules. Output JSON is English; source evidence retains its original language. Models can still misunderstand code: inspect evidence and review narratives before reusing them.

To add a project, add a route and its private environment binding, validate, then collect and investigate it. No application-code edit is needed for an existing source kind. Use stable `project_id`/`work_id` values; changing an ID creates a different identity. Work-unit descriptions guide investigation, not assertions of accomplishment.

Acceptance prompt for the agent working in this checkout:

> Summarize all configured projects and work items. Update changed sources first. Return English portfolio JSON with evidence references, unknowns, coverage, and review status. Do not create a resume or send any messages.

`summarize` reads existing knowledge without collecting or analyzing sources. Ordinary requests use `summarize --all --view brief --format json`; explicit updates first use `status --all` and the incremental workflow. The full JSON view remains the compatibility default and `--out` always saves the full private artifact. `partial` is coverage metadata, not an instruction to reanalyze. Every configured project appears, including disabled, pending, partial, and failed projects. Exit codes: `0` success/ready, `2` invalid or unsafe input, `3` incomplete/retryable, `4` operational failure. JSON stdout uses ASCII escapes (a UTF-8 subset); persisted files are UTF-8. Safe JSON diagnostics use stderr. Export with `--out <absolute-private-state>/exports/portfolio.json`; other state namespaces are protected from export writes. No public export exists.

## Verification and limits

```text
python -m ruff check .
python -m pytest -q
```

Tests use synthetic local repositories and a fake GitHub API client. Windows and Linux CI are configured; consult actual CI results before claiming a platform passed. GitHub API limits, missing patches, large files, rewritten history, and bounded packets can leave coverage partial. Notebooks contribute code/Markdown cells only, without execution or saved outputs. No measured impact is inferred from a successful training script.

Read [security boundaries](docs/security.md) and [the harness workflow](docs/harness-workflow.md). Private source content may still be confidential after heuristic redaction. Your chosen external harness may transmit packets to its model provider.

Private state can optionally live at the validated Git-ignored `.tracefolio-local/state` location under an explicitly bound checkout. Registries remain external. See [repository-local private state](docs/harness-workflow.md#repository-local-private-state).

## Offline work feedback workbench

Open [tools/work-feedback.html](tools/work-feedback.html) directly in a local browser (offline only, no network calls) to review works without inspecting raw JSON by hand. It accepts `.jsonl` files (such as the synthetic [examples/works.example.jsonl](examples/works.example.jsonl)), `summarize --all --format json` / `--view brief` portfolio JSON, individual `works/*.json` files, folder uploads, or pasted JSON/JSONL, and exports structured feedback (`.jsonl` / `.json`) with correctness/ownership verdicts and reasons. You can also attach an `audit authorship` JSON export (`+ Attach Authorship Audit (.json)` or drag-and-drop alongside portfolio JSON) to surface Layer 1 component breakdowns and filter directly to risky False Positive / False Negative works.

To persist confirmed human reviews (`verdict == "correct"`) back into private state without weakening the LLM `machine_review_forbidden` guard on `apply --proposal`, run:

```text
tracefolio review apply --feedback <absolute-private-feedback-file>
```

Confirmed works become `review_status: "user_reviewed"` in a new immutable generation (preserving `record_revision` so repeated imports are idempotent and only matching revisions are accepted). Any subsequent source invalidation or LLM proposal update automatically resets only the modified work back to `"unreviewed"`. Works already marked `"user_reviewed"` in `portfolio.json` are recognized automatically in `tools/work-feedback.html`.

## Optional resume generation (private local checkout)

Tracefolio keeps personal career details and generated resumes strictly out of this public repository. To enable explicit LLM-guided resume generation using your saved `.tracefolio-local` work knowledge and [RenderCV](https://rendercv.com/):

1. Install the optional RenderCV toolchain into `.venv`:

   ```text
   python -m pip install -e ".[dev,resume]"
   ```

2. Clone or initialize your own **private** resume repository inside the Git-ignored `resumes/` directory at the repository root (for example, `git clone <your-private-resume-repo> resumes`).
3. Copy [the synthetic profile example](examples/profile.example.yaml) to `resumes/profile.yaml` and fill in your personal contact details, education, employment records, and `linked_project_ids` (matching your Tracefolio `project_id`s).
4. Follow [the resume generation workflow](docs/resume-workflow.md): the agent asks for your target role first, researches current role criteria via web search, joins `resumes/profile.yaml` with read-only `tracefolio summarize` evidence, and renders a 1-page ATS PDF/PNG into `resumes/Resume-<Position>/`.

Because `/resumes/`, `/docs/Resume.pdf`, and `/rendercv_output/` are Git-ignored in this public checkout, personal profile data and generated PDFs stay private to your machine and your separate private resume repository.
