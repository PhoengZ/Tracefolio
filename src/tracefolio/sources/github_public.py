import base64
import difflib
from urllib.parse import quote

from ..contracts.evidence import CollectionResult, ContextResult, SourceCursor
from ..errors import TracefolioError
from ..privacy import path_allowed
from ..private_config import REPOSITORY
from .base import ContextRequest, SourceRequest, canonical, coverage, evidence
from .github_transport import GitHubTransport
from .notebooks import filter_changed_cells, normalize_text


class GitHubPublicProvider:
    def __init__(self, transport=None):
        self.api = transport or GitHubTransport()
        self._spent = 0

    def _get(self, req, path, params=None):
        remaining = req.limits.max_total_source_bytes - self._spent
        if remaining <= 0:
            raise TracefolioError("source_byte_budget", exit_code=3)
        if isinstance(self.api, GitHubTransport):
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

    def collect_source(self, req: SourceRequest, previous: SourceCursor | None) -> CollectionResult:
        self._spent = 0
        previous = previous or SourceCursor()
        prefix, repo = self._repository(req)
        ref = req.source.ref
        if ref in {None, "default"}:
            ref = repo["default_branch"]
        current = self._get(req, prefix + "/commits/" + quote(ref, safe="")).data["sha"]
        if previous.head == current and not previous.target:
            return CollectionResult(cursor=previous, coverage=coverage(req, current, True))
        warnings = []
        active = previous.target is not None
        target = previous.target if active else current
        remaining = list(previous.pending) if active else []
        page = previous.page if active else 1
        listed = previous.listed_complete if active else False
        base = previous.base_head if active else previous.head
        limitations, records, invalidated = [], [], []
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
                    email = data["commit"].get("author", {}).get("email", "")
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
        cursor = SourceCursor(
            head=target if range_complete else previous.head,
            target=None if range_complete else target,
            pending=remaining, page=page, listed_complete=listed, base_head=base,
            fingerprint=previous.fingerprint,
        )
        return CollectionResult(
            cursor=cursor, evidence=records, coverage=coverage(req, target, complete, limitations),
            invalidated_paths=sorted(set(invalidated)), warnings=warnings, changed=bool(records))

