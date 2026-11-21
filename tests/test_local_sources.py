import json
import subprocess

import pytest

from tracefolio.contracts.routes import Limits, SourceRoute
from tracefolio.privacy import PrivacyPolicy
from tracefolio.sources.base import SourceRequest
from tracefolio.sources.local_directory import LocalDirectoryProvider
from tracefolio.sources.local_git import LocalGitProvider
from tracefolio.sources.notebooks import normalize_notebook


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout.decode().strip()


def commit(root, content, message="update", filename="main.py"):
    (root / filename).write_text(content, encoding="utf-8")
    git(root, "add", filename)
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Synthetic Author")
    git(root, "config", "user.email", "synthetic@example.invalid")
    commit(root, "value = 1\n", "initial")
    return root


def request(root, kind="local_git", **limits):
    return SourceRequest(
        project_id="demo",
        source=SourceRoute(source_id="code", kind=kind, root_env="TRACEFOLIO_TEST_ROOT",
                           include_paths=["**/*.py", "*.ipynb", "README.md"]),
        locator=str(root), limits=Limits(**limits), policy=PrivacyPolicy((str(root),)),
        owner_emails=frozenset({"synthetic@example.invalid"}),
    )


def test_message_and_actual_diff_are_collected(repo):
    commit(repo, "checkpoint = 10\n", "vague update")
    result = LocalGitProvider().collect_source(request(repo), None)
    assert any("vague update" in e.payload and "checkpoint = 10" in e.payload
               for e in result.evidence)
    assert result.coverage.state == "complete"


def test_uncommitted_edits_not_in_git_evidence(repo):
    (repo / "main.py").write_text("uncommitted_secret_marker = True\n")
    before = git(repo, "status", "--porcelain")
    result = LocalGitProvider().collect_source(request(repo), None)
    assert all("uncommitted_secret_marker" not in e.payload for e in result.evidence)
    assert git(repo, "status", "--porcelain") == before


def test_authorship_is_not_inferred(repo):
    req = request(repo)
    req.owner_emails = frozenset()
    result = LocalGitProvider().collect_source(req, None)
    assert all(e.attribution == "unverified" for e in result.evidence)


def test_resume_after_commit_budget_and_noop(repo):
    commit(repo, "checkpoint = 1\n")
    commit(repo, "checkpoint = 2\n")
    provider = LocalGitProvider()
    req = request(repo, max_commits_per_run=1)
    first = provider.collect_source(req, None)
    assert first.coverage.state == "partial" and first.cursor.head is None
    second = provider.collect_source(req, first.cursor)
    third = provider.collect_source(req, second.cursor)
    assert third.coverage.state == "complete"
    assert provider.collect_source(req, third.cursor).evidence == []


def test_revert_retained_and_context_pinned(repo):
    original = git(repo, "rev-parse", "HEAD")
    commit(repo, "checkpoint = 42\n")
    git(repo, "revert", "--no-edit", "HEAD")
    result = LocalGitProvider().collect_source(request(repo), None)
    assert any("Revert" in e.payload for e in result.evidence)
    context = LocalGitProvider().text_at(request(repo), original, "main.py")
    assert "value = 1" in context


def test_rewritten_head_requires_reconciliation(repo):
    provider = LocalGitProvider()
    old = provider.collect_source(request(repo), None)
    git(repo, "checkout", "--orphan", "replacement")
    commit(repo, "replacement = True\n")
    result = provider.collect_source(request(repo), old.cursor)
    assert "history_reconciled" in result.warnings


def test_merge_resolution_delta_is_retained_as_context(repo):
    git(repo, "checkout", "-qb", "feature")
    commit(repo, "value = 2\n", "feature")
    git(repo, "checkout", "-q", "main")
    commit(repo, "value = 3\n", "main")
    subprocess.run(["git", "-C", str(repo), "merge", "feature"], capture_output=True, check=False)
    commit(repo, "resolved_checkpoint = 4\n", "merge resolution")
    result = LocalGitProvider().collect_source(request(repo), None)
    assert any("merge_context" in e.payload and "resolved_checkpoint" in e.payload
               for e in result.evidence)


def notebook(source="checkpoint = 1", output="FAKE_OUTPUT_SECRET"):
    return json.dumps({"cells": [
        {"cell_type": "code", "source": [source], "outputs": [{"text": output}],
         "execution_count": 7, "metadata": {"secret": "FAKE_METADATA_SECRET"}},
        {"cell_type": "markdown", "source": ["Notebook context"],
         "attachments": {"private": "FAKE_ATTACHMENT_SECRET"}},
    ], "metadata": {"secret": "ROOT_SECRET"}})


