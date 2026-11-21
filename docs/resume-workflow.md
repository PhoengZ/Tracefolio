# Resume generation workflow

This workflow defines how an LLM agent generates a role-tailored, ATS-friendly, 1-page PDF resume using saved work knowledge from `.tracefolio-local`, a private baseline profile, live market criteria from web search, and local **RenderCV** (`rendercv[full]`).

> **Activation boundary:** Only run this workflow when the user **explicitly** asks to create or update a resume. Ordinary Tracefolio work-summary requests follow [docs/harness-workflow.md](harness-workflow.md) and never generate resumes.

## Privacy, truthfulness, and scope guardrails

1. **Zero PII in this public repository:** Never write personal contact details, real baseline resume contents, or private repository URLs into tracked files in `tracfolio`. Keep `docs/Resume.pdf`, `.superpowers/`, `.tracefolio-local/`, `resumes/`, and `rendercv_output/` Git-ignored and untracked.
2. **Read-only Tracefolio access:** Read existing saved summaries via the Tracefolio CLI (`summarize`). Do **not** run `tracefolio collect`, create analysis packets, or apply proposals during resume generation unless the user explicitly asks to refresh portfolio data first.
3. **No fabrication:** Never invent metrics, user counts, latencies, deployment claims, employment titles, dates, or sole-authorship assertions.
   - Distinguish **user-asserted baseline facts** from the user's existing `docs/Resume.pdf` vs. **code-evidenced works** from `.tracefolio-local`.
   - If a Tracefolio work lacks numeric evaluation evidence, describe concrete evidenced architecture, engineering scope, and responsibilities—or ask the user to confirm real measured outcomes.
4. **Authorship & AI-tool policy:** Follow [docs/authorship-workflow.md](authorship-workflow.md). Omit AI assistant names, AI co-author labels, and routine AI-use disclosure from resume drafts by default. Respect human collaboration and `unverified` ownership distinctions.
5. **No automatic pushes:** Never run `git push` on `tracfolio` or on a private resume repository unless explicitly instructed by the user.

---

## Step 1 — Mandatory target-role gate (HARD STOP)

Before performing any web search, reading project details, or drafting content, check whether the user has explicitly specified the **target role / job track** (for example: `Software Engineer`, `Backend Engineer`, `Full-Stack Engineer`, `AI Engineer`, `LLM / Agentic AI Engineer`, `Data Engineer`, `ML Engineer`, or `Data Scientist`).

- **If the target role is missing or ambiguous, STOP immediately and ask the user.** Do not guess a role or generate a generic resume.
- In the same brief prompt, confirm any helpful optional parameters (or state clear defaults):
  - Target seniority / level (e.g., Intern, New Grad, Junior, Mid-level)
  - Optional specific Job Description (JD) if the user wants JD-specific tailoring
  - Output folder slug: `resumes/Resume-<Position>/` (for example, `resumes/Resume-Software-Engineer/`)
  - Page size: `us-letter` (default, matching classic US/ATS style) or `a4`, strictly **1 page**

---

## Step 2 — Research target-role criteria via Web Search (MANDATORY)

Once the user confirms the target role, use the **web search tool** to research current hiring criteria from real internet sources (such as LinkedIn job postings/skills expectations, engineering hiring guides, and role benchmarks) for that exact role and seniority.

Synthesize a concise **Target Role Rubric** before touching project selection:

1. **Core Technical Skills & Expected Stack:** Languages, frameworks, databases, infra, and domain tooling most frequently requested for this role.
2. **Must-Have ATS Keywords:** Standard industry terms recruiters and ATS filters screen for.
3. **High-Signal Project & Experience Patterns:** Concrete architectural/system signals that differentiate strong candidates in this track (e.g., concurrency/distributed systems/CI-CD for SWE; agentic pipelines/RAG/evals/guardrails for AI Engineer; orchestration/data modeling/ETL reliability for Data Engineer).
4. **Low-Signal / Off-Track Details to Trim:** Topics to condense or omit so the 1-page budget goes to highest-relevance evidence.

---

## Step 3 — Load private inputs (read-only)

1. **Primary Career & Identity Config (`resumes/profile.yaml`):** Read the local Git-ignored `resumes/profile.yaml` (modeled on [examples/profile.example.yaml](../examples/profile.example.yaml)) as the canonical source of truth for personal/employment metadata:
   - Candidate header/contact links (`basics`: name, location, phone, email, website/portfolio, GitHub, LinkedIn)
   - Education (`education`: institution, degree, graduation timeline, GPAX)
   - Employment records (`experiences`: company, position, start/end dates, location, `linked_project_ids`, and `manual_highlights` for user-asserted metrics or roles without local repositories)
   - Non-company project metadata (`projects_meta`: Capstone, open-source, or side-project roles, categories, dates, and URLs linked to Tracefolio `project_id`s)
   - Baseline skills, languages, and awards
   - **One-time bootstrap fallback:** Only if `resumes/profile.yaml` does not exist yet, read the local Git-ignored `docs/Resume.pdf` (or another user-provided baseline resume) to bootstrap `resumes/profile.yaml` and ask the user to verify extracted fields (especially hidden hyperlinks such as Portfolio URLs) before reusing it.
