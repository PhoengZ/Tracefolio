import json
import re
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..errors import TracefolioError


@dataclass
class ApiResponse:
    data: dict | list
    headers: dict[str, str] = field(default_factory=dict)
    byte_count: int = 0


class GitHubReadError(TracefolioError):
    def __init__(self, code, retry_after=None):
        super().__init__(code, "Public source unavailable; resume later.", 3)
        self.retry_after = retry_after


def github_api_url(path: str, params: dict[str, str] | None = None) -> str:
    if (not isinstance(path, str) or not re.fullmatch(
            r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./%~-]+)?", path)):
        raise TracefolioError("github_path_rejected")
    if re.search(r"%(?![0-9A-Fa-f]{2})", path):
        raise TracefolioError("github_path_rejected")
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeError:
        raise TracefolioError("github_path_rejected") from None
    # Reserved characters remain encoded in the outgoing URL, so encoded #, ? and %
    # are literal ref/filename data. Inspect decoding layers only for path escapes.
    while True:
        if (any(part in {"", ".", ".."} for part in decoded.split("/")[1:])
                or "\\" in decoded
                or any(ord(char) < 32 or ord(char) == 127 for char in decoded)):
            raise TracefolioError("github_path_rejected")
        further = unquote(decoded)
        if further == decoded:
            break
        decoded = further
    if params and (not isinstance(params, dict) or any(
            key not in {"ref", "sha", "per_page", "page", "recursive"} or not isinstance(value, str)
            for key, value in params.items())):
        raise TracefolioError("github_path_rejected")
    return "https://api.github.com" + path + ("?" + urlencode(params) if params else "")


class StrictRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Fail closed even for a rename; the owner can update the explicit locator.
        raise TracefolioError("github_redirect_blocked", exit_code=3)


class GitHubTransport:
    def __init__(self):
        self.response_limit = 16777216
        self.opener = build_opener(ProxyHandler({}), StrictRedirect())

    def get_json(self, path: str, params: dict[str, str] | None = None) -> ApiResponse:
        url = github_api_url(path, params)
        request = Request(url, headers={"Accept": "application/vnd.github+json",
                                       "User-Agent": "Tracefolio/0.1",
                                       "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(self.response_limit + 1)
                if len(raw) > self.response_limit:
                    raise TracefolioError("github_response_budget", exit_code=3)
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, (dict, list)):
                    raise TypeError
                return ApiResponse(data, {k.lower(): v for k, v in response.headers.items()},
                                   len(raw))
        except HTTPError as error:
            if error.code in {403, 429}:
                value = error.headers.get("Retry-After", "") if error.headers else ""
                delay = min(int(value), 86400) if value.isascii() and value.isdigit() and len(value) <= 8 else None
                raise GitHubReadError("github_rate_limited", delay) from None
            if error.code == 404:
                raise GitHubReadError("github_not_found") from None
            raise TracefolioError("github_unavailable", exit_code=3) from None
        except (URLError, OSError, TimeoutError):
            raise TracefolioError("github_unavailable", exit_code=3) from None
        except (ValueError, TypeError, UnicodeError):
            raise TracefolioError("github_invalid_json", exit_code=3) from None

