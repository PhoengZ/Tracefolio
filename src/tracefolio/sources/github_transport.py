import json
import re
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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


class StrictRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Fail closed even for a rename; the owner can update the explicit locator.
        raise TracefolioError("github_redirect_blocked", exit_code=3)


class GitHubTransport:
    def __init__(self):
        self.response_limit = 16777216
        self.opener = build_opener(ProxyHandler({}), StrictRedirect())

    def get_json(self, path: str, params: dict[str, str] | None = None) -> ApiResponse:
        if (not re.fullmatch(r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./%~-]+)?",
                             path) or ".." in path.split("/")):
            raise TracefolioError("github_path_rejected")
        url = "https://api.github.com" + path
        if params:
            url += "?" + urlencode(params)
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

