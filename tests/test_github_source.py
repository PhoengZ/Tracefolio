import base64
import copy

import pytest

from tracefolio.contracts.routes import Limits, SourceRoute
from tracefolio.errors import TracefolioError
from tracefolio.privacy import PrivacyPolicy
from tracefolio.sources.base import SourceRequest
from tracefolio.sources.github_public import GitHubPublicProvider
from tracefolio.sources.github_transport import ApiResponse, GitHubTransport, StrictRedirect

SHA = "a" * 40
OLDER = "b" * 40


class FakeGitHub:
    def __init__(self):
        self.private = False
        self.fail_detail = False
        self.binary = False
        self.history_pages = [[{"sha": SHA}]]
        self.files_pages = [[{"filename": "main.py", "status": "added",
                             "patch": "@@ -0,0 +1 @@\n+checkpoint = 1"}]]
        self.calls = []

    def get_json(self, path, params=None):
        params = params or {}
        self.calls.append((path, params))
        page = int(params.get("page", 1))
        if path == "/repos/example/demo":
            return ApiResponse({"private": self.private, "default_branch": "main",
                                "full_name": "example/demo"})
        if path == "/repos/example/demo/commits/main":
            return ApiResponse({"sha": SHA})
        if path == "/repos/example/demo/commits":
            values = self.history_pages[page - 1]
            headers = {"link": '<https://api.github.com/next>; rel="next"'} if page < len(self.history_pages) else {}
            return ApiResponse(values, headers)
        if path.endswith(("/commits/" + SHA, "/commits/" + OLDER)):
            if self.fail_detail:
                raise TracefolioError("github_rate_limited", exit_code=3)
            files = copy.deepcopy(self.files_pages[page - 1])
            if self.binary:
                files[0].pop("patch", None)
            headers = {"link": '<https://api.github.com/next>; rel="next"'} if page < len(self.files_pages) else {}
            return ApiResponse({"sha": path.rsplit("/", 1)[1],
                                "commit": {"message": "vague update",
                                           "author": {"email": "author@example.invalid"}},
                                "parents": [], "files": files}, headers)
        if "/contents/" in path:
            text = b"checkpoint = 1\n"
            return ApiResponse({"type": "file", "size": len(text), "encoding": "base64",
                                "content": base64.b64encode(text).decode()})
        raise AssertionError("Unexpected fake API request: " + path)


def req():
    return SourceRequest("demo", SourceRoute(
        source_id="code", kind="github_public", repository_env="TRACEFOLIO_DEMO_REPO",
        include_paths=["**/*.py", "*.ipynb"]), "example/demo", Limits(), PrivacyPolicy())


def test_public_commit_message_diff_and_unknown_authorship():
    result = GitHubPublicProvider(FakeGitHub()).collect_source(req(), None)
    assert result.coverage.state == "complete"
    assert "vague update" in result.evidence[0].payload
    assert "checkpoint = 1" in result.evidence[0].payload
    assert result.evidence[0].attribution == "unverified"
    assert result.cursor.head == SHA


def test_rate_limit_retains_safe_retry_after_without_http_body():
    from email.message import Message
    from urllib.error import HTTPError
    transport = GitHubTransport()
    headers = Message()
    headers["Retry-After"] = "60"

    class FailingOpener:
        def open(self, request, timeout):
            assert not request.has_header("Authorization")
            raise HTTPError(request.full_url, 429, "PRIVATE_HTTP_BODY", headers, None)

    transport.opener = FailingOpener()
    with pytest.raises(TracefolioError) as caught:
        transport.get_json("/repos/example/demo", {})
    assert caught.value.retry_after == 60
    assert "PRIVATE_HTTP_BODY" not in str(caught.value)


def test_history_and_detail_pagination_preserve_coverage():
    api = FakeGitHub()
    api.history_pages = [[{"sha": SHA}], [{"sha": OLDER}]]
    provider = GitHubPublicProvider(api)
    first = provider.collect_source(req(), None)
    assert first.coverage.state == "partial" and first.cursor.head is None
    second = provider.collect_source(req(), first.cursor)
    assert second.cursor.head == SHA
    assert {e.revision for e in second.evidence} == {SHA, OLDER}
    api = FakeGitHub()
    api.files_pages.append([{"filename": "second.py", "status": "added",
                             "patch": "@@ -0,0 +1 @@\n+second = 2"}])
    result = GitHubPublicProvider(api).collect_source(req(), None)
    assert {e.source_relative_path for e in result.evidence} == {"main.py", "second.py"}


def test_rate_limit_preserves_pending_commit_and_last_checkpoint():
    api = FakeGitHub()
    api.fail_detail = True
    first = GitHubPublicProvider(api).collect_source(req(), None)
    assert first.coverage.state == "partial"
    assert first.cursor.head is None and SHA in first.cursor.pending
    api.fail_detail = False
    assert GitHubPublicProvider(api).collect_source(req(), first.cursor).cursor.head == SHA


def test_public_repository_becoming_private_does_not_fallback_to_auth_or_local():
    api = FakeGitHub()
    provider = GitHubPublicProvider(api)
    first = provider.collect_source(req(), None)
    api.private = True
    with pytest.raises(TracefolioError, match="github_not_public"):
        provider.collect_source(req(), first.cursor)
    assert first.cursor.head == SHA


def test_binary_missing_patch_is_a_gap():
    api = FakeGitHub()
    api.binary = True
    result = GitHubPublicProvider(api).collect_source(req(), None)
    assert result.coverage.state == "partial" and result.cursor.head is None


@pytest.mark.parametrize("locator", ["https://evil.invalid/repo", "example/demo?token=x",
                                    "example/../secret", "example/demo:443"])
def test_invalid_repository_rejected(locator):
    request = req()
    request.locator = locator
    with pytest.raises(TracefolioError, match="invalid_repository"):
        GitHubPublicProvider(FakeGitHub()).collect_source(request, None)


def test_transport_rejects_external_redirect_and_credentials():
    with pytest.raises(TracefolioError):
        GitHubTransport().get_json("https://evil.invalid/secret", {})
    with pytest.raises(TracefolioError):
        StrictRedirect().redirect_request(None, None, 302, "move", {},
                                          "https://evil.invalid/secret")


def test_rate_limit_stops_requests_and_preserves_remaining_queue():
    api = FakeGitHub()
    api.history_pages = [[{"sha": SHA}, {"sha": OLDER}]]
    api.fail_detail = True
    result = GitHubPublicProvider(api).collect_source(req(), None)
    details = [p for p, _ in api.calls if p.endswith((SHA, OLDER))]
    assert len(details) == 1
    assert set(result.cursor.pending) == {SHA, OLDER}


def test_inaccessible_comparison_base_reconciles_from_pinned_history():
    from tracefolio.contracts.evidence import SourceCursor
    api = FakeGitHub()
    original = api.get_json

    def read(path, params=None):
        if "/compare/" in path:
            raise TracefolioError("github_not_found", exit_code=3)
        return original(path, params)

    api.get_json = read
    result = GitHubPublicProvider(api).collect_source(req(), SourceCursor(head=OLDER))
    assert "history_reconciled" in result.warnings
    assert result.cursor.head == SHA


