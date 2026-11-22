import base64
import json
import os
import subprocess

import pytest

from tracefolio.authorship import (
    audit_projects,
    classify_commit_attribution,
    export_audit,
    parse_coauthor_trailers,
)
from tracefolio.cli import main
from tracefolio.collection import collect_projects, source_fingerprint
from tracefolio.contracts.knowledge import Claim, ProjectRecord, Provenance, WorkRecord
from tracefolio.contracts.routes import Limits, ProjectRoute, RouteRegistry, SourceRoute
from tracefolio.errors import TracefolioError
from tracefolio.privacy import PrivacyPolicy
from tracefolio.private_config import ResolvedConfig, load_config
from tracefolio.sources.base import SourceRequest
from tracefolio.sources.github_public import ApiResponse, GitHubPublicProvider
from tracefolio.sources.local_git import LocalGitProvider
from tracefolio.state import StateStore

OWNER_EMAIL = "owner@example.invalid"
PEER_EMAIL = "peer@example.invalid"
OWNER_GH_ID = 424242
PEER_GH_ID = 999999


def _git(root, *args, email=None, name=None):
    env = dict(os.environ)
    if email:
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_COMMITTER_EMAIL"] = email
    if name:
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_COMMITTER_NAME"] = name
    res = subprocess.run(["git", "-C", str(root), *args], env=env, capture_output=True, check=True)
    return res.stdout.decode("utf-8", "replace").strip()


def _commit(root, rel_path, content, message, *, email=OWNER_EMAIL, name="Synthetic User"):
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(root, "add", rel_path, email=email, name=name)
    _git(root, "commit", "-qm", message, email=email, name=name)
    return _git(root, "rev-parse", "HEAD")


class MultiAuthorFakeGitHub:
    """Synthetic public repo with 3 components:
    - analytics-service/ (owner only, with AI trailer)
    - api-gateway/ (peer only)
    - contracts/ (owner + peer mixed)
    """

    def __init__(self, *, private=False, rate_limit_components=False):
        self.private = private
        self.rate_limit_components = rate_limit_components
        self.head_sha = "1" * 40
        self.c_analytics = "2" * 40
        self.c_gateway = "3" * 40
        self.c_contracts_owner = "4" * 40
        self.c_contracts_peer = "5" * 40
        self.c_merge = "6" * 40
        self.calls = []

    def get_authenticated_user(self):
        return {"id": OWNER_GH_ID, "login": "synthetic-owner"}

    def _commit_item(self, sha, author_id, email, message, parents=None, files=None):
        item = {
            "sha": sha,
            "author": {"id": author_id} if author_id is not None else None,
            "commit": {"message": message, "author": {"email": email, "date": "2026-01-10T00:00:00Z"}},
            "parents": [{"sha": p} for p in (parents or ["0" * 40])],
        }
        if files is not None:
            item["files"] = files
        return item

    def get_json(self, path, params=None):
        params = params or {}
        self.calls.append((path, dict(params)))
        if path == "/repos/org/backend":
            return ApiResponse({
                "private": self.private,
                "default_branch": "main",
                "full_name": "org/backend",
                "created_at": "2025-10-01T00:00:00Z",
            })
        if path == "/repos/org/backend/commits/main":
            return ApiResponse(self._commit_item(
                self.head_sha, OWNER_GH_ID, OWNER_EMAIL, "head commit",
            ))
        if path == f"/repos/org/backend/git/trees/{self.head_sha}":
            return ApiResponse({
                "sha": self.head_sha,
                "truncated": False,
                "tree": [
                    {"path": "analytics-service/main.go", "mode": "100644", "type": "blob", "sha": "b1", "size": 24},
                    {"path": "api-gateway/proxy.go", "mode": "100644", "type": "blob", "sha": "b2", "size": 24},
                    {"path": "contracts/owner.proto", "mode": "100644", "type": "blob", "sha": "b3", "size": 24},
                    {"path": "contracts/peer.proto", "mode": "100644", "type": "blob", "sha": "b4", "size": 24},
                ],
            })
        if "/git/blobs/" in path:
            body = b"package demo\nfunc Run(){}\n"
            return ApiResponse({
                "sha": path.rsplit("/", 1)[1],
                "size": len(body),
                "encoding": "base64",
                "content": base64.b64encode(body).decode(),
            })
        if path == "/repos/org/backend/commits":
            comp_path = params.get("path")
            if comp_path is None:
                return ApiResponse([
                    self._commit_item(
                        self.c_analytics, OWNER_GH_ID, OWNER_EMAIL,
                        "add analytics\n\nCo-authored-by: Claude <noreply@anthropic.com>",
                    ),
                    self._commit_item(self.c_gateway, PEER_GH_ID, PEER_EMAIL, "add gateway"),
                    self._commit_item(self.c_contracts_owner, OWNER_GH_ID, OWNER_EMAIL, "add owner contract"),
                    self._commit_item(self.c_contracts_peer, PEER_GH_ID, PEER_EMAIL, "add peer contract"),
                    self._commit_item(
                        self.c_merge, OWNER_GH_ID, OWNER_EMAIL, "Merge pull request #9",
                        parents=["a" * 40, "b" * 40],
                    ),
                ])
            if self.rate_limit_components:
                raise TracefolioError("github_rate_limited", exit_code=3)
            if comp_path == "analytics-service":
                return ApiResponse([
                    self._commit_item(
                        self.c_analytics, OWNER_GH_ID, OWNER_EMAIL,
                        "add analytics\n\nCo-authored-by: Claude <noreply@anthropic.com>",
                    ),
                ])
            if comp_path == "api-gateway":
                return ApiResponse([
                    self._commit_item(self.c_gateway, PEER_GH_ID, PEER_EMAIL, "add gateway"),
                    self._commit_item(
                        self.c_merge, OWNER_GH_ID, OWNER_EMAIL, "Merge pull request #9",
                        parents=["a" * 40, "b" * 40],
                    ),
                ])
            if comp_path == "contracts":
                return ApiResponse([
                    self._commit_item(self.c_contracts_owner, OWNER_GH_ID, OWNER_EMAIL, "add owner contract"),
                    self._commit_item(self.c_contracts_peer, PEER_GH_ID, PEER_EMAIL, "add peer contract"),
                ])
            return ApiResponse([])
        if path == f"/repos/org/backend/commits/{self.c_contracts_owner}":
            return ApiResponse(self._commit_item(
                self.c_contracts_owner, OWNER_GH_ID, OWNER_EMAIL, "add owner contract",
                files=[{"filename": "contracts/owner.proto", "status": "added"}],
            ))
        raise AssertionError(f"Unexpected API path: {path} {params}")


