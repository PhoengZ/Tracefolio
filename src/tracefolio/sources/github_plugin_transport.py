"""Opt-in, memory-only JSON-line bridge to a harness-owned GitHub plugin."""

import json
import re
import sys

from ..errors import TracefolioError
from .base import canonical
from .github_transport import ApiResponse, GitHubReadError, github_api_url


class GitHubPluginTransport:
    def __init__(self, *, input_stream=None, output_stream=None):
        self.input_stream = input_stream if input_stream is not None else sys.stdin
        self.output_stream = output_stream if output_stream is not None else sys.stdout
        self.response_limit = 16777216
        self._sequence = 0

    def get_json(self, path: str, params: dict[str, str] | None = None) -> ApiResponse:
        url = github_api_url(path, params)
        self._sequence += 1
        request_id = f"github-{self._sequence}"
        event = {"event": "github_request", "request_id": request_id, "method": "GET",
                 "url": url, "max_response_bytes": self.response_limit}
        try:
            self.output_stream.write(json.dumps(event, ensure_ascii=True) + "\n")
            self.output_stream.flush()
            # Prefer bytes even when the enclosing terminal uses a legacy Windows code page.
            stream = getattr(self.input_stream, "buffer", self.input_stream)
            wire_limit = self.response_limit + 65536  # Bounded envelope/header allowance.
            raw = stream.readline(wire_limit + 1)
            if not raw:
                raise TracefolioError("github_unavailable", exit_code=3)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if len(raw) > wire_limit:
                raise TracefolioError("github_response_budget", exit_code=3)
            if not raw.endswith(b"\n"):
                raise TracefolioError("github_plugin_protocol", exit_code=3)
            message = json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant)
            if not isinstance(message, dict) or message.get("request_id") != request_id:
                raise TracefolioError("github_plugin_protocol", exit_code=3)
            headers = message.get("headers", {})
            if not isinstance(headers, dict) or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
                raise TracefolioError("github_plugin_protocol", exit_code=3)
            headers = {k.lower(): v for k, v in headers.items()}
            if "error" in message:
                error = message["error"]
                status = error.get("status") if isinstance(error, dict) else None
                if type(status) is not int:
                    raise TracefolioError("github_plugin_protocol", exit_code=3)
                if status in {403, 429}:
                    value = headers.get("retry-after", "")
                    delay = min(int(value), 86400) if (
                        value.isascii() and value.isdigit() and len(value) <= 8) else None
                    raise GitHubReadError("github_rate_limited", delay)
                if status == 404:
                    raise GitHubReadError("github_not_found")
                if 300 <= status < 400:
                    raise TracefolioError("github_redirect_blocked", exit_code=3)
                raise TracefolioError("github_unavailable", exit_code=3)
            data = message.get("data")
            if not isinstance(data, (dict, list)):
                raise TracefolioError("github_invalid_json", exit_code=3)
            size = len(canonical(data))
            if size > self.response_limit:
                raise TracefolioError("github_response_budget", exit_code=3)
            if "link" not in headers and _full_page(path, params or {}, data):
                # The provider reads only rel=next and constructs its own next pinned request.
                headers["link"] = '<' + url + '>; rel="next"'
            return ApiResponse(data, headers, size)
        except (OSError, TimeoutError):
            raise TracefolioError("github_unavailable", exit_code=3) from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise TracefolioError("github_invalid_json", exit_code=3) from None


def _invalid_constant(value):
    raise ValueError


def _full_page(path, params, data):
    value = params.get("per_page", "")
    if not value.isascii() or not value.isdigit() or len(value) > 3:
        return False
    per_page = int(value)
    if not 1 <= per_page <= 100:
        return False
    items = None
    if re.fullmatch(r"/repos/[^/]+/[^/]+/commits", path) and isinstance(data, list):
        items = data
    elif re.fullmatch(r"/repos/[^/]+/[^/]+/compare/[^/]+", path) and isinstance(data, dict):
        items = data.get("commits")
    elif re.fullmatch(r"/repos/[^/]+/[^/]+/commits/[^/]+", path) and isinstance(data, dict):
        items = data.get("files")
    return isinstance(items, list) and len(items) >= per_page