2. **Saved Tracefolio work knowledge:** Load local bindings and query saved summaries read-only in a single PowerShell invocation:

   ```powershell
   . ./load-config.ps1
   ./.venv/Scripts/python.exe -m tracefolio.cli summarize --all --view brief --format json
   ```

   - **Automatic join, project date resolution, & unmapped-project prompt:** Join each enabled Tracefolio `project_id` with `experiences[].linked_project_ids` and `projects_meta[]` in `resumes/profile.yaml`.
     - If a project in `projects_meta[]` has empty `start_date`/`end_date`, populate defaults from the deterministic `activity_window` (`first_activity_at` → `last_activity_at`, formatted as `YYYY-MM`) returned by `tracefolio summarize`. Note clearly to the user when `activity_window.sources` contains `"filesystem_mtime"` (since non-Git directory modification times can shift after file copies). Always keep **employment** dates in `experiences[]` anchored to `resumes/profile.yaml` (never infer HR employment dates from commits).
     - If `.tracefolio-local` contains any new enabled `project_id` that is not yet mapped in `resumes/profile.yaml`, briefly ask the user once whether it belongs to an employer or is a Capstone/side project (with role and date range) and record that mapping into `resumes/profile.yaml` so future runs stay in sync automatically.
   - For candidate projects shortlisted against the Step 2 Rubric, inspect deeper claim/action details read-only with:

     ```powershell
     . ./load-config.ps1
     ./.venv/Scripts/python.exe -m tracefolio.cli summarize --project <project-id> --view full --format json
     ```

   - If `.tracefolio-local/state/exports/authorship-audit.json` exists, consult it before framing personal ownership on multi-author repositories.
3. **Layout reference:** Inspect the local Git-ignored `.superpowers/Resume ตัวอย่าง.jpg` (if present) as the visual style target.

---

## Step 4 — Map evidence to role criteria & confirmation gate

Match every work experience and Tracefolio project against the Step 2 **Target Role Rubric**:

- **Select & rank** the strongest experiences and projects for the chosen track so the entire resume fits cleanly on **1 page**.
- **Tailor bullet framing** using `Action Verb + System/Architecture Scope + Technologies + Evidenced Outcome` while aligning vocabulary honestly with ATS keywords from Step 2.
- **Re-group `Technical Skills`** so the most role-relevant category appears first and every listed skill is genuinely evidenced in `docs/Resume.pdf` or `.tracefolio-local`.
- **Present a concise Content Blueprint** to the user (shortlisted experiences, shortlisted Tracefolio projects with logical `project_id`/`work_id`, proposed skill ordering, and any optional questions if the user wants to supply real measured numbers) and wait for confirmation before rendering—unless the user explicitly asked to render directly.

---

## Step 5 — Layout & RenderCV v2.8 YAML specification

Install the optional local RenderCV toolchain once inside `.venv` if not already installed:

```powershell
./.venv/Scripts/python.exe -m pip install -e ".[dev,resume]"
```

### Style rules (modeled on `.superpowers/Resume ตัวอย่าง.jpg`)

- **Single-column classic serif ATS layout:** Use RenderCV `design.theme: harvard` (serif `XCharter` font, centered name/contact header, centered bold ALL-CAPS section headers with horizontal rules, left-aligned titles/roles, right-aligned dates/locations).
- **Strict 1-page budget:** Disable footer and top note (`show_footer: false`, `show_top_note: false`), use balanced margins (`0.45in`–`0.5in`), and use bold lead-in topic labels (`**Topic:** ...`) in bullet points when grouping responsibilities cleanly.
- **Mandatory exclusion:** **NEVER include citizenship, visa, or work-authorization statements** (specifically omit footer notes like `Not a US Citizen; Approved I-140; Require H1-B transfer` from the reference image).

### Synthetic RenderCV v2.8 YAML template

Replace all synthetic placeholders below with the user's verified data inside the private `resumes/Resume-<Position>/resume.yaml` file (never commit real data to `tracfolio`):

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/rendercv/rendercv/refs/tags/v2.8/schema.json
cv:
  name: Jane Doe
  location: Bangkok, Thailand
  email: jane.doe@example.com
  phone: "+66-80-000-0000"
  website: https://example.com
  social_networks:
    - network: GitHub
      username: janedoe
    - network: LinkedIn
      username: janedoe
  sections:
    education:
      - institution: Example University
        area: Computer Engineering
        degree: B.Eng.
        start_date: 2023-08
        end_date: 2027-05
        location: Bangkok, Thailand
        highlights:
          - "GPAX: 3.85"
    experience:
      - company: Example Corp
        position: Software Engineer Intern
        start_date: 2026-06
        end_date: present
        location: Bangkok, Thailand
        highlights:
          - "**Backend Architecture:** Designed event-driven microservices in Go and RabbitMQ with deterministic validation."
    projects:
      - name: Example Project
        start_date: 2026-08
        end_date: present
        summary: Short role or architectural summary
        highlights:
          - "**Core Implementation:** Built modular pipeline with automated CI quality gates."
    technical_skills:
      - label: Core Stack
        details: Python, Go, TypeScript, PostgreSQL, Docker, GitHub Actions
      - label: Languages
        details: Thai (Native), English (Conversational Proficiency)