def test_coauthor_trailer_distinguishes_ai_tools_from_humans():
    owners = frozenset({OWNER_EMAIL})
    ai_msg = (
        "feat: parser\n\n"
        "Co-authored-by: Claude <noreply@anthropic.com>\n"
        "Co-authored-by: OpenAI Codex <noreply@openai.com>\n"
        "Co-authored-by: gemini-code-assist[bot] <gemini-code-assist[bot]@users.noreply.github.com>\n"
    )
    has_human, has_ai = parse_coauthor_trailers(ai_msg, owners)
    assert not has_human and has_ai

    attr, _, _ = classify_commit_attribution(
        OWNER_EMAIL, parent_count=1, message=ai_msg, owner_emails=owners,
    )
    assert attr == "self"

    # A human named Claude with a normal corporate email must NOT be treated as an AI tool.
    human_claude_msg = "feat: api\n\nCo-authored-by: Claude Dupont <claude@company.invalid>\n"
    has_human, has_ai = parse_coauthor_trailers(human_claude_msg, owners)
    assert has_human and not has_ai
    attr, _, _ = classify_commit_attribution(
        OWNER_EMAIL, parent_count=1, message=human_claude_msg, owner_emails=owners,
    )
    assert attr == "shared"

    # Merge commits stay unverified integration context regardless of author match.
    merge_attr, _, _ = classify_commit_attribution(
        OWNER_EMAIL, parent_count=2, message="Merge branch 'feat'", owner_emails=owners,
    )
    assert merge_attr == "unverified"


