import hashlib
import os
from pathlib import Path

from ..contracts.common import utc_now
from ..contracts.evidence import CollectionResult, ContextResult, SourceCursor
from ..errors import TracefolioError
from ..privacy import mandatory_allowed, path_allowed, resolve_allowed_path, sanitize_text
from .base import ContextRequest, SourceRequest, coverage, digest, evidence
from .notebooks import normalize_text


class LocalDirectoryProvider:
    def _read(self, req, path, remaining=None):
        target = resolve_allowed_path(Path(req.locator), path)
        limit = req.limits.max_notebook_bytes if path.endswith(".ipynb") else req.limits.max_text_file_bytes
        try:
            before = target.stat()
            if remaining is not None and before.st_size > remaining:
                raise TracefolioError("source_byte_budget", exit_code=3)
            if before.st_size > limit:
                raise TracefolioError("content_budget", exit_code=3)
            with target.open("rb") as handle:
                raw = handle.read(limit + 1)
            after = target.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise TracefolioError("unstable_observation", exit_code=3)
            text, metadata = normalize_text(raw, path, req.limits)
            return text, metadata, len(raw)
        except OSError:
            raise TracefolioError("source_read_failed", exit_code=3) from None

    def read_context(self, request: ContextRequest) -> ContextResult:
        req = request.source_request
        if not path_allowed(request.path, req.source.include_paths, req.source.exclude_paths):
            raise TracefolioError("context_out_of_scope")
        text, metadata, _ = self._read(req, request.path)
        revision = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        if revision != request.revision:
            raise TracefolioError("context_revision_changed")
        return ContextResult(evidence=[evidence(req, revision, request.path, text,
                                               origin="context", **metadata)],
                             coverage=coverage(req, revision, True))

    def collect_source(self, req: SourceRequest, previous: SourceCursor | None) -> CollectionResult:
        previous = previous or SourceCursor()
        if previous.pending:
            names = list(previous.pending)
            inventory = dict(previous.inventory)
            baseline = dict(previous.baseline)
        else:
            names = []
            for current, directories, files in os.walk(req.locator, followlinks=False):
                relative = Path(current).relative_to(req.locator)
                directories[:] = sorted(d for d in directories
                    if mandatory_allowed((relative / d / "scope").as_posix())
                    and not (Path(current) / d).is_symlink()
                    and not (hasattr(Path(current) / d, "is_junction")
                             and (Path(current) / d).is_junction()))
                for name in sorted(files):
                    path = (relative / name).as_posix()
                    if (path_allowed(path, req.source.include_paths, req.source.exclude_paths)
                            and sanitize_text(path, req.policy).text == path):
                        names.append(path)
            names.sort()
            inventory = {}
            baseline = dict(previous.inventory)
        selected = names[:req.limits.max_files_per_source]
        records, retry, limitations, invalidated = [], [], [], []
        total = 0
        for path in selected:
            try:
                text, metadata, size = self._read(
                    req, path, remaining=req.limits.max_total_source_bytes - total)
                total += size
                if total > req.limits.max_total_source_bytes:
                    raise TracefolioError("source_byte_budget", exit_code=3)
                content_hash = hashlib.sha256(text.encode()).hexdigest()
                inventory[path] = content_hash
                if baseline.get(path) != content_hash:
                    records.append(evidence(req, "sha256:" + content_hash, path, text, **metadata))
                    invalidated.append(path)
            except TracefolioError as error:
                retry.append(path)
                limitations.append(error.code)
        pending = retry + names[len(selected):]
        if not pending:
            # Verify earlier reads too: a later file may have changed them.
            for path, expected in list(inventory.items()):
                try:
                    verified, _, size = self._read(
                        req, path, remaining=req.limits.max_total_source_bytes - total)
                    total += size
                    if total > req.limits.max_total_source_bytes:
                        raise TracefolioError("source_byte_budget", exit_code=3)
                    if hashlib.sha256(verified.encode()).hexdigest() != expected:
                        raise TracefolioError("unstable_observation", exit_code=3)
                except TracefolioError as error:
                    pending.append(path)
                    limitations.append(error.code)
        if not pending:
            for removed in sorted(baseline.keys() - inventory.keys()):
                records.append(evidence(req, "deleted-" + baseline[removed], removed,
                                        "File deleted from source snapshot."))
                invalidated.append(removed)
        else:
            limitations.append("pending_files")
        revision = "sha256:" + digest(inventory)
        cursor = SourceCursor(
            fingerprint=previous.fingerprint,
            head=previous.head if pending else revision,
            target=revision if pending else None, pending=pending,
            inventory=inventory, baseline=baseline if pending else {},
            observed_at=utc_now(),
        )
        return CollectionResult(evidence=records, cursor=cursor,
                                coverage=coverage(req, revision, not limitations, limitations),
                                invalidated_paths=invalidated, changed=bool(records))

