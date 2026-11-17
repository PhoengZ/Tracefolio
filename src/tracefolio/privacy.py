import fnmatch
import os
import re
import subprocess
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
    allowed_hidden = {".gitignore", ".dockerignore"}
    for part in parts:
        low = part.lower()
        if low in denied_dirs or (part.startswith(".") and low not in allowed_hidden):
            return False
        if (low.startswith(".env") or low.endswith((".env", ".pem", ".key", ".p12", ".pfx", ".bak", ".bak1"))
                or low in {"credentials.json", "credentials", "key.md", "id_rsa", "id_ed25519"}
                or "recovery-code" in low):
            return False
    return True


def _match(path: str, pattern: str) -> bool:
    while pattern.startswith("**/"):
        if fnmatch.fnmatchcase(path, pattern):
            return True
        pattern = pattern[3:]
    return fnmatch.fnmatchcase(path, pattern)


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
        if os.name == "nt":
            # A fixed program; the private path is passed through a task-specific
            # process environment variable rather than interpolated as shell code.
            script = r"""
$ErrorActionPreference='Stop'
$p=$env:TRACEFOLIO_ACL_TARGET
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=Get-Acl -LiteralPath $p
if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'State directory must be owned by current user' }
$rules=@($acl.Access)
$alreadyPrivate=$acl.AreAccessRulesProtected -and $rules.Count -eq 1
if ($alreadyPrivate) {
  $r=$rules[0]
  $alreadyPrivate=($r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $sid.Value) -and ($r.AccessControlType -eq 'Allow') -and (($r.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq [Security.AccessControl.FileSystemRights]::FullControl) -and ($r.PropagationFlags -eq 'None') -and (($r.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ContainerInherit) -ne 0) -and (($r.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ObjectInherit) -ne 0)
}
if ($alreadyPrivate) { exit 0 }
$acl.SetAccessRuleProtection($true,$false)
foreach ($existingRule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($existingRule) }
$rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $p -AclObject $acl
"""
            env = dict(os.environ, TRACEFOLIO_ACL_TARGET=str(path))
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                env=env, capture_output=True, timeout=30, check=False,
            )
            if result.returncode:
                raise TracefolioError("private_permissions", exit_code=4)
        else:
            path.chmod(0o700)
            if path.stat().st_mode & 0o077:
                raise TracefolioError("private_permissions", exit_code=4)
    except (OSError, subprocess.SubprocessError):
        raise TracefolioError("private_permissions", exit_code=4) from None