def test_local_git_single_pass_snapshot_distinguishes_self_shared_and_other(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "analytics-service/worker.py", "a = 1\n",
            "analytics\n\nCo-authored-by: Codex <noreply@openai.com>", email=OWNER_EMAIL)
    _commit(repo, "api-gateway/router.py", "g = 1\n", "gateway", email=PEER_EMAIL)
    _commit(repo, "shared-lib/util.py", "u = 1\n", "init util", email=OWNER_EMAIL)
    _commit(repo, "shared-lib/util.py", "u = 2\n", "peer edit util", email=PEER_EMAIL)

    req = SourceRequest(
        project_id="demo",
        source=SourceRoute(
            source_id="backend", kind="local_git", collection_mode="snapshot",
            root_env="TRACEFOLIO_ROOT", include_paths=["**/*.py"],
        ),
        locator=str(repo),
        limits=Limits(),
        policy=PrivacyPolicy((str(repo), OWNER_EMAIL)),
        owner_emails=frozenset({OWNER_EMAIL}),
    )
    res = LocalGitProvider().collect_source(req, None)
    by_path = {e.source_relative_path: e for e in res.evidence}
    assert by_path["analytics-service/worker.py"].attribution == "self"
    assert by_path["api-gateway/router.py"].attribution == "other"
    assert by_path["shared-lib/util.py"].attribution == "shared"


def test_github_public_snapshot_separates_components_by_author_id():
    api = MultiAuthorFakeGitHub()
    req = SourceRequest(
        project_id="gelato",
        source=SourceRoute(
            source_id="backend", kind="github_public", collection_mode="snapshot",
            repository_env="TRACEFOLIO_REPO", include_paths=["**/*.go", "**/*.proto"],
        ),
        locator="org/backend",
        limits=Limits(),
        policy=PrivacyPolicy(("org/backend",)),
        owner_github_id=OWNER_GH_ID,
        owner_github_login="synthetic-owner",
    )
    res = GitHubPublicProvider(api).collect_source(req, None)
    assert res.coverage.state == "complete"
    by_path = {e.source_relative_path: e for e in res.evidence}
    assert by_path["analytics-service/main.go"].attribution == "self"
    assert by_path["api-gateway/proxy.go"].attribution == "other"
    assert by_path["contracts/owner.proto"].attribution == "shared"
    assert by_path["contracts/peer.proto"].attribution == "other"
    # Never fetched detail for peer commits
    assert not any(p.endswith(api.c_gateway) for p, _ in api.calls)
    assert not any(p.endswith(api.c_contracts_peer) for p, _ in api.calls)


def test_github_rate_limit_degrades_to_unverified_not_other():
    api = MultiAuthorFakeGitHub(rate_limit_components=True)
    req = SourceRequest(
        project_id="gelato",
        source=SourceRoute(
            source_id="backend", kind="github_public", collection_mode="snapshot",
            repository_env="TRACEFOLIO_REPO", include_paths=["**/*.go", "**/*.proto"],
        ),
        locator="org/backend",
        limits=Limits(),
        policy=PrivacyPolicy(("org/backend",)),
        owner_github_id=OWNER_GH_ID,
    )
    res = GitHubPublicProvider(api).collect_source(req, None)
    assert all(e.attribution == "unverified" for e in res.evidence)


