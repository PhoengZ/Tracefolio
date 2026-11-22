import difflib
import os
import subprocess
from pathlib import Path

from ..authorship import classify_commit_attribution, index_local_git_paths
from ..contracts.common import to_utc
from ..contracts.evidence import CollectionResult, ContextResult, SourceCursor
from ..errors import TracefolioError
from ..privacy import path_allowed, safe_relative
from ..semantic import semantic_outline_payload
from .base import ContextRequest, SourceRequest, coverage, evidence
from .notebooks import filter_changed_cells, normalize_text


def git(request: SourceRequest, *args: str, optional=False) -> bytes | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "log.showSignature=false",
             "-c", "diff.external=", "-c", "core.quotepath=false", "-C", request.locator, *args],
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
    def _require_root(self, req):
        top = git(req, "rev-parse", "--show-toplevel").decode("utf-8", "replace").strip()
        if Path(top).resolve() != Path(req.locator).resolve():
            raise TracefolioError("git_root_required",
                                  "Bind the repository root and narrow scope using include paths.")

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
        self._require_root(req)
        return self._blob(req, revision, path)[0]

    def read_context(self, request: ContextRequest) -> ContextResult:
        req = request.source_request
        if not path_allowed(request.path, req.source.include_paths, req.source.exclude_paths):
            raise TracefolioError("context_out_of_scope")
        self._require_root(req)
        text, metadata, _ = self._blob(req, request.revision, request.path)
        return ContextResult(
            evidence=[evidence(req, request.revision, request.path, text, origin="context",
                               **metadata)],
            coverage=coverage(req, request.revision, True),
        )

    def _snapshot_blob(self, req: SourceRequest, revision: str, path: str, max_bytes: int):
        safe_relative(path)
        spec = f"{revision}:{path}"
        size_raw = git(req, "cat-file", "-s", spec, optional=True)
        if size_raw is None:
            raise TracefolioError("source_missing", exit_code=3)
        size = int(size_raw.strip())
        if size > max_bytes:
            raise TracefolioError("source_byte_budget", exit_code=3)
        limit = (req.limits.max_notebook_bytes if path.endswith(".ipynb")
                 else req.limits.max_text_file_bytes)
        raw = git(req, "cat-file", "blob", spec)
        if size <= limit:
            text, metadata = normalize_text(raw, path, req.limits)
            return text, metadata, size, "complete"
        if path.endswith(".ipynb"):
            text, metadata = normalize_text(raw, path, req.limits)
        else:
            if b"\x00" in raw:
                raise TracefolioError("binary_not_supported", exit_code=3)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise TracefolioError("invalid_utf8", exit_code=3) from None
            metadata = {}
        outlined = semantic_outline_payload(path, text, limit)
        return outlined, metadata, size, "partial"

    def _activity_bounds(self, req: SourceRequest, target: str,
                         previous: SourceCursor) -> tuple[str | None, str | None, str | None]:
        candidates: list[str] = [
            d for d in (previous.first_activity_at, previous.last_activity_at) if d
        ]
        head_raw = git(req, "show", "-s", "--format=%aI", target, optional=True)
        if head_raw:
            head_dt = to_utc(head_raw.decode("utf-8", "replace").strip())
            if head_dt:
                candidates.append(head_dt)
        roots_raw = git(req, "rev-list", "--max-parents=0", "--format=%aI", target, optional=True)
        if roots_raw:
            for line in roots_raw.decode("utf-8", "replace").splitlines():
                if line.startswith("commit "):
                    continue
                parsed = to_utc(line)
                if parsed:
                    candidates.append(parsed)
        if not candidates:
            return previous.first_activity_at, previous.last_activity_at, previous.activity_source
        return min(candidates), max(candidates), "git_commit_dates"

    def _collect_snapshot(self, req: SourceRequest,
                          previous: SourceCursor | None) -> CollectionResult:
        self._require_root(req)
        previous = previous or SourceCursor()
        if not previous.inventory and not previous.baseline:
            previous = SourceCursor(fingerprint=previous.fingerprint)
        current = git(req, "rev-parse", "--verify", "--end-of-options",
                      f"{req.source.ref or 'HEAD'}^{{commit}}").decode().strip()
        if previous.head == current and not previous.pending and not previous.target:
            cursor = previous
            if previous.first_activity_at is None or previous.last_activity_at is None:
                first_at, last_at, src = self._activity_bounds(req, current, previous)
                if first_at or last_at:
                    cursor = previous.model_copy(update={
                        "first_activity_at": first_at,
                        "last_activity_at": last_at,
                        "activity_source": src,
                    })
            return CollectionResult(cursor=cursor, coverage=coverage(req, current, True))
        target = previous.target if (previous.target and previous.pending) else current
        raw_tree = git(req, "ls-tree", "-r", "-z", target).decode("utf-8", "replace").split("\0")
        tree_blobs: dict[str, str] = {}
        limitations: list[str] = []
        for entry in raw_tree:
            if not entry or "\t" not in entry:
                continue
            header, path = entry.split("\t", 1)
            if not path_allowed(path, req.source.include_paths, req.source.exclude_paths):
                continue
            parts = header.split()
            if len(parts) < 3:
                continue
            mode, _, blob_sha = parts[0], parts[1], parts[2]
            if mode == "120000":
                limitations.append("symlink_not_followed")
                continue
            tree_blobs[path] = blob_sha

        baseline = dict(previous.baseline if (previous.target == target and previous.pending)
                        else previous.inventory)
        if previous.target == target and previous.pending:
            names = [p for p in previous.pending if p in tree_blobs]
            inventory = dict(previous.inventory)
        else:
            names = [p for p, sha in sorted(tree_blobs.items()) if baseline.get(p) != sha]
            inventory = {p: sha for p, sha in tree_blobs.items() if baseline.get(p) == sha}

        selected = names[:req.limits.max_files_per_source]
        path_index = (
            index_local_git_paths(req, target, git, tree_paths=sorted(tree_blobs))
            if selected and req.owner_emails
            else None
        )
        records, retry, invalidated = [], [], []
        total = 0
        for path in selected:
            try:
                text, metadata, size, comp = self._snapshot_blob(
                    req, target, path, req.limits.max_total_source_bytes - total)
                total += size
                attr = path_index.attribution_for_path(path) if path_index else "unverified"
                records.append(evidence(req, target, path, text, origin="snapshot",
                                        attribution=attr, completeness=comp, **metadata))
                inventory[path] = tree_blobs[path]
                invalidated.append(path)
            except TracefolioError as error:
                retry.append(path)
                limitations.append(error.code)
                if error.code == "source_byte_budget":
                    retry.extend(selected[selected.index(path) + 1:])
                    break

        pending = retry + names[len(selected):]
        if not pending:
            for deleted_path in sorted(baseline.keys() - tree_blobs.keys()):
                records.append(evidence(req, "deleted-" + baseline[deleted_path], deleted_path,
                                        "File deleted from source snapshot.", origin="snapshot"))
                invalidated.append(deleted_path)
        else:
            limitations.append("pending_files")
        if target != current:
            limitations.append("head_changed")
        complete = not pending and not limitations
        first_at, last_at, activity_src = self._activity_bounds(req, target, previous)
        cursor = SourceCursor(
            fingerprint=previous.fingerprint,
            head=previous.head if pending else target,
            target=target if pending else None,
            pending=pending,
            inventory=inventory,
            baseline=baseline if pending else {},
            first_activity_at=first_at,
            last_activity_at=last_at,
            activity_source=activity_src,
        )
        return CollectionResult(
            evidence=records, cursor=cursor,
            coverage=coverage(req, target, complete, limitations),
            invalidated_paths=sorted(set(invalidated)),
            changed=bool(records),
        )

    def collect_source(self, req: SourceRequest, previous: SourceCursor | None) -> CollectionResult:
        if req.source.collection_mode == "snapshot":
            return self._collect_snapshot(req, previous)
        self._require_root(req)
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
                cursor = previous
                if previous.first_activity_at is None or previous.last_activity_at is None:
                    first_at, last_at, src = self._activity_bounds(req, target, previous)
                    if first_at or last_at:
                        cursor = previous.model_copy(update={
                            "first_activity_at": first_at,
                            "last_activity_at": last_at,
                            "activity_source": src,
                        })
                return CollectionResult(cursor=cursor, coverage=coverage(req, target, True))
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
            attribution, _, _ = classify_commit_attribution(
                email,
                parent_count=len(parents),
                message=message,
                owner_emails=req.owner_emails,
            )
            delta_args = [parent, sha] if parent else ["--root", sha]
            raw_paths = git(req, "diff-tree", "--no-commit-id", "--name-status", "-r", "-z",
                            "-M", *delta_args).decode("utf-8", "replace").split("\0")
            commit_ok, commit_bytes = True, 0
            first_in_commit = True
            full_msg = message.strip()
            subject = full_msg.splitlines()[0] if full_msg else ""
            index = 0
            while index < len(raw_paths) - 1:
                status = raw_paths[index]
                if not status:
                    index += 1
                    continue
                if status.startswith(("R", "C")):
                    old_path, path = raw_paths[index + 1], raw_paths[index + 2]
                    index += 3
                else:
                    old_path = path = raw_paths[index + 1]
                    index += 2
                if not path_allowed(path, req.source.include_paths, req.source.exclude_paths):
                    continue
                paths.append(path)
                if old_path != path:
                    paths.append(old_path)
                try:
                    if old_path != path and not path_allowed(
                            old_path, req.source.include_paths, req.source.exclude_paths):
                        raise TracefolioError("rename_source_out_of_scope", exit_code=3)
                    if status == "R100" and not path.endswith(".ipynb"):
                        diff = ""
                        metadata = {}
                    else:
                        before, before_meta, old_size = self._blob(
                            req, parent, old_path, max_bytes=req.limits.max_total_source_bytes - total)
                        total += old_size
                        after, after_meta, new_size = self._blob(
                            req, sha, path, max_bytes=req.limits.max_total_source_bytes - total)
                        total += new_size
                        metadata = filter_changed_cells(before_meta, after_meta)
                        diff = "".join(difflib.unified_diff(
                            before.splitlines(True), after.splitlines(True),
                            fromfile="before", tofile="after", n=1))
                    commit_bytes += len(diff.encode())
                    if (commit_bytes > req.limits.max_diff_bytes_per_commit
                            or total > req.limits.max_total_source_bytes):
                        raise TracefolioError("diff_budget", exit_code=3)
                    label = "merge_context" if len(parents) > 1 else "commit"
                    msg_text = full_msg if first_in_commit else subject
                    first_in_commit = False
                    change_desc = f"{status} {old_path} -> {path}" if old_path != path else status
                    payload = f"{label}: {msg_text}\nchange: {change_desc}\n{diff}"
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
        bounds_prev = SourceCursor() if "history_reconciled" in warnings else previous
        first_at, last_at, activity_src = self._activity_bounds(req, target, bounds_prev)
        cursor = SourceCursor(
            fingerprint=previous.fingerprint, head=previous.head if pending else target,
            target=target if pending else None, pending=pending,
            first_activity_at=first_at, last_activity_at=last_at, activity_source=activity_src,
        )
        return CollectionResult(evidence=records, cursor=cursor,
                                coverage=coverage(req, target, not limitations, limitations),
                                invalidated_paths=sorted(set(paths)), changed=bool(records),
                                warnings=warnings)