def test_notebook_sources_exclude_outputs_metadata_and_attachments():
    result = normalize_notebook(notebook())
    assert "checkpoint = 1" in result.text and "Notebook context" in result.text
    assert "SECRET" not in result.text
    assert len(result.cells) == 2


def test_notebook_changes_never_execute(tmp_path):
    marker = tmp_path / "executed"
    code = f"open({str(marker)!r}, 'w').write('bad')"
    normalize_notebook(notebook(code))
    assert not marker.exists()


def test_notebook_diff_uses_parent_and_commit_cell_sources(repo):
    commit(repo, notebook("checkpoint = 1"), filename="work.ipynb")
    commit(repo, notebook("checkpoint = 2", "OTHER_OUTPUT"), filename="work.ipynb")
    result = LocalGitProvider().collect_source(request(repo), None)
    payload = "\n".join(e.payload for e in result.evidence)
    assert "checkpoint = 2" in payload and "checkpoint = 1" in payload
    assert "FAKE_OUTPUT" not in payload and "OTHER_OUTPUT" not in payload


def test_malformed_or_oversized_notebook_is_partial(tmp_path):
    (tmp_path / "work.ipynb").write_text("{malformed")
    req = request(tmp_path, kind="local_directory")
    result = LocalDirectoryProvider().collect_source(req, None)
    assert result.coverage.state == "partial"
    (tmp_path / "work.ipynb").write_text(notebook(output="x" * 1100000))
    result = LocalDirectoryProvider().collect_source(req, None)
    assert result.coverage.state == "complete"
    assert "checkpoint" in result.evidence[0].payload
    req.limits.max_notebook_bytes = 100
    assert LocalDirectoryProvider().collect_source(req, None).coverage.state == "partial"


def test_snapshot_new_changed_deleted_and_unverified(tmp_path):
    provider = LocalDirectoryProvider()
    req = request(tmp_path, kind="local_directory")
    (tmp_path / "one.py").write_text("one = 1")
    first = provider.collect_source(req, None)
    assert first.evidence[0].attribution == "unverified"
    assert provider.collect_source(req, first.cursor).evidence == []
    (tmp_path / "one.py").unlink()
    result = provider.collect_source(req, first.cursor)
    assert any("deleted" in e.payload.lower() for e in result.evidence)


def test_snapshot_file_budget_resume_and_denied_paths(tmp_path):
    (tmp_path / "one.py").write_text("one = 1")
    (tmp_path / "two.py").write_text("two = 2")
    (tmp_path / ".env").write_text("DO_NOT_READ")
    provider = LocalDirectoryProvider()
    req = request(tmp_path, kind="local_directory", max_files_per_source=1)
    first = provider.collect_source(req, None)
    assert first.coverage.state == "partial"
    second = provider.collect_source(req, first.cursor)
    for _ in range(4):
        if second.coverage.state == "complete":
            break
        second = provider.collect_source(req, second.cursor)
    assert second.coverage.state == "complete"
    assert "DO_NOT_READ" not in str(second.evidence)


def test_context_scope_is_checked_before_reading_git(repo, monkeypatch):
    import tracefolio.sources.local_git as module
    from tracefolio.errors import TracefolioError
    from tracefolio.sources.base import ContextRequest
    calls = []
    original = module.git

    def record(req, *args, **kwargs):
        calls.append(args)
        return original(req, *args, **kwargs)

    monkeypatch.setattr(module, "git", record)
    with pytest.raises(TracefolioError, match="context_out_of_scope"):
        LocalGitProvider().read_context(ContextRequest(
            request(repo), git(repo, "rev-parse", "HEAD"), ".env"))
    assert calls == []


def test_snapshot_detects_changes_to_an_earlier_file_during_scan(tmp_path, monkeypatch):
    (tmp_path / "one.py").write_text("one = 1")
    (tmp_path / "two.py").write_text("two = 2")
    provider = LocalDirectoryProvider()
    original = provider._read

    def mutate(req, path, **kwargs):
        result = original(req, path, **kwargs)
        if path == "two.py":
            (tmp_path / "one.py").write_text("one = changed_after_read")
        return result

    monkeypatch.setattr(provider, "_read", mutate)
    result = provider.collect_source(request(tmp_path, kind="local_directory"), None)
    assert result.coverage.state == "partial"
    assert "one.py" in result.cursor.pending