def test_two_layer_audit_derives_emails_for_local_git_and_protects_privacy(tmp_path):
    # Setup 3 projects: github_public (multi-author), local_git (uses owner email derived from GitHub),
    # and local_directory (non-git).
    local_repo = tmp_path / "private-repo"
    local_repo.mkdir()
    _git(local_repo, "init", "-q", "-b", "main")
    _commit(local_repo, "core/engine.py", "def run():\n    return 1\n",
            "build engine", email=OWNER_EMAIL)
    _commit(local_repo, "ui/view.py", "def view():\n    return 2\n",
            "build view", email=PEER_EMAIL)

    notes_dir = tmp_path / "notes-dir"
    notes_dir.mkdir()
    (notes_dir / "design.md").write_text("# Design\n", encoding="utf-8")

    state_root = tmp_path / "state"
    registry = RouteRegistry(
        state_dir_env="TRACEFOLIO_STATE_DIR",
        projects=[
            ProjectRoute(
                project_id="pub-proj",
                title="Public Team Project",
                context="Synthetic public team project.",
                sources=[SourceRoute(
                    source_id="backend", kind="github_public", collection_mode="snapshot",
                    repository_env="TRACEFOLIO_PUB_REPO", include_paths=["**/*.go", "**/*.proto"],
                )],
            ),
            ProjectRoute(
                project_id="local-proj",
                title="Private Local Project",
                context="Synthetic private local git project.",
                sources=[SourceRoute(
                    source_id="service", kind="local_git", collection_mode="snapshot",
                    root_env="TRACEFOLIO_LOCAL_REPO", include_paths=["**/*.py"],
                )],
            ),
            ProjectRoute(
                project_id="dir-proj",
                title="Non-Git Notes",
                context="Synthetic directory project.",
                sources=[SourceRoute(
                    source_id="notes", kind="local_directory",
                    root_env="TRACEFOLIO_NOTES_DIR", include_paths=["*.md"],
                )],
            ),
        ],
    )
    config = ResolvedConfig(
        registry=registry,
        state_root=state_root,
        config_paths=[],
        locators={
            ("pub-proj", "backend"): "org/backend",
            ("local-proj", "service"): str(local_repo),
            ("dir-proj", "notes"): str(notes_dir),
        },
        policy=PrivacyPolicy((str(local_repo), str(notes_dir), "org/backend", OWNER_EMAIL, PEER_EMAIL)),
        author_emails=frozenset(),  # Intentionally empty: emails must be derived from GitHub author.id!
    )

    fake_gh = MultiAuthorFakeGitHub()
    collect_projects(
        config,
        ["pub-proj", "local-proj", "dir-proj"],
        providers={"github_public": GitHubPublicProvider(fake_gh)},
    )

    store = StateStore.open(config)
    # Publish a snapshot-backed work on pub-proj with one claim on analytics-service and one on api-gateway.
    pub_ledger = store.read_state("pub-proj")
    ev_by_path = {store.read_evidence(eid).source_relative_path: eid for eid in pub_ledger.evidence_ids}
    work = WorkRecord(
        project_id="pub-proj",
        work_id="backend-services",
        record_revision=1,
        title="Backend Services",
        claims=[
            Claim(claim_id="analytics-claim", statement="Implements analytics.",
                  status="observed", evidence_refs=[ev_by_path["analytics-service/main.go"]]),
            Claim(claim_id="gateway-claim", statement="Implements gateway.",
                  status="observed", evidence_refs=[ev_by_path["api-gateway/proxy.go"]]),
        ],
        provenance=Provenance(
            source_ids=["backend"],
            evidence_refs=[ev_by_path["analytics-service/main.go"], ev_by_path["api-gateway/proxy.go"]],
        ),
        analysis_status="ready",
    )
    proj_rec = ProjectRecord(
        project_id="pub-proj",
        record_revision=1,
        title="Public Team Project",
        summary="Backend services.",
        source_ids=["backend"],
        work_ids=["backend-services"],
        evidence_ids=pub_ledger.evidence_ids,
        coverage=pub_ledger.coverage(),
    )
    pub_ledger.pending_analysis = []
    store.publish("pub-proj", None, proj_rec, [work], pub_ledger)
    before_state = store.read_state("pub-proj").model_dump_json()

    report = audit_projects(store, registry, github_transport=fake_gh)
    assert report.identity.github_authenticated is True
    assert report.identity.derived_email_count >= 1
    assert report.identity.effective_email_count >= 1

    # Immutability: audit must not alter CURRENT or state
    assert store.read_state("pub-proj").model_dump_json() == before_state

    by_proj = {p.project_id: p for p in report.projects}

    # Layer 1 + Layer 2 on github_public snapshot-backed work
    pub_audit = by_proj["pub-proj"]
    assert pub_audit.assessment == "mixed_or_shared"
    comps = {c.component: c for c in pub_audit.sources[0].components}
    assert comps["analytics-service"].verdict == "self_only"
    assert comps["analytics-service"].ai_assisted_commits == 1
    assert comps["api-gateway"].verdict == "other_only"
    assert comps["api-gateway"].self_commits == 0
    assert comps["api-gateway"].merge_context_commits == 1
    assert comps["contracts"].verdict == "shared_with_others"
    assert comps["contracts"].self_touched_files_count == 1
    assert comps["contracts"].total_head_files_count == 2

    work_audit = pub_audit.works[0]
    assert work_audit.assessment == "shared_supported"
    assert work_audit.semantic_contribution_status_unchanged == "unverified"
    claims_by_id = {c.claim_id: c for c in work_audit.claims}
    assert claims_by_id["analytics-claim"].support_verdict == "self_supported"
    assert claims_by_id["gateway-claim"].support_verdict == "other_authored_only"

    # Layer 1 on local_git using derived email from GitHub
    local_audit = by_proj["local-proj"]
    assert local_audit.assessment == "mixed_or_shared"
    local_comps = {c.component: c for c in local_audit.sources[0].components}
    assert local_comps["core"].verdict == "self_only"
    assert local_comps["core"].self_lines_added == 2
    assert local_comps["ui"].verdict == "unmatched_local_only"
    assert local_audit.sources[0].unmatched_local_author_count == 1

    # Layer 1 on local_directory -> unverifiable_non_git
    dir_audit = by_proj["dir-proj"]
    assert dir_audit.assessment == "unverifiable_non_git"
    assert dir_audit.sources[0].components[0].verdict == "unverifiable_non_git"

    # Export safety & zero PII/path leakage
    out_file = state_root / "exports" / "authorship-component-audit.json"
    export_audit(store, report, out_file)
    serialized = out_file.read_text(encoding="utf-8")
    assert OWNER_EMAIL not in serialized
    assert PEER_EMAIL not in serialized
    assert str(local_repo) not in serialized
    assert str(notes_dir) not in serialized
    assert "org/backend" not in serialized

    with pytest.raises(TracefolioError, match="unsafe_export"):
        export_audit(store, report, tmp_path / "outside.json")


