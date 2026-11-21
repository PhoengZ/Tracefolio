import base64
import difflib
import json
import subprocess
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from ..contracts.common import to_utc
from ..contracts.evidence import CollectionResult, ContextResult, SourceCursor
from ..errors import TracefolioError
from ..privacy import path_allowed
from ..private_config import REPOSITORY
from ..semantic import semantic_outline_payload
from .base import ContextRequest, SourceRequest, canonical, coverage, evidence
from .notebooks import filter_changed_cells, normalize_text


@dataclass
class ApiResponse:
    data: object
    headers: dict[str, str] = field(default_factory=dict)
    byte_count: int = 0


def _retry_after(headers: dict[str, str]) -> int | None:
    raw = headers.get("retry-after", "")
    if raw.isdigit():
        return int(raw)
    reset = headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return max(0, int(reset) - int(time.time()))
    return None


def _split_http_response(text: str) -> tuple[int | None, dict[str, str], str]:
    rest = text
    status: int | None = None
    headers: dict[str, str] = {}
    while rest.startswith("HTTP/") and "\n\n" in rest:
        head, rest = rest.split("\n\n", 1)
        lines = head.splitlines()
        parts = lines[0].split()
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
    return status, headers, rest


class GhCliClient:
    def __init__(self, timeout: int = 30, response_limit: int = 16777216):
        self.timeout = timeout
        self.response_limit = response_limit

    def get_json(self, path: str, params: dict[str, str] | None = None) -> ApiResponse:
        if (not isinstance(path, str) or not path.startswith("/repos/")
                or any(ch.isspace() for ch in path) or ".." in path.split("/")):
            raise TracefolioError("invalid_repository")
        endpoint = path + ("?" + urlencode(params) if params else "")
        try:
            result = subprocess.run(
                ["gh", "api", "-i", "-H", "Accept: application/vnd.github+json", endpoint],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise TracefolioError("github_unavailable", exit_code=3) from None
        status, headers, body = _split_http_response(result.stdout or "")
        if status == 429 or (
            status == 403
            and (
                headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in (body + (result.stderr or "")).lower()
            )
        ):
            error = TracefolioError("github_rate_limited", exit_code=3)
            error.retry_after = _retry_after(headers)
            raise error
        if status == 404:
            raise TracefolioError("github_not_found", exit_code=3)
        if status in {401, 403}:
            raise TracefolioError("github_not_public", exit_code=3)
        if result.returncode != 0 or (status is not None and status >= 400):
            raise TracefolioError("github_unavailable", exit_code=3)
        body_bytes = len(body.encode("utf-8"))
        if body_bytes > self.response_limit:
            raise TracefolioError("github_response_budget", exit_code=3)
        try:
            data = json.loads(body)
        except ValueError:
            raise TracefolioError("github_invalid_json", exit_code=3) from None
        return ApiResponse(data=data, headers=headers, byte_count=body_bytes)


class GitHubPublicProvider:
    def __init__(self, transport=None):
        self.api = transport or GhCliClient()
        self._spent = 0

    def _get(self, req, path, params=None):
        remaining = req.limits.max_total_source_bytes - self._spent
        if remaining <= 0:
            raise TracefolioError("source_byte_budget", exit_code=3)
        if hasattr(self.api, "response_limit"):
            self.api.response_limit = min(16777216, remaining)
        response = self.api.get_json(path, params or {})
        self._spent += response.byte_count or len(canonical(response.data))
        if self._spent > req.limits.max_total_source_bytes:
            raise TracefolioError("source_byte_budget", exit_code=3)
        return response

    def _repository(self, req):
        if not REPOSITORY.fullmatch(req.locator) or req.locator.split("/")[1] in {".", ".."}:
            raise TracefolioError("invalid_repository")
        prefix = "/repos/" + req.locator
        metadata = self._get(req, prefix).data
        if not isinstance(metadata, dict) or metadata.get("private") is not False:
            raise TracefolioError("github_not_public")
        if metadata.get("full_name", "").casefold() != req.locator.casefold():
            raise TracefolioError("github_repository_moved")
        return prefix, metadata

    def _text(self, req, prefix, sha, path):
        if not path_allowed(path, req.source.include_paths, req.source.exclude_paths):
            raise TracefolioError("context_out_of_scope")
        item = self._get(req, prefix + "/contents/" + quote(path, safe="/"),
                         {"ref": sha}).data
        limit = req.limits.max_notebook_bytes if path.endswith(".ipynb") else req.limits.max_text_file_bytes
        if not isinstance(item, dict) or item.get("type") != "file" or item.get("size", limit + 1) > limit:
            raise TracefolioError("github_content_unavailable", exit_code=3)
        if item.get("encoding") != "base64":
            item = self._get(req, prefix + "/git/blobs/" + item["sha"]).data
        try:
            raw = base64.b64decode("".join(item["content"].split()), validate=True)
        except (ValueError, KeyError, TypeError):
            raise TracefolioError("github_content_unavailable", exit_code=3) from None
        return normalize_text(raw, path, req.limits)

    def read_context(self, request: ContextRequest) -> ContextResult:
        req = request.source_request
        self._spent = 0
        if not path_allowed(request.path, req.source.include_paths, req.source.exclude_paths):
            raise TracefolioError("context_out_of_scope")
        prefix, _ = self._repository(req)
        text, metadata = self._text(req, prefix, request.revision, request.path)
        return ContextResult(
            evidence=[evidence(req, request.revision, request.path, text, origin="context",
                               **metadata)], coverage=coverage(req, request.revision, True))

    def _snapshot_blob(self, req: SourceRequest, prefix: str, blob_sha: str,
                       path: str, size: int):
        limit = (req.limits.max_notebook_bytes if path.endswith(".ipynb")
                 else req.limits.max_text_file_bytes)
        if path.endswith(".ipynb") and size > limit:
            raise TracefolioError("github_content_unavailable", exit_code=3)
        item = self._get(req, prefix + "/git/blobs/" + blob_sha).data
        if not isinstance(item, dict) or item.get("encoding") != "base64":
            raise TracefolioError("github_content_unavailable", exit_code=3)
        try:
            raw = base64.b64decode("".join(item["content"].split()), validate=True)
        except (ValueError, KeyError, TypeError):
            raise TracefolioError("github_content_unavailable", exit_code=3) from None
        if len(raw) <= limit:
            text, metadata = normalize_text(raw, path, req.limits)
            return text, metadata, "complete"
        if path.endswith(".ipynb"):
            raise TracefolioError("github_content_unavailable", exit_code=3)
        if b"\x00" in raw:
            raise TracefolioError("binary_not_supported", exit_code=3)
        try:
            full_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise TracefolioError("invalid_utf8", exit_code=3) from None
        return semantic_outline_payload(path, full_text, limit), {}, "partial"

    def _snapshot_attribution(self, req: SourceRequest, prefix: str,
                              target: str, commit_data: dict) -> str:
        if not req.owner_emails:
            return "unverified"
        emails = set()
        head_email = commit_data.get("commit", {}).get("author", {}).get("email", "")
        if head_email and len(commit_data.get("parents", [])) <= 1:
            emails.add(head_email.casefold())
        try:
            commits = self._get(req, prefix + "/commits",
                                {"sha": target, "per_page": "100", "page": "1"}).data
            if isinstance(commits, list):
                for item in commits:
                    if len(item.get("parents", [])) <= 1:
                        email = item.get("commit", {}).get("author", {}).get("email", "")
                        if email:
                            emails.add(email.casefold())
        except TracefolioError:
            return "unverified"
        if not emails:
            return "unverified"
        if emails <= req.owner_emails:
            return "self"
        if not (emails & req.owner_emails):
            return "other"
        return "unverified"

    def _activity_bounds(self, repo: dict, commit_data: dict | None,
                         previous: SourceCursor,
                         observed_dates: list[str] | None = None
                         ) -> tuple[str | None, str | None, str | None]:
        candidates: list[str] = [
            d for d in (previous.first_activity_at, previous.last_activity_at) if d
        ]
        repo_created = to_utc(repo.get("created_at"))
        if repo_created:
            candidates.append(repo_created)
        commit_obj = (commit_data or {}).get("commit") if isinstance(commit_data, dict) else None
        if isinstance(commit_obj, dict):
            head_date = (to_utc((commit_obj.get("author") or {}).get("date"))
                         or to_utc((commit_obj.get("committer") or {}).get("date")))
            if head_date:
                candidates.append(head_date)
        valid_observed = [d for d in (to_utc(raw) for raw in (observed_dates or [])) if d]
        candidates.extend(valid_observed)
        if not candidates:
            return previous.first_activity_at, previous.last_activity_at, previous.activity_source
        src = ("git_commit_dates" if valid_observed and not repo_created
               else "github_repo_and_head_dates")
        return min(candidates), max(candidates), src

    def _collect_snapshot(self, req: SourceRequest,
                          previous: SourceCursor | None) -> CollectionResult:
        previous = previous or SourceCursor()
        if not previous.inventory and not previous.baseline:
            previous = SourceCursor(fingerprint=previous.fingerprint)
        prefix, repo = self._repository(req)
        ref = req.source.ref
        if ref in {None, "default"}:
            ref = repo["default_branch"]
        commit_data = self._get(req, prefix + "/commits/" + quote(ref, safe="")).data
        current = commit_data["sha"]
        if previous.head == current and not previous.pending and not previous.target:
            cursor = previous
            if previous.first_activity_at is None or previous.last_activity_at is None:
                first_at, last_at, src = self._activity_bounds(repo, commit_data, previous)
                if first_at or last_at:
                    cursor = previous.model_copy(update={
                        "first_activity_at": first_at,
                        "last_activity_at": last_at,
                        "activity_source": src,
                    })
            return CollectionResult(cursor=cursor, coverage=coverage(req, current, True))
        target = previous.target if (previous.target and previous.pending) else current
        limitations, records, invalidated = [], [], []
        tree_data = self._get(req, prefix + "/git/trees/" + target, {"recursive": "1"}).data
        if not isinstance(tree_data, dict) or not isinstance(tree_data.get("tree"), list):
            raise TracefolioError("github_invalid_json", exit_code=3)
        if tree_data.get("truncated"):
            limitations.append("github_tree_truncated")
        tree_blobs: dict[str, tuple[str, int]] = {}
        for node in tree_data["tree"]:
            if not isinstance(node, dict):
                continue
            path = node.get("path", "")
            if not path or not path_allowed(path, req.source.include_paths, req.source.exclude_paths):
                continue
            if node.get("mode") == "120000":
                limitations.append("symlink_not_followed")
                continue
            if node.get("type") == "blob" and isinstance(node.get("sha"), str):
                tree_blobs[path] = (node["sha"], int(node.get("size") or 0))

        baseline = dict(previous.baseline if (previous.target == target and previous.pending)
                        else previous.inventory)
        if previous.target == target and previous.pending:
            names = [p for p in previous.pending if p in tree_blobs]
            inventory = dict(previous.inventory)
        else:
            names = [p for p, (sha, _) in sorted(tree_blobs.items()) if baseline.get(p) != sha]
            inventory = {p: sha for p, (sha, _) in tree_blobs.items() if baseline.get(p) == sha}

        attribution = (self._snapshot_attribution(req, prefix, target, commit_data)
                       if names else "unverified")
        selected = names[:req.limits.max_files_per_source]
        retry = []
        for idx, path in enumerate(selected):
            blob_sha, size = tree_blobs[path]
            try:
                text, metadata, comp = self._snapshot_blob(req, prefix, blob_sha, path, size)
                records.append(evidence(req, target, path, text, origin="snapshot",
                                        attribution=attribution, completeness=comp, **metadata))
                inventory[path] = blob_sha
                invalidated.append(path)
            except TracefolioError as error:
                limitations.append(error.code)
                if getattr(error, "retry_after", None) is not None:
                    limitations.append(f"retry_after_seconds:{error.retry_after}")
                retry.append(path)
                if error.code in {"github_rate_limited", "github_unavailable",
                                  "source_byte_budget", "github_response_budget"}:
                    retry.extend(selected[idx + 1:])
                    break

        pending = retry + names[len(selected):]
        if not pending:
            for deleted_path in sorted(baseline.keys() - tree_blobs.keys()):
                records.append(evidence(req, "deleted-" + baseline[deleted_path], deleted_path,
                                        "File deleted from source snapshot.", origin="snapshot"))
                invalidated.append(deleted_path)
        else:
            limitations.append("pending_files")
        if current != target:
            limitations.append("head_changed")
        complete = not pending and not limitations and current == target
        first_at, last_at, activity_src = self._activity_bounds(repo, commit_data, previous)
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
            cursor=cursor, evidence=records, coverage=coverage(req, target, complete, limitations),
            invalidated_paths=sorted(set(invalidated)), changed=bool(records),
        )

    def collect_source(self, req: SourceRequest, previous: SourceCursor | None) -> CollectionResult:
        self._spent = 0
        if req.source.collection_mode == "snapshot":
            return self._collect_snapshot(req, previous)
        previous = previous or SourceCursor()
        prefix, repo = self._repository(req)
        ref = req.source.ref
        if ref in {None, "default"}:
            ref = repo["default_branch"]
        head_commit = self._get(req, prefix + "/commits/" + quote(ref, safe="")).data
        current = head_commit["sha"]
        if previous.head == current and not previous.target:
            cursor = previous
            if previous.first_activity_at is None or previous.last_activity_at is None:
                first_at, last_at, src = self._activity_bounds(repo, head_commit, previous)
                if first_at or last_at:
                    cursor = previous.model_copy(update={
                        "first_activity_at": first_at,
                        "last_activity_at": last_at,
                        "activity_source": src,
                    })
            return CollectionResult(cursor=cursor, coverage=coverage(req, current, True))
        warnings = []
        active = previous.target is not None
        target = previous.target if active else current
        remaining = list(previous.pending) if active else []
        page = previous.page if active else 1
        listed = previous.listed_complete if active else False
        base = previous.base_head if active else previous.head
        limitations, records, invalidated = [], [], []
        observed_dates: list[str] = []
        if not listed:
            try:
                params = {"per_page": "100", "page": str(page)}
                if base:
                    try:
                        response = self._get(req, prefix + f"/compare/{base}...{target}", params)
                        status = response.data.get("status")
                    except TracefolioError as error:
                        if error.code != "github_not_found":
                            raise
                        status = "inaccessible_base"
                    if status not in {"ahead", "identical"}:
                        base, remaining, page = None, [], 1
                        warnings.append("history_reconciled")
                        response = self._get(req, prefix + "/commits",
                                             {"sha": target, "per_page": "100", "page": "1"})
                        values = response.data
                    else:
                        values = response.data["commits"]
                else:
                    response = self._get(req, prefix + "/commits",
                                         {**params, "sha": target})
                    values = response.data
                remaining.extend(item["sha"] for item in values)
                if 'rel="next"' in response.headers.get("link", ""):
                    page += 1
                    limitations.append("history_pagination")
                else:
                    listed = True
                    remaining = list(dict.fromkeys(remaining))
                    if base is None:
                        remaining.reverse()
            except TracefolioError as error:
                limitations.append(error.code)
        if listed:
            queue = remaining[:req.limits.max_commits_per_run]
            retry = []
            for queue_index, sha in enumerate(queue):
                try:
                    detail = self._get(req, prefix + "/commits/" + sha,
                                       {"per_page": "100", "page": "1"})
                    data = detail.data
                    files = list(data.get("files", []))
                    file_page = 1
                    while 'rel="next"' in detail.headers.get("link", "") and len(files) < 3000:
                        file_page += 1
                        detail = self._get(req, prefix + "/commits/" + sha,
                                           {"per_page": "100", "page": str(file_page)})
                        files.extend(detail.data.get("files", []))
                    if len(files) >= 3000:
                        raise TracefolioError("github_file_cap", exit_code=3)
                    message = data["commit"]["message"]
                    author_info = data["commit"].get("author", {}) or {}
                    email = author_info.get("email", "")
                    if author_info.get("date"):
                        observed_dates.append(author_info["date"])
                    parents = data.get("parents", [])
                    attribution = ("self" if email.casefold() in req.owner_emails else
                                   "other" if req.owner_emails else "unverified")
                    if len(parents) > 1:
                        attribution = "unverified"
                    commit_bytes = 0
                    ok = True
                    stop = False
                    first_in_commit = True
                    full_msg = message.strip()
                    subject = full_msg.splitlines()[0] if full_msg else ""
                    for item in files:
                        path = item["filename"]
                        if not path_allowed(path, req.source.include_paths,
                                            req.source.exclude_paths):
                            continue
                        invalidated.append(path)
                        try:
                            metadata = {}
                            previous_path = item.get("previous_filename", path)
                            if previous_path != path:
                                invalidated.append(previous_path)
                            if not path_allowed(previous_path, req.source.include_paths,
                                                req.source.exclude_paths):
                                raise TracefolioError("rename_source_out_of_scope", exit_code=3)
                            if path.endswith(".ipynb"):
                                before, before_meta = "", {}
                                if parents and item["status"] != "added":
                                    before, before_meta = self._text(req, prefix, parents[0]["sha"],
                                                                     previous_path)
                                after = ""
                                if item["status"] != "removed":
                                    after, metadata = self._text(req, prefix, sha, path)
                                metadata = filter_changed_cells(before_meta, metadata)
                                patch = "".join(difflib.unified_diff(
                                    before.splitlines(True), after.splitlines(True),
                                    fromfile="before", tofile="after", n=1))
                            else:
                                patch = item.get("patch")
                                if patch is None:
                                    if item.get("status") == "renamed" and item.get("changes", 0) == 0:
                                        patch = ""
                                    else:
                                        raise TracefolioError("github_missing_patch", exit_code=3)
                            commit_bytes += len(patch.encode())
                            if commit_bytes > req.limits.max_diff_bytes_per_commit:
                                raise TracefolioError("diff_budget", exit_code=3)
                            label = "merge_context" if len(parents) > 1 else "commit"
                            msg_text = full_msg if first_in_commit else subject
                            first_in_commit = False
                            status_desc = (f"{item['status']} {previous_path} -> {path}"
                                           if previous_path != path else item["status"])
                            records.append(evidence(
                                req, sha, path, f"{label}: {msg_text}\nchange: {status_desc}\n{patch}",
                                origin="git_commit", attribution=attribution, **metadata))
                        except TracefolioError as error:
                            limitations.append(error.code)
                            if getattr(error, "retry_after", None) is not None:
                                limitations.append(f"retry_after_seconds:{error.retry_after}")
                            ok = False
                            if error.code in {"github_rate_limited", "github_unavailable",
                                              "source_byte_budget", "github_response_budget"}:
                                stop = True
                                break
                    if not ok:
                        retry.append(sha)
                    if stop:
                        retry.extend(queue[queue_index + 1:])
                        break
                except TracefolioError as error:
                    limitations.append(error.code)
                    retry.append(sha)
                    if getattr(error, "retry_after", None) is not None:
                        limitations.append(f"retry_after_seconds:{error.retry_after}")
                    retry.extend(queue[queue_index + 1:])
                    break
            remaining = retry + remaining[len(queue):]
        if remaining or not listed:
            limitations.append("pending_commits")
        range_complete = listed and not remaining and not limitations
        if current != target:
            limitations.append("head_changed")
        complete = range_complete and current == target
        bounds_prev = SourceCursor() if "history_reconciled" in warnings else previous
        first_at, last_at, activity_src = self._activity_bounds(
            repo, head_commit, bounds_prev, observed_dates
        )
        cursor = SourceCursor(
            head=target if range_complete else previous.head,
            target=None if range_complete else target,
            pending=remaining, page=page, listed_complete=listed, base_head=base,
            fingerprint=previous.fingerprint,
            first_activity_at=first_at, last_activity_at=last_at, activity_source=activity_src,
        )
        return CollectionResult(
            cursor=cursor, evidence=records, coverage=coverage(req, target, complete, limitations),
            invalidated_paths=sorted(set(invalidated)), warnings=warnings, changed=bool(records))