def test_snapshot_stops_reading_when_raw_byte_budget_is_spent(tmp_path, monkeypatch):
    for name in ("one.py", "two.py", "three.py"):
        (tmp_path / name).write_text("x" * 20)
    provider = LocalDirectoryProvider()
    calls = []
    original = provider._read

    def tracked(req, path, **kwargs):
        result = original(req, path, **kwargs)
        calls.append(result[2])
        return result

    monkeypatch.setattr(provider, "_read", tracked)
    result = provider.collect_source(request(tmp_path, kind="local_directory",
                                            max_total_source_bytes=25), None)
    assert result.coverage.state == "partial"
    assert sum(calls) <= 25


def test_git_raw_blob_reads_respect_total_budget(repo, monkeypatch):
    import tracefolio.sources.local_git as module
    commit(repo, "checkpoint = 10\n")
    original = module.git
    consumed = []

    def tracked(req, *args, **kwargs):
        result = original(req, *args, **kwargs)
        if args[:2] == ("cat-file", "blob") and result:
            consumed.append(len(result))
        return result

    monkeypatch.setattr(module, "git", tracked)
    result = LocalGitProvider().collect_source(request(repo, max_total_source_bytes=10), None)
    assert result.coverage.state == "partial"
    assert sum(consumed) <= 10


def test_git_rename_and_multi_file_commit_deduplicate_message_and_filter_noise(repo):
    from tracefolio.privacy import mandatory_allowed
    assert not mandatory_allowed("poetry.lock")
    assert not mandatory_allowed("frontend/bundle.min.js")
    base_head = git(repo, "rev-parse", "HEAD")
    (repo / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
    (repo / "beta.py").write_text("beta = 2\n", encoding="utf-8")
    (repo / "poetry.lock").write_text("lock = 999\n", encoding="utf-8")
    git(repo, "add", "alpha.py", "beta.py", "poetry.lock")
    git(repo, "commit", "-qm", "multi file feature\n\nDetailed body paragraph only needed once.")
    git(repo, "mv", "alpha.py", "renamed_alpha.py")
    git(repo, "commit", "-qm", "rename alpha")
    from tracefolio.contracts.evidence import SourceCursor
    result = LocalGitProvider().collect_source(request(repo), SourceCursor(head=base_head))
    assert all(e.source_relative_path != "poetry.lock" for e in result.evidence)
    multi = [e for e in result.evidence if "multi file feature" in e.payload]
    assert len(multi) == 2
    assert sum("Detailed body paragraph only needed once." in e.payload for e in multi) == 1
    renamed = next(e for e in result.evidence if e.source_relative_path == "renamed_alpha.py")
    assert "change: R100 alpha.py -> renamed_alpha.py" in renamed.payload
    assert "alpha = 1" not in renamed.payload


def test_git_snapshot_mode_reads_net_head_and_updates_incrementally(repo):
    commit(repo, "def old_retired():\n    return 0\n", "intermediate churn", "main.py")
    commit(repo, "class ActiveService:\n    def run(self, x):\n        return x + 1\n",
           "final net state", "main.py")
    commit(repo, "big = " + repr("Z" * 400) + "\n", "add oversized file", "large.py")
    snap_req = SourceRequest(
        project_id="demo",
        source=SourceRoute(source_id="code", kind="local_git", collection_mode="snapshot",
                           root_env="TRACEFOLIO_TEST_ROOT", include_paths=["**/*.py"]),
        locator=str(repo), limits=Limits(max_text_file_bytes=200),
        policy=PrivacyPolicy((str(repo),)),
        owner_emails=frozenset({"synthetic@example.invalid"}),
    )
    provider = LocalGitProvider()
    first = provider.collect_source(snap_req, None)
    assert first.coverage.state == "complete"
    by_path = {e.source_relative_path: e for e in first.evidence}
    assert set(by_path) == {"main.py", "large.py"}
    assert "old_retired" not in by_path["main.py"].payload
    assert "class ActiveService" in by_path["main.py"].payload
    assert by_path["main.py"].origin == "snapshot"
    assert by_path["main.py"].attribution == "self"
    assert by_path["large.py"].completeness == "partial"
    assert "[semantic_outline]" in by_path["large.py"].payload

    # Unchanged second run is a noop
    second = provider.collect_source(snap_req, first.cursor)
    assert second.coverage.state == "complete"
    assert second.evidence == []

    # Incremental update reads only the changed file
    commit(repo, "class ActiveService:\n    def run(self, x):\n        return x + 2\n",
           "modify main only", "main.py")
    third = provider.collect_source(snap_req, second.cursor)
    assert third.coverage.state == "complete"
    assert [e.source_relative_path for e in third.evidence] == ["main.py"]


def test_local_git_captures_and_backfills_activity_window(tmp_path):
    import os

    from tracefolio.contracts.evidence import SourceCursor

    root = tmp_path / "dated_repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Synthetic Author")
    git(root, "config", "user.email", "synthetic@example.invalid")

    def dated_commit(content, iso_date, filename="main.py"):
        (root / filename).write_text(content, encoding="utf-8")
        git(root, "add", filename)
        env = {**os.environ, "GIT_AUTHOR_DATE": iso_date, "GIT_COMMITTER_DATE": iso_date}
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "dated"],
                       env=env, capture_output=True, check=True)
        return git(root, "rev-parse", "HEAD")

    dated_commit("v = 1\n", "2025-11-05T10:00:00+07:00")
    head = dated_commit("v = 2\n", "2026-02-14T18:30:00+07:00")

    provider = LocalGitProvider()
    req = request(root)
    res = provider.collect_source(req, None)
    assert res.cursor.first_activity_at == "2025-11-05T03:00:00Z"
    assert res.cursor.last_activity_at == "2026-02-14T11:30:00Z"
    assert res.cursor.activity_source == "git_commit_dates"

    # Legacy cursor at same HEAD without timestamps backfills with zero new evidence
    legacy = SourceCursor(head=head)
    backfill = provider.collect_source(req, legacy)
    assert backfill.evidence == []
    assert backfill.cursor.first_activity_at == "2025-11-05T03:00:00Z"
    assert backfill.cursor.last_activity_at == "2026-02-14T11:30:00Z"
    assert backfill.cursor.activity_source == "git_commit_dates"


