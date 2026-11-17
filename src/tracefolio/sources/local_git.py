import difflib
import os
import subprocess

from ..contracts.evidence import CollectionResult, ContextResult, SourceCursor
from ..errors import TracefolioError
from ..privacy import path_allowed, safe_relative
from .base import ContextRequest, SourceRequest, coverage, evidence
from .notebooks import normalize_text


def git(request: SourceRequest, *args: str, optional=False) -> bytes | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "log.showSignature=false",
             "-c", "diff.external=", "-C", request.locator, *args],
            env=env, capture_output=True, timeout=30, check=False,
        )
        if result.returncode:
            if optional:
                return None
            raise TracefolioError("git_read_failed", exit_code=4)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise TracefolioError("git_read_failed", exit_code=4) from None


class LocalGitProvider:
    def _blob(self, req, revision, path, max_bytes=None):
        if revision is None:
            return "", {}, 0
        safe_relative(path)
        spec = f"{revision}:{path}"
        size_raw = git(req, "cat-file", "-s", spec, optional=True)
        if size_raw is None:
            return "", {}, 0
        size = int(size_raw.strip())
        if max_bytes is not None and size > max_bytes:
            raise TracefolioError("source_byte_budget", exit_code=3)
        limit = req.limits.max_notebook_bytes if path.endswith(".ipynb") else req.limits.max_text_file_bytes
        if size > limit:
            raise TracefolioError("content_budget", exit_code=3)
        mode = git(req, "ls-tree", revision, "--", path) or b""
        if mode.startswith(b"120000"):
            raise TracefolioError("symlink_not_followed", exit_code=3)
        raw = git(req, "cat-file", "blob", spec)
        text, metadata = normalize_text(raw, path, req.limits)
        return text, metadata, size

    def text_at(self, req, revision, path):
        if not path_allowed(path, req.source.include_paths, req.source.exclude_paths):
            raise TracefolioError("context_out_of_scope")
        return self._blob(req, revision, path)[0]

    def read_context(self, request: ContextRequest) -> ContextResult:
        req = request.source_request
        if not path_allowed(request.path, req.source.include_paths, req.source.exclude_paths):
            raise TracefolioError("context_out_of_scope")
        text, metadata, _ = self._blob(req, request.revision, request.path)
        return ContextResult(
            evidence=[evidence(req, request.revision, request.path, text, origin="context",
                               **metadata)],
            coverage=coverage(req, request.revision, True),
        )

    def collect_source(self, req: SourceRequest, previous: SourceCursor | None) -> CollectionResult:
        previous = previous or SourceCursor()
        current = git(req, "rev-parse", "--verify", "--end-of-options",
                      f"{req.source.ref or 'HEAD'}^{{commit}}").decode().strip()
        warnings = []
        if previous.pending:
            target, remaining = previous.target, list(previous.pending)
        else:
            target = current
            base = previous.head
            if base == target:
                return CollectionResult(cursor=previous, coverage=coverage(req, target, True))
            if base and git(req, "merge-base", "--is-ancestor", base, target,
                            optional=True) is None:
                base = None
                warnings.append("history_reconciled")
            revision_range = f"{base}..{target}" if base else target
            remaining = git(req, "rev-list", "--topo-order", "--reverse",
                            revision_range).decode().splitlines()
        records, pending, paths, limitations = [], [], [], []
        total = 0
        selected = remaining[:req.limits.max_commits_per_run]
        for sha in selected:
            commit_size = int(git(req, "cat-file", "-s", sha))
            if commit_size > req.limits.max_text_file_bytes:
                pending.append(sha)
                limitations.append("commit_message_budget")
                continue
            meta = git(req, "show", "-s", "--format=%ae%n%P%n%B", sha).decode("utf-8", "replace")
            email, parents_line, message = meta.split("\n", 2)
            parents = parents_line.split()
            parent = parents[0] if parents else None
            attribution = ("self" if email.casefold() in req.owner_emails else
                           "other" if req.owner_emails else "unverified")
            if len(parents) > 1:
                attribution = "unverified"
            delta_args = [parent, sha] if parent else ["--root", sha]
            raw_paths = git(req, "diff-tree", "--no-commit-id", "--name-status", "-r", "-z",
                            "--no-renames", *delta_args).decode("utf-8", "replace").split("\0")
            commit_ok, commit_bytes = True, 0
            for index in range(0, len(raw_paths) - 1, 2):
                status, path = raw_paths[index:index + 2]
                if not path_allowed(path, req.source.include_paths, req.source.exclude_paths):
                    continue
                paths.append(path)
                try:
                    before, _, old_size = self._blob(
                        req, parent, path, max_bytes=req.limits.max_total_source_bytes - total)
                    total += old_size
                    after, metadata, new_size = self._blob(
                        req, sha, path, max_bytes=req.limits.max_total_source_bytes - total)
                    total += new_size
                    diff = "".join(difflib.unified_diff(
                        before.splitlines(True), after.splitlines(True),
                        fromfile="before", tofile="after"))
                    commit_bytes += len(diff.encode())
                    if (commit_bytes > req.limits.max_diff_bytes_per_commit
                            or total > req.limits.max_total_source_bytes):
                        raise TracefolioError("diff_budget", exit_code=3)
                    label = "merge_context" if len(parents) > 1 else "commit"
                    payload = f"{label}: {message.strip()}\nchange: {status}\n{diff}"
                    records.append(evidence(req, sha, path, payload, origin="git_commit",
                                            attribution=attribution, **metadata))
                except TracefolioError as error:
                    commit_ok = False
                    limitations.append(error.code)
            if not commit_ok:
                pending.append(sha)
        pending.extend(remaining[len(selected):])
        if pending:
            limitations.append("pending_commits")
        if target != current:
            limitations.append("head_changed")
        cursor = SourceCursor(
            fingerprint=previous.fingerprint, head=previous.head if pending else target,
            target=target if pending else None, pending=pending,
        )
        return CollectionResult(evidence=records, cursor=cursor,
                                coverage=coverage(req, target, not limitations, limitations),
                                invalidated_paths=sorted(set(paths)), changed=bool(records),
                                warnings=warnings)

