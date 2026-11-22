import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import TracefolioError


@dataclass(frozen=True)
class PrivacyPolicy:
    literals: tuple[str, ...] = ()
    epoch: str = "redaction-v2"


@dataclass(frozen=True)
class SanitizedText:
    text: str
    redactions: list[str]


def sanitize_text(text: str, policy: PrivacyPolicy) -> SanitizedText:
    labels = []
    for literal in sorted(set(policy.literals), key=len, reverse=True):
        if literal and re.search(re.escape(literal), text, re.IGNORECASE):
            text = re.sub(re.escape(literal), "[PRIVATE_VALUE]", text, flags=re.IGNORECASE)
            labels.append("private_value")
    patterns = [
        ("credential", r"\b(?:gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]{15,}|hf_[A-Za-z0-9]{15,}|sk-[A-Za-z0-9_-]{15,}|AKIA[A-Z0-9]{16})\b"),
        ("credential", r"(?im)\b(?:password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)\s*[:=]\s*[^\r\n]+"),
        ("url", r"(?i)\b(?:https?|postgresql?|redis|mongodb(?:\+srv)?|file)://[^\s<>\"'\])]+"),
        ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        ("absolute_path", r"(?i)\b[A-Z]:[\\/][^\s\"'<>\])]+"),
        ("absolute_path", r"(?<![\w:*?/])/(?!/)[^\s\"'<>\[\](){},;]+"),
        ("absolute_path", r"(?<!\\)\\\\[^\s\"'<>\[\](){},;]+"),
        ("private_ip", r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"),
    ]
    for label, pattern in patterns:
        text, count = re.subn(pattern, f"[REDACTED_{label.upper()}]", text)
        if count:
            labels.append(label)
    return SanitizedText(text, sorted(set(labels)))


def safe_relative(path: str) -> str:
    if (not path or "\\" in path or "\0" in path or ":" in path
            or path.startswith("/") or ".." in path.split("/")):
        raise TracefolioError("unsafe_path")
    normalized = PurePosixPath(path).as_posix()
    if normalized in {".", ""} or any(
            part.endswith((" ", ".")) or part.upper().split(".")[0] in
            {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
            for part in normalized.split("/")):
        raise TracefolioError("unsafe_path")
    return normalized


def mandatory_allowed(path: str) -> bool:
    try:
        parts = safe_relative(path).split("/")
    except TracefolioError:
        return False
    denied_dirs = {".git", ".venv", "venv", "node_modules", "vendor", "dist", "build",
                   "__pycache__", ".pytest_cache", ".next", ".claude", ".gemini", ".agent",
                   ".superpowers", ".vscode", ".ipynb_checkpoints"}
    denied_files = {"credentials.json", "credentials", "key.md", "id_rsa", "id_ed25519",
                    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
                    "uv.lock", "pipfile.lock", "cargo.lock", "go.sum", "composer.lock",
                    "gemfile.lock"}
    denied_suffixes = (".env", ".pem", ".key", ".p12", ".pfx", ".bak", ".bak1",
                       ".min.js", ".min.css", ".map", ".pyc", ".whl",
                       ".otf", ".ttf", ".woff", ".woff2", ".eot", ".ico",
                       ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                       ".pdf", ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".dylib")
    allowed_hidden = {".gitignore", ".dockerignore"}
    for part in parts:
        low = part.lower()
        if low in denied_dirs or (part.startswith(".") and low not in allowed_hidden):
            return False
        if (low.startswith(".env") or low.endswith(denied_suffixes)
                or low in denied_files or "recovery-code" in low):
            return False
    return True


def _match(path: str, pattern: str) -> bool:
    seen = {pattern}
    queue = [pattern]
    while queue:
        current = queue.pop()
        if fnmatch.fnmatchcase(path, current):
            return True
        if current.startswith("**/"):
            nxt = current[3:]
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
        if "/**/" in current:
            nxt = current.replace("/**/", "/", 1)
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def path_allowed(path: str, includes: list[str], excludes: list[str]) -> bool:
    return (mandatory_allowed(path) and any(_match(path, p) for p in includes)
            and not any(_match(path, p) for p in excludes))


def resolve_allowed_path(root: Path, relative_path: str) -> Path:
    if not mandatory_allowed(relative_path):
        raise TracefolioError("unsafe_path")
    try:
        root = root.resolve(strict=True)
        target = (root / safe_relative(relative_path)).resolve()
        if not target.is_relative_to(root):
            raise TracefolioError("path_escape")
        return target
    except (OSError, ValueError):
        raise TracefolioError("unsafe_path") from None


def secure_private_root(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)
            if path.stat().st_mode & 0o077:
                raise TracefolioError("private_permissions", exit_code=4)
    except OSError:
        raise TracefolioError("private_permissions", exit_code=4) from None