def test_local_directory_captures_mtime_and_portfolio_summarizes_activity_window(tmp_path):
    import os

    from tracefolio.collection import collect_projects
    from tracefolio.contracts.routes import ProjectRoute, RouteRegistry
    from tracefolio.portfolio import brief_portfolio, summarize_portfolio
    from tracefolio.private_config import ResolvedConfig
    from tracefolio.state import StateStore

    src = tmp_path / "notes"
    src.mkdir()
    f1 = src / "a.py"
    f2 = src / "b.py"
    f1.write_text("a = 1\n", encoding="utf-8")
    f2.write_text("b = 2\n", encoding="utf-8")
    os.utime(f1, (1762000000, 1762000000))  # 2025-11-01T12:26:40Z
    os.utime(f2, (1766000000, 1766000000))  # 2025-12-17T19:33:20Z

    state_root = tmp_path / "state"
    registry = RouteRegistry(
        state_dir_env="TRACEFOLIO_STATE_DIR",
        projects=[ProjectRoute(
            project_id="demo-dir",
            title="Demo Dir",
            context="Synthetic directory project",
            sources=[SourceRoute(
                source_id="files", kind="local_directory",
                root_env="TRACEFOLIO_TEST_ROOT", include_paths=["**/*.py"],
            )],
        )],
    )
    config = ResolvedConfig(
        registry=registry, state_root=state_root, config_paths=[],
        locators={("demo-dir", "files"): str(src)},
        policy=PrivacyPolicy((str(src),)),
    )
    report = collect_projects(config, ["demo-dir"])
    assert not report.partial

    store = StateStore.open(config)
    snap = summarize_portfolio(store, registry)
    entry = snap.entries[0]
    assert entry.activity_window is not None
    assert entry.activity_window.first_activity_at == "2025-11-01T12:26:40Z"
    assert entry.activity_window.last_activity_at == "2025-12-17T19:33:20Z"
    assert entry.activity_window.sources == ["filesystem_mtime"]

    brief = brief_portfolio(snap)
    assert brief["entries"][0]["activity_window"] == {
        "first_activity_at": "2025-11-01T12:26:40Z",
        "last_activity_at": "2025-12-17T19:33:20Z",
        "sources": ["filesystem_mtime"],
    }

