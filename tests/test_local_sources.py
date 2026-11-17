import json
import subprocess
from pathlib import Path

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
    subprocess.run(["git", "-C", str(repo), "merge", "feature"], capture_output=True)
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