def test_source_fingerprint_backward_compatible_when_github_id_unset(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text("a = 1\n", encoding="utf-8")
    private = tmp_path / "priv"
    private.mkdir()
    routes_path = private / "routes.json"
    routes_path.write_text(json.dumps({
        "schema_version": "1.0.0",
        "state_dir_env": "TRACEFOLIO_STATE_DIR",
        "projects": [{
            "project_id": "demo",
            "title": "Demo",
            "context": "Demo.",
            "sources": [{
                "source_id": "code",
                "kind": "local_directory",
                "root_env": "TRACEFOLIO_SRC",
                "include_paths": ["*.py"],
            }],
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("TRACEFOLIO_SRC", str(source))
    monkeypatch.setenv("TRACEFOLIO_STATE_DIR", str(tmp_path / "state"))
    cfg = load_config(routes_path)
    fp_without = source_fingerprint(cfg, cfg.registry.projects[0], cfg.registry.projects[0].sources[0])
    cfg_with = ResolvedConfig(
        cfg.registry, cfg.state_root, cfg.config_paths, cfg.locators, cfg.policy,
        cfg.author_emails, cfg.local_state_checkout, github_user_id=99,
    )
    fp_with = source_fingerprint(cfg_with, cfg.registry.projects[0], cfg.registry.projects[0].sources[0])
    assert fp_without != fp_with


def test_cli_audit_authorship_subcommand(knowledge_workspace, monkeypatch, capsys):
    cfg, _, path = knowledge_workspace
    collect_projects(cfg, ["demo"])
    monkeypatch.delenv("TRACEFOLIO_CONFIG_FILES", raising=False)
    monkeypatch.setenv("TRACEFOLIO_CONFIG", str(path))
    target = cfg.state_root / "exports" / "audit.json"
    rc = main(["audit", "authorship", "--all", "--out", str(target)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["projects"][0]["project_id"] == "demo"
    assert out["projects"][0]["assessment"] == "unverifiable_non_git"
    assert json.loads(target.read_text(encoding="utf-8")) == out


def test_anti_taint_readme_and_minor_glue_touches():
    from collections import Counter

    from tracefolio.authorship import _derive_support_assessment

    # 1. README.md-only owner touch must not taint an otherwise other-authored app into shared_supported
    verdict, share = _derive_support_assessment(
        Counter({"other_account": 10}),
        [
            ("other_account", "app/page.tsx", 0.0),
            ("other_account", "components/Header.tsx", 0.0),
            ("shared", "README.md", 0.25),
        ],
    )
    assert verdict == "other_authored_only"
    assert share == 0.0

    # 2. Minor shared glue edit (<20% share, <=1 self commit) in teammate-dominated local work -> minor_touch_only
    verdict, share = _derive_support_assessment(
        Counter({"unmatched_local_author": 4}),
        [
            ("unmatched_local_author", "src/nudges/Agent.ts", 0.0),
            ("unmatched_local_author", "src/nudges/Backoff.ts", 0.0),
            ("unmatched_local_author", "src/nudges/Repo.ts", 0.0),
            ("unmatched_local_author", "src/nudges/Scheduler.ts", 0.0),
            ("shared", "src/integrations/Client.ts", 0.4),
            ("shared", "src/db/schema.ts", 0.6),
            ("shared", "src/index.ts", 0.2),
        ],
    )
    assert verdict == "minor_touch_only"
    assert share is not None and share < 0.20

    # 3. Genuine co-owned work with high owner share stays shared_supported
    verdict, share = _derive_support_assessment(
        Counter({"self": 5, "other_account": 3}),
        [
            ("self", "analytics-service/main.go", 1.0),
            ("shared", "api-gateway/auth.go", 0.6),
            ("other_account", "auth-service/login.go", 0.0),
        ],
    )
    assert verdict == "shared_supported"
    assert share is not None and share >= 0.50


def test_audit_work_uses_active_claim_refs_without_historical_provenance_taint(tmp_path):
    from tracefolio.contracts.knowledge import Skill

    repo = tmp_path / "mixed-repo"
    repo.mkdir()
    _git(repo, "init")
    _commit(repo, "mine/pipeline.py", "def run():\n    return 1\n", "feat: mine", email=OWNER_EMAIL)
    _commit(repo, "teammate/other.py", "def other():\n    return 2\n", "feat: peer", email=PEER_EMAIL)

    state_root = tmp_path / "state"
    registry = RouteRegistry(
        limits=Limits(),
        projects=[
            ProjectRoute(
                project_id="demo",
                title="Demo",
                context="Synthetic mixed repo.",
                sources=[SourceRoute(
                    source_id="repo",
                    kind="local_git",
                    collection_mode="snapshot",
                    root_env="TRACEFOLIO_DEMO_REPO",
                    include_paths=["**/*.py"],
                )],
            ),
        ],
    )
    config = ResolvedConfig(
        registry=registry,
        state_root=state_root,
        config_paths=[],
        locators={("demo", "repo"): str(repo)},
        policy=PrivacyPolicy((str(repo), OWNER_EMAIL, PEER_EMAIL)),
        author_emails=frozenset({OWNER_EMAIL}),
    )
    collect_projects(config, ["demo"])

    store = StateStore.open(config)
    ledger = store.read_state("demo")
    ev_by_path = {store.read_evidence(eid).source_relative_path: eid for eid in ledger.evidence_ids}
    mine_id = ev_by_path["mine/pipeline.py"]
    other_id = ev_by_path["teammate/other.py"]

    # Simulate a generation whose provenance retains prior teammate evidence_ref,
    # while active claims/skills cite ONLY the owner's self-authored file.
    work = WorkRecord(
        project_id="demo",
        work_id="ocr-pipeline",
        record_revision=2,
        title="Owner pipeline",
        actions=["Built pipeline"],
        skills=[Skill(name="Python", evidence_refs=[mine_id])],
        claims=[Claim(claim_id="c1", statement="Owner built pipeline.", status="observed", evidence_refs=[mine_id])],
        contribution="self_authored",
        implementation_status="implemented",
        analysis_status="ready",
        review_status="unreviewed",
        provenance=Provenance(source_ids=["repo"], evidence_refs=[mine_id, other_id]),
    )
    project_rec = ProjectRecord(
        project_id="demo",
        record_revision=2,
        title="Demo",
        summary="Demo summary",
        source_ids=["repo"],
        work_ids=["ocr-pipeline"],
        evidence_ids=[mine_id, other_id],
        coverage=ledger.coverage(),
    )
    ledger.pending_analysis = []
    store.publish("demo", None, project_rec, [work], ledger)

    report = audit_projects(store, registry)
    wa = report.projects[0].works[0]
    assert wa.assessment == "self_supported"
    assert wa.self_share == 1.0
    assert "unmatched_local_author" not in wa.referenced_path_counts
    assert wa.referenced_path_counts == {"self": 1}