design:
  theme: harvard
  page:
    size: us-letter
    top_margin: 0.45in
    bottom_margin: 0.45in
    left_margin: 0.5in
    right_margin: 0.5in
    show_footer: false
    show_top_note: false
  typography:
    font_size:
      body: 9.5pt
      name: 20pt
      connections: 9pt
      section_titles: 1.15em
  section_titles:
    type: centered_with_full_line
    line_thickness: 0.5pt
    space_above: 0.35cm
    space_below: 0.15cm
  sections:
    space_between_regular_entries: 0.7em
    space_between_text_based_entries: 0.25em
  entries:
    highlights:
      space_between_items: 0.15em
locale:
  language: english
settings:
  render_command:
    dont_generate_markdown: true
    dont_generate_html: true
```

---

## Step 6 — Render PDF & PNG + verification loop

Write the YAML file to the Git-ignored private path `resumes/Resume-<Position>/resume.yaml` and render PDF + PNG:

```powershell
./.venv/Scripts/rendercv.exe render resumes/Resume-<Position>/resume.yaml -o resumes/Resume-<Position>/output --dont-generate-markdown --dont-generate-html
```

Run all checks before presenting the final PDF to the user:

1. **Schema validation:** Confirm `rendercv render` exits with code `0`. If Pydantic reports a validation error, fix the reported YAML field and re-render.
2. **Exact 1-page check:** Inspect `resumes/Resume-<Position>/output/` and confirm **exactly 1 PNG page** (`*_1.png` only, no `*_2.png`). If content spills onto page 2, tighten wording or adjust vertical spacing in `design:` and re-render.
3. **Visual check:** Read the generated PNG image to verify typography, alignment, margins, no awkward line wraps, no citizenship/visa footer, and no AI-disclosure text.
4. **Data-leak check:** Run `git status --short` in the `tracfolio` checkout and confirm zero files from `docs/Resume.pdf`, `resumes/`, or `rendercv_output/` appear as tracked or untracked.

---

## Step 7 — Private resume repository & CI/CD auto-build

Because `tracfolio` is a public repository, keep generated resumes in a **separate private Git repository** (which can be cloned directly into the Git-ignored `resumes/` directory inside this workspace, or maintained as a sibling private checkout).

Recommended directory layout inside the private resume repository:

```text
<private-resume-repo>/
├── .github/
│   └── workflows/
│       └── build-resumes.yml
├── Resume-Software-Engineer/
│   └── resume.yaml
├── Resume-AI-Engineer/
│   └── resume.yaml
└── Resume-Data-Engineer/
    └── resume.yaml
```

### GitHub Actions auto-build template (for the private resume repository)

Place the following workflow inside `.github/workflows/build-resumes.yml` in the **private resume repository** so every push to `main` automatically validates and builds PDFs with RenderCV and uploads them as private workflow artifacts (or commits built PDFs back to the private repo if desired):

```yaml
name: Build Resumes (RenderCV)

on:
  push:
    branches: [main]
    paths:
      - "Resume-*/**/*.yaml"
      - ".github/workflows/build-resumes.yml"
  workflow_dispatch:

permissions:
  contents: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install RenderCV
        run: python -m pip install "rendercv[full]>=2,<3"

      - name: Render all Resume-* YAML files
        run: |
          shopt -s nullglob
          for yaml in Resume-*/resume.yaml; do
            dir="$(dirname "$yaml")"
            echo "Rendering $yaml -> $dir/output"
            rendercv render "$yaml" -o "$dir/output" --dont-generate-markdown --dont-generate-html
            png_count=$(find "$dir/output" -maxdepth 1 -name '*.png' | wc -l)
            if [ "$png_count" -ne 1 ]; then
              echo "::error::Expected 1 page for $yaml, found $png_count PNG pages."
              exit 1
            fi
          done

      - name: Upload built PDFs as private artifacts
        uses: actions/upload-artifact@v4
        with:
          name: resumes-pdf
          path: Resume-*/output/*.pdf
          if-no-files-found: error
```

> **Optional commit-back:** If you also want rendered `*.pdf` files browsable directly inside the private repository (e.g., from mobile GitHub), add a commit-back step using `git add Resume-*/output/*.pdf` with `[skip ci]` inside the private repository's workflow. Never run `git push` automatically from an LLM agent without explicit user confirmation.
